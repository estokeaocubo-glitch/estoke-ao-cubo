"""
Testes para a camada de serviços — VendaService, EstoqueService.
"""
import pytest
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def db():
    """Banco temporário para testes."""
    import gc
    import time
    tmpdir = tempfile.TemporaryDirectory()
    import database
    original_func = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir.name
    db_instance = database.Database(db_name="test_services.db")
    yield db_instance
    try:
        db_instance.close()
    except Exception:
        pass
    database.get_app_data_path = original_func
    # Forçar coleta de lixo para liberar referências ao arquivo sqlite
    gc.collect()
    # Tentativas de limpeza do diretório temporário
    for _ in range(10):
        try:
            tmpdir.cleanup()
            break
        except PermissionError:
            time.sleep(0.15)


@pytest.fixture
def venda_service(db):
    from services.venda_service import VendaService
    return VendaService(db)


@pytest.fixture
def estoque_service(db):
    from services.estoque_service import EstoqueService
    return EstoqueService(db)


class TestVendaServiceCalculos:
    """Testes de cálculos de venda."""

    def test_calcular_totais_sem_desconto(self, venda_service):
        carrinho = [
            {"nome": "Caneta", "preco_unitario": 2.50, "quantidade": 4},
            {"nome": "Caderno", "preco_unitario": 15.90, "quantidade": 1},
        ]
        result = venda_service.calcular_totais(carrinho)
        assert result['subtotal'] == 25.90
        assert result['desconto'] == 0
        assert result['total'] == 25.90

    def test_calcular_totais_desconto_percentual(self, venda_service):
        carrinho = [
            {"nome": "Item", "preco_unitario": 100.00, "quantidade": 1},
        ]
        result = venda_service.calcular_totais(carrinho, desconto_valor=10, desconto_tipo='%')
        assert result['subtotal'] == 100.00
        assert result['desconto'] == 10.00
        assert result['total'] == 90.00

    def test_calcular_totais_desconto_fixo(self, venda_service):
        carrinho = [
            {"nome": "Item", "preco_unitario": 50.00, "quantidade": 2},
        ]
        result = venda_service.calcular_totais(carrinho, desconto_valor=20, desconto_tipo='R$')
        assert result['subtotal'] == 100.00
        assert result['desconto'] == 20.00
        assert result['total'] == 80.00

    def test_desconto_nao_excede_subtotal(self, venda_service):
        carrinho = [
            {"nome": "Item", "preco_unitario": 10.00, "quantidade": 1},
        ]
        result = venda_service.calcular_totais(carrinho, desconto_valor=50, desconto_tipo='R$')
        assert result['desconto'] == 10.00
        assert result['total'] == 0.00

    def test_calcular_troco_suficiente(self, venda_service):
        troco, suficiente = venda_service.calcular_troco(37.50, 50.00)
        assert suficiente is True
        assert troco == 12.50

    def test_calcular_troco_insuficiente(self, venda_service):
        troco, suficiente = venda_service.calcular_troco(100.00, 80.00)
        assert suficiente is False

    def test_calcular_troco_exato(self, venda_service):
        troco, suficiente = venda_service.calcular_troco(25.00, 25.00)
        assert suficiente is True
        assert troco == 0.00


class TestVendaServiceValidacao:
    """Testes de validação de carrinho."""

    def test_validar_carrinho_com_estoque(self, venda_service, db):
        db.adicionar_produto("Mouse Gamer", 10, 89.90)
        carrinho = [{"nome": "Mouse Gamer", "quantidade": 5, "tipo": "produto"}]
        ok, erros = venda_service.validar_carrinho(carrinho)
        assert ok is True
        assert len(erros) == 0

    def test_validar_carrinho_sem_estoque(self, venda_service, db):
        db.adicionar_produto("Teclado", 2, 150.00)
        carrinho = [{"nome": "Teclado", "quantidade": 10, "tipo": "produto"}]
        ok, erros = venda_service.validar_carrinho(carrinho)
        assert ok is False
        assert len(erros) > 0

    def test_validar_carrinho_produto_inexistente(self, venda_service):
        carrinho = [{"nome": "Produto Fantasma", "quantidade": 1, "tipo": "produto"}]
        ok, erros = venda_service.validar_carrinho(carrinho)
        assert ok is False

    def test_validar_carrinho_quantidade_zero(self, venda_service):
        carrinho = [{"nome": "Qualquer", "quantidade": 0, "tipo": "produto"}]
        ok, erros = venda_service.validar_carrinho(carrinho)
        assert ok is False


class TestEstoqueService:
    """Testes do serviço de estoque."""

    def test_lista_reposicao_vazia(self, estoque_service):
        result = estoque_service.lista_reposicao()
        assert isinstance(result, list)

    def test_produtos_vencendo(self, estoque_service, db):
        # Adicionar produto com validade próxima
        import datetime
        amanha = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        db.adicionar_produto("Iogurte", 10, 5.00, data_validade=amanha)
        df = estoque_service.produtos_vencendo(dias=7)
        assert not df.empty


@pytest.fixture
def sonhos_service(db):
    from services.dreams_service import SonhosService
    return SonhosService(db.conn)


class TestSonhosService:
    """Testes de unidade para o SonhosService (Anota Tudo)."""

    def test_adicionar_e_listar_sonhos(self, sonhos_service):
        dados = {
            "usuario_id": 1,
            "titulo": "Anotação de Teste",
            "descricao": "Esta é uma descrição de teste bem longa com mais de 10 caracteres.",
            "categoria": "Ideias",
            "data_registro": "2026-05-28"
        }
        sonho_id = sonhos_service.adicionar_sonho(dados)
        assert sonho_id > 0

        lista = sonhos_service.listar_sonhos(usuario_id=1)
        assert len(lista) == 1
        assert lista[0]["titulo"] == "Anotação de Teste"
        assert lista[0]["categoria"] == "Ideias"
        assert lista[0]["version"] == 1

    def test_editar_sonho_sucesso_e_concorrencia(self, sonhos_service):
        dados = {
            "usuario_id": 1,
            "titulo": "Anotação Original",
            "descricao": "Esta é uma descrição de teste bem longa com mais de 10 caracteres.",
            "categoria": "Geral",
            "data_registro": "2026-05-28"
        }
        sonho_id = sonhos_service.adicionar_sonho(dados)

        # Editar com sucesso
        dados_novos = dados.copy()
        dados_novos["titulo"] = "Anotação Editada"
        res = sonhos_service.editar_sonho(sonho_id, dados_novos, versao_lida=1, usuario_requisitor_id=1)
        assert res is True

        lista = sonhos_service.listar_sonhos(usuario_id=1)
        assert lista[0]["titulo"] == "Anotação Editada"
        assert lista[0]["version"] == 2

        # Tentar editar com versão desatualizada (OCC Concurrency Error)
        from services.dreams_service import DreamConcurrencyError
        with pytest.raises(DreamConcurrencyError):
            sonhos_service.editar_sonho(sonho_id, dados_novos, versao_lida=1, usuario_requisitor_id=1)

    def test_remover_sonho(self, sonhos_service):
        dados = {
            "usuario_id": 1,
            "titulo": "Anotação Temporária",
            "descricao": "Esta é uma descrição de teste bem longa com mais de 10 caracteres.",
            "categoria": "Geral",
            "data_registro": "2026-05-28"
        }
        sonho_id = sonhos_service.adicionar_sonho(dados)

        # Tentar remover por outro usuário (erro de permissão)
        with pytest.raises(PermissionError):
            sonhos_service.remover_sonho(sonho_id, usuario_requisitor_id=99)

        # Remover com sucesso
        res = sonhos_service.remover_sonho(sonho_id, usuario_requisitor_id=1)
        assert res is True

        lista = sonhos_service.listar_sonhos(usuario_id=1)
        assert len(lista) == 0

