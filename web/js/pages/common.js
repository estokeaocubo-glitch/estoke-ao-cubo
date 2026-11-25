// Peças compartilhadas entre páginas.
import { h, toast } from "../dom.js";
import { call } from "../bridge-client.js";
import { applyTheme, getState } from "../state.js";

/** Troca o tema na hora e persiste em config.json via bridge. */
export async function toggleTheme() {
  const anterior = getState().theme;
  const novo = applyTheme(anterior === "dark" ? "light" : "dark");
  const res = await call("set_theme", novo);
  if (!res.ok) {
    applyTheme(anterior);
    toast(res.error?.message || "Não foi possível salvar o tema.", "danger");
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(syncThemeButton);
}

function syncThemeButton(btn) {
  const dark = getState().theme === "dark";
  btn.textContent = dark ? "☀" : "☾";
  btn.setAttribute("aria-label", dark ? "Usar tema claro" : "Usar tema escuro");
  btn.title = dark ? "Tema claro" : "Tema escuro";
}

export function themeToggleButton() {
  const btn = h("button", { class: "btn btn-icon", type: "button", "data-theme-toggle": true, onClick: toggleTheme });
  syncThemeButton(btn);
  return btn;
}

/** Em qual marco cada área ganha tela real (usado nos placeholders). */
export function marcoDe(slug) {
  if (slug === "inicio") return "Marco 2 — Dashboard Bento";
  if (["vender", "caixa", "servico"].includes(slug)) return "Marco 3 — PDV v2.0";
  return "Marco 4 — Módulos";
}
