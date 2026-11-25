# -*- mode: python ; coding: utf-8 -*-
# EstokeAoCubo.spec — Spec único exclusivo --onedir para Full e Trial
# Variáveis de ambiente suportadas:
#   ESTOKE_EDITION=full|trial   (padrão: full)
#   ESTOKE_EXE_NAME=<nome>      (opcional; sobrescreve nome do executável/pasta)

from PyInstaller.utils.hooks import collect_all, collect_data_files
import glob
import os

_edition = os.environ.get("ESTOKE_EDITION", "full").strip().lower()
if _edition not in ("full", "trial"):
    raise ValueError(f"ESTOKE_EDITION inválida: {_edition!r}. Use 'full' ou 'trial'.")

_default_exe_name = "EstokeAoCubo_Trial" if _edition == "trial" else "EstokeAoCubo"
_exe_name = os.environ.get("ESTOKE_EXE_NAME", _default_exe_name).strip() or _default_exe_name

# Resolução automática dos DLLs nativos do pyzbar (libiconv, libzbar, etc.)
try:
    import pyzbar
    _pyzbar_dir = os.path.dirname(pyzbar.__file__)
    _pyzbar_dlls = glob.glob(os.path.join(_pyzbar_dir, "*.dll"))
    _pyzbar_binaries = [(dll, ".") for dll in _pyzbar_dlls]
    _pyzbar_binaries += [(dll, "pyzbar") for dll in _pyzbar_dlls]
except Exception:
    _pyzbar_binaries = []

datas = [
    ("icon", "icon"),
]
for _doc_file in ("GUIA_DO_USUARIO.txt", "PLANILHA_PADRAO_ITENS.csv", "TERMOS_DE_USO.txt", ".env.exemplo", "README.md"):
    _doc_path = os.path.join("dist_package", _doc_file)
    if os.path.exists(_doc_path):
        datas.append((_doc_path, "."))

# Templates/páginas HTML servidas pela API Flask local
for _web_file in ("index.html", "live_scanner.html"):
    for _cand in (_web_file, os.path.join("..", _web_file)):
        if os.path.exists(_cand):
            datas.append((_cand, "."))
            break

# Bundle raiz SSL CA (certifi) para HTTPS confiável em máquinas zeradas
try:
    datas += collect_data_files("certifi")
except Exception:
    pass

binaries = list(_pyzbar_binaries)
hiddenimports = [
    # === Módulos internos do projeto ===
    "paths",
    "database",
    "migrations",
    "crash_handler",
    "config",
    "printer",
    "ai_module",
    "invoice_generator",
    "notifications",
    "label_generator",
    "barcode_scanner",
    "api_server",
    # Subpacote services
    "services",
    "services.venda_service",
    "services.caixa_service",
    "services.estoque_service",
    "services.dreams_service",
    "services.security",
    "services.relatorio_caixa",
    "services.mercadopago_service",
    "services.outbox_service",
    "services.scheduler",
    "services.updater",
    "services.compras_service",
    "services.fiscal_service",
    "services.producao_service",
    "services.troca_service",
    # Subpacote views
    "views",
    "views.base",
    "views.pdv_view",
    "views.supervisor_dialog",
    "views.vendas_suspensas_view",
    "views.inventario_view",
    "views.nfe_preview_view",
    "views.compras_view",
    "views.financeiro_view",
    "views.admin_view",
    # Subpacote utils
    "utils",
    "utils.formatters",
    # === Dependências externas ===
    "certifi",
    "google.genai",
    "dotenv",
    "escpos",
    "serial",
    "win32print",
    "win32ui",
    "pdfplumber",
    "flask",
    "cv2",
    "pyzbar",
    "barcode",
    "qrcode",
    "requests",
    "pandas",
    "numpy",
    "cryptography",
    "PIL",
    "PIL._tkinter_finder",
    "PIL._webp",
    "tkcalendar",
    "babel",
    "babel.numbers",
    "babel.dates",
    "reportlab",
    "matplotlib",
]

# Coletar dados/DLLs completos de pacotes com templates, temas, fontes ou binários
_collect_packages = [
    "customtkinter",
    "google.genai",
    "escpos",
    "pdfplumber",
    "pyzbar",
    "qrcode",
    "barcode",
    "dotenv",
    "requests",
    "tkcalendar",
    "reportlab",
    "matplotlib",
    "cryptography",
    "flask",
    "jinja2",
]

# Incluir apenas locales essenciais do babel (evita 700+ arquivos de locale desnecessários)
try:
    import babel
    _babel_dir = os.path.dirname(babel.__file__)
    _global_dat = os.path.join(_babel_dir, "global.dat")
    if os.path.exists(_global_dat):
        datas.append((_global_dat, "babel"))
    for _loc in ("pt_BR.dat", "pt.dat", "en_US.dat", "en.dat", "root.dat"):
        _loc_path = os.path.join(_babel_dir, "locale-data", _loc)
        if os.path.exists(_loc_path):
            datas.append((_loc_path, os.path.join("babel", "locale-data")))
except Exception:
    pass

for _pkg in _collect_packages:
    try:
        _ret = collect_all(_pkg)
        datas += _ret[0]
        binaries += _ret[1]
        hiddenimports += _ret[2]
    except Exception:
        pass

hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "sklearn",
        "scipy",
        "numba",
        "llvmlite",
        "av",
        # NÃO excluir "pypdfium2_raw": pdfplumber.display -> pypdfium2 -> pypdfium2_raw (DLL nativa)
        "torch",
        "tensorflow",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        # NÃO excluir "unittest": matplotlib -> pyparsing.testing importa unittest em tempo de import
        "test",
        "tkinter.test",
        "sqlalchemy",
        "google.generativeai",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon\\icon-cubo.ico"],
    version="version_info.txt" if os.path.exists("version_info.txt") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=_exe_name,
)
