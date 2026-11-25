# -*- coding: utf-8 -*-
"""
Camada bridge da Interface v2.0 (PyWebView).

Python puro: só valida parâmetros, delega para ``services/*`` e ``database.py`` e serializa
a resposta. Nenhuma regra de negócio mora aqui.

Todo método público de :class:`EstokeBridge` (exposto ao JS como ``window.pywebview.api``)
devolve o envelope::

    {"ok": bool, "data": <json>, "error": {"code": str, "message": str} | None}

Exceções nunca vazam stacktrace para o frontend: o traceback vai para o logger.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import functools
import logging
import math
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("EstokeAoCubo.bridge")

__all__ = [
    "BridgeError",
    "BridgeSession",
    "EstokeBridge",
    "envelope_fail",
    "envelope_ok",
    "safe_call",
    "to_jsonable",
]

PERFIS_MULTI_LOJA = frozenset({"admin", "gerente"})


class BridgeError(Exception):
    """Erro de domínio da bridge com código estável para o frontend."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


# ---------------------------------------------------------------------------
# Serialização determinística
# ---------------------------------------------------------------------------

def to_jsonable(value: Any) -> Any:
    """
    Converte ``value`` em estrutura JSON determinística.

    - ``Decimal`` vira string (dinheiro nunca trafega como float); NaN/Infinity viram ``None``.
    - ``date``/``datetime``/``time`` viram ISO-8601.
    - ``float`` NaN/Infinity viram ``None``.
    - ``dict`` tem as chaves convertidas para ``str``; ``set`` vira lista ordenada.
    - ``pandas.DataFrame`` vira lista de dicts; escalares numpy/pandas viram nativos.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        itens = [to_jsonable(v) for v in value]
        return sorted(itens, key=lambda x: (str(type(x)), str(x)))
    if isinstance(value, (bytes, bytearray)):
        return None

    # pandas / numpy sem importar os módulos quando não usados
    mod = type(value).__module__ or ""
    if mod.startswith("pandas"):
        if hasattr(value, "to_dict") and hasattr(value, "columns"):
            return [to_jsonable(r) for r in value.to_dict(orient="records")]
        if hasattr(value, "to_dict"):
            return to_jsonable(value.to_dict())
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:  # NaT
                return None
        try:
            import pandas as pd  # noqa: WPS433
            if pd.isna(value):
                return None
        except Exception:
            pass
    if mod.startswith("numpy"):
        if hasattr(value, "tolist"):
            return to_jsonable(value.tolist())
        if hasattr(value, "item"):
            return to_jsonable(value.item())
    return str(value)


def envelope_ok(data: Any = None) -> dict:
    return {"ok": True, "data": to_jsonable(data), "error": None}


def envelope_fail(code: str, message: str) -> dict:
    return {"ok": False, "data": None, "error": {"code": str(code), "message": str(message)}}


def safe_call(func: Callable[..., Any]) -> Callable[..., dict]:
    """Decorador: executa ``func`` e devolve sempre o envelope, sem vazar stacktrace."""

    @functools.wraps(func)
    def _wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return envelope_ok(func(*args, **kwargs))
        except BridgeError as exc:
            return envelope_fail(exc.code, exc.message)
        except PermissionError as exc:
            return envelope_fail("PERMISSION_DENIED", str(exc) or "Permissão negada.")
        except (ValueError, TypeError) as exc:
            logger.info("Bridge %s: entrada inválida: %s", func.__name__, exc)
            return envelope_fail("INVALID_INPUT", str(exc) or "Entrada inválida.")
        except Exception:
            logger.exception("Bridge %s: erro interno", func.__name__)
            return envelope_fail("INTERNAL_ERROR", "Erro interno. Detalhes registrados no log do sistema.")

    return _wrapper


# ---------------------------------------------------------------------------
# Estado de sessão (thread-safe: o pywebview chama a js_api em threads)
# ---------------------------------------------------------------------------

class BridgeSession:
    """Usuário autenticado e loja ativa da janela web."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._user: Optional[dict] = None
        self._loja_ativa_id: Optional[int] = None
        self._fullscreen = False

    def start(self, user: dict) -> None:
        with self._lock:
            self._user = dict(user)
            self._loja_ativa_id = int(user.get("loja_id") or 1)

    def clear(self) -> None:
        with self._lock:
            self._user = None
            self._loja_ativa_id = None

    @property
    def user(self) -> Optional[dict]:
        with self._lock:
            return dict(self._user) if self._user else None

    @property
    def loja_ativa_id(self) -> Optional[int]:
        with self._lock:
            return self._loja_ativa_id

    def set_loja_ativa(self, loja_id: int) -> None:
        with self._lock:
            self._loja_ativa_id = int(loja_id)

    def toggle_fullscreen(self) -> bool:
        with self._lock:
            self._fullscreen = not self._fullscreen
            return self._fullscreen

    def require_user(self) -> dict:
        user = self.user
        if not user:
            raise BridgeError("NOT_AUTHENTICATED", "Faça login para continuar.")
        return user


# ---------------------------------------------------------------------------
# Agregadora exposta ao JS
# ---------------------------------------------------------------------------

class EstokeBridge:
    """
    Objeto ``js_api`` do PyWebView.

    Só métodos públicos (sem ``_``) ficam visíveis ao JS. Atributos internos começam com ``_``
    para o pywebview não introspectar Database, serviços etc.
    """

    def __init__(
        self,
        db: Any = None,
        config_module: Any = None,
        update_fetcher: Optional[Callable[..., Any]] = None,
        on_session_started: Optional[Callable[[dict], None]] = None,
    ) -> None:
        from bridge.events import EventDispatcher
        from bridge.app_bridge import AppBridge
        from bridge.auth_bridge import AuthBridge

        if db is None:
            import database
            db = database.Database()
        if config_module is None:
            import config as config_module  # noqa: WPS433

        self._db = db
        self._config = config_module
        self._session = BridgeSession()
        self._events = EventDispatcher()
        self._window = None
        self._update_fetcher = update_fetcher
        self._on_session_started = on_session_started
        self._app = AppBridge(self)
        self._auth = AuthBridge(self)

    # -- integração com a janela (não exposto ao JS) --
    def _bind_window(self, window: Any) -> None:
        self._window = window
        self._events.bind_window(window)

    def _shutdown(self) -> None:
        self._events.close()

    # -- utilitário --
    @safe_call
    def ping(self) -> dict:
        return {"pong": True}

    # -- app_bridge --
    @safe_call
    def get_bootstrap(self) -> dict:
        return self._app.get_bootstrap()

    @safe_call
    def get_theme(self) -> str:
        return self._app.get_theme()

    @safe_call
    def set_theme(self, theme: str) -> str:
        return self._app.set_theme(theme)

    @safe_call
    def list_lojas(self) -> dict:
        return self._app.list_lojas()

    @safe_call
    def set_loja_ativa(self, loja_id: Any) -> dict:
        return self._app.set_loja_ativa(loja_id)

    @safe_call
    def toggle_fullscreen(self) -> dict:
        return self._app.toggle_fullscreen()

    @safe_call
    def get_sidebar_menu(self) -> dict:
        return self._app.get_sidebar_menu()

    @safe_call
    def check_updates_status(self) -> dict:
        return self._app.check_updates_status()

    # -- auth_bridge --
    @safe_call
    def get_first_access_catalog(self) -> dict:
        return self._auth.get_first_access_catalog()

    @safe_call
    def complete_first_access(self, nicho: str, aceite_termos: Any = False) -> dict:
        return self._auth.complete_first_access(nicho, aceite_termos)

    @safe_call
    def login(self, login: str, senha: str) -> dict:
        return self._auth.login(login, senha)

    @safe_call
    def logout(self) -> dict:
        return self._auth.logout()

    @safe_call
    def get_current_session(self) -> dict:
        return self._auth.get_current_session()

    @safe_call
    def autorizar_supervisor(
        self, login: str, senha: str, acao: str, motivo: str = "", exigir_motivo: Any = False
    ) -> dict:
        return self._auth.autorizar_supervisor(login, senha, acao, motivo, exigir_motivo)
