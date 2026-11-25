"""
Testes do Lote 3 da F1b (+ commit corretivo pós-review) — Cancelamento assíncrono
de NFC-e fora do lock via OutboxService.
Todas as chamadas à Focus NFe são 100% mockadas; urllib.request.urlopen é bloqueado no fixture.
"""
from __future__ import annotations

import datetime as dt
import socket
import threading
import time
import urllib.request

import pytest

from database import Database
from services.fiscal_service import (
    FiscalService,
    FocusNFeProvider,
    classificar_resposta_cancelamento,
)
from services.outbox_service import OutboxService


@pytest.fixture
def fiscal_env(tmp_path, monkeypatch):
    """Cria Database isolado, bloqueia rede real via urlopen e configura OutboxService."""
    def _bloquear_rede_real(*args, **kwargs):
        raise AssertionError("Acesso HTTP real (urllib.request.urlopen) proibido nos testes fiscais!")

    monkeypatch.setattr(urllib.request, "urlopen", _bloquear_rede_real)
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
    db.adicionar_produto("Produto Fiscal", 50, 50.0, categoria="Geral", preco_compra=20.0)
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
    """Cobre os cenários 01 a 20 do Lote 3 + correções do review."""

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

    def test_05_esgotado_max_tentativas_sem_resposta_vira_cancelamento_recusado_contador(self, fiscal_env):
        db, outbox, pid = fiscal_env

        def fake_http(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            raise TimeoutError("Sem resposta SEFAZ")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c05")
        fsrv.emitir_para_venda("tx_c05")
        db.cancelar_venda("tx_c05", motivo="Teste esgotar max tentativas", usuario_id=1)

        # Reduz max_tentativas do job para 2 para testar exaustão de tentativas sem resposta da SEFAZ
        with db._lock:
            db.conn.execute("UPDATE outbox SET max_tentativas = 2 WHERE chave_idempotencia = 'nfce_cancel:tx_c05'")
            db.conn.commit()

        t0 = dt.datetime.now()
        outbox.processar_pendentes(now_dt=t0)
        assert fsrv.obter_documento("tx_c05")["status"] == "cancelamento_pendente"

        outbox.processar_pendentes(now_dt=t0 + dt.timedelta(minutes=10))
        doc = fsrv.obter_documento("tx_c05")
        assert doc["status"] == "cancelamento_recusado"
        assert "sem confirmação da SEFAZ — verificar com o contador" in doc["mensagem_sefaz"]

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

        # Drena toda a outbox: emissão já foi desarmada no agendamento e cancelamento fecha local sem HTTP
        outbox.processar_pendentes()
        assert chamadas == []
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

        ok, _ = db.cancelar_venda("tx_c07", motivo="Corrida emissao", usuario_id=1)
        assert ok is True

        # 1ª execução: vê 'processando', reagenda sem chamar DELETE e sem consumir tentativa
        t0 = dt.datetime.now()
        outbox.processar_pendentes(now_dt=t0)
        assert "DELETE" not in chamadas
        tent_job = db.conn.execute("SELECT tentativas FROM outbox WHERE chave_idempotencia = 'nfce_cancel:tx_c07'").fetchone()[0]
        assert tent_job == 0

        # Emissão finaliza e grava 'autorizado' com protocolo e autorizado_em
        with db._lock:
            db.conn.execute(
                "UPDATE nfce_documentos SET status = 'autorizado', protocolo = '135260000111222', autorizado_em = ? WHERE transacao_id = 'tx_c07'",
                (t0.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            db.conn.commit()

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

        t_ini_canc = time.monotonic()
        ok, _ = db.cancelar_venda("tx_c09", motivo="Teste lock livre", usuario_id=1)
        duracao_canc = time.monotonic() - t_ini_canc
        assert ok is True
        assert duracao_canc < 0.5, f"cancelar_venda demorou {duracao_canc:.3f}s (esperado < 0.5s)"

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
        assert not th.is_alive()
        assert tempo_leitura["duracao"] < 0.5
        assert tempo_leitura["linhas"] >= 1

        assert delete_concluido.wait(timeout=5.0)
        for _ in range(20):
            if fsrv.obter_documento("tx_c09")["status"] == "cancelado":
                break
            time.sleep(0.05)
        assert fsrv.obter_documento("tx_c09")["status"] == "cancelado"

    def test_10_transacao_local_identica_estoque_entradas_caixa_fiado_fidelidade(self, fiscal_env):
        db, outbox, pid = fiscal_env
        db.adicionar_cliente("Joao Fidelidade", "11999990000", "joao@teste.com")
        _registrar_venda(db, pid, "tx_c10", qtd=2, forma="Fiado", cliente="Joao Fidelidade")

        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 48
        ok, msg = db.cancelar_venda("tx_c10", motivo="Estorno integral teste", usuario_id=1)
        assert ok is True, msg

        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 50
        assert db.conn.execute("SELECT status FROM vendas WHERE transacao_id = 'tx_c10'").fetchone()[0] == "Cancelada"
        estornos = db.conn.execute("SELECT COUNT(*) FROM entradas WHERE valor < 0 AND descricao LIKE '%Estorno venda%'").fetchone()[0]
        assert estornos >= 1
        aud_cx = db.conn.execute("SELECT COUNT(*) FROM auditoria_caixa WHERE tipo_operacao = 'CANCELAMENTO'").fetchone()[0]
        assert aud_cx >= 1
        assert db.verificar_integridade_auditoria()[0] is True

    # =====================================================================
    # NOVOS TESTES 11–20 (Correções pós-review do Lote 3)
    # =====================================================================

    def test_11_pendente_emissao_tentativas_zero_desarma_no_agendamento_zero_http(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http(req, timeout):
            chamadas.append(req.get_method())
            raise ConnectionError("Sem internet na emissão inicial")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c11")
        assert fsrv.emitir_para_venda("tx_c11").status == "pendente_emissao"
        chamadas.clear()

        ok, _ = db.cancelar_venda("tx_c11", motivo="Cancelada antes de tentar outbox", usuario_id=1)
        assert ok is True

        # Já no agendamento (antes de drenar outbox), o job nfce:tx_c11 foi desarmado!
        row_em = db.conn.execute(
            "SELECT status, erro_ultimo FROM outbox WHERE chave_idempotencia = 'nfce:tx_c11'"
        ).fetchone()
        assert row_em == ("erro_permanente", "venda cancelada")

        outbox.processar_pendentes()
        assert chamadas == []
        assert fsrv.obter_documento("tx_c11")["status"] == "cancelado"

    def test_12_pendente_emissao_tentativas_maior_zero_get_autorizada_delete_cancelado(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http_offline(req, timeout):
            chamadas.append(req.get_method())
            raise socket.timeout("Timeout de leitura após SEFAZ autorizar")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_offline), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c12")
        assert fsrv.emitir_para_venda("tx_c12").status == "pendente_emissao"

        # 1 tentativa de emissão na outbox falhou por timeout (tentativas_emissao == 1, sem protocolo local!)
        t0 = dt.datetime.now()
        outbox.processar_pendentes(now_dt=t0)
        tent_em = db.conn.execute("SELECT tentativas FROM outbox WHERE chave_idempotencia = 'nfce:tx_c12'").fetchone()[0]
        assert tent_em == 1
        chamadas.clear()

        # Operador cancela a venda; internet volta e GET revela que a nota havia sido autorizada!
        def fake_http_online(req, timeout):
            chamadas.append(req.get_method())
            if req.get_method() == "GET":
                return _resp_autorizada("35261112345678000199650010000001121000001121")
            if req.get_method() == "DELETE":
                return 200, {"status": "cancelado", "mensagem_sefaz": "Cancelada após descoberta via GET"}
            return 200, {}

        fsrv._explicit_provider = FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_online)
        db.cancelar_venda("tx_c12", motivo="Cancelamento caso ambiguo", usuario_id=1)

        outbox.processar_pendentes(now_dt=t0 + dt.timedelta(seconds=5))
        assert chamadas == ["GET", "DELETE"]
        assert fsrv.obter_documento("tx_c12")["status"] == "cancelado"

    def test_13_erro_comunicacao_get_nao_encontrada_cancela_local_sem_delete(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http(req, timeout):
            chamadas.append(req.get_method())
            if req.get_method() == "GET":
                return 404, {"codigo": "nao_encontrado", "mensagem": "Nota não encontrada"}
            return 200, {}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c13")
        # Simula documento em status 'erro_comunicacao' no banco (ambíguo sem protocolo)
        agora = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db._lock:
            db.conn.execute(
                """
                INSERT INTO nfce_documentos (transacao_id, status, ambiente, criado_em, atualizado_em)
                VALUES ('tx_c13', 'erro_comunicacao', 'homologacao', ?, ?)
                """,
                (agora, agora),
            )
            db.conn.commit()

        db.cancelar_venda("tx_c13", motivo="Erro comunicacao previo", usuario_id=1)
        outbox.processar_pendentes()

        assert chamadas == ["GET"]  # Sem DELETE!
        assert fsrv.obter_documento("tx_c13")["status"] == "cancelado"

    def test_14_corrida_fifo_emissao_pendente_antes_cancelamento_zero_post(self, fiscal_env):
        db, outbox, pid = fiscal_env
        chamadas = []

        def fake_http_init(req, timeout):
            raise ConnectionError("Offline inicial")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_init), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c14")
        # Enfileira nfce:tx_c14 (id menor na fila FIFO)
        assert fsrv.emitir_para_venda("tx_c14").status == "pendente_emissao"

        # Internet volta ANTES do worker rodar, mas operador já cancelou a venda!
        def fake_http_online(req, timeout):
            chamadas.append(req.get_method())
            return _resp_autorizada()

        fsrv._explicit_provider = FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_online)
        db.cancelar_venda("tx_c14", motivo="Cancelada antes do worker drenar FIFO", usuario_id=1)

        outbox.processar_pendentes()
        assert "POST" not in chamadas
        assert chamadas == []
        assert fsrv.obter_documento("tx_c14")["status"] == "cancelado"

    def test_15_offline_35min_depois_autorizada_cancela_1min_depois_envia_delete(self, fiscal_env, monkeypatch):
        db, outbox, pid = fiscal_env
        monkeypatch.setenv("NFCE_PRAZO_CANCELAMENTO_MIN", "30")
        chamadas = []

        def fake_http_off(req, timeout):
            raise ConnectionError("Loja sem internet")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_off), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c15")
        assert fsrv.emitir_para_venda("tx_c15").status == "pendente_emissao"
        assert fsrv.obter_documento("tx_c15")["autorizado_em"] is None

        # Simular que criado_em foi há 36 minutos, mas a autorização ocorreu há 1 minuto!
        t_agora = dt.datetime.now()
        t_criado = (t_agora - dt.timedelta(minutes=36)).strftime("%Y-%m-%d %H:%M:%S")
        with db._lock:
            db.conn.execute("UPDATE nfce_documentos SET criado_em = ? WHERE transacao_id = 'tx_c15'", (t_criado,))
            db.conn.commit()

        def fake_http_on(req, timeout):
            chamadas.append(req.get_method())
            if req.get_method() == "POST":
                return _resp_autorizada()
            if req.get_method() == "DELETE":
                return 200, {"status": "cancelado", "mensagem_sefaz": "Cancelada dentro do prazo real de autorizacao"}
            return 200, {}

        fsrv._explicit_provider = FocusNFeProvider("TOK", "homologacao", http_requester=fake_http_on)
        # Worker transmite a emissão pendente -> preenche autorizado_em com o horário atual!
        outbox.processar_pendentes(now_dt=t_agora)
        doc_aut = fsrv.obter_documento("tx_c15")
        assert doc_aut["status"] == "autorizado"
        assert doc_aut["autorizado_em"] is not None
        assert doc_aut["autorizado_em"] != t_criado
        chamadas.clear()

        # Cancelar 1 minuto após autorizar: DELETE DEVE ser enviado (não bloqueia por criado_em antigo!)
        db.cancelar_venda("tx_c15", motivo="Cancelada 1 min apos autorizar", usuario_id=1)
        outbox.processar_pendentes(now_dt=t_agora + dt.timedelta(minutes=1))
        assert chamadas == ["DELETE"]
        assert fsrv.obter_documento("tx_c15")["status"] == "cancelado"

    def test_16_depois_do_prazo_sefaz_501_recusa_e_timeout_continua_pendente(self, fiscal_env, monkeypatch):
        db, outbox, pid = fiscal_env
        monkeypatch.setenv("NFCE_PRAZO_CANCELAMENTO_MIN", "30")

        # Parte A: depois do prazo com timeout -> continua cancelamento_pendente (SEFAZ decide!)
        def fake_timeout(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            raise socket.timeout("Timeout após 40 min")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_timeout), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c16a")
        fsrv.emitir_para_venda("tx_c16a")
        db.cancelar_venda("tx_c16a", motivo="Timeout apos prazo", usuario_id=1)

        t_pos_prazo = dt.datetime.now() + dt.timedelta(minutes=40)
        outbox.processar_pendentes(now_dt=t_pos_prazo)
        assert fsrv.obter_documento("tx_c16a")["status"] == "cancelamento_pendente"

        # Parte B: depois do prazo, SEFAZ responde cStat 501 -> cancelamento_recusado
        def fake_501(req, timeout):
            if req.get_method() == "GET":
                return _resp_autorizada()
            return 422, {
                "status": "erro_cancelamento",
                "status_sefaz": "501",
                "mensagem_sefaz": "Rejeição 501: Prazo de cancelamento superior ao previsto na legislação",
            }

        fsrv._explicit_provider = FocusNFeProvider("TOK", "homologacao", http_requester=fake_501)
        outbox.processar_pendentes(now_dt=t_pos_prazo + dt.timedelta(minutes=6))
        doc_b = fsrv.obter_documento("tx_c16a")
        assert doc_b["status"] == "cancelamento_recusado"
        assert "prazo expirado" in doc_b["mensagem_sefaz"].lower()

    def test_17_http_401_token_expirado_continua_pendente_motivo_credencial(self, fiscal_env):
        db, outbox, pid = fiscal_env

        def fake_401(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            return 401, {"codigo": "nao_autorizado", "mensagem": "Token expirado ou inválido"}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_401), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c17")
        fsrv.emitir_para_venda("tx_c17")
        db.cancelar_venda("tx_c17", motivo="Teste 401 token expirado", usuario_id=1)

        outbox.processar_pendentes()
        doc = fsrv.obter_documento("tx_c17")
        assert doc["status"] == "cancelamento_pendente"
        assert "credencial Focus NFe inválida — verificar token em Configurações" in doc["mensagem_sefaz"]
        assert "prazo expirado" not in doc["mensagem_sefaz"].lower()

        itens = [i for i in outbox.listar_itens() if i["chave_idempotencia"] == "nfce_cancel:tx_c17"]
        assert itens[0]["status"] == "falha_temporaria"

    def test_18_cstat_502_nao_classificado_como_prazo(self):
        status_res, msg_res, perm = classificar_resposta_cancelamento(
            422,
            {
                "status": "erro_cancelamento",
                "status_sefaz": "502",
                "mensagem_sefaz": "Rejeição 502: Erro na Chave de Acesso - Campo Id não corresponde à concatenação",
            },
        )
        assert status_res == "cancelamento_recusado"
        assert perm is True
        assert "prazo expirado" not in msg_res.lower()
        assert "502" in msg_res

    def test_19_job_orfao_processando_mais_60s_recuperado_menos_60s_intocado(self, fiscal_env):
        db, outbox, pid = fiscal_env

        def fake_http(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            if req.get_method() in ("GET", "DELETE"):
                return 200, {"status": "cancelado", "mensagem_sefaz": "Cancelada após recuperar órfão"}
            return 200, {}

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c19_velho")
        fsrv.emitir_para_venda("tx_c19_velho")
        db.cancelar_venda("tx_c19_velho", motivo="Orfao antigo", usuario_id=1)

        _registrar_venda(db, pid, "tx_c19_recente")
        fsrv.emitir_para_venda("tx_c19_recente")
        db.cancelar_venda("tx_c19_recente", motivo="Em voo recente", usuario_id=1)

        agora = dt.datetime.now()
        ts_velho = (agora - dt.timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        ts_recente = (agora - dt.timedelta(seconds=15)).strftime("%Y-%m-%d %H:%M:%S")

        with db._lock:
            db.conn.execute(
                "UPDATE outbox SET status = 'processando', atualizado_em = ? WHERE chave_idempotencia = 'nfce_cancel:tx_c19_velho'",
                (ts_velho,),
            )
            db.conn.execute(
                "UPDATE outbox SET status = 'processando', atualizado_em = ? WHERE chave_idempotencia = 'nfce_cancel:tx_c19_recente'",
                (ts_recente,),
            )
            db.conn.commit()

        # start_worker() recupera órfãos > 60s e preserva < 60s
        outbox.start_worker()
        for _ in range(30):
            if fsrv.obter_documento("tx_c19_velho")["status"] == "cancelado":
                break
            time.sleep(0.05)
        outbox.stop_worker(timeout=1.5)

        assert fsrv.obter_documento("tx_c19_velho")["status"] == "cancelado"
        st_recente = db.conn.execute(
            "SELECT status FROM outbox WHERE chave_idempotencia = 'nfce_cancel:tx_c19_recente'"
        ).fetchone()[0]
        assert st_recente == "processando"

    def test_20_clamp_falha_aos_28min_agenda_proxima_tentativa_antes_dos_30min(self, fiscal_env, monkeypatch):
        db, outbox, pid = fiscal_env
        monkeypatch.setenv("NFCE_PRAZO_CANCELAMENTO_MIN", "30")

        def fake_http(req, timeout):
            if req.get_method() == "POST":
                return _resp_autorizada()
            raise TimeoutError("Falha aos 28 min")

        fsrv = FiscalService(db, provider=FocusNFeProvider("TOK", "homologacao", http_requester=fake_http), outbox_service=outbox)
        db._fiscal_service = fsrv

        _registrar_venda(db, pid, "tx_c20")
        fsrv.emitir_para_venda("tx_c20")

        t_aut = dt.datetime.now().replace(microsecond=0)
        with db._lock:
            db.conn.execute(
                "UPDATE nfce_documentos SET autorizado_em = ? WHERE transacao_id = 'tx_c20'",
                (t_aut.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            db.conn.commit()

        db.cancelar_venda("tx_c20", motivo="Falha aos 28 min", usuario_id=1)
        # Forçar tentativa anterior == 3 (cujo backoff normal na 4ª tentativa seria 300s = 5 min -> 33 min!)
        with db._lock:
            db.conn.execute("UPDATE outbox SET tentativas = 3 WHERE chave_idempotencia = 'nfce_cancel:tx_c20'")
            db.conn.commit()

        t_28m = t_aut + dt.timedelta(minutes=28)
        outbox.processar_pendentes(now_dt=t_28m)

        prox_str = db.conn.execute(
            "SELECT proximo_envio FROM outbox WHERE chave_idempotencia = 'nfce_cancel:tx_c20'"
        ).fetchone()[0]
        dt_prox = dt.datetime.strptime(prox_str, "%Y-%m-%d %H:%M:%S")
        dt_limite = t_aut + dt.timedelta(minutes=30)

        assert dt_prox < dt_limite, f"proximo_envio ({dt_prox}) ultrapassou o prazo de 30 min ({dt_limite})"
        assert dt_prox == t_28m + dt.timedelta(seconds=110)  # min(300s, 120s - 10s) = 110s -> 29m50s!

    def test_21_migracao_aditiva_autorizado_em_em_banco_v9_idempotente(self, tmp_path):
        """
        21. Banco já no schema v9 sem a coluna nfce_documentos.autorizado_em:
            - 1ª execução de run_migrations cria a coluna;
            - 2ª execução de run_migrations não lança erro (idempotente);
            - registros preexistentes permanecem com autorizado_em NULL.
        """
        import sqlite3
        import migrations

        db_file = tmp_path / "legado_v9_sem_autorizado_em.db"
        conn = sqlite3.connect(str(db_file))
        try:
            conn.execute(
                """
                CREATE TABLE db_version (
                    versao TEXT PRIMARY KEY,
                    data_migr TEXT DEFAULT CURRENT_TIMESTAMP,
                    descricao TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO db_version (versao, descricao) VALUES ('schema_v9', '4.6-fiscal legado')"
            )
            conn.execute(
                """
                CREATE TABLE nfce_documentos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transacao_id TEXT NOT NULL UNIQUE,
                    referencia_focus TEXT NOT NULL UNIQUE,
                    ambiente TEXT NOT NULL DEFAULT 'homologacao',
                    serie INTEGER DEFAULT 1,
                    numero INTEGER,
                    chave_nfe TEXT,
                    protocolo TEXT,
                    status TEXT NOT NULL DEFAULT 'pendente_emissao',
                    status_sefaz TEXT,
                    mensagem_sefaz TEXT,
                    xml_path TEXT,
                    danfe_url TEXT,
                    qrcode_url TEXT,
                    url_consulta_nf TEXT,
                    justificativa_cancelamento TEXT,
                    payload_json TEXT DEFAULT '{}',
                    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            conn.execute(
                """
                INSERT INTO nfce_documentos (transacao_id, referencia_focus, status, numero, chave_nfe)
                VALUES ('tx_legado_v9', 'estoke_tx_legado_v9', 'autorizado', 101, '35261100000000000191650010000001011000001010')
                """
            )
            migrations.set_user_version(conn, 9)
            conn.commit()

            cols_antes = [r[1] for r in conn.execute("PRAGMA table_info(nfce_documentos)").fetchall()]
            assert "autorizado_em" not in cols_antes

            # 1ª execução: deve adicionar autorizado_em em banco já na v9
            v1 = migrations.run_migrations(conn)
            assert v1 == migrations.LATEST_SCHEMA_VERSION
            cols_depois = [r[1] for r in conn.execute("PRAGMA table_info(nfce_documentos)").fetchall()]
            assert "autorizado_em" in cols_depois

            # 2ª execução: idempotente, sem erro
            v2 = migrations.run_migrations(conn)
            assert v2 == migrations.LATEST_SCHEMA_VERSION

            # Registro antigo deve permanecer com autorizado_em NULL (None)
            row = conn.execute(
                "SELECT transacao_id, status, autorizado_em FROM nfce_documentos WHERE transacao_id = 'tx_legado_v9'"
            ).fetchone()
            assert row == ("tx_legado_v9", "autorizado", None)
        finally:
            conn.close()
