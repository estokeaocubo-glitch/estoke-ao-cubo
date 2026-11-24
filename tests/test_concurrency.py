"""
Testes de concorrência e ciclo de vida (F1a — migração PyWebView).

Valida:
1. N threads simultâneas operando leitura, escrita de produto, venda detalhada,
   abertura de caixa/sangria e SonhosService sem OperationalError nem perda de dados.
2. Ciclo de vida real de EstokeAPI (make_server + stop() liberando porta efêmera
   e erro claro de OSError quando a porta já está ocupada).
3. Chamadas após Database.close() falhando de forma controlada com RuntimeError,
   mantendo criptografia .db.enc e limpeza de .db/-wal/-shm.
4. Enfileiramento assíncrono de AsyncPrinterWrapper.abrir_gaveta() sem bloquear
   a thread chamadora.
"""
import os
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from database import Database
from api_server import EstokeAPI, FLASK_AVAILABLE
from printer import AsyncPrinterWrapper, BasePrinter
from services.dreams_service import SonhosService


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Cria instância isolada do Database em diretório temporário exclusivo."""
    monkeypatch.setenv("ESTOKE_DATA_DIR", str(tmp_path))
    db = Database(db_name="test_concurrency.db")
    yield db
    if not getattr(db, "_closed", False):
        db.close()


class TestDatabaseConcurrency:
    """Testes de estresse multi-thread sobre Database e SonhosService."""

    def test_multiplas_threads_leitura_escrita_venda_e_caixa(self, isolated_db):
        db = isolated_db
        sonhos = SonhosService(db.conn)

        # Preparar massa base: produto com estoque controlado e abertura de caixa
        ok_prod = db.adicionar_produto("Produto Estresse", 200, 10.0, categoria="Geral", preco_compra=5.0)
        assert ok_prod is not False

        df_init = db.listar_produtos()
        prod_id = int(df_init.iloc[0]["id"])

        ok_cx, msg_cx = db.abrir_caixa(valor_abertura=500.0, observacao="Turno concorrência", usuario_id=1)
        assert ok_cx is True, f"Falha ao abrir caixa: {msg_cx}"
        cx_info = db.caixa_aberto(usuario_id=1)
        assert cx_info is not None
        caixa_id = int(cx_info["id"])

        erros = []
        erros_lock = threading.Lock()
        vendas_ok = []
        sangrias_ok = []
        produtos_criados = []

        def registrar_erro(exc):
            with erros_lock:
                erros.append(exc)

        def worker_leitura(idx):
            try:
                for _ in range(15):
                    _ = db.listar_produtos()
                    _ = db.resumo_caixa_atual(usuario_id=1)
                    _ = db.listar_vendas()
                    _ = sonhos.listar_sonhos(usuario_id=1)
            except Exception as exc:
                registrar_erro(exc)

        def worker_escrita_produto(idx):
            try:
                for j in range(5):
                    nome = f"Prod_T{idx}_{j}"
                    db.adicionar_produto(nome, 10, 15.0, categoria="Concorrencia", preco_compra=7.0)
                    produtos_criados.append(nome)
            except Exception as exc:
                registrar_erro(exc)

        def worker_venda(idx):
            try:
                for j in range(6):
                    itens = [{"id": prod_id, "nome": "Produto Estresse", "qtd": 1, "preco": 10.0}]
                    ok, msg = db.registrar_venda_detalhada(
                        itens=itens,
                        total=10.0,
                        forma_pagamento="Dinheiro",
                        usuario_id=1,
                    )
                    if ok:
                        vendas_ok.append((idx, j))
                    else:
                        registrar_erro(RuntimeError(f"Venda falhou inesperadamente: {msg}"))
            except Exception as exc:
                registrar_erro(exc)

        def worker_caixa_sangria(idx):
            try:
                # Tentar abrir caixa concorrente deve falhar de forma controlada (já existe caixa aberto)
                ok_dup, _ = db.abrir_caixa(valor_abertura=50.0, usuario_id=1)
                assert ok_dup is False

                for j in range(4):
                    ok_s, msg_s = db.registrar_sangria(
                        caixa_id=caixa_id,
                        usuario_id=1,
                        valor=5.0,
                        motivo=f"Sangria T{idx}-{j}",
                    )
                    if ok_s:
                        sangrias_ok.append((idx, j))
                    else:
                        registrar_erro(RuntimeError(f"Sangria falhou inesperadamente: {msg_s}"))
            except Exception as exc:
                registrar_erro(exc)

        def worker_sonhos(idx):
            try:
                for j in range(4):
                    sid = sonhos.adicionar_sonho({
                        "usuario_id": 1,
                        "titulo": f"Nota Concorrente {idx}-{j}",
                        "descricao": "Descrição longa válida para teste multi-thread no SQLite.",
                        "categoria": "Teste",
                    })
                    assert isinstance(sid, int) and sid > 0
            except Exception as exc:
                registrar_erro(exc)

        tarefas = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            for i in range(3):
                tarefas.append(pool.submit(worker_leitura, i))
            for i in range(3):
                tarefas.append(pool.submit(worker_escrita_produto, i))
            for i in range(3):
                tarefas.append(pool.submit(worker_venda, i))
            for i in range(2):
                tarefas.append(pool.submit(worker_caixa_sangria, i))
            for i in range(2):
                tarefas.append(pool.submit(worker_sonhos, i))

            for fut in as_completed(tarefas):
                fut.result()

        assert erros == [], f"Ocorreram erros sob concorrência: {erros}"

        # Conferir consistência dos dados persistidos
        assert len(vendas_ok) == 18
        assert len(sangrias_ok) == 8
        assert len(produtos_criados) == 15

        df_final = db.listar_produtos()
        # 1 produto base + 15 novos produtos = 16
        assert len(df_final) == 16
        estoque_restante = int(df_final.loc[df_final["id"] == prod_id, "quantidade"].iloc[0])
        assert estoque_restante == 200 - len(vendas_ok)

        sangrias_lista = db.listar_sangrias(caixa_id)
        assert len(sangrias_lista) == 8
        assert round(float(sangrias_lista["valor"].sum()), 2) == 40.0

        notas = sonhos.listar_sonhos(usuario_id=1)
        assert len(notas) == 8

    def test_chamada_apos_close_falha_controladamente_e_gera_enc(self, isolated_db):
        db = isolated_db
        sonhos = SonhosService(db.conn)
        db.adicionar_produto("Item Pre Close", 5, 12.0)

        db_path = db.db_path
        enc_path = db.enc_path

        db.close()
        # Segunda chamada a close() deve permanecer idempotente
        db.close()

        assert os.path.exists(enc_path) and os.path.getsize(enc_path) > 0
        assert not os.path.exists(db_path)
        assert not os.path.exists(db_path + "-wal")
        assert not os.path.exists(db_path + "-shm")

        with pytest.raises(RuntimeError, match="fechada"):
            db.listar_produtos()

        with pytest.raises(RuntimeError, match="fechada"):
            db.adicionar_produto("Item Pos Close", 1, 10.0)

        with pytest.raises(RuntimeError, match="fechada"):
            db.abrir_caixa(100.0, usuario_id=1)

        with pytest.raises(RuntimeError, match="fechada"):
            sonhos.listar_sonhos(usuario_id=1)


class TestApiServerLifecycle:
    """Valida start/stop real do servidor WSGI em porta efêmera."""

    @pytest.mark.skipif(not FLASK_AVAILABLE, reason="Flask não instalado")
    def test_stop_libera_porta_efemera_e_trata_porta_ocupada(self, isolated_db):
        api = EstokeAPI(isolated_db, port=0)
        assert api.start() is True
        porta = api.port
        assert porta > 0
        assert api.is_running() is True

        # Confirmar que o servidor está respondendo HTTP na porta alocada
        url = f"http://127.0.0.1:{porta}/"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200

        # Tentar iniciar outra instância na mesma porta deve falhar com erro claro (OSError tratado)
        api_conflito = EstokeAPI(isolated_db, port=porta)
        assert api_conflito.start() is False
        assert api_conflito.is_running() is False
        assert api_conflito._last_error and str(porta) in api_conflito._last_error

        # Encerrar a API e comprovar liberação imediata do socket TCP
        api.stop()
        assert api.is_running() is False

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", porta))
        finally:
            sock.close()


class TestAsyncDrawer:
    """Valida que AsyncPrinterWrapper.abrir_gaveta não bloqueia a thread chamadora."""

    def test_abrir_gaveta_enfileirada_sem_bloquear_chamador(self):
        executado = threading.Event()
        thread_execucao = {}

        class SlowFakePrinter(BasePrinter):
            def print_text(self, text: str, logo_path=None, pix_payload=None, venda_id=None):
                pass

            def abrir_gaveta(self):
                thread_execucao["name"] = threading.current_thread().name
                time.sleep(0.35)
                executado.set()
                return True

        wrapper = AsyncPrinterWrapper(SlowFakePrinter())
        inicio = time.monotonic()
        retorno = wrapper.abrir_gaveta()
        duracao = time.monotonic() - inicio

        assert retorno is True
        assert duracao < 0.2, f"abrir_gaveta bloqueou a thread chamadora por {duracao:.3f}s"
        assert executado.wait(timeout=3.0), "Worker do PrintQueueManager não executou abrir_gaveta"
        assert thread_execucao.get("name") == "PrintQueueManagerThread"
