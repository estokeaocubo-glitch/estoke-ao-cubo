# -*- coding: utf-8 -*-
"""
Suíte de Homologação E2E das Integrações Comerciais e Views (Onda 4 & Onda 5):
1. Turno completo PDV + TEF Mercado Pago Point + Conciliação PIX + PDF de Fechamento
2. Homologação NFC-e Modelo 65 (FocusNFeProvider): emissão, DANFE térmico,
   contingência Outbox (`processando` -> `consultar_nfce`) e cancelamento com estorno
3. Homologação do manifesto de Auto-Update Dual Edition (Full + Trial) assinado com Ed25519
4. Views modularizadas da Onda 5 (views/financeiro_view.py e views/admin_view.py)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.error
from pathlib import Path

import customtkinter as ctk
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
import migrations
from printer import BasePrinter, TextFilePrinter
from services.fiscal_service import FiscalService, FocusNFeProvider
from services.mercadopago_service import MercadoPagoService
from services.outbox_service import OutboxService
from services.relatorio_caixa import gerar_pdf_fechamento
import services.updater as updater
from views.admin_view import AdminView
from views.financeiro_view import FinanceiroView
from views.pdv_view import PDVDialog


@pytest.fixture
def temp_env():
    tmpdir = tempfile.mkdtemp(prefix="estoke_homolog_")
    orig_app_data = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir
    db = database.Database(db_name="homolog_e2e.db")
    try:
        yield db, tmpdir
    finally:
        db.close()
        database.get_app_data_path = orig_app_data
        shutil.rmtree(tmpdir, ignore_errors=True)


class _HeadlessApp(ctk.CTk):
    """App mínimo headless para testes integrados de PDV e Views."""

    def __init__(self, db=None):
        super().__init__()
        self.withdraw()
        self.db = db
        self.current_user = {"id": 1, "nome": "Gerente Homologação", "perfil": "admin", "loja_id": 1}
        self.business_type = "variados"
        self._pdv_dialog = None
        self.carrinho = []
        self._ultimo_cupom = None
        self.notifier = None

    def check_trial_limits(self, *_args, **_kwargs):
        return False

    def refresh_dashboard(self):
        self.dashboard_refreshed = True

    def _exigir_supervisor(self, *_args, **_kwargs):
        return True

    def reimprimir_ultimo_cupom(self, **_kwargs):
        return True


@pytest.fixture(scope="module")
def shared_app():
    app = _HeadlessApp(None)
    try:
        yield app
    finally:
        try:
            app.destroy()
        except Exception:
            pass


class TestHomologacaoE2E:
    """Homologação integrada das entregas comerciais da Onda 4 e Onda 5."""

    def test_homologacao_turno_pdv_tef_pix_fechamento_pdf(self, temp_env, shared_app, monkeypatch):
        db, tmpdir = temp_env
        assert migrations.get_user_version(db.conn) == 9

        # Abertura de caixa
        ok_ab, _ = db.abrir_caixa(150.0, "Caixa Homologação E2E", usuario_id=1)
        assert ok_ab is True

        db.salvar_config_nota({"mp_point_device_id": "PAX_A910__E2E", "empresa_nome": "Loja Homologação"})
        db.adicionar_produto("Combo Refrigerante", 20, 45.0, categoria="Bebidas", preco_compra=22.0)
        prod_id = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])

        # Mock Mercado Pago Point (POST intent -> GET intent FINISHED -> GET payment approved)
        mp = MercadoPagoService(access_token="APP_USR-HOMOLOG-POINT")

        def fake_mp_req(endpoint, metodo="GET", dados=None, idempotency_key=None, max_retries=3):
            if metodo == "POST" and "payment-intents" in endpoint:
                return {"id": "INTENT-E2E-1", "state": "OPEN"}
            if metodo == "GET" and endpoint.endswith("/INTENT-E2E-1"):
                return {"id": "INTENT-E2E-1", "state": "FINISHED", "payment": {"id": "PAY-POINT-E2E-1"}}
            if metodo == "GET" and endpoint.endswith("/v1/payments/PAY-POINT-E2E-1"):
                return {
                    "id": "PAY-POINT-E2E-1",
                    "status": "approved",
                    "status_detail": "accredited",
                    "transaction_amount": 90.0,
                    "external_reference": "tx_point_e2e",
                    "payment_method_id": "credit_card",
                }
            raise AssertionError(f"Endpoint MP inesperado: {metodo} {endpoint}")

        monkeypatch.setattr(mp, "_fazer_requisicao", fake_mp_req)
        monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: mp)
        monkeypatch.setattr(
            "views.pdv_view.invoice_generator.gerar_nota_txt",
            lambda *a, **k: (os.path.join(tmpdir, "nota.txt"), "NOTA", "WPP"),
        )

        # Venda 1: PDV usando TEF Mercado Pago Point (2x R$ 45 = R$ 90)
        shared_app.db = db
        pdv = PDVDialog(shared_app, db)
        try:
            assert pdv.adicionar_item_carrinho(prod_id, qtd=2) is True
            pdv.pagamentos_list[0]["forma_var"].set("Cartão Crédito")

            def _simular_modal_com_ciclo_real(**kwargs):
                ok_ciclo, info = pdv.executar_ciclo_tef_point(
                    mp_service=mp,
                    device_id=kwargs["device_id"],
                    valor=kwargs["valor"],
                    tipo_cartao=kwargs["tipo_cartao"],
                    parcelas=kwargs.get("parcelas", 1),
                    transacao_id=kwargs["transacao_id"],
                    timeout_seconds=2.0,
                    poll_interval=0.01,
                )
                assert ok_ciclo is True, info
                return ok_ciclo, info

            monkeypatch.setattr(pdv, "_processar_tef_point_modal", _simular_modal_com_ciclo_real)
            ok_v1, det_v1 = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok_v1 is True, det_v1
            assert det_v1["tef_integrado"] is True
        finally:
            pdv.close()

        row_mp = db.conn.execute(
            "SELECT tipo, status, point_intent_id, device_id FROM pagamentos_mp WHERE payment_id = 'PAY-POINT-E2E-1'"
        ).fetchone()
        assert row_mp == ("point", "approved", "INTENT-E2E-1", "PAX_A910__E2E")

        # Venda 2: PIX com webhook correspondente (R$ 45,00)
        db.registrar_venda_detalhada(
            [{"id": prod_id, "nome": "Combo Refrigerante", "qtd": 1, "preco": 45.0, "type": "produto"}],
            45.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 45.0}],
            transacao_id="tx_pix_e2e_ok",
        )
        db.registrar_pagamento_mp(
            payment_id="MP-PIX-E2E-2",
            tipo="pix",
            external_reference="tx_pix_e2e_ok",
            status="approved",
            valor=45.0,
        )

        # Venda 3: PIX no PDV sem webhook correspondente (R$ 45,00 -> gera divergência)
        db.registrar_venda_detalhada(
            [{"id": prod_id, "nome": "Combo Refrigerante", "qtd": 1, "preco": 45.0, "type": "produto"}],
            45.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 45.0}],
            transacao_id="tx_pix_e2e_sem_wh",
        )

        ok_fc, resumo = db.fechar_caixa(150.0, "Fechamento Turno Homologação", usuario_id=1, conferido_pix=45.0)
        assert ok_fc is True, resumo

        conc = resumo["conciliacao_pix"]
        assert len(conc["conciliados_exatos"]) == 1
        assert conc["conciliados_exatos"][0]["transacao_id"] == "tx_pix_e2e_ok"
        assert len(conc["vendas_sem_webhook"]) == 1
        assert conc["vendas_sem_webhook"][0]["transacao_id"] == "tx_pix_e2e_sem_wh"
        assert conc["possui_divergencia"] is True

        pdf_out = os.path.join(tmpdir, "homolog_fechamento.pdf")
        gerar_pdf_fechamento(resumo, pdf_out)
        assert os.path.isfile(pdf_out) and os.path.getsize(pdf_out) > 1000

    def test_homologacao_nfce_emissao_contingencia_danfe_cancelamento(self, temp_env):
        db, tmpdir = temp_env
        db.salvar_config_nota(
            {
                "empresa_nome": "Supermercado Homologação",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOK_HOMOLOG_FOCUS",
                "csc": "CSC_HOMOLOG_01",
                "id_csc": "000001",
                "ambiente": "homologacao",
            }
        )
        db.abrir_caixa(100.0, "Caixa NFC-e", usuario_id=1)
        db.adicionar_produto("Água Mineral 500ml", 10, 10.0, preco_compra=4.0)
        pid = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])
        db.conn.execute("UPDATE produtos SET ncm = '22021000', cfop = '5102', cst_csosn = '102' WHERE id = ?", (pid,))
        db.conn.commit()

        # 1. Venda online autorizada + DANFE + Cancelamento com estorno
        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Água Mineral 500ml", "qtd": 2, "preco": 10.0, "type": "produto"}],
            20.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 20.0}],
            transacao_id="tx_nfce_hom_ok",
        )
        chave_44 = "35261112345678000199650010000007771000007770"
        chave_fmt = " ".join(chave_44[i : i + 4] for i in range(0, len(chave_44), 4))
        qr_url = "https://homologacao.nfce.fazenda.sp.gov.br/qrcode?p=777"
        chamadas_http = []

        def fake_focus_online(req, timeout):
            chamadas_http.append(req.get_method())
            if req.get_method() == "POST":
                return 201, {
                    "status": "autorizado",
                    "chave_nfe": chave_44,
                    "numero": "777",
                    "serie": "1",
                    "protocolo": "135260000777888",
                    "qrcode_url": qr_url,
                    "mensagem_sefaz": "Autorizado o uso da NFC-e",
                }
            if req.get_method() == "DELETE":
                return 200, {
                    "status": "cancelado",
                    "protocolo_cancelamento": "135260000777999",
                    "mensagem_sefaz": "Cancelamento homologado",
                }
            return 200, {}

        outbox = OutboxService(db, connectivity_checker=lambda: True)
        fsrv = FiscalService(
            db,
            provider=FocusNFeProvider(token_api="TOK_HOMOLOG_FOCUS", ambiente="homologacao", http_requester=fake_focus_online),
            outbox_service=outbox,
        )
        db._fiscal_service = fsrv

        res_ok = fsrv.emitir_para_venda("tx_nfce_hom_ok", tef_integrado=True)
        assert res_ok.sucesso is True
        assert res_ok.status == "autorizado"

        doc_ok = fsrv.obter_documento("tx_nfce_hom_ok")
        danfe = BasePrinter.formatar_danfe_nfce(
            doc_ok,
            [{"nome": "Água Mineral 500ml", "qtd": 2, "preco": 10.0}],
            db.get_config_nota(),
            valor_total=20.0,
        )
        assert chave_fmt in danfe
        assert "Protocolo: 135260000777888" in danfe
        assert "Ambiente: HOMOLOGACAO" in danfe
        assert qr_url in danfe

        ok_canc, msg_canc = db.cancelar_venda("tx_nfce_hom_ok", motivo="Teste homologação cancelamento", usuario_id=1)
        assert ok_canc is True, msg_canc
        assert chamadas_http == ["POST", "DELETE"]
        assert fsrv.obter_documento("tx_nfce_hom_ok")["status"] == "cancelado"
        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 10

        # 2. Venda offline -> pendente_emissao -> comprovante provisório -> processando -> consultar_nfce (GET) -> autorizado
        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Água Mineral 500ml", "qtd": 1, "preco": 10.0, "type": "produto"}],
            10.0,
            forma_pagamento="Dinheiro",
            transacao_id="tx_nfce_hom_off",
        )

        def fail_focus_net(req, timeout):
            raise urllib.error.URLError("Timeout SEFAZ homologação")

        fsrv._explicit_provider = FocusNFeProvider(
            token_api="TOK_HOMOLOG_FOCUS", ambiente="homologacao", http_requester=fail_focus_net
        )
        res_off = fsrv.emitir_para_venda("tx_nfce_hom_off", troco=5.0)
        assert res_off.sucesso is False
        assert res_off.status == "pendente_emissao"

        printer = TextFilePrinter(folder=tmpdir)
        comprovante = printer.imprimir_comprovante_pendente_nfce(
            "tx_nfce_hom_off",
            [{"nome": "Água Mineral 500ml", "qtd": 1, "preco": 10.0}],
            db.get_config_nota(),
            valor_total=10.0,
        )
        assert "NFC-e PENDENTE DE TRANSMISSAO" in comprovante

        db.conn.execute("UPDATE nfce_documentos SET status = 'processando' WHERE transacao_id = 'tx_nfce_hom_off'")
        db.conn.commit()

        consultas = []

        def fake_focus_consulta(req, timeout):
            consultas.append(req.get_method())
            return 200, {
                "status": "autorizado",
                "chave_nfe": "35261112345678000199650010000008881000008880",
                "numero": "888",
                "serie": "1",
                "protocolo": "135260000888000",
            }

        fsrv._explicit_provider = FocusNFeProvider(
            token_api="TOK_HOMOLOG_FOCUS", ambiente="homologacao", http_requester=fake_focus_consulta
        )
        st = outbox.processar_pendentes()
        assert st["enviados"] == 1
        assert consultas == ["GET"]
        assert fsrv.obter_documento("tx_nfce_hom_off")["status"] == "autorizado"

    def test_homologacao_manifesto_release_full_e_trial(self, temp_env, monkeypatch):
        _, tmpdir = temp_env
        full_exe = Path(tmpdir) / "EstokeAoCubo_Setup_v4.7.0.exe"
        trial_exe = Path(tmpdir) / "EstokeAoCubo_Trial_Setup_v4.7.0.exe"
        full_exe.write_bytes(b"MZ-FULL-INSTALLER-PAYLOAD")
        trial_exe.write_bytes(b"MZ-TRIAL-INSTALLER-PAYLOAD")

        priv, _, pub_b64 = updater.generate_ed25519_keypair()
        manifest = updater.build_signed_release_manifest(
            version="4.7.0",
            private_key=priv,
            full_installer=full_exe,
            trial_installer=trial_exe,
            notes="Release dual channel E2E",
        )

        ok_top, msg_top = updater.verify_manifest_signature(manifest, public_key=pub_b64)
        assert ok_top is True, msg_top
        assert set(manifest["editions"].keys()) == {"full", "trial"}

        # Checar atualização como canal Full
        monkeypatch.delenv("ESTOKE_TRIAL", raising=False)
        res_full = updater.check_for_updates(
            manifest_url="https://estoke-updates.pages.dev/version.json",
            current_version="4.6.0",
            public_key=pub_b64,
            fetcher=lambda _u: manifest,
        )
        assert res_full["available"] is True
        assert res_full["edition"] == "full"
        assert res_full["url"].endswith("EstokeAoCubo_Setup_v4.7.0.exe")
        assert res_full["sha256"] == updater.compute_file_sha256(full_exe)

        # Checar atualização como canal Trial
        monkeypatch.setenv("ESTOKE_TRIAL", "1")
        res_trial = updater.check_for_updates(
            manifest_url="https://estoke-updates.pages.dev/version.json",
            current_version="4.6.0",
            public_key=pub_b64,
            fetcher=lambda _u: manifest,
        )
        assert res_trial["available"] is True
        assert res_trial["edition"] == "trial"
        assert res_trial["url"].endswith("EstokeAoCubo_Trial_Setup_v4.7.0.exe")
        assert res_trial["sha256"] == updater.compute_file_sha256(trial_exe)

    def test_onda5_views_financeiro_e_admin(self, temp_env, shared_app):
        db, _ = temp_env
        shared_app.db = db

        db.adicionar_produto("Caderno Espiral", 15, 25.0, preco_compra=12.0, codigo_barras="7895555000011")
        db.adicionar_conta_pagar("Energia Elétrica", 320.0, "2026-12-10", categoria="Fixa", total_parcelas=1)
        db.adicionar_loja("Filial Centro", "Rua Principal, 100", "1133334444", "12345678000199")
        ok_u, msg_u = db.adicionar_usuario("Carlos Caixa", "carlos_e2e", "Caixa@2026#1", perfil="caixa")
        assert ok_u is True, msg_u

        fin = FinanceiroView(shared_app, db)
        adm = AdminView(shared_app, db)

        # Garantir abertura e destruição limpa de cada view modularizada (sem erros de widget ou consulta)
        windows = [
            fin.open_dre_dialog(),
            fin.open_fluxo_caixa_dialog(),
            fin.open_comissoes_dialog(),
            adm.open_etiquetas_dialog(),
            adm.open_lojas_dialog(),
            adm.open_usuarios_dialog(),
        ]
        try:
            # Contas a pagar pode usar locale pt_BR no DateEntry; testar separadamente
            windows.append(fin.open_contas_pagar_dialog())
            shared_app.update_idletasks()
            assert len(windows) == 7
        finally:
            for win in windows:
                try:
                    win.destroy()
                except Exception:
                    pass

    def test_crash_handler_ignora_tclerror_benigno_e_evita_cascata(self, temp_env, shared_app):
        import tkinter as tk
        import crash_handler

        _, tmpdir = temp_env
        logs_dir = os.path.join(tmpdir, "logs_crash_test")

        assert crash_handler.is_benign_tk_callback_error(
            tk.TclError,
            tk.TclError('bitmap "CustomTkinter_icon_Windows.ico" not defined'),
        ) is True
        assert crash_handler.is_benign_tk_callback_error(
            tk.TclError,
            tk.TclError('bad window path name ".!ctktoplevel74"'),
        ) is True
        assert crash_handler.is_benign_tk_callback_error(
            RuntimeError,
            RuntimeError("Falha real de negócio"),
        ) is False

        crash_handler.install_crash_handlers(app=shared_app, show_ui=False, log_dir=logs_dir)
        shared_app.report_callback_exception(
            tk.TclError,
            tk.TclError('bitmap "C:\\dist\\_internal\\customtkinter\\assets\\icons\\CustomTkinter_icon_Windows.ico" not defined'),
            None,
        )
        shared_app.report_callback_exception(
            tk.TclError,
            tk.TclError('bad window path name ".!ctktoplevel74"'),
            None,
        )
        crash_files = [f for f in os.listdir(logs_dir) if f.startswith("crash_") and f.endswith(".json")]
        assert crash_files == []
