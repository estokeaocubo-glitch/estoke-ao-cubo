"""
Serviço de produção — baixa manual de insumos, fichas técnicas (receitas) e produção diária.

Contrato de serviço (F1b Lote 7):
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependência injetada no construtor (db).
- Retorna dados de domínio e lança ValueError com a mensagem exibida pela UI.
- Estoque negativo continua bloqueado (a checagem fica no banco, dentro do _lock).
"""
import logging

logger = logging.getLogger('EstokeAoCubo.services.producao')


def _parse_float_positivo(valor, mensagem):
    try:
        qtd = float(str(valor).strip().replace(",", "."))
    except (ValueError, TypeError):
        raise ValueError(mensagem)
    if not qtd > 0 or qtd == float("inf"):
        raise ValueError(mensagem)
    return qtd


def _parse_int_positivo(valor, mensagem):
    try:
        qtd = int(str(valor).strip())
    except (ValueError, TypeError):
        raise ValueError(mensagem)
    if qtd <= 0:
        raise ValueError(mensagem)
    return qtd


class ProducaoService:
    """Lógica de negócios de insumos, fichas técnicas e produção."""

    def __init__(self, db):
        self.db = db

    # ==================== BAIXA DE INSUMOS ====================

    def baixar_insumo(self, insumo_id, quantidade, motivo="") -> str:
        """Dá baixa manual de um insumo. Retorna a mensagem de sucesso."""
        if insumo_id is None:
            raise ValueError("Selecione um insumo.")
        qtd = _parse_float_positivo(quantidade, "Quantidade inválida.")
        motivo_s = str(motivo or "").strip() or "Uso diário"
        ok, msg = self.db.registrar_baixa_insumo(insumo_id, qtd, motivo_s)
        if not ok:
            raise ValueError(msg)
        return msg

    # ==================== FICHAS TÉCNICAS ====================

    def criar_ficha(self, produto_id, nome, rendimento, itens) -> int:
        """Cria ficha técnica. itens: [{'insumo_id': int|None, 'quantidade': texto/número}].

        Itens sem insumo ou com quantidade inválida/<= 0 são ignorados (como a tela fazia).
        Retorna o id da ficha.
        """
        if produto_id is None:
            raise ValueError("Selecione o produto final.")
        nome_s = str(nome or "").strip()
        if not nome_s:
            raise ValueError("Nome da ficha é obrigatório.")
        rend = _parse_int_positivo(rendimento, "Rendimento deve ser um número inteiro positivo.")

        itens_ok = []
        for it in itens or []:
            insumo_id = it.get('insumo_id')
            if insumo_id is None:
                continue
            try:
                qtd = _parse_float_positivo(it.get('quantidade'), "")
            except ValueError:
                continue
            itens_ok.append({'insumo_id': insumo_id, 'quantidade': qtd})
        if not itens_ok:
            raise ValueError("Adicione ao menos um insumo à ficha.")
        return self.db.adicionar_ficha_tecnica(produto_id, nome_s, rend, itens_ok)

    def remover_ficha(self, ficha_id) -> None:
        if ficha_id is None:
            raise ValueError("Selecione uma ficha técnica.")
        self.db.remover_ficha_tecnica(ficha_id)

    # ==================== PRODUÇÃO ====================

    def produzir(self, ficha_id, quantidade, observacao="") -> str:
        """Registra produção (baixa insumos, entra produto final). Retorna a mensagem de sucesso."""
        if ficha_id is None:
            raise ValueError("Selecione uma ficha técnica.")
        qtd = _parse_int_positivo(quantidade, "Quantidade inválida.")
        ok, msg = self.db.registrar_producao(ficha_id, qtd, str(observacao or "").strip())
        if not ok:
            raise ValueError(msg)
        return msg
