// Runs inside the web page. Gives Krify its "eyes".
// Injected on demand; safe to inject more than once.

if (!window.__krify) {
  const MAX_ELEMENTS = 100; // visible-on-screen only; fewer = smaller AI requests
  const MAX_TEXT = 2500; // keep AI requests small (free token limits)
  const MAX_SCREEN_TEXT = 3000;

  const SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "textarea", "select", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=switch]", "[contenteditable=true]", "[onclick]",
  ].join(",");

  const clean = (s, max = 50) => {
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
      const options = [...el.options].slice(0, 12).map((o) => clean(o.text, 30)).join(", ");
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
      const seenLinks = new Set();
      let id = 0;
      for (const el of document.querySelectorAll(SELECTOR)) {
        // Some sites (e.g. Amazon's "Sort by") hide the real <select> under a custom button.
        const visible = isVisible(el) || (el.tagName === "SELECT" && el.parentElement && isVisible(el.parentElement));
        if (!visible || el.disabled) continue;
        // Fewer tokens: inside an already-listed link/button = same click; same link target = listed once
        // (shops link each product from its picture AND its title).
        if (el.parentElement?.closest("[data-krify-id]")) continue;
        if (el.tagName === "A" && el.href) {
          if (seenLinks.has(el.href)) continue;
          seenLinks.add(el.href);
        }
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

    // "screen" (default): only the text visible right now, line by line – prices, answers, etc.
    // "page": the whole page from the top (long, cut at MAX_TEXT).
    readText(scope = "screen") {
      if (scope === "page") {
        const text = (document.body?.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
        return text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) + "\n…(text cut, page is long)" : text;
      }
      const lines = [];
      let line = "";
      let lineTop = null;
      let size = 0;
      const range = document.createRange();
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const text = node.nodeValue.replace(/\s+/g, " ").trim();
        const parent = node.parentElement;
        if (!text || !parent || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
        range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > innerHeight) continue; // off screen
        if (parent.checkVisibility && !parent.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
        // Text at about the same height belongs to the same visual line.
        if (lineTop !== null && Math.abs(r.top - lineTop) > 6) {
          lines.push(line);
          line = "";
        }
        line = line ? `${line} ${text}` : text;
        lineTop = r.top;
        size += text.length + 1;
        if (size > MAX_SCREEN_TEXT) {
          line += " …(more on screen not shown)";
          break;
        }
      }
      if (line) lines.push(line);
      return lines.join("\n") || "(no text visible on screen – try scrolling)";
    },

    // ---------- actions ----------
    // When `animate` is on, a visible Krify cursor moves, clicks and types like a person.

    animate: false,

    // Glowing border around the page while a task runs.
    setWorking(on) {
      if (!on && !view.root) return "ok"; // nothing shown, nothing to hide
      overlay().querySelector(".working").classList.toggle("on", on);
      return "ok";
    },

    async click(id) {
      const el = find(id);
      await bringIntoView(el);
      if (this.animate) {
        const { x, y } = await moveCursorTo(el);
        glow(el);
        ripple(x, y);
        await sleep(150);
      }
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new PointerEvent("pointerdown", opts));
      el.dispatchEvent(new MouseEvent("mousedown", opts));
      el.dispatchEvent(new PointerEvent("pointerup", opts));
      el.dispatchEvent(new MouseEvent("mouseup", opts));
      el.focus?.();
      el.click();
      return `Clicked [${id}] ${describe(el)}`;
    },

    async type(id, text, submit = false) {
      const el = find(id);
      const isField = el.tagName === "INPUT" || el.tagName === "TEXTAREA";
      if (!isField && !el.isContentEditable) throw new Error(`[${id}] is not a text box.`);
      await bringIntoView(el);
      if (this.animate) {
        const { x, y } = await moveCursorTo(el);
        glow(el);
        ripple(x, y);
      }
      el.focus();

      // Use the native setter so sites built with React/Vue notice the change.
      const setValue = (value) => {
        if (el.isContentEditable) {
          el.textContent = value;
        } else {
          const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
          Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
      };

      if (this.animate) {
        setValue("");
        const delay = Math.min(60, 1500 / Math.max(text.length, 1)); // long text still finishes in ~1.5s
        for (let i = 1; i <= text.length; i++) {
          setValue(text.slice(0, i));
          await sleep(delay);
        }
      } else {
        setValue(text);
      }
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (submit) {
        await this.pressKey("Enter"); // search in one step instead of two
        return `Typed "${text}" into [${id}] and pressed Enter`;
      }
      return `Typed "${text}" into [${id}]`;
    },

    async pressKey(key) {
      const target = document.activeElement || document.body;
      if (this.animate) {
        showKey(key);
        await sleep(400);
      }
      const opts = { key, code: key, bubbles: true, cancelable: true };
      target.dispatchEvent(new KeyboardEvent("keydown", opts));
      target.dispatchEvent(new KeyboardEvent("keyup", opts));
      // Sites ignore fake key events, so Enter in a form submits the form directly.
      if (key === "Enter" && target.form) target.form.requestSubmit();
      return `Pressed ${key}`;
    },

    async scroll(direction) {
      const before = scrollY;
      scrollBy({ top: (direction === "up" ? -1 : 1) * innerHeight * 0.8, behavior: this.animate ? "smooth" : "instant" });
      if (this.animate) await sleep(600);
      return scrollY === before ? `Can't scroll ${direction} any more` : `Scrolled ${direction}`;
    },

    async selectOption(id, option) {
      const el = find(id);
      if (el.tagName !== "SELECT") throw new Error(`[${id}] is not a dropdown. Try clicking it instead.`);
      const want = option.trim().toLowerCase();
      const match =
        [...el.options].find((o) => o.text.trim().toLowerCase() === want) ||
        [...el.options].find((o) => o.text.toLowerCase().includes(want));
      if (!match) throw new Error(`Option "${option}" not found in [${id}].`);
      if (this.animate) {
        const target = isVisible(el) ? el : el.parentElement; // hidden <select>: point at its visible wrapper
        await bringIntoView(target);
        const { x, y } = await moveCursorTo(target);
        glow(target);
        ripple(x, y);
        await sleep(300);
      }
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

  // ---------- visible cursor (drawn on the page; the real mouse can't be moved) ----------

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const view = { root: null, cursor: null, hideTimer: null };

  async function bringIntoView(el) {
    const smooth = window.__krify.animate;
    el.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "instant" });
    if (smooth) await sleep(350);
  }

  // A shadow-DOM layer on top of the page, so page styles can't break it and it never blocks clicks.
  function overlay() {
    if (view.root && view.root.host.isConnected) return view.root;
    const host = document.createElement("krify-overlay");
    host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        .cursor { position: fixed; left: 0; top: 0; display: flex; align-items: flex-start; gap: 2px;
          transform: translate(${innerWidth / 2}px, ${innerHeight / 2}px);
          transition: transform .55s cubic-bezier(.45,.05,.25,1), opacity .3s; opacity: 0; }
        .cursor svg { width: 26px; height: 26px; filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
        .tag { margin-top: 18px; padding: 1px 7px; border-radius: 10px; background: #5b5bd6; color: #fff;
          font: 600 11px/18px system-ui, sans-serif; }
        .glow { position: fixed; border: 2px solid #5b5bd6; border-radius: 6px;
          box-shadow: 0 0 0 4px rgba(91,91,214,.25); transition: opacity .5s; }
        .ripple { position: fixed; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%;
          background: rgba(91,91,214,.55); animation: ripple .55s ease-out forwards; }
        @keyframes ripple { to { transform: scale(3.5); opacity: 0; } }
        .key { position: fixed; padding: 3px 9px; border-radius: 6px; background: #1d1f24; color: #fff;
          font: 600 12px/18px system-ui, sans-serif; transition: opacity .4s; }

        /* "Krify is working" border: flowing colors on the edges + soft inner glow */
        .working { position: fixed; inset: 0; opacity: 0; transition: opacity .5s; }
        .working.on { opacity: 1; }
        .working .glow-in { position: absolute; inset: 0;
          box-shadow: inset 0 0 28px 6px rgba(124,92,255,.45), inset 0 0 60px 10px rgba(62,168,255,.18);
          animation: breathe 2.4s ease-in-out infinite alternate; }
        .working .edge { position: absolute;
          background: linear-gradient(90deg, #7c5cff, #3ea8ff, #ff5ca8, #7c5cff, #3ea8ff);
          background-size: 300% 100%; animation: flow 3s linear infinite;
          filter: drop-shadow(0 0 6px rgba(124,92,255,.9)); }
        .working .top, .working .bottom { left: 0; right: 0; height: 4px; }
        .working .top { top: 0; }
        .working .bottom { bottom: 0; animation-direction: reverse; }
        .working .left, .working .right { top: 0; bottom: 0; width: 4px;
          background: linear-gradient(180deg, #7c5cff, #3ea8ff, #ff5ca8, #7c5cff, #3ea8ff);
          background-size: 100% 300%; animation-name: flow-v; }
        .working .left { left: 0; animation-direction: reverse; }
        .working .right { right: 0; }
        .working .badge { position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
          padding: 4px 14px; border-radius: 999px; color: #fff; font: 600 12px/20px system-ui, sans-serif;
          background: linear-gradient(90deg, #7c5cff, #3ea8ff, #ff5ca8, #7c5cff); background-size: 300% 100%;
          animation: flow 3s linear infinite; box-shadow: 0 4px 18px rgba(124,92,255,.5); }
        @keyframes flow { to { background-position: 300% 0; } }
        @keyframes flow-v { to { background-position: 0 300%; } }
        @keyframes breathe { from { opacity: .55; } to { opacity: 1; } }
      </style>
      <div class="working">
        <div class="glow-in"></div>
        <div class="edge top"></div><div class="edge right"></div>
        <div class="edge bottom"></div><div class="edge left"></div>
        <div class="badge">✨ Krify is working…</div>
      </div>
      <div class="cursor">
        <svg viewBox="0 0 24 24"><path d="M4 2l15 9.5-6.6 1.4L16 20.5l-2.8 1.3-3.6-7.6L4 18.5z"
          fill="#5b5bd6" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>
        <span class="tag">Krify</span>
      </div>`;
    document.documentElement.append(host);
    view.root = root;
    view.cursor = root.querySelector(".cursor");
    return root;
  }

  function cursorPosition() {
    const m = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(view.cursor?.style.transform || "");
    return m ? { x: +m[1], y: +m[2] } : { x: innerWidth / 2, y: innerHeight / 2 };
  }

  async function moveCursorTo(el) {
    overlay();
    const r = el.getBoundingClientRect();
    const x = r.left + Math.min(r.width / 2, 60);
    const y = r.top + r.height / 2;
    view.cursor.style.opacity = "1";
    view.cursor.style.transform = `translate(${x}px, ${y}px)`;
    clearTimeout(view.hideTimer);
    view.hideTimer = setTimeout(() => (view.cursor.style.opacity = "0"), 5000);
    await sleep(600);
    return { x, y };
  }

  function glow(el) {
    const r = el.getBoundingClientRect();
    const box = document.createElement("div");
    box.className = "glow";
    Object.assign(box.style, { left: `${r.left - 4}px`, top: `${r.top - 4}px`, width: `${r.width + 8}px`, height: `${r.height + 8}px` });
    overlay().append(box);
    setTimeout(() => (box.style.opacity = "0"), 900);
    setTimeout(() => box.remove(), 1500);
  }

  function ripple(x, y) {
    const dot = document.createElement("div");
    dot.className = "ripple";
    Object.assign(dot.style, { left: `${x}px`, top: `${y}px` });
    overlay().append(dot);
    setTimeout(() => dot.remove(), 700);
  }

  function showKey(key) {
    const root = overlay();
    const { x, y } = cursorPosition();
    const badge = document.createElement("div");
    badge.className = "key";
    badge.textContent = key === "Enter" ? "⏎ Enter" : key;
    Object.assign(badge.style, { left: `${x + 20}px`, top: `${y + 26}px` });
    root.append(badge);
    setTimeout(() => (badge.style.opacity = "0"), 700);
    setTimeout(() => badge.remove(), 1200);
  }
}
