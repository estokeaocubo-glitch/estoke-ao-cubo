"""
Serviço de Restaurante (mesas, comandas, cozinha, fechamento e divisão de conta).

Contrato de serviço (F1b Lote 8A):
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependência injetada no construtor (db).
- Retorna dados de domínio e lança ValueError/RuntimeError com a mensagem exibida pela UI.
- Dinheiro em Decimal (ROUND_HALF_UP, 2 casas); o banco continua gravando REAL.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger("EstokeAoCubo.services.restaurante")

_CENTAVOS = Decimal("0.01")


def _df_to_records(df: Any) -> list[dict[str, Any]]:
    """Converte DataFrame (ou lista) para list[dict] preservando vazio como []."""
    if df is None:
        return []
    if isinstance(df, list):
        return list(df)
    if getattr(df, "empty", False):
        return []
    return df.to_dict(orient="records")


def _parse_decimal(valor: Any, padrao: Decimal | None = None) -> Decimal:
    """Converte entrada numérica/texto (vírgula ou ponto) em Decimal de 2 casas."""
    if valor in (None, ""):
        if padrao is not None:
            return padrao
        raise ValueError("Valor inválido.")
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError) as exc:
        if padrao is not None:
            return padrao
        raise ValueError("Valor inválido.") from exc
    if not d.is_finite():
        if padrao is not None:
            return padrao
        raise ValueError("Valor inválido.")
    return d.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


class RestauranteService:
    """Centraliza operações de mesas, comandas, painel da cozinha e fechamento."""

    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def calcular_taxa_10(subtotal: Any) -> Decimal:
        """Calcula a taxa de serviço opcional de 10% em Decimal (ROUND_HALF_UP, 2 casas)."""
        sub_dec = _parse_decimal(subtotal, padrao=Decimal("0.00"))
        if sub_dec <= Decimal("0.00"):
            return Decimal("0.00")
        return (sub_dec * Decimal("0.10")).quantize(_CENTAVOS, rounding=ROUND_HALF_UP)

    # ------------------------------------------------------------------
    # Mesas
    # ------------------------------------------------------------------

    def criar_mesas(self, quantidade: Any, capacidade: Any = 4, area: str = "Salão") -> int:
        try:
            qtd = int(quantidade)
        except (TypeError, ValueError):
            qtd = 1
        try:
            cap = int(capacidade)
        except (TypeError, ValueError):
            cap = 4
        if qtd < 1:
            raise ValueError("Quantidade de mesas deve ser maior que zero.")
        if cap < 1:
            raise ValueError("Capacidade da mesa deve ser maior que zero.")
        area_limpa = str(area or "Salão").strip() or "Salão"
        return int(self.db.criar_mesas(qtd, cap, area_limpa))

    def listar_mesas(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_mesas())

    def atualizar_status_mesa(self, mesa_id: int, status: str) -> None:
        self.db.atualizar_status_mesa(int(mesa_id), str(status).strip())

    def remover_mesa(self, mesa_id: int) -> None:
        self.db.remover_mesa(int(mesa_id))

    # ------------------------------------------------------------------
    # Comandas e Cozinha
    # ------------------------------------------------------------------

    def abrir_comanda(
        self,
        mesa_id: int | None = None,
        tipo_atendimento: str = "mesa",
        cliente: str = "Consumidor Final",
        garcom: str = "",
    ) -> int:
        return int(
            self.db.abrir_comanda(
                mesa_id=mesa_id,
                tipo_atendimento=str(tipo_atendimento or "mesa").strip(),
                cliente=str(cliente or "Consumidor Final").strip() or "Consumidor Final",
                garcom=str(garcom or "").strip(),
            )
        )

    def get_comanda(self, comanda_id: int) -> dict[str, Any] | None:
        return self.db.get_comanda(int(comanda_id))

    def get_comanda_por_mesa(self, mesa_id: int) -> dict[str, Any] | None:
        return self.db.get_comanda_por_mesa(int(mesa_id))

    def listar_comandas_abertas(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_comandas_abertas())

    def listar_comandas_historico(self, dias: int = 7) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_comandas_historico(int(dias)))

    def adicionar_item_comanda(
        self,
        comanda_id: int,
        produto_id: int,
        produto_nome: str,
        quantidade: Any,
        preco_unitario: Any,
        observacao: str = "",
    ) -> None:
        try:
            qtd = int(quantidade)
        except (TypeError, ValueError):
            qtd = 1
        if qtd < 1:
            raise ValueError("Quantidade do item deve ser maior que zero.")
        preco_dec = _parse_decimal(preco_unitario, padrao=Decimal("0.00"))
        self.db.adicionar_item_comanda(
            int(comanda_id),
            int(produto_id),
            str(produto_nome or "").strip(),
            qtd,
            float(preco_dec),
            str(observacao or "").strip(),
        )

    def remover_item_comanda(self, item_id: int) -> None:
        self.db.remover_item_comanda(int(item_id))

    def atualizar_status_item_comanda(self, item_id: int, status: str) -> None:
        self.db.atualizar_status_item_comanda(int(item_id), str(status).strip())

    def listar_itens_comanda(self, comanda_id: int) -> list[dict[str, Any]]:
        return _df_to_records(self.db.listar_itens_comanda(int(comanda_id)))

    def itens_cozinha_pendentes(self) -> list[dict[str, Any]]:
        return _df_to_records(self.db.itens_cozinha_pendentes())

    # ------------------------------------------------------------------
    # Fechamento e Divisão de Conta
    # ------------------------------------------------------------------

    def fechar_comanda(
        self,
        comanda_id: int,
        forma_pagamento: str,
        aplicar_taxa_10: bool = False,
        desconto: Any = 0.0,
        observacao: str = "",
        loja_id: int | None = None,
    ) -> dict[str, Any]:
        """Calcula taxa de 10%/desconto em Decimal e fecha a comanda atomicamente."""
        comanda = self.db.get_comanda(comanda_id)
        if not comanda or comanda.get("status") != "aberta":
            raise ValueError("Comanda não encontrada ou já fechada.")

        subtotal_dec = _parse_decimal(comanda.get("total") or 0, padrao=Decimal("0.00"))
        taxa_dec = self.calcular_taxa_10(subtotal_dec) if aplicar_taxa_10 else Decimal("0.00")
        desc_dec = _parse_decimal(desconto, padrao=Decimal("0.00"))
        if desc_dec < Decimal("0.00"):
            desc_dec = Decimal("0.00")

        total_final_dec = max(subtotal_dec + taxa_dec - desc_dec, Decimal("0.00")).quantize(
            _CENTAVOS, rounding=ROUND_HALF_UP
        )

        obs_limpa = str(observacao or "").strip()
        ok, msg = self.db.fechar_comanda(
            comanda_id,
            forma_pagamento,
            float(taxa_dec),
            float(desc_dec),
            obs_limpa,
            loja_id=loja_id,
        )
        if not ok:
            raise RuntimeError(str(msg))

        return {
            "comanda_id": comanda_id,
            "subtotal": float(subtotal_dec),
            "taxa_servico": float(taxa_dec),
            "desconto": float(desc_dec),
            "total_final": float(total_final_dec),
            "forma_pagamento": forma_pagamento,
            "mensagem": msg,
        }

    def dividir_conta(self, comanda_id: int, num_pessoas: Any) -> dict[str, Any]:
        """Divide o total da comanda em centavos inteiros delegando ao Database."""
        try:
            n = int(num_pessoas)
        except (TypeError, ValueError):
            n = 1
        if n < 1:
            n = 1

        resultado = self.db.dividir_conta(comanda_id, n)
        if not resultado:
            raise ValueError("Comanda não encontrada.")
        return resultado
