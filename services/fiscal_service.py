# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Serviço Fiscal NFC-e Modelo 65 (Arquitetura de Adaptadores REST)
Suporta provedores externos via API REST (Focus NFe como adaptador inicial)
sem exigir certificado A1 local ou ACBr, com validação estrita de NCM/centavos,
sanitização de segredos e fila Outbox para emissão pendente sem fingir tpEmis=9.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

logger = logging.getLogger("EstokeAoCubo.fiscal")

FiscalStatus = Literal[
    "autorizado",
    "processando",
    "rejeitado",
    "pendente_emissao",
    "cancelado",
    "cancelamento_recusado",
    "erro_comunicacao",
]

_SECRET_KEYS = {"token_api", "token", "csc", "id_csc", "authorization", "access_token", "secret"}


def _only_digits(val: Any) -> str:
    return re.sub(r"\D", "", str(val or ""))


def sanitizar_payload_fiscal(obj: Any) -> Any:
    """Remove recursivamente segredos fiscais (token_api, csc, id_csc, Authorization)."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                cleaned[k] = "***REDACTED***"
            else:
                cleaned[k] = sanitizar_payload_fiscal(v)
        return cleaned
    if isinstance(obj, list):
        return [sanitizar_payload_fiscal(item) for item in obj]
    return obj


@dataclass
class FiscalItemPayload:
    numero_item: int
    produto_id: int
    codigo: str
    descricao: str
    ncm: str = "00000000"
    cfop: str = "5102"
    unidade: str = "UN"
    quantidade: float = 1.0
    valor_unitario: float = 0.0
    valor_bruto: float = 0.0
    valor_desconto: float = 0.0
    origem: str = "0"
    cst_csosn: str = "102"


@dataclass
class FiscalPagamentoPayload:
    forma_sefaz: str
    valor: float
    tipo_integracao: str = "2"  # "1" = TEF/PIX integrado, "2" = POS avulso / dinheiro


@dataclass
class FiscalDocumentoPayload:
    referencia: str
    ambiente: Literal["homologacao", "producao"] = "homologacao"
    emitente_cnpj: str = ""
    emitente_nome: str = ""
    emitente_ie: str = ""
    emitente_regime_tributario: str = "1"
    csc: str = ""
    id_csc: str = ""
    consumidor_cpf_cnpj: str = ""
    consumidor_nome: str = ""
    itens: list[FiscalItemPayload] = field(default_factory=list)
    pagamentos: list[FiscalPagamentoPayload] = field(default_factory=list)
    valor_total: float = 0.0
    valor_desconto: float = 0.0
    valor_troco: float = 0.0
    presenca_comprador: str = "1"
    modalidade_frete: str = "9"


@dataclass
class FiscalEmitResult:
    sucesso: bool
    status: FiscalStatus
    referencia: str
    chave_acesso: str = ""
    numero: str = ""
    serie: str = ""
    protocolo: str = ""
    qrcode_url: str = ""
    url_danfe: str = ""
    xml_url: str = ""
    mensagem_sefaz: str = ""
    contingencia: bool = False
    raw_response: dict[str, Any] = field(default_factory=dict)


def ajustar_centavos_documento(doc: FiscalDocumentoPayload) -> None:
    """Garante fechamento exato de centavos entre soma dos itens, descontos e valor total."""
    if not doc.itens:
        return
    soma_bruta = 0.0
    soma_desc = 0.0
    for it in doc.itens:
        it.valor_bruto = round(float(it.quantidade) * float(it.valor_unitario), 2)
        it.valor_desconto = round(max(0.0, float(it.valor_desconto)), 2)
        soma_bruta = round(soma_bruta + it.valor_bruto, 2)
        soma_desc = round(soma_desc + it.valor_desconto, 2)

    liquido_calculado = round(soma_bruta - soma_desc, 2)
    alvo_total = round(float(doc.valor_total), 2)
    diff = round(liquido_calculado - alvo_total, 2)

    # Absorve diferença residual de centavos no desconto do último item compatível
    if abs(diff) >= 0.009 and doc.itens:
        ultimo = doc.itens[-1]
        novo_desc = round(ultimo.valor_desconto + diff, 2)
        if 0.0 <= novo_desc <= ultimo.valor_bruto:
            ultimo.valor_desconto = novo_desc
            doc.valor_desconto = round(sum(i.valor_desconto for i in doc.itens), 2)


def validar_documento_nfce(doc: FiscalDocumentoPayload) -> None:
    """Valida regras fiscais mínimas de NFC-e antes de transmitir ao provedor REST."""
    if not doc.referencia:
        raise ValueError("Referência (transacao_id) obrigatória para emissão de NFC-e.")
    if not doc.itens:
        raise ValueError("A NFC-e deve conter ao menos um item.")
    cnpj = _only_digits(doc.emitente_cnpj)
    if len(cnpj) != 14:
        raise ValueError("CNPJ do emitente inválido (deve conter 14 dígitos).")
    if doc.ambiente not in ("homologacao", "producao"):
        raise ValueError(f"Ambiente fiscal inválido: {doc.ambiente!r}.")

    ajustar_centavos_documento(doc)

    for it in doc.itens:
        ncm = _only_digits(it.ncm or "")
        it.ncm = ncm.zfill(8) if ncm and len(ncm) <= 8 else ncm
        if not re.fullmatch(r"\d{8}", it.ncm):
            raise ValueError(f"NCM inválido ('{it.ncm}') para o item '{it.descricao}'. Deve ter 8 dígitos.")
        if doc.ambiente == "producao" and it.ncm == "00000000":
            raise ValueError(f"NCM '00000000' não é permitido em produção para o item '{it.descricao}'.")

        cfop = _only_digits(it.cfop or "5102")
        if not re.fullmatch(r"\d{4}", cfop):
            raise ValueError(f"CFOP inválido ('{it.cfop}') para o item '{it.descricao}'.")
        it.cfop = cfop

    soma_liquida_itens = round(sum(round(i.valor_bruto - i.valor_desconto, 2) for i in doc.itens), 2)
    if abs(soma_liquida_itens - round(doc.valor_total, 2)) > 0.01:
        raise ValueError(
            f"Fechamento de centavos divergente: itens líquidos R$ {soma_liquida_itens:.2f} != total R$ {doc.valor_total:.2f}."
        )

    if doc.pagamentos:
        soma_pgtos = round(sum(float(p.valor) for p in doc.pagamentos) - float(doc.valor_troco or 0.0), 2)
        if abs(soma_pgtos - round(doc.valor_total, 2)) > 0.01:
            raise ValueError(
                f"Soma de pagamentos líquida de troco (R$ {soma_pgtos:.2f}) diverge do total da nota (R$ {doc.valor_total:.2f})."
            )


def mapear_forma_sefaz(forma: str) -> str:
    """Converte nome da forma de pagamento do PDV para código tPag oficial da SEFAZ."""
    f = str(forma or "").strip().lower()
    if "pix" in f:
        return "17"
    if "créd" in f or "cred" in f:
        return "03"
    if "déb" in f or "deb" in f:
        return "04"
    if "fiado" in f:
        return "05"
    if "dinheiro" in f:
        return "01"
    return "99"


class FiscalProvider(ABC):
    """Contrato desacoplado para provedores REST de NFC-e modelo 65."""

    @abstractmethod
    def emitir_nfce(self, doc: FiscalDocumentoPayload) -> FiscalEmitResult:
        """Transmite uma NFC-e para autorização."""

    @abstractmethod
    def consultar_nfce(self, referencia: str) -> FiscalEmitResult:
        """Consulta o estado de uma NFC-e previamente enviada."""

    @abstractmethod
    def cancelar_nfce(self, referencia: str, justificativa: str) -> FiscalEmitResult:
        """Cancela uma NFC-e autorizada junto à SEFAZ."""


class FocusNFeProvider(FiscalProvider):
    """Adaptador REST inicial compatível com a API v2 da Focus NFe."""

    BASE_URLS = {
        "homologacao": "https://homologacao.focusnfe.com.br",
        "producao": "https://api.focusnfe.com.br",
    }

    def __init__(
        self,
        token_api: str,
        ambiente: str = "homologacao",
        timeout: float = 15.0,
        http_requester: Callable[[urllib.request.Request, float], tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.token_api = str(token_api or "").strip()
        self.ambiente = "producao" if str(ambiente or "").strip().lower() == "producao" else "homologacao"
        self.timeout = float(timeout)
        self._http_requester = http_requester or self._default_http_requester

    @staticmethod
    def _default_http_requester(req: urllib.request.Request, timeout: float) -> tuple[int, dict[str, Any]]:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = int(getattr(resp, "status", 200) or 200)
                body = resp.read().decode("utf-8", errors="replace")
                return code, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            try:
                parsed = json.loads(body) if body.strip() else {}
            except Exception:
                parsed = {"mensagem": body or exc.reason}
            return int(exc.code), parsed

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        if not self.token_api:
            raise ValueError("Token da API Fiscal não configurado em config_nota.")
        base = self.BASE_URLS[self.ambiente]
        url = f"{base}{path}"
        auth_b64 = base64.b64encode(f"{self.token_api}:".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {auth_b64}",
            "Content-Type": "application/json",
            "User-Agent": "EstokeAoCubo/4.6",
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=raw, headers=headers, method=method)
        return self._http_requester(req, self.timeout)

    def build_focus_payload(self, doc: FiscalDocumentoPayload) -> dict[str, Any]:
        """Monta payload JSON compatível com /v2/nfce (sem incluir segredos como token_api)."""
        payload: dict[str, Any] = {
            "natureza_operacao": "Venda ao consumidor",
            "data_emissao": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "tipo_documento": 1,
            "finalidade_emissao": 1,
            "cnpj_emitente": _only_digits(doc.emitente_cnpj),
            "nome_emitente": str(doc.emitente_nome or "Estoke Ao Cubo")[:60],
            "regime_tributario_emitente": str(doc.emitente_regime_tributario or "1"),
            "presenca_comprador": str(doc.presenca_comprador or "1"),
            "modalidade_frete": str(doc.modalidade_frete or "9"),
            "valor_desconto": round(float(doc.valor_desconto or 0.0), 2),
            "valor_troco": round(float(doc.valor_troco or 0.0), 2),
            "valor_total": round(float(doc.valor_total or 0.0), 2),
            "items": [
                {
                    "numero_item": int(it.numero_item),
                    "codigo_produto": str(it.codigo)[:60],
                    "descricao": str(it.descricao)[:120],
                    "codigo_ncm": str(it.ncm),
                    "cfop": str(it.cfop),
                    "unidade_comercial": str(it.unidade or "UN")[:6].upper(),
                    "quantidade_comercial": round(float(it.quantidade), 4),
                    "valor_unitario_comercial": round(float(it.valor_unitario), 4),
                    "valor_bruto": round(float(it.valor_bruto), 2),
                    "valor_desconto": round(float(it.valor_desconto), 2),
                    "icms_origem": str(it.origem or "0"),
                    "icms_situacao_tributaria": str(it.cst_csosn or "102"),
                }
                for it in doc.itens
            ],
            "formas_pagamento": [
                {
                    "forma_pagamento": str(pg.forma_sefaz),
                    "valor_pagamento": round(float(pg.valor), 2),
                    "tipo_integracao": str(pg.tipo_integracao or "2"),
                }
                for pg in doc.pagamentos
            ],
        }
        if doc.emitente_ie:
            payload["inscricao_estadual_emitente"] = _only_digits(doc.emitente_ie)
        doc_cli = _only_digits(doc.consumidor_cpf_cnpj)
        if len(doc_cli) == 11:
            payload["cpf_destinatario"] = doc_cli
        elif len(doc_cli) == 14:
            payload["cnpj_destinatario"] = doc_cli
        if doc.consumidor_nome and doc.consumidor_nome != "Consumidor Final":
            payload["nome_destinatario"] = str(doc.consumidor_nome)[:60]
        return payload

    def _parse_api_result(self, referencia: str, code: int, resp: dict[str, Any]) -> FiscalEmitResult:
        raw_sanitized = sanitizar_payload_fiscal(resp if isinstance(resp, dict) else {})
        if code in (429, 500, 502, 503, 504):
            return FiscalEmitResult(
                sucesso=False,
                status="erro_comunicacao",
                referencia=referencia,
                mensagem_sefaz=str(raw_sanitized.get("mensagem") or f"HTTP {code} transitório na API fiscal"),
                raw_response=raw_sanitized,
            )

        status_api = str(raw_sanitized.get("status") or "").strip().lower()
        msg_sefaz = str(
            raw_sanitized.get("mensagem_sefaz")
            or raw_sanitized.get("mensagem")
            or raw_sanitized.get("status_sefaz")
            or ""
        )

        if code >= 400 or status_api in ("erro_autorizacao", "rejeitado", "denegado"):
            return FiscalEmitResult(
                sucesso=False,
                status="rejeitado",
                referencia=referencia,
                mensagem_sefaz=msg_sefaz or f"Rejeição fiscal HTTP {code}",
                raw_response=raw_sanitized,
            )

        if status_api in ("processando_autorizacao", "processando"):
            return FiscalEmitResult(
                sucesso=True,
                status="processando",
                referencia=referencia,
                mensagem_sefaz=msg_sefaz or "Aguardando retorno da SEFAZ",
                raw_response=raw_sanitized,
            )

        if status_api == "cancelado":
            return FiscalEmitResult(
                sucesso=True,
                status="cancelado",
                referencia=referencia,
                chave_acesso=str(raw_sanitized.get("chave_nfe") or raw_sanitized.get("chave_acesso") or ""),
                protocolo=str(raw_sanitized.get("protocolo") or ""),
                mensagem_sefaz=msg_sefaz or "NFC-e cancelada.",
                raw_response=raw_sanitized,
            )

        return FiscalEmitResult(
            sucesso=True,
            status="autorizado",
            referencia=referencia,
            chave_acesso=str(raw_sanitized.get("chave_nfe") or raw_sanitized.get("chave_acesso") or ""),
            numero=str(raw_sanitized.get("numero") or ""),
            serie=str(raw_sanitized.get("serie") or "1"),
            protocolo=str(raw_sanitized.get("protocolo") or ""),
            qrcode_url=str(raw_sanitized.get("qrcode_url") or raw_sanitized.get("url_consulta_nf") or ""),
            url_danfe=str(raw_sanitized.get("caminho_danfe") or raw_sanitized.get("url_danfe") or ""),
            xml_url=str(raw_sanitized.get("caminho_xml_nota_fiscal") or raw_sanitized.get("xml_url") or ""),
            mensagem_sefaz=msg_sefaz or "Autorizado o uso da NFC-e",
            raw_response=raw_sanitized,
        )

    def emitir_nfce(self, doc: FiscalDocumentoPayload) -> FiscalEmitResult:
        validar_documento_nfce(doc)
        payload = self.build_focus_payload(doc)
        try:
            ref_q = urllib.parse.quote(doc.referencia, safe="")
            code, resp = self._request("POST", f"/v2/nfce?ref={ref_q}", body=payload)
            return self._parse_api_result(doc.referencia, code, resp)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            logger.warning("Falha de comunicação ao emitir NFC-e ref=%s: %s", doc.referencia, exc)
            return FiscalEmitResult(
                sucesso=False,
                status="erro_comunicacao",
                referencia=doc.referencia,
                mensagem_sefaz=f"Falha de conexão com API fiscal: {exc}",
            )

    def consultar_nfce(self, referencia: str) -> FiscalEmitResult:
        ref_q = urllib.parse.quote(str(referencia), safe="")
        try:
            code, resp = self._request("GET", f"/v2/nfce/{ref_q}")
            if code == 404:
                return FiscalEmitResult(
                    sucesso=False,
                    status="erro_comunicacao",
                    referencia=referencia,
                    mensagem_sefaz="NFC-e ainda não localizada na API fiscal (404).",
                )
            return self._parse_api_result(referencia, code, resp)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            return FiscalEmitResult(
                sucesso=False,
                status="erro_comunicacao",
                referencia=referencia,
                mensagem_sefaz=f"Falha de conexão ao consultar NFC-e: {exc}",
            )

    def cancelar_nfce(self, referencia: str, justificativa: str) -> FiscalEmitResult:
        just = str(justificativa or "").strip()
        if len(just) < 15:
            just = f"Cancelamento de venda PDV: {just or 'Desistencia do consumidor'}"
        just = just[:255]
        ref_q = urllib.parse.quote(str(referencia), safe="")
        try:
            code, resp = self._request("DELETE", f"/v2/nfce/{ref_q}", body={"justificativa": just})
            raw_sanitized = sanitizar_payload_fiscal(resp if isinstance(resp, dict) else {})
            if code in (429, 500, 502, 503, 504):
                return FiscalEmitResult(
                    sucesso=False,
                    status="erro_comunicacao",
                    referencia=referencia,
                    mensagem_sefaz=str(raw_sanitized.get("mensagem") or f"HTTP {code} ao cancelar NFC-e"),
                    raw_response=raw_sanitized,
                )
            if code >= 400 or str(raw_sanitized.get("status") or "").lower() in ("erro_cancelamento", "rejeitado"):
                return FiscalEmitResult(
                    sucesso=False,
                    status="cancelamento_recusado",
                    referencia=referencia,
                    mensagem_sefaz=str(
                        raw_sanitized.get("mensagem_sefaz")
                        or raw_sanitized.get("mensagem")
                        or f"Cancelamento recusado pela SEFAZ (HTTP {code})"
                    ),
                    raw_response=raw_sanitized,
                )
            return FiscalEmitResult(
                sucesso=True,
                status="cancelado",
                referencia=referencia,
                protocolo=str(raw_sanitized.get("protocolo") or ""),
                mensagem_sefaz=str(raw_sanitized.get("mensagem_sefaz") or "NFC-e cancelada na SEFAZ."),
                raw_response=raw_sanitized,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            return FiscalEmitResult(
                sucesso=False,
                status="erro_comunicacao",
                referencia=referencia,
                mensagem_sefaz=f"Sem conexão ao tentar cancelar NFC-e: {exc}",
            )


class FiscalService:
    """Orquestrador local de emissão, fila Outbox (pendente_emissao) e cancelamento NFC-e."""

    def __init__(self, db: Any, provider: FiscalProvider | None = None, outbox_service: Any = None) -> None:
        self.db = db
        self._explicit_provider = provider
        self.outbox_service = outbox_service
        if outbox_service is not None and hasattr(outbox_service, "register_handler"):
            outbox_service.register_handler("nfce_pendente", self.processar_outbox_nfce)

    def _get_provider(self) -> FiscalProvider:
        if self._explicit_provider is not None:
            return self._explicit_provider
        cfg = self.db.get_config_nota()
        return FocusNFeProvider(
            token_api=cfg.get("token_api", ""),
            ambiente=cfg.get("ambiente", "homologacao"),
        )

    def registrar_outbox(self, outbox_service: Any) -> None:
        self.outbox_service = outbox_service
        if outbox_service is not None:
            outbox_service.register_handler("nfce_pendente", self.processar_outbox_nfce)

    def construir_documento_da_venda(
        self,
        transacao_id: str,
        consumidor_cpf_cnpj: str = "",
        tef_integrado: bool = False,
        troco: float = 0.0,
    ) -> FiscalDocumentoPayload:
        """Lê venda + produtos + pagamentos_mp + config_nota e monta FiscalDocumentoPayload validado."""
        cfg = self.db.get_config_nota()
        with self.db._lock:
            cur = self.db.conn.cursor()
            rows = cur.execute(
                """
                SELECT v.id, v.produto_id, v.quantidade, v.valor_total, COALESCE(v.desconto, 0),
                       v.cliente, v.forma_pagamento,
                       COALESCE(v.valor_dinheiro, 0), COALESCE(v.valor_pix, 0),
                       COALESCE(v.valor_cartao_debito, 0), COALESCE(v.valor_cartao_credito, 0),
                       COALESCE(v.valor_fiado, 0),
                       COALESCE(p.nome, 'Item PDV'), COALESCE(p.codigo_barras, ''),
                       COALESCE(p.ncm, '00000000'), COALESCE(p.cfop, '5102'),
                       COALESCE(p.cst_csosn, '102'), COALESCE(p.origem, '0'),
                       COALESCE(p.unidade_medida, 'UN')
                FROM vendas v
                LEFT JOIN produtos p ON v.produto_id = p.id
                WHERE v.transacao_id = ?
                ORDER BY v.id ASC
                """,
                (str(transacao_id),),
            ).fetchall()
            if not rows:
                raise ValueError(f"Venda não encontrada para transacao_id='{transacao_id}'.")

            cli_nome = str(rows[0][5] or "Consumidor Final")
            cli_doc = str(consumidor_cpf_cnpj or "").strip()
            if not cli_doc and cli_nome != "Consumidor Final":
                row_cli = cur.execute("SELECT cpf_cnpj FROM clientes WHERE nome = ? LIMIT 1", (cli_nome,)).fetchone()
                if row_cli and row_cli[0]:
                    cli_doc = str(row_cli[0])

            # Detectar pagamentos integrados no Mercado Pago (Point ou PIX webhook)
            mp_rows = cur.execute(
                "SELECT tipo FROM pagamentos_mp WHERE external_reference = ? AND status = 'approved'",
                (str(transacao_id),),
            ).fetchall()
            mp_tipos = {str(r[0]).lower() for r in mp_rows}

        itens: list[FiscalItemPayload] = []
        total_bruto = 0.0
        total_desc = 0.0
        tot_din = tot_pix = tot_deb = tot_cred = tot_fiado = 0.0

        for idx, r in enumerate(rows, 1):
            qtd = float(r[2] or 1.0)
            v_bruto = round(float(r[3] or 0.0), 2)
            v_desc = round(float(r[4] or 0.0), 2)
            v_unit = round(v_bruto / qtd, 4) if qtd > 0 else v_bruto
            total_bruto = round(total_bruto + v_bruto, 2)
            total_desc = round(total_desc + v_desc, 2)
            tot_din = round(tot_din + float(r[7] or 0.0), 2)
            tot_pix = round(tot_pix + float(r[8] or 0.0), 2)
            tot_deb = round(tot_deb + float(r[9] or 0.0), 2)
            tot_cred = round(tot_cred + float(r[10] or 0.0), 2)
            tot_fiado = round(tot_fiado + float(r[11] or 0.0), 2)

            itens.append(
                FiscalItemPayload(
                    numero_item=idx,
                    produto_id=int(r[1] or 0),
                    codigo=str(r[13] or r[1] or idx),
                    descricao=str(r[12]),
                    ncm=str(r[14] or "00000000"),
                    cfop=str(r[15] or "5102"),
                    cst_csosn=str(r[16] or "102"),
                    origem=str(r[17] or "0"),
                    unidade=str(r[18] or "UN").upper(),
                    quantidade=qtd,
                    valor_unitario=v_unit,
                    valor_bruto=v_bruto,
                    valor_desconto=v_desc,
                )
            )

        total_liquido = round(total_bruto - total_desc, 2)
        troco_f = round(max(0.0, float(troco or 0.0)), 2)

        pagamentos: list[FiscalPagamentoPayload] = []
        if tot_din > 0:
            pagamentos.append(FiscalPagamentoPayload("01", round(tot_din + troco_f, 2), tipo_integracao="2"))
        if tot_pix > 0:
            tipo_int_pix = "1" if ("pix" in mp_tipos or tef_integrado) else "2"
            pagamentos.append(FiscalPagamentoPayload("17", tot_pix, tipo_integracao=tipo_int_pix))
        if tot_deb > 0:
            tipo_int_card = "1" if ("point" in mp_tipos or tef_integrado) else "2"
            pagamentos.append(FiscalPagamentoPayload("04", tot_deb, tipo_integracao=tipo_int_card))
        if tot_cred > 0:
            tipo_int_card = "1" if ("point" in mp_tipos or tef_integrado) else "2"
            pagamentos.append(FiscalPagamentoPayload("03", tot_cred, tipo_integracao=tipo_int_card))
        if tot_fiado > 0:
            pagamentos.append(FiscalPagamentoPayload("05", tot_fiado, tipo_integracao="2"))

        if not pagamentos:
            forma_sefaz = mapear_forma_sefaz(rows[0][6])
            pagamentos.append(
                FiscalPagamentoPayload(
                    forma_sefaz,
                    round(total_liquido + troco_f, 2),
                    tipo_integracao=("1" if tef_integrado else "2"),
                )
            )

        ambiente_cfg = "producao" if str(cfg.get("ambiente") or "").lower() == "producao" else "homologacao"
        doc = FiscalDocumentoPayload(
            referencia=str(transacao_id),
            ambiente=ambiente_cfg,
            emitente_cnpj=_only_digits(cfg.get("empresa_cnpj", "")),
            emitente_nome=str(cfg.get("empresa_nome") or "Estoke Ao Cubo"),
            emitente_ie=_only_digits(cfg.get("empresa_ie", "")),
            emitente_regime_tributario=str(cfg.get("regime_tributario") or "1"),
            csc=str(cfg.get("csc") or ""),
            id_csc=str(cfg.get("id_csc") or ""),
            consumidor_cpf_cnpj=cli_doc,
            consumidor_nome=cli_nome,
            itens=itens,
            pagamentos=pagamentos,
            valor_total=total_liquido,
            valor_desconto=total_desc,
            valor_troco=troco_f,
            presenca_comprador="1",
            modalidade_frete="9",
        )
        ajustar_centavos_documento(doc)
        return doc

    def _salvar_documento_db(self, referencia: str, ambiente: str, res: FiscalEmitResult, payload_sanitizado: dict) -> None:
        agora = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload_json = json.dumps(sanitizar_payload_fiscal(payload_sanitizado), ensure_ascii=False)
        with self.db._lock:
            self.db.conn.execute(
                """
                INSERT INTO nfce_documentos (
                    transacao_id, status, ambiente, chave_acesso, numero, serie,
                    protocolo, qrcode_url, url_danfe, xml_url, mensagem_sefaz, payload_json,
                    criado_em, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transacao_id) DO UPDATE SET
                    status = excluded.status,
                    ambiente = excluded.ambiente,
                    chave_acesso = COALESCE(NULLIF(excluded.chave_acesso, ''), nfce_documentos.chave_acesso),
                    numero = COALESCE(NULLIF(excluded.numero, ''), nfce_documentos.numero),
                    serie = COALESCE(NULLIF(excluded.serie, ''), nfce_documentos.serie),
                    protocolo = COALESCE(NULLIF(excluded.protocolo, ''), nfce_documentos.protocolo),
                    qrcode_url = COALESCE(NULLIF(excluded.qrcode_url, ''), nfce_documentos.qrcode_url),
                    url_danfe = COALESCE(NULLIF(excluded.url_danfe, ''), nfce_documentos.url_danfe),
                    xml_url = COALESCE(NULLIF(excluded.xml_url, ''), nfce_documentos.xml_url),
                    mensagem_sefaz = excluded.mensagem_sefaz,
                    payload_json = excluded.payload_json,
                    atualizado_em = excluded.atualizado_em
                """,
                (
                    str(referencia),
                    res.status,
                    ambiente,
                    res.chave_acesso,
                    res.numero,
                    res.serie,
                    res.protocolo,
                    res.qrcode_url,
                    res.url_danfe,
                    res.xml_url,
                    str(res.mensagem_sefaz or "")[:500],
                    payload_json,
                    agora,
                    agora,
                ),
            )
            self.db.conn.commit()

    def obter_documento(self, transacao_id: str) -> dict[str, Any] | None:
        with self.db._lock:
            row = self.db.conn.execute(
                """
                SELECT id, transacao_id, status, ambiente, chave_acesso, numero, serie,
                       protocolo, qrcode_url, url_danfe, xml_url, mensagem_sefaz, payload_json,
                       criado_em, atualizado_em
                FROM nfce_documentos WHERE transacao_id = ? LIMIT 1
                """,
                (str(transacao_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "transacao_id": row[1],
            "status": row[2],
            "ambiente": row[3],
            "chave_acesso": row[4],
            "numero": row[5],
            "serie": row[6],
            "protocolo": row[7],
            "qrcode_url": row[8],
            "url_danfe": row[9],
            "xml_url": row[10],
            "mensagem_sefaz": row[11],
            "payload_json": row[12],
            "criado_em": row[13],
            "atualizado_em": row[14],
        }

    def emitir_para_venda(
        self,
        transacao_id: str,
        consumidor_cpf_cnpj: str = "",
        tef_integrado: bool = False,
        troco: float = 0.0,
        outbox_service: Any = None,
    ) -> FiscalEmitResult:
        """
        Emite NFC-e para uma venda já gravada.
        Em falha de comunicação/timeout/5xx grava 'pendente_emissao' (sem fingir tpEmis=9)
        e enfileira na Outbox para transmissão posterior.
        """
        outbox = outbox_service or self.outbox_service
        doc = self.construir_documento_da_venda(
            transacao_id,
            consumidor_cpf_cnpj=consumidor_cpf_cnpj,
            tef_integrado=tef_integrado,
            troco=troco,
        )
        doc_dict_clean = sanitizar_payload_fiscal(asdict(doc))

        try:
            validar_documento_nfce(doc)
        except ValueError as err:
            rej = FiscalEmitResult(
                sucesso=False,
                status="rejeitado",
                referencia=str(transacao_id),
                mensagem_sefaz=str(err),
            )
            self._salvar_documento_db(transacao_id, doc.ambiente, rej, doc_dict_clean)
            return rej

        provider = self._get_provider()
        res = provider.emitir_nfce(doc)

        if res.status in ("erro_comunicacao", "pendente_emissao"):
            pend = FiscalEmitResult(
                sucesso=False,
                status="pendente_emissao",
                referencia=str(transacao_id),
                mensagem_sefaz=res.mensagem_sefaz or "NFC-e pendente de transmissão por falta de conexão.",
                contingencia=True,
                raw_response=res.raw_response,
            )
            self._salvar_documento_db(transacao_id, doc.ambiente, pend, doc_dict_clean)
            if outbox is not None:
                outbox.enfileirar(
                    "nfce_pendente",
                    {"transacao_id": str(transacao_id)},
                    chave_idempotencia=f"nfce:{transacao_id}",
                    max_tentativas=8,
                )
            return pend

        if res.status == "processando":
            self._salvar_documento_db(transacao_id, doc.ambiente, res, doc_dict_clean)
            if outbox is not None:
                outbox.enfileirar(
                    "nfce_pendente",
                    {"transacao_id": str(transacao_id)},
                    chave_idempotencia=f"nfce:{transacao_id}",
                    max_tentativas=8,
                )
            return res

        self._salvar_documento_db(transacao_id, doc.ambiente, res, doc_dict_clean)
        return res

    def processar_outbox_nfce(self, payload: dict[str, Any]) -> tuple[bool, str, bool] | tuple[bool, str]:
        """
        Handler da fila Outbox para transmitir ou consultar NFC-e pendente.
        Quando status anterior for 'processando', consulta via consultar_nfce antes de reenviar.
        """
        tx_id = str((payload or {}).get("transacao_id") or "").strip()
        if not tx_id:
            return False, "transacao_id ausente no payload da outbox fiscal.", True

        atual = self.obter_documento(tx_id)
        if not atual:
            return False, f"Documento NFC-e '{tx_id}' não encontrado.", True
        if atual["status"] == "autorizado":
            return True, "NFC-e já autorizada."
        if atual["status"] == "cancelado":
            return True, "Venda cancelada antes da transmissão da NFC-e."

        provider = self._get_provider()
        ambiente = atual.get("ambiente") or "homologacao"
        try:
            payload_doc = json.loads(atual.get("payload_json") or "{}")
        except Exception:
            payload_doc = {}

        # Se estava em processamento na SEFAZ, consultar primeiro antes de tentar nova emissão
        if atual["status"] == "processando":
            cons = provider.consultar_nfce(tx_id)
            if cons.status == "autorizado":
                self._salvar_documento_db(tx_id, ambiente, cons, payload_doc)
                return True, f"NFC-e autorizada via consulta (chave {cons.chave_acesso})."
            if cons.status == "processando":
                return False, "NFC-e ainda em processamento na SEFAZ."
            if cons.status == "rejeitado":
                self._salvar_documento_db(tx_id, ambiente, cons, payload_doc)
                return False, cons.mensagem_sefaz or "NFC-e rejeitada pela SEFAZ.", True
            if cons.status == "erro_comunicacao" and "404" not in str(cons.mensagem_sefaz):
                return False, cons.mensagem_sefaz

        # Reconstruir documento atualizado do banco para transmissão
        doc = self.construir_documento_da_venda(tx_id)
        try:
            validar_documento_nfce(doc)
        except ValueError as exc:
            rej = FiscalEmitResult(sucesso=False, status="rejeitado", referencia=tx_id, mensagem_sefaz=str(exc))
            self._salvar_documento_db(tx_id, doc.ambiente, rej, sanitizar_payload_fiscal(asdict(doc)))
            return False, str(exc), True

        res = provider.emitir_nfce(doc)
        doc_clean = sanitizar_payload_fiscal(asdict(doc))
        if res.status == "autorizado":
            self._salvar_documento_db(tx_id, doc.ambiente, res, doc_clean)
            return True, f"NFC-e transmitida e autorizada (chave {res.chave_acesso})."
        if res.status == "processando":
            self._salvar_documento_db(tx_id, doc.ambiente, res, doc_clean)
            return False, "NFC-e em processamento assíncrono na SEFAZ."
        if res.status == "rejeitado":
            self._salvar_documento_db(tx_id, doc.ambiente, res, doc_clean)
            return False, res.mensagem_sefaz or "Rejeição fiscal definitiva.", True

        return False, res.mensagem_sefaz or "API fiscal temporariamente indisponível."

    def cancelar_nfce_venda(self, transacao_id: str, justificativa: str) -> tuple[bool, str]:
        """
        Trata cancelamento fiscal integrado ao cancelar_venda():
        - Se pendente_emissao: cancela localmente e remove pendência da outbox.
        - Se autorizado: chama provider.cancelar_nfce() e trata recusa SEFAZ graciosamente.
        """
        atual = self.obter_documento(str(transacao_id))
        if not atual:
            return True, "Sem documento NFC-e vinculado."

        st = atual.get("status")
        agora = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if st in ("pendente_emissao", "rejeitado", "cancelado"):
            with self.db._lock:
                self.db.conn.execute(
                    "UPDATE nfce_documentos SET status = 'cancelado', mensagem_sefaz = ?, atualizado_em = ? WHERE transacao_id = ?",
                    (f"Cancelado antes da autorização: {justificativa}", agora, str(transacao_id)),
                )
                self.db.conn.execute(
                    "UPDATE outbox SET status = 'erro_permanente', erro_ultimo = 'Venda cancelada antes da emissão', atualizado_em = ? WHERE chave_idempotencia = ? AND status IN ('pendente', 'falha_temporaria')",
                    (agora, f"nfce:{transacao_id}"),
                )
                self.db.conn.commit()
            return True, "Pendência fiscal cancelada antes da transmissão."

        if st in ("autorizado", "processando", "cancelamento_recusado"):
            provider = self._get_provider()
            res = provider.cancelar_nfce(str(transacao_id), justificativa)
            if res.sucesso or res.status == "cancelado":
                with self.db._lock:
                    self.db.conn.execute(
                        "UPDATE nfce_documentos SET status = 'cancelado', mensagem_sefaz = ?, atualizado_em = ? WHERE transacao_id = ?",
                        (res.mensagem_sefaz or "Cancelada na SEFAZ", agora, str(transacao_id)),
                    )
                    self.db.conn.commit()
                return True, res.mensagem_sefaz or "NFC-e cancelada na SEFAZ."

            # Recusa fiscal (ex.: prazo legal de 30 min expirado) ou falha de rede: não corrompe estorno local
            msg_erro = res.mensagem_sefaz or "Não foi possível cancelar a NFC-e na SEFAZ."
            with self.db._lock:
                self.db.conn.execute(
                    "UPDATE nfce_documentos SET status = 'cancelamento_recusado', mensagem_sefaz = ?, atualizado_em = ? WHERE transacao_id = ?",
                    (str(msg_erro)[:500], agora, str(transacao_id)),
                )
                self.db.conn.commit()
            return False, msg_erro

        return True, f"Status fiscal atual: {st}"
