// Estado global mínimo do frontend (fonte da verdade continua no Python).

const state = {
  bootstrap: null,
  session: null, // {autenticado, usuario, loja_ativa_id}
  menu: null, // {nicho, secoes, topbar}
  lojas: null, // {lojas, loja_ativa_id, pode_trocar}
  theme: "light",
  fullscreen: false,
  sidebarCollapsed: localStorage.getItem("estoke.sidebar") === "collapsed",
};

const subs = new Set();

export function getState() {
  return state;
}

export function setState(patch) {
  Object.assign(state, patch);
  for (const fn of [...subs]) fn(state);
}

export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

export function applyTheme(theme) {
  const tema = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", tema);
  state.theme = tema;
  return tema;
}

export function setSidebarCollapsed(collapsed) {
  state.sidebarCollapsed = Boolean(collapsed);
  localStorage.setItem("estoke.sidebar", state.sidebarCollapsed ? "collapsed" : "expanded");
}
