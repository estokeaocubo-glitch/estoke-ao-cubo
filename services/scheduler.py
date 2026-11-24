# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Agendador de Rotinas Operacionais
Loop leve (disparado via App.after(60000) em thread daemon) que verifica a tabela
`rotinas` e enfileira alertas e cobranças de forma idempotente na tabela `outbox`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

logger = logging.getLogger("EstokeAoCubo.scheduler")


def _parse_date_flex(value: str | None) -> dt.date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw10 = raw[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(raw10, fmt).date()
        except ValueError:
            continue
    return None


class RoutineScheduler:
    """Executor idempotente de rotinas diárias conectadas à fila Outbox."""

    def __init__(self, db: Any, outbox_service: Any) -> None:
        self.db = db
        self.outbox = outbox_service

    def _obter_telefone_lojista(self, cfg_rotina: dict[str, Any]) -> str:
        tel = str(cfg_rotina.get("telefone_destino") or cfg_rotina.get("telefone") or "").strip()
        if tel:
            return tel
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT empresa_telefone FROM config_nota ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
        return ""

    def _obter_nome_empresa(self) -> str:
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT empresa_nome FROM config_nota ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
        return "Estoke Ao Cubo"

    def listar_rotinas(self) -> list[dict[str, Any]]:
        with self.db._lock:
            rows = self.db.conn.execute(
                "SELECT id, tipo, hora, ultimo_run, ativo, config_json FROM rotinas ORDER BY id ASC"
            ).fetchall()
        out = []
        for r in rows:
            try:
                cfg = json.loads(r[5]) if r[5] else {}
            except Exception:
                cfg = {}
            out.append(
                {
                    "id": r[0],
                    "tipo": r[1],
                    "hora": r[2],
                    "ultimo_run": r[3] or "",
                    "ativo": bool(r[4]),
                    "config": cfg,
                }
            )
        return out

    def atualizar_rotina(self, tipo: str, hora: str | None = None, ativo: bool | None = None, config: dict | None = None) -> bool:
        sets = []
        params: list[Any] = []
        if hora is not None:
            sets.append("hora = ?")
            params.append(str(hora).strip()[:5])
        if ativo is not None:
            sets.append("ativo = ?")
            params.append(1 if ativo else 0)
        if config is not None:
            sets.append("config_json = ?")
            params.append(json.dumps(config, ensure_ascii=False))
        if not sets:
            return False
        params.append(tipo)
        with self.db._lock:
            cur = self.db.conn.execute(f"UPDATE rotinas SET {', '.join(sets)} WHERE tipo = ?", params)
            self.db.conn.commit()
            return cur.rowcount > 0

    def tick(self, now_dt: dt.datetime | None = None) -> dict[str, Any]:
        """
        Avalia todas as rotinas ativas cujo horário já foi atingido no dia
        e ainda não rodaram hoje (`ultimo_run` != YYYY-MM-DD).
        """
        ref = now_dt or dt.datetime.now()
        hoje_str = ref.strftime("%Y-%m-%d")
        hora_atual = ref.strftime("%H:%M")

        resumo = {"executadas": [], "mensagens_enfileiradas": 0}

        with self.db._lock:
            rotinas = self.db.conn.execute(
                "SELECT id, tipo, hora, ultimo_run, ativo, config_json FROM rotinas WHERE ativo = 1"
            ).fetchall()

        for row in rotinas:
            rotina_id, tipo, hora_alvo, ultimo_run, _ativo, raw_cfg = row
            ultimo_data = str(ultimo_run or "").strip()[:10]
            if ultimo_data == hoje_str:
                continue
            if hora_atual < str(hora_alvo or "00:00")[:5]:
                continue

            try:
                cfg = json.loads(raw_cfg) if raw_cfg else {}
            except Exception:
                cfg = {}

            qtd_msgs = 0
            try:
                if tipo == "resumo_diario_caixa":
                    qtd_msgs = self._executar_resumo_diario(ref, cfg)
                elif tipo == "cobranca_fiado_vencido":
                    qtd_msgs = self._executar_cobranca_fiado(ref, cfg)
                elif tipo == "alerta_lotes_vencendo":
                    qtd_msgs = self._executar_alerta_lotes_vencendo(ref, cfg)
                else:
                    logger.warning("Tipo de rotina desconhecido: %s", tipo)
                    continue

                with self.db._lock:
                    self.db.conn.execute(
                        "UPDATE rotinas SET ultimo_run = ? WHERE id = ?",
                        (hoje_str, int(rotina_id)),
                    )
                    self.db.conn.commit()

                resumo["executadas"].append(tipo)
                resumo["mensagens_enfileiradas"] += qtd_msgs
            except Exception as exc:
                logger.error("Erro ao executar rotina '%s': %s", tipo, exc)

        return resumo

    def _executar_resumo_diario(self, ref: dt.datetime, cfg: dict[str, Any]) -> int:
        hoje_str = ref.strftime("%Y-%m-%d")
        telefone = self._obter_telefone_lojista(cfg)
        if not telefone:
            logger.info("Rotina resumo_diario_caixa sem telefone do lojista configurado.")
            return 0

        empresa = self._obter_nome_empresa()
        with self.db._lock:
            row_v = self.db.conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(valor_total - COALESCE(desconto, 0)), 0),
                       COALESCE(SUM(lucro), 0),
                       COALESCE(SUM(valor_dinheiro), 0),
                       COALESCE(SUM(valor_pix), 0),
                       COALESCE(SUM(valor_cartao_debito + valor_cartao_credito), 0),
                       COALESCE(SUM(valor_fiado), 0)
                FROM vendas
                WHERE status = 'Concluída' AND substr(data_venda, 1, 10) = ?
                """,
                (hoje_str,),
            ).fetchone()
            row_g = self.db.conn.execute(
                "SELECT COALESCE(SUM(valor), 0) FROM gastos WHERE substr(data, 1, 10) = ?",
                (hoje_str,),
            ).fetchone()

        qtd_vendas = int(row_v[0] or 0)
        faturamento = float(row_v[1] or 0.0)
        lucro = float(row_v[2] or 0.0)
        din = float(row_v[3] or 0.0)
        pix = float(row_v[4] or 0.0)
        cartao = float(row_v[5] or 0.0)
        fiado = float(row_v[6] or 0.0)
        gastos = float((row_g[0] if row_g else 0.0) or 0.0)

        msg = (
            f"📊 *Resumo Diário — {empresa} ({ref.strftime('%d/%m/%Y')})*\n"
            f"• Vendas concluídas: {qtd_vendas}\n"
            f"• Faturamento líquido: R$ {faturamento:.2f}\n"
            f"• Lucro estimado: R$ {lucro:.2f}\n"
            f"• Dinheiro: R$ {din:.2f} | Pix: R$ {pix:.2f}\n"
            f"• Cartões: R$ {cartao:.2f} | Fiado: R$ {fiado:.2f}\n"
            f"• Despesas do dia: R$ {gastos:.2f}"
        )

        inserido, _ = self.outbox.enfileirar(
            "whatsapp",
            {
                "telefone": telefone,
                "mensagem": msg,
                "metadata": {"rotina": "resumo_diario_caixa", "data": hoje_str},
            },
            chave_idempotencia=f"resumo_diario:{hoje_str}",
        )
        return 1 if inserido else 0

    def _executar_cobranca_fiado(self, ref: dt.datetime, cfg: dict[str, Any]) -> int:
        hoje_date = ref.date()
        hoje_str = ref.strftime("%Y-%m-%d")
        dias_alvo = set(int(d) for d in (cfg.get("dias") or [1, 7, 15]))
        empresa = self._obter_nome_empresa()

        with self.db._lock:
            rows = self.db.conn.execute(
                """
                SELECT cr.id, cr.valor, cr.data_vencimento, cr.descricao,
                       COALESCE(c.nome, 'Cliente'), COALESCE(c.contato, '')
                FROM contas_receber cr
                LEFT JOIN clientes c ON cr.cliente_id = c.id
                WHERE cr.status = 'Pendente'
                  AND cr.data_vencimento IS NOT NULL
                  AND cr.data_vencimento != ''
                """
            ).fetchall()

        enfileirados = 0
        for r in rows:
            conta_id = int(r[0])
            valor = float(r[1] or 0.0)
            venc = _parse_date_flex(r[2])
            if venc is None or valor <= 0:
                continue
            dias_atraso = (hoje_date - venc).days
            if dias_atraso not in dias_alvo:
                continue
            contato = str(r[5] or "").strip()
            digitos = "".join(ch for ch in contato if ch.isdigit())
            if len(digitos) < 8:
                continue

            nome_cli = str(r[4] or "Cliente").strip()
            msg = (
                f"Olá {nome_cli}, tudo bem?\n"
                f"Lembrete amigável da *{empresa}*: identificamos um débito em aberto "
                f"de *R$ {valor:.2f}* com vencimento em {venc.strftime('%d/%m/%Y')} "
                f"({dias_atraso} dia(s) de atraso).\n"
                f"Se já realizou o pagamento, por favor desconsidere esta mensagem."
            )
            inserido, _ = self.outbox.enfileirar(
                "whatsapp",
                {
                    "telefone": contato,
                    "mensagem": msg,
                    "metadata": {
                        "rotina": "cobranca_fiado_vencido",
                        "conta_id": conta_id,
                        "dias_atraso": dias_atraso,
                    },
                },
                chave_idempotencia=f"fiado:{conta_id}:D+{dias_atraso}:{hoje_str}",
            )
            if inserido:
                enfileirados += 1
        return enfileirados

    def _executar_alerta_lotes_vencendo(self, ref: dt.datetime, cfg: dict[str, Any]) -> int:
        hoje_date = ref.date()
        hoje_str = ref.strftime("%Y-%m-%d")
        dias_antecedencia = max(1, int(cfg.get("dias_antecedencia", 7)))
        limite_date = hoje_date + dt.timedelta(days=dias_antecedencia)
        telefone = self._obter_telefone_lojista(cfg)
        if not telefone:
            logger.info("Rotina alerta_lotes_vencendo sem telefone do lojista configurado.")
            return 0

        empresa = self._obter_nome_empresa()
        with self.db._lock:
            rows = self.db.conn.execute(
                """
                SELECT id, nome, quantidade, data_validade, COALESCE(lote_serie, '')
                FROM produtos
                WHERE data_validade IS NOT NULL AND data_validade != '' AND quantidade > 0
                ORDER BY data_validade ASC
                """
            ).fetchall()

        proximos = []
        for r in rows:
            val_dt = _parse_date_flex(r[3])
            if val_dt is None:
                continue
            if val_dt <= limite_date:
                proximos.append((str(r[1]), float(r[2] or 0), val_dt, str(r[4] or "")))

        if not proximos:
            return 0

        linhas = []
        for nome, qtd, val_dt, lote in proximos[:15]:
            tag_lote = f" [Lote: {lote}]" if lote else ""
            status_val = "VENCIDO" if val_dt < hoje_date else f"vence {val_dt.strftime('%d/%m/%Y')}"
            linhas.append(f"• {nome}{tag_lote} — Qtd: {qtd:g} ({status_val})")

        extra = f"\n... e mais {len(proximos) - 15} item(ns)." if len(proximos) > 15 else ""
        msg = (
            f"⚠️ *Alerta de Validade — {empresa}*\n"
            f"{len(proximos)} produto(s)/lote(s) vencidos ou vencendo nos próximos {dias_antecedencia} dias:\n\n"
            + "\n".join(linhas)
            + extra
        )

        inserido, _ = self.outbox.enfileirar(
            "whatsapp",
            {
                "telefone": telefone,
                "mensagem": msg,
                "metadata": {"rotina": "alerta_lotes_vencendo", "qtd_itens": len(proximos)},
            },
            chave_idempotencia=f"alerta_lotes:{hoje_str}",
        )
        return 1 if inserido else 0
