"""
Classe base para todos os diálogos/views do EstokeAoCubo.

Fornece acesso padronizado ao app, db e notifier,
além de helpers comuns para criação de UI.
"""
import customtkinter as ctk
from tkinter import messagebox
import logging

logger = logging.getLogger('EstokeAoCubo.views')


class BaseDialog(ctk.CTkToplevel):
    """Base reutilizável para janelas modais/diálogos.
    
    Subclasses devem implementar `build_ui()`.
    
    Atributos:
        app:      Referência à instância principal App
        db:       Referência ao Database
        notifier: Referência ao NotificationManager
    """

    def __init__(self, app, title="EstokeAoCubo", geometry="700x600", topmost=True):
        super().__init__(app)
        self.app = app
        self.db = app.db
        self.notifier = app.notifier
        
        self.title(title)
        self.geometry(geometry)
        if topmost:
            self.attributes("-topmost", True)
        
        # Herdar ícone do app em alta definição (após timer de 200ms do CustomTkinter)
        try:
            if hasattr(app, '_icon_path_ico') and app._icon_path_ico:
                self.after(50, lambda: self.iconbitmap(app._icon_path_ico))
            if hasattr(app, '_aplicar_icone_win32_hd'):
                self.after(250, lambda: app._aplicar_icone_win32_hd(self))
        except Exception:
            pass
        
        self.build_ui()

    def build_ui(self):
        """Subclasses devem sobrescrever para construir a interface."""
        raise NotImplementedError

    def refresh_dashboard(self):
        """Atualiza o dashboard do app principal."""
        if hasattr(self.app, 'refresh_dashboard'):
            self.app.refresh_dashboard()

    # --- Helpers de UI comuns ---
    
    def create_card(self, parent, **kwargs):
        """Cria um card com estilo padrão do app."""
        defaults = {
            'fg_color': ("white", "#2B2B2B"),
            'corner_radius': 15,
            'border_width': 1,
            'border_color': ("#E6E6E6", "#444444"),
        }
        defaults.update(kwargs)
        return ctk.CTkFrame(parent, **defaults)

    def create_title_label(self, parent, text, **kwargs):
        """Cria label de título com estilo padrão."""
        defaults = {
            'font': ctk.CTkFont(family="Aileron", size=18, weight="bold"),
            'text_color': ("#001D39", "white"),
        }
        defaults.update(kwargs)
        return ctk.CTkLabel(parent, text=text, **defaults)

    def create_action_button(self, parent, text, command, **kwargs):
        """Cria botão de ação com estilo padrão."""
        defaults = {
            'height': 35,
            'corner_radius': 20,
            'fg_color': "#7BBDE8",
            'hover_color': "#49769F",
            'text_color': "#001D39",
            'font': ctk.CTkFont(family="Aileron", size=12, weight="bold"),
        }
        defaults.update(kwargs)
        return ctk.CTkButton(parent, text=text, command=command, **defaults)
