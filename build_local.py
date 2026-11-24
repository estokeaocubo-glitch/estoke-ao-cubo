# -*- coding: utf-8 -*-
"""
Pipeline Único de Build & Release Windows — Estoke Ao Cubo
Fluxo automatizado (--onedir exclusivo):
1. Limpa artefatos anteriores (build/, dist/)
2. Valida VC++ Redistributable x64 (>= 14.44) em build_assets/vc_redist.x64.exe
3. Executa suíte pytest para garantir zero regressão antes de empacotar
4. Compila com PyInstaller usando --workpath em %TEMP% (evita lock do Google Drive)
5. Audita presença de DLLs e assets críticos em dist/
6. Executa Smoke Test 100% isolado fora do repositório (--self-test com ESTOKE_DATA_DIR virgem)
7. Compila instalador único com Inno Setup 6 em release/
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_VERSION = "4.6.0"

ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def _get_file_version_tuple(filepath: Path) -> tuple[int, int, int, int] | None:
    """Lê VS_FIXEDFILEINFO.dwFileVersion via API Win32."""
    if os.name != "nt" or not filepath.exists():
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(filepath), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(filepath), 0, size, buf):
            return None
        lplp = ctypes.c_void_p()
        u_len = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(lplp), ctypes.byref(u_len)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
            ]

        info = ctypes.cast(lplp, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        return ((ms >> 16) & 0xFFFF, ms & 0xFFFF, (ls >> 16) & 0xFFFF, ls & 0xFFFF)
    except Exception:
        return None


def verify_vc_redist(project_dir: Path) -> Path:
    """Garante que build_assets/vc_redist.x64.exe existe e possui FileVersion >= 14.44."""
    vc_path = project_dir / "build_assets" / "vc_redist.x64.exe"
    if not vc_path.exists():
        raise FileNotFoundError(
            f"vc_redist.x64.exe não encontrado em {vc_path}. "
            "Baixe de https://aka.ms/vs/17/release/vc_redist.x64.exe e salve em build_assets/."
        )
    ver = _get_file_version_tuple(vc_path)
    if ver is None:
        raise RuntimeError(f"Não foi possível ler a versão de arquivo de {vc_path}.")
    ver_str = ".".join(str(x) for x in ver)
    if not (ver[0] > 14 or (ver[0] == 14 and ver[1] >= 44)):
        raise RuntimeError(
            f"vc_redist.x64.exe desatualizado (versão {ver_str}; exigido >= 14.44 para MSVC v1944 / Python 3.14). "
            "Baixe a versão oficial em https://aka.ms/vs/17/release/vc_redist.x64.exe."
        )
    print(f"[+] VC++ Redistributable validado: {vc_path} (v{ver_str})")
    return vc_path


def find_iscc() -> Path:
    for cand in ISCC_CANDIDATES:
        if cand and os.path.exists(cand):
            return Path(cand)
    found = shutil.which("ISCC.exe")
    if found:
        return Path(found)
    raise FileNotFoundError("ISCC.exe (Inno Setup 6) não encontrado no sistema.")


def _stop_running_dist_executables() -> None:
    """Encerra instâncias em execução do EstokeAoCubo para evitar locks ao atualizar dist/."""
    if os.name != "nt":
        return
    import time

    killed_any = False
    for proc_name in ("EstokeAoCubo.exe", "EstokeAoCubo_Trial.exe"):
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                killed_any = True
        except Exception:
            pass
    if killed_any:
        time.sleep(0.5)


def clean_build_and_dist(project_dir: Path, work_dir: Path, dist_tmp_dir: Path) -> None:
    print("\n[*] Limpando build/ e diretórios temporários (preservando dist/ até compilação concluir)...")
    for d in (project_dir / "build", work_dir, dist_tmp_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    (project_dir / "release").mkdir(parents=True, exist_ok=True)


def run_tests(project_dir: Path) -> None:
    print("\n" + "=" * 64)
    print(" 1. Executando suíte pytest pré-empacotamento...")
    print("=" * 64)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-k",
        "not test_01_carregamento_credenciais",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    subprocess.run(cmd, cwd=project_dir, check=True)
    print("[+] Todos os testes selecionados passaram!")


def run_pyinstaller(project_dir: Path, work_dir: Path, dist_tmp_dir: Path, edition: str) -> Path:
    print("\n" + "=" * 64)
    print(f" 2. Compilando pacote --onedir com PyInstaller ({edition.upper()})...")
    print("=" * 64)
    exe_base = "EstokeAoCubo_Trial" if edition == "trial" else "EstokeAoCubo"
    env = os.environ.copy()
    env["ESTOKE_EDITION"] = edition
    env["ESTOKE_EXE_NAME"] = exe_base

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath",
        str(work_dir),
        "--distpath",
        str(dist_tmp_dir),
        "EstokeAoCubo.spec",
    ]
    print(f"[*] workpath=%TEMP% ({work_dir}) -> evitando lock do Google Drive...")
    log_path = Path(tempfile.gettempdir()) / "EstokeAoCubo_pyinstaller.log"
    with open(log_path, "w", encoding="utf-8") as lf:
        subprocess.run(cmd, cwd=project_dir, env=env, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, check=True)

    src_bundle = dist_tmp_dir / exe_base
    if not src_bundle.exists():
        raise FileNotFoundError(f"Bundle --onedir não gerado em {src_bundle}")

    dest_bundle = project_dir / "dist" / exe_base
    (project_dir / "dist").mkdir(parents=True, exist_ok=True)
    _stop_running_dist_executables()
    dest_bundle.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(
        ["robocopy", str(src_bundle), str(dest_bundle), "/MIR", "/R:3", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"],
        capture_output=True,
        text=True,
    )
    if rc.returncode >= 8:
        print(f"[!] Aviso: robocopy retornou código {rc.returncode} ao sincronizar {dest_bundle}")
    else:
        print(f"[+] Bundle --onedir sincronizado in-place via robocopy para: {dest_bundle}")
    print(f"[+] Bundle local de build em %TEMP%: {src_bundle}")
    return src_bundle


def audit_dist_bundle(bundle_dir: Path, exe_name: str) -> list[str]:
    """Audita DLLs nativas, bibliotecas compiladas e assets estáticos críticos dentro de dist/."""
    print("\n" + "=" * 64)
    print(f" 3. Auditando binários, DLLs e assets em {bundle_dir}...")
    print("=" * 64)
    internal = bundle_dir / "_internal"
    checks: list[tuple[str, Path]] = [
        ("Executável principal", bundle_dir / exe_name),
        ("Runtime Python DLL", internal / "python314.dll"),
        ("VC++ Runtime (vcruntime140.dll)", internal / "vcruntime140.dll"),
        ("SQLite nativa (sqlite3.dll / _sqlite3.pyd)", internal / "_sqlite3.pyd"),
        ("Certificados SSL (certifi/cacert.pem)", internal / "certifi" / "cacert.pem"),
        ("Ícone da aplicação", internal / "icon" / "icon-cubo.ico"),
        ("Temas CustomTkinter", internal / "customtkinter"),
        ("Criptografia nativa (cryptography)", internal / "cryptography"),
        ("Processamento de imagem (PIL/Pillow)", internal / "PIL"),
        ("NumPy compilado", internal / "numpy"),
        ("Gerador PDF ReportLab", internal / "reportlab"),
        ("Documentação Termos de Uso", internal / "TERMOS_DE_USO.txt"),
    ]
    encontrados = []
    faltantes = []
    for label, path in checks:
        if path.exists():
            print(f"  [OK] {label}: {path.relative_to(bundle_dir)}")
            encontrados.append(label)
        else:
            print(f"  [FALHA] {label}: AUSENTE ({path})")
            faltantes.append(label)

    if faltantes:
        raise RuntimeError(f"Auditoria de build falhou. Itens ausentes: {', '.join(faltantes)}")
    return encontrados


def run_isolated_smoke_test(bundle_dir: Path, exe_name: str) -> dict:
    """
    Copia o diretório gerado para uma pasta temporária 100% fora do repositório
    (sem código-fonte ou .venv) e executa --self-test com ESTOKE_DATA_DIR virgem.
    """
    print("\n" + "=" * 64)
    print(" 4. Executando Smoke Test isolado fora do repositório (--self-test)...")
    print("=" * 64)
    smoke_root = Path(tempfile.mkdtemp(prefix="EstokeSmokeTest_"))
    try:
        app_dir = smoke_root / "Estoke Ao Cubo"
        data_dir = smoke_root / "VirginLocalAppData"
        shutil.copytree(bundle_dir, app_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        exe_path = app_dir / exe_name
        env = os.environ.copy()
        env["ESTOKE_DATA_DIR"] = str(data_dir)

        proc = subprocess.run(
            [str(exe_path), "--self-test"],
            cwd=str(smoke_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        report_file = data_dir / "selftest_report.json"
        if not report_file.exists():
            raise RuntimeError(
                f"Smoke test falhou (exit={proc.returncode}) sem gravar selftest_report.json. "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )
        report = json.loads(report_file.read_text(encoding="utf-8"))
        if proc.returncode != 0 or report.get("status") != "ok":
            raise RuntimeError(f"Smoke test reprovado (exit={proc.returncode}): {report.get('error')}")

        print(f"  [OK] Executável isolado: {exe_path}")
        print(f"  [OK] Dados gravados exclusivamente em: {data_dir}")
        print(f"  [OK] Schema SQLite v{report['checks']['database_migrations_backup']['schema_version']}, .enc={report['checks']['database_migrations_backup']['enc_exists']}")
        print(f"  [OK] Imports validados: {report['checks']['imports']['count']} módulos críticos")
        print(f"  [OK] PDF ReportLab gerado: {report['checks']['reportlab_pdf']['size_bytes']} bytes")
        if "matplotlib_chart" in report.get("checks", {}):
            print(f"  [OK] Gráfico matplotlib (dashboard) renderizado: {report['checks']['matplotlib_chart']['size_bytes']} bytes")
        return report
    finally:
        shutil.rmtree(smoke_root, ignore_errors=True)


def run_inno_setup(project_dir: Path, edition: str, version: str, source_bundle_dir: Path | None = None) -> Path:
    print("\n" + "=" * 64)
    print(f" 5. Gerando instalador Inno Setup 6 ({edition.upper()} v{version})...")
    print("=" * 64)
    iscc = find_iscc()
    cmd = [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DMyAppEdition={edition}",
    ]
    if source_bundle_dir is not None:
        cmd.append(f"/DMyDistSource={source_bundle_dir}")
    cmd.append("EstokeSetup.iss")
    log_path = Path(tempfile.gettempdir()) / "EstokeAoCubo_innosetup.log"
    with open(log_path, "w", encoding="utf-8") as lf:
        subprocess.run(cmd, cwd=project_dir, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, check=True)

    setup_name = (
        f"EstokeAoCubo_Trial_Setup_v{version}.exe"
        if edition == "trial"
        else f"EstokeAoCubo_Setup_v{version}.exe"
    )
    installer = project_dir / "release" / setup_name
    if not installer.exists():
        raise FileNotFoundError(f"Instalador esperado não foi gerado em {installer}")

    size_mb = installer.stat().st_size / (1024 * 1024)
    print(f"[+] Instalador final gerado: {installer} ({size_mb:.2f} MB)")
    return installer


def _load_or_create_release_keypair() -> tuple[object, str]:
    """Carrega par Ed25519 de ESTOKE_RELEASE_PRIVKEY_B64 ou de %LOCALAPPDATA%\\EstokeAoCubo\\release_keys."""
    import base64
    from cryptography.hazmat.primitives import serialization
    import services.updater as updater

    env_priv = os.environ.get("ESTOKE_RELEASE_PRIVKEY_B64", "").strip()
    if env_priv:
        priv = updater.load_ed25519_private_key(env_priv)
        pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return priv, base64.b64encode(pub_raw).decode("ascii")

    key_dir = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "EstokeAoCubo" / "release_keys"
    key_file = key_dir / "ed25519_release_key.json"
    if key_file.is_file():
        data = json.loads(key_file.read_text(encoding="utf-8"))
        priv = updater.load_ed25519_private_key(data["private_key_b64"])
        pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return priv, base64.b64encode(pub_raw).decode("ascii")

    priv, priv_b64, pub_b64 = updater.generate_ed25519_keypair()
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text(
        json.dumps({"private_key_b64": priv_b64, "public_key_b64": pub_b64}, indent=2),
        encoding="utf-8",
    )
    print(f"[+] Novo par de chaves Ed25519 de release salvo fora do repositório: {key_file}")
    return priv, pub_b64


def generate_release_manifest(project_dir: Path, version: str) -> Path:
    """Gera e valida release/version.json assinado com Ed25519 para auto-update."""
    print("\n" + "=" * 64)
    print(f" 6. Gerando manifesto assinado Ed25519 (version.json v{version})...")
    print("=" * 64)
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    import services.updater as updater

    release_dir = project_dir / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    full_path = release_dir / f"EstokeAoCubo_Setup_v{version}.exe"
    trial_path = release_dir / f"EstokeAoCubo_Trial_Setup_v{version}.exe"

    priv, pub_b64 = _load_or_create_release_keypair()
    base_url = os.environ.get("ESTOKE_RELEASE_BASE_URL", "https://updates.estokeaocubo.com.br/releases").strip()
    manifest = updater.build_signed_release_manifest(
        version=version,
        private_key=priv,
        full_installer=full_path if full_path.exists() else None,
        trial_installer=trial_path if trial_path.exists() else None,
        base_download_url=base_url,
    )

    ok, msg = updater.verify_manifest_signature(manifest, public_key=pub_b64)
    if not ok:
        raise RuntimeError(f"Assinatura do manifesto principal inválida: {msg}")
    for ed_name, ed_payload in manifest.get("editions", {}).items():
        ok_ed, msg_ed = updater.verify_manifest_signature(ed_payload, public_key=pub_b64)
        if not ok_ed:
            raise RuntimeError(f"Assinatura da edição {ed_name} inválida: {msg_ed}")

    manifest_path = release_dir / "version.json"
    pubkey_path = release_dir / "ed25519_public_key.txt"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pubkey_path.write_text(pub_b64 + "\n", encoding="utf-8")

    print(f"  [+] Manifesto assinado gerado: {manifest_path}")
    print(f"  [+] Chave pública Ed25519 salva em: {pubkey_path}")
    print(f"  [+] Edições incluídas: {', '.join(manifest.get('editions', {}).keys())}")
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline único de build Windows Estoke Ao Cubo (--onedir + Inno Setup).")
    parser.add_argument("--edition", choices=["full", "trial"], default="full", help="Edição do build (full ou trial)")
    parser.add_argument("--version", default=APP_VERSION, help="Versão semântica do instalador")
    parser.add_argument("--skip-tests", action="store_true", help="Pula execução de pytest pré-build")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    temp_root = Path(tempfile.gettempdir())
    work_dir = temp_root / "EstokeAoCubo_build_work"
    dist_tmp_dir = temp_root / "EstokeAoCubo_dist_tmp"
    exe_name = "EstokeAoCubo_Trial.exe" if args.edition == "trial" else "EstokeAoCubo.exe"

    try:
        verify_vc_redist(project_dir)
        if not args.skip_tests:
            run_tests(project_dir)
        clean_build_and_dist(project_dir, work_dir, dist_tmp_dir)
        bundle_dir = run_pyinstaller(project_dir, work_dir, dist_tmp_dir, args.edition)
        audit_dist_bundle(bundle_dir, exe_name)
        run_isolated_smoke_test(bundle_dir, exe_name)
        installer = run_inno_setup(project_dir, args.edition, args.version, source_bundle_dir=bundle_dir)
        manifest_path = generate_release_manifest(project_dir, args.version)

        print("\n" + "=" * 64)
        print(f" RELEASE {args.edition.upper()} v{args.version} CONCLUÍDA COM SUCESSO!")
        print(f" Instalador: {installer}")
        print(f" Manifesto:  {manifest_path}")
        print("=" * 64)
    finally:
        for d in (work_dir, dist_tmp_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
