// Runs inside the web page. Gives Krify its "eyes".
// Injected on demand; safe to inject more than once.

if (!window.__krify) {
  const MAX_ELEMENTS = 100; // visible-on-screen only; fewer = smaller AI requests
  const MAX_TEXT = 2500; // keep AI requests small (free token limits)
  const MAX_SCREEN_TEXT = 3000;

  const SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "textarea", "select", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=switch]", "[contenteditable=true]", "[onclick]", "[draggable=true]",
    "[role=menuitemcheckbox]", "[role=menuitemradio]",
  ].join(",");
  const POPUP = '[role=menu], [role=listbox], [role=dialog], [role=alertdialog], [aria-modal="true"]';

  const NAME_BOX = '#t-name-box, input.waffle-name-box, input[aria-label="Name Box" i]';

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
      // An open menu, dropdown or dialog comes first: on busy pages (Google Sheets has 100+ toolbar
      // buttons) its items would otherwise fall past the element limit.
      const popups = [...document.querySelectorAll(POPUP)].filter(isVisible);
      const inPopups = popups.flatMap((p) => [...p.querySelectorAll(SELECTOR)]);
      if (inPopups.length) lines.push("(an open menu/dialog is listed first)");
      for (const el of new Set([...inPopups, ...document.querySelectorAll(SELECTOR)])) {
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

    // Google Sheets: name of the open sheet tab (the data itself is fetched by sheets.js).
    sheetTabName() {
      return document.querySelector(".docs-sheet-active-tab .docs-sheet-tab-name")?.textContent || "";
    },

    // ---------- actions ----------
    // When `animate` is on, a visible Krify cursor moves, clicks and types like a person.

    animate: false,

    // For screenshots: draw each listed element's number on the page (and hide Krify's own
    // border/badge/cursor so they don't cover the content). on=false removes the labels again.
    showLabels(on) {
      const root = overlay();
      root.querySelector(".labels")?.remove();
      for (const part of root.querySelectorAll(".working, .cursor")) part.style.visibility = on ? "hidden" : "";
      if (!on) return "ok";
      const layer = document.createElement("div");
      layer.className = "labels";
      for (const el of document.querySelectorAll("[data-krify-id]")) {
        const r = el.getBoundingClientRect();
        if (r.bottom < 0 || r.top > innerHeight || r.width < 2) continue;
        const tag = document.createElement("div");
        tag.className = "num";
        tag.textContent = el.getAttribute("data-krify-id");
        Object.assign(tag.style, { left: `${Math.max(0, r.left)}px`, top: `${Math.max(0, r.top)}px` });
        layer.append(tag);
      }
      root.append(layer);
      return "ok";
    },

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

    // Point at an element without clicking, so hover menus open (e.g. Amazon "Account & Lists").
    async hover(id) {
      const el = find(id);
      await bringIntoView(el);
      if (this.animate) {
        await moveCursorTo(el);
        glow(el);
      }
      const r = el.getBoundingClientRect();
      const at = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, view: window };
      const fire = (Type, type, bubbles) => el.dispatchEvent(new Type(type, { ...at, bubbles, cancelable: true }));
      fire(PointerEvent, "pointerover", true);
      fire(PointerEvent, "pointerenter", false);
      fire(MouseEvent, "mouseover", true);
      fire(MouseEvent, "mouseenter", false);
      fire(PointerEvent, "pointermove", true);
      fire(MouseEvent, "mousemove", true);
      el.focus?.({ preventScroll: true }); // many menus also open on focus (keyboard users)
      await sleep(500); // let the menu appear
      return `Hovered over [${id}] ${describe(el)}`;
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

    // ---------- helpers for real mouse clicks (realinput.js) ----------

    // Scroll an element into view, show the cursor there, and return the spot to click.
    async pointAt(id) {
      const el = find(id);
      await bringIntoView(el);
      const r = el.getBoundingClientRect();
      let spot = { x: r.left + Math.min(r.width / 2, 60), y: r.top + r.height / 2 };
      if (this.animate) {
        spot = await moveCursorTo(el);
        glow(el);
        ripple(spot.x, spot.y);
      }
      return { ...spot, nameBox: el.matches(NAME_BOX) };
    },

    // An element's spot without scrolling (a drag target must already be on screen).
    spotOf(id) {
      const el = find(id);
      if (!isVisible(el)) throw new Error(`[${id}] is not on screen – scroll so both ends of the drag are visible.`);
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    },

    viewport() {
      return { width: innerWidth, height: innerHeight, scrollX: Math.round(scrollX), scrollY: Math.round(scrollY) };
    },

    // What's at a spot, e.g. 'button "Delete"' – so a click by position gets the same safety check.
    elementAt(x, y) {
      const hit = document.elementFromPoint(x, y); // Krify's own overlay ignores the mouse, so it's skipped
      if (!hit) return "";
      return describe(hit.closest(SELECTOR) || hit);
    },

    // Show Krify's cursor (and a click ripple) at a spot.
    async showPoint(x, y, click = true) {
      if (!this.animate) return;
      await moveCursorToPoint(x, y);
      if (click) ripple(x, y);
    },

    // After a real click: is the cursor in this box? Selects its text so typing replaces it.
    focusCheck(id) {
      const el = find(id);
      if (document.activeElement !== el && !el.contains(document.activeElement)) return false;
      el.select?.();
      return true;
    },

    // Google Sheets' Name box (shows/jumps to a cell like A130): its spot, value and focus.
    async nameBox() {
      const el = document.querySelector(NAME_BOX);
      if (!el) throw new Error("Can't find the Name box on this page.");
      const r = el.getBoundingClientRect();
      let spot = { x: r.left + Math.min(r.width / 2, 60), y: r.top + r.height / 2 };
      if (this.animate) {
        spot = await moveCursorTo(el);
        glow(el);
        ripple(spot.x, spot.y);
      }
      return { ...spot, value: el.value, focused: document.activeElement === el };
    },

    // Google Sheets: the middle of the selected cell, from the border Sheets draws around it.
    activeCellSpot() {
      const parts = [...document.querySelectorAll(".active-cell-border")]
        .map((el) => el.getBoundingClientRect())
        .filter((r) => r.width || r.height);
      if (!parts.length) throw new Error("Can't see the selected cell on screen – take a screenshot and use click_at.");
      const left = Math.min(...parts.map((r) => r.left));
      const right = Math.max(...parts.map((r) => r.right));
      const top = Math.min(...parts.map((r) => r.top));
      const bottom = Math.max(...parts.map((r) => r.bottom));
      if (right - left < 4 || bottom - top < 4) {
        throw new Error("Can't see the selected cell on screen – take a screenshot and use click_at.");
      }
      return { x: (left + right) / 2, y: (top + bottom) / 2 };
    },

    nameBoxValue() {
      return document.querySelector(NAME_BOX)?.value || "";
    },

    // Google Sheets/Docs save status next to the title: "saving", "saved" or "unknown".
    saveState() {
      const said = [...document.querySelectorAll("[aria-label], [data-tooltip], .docs-save-indicator-container")]
        .slice(0, 3000)
        .map((el) => `${el.getAttribute("aria-label") || ""} ${el.getAttribute("data-tooltip") || ""} ${el.classList.contains("docs-save-indicator-container") ? el.textContent : ""}`)
        .join("\n");
      if (/\bsaving\b/i.test(said)) return "saving";
      if (/all changes saved|saved to drive|changes saved/i.test(said)) return "saved";
      return "unknown";
    },

    // Google Sheets: the selected cell and what the formula bar shows for it (live, no saving needed).
    activeCell() {
      const bar = document.querySelector("#t-formula-bar-input, .cell-input[role=combobox], #t-formula-bar-input-container [contenteditable]");
      return { cell: document.querySelector(NAME_BOX)?.value || "", formula: bar ? bar.innerText.replace(/\n$/, "") : null };
    },

    selectNameBox() {
      const el = document.querySelector(NAME_BOX);
      if (!el || document.activeElement !== el) return false;
      el.select();
      return true;
    },

    // Show the key badge for a key pressed for real (by realinput.js).
    showKeyBadge(key) {
      if (this.animate) showKey(key);
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
        /* element numbers drawn for screenshots */
        .num { position: fixed; padding: 0 4px; border-radius: 3px; background: #ffd400; color: #000;
          border: 1px solid #000; font: 700 11px/14px system-ui, sans-serif; }
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
    const r = el.getBoundingClientRect();
    return moveCursorToPoint(r.left + Math.min(r.width / 2, 60), r.top + r.height / 2);
  }

  async function moveCursorToPoint(x, y) {
    overlay();
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
