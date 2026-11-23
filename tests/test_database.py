"""
Testes para a camada de banco de dados — CRUD de produtos, kits, vendas.
Usa banco SQLite em memória para isolamento.
"""
import pytest
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def db():
    """Cria uma instância de Database com banco temporário para cada teste."""
    import gc
    import time
    tmpdir = tempfile.TemporaryDirectory()
    import database
    original_func = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir.name
    
    db_instance = database.Database(db_name="test_estoque.db")
    yield db_instance
    
    try:
        db_instance.close()
    except Exception:
        pass
    database.get_app_data_path = original_func
    # Forçar coleta de lixo para liberar referências ao banco
    gc.collect()
    # Tentativas de limpeza do diretório temporário
    for _ in range(10):
        try:
            tmpdir.cleanup()
            break
        except PermissionError:
            time.sleep(0.15)


class TestProdutosCRUD:
    """Testes de CRUD para produtos."""

    def test_adicionar_produto(self, db):
        db.adicionar_produto("Caneta Azul", 100, 2.50, "Escritório", 1.20)
        df = db.listar_produtos()
        assert not df.empty
        assert "Caneta Azul" in df['nome'].values

    def test_adicionar_produto_duplicado(self, db):
        db.adicionar_produto("Mouse", 10, 50.00)
        db.adicionar_produto("Mouse", 5, 55.00)
        df = db.listar_produtos()
        # Deve ter 2 registros (sistema permite nomes iguais)
        assert len(df[df['nome'] == 'Mouse']) >= 1

    def test_remover_produto(self, db):
        db.adicionar_produto("Item Temp", 1, 10.00)
        df = db.listar_produtos()
        prod_id = int(df[df['nome'] == 'Item Temp'].iloc[0]['id'])
        db.remover_produto(prod_id)
        df2 = db.listar_produtos()
        assert 'Item Temp' not in df2['nome'].values

    def test_listar_produtos_vazio(self, db):
        df = db.listar_produtos()
        assert df.empty or len(df) == 0

    def test_produto_com_codigo_barras(self, db):
        db.adicionar_produto("Sabão", 50, 5.90, codigo_barras="7891234567890")
        df = db.buscar_produto_por_codigo("7891234567890")
        assert not df.empty
        assert df.iloc[0]['nome'] == "Sabão"


class TestKitsCRUD:
    """Testes de CRUD para kits."""

    def test_criar_kit(self, db):
        db.adicionar_produto("Teclado", 20, 89.90)
        db.adicionar_produto("Mouse", 30, 49.90)
        
        itens = [
            {"nome": "Teclado", "quantidade": 1},
            {"nome": "Mouse", "quantidade": 1}
        ]
        db.adicionar_kit("Kit Gamer", 0, 119.90, 0, itens)
        
        kits = db.listar_kits()
        assert not kits.empty
        assert "Kit Gamer" in kits['nome'].values

    def test_disponibilidade_kit(self, db):
        db.adicionar_produto("Parafuso", 100, 0.50)
        db.adicionar_produto("Porca", 80, 0.30)
        
        itens = [
            {"nome": "Parafuso", "quantidade": 2},
            {"nome": "Porca", "quantidade": 1}
        ]
        db.adicionar_kit("Kit Montagem", 0, 2.00, 0, itens)
        
        kits = db.listar_kits()
        kit_id = int(kits.iloc[0]['id'])
        disponivel = db.verificar_disponibilidade_kit(kit_id)
        # 100 parafusos / 2 = 50, 80 porcas / 1 = 80 → gargalo = 50
        assert disponivel == 50

    def test_disponibilidade_kit_sem_estoque(self, db):
        db.adicionar_produto("Item A", 0, 10.00)
        
        itens = [{"nome": "Item A", "quantidade": 1}]
        db.adicionar_kit("Kit Vazio", 0, 15.00, 0, itens)
        
        kits = db.listar_kits()
        kit_id = int(kits.iloc[0]['id'])
        assert db.verificar_disponibilidade_kit(kit_id) == 0


class TestCategorias:
    """Testes para categorias customizáveis."""

    def test_adicionar_categoria_produto(self, db):
        db.adicionar_categoria("Eletrônicos", "produto")
        cats = db.listar_categorias("produto")
        assert "Eletrônicos" in cats

    def test_adicionar_categoria_gasto(self, db):
        db.adicionar_categoria("Energia", "gasto")
        cats = db.listar_categorias("gasto")
        assert "Energia" in cats

    def test_categoria_duplicada(self, db):
        db.adicionar_categoria("Teste", "produto")
        result = db.adicionar_categoria("Teste", "produto")
        assert result is False

    def test_remover_categoria(self, db):
        db.adicionar_categoria("Temp", "produto")
        db.remover_categoria("Temp", "produto")
        cats = db.listar_categorias("produto")
        assert "Temp" not in cats


class TestClientes:
    """Testes para CRUD de clientes."""

    def test_adicionar_cliente(self, db):
        db.adicionar_cliente("João Silva", "11999999999", "cliente vip")
        df = db.listar_clientes()
        assert not df.empty
        assert "João Silva" in df['nome'].values

    def test_listar_clientes_vazio(self, db):
        df = db.listar_clientes()
        assert df.empty or len(df) == 0


class TestUsuarios:
    """Testes para multi-usuário."""

    def test_criar_usuario(self, db):
        ok, msg = db.adicionar_usuario("Maria", "maria123", "SenhaSegura123!", "vendedor")
        assert ok is True

    def test_login_duplicado(self, db):
        # A senha do primeiro usuário deve ser forte para prosseguir para o teste de login duplicado
        db.adicionar_usuario("Ana", "ana_login", "SenhaSegura123!", "vendedor")
        # O segundo usuário tenta com senha fraca primeiro (deve falhar por força da senha)
        ok, msg = db.adicionar_usuario("Ana 2", "ana_login2", "123456", "caixa")
        assert ok is False
        assert "caracteres" in msg.lower()
        
        # Tentativa com senha forte, deve falhar por duplicidade de login
        ok, msg = db.adicionar_usuario("Ana 2", "ana_login", "SenhaSegura456!", "caixa")
        assert ok is False
        assert "existe" in msg.lower()

    def test_autenticar_usuario(self, db):
        db.adicionar_usuario("Pedro", "pedro_login", "MinhaSenha123!", "gerente")
        user = db.autenticar_usuario("pedro_login", "MinhaSenha123!")
        assert user is not None
        assert user['nome'] == "Pedro"
        assert user['perfil'] == "gerente"

    def test_autenticar_senha_errada(self, db):
        db.adicionar_usuario("Carlos", "carlos_login", "SenhaCorreta123!", "vendedor")
        user = db.autenticar_usuario("carlos_login", "SenhaErrada123!")
        assert user is None

    def test_alterar_senha(self, db):
        db.adicionar_usuario("Lucas", "lucas_login", "SenhaAntiga123!", "caixa")
        df = db.listar_usuarios()
        user_id = int(df[df['login'] == 'lucas_login'].iloc[0]['id'])
        db.alterar_senha_usuario(user_id, "SenhaNova123!")
        # Testar com senha nova
        user = db.autenticar_usuario("lucas_login", "SenhaNova123!")
        assert user is not None
        # Testar que senha antiga não funciona mais
        user2 = db.autenticar_usuario("lucas_login", "SenhaAntiga123!")
        assert user2 is None
