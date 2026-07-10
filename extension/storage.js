/**
 * Small chrome.storage wrapper shared by background.js, popup.js, and options.js.
 * Loaded via <script src="storage.js"> in the popup/options pages, and via
 * importScripts('storage.js') in the background service worker.
 */

const AGENT_STORAGE_KEYS = {
  SERVER_URL: "serverUrl",
  TOKEN: "authToken",
};

/** Read the configured server URL + auth token from local storage. */
function getAgentConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(
      [AGENT_STORAGE_KEYS.SERVER_URL, AGENT_STORAGE_KEYS.TOKEN],
      (items) => {
        resolve({
          serverUrl: items[AGENT_STORAGE_KEYS.SERVER_URL] || "",
          token: items[AGENT_STORAGE_KEYS.TOKEN] || "",
        });
      }
    );
  });
}

/** Persist the server URL + auth token to local storage. */
function setAgentConfig({ serverUrl, token }) {
  return new Promise((resolve) => {
    chrome.storage.local.set(
      {
        [AGENT_STORAGE_KEYS.SERVER_URL]: serverUrl,
        [AGENT_STORAGE_KEYS.TOKEN]: token,
      },
      resolve
    );
  });
}
