"""
F1b Lote 7 — kits/reposição (EstoqueService), produção (ProducaoService) e trocas (TrocaService).

Cobre validações dos serviços, atomicidade dos métodos do banco (_lock + rollback),
a regressão da troca (devolução não pode ficar pendente quando o produto novo falta),
dinheiro em Decimal e loja_id nos lançamentos financeiros da troca.
"""
import ast
import gc
import os
import sys
import tempfile
import time
from decimal import Decimal

import pytest

RAIZ = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, RAIZ)


@pytest.fixture
def db():
    tmpdir = tempfile.TemporaryDirectory()
    import database
    original = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir.name
    inst = database.Database(db_name="test_lote7.db")
    yield inst
    try:
        inst.close()
    except Exception:
        pass
    database.get_app_data_path = original
    gc.collect()
    for _ in range(10):
        try:
            tmpdir.cleanup()
            break
        except PermissionError:
            time.sleep(0.15)


def _produto(db, nome, qtd, preco=10.0, insumo=False, minimo=None):
    db.adicionar_produto(nome, qtd, preco, "Cat")
    cur = db.conn.cursor()
    pid = cur.execute("SELECT id FROM produtos WHERE nome=?", (nome,)).fetchone()[0]
    if insumo:
        cur.execute("UPDATE produtos SET tipo_produto='insumo' WHERE id=?", (pid,))
    if minimo is not None:
        cur.execute("UPDATE produtos SET estoque_minimo=? WHERE id=?", (minimo, pid))
    db.conn.commit()
    return pid


def _qtd(db, pid):
    return db.conn.execute("SELECT quantidade FROM produtos WHERE id=?", (pid,)).fetchone()[0]


# ==================== AST: serviços sem UI ====================

@pytest.mark.parametrize("arquivo", ["estoque_service.py", "producao_service.py", "troca_service.py"])
def test_servicos_lote7_sem_tkinter(arquivo):
    caminho = os.path.join(RAIZ, "services", arquivo)
    arvore = ast.parse(open(caminho, encoding="utf-8").read())
    proibidos = {"tkinter", "customtkinter", "tkcalendar"}
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes = [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            nomes = [no.module or ""]
        else:
            continue
        for n in nomes:
            assert n.split(".")[0] not in proibidos, f"{arquivo} importa {n}"


# ==================== EstoqueService: kits ====================

def test_parse_preco_decimal_half_up():
    from services.estoque_service import _parse_preco
    assert _parse_preco("10,005") == Decimal("10.01")
    assert _parse_preco("7") == Decimal("7.00")
    for ruim in ("-1", "abc", "nan", "inf", "", None):
        with pytest.raises(ValueError, match="Preço inválido"):
            _parse_preco(ruim)


def test_parse_qtd_componente():
    from services.estoque_service import EstoqueService
    assert EstoqueService.parse_qtd_componente("3") == 3
    for ruim in ("0", "-2", "x", "1.5"):
        with pytest.raises(ValueError):
            EstoqueService.parse_qtd_componente(ruim)


def test_criar_kit_validacoes_e_id(db):
    from services.estoque_service import EstoqueService
    svc = EstoqueService(db)
    itens = [{'nome': 'Caneca', 'quantidade': 2}]
    with pytest.raises(ValueError, match="Preço inválido"):
        svc.criar_kit("Kit", "abc", itens)
    with pytest.raises(ValueError, match="Defina um nome"):
        svc.criar_kit("  ", "10", itens)
    with pytest.raises(ValueError, match="pelo menos um produto"):
        svc.criar_kit("Kit", "10", [])

    kit_id = svc.criar_kit(" Kit Café ", "19,995", itens)
    assert isinstance(kit_id, int)
    nome, preco = db.conn.execute("SELECT nome, preco_venda FROM kits WHERE id=?", (kit_id,)).fetchone()
    assert nome == "Kit Café"
    assert preco == pytest.approx(20.00)
    assert db.get_kit_components(kit_id) == itens


# ==================== EstoqueService: reposição ====================

def test_itens_reposicao_e_texto_whatsapp(db):
    from services.estoque_service import EstoqueService
    _produto(db, "Abaixo", 1, minimo=5)
    _produto(db, "Acima", 10, minimo=5)
    _produto(db, "Farinha", 0, insumo=True, minimo=5)
    itens = EstoqueService(db).itens_reposicao()
    assert [i['nome'] for i in itens] == ["Abaixo"]
    assert itens[0]['qtd_repor'] == 4
    texto = EstoqueService.texto_reposicao_whatsapp(itens)
    assert texto.startswith("📋 *Lista de Reposição*\n")
    assert "• Abaixo: repor 4un (atual: 1, mín: 5)" in texto


# ==================== ProducaoService ====================

def test_baixar_insumo(db):
    from services.producao_service import ProducaoService
    svc = ProducaoService(db)
    ins = _produto(db, "Açúcar", 10, insumo=True)
    with pytest.raises(ValueError, match="Selecione um insumo"):
        svc.baixar_insumo(None, "1")
    for ruim in ("0", "-1", "x", "nan", "inf"):
        with pytest.raises(ValueError, match="Quantidade inválida"):
            svc.baixar_insumo(ins, ruim)
    with pytest.raises(ValueError, match="Estoque insuficiente"):
        svc.baixar_insumo(ins, "11")
    assert _qtd(db, ins) == 10

    msg = svc.baixar_insumo(ins, "2,5", "")
    assert "registrada" in msg
    assert _qtd(db, ins) == pytest.approx(7.5)
    motivo = db.conn.execute("SELECT motivo FROM baixa_insumos WHERE insumo_id=?", (ins,)).fetchone()[0]
    assert motivo == "Uso diário"


def test_criar_ficha_validacoes(db):
    from services.producao_service import ProducaoService
    svc = ProducaoService(db)
    prod = _produto(db, "Bolo", 0)
    ins = _produto(db, "Ovo", 12, insumo=True)
    with pytest.raises(ValueError, match="produto final"):
        svc.criar_ficha(None, "F", "1", [])
    with pytest.raises(ValueError, match="Nome da ficha"):
        svc.criar_ficha(prod, " ", "1", [])
    with pytest.raises(ValueError, match="Rendimento"):
        svc.criar_ficha(prod, "F", "0", [])
    with pytest.raises(ValueError, match="ao menos um insumo"):
        svc.criar_ficha(prod, "F", "1", [{'insumo_id': None, 'quantidade': '1'},
                                         {'insumo_id': ins, 'quantidade': 'abc'}])
    ficha_id = svc.criar_ficha(prod, "Bolo simples", "2",
                               [{'insumo_id': ins, 'quantidade': '3'},
                                {'insumo_id': ins, 'quantidade': '-1'}])
    assert isinstance(ficha_id, int)
    assert len(db.get_ficha_tecnica_itens(ficha_id)) == 1

    with pytest.raises(ValueError, match="Selecione uma ficha"):
        svc.remover_ficha(None)
    svc.remover_ficha(ficha_id)
    assert db.conn.execute("SELECT COUNT(*) FROM fichas_tecnicas WHERE id=?", (ficha_id,)).fetchone()[0] == 0


def test_produzir_insumo_insuficiente_nao_altera_estoque(db):
    from services.producao_service import ProducaoService
    svc = ProducaoService(db)
    prod = _produto(db, "Pão", 0)
    farinha = _produto(db, "Farinha", 100, insumo=True)
    fermento = _produto(db, "Fermento", 1, insumo=True)
    ficha = svc.criar_ficha(prod, "Pão", "1", [{'insumo_id': farinha, 'quantidade': '10'},
                                              {'insumo_id': fermento, 'quantidade': '1'}])
    with pytest.raises(ValueError, match="Selecione uma ficha"):
        svc.produzir(None, "1")
    with pytest.raises(ValueError, match="Quantidade inválida"):
        svc.produzir(ficha, "1.5")
    with pytest.raises(ValueError, match="Fermento"):
        svc.produzir(ficha, "2")
    db.conn.commit()  # nada pendente pode ser efetivado
    assert (_qtd(db, prod), _qtd(db, farinha), _qtd(db, fermento)) == (0, 100, 1)

    assert "produzidas" in svc.produzir(ficha, "1", " lote A ")
    assert (_qtd(db, prod), _qtd(db, farinha), _qtd(db, fermento)) == (1, 90, 0)


# ==================== TrocaService ====================

def test_parse_diferenca():
    from services.troca_service import parse_diferenca
    assert parse_diferenca("10,005") == Decimal("10.01")
    assert parse_diferenca("-2,345") == Decimal("-2.35")
    for vazio in ("", None, "abc", "nan", "inf"):
        assert parse_diferenca(vazio) == Decimal("0.00")


def test_troca_validacoes(db):
    from services.troca_service import TrocaService
    svc = TrocaService(db)
    with pytest.raises(ValueError, match="produto devolvido"):
        svc.registrar("Ana", None, "1")
    for ruim in ("0", "x", "-1"):
        with pytest.raises(ValueError, match="Quantidade devolvida"):
            svc.registrar("Ana", 1, ruim)


def test_troca_estoque_insuficiente_nao_deixa_devolucao_pendente(db):
    """Regressão: antes, o UPDATE da devolução ficava pendente e era efetivado no próximo commit."""
    from services.troca_service import TrocaService
    dev = _produto(db, "Camisa P", 5)
    novo = _produto(db, "Camisa M", 1)
    with pytest.raises(ValueError, match="Estoque insuficiente do produto novo"):
        TrocaService(db).registrar("Ana", dev, "1", produto_novo_id=novo, qtd_nova="3")
    db.conn.commit()
    assert (_qtd(db, dev), _qtd(db, novo)) == (5, 1)
    assert db.conn.execute("SELECT COUNT(*) FROM trocas_devolucoes").fetchone()[0] == 0


def test_troca_ok_e_entrada_com_loja_padrao(db):
    from services.troca_service import TrocaService
    dev = _produto(db, "Tênis 38", 0)
    novo = _produto(db, "Tênis 39", 2)
    msg = TrocaService(db).registrar("Ana", dev, "1", produto_novo_id=novo, qtd_nova="1",
                                     diferenca="10,005")
    assert msg == "Troca registrada com sucesso!"
    assert (_qtd(db, dev), _qtd(db, novo)) == (1, 1)
    valor, loja = db.conn.execute(
        "SELECT valor, loja_id FROM entradas WHERE descricao LIKE 'Troca:%'").fetchone()
    assert valor == pytest.approx(10.01)
    assert loja == db._loja_padrao_id()
    dif = db.conn.execute("SELECT diferenca_valor FROM trocas_devolucoes").fetchone()[0]
    assert dif == pytest.approx(10.01)


def test_devolucao_gasto_com_loja_ativa(db):
    from services.troca_service import TrocaService
    dev = _produto(db, "Bolsa", 0)
    novo = _produto(db, "Carteira", 3)
    loja = db._loja_padrao_id()
    msg = TrocaService(db).registrar("Bia", dev, "2", tipo="devolucao", produto_novo_id=novo,
                                     qtd_nova="1", diferenca="-15,50", loja_id=loja)
    assert msg == "Devolução registrada com sucesso!"
    assert (_qtd(db, dev), _qtd(db, novo)) == (2, 3)  # devolução não baixa o produto novo
    valor, cat, loja_g = db.conn.execute(
        "SELECT valor, categoria, loja_id FROM gastos WHERE descricao LIKE 'Devolução:%'").fetchone()
    assert valor == pytest.approx(15.50)
    assert cat == "Devolução"
    assert loja_g == loja
