# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Views Financeiras (Onda 5)
Diálogos extraídos do main.py: Contas a Pagar, DRE Simplificado,
Projeção de Fluxo de Caixa Futuro e Controle de Comissões.
"""

from __future__ import annotations

import datetime as _dt
from tkinter import messagebox, ttk
from typing import Any

import customtkinter as ctk


def _format_currency_br(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class _FallbackDateEntry(ctk.CTkEntry):
    """Campo de data nativo compatível com DateEntry.get_date() quando tkcalendar não está instalado."""

    def __init__(self, master: Any, **kwargs: Any):
        super().__init__(master, placeholder_text="AAAA-MM-DD", height=35, **kwargs)
        self.insert(0, _dt.date.today().isoformat())

    def get_date(self) -> _dt.date:
        raw = self.get().strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return _dt.datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Data inválida: {raw!r}")


class FinanceiroView:
    """Agrupa as telas e diálogos do módulo financeiro e gerencial."""

    def __init__(self, app: Any, db: Any = None):
        self.app = app
        self.db = db if db is not None else getattr(app, "db", None)
        self.notifier = getattr(app, "notifier", None)

    def _new_dialog(self, title: str, geometry: str, focus_mode: str = "force") -> ctk.CTkToplevel:
        window = ctk.CTkToplevel(self.app)
        window.title(title)
        window.geometry(geometry)
        window.attributes("-topmost", True)
        window.grab_set()
        if focus_mode == "force":
            window.focus_force()
        else:
            window.focus_set()
        return window

    def open_contas_pagar_dialog(self) -> ctk.CTkToplevel:
        window = self._new_dialog("Contas a Pagar", "950x720", focus_mode="force")

        nav_frame = ctk.CTkFrame(window, fg_color="transparent", height=40)
        nav_frame.pack(fill="x", padx=15, pady=(10, 0))

        frame_lista = ctk.CTkFrame(window, fg_color=("white", "#2B2B2B"))
        frame_nova = ctk.CTkFrame(window, fg_color=("white", "#2B2B2B"))

        def _mostrar_lista():
            frame_nova.pack_forget()
            frame_lista.pack(fill="both", expand=True, padx=15, pady=(5, 15))
            btn_nav_lista.configure(fg_color="#0A4174")
            btn_nav_nova.configure(fg_color="#555555")

        def _mostrar_nova():
            frame_lista.pack_forget()
            frame_nova.pack(fill="both", expand=True, padx=15, pady=(5, 15))
            btn_nav_nova.configure(fg_color="#0A4174")
            btn_nav_lista.configure(fg_color="#555555")

        btn_nav_lista = ctk.CTkButton(
            nav_frame,
            text="Contas",
            command=_mostrar_lista,
            width=120,
            height=32,
            fg_color="#0A4174",
            hover_color="#062F56",
            font=("Aileron", 12, "bold"),
        )
        btn_nav_lista.pack(side="left", padx=(0, 5))

        btn_nav_nova = ctk.CTkButton(
            nav_frame,
            text="+ Nova Conta",
            command=_mostrar_nova,
            width=140,
            height=32,
            fg_color="#555555",
            hover_color="#062F56",
            font=("Aileron", 12, "bold"),
        )
        btn_nav_nova.pack(side="left", padx=5)

        filtro_frame = ctk.CTkFrame(frame_lista, fg_color="transparent")
        filtro_frame.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(filtro_frame, text="Filtro:", font=("Aileron", 11, "bold")).pack(side="left", padx=5)
        filtro_var = ctk.StringVar(value="Pendente")
        for txt in ["Pendente", "Pago", "Todos"]:
            ctk.CTkRadioButton(
                filtro_frame,
                text=txt,
                variable=filtro_var,
                value=txt,
                font=("Aileron", 11),
                command=lambda: _refresh_contas(),
            ).pack(side="left", padx=8)

        total_label = ctk.CTkLabel(
            filtro_frame,
            text="",
            font=("Aileron", 12, "bold"),
            text_color=("#E65100", "#FF9800"),
        )
        total_label.pack(side="right", padx=15)

        cols = ("id", "descricao", "valor", "vencimento", "fornecedor", "status", "parcela")
        tree = ttk.Treeview(frame_lista, columns=cols, show="headings", height=18)
        for c, h, w in [
            ("id", "ID", 40),
            ("descricao", "Descrição", 200),
            ("valor", "Valor", 80),
            ("vencimento", "Vencimento", 100),
            ("fornecedor", "Fornecedor", 150),
            ("status", "Status", 80),
            ("parcela", "Parcela", 70),
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=10, pady=5)

        btn_frame = ctk.CTkFrame(frame_lista, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)

        def _refresh_contas():
            for i in tree.get_children():
                tree.delete(i)
            filtro = filtro_var.get()
            status = filtro if filtro != "Todos" else None
            df = self.db.listar_contas_pagar(status=status)
            total_pend = 0
            for _, row in df.iterrows():
                parc_txt = f"{int(row.get('parcela_atual', 1))}/{int(row.get('total_parcelas', 1))}"
                venc = str(row.get("data_vencimento", ""))
                valor = row.get("valor", 0)
                st = row.get("status", "Pendente")
                if st == "Pendente":
                    total_pend += valor
                tree.insert(
                    "",
                    "end",
                    values=(
                        row["id"],
                        row.get("descricao", ""),
                        f"R$ {valor:.2f}",
                        venc,
                        row.get("fornecedor_nome", ""),
                        st,
                        parc_txt,
                    ),
                )
            total_label.configure(text=f"Total Pendente: R$ {total_pend:.2f}")

        def _pagar_selecionada():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("Alerta", "Selecione uma conta.", parent=window)
                return
            conta_id = tree.item(sel[0])["values"][0]
            if messagebox.askyesno("Confirmar", "Marcar esta conta como PAGA?", parent=window):
                self.db.pagar_conta(conta_id)
                _refresh_contas()
                if self.notifier:
                    self.notifier.notify("Conta Paga", "Conta quitada com sucesso!", tipo="success")

        def _cancelar_selecionada():
            sel = tree.selection()
            if not sel:
                return
            conta_id = tree.item(sel[0])["values"][0]
            if messagebox.askyesno("Confirmar", "Cancelar esta conta?", parent=window):
                self.db.cancelar_conta_pagar(conta_id)
                _refresh_contas()

        ctk.CTkButton(
            btn_frame,
            text="Pagar Selecionada",
            fg_color="#28a745",
            hover_color="#218838",
            text_color="white",
            font=("Aileron", 12, "bold"),
            height=38,
            command=_pagar_selecionada,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancelar Conta",
            fg_color="#dc3545",
            hover_color="#c82333",
            text_color="white",
            font=("Aileron", 12, "bold"),
            height=38,
            command=_cancelar_selecionada,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Atualizar",
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 12, "bold"),
            height=38,
            command=_refresh_contas,
        ).pack(side="right", padx=5)

        _refresh_contas()

        form = ctk.CTkFrame(frame_nova, fg_color="transparent")
        form.pack(padx=30, pady=20, fill="x")

        ctk.CTkLabel(
            form,
            text="Nova Conta a Pagar",
            font=("Aileron", 18, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 15))

        fields_frame = ctk.CTkFrame(form, fg_color="transparent")
        fields_frame.pack(fill="x")
        fields_frame.grid_columnconfigure(1, weight=1)

        entries = {}

        ctk.CTkLabel(fields_frame, text="Descrição:", font=("Aileron", 11)).grid(row=0, column=0, sticky="w", padx=5, pady=5)
        entries["desc"] = ctk.CTkEntry(fields_frame, height=35)
        entries["desc"].grid(row=0, column=1, sticky="ew", padx=5, pady=5)

        ctk.CTkLabel(fields_frame, text="Valor (R$):", font=("Aileron", 11)).grid(row=1, column=0, sticky="w", padx=5, pady=5)
        entries["valor"] = ctk.CTkEntry(fields_frame, height=35)
        entries["valor"].grid(row=1, column=1, sticky="ew", padx=5, pady=5)

        ctk.CTkLabel(fields_frame, text="Vencimento:", font=("Aileron", 11)).grid(row=2, column=0, sticky="w", padx=5, pady=5)
        venc_frame = ctk.CTkFrame(fields_frame, fg_color="transparent")
        venc_frame.grid(row=2, column=1, sticky="ew", padx=5, pady=5)
        venc_frame.grid_columnconfigure(0, weight=1)

        try:
            from tkcalendar import DateEntry

            try:
                date_entry = DateEntry(
                    venc_frame,
                    width=20,
                    background="#0A4174",
                    foreground="white",
                    borderwidth=2,
                    date_pattern="yyyy-mm-dd",
                    font=("Aileron", 11),
                    mindate=_dt.date.today(),
                    locale="pt_BR",
                )
            except Exception:
                date_entry = DateEntry(
                    venc_frame,
                    width=20,
                    background="#0A4174",
                    foreground="white",
                    borderwidth=2,
                    date_pattern="yyyy-mm-dd",
                    font=("Aileron", 11),
                    mindate=_dt.date.today(),
                )
        except ImportError:
            date_entry = _FallbackDateEntry(venc_frame)
        date_entry.grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(fields_frame, text="Categoria:", font=("Aileron", 11)).grid(row=3, column=0, sticky="w", padx=5, pady=5)
        entries["cat"] = ctk.CTkEntry(fields_frame, height=35)
        entries["cat"].grid(row=3, column=1, sticky="ew", padx=5, pady=5)
        entries["cat"].insert(0, "Outro")

        ctk.CTkLabel(fields_frame, text="Parcelas:", font=("Aileron", 11)).grid(row=4, column=0, sticky="w", padx=5, pady=5)
        entries["parcelas"] = ctk.CTkEntry(fields_frame, height=35)
        entries["parcelas"].grid(row=4, column=1, sticky="ew", padx=5, pady=5)
        entries["parcelas"].insert(0, "1")

        ctk.CTkLabel(fields_frame, text="Observação:", font=("Aileron", 11)).grid(row=5, column=0, sticky="w", padx=5, pady=5)
        entries["obs"] = ctk.CTkEntry(fields_frame, height=35)
        entries["obs"].grid(row=5, column=1, sticky="ew", padx=5, pady=5)

        ctk.CTkLabel(fields_frame, text="Fornecedor:", font=("Aileron", 11)).grid(row=6, column=0, sticky="w", padx=5, pady=5)
        df_forn = self.db.listar_fornecedores()
        forn_list = ["Nenhum"] + df_forn["nome"].tolist() if not df_forn.empty else ["Nenhum"]
        forn_var = ctk.StringVar(value="Nenhum")
        ctk.CTkOptionMenu(fields_frame, values=forn_list, variable=forn_var, height=35).grid(
            row=6, column=1, sticky="ew", padx=5, pady=5
        )

        def _salvar_conta():
            desc = entries["desc"].get().strip()
            try:
                valor = float(entries["valor"].get().replace(",", "."))
            except ValueError:
                messagebox.showerror("Erro", "Valor inválido.", parent=window)
                return
            try:
                venc = date_entry.get_date().isoformat()
            except ValueError:
                messagebox.showerror("Erro", "Data de vencimento inválida (use AAAA-MM-DD).", parent=window)
                return
            if not desc:
                messagebox.showerror("Erro", "Preencha a descrição.", parent=window)
                return
            cat = entries["cat"].get().strip() or "Outro"
            try:
                parcelas = int(entries["parcelas"].get())
            except ValueError:
                parcelas = 1
            obs = entries["obs"].get().strip()

            forn_id = None
            if forn_var.get() != "Nenhum" and not df_forn.empty:
                forn_match = df_forn[df_forn["nome"] == forn_var.get()]
                if not forn_match.empty:
                    forn_id = int(forn_match.iloc[0]["id"])

            self.db.adicionar_conta_pagar(desc, valor, venc, forn_id, cat, total_parcelas=parcelas, observacao=obs)
            messagebox.showinfo("Sucesso", "Conta registrada!", parent=window)
            entries["desc"].delete(0, "end")
            entries["valor"].delete(0, "end")
            entries["obs"].delete(0, "end")
            entries["parcelas"].delete(0, "end")
            entries["parcelas"].insert(0, "1")
            entries["cat"].delete(0, "end")
            entries["cat"].insert(0, "Outro")
            _refresh_contas()
            _mostrar_lista()

        ctk.CTkButton(
            form,
            text="Salvar Conta",
            height=42,
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 13, "bold"),
            command=_salvar_conta,
        ).pack(pady=20, fill="x")

        _mostrar_lista()
        return window

    def open_dre_dialog(self) -> ctk.CTkToplevel:
        window = self._new_dialog("DRE - Demonstrativo de Resultado", "800x750", focus_mode="set")

        main_f = ctk.CTkScrollableFrame(window, fg_color="transparent")
        main_f.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(
            main_f,
            text="Demonstrativo de Resultado do Exercício",
            font=("Aileron", 20, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 5))

        periodo_frame = ctk.CTkFrame(main_f, fg_color="transparent")
        periodo_frame.pack(fill="x", pady=(0, 15))

        periodo_var = ctk.StringVar(value="mes")
        for txt, val in [("Mês Atual", "mes"), ("Trimestre", "trimestre"), ("Ano", "ano")]:
            ctk.CTkRadioButton(
                periodo_frame,
                text=txt,
                variable=periodo_var,
                value=val,
                font=("Aileron", 11),
                command=lambda: _render_dre(),
            ).pack(side="left", padx=10)

        dre_container = ctk.CTkFrame(
            main_f,
            fg_color=("white", "#2B2B2B"),
            corner_radius=15,
            border_width=1,
            border_color=("#E6E6E6", "#444444"),
        )
        dre_container.pack(fill="x", padx=5, pady=5)

        def _render_dre():
            for w in dre_container.winfo_children():
                w.destroy()

            dre = self.db.gerar_dre(periodo_var.get())

            periodo_label = {"mes": "Mês Atual", "trimestre": "Trimestre Atual", "ano": "Ano Atual"}.get(
                dre["periodo"], "Período"
            )
            ctk.CTkLabel(
                dre_container,
                text=f"Período: {periodo_label} ({dre['inicio']} a {dre['fim']})",
                font=("Aileron", 11),
                text_color=("gray40", "gray70"),
            ).pack(anchor="w", padx=20, pady=(15, 10))

            def _linha(parent, texto, valor, bold=False, color=None, indent=0):
                f = ctk.CTkFrame(parent, fg_color="transparent")
                f.pack(fill="x", padx=20 + indent, pady=2)
                font = ("Aileron", 12, "bold") if bold else ("Aileron", 11)
                tc = color or ("#001D39", "white")
                ctk.CTkLabel(f, text=texto, font=font, text_color=tc).pack(side="left")
                val_color = color or ("#001D39", "white")
                if isinstance(valor, (int, float)):
                    val_text = _format_currency_br(valor)
                    if valor < 0:
                        val_color = "#dc3545"
                else:
                    val_text = str(valor)
                ctk.CTkLabel(f, text=val_text, font=font, text_color=val_color).pack(side="right")

            def _separador(parent):
                ctk.CTkFrame(parent, fg_color=("#E0E0E0", "#444444"), height=1).pack(fill="x", padx=20, pady=6)

            _linha(dre_container, "RECEITA BRUTA", dre["receita_bruta"], bold=True)
            _linha(dre_container, "  Vendas de Produtos", dre["receita_vendas"], indent=15)
            _linha(dre_container, "  Serviços Prestados", dre["receita_servicos"], indent=15)
            _separador(dre_container)

            _linha(dre_container, "(-) Deduções / Descontos", -dre["deducoes"])
            _separador(dre_container)

            _linha(dre_container, "RECEITA LÍQUIDA", dre["receita_liquida"], bold=True, color="#0D47A1")
            _separador(dre_container)

            _linha(dre_container, "(-) CMV (Custo das Mercadorias)", -dre["cmv"])
            _linha(dre_container, "(-) Custo dos Serviços", -dre["custo_servicos"])
            _separador(dre_container)

            cor_lb = "#28a745" if dre["lucro_bruto"] >= 0 else "#dc3545"
            _linha(dre_container, "LUCRO BRUTO", dre["lucro_bruto"], bold=True, color=cor_lb)
            _linha(dre_container, f"  Margem Bruta: {dre['margem_bruta']:.1f}%", "", indent=15)
            _separador(dre_container)

            _linha(dre_container, "(-) Despesas Fixas", -dre["despesas_fixas"])
            _linha(dre_container, "(-) Despesas Variáveis", -dre["despesas_variaveis"])
            _separador(dre_container)

            cor_ro = "#28a745" if dre["resultado_operacional"] >= 0 else "#dc3545"
            _linha(dre_container, "RESULTADO OPERACIONAL", dre["resultado_operacional"], bold=True, color=cor_ro)
            _separador(dre_container)

            cor_rl = "#28a745" if dre["resultado_liquido"] >= 0 else "#dc3545"
            _linha(dre_container, "RESULTADO LÍQUIDO", dre["resultado_liquido"], bold=True, color=cor_rl)
            _linha(dre_container, f"  Margem Líquida: {dre['margem_liquida']:.1f}%", "", indent=15)

            ctk.CTkFrame(dre_container, fg_color="transparent", height=15).pack()

        _render_dre()

        btn_frame = ctk.CTkFrame(main_f, fg_color="transparent")
        btn_frame.pack(fill="x", padx=5, pady=10)

        ctk.CTkButton(
            btn_frame,
            text="Ver Fluxo de Caixa Futuro",
            height=40,
            fg_color="#2874A6",
            hover_color="#1B4F72",
            text_color="white",
            font=("Aileron", 12, "bold"),
            command=lambda: self.open_fluxo_caixa_dialog(),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            btn_frame,
            text="Ver Comissões",
            height=40,
            fg_color="#5DADE2",
            hover_color="#3498DB",
            text_color="#001D39",
            font=("Aileron", 12, "bold"),
            command=lambda: self.open_comissoes_dialog(),
        ).pack(side="left", padx=5)
        return window

    def open_fluxo_caixa_dialog(self) -> ctk.CTkToplevel:
        window = self._new_dialog("Fluxo de Caixa Futuro", "900x650", focus_mode="set")

        main_f = ctk.CTkFrame(window, fg_color="transparent")
        main_f.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(
            main_f,
            text="Projeção de Fluxo de Caixa",
            font=("Aileron", 18, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 5))

        periodo_frame = ctk.CTkFrame(main_f, fg_color="transparent")
        periodo_frame.pack(fill="x", pady=(0, 10))

        dias_var = ctk.StringVar(value="90")
        ctk.CTkLabel(periodo_frame, text="Projetar:", font=("Aileron", 11)).pack(side="left", padx=5)
        for txt, val in [("30 dias", "30"), ("60 dias", "60"), ("90 dias", "90")]:
            ctk.CTkRadioButton(
                periodo_frame,
                text=txt,
                variable=dias_var,
                value=val,
                font=("Aileron", 11),
                command=lambda: _render_fluxo(),
            ).pack(side="left", padx=8)

        saldo_card = ctk.CTkFrame(main_f, fg_color=("#E3F2FD", "#1A2A3A"), corner_radius=12)
        saldo_card.pack(fill="x", padx=5, pady=(0, 10))
        saldo_label = ctk.CTkLabel(
            saldo_card,
            text="",
            font=("Aileron", 16, "bold"),
            text_color=("#0D47A1", "#4da6ff"),
        )
        saldo_label.pack(pady=12)

        cols = ("data", "tipo", "descricao", "valor", "saldo")
        tree = ttk.Treeview(main_f, columns=cols, show="headings", height=16)
        for c, h, w in [
            ("data", "Data", 100),
            ("tipo", "Tipo", 120),
            ("descricao", "Descrição", 280),
            ("valor", "Valor", 100),
            ("saldo", "Saldo Projetado", 120),
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=5, pady=5)

        def _render_fluxo():
            for i in tree.get_children():
                tree.delete(i)
            dias = int(dias_var.get())
            dados = self.db.projetar_fluxo_caixa(dias)
            saldo_label.configure(
                text=f"Saldo Atual: R$ {dados['saldo_atual']:,.2f}  |  "
                f"Média Diária Vendas: R$ {dados['media_diaria_vendas']:,.2f}"
            )

            tipo_labels = {
                "saida": "Conta a Pagar",
                "saida_fixa": "Gasto Fixo",
                "entrada": "Recebível",
                "projecao_vendas": "Projeção Vendas",
            }

            for item in dados["projecao"]:
                valor = item["valor"]
                saldo = item["saldo_acumulado"]
                tree.insert(
                    "",
                    "end",
                    values=(
                        item["data"],
                        tipo_labels.get(item["tipo"], item["tipo"]),
                        item["descricao"],
                        f"R$ {valor:+,.2f}",
                        f"R$ {saldo:,.2f}",
                    ),
                )

        _render_fluxo()
        return window

    def open_comissoes_dialog(self) -> ctk.CTkToplevel:
        window = self._new_dialog("Controle de Comissões", "800x550", focus_mode="set")

        main_f = ctk.CTkFrame(window, fg_color="transparent")
        main_f.pack(fill="both", expand=True, padx=15, pady=15)

        ctk.CTkLabel(
            main_f,
            text="Relatório de Comissões",
            font=("Aileron", 18, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(0, 10))

        periodo_frame = ctk.CTkFrame(main_f, fg_color="transparent")
        periodo_frame.pack(fill="x", pady=(0, 10))

        periodo_var = ctk.StringVar(value="mes")
        for txt, val in [("Semana", "semana"), ("Mês", "mes"), ("Ano", "ano")]:
            ctk.CTkRadioButton(
                periodo_frame,
                text=txt,
                variable=periodo_var,
                value=val,
                font=("Aileron", 11),
                command=lambda: _render_comissoes(),
            ).pack(side="left", padx=10)

        total_label = ctk.CTkLabel(
            main_f,
            text="",
            font=("Aileron", 14, "bold"),
            text_color=("#E65100", "#FF9800"),
        )
        total_label.pack(pady=5)

        cols = ("vendedor", "taxa", "vendas", "faturamento", "lucro", "comissao")
        tree = ttk.Treeview(main_f, columns=cols, show="headings", height=12)
        for c, h, w in [
            ("vendedor", "Vendedor", 150),
            ("taxa", "Taxa %", 70),
            ("vendas", "Nº Vendas", 80),
            ("faturamento", "Faturamento", 120),
            ("lucro", "Lucro", 100),
            ("comissao", "Comissão", 100),
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, padx=5, pady=5)

        def _render_comissoes():
            for i in tree.get_children():
                tree.delete(i)
            dados = self.db.calcular_comissoes(periodo_var.get())
            df = dados["vendas"]
            total_com = 0
            for _, row in df.iterrows():
                com = row.get("comissao_vendas", 0) or 0
                total_com += com
                tree.insert(
                    "",
                    "end",
                    values=(
                        row.get("vendedor", ""),
                        f"{row.get('taxa_comissao', 0):.1f}%",
                        int(row.get("num_vendas", 0)),
                        f"R$ {row.get('faturamento', 0):,.2f}",
                        f"R$ {row.get('lucro', 0):,.2f}",
                        f"R$ {com:,.2f}",
                    ),
                )
            total_label.configure(text=f"Total Comissões a Pagar: R$ {total_com:,.2f}")

        _render_comissoes()
        return window


def open_contas_pagar_dialog(app: Any) -> ctk.CTkToplevel:
    return FinanceiroView(app).open_contas_pagar_dialog()


def open_dre_dialog(app: Any) -> ctk.CTkToplevel:
    return FinanceiroView(app).open_dre_dialog()


def open_fluxo_caixa_dialog(app: Any) -> ctk.CTkToplevel:
    return FinanceiroView(app).open_fluxo_caixa_dialog()


def open_comissoes_dialog(app: Any) -> ctk.CTkToplevel:
    return FinanceiroView(app).open_comissoes_dialog()
