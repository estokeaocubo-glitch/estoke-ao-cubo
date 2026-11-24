import sqlite3
import pandas as pd
import datetime
import os
import shutil
import sys
import logging
import threading
import uuid
import getpass
import subprocess
import hashlib
import json
from cryptography.fernet import Fernet

__version__ = "4.3.0"

logger = logging.getLogger('EstokeAoCubo.database')


def _restrict_file_acl_windows(filepath: str) -> bool:
    """
    Proteção Fase 1 (Windows): restringe permissões NTFS do arquivo (ex: .db em uso ou .db_key)
    apenas ao usuário atual e à conta SYSTEM (*S-1-5-18), removendo herança de outros usuários da máquina.
    """
    if os.name != 'nt' or not filepath or not os.path.exists(filepath):
        return False
    try:
        username = os.environ.get("USERNAME") or getpass.getuser()
        if not username:
            return False
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        cmd = [
            "icacls",
            filepath,
            "/inheritance:r",
            "/grant:r",
            f"{username}:(F)",
            "/grant:r",
            "*S-1-5-18:(F)",
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creation_flags,
        )
        if res.returncode == 0:
            return True
        logger.debug("icacls retornou código %s para %s: %s", res.returncode, filepath, res.stderr.strip())
    except Exception as exc:
        logger.debug("Não foi possível restringir ACL NTFS de %s: %s", filepath, exc)
    return False


# --- Proteção DPAPI (Windows) + Criptografia Fernet ---
def _dpapi_protect(raw_bytes: bytes) -> bytes:
    """Protege bytes usando Windows DPAPI (CryptProtectData) atrelado ao usuário atual."""
    if os.name != 'nt':
        return raw_bytes
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
        blob_in = DATA_BLOB(len(raw_bytes), buf)
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), "EstokeAoCuboKey", None, None, None, 0x01, ctypes.byref(blob_out)
        ):
            protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return b"DPAPI:" + protected
    except Exception as e:
        logger.debug("Fallback sem DPAPI ao proteger chave: %s", e)
    return raw_bytes


def _dpapi_unprotect(stored_bytes: bytes) -> bytes:
    """Desprotege bytes gerados por _dpapi_protect ou retorna chave legada."""
    if not stored_bytes.startswith(b"DPAPI:"):
        return stored_bytes.strip()
    payload = stored_bytes[6:]
    if os.name != 'nt':
        return payload
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(payload, len(payload))
        blob_in = DATA_BLOB(len(payload), buf)
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return raw.strip()
    except Exception as e:
        logger.error("Erro ao desproteger chave DPAPI: %s", e)
    raise RuntimeError("Não foi possível desproteger a chave do banco de dados (.db_key).")


def _get_key_path():
    """Caminho do arquivo que armazena a chave de criptografia."""
    data_dir = get_app_data_path()
    return os.path.join(data_dir, ".db_key")


def _load_or_create_key():
    """Carrega a chave Fernet existente (migrando para DPAPI se necessário) ou gera uma nova."""
    key_path = _get_key_path()
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            stored = f.read()
        key = _dpapi_unprotect(stored)
        # Migrar chave legada em texto puro para DPAPI automaticamente
        if not stored.startswith(b"DPAPI:") and os.name == 'nt':
            try:
                protected = _dpapi_protect(key)
                if protected.startswith(b"DPAPI:"):
                    with open(key_path, "wb") as fw:
                        fw.write(protected)
            except Exception:
                pass
    else:
        key = Fernet.generate_key()
        protected = _dpapi_protect(key)
        with open(key_path, "wb") as f:
            f.write(protected)
        # Ocultar o arquivo no Windows
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(key_path, 0x02)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass
        logger.info("Chave de criptografia gerada e protegida.")
    _restrict_file_acl_windows(key_path)
    return key


def _encrypt_file(src_path, enc_path, key):
    """Criptografa um arquivo com Fernet (AES-128-CBC)."""
    fernet = Fernet(key)
    with open(src_path, "rb") as f:
        data = f.read()
    encrypted = fernet.encrypt(data)
    tmp_enc = enc_path + ".tmp"
    with open(tmp_enc, "wb") as f:
        f.write(encrypted)
    os.replace(tmp_enc, enc_path)


def _decrypt_file(enc_path, dst_path, key):
    """Descriptografa um arquivo Fernet para o destino."""
    fernet = Fernet(key)
    with open(enc_path, "rb") as f:
        encrypted = f.read()
    data = fernet.decrypt(encrypted)
    with open(dst_path, "wb") as f:
        f.write(data)


import paths


def get_app_data_path():
    """Retorna o caminho para a pasta gravável de dados do aplicativo (ESTOKE_DATA_DIR ou %LOCALAPPDATA%)."""
    return paths.get_app_data_path()


class Database:

    def __init__(self, db_name="estoque.db"):
        self._lock = threading.RLock()
        self._closed = False
        self.current_user_id = None
        self.last_modified = datetime.datetime.now().timestamp()
        self.data_dir = get_app_data_path()
        self.db_path = os.path.join(self.data_dir, db_name)
        self.enc_path = self.db_path + ".enc"
        self._enc_key = _load_or_create_key()

        # Migração Automática: Copiar DB local legado para AppData sem exigir escrita em Program Files
        cand_locals = [os.path.join(paths.get_install_dir(), db_name), os.path.abspath(db_name)]
        for local_db in cand_locals:
            if (
                os.path.abspath(local_db) != os.path.abspath(self.db_path)
                and os.path.exists(local_db)
                and not os.path.exists(self.db_path)
                and not os.path.exists(self.enc_path)
            ):
                try:
                    logger.info("Migrando banco de dados legado para local seguro: %s", self.db_path)
                    shutil.copy2(local_db, self.db_path)
                    try:
                        base, ext = os.path.splitext(local_db)
                        os.rename(local_db, f"{base}_backup_migracao{ext}")
                    except PermissionError:
                        logger.debug("Sem permissão para renomear DB legado em pasta protegida: %s", local_db)
                except (OSError, shutil.Error) as e:
                    logger.error("Erro ao migrar banco de dados legado: %s", e)
                break

        # --- Descriptografar banco com proteção contra perda de dados pós-queda de energia (DB-01) ---
        if os.path.exists(self.db_path) and os.path.getsize(self.db_path) > 0:
            # Se o arquivo .db já existe no disco, a sessão anterior encerrou abruptamente (queda de energia/crash)
            # ou outra conexão no mesmo processo já o descriptografou. NUNCA sobrescrever com um .enc mais antigo!
            logger.warning(
                "Banco ativo/recuperado detectado em disco (%s). Preservando dados sem sobrescrever pelo .enc.",
                self.db_path
            )
            try:
                # Sincronizar o .enc imediatamente com o estado recuperado para garantir durabilidade
                _encrypt_file(self.db_path, self.enc_path, self._enc_key)
            except Exception as sync_err:
                logger.warning("Aviso ao sincronizar .enc na recuperação: %s", sync_err)
        elif os.path.exists(self.enc_path):
            try:
                _decrypt_file(self.enc_path, self.db_path, self._enc_key)
                logger.info("Banco de dados descriptografado com sucesso.")
            except Exception as e:
                logger.error("Erro ao descriptografar banco: %s", e)
                if os.path.exists(self.db_path):
                    logger.warning("Usando banco plaintext como fallback (possível crash anterior).")
                else:
                    raise RuntimeError("Não foi possível descriptografar o banco de dados.") from e

        # Migração de Imagens (Se houver pasta local product_images)
        if getattr(sys, 'frozen', False):
            base_app_path = os.path.dirname(sys.executable)
        else:
            base_app_path = os.path.dirname(os.path.abspath(__file__))

        local_img_dir = os.path.join(base_app_path, "product_images")
        app_data_img_dir = os.path.join(self.data_dir, "product_images")

        if not os.path.exists(app_data_img_dir):
            os.makedirs(app_data_img_dir, exist_ok=True)

        if os.path.exists(local_img_dir):
            try:
                for item in os.listdir(local_img_dir):
                    s = os.path.join(local_img_dir, item)
                    d = os.path.join(app_data_img_dir, item)
                    if os.path.isfile(s) and not os.path.exists(d):
                        shutil.copy2(s, d)
            except (OSError, shutil.Error) as e:
                logger.error("Erro ao migrar imagens: %s", e)

        self.conn = self._open_connection()
        _restrict_file_acl_windows(self.db_path)
        self.create_tables()

    def _open_connection(self):
        """Abre e configura uma conexão SQLite com WAL, Foreign Keys e suporte multi-thread."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=60.0)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA cache_size = -64000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA mmap_size = 268435456")
        except Exception as e:
            logger.warning("Nao foi possivel ativar os pragmas de alta performance: %s", e)
        return conn

    def is_trial_mode(self):
        """Retorna True se o aplicativo estiver rodando em modo trial via fluxo unificado de licença."""
        try:
            from services.updater import get_license_status
            return bool(get_license_status().get("is_trial", False))
        except Exception:
            if os.environ.get('ESTOKE_TRIAL') == '1':
                return True
            executable_name = os.path.basename(sys.executable).lower()
            return "trial" in executable_name

    def contar_produtos(self):
        """Retorna a quantidade de produtos cadastrados na tabela produtos."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM produtos")
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error("Erro ao contar produtos: %s", e)
            return 0

    def contar_transacoes(self):
        """Retorna o total combinado de movimentações (vendas + entradas + gastos)."""
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM vendas")
            vendas_count = cursor.fetchone()[0]
        except Exception as e:
            logger.error("Erro ao contar vendas: %s", e)
            vendas_count = 0

        try:
            cursor.execute("SELECT COUNT(*) FROM entradas")
            entradas_count = cursor.fetchone()[0]
        except Exception as e:
            logger.error("Erro ao contar entradas: %s", e)
            entradas_count = 0

        try:
            cursor.execute("SELECT COUNT(*) FROM gastos")
            gastos_count = cursor.fetchone()[0]
        except Exception as e:
            logger.error("Erro ao contar gastos: %s", e)
            gastos_count = 0

        return vendas_count + entradas_count + gastos_count

    def adicionar_kit(self, nome, quantidade, preco_venda, preco_compra, itens, imagem=None):
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO kits (nome, quantidade, preco_venda, preco_compra, imagem)
                VALUES (?, ?, ?, ?, ?)
            """, (nome, 0, preco_venda, preco_compra, imagem))
            kit_id = cursor.lastrowid
            for item in itens:
                cursor.execute("""
                    INSERT INTO kit_itens (kit_id, produto_nome, quantidade)
                    VALUES (?, ?, ?)
                """, (kit_id, item['nome'], item['quantidade']))
            self.conn.commit()
            self.registrar_alteracao("Adicionar Kit", f"Kit '{nome}' criado com {len(itens)} componentes.")

    def listar_kits(self):
        try:
            return pd.read_sql_query("SELECT * FROM kits", self.conn)
        except Exception as e:
            logger.error("Erro ao listar kits: %s", e)
            return pd.DataFrame()

    def get_kit_components(self, kit_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT produto_nome, quantidade FROM kit_itens WHERE kit_id=?", (kit_id,))
        return [{'nome': row[0], 'quantidade': row[1]} for row in cursor.fetchall()]

    def verificar_disponibilidade_kit(self, kit_id):
        """Retorna a quantidade máxima de kits que podem ser montados com o estoque atual"""
        componentes = self.get_kit_components(kit_id)
        if not componentes:
            return 0
        
        nomes = [comp['nome'] for comp in componentes]
        placeholders = ','.join(['?' for _ in nomes])
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT nome, quantidade FROM produtos WHERE nome IN ({placeholders})", nomes)
        estoque_map = {row[0]: row[1] for row in cursor.fetchall()}
        
        max_kits = float('inf')
        for comp in componentes:
            nome_prod = comp['nome']
            qtd_necessaria = comp['quantidade']
            qtd_estoque = estoque_map.get(nome_prod)
            if qtd_estoque is None:
                return 0
            if qtd_necessaria > 0:
                possiveis = qtd_estoque // qtd_necessaria
                if possiveis < max_kits:
                    max_kits = possiveis
        
        return max_kits if max_kits != float('inf') else 0

    def vender_kit(self, kit_id, qtd_venda=1, forma_pagamento="Dinheiro", cliente="Consumidor Final",
                   registrar_entrada=True, usuario_id=None, registrar_venda_row=None):
        """Deduz de forma atômica os componentes do kit e, se registrar_entrada=True, registra a venda no caixa aberto."""
        if qtd_venda <= 0:
            return False, "Quantidade de venda inválida."
        with self._lock:
            try:
                componentes = self.get_kit_components(kit_id)
                if not componentes:
                    return False, "Kit não encontrado ou sem componentes."
                cursor = self.conn.cursor()

                cursor.execute("SELECT nome, preco_venda, preco_compra FROM kits WHERE id = ?", (kit_id,))
                kit_row = cursor.fetchone()
                nome_kit = kit_row[0] if kit_row else f"Kit #{kit_id}"
                preco_kit_unit = (kit_row[1] or 0.0) if kit_row else 0.0
                custo_kit_unit = (kit_row[2] or 0.0) if kit_row else 0.0

                missing = []
                for comp in componentes:
                    cursor.execute("SELECT quantidade FROM produtos WHERE nome = ?", (comp['nome'],))
                    res = cursor.fetchone()
                    if not res or res[0] < (comp['quantidade'] * qtd_venda):
                        missing.append(comp['nome'])

                if missing:
                    return False, f"Estoque insuficiente para: {', '.join(missing)}"

                valor_componentes = 0.0
                custo_componentes = 0.0
                for comp in componentes:
                    cursor.execute("SELECT preco, preco_compra FROM produtos WHERE nome = ?", (comp['nome'],))
                    res = cursor.fetchone()
                    if res:
                        valor_componentes += (res[0] or 0.0) * comp['quantidade'] * qtd_venda
                        custo_componentes += (res[1] or 0.0) * comp['quantidade'] * qtd_venda

                # Usar o preço promocional do Kit se definido (> 0), senão a soma dos componentes
                valor_total = round((preco_kit_unit * qtd_venda) if preco_kit_unit > 0 else valor_componentes, 2)
                custo_total = round((custo_kit_unit * qtd_venda) if custo_kit_unit > 0 else custo_componentes, 2)
                lucro = round(valor_total - custo_total, 2)

                # Se registrar_venda_row não foi especificado, só cria linha em vendas quando registrar_entrada=True
                # (quando chamado pelo PDV com registrar_entrada=False, registrar_venda_detalhada já grava a linha na tabela vendas)
                if registrar_venda_row is None:
                    registrar_venda_row = bool(registrar_entrada)

                # Baixa efetiva dos componentes
                for comp in componentes:
                    total_deduzir = comp['quantidade'] * qtd_venda
                    cursor.execute(
                        "UPDATE produtos SET quantidade = quantidade - ? WHERE nome = ?",
                        (total_deduzir, comp['nome'])
                    )

                data_venda = datetime.datetime.now().isoformat()
                if registrar_venda_row:
                    caixa = self.caixa_aberto(usuario_id)
                    caixa_id = caixa['id'] if caixa else None
                    v_din = valor_total if forma_pagamento == "Dinheiro" else 0.0
                    v_pix = valor_total if forma_pagamento == "PIX" else 0.0
                    v_deb = valor_total if forma_pagamento == "Cartão Débito" else 0.0
                    v_cred = valor_total if forma_pagamento == "Cartão Crédito" else 0.0
                    v_fia = valor_total if forma_pagamento == "Fiado" else 0.0
                    cursor.execute(
                        """INSERT INTO vendas (
                            produto_id, data_venda, quantidade, valor_total, lucro, forma_pagamento,
                            cliente, status, desconto, caixa_id, valor_dinheiro, valor_pix,
                            valor_cartao_debito, valor_cartao_credito, valor_fiado, formas_pagamento
                        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, 'Concluída', 0.0, ?, ?, ?, ?, ?, ?, ?)""",
                        (data_venda, qtd_venda, valor_total, lucro, forma_pagamento,
                         cliente, caixa_id, v_din, v_pix, v_deb, v_cred, v_fia, forma_pagamento)
                    )
                    venda_id = cursor.lastrowid
                    if caixa_id:
                        cursor.execute(
                            """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, venda_id, forma_pagamento, valor, descricao)
                               VALUES (?, ?, 'VENDA', ?, ?, ?, ?)""",
                            (caixa_id, data_venda, venda_id, forma_pagamento, valor_total, f"Venda Kit: {nome_kit}")
                        )

                if registrar_entrada:
                    cursor.execute(
                        "INSERT INTO entradas (data, valor, descricao) VALUES (?, ?, ?)",
                        (data_venda[:10], valor_total, f"Venda Kit '{nome_kit}' (Qtd: {qtd_venda})")
                    )

                self.conn.commit()
                self.registrar_alteracao("Venda Kit", f"Kit '{nome_kit}' (ID {kit_id}) vendido (Qtd: {qtd_venda}). Total: R$ {valor_total:.2f}")
                return True, f"Venda de kit realizada com sucesso! Total: R$ {valor_total:.2f}"
            except Exception as e:
                self.conn.rollback()
                logger.error("Erro ao vender kit: %s", e)
                return False, str(e)

    def create_tables(self):
        cursor = self.conn.cursor()
        # Tabelas principais
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                quantidade INTEGER NOT NULL,
                preco REAL NOT NULL,
                categoria TEXT,
                quantidade_inicial INTEGER,
                horas_trabalho REAL,
                valor_hora REAL,
                margem_lucro REAL,
                fator_risco REAL,
                fator_raridade REAL,
                preco_compra REAL,
                imagem TEXT,
                data_validade TEXT,
                codigo_barras TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS kits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                quantidade INTEGER NOT NULL,
                preco_venda REAL NOT NULL,
                preco_compra REAL NOT NULL,
                imagem TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS kit_itens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit_id INTEGER NOT NULL,
                produto_nome TEXT NOT NULL,
                quantidade INTEGER NOT NULL,
                FOREIGN KEY (kit_id) REFERENCES kits(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vendas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto_id INTEGER,
                quantidade INTEGER,
                valor_total REAL,
                data_venda TEXT,
                lucro REAL
            )
        ''')
        # Tabela de Gastos (para a IA)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gastos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                valor REAL NOT NULL,
                categoria TEXT NOT NULL,
                descricao TEXT,
                tipo TEXT DEFAULT 'Variável'
            )
        ''')
        # Tabela de Histórico
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS historico (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT NOT NULL,
                acao TEXT NOT NULL,
                detalhe TEXT
            )
        ''')
        # Tabela de Entradas (Saldo)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entradas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                valor REAL NOT NULL,
                descricao TEXT
            )
        ''')
        # Novas Tabelas
        cursor.execute('''CREATE TABLE IF NOT EXISTS metas (id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT, descricao TEXT, status TEXT, prazo TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS fornecedores (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, contato TEXT, produto_fornecido TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, contato TEXT, observacoes TEXT, compras_recorrentes INTEGER DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS estabelecimento (id INTEGER PRIMARY KEY AUTOINCREMENT, area_local TEXT, tarefa TEXT, status TEXT, prioridade TEXT)''')

        # --- Tabela Contas a Receber (Fiado) ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS contas_receber (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER,
            valor REAL NOT NULL,
            data_venda TEXT NOT NULL,
            data_vencimento TEXT,
            status TEXT DEFAULT 'Pendente',
            data_pagamento TEXT,
            descricao TEXT,
            FOREIGN KEY (cliente_id) REFERENCES clientes(id)
        )''')

        # --- Tabela Fidelidade (Pontos) ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS fidelidade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER UNIQUE,
            pontos INTEGER DEFAULT 0,
            total_acumulado REAL DEFAULT 0,
            data_ultima_compra TEXT,
            FOREIGN KEY (cliente_id) REFERENCES clientes(id)
        )''')

        # --- Criar tabela servicos ANTES das migrações para garantir todas as colunas no primeiro boot ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS servicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            cliente TEXT DEFAULT 'Consumidor Final',
            descricao TEXT NOT NULL,
            valor REAL NOT NULL,
            custo REAL DEFAULT 0,
            forma_pagamento TEXT DEFAULT 'Dinheiro',
            observacao TEXT,
            status TEXT DEFAULT 'Concluído',
            vendedor TEXT DEFAULT '',
            status_os TEXT DEFAULT 'Concluído',
            contato_cliente TEXT DEFAULT '',
            equipamento TEXT DEFAULT '',
            data_previsao TEXT DEFAULT '',
            caixa_id INTEGER DEFAULT NULL,
            loja_id INTEGER DEFAULT NULL
        )''')

        # Tabela de categorias customizáveis
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS categorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'produto'
            )
        ''')

        # Tabela de subcategorias (vinculada a uma categoria)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subcategorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                categoria_id INTEGER NOT NULL,
                FOREIGN KEY (categoria_id) REFERENCES categorias(id) ON DELETE CASCADE
            )
        ''')

        # Tabela de tamanhos (vinculada a uma subcategoria)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tamanhos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                subcategoria_id INTEGER NOT NULL,
                FOREIGN KEY (subcategoria_id) REFERENCES subcategorias(id) ON DELETE CASCADE
            )
        ''')

        # Tabela de sub-subcategorias (vinculada a uma subcategoria)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sub_subcategorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                subcategoria_id INTEGER NOT NULL,
                FOREIGN KEY (subcategoria_id) REFERENCES subcategorias(id) ON DELETE CASCADE
            )
        ''')

        # Tabela de configuração da nota personalizada
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config_nota (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_nome TEXT DEFAULT 'Estoke Ao Cubo',
                empresa_email TEXT DEFAULT '',
                empresa_telefone TEXT DEFAULT '',
                empresa_endereco TEXT DEFAULT '',
                empresa_cnpj TEXT DEFAULT '',
                empresa_logo TEXT DEFAULT '',
                mensagem_rodape TEXT DEFAULT 'Obrigado pela preferência!'
            )
        ''')
        # Inserir config padrão se vazia
        cursor.execute("SELECT COUNT(*) FROM config_nota")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO config_nota (empresa_nome) VALUES ('Estoke Ao Cubo')")

        # --- Tabelas de Ficha Técnica (Receitas/Fórmulas) ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS fichas_tecnicas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            rendimento INTEGER DEFAULT 1,
            FOREIGN KEY (produto_id) REFERENCES produtos(id) ON DELETE CASCADE
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS ficha_tecnica_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ficha_id INTEGER NOT NULL,
            insumo_id INTEGER NOT NULL,
            quantidade REAL NOT NULL,
            FOREIGN KEY (ficha_id) REFERENCES fichas_tecnicas(id) ON DELETE CASCADE,
            FOREIGN KEY (insumo_id) REFERENCES produtos(id)
        )''')

        # --- Tabela de Produção Diária ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS producao_diaria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ficha_id INTEGER NOT NULL,
            quantidade_produzida INTEGER NOT NULL,
            data TEXT NOT NULL,
            observacao TEXT,
            FOREIGN KEY (ficha_id) REFERENCES fichas_tecnicas(id)
        )''')

        # --- Tabela de Baixa de Insumos (uso manual) ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS baixa_insumos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            insumo_id INTEGER NOT NULL,
            quantidade REAL NOT NULL,
            data TEXT NOT NULL,
            motivo TEXT,
            FOREIGN KEY (insumo_id) REFERENCES produtos(id)
        )''')

        # --- Tabela de Trocas/Devoluções ---
        cursor.execute('''CREATE TABLE IF NOT EXISTS trocas_devolucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            cliente TEXT,
            produto_devolvido_id INTEGER,
            produto_devolvido_nome TEXT,
            qtd_devolvida INTEGER DEFAULT 1,
            produto_novo_id INTEGER,
            produto_novo_nome TEXT,
            qtd_nova INTEGER DEFAULT 1,
            diferenca_valor REAL DEFAULT 0,
            tipo TEXT DEFAULT 'troca',
            observacao TEXT,
            FOREIGN KEY (produto_devolvido_id) REFERENCES produtos(id),
            FOREIGN KEY (produto_novo_id) REFERENCES produtos(id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS caixa (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_abertura TEXT NOT NULL,
            data_fechamento TEXT,
            valor_abertura REAL NOT NULL DEFAULT 0,
            valor_fechamento REAL,
            total_vendas REAL DEFAULT 0,
            total_dinheiro REAL DEFAULT 0,
            total_pix REAL DEFAULT 0,
            total_cartao REAL DEFAULT 0,
            diferenca REAL,
            observacao TEXT,
            status TEXT DEFAULT 'aberto',
            usuario_id INTEGER DEFAULT 1,
            loja_id INTEGER DEFAULT 1,
            valor_fechamento_declarado REAL,
            valor_fechamento_calculado REAL
        )''')

        # ============ TABELA AUDITORIA DE CAIXA ============
        cursor.execute('''CREATE TABLE IF NOT EXISTS auditoria_caixa (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caixa_id INTEGER NOT NULL,
            data_hora TEXT NOT NULL,
            tipo_operacao TEXT NOT NULL,
            usuario TEXT DEFAULT 'sistema',
            venda_id INTEGER,
            forma_pagamento TEXT,
            valor REAL,
            descricao TEXT,
            valores_antes TEXT,
            valores_depois TEXT,
            FOREIGN KEY (caixa_id) REFERENCES caixa(id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS vendedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            horario_inicio TEXT DEFAULT '08:00',
            horario_fim TEXT DEFAULT '18:00',
            comissao REAL DEFAULT 0,
            ativo INTEGER DEFAULT 1
        )''')

        # ============ TABELAS RESTAURANTE (PDV) ============
        cursor.execute('''CREATE TABLE IF NOT EXISTS mesas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero INTEGER NOT NULL UNIQUE,
            capacidade INTEGER DEFAULT 4,
            status TEXT DEFAULT 'livre',
            area TEXT DEFAULT 'Salão'
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS comandas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mesa_id INTEGER,
            tipo_atendimento TEXT DEFAULT 'mesa',
            cliente TEXT DEFAULT 'Consumidor Final',
            data_abertura TEXT NOT NULL,
            data_fechamento TEXT,
            status TEXT DEFAULT 'aberta',
            taxa_servico REAL DEFAULT 0,
            desconto REAL DEFAULT 0,
            total REAL DEFAULT 0,
            forma_pagamento TEXT,
            observacao TEXT,
            garcom TEXT DEFAULT '',
            FOREIGN KEY (mesa_id) REFERENCES mesas(id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS comanda_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comanda_id INTEGER NOT NULL,
            produto_id INTEGER NOT NULL,
            produto_nome TEXT NOT NULL,
            quantidade INTEGER DEFAULT 1,
            preco_unitario REAL NOT NULL,
            observacao TEXT DEFAULT '',
            status TEXT DEFAULT 'Recebido',
            data_hora TEXT NOT NULL,
            FOREIGN KEY (comanda_id) REFERENCES comandas(id) ON DELETE CASCADE,
            FOREIGN KEY (produto_id) REFERENCES produtos(id)
        )''')

        # ============ MULTI-USUÁRIO / PERMISSÕES ============
        cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            login TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            perfil TEXT NOT NULL DEFAULT 'vendedor',
            ativo INTEGER DEFAULT 1,
            loja_id INTEGER DEFAULT NULL,
            data_criacao TEXT NOT NULL,
            trocar_senha INTEGER DEFAULT 0
        )''')

        # ============ CONTAS A PAGAR ============
        cursor.execute('''CREATE TABLE IF NOT EXISTS contas_pagar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fornecedor_id INTEGER,
            descricao TEXT NOT NULL,
            valor REAL NOT NULL,
            data_emissao TEXT NOT NULL,
            data_vencimento TEXT NOT NULL,
            data_pagamento TEXT,
            status TEXT DEFAULT 'Pendente',
            categoria TEXT DEFAULT 'Outro',
            recorrente INTEGER DEFAULT 0,
            parcela_atual INTEGER DEFAULT 1,
            total_parcelas INTEGER DEFAULT 1,
            observacao TEXT,
            FOREIGN KEY (fornecedor_id) REFERENCES fornecedores(id)
        )''')

        # ============ MÚLTIPLAS LOJAS ============
        cursor.execute('''CREATE TABLE IF NOT EXISTS lojas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            endereco TEXT,
            telefone TEXT,
            cnpj TEXT,
            ativa INTEGER DEFAULT 1,
            data_criacao TEXT NOT NULL
        )''')

        # Tabelas auxiliares
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sangrias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caixa_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                valor REAL NOT NULL,
                motivo TEXT NOT NULL,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                observacao TEXT,
                FOREIGN KEY (caixa_id) REFERENCES caixa(id),
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS suprimentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caixa_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                valor REAL NOT NULL,
                motivo TEXT NOT NULL,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                observacao TEXT,
                FOREIGN KEY (caixa_id) REFERENCES caixa(id),
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS impressoes_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venda_id INTEGER,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL,
                impressora_nome TEXT,
                erro_mensagem TEXT,
                FOREIGN KEY (venda_id) REFERENCES vendas(id) ON DELETE SET NULL
            )
        ''')

        # Executar migrações versionadas (PRAGMA user_version) antes dos seeds
        self.conn.commit()
        from migrations import run_migrations
        run_migrations(self.conn)

        # Inserir loja padrão se vazia
        cursor.execute("SELECT COUNT(*) FROM lojas")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO lojas (nome, data_criacao) VALUES ('Loja Principal', ?)",
                          (datetime.datetime.now().isoformat(),))

        # Inserir admin padrão se vazia (com hash seguro PBKDF2 e flag trocar_senha=1)
        cursor.execute("SELECT COUNT(*) FROM usuarios")
        if cursor.fetchone()[0] == 0:
            from services.security import hash_password
            admin_hash = hash_password("admin9438")
            cursor.execute("""INSERT INTO usuarios (nome, login, senha_hash, perfil, data_criacao, trocar_senha)
                             VALUES ('Administrador', 'admincentral', ?, 'admin', ?, 1)""",
                          (admin_hash, datetime.datetime.now().isoformat()))

        # Inserir categorias padrão se tabela estiver vazia
        cursor.execute("SELECT COUNT(*) FROM categorias")
        if cursor.fetchone()[0] == 0:
            cats_padrao = [
                ('Produto Pronto', 'produto'), ('Material', 'produto'), ('Insumo', 'produto'), ('Outro', 'produto'),
                ('Material', 'gasto'), ('Transporte', 'gasto'), ('Serviços', 'gasto'), ('Aluguel', 'gasto'), ('Outro', 'gasto'),
            ]
            cursor.executemany("INSERT INTO categorias (nome, tipo) VALUES (?, ?)", cats_padrao)

        self.conn.commit()

    # --- Categorias Customizáveis ---
    def listar_categorias(self, tipo='produto'):
        """Retorna lista de nomes de categorias do tipo informado ('produto' ou 'gasto')."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT nome FROM categorias WHERE tipo=? ORDER BY nome", (tipo,))
        return [row[0] for row in cursor.fetchall()]

    def adicionar_categoria(self, nome, tipo='produto'):
        """Adiciona uma nova categoria se ainda não existir."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM categorias WHERE nome=? AND tipo=?", (nome, tipo))
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO categorias (nome, tipo) VALUES (?, ?)", (nome, tipo))
            self.conn.commit()
            return True
        return False

    def remover_categoria(self, nome, tipo='produto'):
        """Remove uma categoria (não remove as padrão se quiser, mas aqui remove qualquer)."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM categorias WHERE nome=? AND tipo=?", (nome, tipo))
        self.conn.commit()

    # --- Subcategorias ---
    def get_categoria_id(self, nome, tipo='produto'):
        """Retorna o id de uma categoria pelo nome e tipo."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM categorias WHERE nome=? AND tipo=?", (nome, tipo))
        row = cursor.fetchone()
        return row[0] if row else None

    def listar_subcategorias(self, categoria_nome, tipo='produto'):
        """Retorna lista de nomes de subcategorias de uma categoria."""
        cat_id = self.get_categoria_id(categoria_nome, tipo)
        if cat_id is None:
            return []
        cursor = self.conn.cursor()
        cursor.execute("SELECT nome FROM subcategorias WHERE categoria_id=? ORDER BY nome", (cat_id,))
        return [row[0] for row in cursor.fetchall()]

    def adicionar_subcategoria(self, categoria_nome, subcategoria_nome, tipo='produto'):
        """Adiciona uma subcategoria a uma categoria."""
        cat_id = self.get_categoria_id(categoria_nome, tipo)
        if cat_id is None:
            return False
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM subcategorias WHERE nome=? AND categoria_id=?", (subcategoria_nome, cat_id))
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO subcategorias (nome, categoria_id) VALUES (?, ?)", (subcategoria_nome, cat_id))
            self.conn.commit()
            return True
        return False

    def remover_subcategoria(self, categoria_nome, subcategoria_nome, tipo='produto'):
        """Remove uma subcategoria."""
        cat_id = self.get_categoria_id(categoria_nome, tipo)
        if cat_id is None:
            return
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM subcategorias WHERE nome=? AND categoria_id=?", (subcategoria_nome, cat_id))
        self.conn.commit()

    def get_subcategoria_id(self, categoria_nome, subcategoria_nome, tipo='produto'):
        """Retorna o id de uma subcategoria."""
        cat_id = self.get_categoria_id(categoria_nome, tipo)
        if cat_id is None:
            return None
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM subcategorias WHERE nome=? AND categoria_id=?", (subcategoria_nome, cat_id))
        row = cursor.fetchone()
        return row[0] if row else None

    # --- Tamanhos ---
    def listar_tamanhos(self, categoria_nome, subcategoria_nome, tipo='produto'):
        """Retorna lista de tamanhos de uma subcategoria."""
        sub_id = self.get_subcategoria_id(categoria_nome, subcategoria_nome, tipo)
        if sub_id is None:
            return []
        cursor = self.conn.cursor()
        cursor.execute("SELECT nome FROM tamanhos WHERE subcategoria_id=? ORDER BY id", (sub_id,))
        return [row[0] for row in cursor.fetchall()]

    def adicionar_tamanho(self, categoria_nome, subcategoria_nome, tamanho_nome, tipo='produto'):
        """Adiciona um tamanho a uma subcategoria."""
        sub_id = self.get_subcategoria_id(categoria_nome, subcategoria_nome, tipo)
        if sub_id is None:
            return False
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tamanhos WHERE nome=? AND subcategoria_id=?", (tamanho_nome, sub_id))
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO tamanhos (nome, subcategoria_id) VALUES (?, ?)", (tamanho_nome, sub_id))
            self.conn.commit()
            return True
        return False

    def remover_tamanho(self, categoria_nome, subcategoria_nome, tamanho_nome, tipo='produto'):
        """Remove um tamanho de uma subcategoria."""
        sub_id = self.get_subcategoria_id(categoria_nome, subcategoria_nome, tipo)
        if sub_id is None:
            return
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM tamanhos WHERE nome=? AND subcategoria_id=?", (tamanho_nome, sub_id))
        self.conn.commit()

    # --- Sub-Subcategorias ---
    def listar_sub_subcategorias(self, categoria_nome, subcategoria_nome, tipo='produto'):
        """Retorna lista de sub-subcategorias de uma subcategoria."""
        sub_id = self.get_subcategoria_id(categoria_nome, subcategoria_nome, tipo)
        if sub_id is None:
            return []
        cursor = self.conn.cursor()
        cursor.execute("SELECT nome FROM sub_subcategorias WHERE subcategoria_id=? ORDER BY nome", (sub_id,))
        return [row[0] for row in cursor.fetchall()]

    def adicionar_sub_subcategoria(self, categoria_nome, subcategoria_nome, sub_sub_nome, tipo='produto'):
        """Adiciona uma sub-subcategoria a uma subcategoria."""
        sub_id = self.get_subcategoria_id(categoria_nome, subcategoria_nome, tipo)
        if sub_id is None:
            return False
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sub_subcategorias WHERE nome=? AND subcategoria_id=?", (sub_sub_nome, sub_id))
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO sub_subcategorias (nome, subcategoria_id) VALUES (?, ?)", (sub_sub_nome, sub_id))
            self.conn.commit()
            return True
        return False

    def remover_sub_subcategoria(self, categoria_nome, subcategoria_nome, sub_sub_nome, tipo='produto'):
        """Remove uma sub-subcategoria."""
        sub_id = self.get_subcategoria_id(categoria_nome, subcategoria_nome, tipo)
        if sub_id is None:
            return
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM sub_subcategorias WHERE nome=? AND subcategoria_id=?", (sub_sub_nome, sub_id))
        self.conn.commit()

    # --- Configuração de Nota Personalizada ---
    def get_config_nota(self):
        """Retorna a configuração da nota personalizada."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM config_nota LIMIT 1")
        row = cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        return {
            'empresa_nome': 'Estoke Ao Cubo', 'empresa_email': '', 'empresa_telefone': '',
            'empresa_endereco': '', 'empresa_cnpj': '', 'empresa_logo': '', 'mensagem_rodape': 'Obrigado pela preferência!'
        }

    def salvar_config_nota(self, config):
        """Salva a configuração da nota personalizada, Point TEF e credenciais fiscais NFC-e."""
        with self._lock:
            cursor = self.conn.cursor()
            cols_exist = {r[1] for r in cursor.execute("PRAGMA table_info(config_nota)").fetchall()}
            updates = {
                "empresa_nome": config.get("empresa_nome", "Estoke Ao Cubo"),
                "empresa_email": config.get("empresa_email", ""),
                "empresa_telefone": config.get("empresa_telefone", ""),
                "empresa_endereco": config.get("empresa_endereco", ""),
                "empresa_cnpj": config.get("empresa_cnpj", ""),
                "empresa_logo": config.get("empresa_logo", ""),
                "mensagem_rodape": config.get("mensagem_rodape", "Obrigado pela preferência!"),
            }
            for opt_col in (
                "mp_point_device_id",
                "csc",
                "id_csc",
                "token_api",
                "ambiente",
                "provedor_fiscal",
                "emitir_nfce_automatico",
                "empresa_ie",
                "regime_tributario",
                "chave_pix",
                "desconto_max_sem_senha",
                "permitir_estoque_negativo",
            ):
                if opt_col in cols_exist and opt_col in config:
                    updates[opt_col] = config[opt_col]

            set_clause = ", ".join(f"{col}=?" for col in updates)
            params = list(updates.values())
            cursor.execute(f"UPDATE config_nota SET {set_clause} WHERE id=1", params)
            self.conn.commit()

    # --- Estoque por Tamanho ---
    def atualizar_estoque_tamanhos(self, produto_id, estoque_p=0, estoque_m=0, estoque_g=0, estoque_gg=0):
        """Atualiza o estoque por tamanho de um produto."""
        cursor = self.conn.cursor()
        cursor.execute("UPDATE produtos SET estoque_p=?, estoque_m=?, estoque_g=?, estoque_gg=? WHERE id=?",
                       (estoque_p, estoque_m, estoque_g, estoque_gg, produto_id))
        self.conn.commit()

    def get_estoque_tamanhos(self, produto_id):
        """Retorna o estoque por tamanho de um produto."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT estoque_p, estoque_m, estoque_g, estoque_gg FROM produtos WHERE id=?", (produto_id,))
        row = cursor.fetchone()
        if row:
            return {'P': row[0] or 0, 'M': row[1] or 0, 'G': row[2] or 0, 'GG': row[3] or 0}
        return {'P': 0, 'M': 0, 'G': 0, 'GG': 0}

    # --- Ranking por Lucro e Vendedor ---
    def ranking_produtos_por_lucro(self, periodo="mes", limit=10):
        """Retorna ranking de produtos por lucro no período."""
        where = ""
        params = []
        hoje = datetime.date.today()
        if periodo == "mes":
            inicio = hoje.replace(day=1)
            where = " WHERE data_venda >= ? AND status='Concluída'"
            params.append(inicio.isoformat())
        elif periodo == "todos":
            where = " WHERE status='Concluída'"
        else:
            where = " WHERE status='Concluída'"

        query = f"""SELECT p.nome, p.tamanho, SUM(v.lucro) as lucro_total, SUM(v.quantidade) as total_vendido,
                    SUM(v.valor_total) as faturamento
                    FROM vendas v LEFT JOIN produtos p ON v.produto_id = p.id
                    {where} GROUP BY v.produto_id ORDER BY lucro_total DESC LIMIT ?"""
        params.append(limit)
        try:
            return pd.read_sql_query(query, self.conn, params=params)
        except Exception as e:
            logger.error("Erro ranking lucro: %s", e)
            return pd.DataFrame()

    def set_current_user(self, user_or_id):
        """Define o usuário ativo na sessão para preenchimento automático da trilha de auditoria."""
        if isinstance(user_or_id, dict):
            self.current_user_id = user_or_id.get("id")
        elif isinstance(user_or_id, int):
            self.current_user_id = user_or_id
        else:
            self.current_user_id = None

    @staticmethod
    def _serialize_audit_value(val):
        if val is None or val == "":
            return ""
        if isinstance(val, str):
            return val
        return json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _compute_audit_hash(hash_anterior, data_hora, uid, acao, detalhe, entidade, entidade_id, antes_str, depois_str):
        uid_str = str(uid) if uid is not None else ""
        payload = "|".join([
            str(hash_anterior or "GENESIS"),
            str(data_hora or ""),
            uid_str,
            str(acao or ""),
            str(detalhe or ""),
            str(entidade or ""),
            str(entidade_id or ""),
            str(antes_str or ""),
            str(depois_str or ""),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def registrar_alteracao(self, acao, detalhe, usuario_id=None, entidade="", entidade_id="", antes=None, depois=None):
        """Grava evento na tabela historico com usuário responsável, snapshot antes/depois e hash encadeado."""
        with self._lock:
            self.last_modified = datetime.datetime.now().timestamp()
            cursor = self.conn.cursor()
            data_hora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            uid = usuario_id if usuario_id is not None else getattr(self, "current_user_id", None)
            entidade_s = str(entidade or "")
            entidade_id_s = str(entidade_id) if entidade_id not in (None, "") else ""
            antes_str = self._serialize_audit_value(antes)
            depois_str = self._serialize_audit_value(depois)

            cursor.execute("SELECT COALESCE(hash_registro, '') FROM historico ORDER BY id DESC LIMIT 1")
            last_row = cursor.fetchone()
            hash_anterior = (last_row[0] if last_row and last_row[0] else "GENESIS")

            hash_registro = self._compute_audit_hash(
                hash_anterior, data_hora, uid, acao, detalhe, entidade_s, entidade_id_s, antes_str, depois_str
            )

            cursor.execute(
                """INSERT INTO historico (
                    data_hora, acao, detalhe, usuario_id, entidade, entidade_id,
                    antes_json, depois_json, hash_anterior, hash_registro
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data_hora, acao, detalhe, uid, entidade_s, entidade_id_s, antes_str, depois_str, hash_anterior, hash_registro)
            )
            self.conn.commit()

    def verificar_integridade_auditoria(self):
        """
        Verifica a cadeia de hashes SHA-256 na tabela historico.
        Retorna (integro: bool, ids_adulterados: list[int]).
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """SELECT id, data_hora, acao, detalhe, usuario_id,
                      COALESCE(entidade, ''), COALESCE(entidade_id, ''),
                      COALESCE(antes_json, ''), COALESCE(depois_json, ''),
                      COALESCE(hash_anterior, ''), COALESCE(hash_registro, '')
               FROM historico ORDER BY id ASC"""
        )
        rows = cursor.fetchall()
        corrompidos = []
        prev_hash = "GENESIS"
        for row in rows:
            rid, dt_hr, acao, detalhe, uid, ent, ent_id, antes_j, depois_j, h_ant, h_reg = row
            if not h_reg:
                # Registro legado anterior à migração v2
                continue
            if h_ant != prev_hash:
                corrompidos.append(rid)
            else:
                esperado = self._compute_audit_hash(
                    h_ant, dt_hr, uid, acao, detalhe, ent, ent_id, antes_j, depois_j
                )
                if h_reg != esperado:
                    corrompidos.append(rid)
            prev_hash = h_reg
        return (len(corrompidos) == 0, corrompidos)

    def adicionar_produto(self, nome, quantidade, preco_venda, categoria=None, preco_compra=None, imagem=None, **kwargs):
        cursor = self.conn.cursor()
        if categoria is None: categoria = ''
        if preco_compra is None: preco_compra = 0
        if imagem is None: imagem = ''
        
        # Extract extra fields with defaults
        horas = kwargs.get('horas_trabalho', 0)
        v_hora = kwargs.get('valor_hora', 0)
        margem = kwargs.get('margem_lucro', 0.20)
        risco = kwargs.get('fator_risco', 0.10)
        raridade = kwargs.get('fator_raridade', 1.0)
        codigo_barras = kwargs.get('codigo_barras', None)
        validade = kwargs.get('data_validade', None)
        lote_serie = kwargs.get('lote_serie', '')
        loja_id = kwargs.get('loja_id', None)

        cursor.execute("""
            INSERT INTO produtos (nome, quantidade, preco, categoria, quantidade_inicial, 
                                horas_trabalho, valor_hora, margem_lucro, fator_risco, fator_raridade, 
                                preco_compra, imagem, data_validade, codigo_barras, lote_serie, loja_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            nome, quantidade, preco_venda, categoria, quantidade, 
            horas, v_hora, margem, risco, raridade, 
            preco_compra, imagem, validade, codigo_barras, lote_serie, loja_id
        ))
        self.conn.commit()
        self.registrar_alteracao("Adicionar Produto", f"Produto '{nome}' adicionado. Qtd: {quantidade}, Preço: {preco_venda}")

    def atualizar_produto(self, id_produto, nome, quantidade, preco, categoria, horas_trabalho=0, valor_hora=0, margem_lucro=0.20, fator_risco=0.10, fator_raridade=1.0, imagem=None, codigo_barras=None, lote_serie=None):
        cursor = self.conn.cursor()
        
        # Verificar se é um reabastecimento (quantidade aumentou significativamente) e capturar snapshot anterior
        cursor.execute("SELECT quantidade, quantidade_inicial, nome, preco, categoria FROM produtos WHERE id=?", (id_produto,))
        row = cursor.fetchone()
        antes_snap = (
            {"nome": row[2], "quantidade": row[0], "preco": row[3], "categoria": row[4]}
            if row else None
        )
        
        # Query base para update
        query_update = (
            "UPDATE produtos SET nome=?, quantidade=?, preco=?, categoria=?, "
            "horas_trabalho=?, valor_hora=?, margem_lucro=?, fator_risco=?, fator_raridade=?, imagem=?, codigo_barras=?, lote_serie=? "
            "WHERE id=?"
        )
        # Se for reabastecimento, atualizar também quantidade_inicial
        if row:
            _, qtd_inicial_antiga, *_ = row
            if quantidade > qtd_inicial_antiga:
                query_update = (
                    "UPDATE produtos SET nome=?, quantidade=?, preco=?, categoria=?, quantidade_inicial=?, "
                    "horas_trabalho=?, valor_hora=?, margem_lucro=?, fator_risco=?, fator_raridade=?, imagem=?, codigo_barras=?, lote_serie=? "
                    "WHERE id=?"
                )
                cursor.execute(query_update, (nome, quantidade, preco, categoria, quantidade, horas_trabalho, valor_hora, margem_lucro, fator_risco, fator_raridade, imagem, codigo_barras, lote_serie or '', id_produto))
            else:
                cursor.execute(query_update, (nome, quantidade, preco, categoria, horas_trabalho, valor_hora, margem_lucro, fator_risco, fator_raridade, imagem, codigo_barras, lote_serie or '', id_produto))
        else:
            cursor.execute(query_update, (nome, quantidade, preco, categoria, horas_trabalho, valor_hora, margem_lucro, fator_risco, fator_raridade, imagem, codigo_barras, lote_serie or '', id_produto))
        self.conn.commit()
        self.registrar_alteracao(
            "Editar Produto",
            f"Produto ID {id_produto} atualizado para '{nome}'",
            entidade="produtos",
            entidade_id=str(id_produto),
            antes=antes_snap,
            depois={"nome": nome, "quantidade": quantidade, "preco": preco, "categoria": categoria},
        )

    def remover_produto(self, id_produto):
        cursor = self.conn.cursor()
        cursor.execute("SELECT nome, quantidade, preco, categoria FROM produtos WHERE id=?", (id_produto,))
        row = cursor.fetchone()
        antes_snap = (
            {"nome": row[0], "quantidade": row[1], "preco": row[2], "categoria": row[3]}
            if row else None
        )
        cursor.execute("DELETE FROM produtos WHERE id=?", (id_produto,))
        self.conn.commit()
        self.registrar_alteracao(
            "Remover Produto",
            f"Produto ID {id_produto} removido",
            entidade="produtos",
            entidade_id=str(id_produto),
            antes=antes_snap,
            depois=None,
        )

    def reabastecer_produto(self, id_produto, quantidade_adicional):
        if quantidade_adicional <= 0:
            return False
        cursor = self.conn.cursor()
        # Pegar quantidade atual e inicial
        cursor.execute("SELECT quantidade, quantidade_inicial FROM produtos WHERE id=?", (id_produto,))
        row = cursor.fetchone()
        if row:
            qtd_atual, qtd_inicial = row
            nova_qtd = qtd_atual + quantidade_adicional
            # Atualizar quantidade e quantidade_inicial (pois é um reabastecimento, o "novo 100%" sobe)
            nova_inicial = max(qtd_inicial, nova_qtd)
            cursor.execute("UPDATE produtos SET quantidade=?, quantidade_inicial=? WHERE id=?", (nova_qtd, nova_inicial, id_produto))
            self.conn.commit()
            self.registrar_alteracao("Reabastecer", f"Produto ID {id_produto} reabastecido (+{quantidade_adicional}). Nova Qtd: {nova_qtd}")
            return True
        return False

    def buscar_produto_por_codigo(self, codigo):
        cursor = self.conn.cursor()
        # Busca por código de barras
        cursor.execute("SELECT * FROM produtos WHERE codigo_barras = ?", (codigo,))
        
        logger.debug("Buscando produto com codigo_barras='%s'", codigo)

        # Retorna DataFrame para compatibilidade
        rows = cursor.fetchall()
        if not rows:
            logger.debug("Nenhum produto encontrado com codigo_barras='%s'", codigo)
            return pd.DataFrame()
        
        logger.debug("Produto encontrado: %s", rows[0])
        # Recuperar nome das colunas
        col_names = [description[0] for description in cursor.description]
        return pd.DataFrame(rows, columns=col_names)

    def listar_produtos(self, loja_id=None):
        if loja_id is not None:
            return pd.read_sql_query("SELECT * FROM produtos WHERE loja_id = ? OR loja_id IS NULL", self.conn, params=[loja_id])
        return pd.read_sql_query("SELECT * FROM produtos", self.conn)

    def adicionar_gasto(self, data, valor, categoria, descricao, tipo="Variável", loja_id=None):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO gastos (data, valor, categoria, descricao, tipo, loja_id) VALUES (?, ?, ?, ?, ?, ?)",
                       (data, valor, categoria, descricao, tipo, loja_id))
        self.conn.commit()
        self.registrar_alteracao("Adicionar Gasto", f"Gasto de R${valor} em '{categoria}' ({descricao}) - {tipo}")

    def atualizar_gasto(self, id_gasto, data, valor, categoria, descricao, tipo="Variável"):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE gastos SET data=?, valor=?, categoria=?, descricao=?, tipo=? WHERE id=?",
                       (data, valor, categoria, descricao, tipo, id_gasto))
        self.conn.commit()
        self.registrar_alteracao("Editar Gasto", f"Gasto ID {id_gasto} atualizado")

    def remover_gasto(self, id_gasto):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM gastos WHERE id=?", (id_gasto,))
        self.conn.commit()
        self.registrar_alteracao("Remover Gasto", f"Gasto ID {id_gasto} removido")

    def adicionar_entrada(self, valor, descricao, loja_id=None):
        cursor = self.conn.cursor()
        data = datetime.date.today().isoformat()
        cursor.execute("INSERT INTO entradas (data, valor, descricao, loja_id) VALUES (?, ?, ?, ?)",
                       (data, valor, descricao, loja_id))
        self.conn.commit()
        self.registrar_alteracao("Adicionar Saldo", f"Entrada de R${valor} ({descricao})")

    def listar_entradas(self, loja_id=None):
        if loja_id is not None:
            return pd.read_sql_query("SELECT * FROM entradas WHERE loja_id = ? OR loja_id IS NULL", self.conn, params=[loja_id])
        return pd.read_sql_query("SELECT * FROM entradas", self.conn)

    def get_saldo_atual(self, loja_id=None):
        cursor = self.conn.cursor()
        if loja_id is not None:
            cursor.execute("SELECT SUM(valor) FROM entradas WHERE loja_id = ? OR loja_id IS NULL", (loja_id,))
            total_entradas = cursor.fetchone()[0] or 0.0
            cursor.execute("SELECT SUM(valor) FROM gastos WHERE loja_id = ? OR loja_id IS NULL", (loja_id,))
            total_gastos = cursor.fetchone()[0] or 0.0
        else:
            cursor.execute("SELECT SUM(valor) FROM entradas")
            total_entradas = cursor.fetchone()[0] or 0.0
            cursor.execute("SELECT SUM(valor) FROM gastos")
            total_gastos = cursor.fetchone()[0] or 0.0
        
        return total_entradas - total_gastos

    # --- Metas ---
    def adicionar_meta(self, titulo, descricao, status="Pendente", prazo=None):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO metas (titulo, descricao, status, prazo) VALUES (?, ?, ?, ?)", (titulo, descricao, status, prazo))
        self.conn.commit()
        self.registrar_alteracao("Adicionar Meta", f"Meta '{titulo}' adicionada")

    def remover_meta(self, id_meta):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM metas WHERE id=?", (id_meta,))
        self.conn.commit()
        self.registrar_alteracao("Remover Meta", f"Meta ID {id_meta} removida")

    def atualizar_status_meta(self, id_meta, novo_status):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE metas SET status=? WHERE id=?", (novo_status, id_meta))
        self.conn.commit()
        self.registrar_alteracao("Atualizar Meta", f"Meta ID {id_meta} status: {novo_status}")

    def atualizar_meta(self, id_meta, titulo, descricao, status, prazo=None):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE metas SET titulo=?, descricao=?, status=?, prazo=? WHERE id=?", 
                     (titulo, descricao, status, prazo, id_meta))
        self.conn.commit()
        self.registrar_alteracao("Atualizar Meta", f"Meta '{titulo}' atualizada")

    def listar_metas(self):
        return pd.read_sql_query("SELECT * FROM metas", self.conn)

    # --- Fornecedores ---
    def adicionar_fornecedor(self, nome, contato, produto_fornecido):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO fornecedores (nome, contato, produto_fornecido) VALUES (?, ?, ?)", (nome, contato, produto_fornecido))
        self.conn.commit()
        self.registrar_alteracao("Adicionar Fornecedor", f"Fornecedor '{nome}' adicionado")

    def remover_fornecedor(self, id_fornecedor):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM fornecedores WHERE id=?", (id_fornecedor,))
        self.conn.commit()
        self.registrar_alteracao("Remover Fornecedor", f"Fornecedor ID {id_fornecedor} removido")

    def listar_fornecedores(self):
        return pd.read_sql_query("SELECT * FROM fornecedores", self.conn)

    # --- Estabelecimento (Organização do Espaço de Trabalho) ---
    def adicionar_tarefa_estabelecimento(self, area_local, tarefa, prioridade="Média", status="Pendente"):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO estabelecimento (area_local, tarefa, status, prioridade) VALUES (?, ?, ?, ?)", 
                       (area_local, tarefa, status, prioridade))
        self.conn.commit()
        self.registrar_alteracao("Adicionar Tarefa Estab.", f"Tarefa '{tarefa}' adicionada à área '{area_local}'")

    def criar_tarefas_estoque_baixo(self):
        """Cria tarefas automáticas para produtos abaixo do estoque mínimo (evita duplicatas)."""
        df_baixo = self.produtos_abaixo_estoque_minimo()
        if df_baixo.empty:
            return 0
        # Buscar tarefas existentes de estoque para evitar duplicatas
        df_tarefas = self.listar_tarefas_estabelecimento()
        tarefas_existentes = set()
        if not df_tarefas.empty:
            for _, t in df_tarefas.iterrows():
                if t['area_local'] == 'Estoque' and t['status'] != 'Concluído':
                    tarefas_existentes.add(t['tarefa'])
        count = 0
        for _, prod in df_baixo.iterrows():
            desc = f"Repor {prod['nome']} (atual: {prod['quantidade']}, mín: {prod['estoque_minimo']})"
            if desc not in tarefas_existentes:
                prioridade = "Alta" if prod['quantidade'] == 0 else "Média"
                self.adicionar_tarefa_estabelecimento("Estoque", desc, prioridade)
                count += 1
        return count

    def remover_tarefa_estabelecimento(self, id_tarefa):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM estabelecimento WHERE id=?", (id_tarefa,))
        self.conn.commit()
        self.registrar_alteracao("Remover Tarefa Estab.", f"Tarefa ID {id_tarefa} removida")

    def atualizar_status_estabelecimento(self, id_tarefa, novo_status):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE estabelecimento SET status=? WHERE id=?", (novo_status, id_tarefa))
        self.conn.commit()
        self.registrar_alteracao("Atualizar Status Estab.", f"Tarefa ID {id_tarefa} marcada como '{novo_status}'")

    def atualizar_tarefa_estabelecimento(self, id_tarefa, area_local, tarefa, prioridade, status):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE estabelecimento SET area_local=?, tarefa=?, prioridade=?, status=? WHERE id=?", 
                       (area_local, tarefa, prioridade, status, id_tarefa))
        self.conn.commit()
        self.registrar_alteracao("Atualizar Tarefa Estab.", f"Tarefa ID {id_tarefa} atualizada")

    def listar_tarefas_estabelecimento(self):
        return pd.read_sql_query(
            "SELECT * FROM estabelecimento ORDER BY CASE prioridade WHEN 'Alta' THEN 1 WHEN 'Média' THEN 2 WHEN 'Baixa' THEN 3 ELSE 4 END",
            self.conn)

    # --- Clientes ---
    def _validar_cpf_cnpj(self, cpf_cnpj):
        if not cpf_cnpj or not str(cpf_cnpj).strip():
            return ""
        digits = "".join(c for c in str(cpf_cnpj) if c.isdigit())
        try:
            from services.security import validar_cpf, validar_cnpj
        except ImportError:
            from utils.formatters import validar_cpf, validar_cnpj
        if len(digits) == 11:
            if not validar_cpf(digits):
                raise ValueError(f"CPF inválido: {cpf_cnpj}")
            return digits
        elif len(digits) == 14:
            if not validar_cnpj(digits):
                raise ValueError(f"CNPJ inválido: {cpf_cnpj}")
            return digits
        else:
            raise ValueError(f"CPF/CNPJ deve conter 11 ou 14 dígitos: {cpf_cnpj}")

    def adicionar_cliente(self, nome, contato, observacoes, cpf_cnpj=""):
        doc = self._validar_cpf_cnpj(cpf_cnpj)
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO clientes (nome, contato, observacoes, cpf_cnpj) VALUES (?, ?, ?, ?)", (nome, contato, observacoes, doc))
        cid = cursor.lastrowid
        self.conn.commit()
        self.registrar_alteracao("Adicionar Cliente", f"Cliente '{nome}' adicionado")
        return cid

    def remover_cliente(self, id_cliente):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM clientes WHERE id=?", (id_cliente,))
        self.conn.commit()
        self.registrar_alteracao("Remover Cliente", f"Cliente ID {id_cliente} removido")

    def listar_clientes(self):
        return pd.read_sql_query("SELECT * FROM clientes", self.conn)

    def atualizar_cliente(self, id_cliente, nome, contato, observacoes, cpf_cnpj=""):
        doc = self._validar_cpf_cnpj(cpf_cnpj)
        cursor = self.conn.cursor()
        cursor.execute("UPDATE clientes SET nome=?, contato=?, observacoes=?, cpf_cnpj=? WHERE id=?", 
                       (nome, contato, observacoes, doc, id_cliente))
        self.conn.commit()
        self.registrar_alteracao("Editar Cliente", f"Cliente ID {id_cliente} atualizado")

    # --- Vendas (Baixa de Estoque) ---
    def registrar_venda(self, produto_id, quantidade, valor_total, data_venda=None, forma_pagamento="Dinheiro", cliente="Consumidor Final"):
        # Verificar se o caixa está aberto
        caixa_atual = self.caixa_aberto()
        if not caixa_atual:
            raise ValueError("Caixa fechado. Abra o caixa antes de registrar vendas.")
        if quantidade <= 0:
            raise ValueError("A quantidade deve ser maior que zero.")

        if data_venda is None:
            data_venda = datetime.datetime.now().isoformat()
            
        cursor = self.conn.cursor()
        caixa_id = caixa_atual['id']
        
        # 1. Verificar Estoque
        cursor.execute("SELECT nome, quantidade, quantidade_inicial, preco_compra FROM produtos WHERE id = ?", (produto_id,))
        prod = cursor.fetchone()
        if not prod:
            raise ValueError("Produto não encontrado")
        
        nome, qtd_atual, qtd_inicial, preco_compra = prod
        preco_compra = preco_compra or 0.0
        if qtd_atual < quantidade:
            raise ValueError(f"Estoque insuficiente. Disponível: {qtd_atual}")
        
        # 2. Atualizar Estoque (Baixa)
        nova_qtd = qtd_atual - quantidade
        cursor.execute("UPDATE produtos SET quantidade = ? WHERE id = ?", (nova_qtd, produto_id))
        
        # 3. Registrar na tabela vendas vinculada ao caixa_id e auditoria_caixa
        lucro = round(valor_total - (preco_compra * quantidade), 2)
        v_din = valor_total if forma_pagamento == "Dinheiro" else 0.0
        v_pix = valor_total if forma_pagamento == "PIX" else 0.0
        v_deb = valor_total if forma_pagamento == "Cartão Débito" else 0.0
        v_cred = valor_total if forma_pagamento == "Cartão Crédito" else 0.0
        v_fia = valor_total if forma_pagamento == "Fiado" else 0.0
        cursor.execute(
            """INSERT INTO vendas (
                produto_id, quantidade, valor_total, data_venda, lucro, forma_pagamento, cliente, status, desconto, caixa_id,
                valor_dinheiro, valor_pix, valor_cartao_debito, valor_cartao_credito, valor_fiado, formas_pagamento, transacao_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Concluída', 0.0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (produto_id, quantidade, valor_total, data_venda, lucro, forma_pagamento, cliente, caixa_id,
             v_din, v_pix, v_deb, v_cred, v_fia, forma_pagamento, uuid.uuid4().hex[:12])
        )
        venda_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, venda_id, forma_pagamento, valor, descricao)
               VALUES (?, ?, 'VENDA', ?, ?, ?, ?)""",
            (caixa_id, data_venda, venda_id, forma_pagamento, valor_total, f"Venda: {quantidade}x {nome}")
        )

        # 4. Registrar Entrada (Saldo)
        cursor.execute("INSERT INTO entradas (data, valor, descricao) VALUES (?, ?, ?)", 
                       (data_venda[:10], valor_total, f"Venda: {quantidade}x {nome}"))
        
        # 5. Registrar Histórico
        self.registrar_alteracao("Venda", f"Venda de {quantidade}un de {nome}. Total: R$ {valor_total:.2f}")
        
        # 6. Verificar Estoque Baixo (30% da quantidade inicial)
        base_calculo = qtd_inicial if qtd_inicial and qtd_inicial > 0 else (nova_qtd + quantidade)
        if base_calculo > 0:
            porcentagem = nova_qtd / base_calculo
            if porcentagem <= 0.30:
                cursor.execute("SELECT id FROM estabelecimento WHERE tarefa LIKE ? AND status != 'Concluído'", (f"%{nome}%",))
                if not cursor.fetchone():
                    cursor.execute("INSERT INTO estabelecimento (area_local, tarefa, status, prioridade) VALUES (?, ?, ?, ?)", 
                                   ("Estoque", f"Repor {nome} (Estoque Baixo: {nova_qtd})", "Pendente", "Alta"))
                    self.registrar_alteracao("Alerta Estoque", f"Gerada tarefa de reposição para {nome}")

        self.conn.commit()

    def listar_gastos(self, loja_id=None):
        if loja_id is not None:
            return pd.read_sql_query("SELECT * FROM gastos WHERE loja_id = ? OR loja_id IS NULL", self.conn, params=[loja_id])
        return pd.read_sql_query("SELECT * FROM gastos", self.conn)

    def listar_historico(self):
        return pd.read_sql_query("SELECT * FROM historico ORDER BY id DESC LIMIT 50", self.conn)

    # --- Relatórios Avançados ---
    def buscar_por_id(self, tabela, id_busca):
        """Busca um registro pelo ID em uma tabela específica"""
        # Validação de segurança: impedir SQL injection no nome da tabela
        tabelas_permitidas = {'produtos', 'gastos', 'entradas', 'historico', 'metas', 
                              'fornecedores', 'clientes', 'estabelecimento', 'kits', 'kit_itens', 'vendas',
                              'fichas_tecnicas', 'ficha_tecnica_itens', 'producao_diaria', 'baixa_insumos', 'trocas_devolucoes', 'servicos'}
        if tabela not in tabelas_permitidas:
            logger.warning("Tentativa de acesso a tabela não permitida: %s", tabela)
            return pd.DataFrame()
        try:
            query = f"SELECT * FROM {tabela} WHERE id = ?"
            return pd.read_sql_query(query, self.conn, params=(id_busca,))
        except sqlite3.DatabaseError as e:
            logger.error("Erro ao buscar por ID: %s", e)
            return pd.DataFrame()

    def listar_transacoes_unificadas(self, periodo="todos", termo_busca=""):
        """
        Lista todas as transações (gastos e entradas/vendas) unificadas.
        Retorna DataFrame com colunas: id, data, tipo, descricao, valor
        """
        params = []
        where_clause = " WHERE 1=1"
        
        # Filtro de Data
        if periodo and periodo != "todos":
            hoje = datetime.date.today()
            inicio = None
            if periodo == "semana":
                inicio = hoje - datetime.timedelta(days=hoje.weekday())
            elif periodo == "mes":
                inicio = hoje.replace(day=1)
            elif periodo == "ano":
                inicio = hoje.replace(month=1, day=1)
            
            if inicio:
                where_clause += " AND data >= ?"
                params.append(inicio.isoformat())
                params.append(inicio.isoformat()) # Precisa duplicar pois será usado em 2 queries UNION

        # Filtro de Busca
        if termo_busca:
            where_clause += " AND descricao LIKE ?"
            params.append(f"%{termo_busca}%")
            params.append(f"%{termo_busca}%") # Duplicar para UNION

        query = f"""
            SELECT id, data, 'Gasto' as tipo, descricao, valor FROM gastos {where_clause}
            UNION ALL
            SELECT id, data, 'Venda' as tipo, descricao, valor FROM entradas {where_clause}
            ORDER BY data DESC
        """
        
        return pd.read_sql_query(query, self.conn, params=params)

    def listar_transacoes_filtradas(self, tabela, periodo="todos", termo_busca=""):
        """
        Lista transações (gastos ou entradas) com filtros de período e busca.
        periodo: 'todos', 'semana', 'mes', 'ano'
        termo_busca: ID ou parte da descrição
        """
        # Validação de segurança: impedir SQL injection no nome da tabela
        tabelas_permitidas = {'gastos', 'entradas'}
        if tabela not in tabelas_permitidas:
            logger.warning("Tentativa de acesso a tabela não permitida em filtro: %s", tabela)
            return pd.DataFrame()
        
        cursor = self.conn.cursor()
        
        query = f"SELECT * FROM {tabela} WHERE 1=1"
        params = []
        
        # Filtro de Data
        if periodo and periodo != "todos":
            hoje = datetime.date.today()
            inicio = None
            if periodo == "semana":
                inicio = hoje - datetime.timedelta(days=hoje.weekday())
            elif periodo == "mes":
                inicio = hoje.replace(day=1)
            elif periodo == "ano":
                inicio = hoje.replace(month=1, day=1)
            
            if inicio:
                query += " AND data >= ?"
                params.append(inicio.isoformat())
            
        # Filtro de Busca (ID ou Descrição/Categoria)
        if termo_busca:
            if termo_busca.isdigit():
                query += " AND id = ?"
                params.append(int(termo_busca))
            else:
                if tabela == 'gastos':
                    query += " AND (descricao LIKE ? OR categoria LIKE ?)"
                    params.extend([f"%{termo_busca}%", f"%{termo_busca}%"])
                else: # entradas
                    query += " AND descricao LIKE ?"
                    params.append(f"%{termo_busca}%")
                    
        query += " ORDER BY data DESC"
        
        return pd.read_sql_query(query, self.conn, params=params)

    # ==================== INSUMOS / MATÉRIA-PRIMA ====================

    def listar_insumos(self):
        """Retorna produtos do tipo 'insumo'."""
        return pd.read_sql_query("SELECT * FROM produtos WHERE tipo_produto='insumo'", self.conn)

    def listar_produtos_vendaveis(self):
        """Retorna produtos que NÃO são insumos (aparecem no PDV)."""
        return pd.read_sql_query("SELECT * FROM produtos WHERE tipo_produto != 'insumo'", self.conn)

    def registrar_baixa_insumo(self, insumo_id, quantidade, motivo="Uso diário"):
        """Registra baixa manual de insumo e atualiza estoque."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT nome, quantidade FROM produtos WHERE id=? AND tipo_produto='insumo'", (insumo_id,))
        row = cursor.fetchone()
        if not row:
            return False, "Insumo não encontrado."
        nome, qtd_atual = row
        if quantidade > qtd_atual:
            return False, f"Estoque insuficiente. Disponível: {qtd_atual}"
        data = datetime.date.today().isoformat()
        cursor.execute("INSERT INTO baixa_insumos (insumo_id, quantidade, data, motivo) VALUES (?, ?, ?, ?)",
                       (insumo_id, quantidade, data, motivo))
        cursor.execute("UPDATE produtos SET quantidade = quantidade - ? WHERE id=?", (quantidade, insumo_id))
        self.conn.commit()
        self.registrar_alteracao("Baixa Insumo", f"{quantidade} de '{nome}' usado ({motivo})")
        return True, f"Baixa de {quantidade} de '{nome}' registrada."

    def listar_baixas_insumos(self, dias=30):
        """Retorna baixas de insumos dos últimos N dias."""
        inicio = (datetime.date.today() - datetime.timedelta(days=dias)).isoformat()
        query = """SELECT bi.id, bi.data, bi.quantidade, bi.motivo, p.nome as insumo_nome
                   FROM baixa_insumos bi LEFT JOIN produtos p ON bi.insumo_id = p.id
                   WHERE bi.data >= ? ORDER BY bi.data DESC"""
        return pd.read_sql_query(query, self.conn, params=[inicio])

    # ==================== FICHAS TÉCNICAS (RECEITAS) ====================

    def adicionar_ficha_tecnica(self, produto_id, nome, rendimento, itens):
        """Cria uma ficha técnica vinculando insumos a um produto final.
        itens: lista de dicts {'insumo_id': int, 'quantidade': float}"""
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO fichas_tecnicas (produto_id, nome, rendimento) VALUES (?, ?, ?)",
                       (produto_id, nome, rendimento))
        ficha_id = cursor.lastrowid
        for item in itens:
            cursor.execute("INSERT INTO ficha_tecnica_itens (ficha_id, insumo_id, quantidade) VALUES (?, ?, ?)",
                           (ficha_id, item['insumo_id'], item['quantidade']))
        self.conn.commit()
        self.registrar_alteracao("Ficha Técnica", f"Ficha '{nome}' criada para produto ID {produto_id}")
        return ficha_id

    def listar_fichas_tecnicas(self):
        """Retorna todas as fichas técnicas com nome do produto."""
        query = """SELECT ft.id, ft.produto_id, ft.nome, ft.rendimento, p.nome as produto_nome
                   FROM fichas_tecnicas ft LEFT JOIN produtos p ON ft.produto_id = p.id
                   ORDER BY ft.nome"""
        return pd.read_sql_query(query, self.conn)

    def get_ficha_tecnica_itens(self, ficha_id):
        """Retorna os insumos de uma ficha técnica."""
        query = """SELECT fti.id, fti.insumo_id, fti.quantidade, p.nome as insumo_nome, 
                          p.quantidade as estoque_atual, p.unidade_medida
                   FROM ficha_tecnica_itens fti LEFT JOIN produtos p ON fti.insumo_id = p.id
                   WHERE fti.ficha_id = ?"""
        return pd.read_sql_query(query, self.conn, params=[ficha_id])

    def remover_ficha_tecnica(self, ficha_id):
        """Remove uma ficha técnica e seus itens."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM fichas_tecnicas WHERE id=?", (ficha_id,))
        self.conn.commit()
        self.registrar_alteracao("Remover Ficha", f"Ficha ID {ficha_id} removida")

    # ==================== PRODUÇÃO DIÁRIA ====================

    def registrar_producao(self, ficha_id, quantidade_produzida, observacao=""):
        """Registra produção: dá entrada no produto final e baixa nos insumos."""
        cursor = self.conn.cursor()
        # Buscar dados da ficha
        cursor.execute("SELECT produto_id, rendimento FROM fichas_tecnicas WHERE id=?", (ficha_id,))
        ficha = cursor.fetchone()
        if not ficha:
            return False, "Ficha técnica não encontrada."
        produto_id, rendimento = ficha

        # Buscar itens da ficha
        cursor.execute("SELECT insumo_id, quantidade FROM ficha_tecnica_itens WHERE ficha_id=?", (ficha_id,))
        itens = cursor.fetchall()

        # Calcular multiplicador (quantas vezes a receita)
        multiplicador = quantidade_produzida / rendimento if rendimento > 0 else quantidade_produzida

        # Verificar estoque dos insumos
        for insumo_id, qtd_base in itens:
            qtd_necessaria = qtd_base * multiplicador
            cursor.execute("SELECT nome, quantidade FROM produtos WHERE id=?", (insumo_id,))
            ins = cursor.fetchone()
            if not ins:
                return False, f"Insumo ID {insumo_id} não encontrado."
            if ins[1] < qtd_necessaria:
                return False, f"Insumo '{ins[0]}' insuficiente. Precisa: {qtd_necessaria:.2f}, Tem: {ins[1]}"

        # Dar baixa nos insumos
        for insumo_id, qtd_base in itens:
            qtd_necessaria = qtd_base * multiplicador
            cursor.execute("UPDATE produtos SET quantidade = quantidade - ? WHERE id=?", (qtd_necessaria, insumo_id))

        # Dar entrada no produto final
        cursor.execute("UPDATE produtos SET quantidade = quantidade + ? WHERE id=?", (quantidade_produzida, produto_id))

        # Registrar produção
        data = datetime.date.today().isoformat()
        cursor.execute("INSERT INTO producao_diaria (ficha_id, quantidade_produzida, data, observacao) VALUES (?, ?, ?, ?)",
                       (ficha_id, quantidade_produzida, data, observacao))
        self.conn.commit()

        cursor.execute("SELECT nome FROM produtos WHERE id=?", (produto_id,))
        prod_nome = cursor.fetchone()[0]
        self.registrar_alteracao("Produção", f"{quantidade_produzida}un de '{prod_nome}' produzidos (Ficha ID {ficha_id})")
        return True, f"{quantidade_produzida} unidades de '{prod_nome}' produzidas com sucesso!"

    def listar_producoes(self, dias=30):
        """Retorna produções dos últimos N dias."""
        inicio = (datetime.date.today() - datetime.timedelta(days=dias)).isoformat()
        query = """SELECT pd.id, pd.data, pd.quantidade_produzida, pd.observacao, 
                          ft.nome as ficha_nome, p.nome as produto_nome
                   FROM producao_diaria pd 
                   LEFT JOIN fichas_tecnicas ft ON pd.ficha_id = ft.id
                   LEFT JOIN produtos p ON ft.produto_id = p.id
                   WHERE pd.data >= ? ORDER BY pd.data DESC"""
        return pd.read_sql_query(query, self.conn, params=[inicio])

    # ==================== TROCAS / DEVOLUÇÕES ====================

    def registrar_troca(self, cliente, prod_devolvido_id, qtd_devolvida, prod_novo_id=None, qtd_nova=0, diferenca=0.0, tipo='troca', obs=""):
        """Registra troca/devolução. Ajusta estoque de ambos os produtos."""
        cursor = self.conn.cursor()
        data = datetime.datetime.now().isoformat()

        # Nome do produto devolvido
        cursor.execute("SELECT nome FROM produtos WHERE id=?", (prod_devolvido_id,))
        dev_row = cursor.fetchone()
        dev_nome = dev_row[0] if dev_row else "Desconhecido"

        # Devolver ao estoque
        cursor.execute("UPDATE produtos SET quantidade = quantidade + ? WHERE id=?", (qtd_devolvida, prod_devolvido_id))

        novo_nome = ""
        if tipo == 'troca' and prod_novo_id and qtd_nova > 0:
            # Verificar estoque do produto novo
            cursor.execute("SELECT nome, quantidade FROM produtos WHERE id=?", (prod_novo_id,))
            novo_row = cursor.fetchone()
            if not novo_row:
                return False, "Produto novo não encontrado."
            novo_nome = novo_row[0]
            if novo_row[1] < qtd_nova:
                return False, f"Estoque insuficiente do produto novo. Disponível: {novo_row[1]}"
            cursor.execute("UPDATE produtos SET quantidade = quantidade - ? WHERE id=?", (qtd_nova, prod_novo_id))

        cursor.execute("""INSERT INTO trocas_devolucoes 
                         (data, cliente, produto_devolvido_id, produto_devolvido_nome, qtd_devolvida,
                          produto_novo_id, produto_novo_nome, qtd_nova, diferenca_valor, tipo, observacao)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (data, cliente, prod_devolvido_id, dev_nome, qtd_devolvida,
                        prod_novo_id, novo_nome, qtd_nova, diferenca, tipo, obs))

        # Se diferença > 0, cliente paga mais; se < 0, loja devolve
        if diferenca != 0:
            desc = f"{'Troca' if tipo == 'troca' else 'Devolução'}: {dev_nome}"
            if diferenca > 0:
                cursor.execute("INSERT INTO entradas (data, valor, descricao) VALUES (?, ?, ?)",
                               (data[:10], diferenca, desc))
            else:
                cursor.execute("INSERT INTO gastos (data, valor, categoria, descricao, tipo) VALUES (?, ?, ?, ?, ?)",
                               (data[:10], abs(diferenca), "Devolução", desc, "Variável"))
        self.conn.commit()
        self.registrar_alteracao("Troca/Devolução", f"{tipo.title()}: '{dev_nome}' → '{novo_nome}' (Cliente: {cliente})")
        return True, f"{'Troca' if tipo == 'troca' else 'Devolução'} registrada com sucesso!"

    def listar_trocas(self, dias=90):
        """Retorna trocas/devoluções dos últimos N dias."""
        inicio = (datetime.date.today() - datetime.timedelta(days=dias)).isoformat()
        return pd.read_sql_query(
            "SELECT * FROM trocas_devolucoes WHERE data >= ? ORDER BY data DESC",
            self.conn, params=[inicio])

    # ==================== LISTA DE REPOSIÇÃO ====================

    def gerar_lista_reposicao(self):
        """Gera lista de produtos abaixo do estoque mínimo para reposição."""
        query = """SELECT id, nome, quantidade, estoque_minimo, categoria, 
                          (estoque_minimo - quantidade) as qtd_repor
                   FROM produtos 
                   WHERE tipo_produto != 'insumo' AND quantidade <= estoque_minimo AND estoque_minimo > 0
                   ORDER BY qtd_repor DESC"""
        return pd.read_sql_query(query, self.conn)

    # ==================== PRODUTOS SOB ENCOMENDA ====================

    def listar_sob_encomenda(self):
        """Retorna produtos sob encomenda."""
        return pd.read_sql_query("SELECT * FROM produtos WHERE sob_encomenda=1", self.conn)

    # --- Serviços ---
    def registrar_servico(self, cliente, descricao, valor, custo=0, forma_pagamento='Dinheiro', observacao='', vendedor='', **kwargs):
        """Registra um serviço prestado (com suporte a OS) e vincula ao caixa se aberto."""
        try:
            import math
            valor = float(valor)
            custo = float(custo or 0)
            if math.isnan(valor) or math.isinf(valor) or valor < 0:
                return False, "Valor do serviço inválido."
            cursor = self.conn.cursor()
            data = datetime.datetime.now().isoformat()
            status_os = kwargs.get('status_os', 'Concluído')
            contato_cliente = kwargs.get('contato_cliente', '')
            equipamento = kwargs.get('equipamento', '')
            data_previsao = kwargs.get('data_previsao', '')
            loja_id = kwargs.get('loja_id', None)
            caixa_atual = self.caixa_aberto()
            caixa_id = caixa_atual['id'] if caixa_atual else None
            cursor.execute(
                """INSERT INTO servicos (
                    data, cliente, descricao, valor, custo, forma_pagamento, observacao,
                    vendedor, status_os, contato_cliente, equipamento, data_previsao, caixa_id, loja_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data, cliente, descricao, valor, custo, forma_pagamento, observacao,
                 vendedor, status_os, contato_cliente, equipamento, data_previsao, caixa_id, loja_id)
            )
            servico_id = cursor.lastrowid
            if caixa_id:
                cursor.execute(
                    """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, forma_pagamento, valor, descricao)
                       VALUES (?, ?, 'SERVICO', ?, ?, ?)""",
                    (caixa_id, data, forma_pagamento, valor, f"OS #{servico_id}: {descricao[:40]}")
                )
            # Registrar como entrada financeira
            self.adicionar_entrada(valor, f"Serviço: {descricao[:50]}", loja_id=loja_id)
            self.conn.commit()
            self.registrar_alteracao("Serviço", f"#{servico_id} | {descricao[:40]} | R$ {valor:.2f} | Cliente: {cliente}")
            return True, servico_id
        except Exception as e:
            logger.error("Erro ao registrar serviço: %s", e)
            return False, str(e)

    def listar_servicos(self, dias=90):
        """Lista serviços dos últimos N dias."""
        data_limite = (datetime.datetime.now() - datetime.timedelta(days=dias)).isoformat()
        return pd.read_sql_query(
            "SELECT * FROM servicos WHERE data >= ? ORDER BY data DESC",
            self.conn, params=[data_limite]
        )

    def atualizar_status_servico(self, servico_id, novo_status):
        """Atualiza status da Ordem de Serviço."""
        try:
            cursor = self.conn.cursor()
            cursor.execute("UPDATE servicos SET status_os = ? WHERE id = ?", (novo_status, servico_id))
            self.conn.commit()
            self.registrar_alteracao("OS Status", f"Serviço #{servico_id} -> {novo_status}")
            return True
        except Exception as e:
            logger.error("Erro ao atualizar status OS: %s", e)
            return False

    def servicos_por_status(self, status):
        """Lista serviços filtrados por status_os."""
        return pd.read_sql_query(
            "SELECT * FROM servicos WHERE status_os = ? ORDER BY data DESC",
            self.conn, params=[status]
        )

    # ==================== CAIXA (ABERTURA/FECHAMENTO) ====================

    def abrir_caixa(self, valor_abertura, observacao="", usuario_id=1, loja_id=1):
        """Abre um novo caixa com valor inicial validado."""
        try:
            import math
            valor_abertura = float(valor_abertura)
            if math.isnan(valor_abertura) or math.isinf(valor_abertura) or valor_abertura < 0:
                return False, "O valor de abertura do caixa não pode ser negativo ou inválido."
            valor_abertura = round(valor_abertura, 2)
            cursor = self.conn.cursor()
            # Verificar se já tem caixa aberto para o usuario
            cursor.execute("SELECT id FROM caixa WHERE status = 'aberto' AND usuario_id = ?", (usuario_id,))
            if cursor.fetchone():
                return False, "Já existe um caixa aberto para este operador. Feche-o antes de abrir um novo."
            data = datetime.datetime.now().isoformat()
            cursor.execute(
                """INSERT INTO caixa (data_abertura, valor_abertura, observacao, status, usuario_id, loja_id) 
                   VALUES (?, ?, ?, 'aberto', ?, ?)""",
                (data, valor_abertura, observacao, usuario_id, loja_id)
            )
            caixa_id = cursor.lastrowid
            cursor.execute(
                """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, usuario, valor, descricao)
                   VALUES (?, ?, 'ABERTURA', ?, ?, ?)""",
                (caixa_id, data, str(usuario_id), valor_abertura, f"Abertura de caixa: R$ {valor_abertura:.2f}")
            )
            self.conn.commit()
            self.registrar_alteracao("Caixa", f"Aberto com R$ {valor_abertura:.2f} por Operador #{usuario_id}")
            return True, "Caixa aberto com sucesso."
        except Exception as e:
            logger.error("Erro ao abrir caixa: %s", e)
            return False, str(e)

    def resumo_caixa_atual(self, usuario_id=None, loja_id=None):
        """Retorna as parciais do caixa atualmente aberto com breakdown detalhado por forma de pagamento."""
        try:
            cursor = self.conn.cursor()
            if usuario_id is not None:
                cursor.execute(
                    "SELECT id, data_abertura, valor_abertura, usuario_id, loja_id FROM caixa WHERE status = 'aberto' AND usuario_id = ?",
                    (usuario_id,)
                )
            else:
                cursor.execute("SELECT id, data_abertura, valor_abertura, usuario_id, loja_id FROM caixa WHERE status = 'aberto'")
            row = cursor.fetchone()
            if not row:
                return None
            caixa_id, data_abertura, valor_abertura, cx_usuario_id, cx_loja_id = row
            valor_abertura = round(float(valor_abertura or 0.0), 2)

            # Total geral de vendas líquidas
            cursor.execute(
                "SELECT COALESCE(SUM(valor_total - desconto), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,)
            )
            total_vendas = round(float(cursor.fetchone()[0] or 0.0), 2)

            # Breakdown por forma de pagamento (Dinheiro a partir de parcelas detalhadas)
            cursor.execute(
                "SELECT COALESCE(SUM(valor_dinheiro), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,)
            )
            total_dinheiro = round(float(cursor.fetchone()[0] or 0.0), 2)

            cursor.execute(
                "SELECT COALESCE(SUM(valor_pix), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,)
            )
            total_pix = round(float(cursor.fetchone()[0] or 0.0), 2)

            cursor.execute(
                "SELECT COALESCE(SUM(valor_cartao_debito + valor_cartao_credito), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,)
            )
            total_cartao = round(float(cursor.fetchone()[0] or 0.0), 2)

            # Vendas a Fiado
            cursor.execute(
                "SELECT COALESCE(SUM(valor_fiado), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,)
            )
            total_fiado = round(float(cursor.fetchone()[0] or 0.0), 2)

            # Contar itens por forma de pagamento
            cursor.execute(
                "SELECT COUNT(*) FROM vendas WHERE caixa_id = ? AND status = 'Concluída' AND (forma_pagamento LIKE '%Dinheiro%' OR valor_dinheiro > 0)",
                (caixa_id,)
            )
            qtd_vendas_dinheiro = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM vendas WHERE caixa_id = ? AND status = 'Concluída' AND (forma_pagamento LIKE '%PIX%' OR valor_pix > 0)",
                (caixa_id,)
            )
            qtd_vendas_pix = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM vendas WHERE caixa_id = ? AND status = 'Concluída' AND (forma_pagamento LIKE '%Cartão%' OR valor_cartao_debito > 0 OR valor_cartao_credito > 0)",
                (caixa_id,)
            )
            qtd_vendas_cartao = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM vendas WHERE caixa_id = ? AND status = 'Concluída' AND (forma_pagamento LIKE '%Fiado%' OR valor_fiado > 0)",
                (caixa_id,)
            )
            qtd_vendas_fiado = cursor.fetchone()[0]

            # Total de sangrias
            cursor.execute(
                "SELECT COALESCE(SUM(valor), 0) FROM sangrias WHERE caixa_id = ?",
                (caixa_id,)
            )
            total_sangrias = round(float(cursor.fetchone()[0] or 0.0), 2)

            # Total de suprimentos (reforço de troco)
            cursor.execute(
                "SELECT COALESCE(SUM(valor), 0) FROM suprimentos WHERE caixa_id = ?",
                (caixa_id,)
            )
            total_suprimentos = round(float(cursor.fetchone()[0] or 0.0), 2)

            # Cálculo do esperado em gaveta (abertura + dinheiro + suprimentos - sangrias)
            esperado = round(valor_abertura + total_dinheiro + total_suprimentos - total_sangrias, 2)
            
            return {
                "id": caixa_id,
                "usuario_id": cx_usuario_id,
                "loja_id": cx_loja_id,
                "data_abertura": data_abertura,
                "valor_abertura": valor_abertura,
                "total_vendas": total_vendas,
                "total_sangrias": total_sangrias,
                "total_suprimentos": total_suprimentos,
                "total_dinheiro": total_dinheiro,
                "total_pix": total_pix,
                "total_cartao": total_cartao,
                "total_fiado": total_fiado,
                "esperado": esperado,
                "esperado_dinheiro": esperado,
                "dinheiro": {
                    "total": total_dinheiro,
                    "quantidade": qtd_vendas_dinheiro,
                    "esperado": esperado
                },
                "pix": {
                    "total": total_pix,
                    "quantidade": qtd_vendas_pix
                },
                "cartao": {
                    "total": total_cartao,
                    "quantidade": qtd_vendas_cartao
                },
                "fiado": {
                    "total": total_fiado,
                    "quantidade": qtd_vendas_fiado
                },
            }
        except Exception as e:
            logger.error("Erro ao gerar resumo do caixa: %s", e)
            return None

    def fechar_caixa(self, valor_fechamento, observacao="", usuario_id=None, conferido_pix=None, conferido_cartao=None):
        """Fecha o caixa aberto (com suporte a conferência cega), calculando divergências e gravando em auditoria."""
        try:
            import math
            valor_fechamento = float(valor_fechamento)
            if math.isnan(valor_fechamento) or math.isinf(valor_fechamento) or valor_fechamento < 0:
                return False, "O valor contado no fechamento não pode ser negativo ou inválido."
            valor_fechamento = round(valor_fechamento, 2)

            resumo = self.resumo_caixa_atual(usuario_id=usuario_id)
            if not resumo:
                return False, "Nenhum caixa aberto para fechar."
            
            caixa_id = resumo['id']
            esperado = resumo['esperado_dinheiro']
            diferenca = round(valor_fechamento - esperado, 2)

            data_fechamento = datetime.datetime.now().isoformat()
            cursor = self.conn.cursor()
            
            # Obter valores esperados pelo sistema
            total_dinheiro = resumo['dinheiro']['total']
            total_pix = resumo['pix']['total']
            total_cartao = resumo['cartao']['total']
            total_vendas = resumo['total_vendas']

            val_pix_conf = round(float(conferido_pix), 2) if conferido_pix is not None else total_pix
            val_cartao_conf = round(float(conferido_cartao), 2) if conferido_cartao is not None else total_cartao
            diff_pix = round(val_pix_conf - total_pix, 2)
            diff_cartao = round(val_cartao_conf - total_cartao, 2)
            
            # Atualizar tabela caixa
            cursor.execute(
                """UPDATE caixa SET data_fechamento=?, valor_fechamento=?, total_vendas=?,
                   total_dinheiro=?, total_pix=?, total_cartao=?, diferenca=?, observacao=?, 
                   valor_fechamento_declarado=?, valor_fechamento_calculado=?, status='fechado'
                   WHERE id=?""",
                (data_fechamento, valor_fechamento, total_vendas, 
                 total_dinheiro, total_pix, total_cartao, 
                 diferenca, observacao, valor_fechamento, esperado, caixa_id)
            )
            
            with self._lock:
                conc_pix = self._conciliar_pix_caixa_locked(
                    caixa_id,
                    data_abertura=resumo.get("data_abertura"),
                    data_fechamento=data_fechamento,
                )

            # Registrar fechamento cego e divergências em auditoria_caixa
            valores_antes = json.dumps({
                "abertura": resumo['valor_abertura'],
                "esperado_dinheiro": esperado,
                "esperado_pix": total_pix,
                "esperado_cartao": total_cartao,
                "suprimentos": resumo.get('total_suprimentos', 0.0),
                "sangrias": resumo['total_sangrias'],
            }, ensure_ascii=False)
            valores_depois = json.dumps({
                "conferido_dinheiro": valor_fechamento,
                "diferenca_dinheiro": diferenca,
                "conferido_pix": val_pix_conf,
                "diferenca_pix": diff_pix,
                "conferido_cartao": val_cartao_conf,
                "diferenca_cartao": diff_cartao,
                "conciliacao_pix": conc_pix,
            }, ensure_ascii=False)
            
            desc_aud = (
                f"Fechamento Cego | Dif. Dinheiro: R$ {diferenca:.2f} | "
                f"Dif. PIX: R$ {diff_pix:.2f} | Dif. Cartão: R$ {diff_cartao:.2f} | Obs: {observacao}"
            )
            cursor.execute('''INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, usuario, valor, descricao, valores_antes, valores_depois)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (caixa_id, data_fechamento, 'FECHAMENTO', str(usuario_id or self.current_user_id or 'sistema'),
                 valor_fechamento, desc_aud, valores_antes, valores_depois))
            
            self.conn.commit()
            self.registrar_alteracao(
                "Caixa",
                f"Fechado | Esperado Din: R$ {esperado:.2f} | Conf Din: R$ {valor_fechamento:.2f} | Dif Din: R$ {diferenca:.2f} | Dif PIX: R$ {diff_pix:.2f} | Dif Cartão: R$ {diff_cartao:.2f}",
                usuario_id=usuario_id,
                entidade="caixa",
                entidade_id=str(caixa_id),
                antes=valores_antes,
                depois=valores_depois,
            )
            
            resumo["informado"] = valor_fechamento
            resumo["esperado"] = esperado
            resumo["diferenca"] = diferenca
            resumo["conferido_pix"] = val_pix_conf
            resumo["diferenca_pix"] = diff_pix
            resumo["conferido_cartao"] = val_cartao_conf
            resumo["diferenca_cartao"] = diff_cartao
            resumo["conciliacao_pix"] = conc_pix
            return True, resumo
        except Exception as e:
            logger.error("Erro ao fechar caixa: %s", e)
            return False, str(e)

    def caixa_aberto(self, usuario_id=None):
        """Retorna dados do caixa aberto ou None."""
        cursor = self.conn.cursor()
        if usuario_id is not None:
            cursor.execute("SELECT * FROM caixa WHERE status = 'aberto' AND usuario_id = ?", (usuario_id,))
        else:
            cursor.execute("SELECT * FROM caixa WHERE status = 'aberto'")
        row = cursor.fetchone()
        if row:
            col_names = [d[0] for d in cursor.description]
            return dict(zip(col_names, row))
        return None

    def registrar_sangria(self, caixa_id, usuario_id, valor, motivo, observacao=""):
        """Registra uma sangria (retirada de caixa) validando saldo físico em gaveta."""
        try:
            import math
            valor = float(valor)
            if math.isnan(valor) or math.isinf(valor) or valor <= 0:
                return False, "O valor da sangria deve ser maior que zero."
            if not motivo or not str(motivo).strip():
                return False, "Informe o motivo da sangria."
            valor = round(valor, 2)

            cursor = self.conn.cursor()
            cursor.execute("SELECT id, valor_abertura, status FROM caixa WHERE id = ?", (caixa_id,))
            cx_row = cursor.fetchone()
            if not cx_row:
                return False, "Caixa não encontrado."
            if cx_row[2] != 'aberto':
                return False, "Não é possível realizar sangria em um caixa fechado."

            # Verificar saldo de dinheiro disponível na gaveta
            valor_abertura = float(cx_row[1] or 0.0)
            cursor.execute(
                "SELECT COALESCE(SUM(valor_dinheiro), 0) FROM vendas WHERE caixa_id = ? AND status = 'Concluída'",
                (caixa_id,)
            )
            total_din = float(cursor.fetchone()[0] or 0.0)
            cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM suprimentos WHERE caixa_id = ?", (caixa_id,))
            total_sup = float(cursor.fetchone()[0] or 0.0)
            cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM sangrias WHERE caixa_id = ?", (caixa_id,))
            total_san = float(cursor.fetchone()[0] or 0.0)
            disponivel_gaveta = round(valor_abertura + total_din + total_sup - total_san, 2)

            if valor > disponivel_gaveta + 0.005:
                return False, f"Saldo em dinheiro insuficiente na gaveta (Disponível: R$ {disponivel_gaveta:.2f})."

            agora = datetime.datetime.now().isoformat()
            cursor.execute(
                """INSERT INTO sangrias (caixa_id, usuario_id, valor, motivo, data_hora, observacao)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (caixa_id, usuario_id, valor, motivo.strip(), agora, observacao)
            )
            cursor.execute(
                """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, usuario, valor, descricao)
                   VALUES (?, ?, 'SANGRIA', ?, ?, ?)""",
                (caixa_id, agora, str(usuario_id), valor, f"Sangria: {motivo.strip()} | {observacao}")
            )
            self.conn.commit()
            self.registrar_alteracao("Sangria", f"Retirada de R$ {valor:.2f} | Motivo: {motivo} | Caixa #{caixa_id}")
            return True, "Sangria registrada com sucesso."
        except Exception as e:
            logger.error("Erro ao registrar sangria: %s", e)
            return False, str(e)

    def listar_sangrias(self, caixa_id):
        """Retorna as sangrias de um determinado caixa."""
        try:
            return pd.read_sql_query(
                "SELECT * FROM sangrias WHERE caixa_id = ? ORDER BY data_hora DESC",
                self.conn, params=[caixa_id]
            )
        except Exception as e:
            logger.error("Erro ao listar sangrias: %s", e)
            return pd.DataFrame()

    def registrar_suprimento(self, caixa_id, usuario_id, valor, motivo="Reforço de troco", observacao=""):
        """Registra um suprimento (entrada de troco/reforço no caixa)."""
        try:
            import math
            valor = float(valor)
            if math.isnan(valor) or math.isinf(valor) or valor <= 0:
                return False, "O valor do suprimento deve ser maior que zero."
            if not motivo or not str(motivo).strip():
                motivo = "Reforço de troco"
            valor = round(valor, 2)

            cursor = self.conn.cursor()
            cursor.execute("SELECT id, status FROM caixa WHERE id = ?", (caixa_id,))
            cx_row = cursor.fetchone()
            if not cx_row:
                return False, "Caixa não encontrado."
            if cx_row[1] != 'aberto':
                return False, "Não é possível realizar suprimento em um caixa fechado."

            agora = datetime.datetime.now().isoformat()
            cursor.execute(
                """INSERT INTO suprimentos (caixa_id, usuario_id, valor, motivo, data_hora, observacao)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (caixa_id, usuario_id, valor, motivo.strip(), agora, observacao)
            )
            cursor.execute(
                """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, usuario, valor, descricao)
                   VALUES (?, ?, 'SUPRIMENTO', ?, ?, ?)""",
                (caixa_id, agora, str(usuario_id), valor, f"Suprimento: {motivo.strip()} | {observacao}")
            )
            self.conn.commit()
            self.registrar_alteracao("Suprimento", f"Reforço de R$ {valor:.2f} | Motivo: {motivo} | Caixa #{caixa_id}")
            return True, "Suprimento registrado com sucesso."
        except Exception as e:
            logger.error("Erro ao registrar suprimento: %s", e)
            return False, str(e)

    def listar_suprimentos(self, caixa_id):
        """Retorna os suprimentos de um determinado caixa."""
        try:
            return pd.read_sql_query(
                "SELECT * FROM suprimentos WHERE caixa_id = ? ORDER BY data_hora DESC",
                self.conn, params=[caixa_id]
            )
        except Exception as e:
            logger.error("Erro ao listar suprimentos: %s", e)
            return pd.DataFrame()

    def calcular_discrepancia_parcial(self, limite_alerta=10.0, usuario_id=None, valor_contado_parcial=None):
        """Calcula discrepâncias parciais durante o expediente do caixa aberto.
        
        Args:
            limite_alerta: Percentual de discrepância que gera alerta (padrão 10%)
            usuario_id: ID opcional do operador
            valor_contado_parcial: Valor contado fisicamente na gaveta (se informado)
        
        Returns:
            Dict com status de discrepâncias ou None se sem caixa aberto
        """
        try:
            resumo = self.resumo_caixa_atual(usuario_id=usuario_id)
            if not resumo:
                return None
            
            cursor = self.conn.cursor()
            caixa_id = resumo['id']
            
            # Analisar: todas as vendas têm caixa_id?
            cursor.execute("""
                SELECT COUNT(*) FROM vendas 
                WHERE status = 'Concluída' 
                AND (caixa_id IS NULL OR caixa_id != ?)
                AND data_venda >= (SELECT data_abertura FROM caixa WHERE id = ?)
            """, (caixa_id, caixa_id))
            vendas_orfas = cursor.fetchone()[0]

            discrepancias = {
                "caixa_id": caixa_id,
                "timestamp": datetime.datetime.now().isoformat(),
                "vendas_sem_caixa": vendas_orfas,
                "alertas": [],
                "status": "OK"
            }
            
            if vendas_orfas > 0:
                discrepancias["alertas"].append(f"⚠️ {vendas_orfas} vendas sem vinculação ao caixa atual")
                discrepancias["status"] = "ALERTA"
            
            # Verificar consistência interna entre total_vendas e a soma das parcelas por forma de pagamento
            soma_formas = round(
                resumo['dinheiro']['total'] + resumo['pix']['total'] +
                resumo['cartao']['total'] + resumo['fiado']['total'], 2
            )
            total_vendas = resumo['total_vendas']
            if total_vendas > 0 and abs(soma_formas - total_vendas) > 0.05:
                pct_inconsistencia = abs(soma_formas - total_vendas) / total_vendas * 100
                if pct_inconsistencia > limite_alerta:
                    discrepancias["alertas"].append(
                        f"⚠️ Divergência entre total de vendas (R$ {total_vendas:.2f}) e formas de pagamento (R$ {soma_formas:.2f})"
                    )
                    discrepancias["status"] = "ALERTA"

            # Se uma contagem parcial física da gaveta foi informada, comparar com o esperado em dinheiro
            total_esperado_dinheiro = resumo['dinheiro']['esperado']
            if valor_contado_parcial is not None and total_esperado_dinheiro > 0:
                percentual_diff = abs(float(valor_contado_parcial) - total_esperado_dinheiro) / total_esperado_dinheiro * 100
                if percentual_diff > limite_alerta:
                    discrepancias["alertas"].append(
                        f"⚠️ Discrepância DINHEIRO: {percentual_diff:.1f}% | "
                        f"Esperado: R$ {total_esperado_dinheiro:.2f} | Contado: R$ {float(valor_contado_parcial):.2f}"
                    )
                    discrepancias["status"] = "ALERTA"
            
            return discrepancias
        except Exception as e:
            logger.error("Erro ao calcular discrepância parcial: %s", e)
            return None

    def relatorio_discrepancia(self, caixa_id):
        """Gera relatório detalhado de discrepâncias para um caixa fechado.
        
        Args:
            caixa_id: ID do caixa a analisar
        
        Returns:
            Dict com análise detalhada das discrepâncias
        """
        try:
            cursor = self.conn.cursor()
            
            # Obter dados do caixa
            cursor.execute("SELECT * FROM caixa WHERE id = ?", (caixa_id,))
            col_names = [d[0] for d in cursor.description]
            row = cursor.fetchone()
            if not row:
                return None
            caixa = dict(zip(col_names, row))

            # Obter sangrias e suprimentos vinculados ao caixa
            cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM sangrias WHERE caixa_id = ?", (caixa_id,))
            total_sangrias = round(float(cursor.fetchone()[0] or 0.0), 2)

            cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM suprimentos WHERE caixa_id = ?", (caixa_id,))
            total_suprimentos = round(float(cursor.fetchone()[0] or 0.0), 2)

            valor_abertura = round(float(caixa.get('valor_abertura') or 0.0), 2)
            total_dinheiro = round(float(caixa.get('total_dinheiro') or 0.0), 2)
            esperado_dinheiro = round(valor_abertura + total_dinheiro + total_suprimentos - total_sangrias, 2)
            informado = round(float(caixa.get('valor_fechamento') or 0.0), 2)
            diferenca = round(informado - esperado_dinheiro, 2)
            
            relatorio = {
                "caixa_id": caixa_id,
                "data_abertura": caixa['data_abertura'],
                "data_fechamento": caixa['data_fechamento'],
                "valor_abertura": valor_abertura,
                "valor_fechamento": informado,
                "total_sangrias": total_sangrias,
                "total_suprimentos": total_suprimentos,
                "diferenca": diferenca,
                "vendas_resumo": {
                    "total": {
                        "valor": round(float(caixa.get('total_vendas') or 0.0), 2),
                        "quantidade": 0
                    },
                    "dinheiro": {
                        "valor": total_dinheiro,
                        "quantidade": 0,
                        "esperado": esperado_dinheiro
                    },
                    "pix": {
                        "valor": round(float(caixa.get('total_pix') or 0.0), 2),
                        "quantidade": 0
                    },
                    "cartao": {
                        "valor": round(float(caixa.get('total_cartao') or 0.0), 2),
                        "quantidade": 0
                    }
                },
                "vendas_por_forma": [],
                "auditoria_eventos": []
            }
            
            # Contar vendas por forma de pagamento
            cursor.execute("""
                SELECT forma_pagamento, COUNT(*) as qtd, SUM(valor_total - desconto) as total
                FROM vendas
                WHERE caixa_id = ? AND status = 'Concluída'
                GROUP BY forma_pagamento
            """, (caixa_id,))
            
            for forma, qtd, total in cursor.fetchall():
                relatorio["vendas_resumo"]["total"]["quantidade"] += qtd
                relatorio["vendas_por_forma"].append({
                    "forma": forma,
                    "quantidade": qtd,
                    "total": round(float(total or 0.0), 2)
                })
                
                if forma and 'Dinheiro' in forma:
                    relatorio["vendas_resumo"]["dinheiro"]["quantidade"] += qtd
                if forma and 'PIX' in forma:
                    relatorio["vendas_resumo"]["pix"]["quantidade"] += qtd
                if forma and 'Cartão' in forma:
                    relatorio["vendas_resumo"]["cartao"]["quantidade"] += qtd
            
            # Obter eventos de auditoria
            cursor.execute("""
                SELECT tipo_operacao, COUNT(*) as qtd, SUM(valor) as total
                FROM auditoria_caixa
                WHERE caixa_id = ?
                GROUP BY tipo_operacao
                ORDER BY tipo_operacao
            """, (caixa_id,))
            
            for tipo, qtd, total in cursor.fetchall():
                relatorio["auditoria_eventos"].append({
                    "tipo": tipo,
                    "quantidade": qtd,
                    "total": round(float(total or 0.0), 2)
                })
            
            # Análise de discrepância (qualquer diferença >= R$ 0,01 já gera ALERTA; > R$ 10,00 gera CRÍTICO)
            diferenca_abs = round(abs(diferenca), 2)
            if esperado_dinheiro > 0:
                pct_diff = (diferenca / esperado_dinheiro) * 100
            else:
                pct_diff = 0.0
            
            if diferenca_abs < 0.01:
                status_disc = "OK"
            elif diferenca_abs <= 10.0:
                status_disc = "ALERTA"
            else:
                status_disc = "CRÍTICO"

            relatorio["analise"] = {
                "esperado_dinheiro": esperado_dinheiro,
                "valor_informado": informado,
                "diferenca_valor": diferenca,
                "diferenca_percentual": round(pct_diff, 2),
                "status": status_disc
            }
            
            return relatorio
        except Exception as e:
            logger.error("Erro ao gerar relatório de discrepância: %s", e)
            return None

    def historico_caixa(self, dias=30):
        """Retorna histórico de caixas fechados."""
        data_limite = (datetime.datetime.now() - datetime.timedelta(days=dias)).isoformat()
        return pd.read_sql_query(
            "SELECT * FROM caixa WHERE status = 'fechado' AND data_abertura >= ? ORDER BY data_abertura DESC",
            self.conn, params=[data_limite]
        )

    # ==================== PRODUTOS MAIS VENDIDOS ====================

    def obter_produtos_mais_vendidos(self, limite=50):
        """Retorna IDs dos produtos mais vendidos ordenados por frequência."""
        try:
            query = """SELECT produto_id, SUM(quantidade) as total_vendido
                       FROM vendas WHERE produto_id IS NOT NULL AND status = 'Concluída'
                       GROUP BY produto_id ORDER BY total_vendido DESC LIMIT ?"""
            df = pd.read_sql_query(query, self.conn, params=[limite])
            return df['produto_id'].tolist() if not df.empty else []
        except Exception:
            return []

    # ==================== VENDEDORES/FUNCIONÁRIOS ====================

    def adicionar_vendedor(self, nome, horario_inicio="08:00", horario_fim="18:00", comissao=0):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO vendedores (nome, horario_inicio, horario_fim, comissao) VALUES (?, ?, ?, ?)",
            (nome, horario_inicio, horario_fim, comissao)
        )
        self.conn.commit()
        self.registrar_alteracao("Vendedor", f"Adicionado: {nome} ({horario_inicio}-{horario_fim})")

    def listar_vendedores(self, apenas_ativos=True):
        filtro = "WHERE ativo = 1" if apenas_ativos else ""
        return pd.read_sql_query(f"SELECT * FROM vendedores {filtro} ORDER BY nome", self.conn)

    def atualizar_vendedor(self, vendedor_id, nome, horario_inicio, horario_fim, comissao):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE vendedores SET nome=?, horario_inicio=?, horario_fim=?, comissao=? WHERE id=?",
            (nome, horario_inicio, horario_fim, comissao, vendedor_id)
        )
        self.conn.commit()

    def remover_vendedor(self, vendedor_id):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE vendedores SET ativo = 0 WHERE id = ?", (vendedor_id,))
        self.conn.commit()
        self.registrar_alteracao("Vendedor", f"Desativado ID #{vendedor_id}")

    def vendedor_no_turno(self):
        """Retorna o vendedor cujo horário inclui a hora atual, ou '' se nenhum."""
        agora = datetime.datetime.now().strftime("%H:%M")
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT nome FROM vendedores WHERE ativo = 1 AND horario_inicio <= ? AND horario_fim > ? ORDER BY horario_inicio",
            (agora, agora)
        )
        row = cursor.fetchone()
        return row[0] if row else ""

    def vendas_por_vendedor(self, dias=30):
        """Retorna resumo de vendas agrupado por vendedor."""
        data_limite = (datetime.datetime.now() - datetime.timedelta(days=dias)).isoformat()
        return pd.read_sql_query(
            """SELECT vendedor, COUNT(*) as num_vendas, SUM(valor_total - desconto) as total_vendido,
               SUM(lucro) as total_lucro
               FROM vendas WHERE vendedor != '' AND vendedor IS NOT NULL
               AND data_venda >= ? AND status = 'Concluída'
               GROUP BY vendedor ORDER BY total_vendido DESC""",
            self.conn, params=[data_limite]
        )

    # ==================== RESTAURANTE (PDV) ====================

    def criar_mesas(self, quantidade, capacidade=4, area="Salão"):
        """Cria mesas em lote. Ignora números já existentes."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(numero), 0) FROM mesas")
        ultimo = cursor.fetchone()[0]
        criadas = 0
        for i in range(1, quantidade + 1):
            num = ultimo + i
            try:
                cursor.execute("INSERT INTO mesas (numero, capacidade, status, area) VALUES (?, ?, 'livre', ?)",
                               (num, capacidade, area))
                criadas += 1
            except sqlite3.IntegrityError:
                pass
        self.conn.commit()
        return criadas

    def listar_mesas(self):
        return pd.read_sql_query("SELECT * FROM mesas ORDER BY numero", self.conn)

    def atualizar_status_mesa(self, mesa_id, status):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE mesas SET status = ? WHERE id = ?", (status, mesa_id))
        self.conn.commit()

    def remover_mesa(self, mesa_id):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM mesas WHERE id = ?", (mesa_id,))
        self.conn.commit()

    def abrir_comanda(self, mesa_id=None, tipo_atendimento="mesa", cliente="Consumidor Final", garcom=""):
        """Abre uma comanda. Para mesa, marca a mesa como ocupada."""
        cursor = self.conn.cursor()
        data = datetime.datetime.now().isoformat()
        if mesa_id and tipo_atendimento == "mesa":
            # Verificar se já existe comanda aberta nesta mesa
            cursor.execute("SELECT id FROM comandas WHERE mesa_id = ? AND status = 'aberta'", (mesa_id,))
            existente = cursor.fetchone()
            if existente:
                return existente[0]  # Retorna comanda já aberta
            cursor.execute("UPDATE mesas SET status = 'ocupada' WHERE id = ?", (mesa_id,))
        cursor.execute(
            "INSERT INTO comandas (mesa_id, tipo_atendimento, cliente, data_abertura, status, garcom) VALUES (?, ?, ?, ?, 'aberta', ?)",
            (mesa_id, tipo_atendimento, cliente, data, garcom))
        self.conn.commit()
        return cursor.lastrowid

    def adicionar_item_comanda(self, comanda_id, produto_id, produto_nome, quantidade, preco_unitario, observacao=""):
        cursor = self.conn.cursor()
        data = datetime.datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO comanda_itens (comanda_id, produto_id, produto_nome, quantidade, preco_unitario, observacao, status, data_hora) VALUES (?, ?, ?, ?, ?, ?, 'Recebido', ?)",
            (comanda_id, produto_id, produto_nome, quantidade, preco_unitario, observacao, data))
        # Atualizar total da comanda
        self._recalcular_total_comanda(comanda_id)
        self.conn.commit()

    def remover_item_comanda(self, item_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT comanda_id FROM comanda_itens WHERE id = ?", (item_id,))
        row = cursor.fetchone()
        if row:
            comanda_id = row[0]
            cursor.execute("DELETE FROM comanda_itens WHERE id = ?", (item_id,))
            self._recalcular_total_comanda(comanda_id)
            self.conn.commit()

    def atualizar_status_item_comanda(self, item_id, status):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE comanda_itens SET status = ? WHERE id = ?", (status, item_id))
        self.conn.commit()

    def listar_itens_comanda(self, comanda_id):
        return pd.read_sql_query(
            "SELECT * FROM comanda_itens WHERE comanda_id = ? ORDER BY data_hora",
            self.conn, params=[comanda_id])

    def _recalcular_total_comanda(self, comanda_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(quantidade * preco_unitario), 0) FROM comanda_itens WHERE comanda_id = ?",
                        (comanda_id,))
        subtotal = cursor.fetchone()[0]
        cursor.execute("UPDATE comandas SET total = ? WHERE id = ?", (subtotal, comanda_id))

    def get_comanda(self, comanda_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM comandas WHERE id = ?", (comanda_id,))
        row = cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        return None

    def get_comanda_por_mesa(self, mesa_id):
        """Retorna comanda aberta de uma mesa ou None."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM comandas WHERE mesa_id = ? AND status = 'aberta' ORDER BY id DESC LIMIT 1",
                        (mesa_id,))
        row = cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        return None

    def fechar_comanda(self, comanda_id, forma_pagamento="Dinheiro", taxa_servico=0, desconto=0, observacao=""):
        """Fecha a comanda, registra a venda vinculada ao caixa e libera a mesa."""
        try:
            cursor = self.conn.cursor()
            comanda = self.get_comanda(comanda_id)
            if not comanda:
                return False, "Comanda não encontrada."
            if comanda['status'] != 'aberta':
                return False, "Comanda já está fechada."

            caixa_atual = self.caixa_aberto()
            caixa_id = caixa_atual['id'] if caixa_atual else None

            data_fechamento = datetime.datetime.now().isoformat()
            total_bruto = round(float(comanda['total'] or 0.0) + float(taxa_servico or 0.0), 2)
            desconto = round(float(desconto or 0.0), 2)
            total_final = round(total_bruto - desconto, 2)

            cursor.execute("""UPDATE comandas SET status='fechada', data_fechamento=?, forma_pagamento=?,
                             taxa_servico=?, desconto=?, total=?, observacao=? WHERE id=?""",
                          (data_fechamento, forma_pagamento, taxa_servico, desconto, total_final, observacao, comanda_id))

            # Liberar a mesa
            if comanda['mesa_id']:
                cursor.execute("UPDATE mesas SET status = 'livre' WHERE id = ?", (comanda['mesa_id'],))

            # Baixar estoque dos itens e calcular custo real
            itens = self.listar_itens_comanda(comanda_id)
            custo_total_itens = 0.0
            for _, item in itens.iterrows():
                cursor.execute("SELECT preco_compra FROM produtos WHERE id = ?", (item['produto_id'],))
                p_row = cursor.fetchone()
                custo_unit = float(p_row[0] or 0.0) if p_row else 0.0
                custo_total_itens += custo_unit * float(item['quantidade'])
                cursor.execute("UPDATE produtos SET quantidade = MAX(0, quantidade - ?) WHERE id = ?",
                              (item['quantidade'], item['produto_id']))

            lucro_real = round(total_final - custo_total_itens, 2)
            v_din = total_final if forma_pagamento == "Dinheiro" else 0.0
            v_pix = total_final if forma_pagamento == "PIX" else 0.0
            v_deb = total_final if forma_pagamento == "Cartão Débito" else 0.0
            v_cred = total_final if forma_pagamento == "Cartão Crédito" else 0.0
            v_fia = total_final if forma_pagamento == "Fiado" else 0.0

            # Registrar venda vinculada ao caixa_id e com colunas por forma de pagamento
            cursor.execute(
                """INSERT INTO vendas (
                    quantidade, valor_total, data_venda, lucro, forma_pagamento, cliente, status, desconto, vendedor, caixa_id,
                    valor_dinheiro, valor_pix, valor_cartao_debito, valor_cartao_credito, valor_fiado, formas_pagamento, transacao_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'Concluída', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (len(itens), total_bruto, data_fechamento, lucro_real, forma_pagamento,
                 comanda.get('cliente', 'Consumidor Final'), desconto, comanda.get('garcom', ''), caixa_id,
                 v_din, v_pix, v_deb, v_cred, v_fia, forma_pagamento, uuid.uuid4().hex[:12]))
            venda_id = cursor.lastrowid

            if caixa_id:
                cursor.execute(
                    """INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, venda_id, forma_pagamento, valor, descricao)
                       VALUES (?, ?, 'VENDA', ?, ?, ?, ?)""",
                    (caixa_id, data_fechamento, venda_id, forma_pagamento, total_final, f"Comanda #{comanda_id}")
                )

            # Registrar entrada financeira
            cursor.execute("INSERT INTO entradas (data, valor, descricao) VALUES (?, ?, ?)",
                          (data_fechamento[:10], total_final, f"Comanda #{comanda_id}"))

            self.conn.commit()
            self.registrar_alteracao("Fechar Comanda",
                f"Comanda #{comanda_id} | Total: R$ {total_final:.2f} | Pgto: {forma_pagamento}")
            return True, f"Comanda #{comanda_id} fechada. Total: R$ {total_final:.2f}"
        except Exception as e:
            logger.error("Erro ao fechar comanda: %s", e)
            return False, str(e)

    def listar_comandas_abertas(self):
        return pd.read_sql_query(
            """SELECT c.*, m.numero as mesa_numero FROM comandas c
               LEFT JOIN mesas m ON c.mesa_id = m.id
               WHERE c.status = 'aberta' ORDER BY c.data_abertura""", self.conn)

    def listar_comandas_historico(self, dias=7):
        limite = (datetime.date.today() - datetime.timedelta(days=dias)).isoformat()
        return pd.read_sql_query(
            """SELECT c.*, m.numero as mesa_numero FROM comandas c
               LEFT JOIN mesas m ON c.mesa_id = m.id
               WHERE c.data_abertura >= ? ORDER BY c.data_abertura DESC""",
            self.conn, params=[limite])

    def itens_cozinha_pendentes(self):
        """Retorna itens que estão pendentes na cozinha (Recebido ou Preparando)."""
        return pd.read_sql_query(
            """SELECT ci.*, c.mesa_id, m.numero as mesa_numero, c.tipo_atendimento
               FROM comanda_itens ci
               JOIN comandas c ON ci.comanda_id = c.id
               LEFT JOIN mesas m ON c.mesa_id = m.id
               WHERE ci.status IN ('Recebido', 'Preparando') AND c.status = 'aberta'
               ORDER BY ci.data_hora""", self.conn)

    def dividir_conta(self, comanda_id, num_pessoas):
        """Calcula divisão da conta."""
        comanda = self.get_comanda(comanda_id)
        if not comanda:
            return None
        total = comanda['total'] + comanda.get('taxa_servico', 0) - comanda.get('desconto', 0)
        valor_por_pessoa = total / max(num_pessoas, 1)
        return {"total": total, "pessoas": num_pessoas, "valor_por_pessoa": round(valor_por_pessoa, 2)}

    def close(self):
        with self._lock:
            if getattr(self, '_closed', False):
                return
            self._closed = True
            conn_closed = False
            try:
                self.conn.close()
                conn_closed = True
                logger.info("Conexão com banco de dados fechada.")
            except Exception as e:
                logger.error("Erro ao fechar conexão: %s", e)

            if not conn_closed:
                # Forçar checkpoint WAL e tentar fechar novamente
                try:
                    self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self.conn.close()
                    conn_closed = True
                except Exception:
                    pass

            # Criptografar o banco ao fechar
            try:
                if os.path.exists(self.db_path):
                    _encrypt_file(self.db_path, self.enc_path, self._enc_key)
                    # Verificar que o .enc foi realmente criado antes de remover plaintext
                    if os.path.exists(self.enc_path) and os.path.getsize(self.enc_path) > 0:
                        os.remove(self.db_path)
                        # Remover também arquivos WAL/SHM do SQLite se existirem
                        for suffix in ("-wal", "-shm"):
                            wal_path = self.db_path + suffix
                            if os.path.exists(wal_path):
                                os.remove(wal_path)
                        logger.info("Banco de dados criptografado com sucesso.")
                    else:
                        logger.error("Criptografia gerou arquivo vazio — mantendo plaintext.")
            except Exception as e:
                logger.error("Erro ao criptografar banco de dados: %s", e)

    # --- Backup Automático com Verificação de Integridade e Retenção GFS ---
    def aplicar_retencao_backups(self, backup_dir=None, diarios=7, semanais=4, mensais=12):
        """
        Aplica política de retenção GFS (Grandfather-Father-Son) em backup_dir:
        - Mantém 1 backup mais recente para cada um dos últimos `diarios` dias com backup.
        - Mantém 1 backup mais recente para cada uma das `semanais` semanas ISO seguintes.
        - Mantém 1 backup mais recente para cada um dos `mensais` meses seguintes.
        Remove do disco os demais arquivos estoque_backup_*.db.enc excedentes.
        """
        target_dir = backup_dir or os.path.join(self.data_dir, "backups")
        if not os.path.exists(target_dir):
            return {"mantidos": [], "removidos": []}

        parsed = []
        for fname in os.listdir(target_dir):
            if fname.startswith("estoque_backup_") and fname.endswith(".db.enc"):
                raw_ts = fname[len("estoque_backup_"):-len(".db.enc")]
                dt = None
                for fmt in ("%Y%m%d_%H%M%S_%f", "%Y%m%d_%H%M%S", "%Y%m%d"):
                    try:
                        sample_len = 22 if fmt == "%Y%m%d_%H%M%S_%f" else (15 if fmt == "%Y%m%d_%H%M%S" else 8)
                        dt = datetime.datetime.strptime(raw_ts[:sample_len], fmt)
                        break
                    except ValueError:
                        continue
                if dt is None:
                    dt = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(target_dir, fname)))
                parsed.append((dt, fname))

        parsed.sort(key=lambda item: (item[0], item[1]), reverse=True)

        keep = set()
        daily_dates = set()
        for dt, fname in parsed:
            d = dt.date()
            if d not in daily_dates and len(daily_dates) < diarios:
                daily_dates.add(d)
                keep.add(fname)

        oldest_daily_date = min(daily_dates) if daily_dates else None

        # Semanais: 1 por semana ISO distinta estritamente anterior à janela diária
        covered_weeks = set()
        weekly_dates = []
        for dt, fname in parsed:
            if fname in keep:
                continue
            if oldest_daily_date is not None and dt.date() >= oldest_daily_date:
                continue
            wk = (dt.isocalendar()[0], dt.isocalendar()[1])
            if wk not in covered_weeks and len(covered_weeks) < semanais:
                covered_weeks.add(wk)
                keep.add(fname)
                weekly_dates.append(dt.date())

        cutoff_monthly_date = min(weekly_dates) if weekly_dates else oldest_daily_date
        covered_months = {(dt.year, dt.month) for dt, fn in parsed if fn in keep}
        monthly_count = 0
        for dt, fname in parsed:
            if fname in keep:
                continue
            if cutoff_monthly_date is not None and dt.date() >= cutoff_monthly_date:
                continue
            ym = (dt.year, dt.month)
            if ym not in covered_months and monthly_count < mensais:
                covered_months.add(ym)
                keep.add(fname)
                monthly_count += 1

        removed = []
        for _, fname in parsed:
            if fname not in keep:
                try:
                    os.remove(os.path.join(target_dir, fname))
                    removed.append(fname)
                    logger.info("Backup fora da janela de retenção GFS removido: %s", fname)
                except OSError as e:
                    logger.warning("Não foi possível remover backup antigo %s: %s", fname, e)

        return {"mantidos": sorted(keep), "removidos": sorted(removed)}

    def criar_backup(self):
        """Cria backup criptografado do banco com integrity_check e retenção GFS (7d/4s/12m)."""
        try:
            backup_dir = os.path.join(self.data_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_plain = os.path.join(backup_dir, f"estoque_backup_{timestamp}.db")
            backup_enc = backup_plain + ".enc"
            
            # Usar backup API do SQLite para cópia segura + verificação de integridade
            backup_conn = sqlite3.connect(backup_plain)
            try:
                self.conn.backup(backup_conn)
                res_int = backup_conn.execute("PRAGMA integrity_check").fetchone()
                if not res_int or res_int[0] != "ok":
                    raise sqlite3.DatabaseError(f"Falha no integrity_check do backup: {res_int}")
            finally:
                backup_conn.close()
            
            # Criptografar o backup e remover plaintext
            _encrypt_file(backup_plain, backup_enc, self._enc_key)
            _restrict_file_acl_windows(backup_enc)
            if os.path.exists(backup_enc) and os.path.getsize(backup_enc) > 0:
                os.remove(backup_plain)
            else:
                logger.error("Criptografia do backup falhou — mantendo plaintext.")
                return True, backup_plain
            
            logger.info("Backup criptografado criado e verificado: %s", backup_enc)
            
            # Aplicar política de retenção 7 diários / 4 semanais / 12 mensais
            self.aplicar_retencao_backups(backup_dir, diarios=7, semanais=4, mensais=12)
            
            # Limpar backups plaintext legados (.db sem .enc)
            for f in list(os.listdir(backup_dir)):
                if f.startswith("estoque_backup_") and f.endswith(".db") and not f.endswith(".db.enc"):
                    try:
                        legacy_path = os.path.join(backup_dir, f)
                        _encrypt_file(legacy_path, legacy_path + ".enc", self._enc_key)
                        if os.path.exists(legacy_path + ".enc") and os.path.getsize(legacy_path + ".enc") > 0:
                            os.remove(legacy_path)
                            logger.info("Backup legado migrado para criptografado: %s", f)
                    except Exception as e:
                        logger.warning("Erro ao migrar backup legado %s: %s", f, e)
            
            return True, backup_enc
        except Exception as e:
            logger.error("Erro ao criar backup: %s", e)
            return False, str(e)

    def verificar_backup_diario(self):
        """Verifica se já existe backup de hoje. Se não, cria automaticamente."""
        try:
            backup_dir = os.path.join(self.data_dir, "backups")
            if not os.path.exists(backup_dir):
                return self.criar_backup()
            
            hoje = datetime.date.today().strftime("%Y%m%d")
            backups_hoje = [
                f for f in os.listdir(backup_dir)
                if f.startswith(f"estoque_backup_{hoje}")
            ]
            
            if not backups_hoje:
                return self.criar_backup()
            return True, "Backup de hoje já existe."
        except Exception as e:
            logger.error("Erro ao verificar backup: %s", e)
            return False, str(e)

    def restaurar_backup(self, backup_path):
        """Restaura banco de dados a partir de um arquivo de backup (criptografado ou legado)."""
        try:
            if not os.path.exists(backup_path):
                return False, "Arquivo de backup não encontrado."
            
            # Criar backup de segurança antes de restaurar
            self.criar_backup()
            
            # Fechar conexão atual
            self.conn.close()
            
            # Guardar cópia de segurança do db atual caso restauração falhe
            db_backup_tmp = self.db_path + ".restore_tmp"
            if os.path.exists(self.db_path):
                shutil.copy2(self.db_path, db_backup_tmp)
            
            try:
                if backup_path.endswith(".db.enc"):
                    _decrypt_file(backup_path, self.db_path, self._enc_key)
                else:
                    shutil.copy2(backup_path, self.db_path)
                
                # Verificar integridade do banco restaurado (incluindo existência de tabelas essenciais)
                test_conn = sqlite3.connect(self.db_path)
                try:
                    res = test_conn.execute("PRAGMA integrity_check").fetchone()
                    if not res or res[0] != "ok":
                        raise sqlite3.DatabaseError(f"Falha de integridade: {res}")
                    test_conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                finally:
                    test_conn.close()
            except Exception as e:
                # Restaurar cópia de segurança
                logger.error("Falha na restauração, revertendo: %s", e)
                if os.path.exists(db_backup_tmp):
                    shutil.copy2(db_backup_tmp, self.db_path)
                self.conn = self._open_connection()
                return False, f"Erro ao restaurar: {e}"
            finally:
                # Limpar arquivo temporário
                if os.path.exists(db_backup_tmp):
                    os.remove(db_backup_tmp)
            
            # Reconectar com pragmas de concorrência e sincronizar .enc
            self.conn = self._open_connection()
            try:
                _encrypt_file(self.db_path, self.enc_path, self._enc_key)
            except Exception:
                pass
            
            logger.info("Backup restaurado de: %s", backup_path)
            return True, "Backup restaurado com sucesso! Reinicie o aplicativo para aplicar todas as alterações."
        except Exception as e:
            logger.error("Erro ao restaurar backup: %s", e)
            try:
                if not hasattr(self, 'conn') or self.conn is None:
                    self.conn = self._open_connection()
            except Exception:
                pass
            return False, str(e)

    def listar_backups(self):
        """Lista todos os backups disponíveis (criptografados e legados) com data e tamanho."""
        backup_dir = os.path.join(self.data_dir, "backups")
        if not os.path.exists(backup_dir):
            return []
        backups = []
        for f in sorted(os.listdir(backup_dir), reverse=True):
            if f.startswith("estoque_backup_") and (f.endswith(".db.enc") or f.endswith(".db")):
                path = os.path.join(backup_dir, f)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                # Extrair data do nome
                try:
                    date_str = f.replace("estoque_backup_", "").replace(".db.enc", "").replace(".db", "")
                    # Suportar formatos com e sem microssegundos
                    dt = datetime.datetime.strptime(date_str[:15], "%Y%m%d_%H%M%S")
                    data_fmt = dt.strftime("%d/%m/%Y %H:%M:%S")
                except Exception:
                    data_fmt = f
                criptografado = "Sim" if f.endswith(".db.enc") else "Não"
                backups.append({"nome": f, "path": path, "data": data_fmt, "tamanho": f"{size_mb:.1f} MB", "criptografado": criptografado})
        return backups

    def produtos_estoque_baixo(self, limite=None):
        """Lista produtos com estoque abaixo do limite configurável."""
        if limite is None:
            limite = 5  # Padrão
        return pd.read_sql_query(
            "SELECT id, nome, quantidade, preco, categoria FROM produtos WHERE quantidade <= ? AND quantidade >= 0 ORDER BY quantidade ASC",
            self.conn, params=(limite,))

    def set_estoque_minimo(self, produto_id, valor):
        """Define o estoque mínimo para um produto específico."""
        cursor = self.conn.cursor()
        cursor.execute("UPDATE produtos SET estoque_minimo = ? WHERE id = ?", (valor, produto_id))
        self.conn.commit()

    def produtos_abaixo_estoque_minimo(self):
        """Lista produtos cuja quantidade está abaixo do estoque mínimo configurado."""
        return pd.read_sql_query(
            "SELECT id, nome, quantidade, preco, categoria, estoque_minimo FROM produtos WHERE quantidade <= estoque_minimo AND quantidade >= 0 ORDER BY quantidade ASC",
            self.conn)

    def exportar_relatorio_pdf(self, tipo="vendas", periodo="mes"):
        """Exporta relatório de vendas/estoque em formato PDF usando pdfplumber/reportlab ou texto formatado."""
        try:
            from datetime import timedelta
            hoje = datetime.date.today()
            
            if periodo == "hoje":
                inicio = hoje
            elif periodo == "semana":
                inicio = hoje - timedelta(days=hoje.weekday())
            elif periodo == "mes":
                inicio = hoje.replace(day=1)
            elif periodo == "ano":
                inicio = hoje.replace(month=1, day=1)
            else:
                inicio = None
            
            if tipo == "vendas":
                query = "SELECT id, data_venda, quantidade, valor_total, lucro, forma_pagamento, cliente, status FROM vendas WHERE status != 'Cancelada'"
                params = []
                if inicio:
                    query += " AND data_venda >= ?"
                    params.append(inicio.isoformat())
                query += " ORDER BY data_venda DESC"
                df = pd.read_sql_query(query, self.conn, params=params)
                titulo = f"Relatório de Vendas - {periodo.title()}"
            elif tipo == "estoque":
                df = pd.read_sql_query("SELECT id, nome, quantidade, preco, categoria, preco_compra FROM produtos ORDER BY nome", self.conn)
                titulo = "Relatório de Estoque"
            elif tipo == "financeiro":
                df_e = pd.read_sql_query("SELECT data, valor, descricao, 'Entrada' as tipo FROM entradas ORDER BY data DESC LIMIT 100", self.conn)
                df_g = pd.read_sql_query("SELECT data, valor, descricao, categoria as tipo FROM gastos ORDER BY data DESC LIMIT 100", self.conn)
                df = pd.concat([df_e, df_g]).sort_values('data', ascending=False)
                titulo = f"Relatório Financeiro - {periodo.title()}"
            else:
                return False, "Tipo de relatório inválido."
            
            # Gerar arquivo de relatório em texto formatado (compatível sem reportlab)
            report_dir = os.path.join(self.data_dir, "relatorios")
            os.makedirs(report_dir, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(report_dir, f"relatorio_{tipo}_{timestamp}.html")
            
            with open(filepath, 'w', encoding='utf-8') as f:
                # Gerar HTML estilizado para melhor apresentação
                f.write("<!DOCTYPE html>\n<html lang='pt-BR'>\n<head>\n<meta charset='UTF-8'>\n")
                f.write(f"<title>{titulo}</title>\n")
                f.write("<style>\n")
                f.write("body{font-family:Arial,sans-serif;margin:40px;background:#f5f5f5;color:#333}\n")
                f.write(".container{max-width:900px;margin:0 auto;background:white;padding:30px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.1)}\n")
                f.write("h1{color:#0A4174;border-bottom:3px solid #0A4174;padding-bottom:10px;font-size:22px}\n")
                f.write(".meta{color:#666;font-size:13px;margin-bottom:20px}\n")
                f.write("table{width:100%;border-collapse:collapse;margin:20px 0}\n")
                f.write("th{background:#0A4174;color:white;padding:10px;text-align:left;font-size:13px}\n")
                f.write("td{padding:8px 10px;border-bottom:1px solid #eee;font-size:12px}\n")
                f.write("tr:nth-child(even){background:#f9f9f9}\n")
                f.write("tr:hover{background:#e8f4fd}\n")
                f.write(".summary{background:#f0f7ff;padding:15px;border-radius:6px;margin-top:20px}\n")
                f.write(".summary b{color:#0A4174}\n")
                f.write(".footer{text-align:center;color:#999;font-size:11px;margin-top:30px;border-top:1px solid #eee;padding-top:15px}\n")
                f.write("@media print{body{margin:0;background:white}.container{box-shadow:none}}\n")
                f.write("</style>\n</head>\n<body>\n<div class='container'>\n")
                f.write(f"<h1>📊 {titulo}</h1>\n")
                f.write(f"<p class='meta'>Gerado em: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</p>\n")
                
                if df.empty:
                    f.write("<p>Nenhum dado encontrado para o período selecionado.</p>\n")
                else:
                    cols = df.columns.tolist()
                    f.write("<table>\n<thead><tr>\n")
                    for c in cols:
                        f.write(f"<th>{c}</th>\n")
                    f.write("</tr></thead>\n<tbody>\n")
                    
                    for _, row in df.iterrows():
                        f.write("<tr>")
                        for c in cols:
                            val = row[c]
                            if isinstance(val, float):
                                f.write(f"<td>{val:.2f}</td>")
                            else:
                                f.write(f"<td>{val}</td>")
                        f.write("</tr>\n")
                    
                    f.write("</tbody>\n</table>\n")
                    
                    f.write("<div class='summary'>\n")
                    f.write(f"<p><b>Total de registros:</b> {len(df)}</p>\n")
                    
                    if tipo == "vendas" and 'valor_total' in df.columns:
                        f.write(f"<p><b>Faturamento Total:</b> R$ {df['valor_total'].sum():.2f}</p>\n")
                        if 'lucro' in df.columns:
                            f.write(f"<p><b>Lucro Total:</b> R$ {df['lucro'].sum():.2f}</p>\n")
                    elif tipo == "estoque" and 'preco' in df.columns:
                        valor_estoque = (df['quantidade'] * df['preco']).sum()
                        f.write(f"<p><b>Valor Total em Estoque:</b> R$ {valor_estoque:.2f}</p>\n")
                    f.write("</div>\n")
                
                f.write("<div class='footer'>EstokeAoCubo - Sistema de Gestão Inteligente</div>\n")
                f.write("</div>\n</body>\n</html>\n")
            
            return True, filepath
        except Exception as e:
            logger.error("Erro ao exportar relatório: %s", e)
            return False, str(e)

    # --- Vendas Detalhadas ---
    def registrar_venda_detalhada(self, itens, total, desconto=0.0, cliente="Consumidor Final", forma_pagamento="Dinheiro", vendedor="", formas_usadas=None, usuario_id=None, loja_id=None, registrar_entrada=True, transacao_id=None):
        """Registra venda detalhada na tabela vendas com itens individuais, validação atômica de estoque e vinculação ao caixa do operador."""
        with self._lock:
            return self._registrar_venda_detalhada_locked(
                itens, total, desconto=desconto, cliente=cliente,
                forma_pagamento=forma_pagamento, vendedor=vendedor,
                formas_usadas=formas_usadas, usuario_id=usuario_id,
                loja_id=loja_id, registrar_entrada=registrar_entrada,
                transacao_id=transacao_id
            )

    def _registrar_venda_detalhada_locked(self, itens, total, desconto=0.0, cliente="Consumidor Final", forma_pagamento="Dinheiro", vendedor="", formas_usadas=None, usuario_id=None, loja_id=None, registrar_entrada=True, transacao_id=None):
        try:
            import math
            if not itens or not isinstance(itens, list):
                return False, "A venda deve possuir pelo menos um item."

            # Verificar se o caixa está aberto (priorizando o operador informado)
            caixa_aberto = self.caixa_aberto(usuario_id=usuario_id) if usuario_id is not None else self.caixa_aberto()
            if not caixa_aberto and usuario_id is not None:
                caixa_aberto = self.caixa_aberto()
            if not caixa_aberto:
                return False, "Caixa fechado. Abra o caixa antes de registrar vendas."

            desconto = round(float(desconto or 0.0), 2)
            if math.isnan(desconto) or math.isinf(desconto) or desconto < 0:
                return False, "Valor de desconto inválido."

            # Validar itens e calcular total bruto real
            total_bruto = 0.0
            for item in itens:
                qtd = float(item.get('qtd', 0))
                preco = float(item.get('preco', 0))
                if math.isnan(qtd) or math.isinf(qtd) or qtd <= 0:
                    return False, "A quantidade de cada item deve ser maior que zero."
                if math.isnan(preco) or math.isinf(preco) or preco < 0:
                    return False, "Preço unitário inválido."
                total_bruto += preco * qtd
            total_bruto = round(total_bruto, 2)

            if desconto > total_bruto + 0.005:
                return False, f"Desconto (R$ {desconto:.2f}) não pode exceder o valor bruto da venda (R$ {total_bruto:.2f})."

            # Detecção inteligente: evita duplo abatimento caso a UI tenha passado total já descontado
            net_total = round(total_bruto - desconto, 2)

            cursor = self.conn.cursor()
            caixa_id = caixa_aberto['id']
            if loja_id is None:
                loja_id = caixa_aberto.get('loja_id')
            data_venda = datetime.datetime.now().isoformat()

            # Pré-validação atômica de estoque para TODOS os itens antes de qualquer escrita
            info_estoque_itens = []
            for item in itens:
                item_type = item.get('type') or item.get('tipo') or 'produto'
                item_id = item.get('id')
                qtd = int(item['qtd']) if float(item['qtd']).is_integer() else float(item['qtd'])

                if item_type == 'kit' and item_id is not None:
                    disp_kit = self.verificar_disponibilidade_kit(item_id)
                    if disp_kit < qtd:
                        return False, f"Estoque insuficiente de componentes para o Kit (Disponível: {disp_kit})."
                    cursor.execute("""
                        SELECT p.id, ki.quantidade, COALESCE(p.preco_compra, 0)
                        FROM kit_itens ki JOIN produtos p ON ki.produto_nome = p.nome
                        WHERE ki.kit_id = ?
                    """, (item_id,))
                    comp_rows = cursor.fetchall()
                    custo_kit_unit = sum(float(r[1]) * float(r[2] or 0.0) for r in comp_rows)
                    info_estoque_itens.append({
                        "is_kit": True,
                        "kit_id": item_id,
                        "componentes": comp_rows,
                        "custo_unit": custo_kit_unit,
                    })
                elif item_id is not None:
                    cursor.execute("SELECT nome, quantidade, quantidade_inicial, preco_compra FROM produtos WHERE id = ?", (item_id,))
                    prod = cursor.fetchone()
                    if not prod:
                        return False, f"Produto ID {item_id} não encontrado."
                    nome_prod, qtd_atual, qtd_inicial, prod_preco_compra = prod
                    if (qtd_atual or 0) < qtd:
                        return False, f"Estoque insuficiente para '{nome_prod}'. Disponível: {qtd_atual}"
                    custo_unit = item.get('preco_compra')
                    if custo_unit is None:
                        custo_unit = prod_preco_compra if prod_preco_compra is not None else 0.0
                    info_estoque_itens.append({
                        "is_kit": False,
                        "produto_id": item_id,
                        "nome_prod": nome_prod,
                        "qtd_atual": qtd_atual,
                        "qtd_inicial": qtd_inicial,
                        "custo_unit": float(custo_unit or 0.0),
                    })
                else:
                    custo_unit = item.get('preco_compra')
                    info_estoque_itens.append({
                        "is_kit": False,
                        "produto_id": None,
                        "custo_unit": float(custo_unit or 0.0),
                    })

            # Mapeamento de formas de pagamento
            pagos = {
                'valor_dinheiro': 0.0,
                'valor_pix': 0.0,
                'valor_cartao_debito': 0.0,
                'valor_cartao_credito': 0.0,
                'valor_fiado': 0.0
            }
            mapping = {
                "Dinheiro": "valor_dinheiro",
                "PIX": "valor_pix",
                "Cartão Débito": "valor_cartao_debito",
                "Cartão Crédito": "valor_cartao_credito",
                "Fiado": "valor_fiado"
            }

            if formas_usadas:
                for f in formas_usadas:
                    col = mapping.get(f.get("forma"))
                    if col:
                        pagos[col] = round(pagos[col] + float(f.get("valor", 0.0)), 2)
            elif forma_pagamento and ("|" in str(forma_pagamento) or ":" in str(forma_pagamento)):
                # Suporte a string de pagamento misto ex: "Dinheiro: 20.00 | PIX: 30.00"
                import re
                partes = re.split(r'[|+]', str(forma_pagamento))
                parsed_any = False
                for parte in partes:
                    parte = parte.strip()
                    for nome_forma, col in mapping.items():
                        if nome_forma.lower() in parte.lower():
                            m = re.search(r'(\d+(?:[.,]\d+)?)', parte)
                            if m:
                                val_f = float(m.group(1).replace(',', '.'))
                                pagos[col] = round(pagos[col] + val_f, 2)
                                parsed_any = True
                            break
                if not parsed_any:
                    pagos['valor_dinheiro'] = net_total
            else:
                col = mapping.get(forma_pagamento)
                if col:
                    pagos[col] = net_total
                else:
                    pagos['valor_dinheiro'] = net_total

            restante_pagos = pagos.copy()
            restante_desconto = desconto
            venda_ids = []
            transacao_id = str(transacao_id).strip() if transacao_id else uuid.uuid4().hex[:12]
            self.ultima_transacao_id = transacao_id

            for idx, item in enumerate(itens):
                info_est = info_estoque_itens[idx]
                qtd = int(item['qtd']) if float(item['qtd']).is_integer() else float(item['qtd'])
                preco_unit = float(item['preco'])
                valor_item = round(preco_unit * qtd, 2)

                if idx == len(itens) - 1:
                    desconto_item = round(restante_desconto, 2)
                else:
                    desconto_item = round((valor_item / total_bruto * desconto), 2) if total_bruto > 0 else 0.0
                    restante_desconto = round(restante_desconto - desconto_item, 2)

                custo_unit = info_est['custo_unit']
                lucro = round((preco_unit - custo_unit) * qtd - desconto_item, 2)

                prop = (valor_item - desconto_item) / net_total if net_total > 0 else (1.0 / len(itens))
                item_pagos = {}
                for col in pagos:
                    if idx == len(itens) - 1:
                        item_pagos[col] = round(restante_pagos[col], 2)
                    else:
                        val_prop = round(pagos[col] * prop, 2)
                        item_pagos[col] = val_prop
                        restante_pagos[col] = round(restante_pagos[col] - val_prop, 2)

                produto_id_db = None if info_est['is_kit'] else info_est.get('produto_id')

                cursor.execute(
                    """INSERT INTO vendas (
                        produto_id, quantidade, valor_total, data_venda, lucro, forma_pagamento, cliente, status, desconto, vendedor, caixa_id,
                        valor_dinheiro, valor_pix, valor_cartao_debito, valor_cartao_credito, valor_fiado, formas_pagamento, transacao_id, loja_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        produto_id_db, qtd, valor_item, data_venda, lucro, forma_pagamento, cliente, 'Concluída', desconto_item, vendedor, caixa_id,
                        item_pagos['valor_dinheiro'], item_pagos['valor_pix'], item_pagos['valor_cartao_debito'], item_pagos['valor_cartao_credito'], item_pagos['valor_fiado'],
                        forma_pagamento, transacao_id, loja_id
                    )
                )
                v_id = cursor.lastrowid
                venda_ids.append((v_id, round(valor_item - desconto_item, 2)))

                # Baixa de estoque
                if info_est['is_kit']:
                    for comp_prod_id, comp_qtd_kit, _ in info_est['componentes']:
                        baixa_comp = int(comp_qtd_kit * qtd)
                        cursor.execute("UPDATE produtos SET quantidade = quantidade - ? WHERE id = ?", (baixa_comp, comp_prod_id))
                elif produto_id_db is not None:
                    nova_qtd = info_est['qtd_atual'] - qtd
                    cursor.execute("UPDATE produtos SET quantidade = ? WHERE id = ?", (nova_qtd, produto_id_db))
                    nome_prod = info_est['nome_prod']
                    qtd_inicial = info_est['qtd_inicial']
                    base = qtd_inicial if qtd_inicial and qtd_inicial > 0 else (nova_qtd + qtd)
                    if base > 0 and (nova_qtd / base) <= 0.30:
                        cursor.execute("SELECT id FROM estabelecimento WHERE tarefa LIKE ? AND status != 'Concluído'", (f"%{nome_prod}%",))
                        if not cursor.fetchone():
                            cursor.execute(
                                "INSERT INTO estabelecimento (area_local, tarefa, status, prioridade) VALUES (?, ?, ?, ?)",
                                ("Estoque", f"Repor {nome_prod} (Estoque Baixo: {nova_qtd})", "Pendente", "Alta")
                            )

            # Registrar entrada financeira pelo valor líquido real
            if registrar_entrada:
                cursor.execute(
                    "INSERT INTO entradas (data, valor, descricao, loja_id) VALUES (?, ?, ?, ?)",
                    (data_venda[:10], net_total, f"Venda PDV: {len(itens)} itens | {cliente} | {forma_pagamento}", loja_id)
                )

            # Atualizar compras_recorrentes e fidelidade do cliente (sobre valor líquido)
            if cliente != "Consumidor Final":
                cursor.execute("UPDATE clientes SET compras_recorrentes = compras_recorrentes + 1 WHERE nome = ?", (cliente,))
                pontos_ganhos = int(net_total // 10)
                if pontos_ganhos > 0:
                    cursor.execute("SELECT id FROM clientes WHERE nome = ?", (cliente,))
                    c_row = cursor.fetchone()
                    if c_row:
                        cursor.execute('''INSERT INTO fidelidade (cliente_id, pontos, total_acumulado, data_ultima_compra)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(cliente_id) DO UPDATE SET
                                pontos = pontos + excluded.pontos,
                                total_acumulado = total_acumulado + excluded.total_acumulado,
                                data_ultima_compra = excluded.data_ultima_compra''',
                            (c_row[0], pontos_ganhos, net_total, data_venda))

            # Registrar em AUDITORIA_CAIXA para cada item vendido
            ids_apenas = []
            for venda_id, val_liq_item in venda_ids:
                ids_apenas.append(venda_id)
                cursor.execute(
                    '''INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, venda_id, forma_pagamento, valor, descricao)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (caixa_id, data_venda, 'VENDA', venda_id, forma_pagamento, val_liq_item, f"Venda registrada: {cliente}")
                )

            self.conn.commit()
            self.registrar_alteracao(
                "Venda Detalhada",
                f"{len(itens)} itens | Líquido: R$ {net_total:.2f} | Desc: R$ {desconto:.2f} | Cliente: {cliente} | Pgto: {forma_pagamento}"
            )
            return True, ids_apenas
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            logger.error("Erro ao registrar venda detalhada: %s", e)
            return False, str(e)

    def listar_vendas(self, periodo="todos", loja_id=None):
        """Lista vendas da tabela vendas com filtro de período (exclui canceladas)."""
        where = "WHERE status != 'Cancelada'"
        params = []
        
        if periodo and periodo != "todos":
            hoje = datetime.date.today()
            inicio = None
            if periodo == "hoje":
                inicio = hoje
            elif periodo == "semana":
                inicio = hoje - datetime.timedelta(days=hoje.weekday())
            elif periodo == "mes":
                inicio = hoje.replace(day=1)
            elif periodo == "ano":
                inicio = hoje.replace(month=1, day=1)
            
            if inicio:
                where += " AND data_venda >= ?"
                params.append(inicio.isoformat())
        if loja_id is not None:
            where += " AND (loja_id = ? OR loja_id IS NULL)"
            params.append(loja_id)
        
        query = f"SELECT * FROM vendas {where} ORDER BY data_venda DESC"
        return pd.read_sql_query(query, self.conn, params=params)

    # --- Cancelamento / Estorno Atômico ---
    def cancelar_venda(self, transacao_id, motivo="", usuario_id=None, cancelar_transacao=True):
        """
        Cancela uma venda (ou todas as linhas de uma transacao_id) de forma atômica sob self._lock.
        Exige motivo obrigatório, repõe estoque, estorna entrada financeira, fiado e fidelidade,
        gravando auditoria completa com antes/depois.
        """
        motivo_limpo = str(motivo or "").strip()
        if not motivo_limpo:
            return False, "O motivo do cancelamento é obrigatório."

        with self._lock:
            uid = usuario_id if usuario_id is not None else getattr(self, "current_user_id", None)
            try:
                cursor = self.conn.cursor()
                rows_to_cancel = []
                ref_str = str(transacao_id).strip()

                # 1. Tentar resolver diretamente por transacao_id
                if cancelar_transacao and ref_str:
                    cursor.execute(
                        """SELECT id, produto_id, quantidade, valor_total, COALESCE(desconto, 0),
                                  status, caixa_id, forma_pagamento, cliente, transacao_id, COALESCE(valor_fiado, 0), loja_id
                           FROM vendas WHERE transacao_id = ?""",
                        (ref_str,)
                    )
                    rows_to_cancel = cursor.fetchall()

                # 2. Fallback: resolver pelo ID numérico da venda e expandir pela transacao_id se aplicável
                if not rows_to_cancel and ref_str.isdigit():
                    cursor.execute(
                        """SELECT id, produto_id, quantidade, valor_total, COALESCE(desconto, 0),
                                  status, caixa_id, forma_pagamento, cliente, transacao_id, COALESCE(valor_fiado, 0), loja_id
                           FROM vendas WHERE id = ?""",
                        (int(ref_str),)
                    )
                    seed_row = cursor.fetchone()
                    if seed_row:
                        tx_found = seed_row[9]
                        if cancelar_transacao and tx_found:
                            cursor.execute(
                                """SELECT id, produto_id, quantidade, valor_total, COALESCE(desconto, 0),
                                          status, caixa_id, forma_pagamento, cliente, transacao_id, COALESCE(valor_fiado, 0), loja_id
                                   FROM vendas WHERE transacao_id = ?""",
                                (tx_found,)
                            )
                            rows_to_cancel = cursor.fetchall()
                        else:
                            rows_to_cancel = [seed_row]

                if not rows_to_cancel:
                    return False, "Venda não encontrada."

                active_rows = [r for r in rows_to_cancel if r[5] != 'Cancelada']
                if not active_rows:
                    return False, "Venda já foi cancelada anteriormente."

                # Bloquear cancelamento se algum caixa vinculado já estiver fechado
                for r in active_rows:
                    r_caixa_id = r[6]
                    if r_caixa_id is not None:
                        cursor.execute("SELECT status FROM caixa WHERE id = ?", (r_caixa_id,))
                        cx_row = cursor.fetchone()
                        if cx_row and cx_row[0] == 'fechado':
                            return False, "Não é permitido cancelar venda de um caixa já fechado."

                data_cancelamento = datetime.datetime.now().isoformat()
                total_liquido_estornado = 0.0
                total_fiado_estornado = 0.0
                cliente_principal = active_rows[0][8]
                forma_principal = active_rows[0][7]
                tx_final = active_rows[0][9] or str(active_rows[0][0])

                antes_info = [
                    {"venda_id": r[0], "produto_id": r[1], "qtd": r[2], "valor_bruto": r[3], "desconto": r[4], "status": r[5]}
                    for r in active_rows
                ]

                cursor.execute("SAVEPOINT sp_cancelar_venda")
                try:
                    for r in active_rows:
                        r_id, r_prod_id, r_qtd, r_val_bruto, r_desc, _r_status, r_caixa_id, r_forma, _r_cli, _tx, r_fiado, r_loja = r

                        # Devolver estoque do produto
                        if r_prod_id:
                            cursor.execute("UPDATE produtos SET quantidade = quantidade + ? WHERE id = ?", (r_qtd, r_prod_id))

                        # Marcar venda como cancelada gravando motivo, operador e timestamp
                        cursor.execute(
                            """UPDATE vendas
                               SET status = 'Cancelada',
                                   motivo_cancelamento = ?,
                                   cancelado_por = ?,
                                   data_cancelamento = ?
                               WHERE id = ?""",
                            (motivo_limpo, uid, data_cancelamento, r_id)
                        )

                        val_liquido = round(float(r_val_bruto or 0.0) - float(r_desc or 0.0), 2)
                        total_liquido_estornado = round(total_liquido_estornado + val_liquido, 2)
                        total_fiado_estornado = round(total_fiado_estornado + float(r_fiado or 0.0), 2)

                        # Estornar entrada financeira pelo valor líquido real cobrado
                        cursor.execute(
                            "INSERT INTO entradas (data, valor, descricao, loja_id) VALUES (?, ?, ?, ?)",
                            (datetime.date.today().isoformat(), -val_liquido, f"Estorno venda #{r_id} | Motivo: {motivo_limpo}", r_loja)
                        )

                        # Registrar cancelamento em auditoria_caixa mantendo vínculo com o caixa
                        if r_caixa_id:
                            cursor.execute(
                                '''INSERT INTO auditoria_caixa (caixa_id, data_hora, tipo_operacao, usuario, venda_id, forma_pagamento, valor, descricao)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                                (r_caixa_id, data_cancelamento, 'CANCELAMENTO', str(uid or 'sistema'),
                                 r_id, r_forma, val_liquido, f"Venda #{r_id} cancelada | Motivo: {motivo_limpo}")
                            )

                    # Se houve parcela em Fiado, cancelar conta a receber correspondente
                    if total_fiado_estornado > 0 or (forma_principal and 'Fiado' in str(forma_principal)):
                        val_busca_fiado = total_fiado_estornado if total_fiado_estornado > 0 else total_liquido_estornado
                        cursor.execute(
                            """SELECT id FROM contas_receber
                               WHERE status = 'Pendente' AND ABS(valor - ?) < 0.05
                               ORDER BY id DESC LIMIT 1""",
                            (val_busca_fiado,)
                        )
                        cr_row = cursor.fetchone()
                        if cr_row:
                            cursor.execute("UPDATE contas_receber SET status = 'Cancelado' WHERE id = ?", (cr_row[0],))

                    # Estornar pontos de fidelidade e compras_recorrentes se cliente identificado
                    if cliente_principal and cliente_principal != "Consumidor Final":
                        cursor.execute(
                            "UPDATE clientes SET compras_recorrentes = MAX(0, compras_recorrentes - 1) WHERE nome = ?",
                            (cliente_principal,)
                        )
                        pontos_estornar = int(total_liquido_estornado // 10)
                        cursor.execute("SELECT id FROM clientes WHERE nome = ?", (cliente_principal,))
                        c_row = cursor.fetchone()
                        if c_row:
                            cursor.execute(
                                """UPDATE fidelidade
                                   SET pontos = MAX(0, pontos - ?),
                                       total_acumulado = MAX(0.0, ROUND(total_acumulado - ?, 2))
                                   WHERE cliente_id = ?""",
                                (pontos_estornar, total_liquido_estornado, c_row[0])
                            )

                    cursor.execute("RELEASE SAVEPOINT sp_cancelar_venda")
                    self.conn.commit()
                except Exception:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_cancelar_venda")
                    cursor.execute("RELEASE SAVEPOINT sp_cancelar_venda")
                    raise

                self.registrar_alteracao(
                    "Cancelar Venda",
                    f"Venda/Tx #{tx_final} cancelada ({len(active_rows)} itens). Motivo: {motivo_limpo}. Estornado: R$ {total_liquido_estornado:.2f}",
                    usuario_id=uid,
                    entidade="vendas",
                    entidade_id=str(tx_final),
                    antes=antes_info,
                    depois={
                        "status": "Cancelada",
                        "motivo": motivo_limpo,
                        "cancelado_por": uid,
                        "valor_estornado": total_liquido_estornado,
                    },
                )

                # Tratamento fiscal gracioso para NFC-e associada (se existir)
                aviso_fiscal = ""
                try:
                    from services.fiscal_service import FiscalService
                    fsrv = getattr(self, "_fiscal_service", None) or FiscalService(self)
                    ok_f, msg_f = fsrv.cancelar_nfce_venda(str(tx_final), motivo_limpo)
                    if not ok_f:
                        aviso_fiscal = f" Atenção fiscal: {msg_f}"
                        self.registrar_alteracao(
                            "Alerta Fiscal Cancelamento",
                            f"Venda #{tx_final} estornada localmente, mas SEFAZ recusou/falhou no cancelamento da NFC-e: {msg_f}",
                            usuario_id=uid,
                            entidade="nfce_documentos",
                            entidade_id=str(tx_final),
                            depois={"status_fiscal": "cancelamento_recusado", "detalhe": msg_f},
                        )
                except Exception as f_exc:
                    logger.warning("Aviso ao tentar cancelar NFC-e de %s: %s", tx_final, f_exc)

                return True, f"Venda #{tx_final} cancelada e estornada com sucesso (R$ {total_liquido_estornado:.2f}).{aviso_fiscal}"
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                logger.error("Erro ao cancelar venda: %s", e)
                return False, str(e)

    # ============================================================
    # ====== PERSISTÊNCIA DE CARRINHO (pdv_rascunho) E F6 =======
    # ============================================================

    @staticmethod
    def _sanitize_cart_items(carrinho):
        """Converte itens do carrinho (incluindo row_data pandas/Series) em estrutura JSON-serializável."""
        clean_items = []
        for item in (carrinho or []):
            rd = item.get("row_data")
            if rd is not None and hasattr(rd, "to_dict"):
                rd_dict = rd.to_dict()
            elif isinstance(rd, dict):
                rd_dict = dict(rd)
            else:
                rd_dict = {}
            # Garantir tipos primitivos seguros em row_data
            safe_rd = {}
            for k, v in rd_dict.items():
                try:
                    json.dumps(v)
                    safe_rd[k] = v
                except TypeError:
                    safe_rd[k] = str(v)
            clean_items.append({
                "id": int(item["id"]) if item.get("id") is not None else None,
                "type": item.get("type", "produto"),
                "nome": str(item.get("nome", "")),
                "preco": float(item.get("preco", 0.0)),
                "preco_original": float(item.get("preco_original", item.get("preco", 0.0))),
                "desconto_item": float(item.get("desconto_item", 0.0)),
                "qtd": int(item["qtd"]) if isinstance(item.get("qtd"), (int, float)) and float(item["qtd"]).is_integer() else float(item.get("qtd", 1)),
                "row_data": safe_rd,
            })
        return clean_items

    def salvar_rascunho_pdv(self, usuario_id, carrinho, cliente="Consumidor Final", desconto="0", tipo_desconto="%", observacao=""):
        """Salva ou limpa o rascunho ativo do carrinho do operador em disco para recuperação pós-crash."""
        with self._lock:
            uid = int(usuario_id or self.current_user_id or 1)
            cursor = self.conn.cursor()
            if not carrinho:
                cursor.execute("DELETE FROM pdv_rascunho WHERE usuario_id = ?", (uid,))
                self.conn.commit()
                return True
            clean_items = self._sanitize_cart_items(carrinho)
            payload = json.dumps(clean_items, ensure_ascii=False)
            agora = datetime.datetime.now().isoformat()
            cursor.execute(
                """INSERT OR REPLACE INTO pdv_rascunho
                   (usuario_id, carrinho_json, cliente, desconto, tipo_desconto, observacao, atualizado_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uid, payload, str(cliente or "Consumidor Final"), str(desconto or "0"), str(tipo_desconto or "%"), str(observacao or ""), agora)
            )
            self.conn.commit()
            return True

    def obter_rascunho_pdv(self, usuario_id=None):
        """Retorna o último rascunho salvo do operador ou None se não houver carrinho pendente."""
        uid = int(usuario_id or self.current_user_id or 1)
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT carrinho_json, cliente, desconto, tipo_desconto, observacao, atualizado_em FROM pdv_rascunho WHERE usuario_id = ?",
            (uid,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        try:
            itens = json.loads(row[0])
        except Exception:
            itens = []
        if not itens:
            return None
        return {
            "usuario_id": uid,
            "itens": itens,
            "cliente": row[1] or "Consumidor Final",
            "desconto": row[2] or "",
            "tipo_desconto": row[3] or "%",
            "observacao": row[4] or "",
            "atualizado_em": row[5] or "",
        }

    def limpar_rascunho_pdv(self, usuario_id=None):
        """Remove o rascunho ativo de carrinho do operador após concluir ou descartar venda."""
        with self._lock:
            uid = int(usuario_id or self.current_user_id or 1)
            self.conn.execute("DELETE FROM pdv_rascunho WHERE usuario_id = ?", (uid,))
            self.conn.commit()

    def suspender_venda(self, itens, subtotal, desconto=0.0, total=0.0, cliente="Consumidor Final",
                        contato_cliente="", vendedor="", observacao="", tipo="suspensa", usuario_id=None, loja_id=None):
        """Salva o carrinho atual em vendas_suspensas (F6 Suspensa ou Orçamento)."""
        if not itens:
            return False, "Carrinho vazio."
        with self._lock:
            uid = usuario_id if usuario_id is not None else self.current_user_id
            clean_items = self._sanitize_cart_items(itens)
            prefixo = "ORC" if tipo == "orcamento" else "SUS"
            codigo = f"{prefixo}-{datetime.datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
            agora = datetime.datetime.now().isoformat()
            cursor = self.conn.cursor()
            cursor.execute(
                """INSERT INTO vendas_suspensas (
                    codigo, tipo, cliente, contato_cliente, vendedor, itens_json,
                    subtotal, desconto, total, observacao, status, usuario_id, loja_id, criado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Ativa', ?, ?, ?)""",
                (
                    codigo, tipo, str(cliente or "Consumidor Final"), str(contato_cliente or ""),
                    str(vendedor or ""), json.dumps(clean_items, ensure_ascii=False),
                    round(float(subtotal or 0.0), 2), round(float(desconto or 0.0), 2), round(float(total or 0.0), 2),
                    str(observacao or ""), uid, loja_id, agora
                )
            )
            susp_id = cursor.lastrowid
            self.conn.commit()
            self.registrar_alteracao(
                "Suspender Venda" if tipo != "orcamento" else "Criar Orçamento",
                f"{codigo} | Cliente: {cliente} | Total: R$ {float(total or 0.0):.2f}",
                usuario_id=uid,
                entidade="vendas_suspensas",
                entidade_id=str(susp_id),
                depois={"codigo": codigo, "tipo": tipo, "total": round(float(total or 0.0), 2), "itens": len(clean_items)},
            )
            return True, {"id": susp_id, "codigo": codigo, "tipo": tipo, "total": round(float(total or 0.0), 2)}

    def listar_vendas_suspensas(self, status="Ativa", tipo=None):
        """Lista vendas suspensas e orçamentos ordenados do mais recente ao mais antigo."""
        clauses = []
        params = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if tipo:
            clauses.append("tipo = ?")
            params.append(tipo)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return pd.read_sql_query(f"SELECT * FROM vendas_suspensas {where} ORDER BY id DESC", self.conn, params=params)

    def obter_venda_suspensa(self, suspensa_id):
        """Retorna os dados completos (com itens desserializados) de uma venda suspensa ou orçamento."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM vendas_suspensas WHERE id = ?", (suspensa_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        data = dict(zip(cols, row))
        try:
            data["itens"] = json.loads(data.get("itens_json") or "[]")
        except Exception:
            data["itens"] = []
        return data

    def marcar_venda_suspensa_retomada(self, suspensa_id, usuario_id=None):
        """Marca uma venda suspensa como Retomada após carregar seus itens de volta ao carrinho."""
        with self._lock:
            self.conn.execute(
                "UPDATE vendas_suspensas SET status = 'Retomada', convertido_em = ? WHERE id = ?",
                (datetime.datetime.now().isoformat(), suspensa_id)
            )
            self.conn.commit()
            return True

    def excluir_venda_suspensa(self, suspensa_id, motivo="", usuario_id=None):
        """Cancela/arquiva uma venda suspensa ou orçamento."""
        with self._lock:
            self.conn.execute(
                "UPDATE vendas_suspensas SET status = 'Cancelada' WHERE id = ?",
                (suspensa_id,)
            )
            self.conn.commit()
            self.registrar_alteracao(
                "Cancelar Venda Suspensa",
                f"Registro #{suspensa_id} cancelado. {motivo}".strip(),
                usuario_id=usuario_id,
                entidade="vendas_suspensas",
                entidade_id=str(suspensa_id),
            )
            return True

    def converter_venda_suspensa(self, suspensa_id, forma_pagamento="Dinheiro", formas_usadas=None, usuario_id=None, loja_id=None):
        """Converte uma venda suspensa ou orçamento diretamente em venda finalizada no PDV."""
        with self._lock:
            dados = self.obter_venda_suspensa(suspensa_id)
            if not dados:
                return False, "Registro não encontrado."
            if dados.get("status") != "Ativa":
                return False, f"Registro #{suspensa_id} não está ativo (status: {dados.get('status')})."

            itens = dados.get("itens") or []
            if not itens:
                return False, "Registro sem itens."

            itens_venda = []
            for it in itens:
                itens_venda.append({
                    "id": it.get("id"),
                    "nome": it.get("nome", ""),
                    "qtd": it.get("qtd", 1),
                    "preco": it.get("preco", 0.0),
                    "preco_compra": (it.get("row_data") or {}).get("preco_compra", 0.0) if it.get("type", "produto") == "produto" else 0.0,
                    "type": it.get("type", "produto"),
                })

            ok, res = self.registrar_venda_detalhada(
                itens_venda,
                float(dados.get("subtotal", 0.0)),
                float(dados.get("desconto", 0.0)),
                dados.get("cliente") or "Consumidor Final",
                forma_pagamento=forma_pagamento,
                vendedor=dados.get("vendedor") or "",
                formas_usadas=formas_usadas,
                usuario_id=usuario_id if usuario_id is not None else dados.get("usuario_id"),
                loja_id=loja_id if loja_id is not None else dados.get("loja_id"),
                registrar_entrada=True,
            )
            if not ok:
                return False, res

            venda_ref = str(res[0]) if isinstance(res, list) and res else str(res)
            self.conn.execute(
                "UPDATE vendas_suspensas SET status = 'Convertida', convertido_em = ?, venda_transacao_id = ? WHERE id = ?",
                (datetime.datetime.now().isoformat(), venda_ref, suspensa_id)
            )
            self.conn.commit()
            return True, res

    # --- Contas a Receber (Fiado) ---
    def registrar_fiado(self, cliente_id, valor, descricao, data_vencimento=None):
        """Registra uma venda fiado (conta a receber)."""
        cursor = self.conn.cursor()
        data_venda = datetime.date.today().isoformat()
        if not data_vencimento:
            data_vencimento = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        cursor.execute(
            "INSERT INTO contas_receber (cliente_id, valor, data_venda, data_vencimento, status, descricao) VALUES (?, ?, ?, ?, 'Pendente', ?)",
            (cliente_id, valor, data_venda, data_vencimento, descricao))
        self.conn.commit()
        self.registrar_alteracao("Venda Fiado", f"Fiado R$ {valor:.2f} p/ cliente ID {cliente_id}. Venc: {data_vencimento}")

    def quitar_fiado(self, conta_id):
        """Marca uma conta fiado como paga."""
        cursor = self.conn.cursor()
        cursor.execute("UPDATE contas_receber SET status = 'Pago', data_pagamento = ? WHERE id = ?",
                       (datetime.date.today().isoformat(), conta_id))
        self.conn.commit()
        self.registrar_alteracao("Quitar Fiado", f"Conta #{conta_id} quitada.")

    def listar_fiados(self, status=None, cliente_id=None):
        """Lista contas a receber com filtros opcionais."""
        query = """SELECT cr.*, c.nome as cliente_nome FROM contas_receber cr
                   LEFT JOIN clientes c ON cr.cliente_id = c.id WHERE 1=1"""
        params = []
        if status:
            query += " AND cr.status = ?"
            params.append(status)
        if cliente_id:
            query += " AND cr.cliente_id = ?"
            params.append(cliente_id)
        query += " ORDER BY cr.data_vencimento ASC"
        return pd.read_sql_query(query, self.conn, params=params)

    def total_fiado_pendente(self):
        """Retorna total de fiados pendentes."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT SUM(valor) FROM contas_receber WHERE status = 'Pendente'")
        return cursor.fetchone()[0] or 0.0

    # --- Fidelidade ---
    def get_pontos_cliente(self, cliente_id):
        """Retorna pontos de fidelidade de um cliente."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT pontos, total_acumulado FROM fidelidade WHERE cliente_id = ?", (cliente_id,))
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (0, 0.0)

    def usar_pontos_fidelidade(self, cliente_id, pontos):
        """Consome pontos de fidelidade para desconto."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT pontos FROM fidelidade WHERE cliente_id = ?", (cliente_id,))
        row = cursor.fetchone()
        if not row or row[0] < pontos:
            return False, "Pontos insuficientes."
        cursor.execute("UPDATE fidelidade SET pontos = pontos - ? WHERE cliente_id = ?", (pontos, cliente_id))
        self.conn.commit()
        self.registrar_alteracao("Usar Pontos", f"Cliente ID {cliente_id} usou {pontos} pontos.")
        return True, pontos

    def listar_fidelidade_clientes(self):
        """Retorna todos os clientes com seus pontos e total acumulado em uma única consulta JOIN (evita N+1)."""
        query = """
            SELECT c.id, c.nome, c.contato, COALESCE(c.compras_recorrentes, 0) as compras_recorrentes,
                   COALESCE(f.pontos, 0) as pontos, COALESCE(f.total_acumulado, 0.0) as total_acumulado
            FROM clientes c
            LEFT JOIN fidelidade f ON c.id = f.cliente_id
            ORDER BY COALESCE(f.pontos, 0) DESC, c.nome ASC
        """
        return pd.read_sql_query(query, self.conn)

    def exportar_backup_externo(self, destino_dir):
        """Exporta um pacote de backup e recuperação externo (para Nuvem/Google Drive/OneDrive/Pendrive)."""
        try:
            with self._lock:
                os.makedirs(destino_dir, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                pkg_dir = os.path.join(destino_dir, f"EstokeAoCubo_Backup_{ts}")
                os.makedirs(pkg_dir, exist_ok=True)

                # 1. Cópia limpa do SQLite via API nativa de backup
                plain_db = os.path.join(pkg_dir, f"estoque_backup_{ts}.db")
                b_conn = sqlite3.connect(plain_db)
                try:
                    self.conn.backup(b_conn)
                finally:
                    b_conn.close()

                # 2. Criptografar backup com a chave do sistema
                enc_db = plain_db + ".enc"
                _encrypt_file(plain_db, enc_db, self._enc_key)
                if os.path.exists(enc_db) and os.path.getsize(enc_db) > 0:
                    os.remove(plain_db)

                # 3. Exportar chave de recuperação (Fernet base64) e config.json para recuperação em outro PC
                key_recovery_file = os.path.join(pkg_dir, "CHAVE_RECUPERACAO.key")
                with open(key_recovery_file, "wb") as fk:
                    fk.write(self._enc_key)

                cfg_path = os.path.join(self.data_dir, "config.json")
                if os.path.exists(cfg_path):
                    shutil.copy2(cfg_path, os.path.join(pkg_dir, "config.json"))

                leia_me = os.path.join(pkg_dir, "LEIA_ME_RECUPERACAO.txt")
                with open(leia_me, "w", encoding="utf-8") as fl:
                    fl.write(
                        "=== KIT DE RECUPERAÇÃO EXTERNA - ESTOKE AO CUBO ===\n"
                        f"Data de geração: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                        "COMO RESTAURAR EM UM NOVO COMPUTADOR:\n"
                        "1. Instale o Estoke Ao Cubo no novo computador.\n"
                        "2. Copie o arquivo CHAVE_RECUPERACAO.key para %LOCALAPPDATA%\\EstokeAoCubo\\.db_key\n"
                        "3. No menu Configurações > Backup, clique em Restaurar e selecione o arquivo .db.enc desta pasta.\n"
                        "Mantenha esta pasta em local seguro (Google Drive, OneDrive ou Pendrive privado).\n"
                    )

                self.registrar_alteracao("Backup Externo", f"Kit exportado para {pkg_dir}")
                return True, pkg_dir
        except Exception as e:
            logger.error("Erro ao exportar backup externo: %s", e)
            return False, str(e)

    def parse_nfe_xml(self, xml_path, margem_padrao=0.35):
        """Faz parsing somente-leitura de NF-e XML (SEFAZ) e sugere vínculos sem gravar nada no banco."""
        import xml.etree.ElementTree as ET
        try:
            if not os.path.exists(xml_path):
                return False, "Arquivo XML não encontrado."

            with open(xml_path, "r", encoding="utf-8", errors="replace") as f:
                xml_conteudo = f.read()

            root = ET.fromstring(xml_conteudo)

            def _strip_ns(tag):
                return tag.split('}', 1)[-1] if '}' in tag else tag

            def _find_child(elem, name):
                if elem is None:
                    return None
                for ch in elem:
                    if _strip_ns(ch.tag) == name:
                        return ch
                return None

            def _find_text(elem, name, default=""):
                ch = _find_child(elem, name)
                return (ch.text or "").strip() if ch is not None and ch.text else default

            inf_nfe = None
            for el in root.iter():
                if _strip_ns(el.tag) == "infNFe":
                    inf_nfe = el
                    break
            if inf_nfe is None:
                return False, "Estrutura XML inválida: tag <infNFe> não encontrada no documento."

            chave_acesso = (inf_nfe.attrib.get("Id") or "").replace("NFe", "").strip()
            ide = _find_child(inf_nfe, "ide")
            num_nf = _find_text(ide, "nNF", "S/N")

            emit = _find_child(inf_nfe, "emit")
            forn_nome = _find_text(emit, "xFant") or _find_text(emit, "xNome") or "Fornecedor NF-e"
            forn_cnpj = _find_text(emit, "CNPJ") or _find_text(emit, "CPF")
            ender_emit = _find_child(emit, "enderEmit")
            forn_fone = _find_text(ender_emit, "fone")
            contato_forn = f"CNPJ: {forn_cnpj}" + (f" | Tel: {forn_fone}" if forn_fone else "")

            itens_raw = []
            total_nf_itens = 0.0
            for ch in inf_nfe:
                if _strip_ns(ch.tag) != "det":
                    continue
                prod = _find_child(ch, "prod")
                if prod is None:
                    continue

                x_prod = _find_text(prod, "xProd")
                c_ean = _find_text(prod, "cEAN")
                if c_ean.upper() in ("SEM GTIN", "0", ""):
                    c_ean = _find_text(prod, "cProd")
                u_com = (_find_text(prod, "uCom", "un") or "un").lower()[:10]
                try:
                    q_com = float(_find_text(prod, "qCom", "0").replace(",", "."))
                except ValueError:
                    q_com = 0.0
                try:
                    v_un = float(_find_text(prod, "vUnCom", "0").replace(",", "."))
                except ValueError:
                    v_un = 0.0

                if not x_prod or q_com <= 0:
                    continue

                qtd_int = max(1, int(round(q_com)))
                subtotal = round(q_com * v_un, 2)
                total_nf_itens += subtotal
                preco_sugerido = round(v_un * (1.0 + float(margem_padrao or 0.35)), 2) if v_un > 0 else 1.0

                itens_raw.append({
                    "index": len(itens_raw),
                    "x_prod": x_prod,
                    "c_ean": c_ean,
                    "u_com": u_com,
                    "q_com": q_com,
                    "qtd_int": qtd_int,
                    "v_un": round(v_un, 4),
                    "subtotal": subtotal,
                    "preco_sugerido": preco_sugerido,
                })

            duplicatas = []
            cobr = _find_child(inf_nfe, "cobr")
            dups = [el for el in cobr if _strip_ns(el.tag) == "dup"] if cobr is not None else []
            hoje_iso = datetime.date.today().isoformat()
            for idx, dup in enumerate(dups, 1):
                n_dup = _find_text(dup, "nDup", str(idx))
                d_venc = _find_text(dup, "dVenc", hoje_iso)
                try:
                    v_dup = float(_find_text(dup, "vDup", "0").replace(",", "."))
                except ValueError:
                    v_dup = 0.0
                if v_dup > 0:
                    duplicatas.append({"n_dup": n_dup, "d_venc": d_venc, "v_dup": round(v_dup, 2)})

            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute("SELECT id FROM fornecedores WHERE nome = ?", (forn_nome,))
                f_row = cursor.fetchone()
                fornecedor_id = int(f_row[0]) if f_row else None

                for item in itens_raw:
                    pid = None
                    pname = ""
                    match_type = "novo"
                    if item["c_ean"]:
                        cursor.execute("SELECT id, nome FROM produtos WHERE codigo_barras = ? LIMIT 1", (item["c_ean"],))
                        row_m = cursor.fetchone()
                        if row_m:
                            pid, pname = int(row_m[0]), str(row_m[1])
                            match_type = "ean"
                    if pid is None:
                        cursor.execute("SELECT id, nome FROM produtos WHERE LOWER(nome) = LOWER(?) LIMIT 1", (item["x_prod"],))
                        row_m = cursor.fetchone()
                        if row_m:
                            pid, pname = int(row_m[0]), str(row_m[1])
                            match_type = "nome"

                    item["matched_produto_id"] = pid
                    item["matched_produto_nome"] = pname
                    item["match_type"] = match_type
                    item["acao"] = "vincular" if pid is not None else "criar"

                cursor.execute("SELECT id, nome, COALESCE(codigo_barras, '') FROM produtos ORDER BY nome ASC")
                catalogo = [{"id": int(r[0]), "nome": str(r[1]), "codigo_barras": str(r[2])} for r in cursor.fetchall()]

            dados = {
                "xml_path": xml_path,
                "arquivo_nome": os.path.basename(xml_path),
                "xml_conteudo": xml_conteudo,
                "numero_nfe": num_nf,
                "chave_acesso": chave_acesso,
                "fornecedor": {
                    "id": fornecedor_id,
                    "nome": forn_nome,
                    "cnpj": forn_cnpj,
                    "fone": forn_fone,
                    "contato": contato_forn,
                },
                "itens": itens_raw,
                "duplicatas": duplicatas,
                "valor_total": round(total_nf_itens, 2),
                "catalogo_produtos": catalogo,
            }
            return True, dados
        except Exception as e:
            logger.error("Erro ao analisar XML NF-e: %s", e)
            return False, f"Falha ao analisar NF-e XML: {e}"

    def aplicar_nfe(self, dados_nfe, mapeamentos=None, margem_padrao=0.35, lancar_conta_pagar=True, usuario_id=None):
        """Aplica NF-e previamente parseada ao banco (fornecedor, estoque com vínculo manual, contas a pagar e histórico XML)."""
        try:
            if not isinstance(dados_nfe, dict):
                return False, "Dados da NF-e inválidos."

            forn = dados_nfe.get("fornecedor") or {}
            forn_nome = forn.get("nome") or "Fornecedor NF-e"
            contato_forn = forn.get("contato") or ""
            num_nf = dados_nfe.get("numero_nfe") or "S/N"
            chave_acesso = dados_nfe.get("chave_acesso") or ""
            itens = dados_nfe.get("itens") or []
            duplicatas = dados_nfe.get("duplicatas") or []
            xml_conteudo = dados_nfe.get("xml_conteudo") or ""
            arquivo_nome = dados_nfe.get("arquivo_nome") or f"nfe_{num_nf}.xml"
            mapeamentos = mapeamentos or {}

            novos = 0
            atualizados = 0
            contas_lancadas = 0
            total_nf_itens = 0.0

            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute("SELECT id FROM fornecedores WHERE nome = ?", (forn_nome,))
                f_row = cursor.fetchone()
                if f_row:
                    fornecedor_id = int(f_row[0])
                else:
                    cursor.execute(
                        "INSERT INTO fornecedores (nome, contato, produto_fornecido) VALUES (?, ?, ?)",
                        (forn_nome, contato_forn, f"NF-e #{num_nf}")
                    )
                    fornecedor_id = int(cursor.lastrowid)

                for item in itens:
                    idx = item.get("index")
                    x_prod = item.get("x_prod", "")
                    c_ean = item.get("c_ean", "")
                    u_com = item.get("u_com", "un")
                    q_com = float(item.get("q_com", 0) or 0)
                    qtd_int = int(item.get("qtd_int") or max(1, int(round(q_com))))
                    v_un = float(item.get("v_un", 0) or 0)
                    total_nf_itens += round(q_com * v_un, 2)

                    target_pid = mapeamentos[idx] if idx in mapeamentos else item.get("matched_produto_id")

                    existente = None
                    if target_pid is not None:
                        cursor.execute(
                            "SELECT id, quantidade, quantidade_inicial, preco, COALESCE(codigo_barras, '') FROM produtos WHERE id = ?",
                            (int(target_pid),),
                        )
                        existente = cursor.fetchone()

                    if existente:
                        pid, qtd_at, qtd_ini, preco_at, ean_atual = existente
                        nova_qtd = int(qtd_at or 0) + qtd_int
                        nova_ini = max(int(qtd_ini or 0), nova_qtd)
                        novo_preco_venda = preco_at if (preco_at and preco_at > v_un) else round(v_un * (1.0 + float(margem_padrao or 0.35)), 2)
                        novo_ean = ean_atual or c_ean
                        cursor.execute(
                            """UPDATE produtos
                               SET quantidade = ?, quantidade_inicial = ?, preco_compra = ?, preco = ?,
                                   fornecedor_id = COALESCE(fornecedor_id, ?), codigo_barras = ?
                               WHERE id = ?""",
                            (nova_qtd, nova_ini, round(v_un, 2), novo_preco_venda, fornecedor_id, novo_ean, pid)
                        )
                        atualizados += 1
                    else:
                        preco_sugerido = round(v_un * (1.0 + float(margem_padrao or 0.35)), 2) if v_un > 0 else 1.0
                        cursor.execute(
                            """INSERT INTO produtos (
                                nome, quantidade, preco, categoria, quantidade_inicial,
                                preco_compra, codigo_barras, unidade_medida, fornecedor_id
                            ) VALUES (?, ?, ?, 'Geral', ?, ?, ?, ?, ?)""",
                            (x_prod, qtd_int, preco_sugerido, qtd_int, round(v_un, 2), c_ean, u_com, fornecedor_id)
                        )
                        novos += 1

                if lancar_conta_pagar and duplicatas:
                    hoje_iso = datetime.date.today().isoformat()
                    total_parc = len(duplicatas)
                    for idx_d, dup in enumerate(duplicatas, 1):
                        n_dup = dup.get("n_dup", str(idx_d))
                        d_venc = dup.get("d_venc", hoje_iso)
                        v_dup = float(dup.get("v_dup", 0) or 0)
                        if v_dup > 0:
                            cursor.execute(
                                """INSERT INTO contas_pagar (
                                    fornecedor_id, descricao, valor, data_emissao, data_vencimento,
                                    categoria, parcela_atual, total_parcelas, observacao
                                ) VALUES (?, ?, ?, ?, ?, 'Material', ?, ?, ?)""",
                                (
                                    fornecedor_id,
                                    f"NF-e #{num_nf} - {forn_nome} (Dup {n_dup})",
                                    round(v_dup, 2),
                                    hoje_iso,
                                    d_venc,
                                    idx_d,
                                    total_parc,
                                    f"Importado de XML NF-e #{num_nf}",
                                ),
                            )
                            contas_lancadas += 1

                if xml_conteudo:
                    cursor.execute(
                        """INSERT INTO nfe_importadas (
                            numero_nfe, chave_acesso, fornecedor, valor_total,
                            xml_conteudo, arquivo_nome, usuario_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(num_nf),
                            str(chave_acesso),
                            str(forn_nome),
                            round(total_nf_itens, 2),
                            xml_conteudo,
                            arquivo_nome,
                            usuario_id or self.current_user_id,
                        ),
                    )

                self.conn.commit()
                resumo = {
                    "numero_nfe": num_nf,
                    "fornecedor": forn_nome,
                    "produtos_criados": novos,
                    "produtos_atualizados": atualizados,
                    "contas_lancadas": contas_lancadas,
                    "valor_total": round(total_nf_itens, 2),
                }
                self.registrar_alteracao(
                    "Importar NF-e XML",
                    f"NF-e #{num_nf} ({forn_nome}): {novos} novos, {atualizados} atualizados",
                    usuario_id=usuario_id,
                    entidade="nfe_importada",
                    entidade_id=str(num_nf),
                    depois=resumo,
                )
                return True, resumo
        except Exception as e:
            logger.error("Erro ao aplicar NF-e XML: %s", e)
            return False, f"Falha ao importar NF-e XML: {e}"

    def importar_nfe_xml(self, xml_path, margem_padrao=0.35, lancar_conta_pagar=True):
        """Importa Nota Fiscal Eletrônica (NF-e XML padrão SEFAZ) diretamente após parsing."""
        ok, dados = self.parse_nfe_xml(xml_path, margem_padrao=margem_padrao)
        if not ok:
            return False, dados
        return self.aplicar_nfe(dados, margem_padrao=margem_padrao, lancar_conta_pagar=lancar_conta_pagar)

    # --- Relatórios Avançados de Vendas ---
    def ranking_produtos_vendidos(self, periodo="todos", limite=10):
        """Ranking dos produtos mais vendidos por quantidade."""
        where = "WHERE v.status != 'Cancelada'"
        params = []
        if periodo != "todos":
            hoje = datetime.date.today()
            inicio = None
            if periodo == "mes":
                inicio = hoje.replace(day=1)
            elif periodo == "semana":
                inicio = hoje - datetime.timedelta(days=hoje.weekday())
            elif periodo == "ano":
                inicio = hoje.replace(month=1, day=1)
            if inicio:
                where += " AND v.data_venda >= ?"
                params.append(inicio.isoformat())
        params.append(limite)
        query = f"""SELECT p.nome, SUM(v.quantidade) as total_vendido, SUM(v.valor_total) as faturamento, SUM(v.lucro) as lucro_total
                    FROM vendas v LEFT JOIN produtos p ON v.produto_id = p.id
                    {where} GROUP BY v.produto_id ORDER BY total_vendido DESC LIMIT ?"""
        return pd.read_sql_query(query, self.conn, params=params)

    def curva_abc(self, periodo="todos"):
        """Análise Curva ABC (Pareto) — classifica produtos em A (80%), B (15%), C (5%) por faturamento."""
        where = "WHERE v.status != 'Cancelada'"
        params = []
        if periodo != "todos":
            hoje = datetime.date.today()
            inicio = None
            if periodo == "mes":
                inicio = hoje.replace(day=1)
            elif periodo == "semana":
                inicio = hoje - datetime.timedelta(days=hoje.weekday())
            elif periodo == "ano":
                inicio = hoje.replace(month=1, day=1)
            if inicio:
                where += " AND v.data_venda >= ?"
                params.append(inicio.isoformat())
        query = f"""SELECT p.nome, SUM(v.quantidade) as total_vendido,
                    SUM(v.valor_total) as faturamento, SUM(v.lucro) as lucro_total
                    FROM vendas v LEFT JOIN produtos p ON v.produto_id = p.id
                    {where} GROUP BY v.produto_id ORDER BY faturamento DESC"""
        df = pd.read_sql_query(query, self.conn, params=params)
        if df.empty:
            return df
        total_fat = df['faturamento'].sum()
        if total_fat > 0:
            df['pct'] = (df['faturamento'] / total_fat * 100).round(2)
            df['pct_acum'] = df['pct'].cumsum().round(2)
            df['classe'] = df['pct_acum'].apply(lambda x: 'A' if x <= 80 else ('B' if x <= 95 else 'C'))
        else:
            df['pct'] = 0
            df['pct_acum'] = 0
            df['classe'] = 'C'
        return df

    def lucro_por_periodo(self, periodo="mes"):
        """Retorna lucro total em um período."""
        where = "WHERE status != 'Cancelada'"
        params = []
        hoje = datetime.date.today()
        inicio = None
        if periodo == "hoje":
            inicio = hoje
        elif periodo == "semana":
            inicio = hoje - datetime.timedelta(days=hoje.weekday())
        elif periodo == "mes":
            inicio = hoje.replace(day=1)
        elif periodo == "ano":
            inicio = hoje.replace(month=1, day=1)
        if inicio:
            where += " AND data_venda >= ?"
            params.append(inicio.isoformat())
        query = f"SELECT SUM(lucro) as lucro, SUM(valor_total) as faturamento FROM vendas {where}"
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return (row[0] or 0.0, row[1] or 0.0)

    def vendas_por_dia(self, dias=30):
        """Retorna vendas agrupadas por dia para gráfico temporal."""
        inicio = (datetime.date.today() - datetime.timedelta(days=dias)).isoformat()
        query = """SELECT DATE(data_venda) as dia, SUM(valor_total) as faturamento, SUM(lucro) as lucro, COUNT(*) as qtd_vendas
                   FROM vendas WHERE status != 'Cancelada' AND data_venda >= ?
                   GROUP BY DATE(data_venda) ORDER BY dia"""
        return pd.read_sql_query(query, self.conn, params=[inicio])

    def comparativo_mensal(self):
        """Compara faturamento/lucro do mês atual vs. mês anterior."""
        hoje = datetime.date.today()
        inicio_mes = hoje.replace(day=1)
        if inicio_mes.month == 1:
            inicio_anterior = inicio_mes.replace(year=inicio_mes.year - 1, month=12)
        else:
            inicio_anterior = inicio_mes.replace(month=inicio_mes.month - 1)
        
        cursor = self.conn.cursor()
        # Mês atual
        cursor.execute("SELECT SUM(valor_total), SUM(lucro), COUNT(*) FROM vendas WHERE status != 'Cancelada' AND data_venda >= ?",
                       (inicio_mes.isoformat(),))
        atual = cursor.fetchone()
        # Mês anterior
        cursor.execute("SELECT SUM(valor_total), SUM(lucro), COUNT(*) FROM vendas WHERE status != 'Cancelada' AND data_venda >= ? AND data_venda < ?",
                       (inicio_anterior.isoformat(), inicio_mes.isoformat()))
        anterior = cursor.fetchone()
        return {
            'atual': {'faturamento': atual[0] or 0, 'lucro': atual[1] or 0, 'vendas': atual[2] or 0},
            'anterior': {'faturamento': anterior[0] or 0, 'lucro': anterior[1] or 0, 'vendas': anterior[2] or 0}
        }

    def vendas_por_cliente(self, cliente_nome):
        """Histórico de compras de um cliente específico."""
        query = """SELECT v.*, p.nome as produto_nome FROM vendas v
                   LEFT JOIN produtos p ON v.produto_id = p.id
                   WHERE v.cliente = ? AND v.status != 'Cancelada'
                   ORDER BY v.data_venda DESC"""
        return pd.read_sql_query(query, self.conn, params=[cliente_nome])

    def verificar_produtos_vencendo(self, dias_alerta=7):
        """Retorna produtos com validade próxima ou vencidos."""
        hoje = datetime.date.today()
        limite = (hoje + datetime.timedelta(days=dias_alerta)).isoformat()
        query = """SELECT id, nome, quantidade, data_validade FROM produtos
                   WHERE data_validade IS NOT NULL AND data_validade != '' AND data_validade <= ?
                   ORDER BY data_validade ASC"""
        return pd.read_sql_query(query, self.conn, params=[limite])

    # Mapeamento inteligente de colunas para importação
    _COLUMN_ALIASES = {
        'nome': ['nome', 'item', 'produto', 'name', 'descricao', 'descrição', 'desc', 'mercadoria'],
        'quantidade': ['quantidade', 'qtd', 'qtde', 'quant', 'qty', 'estoque', 'unidades', 'un'],
        'preco': ['preco', 'preço', 'preco (r$)', 'preço (r$)', 'preco_venda', 'preço_venda',
                  'preco venda', 'preço venda', 'valor', 'valor_venda', 'valor venda',
                  'price', 'vl_venda', 'vl venda', 'p.venda', 'p. venda', 'unitario', 'unitário',
                  'valor unitario', 'valor unitário', 'preco unit', 'preço unit'],
        'categoria': ['categoria', 'cat', 'category', 'tipo', 'grupo', 'departamento', 'setor', 'classe'],
        'subcategoria': ['subcategoria', 'sub_categoria', 'sub categoria', 'subcategory', 'subgrupo', 'sub grupo', 'sub'],
        'sub_subcategoria': ['sub_subcategoria', 'subsubcategoria', 'sub subcategoria', 'sub-subcategoria',
                             'sub_sub_categoria', 'sub sub categoria', 'subsubcat', 'sub_subcat'],
        'tamanho': ['tamanho', 'tam', 'size', 'medida', 'numeracao', 'numeração', 'num'],
        'preco_compra': ['preco_compra', 'preço_compra', 'preco compra', 'preço compra',
                         'custo', 'valor_compra', 'valor compra', 'cost', 'p.compra',
                         'vl_compra', 'vl compra', 'preco custo', 'preço custo'],
        'codigo_barras': ['codigo_barras', 'código_barras', 'codigo barras', 'código barras',
                          'barcode', 'cod_barras', 'cod barras', 'ean', 'sku', 'codigo', 'código', 'cod'],
        'data_validade': ['data_validade', 'validade', 'vencimento', 'data_vencimento',
                          'data vencimento', 'data validade', 'expiry', 'expiration'],
        'imagem': ['imagem', 'image', 'img', 'foto', 'photo', 'picture', 'imagem_path',
                   'image_path', 'foto_path', 'arquivo_imagem'],
        'lote_serie': ['lote_serie', 'lote', 'serie', 'série', 'lote/serie', 'lote/série',
                       'num_serie', 'número_serie', 'numero_serie', 'imei', 'sif', 'batch',
                       'serial', 'serial_number', 'lot', 'ref', 'referencia', 'referência'],
    }

    def _normalizar_colunas(self, df):
        """Renomeia colunas do DataFrame com base em aliases conhecidos.
        Retorna o DataFrame com colunas renomeadas e a lista de mapeamentos aplicados."""
        rename_map = {}
        colunas_lower = {c: c.strip().lower() for c in df.columns}

        for campo_padrao, aliases in self._COLUMN_ALIASES.items():
            # Se já existe a coluna padrão, não precisa mapear
            if campo_padrao in colunas_lower.values():
                # Encontrar a coluna original e mapear para o nome padrão
                for col_orig, col_low in colunas_lower.items():
                    if col_low == campo_padrao and col_orig != campo_padrao:
                        rename_map[col_orig] = campo_padrao
                continue
            # Procurar nas aliases
            for col_orig, col_low in colunas_lower.items():
                if col_low in aliases:
                    rename_map[col_orig] = campo_padrao
                    break

        if rename_map:
            df = df.rename(columns=rename_map)
        return df, rename_map

    def _processar_imagem_importacao(self, img_valor, nome_produto, arquivo_origem):
        """Processa imagem durante importação: copia arquivo local para product_images."""
        try:
            img_dir = os.path.join(self.data_dir, 'product_images')
            os.makedirs(img_dir, exist_ok=True)

            img_path = img_valor.strip()
            # Resolver caminho relativo ao arquivo de origem
            if not os.path.isabs(img_path):
                base_dir = os.path.dirname(os.path.abspath(arquivo_origem))
                img_path = os.path.join(base_dir, img_path)

            if os.path.isfile(img_path):
                ext = os.path.splitext(img_path)[1].lower()
                if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
                    return ''
                safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in nome_produto)
                dest_name = f"{safe_name}_{int(datetime.datetime.now().timestamp())}{ext}"
                dest_path = os.path.join(img_dir, dest_name)
                import shutil
                shutil.copy2(img_path, dest_path)
                return dest_name
            return ''
        except Exception:
            return ''

    def preparar_e_validar_importacao(self, filepath):
        """Lê e valida o arquivo de importação (CSV ou PDF). Retorna (sucesso, dados_validacao, df)."""
        import os
        import pandas as pd
        import datetime
        
        try:
            if filepath.lower().endswith('.pdf'):
                try:
                    import pdfplumber
                except ImportError:
                    return False, {"mensagem": "Biblioteca 'pdfplumber' não instalada. Instale com: pip install pdfplumber"}, None
                
                all_rows = []
                header = None
                with pdfplumber.open(filepath) as pdf:
                    for page in pdf.pages:
                        tables = page.extract_tables()
                        for table in tables:
                            if not table:
                                continue
                            if header is None:
                                header = [str(c).strip() if c else f'col_{i}' for i, c in enumerate(table[0])]
                                data_rows = table[1:]
                            else:
                                data_rows = table
                            for row in data_rows:
                                all_rows.append(row)
                if not all_rows or header is None:
                    return False, {"mensagem": "Nenhuma tabela encontrada no PDF."}, None
                df = pd.DataFrame(all_rows, columns=header)
                df.columns = [c.strip() for c in df.columns]
            else:
                df = pd.read_csv(filepath, comment='#')
            
            df, mapeamentos = self._normalizar_colunas(df)
            
            cols_obrigatorias = {'nome', 'quantidade', 'preco'}
            if not cols_obrigatorias.issubset(set(df.columns)):
                faltando = list(cols_obrigatorias - set(df.columns))
                return False, {
                    "mensagem": f"Colunas obrigatórias não encontradas: {', '.join(faltando)}",
                    "colunas_faltando": faltando,
                    "colunas_encontradas": list(df.columns)
                }, None
            
            erros = []
            linhas_com_erro = set()
            
            for idx, row in df.iterrows():
                linha_num = idx + 2  # 1-based index + header row
                
                # Nome do produto
                nome = str(row.get('nome', '')).strip()
                if not nome or nome.lower() == 'nan':
                    erros.append({"linha": linha_num, "produto": "Desconhecido", "erro": "Nome do produto está em branco ou nulo."})
                    linhas_com_erro.add(idx)
                    continue
                
                # Quantidade
                qtd_str = str(row.get('quantidade', '')).strip()
                if not qtd_str or qtd_str.lower() == 'nan':
                    erros.append({"linha": linha_num, "produto": nome, "erro": "Quantidade está em branco."})
                    linhas_com_erro.add(idx)
                else:
                    try:
                        qtd_val = float(qtd_str.replace(',', '.'))
                        if not qtd_val.is_integer():
                            erros.append({"linha": linha_num, "produto": nome, "erro": f"Quantidade '{qtd_str}' deve ser um número inteiro."})
                            linhas_com_erro.add(idx)
                        elif qtd_val < 0:
                            erros.append({"linha": linha_num, "produto": nome, "erro": f"Quantidade '{qtd_str}' não pode ser negativa."})
                            linhas_com_erro.add(idx)
                    except ValueError:
                        erros.append({"linha": linha_num, "produto": nome, "erro": f"Quantidade '{qtd_str}' é inválida."})
                        linhas_com_erro.add(idx)
                
                # Preço
                preco_str = str(row.get('preco', '')).strip()
                if not preco_str or preco_str.lower() == 'nan':
                    erros.append({"linha": linha_num, "produto": nome, "erro": "Preço de venda está em branco."})
                    linhas_com_erro.add(idx)
                else:
                    try:
                        preco_val = float(preco_str.replace(',', '.'))
                        if preco_val < 0:
                            erros.append({"linha": linha_num, "produto": nome, "erro": f"Preço '{preco_str}' não pode ser negativo."})
                            linhas_com_erro.add(idx)
                    except ValueError:
                        erros.append({"linha": linha_num, "produto": nome, "erro": f"Preço '{preco_str}' é inválido."})
                        linhas_com_erro.add(idx)
                
                # Preço compra (se informado)
                if 'preco_compra' in df.columns:
                    pc_str = str(row.get('preco_compra', '')).strip()
                    if pc_str and pc_str.lower() != 'nan':
                        try:
                            pc_val = float(pc_str.replace(',', '.'))
                            if pc_val < 0:
                                erros.append({"linha": linha_num, "produto": nome, "erro": f"Preço de compra '{pc_str}' não pode ser negativo."})
                                linhas_com_erro.add(idx)
                        except ValueError:
                            erros.append({"linha": linha_num, "produto": nome, "erro": f"Preço de compra '{pc_str}' é inválido."})
                            linhas_com_erro.add(idx)
                
                # Validade (se informada)
                if 'data_validade' in df.columns:
                    validade_str = str(row.get('data_validade', '')).strip()
                    if validade_str and validade_str.lower() != 'nan':
                        data_valida = False
                        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d'):
                            try:
                                datetime.datetime.strptime(validade_str, fmt)
                                data_valida = True
                                break
                            except ValueError:
                                continue
                        if not data_valida:
                            erros.append({"linha": linha_num, "produto": nome, "erro": f"Data de validade '{validade_str}' com formato inválido. Use YYYY-MM-DD ou DD/MM/YYYY."})
                            linhas_com_erro.add(idx)
                            
            dados_validacao = {
                "erros": erros,
                "total_linhas": len(df),
                "linhas_com_erro": list(linhas_com_erro),
                "total_validos": len(df) - len(linhas_com_erro),
                "mapeamentos": mapeamentos
            }
            return True, dados_validacao, df
            
        except Exception as e:
            logger.error("Erro na validação de importação: %s", e)
            return False, {"mensagem": f"Erro de leitura: {str(e)}"}, None

    def importar_dataframe_produtos(self, df, linhas_com_erro, filepath):
        """Importa produtos de um DataFrame já validado, ignorando linhas com erro se necessário."""
        try:
            cursor = self.conn.cursor()
            count = 0
            cursor.execute("BEGIN TRANSACTION")
            try:
                for idx, row in df.iterrows():
                    if idx in linhas_com_erro:
                        continue
                        
                    nome = str(row['nome']).strip()
                    if not nome or nome.lower() == 'nan':
                        continue
                    
                    qtd = int(float(str(row['quantidade']).replace(',', '.').strip()))
                    preco = float(str(row['preco']).replace(',', '.').strip())
                    
                    cat = str(row.get('categoria', '')).strip() if 'categoria' in df.columns else ''
                    if cat.lower() == 'nan': cat = ''
                    subcat = str(row.get('subcategoria', '')).strip() if 'subcategoria' in df.columns else ''
                    if subcat.lower() == 'nan': subcat = ''
                    sub_subcat = str(row.get('sub_subcategoria', '')).strip() if 'sub_subcategoria' in df.columns else ''
                    if sub_subcat.lower() == 'nan': sub_subcat = ''
                    tamanho = str(row.get('tamanho', '')).strip() if 'tamanho' in df.columns else ''
                    if tamanho.lower() == 'nan': tamanho = ''
                    
                    preco_compra = 0.0
                    if 'preco_compra' in df.columns:
                        try:
                            preco_compra = float(str(row.get('preco_compra', 0)).replace(',', '.').strip())
                        except (ValueError, TypeError):
                            preco_compra = 0.0
                            
                    codigo = str(row.get('codigo_barras', '')).strip() if 'codigo_barras' in df.columns else ''
                    if codigo.lower() == 'nan': codigo = ''
                    validade = str(row.get('data_validade', '')).strip() if 'data_validade' in df.columns else ''
                    if validade.lower() == 'nan': validade = ''
                    lote = str(row.get('lote_serie', '')).strip() if 'lote_serie' in df.columns else ''
                    if lote.lower() == 'nan': lote = ''
                    
                    # Processar imagem se disponível
                    img_name = ''
                    if 'imagem' in df.columns:
                        img_val = str(row.get('imagem', '')).strip()
                        if img_val and img_val.lower() != 'nan':
                            img_name = self._processar_imagem_importacao(img_val, nome, filepath)

                    cursor.execute('''INSERT INTO produtos (nome, quantidade, preco, categoria, quantidade_inicial, preco_compra, codigo_barras, data_validade, imagem, subcategoria, tamanho, sub_subcategoria, lote_serie)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (nome, qtd, preco, cat, qtd, preco_compra, codigo, validade, img_name, subcat, tamanho, sub_subcat, lote))
                    count += 1
                
                self.conn.commit()
            except Exception as e:
                self.conn.rollback()
                raise e
                
            info_map = ''
            self.registrar_alteracao("Importar Arquivo", f"{count} produtos importados de {os.path.basename(filepath)}")
            return True, f"{count} produtos importados com sucesso."
        except Exception as e:
            logger.error("Erro ao importar DataFrame: %s", e)
            return False, str(e)

    def importar_produtos_csv(self, filepath):
        """Importa produtos de um arquivo CSV. Mantido para compatibilidade reversa."""
        success, val_data, df = self.preparar_e_validar_importacao(filepath)
        if not success:
            return False, val_data.get("mensagem", "Erro desconhecido.")
        return self.importar_dataframe_produtos(df, val_data["linhas_com_erro"], filepath)

    def importar_produtos_pdf(self, filepath):
        """Importa produtos de um arquivo PDF. Mantido para compatibilidade reversa."""
        success, val_data, df = self.preparar_e_validar_importacao(filepath)
        if not success:
            return False, val_data.get("mensagem", "Erro desconhecido.")
        return self.importar_dataframe_produtos(df, val_data["linhas_com_erro"], filepath)

    def importar_imagem_avulsa(self, src_path, nome_produto):
        """Copia uma imagem para a pasta do sistema e retorna o nome do arquivo final."""
        try:
            img_dir = os.path.join(self.data_dir, 'product_images')
            os.makedirs(img_dir, exist_ok=True)
            
            ext = os.path.splitext(src_path)[1].lower()
            if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
                return None
                
            safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in nome_produto)
            dest_name = f"{safe_name}_{int(datetime.datetime.now().timestamp())}{ext}"
            dest_path = os.path.join(img_dir, dest_name)
            
            shutil.copy2(src_path, dest_path)
            return dest_name
        except Exception as e:
            logger.error("Erro ao importar imagem avulsa: %s", e)
            return None


    def vendas_por_forma_pagamento(self, periodo="mes"):
        """Agrupamento de vendas por forma de pagamento."""
        where = "WHERE status != 'Cancelada'"
        params = []
        hoje = datetime.date.today()
        if periodo == "mes":
            where += " AND data_venda >= ?"
            params.append(hoje.replace(day=1).isoformat())
        elif periodo == "semana":
            where += " AND data_venda >= ?"
            params.append((hoje - datetime.timedelta(days=hoje.weekday())).isoformat())
        query = f"""SELECT COALESCE(forma_pagamento, 'Dinheiro') as forma, SUM(valor_total) as total, COUNT(*) as qtd
                    FROM vendas {where} GROUP BY forma_pagamento ORDER BY total DESC"""
        return pd.read_sql_query(query, self.conn, params=params)

    # ============================================================
    # ============ MULTI-USUÁRIO / PERMISSÕES ============
    # ============================================================

    PERFIS_PERMISSOES = {
        "admin":    {"tudo": True},
        "gerente":  {"ver_relatorio": True, "ver_config": True, "vender": True, "editar": True,
                     "remover": True, "adicionar": True, "gastos": True, "caixa": True,
                     "kits": True, "clientes": True, "fornecedores": True, "vendedores": True,
                     "categorias": True, "insumos": True, "producao": True, "mesas": True,
                     "contas_pagar": True, "etiquetas": True, "chat_ia": True},
        "vendedor": {"vender": True, "adicionar": True, "clientes": True, "kits": True,
                     "chat_ia": True, "etiquetas": True},
        "caixa":    {"vender": True, "caixa": True, "clientes": True},
    }

    def usuario_tem_permissao(self, perfil, acao):
        """Verifica se o perfil tem permissão para a ação."""
        perms = self.PERFIS_PERMISSOES.get(perfil, {})
        return perms.get("tudo", False) or perms.get(acao, False)

    def adicionar_usuario(self, nome, login, senha, perfil='vendedor', loja_id=None):
        from services.security import hash_password, validate_password_strength
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM usuarios WHERE login=?", (login,))
        if cursor.fetchone()[0] > 0:
            return False, "Login já existe."
        
        is_strong, msg = validate_password_strength(senha)
        if not is_strong:
            return False, msg
            
        senha_hash = hash_password(senha)
        cursor.execute("""INSERT INTO usuarios (nome, login, senha_hash, perfil, loja_id, data_criacao)
                         VALUES (?, ?, ?, ?, ?, ?)""",
                      (nome, login, senha_hash, perfil, loja_id, datetime.datetime.now().isoformat()))
        self.conn.commit()
        self.registrar_alteracao("Criar Usuário", f"Usuário '{login}' criado com perfil '{perfil}'")
        return True, "Usuário criado com sucesso."

    def autenticar_usuario(self, login, senha):
        from services.security import verify_password, needs_rehash, hash_password
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id, nome, perfil, loja_id, senha_hash, COALESCE(trocar_senha, 0) FROM usuarios WHERE login=? AND ativo=1",
            (login,)
        )
        row = cursor.fetchone()
        if row and verify_password(senha, row[4]):
            # Migrar hash legado para PBKDF2 automaticamente
            if needs_rehash(row[4]):
                new_hash = hash_password(senha)
                cursor.execute("UPDATE usuarios SET senha_hash=? WHERE id=?", (new_hash, row[0]))
                self.conn.commit()
            return {
                "id": row[0],
                "nome": row[1],
                "perfil": row[2],
                "loja_id": row[3],
                "trocar_senha": bool(row[5]),
            }
        return None

    def listar_usuarios(self):
        return pd.read_sql_query("SELECT id, nome, login, perfil, ativo, loja_id, data_criacao FROM usuarios ORDER BY nome", self.conn)

    def atualizar_usuario(self, user_id, nome, perfil, ativo, loja_id=None):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE usuarios SET nome=?, perfil=?, ativo=?, loja_id=? WHERE id=?",
                      (nome, perfil, ativo, loja_id, user_id))
        self.conn.commit()

    def alterar_senha_usuario(self, user_id, nova_senha):
        from services.security import hash_password
        cursor = self.conn.cursor()
        senha_hash = hash_password(nova_senha)
        cursor.execute("UPDATE usuarios SET senha_hash=?, trocar_senha=0 WHERE id=?", (senha_hash, user_id))
        self.conn.commit()

    def remover_usuario(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE usuarios SET ativo=0 WHERE id=?", (user_id,))
        self.conn.commit()

    # ============================================================
    # ============ CONTAS A PAGAR ============
    # ============================================================

    def adicionar_conta_pagar(self, descricao, valor, data_vencimento, fornecedor_id=None,
                              categoria='Outro', recorrente=0, total_parcelas=1, observacao=''):
        cursor = self.conn.cursor()
        data_emissao = datetime.date.today().isoformat()
        for parcela in range(1, total_parcelas + 1):
            venc = datetime.date.fromisoformat(data_vencimento)
            if total_parcelas > 1:
                venc = venc + datetime.timedelta(days=30 * (parcela - 1))
            cursor.execute("""INSERT INTO contas_pagar
                (fornecedor_id, descricao, valor, data_emissao, data_vencimento, categoria,
                 recorrente, parcela_atual, total_parcelas, observacao)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fornecedor_id, descricao, valor / total_parcelas, data_emissao,
                 venc.isoformat(), categoria, recorrente, parcela, total_parcelas, observacao))
        self.conn.commit()
        self.registrar_alteracao("Conta a Pagar", f"'{descricao}' R${valor:.2f} ({total_parcelas}x)")

    def listar_contas_pagar(self, status=None, dias_futuros=None):
        where = []
        params = []
        if status:
            where.append("cp.status = ?")
            params.append(status)
        if dias_futuros is not None:
            limite = (datetime.date.today() + datetime.timedelta(days=dias_futuros)).isoformat()
            where.append("cp.data_vencimento <= ?")
            params.append(limite)
        where_str = "WHERE " + " AND ".join(where) if where else ""
        query = f"""SELECT cp.*, COALESCE(f.nome, 'Sem fornecedor') as fornecedor_nome
                    FROM contas_pagar cp LEFT JOIN fornecedores f ON cp.fornecedor_id = f.id
                    {where_str} ORDER BY cp.data_vencimento ASC"""
        return pd.read_sql_query(query, self.conn, params=params)

    def pagar_conta(self, conta_id):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE contas_pagar SET status='Pago', data_pagamento=? WHERE id=?",
                      (datetime.date.today().isoformat(), conta_id))
        # Registrar como gasto
        cursor.execute("SELECT descricao, valor, categoria FROM contas_pagar WHERE id=?", (conta_id,))
        row = cursor.fetchone()
        if row:
            self.adicionar_gasto(datetime.date.today().isoformat(), row[1], row[2], f"Pgto: {row[0]}")
        self.conn.commit()
        self.registrar_alteracao("Pagar Conta", f"Conta #{conta_id} quitada")

    def cancelar_conta_pagar(self, conta_id):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE contas_pagar SET status='Cancelado' WHERE id=?", (conta_id,))
        self.conn.commit()

    def contas_vencendo(self, dias=7):
        """Retorna contas a pagar vencendo nos próximos N dias."""
        hoje = datetime.date.today().isoformat()
        limite = (datetime.date.today() + datetime.timedelta(days=dias)).isoformat()
        return pd.read_sql_query(
            "SELECT * FROM contas_pagar WHERE status='Pendente' AND data_vencimento BETWEEN ? AND ? ORDER BY data_vencimento",
            self.conn, params=[hoje, limite])

    def contas_vencidas(self):
        hoje = datetime.date.today().isoformat()
        return pd.read_sql_query(
            "SELECT * FROM contas_pagar WHERE status='Pendente' AND data_vencimento < ? ORDER BY data_vencimento",
            self.conn, params=[hoje])

    def total_contas_pagar_pendente(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM contas_pagar WHERE status='Pendente'")
        return cursor.fetchone()[0]

    # ============================================================
    # ============ MÚLTIPLAS LOJAS ============
    # ============================================================

    def adicionar_loja(self, nome, endereco='', telefone='', cnpj=''):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO lojas (nome, endereco, telefone, cnpj, data_criacao) VALUES (?, ?, ?, ?, ?)",
                      (nome, endereco, telefone, cnpj, datetime.datetime.now().isoformat()))
        self.conn.commit()
        self.registrar_alteracao("Criar Loja", f"Loja '{nome}' criada")
        return cursor.lastrowid

    def listar_lojas(self):
        return pd.read_sql_query("SELECT * FROM lojas WHERE ativa=1 ORDER BY nome", self.conn)

    def remover_loja(self, loja_id):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE lojas SET ativa=0 WHERE id=?", (loja_id,))
        self.conn.commit()

    def atualizar_loja(self, loja_id, nome, endereco='', telefone='', cnpj=''):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE lojas SET nome=?, endereco=?, telefone=?, cnpj=? WHERE id=?",
                      (nome, endereco, telefone, cnpj, loja_id))
        self.conn.commit()

    # ============================================================
    # ============ DRE SIMPLIFICADO ============
    # ============================================================

    def gerar_dre(self, periodo="mes", loja_id=None):
        """Gera Demonstrativo de Resultado do Exercício simplificado (incluindo vendas do dia atual com timestamp)."""
        hoje = datetime.date.today()
        if periodo == "mes":
            inicio = hoje.replace(day=1).isoformat()
            fim = hoje.isoformat()
        elif periodo == "ano":
            inicio = hoje.replace(month=1, day=1).isoformat()
            fim = hoje.isoformat()
        elif periodo == "trimestre":
            mes_inicio = ((hoje.month - 1) // 3) * 3 + 1
            inicio = hoje.replace(month=mes_inicio, day=1).isoformat()
            fim = hoje.isoformat()
        else:
            inicio = hoje.replace(day=1).isoformat()
            fim = hoje.isoformat()

        fim_ts = fim + "T23:59:59.999999"
        cursor = self.conn.cursor()

        loja_clause = " AND (loja_id = ? OR loja_id IS NULL)" if loja_id is not None else ""
        params_ts = (inicio, fim_ts, loja_id) if loja_id is not None else (inicio, fim_ts)
        params_dt = (inicio, fim, loja_id) if loja_id is not None else (inicio, fim)

        # Receita Bruta (vendas + serviços)
        cursor.execute(
            f"SELECT COALESCE(SUM(valor_total), 0) FROM vendas WHERE status='Concluída' AND data_venda >= ? AND data_venda <= ?{loja_clause}",
            params_ts
        )
        receita_vendas = round(float(cursor.fetchone()[0] or 0.0), 2)

        cursor.execute(
            f"SELECT COALESCE(SUM(valor), 0) FROM servicos WHERE data >= ? AND data <= ?{loja_clause}",
            params_ts
        )
        receita_servicos = round(float(cursor.fetchone()[0] or 0.0), 2)

        receita_bruta = round(receita_vendas + receita_servicos, 2)

        # Deduções (descontos dados)
        cursor.execute(
            f"SELECT COALESCE(SUM(desconto), 0) FROM vendas WHERE status='Concluída' AND data_venda >= ? AND data_venda <= ?{loja_clause}",
            params_ts
        )
        deducoes = round(float(cursor.fetchone()[0] or 0.0), 2)

        receita_liquida = round(receita_bruta - deducoes, 2)

        # CMV (custo das mercadorias vendidas = valor_liquido - lucro; custo dos serviços)
        cursor.execute(
            f"SELECT COALESCE(SUM((valor_total - COALESCE(desconto, 0)) - lucro), 0) FROM vendas WHERE status='Concluída' AND data_venda >= ? AND data_venda <= ?{loja_clause}",
            params_ts
        )
        cmv = round(float(cursor.fetchone()[0] or 0.0), 2)

        cursor.execute(
            f"SELECT COALESCE(SUM(custo), 0) FROM servicos WHERE data >= ? AND data <= ?{loja_clause}",
            params_ts
        )
        custo_servicos = round(float(cursor.fetchone()[0] or 0.0), 2)

        custo_total = round(cmv + custo_servicos, 2)
        lucro_bruto = round(receita_liquida - custo_total, 2)

        # Despesas Operacionais (gastos fixos + variáveis)
        cursor.execute(
            f"SELECT COALESCE(SUM(valor), 0) FROM gastos WHERE data >= ? AND data <= ? AND tipo='Fixo'{loja_clause}",
            params_dt
        )
        despesas_fixas = round(float(cursor.fetchone()[0] or 0.0), 2)

        cursor.execute(
            f"SELECT COALESCE(SUM(valor), 0) FROM gastos WHERE data >= ? AND data <= ? AND tipo != 'Fixo'{loja_clause}",
            params_dt
        )
        despesas_variaveis = round(float(cursor.fetchone()[0] or 0.0), 2)

        despesas_total = round(despesas_fixas + despesas_variaveis, 2)
        resultado_operacional = round(lucro_bruto - despesas_total, 2)

        # Contas a pagar pagas no período
        cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM contas_pagar WHERE status='Pago' AND data_pagamento >= ? AND data_pagamento <= ?",
                      (inicio, fim))
        contas_pagas = round(float(cursor.fetchone()[0] or 0.0), 2)

        resultado_liquido = resultado_operacional

        return {
            "periodo": periodo,
            "inicio": inicio,
            "fim": fim,
            "receita_vendas": receita_vendas,
            "receita_servicos": receita_servicos,
            "receita_bruta": receita_bruta,
            "deducoes": deducoes,
            "receita_liquida": receita_liquida,
            "cmv": cmv,
            "custo_servicos": custo_servicos,
            "custo_total": custo_total,
            "lucro_bruto": lucro_bruto,
            "despesas_fixas": despesas_fixas,
            "despesas_variaveis": despesas_variaveis,
            "despesas_total": despesas_total,
            "resultado_operacional": resultado_operacional,
            "contas_pagas": contas_pagas,
            "resultado_liquido": resultado_liquido,
            "margem_bruta": round((lucro_bruto / receita_liquida * 100), 2) if receita_liquida > 0 else 0,
            "margem_liquida": round((resultado_liquido / receita_liquida * 100), 2) if receita_liquida > 0 else 0,
        }

    # ============================================================
    # ============ FLUXO DE CAIXA FUTURO ============
    # ============================================================

    def projetar_fluxo_caixa(self, dias=90):
        """Projeta fluxo de caixa futuro baseado em recorrências e IA."""
        hoje = datetime.date.today()
        projecao = []

        # Saldo atual
        saldo_atual = self.get_saldo_atual()

        # 1. Contas a pagar pendentes (saídas programadas)
        df_contas = self.listar_contas_pagar(status='Pendente', dias_futuros=dias)
        if not df_contas.empty:
            for _, row in df_contas.iterrows():
                projecao.append({
                    "data": row['data_vencimento'],
                    "tipo": "saida",
                    "descricao": f"Conta: {row['descricao']}",
                    "valor": -row['valor'],
                })

        # 2. Gastos fixos recorrentes (projetar meses futuros)
        cursor = self.conn.cursor()
        cursor.execute("SELECT descricao, valor FROM gastos WHERE tipo='Fixo' GROUP BY descricao ORDER BY data DESC")
        gastos_fixos = cursor.fetchall()
        for desc, valor in gastos_fixos:
            for mes_offset in range(1, (dias // 30) + 1):
                data_proj = (hoje + datetime.timedelta(days=30 * mes_offset)).isoformat()
                if data_proj <= (hoje + datetime.timedelta(days=dias)).isoformat():
                    projecao.append({
                        "data": data_proj,
                        "tipo": "saida_fixa",
                        "descricao": f"Fixo: {desc}",
                        "valor": -valor,
                    })

        # 3. Fiados a receber
        df_fiados = self.listar_fiados(status='Pendente')
        if not df_fiados.empty:
            for _, row in df_fiados.iterrows():
                venc = row.get('data_vencimento', '')
                if venc and venc <= (hoje + datetime.timedelta(days=dias)).isoformat():
                    projecao.append({
                        "data": venc,
                        "tipo": "entrada",
                        "descricao": f"Fiado: {row.get('descricao', 'Recebível')}",
                        "valor": row['valor'],
                    })

        # 4. Média diária de vendas (projeção)
        cursor.execute("""SELECT AVG(total_dia) FROM (
            SELECT SUM(valor_total) as total_dia FROM vendas 
            WHERE status='Concluída' AND data_venda >= ? GROUP BY DATE(data_venda))""",
            ((hoje - datetime.timedelta(days=30)).isoformat(),))
        media_diaria = cursor.fetchone()[0] or 0
        if media_diaria > 0:
            for d in range(1, dias + 1, 7):  # Projeção semanal
                data_proj = (hoje + datetime.timedelta(days=d)).isoformat()
                projecao.append({
                    "data": data_proj,
                    "tipo": "projecao_vendas",
                    "descricao": f"Projeção vendas (semana)",
                    "valor": media_diaria * 7,
                })

        projecao.sort(key=lambda x: x['data'])
        
        # Calcular saldo acumulado
        saldo = saldo_atual
        for item in projecao:
            saldo += item['valor']
            item['saldo_acumulado'] = saldo
        
        return {"saldo_atual": saldo_atual, "projecao": projecao, "media_diaria_vendas": media_diaria}

    # ============================================================
    # ============ CONTROLE DE COMISSÕES ============
    # ============================================================

    def calcular_comissoes(self, periodo="mes"):
        """Calcula comissões detalhadas por vendedor no período."""
        hoje = datetime.date.today()
        if periodo == "mes":
            inicio = hoje.replace(day=1).isoformat()
        elif periodo == "semana":
            inicio = (hoje - datetime.timedelta(days=hoje.weekday())).isoformat()
        else:
            inicio = hoje.replace(month=1, day=1).isoformat()

        query = """
            SELECT v.vendedor, vd.comissao as taxa_comissao,
                   COUNT(*) as num_vendas,
                   SUM(v.valor_total) as faturamento,
                   SUM(v.lucro) as lucro,
                   SUM(v.valor_total) * (vd.comissao / 100.0) as comissao_vendas
            FROM vendas v
            LEFT JOIN vendedores vd ON v.vendedor = vd.nome
            WHERE v.status = 'Concluída' AND v.data_venda >= ? AND v.vendedor != ''
            GROUP BY v.vendedor
            ORDER BY comissao_vendas DESC
        """
        df = pd.read_sql_query(query, self.conn, params=[inicio])

        # Adicionar serviços
        query_serv = """
            SELECT s.vendedor, vd.comissao as taxa_comissao,
                   COUNT(*) as num_servicos,
                   SUM(s.valor) as faturamento_servicos,
                   SUM(s.valor) * (vd.comissao / 100.0) as comissao_servicos
            FROM servicos s
            LEFT JOIN vendedores vd ON s.vendedor = vd.nome
            WHERE s.data >= ? AND s.vendedor != ''
            GROUP BY s.vendedor
        """
        df_serv = pd.read_sql_query(query_serv, self.conn, params=[inicio])

        return {"vendas": df, "servicos": df_serv, "periodo_inicio": inicio}

    # ============================================================
    # ============ NOTIFICAÇÕES / ALERTAS ============
    # ============================================================

    def coletar_alertas(self):
        """Coleta todos os alertas pendentes do sistema."""
        alertas = []

        # Estoque baixo
        try:
            df_baixo = self.produtos_abaixo_estoque_minimo()
            if not df_baixo.empty:
                alertas.append({
                    "tipo": "estoque_baixo",
                    "icone": "📦",
                    "titulo": "Estoque Baixo",
                    "mensagem": f"{len(df_baixo)} produto(s) abaixo do mínimo",
                    "prioridade": "alta",
                    "dados": df_baixo,
                })
        except Exception:
            pass

        # Validade
        try:
            df_vencendo = self.verificar_produtos_vencendo(dias_alerta=7)
            if not df_vencendo.empty:
                alertas.append({
                    "tipo": "validade",
                    "icone": "⏰",
                    "titulo": "Validade Próxima",
                    "mensagem": f"{len(df_vencendo)} produto(s) próximo(s) de vencer",
                    "prioridade": "alta",
                    "dados": df_vencendo,
                })
        except Exception:
            pass

        # Fiados vencidos
        try:
            df_fiados = self.listar_fiados(status='Pendente')
            if not df_fiados.empty:
                hoje = datetime.date.today().isoformat()
                vencidos = df_fiados[df_fiados['data_vencimento'] < hoje] if 'data_vencimento' in df_fiados.columns else pd.DataFrame()
                if not vencidos.empty:
                    alertas.append({
                        "tipo": "fiado_vencido",
                        "icone": "💳",
                        "titulo": "Fiados Vencidos",
                        "mensagem": f"{len(vencidos)} fiado(s) vencido(s)",
                        "prioridade": "alta",
                        "dados": vencidos,
                    })
        except Exception:
            pass

        # Contas a pagar vencidas
        try:
            df_contas = self.contas_vencidas()
            if not df_contas.empty:
                alertas.append({
                    "tipo": "conta_vencida",
                    "icone": "🔴",
                    "titulo": "Contas Vencidas",
                    "mensagem": f"{len(df_contas)} conta(s) a pagar vencida(s)",
                    "prioridade": "alta",
                    "dados": df_contas,
                })
        except Exception:
            pass

        # Contas vencendo em 7 dias
        try:
            df_vencendo_contas = self.contas_vencendo(dias=7)
            if not df_vencendo_contas.empty:
                alertas.append({
                    "tipo": "conta_vencendo",
                    "icone": "🟡",
                    "titulo": "Contas Próximas",
                    "mensagem": f"{len(df_vencendo_contas)} conta(s) a pagar nos próximos 7 dias",
                    "prioridade": "media",
                    "dados": df_vencendo_contas,
                })
        except Exception:
            pass

        return alertas

    # --- Onda 3: Inventário / Contagem Cega ---
    def criar_sessao_inventario(self, observacoes="", usuario_id=None, loja_id=1):
        """Inicia uma nova sessão de inventário com contagem cega."""
        uid = usuario_id if usuario_id is not None else self.current_user_id
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO inventarios (status, usuario_id, observacoes, loja_id)
                VALUES ('aberto', ?, ?, ?)
                """,
                (uid, str(observacoes or "").strip(), int(loja_id or 1)),
            )
            self.conn.commit()
            inv_id = int(cur.lastrowid)
        self.registrar_alteracao(
            "Iniciar Inventário",
            f"Sessão de inventário #{inv_id} aberta",
            usuario_id=uid,
            entidade="inventario",
            entidade_id=str(inv_id),
        )
        return True, inv_id

    def listar_sessoes_inventario(self, limit=30):
        """Lista sessões de inventário recentes."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, data_inicio, data_fim, status, usuario_id, supervisor_id, motivo_ajuste, observacoes
                FROM inventarios
                ORDER BY id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [
            {
                "id": r[0],
                "data_inicio": r[1],
                "data_fim": r[2] or "",
                "status": r[3],
                "usuario_id": r[4],
                "supervisor_id": r[5],
                "motivo_ajuste": r[6] or "",
                "observacoes": r[7] or "",
            }
            for r in rows
        ]

    def registrar_contagem_inventario(self, inventario_id, codigo_ou_termo, qtd_contada=1, modo_somar=True):
        """Registra leitura contínua (código de barras ou nome) em sessão de contagem cega."""
        termo = str(codigo_ou_termo or "").strip()
        if not termo:
            return False, "Informe o código de barras ou nome do produto."
        try:
            qtd_val = float(qtd_contada)
        except (TypeError, ValueError):
            return False, "Quantidade contada inválida."
        if qtd_val < 0:
            return False, "Quantidade contada não pode ser negativa."

        with self._lock:
            cur = self.conn.cursor()
            sess = cur.execute("SELECT status FROM inventarios WHERE id = ?", (int(inventario_id),)).fetchone()
            if not sess:
                return False, "Sessão de inventário não encontrada."
            if sess[0] != "aberto":
                return False, f"Sessão #{inventario_id} já está encerrada ({sess[0]})."

            prod = cur.execute(
                """
                SELECT id, nome, COALESCE(codigo_barras, ''), COALESCE(quantidade, 0), COALESCE(preco_compra, 0)
                FROM produtos
                WHERE codigo_barras = ?
                LIMIT 1
                """,
                (termo,),
            ).fetchone()
            if not prod:
                prod = cur.execute(
                    """
                    SELECT id, nome, COALESCE(codigo_barras, ''), COALESCE(quantidade, 0), COALESCE(preco_compra, 0)
                    FROM produtos
                    WHERE LOWER(nome) = LOWER(?)
                    LIMIT 1
                    """,
                    (termo,),
                ).fetchone()
            if not prod:
                return False, f"Produto não encontrado para '{termo}'."

            pid, nome_p, ean_p, qtd_sys, custo_u = int(prod[0]), str(prod[1]), str(prod[2]), float(prod[3] or 0), float(prod[4] or 0)

            existente = cur.execute(
                "SELECT id, qtd_sistema, qtd_contada FROM inventario_itens WHERE inventario_id = ? AND produto_id = ?",
                (int(inventario_id), pid),
            ).fetchone()

            agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if existente:
                item_id, snap_sys, cont_atual = int(existente[0]), float(existente[1]), float(existente[2])
                nova_contada = round((cont_atual + qtd_val) if modo_somar else qtd_val, 3)
                diverg = round(nova_contada - snap_sys, 3)
                cur.execute(
                    """
                    UPDATE inventario_itens
                    SET qtd_contada = ?, divergencia = ?, contado_em = ?
                    WHERE id = ?
                    """,
                    (nova_contada, diverg, agora, item_id),
                )
            else:
                nova_contada = round(qtd_val, 3)
                diverg = round(nova_contada - qtd_sys, 3)
                cur.execute(
                    """
                    INSERT INTO inventario_itens (
                        inventario_id, produto_id, codigo_barras, nome_produto,
                        qtd_sistema, qtd_contada, divergencia, custo_unitario, contado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (int(inventario_id), pid, ean_p, nome_p, qtd_sys, nova_contada, diverg, custo_u, agora),
                )
            self.conn.commit()

        return True, {
            "produto_id": pid,
            "nome_produto": nome_p,
            "codigo_barras": ean_p,
            "qtd_contada": nova_contada,
        }

    def listar_itens_inventario(self, inventario_id, ocultar_sistema=False):
        """Lista itens contados em uma sessão de inventário (oculta saldo do sistema durante contagem cega)."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT produto_id, codigo_barras, nome_produto, qtd_sistema, qtd_contada, divergencia, custo_unitario, contado_em
                FROM inventario_itens
                WHERE inventario_id = ?
                ORDER BY contado_em DESC, id DESC
                """,
                (int(inventario_id),),
            ).fetchall()

        itens = []
        for r in rows:
            item = {
                "produto_id": r[0],
                "codigo_barras": r[1] or "",
                "nome_produto": r[2] or "",
                "qtd_contada": float(r[4] or 0),
                "custo_unitario": float(r[6] or 0),
                "contado_em": r[7] or "",
            }
            if ocultar_sistema:
                item["qtd_sistema"] = None
                item["divergencia"] = None
                item["valor_divergencia"] = None
            else:
                div = float(r[5] or 0)
                item["qtd_sistema"] = float(r[3] or 0)
                item["divergencia"] = div
                item["valor_divergencia"] = round(div * float(r[6] or 0), 2)
            itens.append(item)
        return itens

    def aplicar_ajuste_inventario(self, inventario_id, motivo, supervisor_id, usuario_id=None):
        """Aplica ajustes de estoque da sessão de inventário com motivo obrigatório e trilha de auditoria."""
        motivo_limpo = str(motivo or "").strip()
        if not motivo_limpo:
            return False, "Motivo do ajuste é obrigatório."
        if not supervisor_id:
            return False, "Autorização de supervisor é obrigatória para aplicar ajuste de inventário."

        uid = usuario_id if usuario_id is not None else self.current_user_id
        agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ajustes = []

        with self._lock:
            cur = self.conn.cursor()
            sess = cur.execute("SELECT status FROM inventarios WHERE id = ?", (int(inventario_id),)).fetchone()
            if not sess:
                return False, "Sessão de inventário não encontrada."
            if sess[0] != "aberto":
                return False, f"Sessão #{inventario_id} já foi finalizada ({sess[0]})."

            itens = cur.execute(
                """
                SELECT produto_id, nome_produto, qtd_sistema, qtd_contada, divergencia
                FROM inventario_itens
                WHERE inventario_id = ?
                """,
                (int(inventario_id),),
            ).fetchall()
            if not itens:
                return False, "Nenhum item foi contado nesta sessão de inventário."

            for pid, nome_p, qtd_sys, qtd_cont, diverg in itens:
                qtd_cont_f = max(0.0, float(qtd_cont or 0))
                qtd_final = int(qtd_cont_f) if qtd_cont_f.is_integer() else round(qtd_cont_f, 3)
                row_prod = cur.execute("SELECT quantidade FROM produtos WHERE id = ?", (int(pid),)).fetchone()
                qtd_antes = row_prod[0] if row_prod else qtd_sys
                cur.execute(
                    "UPDATE produtos SET quantidade = ? WHERE id = ?",
                    (qtd_final, int(pid)),
                )
                ajustes.append({
                    "produto_id": int(pid),
                    "nome": nome_p,
                    "antes": qtd_antes,
                    "depois": qtd_final,
                    "divergencia": round(float(qtd_final) - float(qtd_antes or 0), 3),
                })

            cur.execute(
                """
                UPDATE inventarios
                SET status = 'concluido', data_fim = ?, supervisor_id = ?, motivo_ajuste = ?
                WHERE id = ?
                """,
                (agora, int(supervisor_id), motivo_limpo, int(inventario_id)),
            )
            self.conn.commit()

        self.registrar_alteracao(
            "Ajuste de Inventário",
            f"Inventário #{inventario_id} aplicado ({len(ajustes)} item(ns)). Motivo: {motivo_limpo}",
            usuario_id=uid or supervisor_id,
            entidade="inventario",
            entidade_id=str(inventario_id),
            antes={"qtd_itens": len(ajustes)},
            depois={"supervisor_id": supervisor_id, "motivo": motivo_limpo, "ajustes": ajustes[:30]},
        )
        return True, {"inventario_id": int(inventario_id), "itens_ajustados": len(ajustes), "ajustes": ajustes}

    # --- Onda 3: Pacote Mensal do Contador (ZIP) ---
    def exportar_pacote_contador(self, mes_ano=None, destino_zip_path=None):
        """
        Gera ZIP mensal completo para contabilidade contendo:
        - CSV de vendas por forma de pagamento
        - CSV de vendas detalhadas
        - CSV de entradas e CSV de gastos
        - DRE do mês (TXT e JSON)
        - Todos os XMLs de NF-e importados no mês
        """
        import calendar
        import csv
        import io
        import json
        import re
        import zipfile

        hoje = datetime.date.today()
        ref = str(mes_ano or hoje.strftime("%Y-%m")).strip()
        if re.fullmatch(r"\d{2}/\d{4}", ref):
            mes_i, ano_i = int(ref[:2]), int(ref[3:])
            ref = f"{ano_i:04d}-{mes_i:02d}"
        elif re.fullmatch(r"\d{4}-\d{2}", ref):
            ano_i, mes_i = int(ref[:4]), int(ref[5:])
        else:
            return False, f"Formato de competência inválido: '{ref}'. Use AAAA-MM ou MM/AAAA."

        if not (1 <= mes_i <= 12):
            return False, "Mês inválido."

        ultimo_dia = calendar.monthrange(ano_i, mes_i)[1]
        inicio = f"{ano_i:04d}-{mes_i:02d}-01"
        fim = f"{ano_i:04d}-{mes_i:02d}-{ultimo_dia:02d}"
        fim_ts = fim + "T23:59:59.999999"

        if not destino_zip_path:
            pasta_exports = os.path.join(self.data_dir, "exports")
            os.makedirs(pasta_exports, exist_ok=True)
            destino_zip_path = os.path.join(pasta_exports, f"pacote_contador_{ref}.zip")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(destino_zip_path)), exist_ok=True)

        with self._lock:
            cur = self.conn.cursor()
            vendas = cur.execute(
                """
                SELECT v.id, v.data_venda, COALESCE(p.nome, 'Produto #' || v.produto_id),
                       v.quantidade, v.valor_total, COALESCE(v.desconto, 0), COALESCE(v.lucro, 0),
                       COALESCE(v.forma_pagamento, 'Dinheiro'), COALESCE(v.cliente, 'Consumidor Final'),
                       COALESCE(v.vendedor, ''), COALESCE(v.valor_dinheiro, 0), COALESCE(v.valor_pix, 0),
                       COALESCE(v.valor_cartao_debito, 0), COALESCE(v.valor_cartao_credito, 0), COALESCE(v.valor_fiado, 0)
                FROM vendas v
                LEFT JOIN produtos p ON v.produto_id = p.id
                WHERE v.status = 'Concluída' AND v.data_venda >= ? AND v.data_venda <= ?
                ORDER BY v.data_venda ASC, v.id ASC
                """,
                (inicio, fim_ts),
            ).fetchall()

            entradas = cur.execute(
                """
                SELECT id, data, valor, COALESCE(descricao, '')
                FROM entradas
                WHERE substr(data, 1, 10) >= ? AND substr(data, 1, 10) <= ?
                ORDER BY data ASC, id ASC
                """,
                (inicio, fim),
            ).fetchall()

            gastos = cur.execute(
                """
                SELECT id, data, valor, categoria, COALESCE(descricao, ''), COALESCE(tipo, 'Variável')
                FROM gastos
                WHERE substr(data, 1, 10) >= ? AND substr(data, 1, 10) <= ?
                ORDER BY data ASC, id ASC
                """,
                (inicio, fim),
            ).fetchall()

            servicos = cur.execute(
                """
                SELECT COALESCE(SUM(valor), 0), COALESCE(SUM(custo), 0)
                FROM servicos
                WHERE data >= ? AND data <= ?
                """,
                (inicio, fim_ts),
            ).fetchone()

            xmls = cur.execute(
                """
                SELECT id, numero_nfe, chave_acesso, fornecedor, valor_total, xml_conteudo, arquivo_nome, importado_em
                FROM nfe_importadas
                WHERE substr(importado_em, 1, 7) = ?
                ORDER BY id ASC
                """,
                (ref,),
            ).fetchall()

            cfg_row = cur.execute("SELECT empresa_nome, empresa_cnpj FROM config_nota ORDER BY id ASC LIMIT 1").fetchone()

        empresa_nome = (cfg_row[0] if cfg_row and cfg_row[0] else "Estoke Ao Cubo")
        empresa_cnpj = (cfg_row[1] if cfg_row and cfg_row[1] else "")

        def _csv_bytes(headers, rows):
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
            return buf.getvalue().encode("utf-8-sig")

        # Totais por forma de pagamento + DRE exato do mês
        tot_din = tot_pix = tot_deb = tot_cred = tot_fiado = 0.0
        rec_bruta_vendas = deducoes = cmv_vendas = 0.0
        for v in vendas:
            vt = float(v[4] or 0)
            desc = float(v[5] or 0)
            luc = float(v[6] or 0)
            rec_bruta_vendas += vt
            deducoes += desc
            cmv_vendas += ((vt - desc) - luc)
            d, px, db, cr, fi = float(v[10] or 0), float(v[11] or 0), float(v[12] or 0), float(v[13] or 0), float(v[14] or 0)
            if (d + px + db + cr + fi) > 0:
                tot_din += d
                tot_pix += px
                tot_deb += db
                tot_cred += cr
                tot_fiado += fi
            else:
                fp = str(v[7] or "").lower()
                liq = max(0.0, vt - desc)
                if "pix" in fp:
                    tot_pix += liq
                elif "déb" in fp or "deb" in fp:
                    tot_deb += liq
                elif "créd" in fp or "cred" in fp:
                    tot_cred += liq
                elif "fiad" in fp:
                    tot_fiado += liq
                else:
                    tot_din += liq

        rec_serv = float((servicos[0] if servicos else 0) or 0)
        custo_serv = float((servicos[1] if servicos else 0) or 0)
        gastos_fixos = sum(float(g[2] or 0) for g in gastos if str(g[5] or "") == "Fixo")
        gastos_var = sum(float(g[2] or 0) for g in gastos if str(g[5] or "") != "Fixo")

        receita_bruta = round(rec_bruta_vendas + rec_serv, 2)
        receita_liquida = round(receita_bruta - deducoes, 2)
        custo_total = round(cmv_vendas + custo_serv, 2)
        lucro_bruto = round(receita_liquida - custo_total, 2)
        despesas_op = round(gastos_fixos + gastos_var, 2)
        lucro_liquido = round(lucro_bruto - despesas_op, 2)

        dre_dict = {
            "empresa": empresa_nome,
            "cnpj": empresa_cnpj,
            "competencia": ref,
            "periodo": {"inicio": inicio, "fim": fim},
            "receita_bruta": receita_bruta,
            "deducoes_descontos": round(deducoes, 2),
            "receita_liquida": receita_liquida,
            "cmv_custos": custo_total,
            "lucro_bruto": lucro_bruto,
            "despesas_fixas": round(gastos_fixos, 2),
            "despesas_variaveis": round(gastos_var, 2),
            "despesas_operacionais": despesas_op,
            "lucro_liquido": lucro_liquido,
        }

        pagamentos_rows = [
            ("Dinheiro", f"{tot_din:.2f}"),
            ("Pix", f"{tot_pix:.2f}"),
            ("Cartao Debito", f"{tot_deb:.2f}"),
            ("Cartao Credito", f"{tot_cred:.2f}"),
            ("Fiado", f"{tot_fiado:.2f}"),
            ("TOTAL_LIQUIDO_VENDAS", f"{(tot_din + tot_pix + tot_deb + tot_cred + tot_fiado):.2f}"),
        ]

        vendas_rows = [
            (r[0], r[1], r[2], r[3], f"{float(r[4] or 0):.2f}", f"{float(r[5] or 0):.2f}", f"{float(r[6] or 0):.2f}", r[7], r[8], r[9])
            for r in vendas
        ]
        entradas_rows = [(r[0], r[1], f"{float(r[2] or 0):.2f}", r[3]) for r in entradas]
        gastos_rows = [(r[0], r[1], f"{float(r[2] or 0):.2f}", r[3], r[4], r[5]) for r in gastos]

        dre_txt = (
            f"DEMONSTRATIVO DE RESULTADO DO EXERCÍCIO (DRE) — {ref}\n"
            f"Empresa: {empresa_nome} | CNPJ: {empresa_cnpj or 'Não informado'}\n"
            + ("=" * 60) + "\n"
            f"(+) Receita Bruta (Vendas + Serviços): R$ {receita_bruta:.2f}\n"
            f"(-) Deduções / Descontos:              R$ {deducoes:.2f}\n"
            f"(=) Receita Líquida:                   R$ {receita_liquida:.2f}\n"
            f"(-) CMV / Custo dos Serviços:          R$ {custo_total:.2f}\n"
            f"(=) Lucro Bruto:                       R$ {lucro_bruto:.2f}\n"
            f"(-) Despesas Fixas:                    R$ {gastos_fixos:.2f}\n"
            f"(-) Despesas Variáveis:                R$ {gastos_var:.2f}\n"
            f"(=) Resultado / Lucro Líquido:         R$ {lucro_liquido:.2f}\n"
        )

        tmp_zip = destino_zip_path + ".tmp"
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("vendas_por_pagamento.csv", _csv_bytes(["Forma de Pagamento", "Valor Liquido"], pagamentos_rows))
            zf.writestr(
                "vendas_detalhadas.csv",
                _csv_bytes(["ID", "Data/Hora", "Produto", "Qtd", "Valor Total", "Desconto", "Lucro", "Forma Pagamento", "Cliente", "Vendedor"], vendas_rows),
            )
            zf.writestr("entradas.csv", _csv_bytes(["ID", "Data", "Valor", "Descricao"], entradas_rows))
            zf.writestr("gastos.csv", _csv_bytes(["ID", "Data", "Valor", "Categoria", "Descricao", "Tipo"], gastos_rows))
            zf.writestr("dre_resumo.txt", dre_txt.encode("utf-8"))
            zf.writestr("dre_resumo.json", json.dumps(dre_dict, ensure_ascii=False, indent=2).encode("utf-8"))

            used_names = set()
            for xid, num_nf, chave, _forn, _vtot, xml_body, arq_nome, _imp in xmls:
                safe_base = re.sub(r"[^0-9A-Za-z._-]", "_", arq_nome or f"nfe_{num_nf or xid}.xml")
                if not safe_base.lower().endswith(".xml"):
                    safe_base += ".xml"
                if safe_base in used_names:
                    safe_base = f"{xid}_{safe_base}"
                used_names.add(safe_base)
                zf.writestr(f"xmls_nfe/{safe_base}", str(xml_body).encode("utf-8"))

        os.replace(tmp_zip, destino_zip_path)
        return True, {
            "zip_path": destino_zip_path,
            "mes_ano": ref,
            "qtd_vendas": len(vendas),
            "qtd_entradas": len(entradas),
            "qtd_gastos": len(gastos),
            "qtd_xmls": len(xmls),
            "dre": dre_dict,
        }

    # --- Onda 4: Pagamentos Mercado Pago & Conciliação PIX ---
    @staticmethod
    def _sanitize_mp_payload(payload):
        if not isinstance(payload, dict):
            return {}
        blocked = {"access_token", "token", "token_api", "csc", "id_csc", "authorization", "secret"}
        return {
            k: ("***" if str(k).lower() in blocked else v)
            for k, v in payload.items()
        }

    @staticmethod
    def _parse_ts_flex(ts_str):
        raw = str(ts_str or "").strip()
        if not raw:
            return None
        clean = raw.replace("T", " ").split(".")[0].split("+")[0].rstrip("Z")[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(clean, fmt)
            except ValueError:
                continue
        return None

    def registrar_pagamento_mp(
        self,
        payment_id,
        tipo="pix",
        external_reference="",
        status="pending",
        status_detail="",
        valor=0.0,
        point_intent_id="",
        device_id="",
        payload=None,
        data_aprovacao="",
    ):
        """
        UPSERT idempotente em pagamentos_mp protegido contra downgrade de 'approved'
        caso chegue um webhook atrasado com status 'pending' ou 'in_process'.
        """
        pid = str(payment_id or "").strip()
        if not pid:
            return False, "payment_id inválido."

        tipo_norm = str(tipo or "pix").strip().lower()
        status_norm = str(status or "pending").strip().lower()
        ext_ref = str(external_reference or "").strip()
        val_f = round(float(valor or 0.0), 2)
        agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload_clean = self._sanitize_mp_payload(payload or {})
        payload_json = json.dumps(payload_clean, ensure_ascii=False)

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO pagamentos_mp (
                    payment_id, tipo, external_reference, status, status_detail,
                    valor, point_intent_id, device_id, payload_json, data_aprovacao,
                    criado_em, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(payment_id) DO UPDATE SET
                    tipo = COALESCE(NULLIF(excluded.tipo, ''), pagamentos_mp.tipo),
                    external_reference = CASE
                        WHEN COALESCE(excluded.external_reference, '') != '' THEN excluded.external_reference
                        ELSE pagamentos_mp.external_reference
                    END,
                    status = CASE
                        WHEN pagamentos_mp.status = 'approved' AND excluded.status IN ('pending', 'in_process')
                            THEN 'approved'
                        ELSE excluded.status
                    END,
                    status_detail = CASE
                        WHEN pagamentos_mp.status = 'approved' AND excluded.status IN ('pending', 'in_process')
                            THEN pagamentos_mp.status_detail
                        ELSE excluded.status_detail
                    END,
                    valor = CASE WHEN excluded.valor > 0 THEN excluded.valor ELSE pagamentos_mp.valor END,
                    point_intent_id = COALESCE(NULLIF(excluded.point_intent_id, ''), pagamentos_mp.point_intent_id),
                    device_id = COALESCE(NULLIF(excluded.device_id, ''), pagamentos_mp.device_id),
                    payload_json = excluded.payload_json,
                    data_aprovacao = COALESCE(NULLIF(excluded.data_aprovacao, ''), pagamentos_mp.data_aprovacao),
                    atualizado_em = excluded.atualizado_em
                """,
                (
                    pid,
                    tipo_norm,
                    ext_ref,
                    status_norm,
                    str(status_detail or ""),
                    val_f,
                    str(point_intent_id or ""),
                    str(device_id or ""),
                    payload_json,
                    str(data_aprovacao or ""),
                    agora,
                    agora,
                ),
            )
            self.conn.commit()
        return True, pid

    def conciliar_pix_caixa(self, caixa_id):
        """Cruza vendas.valor_pix do caixa com webhooks aprovados em pagamentos_mp."""
        with self._lock:
            row = self.conn.execute(
                "SELECT data_abertura, COALESCE(data_fechamento, '') FROM caixa WHERE id = ?",
                (int(caixa_id),),
            ).fetchone()
            if not row:
                return {"erro": "Caixa não encontrado."}
            return self._conciliar_pix_caixa_locked(int(caixa_id), data_abertura=row[0], data_fechamento=row[1] or datetime.datetime.now().isoformat())

    def _conciliar_pix_caixa_locked(self, caixa_id, data_abertura=None, data_fechamento=None):
        """Conciliação em 2 fases (exato por external_reference e aproximado 1-para-1 por valor/horário)."""
        cur = self.conn.cursor()
        vendas_rows = cur.execute(
            """
            SELECT COALESCE(NULLIF(transacao_id, ''), 'venda_' || id) AS tx_key,
                   ROUND(SUM(
                       CASE WHEN COALESCE(valor_pix, 0) > 0 THEN valor_pix
                            WHEN LOWER(COALESCE(forma_pagamento, '')) LIKE '%pix%'
                                 AND (COALESCE(valor_dinheiro, 0) + COALESCE(valor_cartao_debito, 0)
                                      + COALESCE(valor_cartao_credito, 0) + COALESCE(valor_fiado, 0)) = 0
                            THEN (valor_total - COALESCE(desconto, 0))
                            ELSE 0 END
                   ), 2) AS val_pix,
                   MIN(data_venda) AS dt_venda
            FROM vendas
            WHERE caixa_id = ? AND status = 'Concluída'
            GROUP BY tx_key
            HAVING val_pix > 0.005
            ORDER BY dt_venda ASC
            """,
            (int(caixa_id),),
        ).fetchall()

        vendas_pix = [
            {"transacao_id": str(r[0]), "valor_pix": round(float(r[1]), 2), "data_venda": str(r[2] or "")}
            for r in vendas_rows
        ]
        tx_set = {v["transacao_id"] for v in vendas_pix}

        dt_ab = self._parse_ts_flex(data_abertura) or (datetime.datetime.now() - datetime.timedelta(hours=24))
        dt_fc = self._parse_ts_flex(data_fechamento) or datetime.datetime.now()
        win_start = dt_ab - datetime.timedelta(minutes=5)
        win_end = dt_fc + datetime.timedelta(minutes=5)

        mp_rows = cur.execute(
            """
            SELECT payment_id, external_reference, valor, data_aprovacao, criado_em
            FROM pagamentos_mp
            WHERE tipo = 'pix' AND status = 'approved'
            ORDER BY id ASC
            """
        ).fetchall()

        mps_turno = []
        for r in mp_rows:
            pid, ext_ref, val_mp, dt_ap, dt_cr = (
                str(r[0]),
                str(r[1] or "").strip(),
                round(float(r[2] or 0.0), 2),
                str(r[3] or ""),
                str(r[4] or ""),
            )
            ts_mp = self._parse_ts_flex(dt_ap) or self._parse_ts_flex(dt_cr)
            in_window = ts_mp is not None and (win_start <= ts_mp <= win_end)
            if (ext_ref and ext_ref in tx_set) or in_window:
                mps_turno.append(
                    {
                        "payment_id": pid,
                        "external_reference": ext_ref,
                        "valor": val_mp,
                        "ts": ts_mp,
                        "data_referencia": dt_ap or dt_cr,
                    }
                )

        used_vendas = set()
        used_mps = set()
        conciliados_exatos = []
        conciliados_aproximados = []
        divergencia_valor = []

        # Fase 1: Match Exato por external_reference (1-para-1)
        for vi, v in enumerate(vendas_pix):
            for mi, m in enumerate(mps_turno):
                if mi in used_mps or not m["external_reference"]:
                    continue
                if m["external_reference"] == v["transacao_id"]:
                    used_vendas.add(vi)
                    used_mps.add(mi)
                    if abs(m["valor"] - v["valor_pix"]) <= 0.01:
                        conciliados_exatos.append(
                            {
                                "transacao_id": v["transacao_id"],
                                "payment_id": m["payment_id"],
                                "valor": v["valor_pix"],
                                "match": "exato",
                            }
                        )
                    else:
                        divergencia_valor.append(
                            {
                                "transacao_id": v["transacao_id"],
                                "payment_id": m["payment_id"],
                                "valor_pdv": v["valor_pix"],
                                "valor_mp": m["valor"],
                                "diferenca": round(m["valor"] - v["valor_pix"], 2),
                            }
                        )
                    break

        # Fase 2: Match Aproximado por Valor + Proximidade Horária (consome cada webhook no máximo 1 vez)
        for vi, v in enumerate(vendas_pix):
            if vi in used_vendas:
                continue
            ts_v = self._parse_ts_flex(v["data_venda"])
            best_mi = None
            best_diff_sec = None
            for mi, m in enumerate(mps_turno):
                if mi in used_mps:
                    continue
                # Se possui external_reference de outra venda desconhecida, não casar por aproximação
                if m["external_reference"]:
                    continue
                if abs(m["valor"] - v["valor_pix"]) <= 0.01:
                    if ts_v and m["ts"]:
                        diff_sec = abs((m["ts"] - ts_v).total_seconds())
                    else:
                        diff_sec = 0.0
                    if best_mi is None or diff_sec < best_diff_sec:
                        best_mi = mi
                        best_diff_sec = diff_sec

            if best_mi is not None:
                used_vendas.add(vi)
                used_mps.add(best_mi)
                m = mps_turno[best_mi]
                conciliados_aproximados.append(
                    {
                        "transacao_id": v["transacao_id"],
                        "payment_id": m["payment_id"],
                        "valor": v["valor_pix"],
                        "match": "aproximado_valor_horario",
                    }
                )

        vendas_sem_webhook = [v for vi, v in enumerate(vendas_pix) if vi not in used_vendas]
        webhooks_sem_venda = [
            {
                "payment_id": m["payment_id"],
                "external_reference": m["external_reference"],
                "valor": m["valor"],
                "data_referencia": m["data_referencia"],
            }
            for mi, m in enumerate(mps_turno)
            if mi not in used_mps
        ]

        total_pix_pdv = round(sum(v["valor_pix"] for v in vendas_pix), 2)
        total_pix_mp = round(sum(m["valor"] for m in mps_turno), 2)

        return {
            "caixa_id": int(caixa_id),
            "total_pix_pdv": total_pix_pdv,
            "total_pix_mp": total_pix_mp,
            "diferenca_total": round(total_pix_mp - total_pix_pdv, 2),
            "qtd_vendas_pix": len(vendas_pix),
            "qtd_pagamentos_mp": len(mps_turno),
            "conciliados_exatos": conciliados_exatos,
            "conciliados_aproximados": conciliados_aproximados,
            "vendas_sem_webhook": vendas_sem_webhook,
            "webhooks_sem_venda": webhooks_sem_venda,
            "divergencia_valor": divergencia_valor,
            "possui_divergencia": bool(
                vendas_sem_webhook or webhooks_sem_venda or divergencia_valor or abs(total_pix_mp - total_pix_pdv) > 0.01
            ),
        }
