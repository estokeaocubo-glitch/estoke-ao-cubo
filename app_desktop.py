# -*- coding: utf-8 -*-
"""
Ponto de entrada da Interface v2.0 (PyWebView + frontend web offline).

Uso:  python main.py --ui=web     (o padrão continua sendo a UI legada CustomTkinter)

- Serve ``web/index.html`` (via ``paths.get_web_path``) pelo servidor HTTP embutido do
  pywebview, somente em 127.0.0.1.
- Respeita ``ESTOKE_DATA_DIR`` (todo caminho de dados passa por ``paths``/``database``).
- Sobe Outbox, agendador de rotinas, backup diário e API REST em threads de fundo,
  depois do primeiro login (mesma ordem do shell legado), sem Tkinter.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import paths  # noqa: E402

logger = logging.getLogger("EstokeAoCubo.desktop")

SCHEDULER_INTERVALO_S = 60.0
_FUNDO_POR_TEMA = {"light": "#F0F0F5", "dark": "#12151C"}


def _configurar_logging() -> None:
    raiz = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in raiz.handlers):
        return
    log_dir = paths.get_app_data_path("logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(
                os.path.join(log_dir, "app.log"), maxBytes=5 * 1024 * 1024,
                backupCount=3, encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )


def _registrar_mimetypes() -> None:
    """O registro do Windows às vezes mapeia .js como text/plain, o que quebra ES modules."""
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("image/svg+xml", ".svg")
    mimetypes.add_type("font/woff2", ".woff2")


class BackgroundWorkers:
    """Outbox, agendador, fiscal, backup diário e API REST, sem dependência de Tk."""

    def __init__(self, db: Any) -> None:
        from services.outbox_service import OutboxService
        from services.scheduler import RoutineScheduler
        from services.fiscal_service import FiscalService

        self.db = db
        self.outbox_service = OutboxService(db)
        self.routine_scheduler = RoutineScheduler(db, self.outbox_service)
        self.fiscal_service = FiscalService(db, outbox_service=self.outbox_service)
        db._fiscal_service = self.fiscal_service
        self.api_server = None
        self._lock = threading.Lock()
        self._started = False
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

    def start(self, _user: Optional[dict] = None) -> None:
        """Idempotente: roda uma única vez, no primeiro login."""
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._start_bg, name="EstokeWorkersBoot", daemon=True).start()

    def _start_bg(self) -> None:
        try:
            ok, msg = self.db.verificar_backup_diario()
            logger.info("Backup diário: %s (%s)", "ok" if ok else "não realizado", msg)
        except Exception as exc:
            logger.warning("Erro no backup diário: %s", exc)
        try:
            import api_server
            if api_server.api_disponivel():
                self.api_server = api_server.EstokeAPI(self.db, port=5050)
                self.api_server.start()
        except Exception as exc:
            logger.debug("API REST não disponível: %s", exc)
        try:
            self.outbox_service.start_worker()
        except Exception as exc:
            logger.warning("Erro ao iniciar OutboxWorker: %s", exc)
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="EstokeScheduler", daemon=True
        )
        self._scheduler_thread.start()

    def _scheduler_loop(self) -> None:
        while not self._stop_event.wait(SCHEDULER_INTERVALO_S):
            try:
                self.routine_scheduler.tick()
            except Exception as exc:
                logger.debug("Erro no tick do agendador de rotinas: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        if self.api_server is not None:
            try:
                self.api_server.stop()
            except Exception:
                logger.debug("Falha ao parar API REST.", exc_info=True)
        try:
            stop_sched = getattr(self.routine_scheduler, "stop", None)
            if callable(stop_sched):
                stop_sched()
        except Exception:
            logger.debug("Falha ao parar agendador.", exc_info=True)
        try:
            self.outbox_service.stop_worker(timeout=1.5)
        except Exception:
            logger.debug("Falha ao parar Outbox.", exc_info=True)
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=1.0)


def main(argv: Optional[list[str]] = None) -> int:
    _configurar_logging()
    try:
        import crash_handler
        crash_handler.install_crash_handlers(show_ui=False)
    except Exception:
        logger.debug("crash_handler indisponível.", exc_info=True)

    try:
        import webview
    except ImportError:
        logger.error("pywebview não instalado. Rode: pip install -r requirements.txt")
        print("pywebview não instalado. Rode: pip install -r requirements.txt", file=sys.stderr)
        return 2

    _registrar_mimetypes()
    index = paths.get_web_path("index.html")
    if not os.path.isfile(index):
        logger.error("Frontend web não encontrado: %s", index)
        print(f"Frontend web não encontrado: {index}", file=sys.stderr)
        return 3

    import database
    from bridge import EstokeBridge

    db = database.Database()
    workers = BackgroundWorkers(db)
    bridge = EstokeBridge(db=db, on_session_started=workers.start)

    tema = bridge.get_theme().get("data") or "light"
    icone = paths.get_resource_path("icon", "icon-cubo.ico")

    window = webview.create_window(
        "Estoke ao Cubo",
        url=index,
        js_api=bridge,
        width=1440,
        height=900,
        min_size=(1280, 720),
        background_color=_FUNDO_POR_TEMA.get(tema, "#F0F0F5"),
        text_select=True,
    )
    bridge._bind_window(window)

    debug = os.environ.get("ESTOKE_WEB_DEBUG") == "1"
    try:
        webview.start(
            http_server=True,
            debug=debug,
            private_mode=True,
            icon=icone if os.path.isfile(icone) else None,
        )
    finally:
        workers.stop()
        bridge._shutdown()
        try:
            db.close()
        except Exception:
            logger.debug("Falha ao fechar banco.", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
