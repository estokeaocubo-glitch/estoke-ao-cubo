# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Views Administrativas (Onda 5)
Diálogos extraídos do main.py: Gerador de Etiquetas, Gestão de Lojas/Filiais
e Gestão de Usuários e Perfis.
"""

from __future__ import annotations

import os
from tkinter import messagebox, ttk
from typing import Any

import customtkinter as ctk


class AdminView:
    """Agrupa os diálogos operacionais e administrativos do aplicativo."""

    def __init__(self, app: Any, db: Any = None):
        self.app = app
        self.db = db if db is not None else getattr(app, "db", None)
        self.notifier = getattr(app, "notifier", None)

    def _new_dialog(self, title: str, geometry: str) -> ctk.CTkToplevel:
        window = ctk.CTkToplevel(self.app)
        window.title(title)
        window.geometry(geometry)
        window.attributes("-topmost", True)
        window.grab_set()
        window.focus_set()
        return window

    def open_etiquetas_dialog(self) -> ctk.CTkToplevel:
        import label_generator

        window = self._new_dialog("Gerador de Etiquetas", "700x600")

        main_f = ctk.CTkFrame(window, fg_color="transparent")
        main_f.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(
            main_f,
            text="Gerador de Etiquetas",
            font=("Aileron", 18, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 10))

        if not label_generator.etiquetas_disponiveis():
            ctk.CTkLabel(
                main_f,
                text="Bibliotecas de imagem não disponíveis.\nInstale: pip install pillow python-barcode qrcode",
                font=("Aileron", 12),
                text_color="red",
            ).pack(pady=30)
            return window

        cfg_frame = ctk.CTkFrame(main_f, fg_color=("white", "#2B2B2B"), corner_radius=10)
        cfg_frame.pack(fill="x", pady=(0, 10), padx=5)

        ctk.CTkLabel(cfg_frame, text="Configurações:", font=("Aileron", 12, "bold")).pack(
            anchor="w", padx=15, pady=(10, 5)
        )

        opts_frame = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        opts_frame.pack(fill="x", padx=15, pady=(0, 10))

        var_preco = ctk.BooleanVar(value=True)
        var_codigo = ctk.BooleanVar(value=True)
        var_qr = ctk.BooleanVar(value=False)

        ctk.CTkCheckBox(opts_frame, text="Mostrar preço", variable=var_preco, font=("Aileron", 11)).pack(
            side="left", padx=10
        )
        ctk.CTkCheckBox(opts_frame, text="Código de barras", variable=var_codigo, font=("Aileron", 11)).pack(
            side="left", padx=10
        )
        ctk.CTkCheckBox(opts_frame, text="QR Code", variable=var_qr, font=("Aileron", 11)).pack(
            side="left", padx=10
        )

        ctk.CTkLabel(main_f, text="Selecione produtos para gerar etiquetas:", font=("Aileron", 11)).pack(
            anchor="w", padx=5
        )

        cols = ("sel", "id", "nome", "preco", "codigo")
        tree = ttk.Treeview(main_f, columns=cols, show="headings", height=12)
        for c, h, w in [
            ("sel", "☑", 30),
            ("id", "ID", 40),
            ("nome", "Produto", 250),
            ("preco", "Preço", 80),
            ("codigo", "Código", 150),
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=5, pady=5)

        df = self.db.listar_produtos()
        selected_ids = set()

        def _toggle(event):
            item = tree.identify_row(event.y)
            if item:
                pid = tree.item(item)["values"][1]
                if pid in selected_ids:
                    selected_ids.discard(pid)
                    tree.item(item, values=("☐",) + tuple(tree.item(item)["values"][1:]))
                else:
                    selected_ids.add(pid)
                    tree.item(item, values=("☑",) + tuple(tree.item(item)["values"][1:]))

        tree.bind("<Button-1>", _toggle)

        for _, row in df.iterrows():
            tree.insert(
                "",
                "end",
                values=("☐", row["id"], row["nome"], f"R$ {row['preco']:.2f}", row.get("codigo_barras", "")),
            )

        qty_frame = ctk.CTkFrame(main_f, fg_color="transparent")
        qty_frame.pack(fill="x", padx=5, pady=5)
        ctk.CTkLabel(qty_frame, text="Cópias por produto:", font=("Aileron", 11)).pack(side="left")
        e_qty = ctk.CTkEntry(qty_frame, width=60, height=32)
        e_qty.insert(0, "1")
        e_qty.pack(side="left", padx=5)

        def _gerar():
            if not selected_ids:
                messagebox.showwarning("Alerta", "Selecione ao menos um produto.", parent=window)
                return
            try:
                qty = int(e_qty.get())
            except ValueError:
                qty = 1

            config_nota = self.db.get_config_nota()
            cfg = {
                "mostrar_preco": var_preco.get(),
                "mostrar_codigo": var_codigo.get(),
                "mostrar_qrcode": var_qr.get(),
                "empresa_nome": config_nota.get("empresa_nome", ""),
            }

            gerados = 0
            output_dir = os.path.join(os.path.expanduser("~"), "Documents", "EstokeAoCubo_Etiquetas")
            for pid in selected_ids:
                prod_row = df[df["id"] == pid]
                if prod_row.empty:
                    continue
                prod = prod_row.iloc[0].to_dict()
                for _ in range(qty):
                    path = label_generator.gerar_etiqueta(prod, cfg, output_dir)
                    if path:
                        gerados += 1

            if gerados > 0:
                messagebox.showinfo("Sucesso", f"{gerados} etiqueta(s) gerada(s) em:\n{output_dir}", parent=window)
                try:
                    os.startfile(output_dir)
                except Exception:
                    pass
            else:
                messagebox.showerror("Erro", "Nenhuma etiqueta gerada.", parent=window)

        ctk.CTkButton(
            main_f,
            text="Gerar Etiquetas",
            height=42,
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 13, "bold"),
            command=_gerar,
        ).pack(pady=10, fill="x", padx=5)
        return window

    def open_lojas_dialog(self) -> ctk.CTkToplevel:
        window = self._new_dialog("Gestão de Lojas/Filiais", "650x500")

        main_f = ctk.CTkFrame(window, fg_color="transparent")
        main_f.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(
            main_f,
            text="Lojas / Filiais",
            font=("Aileron", 18, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 10))

        cols = ("id", "nome", "endereco", "telefone", "cnpj")
        tree = ttk.Treeview(main_f, columns=cols, show="headings", height=10)
        for c, h, w in [
            ("id", "ID", 40),
            ("nome", "Nome", 180),
            ("endereco", "Endereço", 200),
            ("telefone", "Telefone", 120),
            ("cnpj", "CNPJ", 140),
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=5, pady=5)

        def _refresh():
            for i in tree.get_children():
                tree.delete(i)
            df = self.db.listar_lojas()
            for _, row in df.iterrows():
                tree.insert(
                    "",
                    "end",
                    values=(row["id"], row["nome"], row.get("endereco", ""), row.get("telefone", ""), row.get("cnpj", "")),
                )

        form = ctk.CTkFrame(main_f, fg_color=("white", "#2B2B2B"), corner_radius=10)
        form.pack(fill="x", padx=5, pady=10)

        entries = {}
        row_f = ctk.CTkFrame(form, fg_color="transparent")
        row_f.pack(fill="x", padx=15, pady=10)

        for i, (label, key, w) in enumerate(
            [("Nome", "nome", 150), ("Endereço", "end", 180), ("Telefone", "tel", 100), ("CNPJ", "cnpj", 130)]
        ):
            ctk.CTkLabel(row_f, text=label + ":", font=("Aileron", 10)).grid(row=0, column=i * 2, padx=2)
            e = ctk.CTkEntry(row_f, width=w, height=32)
            e.grid(row=0, column=i * 2 + 1, padx=2)
            entries[key] = e

        btn_f = ctk.CTkFrame(form, fg_color="transparent")
        btn_f.pack(fill="x", padx=15, pady=(0, 10))

        def _add():
            nome = entries["nome"].get().strip()
            if not nome:
                messagebox.showerror("Erro", "Nome obrigatório.", parent=window)
                return
            self.db.adicionar_loja(
                nome,
                entries["end"].get().strip(),
                entries["tel"].get().strip(),
                entries["cnpj"].get().strip(),
            )
            for e in entries.values():
                e.delete(0, "end")
            _refresh()

        def _remove():
            sel = tree.selection()
            if not sel:
                return
            lid = tree.item(sel[0])["values"][0]
            if messagebox.askyesno("Confirmar", "Desativar esta loja?", parent=window):
                self.db.remover_loja(lid)
                _refresh()

        ctk.CTkButton(
            btn_f,
            text="Adicionar",
            fg_color="#28a745",
            hover_color="#218838",
            text_color="white",
            font=("Aileron", 11, "bold"),
            height=32,
            command=_add,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_f,
            text="Remover",
            fg_color="#dc3545",
            hover_color="#c82333",
            text_color="white",
            font=("Aileron", 11, "bold"),
            height=32,
            command=_remove,
        ).pack(side="left", padx=5)

        _refresh()
        return window

    def open_usuarios_dialog(self) -> ctk.CTkToplevel:
        window = self._new_dialog("Gestão de Usuários", "800x600")

        tabview = ctk.CTkTabview(window, fg_color=("white", "#2B2B2B"))
        tabview.pack(fill="both", expand=True, padx=15, pady=15)

        tab_lista = tabview.add("Usuários")
        tab_novo = tabview.add("Novo Usuário")

        cols = ("id", "nome", "login", "perfil", "ativo", "loja")
        tree = ttk.Treeview(tab_lista, columns=cols, show="headings", height=15)
        for c, h, w in [
            ("id", "ID", 40),
            ("nome", "Nome", 180),
            ("login", "Login", 120),
            ("perfil", "Perfil", 100),
            ("ativo", "Ativo", 60),
            ("loja", "Loja", 80),
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        def _refresh():
            for i in tree.get_children():
                tree.delete(i)
            df = self.db.listar_usuarios()
            for _, row in df.iterrows():
                tree.insert(
                    "",
                    "end",
                    values=(
                        row["id"],
                        row["nome"],
                        row["login"],
                        row["perfil"],
                        "Sim" if row["ativo"] else "Não",
                        row.get("loja_id", "") or "-",
                    ),
                )

        btn_frame = ctk.CTkFrame(tab_lista, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)

        def _desativar():
            sel = tree.selection()
            if not sel:
                return
            uid = tree.item(sel[0])["values"][0]
            if messagebox.askyesno("Confirmar", "Desativar este usuário?", parent=window):
                self.db.remover_usuario(uid)
                _refresh()

        def _alterar_senha():
            sel = tree.selection()
            if not sel:
                return
            uid = tree.item(sel[0])["values"][0]
            nome = tree.item(sel[0])["values"][1]

            dlg = ctk.CTkToplevel(window)
            dlg.title(f"Alterar Senha - {nome}")
            dlg.geometry("350x200")
            dlg.attributes("-topmost", True)
            dlg.grab_set()

            ctk.CTkLabel(dlg, text="Nova Senha:", font=("Aileron", 12)).pack(padx=20, pady=(20, 5))
            e_pw = ctk.CTkEntry(dlg, show="*", height=35)
            e_pw.pack(padx=20, fill="x")

            def _save():
                pw = e_pw.get().strip()
                if len(pw) < 4:
                    messagebox.showerror("Erro", "Senha deve ter 4+ caracteres.", parent=dlg)
                    return
                self.db.alterar_senha_usuario(uid, pw)
                messagebox.showinfo("Sucesso", "Senha alterada!", parent=dlg)
                dlg.destroy()

            ctk.CTkButton(
                dlg, text="Salvar", height=38, fg_color="#7BBDE8", hover_color="#49769F", command=_save
            ).pack(padx=20, pady=20, fill="x")

        ctk.CTkButton(
            btn_frame,
            text="Desativar",
            fg_color="#dc3545",
            hover_color="#c82333",
            text_color="white",
            font=("Aileron", 11, "bold"),
            height=35,
            command=_desativar,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Alterar Senha",
            fg_color="#5DADE2",
            hover_color="#3498DB",
            text_color="#001D39",
            font=("Aileron", 11, "bold"),
            height=35,
            command=_alterar_senha,
        ).pack(side="left", padx=5)

        _refresh()

        form = ctk.CTkFrame(tab_novo, fg_color="transparent")
        form.pack(padx=30, pady=20, fill="both", expand=True)

        ctk.CTkLabel(
            form,
            text="Cadastrar Novo Usuário",
            font=("Aileron", 18, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 15))

        fields_frame = ctk.CTkFrame(form, fg_color="transparent")
        fields_frame.pack(fill="x")
        fields_frame.grid_columnconfigure(1, weight=1)

        entries_u = {}
        for i, (label, key) in enumerate([("Nome Completo", "nome"), ("Login", "login"), ("Senha", "senha")]):
            ctk.CTkLabel(fields_frame, text=label + ":", font=("Aileron", 11)).grid(
                row=i, column=0, sticky="w", padx=5, pady=8
            )
            show = "*" if key == "senha" else ""
            e = ctk.CTkEntry(fields_frame, height=35, show=show)
            e.grid(row=i, column=1, sticky="ew", padx=5, pady=8)
            entries_u[key] = e

        ctk.CTkLabel(fields_frame, text="Perfil:", font=("Aileron", 11)).grid(
            row=3, column=0, sticky="w", padx=5, pady=8
        )
        perfil_var = ctk.StringVar(value="vendedor")
        ctk.CTkOptionMenu(
            fields_frame, values=["admin", "gerente", "vendedor", "caixa"], variable=perfil_var, height=35
        ).grid(row=3, column=1, sticky="ew", padx=5, pady=8)

        ctk.CTkLabel(fields_frame, text="Loja:", font=("Aileron", 11)).grid(
            row=4, column=0, sticky="w", padx=5, pady=8
        )
        df_lojas = self.db.listar_lojas()
        loja_nomes = ["Todas"] + df_lojas["nome"].tolist() if not df_lojas.empty else ["Todas"]
        loja_var = ctk.StringVar(value="Todas")
        ctk.CTkOptionMenu(fields_frame, values=loja_nomes, variable=loja_var, height=35).grid(
            row=4, column=1, sticky="ew", padx=5, pady=8
        )

        perm_frame = ctk.CTkFrame(form, fg_color=("white", "#2B2B2B"), corner_radius=10)
        perm_frame.pack(fill="x", pady=10)
        ctk.CTkLabel(perm_frame, text="Permissões por Perfil:", font=("Aileron", 11, "bold")).pack(
            padx=15, pady=(10, 5), anchor="w"
        )
        perm_text = (
            "Admin: Acesso total\n"
            "Gerente: Relatórios, configurações, vendas, estoque, financeiro\n"
            "Vendedor: Vendas, produtos, clientes, kits, etiquetas, chat IA\n"
            "Caixa: Vendas, caixa, clientes"
        )
        ctk.CTkLabel(
            perm_frame, text=perm_text, font=("Aileron", 10), text_color=("gray40", "gray70"), justify="left"
        ).pack(padx=15, pady=(0, 10), anchor="w")

        def _criar_usuario():
            nome = entries_u["nome"].get().strip()
            login = entries_u["login"].get().strip()
            senha = entries_u["senha"].get().strip()
            if not nome or not login or not senha:
                messagebox.showerror("Erro", "Preencha todos os campos.", parent=window)
                return
            if len(senha) < 4:
                messagebox.showerror("Erro", "Senha deve ter 4+ caracteres.", parent=window)
                return
            perfil = perfil_var.get()

            loja_id = None
            if loja_var.get() != "Todas" and not df_lojas.empty:
                match = df_lojas[df_lojas["nome"] == loja_var.get()]
                if not match.empty:
                    loja_id = int(match.iloc[0]["id"])

            ok, msg = self.db.adicionar_usuario(nome, login, senha, perfil, loja_id)
            if ok:
                messagebox.showinfo("Sucesso", msg, parent=window)
                for e in entries_u.values():
                    e.delete(0, "end")
                _refresh()
            else:
                messagebox.showerror("Erro", msg, parent=window)

        ctk.CTkButton(
            form,
            text="Criar Usuário",
            height=42,
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 13, "bold"),
            command=_criar_usuario,
        ).pack(pady=10, fill="x")
        return window


def open_etiquetas_dialog(app: Any) -> ctk.CTkToplevel:
    return AdminView(app).open_etiquetas_dialog()


def open_lojas_dialog(app: Any) -> ctk.CTkToplevel:
    return AdminView(app).open_lojas_dialog()


def open_usuarios_dialog(app: Any) -> ctk.CTkToplevel:
    return AdminView(app).open_usuarios_dialog()
