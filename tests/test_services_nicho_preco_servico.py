"""
Testes unitários do Lote 2 da F1b:
- NichoService: snapshot dos 18 nichos (mesmos botões, mesma ordem), ação válida,
  cobertura no PERMISSAO_MAP e get_os_config.
- PrecificacaoService: casos normais e de borda (custo 0, preço 0, preço < custo,
  3+ casas decimais), calcular_markup e sugerir_preco (idêntico ao PricingEngine).
- ServicoService: registrar_os, avancar_status, gerar_nota_os sem custo interno,
  montar_mensagem_whatsapp_os sem envio real, fechar_comanda e dividir_conta
  (com xfail documentado para centavos em R$ 100,00 / 3).
- Contrato AST: zero imports de tkinter/customtkinter nos 3 novos services.
"""
import ast
import pathlib
from unittest.mock import MagicMock

import pytest

import ai_module
from database import Database
from services.auth_service import PERMISSAO_MAP
from services.nicho_service import NichoService
from services.precificacao_service import PrecificacaoService
from services.servico_service import ServicoService


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Cria instância isolada do Database em diretório temporário."""
    monkeypatch.setenv("ESTOKE_DATA_DIR", str(tmp_path))
    db = Database(db_name="test_lote2.db")
    yield db
    if not getattr(db, "_closed", False):
        db.close()


def _snapshot_original_sidebar(bt: str) -> list[str]:
    """Reprodução exata da lógica original de main.py::_build_main_ui antes do Lote 2."""
    sidebar = [
        "SALDO", "GASTOS", "CAIXA", "KITS", "INVENTÁRIO", "COMPRAS", "CATEGORIAS", "FORNECEDORES",
        "CLIENTES", "ANOTA TUDO", "VENDEDORES", "CONTAS PAGAR", "ETIQUETAS", "CONFIG", "CHAT IA",
    ]
    if bt == "pet_shop_agro":
        sidebar.insert(2, "BANHO & TOSA")
        sidebar.insert(3, "PETS & VACINAS")
        sidebar.insert(4, "RAÇÃO A GRANEL")
    if bt == "oficina_mecanica":
        sidebar.insert(2, "ORDEM SERVIÇO")
        sidebar.insert(3, "MECÂNICOS")
        sidebar.insert(4, "VEÍCULOS")
        sidebar.insert(5, "ORÇAMENTOS")
    if bt == "farmacia_suplementos":
        sidebar.insert(2, "LOTES & VALIDADE")
        sidebar.insert(3, "VENCIMENTOS")
    if bt == "papelaria_livraria":
        sidebar.insert(2, "LEITOR RÁPIDO")
        sidebar.insert(3, "REPOSIÇÃO")
    if bt == "otica_joalheria":
        sidebar.insert(2, "RECEITAS & GRAUS")
        sidebar.insert(3, "Nº SÉRIE")
        sidebar.insert(4, "GARANTIAS")
    if bt == "treinamento_recorrencia":
        sidebar.insert(2, "MENSALIDADES")
        sidebar.insert(3, "ALUNOS & TURMAS")
    if bt in ("padaria_acougue", "artesanato", "moda_customizacao", "delivery_alimentacao", "estetica_automotiva"):
        sidebar.insert(2, "INSUMOS")
        sidebar.insert(3, "PRODUÇÃO")
    if bt in ("loja_roupa", "moda_customizacao"):
        sidebar.insert(2, "TROCAS")
        if bt == "moda_customizacao":
            sidebar.insert(3, "AJUSTES / ATELIÊ")
    if bt == "variados":
        sidebar.insert(2, "REPOSIÇÃO")
    if bt in ("restaurante", "delivery_alimentacao"):
        sidebar.insert(2, "PEDIDOS / MESAS")
        sidebar.insert(3, "COZINHA")
        if bt == "delivery_alimentacao":
            sidebar.insert(4, "COMBOS")
    if bt == "tecnologia_informatica":
        sidebar.insert(2, "ORDEM SERVIÇO")
        sidebar.insert(3, "HARDWARE")
        sidebar.insert(4, "GARANTIAS")
    if bt == "estetica_automotiva":
        sidebar.insert(2, "AGENDAMENTOS")
        sidebar.insert(3, "VEÍCULOS")
    if bt == "festas_eventos":
        sidebar.insert(2, "LOCAÇÃO")
        sidebar.insert(3, "RESERVAS")
    if bt == "estetica_arte_corporal":
        sidebar.insert(2, "AGENDAMENTOS")
        sidebar.insert(3, "PROFISSIONAIS")
    if bt == "casa_construcao":
        sidebar.insert(2, "ORÇAMENTOS")
        sidebar.insert(3, "CONDOMÍNIOS")
    return sidebar


class TestNichoService:
    """Valida catálogo dos 18 nichos, ordem de botões, ações e cobertura RBAC."""

    def test_18_nichos_snapshot_ordem_acoes_e_permissao_map(self):
        svc = NichoService()
        nichos = svc.listar_nichos()
        assert len(nichos) == 18

        sem_chave_rbac = []
        for item in nichos:
            bt = item["key"]
            esperado = _snapshot_original_sidebar(bt)
            atual_itens = svc.get_sidebar_buttons(bt)
            atual_textos = [b["texto"] for b in atual_itens]

            assert atual_textos == esperado, f"Ordem/botões divergiram para nicho '{bt}'"

            for b in atual_itens:
                assert b["acao"] and isinstance(b["acao"], str)
                assert svc.acao_do_botao(b["texto"]) == b["acao"]
                if b["texto"] not in PERMISSAO_MAP:
                    sem_chave_rbac.append((bt, b["texto"]))

        assert sem_chave_rbac == [], f"Botões sem chave no PERMISSAO_MAP: {sem_chave_rbac}"

    def test_get_os_config_por_nicho(self):
        svc = NichoService()
        cfg_rest = svc.get_os_config("restaurante")
        assert cfg_rest["statuses"] == ["Recebido", "Preparando", "Pronto", "Entregue", "Concluído"]
        cfg_pad = svc.get_os_config("padaria_acougue")
        assert "Em Preparo" in cfg_pad["statuses"]
        cfg_roupa = svc.get_os_config("loja_roupa")
        assert "Em Ajuste" in cfg_roupa["statuses"]
        cfg_art = svc.get_os_config("artesanato")
        assert "Acabamento" in cfg_art["statuses"]
        cfg_default = svc.get_os_config("oficina_mecanica")
        assert "Em Análise" in cfg_default["statuses"]

        # Bug de navegação pendente preservado conforme especificação
        assert svc.acao_do_botao("PROFISSIONAIS") == "clientes_manager"
        assert svc.acao_do_botao("MECÂNICOS") == "clientes_manager"

        with pytest.raises(ValueError):
            svc.acao_do_botao("BOTAO_DESCONHECIDO")


class TestPrecificacaoService:
    """Valida margem, markup e sugestão de preço em casos normais e de borda."""

    def test_calcular_margem_normais_e_borda(self):
        svc = PrecificacaoService()

        # Caso normal
        r = svc.calcular_margem(50.0, 80.0)
        assert r["lucro"] == pytest.approx(30.0)
        assert r["margem_pct"] == pytest.approx((30.0 / 80.0) * 100.0)
        assert r["markup_pct"] == pytest.approx((30.0 / 50.0) * 100.0)
        assert r["retorno_por_real"] == pytest.approx(1.6)

        # Custo zero -> ValueError
        with pytest.raises(ValueError, match="Custo deve ser > 0"):
            svc.calcular_margem(0.0, 50.0)

        # Preço zero -> margem_pct = 0.0, lucro negativo, markup = -100%
        r_zero = svc.calcular_margem(50.0, 0.0)
        assert r_zero["lucro"] == pytest.approx(-50.0)
        assert r_zero["margem_pct"] == 0.0
        assert r_zero["markup_pct"] == pytest.approx(-100.0)

        # Preço < custo (prejuízo)
        r_prej = svc.calcular_margem(100.0, 80.0)
        assert r_prej["lucro"] == pytest.approx(-20.0)
        assert r_prej["margem_pct"] == pytest.approx(-25.0)
        assert r_prej["markup_pct"] == pytest.approx(-20.0)

        # 3+ casas decimais (idêntico ao cálculo float original)
        custo_dec, preco_dec = 10.125, 15.375
        r_dec = svc.calcular_margem(custo_dec, preco_dec)
        assert r_dec["lucro"] == pytest.approx(preco_dec - custo_dec)
        assert r_dec["margem_pct"] == pytest.approx(((preco_dec - custo_dec) / preco_dec) * 100.0)
        assert r_dec["markup_pct"] == pytest.approx(((preco_dec - custo_dec) / custo_dec) * 100.0)

    def test_calcular_markup_e_sugerir_preco_identico_pricing_engine(self):
        svc = PrecificacaoService()
        mk = svc.calcular_markup(60.0, 40.0)
        assert mk["preco_venda"] == pytest.approx(100.0)
        assert mk["markup_pct"] == pytest.approx((40.0 / 60.0) * 100.0)

        with pytest.raises(ValueError):
            svc.calcular_markup(60.0, 100.0)

        # Comparação direta com ai_module.PricingEngine
        engine = ai_module.PricingEngine()
        esperado = engine.calcular_preco_etico(45.5, 2.0, 30.0, 0.35, 0.05, 1.0)
        atual = svc.sugerir_preco(45.5, 2.0, 30.0, 0.35, 0.05, 1.0)
        assert atual == esperado


class TestServicoService:
    """Valida OS, ocultação de custo interno na nota, WhatsApp sem envio real, comandas e divisão."""

    def test_registrar_os_avancar_status_nota_sem_custo_e_whatsapp(self, isolated_db, tmp_path):
        # Stub de invoice_generator para validar que custo interno nunca é enviado nem exibido
        stub_inv = MagicMock()

        def _fake_gerar_nota(descricao, valor, dados_cliente, id_servico=None, forma_pagamento="", observacao="", abrir_arquivo=False, config_nota=None):
            txt = f"NOTA OS #{id_servico}\nCliente: {dados_cliente['nome']}\nServiço: {descricao}\nTotal: R$ {valor:.2f}"
            wpp = f"Olá {dados_cliente['nome']}! OS #{id_servico}: {descricao} - R$ {valor:.2f}"
            return (str(tmp_path / f"os_{id_servico}.txt"), txt, wpp)

        stub_inv.gerar_nota_servico.side_effect = _fake_gerar_nota
        svc = ServicoService(isolated_db, invoice_gen=stub_inv)

        custo_interno_secreto = 77.89
        os_data = svc.registrar_os(
            cliente="Maria Silva",
            descricao="Troca de tela completa",
            valor=250.0,
            custo=custo_interno_secreto,
            forma_pagamento="PIX",
            observacao="Garantia 90 dias",
            status_os="Recebido",
            contato_cliente="11999998888",
            equipamento="iPhone 14",
        )
        sid = os_data["servico_id"]
        assert sid is not None
        assert os_data["lucro"] == pytest.approx(250.0 - custo_interno_secreto)

        # Avançar status na trilha de OS
        fluxo = NichoService().get_os_config("oficina_mecanica")["statuses"]
        novo_st = svc.avancar_status(sid, "Recebido", fluxo)
        assert novo_st == "Em Análise"

        df_serv = isolated_db.listar_servicos(30)
        row_st = df_serv.loc[df_serv["id"] == sid, "status_os"].iloc[0]
        assert row_st == "Em Análise"

        # Nota do cliente NUNCA pode incluir o custo interno
        nota = svc.gerar_nota_os(
            servico_id=sid,
            descricao=os_data["descricao"],
            valor=os_data["valor"],
            cliente=os_data["cliente"],
            contato=os_data["contato_cliente"],
            forma_pagamento=os_data["forma_pagamento"],
            observacao=os_data["observacao"],
        )
        assert "77.89" not in nota["text_content"]
        assert "77,89" not in nota["text_content"]
        assert "250.00" in nota["text_content"]

        # Mensagem WhatsApp gerada sem envio real e sem custo interno
        msg_wpp = svc.montar_mensagem_whatsapp_os(
            wpp_content=nota["wpp_content"],
            text_content=nota["text_content"],
            servico_id=sid,
            descricao=os_data["descricao"],
            valor=os_data["valor"],
            cliente=os_data["cliente"],
        )
        assert "Maria Silva" in msg_wpp
        assert "77.89" not in msg_wpp
        assert "77,89" not in msg_wpp

    def test_fechar_comanda_e_dividir_conta_exato(self, isolated_db):
        svc = ServicoService(isolated_db)
        ok_cx, _ = isolated_db.abrir_caixa(100.0, usuario_id=1)
        assert ok_cx is True

        isolated_db.adicionar_produto("Pizza Grande", 10, 100.0, categoria="Geral", preco_compra=40.0)
        prod_id = int(isolated_db.listar_produtos().iloc[0]["id"])
        comanda_id = isolated_db.abrir_comanda(cliente="Mesa 01")
        isolated_db.adicionar_item_comanda(comanda_id, prod_id, "Pizza Grande", 1, 100.0)

        # Divisão exata (100 / 4 = 25.00)
        div = svc.dividir_conta(comanda_id, 4)
        assert div["valor_por_pessoa"] == pytest.approx(25.0)
        assert round(div["valor_por_pessoa"] * div["pessoas"], 2) == pytest.approx(100.0)

        # Fechamento com taxa de serviço 10% (R$ 10,00) e desconto R$ 5,00 -> total R$ 105,00
        res = svc.fechar_comanda(comanda_id, forma_pagamento="PIX", aplicar_taxa_10=True, desconto=5.0, observacao="OK")
        assert res["taxa_servico"] == pytest.approx(10.0)
        assert res["total_final"] == pytest.approx(105.0)

    def test_dividir_conta_centavos_100_por_3(self, isolated_db):
        svc = ServicoService(isolated_db)
        isolated_db.adicionar_produto("Combo", 10, 100.0, categoria="Geral", preco_compra=40.0)
        prod_id = int(isolated_db.listar_produtos().iloc[0]["id"])
        comanda_id = isolated_db.abrir_comanda(cliente="Mesa 02")
        isolated_db.adicionar_item_comanda(comanda_id, prod_id, "Combo", 1, 100.0)

        div = svc.dividir_conta(comanda_id, 3)
        soma_centavos = sum(round(p * 100) for p in div["partes"])
        total_centavos = round(div["total"] * 100)
        assert soma_centavos == total_centavos


class TestContratoASTLote2:
    """Garante via AST que nenhum dos 3 novos services importa tkinter/customtkinter."""

    @pytest.mark.parametrize(
        "arquivo",
        ["nicho_service.py", "precificacao_service.py", "servico_service.py"],
    )
    def test_service_sem_tkinter(self, arquivo):
        src_path = pathlib.Path(__file__).resolve().parent.parent / "services" / arquivo
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        proibidos = {"tkinter", "customtkinter", "tkcalendar"}
        encontrados = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in proibidos:
                        encontrados.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in proibidos:
                    encontrados.append(node.module)

        assert encontrados == [], f"Imports de UI proibidos em {arquivo}: {encontrados}"
