// Dashboard Bento Grid (Marco 2): KPIs da loja ativa, gráficos offline, metas e alertas.
// Toda informação vem da bridge (get_resumo, get_graficos, get_alertas, list_metas).
// Valores monetários chegam como string Decimal e só são convertidos para exibição.
import { h, clear, toast } from "../dom.js";
import { call, on } from "../bridge-client.js";
import { lineChart, donutChart, barsChart, moeda, num, paleta, token } from "../charts.js";

const PERIODOS = [
  ["hoje", "Hoje"],
  ["7d", "7 dias"],
  ["30d", "30 dias"],
  ["mes", "Mês"],
  ["ano", "Ano"],
];
const CHAVE_PERIODO = "estoke.dashboard.periodo";
const MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
const MESES_LONGOS = [
  "janeiro", "fevereiro", "março", "abril", "maio", "junho",
  "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
];
const DIAS_SEMANA = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];

let periodo = lerPeriodo();
let atual = null; // {root, ctx, gen, graficos[]}
let eventosLigados = false;

function lerPeriodo() {
  try {
    const p = localStorage.getItem(CHAVE_PERIODO);
    if (PERIODOS.some(([k]) => k === p)) return p;
  } catch {
    /* storage indisponível: usa o padrão */
  }
  return "30d";
}

function salvarPeriodo(p) {
  periodo = p;
  try {
    localStorage.setItem(CHAVE_PERIODO, p);
  } catch {
    /* ignora */
  }
}

export function isDashboardMounted() {
  return Boolean(atual && atual.root.isConnected);
}

/**
 * Monta o dashboard em `container`.
 * ctx: {nomeLoja():string, saudacao:string, atalhos:[[rotulo, tecla]]}
 */
export function renderDashboard(container, ctx) {
  destruirGraficos();
  const root = h("div", { class: "dashboard" });
  clear(container);
  container.append(root);
  atual = { root, ctx, gen: 0, graficos: [] };
  initDashboardEvents();
  return refreshDashboard({ skeleton: true });
}

/**
 * Recarrega os dados. `skeleton: true` (F5/primeira carga) troca tudo por placeholders;
 * sem skeleton (troca de loja, tick) mantém o conteúdo até os dados novos chegarem.
 */
export async function refreshDashboard({ skeleton = false, silencioso = false } = {}) {
  if (!isDashboardMounted()) return false;
  const estado = atual;
  const gen = ++estado.gen;
  if (skeleton) desenharSkeleton(estado);
  else if (!silencioso) estado.root.setAttribute("aria-busy", "true");

  const [resumo, graficos, alertas, metas] = await Promise.all([
    call("get_resumo", periodo),
    call("get_graficos", periodo),
    call("get_alertas"),
    call("list_metas"),
  ]);
  if (estado !== atual || gen !== estado.gen || !estado.root.isConnected) return false;
  estado.root.removeAttribute("aria-busy");

  const falha = [resumo, graficos, alertas, metas].find((r) => !r.ok);
  if (falha) {
    if (falha.error?.code === "NOT_AUTHENTICATED") return false;
    desenharErro(estado, falha.error?.message || "Não foi possível carregar o painel.");
    return false;
  }
  desenharPainel(estado, resumo.data, graficos.data, alertas.data, metas.data);
  return true;
}

// ---------------------------------------------------------------------------
// Eventos da bridge (assinados uma única vez)
// ---------------------------------------------------------------------------
export function initDashboardEvents() {
  if (eventosLigados) return;
  eventosLigados = true;

  on("dashboard:tick", () => {
    if (isDashboardMounted() && document.visibilityState !== "hidden") refreshDashboard({ silencioso: true });
  });

  on("metas_changed", () => {
    if (isDashboardMounted()) recarregarMetas();
  });

  on("dashboard:alertas_iniciais", (p) => {
    const partes = [];
    if (num(p?.vencidos)) partes.push(plural(p.vencidos, "produto vencido", "produtos vencidos"));
    const vencendo = num(p?.validade) - num(p?.vencidos);
    if (vencendo > 0) partes.push(plural(vencendo, "produto perto do vencimento", "produtos perto do vencimento"));
    if (num(p?.estoque_baixo)) partes.push(plural(p.estoque_baixo, "item com estoque baixo", "itens com estoque baixo"));
    if (num(p?.outbox_nfce)) partes.push(plural(p.outbox_nfce, "NFC-e na fila", "NFC-e na fila"));
    if (partes.length) toast(`Atenção: ${partes.join(", ")}.`, "warning", 7000);
  });

  on("update:disponivel", (p) => {
    if (p?.version) toast(`Nova versão ${p.version} disponível (atual ${p.current_version || "—"}).`, "info", 8000);
  });
}

// ---------------------------------------------------------------------------
// Estrutura
// ---------------------------------------------------------------------------
function cabecalho(estado, resumo) {
  const seg = h(
    "div",
    { class: "seg", role: "group", "aria-label": "Período" },
    PERIODOS.map(([k, rotulo]) =>
      h(
        "button",
        {
          type: "button",
          "aria-pressed": k === periodo ? "true" : "false",
          onclick: () => {
            if (k === periodo) return;
            salvarPeriodo(k);
            seg.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", PERIODOS[i][0] === k ? "true" : "false"));
            refreshDashboard();
          },
        },
        rotulo
      )
    )
  );

  const status = h("div", { class: "dash-status" });
  if (resumo) {
    const cx = resumo.caixa || {};
    status.append(
      cx.aberto
        ? h("span", { class: "badge badge-success", title: cx.data_abertura ? `Aberto em ${dataHora(cx.data_abertura)}` : "" }, "● Caixa aberto")
        : h("span", { class: "badge badge-muted" }, "○ Caixa fechado")
    );
    const ob = resumo.outbox_nfce || {};
    if (num(ob.falhas) > 0) status.append(h("span", { class: "badge badge-danger" }, `NFC-e: ${ob.falhas} com falha`));
    if (num(ob.pendentes) > 0) status.append(h("span", { class: "badge badge-warning" }, `NFC-e: ${ob.pendentes} pendente(s)`));
    if (!num(ob.total)) status.append(h("span", { class: "badge badge-muted" }, "NFC-e: fila vazia"));
  }

  return h(
    "div",
    { class: "page-head dash-head" },
    h(
      "div",
      {},
      h("h1", {}, "Início"),
      h("p", { class: "muted" }, `${estado.ctx.saudacao} · ${estado.ctx.nomeLoja()}${resumo ? ` · ${intervalo(resumo)}` : ""}`),
      status
    ),
    seg
  );
}

function desenharSkeleton(estado) {
  destruirGraficos();
  const cartao = (span, blocos) => h("article", { class: `card glass ${span}`, "aria-hidden": "true" }, blocos);
  const kpi = () => cartao("span-3", [h("div", { class: "skel skel-line", style: { width: "40%" } }), h("div", { class: "skel skel-big" }), h("div", { class: "skel skel-line", style: { width: "60%", "margin-top": "auto" } })]);
  const bloco = (span, extra = "") => cartao(`${span} ${extra}`, [h("div", { class: "skel skel-line", style: { width: "30%" } }), h("div", { class: "skel skel-block" })]);
  clear(estado.root);
  estado.root.setAttribute("aria-busy", "true");
  estado.root.append(
    cabecalho(estado, null),
    h("div", { class: "bento" }, kpi(), kpi(), kpi(), kpi(), bloco("span-8", "row-2"), bloco("span-4", "row-2"), bloco("span-4"), bloco("span-4"), bloco("span-4"), bloco("span-7"), bloco("span-5"))
  );
}

function desenharErro(estado, mensagem) {
  destruirGraficos();
  clear(estado.root);
  estado.root.append(
    cabecalho(estado, null),
    h(
      "div",
      { class: "card glass" },
      h("div", { class: "empty" }, h("div", {}, h("strong", {}, "Não foi possível carregar o painel"), h("span", {}, mensagem))),
      h("div", { style: { display: "flex", "justify-content": "center" } }, h("button", { class: "btn btn-primary", type: "button", onclick: () => refreshDashboard({ skeleton: true }) }, "Tentar de novo"))
    )
  );
}

function desenharPainel(estado, resumo, graficos, alertas, metas) {
  destruirGraficos();
  const grid = h(
    "div",
    { class: "bento" },
    ...kpis(resumo),
    cardSerie(estado, graficos),
    cardDonut(estado, "Formas de pagamento", graficos.pagamentos, "Sem vendas no período", "span-4 row-2"),
    cardBarras(estado, "Vendas por categoria", graficos.categorias, "--chart-2", "Sem vendas no período"),
    cardBarras(estado, "Gastos por categoria", graficos.gastos_categorias, "--chart-6", "Nenhum gasto no período"),
    cardAlertas(alertas),
    cardMetas(metas),
    cardAtalhos(estado.ctx.atalhos)
  );
  clear(estado.root);
  estado.root.append(cabecalho(estado, resumo), grid);
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------
function kpi(titulo, valor, sub, classeValor = "") {
  return h(
    "article",
    { class: "card glass span-3 kpi" },
    h("div", { class: "card-title" }, titulo),
    h("div", { class: `kpi-valor ${classeValor}` }, valor),
    h("div", { class: "kpi-sub" }, sub)
  );
}

function delta(pct, rotulo = "vs. período anterior") {
  if (pct === null || pct === undefined || pct === "") return h("span", {}, "sem base de comparação");
  const v = num(pct);
  const cls = v > 0 ? "up" : v < 0 ? "down" : "flat";
  const seta = v > 0 ? "▲" : v < 0 ? "▼" : "■";
  return [h("span", { class: `delta ${cls}` }, `${seta} ${Math.abs(v).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`), h("span", {}, rotulo)];
}

function kpis(r) {
  const fat = num(r.faturamento);
  const lucro = num(r.lucro_bruto);
  const res = num(r.resultado);
  const margem = fat > 0 ? `${((lucro / fat) * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}% de margem` : "margem indisponível";
  const qtd = num(r.qtd_vendas);
  const qtdAnt = num(r.qtd_vendas_anterior);
  const pctQtd = qtdAnt > 0 ? String(((qtd - qtdAnt) / qtdAnt) * 100) : null;
  return [
    kpi("Faturamento", moeda(r.faturamento), [delta(r.variacao_pct), h("span", {}, `Vendas ${moeda(r.faturamento_vendas)} · Serviços ${moeda(r.faturamento_servicos)}`)]),
    kpi("Lucro bruto", moeda(r.lucro_bruto), [h("span", {}, margem)], lucro < 0 ? "valor-neg" : ""),
    kpi("Resultado", moeda(r.resultado), [h("span", {}, `Gastos ${moeda(r.gastos)}`), num(r.fiado_pendente) > 0 ? h("span", {}, `· Fiado pendente ${moeda(r.fiado_pendente)}`) : null], res < 0 ? "valor-neg" : ""),
    kpi(
      "Vendas",
      qtd.toLocaleString("pt-BR"),
      [delta(pctQtd), h("span", {}, `Ticket médio ${moeda(r.ticket_medio)} · ${plural(r.servicos_concluidos, "serviço concluído", "serviços concluídos")}`)]
    ),
  ];
}

// ---------------------------------------------------------------------------
// Gráficos
// ---------------------------------------------------------------------------
function vazio(titulo, texto) {
  return h("div", { class: "empty" }, h("div", {}, h("strong", {}, titulo), h("span", {}, texto)));
}

function cardSerie(estado, g) {
  const serie = g.serie || { rotulos: [], faturamento: [], lucro: [] };
  const corpo = h("div", { style: { flex: "1", display: "flex", "flex-direction": "column", "justify-content": "flex-end" } });
  const legenda = h(
    "div",
    { class: "chart-legend" },
    h("span", {}, h("span", { class: "chart-dot", style: { background: token("--chart-1") } }), "Faturamento"),
    h("span", {}, h("span", { class: "chart-dot", style: { background: token("--chart-3") } }), "Lucro bruto")
  );
  const card = h("article", { class: "card glass span-8 row-2 chart-card" }, h("div", { class: "card-title" }, h("span", {}, "Faturamento × lucro"), legenda), corpo);
  if (g.vazio || !serie.rotulos.length) {
    legenda.hidden = true;
    corpo.append(vazio("Nada por aqui ainda", "As vendas e serviços concluídos do período aparecem neste gráfico."));
    return card;
  }
  estado.graficos.push(
    lineChart(corpo, {
      altura: 290,
      rotulo: "Faturamento e lucro bruto no período",
      rotulos: serie.rotulos.map((r) => rotuloCurto(r, g.passo)),
      rotulosLongos: serie.rotulos.map((r) => rotuloLongo(r, g.passo)),
      series: [
        { nome: "Faturamento", valores: serie.faturamento.map(num), cor: "--chart-1" },
        { nome: "Lucro bruto", valores: serie.lucro.map(num), cor: "--chart-3" },
      ],
    })
  );
  return card;
}

function cardDonut(estado, titulo, itens, textoVazio, span) {
  const lista = (itens || []).filter((i) => num(i.valor) > 0);
  const card = h("article", { class: `card glass ${span} chart-card` }, h("div", { class: "card-title" }, titulo));
  if (!lista.length) {
    card.append(vazio("Sem dados", textoVazio));
    return card;
  }
  const total = lista.reduce((a, i) => a + num(i.valor), 0);
  const host = h("div", {});
  card.append(host);
  estado.graficos.push(donutChart(host, { itens: lista, altura: 190, rotulo: titulo, centro: moedaCurta(total) }));
  const cores = paleta();
  card.append(
    h(
      "ul",
      { class: "donut-legend" },
      lista.map((it, i) =>
        h(
          "li",
          {},
          h("span", { class: "chart-dot", style: { background: cores[i % cores.length] } }),
          h("span", { class: "nome", title: it.rotulo }, it.rotulo),
          h("span", { class: "val" }, `${((num(it.valor) / total) * 100).toLocaleString("pt-BR", { maximumFractionDigits: 0 })}%`)
        )
      )
    )
  );
  return card;
}

function cardBarras(estado, titulo, itens, cor, textoVazio) {
  const lista = (itens || []).filter((i) => num(i.valor) > 0);
  const card = h("article", { class: "card glass span-4 chart-card" }, h("div", { class: "card-title" }, titulo));
  if (!lista.length) {
    card.append(vazio("Sem dados", textoVazio));
    return card;
  }
  const host = h("div", {});
  card.append(host);
  estado.graficos.push(barsChart(host, { itens: lista, cor, rotulo: titulo }));
  return card;
}

function destruirGraficos() {
  if (!atual) return;
  for (const g of atual.graficos) g.destroy();
  atual.graficos = [];
}

// ---------------------------------------------------------------------------
// Alertas
// ---------------------------------------------------------------------------
function cardAlertas(a) {
  const card = h("article", { class: "card glass span-4" }, h("div", { class: "card-title" }, "Alertas da loja"));
  const grupos = [];
  if (a.validade?.length) {
    grupos.push(
      h(
        "div",
        { class: "alert-group" },
        h("h4", {}, `Validade (${a.validade_total})`),
        h(
          "ul",
          { class: "alert-list" },
          a.validade.map((v) =>
            h(
              "li",
              {},
              h("span", { class: "nome", title: v.nome }, v.nome),
              h("span", { class: `badge ${v.vencido ? "badge-danger" : "badge-warning"}` }, v.vencido ? "vencido" : v.dias === 0 ? "vence hoje" : v.dias === null ? dataCurta(v.data_validade) : `${v.dias} dia(s)`)
            )
          )
        )
      )
    );
  }
  if (a.estoque_baixo?.length) {
    grupos.push(
      h(
        "div",
        { class: "alert-group" },
        h("h4", {}, `Estoque baixo (${a.estoque_baixo_total})`),
        h(
          "ul",
          { class: "alert-list" },
          a.estoque_baixo.map((p) =>
            h("li", {}, h("span", { class: "nome", title: p.nome }, p.nome), h("span", { class: "badge badge-warning" }, `${qtdTexto(p.quantidade)} / mín. ${qtdTexto(p.estoque_minimo)}`))
          )
        )
      )
    );
  }
  if (num(a.outbox_nfce?.total) > 0) {
    grupos.push(h("div", { class: "alert-group" }, h("h4", {}, "NFC-e"), h("p", { class: "muted" }, `${a.outbox_nfce.pendentes} pendente(s) e ${a.outbox_nfce.falhas} com falha na fila de envio.`)));
  }
  if (!grupos.length) card.append(vazio("Tudo em dia", "Nenhum produto vencendo, sem estoque baixo e fila de NFC-e vazia."));
  else card.append(...grupos);
  return card;
}

// ---------------------------------------------------------------------------
// Metas
// ---------------------------------------------------------------------------
function cardMetas(metas) {
  const lista = h("ul", { class: "metas-list" });
  const titulo = h("input", { class: "input", type: "text", maxlength: "120", placeholder: "Nova meta (ex.: vender 50 kits)", "aria-label": "Título da meta" });
  const prazo = h("input", { class: "input input-prazo", type: "date", "aria-label": "Prazo da meta" });
  const botao = h("button", { class: "btn btn-primary", type: "submit" }, "Adicionar");
  const form = h("form", { class: "metas-form", onsubmit: criarMeta }, titulo, prazo, botao);

  async function criarMeta(ev) {
    ev.preventDefault();
    const t = titulo.value.trim();
    if (!t) {
      titulo.focus();
      return;
    }
    botao.disabled = true;
    const res = await call("create_meta", t, prazo.value || null, "");
    botao.disabled = false;
    if (!res.ok) {
      toast(res.error?.message || "Não foi possível criar a meta.", "danger");
      return;
    }
    titulo.value = "";
    prazo.value = "";
    titulo.focus();
    toast("Meta criada.", "success", 1800);
    recarregarMetas();
  }

  preencherMetas(lista, metas);
  return h("article", { class: "card glass span-7", id: "card-metas" }, h("div", { class: "card-title" }, "Metas"), form, lista);
}

function preencherMetas(lista, metas) {
  clear(lista);
  if (!metas.length) {
    lista.append(h("li", {}, vazio("Nenhuma meta cadastrada", "Crie uma meta acima para acompanhar aqui.")));
    return;
  }
  const hoje = new Date().toISOString().slice(0, 10);
  for (const m of metas) {
    const atrasada = !m.concluida && m.prazo && m.prazo.slice(0, 10) < hoje;
    lista.append(
      h(
        "li",
        { class: `meta${m.concluida ? " concluida" : ""}` },
        h("input", {
          type: "checkbox",
          checked: m.concluida,
          "aria-label": m.concluida ? `Reabrir meta ${m.titulo}` : `Concluir meta ${m.titulo}`,
          onchange: async (ev) => {
            ev.target.disabled = true;
            const res = await call("toggle_meta", m.id);
            if (!res.ok) {
              ev.target.disabled = false;
              ev.target.checked = m.concluida;
              toast(res.error?.message || "Não foi possível atualizar a meta.", "danger");
              return;
            }
            recarregarMetas();
          },
        }),
        h("span", { class: "meta-titulo", title: m.descricao || "" }, m.titulo),
        m.prazo ? h("span", { class: `meta-prazo${atrasada ? " atrasada" : ""}` }, atrasada ? `atrasada · ${dataCurta(m.prazo)}` : dataCurta(m.prazo)) : null,
        h(
          "button",
          {
            class: "btn btn-ghost btn-icon",
            type: "button",
            title: "Excluir meta",
            "aria-label": `Excluir meta ${m.titulo}`,
            onclick: async () => {
              const res = await call("delete_meta", m.id);
              if (!res.ok) toast(res.error?.message || "Não foi possível excluir a meta.", "danger");
              else {
                toast("Meta excluída.", "success", 1800);
                recarregarMetas();
              }
            },
          },
          "✕"
        )
      )
    );
  }
}

async function recarregarMetas() {
  const res = await call("list_metas");
  const lista = atual?.root.querySelector("#card-metas .metas-list");
  if (!res.ok || !lista) return;
  preencherMetas(lista, res.data);
}

function cardAtalhos(atalhos) {
  return h(
    "article",
    { class: "card glass span-5" },
    h("div", { class: "card-title" }, "Atalhos de teclado"),
    h("ul", { class: "shortcut-list" }, (atalhos || []).map(([rotulo, tecla]) => h("li", {}, h("span", {}, rotulo), h("kbd", {}, tecla))))
  );
}

// ---------------------------------------------------------------------------
// Formatação
// ---------------------------------------------------------------------------
function plural(n, um, varios) {
  const v = num(n);
  return `${v.toLocaleString("pt-BR")} ${v === 1 ? um : varios}`;
}

function qtdTexto(v) {
  return num(v).toLocaleString("pt-BR", { maximumFractionDigits: 3 });
}

function moedaCurta(v) {
  const n = num(v);
  if (Math.abs(n) >= 1e5) return `R$ ${(n / 1e3).toLocaleString("pt-BR", { maximumFractionDigits: 0 })} mil`;
  return moeda(n);
}

function partesData(iso) {
  const m = /^(\d{4})-(\d{2})(?:-(\d{2}))?/.exec(String(iso || ""));
  if (!m) return null;
  return { a: Number(m[1]), m: Number(m[2]), d: m[3] ? Number(m[3]) : 1 };
}

function dataCurta(iso) {
  const p = partesData(iso);
  return p ? `${String(p.d).padStart(2, "0")}/${String(p.m).padStart(2, "0")}/${p.a}` : "—";
}

function dataHora(iso) {
  const d = new Date(String(iso).replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function intervalo(r) {
  if (r.periodo === "hoje") return `hoje, ${dataCurta(r.inicio)}`;
  return `${dataCurta(r.inicio)} a ${dataCurta(r.fim)}`;
}

function rotuloCurto(r, passo) {
  if (passo === "hora") return `${r}h`;
  const p = partesData(r);
  if (!p) return String(r);
  if (passo === "mes") return MESES[p.m - 1];
  return `${String(p.d).padStart(2, "0")}/${String(p.m).padStart(2, "0")}`;
}

function rotuloLongo(r, passo) {
  if (passo === "hora") return `${r}:00 às ${r}:59`;
  const p = partesData(r);
  if (!p) return String(r);
  if (passo === "mes") return `${MESES_LONGOS[p.m - 1]} de ${p.a}`;
  const dia = new Date(p.a, p.m - 1, p.d);
  return `${DIAS_SEMANA[dia.getDay()]}, ${dataCurta(r)}`;
}
