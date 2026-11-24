# -*- coding: utf-8 -*-
"""
Testes unitários e de integração da ONDA 4 (100% offline com mocks):
- Etapa 1: Smoke test do PDVDialog isolado (views/pdv_view.py) + cleanup de timers after()
- Etapa 2: Conciliação PIX automática (pagamentos_mp UPSERT anti-downgrade, match exato vs aproximado sem falso positivo, PDF)
- Etapa 3: TEF / Pinpad via Mercado Pago Point (amount em centavos, FINISHED com cartão recusado vs aprovado, timeout e cancelamento)
- Etapa 4: Emissão de NFC-e Mod. 65 (FiscalProvider/FocusNFeProvider, NCM bloqueado em produção, fechamento de centavos,
  valor_troco/presenca_comprador/modalidade_frete/tipo_integracao, pendente_emissao na outbox sem tpEmis=9,
  consulta prévia quando processando, sanitização de token_api/csc e recusa SEFAZ no cancelar_venda)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error

import customtkinter as ctk
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
import migrations
from printer import BasePrinter, TextFilePrinter
from services.fiscal_service import (
    FiscalDocumentoPayload,
    FiscalItemPayload,
    FiscalPagamentoPayload,
    FiscalService,
    FocusNFeProvider,
    validar_documento_nfce,
)
from services.mercadopago_service import MercadoPagoService
from services.outbox_service import OutboxService
from services.relatorio_caixa import gerar_pdf_fechamento
from views.pdv_view import PDVDialog


@pytest.fixture
def temp_env():
    tmpdir = tempfile.mkdtemp(prefix="estoke_onda4_")
    orig_app_data = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir
    db = database.Database(db_name="onda4_test.db")
    try:
        yield db, tmpdir
    finally:
        db.close()
        database.get_app_data_path = orig_app_data
        shutil.rmtree(tmpdir, ignore_errors=True)


class _DummyApp(ctk.CTk):
    """App headless mínimo para instanciar o PDVDialog em testes."""

    def __init__(self, db=None):
        super().__init__()
        self.withdraw()
        self.db = db
        self.current_user = {"id": 1, "nome": "Operador Teste", "perfil": "admin", "loja_id": 1}
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
    app = _DummyApp(None)
    try:
        yield app
    finally:
        try:
            app.destroy()
        except Exception:
            pass


class TestEtapa1PDVSmoke:
    """Teste de fumaça validando abertura, adição de item, finalização e cleanup de timers."""

    def test_abertura_adicao_finalizacao_e_cleanup_timers(self, temp_env, shared_app, monkeypatch):
        db, tmpdir = temp_env
        assert migrations.get_user_version(db.conn) == 9

        db.abrir_caixa(100.0, "Caixa fumaça PDV", usuario_id=1)
        db.adicionar_produto(
            "Café Torrado 500g",
            10,
            18.50,
            categoria="Mercado",
            preco_compra=10.0,
            codigo_barras="7894000000001",
        )
        prod_id = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])

        monkeypatch.setattr(
            "views.pdv_view.invoice_generator.gerar_nota_txt",
            lambda *a, **k: (os.path.join(tmpdir, "nota.txt"), "NOTA", "WPP"),
        )

        shared_app.db = db
        pdv = PDVDialog(shared_app, db)
        try:
            assert shared_app._pdv_dialog is pdv
            assert len(pdv._after_ids) >= 1

            extra_fired = []
            pdv.schedule_after(300, lambda: extra_fired.append(True))

            assert pdv.adicionar_item_carrinho(prod_id, qtd=2) is True
            resumo = pdv.calcular_resumo_venda()
            assert resumo == {"subtotal": 37.0, "desconto": 0.0, "total": 37.0}

            ok, detalhes = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok is True, detalhes
            assert detalhes["total"] == 37.0
            assert detalhes["transacao_id"]
            assert pdv.carrinho == []

            qtd_pos = db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (prod_id,)).fetchone()[0]
            assert qtd_pos == 8
            assert db.obter_rascunho_pdv(1) is None

            pdv.close()
            shared_app.update()
            assert pdv._is_closed is True
            assert pdv._after_ids == set()
            assert shared_app._pdv_dialog is None
            assert extra_fired == []
        finally:
            try:
                if not pdv._is_closed:
                    pdv.close()
            except Exception:
                pass


class TestEtapa2ConciliacaoPix:
    """Valida UPSERT idempotente anti-downgrade, conciliação exata vs aproximada sem falso positivo e PDF."""

    def test_upsert_idempotente_sem_downgrade_de_approved_e_sanitizacao(self, temp_env):
        db, _ = temp_env
        ok1, _ = db.registrar_pagamento_mp(
            payment_id="MP-9001",
            tipo="pix",
            external_reference="tx_abc",
            status="approved",
            status_detail="accredited",
            valor=75.0,
            payload={"id": "MP-9001", "access_token": "APP_USR-SEGREDO", "token_api": "TOK123"},
        )
        assert ok1 is True

        # Webhook fora de ordem ('pending') NÃO pode rebaixar pagamento já 'approved'
        ok2, _ = db.registrar_pagamento_mp(
            payment_id="MP-9001",
            tipo="pix",
            external_reference="",
            status="pending",
            status_detail="pending_waiting_transfer",
            valor=75.0,
        )
        assert ok2 is True

        row = db.conn.execute(
            "SELECT status, status_detail, external_reference, payload_json FROM pagamentos_mp WHERE payment_id = 'MP-9001'"
        ).fetchone()
        assert row[0] == "approved"
        assert row[1] == "accredited"
        assert row[2] == "tx_abc"
        assert "APP_USR-SEGREDO" not in row[3]
        assert "TOK123" not in row[3]

    def test_conciliacao_exata_vs_aproximada_evita_falso_positivo_e_gera_pdf(self, temp_env):
        db, tmpdir = temp_env
        db.abrir_caixa(100.0, "Turno PIX", usuario_id=1)
        db.adicionar_produto("Item PIX", 20, 50.0, preco_compra=20.0)
        pid = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])

        # Venda 1: R$ 80,00 com transacao_id='tx_exata_80' (terá webhook com external_reference exato)
        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Item PIX", "qtd": 1, "preco": 80.0, "type": "produto"}],
            80.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 80.0}],
            transacao_id="tx_exata_80",
        )
        # Venda 2 e Venda 3: DUAS vendas de mesmo valor (R$ 50,00 cada) no mesmo turno
        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Item PIX", "qtd": 1, "preco": 50.0, "type": "produto"}],
            50.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 50.0}],
            transacao_id="tx_dup_50_a",
        )
        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Item PIX", "qtd": 1, "preco": 50.0, "type": "produto"}],
            50.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 50.0}],
            transacao_id="tx_dup_50_b",
        )

        # Registrar APENAS 1 webhook exato (R$ 80) e 1 webhook sem external_reference (R$ 50)
        db.registrar_pagamento_mp("PAY-80", tipo="pix", external_reference="tx_exata_80", status="approved", valor=80.0)
        db.registrar_pagamento_mp("PAY-50-UNICO", tipo="pix", external_reference="", status="approved", valor=50.0)

        ok_fc, resumo = db.fechar_caixa(100.0, "Fechamento com divergência PIX", usuario_id=1, conferido_pix=130.0)
        assert ok_fc is True

        conc = resumo["conciliacao_pix"]
        assert conc["total_pix_pdv"] == 180.0
        assert conc["total_pix_mp"] == 130.0
        assert conc["diferenca_total"] == -50.0
        assert len(conc["conciliados_exatos"]) == 1
        assert conc["conciliados_exatos"][0]["transacao_id"] == "tx_exata_80"
        # Apenas 1 das duas vendas de R$ 50 pode ser conciliada por aproximação; a outra DEVE ficar sem webhook
        assert len(conc["conciliados_aproximados"]) == 1
        assert len(conc["vendas_sem_webhook"]) == 1
        assert conc["vendas_sem_webhook"][0]["valor_pix"] == 50.0
        assert conc["possui_divergencia"] is True

        pdf_path = os.path.join(tmpdir, "fechamento_pix.pdf")
        gerar_pdf_fechamento(resumo, pdf_path)
        assert os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000


class TestEtapa3MercadoPagoPointTEF:
    """Valida TEF Point: centavos inteiros, FINISHED com cartão recusado vs aprovado, timeout e cancelamento."""

    def test_criar_intencao_point_centavos_e_finished_recusado_vs_aprovado(self, monkeypatch):
        mp = MercadoPagoService(access_token="APP_USR-123456789")
        chamadas = []

        def fake_req(endpoint, metodo="GET", dados=None, idempotency_key=None, max_retries=3):
            chamadas.append((metodo, endpoint, dados))
            if metodo == "POST" and "payment-intents" in endpoint:
                return {"id": "INTENT-100", "state": "OPEN"}
            if metodo == "GET" and endpoint.endswith("/INTENT-APROVADA"):
                return {"id": "INTENT-APROVADA", "state": "FINISHED", "payment": {"id": "PAY-OK"}}
            if metodo == "GET" and endpoint.endswith("/INTENT-RECUSADA"):
                return {"id": "INTENT-RECUSADA", "state": "FINISHED", "payment": {"id": "PAY-REJ"}}
            if metodo == "GET" and endpoint.endswith("/v1/payments/PAY-OK"):
                return {
                    "id": "PAY-OK",
                    "status": "approved",
                    "status_detail": "accredited",
                    "transaction_amount": 19.90,
                    "external_reference": "tx_point_1",
                    "payment_method_id": "credit_card",
                }
            if metodo == "GET" and endpoint.endswith("/v1/payments/PAY-REJ"):
                return {
                    "id": "PAY-REJ",
                    "status": "rejected",
                    "status_detail": "cc_rejected_insufficient_amount",
                    "transaction_amount": 19.90,
                    "external_reference": "tx_point_2",
                    "payment_method_id": "credit_card",
                }
            if metodo == "DELETE" and "payment-intents" in endpoint:
                return {}
            raise AssertionError(f"Endpoint não esperado: {metodo} {endpoint}")

        monkeypatch.setattr(mp, "_fazer_requisicao", fake_req)

        # 1. Valor deve ser enviado como inteiro em centavos (R$ 19,90 -> 1990) + external_reference
        res_create = mp.criar_intencao_point(
            device_id="PAX_A910__SMARTPOS123",
            valor=19.90,
            tipo_cartao="credit_card",
            parcelas=2,
            id_venda="tx_point_1",
        )
        assert res_create["sucesso"] is True
        assert res_create["amount_cents"] == 1990
        post_body = chamadas[0][2]
        assert post_body["amount"] == 1990
        assert isinstance(post_body["amount"], int)
        assert post_body["additional_info"]["external_reference"] == "tx_point_1"
        assert post_body["payment"]["installments"] == 2

        # 2. Intent FINISHED com pagamento aprovado -> aprovado=True
        st_ok = mp.consultar_intencao_point("INTENT-APROVADA")
        assert st_ok["finalizado"] is True
        assert st_ok["aprovado"] is True
        assert st_ok["recusado"] is False
        assert st_ok["payment_id"] == "PAY-OK"

        # 3. Intent FINISHED com cartão recusado na maquininha -> aprovado=False, recusado=True
        st_rej = mp.consultar_intencao_point("INTENT-RECUSADA")
        assert st_rej["finalizado"] is True
        assert st_rej["aprovado"] is False
        assert st_rej["recusado"] is True
        assert st_rej["status_pagamento"] == "rejected"

    def test_polling_timeout_cancelamento_e_integracao_pdv(self, temp_env, shared_app, monkeypatch):
        db, tmpdir = temp_env
        db.abrir_caixa(50.0, "Caixa Point", usuario_id=1)
        db.adicionar_produto("Item Point", 5, 25.0, preco_compra=10.0)
        prod_id = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])
        db.salvar_config_nota({"mp_point_device_id": "DEV-PDV-01", "empresa_nome": "Loja Point"})

        class FakeMPTimeout:
            def __init__(self):
                self.cancelados = []

            def esta_configurado(self):
                return True

            def criar_intencao_point(self, **kwargs):
                return {"sucesso": True, "intent_id": "INT-TIMEOUT-1"}

            def consultar_intencao_point(self, intent_id):
                return {"sucesso": True, "intent_id": intent_id, "state": "ON_TERMINAL", "finalizado": False, "aprovado": False}

            def cancelar_intencao_point(self, device_id, intent_id):
                self.cancelados.append((device_id, intent_id))
                return {"sucesso": True, "cancelado": True}

        monkeypatch.setattr(
            "views.pdv_view.invoice_generator.gerar_nota_txt",
            lambda *a, **k: (os.path.join(tmpdir, "nota.txt"), "NOTA", "WPP"),
        )

        shared_app.db = db
        pdv = PDVDialog(shared_app, db)
        try:
            fake_mp = FakeMPTimeout()
            # Simular timeout curto (0.06s) -> deve chamar cancelar_intencao_point para destravar a maquininha
            ok_t, info_t = pdv.executar_ciclo_tef_point(
                mp_service=fake_mp,
                device_id="DEV-01",
                valor=25.0,
                tipo_cartao="debit_card",
                transacao_id="tx_to_1",
                timeout_seconds=0.06,
                poll_interval=0.02,
            )
            assert ok_t is False
            assert info_t.get("timeout") is True
            assert fake_mp.cancelados == [("DEV-01", "INT-TIMEOUT-1")]

            # Simular fechamento/cancelamento pelo operador via cancel_event
            fake_mp2 = FakeMPTimeout()
            evt = threading.Event()
            evt.set()
            ok_c, info_c = pdv.executar_ciclo_tef_point(
                mp_service=fake_mp2,
                device_id="DEV-02",
                valor=25.0,
                tipo_cartao="credit_card",
                transacao_id="tx_cancel_1",
                timeout_seconds=10.0,
                poll_interval=0.02,
                cancel_event=evt,
            )
            assert ok_c is False
            assert info_c.get("cancelado") is True
            assert fake_mp2.cancelados == [("DEV-02", "INT-TIMEOUT-1")]

            # Simular venda em Cartão Crédito pelo PDVDialog com Pinpad aprovado -> conclui venda e grava pagamentos_mp
            pdv.adicionar_item_carrinho(prod_id, qtd=1)
            pdv.pagamentos_list[0]["forma_var"].set("Cartão Crédito")
            monkeypatch.setattr("services.mercadopago_service.get_mercadopago_service", lambda: FakeMPTimeout())
            monkeypatch.setattr(
                pdv,
                "_processar_tef_point_modal",
                lambda **kw: (True, {"intent_id": "INT-OK-PDV", "payment_id": "MP-PT-1", "status": "approved"}),
            )
            ok_v, det_v = pdv.finalizar_venda(confirmar_automatico=True, imprimir_automatico=False)
            assert ok_v is True
            assert det_v["tef_integrado"] is True
            row_pt = db.conn.execute(
                "SELECT tipo, status, point_intent_id, device_id FROM pagamentos_mp WHERE payment_id = 'MP-PT-1'"
            ).fetchone()
            assert row_pt == ("point", "approved", "INT-OK-PDV", "DEV-PDV-01")
        finally:
            pdv.close()


class TestEtapa4FiscalNFCe:
    """Valida emissão NFC-e, NCM em produção, centavos, pendente_emissao, outbox e recusa de cancelamento."""

    def test_validacao_ncm_bloqueado_em_producao_e_fechamento_centavos(self):
        doc_prod_invalido = FiscalDocumentoPayload(
            referencia="tx_ncm_zero",
            ambiente="producao",
            emitente_cnpj="12.345.678/0001-99",
            emitente_nome="Loja Fiscal Ltda",
            itens=[
                FiscalItemPayload(
                    numero_item=1,
                    produto_id=1,
                    codigo="1",
                    descricao="Produto Sem NCM",
                    ncm="00000000",
                    cfop="5102",
                    quantidade=1,
                    valor_unitario=20.0,
                    valor_bruto=20.0,
                )
            ],
            pagamentos=[FiscalPagamentoPayload("01", 25.0, tipo_integracao="2")],
            valor_total=20.0,
            valor_troco=5.0,
        )
        with pytest.raises(ValueError, match="NCM '00000000' não é permitido em produção"):
            validar_documento_nfce(doc_prod_invalido)

        # Em homologação o mesmo documento com NCM '00000000' é aceito para testes
        doc_prod_invalido.ambiente = "homologacao"
        validar_documento_nfce(doc_prod_invalido)

        # Com NCM real de 8 dígitos em produção + campos fiscais obrigatórios
        doc_prod_invalido.ambiente = "producao"
        doc_prod_invalido.itens[0].ncm = "21011110"
        validar_documento_nfce(doc_prod_invalido)

        prov = FocusNFeProvider(token_api="SEGREDO_TOKEN_FOCUS", ambiente="producao")
        focus_json = prov.build_focus_payload(doc_prod_invalido)
        assert focus_json["presenca_comprador"] == "1"
        assert focus_json["modalidade_frete"] == "9"
        assert focus_json["valor_troco"] == 5.0
        assert focus_json["formas_pagamento"][0]["tipo_integracao"] == "2"
        assert "SEGREDO_TOKEN_FOCUS" not in json.dumps(focus_json)

    def test_emissao_autorizada_danfe_e_redacao_de_segredos(self, temp_env):
        db, tmpdir = temp_env
        db.salvar_config_nota(
            {
                "empresa_nome": "Supermercado Cubo",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOKEN_SUPER_SECRETO_999",
                "csc": "CSC_SUPER_SECRETO_888",
                "id_csc": "000001",
                "ambiente": "producao",
            }
        )
        db.abrir_caixa(100.0, "Caixa Fiscal", usuario_id=1)
        db.adicionar_produto("Café Gourmet", 10, 30.0, preco_compra=15.0, codigo_barras="7891234567890")
        pid = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])
        db.conn.execute("UPDATE produtos SET ncm = '09012100', cfop = '5102' WHERE id = ?", (pid,))
        db.conn.commit()

        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Café Gourmet", "qtd": 1, "preco": 30.0, "type": "produto"}],
            30.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 30.0}],
            transacao_id="tx_nfce_ok",
        )
        db.registrar_pagamento_mp("MP-PIX-1", tipo="pix", external_reference="tx_nfce_ok", status="approved", valor=30.0)

        chave_44 = "35261112345678000199650010000001231000001230"
        captured_payloads = []

        def fake_http(req, timeout):
            if req.data:
                captured_payloads.append(json.loads(req.data.decode("utf-8")))
            return 201, {
                "status": "autorizado",
                "chave_nfe": chave_44,
                "numero": "123",
                "serie": "1",
                "protocolo": "135260000123456",
                "qrcode_url": "https://www.fazenda.sp.gov.br/nfce/qrcode?p=123",
                "mensagem_sefaz": "Autorizado o uso da NFC-e",
            }

        provider = FocusNFeProvider(token_api="TOKEN_SUPER_SECRETO_999", ambiente="producao", http_requester=fake_http)
        fsrv = FiscalService(db, provider=provider)
        res = fsrv.emitir_para_venda("tx_nfce_ok", consumidor_cpf_cnpj="12345678909", tef_integrado=True)
        assert res.sucesso is True
        assert res.status == "autorizado"
        assert res.chave_acesso == chave_44
        assert captured_payloads[0]["formas_pagamento"][0]["tipo_integracao"] == "1"

        doc_db = fsrv.obter_documento("tx_nfce_ok")
        assert doc_db is not None
        assert doc_db["status"] == "autorizado"
        assert "TOKEN_SUPER_SECRETO_999" not in doc_db["payload_json"]
        assert "CSC_SUPER_SECRETO_888" not in doc_db["payload_json"]

        printer = TextFilePrinter(folder=tmpdir)
        texto_danfe = printer.imprimir_danfe_nfce(
            doc_db,
            [{"nome": "Café Gourmet", "qtd": 1, "preco": 30.0}],
            db.get_config_nota(),
            valor_total=30.0,
        )
        assert "DANFE NFC-e" in texto_danfe
        assert "135260000123456" in texto_danfe

    def test_falha_rede_pendente_emissao_sem_tpemis9_e_outbox_consulta_processando(self, temp_env):
        db, tmpdir = temp_env
        db.salvar_config_nota(
            {
                "empresa_nome": "Loja Offline",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOK_OFF",
                "ambiente": "homologacao",
            }
        )
        db.abrir_caixa(50.0, "Caixa Offline", usuario_id=1)
        db.adicionar_produto("Biscoito", 10, 12.0, preco_compra=6.0)
        pid = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])

        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Biscoito", "qtd": 1, "preco": 12.0, "type": "produto"}],
            12.0,
            forma_pagamento="Dinheiro",
            transacao_id="tx_offline_1",
        )

        # 1. Simular falta de internet (URLError)
        def fail_net(req, timeout):
            raise urllib.error.URLError("Sem conexão com a internet")

        outbox = OutboxService(db, connectivity_checker=lambda: True)
        prov_off = FocusNFeProvider(token_api="TOK_OFF", ambiente="homologacao", http_requester=fail_net)
        fsrv = FiscalService(db, provider=prov_off, outbox_service=outbox)

        res = fsrv.emitir_para_venda("tx_offline_1", troco=8.0)
        assert res.sucesso is False
        assert res.status == "pendente_emissao"
        assert res.contingencia is True

        doc_pend = fsrv.obter_documento("tx_offline_1")
        assert doc_pend["status"] == "pendente_emissao"

        # É proibido gerar DANFE NFC-e fingindo autorização/tpEmis=9 quando pendente_emissao
        with pytest.raises(ValueError, match="DANFE NFC-e só pode ser gerado após autorização"):
            BasePrinter.formatar_danfe_nfce(doc_pend, [], db.get_config_nota(), valor_total=12.0)

        printer = TextFilePrinter(folder=tmpdir)
        txt_prov = printer.imprimir_comprovante_pendente_nfce(
            "tx_offline_1",
            [{"nome": "Biscoito", "qtd": 1, "preco": 12.0}],
            db.get_config_nota(),
            valor_total=12.0,
        )
        assert "NFC-e PENDENTE DE TRANSMISSAO" in txt_prov

        # 2. Testar handler da outbox quando status == 'processando': deve consultar antes de reenviar
        db.conn.execute("UPDATE nfce_documentos SET status = 'processando' WHERE transacao_id = 'tx_offline_1'")
        db.conn.commit()

        metodos_chamados = []

        def fake_consulta_ok(req, timeout):
            metodos_chamados.append(req.get_method())
            return 200, {
                "status": "autorizado",
                "chave_nfe": "35261112345678000199650010000009991000009990",
                "numero": "999",
                "serie": "1",
                "protocolo": "135269999999999",
            }

        fsrv._explicit_provider = FocusNFeProvider(
            token_api="TOK_OFF", ambiente="homologacao", http_requester=fake_consulta_ok
        )
        st_outbox = outbox.processar_pendentes()
        assert st_outbox["enviados"] == 1
        assert metodos_chamados == ["GET"]
        assert fsrv.obter_documento("tx_offline_1")["status"] == "autorizado"

    def test_cancelar_venda_trata_recusa_sefaz_30min_sem_corromper_banco(self, temp_env):
        db, _ = temp_env
        db.salvar_config_nota(
            {
                "empresa_nome": "Loja Cancelamento",
                "empresa_cnpj": "12345678000199",
                "token_api": "TOK_CANC",
                "ambiente": "producao",
            }
        )
        db.abrir_caixa(100.0, "Caixa Cancelamento", usuario_id=1)
        db.adicionar_produto("Produto Cancelável", 10, 40.0, preco_compra=20.0)
        pid = int(db.conn.execute("SELECT id FROM produtos LIMIT 1").fetchone()[0])
        db.conn.execute("UPDATE produtos SET ncm = '19053100' WHERE id = ?", (pid,))
        db.conn.commit()

        db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Produto Cancelável", "qtd": 2, "preco": 40.0, "type": "produto"}],
            80.0,
            forma_pagamento="Dinheiro",
            transacao_id="tx_canc_30m",
        )
        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 8

        def fake_http_sefaz(req, timeout):
            if req.get_method() == "POST":
                return 201, {
                    "status": "autorizado",
                    "chave_nfe": "35261112345678000199650010000003211000003210",
                    "numero": "321",
                    "serie": "1",
                    "protocolo": "135260000321321",
                }
            if req.get_method() == "DELETE":
                return 422, {
                    "status": "erro_cancelamento",
                    "mensagem_sefaz": "Rejeição 501: Prazo de cancelamento superior ao previsto na legislação (30 min)",
                }
            return 200, {}

        fsrv = FiscalService(
            db,
            provider=FocusNFeProvider(token_api="TOK_CANC", ambiente="producao", http_requester=fake_http_sefaz),
        )
        db._fiscal_service = fsrv

        res_em = fsrv.emitir_para_venda("tx_canc_30m")
        assert res_em.status == "autorizado"

        # Cancelar a venda: estorno local deve concluir e recusa SEFAZ fica registrada
        ok_canc, msg_canc = db.cancelar_venda("tx_canc_30m", motivo="Cliente desistiu da compra", usuario_id=1)
        assert ok_canc is True
        assert "Atenção fiscal" in msg_canc
        assert "Rejeição 501" in msg_canc

        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 10
        assert db.conn.execute("SELECT status FROM vendas WHERE transacao_id = 'tx_canc_30m'").fetchone()[0] == "Cancelada"

        doc_pos = fsrv.obter_documento("tx_canc_30m")
        assert doc_pos["status"] == "cancelamento_recusado"
        assert "501" in doc_pos["mensagem_sefaz"]
        assert db.verificar_integridade_auditoria()[0] is True
