"""
Serviço de relatórios, séries do dashboard, previsão IA, gestão/cobrança PIX de fiados,
verificação de auditoria SHA-256 e acionamento do pacote mensal do contador.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

import ai_module
import pandas as pd
from printer import gerar_payload_pix

logger = logging.getLogger("EstokeAoCubo.services.relatorio")


class RelatorioService:
    """
    Lógica de negócio e agregação de séries/KPIs para relatórios e dashboard.
    Nunca constrói figuras Matplotlib nem importa bibliotecas de UI.
    """

    def __init__(
        self,
        db: Any,
        ai_predictor: Any | None = None,
        mp_service: Any | None = None,
        outbox_service: Any | None = None,
    ):
        self.db = db
        self.ai_predictor = ai_predictor
        self.mp_service = mp_service
        self.outbox_service = outbox_service

    # ------------------------------------------------------------------
    # Dashboard & Previsão IA
    # ------------------------------------------------------------------

    def previsao_vendas(self, treinar: bool = True) -> dict[str, Any]:
        """
        Executa a previsão do próximo mês delegando ao ExpensePredictor do ai_module.
        (Mantém nome do contrato de domínio 'previsao_vendas' preservando o cálculo atual).
        """
        predictor = (
            self.ai_predictor
            if self.ai_predictor is not None
            else ai_module.ExpensePredictor(self.db)
        )
        try:
            treinado = bool(predictor.train()) if treinar else True
            valor = float(predictor.predict_next_month() or 0.0)
            return {
                "treinado": treinado,
                "valor": valor,
                "texto": f"R$ {valor:.2f}",
                "detalhes": "Baseado no histórico de gastos (IA)",
            }
        except Exception as exc:
            logger.warning("Falha ao calcular previsão IA: %s", exc)
            return {
                "treinado": False,
                "valor": 0.0,
                "texto": "R$ 0.00",
                "detalhes": "Dados insuficientes para previsão.",
            }

    def dados_dashboard(self, loja_id: int | None = None) -> dict[str, Any]:
        """
        Retorna todas as séries e KPIs puros do dashboard principal e da aba
        'Dashboard & IA' de relatórios (sem construir figuras gráficas).

        KPI 'Faturamento Hoje': como a tabela entradas não possui coluna de origem/tipo,
        o faturamento líquido diário soma vendas (exceto canceladas) + serviços concluídos
        do dia em Decimal, e a contagem vem diretamente da tabela vendas (exceto canceladas).
        """
        from decimal import Decimal, ROUND_HALF_UP

        # 1. Série do gráfico de pizza: Top 5 produtos por quantidade + 'Outros'
        df_produtos = self.db.listar_produtos()
        if df_produtos is not None and not df_produtos.empty:
            df_top = df_produtos.nlargest(5, "quantidade")
            labels = df_top["nome"].tolist()
            sizes = [int(x) for x in df_top["quantidade"].tolist()]
            outros = int(df_produtos["quantidade"].sum() - df_top["quantidade"].sum())
            if outros > 0:
                labels.append("Outros")
                sizes.append(outros)
        else:
            labels = ["Sem Dados"]
            sizes = [1]

        # 2. Série do gráfico de barras: Gastos agrupados por categoria
        df_gastos = self.db.listar_gastos(loja_id=loja_id)
        if df_gastos is not None and not df_gastos.empty:
            gastos_por_cat = df_gastos.groupby("categoria")["valor"].sum()
            cats = gastos_por_cat.index.tolist()
            vals = [float(x) for x in gastos_por_cat.values.tolist()]
        else:
            cats = ["Sem Gastos"]
            vals = [0]

        # 3. KPIs de hoje: vendas não canceladas (valor líquido) + serviços concluídos
        today_str = self._hoje_iso()
        sales_today = 0.0
        count_sales_today = 0
        loja_clause = " AND (loja_id = ? OR loja_id IS NULL)" if loja_id is not None else ""
        params_hoje = (today_str, loja_id) if loja_id is not None else (today_str,)
        try:
            with self.db._lock:
                cursor = self.db.conn.cursor()
                cursor.execute(
                    f"""SELECT COALESCE(SUM(valor_total - COALESCE(desconto, 0)), 0), COUNT(*)
                        FROM vendas
                        WHERE status != 'Cancelada' AND date(data_venda) = date(?){loja_clause}""",
                    params_hoje,
                )
                row_v = cursor.fetchone()
                vendas_hoje_d = Decimal(str((row_v[0] if row_v else 0) or 0))
                count_sales_today = int((row_v[1] if row_v else 0) or 0)

                cursor.execute(
                    f"""SELECT COALESCE(SUM(valor), 0)
                        FROM servicos
                        WHERE date(data) = date(?)
                          AND COALESCE(status, 'Concluído') != 'Cancelado'
                          AND COALESCE(status_os, 'Concluído') IN ('Concluído', 'Entregue', 'Finalizado', ''){loja_clause}""",
                    params_hoje,
                )
                row_s = cursor.fetchone()
                servicos_hoje_d = Decimal(str((row_s[0] if row_s else 0) or 0))

                sales_today = float(
                    (vendas_hoje_d + servicos_hoje_d).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
        except Exception as exc:
            logger.warning("Erro ao consultar KPIs diários do dashboard: %s", exc)

        # 4. Lucro do mês, fiados pendentes e comparativo mensal
        try:
            lucro_mes, fat_mes = self.db.lucro_por_periodo("mes")
            lucro_mes = float(lucro_mes)
            fat_mes = float(fat_mes)
        except Exception:
            lucro_mes, fat_mes = 0.0, 0.0

        try:
            fiado_pend = float(self.db.total_fiado_pendente())
        except Exception:
            fiado_pend = 0.0

        try:
            comp = self.db.comparativo_mensal()
            fat_atual = float(comp["atual"]["faturamento"])
            fat_ant = float(comp["anterior"]["faturamento"])
            variacao_pct = ((fat_atual - fat_ant) / fat_ant * 100.0) if fat_ant > 0 else 0.0
        except Exception:
            comp = {"atual": {"faturamento": 0.0}, "anterior": {"faturamento": 0.0}}
            fat_atual = fat_ant = variacao_pct = 0.0

        # 5. Alertas de estoque mínimo e validade próxima
        alertas_estoque: list[dict[str, Any]] = []
        try:
            df_baixo = self.db.produtos_abaixo_estoque_minimo()
            if df_baixo is not None and not df_baixo.empty:
                alertas_estoque = [
                    {"nome": r["nome"], "quantidade": r["quantidade"]}
                    for _, r in df_baixo.iterrows()
                ]
        except Exception:
            pass

        alertas_validade: list[dict[str, Any]] = []
        try:
            df_venc = self.db.verificar_produtos_vencendo(dias_alerta=7)
            if df_venc is not None and not df_venc.empty:
                for _, r in df_venc.iterrows():
                    venc_date = str(r["data_validade"])
                    alertas_validade.append(
                        {
                            "nome": r["nome"],
                            "quantidade": r["quantidade"],
                            "data_validade": venc_date,
                            "vencido": venc_date < today_str,
                        }
                    )
        except Exception:
            pass

        return {
            "estoque_pizza": {"labels": labels, "sizes": sizes},
            "gastos_barras": {"categorias": cats, "valores": vals},
            "faturamento_hoje": sales_today,
            "qtd_vendas_hoje": count_sales_today,
            "lucro_mes": lucro_mes,
            "faturamento_mes": fat_mes,
            "fiado_pendente": fiado_pend,
            "comparativo_mensal": comp,
            "faturamento_mes_atual": fat_atual,
            "faturamento_mes_anterior": fat_ant,
            "variacao_mensal_pct": variacao_pct,
            "alertas_estoque": alertas_estoque,
            "alertas_validade": alertas_validade,
        }

    def _hoje_iso(self) -> str:
        """Obtém a data ISO atual respeitando o módulo datetime do database (facilita testes determinísticos)."""
        db_mod_dt = getattr(getattr(self.db, "__class__", None), "__module__", None)
        if db_mod_dt:
            import sys
            mod = sys.modules.get(db_mod_dt)
            dt_ref = getattr(mod, "datetime", None)
            if dt_ref is not None and hasattr(dt_ref, "date"):
                return dt_ref.date.today().isoformat()
        return datetime.date.today().isoformat()

    # ------------------------------------------------------------------
    # Consultas de Vendas, Lucro, Ranking e Curva ABC
    # ------------------------------------------------------------------

    def listar_vendas_relatorio(self, periodo: str = "mes") -> list[dict[str, Any]]:
        """Lista vendas do período enriquecidas com o nome do produto."""
        df_v = self.db.listar_vendas(periodo)
        if df_v is None or df_v.empty:
            return []
        df_p = self.db.listar_produtos()
        prod_names = (
            dict(zip(df_p["id"], df_p["nome"]))
            if df_p is not None and not df_p.empty
            else {}
        )
        itens: list[dict[str, Any]] = []
        for _, r in df_v.iterrows():
            itens.append(
                {
                    "id": r.get("id"),
                    "data_venda": str(r.get("data_venda", ""))[:16],
                    "produto_nome": prod_names.get(r.get("produto_id"), "Kit/Outro"),
                    "quantidade": r.get("quantidade"),
                    "valor_total": float(r.get("valor_total", 0) or 0.0),
                    "lucro": float(r.get("lucro", 0) or 0.0),
                    "forma_pagamento": r.get("forma_pagamento", "N/A"),
                    "cliente": r.get("cliente", "N/A"),
                    "status": r.get("status", "N/A"),
                }
            )
        return itens

    def vendas_por_forma_pagamento(self, periodo: str = "mes") -> list[dict[str, Any]]:
        """Retorna resumo de vendas por forma de pagamento."""
        df = self.db.vendas_por_forma_pagamento(periodo)
        if df is None or df.empty:
            return []
        return [
            {
                "forma": r["forma"],
                "total": float(r["total"]),
                "qtd": int(r["qtd"]),
            }
            for _, r in df.iterrows()
        ]

    def dados_lucro_e_ranking(self, dias_grafico: int = 30, limite_top: int = 10) -> dict[str, Any]:
        """Retorna KPIs de lucro, rankings, Curva ABC e série temporal diária (sem figuras)."""
        lucros_periodo: dict[str, dict[str, float]] = {}
        for per in ("hoje", "semana", "mes", "ano"):
            try:
                lucro_v, fat_v = self.db.lucro_por_periodo(per)
                lucros_periodo[per] = {"lucro": float(lucro_v), "faturamento": float(fat_v)}
            except Exception:
                lucros_periodo[per] = {"lucro": 0.0, "faturamento": 0.0}

        df_rank = self.db.ranking_produtos_vendidos("mes", limite_top)
        ranking_vendidos = (
            df_rank.to_dict(orient="records")
            if df_rank is not None and not df_rank.empty
            else []
        )

        df_lt = self.db.ranking_produtos_por_lucro("mes", limite_top)
        ranking_lucro_tamanho = (
            df_lt.to_dict(orient="records")
            if df_lt is not None and not df_lt.empty
            else []
        )

        df_abc = self.db.curva_abc("mes")
        if df_abc is not None and not df_abc.empty:
            abc_itens = df_abc.to_dict(orient="records")
            abc_contagem = {
                cls: int(len(df_abc[df_abc["classe"] == cls]))
                for cls in ("A", "B", "C")
            }
        else:
            abc_itens = []
            abc_contagem = {"A": 0, "B": 0, "C": 0}

        df_dias = self.db.vendas_por_dia(dias_grafico)
        if df_dias is not None and not df_dias.empty:
            serie_diaria = {
                "dias": [str(d) for d in df_dias["dia"].tolist()],
                "faturamento": [float(v) for v in df_dias["faturamento"].tolist()],
                "lucro": [float(v) for v in df_dias["lucro"].tolist()],
            }
        else:
            serie_diaria = {"dias": [], "faturamento": [], "lucro": []}

        return {
            "lucros_periodo": lucros_periodo,
            "ranking_vendidos": ranking_vendidos,
            "ranking_lucro_tamanho": ranking_lucro_tamanho,
            "curva_abc": {"itens": abc_itens, "contagem": abc_contagem},
            "serie_diaria": serie_diaria,
        }

    # ------------------------------------------------------------------
    # Fiados e Cobrança PIX (WhatsApp / Mercado Pago)
    # ------------------------------------------------------------------

    def listar_fiados(
        self, status: str | None = None, cliente_id: int | None = None
    ) -> dict[str, Any]:
        """Lista contas a receber (fiados) e o total pendente geral."""
        status_param = None if status in (None, "", "Todos") else status
        df_f = self.db.listar_fiados(status=status_param, cliente_id=cliente_id)
        itens = (
            df_f.to_dict(orient="records")
            if df_f is not None and not df_f.empty
            else []
        )
        try:
            total_pendente = float(self.db.total_fiado_pendente())
        except Exception:
            total_pendente = 0.0
        return {"total_pendente": total_pendente, "itens": itens}

    def quitar_fiado(self, conta_id: int) -> None:
        """Quita uma conta de fiado pelo ID."""
        self.db.quitar_fiado(int(conta_id))

    def obter_telefone_cliente(self, cliente_nome: str) -> str:
        """Busca o contato cadastrado de um cliente pelo nome."""
        if not cliente_nome:
            return ""
        try:
            df_cli = self.db.listar_clientes()
            if df_cli is not None and not df_cli.empty:
                m_cli = df_cli[df_cli["nome"] == cliente_nome]
                if not m_cli.empty:
                    return str(m_cli.iloc[0].get("contato", "") or "").strip()
        except Exception:
            pass
        return ""

    def gerar_cobranca_fiado_pix(
        self,
        conta_id: int,
        telefone: str | None = None,
        *,
        usar_mercadopago: bool | None = None,
        enfileirar_whatsapp: bool = True,
    ) -> dict[str, Any]:
        """
        Gera a cobrança PIX (dinâmica via Mercado Pago quando configurado/solicitado,
        com fallback seguro para PIX Copia e Cola estático da loja) e opcionalmente
        enfileira a mensagem de cobrança na Outbox WhatsApp.
        """
        df_todos = self.db.listar_fiados(status=None)
        registro: dict[str, Any] | None = None
        if df_todos is not None and not df_todos.empty:
            match = df_todos[df_todos["id"] == int(conta_id)]
            if not match.empty:
                registro = match.iloc[0].to_dict()

        if registro is None:
            raise ValueError(f"Conta de fiado #{conta_id} não encontrada.")

        status_conta = str(registro.get("status", ""))
        if status_conta == "Pago":
            raise ValueError("Esta conta já consta como paga.")

        cli_nome = str(registro.get("cliente_nome") or "Cliente")
        valor_num = float(registro.get("valor") or 0.0)
        dt_venda = str(registro.get("data_venda") or "")
        dt_venc = str(registro.get("data_vencimento") or "N/A")

        contato = (telefone if telefone is not None else self.obter_telefone_cliente(cli_nome)).strip()
        if enfileirar_whatsapp:
            digitos = "".join(ch for ch in contato if ch.isdigit())
            if len(digitos) < 8:
                raise ValueError("Número de telefone inválido ou incompleto.")

        config_nota = self.db.get_config_nota()
        nome_emp = config_nota.get("empresa_nome") or "Nossa Loja"
        chave_pix = (config_nota.get("chave_pix") or "").strip()

        payload_pix = ""
        origem_pix = "nenhum"
        payment_id: str | None = None
        qr_code_base64 = ""

        habilitar_mp = (
            usar_mercadopago
            if usar_mercadopago is not None
            else (self.mp_service is not None)
        )
        if habilitar_mp and self.mp_service is not None and self.mp_service.esta_configurado():
            res_mp = self.mp_service.criar_cobranca_pix(
                valor=valor_num,
                descricao=f"Cobrança Fiado Conta #{conta_id}",
                cliente_nome=cli_nome,
                id_venda=f"fiado-{conta_id}",
            )
            if res_mp.get("sucesso") and res_mp.get("qr_code"):
                payload_pix = str(res_mp["qr_code"])
                payment_id = res_mp.get("payment_id")
                qr_code_base64 = str(res_mp.get("qr_code_base64") or "")
                origem_pix = "mercadopago"

        msg_cobranca = (
            f"Olá, *{cli_nome}*! Tudo bem? 😊\n\n"
            f"Passando para lembrar do lançamento em aberto na *{nome_emp}*:\n"
            f"• *Referência:* Conta #{conta_id} (Data: {dt_venda})\n"
            f"• *Vencimento:* {dt_venc}\n"
            f"• *Valor:* R$ {valor_num:.2f}\n"
        )

        if payload_pix and origem_pix == "mercadopago":
            if chave_pix:
                msg_cobranca += f"\n💠 *Chave PIX:* {chave_pix}\n"
            msg_cobranca += f"\n📋 *PIX Copia e Cola:*\n{payload_pix}\n"
        elif chave_pix:
            try:
                payload_pix = gerar_payload_pix(
                    chave=chave_pix,
                    valor=valor_num,
                    nome_recebedor=nome_emp,
                    cidade_recebedor="BRASIL",
                )
                origem_pix = "chave_estatica"
                msg_cobranca += (
                    f"\n💠 *Chave PIX:* {chave_pix}\n"
                    f"📋 *PIX Copia e Cola:*\n{payload_pix}\n"
                )
            except Exception:
                origem_pix = "chave_estatica_sem_emv"
                msg_cobranca += f"\n💠 *Chave PIX:* {chave_pix}\n"

        msg_cobranca += (
            "\nQualquer dúvida ou se já tiver realizado o pagamento, "
            "é só nos responder por aqui. Muito obrigado!"
        )

        enfileirado = False
        outbox_info = ""
        if enfileirar_whatsapp and self.outbox_service is not None:
            _, item_id = self.outbox_service.enfileirar(
                "whatsapp",
                {
                    "telefone": contato,
                    "mensagem": msg_cobranca,
                    "metadata": {"origem": "cobranca_fiado"},
                },
            )
            enfileirado = True
            outbox_info = f"Enfileirado na Outbox (ID #{item_id})"

        return {
            "conta_id": int(conta_id),
            "cliente_nome": cli_nome,
            "telefone": contato,
            "valor": valor_num,
            "origem_pix": origem_pix,
            "payload_pix": payload_pix,
            "payment_id": payment_id,
            "qr_code_base64": qr_code_base64,
            "mensagem": msg_cobranca,
            "enfileirado": enfileirado,
            "outbox_info": outbox_info,
        }

    # ------------------------------------------------------------------
    # Auditoria SHA-256, Histórico e Pacote do Contador
    # ------------------------------------------------------------------

    def verificar_auditoria(self) -> dict[str, Any]:
        """Verifica a integridade da cadeia SHA-256 da tabela historico."""
        integro, ids_adulterados = self.db.verificar_integridade_auditoria()
        return {
            "integro": bool(integro),
            "ids_adulterados": list(ids_adulterados),
        }

    def listar_historico(
        self, periodo: str = "Histórico Completo", tipo: str = "Tudo"
    ) -> list[dict[str, Any]]:
        """Consulta e filtra registros da trilha historico preservando a semântica atual."""
        query = "SELECT * FROM historico"
        params: list[Any] = []
        conditions: list[str] = []

        today = datetime.date.fromisoformat(self._hoje_iso())
        if periodo == "Hoje":
            conditions.append("data_hora LIKE ?")
            params.append(f"{today.isoformat()}%")
        elif periodo == "Últimos 7 dias":
            limit_date = today - datetime.timedelta(days=7)
            conditions.append("data_hora >= ?")
            params.append(limit_date.isoformat())
        elif periodo == "Últimos 30 dias":
            limit_date = today - datetime.timedelta(days=30)
            conditions.append("data_hora >= ?")
            params.append(limit_date.isoformat())

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC"

        with self.db._lock:
            df = pd.read_sql_query(query, self.db.conn, params=params)
        if df is None or df.empty:
            return []

        registros: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            if tipo != "Tudo":
                matches = False
                acao_lower = str(row["acao"]).lower()
                if tipo == "Venda" and ("venda" in acao_lower or "saída" in acao_lower):
                    matches = True
                elif tipo == "Estoque" and (
                    "produto" in acao_lower or "estoque" in acao_lower or "kit" in acao_lower
                ):
                    matches = True
                elif tipo == "Financeiro" and (
                    "gasto" in acao_lower or "saldo" in acao_lower or "pagamento" in acao_lower
                ):
                    matches = True
                elif tipo == "Sistema" and ("login" in acao_lower or "config" in acao_lower):
                    matches = True
                if not matches:
                    continue

            registros.append(
                {
                    "id": row["id"],
                    "data_hora": row["data_hora"],
                    "acao": row["acao"],
                    "detalhe": row["detalhe"],
                }
            )
        return registros

    def exportar_historico_csv(
        self,
        destino_csv_path: str,
        periodo: str = "Histórico Completo",
        tipo: str = "Tudo",
    ) -> int:
        """Exporta para CSV apenas os registros do histórico correspondentes aos filtros ativos."""
        registros = self.listar_historico(periodo=periodo, tipo=tipo)
        df = pd.DataFrame(registros, columns=["id", "data_hora", "acao", "detalhe"])
        df.to_csv(destino_csv_path, index=False)
        return len(registros)

    def gerar_pacote_contador(
        self, mes_ano: str | None = None, destino_zip_path: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        """Delega ao Database.exportar_pacote_contador (I/O fora do lock será tratado no Lote 5)."""
        return self.db.exportar_pacote_contador(
            mes_ano=mes_ano, destino_zip_path=destino_zip_path
        )
