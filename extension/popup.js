/**
 * Popup UI: lets the user type a goal, pick safety modes, start/stop a task,
 * and watch a live log fed by the background service worker.
 */

const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const targetUrlEl = document.getElementById("targetUrl");
const goalEl = document.getElementById("goal");
const approvalModeEl = document.getElementById("approvalMode");
const dryRunEl = document.getElementById("dryRun");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const logEl = document.getElementById("log");
const optionsLink = document.getElementById("optionsLink");

const STATUS_LABELS = {
  connected: "מחובר",
  connecting: "מתחבר…",
  disconnected: "מנותק",
  unconfigured: "לא מוגדר",
  error: "שגיאת חיבור",
};

function sendToBackground(message) {
  return chrome.runtime.sendMessage({ kind: "popup", ...message });
}

function renderStatus(status) {
  const state = status.connectionStatus || "disconnected";
  statusDot.className = `status-dot ${state}`;
  statusText.textContent = STATUS_LABELS[state] || state;
  targetUrlEl.textContent = status.targetUrl
    ? `אתר יעד: ${status.targetUrl}`
    : state === "unconfigured"
      ? "יש להגדיר כתובת שרת וטוקן בהגדרות השרת."
      : "ממתין לשרת…";

  const running = !!(status.currentTask && status.currentTask.running);
  startBtn.disabled = running || state !== "connected";
  stopBtn.disabled = !running;
  if (running) {
    goalEl.value = status.currentTask.goal;
  }
}

function appendLogEntry(entry) {
  const div = document.createElement("div");
  div.className = `entry ${entry.level || "info"}`;
  const time = new Date(entry.at).toLocaleTimeString();
  div.textContent = `[${time}] ${entry.text}`;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function renderLogs(entries) {
  logEl.innerHTML = "";
  entries.forEach(appendLogEntry);
}

async function refreshStatus() {
  const status = await sendToBackground({ type: "get_status" });
  renderStatus(status);
  renderLogs(status.logs || []);
}

startBtn.addEventListener("click", async () => {
  const goal = goalEl.value.trim();
  if (!goal) {
    goalEl.focus();
    return;
  }
  await sendToBackground({
    type: "start_task",
    goal,
    approvalMode: approvalModeEl.value,
    dryRun: dryRunEl.checked,
  });
  startBtn.disabled = true;
  stopBtn.disabled = false;
});

stopBtn.addEventListener("click", async () => {
  await sendToBackground({ type: "stop_task" });
  stopBtn.disabled = true;
});

optionsLink.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.kind !== "background") return;
  if (message.type === "status") {
    renderStatus(message.status);
  } else if (message.type === "log") {
    appendLogEntry(message.entry);
  }
});

refreshStatus();
