"""
Testes do Lote 3 da F1b — Cancelamento assíncrono de NFC-e fora do lock via OutboxService.
Todas as chamadas à Focus NFe são 100% mockadas (proibido acesso à rede real).
"""
from __future__ import annotations

import datetime as dt
import socket
import threading
import time

import pytest

from database import Database
from services.fiscal_service import FiscalService, FocusNFeProvider
from services.outbox_service import OutboxService


@pytest.fixture
def fiscal_env(tmp_path, monkeypatch):
    """Cria Database isolado, caixa aberto, produto cadastrado e OutboxService."""
    monkeypatch.setenv("ESTOKE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NFCE_PRAZO_CANCELAMENTO_MIN", raising=False)
    db = Database(db_name="test_cancelamento_fiscal.db")
    db.salvar_config_nota(
        {
            "empresa_nome": "Loja Fiscal Teste",
            "empresa_cnpj": "12.345.678/0001-99",
            "empresa_uf": "SP",
            "token_api": "TOK_FAKE_TEST",
            "ambiente": "homologacao",
        }
    )
    ok_cx, _ = db.abrir_caixa(100.0, observacao="Caixa Teste Fiscal", usuario_id=1)
    assert ok_cx is True
    db.adicionar_produto("Produto Fiscal", 10, 50.0, categoria="Geral", preco_compra=20.0)
    prod_id = int(db.listar_produtos().iloc[0]["id"])
    with db._lock:
        db.conn.execute("UPDATE produtos SET ncm = '22021000' WHERE id = ?", (prod_id,))
        db.conn.commit()
    outbox = OutboxService(db, connectivity_checker=lambda: True, poll_interval_seconds=0.5)
    yield db, outbox, prod_id
    outbox.stop_worker(timeout=2.0)
    if not getattr(db, "_closed", False):
        db.close()


def _registrar_venda(db: Database, prod_id: int, tx_id: str, qtd: int = 2, forma: str = "Dinheiro", cliente: str = "Consumidor Final"):
    ok, msg = db.registrar_venda_detalhada(
        itens=[{"id": prod_id, "nome": "Produto Fiscal", "qtd": qtd, "preco": 50.0}],
        total=round(50.0 * qtd, 2),
        cliente=cliente,
        forma_pagamento=forma,
        usuario_id=1,
        transacao_id=tx_id,
    )
    assert ok is True, msg


def _resp_autorizada(chave: str = "35261112345678000199650010000001001000001001"):
    return 200, {
        "status": "autorizado",
        "chave_nfe": chave,
        "numero": "100",
        "serie": "1",
        "protocolo": "135260000999111",
    }


class TestCancelamentoFiscalAssincrono:
    """Cobre os 10 cenários exigidos do Lote 3."""

    def test_01_autorizada_delete_ok_vira_cancelado(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http(req, timeout):
            chamadas.append(req.get_method())
            if req.get_method() == "POST":
                return _resp_autorizada()
            if req.get_method() == "DELETE":
                return 200, {"status": "cancelado", "mensagem_sefaz": "Evento registrado e vinculado."}
            return 200, {}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c01")
        assert fsrv.emitir_para_venda("tx_c01").status == "autorizado"

        ok, msg = db.cancelar_venda("tx_c01", motivo="Desistencia do cliente", usuario_id=1)
        assert ok is True
        assert "Monitor Outbox" in msg
        assert fsrv.obter_documento("tx_c01")["status"] == "cancelamento_pendente"
        assert chamadas == ["POST"]  # Zero HTTP na thread chamadora

        outbox.processar_pendentes()
        assert chamadas == ["POST", "DELETE"]
        assert fsrv.obter_documento("tx_c01")["status"] == "cancelado"

    def test_02_timeout_depois_get_ja_cancelada_sem_segundo_delete(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http(req, timeout):
            metodo = req.get_method()
            chamadas.append(metodo)
            if metodo == "POST":
                return _resp_autorizada()
            if metodo == "DELETE":
                raise socket.timeout("timed out")
            if metodo == "GET":
                return 200, {"status": "cancelado", "mensagem_sefaz": "NFC-e já cancelada na SEFAZ."}
            return 200, {}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c02")
        fsrv.emitir_para_venda("tx_c02")
        db.cancelar_venda("tx_c02", motivo="Timeout depois GET", usuario_id=1)

        t0 = dt.datetime.now()
        outbox.processar_pendentes(now_dt=t0)
        assert fsrv.obter_documento("tx_c02")["status"] == "cancelamento_pendente"
        assert chamadas == ["POST", "DELETE"]

        # Retentativa 2 (após 15s): consulta GET, vê já cancelada e conclui SEM segundo DELETE
        outbox.processar_pendentes(now_dt=t0 + dt.timedelta(seconds=20))
        assert chamadas == ["POST", "DELETE", "GET"]
        assert fsrv.obter_documento("tx_c02")["status"] == "cancelado"

    def test_03_timeout_nunca_vira_cancelamento_recusado_e_respeita_agenda_backoff(self, fiscal_env):
        db, outbox, pid = fiscal_env

        def fake_http(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            raise TimeoutError("Focus NFe timeout")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        # Confirma agenda de retentativas do tipo nfce_cancelamento: 15s, 60s, 3min, 5min, 5min
        assert [outbox.calcular_backoff_para_tipo("nfce_cancelamento", i) for i in range(1, 6)] == [15, 60, 180, 300, 300]

        _registrar_venda(db, pid, "tx_c03")
        fsrv.emitir_para_venda("tx_c03")
        db.cancelar_venda("tx_c03", motivo="Teste timeout", usuario_id=1)

        outbox.processar_pendentes()
        doc = fsrv.obter_documento("tx_c03")
        assert doc["status"] == "cancelamento_pendente"
        assert doc["status"] != "cancelamento_recusado"

        itens = [i for i in outbox.listar_itens() if i["chave_idempotencia"] == "nfce_cancel:tx_c03"]
        assert len(itens) == 1
        assert itens[0]["status"] == "falha_temporaria"

    def test_04_rejeicao_4xx_vira_cancelamento_recusado_e_erro_permanente(self, fiscal_env):
        db, outbox, pid = fiscal_env

        def fake_http(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            return 422, {"status": "erro_cancelamento", "mensagem_sefaz": "Rejeição 420: Cancelamento inválido"}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c04")
        fsrv.emitir_para_venda("tx_c04")
        db.cancelar_venda("tx_c04", motivo="Teste rejeicao 4xx", usuario_id=1)

        outbox.processar_pendentes()
        assert fsrv.obter_documento("tx_c04")["status"] == "cancelamento_recusado"
        itens = [i for i in outbox.listar_itens() if i["chave_idempotencia"] == "nfce_cancel:tx_c04"]
        assert itens[0]["status"] == "erro_permanente"

    def test_05_prazo_vencido_durante_retentativas_vira_cancelamento_recusado(self, fiscal_env, monkeypatch):
        db, outbox, pid = fiscal_env
        monkeypatch.setenv("NFCE_PRAZO_CANCELAMENTO_MIN", "30")

        def fake_http(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            raise TimeoutError("Sem resposta SEFAZ")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c05")
        fsrv.emitir_para_venda("tx_c05")
        db.cancelar_venda("tx_c05", motivo="Teste prazo expirado", usuario_id=1)

        t0 = dt.datetime.now()
        outbox.processar_pendentes(now_dt=t0)
        assert fsrv.obter_documento("tx_c05")["status"] == "cancelamento_pendente"

        # Simula 35 minutos depois (> 30 min)
        outbox.processar_pendentes(now_dt=t0 + dt.timedelta(minutes=35))
        doc = fsrv.obter_documento("tx_c05")
        assert doc["status"] == "cancelamento_recusado"
        assert "prazo expirado sem confirmação da SEFAZ" in doc["mensagem_sefaz"]

        itens = [i for i in outbox.listar_itens() if i["chave_idempotencia"] == "nfce_cancel:tx_c05"]
        assert itens[0]["status"] == "erro_permanente"

    def test_06_pendente_emissao_nao_enviada_cancela_local_desarma_emissao_zero_http(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http(req, timeout):
            chamadas.append(req.get_method())
            raise ConnectionError("Offline na emissão")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c06")
        res_em = fsrv.emitir_para_venda("tx_c06")
        assert res_em.status == "pendente_emissao"
        chamadas.clear()

        ok, msg = db.cancelar_venda("tx_c06", motivo="Cancelamento offline", usuario_id=1)
        assert ok is True
        assert "NFC-e não havia sido transmitida — cancelada localmente." in msg

        # Processar apenas o job de cancelamento (sem disparar HTTP)
        with db._lock:
            row = db.conn.execute(
                "SELECT payload FROM outbox WHERE chave_idempotencia = 'nfce_cancel:tx_c06'"
            ).fetchone()
        assert row is not None
        import json as _json
        ok_h, _, perm = fsrv.processar_outbox_cancelamento(_json.loads(row[0]))
        assert ok_h is True and perm is True
        assert chamadas == []  # Zero HTTP!
        assert fsrv.obter_documento("tx_c06")["status"] == "cancelado"

        st_em = db.conn.execute("SELECT status FROM outbox WHERE chave_idempotencia = 'nfce:tx_c06'").fetchone()[0]
        assert st_em == "erro_permanente"

    def test_07_corrida_emissao_em_processamento_reagenda_e_depois_deleta(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http(req, timeout):
            chamadas.append(req.get_method())
            if req.get_method() in ("POST", "GET"):
                return 200, {"status": "processando_autorizacao", "mensagem": "Em processamento"}
            if req.get_method() == "DELETE":
                return 200, {"status": "cancelado", "mensagem_sefaz": "Cancelada após autorização"}
            return 200, {}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c07")
        res_em = fsrv.emitir_para_venda("tx_c07")
        assert res_em.status == "processando"
        chamadas.clear()

        # Cancelamento durante emissão 'processando'
        ok, _ = db.cancelar_venda("tx_c07", motivo="Corrida emissao", usuario_id=1)
        assert ok is True

        # 1ª execução do handler: vê 'processando' e reagenda sem chamar DELETE
        t0 = dt.datetime.now()
        outbox.processar_pendentes(now_dt=t0)
        assert "DELETE" not in chamadas

        # Emissão finaliza e grava 'autorizado' com protocolo
        with db._lock:
            db.conn.execute(
                "UPDATE nfce_documentos SET status = 'autorizado', protocolo = '135260000111222' WHERE transacao_id = 'tx_c07'"
            )
            db.conn.commit()

        # Próxima rodada da outbox (após 15s): relê 'autorizado' e dispara DELETE -> 'cancelado'
        # Na 2ª tentativa o handler consulta GET antes; simulamos GET retornando 'autorizado'
        def fake_http_fase2(req, timeout):
            chamadas.append(req.get_method())
            if req.get_method() == "GET":
                return _resp_autorizada()
            if req.get_method() == "DELETE":
                return 200, {"status": "cancelado", "mensagem_sefaz": "Cancelada com sucesso"}
            return 200, {}

        fsrv._explicit_provider = FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_fase2)
        outbox.processar_pendentes(now_dt=t0 + dt.timedelta(seconds=20))
        assert "DELETE" in chamadas
        assert fsrv.obter_documento("tx_c07")["status"] == "cancelado"

    def test_08_cancelar_mesma_venda_duas_vezes_gera_apenas_um_job(self, fiscal_env):
        db, outbox, pid = fiscal_env

        def fake_http(req, timeout):
            return _resp_autorizada()

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c08")
        fsrv.emitir_para_venda("tx_c08")

        ok1, _ = db.cancelar_venda("tx_c08", motivo="Primeira vez", usuario_id=1)
        assert ok1 is True
        # Segunda chamada a cancelar_venda (venda já cancelada) e chamada direta a agendar_cancelamento_nfce
        db.cancelar_venda("tx_c08", motivo="Segunda vez", usuario_id=1)
        fsrv.agendar_cancelamento_nfce("tx_c08", "Segunda tentativa", usuario_id=1)

        with db._lock:
            qtd_jobs = db.conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE chave_idempotencia = 'nfce_cancel:tx_c08'"
            ).fetchone()[0]
        assert qtd_jobs == 1

    def test_09_lock_livre_durante_delete_lento_3s(self, fiscal_env):
        db, outbox, pid = fiscal_env
        delete_iniciado = threading.Event()
        delete_concluido = threading.Event()

        def fake_http_lento(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            if req.get_method() == "DELETE":
                delete_iniciado.set()
                time.sleep(3.0)
                delete_concluido.set()
                return 200, {"status": "cancelado", "mensagem_sefaz": "Cancelada após 3s"}
            return 200, {}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_lento), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c09")
        fsrv.emitir_para_venda("tx_c09")

        outbox.start_worker()

        # 1. cancelar_venda deve retornar em < 0.5s (sem esperar os 3s do DELETE)
        t_ini_canc = time.monotonic()
        ok, _ = db.cancelar_venda("tx_c09", motivo="Teste lock livre", usuario_id=1)
        duracao_canc = time.monotonic() - t_ini_canc
        assert ok is True
        assert duracao_canc < 0.5, f"cancelar_venda demorou {duracao_canc:.3f}s (esperado < 0.5s)"

        # 2. Aguardar worker entrar no DELETE de 3s e ler produtos em outra thread em < 0.5s
        assert delete_iniciado.wait(timeout=3.0), "Worker não iniciou o DELETE a tempo"

        tempo_leitura = {}

        def _ler_produtos():
            t0 = time.monotonic()
            df = db.listar_produtos()
            tempo_leitura["duracao"] = time.monotonic() - t0
            tempo_leitura["linhas"] = len(df)

        th = threading.Thread(target=_ler_produtos)
        th.start()
        th.join(timeout=2.0)
        assert not th.is_alive(), "Thread de leitura travou aguardando db._lock durante DELETE!"
        assert tempo_leitura["duracao"] < 0.5, f"Leitura demorou {tempo_leitura['duracao']:.3f}s"
        assert tempo_leitura["linhas"] >= 1

        assert delete_concluido.wait(timeout=5.0)
        # Pequena margem para o commit final do status no banco
        for _ in range(20):
            if fsrv.obter_documento("tx_c09")["status"] == "cancelado":
                break
            time.sleep(0.05)
        assert fsrv.obter_documento("tx_c09")["status"] == "cancelado"

    def test_10_transacao_local_identica_estoque_entradas_caixa_fiado_fidelidade(self, fiscal_env):
        db, outbox, pid = fiscal_env
        db.adicionar_cliente("Joao Fidelidade", "11999990000", "joao@teste.com")
        _registrar_venda(db, pid, "tx_c10", qtd=2, forma="Fiado", cliente="Joao Fidelidade")

        # Conferir estado pré-cancelamento
        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 8
        ok, msg = db.cancelar_venda("tx_c10", motivo="Estorno integral teste", usuario_id=1)
        assert ok is True, msg

        # Estoque reposto (8 + 2 = 10)
        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 10
        # Venda marcada como Cancelada
        assert db.conn.execute("SELECT status FROM vendas WHERE transacao_id = 'tx_c10'").fetchone()[0] == "Cancelada"
        # Entrada negativa de estorno lançada
        estornos = db.conn.execute("SELECT COUNT(*) FROM entradas WHERE valor < 0 AND descricao LIKE '%Estorno venda%'").fetchone()[0]
        assert estornos >= 1
        # Registro CANCELAMENTO em auditoria_caixa
        aud_cx = db.conn.execute("SELECT COUNT(*) FROM auditoria_caixa WHERE tipo_operacao = 'CANCELAMENTO'").fetchone()[0]
        assert aud_cx >= 1
        # Integridade SHA-256 preservada
        assert db.verificar_integridade_auditoria()[0] is True
