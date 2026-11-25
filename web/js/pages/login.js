// Tela de login (paridade com LoginWindow do legado).
import { h, mount, toast } from "../dom.js";
import { call } from "../bridge-client.js";
import { getState } from "../state.js";
import { themeToggleButton } from "./common.js";

export function renderLogin(root, { onLogged }) {
  const { bootstrap } = getState();
  const erro = h("div", { class: "alert alert-danger", role: "alert", hidden: true });
  const usuario = h("input", {
    class: "input", id: "login-usuario", name: "usuario", autocomplete: "username",
    autocapitalize: "off", spellcheck: "false", required: true,
  });
  const senha = h("input", {
    class: "input", id: "login-senha", name: "senha", type: "password",
    autocomplete: "current-password", required: true,
  });
  const entrar = h("button", { class: "btn btn-primary btn-block btn-lg", type: "submit" }, "Entrar");

  async function submit(event) {
    event.preventDefault();
    erro.hidden = true;
    const login = usuario.value.trim();
    if (!login || !senha.value) {
      erro.textContent = "Informe usuário e senha.";
      erro.hidden = false;
      return;
    }
    entrar.disabled = true;
    entrar.textContent = "Entrando…";
    const res = await call("login", login, senha.value);
    entrar.disabled = false;
    entrar.textContent = "Entrar";
    if (!res.ok) {
      erro.textContent = res.error?.message || "Não foi possível entrar.";
      erro.hidden = false;
      senha.value = "";
      senha.focus();
      return;
    }
    senha.value = "";
    if (res.data?.trocar_senha) {
      toast("Você está usando a senha padrão. Troque-a em Usuários assim que possível.", "danger", 6000);
    }
    onLogged(res.data);
  }

  const dica = bootstrap?.senha_padrao_ativa
    ? h("p", { class: "login-hint" }, "Primeiro acesso? Use: ", h("span", { class: "mono" }, "admincentral / admin9438"))
    : null;

  const card = h(
    "main",
    { class: "login-card glass" },
    h(
      "div",
      { class: "brand" },
      h("img", { src: "assets/icon-cubo.png", alt: "" }),
      h("div", {}, h("div", { class: "brand-name" }, "Estoke ao Cubo"), h("div", { class: "brand-sub" }, `Versão ${bootstrap?.version || ""}`))
    ),
    h("div", {}, h("h1", {}, "Entrar"), h("p", { class: "muted" }, "Acesse com seu usuário e senha.")),
    h(
      "form",
      { class: "login-form", onSubmit: submit, novalidate: true },
      h("div", { class: "field" }, h("label", { for: "login-usuario" }, "Usuário"), usuario),
      h("div", { class: "field" }, h("label", { for: "login-senha" }, "Senha"), senha),
      erro,
      entrar
    ),
    dica
  );

  mount(root, h("div", { class: "center-screen" }, h("div", { class: "corner-actions" }, themeToggleButton()), card));
  usuario.focus();
}
