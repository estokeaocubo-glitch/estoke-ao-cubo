"""
Testes da Auditoria A (Lote 5) — I/O pesado fora de Database._lock e resiliência de restore:
1. Todos os 9 métodos da Auditoria A permitem leitura concorrente (listar_produtos) em < 0.5s
   enquanto a etapa lenta de I/O (2s) está em execução;
2. exportar_pacote_contador gera CSVs idênticos ao snapshot pré-refatoração;
3. Consistência: venda inserida DURANTE a etapa lenta (ZIP) não aparece parcialmente no export;
4. criar_backup -> restaurar_backup: dados idênticos + SonhosService(db) continua funcionando sem reiniciar;
5. Backup corrompido é recusado antes da troca mantendo o banco original intacto;
6. Falha no meio da troca executa rollback automático a partir de estoque.db.pre-restore.
"""
from __future__ import annotations

import builtins
import datetime as _real_dt
import gc
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import xml.etree.ElementTree as ET
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
from services.dreams_service import SonhosService


@pytest.fixture
def isolated_db():
    tmpdir = tempfile.TemporaryDirectory()
    orig_path = database.get_app_data_path
    database.get_app_data_path = lambda *parts: (
        os.path.join(tmpdir.name, *parts) if parts else tmpdir.name
    )
    db = database.Database(db_name="estoque.db")
    db.adicionar_produto("Produto Base Lock", 15, 50.0, categoria="Geral", preco_compra=20.0)
    yield db, tmpdir.name
    try:
        db.close()
    except Exception:
        pass
    database.get_app_data_path = orig_path
    gc.collect()
    for _ in range(10):
        try:
            tmpdir.cleanup()
            break
        except PermissionError:
            time.sleep(0.1)


def _executar_com_leitura_concorrente(db, slow_call, entered_event: threading.Event):
    """
    Executa slow_call em thread de fundo, aguarda entrada na etapa lenta (2s),
    e mede o tempo que outra thread leva para executar db.listar_produtos().
    """
    resultado_slow = {}

    def _worker():
        try:
            resultado_slow["ret"] = slow_call()
        except Exception as exc:
            resultado_slow["exc"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    assert entered_event.wait(timeout=5.0), "A etapa lenta mockada não foi atingida dentro do prazo."

    t0 = time.monotonic()
    df_prods = db.listar_produtos()
    elapsed = time.monotonic() - t0

    t.join(timeout=8.0)
    assert not t.is_alive(), "Thread da operação lenta não finalizou."
    if "exc" in resultado_slow:
        raise resultado_slow["exc"]
    return elapsed, df_prods, resultado_slow.get("ret")


# ==============================================================================
# 1. Todos os 9 métodos da Auditoria A liberam _lock durante etapa lenta (2s)
# ==============================================================================


class TestIOForaDoLockAuditoriaA:
    def test_01_criar_backup_criptografia_fora_do_lock(self, isolated_db, monkeypatch):
        db, _ = isolated_db
        entered = threading.Event()
        orig_enc = database._encrypt_file

        def _slow_encrypt(src, dst, key):
            entered.set()
            time.sleep(2.0)
            return orig_enc(src, dst, key)

        monkeypatch.setattr(database, "_encrypt_file", _slow_encrypt)

        elapsed, df, ret = _executar_com_leitura_concorrente(db, db.criar_backup, entered)
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante criar_backup"

    def test_02_exportar_backup_externo_fora_do_lock(self, isolated_db, tmp_path, monkeypatch):
        db, _ = isolated_db
        entered = threading.Event()
        orig_enc = database._encrypt_file

        def _slow_encrypt(src, dst, key):
            entered.set()
            time.sleep(2.0)
            return orig_enc(src, dst, key)

        monkeypatch.setattr(database, "_encrypt_file", _slow_encrypt)

        elapsed, df, ret = _executar_com_leitura_concorrente(
            db, lambda: db.exportar_backup_externo(str(tmp_path / "ext_kit")), entered
        )
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante exportar_backup_externo"

    def test_03_exportar_pacote_contador_zip_fora_do_lock(self, isolated_db, tmp_path, monkeypatch):
        db, _ = isolated_db
        entered = threading.Event()
        orig_zipfile = zipfile.ZipFile

        class SlowZipFile(orig_zipfile):
            def __init__(self, *args, **kwargs):
                entered.set()
                time.sleep(2.0)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(zipfile, "ZipFile", SlowZipFile)
        destino = str(tmp_path / "pacote_slow.zip")

        elapsed, df, ret = _executar_com_leitura_concorrente(
            db, lambda: db.exportar_pacote_contador(mes_ano="2026-11", destino_zip_path=destino), entered
        )
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante exportar_pacote_contador"

    def test_04_exportar_relatorio_pdf_render_fora_do_lock(self, isolated_db, monkeypatch):
        db, _ = isolated_db
        entered = threading.Event()
        orig_open = builtins.open

        def _slow_open(file, mode="r", *args, **kwargs):
            if "w" in str(mode) and str(file).endswith(".html"):
                entered.set()
                time.sleep(2.0)
            return orig_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _slow_open)

        elapsed, df, ret = _executar_com_leitura_concorrente(
            db, lambda: db.exportar_relatorio_pdf(tipo="estoque"), entered
        )
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante exportar_relatorio_pdf"

    def test_05_parse_nfe_xml_fora_do_lock(self, isolated_db, tmp_path, monkeypatch):
        db, _ = isolated_db
        xml_file = tmp_path / "nota_teste.xml"
        xml_file.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
            <nfeProc><NFe><infNFe Id="NFe35261100000000000191550010000000011000000011">
                <ide><nNF>1001</nNF></ide>
                <emit><xNome>Fornecedor XML</xNome><CNPJ>12345678000199</CNPJ></emit>
                <det nItem="1"><prod>
                    <cProd>P1</cProd><cEAN>7890000000011</cEAN><xProd>Produto Base Lock</xProd>
                    <uCom>UN</uCom><qCom>5.00</qCom><vUnCom>20.00</vUnCom>
                </prod></det>
            </infNFe></NFe></nfeProc>""",
            encoding="utf-8",
        )
        entered = threading.Event()
        orig_fromstring = ET.fromstring

        def _slow_fromstring(text, *args, **kwargs):
            entered.set()
            time.sleep(2.0)
            return orig_fromstring(text, *args, **kwargs)

        monkeypatch.setattr(ET, "fromstring", _slow_fromstring)

        elapsed, df, ret = _executar_com_leitura_concorrente(
            db, lambda: db.parse_nfe_xml(str(xml_file)), entered
        )
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante parse_nfe_xml"

    def test_06_processar_imagem_e_importar_avulsa_fora_do_lock(self, isolated_db, tmp_path, monkeypatch):
        db, _ = isolated_db
        img_src = tmp_path / "foto.png"
        img_src.write_bytes(b"\x89PNG\r\n\x1a\nfakeimg")
        csv_ref = tmp_path / "origem.csv"
        csv_ref.write_text("nome,quantidade,preco\n", encoding="utf-8")

        entered_1 = threading.Event()
        entered_2 = threading.Event()
        orig_copy2 = shutil.copy2
        fase = {"atual": 1}

        def _slow_copy2(src, dst, *args, **kwargs):
            if fase["atual"] == 1:
                entered_1.set()
            else:
                entered_2.set()
            time.sleep(2.0)
            return orig_copy2(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copy2", _slow_copy2)

        elapsed1, df1, ret1 = _executar_com_leitura_concorrente(
            db, lambda: db._processar_imagem_importacao(str(img_src), "Prod Img", str(csv_ref)), entered_1
        )
        assert ret1.endswith(".png")
        assert not df1.empty
        assert elapsed1 < 0.5

        fase["atual"] = 2
        elapsed2, df2, ret2 = _executar_com_leitura_concorrente(
            db, lambda: db.importar_imagem_avulsa(str(img_src), "Prod Avulso"), entered_2
        )
        assert ret2 and ret2.endswith(".png")
        assert not df2.empty
        assert elapsed2 < 0.5

    def test_07_preparar_e_validar_importacao_fora_do_lock(self, isolated_db, tmp_path, monkeypatch):
        import pandas as pd

        db, _ = isolated_db
        csv_file = tmp_path / "lote.csv"
        csv_file.write_text("nome,quantidade,preco\nItem CSV,10,19.90\n", encoding="utf-8")

        entered = threading.Event()
        orig_read_csv = pd.read_csv

        def _slow_read_csv(*args, **kwargs):
            entered.set()
            time.sleep(2.0)
            return orig_read_csv(*args, **kwargs)

        monkeypatch.setattr(pd, "read_csv", _slow_read_csv)

        elapsed, df, ret = _executar_com_leitura_concorrente(
            db, lambda: db.preparar_e_validar_importacao(str(csv_file)), entered
        )
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante preparar_e_validar_importacao"

    def test_08_restaurar_backup_descriptografia_fora_do_lock(self, isolated_db, monkeypatch):
        db, _ = isolated_db
        ok_bk, bk_path = db.criar_backup()
        assert ok_bk is True

        entered = threading.Event()
        orig_dec = database._decrypt_file

        def _slow_decrypt(src, dst, key):
            entered.set()
            time.sleep(2.0)
            return orig_dec(src, dst, key)

        monkeypatch.setattr(database, "_decrypt_file", _slow_decrypt)

        elapsed, df, ret = _executar_com_leitura_concorrente(
            db, lambda: db.restaurar_backup(bk_path), entered
        )
        assert ret[0] is True
        assert not df.empty
        assert elapsed < 0.5, f"listar_produtos bloqueou {elapsed:.3f}s durante descriptografia de restaurar_backup"


# ==============================================================================
# 2. Snapshot pré-refatoração e consistência atômica do exportar_pacote_contador
# ==============================================================================

SNAPSHOT_CONTADOR_CSVS = {
    "entradas.csv": "\ufeffID;Data;Valor;Descricao\r\n1;2026-11-24;380.00;Venda PDV: 1 itens | Cliente Snap | PIX\r\n2;2026-11-24;500.00;Aporte Capital\r\n3;2026-11-24;120.00;Serviço: Manutenção\r\n",
    "gastos.csv": "\ufeffID;Data;Valor;Categoria;Descricao;Tipo\r\n1;2026-11-10;300.00;Aluguel;Aluguel Nov;Fixo\r\n2;2026-11-15;50.00;Material;Bobinas;Variável\r\n",
    "vendas_detalhadas.csv": "\ufeffID;Data/Hora;Produto;Qtd;Valor Total;Desconto;Lucro;Forma Pagamento;Cliente;Vendedor\r\n1;2026-11-24T14:30:00;Teclado Pro;2;400.00;20.00;200.00;PIX;Cliente Snap;Carlos\r\n",
    "vendas_por_pagamento.csv": "\ufeffForma de Pagamento;Valor Liquido\r\nDinheiro;0.00\r\nPix;380.00\r\nCartao Debito;0.00\r\nCartao Credito;0.00\r\nFiado;0.00\r\nTOTAL_LIQUIDO_VENDAS;380.00\r\n",
}


class TestPacoteContadorConsistencia:
    def test_csvs_identicos_ao_snapshot_pre_refatoracao(self, tmp_path, monkeypatch):
        fixed_date = _real_dt.date(2026, 11, 24)
        fixed_dt = _real_dt.datetime(2026, 11, 24, 14, 30, 0)

        class FakeDate(_real_dt.date):
            @classmethod
            def today(cls):
                return fixed_date

        class FakeDateTime(_real_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_dt if tz is None else fixed_dt.replace(tzinfo=tz)

            @classmethod
            def today(cls):
                return fixed_dt

        fake_dt_mod = types.ModuleType("datetime")
        for attr in dir(_real_dt):
            setattr(fake_dt_mod, attr, getattr(_real_dt, attr))
        fake_dt_mod.date = FakeDate
        fake_dt_mod.datetime = FakeDateTime

        monkeypatch.setattr(database, "datetime", fake_dt_mod)
        monkeypatch.setattr(
            database,
            "get_app_data_path",
            lambda *parts: os.path.join(str(tmp_path), *parts) if parts else str(tmp_path),
        )

        db = database.Database(db_name="snap_contador_pos.db")
        try:
            db.salvar_config_nota({"empresa_nome": "Cubo Snapshot Ltda", "empresa_cnpj": "12.345.678/0001-99"})
            db.adicionar_produto("Teclado Pro", 20, 200.0, categoria="Periféricos", preco_compra=90.0)
            pid = int(db.listar_produtos().iloc[0]["id"])
            db.abrir_caixa(100.0, usuario_id=1, loja_id=1)
            db.registrar_venda_detalhada(
                itens=[{"tipo": "produto", "produto_id": pid, "id": pid, "nome": "Teclado Pro", "qtd": 2, "preco": 200.0}],
                total=380.0,
                desconto=20.0,
                cliente="Cliente Snap",
                forma_pagamento="PIX",
                vendedor="Carlos",
                formas_usadas=[{"forma": "PIX", "valor": 380.0}],
                usuario_id=1,
            )
            db.adicionar_entrada(500.0, "Aporte Capital")
            db.adicionar_gasto("2026-11-10", 300.0, "Aluguel", "Aluguel Nov", tipo="Fixo")
            db.adicionar_gasto("2026-11-15", 50.0, "Material", "Bobinas", tipo="Variável")
            db.registrar_servico("Cliente Snap", "Manutenção", 120.0, custo=20.0, forma_pagamento="Dinheiro")

            zip_path = str(tmp_path / "pacote_pos.zip")
            ok, info = db.exportar_pacote_contador(mes_ano="2026-11", destino_zip_path=zip_path)
            assert ok is True, info

            with zipfile.ZipFile(zip_path, "r") as zf:
                for nome_csv, conteudo_esperado in SNAPSHOT_CONTADOR_CSVS.items():
                    assert zf.read(nome_csv).decode("utf-8") == conteudo_esperado
        finally:
            db.close()

    def test_venda_inserida_durante_etapa_lenta_nao_vaza_no_export(self, isolated_db, tmp_path, monkeypatch):
        db, _ = isolated_db
        pid = int(db.listar_produtos().iloc[0]["id"])
        db.abrir_caixa(100.0, usuario_id=1, loja_id=1)
        ok_v1, _ = db.registrar_venda_detalhada(
            itens=[{"tipo": "produto", "produto_id": pid, "id": pid, "nome": "Produto Base Lock", "qtd": 1, "preco": 50.0}],
            total=50.0,
            forma_pagamento="Dinheiro",
            formas_usadas=[{"forma": "Dinheiro", "valor": 50.0}],
            usuario_id=1,
        )
        assert ok_v1 is True

        entered_zip = threading.Event()
        venda_concorrente_feita = threading.Event()
        orig_zipfile = zipfile.ZipFile

        class InterceptZipFile(orig_zipfile):
            def __init__(self, *args, **kwargs):
                entered_zip.set()
                venda_concorrente_feita.wait(timeout=3.0)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(zipfile, "ZipFile", InterceptZipFile)
        destino = str(tmp_path / "pacote_consistente.zip")
        mes_atual = _real_dt.date.today().strftime("%Y-%m")
        res_holder = {}

        def _run_export():
            res_holder["ret"] = db.exportar_pacote_contador(mes_ano=mes_atual, destino_zip_path=destino)

        t = threading.Thread(target=_run_export, daemon=True)
        t.start()
        assert entered_zip.wait(timeout=5.0)

        # Inserir venda concorrente ENQUANTO o ZIP está sendo montado fora do lock
        ok_v2, _ = db.registrar_venda_detalhada(
            itens=[{"tipo": "produto", "produto_id": pid, "id": pid, "nome": "Produto Base Lock", "qtd": 2, "preco": 50.0}],
            total=100.0,
            cliente="Cliente Concorrente",
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 100.0}],
            usuario_id=1,
        )
        assert ok_v2 is True
        venda_concorrente_feita.set()
        t.join(timeout=5.0)

        ok_exp, info_exp = res_holder["ret"]
        assert ok_exp is True
        # Export deve refletir exatamente o snapshot coerente de 1 venda (R$ 50), sem mistura parcial
        assert info_exp["qtd_vendas"] == 1
        assert info_exp["qtd_entradas"] == 1
        with zipfile.ZipFile(destino, "r") as zf:
            csv_vendas = zf.read("vendas_detalhadas.csv").decode("utf-8")
            assert "Cliente Concorrente" not in csv_vendas


# ==============================================================================
# 3. Restore: integridade, SonhosService sem reinício, corrompido e rollback pre-restore
# ==============================================================================


class TestBackupRestoreResiliencia:
    def test_criar_e_restaurar_backup_preserva_dados_e_sonhos_service_sem_reiniciar(self, isolated_db):
        db, _ = isolated_db
        sonhos = SonhosService(db)
        sonho_id = sonhos.adicionar_sonho(
            {
                "usuario_id": 1,
                "titulo": "Sonho do Backup A",
                "descricao": "Anotação criada antes do backup A para testar restore.",
                "categoria": "Metas",
                "data_registro": "2026-11-24",
            }
        )
        assert sonho_id > 0

        ok_bk, bk_path = db.criar_backup()
        assert ok_bk is True

        # Alterar estado após o backup A
        db.adicionar_produto("Produto X Pós-Backup", 5, 99.0, categoria="Temp")
        sonhos.adicionar_sonho(
            {
                "usuario_id": 1,
                "titulo": "Sonho Temporário Pós-Backup",
                "descricao": "Deve desaparecer após restaurar o backup A.",
                "categoria": "Geral",
                "data_registro": "2026-11-24",
            }
        )
        assert len(sonhos.listar_sonhos(usuario_id=1)) == 2

        # Restaurar backup A
        ok_res, _ = db.restaurar_backup(bk_path)
        assert ok_res is True
        assert os.path.exists(db.db_path + ".pre-restore")

        # Produtos voltaram ao estado do backup A
        nomes_prods = db.listar_produtos()["nome"].tolist()
        assert "Produto Base Lock" in nomes_prods
        assert "Produto X Pós-Backup" not in nomes_prods

        # SonhosService continua operando SEM reiniciar e enxerga apenas o estado do backup A
        lista_restaurada = sonhos.listar_sonhos(usuario_id=1)
        assert [s["titulo"] for s in lista_restaurada] == ["Sonho do Backup A"]

        novo_id = sonhos.adicionar_sonho(
            {
                "usuario_id": 1,
                "titulo": "Sonho Pós-Restore",
                "descricao": "Confirmando escrita na nova conexão reaberta pelo restore.",
                "categoria": "Metas",
                "data_registro": "2026-11-24",
            }
        )
        assert novo_id > 0
        assert len(sonhos.listar_sonhos(usuario_id=1)) == 2

    def test_backup_corrompido_recusado_antes_da_troca(self, isolated_db, tmp_path):
        db, _ = isolated_db
        corrupt_file = tmp_path / "backup_corrompido.db"
        corrupt_file.write_bytes(b"ISTO NAO E UM ARQUIVO SQLITE VALIDO 1234567890")

        ok, msg = db.restaurar_backup(str(corrupt_file))
        assert ok is False
        assert "corrompido" in msg.lower() or "inválido" in msg.lower()

        # Banco original permanece 100% intacto e aberto
        nomes = db.listar_produtos()["nome"].tolist()
        assert "Produto Base Lock" in nomes

    def test_falha_no_meio_da_troca_faz_rollback_do_pre_restore(self, isolated_db, monkeypatch):
        db, _ = isolated_db
        ok_bk, bk_path = db.criar_backup()
        assert ok_bk is True

        # Inserir produto que existe antes da tentativa de restore
        db.adicionar_produto("Produto Original Intacto", 9, 77.0, categoria="Geral")

        orig_copy2 = shutil.copy2

        def _falhar_na_troca(src, dst, *args, **kwargs):
            # Simula falha exatamente na cópia do arquivo staged -> self.db_path
            if ".staged_" in str(src) and os.path.abspath(str(dst)) == os.path.abspath(db.db_path):
                raise OSError("Erro de disco simulado durante a troca")
            return orig_copy2(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copy2", _falhar_na_troca)

        ok, msg = db.restaurar_backup(bk_path)
        assert ok is False
        assert "rollback" in msg.lower() or "erro ao restaurar" in msg.lower()
        assert os.path.exists(db.db_path + ".pre-restore")

        # Rollback restaurou exatamente o banco anterior à tentativa falha
        nomes = db.listar_produtos()["nome"].tolist()
        assert "Produto Original Intacto" in nomes
