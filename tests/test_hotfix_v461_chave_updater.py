# -*- coding: utf-8 -*-
"""Hotfix v4.6.1: chave pública Ed25519 do auto-update trocada por um par novo.

- O verificador aceita um manifesto assinado pela chave privada nova (fixture abaixo, assinada
  fora do repositório; a privada nunca entra no código) e recusa qualquer adulteração.
- Sem chave explícita, o auto-update usa a chave nova embutida.
- Tokens de licença continuam validados pela chave da v4.6.0, para que licenças já emitidas
  não caiam para trial com a troca.

Nada aqui faz rede: todo o fluxo usa dicionários locais e um fetcher falso.
"""
from __future__ import annotations

import copy
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.updater as updater  # noqa: E402

CHAVE_NOVA_UPDATER = "D1csAKUbmtRR6bIvsVqW9wu0cJyPVduZJRN/vrOHkUo="
CHAVE_LICENCA_V460 = "fO2QB+dHmaf/kA4UAtfyRSfIK+wUI+tb0DZxX9nlowc="

# Manifesto fictício assinado com a privada nova (C:\dev\_segredos\updater_private.pem).
MANIFESTO_ASSINADO_CHAVE_NOVA = {
    "version": "0.0.1",
    "url": "https://updates.invalid/fixture-hotfix-v461.exe",
    "sha256": "ab" * 32,
    "size_bytes": 1,
    "notes": "fixture de teste, nao e release",
    "signature": "RBnXWd0AL5osMLpex0MIqfvNvx1FjxsJT0slVOVkGrHL1eaWyFO+6Va8k4MKP1lqQlNOkS7p1LRZQhn9doJUDQ==",
}


@pytest.fixture
def sem_override_de_chave(monkeypatch, tmp_path):
    """Garante que só a chave embutida vale (sem env, config nem license.lic do usuário)."""
    for var in ("ESTOKE_UPDATER_PUBKEY_B64", "ESTOKE_LICENSE_TOKEN", "ESTOKE_TRIAL", "ESTOKE_REQUIRE_LICENSE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(updater.app_config, "load_config", lambda: {})


def test_chaves_embutidas():
    assert updater.DEFAULT_ED25519_PUBLIC_KEY_B64 == CHAVE_NOVA_UPDATER
    assert updater.DEFAULT_LICENSE_PUBLIC_KEY_B64 == CHAVE_LICENCA_V460


def test_aceita_assinatura_da_chave_nova(sem_override_de_chave):
    ok, msg = updater.verify_manifest_signature(copy.deepcopy(MANIFESTO_ASSINADO_CHAVE_NOVA))
    assert ok, msg


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("version", "9.9.9"),
        ("url", "https://updates.invalid/malicioso.exe"),
        ("sha256", "cd" * 32),
        ("size_bytes", 2),
        ("extra", "campo injetado"),
    ],
)
def test_recusa_payload_adulterado(sem_override_de_chave, campo, valor):
    adulterado = copy.deepcopy(MANIFESTO_ASSINADO_CHAVE_NOVA)
    adulterado[campo] = valor
    ok, msg = updater.verify_manifest_signature(adulterado)
    assert not ok
    assert "inválida" in msg


def test_recusa_assinatura_de_outra_chave(sem_override_de_chave):
    outra = Ed25519PrivateKey.generate()
    manifesto = {k: v for k, v in MANIFESTO_ASSINADO_CHAVE_NOVA.items() if k != "signature"}
    ok, _ = updater.verify_manifest_signature(updater.sign_manifest(manifesto, outra))
    assert not ok


def test_check_for_updates_usa_chave_nova(sem_override_de_chave):
    """Fluxo completo com fetcher falso: versão maior e assinatura válida -> available."""
    manifesto = copy.deepcopy(MANIFESTO_ASSINADO_CHAVE_NOVA)
    res = updater.check_for_updates(
        manifest_url="https://updates.invalid/version.json",
        current_version="0.0.0",
        fetcher=lambda *_a, **_k: manifesto,
    )
    assert res.get("available") is True, res


def test_licenca_continua_na_chave_da_v460(sem_override_de_chave):
    """Token assinado por outra chave continua recusado; a licença não passa a usar a chave nova."""
    outra = Ed25519PrivateKey.generate()
    token = updater.create_license_token({"edition": "full"}, outra)
    ok, msg = updater.verify_license_token(token)
    assert not ok
    assert "inválida" in msg
    # Com a chave explícita o fluxo de licença segue igual ao da v4.6.0.
    pub = outra.public_key()
    ok, payload = updater.verify_license_token(token, public_key=pub)
    assert ok and payload["edition"] == "full"
