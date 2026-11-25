# -*- coding: utf-8 -*-
"""
Lote H1 (higiene): versão única e isolamento por loja em `resumo_caixa_atual()`.

100% offline: banco SQLite em diretório temporário, sem rede, TEF, SEFAZ ou impressora.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, RAIZ)

import database  # noqa: E402
import services.updater as updater  # noqa: E402


# ---------------------------------------------------------------------------
# 1) Versão: database.__version__ vem de services.updater.APP_VERSION
# ---------------------------------------------------------------------------

def test_database_version_igual_app_version():
    assert database.__version__ == updater.APP_VERSION


@pytest.mark.parametrize(
    "ordem",
    [
        "import database; import services.updater as u",
        "import services.updater as u; import database",
        "import config; import database; import services.updater as u",
    ],
    ids=["database_primeiro", "updater_primeiro", "config_primeiro"],
)
def test_import_limpo_sem_ciclo(ordem, tmp_path):
    """Import em processo novo (sys.modules limpo), em ordens diferentes, sem ImportError de ciclo."""
    codigo = (
        f"{ordem}\n"
        "import database, services.updater as u\n"
        "assert database.__version__ == u.APP_VERSION, (database.__version__, u.APP_VERSION)\n"
        "print('OK', database.__version__)\n"
    )
    env = dict(os.environ)
    env["ESTOKE_DATA_DIR"] = str(tmp_path)
    env["ESTOKE_LOAD_DOTENV"] = "0"
    env["PYTHONPATH"] = RAIZ
    proc = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == f"OK {updater.APP_VERSION}"


# ---------------------------------------------------------------------------
# 2) resumo_caixa_atual() filtra por loja_id
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    tmpdir = tempfile.mkdtemp(prefix="estoke_h1_")
    orig = database.get_app_data_path
    database.get_app_data_path = lambda *parts: os.path.join(tmpdir, *parts) if parts else tmpdir
    banco = database.Database(db_name="h1_test.db")
    try:
        yield banco
    finally:
        try:
            banco.conn.close()
        except Exception:
            pass
        database.get_app_data_path = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


def _caixa_id(db, usuario_id, loja_id):
    row = db.conn.execute(
        "SELECT id FROM caixa WHERE status = 'aberto' AND usuario_id = ? AND loja_id IS ?",
        (usuario_id, loja_id),
    ).fetchone()
    return row[0]


def _venda(db, caixa_id, loja_id, valor):
    db.conn.execute(
        "INSERT INTO vendas (valor_total, desconto, valor_dinheiro, data_venda, status, caixa_id, loja_id) "
        "VALUES (?, 0, ?, ?, 'Concluída', ?, ?)",
        (valor, valor, datetime.datetime.now().isoformat(), caixa_id, loja_id),
    )
    db.conn.commit()


def _caixa_direto(db, usuario_id, loja_id, valor_abertura):
    """Insere caixa aberto sem passar por abrir_caixa() (que só permite 1 aberto por usuário)."""
    cur = db.conn.execute(
        "INSERT INTO caixa (data_abertura, valor_abertura, observacao, status, usuario_id, loja_id) "
        "VALUES (?, ?, '', 'aberto', ?, ?)",
        (datetime.datetime.now().isoformat(), valor_abertura, usuario_id, loja_id),
    )
    db.conn.commit()
    return cur.lastrowid


def test_resumo_caixa_duas_lojas_sem_vazamento(db):
    loja2 = db.adicionar_loja("Loja 2")
    assert db.abrir_caixa(100, usuario_id=1, loja_id=1)[0]
    assert db.abrir_caixa(50, usuario_id=2, loja_id=loja2)[0]
    _venda(db, _caixa_id(db, 1, 1), 1, 10.0)
    _venda(db, _caixa_id(db, 2, loja2), loja2, 25.0)

    r1 = db.resumo_caixa_atual(loja_id=1)
    r2 = db.resumo_caixa_atual(loja_id=loja2)
    assert r1["total_vendas"] == pytest.approx(10.0)
    assert r1["valor_abertura"] == pytest.approx(100.0)
    assert r2["total_vendas"] == pytest.approx(25.0)
    assert r2["valor_abertura"] == pytest.approx(50.0)

    # Usuário + loja que não batem: nada (antes da correção, loja_id era ignorado)
    assert db.resumo_caixa_atual(usuario_id=1, loja_id=loja2) is None
    assert db.resumo_caixa_atual(usuario_id=2, loja_id=1) is None
    # Compatibilidade: sem loja_id continua filtrando só por usuário
    assert db.resumo_caixa_atual(usuario_id=2)["total_vendas"] == pytest.approx(25.0)


def test_resumo_caixa_mesmo_usuario_em_duas_lojas(db):
    """Mesmo usuario_id operando caixas em lojas diferentes.

    Dois caixas abertos ao mesmo tempo para o mesmo usuário são barrados em duas camadas:
    abrir_caixa() e o índice único parcial idx_caixa_aberto_unico (migrations.py). O cenário
    real é sequencial: fecha na loja 1, abre na loja 2. O resumo da loja 1 não pode enxergar o
    caixa da loja 2 (antes da correção, loja_id era ignorado e o caixa da loja 2 aparecia).
    """
    import sqlite3

    loja2 = db.adicionar_loja("Loja 2")
    assert db.abrir_caixa(100, usuario_id=1, loja_id=1)[0]
    _venda(db, _caixa_id(db, 1, 1), 1, 10.0)
    assert db.resumo_caixa_atual(usuario_id=1, loja_id=1)["total_vendas"] == pytest.approx(10.0)
    assert db.resumo_caixa_atual(usuario_id=1, loja_id=loja2) is None

    # Simultâneo é impossível: regra de negócio e constraint do banco
    assert db.abrir_caixa(70, usuario_id=1, loja_id=loja2)[0] is False
    with pytest.raises(sqlite3.IntegrityError):
        _caixa_direto(db, 1, loja2, 70)
    db.conn.rollback()

    # Sequencial: fecha na loja 1 e abre na loja 2
    db.conn.execute("UPDATE caixa SET status = 'fechado' WHERE usuario_id = 1 AND status = 'aberto'")
    db.conn.commit()
    assert db.abrir_caixa(70, usuario_id=1, loja_id=loja2)[0]
    _venda(db, _caixa_id(db, 1, loja2), loja2, 33.0)

    assert db.resumo_caixa_atual(usuario_id=1, loja_id=1) is None
    r2 = db.resumo_caixa_atual(usuario_id=1, loja_id=loja2)
    assert r2["total_vendas"] == pytest.approx(33.0)
    assert r2["valor_abertura"] == pytest.approx(70.0)


def test_resumo_caixa_legado_sem_loja_continua_visivel(db):
    """Caixa legado (loja_id NULL, antes do multi-loja) aparece em qualquer loja, como em caixa_aberto()."""
    cx = _caixa_direto(db, 1, None, 20)
    _venda(db, cx, None, 5.0)
    assert db.resumo_caixa_atual(usuario_id=1, loja_id=1)["total_vendas"] == pytest.approx(5.0)
    assert db.resumo_caixa_atual(usuario_id=1, loja_id=99)["total_vendas"] == pytest.approx(5.0)
    assert db.caixa_aberto(usuario_id=1, loja_id=99)["id"] == cx
