"""
Testes da bridge do Dashboard v2.0 (Marco 2):
- KPIs por período com Decimal (ROUND_HALF_UP) trafegando como string
- Isolamento multi-loja (loja ativa do admin e vendedor preso à própria loja)
- Serviços contados pela data de conclusão (migração v10), não pela data de abertura
- Gráficos (série, formas de pagamento, categorias, gastos) e banco vazio sem NaN
- CRUD de metas com evento ``metas_changed`` entregue à janela
- Alertas de validade/estoque filtrados pela loja ativa e resumo pós-login
- Contratos: assets do dashboard sem CDN e bridge do dashboard sem import de UI

Todo teste roda com ESTOKE_DATA_DIR isolado em tmp_path e sem rede (fetcher mockado).
"""
import ast
import datetime as dt
import json
import pathlib
from decimal import Decimal

import pytest

from bridge import EstokeBridge
from database import Database

RAIZ = pathlib.Path(__file__).resolve().parent.parent
WEB = RAIZ / "web"

ADMIN_LOGIN = "admincentral"
ADMIN_SENHA = "admin9438"
SENHA_FORTE = "Senha@Forte2026"

HOJE = dt.date.today()
ONTEM = HOJE - dt.timedelta(days=1)


def _fetcher_offline(_url):
    raise ConnectionError("sem rede nos testes")


class FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)

    def toggle_fullscreen(self):
        pass


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ESTOKE_DATA_DIR", str(tmp_path))
    db = Database(db_name="test_bridge_dashboard_v2.db")
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


@pytest.fixture
def duas_lojas(isolated_db):
    """Loja 1 e Filial Centro com vendas, serviço (v10), gastos e produtos em alerta."""
    db = isolated_db
    loja2 = db.adicionar_loja("Filial Centro")
    cur = db.conn.cursor()

    def produto(nome, qtd, minimo, cat, validade, loja):
        cur.execute(
            "INSERT INTO produtos (nome, quantidade, preco, categoria, data_validade, estoque_minimo, loja_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (nome, qtd, 10.0, cat, validade, minimo, loja),
        )
        return cur.lastrowid

    p1 = produto("Ração L1", 100, 5, "Rações", None, 1)
    p1_baixo = produto("Coleira L1 baixa", 1, 5, "Acessórios", None, 1)
    p2_vence = produto("Petisco L2 vencendo", 100, 5, "Petiscos", (HOJE + dt.timedelta(days=3)).isoformat(), loja2)

    def venda(prod, total, lucro, data, loja, status="Concluída", desconto=0, forma="PIX"):
        cur.execute(
            "INSERT INTO vendas (produto_id, quantidade, valor_total, desconto, lucro, data_venda, status,"
            " forma_pagamento, loja_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (prod, 1, total, desconto, lucro, data, status, forma, loja),
        )

    # Loja 1, hoje: 100 - 10 de desconto + 50,25 = 140,25 (a cancelada não entra)
    venda(p1, 100.00, 30.00, f"{HOJE.isoformat()} 10:15:00", 1, desconto=10.00, forma="Dinheiro")
    venda(p1_baixo, 50.25, 10.10, f"{HOJE.isoformat()} 11:00:00", 1)
    venda(p1, 999.00, 500.00, f"{HOJE.isoformat()} 12:00:00", 1, status="Cancelada")
    # Loja 1, ontem (janela anterior de "hoje")
    venda(p1, 100.00, 40.00, f"{ONTEM.isoformat()} 09:00:00", 1)
    # Filial Centro, hoje
    venda(p2_vence, 30.00, 12.00, f"{HOJE.isoformat()} 14:00:00", loja2)

    # Serviço aberto há 60 dias e concluído hoje: conta HOJE (data_conclusao, v10)
    cur.execute(
        "INSERT INTO servicos (data, descricao, valor, custo, status_os, data_conclusao, loja_id)"
        " VALUES (?,?,?,?,?,?,?)",
        ((HOJE - dt.timedelta(days=60)).isoformat(), "Banho e tosa", 80.00, 30.00, "Concluído",
         f"{HOJE.isoformat()} 15:00:00", 1),
    )
    # Serviço ainda em andamento: não conta
    cur.execute(
        "INSERT INTO servicos (data, descricao, valor, custo, status_os, data_conclusao, loja_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (HOJE.isoformat(), "Consulta", 500.00, 0, "Em andamento", None, 1),
    )

    cur.execute(
        "INSERT INTO gastos (data, valor, categoria, descricao, loja_id) VALUES (?,?,?,?,?)",
        (HOJE.isoformat(), 20.10, "Energia", "conta", 1),
    )
    cur.execute(
        "INSERT INTO gastos (data, valor, categoria, descricao, loja_id) VALUES (?,?,?,?,?)",
        (HOJE.isoformat(), 5.00, "Transporte", "frete", loja2),
    )
    db.conn.commit()
    return {"loja2": loja2, "p1_baixo": p1_baixo, "p2_vence": p2_vence}


# ---------------------------------------------------------------------------
# Resumo (KPIs)
# ---------------------------------------------------------------------------

class TestResumo:
    def test_exige_login(self, bridge):
        _fail(bridge.get_resumo("hoje"), "NOT_AUTHENTICATED")
        _fail(bridge.get_graficos("hoje"), "NOT_AUTHENTICATED")
        _fail(bridge.get_alertas(), "NOT_AUTHENTICATED")
        _fail(bridge.list_metas(), "NOT_AUTHENTICATED")

    def test_periodo_invalido(self, bridge):
        _logar_admin(bridge)
        for p in ("semana", "1d", "'; DROP TABLE vendas; --"):
            _fail(bridge.get_resumo(p), "INVALID_PERIOD")
            _fail(bridge.get_graficos(p), "INVALID_PERIOD")
        # Vazio/None cai no padrão (30d); maiúsculas são aceitas
        assert _ok(bridge.get_resumo(None))["periodo"] == "30d"
        assert _ok(bridge.get_resumo("HOJE"))["periodo"] == "hoje"

    def test_kpis_hoje_loja1_em_decimal(self, bridge, duas_lojas):
        _logar_admin(bridge)
        r = _ok(bridge.get_resumo("hoje"))
        assert r["loja_id"] == 1
        assert r["inicio"] == r["fim"] == HOJE.isoformat()
        # Dinheiro trafega como string com 2 casas (Decimal), nunca float
        assert r["faturamento_vendas"] == "140.25"
        assert r["faturamento_servicos"] == "80.00"
        assert r["faturamento"] == "220.25"
        assert r["lucro_bruto"] == "90.10"  # 30 + 10,10 + (80 - 30)
        assert r["gastos"] == "20.10"
        assert r["resultado"] == "70.00"
        assert r["qtd_vendas"] == 2
        assert r["servicos_concluidos"] == 1
        assert r["ticket_medio"] == "70.13"  # 140,25 / 2 = 70,125 -> ROUND_HALF_UP
        assert r["faturamento_anterior"] == "100.00"
        assert r["variacao_pct"] == "120.3"  # 120,25% -> 1 casa, HALF_UP
        for campo in ("faturamento", "lucro_bruto", "gastos", "resultado", "ticket_medio"):
            Decimal(r[campo])  # parseável, sem NaN/Infinity
        assert r["vazio"] is False

    def test_isolamento_por_loja_ativa(self, bridge, duas_lojas):
        _logar_admin(bridge)
        _ok(bridge.set_loja_ativa(duas_lojas["loja2"]))
        r = _ok(bridge.get_resumo("hoje"))
        assert r["loja_id"] == duas_lojas["loja2"]
        assert r["faturamento"] == "30.00"
        assert r["faturamento_servicos"] == "0.00"
        assert r["gastos"] == "5.00"
        assert r["qtd_vendas"] == 1
        assert r["variacao_pct"] is None  # sem base de comparação
        _ok(bridge.set_loja_ativa(1))
        assert _ok(bridge.get_resumo("hoje"))["faturamento"] == "220.25"

    def test_vendedor_ve_so_a_propria_loja(self, bridge, isolated_db, duas_lojas):
        ok, msg = isolated_db.adicionar_usuario(
            "Vend", "vend_dash", SENHA_FORTE, perfil="vendedor", loja_id=duas_lojas["loja2"]
        )
        assert ok, msg
        _ok(bridge.login("vend_dash", SENHA_FORTE))
        _fail(bridge.set_loja_ativa(1))
        r = _ok(bridge.get_resumo("hoje"))
        assert r["loja_id"] == duas_lojas["loja2"]
        assert r["faturamento"] == "30.00"

    def test_servico_conta_pela_data_de_conclusao(self, bridge, duas_lojas):
        _logar_admin(bridge)
        assert _ok(bridge.get_resumo("hoje"))["servicos_concluidos"] == 1
        # Aberto há 60 dias, mas a janela anterior de "30d" (31..60 dias atrás) não o conta
        r30 = _ok(bridge.get_resumo("30d"))
        assert r30["faturamento_servicos"] == "80.00"
        assert r30["faturamento_anterior"] == "0.00"

    def test_7d_inclui_ontem(self, bridge, duas_lojas):
        _logar_admin(bridge)
        r = _ok(bridge.get_resumo("7d"))
        assert r["faturamento"] == "320.25"
        assert r["qtd_vendas"] == 3

    def test_banco_vazio_sem_nan(self, bridge):
        _logar_admin(bridge)
        for p in ("hoje", "7d", "30d", "mes", "ano"):
            r = _ok(bridge.get_resumo(p))
            assert r["faturamento"] == "0.00"
            assert r["ticket_medio"] == "0.00"
            assert r["variacao_pct"] is None
            assert r["vazio"] is True
            texto = json.dumps(r)
            for ruim in ("NaN", "Infinity", "undefined"):
                assert ruim not in texto


# ---------------------------------------------------------------------------
# Gráficos
# ---------------------------------------------------------------------------

class TestGraficos:
    def test_hoje_por_hora(self, bridge, duas_lojas):
        _logar_admin(bridge)
        g = _ok(bridge.get_graficos("hoje"))
        assert g["passo"] == "hora"
        serie = g["serie"]
        assert len(serie["rotulos"]) == 24
        assert len(serie["faturamento"]) == len(serie["lucro"]) == 24
        assert sum(Decimal(x) for x in serie["faturamento"]) == Decimal("220.25")
        formas = {p["rotulo"]: p["valor"] for p in g["pagamentos"]}
        assert formas == {"Dinheiro": "90.00", "PIX": "50.25"}
        cats = {c["rotulo"]: c["valor"] for c in g["categorias"]}
        assert cats == {"Rações": "90.00", "Acessórios": "50.25"}
        assert g["gastos_categorias"] == [{"rotulo": "Energia", "valor": "20.10"}]
        assert g["vazio"] is False

    def test_granularidade_por_periodo(self, bridge, duas_lojas):
        _logar_admin(bridge)
        assert len(_ok(bridge.get_graficos("7d"))["serie"]["rotulos"]) == 7
        assert len(_ok(bridge.get_graficos("30d"))["serie"]["rotulos"]) == 30
        ano = _ok(bridge.get_graficos("ano"))
        assert ano["passo"] == "mes"
        assert len(ano["serie"]["rotulos"]) == HOJE.month

    def test_isolamento_por_loja(self, bridge, duas_lojas):
        _logar_admin(bridge)
        _ok(bridge.set_loja_ativa(duas_lojas["loja2"]))
        g = _ok(bridge.get_graficos("hoje"))
        assert sum(Decimal(x) for x in g["serie"]["faturamento"]) == Decimal("30.00")
        assert [c["rotulo"] for c in g["categorias"]] == ["Petiscos"]
        assert g["gastos_categorias"] == [{"rotulo": "Transporte", "valor": "5.00"}]

    def test_banco_vazio(self, bridge):
        _logar_admin(bridge)
        g = _ok(bridge.get_graficos("30d"))
        assert g["vazio"] is True
        assert g["pagamentos"] == g["categorias"] == g["gastos_categorias"] == []
        assert all(x == "0.00" for x in g["serie"]["faturamento"])


# ---------------------------------------------------------------------------
# Metas
# ---------------------------------------------------------------------------

class TestMetas:
    def test_crud_e_evento_metas_changed(self, bridge):
        _logar_admin(bridge)
        janela = FakeWindow()
        bridge._bind_window(janela)

        assert _ok(bridge.list_metas()) == []
        m = _ok(bridge.create_meta("  Vender   100 rações ", "2026-12-31", "meta do mês"))
        assert m["titulo"] == "Vender 100 rações"
        assert m["prazo"] == "2026-12-31"
        assert m["concluida"] is False

        t = _ok(bridge.toggle_meta(m["id"]))
        assert t["concluida"] is True
        assert _ok(bridge.toggle_meta(m["id"]))["concluida"] is False

        assert _ok(bridge.delete_meta(m["id"])) == {"id": m["id"], "removida": True}
        assert _ok(bridge.list_metas()) == []

        bridge._events.close()  # drena a fila
        eventos = [s for s in janela.scripts if "metas_changed" in s]
        assert len(eventos) == 4  # criada, status x2, removida
        assert any('"criada"' in s for s in eventos)
        assert any('"removida"' in s for s in eventos)

    def test_validacoes(self, bridge):
        _logar_admin(bridge)
        _fail(bridge.create_meta("   "), "INVALID_ARGUMENT")
        _fail(bridge.create_meta("x" * 121), "INVALID_ARGUMENT")
        _fail(bridge.create_meta("Meta", "31/12/2026"), "INVALID_ARGUMENT")
        _fail(bridge.toggle_meta("abc"), "INVALID_ARGUMENT")
        _fail(bridge.toggle_meta(0), "INVALID_ARGUMENT")
        _fail(bridge.toggle_meta(99999), "NOT_FOUND")
        _fail(bridge.delete_meta(99999), "NOT_FOUND")

    def test_ordenacao_pendentes_primeiro_por_prazo(self, bridge):
        _logar_admin(bridge)
        a = _ok(bridge.create_meta("A", "2027-03-01"))
        b = _ok(bridge.create_meta("B", "2026-12-01"))
        c = _ok(bridge.create_meta("C"))
        _ok(bridge.toggle_meta(b["id"]))
        assert [m["titulo"] for m in _ok(bridge.list_metas())] == ["A", "C", "B"]
        assert c["prazo"] is None and a["prazo"] == "2027-03-01"


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------

class TestAlertas:
    def test_filtrados_pela_loja_ativa(self, bridge, duas_lojas):
        _logar_admin(bridge)
        a1 = _ok(bridge.get_alertas())
        assert a1["loja_id"] == 1
        assert [p["id"] for p in a1["estoque_baixo"]] == [duas_lojas["p1_baixo"]]
        assert a1["validade"] == []
        assert a1["outbox_nfce"] == {"pendentes": 0, "falhas": 0, "total": 0}

        _ok(bridge.set_loja_ativa(duas_lojas["loja2"]))
        a2 = _ok(bridge.get_alertas())
        assert a2["estoque_baixo"] == []
        assert [p["id"] for p in a2["validade"]] == [duas_lojas["p2_vence"]]
        assert a2["validade"][0]["dias"] == 3
        assert a2["validade"][0]["vencido"] is False
        assert a2["vencidos_total"] == 0

    def test_alertas_iniciais_emitidos_a_janela(self, bridge, duas_lojas):
        _logar_admin(bridge)
        janela = FakeWindow()
        bridge._bind_window(janela)
        resumo = bridge._dash._emitir_alertas_iniciais()
        assert resumo == {"validade": 0, "vencidos": 0, "estoque_baixo": 1, "outbox_nfce": 0}
        bridge._events.close()
        assert any("dashboard:alertas_iniciais" in s for s in janela.scripts)

    def test_sem_alertas_nao_emite(self, bridge):
        _logar_admin(bridge)
        janela = FakeWindow()
        bridge._bind_window(janela)
        assert bridge._dash._emitir_alertas_iniciais() == {
            "validade": 0, "vencidos": 0, "estoque_baixo": 0, "outbox_nfce": 0,
        }
        bridge._events.close()
        assert not any("alertas_iniciais" in s for s in janela.scripts)


# ---------------------------------------------------------------------------
# Contratos do Marco 2
# ---------------------------------------------------------------------------

class TestContratosDashboard:
    ARQUIVOS_WEB = (
        WEB / "js" / "pages" / "dashboard.js",
        WEB / "js" / "charts.js",
        WEB / "styles" / "dashboard.css",
    )

    def test_assets_do_dashboard_sem_cdn(self):
        for arq in self.ARQUIVOS_WEB:
            assert arq.exists(), arq
            texto = arq.read_text(encoding="utf-8")
            assert "http://" not in texto and "https://" not in texto, arq
            assert "//cdn" not in texto, arq

    def test_bridge_do_dashboard_sem_import_de_ui(self):
        proibidos = {"tkinter", "customtkinter", "webview", "views"}
        arvore = ast.parse((RAIZ / "bridge" / "dashboard_bridge.py").read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                nomes = [a.name for a in no.names]
            elif isinstance(no, ast.ImportFrom):
                nomes = [no.module or ""]
            else:
                continue
            for nome in nomes:
                assert nome.split(".")[0] not in proibidos, nome
