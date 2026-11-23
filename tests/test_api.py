"""
Testes para a API REST Flask.
"""
import pytest
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def api_client():
    """Cria um test client Flask com banco temporário."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import database
        original_func = database.get_app_data_path
        database.get_app_data_path = lambda: tmpdir

        db = database.Database(db_name="test_api.db")
        
        from api_server import EstokeAPI, FLASK_AVAILABLE
        if not FLASK_AVAILABLE:
            pytest.skip("Flask não instalado")

        api = EstokeAPI(db, port=0)
        api.app.config['TESTING'] = True

        with api.app.test_client() as client:
            yield client, db

        try:
            db.close()
        except Exception:
            pass
        database.get_app_data_path = original_func


def _get_token(client, db):
    """Helper: faz login e retorna token."""
    # Criar usuário de teste
    db.adicionar_usuario("Tester", "tester", "SenhaSegura123!", "admin")
    
    response = client.post('/api/login', 
                          data=json.dumps({"login": "tester", "senha": "SenhaSegura123!"}),
                          content_type='application/json')
    data = response.get_json()
    return data.get("token", "")


class TestAPIHealth:
    """Testes do endpoint de saúde."""

    def test_health_ok(self, api_client):
        client, db = api_client
        response = client.get('/api/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'


class TestAPILogin:
    """Testes de autenticação."""

    def test_login_valido(self, api_client):
        client, db = api_client
        db.adicionar_usuario("Admin", "admin_test", "SenhaSegura123!", "admin")
        
        response = client.post('/api/login',
                              data=json.dumps({"login": "admin_test", "senha": "SenhaSegura123!"}),
                              content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert "token" in data
        assert data["user"]["nome"] == "Admin"

    def test_login_invalido(self, api_client):
        client, db = api_client
        response = client.post('/api/login',
                              data=json.dumps({"login": "fake", "senha": "wrong"}),
                              content_type='application/json')
        assert response.status_code == 401

    def test_login_sem_dados(self, api_client):
        client, db = api_client
        response = client.post('/api/login',
                              data=json.dumps({}),
                              content_type='application/json')
        assert response.status_code == 400

    def test_rate_limit_login(self, api_client):
        """Verifica que o rate-limit funciona após muitas tentativas."""
        client, db = api_client
        for _ in range(6):
            client.post('/api/login',
                       data=json.dumps({"login": "x", "senha": "y"}),
                       content_type='application/json')
        
        response = client.post('/api/login',
                              data=json.dumps({"login": "x", "senha": "y"}),
                              content_type='application/json')
        # Após 5+ tentativas deve retornar 429
        assert response.status_code == 429


class TestAPIProdutos:
    """Testes dos endpoints de produtos."""

    def test_sem_token_rejeita(self, api_client):
        client, db = api_client
        response = client.get('/api/produtos')
        assert response.status_code == 401

    def test_listar_produtos(self, api_client):
        client, db = api_client
        token = _get_token(client, db)
        
        db.adicionar_produto("Teste Prod", 10, 25.00)
        
        response = client.get('/api/produtos',
                             headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1


class TestAPIVendas:
    """Testes dos endpoints de vendas."""

    def test_listar_vendas(self, api_client):
        client, db = api_client
        token = _get_token(client, db)
        
        response = client.get('/api/vendas',
                             headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200


class TestAPIDashboard:
    """Testes do endpoint de dashboard."""

    def test_dashboard(self, api_client):
        client, db = api_client
        token = _get_token(client, db)
        
        response = client.get('/api/dashboard',
                             headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.get_json()
        assert "saldo" in data
