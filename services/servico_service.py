"""
Serviço de Ordens de Serviço (OS) e Comandas/Divisão de Conta (Estoke ao Cubo).

Contrato de serviço:
- Python puro, sem dependências de UI (tkinter/customtkinter).
- Dependências injetadas no construtor (db, invoice_gen, printer_factory, outbox).
- Retorna dados de domínio e lança exceções de domínio (sem envelope {ok, data, error}).
- Nota do cliente e mensagem WhatsApp nunca incluem custo interno.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional


class ServicoService:
    """Centraliza operações de OS, geração de nota sem custo interno e fechamento/divisão de comandas."""

    def __init__(
        self,
        db: Any,
        invoice_gen: Any = None,
        printer_factory: Optional[Callable[[], Any]] = None,
        outbox: Any = None,
    ):
        self.db = db
        self._invoice_gen = invoice_gen
        self._printer_factory = printer_factory
        self._outbox = outbox

    def _get_invoice_gen(self) -> Any:
        if self._invoice_gen is None:
            import invoice_generator
            self._invoice_gen = invoice_generator
        return self._invoice_gen

    def registrar_os(
        self,
        cliente: str,
        descricao: str,
        valor: float,
        custo: float = 0.0,
        forma_pagamento: str = "Dinheiro",
        observacao: str = "",
        vendedor: str = "",
        status_os: str = "Concluído",
        contato_cliente: str = "",
        equipamento: str = "",
    ) -> dict[str, Any]:
        """Valida e registra uma Ordem de Serviço no banco de dados."""
        desc_limpa = str(descricao or "").strip()
        if not desc_limpa:
            raise ValueError("Descreva o serviço realizado.")

        try:
            valor_f = float(str(valor).replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise ValueError("Valor inválido.") from exc
        if valor_f <= 0:
            raise ValueError("Valor inválido.")

        try:
            custo_str = str(custo).strip() if custo is not None else ""
            custo_f = float(custo_str.replace(",", ".")) if custo_str else 0.0
        except (TypeError, ValueError):
            custo_f = 0.0

        ok, result = self.db.registrar_servico(
            cliente,
            desc_limpa,
            valor_f,
            custo_f,
            forma_pagamento,
            str(observacao or "").strip(),
            vendedor=str(vendedor or "").strip(),
            status_os=status_os,
            contato_cliente=str(contato_cliente or "").strip(),
            equipamento=str(equipamento or "").strip(),
        )
        if not ok:
            raise RuntimeError(f"Erro ao registrar: {result}")

        return {
            "servico_id": result,
            "cliente": cliente,
            "descricao": desc_limpa,
            "valor": valor_f,
            "custo": custo_f,
            "lucro": round(valor_f - custo_f, 2),
            "forma_pagamento": forma_pagamento,
            "observacao": str(observacao or "").strip(),
            "vendedor": str(vendedor or "").strip(),
            "status_os": status_os,
            "contato_cliente": str(contato_cliente or "").strip(),
            "equipamento": str(equipamento or "").strip(),
        }

    def avancar_status(self, servico_id: int, status_atual: str, statuses_fluxo: list[str]) -> str:
        """Avança a OS para o próximo status da trilha configurada e persiste no banco."""
        if not statuses_fluxo:
            raise ValueError("Lista de status de OS não pode ser vazia.")
        idx = statuses_fluxo.index(status_atual) if status_atual in statuses_fluxo else 0
        next_st = statuses_fluxo[min(idx + 1, len(statuses_fluxo) - 1)]
        self.db.atualizar_status_servico(servico_id, next_st)
        return next_st

    def gerar_nota_os(
        self,
        servico_id: Any,
        descricao: str,
        valor: float,
        cliente: str,
        contato: str = "",
        forma_pagamento: str = "Dinheiro",
        observacao: str = "",
        abrir_arquivo: bool = False,
    ) -> dict[str, Any]:
        """
        Gera nota de serviço para o cliente (NUNCA inclui custo interno).
        Resolve também o caminho do logotipo da empresa quando disponível.
        """
        config_nota = self.db.get_config_nota() or {}
        dados_cliente: dict[str, Any] = {"nome": cliente}
        if contato:
            dados_cliente["contato"] = contato

        inv_gen = self._get_invoice_gen()
        file_path, text_content, wpp_content = inv_gen.gerar_nota_servico(
            descricao,
            valor,
            dados_cliente,
            id_servico=servico_id,
            forma_pagamento=forma_pagamento,
            observacao=observacao,
            abrir_arquivo=abrir_arquivo,
            config_nota=config_nota,
        )

        logo_path = None
        logo_file = config_nota.get("empresa_logo", "")
        if logo_file and hasattr(self.db, "data_dir"):
            cand = os.path.join(self.db.data_dir, "product_images", logo_file)
            if os.path.isfile(cand):
                logo_path = cand

        return {
            "file_path": file_path,
            "text_content": text_content,
            "wpp_content": wpp_content,
            "logo_path": logo_path,
        }

    def montar_mensagem_whatsapp_os(
        self,
        wpp_content: Optional[str] = None,
        text_content: Optional[str] = None,
        servico_id: Any = None,
        descricao: str = "",
        valor: float = 0.0,
        cliente: str = "",
    ) -> str:
        """Monta o texto da mensagem WhatsApp da OS (sem envio real e sem custo interno)."""
        if wpp_content and str(wpp_content).strip():
            return str(wpp_content)
        if text_content and str(text_content).strip():
            return str(text_content)
        return (
            f"Ordem de Serviço #{servico_id}\n"
            f"Cliente: {cliente}\n"
            f"Serviço: {descricao}\n"
            f"Valor: R$ {float(valor):.2f}"
        )

    def fechar_comanda(
        self,
        comanda_id: int,
        forma_pagamento: str,
        aplicar_taxa_10: bool = False,
        desconto: float = 0.0,
        observacao: str = "",
    ) -> dict[str, Any]:
        """Calcula taxa de 10%/desconto e fecha a comanda aberta no banco."""
        comanda = self.db.get_comanda(comanda_id)
        if not comanda or comanda.get("status") != "aberta":
            raise ValueError("Comanda não encontrada ou já fechada.")

        subtotal = float(comanda.get("total") or 0.0)
        taxa = round(subtotal * 0.10, 2) if aplicar_taxa_10 else 0.0
        try:
            desc_f = float(str(desconto).replace(",", ".")) if desconto not in (None, "") else 0.0
        except (TypeError, ValueError):
            desc_f = 0.0

        obs_limpa = str(observacao or "").strip()
        ok, msg = self.db.fechar_comanda(comanda_id, forma_pagamento, taxa, desc_f, obs_limpa)
        if not ok:
            raise RuntimeError(str(msg))

        return {
            "comanda_id": comanda_id,
            "subtotal": subtotal,
            "taxa_servico": taxa,
            "desconto": desc_f,
            "total_final": max(subtotal + taxa - desc_f, 0.0),
            "forma_pagamento": forma_pagamento,
            "mensagem": msg,
        }

    def dividir_conta(self, comanda_id: int, num_pessoas: int) -> dict[str, Any]:
        """
        Divide o total da comanda pelo número de pessoas delegando ao Database.
        Preserva integralmente o comportamento atual de arredondamento.
        """
        try:
            n = int(num_pessoas)
        except (TypeError, ValueError):
            n = 1
        if n < 1:
            n = 1

        resultado = self.db.dividir_conta(comanda_id, n)
        if not resultado:
            raise ValueError("Comanda não encontrada.")
        return resultado
