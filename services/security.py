"""
Módulo de segurança — Hashing com scrypt (memory-hard) e políticas de senha.

Substitui o PBKDF2 por scrypt para proteção contra brute force em GPU,
mantendo compatibilidade com hashes PBKDF2 e SHA-256 legados.
"""
import hashlib
import os
import re
import logging
import hmac

logger = logging.getLogger('EstokeAoCubo.security')

# Parâmetros scrypt (Recomendação OWASP / Aprovado para produção)
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_LENGTH = 32

def validate_password_strength(password: str) -> tuple[bool, str]:
    """Valida se a senha atende aos requisitos mínimos de segurança."""
    if len(password) < 8:
        return False, "A senha deve ter pelo menos 8 caracteres."
    if not re.search(r"[A-Z]", password):
        return False, "A senha deve conter pelo menos uma letra maiúscula."
    if not re.search(r"[a-z]", password):
        return False, "A senha deve conter pelo menos uma letra minúscula."
    if not re.search(r"[0-9]", password):
        return False, "A senha deve conter pelo menos um número."
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return False, "A senha deve conter pelo menos um caractere especial."
    return True, ""

def hash_password(password: str) -> str:
    """Gera hash altamente seguro usando scrypt + salt aleatório.
    
    Retorna no formato: 'scrypt:<N>:<r>:<p>:<salt_hex>:<hash_hex>'
    """
    salt = os.urandom(_SALT_LENGTH)
    dk = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P
    )
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{dk.hex()}"

def verify_password(password: str, stored_hash: str) -> bool:
    """Verifica a senha contra o hash armazenado.
    
    Suporta scrypt, PBKDF2 legado e SHA-256 antigo para migração transparente.
    """
    try:
        if stored_hash.startswith("scrypt:"):
            return _verify_scrypt(password, stored_hash)
        elif stored_hash.startswith("pbkdf2:"):
            return _verify_pbkdf2(password, stored_hash)
        else:
            # Legado SHA-256 puro (sem salt) — aceita para migração
            legacy_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
            return hmac.compare_digest(legacy_hash.encode('utf-8'), stored_hash.encode('utf-8'))
    except Exception as e:
        logger.error("Erro crítico na verificação de hash: %s", e)
        return False

def needs_rehash(stored_hash: str) -> bool:
    """Identifica se o hash atual é legado (SHA-256 ou PBKDF2 antigo) e precisa ser atualizado."""
    return not stored_hash.startswith("scrypt:")

def _verify_scrypt(password: str, stored_hash: str) -> bool:
    """Verifica senha contra hash scrypt."""
    _, n_str, r_str, p_str, salt_hex, hash_hex = stored_hash.split(":")
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    dk = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=int(n_str),
        r=int(r_str),
        p=int(p_str)
    )
    return hmac.compare_digest(dk, expected)

def _verify_pbkdf2(password: str, stored_hash: str) -> bool:
    """Verifica senha contra hash PBKDF2 legado."""
    _, iterations_str, salt_hex, hash_hex = stored_hash.split(":")
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    dk = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt,
        int(iterations_str)
    )
    return hmac.compare_digest(dk, expected)


def validar_cpf(cpf: str) -> bool:
    """Valida CPF pelos dígitos verificadores oficiais (Módulo 11)."""
    digits = "".join(c for c in str(cpf or "") if c.isdigit())
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for i in range(9, 11):
        soma = sum(int(digits[num]) * ((i + 1) - num) for num in range(i))
        digito = ((soma * 10) % 11) % 10
        if digito != int(digits[i]):
            return False
    return True


def validar_cnpj(cnpj: str) -> bool:
    """Valida CNPJ pelos dígitos verificadores oficiais (Módulo 11)."""
    digits = "".join(c for c in str(cnpj or "") if c.isdigit())
    if len(digits) != 14 or len(set(digits)) == 1:
        return False
    pesos_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    soma_1 = sum(int(digits[i]) * pesos_1[i] for i in range(12))
    resto_1 = soma_1 % 11
    dig_1 = 0 if resto_1 < 2 else 11 - resto_1
    if int(digits[12]) != dig_1:
        return False
    pesos_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    soma_2 = sum(int(digits[i]) * pesos_2[i] for i in range(13))
    resto_2 = soma_2 % 11
    dig_2 = 0 if resto_2 < 2 else 11 - resto_2
    return int(digits[13]) == dig_2

