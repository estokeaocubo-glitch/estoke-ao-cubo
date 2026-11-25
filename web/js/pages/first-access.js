// Primeiro acesso: escolha do nicho (18 tipos) + aceite dos Termos de Uso.
import { h, mount, toast } from "../dom.js";
import { call } from "../bridge-client.js";
import { themeToggleButton } from "./common.js";

const TERMOS_FALLBACK =
  "O arquivo TERMOS_DE_USO.txt não foi encontrado. Consulte os termos no GUIA_DO_USUARIO.txt (seção 6).";

export async function renderFirstAccess(root, { onDone }) {
  mount(root, h("div", { class: "boot" }, "Carregando tipos de negócio…"));
  const res = await call("get_first_access_catalog");
  if (!res.ok) {
    mount(root, h("div", { class: "boot" }, res.error?.message || "Falha ao carregar o catálogo."));
    return;
  }
  const { nichos = [], termos } = res.data || {};
  let escolhido = null;

  const aceite = h("input", { type: "checkbox", id: "aceite-termos" });
  const confirmar = h("button", { class: "btn btn-primary btn-lg", type: "button", disabled: true }, "Concluir configuração");
  const erro = h("div", { class: "alert alert-danger", role: "alert", hidden: true });

  function atualizar() {
    confirmar.disabled = !(escolhido && aceite.checked);
  }

  const cards = nichos.map((n) =>
    h(
      "button",
      {
        class: "nicho-card",
        type: "button",
        role: "radio",
        "aria-checked": "false",
        dataset: { key: n.key },
        style: { "--nicho-color": n.color || "" },
        onClick: (ev) => {
          escolhido = n.key;
          grid.querySelectorAll(".nicho-card").forEach((c) => c.setAttribute("aria-checked", String(c === ev.currentTarget)));
          atualizar();
        },
      },
      h("span", { class: "nicho-icon", "aria-hidden": "true" }, n.icon || "▣"),
      h("span", { class: "nicho-title" }, n.title),
      h("span", { class: "nicho-desc" }, String(n.desc || "").replace(/\n/g, " "))
    )
  );
  const grid = h("div", { class: "nicho-grid", role: "radiogroup", "aria-label": "Tipo de negócio" }, cards);

  aceite.addEventListener("change", atualizar);
  confirmar.addEventListener("click", async () => {
    erro.hidden = true;
    confirmar.disabled = true;
    const r = await call("complete_first_access", escolhido, aceite.checked === true);
    if (!r.ok) {
      erro.textContent = r.error?.message || "Não foi possível concluir.";
      erro.hidden = false;
      atualizar();
      return;
    }
    toast("Configuração concluída.", "success");
    onDone(r.data);
  });

  const page = h(
    "main",
    { class: "setup glass" },
    h(
      "div",
      { class: "setup-head" },
      h(
        "div",
        {},
        h("div", { class: "brand" }, h("img", { src: "assets/icon-cubo.png", alt: "" }), h("div", { class: "brand-name" }, "Estoke ao Cubo")),
        h("h1", { style: { "margin-top": "16px" } }, "Qual é o seu tipo de negócio?"),
        h("p", { class: "muted" }, "Isso define os módulos do menu lateral. Você poderá ajustar depois em Configurações.")
      ),
      h("span", { class: "badge" }, `${nichos.length} tipos disponíveis`)
    ),
    grid,
    h(
      "section",
      { class: "terms", "aria-label": "Termos de Uso" },
      h("div", { class: "terms-text", tabindex: "0" }, termos || TERMOS_FALLBACK),
      h("label", { class: "checkbox", for: "aceite-termos" }, aceite, h("span", {}, "Li e concordo com os Termos de Uso e Licença de Software")),
      confirmar
    ),
    erro
  );

  mount(root, h("div", { class: "center-screen" }, h("div", { class: "corner-actions" }, themeToggleButton()), page));
}
