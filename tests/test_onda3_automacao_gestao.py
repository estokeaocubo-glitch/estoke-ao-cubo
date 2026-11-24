# -*- coding: utf-8 -*-
"""
Testes unitários e de integração da ONDA 3:
- Migrações v5, v6 e v7 (PRAGMA user_version == 7)
- Fila Outbox offline (retry, backoff exponencial, erro permanente e idempotência)
- Agendador de rotinas (resumo diário caixa, fiado D+1/D+7/D+15, lotes vencendo em 7 dias)
- Auto-update com assinatura Ed25519 e fluxo unificado Full vs. Trial
- Inventário físico e contagem cega (ocultação de qtd_sistema + ajuste atômico com supervisor + auditoria)
- Sugestão automática de compra (giro 30/60/90d, lead time, PDF e WhatsApp por fornecedor)
- Pacote mensal do contador (ZIP com CSVs, DRE e XMLs importados)
- Prévia de NF-e XML (parse_nfe_xml sem escrita no banco + aplicar_nfe com vínculo manual)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
import migrations
from services.compras_service import ComprasService
from services.outbox_service import OutboxService
from services.scheduler import RoutineScheduler
from services.updater import (
    APP_VERSION,
    build_inno_install_command,
    create_license_token,
    get_license_status,
    is_newer_version,
    sign_manifest,
    verify_license_token,
    verify_manifest_signature,
)


@pytest.fixture
def temp_env():
    tmpdir = tempfile.mkdtemp(prefix="estoke_onda3_")
    orig_app_data = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir
    db = database.Database(db_name="onda3_test.db")
    try:
        yield db, tmpdir
    finally:
        db.close()
        database.get_app_data_path = orig_app_data
        shutil.rmtree(tmpdir, ignore_errors=True)


SAMPLE_NFE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
  <NFe>
    <infNFe Id="NFe35261112345678000199550010000012341000012345" versao="4.00">
      <ide><nNF>1234</nNF></ide>
      <emit>
        <CNPJ>12345678000199</CNPJ>
        <xNome>Distribuidora Teste Ltda</xNome>
        <xFant>Distribuidora Alpha</xFant>
        <enderEmit><fone>11999991111</fone></enderEmit>
      </emit>
      <det nItem="1">
        <prod>
          <cProd>FORN-999</cProd>
          <cEAN>SEM GTIN</cEAN>
          <xProd>Refrigerante Lata 350ml Pack</xProd>
          <uCom>UN</uCom>
          <qCom>10.0000</qCom>
          <vUnCom>3.50</vUnCom>
        </prod>
      </det>
      <det nItem="2">
        <prod>
          <cProd>P002</cProd>
          <cEAN>7890000002222</cEAN>
          <xProd>Biscoito Recheado 140g</xProd>
          <uCom>UN</uCom>
          <qCom>5.0000</qCom>
          <vUnCom>2.00</vUnCom>
        </prod>
      </det>
      <cobr>
        <dup>
          <nDup>001</nDup>
          <dVenc>2026-12-20</dVenc>
          <vDup>45.00</vDup>
        </dup>
      </cobr>
    </infNFe>
  </NFe>
</nfeProc>
"""


class TestOnda3MigrationsAndOutbox:
    def test_migrations_v5_v6_v7(self, temp_env):
        db, _ = temp_env
        assert migrations.LATEST_SCHEMA_VERSION >= 7
        assert migrations.get_user_version(db.conn) == migrations.LATEST_SCHEMA_VERSION

        tables = {
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"outbox", "rotinas", "inventarios", "inventario_itens", "nfe_importadas"}.issubset(tables)

        rotinas = {r[0] for r in db.conn.execute("SELECT tipo FROM rotinas").fetchall()}
        assert rotinas == {"resumo_diario_caixa", "cobranca_fiado_vencido", "alerta_lotes_vencendo"}

        cols_forn = {r[1] for r in db.conn.execute("PRAGMA table_info(fornecedores)").fetchall()}
        assert "lead_time_dias" in cols_forn

        cols_prod = {r[1] for r in db.conn.execute("PRAGMA table_info(produtos)").fetchall()}
        assert {"fornecedor_id", "lead_time_dias"}.issubset(cols_prod)

    def test_outbox_idempotencia_backoff_e_transicoes(self, temp_env):
        db, _ = temp_env
        calls = []

        def fake_handler(payload):
            calls.append(payload)
            if payload.get("fail_perm"):
                return False, "Erro definitivo", True
            if len(calls) == 1:
                return False, "Sem internet temporariamente"
            return True, "Entregue"

        outbox = OutboxService(
            db,
            handlers={"whatsapp": fake_handler},
            base_backoff_seconds=15,
            max_backoff_seconds=120,
        )

        # Fórmula de backoff exponencial
        assert outbox.calcular_backoff_seconds(1) == 15
        assert outbox.calcular_backoff_seconds(2) == 30
        assert outbox.calcular_backoff_seconds(3) == 60
        assert outbox.calcular_backoff_seconds(5) == 120

        t0 = dt.datetime(2026, 11, 21, 10, 0, 0)
        t0_str = t0.strftime("%Y-%m-%d %H:%M:%S")

        # Enfileiramento idempotente
        ok1, id1 = outbox.enfileirar(
            "whatsapp",
            {"telefone": "11999990000", "mensagem": "Oi"},
            chave_idempotencia="msg:unica:1",
            max_tentativas=3,
            proximo_envio=t0_str,
        )
        ok2, id2 = outbox.enfileirar(
            "whatsapp",
            {"telefone": "11999990000", "mensagem": "Oi"},
            chave_idempotencia="msg:unica:1",
            max_tentativas=3,
            proximo_envio=t0_str,
        )
        assert ok1 is True
        assert ok2 is False
        assert id1 == id2

        st1 = outbox.processar_pendentes(now_dt=t0)
        assert st1["processados"] == 1
        assert st1["retentativas"] == 1

        row = db.conn.execute(
            "SELECT status, tentativas, proximo_envio FROM outbox WHERE id = ?", (id1,)
        ).fetchone()
        assert row[0] == "falha_temporaria"
        assert row[1] == 1
        assert row[2] == "2026-11-21 10:00:15"

        # Antes de atingir proximo_envio, não deve reprocessar
        st_early = outbox.processar_pendentes(now_dt=t0 + dt.timedelta(seconds=5))
        assert st_early["processados"] == 0

        # Após 15s, reprocessa e envia
        st2 = outbox.processar_pendentes(now_dt=t0 + dt.timedelta(seconds=16))
        assert st2["enviados"] == 1
        row2 = db.conn.execute("SELECT status, tentativas FROM outbox WHERE id = ?", (id1,)).fetchone()
        assert row2 == ("enviado", 2)

        # Erro permanente
        ok_perm, id_perm = outbox.enfileirar(
            "whatsapp",
            {"telefone": "11999990000", "mensagem": "X", "fail_perm": True},
            chave_idempotencia="msg:perm:1",
            proximo_envio=t0_str,
        )
        assert ok_perm is True
        st_perm = outbox.processar_pendentes(now_dt=t0 + dt.timedelta(seconds=20))
        assert st_perm["erros_permanentes"] == 1
        assert db.conn.execute("SELECT status FROM outbox WHERE id = ?", (id_perm,)).fetchone()[0] == "erro_permanente"


class TestOnda3SchedulerAndUpdater:
    def test_scheduler_rotinas_idempotentes(self, temp_env):
        db, _ = temp_env
        outbox = OutboxService(db, connectivity_checker=lambda: True)
        scheduler = RoutineScheduler(db, outbox)

        # Configurar telefone da loja e horários para disparar no ref_dt
        db.conn.execute("UPDATE config_nota SET empresa_nome = 'Loja Teste', empresa_telefone = '11988887777'")
        db.conn.execute("UPDATE rotinas SET hora = '08:00', ativo = 1")

        # Produto e venda do dia + lote vencendo em 4 dias
        ref_dt = dt.datetime(2026, 11, 21, 21, 30, 0)
        hoje = ref_dt.date()
        venc_lote = (hoje + dt.timedelta(days=4)).isoformat()
        db.conn.execute(
            "INSERT INTO produtos (nome, quantidade, preco, preco_compra, data_validade, lote_serie) VALUES (?, ?, ?, ?, ?, ?)",
            ("Leite Integral", 12, 6.0, 4.0, venc_lote, "LOTE-A1"),
        )
        pid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.conn.execute(
            """
            INSERT INTO vendas (produto_id, quantidade, valor_total, data_venda, lucro, status, valor_pix)
            VALUES (?, 2, 12.0, ?, 4.0, 'Concluída', 12.0)
            """,
            (pid, f"{hoje.isoformat()}T14:00:00"),
        )

        # Fiados vencidos: D+1, D+7, D+15 e D+3 (não dispara)
        for idx, atraso in enumerate((1, 3, 7, 15), 1):
            db.conn.execute("INSERT INTO clientes (nome, contato) VALUES (?, ?)", (f"Cliente {idx}", f"1197777000{idx}"))
            cid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            dt_venc = (hoje - dt.timedelta(days=atraso)).isoformat()
            db.conn.execute(
                """
                INSERT INTO contas_receber (cliente_id, valor, data_venda, data_vencimento, status)
                VALUES (?, ?, ?, ?, 'Pendente')
                """,
                (cid, 50.0 * idx, (hoje - dt.timedelta(days=30)).isoformat(), dt_venc),
            )
        db.conn.commit()

        res = scheduler.tick(now_dt=ref_dt)
        assert set(res["executadas"]) == {"resumo_diario_caixa", "cobranca_fiado_vencido", "alerta_lotes_vencendo"}
        # 1 resumo diário + 3 cobranças fiado (D+1, D+7, D+15; D+3 ignorado) + 1 alerta lote = 5
        assert res["mensagens_enfileiradas"] == 5

        itens = outbox.listar_itens()
        assert len(itens) == 5
        idems = {it["chave_idempotencia"] for it in itens}
        assert f"resumo_diario:{hoje.isoformat()}" in idems
        assert f"alerta_lotes:{hoje.isoformat()}" in idems
        assert any(":D+1:" in k for k in idems)
        assert any(":D+7:" in k for k in idems)
        assert any(":D+15:" in k for k in idems)
        assert not any(":D+3:" in k for k in idems)

        # Segundo tick no mesmo dia não deve duplicar nada
        res2 = scheduler.tick(now_dt=ref_dt + dt.timedelta(minutes=5))
        assert res2["executadas"] == []
        assert res2["mensagens_enfileiradas"] == 0

    def test_updater_ed25519_manifest_e_licenca_unificada(self, temp_env, monkeypatch):
        db, tmpdir = temp_env
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()

        manifest = sign_manifest(
            {
                "version": "4.7.0",
                "url": "https://cdn.estoke.dev/EstokeAoCubo-Setup-4.7.0.exe",
                "sha256": "a" * 64,
                "notes": "Onda 3",
            },
            priv,
        )
        ok, msg = verify_manifest_signature(manifest, public_key=pub)
        assert ok is True, msg
        assert is_newer_version(manifest["version"], APP_VERSION) is True

        # Adulteração do SHA-256 deve ser rejeitada
        tampered = dict(manifest, sha256="b" * 64)
        ok_t, _ = verify_manifest_signature(tampered, public_key=pub)
        assert ok_t is False

        # HTTP sem TLS deve ser rejeitado mesmo assinado
        http_m = sign_manifest({"version": "4.7.0", "url": "http://inseguro/setup.exe", "sha256": "a" * 64}, priv)
        ok_h, _ = verify_manifest_signature(http_m, public_key=pub)
        assert ok_h is False

        # Licença Full vs Trial com Ed25519
        tok_full = create_license_token({"edition": "full", "customer": "Mercado Central", "expires_at": "2099-12-31"}, priv)
        ok_f, payload_f = verify_license_token(tok_full, public_key=pub, now_date=dt.date(2026, 11, 21))
        assert ok_f is True
        assert payload_f["edition"] == "full"

        st_full = get_license_status({"license_token": tok_full}, public_key=pub, now_date=dt.date(2026, 11, 21))
        assert st_full["is_trial"] is False
        assert st_full["edition"] == "full"

        # Token expirado volta para trial
        tok_exp = create_license_token({"edition": "full", "expires_at": "2020-01-01"}, priv)
        st_exp = get_license_status({"license_token": tok_exp}, public_key=pub, now_date=dt.date(2026, 11, 21))
        assert st_exp["is_trial"] is True
        assert st_exp["valid"] is False

        # Integração com Database.is_trial_mode() via variável ESTOKE_TRIAL
        monkeypatch.setenv("ESTOKE_TRIAL", "1")
        assert db.is_trial_mode() is True
        monkeypatch.delenv("ESTOKE_TRIAL", raising=False)
        assert db.is_trial_mode() is False

        # Comando Inno Setup silencioso
        fake_exe = os.path.join(tmpdir, "EstokeSetup.exe")
        with open(fake_exe, "wb") as f:
            f.write(b"MZ")
        cmd = build_inno_install_command(fake_exe)
        assert cmd[0] == fake_exe
        assert "/SILENT" in cmd and "/CLOSEAPPLICATIONS" in cmd


class TestOnda3InventarioComprasNfeContador:
    def test_inventario_contagem_cega_e_ajuste_auditado(self, temp_env):
        db, _ = temp_env
        db.conn.execute(
            "INSERT INTO produtos (nome, quantidade, preco, preco_compra, codigo_barras) VALUES (?, ?, ?, ?, ?)",
            ("Arroz 5kg", 20, 28.0, 20.0, "7891111111111"),
        )
        pid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.conn.commit()

        ok, inv_id = db.criar_sessao_inventario("Contagem mensal")
        assert ok is True

        # Duas leituras acumuladas (2 + 15 = 17, sistema tem 20 -> divergência -3)
        ok_b1, _ = db.registrar_contagem_inventario(inv_id, "7891111111111", qtd_contada=2, modo_somar=True)
        ok_b2, res_b2 = db.registrar_contagem_inventario(inv_id, "7891111111111", qtd_contada=15, modo_somar=True)
        assert ok_b1 and ok_b2
        assert res_b2["qtd_contada"] == 17.0

        # Modo cego oculta qtd_sistema e divergencia
        itens_cegos = db.listar_itens_inventario(inv_id, ocultar_sistema=True)
        assert len(itens_cegos) == 1
        assert itens_cegos[0]["qtd_contada"] == 17.0
        assert itens_cegos[0]["qtd_sistema"] is None
        assert itens_cegos[0]["divergencia"] is None

        # Modo revelado mostra divergência exata e impacto
        itens_rev = db.listar_itens_inventario(inv_id, ocultar_sistema=False)
        assert itens_rev[0]["qtd_sistema"] == 20.0
        assert itens_rev[0]["divergencia"] == -3.0
        assert itens_rev[0]["valor_divergencia"] == -60.0

        # Exige motivo e supervisor
        ok_no_mot, _ = db.aplicar_ajuste_inventario(inv_id, motivo="", supervisor_id=1)
        assert ok_no_mot is False

        ok_aj, res_aj = db.aplicar_ajuste_inventario(inv_id, motivo="Quebra de pacote", supervisor_id=1)
        assert ok_aj is True
        assert res_aj["itens_ajustados"] == 1

        nova_qtd = db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0]
        assert nova_qtd == 17
        assert db.verificar_integridade_auditoria()[0] is True

    def test_sugestao_compras_giro_pdf_e_whatsapp(self, temp_env):
        db, tmpdir = temp_env
        db.conn.execute(
            "INSERT INTO fornecedores (nome, contato, produto_fornecido, lead_time_dias) VALUES (?, ?, ?, ?)",
            ("Fornecedor Bebidas", "Tel: (11) 99999-2222", "Refrigerantes", 10),
        )
        fid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.conn.execute(
            """
            INSERT INTO produtos (nome, quantidade, preco, preco_compra, estoque_minimo, fornecedor_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Cola 2L", 4, 10.0, 5.5, 5, fid),
        )
        pid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 30 unidades vendidas nos últimos 30 dias -> giro = 1.0/dia
        # Alvo com lead_time=10, cobertura=15, min=5 => 1*(10+15)+5 = 30 => sugerido = 30 - 4 = 26
        hoje = dt.date.today()
        db.conn.execute(
            """
            INSERT INTO vendas (produto_id, quantidade, valor_total, data_venda, lucro, status)
            VALUES (?, 30, 300.0, ?, 135.0, 'Concluída')
            """,
            (pid, (hoje - dt.timedelta(days=5)).isoformat() + "T12:00:00"),
        )
        db.conn.commit()

        srv = ComprasService(db)
        sugestoes = srv.calcular_sugestao_compras(janela_dias=30, dias_cobertura=15, now_date=hoje)
        assert len(sugestoes) == 1
        it = sugestoes[0]
        assert it["giro_diario"] == 1.0
        assert it["lead_time_dias"] == 10
        assert it["qtd_sugerida"] == 26
        assert it["prioridade"] == "CRÍTICA"

        grupos = srv.agrupar_por_fornecedor(sugestoes)
        assert len(grupos) == 1
        assert grupos[0]["fornecedor_telefone"] == "11999992222"
        msg_wpp = srv.formatar_pedido_whatsapp(grupos[0])
        assert "26x Cola 2L" in msg_wpp

        pdf_path = os.path.join(tmpdir, "pedido_compras.pdf")
        ok_pdf, _ = srv.exportar_pdf_compras(sugestoes, pdf_path, janela_dias=30, dias_cobertura=15)
        assert ok_pdf is True
        assert os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 500

    def test_nfe_preview_somente_leitura_e_pacote_contador_zip(self, temp_env):
        db, tmpdir = temp_env
        # Produto pré-existente sem código de barras para testar vínculo manual no item 1 da NF-e
        db.conn.execute(
            "INSERT INTO produtos (nome, quantidade, preco, preco_compra) VALUES (?, ?, ?, ?)",
            ("Refri Lata 350ml Estoque", 10, 6.0, 3.0),
        )
        pid_manual = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.conn.commit()

        xml_path = os.path.join(tmpdir, "nfe_1234.xml")
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_NFE_XML)

        # 1) parse_nfe_xml NÃO pode alterar o banco
        ok_p, dados = db.parse_nfe_xml(xml_path)
        assert ok_p is True
        assert dados["numero_nfe"] == "1234"
        assert len(dados["itens"]) == 2
        assert db.contar_produtos() == 1
        assert db.conn.execute("SELECT COUNT(*) FROM nfe_importadas").fetchone()[0] == 0

        # 2) aplicar_nfe com vínculo manual do item 0 -> pid_manual, item 1 -> novo produto
        ok_a, resumo = db.aplicar_nfe(dados, mapeamentos={0: pid_manual})
        assert ok_a is True
        assert resumo["produtos_atualizados"] == 1
        assert resumo["produtos_criados"] == 1
        assert resumo["contas_lancadas"] == 1
        assert db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid_manual,)).fetchone()[0] == 20
        assert db.conn.execute("SELECT COUNT(*) FROM nfe_importadas").fetchone()[0] == 1

        # 3) Exportar pacote do contador para o mês atual
        mes_atual = dt.date.today().strftime("%Y-%m")
        hoje_iso = dt.date.today().isoformat()
        db.conn.execute(
            """
            INSERT INTO vendas (produto_id, quantidade, valor_total, data_venda, lucro, status, forma_pagamento, valor_pix)
            VALUES (?, 2, 12.0, ?, 5.0, 'Concluída', 'Pix', 12.0)
            """,
            (pid_manual, f"{hoje_iso}T15:00:00"),
        )
        db.conn.execute("INSERT INTO entradas (data, valor, descricao) VALUES (?, 12.0, 'Venda Balcão')", (hoje_iso,))
        db.conn.execute("INSERT INTO gastos (data, valor, categoria, descricao, tipo) VALUES (?, 4.0, 'Embalagem', 'Sacolas', 'Variável')", (hoje_iso,))
        db.conn.commit()

        zip_dest = os.path.join(tmpdir, "contador.zip")
        ok_z, res_z = db.exportar_pacote_contador(mes_ano=mes_atual, destino_zip_path=zip_dest)
        assert ok_z is True
        assert res_z["qtd_vendas"] == 1
        assert res_z["qtd_xmls"] == 1

        with zipfile.ZipFile(zip_dest, "r") as zf:
            names = set(zf.namelist())
            assert {
                "vendas_por_pagamento.csv",
                "vendas_detalhadas.csv",
                "entradas.csv",
                "gastos.csv",
                "dre_resumo.txt",
                "dre_resumo.json",
                "xmls_nfe/nfe_1234.xml",
            }.issubset(names)
            dre = json.loads(zf.read("dre_resumo.json").decode("utf-8"))
            assert dre["receita_bruta"] == 12.0
            assert dre["despesas_operacionais"] == 4.0
