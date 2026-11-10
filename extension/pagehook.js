// Runs in the web page's own JavaScript world (world: "MAIN"), next to content.js.
// content.js runs in Chrome's isolated world and can't see a React component's props. A custom
// dropdown (e.g. a filter like "All Statuses") often holds its options only as a prop and renders
// them only while open, so the page view would show just its current choice.
// content.js fires "krify:options" on a dropdown element; this replies with "krify:options-result"
// (detail: a JSON string {names, total}) if a React select-like component owns that element.
// Nothing is clicked or changed. Injected on demand; safe to inject more than once.

if (!window.__krifyHook) {
  window.__krifyHook = true;

  const MAX_COMPONENTS = 3; // how far up from the element to look (never the whole page)

  const nameOf = (o) => {
    if (typeof o === "string" || typeof o === "number") return String(o);
    if (!o || typeof o !== "object") return null;
    for (const key of ["label", "name", "title", "text"]) {
      if (typeof o[key] === "string" || typeof o[key] === "number") return String(o[key]);
    }
    return null; // e.g. a JSX label
  };

  // [{label, options: [...]}] groups (react-select) → their options.
  const flatten = (list) => list.flatMap((o) => (o && Array.isArray(o.options) ? o.options : [o]));

  function optionsOf(el) {
    const key = Object.keys(el).find((k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$"));
    let fiber = key ? el[key] : null;
    // The node keeps the fiber from its first render; React swaps between two copies on updates.
    // The current copy is the one holding the element's current props (__reactProps$) – its
    // parents then carry the current props too (e.g. options that loaded after the first render).
    const propsKey = Object.keys(el).find((k) => k.startsWith("__reactProps$"));
    if (fiber?.alternate && propsKey && fiber.alternate.memoizedProps === el[propsKey]) fiber = fiber.alternate;
    let components = 0;
    while (fiber && components <= MAX_COMPONENTS) {
      const props = fiber.memoizedProps;
      const list = props && (Array.isArray(props.options) ? props.options : Array.isArray(props.items) ? props.items : null);
      const selectLike = props && (typeof props.onChange === "function" || typeof props.onSelect === "function" ||
        typeof props.onValueChange === "function" || "value" in props);
      if (list && list.length && selectLike) {
        const all = flatten(list);
        const names = all.map(nameOf).filter((n) => n && n.trim());
        if (names.length && names.length >= all.length / 2) return { names: names.slice(0, 200), total: names.length };
      }
      if (typeof fiber.type !== "string") components++; // a component, not a DOM element
      fiber = fiber.return;
    }
    return null;
  }

  document.addEventListener("krify:options", (event) => {
    const el = event.composedPath?.()[0] || event.target;
    let found = null;
    try {
      found = optionsOf(el);
    } catch {
      return;
    }
    if (found) el.dispatchEvent(new CustomEvent("krify:options-result", { detail: JSON.stringify(found) }));
  }, true);
}
