# -*- coding: utf-8 -*-
"""
Bridge do Dashboard (Marco 2): KPIs, gráficos, metas e alertas da loja ativa.

Regras:

- Tudo é **somente leitura**, exceto o CRUD de metas, que delega para ``database.py``.
- Dinheiro é somado e arredondado em ``Decimal`` (ROUND_HALF_UP) e trafega como string.
- Toda consulta é filtrada pela loja ativa da sessão (``loja_id = ? OR loja_id IS NULL``,
  a mesma convenção de ``RelatorioService.dados_dashboard``).
- As regras de faturamento espelham ``RelatorioService.dados_dashboard``: vendas não
  canceladas pelo valor líquido (``valor_total - desconto``) e serviços em status terminal
  pela data de conclusão (``servicos.data_conclusao``, migração v10).

As séries por período e por loja ainda não existem em ``services/*`` (``vendas_por_dia``,
``lucro_por_periodo`` e ``vendas_por_forma_pagamento`` são globais). Para não alterar regra de
negócio nesta etapa, as agregações por loja ficam aqui, em SQL de leitura. Mover para
``RelatorioService`` é a dívida registrada em ``docs/PLANO_MIGRACAO_UI_V2.md``.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Optional

from bridge import BridgeError

if TYPE_CHECKING:  # pragma: no cover
    from bridge import EstokeBridge

logger = logging.getLogger("EstokeAoCubo.bridge.dashboard")

CENTAVOS = Decimal("0.01")
PERIODOS = ("hoje", "7d", "30d", "mes", "ano")
PERIODO_PADRAO = "30d"
STATUS_META_PENDENTE = "Pendente"
STATUS_META_CONCLUIDA = "Concluída"
STATUS_OUTBOX_ABERTOS = ("pendente", "processando", "falha_temporaria")
STATUS_OUTBOX_FALHA = ("falha",)
TITULO_META_MAX = 120
DIAS_ALERTA_VALIDADE = 7
LIMITE_LISTA_ALERTA = 50
TOP_CATEGORIAS = 6

TICK_INTERVALO_S = 60.0
ATRASO_ALERTA_INICIAL_S = 2.0

# Serviço concluído: mesmo critério de RelatorioService.dados_dashboard (data_conclusao v10,
# com fallback para o histórico de status e para a data de abertura das OS legadas).
_SQL_DATA_CONCLUSAO = """date(COALESCE(
    s.data_conclusao,
    (SELECT MIN(h.data_hora)
       FROM historico h
      WHERE h.acao = 'OS Status'
        AND h.detalhe IN (
            'Serviço #' || s.id || ' -> Concluído',
            'Serviço #' || s.id || ' -> Entregue',
            'Serviço #' || s.id || ' -> Finalizado'
        )),
    s.data
))"""
_SQL_SERVICO_CONCLUIDO = (
    "COALESCE(s.status, 'Concluído') != 'Cancelado' "
    "AND COALESCE(s.status_os, 'Concluído') IN ('Concluído', 'Entregue', 'Finalizado', '')"
)


def _dec(valor: Any) -> Decimal:
    """Converte valor vindo do SQLite (float/int/str/None) em Decimal com 2 casas."""
    if valor is None or valor == "":
        return Decimal("0.00")
    try:
        d = Decimal(str(valor))
    except Exception:
        return Decimal("0.00")
    if not d.is_finite():
        return Decimal("0.00")
    return d.quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def _variacao_pct(atual: Decimal, anterior: Decimal) -> Optional[str]:
    """Variação percentual com 1 casa; None quando não há base de comparação."""
    if anterior <= 0:
        return None
    pct = (atual - anterior) / anterior * Decimal(100)
    return str(pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


class _Janela:
    """Intervalo [inicio, fim] em datas e a granularidade da série temporal."""

    def __init__(self, periodo: str, hoje: _dt.date) -> None:
        self.periodo = periodo
        if periodo == "hoje":
            self.inicio, self.fim, self.passo = hoje, hoje, "hora"
        elif periodo == "7d":
            self.inicio, self.fim, self.passo = hoje - _dt.timedelta(days=6), hoje, "dia"
        elif periodo == "30d":
            self.inicio, self.fim, self.passo = hoje - _dt.timedelta(days=29), hoje, "dia"
        elif periodo == "mes":
            self.inicio, self.fim, self.passo = hoje.replace(day=1), hoje, "dia"
        else:  # ano
            self.inicio, self.fim, self.passo = hoje.replace(month=1, day=1), hoje, "mes"

    def anterior(self) -> tuple[_dt.date, _dt.date]:
        """Janela imediatamente anterior, com o mesmo número de dias."""
        dias = (self.fim - self.inicio).days + 1
        fim_ant = self.inicio - _dt.timedelta(days=1)
        return fim_ant - _dt.timedelta(days=dias - 1), fim_ant

    def rotulos(self) -> list[str]:
        if self.passo == "hora":
            return [f"{h:02d}" for h in range(24)]
        if self.passo == "mes":
            return [f"{self.inicio.year:04d}-{m:02d}" for m in range(1, self.fim.month + 1)]
        dias = (self.fim - self.inicio).days + 1
        return [(self.inicio + _dt.timedelta(days=i)).isoformat() for i in range(dias)]


def _validar_periodo(periodo: Any) -> str:
    p = str(periodo or PERIODO_PADRAO).strip().lower()
    if p not in PERIODOS:
        raise BridgeError("INVALID_PERIOD", f"Período inválido. Use: {', '.join(PERIODOS)}.")
    return p


def _validar_id(valor: Any, nome: str = "id") -> int:
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise BridgeError("INVALID_ARGUMENT", f"Parâmetro {nome} inválido.") from None
    if n <= 0:
        raise BridgeError("INVALID_ARGUMENT", f"Parâmetro {nome} inválido.")
    return n


class DashboardBridge:
    def __init__(self, root: "EstokeBridge") -> None:
        self._root = root
        self._monitor_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._update_info: Optional[dict] = None

    # ------------------------------------------------------------------
    # Contexto
    # ------------------------------------------------------------------
    def _contexto(self) -> tuple[dict, int]:
        user = self._root._session.require_user()
        loja_id = self._root._session.loja_ativa_id or int(user.get("loja_id") or 1)
        return user, int(loja_id)

    def _hoje(self) -> _dt.date:
        return _dt.date.today()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        db = self._root._db
        lock = getattr(db, "_lock", None)
        if lock is None:
            cur = db.conn.cursor()
            cur.execute(sql, params)
            return cur.fetchall()
        with lock:
            cur = db.conn.cursor()
            cur.execute(sql, params)
            return cur.fetchall()

    # ------------------------------------------------------------------
    # Agregações por loja (somente leitura)
    # ------------------------------------------------------------------
    def _totais_vendas(self, loja_id: int, ini: _dt.date, fim: _dt.date) -> dict:
        rows = self._query(
            """SELECT COALESCE(SUM(valor_total - COALESCE(desconto, 0)), 0),
                      COALESCE(SUM(lucro), 0),
                      COUNT(DISTINCT COALESCE(NULLIF(transacao_id, ''), 'v' || id))
                 FROM vendas
                WHERE status != 'Cancelada'
                  AND date(data_venda) BETWEEN date(?) AND date(?)
                  AND (loja_id = ? OR loja_id IS NULL)""",
            (ini.isoformat(), fim.isoformat(), loja_id),
        )
        fat, lucro, qtd = rows[0] if rows else (0, 0, 0)
        return {"faturamento": _dec(fat), "lucro": _dec(lucro), "qtd": int(qtd or 0)}

    def _totais_servicos(self, loja_id: int, ini: _dt.date, fim: _dt.date) -> dict:
        rows = self._query(
            f"""SELECT COALESCE(SUM(s.valor), 0),
                       COALESCE(SUM(s.valor - COALESCE(s.custo, 0)), 0),
                       COUNT(*)
                  FROM servicos s
                 WHERE {_SQL_SERVICO_CONCLUIDO}
                   AND {_SQL_DATA_CONCLUSAO} BETWEEN date(?) AND date(?)
                   AND (s.loja_id = ? OR s.loja_id IS NULL)""",
            (ini.isoformat(), fim.isoformat(), loja_id),
        )
        valor, lucro, qtd = rows[0] if rows else (0, 0, 0)
        return {"faturamento": _dec(valor), "lucro": _dec(lucro), "qtd": int(qtd or 0)}

    def _total_gastos(self, loja_id: int, ini: _dt.date, fim: _dt.date) -> Decimal:
        rows = self._query(
            """SELECT COALESCE(SUM(valor), 0) FROM gastos
                WHERE date(data) BETWEEN date(?) AND date(?)
                  AND (loja_id = ? OR loja_id IS NULL)""",
            (ini.isoformat(), fim.isoformat(), loja_id),
        )
        return _dec(rows[0][0] if rows else 0)

    def _ids_produtos_loja(self, loja_id: int) -> set[int]:
        rows = self._query(
            "SELECT id FROM produtos WHERE loja_id = ? OR loja_id IS NULL", (loja_id,)
        )
        return {int(r[0]) for r in rows}

    def _status_caixa(self, user: dict, loja_id: int) -> dict:
        from services.caixa_service import CaixaService

        try:
            resumo = CaixaService(self._root._db, user).resumo_caixa(
                usuario_id=user.get("id"), loja_id=loja_id
            )
        except Exception:
            logger.debug("Falha ao consultar o caixa.", exc_info=True)
            resumo = None
        if not resumo:
            return {"aberto": False}
        return {
            "aberto": True,
            "caixa_id": resumo.get("caixa_id") or resumo.get("id"),
            "data_abertura": resumo.get("data_abertura"),
        }

    def _outbox_nfce(self) -> dict:
        from services.outbox_service import OutboxService

        svc = OutboxService(self._root._db, handlers={})
        pendentes = falhas = 0
        for status in STATUS_OUTBOX_ABERTOS + STATUS_OUTBOX_FALHA:
            try:
                itens = svc.listar_itens(status=status, limit=1000)
            except Exception:
                logger.debug("Falha ao listar outbox (%s).", status, exc_info=True)
                continue
            n = sum(1 for i in itens if str(i.get("tipo") or "").startswith("nfce"))
            if status in STATUS_OUTBOX_FALHA:
                falhas += n
            else:
                pendentes += n
        return {"pendentes": pendentes, "falhas": falhas, "total": pendentes + falhas}

    # ------------------------------------------------------------------
    # API pública (via EstokeBridge)
    # ------------------------------------------------------------------
    def get_resumo(self, periodo: Any = PERIODO_PADRAO) -> dict:
        user, loja_id = self._contexto()
        p = _validar_periodo(periodo)
        jan = _Janela(p, self._hoje())
        ini_ant, fim_ant = jan.anterior()

        v = self._totais_vendas(loja_id, jan.inicio, jan.fim)
        s = self._totais_servicos(loja_id, jan.inicio, jan.fim)
        gastos = self._total_gastos(loja_id, jan.inicio, jan.fim)
        v_ant = self._totais_vendas(loja_id, ini_ant, fim_ant)
        s_ant = self._totais_servicos(loja_id, ini_ant, fim_ant)

        faturamento = v["faturamento"] + s["faturamento"]
        faturamento_ant = v_ant["faturamento"] + s_ant["faturamento"]
        lucro_bruto = v["lucro"] + s["lucro"]
        ticket = (
            (v["faturamento"] / v["qtd"]).quantize(CENTAVOS, rounding=ROUND_HALF_UP)
            if v["qtd"] > 0
            else Decimal("0.00")
        )

        # KPIs "de hoje" do v1 (mesma fonte do dashboard clássico) para conferência.
        from services.relatorio_service import RelatorioService

        try:
            v1 = RelatorioService(self._root._db).dados_dashboard(loja_id=loja_id)
        except Exception:
            logger.debug("dados_dashboard indisponível.", exc_info=True)
            v1 = {}

        return {
            "periodo": p,
            "inicio": jan.inicio.isoformat(),
            "fim": jan.fim.isoformat(),
            "loja_id": loja_id,
            "faturamento": faturamento,
            "faturamento_vendas": v["faturamento"],
            "faturamento_servicos": s["faturamento"],
            "faturamento_anterior": faturamento_ant,
            "variacao_pct": _variacao_pct(faturamento, faturamento_ant),
            "lucro_bruto": lucro_bruto,
            "gastos": gastos,
            "resultado": lucro_bruto - gastos,
            "qtd_vendas": v["qtd"],
            "qtd_vendas_anterior": v_ant["qtd"],
            "servicos_concluidos": s["qtd"],
            "ticket_medio": ticket,
            "faturamento_hoje_v1": _dec(v1.get("faturamento_hoje")),
            "fiado_pendente": _dec(v1.get("fiado_pendente")),
            "caixa": self._status_caixa(user, loja_id),
            "outbox_nfce": self._outbox_nfce(),
            "vazio": faturamento == 0 and gastos == 0 and v["qtd"] == 0 and s["qtd"] == 0,
        }

    def get_graficos(self, periodo: Any = PERIODO_PADRAO) -> dict:
        _user, loja_id = self._contexto()
        p = _validar_periodo(periodo)
        jan = _Janela(p, self._hoje())
        ini, fim = jan.inicio.isoformat(), jan.fim.isoformat()

        if jan.passo == "hora":
            chave_v, chave_s = "strftime('%H', data_venda)", "strftime('%H', COALESCE(s.data_conclusao, s.data))"
        elif jan.passo == "mes":
            chave_v, chave_s = "strftime('%Y-%m', data_venda)", f"strftime('%Y-%m', {_SQL_DATA_CONCLUSAO})"
        else:
            chave_v, chave_s = "date(data_venda)", _SQL_DATA_CONCLUSAO

        rot = jan.rotulos()
        fat = {k: Decimal("0.00") for k in rot}
        luc = {k: Decimal("0.00") for k in rot}
        for chave, f, l in self._query(
            f"""SELECT {chave_v}, SUM(valor_total - COALESCE(desconto, 0)), SUM(lucro)
                  FROM vendas
                 WHERE status != 'Cancelada'
                   AND date(data_venda) BETWEEN date(?) AND date(?)
                   AND (loja_id = ? OR loja_id IS NULL)
                 GROUP BY 1""",
            (ini, fim, loja_id),
        ):
            if chave in fat:
                fat[chave] += _dec(f)
                luc[chave] += _dec(l)
        for chave, f, l in self._query(
            f"""SELECT {chave_s}, SUM(s.valor), SUM(s.valor - COALESCE(s.custo, 0))
                  FROM servicos s
                 WHERE {_SQL_SERVICO_CONCLUIDO}
                   AND {_SQL_DATA_CONCLUSAO} BETWEEN date(?) AND date(?)
                   AND (s.loja_id = ? OR s.loja_id IS NULL)
                 GROUP BY 1""",
            (ini, fim, loja_id),
        ):
            if chave in fat:
                fat[chave] += _dec(f)
                luc[chave] += _dec(l)

        pagamentos = [
            {"rotulo": str(forma or "Não informado"), "valor": _dec(total), "qtd": int(qtd or 0)}
            for forma, total, qtd in self._query(
                """SELECT COALESCE(NULLIF(forma_pagamento, ''), 'Dinheiro'),
                          SUM(valor_total - COALESCE(desconto, 0)),
                          COUNT(DISTINCT COALESCE(NULLIF(transacao_id, ''), 'v' || id))
                     FROM vendas
                    WHERE status != 'Cancelada'
                      AND date(data_venda) BETWEEN date(?) AND date(?)
                      AND (loja_id = ? OR loja_id IS NULL)
                    GROUP BY 1 ORDER BY 2 DESC""",
                (ini, fim, loja_id),
            )
            if _dec(total) > 0
        ]

        categorias_raw = [
            (str(cat or "Sem categoria"), _dec(total))
            for cat, total in self._query(
                """SELECT COALESCE(NULLIF(p.categoria, ''), 'Sem categoria'),
                          SUM(v.valor_total - COALESCE(v.desconto, 0))
                     FROM vendas v
                     LEFT JOIN produtos p ON p.id = v.produto_id
                    WHERE v.status != 'Cancelada'
                      AND date(v.data_venda) BETWEEN date(?) AND date(?)
                      AND (v.loja_id = ? OR v.loja_id IS NULL)
                    GROUP BY 1 ORDER BY 2 DESC""",
                (ini, fim, loja_id),
            )
        ]
        categorias_raw = [c for c in categorias_raw if c[1] > 0]
        categorias = [{"rotulo": r, "valor": v} for r, v in categorias_raw[:TOP_CATEGORIAS]]
        resto = sum((v for _, v in categorias_raw[TOP_CATEGORIAS:]), Decimal("0.00"))
        if resto > 0:
            categorias.append({"rotulo": "Outras", "valor": resto})

        gastos_cat = [
            {"rotulo": str(cat or "Sem categoria"), "valor": _dec(total)}
            for cat, total in self._query(
                """SELECT COALESCE(NULLIF(categoria, ''), 'Sem categoria'), SUM(valor)
                     FROM gastos
                    WHERE date(data) BETWEEN date(?) AND date(?)
                      AND (loja_id = ? OR loja_id IS NULL)
                    GROUP BY 1 ORDER BY 2 DESC""",
                (ini, fim, loja_id),
            )
            if _dec(total) > 0
        ]

        return {
            "periodo": p,
            "loja_id": loja_id,
            "passo": jan.passo,
            "serie": {
                "rotulos": rot,
                "faturamento": [fat[k] for k in rot],
                "lucro": [luc[k] for k in rot],
            },
            "pagamentos": pagamentos,
            "categorias": categorias,
            "gastos_categorias": gastos_cat,
            "vazio": all(x == 0 for x in fat.values()) and not gastos_cat,
        }

    # -- metas -----------------------------------------------------------
    def list_metas(self) -> list[dict]:
        self._root._session.require_user()
        df = self._root._db.listar_metas()
        registros = df.to_dict(orient="records") if hasattr(df, "to_dict") else list(df or [])
        metas = []
        for r in registros:
            status = str(r.get("status") or STATUS_META_PENDENTE)
            prazo = r.get("prazo")
            metas.append(
                {
                    "id": int(r["id"]),
                    "titulo": str(r.get("titulo") or ""),
                    "descricao": str(r.get("descricao") or ""),
                    "status": status,
                    "prazo": str(prazo) if prazo not in (None, "") and prazo == prazo else None,
                    "concluida": status == STATUS_META_CONCLUIDA,
                }
            )
        metas.sort(key=lambda m: (m["concluida"], m["prazo"] or "9999-12-31", m["id"]))
        return metas

    def _meta(self, meta_id: int) -> Optional[dict]:
        return next((m for m in self.list_metas() if m["id"] == meta_id), None)

    def create_meta(self, titulo: Any, prazo: Any = None, descricao: Any = "") -> dict:
        self._root._session.require_user()
        t = " ".join(str(titulo or "").split())
        if not t:
            raise BridgeError("INVALID_ARGUMENT", "Informe o título da meta.")
        if len(t) > TITULO_META_MAX:
            raise BridgeError("INVALID_ARGUMENT", f"O título pode ter até {TITULO_META_MAX} caracteres.")
        prazo_iso = None
        if prazo not in (None, ""):
            try:
                prazo_iso = _dt.date.fromisoformat(str(prazo)[:10]).isoformat()
            except ValueError:
                raise BridgeError("INVALID_ARGUMENT", "Prazo inválido (use AAAA-MM-DD).") from None
        self._root._db.adicionar_meta(t, str(descricao or "").strip(), STATUS_META_PENDENTE, prazo_iso)
        nova = max((m for m in self.list_metas() if m["titulo"] == t), key=lambda m: m["id"], default=None)
        self._root._events.emit("metas_changed", {"acao": "criada"})
        return nova or {}

    def toggle_meta(self, meta_id: Any) -> dict:
        self._root._session.require_user()
        mid = _validar_id(meta_id, "meta_id")
        meta = self._meta(mid)
        if meta is None:
            raise BridgeError("NOT_FOUND", "Meta não encontrada.")
        novo = STATUS_META_PENDENTE if meta["concluida"] else STATUS_META_CONCLUIDA
        self._root._db.atualizar_status_meta(mid, novo)
        self._root._events.emit("metas_changed", {"acao": "status", "id": mid})
        return self._meta(mid) or {}

    def delete_meta(self, meta_id: Any) -> dict:
        self._root._session.require_user()
        mid = _validar_id(meta_id, "meta_id")
        if self._meta(mid) is None:
            raise BridgeError("NOT_FOUND", "Meta não encontrada.")
        self._root._db.remover_meta(mid)
        self._root._events.emit("metas_changed", {"acao": "removida", "id": mid})
        return {"id": mid, "removida": True}

    # -- alertas ---------------------------------------------------------
    def get_alertas(self) -> dict:
        _user, loja_id = self._contexto()
        db = self._root._db
        hoje_iso = self._hoje().isoformat()
        ids_loja = self._ids_produtos_loja(loja_id)

        validade: list[dict] = []
        try:
            df = db.verificar_produtos_vencendo(dias_alerta=DIAS_ALERTA_VALIDADE)
            for r in (df.to_dict(orient="records") if df is not None else []):
                if int(r["id"]) not in ids_loja:
                    continue
                data_val = str(r.get("data_validade") or "")[:10]
                try:
                    dias = (_dt.date.fromisoformat(data_val) - self._hoje()).days
                except ValueError:
                    dias = None
                validade.append(
                    {
                        "id": int(r["id"]),
                        "nome": str(r.get("nome") or ""),
                        "quantidade": r.get("quantidade"),
                        "data_validade": data_val,
                        "dias": dias,
                        "vencido": data_val < hoje_iso,
                    }
                )
        except Exception:
            logger.debug("Falha ao consultar validade.", exc_info=True)

        estoque: list[dict] = []
        try:
            df = db.produtos_abaixo_estoque_minimo()
            for r in (df.to_dict(orient="records") if df is not None else []):
                if int(r["id"]) not in ids_loja:
                    continue
                estoque.append(
                    {
                        "id": int(r["id"]),
                        "nome": str(r.get("nome") or ""),
                        "quantidade": r.get("quantidade"),
                        "estoque_minimo": r.get("estoque_minimo"),
                    }
                )
        except Exception:
            logger.debug("Falha ao consultar estoque mínimo.", exc_info=True)

        return {
            "loja_id": loja_id,
            "validade": validade[:LIMITE_LISTA_ALERTA],
            "validade_total": len(validade),
            "vencidos_total": sum(1 for x in validade if x["vencido"]),
            "estoque_baixo": estoque[:LIMITE_LISTA_ALERTA],
            "estoque_baixo_total": len(estoque),
            "outbox_nfce": self._outbox_nfce(),
            "update": dict(self._update_info) if self._update_info else None,
        }

    # ------------------------------------------------------------------
    # Eventos de fundo (não expostos ao JS)
    # ------------------------------------------------------------------
    def _emitir_alertas_iniciais(self) -> Optional[dict]:
        """Resumo não-bloqueante pós-login: só emite se houver algo a avisar."""
        try:
            alertas = self.get_alertas()
        except BridgeError:
            return None
        resumo = {
            "validade": alertas["validade_total"],
            "vencidos": alertas["vencidos_total"],
            "estoque_baixo": alertas["estoque_baixo_total"],
            "outbox_nfce": alertas["outbox_nfce"]["total"],
        }
        if any(resumo.values()):
            self._root._events.emit("dashboard:alertas_iniciais", resumo)
        return resumo

    def _verificar_update(self) -> Optional[dict]:
        try:
            info = self._root._app.check_updates_status()
        except Exception:
            logger.debug("Falha ao verificar atualização.", exc_info=True)
            return None
        self._update_info = info
        if info.get("available"):
            self._root._events.emit(
                "update:disponivel",
                {"version": info.get("version"), "current_version": info.get("current_version")},
            )
        return info

    def _emitir_tick(self) -> None:
        self._root._events.emit("dashboard:tick", {"ts": _dt.datetime.now().isoformat(timespec="seconds")})

    def _on_login(self) -> None:
        """Inicia (uma vez) o monitor de fundo, só com janela real ligada."""
        if self._root._window is None:
            return
        with self._monitor_lock:
            if self._monitor_thread is not None:
                return
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="EstokeDashboardMonitor", daemon=True
            )
            self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        if self._stop.wait(ATRASO_ALERTA_INICIAL_S):
            return
        self._emitir_alertas_iniciais()
        self._verificar_update()
        while not self._stop.wait(TICK_INTERVALO_S):
            if self._root._session.user is None:
                continue
            self._emitir_tick()

    def _shutdown(self) -> None:
        self._stop.set()
        thread = self._monitor_thread
        if thread is not None:
            thread.join(timeout=1.0)
