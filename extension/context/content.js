// Runs inside the web page. Gives Krify its "eyes".
// Injected on demand; safe to inject more than once.

if (!window.__krify) {
  const MAX_ELEMENTS = 200; // default cap on elements numbered in one call (operations.js normally
  // passes its own per-frame budget to getElements() instead – see MAX_ELEMENTS there)
  const MAX_FIND_CHARS = 2000; // find_elements response budget (sheets.py-style cap)
  const MAX_TEXT = 2500; // keep AI requests small (free token limits)
  const MAX_SCREEN_TEXT = 3000;
  const MAX_TREE_CHARS = 4000; // inspect() tree view: errors out past this instead of cutting silently
  // getPageModel(): the WHOLE page is collected (the server decides how much of it the AI sees),
  // with a hard cap and a time limit so huge pages stay fast. On-screen elements are collected
  // first, so the cap/time limit only ever cuts far-off parts of the page.
  const MODEL_MAX = 600;
  const COLLECT_MS = 400;
  const MAX_CARD_SUMMARIES = 80;
  const MAX_OPTIONS = 15; // a dropdown's options listed in its line (Unrestricted too)


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
    "[role=menuitemcheckbox]", "[role=menuitemradio]", "[role=radio]", "[role=slider]", "[role=spinbutton]",
  ].join(",");
  
  const POPUP = '[role=menu], [role=listbox], [role=dialog], [role=alertdialog], [aria-modal="true"], dialog[open]';

  // Element number → element, from the latest getElements (also finds elements inside shadow roots,
  // which a page-wide querySelector can't).
  const listed = new Map();

  const NAME_BOX = '#t-name-box, input.waffle-name-box, input[aria-label="Name Box" i]';

  const clean = (s, max = 50) => {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > max ? s.slice(0, max) + "…" : s;
  };
  // Labels, titles and values: shortened to `max` normally, kept whole in Unrestricted mode
  // (operations.js sets window.__krify.unrestricted before every call into the page).
  const keep = (s, max) => clean(s, window.__krify?.unrestricted ? Infinity : max);
  // How many of a list to pass on: `n` normally, all of them in Unrestricted mode.
  const upTo = (n) => (window.__krify?.unrestricted ? Infinity : n);

  // What a person could see, regardless of scroll position: has size, isn't hidden, faded out or
  // clipped away – by its own CSS or an ancestor's – and isn't in an inert / aria-hidden part of
  // the page (the background behind a modal, carousel clones).
  function isRendered(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) {
      // A zero-size link/button (display:contents, only absolutely-placed children) shows through its children.
      return /^(A|BUTTON)$/.test(el.tagName) && [...el.children].some((c) => isRendered(c));
    }
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) <= 0.05) return false;
    return visibleAt(el, rect);
  }

  // The ONE rule for "could a person see this", used for elements (isRendered) and for the page's
  // text (pageTextBlocks) alike – so the view can never show a piece of text whose element it
  // calls hidden, or the other way round. `rect`: the element's box, or a text node's.
  const VISIBILITY = { opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true, checkOpacity: true, checkVisibilityCSS: true };
  function visibleAt(el, rect) {
    // Parked outside the page, where no scrolling reaches (a screen-reader-only "Skip to main
    // content" link, Amazon's hidden keyboard-shortcut menu at left: -9999px).
    if (rect.right + scrollX < 0 || rect.bottom + scrollY < 0) return false;
    // Ancestors too: opacity 0 / visibility / content-visibility on a parent hides this element.
    if (el.checkVisibility && !el.checkVisibility(VISIBILITY)) return false;
    if (closestDeep(el, '[inert], [aria-hidden="true"]')) return false; // background of a modal, decoration
    return !clippedAway(el, rect);
  }

  // Ancestor styles are read once per task tick, not once per element (pages have thousands).
  const styleCache = new Map();
  function cachedStyle(node) {
    if (!styleCache.size) setTimeout(() => styleCache.clear());
    let s = styleCache.get(node);
    if (!s) {
      const cs = getComputedStyle(node);
      s = { position: cs.position, overflowX: cs.overflowX, overflowY: cs.overflowY, transform: cs.transform, display: cs.display };
      styleCache.set(node, s);
    }
    return s;
  }

  // Cut off by an overflow:hidden/clip ancestor (closed accordion, carousel track, "read more")?
  // Follows CSS's rules: a fixed element escapes every ancestor; an absolutely placed one only
  // the ancestors below its containing block (the nearest positioned/transformed one).
  function clippedAway(el, rect) {
    let pos = cachedStyle(el).position;
    if (pos === "fixed") return false;
    let needPositioned = pos === "absolute";
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const s = cachedStyle(p);
      const positioned = s.position !== "static" || s.transform !== "none";
      if (needPositioned && !positioned) continue; // not its containing block: doesn't clip it
      needPositioned = false;
      const clipsX = /hidden|clip/.test(s.overflowX), clipsY = /hidden|clip/.test(s.overflowY);
      if (clipsX || clipsY) {
        const b = p.getBoundingClientRect();
        const w = clipsX ? Math.min(rect.right, b.right) - Math.max(rect.left, b.left) : rect.width;
        const h = clipsY ? Math.min(rect.bottom, b.bottom) - Math.max(rect.top, b.top) : rect.height;
        if (w < 2 || h < 2) return true;
      }
      // A scrolling box (a sidebar menu longer than the screen): what's inside can be scrolled into
      // its view, so from here up only the box itself has to be visible – an overflow:hidden frame
      // around it doesn't make the lower menu items "clipped away".
      if (/auto|scroll/.test(s.overflowX) || /auto|scroll/.test(s.overflowY)) rect = p.getBoundingClientRect();
      if (s.position === "fixed") return false;
      if (s.position === "absolute") needPositioned = true;
    }
    return false;
  }

  // ---------- shadow DOM: parts of a page sealed inside a component ----------
  // Normal searches stop at a component's shadow root; these helpers look inside it too.

  // Open roots are on el.shadowRoot. Closed ones need the extension-only API, asked only for
  // custom elements (tag with "-") so busy pages stay fast. Krify's own overlay is skipped.
  function shadowOf(el) {
    if (el.tagName === "KRIFY-OVERLAY") return null;
    if (el.shadowRoot) return el.shadowRoot;
    return el.tagName.includes("-") ? chrome.dom?.openOrClosedShadowRoot?.(el) || null : null;
  }

  // Every text node in page order, including text inside shadow roots.
  function* textNodes(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === Node.TEXT_NODE) {
        yield node;
      } else {
        const shadow = shadowOf(node);
        if (shadow) yield* textNodes(shadow);
      }
    }
  }

  // Every element matching `selector`, in page order, including those inside shadow roots.
  function deepAll(selector, root = document) {
    const found = [];
    for (const el of root.querySelectorAll("*")) {
      if (el.matches(selector)) found.push(el);
      const shadow = shadowOf(el);
      if (shadow) found.push(...deepAll(selector, shadow));
    }
    return found;
  }

  // Repeated rows/cards (tables, lists, grids), counted by code: the AI miscounts long lists by eye.
  // A group = a parent with 5+ children that look alike (same tag + classes) and have text.
  // Groups sitting inside an item of a bigger group (e.g. the cells of one row) are left out.
  // A table row's cells are its columns, never rows: a 2-row, 5-column table must not read as
  // "5 rows". A table body counts from 1 row, since a short filtered table is exactly what gets asked about.
  function listCounts() {
    const sig = (el) => el.tagName + "." + [...el.classList].filter((c) => !/active|selected|hover|focus|odd|even/i.test(c)).sort().join(".");
    const groups = [];
    for (const parent of deepAll("*")) {
      if (parent.tagName === "SELECT" || parent.tagName === "TR" || parent.getAttribute("role") === "row") continue;
      const tableBody = parent.tagName === "TBODY" ||
        (parent.getAttribute("role") === "rowgroup" && !parent.querySelector("[role=columnheader]"));
      const min = tableBody ? 1 : 5;
      if (parent.children.length < min) continue;
      const tally = new Map();
      for (const kid of parent.children) {
        if (!kid.textContent.trim() || kid.getBoundingClientRect().height < 1) continue;
        const key = sig(kid);
        tally.set(key, [...(tally.get(key) || []), kid]);
      }
      const items = [...tally.values()].sort((a, b) => b.length - a.length)[0];
      const clickable = tableBody || items?.[0]?.querySelector?.("a[href], button, [role=button], [data-krify-id]");
      if (items && items.length >= min && clickable) groups.push({ items, table: tableBody });
    }
    const kept = groups.filter((g) => !groups.some((h) => h !== g && h.items.length >= g.items.length &&
      h.items.some((item) => item.contains(g.items[0]))));
    return kept
      // Table rows first: they're what "how many" questions are about, so they mustn't fall past the cap.
      .sort((a, b) => b.table - a.table || b.items.length - a.items.length)
      .slice(0, upTo(4))
      .map(({ items, table }) => `${items.length} ${table ? "table " : ""}row${items.length === 1 ? "" : "s"} like ` +
        `"${clean(items[0].innerText || items[0].textContent, 60)}"`);
  }

  // The parent, stepping out of a shadow root to the component that holds it.
  const parentOf = (el) => el.parentElement || el.getRootNode()?.host || null;

  // Like el.closest(selector), but also looks past shadow root edges.
  function closestDeep(el, selector) {
    for (let p = el; p; p = parentOf(p)) if (p.matches?.(selector)) return p;
    return null;
  }

  // The focused element, even when it sits inside a component.
  function activeDeep() {
    let el = document.activeElement;
    for (let shadow = el && shadowOf(el); shadow?.activeElement; shadow = shadowOf(el)) el = shadow.activeElement;
    return el;
  }

  // Can this box scroll by itself (not the whole page)?
  function canScroll(box) {
    if (box.scrollHeight <= box.clientHeight + 4) return false;
    return /(auto|scroll|overlay)/.test(getComputedStyle(box).overflowY);
  }

  // Visible boxes with more to scroll to, at most 5 (cheap size checks first, style only then).
  function scrollBoxes() {
    const found = [];
    for (const el of deepAll("*")) {
      if (el === document.body || el === document.documentElement || el.clientHeight < 60) continue;
      if (el.scrollHeight <= el.clientHeight + 4 || !canScroll(el) || !isVisible(el)) continue;
      if (found.some((outer) => outer.contains(el))) continue; // the inner one scrolls with it
      found.push(el);
      if (found.length >= 5) break;
    }
    return found;
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) return false;
    return isRendered(el);
  }

  // A button the site shows only while the mouse is over its row (Mistral's project "…", mail row
  // actions): on screen and full size, but transparent or visibility:hidden until hovered. It counts
  // only if it sits inside a visible row – closed menus (drawn outside their button) and hidden
  // templates stay out.
  const MAX_HOVER = 20; // these add up on list pages (every row has some): keep the token cost small
  function hiddenUntilHover(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    if (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) return false;
    let node = el;
    for (let depth = 0; node && depth < 4; depth++, node = parentOf(node)) {
      if (node.nodeType !== 1) return false;
      const style = getComputedStyle(node);
      if (style.display === "none") return false;
      if (Number(style.opacity) > 0.05 && style.visibility !== "hidden") continue;
      // The hidden part found: its row is the nearest visible element around it.
      for (let row = parentOf(node), up = 0; row && up < 4; row = parentOf(row), up++) {
        if (row.nodeType !== 1 || !isVisible(row)) continue;
        const box = row.getBoundingClientRect();
        const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
        return cx >= box.left && cx <= box.right && cy >= box.top && cy <= box.bottom;
      }
      return false;
    }
    return false;
  }

  // A real control made (almost) transparent and laid over a styled wrapper: Amazon's "Add to cart"
  // <input type=submit> (opacity .01 on top of the yellow button), custom checkboxes/radios, file
  // buttons. The control is what actually gets clicked, so it counts as visible when the wrapper it
  // sits on is. Typing fields are left out on purpose: a transparent text box is usually a trap for
  // bots ("leave this blank"), never something to fill. One that clicks pass through
  // (pointer-events: none) isn't a see-through control either: it's waiting for a hover.
  const SEE_THROUGH = "button, input[type=submit], input[type=button], input[type=image], input[type=reset], " +
    "input[type=checkbox], input[type=radio], input[type=file]";

  // → the visible wrapper the control shows through, or null.
  function seeThroughWrapper(el, vis) {
    if (!el.matches(SEE_THROUGH)) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) > 0.05) return null;
    // Clicks pass through it (pointer-events: none), so it isn't what gets clicked: it's a button the
    // site shows only while the mouse is over its row (Mistral's "New Project" +). Left to
    // hiddenUntilHover(), which lists it "(shows on hover)".
    if (style.pointerEvents === "none") return null;
    let p = el.parentElement;
    for (let i = 0; i < 2 && p && p !== document.body && p !== document.documentElement; i++, p = p.parentElement) {
      const pr = p.getBoundingClientRect();
      const overlaps = r.left < pr.right && r.right > pr.left && r.top < pr.bottom && r.bottom > pr.top;
      if (overlaps && vis(p)) return p;
    }
    return null;
  }

  // Something drawn over a see-through control that's part of it: its own <label> or its wrapper
  // (a custom checkbox's drawn box) – clicking there reaches the control, as in a real browser.
  function partOfControl(el, hit) {
    if ([...(el.labels || [])].some((label) => label.contains(hit))) return true;
    return !!seeThroughWrapper(el, isRendered)?.contains(hit);
  }

  // A control that hides itself but shows through a visible wrapper: a <select> under a custom-styled
  // button (Amazon's "Sort by"), or a see-through control (above).
  function shownByWrapper(el, vis) {
    return !!standInFor(el, vis);
  }

  // The visible thing a person clicks for a control that hides itself: a hidden <select>'s styled
  // wrapper, a see-through control itself (it's on top), or the <label> of a hidden checkbox/radio
  // (display:none input + drawn box: clicking the label toggles it). null = nothing visible.
  function standInFor(el, vis = isRendered) {
    if (el.tagName === "SELECT") return el.parentElement && vis(el.parentElement) ? el.parentElement : null;
    if (seeThroughWrapper(el, vis)) return el;
    if (el.tagName === "INPUT" && /^(checkbox|radio)$/.test(el.type)) {
      return [...(el.labels || [])].find((label) => vis(label)) || null;
    }
    return null;
  }

  // What to point at and click for an element: itself if it's visible, else its stand-in.
  const clickTarget = (el) => (isRendered(el) ? el : standInFor(el) || el);

  // In unrestricted mode "visible" means CSS-visible anywhere in the document, not just the current
  // viewport, so off-screen elements (above/below the fold) are found too.
  function visibleEnough(el) {
    const vis = window.__krify.unrestricted ? isRendered : isVisible;
    return vis(el) || shownByWrapper(el, vis);
  }

  // Text of the elements an aria-labelledby / aria-describedby attribute points at (ids are looked
  // up in the element's own document or shadow root).
  function textOfIds(el, attr) {
    const ids = el.getAttribute(attr);
    if (!ids) return "";
    const root = el.getRootNode();
    return ids.split(/\s+/)
      .map((id) => (root.getElementById?.(id) || document.getElementById(id))?.innerText || "")
      .join(" ").trim();
  }

  // A button-like <input>'s visible text: its value, its alt (image buttons), or the browser's own
  // default caption – read before `name`, which is a field id (Rails' name="commit" value="Sign in").
  function buttonInputText(el) {
    if (el.tagName !== "INPUT" || !/^(submit|button|reset|image)$/.test(el.type)) return "";
    if (el.type === "image") return el.getAttribute("alt") || el.getAttribute("value") || "";
    return el.value || { submit: "Submit", reset: "Reset" }[el.type] || "";
  }

  // An icon-only button (no text, no aria-label): its SVG <title>, tooltip attribute, or a well-known
  // icon name in its classes / sprite reference ("fa-search", "#icon-close") – "search (icon)".
  const ICON_WORDS = new Set(("search close x menu hamburger cart basket bag delete trash bin edit pencil plus add " +
    "minus settings gear cog user account profile heart wishlist favorite star share filter sort download upload " +
    "refresh reload send home bell notification notifications play pause next prev previous back forward more " +
    "ellipsis kebab dots calendar copy print help info expand collapse chevron arrow").split(" "));
  function iconLabel(el) {
    const named = el.querySelector("svg title")?.textContent || el.getAttribute("data-tooltip") ||
      el.getAttribute("data-title") || el.getAttribute("aria-description");
    if (named) return named;
    // Whole words only ("fa-search", "#icon-close", "SearchIcon" → search; never "display" → play).
    const hints = [el, ...el.querySelectorAll("svg, i, span, use")]
      .map((n) => `${n.getAttribute("class") || ""} ${n.getAttribute("href") || n.getAttribute("xlink:href") || ""} ${n.getAttribute("data-icon") || ""}`)
      .join(" ")
      .split(/[^a-zA-Z]+|(?=[A-Z][a-z])/)
      .map((w) => w.toLowerCase());
    const word = hints.find((w) => ICON_WORDS.has(w));
    if (word) return `${word} (icon)`;
    // A submit button with no name at all, in a search form: it's the search button.
    const form = el.form;
    const searchForm = form && (form.matches('[role=search], [action*="search" i]') || !!form.querySelector("input[type=search]"));
    if (searchForm && el.type === "submit") return "search (icon)";
    return "";
  }

  function labelOf(el) {
    if (isClickableRow(el)) return el.getAttribute("aria-label") ? keep(el.getAttribute("aria-label"), 80) : rowLabel(el);
    return keep(
      textOfIds(el, "aria-labelledby") ||
      el.getAttribute("aria-label") ||
      (el.labels && el.labels[0] && el.labels[0].innerText) ||
      el.innerText ||
      (getComputedStyle(el).visibility === "hidden" ? el.textContent : "") || // hidden until hover: innerText is empty
      buttonInputText(el) ||
      el.getAttribute("placeholder") ||
      el.getAttribute("title") ||
      el.querySelector("img[alt]")?.alt ||
      (/^(BUTTON|A)$/.test(el.tagName) || el.getAttribute("role") === "button" ? iconLabel(el) : "") ||
      el.getAttribute("name") ||
      // A button inside a component often gets its label from the component itself.
      el.getRootNode()?.host?.getAttribute("aria-label") ||
      el.getRootNode()?.host?.innerText,
      50
    );
  }

  // A scrollable box's own name – never its text: labelOf() falls back to innerText, which for a
  // scrolling <main> is the whole page (and uncut in Unrestricted mode), repeating every label in one
  // line. A name, not page data, so it's short in both modes.
  // The first heading in a box that a person can actually see (not one in a hidden mega-menu), and
  // the first line of a text (a heading with a badge and subtitle inside it: just its title).
  function visibleHeading(box) {
    return [...box.querySelectorAll(HEADING_SELECTOR)].find((h) => !h.closest("a[href]") && isRendered(h)) || null;
  }
  const firstTextLine = (text) => (text || "").split(String.fromCharCode(10)).map((line) => line.trim()).find(Boolean) || "";

  function boxName(box) {
    const heading = visibleHeading(box);
    const name = box.getAttribute("aria-label") || textOfIds(box, "aria-labelledby") || box.getAttribute("title") ||
      (heading && heading.innerText) || (box.matches("main, [role=main]") ? "main content" : "") ||
      // An unnamed scrolling menu: say what it is ("menu"), not "".
      (closestDeep(box, "nav, aside, [role=navigation], [role=complementary]") ? "menu" : "");
    return clean(name, 40);
  }

  // ---------- clickable <div>s ----------
  // React/Vue sites attach click handlers in JavaScript, so a custom dropdown ("Select a contact")
  // and its options can be plain <div>s with no role, onclick attribute or tabindex – the only sign
  // is the pointer cursor the site gives them. Only the outermost one counts (cursor is inherited),
  // and never a box that already holds a real link/button (a product card: its links are listed).
  const POINTER_TAGS = "div, span, li, p, td, img, svg, i";
  const pointerish = new WeakSet();

  function pointerTargets(roots) {
    const found = [];
    const cursors = new Map(); // each element's style is read once (a child's check reads its parent's)
    const pointer = (el) => {
      if (!cursors.has(el)) {
        const cursor = getComputedStyle(el).cursor;
        // A custom dropdown that can't be opened yet ("Select a type" until a channel is chosen) shows
        // a not-allowed cursor instead: still a field of the form, listed as (disabled).
        cursors.set(el, cursor === "pointer" ||
          (cursor === "not-allowed" && el.children.length <= 3 && PROMPT_TEXT.test(el.innerText || "") && (el.innerText || "").length <= 60));
      }
      return cursors.get(el);
    };
    for (const r of roots) {
      for (const el of r.querySelectorAll(POINTER_TAGS)) {
        if (!pointer(el)) continue;
        const parent = el.parentElement;
        if (parent && pointer(parent)) continue; // inherited from a listed parent
        if (el.closest(SELECTOR) || el.querySelector(SELECTOR)) continue;
        pointerish.add(el);
        found.push(el);
      }
      // Table rows that open their record when clicked (see isClickableRow). Their cells were
      // skipped above: each <td> inherits the row's pointer cursor.
      for (const row of r.querySelectorAll("tr, [role=row]")) {
        if (!row.closest(SELECTOR) && isClickableRow(row)) found.push(row);
      }
    }
    return found;
  }

  // ---------- clickable table rows ----------
  // React tables often open a record by clicking anywhere on its row – a <tr onClick tabIndex=0
  // class="cursor-pointer"> with no link in it. Signs: the row's own pointer cursor (not inherited
  // from a clickable box around the table) or a tabindex. Not header rows, and not a row whose
  // first cell is already a link to the record (the link is listed; the row would repeat it).
  // A row is one element: `row link "Acme | Website revamp | ₹2L"`; its buttons stay listed too.
  const ROW_CELLS = ":scope > [role=cell], :scope > [role=gridcell], :scope > [role=columnheader], :scope > [role=rowheader]";
  const rowCells = (row) => [...(row.tagName === "TR" ? row.cells : row.querySelectorAll(ROW_CELLS))];
  // A link that goes somewhere (hash routes like "#/opportunities/9" too), not "#", script, email or phone.
  const NAV_HREF = (a) => !/^(#?$|javascript:|mailto:|tel:)/i.test((a.getAttribute("href") || "").trim());

  function isClickableRow(el) {
    if (el?.nodeType !== 1 || !(el.tagName === "TR" || el.getAttribute("role") === "row")) return false;
    if (el.closest("thead")) return false;
    const cells = rowCells(el);
    if (!cells.length || cells.every((c) => c.tagName === "TH" || c.getAttribute("role") === "columnheader")) return false;
    const focusable = el.hasAttribute("tabindex") && el.tabIndex >= 0;
    const pointer = getComputedStyle(el).cursor === "pointer" &&
      !(el.parentElement && getComputedStyle(el.parentElement).cursor === "pointer");
    if (!focusable && !pointer) return false;
    const first = cells.find((c) => (c.innerText || "").trim());
    const firstLink = first && [...first.querySelectorAll("a[href], [role=link]")].some((a) => a.getAttribute("role") === "link" || NAV_HREF(a));
    return !firstLink;
  }

  // A row's name: its cells' text, "Acme | Website revamp | Proposal | ₹2L" (as cardSummary names a TR).
  // Cells with no letter or digit ("⋯" menus, "—" blanks) are left out.
  function rowLabel(row) {
    return keep(rowCells(row).map((cell) => keep(cell.innerText, 40)).filter((t) => /[\p{L}\p{N}]/u.test(t)).join(" | "), 80);
  }

  // Where to click a row: the middle of a cell with nothing clickable in it – a row's middle is
  // often its "⋯" menu or a status badge, and the click would go to that instead.
  function rowSpots(row) {
    return rowCells(row)
      .filter((cell) => (cell.innerText || "").trim() && !cell.querySelector(SELECTOR))
      .map((cell) => {
        const r = cell.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      });
  }

  // ---------- open custom dropdown lists ----------
  // A React/Tailwind dropdown's open list is often a plain <div> (no role=listbox): positioned
  // (absolute/fixed + a z-index) under the field that opened it, poking out of that field's box,
  // holding clickable rows. Found here so it's treated like a real popup – listed first, in one piece,
  // named after its field – instead of being sorted in among the form fields it happens to cover.
  let openLists = new Map(); // open list → the field (trigger) that opened it, from the latest numbering

  function findOpenLists(clickables, shown) {
    const lists = new Map();
    const positioned = new Map(); // memo: node → is it a positioned, stacked panel?
    const panel = (node) => {
      if (!positioned.has(node)) {
        const st = getComputedStyle(node);
        positioned.set(node, (st.position === "absolute" || st.position === "fixed") && st.zIndex !== "auto" && Number(st.zIndex) > 0);
      }
      return positioned.get(node);
    };
    const rows = new Map();
    for (const el of clickables) {
      let node = el.parentElement;
      for (let i = 0; node && node !== document.body && i < 6; node = node.parentElement, i++) {
        if (panel(node)) {
          rows.set(node, (rows.get(node) || 0) + 1);
          break;
        }
      }
    }
    for (const [list, n] of rows) {
      if (n < 2 || list.matches(POPUP) || list.querySelector(POPUP)) continue; // a real popup: handled already
      const box = list.parentElement;
      if (!box || box === document.body || box === document.documentElement || !shown(list)) continue;
      const r = list.getBoundingClientRect();
      // Really open = on screen now: a site's hover sub-menus sit in the page all the time, parked off
      // to the side until the mouse is over their menu item – they aren't open dropdown lists.
      if (r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) continue;
      if (r.width * r.height > innerWidth * innerHeight * 0.5) continue; // a drawer or page overlay, not a list
      const b = box.getBoundingClientRect();
      if (r.bottom <= b.bottom + 2 && r.top >= b.top - 2) continue; // stays inside its box: layout, not a pop-out
      const trigger = clickables.find((c) => box.contains(c) && !list.contains(c));
      if (trigger) lists.set(list, trigger);
    }
    return lists;
  }

  // The open list an element sits in (null if none).
  function openListOf(el) {
    for (const list of openLists.keys()) if (list.isConnected && containsDeep(list, el)) return list;
    return null;
  }

  // Custom fields (a <div> dropdown) can't be :disabled – sites mark them with a class instead
  // ("cursor-not-allowed", "disabled", "btn--disabled"), and a later cursor-pointer class can still
  // win, so the cursor alone doesn't tell. Checks the element and its two nearest ancestors.
  const DISABLED_CLASS = /^(cursor-not-allowed|disabled|is-disabled)$|--disabled$/;
  function looksDisabled(el) {
    for (let node = el, i = 0; node && i < 3; node = node.parentElement, i++) {
      if ([...(node.classList || [])].some((c) => DISABLED_CLASS.test(c))) return true;
    }
    return getComputedStyle(el).cursor === "not-allowed";
  }

  // An option in an open custom list that only repeats the field's prompt ("Select a channel"):
  // clicking it chooses nothing and just closes the list.
  const PROMPT_TEXT = /^\s*(-+\s*)?(select|choose|pick)\b/i;

  // ---------- form fields: visible label, required mark, inline error ----------
  // Many forms (React + Tailwind and the like) show "Contact *" as a <label> that isn't linked to its
  // control, and validation errors as plain red text with no ARIA. Both sit in the field's own
  // wrapper, next to the control – read them from there.
  const CONTROL = "input:not([type=hidden]), select, textarea, [role=combobox], [role=listbox], [contenteditable=true]";
  const FORMISH = "form, [role=form], dialog, [role=dialog], [role=alertdialog], [aria-modal=true]";
  const ERROR_CLASS = /error|invalid|danger|destructive|text-red|text-rose|ant-form-item-explain/i;
  const ERROR_WORDS = /required|invalid|must |please|enter |select |choose |can.?t |cannot|at least|at most|too (short|long)|match|already|wrong|not valid|missing/i;
  const isRed = (color) => {
    const [r, g, b] = (color.match(/\d+/g) || []).map(Number);
    return r >= 180 && g <= 80 && b <= 100;
  };

  // Text that reads as a validation error: an error-ish class (or role=alert), or red + error words.
  function looksLikeError(node, text) {
    const cls = node.getAttribute("class") || "";
    if (/(^|\s)valid-feedback/.test(cls)) return false;
    if (node.matches("[role=alert]") || ERROR_CLASS.test(cls) || /invalid-feedback/.test(cls)) return ERROR_WORDS.test(text) || text.length <= 60;
    return isRed(getComputedStyle(node).color) && ERROR_WORDS.test(text);
  }

  const formLike = (el) => el.matches(CONTROL) || pointerish.has(el) || !!closestDeep(el, FORMISH);

  // A real field (something you type into or choose from), not a button: only fields get a label
  // from nearby text, otherwise a "Save" button would be named after the field above it.
  const isField = (el) => el.matches(CONTROL) || (pointerish.has(el) && !/^(BUTTON|A)$/.test(el.tagName));

  // Text before a control that acts as its label without being a <label>: Tailwind forms often use a
  // <div>/<p>/<span> for "Contact *". Short, text only, no controls or numbered elements inside.
  function labelLike(node, el) {
    if (!node || node.nodeType !== 1 || node.contains(el) || node.querySelector(CONTROL)) return false;
    if (node.matches("label, legend")) return true;
    if (!node.matches("div, span, p, dt, h4, h5, h6") || pointerish.has(node) || node.querySelector("[data-krify-id]")) return false;
    const text = clean(node.innerText || "", 80);
    return text.length > 0 && text.length <= 60 && node.children.length <= 3 && isRendered(node);
  }

  // The field's wrapper: the nearest ancestor (≤ 4 up) holding a label for it and at most 2 controls.
  // The label is the CLOSEST one before the field: in a two-column row ("Contact *" | "Channel *")
  // the first label in the box belongs to the neighbour.
  function fieldGroupOf(el) {
    let node = el;
    let child = el;
    for (let i = 0; i < 4; i++) {
      child = node;
      node = node.parentElement;
      if (!node || node === document.body) return null;
      // A whole form/row – not counting an open dropdown list's own search box and options, or
      // parts of this field itself (an icon inside a custom dropdown).
      const controls = [...node.querySelectorAll(`${CONTROL}, [data-krify-id]`)]
        .filter((c) => !openListOf(c) && (c === el || !(el.contains(c) || c.contains(el))));
      if (controls.length > 2) return null;
      let label = null;
      for (const l of node.querySelectorAll("label, legend")) {
        if (!l.contains(el) && l.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) label = l;
      }
      if (!label && isField(el)) {
        for (let sib = child.previousElementSibling; sib; sib = sib.previousElementSibling) {
          if (labelLike(sib, el)) {
            label = sib;
            break;
          }
        }
      }
      if (label) return { box: node, label };
    }
    return null;
  }

  // Is the label marked required ("Contact *", "Email (required)", a CSS ::after "*") or optional
  // ("Notes (optional)")? → "required" | "optional" | "".
  const OPTIONAL_WORD = /\boptional\b/i;
  function requiredMark(label, raw = clean(label.innerText || label.textContent || "", 80)) {
    if (OPTIONAL_WORD.test(raw)) return "optional";
    if (raw.includes("*") || /\brequired\b/i.test(raw) || /required/i.test(label.getAttribute("class") || "")) return "required";
    for (const node of [label, ...label.querySelectorAll("*")].slice(0, 6)) {
      for (const pseudo of ["::after", "::before"]) {
        const content = getComputedStyle(node, pseudo).content;
        if (content && content !== "none" && content.includes("*")) return "required";
      }
    }
    return "";
  }

  // → { label, required, optional, error } for a form field ("" / false when unknown).
  function fieldInfo(el) {
    const info = { label: "", required: false, optional: false, error: "" };
    if (!formLike(el) || openListOf(el)) return info; // an option in an open list isn't a field of its own
    const group = fieldGroupOf(el);
    if (group) {
      const raw = clean(group.label.innerText || group.label.textContent, 60);
      info.label = raw.replace(/\((required|optional)\)/gi, "").replace(/\s*\*\s*/g, " ").trim();
      const mark = requiredMark(group.label);
      info.required = mark === "required";
      info.optional = mark === "optional";
      // A custom dropdown often keeps its real value in a hidden <input required> next to the trigger.
      if (!info.required && isField(el) && group.box.querySelector("input[required], select[required], textarea[required], [aria-required=true]")) {
        info.required = true;
      }
      for (const node of group.box.querySelectorAll("*")) {
        if (node === group.label || group.label.contains(node) || node.contains(el) || el.contains(node)) continue;
        if (node.matches(CONTROL) || node.children.length > 2 || !isRendered(node)) continue;
        const text = clean(node.innerText, 120);
        if (text && text.length <= 100 && looksLikeError(node, text)) {
          info.error = text;
          break;
        }
      }
    }
    // <label>Topic <select>…</select></label>: the label wraps the field – its own words only,
    // not the options' text.
    const wrap = !group && el.labels?.[0]?.contains(el) ? el.labels[0] : null;
    if (wrap) {
      const own = clean([...wrap.childNodes].filter((n) => !n.contains(el)).map((n) => n.textContent).join(" "), 60);
      info.label = own.replace(/\((required|optional)\)/gi, "").replace(/\s*\*\s*/g, " ").trim();
      const mark = own ? requiredMark(wrap, own) : "";
      info.required = mark === "required";
      info.optional = mark === "optional";
    }
    if (el.required || el.getAttribute("aria-required") === "true") info.required = true;
    if (!info.error && el.getAttribute("aria-invalid") === "true") {
      info.error = clean(textOfIds(el, "aria-errormessage") || textOfIds(el, "aria-describedby"), 100);
    }
    return info;
  }

  // Does the field hold a value? → { filled, value }. A custom dropdown's text is its chosen value,
  // or a prompt ("Select a contact", "--", "None") while nothing is chosen.
  const EMPTY_TEXT = /^\s*(-+|—|none|please (select|choose).*|search\.*)?\s*$/i;
  function filledState(el) {
    const short = (t) => keep(t || "", 30);
    const tag = el.tagName;
    if (tag === "SELECT") {
      const opt = el.options[el.selectedIndex];
      const filled = !!opt && opt.value !== "" && !PROMPT_TEXT.test(opt.text) && !EMPTY_TEXT.test(opt.text);
      return { filled, value: filled ? short(opt.text) : "" };
    }
    if (tag === "INPUT" && (el.type === "checkbox" || el.type === "radio")) {
      const group = el.name ? [...(el.form || document).querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
      const on = group.find((i) => i.checked);
      return { filled: !!on, value: on ? short(labelOf(on)) : "" };
    }
    if (tag === "INPUT" && el.type === "file") return { filled: el.files?.length > 0, value: short(el.files?.[0]?.name) };
    if (tag === "INPUT" && el.type === "password") return { filled: !!el.value, value: el.value ? "••••" : "" };
    if (tag === "INPUT" || tag === "TEXTAREA") return { filled: el.value.trim() !== "", value: short(el.value) };
    const text = keep(el.innerText || el.textContent || "", 60);
    const empty = !text || PROMPT_TEXT.test(text) || EMPTY_TEXT.test(text) || text === el.getAttribute("placeholder");
    return { filled: !empty, value: empty ? "" : short(text) };
  }

  // The form being filled: an open dialog, else the form holding the focus, else the form with the
  // most fields on screen. → a checklist { name, marks, fields, submits } for the server's [Form] line,
  // or null. `marks`: the form marks required fields at all – if not, "not marked" means unknown,
  // not optional.
  // No dialog role and no <form> (a React drawer is often a plain fixed <div>): the floating panel
  // (position fixed/absolute + z-index) holding the most labelled fields on screen, if ≥ 2.
  function floatingPanel(numbered) {
    const counts = new Map();
    for (const { el } of numbered) {
      if (!isField(el) || openListOf(el) || !inView(el) || !fieldGroupOf(el)) continue;
      for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
        const st = getComputedStyle(node);
        if ((st.position === "fixed" || st.position === "absolute") && st.zIndex !== "auto" && !openLists.has(node)) {
          counts.set(node, (counts.get(node) || 0) + 1);
          break;
        }
      }
    }
    const best = [...counts].sort((a, b) => b[1] - a[1])[0];
    return best && best[1] >= 2 ? best[0] : null;
  }

  const SUBMIT_TEXT = /^\s*[+✓]?\s*(save|submit|add|create|update|send|continue|next|confirm|apply|register|sign ?up|book|post|done|finish|place order)\b/i;
  const MAX_FORM_FIELDS = 25;
  let lastFormRoot = null; // the form of the latest summary: leaveCheck() asks if a click is outside it
  function formSummary(numbered) {
    lastFormRoot = null;
    const fieldsIn = (root) => numbered.filter((n) => containsDeep(root, n.el) && isField(n.el) && !openListOf(n.el) &&
      !/^(submit|button|reset|image|search)$/.test(n.el.type || ""));
    const dialogs = deepAll("dialog[open], [role=dialog], [role=alertdialog], [aria-modal=true]").filter(isRendered);
    const focus = activeDeep();
    const forms = deepAll("form, [role=form]").filter(isRendered);
    let root = dialogs.filter((d) => fieldsIn(d).length).pop()
      || (focus && closestDeep(focus, "form, [role=form]"))
      || forms.map((f) => [f, fieldsIn(f).filter((n) => inView(n.el)).length]).filter(([, k]) => k >= 2)
        .sort((a, b) => b[1] - a[1])[0]?.[0];
    root ||= floatingPanel(numbered);
    if (!root) return null;
    const seenGroups = new Set();
    const fields = [];
    for (const { id, el } of fieldsIn(root)) {
      if (el.type === "radio" && el.name) {
        if (seenGroups.has(el.name)) continue; // one entry per radio group
        seenGroups.add(el.name);
      }
      const info = fieldInfo(el);
      // "Attach files" is a clickable box in front of a hidden <input type=file>: the input holds the value.
      const fileInput = pointerish.has(el) ? fieldGroupOf(el)?.box.querySelector("input[type=file]") : null;
      const { filled, value } = filledState(fileInput || el);
      fields.push({
        id, label: keep(info.label || labelOf(el), 40), required: info.required, optional: info.optional,
        filled, value, disabled: isDisabled(el), error: keep(info.error, 80),
        kind: fileInput || el.type === "file" ? "file" : el.tagName === "SELECT" || pointerish.has(el) ? "dropdown" :
          /date|time/.test(el.type || "") ? "date" : el.type === "checkbox" || el.type === "radio" ? "choice" : "text",
      });
      if (fields.length >= upTo(MAX_FORM_FIELDS)) break;
    }
    if (fields.length < 2) return null; // a lone search box isn't a form to fill
    // Nor is a search box with a category dropdown next to it (Amazon's "All Categories" + query).
    const textFields = fields.filter((f) => f.kind === "text").length;
    const searchy = root.matches?.("[role=search], [role=search] *") || closestDeep(root, "[role=search], header, [role=banner]") ||
      /search|\/s(\/|$)/i.test(root.getAttribute?.("action") || "") ||
      root.querySelector?.("input[type=search], input[name=q], input[name=k], input[name*=search i], input[name=field-keywords]");
    if (textFields <= 1 && searchy) return null;
    lastFormRoot = root;
    // Its submit button: real type=submit buttons, else the LAST save/add-like button (the footer's
    // "Save", not a "+ Add" for a list inside the form).
    const buttons = numbered.filter((n) => containsDeep(root, n.el) && (!isField(n.el) || n.el.type === "submit"));
    // (A <button> outside a <form> also says type "submit": only real form submits count.)
    let submits = buttons.filter((n) => n.el.type === "submit" && n.el.form && /^(BUTTON|INPUT)$/.test(n.el.tagName)).map((n) => n.id);
    if (!submits.length) {
      const last = buttons.filter((n) => SUBMIT_TEXT.test(labelOf(n.el))).pop();
      if (last) submits = [last.id];
    }
    const heading = root.querySelector("h1, h2, h3, [role=heading]");
    const name = keep(textOfIds(root, "aria-labelledby") || root.getAttribute("aria-label") || heading?.innerText || "", 50);
    return { name, marks: fields.some((f) => f.required || f.optional), fields, submits };
  }

  // Status/error/validation messages a site shows dynamically (toasts, banners, inline form
  // errors) – ARIA conventions a well-built site already uses for accessibility, reused here so
  // Krify can see "it failed" even when the message isn't itself a clickable element. Inline field
  // errors without ARIA are found through fieldInfo() and marked "Form error".
  const STATUS_SELECTOR = '[role="alert"], [role="status"], [aria-live="polite"], [aria-live="assertive"]';

  const offScreen = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom < 0 ? " (above the screen)" : r.top > innerHeight ? " (below the screen)" : "";
  };

  // Errors are read on the WHOLE page, not just the screen: clicking Save scrolls it to the middle,
  // which often pushes the form's error banner off the top – and then "it worked" would be assumed.
  function statusSignals() {
    const seen = new Set();
    const flagged = new Set(); // fields already reported, so the browser's own message isn't repeated
    const lines = [];
    const add = (text, el) => {
      text = keep(text, 1000);
      if (!text || seen.has(text)) return;
      seen.add(text);
      lines.push(keep(text, 150) + offScreen(el));
    };
    // Inline field errors first ("Form error – Contact: Contact is required"): they say exactly what to fix.
    for (const el of listed.values()) {
      if (!el.isConnected || !renderedEnough(el)) continue;
      const { label, error } = fieldInfo(el);
      if (!error) continue;
      flagged.add(el);
      add(`Form error – ${label || labelOf(el)}: ${error}`, el);
    }
    for (const r of allRoots()) {
      for (const el of r.querySelectorAll(STATUS_SELECTOR)) {
        // Alerts count anywhere on the page; quieter status/live regions (cart counts, "3 results")
        // only when they're on screen.
        const loud = el.matches('[role="alert"], [aria-live="assertive"]');
        if (!(loud ? renderedEnough(el) : visibleEnough(el))) continue;
        add(el.innerText, el);
      }
      // Fields the browser itself rejects (required, type=email, pattern…) – only once the user or a
      // submit touched them (:user-invalid), not every empty required field from the start.
      let rejected = [];
      try {
        rejected = r.querySelectorAll(":user-invalid");
      } catch {
        // older Chrome: no :user-invalid
      }
      for (const el of rejected) {
        if (flagged.has(el) || !el.validationMessage || !renderedEnough(el)) continue;
        add(`Form error – ${fieldInfo(el).label || labelOf(el) || el.name || el.type}: ${el.validationMessage}`, el);
      }
    }
    return lines.slice(0, upTo(5)).join(" | ");
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const label = labelOf(el);
    if (tag === "select") {
      const current = el.options[el.selectedIndex]?.text || "";
      // At most MAX_OPTIONS even in Unrestricted mode: a 45-category list costs ~250 tokens every step,
      // and select_option finds any option by its name anyway.
      const all = [...el.options].slice(0, window.__krify?.unrestricted ? MAX_OPTIONS : 12);
      const more = el.options.length > all.length ? `, +${el.options.length - all.length} more` : "";
      const options = all.map((o) => keep(o.text, 30)).join(", ");
      return `select "${label}" = "${keep(current, 40)}" (options: ${options}${more})`;
    }
    if (tag === "input" || tag === "textarea") {
      const type = tag === "textarea" ? "textarea" : `input ${el.type || "text"}`;
      if (el.type === "checkbox" || el.type === "radio") return `${type} "${label}" (${el.checked ? "checked" : "unchecked"})`;
      if (buttonInputText(el) || /^(submit|button|reset|image)$/.test(el.type)) return `${type} "${label}"`; // a button: its text is the label
      return `${type} "${label}"` + (el.value ? ` (value: "${keep(el.value, 40)}")` : " (empty)");
    }
    // A table row that opens its record (before the dropdown check: a row may end in a chevron).
    if (isClickableRow(el)) return `row link "${label}"`;
    const role = el.getAttribute("role") || (tag === "a" ? "link" : pointerish.has(el) ? "clickable" : tag);
    // A <button> that submits (or resets) its form says so: the AI must know which button sends it.
    if (tag === "button" && !el.getAttribute("role") && el.form && (el.type === "submit" || el.type === "reset")) {
      return `button ${el.type} "${label}"`;
    }
    // Opens a list of choices (a filter such as "My Activities ▾", a custom select): its text is only
    // the current choice – the other choices exist on the page only once it's open.
    if (isDropdownTrigger(el)) {
      // Its choices are often not in the page while it's closed: read them from the page's React
      // component (pagehook.js), else show what select_option saw when it last opened it.
      const known = reactOptions(el);
      const names = known?.names || seenOptions.get(el) || [];
      if (!names.length) return `dropdown "${label}"`;
      const shown = names.slice(0, window.__krify?.unrestricted ? MAX_OPTIONS : 12);
      const total = Math.max(known?.total || 0, names.length);
      const more = total > shown.length ? `, +${total - shown.length} more` : "";
      return `dropdown "${label}" (options: ${shown.map((t) => keep(t, 30)).join(", ")}${more})`;
    }
    return `${role} "${label}"`;
  }

  // A button / clickable <div> that opens a list of choices. Sites rarely mark these (no
  // aria-haspopup=listbox, no role=combobox) – the sign is the down-chevron after the text (Lucide's
  // "lucide-chevron-down", Font Awesome's "fa-caret-down", Material's "expand_more", a "▾").
  // Not: sidebar/nav groups and accordions (they toggle a section: aria-expanded without a popup),
  // menus (aria-haspopup=menu → "opens menu"), or an option inside an open list.
  const CHEVRON = /chevron-?down|caret-?down|arrow-?drop-?down|expand[-_]?more|angle-?down|keyboard[-_]arrow[-_]down|select-?(arrow|icon|indicator)|dropdown-?(arrow|icon|indicator|toggle)/i;
  function isDropdownTrigger(el) {
    const tag = el.tagName;
    if (/^(SELECT|INPUT|TEXTAREA|A)$/.test(tag)) return false;
    const role = el.getAttribute("role");
    const popup = el.getAttribute("aria-haspopup");
    if (role === "combobox" || popup === "listbox") return true;
    if (popup && popup !== "false") return false; // a menu or dialog: listed as "opens menu"/"opens dialog"
    if (!(tag === "BUTTON" || role === "button" || pointerish.has(el))) return false;
    if (el.hasAttribute("aria-expanded")) return false; // an accordion / collapsible group
    if (openListOf(el) || closestDeep(el, "nav, [role=navigation], aside, [role=complementary], [role=menu], [role=listbox]")) return false;
    if (closestDeep(el, "header, [role=banner]") && !closestDeep(el, "main, [role=main], form, dialog, [role=dialog]")) return false; // the site's top bar (account menu)
    const text = (el.innerText || "").trim();
    if (text.length > 60) return false; // a card or row, not a field
    if (/[▾▼⌄˅]$/.test(text)) return true;
    for (const icon of el.querySelectorAll("svg, i, span, img")) {
      const hint = `${icon.getAttribute("class") || ""} ${icon.getAttribute("data-icon") || ""} ${icon.getAttribute("data-testid") || ""} ` +
        `${icon.getAttribute("src") || ""} ${/^(I|SPAN)$/.test(icon.tagName) && icon.children.length === 0 && /icon/i.test(icon.getAttribute("class") || "") ? icon.textContent : ""}`;
      if (CHEVRON.test(hint)) return true;
    }
    return false;
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

  // Clears and reassigns data-krify-id (and the id → element map `find()` uses)
  // on every qualifying visible/interactive element, in visual reading order (popups first) – so
  // get_elements, find_elements and inspect() always agree on what [12] means. `start`: embedded
  // pages continue the main page's numbering instead of restarting at 1. `max`: how many elements
  // to number before stopping (operations.js gives each embedded page its share of a shared budget).
  // `whole` (getPageModel): every rendered element on the whole page, not just the screen –
  // on-screen ones and open popups first, then the rest until `max`/COLLECT_MS runs out.
  function numberElements(start = 0, max = MAX_ELEMENTS, whole = false) {
    for (const el of listed.values()) el.removeAttribute("data-krify-id");
    listed.clear();
    const roots = allRoots();
    const seenLinks = new Set();
    const candidates = [];
    const rendered = whole || window.__krify.unrestricted;
    // An open menu, dropdown or dialog comes first: on busy pages (Google Sheets has 100+ toolbar
    // buttons) its items would otherwise fall past the shown-elements window.
    const shown = rendered ? isRendered : isVisible;
    const selected = roots.flatMap((r) => [...r.querySelectorAll(SELECTOR)]);
    const pointers = pointerTargets(roots);
    const clickables = [...new Set([...selected, ...pointers])];
    // A custom dropdown's open list (a plain <div>, no role) counts as a popup too.
    openLists = findOpenLists(clickables, shown);
    const popups = [...roots.flatMap((r) => [...r.querySelectorAll(POPUP)]).filter(shown), ...openLists.keys()];
    const inPopups = new Set(clickables.filter((el) => popups.some((p) => containsDeep(p, el))));
    let all = [...new Set([...inPopups, ...clickables])];
    let nearCount = all.length;
    if (whole) {
      const near = [];
      const far = [];
      for (const el of all) (inPopups.has(el) || inView(el) ? near : far).push(el);
      // Off-screen ones nearest the screen first: when the cap or time limit cuts, it cuts what's far
      // away – not whatever comes last in the HTML (often what's just below the screen).
      const gap = new Map(far.map((el) => {
        const r = el.getBoundingClientRect();
        return [el, Math.max(0, -r.bottom, r.top - innerHeight) + Math.max(0, -r.right, r.left - innerWidth)];
      }));
      // Paging and submit buttons first of all: on a long page they're what moves the task on.
      far.sort((a, b) => keyControl(b) - keyControl(a) || gap.get(a) - gap.get(b));
      all = [...near, ...far];
      nearCount = near.length;
    }
    const deadline = performance.now() + COLLECT_MS;
    let partial = false;
    let hoverCount = 0;
    for (const [i, el] of all.entries()) {
      // Unrestricted: every element on the page – no count or time limit.
      if (whole && !window.__krify.unrestricted && (candidates.length >= max || (i >= nearCount && performance.now() > deadline))) {
        partial = true;
        break;
      }
      const visible = whole ? renderedEnough(el) : visibleEnough(el);
      // Hidden until the mouse is over its row: still listed, tagged "(shows on hover)".
      const hover = !visible && hoverCount < MAX_HOVER && hiddenUntilHover(el);
      // A disabled button in a form/dialog still tells the AI something ("Continue" is disabled =
      // a required field is missing), so the whole-page view keeps it, marked (disabled).
      const keepDisabled = whole && closestDeep(el, "form, [role=form], dialog, [role=dialog], [aria-modal=true]");
      if (!(visible || hover) || (el.disabled && !keepDisabled)) continue;
      // Fewer tokens: inside an already-listed link/button = same click; same link target = listed once
      // (shops link each product from its picture AND its title). A hover button inside a row link
      // is a different action ("…" menu of a project), so it stays.
      if (!hover && el.parentElement?.closest("[data-krify-cand]")) continue;
      if (el.tagName === "A" && el.href) {
        if (seenLinks.has(el.href)) continue;
        seenLinks.add(el.href);
      }
      const text = describe(el) + (hover ? " (shows on hover)" : "");
      if (/"\s*"/.test(text) && !/input|textarea|select/.test(text)) continue; // skip unlabeled
      if (hover) hoverCount += 1;
      // Temporary marker for the containment check above. Not on a clickable row: its own buttons
      // and links ("⋯", an email) do something else than opening the row, so they stay listed.
      if (!isClickableRow(el)) el.setAttribute("data-krify-cand", "1");
      candidates.push({ el, text, rect: el.getBoundingClientRect() });
    }
    candidates.forEach(({ el }) => el.removeAttribute("data-krify-cand"));
    // Visual reading order (top to bottom, left to right), popups first – not raw DOM order, so which elements land in the shown window reflects what a person scanning the page would reach first, not an accident of markup order.
    // An open dropdown list comes before the dialog it opened in (it covers part of it, so sorting
    // both by position would mix its options in among the dialog's other fields).
    const rank = (el) => (openListOf(el) ? 0 : inPopups.has(el) ? 1 : 2);
    candidates.sort((a, b) => rank(a.el) - rank(b.el) || a.rect.top - b.rect.top || a.rect.left - b.rect.left);
    // The rank above decides who fits in the window (an open list is never cut). The NUMBERS go to
    // the open list's options last, though: opening a list then only adds numbers at the end, instead
    // of shifting every form field by the list's length (the AI's next step would hit the wrong one).
    const kept = !whole && !window.__krify.unrestricted ? candidates.slice(0, max) : candidates;
    const inList = new Set(kept.filter(({ el }) => openListOf(el)));
    const numbered = [];
    let id = start;
    for (const { el, text, rect } of [...kept.filter((c) => !inList.has(c)), ...inList]) {
      id += 1;
      el.setAttribute("data-krify-id", String(id));
      listed.set(String(id), el);
      numbered.push({ id, el, text, rect, popup: inPopups.has(el) });
    }
    return { numbered, hasPopup: inPopups.size > 0, lastId: id, partial };
  }

  // On screen right now (a hidden <select> or see-through control counts via its wrapper).
  function inView(el) {
    return isVisible(el) || shownByWrapper(el, isVisible);
  }

  function renderedEnough(el) {
    return isRendered(el) || shownByWrapper(el, isRendered);
  }

  // Paging / "Load more" / a form's submit button – kept in the page view however far away they are.
  const KEY_TEXT = /^\s*(next( page)?|previous( page)?|prev|load more|show more|see more|view more|more results|›|»|‹|«|next ›|‹ previous)\s*$/i;
  function keyControl(el) {
    if (el.getAttribute("rel") === "next" || el.getAttribute("rel") === "prev") return true;
    if (el.form && /^(BUTTON|INPUT)$/.test(el.tagName) && (el.type === "submit" || el.type === "image")) return true;
    const text = el.textContent;
    return text.length < 30 && KEY_TEXT.test(text);
  }

  // Number the elements the latest page view left out (past its cap, or off screen in the flat list)
  // WITHOUT touching existing numbers: ids the AI already has keep pointing at the same elements.
  // New ids start after 1000, so they can't collide with an embedded page's numbers.
  function extendNumbering(root = document) {
    let id = Math.max(1000, ...[...listed.keys()].map(Number));
    const known = new Set(listed.values());
    const roots = root === document ? allRoots() : [root];
    const fresh = [...new Set([...roots.flatMap((r) => [...r.querySelectorAll(SELECTOR)]), ...pointerTargets(roots)])];
    for (const el of fresh) {
      if (known.has(el) || !renderedEnough(el) || el.disabled) continue;
      // Inside a numbered link/button: same click (a clickable row's own buttons aren't, though).
      const holder = el.parentElement?.closest("[data-krify-id]");
      if (holder && !isClickableRow(holder)) continue;
      if (/"\s*"/.test(describe(el)) && !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) continue; // unlabeled
      id += 1;
      el.setAttribute("data-krify-id", String(id));
      listed.set(String(id), el);
      known.add(el);
    }
  }


  // ---------- getPageModel(): the page as structured data ----------
  // Sections (dialog, header, nav, main, form, …) → cards (repeated items like product tiles,
  // with title/price/rating/badges) → elements (with their accessibility states). Sent to the
  // server as JSON; backend/app/agent/pageview.py turns it into the outline the AI reads.

  const KIND_LABEL = {
    dialog: "Dialog", menu: "Menu", header: "Header", nav: "Nav", search: "Search", main: "Main",
    aside: "Sidebar", footer: "Footer", form: "Form", region: "Section", toolbar: "Toolbar", menubar: "Menu bar",
  };
  const HEADING_SELECTOR = "h1, h2, h3, h4, h5, h6, [role=heading]";
  const ITEM_TAG = /^(LI|TR|ARTICLE)$/;
  const ITEM_ROLE = /^(listitem|row|article|option|gridcell)$/;
  const PRICE = /(?:₹|Rs\.?\s?|INR\s?|\$|€|£|¥)\s?\d[\d,]*(?:\.\d{1,2})?/;
  const BADGES = /\b(Sponsored|Out of stock|Currently unavailable|Sold out|Best ?seller|Limited time deal|Deal of the day|Amazon's Choice|Only \d+ left(?: in stock)?)\b/gi;

  // Is this node the start of a section? → { kind, modal } or null.
  function sectionKind(node) {
    if (node.nodeType !== 1) return null;
    if (openLists.has(node)) return { kind: "menu", modal: false, list: true };
    const role = node.getAttribute("role");
    const tag = node.tagName;
    if (node.matches(POPUP)) {
      let modal = node.getAttribute("aria-modal") === "true" || role === "alertdialog";
      try {
        modal = modal || node.matches(":modal");
      } catch {
        // older Chrome: no :modal
      }
      return { kind: role === "menu" || role === "listbox" ? "menu" : "dialog", modal };
    }
    const nested = () => node.parentElement?.closest("article, section, main, aside, [role=main], [role=article]");
    if (role === "banner" || (tag === "HEADER" && !role && !nested())) return { kind: "header" };
    if (role === "contentinfo" || (tag === "FOOTER" && !role && !nested())) return { kind: "footer" };
    // Old-style pages build them from plain boxes: known ids/classes, page-wide, not inside the content.
    if (!role && /^(DIV|SECTION|NAV)$/.test(tag) && !nested()) {
      const names = `${node.id} ${typeof node.className === "string" ? node.className : ""}`;
      const wide = node.getBoundingClientRect().width >= innerWidth * 0.6;
      if (wide && /(^|\s)(nav-?footer|site-footer|page-footer|global-footer|footer)(\s|$)/i.test(names)) return { kind: "footer" };
      if (wide && /(^|\s)(navbar|nav-?belt|masthead|site-header|page-header|global-header|header)(\s|$)/i.test(names)) return { kind: "header" };
    }
    if (role === "navigation" || tag === "NAV") return { kind: "nav" };
    if (role === "search" || tag === "SEARCH") return { kind: "search" };
    if (role === "main" || tag === "MAIN") return { kind: "main" };
    if (role === "complementary" || tag === "ASIDE") return { kind: "aside" };
    if ((role === "form" || tag === "FORM") && node.querySelector("input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea, [contenteditable=true]")) {
      return { kind: "form" };
    }
    if (role === "toolbar") return { kind: "toolbar" };
    if (role === "menubar") return { kind: "menubar" };
    if ((role === "region" || tag === "SECTION") && (node.getAttribute("aria-label") || node.getAttribute("aria-labelledby")) &&
      !/\b\d+(?:[-_]\d+)+\s*$/.test(node.getAttribute("aria-label") || "")) {
      return { kind: "region" };
    }
    return null;
  }

  // A section's own name: its label, or its first heading (not for main, whose headings become
  // groups of their own: "Main › "Results"").
  function sectionName(node, kind) {
    const trigger = openLists.get(node);
    if (trigger) return keep(fieldInfo(trigger).label || labelOf(trigger), 50); // "Channel"
    const label = textOfIds(node, "aria-labelledby") || node.getAttribute("aria-label");
    if (label) return keep(label, 50);
    if (kind === "main" || kind === "aside") return "";
    return keep(firstTextLine(visibleHeading(node)?.innerText), 50);
  }

  // Sibling signature, as in listCounts(): same tag + classes = "looks alike". Semantic list
  // items (li, tr, article, role=listitem/row) also count by tag/role alone.
  function itemKeys(el) {
    const keys = [el.tagName + "." + [...el.classList].filter((c) => !/active|selected|hover|focus|odd|even/i.test(c)).sort().join(".")];
    const role = el.getAttribute("role");
    if (ITEM_TAG.test(el.tagName)) keys.push("tag:" + el.tagName);
    if (role && ITEM_ROLE.test(role)) keys.push("role:" + role);
    return keys;
  }

  // Price strings in a card: the main one (largest text, not struck through) and the others
  // labelled (M.R.P / was / EMI / save) rather than dropped. → "₹699 (M.R.P ₹999)" or "".
  function pricesOf(card) {
    const byValue = new Map();
    for (const el of card.querySelectorAll("*")) {
      const tc = el.textContent || "";
      if (tc.length > 40 || !PRICE.test(tc)) continue;
      if ([...el.children].some((kid) => PRICE.test(kid.textContent || ""))) continue; // a smaller one has it
      const value = tc.match(PRICE)[0].replace(/\s+/g, "");
      const style = getComputedStyle(el);
      const struck = !!el.closest("del, s, strike") || /line-through/.test(style.textDecorationLine) ||
        /line-through/.test(el.parentElement ? getComputedStyle(el.parentElement).textDecorationLine : "");
      // Words just before/after the price, from the nearest short ancestor ("M.R.P: ₹999", "₹33/month").
      let context = el;
      for (let i = 0; i < 3 && context.parentElement && (context.parentElement.textContent || "").length < 90; i++) {
        context = context.parentElement;
      }
      const ctx = (context.textContent || "").replace(/\s+/g, " ");
      const at = ctx.indexOf(tc.trim());
      const before = at > 0 ? ctx.slice(Math.max(0, at - 24), at) : "";
      const after = at >= 0 ? ctx.slice(at + tc.trim().length, at + tc.trim().length + 14) : "";
      let label = "";
      if (/M\.?\s?R\.?\s?P/i.test(before)) label = "M.R.P";
      else if (/\b(was|list|typical)\b/i.test(before) || struck) label = "was";
      else if (/EMI/i.test(before + after) || /^\s*(\/\s?mo|per month|\/month|a month)/i.test(after)) label = "EMI";
      else if (/\b(save|saving)\b/i.test(before) || /^\s*(off|discount)\b/i.test(after)) label = "save";
      const size = el.getBoundingClientRect().width > 2 ? parseFloat(style.fontSize) || 0 : 0;
      const old = byValue.get(value);
      if (!old) byValue.set(value, { value, label, size });
      else {
        old.size = Math.max(old.size, size);
        if (label && !old.label) old.label = label; // any copy struck through / labelled wins
      }
    }
    const all = [...byValue.values()];
    if (!all.length) return "";
    const plain = all.filter((p) => !p.label);
    const main = (plain.length ? plain : all).reduce((a, b) => (b.size > a.size ? b : a));
    const others = all.filter((p) => p !== main).slice(0, upTo(3)).map((p) => `${p.label || "also"} ${p.value}`);
    return others.length ? `${main.value} (${others.join(", ")})` : main.value;
  }

  function ratingOf(card, text) {
    const labelled = card.querySelector('[aria-label*="out of 5" i], [title*="out of 5" i]');
    const source = labelled ? labelled.getAttribute("aria-label") || labelled.getAttribute("title") : text;
    const m = source.match(/(\d(?:\.\d)?)\s*(?:out of 5|★|\/\s?5\b)/i) || source.match(/★\s*(\d(?:\.\d)?)/);
    return m ? `★${m[1]}` : "";
  }

  // Short summary of a repeated item (product tile, search result, table row).
  function cardSummary(card, els) {
    const text = (card.innerText || "").replace(/\s+/g, " ");
    const head = card.querySelector(HEADING_SELECTOR);
    let title = head ? keep(head.innerText, 80) : "";
    if (!title && card.tagName === "TR") {
      title = keep([...card.cells].map((cell) => keep(cell.innerText, 40)).filter(Boolean).join(" | "), 80);
    }
    if (!title) {
      const longest = (list) => list.map((e) => labelOf(e.el)).filter(Boolean).sort((a, b) => b.length - a.length)[0];
      const link = longest(els.filter((e) => e.el.closest?.("a[href]")));
      const any = longest(els);
      title = link ? keep(link, 80) : any ? keep(any, 80) : keep(text, 80);
    }
    const badges = [...new Set((text.match(BADGES) || []).map((b) => b.replace(/\s+/g, " ")))].slice(0, upTo(3));
    return { title, price: pricesOf(card), rating: ratingOf(card, text), badges };
  }

  // Clicking a submit button runs the browser's own checks (required, type=email, pattern…). When they
  // fail, the browser shows a bubble that isn't part of the page and sends nothing – say which fields.
  function validationProblems(el) {
    const form = el.form;
    const submits = form && /^(BUTTON|INPUT)$/.test(el.tagName) && (el.type === "submit" || el.type === "image");
    if (!submits || form.noValidate || el.formNoValidate) return "";
    return [...form.elements]
      .filter((f) => f.willValidate && !f.validity.valid)
      .slice(0, upTo(4))
      .map((f) => `Form error – ${fieldInfo(f).label || labelOf(f) || f.name}: ${f.validationMessage.replace(/\.$/, "")}`)
      .join("; ");
  }

  // A link or form that would open a new tab (target="_blank", a page-wide <base target>, a submit
  // button's formtarget): point it at this tab for the click, then put it back. Named targets that are
  // frames on this page are left alone. Returns the undo (a no-op when nothing changed).
  function keepInThisTab(el) {
    const undo = [];
    const retarget = (node, attr) => {
      const own = (node.getAttribute(attr) || "").trim();
      const target = own || (attr === "target" ? document.querySelector("base[target]")?.getAttribute("target") || "" : "");
      if (!target || /^_(self|parent|top)$/i.test(target)) return;
      if (target !== "_blank" && document.querySelector(`iframe[name="${CSS.escape(target)}"], frame[name="${CSS.escape(target)}"]`)) return;
      undo.push(() => (own ? node.setAttribute(attr, own) : node.removeAttribute(attr)));
      node.setAttribute(attr, "_self");
    };
    const link = el.closest?.("a[href], area[href]");
    if (link) retarget(link, "target");
    const submitter = el.closest?.("button, input[type=submit], input[type=image]");
    if (submitter?.form) {
      if (submitter.hasAttribute("formtarget")) retarget(submitter, "formtarget");
      else retarget(submitter.form, "target");
    }
    return () => undo.forEach((fn) => fn());
  }

  // Runs a click/submit with new-tab links kept in this tab, when the side panel's switch is on.
  function inThisTab(el, fn) {
    const undo = window.__krify.sameTab ? keepInThisTab(el) : () => {};
    try {
      return fn();
    } finally {
      undo();
    }
  }

  // The form's default button – the one a real Enter "clicks": its first submit button.
  function defaultButtonOf(form) {
    return [...form.elements].find((f) => /^(BUTTON|INPUT)$/.test(f.tagName) && (f.type === "submit" || f.type === "image")) || null;
  }

  const TEXT_INPUT = /^(text|search|email|url|tel|password|number|date|datetime-local|month|week|time)$/;

  // What a real browser does after an Enter the page didn't handle itself (fake key events never
  // trigger it, so it's done here – and only what the browser would do, never more).
  function enterDefault(target) {
    const tag = target.tagName;
    // A focused button or link: Enter clicks it.
    if (tag === "A" && target.href || tag === "BUTTON" || (tag === "INPUT" && /^(submit|button|reset|image)$/.test(target.type))) {
      if (isDisabled(target)) return "Pressed Enter – nothing happened: the focused button is disabled";
      inThisTab(target, () => target.click());
      return `Pressed Enter on ${describe(target)}`;
    }
    // A multi-line box: Enter is a new line.
    if (tag === "TEXTAREA" || target.isContentEditable) {
      document.execCommand("insertText", false, "\n");
      return "Pressed Enter (a new line in the text box – to send, click the send/submit button)";
    }
    const form = tag === "INPUT" && TEXT_INPUT.test(target.type) ? target.form : null;
    if (!form) return "Pressed Enter (no form here – if nothing happens, click the search/send button instead)";
    const button = defaultButtonOf(form);
    if (button) {
      if (isDisabled(button)) {
        return `Pressed Enter – the form was NOT sent: its submit button "${labelOf(button)}" is disabled (a required field is probably empty)`;
      }
      const blocked = validationProblems(button);
      inThisTab(button, () => button.click()); // what the browser does: runs its checks and submit handlers, with this submitter
      return blocked ? `Pressed Enter – but the browser did NOT send the form: ${blocked}` : `Pressed Enter (sends the form via ${describe(button)})`;
    }
    // No submit button: the browser only sends a form with a single text field this way.
    const fields = [...form.elements].filter((f) => f.tagName === "INPUT" && TEXT_INPUT.test(f.type));
    if (fields.length === 1) {
      inThisTab(target, () => form.requestSubmit());
      return "Pressed Enter (sends the form)";
    }
    return "Pressed Enter – nothing was sent: this form has no submit button; click the button that sends it";
  }

  // Disabled for real: the property, a disabled <fieldset> around it (:disabled), or aria-disabled
  // (custom buttons that look clickable but ignore clicks).
  function isDisabled(el) {
    try {
      if (el.matches(":disabled")) return true;
    } catch {
      // not a form element
    }
    return el.disabled === true || el.getAttribute("aria-disabled") === "true" || (pointerish.has(el) && looksDisabled(el));
  }

  // Accessibility states the plain description doesn't already show. `field`: fieldInfo(el), so an
  // unlinked "Contact *" label and a plain red "Contact is required" count as required / invalid too.
  function statesOf(el, field = fieldInfo(el)) {
    const a = (name) => el.getAttribute(name);
    const role = a("role");
    const states = [];
    if (/^(checkbox|switch|radio|menuitemcheckbox|menuitemradio)$/.test(role || "") && a("aria-checked") !== null) {
      states.push({ true: "checked", mixed: "partly checked" }[a("aria-checked")] || "unchecked");
    }
    if (a("aria-expanded") === "true") states.push("expanded");
    else if (a("aria-expanded") === "false") states.push("collapsed");
    else if ([...openLists.values()].includes(el)) states.push("expanded – its list is open");
    const list = openListOf(el);
    if (list && el !== openLists.get(list) && !/^(INPUT|TEXTAREA)$/.test(el.tagName)) {
      const own = labelOf(el);
      if (PROMPT_TEXT.test(own)) states.push("placeholder – chooses nothing");
      else if (own && own === labelOf(openLists.get(list))) states.push("selected");
    }
    const popup = a("aria-haspopup");
    if (popup && popup !== "false") {
      states.push({ listbox: "opens list", dialog: "opens dialog", grid: "opens picker", tree: "opens picker" }[popup] || "opens menu");
    }
    if (a("aria-selected") === "true") states.push("selected");
    if (a("aria-pressed") === "true") states.push("pressed");
    if (a("aria-current") && a("aria-current") !== "false") states.push("current");
    if (isDisabled(el)) states.push("disabled");
    if (el.required || a("aria-required") === "true" || field.required) states.push("required");
    else if (field.optional) states.push("optional");
    let invalid = a("aria-invalid") === "true" || !!field.error;
    try {
      invalid = invalid || el.matches(":user-invalid"); // only after the user touched it, not every empty required field
    } catch {
      // older Chrome
    }
    if (invalid) states.push("invalid");
    if ((el.readOnly && /^(INPUT|TEXTAREA)$/.test(el.tagName)) || a("aria-readonly") === "true") states.push("readonly");
    if (role === "slider" || role === "spinbutton") {
      const now = a("aria-valuetext") || a("aria-valuenow");
      const min = a("aria-valuemin");
      const max = a("aria-valuemax");
      if (now !== null) states.push(`value ${keep(now, 20)}${min !== null && max !== null ? ` of ${min}–${max}` : ""}`);
    }
    const sort = closestDeep(el, "th, [role=columnheader]")?.getAttribute("aria-sort");
    if (sort === "ascending" || sort === "descending") states.push(`sorted ${sort}`);
    return states;
  }

  // Extra help text for a field: the field's visible label when the element's own label doesn't
  // say it ("Select a contact" → field: Contact), its error, else its aria-describedby text or a
  // placeholder the label doesn't show.
  function hintOf(el, field = fieldInfo(el)) {
    const own = labelOf(el);
    const parts = [];
    if (field.label && !own.toLowerCase().includes(field.label.toLowerCase())) parts.push(`field: ${field.label}`);
    if (field.error) parts.push(`error: ${field.error}`);
    if (!field.error) {
      // Only a description a person can see: hidden hover pop-ups ("Compare PlansRequest Demo") and
      // screen-reader instructions ("use your keyboard's up or down arrows") are noise for the AI.
      const root = el.getRootNode();
      const seen = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean)
        .map((id) => root.getElementById?.(id) || document.getElementById(id)).filter((d) => d && isRendered(d));
      const described = keep(seen.map((d) => d.innerText || "").join(" ").trim(), 60);
      const placeholder = keep(el.getAttribute("placeholder") || "", 40);
      if (described) parts.push(described);
      else if (placeholder && !own.includes(placeholder) && /^(INPUT|TEXTAREA)$/.test(el.tagName)) parts.push(placeholder);
    }
    return parts.join("; ");
  }

  // Column header of a table cell the element sits in.
  function columnOf(el) {
    const cell = closestDeep(el, "td");
    const table = cell && closestDeep(cell, "table");
    if (!table) return "";
    const head = table.tHead?.rows[0] || [...table.rows].find((r) => r.cells[0]?.tagName === "TH");
    const th = head?.cells[cell.cellIndex];
    return th && th !== cell ? keep(th.innerText, 30) : "";
  }

  // "1-48 of over 10,000 results" – the site's own count, if it shows one.
  function resultCount() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const re = /(?:[\d,]+\s*[-–]\s*[\d,]+\s+of\s+)?(?:over\s+|about\s+)?[\d,]+\+?\s+(?:results?|items?|products?|listings?)\b/i;
    for (let n = 0; n < 5000 && walker.nextNode(); n++) {
      const node = walker.currentNode;
      if (!/result|item|product|listing/i.test(node.nodeValue)) continue;
      const parent = node.parentElement;
      if (!parent || /^(SCRIPT|STYLE|NOSCRIPT|OPTION)$/.test(parent.tagName)) continue;
      const text = (parent.closest("div, p, span, h1, h2, h3") || parent).textContent.replace(/\s+/g, " ");
      const m = text.length < 200 && text.match(re);
      if (m && isRendered(parent)) return clean(m[0], 50);
    }
    return "";
  }

  // A rough page type, as a hint to the AI (never used for decisions in code).
  function pageType(cardCount, numbered, results) {
    const url = location.href;
    if (/docs\.google\.com\/spreadsheets\//.test(url)) return "spreadsheet";
    if (/docs\.google\.com\/document\//.test(url)) return "document";
    if (deepAll("input[type=password]").some(isRendered)) return "login";
    if (/checkout|\/cart\b|basket|payment/i.test(location.pathname)) return "checkout/cart";
    if (window === top && /^\/?(index\.\w+)?$/.test(location.pathname) && !location.search) return "home";
    const searchy = /[?&](q|k|query|search|s|keyword|keywords|text)=/i.test(location.search) ||
      /search/i.test(location.pathname) || !!results;
    if (cardCount >= 3 && searchy) return "search results";
    if (cardCount >= 5) return "list";
    if (cardCount < 3 && numbered.some((n) => /add to (cart|bag|basket)|buy now/i.test(n.text))) return "product";
    const fields = numbered.filter((n) => /^(input|textarea|select)/.test(n.text) && n.el.closest("form")).length;
    if (fields >= 3) return "form";
    if (document.querySelectorAll("article p, main p").length >= 5) return "article";
    return "page";
  }

  function buildPageModel(start, max) {
    const { numbered, hasPopup, lastId, partial } = numberElements(start, max, true);

    // Sections enclosing a node, outermost first (memoised: siblings share their ancestors' work).
    const kinds = new Map();
    const kindOf = (node) => {
      if (!kinds.has(node)) kinds.set(node, sectionKind(node));
      return kinds.get(node);
    };
    const chains = new Map();
    const chainOf = (node) => {
      if (!node) return [];
      if (chains.has(node)) return chains.get(node);
      const outer = chainOf(parentOf(node));
      const chain = kindOf(node) ? [...outer, node] : outer;
      chains.set(node, chain);
      return chain;
    };
    const labelOfSection = (node) => {
      const name = sectionName(node, kindOf(node).kind);
      return name ? `${KIND_LABEL[kindOf(node).kind]} "${name}"` : KIND_LABEL[kindOf(node).kind];
    };
    // One section per distinct path, named by the innermost two sections ("Main › Form "Checkout""),
    // a popup on its own, plus a heading group inside main (or a page with no landmarks at all).
    const sections = [];
    const sectionIds = new Map();
    const sectionFor = (chain, heading) => {
      const chainKinds = chain.map((node) => kindOf(node));
      const popupAt = chainKinds.findLastIndex((k) => k.kind === "dialog" || k.kind === "menu");
      const parts = (popupAt >= 0 ? chain.slice(popupAt) : chain.slice(-2)).map(labelOfSection);
      if (heading && !parts.some((p) => p.endsWith(`"${heading.text}"`))) parts.push(`"${heading.text}"`);
      const name = parts.join(" › ") || "Page";
      if (!sectionIds.has(name)) {
        sectionIds.set(name, sections.length);
        sections.push({
          name,
          kind: chainKinds.length ? chainKinds[chainKinds.length - 1].kind : "page",
          kinds: chainKinds.map((k) => k.kind),
          modal: !!(popupAt >= 0 && chainKinds[popupAt].modal),
          list: !!(popupAt >= 0 && chainKinds[popupAt].list), // a custom dropdown's open list
        });
      }
      return sectionIds.get(name);
    };

    // Cards: the nearest repeated-sibling ancestor (inside the element's own section) that holds
    // 2+ numbered elements or a price – so nav links and colour swatches don't each become a card.
    const tallies = new Map();
    const repeated = (node) => {
      const parent = node.parentElement;
      const min = node.tagName === "TR" ? 2 : 3; // two table rows are already a list
      if (!parent || parent.children.length < min || parent.tagName === "SELECT") return false;
      if (!tallies.has(parent)) {
        const tally = new Map();
        for (const kid of parent.children) {
          if (!(kid.textContent || "").trim()) continue;
          for (const key of itemKeys(kid)) tally.set(key, (tally.get(key) || 0) + 1);
        }
        tallies.set(parent, tally);
      }
      const tally = tallies.get(parent);
      return itemKeys(node).some((k) => (tally.get(k) || 0) >= min);
    };
    const hasPrice = new Map();
    const priced = (node) => {
      if (!hasPrice.has(node)) {
        const text = node.textContent || "";
        hasPrice.set(node, text.length < 3000 && PRICE.test(text));
      }
      return hasPrice.get(node);
    };
    const info = numbered.map((n) => {
      const chain = chainOf(n.el);
      const section = chain[chain.length - 1] || null;
      const candidates = [];
      for (let node = n.el, i = 0; node && node !== section && i < 12; node = parentOf(node), i++) {
        if (node === document.body || node === document.documentElement) break;
        if (repeated(node)) candidates.push(node);
      }
      return { ...n, chain, section, candidates };
    });
    const holds = new Map();
    for (const n of info) for (const c of n.candidates) holds.set(c, (holds.get(c) || 0) + 1);
    // Menus aren't lists of items: the groups of a sidebar menu ("Relationships", "Marketing"…) repeat
    // like product cards, but a ▸ card line with a made-up title would only confuse.
    const MENU_KINDS = new Set(["nav", "aside", "menubar", "toolbar", "header", "footer"]);
    for (const n of info) {
      if (n.chain.some((node) => MENU_KINDS.has(kindOf(node).kind))) {
        n.card = null;
        continue;
      }
      // Table rows always count (a "View" link per order row needs its row to say which order).
      n.card = n.candidates.find((c) => (holds.get(c) >= 2 || priced(c) || c.tagName === "TR") &&
        (c.textContent || "").length < 3000) || null;
    }
    // A card is the WHOLE list item: grow each card out to the largest repeated box around it that
    // holds no other card – so a video's channel link and view count stay with its title (YouTube).
    const firstCards = [...new Set(info.map((n) => n.card).filter(Boolean))];
    const grown = new Map();
    for (const card of firstCards) {
      let top = card;
      for (let p = parentOf(card), i = 0; p && p.nodeType === 1 && p !== document.body && i < 6; p = parentOf(p), i++) {
        if ((p.textContent || "").length >= 3000) break;
        if (firstCards.some((other) => other !== card && containsDeep(p, other))) break;
        if (repeated(p)) top = p;
      }
      grown.set(card, top);
    }
    const grownCards = new Set(grown.values());
    for (const n of info) {
      if (n.card) {
        n.card = grown.get(n.card);
        continue;
      }
      if (n.chain.some((node) => MENU_KINDS.has(kindOf(node).kind))) continue;
      for (let p = parentOf(n.el), i = 0; p && p !== n.section && i < 12; p = parentOf(p), i++) {
        if (grownCards.has(p)) {
          n.card = p;
          break;
        }
      }
    }
    // A row of tabs or filter chips is a control, not a list of items.
    const byCard = new Map();
    for (const n of info) if (n.card) byCard.set(n.card, [...(byCard.get(n.card) || []), n]);
    for (const els of byCard.values()) {
      const controls = els.every((n) => /^(tab|option|menuitem\w*|radio) /.test(n.text) ||
        /^(tab|option|menuitem)/.test(n.el.getAttribute("role") || ""));
      if (controls) for (const n of els) n.card = null;
    }
    const cardNodes = new Set(info.map((n) => n.card).filter(Boolean));

    // Headings split big sections (main, or a page with no landmarks at all) into named groups –
    // but not headings inside cards (each product's own title would make a group per product).
    // (Also headings in any repeated block, even one past the collection cap that holds no
    // collected element – otherwise a far-off product's title would name the group.)
    // (From the heading's parent up: headings that are siblings of each other – a flat page with
    // several <h2>s straight in <main> – are section titles, not repeated item titles.)
    const insideCard = (node) => {
      for (let p = parentOf(node), i = 1; p && i < 15; p = parentOf(p), i++) {
        if (cardNodes.has(p) || (i < 6 && p.nodeType === 1 && repeated(p))) return true;
      }
      return false;
    };
    const headings = deepAll("h1, h2, h3, [role=heading]")
      .filter((h) => {
        const level = Number(h.getAttribute("aria-level") || h.tagName.slice(1)) || 2;
        return level <= 3 && isRendered(h) && !insideCard(h);
      })
      .map((h) => {
        const chain = chainOf(h);
        return { node: h, y: h.getBoundingClientRect().top, section: chain[chain.length - 1] || null, text: keep(firstTextLine(h.innerText), 50) };
      })
      // A heading inside one of several side-by-side boxes (a process step, a footer column) only
      // titles its own box: it mustn't split the whole area at its height (in full mode it's shown as
      // a ### line in its box instead).
      // (Nor a heading inside a link: a search result's <a><h3>title</h3></a> is the link's own label,
      // and splitting there would push each result's link to the end of the previous result.)
      .filter((h) => h.text && !closestDeep(h.node, "a[href]") && !inNarrowColumn(h.node, h.section))
      .sort((a, b) => a.y - b.y);

    const cards = [];
    const cardIds = new Map();
    const elements = [];
    const full = window.__krify.unrestricted;
    const fieldLabels = new Set(); // visible labels of numbered fields: not repeated as text
    // Labels used by more than one element: each of those gets an owner (what it belongs to).
    const labelCount = new Map();
    // (Same role, label AND destination: a search button and box, or two links to different pages, aren't look-alikes.)
    const alikeKey = (n) => `${n.text.split(" ")[0]}|${labelOf(n.el).trim().toLowerCase()}|${n.el.closest?.("a[href]")?.href || ""}`;
    for (const n of info) if (labelOf(n.el).trim()) labelCount.set(alikeKey(n), (labelCount.get(alikeKey(n)) || 0) + 1);
    // The section a node at screen height `top` belongs to: its landmark chain, and inside main (or
    // a page with no landmarks) the heading above it. Used for elements and, in full mode, text.
    const placeOf = (chain, top) => {
      const section = chain[chain.length - 1] || null;
      const innerKind = section ? kindOf(section).kind : "page";
      let heading = null;
      if (innerKind === "main" || innerKind === "page") {
        for (const h of headings) {
          if (h.y > top + 2) break;
          if (h.section === section) heading = h;
        }
      }
      return sectionFor(chain, heading);
    };
    // Where a section sits on the page (page coordinates), grown from its elements and text.
    const growBox = (s, r) => {
      const box = sections[s].box || (sections[s].box = [Infinity, Infinity, -Infinity, -Infinity]);
      box[0] = Math.min(box[0], r.left + scrollX);
      box[1] = Math.min(box[1], r.top + scrollY);
      box[2] = Math.max(box[2], r.right + scrollX);
      box[3] = Math.max(box[3], r.bottom + scrollY);
    };
    for (const n of info) {
      const s = placeOf(n.chain, n.rect.top);
      let c = -1;
      if (n.card) {
        if (!cardIds.has(n.card)) {
          cardIds.set(n.card, cards.length);
          cards.push({ node: n.card, els: [] });
        }
        c = cardIds.get(n.card);
        cards[c].els.push(n);
      }
      const r = n.rect;
      // Where the section sits on the page (page coordinates, grown from its elements): pageview.py
      // shows the sections in the order a person sees them – top bar, left sidebar, main, right column.
      growBox(s, r);
      const pos = inView(n.el) ? 0 : r.bottom < 0 ? -1 : r.top > innerHeight ? 1 : 2;
      const field = fieldInfo(n.el);
      const go = destinationOf(n.el);
      // (Inside a card the ▸ line already says what it belongs to.)
      const of = n.card ? "" : ownerOf(n.el, labelOf(n.el), (labelCount.get(alikeKey(n)) || 0) > 1);
      if (field.label) fieldLabels.add(field.label.toLowerCase());
      elements.push({
        id: n.id, text: n.text, states: statesOf(n.el, field), hint: hintOf(n.el, field),
        col: columnOf(n.el), pos, s, c,
        y: Math.round(r.top + scrollY), x: Math.round(r.left + scrollX), // page order, to place text between elements
        ...(full ? { k: readingKey(n.el) } : {}),
        ...(floating(n.el) ? { float: true } : {}),
        ...(go ? { go } : {}),
        ...(of ? { of } : {}),
      });
    }
    // Full mode: the page's own text, as blocks placed among the elements (see pageTextBlocks).
    const texts = full ? pageTextBlocks(placeOf, chainOf, cardIds, fieldLabels, growBox) : [];
    // Card summaries cost a few DOM reads each: on-screen cards always, all of them in full mode.
    let summaries = 0;
    const cardList = cards.map((card, i) => {
      // A clickable row is itself numbered: pageview.py shows its element line as the card's title
      // line instead of a summary that would repeat the same cell text.
      const self = card.els.find((n) => n.el === card.node && isClickableRow(n.el));
      if (self) return { title: "", price: "", rating: "", badges: [], summarised: false, row: self.id };
      const visible = elements.some((e) => e.c === i && e.pos === 0);
      if ((!visible && !full) || (summaries >= MAX_CARD_SUMMARIES && !full)) return { title: "", price: "", rating: "", badges: [], summarised: false };
      summaries += 1;
      return { ...cardSummary(card.node, card.els), summarised: true };
    });

    // Scrollable boxes and a scrolling embedded page get numbers too (as in getElements).
    let id = lastId;
    const extra = (el, text) => {
      id += 1;
      el.setAttribute("data-krify-id", String(id));
      listed.set(String(id), el);
      elements.push({ id, text, states: [], hint: "", col: "", pos: 0, s: sectionFor(chainOf(el), null), c: -1 });
    };
    for (const box of scrollBoxes()) {
      extra(box, `scrollable box "${boxName(box)}" (scroll with element=${id + 1} to see more)`);
    }
    const doc = document.documentElement;
    const below = doc.scrollHeight - (scrollY + innerHeight);
    if (window !== top && doc.scrollHeight > innerHeight + 4) {
      const at = below > 50 ? (scrollY > 4 ? "more above and below" : "more below") : "at the bottom";
      extra(doc, `this embedded page (${at} – scroll with element=${id + 1})`);
    }

    for (const sec of sections) if (sec.box) sec.box = sec.box.map(Math.round);

    const focusedEl = activeDeep();
    const focused = focusedEl && focusedEl !== document.body ? Number(focusedEl.getAttribute("data-krify-id")) || null : null;
    const mainCards = cards.filter((_, i) => elements.some((e) => e.c === i && /main|page/.test(sections[e.s].kind))).length;
    const h1 = deepAll("h1, [role=heading][aria-level='1']").find(isRendered);
    const results = window === top ? resultCount() : "";
    return {
      page: {
        type: window === top ? pageType(mainCards || cards.length, numbered, results) : "",
        h1: h1 ? keep(h1.innerText, 80) : "",
        results,
        scrollY: Math.round(scrollY),
        scrollMax: Math.max(0, doc.scrollHeight - innerHeight),
        width: Math.round(innerWidth), // to tell a left sidebar / right column from the main area
        summary: window === top ? pageSummary() : "",
        facts: window === top ? seoFacts(pageType(mainCards || cards.length, numbered, results)) : [],
      },
      status: statusSignals(),
      form: formSummary(numbered),
      focused,
      popup: hasPopup,
      partial,
      sections,
      cards: cardList,
      elements,
      texts,
      counts: full && window === top ? listCounts() : [],
      count: id - start,
    };
  }

  // A small box that stays put while the page scrolls (chat bubble, WhatsApp / "Request a quote"
  // button): not part of the header it may sit in. A fixed or sticky bar across the page is not one.
  function floating(el) {
    for (let node = el, i = 0; node && node !== document.body && i < 12; node = parentOf(node), i++) {
      if (node.nodeType !== 1) continue;
      if (cachedStyle(node).position !== "fixed") continue;
      const r = node.getBoundingClientRect();
      return r.width < innerWidth * 0.4 && r.height < innerHeight * 0.5;
    }
    return false;
  }

  const LONG_LABEL = 100; // a numbered element with more text than this (a clickable card) keeps its text as a text block
  const SHORT_SAID = 60; // a link/button text up to this long is "said" even when it isn't numbered ("Read More »")
  // Full mode: the page's own text, one block per paragraph / heading / list item / cell (the
  // nearest non-inline box around it), in page order, each placed in its section (and card) like
  // the elements, so pageview.py can show text and elements together. Headings are marked (h), and
  // named images ("Client: Tata") come along as `image "…"` lines. Left out: text that is only an
  // element's label (its element line already says it – also links the numbering skipped as
  // repeats, like "Read More »"), a field's visible label (its field line says it), hidden or
  // screen-reader-only text.
  function pageTextBlocks(placeOf, chainOf, cardIds, fieldLabels, growBox) {
    const blocks = new Map(); // block element → { parts, own, top, left }
    const range = document.createRange();
    const blockOf = (node) => {
      let block = node;
      while (block && block !== document.body && /^inline/.test(cachedStyle(block).display)) block = parentOf(block);
      return block || document.body;
    };
    for (const node of textNodes(document.body)) {
      const text = node.nodeValue.replace(/\s+/g, " ").trim();
      const parent = node.parentElement;
      if (!text || !parent || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width <= 1 || r.height <= 1) continue; // not laid out, or a screen-reader-only 1px box
      if (!visibleAt(parent, r)) continue; // the same rule as for elements: hidden, decoration, clipped away
      const numbered = closestDeep(parent, "[data-krify-id]");
      const control = numbered || closestDeep(parent, "a[href], button, [role=button]");
      const said = control && (control.innerText || "").length <= (numbered ? LONG_LABEL : SHORT_SAID);
      // A link or chip that flows inside a sentence ("…through the LinkedIn website or app") keeps
      // its words in that sentence's block, so the sentence isn't left with holes.
      const inline = said && /^inline/.test(cachedStyle(control).display);
      const block = inline ? blockOf(parentOf(control) || control) : blockOf(parent);
      let entry = blocks.get(block);
      if (!entry) blocks.set(block, (entry = { parts: [], ownParts: [], own: 0, top: r.top, left: r.left, ownTop: null, ownLeft: null }));
      entry.parts.push(text);
      if (!said) {
        entry.own += text.length;
        entry.ownParts.push(text);
        if (entry.ownTop === null) [entry.ownTop, entry.ownLeft] = [r.top, r.left];
      }
    }
    const out = [];
    const place = (node, top, left, t, extra = {}) => {
      let c = -1;
      for (let p = node, i = 0; p && i < 15; p = parentOf(p), i++) {
        if (cardIds.has(p)) {
          c = cardIds.get(p);
          break;
        }
      }
      const pos = top + 2 < 0 ? -1 : top > innerHeight ? 1 : 0;
      const s = placeOf(chainOf(node), top);
      growBox(s, node.getBoundingClientRect()); // a part with only text still gets its place on the page
      out.push({
        t, s, c, pos,
        y: Math.round(top + scrollY), x: Math.round(left + scrollX), k: readingKey(node), ...extra,
      });
    };
    for (const [block, entry] of blocks) {
      if (!entry.own) continue; // only element labels: nothing new
      const countOnly = entry.own <= 12 && entry.parts.length > entry.ownParts.length;
      if (countOnly) [entry.top, entry.left] = [entry.ownTop, entry.ownLeft];
      const t = (countOnly ? entry.ownParts : entry.parts).join(" ").replace(/\s+/g, " ").trim();
      if (fieldLabels.has(t.replace(/\s*[*:]\s*$/, "").toLowerCase())) continue; // a field's label: on its field line
      const heading = closestDeep(block, "h1, h2, h3, h4, h5, h6, [role=heading]");
      place(block, entry.top, entry.left, t, heading ? { h: 1 } : {});
    }
    // Named images (logos, project shots): their alt text is often the only place a name is written.
    const named = new Set([...listed.values()].map((el) => labelOf(el).trim().toLowerCase())); // already said by an element
    for (const img of deepAll("img[alt], [role=img][aria-label]")) {
      const alt = keep(img.getAttribute("alt") || img.getAttribute("aria-label") || "", 80);
      if (alt.length <= 3 || named.has(alt.toLowerCase()) || closestDeep(img, '[data-krify-id], [aria-hidden="true"]')) continue;
      const r = img.getBoundingClientRect();
      if (r.width < 24 || r.height < 16 || !isRendered(img)) continue; // icons, spacers
      if (!/\s/.test(alt) && /[-_\d]/.test(alt)) continue; // a file name: "slider1", "hero-banner_2", "…-PhotoRoom"
      const plain = alt.replace(/[\s_-]+/g, " ").replace(/\d+$/, "").trim().toLowerCase();
      if (/^(image|img|logo|icon|banner|photo|picture|placeholder|avatar|thumbnail|slider|slide|course image|product image|default|all)$/.test(plain)) continue;
      named.add(alt.toLowerCase());
      place(img, r.top, r.left, `image "${alt}"`);
    }
    return out;
  }

  // A node inside one of several side-by-side boxes that are each at most half as wide as its
  // section (process steps, footer columns, stat boxes) – not a main column next to a sidebar.
  function inNarrowColumn(node, section) {
    const limit = (section || document.body).getBoundingClientRect().width * 0.5;
    for (let box = node.parentElement, i = 0; box && box !== section && box !== document.body && i < 12; box = box.parentElement, i++) {
      if (rowTopOf(box) !== null && box.getBoundingClientRect().width <= limit) return true;
    }
    return false;
  }

  // ---------- what the site tells search engines ----------
  // The page's own one-line summary (meta description) and exact facts from its schema.org data
  // (JSON-LD): organisation contacts and rating, product price/stock/rating, article author/date,
  // breadcrumbs. Written for Google, so short and exact – cheaper and more reliable than reading them
  // off the screen.
  function pageSummary() {
    const meta = document.querySelector('meta[name="description" i], meta[property="og:description" i]');
    return keep(meta?.getAttribute("content") || "", 200);
  }
  const MAX_FACTS = 6;
  function seoFacts(type = "") {
    // A "Product" with a price and rating on a home or company page is an SEO trick (krify.co claims
    // "Product, USD 20, 4.8/575" while its own footer says 4.7/492): only trusted on product/list pages.
    const productPage = /product|search results|checkout/.test(type);
    const facts = [];
    const plain = (v) => (typeof v === "string" ? v : v?.name || v?.["@id"] || "");
    const typeOf = (o) => [].concat(o["@type"] || []).join(" ");
    const rating = (o) => {
      const r = o.aggregateRating;
      if (!r?.ratingValue) return "";
      const count = r.reviewCount || r.ratingCount;
      return `rated ${r.ratingValue}${r.bestRating ? "/" + r.bestRating : ""}${count ? ` (${count} reviews)` : ""}`;
    };
    const visit = (o, depth = 0) => {
      if (!o || typeof o !== "object" || depth > 4 || facts.length >= MAX_FACTS) return;
      if (Array.isArray(o)) return o.forEach((x) => visit(x, depth + 1));
      if (o["@graph"]) visit(o["@graph"], depth + 1);
      const type = typeOf(o);
      const parts = [];
      if (/Product/.test(type) && productPage) {
        const offer = [].concat(o.offers || [])[0] || {};
        const price = offer.price || offer.lowPrice;
        parts.push(`Product "${plain(o.name)}"`, price ? `${offer.priceCurrency || ""} ${price}`.trim() : "",
          (offer.availability || "").replace(/^https?:\/\/schema\.org\//, ""), plain(o.brand) && `brand ${plain(o.brand)}`, rating(o));
      } else if (/Article|BlogPosting|NewsArticle/.test(type)) {
        parts.push(`Article "${plain(o.headline || o.name)}"`, plain([].concat(o.author || [])[0]) && `by ${plain([].concat(o.author)[0])}`,
          o.datePublished && `published ${String(o.datePublished).slice(0, 10)}`);
      } else if (/BreadcrumbList/.test(type)) {
        const names = [].concat(o.itemListElement || []).map((item) => item.name || plain(item.item)).filter(Boolean);
        if (names.length >= 2) parts.push("Breadcrumb: " + names.join(" › ")); // "Home" alone says nothing
      } else if (/Organization|LocalBusiness|Corporation|Store|Restaurant|Hotel/.test(type)) {
        const address = o.address && typeof o.address === "object"
          ? [o.address.streetAddress, o.address.addressLocality, o.address.addressCountry?.name || o.address.addressCountry].filter(Boolean).join(", ")
          : plain(o.address);
        parts.push(`${type.split(" ")[0]} "${plain(o.name)}"`, o.telephone && `phone ${o.telephone}`, o.email && `email ${o.email}`,
          address && `address ${address}`, rating(o), o.foundingDate && `founded ${o.foundingDate}`);
      } else if (/Event/.test(type)) {
        parts.push(`Event "${plain(o.name)}"`, o.startDate && `starts ${o.startDate}`, plain(o.location) && `at ${plain(o.location)}`);
      }
      const fact = parts.filter((p) => p && !/""/.test(p)).join(" · ");
      // (a lone 'Organization "X"' says nothing; a breadcrumb is a fact on its own)
      if (fact && (fact.includes(" · ") || fact.startsWith("Breadcrumb")) && !facts.includes(fact)) facts.push(keep(fact, 160));
      for (const key of ["mainEntity", "itemReviewed", "publisher"]) if (o[key]) visit(o[key], depth + 1);
    };
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        visit(JSON.parse(script.textContent));
      } catch {
        // a broken JSON-LD block: skip it
      }
    }
    return facts.slice(0, MAX_FACTS);
  }

  // ---------- reading order: box by box, not line by line across the page ----------
  // Boxes side by side (columns, stat boxes "20+ years | 1500+ clients", cards in a row) are read
  // one at a time – each top to bottom before the next one to its right – so a number stays next to
  // its own label. readingKey(node) = for every side-by-side box around the node, outermost first,
  // [top of its row, its left edge], then the node's own [top, left]; pageview.py sorts by it.
  const rowTops = new Map(); // element → top of the row of side-by-side boxes it's one of, or null
  function rowTopOf(box) {
    if (rowTops.has(box)) return rowTops.get(box);
    let top = null;
    const parent = box.parentElement;
    if (parent && parent.children.length >= 2 && parent.children.length <= 40) {
      const r = box.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        for (const other of parent.children) {
          if (other === box) continue;
          const q = other.getBoundingClientRect();
          if (q.width <= 0 || q.height <= 0) continue;
          const apart = q.right <= r.left + 2 || q.left >= r.right - 2;
          const overlap = Math.min(r.bottom, q.bottom) - Math.max(r.top, q.top);
          if (apart && overlap > Math.min(r.height, q.height) * 0.5) top = Math.min(top ?? r.top, q.top, r.top);
        }
      }
    }
    rowTops.set(box, top === null ? null : Math.round(top + scrollY));
    return rowTops.get(box);
  }
  function readingKey(node) {
    const key = [];
    for (let box = node, i = 0; box && box !== document.body && i < 15; box = box.parentElement, i++) {
      const top = rowTopOf(box);
      if (top !== null) key.unshift([top, Math.round(box.getBoundingClientRect().left + scrollX)]);
    }
    const r = node.getBoundingClientRect();
    key.push([Math.round(r.top + scrollY), Math.round(r.left + scrollX)]);
    return key;
  }

  // ---------- what a link does ----------
  // Where a link goes, in a few words, when the browser can tell: "call +1 212 380 1160",
  // "email info@krify.co", "WhatsApp chat", "linkedin.com (new tab)", "home page", "/services/",
  // "jumps to "Contact us"", "downloads brochure.pdf". "" when it isn't a real link.
  function destinationOf(el) {
    const a = el.closest?.("a[href]");
    if (!a) return "";
    const raw = (a.getAttribute("href") || "").trim();
    if (!raw || raw === "#" || /^javascript:/i.test(raw)) return "";
    const decode = (s) => {
      try {
        return decodeURIComponent(s);
      } catch {
        return s;
      }
    };
    if (/^tel:/i.test(raw)) {
      // The number as the page writes it ("+1 212 380 1160"), else as the link has it.
      const shown = (a.innerText || "").match(/\+?\d[\d\s().-]{5,}\d/);
      return "call " + (shown ? shown[0] : decode(raw.slice(4))).replace(/\s+/g, " ").trim();
    }
    if (/^mailto:/i.test(raw)) return "email " + decode(raw.slice(7)).split("?")[0];
    let url;
    try {
      url = new URL(raw, location.href);
    } catch {
      return "";
    }
    if (url.protocol === "whatsapp:" || /(^|\.)(wa\.me|whatsapp\.com)$/i.test(url.hostname)) return "WhatsApp chat";
    if (!/^https?:$/.test(url.protocol)) return "";
    const newTab = a.target === "_blank" ? " (new tab)" : "";
    if (a.hasAttribute("download") || /\.(pdf|docx?|xlsx?|pptx?|zip|csv)$/i.test(url.pathname)) {
      return "downloads " + keep(decode(url.pathname.split("/").pop() || "file"), 40);
    }
    const site = (host) => host.replace(/^www\./, "");
    if (site(url.hostname) !== site(location.hostname)) return site(url.hostname) + newTab;
    if (url.pathname === location.pathname && url.hash) {
      const target = document.getElementById(decode(url.hash.slice(1)));
      const name = target ? keep(target.getAttribute("aria-label") || target.querySelector(HEADING_SELECTOR)?.innerText || "", 40) : "";
      return name ? `jumps to "${name}"` : "jumps on this page";
    }
    if (url.pathname === "/") return "home page" + newTab;
    return keep(decode(url.pathname), 50) + newTab;
  }

  // ---------- what a vague button belongs to ----------
  // "Close", "Play Video", "Next", "✕"… only make sense next to what they sit in. ownerOf() names that:
  // 'chat "Talk to Sofi Live"', 'slider', 'testimonial "Priya K"' – from the box's own label or
  // heading, its first line of text, and its kind (from class names). "" for clear labels.
  const VAGUE = /^(close|dismiss|[×✕✖x]|play( video)?|pause|stop|mute|unmute|next|previous|prev|back|forward|more|more (options|actions)|options|edit|delete|remove|view|view (more|all|details)|read more|show more|learn more|see more|details|open|expand|collapse|share|like|copy|download|ok|cancel|\d+|[<>‹›«»←→]|.*\barrow\b.*)$/i;
  const WIDGETS = [ // most telling first: a chat with a video avatar is a chat, a testimonial slider is testimonials
    [/chat|messenger|livechat|intercom|tawk|crisp|drift|zendesk|sofi|talk|assistant|chatbot|\bbot\b|helpdesk|support-widget/i, "chat"],
    [/testimonial|review/i, "testimonial"],
    [/carousel|slider|swiper|slick|splide|glide/i, "slider"],
    [/cookie|consent|gdpr/i, "cookie banner"],
    [/modal|dialog|popup|pop-up|lightbox/i, "pop-up"],
    [/video|player|youtube|vimeo/i, "video"],
  ];
  const MACHINE_NAME = new RegExp(String.raw`\d+(?:[-_]\d+)+\s*$`);
  function ownerOf(el, label, duplicated = false) {
    label = (label || "").trim();
    const bare = label.replace(/\s*\(icon\)\s*$/i, "").trim(); // "next (icon)" is a vague "next"
    if (!duplicated && !VAGUE.test(bare)) return "";
    // A look-alike whose hidden description names its item ("About this result" → the result's title).
    const described = keep(textOfIds(el, "aria-describedby"), 40);
    if (duplicated && described && described.toLowerCase() !== label.toLowerCase()) return `"${described}"`;
    const own = new Set([label, el.innerText || "", el.textContent || ""].map((t) => t.trim().toLowerCase()));
    const sameLabel = (box) => [...box.querySelectorAll("[data-krify-id]")]
      .filter((other) => labelOf(other).trim().toLowerCase() === label.toLowerCase());
    const lookAlike = (box) => sameLabel(box).some((other) => other !== el);
    // A box's other controls aren't its name ("All" is a filter chip next to the "Next" arrow).
    const controlsIn = (box) => new Set([...box.querySelectorAll("[data-krify-id]")].map((x) => labelOf(x).trim().toLowerCase()));
    // The widget kind: the most telling one around it (a chat with a video avatar is a chat).
    let kind = "";
    let rank = WIDGETS.length;
    let name = "";
    let slot = ""; // "2 of 3": which of several look-alike boxes (slides) it's in, when nothing names it
    for (let box = parentOf(el), level = 1; box && box !== document.body && level <= 6; box = parentOf(box), level++) {
      if (box.nodeType !== 1) continue;
      const names = `${box.id} ${typeof box.className === "string" ? box.className : ""}`;
      const found = WIDGETS.findIndex(([re]) => re.test(names));
      if (found >= 0 && found < rank) {
        rank = found;
        kind = WIDGETS[found][1];
      }
      const text = box.innerText || "";
      if (text.length > 2000) break; // a whole page part, not the button's own widget
      if (!slot && box.parentElement && box.parentElement.children.length >= 2) {
        const key = itemKeys(box)[0];
        const same = [...box.parentElement.children].filter((kid) => itemKeys(kid)[0] === key);
        if (same.length >= 2) {
          slot = `${same.indexOf(box) + 1} of ${same.length}`;
          const title = [...box.querySelectorAll("a[href][data-krify-id]")].filter((a) => a !== el)
            .map((a) => labelOf(a).trim()).filter((t) => t.length >= 4 && !own.has(t.toLowerCase()))
            .sort((a, b) => b.length - a.length)[0];
          if (title && !name) name = keep(title, 40);
        }
      }
      // A box that also holds a look-alike (the other "Play Video") is the list, not this item: its
      // name or heading would be the same for both, so naming stops here – with "1 of 2" (which
      // look-alike it is) when nothing smaller told them apart.
      if (lookAlike(box)) {
        if (!name && !slot) {
          const same = sameLabel(box);
          slot = `${same.indexOf(el) + 1} of ${same.length}`;
        }
        break;
      }
      if (!name) {
        const controls = controlsIn(box);
        const busy = controls.size >= 3;
        const usable = (t) => t && !own.has(t.toLowerCase()) && !(busy && controls.has(t.toLowerCase())) &&
          !(label.length >= 3 && t.toLowerCase().includes(label.toLowerCase())) &&
          !MACHINE_NAME.test(t); // not "repository body 0-0"
        // The box's own label or (visible) heading, even for a big box; its first line of text only
        // for a small one.
        name = [box.getAttribute("aria-label"), textOfIds(box, "aria-labelledby"), firstTextLine(visibleHeading(box)?.innerText)]
          .map((t) => (t || "").trim()).find(usable) || "";
        if (!name && level >= 2 && text.length <= 300) {
          // The box's title link (a repo / product name) before its loose text lines.
          const links = [...box.querySelectorAll("a[href][data-krify-id]")].filter((a) => a !== el)
            .map((a) => labelOf(a).trim()).filter((t) => t.length >= 4 && !own.has(t.toLowerCase()) && !t.startsWith("@"));
          name = links.sort((a, b) => b.length - a.length)[0] ||
            text.split(String.fromCharCode(10)).map((line) => line.trim()).find(usable) || "";
        }
        name = keep(name, 40);
      }
    }
    if (name) return kind ? `${kind} "${name}"` : `"${name}"`;
    if (slot && (kind || duplicated)) return `${kind || "item"} ${slot}`;
    return kind;
  }


  // ---------- inspect(): accessibility-tree view ----------

  // An element's own text, not counting text that belongs to its children (so a container's
  // line doesn't just repeat what its children already show).
  function ownText(el) {
    let text = "";
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) text += node.nodeValue;
    }
    return keep(text, 80);
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
      const alt = keep(el.getAttribute("alt") || el.getAttribute("aria-label") || "", 60);
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

    // Forget the element numbers (before a new list is made across the page and its embedded pages).
    forget() {
      for (const el of listed.values()) el.removeAttribute("data-krify-id");
      listed.clear();
      return "ok";
    },

    // Numbers start after `start`: embedded pages continue the main page's numbering.
    // Returns { text, count } – the side panel joins the main page and embedded pages.
    getElements(start = 0, max = MAX_ELEMENTS) {
      const { numbered, hasPopup, lastId } = numberElements(start, max);
      const lines = [];
      const status = statusSignals();
      if (status) lines.push(`[Status] ${status}`);
      if (hasPopup) lines.push("(an open menu/dialog is listed first)");
      // An open dropdown list's options are numbered last but listed first: they're the next pick.
      const first = numbered.filter((n) => openListOf(n.el));
      for (const { id, text } of [...first, ...numbered.filter((n) => !first.includes(n))]) lines.push(`[${id}] ${text}`);
      if (!window.__krify.unrestricted && numbered.length >= max) {
        lines.push("(more elements not shown – use find_elements(\"text\") to search for one)");
      }
      let id = lastId;
      // Panels that scroll on their own (chat lists, popups): numbered so the AI can scroll them.
      for (const box of scrollBoxes()) {
        if (!window.__krify.unrestricted && id - start >= max) break;
        id += 1;
        box.setAttribute("data-krify-id", String(id));
        listed.set(String(id), box);
        lines.push(`[${id}] scrollable box "${boxName(box)}" (scroll with element=${id} to see more)`);
      }
      const below = document.documentElement.scrollHeight - (scrollY + innerHeight);
      if (window === top) {
        if (below > 50) lines.push("(more content below – scroll down to see it)");
      } else if (document.documentElement.scrollHeight > innerHeight + 4) {
        // An embedded page that scrolls gets its own number, even when none of its buttons are in view.
        id += 1;
        const page = document.documentElement;
        page.setAttribute("data-krify-id", String(id));
        listed.set(String(id), page);
        const at = below > 50 ? (scrollY > 4 ? "more above and below" : "more below") : "at the bottom";
        lines.push(`[${id}] this embedded page (${at} – scroll with element=${id})`);
      }
      return { text: lines.join("\n"), count: id - start };
    },

    // The whole page as structured data (sections → cards → elements, with accessibility states):
    // see buildPageModel(). The server (pageview.py) decides how much of it the AI sees.
    getPageModel(start = 0, max = MODEL_MAX) {
      return buildPageModel(start, max);
    },

    // ---------- embedded pages (iframes) ----------

    // Is this embedded page worth looking at? Tiny/hidden ones are trackers and ads.
    frameInfo() {
      const usable = innerWidth >= 50 && innerHeight >= 30 && document.body?.childElementCount > 0;
      return { usable, label: clean(document.title || location.hostname || "embedded page", 40) };
    },

    // Where this embedded page sits on the main page, so real mouse clicks land in the right spot.
    // Only possible when each page in between is from the same site.
    frameOffset() {
      let x = 0;
      let y = 0;
      for (let w = window; w !== top; w = w.parent) {
        let box = null;
        try {
          box = w.frameElement;
        } catch {
          // another site's page: can't look at it
        }
        if (!box) {
          throw new Error("That element is in an embedded page from another site – real mouse actions can't reach it yet. Use a normal click or type.");
        }
        const r = box.getBoundingClientRect();
        x += r.left + box.clientLeft;
        y += r.top + box.clientTop;
      }
      return { x, y };
    },

    // Search ALL numbered elements (not just the shown window above) by label text – for pages
    // with more elements than getElements() describes by default. Tolerates common UI synonyms
    // (see matchesQuery/ALIASES). Any id it returns is already clickable/typeable immediately.
    findElements(query) {
      const q = clean(query || "", 200);
      if (!q) return "(empty query)";
      // The latest page view's numbers stay as they are; elements it left out get new numbers.
      extendNumbering();
      const numbered = [...listed.entries()]
        .filter(([, el]) => el.isConnected)
        .map(([id, el]) => ({ id, text: describe(el) }));
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
      // Every smallest element whose own text contains the full query – descending into a child
      // that also contains it in full, so a match split across siblings resolves to their common
      // ancestor instead of the whole page.
      const matches = [];
      function collect(el) {
        if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName)) return false;
        if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
        if (!norm(el.innerText).includes(q)) return false;
        let inChild = false;
        for (const child of [...el.children, ...(el.shadowRoot?.children || [])]) inChild = collect(child) || inChild;
        if (!inChild) matches.push(el);
        return true;
      }
      collect(document.body);
      if (!matches.length) return `(no text found matching "${query}")`;
      // Asking again for the same text goes to the next match (a long page can have many).
      const n = this.lastFind?.q === q && this.lastFind.count === matches.length ? (this.lastFind.n + 1) % matches.length : 0;
      this.lastFind = { q, n, count: matches.length };
      await bringIntoView(matches[n]);
      const which = matches.length > 1 ? `[Match ${n + 1} of ${matches.length} – find_text again for the next one]\n` : "";
      return which + this.readText("screen");
    },

    // Structure around the page or one element – headings, regions, nearby text – for when the
    // flat element list above doesn't make clear what goes with what (e.g. several "Add to cart"
    // buttons with no indication which belongs to which product).
    getAccessibilityTree({ ref, filter = "interactive", depth = 8 } = {}) {
      extendNumbering(); // keep the latest page view's numbers; number what it left out
      const root = ref ? containerOf(find(ref)) : document.body;
      const { lines, truncated } = buildTree(root, filter, Math.max(1, Number(depth) || 8));
      if (truncated) {
        return "Too much structure to show from here. Try a smaller depth, or ref=<element number> to zoom into one part.";
      }
      return lines.join("\n") || "(nothing to show here)";
    },

    // "screen" (default): only the text visible right now, line by line – prices, answers, etc.
    // "page": the whole page from the top, all of it (can be long, so cut at `max`/MAX_TEXT – or
    // uncapped in unrestricted mode). Both walk the text nodes ourselves (not body.innerText) so
    // text inside shadow roots is read too. `max`: operations.js caps how much text comes back per
    // embedded page (FRAME_TEXT), so one huge iframe can't crowd out everything else.
    readText(scope = "screen", max = null) {
      const wholePage = scope === "page";
      const limit = max || (this.unrestricted ? Infinity : (wholePage ? MAX_TEXT : MAX_SCREEN_TEXT));
      // "page" on a long page: from a little above the current screen down, not from the top every
      // time (that's mostly the header and menu, and the same text however far you've scrolled).
      const doc = document.scrollingElement || document.documentElement;
      const fromHere = wholePage && limit !== Infinity && scrollY > innerHeight / 2;
      const from = fromHere ? -innerHeight / 4 : -Infinity;
      const lines = [];
      let line = "";
      let lineTop = null;
      let size = 0;
      const range = document.createRange();
      for (const node of textNodes(document.body)) {
        const text = node.nodeValue.replace(/\s+/g, " ").trim();
        const parent = node.parentElement;
        if (!text || !parent || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
        range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) continue; // not laid out
        if (!wholePage && (r.bottom < 0 || r.top > innerHeight)) continue; // off screen
        if (r.bottom < from) continue; // above where this read starts
        if (parent.checkVisibility && !parent.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
        // Text at about the same height belongs to the same visual line.
        if (lineTop !== null && Math.abs(r.top - lineTop) > 6) {
          lines.push(line);
          line = "";
        }
        line = line ? `${line} ${text}` : text;
        lineTop = r.top;
        size += text.length + 1;
        if (size > limit) {
          line += wholePage ? " …(text cut, page is long – scroll down and read again for the rest)" : " …(more on screen not shown)";
          break;
        }
      }
      if (line) lines.push(line);
      if (fromHere && lines.length) {
        const screens = Math.max(1, Math.ceil(doc.scrollHeight / innerHeight));
        const at = Math.min(screens, Math.floor(scrollY / innerHeight) + 1);
        lines.unshift(`[Page text from the current screen down (screen ${at} of about ${screens}). Text higher up isn't ` +
          "included – use find_text for it, or scroll up and read again.]");
      }
      if (wholePage && lines.length) {
        const counts = listCounts();
        if (counts.length) lines.push(`[Exact counts of repeated rows loaded on this page so far: ${counts.join("; ")}. ` +
          "Use these, don't count by eye. Rows on other pages, behind 'Load more', or not loaded yet (lists that " +
          "fill in as you scroll) are not included.]");
      }
      return lines.join("\n") || (wholePage ? "(no text on this page)" : "(no text visible on screen – try scrolling)");
    },

    // Google Sheets: name of the open sheet tab (the data itself is fetched by sheets.js).
    sheetTabName() {
      return document.querySelector(".docs-sheet-active-tab .docs-sheet-tab-name")?.textContent || "";
    },

    // ---------- actions ----------
    // When `animate` is on, a visible Krify cursor moves, clicks and types like a person.

    animate: false,

    // When `unrestricted` is on, the caps below (MAX_ELEMENTS/MAX_TEXT/MAX_SCREEN_TEXT/
    // MAX_FIND_CHARS/MAX_TREE_CHARS) are skipped – full page data instead of the token-saving
    // defaults. Set by operations.js on every call, from a side-panel toggle (sidepanel.js).
    unrestricted: false,

    // Glowing border around the page while a task runs.
    setWorking(on) {
      if (window !== top) return "ok"; // only around the main page, not each embedded page
      if (!on && !view.root) return "ok"; // nothing shown, nothing to hide
      overlay().querySelector(".working").classList.toggle("on", on);
      return "ok";
    },

    async click(id) {
      const el = find(id);
      const blocked = await clickElement(el, id);
      if (blocked) {
        return `Clicked [${id}] ${describe(el)} – but the browser did NOT send the form: ${blocked}. ` +
          "Fill those fields, then submit again.";
      }
      return `Clicked [${id}] ${describe(el)}`;
    },

    // Leaving the form being filled? For the server's unsaved-form guard: is [id] outside the form
    // (and outside any open list/popup), and would clicking it go somewhere else (a link, or
    // anything in the site's navigation)? → { form, inside, navigates }.
    leaveCheck(id) {
      const el = find(id);
      if (!lastFormRoot?.isConnected) return { form: false, inside: false, navigates: false };
      const inside = containsDeep(lastFormRoot, el) || !!openListOf(el) || !!closestDeep(el, POPUP);
      const link = closestDeep(el, "a[href]");
      const href = (link?.getAttribute("href") || "").trim();
      const navigates = (!!link && !/^(#|javascript:)/i.test(href)) || el.getAttribute("role") === "link" || isClickableRow(el) ||
        !!closestDeep(el, "nav, [role=navigation], aside, [role=complementary], header, [role=banner]");
      return { form: true, inside, navigates };
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
        const pressed = await this.pressKey("Enter"); // search in one step instead of two
        return `Typed "${text}" into [${id}]; ${pressed}`;
      }
      return `Typed "${text}" into [${id}]`;
    },

    // ---------- helpers for real mouse clicks (realinput.js) ----------

    // Scroll an element into view, show the cursor there, and return the spot to click.
    async pointAt(id) {
      const el = find(id);
      await bringIntoView(clickTarget(el));
      const reach = await waitActionable(el, id);
      const r = el.getBoundingClientRect();
      // The Sheets Name box: near its left end (its middle can be under the dropdown arrow).
      const spot = el.matches(NAME_BOX) ? { x: r.left + Math.min(r.width / 2, 60), y: r.top + r.height / 2 } : reach;
      if (this.animate) {
        await moveCursorToPoint(spot.x, spot.y); // the spot the real click will use, not just the centre
        glow(clickTarget(el));
        ripple(spot.x, spot.y);
      }
      return { ...spot, nameBox: el.matches(NAME_BOX) };
    },

    // Before clicking: if [id] is a "(shows on hover)" button that is still hidden, scroll to it and
    // return the spot to put the real mouse on (that hovers its row, so the site shows it). Else null.
    async hoverSpot(id) {
      const el = find(id);
      if (visibleEnough(el) || !hiddenUntilHover(el)) return null;
      await bringIntoView(el);
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    },

    // An element's spot without scrolling (a drag target must already be on screen).
    async spotOf(id) {
      const el = find(id);
      if (!visibleEnough(el) || !isVisible(clickTarget(el))) throw new Error(`[${id}] is not on screen – scroll so both ends of the drag are visible.`);
      return waitActionable(el, id);
    },

    // Live description of a numbered element right now, or "" if it's gone – lets the backend check
    // an id still means what it meant when the model looked, before acting on it.
    // Wait until the page stops changing: no elements added/removed or text changed for `quietMs`
    // (at most `maxMs`) – content loaded by XHR after a click or scroll, a list filling in. Attribute
    // changes don't count: carousels and animations change styles all the time.
    settle(quietMs = 250, maxMs = 2500) {
      return new Promise((resolve) => {
        let quiet = null;
        const done = () => {
          observer.disconnect();
          clearTimeout(quiet);
          clearTimeout(cap);
          resolve("ok");
        };
        const observer = new MutationObserver(() => {
          clearTimeout(quiet);
          quiet = setTimeout(done, quietMs);
        });
        observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
        quiet = setTimeout(done, quietMs);
        const cap = setTimeout(done, maxMs);
      });
    },

    // For the Enter safety check: what Enter would send – the focused (or given) field, its form's
    // default button, and whether THAT form asks for a password (not just any form on the page).
    enterTarget(id) {
      const el = id != null ? find(id) : activeDeep();
      const form = el && el !== document.body ? el.form || null : null;
      const button = form && defaultButtonOf(form);
      return {
        field: el && el !== document.body ? describe(el) : "",
        submitter: button ? describe(button) : "",
        password: !!form?.querySelector("input[type=password]"),
        inForm: !!form,
      };
    },

    describeId(id) {
      const el = listed.get(String(id)) || document.querySelector(`[data-krify-id="${id}"]`); // listed: also inside components
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
      const active = activeDeep();
      if (active !== el && !el.contains(active)) return false;
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
      if (!parts.length) throw new Error("Can't see the selected cell on screen – scroll it into view with go_to_cell.");
      const left = Math.min(...parts.map((r) => r.left));
      const right = Math.max(...parts.map((r) => r.right));
      const top = Math.min(...parts.map((r) => r.top));
      const bottom = Math.max(...parts.map((r) => r.bottom));
      if (right - left < 4 || bottom - top < 4) {
        throw new Error("Can't see the selected cell on screen – scroll it into view with go_to_cell.");
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
      const target = activeDeep() || document.body;
      if (this.animate) {
        showKey(key);
        await sleep(400);
      }
      // keyCode/which too: older handlers check e.keyCode === 13, not e.key.
      const codes = { Enter: 13, Escape: 27, Tab: 9, Backspace: 8, Delete: 46, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, " ": 32 };
      const keyCode = codes[key] ?? (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
      const opts = { key, code: key, keyCode, which: keyCode, bubbles: true, cancelable: true, composed: true };
      const taken = !target.dispatchEvent(new KeyboardEvent("keydown", opts)); // the page's own handler cancelled it
      if (key === "Enter" || key.length === 1) target.dispatchEvent(new KeyboardEvent("keypress", { ...opts, charCode: keyCode }));
      target.dispatchEvent(new KeyboardEvent("keyup", opts));
      if (key !== "Enter") return `Pressed ${key}`;
      if (taken) return "Pressed Enter (the page handled it)";
      return enterDefault(target);
    },

    async scroll(direction) {
      return scrollIn(document.scrollingElement || document.documentElement, direction, window === top ? "the page" : "the embedded page");
    },

    // Scroll the box an element sits in: a scrollable panel (chat list, popup), or else this
    // page itself – which, inside an embedded page, scrolls just that embedded page.
    async scrollAt(id, direction) {
      const el = find(id);
      let box = null;
      for (let p = el; p && p !== document.body && p !== document.documentElement; p = parentOf(p)) {
        if (p.nodeType === 1 && canScroll(p)) { box = p; break; }
      }
      return box ? scrollIn(box, direction, `the box [${id}]`) : this.scroll(direction);
    },

    async selectOption(id, option) {
      const el = find(id);
      option = String(option ?? "");
      if (el.tagName !== "SELECT") return pickInCustomDropdown(el, id, option);
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

  // Scroll a box (or the page's own scroller) up/down by most of its height, or straight to its
  // top/bottom, and say where it is now – so the AI knows how much is left.
  async function scrollIn(box, direction, name) {
    const before = box.scrollTop;
    const behavior = window.__krify.animate ? "smooth" : "instant";
    if (direction === "top") box.scrollTo({ top: 0, behavior });
    else if (direction === "bottom") box.scrollTo({ top: box.scrollHeight, behavior });
    else box.scrollBy({ top: (direction === "up" ? -1 : 1) * box.clientHeight * 0.8, behavior });
    await sleep(window.__krify.animate ? 600 : 50);
    const room = box.scrollHeight - box.clientHeight;
    const where = room <= 0 ? "" : box.scrollTop >= room - 4 ? "at the bottom" : box.scrollTop <= 4 ? "at the top"
      : `${Math.round((box.scrollTop / room) * 100)}% down, more below`;
    if (box.scrollTop === before) return `Can't scroll ${name} ${direction} any more${where ? ` (${where})` : ""}`;
    return `Scrolled ${name} ${direction}${where ? ` – now ${where}` : ""}`;
  }

  function find(id) {
    const el = listed.get(String(id));
    if (!el?.isConnected) throw new Error(`Element [${id}] not found. The page may have changed – get the elements again.`);
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
  // rect, or null if it never stopped moving in time. 2px tolerance: sub-pixel jitter isn't moving.
  async function waitStable(el, timeoutMs) {
    let lastRect = null, stableSince = 0;
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const r = el.getBoundingClientRect();
      const moved = !lastRect || Math.abs(r.top - lastRect.top) > 2 || Math.abs(r.left - lastRect.left) > 2
        || Math.abs(r.width - lastRect.width) > 2 || Math.abs(r.height - lastRect.height) > 2;
      lastRect = r;
      if (moved) stableSince = Date.now();
      else if (Date.now() - stableSince > 100) return r;
      await sleep(80);
    }
    return null;
  }

  // What's really at a spot – looking inside components, where the page only reports the host.
  function hitAt(x, y) {
    let hit = document.elementFromPoint(x, y); // Krify's own overlay ignores the mouse, so it's skipped
    for (let shadow = hit && shadowOf(hit); shadow; shadow = shadowOf(hit)) {
      const inner = shadow.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  }

  // A mouse-like click on an element: scroll to it, wait until it can be clicked, then the full
  // pointer/mouse event sequence. `id` names it in error messages. → the browser's own form
  // validation problems if this click submits a form that won't send ("" otherwise).
  async function clickElement(el, id) {
    const shown = clickTarget(el); // a hidden checkbox is clicked through its label, like a person does
    await bringIntoView(shown);
    const spot = await waitActionable(el, id);
    if (window.__krify.animate) {
      await moveCursorTo(shown);
      glow(shown);
      ripple(spot.x, spot.y);
      await sleep(150);
    }
    // Make the click look like a real mouse: many apps (React Aria, Radix, Asana…) ignore clicks
    // at (0, 0) or with no pointerType / pressed button. isTrusted can't be faked, though.
    const { x, y } = spot;
    // A real click lands on whatever is on top at that spot (often a child, e.g. the text span).
    const top = hitAt(x, y);
    const target = top && containsDeep(shown, top) ? top : shown;
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, screenX: x + window.screenX, screenY: y + window.screenY, button: 0 };
    const ptr = { pointerId: 1, pointerType: "mouse", isPrimary: true, width: 1, height: 1 };
    const fire = (Type, type, extra = {}) => target.dispatchEvent(new Type(type, { ...base, ...extra }));
    fire(PointerEvent, "pointerover", { ...ptr, buttons: 0 });
    fire(PointerEvent, "pointerenter", { ...ptr, buttons: 0, bubbles: false });
    fire(MouseEvent, "mouseover", { buttons: 0 });
    fire(MouseEvent, "mouseenter", { buttons: 0, bubbles: false });
    fire(PointerEvent, "pointermove", { ...ptr, buttons: 0 });
    fire(MouseEvent, "mousemove", { buttons: 0 });
    fire(PointerEvent, "pointerdown", { ...ptr, buttons: 1, pressure: 0.5 });
    const notCancelled = fire(MouseEvent, "mousedown", { buttons: 1, detail: 1 });
    if (notCancelled) el.focus?.({ preventScroll: true }); // real browsers focus on mousedown unless prevented
    fire(PointerEvent, "pointerup", { ...ptr, buttons: 0, pressure: 0 });
    fire(MouseEvent, "mouseup", { buttons: 0, detail: 1 });
    // A dispatched "click" still follows links and ticks checkboxes, like el.click() did.
    const blocked = validationProblems(el); // checked first: the browser's own checks run on this click
    inThisTab(el, () => fire(PointerEvent, "click", { ...ptr, buttons: 0, detail: 1 }));
    return blocked;
  }

  // ---------- choosing an option in any dropdown (select_option) ----------
  // Custom dropdowns differ per site: a role=listbox (Radix, Headless UI, MUI), a plain <div> list
  // (React + Tailwind), a list rendered at the end of <body> (a "portal"), with or without a search
  // box, options that load as you scroll or type, or an <input> you type into to filter. Instead of
  // the AI clicking the field and then guessing option numbers, this does it in one go: open the
  // list, find the option (searching / scrolling if needed), click it, and check the field shows it.
  const OPTION_ROLES = "[role=option], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], [role=treeitem]";
  const SEARCHABLE = /^(text|search|email|tel|url|)$/; // a box you type into to filter a list
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const optionText = (el) => norm(el.innerText || el.textContent || labelOf(el));
  const firstLine = (el) => ((el.innerText || el.textContent || labelOf(el) || "").split("\n").map((s) => s.trim()).find(Boolean) || "").trim();

  // Everything clickable that's rendered right now – to see what opening a dropdown added.
  function renderedClickables() {
    const roots = allRoots();
    const all = new Set([...roots.flatMap((r) => [...r.querySelectorAll(`${SELECTOR}, ${OPTION_ROLES}`)]), ...pointerTargets(roots)]);
    return new Set([...all].filter((el) => isRendered(el) && !el.closest?.("krify-overlay")));
  }

  // The list a field already has open: from the latest numbering's open lists, or the popup its
  // aria-controls / aria-owns points at (also on a combobox input inside the field).
  function openListFor(field) {
    for (const [list, trigger] of openLists) {
      if (list.isConnected && isRendered(list) && (trigger === field || containsDeep(trigger, field) || containsDeep(field, trigger))) return list;
    }
    const owners = [field, ...field.querySelectorAll("[aria-controls], [aria-owns]")];
    for (const owner of owners) {
      const ids = `${owner.getAttribute("aria-controls") || ""} ${owner.getAttribute("aria-owns") || ""}`.trim().split(/\s+/).filter(Boolean);
      for (const id of ids) {
        const box = owner.getRootNode().getElementById?.(id) || document.getElementById(id);
        if (box && isRendered(box) && box.querySelector(`${OPTION_ROLES}, li, div`)) return box;
      }
    }
    return null;
  }

  // The options showing for `field`: those inside its open list if known, else what appeared since
  // `before` (role=option ones first), never the field itself. Plus the list's search box, if any.
  // `before` null: the list was open before Krify looked, so nothing tells which new things are its
  // options – then only real option-role elements in an open popup count (never the whole page).
  function shownOptions(field, before) {
    const list = openListFor(field);
    const now = renderedClickables();
    let fresh = list ? [...now].filter((el) => containsDeep(list, el))
      : before ? [...now].filter((el) => !before.has(el))
        : [...now].filter((el) => el.matches(OPTION_ROLES) && closestDeep(el, POPUP));
    fresh = fresh.filter((el) => el !== field && !containsDeep(field, el) && !containsDeep(el, field));
    const search = fresh.find((el) => el.tagName === "INPUT" && SEARCHABLE.test(el.type) && !el.disabled) ||
      (list && [...list.querySelectorAll("input")].find((el) => SEARCHABLE.test(el.type) && isRendered(el))) || null;
    let options = fresh.filter((el) => !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    const roled = options.filter((el) => el.matches(OPTION_ROLES));
    if (roled.length) options = roled;
    // An option and a button inside it are one choice: keep the outer one.
    options = options.filter((el) => !options.some((o) => o !== el && containsDeep(o, el)));
    return { options: options.filter((el) => !isDisabled(el)), search, list };
  }

  // The option that best matches `want`: same text, then same first line, then starts with it,
  // then contains it (shortest first), then has all its words. Never a "Select a …" prompt row
  // unless that's what was asked for.
  function bestOption(options, want) {
    const w = norm(want);
    if (!w) return null;
    const real = options.filter((el) => !PROMPT_TEXT.test(optionText(el)) || PROMPT_TEXT.test(w));
    const byLength = [...real].sort((a, b) => optionText(a).length - optionText(b).length);
    const words = w.split(" ");
    return real.find((el) => optionText(el) === w) ||
      real.find((el) => norm(firstLine(el)) === w || norm(labelOf(el)) === w) ||
      byLength.find((el) => optionText(el).startsWith(w)) ||
      byLength.find((el) => optionText(el).includes(w)) ||
      byLength.find((el) => words.every((x) => optionText(el).includes(x))) || null;
  }

  // Type into a box the way a person would, so React/Vue notice (native value setter + input event).
  function setBoxValue(box, value) {
    box.focus();
    const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(box, value);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // Does the field (or its close wrapper, which often shows the chosen value next to a combobox
  // input) now show `text`?
  function fieldShows(field, text) {
    const want = norm(text);
    if (!want) return false;
    if (/^(INPUT|TEXTAREA)$/.test(field.tagName) && norm(field.value).includes(want)) return true;
    let node = field;
    for (let i = 0; node && node !== document.body && i < 3; i++, node = node.parentElement) {
      if (openLists.has(node) || node.querySelector?.(OPTION_ROLES)) break; // don't read the open list's own text
      if (norm(node.innerText).includes(want)) return true;
    }
    return false;
  }

  // Wait (up to `ms`) until `test()` gives something truthy; → that value, or the last one.
  async function waitFor(test, ms) {
    const end = Date.now() + ms;
    let value = test();
    while (!value && Date.now() < end) {
      await sleep(120);
      value = test();
    }
    return value;
  }

  // A React dropdown's options, from its component's props (pagehook.js, in the page's own world,
  // answers synchronously during dispatchEvent) → { names, total }, or null (not React / no hook).
  function reactOptions(el) {
    let reply = null;
    const take = (event) => { reply = event.detail; };
    el.addEventListener("krify:options-result", take);
    try {
      el.dispatchEvent(new CustomEvent("krify:options", { bubbles: true, composed: true }));
    } catch {
      // ignore
    } finally {
      el.removeEventListener("krify:options-result", take);
    }
    try {
      const found = typeof reply === "string" ? JSON.parse(reply) : null;
      return Array.isArray(found?.names) && found.names.length ? found : null;
    } catch {
      return null;
    }
  }

  // A custom dropdown's options, as select_option last saw them (they're only in the page while its
  // list is open), so later page views can show them: dropdown "My Activities" (options: …).
  const seenOptions = new WeakMap();
  function rememberOptions(field, els, onlyIfMore = false) {
    const names = [...new Set(els.map(firstLine).filter((t) => t && !PROMPT_TEXT.test(t)))].slice(0, upTo(12));
    if (names.length > (onlyIfMore ? seenOptions.get(field)?.length || 0 : 0)) seenOptions.set(field, names);
  }

  async function pickInCustomDropdown(field, id, option) {
    const name = fieldInfo(field).label || labelOf(field) || "the field";
    if (openListOf(field)) {
      throw new Error(`[${id}] is an OPTION inside an open list, not a dropdown field. Give select_option the ` +
        "dropdown field's own number (the box that shows the current choice) and the option's text.");
    }
    if (/^(INPUT)$/.test(field.tagName) && /date|time|month|week/.test(field.type)) {
      throw new Error(`[${id}] is a date/time box, not a dropdown: use type with the value (e.g. 2025-12-31).`);
    }
    if (field.tagName === "INPUT" && /checkbox|radio/.test(field.type)) {
      throw new Error(`[${id}] is a checkbox/radio button, not a dropdown: click it.`);
    }
    // Open it – unless its list is already open (clicking again would close it).
    let before = null;
    let found = openListFor(field) || field.getAttribute("aria-expanded") === "true" ? shownOptions(field, before) : null;
    if (!found?.options.length) {
      before = renderedClickables();
      const blocked = await clickElement(field, id);
      if (blocked) throw new Error(`Couldn't open [${id}]: ${blocked}`);
      // A box you type into (a combobox) often opens nothing until you type: don't wait long for it.
      const typeable = field.tagName === "INPUT" && SEARCHABLE.test(field.type);
      found = await waitFor(() => {
        const f = shownOptions(field, before);
        return f.options.length || f.search ? f : null;
      }, typeable ? 700 : 2500) || shownOptions(field, before);
    }
    const firstSeen = found.options; // what the list showed before any search (for the "not found" reply)
    rememberOptions(field, firstSeen);
    // Nothing opened and the field itself is a text box (a combobox you type into): type to filter.
    let typedInto = null;
    if (!found.options.length && !found.search && field.tagName === "INPUT" && SEARCHABLE.test(field.type)) {
      found = { ...found, search: field };
    }
    let match = bestOption(found.options, option);
    // Not listed (yet): search for it, if the list has a search box. Search results often load from
    // the server, so wait for the list to change.
    if (!match && found.search) {
      typedInto = found.search;
      setBoxValue(found.search, option);
      await window.__krify.settle(300, 2500);
      match = await waitFor(() => bestOption(shownOptions(field, before).options, option), 2500);
      if (!match) {
        // A shorter search (first word) catches "Amit Verma (Acme)" when asked for "Amit Verma Acme".
        const word = option.trim().split(/\s+/)[0];
        if (word && word.length >= 2 && word.toLowerCase() !== option.trim().toLowerCase()) {
          setBoxValue(found.search, word);
          await window.__krify.settle(300, 2500);
          match = await waitFor(() => bestOption(shownOptions(field, before).options, option), 1500);
        }
      }
      found = shownOptions(field, before);
    }
    // Still not there: long lists load more rows as you scroll them.
    if (!match && found.options.length) {
      let box = null;
      for (let p = found.options[found.options.length - 1].parentElement; p && p !== document.body; p = p.parentElement) {
        if (canScroll(p)) { box = p; break; }
      }
      for (let i = 0; box && !match && i < 5; i++) {
        const top = box.scrollTop;
        box.scrollTop = box.scrollHeight;
        await window.__krify.settle(250, 1500);
        match = bestOption(shownOptions(field, before).options, option);
        if (box.scrollTop === top) break; // at the end
      }
      found = shownOptions(field, before);
      if (!typedInto) rememberOptions(field, found.options, true); // more rows loaded by scrolling
    }
    if (!match) {
      // Clear Krify's search text, so the list (and the next page view) shows every option again.
      if (typedInto && typedInto !== field) {
        setBoxValue(typedInto, "");
        await window.__krify.settle(250, 1500);
      }
      // The real options (not "Select a …" prompts): as the list shows them now, else as it first did.
      const names = (els) => [...new Set(els.map(firstLine).filter((t) => t && !PROMPT_TEXT.test(t)))];
      let seen = names(typedInto ? shownOptions(field, before).options : found.options);
      if (!seen.length) seen = names(firstSeen);
      const list = seen.slice(0, upTo(15)).map((t) => `"${clean(t, 40)}"`).join(", ");
      const more = seen.length > upTo(15) ? ` (+${seen.length - upTo(15)} more)` : "";
      const searched = typedInto ? ` (searched for "${option}" in its search box)` : "";
      if (!seen.length) {
        throw new Error(`No option "${option}" in [${id}] "${name}"${searched}: its list showed no options. ` +
          "It may still be loading, or need an earlier field chosen first – look at the page.");
      }
      throw new Error(`No option "${option}" in [${id}] "${name}"${searched}. Its options: ${list}${more}. ` +
        "Call select_option again with one of these (for a dummy/test value any real one is fine), or ask_user.");
    }
    const chosen = firstLine(match) || optionText(match);
    try {
      await clickElement(match, id);
    } catch (err) {
      throw new Error(`Found option "${clean(chosen, 40)}" in [${id}] but couldn't click it: ${err.message.replace(/^\[[^\]]*\] /, "")}`);
    }
    await window.__krify.settle(200, 1500);
    const ok = await waitFor(() => fieldShows(field, chosen) || match.getAttribute("aria-selected") === "true", 1200);
    if (ok) return `Selected "${clean(chosen, 60)}" in [${id}] "${name}" – the field now shows it.`;
    const now = filledState(field).value;
    return `Clicked option "${clean(chosen, 60)}" in [${id}]'s list, but the field doesn't show it yet` +
      `${now ? ` (it shows "${now}")` : ""}. Look at the page: if it isn't chosen, try select_option once more.`;
  }

  // a contains b, across shadow-root edges (Node.contains stops at them).
  function containsDeep(a, b) {
    for (let n = b; n; n = n.parentNode || n.host) if (n === a) return true;
    return false;
  }

  // Would a click at this spot reach the element? What's on top must be it, inside it, its own
  // label / see-through wrapper, or a close wrapper of it – never just the page body.
  function reaches(target, el, hit) {
    if (!hit) return false;
    if (hit === target || containsDeep(target, hit) || partOfControl(el, hit)) return true;
    return hit !== document.body && hit !== document.documentElement && containsDeep(hit, target);
  }

  // Name what's in the way, so the AI can deal with it ("covered by [3] button "Accept cookies"").
  function coverName(hit) {
    const list = openListOf(hit);
    if (list) {
      const field = openLists.get(list);
      const id = field.getAttribute("data-krify-id");
      return `the open dropdown list of ${id ? `[${id}] ` : ""}${describe(field)} (pick one of its options first)`;
    }
    const listedOne = hit.closest?.("[data-krify-id]");
    if (listedOne) return `[${listedOne.getAttribute("data-krify-id")}] ${describe(listedOne)}`;
    const box = hit.closest?.('[role=dialog], dialog, [aria-modal=true], [class*="cookie" i], [class*="consent" i], [class*="banner" i], [class*="overlay" i], [class*="backdrop" i], [class*="modal" i]') || hit;
    const text = clean(box.innerText || "", 50);
    return `${box.tagName.toLowerCase()}${text ? ` "${text}"` : " (an overlay)"}`;
  }

  // Playwright-style "actionability" wait: before interacting, make sure the element is actually
  // ready – visible, stopped moving, not covered by something else, not disabled, still on the
  // page – instead of firing the action immediately and hoping for the best. Checks the element's
  // stand-in when it hides itself (a hidden <select>'s wrapper, a hidden checkbox's label);
  // `proxy` overrides that. Returns the spot to click: the centre, or another inner point when
  // something (a badge, a sticky bar) covers just the centre.
  async function waitActionable(el, id, { timeoutMs, proxy } = {}) {
    timeoutMs = timeoutMs ?? (window.__krify.animate ? 3000 : 2000);
    const target = proxy || clickTarget(el);
    const start = Date.now();
    let disabledSince = null;
    let why = "hidden";
    while (Date.now() - start < timeoutMs) {
      if (!el.isConnected) throw new Error(`[${id}] was removed from the page while waiting – get the elements again.`);
      // Disabled: give it a moment (it may enable itself after the last input), then say so plainly.
      if (isDisabled(el)) {
        disabledSince ??= Date.now();
        if (Date.now() - disabledSince > 800) {
          throw new Error(`[${id}] is disabled – NOT clicked. Usually a required field is empty or invalid, or ` +
            "another field must be chosen first (e.g. a type list that opens only once a channel is picked): " +
            "check the form's (required)/(invalid) fields and [Status], fill them, then try again.");
        }
      } else if (!visibleEnough(target) && !visibleEnough(el) && hiddenUntilHover(el)) {
        // "(shows on hover)" and the real mouse couldn't reveal it: it can't pass the visible / not
        // covered checks (a visibility:hidden button is never "on top"), but a click still reaches it.
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      } else if (visibleEnough(target) || visibleEnough(el)) {
        const settled = await waitStable(target, Math.min(600, Math.max(200, timeoutMs - (Date.now() - start))));
        const r = settled || target.getBoundingClientRect();
        const spots = [
          // A clickable row: a plain cell first, so the click doesn't land on its "⋯" menu or a badge.
          ...(isClickableRow(el) ? rowSpots(el) : []),
          ...[[0.5, 0.5], [0.25, 0.5], [0.75, 0.5], [0.5, 0.3], [0.5, 0.7]]
            .map(([fx, fy]) => ({ x: r.left + r.width * fx, y: r.top + r.height * fy })),
        ].filter((s) => s.x >= 0 && s.y >= 0 && s.x < innerWidth && s.y < innerHeight);
        let reached = null, cover = null;
        for (const spot of spots) {
          const hit = hitAt(spot.x, spot.y);
          if (reaches(target, el, hit)) {
            reached = spot;
            break;
          }
          cover ??= hit;
        }
        // Still animating (a pulsing "Buy" button never stands still): click anyway after a while.
        if (reached && (settled || Date.now() - start > timeoutMs / 2)) return reached;
        why = reached ? "still moving" : cover ? `covered by ${coverName(cover)}` : "off screen";
      } else {
        why = "hidden";
      }
      await sleep(80);
    }
    const next = {
      hidden: "it may be inside a closed menu, tab or section – open that first",
      "off screen": "scroll to it first",
      "still moving": "wait a moment and try again",
    }[why] || "close or dismiss that first (e.g. its Close/Accept button), or scroll so it isn't in the way";
    throw new Error(`[${id}] can't be clicked yet: ${why} – ${next}.`);
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
