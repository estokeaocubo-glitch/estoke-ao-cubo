"""
F1b Lote 8A — RestauranteService, fechamento atômico de comanda e spec com collect_submodules.

Cobre:
- Contrato AST (sem imports de UI em services/restaurante_service.py);
- EstokeAoCubo.spec usa collect_submodules("services") e todos os módulos services/*.py são importáveis;
- Ausência de Database.vender_kit (código morto removido);
- Operações de mesas, comandas e cozinha no RestauranteService;
- fechar_comanda: taxa 10% em Decimal (ROUND_HALF_UP), toggle desligado, rollback em erro
  simulado no INSERT INTO vendas, e fallback em 3 níveis de loja_id em vendas e entradas;
- Delegação preservada em ServicoService (incluindo dividir_conta em centavos).
"""
import ast
import gc
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

RAIZ = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, RAIZ)


@pytest.fixture
def db():
    tmpdir = tempfile.TemporaryDirectory()
    import database

    original = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir.name
    inst = database.Database(db_name="test_lote8a.db")
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


def _produto(db, nome="Hambúrguer", qtd=10, preco=10.05, preco_compra=4.00):
    db.adicionar_produto(nome, qtd, preco, "Lanches", preco_compra=preco_compra)
    row = db.conn.execute("SELECT id FROM produtos WHERE nome = ?", (nome,)).fetchone()
    return int(row[0])


# ==================== AST & Spec & Código morto ====================

def test_restaurante_service_sem_imports_ui():
    caminho = Path(RAIZ) / "services" / "restaurante_service.py"
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    proibidos = {"tkinter", "customtkinter", "tkcalendar", "matplotlib", "webview"}
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes = [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            nomes = [no.module or ""]
        else:
            continue
        for n in nomes:
            assert n.split(".")[0] not in proibidos, f"Import proibido em restaurante_service.py: {n}"


def test_spec_usa_collect_submodules_e_todos_services_importaveis():
    spec_path = Path(RAIZ) / "EstokeAoCubo.spec"
    spec_src = spec_path.read_text(encoding="utf-8")
    arvore = ast.parse(spec_src, filename=str(spec_path))

    chamou_collect_services = False
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id == "collect_submodules":
            if no.args and isinstance(no.args[0], ast.Constant) and no.args[0].value == "services":
                chamou_collect_services = True
    assert chamou_collect_services, 'EstokeAoCubo.spec precisa chamar collect_submodules("services")'

    services_dir = Path(RAIZ) / "services"
    modulos = sorted(p.stem for p in services_dir.glob("*.py") if p.name != "__init__.py")
    assert len(modulos) >= 20
    for mod in modulos:
        importado = importlib.import_module(f"services.{mod}")
        assert importado is not None


def test_codigo_morto_vender_kit_removido(db):
    assert not hasattr(db, "vender_kit")


# ==================== RestauranteService: Mesas, Comandas e Cozinha ====================

def test_fluxo_mesas_comandas_cozinha(db):
    from services.restaurante_service import RestauranteService

    svc = RestauranteService(db)
    criadas = svc.criar_mesas(2, capacidade=4, area="Salão")
    assert criadas == 2
    mesas = svc.listar_mesas()
    assert len(mesas) == 2

    pid = _produto(db, "Suco Natural", qtd=5, preco=12.0, preco_compra=3.0)
    cid = svc.abrir_comanda(mesa_id=mesas[0]["id"], tipo_atendimento="mesa", cliente="Ana", garcom="Leo")
    svc.adicionar_item_comanda(cid, pid, "Suco Natural", 2, 12.0, "Sem açúcar")

    abertas = svc.listar_comandas_abertas()
    assert len(abertas) == 1 and abertas[0]["id"] == cid

    pendentes = svc.itens_cozinha_pendentes()
    assert len(pendentes) == 1
    assert pendentes[0]["produto_nome"] == "Suco Natural"

    svc.atualizar_status_item_comanda(pendentes[0]["id"], "Preparando")
    itens = svc.listar_itens_comanda(cid)
    assert itens[0]["status"] == "Preparando"


# ==================== fechar_comanda: Decimal, Toggle, Rollback e loja_id ====================

def test_fechar_comanda_taxa_10_decimal_half_up_e_toggle(db):
    from services.restaurante_service import RestauranteService

    svc = RestauranteService(db)
    pid = _produto(db, "Prato 10.05", qtd=5, preco=10.05, preco_compra=4.00)

    # Caso 1: R$ 10,05 com taxa 10% -> 1.005 arredonda HALF_UP para R$ 1,01 (antes round(1.005, 2) = 1.0)
    cid1 = svc.abrir_comanda(cliente="Mesa HalfUp")
    svc.adicionar_item_comanda(cid1, pid, "Prato 10.05", 1, 10.05)
    res1 = svc.fechar_comanda(cid1, forma_pagamento="PIX", aplicar_taxa_10=True)
    assert res1["taxa_servico"] == pytest.approx(1.01)
    assert res1["total_final"] == pytest.approx(11.06)

    # Caso 2: toggle desligado -> taxa = 0.00
    cid2 = svc.abrir_comanda(cliente="Mesa Sem Taxa")
    svc.adicionar_item_comanda(cid2, pid, "Prato 10.05", 1, 10.05)
    res2 = svc.fechar_comanda(cid2, forma_pagamento="Dinheiro", aplicar_taxa_10=False)
    assert res2["taxa_servico"] == pytest.approx(0.00)
    assert res2["total_final"] == pytest.approx(10.05)


def test_fechar_comanda_rollback_em_erro_insert_vendas(db):
    from services.restaurante_service import RestauranteService

    svc = RestauranteService(db)
    svc.criar_mesas(1, capacidade=4, area="Salão")
    mesa = svc.listar_mesas()[0]
    pid = _produto(db, "Lanche Teste", qtd=8, preco=25.00, preco_compra=10.00)

    cid = svc.abrir_comanda(mesa_id=mesa["id"], tipo_atendimento="mesa", cliente="Carlos")
    svc.adicionar_item_comanda(cid, pid, "Lanche Teste", 2, 25.00)

    # Trigger que força falha exatamente no INSERT INTO vendas
    db.conn.execute(
        """
        CREATE TRIGGER trg_fail_insert_vendas
        BEFORE INSERT ON vendas
        BEGIN
            SELECT RAISE(ABORT, 'erro_simulado_vendas');
        END;
        """
    )
    db.conn.commit()

    with pytest.raises(RuntimeError, match="erro_simulado_vendas"):
        svc.fechar_comanda(cid, forma_pagamento="Dinheiro", aplicar_taxa_10=True)

    # Comanda, mesa e estoque precisam estar intactos após rollback
    comanda_pos = svc.get_comanda(cid)
    assert comanda_pos["status"] == "aberta"
    mesa_pos = [m for m in svc.listar_mesas() if m["id"] == mesa["id"]][0]
    assert mesa_pos["status"] == "ocupada"
    qtd_pos = db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0]
    assert qtd_pos == 8
    assert db.conn.execute("SELECT COUNT(*) FROM entradas").fetchone()[0] == 0


def test_fechar_comanda_loja_id_tres_niveis_fallback(db):
    from services.restaurante_service import RestauranteService

    svc = RestauranteService(db)
    loja2_id = db.adicionar_loja("Filial Norte")
    pid = _produto(db, "Item MultiLoja", qtd=20, preco=30.00, preco_compra=10.00)

    # Nível 3: sem loja_id explícito e sem caixa aberto -> _loja_padrao_id() (1)
    cid3 = svc.abrir_comanda(cliente="Nível 3")
    svc.adicionar_item_comanda(cid3, pid, "Item MultiLoja", 1, 30.00)
    svc.fechar_comanda(cid3, forma_pagamento="Dinheiro")
    v3 = db.conn.execute("SELECT loja_id FROM vendas ORDER BY id DESC LIMIT 1").fetchone()[0]
    e3 = db.conn.execute("SELECT loja_id FROM entradas ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert v3 == 1 and e3 == 1

    # Nível 2: sem loja_id explícito, com caixa aberto na loja 2 -> herda loja 2 do caixa
    ok_cx, _ = db.abrir_caixa(100.0, usuario_id=1, loja_id=loja2_id)
    assert ok_cx is True
    cid2 = svc.abrir_comanda(cliente="Nível 2")
    svc.adicionar_item_comanda(cid2, pid, "Item MultiLoja", 1, 30.00)
    svc.fechar_comanda(cid2, forma_pagamento="PIX")
    v2 = db.conn.execute("SELECT loja_id FROM vendas ORDER BY id DESC LIMIT 1").fetchone()[0]
    e2 = db.conn.execute("SELECT loja_id FROM entradas ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert v2 == loja2_id and e2 == loja2_id

    # Nível 1: loja_id explícito (loja 1) sobrescreve até mesmo o caixa aberto na loja 2
    cid1 = svc.abrir_comanda(cliente="Nível 1")
    svc.adicionar_item_comanda(cid1, pid, "Item MultiLoja", 1, 30.00)
    svc.fechar_comanda(cid1, forma_pagamento="Dinheiro", loja_id=1)
    v1 = db.conn.execute("SELECT loja_id FROM vendas ORDER BY id DESC LIMIT 1").fetchone()[0]
    e1 = db.conn.execute("SELECT loja_id FROM entradas ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert v1 == 1 and e1 == 1


def test_servico_service_delega_restaurante_e_preserva_dividir_conta(db):
    from services.servico_service import ServicoService

    serv_svc = ServicoService(db)
    pid = _produto(db, "Pizza Div", qtd=5, preco=100.00, preco_compra=40.00)
    cid = db.abrir_comanda(cliente="Mesa Div")
    db.adicionar_item_comanda(cid, pid, "Pizza Div", 1, 100.00)

    div = serv_svc.dividir_conta(cid, 3)
    assert div["partes"] == [33.34, 33.33, 33.33]

    res = serv_svc.fechar_comanda(cid, forma_pagamento="PIX", aplicar_taxa_10=True, loja_id=1)
    assert res["taxa_servico"] == pytest.approx(10.00)
    assert res["total_final"] == pytest.approx(110.00)


# ==================== CadastroService: AST + CRUD Completo ====================

def test_cadastro_service_sem_imports_ui():
    caminho = Path(RAIZ) / "services" / "cadastro_service.py"
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    proibidos = {"tkinter", "customtkinter", "tkcalendar", "matplotlib", "webview"}
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes = [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            nomes = [no.module or ""]
        else:
            continue
        for n in nomes:
            assert n.split(".")[0] not in proibidos, f"Import proibido em cadastro_service.py: {n}"


def test_cadastro_service_clientes_cpf_fornecedores_categorias(db):
    from services.cadastro_service import CadastroService

    cad = CadastroService(db)

    # Cliente com CPF válido e rejeição de CPF inválido
    cid = cad.adicionar_cliente("Maria Clara", "11988887777", "VIP", cpf_cnpj="52998224725")
    assert cid > 0
    clientes = cad.listar_clientes()
    assert any(c["id"] == cid and c["cpf_cnpj"] == "52998224725" for c in clientes)
    with pytest.raises(ValueError, match="CPF inválido"):
        cad.adicionar_cliente("Inválido", "", "", cpf_cnpj="11111111111")
    cad.atualizar_cliente(cid, "Maria Clara Editada", "11977776666", "Obs", cpf_cnpj="52998224725")
    assert any(c["nome"] == "Maria Clara Editada" for c in cad.listar_clientes())
    cad.remover_cliente(cid)
    assert not any(c["id"] == cid for c in cad.listar_clientes())

    # Fornecedor
    cad.adicionar_fornecedor("Distribuidora Alfa", "1133334444", "Bebidas")
    forns = cad.listar_fornecedores()
    assert len(forns) == 1 and forns[0]["nome"] == "Distribuidora Alfa"
    cad.remover_fornecedor(forns[0]["id"])
    assert cad.listar_fornecedores() == []

    # Categorias hierárquicas
    assert cad.adicionar_categoria("Moda", tipo="produto") is True
    assert "Moda" in cad.listar_categorias("produto")
    assert cad.adicionar_subcategoria("Moda", "Camisas", tipo="produto") is True
    assert cad.listar_subcategorias("Moda", "produto") == ["Camisas"]
    assert cad.adicionar_tamanho("Moda", "Camisas", "M", tipo="produto") is True
    assert cad.listar_tamanhos("Moda", "Camisas", "produto") == ["M"]
    assert cad.adicionar_sub_subcategoria("Moda", "Camisas", "Algodão", tipo="produto") is True
    assert cad.listar_sub_subcategorias("Moda", "Camisas", "produto") == ["Algodão"]
    cad.remover_categoria("Moda", "produto")


def test_cadastro_service_vendedores_estabelecimento_lojas_usuarios(db):
    from services.cadastro_service import CadastroService

    cad = CadastroService(db)

    # Vendedores (comissão em Decimal)
    cad.adicionar_vendedor("João Vendedor", "00:00", "23:59", "3,505")
    vends = cad.listar_vendedores()
    assert len(vends) == 1
    assert vends[0]["comissao"] == pytest.approx(3.51)
    assert cad.vendedor_no_turno() == "João Vendedor"
    cad.remover_vendedor(vends[0]["id"])
    assert cad.listar_vendedores() == []

    # Estabelecimento
    cad.adicionar_tarefa_estabelecimento("Cozinha", "Higienizar chapa", "Alta")
    tarefas = cad.listar_tarefas_estabelecimento()
    assert len(tarefas) == 1
    cad.atualizar_status_estabelecimento(tarefas[0]["id"], "Concluído")
    assert cad.listar_tarefas_estabelecimento()[0]["status"] == "Concluído"
    cad.remover_tarefa_estabelecimento(tarefas[0]["id"])
    assert cad.listar_tarefas_estabelecimento() == []

    # Lojas
    lid = cad.adicionar_loja("Filial Sul", "Av. Brasil, 200", "1140005000", "12345678000199")
    assert any(l["id"] == lid and l["nome"] == "Filial Sul" for l in cad.listar_lojas())
    cad.atualizar_loja(lid, "Filial Sul 2", "Av. Brasil, 201", "1140005000", "12345678000199")
    assert any(l["nome"] == "Filial Sul 2" for l in cad.listar_lojas())
    cad.remover_loja(lid)
    assert not any(l["id"] == lid for l in cad.listar_lojas())

    # Usuários (hash PBKDF2 continua no db; senha nunca em texto claro)
    senha_teste = "Teste@2026#Seguro"
    ok, msg = cad.adicionar_usuario("Operador Teste", "op_8a", senha_teste, perfil="caixa", loja_id=1)
    assert ok is True, msg
    row_hash = db.conn.execute("SELECT senha_hash FROM usuarios WHERE login = 'op_8a'").fetchone()[0]
    assert senha_teste not in row_hash
    assert row_hash.startswith(("scrypt:", "pbkdf2_", "pbkdf2:"))
    uid = [u["id"] for u in cad.listar_usuarios() if u["login"] == "op_8a"][0]
    cad.alterar_senha_usuario(uid, "Nova@2026#Seguro")
    assert db.autenticar_usuario("op_8a", "Nova@2026#Seguro") is not None
    cad.remover_usuario(uid)
    assert db.autenticar_usuario("op_8a", "Nova@2026#Seguro") is None

