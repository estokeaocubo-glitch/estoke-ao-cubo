"""
Manipulador global de crashes e erros não tratados para aplicação desktop.
Captura exceções na thread principal, threads secundárias e callbacks Tkinter,
além de habilitar faulthandler para crashes nativos, gravando JSON estruturado
em %LOCALAPPDATA%\\EstokeAoCubo\\logs e exibindo diálogo com "Copiar diagnóstico".
"""
import datetime
import faulthandler
import json
import logging
import os
import platform
import sys
import threading
import time
import traceback
import uuid

logger = logging.getLogger("EstokeAoCubo.crash")

APP_VERSION = "4.6.1"
_fault_log_handle = None
_installed = False
_crash_dialog_open = False
_last_dialog_ts = 0.0


def is_benign_tk_callback_error(exc_type, exc_value) -> bool:
    """Identifica erros benignos de teardown/ícone do Tkinter que não devem abrir cascata de diagnóstico."""
    type_name = getattr(exc_type, "__name__", str(exc_type or ""))
    if "TclError" not in type_name:
        return False
    msg = str(exc_value or "").lower()
    if any(
        pat in msg
        for pat in (
            "bad window path name",
            "application has been destroyed",
            "can't invoke",
            "invalid command name",
            "wm_iconbitmap",
        )
    ):
        return True
    if "bitmap" in msg and "not defined" in msg:
        return True
    return False


def get_logs_dir(log_dir=None) -> str:
    """Retorna e garante o diretório de logs em ESTOKE_DATA_DIR ou %LOCALAPPDATA%\\EstokeAoCubo\\logs."""
    if log_dir:
        target = log_dir
    else:
        import paths
        return paths.get_app_data_path("logs")
    os.makedirs(target, exist_ok=True)
    return target


def enable_faulthandler(log_dir=None) -> str:
    """Ativa faulthandler nativo para registrar segfaults e deadlocks em fault.log."""
    global _fault_log_handle
    logs_path = get_logs_dir(log_dir)
    fault_path = os.path.join(logs_path, "fault.log")
    try:
        if _fault_log_handle is None or _fault_log_handle.closed:
            _fault_log_handle = open(fault_path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_fault_log_handle, all_threads=True)
    except Exception as exc:
        logger.warning("Não foi possível ativar faulthandler: %s", exc)
    return fault_path


def record_crash(exc_type, exc_value, exc_tb, context="main", thread_name=None, log_dir=None) -> dict:
    """
    Grava relatório de crash em JSON estruturado e retorna os dados + texto formatado.
    Ignora KeyboardInterrupt e SystemExit.
    """
    if exc_type is not None and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        return {}

    now = datetime.datetime.now()
    crash_id = uuid.uuid4().hex[:8].upper()
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
    tb_text = "".join(tb_lines)

    crash_info = {
        "crash_id": crash_id,
        "timestamp": now.isoformat(),
        "version": APP_VERSION,
        "context": context,
        "thread": thread_name or threading.current_thread().name,
        "python": sys.version,
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "exc_type": getattr(exc_type, "__name__", str(exc_type)),
        "exc_message": str(exc_value),
        "traceback": tb_text,
    }

    logs_path = get_logs_dir(log_dir)
    filename = f"crash_{now.strftime('%Y%m%d_%H%M%S')}_{crash_id}.json"
    crash_file = os.path.join(logs_path, filename)
    crash_info["crash_file"] = crash_file

    diagnostic_text = (
        f"=== ESTOKE AO CUBO - RELATÓRIO DE ERRO #{crash_id} ===\n"
        f"Data/Hora: {crash_info['timestamp']}\n"
        f"Versão: {APP_VERSION} | Frozen: {crash_info['frozen']}\n"
        f"SO: {crash_info['platform']}\n"
        f"Python: {sys.version.splitlines()[0]}\n"
        f"Contexto: {context} (Thread: {crash_info['thread']})\n"
        f"Erro: {crash_info['exc_type']}: {crash_info['exc_message']}\n"
        f"Arquivo de Log: {crash_file}\n\n"
        f"--- Traceback ---\n{tb_text}"
    )
    crash_info["diagnostic_text"] = diagnostic_text

    try:
        with open(crash_file, "w", encoding="utf-8") as f:
            json.dump(crash_info, f, ensure_ascii=False, indent=2)
    except Exception as write_err:
        logger.error("Falha ao gravar arquivo de crash JSON: %s", write_err)

    logger.critical(
        "Erro não tratado [%s] em %s (%s): %s\n%s",
        crash_id,
        context,
        crash_info["thread"],
        exc_value,
        tb_text,
    )
    return crash_info


def show_crash_dialog(crash_info: dict, parent=None) -> None:
    """Exibe diálogo amigável em Tkinter puro (imune a falhas do CustomTkinter) com singleton e cooldown."""
    global _crash_dialog_open, _last_dialog_ts
    if not crash_info:
        return
    if _crash_dialog_open or (time.monotonic() - _last_dialog_ts) < 2.0:
        return

    _crash_dialog_open = True
    diagnostic_text = crash_info.get("diagnostic_text", "")
    crash_id = crash_info.get("crash_id", "DESCONHECIDO")
    crash_file = crash_info.get("crash_file", "")

    try:
        import tkinter as tk

        root = parent
        created_hidden_root = False
        if root is None:
            root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()
            created_hidden_root = True

        win = tk.Toplevel(root)
        win.title("Erro Inesperado — Diagnóstico do Sistema")
        win.geometry("620x460")
        win.resizable(True, True)
        win.configure(bg="#F5F7FA")
        try:
            win.attributes("-topmost", True)
            win.grab_set()
        except Exception:
            pass

        def _release_lock():
            global _crash_dialog_open, _last_dialog_ts
            _crash_dialog_open = False
            _last_dialog_ts = time.monotonic()

        def _fechar():
            _release_lock()
            try:
                win.destroy()
            except Exception:
                pass
            if created_hidden_root:
                try:
                    root.destroy()
                except Exception:
                    pass

        def _on_destroy(event):
            if getattr(event, "widget", None) is win:
                _release_lock()

        win.bind("<Destroy>", _on_destroy)

        tk.Label(
            win,
            text=f"Ops! Ocorreu um erro inesperado (#{crash_id})",
            font=("Segoe UI", 13, "bold"),
            fg="#C0392B",
            bg="#F5F7FA",
            anchor="w",
        ).pack(padx=20, pady=(16, 4), fill="x")

        tk.Label(
            win,
            text=(
                "Não se preocupe: suas alterações salvas no banco de dados estão protegidas.\n"
                f"Log salvo em: {crash_file}"
            ),
            font=("Segoe UI", 9),
            justify="left",
            fg="#333333",
            bg="#F5F7FA",
            anchor="w",
        ).pack(padx=20, pady=(0, 10), fill="x")

        text_frame = tk.Frame(win, bg="#F5F7FA")
        text_frame.pack(padx=20, pady=(0, 12), fill="both", expand=True)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")

        textbox = tk.Text(
            text_frame,
            font=("Consolas", 9),
            wrap="word",
            yscrollcommand=scrollbar.set,
            bg="#FFFFFF",
            fg="#1F2937",
            relief="solid",
            bd=1,
        )
        textbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=textbox.yview)
        textbox.insert("1.0", diagnostic_text)
        textbox.configure(state="disabled")

        btn_row = tk.Frame(win, bg="#F5F7FA")
        btn_row.pack(fill="x", padx=20, pady=(0, 16))

        btn_copy = tk.Button(
            btn_row,
            text="Copiar Diagnóstico",
            bg="#0A4174",
            fg="white",
            activebackground="#062F56",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=14,
            pady=8,
            cursor="hand2",
        )

        def _copiar():
            try:
                win.clipboard_clear()
                win.clipboard_append(diagnostic_text)
                win.update()
                btn_copy.configure(text="Diagnóstico Copiado!", bg="#28a745")
            except Exception:
                pass

        btn_copy.configure(command=_copiar)
        btn_copy.pack(side="left", fill="x", expand=True, padx=(0, 8))

        tk.Button(
            btn_row,
            text="Fechar",
            bg="#D0D5DD",
            fg="#1F2937",
            activebackground="#B8C0CC",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=18,
            pady=8,
            cursor="hand2",
            command=_fechar,
        ).pack(side="right")

        win.protocol("WM_DELETE_WINDOW", _fechar)
        if created_hidden_root:
            win.wait_window()
    except Exception:
        _crash_dialog_open = False
        _last_dialog_ts = time.monotonic()
        try:
            from tkinter import messagebox
            messagebox.showerror(
                f"Erro Inesperado (#{crash_id})",
                f"Ocorreu uma falha inesperada.\nLog salvo em:\n{crash_file}\n\n"
                f"{crash_info.get('exc_type')}: {crash_info.get('exc_message')}",
            )
        except Exception:
            pass


def install_crash_handlers(app=None, show_ui=True, log_dir=None) -> None:
    """
    Instala sys.excepthook, threading.excepthook, faulthandler e report_callback_exception.
    """
    global _installed
    enable_faulthandler(log_dir=log_dir)

    def _dispatch_ui(crash_info):
        if not show_ui or not crash_info:
            return
        target_app = app
        if target_app is not None:
            try:
                target_app.after(0, lambda ci=crash_info: show_crash_dialog(ci, parent=target_app))
                return
            except Exception:
                pass
        show_crash_dialog(crash_info, parent=None)

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        info = record_crash(exc_type, exc_value, exc_tb, context="sys.excepthook", log_dir=log_dir)
        _dispatch_ui(info)

    def _thread_hook(args):
        if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            return
        t_name = args.thread.name if args.thread else "unknown"
        info = record_crash(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            context="threading.excepthook",
            thread_name=t_name,
            log_dir=log_dir,
        )
        _dispatch_ui(info)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook

    if app is not None:
        def _tk_callback_exc(exc_type, exc_value, exc_tb):
            if is_benign_tk_callback_error(exc_type, exc_value):
                logger.debug("Ignorando TclError benigno de callback Tkinter: %s", exc_value)
                return
            info = record_crash(
                exc_type, exc_value, exc_tb,
                context="tk.report_callback_exception",
                log_dir=log_dir,
            )
            if show_ui and info:
                show_crash_dialog(info, parent=app)

        app.report_callback_exception = _tk_callback_exc

    _installed = True
