# -*- coding: utf-8 -*-
"""
Lotes H2 + H3 (migração v10 do schema):
- H2: contas_pagar.loja_id (backfill, CRUD por loja, pagar_conta lança o gasto na loja da conta).
- H3: servicos.data_conclusao (backfill por historico, auto-set no 1º status terminal) e
  DRE/comissões pela data de conclusão.

100% offline: banco SQLite em diretório temporário, sem rede, TEF, SEFAZ ou impressora.
"""
from __future__ import annotations

import datetime as _real_dt
import os
import shutil
import tempfile
import types

import pytest

import database
import migrations
from services.financeiro_service import FinanceiroService


def _fake_datetime(dia: _real_dt.date, hora=(12, 0, 0)):
    momento = _real_dt.datetime(dia.year, dia.month, dia.day, *hora)

    class FakeDate(_real_dt.date):
        @classmethod
        def today(cls):
            return dia

    class FakeDateTime(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return momento if tz is None else momento.replace(tzinfo=tz)

        @classmethod
        def today(cls):
            return momento

    mod = types.ModuleType("datetime")
    for attr in dir(_real_dt):
        setattr(mod, attr, getattr(_real_dt, attr))
    mod.date = FakeDate
    mod.datetime = FakeDateTime
    return mod


@pytest.fixture
def db():
    tmpdir = tempfile.mkdtemp(prefix="estoke_h2h3_")
    orig = database.get_app_data_path
    database.get_app_data_path = lambda *parts: os.path.join(tmpdir, *parts) if parts else tmpdir
    banco = database.Database(db_name="h2h3_test.db")
    try:
        yield banco
    finally:
        try:
            banco.conn.close()
        except Exception:
            pass
        database.get_app_data_path = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


def _colunas(conn, tabela):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})").fetchall()}


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ---------------------------------------------------------------------------
# Schema / migração
# ---------------------------------------------------------------------------

def test_schema_novo_ja_nasce_v10(db):
    assert migrations.LATEST_SCHEMA_VERSION == 10
    assert _user_version(db.conn) == 10
    assert "loja_id" in _colunas(db.conn, "contas_pagar")
    assert "data_conclusao" in _colunas(db.conn, "servicos")
    idx = {r[1] for r in db.conn.execute("PRAGMA index_list(servicos)").fetchall()}
    assert "idx_servicos_data_conclusao" in idx


def _rebaixar_para_v9(conn):
    """Remove as colunas da v10 e volta user_version para 9 (banco 'como era' antes da v10)."""
    conn.execute("DROP INDEX IF EXISTS idx_servicos_data_conclusao")
    conn.execute("ALTER TABLE contas_pagar DROP COLUMN loja_id")
    conn.execute("ALTER TABLE servicos DROP COLUMN data_conclusao")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    assert "loja_id" not in _colunas(conn, "contas_pagar")
    assert "data_conclusao" not in _colunas(conn, "servicos")


def test_migracao_v9_para_v10_backfill_e_idempotencia(db):
    conn = db.conn
    _rebaixar_para_v9(conn)

    # Dados legados v9
    conn.execute(
        "INSERT INTO contas_pagar (descricao, valor, data_emissao, data_vencimento) "
        "VALUES ('Aluguel', 1000, '2026-01-01', '2026-01-10')"
    )
    conn.execute(
        "INSERT INTO contas_pagar (descricao, valor, data_emissao, data_vencimento) "
        "VALUES ('Luz', 200, '2026-01-01', '2026-01-15')"
    )
    ins_serv = (
        "INSERT INTO servicos (data, cliente, descricao, valor, custo, status, status_os) "
        "VALUES (?, 'Cli', ?, 100, 10, 'Concluído', ?)"
    )
    sid_hist = conn.execute(ins_serv, ("2026-01-05T10:00:00", "OS com histórico", "Entregue")).lastrowid
    sid_sem_hist = conn.execute(ins_serv, ("2026-01-06T09:00:00", "OS sem histórico", "Concluído")).lastrowid
    sid_aberta = conn.execute(ins_serv, ("2026-01-07T09:00:00", "OS aberta", "Em andamento")).lastrowid
    sid_vazio = conn.execute(ins_serv, ("2026-01-08T09:00:00", "OS status vazio", "")).lastrowid
    conn.commit()
    # Histórico: entrou em Concluído em 20/02 e depois Entregue em 25/02 -> vale a 1ª transição terminal
    db.registrar_alteracao("OS Status", f"Serviço #{sid_hist} -> Em andamento")
    db.registrar_alteracao("OS Status", f"Serviço #{sid_hist} -> Concluído")
    db.registrar_alteracao("OS Status", f"Serviço #{sid_hist} -> Entregue")
    conn.execute(
        "UPDATE historico SET data_hora = CASE detalhe "
        "WHEN ? THEN '2026-02-10 08:00:00' WHEN ? THEN '2026-02-20 15:30:00' WHEN ? THEN '2026-02-25 09:00:00' END "
        "WHERE acao = 'OS Status' AND detalhe IN (?, ?, ?)",
        tuple([f"Serviço #{sid_hist} -> {s}" for s in ("Em andamento", "Concluído", "Entregue")] * 2),
    )
    conn.commit()

    assert migrations.run_migrations(conn) == 10
    assert _user_version(conn) == 10

    # H2: legado vai para a loja padrão (Loja Principal = menor id ativo)
    loja_padrao = db._loja_padrao_id()
    lojas_cp = {r[0] for r in conn.execute("SELECT loja_id FROM contas_pagar").fetchall()}
    assert lojas_cp == {loja_padrao}

    # H3: backfill só de status terminal
    dc = dict(conn.execute("SELECT id, data_conclusao FROM servicos").fetchall())
    assert dc[sid_hist] == "2026-02-20 15:30:00"       # 1ª transição terminal do historico
    assert dc[sid_sem_hist] == "2026-01-06T09:00:00"   # fallback: servicos.data
    assert dc[sid_vazio] == "2026-01-08T09:00:00"      # '' conta como concluído (RelatorioService)
    assert dc[sid_aberta] is None                       # OS aberta fica NULL

    # Idempotência: rodar de novo (inclusive forçando a função da v10) não muda nada
    antes = conn.execute("SELECT id, loja_id FROM contas_pagar ORDER BY id").fetchall(), \
        conn.execute("SELECT id, data_conclusao FROM servicos ORDER BY id").fetchall()
    assert migrations.run_migrations(conn) == 10
    migrations.migration_010_loja_contas_pagar_e_conclusao_servicos(conn.cursor())
    conn.commit()
    depois = conn.execute("SELECT id, loja_id FROM contas_pagar ORDER BY id").fetchall(), \
        conn.execute("SELECT id, data_conclusao FROM servicos ORDER BY id").fetchall()
    assert antes == depois


def test_backfill_contas_pagar_sem_lojas_usa_1():
    """Banco v9 mínimo sem nenhuma loja cadastrada: backfill cai no id 1 (Loja Principal é criada depois)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE contas_pagar (id INTEGER PRIMARY KEY, descricao TEXT, valor REAL, "
        "data_emissao TEXT, data_vencimento TEXT, status TEXT DEFAULT 'Pendente')"
    )
    conn.execute("CREATE TABLE lojas (id INTEGER PRIMARY KEY, nome TEXT, ativa INTEGER DEFAULT 1)")
    conn.execute("INSERT INTO contas_pagar (descricao, valor, data_emissao, data_vencimento) VALUES ('x', 1, 'a', 'b')")
    migrations.migration_010_loja_contas_pagar_e_conclusao_servicos(conn.cursor())
    assert conn.execute("SELECT loja_id FROM contas_pagar").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# H2: contas_pagar por loja
# ---------------------------------------------------------------------------

def test_contas_pagar_crud_por_loja(db):
    loja1 = db._loja_padrao_id()
    loja2 = db.adicionar_loja("Loja 2")
    db.adicionar_conta_pagar("Aluguel L1", 1000.0, "2026-12-10", loja_id=loja1)
    db.adicionar_conta_pagar("Aluguel L2", 800.0, "2026-12-10", loja_id=loja2)
    db.adicionar_conta_pagar("Sem loja informada", 50.0, "2026-12-10")  # compatível: vai p/ loja padrão
    db.conn.execute(
        "INSERT INTO contas_pagar (descricao, valor, data_emissao, data_vencimento, loja_id) "
        "VALUES ('Legada NULL', 10, '2026-01-01', '2026-12-10', NULL)"
    )
    db.conn.commit()

    assert db.conn.execute(
        "SELECT loja_id FROM contas_pagar WHERE descricao='Sem loja informada'"
    ).fetchone()[0] == loja1

    todas = set(db.listar_contas_pagar()["descricao"])
    assert todas == {"Aluguel L1", "Aluguel L2", "Sem loja informada", "Legada NULL"}
    assert set(db.listar_contas_pagar(loja_id=loja1)["descricao"]) == {"Aluguel L1", "Sem loja informada", "Legada NULL"}
    assert set(db.listar_contas_pagar(loja_id=loja2)["descricao"]) == {"Aluguel L2", "Legada NULL"}

    assert db.total_contas_pagar_pendente() == pytest.approx(1860.0)
    assert db.total_contas_pagar_pendente(loja_id=loja2) == pytest.approx(810.0)

    svc = FinanceiroService(db)
    r2 = svc.listar_contas_pagar(status="Todos", loja_id=loja2)
    assert {c["descricao"] for c in r2["contas"]} == {"Aluguel L2", "Legada NULL"}
    assert r2["total_pendente"] == pytest.approx(810.0)
    svc.crud_contas_pagar("adicionar", descricao="Água L2", valor="30,50", data_vencimento="2026-12-20", loja_id=loja2)
    assert db.conn.execute("SELECT loja_id FROM contas_pagar WHERE descricao='Água L2'").fetchone()[0] == loja2


def test_pagar_conta_isolada_por_loja_sem_vazamento(db, monkeypatch):
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 11, 20)))
    loja1 = db._loja_padrao_id()
    loja2 = db.adicionar_loja("Loja 2")
    db.adicionar_conta_pagar("Fornecedor L2", 333.33, "2026-11-25", loja_id=loja2)
    conta_id = db.conn.execute("SELECT id FROM contas_pagar WHERE descricao='Fornecedor L2'").fetchone()[0]

    dre1_antes = db.gerar_dre("mes", loja_id=loja1)
    # Mesmo chamando com o loja_id da Loja 1, o gasto vai para a loja DA CONTA (Loja 2)
    FinanceiroService(db).pagar_conta(conta_id, loja_id=loja1)

    gastos = db.conn.execute("SELECT loja_id, valor FROM gastos WHERE descricao LIKE 'Pgto: Fornecedor L2%'").fetchall()
    assert gastos == [(loja2, pytest.approx(333.33))]

    dre1 = db.gerar_dre("mes", loja_id=loja1)
    dre2 = db.gerar_dre("mes", loja_id=loja2)
    assert dre1["despesas_variaveis"] == dre1_antes["despesas_variaveis"]
    assert dre1["contas_pagas"] == 0.0
    assert dre1["resultado_liquido"] == dre1_antes["resultado_liquido"]
    assert dre2["despesas_variaveis"] == pytest.approx(333.33)
    assert dre2["contas_pagas"] == pytest.approx(333.33)
    assert dre2["resultado_liquido"] == pytest.approx(-333.33)


def test_pagar_conta_legada_sem_loja_usa_fallback(db, monkeypatch):
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 11, 20)))
    loja2 = db.adicionar_loja("Loja 2")
    db.conn.execute(
        "INSERT INTO contas_pagar (descricao, valor, data_emissao, data_vencimento, loja_id) "
        "VALUES ('Legada', 40, '2026-11-01', '2026-11-10', NULL)"
    )
    db.conn.commit()
    cid = db.conn.execute("SELECT id FROM contas_pagar WHERE descricao='Legada'").fetchone()[0]
    db.pagar_conta(cid, loja_id=loja2)
    assert db.conn.execute("SELECT loja_id FROM gastos WHERE descricao='Pgto: Legada'").fetchone()[0] == loja2


# ---------------------------------------------------------------------------
# H3: data_conclusao
# ---------------------------------------------------------------------------

def _data_conclusao(db, sid):
    return db.conn.execute("SELECT data_conclusao FROM servicos WHERE id=?", (sid,)).fetchone()[0]


def test_data_conclusao_auto_set_primeira_entrada_terminal(db, monkeypatch):
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 3, 1)))
    ok, sid = db.registrar_servico("Cli", "Reparo", 100, status_os="Aberto")
    assert ok and _data_conclusao(db, sid) is None

    db.atualizar_status_servico(sid, "Em andamento")
    assert _data_conclusao(db, sid) is None

    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 3, 5)))
    db.atualizar_status_servico(sid, "Concluído")
    assert _data_conclusao(db, sid).startswith("2026-03-05")

    # Concluído -> Entregue não sobrescreve
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 3, 9)))
    db.atualizar_status_servico(sid, "Entregue")
    assert _data_conclusao(db, sid).startswith("2026-03-05")

    # Sem regra de reabertura no domínio: voltar a não terminal não limpa a data
    db.atualizar_status_servico(sid, "Em andamento")
    assert _data_conclusao(db, sid).startswith("2026-03-05")


def test_servico_avulso_ja_nasce_concluido(db, monkeypatch):
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 4, 2)))
    ok, sid = db.registrar_servico("Cli", "Avulso", 50)  # status_os padrão 'Concluído'
    assert ok
    assert _data_conclusao(db, sid).startswith("2026-04-02")


def _comissao_servicos(res, vendedor):
    df = res["servicos"]
    linha = df[df["vendedor"] == vendedor]
    return 0.0 if linha.empty else float(linha["faturamento_servicos"].iloc[0])


def test_os_aberta_em_janeiro_concluida_em_fevereiro_conta_em_fevereiro(db, monkeypatch):
    loja1 = db._loja_padrao_id()

    # Janeiro: OS aberta
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 1, 20)))
    ok, sid = db.registrar_servico("Cli", "Notebook", 500, custo=120, vendedor="Ana",
                                   status_os="Aberto", loja_id=loja1)
    assert ok

    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 1, 31)))
    dre_jan = db.gerar_dre("mes", loja_id=loja1)
    assert dre_jan["receita_servicos"] == 0.0
    assert dre_jan["custo_servicos"] == 0.0
    assert _comissao_servicos(db.calcular_comissoes("mes", loja_id=loja1), "Ana") == 0.0

    # Fevereiro: concluída
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 2, 10)))
    db.atualizar_status_servico(sid, "Concluído")

    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 2, 28)))
    dre_fev = db.gerar_dre("mes", loja_id=loja1)
    assert dre_fev["receita_servicos"] == pytest.approx(500.0)
    assert dre_fev["custo_servicos"] == pytest.approx(120.0)
    assert _comissao_servicos(db.calcular_comissoes("mes", loja_id=loja1), "Ana") == pytest.approx(500.0)

    # Reconsultando janeiro depois da conclusão: continua fora de janeiro
    monkeypatch.setattr(database, "datetime", _fake_datetime(_real_dt.date(2026, 1, 31)))
    dre_jan2 = db.gerar_dre("mes", loja_id=loja1)
    assert dre_jan2["receita_servicos"] == 0.0
    assert dre_jan2["custo_servicos"] == 0.0
