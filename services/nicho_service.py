"""
Serviço de Catálogo de Nichos, Fluxos de OS e Menus Dinâmicos (Estoke ao Cubo).

Contrato de serviço:
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Não conhece permissões (cruzamento com AuthService ocorre na camada de ponte/UI).
- Retorna dados de domínio e lança exceções de domínio (sem envelope {ok, data, error}).
"""
from __future__ import annotations

from typing import Any


# Catálogo oficial dos 18 tipos de negócio (movido de main.py::_show_first_access_screen)
NICHOS_CATALOGO: list[dict[str, str]] = [
    {
        "key": "pet_shop_agro",
        "title": "Pet Shop & Agro",
        "icon": "🐾",
        "desc": "Produtos, ração a granel,\nbanho, tosa e agendamento",
        "color": "#27AE60",
    },
    {
        "key": "oficina_mecanica",
        "title": "Oficina & Motopeças",
        "icon": "🔧",
        "desc": "OS completa, mecânicos,\npeças e dados do veículo",
        "color": "#D35400",
    },
    {
        "key": "farmacia_suplementos",
        "title": "Farmácia & Nutrição",
        "icon": "💊",
        "desc": "Controle por lote e validade,\nalertas de vencimento",
        "color": "#16A085",
    },
    {
        "key": "papelaria_livraria",
        "title": "Papelaria & Livraria",
        "icon": "📚",
        "desc": "Alto volume de SKUs, ISBN,\nleitor rápido e código barras",
        "color": "#8E44AD",
    },
    {
        "key": "otica_joalheria",
        "title": "Ótica & Joalheria",
        "icon": "👓",
        "desc": "Alto valor, nº de série único,\ngarantias e receitas",
        "color": "#E67E22",
    },
    {
        "key": "treinamento_recorrencia",
        "title": "Estúdios & Treino",
        "icon": "🥋",
        "desc": "Mensalidades, turmas/alunos\ne vendas no balcão",
        "color": "#2980B9",
    },
    {
        "key": "tecnologia_informatica",
        "title": "Tecnologia & TI",
        "icon": "💻",
        "desc": "Manutenção, upgrades, hardware,\nservidores e assistência",
        "color": "#0088FF",
    },
    {
        "key": "estetica_automotiva",
        "title": "Estética Automotiva",
        "icon": "🚗",
        "desc": "Car detailing, polimento,\nhigienização e lavagem",
        "color": "#E74C3C",
    },
    {
        "key": "moda_customizacao",
        "title": "Moda & Customização",
        "icon": "👗",
        "desc": "Boutiques, ateliês, ajustes\nrápidos e peças sob medida",
        "color": "#9B59B6",
    },
    {
        "key": "festas_eventos",
        "title": "Festas & Eventos",
        "icon": "🎉",
        "desc": "Suprimentos, locação de itens,\ndecoração e pegue-monte",
        "color": "#F39C12",
    },
    {
        "key": "estetica_arte_corporal",
        "title": "Arte Corporal & Estética",
        "icon": "✂️",
        "desc": "Tatuagem, barbearias, salões\ne agendamentos digitais",
        "color": "#1ABC9C",
    },
    {
        "key": "delivery_alimentacao",
        "title": "Delivery & Alimentação",
        "icon": "🍔",
        "desc": "Lanches, doces, combos ágeis\ne automação via WhatsApp",
        "color": "#E67E22",
    },
    {
        "key": "casa_construcao",
        "title": "Casa & Construção",
        "icon": "🔨",
        "desc": "Ferragens, suprimentos do dia a dia\ne gestão para condomínios",
        "color": "#34495E",
    },
    {
        "key": "loja_roupa",
        "title": "Loja de Roupas Geral",
        "icon": "👔",
        "desc": "Tamanhos P/M/G/GG, calçados\ne controle de trocas",
        "color": "#7BBDE8",
    },
    {
        "key": "padaria_acougue",
        "title": "Padaria & Açougue",
        "icon": "🥖",
        "desc": "Venda por peso (kg/g),\nbalança e perecíveis",
        "color": "#D4A373",
    },
    {
        "key": "restaurante",
        "title": "Restaurante & Bar",
        "icon": "🍽️",
        "desc": "Mesas, comandas digitais,\ncozinha e garçons",
        "color": "#E07B56",
    },
    {
        "key": "artesanato",
        "title": "Artesanato & Handmade",
        "icon": "🎨",
        "desc": "Produtos manuais, insumos\ne cálculo de materiais",
        "color": "#E8A87C",
    },
    {
        "key": "variados",
        "title": "Comércio Geral",
        "icon": "🏪",
        "desc": "Mercados, conveniências,\nprodutos diversos",
        "color": "#85C1E9",
    },
]


# Botões padrão da barra lateral antes das inserções por nicho
SIDEBAR_BASE_BUTTONS: list[str] = [
    "SALDO",
    "GASTOS",
    "CAIXA",
    "KITS",
    "INVENTÁRIO",
    "COMPRAS",
    "CATEGORIAS",
    "FORNECEDORES",
    "CLIENTES",
    "ANOTA TUDO",
    "VENDEDORES",
    "CONTAS PAGAR",
    "ETIQUETAS",
    "CONFIG",
    "CHAT IA",
]


# Tabela estável botão -> id de ação (movida da cadeia elif de main.py::sidebar_event)
# Nota: PROFISSIONAIS e MECÂNICOS mantêm 'clientes_manager' (bug de navegação registrado como pendente).
BOTAO_ACAO_MAP: dict[str, str] = {
    "GASTOS": "gastos_dialog",
    "KITS": "kit_manager",
    "INVENTÁRIO": "inventario_dialog",
    "COMPRAS": "sugestao_compras_dialog",
    "CHAT IA": "chat_ai",
    "SALDO": "saldo_dialog",
    "CAIXA": "caixa_dialog",
    "CATEGORIAS": "categorias_manager",
    "FORNECEDORES": "fornecedores_manager",
    "CLIENTES": "clientes_manager",
    "ANOTA TUDO": "anota_tudo_dialog",
    "VENDEDORES": "vendedores_dialog",
    "CONFIG": "config_dialog",
    "INSUMOS": "insumos_manager",
    "PRODUÇÃO": "producao_dialog",
    "TROCAS": "trocas_dialog",
    "REPOSIÇÃO": "lista_reposicao",
    "MESAS": "mesas_manager",
    "COMANDAS": "comandas_dialog",
    "COZINHA": "cozinha_dialog",
    "CONTAS PAGAR": "contas_pagar_dialog",
    "ETIQUETAS": "etiquetas_dialog",
    # Módulos condicionais por nicho
    "ORDEM SERVIÇO": "servico_dialog",
    "AJUSTES / ATELIÊ": "servico_dialog",
    "BANHO & TOSA": "servico_dialog",
    "HARDWARE": "insumos_manager",
    "RAÇÃO A GRANEL": "insumos_manager",
    "MEDICAMENTOS": "insumos_manager",
    "GARANTIAS": "trocas_dialog",
    "Nº SÉRIE": "trocas_dialog",
    "AGENDAMENTOS": "anota_tudo_dialog",
    "RESERVAS": "anota_tudo_dialog",
    "LOCAÇÃO": "anota_tudo_dialog",
    "ORÇAMENTOS": "anota_tudo_dialog",
    "LOTES & VALIDADE": "anota_tudo_dialog",
    "VENCIMENTOS": "anota_tudo_dialog",
    "VEÍCULOS": "clientes_manager",
    "CONDOMÍNIOS": "clientes_manager",
    "PROFISSIONAIS": "clientes_manager",
    "PETS & VACINAS": "clientes_manager",
    "MECÂNICOS": "clientes_manager",
    "RECEITAS & GRAUS": "clientes_manager",
    "ALUNOS & TURMAS": "clientes_manager",
    "MENSALIDADES": "clientes_manager",
    "COMBOS": "mesas_manager",
    "PEDIDOS / MESAS": "mesas_manager",
    "LEITOR RÁPIDO": "sell_product_dialog",
}


class NichoService:
    """Fornece catálogo de nichos, configurações de OS e estrutura da sidebar."""

    def __init__(self, config_module: Any = None):
        self._config = config_module

    def listar_nichos(self) -> list[dict[str, str]]:
        """Retorna cópia do catálogo completo dos 18 tipos de negócio."""
        return [dict(item) for item in NICHOS_CATALOGO]

    def get_os_config(self, nicho: str | None = None) -> dict[str, Any]:
        """Retorna status, cores, ícones e rótulos de OS adaptados ao tipo de negócio."""
        bt = nicho or "variados"
        if bt == "restaurante":
            statuses = ["Recebido", "Preparando", "Pronto", "Entregue", "Concluído"]
            colors = {
                "Recebido": "#3498DB",
                "Preparando": "#F39C12",
                "Pronto": "#27AE60",
                "Entregue": "#2ECC71",
                "Concluído": "#28a745",
            }
            icons = {
                "Recebido": "📥",
                "Preparando": "🍳",
                "Pronto": "✅",
                "Entregue": "🍽️",
                "Concluído": "🏁",
            }
            title = "🍽️ PEDIDOS"
            equip_label = "Prato / Pedido:"
            equip_placeholder = "Ex: X-Burguer, Pizza Calabresa, Combo 1..."
        elif bt == "padaria_acougue":
            statuses = ["Recebido", "Em Preparo", "Pronto p/ Retirada", "Entregue", "Concluído"]
            colors = {
                "Recebido": "#3498DB",
                "Em Preparo": "#F39C12",
                "Pronto p/ Retirada": "#27AE60",
                "Entregue": "#2ECC71",
                "Concluído": "#28a745",
            }
            icons = {
                "Recebido": "📥",
                "Em Preparo": "🥩",
                "Pronto p/ Retirada": "✅",
                "Entregue": "📦",
                "Concluído": "🏁",
            }
            title = "🥩 ENCOMENDAS"
            equip_label = "Produto / Encomenda:"
            equip_placeholder = "Ex: Bolo de Aniversário, 5kg Picanha, Pão Encomenda..."
        elif bt == "loja_roupa":
            statuses = ["Recebido", "Em Ajuste", "Pronto p/ Retirada", "Entregue", "Concluído"]
            colors = {
                "Recebido": "#3498DB",
                "Em Ajuste": "#9B59B6",
                "Pronto p/ Retirada": "#27AE60",
                "Entregue": "#2ECC71",
                "Concluído": "#28a745",
            }
            icons = {
                "Recebido": "📥",
                "Em Ajuste": "✂️",
                "Pronto p/ Retirada": "✅",
                "Entregue": "👗",
                "Concluído": "🏁",
            }
            title = "✂️ AJUSTES / COSTURAS"
            equip_label = "Peça / Item:"
            equip_placeholder = "Ex: Vestido Longo, Calça Jeans (barra), Terno..."
        elif bt == "artesanato":
            statuses = ["Recebido", "Em Produção", "Acabamento", "Pronto", "Entregue", "Concluído"]
            colors = {
                "Recebido": "#3498DB",
                "Em Produção": "#F39C12",
                "Acabamento": "#9B59B6",
                "Pronto": "#27AE60",
                "Entregue": "#2ECC71",
                "Concluído": "#28a745",
            }
            icons = {
                "Recebido": "📥",
                "Em Produção": "🎨",
                "Acabamento": "✨",
                "Pronto": "✅",
                "Entregue": "📦",
                "Concluído": "🏁",
            }
            title = "🎨 ENCOMENDAS"
            equip_label = "Peça / Encomenda:"
            equip_placeholder = "Ex: Quadro Personalizado, Vaso Cerâmica, Kit Presente..."
        else:  # variados / assistência técnica
            statuses = ["Recebido", "Em Análise", "Aguardando Peça", "Em Execução", "Pronto", "Entregue", "Concluído"]
            colors = {
                "Recebido": "#3498DB",
                "Em Análise": "#F39C12",
                "Aguardando Peça": "#E67E22",
                "Em Execução": "#9B59B6",
                "Pronto": "#27AE60",
                "Entregue": "#2ECC71",
                "Concluído": "#28a745",
            }
            icons = {
                "Recebido": "📥",
                "Em Análise": "🔍",
                "Aguardando Peça": "⏳",
                "Em Execução": "🔧",
                "Pronto": "✅",
                "Entregue": "📦",
                "Concluído": "🏁",
            }
            title = "🔧 SERVIÇOS (OS)"
            equip_label = "Equipamento / Item:"
            equip_placeholder = "Ex: iPhone 14, Notebook Dell, Motor X..."

        return {
            "statuses": statuses,
            "colors": colors,
            "icons": icons,
            "title": title,
            "equip_label": equip_label,
            "equip_placeholder": equip_placeholder,
        }

    def acao_do_botao(self, texto: str) -> str:
        """Retorna o identificador estável de ação para o texto do botão da sidebar."""
        acao = BOTAO_ACAO_MAP.get(str(texto or "").strip())
        if not acao:
            raise ValueError(f"Botão de sidebar desconhecido: '{texto}'")
        return acao

    def get_sidebar_buttons(self, nicho: str | None = None) -> list[dict[str, str]]:
        """
        Monta a lista ordenada de botões da sidebar para o nicho informado,
        preservando exatamente a ordem histórica de inserção da UI.
        Cada item contém {'texto': str, 'acao': str}.
        """
        bt = nicho or "variados"
        botoes = list(SIDEBAR_BASE_BUTTONS)

        if bt == "pet_shop_agro":
            botoes.insert(2, "BANHO & TOSA")
            botoes.insert(3, "PETS & VACINAS")
            botoes.insert(4, "RAÇÃO A GRANEL")
        if bt == "oficina_mecanica":
            botoes.insert(2, "ORDEM SERVIÇO")
            botoes.insert(3, "MECÂNICOS")
            botoes.insert(4, "VEÍCULOS")
            botoes.insert(5, "ORÇAMENTOS")
        if bt == "farmacia_suplementos":
            botoes.insert(2, "LOTES & VALIDADE")
            botoes.insert(3, "VENCIMENTOS")
        if bt == "papelaria_livraria":
            botoes.insert(2, "LEITOR RÁPIDO")
            botoes.insert(3, "REPOSIÇÃO")
        if bt == "otica_joalheria":
            botoes.insert(2, "RECEITAS & GRAUS")
            botoes.insert(3, "Nº SÉRIE")
            botoes.insert(4, "GARANTIAS")
        if bt == "treinamento_recorrencia":
            botoes.insert(2, "MENSALIDADES")
            botoes.insert(3, "ALUNOS & TURMAS")
        if bt in ("padaria_acougue", "artesanato", "moda_customizacao", "delivery_alimentacao", "estetica_automotiva"):
            botoes.insert(2, "INSUMOS")
            botoes.insert(3, "PRODUÇÃO")
        if bt in ("loja_roupa", "moda_customizacao"):
            botoes.insert(2, "TROCAS")
            if bt == "moda_customizacao":
                botoes.insert(3, "AJUSTES / ATELIÊ")
        if bt == "variados":
            botoes.insert(2, "REPOSIÇÃO")
        if bt in ("restaurante", "delivery_alimentacao"):
            botoes.insert(2, "PEDIDOS / MESAS")
            botoes.insert(3, "COZINHA")
            if bt == "delivery_alimentacao":
                botoes.insert(4, "COMBOS")
        if bt == "tecnologia_informatica":
            botoes.insert(2, "ORDEM SERVIÇO")
            botoes.insert(3, "HARDWARE")
            botoes.insert(4, "GARANTIAS")
        if bt == "estetica_automotiva":
            botoes.insert(2, "AGENDAMENTOS")
            botoes.insert(3, "VEÍCULOS")
        if bt == "festas_eventos":
            botoes.insert(2, "LOCAÇÃO")
            botoes.insert(3, "RESERVAS")
        if bt == "estetica_arte_corporal":
            botoes.insert(2, "AGENDAMENTOS")
            botoes.insert(3, "PROFISSIONAIS")
        if bt == "casa_construcao":
            botoes.insert(2, "ORÇAMENTOS")
            botoes.insert(3, "CONDOMÍNIOS")

        return [{"texto": txt, "acao": self.acao_do_botao(txt)} for txt in botoes]
