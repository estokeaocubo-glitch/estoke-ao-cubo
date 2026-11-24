import pandas as pd
# from sklearn.linear_model import LinearRegression (Importação movida para uso preguiçoso para acelerar startup)
import datetime
import re
import os
import random
import difflib
import logging
from dotenv import load_dotenv

logger = logging.getLogger('EstokeAoCubo.ai')

# Carregar variáveis de ambiente
load_dotenv()

# Importar Gemini (SDK oficial google-genai)
GEMINI_LIB_AVAILABLE = False
genai = None
try:
    from google import genai
    GEMINI_LIB_AVAILABLE = True
except ImportError:
    logger.info("Biblioteca google-genai não encontrada (modo offline).")

class ResponseGenerator:
    """Gera respostas variadas e dinâmicas para o sistema."""
    
    RESPONSES = {
        "add_gasto_success": [
            "✅ Gasto de R$ {valor:.2f} registrado em '{categoria}'.",
            "💸 Anotei aqui: R$ {valor:.2f} gastos com {descricao}.",
            "📝 Feito! Gasto de R$ {valor:.2f} adicionado ao sistema."
        ],
        "add_produto_success": [
            "✅ Produto '{nome}' adicionado com sucesso! ({qtd} un a R$ {preco:.2f})",
            "📦 Estoque atualizado: {nome} agora faz parte do inventário.",
            "👍 Tudo certo. Cadastrei {qtd} unidades de {nome}."
        ],
        "add_meta_success": [
            "🎯 Meta '{titulo}' criada! Vamos alcançar isso.",
            "🚀 Nova meta definida: {titulo}. Foco total!",
            "✅ Meta registrada. Descrição: {descricao}"
        ],
        "add_saldo_success": [
            "💰 R$ {valor:.2f} adicionado ao saldo. O caixa agradece!",
            "📈 Entrada registrada: R$ {valor:.2f}.",
            "✅ Saldo atualizado com sucesso (+ R$ {valor:.2f})."
        ],
        "sale_success": [
            "✅ Venda registrada: {qtd}x {nome}. Total: R$ {total:.2f}",
            "📉 Saída de estoque confirmada: {qtd} unidades de {nome}.",
            "💸 Venda realizada! {nome} (x{qtd}) saiu do estoque."
        ],
        "sale_error_stock": [
            "⚠️ Estoque insuficiente para {nome}. Disponível: {disponivel}.",
            "❌ Não posso vender {qtd} de {nome}. Só temos {disponivel} no momento.",
            "🚫 Operação cancelada. Estoque de {nome} é apenas {disponivel}."
        ],
        "not_found": [
            "❌ Não encontrei '{termo}'.",
            "🔍 '{termo}' não consta no banco de dados.",
            "❓ O que seria '{termo}'? Não achei aqui."
        ]
    }

    @staticmethod
    def get(key, **kwargs):
        """Retorna uma resposta aleatória formatada com os dados fornecidos."""
        templates = ResponseGenerator.RESPONSES.get(key, ["Ação concluída."])
        template = random.choice(templates)
        try:
            return template.format(**kwargs)
        except KeyError:
            return template

class SimpleLinearRegression:
    """Regressão Linear nativa de alta velocidade (substitui scikit-learn economizando ~75MB)."""
    def __init__(self):
        self.slope = 0.0
        self.intercept = 0.0

    def fit(self, X, y):
        if hasattr(X, 'values'):
            x_vals = X.values.flatten()
        else:
            x_vals = [item[0] if isinstance(item, (list, tuple)) else item for item in X]
        
        if hasattr(y, 'values'):
            y_vals = y.values.flatten()
        else:
            y_vals = list(y)
        
        n = len(x_vals)
        if n <= 1:
            self.slope = 0.0
            self.intercept = float(y_vals[0]) if n == 1 else 0.0
            return self

        mean_x = sum(x_vals) / n
        mean_y = sum(y_vals) / n
        
        num = sum((x_vals[i] - mean_x) * (y_vals[i] - mean_y) for i in range(n))
        den = sum((x_vals[i] - mean_x) ** 2 for i in range(n))
        
        if den != 0:
            self.slope = num / den
            self.intercept = mean_y - (self.slope * mean_x)
        else:
            self.slope = 0.0
            self.intercept = mean_y
        return self

    def predict(self, X):
        if hasattr(X, 'values'):
            x_vals = X.values.flatten()
        else:
            x_vals = [item[0] if isinstance(item, (list, tuple)) else item for item in X]
        return [float(self.slope * x + self.intercept) for x in x_vals]

class ExpensePredictor:
    def __init__(self, db):
        self.db = db
        self.model = None
        self.is_trained = False
        self.gemini_available = False
        self.genai_client = None
        self.configure_ai()

    def configure_ai(self):
        """Configura a API do Gemini procurando a chave no env ou em arquivo"""
        if not GEMINI_LIB_AVAILABLE or genai is None:
            self.gemini_available = False
            self.genai_client = None
            return

        api_key = os.getenv("GEMINI_API_KEY")
        
        # Tentar ler do arquivo seguro se não estiver no env
        key_file = os.path.join(self.db.data_dir, "gemini_key.txt")
        if not api_key and os.path.exists(key_file):
            try:
                with open(key_file, "r") as f:
                    api_key = f.read().strip()
            except Exception as e:
                logger.warning("Erro ao ler arquivo de chave: %s", e)

        if api_key:
            # Validação básica de formato da chave
            if len(api_key) < 10 or ' ' in api_key:
                logger.warning("Chave de API inválida (formato incorreto). Ignorando.")
                self.gemini_available = False
                self.genai_client = None
                return
            try:
                self.api_key = api_key
                self.genai_client = genai.Client(api_key=api_key)
                self.gemini_available = True
                logger.info("Gemini AI (google-genai) ativado com sucesso.")
            except Exception as e:
                logger.error("Erro ao configurar Gemini: %s", e)
                self.gemini_available = False
                self.genai_client = None
        else:
            self.gemini_available = False
            self.genai_client = None

    def prepare_data(self):
        df = self.db.listar_gastos()
        if df.empty:
            return None, None
        
        # Converter data para ordinal para regressão (com tratamento de datas inválidas)
        try:
            df['data'] = pd.to_datetime(df['data'], format='%Y-%m-%d', errors='coerce')
        except:
            # Se falhar, tentar com formato misto
            df['data'] = pd.to_datetime(df['data'], format='mixed', errors='coerce')
        
        # Remover linhas com datas inválidas
        df = df.dropna(subset=['data'])
        
        if df.empty:
            return None, None
        
        df['data_ordinal'] = df['data'].apply(lambda x: x.toordinal())
        
        X = df[['data_ordinal']]
        y = df['valor']
        return X, y

    def train(self):
        if self.model is None:
            self.model = SimpleLinearRegression()

        X, y = self.prepare_data()
        if X is not None and len(X) > 1:
            self.model.fit(X, y)
            self.is_trained = True
            return True
        return False

    def ask_gemini(self, pergunta):
        """Faz uma pergunta ao Gemini sobre dados do sistema com contexto rico."""
        if not self.gemini_available or self.genai_client is None:
            return None
        
        try:
            # Preparar contexto completo com dados atuais
            df_gastos = self.db.listar_gastos()
            df_produtos = self.db.listar_produtos()
            df_metas = self.db.listar_metas()
            df_clientes = self.db.listar_clientes()
            df_vendedores = self.db.listar_vendedores()
            df_fornecedores = self.db.listar_fornecedores()

            # Financeiro
            saldo = self.db.get_saldo_atual()
            total_gastos = df_gastos['valor'].sum() if not df_gastos.empty else 0
            total_fiado = self.db.total_fiado_pendente()
            total_contas_pagar = self.db.total_contas_pagar_pendente()

            # Vendas
            df_vendas = self.db.listar_vendas()
            hoje = datetime.date.today()
            vendas_hoje = 0
            vendas_mes = 0
            if not df_vendas.empty:
                try:
                    col_data = 'data_venda' if 'data_venda' in df_vendas.columns else 'data'
                    df_vendas['data_dt'] = pd.to_datetime(df_vendas[col_data], errors='coerce').dt.date
                    vendas_hoje = df_vendas[df_vendas['data_dt'] == hoje]['valor_total'].sum()
                    inicio_mes = hoje.replace(day=1)
                    vendas_mes = df_vendas[df_vendas['data_dt'] >= inicio_mes]['valor_total'].sum()
                except Exception:
                    pass

            # Lucro
            res_lucro = self.db.lucro_por_periodo('mes')
            lucro_mes = res_lucro[0] if isinstance(res_lucro, tuple) else float(res_lucro or 0.0)

            # Estoque
            total_itens = df_produtos['quantidade'].sum() if not df_produtos.empty else 0
            valor_estoque = (df_produtos['quantidade'] * df_produtos['preco']).sum() if not df_produtos.empty else 0
            produtos_baixo = self.db.produtos_abaixo_estoque_minimo()
            num_baixo = len(produtos_baixo) if not produtos_baixo.empty else 0

            # Produtos vencendo
            vencendo = self.db.verificar_produtos_vencendo(dias=7)
            num_vencendo = len(vencendo) if not vencendo.empty else 0

            # Top produtos
            top_ids = self.db.obter_produtos_mais_vendidos(5)
            top_nomes = []
            if top_ids and not df_produtos.empty:
                for pid in top_ids[:5]:
                    p = df_produtos[df_produtos['id'] == pid]
                    if not p.empty:
                        top_nomes.append(p.iloc[0]['nome'])

            # Ranking vendas
            ranking = self.db.ranking_produtos_vendidos(5)
            ranking_txt = ""
            if not ranking.empty:
                ranking_txt = "; ".join([f"{r['nome']}({r['total_vendido']}un, R${r['faturamento']:.0f})" for _, r in ranking.head(5).iterrows()])

            # Comparativo mensal
            comp = self.db.comparativo_mensal()
            comp_txt = ""
            if comp:
                fat_at = comp.get('atual', {}).get('faturamento', comp.get('faturamento_atual', 0))
                fat_an = comp.get('anterior', {}).get('faturamento', comp.get('faturamento_anterior', 0))
                comp_txt = f"Faturamento mês atual: R${fat_at:.2f}, mês anterior: R${fat_an:.2f}"

            # Vendedores
            vendedores_txt = ""
            if not df_vendedores.empty:
                vendedores_txt = ", ".join(df_vendedores['nome'].tolist()[:10])

            # Contas a pagar vencendo
            contas_vencendo = self.db.contas_vencendo(dias=7)
            num_contas_vencendo = len(contas_vencendo) if not contas_vencendo.empty else 0

            contexto = f"""Você é o assistente de gestão inteligente do EstokeAoCubo, um sistema completo para pequenas empresas brasileiras.
Responda SEMPRE em português do Brasil, de forma clara, objetiva e profissional. Use emojis para deixar a leitura agradável.
Se o usuário pedir um comando de ação (adicionar produto, registrar gasto, etc.), explique que ele pode digitar o comando no chat.

DADOS ATUAIS DO NEGÓCIO:
📊 Financeiro:
  - Saldo em caixa: R$ {saldo:.2f}
  - Total de gastos registrados: R$ {total_gastos:.2f}
  - Faturamento do mês: R$ {vendas_mes:.2f}
  - Vendas hoje: R$ {vendas_hoje:.2f}
  - Lucro líquido do mês: R$ {lucro_mes:.2f}
  - Fiado pendente (a receber): R$ {total_fiado:.2f}
  - Contas a pagar pendentes: R$ {total_contas_pagar:.2f}

📦 Estoque:
  - Total de itens: {total_itens} unidades
  - Valor do estoque: R$ {valor_estoque:.2f}
  - Produtos com estoque baixo: {num_baixo}
  - Produtos vencendo em 7 dias: {num_vencendo}
  - Total de produtos cadastrados: {len(df_produtos)}

🏆 Top 5 mais vendidos: {ranking_txt if ranking_txt else 'Sem vendas ainda'}
📈 {comp_txt if comp_txt else 'Sem dados de comparativo'}

👥 Equipe:
  - Clientes cadastrados: {len(df_clientes)}
  - Vendedores: {vendedores_txt if vendedores_txt else 'Nenhum cadastrado'}
  - Fornecedores: {len(df_fornecedores)}

⚠️ Alertas:
  - {num_baixo} produto(s) com estoque baixo
  - {num_vencendo} produto(s) vencendo em 7 dias
  - {num_contas_vencendo} conta(s) a pagar vencendo em 7 dias

🎯 Metas ativas: {len(df_metas) if not df_metas.empty else 0}

COMANDOS QUE O USUÁRIO PODE DIGITAR NO CHAT:
  - "Registre um gasto de 50 em Material para compra de tintas"
  - "Adicione um produto Caneta com 10 unidades a 2 reais"
  - "Adicione 500 reais ao saldo"
  - "Vender 3 unidades de Camiseta"
  - "Qual meu saldo?" / "Quanto vendi hoje?" / "Qual meu lucro do mês?"
  - "Quais produtos estão com estoque baixo?"
  - "Quais contas vencem essa semana?"
  - "Quanto de fiado tenho pendente?"

Pergunta do usuário: {pergunta}
"""
            
            model_candidates = [
                "gemini-2.5-flash",
                "gemini-2.0-flash",
                "gemini-2.0-flash-lite",
            ]

            last_error = None
            for model_name in model_candidates:
                try:
                    logger.debug("Tentando conectar com modelo: %s", model_name)
                    response = self.genai_client.models.generate_content(
                        model=model_name,
                        contents=contexto,
                    )
                    return response.text
                except Exception as e:
                    logger.debug("Falha ao tentar %s: %s", model_name, e)
                    last_error = e
            
            # Se chegou aqui, todos falharam
            if last_error:
                logger.error("Todos os modelos Gemini falharam. Último erro: %s", last_error)
            return None
            
        except Exception as e:
            logger.error("Erro ao usar Gemini: %s", e)
            return None

    def predict_next_month(self):
        """
        Calcula a previsão de gastos para o próximo mês usando a fórmula:
        G_total = Sum(F) + Sum(P_i * Q_avg_i) * (1 + alpha)
        
        Onde:
        - Sum(F): Soma dos gastos fixos (último valor registrado de cada descrição única marcada como 'Fixo')
        - P_i * Q_avg_i: Média mensal de gastos variáveis por categoria/descrição (últimos 3 meses)
        - alpha: Margem de segurança (10%)
        """
        df = self.db.listar_gastos()
        if df.empty:
            return 0.0
            
        # Garantir que a coluna 'tipo' existe (para compatibilidade com BD antigo sem migração imediata)
        if 'tipo' not in df.columns:
            df['tipo'] = 'Variável'
            
        # Converter data
        try:
            df['data'] = pd.to_datetime(df['data'], format='mixed', dayfirst=False, errors='coerce')
        except:
            df['data'] = pd.to_datetime(df['data'], errors='coerce')
            
        df = df.dropna(subset=['data'])
        if df.empty:
            return 0.0

        # 1. Calcular Gastos Fixos (Sum F)
        # Pegar o último valor de cada gasto fixo único (identificado pela descrição)
        gastos_fixos = df[df['tipo'] == 'Fixo']
        total_fixo = 0.0
        
        if not gastos_fixos.empty:
            # Agrupar por descrição e pegar o valor mais recente
            # Ordenar por data para garantir que pegamos o último
            gastos_fixos = gastos_fixos.sort_values('data')
            # Drop duplicates keep last
            ultimos_fixos = gastos_fixos.drop_duplicates(subset=['descricao'], keep='last')
            total_fixo = ultimos_fixos['valor'].sum()

        # 2. Calcular Gastos Variáveis (Sum P_i * Q_avg_i)
        # Vamos calcular a média mensal dos últimos 3 meses para gastos variáveis
        gastos_variaveis = df[df['tipo'] != 'Fixo']
        total_variavel_medio = 0.0
        
        if not gastos_variaveis.empty:
            # Filtrar últimos 90 dias
            data_limite = datetime.datetime.now() - datetime.timedelta(days=90)
            recentes = gastos_variaveis[gastos_variaveis['data'] >= data_limite]
            
            if not recentes.empty:
                # Soma total dos últimos 3 meses
                soma_recente = recentes['valor'].sum()
                # Média mensal (dividir por 3, ou pelo número de meses disponíveis se for menos)
                # Se tiver dados de apenas 1 mês, divide por 1. Se tiver de 3, por 3.
                meses_disponiveis = (recentes['data'].max() - recentes['data'].min()).days / 30
                meses_disponiveis = max(1, round(meses_disponiveis)) # Pelo menos 1
                
                total_variavel_medio = soma_recente / 3 # Assumindo média trimestral padrão para projeção
            else:
                # Se não tiver dados recentes, usar média geral
                total_variavel_medio = gastos_variaveis['valor'].mean() if len(gastos_variaveis) > 0 else 0

        # 3. Aplicar Margem (alpha = 10%)
        alpha = 0.10
        
        # Fórmula Final
        g_total = total_fixo + (total_variavel_medio * (1 + alpha))
        
        return g_total

    # --- NOVA IMPLEMENTAÇÃO DE COMANDOS LOCAIS (OFFLINE MODE) ---
    
    def process_local_command(self, command, imagem_path=None):
        """
        Processa comandos e perguntas localmente usando grupos de palavras-chave.
        Substitui e expande a antiga execute_action.
        """
        original = command
        command = command.lower().strip()

        # --- SAUDAÇÕES E CONVERSAÇÃO ---
        saudacoes = ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "eae", "eai",
                     "fala", "salve", "hey", "hello", "hi", "e aí", "e ai", "opa"]
        agradecimentos = ["obrigado", "obrigada", "valeu", "vlw", "brigado", "brigada",
                         "thanks", "grato", "grata", "tmj", "muito obrigado"]
        despedidas = ["tchau", "até mais", "ate mais", "flw", "falou", "até logo", "bye",
                     "adeus", "t+", "fui"]

        if command in saudacoes or any(command.startswith(s + " ") or command.startswith(s + ",") or command.startswith(s + "!") for s in saudacoes):
            hora = datetime.datetime.now().hour
            if hora < 12:
                saudacao = "Bom dia"
            elif hora < 18:
                saudacao = "Boa tarde"
            else:
                saudacao = "Boa noite"
            return True, f"👋 {saudacao}! Sou o assistente do EstokeAoCubo. Como posso ajudar?\n\nDigite 'ajuda' para ver o que posso fazer."

        if command in agradecimentos or any(command.startswith(a) for a in agradecimentos):
            respostas = ["😊 De nada! Estou aqui se precisar.", "👍 Por nada! Qualquer coisa, é só chamar.",
                        "😄 Disponha! Precisando, estou aqui.", "🤝 Sempre às ordens!"]
            return True, random.choice(respostas)

        if command in despedidas or any(command.startswith(d) for d in despedidas):
            return True, "👋 Até mais! Boas vendas! 🚀"

        # --- AJUDA ---
        if command in ["ajuda", "help", "comandos", "o que voce faz", "o que você faz",
                       "quais comandos", "menu", "opcoes", "opções"]:
            return True, (
                "🤖 **Assistente EstokeAoCubo — O que posso fazer:**\n\n"
                "📋 **CONSULTAS:**\n"
                "  • 'Qual meu saldo?' / 'Quanto tenho em caixa?'\n"
                "  • 'Quanto vendi hoje?' / 'Vendas da semana'\n"
                "  • 'Qual meu lucro do mês?'\n"
                "  • 'Quais produtos estão com estoque baixo?'\n"
                "  • 'Quais produtos estão vencendo?'\n"
                "  • 'Quanto de fiado tenho pendente?'\n"
                "  • 'Contas a pagar vencendo?'\n"
                "  • 'Quem mais vendeu este mês?' (vendedores)\n"
                "  • 'Qual forma de pagamento mais usada?'\n"
                "  • 'Qual produto mais vendido?'\n"
                "  • 'Listar produtos' / 'Listar clientes'\n"
                "  • 'Resumo do dia' / 'Resumo do mês'\n\n"
                "⚡ **COMANDOS:**\n"
                "  • 'Registre um gasto de 50 em Material'\n"
                "  • 'Adicione produto Caneta com 10 unidades a 2 reais'\n"
                "  • 'Adicione 500 ao saldo'\n"
                "  • 'Vender 3 unidades de Camiseta'\n"
                "  • 'Crie uma meta Crescer 20%'\n"
                "  • 'Adicione fornecedor Tintas ABC'\n\n"
                "🔮 **ANÁLISES:**\n"
                "  • 'Previsão de gastos'\n"
                "  • 'Uma dica para meu negócio'\n"
                "  • 'Resumo financeiro'\n\n"
                "💡 Dica: Prefixe com 'ia:' para perguntar direto ao Google Gemini."
            )

        # --- NORMALIZAÇÃO DO PORTUGUÊS ---
        # Corrigir erros comuns de digitação e variações
        substituicoes = {
            "pra ": "para ", "pro ": "para o ", "pros ": "para os ",
            "to ": "estou ", "tô ": "estou ", "tá ": "está ",
            "tb ": "também ", "tbm ": "também ",
            "qto ": "quanto ", "qnto ": "quanto ", "qts ": "quantos ",
            "qnts ": "quantos ", "qnt ": "quantidade ",
            "hj ": "hoje ", "seman ": "semana ",
            "prod ": "produto ", "prods ": "produtos ",
            "est ": "estoque ", "estoq ": "estoque ",
            "forne ": "fornecedor ", "fornec ": "fornecedor ",
            "cli ": "cliente ", "clis ": "clientes ",
            "vend ": "venda ", "vends ": "vendas ",
            "funcion ": "funcionário ", "func ": "funcionário ",
            "comiss ": "comissão ", "comis ": "comissão ",
            "relat ": "relatório ", "relatorio ": "relatório ",
            "preço ": "preco ", "valor ": "valor ",
            "adcionar ": "adicionar ", "adcione ": "adicione ",
            "adionar ": "adicionar ", "adione ": "adicione ",
            "cadatrar ": "cadastrar ", "cadrastrar ": "cadastrar ",
            "reeistrar ": "registrar ", "rejistrar ": "registrar ",
            "resgistrar ": "registrar ",
        }
        command_norm = command
        for errado, correto in substituicoes.items():
            command_norm = command_norm.replace(errado, correto)

        # --- FUZZY MATCHING (Tolerância a erros) ---
        intent_map = {
            "adicionar": ["adicione", "registre", "criar", "nova", "novo", "lançar", "add",
                         "insira", "adcionar", "cadastrar", "cadastre", "inclua", "incluir",
                         "coloque", "colocar", "bota", "botar", "põe", "colocar"],
            "vender": ["vender", "venda", "saída", "baixa", "vendido", "saida", "vendas",
                      "despachar", "saiu"],
            "remover": ["remover", "excluir", "deletar", "apagar", "remove", "limpar",
                       "tirar", "retirar", "eliminar"],
            "editar": ["editar", "alterar", "mudar", "atualizar", "edit", "modificar",
                      "trocar", "corrigir"],
            "consultar": ["quanto", "qual", "quais", "listar", "mostre", "ver", "relatório",
                         "total", "gastos", "estoque", "saldo", "lucro", "caixa", "fiado",
                         "vendas", "clientes", "produtos", "vencendo", "baixo", "ranking",
                         "forma de pagamento", "vendedor", "vendedores", "contas", "resumo",
                         "faturamento", "receita", "mais vendido", "pendente", "devendo",
                         "comparativo", "comparação", "mês anterior", "fornecedor",
                         "fornecedores", "meta", "metas", "reposição", "reposicao",
                         "acabando", "repor"],
            "analisar": ["previsão", "futuro", "analisar", "sugestão", "dica", "otimizar",
                        "melhorar", "conselho", "recomendação", "sugerir", "análise",
                        "projeção", "projecao", "tendência", "tendencia",
                        "dre", "demonstrativo", "fluxo de caixa", "comissão", "comissao",
                        "comissões"]
        }
        
        # Verificar correspondência direta ou fuzzy
        words = command_norm.split()
        detected_intent = None
        
        for intent, keywords in intent_map.items():
            if any(k in command_norm for k in keywords):
                detected_intent = intent
                break
            for word in words:
                matches = difflib.get_close_matches(word, keywords, n=1, cutoff=0.9)
                if matches:
                    detected_intent = intent
                    break
            if detected_intent: break

        # Roteamento baseado na intenção detectada
        result = None
        if detected_intent == "adicionar":
            success, msg = self._handle_creation(command_norm, imagem_path=imagem_path)
            if success: return True, msg
            if not success and msg and not GEMINI_LIB_AVAILABLE: return False, msg
        elif detected_intent == "vender":
            success, msg = self._handle_sales(command_norm)
            if success: return True, msg
            if not success and msg and not GEMINI_LIB_AVAILABLE: return False, msg
        elif detected_intent == "remover":
            return self._handle_deletion(command_norm)
        elif detected_intent == "editar":
            return self._handle_update(command_norm)
        elif detected_intent == "consultar":
            return self._handle_query(command_norm)
        elif detected_intent == "analisar":
            return self._handle_analysis(command_norm)

        return False, None

    def _handle_creation(self, command, imagem_path=None):
        # Adicionar Gasto
        if "gasto" in command:
            valor_match = re.search(r'(\d+(?:\.\d{2})?)', command)
            if not valor_match:
                return False, "Para registrar gasto, especifique o valor. Ex: 'Registre um gasto de 50 em Material'"
            
            valor = float(valor_match.group(1))
            categoria = "Outro"
            descricao = "Gasto via IA"
            
            for cat in ["Material", "Transporte", "Serviços", "Aluguel", "Impostos", "Marketing"]:
                if cat.lower() in command:
                    categoria = cat
                    break
            
            desc_match = re.search(r'para\s+(.+?)(?:\s|$)', command) or re.search(r'descrição\s+(.+?)(?:\s|$)', command)
            if desc_match:
                descricao = desc_match.group(1)
            
            try:
                data = datetime.date.today().isoformat()
                self.db.adicionar_gasto(data, valor, categoria, descricao)
                return True, ResponseGenerator.get("add_gasto_success", valor=valor, categoria=categoria, descricao=descricao)
            except Exception as e:
                return False, f"❌ Erro ao registrar gasto: {str(e)}"

        # Adicionar Produto
        elif "produto" in command:
            valor_match = re.search(r'(\d+(?:\.\d{2})?)\s*reais?', command)
            qtd_match = re.search(r'(\d+)\s*unid', command)
            nome_match = re.search(r"produto\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s+com|\s+a\s|\s*$)", command)
            
            if not (valor_match and qtd_match and nome_match):
                return False, "Use: 'adicione um produto [Nome] com [Qtd] unidades a [Preço] reais'"
            
            nome = nome_match.group(1).strip()
            qtd = int(qtd_match.group(1))
            preco = float(valor_match.group(1))
            categoria = "Produto Pronto"
            
            for cat in ["Material", "Produto Pronto", "Insumo", "Outro"]:
                if cat.lower() in command:
                    categoria = cat
                    break
            
            try:
                img_nome = None
                if imagem_path and os.path.isfile(imagem_path):
                    img_dir = os.path.join(self.db.data_dir, "product_images")
                    os.makedirs(img_dir, exist_ok=True)
                    ext = os.path.splitext(imagem_path)[1]
                    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                    safe = "".join([c for c in nome if c.isalnum() or c in (' ', '-', '_')]).strip()
                    img_nome = f"prod_{safe}_{ts}{ext}"
                    try:
                        from PIL import Image as PILImg
                        pil = PILImg.open(imagem_path)
                        pil.thumbnail((800, 800))
                        pil.save(os.path.join(img_dir, img_nome))
                    except Exception:
                        import shutil
                        shutil.copy2(imagem_path, os.path.join(img_dir, img_nome))
                self.db.adicionar_produto(nome, qtd, preco, categoria, imagem=img_nome)
                msg = ResponseGenerator.get("add_produto_success", nome=nome, qtd=qtd, preco=preco)
                if img_nome:
                    msg += " 📷 Imagem salva!"
                return True, msg
            except Exception as e:
                return False, f"❌ Erro ao adicionar produto: {str(e)}"

        # Adicionar Meta
        elif "meta" in command:
            titulo_match = re.search(r"meta\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s+com|\s*$)", command)
            if not titulo_match:
                return False, "Use: 'crie uma meta [Título] com descrição [Descrição]'"
            
            titulo = titulo_match.group(1).strip()
            descricao = "Meta via IA"
            desc_match = re.search(r"descrição\s+(.+?)(?:\s|$)", command)
            if desc_match: descricao = desc_match.group(1)
            
            try:
                self.db.adicionar_meta(titulo, descricao)
                return True, ResponseGenerator.get("add_meta_success", titulo=titulo, descricao=descricao)
            except Exception as e:
                return False, f"❌ Erro: {str(e)}"

        # Adicionar Saldo/Entrada
        elif "saldo" in command or "entrada" in command:
            valor_match = re.search(r'(\d+(?:\.\d{2})?)', command)
            if not valor_match: return False, "Especifique o valor para adicionar ao saldo."
            valor = float(valor_match.group(1))
            try:
                self.db.adicionar_entrada(valor, "Entrada via IA")
                return True, ResponseGenerator.get("add_saldo_success", valor=valor)
            except Exception as e:
                return False, f"❌ Erro: {str(e)}"
                
        # Adicionar Fornecedor
        elif "fornecedor" in command:
            nome_match = re.search(r"fornecedor\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s+com|\s*$)", command)
            if not nome_match: return False, "Use: 'adicione fornecedor [Nome] com contato [Contato]'"
            nome = nome_match.group(1).strip()
            contato = "N/A"
            contato_match = re.search(r"contato\s+(.+?)(?:\s|$)", command)
            if contato_match: contato = contato_match.group(1)
            
            try:
                self.db.adicionar_fornecedor(nome, contato, "Geral")
                return True, f"✅ Fornecedor '{nome}' adicionado."
            except Exception as e:
                return False, f"❌ Erro: {str(e)}"

        return False, "Comando de criação não reconhecido. Tente: gasto, produto, meta, saldo, fornecedor."

    def _handle_sales(self, command):
        # Registrar Venda
        # Ex: "vender 5 unidades de Tinta"
        qtd_match = re.search(r'(\d+)\s*unid', command)
        nome_match = re.search(r"de\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s*$)", command)
        
        # Slot Filling Simplificado: Se não achar "de X", tenta pegar o resto da frase
        if not nome_match and qtd_match:
             # Tenta pegar tudo depois de "unidades" ou "unid"
             resto = command.split(qtd_match.group(0))[1].strip()
             # Remove preposições comuns
             if resto.startswith("de "): resto = resto[3:]
             if resto: nome_match = re.match(r"(.+)", resto)

        if qtd_match and nome_match:
            qtd = int(qtd_match.group(1))
            nome_busca = nome_match.group(1).strip()
            
            # Buscar produto pelo nome (Fuzzy Match no nome do produto)
            df = self.db.listar_produtos()
            produto = None
            melhor_match = None
            maior_ratio = 0
            
            for _, row in df.iterrows():
                ratio = difflib.SequenceMatcher(None, nome_busca.lower(), row['nome'].lower()).ratio()
                if ratio > 0.6 and ratio > maior_ratio: # 60% de similaridade mínima
                    maior_ratio = ratio
                    melhor_match = row
            
            if melhor_match is not None:
                produto = melhor_match
                # Se a similaridade não for perfeita, avisar (Proactive Trigger)
                aviso_nome = ""
                if maior_ratio < 0.9:
                    aviso_nome = f" (Assumi que você quis dizer '{produto['nome']}')"

                try:
                    valor_total = qtd * produto['preco']
                    res_ai = self.db.registrar_venda_detalhada(
                        [{
                            'id': int(produto['id']),
                            'nome': produto['nome'],
                            'qtd': int(qtd),
                            'preco': float(produto['preco']),
                            'preco_compra': float(produto.get('preco_compra') or 0.0),
                            'type': 'produto'
                        }],
                        valor_total,
                        0.0,
                        "Consumidor Final",
                        "Dinheiro",
                        vendedor="Assistente IA"
                    )
                    if isinstance(res_ai, tuple) and not res_ai[0]:
                        return False, f"❌ Erro ao vender: {res_ai[1]}"
                    
                    # Proactive Trigger: Estoque Baixo Pós-Venda
                    nova_qtd = produto['quantidade'] - qtd
                    aviso_estoque = ""
                    if nova_qtd < 5:
                        aviso_estoque = f"\n⚠️ Atenção: Estoque de {produto['nome']} está baixo ({nova_qtd} un). Considere repor."
                        
                    msg = ResponseGenerator.get("sale_success", qtd=qtd, nome=produto['nome'], total=valor_total)
                    return True, msg + aviso_nome + aviso_estoque
                    
                except ValueError as ve:
                    return False, ResponseGenerator.get("sale_error_stock", nome=produto['nome'], qtd=qtd, disponivel=produto['quantidade'])
                except Exception as e:
                    return False, f"❌ Erro ao vender: {str(e)}"
            else:
                return False, ResponseGenerator.get("not_found", termo=nome_busca)
                
        return False, "Use: 'vender [Qtd] unidades de [Nome do Produto]'"

    def _handle_deletion(self, command):
        # Remover Produto
        if "produto" in command:
            nome_match = re.search(r"produto\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s*$)", command)
            if nome_match:
                nome_busca = nome_match.group(1).strip()
                df = self.db.listar_produtos()
                for _, row in df.iterrows():
                    if nome_busca.lower() == row['nome'].lower():
                        self.db.remover_produto(row['id'])
                        return True, f"✅ Produto '{row['nome']}' removido."
                return False, f"Produto '{nome_busca}' não encontrado para remoção."
        
        # Remover Meta
        elif "meta" in command:
            titulo_match = re.search(r"meta\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s*$)", command)
            if titulo_match:
                titulo_busca = titulo_match.group(1).strip()
                df_metas = self.db.listar_metas()
                for _, row in df_metas.iterrows():
                    if titulo_busca.lower() in str(row['titulo']).lower():
                        self.db.remover_meta(row['id'])
                        return True, f"✅ Meta '{row['titulo']}' removida."
                return False, f"Meta '{titulo_busca}' não encontrada para remoção."
            return False, "Especifique o título da meta. Ex: 'remover meta Aumentar vendas'"
            
        return False, "Remoção via IA suportada apenas para produtos por enquanto (por segurança)."

    def _handle_update(self, command):
        # Editar Preço Produto
        if "preço" in command and "produto" in command:
            # Ex: "alterar preço do produto Tinta para 30"
            valor_match = re.search(r'para\s+(\d+(?:\.\d{2})?)', command)
            nome_match = re.search(r"produto\s+['\"]?([^'\"0-9]+?)['\"]?(?=\s+para)", command)
            
            if valor_match and nome_match:
                novo_preco = float(valor_match.group(1))
                nome_busca = nome_match.group(1).strip()
                
                df = self.db.listar_produtos()
                for _, row in df.iterrows():
                    if nome_busca.lower() in row['nome'].lower():
                        # Manter outros dados
                        self.db.atualizar_produto(row['id'], row['nome'], row['quantidade'], novo_preco, row['categoria'])
                        return True, f"✅ Preço de '{row['nome']}' atualizado para R$ {novo_preco:.2f}"
                return False, "Produto não encontrado."
                
        return False, "Edição via IA suportada apenas para preço de produtos. Use a interface para outras alterações."

    def _handle_query(self, command):
        # Filtros Temporais Inteligentes
        filtro_periodo = None
        hoje = datetime.date.today()
        
        if "hoje" in command:
            filtro_periodo = "hoje"
        elif "ontem" in command:
            filtro_periodo = "ontem"
        elif "semana" in command:
            filtro_periodo = "semana"
        elif "mês" in command or "mes" in command or "mensal" in command:
            filtro_periodo = "mes"
        elif "ano" in command or "anual" in command:
            filtro_periodo = "ano"
        elif "trimestre" in command:
            filtro_periodo = "trimestre"

        # === RESUMO DO DIA / MÊS ===
        if "resumo" in command:
            saldo = self.db.get_saldo_atual()
            df_vendas = self.db.listar_vendas()
            
            vendas_hoje = 0
            vendas_mes = 0
            num_vendas_hoje = 0
            num_vendas_mes = 0
            
            if not df_vendas.empty:
                try:
                    col_data = 'data_venda' if 'data_venda' in df_vendas.columns else 'data'
                    df_vendas['data_dt'] = pd.to_datetime(df_vendas[col_data], errors='coerce').dt.date
                    v_hoje = df_vendas[df_vendas['data_dt'] == hoje]
                    vendas_hoje = v_hoje['valor_total'].sum()
                    num_vendas_hoje = len(v_hoje)
                    inicio_mes = hoje.replace(day=1)
                    v_mes = df_vendas[df_vendas['data_dt'] >= inicio_mes]
                    vendas_mes = v_mes['valor_total'].sum()
                    num_vendas_mes = len(v_mes)
                except Exception:
                    pass
            
            res_lucro = self.db.lucro_por_periodo('mes')
            lucro_mes = res_lucro[0] if isinstance(res_lucro, tuple) else float(res_lucro or 0.0)
            total_fiado = self.db.total_fiado_pendente()
            total_contas = self.db.total_contas_pagar_pendente()
            produtos_baixo = self.db.produtos_abaixo_estoque_minimo()
            num_baixo = len(produtos_baixo) if not produtos_baixo.empty else 0
            
            if filtro_periodo == "hoje" or "dia" in command:
                return True, (
                    f"📊 **Resumo do Dia ({hoje.strftime('%d/%m/%Y')}):**\n"
                    f"  💰 Saldo em caixa: R$ {saldo:.2f}\n"
                    f"  🛒 Vendas hoje: {num_vendas_hoje} vendas → R$ {vendas_hoje:.2f}\n"
                    f"  📦 Produtos com estoque baixo: {num_baixo}\n"
                    f"  💳 Fiado pendente: R$ {total_fiado:.2f}\n"
                    f"  📄 Contas a pagar pendentes: R$ {total_contas:.2f}"
                )
            else:
                return True, (
                    f"📊 **Resumo do Mês ({hoje.strftime('%m/%Y')}):**\n"
                    f"  💰 Saldo em caixa: R$ {saldo:.2f}\n"
                    f"  🛒 Vendas no mês: {num_vendas_mes} vendas → R$ {vendas_mes:.2f}\n"
                    f"  📈 Lucro líquido do mês: R$ {lucro_mes:.2f}\n"
                    f"  💳 Fiado pendente: R$ {total_fiado:.2f}\n"
                    f"  📄 Contas a pagar: R$ {total_contas:.2f}\n"
                    f"  📦 Produtos com estoque baixo: {num_baixo}"
                )

        # === SALDO / CAIXA ===
        if "saldo" in command or ("caixa" in command and "abrir" not in command and "fechar" not in command):
            saldo = self.db.get_saldo_atual()
            return True, f"💰 Seu saldo atual é: R$ {saldo:.2f}"

        # === LUCRO ===
        if "lucro" in command or "lucrando" in command or "lucrei" in command:
            periodo = filtro_periodo or "mes"
            periodo_map = {"hoje": "hoje", "ontem": "hoje", "semana": "semana", "mes": "mes", "ano": "ano", "trimestre": "mes"}
            p = periodo_map.get(periodo, "mes")
            res_lucro = self.db.lucro_por_periodo(p)
            lucro = res_lucro[0] if isinstance(res_lucro, tuple) else float(res_lucro or 0.0)
            periodo_txt = {"hoje": "hoje", "ontem": "ontem", "semana": "esta semana",
                          "mes": "este mês", "ano": "este ano", "trimestre": "este trimestre"}.get(periodo, "este mês")
            return True, f"📈 Lucro líquido {periodo_txt}: R$ {lucro:.2f}"

        # === FATURAMENTO / VENDAS TOTAIS ===
        if ("faturamento" in command or "receita" in command or
            ("vend" in command and ("total" in command or "quanto" in command or "quantas" in command))):
            df_vendas = self.db.listar_vendas()
            if df_vendas.empty:
                return True, "📊 Nenhuma venda registrada ainda."
            
            try:
                col_data = 'data_venda' if 'data_venda' in df_vendas.columns else 'data'
                df_vendas['data_dt'] = pd.to_datetime(df_vendas[col_data], errors='coerce').dt.date
            except Exception:
                pass

            if filtro_periodo == "hoje":
                mask = df_vendas['data_dt'] == hoje
                txt_p = "hoje"
            elif filtro_periodo == "semana":
                mask = df_vendas['data_dt'] >= (hoje - datetime.timedelta(days=7))
                txt_p = "esta semana"
            elif filtro_periodo == "mes" or filtro_periodo is None:
                mask = df_vendas['data_dt'] >= hoje.replace(day=1)
                txt_p = "este mês"
            elif filtro_periodo == "ano":
                mask = df_vendas['data_dt'] >= hoje.replace(month=1, day=1)
                txt_p = "este ano"
            else:
                mask = pd.Series([True] * len(df_vendas))
                txt_p = "no total"

            filtrado = df_vendas[mask] if 'data_dt' in df_vendas.columns else df_vendas
            total = filtrado['valor_total'].sum() if not filtrado.empty else 0
            qtd = len(filtrado)
            return True, f"🛒 Vendas {txt_p}: {qtd} vendas totalizando R$ {total:.2f}"

        # === FIADO / CONTAS A RECEBER ===
        if "fiado" in command or "devendo" in command or "dívida" in command or "divida" in command or "a receber" in command:
            total = self.db.total_fiado_pendente()
            df_fiado = self.db.listar_fiados()
            pendentes = df_fiado[df_fiado['status'] == 'Pendente'] if not df_fiado.empty and 'status' in df_fiado.columns else pd.DataFrame()
            
            if pendentes.empty:
                return True, "✅ Nenhum fiado pendente! Tudo em dia."
            
            detalhes = []
            for _, f in pendentes.head(10).iterrows():
                nome_cli = f.get('cliente_nome') or f.get('cliente') or 'Desconhecido'
                valor = f.get('valor', 0)
                detalhes.append(f"  • {nome_cli}: R$ {valor:.2f}")
            
            txt = f"💳 **Fiado pendente total: R$ {total:.2f}** ({len(pendentes)} conta(s))\n"
            txt += "\n".join(detalhes[:10])
            if len(pendentes) > 10:
                txt += f"\n  ... e mais {len(pendentes) - 10} conta(s)."
            return True, txt

        # === CONTAS A PAGAR ===
        if "contas a pagar" in command or "conta a pagar" in command or ("contas" in command and ("pagar" in command or "vencendo" in command or "vencida" in command)):
            total = self.db.total_contas_pagar_pendente()
            vencidas = self.db.contas_vencidas()
            vencendo = self.db.contas_vencendo(dias=7)
            
            txt = f"📄 **Contas a pagar pendentes: R$ {total:.2f}**\n"
            
            if not vencidas.empty:
                txt += f"  🔴 {len(vencidas)} conta(s) VENCIDA(S)!\n"
                for _, c in vencidas.head(5).iterrows():
                    txt += f"    • {c.get('descricao', '?')}: R$ {c.get('valor', 0):.2f} (venceu {c.get('data_vencimento', '?')})\n"
            
            if not vencendo.empty:
                txt += f"  🟡 {len(vencendo)} conta(s) vencendo em 7 dias:\n"
                for _, c in vencendo.head(5).iterrows():
                    txt += f"    • {c.get('descricao', '?')}: R$ {c.get('valor', 0):.2f} (vence {c.get('data_vencimento', '?')})\n"
            
            if vencidas.empty and vencendo.empty:
                txt += "  ✅ Nenhuma conta vencida ou próxima do vencimento."
            
            return True, txt

        # === PRODUTO MAIS VENDIDO / RANKING ===
        if "mais vendido" in command or "ranking" in command or "top" in command or "campeão" in command or "campeao" in command:
            ranking = self.db.ranking_produtos_vendidos(10)
            if ranking.empty:
                return True, "📊 Sem dados de vendas ainda para gerar ranking."
            
            txt = "🏆 **Ranking de Produtos Mais Vendidos:**\n"
            for i, (_, r) in enumerate(ranking.head(10).iterrows(), 1):
                medalha = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                txt += f"  {medalha} {r['nome']} — {r['total_vendido']} un | R$ {r['faturamento']:.2f}\n"
            return True, txt

        # === FORMA DE PAGAMENTO ===
        if "forma de pagamento" in command or "pagamento mais" in command or "como pagam" in command or ("pix" in command and "quanto" in command):
            df_fp = self.db.vendas_por_forma_pagamento()
            if df_fp.empty:
                return True, "📊 Sem dados de vendas para análise de pagamento."
            
            txt = "💳 **Vendas por Forma de Pagamento:**\n"
            for _, r in df_fp.iterrows():
                forma_lbl = r.get('forma') or r.get('forma_pagamento') or '?'
                qtd_lbl = r.get('qtd') if r.get('qtd') is not None else r.get('quantidade', 0)
                txt += f"  • {forma_lbl}: R$ {r.get('total', 0):.2f} ({int(qtd_lbl)} vendas)\n"
            return True, txt

        # === VENDEDORES / QUEM MAIS VENDEU ===
        if "vendedor" in command or "funcionário" in command or "funcionario" in command or "quem mais vendeu" in command:
            df_vendedores = self.db.listar_vendedores()
            if df_vendedores.empty:
                return True, "👥 Nenhum vendedor cadastrado."
            
            if "mais vendeu" in command or "desempenho" in command or "melhor" in command:
                try:
                    vendas_v = self.db.vendas_por_vendedor(30)
                    if not vendas_v.empty:
                        txt = "🏆 **Desempenho dos Vendedores (últimos 30 dias):**\n"
                        for _, r in vendas_v.iterrows():
                            fat_v = r.get('total_vendido') if r.get('total_vendido') is not None else r.get('faturamento', 0)
                            txt += f"  • {r.get('vendedor', '?')}: {r.get('num_vendas', 0)} vendas | R$ {fat_v:.2f}\n"
                        return True, txt
                except Exception:
                    pass
            
            txt = "👥 **Vendedores cadastrados:**\n"
            for _, v in df_vendedores.iterrows():
                status = "🟢" if v.get('ativo', 1) else "🔴"
                txt += f"  {status} {v['nome']} (Comissão: {v.get('comissao', 0)}%)\n"
            return True, txt

        # === CLIENTES ===
        if "cliente" in command:
            df_cli = self.db.listar_clientes()
            if df_cli.empty:
                return True, "👥 Nenhum cliente cadastrado."
            
            if "listar" in command or "quais" in command or "todos" in command:
                txt = f"👥 **{len(df_cli)} clientes cadastrados:**\n"
                for _, c in df_cli.head(20).iterrows():
                    txt += f"  • {c['nome']}"
                    tel = c.get('contato') or c.get('telefone')
                    if tel:
                        txt += f" ({tel})"
                    txt += "\n"
                if len(df_cli) > 20:
                    txt += f"  ... e mais {len(df_cli) - 20} clientes."
                return True, txt
            
            if "quantos" in command or "total" in command:
                return True, f"👥 Você tem {len(df_cli)} clientes cadastrados."

        # === PRODUTOS VENCENDO ===
        if "vencendo" in command or "validade" in command or "vencer" in command or "vencido" in command:
            vencendo = self.db.verificar_produtos_vencendo(dias=7)
            if vencendo.empty:
                return True, "✅ Nenhum produto vencendo nos próximos 7 dias!"
            
            txt = f"⏰ **{len(vencendo)} produto(s) vencendo em 7 dias:**\n"
            for _, p in vencendo.iterrows():
                txt += f"  • {p.get('nome', '?')} — Validade: {p.get('data_validade', '?')}\n"
            return True, txt

        # === PRODUTOS ESTOQUE BAIXO ===
        if "baixo" in command and "estoque" in command or "repor" in command or "reposição" in command or "reposicao" in command or "acabando" in command:
            baixo = self.db.produtos_abaixo_estoque_minimo()
            if baixo.empty:
                return True, "✅ Nenhum produto com estoque crítico no momento!"
            
            txt = f"⚠️ **{len(baixo)} produto(s) com estoque baixo:**\n"
            for _, p in baixo.iterrows():
                txt += f"  • {p.get('nome', '?')}: {p.get('quantidade', 0)} un (mínimo: {p.get('estoque_minimo', 5)})\n"
            return True, txt

        # === FORNECEDORES ===
        if "fornecedor" in command:
            df_forn = self.db.listar_fornecedores()
            if df_forn.empty:
                return True, "📦 Nenhum fornecedor cadastrado."
            
            txt = f"📦 **{len(df_forn)} fornecedores cadastrados:**\n"
            for _, f in df_forn.head(15).iterrows():
                txt += f"  • {f['nome']}"
                if f.get('contato'):
                    txt += f" — {f['contato']}"
                txt += "\n"
            return True, txt

        # === COMPARATIVO MENSAL ===
        if "comparativo" in command or "comparação" in command or "comparar" in command or "mês anterior" in command or "mes anterior" in command:
            comp = self.db.comparativo_mensal()
            if not comp:
                return True, "📊 Sem dados suficientes para comparativo mensal."
            
            fat_atual = comp.get('atual', {}).get('faturamento', comp.get('faturamento_atual', 0))
            fat_ant = comp.get('anterior', {}).get('faturamento', comp.get('faturamento_anterior', 0))
            var = ((fat_atual - fat_ant) / fat_ant * 100) if fat_ant > 0 else 0
            seta = "📈" if var >= 0 else "📉"
            
            return True, (
                f"📊 **Comparativo Mensal:**\n"
                f"  Mês atual: R$ {fat_atual:.2f}\n"
                f"  Mês anterior: R$ {fat_ant:.2f}\n"
                f"  {seta} Variação: {var:+.1f}%"
            )
            
        # === TOTAL GASTOS ===
        if "gasto" in command and ("total" in command or "quanto" in command):
            df = self.db.listar_gastos()
            
            if filtro_periodo:
                try:
                    df['data_dt'] = pd.to_datetime(df['data'], errors='coerce').dt.date
                    if filtro_periodo == "hoje":
                        df = df[df['data_dt'] == hoje]
                        periodo_str = "hoje"
                    elif filtro_periodo == "ontem":
                        df = df[df['data_dt'] == (hoje - datetime.timedelta(days=1))]
                        periodo_str = "ontem"
                    elif filtro_periodo == "semana":
                        df = df[df['data_dt'] >= (hoje - datetime.timedelta(days=7))]
                        periodo_str = "esta semana"
                    elif filtro_periodo == "mes":
                        df = df[df['data_dt'] >= hoje.replace(day=1)]
                        periodo_str = "este mês"
                    elif filtro_periodo == "ano":
                        df = df[df['data_dt'] >= hoje.replace(month=1, day=1)]
                        periodo_str = "este ano"
                    else:
                        periodo_str = "registrados"
                except Exception:
                    periodo_str = "registrados"
            else:
                periodo_str = "registrados"

            total = df['valor'].sum() if not df.empty else 0
            return True, f"💸 Total de gastos {periodo_str}: R$ {total:.2f}"
            
        # === ESTOQUE TOTAL ===
        if "estoque" in command and ("total" in command or "valor" in command or "quanto" in command):
            df = self.db.listar_produtos()
            total_itens = df['quantidade'].sum() if not df.empty else 0
            valor_estoque = (df['quantidade'] * df['preco']).sum() if not df.empty else 0
            return True, f"📦 Estoque Total: {total_itens} itens avaliados em R$ {valor_estoque:.2f}"
            
        # === LISTAR PRODUTOS ===
        if "listar produtos" in command or "quais produtos" in command or "meus produtos" in command:
            df = self.db.listar_produtos()
            if df.empty: return True, "Nenhum produto cadastrado."
            txt = f"📋 **{len(df)} produtos cadastrados:**\n"
            for _, row in df.head(20).iterrows():
                txt += f"  • {row['nome']} — {row['quantidade']} un | R$ {row['preco']:.2f}\n"
            if len(df) > 20:
                txt += f"  ... e mais {len(df) - 20} produtos."
            return True, txt

        # === METAS ===
        if "meta" in command:
            df_metas = self.db.listar_metas()
            if df_metas.empty:
                return True, "🎯 Nenhuma meta cadastrada. Crie uma: 'crie uma meta Crescer 20%'"
            txt = f"🎯 **{len(df_metas)} metas:**\n"
            for _, m in df_metas.iterrows():
                txt += f"  • {m.get('titulo', '?')} — {m.get('status', 'Pendente')}\n"
            return True, txt

        # === CONSULTA ESPECÍFICA DE PRODUTO ===
        if "estoque" in command or "tenho" in command or "tem" in command:
            df = self.db.listar_produtos()
            if not df.empty:
                for _, row in df.iterrows():
                    if row['nome'].lower() in command:
                        qtd = row['quantidade']
                        aviso = ""
                        if qtd < 5:
                            aviso = " ⚠️ Estoque baixo! Considere repor."
                        return True, f"📦 {row['nome']}: {qtd} unidades em estoque (R$ {row['preco']:.2f} cada).{aviso}"
                
                # Fuzzy match em nomes de produto
                for _, row in df.iterrows():
                    for word in command.split():
                        if len(word) > 3:
                            ratio = difflib.SequenceMatcher(None, word, row['nome'].lower()).ratio()
                            if ratio > 0.7:
                                qtd = row['quantidade']
                                return True, f"📦 {row['nome']}: {qtd} unidades em estoque (R$ {row['preco']:.2f} cada)."

        return False, None

    def _handle_analysis(self, command):
        hoje = datetime.date.today()

        # Previsão de gastos
        if "previsão" in command or "prever" in command or "previsao" in command:
            if not self.is_trained: self.train()
            pred = self.predict_next_month()
            return True, f"🔮 Previsão de gastos para o próximo mês: R$ {pred:.2f}"
        
        # DRE / Demonstrativo de Resultados
        if "dre" in command or "demonstrativo" in command or "resultado do exercício" in command:
            try:
                dre = self.db.gerar_dre("mes")
                if dre:
                    custos = dre.get('custo_total', dre.get('custos', 0))
                    despesas = dre.get('despesas_total', dre.get('despesas', 0))
                    return True, (
                        f"📊 **DRE — {hoje.strftime('%m/%Y')}:**\n"
                        f"  Receita Bruta: R$ {dre.get('receita_bruta', 0):.2f}\n"
                        f"  (-) Deduções: R$ {dre.get('deducoes', 0):.2f}\n"
                        f"  = Receita Líquida: R$ {dre.get('receita_liquida', 0):.2f}\n"
                        f"  (-) Custos (CMV): R$ {custos:.2f}\n"
                        f"  = Lucro Bruto: R$ {dre.get('lucro_bruto', 0):.2f}\n"
                        f"  (-) Despesas: R$ {despesas:.2f}\n"
                        f"  = Resultado Líquido: R$ {dre.get('resultado_liquido', 0):.2f}"
                    )
            except Exception:
                pass
            return True, "📊 Sem dados suficientes para gerar o DRE deste mês."

        # Fluxo de caixa
        if "fluxo de caixa" in command or "cash flow" in command:
            try:
                fluxo = self.db.projetar_fluxo_caixa(dias=30)
                if fluxo:
                    saldo_atual = fluxo.get('saldo_atual', 0)
                    media = fluxo.get('media_diaria_vendas', 0)
                    projecao = fluxo.get('projecao', [])
                    total_saidas = sum(abs(p['valor']) for p in projecao if p['valor'] < 0)
                    total_entradas = sum(p['valor'] for p in projecao if p['valor'] > 0)
                    return True, (
                        f"💹 **Fluxo de Caixa (projeção 30 dias):**\n"
                        f"  Saldo atual: R$ {saldo_atual:.2f}\n"
                        f"  Média diária de vendas: R$ {media:.2f}\n"
                        f"  Entradas projetadas: R$ {total_entradas:.2f}\n"
                        f"  Saídas previstas: R$ {total_saidas:.2f}\n"
                        f"  Movimentações agendadas: {len(projecao)}"
                    )
            except Exception:
                pass
            return True, "💹 Sem dados suficientes para gerar fluxo de caixa."

        # Sugestão inteligente baseada em dados reais
        if "sugestão" in command or "sugestao" in command or "otimizar" in command or "dica" in command or "conselho" in command:
            sugestoes = []
            
            # Analisar estoque baixo
            baixo = self.db.produtos_abaixo_estoque_minimo()
            if not baixo.empty:
                nomes = ", ".join(baixo['nome'].head(3).tolist())
                sugestoes.append(f"⚠️ {len(baixo)} produto(s) com estoque baixo ({nomes}). Faça reposição!")
            
            # Analisar contas vencidas
            try:
                vencidas = self.db.contas_vencidas()
                if not vencidas.empty:
                    total_v = vencidas['valor'].sum()
                    sugestoes.append(f"🔴 Você tem {len(vencidas)} conta(s) vencida(s) totalizando R$ {total_v:.2f}. Regularize!")
            except Exception:
                pass
            
            # Analisar lucro
            try:
                res_lucro = self.db.lucro_por_periodo('mes')
                lucro = res_lucro[0] if isinstance(res_lucro, tuple) else float(res_lucro or 0.0)
                if lucro < 0:
                    sugestoes.append(f"📉 Seu lucro do mês está NEGATIVO (R$ {lucro:.2f}). Revise seus custos e margem de preço.")
                elif lucro > 0:
                    sugestoes.append(f"📈 Lucro do mês: R$ {lucro:.2f}. Considere reinvestir parte em estoque ou marketing.")
            except Exception:
                pass

            # Analisar fiado
            try:
                fiado = self.db.total_fiado_pendente()
                if fiado > 500:
                    sugestoes.append(f"💳 Fiado pendente de R$ {fiado:.2f}. Considere cobrar clientes em atraso.")
            except Exception:
                pass

            # Produtos vencendo
            try:
                vencendo = self.db.verificar_produtos_vencendo(7)
                if not vencendo.empty:
                    sugestoes.append(f"⏰ {len(vencendo)} produto(s) vencendo em 7 dias. Faça promoção para evitar perdas!")
            except Exception:
                pass

            if not sugestoes:
                sugestoes.append("✅ Tudo está em ordem! Continue monitorando seu negócio diariamente.")
                sugestoes.append("💡 Dica: Mantenha estoque de alta rotação sempre acima do mínimo e revise gastos fixos mensalmente.")

            return True, "💡 **Sugestões Inteligentes:**\n" + "\n".join(sugestoes)

        # Comissões
        if "comissão" in command or "comissao" in command or "comissões" in command:
            try:
                resultado = self.db.calcular_comissoes(periodo='mes')
                df_vendas_com = resultado.get('vendas', pd.DataFrame())
                if not df_vendas_com.empty:
                    txt = "💰 **Comissões dos Vendedores (mês atual):**\n"
                    for _, r in df_vendas_com.iterrows():
                        txt += (f"  • {r.get('vendedor', '?')}: "
                               f"{r.get('num_vendas', 0)} vendas | "
                               f"Faturamento: R$ {r.get('faturamento', 0):.2f} | "
                               f"Comissão: R$ {r.get('comissao_vendas', 0):.2f} ({r.get('taxa_comissao', 0)}%)\n")
                    return True, txt
                else:
                    return True, "💰 Nenhuma venda com vendedor atribuído neste mês."
            except Exception:
                pass
            return True, "💰 Sem dados de comissões disponíveis."
            
        return False, None

    def process_query(self, question, imagem_path=None):
        """
        Processa a entrada do usuário.
        1. Verifica comando de configuração (Chave API).
        2. Tenta processar como comando local (Offline Mode).
        3. Se não for comando local, tenta usar o Gemini (se disponível).
        4. Se falhar, retorna ajuda.
        """
        original_question = question
        question = question.lower().strip()
        
        # 0. CONFIGURAÇÃO DE APY KEY
        if question.startswith("chave para ia:") or question.startswith("api key:"):
            try:
                parts = original_question.split(":", 1)
                if len(parts) > 1:
                    new_key = parts[1].strip()
                    # Salvar arquivo seguro
                    key_file = os.path.join(self.db.data_dir, "gemini_key.txt")
                    with open(key_file, "w") as f:
                        f.write(new_key)
                    
                    # Tentar reconfigurar
                    self.configure_ai()
                    
                    if self.gemini_available:
                        return "✅ Chave de API salva e IA ativada com sucesso! Em que posso ajudar com seu estoque?"
                    else:
                        return "⚠️ Chave salva, mas não consegui conectar. Verifique se a chave é válida e tente novamente."
            except Exception as e:
                return f"❌ Erro ao salvar chave: {e}"

        # 0.5. COMANDO DIRETO GEMINI (G:)
        # Se começar com 'g:' ou 'G:', usa diretamente a IA ignorando comandos locais
        if question.startswith("g:") or question.startswith("ia:"):
            if not self.gemini_available:
                return "⚠️ A IA não está configurada. Envie 'Chave para ia: SUA_CHAVE' para ativar."
            
            # Remover o prefixo 'g:' ou 'ia:'
            pergunta_limpa = original_question.split(":", 1)[1].strip()
            if not pergunta_limpa: return "O que você gostaria de perguntar à IA?"
            
            resposta = self.ask_gemini(pergunta_limpa)
            if resposta:
                return resposta
            else:
                return "❌ Erro ao conectar com o Gemini."

        # 1. TENTAR COMANDO LOCAL
        success, result = self.process_local_command(question, imagem_path=imagem_path)
        if success:
            return result
        elif result is not None:
            # Comando reconhecido mas com erro de sintaxe ou mensagem específica
            return result
        
        # 2. TENTAR GEMINI (Se não for comando local reconhecido)
        if self.gemini_available:
            resposta_gemini = self.ask_gemini(original_question)
            if resposta_gemini:
                return resposta_gemini
        
        # 3. FALLBACK FINAL
        msg_erro = "Desculpe, não entendi."
        if not self.gemini_available:
            msg_erro += " (A IA Gemini não está configurada. Envie 'Chave para ia: SUA_CHAVE' para ativar)."
            
        return f"{msg_erro}\n\nTente comandos como:\n'Registre um gasto de 50 em Material'\n'Adicione um produto Tinta com 10 unidades'\n'Qual é o meu saldo?'"

class PricingEngine:
    def __init__(self):
        pass

    def calcular_preco_etico(self, custo_material, horas_trabalho, valor_hora, margem_lucro, fator_risco, fator_raridade):
        """
        Calcula o Preço Justo (Ético) baseado na fórmula:
        Preço = (Custo + (Tempo * ValorHora)) / (1 - (Margem + Risco)) * Raridade
        
        Args:
            custo_material (float): Custo dos materiais
            horas_trabalho (float): Horas gastas
            valor_hora (float): Valor da hora de trabalho
            margem_lucro (float): Margem de lucro desejada (0.0 a 1.0)
            fator_risco (float): Fator de risco/imprevistos (0.0 a 1.0)
            fator_raridade (float): Multiplicador de raridade (>= 1.0)
            
        Returns:
            dict: Detalhamento do cálculo e preço final
        """
        # 1. Custo Base (Mão de obra + Material)
        custo_mao_obra = horas_trabalho * valor_hora
        custo_base = custo_material + custo_mao_obra
        
        # 2. Divisor de Margem (Proteção contra prejuízo)
        # Se margem + risco >= 1, o preço tenderia ao infinito. Limitamos a 0.9 para segurança.
        fator_divisor = 1 - (margem_lucro + fator_risco)
        if fator_divisor <= 0.1:
            fator_divisor = 0.1 # Evita divisão por zero ou números negativos
            
        preco_base_margem = custo_base / fator_divisor
        
        # 3. Aplicação da Raridade
        preco_final = preco_base_margem * fator_raridade
        
        return {
            'custo_material': custo_material,
            'custo_mao_obra': custo_mao_obra,
            'custo_base': custo_base,
            'preco_sugerido': round(preco_final, 2),
            'detalhes': f'Base: {custo_base:.2f} / (1 - {margem_lucro+fator_risco:.2f}) * {fator_raridade:.2f}'
        }

