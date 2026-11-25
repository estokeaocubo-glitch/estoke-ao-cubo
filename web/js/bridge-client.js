// Cliente da bridge Python (window.pywebview.api). Toda chamada devolve o envelope
// {ok, data, error:{code, message}|null}; nenhuma exceção vaza para as páginas.

const READY_TIMEOUT_MS = 10000;
const listeners = new Map();

function apiDisponivel() {
  return Boolean(window.pywebview && window.pywebview.api && typeof window.pywebview.api.ping === "function");
}

const ready = new Promise((resolve, reject) => {
  if (apiDisponivel()) {
    resolve();
    return;
  }
  const timer = setTimeout(() => reject(new Error("BRIDGE_UNAVAILABLE")), READY_TIMEOUT_MS);
  window.addEventListener(
    "pywebviewready",
    () => {
      clearTimeout(timer);
      resolve();
    },
    { once: true }
  );
});

export function whenReady() {
  return ready;
}

function falha(code, message) {
  return { ok: false, data: null, error: { code, message } };
}

export async function call(method, ...args) {
  try {
    await ready;
  } catch {
    return falha("BRIDGE_UNAVAILABLE", "Não foi possível conectar ao núcleo do sistema.");
  }
  const fn = window.pywebview.api[method];
  if (typeof fn !== "function") {
    return falha("UNKNOWN_METHOD", `Função indisponível: ${method}`);
  }
  try {
    const result = await fn(...args);
    if (result && typeof result === "object" && "ok" in result) return result;
    return { ok: true, data: result ?? null, error: null };
  } catch (err) {
    console.error("[bridge]", method, err);
    return falha("BRIDGE_ERROR", "Falha de comunicação com o núcleo do sistema.");
  }
}

/** Assina eventos emitidos pelo Python (window.__estokeEmit). Devolve a função de cancelamento. */
export function on(event, callback) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(callback);
  return () => listeners.get(event)?.delete(callback);
}

window.__estokeEmit = (event, payload) => {
  const set = listeners.get(event);
  if (!set) return;
  for (const cb of [...set]) {
    try {
      cb(payload);
    } catch (err) {
      console.error("[bridge:event]", event, err);
    }
  }
};
