// Runs inside the web page. Gives Krify its "eyes".
// Injected on demand; safe to inject more than once.

if (!window.__krify) {
  const MAX_ELEMENTS = 150;
  const MAX_TEXT = 8000;

  const SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "textarea", "select", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=switch]", "[contenteditable=true]", "[onclick]",
  ].join(",");

  const clean = (s, max = 80) => {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > max ? s.slice(0, max) + "…" : s;
  };

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    if (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0.05;
  }

  function labelOf(el) {
    return clean(
      el.getAttribute("aria-label") ||
      (el.labels && el.labels[0] && el.labels[0].innerText) ||
      el.innerText ||
      el.getAttribute("placeholder") ||
      el.getAttribute("title") ||
      el.querySelector("img[alt]")?.alt ||
      el.getAttribute("name") ||
      (el.type === "submit" ? el.value : "")
    );
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const label = labelOf(el);
    if (tag === "select") {
      const current = el.options[el.selectedIndex]?.text || "";
      const options = [...el.options].slice(0, 15).map((o) => clean(o.text, 40)).join(", ");
      return `select "${label}" = "${clean(current, 40)}" (options: ${options})`;
    }
    if (tag === "input" || tag === "textarea") {
      const type = tag === "textarea" ? "textarea" : `input ${el.type || "text"}`;
      if (el.type === "checkbox" || el.type === "radio") return `${type} "${label}" (${el.checked ? "checked" : "unchecked"})`;
      return `${type} "${label}"` + (el.value ? ` (value: "${clean(el.value, 40)}")` : " (empty)");
    }
    const role = el.getAttribute("role") || (tag === "a" ? "link" : tag);
    return `${role} "${label}"`;
  }

  window.__krify = {
    getPageInfo() {
      return {
        url: location.href,
        title: document.title,
        scroll: `${Math.round(scrollY)} of ${Math.max(0, document.documentElement.scrollHeight - innerHeight)} px`,
      };
    },

    getElements() {
      document.querySelectorAll("[data-krify-id]").forEach((el) => el.removeAttribute("data-krify-id"));
      const lines = [];
      let id = 0;
      for (const el of document.querySelectorAll(SELECTOR)) {
        // Some sites (e.g. Amazon's "Sort by") hide the real <select> under a custom button.
        const visible = isVisible(el) || (el.tagName === "SELECT" && el.parentElement && isVisible(el.parentElement));
        if (!visible || el.disabled) continue;
        const text = describe(el);
        if (/"\s*"/.test(text) && !/input|textarea|select/.test(text)) continue; // skip unlabeled
        id += 1;
        el.setAttribute("data-krify-id", String(id));
        lines.push(`[${id}] ${text}`);
        if (id >= MAX_ELEMENTS) { lines.push("(more elements not shown)"); break; }
      }
      const below = document.documentElement.scrollHeight - (scrollY + innerHeight);
      if (below > 50) lines.push("(more content below – scroll down to see it)");
      return lines.join("\n") || "(no interactive elements visible)";
    },

    readText() {
      const text = (document.body?.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
      return text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) + "\n…(text cut, page is long)" : text;
    },

    // ---------- actions ----------

    click(id) {
      const el = find(id);
      el.scrollIntoView({ block: "center", behavior: "instant" });
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new PointerEvent("pointerdown", opts));
      el.dispatchEvent(new MouseEvent("mousedown", opts));
      el.dispatchEvent(new PointerEvent("pointerup", opts));
      el.dispatchEvent(new MouseEvent("mouseup", opts));
      el.focus?.();
      el.click();
      return `Clicked [${id}] ${describe(el)}`;
    },

    type(id, text) {
      const el = find(id);
      el.scrollIntoView({ block: "center", behavior: "instant" });
      el.focus();
      if (el.isContentEditable) {
        el.textContent = text;
      } else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        // Use the native setter so sites built with React/Vue notice the change.
        const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
      } else {
        throw new Error(`[${id}] is not a text box.`);
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return `Typed "${text}" into [${id}]`;
    },

    pressKey(key) {
      const target = document.activeElement || document.body;
      const opts = { key, code: key, bubbles: true, cancelable: true };
      target.dispatchEvent(new KeyboardEvent("keydown", opts));
      target.dispatchEvent(new KeyboardEvent("keyup", opts));
      // Sites ignore fake key events, so Enter in a form submits the form directly.
      if (key === "Enter" && target.form) target.form.requestSubmit();
      return `Pressed ${key}`;
    },

    scroll(direction) {
      const before = scrollY;
      scrollBy({ top: (direction === "up" ? -1 : 1) * innerHeight * 0.8, behavior: "instant" });
      return scrollY === before ? `Can't scroll ${direction} any more` : `Scrolled ${direction}`;
    },

    selectOption(id, option) {
      const el = find(id);
      if (el.tagName !== "SELECT") throw new Error(`[${id}] is not a dropdown. Try clicking it instead.`);
      const want = option.trim().toLowerCase();
      const match =
        [...el.options].find((o) => o.text.trim().toLowerCase() === want) ||
        [...el.options].find((o) => o.text.toLowerCase().includes(want));
      if (!match) throw new Error(`Option "${option}" not found in [${id}].`);
      el.value = match.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return `Selected "${match.text.trim()}" in [${id}]`;
    },
  };

  function find(id) {
    const el = document.querySelector(`[data-krify-id="${id}"]`);
    if (!el) throw new Error(`Element [${id}] not found. The page may have changed – get the elements again.`);
    return el;
  }
}
