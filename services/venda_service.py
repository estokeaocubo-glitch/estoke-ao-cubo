"""
Serviço de vendas — lógica de negócios separada da UI.

Centraliza: validação de carrinho, cálculo de desconto,
dedução de estoque, registro financeiro e geração de invoice.
"""
import logging
import datetime
import re

logger = logging.getLogger('EstokeAoCubo.services.venda')


def parse_scan_input(raw_text: str):
    """
    Analisa entrada do leitor de código de barras suportando sintaxe `qtd*codigo`
    (ex: '3*7891234567895' -> (3, '7891234567895', True), '0,450*2001' -> (0.45, '2001', True)).

    Retorna:
        (quantidade: int|float, codigo_limpo: str, multiplicador_explicito: bool)
    Levanta ValueError se o multiplicador ou código forem inválidos.
    """
    texto = (raw_text or "").strip()
    if not texto:
        raise ValueError("Código de barras vazio.")

    m = re.match(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*\*\s*(.+?)\s*$", texto)
    if m:
        qtd_raw = float(m.group(1).replace(",", "."))
        codigo = m.group(2).strip()
        if qtd_raw <= 0:
            raise ValueError("A quantidade informada antes de '*' deve ser maior que zero.")
        if not codigo:
            raise ValueError("Código de barras após '*' não pode ser vazio.")
        qtd = int(qtd_raw) if qtd_raw.is_integer() else round(qtd_raw, 3)
        return qtd, codigo, True

    return 1, texto, False


class VendaService:
    """Orquestra o fluxo completo de uma venda."""

    parse_scan_input = staticmethod(parse_scan_input)

    def __init__(self, db):
        self.db = db

    def validar_carrinho(self, carrinho):
        """Valida se todos os itens do carrinho têm estoque suficiente.
        
        Args:
            carrinho: lista de dicts com keys 'nome', 'quantidade', 'tipo' ('produto' ou 'kit')
        
        Returns:
            (ok: bool, erros: list[str])
        """
        erros = []
        for item in carrinho:
            nome = item.get('nome', '')
            qtd = item.get('quantidade', 0)
            tipo = item.get('tipo', 'produto')

            if qtd <= 0:
                erros.append(f"Quantidade inválida para '{nome}'.")
                continue

            if tipo == 'kit':
                kit_id = item.get('kit_id')
                if kit_id:
                    disponivel = self.db.verificar_disponibilidade_kit(kit_id)
                    if qtd > disponivel:
                        erros.append(f"Kit '{nome}': estoque insuficiente ({disponivel} disponíveis).")
            else:
                df = self.db.listar_produtos()
                match = df[df['nome'] == nome]
                if match.empty:
                    erros.append(f"Produto '{nome}' não encontrado.")
                else:
                    estoque = int(match.iloc[0].get('quantidade', 0))
                    if qtd > estoque:
                        erros.append(f"'{nome}': estoque insuficiente ({estoque} disponíveis).")

        return (len(erros) == 0, erros)

    def calcular_totais(self, carrinho, desconto_valor=0, desconto_tipo='R$'):
        """Calcula subtotal, desconto aplicado e total final.
        
        Returns:
            dict com 'subtotal', 'desconto', 'total'
        """
        subtotal = sum(
            item.get('preco_unitario', 0) * item.get('quantidade', 0)
            for item in carrinho
        )
        
        if desconto_tipo == '%':
            desconto = subtotal * (desconto_valor / 100)
        else:
            desconto = desconto_valor

        desconto = min(desconto, subtotal)  # Não pode exceder o subtotal
        total = subtotal - desconto

        return {
            'subtotal': round(subtotal, 2),
            'desconto': round(desconto, 2),
            'total': round(total, 2),
        }

    def calcular_troco(self, total, valor_recebido):
        """Calcula troco para pagamento em dinheiro.
        
        Returns:
            (troco: float, suficiente: bool)
        """
        troco = valor_recebido - total
        return (round(troco, 2), troco >= 0)

    def finalizar_venda(self, carrinho, total, desconto, cliente, forma_pagamento,
                        vendedor='', observacao='', loja_id=None):
        """Executa o fluxo completo de finalização de venda.
        
        Delega para Database.registrar_venda_detalhada() que já cuida de:
        - Vincular ao caixa aberto
        - Distribuir desconto proporcionalmente
        - Deduzir estoque
        - Registrar auditoria de caixa
        - Atualizar fidelidade do cliente
        - Registrar entrada financeira
        
        Returns:
            dict com 'venda_id', 'sucesso', 'mensagem'
        """
        try:
            # Converter carrinho para o formato esperado por registrar_venda_detalhada
            itens = []
            for item in carrinho:
                preco_unitario = item.get('preco_unitario', item.get('preco', 0))
                preco_custo = item.get('preco_custo', item.get('preco_compra', 0))
                itens.append({
                    'id': item.get('id') or item.get('produto_id'),
                    'nome': item.get('nome', ''),
                    'qtd': item.get('quantidade', item.get('qtd', 0)),
                    'preco': preco_unitario,
                    'preco_compra': preco_custo,
                })

            resultado = self.db.registrar_venda_detalhada(
                itens, total, desconto, cliente, forma_pagamento, vendedor
            )

            if isinstance(resultado, tuple):
                sucesso, msg = resultado
                return {'venda_id': None, 'sucesso': sucesso, 'mensagem': msg}

            if resultado:
                # Registrar fiado se necessário
                if 'fiado' in forma_pagamento.lower() and cliente != 'Consumidor Final':
                    try:
                        cursor = self.db.conn.cursor()
                        cursor.execute("SELECT id FROM clientes WHERE nome = ?", (cliente,))
                        c_row = cursor.fetchone()
                        if c_row:
                            vencimento = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
                            self.db.registrar_fiado(c_row[0], total, f"Venda fiado: {cliente}")
                    except Exception as e:
                        logger.warning("Erro ao registrar fiado via service: %s", e)

                return {'venda_id': None, 'sucesso': True, 'mensagem': 'Venda registrada com sucesso.'}

            return {'venda_id': None, 'sucesso': False, 'mensagem': 'Falha ao registrar venda.'}

        except Exception as e:
            logger.error("Erro ao finalizar venda: %s", e)
            return {'venda_id': None, 'sucesso': False, 'mensagem': str(e)}

    def cancelar_venda(self, venda_id, motivo="Cancelamento via serviço", usuario_id=None):
        """Cancela uma venda: restaura estoque e faz estorno financeiro.
        
        Returns:
            (sucesso: bool, mensagem: str)
        """
        try:
            return self.db.cancelar_venda(venda_id, motivo=motivo, usuario_id=usuario_id)
        except Exception as e:
            logger.error("Erro ao cancelar venda %s: %s", venda_id, e)
            return (False, str(e))
