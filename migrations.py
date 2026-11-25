"""
Módulo de migrações versionadas do banco SQLite (via PRAGMA user_version).
Consolida alterações de schema de forma idempotente e transacional.
"""
import logging
import sqlite3

logger = logging.getLogger("EstokeAoCubo.migrations")

LATEST_SCHEMA_VERSION = 10


def get_user_version(conn) -> int:
    """Retorna a versão inteira atual armazenada em PRAGMA user_version."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def set_user_version(conn, version: int) -> None:
    """Atualiza PRAGMA user_version para a versão informada."""
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _get_columns(cursor, table: str) -> set:
    """Retorna o conjunto de nomes de colunas de uma tabela existente."""
    rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_exists(cursor, table: str) -> bool:
    """Verifica se a tabela existe no banco SQLite atual."""
    row = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(cursor, table: str, column: str, definition: str) -> bool:
    """Adiciona uma coluna caso não exista ainda na tabela. Retorna True se adicionou."""
    if not _table_exists(cursor, table):
        return False
    cols = _get_columns(cursor, table)
    if column in cols:
        return False
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    logger.info("Migração schema: coluna '%s.%s' adicionada.", table, column)
    return True


def migration_001_baseline_v43(cursor) -> None:
    """
    Migração 1 (Baseline v4.3): consolida de forma 100% idempotente todas as colunas,
    reestruturação legada da tabela estabelecimento e índices de performance.
    """
    # 1. Gastos
    _add_column_if_missing(cursor, "gastos", "tipo", "TEXT DEFAULT 'Variável'")
    _add_column_if_missing(cursor, "gastos", "loja_id", "INTEGER DEFAULT NULL")

    # 2. Metas
    _add_column_if_missing(cursor, "metas", "prazo", "TEXT")

    # 3. Estabelecimento (migração de estrutura legada com coluna 'item')
    if _table_exists(cursor, "estabelecimento"):
        cols_estab = _get_columns(cursor, "estabelecimento")
        if "item" in cols_estab and "area_local" not in cols_estab:
            cursor.execute("ALTER TABLE estabelecimento RENAME TO estabelecimento_old")
            cursor.execute(
                """
                CREATE TABLE estabelecimento (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_local TEXT,
                    tarefa TEXT,
                    status TEXT DEFAULT 'Pendente',
                    prioridade TEXT DEFAULT 'Média'
                )
                """
            )
            cnt = cursor.execute("SELECT COUNT(*) FROM estabelecimento_old").fetchone()[0]
            if cnt > 0:
                cursor.execute(
                    """
                    INSERT INTO estabelecimento (area_local, tarefa, status, prioridade)
                    SELECT item, detalhe, 'Pendente', 'Média' FROM estabelecimento_old
                    """
                )
            cursor.execute("DROP TABLE estabelecimento_old")
            logger.info("Migração schema: tabela 'estabelecimento' convertida do formato legado.")

    # 4. Vendas
    for col, defn in (
        ("forma_pagamento", "TEXT DEFAULT 'Dinheiro'"),
        ("cliente", "TEXT DEFAULT 'Consumidor Final'"),
        ("status", "TEXT DEFAULT 'Concluída'"),
        ("desconto", "REAL DEFAULT 0"),
        ("vendedor", "TEXT DEFAULT ''"),
        ("caixa_id", "INTEGER DEFAULT NULL"),
        ("valor_dinheiro", "REAL DEFAULT 0.0"),
        ("valor_pix", "REAL DEFAULT 0.0"),
        ("valor_cartao_debito", "REAL DEFAULT 0.0"),
        ("valor_cartao_credito", "REAL DEFAULT 0.0"),
        ("valor_fiado", "REAL DEFAULT 0.0"),
        ("formas_pagamento", "TEXT"),
        ("transacao_id", "TEXT DEFAULT NULL"),
        ("loja_id", "INTEGER DEFAULT NULL"),
    ):
        _add_column_if_missing(cursor, "vendas", col, defn)

    # 5. Clientes
    _add_column_if_missing(cursor, "clientes", "compras_recorrentes", "INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "clientes", "cpf_cnpj", "TEXT DEFAULT ''")

    # 6. Serviços / OS
    for col, defn in (
        ("vendedor", "TEXT DEFAULT ''"),
        ("status_os", "TEXT DEFAULT 'Concluído'"),
        ("contato_cliente", "TEXT DEFAULT ''"),
        ("equipamento", "TEXT DEFAULT ''"),
        ("data_previsao", "TEXT DEFAULT ''"),
        ("caixa_id", "INTEGER DEFAULT NULL"),
        ("loja_id", "INTEGER DEFAULT NULL"),
    ):
        _add_column_if_missing(cursor, "servicos", col, defn)

    # 7. Produtos (colunas base legadas + colunas v4.3)
    for col, defn in (
        ("categoria", "TEXT"),
        ("quantidade_inicial", "INTEGER"),
        ("horas_trabalho", "REAL"),
        ("valor_hora", "REAL"),
        ("margem_lucro", "REAL"),
        ("fator_risco", "REAL"),
        ("fator_raridade", "REAL"),
        ("preco_compra", "REAL"),
        ("imagem", "TEXT"),
        ("data_validade", "TEXT"),
        ("codigo_barras", "TEXT"),
        ("estoque_minimo", "INTEGER DEFAULT 5"),
        ("subcategoria", "TEXT"),
        ("tamanho", "TEXT"),
        ("sub_subcategoria", "TEXT"),
        ("desconto_item", "REAL DEFAULT 0"),
        ("estoque_p", "INTEGER DEFAULT 0"),
        ("estoque_m", "INTEGER DEFAULT 0"),
        ("estoque_g", "INTEGER DEFAULT 0"),
        ("estoque_gg", "INTEGER DEFAULT 0"),
        ("unidade_medida", "TEXT DEFAULT 'un'"),
        ("preco_por_peso", "REAL DEFAULT 0"),
        ("tipo_produto", "TEXT DEFAULT 'produto'"),
        ("sob_encomenda", "INTEGER DEFAULT 0"),
        ("lote_serie", "TEXT DEFAULT ''"),
        ("loja_id", "INTEGER DEFAULT NULL"),
    ):
        _add_column_if_missing(cursor, "produtos", col, defn)

    # 8. Entradas
    _add_column_if_missing(cursor, "entradas", "loja_id", "INTEGER DEFAULT NULL")

    # 9. Usuários
    _add_column_if_missing(cursor, "usuarios", "trocar_senha", "INTEGER DEFAULT 0")

    # 10. Caixa
    for col, defn in (
        ("usuario_id", "INTEGER DEFAULT 1"),
        ("loja_id", "INTEGER DEFAULT 1"),
        ("valor_fechamento_declarado", "REAL"),
        ("valor_fechamento_calculado", "REAL"),
    ):
        _add_column_if_missing(cursor, "caixa", col, defn)

    # 11. Config Nota
    for col, defn in (
        ("imprimir_segunda_via", "INTEGER DEFAULT 0"),
        ("largura_papel_mm", "INTEGER DEFAULT 80"),
        ("auto_detectar_papel", "INTEGER DEFAULT 1"),
        ("arredondamento_troco", "REAL DEFAULT 0.0"),
        ("chave_pix", "TEXT DEFAULT ''"),
        ("tipo_chave_pix", "TEXT DEFAULT 'CNPJ'"),
        ("pix_key", "TEXT DEFAULT ''"),
        ("pix_recebedor", "TEXT DEFAULT ''"),
        ("pix_cidade", "TEXT DEFAULT ''"),
    ):
        _add_column_if_missing(cursor, "config_nota", col, defn)

    # 12. Índices de performance e unicidade
    index_ddls = [
        ("produtos", "CREATE INDEX IF NOT EXISTS idx_produtos_nome ON produtos(nome)"),
        ("produtos", "CREATE INDEX IF NOT EXISTS idx_produtos_codigo ON produtos(codigo_barras)"),
        ("gastos", "CREATE INDEX IF NOT EXISTS idx_gastos_data ON gastos(data)"),
        ("gastos", "CREATE INDEX IF NOT EXISTS idx_gastos_categoria ON gastos(categoria)"),
        ("entradas", "CREATE INDEX IF NOT EXISTS idx_entradas_data ON entradas(data)"),
        ("historico", "CREATE INDEX IF NOT EXISTS idx_historico_data ON historico(data_hora)"),
        ("historico", "CREATE INDEX IF NOT EXISTS idx_historico_acao ON historico(acao)"),
        ("kit_itens", "CREATE INDEX IF NOT EXISTS idx_kit_itens_kit ON kit_itens(kit_id)"),
        ("estabelecimento", "CREATE INDEX IF NOT EXISTS idx_estabelecimento_status ON estabelecimento(status)"),
        ("vendas", "CREATE INDEX IF NOT EXISTS idx_vendas_data ON vendas(data_venda)"),
        ("vendas", "CREATE INDEX IF NOT EXISTS idx_vendas_cliente ON vendas(cliente)"),
        ("vendas", "CREATE INDEX IF NOT EXISTS idx_vendas_caixa ON vendas(caixa_id)"),
        ("vendas", "CREATE INDEX IF NOT EXISTS idx_vendas_transacao ON vendas(transacao_id)"),
        ("contas_receber", "CREATE INDEX IF NOT EXISTS idx_contas_receber_status ON contas_receber(status)"),
        ("fidelidade", "CREATE INDEX IF NOT EXISTS idx_fidelidade_cliente ON fidelidade(cliente_id)"),
        ("auditoria_caixa", "CREATE INDEX IF NOT EXISTS idx_auditoria_caixa_id ON auditoria_caixa(caixa_id)"),
        ("auditoria_caixa", "CREATE INDEX IF NOT EXISTS idx_auditoria_tipo ON auditoria_caixa(tipo_operacao)"),
        ("auditoria_caixa", "CREATE INDEX IF NOT EXISTS idx_auditoria_data ON auditoria_caixa(data_hora)"),
        ("mesas", "CREATE INDEX IF NOT EXISTS idx_mesas_status ON mesas(status)"),
        ("comandas", "CREATE INDEX IF NOT EXISTS idx_comandas_status ON comandas(status)"),
        ("comandas", "CREATE INDEX IF NOT EXISTS idx_comandas_mesa ON comandas(mesa_id)"),
        ("comanda_itens", "CREATE INDEX IF NOT EXISTS idx_comanda_itens_comanda ON comanda_itens(comanda_id)"),
        ("usuarios", "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_login ON usuarios(login)"),
        ("contas_pagar", "CREATE INDEX IF NOT EXISTS idx_contas_pagar_status ON contas_pagar(status)"),
        ("contas_pagar", "CREATE INDEX IF NOT EXISTS idx_contas_pagar_vencimento ON contas_pagar(data_vencimento)"),
        ("sangrias", "CREATE INDEX IF NOT EXISTS idx_sangrias_caixa ON sangrias(caixa_id)"),
        ("suprimentos", "CREATE INDEX IF NOT EXISTS idx_suprimentos_caixa ON suprimentos(caixa_id)"),
        ("impressoes_log", "CREATE INDEX IF NOT EXISTS idx_impressoes_venda ON impressoes_log(venda_id)"),
    ]
    for tbl, ddl in index_ddls:
        if _table_exists(cursor, tbl):
            cursor.execute(ddl)

    # Índice único parcial de caixa aberto por operador (pode falhar apenas se banco legado já tiver duplicata)
    if _table_exists(cursor, "caixa"):
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_caixa_aberto_unico ON caixa(usuario_id) WHERE status = 'aberto'"
            )
        except sqlite3.DatabaseError as exc:
            logger.debug("Aviso ao criar idx_caixa_aberto_unico: %s", exc)


def migration_002_auditoria_historico(cursor) -> None:
    """
    Migração 2: Auditoria completa em historico (usuario_id, entidade, entidade_id,
    antes_json, depois_json, hash_anterior, hash_registro).
    """
    for col, defn in (
        ("usuario_id", "INTEGER DEFAULT NULL"),
        ("entidade", "TEXT DEFAULT ''"),
        ("entidade_id", "TEXT DEFAULT ''"),
        ("antes_json", "TEXT DEFAULT ''"),
        ("depois_json", "TEXT DEFAULT ''"),
        ("hash_anterior", "TEXT DEFAULT ''"),
        ("hash_registro", "TEXT DEFAULT ''"),
    ):
        _add_column_if_missing(cursor, "historico", col, defn)

    if _table_exists(cursor, "historico"):
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_historico_usuario ON historico(usuario_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_historico_entidade ON historico(entidade, entidade_id)")


def migration_003_triggers_e_config_antifraude(cursor) -> None:
    """
    Migração 3: Flags antifraude em config_nota, metadados de cancelamento em vendas
    e triggers SQLite impedindo estoque negativo e venda com total líquido <= 0.
    """
    _add_column_if_missing(cursor, "config_nota", "permitir_estoque_negativo", "INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "config_nota", "desconto_max_sem_senha", "REAL DEFAULT 10.0")

    _add_column_if_missing(cursor, "vendas", "motivo_cancelamento", "TEXT DEFAULT ''")
    _add_column_if_missing(cursor, "vendas", "cancelado_por", "INTEGER DEFAULT NULL")
    _add_column_if_missing(cursor, "vendas", "data_cancelamento", "TEXT DEFAULT ''")

    if _table_exists(cursor, "produtos"):
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_estoque_nao_negativo_insert
            BEFORE INSERT ON produtos
            WHEN NEW.quantidade < 0
              AND (NOT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='config_nota')
                   OR COALESCE((SELECT permitir_estoque_negativo FROM config_nota LIMIT 1), 0) = 0)
            BEGIN
                SELECT RAISE(ABORT, 'ESTOQUE_NEGATIVO: Estoque não pode ficar negativo.');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_estoque_nao_negativo_update
            BEFORE UPDATE OF quantidade ON produtos
            WHEN NEW.quantidade < 0
              AND (NOT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='config_nota')
                   OR COALESCE((SELECT permitir_estoque_negativo FROM config_nota LIMIT 1), 0) = 0)
            BEGIN
                SELECT RAISE(ABORT, 'ESTOQUE_NEGATIVO: Estoque não pode ficar negativo.');
            END
            """
        )

    if _table_exists(cursor, "vendas"):
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_venda_total_positivo_insert
            BEFORE INSERT ON vendas
            WHEN COALESCE(NEW.valor_total, 0) <= 0
              OR (COALESCE(NEW.valor_total, 0) - COALESCE(NEW.desconto, 0)) <= 0
            BEGIN
                SELECT RAISE(ABORT, 'VENDA_INVALIDA: Valor total da venda deve ser maior que zero.');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_venda_total_positivo_update
            BEFORE UPDATE OF valor_total, desconto ON vendas
            WHEN COALESCE(NEW.valor_total, 0) <= 0
              OR (COALESCE(NEW.valor_total, 0) - COALESCE(NEW.desconto, 0)) <= 0
            BEGIN
                SELECT RAISE(ABORT, 'VENDA_INVALIDA: Valor total da venda deve ser maior que zero.');
            END
            """
        )


def migration_004_pdv_rascunho_e_vendas_suspensas(cursor) -> None:
    """
    Migração 4: Tabelas pdv_rascunho (recuperação de carrinho pós-crash)
    e vendas_suspensas (vendas suspensas F6 / orçamentos).
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS pdv_rascunho (
            usuario_id INTEGER PRIMARY KEY,
            carrinho_json TEXT NOT NULL,
            cliente TEXT DEFAULT 'Consumidor Final',
            desconto TEXT DEFAULT '0',
            tipo_desconto TEXT DEFAULT '%',
            observacao TEXT DEFAULT '',
            atualizado_em TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vendas_suspensas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT NOT NULL UNIQUE,
            tipo TEXT NOT NULL DEFAULT 'suspensa',
            cliente TEXT DEFAULT 'Consumidor Final',
            contato_cliente TEXT DEFAULT '',
            vendedor TEXT DEFAULT '',
            itens_json TEXT NOT NULL,
            subtotal REAL NOT NULL DEFAULT 0.0,
            desconto REAL NOT NULL DEFAULT 0.0,
            total REAL NOT NULL DEFAULT 0.0,
            observacao TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Ativa',
            usuario_id INTEGER DEFAULT NULL,
            loja_id INTEGER DEFAULT NULL,
            criado_em TEXT NOT NULL,
            convertido_em TEXT DEFAULT NULL,
            venda_transacao_id TEXT DEFAULT NULL
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_vendas_suspensas_status ON vendas_suspensas(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_vendas_suspensas_cliente ON vendas_suspensas(cliente)")


def migration_005_outbox_e_rotinas(conn: sqlite3.Connection) -> None:
    """Migração 5: Fila de saída offline (outbox) e agendador de rotinas."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            payload TEXT NOT NULL,
            tentativas INTEGER NOT NULL DEFAULT 0,
            max_tentativas INTEGER NOT NULL DEFAULT 5,
            proximo_envio TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            status TEXT NOT NULL DEFAULT 'pendente',
            erro_ultimo TEXT DEFAULT '',
            chave_idempotencia TEXT DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status_prox ON outbox(status, proximo_envio)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_idempotencia ON outbox(chave_idempotencia)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rotinas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL UNIQUE,
            hora TEXT NOT NULL DEFAULT '19:00',
            ultimo_run TEXT DEFAULT '',
            ativo INTEGER NOT NULL DEFAULT 1,
            config_json TEXT DEFAULT '{}'
        )
        """
    )

    rotinas_padrao = [
        ("resumo_diario_caixa", "21:00", "", 1, "{}"),
        ("cobranca_fiado_vencido", "09:30", "", 1, '{"dias": [1, 7, 15]}'),
        ("alerta_lotes_vencendo", "08:30", "", 1, '{"dias_antecedencia": 7}'),
    ]
    for tipo, hora, ultimo_run, ativo, cfg in rotinas_padrao:
        conn.execute(
            """
            INSERT OR IGNORE INTO rotinas (tipo, hora, ultimo_run, ativo, config_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tipo, hora, ultimo_run, ativo, cfg),
        )


def migration_006_inventario_contagem_cega(conn: sqlite3.Connection) -> None:
    """Migração 6: Tabelas de inventário e contagem cega por código de barras."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_inicio TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            data_fim TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'aberto',
            usuario_id INTEGER,
            supervisor_id INTEGER,
            motivo_ajuste TEXT DEFAULT '',
            observacoes TEXT DEFAULT '',
            loja_id INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventario_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventario_id INTEGER NOT NULL,
            produto_id INTEGER NOT NULL,
            codigo_barras TEXT DEFAULT '',
            nome_produto TEXT DEFAULT '',
            qtd_sistema REAL NOT NULL DEFAULT 0,
            qtd_contada REAL NOT NULL DEFAULT 0,
            divergencia REAL NOT NULL DEFAULT 0,
            custo_unitario REAL NOT NULL DEFAULT 0,
            contado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(inventario_id, produto_id),
            FOREIGN KEY (inventario_id) REFERENCES inventarios(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_itens_inv ON inventario_itens(inventario_id)")


def migration_007_compras_e_nfe_log(conn: sqlite3.Connection) -> None:
    """Migração 7: Lead time em fornecedores/produtos e registro mensal de NF-e XML importadas."""
    if _table_exists(conn, "fornecedores"):
        _add_column_if_missing(conn, "fornecedores", "lead_time_dias", "INTEGER DEFAULT 7")
    if _table_exists(conn, "produtos"):
        _add_column_if_missing(conn, "produtos", "fornecedor_id", "INTEGER")
        _add_column_if_missing(conn, "produtos", "lead_time_dias", "INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nfe_importadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_nfe TEXT DEFAULT '',
            chave_acesso TEXT DEFAULT '',
            fornecedor TEXT DEFAULT '',
            valor_total REAL DEFAULT 0,
            xml_conteudo TEXT NOT NULL,
            arquivo_nome TEXT DEFAULT '',
            usuario_id INTEGER,
            importado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nfe_importadas_data ON nfe_importadas(importado_em)")


def migration_008_conciliacao_pix_e_point(conn: sqlite3.Connection) -> None:
    """Migração 8: Tabela pagamentos_mp para conciliação PIX/Point e device_id do Pinpad em config_nota."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pagamentos_mp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL UNIQUE,
            tipo TEXT NOT NULL DEFAULT 'pix',
            external_reference TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            status_detail TEXT DEFAULT '',
            valor REAL NOT NULL DEFAULT 0,
            point_intent_id TEXT DEFAULT '',
            device_id TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            data_aprovacao TEXT DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pag_mp_ext_ref ON pagamentos_mp(external_reference)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pag_mp_tipo_status ON pagamentos_mp(tipo, status, criado_em)")
    if _table_exists(conn, "config_nota"):
        _add_column_if_missing(conn, "config_nota", "mp_point_device_id", "TEXT DEFAULT ''")


def migration_009_fiscal_nfce(conn: sqlite3.Connection) -> None:
    """Migração 9: Campos fiscais NFC-e em produtos/config_nota e tabela nfce_documentos."""
    if _table_exists(conn, "produtos"):
        for col, defn in (
            ("ncm", "TEXT DEFAULT '00000000'"),
            ("cfop", "TEXT DEFAULT '5102'"),
            ("cst_csosn", "TEXT DEFAULT '102'"),
            ("origem", "TEXT DEFAULT '0'"),
        ):
            _add_column_if_missing(conn, "produtos", col, defn)

    if _table_exists(conn, "config_nota"):
        for col, defn in (
            ("csc", "TEXT DEFAULT ''"),
            ("id_csc", "TEXT DEFAULT ''"),
            ("token_api", "TEXT DEFAULT ''"),
            ("ambiente", "TEXT DEFAULT 'homologacao'"),
            ("provedor_fiscal", "TEXT DEFAULT 'focusnfe'"),
            ("emitir_nfce_automatico", "INTEGER DEFAULT 0"),
            ("empresa_ie", "TEXT DEFAULT ''"),
            ("regime_tributario", "TEXT DEFAULT '1'"),
        ):
            _add_column_if_missing(conn, "config_nota", col, defn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nfce_documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transacao_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pendente_emissao',
            ambiente TEXT NOT NULL DEFAULT 'homologacao',
            chave_acesso TEXT DEFAULT '',
            numero TEXT DEFAULT '',
            serie TEXT DEFAULT '',
            protocolo TEXT DEFAULT '',
            qrcode_url TEXT DEFAULT '',
            url_danfe TEXT DEFAULT '',
            xml_url TEXT DEFAULT '',
            mensagem_sefaz TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            autorizado_em TEXT DEFAULT NULL
        )
        """
    )
    _add_column_if_missing(conn, "nfce_documentos", "autorizado_em", "TEXT DEFAULT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nfce_status ON nfce_documentos(status)")


# Status terminais de OS (mesmo conjunto do RelatorioService, Lote 4b-r1). NULL e '' contam
# como concluído porque serviço avulso nasce com status_os padrão 'Concluído'.
STATUS_OS_TERMINAIS = ("Concluído", "Entregue", "Finalizado")


def migration_010_loja_contas_pagar_e_conclusao_servicos(cursor) -> None:
    """
    Migração 10 (Lotes H2 + H3):
    - contas_pagar.loja_id: coluna nova; linhas legadas (NULL) vão para a loja padrão
      (menor id ativo, senão menor id, senão 1).
    - servicos.data_conclusao: coluna nova; backfill só para OS em status terminal, usando a
      primeira transição terminal registrada em historico ('OS Status') e, na falta dela,
      servicos.data. OS abertas ficam NULL.
    Idempotente: colunas checadas via PRAGMA e backfills só tocam linhas NULL.
    """
    if _table_exists(cursor, "contas_pagar"):
        _add_column_if_missing(cursor, "contas_pagar", "loja_id", "INTEGER DEFAULT NULL")
        if _table_exists(cursor, "lojas"):
            loja_padrao_sql = (
                "COALESCE((SELECT MIN(id) FROM lojas WHERE COALESCE(ativa, 1) = 1), "
                "(SELECT MIN(id) FROM lojas), 1)"
            )
        else:
            loja_padrao_sql = "1"
        cursor.execute(f"UPDATE contas_pagar SET loja_id = {loja_padrao_sql} WHERE loja_id IS NULL")

    if _table_exists(cursor, "servicos"):
        _add_column_if_missing(cursor, "servicos", "data_conclusao", "TEXT DEFAULT NULL")
        terminais_sql = ", ".join("'" + st + "'" for st in STATUS_OS_TERMINAIS)
        if _table_exists(cursor, "historico"):
            detalhes_sql = ", ".join("'Serviço #' || s.id || ' -> " + st + "'" for st in STATUS_OS_TERMINAIS)
            origem_sql = (
                "COALESCE((SELECT MIN(h.data_hora) FROM historico h "
                f"WHERE h.acao = 'OS Status' AND h.detalhe IN ({detalhes_sql})), s.data)"
            )
        else:
            origem_sql = "s.data"
        cursor.execute(
            f"""UPDATE servicos AS s SET data_conclusao = {origem_sql}
                WHERE s.data_conclusao IS NULL
                  AND COALESCE(s.status_os, 'Concluído') IN ({terminais_sql}, '')"""
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_servicos_data_conclusao ON servicos(data_conclusao)")


MIGRATIONS = [
    (1, "4.3-baseline: Consolidação idempotente de schema e índices", migration_001_baseline_v43),
    (2, "4.4-auditoria: Colunas estruturadas e hash encadeado em historico", migration_002_auditoria_historico),
    (3, "4.4-antifraude: Triggers de integridade e configs de supervisor/estoque", migration_003_triggers_e_config_antifraude),
    (4, "4.4-pdv: Persistência de rascunho de carrinho e vendas suspensas/orçamentos", migration_004_pdv_rascunho_e_vendas_suspensas),
    (5, "4.5-automacao: Fila outbox offline e tabela de rotinas agendadas", migration_005_outbox_e_rotinas),
    (6, "4.5-inventario: Sessões e itens de inventário/contagem cega", migration_006_inventario_contagem_cega),
    (7, "4.5-gestao: Lead time de compras e repositório mensal de XMLs de NF-e", migration_007_compras_e_nfe_log),
    (8, "4.6-pagamentos: Persistência de webhooks/Point Mercado Pago e conciliação PIX", migration_008_conciliacao_pix_e_point),
    (9, "4.6-fiscal: Campos fiscais NFC-e Mod. 65 em produtos/config_nota e nfce_documentos", migration_009_fiscal_nfce),
    (10, "4.7-higiene: loja_id em contas_pagar e data_conclusao em servicos (H2+H3)", migration_010_loja_contas_pagar_e_conclusao_servicos),
]


def run_migrations(conn, migrations=None) -> int:
    """
    Executa todas as migrações pendentes acima de PRAGMA user_version.
    Cada migração roda protegida por SAVEPOINT, garantindo rollback em caso de erro.
    """
    migrations_list = sorted(migrations if migrations is not None else MIGRATIONS, key=lambda item: item[0])
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS db_version (
            versao TEXT PRIMARY KEY,
            data_migr TEXT DEFAULT CURRENT_TIMESTAMP,
            descricao TEXT
        )
        """
    )

    current_version = get_user_version(conn)
    for version, description, fn in migrations_list:
        if version <= current_version:
            continue

        savepoint_name = f"sp_mig_{int(version)}"
        logger.info("Aplicando migração v%s: %s", version, description)
        cursor.execute(f"SAVEPOINT {savepoint_name}")
        try:
            fn(cursor)
            cursor.execute(
                "INSERT OR REPLACE INTO db_version (versao, descricao) VALUES (?, ?)",
                (f"schema_v{int(version)}", description),
            )
            set_user_version(conn, int(version))
            cursor.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            conn.commit()
            current_version = int(version)
            logger.info("Migração v%s aplicada com sucesso.", version)
        except Exception:
            try:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                cursor.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except sqlite3.Error:
                pass
            logger.exception("Falha ao aplicar migração v%s. Rollback executado.", version)
            raise

    # Garantir coluna aditiva idempotente autorizado_em em bancos que já estavam na v9
    if _table_exists(cursor, "nfce_documentos"):
        if _add_column_if_missing(cursor, "nfce_documentos", "autorizado_em", "TEXT DEFAULT NULL"):
            conn.commit()

    # Garantir registro legado '4.3' se a tabela estiver vazia
    if cursor.execute("SELECT COUNT(*) FROM db_version").fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO db_version (versao, descricao) VALUES ('4.3', 'Abertura/Fechamento, Sangrias e Suprimentos')"
        )
        conn.commit()

    return get_user_version(conn)
