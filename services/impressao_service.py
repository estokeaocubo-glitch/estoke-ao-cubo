"""
Serviço de impressão térmica, pré-visualização de cupom, diagnóstico e abertura
de gaveta com autorização de supervisor (fila assíncrona da F1a).
Imports pesados (win32print, PIL, printer) são carregados de forma preguiçosa.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Callable

logger = logging.getLogger("EstokeAoCubo.services.impressao")

PERFIS_ISENTOS_SUPERVISOR = ("admin", "gerente")


class ImpressaoService:
    """Lógica de negócio pura para impressão, preview, diagnóstico e gaveta."""

    def __init__(
        self,
        db: Any = None,
        printer_factory: Callable[[], Any] | None = None,
        config_module: Any = None,
        operador: dict[str, Any] | None = None,
    ):
        self.db = db
        self._printer_factory = printer_factory
        self._config_module = config_module
        self.operador = operador

    def _get_printer(self) -> Any:
        if self._printer_factory is not None:
            return self._printer_factory()
        from printer import get_printer

        return get_printer()

    def _get_config_module(self) -> Any:
        if self._config_module is not None:
            return self._config_module
        import config

        return config

    def _validar_autorizacao_supervisor(
        self,
        autorizacao: dict[str, Any] | None,
        operador: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Aplica exatamente a mesma regra de supervisor da sangria:
        - Operador com perfil 'admin' ou 'gerente' tem isenção automática quando
          autorizacao não é passada;
        - Caso contrário, exige dict aprovado de supervisor ('admin'/'gerente')
          ou lança PermissionError.
        """
        op_ctx = operador if operador is not None else self.operador
        if (
            autorizacao is None
            and isinstance(op_ctx, dict)
            and op_ctx.get("perfil") in PERFIS_ISENTOS_SUPERVISOR
        ):
            return {
                "approved": True,
                "supervisor_id": op_ctx.get("id"),
                "supervisor_nome": op_ctx.get("nome", "Supervisor"),
                "supervisor_perfil": op_ctx.get("perfil"),
                "motivo": "",
            }

        if not isinstance(autorizacao, dict) or autorizacao.get("approved") is not True:
            raise PermissionError(
                "Operação negada: abertura de gaveta exige autorização válida de supervisor (admin ou gerente)."
            )

        perfil_sup = autorizacao.get("supervisor_perfil")
        if perfil_sup is not None and perfil_sup not in PERFIS_ISENTOS_SUPERVISOR:
            raise PermissionError(
                f"Perfil '{perfil_sup}' não possui permissão de supervisor para abrir a gaveta."
            )

        sup_id = autorizacao.get("supervisor_id")
        sup_nome = str(autorizacao.get("supervisor_nome") or "").strip()
        if sup_id is None and not sup_nome:
            raise PermissionError(
                "Autorização de supervisor incompleta: identificação do autorizador ausente."
            )
        return autorizacao

    # ------------------------------------------------------------------
    # Reimpressão, listagem, configuração, teste e diagnóstico
    # ------------------------------------------------------------------

    def reimprimir_ultimo_cupom(
        self,
        ultimo_cupom: dict[str, Any] | None = None,
        auditor: Any = None,
    ) -> dict[str, Any]:
        """Reimprime o último cupom da sessão ou recupera do cache do ReceiptAuditor."""
        if ultimo_cupom and ultimo_cupom.get("text"):
            printer = self._get_printer()
            printer.print_text(
                ultimo_cupom["text"],
                logo_path=ultimo_cupom.get("logo_path"),
                venda_id=ultimo_cupom.get("venda_id"),
            )
            if self.db is not None:
                self.db.registrar_alteracao(
                    "Reimprimir Cupom",
                    f"Reimpressão do último cupom (Venda #{ultimo_cupom.get('venda_id') or 'N/A'})",
                    entidade="impressoes",
                    entidade_id=str(ultimo_cupom.get("venda_id") or ""),
                )
            return {
                "origem": "sessao",
                "venda_id": ultimo_cupom.get("venda_id"),
                "mensagem": "Último cupom reenviado para impressão!",
            }

        if auditor is None:
            from printer import ReceiptAuditor

            auditor = ReceiptAuditor()

        cached = auditor.recuperar_ultimo_recibo()
        if cached and "=== INICIO RECIBO" in cached:
            self._get_printer().print_text(cached)
            if self.db is not None:
                self.db.registrar_alteracao(
                    "Reimprimir Cupom", "Reimpressão do último recibo em cache."
                )
            return {
                "origem": "cache",
                "venda_id": None,
                "mensagem": "Último recibo do histórico enviado para impressão!",
            }

        raise LookupError("Nenhum cupom recente disponível para reimprimir.")

    def listar_impressoras(self) -> list[str]:
        """Lista impressoras instaladas no Windows usando import preguiçoso de win32print."""
        try:
            import win32print

            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            printers = win32print.EnumPrinters(flags)
            return [str(p[2]) for p in printers if len(p) >= 3 and p[2]]
        except Exception as exc:
            logger.debug("Listagem de impressoras indisponível: %s", exc)
            return []

    def salvar_config_impressora(
        self,
        *,
        enabled: bool,
        tipo: str,
        printer_name: str = "",
        interface: str = "file",
        params_json: str = "{}",
    ) -> dict[str, Any]:
        """Monta e persiste a configuração da impressora no módulo config."""
        newcfg: dict[str, Any] = {
            "enabled": bool(enabled),
            "type": str(tipo or "text"),
            "interface": str(interface or "file") if tipo == "escpos" else "file",
            "settings": {},
        }
        if tipo == "windows":
            newcfg["settings"]["printer_name"] = str(printer_name or "")
        elif tipo == "escpos":
            try:
                parsed = json.loads(params_json or "{}")
                if isinstance(parsed, dict):
                    newcfg["settings"] = parsed
            except Exception:
                pass
        cfg_mod = self._get_config_module()
        cfg_mod.set_printer_config(newcfg)
        return newcfg

    def testar_impressora(self) -> dict[str, Any]:
        """Envia cupom de teste para a impressora configurada."""
        printer = self._get_printer()
        printer.print_text("== TESTE DE IMPRESSÃO ==\n")
        return {
            "enviado": True,
            "mensagem": "Envio concluído (ou mensagem registrada).",
        }

    def diagnosticar(self) -> dict[str, Any]:
        """Executa diagnóstico da configuração, fila de impressão e spooler do Windows."""
        cfg_mod = self._get_config_module()
        cfg = cfg_mod.get_printer_config()
        impressoras_win = self.listar_impressoras()
        driver_nome = ""
        ok = True
        erro = None
        try:
            printer = self._get_printer()
            driver_nome = type(printer).__name__
        except Exception as exc:
            ok = False
            erro = str(exc)

        return {
            "ok": ok,
            "config": cfg,
            "driver": driver_nome,
            "impressoras_windows": impressoras_win,
            "erro": erro,
        }

    # ------------------------------------------------------------------
    # Pré-visualização do cupom (texto + imagem PNG via PIL)
    # ------------------------------------------------------------------

    def _carregar_dados_ultima_venda(self) -> tuple[list[dict[str, Any]], dict[str, Any], float, float, Any]:
        """Coleta dados da última venda em um único bloco de lock (com fallback de exemplo)."""
        if self.db is not None:
            try:
                with self.db._lock:
                    cursor = self.db.conn.cursor()
                    cursor.execute("SELECT id, data_venda, cliente FROM vendas ORDER BY id DESC LIMIT 1")
                    last_venda = cursor.fetchone()
                    if last_venda:
                        v_id, data_venda, cliente_nome = last_venda
                        cursor.execute(
                            """
                            SELECT p.nome, v.quantidade, (v.valor_total / v.quantidade) as preco, v.desconto
                            FROM vendas v
                            LEFT JOIN produtos p ON v.produto_id = p.id
                            WHERE v.data_venda = ? AND v.cliente = ?
                            """,
                            (data_venda, cliente_nome),
                        )
                        rows = cursor.fetchall()

                        itens: list[dict[str, Any]] = []
                        total_bruto = 0.0
                        total_desconto = 0.0
                        for r in rows:
                            nome_p = r[0] or "Produto"
                            qtd = r[1] or 1
                            preco = r[2] or 0.0
                            desc_item = r[3] or 0.0
                            total_desconto += desc_item
                            total_bruto += preco * qtd
                            itens.append({"nome": nome_p, "qtd": qtd, "preco": preco})

                        cliente: dict[str, Any] = {"nome": cliente_nome}
                        if cliente_nome != "Consumidor Final":
                            cursor.execute("SELECT contato FROM clientes WHERE nome=?", (cliente_nome,))
                            c_row = cursor.fetchone()
                            if c_row:
                                cliente["contato"] = c_row[0]
                        return itens, cliente, total_bruto - total_desconto, total_desconto, v_id
            except Exception:
                pass

        return (
            [
                {"nome": "Produto Exemplo A", "qtd": 2, "preco": 49.90},
                {"nome": "Produto Exemplo B", "qtd": 1, "preco": 29.90},
            ],
            {"nome": "Cliente Teste", "contato": "(11) 99999-0000"},
            119.70,
            10.00,
            "PREVIEW",
        )

    def gerar_preview_cupom(
        self,
        preview_cfg: dict[str, Any],
        destino_png: str | None = None,
    ) -> dict[str, Any]:
        """Gera texto da nota e renderiza imagem PNG de pré-visualização (sem abrir janela)."""
        import invoice_generator

        itens, cliente, total_liquido, total_desconto, v_id = self._carregar_dados_ultima_venda()
        _, text_content, _ = invoice_generator.gerar_nota_txt(
            itens,
            cliente,
            total_liquido,
            id_venda=v_id,
            desconto=total_desconto,
            abrir_arquivo=False,
            config_nota=preview_cfg,
        )
        if not text_content:
            raise RuntimeError("Erro ao gerar pré-visualização.")

        from PIL import Image, ImageDraw, ImageFont

        margin = 12
        receipt_width = 360
        font_size = 14
        title_font_size = 20
        line_spacing = 2

        try:
            font = ImageFont.truetype("consola.ttf", font_size)
            title_font = ImageFont.truetype("consola.ttf", title_font_size)
        except Exception:
            try:
                font = ImageFont.truetype("cour.ttf", font_size)
                title_font = ImageFont.truetype("cour.ttf", title_font_size)
            except Exception:
                font = ImageFont.load_default()
                title_font = font

        lines = text_content.split("\n")
        dummy = Image.new("RGB", (1, 1))
        dd = ImageDraw.Draw(dummy)
        bbox = dd.textbbox((0, 0), "Ag", font=font)
        line_h = bbox[3] - bbox[1] + line_spacing
        bbox_t = dd.textbbox((0, 0), "Ag", font=title_font)
        title_h = bbox_t[3] - bbox_t[1] + line_spacing

        logo_img = None
        logo_h = 0
        logo_file = preview_cfg.get("empresa_logo")
        if logo_file and self.db is not None:
            logo_full = os.path.join(self.db.data_dir, "product_images", logo_file)
            if os.path.isfile(logo_full):
                logo_img = Image.open(logo_full)
                logo_img.thumbnail((receipt_width - 2 * margin, 80), Image.LANCZOS)
                logo_h = logo_img.size[1]

        title_line = None
        text_lines: list[str] = []
        found = False
        for line_txt in lines:
            s = line_txt.strip()
            if not found and s:
                title_line = s
                found = True
            else:
                text_lines.append(line_txt)
        while text_lines and not text_lines[-1].strip():
            text_lines.pop()

        logo_gap = 8 if logo_img else 0
        text_height = len(text_lines) * line_h + (title_h if title_line else 0)
        total_h = margin + logo_h + logo_gap + text_height + margin

        img = Image.new("RGB", (receipt_width, total_h), "white")
        draw = ImageDraw.Draw(img)
        y = margin

        if logo_img:
            lx = (receipt_width - logo_img.size[0]) // 2
            if logo_img.mode == "RGBA":
                img.paste(logo_img, (lx, y), logo_img)
            else:
                img.paste(logo_img, (lx, y))
            y += logo_h + logo_gap

        if title_line:
            tb = draw.textbbox((0, 0), title_line, font=title_font)
            tw = tb[2] - tb[0]
            draw.text(((receipt_width - tw) // 2, y), title_line, fill="black", font=title_font)
            y += title_h + 2

        for line_txt in text_lines:
            stripped = line_txt.strip()
            if stripped and all(c == "-" for c in stripped):
                tb = draw.textbbox((0, 0), stripped, font=font)
                tw = tb[2] - tb[0]
                draw.text(((receipt_width - tw) // 2, y), stripped, fill="black", font=font)
            else:
                draw.text((margin, y), line_txt, fill="black", font=font)
            y += line_h

        img = img.crop((0, 0, receipt_width, y + margin))
        png_path = destino_png or os.path.join(tempfile.gettempdir(), "estokeaocubo_preview.png")
        img.save(png_path, "PNG")

        return {"texto": text_content, "png_path": png_path, "venda_id": v_id}

    # ------------------------------------------------------------------
    # Gaveta de dinheiro (com autorização de supervisor + fila F1a)
    # ------------------------------------------------------------------

    def abrir_gaveta(
        self,
        autorizacao: dict[str, Any] | None = None,
        *,
        caixa_id: int | None = None,
        operador: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Envia pulso assíncrono de abertura para a gaveta exigindo autorização
        de supervisor (mesma regra da sangria) e registrando trilha de auditoria.
        """
        auth_info = self._validar_autorizacao_supervisor(autorizacao, operador=operador)

        printer = self._get_printer()
        printer.abrir_gaveta()

        sup_id = auth_info.get("supervisor_id")
        sup_nome = auth_info.get("supervisor_nome") or "Supervisor"
        if self.db is not None:
            cx_label = f"Caixa #{caixa_id}" if caixa_id is not None else "Caixa N/A"
            self.db.registrar_alteracao(
                "Abrir Gaveta",
                f"Gaveta aberta manualmente no {cx_label} | Autorizado por {sup_nome} (ID: {sup_id})",
                usuario_id=sup_id,
                entidade="caixa",
                entidade_id=str(caixa_id or ""),
            )

        return {
            "enviado": True,
            "caixa_id": caixa_id,
            "supervisor_id": sup_id,
            "supervisor_nome": sup_nome,
        }
