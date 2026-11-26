// Renderizador de gráficos próprio em <canvas> (100% offline, sem biblioteca externa).
// Cores sempre lidas dos tokens CSS via getComputedStyle, então o gráfico segue o tema.
// Cada gráfico se redesenha sozinho ao mudar de tamanho (ResizeObserver) ou de tema.
import { h } from "./dom.js";

const PALETA = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5", "--chart-6", "--chart-7"];
const ativos = new Set();

// Um único observador de tema para todos os gráficos vivos.
new MutationObserver(() => {
  for (const c of [...ativos]) c.redraw();
}).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

export function token(nome, reserva = "#888") {
  const v = getComputedStyle(document.documentElement).getPropertyValue(nome).trim();
  return v || reserva;
}

export function paleta() {
  return PALETA.map((t) => token(t));
}

/** Número finito a partir de string/number (valores da bridge chegam como string Decimal). */
export function num(v) {
  const n = typeof v === "number" ? v : Number.parseFloat(String(v ?? "").replace(",", "."));
  return Number.isFinite(n) ? n : 0;
}

const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
export function moeda(v) {
  return brl.format(num(v));
}

export function moedaCompacta(v) {
  const n = num(v);
  const abs = Math.abs(n);
  if (abs >= 1e6) return `R$ ${(n / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mi`;
  if (abs >= 1e3) return `R$ ${(n / 1e3).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mil`;
  return `R$ ${n.toLocaleString("pt-BR", { maximumFractionDigits: 0 })}`;
}

/** Teto "bonito" para o eixo Y (1, 2, 2.5, 5 × 10^n). */
function tetoBonito(max) {
  if (!(max > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(max)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= max) return m * exp;
  return 10 * exp;
}

/**
 * Base: canvas com DPR, tooltip em DOM (texto via textContent) e ciclo de vida.
 * `desenhar(ctx, w, h)` devolve "alvos" para hover: [{x, y, w, h} | {teste(px,py)}, linhas[]].
 */
function criarGrafico(host, { altura = 220, rotulo, desenhar }) {
  const canvas = h("canvas", { role: "img", "aria-label": rotulo || "Gráfico" });
  const tip = h("div", { class: "chart-tip", role: "tooltip", hidden: true });
  const wrap = h("div", { class: "chart-wrap", style: { height: `${altura}px` } }, canvas, tip);
  host.append(wrap);

  let alvos = [];
  let hover = -1;
  let largura = 0;
  let alturaAtual = 0;

  function redraw() {
    if (!wrap.isConnected) {
      destroy();
      return;
    }
    const rect = wrap.getBoundingClientRect();
    largura = Math.max(0, Math.floor(rect.width));
    alturaAtual = Math.max(0, Math.floor(rect.height));
    if (!largura || !alturaAtual) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(largura * dpr);
    canvas.height = Math.round(alturaAtual * dpr);
    canvas.style.width = `${largura}px`;
    canvas.style.height = `${alturaAtual}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, largura, alturaAtual);
    ctx.font = `12px ${token("--font", "sans-serif")}`;
    alvos = desenhar(ctx, largura, alturaAtual, hover) || [];
  }

  function mostrarTip(i, px, py) {
    const alvo = alvos[i];
    if (!alvo) {
      tip.hidden = true;
      return;
    }
    tip.replaceChildren(
      ...alvo.linhas.map((l, idx) =>
        h(
          "div",
          { class: idx === 0 ? "chart-tip-title" : "chart-tip-row" },
          l.cor ? h("span", { class: "chart-dot", style: { background: l.cor } }) : null,
          h("span", {}, l.texto)
        )
      )
    );
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let x = px + 14;
    if (x + tw > largura) x = px - tw - 14;
    let y = py - th - 10;
    if (y < 0) y = py + 14;
    tip.style.transform = `translate(${Math.max(0, x)}px, ${Math.max(0, y)}px)`;
  }

  function onMove(ev) {
    const r = canvas.getBoundingClientRect();
    const px = ev.clientX - r.left;
    const py = ev.clientY - r.top;
    const i = alvos.findIndex((a) => (a.teste ? a.teste(px, py) : px >= a.x && px <= a.x + a.w && py >= a.y && py <= a.y + a.h));
    if (i !== hover) {
      hover = i;
      redraw();
    }
    canvas.style.cursor = i >= 0 ? "pointer" : "default";
    mostrarTip(i, px, py);
  }

  function onLeave() {
    hover = -1;
    tip.hidden = true;
    redraw();
  }

  canvas.addEventListener("mousemove", onMove);
  canvas.addEventListener("mouseleave", onLeave);
  const ro = new ResizeObserver(() => redraw());
  ro.observe(wrap);

  const api = { redraw, destroy };
  function destroy() {
    ro.disconnect();
    ativos.delete(api);
  }
  ativos.add(api);
  requestAnimationFrame(redraw);
  return api;
}

/**
 * Série temporal: área (série 0) + linha (série 1).
 * series: [{nome, valores:number[], cor:token}], rotulos: string[] (já formatados p/ eixo),
 * rotulosLongos: string[] (tooltip).
 */
export function lineChart(host, { rotulos, rotulosLongos, series, altura, rotulo }) {
  return criarGrafico(host, {
    altura,
    rotulo,
    desenhar(ctx, w, hgt, hover) {
      const pad = { l: 64, r: 12, t: 12, b: 26 };
      const iw = Math.max(1, w - pad.l - pad.r);
      const ih = Math.max(1, hgt - pad.t - pad.b);
      const n = rotulos.length;
      const maxV = Math.max(0, ...series.flatMap((s) => s.valores));
      const minV = Math.min(0, ...series.flatMap((s) => s.valores));
      const topo = tetoBonito(maxV);
      const base = minV < 0 ? -tetoBonito(-minV) : 0;
      const yDe = (v) => pad.t + ih - ((v - base) / (topo - base)) * ih;
      const xDe = (i) => pad.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
      const corTexto = token("--text-2");
      const corGrade = token("--border-color");

      // Grade e eixo Y
      ctx.fillStyle = corTexto;
      ctx.strokeStyle = corGrade;
      ctx.lineWidth = 1;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      const passos = 4;
      for (let k = 0; k <= passos; k++) {
        const v = base + ((topo - base) * k) / passos;
        const y = Math.round(yDe(v)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(pad.l, y);
        ctx.lineTo(w - pad.r, y);
        ctx.stroke();
        ctx.fillText(moedaCompacta(v), pad.l - 8, y);
      }

      // Eixo X (rótulos espaçados para não encavalar)
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const cabe = Math.max(1, Math.floor(iw / 56));
      const salto = Math.max(1, Math.ceil(n / cabe));
      for (let i = 0; i < n; i += salto) ctx.fillText(rotulos[i], xDe(i), hgt - pad.b + 8);

      // Séries
      series.forEach((s, idx) => {
        const cor = token(s.cor);
        if (!s.valores.length) return;
        ctx.beginPath();
        s.valores.forEach((v, i) => (i ? ctx.lineTo(xDe(i), yDe(v)) : ctx.moveTo(xDe(i), yDe(v))));
        if (idx === 0) {
          ctx.save();
          ctx.lineTo(xDe(n - 1), yDe(Math.max(base, 0)));
          ctx.lineTo(xDe(0), yDe(Math.max(base, 0)));
          ctx.closePath();
          const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ih);
          grad.addColorStop(0, comAlfa(cor, 0.28));
          grad.addColorStop(1, comAlfa(cor, 0.02));
          ctx.fillStyle = grad;
          ctx.fill();
          ctx.restore();
          ctx.beginPath();
          s.valores.forEach((v, i) => (i ? ctx.lineTo(xDe(i), yDe(v)) : ctx.moveTo(xDe(i), yDe(v))));
        }
        ctx.strokeStyle = cor;
        ctx.lineWidth = idx === 0 ? 2.25 : 1.75;
        ctx.setLineDash(idx === 0 ? [] : [5, 4]);
        ctx.stroke();
        ctx.setLineDash([]);
      });

      // Hover: guia vertical + pontos
      if (hover >= 0 && hover < n) {
        const x = Math.round(xDe(hover)) + 0.5;
        ctx.strokeStyle = token("--border-strong");
        ctx.beginPath();
        ctx.moveTo(x, pad.t);
        ctx.lineTo(x, pad.t + ih);
        ctx.stroke();
        series.forEach((s) => {
          ctx.fillStyle = token(s.cor);
          ctx.beginPath();
          ctx.arc(xDe(hover), yDe(s.valores[hover] ?? 0), 4, 0, Math.PI * 2);
          ctx.fill();
        });
      }

      // Alvos de hover: faixas verticais
      const faixa = n <= 1 ? iw : iw / (n - 1);
      return rotulos.map((_, i) => ({
        x: xDe(i) - faixa / 2,
        y: pad.t,
        w: faixa,
        h: ih,
        linhas: [
          { texto: (rotulosLongos || rotulos)[i] },
          ...series.map((s) => ({ texto: `${s.nome}: ${moeda(s.valores[i])}`, cor: token(s.cor) })),
        ],
      }));
    },
  });
}

/** Rosca: itens [{rotulo, valor}] com as cores da paleta (índice estável). */
export function donutChart(host, { itens, altura = 200, rotulo, centro }) {
  const total = itens.reduce((a, i) => a + num(i.valor), 0);
  return criarGrafico(host, {
    altura,
    rotulo,
    desenhar(ctx, w, hgt, hover) {
      const cores = paleta();
      const cx = w / 2;
      const cy = hgt / 2;
      const r = Math.max(10, Math.min(w, hgt) / 2 - 6);
      const esp = Math.max(14, r * 0.34);
      let ang = -Math.PI / 2;
      const fatias = itens.map((it, i) => {
        const frac = total > 0 ? num(it.valor) / total : 0;
        const ini = ang;
        ang += frac * Math.PI * 2;
        return { it, i, ini, fim: ang, frac };
      });
      for (const f of fatias) {
        if (f.frac <= 0) continue;
        ctx.beginPath();
        const rr = f.i === hover ? r + 3 : r;
        ctx.arc(cx, cy, rr, f.ini, f.fim);
        ctx.arc(cx, cy, rr - esp, f.fim, f.ini, true);
        ctx.closePath();
        ctx.fillStyle = cores[f.i % cores.length];
        ctx.fill();
      }
      if (centro) {
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = token("--text");
        ctx.font = `600 15px ${token("--font-display", "sans-serif")}`;
        ctx.fillText(centro, cx, cy);
      }
      return fatias.map((f) => ({
        teste(px, py) {
          const dx = px - cx;
          const dy = py - cy;
          const d = Math.hypot(dx, dy);
          if (d > r + 3 || d < r - esp) return false;
          let a = Math.atan2(dy, dx);
          if (a < -Math.PI / 2) a += Math.PI * 2;
          return a >= f.ini && a < f.fim;
        },
        linhas: [
          { texto: f.it.rotulo, cor: cores[f.i % cores.length] },
          { texto: `${moeda(f.it.valor)} · ${(f.frac * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%` },
        ],
      }));
    },
  });
}

/** Barras horizontais: itens [{rotulo, valor}], cor = token. */
export function barsChart(host, { itens, cor = "--chart-1", rotulo, alturaLinha = 30 }) {
  const altura = Math.max(60, itens.length * alturaLinha + 8);
  return criarGrafico(host, {
    altura,
    rotulo,
    desenhar(ctx, w, _hgt, hover) {
      const max = Math.max(0, ...itens.map((i) => num(i.valor))) || 1;
      const corBarra = token(cor);
      const corTexto = token("--text");
      const corTexto2 = token("--text-2");
      const trilho = token("--hover-bg");
      const larguraValor = 92;
      const larguraRotulo = Math.min(150, Math.max(80, w * 0.32));
      const x0 = larguraRotulo + 8;
      const iw = Math.max(1, w - x0 - larguraValor);
      return itens.map((it, i) => {
        const y = i * alturaLinha + 4;
        const bh = Math.min(16, alturaLinha - 12);
        const by = y + (alturaLinha - bh) / 2 - 2;
        ctx.fillStyle = corTexto2;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(cortar(ctx, it.rotulo, larguraRotulo), 0, by + bh / 2);
        arredondado(ctx, x0, by, iw, bh, bh / 2);
        ctx.fillStyle = trilho;
        ctx.fill();
        const bw = Math.max(bh, (num(it.valor) / max) * iw);
        arredondado(ctx, x0, by, bw, bh, bh / 2);
        ctx.fillStyle = i === hover ? comAlfa(corBarra, 0.75) : corBarra;
        ctx.fill();
        ctx.fillStyle = corTexto;
        ctx.textAlign = "right";
        ctx.fillText(moedaCompacta(it.valor), w, by + bh / 2);
        return { x: 0, y, w, h: alturaLinha, linhas: [{ texto: it.rotulo, cor: corBarra }, { texto: moeda(it.valor) }] };
      });
    },
  });
}

function cortar(ctx, texto, max) {
  const t = String(texto ?? "");
  if (ctx.measureText(t).width <= max) return t;
  let s = t;
  while (s.length > 1 && ctx.measureText(`${s}…`).width > max) s = s.slice(0, -1);
  return `${s}…`;
}

function arredondado(ctx, x, y, w, hgt, r) {
  const rr = Math.min(r, w / 2, hgt / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + hgt, rr);
  ctx.arcTo(x + w, y + hgt, x, y + hgt, rr);
  ctx.arcTo(x, y + hgt, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

/** Aplica alfa a uma cor #rrggbb / #rgb / rgb() / rgba(). */
export function comAlfa(cor, alfa) {
  const c = String(cor).trim();
  if (c.startsWith("#")) {
    let hex = c.slice(1);
    if (hex.length === 3) hex = [...hex].map((x) => x + x).join("");
    const n = Number.parseInt(hex.slice(0, 6), 16);
    if (Number.isFinite(n)) return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alfa})`;
  }
  const m = c.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b] = m[1].split(",").map((x) => x.trim());
    return `rgba(${r}, ${g}, ${b}, ${alfa})`;
  }
  return c;
}
