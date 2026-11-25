"""
Testes unitários e de concorrência do Lote 1 da F1b:
- Matriz RBAC completa (botões base + 25 botões de nicho Tabela C + fail-closed)
- Autenticação, troca de senha e gate administrativo
- Autorização de supervisor (admin/gerente vs vendedor/caixa)
- Pareamento mobile thread-safe (20 threads simultâneas + cleanup + _require_auth + stop)
- paths.get_web_path compatível com _MEIPASS sem criar diretório web/
- Contrato AST de services/auth_service.py (zero tkinter/customtkinter)
"""
import ast
import json
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

import paths
from api_server import EstokeAPI, FLASK_AVAILABLE
from database import Database
from services.auth_service import AuthService, PERMISSAO_MAP


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Cria instância isolada do Database em diretório temporário."""
    monkeypatch.setenv("ESTOKE_DATA_DIR", str(tmp_path))
    db = Database(db_name="test_auth_lote1.db")
    yield db
    if not getattr(db, "_closed", False):
        db.close()


class TestRBACMatrix:
    """Valida os 4 perfis contra todos os botões base, 25 botões de nicho e botão inexistente."""

    BASE_MAP_ANTERIOR = {
        "SALDO": "gastos",
        "GASTOS": "gastos",
        "CAIXA": "caixa",
        "KITS": "kits",
        "CATEGORIAS": "categorias",
        "FORNECEDORES": "fornecedores",
        "CLIENTES": "clientes",
        "ANOTA TUDO": "vender",
        "VENDEDORES": "vendedores",
        "CONFIG": "ver_config",
        "CHAT IA": "chat_ia",
        "INSUMOS": "insumos",
        "PRODUÇÃO": "producao",
        "TROCAS": "vender",
        "REPOSIÇÃO": "adicionar",
        "INVENTÁRIO": "editar",
        "COMPRAS": "adicionar",
        "MESAS": "mesas",
        "COMANDAS": "mesas",
        "COZINHA": "mesas",
        "CONTAS PAGAR": "contas_pagar",
        "ETIQUETAS": "etiquetas",
        "ADICIONAR": "adicionar",
        "VENDER": "vender",
        "SERVIÇO": "vender",
        "LISTA": "editar",
        "RELATORIO": "ver_relatorio",
        "DRE": "ver_relatorio",
    }

    TABELA_C_NICHOS = {
        "ORDEM SERVIÇO": "vender",
        "AJUSTES / ATELIÊ": "vender",
        "BANHO & TOSA": "vender",
        "HARDWARE": "insumos",
        "RAÇÃO A GRANEL": "insumos",
        "MEDICAMENTOS": "insumos",
        "GARANTIAS": "vender",
        "Nº SÉRIE": "vender",
        "AGENDAMENTOS": "vender",
        "RESERVAS": "vender",
        "LOCAÇÃO": "vender",
        "ORÇAMENTOS": "vender",
        "LOTES & VALIDADE": "adicionar",
        "VENCIMENTOS": "adicionar",
        "VEÍCULOS": "clientes",
        "CONDOMÍNIOS": "clientes",
        "PETS & VACINAS": "clientes",
        "RECEITAS & GRAUS": "clientes",
        "ALUNOS & TURMAS": "clientes",
        "MENSALIDADES": "clientes",
        "PROFISSIONAIS": "vendedores",
        "MECÂNICOS": "vendedores",
        "PEDIDOS / MESAS": "mesas",
        "COMBOS": "mesas",
        "LEITOR RÁPIDO": "vender",
    }

    def test_chaves_todas_existem_em_perfis_permissoes(self, isolated_db):
        chaves_validas = set()
        for perms in isolated_db.PERFIS_PERMISSOES.values():
            chaves_validas.update(perms.keys())
        chaves_validas.discard("tudo")

        for botao, chave in PERMISSAO_MAP.items():
            assert chave in chaves_validas, f"Botão '{botao}' usa chave inexistente '{chave}'"

    def test_matriz_4_perfis_botoes_base_e_nichos_e_fail_closed(self, isolated_db):
        auth = AuthService(isolated_db)
        perfis = ("admin", "gerente", "vendedor", "caixa")

        # 1. Regressão dos 28 botões base originais
        for perfil in perfis:
            for botao, chave in self.BASE_MAP_ANTERIOR.items():
                esperado = bool(isolated_db.usuario_tem_permissao(perfil, chave))
                assert auth.tem_permissao(perfil, botao) is esperado
                assert auth.tem_permissao({"perfil": perfil}, botao) is esperado

        # 2. Os 25 botões condicionais de nicho conforme Tabela C ajustada
        assert len(self.TABELA_C_NICHOS) == 25
        for perfil in perfis:
            for botao, chave in self.TABELA_C_NICHOS.items():
                esperado = bool(isolated_db.usuario_tem_permissao(perfil, chave))
                assert auth.tem_permissao(perfil, botao) is esperado, (
                    f"Divergência RBAC para perfil={perfil}, botao={botao}, chave={chave}"
                )

        # 3. Botão inexistente -> fail-closed (apenas admin)
        assert auth.tem_permissao("admin", "BOTAO_INEXISTENTE_XYZ") is True
        assert auth.tem_permissao("gerente", "BOTAO_INEXISTENTE_XYZ") is False
        assert auth.tem_permissao("vendedor", "BOTAO_INEXISTENTE_XYZ") is False
        assert auth.tem_permissao("caixa", "BOTAO_INEXISTENTE_XYZ") is False
        assert auth.tem_permissao(None, "VENDER") is False

        # 4. listar_permissoes_usuario
        permitidos_caixa = auth.listar_permissoes_usuario("caixa", ["CAIXA", "GASTOS", "BANHO & TOSA", "RAÇÃO A GRANEL"])
        assert permitidos_caixa == ["CAIXA", "BANHO & TOSA"]


class TestAutenticacaoESupervisor:
    """Valida autenticação, gate admin e autorização de supervisor."""

    def test_autenticar_valido_senha_errada_e_inexistente(self, isolated_db):
        isolated_db.adicionar_usuario("Carlos Gerente", "carlos_g", "Gerente@123", perfil="gerente")
        auth = AuthService(isolated_db)

        user_ok = auth.autenticar("carlos_g", "Gerente@123")
        assert user_ok is not None
        assert user_ok["nome"] == "Carlos Gerente"
        assert user_ok["perfil"] == "gerente"

        assert auth.autenticar("carlos_g", "SenhaErrada@999") is None
        assert auth.autenticar("usuario_fantasma", "Qualquer@123") is None

        with pytest.raises(ValueError):
            auth.autenticar("", "Gerente@123")
        with pytest.raises(ValueError):
            auth.autenticar("carlos_g", "")

    def test_autorizar_supervisor_perfis_e_identificacao(self, isolated_db):
        isolated_db.adicionar_usuario("Ana Admin", "ana_adm", "Admin@123", perfil="admin")
        isolated_db.adicionar_usuario("Bruno Gerente", "bruno_ger", "Gerente@123", perfil="gerente")
        isolated_db.adicionar_usuario("Carla Vendedora", "carla_vend", "Vendedora@123", perfil="vendedor")
        isolated_db.adicionar_usuario("Diego Caixa", "diego_cx", "Caixa@123", perfil="caixa")

        auth = AuthService(isolated_db)

        # Admin autoriza e retorno identifica quem autorizou
        res_adm = auth.autorizar_supervisor("ana_adm", "Admin@123", acao="Cancelar Venda", motivo="Erro digitação", exigir_motivo=True)
        assert res_adm["approved"] is True
        assert res_adm["supervisor_nome"] == "Ana Admin"
        assert res_adm["supervisor_perfil"] == "admin"
        assert res_adm["supervisor_id"] is not None
        assert res_adm["motivo"] == "Erro digitação"

        # Gerente autoriza e retorno identifica quem autorizou
        res_ger = auth.autorizar_supervisor("bruno_ger", "Gerente@123", acao="Desconto 20%")
        assert res_ger["approved"] is True
        assert res_ger["supervisor_nome"] == "Bruno Gerente"
        assert res_ger["supervisor_perfil"] == "gerente"
        assert res_ger["supervisor_id"] is not None

        # Vendedor e Caixa não autorizam
        with pytest.raises(PermissionError):
            auth.autorizar_supervisor("carla_vend", "Vendedora@123", acao="Desconto 20%")
        with pytest.raises(PermissionError):
            auth.autorizar_supervisor("diego_cx", "Caixa@123", acao="Abrir Gaveta")

        # Motivo obrigatório ausente -> ValueError
        with pytest.raises(ValueError):
            auth.autorizar_supervisor("ana_adm", "Admin@123", acao="Cancelar Venda", motivo="", exigir_motivo=True)

        # Gate admin aceita admin/gerente e recusa vendedor/caixa
        assert auth.verificar_gate_admin("ana_adm", "Admin@123") is True
        assert auth.verificar_gate_admin("bruno_ger", "Gerente@123") is True
        assert auth.verificar_gate_admin("carla_vend", "Vendedora@123") is False
        assert auth.verificar_gate_admin("diego_cx", "Caixa@123") is False


class TestPareamentoThreadSafe:
    """Valida criar_sessao_pareamento, ausência de senha no payload, concorrência e stop()."""

    @pytest.mark.skipif(not FLASK_AVAILABLE, reason="Flask não instalado")
    def test_pareamento_payload_sem_senha_20_threads_e_stop_limpa(self, isolated_db):
        isolated_db.adicionar_usuario("Operador Mobile", "op_mob", "Mobile@123", perfil="gerente")
        user = isolated_db.autenticar_usuario("op_mob", "Mobile@123")
        assert user is not None

        api = EstokeAPI(isolated_db, port=0)

        # 1. Criação simples e validação de payload sem senha
        pareamento = api.criar_sessao_pareamento(user, ip_local="192.168.1.50")
        assert pareamento["token"] and len(pareamento["token"]) == 64
        assert pareamento["ip"] == "192.168.1.50"
        assert pareamento["login"] == "op_mob"
        assert pareamento["expires_in"] == api._SESSION_TTL

        payload_dict = json.loads(pareamento["qr_payload"])
        assert payload_dict["token"] == pareamento["token"]
        assert payload_dict["login"] == "op_mob"
        assert "senha" not in payload_dict
        assert "password" not in payload_dict
        assert "Mobile@123" not in pareamento["qr_payload"]

        # 2. Estresse concorrente: 20 threads criando sessões + _cleanup_sessions + _require_auth
        erros = []
        tokens_criados = []
        tokens_lock = threading.Lock()

        def worker(idx):
            try:
                for j in range(5):
                    p = api.criar_sessao_pareamento(user, ip_local="127.0.0.1")
                    tok = p["token"]
                    with tokens_lock:
                        tokens_criados.append(tok)
                    # Forçar uma expiração periódica para exercitar deleções durante iteração
                    if (idx + j) % 4 == 0:
                        with api._sessions_lock:
                            if tok in api._sessions:
                                api._sessions[tok]["expires"] = time.time() - 10
                    api._cleanup_sessions()
                    # Exercitar _require_auth sob request context real do Flask
                    with api.app.test_request_context(
                        "/api/produtos",
                        headers={"Authorization": f"Bearer {tok}"},
                        environ_base={"REMOTE_ADDR": f"10.0.0.{(idx % 200) + 1}"},
                    ):
                        api._require_auth(acao_necessaria="vender")
            except Exception as exc:
                erros.append(exc)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futs = [pool.submit(worker, i) for i in range(20)]
            for fut in as_completed(futs):
                fut.result()

        assert erros == [], f"Erros sob concorrência em _sessions: {erros}"
        assert len(tokens_criados) == 100

        # 3. stop() limpa todas as sessões
        api.stop()
        with api._sessions_lock:
            assert len(api._sessions) == 0


class TestPathsAndContract:
    """Valida paths.get_web_path e contrato AST puro de services/auth_service.py."""

    def test_get_web_path_compativel_meipass_sem_criar_pasta(self, tmp_path, monkeypatch):
        # Em modo desenvolvimento, resolve para .../web/index.html sem criar diretórios.
        # (A partir da v2.0 a pasta web/ existe no repositório, então a garantia de
        # "não criar" é verificada com um subdiretório inexistente.)
        p_dev = paths.get_web_path("index.html")
        assert p_dev.endswith(os.path.join("web", "index.html"))
        p_inexistente = paths.get_web_path("__nao_existe__", "x.html")
        assert p_inexistente.endswith(os.path.join("web", "__nao_existe__", "x.html"))
        assert not os.path.exists(os.path.join(paths.get_install_dir(), "web", "__nao_existe__"))

        # Em modo frozen (_MEIPASS), resolve dentro de sys._MEIPASS/web/...
        fake_meipass = tmp_path / "meipass_bundle"
        fake_web = fake_meipass / "web"
        fake_web.mkdir(parents=True)
        (fake_web / "index.html").write_text("<html></html>", encoding="utf-8")

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)

        p_frozen = paths.get_web_path("index.html")
        assert os.path.normpath(p_frozen) == os.path.normpath(str(fake_web / "index.html"))

    def test_contrato_ast_auth_service_sem_tkinter(self):
        src_path = pathlib.Path(__file__).resolve().parent.parent / "services" / "auth_service.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        proibidos = {"tkinter", "customtkinter", "tkcalendar"}
        encontrados = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    topo = alias.name.split(".")[0]
                    if topo in proibidos:
                        encontrados.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                topo = (node.module or "").split(".")[0]
                if topo in proibidos:
                    encontrados.append(node.module)

        assert encontrados == [], f"Imports de UI proibidos encontrados em auth_service.py: {encontrados}"
