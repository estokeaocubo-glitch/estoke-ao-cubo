# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Tela de Prévia na Importação de NF-e XML (Onda 3)
Exibe fornecedor, duplicatas e itens da NF-e antes de gravar no banco, permitindo
vincular manualmente itens sem EAN reconhecido a produtos já existentes no catálogo.
"""

from __future__ import annotations

import threading
from tkinter import messagebox, ttk

import customtkinter as ctk

from views.base import BaseDialog


class NFePreviewDialog(BaseDialog):
    """Diálogo de revisão e vinculação manual antes de efetivar NF-e XML no estoque."""

    def __init__(self, app, dados_nfe: dict, on_imported_callback=None, parent=None):
        self.dados_nfe = dados_nfe
        self.on_imported_callback = on_imported_callback
        self._parent_win = parent
        # idx_item -> produto_id (int) ou None (criar novo)
        self.mapeamentos: dict[int, int | None] = {
            int(it["index"]): it.get("matched_produto_id")
            for it in (dados_nfe.get("itens") or [])
        }
        self._catalogo_labels: dict[str, int | None] = {"🆕 Criar Novo Produto": None}
        self._pid_to_label: dict[int | None, str] = {None: "🆕 Criar Novo Produto"}
        for p in dados_nfe.get("catalogo_produtos") or []:
            ean = f" [{p['codigo_barras']}]" if p.get("codigo_barras") else ""
            lbl = f"#{p['id']} - {p['nome']}{ean}"
            self._catalogo_labels[lbl] = int(p["id"])
            self._pid_to_label[int(p["id"])] = lbl

        super().__init__(
            app,
            title=f"Prévia NF-e #{dados_nfe.get('numero_nfe', 'S/N')}",
            geometry="980x640",
            topmost=True,
        )

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

        forn = self.dados_nfe.get("fornecedor") or {}
        itens = self.dados_nfe.get("itens") or []
        dups = self.dados_nfe.get("duplicatas") or []

        header = self.create_card(self)
        header.pack(fill="x", padx=14, pady=(12, 8))

        self.create_title_label(
            header,
            text=f"📄 NF-e #{self.dados_nfe.get('numero_nfe', 'S/N')} — {forn.get('nome', 'Fornecedor')}",
        ).pack(anchor="w", padx=14, pady=(10, 2))

        sub = (
            f"CNPJ/CPF: {forn.get('cnpj') or 'Não inf.'}  |  Itens: {len(itens)}  |  "
            f"Duplicatas: {len(dups)}  |  Total NF-e: R$ {float(self.dados_nfe.get('valor_total', 0)):.2f}"
        )
        ctk.CTkLabel(header, text=sub, font=("Segoe UI", 12), text_color="#64748B").pack(
            anchor="w", padx=14, pady=(0, 10)
        )

        # Tabela de itens da NF-e
        tree_frame = ctk.CTkFrame(self, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=14, pady=4)

        cols = ("idx", "ean", "xprod", "qtd", "vun", "status", "vinculo")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=14)
        specs = [
            ("idx", "#", 40),
            ("ean", "EAN / Cód.", 115),
            ("xprod", "Descrição no XML", 250),
            ("qtd", "Qtd", 65),
            ("vun", "Custo Un.", 85),
            ("status", "Match", 115),
            ("vinculo", "Ação / Produto Vinculado", 250),
        ]
        for c, h, w in specs:
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center" if c in ("idx", "qtd", "status") else "w")

        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._carregar_editor_item())

        # Barra de vínculo manual para o item selecionado
        bind_card = self.create_card(self)
        bind_card.pack(fill="x", padx=14, pady=(6, 8))

        self.lbl_item_sel = ctk.CTkLabel(
            bind_card,
            text="Selecione um item acima para vincular a um produto existente ou criar novo:",
            font=("Segoe UI", 12, "bold"),
        )
        self.lbl_item_sel.pack(anchor="w", padx=14, pady=(8, 4))

        row_bind = ctk.CTkFrame(bind_card, fg_color="transparent")
        row_bind.pack(fill="x", padx=14, pady=(0, 10))

        self.combo_vinculo = ctk.CTkComboBox(
            row_bind,
            values=list(self._catalogo_labels.keys()),
            width=540,
            state="readonly",
        )
        self.combo_vinculo.set("🆕 Criar Novo Produto")
        self.combo_vinculo.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            row_bind,
            text="Aplicar Vínculo ao Item",
            width=180,
            height=34,
            fg_color="#0A4174",
            hover_color="#062F56",
            command=self._aplicar_vinculo_item,
        ).pack(side="left")

        # Rodapé de opções e confirmação
        footer = self.create_card(self)
        footer.pack(fill="x", padx=14, pady=(4, 12))

        self.var_lancar_dups = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            footer,
            text=f"Lançar {len(dups)} duplicata(s) em Contas a Pagar",
            variable=self.var_lancar_dups,
            font=("Segoe UI", 12),
        ).pack(side="left", padx=14, pady=12)

        ctk.CTkLabel(footer, text="Margem padrão (%):", font=("Segoe UI", 12)).pack(side="left", padx=(10, 4))
        self.entry_margem = ctk.CTkEntry(footer, width=60)
        self.entry_margem.insert(0, "35")
        self.entry_margem.pack(side="left", padx=(0, 10))

        self.btn_confirmar = ctk.CTkButton(
            footer,
            text="✅ Confirmar e Importar NF-e",
            height=38,
            fg_color="#059669",
            hover_color="#047857",
            command=self._confirmar_importacao,
        )
        self.btn_confirmar.pack(side="right", padx=(6, 14), pady=10)

        ctk.CTkButton(
            footer,
            text="Cancelar",
            width=100,
            height=38,
            fg_color="#64748B",
            hover_color="#475569",
            command=self.destroy,
        ).pack(side="right", padx=6, pady=10)

        self._popular_tabela()

    def _popular_tabela(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)

        for it in self.dados_nfe.get("itens") or []:
            idx = int(it["index"])
            target_pid = self.mapeamentos.get(idx)
            if target_pid is not None:
                if it.get("match_type") in ("ean", "nome") and target_pid == it.get("matched_produto_id"):
                    status_lbl = f"✅ Auto ({it['match_type'].upper()})"
                else:
                    status_lbl = "🔗 Manual"
                vinculo_lbl = self._pid_to_label.get(target_pid, f"Produto #{target_pid}")
            else:
                status_lbl = "⚠️ Novo"
                vinculo_lbl = "🆕 Criar Novo Produto"

            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    idx + 1,
                    it.get("c_ean") or "-",
                    it.get("x_prod", ""),
                    f"{it.get('qtd_int', 1)}",
                    f"R$ {float(it.get('v_un', 0)):.2f}",
                    status_lbl,
                    vinculo_lbl,
                ),
            )

    def _carregar_editor_item(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        item = next((i for i in (self.dados_nfe.get("itens") or []) if int(i["index"]) == idx), None)
        if not item:
            return
        self.lbl_item_sel.configure(
            text=f"Item #{idx + 1}: {item.get('x_prod', '')[:60]} (EAN: {item.get('c_ean') or 'S/N'})"
        )
        target_pid = self.mapeamentos.get(idx)
        self.combo_vinculo.set(self._pid_to_label.get(target_pid, "🆕 Criar Novo Produto"))

    def _aplicar_vinculo_item(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Seleção", "Selecione um item na tabela primeiro.", parent=self)
            return
        idx = int(sel[0])
        lbl = self.combo_vinculo.get()
        self.mapeamentos[idx] = self._catalogo_labels.get(lbl, None)
        self._popular_tabela()
        try:
            self.tree.selection_set(str(idx))
        except Exception:
            pass

    def _confirmar_importacao(self):
        try:
            margem = max(0.0, float(self.entry_margem.get().replace(",", ".")) / 100.0)
        except ValueError:
            margem = 0.35

        lancar_dups = bool(self.var_lancar_dups.get())
        self.btn_confirmar.configure(state="disabled", text="⏳ Importando NF-e...")

        def _worker():
            ok, resumo = self.db.aplicar_nfe(
                self.dados_nfe,
                mapeamentos=self.mapeamentos,
                margem_padrao=margem,
                lancar_conta_pagar=lancar_dups,
                usuario_id=getattr(self.db, "current_user_id", None),
            )

            def _done():
                if not self.winfo_exists():
                    return
                if ok:
                    self.refresh_dashboard()
                    if callable(self.on_imported_callback):
                        try:
                            self.on_imported_callback(resumo)
                        except Exception:
                            pass
                    msg = (
                        f"✅ NF-e #{resumo.get('numero_nfe') or 'S/N'} importada com sucesso!\n\n"
                        f"• Fornecedor: {resumo.get('fornecedor')}\n"
                        f"• Produtos novos cadastrados: {resumo.get('produtos_criados', 0)}\n"
                        f"• Produtos vinculados/atualizados: {resumo.get('produtos_atualizados', 0)}\n"
                        f"• Contas a pagar lançadas: {resumo.get('contas_lancadas', 0)}\n"
                        f"• Valor total da nota: R$ {resumo.get('valor_total', 0.0):.2f}"
                    )
                    messagebox.showinfo("Importação NF-e Concluída", msg, parent=self)
                    self.destroy()
                else:
                    self.btn_confirmar.configure(state="normal", text="✅ Confirmar e Importar NF-e")
                    messagebox.showerror("Erro ao Importar NF-e", str(resumo), parent=self)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()
