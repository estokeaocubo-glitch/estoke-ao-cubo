"""
Testes da camada de serviços do Lote 5 (F1b):
- ImpressaoService: gaveta com regra de supervisor + auditoria, reimpressão, listar_impressoras
  (win32print mockado), testar_impressora, diagnosticar e gerar_preview_cupom;
- HardwareService: balança serial e webcam com mock e timeout;
- PIX de fiado estático (RelatorioService): campo de valor (ID 54) e CRC-16 CCITT válido;
- Contrato AST + lazy imports: zero tkinter/customtkinter nos 2 services e import de
  services.hardware_service / services.impressao_service não carrega cv2/pyzbar/serial/win32print.
"""
from __future__ import annotations

import ast
import gc
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database
from services.auth_service import AuthService
from services.hardware_service import (
    HardwareIndisponivelError,
    HardwareService,
    HardwareTimeoutError,
)
from services.impressao_service import ImpressaoService
from services.relatorio_service import RelatorioService


@pytest.fixture
def isolated_db():
    tmpdir = tempfile.TemporaryDirectory()
    orig_path = database.get_app_data_path
    database.get_app_data_path = lambda *parts: (
        os.path.join(tmpdir.name, *parts) if parts else tmpdir.name
    )
    db = database.Database(db_name="test_imp_hw.db")
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


def _crc16_ccitt_false(payload: str) -> str:
    """Calcula CRC-16/CCITT-FALSE (polinômio 0x1021, init 0xFFFF) padrão EMV BR Code."""
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


# ==============================================================================
# 1. ImpressaoService: Gaveta (autorização + auditoria), listagem, teste e preview
# ==============================================================================


class TestImpressaoService:
    def test_gaveta_sem_autorizacao_para_caixa_ou_vendedor_levanta_permission_error(self, isolated_db):
        isolated_db.adicionar_usuario("Operador Caixa", "cx_gav", "Caixa@12345", perfil="caixa", loja_id=1)
        op = isolated_db.autenticar_usuario("cx_gav", "Caixa@12345")

        chamadas_gaveta = []

        class FakePrinter:
            def abrir_gaveta(self):
                chamadas_gaveta.append("pulso")

        svc = ImpressaoService(isolated_db, printer_factory=FakePrinter, operador=op)

        with pytest.raises(PermissionError):
            svc.abrir_gaveta(autorizacao=None, caixa_id=1)

        with pytest.raises(PermissionError):
            svc.abrir_gaveta(autorizacao={"approved": False, "supervisor_id": 1}, caixa_id=1)

        with pytest.raises(PermissionError):
            svc.abrir_gaveta(
                autorizacao={
                    "approved": True,
                    "supervisor_id": op["id"],
                    "supervisor_nome": op["nome"],
                    "supervisor_perfil": "vendedor",
                },
                caixa_id=1,
            )

        assert chamadas_gaveta == []

    def test_gaveta_com_autorizacao_gerente_e_isencao_gerente_logado_registra_auditoria(self, isolated_db):
        isolated_db.adicionar_usuario("Operador Caixa", "cx_gav2", "Caixa@12345", perfil="caixa", loja_id=1)
        isolated_db.adicionar_usuario("Carla Gerente", "ger_gav", "Gerente@12345", perfil="gerente", loja_id=1)
        op_cx = isolated_db.autenticar_usuario("cx_gav2", "Caixa@12345")
        op_ger = isolated_db.autenticar_usuario("ger_gav", "Gerente@12345")

        chamadas_gaveta = []

        class FakePrinter:
            def abrir_gaveta(self):
                chamadas_gaveta.append("pulso")

        # 1. Caixa com autorização real de gerente
        auth = AuthService(isolated_db).autorizar_supervisor(
            "ger_gav", "Gerente@12345", acao="Abertura manual da gaveta"
        )
        svc_cx = ImpressaoService(isolated_db, printer_factory=FakePrinter, operador=op_cx)
        res = svc_cx.abrir_gaveta(autorizacao=auth, caixa_id=7)
        assert res["enviado"] is True
        assert res["supervisor_nome"] == "Carla Gerente"
        assert len(chamadas_gaveta) == 1

        # 2. Gerente logado: isenção automática
        svc_ger = ImpressaoService(isolated_db, printer_factory=FakePrinter, operador=op_ger)
        res2 = svc_ger.abrir_gaveta(autorizacao=None, caixa_id=7)
        assert res2["enviado"] is True
        assert len(chamadas_gaveta) == 2

        # Conferir auditoria SHA-256
        aud = RelatorioService(isolated_db).verificar_auditoria()
        assert aud["integro"] is True
        hist = RelatorioService(isolated_db).listar_historico()
        logs_gav = [h for h in hist if h["acao"] == "Abrir Gaveta"]
        assert len(logs_gav) == 2
        assert all("Carla Gerente" in h["detalhe"] for h in logs_gav)

    def test_listar_impressoras_com_win32print_mockado(self, monkeypatch):
        fake_win32 = types.ModuleType("win32print")
        fake_win32.PRINTER_ENUM_LOCAL = 2
        fake_win32.PRINTER_ENUM_CONNECTIONS = 4
        fake_win32.EnumPrinters = lambda flags: [
            (0, "desc1", "Epson TM-T20X", "cmt"),
            (0, "desc2", "Elgin i9 USB", "cmt"),
        ]
        monkeypatch.setitem(sys.modules, "win32print", fake_win32)

        svc = ImpressaoService()
        assert svc.listar_impressoras() == ["Epson TM-T20X", "Elgin i9 USB"]

    def test_reimprimir_testar_diagnosticar_e_preview_cupom(self, isolated_db, tmp_path):
        impressos = []

        class FakePrinter:
            def print_text(self, text, logo_path=None, venda_id=None):
                impressos.append({"text": text, "venda_id": venda_id})

        svc = ImpressaoService(isolated_db, printer_factory=FakePrinter)

        # Testar impressora
        res_t = svc.testar_impressora()
        assert res_t["enviado"] is True
        assert "== TESTE DE IMPRESSÃO ==" in impressos[-1]["text"]

        # Reimprimir da sessão
        res_r = svc.reimprimir_ultimo_cupom(ultimo_cupom={"text": "CUPOM #42", "venda_id": 42})
        assert res_r["origem"] == "sessao"
        assert impressos[-1]["text"] == "CUPOM #42"

        # Reimprimir sem cupom na sessão e cache vazio -> LookupError
        class EmptyAuditor:
            def recuperar_ultimo_recibo(self):
                return ""

        with pytest.raises(LookupError):
            svc.reimprimir_ultimo_cupom(ultimo_cupom=None, auditor=EmptyAuditor())

        # Diagnosticar
        diag = svc.diagnosticar()
        assert diag["ok"] is True
        assert diag["driver"] == "FakePrinter"

        # Preview PNG
        png_dest = str(tmp_path / "preview_cupom.png")
        prev = svc.gerar_preview_cupom({"empresa_nome": "Loja Preview"}, destino_png=png_dest)
        assert os.path.exists(prev["png_path"])
        assert os.path.getsize(prev["png_path"]) > 0


# ==============================================================================
# 2. HardwareService: Balança serial e Webcam com mocks e timeout
# ==============================================================================


class TestHardwareService:
    def test_ler_peso_balanca_sucesso_e_timeout(self, monkeypatch):
        class FakePortInfo:
            def __init__(self, device):
                self.device = device

        class FakeSerialOK:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def write(self, data):
                assert data == b"\x05"

            def read(self, n):
                return b"\x0201250\x03"

        fake_serial = types.ModuleType("serial")
        fake_serial.Serial = FakeSerialOK
        fake_tools = types.ModuleType("serial.tools")
        fake_list_ports = types.ModuleType("serial.tools.list_ports")
        fake_list_ports.comports = lambda: [FakePortInfo("COM4")]
        fake_tools.list_ports = fake_list_ports
        fake_serial.tools = fake_tools

        monkeypatch.setitem(sys.modules, "serial", fake_serial)
        monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)
        monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake_list_ports)

        hw = HardwareService()
        assert hw.ler_peso_balanca(porta="COM4", timeout=0.5) == pytest.approx(1.250)
        assert hw.ler_peso_balanca(porta=None, timeout=0.5) == pytest.approx(1.250)

        # Timeout (balança não responde dados válidos)
        class FakeSerialTimeout(FakeSerialOK):
            def read(self, n):
                time.sleep(0.06)
                return b""

        fake_serial.Serial = FakeSerialTimeout
        with pytest.raises(HardwareTimeoutError):
            hw.ler_peso_balanca(porta="COM4", timeout=0.05)

        # Sem portas disponíveis -> HardwareIndisponivelError
        fake_list_ports.comports = lambda: []
        with pytest.raises(HardwareIndisponivelError):
            hw.ler_peso_balanca(porta=None, timeout=0.2)

    def test_capturar_codigo_webcam_sucesso_e_timeout(self, monkeypatch):
        class FakeDecoded:
            def __init__(self, val):
                self.data = val

        class FakeCapture:
            def __init__(self, opened=True):
                self._opened = opened
                self.released = False

            def isOpened(self):
                return self._opened

            def read(self):
                return True, object()

            def release(self):
                self.released = True

        cap_ok = FakeCapture(opened=True)
        fake_cv2 = types.ModuleType("cv2")
        fake_cv2.CAP_DSHOW = 700
        fake_cv2.VideoCapture = lambda *a, **kw: cap_ok

        fake_pyzbar_pkg = types.ModuleType("pyzbar")
        fake_pyzbar_mod = types.ModuleType("pyzbar.pyzbar")
        fake_pyzbar_mod.decode = lambda frame: [FakeDecoded(b"7891234567895")]
        fake_pyzbar_pkg.pyzbar = fake_pyzbar_mod

        monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
        monkeypatch.setitem(sys.modules, "pyzbar", fake_pyzbar_pkg)
        monkeypatch.setitem(sys.modules, "pyzbar.pyzbar", fake_pyzbar_mod)

        hw = HardwareService()
        codigo = hw.capturar_codigo_webcam(timeout=1.0)
        assert codigo == "7891234567895"
        assert cap_ok.released is True

        # Timeout quando nenhum código é decodificado
        cap_to = FakeCapture(opened=True)
        fake_cv2.VideoCapture = lambda *a, **kw: cap_to
        fake_pyzbar_mod.decode = lambda frame: []

        with pytest.raises(HardwareTimeoutError):
            hw.capturar_codigo_webcam(timeout=0.08)
        assert cap_to.released is True


# ==============================================================================
# 3. PIX de fiado estático com valor (ID 54) e CRC-16 CCITT válido (bug #5 corrigido)
# ==============================================================================


class TestPixFiadoEstaticoCorrigido:
    def test_pix_fiado_estatico_contem_campo_valor_id54_e_crc_valido(self, isolated_db, monkeypatch):
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("Sem rede!")),
        )
        isolated_db.salvar_config_nota({"empresa_nome": "Loja Cubo", "chave_pix": "pix@lojacubo.com.br"})
        isolated_db.adicionar_cliente("Lucas Pereira", "11988881111", "")
        cli_id = int(isolated_db.listar_clientes().iloc[0]["id"])
        isolated_db.registrar_fiado(cli_id, 87.50, "Fiado #1", "2026-12-20")
        conta_id = int(isolated_db.listar_fiados(status="Pendente").iloc[0]["id"])

        rel = RelatorioService(isolated_db)
        res = rel.gerar_cobranca_fiado_pix(
            conta_id=conta_id,
            usar_mercadopago=False,
            enfileirar_whatsapp=False,
        )

        assert res["origem_pix"] == "chave_estatica"
        payload = res["payload_pix"]
        assert payload.startswith("000201")
        # Campo EMV ID 54 (Transaction Amount): 54 + len("87.50")=05 + "87.50"
        assert "540587.50" in payload
        assert "PIX Copia e Cola" in res["mensagem"]
        assert payload in res["mensagem"]

        # Verificar CRC-16/CCITT-FALSE no final (6304XXXX)
        m_crc = re.search(r"6304([0-9A-F]{4})$", payload)
        assert m_crc is not None, f"CRC EMV ausente no payload: {payload}"
        sem_crc = payload[:-4]
        assert m_crc.group(1) == _crc16_ccitt_false(sem_crc)


# ==============================================================================
# 4. Contrato AST + Imports pesados preguiçosos (sys.modules em processo limpo)
# ==============================================================================


class TestContratoImpressaoHardware:
    FORBIDDEN_UI = {"tkinter", "customtkinter"}

    @pytest.mark.parametrize("filename", ("impressao_service.py", "hardware_service.py"))
    def test_sem_imports_de_tkinter_ou_customtkinter(self, filename):
        filepath = Path(__file__).resolve().parent.parent / "services" / filename
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
        violacoes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in self.FORBIDDEN_UI:
                        violacoes.append(f"L{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in self.FORBIDDEN_UI:
                    violacoes.append(f"L{node.lineno}: from {node.module} import ...")
        assert violacoes == [], f"Imports de UI encontrados em {filename}: {violacoes}"

    def test_importar_services_nao_carrega_cv2_pyzbar_serial_nem_win32print(self):
        project_root = str(Path(__file__).resolve().parent.parent)
        code = (
            "import sys\n"
            "import services.hardware_service\n"
            "import services.impressao_service\n"
            "pesados = [m for m in ('cv2', 'pyzbar', 'serial', 'win32print') if m in sys.modules]\n"
            "assert pesados == [], f'Modulos pesados carregados no import: {pesados}'\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, f"Falha no teste de lazy import:\n{proc.stderr}\n{proc.stdout}"
