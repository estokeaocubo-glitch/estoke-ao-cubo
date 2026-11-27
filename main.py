import sys
import os

# 1 linha para explicar o motivo: Adiciona o diretório do script ao path para evitar erros de importação ao rodar fora do Cwd.
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# Interface v2.0 (PyWebView) opt-in via --ui=web; o padrão segue sendo a UI legada (CustomTkinter).
if __name__ == '__main__' and "--ui=web" in sys.argv[1:] and "--self-test" not in sys.argv[1:]:
    from app_desktop import main as _main_web
    sys.exit(_main_web(sys.argv[1:]))

import customtkinter as ctk
from customtkinter import CTkButton
from tkinter import messagebox, filedialog, ttk
import database
import ai_module
import invoice_generator
from PIL import Image, ImageTk
import os
import shutil
import datetime
import sys
import logging
import threading
import ctypes
import subprocess
import pandas as pd
from notifications import NotificationManager
import label_generator
# Importação opcional para barcode_scanner (pode falhar em alguns ambientes)
try:
    import barcode_scanner
    BARCODE_SCANNER_AVAILABLE = True
except ImportError:
    BARCODE_SCANNER_AVAILABLE = False
    logger_temp = None
import api_server

# --- Ícone na barra de tarefas do Windows ---
# Define um AppUserModelID exclusivo para que o Windows exiba o ícone nítido do app
# e descarte qualquer cache desatualizado/pixelizado da barra de tarefas.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('EstokeAoCubo.App.4.6')
except Exception:
    pass

# configuração e impressão
import config
from config import get_printer_config, set_printer_config, get_business_type, set_business_type, verify_admin, get_admin_credentials, set_admin_credentials, admin_needs_setup
from printer import get_printer
import json
from logging.handlers import RotatingFileHandler
import crash_handler
import paths

# --- Configuração de Logging com Rotação Automática (Máx 5MB x 3 arquivos) ---
_log_dir = paths.get_app_data_path("logs")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        RotatingFileHandler(
            os.path.join(_log_dir, 'app.log'),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding='utf-8'
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('EstokeAoCubo')
crash_handler.install_crash_handlers(show_ui=True)

# --- Monkeypatch para corrigir bug do CTkButton.destroy ---
# O erro ocorre porque _font às vezes não existe quando destroy é chamado.
_original_destroy = CTkButton.destroy

def _safe_destroy(self):
    if not hasattr(self, '_font'):
        self._font = None
    try:
        _original_destroy(self)
    except (AttributeError, TypeError, Exception):
        # Se falhar mesmo assim (ex: widget já destruído), ignorar
        pass

CTkButton.destroy = _safe_destroy
# ----------------------------------------------------------

def get_base_path():
    """Retorna o caminho base de recursos somente-leitura (compatível com PyInstaller _MEIPASS)."""
    return paths.get_resource_path()

ctk.set_appearance_mode("Light")  # Modes: "System" (standard), "Dark", "Light"
ctk.set_default_color_theme("blue")  # Themes: "blue" (standard), "green", "dark-blue"

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Estoke ao Cubo - Gestão Inteligente")
        try:
            screen_h = self.winfo_screenheight()
            win_h = min(900, max(680, screen_h - 80))
        except Exception:
            win_h = 800
        self.geometry(f"1100x{win_h}")
        self.minsize(800, 500)
        self.configure(fg_color=("#F0F0F5", "#191919")) # Fundo levemente lavanda/cinza para contraste com botões glass
        self._thumbnail_cache = {}
        self._last_dashboard_render_ts = 0.0

        # Adicionar ícone da aplicação em alta definição (titlebar + taskbar + Alt+Tab)
        self._icon_path_ico = None
        self._icon_photos = []
        self._hicon_big = None
        self._hicon_small = None
        try:
            base_path = get_base_path()
            icon_path_ico = os.path.join(base_path, "icon", "icon-cubo.ico")
            icon_path_png = os.path.join(base_path, "icon", "icon-cubo.png")

            # No Windows, NÃO chamar iconphoto() pois o Tk converte PhotoImage para 16/32px
            # e sobrescreve WM_SETICON com bitmap pixelizado na taskbar HiDPI.
            # Usar .ico multi-resolução + Win32 LoadImageW(256x256) / WM_SETICON diretamente.
            if os.name == "nt":
                if os.path.exists(icon_path_ico):
                    self._icon_path_ico = icon_path_ico
                    try:
                        self.iconbitmap(default=icon_path_ico)
                        self.iconbitmap(icon_path_ico)
                    except Exception:
                        pass
            else:
                if os.path.exists(icon_path_png):
                    master_png = Image.open(icon_path_png)
                    for sz in (256, 128, 64, 48, 32, 24, 16):
                        resized = master_png.resize((sz, sz), Image.Resampling.LANCZOS)
                        self._icon_photos.append(ImageTk.PhotoImage(resized))
                    if self._icon_photos:
                        self.iconphoto(True, *self._icon_photos)

            # Reaplica após o timer de 200ms do CustomTkinter para garantir ícone HD definitivo
            self.after(50, self._forcar_icone)
            self.after(250, self._forcar_icone)
            self.after(600, self._forcar_icone)
            self.after(1500, self._forcar_icone)
        except Exception as e:
            logger.warning("Erro ao carregar ícone: %s", e)

        # Configuração do Grid
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=0)  # Topbar não expande
        self.grid_rowconfigure(1, weight=1)  # Main frame expande

        # Tela cheia (F11 / Esc)
        self._fullscreen = False
        self.bind_all("<F11>", self._toggle_fullscreen)
        self.bind_all("<Escape>", self._exit_fullscreen)

        # Inicializar Banco de Dados e IA
        self.db = database.Database()
        self.ai = ai_module.ExpensePredictor(self.db)

        # Inicializar camada de anotações (recebe Database para resistir a restaurar_backup)
        from services.dreams_service import SonhosService
        self.sonhos_service = SonhosService(self.db)

        # Sessão do usuário logado (None = sem login multi-usuário)
        self.current_user = None
        self._ultimo_cupom = None
        self._pending_installer_path = None

        # Fila Outbox offline, agendador de rotinas (Onda 3) e serviço fiscal NFC-e (Onda 4)
        from services.outbox_service import OutboxService
        from services.scheduler import RoutineScheduler
        from services.fiscal_service import FiscalService
        self.outbox_service = OutboxService(self.db)
        self.routine_scheduler = RoutineScheduler(self.db, self.outbox_service)
        self.fiscal_service = FiscalService(self.db, outbox_service=self.outbox_service)
        self.db._fiscal_service = self.fiscal_service

        # Gerenciador de notificações toast
        self.notifier = NotificationManager(self)

        # API REST para app mobile
        self.api_server = None

        # Singleton tracking para diálogos críticos
        self._pdv_dialog = None

        # Verificar tipo de negócio (primeiro acesso)
        self.business_type = get_business_type()
        
        if self.business_type is None:
            self._show_first_access_screen()
        else:
            self._show_login_screen()

    def check_trial_limits(self, type_to_add, parent_win=None):
        """
        Verifica se os limites da versão trial foram atingidos.
        Retorna True se o limite foi atingido (bloqueando a ação) e exibe o modal de upgrade.
        Retorna False se a ação for permitida.
        """
        if not self.db.is_trial_mode():
            return False

        if type_to_add == 'produto':
            if self.db.contar_produtos() >= 10:
                self.show_trial_limit_modal(parent_win)
                return True
        elif type_to_add == 'transacao':
            if self.db.contar_transacoes() >= 5:
                self.show_trial_limit_modal(parent_win)
                return True
        return False

    def show_trial_limit_modal(self, parent_win=None):
        """
        Exibe um modal de CustomTkinter premium e com design sofisticado (dark/glassmorphism)
        informando que o limite do trial foi atingido, com um botão que redireciona para o WhatsApp de vendas.
        """
        import webbrowser
        
        # Determinar a janela pai para centralização
        parent = parent_win if parent_win else self
        
        # Criar janela de nível superior (Toplevel)
        modal = ctk.CTkToplevel(parent)
        modal.title("Limite de Teste Atingido")
        modal.geometry("520x340")
        modal.resizable(False, False)
        
        # Traz o modal para a frente e o torna modal
        modal.transient(parent)
        modal.grab_set()
        modal.focus_set()
        
        # Centralizar a janela em relação ao pai
        x = parent.winfo_x() + (parent.winfo_width() // 2) - 260
        y = parent.winfo_y() + (parent.winfo_height() // 2) - 170
        modal.geometry(f"+{x}+{y}")
        
        # Forçar o ícone
        if hasattr(self, '_icon_path_ico') and self._icon_path_ico:
            try:
                modal.iconbitmap(self._icon_path_ico)
            except Exception:
                pass

        # Container Principal com visual premium (Dark Mode estético)
        main_frame = ctk.CTkFrame(modal, corner_radius=18, fg_color=("#1A1A1E", "#151518"), border_width=2, border_color="#33333C")
        main_frame.pack(fill="both", expand=True, padx=12, pady=12)
        
        # Título
        title_label = ctk.CTkLabel(
            main_frame,
            text="Limite do Trial Atingido! 🚀",
            font=("Segoe UI", 20, "bold"),
            text_color="#FFA500"
        )
        title_label.pack(pady=(25, 15))
        
        # Mensagem persuasiva
        msg_text = (
            "Você atingiu o limite da versão de teste (máximo de 10 produtos e 5 movimentações).\n\n"
            "Assine a versão completa por apenas R$ 60/mês para liberar cadastros e "
            "vendas ilimitados e desfrutar de toda a potência do Estoke ao Cubo!"
        )
        msg_label = ctk.CTkLabel(
            main_frame,
            text=msg_text,
            font=("Segoe UI", 13),
            text_color="#E0E0E6",
            justify="center",
            wraplength=440
        )
        msg_label.pack(pady=(0, 20), padx=25)
        
        # Frame de Ações (Botões)
        actions_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        actions_frame.pack(fill="x", side="bottom", pady=(0, 25), padx=25)
        
        def open_whatsapp():
            phone = "5524992992130"
            message = "Olá! Gostaria de assinar a versão completa do Estoke ao Cubo por R$ 60/mês. Atingi o limite do trial."
            import urllib.parse
            url = f"https://wa.me/{phone}?text={urllib.parse.quote(message)}"
            webbrowser.open(url)
            modal.grab_release()
            modal.destroy()

        def close_modal():
            modal.grab_release()
            modal.destroy()
            
        # Botão Whatsapp (Verde premium)
        whatsapp_btn = ctk.CTkButton(
            actions_frame,
            text="💬 Assinar Versão Completa (R$ 60/mês)",
            font=("Segoe UI", 13, "bold"),
            fg_color="#128C7E",
            hover_color="#075E54",
            text_color="#FFFFFF",
            corner_radius=10,
            height=40,
            command=open_whatsapp
        )
        whatsapp_btn.pack(fill="x", pady=(0, 10))
        
        # Botão Fechar / Continuar depois
        close_btn = ctk.CTkButton(
            actions_frame,
            text="Voltar",
            font=("Segoe UI", 12),
            fg_color="transparent",
            hover_color=("#E5E5E5", "#2D2D35"),
            text_color="#B0B0B8",
            corner_radius=10,
            height=30,
            command=close_modal
        )
        close_btn.pack(fill="x")

    def _get_os_config(self):
        """Retorna status, cores, ícones e rótulos de OS adaptados ao tipo de negócio."""
        from services.nicho_service import NichoService
        return NichoService().get_os_config(getattr(self, 'business_type', None))

    def _show_first_access_screen(self):
        """Tela de primeiro acesso — escolha do tipo de negócio."""
        self._setup_frame = ctk.CTkFrame(self, fg_color=("#F0F0F5", "#191919"))
        self._setup_frame.grid(row=0, column=0, rowspan=4, columnspan=2, sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        inner = ctk.CTkFrame(self._setup_frame, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        # Logo
        try:
            base_path = get_base_path()
            icon_path = os.path.join(base_path, "icon", "icon-cubo.png")
            if os.path.exists(icon_path):
                pil_img = Image.open(icon_path)
                self._setup_logo = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(100, 100))
                ctk.CTkLabel(inner, image=self._setup_logo, text="").pack(pady=(0, 10))
        except Exception:
            pass

        ctk.CTkLabel(inner, text="Bem-vindo ao Estoke ao Cubo!",
                     font=("Aileron", 24, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 5))
        ctk.CTkLabel(inner, text="Selecione o tipo do seu negócio para personalizar a experiência:",
                     font=("Aileron", 13), text_color=("gray40", "gray70")).pack(pady=(0, 25))

        # Grid de cards com scroll
        cards_frame = ctk.CTkScrollableFrame(inner, width=880, height=360, fg_color="transparent")
        cards_frame.pack(pady=(0, 10))

        from services.nicho_service import NichoService
        business_options = NichoService().listar_nichos()

        select_buttons = []

        def _select_business(btype):
            set_business_type(btype)
            self.business_type = btype
            self._setup_frame.destroy()
            self._show_login_screen()

        for i, opt in enumerate(business_options):
            row_idx = i // 4
            col_idx = i % 4
            card = ctk.CTkFrame(cards_frame, width=205, height=165, fg_color=("white", "#2B2B2B"),
                               corner_radius=14, border_width=2, border_color=(opt["color"], opt["color"]))
            card.grid(row=row_idx, column=col_idx, padx=6, pady=6)
            card.grid_propagate(False)

            content = ctk.CTkFrame(card, fg_color="transparent")
            content.place(relx=0.5, rely=0.5, anchor="center")

            ctk.CTkLabel(content, text=opt["icon"], font=("Segoe UI Emoji", 28)).pack(pady=(0, 2))
            ctk.CTkLabel(content, text=opt["title"], font=("Aileron", 12, "bold"),
                        text_color=("#001D39", "white")).pack(pady=(0, 2))
            ctk.CTkLabel(content, text=opt["desc"], font=("Aileron", 9),
                        text_color=("gray40", "gray70"), justify="center").pack(pady=(0, 6))
            btn = ctk.CTkButton(content, text="Selecionar", height=28, width=120, corner_radius=16,
                         fg_color=opt["color"], hover_color="#49769F",
                         text_color="white", font=("Aileron", 10, "bold"),
                         command=lambda k=opt["key"]: _select_business(k),
                         state="disabled")
            btn.pack()
            select_buttons.append(btn)

        # --- Aceite dos Termos de Uso ---
        terms_frame = ctk.CTkFrame(inner, fg_color="transparent")
        terms_frame.pack(pady=(20, 0))

        terms_var = ctk.BooleanVar(value=False)

        def _toggle_terms():
            state = "normal" if terms_var.get() else "disabled"
            for btn in select_buttons:
                btn.configure(state=state)

        ctk.CTkCheckBox(
            terms_frame,
            text="Li e concordo com os Termos de Uso e Licença de Software",
            variable=terms_var,
            command=_toggle_terms,
            font=("Aileron", 12),
            text_color=("#001D39", "white"),
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            border_color=("#49769F", "#7BBDE8"),
            fg_color="#49769F",
            hover_color="#3A6A8F",
        ).pack(side="left", padx=(0, 8))

        def _open_terms():
            try:
                terms_path = paths.get_resource_path("TERMOS_DE_USO.txt")
                if not os.path.exists(terms_path):
                    terms_path = os.path.join(paths.get_install_dir(), "TERMOS_DE_USO.txt")
                if os.path.exists(terms_path):
                    os.startfile(terms_path)
                else:
                    messagebox.showinfo("Termos de Uso", "O arquivo TERMOS_DE_USO.txt não foi encontrado.\nConsulte os termos no GUIA_DO_USUARIO.txt (seção 6).")
            except Exception:
                messagebox.showinfo("Termos de Uso", "Não foi possível abrir o arquivo.\nConsulte os termos no GUIA_DO_USUARIO.txt (seção 6).")

        ctk.CTkButton(
            terms_frame,
            text="Ler Termos",
            width=100,
            height=28,
            corner_radius=15,
            fg_color="transparent",
            hover_color=("#E0E0E0", "#333333"),
            text_color=("#49769F", "#7BBDE8"),
            border_width=1,
            border_color=("#49769F", "#7BBDE8"),
            font=("Aileron", 11),
            command=_open_terms,
        ).pack(side="left")

        # Confirmação ao fechar
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    # --- Delegação de autenticação e RBAC para AuthService ---
    def _get_auth_service(self):
        """Retorna instância lazy do AuthService vinculada ao banco atual (seguro para stubs Tk)."""
        d = self.__dict__
        svc = d.get("_auth_service")
        db_ref = d.get("db")
        if svc is None or svc.db is not db_ref:
            from services.auth_service import AuthService
            svc = AuthService(db_ref)
            d["_auth_service"] = svc
        return svc

    def _tem_permissao(self, acao_btn):
        """Verifica se o usuário logado tem permissão para a ação (fail-closed)."""
        return self._get_auth_service().tem_permissao(self.__dict__.get("current_user"), acao_btn)

    def report_callback_exception(self, exc, val, tb):
        """Captura exceções em callbacks do Tkinter/CustomTkinter com log e diálogo de diagnóstico."""
        info = crash_handler.record_crash(exc, val, tb, context="tk.report_callback_exception")
        crash_handler.show_crash_dialog(info, parent=self)

    def _exigir_supervisor(self, acao: str, parent=None, exigir_motivo: bool = False, exigir_senha_sempre: bool = False):
        """Solicita autorização de perfil Admin/Gerente para ações sensíveis (auditoria + anti-fraude)."""
        from views.supervisor_dialog import solicitar_autorizacao_supervisor
        ok, info = solicitar_autorizacao_supervisor(
            self,
            acao=acao,
            exigir_motivo=exigir_motivo,
            parent=parent or self,
            exigir_senha_sempre=exigir_senha_sempre,
        )
        return (ok, info) if exigir_motivo else ok

    def reimprimir_ultimo_cupom(self, parent=None):
        """Reimprime o último cupom emitido na sessão atual ou recuperado do cache de recibos (F11)."""
        from services.impressao_service import ImpressaoService

        target_parent = parent or self
        ultimo = getattr(self, "_ultimo_cupom", None)
        svc = ImpressaoService(self.db, printer_factory=get_printer)
        try:
            res = svc.reimprimir_ultimo_cupom(ultimo_cupom=ultimo)
            if res["origem"] == "sessao":
                try:
                    self.notifier.notify("Reimpressão", res["mensagem"], "info")
                except Exception:
                    messagebox.showinfo("Reimpressão", res["mensagem"], parent=target_parent)
            else:
                messagebox.showinfo("Reimpressão", res["mensagem"], parent=target_parent)
            return True
        except LookupError as exc:
            messagebox.showinfo("Reimpressão", str(exc), parent=target_parent)
            return False
        except Exception as exc:
            logger.warning("Erro ao reimprimir último cupom: %s", exc)
            messagebox.showerror("Erro de Impressão", f"Não foi possível reimprimir: {exc}", parent=target_parent)
            return False

    def _show_login_screen(self):
        """Tela de login — autenticação do usuário."""
        self._login_frame = ctk.CTkFrame(self, fg_color=("#F0F0F5", "#191919"))
        self._login_frame.grid(row=0, column=0, rowspan=4, columnspan=2, sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        inner = ctk.CTkFrame(self._login_frame, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        # Logo
        try:
            base_path = get_base_path()
            icon_path = os.path.join(base_path, "icon", "icon-cubo.png")
            if os.path.exists(icon_path):
                pil_img = Image.open(icon_path)
                self._login_logo = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(100, 100))
                ctk.CTkLabel(inner, image=self._login_logo, text="").pack(pady=(0, 10))
        except Exception:
            pass

        ctk.CTkLabel(inner, text="Estoke ao Cubo",
                     font=("Aileron", 22, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 5))
        ctk.CTkLabel(inner, text="Faça login para continuar",
                     font=("Aileron", 13), text_color=("gray40", "gray70")).pack(pady=(0, 20))

        login_card = ctk.CTkFrame(inner, fg_color=("white", "#2B2B2B"), corner_radius=15)
        login_card.pack(padx=20, pady=10)

        ctk.CTkLabel(login_card, text="Usuário:", font=("Aileron", 12, "bold"),
                     text_color=("#001D39", "white")).pack(anchor="w", padx=30, pady=(20, 2))
        entry_user = ctk.CTkEntry(login_card, height=38, placeholder_text="Login do usuário", width=290)
        entry_user.pack(padx=30, pady=(2, 10))

        ctk.CTkLabel(login_card, text="Senha:", font=("Aileron", 12, "bold"),
                     text_color=("#001D39", "white")).pack(anchor="w", padx=30, pady=(0, 2))
        entry_pass = ctk.CTkEntry(login_card, height=38, placeholder_text="Senha", show="*", width=290)
        entry_pass.pack(padx=30, pady=(2, 10))

        lbl_erro = ctk.CTkLabel(login_card, text="", font=("Aileron", 11), text_color="#FF6B6B")
        lbl_erro.pack()

        def _fazer_login():
            login_val = entry_user.get().strip()
            senha_val = entry_pass.get()
            auth = self._get_auth_service()
            try:
                user = auth.autenticar(login_val, senha_val)
            except ValueError as err:
                lbl_erro.configure(text=str(err))
                return
            if user:
                self.current_user = user
                self.db.set_current_user(user)
                self._login_frame.destroy()
                self.grid_columnconfigure(0, weight=0)
                self.grid_columnconfigure(1, weight=1)
                self.grid_rowconfigure(0, weight=0)
                self.grid_rowconfigure(1, weight=1)
                self._build_main_ui()
                if auth.precisa_trocar_senha(user):
                    self.after(300, lambda: messagebox.showwarning(
                        "Segurança — Senha Padrão",
                        "Você está utilizando a senha padrão inicial.\nPor segurança, altere sua senha em Configurações > Usuários.",
                        parent=self
                    ))
            else:
                lbl_erro.configure(text="Usuário ou senha incorretos!")

        ctk.CTkButton(login_card, text="Entrar", command=_fazer_login,
                      fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 13, "bold"), height=40, width=290).pack(padx=30, pady=(10, 5))

        _mostrar_dica = self._get_auth_service().precisa_trocar_senha()
        if _mostrar_dica:
            ctk.CTkLabel(login_card, text="Primeiro acesso? Use: admincentral / admin9438",
                         font=("Aileron", 10), text_color=("gray50", "gray60")).pack(pady=(0, 15))

        entry_user.focus_set()
        entry_pass.bind("<Return>", lambda e: _fazer_login())

        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _build_main_ui(self):
        """Constrói a interface principal após o tipo de negócio ser definido."""
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0, fg_color=("white", "#2B2B2B"))
        self.sidebar_frame.grid(row=0, column=0, rowspan=4, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(1, weight=1)
        self.sidebar_frame.grid_columnconfigure(0, weight=1)

        # Logo com imagem (topo fixo)
        try:
            base_path = get_base_path()
            icon_path = os.path.join(base_path, "icon", "icon-cubo.png")
            
            if os.path.exists(icon_path):
                pil_image = Image.open(icon_path)
                self.logo_image = ctk.CTkImage(light_image=pil_image, 
                                        dark_image=pil_image, 
                                        size=(85, 85))
                
                logo_img_label = ctk.CTkLabel(self.sidebar_frame, image=self.logo_image, text="")
                logo_img_label.grid(row=0, column=0, padx=15, pady=(15, 10))
            else:
                self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Estoke ao Cubo", font=ctk.CTkFont(family="Aileron", size=20, weight="bold"), text_color="#001D39")
                self.logo_label.grid(row=0, column=0, padx=15, pady=(15, 10))
        except Exception as e:
            logger.warning("Erro ao carregar logo: %s", e)
            self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Estoke ao Cubo", font=ctk.CTkFont(family="Aileron", size=20, weight="bold"), text_color="#001D39")
            self.logo_label.grid(row=0, column=0, padx=15, pady=(15, 10))

        # Botões da Sidebar dentro de scrollable frame
        self._sidebar_scroll = ctk.CTkScrollableFrame(self.sidebar_frame, fg_color="transparent",
                                                       scrollbar_button_color=("#CCCCCC", "#555555"))
        self._sidebar_scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._sidebar_scroll.grid_columnconfigure(0, weight=1)

        from services.nicho_service import NichoService
        self.sidebar_buttons = [
            item["texto"] for item in NichoService().get_sidebar_buttons(self.business_type)
        ]
        
        for i, btn_text in enumerate(self.sidebar_buttons):
            btn = ctk.CTkButton(self._sidebar_scroll, text=btn_text, height=44, corner_radius=30,
                                fg_color=("#EBEBF0", "#3A3A3A"), text_color=("#333333", "white"), 
                                hover_color=("#D9D9E0", "#4A4A4A"),
                                border_width=2, border_color=("white", "#555555"),
                                font=ctk.CTkFont(family="Aileron", size=13, weight="bold"),
                                command=lambda t=btn_text: self.sidebar_event(t))
            btn.grid(row=i, column=0, padx=15, pady=5, sticky="ew")

        # Usuário logado + botão sair (fundo fixo da sidebar)
        user_bottom = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        user_bottom.grid(row=2, column=0, padx=10, pady=(5, 2), sticky="ew")
        user_bottom.grid_columnconfigure(0, weight=1)

        if self.current_user:
            perfil_emoji = {"admin": "👑", "gerente": "📊", "vendedor": "🛒", "caixa": "💰"}.get(self.current_user.get("perfil", ""), "👤")
            ctk.CTkLabel(user_bottom, text=f"{perfil_emoji} {self.current_user['nome']}",
                        font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(user_bottom, text="Sair", width=60, height=28, corner_radius=15,
                         fg_color="#E74C3C", hover_color="#C0392B", text_color="white",
                         font=("Aileron", 10, "bold"), command=self._logout).grid(row=1, column=0, pady=(3, 0))

        # Switch de Modo Escuro (fundo fixo da sidebar)
        self.appearance_mode_btn = ctk.CTkButton(self.sidebar_frame, text="☀", width=40, height=40, corner_radius=20,
                                                fg_color=("#EBEBF0", "#3A3A3A"), text_color=("#FDB813", "#FDB813"),
                                                hover_color=("#D9D9E0", "#4A4A4A"),
                                                font=ctk.CTkFont(size=20),
                                                command=self.toggle_appearance_mode)
        self.appearance_mode_btn.grid(row=3, column=0, padx=15, pady=(5, 10))

        # --- Top Bar (Direita Superior) ---
        self.topbar_frame = ctk.CTkFrame(self, height=80, corner_radius=0, fg_color="transparent")
        self.topbar_frame.grid(row=0, column=1, sticky="new", padx=20, pady=10)
        
        self.top_buttons = ["ADICIONAR", "VENDER", "SERVIÇO", "LISTA", "RELATORIO", "DRE"]
        
        # Cores personalizadas para cada botão (Tons de azul)
        button_configs = {
            "ADICIONAR": {"fg": "#AED6F1", "hover": "#85C1E9", "text": "#001D39"}, # Azul muito claro
            "VENDER":    {"fg": "#7BBDE8", "hover": "#49769F", "text": "#001D39"}, # Azul original
            "SERVIÇO":   {"fg": "#6AADDA", "hover": "#5899C4", "text": "#001D39"}, # Azul intermediário
            "LISTA":     {"fg": "#5DADE2", "hover": "#3498DB", "text": "#001D39"}, # Azul médio claro
            "RELATORIO": {"fg": "#2874A6", "hover": "#1B4F72", "text": "white"},   # Azul escuro
            "DRE":       {"fg": "#1A5276", "hover": "#154360", "text": "white"},   # Azul marinho
        }

        for i, btn_text in enumerate(self.top_buttons):
            # Botões superiores com estilo "Glassy"
            # Configuração padrão
            fg_color = ("#EBEBF0", "#3A3A3A")
            text_color = ("#333333", "white")
            hover_color = ("#D9D9E0", "#4A4A4A")
            
            # Aplicar cores personalizadas se existir na configuração
            if btn_text in button_configs:
                config = button_configs[btn_text]
                fg_color = config["fg"]
                text_color = config["text"]
                hover_color = config["hover"]

            btn = ctk.CTkButton(self.topbar_frame, text=btn_text, width=120, height=45, corner_radius=30,
                                fg_color=fg_color, hover_color=hover_color, text_color=text_color,
                                border_width=2, border_color=("white", "#555555"),
                                font=ctk.CTkFont(family="Aileron", size=13, weight="bold"),
                                command=lambda t=btn_text: self.topbar_event(t))
            btn.grid(row=0, column=i, padx=10)

        btn_fullscreen = ctk.CTkButton(self.topbar_frame, text="F11", width=90, height=45, corner_radius=30,
                                       fg_color="#154360", hover_color="#0F3B5A", text_color="white",
                                       border_width=2, border_color=("white", "#555555"),
                                       font=ctk.CTkFont(family="Aileron", size=13, weight="bold"),
                                       command=self._toggle_fullscreen)
        btn_fullscreen.grid(row=0, column=len(self.top_buttons), padx=10)

        # --- Área Principal (Conteúdo) ---
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.grid(row=1, column=1, sticky="nsew", padx=20, pady=20)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(1, weight=1)
        self.main_frame.grid_rowconfigure(0, weight=0)  # Metas não expandem
        self.main_frame.grid_rowconfigure(1, weight=1)  # Gráficos expandem

        # Metas (Checklist)
        self.create_metas_checklist()
        
        # Gráficos
        self.create_charts()

        # --- Backup automático diário ---
        try:
            success, msg = self.db.verificar_backup_diario()
            if success and "criado" not in msg.lower():
                logger.info("Backup diário: %s", msg)
            elif success:
                logger.info("Backup automático criado com sucesso.")
        except Exception as e:
            logger.warning("Erro no backup automático: %s", e)

        # --- Alerta de produtos vencendo (toast não-bloqueante) ---
        try:
            df_vencendo = self.db.verificar_produtos_vencendo(dias_alerta=7)
            if not df_vencendo.empty:
                nomes = ", ".join(df_vencendo['nome'].tolist()[:5])
                total_v = len(df_vencendo)
                msg = f"{total_v} produto(s) com validade próxima ou vencidos: {nomes}"
                if total_v > 5:
                    msg += "..."
                self.after(1500, lambda: self.notifier.notify(
                    "Alerta de Validade", msg, tipo="warning", duracao=8000
                ))
        except Exception as e:
            logger.debug("Erro verificar validade: %s", e)

        # --- Atalhos de Teclado Globais ---
        self.bind_all("<Control-n>", lambda e: self.topbar_event("ADICIONAR"))
        self.bind_all("<Control-Shift-V>", lambda e: self.topbar_event("VENDER"))
        self.bind_all("<Control-e>", lambda e: self.topbar_event("LISTA"))
        self.bind_all("<Control-r>", lambda e: self.topbar_event("RELATORIO"))
        self.bind_all("<Control-g>", lambda e: self.sidebar_event("GASTOS"))
        self.bind_all("<Control-k>", lambda e: self.sidebar_event("KITS"))
        self.bind_all("<F5>", lambda e: self.refresh_dashboard())

        # --- Confirmação ao Fechar ---
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # --- Dashboard Auto-Refresh (a cada 60 segundos) ---
        self._start_auto_refresh()

        # --- Notificações Toast de Alertas (após 3s para não bloquear boot) ---
        self.after(3000, self._exibir_alertas_iniciais)

        # --- Iniciar API REST em background (se disponível) ---
        self._iniciar_api_server()

        # --- Onda 3: Outbox Offline, Agendador de Rotinas e Auto-Update Assinado ---
        try:
            self.outbox_service.start_worker()
        except Exception as e:
            logger.warning("Erro ao iniciar OutboxWorker: %s", e)
        self._start_routine_scheduler()
        self.after(5000, self._check_updates_async)

    def _start_routine_scheduler(self):
        """Agenda verificação leve das rotinas operacionais a cada 60 segundos sem travar a UI."""
        def _tick_loop():
            def _run_bg():
                try:
                    self.routine_scheduler.tick()
                except Exception as exc:
                    logger.debug("Erro no tick do agendador de rotinas: %s", exc)

            threading.Thread(target=_run_bg, name="EstokeSchedulerTick", daemon=True).start()
            if self.winfo_exists():
                self.after(60000, _tick_loop)

        self.after(60000, _tick_loop)

    def _check_updates_async(self):
        """Consulta version.json HTTPS com assinatura Ed25519 em thread na inicialização."""
        def _worker():
            try:
                from services.updater import check_for_updates, download_verified_installer
                info = check_for_updates()
                if not info.get("available"):
                    return

                def _prompt_user():
                    if not self.winfo_exists():
                        return
                    ver = info.get("version", "")
                    notes = info.get("notes") or "Melhorias de segurança, desempenho e estabilidade."
                    aceitou = messagebox.askyesno(
                        "Atualização Disponível",
                        f"Uma nova versão do Estoke Ao Cubo (v{ver}) foi verificada com assinatura digital.\n\n"
                        f"Novidades:\n{notes}\n\n"
                        f"Deseja baixar em segundo plano e instalar automaticamente ao fechar o sistema?",
                        parent=self,
                    )
                    if not aceitou:
                        return

                    def _dl_worker():
                        ok_dl, path_or_err = download_verified_installer(
                            info["url"], info["sha256"], version=ver
                        )
                        def _dl_done():
                            if ok_dl:
                                self._pending_installer_path = path_or_err
                                try:
                                    self.notifier.notify(
                                        "Atualização Pronta",
                                        f"Versão {ver} baixada e verificada! Será instalada ao fechar o app.",
                                        tipo="success",
                                        duracao=7000,
                                    )
                                except Exception:
                                    pass
                            else:
                                logger.warning("Download da atualização falhou: %s", path_or_err)
                        self.after(0, _dl_done)

                    threading.Thread(target=_dl_worker, daemon=True).start()

                self.after(0, _prompt_user)
            except Exception as exc:
                logger.debug("Auto-update check ignorado/offline: %s", exc)

        threading.Thread(target=_worker, name="EstokeUpdaterCheck", daemon=True).start()

    def _enviar_whatsapp_resiliente(self, telefone: str, mensagem: str, metadata: dict | None = None) -> tuple[bool, str]:
        """Enfileira envio de WhatsApp na Outbox offline para nunca travar o caixa."""
        digs = "".join(ch for ch in str(telefone or "") if ch.isdigit())
        if len(digs) < 8:
            return False, "Número de telefone inválido ou incompleto."
        try:
            _, item_id = self.outbox_service.enfileirar(
                "whatsapp",
                {"telefone": telefone, "mensagem": mensagem, "metadata": metadata or {}},
            )
            return True, f"Mensagem enfileirada na Outbox (#{item_id}) para envio assíncrono."
        except Exception as exc:
            logger.warning("Falha ao enfileirar WhatsApp na Outbox: %s", exc)
            return False, str(exc)

    def _aplicar_icone_win32_hd(self, widget=None):
        """Aplica HICON nativo 256x256 (ICON_BIG) e DPI-aware (ICON_SMALL) via Win32 WM_SETICON."""
        if os.name != "nt" or not self._icon_path_ico or not os.path.exists(self._icon_path_ico):
            return
        try:
            target = widget or self
            if not target.winfo_exists():
                return
            user32 = ctypes.windll.user32
            user32.LoadImageW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.GetParent.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p,
            ]
            user32.SendMessageW.restype = ctypes.c_ssize_t

            raw_hwnd = ctypes.c_void_p(target.winfo_id())
            hwnd = user32.GetAncestor(raw_hwnd, 2) or user32.GetParent(raw_hwnd) or raw_hwnd

            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            if not self._hicon_big:
                self._hicon_big = user32.LoadImageW(
                    None, self._icon_path_ico, IMAGE_ICON, 256, 256, LR_LOADFROMFILE
                )
            if not self._hicon_small:
                small_sz = 32
                try:
                    user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
                    user32.GetDpiForWindow.restype = ctypes.c_uint
                    dpi = user32.GetDpiForWindow(hwnd) or 96
                    px = int(round(16 * dpi / 96.0))
                    small_sz = min((16, 24, 32, 48), key=lambda s: abs(s - px))
                except Exception:
                    pass
                self._hicon_small = user32.LoadImageW(
                    None, self._icon_path_ico, IMAGE_ICON, small_sz, small_sz, LR_LOADFROMFILE
                )

            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            ICON_SMALL2 = 2
            if self._hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, self._hicon_big)
            if self._hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, self._hicon_small)
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, self._hicon_small)

            set_cls = getattr(user32, "SetClassLongPtrW", None) or getattr(user32, "SetClassLongW", None)
            if set_cls is not None:
                set_cls.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                set_cls.restype = ctypes.c_void_p
                if self._hicon_big:
                    set_cls(hwnd, -14, self._hicon_big)   # GCLP_HICON
                if self._hicon_small:
                    set_cls(hwnd, -34, self._hicon_small)  # GCLP_HICONSM
        except Exception as exc:
            logger.debug("Falha ao aplicar ícone Win32 HD: %s", exc)

    def _forcar_icone(self):
        """Força a reaplicação do ícone na titlebar e taskbar em alta definição."""
        try:
            if os.name == "nt":
                if self._icon_path_ico and os.path.exists(self._icon_path_ico):
                    self.iconbitmap(self._icon_path_ico)
                self._aplicar_icone_win32_hd(self)
            elif hasattr(self, '_icon_photos') and self._icon_photos:
                self.iconphoto(True, *self._icon_photos)
        except Exception:
            pass

    def _get_cached_thumbnail(self, img_name, size=(40, 40)):
        """Retorna uma CTkImage de miniatura carregada em cache (LRU)."""
        if not img_name:
            return None
        cache_key = (img_name, size)
        if not hasattr(self, '_thumbnail_cache'):
            self._thumbnail_cache = {}
        if cache_key in self._thumbnail_cache:
            val = self._thumbnail_cache.pop(cache_key)
            self._thumbnail_cache[cache_key] = val
            return val
        try:
            img_path = os.path.join(self.db.data_dir, "product_images", img_name)
            if os.path.exists(img_path):
                pil_img = Image.open(img_path)
                pil_img.thumbnail(size)
                if pil_img.mode in ('RGBA', 'LA') or (pil_img.mode == 'P' and 'transparency' in pil_img.info):
                    fundo = Image.new("RGBA", pil_img.size, (255, 255, 255, 255))
                    pil_img = Image.alpha_composite(fundo, pil_img.convert("RGBA")).convert("RGB")
                ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=size)
                if len(self._thumbnail_cache) >= 256:
                    first_key = next(iter(self._thumbnail_cache))
                    self._thumbnail_cache.pop(first_key)
                self._thumbnail_cache[cache_key] = ctk_img
                return ctk_img
        except Exception as e:
            pass
        return None

    def _toggle_fullscreen(self, event=None):
        if getattr(self, "_pdv_dialog", None) is not None:
            try:
                if self._pdv_dialog.winfo_exists():
                    return
            except Exception:
                pass
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)

    def _exit_fullscreen(self, event=None):
        if self._fullscreen:
            self._fullscreen = False
            self.attributes("-fullscreen", False)
    
    def _on_closing(self, confirmar: bool = True):
        """Encerramento controlado (para API, workers, consolida WAL, fecha e criptografa banco)."""
        if confirmar and not messagebox.askyesno("Sair", "Deseja realmente fechar o Estoke ao Cubo?"):
            return
        try:
            if getattr(self, "api_server", None):
                self.api_server.stop()
        except Exception:
            pass
        try:
            sched = getattr(self, "routine_scheduler", None)
            if sched is not None and hasattr(sched, "stop"):
                sched.stop()
        except Exception:
            pass
        try:
            if getattr(self, "outbox_service", None):
                self.outbox_service.stop_worker(timeout=1.5)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        pending_installer = getattr(self, "_pending_installer_path", None)
        if pending_installer:
            try:
                from services.updater import launch_installer_on_exit
                launch_installer_on_exit(pending_installer)
            except Exception as exc:
                logger.warning("Não foi possível disparar instalador na saída: %s", exc)
        self.destroy()

    @staticmethod
    def _safe_float(val, default=0.0):
        """Converte qualquer valor para float de forma segura (None, str, NaN -> default)."""
        if val is None:
            return default
        try:
            result = float(val)
            # Proteger contra NaN e Inf
            import math
            if math.isnan(result) or math.isinf(result):
                return default
            return result
        except (ValueError, TypeError):
            # Tenta limpar string com vírgula (ex: "25,00" -> 25.0)
            if isinstance(val, str):
                try:
                    return float(val.replace(',', '.').strip())
                except (ValueError, TypeError):
                    pass
            return default

    @staticmethod
    def _safe_int(val, default=0):
        """Converte qualquer valor para int de forma segura (None, str -> default)."""
        if val is None:
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def _logout(self):
        """Faz logout e retorna à tela de login."""
        if messagebox.askyesno("Sair da Conta", "Deseja trocar de usuário?"):
            self.current_user = None
            self.db.set_current_user(None)
            self._pdv_dialog = None
            # Destruir todos os widgets da UI principal
            for widget in self.winfo_children():
                widget.destroy()
            self._show_login_screen()
    
    def toggle_appearance_mode(self):
        if ctk.get_appearance_mode() == "Light":
            ctk.set_appearance_mode("Dark")
            self.appearance_mode_btn.configure(text="🌙", text_color="#C2C2C2") # Lua cinza claro
        else:
            ctk.set_appearance_mode("Light")
            self.appearance_mode_btn.configure(text="☀", text_color="#FDB813") # Sol amarelo
        
        # Atualizar gráficos para refletir a mudança de tema
        self.after(200, self.refresh_dashboard)

    def refresh_dashboard(self):
        """Limpa e recarrega os gráficos e metas em tempo real"""
        import matplotlib.pyplot as plt
        self._last_dashboard_render_ts = getattr(self.db, 'last_modified', 0.0)
        # Atualizar metas
        self.refresh_metas_list()
        
        # Fechar figuras matplotlib anteriores para evitar memory leak
        plt.close('all')
        
        # Limpar o frame de gráficos
        for widget in self.charts_frame.winfo_children():
            widget.destroy()
        
        # Recriar os gráficos
        self.create_charts_content()
        self.update_prediction_display()
    
    def create_metas_checklist(self):
        """Cria painel de OS em tempo real e tarefas no dashboard (lado a lado)"""
        # Frame principal (Container para OS e tarefas)
        self.metas_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.metas_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        self.metas_frame.grid_columnconfigure(0, weight=1)
        self.metas_frame.grid_columnconfigure(1, weight=1)
        
        # ===== CARD DE SERVIÇOS / OS (lado esquerdo) =====
        self.metas_card = ctk.CTkFrame(self.metas_frame, fg_color=("white", "#2B2B2B"), corner_radius=20, height=200,
                                        border_width=1, border_color=("#E6E6E6", "#444444"))
        self.metas_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=0)
        self.metas_card.grid_columnconfigure(0, weight=1)
        self.metas_card.grid_rowconfigure(1, weight=1)
        
        # Cabeçalho OS
        os_cfg = self._get_os_config()
        os_header = ctk.CTkFrame(self.metas_card, fg_color="transparent")
        os_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 10))
        os_header.grid_columnconfigure(0, weight=1)
        
        ctk.CTkLabel(os_header, text=os_cfg["title"], font=ctk.CTkFont(family="Aileron", size=14, weight="bold"), text_color=("#001D39", "white")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(os_header, text="+ Novo", width=85, height=28, 
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                    command=self.open_servico_dialog).grid(row=0, column=1, sticky="e", padx=5)
        
        # Frame rolável para OS
        self.metas_scroll_frame = ctk.CTkScrollableFrame(self.metas_card, fg_color="transparent")
        self.metas_scroll_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 15))
        
        # ===== CARD DE TAREFAS (lado direito) =====
        # Card com borda suave
        self.tarefas_card = ctk.CTkFrame(self.metas_frame, fg_color=("white", "#2B2B2B"), corner_radius=20, height=200,
                                        border_width=1, border_color=("#E6E6E6", "#444444"))
        self.tarefas_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=0)
        self.tarefas_card.grid_columnconfigure(0, weight=1)
        self.tarefas_card.grid_rowconfigure(1, weight=1)
        
        # Cabeçalho de tarefas
        tarefas_header = ctk.CTkFrame(self.tarefas_card, fg_color="transparent")
        tarefas_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 10))
        tarefas_header.grid_columnconfigure(0, weight=1)
        
        ctk.CTkLabel(tarefas_header, text="TAREFAS", font=ctk.CTkFont(family="Aileron", size=14, weight="bold"), text_color=("#001D39", "white")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(tarefas_header, text="+ Tarefa", width=85, height=28, 
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                    command=self.open_quick_add_tarefa_dialog).grid(row=0, column=1, sticky="e", padx=5)
        
        # Frame rolável para tarefas
        self.tarefas_scroll_frame = ctk.CTkScrollableFrame(self.tarefas_card, fg_color="transparent")
        self.tarefas_scroll_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 15))
        
        self.refresh_metas_list()
    
    def refresh_metas_list(self):
        """Atualiza a lista de serviços (OS) e tarefas no checklist"""
        # Limpar OS antigas
        for widget in self.metas_scroll_frame.winfo_children():
            widget.destroy()
        
        # Limpar tarefas antigas
        for widget in self.tarefas_scroll_frame.winfo_children():
            widget.destroy()
        
        # ===== SERVIÇOS / ORDENS DE SERVIÇO =====
        os_cfg = self._get_os_config()
        status_os_all = os_cfg["statuses"]
        status_colors = os_cfg["colors"]
        status_icons = os_cfg["icons"]

        try:
            df_servicos = self.db.listar_servicos(dias=90)
        except Exception:
            df_servicos = None

        if df_servicos is None or df_servicos.empty:
            ctk.CTkLabel(self.metas_scroll_frame, text="Nenhum serviço recente.",
                        font=ctk.CTkFont(family="Aileron", size=10), text_color="gray").pack(pady=10)
        else:
            # Contagem por status
            ativos = df_servicos[~df_servicos.get('status_os', pd.Series(dtype=str)).isin(["Concluído", "Entregue", ""])]
            total_ativos = len(ativos)

            # Resumo compacto
            resumo_frame = ctk.CTkFrame(self.metas_scroll_frame, fg_color="transparent")
            resumo_frame.pack(fill="x", padx=5, pady=(0, 8))
            ctk.CTkLabel(resumo_frame, text=f"{total_ativos} OS ativas",
                        font=ctk.CTkFont(family="Aileron", size=9, weight="bold"),
                        text_color=("#28a745", "#6BCB77") if total_ativos == 0 else ("#E67E22", "#F39C12")).pack(side="left")

            # Listar OS ativas (não concluídas/entregues), mais recentes primeiro
            if not ativos.empty:
                for _, srv in ativos.head(8).iterrows():
                    st = srv.get('status_os', 'Recebido')
                    cor = status_colors.get(st, "#888")
                    icon = status_icons.get(st, "🔧")
                    equip = srv.get('equipamento', '') or ''
                    desc = srv.get('descricao', '') or ''
                    cliente = srv.get('cliente', '') or ''
                    display = equip if equip else desc[:40]

                    item_f = ctk.CTkFrame(self.metas_scroll_frame, fg_color=("white", "#3A3A3A"),
                                         corner_radius=10, border_width=2, border_color=cor)
                    item_f.pack(fill="x", padx=5, pady=3)

                    content_f = ctk.CTkFrame(item_f, fg_color="transparent")
                    content_f.pack(fill="x", padx=10, pady=8, side="left", expand=True)

                    # Ícone
                    ctk.CTkLabel(content_f, text=icon, font=ctk.CTkFont(size=16)).pack(side="left", padx=(0, 8))

                    # Texto
                    txt_f = ctk.CTkFrame(content_f, fg_color="transparent")
                    txt_f.pack(side="left", fill="both", expand=True)
                    ctk.CTkLabel(txt_f, text=display, font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                                text_color=("#10367D", "white"), wraplength=200).pack(anchor="w")
                    if cliente:
                        ctk.CTkLabel(txt_f, text=cliente, font=ctk.CTkFont(family="Aileron", size=8),
                                    text_color=("gray", "#AAA")).pack(anchor="w")

                    # Badge de status
                    ctk.CTkLabel(item_f, text=f" {st} ", font=ctk.CTkFont(family="Aileron", size=9, weight="bold"),
                                text_color="white", fg_color=cor, corner_radius=6).pack(side="right", padx=10, pady=8)

                    # Botão avançar
                    sid = srv.get('id')
                    if st not in ("Concluído", "Entregue"):
                        idx = status_os_all.index(st) if st in status_os_all else 0
                        next_st = status_os_all[min(idx + 1, len(status_os_all) - 1)]

                        def _adv(s_id=sid, ns=next_st):
                            self.db.atualizar_status_servico(s_id, ns)
                            self.refresh_metas_list()

                        ctk.CTkButton(item_f, text="→", width=28, height=28,
                                     fg_color=cor, text_color="white", hover_color="#49769F",
                                     font=ctk.CTkFont(size=12, weight="bold"),
                                     command=_adv).pack(side="right", padx=(0, 4), pady=8)
        
        # ===== TAREFAS =====
        df_tarefas = self.db.listar_tarefas_estabelecimento()
        
        if df_tarefas.empty:
            ctk.CTkLabel(self.tarefas_scroll_frame, text="Nenhuma tarefa cadastrada.", 
                        font=ctk.CTkFont(family="Aileron", size=10), text_color="gray").pack(pady=10)
        else:
            # Separar tarefas por status
            tarefas_pendentes = df_tarefas[df_tarefas['status'] == 'Pendente']
            tarefas_progresso = df_tarefas[df_tarefas['status'] == 'Em Progresso']
            tarefas_concluidas = df_tarefas[df_tarefas['status'] == 'Concluído']
            
            # Contador de progresso
            total_t = len(df_tarefas)
            concluidas_t = len(tarefas_concluidas)
            progresso_t = len(tarefas_progresso)
            progress_frame_t = ctk.CTkFrame(self.tarefas_scroll_frame, fg_color="transparent")
            progress_frame_t.pack(fill="x", padx=5, pady=(0, 8))
            ctk.CTkLabel(progress_frame_t, text=f"{concluidas_t}/{total_t} feitas  •  {progresso_t} em andamento", 
                        font=ctk.CTkFont(family="Aileron", size=9, weight="bold"), 
                        text_color=("#28a745", "#6BCB77") if concluidas_t == total_t else ("#666", "#AAA")).pack(side="left")
            progress_bar_t = ctk.CTkProgressBar(progress_frame_t, height=6, width=120, 
                                                progress_color="#6EA2B3", fg_color=("#E0E0E0", "#444"))
            progress_bar_t.set(concluidas_t / total_t if total_t > 0 else 0)
            progress_bar_t.pack(side="right", padx=(5, 0))
            
            # Mostrar tarefas pendentes
            if not tarefas_pendentes.empty:
                for index, row in tarefas_pendentes.iterrows():
                    self.create_tarefa_item(row)
            
            # Mostrar tarefas em progresso
            if not tarefas_progresso.empty:
                if not tarefas_pendentes.empty:
                    sep_frame = ctk.CTkFrame(self.tarefas_scroll_frame, fg_color="#EBEBEB", height=2)
                    sep_frame.pack(fill="x", padx=5, pady=10)
                for index, row in tarefas_progresso.iterrows():
                    self.create_tarefa_item(row)
            
            # Mostrar tarefas concluídas
            if not tarefas_concluidas.empty:
                if not tarefas_pendentes.empty or not tarefas_progresso.empty:
                    sep_frame = ctk.CTkFrame(self.tarefas_scroll_frame, fg_color="#EBEBEB", height=2)
                    sep_frame.pack(fill="x", padx=5, pady=10)
                for index, row in tarefas_concluidas.iterrows():
                    self.create_tarefa_item(row)
    
    def create_tarefa_item(self, tarefa_row):
        """Cria um item de tarefa de estabelecimento"""
        is_completed = tarefa_row['status'] == 'Concluído'
        is_progress = tarefa_row['status'] == 'Em Progresso'
        
        item_frame = ctk.CTkFrame(self.tarefas_scroll_frame, fg_color=("#F5F5F5", "#2B2B2B") if is_completed else ("white", "#3A3A3A"), corner_radius=10)
        item_frame.pack(fill="x", padx=5, pady=5)
        
        # Content frame
        content_frame = ctk.CTkFrame(item_frame, fg_color="transparent")
        content_frame.pack(fill="x", padx=10, pady=10, side="left", expand=True)
        
        # Prioridade como indicador visual
        prioridade = tarefa_row['prioridade']
        cor_prioridade = {"Alta": "#FF6B6B", "Média": "#6EA2B3", "Baixa": "#7BBDE8"}.get(prioridade, "#FFD93D")
        status_icon = "✅" if is_completed else "🔄" if is_progress else "⬚"
        
        ctk.CTkLabel(content_frame, text=status_icon, font=ctk.CTkFont(family="Aileron", size=16, weight="bold"), 
                    text_color=cor_prioridade).pack(side="left", padx=(0, 8))
        
        # Texto
        text_frame = ctk.CTkFrame(content_frame, fg_color="transparent")
        text_frame.pack(side="left", fill="both", expand=True)
        
        titulo_color = "#888888" if is_completed else ("#10367D", "white")
        titulo_font = ctk.CTkFont(family="Aileron", size=10, weight="bold") if not is_completed else ctk.CTkFont(family="Aileron", size=10)
        
        # Área + Tarefa
        display_text = f"{tarefa_row['area_local']} - {tarefa_row['tarefa']}"
        ctk.CTkLabel(text_frame, text=display_text, font=titulo_font, text_color=titulo_color, wraplength=200).pack(anchor="w")
        
        # Status + Prioridade
        status_text = f"{tarefa_row['status']}  •  {prioridade}"
        status_color = "#888888" if is_completed else cor_prioridade
        ctk.CTkLabel(text_frame, text=status_text, font=ctk.CTkFont(family="Aileron", size=8, weight="bold"), 
                    text_color=status_color).pack(anchor="w", pady=(2, 0))
        
        # Botões de ação
        action_frame = ctk.CTkFrame(item_frame, fg_color="transparent")
        action_frame.pack(side="right", padx=10, pady=10)
        
        # Botão de status com label descritivo
        if tarefa_row['status'] == 'Pendente':
            proximo_status = "Em Progresso"
            btn_text = "▶ Iniciar"
            cor_btn = "#6EA2B3"
            text_cor = "white"
        elif tarefa_row['status'] == 'Em Progresso':
            proximo_status = "Concluído"
            btn_text = "✓ Concluir"
            cor_btn = "#28a745"
            text_cor = "white"
        else:
            proximo_status = "Pendente"
            btn_text = "↩ Reabrir"
            cor_btn = "#10367D"
            text_cor = "white"
        
        def update_tarefa_status(tarefa_id=tarefa_row['id'], novo_status=proximo_status):
            self.db.atualizar_status_estabelecimento(tarefa_id, novo_status)
            self.refresh_metas_list()
        
        ctk.CTkButton(action_frame, text=btn_text, width=75, height=28,
                    fg_color=cor_btn, text_color=text_cor,
                    hover_color=cor_btn, font=ctk.CTkFont(family="Aileron", size=9, weight="bold"),
                    command=update_tarefa_status).pack(side="left", padx=2)
        
        # Botão remover
        def delete_tarefa(tarefa_id=tarefa_row['id']):
            if messagebox.askyesno("Confirmar", "Remover esta tarefa?", parent=self):
                self.db.remover_tarefa_estabelecimento(tarefa_id)
                self.refresh_metas_list()
        
        ctk.CTkButton(action_frame, text="✕", width=28, height=28,
                    fg_color="red", text_color="white", hover_color="darkred",
                    font=ctk.CTkFont(family="Aileron", size=9, weight="bold"),
                    command=delete_tarefa).pack(side="left", padx=2)

    def open_quick_add_tarefa_dialog(self):
        """Diálogo rápido para adicionar tarefa direto do dashboard."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Adicionar Tarefa")
        dialog.geometry("400x400")
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.focus_set()
        
        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        ctk.CTkLabel(main_frame, text="Nova Tarefa", font=ctk.CTkFont(family="Aileron", size=16, weight="bold"), text_color=("#001D39", "white")).pack(pady=15)
        
        ctk.CTkLabel(main_frame, text="Área/Local:", font=ctk.CTkFont(family="Aileron", weight="bold"), text_color=("#001D39", "white")).pack(pady=(10, 0), padx=20, anchor="w")
        entry_area = ctk.CTkEntry(main_frame, placeholder_text="Ex: Cozinha, Estoque...", height=35)
        entry_area.pack(pady=5, padx=20, fill="x")
        
        ctk.CTkLabel(main_frame, text="Tarefa:", font=ctk.CTkFont(family="Aileron", weight="bold"), text_color=("#001D39", "white")).pack(pady=(10, 0), padx=20, anchor="w")
        entry_tarefa = ctk.CTkEntry(main_frame, placeholder_text="Ex: Organizar prateleiras...", height=35)
        entry_tarefa.pack(pady=5, padx=20, fill="x")
        
        ctk.CTkLabel(main_frame, text="Prioridade:", font=ctk.CTkFont(family="Aileron", weight="bold"), text_color=("#001D39", "white")).pack(pady=(10, 0), padx=20, anchor="w")
        var_prioridade = ctk.StringVar(value="Média")
        combo_prioridade = ctk.CTkComboBox(main_frame, variable=var_prioridade, 
                                          values=["Baixa", "Média", "Alta"], state="readonly", height=35)
        combo_prioridade.pack(pady=5, padx=20, fill="x")
        
        def salvar():
            area = entry_area.get().strip()
            tarefa = entry_tarefa.get().strip()
            if not area or not tarefa:
                messagebox.showerror("Erro", "Preencha Área/Local e Tarefa.", parent=dialog)
                dialog.lift()
                dialog.focus_force()
                return
            self.db.adicionar_tarefa_estabelecimento(area, tarefa, var_prioridade.get())
            messagebox.showinfo("Sucesso", "Tarefa adicionada!", parent=dialog)
            dialog.destroy()
            self.refresh_metas_list()
        
        ctk.CTkButton(main_frame, text="Adicionar Tarefa", command=salvar, height=40,
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F").pack(pady=20, padx=20, fill="x", side="bottom")

    def create_charts(self):
        # Frame para gráficos com scroll e borda suave
        # Alterado para transparent para mostrar o background
        # Borda azul clara fraca conforme solicitado
        self.charts_frame = ctk.CTkScrollableFrame(self.main_frame, fg_color="transparent", corner_radius=20,
                                                border_width=1, border_color=("#7BBDE8", "#3A506B"))
        self.charts_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=10, pady=10)
        self.charts_frame.grid_rowconfigure(0, minsize=320)  # Altura mínima para os gráficos
        
        # Configurar grid do card
        self.charts_frame.grid_columnconfigure(0, weight=1)
        self.charts_frame.grid_columnconfigure(1, weight=1)
        
        self.create_charts_content()
    
    def create_charts_content(self):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from services.relatorio_service import RelatorioService

        dash_data = RelatorioService(self.db, ai_predictor=self.ai).dados_dashboard()
        labels = dash_data["estoque_pizza"]["labels"]
        sizes = dash_data["estoque_pizza"]["sizes"]
        cats = dash_data["gastos_barras"]["categorias"]
        vals = dash_data["gastos_barras"]["valores"]

        # Definir cores baseadas no tema atual
        is_dark = ctk.get_appearance_mode() == "Dark"
        bg_color = "#2B2B2B" if is_dark else "#F0F0F5"
        text_color = "white" if is_dark else "black"

        # Alterado facecolor para combinar com o fundo
        fig_pie = Figure(figsize=(4.5, 3), dpi=100, facecolor=bg_color)
        ax_pie = fig_pie.add_subplot(111)
        # Cores da paleta
        colors = ['#10367D', '#7BBDE8', '#EBEBEB', '#0D2B63', '#49769F', '#CCCCCC']
        
        wedges, texts, autotexts = ax_pie.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90, colors=colors[:len(labels)])
        
        # Ajustar cor do texto do gráfico de pizza
        for text in texts:
            text.set_color(text_color)
        for autotext in autotexts:
            autotext.set_color("black") # Manter contraste dentro das fatias

        ax_pie.set_title("Estoque por Produto", fontsize=10, fontweight='bold', color=text_color)
        fig_pie.tight_layout()
        
        # Tooltip para hover
        annot = ax_pie.annotate("", xy=(0,0), xytext=(20,20), textcoords="offset points",
                            bbox=dict(boxstyle="round", fc="white", ec="black", alpha=0.9),
                            arrowprops=dict(arrowstyle="->"))
        annot.set_visible(False)

        def hover(event):
            vis = annot.get_visible()
            if event.inaxes == ax_pie:
                for i, wedge in enumerate(wedges):
                    cont, _ = wedge.contains(event)
                    if cont:
                        annot.xy = (event.xdata, event.ydata)
                        annot.set_text(f"{labels[i]}: {sizes[i]} un")
                        annot.set_visible(True)
                        fig_pie.canvas.draw_idle()
                        return
            if vis:
                annot.set_visible(False)
                fig_pie.canvas.draw_idle()
        
        # Container para o gráfico de pizza com borda
        pie_container = ctk.CTkFrame(self.charts_frame, fg_color="transparent", corner_radius=10,
                                    border_width=1, border_color=("#7BBDE8", "#3A506B"))
        pie_container.grid(row=0, column=0, padx=(5, 20), pady=(10, 5))

        canvas_pie = FigureCanvasTkAgg(fig_pie, master=pie_container)
        canvas_pie.draw()
        canvas_pie.mpl_connect("motion_notify_event", hover)
        canvas_pie.get_tk_widget().configure(bg=bg_color, highlightthickness=0)
        canvas_pie.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

        # --- Gráfico de Barras: Gastos por Categoria ---
        fig_bar = Figure(figsize=(4.5, 2.8), dpi=100, facecolor=bg_color)
        ax_bar = fig_bar.add_subplot(111)
        ax_bar.set_facecolor(bg_color) # Fundo do plot também
        
        # Cor das barras adaptativa
        bar_color = "#7BBDE8" if is_dark else "#0A4174"
        ax_bar.bar(cats, vals, color=bar_color)
        
        ax_bar.set_title("Gastos por Categoria", fontsize=9, fontweight='bold', color=text_color)
        
        # Ajustar cores dos eixos
        ax_bar.tick_params(axis='x', colors=text_color)
        ax_bar.tick_params(axis='y', colors=text_color)
        ax_bar.spines['bottom'].set_color(text_color)
        ax_bar.spines['left'].set_color(text_color)
        ax_bar.spines['top'].set_visible(False)
        ax_bar.spines['right'].set_visible(False)
        
        # Teto dinâmico baseado nos dados reais
        if vals and max(vals) > 0:
            max_val = max(vals)
            ax_bar.set_ylim(0, max_val * 1.3)  # 30% acima do valor máximo
        else:
            ax_bar.set_ylim(0, 100)
        
        # Rotacionar labels se houver muitas categorias
        if len(cats) > 3:
            ax_bar.tick_params(axis='x', rotation=45)
        
        fig_bar.tight_layout()
        
        # Container para o gráfico de barras com borda
        bar_container = ctk.CTkFrame(self.charts_frame, fg_color="transparent", corner_radius=10,
                                    border_width=1, border_color=("#7BBDE8", "#3A506B"))
        bar_container.grid(row=0, column=1, padx=(20, 5), pady=(10, 5))

        canvas_bar = FigureCanvasTkAgg(fig_bar, master=bar_container)
        canvas_bar.draw()
        canvas_bar.get_tk_widget().configure(bg=bg_color, highlightthickness=0)
        canvas_bar.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

        # Previsão de Gastos (IA) - DEPOIS dos gráficos
        self.update_prediction_display()

    def update_prediction_display(self):
        from services.relatorio_service import RelatorioService

        prev = RelatorioService(self.db, ai_predictor=self.ai).previsao_vendas(treinar=True)
        trained = prev["treinado"]
        prediction = prev["valor"]
        
        # Frame de Previsão com design profissional - ABAIXO DOS GRÁFICOS (row=1)
        # Frame com estilo mais leve e luminoso
        pred_frame = ctk.CTkFrame(self.charts_frame, fg_color=("#F0F9FF", "#2B2B2B"), corner_radius=15, 
                                border_width=1, border_color=("#7BBDE8", "#444444"))
        pred_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 5))
        pred_frame.grid_columnconfigure(0, weight=1)
        
        # Container com ícone e conteúdo
        content_frame = ctk.CTkFrame(pred_frame, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=15, pady=10)
        
        # Ícone + Título
        header_frame = ctk.CTkFrame(content_frame, fg_color="transparent")
        header_frame.pack(fill="x", pady=(0, 10))
        
        ctk.CTkLabel(header_frame, text="", font=ctk.CTkFont(family="Aileron", size=24)).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(header_frame, text="Previsão IA - Gastos Próximo Mês", font=ctk.CTkFont(family="Aileron", size=15, weight="bold"), text_color=("#001D39", "white")).pack(side="left")
        
        # Separador
        sep_frame = ctk.CTkFrame(content_frame, fg_color=("#0A4174", "#4da6ff"), height=2)
        sep_frame.pack(fill="x", pady=(0, 15))
        
        # Conteúdo
        if trained:
            # Valor com destaque
            valor_frame = ctk.CTkFrame(content_frame, fg_color=("#E8F5FF", "#3A3A3A"), corner_radius=10)
            valor_frame.pack(fill="x", pady=(0, 10))
            
            ctk.CTkLabel(valor_frame, text=f"R$ {prediction:.2f}".replace('.', ','), 
                        font=ctk.CTkFont(family="Aileron", size=28, weight="bold"), text_color=("#001D39", "#4da6ff")).pack(pady=15)
            
            # Descrição
            ctk.CTkLabel(content_frame, text="Baseado em análise de gastos históricos e tendências", 
                        font=ctk.CTkFont(family="Aileron", size=11), text_color=("#666", "#CCCCCC")).pack(pady=(0, 5))
            ctk.CTkLabel(content_frame, text="A IA está preparada e pronta para orientar suas decisões financeiras", 
                        font=ctk.CTkFont(family="Aileron", size=10), text_color=("#999", "#AAAAAA")).pack()
        else:
            # Sem dados
            ctk.CTkLabel(content_frame, text="Dados Insuficientes", 
                        font=ctk.CTkFont(family="Aileron", size=14, weight="bold"), text_color="#FF6B6B").pack(pady=(0, 10))
            ctk.CTkLabel(content_frame, text="Adicione gastos no menu 'GASTOS' para que a IA possa fazer previsões precisas.", 
                        font=ctk.CTkFont(family="Aileron", size=11), text_color=("#555", "#CCCCCC")).pack(pady=(0, 5))
            ctk.CTkLabel(content_frame, text="Quanto mais dados, melhor a previsão!", 
                        font=ctk.CTkFont(family="Aileron", size=10), text_color=("#999", "#AAAAAA")).pack()


    def _ask_admin_login(self, on_success):
        """Exibe dialog de login admin. Chama on_success() se credenciais corretas."""
        login_win = ctk.CTkToplevel(self)
        login_win.title("Acesso Restrito")
        login_win.geometry("370x310")
        login_win.attributes("-topmost", True)
        login_win.resizable(False, False)

        ctk.CTkLabel(login_win, text="🔒 Área Administrativa",
                     font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                     text_color=("#001D39", "white")).pack(pady=(20, 5))
        ctk.CTkLabel(login_win, text="Insira suas credenciais para continuar.",
                     font=("Aileron", 11), text_color=("gray40", "gray70")).pack(pady=(0, 15))

        ctk.CTkLabel(login_win, text="Usuário:", font=("Aileron", 12, "bold")).pack(anchor="w", padx=30)
        entry_user = ctk.CTkEntry(login_win, height=35, placeholder_text="Usuário")
        entry_user.pack(fill="x", padx=30, pady=(2, 10))

        ctk.CTkLabel(login_win, text="Senha:", font=("Aileron", 12, "bold")).pack(anchor="w", padx=30)
        entry_pass = ctk.CTkEntry(login_win, height=35, placeholder_text="Senha", show="*")
        entry_pass.pack(fill="x", padx=30, pady=(2, 10))

        lbl_erro = ctk.CTkLabel(login_win, text="", font=("Aileron", 11), text_color="#FF6B6B")
        lbl_erro.pack()

        def _tentar_login():
            u = entry_user.get().strip()
            p = entry_pass.get()
            if self._get_auth_service().verificar_gate_admin(u, p):
                login_win.destroy()
                on_success()
                return
            lbl_erro.configure(text="Usuário ou senha incorretos!")

        ctk.CTkButton(login_win, text="Entrar", command=_tentar_login,
                      fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 13, "bold"), height=40).pack(fill="x", padx=30, pady=(5, 5))

        if admin_needs_setup():
            ctk.CTkLabel(login_win, text="Primeiro acesso padrão: admin / admin",
                         font=("Aileron", 9), text_color=("gray50", "gray60")).pack(pady=(0, 10))

        entry_user.focus_set()
        entry_pass.bind("<Return>", lambda e: _tentar_login())
        login_win.grab_set()

    def sidebar_event(self, btn_text):
        if not self.current_user:
            return
        if not self._tem_permissao(btn_text):
            messagebox.showwarning("Acesso Negado", f"Seu perfil não tem permissão para '{btn_text}'.")
            return
        from services.nicho_service import NichoService
        try:
            acao = NichoService().acao_do_botao(btn_text)
        except ValueError:
            return

        dispatch = {
            "gastos_dialog": self.open_gastos_dialog,
            "kit_manager": self.open_kit_manager,
            "inventario_dialog": self.open_inventario_dialog,
            "sugestao_compras_dialog": self.open_sugestao_compras_dialog,
            "chat_ai": self.open_chat_ai,
            "saldo_dialog": self.open_saldo_dialog,
            "caixa_dialog": self.open_caixa_dialog,
            "categorias_manager": self.open_categorias_manager,
            "fornecedores_manager": lambda: self.open_generic_manager(
                "Fornecedores", self.db.listar_fornecedores, self.db.adicionar_fornecedor,
                self.db.remover_fornecedor, ["Nome", "Contato", "Produto Fornecido"]
            ),
            "clientes_manager": self.open_clientes_manager,
            "anota_tudo_dialog": self.open_anota_tudo_dialog,
            "vendedores_dialog": self.open_vendedores_dialog,
            "config_dialog": lambda: self._ask_admin_login(self.open_config_dialog),
            "insumos_manager": self.open_insumos_manager,
            "producao_dialog": self.open_producao_dialog,
            "trocas_dialog": self.open_trocas_dialog,
            "lista_reposicao": self.open_lista_reposicao,
            "mesas_manager": self.open_mesas_manager,
            "comandas_dialog": self.open_comandas_dialog,
            "cozinha_dialog": self.open_cozinha_dialog,
            "contas_pagar_dialog": self.open_contas_pagar_dialog,
            "etiquetas_dialog": self.open_etiquetas_dialog,
            "servico_dialog": self.open_servico_dialog,
            "sell_product_dialog": self.open_sell_product_dialog,
        }
        handler = dispatch.get(acao)
        if handler:
            handler()

    def topbar_event(self, btn_text):
        if not self.current_user:
            return
        if not self._tem_permissao(btn_text):
            messagebox.showwarning("Acesso Negado", f"Seu perfil não tem permissão para '{btn_text}'.")
            return
        if btn_text == "ADICIONAR":
            self.open_add_product_dialog()
        elif btn_text == "VENDER":
            self.open_sell_product_dialog()
        elif btn_text == "SERVIÇO":
            self.open_servico_dialog()
        elif btn_text == "LISTA":
            self.open_lista_dialog()
        elif btn_text == "RELATORIO":
            self._ask_admin_login(self.open_report_dialog)
        elif btn_text == "DRE":
            self._ask_admin_login(self.open_dre_dialog)
    
    def open_gastos_dialog(self):
        self.open_gasto_manager()

    def open_generic_manager(self, title, list_func, add_func, remove_func, field_names):
        window = ctk.CTkToplevel(self)
        window.title(f"Gestão de {title}")
        window.geometry("650x600")
        window.attributes("-topmost", True)

        # Frame principal
        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(title_frame, text=f"{title}", font=ctk.CTkFont(family="Aileron", size=18, weight="bold"), text_color=("#001D39", "white")).pack()
        
        # Adição de itens
        add_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        add_frame.pack(fill="x", pady=(0, 20))
        
        ctk.CTkLabel(add_frame, text=f"Adicionar {title}", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 10), padx=15, anchor="w")
        
        entries = []
        for field in field_names:
            ctk.CTkLabel(add_frame, text=f"{field}:", font=ctk.CTkFont(family="Aileron", weight="bold", size=11), text_color=("#001D39", "white")).pack(pady=(5, 2), padx=15, anchor="w")
            entry = ctk.CTkEntry(add_frame, placeholder_text=f"Digite {field.lower()}...", height=35)
            entry.pack(pady=2, padx=15, fill="x")
            entries.append(entry)
            
        def add_item():
            values = [e.get().strip() for e in entries]
            if all(values):
                add_func(*values)
                messagebox.showinfo("Sucesso", "Item adicionado!", parent=window)
                refresh_list()
                for e in entries: e.delete(0, "end")
            else:
                messagebox.showerror("Erro", "Preencha todos os campos.", parent=window)

        btn_frame = ctk.CTkFrame(add_frame, fg_color="transparent")
        btn_frame.pack(pady=15, padx=15, fill="x")
        ctk.CTkButton(btn_frame, text="Adicionar", command=add_item, height=35, 
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=ctk.CTkFont(family="Aileron", weight="bold")).pack(fill="x")

        # Lista de itens
        list_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        list_card.pack(fill="both", expand=True)
        
        ctk.CTkLabel(list_card, text=f"Lista de {title}", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 10), padx=15, anchor="w")
        
        list_frame = ctk.CTkScrollableFrame(list_card, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        list_frame.pack(pady=10, padx=15, fill="both", expand=True)

        def refresh_list():
            for widget in list_frame.winfo_children():
                widget.destroy()
            
            df = list_func()
            if df.empty:
                ctk.CTkLabel(list_frame, text="Nenhum item encontrado.", text_color=("#999999", "#AAAAAA")).pack(pady=20)
                return

            for index, row in df.iterrows():
                item_frame = ctk.CTkFrame(list_frame, fg_color=("white", "#404040"), corner_radius=8)
                item_frame.pack(pady=5, fill="x", padx=5)
                
                # Display values
                display_text = " - ".join([str(val) for col, val in row.items() if col != 'id'])
                
                ctk.CTkLabel(item_frame, text=display_text, anchor="w", text_color=("#333333", "white")).pack(side="left", padx=10, pady=8, expand=True, fill="x")
                
                def delete_item(id_val=row['id']):
                    if messagebox.askyesno("Confirmar", "Remover este item?", parent=window):
                        remove_func(id_val)
                        refresh_list()

                ctk.CTkButton(item_frame, text="Deletar", width=30, fg_color="#FF6B6B", hover_color="#FF5252", text_color="white", command=delete_item).pack(side="right", padx=5, pady=5)

        refresh_list()

    def open_categorias_manager(self):
        """Gerencia categorias, subcategorias e tamanhos."""
        window = ctk.CTkToplevel(self)
        window.title("Gerenciar Categorias")
        window.geometry("750x750")
        window.attributes("-topmost", True)

        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)

        ctk.CTkLabel(main_frame, text="Gerenciar Categorias", font=ctk.CTkFont(family="Aileron", size=18, weight="bold"), text_color=("#001D39", "white")).pack(pady=(0, 15))

        # Tabview para separar Produto / Gasto
        tabview = ctk.CTkTabview(main_frame, width=700, height=650)
        tabview.pack(fill="both", expand=True)

        for tipo_label, tipo_key in [("Categorias de Produto", "produto"), ("Categorias de Gasto", "gasto")]:
            tab = tabview.add(tipo_label)

            # --- Adicionar Categoria ---
            add_frame = ctk.CTkFrame(tab, fg_color=("white", "#2B2B2B"), corner_radius=15)
            add_frame.pack(fill="x", pady=(0, 10))

            ctk.CTkLabel(add_frame, text="Nova Categoria:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(10, 5), padx=15, anchor="w")
            entry_cat = ctk.CTkEntry(add_frame, placeholder_text="Ex: Camisa, Calça, Acessórios...", height=35)
            entry_cat.pack(pady=5, padx=15, fill="x")

            # --- Lista Hierárquica ---
            list_frame = ctk.CTkScrollableFrame(tab, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
            list_frame.pack(fill="both", expand=True)

            def refresh_cats(lf=list_frame, tk=tipo_key):
                for w in lf.winfo_children():
                    w.destroy()
                cats = self.db.listar_categorias(tk)
                if not cats:
                    ctk.CTkLabel(lf, text="Nenhuma categoria cadastrada.", text_color=("#999999", "#AAAAAA")).pack(pady=20)
                    return
                for cat_name in cats:
                    # --- Card da Categoria ---
                    cat_card = ctk.CTkFrame(lf, fg_color=("white", "#404040"), corner_radius=10, border_width=1, border_color=("#DDDDDD", "#555555"))
                    cat_card.pack(pady=4, fill="x", padx=5)

                    # Header da categoria
                    cat_header = ctk.CTkFrame(cat_card, fg_color="transparent")
                    cat_header.pack(fill="x", padx=10, pady=(8, 4))
                    ctk.CTkLabel(cat_header, text=f"📁 {cat_name}", anchor="w", text_color=("#001D39", "white"), font=ctk.CTkFont(family="Aileron", size=13, weight="bold")).pack(side="left", expand=True, fill="x")

                    def del_cat(n=cat_name, t=tk, r=refresh_cats):
                        if messagebox.askyesno("Confirmar", f"Remover categoria '{n}' e todas suas subcategorias?", parent=window):
                            self.db.remover_categoria(n, t)
                            r()
                    ctk.CTkButton(cat_header, text="✕", width=30, fg_color="#FF6B6B", hover_color="#FF5252", text_color="white", command=del_cat).pack(side="right")

                    # Subcategorias dentro da categoria
                    subcats = self.db.listar_subcategorias(cat_name, tk)
                    sub_container = ctk.CTkFrame(cat_card, fg_color="transparent")
                    sub_container.pack(fill="x", padx=20, pady=(0, 4))

                    for sub_name in subcats:
                        sub_frame = ctk.CTkFrame(sub_container, fg_color=("#EBF5FB", "#3A3A3A"), corner_radius=8)
                        sub_frame.pack(fill="x", pady=2)

                        sub_header = ctk.CTkFrame(sub_frame, fg_color="transparent")
                        sub_header.pack(fill="x", padx=8, pady=(4, 2))
                        ctk.CTkLabel(sub_header, text=f"  ↳ {sub_name}", anchor="w", text_color=("#333333", "#CCCCCC"), font=ctk.CTkFont(family="Aileron", size=12)).pack(side="left", expand=True, fill="x")

                        def del_sub(cn=cat_name, sn=sub_name, t=tk, r=refresh_cats):
                            if messagebox.askyesno("Confirmar", f"Remover subcategoria '{sn}'?", parent=window):
                                self.db.remover_subcategoria(cn, sn, t)
                                r()
                        ctk.CTkButton(sub_header, text="✕", width=25, height=22, fg_color="#FF9999", hover_color="#FF6666", text_color="white", command=del_sub, font=ctk.CTkFont(size=10)).pack(side="right")

                        # Sub-subcategorias dentro da subcategoria
                        sub_subs = self.db.listar_sub_subcategorias(cat_name, sub_name, tk)
                        if sub_subs:
                            ss_frame = ctk.CTkFrame(sub_frame, fg_color="transparent")
                            ss_frame.pack(fill="x", padx=25, pady=(0, 4))
                            for ss_name in sub_subs:
                                ss_item = ctk.CTkFrame(ss_frame, fg_color=("#D6EAF8", "#2A3A4A"), corner_radius=6)
                                ss_item.pack(fill="x", pady=1, padx=2)
                                ctk.CTkLabel(ss_item, text=f"    ↳ {ss_name}", font=ctk.CTkFont(size=11), text_color=("#444", "#CCC")).pack(side="left", padx=6, pady=2)

                                def del_ss(cn=cat_name, sn=sub_name, ssn=ss_name, t=tk, r=refresh_cats):
                                    self.db.remover_sub_subcategoria(cn, sn, ssn, t)
                                    r()
                                ctk.CTkButton(ss_item, text="✕", width=18, height=18, fg_color="transparent", hover_color="#FF9999", text_color="#999", command=del_ss, font=ctk.CTkFont(size=9)).pack(side="right")

                        # Botão adicionar sub-subcategoria
                        def add_ss_dlg(cn=cat_name, sn=sub_name, t=tk, r=refresh_cats):
                            ss_str = ctk.CTkInputDialog(text=f"Nova sub-subcategoria em '{sn}':\n(Ex: Estampada, Lisa, Bordada...)", title="Nova Sub-Subcategoria").get_input()
                            if ss_str and ss_str.strip():
                                if self.db.adicionar_sub_subcategoria(cn, sn, ss_str.strip(), t):
                                    r()
                                else:
                                    messagebox.showwarning("Aviso", "Sub-subcategoria já existe.", parent=window)
                        ctk.CTkButton(sub_frame, text="+ Sub-Subcat", width=100, height=22, fg_color="#85C1E9", hover_color="#5DADE2", text_color="#001D39", font=ctk.CTkFont(size=10), command=add_ss_dlg).pack(padx=25, pady=(0, 2), anchor="w")

                        # Tamanhos dentro da subcategoria
                        tamanhos = self.db.listar_tamanhos(cat_name, sub_name, tk)
                        if tamanhos:
                            tam_frame = ctk.CTkFrame(sub_frame, fg_color="transparent")
                            tam_frame.pack(fill="x", padx=25, pady=(0, 4))
                            for tam in tamanhos:
                                t_item = ctk.CTkFrame(tam_frame, fg_color=("#D5F5E3", "#2D4A3E"), corner_radius=6)
                                t_item.pack(side="left", padx=2, pady=2)
                                ctk.CTkLabel(t_item, text=f" {tam} ", font=ctk.CTkFont(size=11), text_color=("#333", "#DDD")).pack(side="left", padx=4, pady=2)

                                def del_tam(cn=cat_name, sn=sub_name, tn=tam, t=tk, r=refresh_cats):
                                    self.db.remover_tamanho(cn, sn, tn, t)
                                    r()
                                ctk.CTkButton(t_item, text="✕", width=18, height=18, fg_color="transparent", hover_color="#FF9999", text_color="#999", command=del_tam, font=ctk.CTkFont(size=9)).pack(side="left")

                        # Botão adicionar tamanho
                        def add_tam_dlg(cn=cat_name, sn=sub_name, t=tk, r=refresh_cats):
                            tam_str = ctk.CTkInputDialog(text=f"Tamanho para '{sn}':\n(Ex: P, M, G, GG, 38, 40...)", title="Novo Tamanho").get_input()
                            if tam_str and tam_str.strip():
                                self.db.adicionar_tamanho(cn, sn, tam_str.strip().upper(), t)
                                r()
                        ctk.CTkButton(sub_frame, text="+ Tamanho", width=90, height=22, fg_color="#5DADE2", hover_color="#3498DB", text_color="white", font=ctk.CTkFont(size=10), command=add_tam_dlg).pack(padx=25, pady=(0, 6), anchor="w")

                    # Botão adicionar subcategoria
                    def add_sub_dlg(cn=cat_name, t=tk, r=refresh_cats):
                        sub_str = ctk.CTkInputDialog(text=f"Nova subcategoria em '{cn}':\n(Ex: Malha, Linho, Jeans...)", title="Nova Subcategoria").get_input()
                        if sub_str and sub_str.strip():
                            if self.db.adicionar_subcategoria(cn, sub_str.strip(), t):
                                r()
                            else:
                                messagebox.showwarning("Aviso", "Subcategoria já existe.", parent=window)
                    ctk.CTkButton(cat_card, text="+ Subcategoria", width=130, height=26, fg_color="#0A4174", hover_color="#062F56", text_color="white", font=ctk.CTkFont(size=11, weight="bold"), command=add_sub_dlg).pack(padx=10, pady=(0, 8), anchor="w")

            def add_cat(entry=entry_cat, tk=tipo_key, rf=refresh_cats):
                nome = entry.get().strip()
                if not nome:
                    messagebox.showerror("Erro", "Digite um nome para a categoria.", parent=window)
                    return
                if self.db.adicionar_categoria(nome, tk):
                    messagebox.showinfo("Sucesso", f"Categoria '{nome}' adicionada!", parent=window)
                    entry.delete(0, "end")
                    rf()
                else:
                    messagebox.showwarning("Aviso", f"Categoria '{nome}' já existe.", parent=window)

            ctk.CTkButton(add_frame, text="Adicionar Categoria", command=add_cat, height=35,
                        fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                        font=ctk.CTkFont(family="Aileron", weight="bold")).pack(pady=(5, 10), padx=15, fill="x")

            refresh_cats()

    def open_config_dialog(self):
        """Abre janela de configuração geral do sistema com abas."""
        cfg = get_printer_config()

        dialog = ctk.CTkToplevel(self)
        dialog.title("Configurações")
        dialog.geometry("650x620")
        dialog.attributes("-topmost", True)

        tabview = ctk.CTkTabview(dialog, width=610, height=570)
        tabview.pack(padx=15, pady=15, fill="both", expand=True)

        # ====== ABA IMPRESSORA ======
        tab_printer = tabview.add("Impressora")

        frame = ctk.CTkFrame(tab_printer, fg_color="transparent")
        frame.pack(padx=10, pady=10, fill="both", expand=True)

        enabled_var = ctk.BooleanVar(value=cfg.get("enabled", False))
        ctk.CTkCheckBox(frame, text="Usar impressora física", variable=enabled_var).pack(anchor="w", pady=5)

        type_var = ctk.StringVar(value=cfg.get("type", "text"))
        ctk.CTkLabel(frame, text="Tipo de Impressora:").pack(anchor="w", pady=(10,2))
        type_combo = ctk.CTkComboBox(frame, values=["text", "windows", "escpos"], variable=type_var, state="readonly")
        type_combo.pack(pady=5, fill="x")

        # Seleção de impressora Windows instalada
        printer_name_var = ctk.StringVar(value=cfg.get("settings", {}).get("printer_name", ""))
        lbl_printer_name = ctk.CTkLabel(frame, text="Impressora Windows:")
        combo_printer_name = ctk.CTkComboBox(frame, variable=printer_name_var, values=[""], state="readonly")

        from services.impressao_service import ImpressaoService

        imp_svc = ImpressaoService(self.db, printer_factory=get_printer)
        impressoras = imp_svc.listar_impressoras()
        if impressoras:
            combo_printer_name.configure(values=impressoras)
            if not printer_name_var.get():
                printer_name_var.set(impressoras[0])

        interface_var = ctk.StringVar(value=cfg.get("interface", "file"))
        lbl_interface = ctk.CTkLabel(frame, text="Interface:")
        combo_interface = ctk.CTkComboBox(frame, values=["file", "usb", "network", "serial"], variable=interface_var, state="readonly")

        lbl_params = ctk.CTkLabel(frame, text="Parâmetros (JSON):")
        entry_params = ctk.CTkEntry(frame, width=300)
        entry_params.insert(0, json.dumps(cfg.get("settings", {})))

        def atualizar_campos_tipo(*args):
            tipo = type_var.get()
            # Esconder todos os campos opcionais
            lbl_printer_name.pack_forget()
            combo_printer_name.pack_forget()
            lbl_interface.pack_forget()
            combo_interface.pack_forget()
            lbl_params.pack_forget()
            entry_params.pack_forget()
            # Mostrar campos relevantes
            if tipo == "windows":
                lbl_printer_name.pack(anchor="w", pady=(10,2))
                combo_printer_name.pack(pady=5, fill="x")
            elif tipo == "escpos":
                lbl_interface.pack(anchor="w", pady=(10,2))
                combo_interface.pack(pady=5, fill="x")
                lbl_params.pack(anchor="w", pady=(10,2))
                entry_params.pack(pady=5, fill="x")

        type_var.trace_add("write", atualizar_campos_tipo)
        atualizar_campos_tipo()

        def save_config_action():
            imp_svc.salvar_config_impressora(
                enabled=enabled_var.get(),
                tipo=type_var.get(),
                printer_name=printer_name_var.get(),
                interface=interface_var.get(),
                params_json=entry_params.get(),
            )
            messagebox.showinfo("Configurações", "Configurações salvas!", parent=dialog)

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(15,0))
        ctk.CTkButton(btn_frame, text="Salvar", command=save_config_action, fg_color="#7BBDE8", text_color="#001D39").pack(side="left", expand=True, fill="x", padx=5)

        def test_printer():
            try:
                res = imp_svc.testar_impressora()
                messagebox.showinfo("Teste", res["mensagem"], parent=dialog)
            except Exception as e:
                messagebox.showerror("Teste", f"Falha: {e}", parent=dialog)

        ctk.CTkButton(btn_frame, text="Testar", command=test_printer, fg_color="#AED6F1", text_color="#001D39").pack(side="left", expand=True, fill="x", padx=5)

        # ====== ABA BACKUP ======
        tab_backup = tabview.add("Backup")
        backup_frame = ctk.CTkFrame(tab_backup, fg_color="transparent")
        backup_frame.pack(padx=10, pady=10, fill="both", expand=True)

        ctk.CTkLabel(backup_frame, text="Gerenciamento de Backup", font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                    text_color=("#001D39", "white")).pack(pady=(0, 15))

        def criar_backup_manual():
            ok, result = self.db.criar_backup()
            if ok:
                messagebox.showinfo("Backup", f"Backup criado com sucesso!\n{result}", parent=dialog)
                atualizar_lista_backups()
            else:
                messagebox.showerror("Erro", f"Erro ao criar backup: {result}", parent=dialog)

        ctk.CTkButton(backup_frame, text="📦 Criar Backup Agora", command=criar_backup_manual,
                        fg_color="#28a745", hover_color="#218838", text_color="white",
                        font=("Aileron", 13, "bold"), height=40).pack(fill="x", pady=(0, 8))

        def exportar_kit_recuperacao():
            dest = filedialog.askdirectory(title="Selecione a Pasta de Destino (Google Drive / OneDrive / Pendrive)", parent=dialog)
            if not dest:
                return
            ok, msg = self.db.exportar_backup_externo(dest)
            if ok:
                messagebox.showinfo("Backup Externo Exportado", msg, parent=dialog)
                atualizar_lista_backups()
            else:
                messagebox.showerror("Erro ao Exportar", msg, parent=dialog)

        ctk.CTkButton(backup_frame, text="☁️ Exportar Kit de Recuperação (Nuvem / Pendrive)", command=exportar_kit_recuperacao,
                        fg_color="#0A4174", hover_color="#062F56", text_color="white",
                        font=("Aileron", 12, "bold"), height=36).pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(backup_frame, text="Backups Disponíveis:", font=("Aileron", 12, "bold"),
                    text_color=("#001D39", "white")).pack(anchor="w", pady=(0, 5))

        backup_list_frame = ctk.CTkScrollableFrame(backup_frame, height=180, fg_color=("white", "#2B2B2B"))
        backup_list_frame.pack(fill="both", expand=True, pady=(0, 10))

        def atualizar_lista_backups():
            for w in backup_list_frame.winfo_children():
                w.destroy()
            backups = self.db.listar_backups()
            if not backups:
                ctk.CTkLabel(backup_list_frame, text="Nenhum backup encontrado.", text_color="gray").pack(pady=20)
                return
            for bk in backups:
                row = ctk.CTkFrame(backup_list_frame, fg_color="transparent")
                row.pack(fill="x", pady=2, padx=5)
                ctk.CTkLabel(row, text=f"📁 {bk['data']}  ({bk['tamanho']})",
                            font=("Aileron", 11), anchor="w").pack(side="left", fill="x", expand=True)
                ctk.CTkButton(row, text="Restaurar", width=80, height=28,
                                fg_color="#E67E22", hover_color="#D35400", text_color="white",
                                font=("Aileron", 10, "bold"),
                                command=lambda p=bk['path']: restaurar_backup(p)).pack(side="right", padx=2)

        def restaurar_backup(path):
            if messagebox.askyesno("Restaurar Backup",
                    "ATENÇÃO: Isso substituirá TODOS os dados atuais pelo backup selecionado.\n\n"
                    "Um backup de segurança será criado antes.\n\nDeseja continuar?", parent=dialog):
                ok, msg = self.db.restaurar_backup(path)
                if ok:
                    messagebox.showinfo(
                        "Backup Restaurado",
                        "Backup restaurado. O Estoke será fechado para concluir a restauração — abra novamente em seguida.",
                        parent=dialog,
                    )
                    self._on_closing(confirmar=False)
                else:
                    messagebox.showerror("Erro", msg, parent=dialog)

        atualizar_lista_backups()

        # ====== ABA EXPORTAR ======
        tab_reports = tabview.add("Exportar")
        report_frame = ctk.CTkFrame(tab_reports, fg_color="transparent")
        report_frame.pack(padx=10, pady=10, fill="both", expand=True)

        ctk.CTkLabel(report_frame, text="Exportar Relatórios", font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                    text_color=("#001D39", "white")).pack(pady=(0, 15))

        ctk.CTkLabel(report_frame, text="Tipo:", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(0, 3))
        tipo_report_var = ctk.StringVar(value="vendas")
        ctk.CTkComboBox(report_frame, values=["vendas", "estoque", "financeiro"], variable=tipo_report_var,
                        state="readonly", height=35).pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(report_frame, text="Período:", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(0, 3))
        periodo_report_var = ctk.StringVar(value="mes")
        ctk.CTkComboBox(report_frame, values=["hoje", "semana", "mes", "ano", "todos"], variable=periodo_report_var,
                        state="readonly", height=35).pack(fill="x", pady=(0, 15))

        def exportar_relatorio():
            tipo = tipo_report_var.get()
            periodo = periodo_report_var.get()
            ok, result = self.db.exportar_relatorio_pdf(tipo, periodo)
            if ok:
                messagebox.showinfo("Exportado", f"Relatório salvo em:\n{result}", parent=dialog)
                try:
                    os.startfile(result)
                except Exception:
                    pass
            else:
                messagebox.showerror("Erro", result, parent=dialog)

        ctk.CTkButton(report_frame, text="📊 Exportar Relatório", command=exportar_relatorio,
                        fg_color="#0A4174", hover_color="#062F56", text_color="white",
                        font=("Aileron", 13, "bold"), height=40).pack(fill="x")

        # ====== ABA NOTA PERSONALIZADA ======
        tab_nota = tabview.add("Nota/Recibo")
        nota_frame = ctk.CTkScrollableFrame(tab_nota, fg_color="transparent")
        nota_frame.pack(padx=10, pady=10, fill="both", expand=True)

        ctk.CTkLabel(nota_frame, text="Personalizar Nota/Recibo", font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                    text_color=("#001D39", "white")).pack(pady=(0, 15))

        nota_cfg = self.db.get_config_nota()

        nota_entries = {}
        nota_fields = [
            ("Nome da Empresa", "empresa_nome"),
            ("E-mail", "empresa_email"),
            ("Telefone", "empresa_telefone"),
            ("Endereço", "empresa_endereco"),
            ("CNPJ/CPF", "empresa_cnpj"),
            ("Inscrição Estadual (IE)", "empresa_ie"),
            ("Device ID Pinpad Point (Mercado Pago)", "mp_point_device_id"),
            ("Mensagem de Rodapé", "mensagem_rodape"),
        ]
        for label, key in nota_fields:
            ctk.CTkLabel(nota_frame, text=f"{label}:", font=("Aileron", 11, "bold")).pack(anchor="w", pady=(8, 2))
            e = ctk.CTkEntry(nota_frame, height=35)
            e.insert(0, nota_cfg.get(key, '') or '')
            e.pack(fill="x", pady=(0, 5))
            nota_entries[key] = e

        ctk.CTkLabel(nota_frame, text="Configuração NFC-e Modelo 65 (API REST):", font=("Aileron", 12, "bold"),
                     text_color=("#001D39", "white")).pack(anchor="w", pady=(12, 4))
        var_ambiente_nfce = ctk.StringVar(value=str(nota_cfg.get("ambiente") or "homologacao"))
        ctk.CTkComboBox(nota_frame, values=["homologacao", "producao"], variable=var_ambiente_nfce,
                        state="readonly", height=32).pack(fill="x", pady=(0, 6))

        fiscal_secret_fields = [
            ("Token API Fiscal (Focus NFe)", "token_api", True),
            ("ID do CSC (SEFAZ)", "id_csc", False),
            ("CSC (Código de Segurança do Contribuinte)", "csc", True),
        ]
        for label, key, masked in fiscal_secret_fields:
            ctk.CTkLabel(nota_frame, text=f"{label}:", font=("Aileron", 11, "bold")).pack(anchor="w", pady=(6, 2))
            e = ctk.CTkEntry(nota_frame, height=35, show="*" if masked else "")
            e.insert(0, nota_cfg.get(key, '') or '')
            e.pack(fill="x", pady=(0, 5))
            nota_entries[key] = e

        var_emitir_nfce_auto = ctk.BooleanVar(value=bool(nota_cfg.get("emitir_nfce_automatico", 0)))
        ctk.CTkCheckBox(nota_frame, text="Emitir NFC-e automaticamente ao finalizar venda no PDV",
                        variable=var_emitir_nfce_auto, font=("Aileron", 11)).pack(anchor="w", pady=(4, 8))

        # Logo/Foto
        ctk.CTkLabel(nota_frame, text="Logo da Empresa:", font=("Aileron", 11, "bold")).pack(anchor="w", pady=(8, 2))
        lbl_logo = ctk.CTkLabel(nota_frame, text=nota_cfg.get('empresa_logo', '') or "Nenhum logo selecionado",
                                font=("Aileron", 10, "italic"), text_color="gray")
        lbl_logo.pack(anchor="w", padx=5)

        _temp_logo_path = [nota_cfg.get('empresa_logo', '')]

        def pick_logo():
            dialog.attributes("-topmost", False)
            p = filedialog.askopenfilename(filetypes=[("Imagens", "*.png *.jpg *.jpeg")])
            dialog.attributes("-topmost", True)
            if p:
                # Copiar para appdata
                img_dir = os.path.join(self.db.data_dir, "product_images")
                os.makedirs(img_dir, exist_ok=True)
                ext = os.path.splitext(p)[1]
                logo_name = f"logo_empresa{ext}"
                try:
                    shutil.copy2(p, os.path.join(img_dir, logo_name))
                    _temp_logo_path[0] = logo_name
                    lbl_logo.configure(text=logo_name)
                except Exception as e:
                    logger.warning("Erro ao copiar logo: %s", e)

        ctk.CTkButton(nota_frame, text="Selecionar Logo", command=pick_logo,
                      fg_color="#5DADE2", text_color="#001D39", height=30).pack(fill="x", pady=(5, 10))

        def salvar_nota_config():
            new_cfg = {}
            for key, entry in nota_entries.items():
                new_cfg[key] = entry.get().strip()
            new_cfg['ambiente'] = var_ambiente_nfce.get().strip() or 'homologacao'
            new_cfg['emitir_nfce_automatico'] = 1 if var_emitir_nfce_auto.get() else 0
            new_cfg['empresa_logo'] = _temp_logo_path[0]
            self.db.salvar_config_nota(new_cfg)
            messagebox.showinfo("Sucesso", "Configuração da nota salva!", parent=dialog)

        ctk.CTkButton(nota_frame, text="Salvar Configurações da Nota", command=salvar_nota_config,
                      fg_color="#28a745", hover_color="#218838", text_color="white",
                      font=("Aileron", 13, "bold"), height=40).pack(fill="x", pady=(10, 5))

        def previsualizar_nota():
            """Gera uma nota com os dados reais da última venda e exibe como imagem."""
            preview_cfg = {}
            for key, entry in nota_entries.items():
                preview_cfg[key] = entry.get().strip()
            preview_cfg['empresa_logo'] = _temp_logo_path[0]

            try:
                res_prev = imp_svc.gerar_preview_cupom(preview_cfg)
                tmp = res_prev["png_path"]
                if os.name == 'nt':
                    os.startfile(tmp)
                else:
                    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
                    subprocess.call([opener, tmp])
            except ImportError:
                messagebox.showwarning("Aviso",
                    "Biblioteca Pillow não instalada.\nInstale com: pip install Pillow",
                    parent=dialog)
            except RuntimeError as exc:
                messagebox.showwarning("Aviso", str(exc), parent=dialog)
            except Exception as e:
                logger.warning("Erro na pré-visualização: %s", e)
                messagebox.showwarning("Aviso", f"Erro ao gerar pré-visualização:\n{e}", parent=dialog)

        ctk.CTkButton(nota_frame, text="👁 Pré-visualizar Nota", command=previsualizar_nota,
                      fg_color="#5DADE2", hover_color="#3498DB", text_color="#001D39",
                      font=("Aileron", 13, "bold"), height=40).pack(fill="x", pady=(5, 5))

        # ====== ABA CALCULADORA DE LUCRO ======
        tab_calc = tabview.add("Calc. Lucro")
        calc_frame = ctk.CTkFrame(tab_calc, fg_color="transparent")
        calc_frame.pack(padx=10, pady=10, fill="both", expand=True)

        # ====== ABA TIPO DE NEGÓCIO ======
        tab_btype = tabview.add("Tipo Negócio")
        btype_scroll = ctk.CTkScrollableFrame(tab_btype, fg_color="transparent")
        btype_scroll.pack(padx=10, pady=10, fill="both", expand=True)

        ctk.CTkLabel(btype_scroll, text="Tipo de Negócio",
                     font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                     text_color=("#001D39", "white")).pack(pady=(0, 5))
        ctk.CTkLabel(btype_scroll, text="Altere o nicho para personalizar campos, menus e fluxo de trabalho.",
                     font=("Aileron", 11), text_color=("gray40", "gray70")).pack(pady=(0, 10))

        btype_labels = {
            "pet_shop_agro": "🐾 Pet Shop & Agropecuária (Produtos + Banho e Tosa)",
            "oficina_mecanica": "🔧 Oficina Mecânica & Motopeças (OS + Peças + Mão de Obra)",
            "farmacia_suplementos": "💊 Farmácia, Suplementos & Nutrição (Lotes + Validades)",
            "papelaria_livraria": "📚 Papelaria & Livraria (Alto Volume SKUs + ISBN)",
            "otica_joalheria": "👓 Ótica & Joalheria (Nº Série + Garantias + Receitas)",
            "treinamento_recorrencia": "🥋 Estúdios & Treinamento (Mensalidades + Turmas + Balcão)",
            "tecnologia_informatica": "💻 Tecnologia & Informática (Assistência/Hardware)",
            "estetica_automotiva": "🚗 Estética Automotiva (Car Detailing/Lavagem)",
            "moda_customizacao": "👗 Moda & Customização (Boutiques/Ateliês)",
            "festas_eventos": "🎉 Artigos para Festas & Eventos (Locação)",
            "estetica_arte_corporal": "✂️ Arte Corporal & Estética (Tattoo/Barbearias)",
            "delivery_alimentacao": "🍔 Delivery Rápido & Alimentação (Lanches/Combos)",
            "casa_construcao": "🔨 Casa, Construção & Condomínios (Ferragens)",
            "loja_roupa": "👔 Loja de Roupas Geral (Grade P/M/G/GG)",
            "padaria_acougue": "🥖 Padaria & Açougue (Venda por Peso)",
            "restaurante": "🍽️ Restaurante & Bar (Mesas/Comandas)",
            "artesanato": "🎨 Artesanato & Handmade",
            "variados": "🏪 Variados / Comércio Geral",
        }
        current_btype = self.business_type or "variados"
        btype_var = ctk.StringVar(value=current_btype)

        for key, label in btype_labels.items():
            ctk.CTkRadioButton(btype_scroll, text=label, variable=btype_var, value=key,
                               font=("Aileron", 12), text_color=("#001D39", "white")).pack(anchor="w", pady=4, padx=5)

        def _save_btype():
            new_type = btype_var.get()
            set_business_type(new_type)
            self.business_type = new_type
            messagebox.showinfo("Tipo de Negócio",
                                f"Tipo alterado para: {btype_labels.get(new_type, new_type)}\n\n"
                                "As mudanças foram salvas e aplicadas aos menus e cadastros.",
                                parent=dialog)

        ctk.CTkButton(btype_scroll, text="Salvar Tipo de Negócio", command=_save_btype,
                      fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 13, "bold"), height=38).pack(fill="x", pady=(15, 10))

        ctk.CTkLabel(calc_frame, text="Calculadora de Margem de Lucro", font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                    text_color=("#001D39", "white")).pack(pady=(0, 15))

        ctk.CTkLabel(calc_frame, text="Preço de Custo (R$):", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(5, 2))
        e_custo_calc = ctk.CTkEntry(calc_frame, height=35, placeholder_text="Ex: 50.00")
        e_custo_calc.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(calc_frame, text="Preço de Venda (R$):", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(5, 2))
        e_venda_calc = ctk.CTkEntry(calc_frame, height=35, placeholder_text="Ex: 80.00")
        e_venda_calc.pack(fill="x", pady=(0, 10))

        lbl_resultado_calc = ctk.CTkLabel(calc_frame, text="", font=("Aileron", 18, "bold"), text_color="#28a745")
        lbl_resultado_calc.pack(pady=10)

        lbl_detalhes_calc = ctk.CTkLabel(calc_frame, text="", font=("Aileron", 12), text_color=("#333", "white"), wraplength=400)
        lbl_detalhes_calc.pack(pady=5)

        def calcular_margem():
            from services.precificacao_service import PrecificacaoService
            try:
                res = PrecificacaoService().calcular_margem(e_custo_calc.get(), e_venda_calc.get())
                # Preserva rótulos históricos desta calculadora (bug de rótulos invertidos sinalizado no Lote 2)
                cor = "#28a745" if res["lucro"] > 0 else "#FF6B6B"
                lbl_resultado_calc.configure(
                    text=f"Lucro: R$ {res['lucro']:.2f}  |  Margem: {res['markup_pct']:.1f}%",
                    text_color=cor
                )
                lbl_detalhes_calc.configure(
                    text=f"Markup sobre venda: {res['margem_pct']:.1f}%\n"
                         f"Para cada R$ 1,00 investido, retorna R$ {res['retorno_por_real']:.2f}"
                )
            except ValueError as err:
                msg = str(err) if "Custo deve ser > 0" in str(err) else "Valores inválidos!"
                lbl_resultado_calc.configure(text=msg, text_color="#FF6B6B")
                if msg == "Valores inválidos!":
                    lbl_detalhes_calc.configure(text="")

        ctk.CTkButton(calc_frame, text="CALCULAR MARGEM DE LUCRO", command=calcular_margem,
                      fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 13, "bold"), height=40).pack(fill="x", pady=(10, 0))

        # ====== ABA ADMINISTRAÇÃO (alterar credenciais) ======
        tab_admin = tabview.add("Administração")
        admin_frame = ctk.CTkFrame(tab_admin, fg_color="transparent")
        admin_frame.pack(padx=10, pady=10, fill="both", expand=True)

        ctk.CTkLabel(admin_frame, text="🔐 Alterar Credenciais de Acesso",
                     font=ctk.CTkFont(family="Aileron", size=16, weight="bold"),
                     text_color=("#001D39", "white")).pack(pady=(0, 5))
        ctk.CTkLabel(admin_frame, text="Altere o usuário e senha usados para acessar Relatório e Configurações.",
                     font=("Aileron", 11), text_color=("gray40", "gray70")).pack(pady=(0, 15))

        admin_user_atual, _ = get_admin_credentials()

        ctk.CTkLabel(admin_frame, text="Novo Usuário:", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(5, 2))
        e_admin_user = ctk.CTkEntry(admin_frame, height=35)
        e_admin_user.insert(0, admin_user_atual)
        e_admin_user.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(admin_frame, text="Nova Senha:", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(5, 2))
        e_admin_pass = ctk.CTkEntry(admin_frame, height=35, placeholder_text="Digite a nova senha", show="*")
        e_admin_pass.pack(fill="x", pady=(0, 5))

        ctk.CTkLabel(admin_frame, text="Confirmar Senha:", font=("Aileron", 12, "bold")).pack(anchor="w", pady=(5, 2))
        e_admin_pass2 = ctk.CTkEntry(admin_frame, height=35, placeholder_text="Repita a nova senha", show="*")
        e_admin_pass2.pack(fill="x", pady=(0, 10))

        lbl_admin_status = ctk.CTkLabel(admin_frame, text="", font=("Aileron", 11))
        lbl_admin_status.pack(pady=5)

        def _salvar_credenciais():
            novo_user = e_admin_user.get().strip()
            nova_senha = e_admin_pass.get()
            confirm_senha = e_admin_pass2.get()
            if not novo_user:
                lbl_admin_status.configure(text="Usuário não pode ser vazio.", text_color="#FF6B6B")
                return
            if len(nova_senha) < 4:
                lbl_admin_status.configure(text="Senha deve ter pelo menos 4 caracteres.", text_color="#FF6B6B")
                return
            if nova_senha != confirm_senha:
                lbl_admin_status.configure(text="As senhas não coincidem!", text_color="#FF6B6B")
                return
            set_admin_credentials(novo_user, nova_senha)
            lbl_admin_status.configure(text="✅ Credenciais atualizadas com sucesso!", text_color="#28a745")

        ctk.CTkButton(admin_frame, text="Salvar Credenciais", command=_salvar_credenciais,
                      fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 13, "bold"), height=40).pack(fill="x", pady=(10, 0))

        # ====== ABA LOJAS / FILIAIS ======
        tab_lojas = tabview.add("Lojas")
        lojas_frame = ctk.CTkFrame(tab_lojas, fg_color="transparent")
        lojas_frame.pack(padx=10, pady=10, fill="both", expand=True)
        ctk.CTkLabel(lojas_frame, text="Gestão de Lojas / Filiais",
                     font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 10))
        ctk.CTkButton(lojas_frame, text="Abrir Gerenciador de Lojas", height=40,
                      fg_color="#7BBDE8", hover_color="#49769F", text_color="#001D39",
                      font=("Aileron", 12, "bold"),
                      command=self.open_lojas_dialog).pack(fill="x", pady=5)

        # ====== ABA USUÁRIOS ======
        tab_users = tabview.add("Usuários")
        users_frame = ctk.CTkFrame(tab_users, fg_color="transparent")
        users_frame.pack(padx=10, pady=10, fill="both", expand=True)
        ctk.CTkLabel(users_frame, text="Gestão de Usuários (Multi-Usuário)",
                     font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 10))
        ctk.CTkLabel(users_frame, text="Perfis: Admin, Gerente, Vendedor, Caixa\nCada perfil tem permissões específicas.",
                     font=("Aileron", 11), text_color=("gray40", "gray70"), justify="left").pack(pady=(0, 10), anchor="w")
        ctk.CTkButton(users_frame, text="Abrir Gerenciador de Usuários", height=40,
                      fg_color="#5DADE2", hover_color="#3498DB", text_color="#001D39",
                      font=("Aileron", 12, "bold"),
                      command=self.open_usuarios_dialog).pack(fill="x", pady=5)

        # ====== ABA API MOBILE ======
        tab_api = tabview.add("API Mobile")
        api_frame = ctk.CTkFrame(tab_api, fg_color="transparent")
        api_frame.pack(padx=10, pady=10, fill="both", expand=True)
        ctk.CTkLabel(api_frame, text="API REST para App Mobile",
                     font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 10))

        import api_server
        api_status = "Disponível" if api_server.api_disponivel() else "Indisponível (instale: pip install flask)"
        api_running = self.api_server is not None and hasattr(self.api_server, '_running') and self.api_server._running
        ctk.CTkLabel(api_frame, text=f"Status: {api_status}", font=("Aileron", 11),
                     text_color="#28a745" if "Disponível" == api_status else "#dc3545").pack(anchor="w", pady=5)
        ctk.CTkLabel(api_frame, text=f"Servidor: {'Rodando na porta 5050' if api_running else 'Parado'}",
                     font=("Aileron", 11)).pack(anchor="w", pady=5)
        ctk.CTkLabel(api_frame, text="O app Android se conecta via rede local.\nCertifique-se de que PC e celular estão na mesma rede WiFi.",
                     font=("Aileron", 10), text_color=("gray40", "gray70"), justify="left").pack(anchor="w", pady=10)

        def _restart_api():
            self._iniciar_api_server()
            messagebox.showinfo("API", "API REST reiniciada na porta 5050.", parent=dialog)

        ctk.CTkButton(api_frame, text="Reiniciar API", height=38,
                      fg_color="#2874A6", hover_color="#1B4F72", text_color="white",
                      font=("Aileron", 12, "bold"), command=_restart_api).pack(fill="x", pady=5)

        # --- Separador ---
        ctk.CTkLabel(api_frame, text="─" * 60, text_color=("gray70", "gray40")).pack(pady=(10, 5))

        # --- Detectar IP local ---
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_local = s.getsockname()[0]
            s.close()
        except Exception:
            ip_local = "127.0.0.1"

        ctk.CTkLabel(api_frame, text=f"IP deste PC: {ip_local}  |  Porta: 5050",
                     font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(anchor="w", pady=5)

        # --- Seleção de usuário para QR Code ---
        ctk.CTkLabel(api_frame, text="Gerar QR Code de acesso para o App:",
                     font=("Aileron", 11), text_color=("gray40", "gray70")).pack(anchor="w", pady=(10, 5))

        qr_select_frame = ctk.CTkFrame(api_frame, fg_color="transparent")
        qr_select_frame.pack(fill="x", pady=5)

        usuarios_df = self.db.listar_usuarios()
        usuarios_ativos = usuarios_df[usuarios_df['ativo'] == 1] if not usuarios_df.empty else usuarios_df
        user_options = [f"{row['login']} ({row['perfil']})" for _, row in usuarios_ativos.iterrows()] if not usuarios_ativos.empty else ["Nenhum usuário"]
        user_logins = [row['login'] for _, row in usuarios_ativos.iterrows()] if not usuarios_ativos.empty else []

        ctk.CTkLabel(qr_select_frame, text="Usuário:", font=("Aileron", 11)).pack(side="left", padx=(0, 8))
        combo_user = ctk.CTkComboBox(qr_select_frame, values=user_options, width=200, state="readonly")
        combo_user.pack(side="left", padx=(0, 8))
        if user_options:
            combo_user.set(user_options[0])

        # Campo de senha para incluir no QR
        senha_frame = ctk.CTkFrame(api_frame, fg_color="transparent")
        senha_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(senha_frame, text="Senha:", font=("Aileron", 11)).pack(side="left", padx=(0, 8))
        entry_senha_qr = ctk.CTkEntry(senha_frame, show="*", width=200, placeholder_text="Senha do usuário")
        entry_senha_qr.pack(side="left", padx=(0, 8))

        # Label para mostrar o QR Code
        qr_label = ctk.CTkLabel(api_frame, text="")
        qr_label.pack(pady=10)

        def _gerar_qr_code():
            idx = combo_user.current() if hasattr(combo_user, 'current') else user_options.index(combo_user.get()) if combo_user.get() in user_options else -1
            if idx < 0 or not user_logins:
                messagebox.showwarning("QR Code", "Selecione um usuário válido.", parent=dialog)
                return
            senha_val = entry_senha_qr.get().strip()
            if not senha_val:
                messagebox.showwarning("QR Code", "Digite a senha do usuário para gerar o QR Code.", parent=dialog)
                return
            login_val = user_logins[idx]

            # Validar credenciais antes de gerar QR via AuthService
            try:
                user = self._get_auth_service().autenticar(login_val, senha_val)
            except ValueError as err:
                messagebox.showwarning("QR Code", str(err), parent=dialog)
                return
            if not user:
                messagebox.showerror("QR Code", "Senha incorreta para o usuário selecionado.", parent=dialog)
                return

            if not self.api_server:
                messagebox.showerror("QR Code", "Servidor API local indisponível.", parent=dialog)
                return

            # Criar sessão de pareamento via método público thread-safe (sem tocar em _sessions)
            pareamento = self.api_server.criar_sessao_pareamento(user, ip_local=ip_local)
            qr_data = pareamento["qr_payload"]
            try:
                import qrcode
                from PIL import Image as PILImage, ImageTk
                qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
                qr.add_data(qr_data)
                qr.make(fit=True)
                img_qr = qr.make_image(fill_color="#001D39", back_color="white").convert("RGB")
                img_tk = ImageTk.PhotoImage(img_qr)
                qr_label.configure(image=img_tk, text="")
                qr_label._img_ref = img_tk  # manter referência
                ctk.CTkLabel(api_frame, text="📱 Escaneie este QR Code com o App ao Cubo.\n🔒 Token seguro (expira em 8h — sem senha exposta).",
                             font=("Aileron", 11, "bold"), text_color="#28a745").pack(pady=(0, 5))
            except Exception as e:
                messagebox.showerror("Erro", f"Erro ao gerar QR Code: {e}", parent=dialog)

        ctk.CTkButton(api_frame, text="📱 Gerar QR Code para App", height=42,
                      fg_color="#28a745", hover_color="#1e7e34", text_color="white",
                      font=("Aileron", 13, "bold"), command=_gerar_qr_code).pack(fill="x", pady=(5, 10))

    def open_estabelecimento_manager(self):
        """Gerencia tarefas de organização do espaço físico (estabelecimento)"""
        window = ctk.CTkToplevel(self)
        window.title("Gestão de Estabelecimento")
        window.geometry("750x650")
        window.attributes("-topmost", True)

        # Frame principal
        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(title_frame, text="Organização do Estabelecimento", font=ctk.CTkFont(family="Aileron", size=18, weight="bold"), text_color=("#001D39", "white")).pack()
        
        # Card de Adição
        add_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        add_frame.pack(fill="x", pady=(0, 20))
        
        ctk.CTkLabel(add_frame, text="Adicionar Tarefa", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 10), padx=15, anchor="w")
        
        # Campo Área/Local
        ctk.CTkLabel(add_frame, text="Área/Local:", font=ctk.CTkFont(family="Aileron", weight="bold", size=11), text_color=("#001D39", "white")).pack(pady=(5, 2), padx=15, anchor="w")
        entry_area = ctk.CTkEntry(add_frame, placeholder_text="Ex: Cozinha, Estoque...", height=35)
        entry_area.pack(pady=2, padx=15, fill="x")
        
        # Campo Tarefa
        ctk.CTkLabel(add_frame, text="Tarefa:", font=ctk.CTkFont(family="Aileron", weight="bold", size=11), text_color=("#001D39", "white")).pack(pady=(10, 2), padx=15, anchor="w")
        entry_tarefa = ctk.CTkEntry(add_frame, placeholder_text="Ex: Limpar prateleiras...", height=35)
        entry_tarefa.pack(pady=2, padx=15, fill="x")
        
        # Campo Prioridade
        ctk.CTkLabel(add_frame, text="Prioridade:", font=ctk.CTkFont(family="Aileron", weight="bold", size=11), text_color=("#001D39", "white")).pack(pady=(10, 2), padx=15, anchor="w")
        var_prioridade = ctk.StringVar(value="Média")
        combo_prioridade = ctk.CTkComboBox(add_frame, variable=var_prioridade, 
                                        values=["Baixa", "Média", "Alta"], state="readonly", height=35)
        combo_prioridade.pack(pady=2, padx=15, fill="x")
        
        def add_task():
            area = entry_area.get().strip()
            tarefa = entry_tarefa.get().strip()
            prioridade = var_prioridade.get()
            
            if area and tarefa:
                self.db.adicionar_tarefa_estabelecimento(area, tarefa, prioridade)
                messagebox.showinfo("Sucesso", "Tarefa adicionada!", parent=window)
                refresh_list()
                self.refresh_dashboard()  # Atualizar dashboard principal
                entry_area.delete(0, "end")
                entry_tarefa.delete(0, "end")
            else:
                messagebox.showerror("Erro", "Preencha Área/Local e Tarefa.", parent=window)

        btn_frame = ctk.CTkFrame(add_frame, fg_color="transparent")
        btn_frame.pack(pady=15, padx=15, fill="x")
        ctk.CTkButton(btn_frame, text="Adicionar Tarefa", command=add_task, height=35,
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=ctk.CTkFont(family="Aileron", weight="bold")).pack(fill="x")

        # Card de Lista
        list_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        list_card.pack(fill="both", expand=True)
        
        ctk.CTkLabel(list_card, text="Tarefas Pendentes", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 10), padx=15, anchor="w")
        
        list_frame = ctk.CTkScrollableFrame(list_card, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        list_frame.pack(pady=10, padx=15, fill="both", expand=True)

        def refresh_list():
            for widget in list_frame.winfo_children():
                widget.destroy()
            
            df = self.db.listar_tarefas_estabelecimento()
            if df.empty:
                ctk.CTkLabel(list_frame, text="Nenhuma tarefa encontrada.", text_color=("#999999", "#AAAAAA")).pack(pady=20)
                return

            for index, row in df.iterrows():
                task_frame = ctk.CTkFrame(list_frame, fg_color=("white", "#404040"), corner_radius=8, border_width=1, border_color=("#EEEEEE", "#555555"))
                task_frame.pack(pady=5, padx=5, fill="x")
                
                # Cor da prioridade
                prioridade = row['prioridade']
                cor_prioridade = {"Alta": "#FF6B6B", "Média": "#6EA2B3", "Baixa": "#7BBDE8"}.get(prioridade, "#FFD93D")
                
                # Info frame
                info_frame = ctk.CTkFrame(task_frame, fg_color="transparent")
                info_frame.pack(side="left", padx=10, pady=8, fill="x", expand=True)
                
                # Prioridade com cor
                priority_label = ctk.CTkLabel(info_frame, text=f"{prioridade}", text_color=cor_prioridade, font=ctk.CTkFont(family="Aileron", weight="bold", size=10))
                priority_label.pack(anchor="w")
                
                # Display task info
                info_text = f"{row['area_local']} - {row['tarefa']}"
                ctk.CTkLabel(info_frame, text=info_text, anchor="w", text_color=("#333333", "white"), font=ctk.CTkFont(family="Aileron", size=11)).pack(anchor="w")
                
                status_text = f"Status: {row['status']}"
                ctk.CTkLabel(info_frame, text=status_text, anchor="w", text_color=("#999999", "#AAAAAA"), font=ctk.CTkFont(family="Aileron", size=9)).pack(anchor="w")
                
                # Botões
                btn_frame = ctk.CTkFrame(task_frame, fg_color="transparent")
                btn_frame.pack(side="right", padx=10, pady=8)
                
                # Botão de status
                status_atual = row['status']
                proximo_status = "Em Progresso" if status_atual == "Pendente" else "Concluído" if status_atual == "Em Progresso" else "Pendente"
                cor_status = "#0A4174" if status_atual == "Pendente" else "#6EA2B3" if status_atual == "Em Progresso" else "#7BBDE8"
                
                def update_status(id_val=row['id'], novo_status=proximo_status):
                    self.db.atualizar_status_estabelecimento(id_val, novo_status)
                    self.refresh_metas_list()
        
                ctk.CTkButton(btn_frame, text=f"{proximo_status}", width=110, fg_color=cor_status, 
                            text_color="black" if status_atual != "Pendente" else "white",
                            hover_color=cor_status, command=update_status, font=ctk.CTkFont(family="Aileron", weight="bold", size=10)).pack(side="left", padx=3)
                
                def delete_task(id_val=row['id']):
                    if messagebox.askyesno("Confirmar", "Remover esta tarefa?", parent=window):
                        self.db.remover_tarefa_estabelecimento(id_val)
                        refresh_list()
                        self.refresh_dashboard()  # Atualizar dashboard principal

                ctk.CTkButton(btn_frame, text="Remover", width=30, fg_color="#FF6B6B", 
                            hover_color="#FF5252", text_color="white", command=delete_task).pack(side="left", padx=2)

        refresh_list()

    def open_saldo_dialog(self):
        from services.financeiro_service import FinanceiroService

        fin_svc = FinanceiroService(self.db)
        saldo_atual = fin_svc.saldo()
        
        dialog = ctk.CTkToplevel(self)
        dialog.title("Gestão de Saldo")
        dialog.geometry("450x450")
        dialog.attributes("-topmost", True)
        
        # Frame principal com padding
        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título com saldo atual
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(title_frame, text="Gestão de Saldo", font=ctk.CTkFont(family="Aileron", size=18, weight="bold"), text_color=("#001D39", "white")).pack()
        ctk.CTkLabel(title_frame, text=f"Saldo Atual: R$ {saldo_atual:.2f}".replace('.', ','), 
                    font=ctk.CTkFont(family="Aileron", size=14, weight="bold"), text_color="#7BBDE8").pack(pady=(10, 0))
        
        # Card com campos
        card_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card_frame.pack(fill="both", expand=True)
        
        # Campos com layout melhorado
        ctk.CTkLabel(card_frame, text="Valor da Entrada (R$):", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
        entry_valor = ctk.CTkEntry(card_frame, placeholder_text="Ex: 1000,00", height=35)
        entry_valor.pack(pady=5, padx=15, fill="x")
        
        ctk.CTkLabel(card_frame, text="Descrição:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
        entry_desc = ctk.CTkEntry(card_frame, placeholder_text="Ex: Venda de produtos", height=35)
        entry_desc.pack(pady=5, padx=15, fill="x")
        
        # Frame para botões
        button_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
        button_frame.pack(pady=20, padx=15, fill="x")
        
        def adicionar():
            if self.check_trial_limits('transacao', dialog):
                return
            try:
                fin_svc.adicionar_entrada(entry_valor.get(), entry_desc.get())
                messagebox.showinfo("Sucesso", "Saldo adicionado!", parent=dialog)
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror("Erro", str(exc), parent=dialog)
        
        def limpar():
            entry_valor.delete(0, "end")
            entry_desc.delete(0, "end")
        
        ctk.CTkButton(button_frame, text="Adicionar", command=adicionar, height=40, 
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=ctk.CTkFont(family="Aileron", weight="bold")).pack(side="left", padx=5, fill="x", expand=True)
        ctk.CTkButton(button_frame, text="Limpar", command=limpar, height=40, 
                    fg_color="#CCCCCC", text_color="black", hover_color="#AAAAAA",
                    font=ctk.CTkFont(family="Aileron", weight="bold")).pack(side="left", padx=5, fill="x", expand=True)

    def open_chat_ai(self):
        chat_window = ctk.CTkToplevel(self)
        chat_window.title("Chat com IA")
        chat_window.geometry("600x750")
        chat_window.attributes("-topmost", True)

        # Frame principal
        main_frame = ctk.CTkFrame(chat_window, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(title_frame, text="Assistente IA Gemini", font=ctk.CTkFont(family="Aileron", size=18, weight="bold"), text_color=("#001D39", "white")).pack()
        
        # Card com histórico
        card_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card_frame.pack(fill="both", expand=True, pady=(0, 15))
        
        # Histórico de chat
        chat_history = ctk.CTkTextbox(card_frame, width=560, height=500, fg_color=("#F5F5F5", "#333333"), corner_radius=10, border_width=0)
        chat_history.pack(pady=10, padx=10, fill="both", expand=True)
        chat_history.insert("0.0", "IA: Olá! 👋 Sou seu assistente inteligente. Pergunte qualquer coisa!\n\n"
                                "📊 CONSULTAS:\n"
                                "  • 'Resumo do dia' / 'Resumo do mês'\n"
                                "  • 'Quanto vendi hoje?' / 'Vendas da semana'\n"
                                "  • 'Qual meu lucro do mês?'\n"
                                "  • 'Quanto de fiado pendente?'\n"
                                "  • 'Contas a pagar vencendo'\n"
                                "  • 'Ranking de mais vendidos'\n"
                                "  • 'Produtos com estoque baixo'\n"
                                "  • 'Produtos vencendo'\n\n"
                                "🔧 COMANDOS:\n"
                                "  • 'Registre um gasto de 50 em Material'\n"
                                "  • 'Adicione produto Tinta com 10 un a 25 reais'\n"
                                "  • 'Venda 3 unidades de Tinta'\n"
                                "  📷 Use o botão 📷 para anexar uma imagem ao adicionar produto!\n\n"
                                "📈 ANÁLISES:\n"
                                "  • 'Me dê sugestões' / 'DRE' / 'Fluxo de caixa'\n"
                                "  • 'Previsão de gastos' / 'Comparativo mensal'\n\n"
                                "💡 Digite 'ajuda' para ver todos os comandos!\n\n")

        # Frame para input
        input_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        input_frame.pack(fill="x", pady=(0, 0))

        # Variável para imagem anexada
        _chat_img_path = [None]  # lista para mutabilidade em closure
        lbl_img_status = ctk.CTkLabel(input_frame, text="", font=ctk.CTkFont(family="Aileron", size=9),
                                       text_color=("#28a745", "#28a745"), width=20)

        def _anexar_imagem_chat():
            from tkinter import filedialog as fd
            path = fd.askopenfilename(
                title="Anexar imagem ao comando",
                filetypes=[("Imagens", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                           ("Todos", "*.*")]
            )
            if path:
                _chat_img_path[0] = path
                nome_arq = os.path.basename(path)
                lbl_img_status.configure(text=f"📷 {nome_arq[:20]}")
                chat_history.insert("end", f"📷 Imagem anexada: {nome_arq}\n")
                chat_history.see("end")

        btn_img = ctk.CTkButton(input_frame, text="📷", width=35, height=35, fg_color="#6EA2B3",
                                hover_color="#49769F", text_color="white",
                                font=ctk.CTkFont(family="Aileron", size=14),
                                command=_anexar_imagem_chat)
        btn_img.pack(side="left", padx=(0, 5))

        entry = ctk.CTkEntry(input_frame, placeholder_text="Digitar pergunta ou comando...", height=35)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        lbl_img_status.pack(side="left", padx=(0, 5))

        def send_message():
            question = entry.get().strip()
            if not question: 
                return
            
            chat_history.insert("end", f"Você: {question}\n⏳ IA analisando...\n")
            chat_history.see("end")
            entry.delete(0, "end")
            btn_send.configure(state="disabled", text="...")
            
            imagem = _chat_img_path[0]
            _chat_img_path[0] = None
            lbl_img_status.configure(text="")

            def _worker():
                try:
                    answer = self.ai.process_query(question, imagem_path=imagem)
                except Exception as exc:
                    answer = f"❌ Erro ao consultar IA: {exc}"

                def _deliver():
                    try:
                        if not chat_window.winfo_exists():
                            return
                        # Remove a linha temporária de status
                        content = chat_history.get("1.0", "end-1c")
                        if content.endswith("⏳ IA analisando...\n"):
                            chat_history.delete("end-2l", "end-1l")
                        chat_history.insert("end", f"IA: {answer}\n\n")
                        chat_history.see("end")
                        btn_send.configure(state="normal", text="Enviar")
                        if "✅" in answer:
                            self.refresh_dashboard()
                    except Exception:
                        pass

                try:
                    chat_window.after(0, _deliver)
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

        btn_send = ctk.CTkButton(input_frame, text="Enviar", width=80, fg_color="#7BBDE8", 
                                text_color="black", hover_color="#49769F", command=send_message,
                                font=ctk.CTkFont(family="Aileron", weight="bold"), height=35)
        btn_send.pack(side="right")
        
        # Permitir enviar com Enter
        entry.bind("<Return>", lambda e: send_message())



    def open_lista_dialog(self):
        """Lista unificada com abas, busca em tempo real e filtros por categoria/status."""
        window = ctk.CTkToplevel(self)
        window.title("Lista de Itens")
        window.geometry("780x650")
        window.attributes("-topmost", True)
        window.lift()
        window.focus_force()

        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(padx=15, pady=15, fill="both", expand=True)

        # === Header ===
        header = ctk.CTkFrame(main_frame, fg_color="transparent")
        header.pack(fill="x")
        ctk.CTkLabel(header, text="📋 Lista",
                     font=ctk.CTkFont(family="Aileron", size=20, weight="bold"),
                     text_color=("#001D39", "white")).pack(side="left")
        lbl_count = ctk.CTkLabel(header, text="",
                                 font=ctk.CTkFont(family="Aileron", size=12),
                                 text_color=("#999999", "#AAAAAA"))
        lbl_count.pack(side="right", padx=10)

        # === Abas ===
        tab_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        tab_frame.pack(fill="x", pady=(10, 0))

        current_tab = {"value": "produto"}
        tab_buttons = {}
        for tab_id, tab_label in [("produto", "📦 Produtos"), ("gasto", "💰 Gastos"), ("tarefa", "📝 Tarefas")]:
            btn = ctk.CTkButton(tab_frame, text=tab_label, width=130, height=35, corner_radius=20,
                                font=ctk.CTkFont(family="Aileron", size=12, weight="bold"),
                                fg_color=("#EBEBF0", "#3A3A3A"), text_color=("#333333", "white"),
                                hover_color=("#D9D9E0", "#4A4A4A"),
                                command=lambda t=tab_id: switch_tab(t))
            btn.pack(side="left", padx=5)
            tab_buttons[tab_id] = btn

        # === Busca + Filtro ===
        filter_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        filter_frame.pack(fill="x", pady=10)

        search_var = ctk.StringVar()
        search_entry = ctk.CTkEntry(filter_frame,
                                    placeholder_text="🔍 Buscar por nome, descrição...",
                                    textvariable=search_var, height=38, corner_radius=20)
        search_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        cat_var = ctk.StringVar(value="Todas")
        cat_combo = ctk.CTkComboBox(filter_frame, variable=cat_var, values=["Todas"],
                                    width=170, height=38, state="readonly",
                                    font=ctk.CTkFont(family="Aileron", size=11),
                                    command=lambda _: refresh_list())
        cat_combo.pack(side="right")

        # === Lista scrollável ===
        list_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        list_card.pack(fill="both", expand=True, pady=(5, 0))

        scroll = ctk.CTkScrollableFrame(list_card, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=8, pady=8)

        _search_timer = {"id": None}

        # ---- Funções internas ----

        def refresh_list(*_args):
            for w in scroll.winfo_children():
                w.destroy()

            tab = current_tab["value"]
            search = search_var.get().strip().lower()
            cat_filter = cat_var.get()

            if tab == "produto":
                df = self.db.listar_produtos()
                display_col = "nome"
            elif tab == "gasto":
                df = self.db.listar_gastos()
                display_col = "descricao"
            else:
                df = self.db.listar_tarefas_estabelecimento()
                display_col = "tarefa"

            if df is None or df.empty:
                ctk.CTkLabel(scroll, text="Nenhum item cadastrado.",
                             text_color=("#999999", "#AAAAAA"),
                             font=ctk.CTkFont(family="Aileron", size=13)).pack(pady=40)
                lbl_count.configure(text="0 itens")
                return

            # Filtro de busca
            if search:
                mask = df[display_col].astype(str).str.lower().str.contains(search, na=False, regex=False)
                if "categoria" in df.columns:
                    mask = mask | df["categoria"].astype(str).str.lower().str.contains(search, na=False, regex=False)
                if "area_local" in df.columns:
                    mask = mask | df["area_local"].astype(str).str.lower().str.contains(search, na=False, regex=False)
                df = df[mask]

            # Filtro de categoria / status
            if tab in ("produto", "gasto") and cat_filter != "Todas" and "categoria" in df.columns:
                df = df[df["categoria"] == cat_filter]
            elif tab == "tarefa" and cat_filter != "Todos" and "status" in df.columns:
                df = df[df["status"] == cat_filter]

            total = len(df)
            lbl_count.configure(text=f"{total} {'item' if total == 1 else 'itens'}")

            if df.empty:
                ctk.CTkLabel(scroll, text="Nenhum resultado para a busca.",
                             text_color=("#999999", "#AAAAAA"),
                             font=ctk.CTkFont(family="Aileron", size=13)).pack(pady=40)
                return

            for _, row in df.iterrows():
                _create_list_item(tab, row, display_col)

        def _create_list_item(tab, row, display_col):
            item_frame = ctk.CTkFrame(scroll, fg_color=("#F8F9FA", "#3A3A3A"), corner_radius=10,
                                      border_width=1, border_color=("#EEEEEE", "#555555"))
            item_frame.pack(fill="x", pady=3)

            # Botões (pack primeiro para ficarem à direita)
            btn_frame = ctk.CTkFrame(item_frame, fg_color="transparent")
            btn_frame.pack(side="right", padx=10, pady=10)

            def do_edit(t=tab, r=row):
                placeholder = ctk.CTkToplevel(self)
                placeholder.withdraw()
                self.perform_edit(t, r, placeholder)

            def do_remove(t=tab, r=row):
                if messagebox.askyesno("Confirmar", f"Remover este {t}?", parent=window):
                    if t == "produto":
                        self.db.remover_produto(r["id"])
                    elif t == "gasto":
                        self.db.remover_gasto(r["id"])
                    elif t == "tarefa":
                        self.db.remover_tarefa_estabelecimento(r["id"])
                    refresh_list()
                    self.refresh_dashboard()
                else:
                    window.lift()

            if tab == "produto":
                def do_qrcode(r=row):
                    self.show_product_qrcode_dialog(r)
                ctk.CTkButton(btn_frame, text="QR", width=35, height=30, corner_radius=8,
                              fg_color="#6EA2B3", text_color="white", hover_color="#49769F",
                              font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                              command=do_qrcode).pack(side="left", padx=2)

            ctk.CTkButton(btn_frame, text="✏️ Editar", width=80, height=30, corner_radius=8,
                          fg_color="#5DADE2", text_color="#001D39", hover_color="#3498DB",
                          font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                          command=do_edit).pack(side="left", padx=2)
            ctk.CTkButton(btn_frame, text="🗑️", width=35, height=30, corner_radius=8,
                          fg_color="#E74C3C", text_color="white", hover_color="#C0392B",
                          command=do_remove).pack(side="left", padx=2)

            # Conteúdo à esquerda
            content = ctk.CTkFrame(item_frame, fg_color="transparent")
            content.pack(side="left", fill="both", expand=True, padx=12, pady=10)

            if tab == "produto":
                if row.get("imagem"):
                    ctk_img = self._get_cached_thumbnail(row["imagem"], size=(40, 40))
                    if ctk_img:
                        ctk.CTkLabel(content, image=ctk_img, text="").pack(side="left", padx=(0, 10))

                text_frame = ctk.CTkFrame(content, fg_color="transparent")
                text_frame.pack(side="left", fill="both", expand=True)
                ctk.CTkLabel(text_frame, text=row["nome"],
                             font=ctk.CTkFont(family="Aileron", size=13, weight="bold"),
                             text_color=("#001D39", "white")).pack(anchor="w")

                preco = row.get("preco", 0) or 0
                qtd = row.get("quantidade", 0) or 0
                cat = row.get("categoria", "")
                qtd_color = "#E74C3C" if qtd <= 0 else "#F39C12" if qtd < 5 else ("#777777", "#AAAAAA")

                info_f = ctk.CTkFrame(text_frame, fg_color="transparent")
                info_f.pack(anchor="w")
                ctk.CTkLabel(info_f, text=cat, font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#777777", "#AAAAAA")).pack(side="left")
                ctk.CTkLabel(info_f, text=" • ", font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#CCCCCC", "#666666")).pack(side="left")
                ctk.CTkLabel(info_f, text=f"Qtd: {qtd}",
                             font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                             text_color=qtd_color).pack(side="left")
                ctk.CTkLabel(info_f, text=" • ", font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#CCCCCC", "#666666")).pack(side="left")
                ctk.CTkLabel(info_f, text=f"R$ {preco:.2f}",
                             font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                             text_color=("#28a745", "#6BCB77")).pack(side="left")

            elif tab == "gasto":
                ctk.CTkLabel(content, text=row.get("descricao", ""),
                             font=ctk.CTkFont(family="Aileron", size=13, weight="bold"),
                             text_color=("#001D39", "white")).pack(anchor="w")
                valor = row.get("valor", 0) or 0
                cat = row.get("categoria", "")
                data = row.get("data", "")
                ctk.CTkLabel(content, text=f"{cat}  •  R$ {valor:.2f}  •  {data}",
                             font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#777777", "#AAAAAA")).pack(anchor="w")

            else:  # tarefa
                ctk.CTkLabel(content, text=row.get("tarefa", ""),
                             font=ctk.CTkFont(family="Aileron", size=13, weight="bold"),
                             text_color=("#001D39", "white")).pack(anchor="w")
                area = row.get("area_local", "")
                prio = row.get("prioridade", "")
                status = row.get("status", "")
                prio_color = "#E74C3C" if prio == "Alta" else "#F39C12" if prio == "Média" else ("#777777", "#AAAAAA")
                status_color = "#28a745" if status == "Concluído" else "#F39C12" if status == "Em Progresso" else ("#777777", "#AAAAAA")

                info_f = ctk.CTkFrame(content, fg_color="transparent")
                info_f.pack(anchor="w")
                ctk.CTkLabel(info_f, text=area, font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#777777", "#AAAAAA")).pack(side="left")
                ctk.CTkLabel(info_f, text=" • ", font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#CCCCCC", "#666666")).pack(side="left")
                ctk.CTkLabel(info_f, text=prio,
                             font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                             text_color=prio_color).pack(side="left")
                ctk.CTkLabel(info_f, text=" • ", font=ctk.CTkFont(family="Aileron", size=10),
                             text_color=("#CCCCCC", "#666666")).pack(side="left")
                ctk.CTkLabel(info_f, text=status,
                             font=ctk.CTkFont(family="Aileron", size=10, weight="bold"),
                             text_color=status_color).pack(side="left")

        def switch_tab(tab_name):
            current_tab["value"] = tab_name
            for name, btn in tab_buttons.items():
                if name == tab_name:
                    btn.configure(fg_color="#0A4174", text_color="white")
                else:
                    btn.configure(fg_color=("#EBEBF0", "#3A3A3A"), text_color=("#333333", "white"))
            _update_filter_options(tab_name)
            search_var.set("")
            refresh_list()

        def _update_filter_options(tab_name):
            if tab_name in ("produto", "gasto"):
                cats = ["Todas"]
                try:
                    cat_list = self.db.listar_categorias(tab_name)
                    if cat_list:
                        cats.extend(cat_list)
                except Exception:
                    pass
                cat_combo.configure(values=cats)
                cat_var.set("Todas")
            else:  # tarefa → filtrar por status
                cat_combo.configure(values=["Todos", "Pendente", "Em Progresso", "Concluído"])
                cat_var.set("Todos")

        def _on_search(*_args):
            if _search_timer["id"]:
                window.after_cancel(_search_timer["id"])
            _search_timer["id"] = window.after(300, refresh_list)

        search_var.trace_add("write", _on_search)

        # Atualizar lista quando a janela recebe foco (após edição de item)
        def _on_focus(event):
            if event.widget == window:
                refresh_list()
        window.bind("<FocusIn>", _on_focus)

        # Inicializar na aba Produtos
        switch_tab("produto")
        search_entry.focus_set()

    def open_edit_selection_dialog(self):
        self.open_selection_dialog("Editar", self.perform_edit)

    def open_remove_selection_dialog(self):
        self.open_selection_dialog("Remover", self.perform_remove)

    def open_selection_dialog(self, action_name, callback):
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"{action_name} Item")
        dialog.geometry("450x550")
        dialog.attributes("-topmost", True)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.lift()
        dialog.focus_force()

        # Frame principal
        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(title_frame, text=f"{action_name} Item", font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack()
        ctk.CTkLabel(title_frame, text="Selecione o tipo de item", font=("Aileron", 11), text_color=("#999999", "#AAAAAA")).pack()
        
        # Card com botões
        card_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card_frame.pack(fill="both", expand=True)
        
        button_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
        button_frame.pack(padx=20, pady=20, fill="both", expand=True)

        def select_type(type_):
            dialog.destroy()
            self.list_items_for_action(type_, action_name, callback)

        ctk.CTkButton(button_frame, text="Produto", command=lambda: select_type("produto"), height=50,
                    fg_color="#0A4174", text_color="white", hover_color="#0A1F4D",
                    font=("Aileron", 12, "bold")).pack(pady=10, fill="x")
        ctk.CTkButton(button_frame, text="Gasto", command=lambda: select_type("gasto"), height=50,
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=("Aileron", 12, "bold")).pack(pady=10, fill="x")
        ctk.CTkButton(button_frame, text="Tarefa", command=lambda: select_type("tarefa"), height=50,
                    fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                    font=("Aileron", 12, "bold")).pack(pady=10, fill="x")

    def list_items_for_action(self, type_, action_name, callback):
        list_window = ctk.CTkToplevel(self)
        list_window.title(f"Selecione o {type_} para {action_name}")
        list_window.geometry("550x500")
        list_window.attributes("-topmost", True)
        list_window.protocol("WM_DELETE_WINDOW", list_window.destroy)
        list_window.lift()
        list_window.focus_force()

        if type_ == "produto":
            df = self.db.listar_produtos()
            display_col = "nome"
            emoji = ""
        elif type_ == "gasto":
            df = self.db.listar_gastos()
            display_col = "descricao"
            emoji = ""
        elif type_ == "tarefa":
            df = self.db.listar_tarefas_estabelecimento()
            display_col = "tarefa"
            emoji = ""
        else:
            df = None
            emoji = ""

        if df is None or df.empty:
            # Frame principal
            main_frame = ctk.CTkFrame(list_window, fg_color="transparent")
            main_frame.pack(padx=20, pady=20, fill="both", expand=True)
            
            card_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
            card_frame.pack(fill="both", expand=True)
            
            ctk.CTkLabel(card_frame, text=f"Nenhum {type_} encontrado.", text_color=("#999999", "#AAAAAA")).pack(pady=40)
            return

        # Frame principal
        main_frame = ctk.CTkFrame(list_window, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(title_frame, text=f"Selecione o {type_}", font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack()
        
        # Card com lista
        card_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card_frame.pack(fill="both", expand=True)
        
        scroll = ctk.CTkScrollableFrame(card_frame, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        logger.debug("Listando itens para: %s", type_)
        if df is not None:
            logger.debug("Total de itens: %d", len(df))
            logger.debug("Colunas: %s", df.columns.tolist())

        for index, row in df.iterrows():
            try:
                icon_image = None
                
                if type_ == "meta":
                    # Para metas, mostrar título, descrição e status
                    text = f"{row[display_col]}"
                    if row.get('descricao'):
                        text += f" - {row['descricao'][:30]}"
                    color = "#CCCCCC" if row['status'] == "Concluído" else ("#333333", "white")
                elif type_ == "tarefa":
                    text = f"{row['area_local']} - {row['tarefa']} ({row['prioridade']})"
                    color = "#CCCCCC" if row['status'] == "Concluído" else ("#333333", "white")
                else:
                    # Lógica simplificada para preço/valor
                    val = row.get('valor', row.get('preco', 0))
                    text = f"{row[display_col]} (R$ {val})"
                    color = ("#333333", "white")
                    
                    # Carregar imagem para produtos
                    if type_ == "produto" and 'imagem' in row and row['imagem']:
                        icon_image = self._get_cached_thumbnail(row['imagem'], size=(30, 30))
                
                logger.debug("Criando botão para: %s", text)
                
                item_frame = ctk.CTkFrame(scroll, fg_color=("white", "#404040"), corner_radius=8, border_width=1, border_color=("#EEEEEE", "#555555"))
                item_frame.pack(pady=3, fill="x")
                
                btn = ctk.CTkButton(item_frame, text=text, text_color=color, fg_color=("white", "#404040"), 
                                    hover_color=("#F0F0F0", "#505050"), anchor="w",
                                    font=("Arial", 12),
                                    image=icon_image, compound="left",
                                    command=lambda r=row: callback(type_, r, list_window))
                btn.pack(pady=10, padx=10, fill="x")
            except Exception as e:
                logger.warning("Erro ao criar item %s: %s", index, e)

    def perform_remove(self, type_, row, window):
        if messagebox.askyesno("Confirmar", f"Tem certeza que deseja remover este {type_}?", parent=window):
            if type_ == "produto":
                self.db.remover_produto(row['id'])
            elif type_ == "gasto":
                self.db.remover_gasto(row['id'])
            elif type_ == "tarefa":
                self.db.remover_tarefa_estabelecimento(row['id'])
            window.destroy()
            messagebox.showinfo("Sucesso", "Item removido!", parent=self)
            self.refresh_dashboard()  # Atualizar em tempo real
        else:
            window.attributes("-topmost", True)

    def perform_edit(self, type_, row, window):
        window.destroy()
        
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Editar {type_.capitalize()}")
        dialog.geometry("450x600")
        dialog.attributes("-topmost", True)
        
        # Frame principal
        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)
        
        # Título
        title_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        title_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(title_frame, text=f"Editar {type_.capitalize()}", font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack()
        
        # Card
        card_frame = ctk.CTkScrollableFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card_frame.pack(fill="both", expand=True)
        
        cmd_salvar = None

        if type_ == "tarefa":
            ctk.CTkLabel(card_frame, text="Área/Local:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_area = ctk.CTkEntry(card_frame, height=35)
            entry_area.insert(0, row['area_local'])
            entry_area.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Tarefa:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_tarefa = ctk.CTkEntry(card_frame, height=35)
            entry_tarefa.insert(0, row['tarefa'])
            entry_tarefa.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Prioridade:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            prioridade_var = ctk.StringVar(value=row['prioridade'])
            prioridade_combo = ctk.CTkComboBox(card_frame, values=["Baixa", "Média", "Alta"], variable=prioridade_var, state="readonly", height=35)
            prioridade_combo.pack(pady=5, padx=15, fill="x")

            ctk.CTkLabel(card_frame, text="Status:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            status_var = ctk.StringVar(value=row['status'])
            status_combo = ctk.CTkComboBox(card_frame, values=["Pendente", "Em Progresso", "Concluído"], variable=status_var, state="readonly", height=35)
            status_combo.pack(pady=5, padx=15, fill="x")
            
            def salvar_tarefa():
                area = entry_area.get().strip()
                tarefa = entry_tarefa.get().strip()
                prioridade = prioridade_var.get()
                status = status_var.get()
                
                if not area or not tarefa:
                    messagebox.showerror("Erro", "Preencha Área e Tarefa.", parent=dialog)
                    dialog.lift()
                    dialog.focus_force()
                    return
                
                self.db.atualizar_tarefa_estabelecimento(row['id'], area, tarefa, prioridade, status)
                messagebox.showinfo("Sucesso", "Tarefa atualizada!", parent=dialog)
                dialog.destroy()
                self.refresh_dashboard()
            
            cmd_salvar = salvar_tarefa

        elif type_ == "gasto":
            ctk.CTkLabel(card_frame, text="Valor (R$):", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_valor = ctk.CTkEntry(card_frame, height=35)
            entry_valor.insert(0, str(row['valor']).replace('.', ','))
            entry_valor.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Categoria:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            cat_options = self.db.listar_categorias('gasto') or ["Material", "Transporte", "Serviços", "Aluguel", "Outro"]
            cat_var = ctk.StringVar(value=row['categoria'])
            cat_menu = ctk.CTkComboBox(card_frame, values=cat_options, variable=cat_var, state="readonly", height=35)
            cat_menu.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Descrição:", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_desc = ctk.CTkEntry(card_frame, height=35)
            entry_desc.insert(0, row['descricao'])
            entry_desc.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Data (YYYY-MM-DD):", font=ctk.CTkFont(family="Aileron", weight="bold", size=12), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_data = ctk.CTkEntry(card_frame, height=35)
            entry_data.insert(0, row['data'])
            entry_data.pack(pady=5, padx=15, fill="x")
            
            def salvar_gasto():
                valor = entry_valor.get().strip()
                categoria = cat_var.get()
                descricao = entry_desc.get().strip()
                data = entry_data.get().strip()
                
                try:
                    valor_clean = valor.replace("R$", "").replace(" ", "").replace(",", ".")
                    valor_float = float(valor_clean)
                    
                    self.db.atualizar_gasto(row['id'], data, valor_float, categoria, descricao)
                    messagebox.showinfo("Sucesso", "Gasto atualizado!", parent=dialog)
                    dialog.destroy()
                    self.refresh_dashboard()
                except ValueError:
                    messagebox.showerror("Erro", "Valor inválido.", parent=dialog)
            
            cmd_salvar = salvar_gasto

        elif type_ == "produto":
            ctk.CTkLabel(card_frame, text="Nome:", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_nome = ctk.CTkEntry(card_frame, height=35)
            entry_nome.insert(0, row['nome'])
            entry_nome.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Quantidade:", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_qtd = ctk.CTkEntry(card_frame, height=35)
            entry_qtd.insert(0, str(row['quantidade']))
            entry_qtd.pack(pady=5, padx=15, fill="x")
            
            ctk.CTkLabel(card_frame, text="Categoria:", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            categoria_options = self.db.listar_categorias('produto') or ["Material", "Produto Pronto", "Insumo", "Outro"]
            categoria_var = ctk.StringVar(value=row['categoria'])
            categoria_menu = ctk.CTkComboBox(card_frame, values=categoria_options, variable=categoria_var, state="readonly", height=35)
            categoria_menu.pack(pady=5, padx=15, fill="x")

            # --- Seção de Precificação Ética ---
            ctk.CTkLabel(card_frame, text="Precificação Ética", font=ctk.CTkFont(family="Aileron", size=14, weight="bold"), text_color=("#0A4174", "#7BBDE8")).pack(pady=(20, 10), padx=15, anchor="w")
            
            # Grid para campos numéricos
            grid_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
            grid_frame.pack(fill="x", padx=10)
            
            # Coluna 1
            ctk.CTkLabel(grid_frame, text="Horas Trab.:", font=("Aileron", 11, "bold")).grid(row=0, column=0, padx=5, pady=5, sticky="w")
            entry_horas = ctk.CTkEntry(grid_frame, width=100)
            entry_horas.grid(row=1, column=0, padx=5, pady=5)
            entry_horas.insert(0, str(row.get('horas_trabalho', 0)))
            
            ctk.CTkLabel(grid_frame, text="Valor Hora (R$):", font=("Aileron", 11, "bold")).grid(row=0, column=1, padx=5, pady=5, sticky="w")
            entry_valor_hora = ctk.CTkEntry(grid_frame, width=100)
            entry_valor_hora.grid(row=1, column=1, padx=5, pady=5)
            entry_valor_hora.insert(0, str(row.get('valor_hora', 0.0)))
            
            # Coluna 2
            ctk.CTkLabel(grid_frame, text="Margem Lucro (0-1):", font=("Aileron", 11, "bold")).grid(row=2, column=0, padx=5, pady=5, sticky="w")
            entry_margem = ctk.CTkEntry(grid_frame, width=100)
            entry_margem.grid(row=3, column=0, padx=5, pady=5)
            entry_margem.insert(0, str(row.get('margem_lucro', 0.20)))

            ctk.CTkLabel(card_frame, text="Preço de Venda (R$):", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            entry_preco = ctk.CTkEntry(card_frame, height=35)
            entry_preco.insert(0, str(row['preco']).replace('.', ','))
            entry_preco.pack(pady=5, padx=15, fill="x")

            # --- Indicador de Lucro em Tempo Real ---
            lbl_lucro_live = ctk.CTkLabel(card_frame, text="", font=("Aileron", 13, "bold"), text_color="#28a745")
            lbl_lucro_live.pack(padx=15, pady=(0, 5))

            def _atualizar_lucro_edit(*args):
                try:
                    custo_v = entry_custo_mat.get().strip().replace(",", ".")
                    venda_v = entry_preco.get().strip().replace(",", ".")
                    qtd_v = entry_qtd.get().strip()
                    if not custo_v or not venda_v or not qtd_v:
                        lbl_lucro_live.configure(text="")
                        return
                    custo_f = float(custo_v)
                    venda_f = float(venda_v)
                    qtd_f = int(qtd_v)
                    if custo_f <= 0 and venda_f > 0:
                        lbl_lucro_live.configure(text=f"Lucro unitário: R$ {venda_f:.2f} (sem custo informado)", text_color="#28a745")
                        return
                    if custo_f <= 0:
                        lbl_lucro_live.configure(text="")
                        return
                    lucro_un = venda_f - custo_f
                    lucro_total = lucro_un * qtd_f
                    margem_pct = (lucro_un / venda_f) * 100 if venda_f > 0 else 0
                    cor = "#28a745" if lucro_un >= 0 else "#FF6B6B"
                    lbl_lucro_live.configure(
                        text=f"Lucro unit: R$ {lucro_un:.2f} ({margem_pct:.1f}%)  |  Total estoque: R$ {lucro_total:.2f}",
                        text_color=cor)
                except (ValueError, TypeError):
                    lbl_lucro_live.configure(text="")

            entry_preco.bind("<KeyRelease>", _atualizar_lucro_edit)
            entry_qtd.bind("<KeyRelease>", _atualizar_lucro_edit)

            # --- Código de Barras ---
            ctk.CTkLabel(card_frame, text="Código de Barras:", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            barcode_edit_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
            barcode_edit_frame.pack(padx=15, pady=5, fill="x")
            entry_codigo_edit = ctk.CTkEntry(barcode_edit_frame, height=35)
            current_code = row.get('codigo_barras', '') or row.get('codigo', '')
            if current_code:  entry_codigo_edit.insert(0, current_code)
            entry_codigo_edit.pack(side="left", fill="x", expand=True, padx=(0, 5))
            # --------------------------------

            # --- Lote / Nº de Série ---
            ctk.CTkLabel(card_frame, text="Lote / Nº de Série:", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(10, 3), padx=15, anchor="w")
            entry_lote_edit = ctk.CTkEntry(card_frame, height=35, placeholder_text="Lote, IMEI, SIF...")
            current_lote = row.get('lote_serie', '') or ''
            if current_lote:  entry_lote_edit.insert(0, current_lote)
            entry_lote_edit.pack(padx=15, pady=(0, 5), fill="x")
            # --------------------------------

            # --- Seção de Imagem (Update) ---
            ctk.CTkLabel(card_frame, text="Imagem do Produto:", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            
            self.temp_image_path = None # Para edição
            current_img = row.get('imagem')
            
            img_display_text = current_img if current_img else "Sem imagem"
            lbl_img_path = ctk.CTkLabel(card_frame, text=img_display_text, font=ctk.CTkFont(family="Aileron", size=11, slant="italic"), text_color="gray")
            lbl_img_path.pack(pady=2, padx=15, anchor="w")
            
            def alterar_imagem():
                dialog.attributes("-topmost", False)
                file_path = filedialog.askopenfilename(title="Selecionar Imagem", filetypes=[("Imagens", "*.png *.jpg *.jpeg *.bmp *.gif")])
                dialog.attributes("-topmost", True)
                if file_path:
                    self.temp_image_path = file_path
                    lbl_img_path.configure(text=os.path.basename(file_path))
                    dialog.lift()

            ctk.CTkButton(card_frame, text="Alterar Imagem", command=alterar_imagem, height=30,
                        fg_color="#5DADE2", text_color="#001D39", hover_color="#3498DB",
                        font=("Aileron", 11, "bold")).pack(pady=5, padx=15, fill="x")

            def calcular_sugestao():
                try:
                    custo_mat = float(entry_custo_mat.get().replace(',', '.'))
                    horas = float(entry_horas.get().replace(',', '.'))
                    v_hora = float(entry_valor_hora.get().replace(',', '.'))
                    margem = float(entry_margem.get().replace(',', '.'))
                    
                    engine = ai_module.PricingEngine()
                    resultado = engine.calcular_preco_etico(custo_mat, horas, v_hora, margem, 0.0, 1.0)
                    
                    entry_preco.delete(0, "end")
                    entry_preco.insert(0, f"{resultado['preco_sugerido']:.2f}")
                    _atualizar_lucro_edit()
                    messagebox.showinfo("Cálculo Realizado", f"Preço Sugerido: R$ {resultado['preco_sugerido']:.2f}\n\nDetalhes:\n{resultado['detalhes']}", parent=dialog)
                except ValueError:
                    messagebox.showerror("Erro", "Verifique se todos os campos numéricos estão preenchidos corretamente.", parent=dialog)

            ctk.CTkLabel(grid_frame, text="Custo Material (R$):", font=("Aileron", 11, "bold")).grid(row=2, column=1, padx=5, pady=5, sticky="w")
            entry_custo_mat = ctk.CTkEntry(grid_frame, width=100)
            entry_custo_mat.grid(row=3, column=1, padx=5, pady=5)
            entry_custo_mat.insert(0, str(row.get('preco_compra', 0.0) or 0.0))

            # Bind custo ao indicador de lucro
            entry_custo_mat.bind("<KeyRelease>", _atualizar_lucro_edit)
            _atualizar_lucro_edit()

            ctk.CTkButton(card_frame, text="Calcular Preço Sugerido", command=calcular_sugestao, fg_color="#5DADE2", text_color="#001D39", height=30).pack(pady=10, padx=15, fill="x")
            ctk.CTkButton(card_frame, text="🔍 Visualizar QR Code", command=lambda: self.show_product_qrcode_dialog(row), fg_color="#6EA2B3", text_color="white", height=30, font=("Aileron", 11, "bold")).pack(pady=5, padx=15, fill="x")
            
            def salvar_produto():
                nome = entry_nome.get().strip()
                qtd = entry_qtd.get().strip()
                preco = entry_preco.get().strip()
                categoria = categoria_var.get()
                codigo_barras_final = entry_codigo_edit.get().strip()
                lote_serie_final = entry_lote_edit.get().strip()
                
                try:
                    qtd_int = int(qtd)
                    preco_float = float(preco.replace(',', '.'))
                    novo_custo = float(entry_custo_mat.get().replace(',', '.'))
                    
                    # Novos campos
                    horas = float(entry_horas.get().replace(',', '.'))
                    v_hora = float(entry_valor_hora.get().replace(',', '.'))
                    margem = float(entry_margem.get().replace(',', '.'))

                    # Processar Imagem Nova
                    imagem_final = row.get('imagem') # Manter a antiga se não houver mudança
                    if self.temp_image_path:
                        
                        img_dir = os.path.join(self.db.data_dir, "product_images")
                        if not os.path.exists(img_dir): os.makedirs(img_dir)
                        
                        ext = os.path.splitext(self.temp_image_path)[1]
                        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                        nome_seguro = "".join([c for c in nome if c.isalnum() or c in (' ', '-', '_')]).strip()
                        novo_nome = f"prod_{nome_seguro}_{timestamp}{ext}"
                        dest_path = os.path.join(img_dir, novo_nome)
                        
                        try:
                            shutil.copy2(self.temp_image_path, dest_path)
                            imagem_final = novo_nome
                        except Exception as e:
                            logger.warning("Erro ao copiar imagem: %s", e)

                    # Atualizar preco_compra no banco
                    old_custo = float(row.get('preco_compra', 0) or 0)
                    old_preco = float(row.get('preco', 0) or 0)
                    old_qtd = int(row.get('quantidade', 0) or 0)

                    self.db.atualizar_produto(row['id'], nome, qtd_int, preco_float, categoria, horas, v_hora, margem, 0.0, 1.0, imagem=imagem_final, codigo_barras=codigo_barras_final, lote_serie=lote_serie_final)

                    # Atualizar preco_compra separadamente
                    try:
                        cursor = self.db.conn.cursor()
                        cursor.execute("UPDATE produtos SET preco_compra = ? WHERE id = ?", (novo_custo, row['id']))
                        self.db.conn.commit()
                    except Exception:
                        pass

                    # Registrar impacto financeiro se houve mudança significativa
                    mudancas = []
                    if abs(novo_custo - old_custo) > 0.01:
                        mudancas.append(f"Custo: R$ {old_custo:.2f} → R$ {novo_custo:.2f}")
                    if abs(preco_float - old_preco) > 0.01:
                        mudancas.append(f"Preço venda: R$ {old_preco:.2f} → R$ {preco_float:.2f}")
                    if qtd_int != old_qtd:
                        mudancas.append(f"Estoque: {old_qtd} → {qtd_int}")

                    if mudancas:
                        self.db.registrar_alteracao("Editar Produto (Financeiro)", f"'{nome}': {'; '.join(mudancas)}")

                    # Se quantidade aumentou e custo > 0, registrar gasto de reposição
                    if qtd_int > old_qtd and novo_custo > 0:
                        qtd_adicionada = qtd_int - old_qtd
                        gasto_reposicao = qtd_adicionada * novo_custo
                        self.db.adicionar_gasto(
                            datetime.date.today().isoformat(),
                            gasto_reposicao,
                            "Reposição de Estoque",
                            f"Reposição: +{qtd_adicionada}x {nome} (R$ {novo_custo:.2f}/un)"
                        )

                    messagebox.showinfo("Sucesso", "Produto atualizado!", parent=dialog)
                    dialog.destroy()
                    self.refresh_dashboard()
                except ValueError:
                    messagebox.showerror("Erro", "Valores inválidos.", parent=dialog)
            
            cmd_salvar = salvar_produto

        # Botão Salvar
        if cmd_salvar:
            button_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
            button_frame.pack(pady=20, padx=15, fill="x")
            ctk.CTkButton(button_frame, text="Salvar Alterações", command=cmd_salvar, height=40,
                        fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F",
                        font=("Aileron", 14, "bold")).pack(fill="x")

    def open_gasto_manager(self):
        window = ctk.CTkToplevel(self)
        window.title("Gestão de Gastos")
        window.geometry("700x650")
        window.attributes("-topmost", True)
        window.grab_set()

        # Frame principal
        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)

        # Cabeçalho
        ctk.CTkLabel(main_frame, text="Gestão de Gastos", font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 20))

        # Adicionar Gasto Frame
        add_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        add_frame.pack(fill="x", pady=(0, 20))
        
        ctk.CTkLabel(add_frame, text="Novo Gasto", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(pady=(10, 5), padx=15, anchor="w")

        # Grid para inputs
        grid = ctk.CTkFrame(add_frame, fg_color="transparent")
        grid.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(grid, text="Valor (R$):", text_color=("#001D39", "white")).grid(row=0, column=0, ipady=5, sticky="w")
        entry_val = ctk.CTkEntry(grid, width=100)
        entry_val.grid(row=1, column=0, padx=5)

        ctk.CTkLabel(grid, text="Data (YYYY-MM-DD):", text_color=("#001D39", "white")).grid(row=0, column=1, ipady=5, sticky="w")
        entry_date = ctk.CTkEntry(grid, width=120)
        entry_date.insert(0, datetime.date.today().isoformat())
        entry_date.grid(row=1, column=1, padx=5)
        
        ctk.CTkLabel(grid, text="Categoria:", text_color=("#001D39", "white")).grid(row=0, column=2, ipady=5, sticky="w")
        from services.financeiro_service import FinanceiroService

        fin_svc = FinanceiroService(self.db)
        cat_var = ctk.StringVar(value="Material")
        gasto_cats = fin_svc.listar_categorias_gasto()
        cat_menu = ctk.CTkComboBox(grid, values=gasto_cats, variable=cat_var, width=120)
        cat_menu.grid(row=1, column=2, padx=5)

        ctk.CTkLabel(add_frame, text="Descrição:", font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).pack(pady=(5, 0), padx=15, anchor="w")
        entry_desc = ctk.CTkEntry(add_frame)
        entry_desc.pack(fill="x", padx=15, pady=5)

        def add_gasto():
            if self.check_trial_limits('transacao', window):
                return
            try:
                fin_svc.adicionar_gasto(
                    data=entry_date.get(),
                    valor=entry_val.get(),
                    categoria=cat_var.get(),
                    descricao=entry_desc.get(),
                )
                messagebox.showinfo("Sucesso", "Gasto registrado!", parent=window)
                entry_val.delete(0, "end")
                entry_desc.delete(0, "end")
                refresh_gastos()
                self.refresh_dashboard()
            except ValueError as exc:
                messagebox.showerror("Erro", str(exc), parent=window)

        ctk.CTkButton(add_frame, text="Adicionar Gasto", command=add_gasto, fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F").pack(pady=10, padx=15, fill="x")

        # Lista Gastos
        list_frame = ctk.CTkScrollableFrame(main_frame, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        list_frame.pack(fill="both", expand=True)

        def refresh_gastos():
            for w in list_frame.winfo_children(): w.destroy()
            gastos = fin_svc.listar_gastos(invertido=True)
            if not gastos:
                ctk.CTkLabel(list_frame, text="Nenhum gasto registrado.", text_color=("#999999", "#AAAAAA")).pack(pady=20)
            else:
                for row in gastos:
                    f = ctk.CTkFrame(list_frame, fg_color=("white", "#404040"))
                    f.pack(fill="x", pady=2, padx=5)
                    txt = f"{row['data']} - {row['categoria']}: R$ {row['valor']:.2f} ({row['descricao']})"
                    ctk.CTkLabel(f, text=txt, anchor="w", text_color=("#333333", "white")).pack(side="left", padx=10)
                    
                    def del_g(gasto_id=row['id']):
                        if messagebox.askyesno("Confirmar", "Deletar gasto?", parent=window):
                            fin_svc.remover_gasto(gasto_id)
                            refresh_gastos()
                            self.refresh_dashboard()

                    ctk.CTkButton(f, text="X", width=30, fg_color="red", command=del_g).pack(side="right", padx=5, pady=5)

        refresh_gastos()

    def open_add_product_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Adicionar Novo Produto")
        dialog.geometry("750x850")
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        main_frame = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)

        ctk.CTkLabel(main_frame, text="Gestão de Produtos", font=("Aileron", 20, "bold"), text_color=("#001D39", "white")).pack(pady=(0, 10))

        # Botões de Importação Lote
        import_btns_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        import_btns_frame.pack(fill="x", pady=(0, 15))
        
        ctk.CTkButton(import_btns_frame, text="📂 Importar Produtos (CSV/PDF)", command=lambda: (dialog.destroy(), self.open_importar_csv()),
                        height=35, fg_color=("#EBEBF0", "#3A3A3A"), text_color=("#333", "white"),
                        hover_color=("#D9D9E0", "#4A4A4A"), font=("Aileron", 11, "bold")).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(import_btns_frame, text="🧾 Importar NF-e (XML)", command=lambda: (dialog.destroy(), self.open_importar_nfe_xml()),
                        height=35, fg_color="#0A4174", text_color="white",
                        hover_color="#062F56", font=("Aileron", 11, "bold")).pack(side="left", fill="x", expand=True, padx=4)
                        
        ctk.CTkButton(import_btns_frame, text="📸 Importar Imagens Lote", command=lambda: (dialog.destroy(), self.open_importar_imagens_lote()),
                        height=35, fg_color=("#EBEBF0", "#3A3A3A"), text_color=("#333", "white"),
                        hover_color=("#D9D9E0", "#4A4A4A"), font=("Aileron", 11, "bold")).pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ===== CARD PRINCIPAL: ADICIONAR AO PRODUTO EXISTENTE (PRIMEIRO!) =====
        existing_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15, border_width=2, border_color="#0A4174")
        existing_card.pack(fill="x", pady=(0, 20))

        # Header com destaque
        exist_header = ctk.CTkFrame(existing_card, fg_color="#0A4174", corner_radius=12, height=45)
        exist_header.pack(fill="x", padx=3, pady=(3, 0))
        exist_header.pack_propagate(False)
        ctk.CTkLabel(exist_header, text="📦  Adicionar Estoque a Produto Existente", font=("Aileron", 15, "bold"), text_color="white").pack(pady=10, padx=15, anchor="w")

        ctk.CTkLabel(existing_card, text="Filtre por categoria e selecione o produto para reabastecer:", font=("Aileron", 11, "italic"), text_color=("gray40", "gray70")).pack(padx=15, pady=(10, 5), anchor="w")

        # Filtros: Categoria + Subcategoria
        filter_frame = ctk.CTkFrame(existing_card, fg_color="transparent")
        filter_frame.pack(fill="x", padx=15, pady=5)
        filter_frame.columnconfigure(0, weight=1)
        filter_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(filter_frame, text="Categoria:", font=("Aileron", 11, "bold")).grid(row=0, column=0, sticky="w", padx=5)
        exist_cats = ["Todas"] + (self.db.listar_categorias('produto') or [])
        combo_cat_filter = ctk.CTkComboBox(filter_frame, values=exist_cats, state="readonly")
        combo_cat_filter.set("Todas")
        combo_cat_filter.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))

        ctk.CTkLabel(filter_frame, text="Subcategoria:", font=("Aileron", 11, "bold")).grid(row=0, column=1, sticky="w", padx=5)
        combo_subcat_filter = ctk.CTkComboBox(filter_frame, values=["Todas"], state="readonly")
        combo_subcat_filter.set("Todas")
        combo_subcat_filter.grid(row=1, column=1, sticky="ew", padx=5, pady=(0, 5))

        # Busca por nome
        ctk.CTkLabel(existing_card, text="Buscar:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=20, pady=(5, 0))
        e_busca_exist = ctk.CTkEntry(existing_card, height=35, placeholder_text="🔍 Digite o nome ou código do produto...")
        e_busca_exist.pack(fill="x", padx=20, pady=(2, 5))

        # Lista de produtos filtrada
        exist_list_frame = ctk.CTkScrollableFrame(existing_card, fg_color=("#F5F5F5", "#333333"), corner_radius=10, height=150)
        exist_list_frame.pack(fill="x", padx=15, pady=5)

        selected_exist_product = {"id": None, "nome": None, "row": None}

        try:
            df_exist = self.db.listar_produtos()
        except Exception:
            df_exist = pd.DataFrame()

        def _filtrar_produtos(*args):
            for w in exist_list_frame.winfo_children():
                w.destroy()
            selected_exist_product["id"] = None
            selected_exist_product["nome"] = None
            selected_exist_product["row"] = None

            if df_exist.empty:
                ctk.CTkLabel(exist_list_frame, text="Nenhum produto cadastrado.", text_color="gray").pack(pady=10)
                return

            cat_filtro = combo_cat_filter.get()
            subcat_filtro = combo_subcat_filter.get()
            busca = e_busca_exist.get().strip().lower()

            filtered = df_exist.copy()
            if cat_filtro != "Todas":
                filtered = filtered[filtered['categoria'] == cat_filtro]
            if subcat_filtro != "Todas" and 'subcategoria' in filtered.columns:
                filtered = filtered[filtered['subcategoria'] == subcat_filtro]
            if busca:
                mask = filtered['nome'].str.lower().str.contains(busca, na=False)
                if 'codigo_barras' in filtered.columns:
                    mask = mask | filtered['codigo_barras'].fillna('').str.lower().str.contains(busca, na=False)
                filtered = filtered[mask]

            if filtered.empty:
                ctk.CTkLabel(exist_list_frame, text="Nenhum produto encontrado com esses filtros.", text_color="gray").pack(pady=10)
                return

            for _, prod_row in filtered.iterrows():
                item_frame = ctk.CTkFrame(exist_list_frame, fg_color=("white", "#404040"), corner_radius=8, height=40)
                item_frame.pack(fill="x", pady=2, padx=3)
                item_frame.pack_propagate(False)

                # Thumbnail
                icon_img = None
                if 'imagem' in prod_row and prod_row['imagem']:
                    try:
                        img_p = os.path.join(self.db.data_dir, "product_images", prod_row['imagem'])
                        if os.path.exists(img_p):
                            pil_img = Image.open(img_p)
                            icon_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(28, 28))
                    except Exception:
                        pass

                cat_txt = f" [{prod_row['categoria']}]" if prod_row.get('categoria') else ""
                estoque_cor = "#28a745" if prod_row['quantidade'] > 5 else ("#E67E22" if prod_row['quantidade'] > 0 else "#FF6B6B")
                display_text = f"{prod_row['nome']}{cat_txt}  •  Estoque: {prod_row['quantidade']}  •  R$ {prod_row['preco']:.2f}"

                def _selecionar(pid=prod_row['id'], pnome=prod_row['nome'], prow=prod_row, frame=item_frame):
                    selected_exist_product["id"] = pid
                    selected_exist_product["nome"] = pnome
                    selected_exist_product["row"] = prow
                    lbl_selecionado.configure(text=f"✅ Selecionado: {pnome}", text_color="#28a745")
                    # Reset visual de todos
                    for child in exist_list_frame.winfo_children():
                        if isinstance(child, ctk.CTkFrame):
                            child.configure(fg_color=("white", "#404040"))
                    frame.configure(fg_color=("#D6EAF8", "#1A3A5C"))

                btn = ctk.CTkButton(item_frame, text=display_text, image=icon_img, compound="left",
                                    fg_color="transparent", hover_color=("#D6EAF8", "#1A3A5C"),
                                    text_color=(estoque_cor, "white"), anchor="w", font=("Aileron", 11),
                                    command=_selecionar)
                btn.pack(fill="both", expand=True, padx=3, pady=2)

        def _atualizar_subcat_filter(*args):
            cat_sel = combo_cat_filter.get()
            if cat_sel == "Todas":
                combo_subcat_filter.configure(values=["Todas"])
            else:
                subcats = self.db.listar_subcategorias(cat_sel, 'produto')
                combo_subcat_filter.configure(values=["Todas"] + (subcats or []))
            combo_subcat_filter.set("Todas")
            _filtrar_produtos()

        combo_cat_filter.configure(command=lambda v: _atualizar_subcat_filter())
        combo_subcat_filter.configure(command=lambda v: _filtrar_produtos())
        e_busca_exist.bind("<KeyRelease>", _filtrar_produtos)
        _filtrar_produtos()

        lbl_selecionado = ctk.CTkLabel(existing_card, text="Nenhum produto selecionado", font=("Aileron", 11, "italic"), text_color=("gray40", "gray70"))
        lbl_selecionado.pack(padx=15, pady=(5, 0))

        # Quantidade + Botão
        exist_action_frame = ctk.CTkFrame(existing_card, fg_color="transparent")
        exist_action_frame.pack(fill="x", padx=15, pady=(5, 10))
        exist_action_frame.columnconfigure(0, weight=1)
        exist_action_frame.columnconfigure(1, weight=2)

        ctk.CTkLabel(exist_action_frame, text="Qtd a adicionar:", font=("Aileron", 11, "bold")).grid(row=0, column=0, sticky="w", padx=5)
        e_add_qtd = ctk.CTkEntry(exist_action_frame, placeholder_text="0", height=38)
        e_add_qtd.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))

        def _adicionar_ao_existente():
            if not selected_exist_product["id"]:
                messagebox.showerror("Erro", "Selecione um produto da lista.", parent=dialog)
                return
            try:
                qtd_add = int(e_add_qtd.get())
                if qtd_add <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Erro", "Informe uma quantidade válida.", parent=dialog)
                return
            try:
                prod_row = selected_exist_product["row"]
                nova_qtd = int(prod_row['quantidade']) + qtd_add
                self.db.atualizar_produto(
                    int(prod_row['id']), prod_row['nome'], nova_qtd, prod_row['preco'], prod_row['categoria'],
                    prod_row.get('horas_trabalho', 0), prod_row.get('valor_hora', 0),
                    margem_lucro=prod_row.get('margem_lucro', 0.20),
                    fator_risco=prod_row.get('fator_risco', 0.10),
                    fator_raridade=prod_row.get('fator_raridade', 1.0),
                    imagem=prod_row.get('imagem'), codigo_barras=prod_row.get('codigo_barras')
                )
                messagebox.showinfo("Sucesso", f"+{qtd_add} unidades adicionadas a '{selected_exist_product['nome']}'!\nNovo estoque: {nova_qtd}", parent=dialog)
                e_add_qtd.delete(0, "end")
                # Recarregar lista
                nonlocal df_exist
                try:
                    df_exist = self.db.listar_produtos()
                except Exception:
                    pass
                _filtrar_produtos()
                self.refresh_dashboard()
            except Exception as e:
                messagebox.showerror("Erro", f"Falha ao atualizar: {e}", parent=dialog)

        ctk.CTkButton(exist_action_frame, text="⬆ ADICIONAR AO ESTOQUE", command=_adicionar_ao_existente,
                      height=38, fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 12, "bold")).grid(row=1, column=1, sticky="ew", padx=5, pady=(0, 5))

        # ===== SEPARADOR =====
        sep_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        sep_frame.pack(fill="x", pady=5)
        ctk.CTkFrame(sep_frame, height=2, fg_color=("#CCCCCC", "#555555")).pack(fill="x", padx=20)
        ctk.CTkLabel(sep_frame, text="─ ou cadastre um novo produto ─", font=("Aileron", 11), text_color=("gray50", "gray60")).pack(pady=5)

        # --- Cards ---
        basic_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        basic_card.pack(fill="x", pady=(0, 15))
        
        pricing_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        pricing_card.pack(fill="x", pady=(0, 15))

        # --- BASIC INFO ---
        ctk.CTkLabel(basic_card, text="Informações Básicas", font=("Aileron", 14, "bold"), text_color="#7BBDE8").pack(pady=10, padx=15, anchor="w")
        
        entries = {}
        
        # Grid layout for basic info
        grid_basic = ctk.CTkFrame(basic_card, fg_color="transparent")
        grid_basic.pack(fill="x", padx=10, pady=5)
        
        # Nome (Full width)
        ctk.CTkLabel(grid_basic, text="Nome do Produto:", font=("Aileron", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=5)
        e_nome = ctk.CTkEntry(grid_basic, width=400)
        e_nome.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=(0, 10))
        entries["Nome"] = e_nome

        # Barcode (New)
        ctk.CTkLabel(grid_basic, text="Código de Barras (Leitor):", font=("Aileron", 11, "bold")).grid(row=2, column=0, sticky="w", padx=5)
        e_barcode = ctk.CTkEntry(grid_basic, placeholder_text="Scaneie aqui...")
        e_barcode.grid(row=3, column=0, sticky="ew", padx=5, pady=(0, 10))
        entries["Codigo"] = e_barcode
        
        # Lote/Série (rastreabilidade)
        ctk.CTkLabel(grid_basic, text="Lote/Nº de Série:", font=("Aileron", 11, "bold")).grid(row=2, column=1, sticky="w", padx=5)
        e_lote = ctk.CTkEntry(grid_basic, placeholder_text="Lote, IMEI, SIF...")
        e_lote.grid(row=3, column=1, sticky="ew", padx=5, pady=(0, 10))
        entries["Lote"] = e_lote
        
        # Qtd & Categoria
        ctk.CTkLabel(grid_basic, text="Quantidade:", font=("Aileron", 11, "bold")).grid(row=4, column=0, sticky="w", padx=5)
        e_qtd = ctk.CTkEntry(grid_basic)
        e_qtd.grid(row=5, column=0, sticky="ew", padx=5, pady=(0, 10))
        entries["Quantidade"] = e_qtd
        
        ctk.CTkLabel(grid_basic, text="Categoria:", font=("Aileron", 11, "bold")).grid(row=4, column=1, sticky="w", padx=5)
        produto_cats = self.db.listar_categorias('produto') or ["Produto Pronto", "Material", "Insumo", "Outro"]
        e_cat = ctk.CTkComboBox(grid_basic, values=produto_cats)
        e_cat.grid(row=5, column=1, sticky="ew", padx=5, pady=(0, 10))
        entries["Categoria"] = e_cat

        ctk.CTkLabel(grid_basic, text="Estoque Mínimo:", font=("Aileron", 11, "bold")).grid(row=6, column=0, sticky="w", padx=5)
        e_estoque_min = ctk.CTkEntry(grid_basic, placeholder_text="5")
        e_estoque_min.insert(0, "5")
        e_estoque_min.grid(row=7, column=0, sticky="ew", padx=5, pady=(0, 10))
        entries["EstoqueMinimo"] = e_estoque_min

        # Preço de Compra (visível para todas as modalidades)
        ctk.CTkLabel(grid_basic, text="Preço de Compra (R$):", font=("Aileron", 11, "bold")).grid(row=6, column=1, sticky="w", padx=5)
        e_preco_compra = ctk.CTkEntry(grid_basic, placeholder_text="0.00")
        e_preco_compra.grid(row=7, column=1, sticky="ew", padx=5, pady=(0, 10))
        entries["PrecoCompra"] = e_preco_compra

        # Preço de Venda (ao lado do Preço de Compra)
        ctk.CTkLabel(grid_basic, text="Preço de Venda (R$):", font=("Aileron", 11, "bold")).grid(row=8, column=0, sticky="w", padx=5)
        e_preco = ctk.CTkEntry(grid_basic, placeholder_text="0.00")
        e_preco.grid(row=9, column=0, sticky="ew", padx=5, pady=(0, 10))
        entries["Preço Venda"] = e_preco

        # Tipo de Produto e Sob Encomenda (condicional por tipo de negócio)
        e_tipo_produto = None
        e_sob_encomenda = None
        next_row = 10
        if self.business_type in ("padaria_acougue", "artesanato"):
            ctk.CTkLabel(grid_basic, text="Tipo de Produto:", font=("Aileron", 11, "bold")).grid(row=next_row, column=0, sticky="w", padx=5)
            e_tipo_produto = ctk.CTkComboBox(grid_basic, values=["produto", "insumo"], state="readonly")
            e_tipo_produto.set("produto")
            e_tipo_produto.grid(row=next_row+1, column=0, sticky="ew", padx=5, pady=(0, 10))
            next_row += 2
        if self.business_type == "artesanato":
            ctk.CTkLabel(grid_basic, text="Sob Encomenda:", font=("Aileron", 11, "bold")).grid(row=next_row-2, column=1, sticky="w", padx=5)
            e_sob_encomenda = ctk.CTkComboBox(grid_basic, values=["Não", "Sim"], state="readonly")
            e_sob_encomenda.set("Não")
            e_sob_encomenda.grid(row=next_row-1, column=1, sticky="ew", padx=5, pady=(0, 10))

        # Subcategoria (dinâmica baseada na categoria selecionada)
        ctk.CTkLabel(grid_basic, text="Subcategoria:", font=("Aileron", 11, "bold")).grid(row=next_row, column=0, sticky="w", padx=5)
        e_subcat = ctk.CTkComboBox(grid_basic, values=["(nenhuma)"])
        e_subcat.grid(row=next_row+1, column=0, sticky="ew", padx=5, pady=(0, 10))
        entries["Subcategoria"] = e_subcat

        # Tamanho (dinâmico baseado na subcategoria)
        ctk.CTkLabel(grid_basic, text="Tamanho:", font=("Aileron", 11, "bold")).grid(row=next_row, column=1, sticky="w", padx=5)
        e_tamanho = ctk.CTkComboBox(grid_basic, values=["(nenhum)"])
        e_tamanho.grid(row=next_row+1, column=1, sticky="ew", padx=5, pady=(0, 10))
        entries["Tamanho"] = e_tamanho

        # Sub-subcategoria (dinâmica baseada na subcategoria)
        ctk.CTkLabel(grid_basic, text="Sub-Subcategoria:", font=("Aileron", 11, "bold")).grid(row=next_row+2, column=0, sticky="w", padx=5)
        e_sub_subcat = ctk.CTkComboBox(grid_basic, values=["(nenhuma)"])
        e_sub_subcat.grid(row=next_row+3, column=0, sticky="ew", padx=5, pady=(0, 10))
        entries["SubSubcategoria"] = e_sub_subcat

        def _atualizar_subcategorias(*args):
            cat_sel = e_cat.get()
            subcats = self.db.listar_subcategorias(cat_sel, 'produto')
            if subcats:
                e_subcat.configure(values=["(nenhuma)"] + subcats)
            else:
                e_subcat.configure(values=["(nenhuma)"])
            e_subcat.set("(nenhuma)")
            e_tamanho.configure(values=["(nenhum)"])
            e_tamanho.set("(nenhum)")
            e_sub_subcat.configure(values=["(nenhuma)"])
            e_sub_subcat.set("(nenhuma)")

        def _atualizar_tamanhos(*args):
            cat_sel = e_cat.get()
            sub_sel = e_subcat.get()
            if sub_sel and sub_sel != "(nenhuma)":
                tams = self.db.listar_tamanhos(cat_sel, sub_sel, 'produto')
                if tams:
                    e_tamanho.configure(values=["(nenhum)"] + tams)
                else:
                    e_tamanho.configure(values=["(nenhum)"])
                # Atualizar sub-subcategorias
                sub_subs = self.db.listar_sub_subcategorias(cat_sel, sub_sel, 'produto')
                if sub_subs:
                    e_sub_subcat.configure(values=["(nenhuma)"] + sub_subs)
                else:
                    e_sub_subcat.configure(values=["(nenhuma)"])
            else:
                e_tamanho.configure(values=["(nenhum)"])
                e_sub_subcat.configure(values=["(nenhuma)"])
            e_tamanho.set("(nenhum)")
            e_sub_subcat.set("(nenhuma)")

        e_cat.configure(command=lambda val: _atualizar_subcategorias())
        e_subcat.configure(command=lambda val: _atualizar_tamanhos())
        _atualizar_subcategorias()

        # --- Calculadora de Lucro ao Vivo ---
        lbl_live_margin = ctk.CTkLabel(basic_card, text="", font=("Aileron", 12, "bold"), text_color="#28a745")
        lbl_live_margin.pack(padx=15, pady=(0, 5), anchor="w")

        def _calcular_margem_live(*args):
            from services.precificacao_service import PrecificacaoService
            custo_v = e_preco_compra.get().strip()
            venda_v = e_preco.get().strip()
            if not custo_v or not venda_v:
                lbl_live_margin.configure(text="")
                return
            try:
                res = PrecificacaoService().calcular_margem(custo_v, venda_v)
                cor = "#28a745" if res["lucro"] >= 0 else "#FF6B6B"
                lbl_live_margin.configure(
                    text=f"💰 Lucro: R$ {res['lucro']:.2f}  |  Margem: {res['margem_pct']:.1f}%  |  Markup: {res['markup_pct']:.1f}%",
                    text_color=cor
                )
            except ValueError:
                lbl_live_margin.configure(text="")

        e_preco_compra.bind("<KeyRelease>", _calcular_margem_live)
        e_preco.bind("<KeyRelease>", _calcular_margem_live)

        # Imagem
        self.temp_image_path = None
        lbl_img = ctk.CTkLabel(basic_card, text="Sem imagem selecionada", font=("Aileron", 10, "italic"))
        lbl_img.pack(pady=5)
        
        def pick_img():
            dialog.attributes("-topmost", False) # Necessário para dialog file
            p = filedialog.askopenfilename(filetypes=[("Imagens", "*.png *.jpg *.jpeg")])
            dialog.attributes("-topmost", True)
            if p:
                self.temp_image_path = p
                lbl_img.configure(text=os.path.basename(p))
                dialog.lift()

        ctk.CTkButton(basic_card, text="Selecionar Imagem", command=pick_img, fg_color="#5DADE2", text_color="#001D39", height=30).pack(fill="x", padx=15, pady=(0, 15))

        # --- UNIDADE DE MEDIDA (Padaria/Açougue/Construção/Pet/Farmácia) ---
        e_unidade = None
        e_preco_peso = None
        if self.business_type in ("padaria_acougue", "casa_construcao", "pet_shop_agro", "farmacia_suplementos"):
            weight_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
            weight_card.pack(fill="x", pady=(0, 15))
            card_title = "Unidade / Peso / Medida" if self.business_type in ("casa_construcao", "pet_shop_agro", "farmacia_suplementos") else "Venda por Peso"
            ctk.CTkLabel(weight_card, text=card_title, font=("Aileron", 14, "bold"), text_color="#D4A373").pack(pady=10, padx=15, anchor="w")
            ctk.CTkLabel(weight_card, text="Configure a unidade de medida e preço de referência:", font=("Aileron", 10, "italic"), text_color="gray").pack(padx=15, anchor="w")
            w_grid = ctk.CTkFrame(weight_card, fg_color="transparent")
            w_grid.pack(fill="x", padx=15, pady=10)
            w_grid.columnconfigure(0, weight=1)
            w_grid.columnconfigure(1, weight=1)
            ctk.CTkLabel(w_grid, text="Unidade de Medida:", font=("Aileron", 11, "bold")).grid(row=0, column=0, sticky="w", padx=5)
            if self.business_type == "casa_construcao":
                unidades_list = ["un", "m", "kg", "g", "L", "mL", "barra", "rolo", "pacote", "saco"]
            elif self.business_type == "pet_shop_agro":
                unidades_list = ["un", "kg", "g", "saco (15kg)", "saco (20kg)", "frasco", "dose", "pacote"]
            elif self.business_type == "farmacia_suplementos":
                unidades_list = ["un", "cx", "frasco", "blister", "pote", "g", "kg", "dose"]
            else:
                unidades_list = ["un", "kg", "g", "L", "mL"]
            e_unidade = ctk.CTkComboBox(w_grid, values=unidades_list, state="readonly")
            e_unidade.set("un")
            e_unidade.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 10))
            preco_label = "Preço por Unid/Kg (R$):" if self.business_type != "padaria_acougue" else "Preço por Kg (R$):"
            ctk.CTkLabel(w_grid, text=preco_label, font=("Aileron", 11, "bold")).grid(row=0, column=1, sticky="w", padx=5)
            e_preco_peso = ctk.CTkEntry(w_grid, placeholder_text="0.00")
            e_preco_peso.grid(row=1, column=1, sticky="ew", padx=5, pady=(0, 10))

        # --- ESTOQUE POR TAMANHO (P/M/G/GG) — Loja de Roupas e Moda / Customização ---
        size_entries = {}
        if self.business_type in ("loja_roupa", "moda_customizacao"):
            size_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
            size_card.pack(fill="x", pady=(0, 15))

            ctk.CTkLabel(size_card, text="Controle de Estoque por Tamanho / Grade", font=("Aileron", 14, "bold"), text_color="#7BBDE8").pack(pady=10, padx=15, anchor="w")
            ctk.CTkLabel(size_card, text="(Opcional - preencha se o produto tem variações de tamanho)", font=("Aileron", 10, "italic"), text_color="gray").pack(padx=15, anchor="w")

            size_grid = ctk.CTkFrame(size_card, fg_color="transparent")
            size_grid.pack(fill="x", padx=15, pady=10)

            for col_idx, size_label in enumerate(["P", "M", "G", "GG"]):
                ctk.CTkLabel(size_grid, text=size_label, font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).grid(row=0, column=col_idx, padx=10, pady=(0, 5))
                e_size = ctk.CTkEntry(size_grid, width=60, placeholder_text="0", justify="center")
                e_size.insert(0, "0")
                e_size.grid(row=1, column=col_idx, padx=10, pady=(0, 10))
                size_entries[size_label] = e_size

        # --- SEÇÃO SAPATOS / CALÇAS / SOB MEDIDA — Loja de Roupas e Moda ---
        shoe_sizes_list = []
        pants_sizes_list = []
        if self.business_type in ("loja_roupa", "moda_customizacao"):
            shoes_pants_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
            shoes_pants_card.pack(fill="x", pady=(0, 15))

            # Container horizontal com separador
            sp_container = ctk.CTkFrame(shoes_pants_card, fg_color="transparent")
            sp_container.pack(fill="x", padx=10, pady=10)
            sp_container.columnconfigure(0, weight=1)
            sp_container.columnconfigure(2, weight=1)

            # === SAPATOS (esquerda) ===
            shoe_frame = ctk.CTkFrame(sp_container, fg_color="transparent")
            shoe_frame.grid(row=0, column=0, sticky="nsew", padx=5)

            ctk.CTkLabel(shoe_frame, text="Sapatos / Tênis", font=("Aileron", 13, "bold"), text_color="#7BBDE8").pack(pady=(0, 5))

            shoe_list_frame = ctk.CTkFrame(shoe_frame, fg_color="transparent")
            shoe_list_frame.pack(fill="x")

            def _add_shoe_row(tam_default=26, qtd_default=0):
                row_frame = ctk.CTkFrame(shoe_list_frame, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)
                ctk.CTkLabel(row_frame, text="Nº", font=("Aileron", 10)).pack(side="left", padx=(0, 2))
                e_tam = ctk.CTkEntry(row_frame, width=55, justify="center")
                e_tam.insert(0, str(tam_default))
                e_tam.pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text="Qtd", font=("Aileron", 10)).pack(side="left", padx=(5, 2))
                e_qtd = ctk.CTkEntry(row_frame, width=55, justify="center")
                e_qtd.insert(0, str(qtd_default))
                e_qtd.pack(side="left", padx=2)
                btn_rm = ctk.CTkButton(row_frame, text="✕", width=28, height=28, fg_color="#FF6B6B",
                                       hover_color="#FF5252", text_color="white",
                                       command=lambda: (_remove_shoe_row(row_frame, entry_data)))
                btn_rm.pack(side="left", padx=5)
                entry_data = {'frame': row_frame, 'tam': e_tam, 'qtd': e_qtd}
                shoe_sizes_list.append(entry_data)

            def _remove_shoe_row(frame, data):
                frame.destroy()
                if data in shoe_sizes_list:
                    shoe_sizes_list.remove(data)

            _add_shoe_row(26, 0)

            ctk.CTkButton(shoe_frame, text="+ Tamanho", height=30, fg_color="#28a745", hover_color="#218838",
                          text_color="white", font=("Aileron", 11, "bold"),
                          command=lambda: _add_shoe_row(26, 0)).pack(pady=(5, 0))

            # === Separador vertical azul ===
            separator = ctk.CTkFrame(sp_container, width=3, fg_color="#7BBDE8")
            separator.grid(row=0, column=1, sticky="ns", padx=8)

            # === CALÇAS (direita) ===
            pants_frame = ctk.CTkFrame(sp_container, fg_color="transparent")
            pants_frame.grid(row=0, column=2, sticky="nsew", padx=5)

            ctk.CTkLabel(pants_frame, text="Calças", font=("Aileron", 13, "bold"), text_color="#7BBDE8").pack(pady=(0, 5))

            pants_list_frame = ctk.CTkFrame(pants_frame, fg_color="transparent")
            pants_list_frame.pack(fill="x")

            def _add_pants_row(tam_default=34, qtd_default=0):
                row_frame = ctk.CTkFrame(pants_list_frame, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)
                ctk.CTkLabel(row_frame, text="Nº", font=("Aileron", 10)).pack(side="left", padx=(0, 2))
                e_tam = ctk.CTkEntry(row_frame, width=55, justify="center")
                e_tam.insert(0, str(tam_default))
                e_tam.pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text="Qtd", font=("Aileron", 10)).pack(side="left", padx=(5, 2))
                e_qtd = ctk.CTkEntry(row_frame, width=55, justify="center")
                e_qtd.insert(0, str(qtd_default))
                e_qtd.pack(side="left", padx=2)
                btn_rm = ctk.CTkButton(row_frame, text="✕", width=28, height=28, fg_color="#FF6B6B",
                                       hover_color="#FF5252", text_color="white",
                                       command=lambda: (_remove_pants_row(row_frame, entry_data)))
                btn_rm.pack(side="left", padx=5)
                entry_data = {'frame': row_frame, 'tam': e_tam, 'qtd': e_qtd}
                pants_sizes_list.append(entry_data)

            def _remove_pants_row(frame, data):
                frame.destroy()
                if data in pants_sizes_list:
                    pants_sizes_list.remove(data)

            _add_pants_row(34, 0)

            ctk.CTkButton(pants_frame, text="+ Tamanho", height=30, fg_color="#28a745", hover_color="#218838",
                          text_color="white", font=("Aileron", 11, "bold"),
                          command=lambda: _add_pants_row(34, 0)).pack(pady=(5, 0))

        # --- PRECIFICAÇÃO INTELIGENTE (IA) ---
        ctk.CTkLabel(pricing_card, text="Precificação Inteligente (IA)", font=("Aileron", 14, "bold"), text_color="#7BBDE8").pack(pady=10, padx=15, anchor="w")
        ctk.CTkLabel(pricing_card, text="Preencha para calcular o preço de venda sugerido pela IA:", font=("Aileron", 10, "italic"), text_color="gray").pack(padx=15, anchor="w")
        
        grid_price = ctk.CTkFrame(pricing_card, fg_color="transparent")
        grid_price.pack(fill="x", padx=10, pady=5)
        grid_price.columnconfigure(0, weight=1)
        grid_price.columnconfigure(1, weight=1)
        grid_price.columnconfigure(2, weight=1)
        grid_price.columnconfigure(3, weight=1)

        fields_config = [
            ("Horas Trabalho", "horas", "0", 0, 0),
            ("Valor Hora (R$)", "v_hora", "0.00", 0, 1),
            ("Margem Lucro (%)", "margem", "20", 0, 2),
            ("Risco (%)", "risco", "10", 0, 3),
        ]
        
        adv_entries = {}
        for label, key, default, r, c in fields_config:
            ctk.CTkLabel(grid_price, text=label, font=("Aileron", 10, "bold")).grid(row=r, column=c, sticky="w", padx=5, pady=(5,0))
            e = ctk.CTkEntry(grid_price)
            e.insert(0, default)
            e.grid(row=r+1, column=c, sticky="ew", padx=5, pady=(0, 10))
            adv_entries[key] = e

        def calcular_sugestao():
            from services.precificacao_service import PrecificacaoService
            try:
                margem = float(adv_entries["margem"].get().replace(",", ".")) / 100.0
                risco = float(adv_entries["risco"].get().replace(",", ".")) / 100.0
                res = PrecificacaoService().sugerir_preco(
                    custo_material=e_preco_compra.get(),
                    horas_trabalho=adv_entries["horas"].get(),
                    valor_hora=adv_entries["v_hora"].get(),
                    margem_lucro=margem,
                    fator_risco=risco,
                    fator_raridade=1.0,
                )

                e_preco.delete(0, "end")
                e_preco.insert(0, f"{res['preco_sugerido']:.2f}")
                _calcular_margem_live()

                messagebox.showinfo("Cálculo Detalhado",
                                f"Preço Sugerido: R$ {res['preco_sugerido']:.2f}\n\n"
                                f"Custo Base: R$ {res['custo_base']:.2f}\n"
                                f"Fórmula: {res['detalhes']}", parent=dialog)
            except ValueError:
                messagebox.showerror("Erro", "Preencha o Preço de Compra e os valores corretamente.", parent=dialog)

        ctk.CTkButton(pricing_card, text="CALCULAR RECOMENDAÇÃO (IA)", command=calcular_sugestao, 
                    fg_color="#0A4174", hover_color="#062F56", height=35).pack(fill="x", padx=15, pady=(5, 15))

        # --- Save ---
        def save():
            if self.check_trial_limits('produto', dialog):
                return
            try:
                nome = entries["Nome"].get().strip()
                if not nome:
                    messagebox.showerror("Erro", "Nome é obrigatório", parent=dialog)
                    return
                
                qtd = int(entries["Quantidade"].get())
                preco_txt = entries["Preço Venda"].get().replace(",", ".")
                preco = float(preco_txt) if preco_txt else 0.0
                cat = entries["Categoria"].get()
                barcode = entries["Codigo"].get().strip()
                lote_serie = entries["Lote"].get().strip()
                
                # Extra data - Preço de Compra (campo principal)
                preco_compra_txt = entries["PrecoCompra"].get().replace(",", ".")
                custo = float(preco_compra_txt) if preco_compra_txt.strip() else 0.0
                horas = float(adv_entries["horas"].get().replace(",", "."))
                v_hora = float(adv_entries["v_hora"].get().replace(",", "."))
                margem = float(adv_entries["margem"].get().replace(",", ".")) / 100.0
                risco = float(adv_entries["risco"].get().replace(",", ".")) / 100.0
                raridade = 1.0
                
                # Image
                img_name = None
                if self.temp_image_path:
                    img_dir = os.path.join(self.db.data_dir, "product_images")
                    if not os.path.exists(img_dir): os.makedirs(img_dir)
                    ext = os.path.splitext(self.temp_image_path)[1]
                    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                    safe_name = "".join([c for c in nome if c.isalnum()]).strip()
                    img_name = f"prod_{safe_name}_{ts}{ext}"
                    try:
                        shutil.copy2(self.temp_image_path, os.path.join(img_dir, img_name))
                    except Exception as e: logger.warning("Erro ao copiar imagem do produto: %s", e)

                self.db.adicionar_produto(
                    nome, qtd, preco, cat, 
                    preco_compra=custo, imagem=img_name,
                    horas_trabalho=horas, valor_hora=v_hora,
                    margem_lucro=margem, fator_risco=risco, fator_raridade=raridade,
                    codigo_barras=barcode
                )
                
                # Salvar estoque mínimo configurado
                estoque_min_val = entries["EstoqueMinimo"].get().strip()
                subcat_val = entries["Subcategoria"].get().strip()
                tamanho_val = entries["Tamanho"].get().strip()
                sub_subcat_val = entries["SubSubcategoria"].get().strip()

                # Buscar id do produto recém inserido
                cursor = self.db.conn.cursor()
                cursor.execute("SELECT id FROM produtos WHERE nome = ? ORDER BY id DESC LIMIT 1", (nome,))
                row = cursor.fetchone()
                if row:
                    prod_id = row[0]
                    if estoque_min_val:
                        try:
                            min_val = int(estoque_min_val)
                            self.db.set_estoque_minimo(prod_id, min_val)
                        except (ValueError, Exception):
                            pass
                    # Salvar subcategoria e tamanho
                    if subcat_val and subcat_val != "(nenhuma)":
                        cursor.execute("UPDATE produtos SET subcategoria = ? WHERE id = ?", (subcat_val, prod_id))
                    if tamanho_val and tamanho_val != "(nenhum)":
                        cursor.execute("UPDATE produtos SET tamanho = ? WHERE id = ?", (tamanho_val, prod_id))
                    if sub_subcat_val and sub_subcat_val != "(nenhuma)":
                        cursor.execute("UPDATE produtos SET sub_subcategoria = ? WHERE id = ?", (sub_subcat_val, prod_id))
                    if lote_serie:
                        cursor.execute("UPDATE produtos SET lote_serie = ? WHERE id = ?", (lote_serie, prod_id))
                    
                    # Salvar estoque por tamanho (P/M/G/GG)
                    if size_entries:
                        try:
                            ep = int(size_entries["P"].get() or 0)
                            em = int(size_entries["M"].get() or 0)
                            eg = int(size_entries["G"].get() or 0)
                            egg = int(size_entries["GG"].get() or 0)
                            if ep > 0 or em > 0 or eg > 0 or egg > 0:
                                self.db.atualizar_estoque_tamanhos(prod_id, ep, em, eg, egg)
                        except (ValueError, Exception):
                            pass

                    # Salvar unidade de medida e preço por peso (Padaria/Açougue)
                    if e_unidade is not None:
                        un = e_unidade.get()
                        cursor.execute("UPDATE produtos SET unidade_medida = ? WHERE id = ?", (un, prod_id))
                    if e_preco_peso is not None:
                        try:
                            pp = float(e_preco_peso.get().replace(",", ".") or 0)
                            cursor.execute("UPDATE produtos SET preco_por_peso = ? WHERE id = ?", (pp, prod_id))
                        except (ValueError, Exception):
                            pass

                    # Salvar tipo de produto (produto/insumo) e sob encomenda
                    if e_tipo_produto is not None:
                        tp = e_tipo_produto.get()
                        cursor.execute("UPDATE produtos SET tipo_produto = ? WHERE id = ?", (tp, prod_id))
                    if e_sob_encomenda is not None:
                        se = 1 if e_sob_encomenda.get() == "Sim" else 0
                        cursor.execute("UPDATE produtos SET sob_encomenda = ? WHERE id = ?", (se, prod_id))
                    
                    self.db.conn.commit()
                
                # Salvar variações de Sapatos (tamanhos numéricos)
                for sd in shoe_sizes_list:
                    try:
                        tam = sd['tam'].get().strip()
                        qtd_s = int(sd['qtd'].get() or 0)
                        if tam and qtd_s > 0:
                            nome_var = f"{nome} - Nº {tam}"
                            self.db.adicionar_produto(
                                nome_var, qtd_s, preco, cat,
                                preco_compra=custo, imagem=img_name,
                                horas_trabalho=horas, valor_hora=v_hora,
                                margem_lucro=margem, fator_risco=risco, fator_raridade=raridade,
                                codigo_barras=barcode
                            )
                    except (ValueError, Exception):
                        pass

                # Salvar variações de Calças (tamanhos numéricos)
                for pd_item in pants_sizes_list:
                    try:
                        tam = pd_item['tam'].get().strip()
                        qtd_c = int(pd_item['qtd'].get() or 0)
                        if tam and qtd_c > 0:
                            nome_var = f"{nome} - Tam {tam}"
                            self.db.adicionar_produto(
                                nome_var, qtd_c, preco, cat,
                                preco_compra=custo, imagem=img_name,
                                horas_trabalho=horas, valor_hora=v_hora,
                                margem_lucro=margem, fator_risco=risco, fator_raridade=raridade,
                                codigo_barras=barcode
                            )
                    except (ValueError, Exception):
                        pass

                messagebox.showinfo("Sucesso", "Produto cadastrado com sucesso!", parent=dialog)
                self.refresh_dashboard()
                dialog.destroy()
                
            except ValueError:
                messagebox.showerror("Erro", "Verifique os campos numéricos.", parent=dialog)

        ctk.CTkButton(main_frame, text="CONFIRMAR CADASTRO", command=save, height=50, 
                    fg_color="#28a745", hover_color="#218838", font=("Aileron", 14, "bold")).pack(pady=20, fill="x")

    def open_kit_manager(self):
        window = ctk.CTkToplevel(self)
        window.title("Gerenciador de Kits")
        window.geometry("950x700")
        window.attributes("-topmost", True)

        # State
        self.temp_kit_items = []

        # Layout
        main_layout = ctk.CTkFrame(window, fg_color="transparent")
        main_layout.pack(fill="both", expand=True, padx=20, pady=20)

        left_col = ctk.CTkFrame(main_layout, width=400, fg_color=("white", "#2B2B2B"), corner_radius=15)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        right_col = ctk.CTkFrame(main_layout, width=400, fg_color=("white", "#2B2B2B"), corner_radius=15)
        right_col.pack(side="right", fill="both", expand=True, padx=(10, 0))

        # --- LEFT: PRODUTOS ---
        ctk.CTkLabel(left_col, text="1. Selecionar Produtos", font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=15)
        
        e_search = ctk.CTkEntry(left_col, placeholder_text="Buscar produto...", height=35)
        e_search.pack(fill="x", padx=15, pady=(0, 15))
        
        prod_list = ctk.CTkScrollableFrame(left_col, fg_color="transparent")
        prod_list.pack(fill="both", expand=True, padx=5, pady=5)

        try:
            df_prods = self.db.listar_produtos()
        except Exception: df_prods = pd.DataFrame(columns=['id', 'nome', 'preco'])
        
        def add_item_to_kit_action(nome): 
             # Simples input dialog para QTD
            dialog_qtd = ctk.CTkInputDialog(text=f"Quantidade de '{nome}' no kit:", title="Composição")
             # Hack para tornar modal/topmost
             # (CTkInputDialog é difícil de customizar o parent, mas geralmente funciona)
            try:
                val_str = dialog_qtd.get_input()
                if val_str:
                    from services.estoque_service import EstoqueService
                    val = EstoqueService.parse_qtd_componente(val_str)
                    self.temp_kit_items.append({'nome': nome, 'quantidade': val})
                    refresh_kit_ui()
            except Exception: pass

        def load_prods(query=""):
            for w in prod_list.winfo_children(): w.destroy()
            if df_prods.empty: return
            
            for _, row in df_prods.iterrows():
                if query.lower() in str(row['nome']).lower():
                    f = ctk.CTkFrame(prod_list, fg_color=("white", "#404040"), corner_radius=8, border_width=1, border_color=("#EEEEEE", "#555555"))
                    f.pack(fill="x", pady=3, padx=5)
                    
                    lbl = ctk.CTkLabel(f, text=f"{row['nome']}", anchor="w", font=("Aileron", 12))
                    lbl.pack(side="left", padx=10, fill="x", expand=True)
                    
                    ctk.CTkButton(f, text="+", width=30, height=25, fg_color="#0A4174", 
                                command=lambda n=row['nome']: add_item_to_kit_action(n)).pack(side="right", padx=5, pady=5)

        e_search.bind("<KeyRelease>", lambda e: load_prods(e_search.get()))
        load_prods()

        # --- RIGHT: KIT DEFINITION ---
        ctk.CTkLabel(right_col, text="2. Configurar Kit (Virtual)", font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=15)
        
        ctk.CTkLabel(right_col, text="Nome do Kit:", font=("Aileron", 12, "bold")).pack(anchor="w", padx=15)
        e_nome = ctk.CTkEntry(right_col, height=35)
        e_nome.pack(fill="x", padx=15, pady=(5, 15))
        
        ctk.CTkLabel(right_col, text="Preço Venda (Fixo/Promo):", font=("Aileron", 12, "bold")).pack(anchor="w", padx=15)
        e_preco = ctk.CTkEntry(right_col, height=35)
        e_preco.pack(fill="x", padx=15, pady=(5, 15))
        
        ctk.CTkLabel(right_col, text="Composição:", font=("Aileron", 12, "bold")).pack(anchor="w", padx=15)
        kit_list_frame = ctk.CTkScrollableFrame(right_col, height=250, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        kit_list_frame.pack(fill="both", expand=True, padx=15, pady=5)

        lbl_soma = ctk.CTkLabel(right_col, text="Soma Produtos: R$ 0.00", text_color="gray")
        lbl_soma.pack(pady=5)

        def refresh_kit_ui():
            for w in kit_list_frame.winfo_children(): w.destroy()
            soma_individual = 0
            
            for i, item in enumerate(self.temp_kit_items):
                f = ctk.CTkFrame(kit_list_frame, fg_color=("white", "#404040"), corner_radius=6)
                f.pack(fill="x", pady=2, padx=5)
                
                txt = f"{item['quantidade']}un - {item['nome']}"
                ctk.CTkLabel(f, text=txt, anchor="w").pack(side="left", padx=10, pady=5)
                
                ctk.CTkButton(f, text="X", width=30, height=20, fg_color="#FF6B6B", text_color="white",
                            command=lambda idx=i: remove_kit_item(idx)).pack(side="right", padx=5)
                
                # Soma Custo
                if not df_prods.empty:
                    p = df_prods[df_prods['nome'] == item['nome']]
                    if not p.empty:
                        soma_individual += p.iloc[0]['preco'] * item['quantidade']
            
            lbl_soma.configure(text=f"Soma Produtos: R$ {soma_individual:.2f}")

        def remove_kit_item(idx):
            del self.temp_kit_items[idx]
            refresh_kit_ui()
        
        def save_kit():
            from services.estoque_service import EstoqueService
            try:
                EstoqueService(self.db).criar_kit(e_nome.get(), e_preco.get(), self.temp_kit_items)
            except ValueError as e:
                messagebox.showerror("Erro", str(e), parent=window)
                return
            messagebox.showinfo("Sucesso", "Kit criado com sucesso!\nEle aparecerá no PDV.", parent=window)
            window.destroy()

        ctk.CTkButton(right_col, text="SALVAR KIT", height=45, fg_color="#28a745", hover_color="#218838", font=("Aileron", 14, "bold"), command=save_kit).pack(pady=20, padx=15, fill="x")

    def _fechar_pdv(self, dialog=None):
        """Fecha janela do PDV e limpa referência singleton com cleanup de timers."""
        target = dialog or getattr(self, '_pdv_dialog', None)
        self._pdv_dialog = None
        if target is not None:
            try:
                if hasattr(target, 'close'):
                    target.close()
                else:
                    target.destroy()
            except Exception:
                pass

    def open_sell_product_dialog(self):
        """Abre a janela isolada do PDV (Ponto de Venda)."""
        if self._pdv_dialog is not None:
            try:
                if self._pdv_dialog.winfo_exists():
                    self._pdv_dialog.focus()
                    return self._pdv_dialog
            except Exception:
                self._pdv_dialog = None

        user_id = self.current_user.get("id") if self.current_user else 1
        if not self.db.caixa_aberto(user_id):
            messagebox.showwarning(
                "Caixa Fechado",
                "Você precisa abrir o caixa antes de registrar vendas."
            )
            self.open_caixa_dialog()
            return None

        from views.pdv_view import PDVDialog
        return PDVDialog(self, self.db)

    # --- Gerenciador de Clientes com Histórico e Fidelidade ---
    def open_clientes_manager(self):
        window = ctk.CTkToplevel(self)
        window.title("Gerenciador de Clientes")
        window.geometry("900x650")
        window.attributes("-topmost", True)

        tabview = ctk.CTkTabview(window, width=860, height=580)
        tabview.pack(padx=15, pady=15, fill="both", expand=True)

        tab_lista = tabview.add("Clientes")
        tab_historico = tabview.add("Histórico de Compras")
        tab_fidelidade = tabview.add("Fidelidade")

        # --- TAB 1: Lista de Clientes ---
        add_frame = ctk.CTkFrame(tab_lista, fg_color=("white", "#2B2B2B"), corner_radius=10)
        add_frame.pack(fill="x", pady=5, padx=5)
        ctk.CTkLabel(add_frame, text="Novo Cliente:", font=("Aileron", 12, "bold")).pack(side="left", padx=10, pady=10)
        e_nome = ctk.CTkEntry(add_frame, placeholder_text="Nome", width=150)
        e_nome.pack(side="left", padx=5, pady=10)
        e_contato = ctk.CTkEntry(add_frame, placeholder_text="Contato", width=130)
        e_contato.pack(side="left", padx=5, pady=10)
        e_obs = ctk.CTkEntry(add_frame, placeholder_text="Observações", width=150)
        e_obs.pack(side="left", padx=5, pady=10)

        list_frame = ctk.CTkFrame(tab_lista, fg_color="transparent")
        list_frame.pack(fill="both", expand=True, pady=5, padx=5)

        c_cols = ("id", "nome", "contato", "obs", "compras")
        tree_cli = ttk.Treeview(list_frame, columns=c_cols, show="headings", height=12)
        for c, h, w in [("id", "ID", 40), ("nome", "Nome", 180), ("contato", "Contato", 150), ("obs", "Observações", 200), ("compras", "Compras", 60)]:
            tree_cli.heading(c, text=h)
            tree_cli.column(c, width=w)
        tree_cli.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(list_frame, orient="vertical", command=tree_cli.yview).pack(side="right", fill="y")

        def refresh_clientes():
            for i in tree_cli.get_children(): tree_cli.delete(i)
            try:
                df = self.db.listar_clientes()
                for _, r in df.iterrows():
                    tree_cli.insert("", "end", values=(r['id'], r['nome'], r.get('contato', ''), r.get('observacoes', ''), r.get('compras_recorrentes', 0)))
            except Exception as e:
                logger.warning("Erro listar clientes: %s", e)

        def add_cliente():
            nome = e_nome.get().strip()
            if not nome:
                messagebox.showerror("Erro", "Nome é obrigatório.", parent=window)
                return
            self.db.adicionar_cliente(nome, e_contato.get().strip(), e_obs.get().strip())
            e_nome.delete(0, "end")
            e_contato.delete(0, "end")
            e_obs.delete(0, "end")
            refresh_clientes()

        def remover_cliente():
            sel = tree_cli.selection()
            if not sel: return
            c_id = tree_cli.item(sel[0])['values'][0]
            if messagebox.askyesno("Confirmar", "Remover este cliente?", parent=window):
                self.db.remover_cliente(c_id)
                refresh_clientes()

        ctk.CTkButton(add_frame, text="Adicionar", command=add_cliente, width=80, fg_color="#28a745", hover_color="#218838").pack(side="left", padx=5, pady=10)
        ctk.CTkButton(add_frame, text="Remover", command=remover_cliente, width=80, fg_color="#FF6B6B", hover_color="#FF5252", text_color="white").pack(side="left", padx=5, pady=10)

        refresh_clientes()

        # --- TAB 2: Histórico de Compras por Cliente ---
        hc_frame = ctk.CTkFrame(tab_historico, fg_color=("white", "#2B2B2B"), corner_radius=10)
        hc_frame.pack(fill="x", pady=5, padx=5)
        ctk.CTkLabel(hc_frame, text="Cliente:", font=("Aileron", 11, "bold")).pack(side="left", padx=10, pady=10)
        df_c = self.db.listar_clientes()
        c_names = df_c['nome'].tolist() if not df_c.empty else []
        var_cli = ctk.StringVar(value=c_names[0] if c_names else "")
        combo_cli = ctk.CTkComboBox(hc_frame, values=c_names, variable=var_cli, width=200)
        combo_cli.pack(side="left", padx=5, pady=10)

        hc_list_frame = ctk.CTkFrame(tab_historico, fg_color="transparent")
        hc_list_frame.pack(fill="both", expand=True, pady=5, padx=5)

        hc_cols = ("data", "produto", "qtd", "valor", "lucro", "pgto")
        tree_hc = ttk.Treeview(hc_list_frame, columns=hc_cols, show="headings", height=12)
        for c, h, w in [("data", "Data", 130), ("produto", "Produto", 150), ("qtd", "Qtd", 50), ("valor", "Valor", 90), ("lucro", "Lucro", 80), ("pgto", "Pagamento", 100)]:
            tree_hc.heading(c, text=h)
            tree_hc.column(c, width=w)
        tree_hc.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(hc_list_frame, orient="vertical", command=tree_hc.yview).pack(side="right", fill="y")

        lbl_total_cli = ctk.CTkLabel(tab_historico, text="", font=("Aileron", 14, "bold"), text_color="#0A4174")
        lbl_total_cli.pack(pady=5)

        def carregar_historico_cliente():
            for i in tree_hc.get_children(): tree_hc.delete(i)
            nome = var_cli.get()
            if not nome: return
            try:
                df_vc = self.db.vendas_por_cliente(nome)
                total = 0
                for _, r in df_vc.iterrows():
                    tree_hc.insert("", "end", values=(
                        str(r['data_venda'])[:16], r.get('produto_nome', 'N/A'), r['quantidade'],
                        f"R$ {r['valor_total']:.2f}", f"R$ {r.get('lucro', 0):.2f}", r.get('forma_pagamento', 'N/A')
                    ))
                    total += r['valor_total']
                lbl_total_cli.configure(text=f"Total gasto: R$ {total:.2f} | {len(df_vc)} compras")
            except Exception as e:
                logger.warning("Erro hist cliente: %s", e)

        ctk.CTkButton(hc_frame, text="Ver Histórico", command=carregar_historico_cliente, width=100, fg_color="#7BBDE8", text_color="#001D39").pack(side="left", padx=10)

        # --- TAB 3: Programa de Fidelidade ---
        fid_scroll = ctk.CTkScrollableFrame(tab_fidelidade, fg_color="transparent")
        fid_scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(fid_scroll, text="Programa de Fidelidade", font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=10, anchor="w")
        ctk.CTkLabel(fid_scroll, text="A cada R$ 10 em compras = 1 ponto. 100 pontos = R$ 10 de desconto.",
                    font=("Aileron", 11, "italic"), text_color="gray").pack(anchor="w", padx=10)

        fid_frame = ctk.CTkFrame(fid_scroll, fg_color="transparent")
        fid_frame.pack(fill="both", expand=True, pady=10)

        def carregar_fidelidade():
            for w in fid_frame.winfo_children(): w.destroy()
            try:
                df_c = self.db.listar_fidelidade_clientes()
                if df_c.empty:
                    ctk.CTkLabel(fid_frame, text="Nenhum cliente cadastrado.", text_color="gray").pack(pady=20)
                    return
                for _, r in df_c.iterrows():
                    pontos = int(r.get('pontos', 0) or 0)
                    total_ac = float(r.get('total_acumulado', 0.0) or 0.0)
                    card = ctk.CTkFrame(fid_frame, fg_color=("white", "#2B2B2B"), corner_radius=10)
                    card.pack(fill="x", pady=3, padx=5)
                    ctk.CTkLabel(card, text=f"{r['nome']}", font=("Aileron", 13, "bold"), text_color=("#001D39", "white")).pack(side="left", padx=15, pady=10)
                    ctk.CTkLabel(card, text=f"⭐ {pontos} pts", font=("Aileron", 12, "bold"), text_color="#E67E22").pack(side="left", padx=10)
                    ctk.CTkLabel(card, text=f"Total acumulado: R$ {total_ac:.2f}", font=("Aileron", 11), text_color="gray").pack(side="left", padx=10)
                    ctk.CTkLabel(card, text=f"Compras: {r.get('compras_recorrentes', 0)}", font=("Aileron", 11), text_color="gray").pack(side="left", padx=10)
            except Exception as e:
                logger.warning("Erro fidelidade: %s", e)

        carregar_fidelidade()

    def open_anota_tudo_dialog(self):
        """Abre janela moderna do painel Anota Tudo."""
        window = ctk.CTkToplevel(self)
        window.title("📝 Anota Tudo — Painel de Anotações")
        window.geometry("950x700")
        window.attributes("-topmost", True)
        
        # Centralizar na tela
        window.update_idletasks()
        w = window.winfo_width()
        h = window.winfo_height()
        extra_w = (window.winfo_screenwidth() - w) // 2
        extra_h = (window.winfo_screenheight() - h) // 2
        window.geometry(f"{w}x{h}+{extra_w}+{extra_h}")
        
        # Grid layout na janela
        window.grid_columnconfigure(0, weight=4)  # Coluna do formulário (Esquerda)
        window.grid_columnconfigure(1, weight=6)  # Coluna das notas (Direita)
        window.grid_rowconfigure(0, weight=1)
        
        # Tema e fontes
        lbl_font_bold = ctk.CTkFont(family="Aileron", size=14, weight="bold")
        lbl_font_normal = ctk.CTkFont(family="Aileron", size=12)
        title_font = ctk.CTkFont(family="Aileron", size=18, weight="bold")
        
        # Estado de Edição
        editando_sonho_id = [None]
        editando_versao = [1]
        
        # ID do usuário logado
        usuario_id = self.current_user["id"] if self.current_user else 1

        # ========================================================
        # COLUNA ESQUERDA - FORMULÁRIO DE CRIAÇÃO/EDIÇÃO
        # ========================================================
        form_frame = ctk.CTkFrame(window, fg_color=("white", "#2B2B2B"), corner_radius=15, border_width=1, border_color=("#E5E5E5", "#3E3E3E"))
        form_frame.grid(row=0, column=0, padx=(15, 10), pady=15, sticky="nsew")
        form_frame.grid_columnconfigure(0, weight=1)
        
        lbl_form_title = ctk.CTkLabel(form_frame, text="📝 Nova Anotação", font=title_font, text_color=("#0A4174", "white"))
        lbl_form_title.pack(anchor="w", padx=20, pady=(20, 15))
        
        # Campo Título
        ctk.CTkLabel(form_frame, text="Título:", font=lbl_font_bold).pack(anchor="w", padx=20, pady=(5, 2))
        entry_titulo = ctk.CTkEntry(form_frame, placeholder_text="Ex: Compras da semana, Tarefas...", height=35)
        entry_titulo.pack(fill="x", padx=20, pady=(0, 10))
        
        # Campo Categoria
        ctk.CTkLabel(form_frame, text="Categoria/Tag:", font=lbl_font_bold).pack(anchor="w", padx=20, pady=(5, 2))
        entry_cat = ctk.CTkEntry(form_frame, placeholder_text="Ex: Estoque, Clientes, Financeiro, Ideia", height=35)
        entry_cat.pack(fill="x", padx=20, pady=(0, 10))
        
        # Campo Descrição (Textbox)
        ctk.CTkLabel(form_frame, text="Conteúdo / Detalhes:", font=lbl_font_bold).pack(anchor="w", padx=20, pady=(5, 2))
        txt_desc = ctk.CTkTextbox(form_frame, height=180, corner_radius=8, border_width=1, border_color=("#CCCCCC", "#4A4A4A"))
        txt_desc.pack(fill="both", expand=True, padx=20, pady=(0, 15))
        
        # Botões de Ação do Form
        btn_frame = ctk.CTkFrame(form_frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 20))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        
        def resetar_formulario():
            editando_sonho_id[0] = None
            editando_versao[0] = 1
            lbl_form_title.configure(text="📝 Nova Anotação", text_color=("#0A4174", "white"))
            entry_titulo.delete(0, "end")
            entry_cat.delete(0, "end")
            txt_desc.delete("1.0", "end")
            btn_salvar.configure(text="Adicionar")
            btn_limpar.configure(text="Limpar")
            
        def salvar_anotacao():
            titulo = entry_titulo.get().strip()
            categoria = entry_cat.get().strip() or "Geral"
            descricao = txt_desc.get("1.0", "end-1c").strip()
            
            if not titulo or len(titulo) < 3:
                messagebox.showerror("Erro de Validação", "O título deve ter pelo menos 3 caracteres.", parent=window)
                return
            if not descricao or len(descricao) < 10:
                messagebox.showerror("Erro de Validação", "A descrição deve possuir pelo menos 10 caracteres.", parent=window)
                return
                
            dados = {
                "usuario_id": usuario_id,
                "titulo": titulo,
                "descricao": descricao,
                "categoria": categoria,
                "data_registro": datetime.date.today().isoformat()
            }
            
            try:
                if editando_sonho_id[0] is not None:
                    # Modo Edição (OCC)
                    from services.dreams_service import DreamConcurrencyError
                    try:
                        self.sonhos_service.editar_sonho(
                            sonho_id=editando_sonho_id[0],
                            dados_input=dados,
                            versao_lida=editando_versao[0],
                            usuario_requisitor_id=usuario_id
                        )
                        messagebox.showinfo("Sucesso", "Anotação atualizada!", parent=window)
                    except DreamConcurrencyError:
                        messagebox.showerror(
                            "Concorrência Detectada", 
                            "Erro de Concorrência: A anotação foi alterada por outro usuário enquanto você a visualizava.\n"
                            "Por favor, recarregue e tente novamente.", 
                            parent=window
                        )
                        return
                    except Exception as e:
                        messagebox.showerror("Erro de Gravação", f"Falha ao salvar: {e}", parent=window)
                        return
                else:
                    # Modo Criação
                    self.sonhos_service.adicionar_sonho(dados)
                    messagebox.showinfo("Sucesso", "Anotação criada!", parent=window)
                
                resetar_formulario()
                carregar_anotacoes()
            except Exception as ex:
                messagebox.showerror("Erro", f"Erro: {ex}", parent=window)
                
        btn_salvar = ctk.CTkButton(btn_frame, text="Adicionar", fg_color="#28a745", hover_color="#218838", text_color="white", font=lbl_font_bold, height=38, command=salvar_anotacao)
        btn_salvar.grid(row=0, column=0, padx=(0, 5), sticky="ew")
        
        btn_limpar = ctk.CTkButton(btn_frame, text="Limpar", fg_color=("#CCCCCC", "#4A4A4A"), hover_color=("#B3B3B3", "#3A3A3A"), text_color=("#333333", "white"), font=lbl_font_bold, height=38, command=resetar_formulario)
        btn_limpar.grid(row=0, column=1, padx=(5, 0), sticky="ew")

        # ========================================================
        # COLUNA DIREITA - LISTA DE ANOTAÇÕES
        # ========================================================
        list_card = ctk.CTkFrame(window, fg_color="transparent")
        list_card.grid(row=0, column=1, padx=(10, 15), pady=15, sticky="nsew")
        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_rowconfigure(2, weight=1)
        
        # Barra de Pesquisa e Combobox de Categoria
        search_frame = ctk.CTkFrame(list_card, fg_color=("white", "#2B2B2B"), corner_radius=12, border_width=1, border_color=("#E5E5E5", "#3E3E3E"))
        search_frame.grid(row=0, column=0, fill="x", pady=(0, 10), sticky="ew")
        search_frame.grid_columnconfigure(0, weight=7)
        search_frame.grid_columnconfigure(1, weight=3)
        
        entry_busca = ctk.CTkEntry(search_frame, placeholder_text="🔍 Pesquisar por título...", height=35)
        entry_busca.grid(row=0, column=0, padx=(15, 5), pady=10, sticky="ew")
        
        combo_filtro_cat = ctk.CTkComboBox(search_frame, values=["Todas as Categorias"], height=35, state="readonly")
        combo_filtro_cat.grid(row=0, column=1, padx=(5, 15), pady=10, sticky="ew")
        combo_filtro_cat.set("Todas as Categorias")
        
        # Painel scrollable para os cartões das anotações
        scroll_notes = ctk.CTkScrollableFrame(list_card, fg_color=("#F5F5F5", "#1E1E1E"), corner_radius=12, border_width=1, border_color=("#E0E0E0", "#333333"))
        scroll_notes.grid(row=2, column=0, sticky="nsew")
        
        # Cache local das anotações listadas para filtragem instantânea
        anotacoes_cache = []

        def renderizar_anotacoes_filtradas():
            # Limpar painel
            for w in scroll_notes.winfo_children():
                w.destroy()
                
            busca = entry_busca.get().strip().lower()
            cat_filtro = combo_filtro_cat.get()
            
            anotacoes_exibidas = []
            for a in anotacoes_cache:
                match_busca = (busca in a["titulo"].lower() or busca in a["descricao"].lower())
                match_cat = (cat_filtro == "Todas as Categorias" or a["categoria"] == cat_filtro)
                if match_busca and match_cat:
                    anotacoes_exibidas.append(a)
                    
            if not anotacoes_exibidas:
                ctk.CTkLabel(scroll_notes, text="Nenhuma anotação encontrada.", font=lbl_font_normal, text_color="gray").pack(pady=40)
                return
                
            # Renderizar cartões dinâmicos premium
            for a in anotacoes_exibidas:
                card = ctk.CTkFrame(scroll_notes, fg_color=("white", "#2A2A2D"), corner_radius=12, border_width=1, border_color=("#EBEBEB", "#3A3A3D"))
                card.pack(fill="x", pady=6, padx=10)
                card.grid_columnconfigure(0, weight=1)
                
                # Header do cartão: Título + Tag Categoria
                header = ctk.CTkFrame(card, fg_color="transparent")
                header.pack(fill="x", padx=15, pady=(12, 4))
                
                lbl_title = ctk.CTkLabel(header, text=a["titulo"], font=ctk.CTkFont(family="Aileron", size=13, weight="bold"), anchor="w", text_color=("#001D39", "white"))
                lbl_title.pack(side="left", fill="x", expand=True)
                
                # Tag / Pill de Categoria com cor harmoniosa e moderna
                tag_bg = ("#EBF5FB", "#1B2A35")
                tag_fg = ("#2980B9", "#7BBDE8")
                
                if a["categoria"].lower() in ("financeiro", "gasto", "saldo"):
                    tag_bg = ("#EAF2F8", "#1E3A2F")
                    tag_fg = ("#27AE60", "#2ECC71")
                elif a["categoria"].lower() in ("estoque", "produtos", "kits"):
                    tag_bg = ("#FDF2E9", "#3A281E")
                    tag_fg = ("#D35400", "#E67E22")
                elif a["categoria"].lower() in ("clientes", "fidelidade", "vendedores"):
                    tag_bg = ("#F5EEF8", "#2E1F3A")
                    tag_fg = ("#8E44AD", "#AF7AC5")
                    
                tag_lbl = ctk.CTkLabel(header, text=f"🏷️ {a['categoria']}", font=ctk.CTkFont(family="Aileron", size=9, weight="bold"), fg_color=tag_bg, text_color=tag_fg, corner_radius=8, height=18)
                tag_lbl.pack(side="right", padx=5)
                
                # Metadata do cartão (Data)
                meta_frame = ctk.CTkFrame(card, fg_color="transparent")
                meta_frame.pack(fill="x", padx=15, pady=(0, 6))
                
                dt_obj = datetime.date.fromisoformat(a["data_registro"])
                dt_str = dt_obj.strftime("%d/%m/%Y")
                lbl_date = ctk.CTkLabel(meta_frame, text=f"📅 {dt_str}", font=ctk.CTkFont(family="Aileron", size=10), text_color="gray")
                lbl_date.pack(side="left")
                
                # Conteúdo / Descrição da Anotação
                desc_label = ctk.CTkLabel(card, text=a["descricao"], font=lbl_font_normal, text_color=("#555555", "#CCCCCC"), wraplength=450, justify="left", anchor="w")
                desc_label.pack(fill="x", padx=15, pady=(4, 12))
                
                # Ações do cartão
                actions_bar = ctk.CTkFrame(card, fg_color="transparent")
                actions_bar.pack(fill="x", padx=15, pady=(0, 12))
                
                def carregar_para_edicao(anot=a):
                    editando_sonho_id[0] = anot["id"]
                    editando_versao[0] = anot["version"]
                    
                    lbl_form_title.configure(text="✏️ Editar Anotação", text_color="#E67E22")
                    entry_titulo.delete(0, "end")
                    entry_titulo.insert(0, anot["titulo"])
                    entry_cat.delete(0, "end")
                    entry_cat.insert(0, anot["categoria"])
                    txt_desc.delete("1.0", "end")
                    txt_desc.insert("1.0", anot["descricao"])
                    btn_salvar.configure(text="Salvar Alterações")
                    btn_limpar.configure(text="Cancelar")
                    entry_titulo.focus()
                    
                def deletar_anotacao(anot=a):
                    if messagebox.askyesno("Confirmar Exclusão", f"Deseja realmente excluir a anotação '{anot['titulo']}'?", parent=window):
                        try:
                            self.sonhos_service.remover_sonho(anot["id"], usuario_id)
                            messagebox.showinfo("Sucesso", "Anotação excluída!", parent=window)
                            if editando_sonho_id[0] == anot["id"]:
                                resetar_formulario()
                            carregar_anotacoes()
                        except Exception as e:
                            messagebox.showerror("Erro ao Deletar", f"Não foi possível excluir: {e}", parent=window)
                
                # Botões rápidos com hover
                btn_edit = ctk.CTkButton(actions_bar, text="✏️ Editar", width=75, height=26, fg_color=("#E5F3FD", "#1D3244"), text_color=("#2980B9", "#7BBDE8"), hover_color=("#D1E9FC", "#284A65"), font=ctk.CTkFont(family="Aileron", size=10, weight="bold"), command=carregar_para_edicao)
                btn_edit.pack(side="left", padx=(0, 8))
                
                btn_del = ctk.CTkButton(actions_bar, text="✕ Excluir", width=75, height=26, fg_color=("#FCE4D6", "#3E211F"), text_color=("#C0392B", "#E74C3C"), hover_color=("#FADBD8", "#542D2A"), font=ctk.CTkFont(family="Aileron", size=10, weight="bold"), command=deletar_anotacao)
                btn_del.pack(side="left")

        def carregar_anotacoes():
            nonlocal anotacoes_cache
            try:
                # Obter do sqlite via sonhos_service
                anotacoes_cache = self.sonhos_service.listar_sonhos()
                
                # Coletar categorias exclusivas para o Combobox
                cats = {"Todas as Categorias"}
                for a in anotacoes_cache:
                    if a["categoria"]:
                        cats.add(a["categoria"])
                        
                combo_filtro_cat.configure(values=list(cats))
                renderizar_anotacoes_filtradas()
            except Exception as e:
                logger.warning("Erro carregar anotações: %s", e)
                
        # Ouvir alterações na pesquisa e no combobox
        entry_busca.bind("<KeyRelease>", lambda e: renderizar_anotacoes_filtradas())
        combo_filtro_cat.configure(command=lambda e: renderizar_anotacoes_filtradas())
        
        # Carregar na abertura
        carregar_anotacoes()

    # --- Importar Produtos via CSV/PDF ---
    def open_importar_csv(self):
        """Abre a interface avançada de importação com validador e modelo de exemplo."""
        import os
        dialog = ctk.CTkToplevel(self)
        dialog.title("Importador de Produtos (CSV/PDF)")
        dialog.geometry("850x650")
        dialog.attributes("-topmost", True)
        dialog.lift()
        dialog.focus_force()

        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)

        # Divisão em duas colunas (Esquerda: Instruções/Modelo, Direita: Validador/Ações)
        main_frame.grid_columnconfigure(0, weight=4)  # Coluna esquerda (modelo/dicas)
        main_frame.grid_columnconfigure(1, weight=5)  # Coluna direita (validação/importação)
        main_frame.grid_rowconfigure(0, weight=1)

        # --- Coluna Esquerda: Instruções e Modelo ---
        left_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        left_frame.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
        left_frame.grid_columnconfigure(0, weight=1)
        left_frame.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(left_frame, text="Instruções & Modelo CSV", font=("Aileron", 16, "bold"), text_color=("#0A4174", "#7BBDE8")).grid(row=0, column=0, padx=15, pady=(15, 5), sticky="w")
        
        instrucoes = (
            "Para importar produtos em lote:\n"
            "1. Crie uma tabela com as colunas obrigatórias.\n"
            "2. Salve no formato CSV ou use a tabela de um PDF.\n"
            "3. O sistema mapeia colunas sinônimas automaticamente.\n\n"
            "Campos Obrigatórios:\n"
            "• nome (ou Item/Produto/Nome)\n"
            "• quantidade (ou Qtd/Estoque)\n"
            "• preco (ou Preço/Preço Venda/Valor)\n\n"
            "Campos Opcionais:\n"
            "• categoria, subcategoria, tamanho, preco_compra,\n"
            "  codigo_barras, data_validade, lote_serie, imagem"
        )
        ctk.CTkLabel(left_frame, text=instrucoes, font=("Aileron", 11), justify="left", text_color=("#333333", "#CCCCCC")).grid(row=1, column=0, padx=15, pady=5, sticky="w")

        # Modelo CSV Comentado
        template_text = (
            "# Exemplo de arquivo de importação do EstokeAoCubo\n"
            "# As linhas com '#' no início serão ignoradas\n"
            "nome,quantidade,preco,categoria,preco_compra,codigo_barras\n"
            "Camiseta Estampada,50,45.00,Vestuário,20.00,7891234567890\n"
            "Caneca Porcelana,25,29.90,Decoração,12.50,7891234567891\n"
            "Almofada Conforto,12,39.90,Decoração,15.00,7891234567892\n"
        )
        
        template_box = ctk.CTkTextbox(left_frame, font=("Courier", 10), height=180)
        template_box.grid(row=2, column=0, padx=15, pady=5, sticky="nsew")
        template_box.insert("0.0", template_text)
        template_box.configure(state="disabled")

        def copiar_modelo():
            self.clipboard_clear()
            self.clipboard_append(template_text)
            messagebox.showinfo("Modelo Copiado", "Modelo CSV copiado para a área de transferência!", parent=dialog)
            dialog.lift()

        ctk.CTkButton(left_frame, text="📋 Copiar Modelo CSV", command=copiar_modelo, height=35, fg_color="#6EA2B3", hover_color="#49769F", text_color="white", font=("Aileron", 12, "bold")).grid(row=3, column=0, padx=15, pady=15, sticky="ew")

        # --- Coluna Direita: Validação e Importação ---
        right_frame = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        right_frame.grid(row=0, column=1, padx=(10, 0), sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(right_frame, text="Validador de Arquivo", font=("Aileron", 16, "bold"), text_color=("#0A4174", "#7BBDE8")).grid(row=0, column=0, padx=15, pady=(15, 15), sticky="w")

        # Seleção de arquivo
        file_select_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        file_select_frame.grid(row=1, column=0, padx=15, pady=5, sticky="ew")
        file_select_frame.grid_columnconfigure(0, weight=1)

        selected_file_var = ctk.StringVar(value="Nenhum arquivo selecionado")
        lbl_selected_file = ctk.CTkLabel(file_select_frame, textvariable=selected_file_var, font=("Aileron", 11, "italic"), text_color="gray", anchor="w")
        lbl_selected_file.grid(row=0, column=0, padx=(0, 10), sticky="ew")

        # Variáveis de estado da validação
        state = {
            "df": None,
            "validation_data": None,
            "filepath": None
        }

        # Status do arquivo (resultado da validação)
        status_box = ctk.CTkFrame(right_frame, fg_color="transparent")
        status_box.grid(row=2, column=0, padx=15, pady=10, sticky="nsew")
        status_box.grid_columnconfigure(0, weight=1)
        status_box.grid_rowconfigure(1, weight=1)

        lbl_status_title = ctk.CTkLabel(status_box, text="Aguardando arquivo para validação...", font=("Aileron", 12, "bold"), text_color="gray")
        lbl_status_title.grid(row=0, column=0, pady=(0, 5), sticky="w")

        log_box = ctk.CTkTextbox(status_box, font=("Courier", 10), text_color="#E74C3C")
        log_box.grid(row=1, column=0, sticky="nsew")
        log_box.configure(state="disabled")

        action_btn_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        action_btn_frame.grid(row=3, column=0, padx=15, pady=15, sticky="ew")
        action_btn_frame.grid_columnconfigure(0, weight=1)

        btn_importar = ctk.CTkButton(action_btn_frame, text="Confirmar Importação", state="disabled", height=40, fg_color="#28a745", hover_color="#218838", text_color="white", font=("Aileron", 13, "bold"))
        btn_importar.grid(row=0, column=0, sticky="ew", pady=(0, 5))

        def selecionar_e_validar():
            dialog.attributes("-topmost", False)
            filepath = filedialog.askopenfilename(
                title="Selecionar arquivo CSV ou PDF",
                filetypes=[("CSV Files", "*.csv"), ("PDF Files", "*.pdf"), ("Todos", "*.*")]
            )
            dialog.attributes("-topmost", True)
            dialog.lift()
            
            if not filepath:
                return

            selected_file_var.set(os.path.basename(filepath))
            state["filepath"] = filepath

            # Rodar validação
            success, val_data, df = self.db.preparar_e_validar_importacao(filepath)
            
            log_box.configure(state="normal")
            log_box.delete("0.0", "end")
            
            if not success:
                lbl_status_title.configure(text="❌ Erro ao processar arquivo", text_color="#E74C3C")
                log_box.insert("end", val_data.get("mensagem", "Erro desconhecido."))
                log_box.configure(state="disabled")
                btn_importar.configure(state="disabled", text="Confirmar Importação")
                state["df"] = None
                state["validation_data"] = None
                return
            
            state["df"] = df
            state["validation_data"] = val_data
            
            erros = val_data["erros"]
            total_linhas = val_data["total_linhas"]
            total_validos = val_data["total_validos"]
            mapeamentos = val_data["mapeamentos"]

            log_msg = ""
            if mapeamentos:
                log_msg += "=== MAPEAMENTO DE COLUNAS ===\n"
                for k, v in mapeamentos.items():
                    log_msg += f"• Coluna '{k}' mapeada para '{v}'\n"
                log_msg += "\n"

            if erros:
                lbl_status_title.configure(text=f"⚠️ Concluído com {len(erros)} inconsistência(s)", text_color="#F39C12")
                log_msg += f"=== INCONSISTÊNCIAS ENCONTRADAS ({len(erros)}) ===\n"
                log_msg += "Estes itens serão ignorados ou devem ser corrigidos:\n\n"
                for err in erros:
                    log_msg += f"Linha {err['linha']} | Produto: {err['produto']}\n   Erro: {err['erro']}\n\n"
                
                log_box.insert("end", log_msg)
                log_box.configure(state="disabled", text_color=("#E74C3C", "#FF6B6B"))
                
                if total_validos > 0:
                    btn_importar.configure(
                        state="normal", 
                        text=f"Importar apenas {total_validos} válidos (desconsiderar erros)",
                        command=confirmar_importacao
                    )
                else:
                    btn_importar.configure(state="disabled", text="Nenhum item válido para importar")
            else:
                lbl_status_title.configure(text="✅ Arquivo 100% válido!", text_color="#28a745")
                log_msg += f"Tudo certo! Todas as {total_linhas} linhas estão em formato correto.\n"
                log_msg += "\nPrimeiros 5 registros para importação:\n"
                for _, r in df.head(5).iterrows():
                    log_msg += f"• {r.get('nome')} | Preço: R$ {r.get('preco')} | Qtd: {r.get('quantidade')}\n"
                
                log_box.insert("end", log_msg)
                log_box.configure(state="disabled", text_color=("#28a745", "#2ecc71"))
                btn_importar.configure(
                    state="normal", 
                    text=f"Confirmar Importação de {total_linhas} itens", 
                    command=confirmar_importacao
                )

        btn_selecionar = ctk.CTkButton(file_select_frame, text="Procurar Arquivo", command=selecionar_e_validar, width=120)
        btn_selecionar.grid(row=0, column=1, sticky="e")

        def confirmar_importacao():
            df = state["df"]
            val_data = state["validation_data"]
            filepath = state["filepath"]
            
            if df is None or val_data is None or not filepath:
                return

            if self.db.is_trial_mode():
                linhas_com_erro = val_data["linhas_com_erro"]
                itens_a_importar = len(df) - len(linhas_com_erro)
                contagem_atual = self.db.contar_produtos()
                if contagem_atual + itens_a_importar > 10:
                    self.show_trial_limit_modal(dialog)
                    return

            linhas_com_erro = val_data["linhas_com_erro"]
            ok, msg = self.db.importar_dataframe_produtos(df, linhas_com_erro, filepath)
            
            if ok:
                messagebox.showinfo("Sucesso", msg, parent=dialog)
                dialog.destroy()
                self.refresh_dashboard()
            else:
                messagebox.showerror("Erro", msg, parent=dialog)
                dialog.lift()

    def open_importar_imagens_lote(self):
        """Interface para importar imagens em lote, associando-as aos produtos pelo nome ou código de barras."""
        import os
        dialog = ctk.CTkToplevel(self)
        dialog.title("Importador de Imagens em Lote")
        dialog.geometry("700x600")
        dialog.attributes("-topmost", True)
        dialog.lift()
        dialog.focus_force()

        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)

        ctk.CTkLabel(main_frame, text="📸 Importar Imagens em Lote", font=("Aileron", 18, "bold"), text_color=("#0A4174", "#7BBDE8")).pack(anchor="w", pady=(0, 10))

        instrucoes = (
            "Esta ferramenta associa imagens aos produtos automaticamente.\n"
            "Como nomear os arquivos de imagem na sua pasta:\n"
            "• Pelo Código de Barras (ex: 7891234567890.jpg)\n"
            "• Pelo Nome Exato do Produto (ex: Camiseta Azul.png)\n"
            "• Por Parte do Nome (ex: camiseta.jpg associará à 'Camiseta Estampada')"
        )
        ctk.CTkLabel(main_frame, text=instrucoes, font=("Aileron", 11), justify="left", text_color=("#333", "#AAA")).pack(anchor="w", fill="x", pady=5)

        # Seleção de pasta
        dir_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        dir_frame.pack(fill="x", pady=10)

        selected_dir_var = ctk.StringVar(value="Nenhuma pasta selecionada")
        lbl_dir = ctk.CTkLabel(dir_frame, textvariable=selected_dir_var, font=("Aileron", 11, "italic"), text_color="gray", anchor="w")
        lbl_dir.pack(side="left", fill="x", expand=True, padx=(0, 10))

        state = {
            "matches": [],
            "directory": None
        }

        # Área de Preview/Log
        preview_box = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=12)
        preview_box.pack(fill="both", expand=True, pady=10)
        
        scroll = ctk.CTkScrollableFrame(preview_box, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=8, pady=8)

        lbl_no_items = ctk.CTkLabel(scroll, text="Selecione uma pasta para escanear imagens", font=("Aileron", 12), text_color="gray")
        lbl_no_items.pack(pady=40)

        action_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        action_frame.pack(fill="x", pady=(10, 0))

        btn_confirmar = ctk.CTkButton(action_frame, text="Confirmar Importação", state="disabled", height=38, fg_color="#28a745", hover_color="#218838", text_color="white", font=("Aileron", 12, "bold"))
        btn_confirmar.pack(side="right", padx=5)

        def selecionar_pasta():
            dialog.attributes("-topmost", False)
            folder = filedialog.askdirectory(title="Selecionar Pasta de Imagens")
            dialog.attributes("-topmost", True)
            dialog.lift()
            
            if not folder:
                return

            selected_dir_var.set(folder)
            state["directory"] = folder

            df_prods = self.db.listar_produtos()
            if df_prods.empty:
                messagebox.showerror("Erro", "Não há produtos cadastrados no sistema para associar.", parent=dialog)
                return

            for w in scroll.winfo_children():
                w.destroy()

            valid_exts = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
            files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f)) and f.lower().endswith(valid_exts)]
            
            if not files:
                ctk.CTkLabel(scroll, text="Nenhuma imagem válida encontrada nesta pasta.", font=("Aileron", 12), text_color="#E74C3C").pack(pady=40)
                btn_confirmar.configure(state="disabled")
                return

            state["matches"] = []
            
            for filename in files:
                name_without_ext = os.path.splitext(filename)[0].strip()
                file_path = os.path.join(folder, filename)
                
                matched_row = None
                
                match_barcode = df_prods[df_prods['codigo_barras'].astype(str).str.strip() == name_without_ext]
                if not match_barcode.empty:
                    matched_row = match_barcode.iloc[0]
                    match_type = "Código de Barras"
                else:
                    match_name = df_prods[df_prods['nome'].astype(str).str.lower().str.strip() == name_without_ext.lower()]
                    if not match_name.empty:
                        matched_row = match_name.iloc[0]
                        match_type = "Nome Exato"
                    else:
                        match_sub = df_prods[df_prods['nome'].astype(str).str.lower().str.contains(name_without_ext.lower(), na=False)]
                        if not match_sub.empty:
                            matched_row = match_sub.iloc[0]
                            match_type = "Nome Parcial"
                
                if matched_row is not None:
                    status = "Associar"
                    prod_name = matched_row["nome"]
                    prod_id = int(matched_row["id"])
                    has_current = bool(matched_row.get("imagem"))
                    if has_current:
                        status = "Substituir Imagem"
                else:
                    status = "Não Correspondido"
                    prod_name = "Nenhum"
                    prod_id = None
                    match_type = ""

                state["matches"].append({
                    "file_path": file_path,
                    "file_name": filename,
                    "product_id": prod_id,
                    "product_name": prod_name,
                    "status": status,
                    "match_type": match_type
                })

            total_matched = 0
            for idx, match in enumerate(state["matches"]):
                item_f = ctk.CTkFrame(scroll, fg_color=("#F5F5F5", "#3A3A3A"), corner_radius=8)
                item_f.pack(fill="x", pady=2, padx=2)
                
                ctk.CTkLabel(item_f, text=match["file_name"], font=("Courier", 10), text_color=("#333", "#CCC")).pack(side="left", padx=10)
                
                if match["product_id"]:
                    total_matched += 1
                    status_text = f"→ {match['product_name']} ({match['match_type']})"
                    status_color = "#28a745" if "Substituir" not in match["status"] else "#F39C12"
                    
                    if "Substituir" in match["status"]:
                        status_text += " [Substituir]"
                else:
                    status_text = "→ (Sem correspondência)"
                    status_color = "gray"
                    
                ctk.CTkLabel(item_f, text=status_text, font=("Aileron", 11, "bold"), text_color=status_color).pack(side="left", padx=10)
            
            if total_matched > 0:
                btn_confirmar.configure(state="normal", text=f"Associar {total_matched} Imagens", command=confirmar_importacao_imagens)
            else:
                btn_confirmar.configure(state="disabled", text="Nenhuma imagem correspondida")

        btn_procurar = ctk.CTkButton(dir_frame, text="Escolher Pasta", command=selecionar_pasta, width=120)
        btn_procurar.pack(side="right")

        def confirmar_importacao_imagens():
            matches = state["matches"]
            if not matches:
                return
                
            cursor = self.db.conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            count = 0
            try:
                for match in matches:
                    if not match["product_id"]:
                        continue
                    
                    img_name = self.db.importar_imagem_avulsa(match["file_path"], match["product_name"])
                    if img_name:
                        cursor.execute("UPDATE produtos SET imagem = ? WHERE id = ?", (img_name, match["product_id"]))
                        count += 1
                
                self.db.conn.commit()
            except Exception as e:
                self.db.conn.rollback()
                messagebox.showerror("Erro", f"Erro ao importar imagens: {e}", parent=dialog)
                return
                
            self.db.registrar_alteracao("Importar Imagens Lote", f"{count} imagens associadas aos produtos")
            messagebox.showinfo("Sucesso", f"{count} imagens associadas aos produtos com sucesso!", parent=dialog)
            dialog.destroy()
            self.refresh_dashboard()

    def show_product_qrcode_dialog(self, product_row):
        """Exibe uma janela com o QR Code do produto contendo seus dados em formato JSON."""
        import json
        from PIL import Image
        
        try:
            import qrcode
        except ImportError:
            messagebox.showerror("Erro", "A biblioteca 'qrcode' não está instalada.", parent=self)
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"QR Code - {product_row['nome']}")
        dialog.geometry("400x500")
        dialog.attributes("-topmost", True)
        dialog.lift()
        dialog.focus_force()

        main_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        main_frame.pack(padx=20, pady=20, fill="both", expand=True)

        ctk.CTkLabel(main_frame, text=product_row['nome'], font=("Aileron", 16, "bold"), text_color=("#0A4174", "#7BBDE8")).pack(pady=(0, 5))
        ctk.CTkLabel(main_frame, text=f"Preço: R$ {product_row['preco']:.2f}", font=("Aileron", 13), text_color="gray").pack(pady=(0, 15))

        # Montar payload JSON
        payload = {
            "nome": product_row["nome"],
            "preco": float(product_row["preco"]),
            "codigo_barras": product_row.get("codigo_barras") or ""
        }
        qr_data = json.dumps(payload, ensure_ascii=False)

        # Gerar o QR Code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=8,
            border=2
        )
        qr.add_data(qr_data)
        qr.make(fit=True)

        qr_img = qr.make_image(fill_color="black", back_color="white")
        
        # Converter para formato compatível com CTk
        qr_img_resized = qr_img.resize((250, 250), Image.Resampling.NEAREST)
        
        from customtkinter import CTkImage
        ctk_qr = CTkImage(light_image=qr_img_resized, dark_image=qr_img_resized, size=(250, 250))
        
        lbl_qr = ctk.CTkLabel(main_frame, image=ctk_qr, text="")
        lbl_qr.pack(pady=10)

        # Botão para salvar
        def salvar_imagem():
            dialog.attributes("-topmost", False)
            dest_file = filedialog.asksaveasfilename(
                defaultextension=".png",
                filetypes=[("Imagens PNG", "*.png")],
                initialfile=f"qrcode_{product_row['nome']}.png",
                parent=dialog
            )
            dialog.attributes("-topmost", True)
            dialog.lift()
            
            if dest_file:
                qr_img.save(dest_file)
                messagebox.showinfo("Sucesso", "QR Code salvo com sucesso!", parent=dialog)

        ctk.CTkButton(main_frame, text="💾 Salvar QR Code", command=salvar_imagem, height=35, fg_color="#6EA2B3", hover_color="#49769F", text_color="white", font=("Aileron", 11, "bold")).pack(fill="x", pady=15)


    def open_report_dialog(self):
        window = ctk.CTkToplevel(self)
        window.title("Relatórios & Inteligência de Negócio")
        window.geometry("1000x750")
        window.attributes("-topmost", True)
        
        # Tabview structure
        tabview = ctk.CTkTabview(window, width=900, height=650)
        tabview.pack(padx=20, pady=20, fill="both", expand=True)
        
        tab_dashboard = tabview.add("Dashboard & IA")
        tab_vendas = tabview.add("Vendas")
        tab_lucro = tabview.add("Lucro & Ranking")
        tab_fiado = tabview.add("Fiados")
        tab_historico = tabview.add("Histórico Completo")
        
        # =======================
        # TAB 1: DASHBOARD
        # =======================
        from services.relatorio_service import RelatorioService

        rel_svc = RelatorioService(
            self.db,
            outbox_service=getattr(self, "outbox_service", None),
        )
        dash = rel_svc.dados_dashboard()
        prev_rep = rel_svc.previsao_vendas(treinar=False)
        sales_today = dash["faturamento_hoje"]
        count_sales_today = dash["qtd_vendas_hoje"]
        pred_text = prev_rep["texto"]
        pred_details = prev_rep["detalhes"]

        # UI Layout for Dashboard
        dash_frame = ctk.CTkScrollableFrame(tab_dashboard, fg_color="transparent")
        dash_frame.pack(fill="both", expand=True)

        ctk.CTkLabel(dash_frame, text=f"Visão Geral - {datetime.date.today().strftime('%d/%m/%Y')}", 
                    font=("Aileron", 20, "bold"), text_color=("#001D39", "white")).pack(pady=10, anchor="w")

        # Pacote Mensal do Contador (ZIP) — processamento em thread com feedback
        pacote_frame = ctk.CTkFrame(dash_frame, fg_color=("white", "#2B2B2B"), corner_radius=12)
        pacote_frame.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            pacote_frame,
            text="📦 Pacote Mensal do Contador (CSVs + DRE + XMLs NF-e):",
            font=("Aileron", 12, "bold"),
            text_color=("#001D39", "white"),
        ).pack(side="left", padx=12, pady=10)

        entry_comp_zip = ctk.CTkEntry(pacote_frame, width=95, placeholder_text="AAAA-MM")
        entry_comp_zip.insert(0, datetime.date.today().strftime("%Y-%m"))
        entry_comp_zip.pack(side="left", padx=6)

        lbl_zip_status = ctk.CTkLabel(pacote_frame, text="", font=("Aileron", 11), text_color="#0A4174")

        def _exportar_pacote_zip():
            comp = entry_comp_zip.get().strip() or datetime.date.today().strftime("%Y-%m")
            dest = filedialog.asksaveasfilename(
                parent=window,
                title="Salvar Pacote do Contador (ZIP)",
                defaultextension=".zip",
                initialfile=f"pacote_contador_{comp.replace('/', '-')}.zip",
                filetypes=[("Arquivo ZIP", "*.zip")],
            )
            if not dest:
                return
            btn_pacote_zip.configure(state="disabled", text="⏳ Gerando ZIP...")
            lbl_zip_status.configure(text="Exportando em segundo plano...")

            def _worker():
                ok_z, res_z = rel_svc.gerar_pacote_contador(mes_ano=comp, destino_zip_path=dest)
                def _done():
                    if not window.winfo_exists():
                        return
                    btn_pacote_zip.configure(state="normal", text="⬇️ Gerar ZIP do Contador")
                    if ok_z:
                        lbl_zip_status.configure(text=f"Salvo ({res_z['qtd_vendas']} vendas, {res_z['qtd_xmls']} XMLs)")
                        messagebox.showinfo(
                            "Pacote do Contador Gerado",
                            f"Arquivo ZIP gerado com sucesso:\n{res_z['zip_path']}\n\n"
                            f"• Competência: {res_z['mes_ano']}\n"
                            f"• Vendas incluídas: {res_z['qtd_vendas']}\n"
                            f"• Entradas: {res_z['qtd_entradas']} | Gastos: {res_z['qtd_gastos']}\n"
                            f"• XMLs NF-e anexados: {res_z['qtd_xmls']}",
                            parent=window,
                        )
                    else:
                        lbl_zip_status.configure(text="Erro ao gerar")
                        messagebox.showerror("Erro", str(res_z), parent=window)
                window.after(0, _done)

            threading.Thread(target=_worker, daemon=True).start()

        btn_pacote_zip = ctk.CTkButton(
            pacote_frame,
            text="⬇️ Gerar ZIP do Contador",
            command=_exportar_pacote_zip,
            height=34,
            fg_color="#0A4174",
            hover_color="#062F56",
            text_color="white",
            font=("Aileron", 12, "bold"),
        )
        btn_pacote_zip.pack(side="right", padx=12, pady=8)
        lbl_zip_status.pack(side="right", padx=8)

        # KPI Cards Row
        kpi_row = ctk.CTkFrame(dash_frame, fg_color="transparent")
        kpi_row.pack(fill="x", pady=10)
        
        def create_card(parent, title, value, subtext, color):
            card = ctk.CTkFrame(parent, fg_color=color, corner_radius=15)
            card.pack(side="left", fill="both", expand=True, padx=5)
            ctk.CTkLabel(card, text=title, font=("Aileron", 14, "bold"), text_color="white").pack(pady=(15,5), padx=10)
            ctk.CTkLabel(card, text=value, font=("Aileron", 24, "bold"), text_color="white").pack(pady=5, padx=10)
            ctk.CTkLabel(card, text=subtext, font=("Aileron", 11), text_color="#E0E0E0").pack(pady=(0,15), padx=10)

        create_card(kpi_row, "Faturamento Hoje", f"R$ {sales_today:.2f}", f"{count_sales_today} vendas realizadas", "#28a745")
        create_card(kpi_row, "Previsão Gastos (Próx. Mês)", pred_text, pred_details, "#0A4174")

        lucro_mes = dash["lucro_mes"]
        fat_mes = dash["faturamento_mes"]
        fiado_pend = dash["fiado_pendente"]

        kpi_row2 = ctk.CTkFrame(dash_frame, fg_color="transparent")
        kpi_row2.pack(fill="x", pady=5)
        create_card(kpi_row2, "Lucro do Mês", f"R$ {lucro_mes:.2f}", f"Faturamento: R$ {fat_mes:.2f}", "#6C3483")
        create_card(kpi_row2, "Fiados Pendentes", f"R$ {fiado_pend:.2f}", "Contas a receber", "#E67E22" if fiado_pend > 0 else "#28a745")

        fat_atual = dash["faturamento_mes_atual"]
        fat_ant = dash["faturamento_mes_anterior"]
        variacao = dash["variacao_mensal_pct"]
        seta = "▲" if variacao >= 0 else "▼"
        cor_var = "#28a745" if variacao >= 0 else "#FF6B6B"

        kpi_row3 = ctk.CTkFrame(dash_frame, fg_color="transparent")
        kpi_row3.pack(fill="x", pady=5)
        create_card(kpi_row3, "Mês Atual", f"R$ {fat_atual:.2f}", f"{seta} {variacao:+.1f}% vs anterior", cor_var)
        create_card(kpi_row3, "Mês Anterior", f"R$ {fat_ant:.2f}", "Faturamento do mês passado", "#5D6D7E")

        # Stock Alert Section
        ctk.CTkLabel(dash_frame, text="Alertas de Estoque Baixo", font=("Aileron", 16, "bold"), text_color="#FF6B6B").pack(pady=(20, 5), anchor="w")
        
        alert_frame = ctk.CTkFrame(dash_frame, fg_color=("white", "#2B2B2B"))
        alert_frame.pack(fill="x")
        
        if not dash["alertas_estoque"]:
            ctk.CTkLabel(alert_frame, text="Nenhum produto com estoque crítico.", text_color="gray").pack(pady=20)
        else:
            for row in dash["alertas_estoque"]:
                ctk.CTkLabel(alert_frame, text=f"• {row['nome']} (Restam: {row['quantidade']})",
                            font=("Aileron", 12, "bold"), text_color="#FF6B6B", anchor="w").pack(fill="x", padx=15, pady=2)

        # Alerta de Validade
        ctk.CTkLabel(dash_frame, text="Produtos com Validade Próxima", font=("Aileron", 16, "bold"), text_color="#E67E22").pack(pady=(20, 5), anchor="w")
        val_frame = ctk.CTkFrame(dash_frame, fg_color=("white", "#2B2B2B"))
        val_frame.pack(fill="x")
        if not dash["alertas_validade"]:
            ctk.CTkLabel(val_frame, text="Nenhum produto vencendo nos próximos 7 dias.", text_color="gray").pack(pady=20)
        else:
            for row in dash["alertas_validade"]:
                venc_date = row['data_validade']
                status_txt = "VENCIDO!" if row['vencido'] else f"Vence: {venc_date}"
                cor = "#FF0000" if row['vencido'] else "#E67E22"
                ctk.CTkLabel(val_frame, text=f"• {row['nome']} (Qtd: {row['quantidade']}) - {status_txt}",
                            font=("Aileron", 12, "bold"), text_color=cor, anchor="w").pack(fill="x", padx=15, pady=2)

        # =======================
        # TAB 2: VENDAS
        # =======================
        vendas_scroll = ctk.CTkScrollableFrame(tab_vendas, fg_color="transparent")
        vendas_scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(vendas_scroll, text="Histórico de Vendas", font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack(pady=10, anchor="w")

        # Filtros de vendas
        vf_frame = ctk.CTkFrame(vendas_scroll, fg_color=("white", "#2B2B2B"), corner_radius=10)
        vf_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(vf_frame, text="Período:", font=("Aileron", 11, "bold")).pack(side="left", padx=10, pady=10)
        combo_venda_periodo = ctk.CTkComboBox(vf_frame, values=["Hoje", "Semana", "Mês", "Ano", "Todos"], width=120)
        combo_venda_periodo.set("Mês")
        combo_venda_periodo.pack(side="left", padx=5)

        # Treeview de vendas
        vt_frame = ctk.CTkFrame(vendas_scroll, fg_color="transparent", height=300)
        vt_frame.pack(fill="both", expand=True, pady=5)
        vt_frame.pack_propagate(False)

        v_cols = ("id", "data", "produto", "qtd", "valor", "lucro", "pgto", "cliente", "status")
        tree_vendas = ttk.Treeview(vt_frame, columns=v_cols, show="headings", height=12)
        for c, w in [("id", 40), ("data", 130), ("produto", 120), ("qtd", 50), ("valor", 80), ("lucro", 70), ("pgto", 90), ("cliente", 100), ("status", 80)]:
            tree_vendas.heading(c, text=c.capitalize())
            tree_vendas.column(c, width=w, anchor="center" if c in ("id", "qtd") else "w")
        tree_vendas.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(vt_frame, orient="vertical", command=tree_vendas.yview).pack(side="right", fill="y")

        def carregar_vendas():
            for i in tree_vendas.get_children(): tree_vendas.delete(i)
            periodo_map = {"Hoje": "hoje", "Semana": "semana", "Mês": "mes", "Ano": "ano", "Todos": "todos"}
            per = periodo_map.get(combo_venda_periodo.get(), "todos")
            try:
                for r in rel_svc.listar_vendas_relatorio(per):
                    tree_vendas.insert("", "end", values=(
                        r['id'], r['data_venda'], r['produto_nome'], r['quantidade'],
                        f"R$ {r['valor_total']:.2f}", f"R$ {r['lucro']:.2f}",
                        r['forma_pagamento'], r['cliente'], r['status']
                    ))
            except Exception as e:
                logger.warning("Erro ao listar vendas: %s", e)

        ctk.CTkButton(vf_frame, text="Filtrar", command=carregar_vendas, width=80, fg_color="#7BBDE8", text_color="#001D39").pack(side="left", padx=10)

        # Botão cancelar venda (exige supervisor + motivo obrigatório + estorno atômico)
        def cancelar_venda_selecionada():
            sel = tree_vendas.selection()
            if not sel:
                messagebox.showwarning("Aviso", "Selecione uma venda para cancelar.", parent=window)
                return
            vals = tree_vendas.item(sel[0])['values']
            venda_id = vals[0]
            if str(vals[8]).lower() == "cancelada":
                messagebox.showinfo("Aviso", "Esta venda já está cancelada.", parent=window)
                return
            ok_sup, sup_info = self._exigir_supervisor(
                f"Cancelar e estornar venda #{venda_id}",
                parent=window,
                exigir_motivo=True,
                exigir_senha_sempre=True,
            )
            if not ok_sup:
                return
            ok, msg = self.db.cancelar_venda(
                venda_id,
                motivo=sup_info.get("motivo", ""),
                usuario_id=sup_info.get("supervisor_id"),
            )
            if ok:
                messagebox.showinfo("Sucesso", msg, parent=window)
                self.refresh_dashboard()
            else:
                messagebox.showerror("Erro ao Cancelar", msg, parent=window)
            carregar_vendas()

        ctk.CTkButton(vf_frame, text="Cancelar Venda", command=cancelar_venda_selecionada, width=120, fg_color="#FF6B6B", hover_color="#FF5252", text_color="white").pack(side="right", padx=10)

        # Vendas por forma de pagamento
        ctk.CTkLabel(vendas_scroll, text="Vendas por Forma de Pagamento (Mês)", font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), anchor="w")
        try:
            pgto_rows = rel_svc.vendas_por_forma_pagamento("mes")
            if pgto_rows:
                pgto_frame_r = ctk.CTkFrame(vendas_scroll, fg_color=("white", "#2B2B2B"), corner_radius=10)
                pgto_frame_r.pack(fill="x", pady=5)
                for r in pgto_rows:
                    ctk.CTkLabel(pgto_frame_r, text=f"  {r['forma']}: R$ {r['total']:.2f} ({int(r['qtd'])} vendas)",
                                font=("Aileron", 12), text_color=("#333", "white"), anchor="w").pack(fill="x", padx=15, pady=3)
            else:
                ctk.CTkLabel(vendas_scroll, text="Sem dados de vendas este mês.", text_color="gray").pack()
        except Exception: pass

        carregar_vendas()

        # =======================
        # TAB 3: LUCRO & RANKING
        # =======================
        lucro_scroll = ctk.CTkScrollableFrame(tab_lucro, fg_color="transparent")
        lucro_scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(lucro_scroll, text="Análise de Lucro & Ranking", font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack(pady=10, anchor="w")

        dados_lr = rel_svc.dados_lucro_e_ranking(dias_grafico=30, limite_top=10)

        # Lucro por período
        lucro_cards = ctk.CTkFrame(lucro_scroll, fg_color="transparent")
        lucro_cards.pack(fill="x", pady=5)
        for label, per in [("Hoje", "hoje"), ("Semana", "semana"), ("Mês", "mes"), ("Ano", "ano")]:
            lp = dados_lr["lucros_periodo"].get(per, {"lucro": 0.0, "faturamento": 0.0})
            create_card(lucro_cards, f"Lucro {label}", f"R$ {lp['lucro']:.2f}", f"Fat: R$ {lp['faturamento']:.2f}", "#6C3483")

        # Ranking de Produtos
        ctk.CTkLabel(lucro_scroll, text="Top 10 Produtos Mais Vendidos (Mês)", font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), anchor="w")
        try:
            rank_rows = dados_lr["ranking_vendidos"]
            if rank_rows:
                rank_frame = ctk.CTkFrame(lucro_scroll, fg_color="transparent", height=200)
                rank_frame.pack(fill="both", expand=True, pady=5)
                rank_frame.pack_propagate(False)
                r_cols = ("pos", "produto", "qtd", "faturamento", "lucro")
                tree_rank = ttk.Treeview(rank_frame, columns=r_cols, show="headings", height=10)
                for c, h, w in [("pos", "#", 30), ("produto", "Produto", 180), ("qtd", "Qtd Vendida", 80), ("faturamento", "Faturamento", 100), ("lucro", "Lucro", 100)]:
                    tree_rank.heading(c, text=h)
                    tree_rank.column(c, width=w, anchor="center" if c in ("pos", "qtd") else "w")
                tree_rank.pack(fill="both", expand=True)
                for i, r in enumerate(rank_rows, 1):
                    tree_rank.insert("", "end", values=(i, r['nome'] or 'N/A', int(r['total_vendido']), f"R$ {r['faturamento']:.2f}", f"R$ {r['lucro_total']:.2f}"))
            else:
                ctk.CTkLabel(lucro_scroll, text="Sem dados de vendas para ranking.", text_color="gray").pack()
        except Exception as e:
            logger.warning("Erro ranking: %s", e)

        # Ranking de Lucro por Produto/Tamanho
        ctk.CTkLabel(lucro_scroll, text="Top 10 Lucro por Produto / Tamanho (Mês)", font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), anchor="w")
        try:
            lt_rows = dados_lr["ranking_lucro_tamanho"]
            if lt_rows:
                lt_frame = ctk.CTkFrame(lucro_scroll, fg_color="transparent", height=200)
                lt_frame.pack(fill="both", expand=True, pady=5)
                lt_frame.pack_propagate(False)
                lt_cols = ("pos", "produto", "tamanho", "qtd", "faturamento", "lucro")
                tree_lt = ttk.Treeview(lt_frame, columns=lt_cols, show="headings", height=10)
                for c, h, w in [("pos", "#", 30), ("produto", "Produto", 160), ("tamanho", "Tamanho", 80), ("qtd", "Qtd", 60), ("faturamento", "Faturamento", 100), ("lucro", "Lucro", 100)]:
                    tree_lt.heading(c, text=h)
                    tree_lt.column(c, width=w, anchor="center" if c in ("pos", "qtd", "tamanho") else "w")
                tree_lt.pack(fill="both", expand=True)
                for i, r in enumerate(lt_rows, 1):
                    tam = r.get('tamanho', '') or 'Único'
                    tree_lt.insert("", "end", values=(i, r['nome'] or 'N/A', tam, int(r['total_vendido']), f"R$ {r['faturamento']:.2f}", f"R$ {r['lucro_total']:.2f}"))
            else:
                ctk.CTkLabel(lucro_scroll, text="Sem dados de lucro por tamanho.", text_color="gray").pack()
        except Exception as e:
            logger.warning("Erro ranking lucro tamanho: %s", e)

        # Curva ABC (Análise de Pareto)
        ctk.CTkLabel(lucro_scroll, text="📊 Curva ABC (Pareto) — Mês", font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), anchor="w")
        try:
            abc_itens = dados_lr["curva_abc"]["itens"]
            abc_contagem = dados_lr["curva_abc"]["contagem"]
            if abc_itens:
                abc_frame = ctk.CTkFrame(lucro_scroll, fg_color="transparent", height=220)
                abc_frame.pack(fill="both", expand=True, pady=5)
                abc_frame.pack_propagate(False)
                abc_cols = ("classe", "produto", "faturamento", "pct", "pct_acum")
                tree_abc = ttk.Treeview(abc_frame, columns=abc_cols, show="headings", height=10)
                for c, h, w in [("classe", "Classe", 50), ("produto", "Produto", 180), ("faturamento", "Faturamento", 100), ("pct", "% Fat.", 60), ("pct_acum", "% Acum.", 70)]:
                    tree_abc.heading(c, text=h)
                    tree_abc.column(c, width=w, anchor="center" if c != "produto" else "w")
                tree_abc.tag_configure("A", background="#D4EDDA")
                tree_abc.tag_configure("B", background="#FFF3CD")
                tree_abc.tag_configure("C", background="#F8D7DA")
                tree_abc.pack(fill="both", expand=True)
                for r in abc_itens:
                    tree_abc.insert("", "end", values=(
                        r['classe'], r['nome'] or 'N/A',
                        f"R$ {r['faturamento']:.2f}", f"{r['pct']:.1f}%", f"{r['pct_acum']:.1f}%"
                    ), tags=(r['classe'],))
                # Resumo
                resumo_f = ctk.CTkFrame(lucro_scroll, fg_color="transparent")
                resumo_f.pack(fill="x", pady=5)
                for cls, cor, desc in [("A", "#28a745", "80% do faturamento"), ("B", "#ffc107", "15% do faturamento"), ("C", "#dc3545", "5% do faturamento")]:
                    n = abc_contagem.get(cls, 0)
                    ctk.CTkLabel(resumo_f, text=f"  {cls}: {n} produtos ({desc})  ", font=("Aileron", 11, "bold"),
                                fg_color=cor, text_color="white" if cls != "B" else "#333", corner_radius=5).pack(side="left", padx=3)
            else:
                ctk.CTkLabel(lucro_scroll, text="Sem dados de vendas para análise ABC.", text_color="gray").pack()
        except Exception as e:
            logger.warning("Erro curva ABC: %s", e)

        # Gráfico de vendas ao longo do tempo
        ctk.CTkLabel(lucro_scroll, text="Faturamento Diário (Últimos 30 dias)", font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(15, 5), anchor="w")
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

            serie_dias = dados_lr["serie_diaria"]
            dias_lista = serie_dias["dias"]
            if dias_lista:
                fig = Figure(figsize=(9, 3), dpi=80)
                ax = fig.add_subplot(111)
                n_pts = len(dias_lista)
                ax.fill_between(range(n_pts), serie_dias['faturamento'], alpha=0.3, color='#28a745')
                ax.plot(range(n_pts), serie_dias['faturamento'], color='#28a745', linewidth=2, marker='o', markersize=3)
                ax.plot(range(n_pts), serie_dias['lucro'], color='#6C3483', linewidth=2, linestyle='--', marker='s', markersize=3)
                passo = max(1, n_pts // 7)
                ax.set_xticks(range(0, n_pts, passo))
                ax.set_xticklabels([str(d)[-5:] for d in dias_lista[::passo]], rotation=45, fontsize=7)
                ax.set_ylabel("R$", fontsize=9)
                ax.legend(["Faturamento", "Lucro"], fontsize=8)
                ax.grid(True, alpha=0.3)
                is_dark = ctk.get_appearance_mode() == "Dark"
                if is_dark:
                    fig.patch.set_facecolor('#2B2B2B')
                    ax.set_facecolor('#333333')
                    ax.tick_params(colors='white')
                    ax.yaxis.label.set_color('white')
                    ax.xaxis.label.set_color('white')
                    for spine in ax.spines.values():
                        spine.set_color('#555555')
                fig.tight_layout()
                chart_frame = ctk.CTkFrame(lucro_scroll, fg_color="transparent", height=250)
                chart_frame.pack(fill="x", pady=5)
                chart_frame.pack_propagate(False)
                canvas = FigureCanvasTkAgg(fig, master=chart_frame)
                canvas.draw()
                canvas.get_tk_widget().pack(fill="both", expand=True)
            else:
                ctk.CTkLabel(lucro_scroll, text="Sem dados de vendas para gráfico.", text_color="gray").pack()
        except Exception as e:
            logger.warning("Erro gráfico temporal: %s", e)

        # =======================
        # TAB 4: FIADOS / CONTAS A RECEBER
        # =======================
        fiado_scroll = ctk.CTkScrollableFrame(tab_fiado, fg_color="transparent")
        fiado_scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(fiado_scroll, text="Contas a Receber (Fiado)", font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack(pady=10, anchor="w")

        total_fiado = dash["fiado_pendente"]
        ctk.CTkLabel(fiado_scroll, text=f"Total Pendente: R$ {total_fiado:.2f}", font=("Aileron", 16, "bold"),
                    text_color="#E67E22" if total_fiado > 0 else "#28a745").pack(pady=5, anchor="w")

        # Filtro
        ff_frame = ctk.CTkFrame(fiado_scroll, fg_color=("white", "#2B2B2B"), corner_radius=10)
        ff_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(ff_frame, text="Status:", font=("Aileron", 11, "bold")).pack(side="left", padx=10, pady=10)
        combo_fiado_status = ctk.CTkComboBox(ff_frame, values=["Pendente", "Pago", "Todos"], width=120)
        combo_fiado_status.set("Pendente")
        combo_fiado_status.pack(side="left", padx=5)

        ft_frame = ctk.CTkFrame(fiado_scroll, fg_color="transparent", height=250)
        ft_frame.pack(fill="both", expand=True, pady=5)
        ft_frame.pack_propagate(False)

        f_cols = ("id", "cliente", "valor", "data_venda", "vencimento", "status")
        tree_fiado = ttk.Treeview(ft_frame, columns=f_cols, show="headings", height=10)
        for c, h, w in [("id", "ID", 40), ("cliente", "Cliente", 150), ("valor", "Valor", 100), ("data_venda", "Data Venda", 100), ("vencimento", "Vencimento", 100), ("status", "Status", 80)]:
            tree_fiado.heading(c, text=h)
            tree_fiado.column(c, width=w, anchor="center" if c == "id" else "w")
        tree_fiado.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(ft_frame, orient="vertical", command=tree_fiado.yview).pack(side="right", fill="y")

        def carregar_fiados():
            for i in tree_fiado.get_children(): tree_fiado.delete(i)
            try:
                res_f = rel_svc.listar_fiados(status=combo_fiado_status.get())
                for r in res_f["itens"]:
                    tree_fiado.insert("", "end", values=(
                        r['id'], r.get('cliente_nome', 'N/A'), f"R$ {r['valor']:.2f}",
                        r['data_venda'], r.get('data_vencimento', 'N/A'), r['status']
                    ))
            except Exception as e:
                logger.warning("Erro ao listar fiados: %s", e)

        ctk.CTkButton(ff_frame, text="Filtrar", command=carregar_fiados, width=80, fg_color="#7BBDE8", text_color="#001D39").pack(side="left", padx=10)

        def quitar_selecionado():
            sel = tree_fiado.selection()
            if not sel:
                messagebox.showwarning("Aviso", "Selecione uma conta para quitar.", parent=window)
                return
            conta_id = tree_fiado.item(sel[0])['values'][0]
            if messagebox.askyesno("Confirmar", f"Marcar conta #{conta_id} como paga?", parent=window):
                rel_svc.quitar_fiado(conta_id)
                messagebox.showinfo("Sucesso", "Conta quitada!", parent=window)
                carregar_fiados()

        def cobrar_fiado_whatsapp():
            sel = tree_fiado.selection()
            if not sel:
                messagebox.showwarning("Aviso", "Selecione uma conta na lista para cobrar via WhatsApp.", parent=window)
                return
            vals = tree_fiado.item(sel[0])['values']
            conta_id, cli_nome, st_conta = vals[0], vals[1], vals[5]
            if st_conta == "Pago":
                messagebox.showinfo("Já Quitada", "Esta conta já consta como paga.", parent=window)
                return

            contato = rel_svc.obter_telefone_cliente(cli_nome)
            if not contato:
                from tkinter import simpledialog
                contato = simpledialog.askstring(
                    "Telefone do Cliente",
                    f"Cliente '{cli_nome}' não possui telefone cadastrado.\nDigite o WhatsApp com DDD (ex: 11999999999):",
                    parent=window
                ) or ""
                if not contato.strip():
                    return

            try:
                cobr = rel_svc.gerar_cobranca_fiado_pix(
                    conta_id=int(conta_id),
                    telefone=contato,
                    usar_mercadopago=False,
                    enfileirar_whatsapp=False,
                )
            except ValueError as exc:
                messagebox.showwarning("Erro WhatsApp", str(exc), parent=window)
                return

            ok_q, info_q = self._enviar_whatsapp_resiliente(
                cobr["telefone"], cobr["mensagem"], metadata={"origem": "cobranca_fiado"}
            )
            if ok_q:
                messagebox.showinfo("WhatsApp (Outbox)", f"Cobrança para {cli_nome} ({cobr['telefone']}) adicionada à fila Outbox.", parent=window)
            else:
                messagebox.showwarning("Erro WhatsApp", f"Não foi possível enfileirar: {info_q}", parent=window)

        ctk.CTkButton(ff_frame, text="Quitar Selecionado", command=quitar_selecionado, width=140, fg_color="#28a745", hover_color="#218838").pack(side="right", padx=(5, 10))
        ctk.CTkButton(ff_frame, text="💬 Cobrar via WhatsApp", command=cobrar_fiado_whatsapp, width=165, fg_color="#00897B", hover_color="#00695C", text_color="white").pack(side="right", padx=5)

        carregar_fiados()

        # =======================
        # TAB 2: HISTORY (Existing Logic Improved)
        # =======================
        
        filter_frame = ctk.CTkFrame(tab_historico, fg_color=("white", "#2B2B2B"), corner_radius=10)
        filter_frame.pack(fill="x", pady=(0, 15))
        
        ctk.CTkLabel(filter_frame, text="Filtros:", font=("Aileron", 12, "bold")).pack(side="left", padx=15, pady=15)
        
        combo_tipo = ctk.CTkComboBox(filter_frame, values=["Tudo", "Venda", "Estoque", "Financeiro", "Sistema"], width=150)
        combo_tipo.set("Tudo")
        combo_tipo.pack(side="left", padx=5)
        
        combo_periodo = ctk.CTkComboBox(filter_frame, values=["Histórico Completo", "Hoje", "Últimos 7 dias", "Últimos 30 dias"], width=150)
        combo_periodo.set("Histórico Completo")
        combo_periodo.pack(side="left", padx=5)
        
        # Table
        table_frame = ctk.CTkFrame(tab_historico, fg_color="transparent")
        table_frame.pack(fill="both", expand=True)
        
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#2B2B2B", fieldbackground="#2B2B2B", foreground="white", rowheight=30, borderwidth=0)
        style.configure("Treeview.Heading", background="#404040", foreground="white", relief="flat", font=('Aileron', 10, 'bold'))
        style.map("Treeview", background=[('selected', '#10367D')])
        
        columns = ("ID", "Data/Hora", "Ação", "Detalhes")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        
        tree.heading("ID", text="ID")
        tree.heading("Data/Hora", text="Data/Hora")
        tree.heading("Ação", text="Tipo / Ação")
        tree.heading("Detalhes", text="Detalhes")
        
        tree.column("ID", width=50, anchor="center")
        tree.column("Data/Hora", width=150, anchor="center")
        tree.column("Ação", width=150)
        tree.column("Detalhes", width=400)
        
        tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        
        def aplicar_filtro():
            for i in tree.get_children(): tree.delete(i)
            try:
                for row in rel_svc.listar_historico(periodo=combo_periodo.get(), tipo=combo_tipo.get()):
                    tree.insert("", "end", values=(row['id'], row['data_hora'], row['acao'], row['detalhe']))
            except Exception as e:
                logger.warning("Erro filtro: %s", e)
        
        ctk.CTkButton(filter_frame, text="Aplicar Filtros", command=aplicar_filtro, width=120, fg_color="#7BBDE8", text_color="#001D39", hover_color="#49769F").pack(side="left", padx=15)
        
        def exportar_csv():
            filename = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv")])
            if filename:
                rel_svc.exportar_historico_csv(
                    filename,
                    periodo=combo_periodo.get(),
                    tipo=combo_tipo.get(),
                )
                messagebox.showinfo("Sucesso", "Relatório exportado!", parent=window)
        
        ctk.CTkButton(filter_frame, text="Exportar Dados (CSV)", command=exportar_csv, width=150, fg_color="#0A4174", hover_color="#062F56").pack(side="right", padx=15)
        
        aplicar_filtro()

    # ==================== INSUMOS / MATÉRIA-PRIMA ====================

    def open_insumos_manager(self):
        """Gerenciador de insumos com baixa manual e ficha técnica."""
        window = ctk.CTkToplevel(self)
        window.title("Gerenciador de Insumos")
        window.geometry("900x650")
        window.attributes("-topmost", True)

        tabview = ctk.CTkTabview(window, fg_color="transparent")
        tabview.pack(fill="both", expand=True, padx=10, pady=10)

        tab_lista = tabview.add("Estoque de Insumos")
        tab_baixa = tabview.add("Baixa de Insumos")
        tab_fichas = tabview.add("Fichas Técnicas")

        # ===== ABA 1: ESTOQUE DE INSUMOS =====
        def refresh_insumos_list():
            for w in insumos_scroll.winfo_children():
                w.destroy()
            df_insumos = self.db.listar_insumos()
            if df_insumos.empty:
                ctk.CTkLabel(insumos_scroll, text="Nenhum insumo cadastrado.\nCadastre um produto com Tipo = 'insumo' na tela Adicionar.",
                            font=("Aileron", 12), text_color="gray").pack(pady=30)
                return
            for _, row in df_insumos.iterrows():
                f = ctk.CTkFrame(insumos_scroll, fg_color=("white", "#3A3A3A"), corner_radius=10,
                                border_width=1, border_color=("#7BBDE8", "#49769F"))
                f.pack(fill="x", pady=3, padx=5)
                un = row.get('unidade_medida', 'un') or 'un'
                txt = f"{row['nome']}  —  Estoque: {row['quantidade']} {un}"
                if row.get('categoria'):
                    txt += f"  |  {row['categoria']}"
                ctk.CTkLabel(f, text=txt, font=("Aileron", 12), anchor="w",
                            text_color=("#333", "white")).pack(side="left", padx=15, pady=10, fill="x", expand=True)
                # Indicador visual de estoque baixo
                est_min = row.get('estoque_minimo', 5) or 5
                if row['quantidade'] <= est_min:
                    ctk.CTkLabel(f, text="⚠ BAIXO", font=("Aileron", 10, "bold"),
                                text_color="#FF6B6B").pack(side="right", padx=10)

        ctk.CTkLabel(tab_lista, text="Estoque de Insumos / Matéria-Prima",
                    font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=10)
        ctk.CTkLabel(tab_lista, text="Para cadastrar insumos, use ADICIONAR e selecione Tipo = 'insumo'.",
                    font=("Aileron", 10, "italic"), text_color="gray").pack()
        insumos_scroll = ctk.CTkScrollableFrame(tab_lista, fg_color="transparent")
        insumos_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        refresh_insumos_list()

        # ===== ABA 2: BAIXA DE INSUMOS (uso diário) =====
        ctk.CTkLabel(tab_baixa, text="Baixa de Insumos Usados",
                    font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=10)
        ctk.CTkLabel(tab_baixa, text="Registre os insumos que foram utilizados hoje.",
                    font=("Aileron", 10, "italic"), text_color="gray").pack()

        baixa_form = ctk.CTkFrame(tab_baixa, fg_color=("white", "#2B2B2B"), corner_radius=15)
        baixa_form.pack(fill="x", padx=15, pady=10)

        df_ins_all = self.db.listar_insumos()
        insumos_nomes = df_ins_all['nome'].tolist() if not df_ins_all.empty else ["(nenhum)"]
        insumos_map = {}
        if not df_ins_all.empty:
            for _, r in df_ins_all.iterrows():
                insumos_map[r['nome']] = r

        ctk.CTkLabel(baixa_form, text="Insumo:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        combo_insumo = ctk.CTkComboBox(baixa_form, values=insumos_nomes, state="readonly")
        combo_insumo.pack(fill="x", padx=15, pady=3)

        ctk.CTkLabel(baixa_form, text="Quantidade:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_qtd_baixa = ctk.CTkEntry(baixa_form, placeholder_text="Ex: 2.5")
        e_qtd_baixa.pack(fill="x", padx=15, pady=3)

        ctk.CTkLabel(baixa_form, text="Motivo:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_motivo = ctk.CTkEntry(baixa_form, placeholder_text="Uso diário / Perda / Vencido...")
        e_motivo.insert(0, "Uso diário")
        e_motivo.pack(fill="x", padx=15, pady=3)

        def _fazer_baixa():
            from services.producao_service import ProducaoService
            nome_sel = combo_insumo.get()
            ins_id = int(insumos_map[nome_sel]['id']) if nome_sel in insumos_map else None
            try:
                msg = ProducaoService(self.db).baixar_insumo(ins_id, e_qtd_baixa.get(), e_motivo.get())
            except ValueError as e:
                messagebox.showerror("Erro", str(e), parent=window)
                return
            messagebox.showinfo("Sucesso", msg, parent=window)
            e_qtd_baixa.delete(0, "end")
            refresh_insumos_list()
            _refresh_historico_baixas()
            self.refresh_dashboard()

        ctk.CTkButton(baixa_form, text="REGISTRAR BAIXA", command=_fazer_baixa,
                      height=40, fg_color="#D4A373", hover_color="#B8956A", text_color="white",
                      font=("Aileron", 13, "bold")).pack(fill="x", padx=15, pady=15)

        # Histórico de baixas
        ctk.CTkLabel(tab_baixa, text="Histórico de Baixas (últimos 30 dias):",
                    font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(anchor="w", padx=15, pady=(10, 5))
        hist_baixa_scroll = ctk.CTkScrollableFrame(tab_baixa, fg_color="transparent", height=150)
        hist_baixa_scroll.pack(fill="both", expand=True, padx=15, pady=(0, 10))

        def _refresh_historico_baixas():
            for w in hist_baixa_scroll.winfo_children():
                w.destroy()
            df_b = self.db.listar_baixas_insumos(30)
            if df_b.empty:
                ctk.CTkLabel(hist_baixa_scroll, text="Nenhuma baixa registrada.", text_color="gray").pack(pady=10)
                return
            for _, r in df_b.iterrows():
                txt = f"{r['data']}  |  {r['insumo_nome']}  |  Qtd: {r['quantidade']}  |  {r['motivo']}"
                ctk.CTkLabel(hist_baixa_scroll, text=txt, font=("Aileron", 10),
                            text_color=("#555", "#CCC"), anchor="w").pack(fill="x", padx=5, pady=1)
        _refresh_historico_baixas()

        # ===== ABA 3: FICHAS TÉCNICAS =====
        ctk.CTkLabel(tab_fichas, text="Fichas Técnicas / Receitas",
                    font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=10)
        ctk.CTkLabel(tab_fichas, text="Vincule insumos a um produto final para produção automática.",
                    font=("Aileron", 10, "italic"), text_color="gray").pack()

        fichas_scroll = ctk.CTkScrollableFrame(tab_fichas, fg_color="transparent")
        fichas_scroll.pack(fill="both", expand=True, padx=10, pady=5)

        def _refresh_fichas():
            for w in fichas_scroll.winfo_children():
                w.destroy()
            df_fichas = self.db.listar_fichas_tecnicas()
            if df_fichas.empty:
                ctk.CTkLabel(fichas_scroll, text="Nenhuma ficha técnica cadastrada.",
                            text_color="gray").pack(pady=20)
                return
            for _, r in df_fichas.iterrows():
                f = ctk.CTkFrame(fichas_scroll, fg_color=("white", "#3A3A3A"), corner_radius=10,
                                border_width=1, border_color=("#7BBDE8", "#49769F"))
                f.pack(fill="x", pady=3, padx=5)
                txt = f"📋 {r['nome']}  →  {r['produto_nome']}  (Rend: {r['rendimento']}un)"
                ctk.CTkLabel(f, text=txt, font=("Aileron", 12), anchor="w",
                            text_color=("#333", "white")).pack(side="left", padx=15, pady=10, fill="x", expand=True)
                # Botão ver detalhes
                ctk.CTkButton(f, text="Ver", width=50, fg_color="#7BBDE8", text_color="#001D39",
                             command=lambda fid=r['id'], fn=r['nome']: _ver_ficha(fid, fn)).pack(side="right", padx=3, pady=5)
                ctk.CTkButton(f, text="✕", width=30, fg_color="#FF6B6B", text_color="white",
                             command=lambda fid=r['id']: (_remover_ficha(fid))).pack(side="right", padx=3, pady=5)

        def _ver_ficha(ficha_id, ficha_nome):
            df_itens = self.db.get_ficha_tecnica_itens(ficha_id)
            if df_itens.empty:
                messagebox.showinfo("Ficha Técnica", f"'{ficha_nome}' não tem insumos vinculados.", parent=window)
                return
            linhas = [f"📋 Ficha: {ficha_nome}\n"]
            for _, it in df_itens.iterrows():
                un = it.get('unidade_medida', 'un') or 'un'
                linhas.append(f"  • {it['insumo_nome']}: {it['quantidade']} {un} (Estoque: {it['estoque_atual']})")
            messagebox.showinfo("Ficha Técnica", "\n".join(linhas), parent=window)

        def _remover_ficha(ficha_id):
            if messagebox.askyesno("Confirmar", "Remover esta ficha técnica?", parent=window):
                from services.producao_service import ProducaoService
                ProducaoService(self.db).remover_ficha(ficha_id)
                _refresh_fichas()

        _refresh_fichas()

        # Botão criar nova ficha
        ctk.CTkButton(tab_fichas, text="+ NOVA FICHA TÉCNICA", command=lambda: self._open_criar_ficha_dialog(window, _refresh_fichas),
                      height=40, fg_color="#0A4174", hover_color="#062F56",
                      font=("Aileron", 13, "bold")).pack(fill="x", padx=15, pady=10)

    def _open_criar_ficha_dialog(self, parent_window, refresh_callback):
        """Dialog para criar uma nova ficha técnica."""
        dialog = ctk.CTkToplevel(parent_window)
        dialog.title("Nova Ficha Técnica")
        dialog.geometry("600x550")
        dialog.attributes("-topmost", True)

        main_frame = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(main_frame, text="Criar Ficha Técnica", font=("Aileron", 18, "bold"),
                    text_color=("#001D39", "white")).pack(pady=(0, 10))

        # Produto final
        ctk.CTkLabel(main_frame, text="Produto Final (que será produzido):", font=("Aileron", 11, "bold")).pack(anchor="w")
        df_prods = self.db.listar_produtos()
        df_vendaveis = df_prods[df_prods.get('tipo_produto', 'produto') != 'insumo'] if not df_prods.empty and 'tipo_produto' in df_prods.columns else df_prods
        prods_names = df_vendaveis['nome'].tolist() if not df_vendaveis.empty else ["(nenhum)"]
        prods_map = {}
        if not df_vendaveis.empty:
            for _, r in df_vendaveis.iterrows():
                prods_map[r['nome']] = int(r['id'])
        combo_prod = ctk.CTkComboBox(main_frame, values=prods_names, state="readonly")
        combo_prod.pack(fill="x", pady=5)

        # Nome da ficha
        ctk.CTkLabel(main_frame, text="Nome da Ficha/Receita:", font=("Aileron", 11, "bold")).pack(anchor="w", pady=(10, 0))
        e_nome_ficha = ctk.CTkEntry(main_frame, placeholder_text="Ex: Receita Pão Francês")
        e_nome_ficha.pack(fill="x", pady=5)

        # Rendimento
        ctk.CTkLabel(main_frame, text="Rendimento (unidades produzidas):", font=("Aileron", 11, "bold")).pack(anchor="w", pady=(10, 0))
        e_rendimento = ctk.CTkEntry(main_frame, placeholder_text="Ex: 50")
        e_rendimento.insert(0, "1")
        e_rendimento.pack(fill="x", pady=5)

        # Insumos da receita
        ctk.CTkLabel(main_frame, text="Insumos da Receita:", font=("Aileron", 13, "bold"),
                    text_color="#D4A373").pack(anchor="w", pady=(15, 5))

        itens_ficha = []
        itens_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        itens_frame.pack(fill="x", pady=5)

        df_insumos = self.db.listar_insumos()
        insumos_names = df_insumos['nome'].tolist() if not df_insumos.empty else ["(nenhum insumo cadastrado)"]
        insumos_id_map = {}
        if not df_insumos.empty:
            for _, r in df_insumos.iterrows():
                insumos_id_map[r['nome']] = int(r['id'])

        def _add_insumo_row():
            row_f = ctk.CTkFrame(itens_frame, fg_color="transparent")
            row_f.pack(fill="x", pady=2)
            c_ins = ctk.CTkComboBox(row_f, values=insumos_names, state="readonly", width=250)
            c_ins.pack(side="left", padx=(0, 5))
            e_q = ctk.CTkEntry(row_f, placeholder_text="Qtd", width=80)
            e_q.pack(side="left", padx=5)
            btn_rm = ctk.CTkButton(row_f, text="✕", width=28, height=28, fg_color="#FF6B6B",
                                   command=lambda: (_rm_row(row_f, item_data)))
            btn_rm.pack(side="left", padx=5)
            item_data = {'frame': row_f, 'combo': c_ins, 'qtd': e_q}
            itens_ficha.append(item_data)

        def _rm_row(frame, data):
            frame.destroy()
            if data in itens_ficha:
                itens_ficha.remove(data)

        _add_insumo_row()
        ctk.CTkButton(main_frame, text="+ Adicionar Insumo", height=30, fg_color="#28a745",
                      hover_color="#218838", text_color="white",
                      command=_add_insumo_row).pack(pady=5)

        def _salvar_ficha():
            from services.producao_service import ProducaoService
            prod_nome = combo_prod.get()
            nome_ficha = e_nome_ficha.get().strip()
            itens = [
                {'insumo_id': insumos_id_map.get(item['combo'].get()), 'quantidade': item['qtd'].get()}
                for item in itens_ficha
            ]
            try:
                ProducaoService(self.db).criar_ficha(
                    prods_map.get(prod_nome), nome_ficha, e_rendimento.get(), itens)
            except ValueError as e:
                messagebox.showerror("Erro", str(e), parent=dialog)
                return
            messagebox.showinfo("Sucesso", f"Ficha '{nome_ficha}' criada!", parent=dialog)
            refresh_callback()
            dialog.destroy()

        ctk.CTkButton(main_frame, text="SALVAR FICHA TÉCNICA", command=_salvar_ficha,
                      height=45, fg_color="#0A4174", hover_color="#062F56",
                      font=("Aileron", 14, "bold")).pack(fill="x", pady=15)

    # ==================== PRODUÇÃO DIÁRIA ====================

    def open_producao_dialog(self):
        """Dialog para registrar produção baseada em fichas técnicas."""
        window = ctk.CTkToplevel(self)
        window.title("Produção Diária")
        window.geometry("700x550")
        window.attributes("-topmost", True)

        main_frame = ctk.CTkScrollableFrame(window, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(main_frame, text="Registrar Produção", font=("Aileron", 18, "bold"),
                    text_color=("#001D39", "white")).pack(pady=(0, 5))
        ctk.CTkLabel(main_frame, text="Selecione uma ficha técnica e informe a quantidade produzida.\nOs insumos serão descontados automaticamente.",
                    font=("Aileron", 10, "italic"), text_color="gray").pack(pady=(0, 10))

        # Selecionar ficha técnica
        prod_card = ctk.CTkFrame(main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
        prod_card.pack(fill="x", pady=(0, 15))

        df_fichas = self.db.listar_fichas_tecnicas()
        fichas_map = {}
        fichas_names = []
        if not df_fichas.empty:
            for _, r in df_fichas.iterrows():
                display = f"{r['nome']} → {r['produto_nome']} (Rend: {r['rendimento']})"
                fichas_names.append(display)
                fichas_map[display] = int(r['id'])

        ctk.CTkLabel(prod_card, text="Ficha Técnica:", font=("Aileron", 12, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        combo_ficha = ctk.CTkComboBox(prod_card, values=fichas_names if fichas_names else ["(nenhuma ficha cadastrada)"], state="readonly")
        combo_ficha.pack(fill="x", padx=15, pady=5)

        # Mostrar insumos necessários
        lbl_insumos_info = ctk.CTkLabel(prod_card, text="", font=("Aileron", 10), text_color="gray", justify="left")
        lbl_insumos_info.pack(anchor="w", padx=15, pady=5)

        def _on_ficha_change(val):
            if val in fichas_map:
                fid = fichas_map[val]
                df_itens = self.db.get_ficha_tecnica_itens(fid)
                if not df_itens.empty:
                    linhas = []
                    for _, it in df_itens.iterrows():
                        un = it.get('unidade_medida', 'un') or 'un'
                        linhas.append(f"  • {it['insumo_nome']}: {it['quantidade']} {un} (tem: {it['estoque_atual']})")
                    lbl_insumos_info.configure(text="Insumos necessários (por lote):\n" + "\n".join(linhas))
                else:
                    lbl_insumos_info.configure(text="")

        combo_ficha.configure(command=_on_ficha_change)

        ctk.CTkLabel(prod_card, text="Quantidade a Produzir:", font=("Aileron", 12, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        e_qtd_prod = ctk.CTkEntry(prod_card, placeholder_text="Ex: 50")
        e_qtd_prod.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(prod_card, text="Observação:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_obs_prod = ctk.CTkEntry(prod_card, placeholder_text="(opcional)")
        e_obs_prod.pack(fill="x", padx=15, pady=5)

        def _registrar_producao():
            from services.producao_service import ProducaoService
            sel = combo_ficha.get()
            try:
                msg = ProducaoService(self.db).produzir(
                    fichas_map.get(sel), e_qtd_prod.get(), e_obs_prod.get())
            except ValueError as e:
                messagebox.showerror("Erro", str(e), parent=window)
                return
            messagebox.showinfo("Sucesso", msg, parent=window)
            e_qtd_prod.delete(0, "end")
            _refresh_historico_prod()
            self.refresh_dashboard()

        ctk.CTkButton(prod_card, text="PRODUZIR", command=_registrar_producao,
                      height=45, fg_color="#28a745", hover_color="#218838",
                      font=("Aileron", 14, "bold")).pack(fill="x", padx=15, pady=15)

        # Histórico de produções
        ctk.CTkLabel(main_frame, text="Histórico de Produções (últimos 30 dias):",
                    font=("Aileron", 13, "bold"), text_color=("#001D39", "white")).pack(anchor="w", pady=(10, 5))
        hist_prod_scroll = ctk.CTkScrollableFrame(main_frame, fg_color="transparent", height=150)
        hist_prod_scroll.pack(fill="both", expand=True, pady=(0, 10))

        def _refresh_historico_prod():
            for w in hist_prod_scroll.winfo_children():
                w.destroy()
            df_prod = self.db.listar_producoes(30)
            if df_prod.empty:
                ctk.CTkLabel(hist_prod_scroll, text="Nenhuma produção registrada.", text_color="gray").pack(pady=10)
                return
            for _, r in df_prod.iterrows():
                f = ctk.CTkFrame(hist_prod_scroll, fg_color=("white", "#3A3A3A"), corner_radius=8)
                f.pack(fill="x", pady=2, padx=3)
                txt = f"📦 {r['data']}  |  {r['ficha_nome']}  |  {r['quantidade_produzida']}un de {r['produto_nome']}"
                if r.get('observacao'):
                    txt += f"  |  {r['observacao']}"
                ctk.CTkLabel(f, text=txt, font=("Aileron", 10), text_color=("#555", "#CCC"),
                            anchor="w").pack(padx=10, pady=5, fill="x")
        _refresh_historico_prod()

    # ==================== TROCAS / DEVOLUÇÕES ====================

    def open_trocas_dialog(self):
        """Dialog para registrar trocas e devoluções (Loja de Roupas)."""
        window = ctk.CTkToplevel(self)
        window.title("Trocas e Devoluções")
        window.geometry("800x600")
        window.attributes("-topmost", True)

        tabview = ctk.CTkTabview(window, fg_color="transparent")
        tabview.pack(fill="both", expand=True, padx=10, pady=10)

        tab_nova = tabview.add("Nova Troca/Devolução")
        tab_hist = tabview.add("Histórico")

        # ===== ABA 1: NOVA TROCA =====
        main_f = ctk.CTkScrollableFrame(tab_nova, fg_color="transparent")
        main_f.pack(fill="both", expand=True)

        ctk.CTkLabel(main_f, text="Registrar Troca ou Devolução",
                    font=("Aileron", 16, "bold"), text_color=("#001D39", "white")).pack(pady=10)

        card = ctk.CTkFrame(main_f, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card.pack(fill="x", padx=10, pady=5)

        df_prods = self.db.listar_produtos()
        prods_names = df_prods['nome'].tolist() if not df_prods.empty else ["(nenhum)"]
        prods_map = {}
        if not df_prods.empty:
            for _, r in df_prods.iterrows():
                prods_map[r['nome']] = r

        # Tipo
        ctk.CTkLabel(card, text="Tipo:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        tipo_var = ctk.StringVar(value="troca")
        tipo_frame = ctk.CTkFrame(card, fg_color="transparent")
        tipo_frame.pack(fill="x", padx=15, pady=3)
        ctk.CTkRadioButton(tipo_frame, text="Troca", variable=tipo_var, value="troca").pack(side="left", padx=10)
        ctk.CTkRadioButton(tipo_frame, text="Devolução", variable=tipo_var, value="devolucao").pack(side="left", padx=10)

        # Cliente
        ctk.CTkLabel(card, text="Cliente:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        df_clientes = self.db.listar_clientes()
        clientes_opt = ["Consumidor Final"] + (df_clientes['nome'].tolist() if not df_clientes.empty else [])
        combo_cliente = ctk.CTkComboBox(card, values=clientes_opt)
        combo_cliente.pack(fill="x", padx=15, pady=3)

        # Produto devolvido
        ctk.CTkLabel(card, text="Produto Devolvido:", font=("Aileron", 12, "bold"), text_color="#FF6B6B").pack(anchor="w", padx=15, pady=(10, 0))
        combo_devolvido = ctk.CTkComboBox(card, values=prods_names, state="readonly")
        combo_devolvido.pack(fill="x", padx=15, pady=3)
        ctk.CTkLabel(card, text="Qtd Devolvida:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_qtd_dev = ctk.CTkEntry(card, placeholder_text="1")
        e_qtd_dev.insert(0, "1")
        e_qtd_dev.pack(fill="x", padx=15, pady=3)

        # Produto novo (troca)
        ctk.CTkLabel(card, text="Produto Novo (se troca):", font=("Aileron", 12, "bold"), text_color="#28a745").pack(anchor="w", padx=15, pady=(10, 0))
        combo_novo = ctk.CTkComboBox(card, values=["(nenhum)"] + prods_names, state="readonly")
        combo_novo.set("(nenhum)")
        combo_novo.pack(fill="x", padx=15, pady=3)
        ctk.CTkLabel(card, text="Qtd Nova:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_qtd_nova = ctk.CTkEntry(card, placeholder_text="1")
        e_qtd_nova.insert(0, "1")
        e_qtd_nova.pack(fill="x", padx=15, pady=3)

        # Diferença de valor
        ctk.CTkLabel(card, text="Diferença de Valor (R$):", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        ctk.CTkLabel(card, text="Positivo = cliente paga mais, Negativo = loja devolve", font=("Aileron", 9, "italic"), text_color="gray").pack(anchor="w", padx=15)
        e_diferenca = ctk.CTkEntry(card, placeholder_text="0.00")
        e_diferenca.insert(0, "0")
        e_diferenca.pack(fill="x", padx=15, pady=3)

        # Observação
        ctk.CTkLabel(card, text="Observação:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_obs_troca = ctk.CTkEntry(card, placeholder_text="(opcional)")
        e_obs_troca.pack(fill="x", padx=15, pady=3)

        def _registrar_troca():
            from services.troca_service import TrocaService
            dev_nome = combo_devolvido.get()
            dev_id = int(prods_map[dev_nome]['id']) if dev_nome in prods_map else None
            novo_nome = combo_novo.get()
            novo_id = (int(prods_map[novo_nome]['id'])
                       if novo_nome != "(nenhum)" and novo_nome in prods_map else None)
            loja_id = self.current_user.get("loja_id") if self.current_user else None
            try:
                msg = TrocaService(self.db).registrar(
                    combo_cliente.get(), dev_id, e_qtd_dev.get(), tipo=tipo_var.get(),
                    produto_novo_id=novo_id, qtd_nova=e_qtd_nova.get(),
                    diferenca=e_diferenca.get(), observacao=e_obs_troca.get(), loja_id=loja_id)
            except ValueError as e:
                messagebox.showerror("Erro", str(e), parent=window)
                return
            messagebox.showinfo("Sucesso", msg, parent=window)
            _refresh_hist_trocas()
            self.refresh_dashboard()

        ctk.CTkButton(card, text="CONFIRMAR", command=_registrar_troca,
                      height=45, fg_color="#0A4174", hover_color="#062F56",
                      font=("Aileron", 14, "bold")).pack(fill="x", padx=15, pady=15)

        # ===== ABA 2: HISTÓRICO =====
        hist_trocas_scroll = ctk.CTkScrollableFrame(tab_hist, fg_color="transparent")
        hist_trocas_scroll.pack(fill="both", expand=True, padx=10, pady=10)

        def _refresh_hist_trocas():
            for w in hist_trocas_scroll.winfo_children():
                w.destroy()
            df_t = self.db.listar_trocas(90)
            if df_t.empty:
                ctk.CTkLabel(hist_trocas_scroll, text="Nenhuma troca/devolução registrada.", text_color="gray").pack(pady=20)
                return
            for _, r in df_t.iterrows():
                f = ctk.CTkFrame(hist_trocas_scroll, fg_color=("white", "#3A3A3A"), corner_radius=10,
                                border_width=1, border_color=("#7BBDE8", "#49769F"))
                f.pack(fill="x", pady=3, padx=5)
                tipo_icon = "🔄" if r['tipo'] == 'troca' else "↩️"
                txt = f"{tipo_icon} {r['data'][:10]}  |  {r['tipo'].title()}  |  {r['cliente']}\n"
                txt += f"  Devolvido: {r['produto_devolvido_nome']} (x{r['qtd_devolvida']})"
                if r.get('produto_novo_nome'):
                    txt += f"  →  Novo: {r['produto_novo_nome']} (x{r['qtd_nova']})"
                if r.get('diferenca_valor', 0) != 0:
                    txt += f"  |  Diferença: R$ {r['diferenca_valor']:.2f}"
                ctk.CTkLabel(f, text=txt, font=("Aileron", 10), text_color=("#555", "#CCC"),
                            anchor="w", justify="left").pack(padx=15, pady=8, fill="x")
        _refresh_hist_trocas()

    # ==================== LISTA DE REPOSIÇÃO ====================

    def open_lista_reposicao(self):
        """Lista de produtos abaixo do estoque mínimo para reposição."""
        window = ctk.CTkToplevel(self)
        window.title("Lista de Reposição")
        window.geometry("700x500")
        window.attributes("-topmost", True)

        ctk.CTkLabel(window, text="Lista de Reposição Automática",
                    font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).pack(pady=15)
        ctk.CTkLabel(window, text="Produtos abaixo do estoque mínimo configurado.",
                    font=("Aileron", 10, "italic"), text_color="gray").pack()

        lista_scroll = ctk.CTkScrollableFrame(window, fg_color="transparent")
        lista_scroll.pack(fill="both", expand=True, padx=15, pady=10)

        from services.estoque_service import EstoqueService
        itens_repor = EstoqueService(self.db).itens_reposicao()
        if not itens_repor:
            ctk.CTkLabel(lista_scroll, text="✅ Todos os produtos estão acima do estoque mínimo!",
                        font=("Aileron", 14), text_color="#28a745").pack(pady=30)
        else:
            for r in itens_repor:
                f = ctk.CTkFrame(lista_scroll, fg_color=("white", "#3A3A3A"), corner_radius=10,
                                border_width=1, border_color=("#FF6B6B", "#CC5555"))
                f.pack(fill="x", pady=3, padx=5)

                urgencia = "🔴" if r['quantidade'] == 0 else "🟡"
                txt = f"{urgencia}  {r['nome']}  |  Estoque: {r['quantidade']}  |  Mínimo: {r['estoque_minimo']}  |  Repor: {r['qtd_repor']}un"
                if r.get('categoria'):
                    txt += f"  |  {r['categoria']}"
                ctk.CTkLabel(f, text=txt, font=("Aileron", 11), anchor="w",
                            text_color=("#333", "white")).pack(padx=15, pady=10, fill="x")

        # Botão exportar para texto (WhatsApp)
        def _exportar_whatsapp():
            if not itens_repor:
                messagebox.showinfo("Info", "Nenhum produto para repor.", parent=window)
                return
            texto = EstoqueService.texto_reposicao_whatsapp(itens_repor)
            # Copiar para clipboard
            window.clipboard_clear()
            window.clipboard_append(texto)
            messagebox.showinfo("Copiado!", "Lista copiada para a área de transferência!\nCole no WhatsApp para enviar ao fornecedor.", parent=window)

        btn_frame = ctk.CTkFrame(window, fg_color="transparent")
        btn_frame.pack(fill="x", padx=15, pady=10)
        ctk.CTkButton(btn_frame, text="📈 Sugestão Compra (30/60/90d)",
                      command=lambda: self.open_sugestao_compras_dialog(parent=window),
                      height=40, fg_color="#0A4174", hover_color="#062F56", text_color="white",
                      font=("Aileron", 12, "bold")).pack(side="left", fill="x", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="📋 Copiar para WhatsApp", command=_exportar_whatsapp,
                      height=40, fg_color="#25D366", hover_color="#1DA851", text_color="white",
                      font=("Aileron", 12, "bold")).pack(side="left", fill="x", expand=True, padx=5)
        ctk.CTkButton(btn_frame, text="🔄 Atualizar", command=lambda: (window.destroy(), self.open_lista_reposicao()),
                      height=40, fg_color="#7BBDE8", hover_color="#49769F", text_color="#001D39",
                      font=("Aileron", 12, "bold")).pack(side="right", fill="x", expand=True, padx=5)

    # ==================== ABERTURA / FECHAMENTO DE CAIXA ====================

    def open_caixa_dialog(self):
        """Dialog para abertura e fechamento de caixa com histórico e sangrias."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Controle de Caixa")
        dialog.geometry("750x580")
        dialog.transient(self)
        dialog.grab_set()

        tabview = ctk.CTkTabview(dialog, fg_color="transparent")
        tabview.pack(padx=10, pady=10, fill="both", expand=True)

        tab_caixa = tabview.add("Caixa")
        tab_sangrias = tabview.add("Sangrias / Suprimentos")
        tab_historico = tabview.add("Histórico")

        # --- CONTEXTO OPERADOR ---
        from services.caixa_service import CaixaService

        caixa_svc = CaixaService(self.db, operador=self.current_user)
        user_id = self.current_user.get("id") if self.current_user else 1
        loja_id = self.current_user.get("loja_id") if self.current_user else 1
        nome_operador = self.current_user.get("nome") if self.current_user else "Operador"

        # --- TAB CAIXA ---
        caixa_aberto = caixa_svc.caixa_aberto(user_id)
        resumo_caixa = caixa_svc.resumo_caixa(usuario_id=user_id) if caixa_aberto else None

        status_frame = ctk.CTkFrame(tab_caixa, fg_color=("white", "#2B2B2B"), corner_radius=15)
        status_frame.pack(fill="x", padx=10, pady=10)

        if caixa_aberto:
            ctk.CTkLabel(status_frame, text="CAIXA ABERTO", font=("Aileron", 18, "bold"),
                        text_color="#28a745").pack(pady=(15, 5))
            ctk.CTkLabel(status_frame, text=f"Operador: {nome_operador}  |  Aberto em: {caixa_aberto['data_abertura'][:16].replace('T', ' ')}",
                        font=("Aileron", 12), text_color=("#555", "#CCC")).pack()
            ctk.CTkLabel(status_frame, text=f"Valor inicial: R$ {caixa_aberto['valor_abertura']:.2f}",
                        font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(5, 15))

            # Fechar caixa (Conferência Cega - não exibe esperados antes da conferência)
            fechar_frame = ctk.CTkFrame(tab_caixa, fg_color=("white", "#2B2B2B"), corner_radius=15)
            fechar_frame.pack(fill="x", padx=10, pady=10)
            ctk.CTkLabel(fechar_frame, text="Fechar Caixa (Conferência Cega)", font=("Aileron", 14, "bold"),
                        text_color=("#001D39", "white")).pack(pady=(12, 4), padx=15, anchor="w")
            ctk.CTkLabel(fechar_frame, text="Informe os valores apurados fisicamente antes de visualizar os saldos do sistema.",
                        font=("Aileron", 10, "italic"), text_color=("#555", "#CCC")).pack(padx=15, pady=(0, 8), anchor="w")

            conf_grid = ctk.CTkFrame(fechar_frame, fg_color="transparent")
            conf_grid.pack(fill="x", padx=15, pady=2)
            for col_i in range(3):
                conf_grid.grid_columnconfigure(col_i, weight=1)

            ctk.CTkLabel(conf_grid, text="Dinheiro contado (R$):", font=("Aileron", 11, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 6))
            entry_fechamento = ctk.CTkEntry(conf_grid, placeholder_text="0,00")
            entry_fechamento.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))

            ctk.CTkLabel(conf_grid, text="PIX conferido (R$):", font=("Aileron", 11, "bold")).grid(row=0, column=1, sticky="w", padx=3)
            entry_conf_pix = ctk.CTkEntry(conf_grid, placeholder_text="0,00")
            entry_conf_pix.grid(row=1, column=1, sticky="ew", padx=3, pady=(2, 6))

            ctk.CTkLabel(conf_grid, text="Cartão conferido (R$):", font=("Aileron", 11, "bold")).grid(row=0, column=2, sticky="w", padx=(6, 0))
            entry_conf_cartao = ctk.CTkEntry(conf_grid, placeholder_text="0,00")
            entry_conf_cartao.grid(row=1, column=2, sticky="ew", padx=(6, 0), pady=(2, 6))

            ctk.CTkLabel(fechar_frame, text="Observação:", font=("Aileron", 11),
                        text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            entry_obs_fecha = ctk.CTkEntry(fechar_frame, placeholder_text="Opcional", width=400)
            entry_obs_fecha.pack(padx=15, pady=5, anchor="w")

            def _fechar_caixa():
                try:
                    valor = float(entry_fechamento.get().strip().replace(",", "."))
                except ValueError:
                    messagebox.showerror("Erro", "Informe o valor contado em dinheiro.", parent=dialog)
                    return

                pix_str = entry_conf_pix.get().strip().replace(",", ".")
                cartao_str = entry_conf_cartao.get().strip().replace(",", ".")
                try:
                    val_pix = float(pix_str) if pix_str else 0.0
                    val_cartao = float(cartao_str) if cartao_str else 0.0
                except ValueError:
                    messagebox.showerror("Erro", "Valores de PIX ou Cartão inválidos.", parent=dialog)
                    return

                obs = entry_obs_fecha.get().strip()
                ok, resultado = caixa_svc.fechar_caixa(
                    valor,
                    obs,
                    usuario_id=user_id,
                    conferido_pix=val_pix,
                    conferido_cartao=val_cartao,
                )
                if ok:
                    resumo = caixa_svc.formatar_resumo_fechamento(resultado)
                    messagebox.showinfo("Caixa Fechado — Resultado da Conferência", resumo, parent=dialog)
                    
                    # Propor a exportação em PDF do Relatório
                    if messagebox.askyesno("Exportar PDF", "Deseja salvar o relatório de fechamento em PDF?", parent=dialog):
                        try:
                            filepath = filedialog.asksaveasfilename(
                                parent=dialog,
                                title="Salvar Relatório de Fechamento",
                                defaultextension=".pdf",
                                filetypes=[("Arquivos PDF", "*.pdf")],
                                initialfile=f"fechamento_caixa_{resultado['id']}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                            )
                            if filepath:
                                from services.relatorio_caixa import gerar_pdf_fechamento
                                resultado['usuario_id'] = user_id
                                resultado['loja_id'] = loja_id
                                resultado['total_fiado'] = caixa_svc.obter_total_fiado_caixa(resultado['id'])
                                gerar_pdf_fechamento(resultado, filepath)
                                messagebox.showinfo("Sucesso", f"Relatório salvo com sucesso em:\n{filepath}", parent=dialog)
                                # Abrir PDF automaticamente no Windows
                                try:
                                    if os.name == 'nt':
                                        os.startfile(filepath)
                                except Exception:
                                    pass
                        except Exception as pdf_err:
                            messagebox.showerror("Erro PDF", f"Erro ao gerar relatório PDF: {pdf_err}", parent=dialog)

                    # Oferecer envio do resumo de fechamento de caixa via WhatsApp ao gestor
                    if messagebox.askyesno("Enviar Resumo via WhatsApp", "Deseja enviar o resumo do fechamento de caixa por WhatsApp?", parent=dialog):
                        try:
                            config_nota = self.db.get_config_nota()
                            tel_gestor = (config_nota.get("empresa_telefone") or "").strip()
                            if not tel_gestor:
                                from tkinter import simpledialog
                                tel_gestor = simpledialog.askstring(
                                    "WhatsApp do Gestor",
                                    "Informe o número de WhatsApp com DDD (ex: 11999999999):",
                                    parent=dialog
                                ) or ""
                            if tel_gestor.strip():
                                msg_wpp = f"📊 *FECHAMENTO DE CAIXA #{resultado['id']}*\n" + resumo
                                self._enviar_whatsapp_resiliente(
                                    tel_gestor,
                                    msg_wpp,
                                    metadata={"origem": "fechamento_caixa", "caixa_id": resultado.get("id")},
                                )
                        except Exception as wpp_err:
                            logger.warning("Erro ao enviar resumo de caixa via WhatsApp: %s", wpp_err)
                    
                    dialog.grab_release()
                    dialog.destroy()
                    self.after(200, self.open_caixa_dialog)
                else:
                    messagebox.showerror("Erro", resultado, parent=dialog)

            ctk.CTkButton(fechar_frame, text="FECHAR CAIXA", command=_fechar_caixa, height=40,
                         fg_color="#FF6B6B", hover_color="#FF5252", text_color="white",
                         font=("Aileron", 13, "bold")).pack(padx=15, pady=15, fill="x")
        else:
            ctk.CTkLabel(status_frame, text="CAIXA FECHADO", font=("Aileron", 18, "bold"),
                        text_color="#FF6B6B").pack(pady=(15, 5))
            ctk.CTkLabel(status_frame, text=f"Operador: {nome_operador}  |  Abra o caixa para iniciar as operações.",
                        font=("Aileron", 12), text_color=("#555", "#CCC")).pack(pady=(0, 15))

            abrir_frame = ctk.CTkFrame(tab_caixa, fg_color=("white", "#2B2B2B"), corner_radius=15)
            abrir_frame.pack(fill="x", padx=10, pady=10)
            ctk.CTkLabel(abrir_frame, text="Abrir Caixa", font=("Aileron", 14, "bold"),
                        text_color=("#001D39", "white")).pack(pady=(15, 5), padx=15, anchor="w")
            ctk.CTkLabel(abrir_frame, text="Valor inicial (troco):", font=("Aileron", 11),
                        text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            entry_abertura = ctk.CTkEntry(abrir_frame, placeholder_text="R$ 0,00", width=200)
            entry_abertura.pack(padx=15, pady=5, anchor="w")
            ctk.CTkLabel(abrir_frame, text="Observação:", font=("Aileron", 11),
                        text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            entry_obs_abre = ctk.CTkEntry(abrir_frame, placeholder_text="Opcional", width=400)
            entry_obs_abre.pack(padx=15, pady=5, anchor="w")

            def _abrir_caixa():
                try:
                    valor = float(entry_abertura.get().replace(",", "."))
                except ValueError:
                    valor = 0.0
                obs = entry_obs_abre.get().strip()
                ok, msg = caixa_svc.abrir_caixa(valor, obs, usuario_id=user_id, loja_id=loja_id)
                if ok:
                    messagebox.showinfo("Sucesso", msg, parent=dialog)
                    dialog.grab_release()
                    dialog.destroy()
                    self.after(200, self.open_caixa_dialog)
                else:
                    messagebox.showerror("Erro", msg, parent=dialog)

            ctk.CTkButton(abrir_frame, text="ABRIR CAIXA", command=_abrir_caixa, height=40,
                         fg_color="#28a745", hover_color="#218838", text_color="white",
                         font=("Aileron", 13, "bold")).pack(padx=15, pady=15, fill="x")

        # --- TAB SANGRIAS / SUPRIMENTOS ---
        if not caixa_aberto:
            ctk.CTkLabel(tab_sangrias, text="Abra o caixa para gerenciar sangrias e suprimentos.",
                         font=("Aileron", 14), text_color=("#555", "#CCC")).pack(pady=50)
        else:
            sangria_main_frame = ctk.CTkFrame(tab_sangrias, fg_color="transparent")
            sangria_main_frame.pack(fill="both", expand=True)

            left_sangria = ctk.CTkFrame(sangria_main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
            left_sangria.pack(side="left", fill="both", expand=True, padx=5, pady=5)

            right_sangria = ctk.CTkFrame(sangria_main_frame, fg_color=("white", "#2B2B2B"), corner_radius=15)
            right_sangria.pack(side="right", fill="both", expand=True, padx=5, pady=5)

            # Left: Registrar Sangria ou Suprimento
            ctk.CTkLabel(left_sangria, text="Movimentação de Gaveta", font=("Aileron", 14, "bold"),
                         text_color=("#001D39", "white")).pack(pady=8, padx=15, anchor="w")

            ctk.CTkLabel(left_sangria, text="Tipo de Operação:", font=("Aileron", 11),
                         text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            combo_tipo_mov = ctk.CTkComboBox(left_sangria, values=["Sangria (Retirada)", "Suprimento (Reforço de Troco)"], width=220, state="readonly")
            combo_tipo_mov.pack(padx=15, pady=4, anchor="w")
            combo_tipo_mov.set("Sangria (Retirada)")

            ctk.CTkLabel(left_sangria, text="Valor (R$):", font=("Aileron", 11),
                         text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            entry_s_valor = ctk.CTkEntry(left_sangria, placeholder_text="0.00", width=220)
            entry_s_valor.pack(padx=15, pady=4, anchor="w")

            ctk.CTkLabel(left_sangria, text="Motivo:", font=("Aileron", 11),
                         text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            combo_s_motivo = ctk.CTkComboBox(left_sangria, values=["Depósito em Cofre", "Pagamento Fornecedor", "Troco Adicional", "Reforço de Caixa", "Outro"], width=220, state="readonly")
            combo_s_motivo.pack(padx=15, pady=4, anchor="w")
            combo_s_motivo.set("Depósito em Cofre")

            ctk.CTkLabel(left_sangria, text="Observação:", font=("Aileron", 11),
                         text_color=("#001D39", "white")).pack(padx=15, anchor="w")
            entry_s_obs = ctk.CTkEntry(left_sangria, placeholder_text="Opcional", width=220)
            entry_s_obs.pack(padx=15, pady=4, anchor="w")

            # Right: Lista de Sangrias e Suprimentos
            ctk.CTkLabel(right_sangria, text="Movimentações no Turno", font=("Aileron", 14, "bold"),
                         text_color=("#001D39", "white")).pack(pady=10, padx=15, anchor="w")

            list_frame = ctk.CTkScrollableFrame(right_sangria, fg_color="transparent")
            list_frame.pack(fill="both", expand=True, padx=5, pady=5)

            def _atualizar_lista_sangrias():
                for widget in list_frame.winfo_children():
                    widget.destroy()
                movs = caixa_svc.listar_movimentacoes_turno(caixa_aberto['id'])
                sangrias = movs["sangrias"]
                suprimentos = movs["suprimentos"]
                if not sangrias and not suprimentos:
                    ctk.CTkLabel(list_frame, text="Nenhuma sangria ou suprimento no turno.", font=("Aileron", 11), text_color=("#555", "#CCC")).pack(pady=15)
                else:
                    for sup_row in suprimentos:
                        s_card = ctk.CTkFrame(list_frame, fg_color=("#E8F5E9", "#1E3A2B"), corner_radius=8)
                        s_card.pack(fill="x", pady=2, padx=2)
                        lbl_info = f"[SUPRIMENTO] +R$ {sup_row['valor']:.2f} - {sup_row['motivo']}\nObs: {sup_row['observacao'] or ''}"
                        ctk.CTkLabel(s_card, text=lbl_info, font=("Aileron", 11), justify="left", anchor="w").pack(padx=8, pady=4, fill="x")
                    for s_row in sangrias:
                        s_card = ctk.CTkFrame(list_frame, fg_color=("gray95", "#3A3A3A"), corner_radius=8)
                        s_card.pack(fill="x", pady=2, padx=2)
                        lbl_info = f"[SANGRIA] -R$ {s_row['valor']:.2f} - {s_row['motivo']}\nObs: {s_row['observacao'] or ''}"
                        ctk.CTkLabel(s_card, text=lbl_info, font=("Aileron", 11), justify="left", anchor="w").pack(padx=8, pady=4, fill="x")

            def _salvar_sangria():
                try:
                    val = float(entry_s_valor.get().replace(",", "."))
                    if val <= 0: raise ValueError
                except ValueError:
                    messagebox.showerror("Erro", "Valor inválido.", parent=dialog)
                    return
                motivo = combo_s_motivo.get()
                obs = entry_s_obs.get().strip()
                tipo_op = combo_tipo_mov.get()

                if "Suprimento" in tipo_op:
                    ok, msg = caixa_svc.registrar_suprimento(
                        val, motivo, caixa_id=caixa_aberto['id'], usuario_id=user_id, observacao=obs
                    )
                    titulo_ok = "Suprimento registrado!"
                else:
                    from views.supervisor_dialog import solicitar_autorizacao_supervisor

                    ok_sup, sup_info = solicitar_autorizacao_supervisor(
                        self,
                        acao=f"Sangria de R$ {val:.2f} ({motivo})",
                        exigir_motivo=False,
                        parent=dialog,
                    )
                    if not ok_sup:
                        return
                    try:
                        ok, msg = caixa_svc.registrar_sangria(
                            val,
                            motivo,
                            autorizacao=sup_info,
                            caixa_id=caixa_aberto['id'],
                            usuario_id=user_id,
                            observacao=obs,
                        )
                    except PermissionError as perm_err:
                        messagebox.showerror("Acesso Negado", str(perm_err), parent=dialog)
                        return
                    titulo_ok = "Sangria registrada!"

                if ok:
                    messagebox.showinfo("Sucesso", titulo_ok, parent=dialog)
                    entry_s_valor.delete(0, 'end')
                    entry_s_obs.delete(0, 'end')
                    _atualizar_lista_sangrias()
                    dialog.destroy()
                    self.after(200, self.open_caixa_dialog)
                else:
                    messagebox.showerror("Erro", msg, parent=dialog)

            def _abrir_gaveta_manual():
                from services.impressao_service import ImpressaoService
                from views.supervisor_dialog import solicitar_autorizacao_supervisor

                ok_sup, sup_info = solicitar_autorizacao_supervisor(
                    self,
                    acao="Abertura manual da gaveta de dinheiro",
                    exigir_motivo=False,
                    parent=dialog,
                )
                if not ok_sup:
                    return
                try:
                    imp_gav = ImpressaoService(
                        self.db,
                        printer_factory=get_printer,
                        operador=self.current_user,
                    )
                    imp_gav.abrir_gaveta(autorizacao=sup_info, caixa_id=caixa_aberto['id'])
                    messagebox.showinfo("Gaveta", "Pulso de abertura enviado para a gaveta.", parent=dialog)
                except PermissionError as perm_err:
                    messagebox.showerror("Acesso Negado", str(perm_err), parent=dialog)
                except Exception as exc:
                    messagebox.showwarning("Gaveta", f"Não foi possível abrir a gaveta: {exc}", parent=dialog)

            ctk.CTkButton(left_sangria, text="CONFIRMAR MOVIMENTAÇÃO", command=_salvar_sangria, height=35,
                          fg_color="#FF9800", hover_color="#E68900", text_color="white",
                          font=("Aileron", 12, "bold")).pack(pady=(12, 6), padx=15, fill="x")
            ctk.CTkButton(left_sangria, text="🗄️ Abrir Gaveta (Supervisor)", command=_abrir_gaveta_manual, height=32,
                          fg_color="#455A64", hover_color="#263238", text_color="white",
                          font=("Aileron", 11, "bold")).pack(pady=(0, 12), padx=15, fill="x")

            _atualizar_lista_sangrias()

        # --- TAB HISTÓRICO ---
        hist_rows = caixa_svc.historico_caixa(60)
        if not hist_rows:
            ctk.CTkLabel(tab_historico, text="Nenhum registro de caixa encontrado.",
                        font=("Aileron", 12), text_color=("#555", "#CCC")).pack(pady=30)
        else:
            hist_scroll = ctk.CTkScrollableFrame(tab_historico, fg_color="transparent")
            hist_scroll.pack(fill="both", expand=True, padx=5, pady=5)
            for row in hist_rows:
                card = ctk.CTkFrame(hist_scroll, fg_color=("white", "#2B2B2B"), corner_radius=10)
                card.pack(fill="x", padx=5, pady=3)
                data_ab = str(row.get('data_abertura', ''))[:16].replace('T', ' ')
                data_fe = str(row.get('data_fechamento', ''))[:16].replace('T', ' ')
                dif = row.get('diferenca', 0) or 0
                cor_dif = "#28a745" if dif >= 0 else "#FF6B6B"
                ctk.CTkLabel(card, text=f"{data_ab}  →  {data_fe}",
                            font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).pack(anchor="w", padx=10, pady=(5, 0))
                ctk.CTkLabel(card, text=f"Vendas: R$ {row.get('total_vendas', 0):.2f} | Dinheiro: R$ {row.get('total_dinheiro', 0):.2f} | PIX: R$ {row.get('total_pix', 0):.2f} | Cartão: R$ {row.get('total_cartao', 0):.2f}",
                            font=("Aileron", 10), text_color=("#555", "#CCC")).pack(anchor="w", padx=10)
                ctk.CTkLabel(card, text=f"Diferença: R$ {dif:.2f}",
                            font=("Aileron", 11, "bold"), text_color=cor_dif).pack(anchor="w", padx=10, pady=(0, 5))

    # ==================== VENDEDORES / FUNCIONÁRIOS ====================

    def open_vendedores_dialog(self):
        """Dialog para gerenciar vendedores/funcionários com desempenho."""
        window = ctk.CTkToplevel(self)
        window.title("Vendedores / Funcionários")
        window.geometry("850x600")
        window.attributes("-topmost", True)

        tabview = ctk.CTkTabview(window, fg_color="transparent")
        tabview.pack(fill="both", expand=True, padx=10, pady=10)

        tab_lista = tabview.add("Equipe")
        tab_desempenho = tabview.add("Desempenho")

        # ===== ABA EQUIPE =====
        add_frame = ctk.CTkFrame(tab_lista, fg_color=("white", "#2B2B2B"), corner_radius=15)
        add_frame.pack(fill="x", padx=10, pady=(5, 10))
        ctk.CTkLabel(add_frame, text="Adicionar Vendedor", font=("Aileron", 13, "bold"),
                    text_color=("#001D39", "white")).pack(anchor="w", padx=15, pady=(10, 5))

        fields_f = ctk.CTkFrame(add_frame, fg_color="transparent")
        fields_f.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(fields_f, text="Nome:", font=("Aileron", 11)).pack(side="left", padx=(0, 5))
        e_nome = ctk.CTkEntry(fields_f, width=150, placeholder_text="Nome do vendedor")
        e_nome.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(fields_f, text="Entrada:", font=("Aileron", 11)).pack(side="left", padx=(0, 3))
        e_h_ini = ctk.CTkEntry(fields_f, width=60, placeholder_text="08:00")
        e_h_ini.insert(0, "08:00")
        e_h_ini.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(fields_f, text="Saída:", font=("Aileron", 11)).pack(side="left", padx=(0, 3))
        e_h_fim = ctk.CTkEntry(fields_f, width=60, placeholder_text="18:00")
        e_h_fim.insert(0, "18:00")
        e_h_fim.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(fields_f, text="Comissão %:", font=("Aileron", 11)).pack(side="left", padx=(0, 3))
        e_comissao = ctk.CTkEntry(fields_f, width=50, placeholder_text="0")
        e_comissao.insert(0, "0")
        e_comissao.pack(side="left", padx=(0, 10))

        list_frame = ctk.CTkFrame(tab_lista, fg_color="transparent")
        list_frame.pack(fill="both", expand=True, padx=10)

        from tkinter import ttk as ttk_local
        cols = ("id", "nome", "horario", "comissao")
        tree_v = ttk_local.Treeview(list_frame, columns=cols, show="headings", height=12)
        for c, h, w in [("id", "ID", 40), ("nome", "Nome", 200), ("horario", "Horário", 150), ("comissao", "Comissão %", 80)]:
            tree_v.heading(c, text=h)
            tree_v.column(c, width=w, anchor="center" if c != "nome" else "w")
        tree_v.pack(side="left", fill="both", expand=True)
        ttk_local.Scrollbar(list_frame, orient="vertical", command=tree_v.yview).pack(side="right", fill="y")

        # Indicador de turno atual
        turno_atual = self.db.vendedor_no_turno()
        if turno_atual:
            ctk.CTkLabel(tab_lista, text=f"\U0001f7e2 No turno agora: {turno_atual}",
                        font=("Aileron", 12, "bold"), text_color="#28a745").pack(pady=(5, 0))

        def _refresh():
            for i in tree_v.get_children():
                tree_v.delete(i)
            df = self.db.listar_vendedores()
            for _, r in df.iterrows():
                tree_v.insert("", "end", values=(
                    r['id'], r['nome'],
                    f"{r['horario_inicio']} - {r['horario_fim']}",
                    f"{r.get('comissao', 0):.1f}%"
                ))
        _refresh()

        def _add():
            nome = e_nome.get().strip()
            if not nome:
                messagebox.showerror("Erro", "Informe o nome.", parent=window)
                return
            h_ini = e_h_ini.get().strip() or "08:00"
            h_fim = e_h_fim.get().strip() or "18:00"
            try:
                com = float(e_comissao.get().replace(",", ".")) if e_comissao.get().strip() else 0
            except ValueError:
                com = 0
            self.db.adicionar_vendedor(nome, h_ini, h_fim, com)
            e_nome.delete(0, "end")
            _refresh()

        def _remove():
            sel = tree_v.selection()
            if not sel:
                return
            vid = tree_v.item(sel[0])['values'][0]
            if messagebox.askyesno("Confirmar", "Desativar este vendedor?", parent=window):
                self.db.remover_vendedor(vid)
                _refresh()

        btn_f = ctk.CTkFrame(tab_lista, fg_color="transparent")
        btn_f.pack(fill="x", padx=10, pady=5)
        ctk.CTkButton(add_frame, text="Adicionar", command=_add, height=32,
                     fg_color="#0A4174", hover_color="#062F56").pack(padx=15, pady=(0, 10), fill="x")
        ctk.CTkButton(btn_f, text="Desativar Selecionado", command=_remove, height=30,
                     fg_color="#FF6B6B", hover_color="#FF5252", text_color="white").pack(side="right")

        # ===== ABA DESEMPENHO =====
        ctk.CTkLabel(tab_desempenho, text="Desempenho dos Vendedores (30 dias)",
                    font=("Aileron", 14, "bold"), text_color=("#001D39", "white")).pack(pady=(10, 5))

        df_perf = self.db.vendas_por_vendedor(30)
        if df_perf.empty:
            ctk.CTkLabel(tab_desempenho, text="Nenhuma venda com vendedor identificado nos últimos 30 dias.",
                        font=("Aileron", 12), text_color=("#555", "#CCC")).pack(pady=20)
        else:
            perf_scroll = ctk.CTkScrollableFrame(tab_desempenho, fg_color="transparent")
            perf_scroll.pack(fill="both", expand=True, padx=10, pady=5)

            # Calcular comissões
            df_vendedores = self.db.listar_vendedores()
            comissao_map = {}
            if not df_vendedores.empty:
                comissao_map = dict(zip(df_vendedores['nome'], df_vendedores['comissao']))

            for _, r in df_perf.iterrows():
                card = ctk.CTkFrame(perf_scroll, fg_color=("white", "#2B2B2B"), corner_radius=10)
                card.pack(fill="x", padx=5, pady=3)
                nome_v = r['vendedor']
                vendas_n = int(r['num_vendas'])
                total_v = r['total_vendido'] or 0
                lucro_v = r['total_lucro'] or 0
                pct_comissao = comissao_map.get(nome_v, 0) or 0
                valor_comissao = total_v * (pct_comissao / 100)

                ctk.CTkLabel(card, text=nome_v, font=("Aileron", 13, "bold"),
                            text_color=("#001D39", "white")).pack(anchor="w", padx=10, pady=(5, 0))
                info = f"{vendas_n} vendas | Faturamento: R$ {total_v:.2f} | Lucro: R$ {lucro_v:.2f}"
                if pct_comissao > 0:
                    info += f" | Comissão ({pct_comissao:.0f}%): R$ {valor_comissao:.2f}"
                ctk.CTkLabel(card, text=info, font=("Aileron", 10),
                            text_color=("#555", "#CCC")).pack(anchor="w", padx=10, pady=(0, 5))

    # ==================== NOTA DE SERVIÇO ====================

    def open_servico_dialog(self):
        """Dialog para registrar serviço e gerar nota (custo visível só pro vendedor)."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Nota de Serviço")
        dialog.geometry("800x600")
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        tabview = ctk.CTkTabview(dialog, fg_color="transparent")
        tabview.pack(fill="both", expand=True, padx=10, pady=10)

        tab_novo = tabview.add("Novo Serviço")
        tab_hist = tabview.add("Histórico")

        # ===== ABA 1: NOVO SERVIÇO =====
        main_f = ctk.CTkScrollableFrame(tab_novo, fg_color="transparent")
        main_f.pack(fill="both", expand=True)

        ctk.CTkLabel(main_f, text="Registrar Serviço", font=("Aileron", 18, "bold"),
                    text_color=("#001D39", "white")).pack(pady=(5, 10))

        card = ctk.CTkFrame(main_f, fg_color=("white", "#2B2B2B"), corner_radius=15)
        card.pack(fill="x", padx=10, pady=5)

        # Cliente
        ctk.CTkLabel(card, text="Cliente:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        df_clientes = self.db.listar_clientes()
        clientes_opt = ["Consumidor Final"] + (df_clientes['nome'].tolist() if not df_clientes.empty else [])
        combo_cliente = ctk.CTkComboBox(card, values=clientes_opt)
        combo_cliente.pack(fill="x", padx=15, pady=3)

        ctk.CTkLabel(card, text="Contato (WhatsApp):", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_contato = ctk.CTkEntry(card, placeholder_text="(00) 00000-0000")
        e_contato.pack(fill="x", padx=15, pady=3)

        def _on_cliente_change(val):
            if val != "Consumidor Final" and not df_clientes.empty:
                row = df_clientes[df_clientes['nome'] == val]
                if not row.empty:
                    contato = row.iloc[0].get('contato', '') or ''
                    e_contato.delete(0, "end")
                    e_contato.insert(0, contato)
        combo_cliente.configure(command=_on_cliente_change)

        # Descrição do serviço
        ctk.CTkLabel(card, text="Serviço Realizado:", font=("Aileron", 12, "bold"),
                    text_color="#0A4174").pack(anchor="w", padx=15, pady=(10, 0))
        e_descricao = ctk.CTkTextbox(card, height=80, font=("Aileron", 11),
                                     fg_color=("#F5F5F5", "#333333"), corner_radius=8)
        e_descricao.pack(fill="x", padx=15, pady=3)

        # Valor cobrado
        ctk.CTkLabel(card, text="Valor Cobrado (R$):", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        e_valor = ctk.CTkEntry(card, placeholder_text="Ex: 150.00")
        e_valor.pack(fill="x", padx=15, pady=3)

        # Custo (peças, material) — VISÍVEL SOMENTE NO SISTEMA
        custo_frame = ctk.CTkFrame(card, fg_color=("#FFF8E1", "#3A3520"), corner_radius=10,
                                   border_width=1, border_color=("#FFD54F", "#8D6E00"))
        custo_frame.pack(fill="x", padx=15, pady=(10, 0))
        ctk.CTkLabel(custo_frame, text="🔒 Custo Interno (não aparece na nota do cliente)",
                    font=("Aileron", 10, "italic"), text_color=("#8D6E00", "#FFD54F")).pack(anchor="w", padx=10, pady=(5, 0))
        ctk.CTkLabel(custo_frame, text="Custo (peças, material, etc.):", font=("Aileron", 11, "bold")).pack(anchor="w", padx=10, pady=(3, 0))
        e_custo = ctk.CTkEntry(custo_frame, placeholder_text="Ex: 45.00 (opcional)")
        e_custo.pack(fill="x", padx=10, pady=(3, 8))

        # Forma de pagamento
        ctk.CTkLabel(card, text="Forma de Pagamento:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(10, 0))
        formas = ["Dinheiro", "PIX", "Cartão Débito", "Cartão Crédito", "Transferência", "Fiado"]
        combo_forma = ctk.CTkComboBox(card, values=formas, state="readonly")
        combo_forma.set("Dinheiro")
        combo_forma.pack(fill="x", padx=15, pady=3)

        # Observação
        ctk.CTkLabel(card, text="Observação:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_obs = ctk.CTkEntry(card, placeholder_text="(opcional)")
        e_obs.pack(fill="x", padx=15, pady=3)

        # Equipamento / Item (rastreabilidade)
        os_cfg = self._get_os_config()
        ctk.CTkLabel(card, text=os_cfg["equip_label"], font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        e_equip = ctk.CTkEntry(card, placeholder_text=os_cfg["equip_placeholder"])
        e_equip.pack(fill="x", padx=15, pady=3)

        # Status OS
        status_os_options = os_cfg["statuses"]
        ctk.CTkLabel(card, text="Status da OS:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        combo_status_os = ctk.CTkComboBox(card, values=status_os_options, state="readonly")
        combo_status_os.set("Concluído")
        combo_status_os.pack(fill="x", padx=15, pady=3)

        # Vendedor
        ctk.CTkLabel(card, text="Vendedor/Atendente:", font=("Aileron", 11, "bold")).pack(anchor="w", padx=15, pady=(5, 0))
        df_vend_serv = self.db.listar_vendedores()
        vend_opts_serv = [""] + (df_vend_serv['nome'].tolist() if not df_vend_serv.empty else [])
        combo_vendedor_serv = ctk.CTkComboBox(card, values=vend_opts_serv)
        combo_vendedor_serv.set(self.db.vendedor_no_turno())
        combo_vendedor_serv.pack(fill="x", padx=15, pady=3)

        # Lucro estimado em tempo real
        lbl_lucro = ctk.CTkLabel(card, text="", font=("Aileron", 11, "bold"), text_color="#28a745")
        lbl_lucro.pack(anchor="w", padx=15, pady=(5, 0))

        def _atualizar_lucro(*_):
            try:
                valor = float(e_valor.get().replace(",", ".")) if e_valor.get().strip() else 0
                custo = float(e_custo.get().replace(",", ".")) if e_custo.get().strip() else 0
                lucro = valor - custo
                cor = "#28a745" if lucro >= 0 else "#FF6B6B"
                lbl_lucro.configure(text=f"Lucro estimado: R$ {lucro:.2f}", text_color=cor)
            except ValueError:
                lbl_lucro.configure(text="")
        e_valor.bind("<KeyRelease>", _atualizar_lucro)
        e_custo.bind("<KeyRelease>", _atualizar_lucro)

        def _registrar_servico():
            from services.servico_service import ServicoService
            svc = ServicoService(self.db, invoice_gen=invoice_generator)
            try:
                os_data = svc.registrar_os(
                    cliente=combo_cliente.get(),
                    descricao=e_descricao.get("1.0", "end-1c"),
                    valor=e_valor.get(),
                    custo=e_custo.get(),
                    forma_pagamento=combo_forma.get(),
                    observacao=e_obs.get(),
                    vendedor=combo_vendedor_serv.get(),
                    status_os=combo_status_os.get(),
                    contato_cliente=e_contato.get(),
                    equipamento=e_equip.get(),
                )
            except (ValueError, RuntimeError) as err:
                messagebox.showerror("Erro", str(err), parent=dialog)
                return

            servico_id = os_data["servico_id"]
            valor = os_data["valor"]
            contato = os_data["contato_cliente"]

            # Gerar nota de serviço via ServicoService (SEM custo — custo é interno)
            try:
                nota = svc.gerar_nota_os(
                    servico_id=servico_id,
                    descricao=os_data["descricao"],
                    valor=valor,
                    cliente=os_data["cliente"],
                    contato=contato,
                    forma_pagamento=os_data["forma_pagamento"],
                    observacao=os_data["observacao"],
                    abrir_arquivo=False,
                )
                file_path = nota["file_path"]
                text_content = nota["text_content"]
                wpp_content = nota["wpp_content"]

                if file_path and text_content:
                    resp = messagebox.askyesno("Imprimir Nota",
                        f"Serviço #{servico_id} registrado!\nValor: R$ {valor:.2f}\n\nDeseja imprimir a nota?",
                        parent=dialog)
                    if resp:
                        try:
                            get_printer().print_text(text_content, logo_path=nota["logo_path"])
                            messagebox.showinfo("Sucesso", "Nota enviada para impressão!", parent=dialog)
                        except Exception as e:
                            logger.warning("Erro ao imprimir nota de serviço: %s", e)
                            messagebox.showwarning("Aviso",
                                f"Não foi possível imprimir.\nNota salva em:\n{file_path}", parent=dialog)
                            try:
                                if os.name == 'nt':
                                    os.startfile(file_path)
                            except Exception:
                                pass

                    # WhatsApp
                    if contato:
                        enviar_wpp = messagebox.askyesno("Enviar por WhatsApp",
                            f"Enviar nota por WhatsApp para {contato}?", parent=dialog)
                        if enviar_wpp:
                            try:
                                msg_wpp = svc.montar_mensagem_whatsapp_os(
                                    wpp_content=wpp_content,
                                    text_content=text_content,
                                    servico_id=servico_id,
                                    descricao=os_data["descricao"],
                                    valor=valor,
                                    cliente=os_data["cliente"],
                                )
                                sucesso, info_env = self._enviar_whatsapp_resiliente(
                                    contato,
                                    msg_wpp,
                                    metadata={"origem": "recibo_servico", "servico_id": servico_id},
                                )
                                if sucesso:
                                    messagebox.showinfo("WhatsApp (Outbox)",
                                        f"{info_env}\nO envio ocorrerá sem travar o caixa.", parent=dialog)
                                else:
                                    messagebox.showwarning("WhatsApp",
                                        f"Não foi possível enfileirar: {info_env}", parent=dialog)
                            except Exception as e:
                                logger.warning("Erro WhatsApp serviço: %s", e)
                else:
                    messagebox.showinfo("Sucesso", f"Serviço #{servico_id} registrado com sucesso!", parent=dialog)
            except Exception as e:
                logger.warning("Erro ao gerar nota de serviço: %s", e)
                messagebox.showinfo("Sucesso", f"Serviço #{servico_id} registrado!\n(Nota não gerada)", parent=dialog)

            # Limpar campos
            e_descricao.delete("1.0", "end")
            e_valor.delete(0, "end")
            e_custo.delete(0, "end")
            e_obs.delete(0, "end")
            e_equip.delete(0, "end")
            combo_status_os.set("Concluído")
            lbl_lucro.configure(text="")
            _refresh_hist()
            self.refresh_dashboard()

        ctk.CTkButton(card, text="REGISTRAR SERVIÇO E GERAR NOTA", command=_registrar_servico,
                      height=45, fg_color="#0A4174", hover_color="#062F56",
                      font=("Aileron", 14, "bold")).pack(fill="x", padx=15, pady=15)

        # ===== ABA 2: HISTÓRICO =====
        hist_filter_frame = ctk.CTkFrame(tab_hist, fg_color="transparent")
        hist_filter_frame.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(hist_filter_frame, text="🔍", font=ctk.CTkFont(size=14)).pack(side="left", padx=(0, 5))

        hist_search_var = ctk.StringVar()
        hist_search = ctk.CTkEntry(hist_filter_frame, placeholder_text="Buscar por ID, cliente...",
                                   textvariable=hist_search_var, height=34, width=180, corner_radius=20)
        hist_search.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(hist_filter_frame, text="De:", font=ctk.CTkFont(family="Aileron", size=11),
                     text_color=("#555", "#AAA")).pack(side="left", padx=(5, 2))
        hist_date_from = ctk.CTkEntry(hist_filter_frame, placeholder_text="DD/MM/AAAA",
                                      height=34, width=110, corner_radius=10)
        hist_date_from.pack(side="left", padx=(0, 5))

        ctk.CTkLabel(hist_filter_frame, text="Até:", font=ctk.CTkFont(family="Aileron", size=11),
                     text_color=("#555", "#AAA")).pack(side="left", padx=(5, 2))
        hist_date_to = ctk.CTkEntry(hist_filter_frame, placeholder_text="DD/MM/AAAA",
                                    height=34, width=110, corner_radius=10)
        hist_date_to.pack(side="left", padx=(0, 5))

        hist_status_var = ctk.StringVar(value="Todos")
        hist_status_combo = ctk.CTkComboBox(hist_filter_frame, variable=hist_status_var,
                                            values=["Todos"] + os_cfg["statuses"],
                                            width=130, height=34, state="readonly",
                                            font=ctk.CTkFont(family="Aileron", size=10),
                                            command=lambda _: _refresh_hist())
        hist_status_combo.pack(side="left", padx=(5, 0))

        _hist_timer = {"id": None}

        hist_scroll = ctk.CTkScrollableFrame(tab_hist, fg_color="transparent")
        hist_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        status_colors = os_cfg["colors"]
        status_os_all = os_cfg["statuses"]

        def _parse_date(txt):
            """Converte DD/MM/AAAA ou AAAA-MM-DD para date."""
            txt = txt.strip()
            if not txt:
                return None
            try:
                if "/" in txt:
                    parts = txt.split("/")
                    return datetime.date(int(parts[2]), int(parts[1]), int(parts[0]))
                return datetime.date.fromisoformat(txt)
            except (ValueError, IndexError):
                return None

        def _refresh_hist():
            for w in hist_scroll.winfo_children():
                w.destroy()
            df_s = self.db.listar_servicos(365)
            if df_s.empty:
                ctk.CTkLabel(hist_scroll, text="Nenhum serviço registrado.", text_color="gray").pack(pady=20)
                return

            search_txt = hist_search_var.get().strip().lower()
            date_from = _parse_date(hist_date_from.get())
            date_to = _parse_date(hist_date_to.get())
            status_filter = hist_status_var.get()

            filtered = df_s.copy()

            # Filtro por busca (ID ou cliente)
            if search_txt:
                mask = filtered['id'].astype(str).str.contains(search_txt, na=False, regex=False)
                mask = mask | filtered['cliente'].astype(str).str.lower().str.contains(search_txt, na=False, regex=False)
                if 'descricao' in filtered.columns:
                    mask = mask | filtered['descricao'].astype(str).str.lower().str.contains(search_txt, na=False, regex=False)
                if 'equipamento' in filtered.columns:
                    mask = mask | filtered['equipamento'].astype(str).str.lower().str.contains(search_txt, na=False, regex=False)
                filtered = filtered[mask]

            # Filtro por data
            if date_from or date_to:
                def _to_date(val):
                    try:
                        return datetime.date.fromisoformat(str(val)[:10])
                    except (ValueError, TypeError):
                        return None
                filtered['_dt'] = filtered['data'].apply(_to_date)
                if date_from:
                    filtered = filtered[filtered['_dt'].apply(lambda d: d is not None and d >= date_from)]
                if date_to:
                    filtered = filtered[filtered['_dt'].apply(lambda d: d is not None and d <= date_to)]
                filtered = filtered.drop(columns=['_dt'])

            # Filtro por status
            if status_filter != "Todos":
                st_col = filtered.get('status_os', pd.Series(dtype=str))
                filtered = filtered[st_col == status_filter]

            if filtered.empty:
                ctk.CTkLabel(hist_scroll, text="Nenhum serviço encontrado com esses filtros.",
                             text_color="gray").pack(pady=20)
                return

            for _, r in filtered.iterrows():
                sid = r['id']
                st = r.get('status_os', r.get('status', 'Concluído')) or 'Concluído'
                equip = r.get('equipamento', '') or ''
                cor_st = status_colors.get(st, "#888")

                f = ctk.CTkFrame(hist_scroll, fg_color=("white", "#3A3A3A"), corner_radius=10,
                                border_width=1, border_color=(cor_st, cor_st))
                f.pack(fill="x", pady=3, padx=5)

                data_str = str(r['data'])[:10] if r.get('data') else ''
                custo_v = r.get('custo', 0) or 0
                valor_v = r.get('valor', 0)
                lucro_v = valor_v - custo_v

                # Primeira linha: data, cliente, descrição
                top_f = ctk.CTkFrame(f, fg_color="transparent")
                top_f.pack(fill="x", padx=10, pady=(6, 0))

                ctk.CTkLabel(top_f, text=f"🔧 #{sid}  |  {data_str}  |  {r['cliente']}",
                            font=("Aileron", 10, "bold"), text_color=("#333", "#DDD")).pack(side="left")
                # Badge de status
                ctk.CTkLabel(top_f, text=f"  {st}  ", font=("Aileron", 10, "bold"),
                            text_color="white", fg_color=cor_st, corner_radius=5).pack(side="right")

                # Segunda linha: detalhes
                desc_txt = r['descricao'][:50]
                if equip:
                    desc_txt = f"[{equip}] {desc_txt}"
                detail = f" {desc_txt}  |  R$ {valor_v:.2f}"
                if custo_v > 0:
                    detail += f"  |  Lucro: R$ {lucro_v:.2f}"
                detail += f"  |  {r.get('forma_pagamento', '')}"

                ctk.CTkLabel(f, text=detail, font=("Aileron", 10), text_color=("#555", "#CCC"),
                            anchor="w", justify="left").pack(padx=15, pady=(0, 2), fill="x")

                # Botão alterar status (se não concluído/entregue)
                if st not in ("Concluído", "Entregue"):
                    btn_f = ctk.CTkFrame(f, fg_color="transparent")
                    btn_f.pack(fill="x", padx=15, pady=(0, 6))
                    def _change_status(s_id=sid, current=st):
                        from services.servico_service import ServicoService
                        ServicoService(self.db).avancar_status(s_id, current, status_os_all)
                        _refresh_hist()
                    ctk.CTkButton(btn_f, text=f"Avançar Status →", height=24, font=("Aileron", 10),
                                  fg_color=cor_st, hover_color="#333", text_color="white",
                                  command=_change_status).pack(side="left")

        def _on_hist_search(*_a):
            if _hist_timer["id"]:
                dialog.after_cancel(_hist_timer["id"])
            _hist_timer["id"] = dialog.after(400, _refresh_hist)

        hist_search_var.trace_add("write", _on_hist_search)
        hist_date_from.bind("<KeyRelease>", _on_hist_search)
        hist_date_to.bind("<KeyRelease>", _on_hist_search)

        _refresh_hist()

    # ==================== RESTAURANTE - GESTÃO DE MESAS ====================

    def open_mesas_manager(self):
        """Gerencia mesas do restaurante com mapa visual."""
        window = ctk.CTkToplevel(self)
        window.title("Gestão de Mesas")
        window.geometry("850x650")
        window.attributes("-topmost", True)

        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)

        # Header
        header = ctk.CTkFrame(main_frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="🍽️ Mapa de Mesas", font=("Aileron", 20, "bold"),
                     text_color=("#001D39", "white")).pack(side="left")

        # Adicionar mesas
        add_f = ctk.CTkFrame(header, fg_color="transparent")
        add_f.pack(side="right")
        ctk.CTkLabel(add_f, text="Qtd:", font=("Aileron", 11)).pack(side="left", padx=3)
        e_qtd = ctk.CTkEntry(add_f, width=50, placeholder_text="5")
        e_qtd.pack(side="left", padx=3)
        ctk.CTkLabel(add_f, text="Lugares:", font=("Aileron", 11)).pack(side="left", padx=3)
        e_cap = ctk.CTkEntry(add_f, width=40, placeholder_text="4")
        e_cap.pack(side="left", padx=3)
        ctk.CTkLabel(add_f, text="Área:", font=("Aileron", 11)).pack(side="left", padx=3)
        combo_area = ctk.CTkComboBox(add_f, values=["Salão", "Varanda", "Área Externa", "VIP", "Balcão"],
                                      width=110, state="readonly")
        combo_area.set("Salão")
        combo_area.pack(side="left", padx=3)

        def _add_mesas():
            try:
                qtd = int(e_qtd.get())
            except ValueError:
                qtd = 1
            try:
                cap = int(e_cap.get())
            except ValueError:
                cap = 4
            area = combo_area.get()
            criadas = self.db.criar_mesas(qtd, cap, area)
            messagebox.showinfo("Sucesso", f"{criadas} mesa(s) criada(s).", parent=window)
            _refresh()

        ctk.CTkButton(add_f, text="+ Criar", width=80, height=30, fg_color="#E07B56",
                      hover_color="#C4623E", text_color="white", font=("Aileron", 11, "bold"),
                      command=_add_mesas).pack(side="left", padx=5)

        # Legenda
        leg = ctk.CTkFrame(main_frame, fg_color="transparent")
        leg.pack(fill="x", pady=(0, 5))
        for cor, txt in [("#28a745", "Livre"), ("#FF6B6B", "Ocupada"), ("#FFD93D", "Reservada")]:
            ctk.CTkLabel(leg, text=f"● {txt}", font=("Aileron", 10, "bold"), text_color=cor).pack(side="left", padx=10)

        # Grid de mesas
        mesas_scroll = ctk.CTkScrollableFrame(main_frame, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        mesas_scroll.pack(fill="both", expand=True)

        def _refresh():
            for w in mesas_scroll.winfo_children():
                w.destroy()
            df = self.db.listar_mesas()
            if df.empty:
                ctk.CTkLabel(mesas_scroll, text="Nenhuma mesa cadastrada.\nUse o botão '+ Criar' acima.",
                             font=("Aileron", 13), text_color="gray").pack(pady=40)
                return

            # Agrupar por área
            for area_nome in df['area'].unique():
                area_label = ctk.CTkLabel(mesas_scroll, text=f"📍 {area_nome}",
                                          font=("Aileron", 13, "bold"), text_color=("#001D39", "white"))
                area_label.pack(anchor="w", padx=10, pady=(10, 3))

                area_frame = ctk.CTkFrame(mesas_scroll, fg_color="transparent")
                area_frame.pack(fill="x", padx=10, pady=(0, 5))

                area_mesas = df[df['area'] == area_nome]
                col = 0
                for _, mesa in area_mesas.iterrows():
                    status = mesa['status']
                    cores = {"livre": "#28a745", "ocupada": "#FF6B6B", "reservada": "#FFD93D"}
                    cor = cores.get(status, "#888")
                    text_cor = "white" if status != "reservada" else "#333"

                    card = ctk.CTkFrame(area_frame, width=120, height=100, fg_color=cor,
                                        corner_radius=15)
                    card.grid(row=0, column=col, padx=6, pady=6)
                    card.grid_propagate(False)

                    inner = ctk.CTkFrame(card, fg_color="transparent")
                    inner.place(relx=0.5, rely=0.5, anchor="center")

                    ctk.CTkLabel(inner, text=f"Mesa {mesa['numero']}", font=("Aileron", 13, "bold"),
                                 text_color=text_cor).pack()
                    ctk.CTkLabel(inner, text=f"{mesa['capacidade']} lug.", font=("Aileron", 9),
                                 text_color=text_cor).pack()
                    ctk.CTkLabel(inner, text=status.upper(), font=("Aileron", 9, "bold"),
                                 text_color=text_cor).pack()

                    # Botões de ação
                    btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
                    btn_frame.pack(pady=(3, 0))

                    if status == "livre":
                        ctk.CTkButton(btn_frame, text="Abrir", width=50, height=22, font=("Aileron", 9),
                                      fg_color="white", text_color="#28a745", hover_color="#e0e0e0",
                                      command=lambda m=mesa: self._abrir_comanda_mesa(m, window)).pack(side="left", padx=1)
                    elif status == "ocupada":
                        ctk.CTkButton(btn_frame, text="Ver", width=50, height=22, font=("Aileron", 9),
                                      fg_color="white", text_color="#FF6B6B", hover_color="#e0e0e0",
                                      command=lambda m=mesa: self._ver_comanda_mesa(m, window)).pack(side="left", padx=1)

                    def _del_mesa(mid=mesa['id']):
                        if messagebox.askyesno("Confirmar", "Remover esta mesa?", parent=window):
                            self.db.remover_mesa(mid)
                            _refresh()
                    ctk.CTkButton(btn_frame, text="✕", width=22, height=22, font=("Aileron", 9),
                                  fg_color="white", text_color="red", hover_color="#e0e0e0",
                                  command=_del_mesa).pack(side="left", padx=1)

                    col += 1

        self._refresh_mesas = _refresh
        _refresh()

    def _abrir_comanda_mesa(self, mesa, parent_window=None):
        """Abre uma comanda para a mesa selecionada e abre o dialog de comanda."""
        comanda_id = self.db.abrir_comanda(mesa_id=mesa['id'], tipo_atendimento="mesa")
        if parent_window and hasattr(self, '_refresh_mesas'):
            self._refresh_mesas()
        self._open_comanda_editor(comanda_id)

    def _ver_comanda_mesa(self, mesa, parent_window=None):
        """Abre a comanda existente da mesa."""
        comanda = self.db.get_comanda_por_mesa(mesa['id'])
        if comanda:
            self._open_comanda_editor(comanda['id'])
        else:
            messagebox.showinfo("Info", "Nenhuma comanda aberta nesta mesa.", parent=parent_window or self)

    # ==================== RESTAURANTE - COMANDAS ====================

    def open_comandas_dialog(self):
        """Dialog principal de comandas — lista abertas e permite criar nova."""
        window = ctk.CTkToplevel(self)
        window.title("Comandas")
        window.geometry("900x650")
        window.attributes("-topmost", True)

        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)

        # Header + Nova comanda
        header = ctk.CTkFrame(main_frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="📋 Comandas", font=("Aileron", 20, "bold"),
                     text_color=("#001D39", "white")).pack(side="left")

        # Botões de nova comanda
        new_btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        new_btn_frame.pack(side="right")

        for tipo, icon, cor in [("Balcão", "🪑", "#6EA2B3"), ("Delivery", "🛵", "#E07B56"), ("Retirada", "📦", "#85C1E9")]:
            ctk.CTkButton(new_btn_frame, text=f"{icon} {tipo}", width=100, height=30,
                          fg_color=cor, hover_color="#49769F", text_color="white",
                          font=("Aileron", 10, "bold"),
                          command=lambda t=tipo.lower(): self._nova_comanda_sem_mesa(t, window, _refresh)).pack(side="left", padx=3)

        # Tabs
        tabview = ctk.CTkTabview(main_frame, fg_color="transparent")
        tabview.pack(fill="both", expand=True)
        tab_abertas = tabview.add("Abertas")
        tab_historico = tabview.add("Histórico")

        # === Abertas ===
        scroll_abertas = ctk.CTkScrollableFrame(tab_abertas, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        scroll_abertas.pack(fill="both", expand=True)

        def _refresh():
            for w in scroll_abertas.winfo_children():
                w.destroy()
            df = self.db.listar_comandas_abertas()
            if df.empty:
                ctk.CTkLabel(scroll_abertas, text="Nenhuma comanda aberta.", font=("Aileron", 13),
                             text_color="gray").pack(pady=30)
                return
            for _, c in df.iterrows():
                card = ctk.CTkFrame(scroll_abertas, fg_color=("white", "#2B2B2B"), corner_radius=12)
                card.pack(fill="x", padx=5, pady=4)

                info = ctk.CTkFrame(card, fg_color="transparent")
                info.pack(side="left", fill="x", expand=True, padx=12, pady=10)

                tipo_icons = {"mesa": "🍽️", "balcão": "🪑", "delivery": "🛵", "retirada": "📦"}
                tipo_icon = tipo_icons.get(c['tipo_atendimento'], "📋")
                mesa_txt = f" — Mesa {int(c['mesa_numero'])}" if pd.notna(c.get('mesa_numero')) else ""
                ctk.CTkLabel(info, text=f"{tipo_icon} Comanda #{c['id']}{mesa_txt}",
                             font=("Aileron", 13, "bold"), text_color=("#001D39", "white")).pack(anchor="w")
                hora = str(c['data_abertura'])[:16].replace('T', ' ')
                ctk.CTkLabel(info, text=f"Cliente: {c['cliente']} | Aberta: {hora} | Garçom: {c.get('garcom', '')}",
                             font=("Aileron", 10), text_color=("#555", "#CCC")).pack(anchor="w")
                ctk.CTkLabel(info, text=f"Total: R$ {c['total']:.2f}",
                             font=("Aileron", 12, "bold"), text_color="#E07B56").pack(anchor="w")

                btns = ctk.CTkFrame(card, fg_color="transparent")
                btns.pack(side="right", padx=10, pady=10)
                ctk.CTkButton(btns, text="Editar", width=65, height=28, fg_color="#7BBDE8",
                              text_color="#001D39", hover_color="#49769F",
                              font=("Aileron", 10, "bold"),
                              command=lambda cid=c['id']: [self._open_comanda_editor(cid), window.after(300, _refresh)]).pack(pady=2)
                ctk.CTkButton(btns, text="Fechar", width=65, height=28, fg_color="#28a745",
                              text_color="white", hover_color="#218838",
                              font=("Aileron", 10, "bold"),
                              command=lambda cid=c['id']: [self._fechar_comanda_dialog(cid), window.after(500, _refresh)]).pack(pady=2)

        _refresh()

        # === Histórico ===
        scroll_hist = ctk.CTkScrollableFrame(tab_historico, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        scroll_hist.pack(fill="both", expand=True)

        df_hist = self.db.listar_comandas_historico(30)
        if df_hist.empty:
            ctk.CTkLabel(scroll_hist, text="Nenhum registro.", font=("Aileron", 12), text_color="gray").pack(pady=30)
        else:
            for _, c in df_hist.iterrows():
                card = ctk.CTkFrame(scroll_hist, fg_color=("white", "#2B2B2B"), corner_radius=10)
                card.pack(fill="x", padx=5, pady=3)
                status_cor = "#28a745" if c['status'] == 'fechada' else ("#FFD93D" if c['status'] == 'aberta' else "#FF6B6B")
                mesa_txt = f" Mesa {int(c['mesa_numero'])}" if pd.notna(c.get('mesa_numero')) else f" {c['tipo_atendimento']}"
                dt_txt = str(c['data_abertura'])[:16].replace('T', ' ')
                ctk.CTkLabel(card, text=f"#{c['id']} |{mesa_txt} | {dt_txt} | {c['cliente']} | R$ {c['total']:.2f}",
                             font=("Aileron", 11), text_color=("#333", "#CCC")).pack(anchor="w", padx=12, pady=6)
                ctk.CTkLabel(card, text=c['status'].upper(), font=("Aileron", 9, "bold"),
                             text_color=status_cor).pack(anchor="e", padx=12, pady=(0, 5))

    def _nova_comanda_sem_mesa(self, tipo, parent_window, refresh_callback):
        """Cria comanda sem mesa (balcão/delivery/retirada)."""
        comanda_id = self.db.abrir_comanda(mesa_id=None, tipo_atendimento=tipo)
        self._open_comanda_editor(comanda_id)
        parent_window.after(300, refresh_callback)

    def _open_comanda_editor(self, comanda_id):
        """Editor de comanda — adicionar itens, observações, ver total."""
        comanda = self.db.get_comanda(comanda_id)
        if not comanda or comanda['status'] != 'aberta':
            messagebox.showinfo("Info", "Comanda não encontrada ou já fechada.")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Comanda #{comanda_id}")
        dialog.geometry("950x700")
        dialog.attributes("-topmost", True)

        main = ctk.CTkFrame(dialog, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=10, pady=10)
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(1, weight=1)

        # Header
        tipo_icons = {"mesa": "🍽️", "balcão": "🪑", "delivery": "🛵", "retirada": "📦"}
        tipo_icon = tipo_icons.get(comanda['tipo_atendimento'], "📋")
        mesa_txt = ""
        if comanda['mesa_id']:
            df_mesas = self.db.listar_mesas()
            mesa_row = df_mesas[df_mesas['id'] == comanda['mesa_id']]
            if not mesa_row.empty:
                mesa_txt = f" — Mesa {mesa_row.iloc[0]['numero']}"

        ctk.CTkLabel(main, text=f"{tipo_icon} Comanda #{comanda_id}{mesa_txt}",
                     font=("Aileron", 18, "bold"), text_color=("#001D39", "white")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

        # ======= ESQUERDA: Cardápio =======
        left = ctk.CTkFrame(main, fg_color=("white", "#2B2B2B"), corner_radius=15)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        left.grid_rowconfigure(2, weight=1)
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left, text="Cardápio", font=("Aileron", 14, "bold"),
                     text_color=("#001D39", "white")).grid(row=0, column=0, padx=15, pady=(10, 5), sticky="w")

        # Busca
        search_frame = ctk.CTkFrame(left, fg_color="transparent")
        search_frame.grid(row=1, column=0, sticky="ew", padx=15, pady=(0, 5))
        entry_busca = ctk.CTkEntry(search_frame, placeholder_text="🔍 Buscar item...", height=30)
        entry_busca.pack(side="left", fill="x", expand=True, padx=(0, 5))

        # Filtro de categorias
        df_prods = self.db.listar_produtos_vendaveis()
        cats = ["Todos"]
        if not df_prods.empty and 'categoria' in df_prods.columns:
            cats += sorted(df_prods['categoria'].dropna().unique().tolist())
        var_cat = ctk.StringVar(value="Todos")
        combo_cat = ctk.CTkComboBox(search_frame, values=cats, variable=var_cat,
                                     width=120, state="readonly", height=28, font=("Aileron", 10))
        combo_cat.pack(side="right")

        # Lista de produtos
        prod_scroll = ctk.CTkScrollableFrame(left, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        prod_scroll.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))

        def _render_produtos(filtro="", cat="Todos"):
            for w in prod_scroll.winfo_children():
                w.destroy()
            if df_prods.empty:
                ctk.CTkLabel(prod_scroll, text="Nenhum produto cadastrado.", text_color="gray").pack(pady=20)
                return
            for _, p in df_prods.iterrows():
                nome = str(p['nome'])
                if filtro and filtro.lower() not in nome.lower():
                    continue
                if cat != "Todos" and str(p.get('categoria', '')) != cat:
                    continue

                item_f = ctk.CTkFrame(prod_scroll, fg_color=("white", "#404040"), corner_radius=8)
                item_f.pack(fill="x", padx=3, pady=2)

                ctk.CTkLabel(item_f, text=nome, font=("Aileron", 11, "bold"),
                             text_color=("#001D39", "white")).pack(side="left", padx=10, pady=6)
                ctk.CTkLabel(item_f, text=f"R$ {p['preco']:.2f}", font=("Aileron", 11),
                             text_color="#E07B56").pack(side="left", padx=5)
                ctk.CTkLabel(item_f, text=f"Est: {p['quantidade']}", font=("Aileron", 9),
                             text_color=("#888", "#AAA")).pack(side="left", padx=5)

                # Frame para qtd + obs + botão
                action_f = ctk.CTkFrame(item_f, fg_color="transparent")
                action_f.pack(side="right", padx=5, pady=3)

                e_obs = ctk.CTkEntry(action_f, placeholder_text="Obs...", width=80, height=24, font=("Aileron", 9))
                e_obs.pack(side="left", padx=2)
                e_qtd = ctk.CTkEntry(action_f, width=35, height=24, font=("Aileron", 10))
                e_qtd.insert(0, "1")
                e_qtd.pack(side="left", padx=2)

                def _add_item(prod=p, eq=e_qtd, eo=e_obs):
                    try:
                        qtd = int(eq.get())
                    except ValueError:
                        qtd = 1
                    obs = eo.get().strip()
                    self.db.adicionar_item_comanda(comanda_id, prod['id'], prod['nome'], qtd, prod['preco'], obs)
                    _refresh_comanda()

                ctk.CTkButton(action_f, text="+", width=28, height=24, fg_color="#E07B56",
                              hover_color="#C4623E", text_color="white", font=("Aileron", 12, "bold"),
                              command=_add_item).pack(side="left", padx=2)

        def _on_search(*_):
            _render_produtos(entry_busca.get(), var_cat.get())
        entry_busca.bind("<KeyRelease>", _on_search)
        var_cat.trace_add("write", _on_search)
        _render_produtos()

        # ======= DIREITA: Itens da Comanda =======
        right = ctk.CTkFrame(main, fg_color=("white", "#2B2B2B"), corner_radius=15)
        right.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(right, text="Itens do Pedido", font=("Aileron", 14, "bold"),
                     text_color=("#001D39", "white")).grid(row=0, column=0, padx=15, pady=(10, 5), sticky="w")

        itens_scroll = ctk.CTkScrollableFrame(right, fg_color=("#F5F5F5", "#333333"), corner_radius=10)
        itens_scroll.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 5))

        # Rodapé: total + ações
        footer = ctk.CTkFrame(right, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=15, pady=(5, 10))

        lbl_total = ctk.CTkLabel(footer, text="Total: R$ 0,00", font=("Aileron", 16, "bold"),
                                  text_color="#E07B56")
        lbl_total.pack(anchor="w")

        btns_footer = ctk.CTkFrame(footer, fg_color="transparent")
        btns_footer.pack(fill="x", pady=(5, 0))

        ctk.CTkButton(btns_footer, text="💵 Fechar Comanda", height=36, fg_color="#28a745",
                      hover_color="#218838", text_color="white", font=("Aileron", 12, "bold"),
                      command=lambda: [self._fechar_comanda_dialog(comanda_id), dialog.destroy()]).pack(side="left", padx=3)
        ctk.CTkButton(btns_footer, text="÷ Dividir", height=36, fg_color="#6EA2B3",
                      hover_color="#49769F", text_color="white", font=("Aileron", 12, "bold"),
                      command=lambda: self._dividir_conta_dialog(comanda_id)).pack(side="left", padx=3)

        status_cores = {"Recebido": "#FFD93D", "Preparando": "#E07B56", "Pronto": "#28a745", "Entregue": "#7BBDE8"}

        def _refresh_comanda():
            for w in itens_scroll.winfo_children():
                w.destroy()
            df_itens = self.db.listar_itens_comanda(comanda_id)
            comanda_atualizada = self.db.get_comanda(comanda_id)
            total = comanda_atualizada['total'] if comanda_atualizada else 0
            lbl_total.configure(text=f"Total: R$ {total:.2f}")

            if df_itens.empty:
                ctk.CTkLabel(itens_scroll, text="Nenhum item adicionado.", text_color="gray").pack(pady=20)
                return

            for _, item in df_itens.iterrows():
                item_card = ctk.CTkFrame(itens_scroll, fg_color=("white", "#404040"), corner_radius=8)
                item_card.pack(fill="x", padx=3, pady=2)

                # Info
                info_f = ctk.CTkFrame(item_card, fg_color="transparent")
                info_f.pack(side="left", fill="x", expand=True, padx=8, pady=6)

                ctk.CTkLabel(info_f, text=f"{item['quantidade']}x {item['produto_nome']}",
                             font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).pack(anchor="w")
                sub = item['quantidade'] * item['preco_unitario']
                ctk.CTkLabel(info_f, text=f"R$ {sub:.2f}", font=("Aileron", 10),
                             text_color="#E07B56").pack(anchor="w")
                if item['observacao']:
                    ctk.CTkLabel(info_f, text=f"📝 {item['observacao']}", font=("Aileron", 9),
                                 text_color=("#888", "#AAA")).pack(anchor="w")

                # Status + Ações
                act_f = ctk.CTkFrame(item_card, fg_color="transparent")
                act_f.pack(side="right", padx=8, pady=4)

                status_cor = status_cores.get(item['status'], "#888")
                ctk.CTkLabel(act_f, text=item['status'], font=("Aileron", 9, "bold"),
                             text_color=status_cor).pack()

                # Avançar status
                status_flow = ["Recebido", "Preparando", "Pronto", "Entregue"]
                idx = status_flow.index(item['status']) if item['status'] in status_flow else -1
                if idx < len(status_flow) - 1:
                    next_status = status_flow[idx + 1]
                    ctk.CTkButton(act_f, text=f"→ {next_status}", width=70, height=20,
                                  font=("Aileron", 8), fg_color=status_cores.get(next_status, "#888"),
                                  text_color="white", hover_color="#49769F",
                                  command=lambda iid=item['id'], ns=next_status: [
                                      self.db.atualizar_status_item_comanda(iid, ns), _refresh_comanda()]).pack(pady=1)

                ctk.CTkButton(act_f, text="✕", width=25, height=20, fg_color="#FF6B6B",
                              text_color="white", hover_color="#FF5252", font=("Aileron", 9),
                              command=lambda iid=item['id']: [
                                  self.db.remover_item_comanda(iid), _refresh_comanda()]).pack(pady=1)

        _refresh_comanda()

    def _fechar_comanda_dialog(self, comanda_id):
        """Dialog para fechar comanda com forma de pagamento, taxa e desconto."""
        comanda = self.db.get_comanda(comanda_id)
        if not comanda or comanda['status'] != 'aberta':
            messagebox.showinfo("Info", "Comanda não encontrada ou já fechada.")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Fechar Comanda #{comanda_id}")
        dialog.geometry("450x500")
        dialog.attributes("-topmost", True)

        frame = ctk.CTkFrame(dialog, fg_color=("white", "#2B2B2B"), corner_radius=15)
        frame.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(frame, text=f"Fechar Comanda #{comanda_id}", font=("Aileron", 16, "bold"),
                     text_color=("#001D39", "white")).pack(pady=(15, 10))

        subtotal = comanda['total']
        ctk.CTkLabel(frame, text=f"Subtotal: R$ {subtotal:.2f}", font=("Aileron", 14),
                     text_color=("#001D39", "white")).pack(pady=5)

        # Taxa de serviço (10%)
        taxa_10 = round(subtotal * 0.10, 2)
        var_taxa = ctk.BooleanVar(value=False)
        chk_taxa = ctk.CTkCheckBox(frame, text=f"Taxa de serviço 10% (R$ {taxa_10:.2f})",
                                     variable=var_taxa, font=("Aileron", 12))
        chk_taxa.pack(padx=20, pady=5, anchor="w")

        # Desconto
        ctk.CTkLabel(frame, text="Desconto (R$):", font=("Aileron", 11)).pack(padx=20, anchor="w", pady=(10, 2))
        e_desc = ctk.CTkEntry(frame, placeholder_text="0,00", width=150)
        e_desc.pack(padx=20, anchor="w")

        # Forma de pagamento
        ctk.CTkLabel(frame, text="Forma de Pagamento:", font=("Aileron", 11)).pack(padx=20, anchor="w", pady=(10, 2))
        var_pgto = ctk.StringVar(value="Dinheiro")
        combo_pgto = ctk.CTkComboBox(frame, values=["Dinheiro", "PIX", "Cartão Débito", "Cartão Crédito", "Misto"],
                                      variable=var_pgto, width=200, state="readonly")
        combo_pgto.pack(padx=20, anchor="w")

        # Observação
        ctk.CTkLabel(frame, text="Observação:", font=("Aileron", 11)).pack(padx=20, anchor="w", pady=(10, 2))
        e_obs = ctk.CTkEntry(frame, placeholder_text="Opcional", width=300)
        e_obs.pack(padx=20, anchor="w")

        # Total final dinâmico
        lbl_final = ctk.CTkLabel(frame, text=f"Total: R$ {subtotal:.2f}",
                                  font=("Aileron", 18, "bold"), text_color="#28a745")
        lbl_final.pack(pady=15)

        def _recalc(*_):
            taxa = taxa_10 if var_taxa.get() else 0
            try:
                desc = float(e_desc.get().replace(",", "."))
            except ValueError:
                desc = 0
            total_f = subtotal + taxa - desc
            lbl_final.configure(text=f"Total: R$ {max(total_f, 0):.2f}")

        var_taxa.trace_add("write", _recalc)
        e_desc.bind("<KeyRelease>", _recalc)

        def _confirmar():
            from services.servico_service import ServicoService
            try:
                res = ServicoService(self.db).fechar_comanda(
                    comanda_id=comanda_id,
                    forma_pagamento=var_pgto.get(),
                    aplicar_taxa_10=bool(var_taxa.get()),
                    desconto=e_desc.get(),
                    observacao=e_obs.get(),
                )
                messagebox.showinfo("Sucesso", res["mensagem"], parent=dialog)
                dialog.destroy()
                if hasattr(self, '_refresh_mesas'):
                    self._refresh_mesas()
            except (ValueError, RuntimeError) as err:
                messagebox.showerror("Erro", str(err), parent=dialog)

        ctk.CTkButton(frame, text="CONFIRMAR FECHAMENTO", height=40, fg_color="#28a745",
                      hover_color="#218838", text_color="white", font=("Aileron", 13, "bold"),
                      command=_confirmar).pack(fill="x", padx=20, pady=(5, 15))

    def _dividir_conta_dialog(self, comanda_id):
        """Dialog para dividir a conta."""
        comanda = self.db.get_comanda(comanda_id)
        if not comanda:
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Dividir Conta")
        dialog.geometry("350x300")
        dialog.attributes("-topmost", True)

        frame = ctk.CTkFrame(dialog, fg_color=("white", "#2B2B2B"), corner_radius=15)
        frame.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(frame, text="÷ Dividir Conta", font=("Aileron", 16, "bold"),
                     text_color=("#001D39", "white")).pack(pady=(15, 10))

        total = comanda['total']
        ctk.CTkLabel(frame, text=f"Total: R$ {total:.2f}", font=("Aileron", 14),
                     text_color="#E07B56").pack(pady=5)

        ctk.CTkLabel(frame, text="Número de pessoas:", font=("Aileron", 12)).pack(pady=(10, 3))
        e_pessoas = ctk.CTkEntry(frame, width=80, font=("Aileron", 14))
        e_pessoas.insert(0, "2")
        e_pessoas.pack()

        lbl_result = ctk.CTkLabel(frame, text="", font=("Aileron", 15, "bold"), text_color="#28a745", wraplength=300)
        lbl_result.pack(pady=15)

        def _calc(*_):
            from collections import Counter
            from services.servico_service import ServicoService
            try:
                resultado = ServicoService(self.db).dividir_conta(comanda_id, e_pessoas.get())
                partes = resultado.get("partes") or []
                if partes:
                    contagem = Counter(partes)
                    texto = " + ".join(
                        f"{qtd} × R$ {val:.2f}".replace(".", ",")
                        for val, qtd in contagem.items()
                    )
                    lbl_result.configure(text=texto)
                else:
                    lbl_result.configure(text=f"R$ {resultado['valor_por_pessoa']:.2f} por pessoa")
            except ValueError:
                pass

        e_pessoas.bind("<KeyRelease>", _calc)
        _calc()

    # ==================== RESTAURANTE - COZINHA ====================

    def open_cozinha_dialog(self):
        """Painel da cozinha — mostra pedidos pendentes para preparar."""
        window = ctk.CTkToplevel(self)
        window.title("🍳 Painel da Cozinha")
        window.geometry("900x600")
        window.attributes("-topmost", True)

        main_frame = ctk.CTkFrame(window, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)

        header = ctk.CTkFrame(main_frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header, text="🍳 Painel da Cozinha", font=("Aileron", 20, "bold"),
                     text_color=("#001D39", "white")).pack(side="left")
        ctk.CTkButton(header, text="🔄 Atualizar", width=100, height=30, fg_color="#E07B56",
                      hover_color="#C4623E", text_color="white", font=("Aileron", 11, "bold"),
                      command=lambda: _refresh()).pack(side="right")

        # Colunas: Recebido | Preparando | Pronto
        cols_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        cols_frame.pack(fill="both", expand=True)
        cols_frame.grid_columnconfigure(0, weight=1)
        cols_frame.grid_columnconfigure(1, weight=1)
        cols_frame.grid_columnconfigure(2, weight=1)
        cols_frame.grid_rowconfigure(1, weight=1)

        status_config = [
            ("Recebido", "#FFD93D", "Preparando"),
            ("Preparando", "#E07B56", "Pronto"),
            ("Pronto", "#28a745", "Entregue"),
        ]

        col_scrolls = {}
        for i, (status, cor, next_st) in enumerate(status_config):
            ctk.CTkLabel(cols_frame, text=f"● {status.upper()}", font=("Aileron", 14, "bold"),
                         text_color=cor).grid(row=0, column=i, padx=5, pady=(0, 5))
            scroll = ctk.CTkScrollableFrame(cols_frame, fg_color=("#F0F0F0", "#2B2B2B"), corner_radius=10)
            scroll.grid(row=1, column=i, sticky="nsew", padx=5)
            col_scrolls[status] = scroll

        def _refresh():
            for scroll in col_scrolls.values():
                for w in scroll.winfo_children():
                    w.destroy()

            df = self.db.itens_cozinha_pendentes()
            if df.empty:
                ctk.CTkLabel(col_scrolls["Recebido"], text="Nenhum pedido pendente! 🎉",
                             font=("Aileron", 12), text_color="gray").pack(pady=30)
                return

            for _, item in df.iterrows():
                status = item['status']
                if status not in col_scrolls:
                    continue
                scroll = col_scrolls[status]

                card = ctk.CTkFrame(scroll, fg_color=("white", "#404040"), corner_radius=10)
                card.pack(fill="x", padx=5, pady=4)

                mesa_txt = f"Mesa {int(item['mesa_numero'])}" if pd.notna(item.get('mesa_numero')) else item['tipo_atendimento'].capitalize()
                ctk.CTkLabel(card, text=f"📍 {mesa_txt}", font=("Aileron", 9, "bold"),
                             text_color=("#888", "#AAA")).pack(anchor="w", padx=10, pady=(6, 0))
                ctk.CTkLabel(card, text=f"{item['quantidade']}x {item['produto_nome']}",
                             font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(anchor="w", padx=10)
                if item['observacao']:
                    ctk.CTkLabel(card, text=f"📝 {item['observacao']}", font=("Aileron", 9),
                                 text_color="#E07B56").pack(anchor="w", padx=10)

                hora = str(item['data_hora'])[11:16]
                ctk.CTkLabel(card, text=f"⏰ {hora}", font=("Aileron", 9), text_color=("#888", "#AAA")).pack(anchor="w", padx=10)

                # Botão avançar
                next_map = {"Recebido": "Preparando", "Preparando": "Pronto", "Pronto": "Entregue"}
                next_st = next_map.get(status)
                if next_st:
                    cor_btn = {"Preparando": "#E07B56", "Pronto": "#28a745", "Entregue": "#7BBDE8"}.get(next_st, "#888")
                    ctk.CTkButton(card, text=f"→ {next_st}", height=26, fg_color=cor_btn,
                                  text_color="white", hover_color="#49769F", font=("Aileron", 10, "bold"),
                                  command=lambda iid=item['id'], ns=next_st: [
                                      self.db.atualizar_status_item_comanda(iid, ns), _refresh()]).pack(
                        fill="x", padx=10, pady=(3, 8))

        _refresh()
        # Auto-refresh a cada 15 segundos
        def _auto_refresh():
            try:
                if window.winfo_exists():
                    _refresh()
                    window.after(15000, _auto_refresh)
            except Exception:
                pass
        window.after(15000, _auto_refresh)

    # ================================================================
    # ============ NOVAS FUNCIONALIDADES ============
    # ================================================================

    # --- Dashboard Auto-Refresh (Smart Dirty-Check) & Backup Periódico ---
    def _check_scheduled_backup(self):
        """Executa verificação periódica do backup automático com retenção GFS (a cada 6h)."""
        try:
            if not self.winfo_exists():
                return
            success, msg = self.db.verificar_backup_diario()
            if success:
                logger.info("Verificação periódica de backup: %s", msg)
            self.after(6 * 3600 * 1000, self._check_scheduled_backup)
        except Exception as e:
            logger.warning("Erro no agendamento periódico de backup: %s", e)

    def _start_auto_refresh(self):
        """Atualiza o dashboard a cada 60s apenas se houve alteração real no banco de dados."""
        def _tick():
            try:
                if self.winfo_exists():
                    db_mod = getattr(self.db, 'last_modified', 0.0)
                    last_render = getattr(self, '_last_dashboard_render_ts', -1.0)
                    if db_mod != last_render:
                        self.refresh_dashboard()
                    self.after(60000, _tick)
            except Exception:
                pass
        self.after(60000, _tick)
        self.after(6 * 3600 * 1000, self._check_scheduled_backup)

    def open_inventario_dialog(self, parent=None):
        """Abre diálogo de Inventário Físico e Contagem Cega."""
        from views.inventario_view import InventarioDialog
        InventarioDialog(self, parent=parent)

    def open_sugestao_compras_dialog(self, parent=None):
        """Abre tela de Sugestão Inteligente de Compras (30/60/90 dias)."""
        from views.compras_view import SugestaoComprasDialog
        SugestaoComprasDialog(self, parent=parent)

    def open_importar_nfe_xml(self):
        """Faz parsing não-bloqueante de XML NF-e e abre prévia para vínculo manual antes de gravar."""
        filepath = filedialog.askopenfilename(
            title="Selecionar XML da NF-e (SEFAZ)",
            filetypes=[("Arquivo XML NF-e", "*.xml"), ("Todos os arquivos", "*.*")]
        )
        if not filepath:
            return
        try:
            self.notifier.notify("NF-e XML", "Analisando itens do XML em segundo plano...", tipo="info", duracao=3000)
        except Exception:
            pass

        def _worker():
            ok, dados_ou_erro = self.db.parse_nfe_xml(filepath, margem_padrao=0.35)

            def _done():
                if not self.winfo_exists():
                    return
                if ok:
                    from views.nfe_preview_view import NFePreviewDialog
                    NFePreviewDialog(self, dados_ou_erro)
                else:
                    messagebox.showerror("Erro ao Analisar NF-e", str(dados_ou_erro), parent=self)

            self.after(0, _done)

        threading.Thread(target=_worker, name="EstokeParseNFe", daemon=True).start()

    # --- Notificações Toast na Inicialização ---
    def _exibir_alertas_iniciais(self):
        """Exibe notificações toast para alertas pendentes (exceto estoque baixo, que vira tarefa)."""
        try:
            # Criar tarefas automáticas para estoque baixo (sem popup)
            count = self.db.criar_tarefas_estoque_baixo()
            if count > 0:
                self.refresh_metas_list()
                self.notifier.notify(
                    "Estoque Baixo",
                    f"{count} tarefa(s) de reposição criada(s) automaticamente.",
                    tipo="info",
                    duracao=5000
                )
            # Demais alertas (validade, fiados etc.) continuam como toast
            alertas = self.db.coletar_alertas()
            for alerta in alertas:
                if alerta["tipo"] == "estoque_baixo":
                    continue  # Já tratado como tarefa
                tipo_toast = "error" if alerta["prioridade"] == "alta" else "warning"
                self.notifier.notify(
                    alerta["titulo"],
                    alerta["mensagem"],
                    tipo=tipo_toast,
                    duracao=8000
                )
        except Exception as e:
            logger.debug("Erro ao exibir alertas iniciais: %s", e)

    # --- Iniciar API REST ---
    def _iniciar_api_server(self):
        """Inicia o servidor REST API para comunicação com o app mobile."""
        try:
            import api_server
            if api_server.api_disponivel():
                self.api_server = api_server.EstokeAPI(self.db, port=5050)
                self.api_server.start()
                logger.info("API REST iniciada na porta 5050")
        except Exception as e:
            logger.debug("API REST não disponível: %s", e)

    # ================================================================
    # ============ VIEWS MODULARIZADAS (ONDA 5) ============
    # ================================================================

    def open_contas_pagar_dialog(self):
        from views.financeiro_view import FinanceiroView
        FinanceiroView(self).open_contas_pagar_dialog()

    def open_etiquetas_dialog(self):
        from views.admin_view import AdminView
        AdminView(self).open_etiquetas_dialog()

    def open_dre_dialog(self):
        from views.financeiro_view import FinanceiroView
        FinanceiroView(self).open_dre_dialog()

    def open_fluxo_caixa_dialog(self):
        from views.financeiro_view import FinanceiroView
        FinanceiroView(self).open_fluxo_caixa_dialog()

    def open_comissoes_dialog(self):
        from views.financeiro_view import FinanceiroView
        FinanceiroView(self).open_comissoes_dialog()

    def open_lojas_dialog(self):
        from views.admin_view import AdminView
        AdminView(self).open_lojas_dialog()

    def open_usuarios_dialog(self):
        from views.admin_view import AdminView
        AdminView(self).open_usuarios_dialog()


def run_self_test() -> int:
    """
    Executa smoke test completo de boot isolado (--self-test) em ambiente virgem (ESTOKE_DATA_DIR).
    Grava relatório JSON estruturado sem depender de console/stdout (compatível com console=False).
    """
    report_dir = paths.get_app_data_path()
    report_path = os.path.join(report_dir, "selftest_report.json")
    report = {
        "status": "error",
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "python_version": sys.version,
        "data_dir": report_dir,
        "resource_dir": paths.get_resource_path(),
        "checks": {},
        "error": "",
    }
    try:
        # 1. Validar módulos nativos, empacotados e internos
        modulos = [
            "sqlite3",
            "cryptography",
            "numpy",
            "pandas",
            "PIL",
            "reportlab",
            "customtkinter",
            "certifi",
            "flask",
            "google.genai",
            "paths",
            "migrations",
            "views.pdv_view",
            "views.inventario_view",
            "views.nfe_preview_view",
            "views.compras_view",
            "views.financeiro_view",
            "views.admin_view",
            "services.fiscal_service",
            "services.outbox_service",
            "services.scheduler",
            "services.updater",
            "services.compras_service",
            "services.relatorio_caixa",
            "services.mercadopago_service",
            # Dependências pesadas carregadas sob demanda pelas telas (dashboard, NF-e, etiquetas, scanner)
            "unittest",
            "pyparsing",
            "matplotlib",
            "matplotlib.figure",
            "matplotlib.backends.backend_tkagg",
            "pdfplumber",
            "pdfplumber.display",
            "qrcode",
            "barcode",
            "escpos",
            "cv2",
            "pyzbar.pyzbar",
            "label_generator",
            "invoice_generator",
            "ai_module",
            "notifications",
            "api_server",
            "printer",
        ]
        for mod_name in modulos:
            __import__(mod_name)
        report["checks"]["imports"] = {"ok": True, "count": len(modulos)}

        # 2. Validar bundle SSL certifi e recursos empacotados
        import certifi
        cacert = certifi.where()
        icon_ico = paths.get_resource_path("icon", "icon-cubo.ico")
        if not os.path.isfile(cacert):
            raise FileNotFoundError(f"certifi cacert.pem ausente: {cacert}")
        if not os.path.isfile(icon_ico):
            raise FileNotFoundError(f"Ícone principal ausente: {icon_ico}")
        report["checks"]["assets"] = {"ok": True, "cacert": cacert, "icon": icon_ico}

        # 3. Validar gravação de config.json, banco SQLite, migrações, backup criptografado e PDF ReportLab
        cfg = config.load_config()
        cfg["_selftest_boot"] = True
        config.save_config(cfg)

        import migrations
        db = database.Database(db_name="selftest.db")
        try:
            ver = migrations.get_user_version(db.conn)
            if ver != migrations.LATEST_SCHEMA_VERSION:
                raise RuntimeError(f"Versão de schema inesperada: {ver} != {migrations.LATEST_SCHEMA_VERSION}")
            ok_bk, bk_path = db.criar_backup()
            if not ok_bk:
                raise RuntimeError(f"Falha ao criar backup criptografado no self-test: {bk_path}")
        finally:
            db.close()

        from services.relatorio_caixa import gerar_pdf_fechamento
        pdf_tmp = paths.get_temp_dir("selftest_caixa.pdf")
        gerar_pdf_fechamento({"id": 1, "total_vendas": 100.0, "conciliacao_pix": {}}, pdf_tmp)
        if not os.path.isfile(pdf_tmp) or os.path.getsize(pdf_tmp) < 500:
            raise RuntimeError("PDF ReportLab de smoke test não foi gerado corretamente.")

        report["checks"]["database_migrations_backup"] = {"ok": True, "schema_version": ver, "enc_exists": os.path.exists(os.path.join(report_dir, "selftest.db.enc"))}
        report["checks"]["reportlab_pdf"] = {"ok": True, "size_bytes": os.path.getsize(pdf_tmp)}

        # 4. Renderizar gráfico matplotlib (valida mpl-data, fontes e cadeia pyparsing/unittest do dashboard)
        from matplotlib.figure import Figure as _SelfTestFigure
        fig = _SelfTestFigure(figsize=(3, 2), dpi=80)
        ax = fig.add_subplot(111)
        ax.pie([3, 2, 1], labels=["A", "B", "C"])
        png_tmp = paths.get_temp_dir("selftest_chart.png")
        fig.savefig(png_tmp)
        if not os.path.isfile(png_tmp) or os.path.getsize(png_tmp) < 500:
            raise RuntimeError("Gráfico matplotlib de smoke test não foi gerado corretamente.")
        report["checks"]["matplotlib_chart"] = {"ok": True, "size_bytes": os.path.getsize(png_tmp)}
        report["status"] = "ok"
        code = 0
    except Exception as exc:
        import traceback
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        code = 1

    try:
        with open(report_path, "w", encoding="utf-8") as rf:
            json.dump(report, rf, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return code


if __name__ == '__main__':
    if "--self-test" in sys.argv[1:]:
        sys.exit(run_self_test())

    import atexit
    app = App()
    crash_handler.install_crash_handlers(app=app, show_ui=True)
    
    def fechar_banco_atexit():
        try:
            app.db.close()
        except Exception:
            pass

    atexit.register(fechar_banco_atexit)
    
    try:
        app.mainloop()
    finally:
        # Garantir fechamento do banco de dados
        fechar_banco_atexit()
        logger.info("Aplicação encerrada.")
