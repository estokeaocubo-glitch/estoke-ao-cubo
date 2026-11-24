# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - View Isolada do PDV (Ponto de Venda)
Extraída de main.py (Onda 4) mantendo todos os atalhos F2..F12, leitores,
persistência de rascunho, vendas suspensas, multi-pagamento e cleanup seguro de timers.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import threading
import time
import uuid
from tkinter import messagebox, simpledialog, ttk
from typing import Any

import customtkinter as ctk
from PIL import Image

import invoice_generator
from printer import get_printer
from services.venda_service import parse_scan_input

try:
    import barcode_scanner
    BARCODE_SCANNER_AVAILABLE = True
except ImportError:
    barcode_scanner = None
    BARCODE_SCANNER_AVAILABLE = False

logger = logging.getLogger("EstokeAoCubo.pdv")


class PDVDialog(ctk.CTkToplevel):
    """Janela isolada do Ponto de Venda (PDV), desacoplada do App monolítico."""

    def __init__(self, app: Any, db: Any = None):
        super().__init__(app)
        self.app = app
        self.db = db if db is not None else getattr(app, "db", None)
        self.current_user = getattr(app, "current_user", None)
        self.user_id = self.current_user.get("id") if isinstance(self.current_user, dict) else 1
        self.business_type = getattr(app, "business_type", "") or ""

        self._is_closed = False
        self._closing_in_progress = False
        self._after_ids: set[str] = set()
        self._bound_shortcuts: list[str] = []
        self._search_after_id: str | None = None
        self._hover_after_id: str | None = None
        self._hover_tooltip = None
        self._hover_tooltip_img_ref = None
        self._hover_current_item = None
        self._suspend_draft_save = False

        self.title("PDV - Registrar Venda")
        self.geometry("900x600")
        self.minsize(700, 400)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        if hasattr(self.app, "_pdv_dialog"):
            self.app._pdv_dialog = self

        self.carrinho: list[dict[str, Any]] = []
        if hasattr(self.app, "carrinho"):
            self.app.carrinho = self.carrinho

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._on_destroy, add="+")

        try:
            if hasattr(app, "_icon_path_ico") and app._icon_path_ico:
                self.schedule_after(50, lambda: self.iconbitmap(app._icon_path_ico))
        except Exception:
            pass

        self._build_ui()

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        if val is None:
            return default
        try:
            res = float(val)
            return default if (math.isnan(res) or math.isinf(res)) else res
        except (ValueError, TypeError):
            if isinstance(val, str):
                try:
                    res = float(val.replace(",", ".").strip())
                    return default if (math.isnan(res) or math.isinf(res)) else res
                except (ValueError, TypeError):
                    pass
            return default

    @staticmethod
    def _safe_int(val: Any, default: int = 0) -> int:
        if val is None:
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def schedule_after(self, delay_ms: int, callback) -> str | None:
        """Agenda callback rastreado para cancelamento automático ao fechar o PDV."""
        if self._is_closed:
            return None

        def _wrapped():
            if self._is_closed:
                return
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            callback()

        try:
            after_id = self.after(delay_ms, _wrapped)
            self._after_ids.add(after_id)
            return after_id
        except Exception:
            return None

    def cancel_after(self, after_id: str | None) -> None:
        if not after_id:
            return
        self._after_ids.discard(after_id)
        try:
            self.after_cancel(after_id)
        except Exception:
            pass

    def _cleanup_resources(self) -> None:
        """Cancela todos os timers after(), tooltips e bindings pendentes sem gerar TclError."""
        if self._is_closed:
            return
        self._is_closed = True

        self.cancel_after(self._search_after_id)
        self._search_after_id = None
        self.cancel_after(self._hover_after_id)
        self._hover_after_id = None

        for aid in list(self._after_ids):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._after_ids.clear()

        if self._hover_tooltip is not None:
            try:
                self._hover_tooltip.destroy()
            except Exception:
                pass
            self._hover_tooltip = None
            self._hover_tooltip_img_ref = None

        for seq in list(self._bound_shortcuts):
            try:
                self.unbind(seq)
            except Exception:
                pass
        self._bound_shortcuts.clear()

        if getattr(self.app, "_pdv_dialog", None) is self:
            self.app._pdv_dialog = None

    def _on_destroy(self, event=None) -> None:
        if event is None or getattr(event, "widget", None) is self:
            self._cleanup_resources()

    def close(self) -> None:
        """Fecha o PDV com limpeza determinística de timers, tooltips e singleton."""
        if self._closing_in_progress:
            return
        self._closing_in_progress = True
        self._cleanup_resources()
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass

    def _bind_shortcut(self, sequence: str, handler) -> None:
        self.bind(sequence, handler)
        self._bound_shortcuts.append(sequence)

    def _exigir_supervisor(self, acao: str) -> bool:
        fn = getattr(self.app, "_exigir_supervisor", None)
        if callable(fn):
            return bool(fn(acao, parent=self))
        return True

    def _check_trial_limits(self) -> bool:
        fn = getattr(self.app, "check_trial_limits", None)
        if callable(fn):
            return bool(fn("transacao", self))
        return False

    def _notify(self, title: str, message: str, tipo: str = "info") -> None:
        notifier = getattr(self.app, "notifier", None)
        if notifier is not None and hasattr(notifier, "notify"):
            try:
                notifier.notify(title, message, tipo)
            except Exception:
                pass

    def _build_ui(self) -> None:
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        left_container = ctk.CTkFrame(main_frame, fg_color="transparent")
        left_container.pack(side="left", fill="both", expand=True, padx=(0, 5))

        left_frame = ctk.CTkScrollableFrame(
            left_container,
            fg_color=("white", "#2B2B2B"),
            corner_radius=15,
            scrollbar_button_color=("#CCCCCC", "#555555"),
        )
        left_frame.pack(fill="both", expand=True)

        right_container = ctk.CTkFrame(main_frame, fg_color="transparent")
        right_container.pack(side="right", fill="both", expand=True, padx=(5, 0))

        right_frame = ctk.CTkScrollableFrame(
            right_container,
            fg_color=("white", "#2B2B2B"),
            corner_radius=15,
            scrollbar_button_color=("#CCCCCC", "#555555"),
        )
        right_frame.pack(fill="both", expand=True)

        self._left_frame = left_frame
        self._right_frame = right_frame

        # --- ESQUERDA: Seleção de Produto ---
        ctk.CTkLabel(
            left_frame,
            text="Adicionar Produto / Kit",
            font=("Aileron", 16, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=15)

        ctk.CTkLabel(left_frame, text="Scan Código de Barras:", font=("Aileron", 11, "italic")).pack(
            fill="x", padx=15, pady=(0, 0)
        )
        scan_row = ctk.CTkFrame(left_frame, fg_color="transparent")
        scan_row.pack(fill="x", padx=15, pady=(0, 5))
        self.entry_scan = ctk.CTkEntry(scan_row, placeholder_text="Clique aqui e escaneie...")
        self.entry_scan.pack(side="left", fill="x", expand=True, padx=(0, 5))

        def _abrir_camera_scanner():
            if not BARCODE_SCANNER_AVAILABLE or barcode_scanner is None:
                messagebox.showinfo(
                    "Indisponível",
                    "Instale opencv-python e pyzbar para usar o scanner por câmera.",
                    parent=self,
                )
                return
            if not barcode_scanner.scanner_disponivel():
                messagebox.showinfo(
                    "Indisponível",
                    "Câmera não disponível ou drivers não instalados.",
                    parent=self,
                )
                return

            cam_win = ctk.CTkToplevel(self)
            cam_win.title("Scanner por Câmera")
            cam_win.geometry("640x520")
            cam_win.attributes("-topmost", True)
            cam_win.resizable(False, False)

            lbl_video = ctk.CTkLabel(cam_win, text="Iniciando câmera...", width=640, height=480)
            lbl_video.pack()
            ctk.CTkLabel(
                cam_win,
                text="Aponte o código de barras para a câmera",
                font=("Aileron", 12),
                text_color=("#333", "white"),
            ).pack(pady=5)

            import cv2 as _cv2

            cap = _cv2.VideoCapture(0, _cv2.CAP_DSHOW) if os.name == "nt" else _cv2.VideoCapture(0)
            _cam_running = [True]

            def _release_cam(*_args):
                _cam_running[0] = False
                try:
                    if cap is not None and cap.isOpened():
                        cap.release()
                except Exception:
                    pass

            cam_win.bind("<Destroy>", _release_cam)

            def _update_frame():
                if not _cam_running[0] or self._is_closed:
                    return
                try:
                    if not cam_win.winfo_exists():
                        return
                except Exception:
                    return
                ret, frame = cap.read()
                if ret:
                    from services.hardware_service import HardwareService

                    code_data, barcodes = HardwareService().decodificar_frame(frame)
                    for bc in barcodes:
                        pts = bc.polygon
                        if len(pts) == 4:
                            for i in range(4):
                                _cv2.line(
                                    frame,
                                    (pts[i].x, pts[i].y),
                                    (pts[(i + 1) % 4].x, pts[(i + 1) % 4].y),
                                    (0, 255, 0),
                                    3,
                                )
                    if code_data:
                        _cam_running[0] = False
                        cap.release()
                        cam_win.destroy()
                        self.entry_scan.delete(0, "end")
                        self.entry_scan.insert(0, code_data)
                        self.entry_scan.event_generate("<Return>")
                        return

                    frame_rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(frame_rgb)
                    ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(640, 480))
                    lbl_video.configure(image=ctk_img, text="")
                    lbl_video._cam_img_ref = ctk_img

                if _cam_running[0] and not self._is_closed:
                    cam_win.after(33, _update_frame)

            def _on_cam_close():
                _cam_running[0] = False
                try:
                    cap.release()
                except Exception:
                    pass
                try:
                    cam_win.destroy()
                except Exception:
                    pass

            cam_win.protocol("WM_DELETE_WINDOW", _on_cam_close)
            cam_win.after(100, _update_frame)

        ctk.CTkButton(
            scan_row,
            text="📷",
            width=40,
            height=32,
            fg_color="#0A4174",
            hover_color="#062F56",
            command=_abrir_camera_scanner,
        ).pack(side="right")

        # Carregar Produtos e Kits
        df_prods = self.db.listar_produtos_vendaveis()
        if not df_prods.empty:
            df_prods["type"] = "produto"
            df_prods["display_name"] = df_prods.apply(
                lambda r: f"{r['nome']} (Estoque: {self._safe_int(r['quantidade'])} | R$ {self._safe_float(r['preco']):.2f})",
                axis=1,
            )

        df_kits = self.db.listar_kits()
        if not df_kits.empty:
            df_kits["type"] = "kit"
            df_kits["display_name"] = df_kits.apply(
                lambda r: f"[KIT] {r['nome']} (Promo: R$ {self._safe_float(r['preco_venda']):.2f})",
                axis=1,
            )

        self.item_map: dict[str, Any] = {}
        self.all_items_list: list[dict[str, Any]] = []
        if not df_prods.empty:
            for _, r in df_prods.iterrows():
                self.item_map[r["display_name"]] = r
                self.all_items_list.append(dict(r))
        if not df_kits.empty:
            for _, r in df_kits.iterrows():
                self.item_map[r["display_name"]] = r
                self.all_items_list.append(dict(r))

        top_vendidos = self.db.obter_produtos_mais_vendidos(50)
        top_vendidos_set = set(top_vendidos)
        top_vendidos_rank = {pid: idx for idx, pid in enumerate(top_vendidos)}

        def _sort_key(item):
            item_id = item.get("id")
            if item.get("type") == "produto" and item_id in top_vendidos_set:
                return (0, top_vendidos_rank[item_id])
            return (1, item.get("display_name", ""))

        self.all_items_list.sort(key=_sort_key)

        if not self.all_items_list:
            ctk.CTkLabel(left_frame, text="Sem itens cadastrados.", text_color="red").pack(pady=20)
            self._sem_itens = True
            return
        self._sem_itens = False

        # --- Busca + Filtros ---
        search_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
        search_frame.pack(fill="x", padx=15, pady=(5, 0))
        self.entry_busca = ctk.CTkEntry(search_frame, placeholder_text="🔍 Buscar produto...", height=32)
        self.entry_busca.pack(fill="x")

        filter_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
        filter_frame.pack(fill="x", padx=15, pady=(3, 0))

        cats_unicas = ["Todas"]
        if not df_prods.empty and "categoria" in df_prods.columns:
            cats_unicas += sorted(df_prods["categoria"].dropna().unique().tolist())
        var_filtro_cat = ctk.StringVar(value="Todas")
        ctk.CTkComboBox(
            filter_frame,
            values=cats_unicas,
            variable=var_filtro_cat,
            width=120,
            state="readonly",
            height=28,
            font=("Aileron", 10),
        ).pack(side="left", padx=(0, 3))

        var_filtro_subcat = ctk.StringVar(value="Todas")
        combo_filtro_subcat = ctk.CTkComboBox(
            filter_frame,
            values=["Todas"],
            variable=var_filtro_subcat,
            width=120,
            state="readonly",
            height=28,
            font=("Aileron", 10),
        )
        combo_filtro_subcat.pack(side="left", padx=(0, 3))

        var_filtro_subsub = ctk.StringVar(value="Todas")
        combo_filtro_subsub = ctk.CTkComboBox(
            filter_frame,
            values=["Todas"],
            variable=var_filtro_subsub,
            width=120,
            state="readonly",
            height=28,
            font=("Aileron", 10),
        )
        combo_filtro_subsub.pack(side="left")

        def _on_cat_change(*_):
            cat_sel = var_filtro_cat.get()
            if cat_sel == "Todas":
                combo_filtro_subcat.configure(values=["Todas"])
                var_filtro_subcat.set("Todas")
                combo_filtro_subsub.configure(values=["Todas"])
                var_filtro_subsub.set("Todas")
            else:
                subcats = ["Todas"]
                if not df_prods.empty and "subcategoria" in df_prods.columns:
                    mask = df_prods["categoria"] == cat_sel
                    subcats += sorted(df_prods.loc[mask, "subcategoria"].dropna().loc[lambda s: s != ""].unique().tolist())
                combo_filtro_subcat.configure(values=subcats)
                var_filtro_subcat.set("Todas")
                combo_filtro_subsub.configure(values=["Todas"])
                var_filtro_subsub.set("Todas")

        def _on_subcat_change(*_):
            cat_sel = var_filtro_cat.get()
            subcat_sel = var_filtro_subcat.get()
            if subcat_sel == "Todas" or cat_sel == "Todas":
                combo_filtro_subsub.configure(values=["Todas"])
                var_filtro_subsub.set("Todas")
            else:
                subsubs = ["Todas"]
                if not df_prods.empty and "sub_subcategoria" in df_prods.columns:
                    mask = (df_prods["categoria"] == cat_sel) & (df_prods["subcategoria"] == subcat_sel)
                    subsubs += sorted(
                        df_prods.loc[mask, "sub_subcategoria"].dropna().loc[lambda s: s != ""].unique().tolist()
                    )
                combo_filtro_subsub.configure(values=subsubs)
                var_filtro_subsub.set("Todas")

        var_filtro_cat.trace_add("write", _on_cat_change)
        var_filtro_subcat.trace_add("write", _on_subcat_change)

        prod_scroll = ctk.CTkScrollableFrame(left_frame, fg_color=("#F5F5F5", "#333333"), corner_radius=10, height=180)
        prod_scroll.pack(fill="both", expand=True, padx=15, pady=5)

        _img_cache_pdv: dict[str, Any] = {}

        def _carregar_imagem_produto(item_row):
            if item_row.get("type") != "produto":
                return None
            img_name = item_row.get("imagem", "")
            if not img_name:
                return None
            if img_name in _img_cache_pdv:
                return _img_cache_pdv[img_name]
            try:
                img_path = os.path.join(self.db.data_dir, "product_images", img_name)
                if os.path.exists(img_path):
                    pil_img = Image.open(img_path)
                    pil_img.thumbnail((100, 100), Image.Resampling.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(100, 100))
                    _img_cache_pdv[img_name] = ctk_img
                    return ctk_img
            except Exception as exc:
                logger.debug("Erro ao carregar preview: %s", exc)
            return None

        _HOVER_DELAY_MS = 200

        def _destroy_tooltip():
            if self._hover_after_id:
                self.cancel_after(self._hover_after_id)
                self._hover_after_id = None
            if self._hover_tooltip is not None:
                try:
                    self._hover_tooltip.destroy()
                except Exception:
                    pass
                self._hover_tooltip = None
                self._hover_tooltip_img_ref = None

        def _do_show_tooltip(item_data, x_root, y_root):
            if self._is_closed or self._hover_current_item is not item_data:
                return
            if self._hover_tooltip is not None:
                try:
                    if self._hover_tooltip.winfo_exists():
                        return
                except Exception:
                    self._hover_tooltip = None

            ctk_img = _carregar_imagem_produto(item_data)
            if not ctk_img:
                return

            tw = ctk.CTkToplevel(self)
            tw.wm_overrideredirect(True)
            tw.attributes("-topmost", True)
            tw.wm_geometry(f"+{x_root + 15}+{y_root + 10}")
            self._hover_tooltip = tw

            frame_tt = ctk.CTkFrame(
                tw,
                fg_color=("white", "#2B2B2B"),
                corner_radius=10,
                border_width=1,
                border_color=("#CCCCCC", "#555555"),
            )
            frame_tt.pack(fill="both", expand=True, padx=2, pady=2)
            self._hover_tooltip_img_ref = ctk_img
            ctk.CTkLabel(frame_tt, image=ctk_img, text="").pack(padx=8, pady=(8, 4))
            ctk.CTkLabel(
                frame_tt,
                text=item_data.get("nome", ""),
                font=("Aileron", 11, "bold"),
                text_color=("#001D39", "white"),
                wraplength=160,
            ).pack(padx=8, pady=(0, 8))

        def _show_hover_preview(event, item_data):
            if self._hover_current_item is item_data:
                if self._hover_tooltip is not None:
                    try:
                        if self._hover_tooltip.winfo_exists():
                            self._hover_tooltip.wm_geometry(f"+{event.x_root + 15}+{event.y_root + 10}")
                    except Exception:
                        pass
                return
            _destroy_tooltip()
            self._hover_current_item = item_data
            self._hover_after_id = self.schedule_after(
                _HOVER_DELAY_MS,
                lambda: _do_show_tooltip(item_data, event.x_root, event.y_root),
            )

        def _is_mouse_inside_frame(frame):
            try:
                mx, my = frame.winfo_pointerxy()
                fx, fy = frame.winfo_rootx(), frame.winfo_rooty()
                fw, fh = frame.winfo_width(), frame.winfo_height()
                return fx <= mx < fx + fw and fy <= my < fy + fh
            except Exception:
                return False

        def _on_frame_leave(event, frame):
            if _is_mouse_inside_frame(frame):
                return
            _destroy_tooltip()
            self._hover_current_item = None

        def _hide_hover_preview(event=None):
            _destroy_tooltip()
            self._hover_current_item = None

        _selected_item = [None]

        def _select_item(item_data):
            _selected_item[0] = item_data
            _populate_product_list()

        def _quick_add_to_cart(item_data):
            try:
                self._add_to_cart_logic(item_data, 1)
            except Exception:
                pass

        def _populate_product_list():
            if self._is_closed:
                return
            for w in prod_scroll.winfo_children():
                w.destroy()

            query = self.entry_busca.get().strip().lower()
            cat_filter = var_filtro_cat.get()
            subcat_filter = var_filtro_subcat.get()
            subsub_filter = var_filtro_subsub.get()

            MAX_RENDER = 50
            total_matches = 0
            rendered = 0

            for item_data in self.all_items_list:
                if query:
                    nome_match = query in item_data.get("display_name", "").lower()
                    ean = str(item_data.get("codigo_barras") or "").lower()
                    codigo_match = (ean == query or ean.startswith(query)) if ean else False
                    if not nome_match and not codigo_match:
                        continue
                if cat_filter != "Todas" and item_data.get("type") != "kit":
                    if str(item_data.get("categoria", "")) != cat_filter:
                        continue
                if subcat_filter != "Todas" and item_data.get("type") != "kit":
                    if str(item_data.get("subcategoria", "") or "") != subcat_filter:
                        continue
                if subsub_filter != "Todas" and item_data.get("type") != "kit":
                    if str(item_data.get("sub_subcategoria", "") or "") != subsub_filter:
                        continue

                total_matches += 1
                if rendered >= MAX_RENDER:
                    continue
                rendered += 1

                is_selected = (
                    _selected_item[0] is not None
                    and item_data.get("display_name") == _selected_item[0].get("display_name")
                )
                fg = ("#D6EAF8", "#1A3A5C") if is_selected else ("white", "#404040")
                border_color = ("#0A4174", "#7BBDE8") if is_selected else ("#7BBDE8", "#49769F")

                f = ctk.CTkFrame(prod_scroll, fg_color=fg, corner_radius=8, border_width=2, border_color=border_color)
                f.pack(fill="x", pady=2, padx=3)

                nome_display = item_data.get("nome", "")
                if item_data.get("type", "produto") == "kit":
                    txt = f"[KIT] {nome_display}  —  R$ {self._safe_float(item_data.get('preco_venda')):.2f}"
                else:
                    extras = [
                        str(item_data.get(k) or "")
                        for k in ("categoria", "subcategoria", "sub_subcategoria", "tamanho")
                        if item_data.get(k)
                    ]
                    info_extra = " | ".join(extras)
                    ep = self._safe_int(item_data.get("estoque_p"))
                    em = self._safe_int(item_data.get("estoque_m"))
                    eg = self._safe_int(item_data.get("estoque_g"))
                    egg = self._safe_int(item_data.get("estoque_gg"))
                    _preco_display = self._safe_float(item_data.get("preco"))
                    _qtd_display = self._safe_int(item_data.get("quantidade"))
                    txt = f"{nome_display}  —  Est: {_qtd_display} | R$ {_preco_display:.2f}"
                    unidade_m = item_data.get("unidade_medida", "un") or "un"
                    preco_peso_v = self._safe_float(item_data.get("preco_por_peso"))
                    if unidade_m in ("kg", "g") and preco_peso_v > 0:
                        txt = f"{nome_display}  —  R$ {preco_peso_v:.2f}/kg"
                    if ep > 0 or em > 0 or eg > 0 or egg > 0:
                        txt += f"\nP:{ep} M:{em} G:{eg} GG:{egg}"
                    if info_extra:
                        txt += f"\n{info_extra}"

                btn = ctk.CTkButton(
                    f,
                    text=txt,
                    text_color=("#333333", "white"),
                    fg_color="transparent",
                    hover_color=("#E8F4FD", "#2A4A6A"),
                    anchor="w",
                    font=("Aileron", 11),
                    command=lambda d=item_data: _select_item(d),
                )
                btn.pack(fill="x", padx=5, pady=4)
                btn.bind("<Double-1>", lambda e, d=item_data: _quick_add_to_cart(d))
                f.bind("<Double-1>", lambda e, d=item_data: _quick_add_to_cart(d))
                f.bind("<Enter>", lambda e, d=item_data: _show_hover_preview(e, d))
                btn.bind("<Enter>", lambda e, d=item_data: _show_hover_preview(e, d))
                f.bind("<Leave>", lambda e, fr=f: _on_frame_leave(e, fr))
                btn.bind("<Leave>", lambda e, fr=f: _on_frame_leave(e, fr))

            if total_matches > MAX_RENDER:
                ctk.CTkLabel(
                    prod_scroll,
                    text=f"Exibindo {MAX_RENDER} de {total_matches} itens — digite mais para refinar a busca",
                    font=("Aileron", 10, "italic"),
                    text_color=("gray45", "gray65"),
                ).pack(pady=6)

        self._populate_product_list = _populate_product_list

        def _agendar_busca_pdv(_event=None):
            if self._search_after_id is not None:
                self.cancel_after(self._search_after_id)
            self._search_after_id = self.schedule_after(150, _populate_product_list)

        prod_scroll.bind("<Leave>", _hide_hover_preview)
        self.entry_busca.bind("<KeyRelease>", _agendar_busca_pdv)
        var_filtro_cat.trace_add("write", lambda *a: _populate_product_list())
        var_filtro_subcat.trace_add("write", lambda *a: _populate_product_list())
        var_filtro_subsub.trace_add("write", lambda *a: _populate_product_list())
        _populate_product_list()

        # Quantidade
        ctk.CTkLabel(left_frame, text="Quantidade:", anchor="w", text_color=("#001D39", "white")).pack(
            fill="x", padx=15, pady=(5, 0)
        )
        qtd_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
        qtd_frame.pack(fill="x", padx=15, pady=3)

        def _diminuir_qtd():
            try:
                val = int(self.entry_qtd.get())
                if val > 1:
                    self.entry_qtd.delete(0, "end")
                    self.entry_qtd.insert(0, str(val - 1))
            except ValueError:
                self.entry_qtd.delete(0, "end")
                self.entry_qtd.insert(0, "1")

        def _aumentar_qtd():
            try:
                val = int(self.entry_qtd.get())
                self.entry_qtd.delete(0, "end")
                self.entry_qtd.insert(0, str(val + 1))
            except ValueError:
                self.entry_qtd.delete(0, "end")
                self.entry_qtd.insert(0, "1")

        ctk.CTkButton(
            qtd_frame,
            text="▼",
            width=40,
            height=35,
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 16, "bold"),
            command=_diminuir_qtd,
        ).pack(side="left", padx=(0, 5))
        self.entry_qtd = ctk.CTkEntry(qtd_frame, width=80, justify="center")
        self.entry_qtd.insert(0, "1")
        self.entry_qtd.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            qtd_frame,
            text="▲",
            width=40,
            height=35,
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 16, "bold"),
            command=_aumentar_qtd,
        ).pack(side="left", padx=(5, 0))

        self.entry_peso = None
        if self.business_type == "padaria_acougue":
            ctk.CTkLabel(left_frame, text="Peso (kg):", anchor="w", text_color=("#001D39", "white")).pack(
                fill="x", padx=15, pady=(5, 0)
            )
            peso_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
            peso_frame.pack(fill="x", padx=15, pady=3)
            self.entry_peso = ctk.CTkEntry(peso_frame, placeholder_text="Ex: 0.500", justify="center")
            self.entry_peso.pack(side="left", fill="x", expand=True, padx=(0, 5))
            ctk.CTkLabel(peso_frame, text="kg", font=("Aileron", 12, "bold"), text_color=("#001D39", "white")).pack(
                side="left"
            )

        # Desconto por item
        ctk.CTkLabel(left_frame, text="Desconto por Item:", anchor="w", text_color=("#001D39", "white")).pack(
            fill="x", padx=15, pady=(5, 0)
        )
        desc_item_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
        desc_item_frame.pack(fill="x", padx=15, pady=3)
        self.entry_desc_item = ctk.CTkEntry(desc_item_frame, placeholder_text="0", width=80)
        self.entry_desc_item.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.tipo_desc_item_var = ctk.StringVar(value="%")
        ctk.CTkComboBox(desc_item_frame, values=["%", "R$"], variable=self.tipo_desc_item_var, width=60, state="readonly").pack(
            side="left"
        )

        # Cliente
        ctk.CTkLabel(left_frame, text="Cliente:", anchor="w", text_color=("#001D39", "white")).pack(
            fill="x", padx=15, pady=(5, 0)
        )
        self.df_clientes = self.db.listar_clientes()
        self.clientes_options = ["Consumidor Final"] + (
            self.df_clientes["nome"].tolist() if not self.df_clientes.empty else []
        )
        self.var_cliente = ctk.StringVar(value="Consumidor Final")
        ctk.CTkComboBox(left_frame, values=self.clientes_options, variable=self.var_cliente).pack(
            fill="x", padx=15, pady=3
        )

        ctk.CTkLabel(left_frame, text="Contato / WhatsApp:", anchor="w", text_color=("#001D39", "white")).pack(
            fill="x", padx=15, pady=(5, 0)
        )
        self.entry_contato_cliente = ctk.CTkEntry(left_frame, placeholder_text="(11) 99999-9999")
        self.entry_contato_cliente.pack(fill="x", padx=15, pady=3)

        def _on_cliente_change(*_args):
            nome_sel = self.var_cliente.get()
            self.entry_contato_cliente.delete(0, "end")
            if nome_sel != "Consumidor Final" and not self.df_clientes.empty:
                c_rows = self.df_clientes[self.df_clientes["nome"] == nome_sel]
                if not c_rows.empty:
                    contato = c_rows.iloc[0].get("contato", "")
                    if contato:
                        self.entry_contato_cliente.insert(0, contato)

        self.var_cliente.trace_add("write", _on_cliente_change)

        # Vendedor
        ctk.CTkLabel(left_frame, text="Vendedor:", anchor="w", text_color=("#001D39", "white")).pack(
            fill="x", padx=15, pady=(5, 0)
        )
        df_vendedores = self.db.listar_vendedores()
        self.vendedores_options = [""] + (df_vendedores["nome"].tolist() if not df_vendedores.empty else [])
        vendedor_turno = self.db.vendedor_no_turno()
        self.var_vendedor = ctk.StringVar(value=vendedor_turno)
        ctk.CTkComboBox(left_frame, values=self.vendedores_options, variable=self.var_vendedor).pack(
            fill="x", padx=15, pady=3
        )

        def _add_to_cart_click():
            try:
                item_row = _selected_item[0]
                if item_row is None:
                    messagebox.showerror("Erro", "Selecione um produto na lista.", parent=self)
                    return
                qtd = int(self.entry_qtd.get())
                peso_kg = None
                if self.entry_peso is not None:
                    peso_str = self.entry_peso.get().strip().replace(",", ".")
                    if peso_str:
                        peso_kg = float(peso_str)
                self._add_to_cart_logic(item_row, qtd, peso_kg=peso_kg)
            except ValueError:
                messagebox.showerror("Erro", "Quantidade inválida.", parent=self)
            except Exception as exc:
                messagebox.showerror("Erro", f"Ocorreu um erro: {exc}", parent=self)

        ctk.CTkButton(
            left_frame,
            text="Adicionar ao Carrinho",
            command=_add_to_cart_click,
            height=40,
            fg_color="#0A4174",
            hover_color="#062F56",
        ).pack(padx=15, pady=20, fill="x")

        def _mostrar_popup_desconto_scan(p_row, initial_qtd=1):
            preco = self._safe_float(p_row["preco"])
            nome = p_row["nome"]

            popup = ctk.CTkToplevel(self)
            popup.title("Produto Escaneado")
            popup.geometry("380x340")
            popup.attributes("-topmost", True)
            popup.resizable(False, False)
            popup.grab_set()

            ctk.CTkLabel(popup, text=f"📦 {nome}", font=("Aileron", 15, "bold"), wraplength=340).pack(pady=(15, 3))
            ctk.CTkLabel(popup, text=f"Preço: R$ {preco:.2f}", font=("Aileron", 13)).pack(pady=(0, 10))

            qtd_f = ctk.CTkFrame(popup, fg_color="transparent")
            qtd_f.pack(fill="x", padx=25, pady=(0, 5))
            ctk.CTkLabel(qtd_f, text="Quantidade:", anchor="w").pack(side="left")
            entry_qtd_scan = ctk.CTkEntry(qtd_f, width=80)
            entry_qtd_scan.pack(side="right")
            entry_qtd_scan.insert(0, str(initial_qtd))

            var_desc = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(popup, text="Aplicar desconto?", variable=var_desc).pack(pady=(5, 5))

            desc_f = ctk.CTkFrame(popup, fg_color="transparent")
            ctk.CTkLabel(desc_f, text="Tipo:", anchor="w").pack(fill="x", padx=5)
            var_tipo = ctk.StringVar(value="%")
            ctk.CTkComboBox(desc_f, values=["%", "R$"], variable=var_tipo, width=80, state="readonly").pack(
                padx=5, pady=(0, 5)
            )
            ctk.CTkLabel(desc_f, text="Valor do desconto:", anchor="w").pack(fill="x", padx=5)
            entry_desc_val = ctk.CTkEntry(desc_f, placeholder_text="0", width=100)
            entry_desc_val.pack(padx=5, pady=(0, 5))

            def _toggle_desc():
                if var_desc.get():
                    desc_f.pack(fill="x", padx=25, pady=(0, 5))
                else:
                    desc_f.pack_forget()

            var_desc.trace_add("write", lambda *_a: _toggle_desc())

            def _confirmar(_event=None):
                try:
                    qtd_raw = float(entry_qtd_scan.get().strip().replace(",", "."))
                    if qtd_raw <= 0:
                        raise ValueError
                    qtd_ok = int(qtd_raw) if qtd_raw.is_integer() else round(qtd_raw, 3)
                except ValueError:
                    messagebox.showerror("Erro", "Quantidade inválida.", parent=popup)
                    return

                preco_final = preco
                if var_desc.get():
                    try:
                        desc_v = float(entry_desc_val.get().strip().replace(",", "."))
                    except (ValueError, AttributeError):
                        desc_v = 0.0
                    if desc_v > 0:
                        if var_tipo.get() == "%":
                            preco_final = preco * (1 - desc_v / 100)
                        else:
                            preco_final = max(0, preco - desc_v)

                old_desc = self.entry_desc_item.get()
                self.entry_desc_item.delete(0, "end")
                self.entry_desc_item.insert(0, "0")

                p_mod = copy.copy(p_row)
                p_mod["preco"] = preco_final

                try:
                    self._add_to_cart_logic(p_mod, qtd_ok)
                except Exception as exc:
                    messagebox.showerror("Erro ao adicionar", str(exc), parent=popup)
                finally:
                    self.entry_desc_item.delete(0, "end")
                    if old_desc:
                        self.entry_desc_item.insert(0, old_desc)
                    popup.destroy()

            ctk.CTkButton(
                popup,
                text="✅ Adicionar ao Carrinho",
                command=_confirmar,
                height=38,
                fg_color="#0A4174",
                hover_color="#062F56",
            ).pack(pady=(10, 10), padx=25, fill="x")
            entry_qtd_scan.bind("<Return>", _confirmar)
            entry_qtd_scan.focus_set()
            entry_qtd_scan.select_range(0, "end")

        def _on_scan(_event=None):
            raw_code = self.entry_scan.get().strip()
            if not raw_code:
                return
            try:
                qtd_scan, code, _has_mult = parse_scan_input(raw_code)
            except ValueError as err:
                messagebox.showerror("Leitura Inválida", str(err), parent=self)
                self.entry_scan.select_range(0, "end")
                return

            res_df = self.db.buscar_produto_por_codigo(code)
            if not res_df.empty:
                p_row = res_df.iloc[0]
                p_row["type"] = "produto"
                _mostrar_popup_desconto_scan(p_row, initial_qtd=qtd_scan)
                self.entry_scan.delete(0, "end")
            else:
                messagebox.showwarning("Não encontrado", f"Produto com código '{code}' não encontrado.", parent=self)
                self.entry_scan.select_range(0, "end")

        self.entry_scan.bind("<Return>", _on_scan)
        self.entry_scan.focus_set()

        # --- DIREITA: Carrinho e Finalização ---
        ctk.CTkLabel(
            right_frame,
            text="Carrinho de Compras",
            font=("Aileron", 16, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(10, 5))

        tree_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=10)

        style_cart = ttk.Style()
        if ctk.get_appearance_mode() == "Dark":
            style_cart.configure("Cart.Treeview", background="#2B2B2B", foreground="white", fieldbackground="#2B2B2B", rowheight=28)
            style_cart.configure("Cart.Treeview.Heading", background="#3A3A3A", foreground="white")
        else:
            style_cart.configure("Cart.Treeview", background="white", foreground="#333333", fieldbackground="white", rowheight=28)
            style_cart.configure("Cart.Treeview.Heading", background="#E8F4FD", foreground="#001D39")

        self.tree = ttk.Treeview(tree_frame, columns=("qtd", "produto", "unit", "total"), show="headings", height=10, style="Cart.Treeview")
        self.tree.heading("qtd", text="Qtd")
        self.tree.heading("produto", text="Produto")
        self.tree.heading("unit", text="Unit (R$)")
        self.tree.heading("total", text="Total (R$)")
        self.tree.column("qtd", width=50, anchor="center")
        self.tree.column("produto", width=150)
        self.tree.column("unit", width=80, anchor="e")
        self.tree.column("total", width=80, anchor="e")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.lbl_total = ctk.CTkLabel(right_frame, text="Total: R$ 0,00", font=("Aileron", 18, "bold"), text_color="#28a745")
        self.lbl_total.pack(pady=(5, 3))

        # Desconto Global
        desc_frame = ctk.CTkFrame(right_frame, fg_color=("white", "#3A3A3A"), corner_radius=10)
        desc_frame.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(desc_frame, text="Desconto:", font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).pack(
            side="left", padx=(10, 5), pady=8
        )
        self.entry_desconto = ctk.CTkEntry(desc_frame, width=80, placeholder_text="0")
        self.entry_desconto.pack(side="left", padx=2, pady=8)
        self.tipo_desconto_var = ctk.StringVar(value="%")
        ctk.CTkComboBox(desc_frame, values=["%", "R$"], variable=self.tipo_desconto_var, width=60, state="readonly").pack(
            side="left", padx=5, pady=8
        )

        self.lbl_total_final = ctk.CTkLabel(right_frame, text="", font=("Aileron", 13, "bold"), text_color="#0A4174")
        self.lbl_total_final.pack(pady=(0, 3))

        # Pagamentos Múltiplos
        pgto_container = ctk.CTkFrame(right_frame, fg_color=("white", "#3A3A3A"), corner_radius=10)
        pgto_container.pack(fill="x", padx=10, pady=3)
        pgto_header = ctk.CTkFrame(pgto_container, fg_color="transparent")
        pgto_header.pack(fill="x", padx=10, pady=(5, 0))
        ctk.CTkLabel(pgto_header, text="Pagamento:", font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).pack(
            side="left"
        )

        self.pagamentos_list: list[dict[str, Any]] = []
        pgto_rows_frame = ctk.CTkFrame(pgto_container, fg_color="transparent")
        pgto_rows_frame.pack(fill="x", padx=5, pady=3)
        formas_pgto_options = ["Dinheiro", "PIX", "Cartão Débito", "Cartão Crédito", "Fiado"]

        # Troco (Dinheiro)
        troco_frame = ctk.CTkFrame(right_frame, fg_color=("white", "#3A3A3A"), corner_radius=10)
        row_troco = ctk.CTkFrame(troco_frame, fg_color="transparent")
        row_troco.pack(fill="x", padx=5, pady=5)
        ctk.CTkLabel(row_troco, text="Valor recebido:", font=("Aileron", 11), text_color=("#001D39", "white")).pack(
            side="left", padx=5
        )
        self.entry_troco = ctk.CTkEntry(row_troco, width=100, placeholder_text="0,00")
        self.entry_troco.pack(side="left", padx=2)
        self.lbl_troco = ctk.CTkLabel(row_troco, text="", font=("Aileron", 12, "bold"), text_color="#28a745")
        self.lbl_troco.pack(side="left", padx=10)

        row_buttons = ctk.CTkFrame(troco_frame, fg_color="transparent")
        row_buttons.pack(fill="x", padx=5, pady=(0, 5))

        def _calcular_troco(*_args):
            try:
                recebido = float(self.entry_troco.get().replace(",", "."))
                outros = 0.0
                for p in self.pagamentos_list:
                    if p["forma_var"].get() != "Dinheiro":
                        val_str = p["valor_entry"].get().strip().replace(",", ".")
                        try:
                            outros += float(val_str) if val_str else 0.0
                        except ValueError:
                            pass
                _, total_final = self._calcular_desconto()
                esperado_dinheiro = max(0.0, total_final - outros)
                troco = recebido - esperado_dinheiro

                try:
                    config_nota = self.db.get_config_nota()
                    arredondar = config_nota.get("arredondamento_troco", 0.0)
                except Exception:
                    arredondar = 0.0
                if arredondar > 0:
                    troco = math.floor(troco / arredondar) * arredondar

                if troco >= 0:
                    self.lbl_troco.configure(text=f"Troco: R$ {troco:.2f}", text_color="#28a745")
                else:
                    self.lbl_troco.configure(text=f"Falta: R$ {abs(troco):.2f}", text_color="#FF6B6B")
            except ValueError:
                self.lbl_troco.configure(text="")

        def _set_nota(valor):
            self.entry_troco.delete(0, "end")
            self.entry_troco.insert(0, f"{valor:.2f}".replace(".", ","))
            _calcular_troco()

        for n in [5, 10, 20, 50, 100, 200]:
            ctk.CTkButton(
                row_buttons,
                text=f"R${n}",
                width=40,
                height=22,
                fg_color="#0A4174",
                hover_color="#062F56",
                text_color="white",
                font=("Aileron", 10),
                command=lambda val=n: _set_nota(val),
            ).pack(side="left", padx=2)

        def _has_dinheiro():
            return any(p["forma_var"].get() == "Dinheiro" for p in self.pagamentos_list)

        def _update_troco(*_args):
            if _has_dinheiro():
                troco_frame.pack(fill="x", padx=10, pady=3)
            else:
                troco_frame.pack_forget()

        def _add_pgto_row(forma="Dinheiro", valor=""):
            row_f = ctk.CTkFrame(pgto_rows_frame, fg_color="transparent")
            row_f.pack(fill="x", pady=2)
            var_forma = ctk.StringVar(value=forma)
            ctk.CTkComboBox(row_f, values=formas_pgto_options, variable=var_forma, width=130, state="readonly").pack(
                side="left", padx=2
            )
            entry_val = ctk.CTkEntry(row_f, width=90, placeholder_text="R$ 0,00")
            if valor:
                entry_val.insert(0, valor)
            entry_val.pack(side="left", padx=2)
            entry_val.bind("<KeyRelease>", lambda _e: _update_troco())
            item = {"forma_var": var_forma, "valor_entry": entry_val, "frame": row_f}

            def _remove():
                if len(self.pagamentos_list) <= 1:
                    return
                self.pagamentos_list.remove(item)
                row_f.destroy()
                _update_troco()

            ctk.CTkButton(
                row_f,
                text="✕",
                width=28,
                height=28,
                fg_color="#FF6B6B",
                hover_color="#FF5252",
                text_color="white",
                command=_remove,
            ).pack(side="left", padx=2)
            self.pagamentos_list.append(item)
            var_forma.trace_add("write", lambda *_a: _update_troco())

        _add_pgto_row()
        ctk.CTkButton(
            pgto_container,
            text="+ Forma de Pagamento",
            height=26,
            fg_color="#7BBDE8",
            hover_color="#49769F",
            text_color="#001D39",
            font=("Aileron", 10, "bold"),
            command=lambda: _add_pgto_row(),
        ).pack(padx=10, pady=(2, 5))

        _update_troco()
        self.entry_troco.bind("<KeyRelease>", _calcular_troco)

        # Observação
        obs_frame = ctk.CTkFrame(right_frame, fg_color=("white", "#3A3A3A"), corner_radius=10)
        obs_frame.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(obs_frame, text="Observação:", font=("Aileron", 11, "bold"), text_color=("#001D39", "white")).pack(
            side="left", padx=(10, 5), pady=8
        )
        self.entry_obs_venda = ctk.CTkEntry(obs_frame, placeholder_text="Ex: entregar às 14h, embalar para presente...")
        self.entry_obs_venda.pack(side="left", fill="x", expand=True, padx=(2, 10), pady=8)

        self.entry_desconto.bind("<KeyRelease>", self._atualizar_label_desconto)
        self.tipo_desconto_var.trace_add("write", self._atualizar_label_desconto)

        def remover_item():
            selected = self.tree.selection()
            if not selected:
                return
            idx = self.tree.index(selected[0])
            item_rem = self.carrinho[idx]
            if not self._exigir_supervisor(f"Remover item '{item_rem.get('nome', '')}' do carrinho PDV"):
                return
            del self.carrinho[idx]
            self.update_cart_ui()

        def alterar_preco_item():
            selected = self.tree.selection()
            if not selected:
                messagebox.showwarning("Alerta", "Selecione um item no carrinho.", parent=self)
                return
            idx = self.tree.index(selected[0])
            item = self.carrinho[idx]

            preco_dialog = ctk.CTkToplevel(self)
            preco_dialog.title("Alterar Preço")
            preco_dialog.geometry("350x200")
            preco_dialog.attributes("-topmost", True)
            preco_dialog.grab_set()
            preco_dialog.focus_set()

            ctk.CTkLabel(preco_dialog, text=f"Produto: {item['nome']}", font=("Aileron", 13, "bold")).pack(pady=(15, 5))
            ctk.CTkLabel(preco_dialog, text=f"Preço atual: R$ {item['preco']:.2f}", font=("Aileron", 11)).pack(
                pady=(0, 10)
            )
            ctk.CTkLabel(preco_dialog, text="Novo preço (R$):", font=("Aileron", 11)).pack(padx=20, anchor="w")
            entry_novo_preco = ctk.CTkEntry(preco_dialog, height=35, placeholder_text="0.00")
            entry_novo_preco.pack(padx=20, fill="x", pady=(2, 10))
            entry_novo_preco.insert(0, f"{item['preco']:.2f}")
            entry_novo_preco.select_range(0, "end")
            entry_novo_preco.focus_set()

            def _aplicar_preco(_event=None):
                try:
                    novo = float(entry_novo_preco.get().strip().replace(",", "."))
                    if novo < 0:
                        messagebox.showerror("Erro", "Preço não pode ser negativo.", parent=preco_dialog)
                        return
                except ValueError:
                    messagebox.showerror("Erro", "Valor inválido.", parent=preco_dialog)
                    return
                preco_original = item.get("preco_original", item["preco"])
                item["preco"] = novo
                item["desconto_item"] = max(0, preco_original - novo)
                self.update_cart_ui()
                preco_dialog.destroy()

            entry_novo_preco.bind("<Return>", _aplicar_preco)
            ctk.CTkButton(
                preco_dialog, text="Aplicar", command=_aplicar_preco, height=36, fg_color="#0A4174", hover_color="#062F56"
            ).pack(pady=10, padx=20, fill="x")

        ctk.CTkButton(
            right_frame,
            text="Remover Item Selecionado",
            command=remover_item,
            height=28,
            fg_color="#FF6B6B",
            hover_color="#FF5252",
            text_color="white",
        ).pack(pady=3, padx=10, fill="x")
        ctk.CTkButton(
            right_frame,
            text="Alterar Preço do Item",
            command=alterar_preco_item,
            height=28,
            fg_color="#FF9800",
            hover_color="#E68900",
            text_color="white",
        ).pack(pady=3, padx=10, fill="x")

        # Opções rápidas
        pdv_opts_frame = ctk.CTkFrame(right_frame, fg_color=("white", "#3A3A3A"), corner_radius=10)
        pdv_opts_frame.pack(fill="x", padx=10, pady=3)
        self.var_fila_continua = ctk.BooleanVar(value=True)
        self.var_imprimir_direto = ctk.BooleanVar(value=False)
        try:
            _emit_default = bool(self.db.get_config_nota().get("emitir_nfce_automatico", 0))
        except Exception:
            _emit_default = False
        self.var_emitir_nfce = ctk.BooleanVar(value=_emit_default)
        ctk.CTkCheckBox(
            pdv_opts_frame,
            text="Manter PDV aberto (Fila Contínua)",
            variable=self.var_fila_continua,
            font=("Aileron", 11),
            checkbox_width=18,
            checkbox_height=18,
        ).pack(anchor="w", padx=10, pady=(6, 2))
        ctk.CTkCheckBox(
            pdv_opts_frame,
            text="Imprimir recibo direto (sem perguntar)",
            variable=self.var_imprimir_direto,
            font=("Aileron", 11),
            checkbox_width=18,
            checkbox_height=18,
        ).pack(anchor="w", padx=10, pady=(2, 2))
        ctk.CTkCheckBox(
            pdv_opts_frame,
            text="Emitir NFC-e (Mod. 65) ao finalizar",
            variable=self.var_emitir_nfce,
            font=("Aileron", 11),
            checkbox_width=18,
            checkbox_height=18,
        ).pack(anchor="w", padx=10, pady=(2, 4))

        cpf_row = ctk.CTkFrame(pdv_opts_frame, fg_color="transparent")
        cpf_row.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkLabel(cpf_row, text="CPF/CNPJ Nota:", font=("Aileron", 10, "bold")).pack(side="left", padx=(0, 6))
        self.entry_cpf_nota = ctk.CTkEntry(cpf_row, placeholder_text="Opcional (somente dígitos)", height=26)
        self.entry_cpf_nota.pack(side="left", fill="x", expand=True)

        def _ler_balanca_serial(_event=None):
            from services.hardware_service import HardwareError, HardwareService

            peso_lido = None
            try:
                peso_lido = HardwareService().ler_peso_balanca(timeout=0.6)
            except HardwareError:
                peso_lido = None
            except Exception:
                peso_lido = None

            if peso_lido is None:
                val_str = simpledialog.askstring(
                    "Balança Checkout (F9)",
                    "Balança serial não detectada nas portas COM.\nInforme o peso em kg (ex: 0.450):",
                    parent=self,
                )
                if not val_str:
                    return
                try:
                    peso_lido = float(val_str.strip().replace(",", "."))
                except ValueError:
                    messagebox.showerror("Erro", "Peso inválido.", parent=self)
                    return

            if peso_lido and peso_lido > 0:
                target = self.entry_peso if self.entry_peso is not None else self.entry_qtd
                target.delete(0, "end")
                target.insert(0, f"{peso_lido:.3f}")
                self._notify("Balança Checkout", f"Peso capturado: {peso_lido:.3f} kg", "info")

        def _mostrar_qr_pix_pdv(_event=None):
            if not self.carrinho:
                messagebox.showwarning(
                    "Carrinho Vazio", "Adicione itens ao carrinho antes de gerar o QR Code PIX.", parent=self
                )
                return
            _, total_pix = self._calcular_desconto()
            config_nota = self.db.get_config_nota()
            chave_pix = (config_nota.get("chave_pix") or "").strip()
            if not chave_pix:
                messagebox.showwarning(
                    "Chave PIX não configurada",
                    "Configure sua Chave PIX em Configurações → Nota Fiscal para exibir o QR Code no PDV.",
                    parent=self,
                )
                return
            from printer import gerar_payload_pix

            nome_emp = config_nota.get("empresa_nome") or "EstokeAoCubo"
            payload = gerar_payload_pix(chave_pix, nome_emp, "BRASIL", valor=total_pix)

            pix_win = ctk.CTkToplevel(self)
            pix_win.title(f"Pagamento PIX — R$ {total_pix:.2f}")
            pix_win.geometry("380x470")
            pix_win.attributes("-topmost", True)
            pix_win.grab_set()

            ctk.CTkLabel(pix_win, text=f"PIX: R$ {total_pix:.2f}", font=("Aileron", 20, "bold"), text_color="#28a745").pack(
                pady=(15, 5)
            )
            ctk.CTkLabel(pix_win, text=f"Chave: {chave_pix}", font=("Aileron", 11), text_color="gray").pack(pady=(0, 8))

            try:
                import qrcode

                qr = qrcode.QRCode(box_size=6, border=2)
                qr.add_data(payload)
                qr.make(fit=True)
                img_pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
                ctk_qr = ctk.CTkImage(light_image=img_pil, dark_image=img_pil, size=(240, 240))
                lbl_qr = ctk.CTkLabel(pix_win, image=ctk_qr, text="")
                lbl_qr._qr_ref = ctk_qr
                lbl_qr.pack(pady=5)
            except Exception as exc:
                ctk.CTkLabel(pix_win, text=f"QR Code indisponível: {exc}").pack(pady=20)

            entry_copia = ctk.CTkEntry(pix_win, width=320)
            entry_copia.pack(padx=20, pady=(8, 4))
            entry_copia.insert(0, payload)

            def _copiar_pix():
                pix_win.clipboard_clear()
                pix_win.clipboard_append(payload)
                messagebox.showinfo("Copiado", "Código PIX Copia e Cola copiado!", parent=pix_win)

            ctk.CTkButton(
                pix_win,
                text="📋 Copiar PIX Copia e Cola",
                command=_copiar_pix,
                fg_color="#0A4174",
                hover_color="#062F56",
                height=36,
            ).pack(padx=20, pady=6, fill="x")

        def _suspender_venda_atual(_event=None, tipo="suspensa"):
            if not self.carrinho:
                messagebox.showwarning(
                    "Carrinho Vazio", "Adicione itens antes de suspender ou gerar orçamento.", parent=self
                )
                return
            subt = sum(float(i["qtd"]) * float(i["preco"]) for i in self.carrinho)
            val_desc, total_liq = self._calcular_desconto()
            loja_id_pdv = self.current_user.get("loja_id") if isinstance(self.current_user, dict) else None
            ok_s, res_s = self.db.suspender_venda(
                self.carrinho,
                subtotal=subt,
                desconto=val_desc,
                total=total_liq,
                cliente=self.var_cliente.get(),
                contato_cliente=self.entry_contato_cliente.get().strip(),
                vendedor=self.var_vendedor.get(),
                observacao=self.entry_obs_venda.get().strip(),
                tipo=tipo,
                usuario_id=self.user_id,
                loja_id=loja_id_pdv,
            )
            if ok_s:
                self._suspend_draft_save = True
                self.carrinho.clear()
                self.entry_desconto.delete(0, "end")
                self.entry_obs_venda.delete(0, "end")
                self.update_cart_ui()
                self._suspend_draft_save = False
                self.db.limpar_rascunho_pdv(self.user_id)
                lbl_tipo = "Orçamento" if tipo == "orcamento" else "Venda suspensa"
                messagebox.showinfo(
                    lbl_tipo,
                    f"{lbl_tipo} salvo com código: {res_s['codigo']}\nUse Shift+F6 para retomar.",
                    parent=self,
                )
                self.entry_scan.focus_set()
            else:
                messagebox.showerror("Erro", str(res_s), parent=self)

        def _retomar_venda_callback(dados_suspensa):
            self._suspend_draft_save = True
            try:
                self.carrinho.clear()
                self.carrinho.extend(list(dados_suspensa.get("itens") or []))
                cli = dados_suspensa.get("cliente") or "Consumidor Final"
                if cli in self.clientes_options:
                    self.var_cliente.set(cli)
                if dados_suspensa.get("contato_cliente"):
                    self.entry_contato_cliente.delete(0, "end")
                    self.entry_contato_cliente.insert(0, dados_suspensa["contato_cliente"])
                if dados_suspensa.get("vendedor") in self.vendedores_options:
                    self.var_vendedor.set(dados_suspensa["vendedor"])
                self.entry_obs_venda.delete(0, "end")
                if dados_suspensa.get("observacao"):
                    self.entry_obs_venda.insert(0, dados_suspensa["observacao"])
                self.entry_desconto.delete(0, "end")
                desc_v = float(dados_suspensa.get("desconto") or 0.0)
                if desc_v > 0:
                    self.tipo_desconto_var.set("R$")
                    self.entry_desconto.insert(0, f"{desc_v:.2f}")
            finally:
                self._suspend_draft_save = False
            self.update_cart_ui()
            self.entry_scan.focus_set()

        def _abrir_vendas_suspensas(_event=None):
            from views.vendas_suspensas_view import VendasSuspensasDialog

            VendasSuspensasDialog(self.app, on_resume_callback=_retomar_venda_callback, parent=self)

        quick_tools_row = ctk.CTkFrame(right_frame, fg_color="transparent")
        quick_tools_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(
            quick_tools_row,
            text="⚖️ Balança (F9)",
            command=_ler_balanca_serial,
            height=28,
            fg_color="#49769F",
            hover_color="#0A4174",
            text_color="white",
            font=("Aileron", 11, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=(0, 3))
        ctk.CTkButton(
            quick_tools_row,
            text="💠 QR PIX (F10)",
            command=_mostrar_qr_pix_pdv,
            height=28,
            fg_color="#00897B",
            hover_color="#00695C",
            text_color="white",
            font=("Aileron", 11, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=(3, 0))

        quick_tools_row2 = ctk.CTkFrame(right_frame, fg_color="transparent")
        quick_tools_row2.pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(
            quick_tools_row2,
            text="⏸️ Suspender (F6)",
            command=lambda: _suspender_venda_atual(tipo="suspensa"),
            height=28,
            fg_color="#7D3C98",
            hover_color="#5B2C6F",
            text_color="white",
            font=("Aileron", 10, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ctk.CTkButton(
            quick_tools_row2,
            text="📝 Orçamento",
            command=lambda: _suspender_venda_atual(tipo="orcamento"),
            height=28,
            fg_color="#5D6D7E",
            hover_color="#34495E",
            text_color="white",
            font=("Aileron", 10, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=2)
        ctk.CTkButton(
            quick_tools_row2,
            text="📂 Retomar (Shift+F6)",
            command=_abrir_vendas_suspensas,
            height=28,
            fg_color="#2E86C1",
            hover_color="#1B4F72",
            text_color="white",
            font=("Aileron", 10, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=2)
        ctk.CTkButton(
            quick_tools_row2,
            text="🖨️ Cupom (F11)",
            command=lambda: self.app.reimprimir_ultimo_cupom(parent=self) if hasattr(self.app, "reimprimir_ultimo_cupom") else None,
            height=28,
            fg_color="#455A64",
            hover_color="#263238",
            text_color="white",
            font=("Aileron", 10, "bold"),
        ).pack(side="left", fill="x", expand=True, padx=(2, 0))

        ctk.CTkLabel(
            right_frame,
            text="Atalhos: F2 Scan (qtd*cod) | F4 Qtd | F6 Suspender | Shift+F6 Retomar | F8 Desc | F9 Bal | F10 PIX | F11 Reimp | F12 Finalizar",
            font=("Aileron", 9),
            text_color="gray",
            wraplength=410,
        ).pack(pady=(1, 2))

        ctk.CTkButton(
            right_frame,
            text="FINALIZAR VENDA (F12)",
            command=lambda: self.finalizar_venda(),
            height=50,
            fg_color="#28a745",
            hover_color="#218838",
            font=("Aileron", 14, "bold"),
        ).pack(pady=(8, 10), padx=10, fill="x")

        # Atalhos de Teclado rastreados para cleanup seguro no destroy
        self._bind_shortcut("<F2>", lambda _e: self.entry_scan.focus_set())
        self._bind_shortcut("<F4>", lambda _e: (self.entry_qtd.focus_set(), self.entry_qtd.select_range(0, "end")))
        self._bind_shortcut("<F6>", lambda _e: _suspender_venda_atual(tipo="suspensa"))
        self._bind_shortcut("<Shift-F6>", _abrir_vendas_suspensas)
        self._bind_shortcut("<F8>", lambda _e: (self.entry_desconto.focus_set(), self.entry_desconto.select_range(0, "end")))
        self._bind_shortcut("<F9>", _ler_balanca_serial)
        self._bind_shortcut("<F10>", _mostrar_qr_pix_pdv)
        self._bind_shortcut(
            "<F11>",
            lambda _e: (
                (self.app.reimprimir_ultimo_cupom(parent=self) if hasattr(self.app, "reimprimir_ultimo_cupom") else None),
                "break",
            )[1],
        )
        self._bind_shortcut("<F12>", lambda _e: self.finalizar_venda())

        def _verificar_restaurar_rascunho():
            if self._is_closed:
                return
            try:
                rascunho = self.db.obter_rascunho_pdv(self.user_id)
                if not rascunho or not rascunho.get("itens"):
                    return
                qtd_itens = len(rascunho["itens"])
                if messagebox.askyesno(
                    "Restaurar Venda Interrompida",
                    f"Foi encontrado um carrinho não finalizado ({qtd_itens} item(ns), salvo em {rascunho['atualizado_em'][:16].replace('T', ' ')}).\n\nDeseja restaurar estes itens?",
                    parent=self,
                ):
                    _retomar_venda_callback(rascunho)
                else:
                    self.db.limpar_rascunho_pdv(self.user_id)
            except Exception as exc:
                logger.debug("Aviso ao verificar rascunho PDV: %s", exc)

        self.schedule_after(120, _verificar_restaurar_rascunho)

    def _add_to_cart_logic(self, item_row: dict[str, Any], qtd: float, peso_kg: float | None = None) -> bool:
        if qtd <= 0:
            raise ValueError("Quantidade deve ser positiva.")

        item_type = item_row.get("type", "produto")
        if item_type == "produto":
            if qtd > self._safe_float(item_row.get("quantidade", 0)):
                messagebox.showerror("Erro", f"Estoque insuficiente! Disponível: {item_row.get('quantidade')}", parent=self)
                return False
            price = self._safe_float(item_row.get("preco"))
            name = item_row["nome"]
            item_id = item_row["id"]

            unidade = item_row.get("unidade_medida", "un") or "un"
            if peso_kg and peso_kg > 0 and unidade in ("kg", "g"):
                preco_peso = self._safe_float(item_row.get("preco_por_peso"))
                if preco_peso > 0:
                    price = preco_peso * peso_kg
                name = f"{name} ({peso_kg:.3f}kg)"
        elif item_type == "kit":
            max_kits = self.db.verificar_disponibilidade_kit(item_row["id"])
            if qtd > max_kits:
                messagebox.showerror(
                    "Erro", f"Estoque insuficiente para montar {qtd} kits!\nMáximo possível hoje: {max_kits}", parent=self
                )
                return False
            price = self._safe_float(item_row.get("preco_venda"))
            name = f"[KIT] {item_row['nome']}"
            item_id = item_row["id"]
        else:
            return False

        desc_val_item = 0.0
        try:
            desc_input = self.entry_desc_item.get().strip()
            if desc_input:
                desc_val_item = float(desc_input.replace(",", "."))
        except ValueError:
            desc_val_item = 0.0

        if desc_val_item > 0:
            if self.tipo_desc_item_var.get() == "%":
                price = price * (1 - desc_val_item / 100)
            else:
                price = max(0, price - desc_val_item)

        for c_item in self.carrinho:
            if c_item["id"] == item_id and c_item["type"] == item_type:
                if item_type == "produto":
                    if c_item["qtd"] + qtd > self._safe_float(item_row.get("quantidade", 0)):
                        messagebox.showerror("Erro", "Limite de estoque atingido.", parent=self)
                        return False
                elif item_type == "kit":
                    max_kits_check = self.db.verificar_disponibilidade_kit(item_row["id"])
                    if c_item["qtd"] + qtd > max_kits_check:
                        messagebox.showerror("Erro", "Limite de kits atingido.", parent=self)
                        return False
                c_item["qtd"] += qtd
                self.update_cart_ui()
                return True

        preco_original = (
            self._safe_float(item_row.get("preco", price))
            if item_type == "produto"
            else self._safe_float(item_row.get("preco_venda", price))
        )
        desconto_aplicado = preco_original - price if price < preco_original else 0.0
        self.carrinho.append(
            {
                "id": item_id,
                "type": item_type,
                "nome": name,
                "preco": price,
                "preco_original": preco_original,
                "desconto_item": desconto_aplicado,
                "qtd": qtd,
                "row_data": item_row,
            }
        )
        self.update_cart_ui()
        return True

    def adicionar_item_carrinho(self, item_ou_id: dict[str, Any] | int, qtd: float = 1, peso_kg: float | None = None) -> bool:
        """Adiciona item programaticamente ao carrinho usando catálogo carregado ou dict direto."""
        if getattr(self, "_sem_itens", False):
            return False
        if isinstance(item_ou_id, int):
            item_row = next(
                (it for it in self.all_items_list if it.get("type") == "produto" and int(it.get("id", 0)) == item_ou_id),
                None,
            )
            if item_row is None:
                return False
        else:
            item_row = dict(item_ou_id)
            item_row.setdefault("type", "produto")
        return self._add_to_cart_logic(item_row, qtd, peso_kg=peso_kg)

    def _calcular_desconto(self) -> tuple[float, float]:
        total_bruto = sum(item["qtd"] * item["preco"] for item in self.carrinho)
        try:
            raw = self.entry_desconto.get().strip()
            desc_val = float(raw.replace(",", ".")) if raw else 0.0
        except ValueError:
            desc_val = 0.0
        if self.tipo_desconto_var.get() == "%":
            valor_desconto = total_bruto * (desc_val / 100.0)
        else:
            valor_desconto = desc_val
        valor_desconto = min(max(0.0, valor_desconto), total_bruto)
        return round(valor_desconto, 2), round(total_bruto - valor_desconto, 2)

    def calcular_resumo_venda(self) -> dict[str, float]:
        subtotal = round(sum(float(i["qtd"]) * float(i["preco"]) for i in self.carrinho), 2)
        desconto, total = self._calcular_desconto()
        return {"subtotal": subtotal, "desconto": desconto, "total": total}

    def _atualizar_label_desconto(self, *_args) -> None:
        if not self.carrinho:
            self.lbl_total_final.configure(text="")
            return
        valor_desc, total_final = self._calcular_desconto()
        if valor_desc > 0:
            self.lbl_total_final.configure(text=f"Desconto: -R$ {valor_desc:.2f}  |  Total Final: R$ {total_final:.2f}")
        else:
            self.lbl_total_final.configure(text="")

    def update_cart_ui(self) -> None:
        if self._is_closed:
            return
        self.entry_troco.delete(0, "end")
        self.lbl_troco.configure(text="")
        for item in self.tree.get_children():
            self.tree.delete(item)

        total = 0.0
        for item in self.carrinho:
            subtotal = item["qtd"] * item["preco"]
            total += subtotal
            self.tree.insert("", "end", values=(item["qtd"], item["nome"], f"{item['preco']:.2f}", f"{subtotal:.2f}"))

        self.lbl_total.configure(text=f"Subtotal: R$ {total:.2f}")
        self._atualizar_label_desconto()

        if not self._suspend_draft_save:
            try:
                self.db.salvar_rascunho_pdv(
                    self.user_id,
                    self.carrinho,
                    cliente=self.var_cliente.get(),
                    desconto=self.entry_desconto.get().strip(),
                    tipo_desconto=self.tipo_desconto_var.get(),
                    observacao=self.entry_obs_venda.get().strip(),
                )
            except Exception as draft_err:
                logger.debug("Aviso ao salvar rascunho do PDV: %s", draft_err)

    def executar_ciclo_tef_point(
        self,
        mp_service: Any,
        device_id: str,
        valor: float,
        tipo_cartao: str,
        transacao_id: str,
        parcelas: int = 1,
        timeout_seconds: float = 120.0,
        poll_interval: float = 2.0,
        cancel_event: threading.Event | None = None,
        status_callback=None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Fluxo puro de criação + polling assíncrono + cancelamento no Pinpad Point.
        Garante que timeout ou cancel_event sempre disparem cancelar_intencao_point.
        """
        c_evt = cancel_event or threading.Event()
        if status_callback:
            status_callback(f"Enviando R$ {valor:.2f} para maquininha Point ({device_id})...")

        criada = mp_service.criar_intencao_point(
            device_id=device_id,
            valor=valor,
            tipo_cartao=tipo_cartao,
            parcelas=parcelas,
            id_venda=transacao_id,
            descricao=f"PDV Estoke #{transacao_id}",
        )
        if not criada.get("sucesso") or not criada.get("intent_id"):
            return False, {"erro": criada.get("erro") or "Falha ao enviar cobrança para a Point."}

        intent_id = str(criada["intent_id"])
        if c_evt.is_set():
            mp_service.cancelar_intencao_point(device_id, intent_id)
            return False, {"intent_id": intent_id, "cancelado": True, "erro": "Operação cancelada antes do início."}

        inicio = time.monotonic()
        while True:
            if c_evt.is_set():
                mp_service.cancelar_intencao_point(device_id, intent_id)
                return False, {"intent_id": intent_id, "cancelado": True, "erro": "Cobrança cancelada no Pinpad."}

            decorrido = time.monotonic() - inicio
            if decorrido >= float(timeout_seconds):
                mp_service.cancelar_intencao_point(device_id, intent_id)
                return False, {
                    "intent_id": intent_id,
                    "timeout": True,
                    "cancelado": True,
                    "erro": f"Tempo limite ({int(timeout_seconds)}s) excedido na maquininha Point.",
                }

            st = mp_service.consultar_intencao_point(intent_id)
            state_lbl = st.get("state") or "AGUARDANDO"
            if status_callback:
                rest = max(0, int(timeout_seconds - decorrido))
                status_callback(f"Status Point: {state_lbl} ({rest}s restantes)...")

            if st.get("finalizado"):
                if st.get("aprovado"):
                    return True, {
                        "intent_id": intent_id,
                        "payment_id": st.get("payment_id", ""),
                        "status": st.get("status_pagamento", "approved"),
                        "valor": float(st.get("valor") or valor),
                        "external_reference": transacao_id,
                    }
                return False, {
                    "intent_id": intent_id,
                    "payment_id": st.get("payment_id", ""),
                    "recusado": bool(st.get("recusado")),
                    "cancelado": bool(st.get("cancelado")),
                    "erro": st.get("erro") or "Pagamento não aprovado na maquininha Point.",
                }

            if c_evt.wait(timeout=max(0.02, float(poll_interval))):
                mp_service.cancelar_intencao_point(device_id, intent_id)
                return False, {"intent_id": intent_id, "cancelado": True, "erro": "Cobrança cancelada pelo operador."}

    def _processar_tef_point_modal(
        self,
        device_id: str,
        valor: float,
        tipo_cartao: str,
        transacao_id: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 2.0,
        mp_service: Any = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Exibe modal não-bloqueante para polling da maquininha Point com cancelamento seguro."""
        if mp_service is None:
            from services.mercadopago_service import get_mercadopago_service
            mp_service = get_mercadopago_service()

        cancel_evt = threading.Event()
        resultado: dict[str, Any] = {"ok": False, "dados": {"erro": "Cancelado pelo usuário."}}

        modal = ctk.CTkToplevel(self)
        modal.title("TEF / Pinpad Mercado Pago Point")
        modal.geometry("420x230")
        modal.resizable(False, False)
        try:
            modal.attributes("-topmost", True)
            modal.transient(self)
            modal.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            modal,
            text=f"💳 Cobrança na Maquininha Point\nR$ {valor:.2f} ({tipo_cartao})",
            font=("Aileron", 15, "bold"),
            text_color=("#001D39", "white"),
        ).pack(pady=(18, 8), padx=16)

        lbl_status = ctk.CTkLabel(modal, text="Conectando ao terminal Point...", font=("Aileron", 12), wraplength=380)
        lbl_status.pack(pady=(4, 16), padx=16)

        def _solicitar_cancelamento():
            cancel_evt.set()
            try:
                if modal.winfo_exists():
                    lbl_status.configure(text="Cancelando cobrança na maquininha...")
                    btn_cancel.configure(state="disabled")
            except Exception:
                pass

        btn_cancel = ctk.CTkButton(
            modal,
            text="Cancelar na Maquininha",
            command=_solicitar_cancelamento,
            height=38,
            fg_color="#DC2626",
            hover_color="#B91C1C",
            text_color="white",
            font=("Aileron", 12, "bold"),
        )
        btn_cancel.pack(pady=(0, 14), padx=24, fill="x")
        modal.protocol("WM_DELETE_WINDOW", _solicitar_cancelamento)

        def _update_status_ui(texto: str):
            try:
                if modal.winfo_exists() and not self._is_closed:
                    modal.after(0, lambda t=texto: lbl_status.configure(text=t) if modal.winfo_exists() else None)
            except Exception:
                pass

        def _worker():
            ok, info = self.executar_ciclo_tef_point(
                mp_service=mp_service,
                device_id=device_id,
                valor=valor,
                tipo_cartao=tipo_cartao,
                transacao_id=transacao_id,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
                cancel_event=cancel_evt,
                status_callback=_update_status_ui,
            )
            resultado["ok"] = ok
            resultado["dados"] = info

            def _fechar_modal():
                try:
                    if modal.winfo_exists():
                        modal.grab_release()
                        modal.destroy()
                except Exception:
                    pass

            try:
                if not self._is_closed and modal.winfo_exists():
                    modal.after(0, _fechar_modal)
            except Exception:
                pass

        threading.Thread(target=_worker, name="EstokePointPolling", daemon=True).start()
        try:
            self.wait_window(modal)
        except Exception:
            cancel_evt.set()
        return bool(resultado["ok"]), resultado["dados"]

    def finalizar_venda(self, confirmar_automatico: bool = False, imprimir_automatico: bool | None = None) -> tuple[bool, Any]:
        """Executa validações, TEF Point (se configurado), baixa atômica de estoque, NFC-e, recibo e limpeza."""
        if getattr(self, "_sem_itens", False):
            return False, "Sem itens disponíveis."
        if self._check_trial_limits():
            return False, "Limite trial atingido."
        if not self.carrinho:
            if not confirmar_automatico:
                messagebox.showerror("Erro", "Carrinho vazio.", parent=self)
            return False, "Carrinho vazio."

        formas_usadas = []
        total_pgto_informado = 0.0
        for p in self.pagamentos_list:
            forma = p["forma_var"].get()
            val_str = p["valor_entry"].get().strip().replace(",", ".")
            try:
                val = float(val_str) if val_str else 0.0
            except ValueError:
                val = 0.0
            if val > 0 or len(self.pagamentos_list) == 1:
                formas_usadas.append({"forma": forma, "valor": val})
                total_pgto_informado += val

        valor_desconto, total_com_desconto = self._calcular_desconto()
        if len(formas_usadas) == 1 and formas_usadas[0]["valor"] == 0:
            formas_usadas[0]["valor"] = total_com_desconto

        has_dinheiro = any(f["forma"] == "Dinheiro" for f in formas_usadas)
        outros_valores = sum(f["valor"] for f in formas_usadas if f["forma"] != "Dinheiro")
        if has_dinheiro:
            for f in formas_usadas:
                if f["forma"] == "Dinheiro" and (f["valor"] == 0 or total_pgto_informado < total_com_desconto):
                    f["valor"] = max(0.0, total_com_desconto - outros_valores)

        total_pgto_informado = sum(f["valor"] for f in formas_usadas)
        if total_pgto_informado < total_com_desconto - 0.005:
            msg_insuf = f"O valor total pago (R$ {total_pgto_informado:.2f}) é insuficiente para cobrir o total da venda (R$ {total_com_desconto:.2f})."
            if not confirmar_automatico:
                messagebox.showerror("Erro de Pagamento", msg_insuf, parent=self)
            return False, msg_insuf

        troco_real = 0.0
        if has_dinheiro:
            try:
                recebido_val = float(self.entry_troco.get().replace(",", "."))
                esperado_din = max(0.0, total_com_desconto - outros_valores)
                if recebido_val > esperado_din:
                    troco_real = round(recebido_val - esperado_din, 2)
            except ValueError:
                pass

        forma_pgto = (
            " + ".join(f"{f['forma']} R${f['valor']:.2f}" for f in formas_usadas)
            if len(formas_usadas) > 1
            else formas_usadas[0]["forma"]
        )

        # Anti-fraude: supervisor se desconto exceder teto configurado
        try:
            cfg_antifraude = self.db.get_config_nota()
            limite_desc_pct = float(cfg_antifraude.get("desconto_max_sem_senha", 10.0) or 10.0)
        except Exception:
            cfg_antifraude = {}
            limite_desc_pct = 10.0

        subtotal_original = sum(
            float(i.get("qtd", 0)) * float(i.get("preco_original", i.get("preco", 0))) for i in self.carrinho
        )
        desconto_itens_soma = sum(
            float(i.get("qtd", 0)) * max(0.0, float(i.get("preco_original", i.get("preco", 0))) - float(i.get("preco", 0)))
            for i in self.carrinho
        )
        desconto_total_efetivo = desconto_itens_soma + valor_desconto
        if subtotal_original > 0 and desconto_total_efetivo > 0:
            pct_desconto_efetivo = (desconto_total_efetivo / subtotal_original) * 100.0
            if pct_desconto_efetivo > limite_desc_pct + 0.01:
                if not self._exigir_supervisor(
                    f"Desconto de {pct_desconto_efetivo:.1f}% (R$ {desconto_total_efetivo:.2f}) acima do limite de {limite_desc_pct:.1f}%"
                ):
                    return False, "Autorização de supervisor negada."

        if any(f["forma"] == "Fiado" for f in formas_usadas) and self.var_cliente.get() == "Consumidor Final":
            if not confirmar_automatico:
                messagebox.showerror("Erro", "Para venda fiado, selecione um cliente cadastrado.", parent=self)
            return False, "Cliente obrigatório para venda fiado."

        obs_venda = self.entry_obs_venda.get().strip()
        if not confirmar_automatico:
            msg_confirm = f"Finalizar venda?\n\nSubtotal: R$ {sum(i['qtd']*i['preco'] for i in self.carrinho):.2f}"
            if valor_desconto > 0:
                msg_confirm += f"\nDesconto: -R$ {valor_desconto:.2f}"
            msg_confirm += f"\nTotal: R$ {total_com_desconto:.2f}\nPagamento: {forma_pgto}"
            if obs_venda:
                msg_confirm += f"\nObs: {obs_venda}"
            if troco_real > 0:
                msg_confirm += f"\nTroco: R$ {troco_real:.2f}"
            if not messagebox.askyesno("Confirmar", msg_confirm, parent=self):
                return False, "Cancelado pelo operador."

        transacao_id = uuid.uuid4().hex[:12]
        tef_integrado = False

        # --- TEF / Pinpad Mercado Pago Point (quando dispositivo configurado para Cartão) ---
        device_id = str(cfg_antifraude.get("mp_point_device_id") or "").strip()
        pagamentos_cartao = [f for f in formas_usadas if f["forma"] in ("Cartão Crédito", "Cartão Débito") and f["valor"] > 0]
        if device_id and pagamentos_cartao:
            from services.mercadopago_service import get_mercadopago_service
            mp_srv = get_mercadopago_service()
            if mp_srv.esta_configurado():
                for pg_c in pagamentos_cartao:
                    tipo_pt = "debit_card" if "Débito" in pg_c["forma"] else "credit_card"
                    ok_pt, dados_pt = self._processar_tef_point_modal(
                        device_id=device_id,
                        valor=pg_c["valor"],
                        tipo_cartao=tipo_pt,
                        transacao_id=transacao_id,
                        mp_service=mp_srv,
                    )
                    if not ok_pt:
                        err_pt = dados_pt.get("erro") or "Pagamento não aprovado no Pinpad Point."
                        if not confirmar_automatico:
                            messagebox.showwarning("TEF Point", err_pt, parent=self)
                        return False, err_pt
                    tef_integrado = True
                    if dados_pt.get("payment_id") and hasattr(self.db, "registrar_pagamento_mp"):
                        self.db.registrar_pagamento_mp(
                            payment_id=str(dados_pt["payment_id"]),
                            tipo="point",
                            external_reference=transacao_id,
                            status="approved",
                            valor=float(pg_c["valor"]),
                            point_intent_id=str(dados_pt.get("intent_id") or ""),
                            device_id=device_id,
                            payload=dados_pt,
                        )

        try:
            total_geral = 0.0
            itens_nota = []
            itens_venda = []
            for item in self.carrinho:
                total_geral += item["qtd"] * item["preco"]
                itens_nota.append(
                    {
                        "nome": item["nome"],
                        "qtd": item["qtd"],
                        "preco": item["preco"],
                        "preco_original": item.get("preco_original", item["preco"]),
                        "desconto_item": item.get("desconto_item", 0.0),
                    }
                )
                itens_venda.append(
                    {
                        "id": item.get("id"),
                        "nome": item["nome"],
                        "qtd": item["qtd"],
                        "preco": item["preco"],
                        "preco_compra": (item.get("row_data") or {}).get("preco_compra", 0)
                        if item["type"] == "produto"
                        else 0,
                        "type": item.get("type", "produto"),
                    }
                )

            cliente_nome = self.var_cliente.get()
            user_id_pdv = self.current_user.get("id") if isinstance(self.current_user, dict) else None
            loja_id_pdv = self.current_user.get("loja_id") if isinstance(self.current_user, dict) else None
            res_venda = self.db.registrar_venda_detalhada(
                itens_venda,
                total_geral,
                valor_desconto,
                cliente_nome,
                forma_pgto,
                vendedor=self.var_vendedor.get(),
                formas_usadas=formas_usadas,
                usuario_id=user_id_pdv,
                loja_id=loja_id_pdv,
                registrar_entrada=True,
                transacao_id=transacao_id,
            )
            venda_id = None
            if isinstance(res_venda, tuple):
                if not res_venda[0]:
                    if not confirmar_automatico:
                        messagebox.showerror("Erro", f"Não foi possível registrar a venda: {res_venda[1]}", parent=self)
                    return False, res_venda[1]
                venda_ids = res_venda[1]
                if venda_ids:
                    venda_id = venda_ids[0]

            if any(f["forma"] == "Fiado" for f in formas_usadas) and cliente_nome != "Consumidor Final":
                try:
                    c_df = self.df_clientes[self.df_clientes["nome"] == cliente_nome]
                    if not c_df.empty:
                        c_id = int(c_df.iloc[0]["id"])
                        valor_fiado_total = sum(f["valor"] for f in formas_usadas if f["forma"] == "Fiado")
                        if valor_fiado_total <= 0:
                            valor_fiado_total = total_com_desconto
                        desc_fiado = obs_venda or f"Venda PDV #{venda_id or ''} ({len(itens_venda)} itens)"
                        self.db.registrar_fiado(c_id, round(valor_fiado_total, 2), desc_fiado)
                except Exception as exc:
                    logger.warning("Erro ao registrar fiado: %s", exc)

            config_nota = self.db.get_config_nota()

            # --- Emissão NFC-e Modelo 65 (quando habilitada) ---
            nfce_res_dict = None
            if self.var_emitir_nfce.get() and (config_nota.get("token_api") or getattr(self.app, "fiscal_service", None)):
                try:
                    from services.fiscal_service import FiscalService
                    fsrv = getattr(self.app, "fiscal_service", None) or FiscalService(
                        self.db, outbox_service=getattr(self.app, "outbox_service", None)
                    )
                    cpf_nota = self.entry_cpf_nota.get().strip() if hasattr(self, "entry_cpf_nota") else ""
                    f_res = fsrv.emitir_para_venda(
                        transacao_id,
                        consumidor_cpf_cnpj=cpf_nota,
                        tef_integrado=tef_integrado,
                        troco=troco_real,
                        outbox_service=getattr(self.app, "outbox_service", None),
                    )
                    nfce_res_dict = {
                        "sucesso": f_res.sucesso,
                        "status": f_res.status,
                        "chave_acesso": f_res.chave_acesso,
                        "numero": f_res.numero,
                        "serie": f_res.serie,
                        "protocolo": f_res.protocolo,
                        "qrcode_url": f_res.qrcode_url,
                        "url_danfe": f_res.url_danfe,
                        "ambiente": config_nota.get("ambiente") or "homologacao",
                        "mensagem_sefaz": f_res.mensagem_sefaz,
                    }
                    if f_res.status == "rejeitado" and not confirmar_automatico:
                        messagebox.showwarning("NFC-e Rejeitada pela SEFAZ", f_res.mensagem_sefaz, parent=self)
                except Exception as f_exc:
                    logger.warning("Erro na emissão NFC-e: %s", f_exc)

            # Gerar Recibo / DANFE
            try:
                dados_cliente = {"nome": cliente_nome}
                contato_cliente = self.entry_contato_cliente.get().strip()
                if cliente_nome != "Consumidor Final" and not self.df_clientes.empty:
                    c_row = self.df_clientes[self.df_clientes["nome"] == cliente_nome].iloc[0]
                    dados_cliente["contato"] = c_row.get("contato", contato_cliente) or contato_cliente
                if contato_cliente:
                    dados_cliente["contato"] = contato_cliente

                file_path, text_content, wpp_content = invoice_generator.gerar_nota_txt(
                    itens_nota, dados_cliente, total_com_desconto, desconto=valor_desconto, abrir_arquivo=False, config_nota=config_nota
                )
                logo_path = None
                logo_file = config_nota.get("empresa_logo", "")
                if logo_file:
                    cand_logo = os.path.join(self.db.data_dir, "product_images", logo_file)
                    if os.path.isfile(cand_logo):
                        logo_path = cand_logo
                if hasattr(self.app, "_ultimo_cupom"):
                    self.app._ultimo_cupom = {
                        "text": text_content,
                        "logo_path": logo_path,
                        "venda_id": venda_id,
                        "file_path": file_path,
                    }

                if imprimir_automatico is not None:
                    resp = bool(imprimir_automatico)
                elif self.var_imprimir_direto.get():
                    resp = True
                elif confirmar_automatico:
                    resp = False
                else:
                    resp = messagebox.askyesno(
                        "Imprimir Recibo",
                        "Venda finalizada com sucesso!\n\nDeseja imprimir o recibo agora?",
                        parent=self,
                    )

                if resp:
                    try:
                        printer = get_printer()
                        if nfce_res_dict and nfce_res_dict.get("status") == "autorizado":
                            printer.imprimir_danfe_nfce(
                                nfce_res_dict,
                                itens_nota,
                                config_nota,
                                valor_total=total_com_desconto,
                                valor_desconto=valor_desconto,
                                valor_troco=troco_real,
                                logo_path=logo_path,
                                venda_id=venda_id,
                            )
                        elif nfce_res_dict and nfce_res_dict.get("status") in ("pendente_emissao", "processando"):
                            printer.imprimir_comprovante_pendente_nfce(
                                transacao_id,
                                itens_nota,
                                config_nota,
                                valor_total=total_com_desconto,
                                valor_desconto=valor_desconto,
                                logo_path=logo_path,
                                venda_id=venda_id,
                            )
                        else:
                            printer.print_text(text_content, logo_path=logo_path, venda_id=venda_id)
                            if config_nota.get("imprimir_segunda_via", 0):
                                printer.print_text("=== SEGUNDA VIA - VIA CLIENTE ===\n\n" + text_content, logo_path=logo_path, venda_id=venda_id)
                        if not confirmar_automatico:
                            messagebox.showinfo("Sucesso", "Documento enviado para impressão!", parent=self)
                    except Exception as exc:
                        logger.warning("Erro ao imprimir: %s", exc)
                        if not confirmar_automatico:
                            messagebox.showwarning(
                                "Aviso", f"Não foi possível imprimir diretamente.\nO recibo foi salvo em:\n{file_path}", parent=self
                            )
                            try:
                                if os.name == "nt":
                                    os.startfile(file_path)
                            except Exception:
                                pass

                    if contato_cliente and not confirmar_automatico:
                        enviar_wpp = messagebox.askyesno(
                            "Enviar por WhatsApp",
                            f"Deseja enviar o recibo por WhatsApp para {contato_cliente}?",
                            parent=self,
                        )
                        if enviar_wpp and hasattr(self.app, "_enviar_whatsapp_resiliente"):
                            sucesso, info_env = self.app._enviar_whatsapp_resiliente(
                                contato_cliente, wpp_content or text_content, metadata={"origem": "recibo_venda"}
                            )
                            if sucesso:
                                messagebox.showinfo("WhatsApp (Outbox)", f"{info_env}\nEnvio assíncrono garantido.", parent=self)
                            else:
                                messagebox.showwarning("WhatsApp", f"Não foi possível enfileirar: {info_env}", parent=self)
                elif not confirmar_automatico:
                    messagebox.showinfo("Sucesso", "Venda finalizada!", parent=self)
            except Exception as exc:
                logger.warning("Erro ao gerar recibo: %s", exc)
                if not confirmar_automatico:
                    messagebox.showinfo("Sucesso", "Venda finalizada! (Erro ao gerar recibo)", parent=self)

            self.db.limpar_rascunho_pdv(self.user_id)
            if hasattr(self.app, "refresh_dashboard"):
                self.app.refresh_dashboard()

            if self.var_fila_continua.get():
                self._suspend_draft_save = True
                try:
                    self.carrinho.clear()
                    self.update_cart_ui()
                    self.entry_desconto.delete(0, "end")
                    self.entry_obs_venda.delete(0, "end")
                    if hasattr(self, "entry_cpf_nota"):
                        self.entry_cpf_nota.delete(0, "end")
                    for p in list(self.pagamentos_list[1:]):
                        try:
                            p["frame"].destroy()
                            self.pagamentos_list.remove(p)
                        except Exception:
                            pass
                    if self.pagamentos_list:
                        self.pagamentos_list[0]["valor_entry"].delete(0, "end")
                    try:
                        df_p_new = self.db.listar_produtos()
                        est_map = {int(r["id"]): int(r["quantidade"]) for _, r in df_p_new.iterrows()} if not df_p_new.empty else {}
                        for it in self.all_items_list:
                            if it.get("type") == "produto" and int(it.get("id", 0)) in est_map:
                                it["quantidade"] = est_map[int(it["id"])]
                        self._populate_product_list()
                    except Exception:
                        pass
                finally:
                    self._suspend_draft_save = False
                self._notify("Venda Registrada", f"Venda #{venda_id or ''} de R$ {total_com_desconto:.2f} concluída!", "info")
                try:
                    self.entry_scan.focus_set()
                except Exception:
                    pass
            elif not confirmar_automatico:
                self.close()

            return True, {
                "venda_id": venda_id,
                "transacao_id": transacao_id,
                "total": total_com_desconto,
                "forma_pagamento": forma_pgto,
                "tef_integrado": tef_integrado,
                "nfce": nfce_res_dict,
            }
        except Exception as exc:
            if not confirmar_automatico:
                messagebox.showerror("Erro Fatal", f"Ocorreu um erro ao processar venda: {exc}", parent=self)
            return False, str(exc)
