# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Serviço de Sugestão Inteligente de Compras
Calcula giro de 30/60/90 dias, lead time por fornecedor/produto, estoque mínimo,
agrupamento por fornecedor, pedido formatado para WhatsApp e exportação em PDF.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger("EstokeAoCubo.compras")


def extract_phone_from_contact(contato: str) -> str:
    """Extrai telefone de string livre (ex: 'CNPJ: 00... | Tel: (11) 99999-8888')."""
    raw = str(contato or "")
    # Prioriza trecho após Tel/Whats/Fone se existir
    m = re.search(r"(?:tel|fone|whats(?:app)?)[^0-9]*([0-9()\s\-+]{8,20})", raw, flags=re.IGNORECASE)
    cand = m.group(1) if m else raw
    digits = re.sub(r"\D", "", cand)
    if len(digits) in (10, 11, 12, 13):
        return digits
    return ""


class ComprasService:
    """Motor analítico de reposição e planejamento de compras."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def calcular_sugestao_compras(
        self,
        janela_dias: int = 30,
        dias_cobertura: int = 15,
        lead_time_padrao: int = 7,
        apenas_necessarios: bool = True,
        now_date: dt.date | None = None,
    ) -> list[dict[str, Any]]:
        """
        Analisa giro de vendas nos últimos `janela_dias` (ex: 30, 60 ou 90),
        combina com `lead_time_dias` + `dias_cobertura` + `estoque_minimo`
        e retorna lista ordenada por urgência e necessidade de compra.
        """
        janela = max(1, int(janela_dias or 30))
        cobertura = max(1, int(dias_cobertura or 15))
        lt_default = max(1, int(lead_time_padrao or 7))
        ref_date = now_date or dt.date.today()
        corte_iso = (ref_date - dt.timedelta(days=janela)).isoformat()

        with self.db._lock:
            cur = self.db.conn.cursor()
            produtos = cur.execute(
                """
                SELECT p.id, p.nome, COALESCE(p.quantidade, 0),
                       COALESCE(p.estoque_minimo, 5), COALESCE(p.preco_compra, 0),
                       COALESCE(p.codigo_barras, ''), COALESCE(p.categoria, ''),
                       p.fornecedor_id, COALESCE(f.nome, 'Sem Fornecedor'),
                       COALESCE(f.contato, ''),
                       COALESCE(p.lead_time_dias, f.lead_time_dias, ?)
                FROM produtos p
                LEFT JOIN fornecedores f ON p.fornecedor_id = f.id
                WHERE COALESCE(p.tipo_produto, 'produto') != 'insumo'
                ORDER BY p.nome ASC
                """,
                (lt_default,),
            ).fetchall()

            vendas_rows = cur.execute(
                """
                SELECT produto_id, COALESCE(SUM(quantidade), 0)
                FROM vendas
                WHERE status = 'Concluída' AND substr(data_venda, 1, 10) >= ?
                GROUP BY produto_id
                """,
                (corte_iso,),
            ).fetchall()

        vendas_map = {int(r[0]): float(r[1] or 0) for r in vendas_rows if r[0] is not None}

        sugestoes: list[dict[str, Any]] = []
        for row in produtos:
            pid = int(row[0])
            nome = str(row[1])
            estoque_atual = float(row[2] or 0)
            estoque_min = max(0, int(row[3] or 0))
            preco_compra = float(row[4] or 0)
            ean = str(row[5] or "")
            categoria = str(row[6] or "")
            fornecedor_id = row[7]
            fornecedor_nome = str(row[8] or "Sem Fornecedor")
            fornecedor_contato = str(row[9] or "")
            lead_time = max(1, int(row[10] or lt_default))

            vendas_janela = float(vendas_map.get(pid, 0.0))
            giro_diario = vendas_janela / float(janela)
            demanda_ciclo = giro_diario * (lead_time + cobertura)
            estoque_alvo = max(estoque_min, math.ceil(demanda_ciclo + estoque_min))
            qtd_sugerida = max(0, int(math.ceil(estoque_alvo - estoque_atual)))

            if apenas_necessarios and qtd_sugerida <= 0:
                continue

            if giro_diario > 0:
                dias_restantes = round(max(0.0, estoque_atual) / giro_diario, 1)
            else:
                dias_restantes = 0.0 if estoque_atual <= 0 else 999.0

            if estoque_atual <= 0 or (giro_diario > 0 and dias_restantes <= lead_time):
                prioridade = "CRÍTICA"
                prio_ord = 0
            elif estoque_atual < estoque_min:
                prioridade = "ALTA"
                prio_ord = 1
            else:
                prioridade = "NORMAL"
                prio_ord = 2

            custo_estimado = round(qtd_sugerida * preco_compra, 2)
            sugestoes.append(
                {
                    "produto_id": pid,
                    "nome": nome,
                    "codigo_barras": ean,
                    "categoria": categoria,
                    "estoque_atual": int(estoque_atual) if estoque_atual.is_integer() else round(estoque_atual, 2),
                    "estoque_minimo": estoque_min,
                    "vendas_janela": round(vendas_janela, 2),
                    "janela_dias": janela,
                    "giro_diario": round(giro_diario, 3),
                    "lead_time_dias": lead_time,
                    "dias_cobertura": cobertura,
                    "dias_estoque_restante": dias_restantes,
                    "estoque_alvo": estoque_alvo,
                    "qtd_sugerida": qtd_sugerida,
                    "preco_compra": round(preco_compra, 2),
                    "custo_estimado": custo_estimado,
                    "fornecedor_id": fornecedor_id,
                    "fornecedor_nome": fornecedor_nome,
                    "fornecedor_contato": fornecedor_contato,
                    "fornecedor_telefone": extract_phone_from_contact(fornecedor_contato),
                    "prioridade": prioridade,
                    "_prio_ord": prio_ord,
                }
            )

        sugestoes.sort(key=lambda x: (x["_prio_ord"], -x["qtd_sugerida"], x["nome"]))
        for item in sugestoes:
            item.pop("_prio_ord", None)
        return sugestoes

    def agrupar_por_fornecedor(self, itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Agrupa itens sugeridos por fornecedor para cotação e pedido em lote."""
        grupos: dict[str, dict[str, Any]] = {}
        for it in itens:
            if int(it.get("qtd_sugerida", 0) or 0) <= 0:
                continue
            chave = str(it.get("fornecedor_id") or it.get("fornecedor_nome") or "Sem Fornecedor")
            if chave not in grupos:
                grupos[chave] = {
                    "fornecedor_id": it.get("fornecedor_id"),
                    "fornecedor_nome": it.get("fornecedor_nome") or "Sem Fornecedor",
                    "fornecedor_contato": it.get("fornecedor_contato") or "",
                    "fornecedor_telefone": it.get("fornecedor_telefone") or "",
                    "itens": [],
                    "total_unidades": 0,
                    "custo_total": 0.0,
                }
            g = grupos[chave]
            g["itens"].append(it)
            g["total_unidades"] += int(it.get("qtd_sugerida", 0))
            g["custo_total"] = round(g["custo_total"] + float(it.get("custo_estimado", 0.0)), 2)
        return sorted(grupos.values(), key=lambda x: (-x["custo_total"], x["fornecedor_nome"]))

    def formatar_pedido_whatsapp(self, grupo: dict[str, Any], empresa_nome: str = "Estoke Ao Cubo") -> str:
        """Gera texto pronto para envio de cotação/pedido de compra via WhatsApp."""
        forn = grupo.get("fornecedor_nome") or "Parceiro"
        linhas = [
            f"📦 *Pedido de Reposição — {empresa_nome}*",
            f"Fornecedor: *{forn}*",
            f"Data: {dt.date.today().strftime('%d/%m/%Y')}",
            "",
            "*Itens solicitados:*",
        ]
        for it in grupo.get("itens") or []:
            ean = f" (EAN: {it['codigo_barras']})" if it.get("codigo_barras") else ""
            linhas.append(f"• {it['qtd_sugerida']}x {it['nome']}{ean}")

        linhas.append("")
        linhas.append(f"Total de itens: {grupo.get('total_unidades', 0)} un.")
        if float(grupo.get("custo_total", 0) or 0) > 0:
            linhas.append(f"Estimativa de referência: R$ {grupo['custo_total']:.2f}")
        linhas.append("Favor confirmar disponibilidade e prazo de entrega. Obrigado!")
        return "\n".join(linhas)

    def exportar_pdf_compras(
        self,
        itens: list[dict[str, Any]],
        destino_pdf_path: str,
        janela_dias: int = 30,
        dias_cobertura: int = 15,
        empresa_nome: str = "Estoke Ao Cubo",
    ) -> tuple[bool, str]:
        """Exporta relatório tabular de sugestão de compras em PDF."""
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

            os.makedirs(os.path.dirname(os.path.abspath(destino_pdf_path)), exist_ok=True)
            doc = SimpleDocTemplate(destino_pdf_path, pagesize=landscape(A4), topMargin=28, bottomMargin=28)
            styles = getSampleStyleSheet()
            story = [
                Paragraph(f"Sugestão Inteligente de Compras — {empresa_nome}", styles["Title"]),
                Paragraph(
                    f"Base de giro: últimos {janela_dias} dias | Cobertura desejada: {dias_cobertura} dias | "
                    f"Gerado em {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}",
                    styles["Normal"],
                ),
                Spacer(1, 12),
            ]

            dados = [
                [
                    "Prioridade",
                    "Produto",
                    "Fornecedor",
                    "Atual",
                    "Mín.",
                    "Giro/dia",
                    "Lead(d)",
                    "Comprar",
                    "Custo Est.",
                ]
            ]
            custo_global = 0.0
            for it in itens:
                custo_global += float(it.get("custo_estimado", 0.0) or 0.0)
                dados.append(
                    [
                        it.get("prioridade", "NORMAL"),
                        str(it.get("nome", ""))[:36],
                        str(it.get("fornecedor_nome", ""))[:24],
                        str(it.get("estoque_atual", 0)),
                        str(it.get("estoque_minimo", 0)),
                        f"{float(it.get('giro_diario', 0)):.2f}",
                        str(it.get("lead_time_dias", 7)),
                        str(it.get("qtd_sugerida", 0)),
                        f"R$ {float(it.get('custo_estimado', 0)):.2f}",
                    ]
                )
            dados.append(["", "TOTAL ESTIMADO DO PEDIDO", "", "", "", "", "", str(sum(int(i.get("qtd_sugerida", 0)) for i in itens)), f"R$ {custo_global:.2f}"])

            table = Table(dados, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
                        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
                    ]
                )
            )
            story.append(table)
            doc.build(story)
            return True, destino_pdf_path
        except Exception as exc:
            logger.error("Erro ao gerar PDF de compras: %s", exc)
            return False, str(exc)
