"""
Serviço de trocas e devoluções.

Contrato de serviço (F1b Lote 7):
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependência injetada no construtor (db).
- Retorna dados de domínio e lança ValueError com a mensagem exibida pela UI.
- Diferença em dinheiro em Decimal (ROUND_HALF_UP, 2 casas); o banco continua gravando REAL.
- O lançamento financeiro da diferença vai para a loja ativa (None = loja padrão).
"""
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

logger = logging.getLogger('EstokeAoCubo.services.troca')

_CENTAVOS = Decimal("0.01")

TIPOS_VALIDOS = ("troca", "devolucao")


def parse_diferenca(valor) -> Decimal:
    """Diferença de valor da troca; vazio/inválido vira 0 (como a tela fazia)."""
    try:
        d = Decimal(str(valor if valor is not None else "0").strip().replace(",", ".") or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    if not d.is_finite():
        return Decimal("0.00")
    return d.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


class TrocaService:
    """Lógica de negócios de trocas e devoluções."""

    def __init__(self, db):
        self.db = db

    def registrar(self, cliente, produto_devolvido_id, qtd_devolvida, tipo="troca",
                  produto_novo_id=None, qtd_nova=None, diferenca="0", observacao="",
                  loja_id=None) -> str:
        """Registra troca/devolução. Retorna a mensagem de sucesso."""
        if produto_devolvido_id is None:
            raise ValueError("Selecione o produto devolvido.")
        try:
            qtd_dev = int(str(qtd_devolvida).strip())
        except (ValueError, TypeError):
            raise ValueError("Quantidade devolvida inválida.")
        if qtd_dev <= 0:
            raise ValueError("Quantidade devolvida inválida.")

        if tipo == "troca" and produto_novo_id is not None:
            try:
                qtd_n = int(str(qtd_nova).strip())
            except (ValueError, TypeError):
                qtd_n = 1
            novo_id = produto_novo_id
        else:
            novo_id, qtd_n = None, 0

        dif = parse_diferenca(diferenca)
        ok, msg = self.db.registrar_troca(
            cliente, produto_devolvido_id, qtd_dev, novo_id, qtd_n,
            float(dif), tipo, str(observacao or "").strip(), loja_id=loja_id,
        )
        if not ok:
            raise ValueError(msg)
        return msg
