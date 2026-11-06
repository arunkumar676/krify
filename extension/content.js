// Runs inside the web page. Gives Krify its "eyes".
// Injected on demand; safe to inject more than once.

if (!window.__krify) {
  const MAX_SHOWN = 100; // described in getElements() text + drawn by showLabels() on screenshots
  const MAX_NUMBERED = 400; // hard ceiling: every qualifying visible element up to this gets an id
  const MAX_FIND_CHARS = 2000; // find_elements response budget (sheets.py-style cap)
  const MAX_TEXT = 2500; // keep AI requests small (free token limits)
  const MAX_SCREEN_TEXT = 3000;
  const MAX_TREE_CHARS = 4000; // inspect() tree view: errors out past this instead of cutting silently


  // inspect(): implicit roles for non-interactive structural context (headings, regions, text).
  const STRUCTURAL_ROLE = {
    H1: "heading", H2: "heading", H3: "heading", H4: "heading", H5: "heading", H6: "heading",
    LI: "listitem", UL: "list", OL: "list", TABLE: "table", TR: "row", TD: "cell", TH: "cell",
    NAV: "navigation", MAIN: "main", HEADER: "banner", FOOTER: "contentinfo",
    SECTION: "region", ARTICLE: "article", ASIDE: "complementary", IMG: "img",
  };

  const LANDMARK_ROLES = new Set(
    ["navigation", "main", "banner", "contentinfo", "region", "article", "complementary", "table", "list"]
  );

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

  // CSS-visible (has size, not hidden/display:none/near-zero-opacity) regardless of scroll position.
  function isRendered(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0.05;
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) return false;
    return isRendered(el);
  }

  // Some sites (e.g. Amazon's "Sort by") hide the real <select> under a custom-styled button –
  // treat it as visible enough if its visible wrapper is on screen. In unrestricted mode "visible"
  // means CSS-visible anywhere in the document, not just the current viewport, so off-screen
  // elements (above/below the fold) are found too.
  function visibleEnough(el) {
    const vis = window.__krify.unrestricted ? isRendered : isVisible;
    return vis(el) || (el.tagName === "SELECT" && el.parentElement && vis(el.parentElement));
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

  // Status/error/validation messages a site shows dynamically (toasts, banners, inline form
  // errors) – ARIA conventions a well-built site already uses for accessibility, reused here so
  // Krify can see "it failed" even when the message isn't itself a clickable element.
  const STATUS_SELECTOR = '[role="alert"], [role="status"], [aria-live="polite"], [aria-live="assertive"]';

  function statusSignals() {
    const seen = new Set();
    const lines = [];
    for (const r of allRoots()) {
      for (const el of r.querySelectorAll(STATUS_SELECTOR)) {
        if (!visibleEnough(el)) continue;
        const text = clean(el.innerText, 150);
        if (!text || seen.has(text)) continue;
        seen.add(text);
        lines.push(text);
      }
      for (const el of r.querySelectorAll(":invalid")) {
        if (!visibleEnough(el) || !el.validationMessage) continue;
        const text = clean(`${labelOf(el) || el.name || el.type}: ${el.validationMessage}`, 150);
        if (seen.has(text)) continue;
        seen.add(text);
        lines.push(text);
      }
    }
    return lines.slice(0, 5).join(" | ");
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

  // find_elements(): common UI-action synonyms, so searching "sign out" also matches "Logout".
  // Kept small and regex-based (no embeddings/fuzzy-NLP – this is plain JS, no build step).
  const ALIASES = [
    /sign ?in|log ?in|logon/i, /sign ?out|log ?out|logoff|exit account/i, /sign ?up|register|create account/i,
    /delete|remove|discard|trash/i, /save|submit|confirm|apply/i, /cancel|close|dismiss/i,
    /edit|modify|change/i, /search|find/i,
  ];
  function matchesQuery(text, query) {
    const q = query.toLowerCase();
    const group = ALIASES.find((re) => re.test(q));
    return group ? group.test(text) : text.toLowerCase().includes(q);
  }

  // Every open shadow root nested anywhere under `root` (including shadow roots inside other
  // shadow roots), plus `root` itself – so the scans below also see modern web components' own
  // content, not just the page's light DOM. Closed shadow roots are invisible to el.shadowRoot by
  // design (no way around that, same as any extension).
  function allRoots(root = document) {
    const roots = [root];
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) roots.push(...allRoots(el.shadowRoot));
    return roots;
  }

  // Clears and reassigns data-krify-id on the same visible/interactive elements getElements()
  // has always used, so get_elements, find_elements and inspect() always agree on what [12] means.
  // Every qualifying element gets an id (up to MAX_NUMBERED), not just the first MAX_SHOWN – so
  // click/type/etc. can reach anything find_elements() turns up, even past the default shown list.
  function numberElements() {
    const roots = allRoots();
    for (const r of roots) r.querySelectorAll("[data-krify-id]").forEach((el) => el.removeAttribute("data-krify-id"));
    const seenLinks = new Set();
    const candidates = [];
    // An open menu, dropdown or dialog comes first: on busy pages (Google Sheets has 100+ toolbar
    // buttons) its items would otherwise fall past the shown-elements window.
    const popups = roots.flatMap((r) => [...r.querySelectorAll(POPUP)]).filter(window.__krify.unrestricted ? isRendered : isVisible);
    const inPopups = new Set(popups.flatMap((p) => [...p.querySelectorAll(SELECTOR)]));
    for (const el of new Set([...inPopups, ...roots.flatMap((r) => [...r.querySelectorAll(SELECTOR)])])) {
      const visible = visibleEnough(el);
      if (!visible || el.disabled) continue;
      // Fewer tokens: inside an already-listed link/button = same click; same link target = listed once
      // (shops link each product from its picture AND its title).
      if (el.parentElement?.closest("[data-krify-cand]")) continue;
      if (el.tagName === "A" && el.href) {
        if (seenLinks.has(el.href)) continue;
        seenLinks.add(el.href);
      }
      const text = describe(el);
      if (/"\s*"/.test(text) && !/input|textarea|select/.test(text)) continue; // skip unlabeled
      el.setAttribute("data-krify-cand", "1"); // temporary marker for the containment check above
      candidates.push({ el, text, rect: el.getBoundingClientRect() });
    }
    candidates.forEach(({ el }) => el.removeAttribute("data-krify-cand"));
    // Visual reading order (top to bottom, left to right), popups first – not raw DOM order, so
    // which elements land in the shown window reflects what a person scanning the page would
    // reach first, not an accident of markup order.
    candidates.sort((a, b) => {
      const pa = inPopups.has(a.el), pb = inPopups.has(b.el);
      if (pa !== pb) return pa ? -1 : 1;
      return a.rect.top - b.rect.top || a.rect.left - b.rect.left;
    });
    const numbered = [];
    for (const { el, text } of candidates) {
      const id = numbered.length + 1;
      el.setAttribute("data-krify-id", String(id));
      numbered.push({ id, el, text });
      if (!window.__krify.unrestricted && id >= MAX_NUMBERED) break;
    }
    return { numbered, hasPopup: inPopups.size > 0 };
  }

  // ---------- inspect(): accessibility-tree view ----------

  // An element's own text, not counting text that belongs to its children (so a container's
  // line doesn't just repeat what its children already show).
  function ownText(el) {
    let text = "";
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) text += node.nodeValue;
    }
    return clean(text, 80);
  }

  // Walk up from a numbered element to the nearest landmark-ish ancestor (or up to 4 levels),
  // so ref=<element> zooms into the card/section around it, not just its own (usually empty)
  // descendants.
  function containerOf(el) {
    let node = el;
    for (let i = 0; i < 4 && node.parentElement; i++) {
      node = node.parentElement;
      const role = node.getAttribute("role") || STRUCTURAL_ROLE[node.tagName];
      if (role && LANDMARK_ROLES.has(role)) return node;
    }
    return node;
  }

  // One tree line for an element, or null if it's just layout (skipped, not a dead end –
  // its children are still visited at the same depth).
  function treeLine(el, filter) {
    const id = el.getAttribute("data-krify-id");
    if (id) return `[${id}] ${describe(el)}`;
    if (filter !== "all") return null;
    const role = el.getAttribute("role") || STRUCTURAL_ROLE[el.tagName];
    if (!role) return null;
    if (role === "img") {
      const alt = clean(el.getAttribute("alt") || el.getAttribute("aria-label") || "", 60);
      return alt ? `img "${alt}"` : null;
    }
    const text = ownText(el);
    if (!text && !LANDMARK_ROLES.has(role)) return null; // empty non-landmark: not worth a line
    return text ? `${role} "${text}"` : role;
  }

  function buildTree(root, filter, maxDepth) {
    const lines = [];
    let truncated = false;
    function visit(el, depth) {
      if (truncated) return;
      const line = treeLine(el, filter);
      if (line !== null) {
        lines.push("  ".repeat(depth) + line);
        if (!window.__krify.unrestricted && lines.join("\n").length > MAX_TREE_CHARS) {
          truncated = true;
          return;
        }
      }
      const nextDepth = line !== null ? depth + 1 : depth; // layout wrappers don't cost depth
      if (nextDepth > maxDepth) return;
      for (const child of [...el.children, ...(el.shadowRoot?.children || [])]) {
        if (!(window.__krify.unrestricted ? isRendered : isVisible)(child)) continue;
        visit(child, nextDepth);
        if (truncated) return;
      }
    }
    visit(root, 0);
    return { lines, truncated };
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
      const { numbered, hasPopup } = numberElements();
      const lines = [];
      const status = statusSignals();
      if (status) lines.push(`[Status] ${status}`);
      if (hasPopup) lines.push("(an open menu/dialog is listed first)");
      const shown = this.unrestricted ? numbered : numbered.slice(0, MAX_SHOWN);
      for (const { id, text } of shown) lines.push(`[${id}] ${text}`);
      if (!this.unrestricted && numbered.length > MAX_SHOWN) {
        lines.push(`(${numbered.length - MAX_SHOWN} more elements not shown – use find_elements("text") to search for one)`);
      }
      if (!this.unrestricted && numbered.length >= MAX_NUMBERED) {
        lines.push(`(page has more than ${MAX_NUMBERED} elements; some may be unreachable)`);
      }
      const below = document.documentElement.scrollHeight - (scrollY + innerHeight);
      if (below > 50) lines.push("(more content below – scroll down to see it)");
      return lines.join("\n") || "(no interactive elements visible)";
    },

    // Search ALL numbered elements (not just the shown window above) by label text – for pages
    // with more elements than getElements() describes by default. Tolerates common UI synonyms
    // (see matchesQuery/ALIASES). Any id it returns is already clickable/typeable immediately.
    findElements(query) {
      const q = clean(query || "", 200);
      if (!q) return "(empty query)";
      const { numbered } = numberElements();
      const matches = numbered.filter(({ text }) => matchesQuery(text, q));
      if (!matches.length) return `(no elements match "${q}")`;
      const lines = [];
      let chars = 0;
      for (const { id, text } of matches) {
        const line = `[${id}] ${text}`;
        chars += line.length + 1;
        if (!this.unrestricted && chars > MAX_FIND_CHARS) {
          lines.push(`…(${matches.length - lines.length} more matches not shown – narrow your search)`);
          break;
        }
        lines.push(line);
      }
      return lines.join("\n");
    },

    // Finds ANY text on the page (not just clickable elements, and not limited by read_text's
    // length cap, or to what's currently scrolled into view), scrolls to it, and reads what's
    // around it. Walks the DOM ourselves (rather than the native window.find()) so matching is
    // something we control: window.find()'s matching depends on hidden selection/anchor state and
    // can miss a phrase split across sibling elements (e.g. a styled heading like "Testimonial" +
    // "s" in separate spans) even when it's plainly on the page.
    async findText(query) {
      const q = clean(query || "", 200).toLowerCase();
      if (!q) return "(empty query)";
      const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
      // Smallest element whose own text contains the full query – descending into a child that
      // also contains it in full, so a match split across siblings resolves to their common
      // ancestor instead of the whole page.
      function findContainer(el) {
        if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName)) return null;
        if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return null;
        if (!norm(el.innerText).includes(q)) return null;
        for (const child of [...el.children, ...(el.shadowRoot?.children || [])]) {
          const found = findContainer(child);
          if (found) return found;
        }
        return el;
      }
      const match = findContainer(document.body);
      if (!match) return `(no text found matching "${query}")`;
      await bringIntoView(match);
      return this.readText("screen");
    },

    // Structure around the page or one element – headings, regions, nearby text – for when the
    // flat element list above doesn't make clear what goes with what (e.g. several "Add to cart"
    // buttons with no indication which belongs to which product).
    getAccessibilityTree({ ref, filter = "interactive", depth = 8 } = {}) {
      numberElements(); // refresh ids so they match what a fresh getElements() would assign
      const root = ref ? containerOf(find(ref)) : document.body;
      const { lines, truncated } = buildTree(root, filter, Math.max(1, Number(depth) || 8));
      if (truncated) {
        return "Too much structure to show from here. Try a smaller depth, or ref=<element number> to zoom into one part.";
      }
      return lines.join("\n") || "(nothing to show here)";
    },

    // "screen" (default): only the text visible right now, line by line – prices, answers, etc.
    // "page": the whole page (long: keeps the start and end, cuts the middle at MAX_TEXT – totals/
    // pagination summaries are usually at one end or the other). Unrestricted mode: "screen" reads
    // the whole page too (same as "page", uncapped) – the on-screen-only restriction is a cap like
    // the others, not a fixed meaning of the default scope.
    readText(scope = "screen") {
      if (scope === "page" || this.unrestricted) {
        const text = (document.body?.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
        if (this.unrestricted || text.length <= MAX_TEXT) return text;
        const TAIL_CHARS = 700; // totals/pagination summaries often live at the very end
        const head = text.slice(0, MAX_TEXT - TAIL_CHARS);
        const tail = text.slice(-TAIL_CHARS);
        return `${head}\n…(${text.length - MAX_TEXT} chars cut – page is long)…\n${tail}\n`
          + "(if what you need isn't above, try find_text(\"...\") instead – it searches the WHOLE "
          + "page and jumps straight to a match)";
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
        if (!this.unrestricted && size > MAX_SCREEN_TEXT) {
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

    // When `unrestricted` is on, the caps below (MAX_SHOWN/MAX_NUMBERED/MAX_TEXT/MAX_SCREEN_TEXT/
    // MAX_FIND_CHARS/MAX_TREE_CHARS) are skipped – full page data instead of the token-saving
    // defaults. Set by operations.js on every call, from a side-panel toggle (sidepanel.js).
    unrestricted: false,

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
        if (!this.unrestricted && Number(el.dataset.krifyId) > MAX_SHOWN) continue; // only label what getElements() describes
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
      await waitActionable(el, id);
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
      await waitActionable(el, id);
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
      await waitActionable(el, id);
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
      await waitActionable(el, id);
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
    async spotOf(id) {
      const el = find(id);
      if (!isVisible(el)) throw new Error(`[${id}] is not on screen – scroll so both ends of the drag are visible.`);
      await waitActionable(el, id);
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

    // Live description of a numbered element right now, or "" if it's gone – lets the backend check
    // an id still means what it meant when the model looked, before acting on it.
    describeId(id) {
      const el = document.querySelector(`[data-krify-id="${id}"]`);
      return el ? describe(el) : "";
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
      const target = isVisible(el) ? el : el.parentElement; // hidden <select>: point at its visible wrapper
      await bringIntoView(target);
      await waitActionable(el, id, { proxy: target === el ? undefined : target });
      if (this.animate) {
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
    await waitStable(el, smooth ? 1000 : 400);
  }

  // Waits until an element's bounding box stops changing (scrolling/animating settled) for ~100ms,
  // or until timeout – a real settle-check instead of a fixed sleep guess. Returns the settled
  // rect, or null if it never stopped moving in time.
  async function waitStable(el, timeoutMs) {
    let lastRect = null, stableSince = 0;
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const r = el.getBoundingClientRect();
      const moved = !lastRect || Math.abs(r.top - lastRect.top) > 0.5 || Math.abs(r.left - lastRect.left) > 0.5
        || Math.abs(r.width - lastRect.width) > 0.5 || Math.abs(r.height - lastRect.height) > 0.5;
      lastRect = r;
      if (moved) stableSince = Date.now();
      else if (Date.now() - stableSince > 100) return r;
      await sleep(80);
    }
    return null;
  }

  // Playwright-style "actionability" wait: before interacting, make sure the element is actually
  // ready – visible, stopped moving, not covered by something else, not disabled, still on the
  // page – instead of firing the action immediately and hoping for the best. `proxy`: for a
  // control with no meaningful on-screen point of its own (a hidden native <select>), check
  // stability/occlusion against its visible wrapper instead of the real element.
  async function waitActionable(el, id, { timeoutMs, proxy } = {}) {
    timeoutMs = timeoutMs ?? (window.__krify.animate ? 3000 : 2000);
    const target = proxy || el;
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!el.isConnected) throw new Error(`[${id}] was removed from the page while waiting – get the elements again.`);
      if (visibleEnough(target) && !el.disabled) {
        const r = await waitStable(target, Math.max(200, timeoutMs - (Date.now() - start)));
        if (r) {
          const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
          const hit = document.elementFromPoint(cx, cy);
          if (hit && (target === hit || target.contains(hit) || hit.contains(target))) return;
        }
      }
      await sleep(80);
    }
    throw new Error(`[${id}] isn't ready yet (hidden, disabled, still moving, or covered by something) – take a screenshot to check, or try again.`);
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
