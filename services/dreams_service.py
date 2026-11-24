"""
Módulo de Gerenciamento de Sonhos — Validação estrita, Sanitização e Prevenção de Sobreposição (OCC).
"""
import sqlite3
import html
import datetime
import threading
from typing import Dict, Any, Optional

class DreamValidationError(Exception):
    """Exceção levantada para falhas de validação de regras de negócio."""
    pass

class DreamConcurrencyError(Exception):
    """Exceção para modificações simultâneas que causariam perda de dados (OCC)."""
    pass

class SonhosService:
    def __init__(self, conn: sqlite3.Connection, lock: Optional[threading.RLock] = None):
        # Aceita tanto instância de Database quanto sqlite3.Connection (retrocompatibilidade)
        if hasattr(conn, "conn") and hasattr(conn, "_lock"):
            self._db = conn
            self._raw_conn = None
            self._explicit_lock = lock
            self._raw_lock = None
            self._ensure_open_fn = None
        else:
            self._db = None
            self._raw_conn = conn
            self._explicit_lock = lock
            self._raw_lock = lock
            self._ensure_open_fn = None
            if self._raw_lock is None:
                try:
                    from database import Database
                    db_lock, ensure_fn = Database.get_lock_for_connection(conn)
                    if db_lock is not None:
                        self._raw_lock = db_lock
                        self._ensure_open_fn = ensure_fn
                except Exception:
                    pass
            if self._raw_lock is None:
                self._raw_lock = threading.RLock()
        self._ensure_table()

    @property
    def conn(self) -> sqlite3.Connection:
        """Lê dinamicamente self._db.conn quando vinculado a Database (resiste a restaurar_backup)."""
        if self._db is not None:
            return self._db.conn
        return self._raw_conn

    @property
    def _lock(self) -> threading.RLock:
        """Lê dinamicamente self._db._lock quando vinculado a Database."""
        if self._explicit_lock is not None:
            return self._explicit_lock
        if self._db is not None:
            return self._db._lock
        return self._raw_lock

    def _ensure_open(self):
        """Valida que a conexão gerenciada pelo Database ainda está aberta."""
        if self._db is not None:
            ensure_fn = getattr(self._db, "_ensure_open", None)
            if ensure_fn is not None:
                ensure_fn()
            return
        if self._ensure_open_fn is not None:
            self._ensure_open_fn()

    def _ensure_table(self):
        """Garante a estrutura correta com a coluna 'version' para OCC."""
        with self._lock:
            self._ensure_open()
            cursor = self.conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sonhos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usuario_id INTEGER NOT NULL,
                    titulo TEXT NOT NULL,
                    descricao TEXT NOT NULL,
                    categoria TEXT,
                    data_registro TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
                )
            """)
            self.conn.commit()

    def _sanitizar_e_validar(self, dados: Dict[str, Any]) -> Dict[str, Any]:
        """Realiza sanitização robusta contra XSS e valida regras de negócio."""
        titulo = html.escape(str(dados.get('titulo', '')).strip())
        descricao = html.escape(str(dados.get('descricao', '')).strip())
        categoria = html.escape(str(dados.get('categoria', 'Geral')).strip())
        usuario_id = dados.get('usuario_id')
        data_registro = dados.get('data_registro', datetime.date.today().isoformat())

        # Validações estritas
        if not usuario_id or not isinstance(usuario_id, int):
            raise DreamValidationError("ID de usuário inválido ou ausente.")
        if not titulo or len(titulo) < 3 or len(titulo) > 100:
            raise DreamValidationError("O título deve ter entre 3 e 100 caracteres.")
        if not descricao or len(descricao) < 10:
            raise DreamValidationError("A descrição deve possuir pelo menos 10 caracteres.")

        try:
            datetime.date.fromisoformat(data_registro)
        except ValueError:
            raise DreamValidationError("Formato de data inválido. Use YYYY-MM-DD.")

        return {
            "usuario_id": usuario_id,
            "titulo": titulo,
            "descricao": descricao,
            "categoria": categoria,
            "data_registro": data_registro
        }

    def adicionar_sonho(self, dados_input: Dict[str, Any]) -> int:
        """Adiciona um sonho de forma segura via queries parametrizadas."""
        dados = self._sanitizar_e_validar(dados_input)
        with self._lock:
            self._ensure_open()
            cursor = self.conn.cursor()

            cursor.execute("""
                INSERT INTO sonhos (usuario_id, titulo, descricao, categoria, data_registro, version)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (dados['usuario_id'], dados['titulo'], dados['descricao'], dados['categoria'], dados['data_registro']))

            self.conn.commit()
            return cursor.lastrowid

    def editar_sonho(self, sonho_id: int, dados_input: Dict[str, Any], versao_lida: int, usuario_requisitor_id: int) -> bool:
        """Edita o registro garantindo consistência com Controle de Concorrência Otimista (OCC)."""
        dados = self._sanitizar_e_validar(dados_input)
        with self._lock:
            self._ensure_open()
            cursor = self.conn.cursor()

            # 1. Validação de Autorização e existência do registro
            cursor.execute("SELECT usuario_id, version FROM sonhos WHERE id = ?", (sonho_id,))
            row = cursor.fetchone()
            if not row:
                raise DreamValidationError("Sonho não encontrado.")

            dono_id, versao_atual = row[0], row[1]

            if dono_id != usuario_requisitor_id:
                raise PermissionError("Operação não autorizada. Este registro não pertence a você.")

            # 2. Verificação de Concorrência (OCC)
            if versao_atual != versao_lida:
                raise DreamConcurrencyError(
                    "O registro foi atualizado por outro usuário enquanto você o visualizava. "
                    "Por favor, atualize a página e refaça a edição para evitar perda de informações."
                )

            # 3. Atualização segura incrementando a versão
            cursor.execute("""
                UPDATE sonhos
                SET titulo = ?, descricao = ?, categoria = ?, data_registro = ?, version = version + 1
                WHERE id = ? AND version = ? AND usuario_id = ?
            """, (dados['titulo'], dados['descricao'], dados['categoria'], dados['data_registro'], sonho_id, versao_lida, usuario_requisitor_id))

            self.conn.commit()

            if cursor.rowcount == 0:
                # Se nenhuma linha foi alterada, a concorrência falhou no exato momento do commit
                raise DreamConcurrencyError("Erro de concorrência detectado durante a gravação.")

            return True

    def listar_sonhos(self, usuario_id: Optional[int] = None) -> list:
        """Lista todos os sonhos/anotações do banco de dados, decodificados."""
        # A consulta roda sob o lock do banco para evitar corrida de cursor em multi-thread.
        with self._lock:
            self._ensure_open()
            cursor = self.conn.cursor()
            if usuario_id:
                cursor.execute("""
                    SELECT id, usuario_id, titulo, descricao, categoria, data_registro, version
                    FROM sonhos WHERE usuario_id = ? ORDER BY data_registro DESC
                """, (usuario_id,))
            else:
                cursor.execute("""
                    SELECT id, usuario_id, titulo, descricao, categoria, data_registro, version
                    FROM sonhos ORDER BY data_registro DESC
                """)

            rows = cursor.fetchall()
        import html
        return [
            {
                "id": r[0],
                "usuario_id": r[1],
                "titulo": html.unescape(r[2]),
                "descricao": html.unescape(r[3]),
                "categoria": html.unescape(r[4]) if r[4] else "Geral",
                "data_registro": r[5],
                "version": r[6]
            }
            for r in rows
        ]

    def remover_sonho(self, sonho_id: int, usuario_requisitor_id: int) -> bool:
        """Remove um sonho/anotação garantindo que pertence ao usuário requisitor."""
        # Bloqueio estrito sob lock garante propriedade e consistência em ambiente multi-thread.
        with self._lock:
            self._ensure_open()
            cursor = self.conn.cursor()
            cursor.execute("SELECT usuario_id FROM sonhos WHERE id = ?", (sonho_id,))
            row = cursor.fetchone()
            if not row:
                raise DreamValidationError("Anotação não encontrada.")

            dono_id = row[0]
            if dono_id != usuario_requisitor_id:
                raise PermissionError("Operação não autorizada. Este registro não pertence a você.")

            cursor.execute("DELETE FROM sonhos WHERE id = ? AND usuario_id = ?", (sonho_id, usuario_requisitor_id))
            self.conn.commit()
            return cursor.rowcount > 0

