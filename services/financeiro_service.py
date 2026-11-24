"""
Serviço financeiro — gestão de saldo/entradas, CRUD de gastos, CRUD de contas a pagar,
DRE simplificado, projeção de fluxo de caixa (30/60/90 dias) e comissões.
"""
from __future__ import annotations

import datetime
from typing import Any

CATEGORIAS_GASTO_PADRAO = ["Material", "Transporte", "Serviços", "Aluguel", "Outro"]


class FinanceiroService:
    """Lógica de negócio pura para rotinas financeiras (sem dependências de UI)."""

    def __init__(self, db: Any):
        self.db = db

    # ------------------------------------------------------------------
    # Saldo / Entradas Manuais
    # ------------------------------------------------------------------

    def saldo(self, loja_id: int | None = None) -> float:
        """Retorna o saldo atual em caixa/conta."""
        return float(self.db.get_saldo_atual(loja_id=loja_id))

    def adicionar_entrada(
        self,
        valor: float | str,
        descricao: str = "Entrada Manual",
        loja_id: int | None = None,
    ) -> float:
        """Valida e registra entrada manual de saldo, retornando o novo saldo."""
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            raise ValueError("Digite um valor.")
        try:
            valor_str = str(valor).strip().replace(",", ".")
            valor_float = float(valor_str)
        except ValueError as exc:
            raise ValueError(
                "Valor inválido. Use vírgula ou ponto como separador decimal."
            ) from exc

        desc_final = (descricao or "").strip() or "Entrada Manual"
        self.db.adicionar_entrada(valor_float, desc_final, loja_id=loja_id)
        return self.saldo(loja_id=loja_id)

    # ------------------------------------------------------------------
    # CRUD de Gastos
    # ------------------------------------------------------------------

    def listar_categorias_gasto(self) -> list[str]:
        """Lista categorias cadastradas para gastos com fallback padrão."""
        cats = self.db.listar_categorias("gasto")
        return list(cats) if cats else list(CATEGORIAS_GASTO_PADRAO)

    def listar_gastos(
        self, invertido: bool = False, loja_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Retorna gastos registrados como lista de dicts."""
        df = self.db.listar_gastos(loja_id=loja_id)
        if df is None or df.empty:
            return []
        if invertido:
            df = df.iloc[::-1]
        return df.to_dict(orient="records")

    def adicionar_gasto(
        self,
        data: str,
        valor: float | str,
        categoria: str,
        descricao: str = "",
        tipo: str = "Variável",
        loja_id: int | None = None,
    ) -> None:
        """Valida valor e data ISO (YYYY-MM-DD) e registra um novo gasto."""
        try:
            val_float = float(str(valor).strip().replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise ValueError("Valor inválido.") from exc

        data_str = str(data or "").strip()
        try:
            datetime.date.fromisoformat(data_str)
        except ValueError as exc:
            raise ValueError(
                "Data inválida. Use o formato YYYY-MM-DD (ex: 2026-02-28)."
            ) from exc

        cat_str = str(categoria or "").strip() or "Outro"
        desc_str = str(descricao or "").strip()
        self.db.adicionar_gasto(
            data_str, val_float, cat_str, desc_str, tipo=tipo, loja_id=loja_id
        )

    def remover_gasto(self, gasto_id: int) -> None:
        """Remove gasto pelo ID."""
        self.db.remover_gasto(int(gasto_id))

    def crud_gastos(self, acao: str, **kwargs: Any) -> Any:
        """Fachada unificada de CRUD para gastos ('listar', 'adicionar', 'remover', 'categorias')."""
        op = (acao or "").strip().lower()
        if op == "listar":
            return self.listar_gastos(
                invertido=bool(kwargs.get("invertido", False)),
                loja_id=kwargs.get("loja_id"),
            )
        if op == "adicionar":
            return self.adicionar_gasto(
                data=kwargs["data"],
                valor=kwargs["valor"],
                categoria=kwargs.get("categoria", "Outro"),
                descricao=kwargs.get("descricao", ""),
                tipo=kwargs.get("tipo", "Variável"),
                loja_id=kwargs.get("loja_id"),
            )
        if op == "remover":
            return self.remover_gasto(kwargs["gasto_id"])
        if op == "categorias":
            return self.listar_categorias_gasto()
        raise ValueError(f"Ação de gastos desconhecida: {acao!r}")

    # ------------------------------------------------------------------
    # CRUD de Contas a Pagar
    # ------------------------------------------------------------------

    def listar_fornecedores(self) -> list[dict[str, Any]]:
        """Retorna fornecedores cadastrados como lista de dicts (id, nome)."""
        df = self.db.listar_fornecedores()
        if df is None or df.empty:
            return []
        return df.to_dict(orient="records")

    def listar_contas_pagar(self, status: str | None = None) -> dict[str, Any]:
        """
        Lista contas a pagar filtradas por status ('Pendente', 'Pago' ou None/'Todos')
        e calcula o total pendente nos itens retornados, preservando o comportamento da UI.
        """
        status_param = None if status in (None, "", "Todos") else status
        df = self.db.listar_contas_pagar(status=status_param)
        contas: list[dict[str, Any]] = []
        total_pendente = 0.0
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                parc_atual = int(row.get("parcela_atual", 1) or 1)
                tot_parc = int(row.get("total_parcelas", 1) or 1)
                valor = float(row.get("valor", 0) or 0.0)
                st = str(row.get("status", "Pendente") or "Pendente")
                if st == "Pendente":
                    total_pendente += valor
                contas.append(
                    {
                        "id": row["id"],
                        "descricao": row.get("descricao", ""),
                        "valor": valor,
                        "data_vencimento": str(row.get("data_vencimento", "")),
                        "fornecedor_nome": row.get("fornecedor_nome", ""),
                        "status": st,
                        "parcela_atual": parc_atual,
                        "total_parcelas": tot_parc,
                        "parcela_texto": f"{parc_atual}/{tot_parc}",
                    }
                )
        return {"contas": contas, "total_pendente": total_pendente}

    def adicionar_conta_pagar(
        self,
        descricao: str,
        valor: float | str,
        data_vencimento: str,
        fornecedor_id: int | None = None,
        categoria: str = "Outro",
        total_parcelas: int | str = 1,
        observacao: str = "",
    ) -> None:
        """Valida dados da conta a pagar e registra no banco."""
        try:
            valor_float = float(str(valor).strip().replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise ValueError("Valor inválido.") from exc

        venc_str = str(data_vencimento or "").strip()
        try:
            datetime.date.fromisoformat(venc_str)
        except ValueError as exc:
            raise ValueError("Data de vencimento inválida (use AAAA-MM-DD).") from exc

        desc_str = str(descricao or "").strip()
        if not desc_str:
            raise ValueError("Preencha a descrição.")

        cat_str = str(categoria or "").strip() or "Outro"
        try:
            parcelas_int = int(total_parcelas)
        except (TypeError, ValueError):
            parcelas_int = 1
        obs_str = str(observacao or "").strip()

        self.db.adicionar_conta_pagar(
            desc_str,
            valor_float,
            venc_str,
            fornecedor_id,
            cat_str,
            total_parcelas=parcelas_int,
            observacao=obs_str,
        )

    def pagar_conta(self, conta_id: int) -> None:
        """Marca conta a pagar como paga."""
        self.db.pagar_conta(int(conta_id))

    def cancelar_conta_pagar(self, conta_id: int) -> None:
        """Cancela conta a pagar."""
        self.db.cancelar_conta_pagar(int(conta_id))

    def crud_contas_pagar(self, acao: str, **kwargs: Any) -> Any:
        """Fachada unificada de CRUD para contas a pagar ('listar', 'adicionar', 'pagar', 'cancelar')."""
        op = (acao or "").strip().lower()
        if op == "listar":
            return self.listar_contas_pagar(status=kwargs.get("status"))
        if op == "adicionar":
            return self.adicionar_conta_pagar(
                descricao=kwargs.get("descricao", ""),
                valor=kwargs["valor"],
                data_vencimento=kwargs["data_vencimento"],
                fornecedor_id=kwargs.get("fornecedor_id"),
                categoria=kwargs.get("categoria", "Outro"),
                total_parcelas=kwargs.get("total_parcelas", 1),
                observacao=kwargs.get("observacao", ""),
            )
        if op == "pagar":
            return self.pagar_conta(kwargs["conta_id"])
        if op == "cancelar":
            return self.cancelar_conta_pagar(kwargs["conta_id"])
        raise ValueError(f"Ação de contas a pagar desconhecida: {acao!r}")

    # ------------------------------------------------------------------
    # DRE, Fluxo de Caixa Projetado e Comissões
    # ------------------------------------------------------------------

    def gerar_dre(
        self, periodo: str = "mes", loja_id: int | None = None
    ) -> dict[str, Any]:
        """Gera DRE simplificado do período ('mes', 'trimestre' ou 'ano')."""
        return self.db.gerar_dre(periodo=periodo, loja_id=loja_id)

    def projetar_fluxo(self, dias: int = 90) -> dict[str, Any]:
        """Projeta o fluxo de caixa futuro (ex.: 30, 60 ou 90 dias)."""
        dias_int = int(dias)
        if dias_int <= 0:
            raise ValueError("O horizonte de projeção em dias deve ser positivo.")
        return self.db.projetar_fluxo_caixa(dias_int)

    def calcular_comissoes(self, periodo: str = "mes") -> dict[str, Any]:
        """
        Calcula comissões de vendedores no período ('semana', 'mes' ou 'ano'),
        convertendo os DataFrames do banco em listas de dicts puras.
        """
        dados = self.db.calcular_comissoes(periodo)
        df_vendas = dados.get("vendas")
        df_servicos = dados.get("servicos")
        vendas_list = (
            df_vendas.to_dict(orient="records")
            if df_vendas is not None and not df_vendas.empty
            else []
        )
        servicos_list = (
            df_servicos.to_dict(orient="records")
            if df_servicos is not None and not df_servicos.empty
            else []
        )
        total_comissoes_vendas = sum(
            float(r.get("comissao_vendas", 0) or 0.0) for r in vendas_list
        )
        return {
            "periodo_inicio": dados.get("periodo_inicio", ""),
            "vendas": vendas_list,
            "servicos": servicos_list,
            "total_comissoes_vendas": total_comissoes_vendas,
        }
