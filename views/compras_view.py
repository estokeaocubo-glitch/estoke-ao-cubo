# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Sugestão Inteligente de Compras (30/60/90 dias)
Exibe giro diário, lead time por fornecedor, estoque mínimo, exportação em PDF
e envio de pedido agrupado por fornecedor via fila Outbox (WhatsApp).
"""

from __future__ import annotations

import threading
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from services.compras_service import ComprasService
from views.base import BaseDialog


class SugestaoComprasDialog(BaseDialog):
    """Tela analítica para planejamento e envio de pedidos de compra."""

    def __init__(self, app, parent=None):
        self._parent_win = parent
        self.compras_service = ComprasService(app.db)
        self.sugestoes_atuais: list[dict] = []
        super().__init__(app, title="Sugestão Inteligente de Compras (Giro 30/60/90d)", geometry="1020x640", topmost=True)

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

        self.create_title_label(header, text="📈 Sugestão Automática de Compra").pack(side="left", padx=14, pady=12)

        ctk.CTkLabel(header, text="Janela:", font=("Segoe UI", 12)).pack(side="left", padx=(10, 4))
        self.seg_janela = ctk.CTkSegmentedButton(
            header,
            values=["30 dias", "60 dias", "90 dias"],
            command=lambda _v: self._recalcular(),
        )
        self.seg_janela.set("30 dias")
        self.seg_janela.pack(side="left", padx=4)

        ctk.CTkLabel(header, text="Cobertura (d):", font=("Segoe UI", 12)).pack(side="left", padx=(12, 4))
        self.entry_cobertura = ctk.CTkEntry(header, width=52)
        self.entry_cobertura.insert(0, "15")
        self.entry_cobertura.pack(side="left", padx=4)

        ctk.CTkLabel(header, text="Lead Time (d):", font=("Segoe UI", 12)).pack(side="left", padx=(10, 4))
        self.entry_lead = ctk.CTkEntry(header, width=52)
        self.entry_lead.insert(0, "7")
        self.entry_lead.pack(side="left", padx=4)

        self.btn_calc = ctk.CTkButton(
            header,
            text="🔄 Recalcular",
            width=115,
            height=34,
            fg_color="#0A4174",
            hover_color="#062F56",
            command=self._recalcular,
        )
        self.btn_calc.pack(side="right", padx=14)

        tree_frame = ctk.CTkFrame(self, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=14, pady=4)

        cols = ("prio", "produto", "fornecedor", "atual", "minimo", "giro", "lead", "rest", "comprar", "custo")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=16)
        specs = [
            ("prio", "Prioridade", 85),
            ("produto", "Produto", 220),
            ("fornecedor", "Fornecedor", 160),
            ("atual", "Atual", 60),
            ("minimo", "Mín.", 55),
            ("giro", "Giro/dia", 70),
            ("lead", "Lead(d)", 65),
            ("rest", "Autonomia", 80),
            ("comprar", "Sugestão", 80),
            ("custo", "Custo Est.", 95),
        ]
        for c, h, w in specs:
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center" if c not in ("produto", "fornecedor") else "w")

        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        footer = self.create_card(self)
        footer.pack(fill="x", padx=14, pady=(8, 12))

        self.lbl_resumo = ctk.CTkLabel(footer, text="Calculando...", font=("Segoe UI", 12, "bold"))
        self.lbl_resumo.pack(side="left", padx=14, pady=12)

        ctk.CTkButton(
            footer,
            text="📲 Enviar Pedido WhatsApp (Fornecedor)",
            height=38,
            fg_color="#059669",
            hover_color="#047857",
            command=self._enviar_fornecedores,
        ).pack(side="right", padx=(6, 14), pady=10)

        ctk.CTkButton(
            footer,
            text="📄 Exportar PDF",
            height=38,
            fg_color="#0A4174",
            hover_color="#062F56",
            command=self._exportar_pdf,
        ).pack(side="right", padx=6, pady=10)

        self._recalcular()

    def _ler_parametros(self) -> tuple[int, int, int]:
        janela_str = self.seg_janela.get() or "30 dias"
        janela = int(janela_str.split()[0])
        try:
            cobertura = max(1, int(self.entry_cobertura.get().strip()))
        except ValueError:
            cobertura = 15
        try:
            lead = max(1, int(self.entry_lead.get().strip()))
        except ValueError:
            lead = 7
        return janela, cobertura, lead

    def _recalcular(self):
        janela, cobertura, lead = self._ler_parametros()
        self.btn_calc.configure(state="disabled", text="⏳ Calculando...")
        self.lbl_resumo.configure(text=f"Analisando giro dos últimos {janela} dias...")

        def _worker():
            itens = self.compras_service.calcular_sugestao_compras(
                janela_dias=janela,
                dias_cobertura=cobertura,
                lead_time_padrao=lead,
                apenas_necessarios=True,
            )

            def _done():
                if not self.winfo_exists():
                    return
                self.btn_calc.configure(state="normal", text="🔄 Recalcular")
                self.sugestoes_atuais = itens
                for iid in self.tree.get_children():
                    self.tree.delete(iid)

                total_un = 0
                total_r = 0.0
                for it in itens:
                    total_un += int(it["qtd_sugerida"])
                    total_r += float(it["custo_estimado"])
                    aut = f"{it['dias_estoque_restante']:.1f}d" if it["dias_estoque_restante"] < 999 else ">90d"
                    self.tree.insert(
                        "",
                        "end",
                        values=(
                            it["prioridade"],
                            it["nome"],
                            it["fornecedor_nome"],
                            it["estoque_atual"],
                            it["estoque_minimo"],
                            f"{it['giro_diario']:.2f}",
                            it["lead_time_dias"],
                            aut,
                            it["qtd_sugerida"],
                            f"R$ {it['custo_estimado']:.2f}",
                        ),
                    )
                self.lbl_resumo.configure(
                    text=f"{len(itens)} produto(s) para repor | Total: {total_un} un. | Custo estimado: R$ {total_r:.2f}"
                )

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _exportar_pdf(self):
        if not self.sugestoes_atuais:
            messagebox.showinfo("Lista Vazia", "Não há itens com necessidade de compra no momento.", parent=self)
            return
        janela, cobertura, _ = self._ler_parametros()
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Salvar Sugestão de Compras (PDF)",
            defaultextension=".pdf",
            initialfile=f"sugestao_compras_{janela}d.pdf",
            filetypes=[("PDF", "*.pdf")],
        )
        if not path:
            return

        def _worker():
            ok, res = self.compras_service.exportar_pdf_compras(
                self.sugestoes_atuais,
                path,
                janela_dias=janela,
                dias_cobertura=cobertura,
            )

            def _done():
                if not self.winfo_exists():
                    return
                if ok:
                    messagebox.showinfo("PDF Gerado", f"Relatório de compras salvo em:\n{res}", parent=self)
                else:
                    messagebox.showerror("Erro", f"Falha ao gerar PDF: {res}", parent=self)

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _enviar_fornecedores(self):
        if not self.sugestoes_atuais:
            messagebox.showinfo("Lista Vazia", "Não há itens para enviar pedido.", parent=self)
            return

        grupos = self.compras_service.agrupar_por_fornecedor(self.sugestoes_atuais)
        outbox = getattr(self.app, "outbox_service", None)
        enfileirados = 0
        sem_tel = []

        for g in grupos:
            tel = g.get("fornecedor_telefone") or ""
            msg = self.compras_service.formatar_pedido_whatsapp(g)
            if tel and outbox is not None:
                outbox.enfileirar(
                    "whatsapp",
                    {
                        "telefone": tel,
                        "mensagem": msg,
                        "metadata": {"origem": "sugestao_compras", "fornecedor": g["fornecedor_nome"]},
                    },
                )
                enfileirados += 1
            else:
                sem_tel.append((g["fornecedor_nome"], msg))

        if enfileirados:
            messagebox.showinfo(
                "Pedidos na Fila Outbox",
                f"{enfileirados} pedido(s) colocado(s) na fila Outbox para envio via WhatsApp!",
                parent=self,
            )
        if sem_tel:
            preview_txt = "\n\n---\n\n".join(txt for _, txt in sem_tel[:3])
            try:
                self.clipboard_clear()
                self.clipboard_append(preview_txt)
            except Exception:
                pass
            messagebox.showinfo(
                "Fornecedores Sem Telefone",
                f"{len(sem_tel)} grupo(s) sem telefone cadastrado foram copiados para a área de transferência.\n\n{preview_txt[:700]}",
                parent=self,
            )
