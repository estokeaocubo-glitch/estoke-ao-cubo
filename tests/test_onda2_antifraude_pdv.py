"""
Testes unitários e de integração da ONDA 2:
- Migrações v2, v3 e v4 (PRAGMA user_version == 4)
- Auditoria estruturada em historico com usuario_id, antes/depois JSON e hash encadeado SHA-256
- Triggers SQLite impedindo estoque negativo (com respeito à flag permitir_estoque_negativo)
  e impedindo gravação de venda com valor líquido <= 0
- Cancelamento/estorno atômico com motivo obrigatório (cancelar_venda)
- Fechamento de caixa cego com cálculo de divergências PIX e Cartão em auditoria_caixa
- Persistência de carrinho (pdv_rascunho) e vendas suspensas / orçamentos (F6 / Shift+F6)
- Parser de sintaxe qtd*código (parse_scan_input)
- Abertura de gaveta ESC/POS em BasePrinter / TextFilePrinter / EscPosPrinter
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
import migrations
from printer import BasePrinter, TextFilePrinter
from services.venda_service import VendaService, parse_scan_input


@pytest.fixture
def temp_db():
    tmpdir = tempfile.mkdtemp(prefix="estoke_onda2_")
    orig_app_data = database.get_app_data_path
    database.get_app_data_path = lambda: tmpdir
    db = database.Database(db_name="onda2_test.db")
    try:
        yield db
    finally:
        db.close()
        database.get_app_data_path = orig_app_data
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestOnda2MigrationsAndAuditoria:
    """Valida migrações v2-v4 e trilha de auditoria encadeada por SHA-256."""

    def test_schema_version_and_objects(self, temp_db):
        assert migrations.get_user_version(temp_db.conn) == migrations.LATEST_SCHEMA_VERSION
        assert migrations.get_user_version(temp_db.conn) >= 4

        cols_hist = {r[1] for r in temp_db.conn.execute("PRAGMA table_info(historico)").fetchall()}
        assert {"usuario_id", "entidade", "entidade_id", "antes_json", "depois_json", "hash_anterior", "hash_registro"}.issubset(cols_hist)

        cols_cfg = {r[1] for r in temp_db.conn.execute("PRAGMA table_info(config_nota)").fetchall()}
        assert {"permitir_estoque_negativo", "desconto_max_sem_senha"}.issubset(cols_cfg)

        cols_vendas = {r[1] for r in temp_db.conn.execute("PRAGMA table_info(vendas)").fetchall()}
        assert {"motivo_cancelamento", "cancelado_por", "data_cancelamento"}.issubset(cols_vendas)

        triggers = {
            r[0]
            for r in temp_db.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        }
        assert {
            "trg_estoque_nao_negativo_insert",
            "trg_estoque_nao_negativo_update",
            "trg_venda_total_positivo_insert",
            "trg_venda_total_positivo_update",
        }.issubset(triggers)

        tables = {
            r[0]
            for r in temp_db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"pdv_rascunho", "vendas_suspensas"}.issubset(tables)

    def test_auditoria_usuario_e_hash_encadeado(self, temp_db):
        temp_db.set_current_user({"id": 42, "nome": "Operador 42", "perfil": "gerente"})
        temp_db.adicionar_produto("Café Torrado", 10, 25.0, "Alimentos", 14.0)
        prod_id = int(temp_db.listar_produtos().iloc[0]["id"])
        temp_db.atualizar_produto(prod_id, "Café Gourmet", 15, 29.90, "Alimentos")

        row = temp_db.conn.execute(
            """SELECT usuario_id, entidade, entidade_id, antes_json, depois_json, hash_anterior, hash_registro
               FROM historico ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        assert row[0] == 42
        assert row[1] == "produtos"
        assert row[2] == str(prod_id)
        antes = json.loads(row[3])
        depois = json.loads(row[4])
        assert antes["nome"] == "Café Torrado" and antes["quantidade"] == 10
        assert depois["nome"] == "Café Gourmet" and depois["quantidade"] == 15
        assert row[5] not in ("", None)
        assert len(row[6]) == 64

        integro, adulterados = temp_db.verificar_integridade_auditoria()
        assert integro is True
        assert adulterados == []

        # Simular adulteração direta no banco e confirmar detecção
        last_id = temp_db.conn.execute("SELECT MAX(id) FROM historico").fetchone()[0]
        temp_db.conn.execute("UPDATE historico SET detalhe = 'alterado_indevidamente' WHERE id = ?", (last_id,))
        temp_db.conn.commit()

        integro_pos, adulterados_pos = temp_db.verificar_integridade_auditoria()
        assert integro_pos is False
        assert last_id in adulterados_pos


class TestOnda2TriggersIntegridade:
    """Valida triggers SQLite impedindo estoque negativo e vendas com total <= 0."""

    def test_trigger_estoque_negativo_e_flag_config(self, temp_db):
        temp_db.adicionar_produto("Produto Trigger", 5, 10.0, "Geral")
        prod_id = int(temp_db.listar_produtos().iloc[0]["id"])

        # Por padrão (permitir_estoque_negativo=0), atualizar para negativo deve abortar
        with pytest.raises(sqlite3.IntegrityError, match="ESTOQUE_NEGATIVO"):
            temp_db.conn.execute("UPDATE produtos SET quantidade = -1 WHERE id = ?", (prod_id,))

        with pytest.raises(sqlite3.IntegrityError, match="ESTOQUE_NEGATIVO"):
            temp_db.conn.execute(
                "INSERT INTO produtos (nome, quantidade, preco) VALUES ('Negativo Direto', -3, 10.0)"
            )

        # Ativando a flag na configuração do sistema, passa a permitir quando configurado
        temp_db.conn.execute("UPDATE config_nota SET permitir_estoque_negativo = 1 WHERE id = 1")
        temp_db.conn.execute("UPDATE produtos SET quantidade = -2 WHERE id = ?", (prod_id,))
        temp_db.conn.commit()
        qtd = temp_db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (prod_id,)).fetchone()[0]
        assert qtd == -2

    def test_trigger_venda_total_positivo(self, temp_db):
        temp_db.adicionar_produto("Item Zero", 10, 20.0, "Geral")
        prod_id = int(temp_db.listar_produtos().iloc[0]["id"])

        with pytest.raises(sqlite3.IntegrityError, match="VENDA_INVALIDA"):
            temp_db.conn.execute(
                "INSERT INTO vendas (produto_id, quantidade, valor_total, desconto, data_venda, lucro) VALUES (?, 1, 0.0, 0.0, '2026-11-21', 0.0)",
                (prod_id,),
            )

        with pytest.raises(sqlite3.IntegrityError, match="VENDA_INVALIDA"):
            temp_db.conn.execute(
                "INSERT INTO vendas (produto_id, quantidade, valor_total, desconto, data_venda, lucro) VALUES (?, 1, 50.0, 50.0, '2026-11-21', 0.0)",
                (prod_id,),
            )


class TestOnda2CancelamentoECaixaCego:
    """Valida cancelamento atômico com motivo obrigatório e fechamento de caixa cego."""

    def test_cancelamento_atomico_com_motivo(self, temp_db):
        temp_db.abrir_caixa(100.0, usuario_id=1, loja_id=1)
        cid = temp_db.adicionar_cliente("João Fiado", "11988887777", "", "52998224725")
        temp_db.adicionar_produto("Produto A", 10, 50.0, "Geral", 25.0)
        temp_db.adicionar_produto("Produto B", 8, 30.0, "Geral", 15.0)
        df_p = temp_db.listar_produtos()
        id_a = int(df_p[df_p["nome"] == "Produto A"].iloc[0]["id"])
        id_b = int(df_p[df_p["nome"] == "Produto B"].iloc[0]["id"])

        ok_v, venda_ids = temp_db.registrar_venda_detalhada(
            [
                {"id": id_a, "nome": "Produto A", "qtd": 2, "preco": 50.0, "preco_compra": 25.0, "type": "produto"},
                {"id": id_b, "nome": "Produto B", "qtd": 1, "preco": 30.0, "preco_compra": 15.0, "type": "produto"},
            ],
            130.0,
            10.0,
            "João Fiado",
            "PIX R$70.00 + Fiado R$50.00",
            formas_usadas=[{"forma": "PIX", "valor": 70.0}, {"forma": "Fiado", "valor": 50.0}],
            usuario_id=1,
            loja_id=1,
            registrar_entrada=True,
        )
        assert ok_v is True and len(venda_ids) == 2
        tx_id = temp_db.conn.execute("SELECT transacao_id FROM vendas WHERE id = ?", (venda_ids[0],)).fetchone()[0]
        temp_db.registrar_fiado(cid, 50.0, f"Venda {tx_id}")

        # Bloquear cancelamento sem motivo
        ok_sem_motivo, msg_sem = temp_db.cancelar_venda(tx_id, motivo="   ", usuario_id=1)
        assert ok_sem_motivo is False and "motivo" in msg_sem.lower()

        # Cancelar atomicamente pela transacao_id com motivo obrigatório
        ok_canc, _msg_canc = temp_db.cancelar_venda(tx_id, motivo="Erro no lançamento de caixa", usuario_id=1)
        assert ok_canc is True

        # Estoque dos 2 itens devolvido integralmente
        qtd_a = temp_db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (id_a,)).fetchone()[0]
        qtd_b = temp_db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (id_b,)).fetchone()[0]
        assert qtd_a == 10 and qtd_b == 8

        # Todas as linhas da transação marcadas como Cancelada com motivo e cancelado_por
        rows_v = temp_db.conn.execute(
            "SELECT status, motivo_cancelamento, cancelado_por FROM vendas WHERE transacao_id = ?", (tx_id,)
        ).fetchall()
        assert len(rows_v) == 2
        assert all(r == ("Cancelada", "Erro no lançamento de caixa", 1) for r in rows_v)

        # Saldo financeiro líquido estornado (entrada +120 seguida de estorno -92.31 e -27.69 -> saldo 0)
        assert abs(temp_db.get_saldo_atual()) < 0.01

        # Fiado vinculado cancelado
        st_fiado = temp_db.conn.execute("SELECT status FROM contas_receber ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert st_fiado == "Cancelado"

    def test_fechamento_caixa_cego_e_gaveta(self, temp_db):
        temp_db.abrir_caixa(100.0, usuario_id=1, loja_id=1)
        temp_db.adicionar_produto("Item Caixa", 10, 40.0, "Geral", 20.0)
        pid = int(temp_db.listar_produtos().iloc[0]["id"])
        temp_db.registrar_venda_detalhada(
            [{"id": pid, "nome": "Item Caixa", "qtd": 2, "preco": 40.0, "preco_compra": 20.0, "type": "produto"}],
            80.0,
            0.0,
            "Consumidor Final",
            "Dinheiro R$30.00 + PIX R$50.00",
            formas_usadas=[{"forma": "Dinheiro", "valor": 30.0}, {"forma": "PIX", "valor": 50.0}],
            usuario_id=1,
            loja_id=1,
        )

        # Fechamento cego com pequena divergência em dinheiro e PIX
        ok_f, res_f = temp_db.fechar_caixa(128.0, "Conferência turno", usuario_id=1, conferido_pix=45.0, conferido_cartao=0.0)
        assert ok_f is True
        assert res_f["diferenca"] == -2.0  # esperado 130.0, contado 128.0
        assert res_f["diferenca_pix"] == -5.0  # esperado 50.0, conferido 45.0
        assert res_f["diferenca_cartao"] == 0.0

        aud_row = temp_db.conn.execute(
            "SELECT valores_antes, valores_depois FROM auditoria_caixa WHERE tipo_operacao = 'FECHAMENTO' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        depois_aud = json.loads(aud_row[1])
        assert depois_aud["diferenca_dinheiro"] == -2.0
        assert depois_aud["diferenca_pix"] == -5.0

        # Pulso de abertura de gaveta em driver base/texto
        assert BasePrinter().abrir_gaveta() is True
        assert TextFilePrinter().abrir_gaveta() is True


class TestOnda2RascunhoSuspensasEParser:
    """Valida persistência de carrinho (pdv_rascunho), vendas suspensas e parser qtd*código."""

    def test_persistencia_rascunho_pdv(self, temp_db):
        carrinho = [
            {"id": 1, "type": "produto", "nome": "Produto Rascunho", "preco": 15.5, "qtd": 2, "row_data": {"id": 1, "preco": 15.5}}
        ]
        temp_db.salvar_rascunho_pdv(7, carrinho, cliente="Ana", desconto="5", tipo_desconto="R$", observacao="Embalar")
        rec = temp_db.obter_rascunho_pdv(7)
        assert rec is not None
        assert rec["cliente"] == "Ana" and rec["desconto"] == "5" and rec["observacao"] == "Embalar"
        assert len(rec["itens"]) == 1 and rec["itens"][0]["qtd"] == 2

        temp_db.limpar_rascunho_pdv(7)
        assert temp_db.obter_rascunho_pdv(7) is None

    def test_vendas_suspensas_e_conversao(self, temp_db):
        temp_db.abrir_caixa(50.0, usuario_id=1, loja_id=1)
        temp_db.adicionar_produto("Item F6", 10, 25.0, "Geral", 10.0)
        pid = int(temp_db.listar_produtos().iloc[0]["id"])
        itens = [{"id": pid, "type": "produto", "nome": "Item F6", "preco": 25.0, "qtd": 3, "row_data": {"id": pid, "preco_compra": 10.0}}]

        ok_s, res_s = temp_db.suspender_venda(itens, subtotal=75.0, desconto=5.0, total=70.0, cliente="Carlos", tipo="suspensa", usuario_id=1)
        assert ok_s is True and res_s["codigo"].startswith("SUS-")

        df_ativas = temp_db.listar_vendas_suspensas(status="Ativa")
        assert len(df_ativas) == 1

        ok_conv, venda_ids = temp_db.converter_venda_suspensa(res_s["id"], forma_pagamento="PIX", usuario_id=1)
        assert ok_conv is True and len(venda_ids) == 1

        susp_after = temp_db.obter_venda_suspensa(res_s["id"])
        assert susp_after["status"] == "Convertida"
        assert temp_db.conn.execute("SELECT quantidade FROM produtos WHERE id = ?", (pid,)).fetchone()[0] == 7

    def test_parser_qtd_multiplicador(self):
        assert parse_scan_input("3*7891234567895") == (3, "7891234567895", True)
        assert parse_scan_input("0,450*2001") == (0.45, "2001", True)
        assert parse_scan_input(" 2.5 * 7890001112223 ") == (2.5, "7890001112223", True)
        assert parse_scan_input("7891234567895") == (1, "7891234567895", False)
        assert VendaService.parse_scan_input("5*ABC123") == (5, "ABC123", True)

        with pytest.raises(ValueError):
            parse_scan_input("0*7891234567895")
        with pytest.raises(ValueError):
            parse_scan_input("")
