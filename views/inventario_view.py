# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Tela de Inventário & Contagem Cega (Onda 3)
Permite leitura contínua por código de barras (com suporte a qtd*código),
ocultando o saldo do sistema até a conferência final e aplicando ajustes com supervisor + auditoria.
"""

from __future__ import annotations

import csv
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from services.venda_service import parse_scan_input
from views.base import BaseDialog
from views.supervisor_dialog import solicitar_autorizacao_supervisor


class InventarioDialog(BaseDialog):
    """Diálogo operacional para sessões de inventário físico e contagem cega."""

    def __init__(self, app, parent=None):
        self._parent_win = parent
        self.sessao_atual_id: int | None = None
        self.modo_cego: bool = True
        self.sessao_status: str = "aberto"
        super().__init__(app, title="Inventário Físico & Contagem Cega", geometry="980x640", topmost=True)

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

        # Topo / Sessão
        header = self.create_card(self)
        header.pack(fill="x", padx=14, pady=(12, 8))

        self.create_title_label(header, text="📋 Inventário & Contagem Cega").pack(side="left", padx=14, pady=12)

        self.combo_sessao = ctk.CTkComboBox(
            header,
            values=["Nenhuma sessão"],
            width=290,
            state="readonly",
            command=lambda _v: self._ao_trocar_sessao(),
        )
        self.combo_sessao.pack(side="left", padx=(6, 8), pady=10)

        ctk.CTkButton(
            header,
            text="➕ Nova Contagem",
            width=145,
            height=34,
            fg_color="#0A4174",
            hover_color="#062F56",
            command=self._nova_sessao,
        ).pack(side="left", padx=4)

        self.lbl_status_badge = ctk.CTkLabel(
            header,
            text="🔒 Modo Cego Ativo",
            font=("Segoe UI", 12, "bold"),
            text_color="#D97706",
        )
        self.lbl_status_badge.pack(side="right", padx=14)

        # Barra de Bipe Contínuo
        scan_card = self.create_card(self)
        scan_card.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(
            scan_card,
            text="Leitura (código, nome ou qtd*código):",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=(14, 8), pady=10)

        self.entry_scan = ctk.CTkEntry(
            scan_card,
            placeholder_text="Bipe o código de barras ou digite ex: 5*7891234567895",
            width=360,
            height=36,
        )
        self.entry_scan.pack(side="left", padx=4, pady=10)
        self.entry_scan.bind("<Return>", lambda _e: self._registrar_bipe())

        self.var_somar = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            scan_card,
            text="Somar leituras repetidas",
            variable=self.var_somar,
            font=("Segoe UI", 12),
        ).pack(side="left", padx=12)

        ctk.CTkButton(
            scan_card,
            text="Registrar",
            width=110,
            height=36,
            fg_color="#059669",
            hover_color="#047857",
            command=self._registrar_bipe,
        ).pack(side="left", padx=(4, 12))

        self.lbl_feedback = ctk.CTkLabel(scan_card, text="", font=("Segoe UI", 11))
        self.lbl_feedback.pack(side="right", padx=14)

        # Tabela de Itens Contados
        tree_frame = ctk.CTkFrame(self, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=14, pady=4)

        cols = ("codigo", "produto", "contada", "sistema", "divergencia", "impacto", "hora")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=15)
        specs = [
            ("codigo", "Código/EAN", 130),
            ("produto", "Produto", 270),
            ("contada", "Qtd Contada", 95),
            ("sistema", "Qtd Sistema", 95),
            ("divergencia", "Divergência", 95),
            ("impacto", "Impacto (R$)", 105),
            ("hora", "Última Leitura", 135),
        ]
        for col, head, width in specs:
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, anchor="center" if col not in ("produto",) else "w")

        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        # Rodapé / Resumo e Ações
        footer = self.create_card(self)
        footer.pack(fill="x", padx=14, pady=(8, 12))

        self.lbl_resumo = ctk.CTkLabel(footer, text="Itens contados: 0", font=("Segoe UI", 12, "bold"))
        self.lbl_resumo.pack(side="left", padx=14, pady=12)

        self.btn_aplicar = ctk.CTkButton(
            footer,
            text="✅ Aplicar Ajuste no Estoque",
            height=38,
            fg_color="#0A4174",
            hover_color="#062F56",
            command=self._aplicar_ajuste,
        )
        self.btn_aplicar.pack(side="right", padx=(6, 14), pady=10)

        self.btn_revelar = ctk.CTkButton(
            footer,
            text="📊 Revelar Divergências",
            height=38,
            fg_color="#D97706",
            hover_color="#B45309",
            command=self._revelar_divergencias,
        )
        self.btn_revelar.pack(side="right", padx=6, pady=10)

        ctk.CTkButton(
            footer,
            text="📄 Exportar CSV",
            height=38,
            fg_color="#475569",
            hover_color="#334155",
            command=self._exportar_csv,
        ).pack(side="right", padx=6, pady=10)

        self._carregar_sessoes(criar_se_vazio=True)
        self.after(150, self.entry_scan.focus_set)

    def _carregar_sessoes(self, criar_se_vazio=False):
        sessoes = self.db.listar_sessoes_inventario(limit=25)
        if not sessoes and criar_se_vazio:
            ok, inv_id = self.db.criar_sessao_inventario(observacoes="Contagem automática inicial")
            if ok:
                sessoes = self.db.listar_sessoes_inventario(limit=25)

        self._sessoes_map = {}
        valores = []
        for s in sessoes:
            tag = "🟢 Aberta" if s["status"] == "aberto" else "✅ Concluída"
            label = f"#{s['id']} — {s['data_inicio'][:16]} ({tag})"
            valores.append(label)
            self._sessoes_map[label] = s

        if valores:
            self.combo_sessao.configure(values=valores)
            self.combo_sessao.set(valores[0])
            self._ao_trocar_sessao()
        else:
            self.combo_sessao.configure(values=["Nenhuma sessão"])
            self.combo_sessao.set("Nenhuma sessão")
            self.sessao_atual_id = None
            self._atualizar_tabela()

    def _ao_trocar_sessao(self):
        sel = self.combo_sessao.get()
        meta = self._sessoes_map.get(sel)
        if not meta:
            return
        self.sessao_atual_id = int(meta["id"])
        self.sessao_status = str(meta["status"])
        # Sessão já concluída já pode exibir divergências; sessão aberta inicia cega
        self.modo_cego = (self.sessao_status == "aberto")
        self._atualizar_badges()
        self._atualizar_tabela()

    def _atualizar_badges(self):
        if self.sessao_status != "aberto":
            self.lbl_status_badge.configure(text="✅ Sessão Concluída", text_color="#059669")
            self.btn_aplicar.configure(state="disabled")
            self.btn_revelar.configure(state="disabled")
        elif self.modo_cego:
            self.lbl_status_badge.configure(text="🔒 Contagem Cega Ativa", text_color="#D97706")
            self.btn_aplicar.configure(state="normal")
            self.btn_revelar.configure(state="normal")
        else:
            self.lbl_status_badge.configure(text="👁️ Divergências Reveladas", text_color="#2563EB")
            self.btn_aplicar.configure(state="normal")
            self.btn_revelar.configure(state="disabled")

    def _nova_sessao(self):
        ok, res = self.db.criar_sessao_inventario(observacoes="Nova contagem física")
        if not ok:
            messagebox.showerror("Erro", str(res), parent=self)
            return
        self._carregar_sessoes(criar_se_vazio=False)
        self.entry_scan.focus_set()

    def _registrar_bipe(self):
        if not self.sessao_atual_id:
            messagebox.showwarning("Atenção", "Crie ou selecione uma sessão de inventário.", parent=self)
            return
        if self.sessao_status != "aberto":
            messagebox.showwarning("Sessão Encerrada", "Inicie uma nova sessão para continuar contando.", parent=self)
            return

        raw = self.entry_scan.get().strip()
        if not raw:
            return
        qtd, termo, _ = parse_scan_input(raw)
        ok, res = self.db.registrar_contagem_inventario(
            self.sessao_atual_id,
            termo,
            qtd_contada=qtd,
            modo_somar=bool(self.var_somar.get()),
        )
        self.entry_scan.delete(0, "end")
        if ok:
            self.lbl_feedback.configure(
                text=f"✓ {res['nome_produto'][:24]} -> Contado: {res['qtd_contada']:g}",
                text_color="#059669",
            )
            self._atualizar_tabela()
        else:
            self.lbl_feedback.configure(text=f"✗ {res}", text_color="#DC2626")
        self.entry_scan.focus_set()

    def _revelar_divergencias(self):
        self.modo_cego = False
        self._atualizar_badges()
        self._atualizar_tabela()

    def _atualizar_tabela(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        if not self.sessao_atual_id:
            return

        itens = self.db.listar_itens_inventario(self.sessao_atual_id, ocultar_sistema=self.modo_cego)
        total_div_val = 0.0
        qtd_divergentes = 0
        for it in itens:
            if self.modo_cego:
                sys_str = "🔒 Cego"
                div_str = "🔒 Cego"
                imp_str = "🔒 Cego"
            else:
                div = float(it["divergencia"] or 0)
                val_d = float(it["valor_divergencia"] or 0)
                total_div_val += val_d
                if div != 0:
                    qtd_divergentes += 1
                sys_str = f"{it['qtd_sistema']:g}"
                div_str = f"{div:+g}" if div != 0 else "0"
                imp_str = f"R$ {val_d:+.2f}"

            self.tree.insert(
                "",
                "end",
                values=(
                    it["codigo_barras"] or "-",
                    it["nome_produto"],
                    f"{it['qtd_contada']:g}",
                    sys_str,
                    div_str,
                    imp_str,
                    it["contado_em"][11:19] if len(it["contado_em"]) >= 16 else it["contado_em"],
                ),
            )

        if self.modo_cego:
            self.lbl_resumo.configure(text=f"Itens contados: {len(itens)} (saldo do sistema oculto)")
        else:
            self.lbl_resumo.configure(
                text=f"Itens: {len(itens)} | Divergentes: {qtd_divergentes} | Impacto Financeiro: R$ {total_div_val:+.2f}"
            )

    def _exportar_csv(self):
        if not self.sessao_atual_id:
            return
        if self.modo_cego:
            messagebox.showinfo("Modo Cego", "Clique em 'Revelar Divergências' antes de exportar o relatório completo.", parent=self)
            return
        itens = self.db.listar_itens_inventario(self.sessao_atual_id, ocultar_sistema=False)
        if not itens:
            messagebox.showwarning("Vazio", "Nenhum item contado para exportar.", parent=self)
            return

        path = filedialog.asksaveasfilename(
            parent=self,
            title="Salvar Relatório de Inventário",
            defaultextension=".csv",
            initialfile=f"inventario_sessao_{self.sessao_atual_id}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Codigo Barras", "Produto", "Qtd Sistema", "Qtd Contada", "Divergencia", "Impacto R$"])
            for it in itens:
                writer.writerow([
                    it["codigo_barras"],
                    it["nome_produto"],
                    it["qtd_sistema"],
                    it["qtd_contada"],
                    it["divergencia"],
                    f"{float(it['valor_divergencia'] or 0):.2f}",
                ])
        messagebox.showinfo("Exportado", f"Relatório salvo em:\n{path}", parent=self)

    def _aplicar_ajuste(self):
        if not self.sessao_atual_id or self.sessao_status != "aberto":
            return
        if self.modo_cego:
            self._revelar_divergencias()
            if not messagebox.askyesno(
                "Conferir Divergências",
                "As divergências foram reveladas na tabela. Deseja prosseguir e aplicar os ajustes no estoque agora?",
                parent=self,
            ):
                return

        autorizado, sup_info = solicitar_autorizacao_supervisor(
            self.app,
            acao=f"Aplicar ajuste do Inventário #{self.sessao_atual_id} no estoque",
            exigir_motivo=True,
            parent=self,
        )
        if not autorizado:
            return

        motivo = sup_info.get("motivo") or "Ajuste de inventário físico"
        sup_id = sup_info.get("supervisor_id")
        self.btn_aplicar.configure(state="disabled", text="⏳ Aplicando...")

        def _worker():
            ok, res = self.db.aplicar_ajuste_inventario(
                self.sessao_atual_id,
                motivo=motivo,
                supervisor_id=sup_id,
                usuario_id=getattr(self.db, "current_user_id", None),
            )

            def _done():
                if not self.winfo_exists():
                    return
                if ok:
                    self.refresh_dashboard()
                    self._carregar_sessoes(criar_se_vazio=False)
                    messagebox.showinfo(
                        "Inventário Aplicado",
                        f"Ajuste concluído com sucesso! {res.get('itens_ajustados', 0)} item(ns) atualizado(s) no estoque.",
                        parent=self,
                    )
                else:
                    self.btn_aplicar.configure(state="normal", text="✅ Aplicar Ajuste no Estoque")
                    messagebox.showerror("Erro", str(res), parent=self)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()
