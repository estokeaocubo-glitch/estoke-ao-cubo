// Shell autenticado: sidebar colapsável + topbar + área de conteúdo (home em bento grid).
import { h, mount, clear, glyph, toast } from "../dom.js";
import { call } from "../bridge-client.js";
import { getState, setState, setSidebarCollapsed } from "../state.js";
import { navigate } from "../router.js";
import { themeToggleButton, marcoDe } from "./common.js";
import { renderDashboard, refreshDashboard, isDashboardMounted } from "./dashboard.js";

const TOPBAR_CLASSE = {
  adicionar: "tb-adicionar",
  vender: "tb-vender",
  servico: "tb-servico",
  lista: "tb-lista",
  relatorio: "tb-relatorio",
  dre: "tb-dre",
};
const TOPBAR_ATALHO = { adicionar: "Ctrl+N", vender: "Ctrl+Shift+V", lista: "Ctrl+E", relatorio: "Ctrl+R" };
const TOPBAR_ROTULO = { servico: "SERVIÇO", relatorio: "RELATÓRIO" };

const ATALHOS_HOME = [
  ["Adicionar produto", "Ctrl+N"],
  ["Vender (PDV)", "Ctrl+Shift+V"],
  ["Lista de produtos", "Ctrl+E"],
  ["Relatórios", "Ctrl+R"],
  ["Gastos", "Ctrl+G"],
  ["Kits", "Ctrl+K"],
  ["Atualizar painel", "F5"],
  ["Tela cheia", "F11"],
];

let refs = null; // elementos do shell montado

export function isShellMounted() {
  return Boolean(refs && document.body.contains(refs.shell));
}

export function unmountShell() {
  refs = null;
}

/** Busca menu e lojas e monta o shell (uma vez por sessão). */
export async function mountShell(root, { onLogout }) {
  const [menu, lojas] = await Promise.all([call("get_sidebar_menu"), call("list_lojas")]);
  if (!menu.ok) {
    toast(menu.error?.message || "Falha ao carregar o menu.", "danger");
  }
  setState({ menu: menu.ok ? menu.data : { secoes: [], topbar: [] }, lojas: lojas.ok ? lojas.data : null });

  const shell = h("div", { class: "shell", "data-sidebar": getState().sidebarCollapsed ? "collapsed" : "expanded" });
  const content = h("section", { class: "content", id: "content", tabindex: "-1" });
  const sidebar = buildSidebar(shell);
  const topbar = buildTopbar(onLogout);
  shell.append(sidebar, h("div", { class: "main" }, topbar, content));
  mount(root, shell);
  refs = { shell, content, sidebar };
}

function buildSidebar(shell) {
  const { menu, bootstrap } = getState();
  const toggle = h(
    "button",
    {
      class: "btn btn-icon btn-ghost",
      type: "button",
      "aria-label": "Recolher menu lateral",
      title: "Recolher/expandir menu",
      onClick: () => {
        const recolher = shell.dataset.sidebar !== "collapsed";
        shell.dataset.sidebar = recolher ? "collapsed" : "expanded";
        setSidebarCollapsed(recolher);
        toggle.setAttribute("aria-label", recolher ? "Expandir menu lateral" : "Recolher menu lateral");
      },
    },
    "☰"
  );

  const inicio = navItem({ id: "inicio", texto: "Início", permitido: true }, "#/inicio", "⌂");
  const secoes = (menu?.secoes || []).map((secao) =>
    h(
      "div",
      { class: "nav-section", role: "group", "aria-label": secao.titulo },
      h("div", { class: "nav-section-title" }, secao.titulo),
      secao.itens.map((item) => navItem(item, `#/m/${item.id}`))
    )
  );

  const nicho = bootstrap?.nicho_info;
  return h(
    "aside",
    { class: "sidebar glass", "aria-label": "Menu principal" },
    h(
      "div",
      { class: "sidebar-head" },
      h(
        "div",
        { class: "brand" },
        h("img", { src: "assets/icon-cubo.png", alt: "" }),
        h("div", { class: "brand-text" }, h("div", { class: "brand-name" }, "Estoke ao Cubo"))
      ),
      toggle
    ),
    h("nav", { class: "sidebar-nav" }, h("div", { class: "nav-section" }, inicio), secoes),
    h(
      "div",
      { class: "sidebar-foot" },
      h("span", {}, nicho ? `${nicho.icon || ""} ${nicho.title}` : "Variados"),
      h("span", { class: "mono" }, `v${bootstrap?.version || ""}`)
    )
  );
}

function navItem(item, href, icone) {
  const bloqueado = !item.permitido;
  return h(
    "button",
    {
      class: "nav-item",
      type: "button",
      title: bloqueado ? `${item.texto} — sem permissão` : item.texto,
      "aria-disabled": bloqueado ? "true" : null,
      dataset: { href },
      onClick: () => {
        if (bloqueado) {
          toast(`Seu perfil não tem acesso a ${item.texto}.`, "danger");
          return;
        }
        navigate(href.slice(1));
      },
    },
    h("span", { class: "nav-glyph", "aria-hidden": "true" }, icone || glyph(item.texto)),
    h("span", { class: "nav-label" }, item.texto),
    bloqueado ? h("span", { class: "nav-lock", "aria-hidden": "true" }, "🔒") : null
  );
}

function buildTopbar(onLogout) {
  const { menu, lojas, session } = getState();
  const botoes = (menu?.topbar || []).map((item) => {
    const bloqueado = !item.permitido;
    const atalho = TOPBAR_ATALHO[item.id];
    return h(
      "button",
      {
        class: `tb-btn ${TOPBAR_CLASSE[item.id] || ""}`,
        type: "button",
        "aria-disabled": bloqueado ? "true" : null,
        title: bloqueado ? "Sem permissão" : atalho ? `${item.texto} (${atalho})` : item.texto,
        onClick: () => (bloqueado ? toast(`Seu perfil não tem acesso a ${item.texto}.`, "danger") : navigate(`/t/${item.id}`)),
      },
      TOPBAR_ROTULO[item.id] || item.texto,
      atalho ? h("kbd", {}, atalho) : null
    );
  });
  botoes.push(
    h("button", { class: "tb-btn tb-f11", type: "button", title: "Tela cheia (F11)", onClick: toggleFullscreen }, "F11")
  );

  const seletor = lojaSelector(lojas);
  const u = session?.usuario || {};
  const chip = h(
    "div",
    { class: "user-chip", title: `${u.nome || ""} (${u.perfil || ""})` },
    h("span", { class: "avatar", "aria-hidden": "true" }, u.emoji || "👤"),
    h("span", { class: "user-text" }, h("span", { class: "user-name" }, u.nome || "Usuário"), h("span", { class: "user-role" }, u.perfil || ""))
  );
  const sair = h("button", { class: "btn", type: "button", onClick: onLogout, title: "Sair do sistema" }, "Sair");

  return h(
    "header",
    { class: "topbar glass" },
    h("div", { class: "topbar-actions", role: "toolbar", "aria-label": "Ações rápidas" }, botoes),
    h("div", { class: "topbar-right" }, seletor, themeToggleButton(), chip, sair)
  );
}

function lojaSelector(lojas) {
  if (!lojas || !lojas.lojas?.length) return null;
  const select = h(
    "select",
    { class: "select", "aria-label": "Loja ativa", disabled: !lojas.pode_trocar },
    lojas.lojas.map((l) => h("option", { value: String(l.id), selected: l.id === lojas.loja_ativa_id }, l.nome))
  );
  select.addEventListener("change", async () => {
    const res = await call("set_loja_ativa", Number(select.value));
    if (!res.ok) {
      toast(res.error?.message || "Não foi possível trocar de loja.", "danger");
      select.value = String(getState().lojas?.loja_ativa_id ?? "");
      return;
    }
    const s = getState();
    setState({ lojas: { ...s.lojas, loja_ativa_id: res.data.loja_ativa_id }, session: { ...s.session, loja_ativa_id: res.data.loja_ativa_id } });
    toast(`Loja ativa: ${select.options[select.selectedIndex].text}`, "success");
    if (location.hash === "#/inicio") {
      if (isDashboardMounted()) refreshDashboard();
      else renderHome();
    }
  });
  return select;
}

export async function toggleFullscreen() {
  const res = await call("toggle_fullscreen");
  if (!res.ok) {
    toast(res.error?.message || "Tela cheia indisponível.", "danger");
    return null;
  }
  setState({ fullscreen: Boolean(res.data.fullscreen) });
  return res.data.fullscreen;
}

function marcarAtivo(href) {
  if (!refs) return;
  refs.sidebar.querySelectorAll(".nav-item").forEach((el) => {
    if (el.dataset.href === href) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
}

function nomeDaLoja() {
  const { lojas } = getState();
  return lojas?.lojas?.find((l) => l.id === lojas.loja_ativa_id)?.nome || "—";
}

/** Home: dashboard Bento do Marco 2 (dados reais da loja ativa via bridge). */
export function renderHome() {
  if (!refs) return;
  marcarAtivo("#/inicio");
  const u = getState().session?.usuario || {};
  const hora = new Date().getHours();
  const saudacao = hora < 12 ? "Bom dia" : hora < 18 ? "Boa tarde" : "Boa noite";
  renderDashboard(refs.content, {
    saudacao: u.nome ? `${saudacao}, ${u.nome}` : saudacao,
    nomeLoja: nomeDaLoja,
    atalhos: ATALHOS_HOME,
  });
}

/** Placeholder navegável para itens da sidebar (#/m/<slug>) e da topbar (#/t/<slug>). */
export function renderPlaceholder(tipo, slug) {
  if (!refs) return;
  const href = `#/${tipo}/${slug}`;
  marcarAtivo(href);
  const { menu } = getState();
  const itens = tipo === "t" ? menu?.topbar || [] : (menu?.secoes || []).flatMap((s) => s.itens);
  const item = itens.find((i) => i.id === slug);
  if (!item) {
    navigate("/inicio", { replace: true });
    return;
  }
  if (!item.permitido) {
    toast(`Seu perfil não tem acesso a ${item.texto}.`, "danger");
    navigate("/inicio", { replace: true });
    return;
  }
  const titulo = TOPBAR_ROTULO[slug] || item.texto;
  clear(refs.content);
  refs.content.append(
    h(
      "div",
      { class: "placeholder-page card glass" },
      h(
        "div",
        { class: "inner" },
        h("div", { class: "glyph", "aria-hidden": "true" }, glyph(item.texto)),
        h("h1", {}, titulo),
        h("p", { class: "muted" }, `Esta tela ainda roda na interface clássica. Disponível na v2.0 no ${marcoDe(slug)}.`),
        h("button", { class: "btn", type: "button", onClick: () => navigate("/inicio") }, "Voltar ao início")
      )
    )
  );
  refs.content.focus({ preventScroll: true });
}
