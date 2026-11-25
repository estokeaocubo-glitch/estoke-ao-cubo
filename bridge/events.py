# -*- coding: utf-8 -*-
"""
Dispatcher thread-safe de eventos Python -> JS.

Qualquer thread pode chamar :meth:`EventDispatcher.emit`. Os eventos entram numa fila e uma
única thread daemon os entrega via ``window.evaluate_js("window.__estokeEmit(nome, payload)")``,
então nenhuma thread de serviço fica presa esperando o WebView.
"""
from __future__ import annotations

import collections
import json
import logging
import queue
import re
import threading
from typing import Any, Optional

logger = logging.getLogger("EstokeAoCubo.bridge.events")

_NOME_EVENTO = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_SENTINELA = object()


def build_emit_script(event: str, payload: Any = None) -> str:
    """Monta o JS de emissão com JSON escapado (ASCII), seguro para ``evaluate_js``."""
    from bridge import to_jsonable

    if not isinstance(event, str) or not _NOME_EVENTO.match(event):
        raise ValueError(f"Nome de evento inválido: {event!r}")
    nome = json.dumps(event)
    dados = json.dumps(to_jsonable(payload), ensure_ascii=True, sort_keys=True)
    # Evita que '</' ou separadores de linha Unicode quebrem o contexto de script.
    dados = dados.replace("</", "<\\/")
    return f"window.__estokeEmit && window.__estokeEmit({nome}, {dados});"


class EventDispatcher:
    """Fila de eventos com entrega serializada para a janela PyWebView."""

    def __init__(self, history_size: int = 200) -> None:
        self._lock = threading.Lock()
        self._window: Optional[Any] = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self.history: "collections.deque[tuple[str, Any]]" = collections.deque(maxlen=history_size)

    def bind_window(self, window: Any) -> None:
        with self._lock:
            self._window = window
            if self._thread is None and not self._closed:
                self._thread = threading.Thread(
                    target=self._run, name="EstokeBridgeEvents", daemon=True
                )
                self._thread.start()

    def emit(self, event: str, payload: Any = None) -> bool:
        """Enfileira o evento. Retorna True se há janela para entregar."""
        script = build_emit_script(event, payload)
        with self._lock:
            self.history.append((event, payload))
            if self._closed or self._window is None:
                return False
            self._queue.put(script)
            return True

    def close(self, timeout: float = 1.0) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
            self._queue.put(_SENTINELA)
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINELA:
                return
            window = self._window
            if window is None:
                continue
            try:
                window.evaluate_js(item)
            except Exception as exc:  # janela fechando, WebView indisponível etc.
                logger.debug("Falha ao entregar evento ao WebView: %s", exc)
