/**
 * Content script: runs inside the target page and does the actual DOM work
 * (reading page state, clicking, filling, waiting, scrolling, pressing keys,
 * coordinate-based tapping, and typing into whatever currently has focus).
 * Injected on demand by background.js - not declared as a static content
 * script in manifest.json - so it only ever runs against the target site,
 * for as long as an action is in flight.
 *
 * Tab-level operations that content scripts cannot perform (navigate,
 * refresh, screenshot, upload, download, open_tab, close_tab, and the
 * network fetches behind check_links) are handled directly in
 * background.js instead; this script only supplies the link data
 * (read_href / collect_links) that those operations need from the page.
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
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          tag: el.tagName.toLowerCase(),
          type: el.getAttribute("type") || "",
          selector: buildSelector(el),
          text: (el.innerText || el.value || el.placeholder || "").trim().slice(0, 80),
          placeholder: el.getAttribute("placeholder") || "",
          name: el.getAttribute("name") || "",
          role: el.getAttribute("role") || "",
          disabled: !!el.disabled,
          // Viewport-relative center point, for the "tap" fallback action when
          // no reliable selector exists (e.g. canvas-drawn or map/chart UIs).
          x: Math.round(rect.left + rect.width / 2),
          y: Math.round(rect.top + rect.height / 2),
        };
      });

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
    const preview = (value ?? "").toString().trim().slice(0, 200);
    return result(true, `Read '${selector}': "${preview}"`, value);
  }

  function resolveHref(el) {
    const anchor = el.closest ? el.closest("a[href]") : null;
    const href = anchor ? anchor.getAttribute("href") : el.getAttribute && el.getAttribute("href");
    if (!href) return null;
    try {
      return new URL(href, window.location.href).href;
    } catch {
      return null;
    }
  }

  function doReadHref(selector) {
    const el = findElement(selector);
    if (!el) return result(false, `Element not found: '${selector}'.`, null, "not_found");
    const href = resolveHref(el);
    if (!href) return result(false, `'${selector}' is not a link (no resolvable href).`, null, "no_href");
    return result(true, `Resolved href for '${selector}'.`, href);
  }

  const SKIPPED_HREF_PREFIXES = ["#", "javascript:", "mailto:", "tel:"];
  const MAX_LINKS = 40;

  function doCollectLinks(selector) {
    const root = selector ? findElement(selector) : document;
    if (selector && !root) return result(false, `Element not found: '${selector}'.`, null, "not_found");

    const seen = new Set();
    const links = [];
    for (const anchor of root.querySelectorAll("a[href]")) {
      const raw = anchor.getAttribute("href") || "";
      if (!raw || SKIPPED_HREF_PREFIXES.some((prefix) => raw.startsWith(prefix))) continue;
      let absolute;
      try {
        absolute = new URL(raw, window.location.href).href;
      } catch {
        continue;
      }
      if (seen.has(absolute)) continue;
      seen.add(absolute);
      links.push({ href: absolute, text: (anchor.innerText || "").trim().slice(0, 80) });
      if (links.length >= MAX_LINKS) break;
    }
    return result(true, `Collected ${links.length} link(s).`, links);
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

  const SCROLL_STEP_PX = 800;
  const SCROLL_DIRECTIONS = {
    up: [0, -SCROLL_STEP_PX],
    down: [0, SCROLL_STEP_PX],
    left: [-SCROLL_STEP_PX, 0],
    right: [SCROLL_STEP_PX, 0],
  };

  function doScroll(selector, text) {
    if (selector) {
      const el = findElement(selector);
      if (!el) return result(false, `Element not found: '${selector}'.`, null, "not_found");
      el.scrollIntoView({ block: "center", inline: "center" });
      return result(true, `Scrolled '${selector}' into view.`);
    }

    const spec = (text || "").trim().toLowerCase();
    if (spec in SCROLL_DIRECTIONS) {
      const [dx, dy] = SCROLL_DIRECTIONS[spec];
      window.scrollBy(dx, dy);
      return result(true, `Scrolled ${spec}.`);
    }
    const amount = Number(spec);
    if (spec && Number.isFinite(amount)) {
      window.scrollBy(0, amount);
      return result(true, `Scrolled by ${amount}px.`);
    }

    window.scrollBy(0, SCROLL_STEP_PX);
    return result(true, "Scrolled the page down.");
  }

  function doTap(text) {
    const match = (text || "").match(/(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)/);
    if (!match) {
      return result(false, "tap requires text in the form 'x,y' (viewport pixel coordinates).", null, "missing_coordinates");
    }
    const x = Number(match[1]);
    const y = Number(match[2]);
    const el = document.elementFromPoint(x, y);
    if (!el) {
      return result(false, `No element found at (${x}, ${y}).`, null, "not_found");
    }
    el.scrollIntoView({ block: "center", inline: "center" });
    el.click();
    return result(true, `Tapped the element at (${x}, ${y}).`);
  }

  function doType(text) {
    const el = document.activeElement;
    if (!el || el === document.body) {
      return result(false, "No element is currently focused to type into - click/tap it first.", null, "no_focus");
    }
    const isContentEditable = !!el.isContentEditable;
    const isTextInput = el.tagName === "INPUT" || el.tagName === "TEXTAREA";
    if (!isContentEditable && !isTextInput) {
      return result(false, "The focused element is not editable.", null, "not_editable");
    }

    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const nativeSetter = isTextInput ? Object.getOwnPropertyDescriptor(proto, "value")?.set : null;

    for (const char of text ?? "") {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: char, bubbles: true, cancelable: true }));
      if (isContentEditable) {
        document.execCommand("insertText", false, char);
      } else {
        const newValue = (el.value ?? "") + char;
        if (nativeSetter) nativeSetter.call(el, newValue);
        else el.value = newValue;
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
      el.dispatchEvent(new KeyboardEvent("keyup", { key: char, bubbles: true, cancelable: true }));
    }
    if (isTextInput) el.dispatchEvent(new Event("change", { bubbles: true }));
    return result(true, `Typed into the focused ${el.tagName.toLowerCase()} element.`);
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
        return doScroll(selector, text);
      case "press":
        return doPress(selector, text);
      case "tap":
        return doTap(text);
      case "type":
        return doType(text);
      case "read_href":
        return doReadHref(selector);
      case "collect_links":
        return doCollectLinks(selector);
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
