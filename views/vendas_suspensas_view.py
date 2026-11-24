"""
Diálogo de gerenciamento de Vendas Suspensas e Orçamentos do PDV (Shift+F6).
Permite retomar itens no carrinho ativo do PDV, converter em venda ou excluir.
"""
from tkinter import messagebox, ttk
import customtkinter as ctk

from views.base import BaseDialog


class VendasSuspensasDialog(BaseDialog):
    """Lista e gerencia vendas suspensas (F6) e orçamentos ativos."""

    def __init__(self, app, on_resume_callback=None, parent=None):
        self.on_resume_callback = on_resume_callback
        self._parent_win = parent
        super().__init__(app, title="Vendas Suspensas e Orçamentos (Shift+F6)", geometry="860x520", topmost=True)

    def build_ui(self):
        if self._parent_win is not None:
            try:
                self.transient(self._parent_win)
            except Exception:
                pass
        try:
            self.grab_set()
        except Exception:
            pass

        header = self.create_card(self)
        header.pack(fill="x", padx=14, pady=(12, 8))

        self.create_title_label(header, text="⏸️ Vendas Suspensas & Orçamentos").pack(side="left", padx=15, pady=12)

        self.combo_tipo = ctk.CTkComboBox(
            header,
            values=["Todos", "suspensa", "orcamento"],
            width=150,
            state="readonly",
            command=lambda _v: self._carregar_lista(),
        )
        self.combo_tipo.set("Todos")
        self.combo_tipo.pack(side="right", padx=15, pady=10)

        tree_container = ctk.CTkFrame(self, fg_color="transparent")
        tree_container.pack(fill="both", expand=True, padx=14, pady=4)

        cols = ("id", "codigo", "tipo", "criado_em", "cliente", "subtotal", "desconto", "total", "observacao")
        self.tree = ttk.Treeview(tree_container, columns=cols, show="headings", height=14)
        col_specs = [
            ("id", "ID", 45),
            ("codigo", "Código", 130),
            ("tipo", "Tipo", 85),
            ("criado_em", "Data/Hora", 125),
            ("cliente", "Cliente", 150),
            ("subtotal", "Subtotal", 85),
            ("desconto", "Desc.", 75),
            ("total", "Total", 85),
            ("observacao", "Observação", 150),
        ]
        for col, heading, width in col_specs:
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, anchor="center" if col in ("id", "tipo", "codigo") else "w")

        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self._retomar_selecionada())

        btn_bar = ctk.CTkFrame(self, fg_color="transparent")
        btn_bar.pack(fill="x", padx=14, pady=(8, 14))

        ctk.CTkButton(
            btn_bar,
            text="🔄 Retomar no Carrinho",
            height=38,
            fg_color="#0A4174",
            hover_color="#062F56",
            text_color="white",
            font=("Aileron", 12, "bold"),
            command=self._retomar_selecionada,
        ).pack(side="left", fill="x", expand=True, padx=(0, 5))

        ctk.CTkButton(
            btn_bar,
            text="✅ Converter em Venda (Dinheiro)",
            height=38,
            fg_color="#28a745",
            hover_color="#218838",
            text_color="white",
            font=("Aileron", 12, "bold"),
            command=self._converter_em_venda,
        ).pack(side="left", fill="x", expand=True, padx=5)

        ctk.CTkButton(
            btn_bar,
            text="🗑️ Excluir",
            height=38,
            fg_color="#FF6B6B",
            hover_color="#FF5252",
            text_color="white",
            font=("Aileron", 12, "bold"),
            width=120,
            command=self._excluir_selecionada,
        ).pack(side="right", padx=(5, 0))

        self._carregar_lista()

    def _carregar_lista(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        tipo_sel = self.combo_tipo.get()
        tipo_param = None if tipo_sel == "Todos" else tipo_sel
        df = self.db.listar_vendas_suspensas(status="Ativa", tipo=tipo_param)
        if df.empty:
            return
        for _, r in df.iterrows():
            self.tree.insert(
                "",
                "end",
                values=(
                    int(r["id"]),
                    r.get("codigo", ""),
                    "Orçamento" if r.get("tipo") == "orcamento" else "Suspensa",
                    str(r.get("criado_em", ""))[:16].replace("T", " "),
                    r.get("cliente", "Consumidor Final"),
                    f"R$ {float(r.get('subtotal', 0)):.2f}",
                    f"R$ {float(r.get('desconto', 0)):.2f}",
                    f"R$ {float(r.get('total', 0)):.2f}",
                    r.get("observacao", "") or "",
                ),
            )

    def _get_selected_id(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Aviso", "Selecione um registro na lista.", parent=self)
            return None
        return int(self.tree.item(sel[0])["values"][0])

    def _retomar_selecionada(self):
        susp_id = self._get_selected_id()
        if susp_id is None:
            return
        dados = self.db.obter_venda_suspensa(susp_id)
        if not dados:
            messagebox.showerror("Erro", "Registro não encontrado.", parent=self)
            return
        self.db.marcar_venda_suspensa_retomada(susp_id)
        if callable(self.on_resume_callback):
            self.on_resume_callback(dados)
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _converter_em_venda(self):
        susp_id = self._get_selected_id()
        if susp_id is None:
            return
        if not messagebox.askyesno("Confirmar Conversão", f"Converter registro #{susp_id} diretamente em venda concluída?", parent=self):
            return
        user_id = self.app.current_user.get("id") if getattr(self.app, "current_user", None) else None
        loja_id = self.app.current_user.get("loja_id") if getattr(self.app, "current_user", None) else None
        ok, res = self.db.converter_venda_suspensa(
            susp_id, forma_pagamento="Dinheiro", usuario_id=user_id, loja_id=loja_id
        )
        if ok:
            messagebox.showinfo("Venda Gerada", f"Orçamento/Venda suspensa #{susp_id} convertido com sucesso!", parent=self)
            self._carregar_lista()
            self.refresh_dashboard()
        else:
            messagebox.showerror("Erro", f"Não foi possível converter: {res}", parent=self)

    def _excluir_selecionada(self):
        susp_id = self._get_selected_id()
        if susp_id is None:
            return
        if not messagebox.askyesno("Excluir", f"Remover registro #{susp_id} da lista?", parent=self):
            return
        self.db.excluir_venda_suspensa(susp_id, motivo="Removido manualmente pelo operador")
        self._carregar_lista()
