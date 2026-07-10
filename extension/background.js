/**
 * Background service worker: owns the WebSocket connection to the Web Agent
 * server, and is the only place with access to privileged extension APIs
 * (chrome.tabs, chrome.debugger, chrome.downloads, chrome.notifications).
 *
 * Protocol (see agent/server.py for the authoritative definition):
 *   server -> extension: {type:"hello_ack"|"request"|"log"|"task_finished"|"error", ...}
 *   extension -> server: {type:"hello"|"start_task"|"stop_task"|"response", ...}
 */

importScripts("storage.js");

const RECONNECT_DELAY_MS = 5000;
const KEEPALIVE_ALARM = "web-agent-keepalive";
const MAX_LOG_ENTRIES = 200;
const APPROVAL_TIMEOUT_MS = 120000;
const DOWNLOAD_WAIT_MS = 8000;

let socket = null;
let connectionStatus = "disconnected"; // disconnected | connecting | connected | unconfigured
let targetUrl = "";
let currentTask = null; // { goal, running }
let reconnectTimer = null;
const pendingApprovals = new Map(); // notificationId -> resolve

// --------------------------------------------------------------------- //
// Lifecycle
// --------------------------------------------------------------------- //

chrome.runtime.onStartup.addListener(() => connectWebSocket());
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 1 });
  connectWebSocket();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === KEEPALIVE_ALARM) connectWebSocket();
});

// --------------------------------------------------------------------- //
// WebSocket connection management
// --------------------------------------------------------------------- //

async function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const { serverUrl, token } = await getAgentConfig();
  if (!serverUrl || !token) {
    connectionStatus = "unconfigured";
    broadcastStatus();
    return;
  }

  connectionStatus = "connecting";
  broadcastStatus();

  const wsUrl = `${serverUrl.replace(/\/+$/, "")}/ws?token=${encodeURIComponent(token)}`;
  try {
    socket = new WebSocket(wsUrl);
  } catch (err) {
    connectionStatus = "error";
    broadcastStatus();
    scheduleReconnect();
    return;
  }

  socket.addEventListener("open", () => {
    connectionStatus = "connected";
    pushLog("info", "Connected to server.");
    broadcastStatus();
  });
  socket.addEventListener("message", (event) => handleServerMessage(event.data));
  socket.addEventListener("close", () => {
    connectionStatus = "disconnected";
    broadcastStatus();
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    pushLog("error", "WebSocket error.");
  });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWebSocket();
  }, RECONNECT_DELAY_MS);
}

function disconnectWebSocket() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (socket) {
    socket.close();
    socket = null;
  }
  connectionStatus = "disconnected";
  broadcastStatus();
}

function sendToServer(obj) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(obj));
  }
}

// --------------------------------------------------------------------- //
// Server message handling
// --------------------------------------------------------------------- //

async function handleServerMessage(raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    pushLog("error", "Received malformed message from server.");
    return;
  }

  switch (message.type) {
    case "hello_ack":
      targetUrl = message.target_url || "";
      await chrome.storage.session.set({ targetUrl });
      pushLog("info", `Server ready. Target site: ${targetUrl}`);
      broadcastStatus();
      break;
    case "request":
      await handleServerRequest(message);
      break;
    case "log":
      pushLog(message.level || "info", message.message || "");
      break;
    case "task_finished":
      currentTask = null;
      pushLog(message.completed ? "success" : "warn", `Task finished (${message.stop_reason}).`);
      broadcastStatus();
      break;
    case "error":
      pushLog("error", message.message || "Server error.");
      break;
    default:
      console.warn("Unknown message type from server:", message.type);
  }
}

async function handleServerRequest(message) {
  const { request_id: requestId, action, params = {} } = message;
  let payload;
  try {
    if (action === "get_page_state") {
      payload = await getPageStateFromTab();
    } else if (action === "execute_action") {
      payload = await performAction(params.action, params.selector, params.text, params.is_final);
    } else if (action === "approval") {
      payload = await requestApproval(params.action, params.selector, params.text);
    } else {
      payload = { success: false, message: `Unknown request action: ${action}`, error: "unknown_request" };
    }
  } catch (err) {
    payload = { success: false, message: String(err && err.message ? err.message : err), error: "extension_exception" };
  }
  sendToServer({ type: "response", request_id: requestId, payload });
}

// --------------------------------------------------------------------- //
// Popup <-> background messaging
// --------------------------------------------------------------------- //

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.kind !== "popup") return false;

  (async () => {
    switch (message.type) {
      case "get_status":
        sendResponse(await getStatusSnapshot());
        break;
      case "reconnect":
        await connectWebSocket();
        sendResponse(await getStatusSnapshot());
        break;
      case "start_task":
        currentTask = { goal: message.goal, running: true };
        sendToServer({
          type: "start_task",
          goal: message.goal,
          dry_run: message.dryRun ?? null,
          approval_mode: message.approvalMode || null,
        });
        sendResponse({ ok: true });
        break;
      case "stop_task":
        sendToServer({ type: "stop_task" });
        sendResponse({ ok: true });
        break;
      default:
        sendResponse({ ok: false, error: "unknown popup message" });
    }
  })();
  return true; // async sendResponse
});

async function getStatusSnapshot() {
  const session = await chrome.storage.session.get(["logBuffer", "targetUrl"]);
  return {
    connectionStatus,
    targetUrl: session.targetUrl || targetUrl,
    currentTask,
    logs: session.logBuffer || [],
  };
}

function broadcastStatus() {
  getStatusSnapshot().then((status) => {
    chrome.runtime.sendMessage({ kind: "background", type: "status", status }).catch(() => {});
  });
}

async function pushLog(level, text) {
  const entry = { level, text, at: new Date().toISOString() };
  const session = await chrome.storage.session.get(["logBuffer"]);
  const buffer = (session.logBuffer || []).concat(entry).slice(-MAX_LOG_ENTRIES);
  await chrome.storage.session.set({ logBuffer: buffer });
  chrome.runtime.sendMessage({ kind: "background", type: "log", entry }).catch(() => {});
}

// --------------------------------------------------------------------- //
// Tab targeting
// --------------------------------------------------------------------- //

async function findTargetTab() {
  if (!targetUrl) {
    throw new Error("No target_url received from the server yet.");
  }
  const tabs = await chrome.tabs.query({});
  const match = tabs.find((t) => t.url && t.url.includes(targetUrl));
  if (match) return match;

  const created = await chrome.tabs.create({ url: targetUrl });
  await waitForTabComplete(created.id);
  return created;
}

function waitForTabComplete(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(new Error("Timed out waiting for the tab to finish loading."));
    }, timeoutMs);

    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete" && !settled) {
        settled = true;
        cleanup();
        resolve();
      }
    }
    function cleanup() {
      clearTimeout(timeout);
      chrome.tabs.onUpdated.removeListener(listener);
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId, (tab) => {
      if (tab && tab.status === "complete" && !settled) {
        settled = true;
        cleanup();
        resolve();
      }
    });
  });
}

async function ensureContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { kind: "ping" });
  } catch {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  }
}

// --------------------------------------------------------------------- //
// Action dispatch
// --------------------------------------------------------------------- //

async function getPageStateFromTab() {
  const tab = await findTargetTab();
  await ensureContentScript(tab.id);
  return await chrome.tabs.sendMessage(tab.id, { kind: "get_page_state" });
}

async function performAction(action, selector, text, _isFinal) {
  const tab = await findTargetTab();
  switch (action) {
    case "navigate":
      return await navigateTab(tab.id, text);
    case "refresh":
      return await refreshTab(tab.id);
    case "screenshot":
      return await captureScreenshot(tab);
    case "upload":
      return await uploadFile(tab.id, selector, text);
    case "download":
      return await downloadViaClick(tab.id, selector);
    default:
      await ensureContentScript(tab.id);
      return await chrome.tabs.sendMessage(tab.id, { kind: "execute_action", action, selector, text });
  }
}

async function navigateTab(tabId, url) {
  if (!url) return { success: false, message: "navigate requires a url.", error: "missing_url" };
  try {
    await chrome.tabs.update(tabId, { url });
    await waitForTabComplete(tabId);
    const tab = await chrome.tabs.get(tabId);
    return { success: true, message: `Navigated to '${url}'.`, data: tab.url };
  } catch (err) {
    return { success: false, message: `Failed to navigate to '${url}'.`, error: String(err) };
  }
}

async function refreshTab(tabId) {
  try {
    await chrome.tabs.reload(tabId);
    await waitForTabComplete(tabId);
    return { success: true, message: "Page refreshed." };
  } catch (err) {
    return { success: false, message: "Refresh failed.", error: String(err) };
  }
}

async function captureScreenshot(tab) {
  // chrome.tabs.captureVisibleTab only captures the visible viewport, not a
  // stitched full-page image - unlike a local Playwright session.
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    const base64 = dataUrl.split(",")[1];
    return { success: true, message: "Captured a screenshot of the visible viewport.", data: base64 };
  } catch (err) {
    return { success: false, message: "Screenshot failed.", error: String(err) };
  }
}

async function uploadFile(tabId, selector, filePath) {
  // Content scripts cannot programmatically set <input type=file>. We attach
  // the debugger API (the same mechanism Playwright/CDP use) just long enough
  // to set the file, then detach - Chrome shows an infobar while attached.
  if (!selector || !filePath) {
    return { success: false, message: "upload requires a selector and a file_path.", error: "missing_arguments" };
  }
  const debuggee = { tabId };
  try {
    await chrome.debugger.attach(debuggee, "1.3");
    await chrome.debugger.sendCommand(debuggee, "DOM.enable");
    const { root } = await chrome.debugger.sendCommand(debuggee, "DOM.getDocument", { depth: -1, pierce: true });
    const { nodeId } = await chrome.debugger.sendCommand(debuggee, "DOM.querySelector", {
      nodeId: root.nodeId,
      selector,
    });
    if (!nodeId) {
      return { success: false, message: `Element not found: '${selector}'.`, error: "not_found" };
    }
    await chrome.debugger.sendCommand(debuggee, "DOM.setFileInputFiles", { files: [filePath], nodeId });
    return { success: true, message: `Uploaded '${filePath}' via '${selector}'.` };
  } catch (err) {
    return { success: false, message: "Upload failed.", error: String(err) };
  } finally {
    try {
      await chrome.debugger.detach(debuggee);
    } catch {
      /* already detached */
    }
  }
}

async function downloadViaClick(tabId, selector) {
  if (!selector) {
    return { success: false, message: "download requires a selector that triggers the download.", error: "missing_selector" };
  }
  let downloadInfo = null;
  const listener = (item) => {
    downloadInfo = item;
  };
  chrome.downloads.onCreated.addListener(listener);
  try {
    await ensureContentScript(tabId);
    const clickResult = await chrome.tabs.sendMessage(tabId, { kind: "execute_action", action: "click", selector });
    if (!clickResult.success) return clickResult;

    const start = Date.now();
    while (!downloadInfo && Date.now() - start < DOWNLOAD_WAIT_MS) {
      await new Promise((r) => setTimeout(r, 200));
    }
    if (downloadInfo) {
      return {
        success: true,
        message: `Download started: '${downloadInfo.filename}'. The file was saved locally; the server cannot access its contents.`,
        data: downloadInfo.filename,
      };
    }
    return { success: true, message: "Click succeeded but no download event was observed in time." };
  } finally {
    chrome.downloads.onCreated.removeListener(listener);
  }
}

// --------------------------------------------------------------------- //
// Human approval, via a system notification with Approve/Deny buttons
// --------------------------------------------------------------------- //

function requestApproval(action, selector, text) {
  return new Promise((resolve) => {
    const notificationId = `approval-${Date.now()}`;
    pendingApprovals.set(notificationId, resolve);

    const target = selector || text || "";
    chrome.notifications.create(notificationId, {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "Web Agent - approval needed",
      message: `${action} on "${target}"${text && selector ? ` with "${text}"` : ""}`,
      buttons: [{ title: "Approve" }, { title: "Deny" }],
      requireInteraction: true,
    });

    setTimeout(() => {
      if (pendingApprovals.has(notificationId)) {
        pendingApprovals.delete(notificationId);
        chrome.notifications.clear(notificationId);
        resolve({ approved: false });
      }
    }, APPROVAL_TIMEOUT_MS);
  });
}

chrome.notifications.onButtonClicked.addListener((notificationId, buttonIndex) => {
  const resolve = pendingApprovals.get(notificationId);
  if (!resolve) return;
  pendingApprovals.delete(notificationId);
  chrome.notifications.clear(notificationId);
  resolve({ approved: buttonIndex === 0 });
});
