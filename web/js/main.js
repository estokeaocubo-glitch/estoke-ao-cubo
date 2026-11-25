// Entrada do frontend v2.0: bootstrap, guarda de rotas e atalhos globais.
import { h, mount, toast } from "./dom.js";
import { call, on } from "./bridge-client.js";
import { getState, setState, applyTheme } from "./state.js";
import { route, setGuard, start, navigate } from "./router.js";
import { bind } from "./hotkeys.js";
import { renderLogin } from "./pages/login.js";
import { renderFirstAccess } from "./pages/first-access.js";
import {
  mountShell, isShellMounted, unmountShell, renderHome, renderPlaceholder, toggleFullscreen,
} from "./pages/shell.js";

const app = document.getElementById("app");

async function carregarBootstrap() {
  const res = await call("get_bootstrap");
  if (!res.ok) {
    mount(
      app,
      h("div", { class: "boot" }, h("div", { class: "alert alert-danger" }, res.error?.message || "Falha ao iniciar o sistema."))
    );
    return false;
  }
  applyTheme(res.data.theme);
  setState({ bootstrap: res.data, session: res.data.session, theme: res.data.theme });
  return true;
}

function autenticado() {
  return Boolean(getState().session?.autenticado);
}

async function logout() {
  await call("logout");
  unmountShell();
  setState({ session: { autenticado: false, usuario: null, loja_ativa_id: null }, menu: null, lojas: null });
  await carregarBootstrap(); // reavalia dica de senha padrão
  navigate("/login");
}

async function comShell(render) {
  if (!isShellMounted()) {
    mount(app, h("div", { class: "boot" }, "Abrindo…"));
    await mountShell(app, { onLogout: logout });
  }
  render();
}

// Guarda: primeiro acesso > login > shell.
setGuard((path) => {
  const { bootstrap } = getState();
  if (bootstrap?.first_access_required) return "/primeiro-acesso";
  if (path === "/primeiro-acesso") return autenticado() ? "/inicio" : "/login";
  if (!autenticado()) return "/login";
  if (path === "/login" || path === "/") return "/inicio";
  return null;
});

route("/primeiro-acesso", () => {
  unmountShell();
  renderFirstAccess(app, {
    onDone: async () => {
      await carregarBootstrap();
      navigate("/login");
    },
  });
});

route("/login", () => {
  unmountShell();
  renderLogin(app, {
    onLogged: (data) => {
      setState({ session: { autenticado: true, usuario: data.usuario, loja_ativa_id: data.loja_ativa_id } });
      navigate("/inicio");
    },
  });
});

route("/inicio", () => comShell(renderHome));
route("/m/:slug", ({ slug }) => comShell(() => renderPlaceholder("m", slug)));
route("/t/:slug", ({ slug }) => comShell(() => renderPlaceholder("t", slug)));

// Atalhos globais (paridade com main.py do legado).
function irPara(path) {
  return () => {
    if (autenticado() && isShellMounted()) navigate(path);
  };
}
bind("Ctrl+N", irPara("/t/adicionar"));
bind("Ctrl+Shift+V", irPara("/t/vender"));
bind("Ctrl+E", irPara("/t/lista"));
bind("Ctrl+R", irPara("/t/relatorio"));
bind("Ctrl+G", irPara("/m/gastos"));
bind("Ctrl+K", irPara("/m/kits"));
bind("F5", () => {
  if (autenticado() && isShellMounted() && location.hash === "#/inicio") {
    renderHome();
    toast("Painel atualizado.", "success", 1500);
  }
});
bind("F11", () => toggleFullscreen());
bind("Escape", () => {
  // Esc só sai da tela cheia (como no legado); fora dela não faz nada.
  if (getState().fullscreen) toggleFullscreen();
});

on("session_changed", (payload) => {
  if (payload && payload.autenticado === false && isShellMounted()) {
    unmountShell();
    setState({ session: { autenticado: false, usuario: null, loja_ativa_id: null } });
    navigate("/login");
  }
});

(async function boot() {
  if (await carregarBootstrap()) start();
})();

