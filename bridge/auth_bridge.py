# -*- coding: utf-8 -*-
"""
Bridge de autenticação: primeiro acesso (18 nichos + termos), login, logout, sessão e
autorização de supervisor.

Delega para ``services.auth_service.AuthService``, ``services.nicho_service.NichoService``,
``config`` e ``database.Database`` exatamente como o fluxo legado de ``main.py``.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from bridge import BridgeError

if TYPE_CHECKING:  # pragma: no cover
    from bridge import EstokeBridge

logger = logging.getLogger("EstokeAoCubo.bridge.auth")

PERFIL_EMOJI = {"admin": "👑", "gerente": "📊", "vendedor": "🛒", "caixa": "💰"}
_CAMPOS_USUARIO = ("id", "nome", "perfil", "loja_id")


def _usuario_publico(user: dict | None) -> dict | None:
    if not user:
        return None
    pub = {k: user.get(k) for k in _CAMPOS_USUARIO}
    pub["emoji"] = PERFIL_EMOJI.get(str(user.get("perfil") or ""), "👤")
    return pub


def _ler_termos() -> str | None:
    import paths

    candidatos = [
        paths.get_resource_path("TERMOS_DE_USO.txt"),
        os.path.join(paths.get_install_dir(), "TERMOS_DE_USO.txt"),
    ]
    for caminho in candidatos:
        try:
            if os.path.exists(caminho):
                with open(caminho, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read()
        except OSError:
            logger.debug("Não foi possível ler %s", caminho, exc_info=True)
    return None


class AuthBridge:
    def __init__(self, root: "EstokeBridge") -> None:
        self._root = root

    def _auth_service(self):
        from services.auth_service import AuthService
        return AuthService(self._root._db, self._root._config)

    def _nicho_service(self):
        from services.nicho_service import NichoService
        return NichoService(self._root._config)

    # ------------------------------------------------------------------
    def get_first_access_catalog(self) -> dict:
        nichos = self._nicho_service().listar_nichos()
        return {
            "nichos": nichos,
            "termos": _ler_termos(),
            "ja_configurado": self._root._config.get_business_type() is not None,
        }

    def complete_first_access(self, nicho: str, aceite_termos: Any = False) -> dict:
        if aceite_termos is not True:
            raise ValueError("É preciso aceitar os Termos de Uso para continuar.")
        cfg_mod = self._root._config
        if cfg_mod.get_business_type() is not None:
            raise BridgeError("ALREADY_CONFIGURED", "O primeiro acesso já foi concluído.")
        chave = str(nicho or "").strip()
        validos = {n["key"] for n in self._nicho_service().listar_nichos()}
        if chave not in validos:
            raise ValueError("Nicho inválido.")
        cfg_mod.set_business_type(chave)
        self._root._events.emit("first_access_completed", {"nicho": chave})
        return {"nicho": chave}

    # ------------------------------------------------------------------
    def login(self, login: str, senha: str) -> dict:
        auth = self._auth_service()
        user = auth.autenticar(login, senha)  # ValueError se vazio
        if not user:
            raise BridgeError("AUTH_FAILED", "Usuário ou senha incorretos!")
        self._root._db.set_current_user(user)
        self._root._session.start(user)
        trocar = bool(auth.precisa_trocar_senha(user))
        sessao = self.get_current_session()
        self._root._events.emit("session_changed", {"autenticado": True})
        callback = self._root._on_session_started
        if callback is not None:
            try:
                callback(dict(user))
            except Exception:
                logger.exception("Falha ao iniciar rotinas pós-login.")
        sessao["trocar_senha"] = trocar
        return sessao

    def logout(self) -> dict:
        self._root._db.set_current_user(None)
        self._root._session.clear()
        self._root._events.emit("session_changed", {"autenticado": False})
        return {"autenticado": False}

    def get_current_session(self) -> dict:
        session = self._root._session
        user = session.user
        return {
            "autenticado": user is not None,
            "usuario": _usuario_publico(user),
            "loja_ativa_id": session.loja_ativa_id,
        }

    # ------------------------------------------------------------------
    def autorizar_supervisor(
        self, login: str, senha: str, acao: str, motivo: str = "", exigir_motivo: Any = False
    ) -> dict:
        self._root._session.require_user()
        acao_limpa = str(acao or "").strip()
        if not acao_limpa:
            raise ValueError("Ação não informada.")
        resultado = self._auth_service().autorizar_supervisor(
            login, senha, acao_limpa, motivo=motivo or "", exigir_motivo=bool(exigir_motivo)
        )
        return {
            "approved": bool(resultado.get("approved")),
            "supervisor_nome": resultado.get("supervisor_nome"),
            "supervisor_perfil": resultado.get("supervisor_perfil"),
            "motivo": resultado.get("motivo") or "",
        }
