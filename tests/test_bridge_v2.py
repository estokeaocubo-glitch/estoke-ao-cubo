"""
Testes da Interface v2.0 (Marco 1):
- Envelope {ok, data, error} da bridge, serialização de Decimal/datas e erros sem stacktrace
- Login, logout, sessão e autorização de supervisor via bridge
- Primeiro acesso com os 18 nichos e menu lateral por nicho
- Tema persistido em config.json e troca de loja ativa
- Eventos Python -> JS e inicialização da bridge (só métodos públicos esperados)
- Contrato offline de web/ (sem http:// nem https://) e assets obrigatórios
- Contrato AST: services/ não importa UI (tkinter/customtkinter/webview/views/bridge)

Todo teste roda com ESTOKE_DATA_DIR isolado em tmp_path e sem rede (fetcher mockado).
"""
import ast
import datetime
import json
import logging
import os
import pathlib
from decimal import Decimal

import pytest

import paths
from bridge import EstokeBridge, envelope_fail, envelope_ok, safe_call, to_jsonable
from bridge.app_bridge import SECOES_SIDEBAR, TOPBAR_BOTOES, _SECAO_POR_BOTAO, slugify
from bridge.events import build_emit_script
from database import Database
from services.nicho_service import NichoService

RAIZ = pathlib.Path(__file__).resolve().parent.parent
WEB = RAIZ / "web"

ADMIN_LOGIN = "admincentral"
ADMIN_SENHA = "admin9438"
SENHA_FORTE = "Senha@Forte2026"

METODOS_PUBLICOS = {
    "ping", "get_bootstrap", "get_theme", "set_theme", "list_lojas", "set_loja_ativa",
    "toggle_fullscreen", "get_sidebar_menu", "check_updates_status",
    "get_first_access_catalog", "complete_first_access", "login", "logout",
    "get_current_session", "autorizar_supervisor",
}


def _fetcher_offline(_url):
    raise ConnectionError("sem rede nos testes")


class FakeWindow:
    def __init__(self):
        self.scripts = []
        self.fullscreen_calls = 0

    def evaluate_js(self, script):
        self.scripts.append(script)

    def toggle_fullscreen(self):
        self.fullscreen_calls += 1


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTOKE_DATA_DIR", str(tmp_path))
    db = Database(db_name="test_bridge_v2.db")
    yield db
    if not getattr(db, "_closed", False):
        db.close()


@pytest.fixture
def bridge(isolated_db):
    b = EstokeBridge(db=isolated_db, update_fetcher=_fetcher_offline)
    yield b
    b._shutdown()


def _ok(res):
    assert set(res) == {"ok", "data", "error"}, res
    assert res["ok"] is True, res
    assert res["error"] is None
    return res["data"]


def _fail(res, code=None):
    assert set(res) == {"ok", "data", "error"}, res
    assert res["ok"] is False, res
    assert res["data"] is None
    assert set(res["error"]) == {"code", "message"}
    if code is not None:
        assert res["error"]["code"] == code, res
    return res["error"]


def _logar_admin(bridge):
    return _ok(bridge.login(ADMIN_LOGIN, ADMIN_SENHA))


# ---------------------------------------------------------------------------
# Envelope e serialização
# ---------------------------------------------------------------------------

class TestEnvelope:
    def test_decimal_vira_string_e_datas_iso(self):
        dados = {
            "preco": Decimal("19.90"),
            "zero": Decimal("0.00"),
            "nan": Decimal("NaN"),
            "dia": datetime.date(2026, 11, 25),
            "quando": datetime.datetime(2026, 11, 25, 14, 30, 5),
            "floats": [1.5, float("inf"), float("nan")],
            1: {"aninhado": (Decimal("2.5"),)},
            "conjunto": {3, 1, 2},
        }
        res = envelope_ok(dados)
        assert res["ok"] is True and res["error"] is None
        d = res["data"]
        assert d["preco"] == "19.90" and isinstance(d["preco"], str)
        assert d["zero"] == "0.00"
        assert d["nan"] is None
        assert d["dia"] == "2026-11-25"
        assert d["quando"] == "2026-11-25T14:30:05"
        assert d["floats"] == [1.5, None, None]
        assert d["1"] == {"aninhado": ["2.5"]}
        assert d["conjunto"] == [1, 2, 3]
        # Tem que ser JSON estrito (sem NaN/Infinity)
        json.loads(json.dumps(res, allow_nan=False))

    def test_dataframe_vira_lista_de_dicts(self):
        import pandas as pd

        df = pd.DataFrame([{"id": 1, "nome": "A"}, {"id": 2, "nome": "B"}])
        assert to_jsonable(df) == [{"id": 1, "nome": "A"}, {"id": 2, "nome": "B"}]

    def test_envelope_fail(self):
        res = envelope_fail("X", "mensagem")
        assert res == {"ok": False, "data": None, "error": {"code": "X", "message": "mensagem"}}

    def test_excecao_nao_vaza_stacktrace_e_vai_para_o_log(self, caplog):
        @safe_call
        def explode():
            raise RuntimeError("segredo interno /caminho/arquivo.py linha 42")

        with caplog.at_level(logging.ERROR, logger="EstokeAoCubo.bridge"):
            res = explode()
        err = _fail(res, "INTERNAL_ERROR")
        assert "segredo" not in err["message"]
        assert "Traceback" not in json.dumps(res)
        assert any("segredo interno" in (r.exc_text or "") or r.exc_info for r in caplog.records)

    def test_mapeamento_de_erros_de_dominio(self):
        @safe_call
        def negado():
            raise PermissionError("Sem permissão.")

        @safe_call
        def invalido():
            raise ValueError("Campo inválido.")

        assert _fail(negado(), "PERMISSION_DENIED")["message"] == "Sem permissão."
        assert _fail(invalido(), "INVALID_INPUT")["message"] == "Campo inválido."


# ---------------------------------------------------------------------------
# Inicialização da bridge e eventos
# ---------------------------------------------------------------------------

class TestInicializacao:
    def test_bridge_expoe_apenas_metodos_publicos_esperados(self, bridge):
        publicos = {n for n in dir(bridge) if not n.startswith("_")}
        assert publicos == METODOS_PUBLICOS
        for nome in METODOS_PUBLICOS:
            assert callable(getattr(bridge, nome))

    def test_ping_e_bootstrap_inicial(self, bridge):
        assert _ok(bridge.ping()) == {"pong": True}
        boot = _ok(bridge.get_bootstrap())
        assert boot["app"] == "Estoke ao Cubo"
        assert boot["ui"] == "web"
        assert boot["theme"] == "light"
        assert boot["first_access_required"] is True
        assert boot["nicho"] is None
        assert boot["session"] == {"autenticado": False, "usuario": None, "loja_ativa_id": None}
        assert isinstance(boot["version"], str) and boot["version"]

    def test_app_desktop_importa_sem_abrir_janela(self):
        import app_desktop

        assert callable(app_desktop.main)
        assert hasattr(app_desktop, "BackgroundWorkers")

    def test_main_mantem_legado_como_padrao(self):
        src = (RAIZ / "main.py").read_text(encoding="utf-8")
        assert '"--ui=web" in sys.argv[1:]' in src
        assert '"--self-test" not in sys.argv[1:]' in src

    def test_eventos_entregues_a_janela(self, bridge):
        janela = FakeWindow()
        bridge._bind_window(janela)
        _ok(bridge.set_theme("dark"))
        bridge._events.close()  # drena a fila
        assert any("theme_changed" in s and '"dark"' in s for s in janela.scripts)
        assert all(s.startswith("window.__estokeEmit") for s in janela.scripts)

    def test_build_emit_script_escapa_e_valida_nome(self):
        script = build_emit_script("x_evt", {"t": "</script><b>"})
        assert "</script>" not in script
        with pytest.raises(ValueError):
            build_emit_script("nome inválido; alert(1)", None)

    def test_toggle_fullscreen(self, bridge):
        _fail(bridge.toggle_fullscreen(), "NO_WINDOW")
        janela = FakeWindow()
        bridge._bind_window(janela)
        assert _ok(bridge.toggle_fullscreen()) == {"fullscreen": True}
        assert _ok(bridge.toggle_fullscreen()) == {"fullscreen": False}
        assert janela.fullscreen_calls == 2

    def test_check_updates_offline_nao_quebra(self, bridge):
        info = _ok(bridge.check_updates_status())
        assert info["available"] is False
        assert info["current_version"]
        assert info["error"]


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------

class TestAuth:
    def test_login_logout_e_sessao(self, bridge):
        _fail(bridge.login(ADMIN_LOGIN, "errada"), "AUTH_FAILED")
        _fail(bridge.login("", ""), "INVALID_INPUT")

        sessao = _logar_admin(bridge)
        assert sessao["autenticado"] is True
        assert sessao["usuario"]["perfil"] == "admin"
        assert sessao["usuario"]["emoji"] == "👑"
        assert set(sessao["usuario"]) == {"id", "nome", "perfil", "loja_id", "emoji"}
        assert "senha_hash" not in json.dumps(sessao)
        assert sessao["loja_ativa_id"] == 1
        assert "trocar_senha" in sessao

        atual = _ok(bridge.get_current_session())
        assert atual["autenticado"] is True

        assert _ok(bridge.logout()) == {"autenticado": False}
        assert _ok(bridge.get_current_session())["autenticado"] is False
        _fail(bridge.list_lojas(), "NOT_AUTHENTICATED")

    def test_login_chama_callback_pos_login(self, isolated_db):
        chamados = []
        b = EstokeBridge(db=isolated_db, update_fetcher=_fetcher_offline, on_session_started=chamados.append)
        try:
            _ok(b.login(ADMIN_LOGIN, ADMIN_SENHA))
            assert len(chamados) == 1 and chamados[0]["perfil"] == "admin"
        finally:
            b._shutdown()

    def test_supervisor_admin_aprova_e_vendedor_nega(self, bridge, isolated_db):
        ok, msg = isolated_db.adicionar_usuario("Vend", "vend1", SENHA_FORTE, perfil="vendedor", loja_id=1)
        assert ok, msg

        _fail(bridge.autorizar_supervisor(ADMIN_LOGIN, ADMIN_SENHA, "cancelar_venda"), "NOT_AUTHENTICATED")
        _logar_admin(bridge)

        aprovado = _ok(bridge.autorizar_supervisor(ADMIN_LOGIN, ADMIN_SENHA, "cancelar_venda", "teste", True))
        assert aprovado["approved"] is True
        assert aprovado["supervisor_perfil"] in ("admin", "gerente")
        assert aprovado["motivo"] == "teste"

        _fail(bridge.autorizar_supervisor("vend1", SENHA_FORTE, "cancelar_venda"), "PERMISSION_DENIED")
        _fail(bridge.autorizar_supervisor(ADMIN_LOGIN, ADMIN_SENHA, "cancelar_venda", "", True), "INVALID_INPUT")
        _fail(bridge.autorizar_supervisor(ADMIN_LOGIN, ADMIN_SENHA, ""), "INVALID_INPUT")


# ---------------------------------------------------------------------------
# Primeiro acesso e nichos
# ---------------------------------------------------------------------------

class TestNichos:
    def test_catalogo_tem_18_nichos(self, bridge):
        cat = _ok(bridge.get_first_access_catalog())
        assert len(cat["nichos"]) == 18
        chaves = [n["key"] for n in cat["nichos"]]
        assert len(set(chaves)) == 18
        for n in cat["nichos"]:
            assert {"key", "title", "icon", "desc", "color"} <= set(n)
        assert cat["ja_configurado"] is False

    def test_primeiro_acesso_exige_aceite_e_nicho_valido(self, bridge):
        _fail(bridge.complete_first_access("pet_shop_agro", False), "INVALID_INPUT")
        _fail(bridge.complete_first_access("pet_shop_agro", "true"), "INVALID_INPUT")
        _fail(bridge.complete_first_access("nao_existe", True), "INVALID_INPUT")

        assert _ok(bridge.complete_first_access("pet_shop_agro", True)) == {"nicho": "pet_shop_agro"}
        boot = _ok(bridge.get_bootstrap())
        assert boot["first_access_required"] is False
        assert boot["nicho_info"]["key"] == "pet_shop_agro"
        _fail(bridge.complete_first_access("varejo", True), "ALREADY_CONFIGURED")

    @pytest.mark.parametrize("nicho", [n["key"] for n in NichoService().listar_nichos()])
    def test_menu_por_nicho(self, bridge, nicho):
        import config

        config.set_business_type(nicho)
        _logar_admin(bridge)
        menu = _ok(bridge.get_sidebar_menu())
        assert menu["nicho"] == nicho

        ids_validos = [sid for sid, _ in SECOES_SIDEBAR]
        assert [s["id"] for s in menu["secoes"]] == [sid for sid in ids_validos if sid in {s["id"] for s in menu["secoes"]}]

        textos_menu = [i["texto"] for s in menu["secoes"] for i in s["itens"]]
        esperados = [b["texto"] for b in NichoService().get_sidebar_buttons(nicho)]
        assert sorted(textos_menu) == sorted(esperados)
        for texto in esperados:
            assert texto in _SECAO_POR_BOTAO, f"Botão sem seção mapeada: {texto}"

        slugs = [i["id"] for s in menu["secoes"] for i in s["itens"]]
        assert len(slugs) == len(set(slugs)), "slugs duplicados quebram as rotas"
        assert all(i["permitido"] for s in menu["secoes"] for i in s["itens"])  # admin vê tudo

        assert [t["texto"] for t in menu["topbar"]] == list(TOPBAR_BOTOES)
        assert [t["id"] for t in menu["topbar"]] == ["adicionar", "vender", "servico", "lista", "relatorio", "dre"]

    def test_menu_sem_login_nao_permite_nada(self, bridge):
        menu = _ok(bridge.get_sidebar_menu())
        assert not any(i["permitido"] for s in menu["secoes"] for i in s["itens"])
        assert not any(t["permitido"] for t in menu["topbar"])

    def test_menu_vendedor_respeita_rbac(self, bridge, isolated_db):
        ok, msg = isolated_db.adicionar_usuario("Vend", "vend2", SENHA_FORTE, perfil="vendedor", loja_id=1)
        assert ok, msg
        _ok(bridge.login("vend2", SENHA_FORTE))
        menu = _ok(bridge.get_sidebar_menu())
        permitidos = {i["texto"] for s in menu["secoes"] for i in s["itens"] if i["permitido"]}
        assert "CONFIG" not in permitidos

    def test_slugify(self):
        assert slugify("CONTAS PAGAR") == "contas-pagar"
        assert slugify("Nº SÉRIE") == "no-serie"
        assert slugify("SERVIÇO") == "servico"
        assert slugify("") == "item"


# ---------------------------------------------------------------------------
# Tema e loja
# ---------------------------------------------------------------------------

class TestTemaELoja:
    def test_tema_persistido_em_config_json(self, bridge, tmp_path):
        assert _ok(bridge.get_theme()) == "light"
        assert _ok(bridge.set_theme("DARK")) == "dark"
        assert _ok(bridge.get_theme()) == "dark"
        cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert cfg["ui_theme"] == "dark"
        _fail(bridge.set_theme("rosa"), "INVALID_INPUT")
        assert _ok(bridge.get_theme()) == "dark"

    def test_admin_troca_loja(self, bridge, isolated_db):
        nova = isolated_db.adicionar_loja("Filial Centro")
        _logar_admin(bridge)
        lojas = _ok(bridge.list_lojas())
        assert lojas["pode_trocar"] is True
        assert {l["id"] for l in lojas["lojas"]} >= {1, nova}
        assert _ok(bridge.set_loja_ativa(nova)) == {"loja_ativa_id": nova}
        assert _ok(bridge.get_current_session())["loja_ativa_id"] == nova
        _fail(bridge.set_loja_ativa(99999), "INVALID_INPUT")
        _fail(bridge.set_loja_ativa("abc"), "INVALID_INPUT")

    def test_vendedor_preso_a_propria_loja(self, bridge, isolated_db):
        nova = isolated_db.adicionar_loja("Filial Norte")
        ok, msg = isolated_db.adicionar_usuario("Vend", "vend3", SENHA_FORTE, perfil="vendedor", loja_id=1)
        assert ok, msg
        _ok(bridge.login("vend3", SENHA_FORTE))
        lojas = _ok(bridge.list_lojas())
        assert lojas["pode_trocar"] is False
        assert [l["id"] for l in lojas["lojas"]] == [1]
        _fail(bridge.set_loja_ativa(nova), "PERMISSION_DENIED")


# ---------------------------------------------------------------------------
# Contratos estáticos
# ---------------------------------------------------------------------------

class TestContratos:
    def test_web_sem_http_https(self):
        assert WEB.is_dir()
        ofensores = []
        for arq in WEB.rglob("*"):
            if not arq.is_file() or arq.suffix.lower() in {".png", ".ico", ".jpg", ".woff2"}:
                continue
            texto = arq.read_text(encoding="utf-8", errors="ignore")
            if "http://" in texto or "https://" in texto:
                ofensores.append(str(arq.relative_to(RAIZ)))
        assert ofensores == [], f"URLs externas em web/: {ofensores}"

    def test_web_sem_cdns_conhecidos(self):
        proibidos = ("unpkg", "jsdelivr", "cdnjs", "fonts.googleapis", "fonts.gstatic")
        for arq in WEB.rglob("*"):
            if arq.is_file() and arq.suffix.lower() in {".html", ".js", ".css", ".md"}:
                texto = arq.read_text(encoding="utf-8").lower()
                for p in proibidos:
                    assert p not in texto or arq.name == "README.md", f"{p} em {arq}"

    def test_get_web_path_index_existe(self):
        index = paths.get_web_path("index.html")
        assert os.path.isfile(index)
        html = pathlib.Path(index).read_text(encoding="utf-8")
        assert "Content-Security-Policy" in html
        assert 'lang="pt-BR"' in html

    def test_assets_obrigatorios_existem(self):
        obrigatorios = [
            "index.html", "vendor/README.md", "assets/icon-cubo.png",
            "styles/tokens.css", "styles/base.css", "styles/components.css",
            "js/bridge-client.js", "js/state.js", "js/router.js", "js/hotkeys.js",
            "js/pages/login.js", "js/pages/first-access.js", "js/pages/shell.js",
        ]
        faltando = [p for p in obrigatorios if not (WEB / p).is_file()]
        assert faltando == []

    def test_scripts_do_index_existem(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        import re

        refs = re.findall(r'(?:src|href)="([^"#:]+)"', html)
        assert refs, "index.html sem referências locais"
        for ref in refs:
            assert (WEB / ref).is_file(), f"Referência quebrada em index.html: {ref}"

    def test_imports_js_relativos_resolvem(self):
        import re

        for arq in (WEB / "js").rglob("*.js"):
            src = arq.read_text(encoding="utf-8")
            for ref in re.findall(r'from\s+"(\.[^"]+)"', src):
                alvo = (arq.parent / ref).resolve()
                assert alvo.is_file(), f"{arq.name} importa {ref} inexistente"

    def test_services_nao_importa_ui_nem_webview(self):
        proibidos = {"tkinter", "customtkinter", "webview", "views", "bridge", "app_desktop", "main"}
        violacoes = []
        for arq in sorted((RAIZ / "services").rglob("*.py")):
            tree = ast.parse(arq.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                nomes = []
                if isinstance(node, ast.Import):
                    nomes = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    nomes = [node.module]
                for nome in nomes:
                    if nome.split(".")[0] in proibidos:
                        violacoes.append(f"{arq.name}:{node.lineno} -> {nome}")
        assert violacoes == [], violacoes

    def test_bridge_nao_importa_ui(self):
        proibidos = {"tkinter", "customtkinter", "views"}
        for arq in sorted((RAIZ / "bridge").rglob("*.py")):
            tree = ast.parse(arq.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(a.name.split(".")[0] in proibidos for a in node.names), arq
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    assert node.module.split(".")[0] not in proibidos, arq

    def test_requirements_fixa_pywebview(self):
        req = (RAIZ / "requirements.txt").read_text(encoding="utf-8")
        assert "pywebview==" in req
