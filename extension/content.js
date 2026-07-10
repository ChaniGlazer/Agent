/**
 * Content script: runs inside the target page and does the actual DOM work
 * (reading page state, clicking, filling, waiting, scrolling, pressing keys).
 * Injected on demand by background.js - not declared as a static content
 * script in manifest.json - so it only ever runs against the target site,
 * for as long as an action is in flight.
 *
 * Tab-level operations that content scripts cannot perform (navigate,
 * refresh, screenshot, upload, download) are handled directly in
 * background.js instead.
 */

(() => {
  if (window.__webAgentContentScriptInstalled) {
    return;
  }
  window.__webAgentContentScriptInstalled = true;

  const DEFAULT_WAIT_TIMEOUT_MS = 10000;
  const POLL_INTERVAL_MS = 100;

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function cssEscape(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  function buildSelector(el) {
    if (el.id) return "#" + cssEscape(el.id);
    const testId = el.getAttribute("data-testid");
    if (testId) return `[data-testid="${testId}"]`;
    const name = el.getAttribute("name");
    if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) return `${el.tagName.toLowerCase()}[aria-label="${ariaLabel}"]`;
    const path = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      let index = 1;
      let sibling = node;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === node.tagName) index += 1;
      }
      path.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${index})`);
      node = node.parentElement;
    }
    return path.length ? path.join(" > ") : el.tagName.toLowerCase();
  }

  function getPageState() {
    const SELECTOR = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [contenteditable="true"]';
    const elements = Array.from(document.querySelectorAll(SELECTOR))
      .filter(isVisible)
      .slice(0, 60)
      .map((el) => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute("type") || "",
        selector: buildSelector(el),
        text: (el.innerText || el.value || el.placeholder || "").trim().slice(0, 80),
        placeholder: el.getAttribute("placeholder") || "",
        name: el.getAttribute("name") || "",
        role: el.getAttribute("role") || "",
        disabled: !!el.disabled,
      }));

    return {
      url: window.location.href,
      title: document.title,
      visible_text: (document.body ? document.body.innerText : "").slice(0, 2000),
      elements,
    };
  }

  function findElement(selector) {
    if (!selector) return null;
    return document.querySelector(selector);
  }

  function result(success, message, data, error) {
    return { success, message, data: data ?? null, error: error ?? null };
  }

  function doClick(selector) {
    const el = findElement(selector);
    if (!el) return result(false, `Element not found: '${selector}'.`, null, "not_found");
    el.scrollIntoView({ block: "center", inline: "center" });
    el.click();
    return result(true, `Clicked element '${selector}'.`);
  }

  function doFill(selector, text) {
    const el = findElement(selector);
    if (!el) return result(false, `Element not found: '${selector}'.`, null, "not_found");
    el.scrollIntoView({ block: "center", inline: "center" });
    el.focus();
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (nativeSetter) {
      nativeSetter.call(el, text ?? "");
    } else {
      el.value = text ?? "";
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return result(true, `Filled '${selector}' with the given text.`);
  }

  function doRead(selector) {
    const el = findElement(selector);
    if (!el) return result(false, `Element not found: '${selector}'.`, null, "not_found");
    const tag = el.tagName.toLowerCase();
    const value = tag === "input" || tag === "textarea" || tag === "select" ? el.value : el.innerText;
    return result(true, `Read content of '${selector}'.`, value);
  }

  function doWait(selector) {
    return new Promise((resolve) => {
      const start = Date.now();
      const timer = setInterval(() => {
        const el = findElement(selector);
        if (el && isVisible(el)) {
          clearInterval(timer);
          resolve(result(true, `Element '${selector}' became visible.`));
          return;
        }
        if (Date.now() - start > DEFAULT_WAIT_TIMEOUT_MS) {
          clearInterval(timer);
          resolve(result(false, `Timed out waiting for '${selector}' to become visible.`, null, "timeout"));
        }
      }, POLL_INTERVAL_MS);
    });
  }

  function doScroll(selector) {
    if (selector) {
      const el = findElement(selector);
      if (!el) return result(false, `Element not found: '${selector}'.`, null, "not_found");
      el.scrollIntoView({ block: "center", inline: "center" });
      return result(true, `Scrolled '${selector}' into view.`);
    }
    window.scrollBy(0, 1000);
    return result(true, "Scrolled the page down.");
  }

  function doPress(selector, key) {
    if (!key) return result(false, "press requires a key.", null, "missing_key");
    const target = (selector && findElement(selector)) || document.activeElement || document.body;
    if (selector && !findElement(selector)) {
      return result(false, `Element not found: '${selector}'.`, null, "not_found");
    }
    for (const type of ["keydown", "keypress", "keyup"]) {
      target.dispatchEvent(new KeyboardEvent(type, { key, bubbles: true, cancelable: true }));
    }
    return result(true, `Pressed key '${key}'.`);
  }

  async function executeAction(action, selector, text) {
    switch (action) {
      case "click":
        return doClick(selector);
      case "fill":
        return doFill(selector, text);
      case "read":
        return doRead(selector);
      case "wait":
        return await doWait(selector);
      case "scroll":
        return doScroll(selector);
      case "press":
        return doPress(selector, text);
      default:
        return result(false, `Action '${action}' is not handled by the content script.`, null, "unsupported_here");
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message !== "object") return false;

    if (message.kind === "ping") {
      sendResponse({ ok: true });
      return false;
    }
    if (message.kind === "get_page_state") {
      sendResponse(getPageState());
      return false;
    }
    if (message.kind === "execute_action") {
      executeAction(message.action, message.selector, message.text).then(sendResponse);
      return true; // keep the message channel open for the async response
    }
    return false;
  });
})();
