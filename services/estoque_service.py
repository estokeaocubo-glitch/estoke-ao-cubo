"""
Serviço de estoque — lógica de negócios para gestão de produtos, kits virtuais e reposição.

Contrato de serviço (F1b Lote 7):
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependência injetada no construtor (db).
- Retorna dados de domínio e lança ValueError com a mensagem exibida pela UI.
- Dinheiro em Decimal (ROUND_HALF_UP, 2 casas); o banco continua gravando REAL.
"""
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

logger = logging.getLogger('EstokeAoCubo.services.estoque')

_CENTAVOS = Decimal("0.01")


def _parse_preco(valor) -> Decimal:
    """Converte texto/número em Decimal com 2 casas; aceita vírgula decimal."""
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError("Preço inválido")
    if not d.is_finite() or d < 0:
        raise ValueError("Preço inválido")
    return d.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


class EstoqueService:
    """Lógica de negócios para gestão de estoque."""

    def __init__(self, db):
        self.db = db

    def produtos_abaixo_minimo(self):
        """Retorna produtos com estoque abaixo do mínimo configurado."""
        return self.db.produtos_abaixo_estoque_minimo()

    def produtos_vencendo(self, dias=7):
        """Retorna produtos com validade dentro dos próximos N dias."""
        return self.db.verificar_produtos_vencendo(dias_alerta=dias)

    def disponibilidade_kit(self, kit_id):
        """Calcula quantos kits podem ser montados com estoque atual."""
        return self.db.verificar_disponibilidade_kit(kit_id)

    def lista_reposicao(self):
        """Gera lista de produtos que precisam ser repostos.
        
        Returns:
            list[dict] com 'nome', 'estoque_atual', 'estoque_minimo', 'urgencia'
        """
        df = self.db.produtos_abaixo_estoque_minimo()
        resultado = []
        if df.empty:
            return resultado
        
        for _, row in df.iterrows():
            atual = int(row.get('quantidade', 0))
            minimo = int(row.get('estoque_minimo', 5))
            urgencia = 'critica' if atual == 0 else 'baixa'
            resultado.append({
                'nome': row.get('nome', ''),
                'estoque_atual': atual,
                'estoque_minimo': minimo,
                'repor': max(0, minimo - atual),
                'urgencia': urgencia,
            })
        
        # Ordenar: críticos primeiro
        resultado.sort(key=lambda x: (0 if x['urgencia'] == 'critica' else 1, x['nome']))
        return resultado

    # ==================== KITS VIRTUAIS ====================

    @staticmethod
    def parse_qtd_componente(valor) -> int:
        """Quantidade de um produto dentro do kit: inteiro > 0."""
        try:
            qtd = int(str(valor).strip())
        except (ValueError, TypeError):
            raise ValueError("Quantidade inválida.")
        if qtd <= 0:
            raise ValueError("Quantidade inválida.")
        return qtd

    def criar_kit(self, nome, preco, itens) -> int:
        """Valida e cria um kit virtual (estoque calculado pelos componentes).

        itens: lista de dicts {'nome': str, 'quantidade': int} (componentes ligados por nome).
        Retorna o id do kit criado.
        """
        preco_d = _parse_preco(preco)
        nome_s = str(nome or "").strip()
        if not nome_s:
            raise ValueError("Defina um nome para o kit.")
        if not itens:
            raise ValueError("O kit precisa ter pelo menos um produto.")
        itens_ok = [
            {'nome': str(it['nome']), 'quantidade': self.parse_qtd_componente(it['quantidade'])}
            for it in itens
        ]
        return self.db.adicionar_kit(nome_s, 0, float(preco_d), 0, itens_ok)

    # ==================== LISTA DE REPOSIÇÃO ====================

    def itens_reposicao(self) -> list:
        """Produtos vendáveis com quantidade <= estoque mínimo (> 0), maior falta primeiro."""
        df = self.db.gerar_lista_reposicao()
        if df is None or df.empty:
            return []
        itens = []
        for _, r in df.iterrows():
            categoria = r.get('categoria')
            itens.append({
                'id': int(r['id']),
                'nome': r['nome'],
                'quantidade': r['quantidade'],
                'estoque_minimo': r['estoque_minimo'],
                'qtd_repor': r['qtd_repor'],
                'categoria': categoria if isinstance(categoria, str) else "",
            })
        return itens

    @staticmethod
    def texto_reposicao_whatsapp(itens) -> str:
        """Texto da lista de reposição para colar no WhatsApp ('' se não há itens)."""
        if not itens:
            return ""
        linhas = ["📋 *Lista de Reposição*\n"]
        for r in itens:
            linhas.append(
                f"• {r['nome']}: repor {r['qtd_repor']}un (atual: {r['quantidade']}, mín: {r['estoque_minimo']})"
            )
        return "\n".join(linhas)
