"""
Serviço de Integração Oficial do Mercado Pago para o Estoke ao Cubo.
Processa pagamentos reais de PIX (QR Code dinâmico com confirmação em tempo real)
e Cartão de Crédito com alta disponibilidade e fallback seguro.
"""
import os
import json
import logging
import uuid
import time
import hmac
import hashlib
import urllib.request
import urllib.error
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, Optional

logger = logging.getLogger('EstokeAoCubo.mercadopago')

# ============================================================
# CARREGAMENTO AUTOMÁTICO DE CREDENCIAIS (.env)
# ============================================================

def _carregar_env():
    """Carrega variáveis do arquivo .env priorizando %LOCALAPPDATA%\\EstokeAoCubo\\.env e fallback somente-leitura."""
    caminhos = []
    try:
        import paths
        caminhos.append(os.path.join(paths.get_app_data_path(), ".env"))
        caminhos.append(os.path.join(paths.get_install_dir(), ".env"))
        caminhos.append(paths.get_resource_path(".env"))
    except Exception:
        pass
    caminhos.extend([
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.getcwd(), "EstokeAoCubo", ".env"),
    ])
    for caminho in caminhos:
        if os.path.exists(caminho):
            try:
                with open(caminho, 'r', encoding='utf-8') as f:
                    for linha in f:
                        linha = linha.strip()
                        if linha and not linha.startswith('#') and '=' in linha:
                            chave, valor = linha.split('=', 1)
                            chave = chave.strip()
                            valor = valor.strip()
                            if chave not in os.environ:
                                os.environ[chave] = valor
            except Exception as e:
                logger.debug("Aviso ao ler .env em %s: %s", caminho, e)

_carregar_env()


class MercadoPagoService:
    """Gerenciador central de cobranças e pagamentos do Mercado Pago."""

    API_BASE = "https://api.mercadopago.com"

    def __init__(self, access_token: Optional[str] = None, public_key: Optional[str] = None):
        _carregar_env()
        self.access_token = access_token or os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "").strip()
        self.public_key = public_key or os.getenv("MERCADO_PAGO_PUBLIC_KEY", "").strip()
        self.client_id = os.getenv("MERCADO_PAGO_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("MERCADO_PAGO_CLIENT_SECRET", "").strip()

    def esta_configurado(self) -> bool:
        """Verifica se o Access Token do Mercado Pago está configurado."""
        return bool(self.access_token and self.access_token.startswith("APP_USR-"))

    def get_public_config(self) -> Dict[str, Any]:
        """Retorna configurações públicas seguras para o frontend / web."""
        return {
            "public_key": self.public_key,
            "configured": self.esta_configurado(),
            "mode": "production" if "APP_USR-" in self.access_token else "sandbox"
        }

    def _fazer_requisicao(
        self,
        endpoint: str,
        metodo: str = "GET",
        dados: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """Executa chamadas HTTP seguras na API do Mercado Pago com retry e backoff exponencial."""
        if not self.access_token:
            raise ValueError("MERCADO_PAGO_ACCESS_TOKEN não configurado no ambiente (.env).")

        url = f"{self.API_BASE}{endpoint}"
        idem_key = idempotency_key or str(uuid.uuid4())
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": idem_key
        }

        payload = json.dumps(dados).encode('utf-8') if dados else None
        last_err = None

        for attempt in range(max_retries):
            req = urllib.request.Request(url, data=payload, headers=headers, method=metodo)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    corpo = resp.read().decode('utf-8')
                    return json.loads(corpo) if corpo and corpo.strip() else {}
            except urllib.error.HTTPError as e:
                corpo_erro = e.read().decode('utf-8') if e.fp else ""
                if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    wait_s = 0.6 * (2 ** attempt)
                    logger.warning("Mercado Pago HTTP %d (tentativa %d/%d). Aguardando %.1fs...", e.code, attempt + 1, max_retries, wait_s)
                    time.sleep(wait_s)
                    continue
                logger.error("Erro HTTP Mercado Pago [%d]: %s", e.code, corpo_erro)
                try:
                    detalhes = json.loads(corpo_erro)
                except Exception:
                    detalhes = {"message": corpo_erro}
                raise RuntimeError(f"Erro Mercado Pago ({e.code}): {detalhes.get('message', corpo_erro)}") from e
            except Exception as e:
                last_err = e
                if attempt < max_retries - 1:
                    wait_s = 0.6 * (2 ** attempt)
                    logger.warning("Falha de rede Mercado Pago (tentativa %d/%d): %s. Retry em %.1fs...", attempt + 1, max_retries, e, wait_s)
                    time.sleep(wait_s)
                    continue
                logger.error("Falha de conexão com Mercado Pago: %s", e)
                raise RuntimeError(f"Falha de comunicação com Mercado Pago: {e}") from e
        raise RuntimeError(f"Falha de comunicação com Mercado Pago: {last_err}")

    @staticmethod
    def validar_assinatura_webhook(
        x_signature: str,
        x_request_id: str,
        data_id: str,
        webhook_secret: Optional[str] = None
    ) -> bool:
        """Valida a assinatura HMAC-SHA256 do webhook do Mercado Pago."""
        secret = (webhook_secret if webhook_secret is not None else os.getenv("MERCADO_PAGO_WEBHOOK_SECRET", "")).strip()
        if not secret:
            return True  # Sem secret configurado, validação delegada à consulta ativa do payment_id na API
        if not x_signature or not data_id:
            return False
        try:
            parts = dict(item.split("=", 1) for item in x_signature.split(",") if "=" in item)
            ts = parts.get("ts", "").strip()
            v1 = parts.get("v1", "").strip()
            if not ts or not v1:
                return False
            manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
            computed = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(computed, v1)
        except Exception as e:
            logger.warning("Erro ao validar assinatura do webhook: %s", e)
            return False

    def criar_cobranca_pix(
        self,
        valor: float,
        descricao: str = "Venda Estoke ao Cubo",
        cliente_email: Optional[str] = None,
        cliente_nome: Optional[str] = None,
        id_venda: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Cria cobrança PIX dinâmica oficial no Mercado Pago.
        Retorna QR Code (Base64), Código Copia e Cola e ID da Transação para monitoramento.
        """
        if valor <= 0:
            raise ValueError("O valor da cobrança deve ser maior que zero.")

        nome_partes = (cliente_nome or "Consumidor Final").strip().split()
        primeiro_nome = nome_partes[0] if nome_partes else "Cliente"
        sobrenome = " ".join(nome_partes[1:]) if len(nome_partes) > 1 else "Estoke"
        email_valido = cliente_email if (cliente_email and "@" in cliente_email) else "cliente@estokeaocubo.com.br"

        dados_pagamento = {
            "transaction_amount": round(float(valor), 2),
            "description": f"{descricao} #{id_venda}" if id_venda else descricao,
            "payment_method_id": "pix",
            "payer": {
                "email": email_valido,
                "first_name": primeiro_nome,
                "last_name": sobrenome
            }
        }

        if id_venda:
            dados_pagamento["external_reference"] = str(id_venda)
            idem_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"estoke-pix-{id_venda}-{round(float(valor), 2)}"))
        else:
            idem_key = str(uuid.uuid4())

        try:
            resposta = self._fazer_requisicao("/v1/payments", metodo="POST", dados=dados_pagamento, idempotency_key=idem_key)
            poi = resposta.get("point_of_interaction", {})
            t_data = poi.get("transaction_data", {})

            payment_id = resposta.get("id")
            qr_code = t_data.get("qr_code", "")
            qr_code_base64 = t_data.get("qr_code_base64", "")
            ticket_url = t_data.get("ticket_url", "")
            status = resposta.get("status", "pending")

            logger.info("Cobrança PIX criada com sucesso no Mercado Pago! ID: %s, Status: %s", payment_id, status)

            return {
                "sucesso": True,
                "payment_id": str(payment_id),
                "status": status,
                "qr_code": qr_code,
                "qr_code_base64": qr_code_base64,
                "ticket_url": ticket_url,
                "valor": valor,
                "id_venda": id_venda,
                "expiracao": resposta.get("date_of_expiration")
            }
        except Exception as e:
            logger.error("Erro ao gerar PIX oficial no Mercado Pago: %s", e)
            # Fallback seguro: se falhar conexão online, permitir fallback com chave estática
            return {
                "sucesso": False,
                "erro": str(e),
                "valor": valor,
                "id_venda": id_venda
            }

    def consultar_pagamento(self, payment_id: str) -> Dict[str, Any]:
        """
        Consulta o status atualizado de um pagamento pelo ID no Mercado Pago.
        Status possíveis: 'approved', 'pending', 'in_process', 'rejected', 'cancelled', 'refunded'.
        """
        if not payment_id:
            return {"sucesso": False, "erro": "Payment ID não fornecido."}

        try:
            resposta = self._fazer_requisicao(f"/v1/payments/{payment_id}", metodo="GET")
            status = resposta.get("status", "unknown")
            status_detail = resposta.get("status_detail", "")
            valor_pago = resposta.get("transaction_amount", 0.0)

            return {
                "sucesso": True,
                "payment_id": str(payment_id),
                "status": status,
                "status_detail": status_detail,
                "aprovado": status == "approved",
                "valor": valor_pago,
                "external_reference": str(resposta.get("external_reference") or ""),
                "data_aprovacao": resposta.get("date_approved"),
                "metodo_pagamento": resposta.get("payment_method_id")
            }
        except Exception as e:
            logger.error("Erro ao consultar pagamento %s: %s", payment_id, e)
            return {
                "sucesso": False,
                "payment_id": str(payment_id),
                "erro": str(e),
                "aprovado": False
            }

    def processar_pagamento_cartao(
        self,
        token: str,
        valor: float,
        parcelas: int = 1,
        payment_method_id: str = "master",
        cliente_email: Optional[str] = None,
        id_venda: Optional[str] = None
    ) -> Dict[str, Any]:
        """Processa transação com token de cartão gerado pelo SDK do Mercado Pago."""
        if not token:
            return {"sucesso": False, "erro": "Token do cartão é obrigatório."}

        dados = {
            "token": token,
            "transaction_amount": round(float(valor), 2),
            "installments": int(parcelas),
            "payment_method_id": payment_method_id,
            "payer": {
                "email": cliente_email or "cliente@estokeaocubo.com.br"
            },
            "description": f"Venda Estoke ao Cubo #{id_venda}" if id_venda else "Venda Estoke ao Cubo"
        }

        if id_venda:
            dados["external_reference"] = str(id_venda)
            idem_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"estoke-card-{id_venda}-{token[:16]}-{round(float(valor), 2)}"))
        else:
            idem_key = str(uuid.uuid4())

        try:
            resposta = self._fazer_requisicao("/v1/payments", metodo="POST", dados=dados, idempotency_key=idem_key)
            status = resposta.get("status", "rejected")
            payment_id = resposta.get("id")
            return {
                "sucesso": status == "approved",
                "payment_id": str(payment_id),
                "status": status,
                "status_detail": resposta.get("status_detail"),
                "aprovado": status == "approved"
            }
        except Exception as e:
            return {"sucesso": False, "erro": str(e), "aprovado": False}

    # ============================================================
    # TEF / PINPAD - MERCADO PAGO POINT INTEGRATION API
    # ============================================================

    def criar_intencao_point(
        self,
        device_id: str,
        valor: float,
        tipo_cartao: str = "credit_card",
        parcelas: int = 1,
        id_venda: Optional[str] = None,
        descricao: str = "Venda Estoke ao Cubo",
    ) -> Dict[str, Any]:
        """
        Cria intenção de pagamento na maquininha Point (valor convertido para centavos inteiros).
        """
        dev_id = str(device_id or "").strip()
        if not dev_id:
            return {"sucesso": False, "erro": "device_id do terminal Point é obrigatório."}
        if float(valor or 0.0) <= 0:
            return {"sucesso": False, "erro": "O valor da cobrança Point deve ser maior que zero."}

        amount_cents = int((Decimal(str(valor)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if amount_cents <= 0:
            return {"sucesso": False, "erro": "Valor em centavos inválido."}

        is_debito = "deb" in str(tipo_cartao or "").lower()
        payment_block: Dict[str, Any] = {
            "type": "debit_card" if is_debito else "credit_card",
            "installments": 1 if is_debito else max(1, int(parcelas or 1)),
        }
        if not is_debito:
            payment_block["installments_cost"] = "seller"

        ext_ref = str(id_venda or "").strip()
        dados = {
            "amount": amount_cents,
            "description": str(descricao or "Venda Estoke ao Cubo")[:100],
            "payment": payment_block,
            "additional_info": {
                "external_reference": ext_ref,
                "print_on_terminal": True,
            },
        }

        idem_key = (
            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"estoke-point-{ext_ref}-{amount_cents}"))
            if ext_ref
            else str(uuid.uuid4())
        )

        try:
            resposta = self._fazer_requisicao(
                f"/point/integration-api/devices/{dev_id}/payment-intents",
                metodo="POST",
                dados=dados,
                idempotency_key=idem_key,
            )
            intent_id = str(resposta.get("id") or "")
            return {
                "sucesso": bool(intent_id),
                "intent_id": intent_id,
                "device_id": dev_id,
                "amount_cents": amount_cents,
                "valor": round(amount_cents / 100.0, 2),
                "external_reference": ext_ref,
                "status": str(resposta.get("state") or resposta.get("status") or "OPEN").upper(),
            }
        except Exception as exc:
            logger.error("Erro ao criar intenção Point (%s): %s", dev_id, exc)
            return {"sucesso": False, "erro": str(exc), "device_id": dev_id, "amount_cents": amount_cents}

    def consultar_intencao_point(self, intent_id: str) -> Dict[str, Any]:
        """
        Consulta status da intenção Point.
        REGRA CRÍTICA: state == 'FINISHED' NÃO implica aprovação automática —
        sempre consulta payment.id via /v1/payments/{id} para confirmar status == 'approved'.
        """
        iid = str(intent_id or "").strip()
        if not iid:
            return {"sucesso": False, "aprovado": False, "finalizado": True, "erro": "intent_id não informado."}

        try:
            resposta = self._fazer_requisicao(f"/point/integration-api/payment-intents/{iid}", metodo="GET")
            state = str(resposta.get("state") or resposta.get("status") or "UNKNOWN").upper()
            payment_obj = resposta.get("payment") or {}
            payment_id = str(payment_obj.get("id") or "").strip()

            if state == "FINISHED":
                if not payment_id:
                    return {
                        "sucesso": True,
                        "intent_id": iid,
                        "state": state,
                        "finalizado": True,
                        "aprovado": False,
                        "recusado": True,
                        "payment_id": "",
                        "status_pagamento": "unknown",
                        "erro": "Intent finalizada sem pagamento vinculado aprovado.",
                    }
                pgto = self.consultar_pagamento(payment_id)
                st_pgto = str(pgto.get("status") or "unknown").lower()
                aprovado = bool(pgto.get("sucesso") and st_pgto == "approved")
                return {
                    "sucesso": True,
                    "intent_id": iid,
                    "state": state,
                    "finalizado": True,
                    "aprovado": aprovado,
                    "recusado": not aprovado,
                    "payment_id": payment_id,
                    "status_pagamento": st_pgto,
                    "status_detail": pgto.get("status_detail", ""),
                    "valor": float(pgto.get("valor") or 0.0),
                    "external_reference": str(pgto.get("external_reference") or ""),
                    "erro": "" if aprovado else f"Pagamento não aprovado no pinpad ({st_pgto}).",
                }

            if state in ("CANCELED", "CANCELLED", "ABANDONED", "ERROR"):
                return {
                    "sucesso": True,
                    "intent_id": iid,
                    "state": state,
                    "finalizado": True,
                    "aprovado": False,
                    "cancelado": True,
                    "payment_id": payment_id,
                    "status_pagamento": state.lower(),
                    "erro": f"Transação cancelada/encerrada no terminal ({state}).",
                }

            return {
                "sucesso": True,
                "intent_id": iid,
                "state": state,
                "finalizado": False,
                "aprovado": False,
                "payment_id": payment_id,
                "status_pagamento": "pending",
            }
        except Exception as exc:
            logger.error("Erro ao consultar intenção Point %s: %s", iid, exc)
            return {
                "sucesso": False,
                "intent_id": iid,
                "finalizado": False,
                "aprovado": False,
                "erro": str(exc),
            }

    def cancelar_intencao_point(self, device_id: str, intent_id: str) -> Dict[str, Any]:
        """Cancela intenção ativa na maquininha Point para destravar o terminal."""
        dev_id = str(device_id or "").strip()
        iid = str(intent_id or "").strip()
        if not dev_id or not iid:
            return {"sucesso": False, "cancelado": False, "erro": "device_id e intent_id são obrigatórios."}

        try:
            self._fazer_requisicao(
                f"/point/integration-api/devices/{dev_id}/payment-intents/{iid}",
                metodo="DELETE",
            )
            return {"sucesso": True, "intent_id": iid, "device_id": dev_id, "cancelado": True}
        except Exception as exc:
            logger.error("Erro ao cancelar intenção Point %s (%s): %s", iid, dev_id, exc)
            return {"sucesso": False, "intent_id": iid, "device_id": dev_id, "cancelado": False, "erro": str(exc)}

    def estornar_pagamento(
        self,
        payment_id: str,
        valor: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Solicita estorno/reembolso de um pagamento aprovado no Mercado Pago
        via POST /v1/payments/{payment_id}/refunds com chave de idempotência.
        """
        pid = str(payment_id or "").strip()
        if not pid:
            return {"sucesso": False, "payment_id": "", "erro": "payment_id obrigatório para estorno."}

        dados: Optional[dict] = None
        valor_tag = "full"
        if valor is not None:
            valor_dec = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if valor_dec <= Decimal("0.00"):
                return {"sucesso": False, "payment_id": pid, "erro": "Valor de estorno deve ser maior que zero."}
            dados = {"amount": float(valor_dec)}
            valor_tag = str(valor_dec)

        idem_key = idempotency_key or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"estoke-refund-{pid}-{valor_tag}"))
        try:
            resp = self._fazer_requisicao(
                f"/v1/payments/{pid}/refunds",
                metodo="POST",
                dados=dados,
                idempotency_key=idem_key,
            )
            return {
                "sucesso": True,
                "refund_id": str(resp.get("id") or ""),
                "payment_id": pid,
                "status": str(resp.get("status") or "approved"),
                "valor": float(resp.get("amount") or (dados["amount"] if dados else 0.0)),
                "raw": resp,
            }
        except Exception as exc:
            logger.error("Erro ao estornar pagamento %s no Mercado Pago: %s", pid, exc)
            return {"sucesso": False, "payment_id": pid, "erro": str(exc)}


# Instância global singleton
_mp_instance: Optional[MercadoPagoService] = None

def get_mercadopago_service() -> MercadoPagoService:
    global _mp_instance
    if _mp_instance is None:
        _mp_instance = MercadoPagoService()
    return _mp_instance
