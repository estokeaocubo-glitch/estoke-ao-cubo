# -*- coding: utf-8 -*-
"""
Testes de caracterização (Etapa 1) e regressão/robustez (Etapa 2 - Lote 6)
para o fluxo completo de `PDVDialog.finalizar_venda`.

100% offline com mocks/stubs — nenhuma chamada real a TEF, SEFAZ, impressora ou balança.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
from decimal import Decimal
from types import SimpleNamespace

import customtkinter as ctk
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
from services.fiscal_service import FiscalService, FocusNFeProvider
from services.impressao_service import ImpressaoService
from services.mercadopago_service import MercadoPagoService
from services.outbox_service import OutboxService
from services.relatorio_service import RelatorioService
from views.pdv_view import PDVDialog


class _DummyApp(ctk.CTk):
    """App headless mínimo para instanciar o PDVDialog em testes."""

    def __init__(self, db=None):
        super().__init__()
        self.withdraw()
        self.db = db
        self.current_user = {"id": 1, "nome": "Operador PDV", "perfil": "admin", "loja_id": 1}
        self.business_type = "variados"
        self._pdv_dialog = None
        self.carrinho = []
        self._ultimo_cupom = None
        self.notifier = None
        self.fiscal_service = None
        self.outbox_service = None
        self.dashboard_refreshed = False

    def check_trial_limits(self, *_args, **_kwargs):
        return False

    def refresh_dashboard(self):
        self.dashboard_refreshed = True

    def _exigir_supervisor(self, *_args, **_kwargs):
        return True

    def reimprimir_ultimo_cupom(self, **_kwargs):
        return True


class _FakeEntry:
    def __init__(self, value: str = ""):
        self._val = value

    def get(self) -> str:
        return self._val

    def delete(self, *_args):
        self._val = ""

    def insert(self, _idx, val: str):
        self._val = str(val)


class _FakeFrame:
    def destroy(self):
        pass


class _FakeVar:
    def __init__(self, value: str):
        self._val = value

    def get(self) -> str:
        return self._val

    def set(self, val: str):
        self._val = val


def _make_pgto_item(forma: str, valor: str = "") -> dict:
    return {
        "forma_var": _FakeVar(forma),
        "valor_entry": _FakeEntry(valor),
        "frame": _FakeFrame(),
    }


@pytest.fixture(scope="module")
def shared_app():
    app = _DummyApp(None)
    try:
        yield app
    finally:
        try:
            app.destroy()
        except Exception:
            pass


@pytest.fixture
def env_pdv(shared_app, monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="estoke_lote6_")
    orig_app_data = database.get_app_data_path
    database.get_app_data_path = lambda *parts: os.path.join(tmpdir, *parts) if parts else tmpdir
    db = database.Database(db_name="lote6_test.db")

    # Bloquear qualquer chamada HTTP acidental
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Rede HTTP real proibida em testes!")),
    )
    monkeypatch.setattr(
        "views.pdv_view.invoice_generator.gerar_nota_txt",
        lambda itens, cli, total, **kw: (
            os.path.join(tmpdir, "cupom_teste.txt"),
            f"CUPOM TESTE TOTAL={total:.2f}",
            f"WPP TOTAL={total:.2f}",
        ),
    )

    shared_app.db = db
    shared_app.current_user = {"id": 1, "nome": "Operador PDV", "perfil": "admin", "loja_id": 1}
    shared_app._ultimo_cupom = None
    shared_app.fiscal_service = None
    shared_app.outbox_service = None
    shared_app.dashboard_refreshed = False

    db.abrir_caixa(100.0, "Caixa Lote 6", usuario_id=1, loja_id=1)
    db.adicionar_produto("Café Especial", 20, 25.00, categoria="Bebidas", preco_compra=12.00)
    db.adicionar_produto("Biscoito Integral", 30, 10.00, categoria="Alimentos", preco_compra=4.00)
    prods = db.listar_produtos()
    pid_cafe = int(prods[prods["nome"] == "Café Especial"].iloc[0]["id"])
    pid_bisc = int(prods[prods["nome"] == "Biscoito Integral"].iloc[0]["id"])

    try:
        yield {
            "db": db,
            "tmpdir": tmpdir,
            "app": shared_app,
            "pid_cafe": pid_cafe,
            "pid_bisc": pid_bisc,
        }
    finally:
        try:
            db.close()
        except Exception:
            pass
        database.get_app_data_path = orig_app_data
        shutil.rmtree(tmpdir, ignore_errors=True)


def _abrir_pdv(env) -> PDVDialog:
    return PDVDialog(env["app"], env["db"])


# ==============================================================================
# ETAPA 1 — Testes de Caracterização do Caminho Feliz Atual
# ==============================================================================


class TestCaracterizacaoCaminhoFeliz:
    """Caracteriza o comportamento observável atual do caminho feliz antes de alterar Lote 6."""

    def test_caracterizacao_venda_dinheiro(self, env_pdv):
        db = env_pdv["db"]
        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.pagamentos_list = [_make_pgto_item("Dinheiro", "")]
            pdv.entry_troco.delete(0, "end")
            pdv.entry_troco.insert(0, "60,00")

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, det
            assert det["total"] == pytest.approx(50.00)
            assert det["forma_pagamento"] == "Dinheiro"
            assert det["tef_integrado"] is False

            # Estoque baixado
            qtd_cafe = db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0]
            assert qtd_cafe == 18

            # Venda, entrada, auditoria_caixa e faturamento
            row_v = db.conn.execute(
                "SELECT valor_total, desconto, valor_dinheiro, loja_id FROM vendas WHERE transacao_id = ?",
                (det["transacao_id"],),
            ).fetchone()
            assert row_v == (50.0, 0.0, 50.0, 1)

            resumo = db.resumo_caixa_atual(usuario_id=1)
            assert resumo["total_vendas"] == pytest.approx(50.00)
            assert resumo["total_dinheiro"] == pytest.approx(50.00)
            assert resumo["esperado_dinheiro"] == pytest.approx(150.00)

            dash = RelatorioService(db).dados_dashboard(loja_id=1)
            assert dash["faturamento_hoje"] == pytest.approx(50.00)
        finally:
            pdv.close()

    def test_caracterizacao_venda_cartao_via_tef(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__CHAR", "empresa_nome": "Loja Char"})

        class FakeMP:
            def esta_configurado(self):
                return True

        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: FakeMP())

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.pagamentos_list = [_make_pgto_item("Cartão Crédito", "50.00")]

            monkeypatch.setattr(
                pdv,
                "_processar_tef_point_modal",
                lambda **kw: (
                    True,
                    {
                        "intent_id": "INT-CHAR-1",
                        "payment_id": "PAY-CHAR-1",
                        "status": "approved",
                        "valor": 50.0,
                    },
                ),
            )

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, det
            assert det["tef_integrado"] is True
            assert det["total"] == pytest.approx(50.00)

            row_mp = db.conn.execute(
                "SELECT tipo, status, external_reference, valor FROM pagamentos_mp WHERE payment_id = 'PAY-CHAR-1'"
            ).fetchone()
            assert row_mp == ("point", "approved", det["transacao_id"], 50.0)

            resumo = db.resumo_caixa_atual(usuario_id=1)
            assert resumo["total_cartao"] == pytest.approx(50.00)
        finally:
            pdv.close()

    def test_caracterizacao_venda_mista(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__MISTA", "empresa_nome": "Loja Mista"})

        class FakeMP:
            def esta_configurado(self):
                return True

        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: FakeMP())

        pdv = _abrir_pdv(env_pdv)
        try:
            # 2x Café (50) + 3x Biscoito (30) = R$ 80,00
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            assert pdv.adicionar_item_carrinho(env_pdv["pid_bisc"], qtd=3) is True
            pdv.pagamentos_list = [
                _make_pgto_item("Dinheiro", "20.00"),
                _make_pgto_item("PIX", "30.00"),
                _make_pgto_item("Cartão Débito", "30.00"),
            ]
            monkeypatch.setattr(
                pdv,
                "_processar_tef_point_modal",
                lambda **kw: (
                    True,
                    {"intent_id": "INT-MISTA-1", "payment_id": "PAY-MISTA-1", "status": "approved", "valor": 30.0},
                ),
            )

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, det
            assert det["total"] == pytest.approx(80.00)

            sums = db.conn.execute(
                """SELECT SUM(valor_dinheiro), SUM(valor_pix), SUM(valor_cartao_debito)
                   FROM vendas WHERE transacao_id = ?""",
                (det["transacao_id"],),
            ).fetchone()
            assert sums[0] == pytest.approx(20.0)
            assert sums[1] == pytest.approx(30.0)
            assert sums[2] == pytest.approx(30.0)
        finally:
            pdv.close()

    def test_caracterizacao_venda_com_desconto(self, env_pdv):
        db = env_pdv["db"]
        pdv = _abrir_pdv(env_pdv)
        try:
            # 4x Café = R$ 100,00 bruto, desconto R$ 10,00 -> R$ 90,00 líquido
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=4) is True
            pdv.tipo_desconto_var.set("R$")
            pdv.entry_desconto.delete(0, "end")
            pdv.entry_desconto.insert(0, "10,00")
            pdv.pagamentos_list = [_make_pgto_item("PIX", "")]

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, det
            assert det["total"] == pytest.approx(90.00)

            row_v = db.conn.execute(
                "SELECT valor_total, desconto FROM vendas WHERE transacao_id = ?",
                (det["transacao_id"],),
            ).fetchone()
            assert row_v == (100.0, 10.0)

            row_ent = db.conn.execute(
                "SELECT valor FROM entradas WHERE loja_id = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row_ent[0] == pytest.approx(90.0)
            assert RelatorioService(db).dados_dashboard(loja_id=1)["faturamento_hoje"] == pytest.approx(90.0)
        finally:
            pdv.close()

    def test_caracterizacao_venda_com_nfce_autorizada(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota(
            {
                "empresa_nome": "Mercado Teste",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOK_CHAR_NFCE",
                "ambiente": "homologacao",
            }
        )
        db.conn.execute("UPDATE produtos SET ncm = '09012100', cfop = '5102' WHERE id = ?", (env_pdv["pid_cafe"],))
        db.conn.commit()

        chave_44 = "35261112345678000199650010000001111000001110"

        def fake_focus(req, timeout):
            return 201, {
                "status": "autorizado",
                "chave_nfe": chave_44,
                "numero": "111",
                "serie": "1",
                "protocolo": "135260000111222",
                "qrcode_url": "https://homologacao.nfce.fazenda.sp.gov.br/qrcode?p=111",
                "mensagem_sefaz": "Autorizado o uso da NFC-e",
            }

        provider = FocusNFeProvider(token_api="TOK_CHAR_NFCE", ambiente="homologacao", http_requester=fake_focus)
        env_pdv["app"].fiscal_service = FiscalService(db, provider=provider)

        class _FakePrinter:
            def __init__(self):
                self.danfes = []

            def imprimir_danfe_nfce(self, dados_nfce, itens, config_nota, **kwargs):
                self.danfes.append((dados_nfce["chave_acesso"], kwargs.get("valor_total")))

        fake_printer = _FakePrinter()
        monkeypatch.setattr("views.pdv_view.get_printer", lambda: fake_printer)

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.var_emitir_nfce.set(True)
            pdv.pagamentos_list = [_make_pgto_item("Dinheiro", "")]

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=True)
            assert ok is True, det
            assert det["nfce"]["status"] == "autorizado"
            assert det["nfce"]["chave_acesso"] == chave_44
            assert fake_printer.danfes == [(chave_44, 50.0)]

            doc = env_pdv["app"].fiscal_service.obter_documento(det["transacao_id"])
            assert doc is not None and doc["status"] == "autorizado"
        finally:
            pdv.close()


# ==============================================================================
# ETAPA 2 — Cenários Críticos do Lote 6 (Decisões a, b, c, Idempotência, Loja, Centavos)
# ==============================================================================


class TestFalhasTEFEBancoDecisaoA:
    """Decisão (a): TEF aprovado + falha no banco -> estorno automático ou pendência com NSU e valor."""

    def test_falha_banco_apos_tef_aprovado_estorna_automaticamente_e_nao_grava_nada(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__EST", "empresa_nome": "Loja Estorno"})

        mp = MercadoPagoService(access_token="APP_USR-ESTORNO-OK")
        chamadas_http = []

        def fake_req(endpoint, metodo="GET", dados=None, idempotency_key=None, max_retries=3):
            chamadas_http.append((metodo, endpoint, dados, idempotency_key))
            if metodo == "POST" and endpoint == "/v1/payments/PAY-EST-1/refunds":
                return {"id": "REF-100", "status": "approved", "amount": 50.0}
            raise AssertionError(f"Chamada MP inesperada: {metodo} {endpoint}")

        monkeypatch.setattr(mp, "_fazer_requisicao", fake_req)
        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: mp)

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.pagamentos_list = [_make_pgto_item("Cartão Crédito", "50.00")]

            monkeypatch.setattr(
                pdv,
                "_processar_tef_point_modal",
                lambda **kw: (
                    True,
                    {
                        "intent_id": "INT-EST-1",
                        "payment_id": "PAY-EST-1",
                        "nsu": "NSU-998877",
                        "status": "approved",
                        "valor": 50.0,
                    },
                ),
            )
            # Simular falha de banco após TEF aprovado
            monkeypatch.setattr(
                db,
                "registrar_venda_detalhada",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("database is locked")),
            )

            ok, msg = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is False
            assert "estornada automaticamente" in msg
            assert "NSU-998877" in msg
            assert "50.00" in msg

            # Estorno foi chamado exatamente uma vez no MP
            assert len(chamadas_http) == 1
            assert chamadas_http[0][0] == "POST"
            assert chamadas_http[0][1] == "/v1/payments/PAY-EST-1/refunds"
            assert chamadas_http[0][2] == {"amount": 50.0}

            # Zero venda, estoque intacto, zero entrada, zero auditoria_caixa
            assert db.conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0] == 0
            assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0] == 20
            assert db.conn.execute("SELECT COUNT(*) FROM entradas").fetchone()[0] == 0
            assert db.conn.execute("SELECT COUNT(*) FROM auditoria_caixa WHERE tipo_operacao = 'VENDA'").fetchone()[0] == 0

            # Registro do estorno persistido em pagamentos_mp e historico
            row_mp = db.conn.execute("SELECT status, valor FROM pagamentos_mp WHERE payment_id = 'PAY-EST-1'").fetchone()
            assert row_mp == ("refunded", 50.0)
            hist_est = db.conn.execute(
                "SELECT acao, detalhe FROM historico WHERE acao = 'Estorno TEF Automático'"
            ).fetchone()
            assert hist_est is not None and "NSU-998877" in hist_est[1]
        finally:
            pdv.close()

    def test_falha_banco_e_falha_estorno_registra_pendencia_com_nsu_e_valor(self, env_pdv, monkeypatch, caplog):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__PEND", "empresa_nome": "Loja Pendência"})

        mp = MercadoPagoService(access_token="APP_USR-ESTORNO-FAIL")
        monkeypatch.setattr(
            mp,
            "estornar_pagamento",
            lambda **kw: {"sucesso": False, "payment_id": kw["payment_id"], "erro": "Gateway timeout 504"},
        )
        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: mp)

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.pagamentos_list = [_make_pgto_item("Cartão Débito", "50.00")]
            monkeypatch.setattr(
                pdv,
                "_processar_tef_point_modal",
                lambda **kw: (
                    True,
                    {
                        "intent_id": "INT-PEND-1",
                        "payment_id": "PAY-PEND-1",
                        "nsu": "NSU-554433",
                        "status": "approved",
                        "valor": 50.0,
                    },
                ),
            )
            monkeypatch.setattr(db, "registrar_venda_detalhada", lambda *a, **kw: (False, "Falha I/O disco"))

            with caplog.at_level(logging.CRITICAL, logger="EstokeAoCubo.pdv"):
                ok, msg = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)

            assert ok is False
            assert "estorno manual" in msg.lower()
            assert "NSU-554433" in msg
            assert "50.00" in msg

            # Log crítico contém NSU e valor
            crit_logs = [r.message for r in caplog.records if r.levelno == logging.CRITICAL]
            assert any("NSU-554433" in m and "50.00" in m for m in crit_logs)

            # Pendência persistida no estado do PDV, pagamentos_mp e historico
            assert len(pdv.ultima_pendencia_tef) == 1
            assert pdv.ultima_pendencia_tef[0]["nsu"] == "NSU-554433"
            assert pdv.ultima_pendencia_tef[0]["valor"] == pytest.approx(50.0)

            row_mp = db.conn.execute("SELECT status FROM pagamentos_mp WHERE payment_id = 'PAY-PEND-1'").fetchone()
            assert row_mp == ("estorno_pendente",)
            row_hist = db.conn.execute(
                "SELECT detalhe FROM historico WHERE acao = 'Pendência Estorno TEF' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row_hist is not None and "NSU-554433" in row_hist[0] and "50.00" in row_hist[0]

            # Nenhuma venda, baixa de estoque ou lançamento de caixa
            assert db.conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0] == 0
            assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0] == 20
            assert db.conn.execute("SELECT COUNT(*) FROM entradas").fetchone()[0] == 0
            assert db.conn.execute("SELECT COUNT(*) FROM auditoria_caixa WHERE tipo_operacao = 'VENDA'").fetchone()[0] == 0
        finally:
            pdv.close()

    def test_pre_validacao_bloqueia_antes_do_tef_quando_caixa_fechado_ou_sem_estoque(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__PRE", "empresa_nome": "Loja Pre"})
        tef_chamado = []

        class FakeMP:
            def esta_configurado(self):
                return True

        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: FakeMP())

        pdv = _abrir_pdv(env_pdv)
        try:
            # 1. Estoque insuficiente manipulado após adição -> pré-validação barra ANTES do TEF
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            db.conn.execute("UPDATE produtos SET quantidade = 1 WHERE id = ?", (env_pdv["pid_cafe"],))
            db.conn.commit()
            pdv.pagamentos_list = [_make_pgto_item("Cartão Crédito", "50.00")]
            monkeypatch.setattr(pdv, "_processar_tef_point_modal", lambda **kw: tef_chamado.append(kw) or (True, {}))

            ok, msg = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is False
            assert "Estoque insuficiente" in msg
            assert tef_chamado == []

            # 2. Caixa fechado -> pré-validação barra ANTES do TEF
            db.conn.execute("UPDATE produtos SET quantidade = 20 WHERE id = ?", (env_pdv["pid_cafe"],))
            db.conn.execute("UPDATE caixa SET status = 'fechado'")
            db.conn.commit()
            ok2, msg2 = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok2 is False
            assert "Caixa fechado" in msg2
            assert tef_chamado == []
        finally:
            pdv.close()

    def test_tef_negado_ou_cancelado_nao_grava_nada_e_multicartao_parcial_estorna_primeiro(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__NEG", "empresa_nome": "Loja Neg"})

        estornos = []

        class FakeMP:
            def esta_configurado(self):
                return True

            def estornar_pagamento(self, payment_id, valor=None, idempotency_key=None):
                estornos.append((payment_id, valor))
                return {"sucesso": True, "refund_id": "REF-MULTI-1", "payment_id": payment_id, "valor": valor}

        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: FakeMP())

        pdv = _abrir_pdv(env_pdv)
        try:
            # 1. Cartão único negado -> nada gravado
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=1) is True
            pdv.pagamentos_list = [_make_pgto_item("Cartão Crédito", "25.00")]
            monkeypatch.setattr(
                pdv,
                "_processar_tef_point_modal",
                lambda **kw: (False, {"recusado": True, "erro": "Cartão recusado pelo emissor"}),
            )
            ok1, msg1 = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok1 is False
            assert "recusado" in msg1.lower()
            assert db.conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0] == 0
            assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0] == 20

            # 2. Dois cartões: 1º aprova (R$ 15), 2º cancelado (R$ 10) -> 1º estornado e nada gravado
            pdv.pagamentos_list = [
                _make_pgto_item("Cartão Débito", "15.00"),
                _make_pgto_item("Cartão Crédito", "10.00"),
            ]
            respostas = iter([
                (True, {"intent_id": "INT-1", "payment_id": "PAY-MULTI-1", "nsu": "NSU-M1", "status": "approved", "valor": 15.0}),
                (False, {"cancelado": True, "erro": "Cancelado pelo cliente no pinpad"}),
            ])
            monkeypatch.setattr(pdv, "_processar_tef_point_modal", lambda **kw: next(respostas))

            ok2, msg2 = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok2 is False
            assert estornos == [("PAY-MULTI-1", 15.0)]
            assert "NSU-M1" in msg2
            assert db.conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0] == 0
            assert db.conn.execute("SELECT COUNT(*) FROM entradas").fetchone()[0] == 0
        finally:
            pdv.close()


class TestFalhasNFCeImpressoraDecisoesBC:
    """Decisões (b) e (c): venda mantida em falhas fiscais e de impressora, com reemissão e reimpressão."""

    def test_nfce_falha_sefaz_fora_mantem_venda_e_permite_reemissao(self, env_pdv):
        db = env_pdv["db"]
        db.salvar_config_nota(
            {
                "empresa_nome": "Loja Contingência",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOK_NFCE_CONT",
                "ambiente": "homologacao",
            }
        )
        db.conn.execute("UPDATE produtos SET ncm = '09012100', cfop = '5102' WHERE id = ?", (env_pdv["pid_cafe"],))
        db.conn.commit()

        outbox = OutboxService(db, connectivity_checker=lambda: True)
        env_pdv["app"].outbox_service = outbox

        def fail_sefaz(req, timeout):
            raise urllib.error.URLError("SEFAZ indisponível (timeout)")

        fsrv = FiscalService(
            db,
            provider=FocusNFeProvider(token_api="TOK_NFCE_CONT", ambiente="homologacao", http_requester=fail_sefaz),
            outbox_service=outbox,
        )
        env_pdv["app"].fiscal_service = fsrv

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.var_emitir_nfce.set(True)
            pdv.pagamentos_list = [_make_pgto_item("Dinheiro", "")]

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, det
            tx_id = det["transacao_id"]
            assert det["nfce"]["status"] == "pendente_emissao"
            assert det["nfce"]["contingencia"] is True
            assert det["nfce"]["reemissivel"] is True

            # Venda, estoque e caixa gravados exatamente uma vez
            assert db.conn.execute("SELECT COUNT(*) FROM vendas WHERE transacao_id = ?", (tx_id,)).fetchone()[0] == 1
            assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0] == 18
            assert db.conn.execute("SELECT COUNT(*) FROM entradas").fetchone()[0] == 1
            assert db.conn.execute("SELECT COUNT(*) FROM auditoria_caixa WHERE tipo_operacao = 'VENDA'").fetchone()[0] == 1

            # NFC-e ficou pendente no banco e na Outbox
            assert fsrv.obter_documento(tx_id)["status"] == "pendente_emissao"
            jobs = [j for j in outbox.listar_itens() if j["chave_idempotencia"] == f"nfce:{tx_id}"]
            assert len(jobs) == 1 and jobs[0]["status"] == "pendente"

            # Reemissão posterior quando SEFAZ volta -> autoriza sem duplicar venda
            chave_44 = "35261112345678000199650010000008881000008880"
            fsrv._explicit_provider = FocusNFeProvider(
                token_api="TOK_NFCE_CONT",
                ambiente="homologacao",
                http_requester=lambda req, timeout: (
                    201,
                    {
                        "status": "autorizado",
                        "chave_nfe": chave_44,
                        "numero": "888",
                        "serie": "1",
                        "protocolo": "135260000888999",
                    },
                ),
            )
            res_re = fsrv.reemitir_nfce(tx_id)
            assert res_re.sucesso is True
            assert res_re.status == "autorizado"
            assert res_re.chave_acesso == chave_44
            assert fsrv.obter_documento(tx_id)["status"] == "autorizado"
            assert db.conn.execute("SELECT COUNT(*) FROM vendas WHERE transacao_id = ?", (tx_id,)).fetchone()[0] == 1
        finally:
            pdv.close()

    def test_nfce_rejeitada_ou_excecao_mantem_venda_e_permite_reemissao(self, env_pdv):
        db = env_pdv["db"]
        db.salvar_config_nota(
            {
                "empresa_nome": "Loja Rejeição",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOK_NFCE_REJ",
                "ambiente": "homologacao",
            }
        )
        db.conn.execute("UPDATE produtos SET ncm = '09012100', cfop = '5102' WHERE id = ?", (env_pdv["pid_cafe"],))
        db.conn.commit()

        fsrv = FiscalService(
            db,
            provider=FocusNFeProvider(
                token_api="TOK_NFCE_REJ",
                ambiente="homologacao",
                http_requester=lambda req, timeout: (
                    422,
                    {"status": "erro_autorizacao", "mensagem_sefaz": "Rejeição 528: Valor do ICMS difere"},
                ),
            ),
        )
        env_pdv["app"].fiscal_service = fsrv

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=1) is True
            pdv.var_emitir_nfce.set(True)
            pdv.pagamentos_list = [_make_pgto_item("PIX", "")]

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True
            assert det["nfce"]["status"] == "rejeitado"
            assert det["nfce"]["reemissivel"] is True
            assert db.conn.execute("SELECT COUNT(*) FROM vendas WHERE transacao_id = ?", (det["transacao_id"],)).fetchone()[0] == 1

            # Reemissão após correção fiscal -> autoriza
            fsrv._explicit_provider = FocusNFeProvider(
                token_api="TOK_NFCE_REJ",
                ambiente="homologacao",
                http_requester=lambda req, timeout: (
                    201,
                    {"status": "autorizado", "chave_nfe": "35261112345678000199650010000005551000005550", "numero": "555", "protocolo": "13526555"},
                ),
            )
            res_re = fsrv.reemitir_nfce(det["transacao_id"])
            assert res_re.sucesso is True and res_re.status == "autorizado"
        finally:
            pdv.close()

    def test_impressora_falha_mantem_venda_e_permite_reimpressao(self, env_pdv, monkeypatch):
        db = env_pdv["db"]

        class _BrokenPrinter:
            def print_text(self, *a, **k):
                raise RuntimeError("Spooler indisponível / impressora desligada")

        monkeypatch.setattr("views.pdv_view.get_printer", lambda: _BrokenPrinter())

        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=1) is True
            pdv.pagamentos_list = [_make_pgto_item("Dinheiro", "")]

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=True)
            assert ok is True
            assert det["impressao_ok"] is False
            assert "Spooler indisponível" in det["impressao_erro"]
            assert det["reimpressao_disponivel"] is True

            # Venda, estoque e caixa gravados normalmente
            assert db.conn.execute("SELECT COUNT(*) FROM vendas WHERE transacao_id = ?", (det["transacao_id"],)).fetchone()[0] == 1
            assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0] == 19
            assert db.resumo_caixa_atual(usuario_id=1)["total_vendas"] == pytest.approx(25.00)

            # Cupom salvo para reimpressão
            cupom = env_pdv["app"]._ultimo_cupom
            assert cupom is not None and "CUPOM TESTE TOTAL=25.00" in cupom["text"]

            reimpressos = []

            class _WorkingPrinter:
                def print_text(self, text, logo_path=None, venda_id=None):
                    reimpressos.append((text, venda_id))

            imp_svc = ImpressaoService(db, printer_factory=lambda: _WorkingPrinter())
            res_reimp = imp_svc.reimprimir_ultimo_cupom(ultimo_cupom=cupom)
            assert res_reimp["origem"] == "sessao"
            assert len(reimpressos) == 1 and reimpressos[0][1] == det["venda_id"]
        finally:
            pdv.close()


class TestIdempotenciaLojaECentavosLote6:
    """Idempotência contra duplo clique/reenvio, estoque/caixa únicos e isolamento multi-loja em centavos."""

    def test_duplo_finalizar_sequencial_e_concorrente_gera_uma_unica_venda_e_cobranca(self, env_pdv, monkeypatch):
        db = env_pdv["db"]
        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__IDEM", "empresa_nome": "Loja Idem"})

        cobrancas_tef = []

        class FakeMP:
            def esta_configurado(self):
                return True

        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: FakeMP())

        pdv = _abrir_pdv(env_pdv)
        try:
            # 1. Duplo clique / reentrância durante o processamento do TEF na main thread
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.pagamentos_list = [_make_pgto_item("Cartão Crédito", "50.00")]
            resultados_reentrantes = []

            def _tef_com_duplo_clique(**kw):
                cobrancas_tef.append(kw["transacao_id"])
                # Simula operador clicando Finalizar/F12 de novo enquanto o TEF está em curso
                resultados_reentrantes.append(
                    pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
                )
                return True, {"intent_id": "INT-IDEM", "payment_id": "PAY-IDEM-1", "status": "approved", "valor": 50.0}

            monkeypatch.setattr(pdv, "_processar_tef_point_modal", _tef_com_duplo_clique)

            ok_v1, det_v1 = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok_v1 is True, det_v1
            assert len(resultados_reentrantes) == 1
            assert resultados_reentrantes[0][0] is False
            assert "andamento" in resultados_reentrantes[0][1].lower()
            tx_usada = det_v1["transacao_id"]
            assert len(cobrancas_tef) == 1

            # 2. Reenvio sequencial com a mesma transacao_id (ex.: retry de UI) -> idempotente, sem cobrar nem baixar novamente
            assert pdv.adicionar_item_carrinho(env_pdv["pid_cafe"], qtd=2) is True
            pdv.pagamentos_list = [_make_pgto_item("Cartão Crédito", "50.00")]
            ok_retry, det_retry = pdv.finalizar_venda(
                confirmar_automatico=True, imprimir_automatico=False, transacao_id=tx_usada
            )
            assert ok_retry is True
            assert det_retry["idempotente"] is True
            assert det_retry["transacao_id"] == tx_usada
            assert len(cobrancas_tef) == 1  # TEF não foi chamado de novo

            # Exatamente 1 venda, 1 entrada, 1 auditoria e estoque baixado apenas 1 vez (20 -> 18)
            assert db.conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0] == 1
            assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (env_pdv["pid_cafe"],)).fetchone()[0] == 18
            assert db.conn.execute("SELECT COUNT(*) FROM entradas").fetchone()[0] == 1
            assert db.conn.execute("SELECT COUNT(*) FROM auditoria_caixa WHERE tipo_operacao = 'VENDA'").fetchone()[0] == 1
        finally:
            pdv.close()

    def test_estoque_unico_centavos_e_caixa_na_loja_ativa(self, env_pdv):
        db = env_pdv["db"]
        # Criar Loja 2 com operador e caixa próprios
        loja2_id = db.adicionar_loja("Filial Centro")
        db.adicionar_usuario("Op Filial 2", "op_loja2", "Senha@12345", perfil="admin", loja_id=loja2_id)
        user2 = db.autenticar_usuario("op_loja2", "Senha@12345")
        db.abrir_caixa(50.0, "Caixa Filial 2", usuario_id=user2["id"], loja_id=loja2_id)

        # Produto com preço unitário de R$ 33,33 (3 unidades = R$ 99,99, desconto R$ 10,00 -> R$ 89,99)
        db.adicionar_produto("Item Decimal", 10, 33.33, categoria="Geral", preco_compra=10.00, loja_id=loja2_id)
        pid_dec = int(db.conn.execute("SELECT id FROM produtos WHERE nome = 'Item Decimal'").fetchone()[0])

        env_pdv["app"].current_user = user2
        pdv = _abrir_pdv(env_pdv)
        try:
            assert pdv.adicionar_item_carrinho(pid_dec, qtd=2) is True
            # Simular segunda linha avulsa do mesmo produto no carrinho para garantir baixa agregada única
            pdv.carrinho.append(
                {
                    "id": pid_dec,
                    "type": "produto",
                    "nome": "Item Decimal",
                    "preco": 33.33,
                    "preco_original": 33.33,
                    "desconto_item": 0.0,
                    "qtd": 1,
                    "row_data": {"preco_compra": 10.00},
                }
            )
            pdv.tipo_desconto_var.set("R$")
            pdv.entry_desconto.delete(0, "end")
            pdv.entry_desconto.insert(0, "10,00")
            pdv.pagamentos_list = [_make_pgto_item("PIX", "")]

            ok, det = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, det
            assert det["total"] == pytest.approx(89.99)
            assert det["loja_id"] == loja2_id

            # Estoque baixado exatamente 3 unidades (10 -> 7) mesmo com 2 linhas do mesmo produto
            qtd_pos = db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid_dec,)).fetchone()[0]
            assert qtd_pos == 7

            # Soma exata em centavos nas linhas da venda (valor_total - desconto == 89.99)
            rows_v = db.conn.execute(
                "SELECT valor_total, desconto, valor_pix, loja_id, caixa_id FROM vendas WHERE transacao_id = ?",
                (det["transacao_id"],),
            ).fetchall()
            assert len(rows_v) == 2
            soma_liq = sum(Decimal(str(r[0])) - Decimal(str(r[1])) for r in rows_v)
            soma_pix = sum(Decimal(str(r[2])) for r in rows_v)
            assert soma_liq == Decimal("89.99")
            assert soma_pix == Decimal("89.99")
            cx2 = db.caixa_aberto(usuario_id=user2["id"], loja_id=loja2_id)
            assert all(r[3] == loja2_id and r[4] == cx2["id"] for r in rows_v)

            # Exatamente 1 entrada na loja 2
            ents_l2 = db.conn.execute("SELECT valor, loja_id FROM entradas WHERE loja_id = ?", (loja2_id,)).fetchall()
            assert ents_l2 == [(89.99, loja2_id)]

            # Isolamento multi-loja nos caixas e KPIs
            assert db.resumo_caixa_atual(usuario_id=1, loja_id=1)["total_vendas"] == pytest.approx(0.0)
            assert db.resumo_caixa_atual(usuario_id=user2["id"], loja_id=loja2_id)["total_vendas"] == pytest.approx(89.99)
            rel = RelatorioService(db)
            assert rel.dados_dashboard(loja_id=1)["faturamento_hoje"] == pytest.approx(0.0)
            assert rel.dados_dashboard(loja_id=loja2_id)["faturamento_hoje"] == pytest.approx(89.99)
        finally:
            pdv.close()
