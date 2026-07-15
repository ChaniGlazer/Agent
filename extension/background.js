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
const OPEN_TAB_CLICK_TIMEOUT_MS = 6000;
// Sent as a WebSocket subprotocol instead of a `?token=` query parameter,
// so the auth token never appears in the connection URL - some content
// filters inspect and block URLs that carry a token in plain sight.
const AUTH_SUBPROTOCOL_PREFIX = "agent-token.";

let socket = null;
let connectionStatus = "disconnected"; // disconnected | connecting | connected | unconfigured
let targetUrl = "";
let currentTask = null; // { goal, running }
let reconnectTimer = null;
const pendingApprovals = new Map(); // notificationId -> resolve

// Which tab actions currently apply to. null means "resolve from targetUrl"
// (the normal case); open_tab pushes the previous tab here and switches to
// the new one, close_tab pops back. Reset whenever a new task starts.
let workingTabId = null;
const tabStack = [];
const LINK_CHECK_TIMEOUT_MS = 8000;
const LINK_CHECK_CONCURRENCY = 5;

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

  const wsUrl = `${serverUrl.replace(/\/+$/, "")}/ws`;
  try {
    socket = new WebSocket(wsUrl, [`${AUTH_SUBPROTOCOL_PREFIX}${token}`]);
  } catch (err) {
    connectionStatus = "error";
    broadcastStatus();
    scheduleReconnect();
    return;
  }

  socket.addEventListener("open", () => {
    connectionStatus = "connected";
    pushLog("info", "מחובר לשרת.");
    broadcastStatus();
  });
  socket.addEventListener("message", (event) => handleServerMessage(event.data));
  socket.addEventListener("close", () => {
    connectionStatus = "disconnected";
    broadcastStatus();
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    pushLog("error", "שגיאת תקשורת (WebSocket).");
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
    pushLog("error", "התקבלה הודעה לא תקינה מהשרת.");
    return;
  }

  switch (message.type) {
    case "hello_ack":
      targetUrl = message.target_url || "";
      await chrome.storage.session.set({ targetUrl });
      pushLog("info", `השרת מוכן. אתר היעד: ${targetUrl}`);
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
      pushLog(message.completed ? "success" : "warn", `המשימה הסתיימה (${message.stop_reason}).`);
      broadcastStatus();
      break;
    case "error":
      pushLog("error", message.message || "שגיאת שרת.");
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
        workingTabId = null;
        tabStack.length = 0;
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
  if (workingTabId !== null) {
    try {
      return await chrome.tabs.get(workingTabId);
    } catch {
      // The tab was closed outside our control (e.g. by the user); fall
      // back to the normal target_url resolution below.
      workingTabId = null;
    }
  }

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
    case "open_tab":
      return await openTab(tab.id, selector);
    case "close_tab":
      return await closeTab();
    case "check_links":
      return await checkLinks(tab.id, selector);
    default:
      await ensureContentScript(tab.id);
      return await chrome.tabs.sendMessage(tab.id, { kind: "execute_action", action, selector, text });
  }
}

async function openTab(currentTabId, selector) {
  if (!selector) return { success: false, message: "open_tab requires a selector.", error: "missing_selector" };
  await ensureContentScript(currentTabId);

  // Fast path: the selector resolves to a real <a href>, so we can open it
  // directly without touching the page.
  const hrefResult = await chrome.tabs.sendMessage(currentTabId, {
    kind: "execute_action",
    action: "read_href",
    selector,
  });
  if (hrefResult.success && hrefResult.data) {
    try {
      const newTab = await chrome.tabs.create({ url: hrefResult.data, active: true, openerTabId: currentTabId });
      await waitForTabComplete(newTab.id);
      tabStack.push(currentTabId);
      workingTabId = newTab.id;
      return { success: true, message: `Opened '${hrefResult.data}' in a new tab.`, data: hrefResult.data };
    } catch (err) {
      return { success: false, message: "Failed to open a new tab.", error: String(err) };
    }
  }

  // Fallback: many inquiry/list UIs render rows as clickable <div>/<tr>
  // elements with a JS click handler instead of a plain <a href> - there is
  // no href to resolve. Click the element for real and watch for the tab it
  // opens, the same way a person clicking the row with a mouse would.
  const newTabPromise = waitForNewTab(OPEN_TAB_CLICK_TIMEOUT_MS);
  const clickResult = await chrome.tabs.sendMessage(currentTabId, {
    kind: "execute_action",
    action: "click",
    selector,
  });
  if (!clickResult.success) {
    return {
      success: false,
      message: clickResult.message || `Could not click '${selector}' to open it.`,
      error: clickResult.error || "click_failed",
    };
  }

  const newTabId = await newTabPromise;
  if (newTabId === null) {
    return {
      success: false,
      message:
        `Clicked '${selector}' but no new tab opened within ${OPEN_TAB_CLICK_TIMEOUT_MS / 1000}s. ` +
        "If this row opens the item in the same tab instead of a new one, use 'click' directly instead of 'open_tab'.",
      error: "no_new_tab",
    };
  }
  try {
    await waitForTabComplete(newTabId);
  } catch {
    /* the tab may still be usable even if it never reaches "complete" in time */
  }
  tabStack.push(currentTabId);
  workingTabId = newTabId;
  return { success: true, message: `Clicked '${selector}' and switched to the tab it opened.` };
}

function waitForNewTab(timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      chrome.tabs.onCreated.removeListener(listener);
      resolve(null);
    }, timeoutMs);

    function listener(tab) {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      chrome.tabs.onCreated.removeListener(listener);
      resolve(tab.id);
    }
    chrome.tabs.onCreated.addListener(listener);
  });
}

async function closeTab() {
  if (workingTabId === null || tabStack.length === 0) {
    return { success: false, message: "There is no extra tab to close.", error: "no_tab_to_close" };
  }
  const toClose = workingTabId;
  const previous = tabStack.pop();
  try {
    await chrome.tabs.remove(toClose);
  } catch {
    /* already closed, e.g. by the user */
  }
  workingTabId = previous;
  return { success: true, message: "Closed the tab and returned to the previous one." };
}

async function checkLinks(tabId, selector) {
  await ensureContentScript(tabId);
  const collected = await chrome.tabs.sendMessage(tabId, { kind: "execute_action", action: "collect_links", selector });
  if (!collected.success) return collected;

  const links = collected.data || [];
  if (links.length === 0) {
    return { success: true, message: "No links found to check.", data: { total: 0, ok_count: 0, broken: [] } };
  }

  const broken = [];
  let okCount = 0;
  let index = 0;
  async function worker() {
    while (index < links.length) {
      const link = links[index++];
      const outcome = await checkOneLink(link.href);
      if (outcome.ok) okCount += 1;
      else broken.push({ href: link.href, text: link.text, reason: outcome.reason });
    }
  }
  await Promise.all(Array.from({ length: Math.min(LINK_CHECK_CONCURRENCY, links.length) }, worker));

  const preview = broken
    .slice(0, 10)
    .map((b) => `${b.href} (${b.reason})`)
    .join("; ");
  const message = broken.length
    ? `Checked ${links.length} link(s): ${broken.length} broken - ${preview}${broken.length > 10 ? "; ..." : ""}`
    : `Checked ${links.length} link(s): all OK.`;
  return { success: true, message, data: { total: links.length, ok_count: okCount, broken } };
}

async function checkOneLink(href) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), LINK_CHECK_TIMEOUT_MS);
  try {
    let response = await fetch(href, { method: "HEAD", redirect: "follow", signal: controller.signal });
    if (response.status === 405 || response.status === 501) {
      response = await fetch(href, { method: "GET", redirect: "follow", signal: controller.signal });
    }
    return { ok: response.ok, reason: response.ok ? null : `HTTP ${response.status}` };
  } catch (err) {
    return { ok: false, reason: err && err.name === "AbortError" ? "timeout" : String(err) };
  } finally {
    clearTimeout(timeout);
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
      title: "סוכן האינטרנט - נדרש אישור",
      message: `פעולת "${action}" על "${target}"${text && selector ? ` עם הטקסט "${text}"` : ""}`,
      buttons: [{ title: "אשר" }, { title: "דחה" }],
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
