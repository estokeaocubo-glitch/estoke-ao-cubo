"""
Serviço de caixa — abertura, fechamento e resumo financeiro.
"""
import logging
import datetime

logger = logging.getLogger('EstokeAoCubo.services.caixa')


class CaixaService:
    """Lógica de negócios para controle de caixa."""

    def __init__(self, db):
        self.db = db

    def caixa_aberto(self, usuario_id=None):
        """Retorna dados do caixa aberto ou None."""
        return self.db.caixa_aberto(usuario_id)

    def abrir_caixa(self, valor_inicial, observacao='', usuario_id=1, loja_id=1):
        """Abre o caixa com valor de troco inicial.
        
        Returns:
            (sucesso: bool, mensagem: str)
        """
        try:
            return self.db.abrir_caixa(valor_inicial, observacao, usuario_id, loja_id)
        except Exception as e:
            logger.error("Erro ao abrir caixa: %s", e)
            return (False, str(e))

    def fechar_caixa(self, valor_informado, observacao='', usuario_id=None):
        """Fecha o caixa e gera resumo.
        
        Returns:
            (sucesso: bool, resumo: dict | str)
        """
        caixa = self.caixa_aberto(usuario_id)
        if not caixa:
            return (False, "Nenhum caixa aberto para fechar.")
        
        try:
            resumo = self.db.fechar_caixa(valor_informado, observacao, usuario_id)
            return (True, resumo)
        except Exception as e:
            logger.error("Erro ao fechar caixa: %s", e)
            return (False, str(e))
