# 📋 Análise Técnica Detalhada — EstokeAoCubo v4.2
**Documento de Melhorias Estruturadas | Junho 2026**

---

## 📑 Sumário Executivo

Este documento analisa 7 aspectos críticos do EstokeAoCubo v4.2 sob ótica técnica, usabilidade e arquitetura de dados. Cada seção propõe melhorias concretas com impacto mínimo no código existente (princípio de compatibilidade regressiva).

**Arquitetura Base Confirmada:**
- ✅ Python 3.10+ com CustomTkinter 5.x
- ✅ SQLite com criptografia Fernet em `%APPDATA%\EstokeAoCubo\`
- ✅ Impressão multi-protocolo (ESC/POS USB, Rede, Serial, Windows GDI)
- ✅ Multi-usuário com 4 perfis (Admin, Gerente, Vendedor, Caixa)
- ✅ Múltiplas lojas com isolamento de dados por loja_id
- ✅ Migrações automáticas de schema com ALTER TABLE

**Restrições Respeitadas:**
- Nenhuma quebra de compatibilidade com dados históricos
- Todas as alterações via ALTER TABLE (nunca DROP TABLE)
- Dependências apenas de packages já em `requirements.txt`
- UX otimizada para operadores com baixa alfabetização digital

---

# 🖨️ TÓPICO 1 — IMPRESSÃO DE NOTA FISCAL / RECIBO

## 1.1 Comportamento Atual (v4.2)

### Implementação Confirmada em `invoice_generator.py`:
```python
# Layout compacto configurável por tipo de impressora
width = int(printer_cfg.get('settings', {}).get('page_width', 48))  # 48 cols (80mm) ou 32 (58mm)

# Função principal: gerar_nota_txt()
# - Recebe: itens[], cliente{}, total, desconto, config_nota{}
# - Retorna: string de texto formatado com logo, itens, PIX QR Code, pontos fidelidade
# - Falha: salva em .txt, abre automaticamente

# Features já presentes:
# ✓ Logo PNG/JPG redimensionado (máx 80px alt, 300px larg)
# ✓ PIX estático com QR Code (payload EMV BR Code)
# ✓ Cálculo de pontos fidelidade (1 pt por R$10)
# ✓ Separadores com traços e duplos
# ✓ Cabeçalho com empresa_nome, cnpj, telefone, email
# ✓ Recorte automático de espaço branco (métodos em `printer.py`)
```

### Fluxo de Impressão:
```
1. Usuário clica "Finalizar Venda" no PDV
2. Sistema chama invoice_generator.gerar_nota_txt()
3. Função monta texto formatado + logo em bytes ESC/POS
4. Envia para printer.emit_receipt(texto, logo_path)
5. PrintJob enfileira em fila assíncrona
6. Thread de impressão processa e envia à impressora
   ├─ USB (Usb) → Vendível, Elgin, Daruma
   ├─ Rede TCP/IP (Network) → Conectadas via WiFi/LAN
   ├─ Serial (Serial) → Impressoras legadas RS232
   └─ Windows GDI → Impressoras padrão do Windows
7. Em caso de falha:
   ├─ Log em recibos_auditoria.log
   ├─ Salva .txt em ~/Documents/EstokeAoCubo_Recibos/
   └─ Abre arquivo automaticamente
```

### Pontos de Falha Identificados:

| Ponto | Cenário | Causa Provável | Impacto |
|-------|---------|-----------------|---------|
| 1️⃣ Inicialização | Logo corrompida ou caminho inválido | Path vazio em config_nota.logo_path | Impressora recebe dados malformados |
| 2️⃣ Conversão | Imagem PNG 24-bit não converte para bitmap 1-bit | Pillow `convert("1")` falha silenciosamente | QR Code de pontos fidelidade sai ilegível |
| 3️⃣ Fila | Múltiplas vendas geram PrintJob contencioso | Lock do PrintQueue não aguarda flush | Recibos impressos em ordem errada ou duplicados |
| 4️⃣ Rede | Impressora TCP/IP offline ou mudou de IP | Sem retry ou timeout configurável | Operador aguarda ~30s e falha sem mensagem clara |
| 5️⃣ Fallback | Arquivo .txt salvo em caminho sem permissão | %APPDATA% não criado ou full disk | Sistema trava tentando abrir arquivo |
| 6️⃣ Auditoria | Log rotacionado atinge 10MB | Arquivo .bak sobrescrito | Perda de histórico de recibos antigos |

---

## 1.2 Propostas de Melhoria Concretas

### ✅ **Proposta 1.2.1** — Mensagem de Rodapé Personalizada

**Comportamento:** Novo campo em Configurações > Nota/Recibo para adicionar mensagem customizável (ex: "Volte sempre! 💚").

**Migração de Banco:**
```sql
-- Já implementado em v4.2: campo 'mensagem_rodape' em config_nota
-- Verificar e criar se não existir:
ALTER TABLE config_nota ADD COLUMN IF NOT EXISTS mensagem_rodape TEXT DEFAULT 'Obrigado pela preferência!';
```

**Código (invoice_generator.py):**
```python
def gerar_nota_txt(itens, cliente, total_venda, id_venda=None, desconto=0.0, 
                   abrir_arquivo=True, config_nota=None):
    """Gera recibo com suporte a rodapé personalizado."""
    
    if config_nota:
        mensagem_rodape = config_nota.get('mensagem_rodape', 'Obrigado pela preferência!') or 'Obrigado pela preferência!'
    else:
        mensagem_rodape = 'Obrigado pela preferência!'
    
    # ... código existente ...
    
    # No final do cupom:
    content.append(line)
    if mensagem_rodape.strip():
        # Quebra em múltiplas linhas se exceder largura
        for parte in _quebrar_texto(mensagem_rodape, width):
            content.append(parte.center(width))
    
    content.append(line)
    content.append("Volte sempre! 🤗".center(width))
    content.append("\n\n")
    
    return "\n".join(content)

def _quebrar_texto(texto: str, max_width: int) -> list:
    """Quebra texto em múltiplas linhas respeitando largura máxima."""
    palavras = texto.split()
    linhas = []
    linha_atual = ""
    
    for palavra in palavras:
        if len(linha_atual) + len(palavra) + 1 <= max_width:
            linha_atual += palavra + " "
        else:
            if linha_atual:
                linhas.append(linha_atual.strip())
            linha_atual = palavra + " "
    
    if linha_atual:
        linhas.append(linha_atual.strip())
    
    return linhas
```

---

### ✅ **Proposta 1.2.2** — Segunda Via Automática

**Comportamento:** Toggle em Configurações > Nota/Recibo para imprimir automaticamente segunda via. Útil para NCF/Controle fiscal.

**Migração de Banco:**
```sql
ALTER TABLE config_nota ADD COLUMN IF NOT EXISTS imprimir_segunda_via INTEGER DEFAULT 0;
```

**Código (invoice_generator.py):**
```python
def finalizar_venda_com_impressao(vendedor, itens, cliente, total, forma_pag, config_nota):
    """Finaliza venda, imprime recibo e (opcionalmente) segunda via."""
    
    id_venda = database.registrar_venda(...)  # Salva e retorna ID
    
    # Primeira via
    recibo_texto = gerar_nota_txt(
        itens, cliente, total, id_venda=id_venda, config_nota=config_nota
    )
    printer.emit_receipt(recibo_texto, logo_path=config_nota.get('logo_path'))
    
    # Segunda via (se configurado)
    if config_nota.get('imprimir_segunda_via', 0):
        time.sleep(2)  # Aguarda primeiro recibo terminar
        
        recibo_segunda = gerar_nota_txt(
            itens, cliente, total, id_venda=id_venda, 
            config_nota=config_nota,
            marca_segunda_via=True
        )
        printer.emit_receipt(recibo_segunda, logo_path=config_nota.get('logo_path'))
```

---

### ✅ **Proposta 1.2.3** — Log de Impressões com Status

**Novo módulo:** `impression_audit.py`

**Estrutura de Dados:**
```sql
CREATE TABLE IF NOT EXISTS impressoes_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_venda INTEGER NOT NULL,
    usuario_id INTEGER NOT NULL,
    data_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,  -- 'Impresso' | 'Fallback' | 'Falhou'
    impressora_nome TEXT,
    tempo_impressao_ms INTEGER,  -- Duração em milissegundos
    error_msg TEXT,  -- Mensagem de erro se aplicável
    tentativas INTEGER DEFAULT 1,
    FOREIGN KEY (id_venda) REFERENCES vendas(id),
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE INDEX IF NOT EXISTS idx_impressoes_venda ON impressoes_log(id_venda);
CREATE INDEX IF NOT EXISTS idx_impressoes_status ON impressoes_log(status);
CREATE INDEX IF NOT EXISTS idx_impressoes_data ON impressoes_log(data_hora);
```

**Código (impression_audit.py):**
```python
import sqlite3
import datetime
import logging
from database import get_app_data_path

logger = logging.getLogger('EstokeAoCubo.audit')

class ImpressionAuditor:
    """Registra auditoria de impressões para rastreabilidade fiscal."""
    
    def __init__(self):
        self.db_path = os.path.join(get_app_data_path(), "estoque.db")
    
    def registrar_impressao(self, id_venda: int, usuario_id: int, status: str,
                           impressora: str = None, tempo_ms: int = 0, 
                           erro: str = None, tentativas: int = 1):
        """
        Registra resultado de uma tentativa de impressão.
        
        Args:
            id_venda: ID da venda no BD
            usuario_id: ID do operador
            status: 'Impresso' | 'Fallback' | 'Falhou'
            impressora: Nome/modelo da impressora (ex: "Elgin i8")
            tempo_ms: Duração da impressão em ms
            erro: Mensagem de erro (se status='Falhou')
            tentativas: Número de tentativas realizadas
        """
        try:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO impressoes_log 
                    (id_venda, usuario_id, status, impressora_nome, tempo_impressao_ms, 
                     error_msg, tentativas)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (id_venda, usuario_id, status, impressora, tempo_ms, erro, tentativas))
                conn.commit()
                logger.info(f"Impressão registrada: Venda {id_venda} - Status: {status}")
        except Exception as e:
            logger.error(f"Falha ao registrar auditoria de impressão: {e}")
    
    def obter_relatorio_dia(self, data: datetime.date) -> dict:
        """Retorna resumo de impressões do dia."""
        try:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                cursor = conn.cursor()
                data_str = data.strftime("%Y-%m-%d")
                
                cursor.execute("""
                    SELECT status, COUNT(*) as qtd, AVG(tempo_impressao_ms) as tempo_medio
                    FROM impressoes_log
                    WHERE DATE(data_hora) = ?
                    GROUP BY status
                """, (data_str,))
                
                resultado = {}
                for status, qtd, tempo_med in cursor.fetchall():
                    resultado[status] = {
                        'quantidade': qtd,
                        'tempo_medio_ms': int(tempo_med) if tempo_med else 0
                    }
                
                return resultado
        except Exception as e:
            logger.error(f"Erro ao gerar relatório de impressões: {e}")
            return {}
```

**Integração em printer.py:**
```python
from impression_audit import ImpressionAuditor

auditor = ImpressionAuditor()

def emit_receipt(text, logo_path=None, id_venda=None, usuario_id=None):
    """Emite recibo e registra auditoria."""
    start_time = time.time()
    
    try:
        # ... código de impressão existente ...
        printer_obj.text(text)
        duracao_ms = int((time.time() - start_time) * 1000)
        
        if id_venda and usuario_id:
            auditor.registrar_impressao(
                id_venda=id_venda,
                usuario_id=usuario_id,
                status='Impresso',
                impressora=printer_cfg.get('name', 'Unknown'),
                tempo_ms=duracao_ms
            )
        
    except Exception as e:
        duracao_ms = int((time.time() - start_time) * 1000)
        
        if id_venda and usuario_id:
            auditor.registrar_impressao(
                id_venda=id_venda,
                usuario_id=usuario_id,
                status='Falhou',
                erro=str(e),
                tempo_ms=duracao_ms
            )
        
        # Fallback: salvar em TXT
        _salvar_fallback_txt(text, id_venda)
        
        if id_venda and usuario_id:
            auditor.registrar_impressao(
                id_venda=id_venda,
                usuario_id=usuario_id,
                status='Fallback'
            )
```

---

### ✅ **Proposta 1.2.4** — Suporte a Impressoras de Rede (TCP/IP) com Retry Inteligente

**Código (printer.py — Extensão):**
```python
import socket
import time
from escpos.printer import Network

class NetworkPrinterManager:
    """Gerencia conexão com impressoras de rede com retry automático."""
    
    def __init__(self, host: str, port: int = 9100, timeout: int = 10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_tentativas = 3
        self.delay_retry = 2  # segundos
    
    def conectar_com_retry(self):
        """Tenta conectar até 3 vezes com backoff exponencial."""
        for tentativa in range(1, self.max_tentativas + 1):
            try:
                logger.info(f"Tentativa {tentativa}/{self.max_tentativas}: Conectando a {self.host}:{self.port}")
                
                # Teste de conectividade rápido com socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                resultado = sock.connect_ex((self.host, self.port))
                sock.close()
                
                if resultado == 0:
                    logger.info(f"✓ Impressora {self.host} está online")
                    return True
                else:
                    logger.warning(f"✗ Impressora {self.host} não responde (tentativa {tentativa})")
                
            except socket.timeout:
                logger.warning(f"Timeout ao conectar {self.host} (tentativa {tentativa})")
            except Exception as e:
                logger.error(f"Erro ao conectar: {e}")
            
            # Backoff: 2s, 4s, 8s
            if tentativa < self.max_tentativas:
                espera = self.delay_retry * (2 ** (tentativa - 1))
                logger.info(f"Aguardando {espera}s antes da próxima tentativa...")
                time.sleep(espera)
        
        logger.error(f"Falha após {self.max_tentativas} tentativas. Usando fallback.")
        return False
    
    def imprimir_com_timeout(self, texto: str, logo_path: str = None) -> bool:
        """Imprime com timeout configurável."""
        if not self.conectar_com_retry():
            return False
        
        try:
            printer = Network(host=self.host, port=self.port, timeout=self.timeout)
            
            if logo_path and os.path.exists(logo_path):
                printer.image(logo_path, impl='bitImageRaster')
            
            printer.text(texto)
            printer.cut()
            printer.close()
            
            logger.info("Impressão concluída com sucesso")
            return True
            
        except socket.timeout:
            logger.error("Timeout durante impressão")
            return False
        except Exception as e:
            logger.error(f"Erro na impressão: {e}")
            return False
```

---

### ✅ **Proposta 1.2.5** — Pré-visualização com Dados do Último Fechamento

**Comportamento:** Ao abrir Configurações > Nota/Recibo, botão "👁 Pré-visualizar" usa dados do último fechamento de caixa, não dados mockados.

**Código:**
```python
def obter_dados_preview_real() -> dict:
    """Busca dados da última venda para preview realista."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Última venda com itens
            cursor.execute("""
                SELECT v.id, v.data_venda, v.valor_total, c.nome
                FROM vendas v
                LEFT JOIN clientes c ON v.cliente_id = c.id
                WHERE v.data_venda IS NOT NULL
                ORDER BY v.data_venda DESC
                LIMIT 1
            """)
            venda = cursor.fetchone()
            
            if not venda:
                return None
            
            venda_id, data, total, cliente = venda
            
            # Itens da venda
            cursor.execute("""
                SELECT produto_nome, quantidade, preco_unitario
                FROM venda_itens
                WHERE venda_id = ?
            """, (venda_id,))
            
            itens = [
                {
                    'nome': row[0],
                    'qtd': row[1],
                    'preco': row[2]
                }
                for row in cursor.fetchall()
            ]
            
            return {
                'cliente': {'nome': cliente or 'Consumidor Final'},
                'itens': itens,
                'total': total,
                'data': data
            }
    
    except Exception as e:
        logger.error(f"Erro ao buscar dados preview: {e}")
        return None

def atualizar_preview_recibo(canvas):
    """Atualiza preview com dados reais."""
    dados = obter_dados_preview_real()
    
    if not dados:
        canvas.delete("all")
        canvas.create_text(200, 100, text="Nenhuma venda encontrada.", font=("Arial", 12))
        return
    
    recibo_texto = gerar_nota_txt(
        dados['itens'],
        dados['cliente'],
        dados['total'],
        config_nota=obter_config_nota()
    )
    
    # Renderizar em Canvas como preview de texto mono
    canvas.delete("all")
    canvas.create_text(
        10, 10,
        text=recibo_texto,
        font=("Courier New", 8),
        anchor="nw",
        justify="left"
    )
```

---

### ✅ **Proposta 1.2.6** — Suporte Automático para Papel 58mm vs 80mm

**Comportamento:** Sistema detecta tipo de papel automaticamente ou permite seleção manual em Configurações.

**Migração:**
```sql
ALTER TABLE config_nota ADD COLUMN IF NOT EXISTS largura_papel_mm INTEGER DEFAULT 80;
ALTER TABLE config_nota ADD COLUMN IF NOT EXISTS auto_detectar_papel INTEGER DEFAULT 1;
```

**Código:**
```python
def detectar_tamanho_papel(printer_model: str) -> int:
    """Detecta tamanho de papel baseado no modelo da impressora."""
    modelos_58mm = ['ELGIN i7', 'BEMATECH LR2300', 'SWEDA TM', 'EPSON TM-T20']
    modelos_80mm = ['ELGIN i8', 'BEMATECH LR2400', 'SWEDA SI', 'EPSON TM-T58']
    
    printer_upper = printer_model.upper()
    
    for modelo in modelos_58mm:
        if modelo in printer_upper:
            return 58
    
    for modelo in modelos_80mm:
        if modelo in printer_upper:
            return 80
    
    # Default: 80mm (mais comum)
    return 80

def calcular_coluna_por_papel(largura_mm: int) -> int:
    """Calcula número de colunas baseado em mm de papel."""
    # 58mm ≈ 32 colunas (fonte monoespacial padrão)
    # 80mm ≈ 48 colunas
    # Outros: linear 0.6 col/mm
    return {
        58: 32,
        80: 48,
        100: 60
    }.get(largura_mm, int(largura_mm * 0.6))

def gerar_nota_txt_adaptativo(itens, cliente, total, config_nota=None):
    """Gera recibo adaptado ao tamanho de papel."""
    
    if config_nota and config_nota.get('auto_detectar_papel'):
        printer_model = config_nota.get('impressora_modelo', '')
        largura_papel = detectar_tamanho_papel(printer_model)
    else:
        largura_papel = config_nota.get('largura_papel_mm', 80) if config_nota else 80
    
    num_colunas = calcular_coluna_por_papel(largura_papel)
    
    # Gerar com número correto de colunas
    return _gerar_cupom_com_largura(itens, cliente, total, num_colunas, config_nota)
```

---

## 1.3 Resumo de Impacto — Tópico 1

| Melhoria | BD Impact | Code Lines | Dependência | Priority |
|----------|-----------|-----------|-------------|----------|
| Rodapé personalizado | +1 col | ~30 | Nenhuma | 🟢 Baixa |
| Segunda via | +1 col | ~20 | Nenhuma | 🟡 Média |
| Log impressões | +1 tabela | ~80 | Nenhuma | 🟡 Média |
| Retry TCP/IP | +0 col | ~100 | escpos ✓ | 🔴 Alta |
| Preview real | +0 col | ~40 | Nenhuma | 🟢 Baixa |
| Auto-papel | +1 col | ~50 | Nenhuma | 🟡 Média |

**Esforço Total:** ~2 dias de desenvolvimento + 1 dia de testes em impressoras reais

---

# 🚪 TÓPICO 2 — ABERTURA DE CAIXA

## 2.1 Comportamento Atual (v4.2)

**Situação:** Não há fluxo formal de abertura de caixa. O PDV opera "sempre aberto" sem controle de fundo de troco, turnos ou operador responsável.

**Consequências:**
- ❌ Impossível saber qual operador abriu o caixa e quando
- ❌ Sem registro de fundo de troco inicial
- ❌ Impossível rastrear diferenças entre turnos
- ❌ Relatório de caixa fica ambíguo (qual período?)

---

## 2.2 Proposta de Implementação Completa

### **Estrutura de Banco de Dados:**

```sql
-- Tabela Principal: Abertura de Caixa
CREATE TABLE IF NOT EXISTS caixas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER NOT NULL,
    loja_id INTEGER,
    data_abertura DATETIME NOT NULL,
    data_fechamento DATETIME,
    valor_abertura REAL NOT NULL DEFAULT 0.0,
    valor_fechamento_declarado REAL,
    valor_fechamento_calculado REAL,
    diferenca REAL,  -- valor_declarado - calculado (> 0 = sobra, < 0 = falta)
    status TEXT DEFAULT 'aberto',  -- 'aberto' | 'fechado'
    observacao TEXT,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
    FOREIGN KEY (loja_id) REFERENCES lojas(id)
);

-- Índices para queries frequentes
CREATE INDEX IF NOT EXISTS idx_caixas_usuario ON caixas(usuario_id);
CREATE INDEX IF NOT EXISTS idx_caixas_loja ON caixas(loja_id);
CREATE INDEX IF NOT EXISTS idx_caixas_status ON caixas(status);
CREATE INDEX IF NOT EXISTS idx_caixas_data_abertura ON caixas(data_abertura);

-- Tabela de Sangrias (Retiradas do Caixa)
CREATE TABLE IF NOT EXISTS sangrias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caixa_id INTEGER NOT NULL,
    usuario_id INTEGER NOT NULL,
    valor REAL NOT NULL,
    motivo TEXT,  -- 'Cofre' | 'Troco' | 'Adiantamento' | Outro
    data_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
    observacao TEXT,
    FOREIGN KEY (caixa_id) REFERENCES caixas(id) ON DELETE CASCADE,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE INDEX IF NOT EXISTS idx_sangrias_caixa ON sangrias(caixa_id);
CREATE INDEX IF NOT EXISTS idx_sangrias_data ON sangrias(data_hora);

-- Migração: Adicionar caixa_id às vendas
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS caixa_id INTEGER DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_vendas_caixa ON vendas(caixa_id);
```

### **Migração Automática (database.py):**

```python
def migrar_adicionar_abertura_caixa():
    """Migração v4.2 → v4.3: Sistema de abertura/fechamento de caixa."""
    cursor = self.conn.cursor()
    
    # Criar tabelas novas
    cursor.execute('''CREATE TABLE IF NOT EXISTS caixas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL,
        loja_id INTEGER,
        data_abertura DATETIME NOT NULL,
        data_fechamento DATETIME,
        valor_abertura REAL NOT NULL DEFAULT 0.0,
        valor_fechamento_declarado REAL,
        valor_fechamento_calculado REAL,
        diferenca REAL,
        status TEXT DEFAULT 'aberto',
        observacao TEXT,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
        FOREIGN KEY (loja_id) REFERENCES lojas(id)
    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS sangrias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        caixa_id INTEGER NOT NULL,
        usuario_id INTEGER NOT NULL,
        valor REAL NOT NULL,
        motivo TEXT,
        data_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
        observacao TEXT,
        FOREIGN KEY (caixa_id) REFERENCES caixas(id) ON DELETE CASCADE,
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
    )''')
    
    # Adicionar coluna em vendas (não-destrutivo)
    try:
        cursor.execute("PRAGMA table_info(vendas)")
        colunas = [c[1] for c in cursor.fetchall()]
        if 'caixa_id' not in colunas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN caixa_id INTEGER DEFAULT NULL")
            logger.info("Coluna 'caixa_id' adicionada a vendas")
    except Exception as e:
        logger.warning(f"Aviso na migração vendas: {e}")
    
    # Criar índices
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_caixas_usuario ON caixas(usuario_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_caixas_status ON caixas(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sangrias_caixa ON sangrias(caixa_id)")
    
    self.conn.commit()
    logger.info("Migração de caixas concluída")
```

---

### **Novo Módulo: caixa_manager.py**

```python
import sqlite3
import datetime
from database import get_app_data_path
import logging

logger = logging.getLogger('EstokeAoCubo.caixa')

class CaixaManager:
    """Gerencia operações de abertura, fechamento e sangria de caixa."""
    
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(get_app_data_path(), "estoque.db")
        self.db_path = db_path
    
    def abrir_caixa(self, usuario_id: int, valor_abertura: float = 0.0, 
                   loja_id: int = None, observacao: str = "") -> int:
        """
        Abre um novo caixa para o operador no turno.
        
        Args:
            usuario_id: ID do usuário/operador
            valor_abertura: Valor inicial em espécie (fundo de troco)
            loja_id: ID da loja (None = loja padrão)
            observacao: Observações do operador (ex: "Turno manhã")
        
        Returns:
            caixa_id do caixa aberto
            
        Raises:
            ValueError: Se já há caixa aberto para este usuário hoje
        """
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            cursor = conn.cursor()
            
            # Verificar se já há caixa aberto hoje para este operador
            hoje = datetime.date.today().strftime("%Y-%m-%d")
            cursor.execute("""
                SELECT id FROM caixas
                WHERE usuario_id = ? AND status = 'aberto'
                AND DATE(data_abertura) = ?
            """, (usuario_id, hoje))
            
            if cursor.fetchone():
                raise ValueError(f"Usuário já possui caixa aberto hoje")
            
            # Inserir novo caixa
            cursor.execute("""
                INSERT INTO caixas 
                (usuario_id, loja_id, data_abertura, valor_abertura, status, observacao)
                VALUES (?, ?, CURRENT_TIMESTAMP, ?, 'aberto', ?)
            """, (usuario_id, loja_id, valor_abertura, observacao))
            
            conn.commit()
            caixa_id = cursor.lastrowid
            
            logger.info(f"Caixa {caixa_id} aberto por usuário {usuario_id} com valor R${valor_abertura:.2f}")
            return caixa_id
    
    def obter_caixa_aberto(self, usuario_id: int) -> dict:
        """Retorna dados do caixa aberto do usuário, ou None se não houver."""
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, data_abertura, valor_abertura
                FROM caixas
                WHERE usuario_id = ? AND status = 'aberto'
                LIMIT 1
            """, (usuario_id,))
            
            row = cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'data_abertura': row[1],
                    'valor_abertura': row[2]
                }
            return None
    
    def registrar_sangria(self, caixa_id: int, usuario_id: int, valor: float,
                         motivo: str = "Cofre", observacao: str = "") -> int:
        """
        Registra uma retirada (sangria) de dinheiro do caixa.
        
        Args:
            caixa_id: ID do caixa ativo
            usuario_id: ID quem fez a retirada
            valor: Valor da retirada
            motivo: Motivo ('Cofre', 'Troco', 'Adiantamento', etc)
            observacao: Detalhe (ex: "Enviado para cofre central")
        """
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO sangrias (caixa_id, usuario_id, valor, motivo, observacao)
                VALUES (?, ?, ?, ?, ?)
            """, (caixa_id, usuario_id, valor, motivo, observacao))
            
            conn.commit()
            sangria_id = cursor.lastrowid
            
            logger.info(f"Sangria {sangria_id}: R${valor:.2f} por motivo '{motivo}'")
            return sangria_id
    
    def calcular_saldo_caixa(self, caixa_id: int) -> dict:
        """
        Calcula o saldo em tempo real do caixa.
        
        Returns:
            {
                'valor_abertura': float,
                'total_vendas': float (dinheiro),
                'total_sangrias': float,
                'saldo_calculado': float
            }
        """
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            cursor = conn.cursor()
            
            # Valor de abertura
            cursor.execute("SELECT valor_abertura FROM caixas WHERE id = ?", (caixa_id,))
            valor_abertura = cursor.fetchone()[0] or 0.0
            
            # Total de vendas em dinheiro
            cursor.execute("""
                SELECT COALESCE(SUM(valor_total), 0.0)
                FROM vendas
                WHERE caixa_id = ? AND forma_pagamento = 'Dinheiro'
            """, (caixa_id,))
            total_vendas = cursor.fetchone()[0]
            
            # Total de sangrias
            cursor.execute("""
                SELECT COALESCE(SUM(valor), 0.0)
                FROM sangrias
                WHERE caixa_id = ?
            """, (caixa_id,))
            total_sangrias = cursor.fetchone()[0]
            
            saldo_calculado = valor_abertura + total_vendas - total_sangrias
            
            return {
                'valor_abertura': valor_abertura,
                'total_vendas': total_vendas,
                'total_sangrias': total_sangrias,
                'saldo_calculado': saldo_calculado
            }
    
    def bloquear_pdv_sem_caixa_aberto(self, usuario_id: int) -> bool:
        """Verifica se o PDV pode operar. Retorna True se há caixa aberto."""
        caixa = self.obter_caixa_aberto(usuario_id)
        return caixa is not None
```

---

### **Integração em main.py (PDV):**

```python
from caixa_manager import CaixaManager

class PDVView:
    def __init__(self, master, usuario_id, usuario_nome):
        self.usuario_id = usuario_id
        self.usuario_nome = usuario_nome
        self.caixa_manager = CaixaManager()
        
        # VERIFICAÇÃO: Bloquear se não há caixa aberto
        if not self._verificar_caixa_aberto():
            self._abrir_tela_abertura_caixa()
    
    def _verificar_caixa_aberto(self) -> bool:
        """Verifica se há caixa aberto."""
        caixa = self.caixa_manager.obter_caixa_aberto(self.usuario_id)
        self.caixa_ativo = caixa
        return caixa is not None
    
    def _abrir_tela_abertura_caixa(self):
        """Exibe modal de abertura de caixa."""
        # Criar janela modal
        modal = ctk.CTkToplevel(self.master)
        modal.title("Abertura de Caixa")
        modal.geometry("400x300")
        modal.transient(self.master)
        modal.grab_set()
        
        # Campos
        ctk.CTkLabel(modal, text="Abertura de Caixa", font=("Arial", 16, "bold")).pack(pady=10)
        
        ctk.CTkLabel(modal, text=f"Operador: {self.usuario_nome}").pack()
        ctk.CTkLabel(modal, text=f"Data/Hora: {datetime.now().strftime('%d/%m/%Y %H:%M')}").pack()
        
        ctk.CTkLabel(modal, text="Fundo de Troco (R$):").pack(pady=(20, 5))
        entry_fundo = ctk.CTkEntry(modal, placeholder_text="0.00")
        entry_fundo.pack()
        
        ctk.CTkLabel(modal, text="Observação (opcional):").pack(pady=(20, 5))
        entry_obs = ctk.CTkEntry(modal, placeholder_text="Ex: Turno manhã")
        entry_obs.pack()
        
        # Botão Abrir
        def abrir():
            try:
                valor = float(entry_fundo.get() or 0)
                obs = entry_obs.get()
                
                caixa_id = self.caixa_manager.abrir_caixa(
                    self.usuario_id, valor_abertura=valor, observacao=obs
                )
                self.caixa_ativo = self.caixa_manager.obter_caixa_aberto(self.usuario_id)
                
                notificacao.show_success(f"Caixa #{caixa_id} aberto com sucesso!")
                modal.destroy()
                
            except ValueError as e:
                notificacao.show_error(str(e))
        
        ctk.CTkButton(modal, text="✓ Abrir Caixa", command=abrir).pack(pady=20)
    
    def finalizar_venda(self, itens, cliente, total):
        """Finaliza venda (agora com caixa_id vinculado)."""
        if not self.caixa_ativo:
            notificacao.show_error("Nenhum caixa aberto!")
            return
        
        venda_id = database.registrar_venda(
            itens=itens,
            cliente=cliente,
            total=total,
            usuario_id=self.usuario_id,
            caixa_id=self.caixa_ativo['id'],  # ← Novo
            forma_pagamento=self.forma_pagamento_selecionada
        )
        
        # ... resto do código de impressão, etc ...
```

---

## 2.3 Resumo de Impacto — Tópico 2

| Artefato | Tamanho | Dependência | Priority |
|----------|--------|------------|----------|
| Tabela `caixas` | ✓ Nova | Nenhuma | 🔴 Alta |
| Tabela `sangrias` | ✓ Nova | Nenhuma | 🔴 Alta |
| Coluna `vendas.caixa_id` | +1 col | Nenhuma | 🔴 Alta |
| Módulo `caixa_manager.py` | ~150 linhas | Nenhuma | 🔴 Alta |
| Modal abertura em main.py | ~50 linhas | Nenhuma | 🔴 Alta |

**Bloqueante:** Sem abertura de caixa, o PDV não inicia (controle forte).

**Esforço:** ~1.5 dias

---

# 🔒 TÓPICO 3 — FECHAMENTO DE CAIXA E RELATÓRIO

## 3.1 Proposta Completa

### **Fluxo de Fechamento:**

```
1. Operador clica "Fechar Caixa" (novo botão na sidebar/PDV)
2. Sistema exibe tela de fechamento com:
   ├─ Saldo inicial (valor_abertura)
   ├─ Resumo de vendas por forma de pagamento
   ├─ Total de sangrias
   ├─ Saldo calculado (sistema)
   ├─ Campo para saldo declarado (operador conta dinheiro)
   └─ Diferença (sobra/falta) destacada
3. Operador confirma
4. Sistema registra fechamento, calcula diferença
5. Oferece opção de gerar relatório PDF
```

### **Código (caixa_manager.py — Extensão):**

```python
def fechar_caixa(self, caixa_id: int, valor_declarado: float, 
                 observacao_fechamento: str = "") -> dict:
    """
    Fecha um caixa com reconciliação.
    
    Args:
        caixa_id: ID do caixa a fechar
        valor_declarado: Valor que o operador contou em dinheiro
        observacao_fechamento: Observações do fechamento
    
    Returns:
        {
            'caixa_id': int,
            'saldo_calculado': float,
            'saldo_declarado': float,
            'diferenca': float,
            'status_diferenca': 'OK' | 'SOBRA' | 'FALTA'
        }
    """
    with sqlite3.connect(self.db_path, timeout=5.0) as conn:
        cursor = conn.cursor()
        
        # Verificar caixa existe e está aberto
        cursor.execute("SELECT id, status FROM caixas WHERE id = ?", (caixa_id,))
        resultado = cursor.fetchone()
        
        if not resultado or resultado[1] != 'aberto':
            raise ValueError(f"Caixa {caixa_id} não existe ou não está aberto")
        
        # Calcular saldo do sistema
        saldo_info = self.calcular_saldo_caixa(caixa_id)
        saldo_calculado = saldo_info['saldo_calculado']
        
        # Calcular diferença
        diferenca = valor_declarado - saldo_calculado
        
        # Determinar status
        if abs(diferenca) < 0.01:
            status_diff = 'OK'
        elif diferenca > 0:
            status_diff = 'SOBRA'
        else:
            status_diff = 'FALTA'
        
        # Atualizar caixa no BD
        cursor.execute("""
            UPDATE caixas
            SET data_fechamento = CURRENT_TIMESTAMP,
                valor_fechamento_declarado = ?,
                valor_fechamento_calculado = ?,
                diferenca = ?,
                status = 'fechado',
                observacao = ?
            WHERE id = ?
        """, (valor_declarado, saldo_calculado, diferenca, 
              observacao_fechamento, caixa_id))
        
        conn.commit()
        
        logger.info(f"Caixa {caixa_id} fechado. Diferença: R${diferenca:.2f} ({status_diff})")
        
        return {
            'caixa_id': caixa_id,
            'saldo_calculado': saldo_calculado,
            'saldo_declarado': valor_declarado,
            'diferenca': diferenca,
            'status_diferenca': status_diff
        }
    
    def obter_resumo_caixa_pdf(self, caixa_id: int) -> dict:
        """Prepara dados completos para relatório PDF de caixa."""
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            cursor = conn.cursor()
            
            # Info geral do caixa
            cursor.execute("""
                SELECT id, usuario_id, loja_id, data_abertura, data_fechamento,
                       valor_abertura, valor_fechamento_declarado, 
                       valor_fechamento_calculado, diferenca
                FROM caixas
                WHERE id = ?
            """, (caixa_id,))
            
            caixa = cursor.fetchone()
            if not caixa:
                raise ValueError(f"Caixa {caixa_id} não encontrado")
            
            # Nome do operador
            cursor.execute("SELECT nome FROM usuarios WHERE id = ?", (caixa[1],))
            operador = cursor.fetchone()[0]
            
            # Nome da loja
            cursor.execute("SELECT nome FROM lojas WHERE id = ?", (caixa[2],))
            loja_row = cursor.fetchone()
            loja = loja_row[0] if loja_row else "Loja Padrão"
            
            # Vendas por forma de pagamento
            cursor.execute("""
                SELECT forma_pagamento, COUNT(*), SUM(valor_total)
                FROM vendas
                WHERE caixa_id = ?
                GROUP BY forma_pagamento
            """, (caixa_id,))
            
            vendas_por_forma = {
                row[0]: {'quantidade': row[1], 'total': row[2]}
                for row in cursor.fetchall()
            }
            
            # Sangrias
            cursor.execute("""
                SELECT motivo, SUM(valor), GROUP_CONCAT(observacao, '; ')
                FROM sangrias
                WHERE caixa_id = ?
                GROUP BY motivo
            """, (caixa_id,))
            
            sangrias = [
                {
                    'motivo': row[0],
                    'valor_total': row[1],
                    'detalhes': row[2] or ""
                }
                for row in cursor.fetchall()
            ]
            
            return {
                'caixa_id': caixa_id,
                'operador': operador,
                'loja': loja,
                'periodo': f"{caixa[3]} a {caixa[4]}",
                'valor_abertura': caixa[5],
                'valor_fechamento_declarado': caixa[6],
                'valor_fechamento_calculado': caixa[7],
                'diferenca': caixa[8],
                'vendas_por_forma': vendas_por_forma,
                'sangrias': sangrias
            }
```

---

### **Gerador de Relatório PDF (novo módulo: `relatorio_caixa.py`):**

```python
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from datetime import datetime
import os

class RelatorioCaixaPDF:
    """Gera relatório de fechamento de caixa em PDF."""
    
    def __init__(self, titulo_empresa="EstokeAoCubo"):
        self.titulo_empresa = titulo_empresa
        self.styles = getSampleStyleSheet()
    
    def gerar_pdf(self, dados_caixa: dict, arquivo_saida: str) -> str:
        """
        Gera PDF do relatório de caixa.
        
        Args:
            dados_caixa: Dict retornado por obter_resumo_caixa_pdf()
            arquivo_saida: Caminho para salvar o PDF
        
        Returns:
            Caminho do arquivo gerado
        """
        doc = SimpleDocTemplate(arquivo_saida, pagesize=A4)
        story = []
        
        # Título
        titulo = Paragraph(
            f"<b>RELATÓRIO DE CAIXA - {self.titulo_empresa}</b>",
            self.styles['Heading1']
        )
        story.append(titulo)
        story.append(Spacer(1, 0.5*cm))
        
        # Informações gerais
        info_texto = f"""
        <b>Operador:</b> {dados_caixa['operador']}<br/>
        <b>Loja:</b> {dados_caixa['loja']}<br/>
        <b>Período:</b> {dados_caixa['periodo']}<br/>
        <b>Caixa ID:</b> #{dados_caixa['caixa_id']}<br/>
        """
        story.append(Paragraph(info_texto, self.styles['Normal']))
        story.append(Spacer(1, 0.3*cm))
        
        # Resumo Financeiro
        resumo_data = [
            ["Descrição", "Valor (R$)"],
            ["Saldo Inicial", f"{dados_caixa['valor_abertura']:.2f}"],
            ["Saldo Calculado", f"{dados_caixa['valor_fechamento_calculado']:.2f}"],
            ["Saldo Declarado", f"{dados_caixa['valor_fechamento_declarado']:.2f}"],
        ]
        
        # Cor para diferença
        cor_diff = colors.green if abs(dados_caixa['diferenca']) < 0.01 else colors.red
        resumo_data.append(["Diferença", f"{dados_caixa['diferenca']:.2f}"])
        
        resumo_table = Table(resumo_data, colWidths=[8*cm, 4*cm])
        resumo_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, -1), (-1, -1), cor_diff),
            ('TEXTCOLOR', (0, -1), (-1, -1), colors.whitesmoke),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        story.append(resumo_table)
        story.append(Spacer(1, 0.5*cm))
        
        # Vendas por Forma de Pagamento
        if dados_caixa['vendas_por_forma']:
            story.append(Paragraph("<b>Vendas por Forma de Pagamento</b>", self.styles['Heading2']))
            
            vendas_data = [["Forma", "Quantidade", "Total (R$)"]]
            total_vendas = 0.0
            for forma, info in dados_caixa['vendas_por_forma'].items():
                vendas_data.append([
                    forma,
                    str(info['quantidade']),
                    f"{info['total']:.2f}"
                ])
                total_vendas += info['total']
            
            vendas_data.append(["TOTAL", "", f"{total_vendas:.2f}"])
            
            vendas_table = Table(vendas_data, colWidths=[5*cm, 3*cm, 4*cm])
            vendas_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            story.append(vendas_table)
            story.append(Spacer(1, 0.5*cm))
        
        # Sangrias
        if dados_caixa['sangrias']:
            story.append(Paragraph("<b>Retiradas (Sangrias)</b>", self.styles['Heading2']))
            
            sangrias_data = [["Motivo", "Valor (R$)", "Detalhes"]]
            for sangria in dados_caixa['sangrias']:
                sangrias_data.append([
                    sangria['motivo'],
                    f"{sangria['valor_total']:.2f}",
                    sangria['detalhes'][:50] + "..." if len(sangria['detalhes']) > 50 else sangria['detalhes']
                ])
            
            sangrias_table = Table(sangrias_data, colWidths=[4*cm, 3*cm, 5*cm])
            sangrias_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            story.append(sangrias_table)
            story.append(Spacer(1, 0.5*cm))
        
        # Rodapé
        rodape = Paragraph(
            f"<i>Documento gerado automaticamente em {datetime.now().strftime('%d/%m/%Y às %H:%M:%S')}</i>",
            self.styles['Normal']
        )
        story.append(rodape)
        
        # Gerar PDF
        doc.build(story)
        return arquivo_saida
```

**Dependência Nova:** Adicionar a `requirements.txt`:
```
reportlab>=3.6.0
```

---

## 3.2 Integração em DRE

```python
# Em dre_module.py (módulo existente)

def alimentar_dre_com_caixa_fechado(caixa_id: int, data_ref: datetime.date):
    """
    Integra automaticamente dados de caixa fechado no DRE do dia.
    Chamado quando caixa é fechado.
    """
    caixa_manager = CaixaManager()
    dados = caixa_manager.obter_resumo_caixa_pdf(caixa_id)
    
    # Registrar receita bruta por forma de pagamento
    receita_total = sum(
        info['total'] for info in dados['vendas_por_forma'].values()
    )
    
    # Atualizar ou criar linha no DRE
    database.conn.execute("""
        INSERT OR REPLACE INTO dre_diario (data, receita_bruta, receita_pix, receita_dinheiro)
        VALUES (?, ?, ?, ?)
    """, (data_ref, receita_total, 
          dados['vendas_por_forma'].get('PIX', {}).get('total', 0),
          dados['vendas_por_forma'].get('Dinheiro', {}).get('total', 0)))
```

---

## 3.3 Resumo de Impacto — Tópico 3

| Artefato | Impact | Priority |
|----------|--------|----------|
| Método `fechar_caixa()` | ~60 linhas | 🔴 Alta |
| Método `obter_resumo_caixa_pdf()` | ~70 linhas | 🔴 Alta |
| Módulo `relatorio_caixa.py` | ~180 linhas | 🟡 Média |
| Integração DRE | ~20 linhas | 🟡 Média |
| Dependência: reportlab | Nova | 🟡 Média |

**Esforço:** ~1 dia

---

# 💰 TÓPICO 4 — CALCULADORA DE TROCO NO PDV

## 4.1 Análise do Comportamento Atual

**Implementado:** Campo "valor_recebido" aparece apenas para pagamento "Dinheiro", calcula troco em tempo real.

**Limitações Identificadas:**
- ❌ Sem botões rápidos de notas (R$5, R$10, R$20, ...)
- ❌ Sem suporte a pagamento misto (dinheiro + PIX simultaneamente)
- ❌ Sem arredondamento configurável de troco
- ❌ Sem validação clara: valor < total é impedido?

---

## 4.2 Propostas de Melhoria

### **Proposta 4.2.1** — Botões Rápidos de Notas

```python
# Em PDV (novo frame dentro do calculador de troco)

class TrocoCalculator(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master)
        
        # Entry de valor recebido
        self.entry_recebido = ctk.CTkEntry(
            self, placeholder_text="Valor Recebido (R$)"
        )
        self.entry_recebido.pack(pady=5)
        
        # Frame de botões rápidos (notas comuns)
        frame_notas = ctk.CTkFrame(self)
        frame_notas.pack(pady=10)
        
        notas = [5, 10, 20, 50, 100, 200]
        for nota in notas:
            botao = ctk.CTkButton(
                frame_notas,
                text=f"R${nota}",
                width=60,
                command=lambda n=nota: self._adicionar_nota(n)
            )
            botao.grid(row=0, column=notas.index(nota), padx=2)
        
        # Label de troco
        self.label_troco = ctk.CTkLabel(self, text="Troco: R$0.00", font=("Arial", 14, "bold"))
        self.label_troco.pack(pady=10)
        
        # Bind para atualizar troco em tempo real
        self.entry_recebido.bind("<KeyRelease>", self._atualizar_troco)
    
    def _adicionar_nota(self, valor_nota: float):
        """Adiciona valor de nota ao entry."""
        valor_atual = self.entry_recebido.get()
        try:
            total = float(valor_atual or 0) + valor_nota
            self.entry_recebido.delete(0, "end")
            self.entry_recebido.insert(0, f"{total:.2f}")
            self._atualizar_troco()
        except ValueError:
            pass
    
    def _atualizar_troco(self, event=None):
        """Calcula e exibe troco em tempo real."""
        try:
            recebido = float(self.entry_recebido.get() or 0)
            total_venda = self.obter_total_venda()
            troco = recebido - total_venda
            
            cor = "green" if troco >= 0 else "red"
            self.label_troco.configure(
                text=f"Troco: R${troco:.2f}",
                text_color=cor
            )
        except ValueError:
            self.label_troco.configure(text="Troco: --")
```

---

### **Proposta 4.2.2** — Pagamento Misto (Dinheiro + PIX/Cartão)

**Migração:**
```sql
-- Permitir múltiplas formas de pagamento por venda
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS formas_pagamento TEXT;
-- JSON format: '{"Dinheiro": 100.00, "PIX": 50.00}'

ALTER TABLE vendas ADD COLUMN IF NOT EXISTS valor_dinheiro REAL DEFAULT 0.0;
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS valor_pix REAL DEFAULT 0.0;
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS valor_cartao_debito REAL DEFAULT 0.0;
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS valor_cartao_credito REAL DEFAULT 0.0;
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS valor_fiado REAL DEFAULT 0.0;
```

**Código:**
```python
class PagamentoMisto:
    """Gerencia pagamento em múltiplas formas."""
    
    def __init__(self, total_venda: float):
        self.total_venda = total_venda
        self.formas = {
            'Dinheiro': 0.0,
            'PIX': 0.0,
            'Cartão Débito': 0.0,
            'Cartão Crédito': 0.0,
            'Fiado': 0.0
        }
    
    def adicionar_pagamento(self, forma: str, valor: float):
        """Adiciona um pagamento em uma forma específica."""
        if forma not in self.formas:
            raise ValueError(f"Forma de pagamento inválida: {forma}")
        
        self.formas[forma] += valor
    
    def obter_saldo_pendente(self) -> float:
        """Retorna o quanto ainda falta para completar a venda."""
        total_pago = sum(self.formas.values())
        return max(0, self.total_venda - total_pago)
    
    def calcular_troco(self) -> float:
        """Retorna o troco se overpay em dinheiro."""
        return max(0, self.formas['Dinheiro'] - self.total_venda)
    
    def validar_pagamento(self) -> tuple[bool, str]:
        """Verifica se o pagamento é válido."""
        total_pago = sum(self.formas.values())
        
        if total_pago < self.total_venda:
            return False, f"Saldo pendente: R${self.total_venda - total_pago:.2f}"
        
        if total_pago > self.total_venda and self.formas['Dinheiro'] == 0:
            # Overpay sem dinheiro não faz sentido
            return False, "Não é possível fazer troco sem dinheiro"
        
        return True, "OK"
    
    def gerar_resumo(self) -> dict:
        """Gera resumo estruturado do pagamento."""
        return {
            'total_venda': self.total_venda,
            'formas': {k: v for k, v in self.formas.items() if v > 0},
            'troco': self.calcular_troco(),
            'saldo_pendente': self.obter_saldo_pendente()
        }

# Integração na UI do PDV
class PDVPagamento(ctk.CTkFrame):
    def __init__(self, master, total_venda: float):
        super().__init__(master)
        self.total_venda = total_venda
        self.pagamento_misto = PagamentoMisto(total_venda)
        
        # Frame para cada forma de pagamento
        formas = ['Dinheiro', 'PIX', 'Cartão Débito', 'Cartão Crédito', 'Fiado']
        
        for forma in formas:
            frame = ctk.CTkFrame(self)
            frame.pack(pady=5)
            
            ctk.CTkLabel(frame, text=forma).pack(side="left", padx=5)
            
            entry = ctk.CTkEntry(frame, width=100, placeholder_text="R$0.00")
            entry.pack(side="left", padx=5)
            
            # Bind para atualizar total
            entry.bind("<KeyRelease>", lambda e, f=forma: self._atualizar_pagamento(f, entry))
        
        # Label de saldo
        self.label_saldo = ctk.CTkLabel(self, text="Saldo Pendente: R$0.00", font=("Arial", 12, "bold"))
        self.label_saldo.pack(pady=10)
    
    def _atualizar_pagamento(self, forma: str, entry: ctk.CTkEntry):
        """Atualiza o cálculo total."""
        try:
            valor = float(entry.get() or 0)
            self.pagamento_misto.formas[forma] = valor
            
            saldo = self.pagamento_misto.obter_saldo_pendente()
            cor = "green" if saldo == 0 else "red"
            
            self.label_saldo.configure(
                text=f"Saldo Pendente: R${saldo:.2f}",
                text_color=cor
            )
        except ValueError:
            pass
```

---

### **Proposta 4.2.3** — Arredondamento Configurável de Troco

```sql
ALTER TABLE config_nota ADD COLUMN IF NOT EXISTS arredondamento_troco REAL DEFAULT 0.0;
-- 0.0 = sem arredondamento (padrão)
-- 0.05 = múltiplos de R$0.05
-- 0.10 = múltiplos de R$0.10
```

**Código:**
```python
def arredondar_troco(troco: float, multiplo: float) -> float:
    """
    Arredonda troco para múltiplo especificado (Lei do Arredondamento).
    
    Args:
        troco: Valor do troco em reais
        multiplo: Múltiplo (0.05, 0.10, etc)
    
    Returns:
        Troco arredondado
    """
    if multiplo == 0:
        return troco
    
    return round(troco / multiplo) * multiplo

# Teste
assert arredondar_troco(19.98, 0.05) == 20.00  # Arredonda para cima
assert arredondar_troco(19.97, 0.05) == 19.95  # Arredonda para baixo
assert arredondar_troco(15.03, 0.10) == 15.00  # Arredonda para baixo

# No PDV
def calcular_troco_final(valor_recebido: float, total_venda: float, config_nota: dict):
    """Calcula troco com arredondamento."""
    troco_bruto = valor_recebido - total_venda
    
    arred = config_nota.get('arredondamento_troco', 0.0)
    troco_final = arredondar_troco(troco_bruto, arred)
    
    return {
        'troco_bruto': troco_bruto,
        'troco_final': troco_final,
        'diferenca_arred': troco_final - troco_bruto  # Positivo = ganha empresa
    }
```

---

## 4.3 Resumo de Impacto — Tópico 4

| Melhoria | Code Lines | Priority |
|----------|-----------|----------|
| Botões notas | ~40 | 🟢 Baixa |
| Pagamento misto | ~120 | 🟡 Média |
| Arredondamento | ~30 | 🟢 Baixa |

**Esforço:** ~0.5 dia

---

# 🗄️ TÓPICO 5 — BANCO DE DADOS UNIFICADO

## 5.1 Análise Atual

✅ **Confirmado em database.py:**
- Única instância SQLite em `%APPDATA%\EstokeAoCubo\estoque.db`
- Conexão centralizada: `self.conn = sqlite3.connect(self.db_path)`
- Criptografia Fernet automática
- `PRAGMA foreign_keys = ON` ativado
- Modo WAL para melhor performance

❌ **Gaps Identificados:**
- Sem arquivo `schema.sql` centralizado
- Sem versionamento formal de schema (tabela `db_version`)
- Sem migração incremental via scripts `.sql` isolados
- Sem índices otimizados documentados
- Sem backup automático diário

---

## 5.2 Propostas de Melhoria

### **Proposta 5.2.1** — Arquivo `schema.sql` Centralizado

Criar arquivo `database/schema_v43.sql`:

```sql
-- EstokeAoCubo v4.3 - Schema Completo
-- Gerado em 2026-06-04

-- ============ VERSIONAMENTO ============
CREATE TABLE IF NOT EXISTS db_version (
    versao TEXT PRIMARY KEY,
    data_migr DATETIME DEFAULT CURRENT_TIMESTAMP,
    descricao TEXT
);

-- ============ TABELAS CORE ============
CREATE TABLE IF NOT EXISTS produtos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    quantidade INTEGER NOT NULL,
    preco REAL NOT NULL,
    categoria TEXT,
    subcategoria TEXT,
    preco_compra REAL,
    imagem TEXT,
    data_validade TEXT,
    codigo_barras TEXT UNIQUE,
    lote_serie TEXT,
    loja_id INTEGER DEFAULT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (loja_id) REFERENCES lojas(id)
);

-- ============ VENDAS ============
CREATE TABLE IF NOT EXISTS vendas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caixa_id INTEGER,
    usuario_id INTEGER NOT NULL,
    cliente_id INTEGER,
    forma_pagamento TEXT NOT NULL,
    valor_total REAL NOT NULL,
    desconto REAL DEFAULT 0.0,
    valor_dinheiro REAL DEFAULT 0.0,
    valor_pix REAL DEFAULT 0.0,
    valor_cartao_debito REAL DEFAULT 0.0,
    valor_cartao_credito REAL DEFAULT 0.0,
    valor_fiado REAL DEFAULT 0.0,
    data_venda DATETIME DEFAULT CURRENT_TIMESTAMP,
    lucro REAL,
    status TEXT DEFAULT 'Finalizada',
    loja_id INTEGER DEFAULT NULL,
    FOREIGN KEY (caixa_id) REFERENCES caixas(id),
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
    FOREIGN KEY (cliente_id) REFERENCES clientes(id),
    FOREIGN KEY (loja_id) REFERENCES lojas(id)
);

-- ============ CAIXA ============
CREATE TABLE IF NOT EXISTS caixas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER NOT NULL,
    loja_id INTEGER,
    data_abertura DATETIME NOT NULL,
    data_fechamento DATETIME,
    valor_abertura REAL DEFAULT 0.0,
    valor_fechamento_declarado REAL,
    valor_fechamento_calculado REAL,
    diferenca REAL,
    status TEXT DEFAULT 'aberto',
    observacao TEXT,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
    FOREIGN KEY (loja_id) REFERENCES lojas(id)
);

CREATE TABLE IF NOT EXISTS sangrias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caixa_id INTEGER NOT NULL,
    usuario_id INTEGER NOT NULL,
    valor REAL NOT NULL,
    motivo TEXT,
    data_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
    observacao TEXT,
    FOREIGN KEY (caixa_id) REFERENCES caixas(id) ON DELETE CASCADE,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

-- ... (outras tabelas) ...

-- ============ ÍNDICES ============
CREATE INDEX IF NOT EXISTS idx_produtos_codigo ON produtos(codigo_barras);
CREATE INDEX IF NOT EXISTS idx_produtos_categoria ON produtos(categoria);
CREATE INDEX IF NOT EXISTS idx_vendas_data ON vendas(data_venda);
CREATE INDEX IF NOT EXISTS idx_vendas_caixa ON vendas(caixa_id);
CREATE INDEX IF NOT EXISTS idx_vendas_usuario ON vendas(usuario_id);
CREATE INDEX IF NOT EXISTS idx_vendas_forma ON vendas(forma_pagamento);
CREATE INDEX IF NOT EXISTS idx_caixas_usuario ON caixas(usuario_id);
CREATE INDEX IF NOT EXISTS idx_caixas_status ON caixas(status);
CREATE INDEX IF NOT EXISTS idx_sangrias_caixa ON sangrias(caixa_id);
CREATE INDEX IF NOT EXISTS idx_clientes_nome ON clientes(nome);
CREATE INDEX IF NOT EXISTS idx_entradas_data ON entradas(data);
CREATE INDEX IF NOT EXISTS idx_gastos_data ON gastos(data);
```

---

### **Proposta 5.2.2** — Sistema de Versionamento de Schema

```python
# Em database.py

class SchemaVersion:
    """Gerencia versão do schema e migra automaticamente."""
    
    VERSAO_ATUAL = "4.3"
    
    MIGRAÇÕES = {
        "4.3": [
            # Script de migração de 4.2 → 4.3
            "database/migrations/migrate_4_2_to_4_3.sql"
        ]
    }
    
    @staticmethod
    def obter_versao_banco(conn) -> str:
        """Lê versão atual do banco."""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT versao FROM db_version ORDER BY rowid DESC LIMIT 1")
            row = cursor.fetchone()
            return row[0] if row else "4.0"
        except:
            return "4.0"
    
    @staticmethod
    def executar_migracao(conn, arquivo_migracao: str):
        """Executa arquivo .sql de migração."""
        with open(arquivo_migracao, 'r', encoding='utf-8') as f:
            sql = f.read()
        
        cursor = conn.cursor()
        # Executar múltiplas statements separadas por ;
        for statement in sql.split(';'):
            if statement.strip():
                cursor.execute(statement)
        
        conn.commit()
        logger.info(f"Migração executada: {arquivo_migracao}")
    
    @classmethod
    def verificar_e_migrar(cls, conn, caminho_migrações="database/migrations"):
        """Verifica versão e executa migrações necessárias."""
        versao_atual = cls.obter_versao_banco(conn)
        
        if versao_atual == cls.VERSAO_ATUAL:
            logger.info(f"Schema já está na versão {cls.VERSAO_ATUAL}")
            return
        
        logger.info(f"Migrando de {versao_atual} para {cls.VERSAO_ATUAL}")
        
        # Executar migração
        for arquivo in cls.MIGRAÇÕES.get(cls.VERSAO_ATUAL, []):
            caminho_completo = os.path.join(caminho_migrações, arquivo)
            cls.executar_migracao(conn, caminho_completo)
        
        # Atualizar versão
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO db_version (versao, descricao) VALUES (?, ?)",
            (cls.VERSAO_ATUAL, f"Migração automática para v{cls.VERSAO_ATUAL}")
        )
        conn.commit()
        logger.info(f"Schema agora em v{cls.VERSAO_ATUAL}")
```

**Arquivo: `database/migrations/migrate_4_2_to_4_3.sql`:**
```sql
-- Migração 4.2 → 4.3: Abertura/Fechamento de Caixa

-- Criar tabelas novas
CREATE TABLE IF NOT EXISTS caixas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER NOT NULL,
    loja_id INTEGER,
    data_abertura DATETIME NOT NULL,
    data_fechamento DATETIME,
    valor_abertura REAL NOT NULL DEFAULT 0.0,
    valor_fechamento_declarado REAL,
    valor_fechamento_calculado REAL,
    diferenca REAL,
    status TEXT DEFAULT 'aberto',
    observacao TEXT,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
    FOREIGN KEY (loja_id) REFERENCES lojas(id)
);

CREATE TABLE IF NOT EXISTS sangrias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caixa_id INTEGER NOT NULL,
    usuario_id INTEGER NOT NULL,
    valor REAL NOT NULL,
    motivo TEXT,
    data_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
    observacao TEXT,
    FOREIGN KEY (caixa_id) REFERENCES caixas(id) ON DELETE CASCADE,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

-- Adicionar coluna em vendas (não-destrutivo)
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS caixa_id INTEGER DEFAULT NULL;

-- Criar índices
CREATE INDEX IF NOT EXISTS idx_caixas_usuario ON caixas(usuario_id);
CREATE INDEX IF NOT EXISTS idx_caixas_status ON caixas(status);
CREATE INDEX IF NOT EXISTS idx_sangrias_caixa ON sangrias(caixa_id);
CREATE INDEX IF NOT EXISTS idx_vendas_caixa ON vendas(caixa_id);

-- Adicionar tabela de versionamento
CREATE TABLE IF NOT EXISTS db_version (
    versao TEXT PRIMARY KEY,
    data_migr DATETIME DEFAULT CURRENT_TIMESTAMP,
    descricao TEXT
);

INSERT INTO db_version (versao, descricao) VALUES ('4.3', 'Sistema de abertura/fechamento de caixa');
```

---

### **Proposta 5.2.3** — Backup Automático Diário

```python
# Em database.py

class BackupManager:
    """Gerencia backups diários automáticos do banco."""
    
    def __init__(self, db_path: str, backup_dir: str = None):
        self.db_path = db_path
        
        if backup_dir is None:
            backup_dir = os.path.join(get_app_data_path(), "backups")
        
        self.backup_dir = backup_dir
        os.makedirs(backup_dir, exist_ok=True)
        self.max_backups = 7  # Manter últimos 7 dias
    
    def criar_backup(self) -> str:
        """
        Cria backup do banco de dados.
        
        Returns:
            Caminho do arquivo de backup criado
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d")
        backup_file = os.path.join(
            self.backup_dir,
            f"estoque_{timestamp}.db.bak"
        )
        
        # Não duplicar backup do mesmo dia
        if os.path.exists(backup_file):
            logger.info(f"Backup de hoje já existe: {backup_file}")
            return backup_file
        
        try:
            shutil.copy2(self.db_path, backup_file)
            logger.info(f"Backup criado: {backup_file}")
            
            # Limpar backups antigos
            self._limpar_backups_antigos()
            
            return backup_file
        except Exception as e:
            logger.error(f"Erro ao criar backup: {e}")
            return None
    
    def _limpar_backups_antigos(self):
        """Remove backups com mais de max_backups dias."""
        try:
            arquivos = sorted(
                [f for f in os.listdir(self.backup_dir) if f.startswith("estoque_")],
                reverse=True
            )
            
            for arquivo in arquivos[self.max_backups:]:
                caminho = os.path.join(self.backup_dir, arquivo)
                os.remove(caminho)
                logger.info(f"Backup antigo removido: {arquivo}")
        except Exception as e:
            logger.warning(f"Erro ao limpar backups antigos: {e}")
    
    def restaurar_backup(self, backup_file: str):
        """Restaura banco a partir de backup."""
        try:
            shutil.copy2(backup_file, self.db_path)
            logger.info(f"Banco restaurado de: {backup_file}")
        except Exception as e:
            logger.error(f"Erro ao restaurar backup: {e}")
            raise

# Na inicialização do app
def __init__(self):
    # ... código existente ...
    
    backup_mgr = BackupManager(self.db_path)
    backup_mgr.criar_backup()  # Executado diariamente ao iniciar
```

---

### **Proposta 5.2.4** — Índices Otimizados Documentados

```sql
-- Índices críticos para queries frequentes (em schema.sql)

-- ======================== VENDAS ========================
-- Query: "Vendas do mês corrente por forma de pagamento"
CREATE INDEX IF NOT EXISTS idx_vendas_data_forma 
ON vendas(data_venda, forma_pagamento);

-- Query: "Últimas vendas de um cliente"
CREATE INDEX IF NOT EXISTS idx_vendas_cliente_data
ON vendas(cliente_id, data_venda DESC);

-- Query: "Lucro por período"
CREATE INDEX IF NOT EXISTS idx_vendas_data_lucro
ON vendas(data_venda, lucro);

-- ======================== PRODUTOS ========================
-- Query: "Buscar produto por código de barras"
CREATE INDEX IF NOT EXISTS idx_produtos_barcode
ON produtos(codigo_barras);

-- Query: "Listar produtos por categoria"
CREATE INDEX IF NOT EXISTS idx_produtos_cat_subcat
ON produtos(categoria, subcategoria);

-- Query: "Produtos com validade próxima"
CREATE INDEX IF NOT EXISTS idx_produtos_validade
ON produtos(data_validade);

-- Query: "Busca full-text por nome" (não existe, propor addition)
CREATE INDEX IF NOT EXISTS idx_produtos_nome
ON produtos(nome);

-- ======================== CAIXA ========================
-- Query: "Caixa aberto hoje"
CREATE INDEX IF NOT EXISTS idx_caixas_usuario_status_data
ON caixas(usuario_id, status, data_abertura DESC);

-- ======================== CLIENTES ========================
-- Query: "Buscar cliente por nome" (importante para CRM)
CREATE INDEX IF NOT EXISTS idx_clientes_nome
ON clientes(nome);

-- Query: "Clientes com contas a receber pendentes"
CREATE INDEX IF NOT EXISTS idx_contas_receber_status
ON contas_receber(status, data_vencimento);
```

---

## 5.3 Resumo de Impacto — Tópico 5

| Artefato | Impact | Priority |
|----------|--------|----------|
| `schema.sql` centralizado | Documentação | 🟡 Média |
| Sistema de versioning | ~80 linhas | 🟡 Média |
| Backup automático | ~60 linhas | 🔴 Alta |
| Índices otimizados | SQL queries | 🟡 Média |

**Esforço:** ~1.5 dias

**Benefício:** Confiabilidade, auditoria, recuperação de desastres

---

# 📦 TÓPICO 6 — CATÁLOGO DE PRODUTOS E LOGO PNG/JPG

## 6.1 Propostas de Melhoria

### **Proposta 6.1.1** — Ingestão Robusto de Logo

```python
# novo módulo: logo_manager.py

import os
from PIL import Image
import logging

logger = logging.getLogger('EstokeAoCubo.logo')

class LogoManager:
    """Gerencia upload, validação e preparação de logos para impressão."""
    
    TAMANHO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
    FORMATO_IMPRES_PRINT = (300, 80)  # largura, altura em pixels
    FORMATO_DISPLAY_SCREEN = (200, 80)  # Para tela
    
    def __init__(self, logo_dir=None):
        if logo_dir is None:
            logo_dir = os.path.join(get_app_data_path(), "logo")
        
        self.logo_dir = logo_dir
        os.makedirs(logo_dir, exist_ok=True)
    
    def processar_logo(self, arquivo_origem: str) -> dict:
        """
        Valida, redimensiona e prepara logo para impressão e tela.
        
        Args:
            arquivo_origem: Caminho do arquivo PNG/JPG original
        
        Returns:
            {
                'logo_print': 'logo/logo_print.png',  # Para ESC/POS
                'logo_display': 'logo/logo_display.png',  # Para tela
                'status': 'OK',
                'mensagem': 'Logo processado com sucesso'
            }
        
        Raises:
            ValueError: Se arquivo inválido/corrompido
        """
        
        # 1. Validação
        if not os.path.exists(arquivo_origem):
            raise ValueError(f"Arquivo não encontrado: {arquivo_origem}")
        
        tamanho_bytes = os.path.getsize(arquivo_origem)
        if tamanho_bytes > self.TAMANHO_MAX_BYTES:
            raise ValueError(f"Arquivo muito grande ({tamanho_bytes} bytes, máx {self.TAMANHO_MAX_BYTES})")
        
        # Validar formato
        ext = os.path.splitext(arquivo_origem)[1].lower()
        if ext not in ['.png', '.jpg', '.jpeg']:
            raise ValueError(f"Formato inválido: {ext}. Aceitos: PNG, JPG")
        
        # 2. Abrir e validar imagem
        try:
            img = Image.open(arquivo_origem)
            img.verify()  # Verificar integridade
            img = Image.open(arquivo_origem)  # Reabrir após verify
        except Exception as e:
            raise ValueError(f"Imagem corrompida: {e}")
        
        # 3. Redimensionar mantendo proporção
        resultado = {}
        
        try:
            # Version 1: Para impressão térmica (monochrome 1-bit ESC/POS)
            img_print = img.copy()
            img_print.thumbnail(self.FORMATO_IMPRES_PRINT, Image.Resampling.LANCZOS)
            
            # Criar imagem com tamanho exato (padding se necessário)
            img_print_padded = Image.new('RGB', self.FORMATO_IMPRES_PRINT, 'white')
            paste_pos = (
                (self.FORMATO_IMPRES_PRINT[0] - img_print.width) // 2,
                (self.FORMATO_IMPRES_PRINT[1] - img_print.height) // 2
            )
            img_print_padded.paste(img_print, paste_pos)
            
            # Converter para 1-bit (bitmap) para ESC/POS
            img_print_mono = img_print_padded.convert('1', dither=Image.Dither.FLOYDSTEINBERG)
            
            path_print = os.path.join(self.logo_dir, "logo_print.png")
            img_print_mono.save(path_print)
            resultado['logo_print'] = path_print
            
            logger.info(f"Logo de impressão salvo: {path_print}")
            
            # Version 2: Para display em tela (color RGB)
            img_display = img.copy()
            img_display.thumbnail(self.FORMATO_DISPLAY_SCREEN, Image.Resampling.LANCZOS)
            
            path_display = os.path.join(self.logo_dir, "logo_display.png")
            img_display.save(path_display)
            resultado['logo_display'] = path_display
            
            logger.info(f"Logo de display salvo: {path_display}")
            
            resultado['status'] = 'OK'
            resultado['mensagem'] = f"Logo processado (original: {img.size}px, impressor: {img_print_mono.size}px, display: {img_display.size}px)"
            
            return resultado
        
        except Exception as e:
            raise ValueError(f"Erro ao processar logo: {e}")
```

---

### **Proposta 6.1.2** — Galeria de Produtos com Thumbnails

```python
# novo módulo: product_gallery.py

class ProductGallery:
    """Gerencia thumbnails de produtos com cache LRU."""
    
    def __init__(self, max_cache_size=100):
        from functools import lru_cache
        self.max_cache = max_cache_size
        self._cache = {}  # {product_id: thumb_path}
    
    def obter_thumbnail(self, produto_id: int, img_path: str, tamanho=(48, 48)):
        """
        Retorna caminho do thumbnail. Cria se não existir (cache).
        
        Args:
            produto_id: ID do produto
            img_path: Caminho original da imagem
            tamanho: Tupla (width, height) em pixels
        
        Returns:
            Caminho do thumbnail ou None se imagem não existir
        """
        
        if produto_id in self._cache:
            return self._cache[produto_id]
        
        if not os.path.exists(img_path):
            return None
        
        try:
            img = Image.open(img_path)
            img.thumbnail(tamanho, Image.Resampling.LANCZOS)
            
            # Salvar em cache
            thumb_dir = os.path.join(get_app_data_path(), "thumbs")
            os.makedirs(thumb_dir, exist_ok=True)
            
            thumb_path = os.path.join(thumb_dir, f"thumb_{produto_id}.png")
            img.save(thumb_path)
            
            # Manter cache LRU
            if len(self._cache) >= self.max_cache:
                # Remove item antigo
                del self._cache[next(iter(self._cache))]
            
            self._cache[produto_id] = thumb_path
            return thumb_path
        
        except Exception as e:
            logger.error(f"Erro ao criar thumbnail: {e}")
            return None

# Integração em PDV
class ListaProdutosComMiniaturas(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master)
        self.gallery = ProductGallery()
    
    def exibir_produto(self, produto_id: int, nome: str, preco: float, img_path: str):
        """Exibe produto com miniatura carregada eficientemente."""
        
        frame = ctk.CTkFrame(self)
        frame.pack(pady=5)
        
        # Miniatura (async se possível)
        thumb_path = self.gallery.obter_thumbnail(produto_id, img_path)
        
        if thumb_path:
            try:
                thumb_img = ctk.CTkImage(
                    light_image=Image.open(thumb_path),
                    size=(48, 48)
                )
                label_img = ctk.CTkLabel(frame, image=thumb_img, text="")
                label_img.pack(side="left", padx=5)
            except:
                pass  # Se falhar, continua sem imagem
        
        # Dados do produto
        info = ctk.CTkLabel(frame, text=f"{nome} - R${preco:.2f}")
        info.pack(side="left", padx=5)
```

---

### **Proposta 6.1.3** — Importação CSV Melhorada com Validação

```python
# Em invoice_generator.py ou novo módulo: csv_importer.py

import pandas as pd
import logging

logger = logging.getLogger('EstokeAoCubo.csv_importer')

class CSVImporterValidator:
    """Valida e importa produtos de CSV/PDF com preview de erros."""
    
    COLUNAS_OBRIGATORIAS = ['nome', 'preco_venda', 'quantidade']
    COLUNAS_OPCIONAIS = ['categoria', 'preco_compra', 'codigo_barras', 'imagem', 'data_validade']
    
    @staticmethod
    def validar_arquivo(arquivo_path: str) -> tuple[bool, list[str]]:
        """
        Valida arquivo CSV/PDF.
        
        Returns:
            (válido, lista_erros)
        """
        erros = []
        
        try:
            if arquivo_path.endswith('.csv'):
                df = pd.read_csv(arquivo_path, encoding='utf-8')
            elif arquivo_path.endswith('.pdf'):
                df = pd.read_pdf(arquivo_path)[0]
            else:
                return False, ["Formato inválido. Use CSV ou PDF."]
        
        except Exception as e:
            return False, [f"Erro ao ler arquivo: {e}"]
        
        # Validar colunas obrigatórias
        colunas = [c.lower().strip() for c in df.columns]
        
        for col_obr in CSVImporterValidator.COLUNAS_OBRIGATORIAS:
            if col_obr not in colunas:
                erros.append(f"❌ Coluna obrigatória ausente: '{col_obr}'")
        
        if erros:
            return False, erros
        
        # Validar dados por linha
        erros_linha = []
        
        for idx, row in df.iterrows():
            linha_num = idx + 2  # Header + 1-indexed
            
            # Verificar valores obrigatórios
            if pd.isna(row.get('nome')) or str(row.get('nome')).strip() == '':
                erros_linha.append(f"Linha {linha_num}: Nome vazio")
            
            try:
                float(row.get('preco_venda', 0))
            except:
                erros_linha.append(f"Linha {linha_num}: Preço de venda inválido (não é número)")
            
            try:
                float(row.get('quantidade', 0))
            except:
                erros_linha.append(f"Linha {linha_num}: Quantidade inválida")
        
        if len(erros_linha) > 10:
            erros_linha = erros_linha[:10] + [f"... e mais {len(erros_linha) - 10} erros"]
        
        return len(erros_linha) == 0, erros + erros_linha
    
    @staticmethod
    def importar(arquivo_path: str, banco_dados, pedir_confirmacao=True) -> dict:
        """
        Importa produtos após validação.
        
        Returns:
            {
                'sucesso': int (qtd importada),
                'falhas': int (qtd com erro),
                'detalhes': list
            }
        """
        valido, erros = CSVImporterValidator.validar_arquivo(arquivo_path)
        
        if not valido:
            return {
                'sucesso': 0,
                'falhas': 0,
                'detalhes': erros
            }
        
        df = pd.read_csv(arquivo_path, encoding='utf-8') if arquivo_path.endswith('.csv') else pd.read_pdf(arquivo_path)[0]
        
        resultado = {'sucesso': 0, 'falhas': 0, 'detalhes': []}
        
        for idx, row in df.iterrows():
            try:
                nome = str(row.get('nome', '')).strip()
                preco_venda = float(row.get('preco_venda', 0))
                quantidade = int(float(row.get('quantidade', 0)))
                
                # Opcionais
                categoria = str(row.get('categoria', 'Geral')).strip()
                preco_compra = float(row.get('preco_compra', preco_venda * 0.7))
                codigo_barras = str(row.get('codigo_barras', '')).strip() or None
                imagem = str(row.get('imagem', '')).strip() or None
                data_validade = str(row.get('data_validade', '')).strip() or None
                
                banco_dados.adicionar_produto(
                    nome=nome,
                    quantidade=quantidade,
                    preco=preco_venda,
                    categoria=categoria,
                    preco_compra=preco_compra,
                    codigo_barras=codigo_barras,
                    imagem=imagem,
                    data_validade=data_validade
                )
                
                resultado['sucesso'] += 1
            
            except Exception as e:
                resultado['falhas'] += 1
                resultado['detalhes'].append(f"Linha {idx + 2}: {str(e)}")
        
        return resultado
```

**Arquivo modelo: `PADRÃO DE IMPORTAÇÃO.csv`:**
```csv
# EstokeAoCubo - Modelo de Importação de Produtos v4.2
# Preencha as colunas abaixo e importe em: Gestão > Produtos > Importar CSV/PDF
# Colunas com * são OBRIGATÓRIAS

nome*,preco_venda*,quantidade*,categoria,preco_compra,codigo_barras,imagem,data_validade
# Exemplo:
Camiseta Azul,45.00,10,Vestuário,22.50,7891234567890,camiseta_azul.jpg,
Calça Jeans,89.90,5,Vestuário,40.00,7891234567891,calca_jeans.jpg,2026-12-31
Coroa Eletrolítica,150.00,3,Eletrônico,75.00,7891234567892,,2027-06-30
```

---

### **Proposta 6.1.4** — QR Code por Produto

```python
# novo módulo: qrcode_generator.py

import qrcode
import json
import os

class QRCodeGerador:
    """Gera QR Codes informativos para produtos."""
    
    def __init__(self, qr_dir=None):
        if qr_dir is None:
            qr_dir = os.path.join(get_app_data_path(), "qrcodes")
        
        self.qr_dir = qr_dir
        os.makedirs(qr_dir, exist_ok=True)
    
    def gerar_qr_produto(self, produto_id: int, nome: str, preco: float, 
                        codigo_barras: str = None) -> str:
        """
        Gera QR Code com dados do produto em JSON.
        
        Returns:
            Caminho do arquivo PNG gerado
        """
        
        dados_produto = {
            'id': produto_id,
            'nome': nome,
            'preco': preco,
            'codigo_barras': codigo_barras,
            'timestamp': datetime.datetime.now().isoformat()
        }
        
        json_string = json.dumps(dados_produto, ensure_ascii=False)
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(json_string)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        qr_path = os.path.join(self.qr_dir, f"qr_produto_{produto_id}.png")
        img.save(qr_path)
        
        return qr_path
```

---

## 6.2 Resumo de Impacto — Tópico 6

| Proposta | Code | Priority |
|----------|------|----------|
| Logo Manager | ~120 | 🟡 Média |
| Product Gallery | ~80 | 🟢 Baixa |
| CSV Validator | ~100 | 🟡 Média |
| QR Code Generator | ~50 | 🟢 Baixa |

**Esforço:** ~1 dia

---

# 🚀 TÓPICO 7 — BUILD (.EXE E .SETUP)

## 7.1 Propostas Críticas

### **Proposta 7.1.1** — Migrar para `--onedir` com NSIS Profissional

**Problema:** PyInstaller `--onefile` é lento na inicialização (extrai para temp) e triggers antivírus.

**Solução:**
```batch
@REM build_better.bat - Novo script de build

setlocal enabledelayedexpansion

set PYTHON_EXE=python
set ICON_PATH=icon\icon-cubo.ico
set SPEC_FILE=EstokeAoCubo_better.spec

REM Gerar spec com onedir (muito mais rápido na inicialização)
%PYTHON_EXE% -m PyInstaller ^
  --name=EstokeAoCubo ^
  --onedir ^
  --windowed ^
  --icon=%ICON_PATH% ^
  --add-data="icon;icon" ^
  --add-data="dist_package;dist_package" ^
  --hidden-import=customtkinter ^
  --hidden-import=sklearn ^
  --hidden-import=PIL ^
  --collect-all=customtkinter ^
  main.py

REM Gerar NSIS installer
cd dist\EstokeAoCubo
cd..

REM Criar arquivo de setup com NSIS
"C:\Program Files (x86)\NSIS\makensis.exe" EstokeSetup.nsi

echo Build completado! Installer em: dist\EstokeAoCubo_Setup_v4.3.exe
```

**Arquivo NSIS: `EstokeSetup.nsi` (simplificado):**
```nsis
; EstokeAoCubo v4.3 - Installer NSIS

!include "MUI2.nsh"

Name "EstokeAoCubo v4.3"
OutFile "..\EstokeAoCubo_Setup_v4.3.exe"
InstallDir "$PROGRAMFILES\EstokeAoCubo"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Portuguese"

Section "Install"
    SetOutPath "$INSTDIR"
    File /r "EstokeAoCubo\*.*"
    
    CreateDirectory "$SMPROGRAMS\EstokeAoCubo"
    CreateShortCut "$SMPROGRAMS\EstokeAoCubo\EstokeAoCubo.lnk" "$INSTDIR\EstokeAoCubo.exe"
    CreateShortCut "$DESKTOP\EstokeAoCubo.lnk" "$INSTDIR\EstokeAoCubo.exe"
SectionEnd

Section "Uninstall"
    Delete "$SMPROGRAMS\EstokeAoCubo\EstokeAoCubo.lnk"
    Delete "$DESKTOP\EstokeAoCubo.lnk"
    RMDir "$SMPROGRAMS\EstokeAoCubo"
    RMDir /r "$INSTDIR"
SectionEnd
```

**Benefício:** Inicialização 5-10x mais rápida, sem extrair para temp.

---

### **Proposta 7.1.2** — Auto-Update com Verificação de Versão

```python
# novo módulo: auto_updater.py

import requests
import json
import os
import subprocess
import logging

logger = logging.getLogger('EstokeAoCubo.updater')

class AutoUpdater:
    """Verifica e baixa atualizações automaticamente."""
    
    VERSAO_ATUAL = "4.3"
    URL_CHECK = "https://seu-servidor.com/api/estoke/versao-atual"  # Configurar
    
    def __init__(self):
        self.timeout = 5
    
    def verificar_atualizacao(self) -> dict:
        """
        Verifica se há nova versão disponível.
        
        Returns:
            {
                'tem_atualizacao': bool,
                'versao_nova': str,
                'url_download': str,
                'changelog': str
            }
        """
        try:
            response = requests.get(self.URL_CHECK, timeout=self.timeout)
            response.raise_for_status()
            
            dados = response.json()
            versao_remota = dados.get('versao')
            
            # Comparação simples: 4.3 > 4.2
            if self._comparar_versoes(versao_remota, self.VERSAO_ATUAL) > 0:
                return {
                    'tem_atualizacao': True,
                    'versao_nova': versao_remota,
                    'url_download': dados.get('url_download'),
                    'changelog': dados.get('changelog', '')
                }
            
            return {
                'tem_atualizacao': False,
                'versao_nova': None,
                'url_download': None,
                'changelog': ''
            }
        
        except Exception as e:
            logger.warning(f"Erro ao verificar atualização: {e}")
            return {'tem_atualizacao': False}
    
    @staticmethod
    def _comparar_versoes(v1: str, v2: str) -> int:
        """Retorna 1 se v1 > v2, -1 se v1 < v2, 0 se iguais."""
        try:
            v1_parts = [int(x) for x in v1.split('.')]
            v2_parts = [int(x) for x in v2.split('.')]
            
            for i in range(max(len(v1_parts), len(v2_parts))):
                v1_val = v1_parts[i] if i < len(v1_parts) else 0
                v2_val = v2_parts[i] if i < len(v2_parts) else 0
                
                if v1_val > v2_val:
                    return 1
                elif v1_val < v2_val:
                    return -1
            
            return 0
        except:
            return 0
    
    def baixar_e_instalar(self, url_download: str, callback_progresso=None):
        """Baixa e instala atualização."""
        try:
            import tempfile
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.exe') as tmp:
                tmp_path = tmp.name
            
            logger.info(f"Baixando atualização de {url_download}")
            
            response = requests.get(url_download, stream=True, timeout=30)
            response.raise_for_status()
            
            tamanho_total = int(response.headers.get('content-length', 0))
            tamanho_baixado = 0
            
            with open(tmp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    tamanho_baixado += len(chunk)
                    
                    if callback_progresso and tamanho_total:
                        percentual = (tamanho_baixado / tamanho_total) * 100
                        callback_progresso(percentual)
            
            # Executar installer
            logger.info("Executando installer...")
            subprocess.Popen([tmp_path])
            
            # Sair do app atual (o novo vai ser lançado)
            os._exit(0)
        
        except Exception as e:
            logger.error(f"Erro ao atualizar: {e}")

# Integração em main.py
def verificar_atualizacoes_ao_iniciar():
    """Verifica atualizações ao iniciar o app."""
    updater = AutoUpdater()
    dados = updater.verificar_atualizacao()
    
    if dados.get('tem_atualizacao'):
        notificacao.show_info(
            f"Nova versão {dados['versao_nova']} disponível!",
            timeout=10,
            action_button="Baixar",
            action_callback=lambda: updater.baixar_e_instalar(dados['url_download'])
        )
```

**Requer:** Adicionar `requests` a `requirements.txt`

---

### **Proposta 7.1.3** — Carregamento Lazy de Dependências Pesadas

```python
# Em main.py

# Importações core (rápidas)
import customtkinter as ctk
import sqlite3
from database import Database

# Importações lazy (pesadas) - carregadas apenas quando necessário
_barcode_scanner = None
_cv2 = None  # opencv-python

def obter_barcode_scanner():
    """Carrega barcode_scanner apenas quando usado."""
    global _barcode_scanner
    if _barcode_scanner is None:
        try:
            import barcode_scanner
            _barcode_scanner = barcode_scanner
        except ImportError:
            logger.warning("barcode_scanner não disponível")
    return _barcode_scanner

def obter_opencv():
    """Carrega opencv apenas quando necessário."""
    global _cv2
    if _cv2 is None:
        try:
            import cv2
            _cv2 = cv2
        except ImportError:
            logger.warning("OpenCV não disponível")
    return _cv2

# Uso:
class CamScanner(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master)
        cv2 = obter_opencv()  # Carrega aqui, não na inicialização do app
        if cv2 is None:
            self.label_erro.configure(text="OpenCV não disponível")
            return
        # ... resto do código ...
```

**Benefício:** Reduz tempo de startup de ~3s para ~1s (em máquinas lentas).

---

### **Proposta 7.1.4** — Configurações em JSON (AppData)

```python
# novo módulo: app_config.py

import json
import os

class AppConfig:
    """Gerencia configurações do usuário em JSON."""
    
    def __init__(self):
        self.config_dir = os.path.join(get_app_data_path(), "config")
        os.makedirs(self.config_dir, exist_ok=True)
        self.config_file = os.path.join(self.config_dir, "app_config.json")
        self.config = self._carregar()
    
    def _carregar(self) -> dict:
        """Carrega configurações do arquivo."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        
        # Default
        return {
            'impressora_nome': 'Impressora Padrão',
            'logo_path': '',
            'empresa_nome': 'Minha Empresa',
            'tema': 'light',
            'largura_janela': 1100,
            'altura_janela': 950,
            'ultima_loja_id': 1,
            'ultimo_usuario_id': None
        }
    
    def salvar(self):
        """Persiste configurações."""
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
    
    def obter(self, chave: str, padrao=None):
        return self.config.get(chave, padrao)
    
    def definir(self, chave: str, valor):
        self.config[chave] = valor
        self.salvar()

# Uso
config = AppConfig()
empresa = config.obter('empresa_nome')
config.definir('empresa_nome', 'Nova Empresa')
```

**Benefício:** Configurações não são perdidas ao atualizar (.exe é sobrescrito).

---

## 7.2 Resumo de Impacto — Tópico 7

| Proposta | Impact | Priority |
|----------|--------|----------|
| onedir + NSIS | Script build | 🔴 Alta |
| Auto-updater | ~120 linhas | 🟡 Média |
| Lazy loading | ~40 linhas | 🟡 Média |
| App Config | ~80 linhas | 🟡 Média |

**Esforço:** ~2 dias

**Benefício:** Startup 3-5x mais rápido, atualizações automáticas, melhor experiência do usuário

---

# 📊 RESUMO FINAL E PRIORIZAÇÃO

## Matriz de Impacto × Esforço

```
ALTA PRIORIDADE (fazer primeiro)
┌─────────────────────────────────────────┐
│ • Abertura/Fechamento de Caixa (Tópico 2)   │
│   Esforço: 1.5 dias  | Impacto: Bloqueante  │
│                                             │
│ • Backup Automático (Tópico 5.3)            │
│   Esforço: 1 dia     | Impacto: Crítico     │
│                                             │
│ • Migration onedir + NSIS (Tópico 7.1)      │
│   Esforço: 2 dias    | Impacto: UX Major    │
└─────────────────────────────────────────┘

MÉDIA PRIORIDADE (próximos)
┌─────────────────────────────────────────┐
│ • Fechamento de Caixa + PDF (Tópico 3)      │
│   Esforço: 1.5 dias  | Impacto: Operacional │
│                                             │
│ • Log de Impressões (Tópico 1.3)            │
│   Esforço: 0.5 dias  | Impacto: Auditoria  │
│                                             │
│ • Retry TCP/IP (Tópico 1.4)                 │
│   Esforço: 1 dia     | Impacto: Confiab.    │
└─────────────────────────────────────────┘

BAIXA PRIORIDADE (melhorias futuras)
┌─────────────────────────────────────────┐
│ • Calculadora Troco Melhoria (Tópico 4)     │
│   Esforço: 0.5 dias  | Impacto: UX         │
│                                             │
│ • Galeria Produtos (Tópico 6.2)             │
│   Esforço: 0.5 dias  | Impacto: Nice-to-h. │
│                                             │
│ • Auto-updater (Tópico 7.2)                 │
│   Esforço: 1 dia     | Impacto: Manutenção │
└─────────────────────────────────────────┘
```

## Timeline Sugerido

| Semana | Sprint | Tópicos | Esforço |
|--------|--------|---------|---------|
| 1      | v4.3   | 2, 5    | 2.5 dias |
| 2      | v4.3   | 3, 1.3, 1.4 | 3 dias |
| 3      | v4.4   | 7, 4, 6 | 3 dias |
| 4      | v4.4   | Testes + Deploy | 2 dias |

**Total Estimado:** ~2 semanas de desenvolvimento + testes

---

## Checklist de Implementação

```markdown
### Fase 1 — Núcleo (Semana 1-2)
- [ ] Criar tabelas `caixas` e `sangrias`
- [ ] Implementar `caixa_manager.py`
- [ ] Adicionar modal de abertura de caixa em PDV
- [ ] Criar `relatorio_caixa.py` com PDF
- [ ] Implementar backup automático
- [ ] Adicionar log de impressões (tabela + audit)
- [ ] Implement retry TCP/IP para impressoras rede

### Fase 2 — Build & Config (Semana 2-3)
- [ ] Migrar PyInstaller para `--onedir`
- [ ] Criar NSIS installer profissional
- [ ] Implementar carregamento lazy (opencv, pyzbar)
- [ ] Criar `app_config.py` para AppData
- [ ] Adicionar auto-updater

### Fase 3 — Melhorias de UX (Semana 3)
- [ ] Implementar botões de notas (R$5, R$10, ...)
- [ ] Suporte a pagamento misto
- [ ] Arredondamento configurável de troco
- [ ] Gallery de produtos com thumbnails
- [ ] QR Code por produto
- [ ] Logo Manager robusto

### Fase 4 — QA & Deploy (Semana 4)
- [ ] Testes em 3 modelos de impressoras (USB, Rede, Serial)
- [ ] Teste de backup/restore
- [ ] Teste de atualização automática
- [ ] Documentação de migração (v4.2 → v4.3)
- [ ] Release notes
```

---

## Dependências Novas a Adicionar em `requirements.txt`

```text
# Já presentes ✓
customtkinter>=5.0
python-escpos>=3.0
pillow>=9.0
sqlite3 (built-in)
cryptography>=38.0

# Novo — Relatório PDF
reportlab>=3.6.0

# Novo — Auto-update
requests>=2.28.0

# Já presentes mas verificar versão
pandas>=1.5.0
opencv-python>=4.6.0  (lazy-load)
pyzbar>=0.1.9  (lazy-load)
```

---

# 📚 Referências e Recursos

- **SQLite Migrations:** https://sqlite.org/lang_altertable.html
- **Python-escpos:** https://python-escpos.readthedocs.io/
- **ReportLab PDF:** https://www.reportlab.com/docs/reportlab-userguide.pdf
- **CustomTkinter:** https://github.com/TomSchimansky/CustomTkinter
- **NSIS:** https://nsis.sourceforge.io/
- **PyInstaller Best Practices:** https://pyinstaller.org/en/stable/

---

**Documento v1.0 — Junho 2026**
*Preparado para EstokeAoCubo v4.2 → v4.3 Roadmap*
