/** Options page: persist the server URL + auth token, then ask background.js to reconnect. */

const serverUrlEl = document.getElementById("serverUrl");
const tokenEl = document.getElementById("token");
const saveBtn = document.getElementById("saveBtn");
const savedEl = document.getElementById("saved");

async function load() {
  const { serverUrl, token } = await getAgentConfig();
  serverUrlEl.value = serverUrl;
  tokenEl.value = token;
}

saveBtn.addEventListener("click", async () => {
  await setAgentConfig({ serverUrl: serverUrlEl.value.trim(), token: tokenEl.value.trim() });
  await chrome.runtime.sendMessage({ kind: "popup", type: "reconnect" });
  savedEl.style.visibility = "visible";
  setTimeout(() => {
    savedEl.style.visibility = "hidden";
  }, 2000);
});

load();
