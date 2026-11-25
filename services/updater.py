# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Auto-Update Assinado (Ed25519) e Validação Unificada de Licença/Trial.
Compatível com manifestos JSON hospedados em Cloudflare R2 ou GitHub Releases (HTTPS).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import config as app_config

logger = logging.getLogger("EstokeAoCubo.updater")

APP_VERSION = "4.6.0"
DEFAULT_UPDATE_MANIFEST_URL = "https://updates.estokeaocubo.com.br/version.json"
# Chave pública que valida o version.json do auto-update (par gerado no hotfix v4.6.1;
# a privada fica fora do repositório).
DEFAULT_ED25519_PUBLIC_KEY_B64 = "D1csAKUbmtRR6bIvsVqW9wu0cJyPVduZJRN/vrOHkUo="
# Chave pública que valida tokens de licença: continua a mesma da v4.6.0 para que
# licenças já emitidas não caiam para trial ao trocar a chave do auto-update.
DEFAULT_LICENSE_PUBLIC_KEY_B64 = "fO2QB+dHmaf/kA4UAtfyRSfIK+wUI+tb0DZxX9nlowc="
INNO_SILENT_FLAGS = ("/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/SUPPRESSMSGBOXES")


def parse_semver(version: str) -> tuple[int, ...]:
    """Converte string semver ('v4.5.1' -> (4, 5, 1)) em tupla comparável."""
    cleaned = re.sub(r"^v", "", str(version or "").strip(), flags=re.IGNORECASE)
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0, 0, 0)
    nums = [int(p) for p in parts[:4]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def is_newer_version(remote_version: str, current_version: str = APP_VERSION) -> bool:
    """Retorna True se remote_version for estritamente maior que current_version."""
    return parse_semver(remote_version) > parse_semver(current_version)


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def load_ed25519_public_key(
    public_key: Ed25519PublicKey | bytes | str | None = None,
    default_b64: str = DEFAULT_ED25519_PUBLIC_KEY_B64,
) -> Ed25519PublicKey:
    """Carrega chave pública Ed25519 de objeto, bytes PEM/raw ou string base64.

    Sem chave explícita: env ESTOKE_UPDATER_PUBKEY_B64, config updater_pubkey_b64 ou default_b64.
    """
    if isinstance(public_key, Ed25519PublicKey):
        return public_key

    if public_key is None:
        cfg = app_config.load_config()
        key_str = (
            os.environ.get("ESTOKE_UPDATER_PUBKEY_B64")
            or cfg.get("updater_pubkey_b64")
            or default_b64
        )
        public_key = key_str

    if isinstance(public_key, str):
        raw_str = public_key.strip()
        if "BEGIN PUBLIC KEY" in raw_str:
            loaded = serialization.load_pem_public_key(raw_str.encode("utf-8"))
            if not isinstance(loaded, Ed25519PublicKey):
                raise TypeError("Chave PEM fornecida não é Ed25519.")
            return loaded
        raw_bytes = base64.b64decode(raw_str)
        return Ed25519PublicKey.from_public_bytes(raw_bytes)

    if isinstance(public_key, (bytes, bytearray)):
        raw = bytes(public_key)
        if b"BEGIN PUBLIC KEY" in raw:
            loaded = serialization.load_pem_public_key(raw)
            if not isinstance(loaded, Ed25519PublicKey):
                raise TypeError("Chave PEM fornecida não é Ed25519.")
            return loaded
        return Ed25519PublicKey.from_public_bytes(raw)

    raise TypeError("Formato inválido de chave pública Ed25519.")


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Serialização determinística do manifesto excluindo o campo 'signature'."""
    body = {k: v for k, v in (manifest or {}).items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_file_sha256(filepath: str | Path) -> str:
    """Calcula checksum SHA-256 em hexadecimal minúsculo de um arquivo binário."""
    path = Path(filepath)
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado para cálculo SHA-256: {path}")
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def generate_ed25519_keypair() -> tuple[Ed25519PrivateKey, str, str]:
    """Gera par de chaves Ed25519 retornando `(private_key_obj, private_b64, public_b64)`."""
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, base64.b64encode(priv_raw).decode("ascii"), base64.b64encode(pub_raw).decode("ascii")


def load_ed25519_private_key(private_key: Ed25519PrivateKey | bytes | str) -> Ed25519PrivateKey:
    """Carrega chave privada Ed25519 de objeto, bytes PEM/raw ou string Base64."""
    if isinstance(private_key, Ed25519PrivateKey):
        return private_key

    if isinstance(private_key, str):
        raw_str = private_key.strip()
        if "BEGIN PRIVATE KEY" in raw_str:
            loaded = serialization.load_pem_private_key(raw_str.encode("utf-8"), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise TypeError("Chave PEM fornecida não é Ed25519.")
            return loaded
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw_str))

    if isinstance(private_key, (bytes, bytearray)):
        raw = bytes(private_key)
        if b"BEGIN PRIVATE KEY" in raw:
            loaded = serialization.load_pem_private_key(raw, password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise TypeError("Chave PEM fornecida não é Ed25519.")
            return loaded
        return Ed25519PrivateKey.from_private_bytes(raw)

    raise TypeError("Formato inválido de chave privada Ed25519.")


def build_signed_release_manifest(
    version: str,
    private_key: Ed25519PrivateKey | bytes | str,
    full_installer: str | Path | None = None,
    trial_installer: str | Path | None = None,
    base_download_url: str = "https://updates.estokeaocubo.com.br/releases",
    notes: str = "",
) -> dict[str, Any]:
    """
    Constrói e assina com Ed25519 um manifesto `version.json` dual-edition (`full` e/ou `trial`)
    com SHA-256 e tamanho real dos instaladores Inno Setup.
    """
    clean_ver = re.sub(r"^v", "", str(version or APP_VERSION).strip(), flags=re.IGNORECASE) or APP_VERSION
    base_url = str(base_download_url or "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("base_download_url deve usar HTTPS.")

    priv = load_ed25519_private_key(private_key)
    released_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    default_notes = notes or f"Estoke Ao Cubo v{clean_ver}"

    editions: dict[str, dict[str, Any]] = {}
    for ed_name, inst_arg in (("full", full_installer), ("trial", trial_installer)):
        if inst_arg is None:
            continue
        inst_path = Path(inst_arg)
        if not inst_path.is_file():
            raise FileNotFoundError(f"Instalador {ed_name} não encontrado: {inst_path}")
        entry_payload = {
            "version": clean_ver,
            "edition": ed_name,
            "filename": inst_path.name,
            "url": f"{base_url}/v{clean_ver}/{inst_path.name}",
            "sha256": compute_file_sha256(inst_path),
            "size_bytes": inst_path.stat().st_size,
            "released_at": released_at,
            "notes": default_notes,
        }
        editions[ed_name] = sign_manifest(entry_payload, priv)

    if not editions:
        raise ValueError("Informe ao menos um instalador (`full_installer` ou `trial_installer`).")

    primary = editions.get("full") or editions["trial"]
    top_manifest: dict[str, Any] = {
        "version": clean_ver,
        "filename": primary["filename"],
        "url": primary["url"],
        "sha256": primary["sha256"],
        "size_bytes": primary["size_bytes"],
        "released_at": released_at,
        "notes": default_notes,
        "editions": editions,
    }
    return sign_manifest(top_manifest, priv)


def sign_manifest(manifest: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Assina um dicionário de manifesto de release usando Ed25519."""
    copy_m = {k: v for k, v in manifest.items() if k != "signature"}
    sig = private_key.sign(canonical_manifest_bytes(copy_m))
    copy_m["signature"] = base64.b64encode(sig).decode("ascii")
    return copy_m


def verify_manifest_signature(
    manifest: dict[str, Any],
    public_key: Ed25519PublicKey | bytes | str | None = None,
) -> tuple[bool, str]:
    """Verifica assinatura Ed25519 e campos obrigatórios de segurança do version.json."""
    if not isinstance(manifest, dict):
        return False, "Manifesto inválido."
    sig_b64 = str(manifest.get("signature") or "").strip()
    if not sig_b64:
        return False, "Manifesto sem assinatura Ed25519."

    url = str(manifest.get("url") or "").strip()
    sha256_hex = str(manifest.get("sha256") or "").strip().lower()
    if not url.startswith("https://"):
        return False, "URL do instalador deve usar HTTPS."
    if not re.fullmatch(r"[0-9a-f]{64}", sha256_hex):
        return False, "Checksum SHA-256 do instalador ausente ou inválido."

    try:
        pub = load_ed25519_public_key(public_key)
        sig_bytes = base64.b64decode(sig_b64)
        pub.verify(sig_bytes, canonical_manifest_bytes(manifest))
        return True, "Assinatura Ed25519 válida."
    except InvalidSignature:
        return False, "Assinatura Ed25519 do manifesto é inválida (possível adulteração)."
    except Exception as exc:
        return False, f"Erro ao validar assinatura do manifesto: {exc}"


def get_machine_fingerprint() -> str:
    """Gera identificador estável de máquina sem expor dados sensíveis em claro."""
    raw = "|".join([platform.node(), platform.machine(), os.environ.get("COMPUTERNAME", "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def create_license_token(payload: dict[str, Any], private_key: Ed25519PrivateKey) -> str:
    """Emite token de licença/trial assinado com Ed25519 (<payload_b64u>.<sig_b64u>)."""
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = _b64u_encode(payload_bytes)
    sig = private_key.sign(payload_b64.encode("ascii"))
    return f"{payload_b64}.{_b64u_encode(sig)}"


def verify_license_token(
    token: str,
    public_key: Ed25519PublicKey | bytes | str | None = None,
    now_date: dt.date | None = None,
    machine_id: str | None = None,
) -> tuple[bool, dict[str, Any] | str]:
    """Valida token assinado Ed25519 (edição full/trial, validade e vínculo opcional de máquina)."""
    raw = str(token or "").strip()
    if "." not in raw:
        return False, "Formato de token de licença inválido."

    parts = raw.split(".", 1)
    payload_b64, sig_b64 = parts[0], parts[1]
    try:
        pub = load_ed25519_public_key(public_key, default_b64=DEFAULT_LICENSE_PUBLIC_KEY_B64)
        sig_bytes = _b64u_decode(sig_b64)
        pub.verify(sig_bytes, payload_b64.encode("ascii"))
        payload = json.loads(_b64u_decode(payload_b64).decode("utf-8"))
    except InvalidSignature:
        return False, "Assinatura Ed25519 da licença inválida."
    except Exception as exc:
        return False, f"Falha ao decodificar token de licença: {exc}"

    edition = str(payload.get("edition") or "").strip().lower()
    if edition not in ("full", "trial"):
        return False, "Edição desconhecida no token de licença."

    exp_str = str(payload.get("expires_at") or "").strip()
    if exp_str:
        try:
            exp_date = dt.date.fromisoformat(exp_str[:10])
        except ValueError:
            return False, "Data de expiração inválida na licença."
        ref_date = now_date or dt.date.today()
        if ref_date > exp_date:
            return False, f"Licença expirada em {exp_date.strftime('%d/%m/%Y')}."

    token_mid = str(payload.get("machine_id") or "").strip()
    if token_mid and machine_id and token_mid != machine_id:
        return False, "Licença vinculada a outro computador."

    return True, payload


def _read_external_license_file() -> str:
    local_app = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    lic_path = Path(local_app) / "EstokeAoCubo" / "license.lic"
    if lic_path.exists():
        try:
            return lic_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def get_license_status(
    config_dict: dict[str, Any] | None = None,
    public_key: Ed25519PublicKey | bytes | str | None = None,
    now_date: dt.date | None = None,
    machine_id: str | None = None,
) -> dict[str, Any]:
    """
    Fluxo único de validação Full vs. Trial:
    1. ESTOKE_TRIAL=1 -> trial imediato
    2. Executável contendo 'trial' no nome -> trial imediato
    3. Token Ed25519 em ESTOKE_LICENSE_TOKEN / config['license_token'] / %LOCALAPPDATA%/EstokeAoCubo/license.lic
    4. Fallback padrão Full (exceto se require_signed_license=True)
    """
    cfg = config_dict if config_dict is not None else app_config.load_config()

    if os.environ.get("ESTOKE_TRIAL") == "1":
        return {"edition": "trial", "is_trial": True, "valid": True, "source": "env", "reason": "ESTOKE_TRIAL=1"}

    exe_name = os.path.basename(sys.executable).lower()
    if "trial" in exe_name:
        return {"edition": "trial", "is_trial": True, "valid": True, "source": "executable", "reason": exe_name}

    token = (
        os.environ.get("ESTOKE_LICENSE_TOKEN")
        or str(cfg.get("license_token") or "").strip()
        or _read_external_license_file()
    )
    if token:
        ok, res = verify_license_token(
            token,
            public_key=public_key,
            now_date=now_date,
            machine_id=machine_id or get_machine_fingerprint(),
        )
        if not ok:
            return {
                "edition": "trial",
                "is_trial": True,
                "valid": False,
                "source": "license_token_invalido",
                "reason": str(res),
            }
        edition = str(res.get("edition", "trial")).lower()
        return {
            "edition": edition,
            "is_trial": edition != "full",
            "valid": True,
            "source": "license_token",
            "payload": res,
            "expires_at": res.get("expires_at", ""),
        }

    require_lic = os.environ.get("ESTOKE_REQUIRE_LICENSE") == "1" or bool(cfg.get("require_signed_license", False))
    if require_lic:
        return {
            "edition": "trial",
            "is_trial": True,
            "valid": False,
            "source": "license_missing",
            "reason": "Licença assinada obrigatória não encontrada.",
        }

    return {"edition": "full", "is_trial": False, "valid": True, "source": "default_full", "reason": ""}


def _resolve_edition_payload(manifest: dict[str, Any], edition: str) -> dict[str, Any]:
    """Suporta manifesto simples ou manifesto separado por canal/edição (`editions.full` / `editions.trial`)."""
    editions = manifest.get("editions")
    if isinstance(editions, dict) and isinstance(editions.get(edition), dict):
        ed_payload = editions[edition]
        if ed_payload.get("signature"):
            return dict(ed_payload)
        merged = {k: v for k, v in manifest.items() if k != "editions"}
        merged.update(ed_payload)
        return merged
    return manifest


def fetch_manifest(manifest_url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Baixa version.json exclusivamente via HTTPS."""
    url = str(manifest_url or "").strip()
    if not url.startswith("https://"):
        raise ValueError("URL de atualização deve utilizar HTTPS.")

    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"EstokeAoCubo/{APP_VERSION}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2 * 1024 * 1024).decode("utf-8")
    return json.loads(raw)


def check_for_updates(
    manifest_url: str | None = None,
    current_version: str = APP_VERSION,
    public_key: Ed25519PublicKey | bytes | str | None = None,
    fetcher: Any = None,
) -> dict[str, Any]:
    """
    Consulta e valida assinatura Ed25519 do manifesto remoto.
    Retorna dict estruturado: available (bool), version, url, sha256, notes, edition, error.
    """
    cfg = app_config.load_config()
    lic = get_license_status(cfg, public_key=public_key)
    edition = lic["edition"]
    url = (
        manifest_url
        or os.environ.get("ESTOKE_UPDATE_URL")
        or str(cfg.get("update_manifest_url") or "").strip()
        or DEFAULT_UPDATE_MANIFEST_URL
    )

    try:
        manifest_raw = fetcher(url) if callable(fetcher) else fetch_manifest(url)
        manifest = _resolve_edition_payload(manifest_raw, edition)
    except Exception as exc:
        logger.debug("Check de atualização falhou (%s): %s", url, exc)
        return {"available": False, "edition": edition, "error": str(exc)}

    sig_ok, sig_msg = verify_manifest_signature(manifest, public_key=public_key)
    if not sig_ok:
        logger.warning("Manifesto de atualização rejeitado: %s", sig_msg)
        return {"available": False, "edition": edition, "error": sig_msg}

    remote_ver = str(manifest.get("version") or "0.0.0").strip()
    available = is_newer_version(remote_ver, current_version)
    return {
        "available": available,
        "edition": edition,
        "version": remote_ver,
        "current_version": current_version,
        "url": str(manifest.get("url") or "").strip(),
        "sha256": str(manifest.get("sha256") or "").strip().lower(),
        "notes": str(manifest.get("notes") or "").strip(),
        "manifest": manifest,
        "error": "",
    }


def download_verified_installer(
    url: str,
    expected_sha256: str,
    version: str = "",
    dest_dir: str | None = None,
    timeout: float = 120.0,
) -> tuple[bool, str]:
    """
    Baixa o instalador Inno Setup para %TEMP% (ou dest_dir) e valida SHA-256 em streaming.
    Remove o arquivo temporário imediatamente se o hash divergir.
    """
    if not str(url or "").startswith("https://"):
        return False, "URL de download deve usar HTTPS."
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False, "Hash SHA-256 esperado é inválido."

    target_root = Path(dest_dir or tempfile.gettempdir())
    target_root.mkdir(parents=True, exist_ok=True)
    safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", str(version or "latest"))
    final_path = target_root / f"EstokeAoCubo-Setup-{safe_ver}.exe"
    tmp_path = target_root / f"EstokeAoCubo-Setup-{safe_ver}.exe.part"

    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": f"EstokeAoCubo/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp_path, "wb") as out_f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
                out_f.write(chunk)

        got = digest.hexdigest().lower()
        if got != expected:
            tmp_path.unlink(missing_ok=True)
            return False, f"Checksum SHA-256 divergente (esperado={expected[:12]}..., obtido={got[:12]}...)."

        os.replace(tmp_path, final_path)
        return True, str(final_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return False, f"Falha ao baixar instalador: {exc}"


def build_inno_install_command(installer_path: str) -> list[str]:
    """Monta comando seguro do Inno Setup para execução silenciosa ao fechar o app."""
    path = Path(installer_path)
    if not path.exists() or path.suffix.lower() != ".exe":
        raise FileNotFoundError(f"Instalador inválido ou não encontrado: {installer_path}")
    return [str(path), *INNO_SILENT_FLAGS]


def launch_installer_on_exit(installer_path: str) -> tuple[bool, str]:
    """Dispara o instalador Inno Setup em processo desacoplado ao encerrar a aplicação."""
    try:
        cmd = build_inno_install_command(installer_path)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)
        logger.info("Instalador silencioso iniciado: %s", cmd)
        return True, "Instalador iniciado."
    except Exception as exc:
        logger.error("Erro ao iniciar instalador na saída: %s", exc)
        return False, str(exc)
