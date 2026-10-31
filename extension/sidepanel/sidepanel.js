import { runOperation } from "./operations.js";

const output = document.getElementById("output");
const elementInput = document.getElementById("elementInput");
const textInput = document.getElementById("textInput");

function show(value, isError = false) {
  output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  output.classList.toggle("error", isError);
}

// Build the operation's inputs from the test boxes and the button's data-* values.
function argsFor(btn) {
  const element = Number(elementInput.value);
  const text = textInput.value;
  switch (btn.dataset.op) {
    case "click": return { element };
    case "type": return { element, text };
    case "select_option": return { element, option: text };
    case "open_url": return { url: text };
    case "press_key": return { key: btn.dataset.key };
    case "scroll": return { direction: btn.dataset.direction };
    case "wait": return { seconds: Number(btn.dataset.seconds) };
    default: return {};
  }
}

document.querySelectorAll("button[data-op]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    show("Working…");
    try {
      show(await runOperation(btn.dataset.op, argsFor(btn)));
    } catch (err) {
      show(err.message, true);
    }
  });
});
