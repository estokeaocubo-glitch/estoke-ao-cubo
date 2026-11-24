# -*- coding: utf-8 -*-
r"""
Estoke Ao Cubo - Módulo Central de Caminhos e Isolamento de Permissões no Windows.

Regras estritas de portabilidade:
- Recursos somente-leitura (ícones, templates HTML, cacert.pem, termos):
  resolvidos via sys._MEIPASS / pasta do executável ou código-fonte.
- Dados mutáveis (SQLite .db/.db.enc, .db_key, config.json, logs, backups, exports, temp):
  gravados exclusivamente em ESTOKE_DATA_DIR (quando definido) ou %LOCALAPPDATA%\EstokeAoCubo,
  jamais dentro de C:\Program Files.
"""

from __future__ import annotations

import os
import sys

APP_DIR_NAME = "EstokeAoCubo"


def get_install_dir() -> str:
    """Retorna o diretório de instalação do executável (ou raiz do código em desenvolvimento)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_path(*parts: str) -> str:
    """
    Resolve caminho para recursos somente-leitura (compatível com PyInstaller --onedir / _MEIPASS).
    Procura em sys._MEIPASS, na pasta de instalação e na raiz do projeto.
    """
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(meipass)
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        src_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(src_dir)
        candidates.append(os.path.dirname(src_dir))

    if not parts:
        return candidates[0]

    for base in candidates:
        cand = os.path.join(base, *parts)
        if os.path.exists(cand):
            return cand
    return os.path.join(candidates[0], *parts)


def get_app_data_path(*parts: str) -> str:
    """
    Retorna a pasta gravável de dados do usuário:
    1. ESTOKE_DATA_DIR (se definida no ambiente — usada em testes e --self-test isolado)
    2. %LOCALAPPDATA%\\EstokeAoCubo (Windows) ou ~/.local/share/EstokeAoCubo
    """
    env_dir = os.environ.get("ESTOKE_DATA_DIR", "").strip()
    if env_dir:
        base_dir = os.path.abspath(env_dir)
    elif os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        base_dir = os.path.join(local_app_data, APP_DIR_NAME)
    else:
        base_dir = os.path.join(os.path.expanduser("~/.local/share"), APP_DIR_NAME)

    target = os.path.join(base_dir, *parts) if parts else base_dir
    os.makedirs(target, exist_ok=True)
    return target


def get_temp_dir(*parts: str) -> str:
    r"""
    Retorna a pasta temporária gravável (%LOCALAPPDATA%\EstokeAoCubo\temp)
    ou o caminho completo dentro dela quando `parts` for informado (sem criar o último segmento como diretório).
    """
    temp_base = get_app_data_path("temp")
    if not parts:
        return temp_base
    target = os.path.join(temp_base, *parts)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return target
