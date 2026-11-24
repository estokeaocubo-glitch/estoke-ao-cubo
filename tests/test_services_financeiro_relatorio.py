"""
Testes da camada de serviços do Lote 4 (F1b):
1. Snapshot pré-extração (DRE, fluxo 30/60/90, comissões, dados_dashboard e resumo_caixa);
2. Ciclo completo do CaixaService + regra de supervisor na sangria + trilha de auditoria;
3. Correção de centavos em dividir_conta (100/3, 0.01/3, 10/4, 3 casas decimais e casos degenerados);
4. Cobrança PIX de fiado com Mercado Pago mockado (zero rede);
5. Contrato AST: ausência absoluta de imports de tkinter, customtkinter e matplotlib nos 3 services.
"""
from __future__ import annotations

import ast
import datetime as _real_dt
from decimal import Decimal
import gc
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ai_module
import database
from services.auth_service import AuthService
from services.caixa_service import CaixaService
from services.financeiro_service import FinanceiroService
from services.outbox_service import OutboxService
from services.relatorio_service import RelatorioService
from services.servico_service import ServicoService


# ==============================================================================
# Fixture de banco isolado
# ==============================================================================


@pytest.fixture
def isolated_db():
    tmpdir = tempfile.TemporaryDirectory()
    orig_path = database.get_app_data_path
    database.get_app_data_path = lambda *parts: (
        os.path.join(tmpdir.name, *parts) if parts else tmpdir.name
    )
    db = database.Database(db_name="test_lote4.db")
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
# 1. Snapshot gerado com o código original (commit 00b68a5) sobre seed fixo
# ==============================================================================

SNAPSHOT_PRE_EXTRACAO = {
    "dre": {
        "mes": {
            "periodo": "mes",
            "inicio": "2026-11-01",
            "fim": "2026-11-24",
            "receita_vendas": 860.0,
            "receita_servicos": 150.0,
            "receita_bruta": 1010.0,
            "deducoes": 20.0,
            "receita_liquida": 990.0,
            "cmv": 390.0,
            "custo_servicos": 30.0,
            "custo_total": 420.0,
            "lucro_bruto": 570.0,
            "despesas_fixas": 1200.0,
            "despesas_variaveis": 275.0,
            "despesas_total": 1475.0,
            "resultado_operacional": -905.0,
            "contas_pagas": 0.0,
            "contas_pagas_informativo": True,
            "resultado_liquido": -905.0,
            "margem_bruta": 57.58,
            "margem_liquida": -91.41,
        },
        "trimestre": {
            "periodo": "trimestre",
            "inicio": "2026-10-01",
            "fim": "2026-11-24",
            "receita_vendas": 860.0,
            "receita_servicos": 150.0,
            "receita_bruta": 1010.0,
            "deducoes": 20.0,
            "receita_liquida": 990.0,
            "cmv": 390.0,
            "custo_servicos": 30.0,
            "custo_total": 420.0,
            "lucro_bruto": 570.0,
            "despesas_fixas": 2400.0,
            "despesas_variaveis": 275.0,
            "despesas_total": 2675.0,
            "resultado_operacional": -2105.0,
            "contas_pagas": 0.0,
            "contas_pagas_informativo": True,
            "resultado_liquido": -2105.0,
            "margem_bruta": 57.58,
            "margem_liquida": -212.63,
        },
        "ano": {
            "periodo": "ano",
            "inicio": "2026-01-01",
            "fim": "2026-11-24",
            "receita_vendas": 860.0,
            "receita_servicos": 150.0,
            "receita_bruta": 1010.0,
            "deducoes": 20.0,
            "receita_liquida": 990.0,
            "cmv": 390.0,
            "custo_servicos": 30.0,
            "custo_total": 420.0,
            "lucro_bruto": 570.0,
            "despesas_fixas": 2400.0,
            "despesas_variaveis": 275.0,
            "despesas_total": 2675.0,
            "resultado_operacional": -2105.0,
            "contas_pagas": 0.0,
            "contas_pagas_informativo": True,
            "resultado_liquido": -2105.0,
            "margem_bruta": 57.58,
            "margem_liquida": -212.63,
        },
    },
    "fluxo": {
        "30": {
            "saldo_atual": -185.0,
            "projecao": [
                {"data": "2026-11-25", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": 15.67},
                {"data": "2026-12-02", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": 216.33},
                {"data": "2026-12-05", "tipo": "saida_fixa", "descricao": "Fixo: Aluguel loja", "valor": -1200.0, "saldo_acumulado": -983.67},
                {"data": "2026-12-05", "tipo": "entrada", "descricao": "Fiado: Compra a prazo", "valor": 220.0, "saldo_acumulado": -763.67},
                {"data": "2026-12-09", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -563.0},
                {"data": "2026-12-10", "tipo": "saida", "descricao": "Conta: Energia Elétrica", "valor": -160.0, "saldo_acumulado": -723.0},
                {"data": "2026-12-16", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -522.33},
                {"data": "2026-12-23", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -321.67},
            ],
            "media_diaria_vendas": 28.67,
        },
        "60": {
            "saldo_atual": -185.0,
            "projecao": [
                {"data": "2026-11-25", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": 15.67},
                {"data": "2026-12-02", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": 216.33},
                {"data": "2026-12-05", "tipo": "saida_fixa", "descricao": "Fixo: Aluguel loja", "valor": -1200.0, "saldo_acumulado": -983.67},
                {"data": "2026-12-05", "tipo": "entrada", "descricao": "Fiado: Compra a prazo", "valor": 220.0, "saldo_acumulado": -763.67},
                {"data": "2026-12-09", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -563.0},
                {"data": "2026-12-10", "tipo": "saida", "descricao": "Conta: Energia Elétrica", "valor": -160.0, "saldo_acumulado": -723.0},
                {"data": "2026-12-16", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -522.33},
                {"data": "2026-12-23", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -321.67},
                {"data": "2026-12-30", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -121.0},
                {"data": "2027-01-05", "tipo": "saida_fixa", "descricao": "Fixo: Aluguel loja", "valor": -1200.0, "saldo_acumulado": -1321.0},
                {"data": "2027-01-06", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -1120.33},
                {"data": "2027-01-09", "tipo": "saida", "descricao": "Conta: Energia Elétrica", "valor": -160.0, "saldo_acumulado": -1280.33},
                {"data": "2027-01-13", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -1079.67},
                {"data": "2027-01-20", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -879.0},
            ],
            "media_diaria_vendas": 28.67,
        },
        "90": {
            "saldo_atual": -185.0,
            "projecao": [
                {"data": "2026-11-25", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": 15.67},
                {"data": "2026-12-02", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": 216.33},
                {"data": "2026-12-05", "tipo": "saida_fixa", "descricao": "Fixo: Aluguel loja", "valor": -1200.0, "saldo_acumulado": -983.67},
                {"data": "2026-12-05", "tipo": "entrada", "descricao": "Fiado: Compra a prazo", "valor": 220.0, "saldo_acumulado": -763.67},
                {"data": "2026-12-09", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -563.0},
                {"data": "2026-12-10", "tipo": "saida", "descricao": "Conta: Energia Elétrica", "valor": -160.0, "saldo_acumulado": -723.0},
                {"data": "2026-12-16", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -522.33},
                {"data": "2026-12-23", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -321.67},
                {"data": "2026-12-30", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -121.0},
                {"data": "2027-01-05", "tipo": "saida_fixa", "descricao": "Fixo: Aluguel loja", "valor": -1200.0, "saldo_acumulado": -1321.0},
                {"data": "2027-01-06", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -1120.33},
                {"data": "2027-01-09", "tipo": "saida", "descricao": "Conta: Energia Elétrica", "valor": -160.0, "saldo_acumulado": -1280.33},
                {"data": "2027-01-13", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -1079.67},
                {"data": "2027-01-20", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -879.0},
                {"data": "2027-01-27", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -678.33},
                {"data": "2027-02-03", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -477.67},
                {"data": "2027-02-05", "tipo": "saida_fixa", "descricao": "Fixo: Aluguel loja", "valor": -1200.0, "saldo_acumulado": -1677.67},
                {"data": "2027-02-10", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -1477.0},
                {"data": "2027-02-17", "tipo": "projecao_vendas", "descricao": "Projeção vendas (semana)", "valor": 200.67, "saldo_acumulado": -1276.33},
            ],
            "media_diaria_vendas": 28.67,
        },
    },
    "comissoes": {
        "semana": {
            "periodo_inicio": "2026-11-23",
            "vendas": [
                {"vendedor": "Ana Souza", "taxa_comissao": 8.0, "sem_cadastro": False, "num_vendas": 1, "faturamento": 360.0, "lucro": 210.0, "comissao_vendas": 28.8},
                {"vendedor": "Carlos Silva", "taxa_comissao": 5.0, "sem_cadastro": False, "num_vendas": 1, "faturamento": 480.0, "lucro": 240.0, "comissao_vendas": 24.0},
            ],
            "servicos": [
                {"vendedor": "Carlos Silva", "taxa_comissao": 5.0, "sem_cadastro": False, "num_servicos": 1, "faturamento_servicos": 150.0, "comissao_servicos": 7.5},
            ],
        },
        "mes": {
            "periodo_inicio": "2026-11-01",
            "vendas": [
                {"vendedor": "Ana Souza", "taxa_comissao": 8.0, "sem_cadastro": False, "num_vendas": 1, "faturamento": 360.0, "lucro": 210.0, "comissao_vendas": 28.8},
                {"vendedor": "Carlos Silva", "taxa_comissao": 5.0, "sem_cadastro": False, "num_vendas": 1, "faturamento": 480.0, "lucro": 240.0, "comissao_vendas": 24.0},
            ],
            "servicos": [
                {"vendedor": "Carlos Silva", "taxa_comissao": 5.0, "sem_cadastro": False, "num_servicos": 1, "faturamento_servicos": 150.0, "comissao_servicos": 7.5},
            ],
        },
        "ano": {
            "periodo_inicio": "2026-01-01",
            "vendas": [
                {"vendedor": "Ana Souza", "taxa_comissao": 8.0, "sem_cadastro": False, "num_vendas": 1, "faturamento": 360.0, "lucro": 210.0, "comissao_vendas": 28.8},
                {"vendedor": "Carlos Silva", "taxa_comissao": 5.0, "sem_cadastro": False, "num_vendas": 1, "faturamento": 480.0, "lucro": 240.0, "comissao_vendas": 24.0},
            ],
            "servicos": [
                {"vendedor": "Carlos Silva", "taxa_comissao": 5.0, "sem_cadastro": False, "num_servicos": 1, "faturamento_servicos": 150.0, "comissao_servicos": 7.5},
            ],
        },
    },
    "dados_dashboard": {
        "estoque_pizza": {
            "labels": ["Teclado Mecânico", "Mouse Gamer", "Monitor 24", "Headset USB", "Webcam HD", "Outros"],
            "sizes": [38, 32, 20, 15, 10, 12],
        },
        "gastos_barras": {
            "categorias": ["Aluguel", "Material", "Transporte"],
            "valores": [2400.0, 180.0, 95.0],
        },
        "faturamento_hoje": 990.0,
        "qtd_vendas_hoje": 2,
        "lucro_mes": 450.0,
        "faturamento_mes": 860.0,
        "fiado_pendente": 220.0,
        "comparativo_mensal": {
            "atual": {"faturamento": 860.0, "lucro": 450.0, "vendas": 2},
            "anterior": {"faturamento": 0, "lucro": 0, "vendas": 0},
        },
    },
    "resumo_caixa": {
        "id": 1,
        "usuario_id": 1,
        "loja_id": 1,
        "data_abertura": "2026-11-24T14:30:00",
        "valor_abertura": 200.0,
        "total_vendas": 840.0,
        "total_sangrias": 100.0,
        "total_suprimentos": 50.0,
        "total_dinheiro": 480.0,
        "total_pix": 360.0,
        "total_cartao": 0.0,
        "total_fiado": 0.0,
        "esperado": 630.0,
        "esperado_dinheiro": 630.0,
        "dinheiro": {"total": 480.0, "quantidade": 1, "esperado": 630.0},
        "pix": {"total": 360.0, "quantidade": 1},
        "cartao": {"total": 0.0, "quantidade": 0},
        "fiado": {"total": 0.0, "quantidade": 0},
    },
}


@pytest.fixture
def snapshot_seed_db(monkeypatch):
    """Recria exatamente o banco de seed determinístico (2026-11-24T14:30:00) usado no snapshot original."""
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
    monkeypatch.setattr(ai_module, "datetime", fake_dt_mod)

    tmpdir = tempfile.TemporaryDirectory()
    monkeypatch.setattr(
        database,
        "get_app_data_path",
        lambda *parts: os.path.join(tmpdir.name, *parts) if parts else tmpdir.name,
    )
    db = database.Database(db_name="seed_snapshot.db")

    db.adicionar_vendedor("Carlos Silva", "08:00", "18:00", comissao=5.0)
    db.adicionar_vendedor("Ana Souza", "09:00", "19:00", comissao=8.0)
    db.adicionar_cliente("João Cliente", "11988887777", "Cliente VIP")
    cli_id = int(db.listar_clientes().iloc[0]["id"])

    for nome, qtd, pv, pc, cat in [
        ("Teclado Mecânico", 40, 250.0, 120.0, "Periféricos"),
        ("Mouse Gamer", 35, 120.0, 50.0, "Periféricos"),
        ("Monitor 24", 20, 900.0, 600.0, "Monitores"),
        ("Headset USB", 15, 180.0, 80.0, "Áudio"),
        ("Webcam HD", 10, 150.0, 70.0, "Vídeo"),
        ("Cabo HDMI", 8, 35.0, 10.0, "Cabos"),
        ("Mousepad", 4, 40.0, 12.0, "Acessórios"),
    ]:
        db.adicionar_produto(nome, qtd, pv, categoria=cat, preco_compra=pc)

    prods = db.listar_produtos()
    pid_tec = int(prods[prods["nome"] == "Teclado Mecânico"].iloc[0]["id"])
    pid_mou = int(prods[prods["nome"] == "Mouse Gamer"].iloc[0]["id"])

    db.adicionar_entrada(1500.0, "Capital Inicial")
    ok_cx, _ = db.abrir_caixa(200.0, "Turno manhã", usuario_id=1, loja_id=1)
    assert ok_cx is True
    cx_id = db.caixa_aberto(1)["id"]

    ok_v1, _ = db.registrar_venda_detalhada(
        itens=[{"tipo": "produto", "produto_id": pid_tec, "id": pid_tec, "nome": "Teclado Mecânico", "qtd": 2, "preco": 250.0}],
        total=480.0,
        desconto=20.0,
        cliente="João Cliente",
        forma_pagamento="Dinheiro",
        vendedor="Carlos Silva",
        formas_usadas=[{"forma": "Dinheiro", "valor": 480.0}],
        usuario_id=1,
    )
    assert ok_v1 is True

    ok_v2, _ = db.registrar_venda_detalhada(
        itens=[{"tipo": "produto", "produto_id": pid_mou, "id": pid_mou, "nome": "Mouse Gamer", "qtd": 3, "preco": 120.0}],
        total=360.0,
        desconto=0.0,
        cliente="Consumidor Final",
        forma_pagamento="PIX",
        vendedor="Ana Souza",
        formas_usadas=[{"forma": "PIX", "valor": 360.0}],
        usuario_id=1,
    )
    assert ok_v2 is True

    db.registrar_suprimento(cx_id, 1, 50.0, "Reforço de troco", "Moedas")
    db.registrar_sangria(cx_id, 1, 100.0, "Depósito em Cofre", "Parcial")
    db.registrar_servico("João Cliente", "Formatação PC", 150.0, custo=30.0, forma_pagamento="Dinheiro", vendedor="Carlos Silva")

    db.adicionar_gasto("2026-10-15", 1200.0, "Aluguel", "Aluguel loja", tipo="Fixo")
    db.adicionar_gasto("2026-11-05", 1200.0, "Aluguel", "Aluguel loja", tipo="Fixo")
    db.adicionar_gasto("2026-11-12", 180.0, "Material", "Bobinas térmicas", tipo="Variável")
    db.adicionar_gasto("2026-11-18", 95.0, "Transporte", "Entrega motoboy", tipo="Variável")

    db.adicionar_conta_pagar("Energia Elétrica", 320.0, "2026-12-10", None, "Serviços", total_parcelas=2, observacao="CPFL")
    db.registrar_fiado(cli_id, 220.0, "Compra a prazo", "2026-12-05")

    yield db, SNAPSHOT_PRE_EXTRACAO

    try:
        db.close()
    except Exception:
        pass
    gc.collect()
    for _ in range(10):
        try:
            tmpdir.cleanup()
            break
        except PermissionError:
            time.sleep(0.1)


class TestSnapshotCodigoOriginal:
    """Garante paridade exata pós-extração contra o snapshot gerado no commit anterior."""

    def test_dre_identica_ao_snapshot(self, snapshot_seed_db):
        db, snap = snapshot_seed_db
        fin = FinanceiroService(db)
        for periodo in ("mes", "trimestre", "ano"):
            assert fin.gerar_dre(periodo) == snap["dre"][periodo]

    def test_fluxo_30_60_90_identico_ao_snapshot(self, snapshot_seed_db):
        db, snap = snapshot_seed_db
        fin = FinanceiroService(db)
        for dias in (30, 60, 90):
            assert fin.projetar_fluxo(dias) == snap["fluxo"][str(dias)]

    def test_comissoes_identicas_ao_snapshot(self, snapshot_seed_db):
        db, snap = snapshot_seed_db
        fin = FinanceiroService(db)
        for periodo in ("semana", "mes", "ano"):
            atual = fin.calcular_comissoes(periodo)
            esperado = snap["comissoes"][periodo]
            assert atual["periodo_inicio"] == esperado["periodo_inicio"]
            assert atual["vendas"] == esperado["vendas"]
            assert atual["servicos"] == esperado["servicos"]
            assert atual["total_comissoes_vendas"] == pytest.approx(52.8)
            assert atual["total_comissoes_servicos"] == pytest.approx(7.5)
            assert atual["total_comissoes"] == pytest.approx(60.3)

    def test_dados_dashboard_e_resumo_caixa_identicos_ao_snapshot(self, snapshot_seed_db):
        db, snap = snapshot_seed_db
        rel = RelatorioService(db)
        cx = CaixaService(db)

        dash = rel.dados_dashboard()
        for chave, valor_esp in snap["dados_dashboard"].items():
            assert dash[chave] == valor_esp

        assert cx.resumo_caixa(usuario_id=1) == snap["resumo_caixa"]


# ==============================================================================
# 2. CaixaService: ciclo completo + autorização de supervisor + auditoria
# ==============================================================================


class TestCaixaServiceAutorizacao:
    def test_ciclo_completo_abrir_suprimento_sangria_fechar(self, isolated_db):
        # Criar operador caixa e supervisor gerente
        ok_op, _ = isolated_db.adicionar_usuario("Operador Caixa", "op_caixa", "SenhaForte@123", perfil="caixa", loja_id=1)
        ok_sup, _ = isolated_db.adicionar_usuario("Mariana Gerente", "sup_gerente", "Gerente@12345", perfil="gerente", loja_id=1)
        assert ok_op and ok_sup
        operador_caixa = isolated_db.autenticar_usuario("op_caixa", "SenhaForte@123")
        sup_user = isolated_db.autenticar_usuario("sup_gerente", "Gerente@12345")
        op_id = operador_caixa["id"]
        sup_id = sup_user["id"]

        isolated_db.adicionar_produto("Café Torrado", 20, 30.0, categoria="Bebidas", preco_compra=15.0)
        pid = int(isolated_db.listar_produtos().iloc[0]["id"])

        caixa_svc = CaixaService(isolated_db, operador=operador_caixa)
        auth_svc = AuthService(isolated_db)

        # 1. Abrir caixa com R$ 150,00
        ok_ab, _ = caixa_svc.abrir_caixa(150.0, "Início turno", usuario_id=op_id, loja_id=1)
        assert ok_ab is True

        # 2. Suprimento de R$ 50,00 -> gaveta = 200,00
        ok_sup_mov, _ = caixa_svc.registrar_suprimento(50.0, "Troco moedas", usuario_id=op_id)
        assert ok_sup_mov is True

        # 3. Venda em dinheiro de R$ 60,00 -> gaveta = 260,00
        ok_v, msg_v = isolated_db.registrar_venda_detalhada(
            itens=[{"tipo": "produto", "produto_id": pid, "id": pid, "nome": "Café Torrado", "qtd": 2, "preco": 30.0}],
            total=60.0,
            forma_pagamento="Dinheiro",
            formas_usadas=[{"forma": "Dinheiro", "valor": 60.0}],
            usuario_id=op_id,
        )
        assert ok_v is True, msg_v

        # 4. Sangria de R$ 80,00 com autorização real de gerente -> gaveta esperada = 180,00
        autorizacao = auth_svc.autorizar_supervisor(
            "sup_gerente",
            "Gerente@12345",
            acao="Sangria de R$ 80.00 (Depósito em Cofre)",
        )
        ok_san, _ = caixa_svc.registrar_sangria(
            80.0,
            "Depósito em Cofre",
            autorizacao=autorizacao,
            usuario_id=op_id,
            observacao="Malote tarde",
        )
        assert ok_san is True

        resumo_parcial = caixa_svc.resumo_caixa(usuario_id=op_id)
        assert resumo_parcial["valor_abertura"] == pytest.approx(150.0)
        assert resumo_parcial["total_suprimentos"] == pytest.approx(50.0)
        assert resumo_parcial["total_dinheiro"] == pytest.approx(60.0)
        assert resumo_parcial["total_sangrias"] == pytest.approx(80.0)
        assert resumo_parcial["esperado"] == pytest.approx(180.0)

        # 5. Fechar caixa com conferência exata
        ok_fech, resultado = caixa_svc.fechar_caixa(
            180.0,
            observacao="Fechamento redondo",
            usuario_id=op_id,
            conferido_pix=0.0,
            conferido_cartao=0.0,
        )
        assert ok_fech is True
        assert resultado["esperado"] == pytest.approx(180.0)
        assert resultado["informado"] == pytest.approx(180.0)
        assert resultado["diferenca"] == pytest.approx(0.0)
        assert caixa_svc.caixa_aberto(op_id) is None

        # Auditoria registra quem autorizou e cadeia SHA-256 permanece íntegra
        rel_svc = RelatorioService(isolated_db)
        aud = rel_svc.verificar_auditoria()
        assert aud["integro"] is True
        assert aud["ids_adulterados"] == []

        hist = rel_svc.listar_historico()
        acoes_sangria = [h for h in hist if h["acao"] == "Autorização Sangria"]
        assert len(acoes_sangria) == 1
        assert "Mariana Gerente" in acoes_sangria[0]["detalhe"]
        assert f"ID: {sup_id}" in acoes_sangria[0]["detalhe"]

    def test_sangria_sem_autorizacao_levanta_permission_error(self, isolated_db):
        ok_op, _ = isolated_db.adicionar_usuario("Vendedor Um", "vendedor1", "SenhaForte@123", perfil="vendedor", loja_id=1)
        assert ok_op is True
        operador_vendedor = isolated_db.autenticar_usuario("vendedor1", "SenhaForte@123")
        op_id = operador_vendedor["id"]

        caixa_svc = CaixaService(isolated_db, operador=operador_vendedor)
        caixa_svc.abrir_caixa(200.0, usuario_id=op_id)

        # Sem dict de autorização -> PermissionError
        with pytest.raises(PermissionError):
            caixa_svc.registrar_sangria(50.0, "Cofre", autorizacao=None, usuario_id=op_id)

        # Com dict não aprovado -> PermissionError
        with pytest.raises(PermissionError):
            caixa_svc.registrar_sangria(
                50.0,
                "Cofre",
                autorizacao={"approved": False, "supervisor_id": 1, "supervisor_nome": "Admin"},
                usuario_id=op_id,
            )

        # Com perfil sem poder de supervisão -> PermissionError
        with pytest.raises(PermissionError):
            caixa_svc.registrar_sangria(
                50.0,
                "Cofre",
                autorizacao={"approved": True, "supervisor_id": op_id, "supervisor_nome": "Vendedor Um", "supervisor_perfil": "vendedor"},
                usuario_id=op_id,
            )

        # Nenhuma sangria foi persistida
        cx = caixa_svc.caixa_aberto(op_id)
        movs = caixa_svc.listar_movimentacoes_turno(cx["id"])
        assert movs["sangrias"] == []

    def test_sangria_isencao_quando_gerente_ja_esta_logado(self, isolated_db):
        ok_ger, _ = isolated_db.adicionar_usuario("Roberto Gerente", "gerente_logado", "Gerente@12345", perfil="gerente", loja_id=1)
        assert ok_ger is True
        operador_gerente = isolated_db.autenticar_usuario("gerente_logado", "Gerente@12345")
        ger_id = operador_gerente["id"]

        caixa_svc = CaixaService(isolated_db, operador=operador_gerente)
        caixa_svc.abrir_caixa(300.0, usuario_id=ger_id)

        ok, _ = caixa_svc.registrar_sangria(75.0, "Depósito em Cofre", autorizacao=None, usuario_id=ger_id)
        assert ok is True

        hist = RelatorioService(isolated_db).listar_historico()
        acoes_aut = [h for h in hist if h["acao"] == "Autorização Sangria"]
        assert len(acoes_aut) == 1
        assert "Roberto Gerente" in acoes_aut[0]["detalhe"]


# ==============================================================================
# 3. dividir_conta em centavos inteiros (Decimal + ROUND_HALF_UP)
# ==============================================================================


class TestDividirContaCentavos:
    def _criar_comanda_valor(self, db, valor_unitario: float, desconto: float = 0.0) -> int:
        db.adicionar_produto("Prato Teste", 50, max(valor_unitario, 0.01), categoria="Restaurante", preco_compra=1.0)
        pid = int(db.listar_produtos().iloc[-1]["id"])
        cid = db.abrir_comanda(cliente="Mesa Teste")
        db.adicionar_item_comanda(cid, pid, "Prato Teste", 1, valor_unitario)
        if desconto != 0.0:
            db.conn.execute("UPDATE comandas SET desconto = ? WHERE id = ?", (desconto, cid))
            db.conn.commit()
        return cid

    def test_100_por_3_distribui_centavo_exato(self, isolated_db):
        cid = self._criar_comanda_valor(isolated_db, 100.00)
        div = ServicoService(isolated_db).dividir_conta(cid, 3)

        assert div["partes"] == [33.34, 33.33, 33.33]
        assert sum(Decimal(str(p)) for p in div["partes"]) == Decimal("100.00")
        assert div["total"] == pytest.approx(100.00)
        assert div["pessoas"] == 3
        assert div["valor_por_pessoa"] == pytest.approx(33.33)

    def test_um_centavo_por_3_e_10_por_4(self, isolated_db):
        # R$ 0,01 / 3 -> [0.01, 0.0, 0.0]
        cid_1c = self._criar_comanda_valor(isolated_db, 0.01)
        div_1c = isolated_db.dividir_conta(cid_1c, 3)
        assert div_1c["partes"] == [0.01, 0.0, 0.0]
        assert sum(Decimal(str(p)) for p in div_1c["partes"]) == Decimal("0.01")

        # R$ 10,00 / 4 -> [2.5, 2.5, 2.5, 2.5]
        cid_10 = self._criar_comanda_valor(isolated_db, 10.00)
        div_10 = isolated_db.dividir_conta(cid_10, 4)
        assert div_10["partes"] == [2.5, 2.5, 2.5, 2.5]
        assert sum(Decimal(str(p)) for p in div_10["partes"]) == Decimal("10.00")
        assert div_10["valor_por_pessoa"] == pytest.approx(2.5)

    def test_valores_com_3_casas_decimais_round_half_up(self, isolated_db):
        # 10.005 -> ROUND_HALF_UP -> 10.01 (1001 centavos / 3 = 334, 334, 333)
        cid_up = self._criar_comanda_valor(isolated_db, 10.005)
        div_up = isolated_db.dividir_conta(cid_up, 3)
        assert div_up["total"] == pytest.approx(10.01)
        assert div_up["partes"] == [3.34, 3.34, 3.33]
        assert sum(Decimal(str(p)) for p in div_up["partes"]) == Decimal("10.01")

        # 10.004 -> ROUND_HALF_UP -> 10.00 (1000 centavos / 3 = 334, 333, 333)
        cid_down = self._criar_comanda_valor(isolated_db, 10.004)
        div_down = isolated_db.dividir_conta(cid_down, 3)
        assert div_down["total"] == pytest.approx(10.00)
        assert div_down["partes"] == [3.34, 3.33, 3.33]
        assert sum(Decimal(str(p)) for p in div_down["partes"]) == Decimal("10.00")

    def test_casos_degenerados_n_menor_igual_zero_e_total_negativo(self, isolated_db):
        cid = self._criar_comanda_valor(isolated_db, 50.00)
        div_zero = isolated_db.dividir_conta(cid, 0)
        assert div_zero["pessoas"] == 0
        assert div_zero["valor_por_pessoa"] == pytest.approx(50.00)
        assert div_zero["partes"] == [50.00]

        cid_neg = self._criar_comanda_valor(isolated_db, 10.00, desconto=15.00)
        div_neg = isolated_db.dividir_conta(cid_neg, 2)
        assert div_neg["total"] == pytest.approx(-5.0)
        assert div_neg["valor_por_pessoa"] == pytest.approx(-2.5)
        assert div_neg["partes"] == [-2.5, -2.5]


# ==============================================================================
# 4. Fiado PIX com Mercado Pago mockado (zero rede) + Financeiro CRUD/Pacote
# ==============================================================================


class TestFiadoPixMercadoPagoMockado:
    def test_cobranca_fiado_pix_mercadopago_mockado_sem_rede(self, isolated_db, monkeypatch):
        def _bloquear_rede(*args, **kwargs):
            raise AssertionError("Chamada HTTP real detectada durante teste de Fiado PIX!")

        monkeypatch.setattr(urllib.request, "urlopen", _bloquear_rede)

        isolated_db.adicionar_cliente(" Fernanda Lima ", "11977776666", "Cliente recorrente")
        cli_row = isolated_db.listar_clientes().iloc[0]
        cli_id = int(cli_row["id"])
        isolated_db.registrar_fiado(cli_id, 145.90, "Venda #88", "2026-12-15")
        conta_id = int(isolated_db.listar_fiados(status="Pendente").iloc[0]["id"])

        chamadas_mp = []

        class FakeMPService:
            def esta_configurado(self) -> bool:
                return True

            def criar_cobranca_pix(self, valor, descricao="", cliente_email=None, cliente_nome=None, id_venda=None):
                chamadas_mp.append({"valor": valor, "descricao": descricao, "cliente_nome": cliente_nome, "id_venda": id_venda})
                return {
                    "sucesso": True,
                    "payment_id": "MP-998877",
                    "status": "pending",
                    "qr_code": "00020126580014BR.GOV.BCB.PIX0136mock-mp-qr-fiado-88",
                    "qr_code_base64": "iVBORw0KGgoAAAANSUhEUg==",
                    "valor": valor,
                }

        outbox = OutboxService(isolated_db)
        rel_svc = RelatorioService(isolated_db, mp_service=FakeMPService(), outbox_service=outbox)

        res = rel_svc.gerar_cobranca_fiado_pix(conta_id=conta_id)
        assert len(chamadas_mp) == 1
        assert chamadas_mp[0]["valor"] == pytest.approx(145.90)
        assert chamadas_mp[0]["id_venda"] == f"fiado-{conta_id}"
        assert res["origem_pix"] == "mercadopago"
        assert res["payment_id"] == "MP-998877"
        assert "00020126580014BR.GOV.BCB.PIX0136mock-mp-qr-fiado-88" in res["mensagem"]
        assert res["enfileirado"] is True

        # Confirmar enfileiramento na Outbox WhatsApp
        row_out = isolated_db.conn.execute(
            "SELECT tipo, status FROM outbox ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row_out == ("whatsapp", "pendente")

    def test_cobranca_fiado_fallback_chave_estatica_e_bloqueio_conta_paga(self, isolated_db, monkeypatch):
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("Rede proibida!")),
        )
        isolated_db.salvar_config_nota({"empresa_nome": "Loja Cubo", "chave_pix": "pix@lojacubo.com.br"})
        isolated_db.adicionar_cliente("Marcos Rocha", "11966665555", "")
        cli_id = int(isolated_db.listar_clientes().iloc[0]["id"])
        isolated_db.registrar_fiado(cli_id, 80.0, "Fiado balcão", "2026-12-20")
        conta_id = int(isolated_db.listar_fiados(status="Pendente").iloc[0]["id"])

        class FailingMPService:
            def esta_configurado(self) -> bool:
                return True

            def criar_cobranca_pix(self, **kwargs):
                return {"sucesso": False, "erro": "timeout simulado"}

        rel_svc = RelatorioService(isolated_db, mp_service=FailingMPService())
        res = rel_svc.gerar_cobranca_fiado_pix(conta_id=conta_id, enfileirar_whatsapp=False)
        # Preserva exatamente o comportamento legado (onde gerar_payload_pix cai no fallback só com a chave PIX)
        assert res["origem_pix"] in ("chave_estatica", "chave_estatica_sem_emv")
        assert "pix@lojacubo.com.br" in res["mensagem"]

        # Quitar e confirmar recusa para conta já paga
        rel_svc.quitar_fiado(conta_id)
        with pytest.raises(ValueError, match="paga"):
            rel_svc.gerar_cobranca_fiado_pix(conta_id=conta_id, enfileirar_whatsapp=False)

    def test_financeiro_crud_previsao_e_pacote_contador(self, isolated_db, tmp_path):
        fin = FinanceiroService(isolated_db)
        novo_saldo = fin.adicionar_entrada("500,50", "Aporte")
        assert novo_saldo == pytest.approx(500.50)

        fin.crud_gastos("adicionar", data="2026-11-10", valor="120,00", categoria="Material", descricao="Papel")
        fin.crud_gastos("adicionar", data="2026-11-20", valor="180,00", categoria="Serviços", descricao="Internet")
        gastos = fin.crud_gastos("listar", invertido=True)
        assert len(gastos) == 2

        fin.crud_contas_pagar(
            "adicionar",
            descricao="Aluguel Galpão",
            valor="900,00",
            data_vencimento="2026-12-10",
            categoria="Aluguel",
            total_parcelas=1,
        )
        cp = fin.crud_contas_pagar("listar", status="Pendente")
        assert cp["total_pendente"] == pytest.approx(900.0)
        fin.crud_contas_pagar("pagar", conta_id=cp["contas"][0]["id"])
        assert fin.crud_contas_pagar("listar", status="Pendente")["total_pendente"] == pytest.approx(0.0)

        rel = RelatorioService(isolated_db)
        prev = rel.previsao_vendas(treinar=True)
        assert prev["treinado"] is True
        assert isinstance(prev["valor"], float)

        zip_dest = str(tmp_path / "pacote_contador_teste.zip")
        ok_zip, info_zip = rel.gerar_pacote_contador(mes_ano="2026-11", destino_zip_path=zip_dest)
        assert ok_zip is True
        assert os.path.exists(info_zip["zip_path"])


# ==============================================================================
# 5. Contrato AST: proibição de imports de UI / Matplotlib nos 3 services
# ==============================================================================


class TestContratoASTServicesLote4:
    FORBIDDEN_ROOTS = {"tkinter", "customtkinter", "matplotlib"}
    SERVICE_FILES = (
        "caixa_service.py",
        "financeiro_service.py",
        "relatorio_service.py",
    )

    @pytest.mark.parametrize("filename", SERVICE_FILES)
    def test_service_nao_importa_tkinter_customtkinter_ou_matplotlib(self, filename):
        services_dir = Path(__file__).resolve().parent.parent / "services"
        filepath = services_dir / filename
        assert filepath.exists(), f"Arquivo ausente: {filepath}"

        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
        violacoes: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg in self.FORBIDDEN_ROOTS:
                        violacoes.append(f"L{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_pkg = node.module.split(".")[0]
                    if root_pkg in self.FORBIDDEN_ROOTS:
                        violacoes.append(f"L{node.lineno}: from {node.module} import ...")

        assert violacoes == [], f"Imports proibidos em {filename}: {violacoes}"
