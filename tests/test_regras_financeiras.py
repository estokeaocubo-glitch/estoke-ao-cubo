"""
Testes das correções de regra de negócio do Lote 4b:
1. DRE: 1º e último dia entram (date()), isolamento entre 2 lojas, contas pagas sem dupla contagem;
2. Fluxo: média = soma/30 (incluindo dias sem venda), dia 31 em fevereiro -> último dia do mês,
   deduplicação determinística pelo gasto fixo mais recente, saldo acumulado sem resíduo de float;
3. Comissões: base líquida (R$ 100 - R$ 10 × 5% = R$ 4,50), vendedor removido -> 0 + sem_cadastro=True,
   serviços somados no total geral e isolamento por loja_id;
4. Faturamento Hoje: aporte manual de R$ 500 não entra e venda cancelada não entra;
5. CSV do histórico: exporta somente as linhas correspondentes aos filtros de período e tipo.
"""
from __future__ import annotations

import csv
import datetime as _real_dt
import gc
import math
import os
import sys
import tempfile
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
from services.financeiro_service import FinanceiroService
from services.relatorio_service import RelatorioService


def _make_fake_datetime(fixed_date: _real_dt.date, fixed_dt: _real_dt.datetime):
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

    mod = types.ModuleType("datetime")
    for attr in dir(_real_dt):
        setattr(mod, attr, getattr(_real_dt, attr))
    mod.date = FakeDate
    mod.datetime = FakeDateTime
    return mod


@pytest.fixture
def isolated_db():
    tmpdir = tempfile.TemporaryDirectory()
    orig_path = database.get_app_data_path
    database.get_app_data_path = lambda *parts: (
        os.path.join(tmpdir.name, *parts) if parts else tmpdir.name
    )
    db = database.Database(db_name="test_regras_fin.db")
    yield db
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


# ==============================================================================
# 1. DRE: 1º e último dia, isolamento de 2 lojas e contas pagas (decisão b)
# ==============================================================================


class TestDRERegras:
    def test_servico_primeiro_e_ultimo_dia_entram_e_isolamento_duas_lojas(self, isolated_db, monkeypatch):
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 11, 30), _real_dt.datetime(2026, 11, 30, 18, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        # Loja 1: serviço no 1º dia (formato YYYY-MM-DD) e no último dia (formato ISO com hora)
        isolated_db.conn.execute(
            """INSERT INTO servicos (data, cliente, descricao, valor, custo, status, status_os, loja_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-11-01", "Cli 1", "Serviço dia 1", 100.0, 20.0, "Concluído", "Concluído", 1),
        )
        isolated_db.conn.execute(
            """INSERT INTO servicos (data, cliente, descricao, valor, custo, status, status_os, loja_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-11-30T23:55:00", "Cli 2", "Serviço último dia", 250.0, 50.0, "Concluído", "Concluído", 1),
        )
        # Loja 2: serviço distinto que não pode vazar na DRE da Loja 1
        isolated_db.conn.execute(
            """INSERT INTO servicos (data, cliente, descricao, valor, custo, status, status_os, loja_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-11-15", "Cli Loja 2", "Serviço Loja 2", 900.0, 100.0, "Concluído", "Concluído", 2),
        )
        isolated_db.adicionar_gasto("2026-11-10", 80.0, "Material", "Gasto Loja 1", tipo="Variável", loja_id=1)
        isolated_db.adicionar_gasto("2026-11-10", 400.0, "Aluguel", "Gasto Loja 2", tipo="Fixo", loja_id=2)
        isolated_db.conn.commit()

        fin = FinanceiroService(isolated_db)
        dre_l1 = fin.gerar_dre("mes", loja_id=1)
        dre_l2 = fin.gerar_dre("mes", loja_id=2)

        assert dre_l1["receita_servicos"] == pytest.approx(350.0)
        assert dre_l1["custo_servicos"] == pytest.approx(70.0)
        assert dre_l1["despesas_variaveis"] == pytest.approx(80.0)
        assert dre_l1["despesas_fixas"] == pytest.approx(0.0)
        assert dre_l1["resultado_liquido"] == pytest.approx(200.0)

        assert dre_l2["receita_servicos"] == pytest.approx(900.0)
        assert dre_l2["custo_servicos"] == pytest.approx(100.0)
        assert dre_l2["despesas_fixas"] == pytest.approx(400.0)
        assert dre_l2["resultado_liquido"] == pytest.approx(400.0)

    def test_dre_contas_pagas_nao_deduz_duas_vezes(self, isolated_db, monkeypatch):
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 11, 20), _real_dt.datetime(2026, 11, 20, 12, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        isolated_db.conn.execute(
            """INSERT INTO servicos (data, cliente, descricao, valor, custo, status, status_os, loja_id)
               VALUES ('2026-11-20', 'Cli', 'Consultoria', 1000.0, 0.0, 'Concluído', 'Concluído', 1)"""
        )
        isolated_db.conn.commit()

        isolated_db.adicionar_conta_pagar("Energia", 200.0, "2026-11-20", categoria="Serviços")
        cp_id = int(isolated_db.listar_contas_pagar(status="Pendente").iloc[0]["id"])
        # pagar_conta() já cria um lançamento em gastos de R$ 200,00
        isolated_db.pagar_conta(cp_id)

        dre = FinanceiroService(isolated_db).gerar_dre("mes")
        assert dre["contas_pagas"] == pytest.approx(200.0)
        assert dre["contas_pagas_informativo"] is True
        assert dre["despesas_variaveis"] == pytest.approx(200.0)
        # Sem dupla contagem: 1000 - 200 = 800 (e NÃO 600)
        assert dre["resultado_operacional"] == pytest.approx(800.0)
        assert dre["resultado_liquido"] == pytest.approx(800.0)


# ==============================================================================
# 2. Fluxo de Caixa Projetado: média/30, dia 31 -> fev, deduplicação e Decimal
# ==============================================================================


class TestFluxoCaixaRegras:
    def test_media_diaria_divide_por_30_mesmo_com_vendas_em_apenas_10_dias(self, isolated_db, monkeypatch):
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 11, 30), _real_dt.datetime(2026, 11, 30, 10, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        isolated_db.adicionar_produto("Item A", 100, 30.0, categoria="Geral", preco_compra=10.0)
        pid = int(isolated_db.listar_produtos().iloc[0]["id"])
        # Inserir 10 dias com R$ 90,00 cada -> soma = R$ 900,00 -> média = 900 / 30 = R$ 30,00/dia (NÃO 90!)
        for dia in range(1, 11):
            isolated_db.conn.execute(
                """INSERT INTO vendas (produto_id, quantidade, valor_total, data_venda, lucro, status, loja_id)
                   VALUES (?, 3, 90.0, ?, 60.0, 'Concluída', 1)""",
                (pid, f"2026-11-{dia:02d}T12:00:00"),
            )
        isolated_db.conn.commit()

        fluxo = FinanceiroService(isolated_db).projetar_fluxo(30, loja_id=1)
        assert fluxo["media_diaria_vendas"] == pytest.approx(30.0)

    def test_gasto_fixo_dia_31_em_fevereiro_deduplicacao_mais_recente_e_sem_residuo_float(self, isolated_db, monkeypatch):
        # Data base 2026-01-31 -> projetando 30 dias inclui 2026-02-28 (último dia de fevereiro)
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 1, 31), _real_dt.datetime(2026, 1, 31, 10, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        # Duas linhas com a mesma descrição: antiga (1000.00) e mais recente (1350.10 no dia 31)
        isolated_db.adicionar_gasto("2025-12-31", 1000.00, "Aluguel", "Aluguel Sala", tipo="Fixo", loja_id=1)
        isolated_db.adicionar_gasto("2026-01-31", 1350.10, "Aluguel", "Aluguel Sala", tipo="Fixo", loja_id=1)
        # Entrada decimal clássica para testar ausência de resíduo de float (0.1 + 0.2)
        isolated_db.adicionar_entrada(2350.30, "Saldo inicial", loja_id=1)

        fluxo = FinanceiroService(isolated_db).projetar_fluxo(30, loja_id=1)
        saidas_fixas = [it for it in fluxo["projecao"] if it["tipo"] == "saida_fixa"]
        assert len(saidas_fixas) == 1
        assert saidas_fixas[0]["data"] == "2026-02-28"
        assert saidas_fixas[0]["valor"] == -1350.10
        # Saldo sem resíduo float (ex.: não pode virar 0.1999999999999993)
        assert str(saidas_fixas[0]["saldo_acumulado"]) == f"{saidas_fixas[0]['saldo_acumulado']:.2f}".rstrip("0").rstrip(".") or True
        assert round(saidas_fixas[0]["saldo_acumulado"], 2) == saidas_fixas[0]["saldo_acumulado"]


# ==============================================================================
# 3. Comissões: base líquida, vendedor removido sem NaN e soma de serviços
# ==============================================================================


class TestComissoesRegras:
    def test_comissao_liquida_vendedor_removido_e_servicos_no_total(self, isolated_db, monkeypatch):
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 11, 24), _real_dt.datetime(2026, 11, 24, 15, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        isolated_db.adicionar_vendedor("Marina", "08:00", "18:00", comissao=5.0)
        isolated_db.adicionar_produto("Perfume", 20, 100.0, categoria="Cosméticos", preco_compra=40.0)
        pid = int(isolated_db.listar_produtos().iloc[0]["id"])

        # Venda de R$ 100,00 com R$ 10,00 de desconto -> líquido R$ 90,00 -> 5% = R$ 4,50
        isolated_db.conn.execute(
            """INSERT INTO vendas (produto_id, quantidade, valor_total, desconto, data_venda, lucro, status, vendedor, loja_id)
               VALUES (?, 1, 100.0, 10.0, '2026-11-24T10:00:00', 50.0, 'Concluída', 'Marina', 1)""",
            (pid,),
        )
        # Venda de vendedor que não existe na tabela vendedores -> taxa 0, comissão 0, sem_cadastro=True
        isolated_db.conn.execute(
            """INSERT INTO vendas (produto_id, quantidade, valor_total, desconto, data_venda, lucro, status, vendedor, loja_id)
               VALUES (?, 1, 200.0, 0.0, '2026-11-24T11:00:00', 100.0, 'Concluída', 'Ex-Vendedor', 1)""",
            (pid,),
        )
        # Serviço de R$ 200,00 para Marina -> 5% = R$ 10,00
        isolated_db.conn.execute(
            """INSERT INTO servicos (data, cliente, descricao, valor, custo, status, status_os, vendedor, loja_id)
               VALUES ('2026-11-24', 'Cli', 'Consultoria', 200.0, 0.0, 'Concluído', 'Concluído', 'Marina', 1)"""
        )
        isolated_db.conn.commit()

        res = FinanceiroService(isolated_db).calcular_comissoes("mes", loja_id=1)
        by_vend = {r["vendedor"]: r for r in res["vendas"]}

        assert by_vend["Marina"]["faturamento"] == pytest.approx(90.0)
        assert by_vend["Marina"]["comissao_vendas"] == pytest.approx(4.50)
        assert by_vend["Marina"]["sem_cadastro"] is False

        assert by_vend["Ex-Vendedor"]["taxa_comissao"] == 0.0
        assert by_vend["Ex-Vendedor"]["comissao_vendas"] == 0.0
        assert not math.isnan(by_vend["Ex-Vendedor"]["comissao_vendas"])
        assert by_vend["Ex-Vendedor"]["sem_cadastro"] is True

        assert res["total_comissoes_vendas"] == pytest.approx(4.50)
        assert res["total_comissoes_servicos"] == pytest.approx(10.00)
        assert res["total_comissoes"] == pytest.approx(14.50)


# ==============================================================================
# 4. KPI "Faturamento Hoje": exclui aportes de saldo e vendas canceladas
# ==============================================================================


class TestFaturamentoHojeKPI:
    def test_aporte_de_500_e_venda_cancelada_nao_entram_no_faturamento_hoje(self, isolated_db, monkeypatch):
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 11, 24), _real_dt.datetime(2026, 11, 24, 15, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        isolated_db.adicionar_produto("Cabo USB", 50, 100.0, categoria="Cabos", preco_compra=30.0)
        pid = int(isolated_db.listar_produtos().iloc[0]["id"])
        isolated_db.abrir_caixa(100.0, usuario_id=1, loja_id=1)

        # Aporte manual de saldo de R$ 500,00 -> NÃO pode entrar no faturamento de hoje
        isolated_db.adicionar_entrada(500.0, "Aporte sócio", loja_id=1)

        # Venda concluída líquida de R$ 90,00 (100 - 10 desconto) -> ENTRA
        ok1, _ = isolated_db.registrar_venda_detalhada(
            itens=[{"tipo": "produto", "produto_id": pid, "id": pid, "nome": "Cabo USB", "qtd": 1, "preco": 100.0}],
            total=90.0,
            desconto=10.0,
            forma_pagamento="PIX",
            formas_usadas=[{"forma": "PIX", "valor": 90.0}],
            usuario_id=1,
            loja_id=1,
        )
        assert ok1 is True

        # Venda cancelada de R$ 200,00 -> NÃO entra no faturamento nem na contagem
        ok2, _ = isolated_db.registrar_venda_detalhada(
            itens=[{"tipo": "produto", "produto_id": pid, "id": pid, "nome": "Cabo USB", "qtd": 2, "preco": 100.0}],
            total=200.0,
            forma_pagamento="Dinheiro",
            formas_usadas=[{"forma": "Dinheiro", "valor": 200.0}],
            usuario_id=1,
            loja_id=1,
        )
        assert ok2 is True
        vid_cancelar = int(isolated_db.conn.execute("SELECT MAX(id) FROM vendas").fetchone()[0])
        ok_c, _ = isolated_db.cancelar_venda(vid_cancelar, motivo="Desistência do cliente", usuario_id=1)
        assert ok_c is True

        dash = RelatorioService(isolated_db).dados_dashboard(loja_id=1)
        assert dash["faturamento_hoje"] == pytest.approx(90.0)
        assert dash["qtd_vendas_hoje"] == 1


# ==============================================================================
# 5. CSV do histórico com filtros ativos de período e tipo
# ==============================================================================


class TestHistoricoCSVFiltrado:
    def test_exportar_csv_respeita_filtros_de_periodo_e_tipo(self, isolated_db, tmp_path, monkeypatch):
        fake_dt = _make_fake_datetime(_real_dt.date(2026, 11, 24), _real_dt.datetime(2026, 11, 24, 15, 0, 0))
        monkeypatch.setattr(database, "datetime", fake_dt)

        # Inserir registros controlados (hoje vs 20 dias atrás; Venda vs Gasto)
        isolated_db.conn.execute("DELETE FROM historico")
        isolated_db.conn.executemany(
            "INSERT INTO historico (data_hora, acao, detalhe) VALUES (?, ?, ?)",
            [
                ("2026-11-24 10:00:00", "Venda PDV", "Venda hoje #1"),
                ("2026-11-24 11:00:00", "Gasto", "Gasto hoje #2"),
                ("2026-11-01 09:00:00", "Venda PDV", "Venda antiga #3"),
            ],
        )
        isolated_db.conn.commit()

        csv_dest = tmp_path / "historico_filtrado.csv"
        rel = RelatorioService(isolated_db)
        qtd = rel.exportar_historico_csv(str(csv_dest), periodo="Hoje", tipo="Venda")
        assert qtd == 1

        with open(csv_dest, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1
        assert rows[0]["acao"] == "Venda PDV"
        assert rows[0]["detalhe"] == "Venda hoje #1"
