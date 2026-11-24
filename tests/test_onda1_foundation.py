"""
Testes unitários e de integração da ONDA 1:
- migrations.py (PRAGMA user_version, idempotência, bancos legados e rollback de savepoint)
- _tem_permissao fail-closed (quando current_user é None/inválido)
- Backup automático com integrity_check e retenção GFS (7 diários / 4 semanais / 12 mensais)
- Proteção Fase 1 ACL NTFS via icacls em arquivos .db / .db_key
- crash_handler (log estruturado JSON e faulthandler)
- Migração de ai_module.py para google-genai (sem google.generativeai)
"""
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ai_module
import crash_handler
import database
import migrations


class TestMigrations:
    """Valida versionamento com PRAGMA user_version e upgrade de bancos legados."""

    def test_migrations_fresh_and_idempotent(self):
        tmpdir = tempfile.mkdtemp(prefix="estoke_mig_fresh_")
        orig_app_data = database.get_app_data_path
        database.get_app_data_path = lambda: tmpdir
        db = None
        try:
            db = database.Database(db_name="fresh.db")
            ver = migrations.get_user_version(db.conn)
            assert ver == migrations.LATEST_SCHEMA_VERSION
            assert ver >= 1

            # Verificar colunas essenciais adicionadas pela migração baseline
            cols_vendas = {r[1] for r in db.conn.execute("PRAGMA table_info(vendas)").fetchall()}
            assert {"forma_pagamento", "transacao_id", "loja_id", "caixa_id"}.issubset(cols_vendas)

            cols_clientes = {r[1] for r in db.conn.execute("PRAGMA table_info(clientes)").fetchall()}
            assert "cpf_cnpj" in cols_clientes

            # Idempotência: rodar novamente não deve falhar nem alterar user_version
            ver2 = migrations.run_migrations(db.conn)
            assert ver2 == ver
        finally:
            if db is not None:
                db.close()
            database.get_app_data_path = orig_app_data
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_migrations_legacy_upgrade(self):
        tmpdir = tempfile.mkdtemp(prefix="estoke_mig_legacy_")
        orig_app_data = database.get_app_data_path
        database.get_app_data_path = lambda: tmpdir
        db = None
        try:
            legacy_path = os.path.join(tmpdir, "legacy.db")
            conn = sqlite3.connect(legacy_path)
            conn.execute("PRAGMA user_version = 0")
            # Criar tabelas no formato antigo v1 sem as colunas novas e sem loja_id
            conn.execute(
                "CREATE TABLE produtos (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, quantidade INTEGER NOT NULL, preco REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE vendas (id INTEGER PRIMARY KEY AUTOINCREMENT, produto_id INTEGER, quantidade INTEGER, valor_total REAL, data_venda TEXT, lucro REAL)"
            )
            conn.execute(
                "CREATE TABLE clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, contato TEXT, observacoes TEXT)"
            )
            conn.execute(
                "CREATE TABLE gastos (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL, valor REAL NOT NULL, categoria TEXT NOT NULL, descricao TEXT)"
            )
            conn.execute(
                "CREATE TABLE estabelecimento (id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, detalhe TEXT)"
            )
            conn.execute("INSERT INTO produtos (nome, quantidade, preco) VALUES ('Camiseta Legada', 12, 45.0)")
            conn.execute("INSERT INTO clientes (nome, contato) VALUES ('Cliente Antigo', '11999990000')")
            conn.execute("INSERT INTO estabelecimento (item, detalhe) VALUES ('Salão', 'Limpar balcão')")
            conn.commit()
            conn.close()

            # Abrir usando Database() -> deve aplicar migration_001_baseline_v43 sem quebrar e preservar dados
            db = database.Database(db_name="legacy.db")
            assert migrations.get_user_version(db.conn) == migrations.LATEST_SCHEMA_VERSION

            cols_prods = {r[1] for r in db.conn.execute("PRAGMA table_info(produtos)").fetchall()}
            assert {"estoque_minimo", "loja_id", "lote_serie", "unidade_medida"}.issubset(cols_prods)

            cols_estab = {r[1] for r in db.conn.execute("PRAGMA table_info(estabelecimento)").fetchall()}
            assert {"area_local", "tarefa", "status", "prioridade"}.issubset(cols_estab)

            prod_row = db.conn.execute("SELECT nome, quantidade, preco FROM produtos").fetchone()
            assert prod_row == ("Camiseta Legada", 12, 45.0)

            cli_row = db.conn.execute("SELECT nome, cpf_cnpj FROM clientes").fetchone()
            assert cli_row == ("Cliente Antigo", "")

            estab_row = db.conn.execute("SELECT area_local, tarefa FROM estabelecimento").fetchone()
            assert estab_row == ("Salão", "Limpar balcão")
        finally:
            if db is not None:
                db.close()
            database.get_app_data_path = orig_app_data
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_migrations_custom_step_and_rollback(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, nome TEXT)")
        conn.commit()

        def mig_1(cur):
            cur.execute("ALTER TABLE demo ADD COLUMN tag TEXT DEFAULT 'ok'")

        def mig_2_failing(cur):
            cur.execute("ALTER TABLE demo ADD COLUMN deve_reverter INTEGER DEFAULT 0")
            raise RuntimeError("Erro simulado na migração 2")

        ver = migrations.run_migrations(conn, migrations=[(1, "add tag", mig_1)])
        assert ver == 1

        with pytest.raises(RuntimeError, match="Erro simulado na migração 2"):
            migrations.run_migrations(
                conn,
                migrations=[
                    (1, "add tag", mig_1),
                    (2, "broken step", mig_2_failing),
                ],
            )

        # Savepoint deve ter revertido a versão 2 e a coluna parcial
        assert migrations.get_user_version(conn) == 1
        cols = {r[1] for r in conn.execute("PRAGMA table_info(demo)").fetchall()}
        assert "deve_reverter" not in cols
        conn.close()


class TestPermissionsFailClosed:
    """Garante que _tem_permissao é fail-closed quando current_user é None ou inválido."""

    def test_tem_permissao_fail_closed(self):
        import main as app_main

        class _DbStub:
            def usuario_tem_permissao(self, perfil, acao):
                if perfil == "admin":
                    return True
                if perfil == "vendedor":
                    return acao == "vender"
                return False

        app_stub = app_main.App.__new__(app_main.App)
        app_stub.db = _DbStub()

        # Quando current_user é None ou vazio: negar TODAS as ações
        for acao in ("VENDER", "ADICIONAR", "CONFIG", "RELATORIO", "CAIXA", "QUALQUER"):
            app_stub.current_user = None
            assert app_main.App._tem_permissao(app_stub, acao) is False

            app_stub.current_user = {}
            assert app_main.App._tem_permissao(app_stub, acao) is False

            app_stub.current_user = {"nome": "X", "perfil": ""}
            assert app_main.App._tem_permissao(app_stub, acao) is False

        # Vendedor: apenas ações permitidas
        app_stub.current_user = {"id": 2, "nome": "João", "perfil": "vendedor"}
        assert app_main.App._tem_permissao(app_stub, "VENDER") is True
        assert app_main.App._tem_permissao(app_stub, "CONFIG") is False

        # Admin: acesso total
        app_stub.current_user = {"id": 1, "nome": "Admin", "perfil": "admin"}
        assert app_main.App._tem_permissao(app_stub, "CONFIG") is True


class TestBackupGFSAndIntegrity:
    """Testa verificação de integridade do backup e política de retenção 7d/4s/12m."""

    def test_backup_integrity_and_gfs_retention(self):
        tmpdir = tempfile.mkdtemp(prefix="estoke_bk_gfs_")
        orig_app_data = database.get_app_data_path
        database.get_app_data_path = lambda: tmpdir
        db = None
        try:
            db = database.Database(db_name="bk.db")
            db.adicionar_produto("Item Backup", 5, 10.0, 20.0, "Geral")

            # Testar criação real de backup com integrity_check e criptografia
            ok, bk_path = db.criar_backup()
            assert ok is True
            assert bk_path.endswith(".db.enc")
            assert os.path.exists(bk_path) and os.path.getsize(bk_path) > 0

            # Simular múltiplos arquivos de backup em backup_dir isolado:
            # 12 backups diários + 6 semanais anteriores + 15 mensais anteriores
            gfs_dir = os.path.join(tmpdir, "gfs_test")
            os.makedirs(gfs_dir, exist_ok=True)
            base_dt = datetime.datetime(2026, 11, 21, 18, 0, 0)

            daily_expected = set()
            # 12 dias consecutivos (2 snapshots no dia 0 para testar que só mantém o mais recente do dia)
            for d_off in range(12):
                dt = base_dt - datetime.timedelta(days=d_off)
                fname = f"estoque_backup_{dt.strftime('%Y%m%d_%H%M%S')}_000001.db.enc"
                with open(os.path.join(gfs_dir, fname), "wb") as f:
                    f.write(b"dummy")
                if d_off < 7:
                    daily_expected.add(fname)
            # Extra no mesmo dia 0 mais cedo (deve ser removido)
            dt_old_today = base_dt - datetime.timedelta(hours=5)
            with open(os.path.join(gfs_dir, f"estoque_backup_{dt_old_today.strftime('%Y%m%d_%H%M%S')}_000001.db.enc"), "wb") as f:
                f.write(b"dummy")

            # 6 backups semanais anteriores (semanas distintas antes dos 12 dias)
            cur = base_dt - datetime.timedelta(days=20)
            weekly_candidates = []
            seen_weeks = {((base_dt - datetime.timedelta(days=i)).isocalendar()[:2]) for i in range(12)}
            while len(weekly_candidates) < 6:
                wk = cur.isocalendar()[:2]
                if wk not in seen_weeks:
                    seen_weeks.add(wk)
                    fname = f"estoque_backup_{cur.strftime('%Y%m%d_%H%M%S')}_000002.db.enc"
                    with open(os.path.join(gfs_dir, fname), "wb") as f:
                        f.write(b"dummy")
                    weekly_candidates.append(fname)
                cur -= datetime.timedelta(days=7)

            # 15 backups mensais anteriores (meses distintos anteriores)
            monthly_candidates = []
            y, m = 2026, 9
            while len(monthly_candidates) < 15:
                dt_m = datetime.datetime(y, m, 15, 12, 0, 0)
                ym = (dt_m.year, dt_m.month)
                existing_months = {
                    (datetime.datetime.strptime(n[15:23], "%Y%m%d").year, datetime.datetime.strptime(n[15:23], "%Y%m%d").month)
                    for n in (list(daily_expected) + weekly_candidates)
                }
                if ym not in existing_months:
                    fname = f"estoque_backup_{dt_m.strftime('%Y%m%d_%H%M%S')}_000003.db.enc"
                    with open(os.path.join(gfs_dir, fname), "wb") as f:
                        f.write(b"dummy")
                    monthly_candidates.append(fname)
                m -= 1
                if m == 0:
                    m = 12
                    y -= 1

            res = db.aplicar_retencao_backups(gfs_dir, diarios=7, semanais=4, mensais=12)
            mantidos = set(res["mantidos"])

            # 7 diários mais recentes devem estar mantidos (sem duplicata extra do dia 0)
            assert daily_expected.issubset(mantidos)
            assert f"estoque_backup_{dt_old_today.strftime('%Y%m%d_%H%M%S')}_000001.db.enc" in res["removidos"]
            # Os 4 semanais distintos mais recentes fora da janela diária incluem 2026-11-14 (W46) e os 3 seguintes (Nov 1, Out 25, Out 18)
            assert set(weekly_candidates[:3]).issubset(mantidos)
            # Exatamente 7 + 4 + 12 = 23 backups mantidos na retenção GFS
            assert len(mantidos) == 23
            assert len(res["removidos"]) >= 1
        finally:
            if db is not None:
                db.close()
            database.get_app_data_path = orig_app_data
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSecurityAndCrashHandler:
    """Testa restrição de ACL NTFS no Windows e gravação de crash estruturado."""

    @pytest.mark.skipif(os.name != "nt", reason="Teste específico de ACL NTFS do Windows")
    def test_ntfs_acl_restriction(self):
        tmpdir = tempfile.mkdtemp(prefix="estoke_acl_")
        try:
            secret_file = os.path.join(tmpdir, "segredo.db")
            with open(secret_file, "wb") as f:
                f.write(b"sqlite_dummy")
            ok = database._restrict_file_acl_windows(secret_file)
            assert ok is True
            out = subprocess.check_output(["icacls", secret_file], text=True, stderr=subprocess.STDOUT)
            username = os.environ.get("USERNAME", "")
            assert username.lower() in out.lower()
            assert "(I)" not in out  # Herança removida (/inheritance:r)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_crash_handler_json_log(self):
        tmpdir = tempfile.mkdtemp(prefix="estoke_crash_")
        try:
            try:
                _ = 1 / 0
            except ZeroDivisionError:
                exc_type, exc_val, exc_tb = sys.exc_info()
                info = crash_handler.record_crash(
                    exc_type, exc_val, exc_tb,
                    context="pytest_simulated",
                    log_dir=tmpdir,
                )

            assert info["exc_type"] == "ZeroDivisionError"
            assert "crash_id" in info and len(info["crash_id"]) == 8
            assert os.path.exists(info["crash_file"])
            with open(info["crash_file"], "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data["context"] == "pytest_simulated"
            assert "ZeroDivisionError" in data["traceback"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestGoogleGenAIMigration:
    """Valida uso exclusivo de google-genai (sem FutureWarning legado)."""

    def test_google_genai_migration(self):
        assert ai_module.GEMINI_LIB_AVAILABLE is True
        assert ai_module.genai is not None
        assert hasattr(ai_module.genai, "Client")

        tmpdir = tempfile.mkdtemp(prefix="estoke_genai_")
        orig_app_data = database.get_app_data_path
        database.get_app_data_path = lambda: tmpdir
        db = None
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always", FutureWarning)
                db = database.Database(db_name="genai_test.db")
                # Simular chave fake válida em formato (>=10 chars sem espaço) para instanciar Client
                os.environ["GEMINI_API_KEY"] = "AIzaTestFakeKey123456"
                pred = ai_module.ExpensePredictor(db)
                assert pred.gemini_available is True
                assert pred.genai_client is not None
                future_msgs = [str(item.message) for item in w if "google.generativeai" in str(item.message)]
                assert not future_msgs
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            if db is not None:
                db.close()
            database.get_app_data_path = orig_app_data
            shutil.rmtree(tmpdir, ignore_errors=True)
