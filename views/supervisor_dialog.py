"""
Modal de autorização de supervisor (Admin / Gerente) para ações sensíveis no PDV e Caixa.
"""
import customtkinter as ctk

import config
from views.base import BaseDialog


class SupervisorAuthDialog(BaseDialog):
    """Solicita credenciais de administrador/gerente e, opcionalmente, motivo obrigatório."""

    def __init__(self, app, acao: str, exigir_motivo: bool = False, parent=None):
        self.acao = acao
        self.exigir_motivo = exigir_motivo
        self._parent_win = parent
        self.result = {"approved": False, "supervisor_id": None, "supervisor_nome": "", "motivo": ""}
        height = 410 if exigir_motivo else 330
        super().__init__(app, title="Autorização de Supervisor", geometry=f"420x{height}", topmost=True)

    def build_ui(self):
        self.resizable(False, False)
        if self._parent_win is not None:
            try:
                self.transient(self._parent_win)
            except Exception:
                pass
        try:
            self.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            self,
            text="🔐 Autorização de Supervisor",
            font=("Aileron", 17, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(16, 4))

        ctk.CTkLabel(
            self,
            text=f"Ação: {self.acao}",
            font=("Aileron", 11, "bold"),
            text_color="#E67E22",
            wraplength=370,
        ).pack(pady=(0, 10), padx=20)

        card = self.create_card(self)
        card.pack(fill="both", expand=True, padx=20, pady=(0, 14))

        ctk.CTkLabel(card, text="Login do Supervisor (Admin / Gerente):", font=("Aileron", 11)).pack(
            anchor="w", padx=15, pady=(12, 2)
        )
        self.entry_login = ctk.CTkEntry(card, height=34, placeholder_text="Usuário supervisor")
        self.entry_login.pack(fill="x", padx=15, pady=(0, 8))

        ctk.CTkLabel(card, text="Senha:", font=("Aileron", 11)).pack(anchor="w", padx=15, pady=(0, 2))
        self.entry_senha = ctk.CTkEntry(card, height=34, placeholder_text="Senha", show="*")
        self.entry_senha.pack(fill="x", padx=15, pady=(0, 8))

        self.entry_motivo = None
        if self.exigir_motivo:
            ctk.CTkLabel(card, text="Motivo obrigatório:", font=("Aileron", 11, "bold")).pack(
                anchor="w", padx=15, pady=(2, 2)
            )
            self.entry_motivo = ctk.CTkEntry(
                card, height=34, placeholder_text="Descreva o motivo do cancelamento/estorno..."
            )
            self.entry_motivo.pack(fill="x", padx=15, pady=(0, 8))

        self.lbl_erro = ctk.CTkLabel(card, text="", font=("Aileron", 11), text_color="#E74C3C")
        self.lbl_erro.pack(pady=(2, 4))

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=15, pady=(4, 12))

        ctk.CTkButton(
            btn_row,
            text="Autorizar",
            height=36,
            fg_color="#0A4174",
            hover_color="#062F56",
            text_color="white",
            font=("Aileron", 12, "bold"),
            command=self._validar,
        ).pack(side="left", fill="x", expand=True, padx=(0, 5))

        ctk.CTkButton(
            btn_row,
            text="Cancelar",
            height=36,
            fg_color=("#D5D5DC", "#444444"),
            hover_color=("#C0C0CA", "#555555"),
            text_color=("#222222", "white"),
            font=("Aileron", 12, "bold"),
            command=self._cancelar,
        ).pack(side="right", fill="x", expand=True, padx=(5, 0))

        self.entry_login.focus_set()
        self.entry_senha.bind("<Return>", lambda _e: self._validar())
        if self.entry_motivo is not None:
            self.entry_motivo.bind("<Return>", lambda _e: self._validar())
        self.protocol("WM_DELETE_WINDOW", self._cancelar)

    def _validar(self):
        from services.auth_service import AuthService

        login = self.entry_login.get().strip()
        senha = self.entry_senha.get()
        motivo = self.entry_motivo.get().strip() if self.entry_motivo is not None else ""

        try:
            auth = AuthService(self.db, config_module=config)
            res = auth.autorizar_supervisor(
                login=login,
                senha=senha,
                acao=self.acao,
                motivo=motivo,
                exigir_motivo=self.exigir_motivo,
            )
        except ValueError as err:
            self.lbl_erro.configure(text=str(err))
            if self.exigir_motivo and not motivo and self.entry_motivo is not None:
                self.entry_motivo.focus_set()
            return
        except PermissionError as err:
            self.lbl_erro.configure(text=str(err))
            return

        self.result = {
            "approved": res["approved"],
            "supervisor_id": res["supervisor_id"],
            "supervisor_nome": res["supervisor_nome"],
            "motivo": res["motivo"],
        }
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _cancelar(self):
        self.result = {"approved": False, "supervisor_id": None, "supervisor_nome": "", "motivo": ""}
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def solicitar_autorizacao_supervisor(
    app,
    acao: str,
    exigir_motivo: bool = False,
    parent=None,
    exigir_senha_sempre: bool = False,
):
    """
    Solicita autorização de supervisor (Admin/Gerente).
    Se o usuário logado já for Admin/Gerente e não exigir senha nem motivo, aprova direto;
    caso contrário exibe SupervisorAuthDialog e aguarda conclusão.
    """
    cur_user = getattr(app, "current_user", None)
    if (
        not exigir_senha_sempre
        and not exigir_motivo
        and isinstance(cur_user, dict)
        and cur_user.get("perfil") in ("admin", "gerente")
    ):
        return True, {
            "approved": True,
            "supervisor_id": cur_user.get("id"),
            "supervisor_nome": cur_user.get("nome", "Supervisor"),
            "motivo": "",
        }

    dlg = SupervisorAuthDialog(app, acao=acao, exigir_motivo=exigir_motivo, parent=parent)
    dlg.wait_window()
    return bool(dlg.result.get("approved")), dlg.result
