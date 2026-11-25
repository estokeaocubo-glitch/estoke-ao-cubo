// Atalhos globais (mesmo mapa da UI legada em main.py). Ficam isolados quando há foco
// em campo editável ou modal aberto; um escopo (ex.: PDV no Marco 3) pode assumir o teclado.

const bindings = new Map(); // combo -> handler
let scope = null; // {name, handler(event) -> true se consumiu}
let enabled = true;

function combo(event) {
  const partes = [];
  if (event.ctrlKey || event.metaKey) partes.push("Ctrl");
  if (event.shiftKey) partes.push("Shift");
  if (event.altKey) partes.push("Alt");
  let key = event.key;
  if (key.length === 1) key = key.toUpperCase();
  partes.push(key);
  return partes.join("+");
}

export function isEditable(el) {
  if (!el || el === document.body) return false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const tipo = (el.getAttribute("type") || "text").toLowerCase();
    return !["checkbox", "radio", "button", "submit", "reset"].includes(tipo);
  }
  return el.isContentEditable === true;
}

export function isModalOpen() {
  const root = document.getElementById("modal-root");
  return Boolean(root && root.childElementCount > 0);
}

/** Registra um atalho global. Ex.: bind("Ctrl+N", fn). */
export function bind(keyCombo, handler) {
  bindings.set(keyCombo, handler);
}

export function unbindAll() {
  bindings.clear();
}

export function setEnabled(value) {
  enabled = Boolean(value);
}

/** Gancho do PDV: o handler recebe todo keydown antes dos globais e devolve true se consumiu. */
export function setScope(name, handler) {
  scope = name && handler ? { name, handler } : null;
}

export function getScope() {
  return scope ? scope.name : null;
}

// Teclas que o WebView trataria sozinho e que o sistema usa (F5 recarregaria a página).
const BLOQUEAR_PADRAO = new Set(["F5", "Ctrl+R", "Ctrl+Shift+R", "F11", "Ctrl+N", "Ctrl+E", "Ctrl+G", "Ctrl+K", "Ctrl+Shift+V"]);

function onKeyDown(event) {
  const k = combo(event);
  if (BLOQUEAR_PADRAO.has(k)) event.preventDefault();
  if (!enabled) return;

  if (scope) {
    try {
      if (scope.handler(event) === true) {
        event.preventDefault();
        return;
      }
    } catch (err) {
      console.error("[hotkeys:scope]", err);
    }
  }

  if (isModalOpen()) return;
  // Em campo editável só passam teclas de função (F5/F11/Esc), nunca Ctrl+letra.
  if (isEditable(document.activeElement) && !/^F\d+$|^Escape$/.test(k)) return;

  const handler = bindings.get(k);
  if (handler) {
    event.preventDefault();
    handler(event);
  }
}

window.addEventListener("keydown", onKeyDown, true);
