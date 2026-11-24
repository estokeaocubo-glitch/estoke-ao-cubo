# -*- coding: utf-8 -*-
"""
Estoke Ao Cubo - Fila de Saída Offline (Outbox Pattern)
Garante entrega assíncrona e resiliente de mensagens WhatsApp, e-mails e webhooks
sem bloquear o caixa ou perder notificações quando a loja está sem internet.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from typing import Any, Callable

logger = logging.getLogger("EstokeAoCubo.outbox")


def default_connectivity_check(timeout: float = 1.5) -> bool:
    """Verifica rapidamente se há conectividade TCP externa sem bloquear a UI."""
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _normalize_phone_br(telefone: str) -> str:
    digits = re.sub(r"\D", "", str(telefone or ""))
    if not digits:
        return ""
    if len(digits) in (10, 11) and not digits.startswith("55"):
        digits = f"55{digits}"
    return digits


class OutboxService:
    """Gerenciador persistente de fila outbox com retry e backoff exponencial."""

    def __init__(
        self,
        db: Any,
        handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        base_backoff_seconds: int = 15,
        max_backoff_seconds: int = 3600,
        poll_interval_seconds: float = 10.0,
        connectivity_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.db = db
        self.base_backoff_seconds = max(1, int(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, int(max_backoff_seconds))
        self.poll_interval_seconds = max(0.5, float(poll_interval_seconds))
        self.connectivity_checker = connectivity_checker or default_connectivity_check

        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "whatsapp": self._handle_whatsapp,
            "email": self._handle_email,
            "webhook": self._handle_webhook,
        }
        if handlers:
            self._handlers.update(handlers)

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def register_handler(self, tipo: str, fn: Callable[[dict[str, Any]], Any]) -> None:
        """Registra ou sobrescreve o handler de envio para um tipo da outbox."""
        self._handlers[str(tipo).strip().lower()] = fn

    def calcular_backoff_seconds(self, tentativa: int) -> int:
        """Calcula backoff exponencial em segundos: base * 2^(tentativa - 1), limitado ao máximo."""
        exp = max(0, int(tentativa) - 1)
        return min(self.max_backoff_seconds, int(self.base_backoff_seconds * (2 ** exp)))

    def enfileirar(
        self,
        tipo: str,
        payload: dict[str, Any],
        chave_idempotencia: str = "",
        max_tentativas: int = 5,
        proximo_envio: str | None = None,
    ) -> tuple[bool, int]:
        """
        Persiste um item na tabela outbox sob lock do banco.
        Se chave_idempotencia já existir (exceto erro_permanente substituído manualmente),
        evita duplicidade retornando (False, existing_id).
        """
        tipo_norm = str(tipo or "").strip().lower()
        if not tipo_norm:
            raise ValueError("Tipo da outbox não pode ser vazio.")

        idem = str(chave_idempotencia or "").strip()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prox_str = proximo_envio or agora_str

        with self.db._lock:
            cur = self.db.conn.cursor()
            if idem:
                cur.execute(
                    """
                    SELECT id FROM outbox
                    WHERE chave_idempotencia = ? AND status IN ('pendente', 'processando', 'falha_temporaria', 'enviado')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (idem,),
                )
                existente = cur.fetchone()
                if existente:
                    return False, int(existente[0])

            cur.execute(
                """
                INSERT INTO outbox (
                    tipo, payload, tentativas, max_tentativas,
                    proximo_envio, status, erro_ultimo, chave_idempotencia,
                    criado_em, atualizado_em
                )
                VALUES (?, ?, 0, ?, ?, 'pendente', '', ?, ?, ?)
                """,
                (tipo_norm, payload_json, max(1, int(max_tentativas)), prox_str, idem, agora_str, agora_str),
            )
            self.db.conn.commit()
            row_id = int(cur.lastrowid)

        self._wake_event.set()
        return True, row_id

    def listar_itens(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Lista registros da outbox para inspeção na UI ou auditoria."""
        with self.db._lock:
            cur = self.db.conn.cursor()
            if status:
                cur.execute(
                    """
                    SELECT id, tipo, payload, tentativas, max_tentativas, proximo_envio,
                           status, erro_ultimo, chave_idempotencia, criado_em, atualizado_em
                    FROM outbox WHERE status = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (status, int(limit)),
                )
            else:
                cur.execute(
                    """
                    SELECT id, tipo, payload, tentativas, max_tentativas, proximo_envio,
                           status, erro_ultimo, chave_idempotencia, criado_em, atualizado_em
                    FROM outbox
                    ORDER BY id DESC LIMIT ?
                    """,
                    (int(limit),),
                )
            rows = cur.fetchall()

        result = []
        for r in rows:
            try:
                parsed = json.loads(r[2]) if r[2] else {}
            except Exception:
                parsed = {"raw": r[2]}
            result.append(
                {
                    "id": r[0],
                    "tipo": r[1],
                    "payload": parsed,
                    "tentativas": r[3],
                    "max_tentativas": r[4],
                    "proximo_envio": r[5],
                    "status": r[6],
                    "erro_ultimo": r[7],
                    "chave_idempotencia": r[8],
                    "criado_em": r[9],
                    "atualizado_em": r[10],
                }
            )
        return result

    def reprocessar_item(self, item_id: int) -> bool:
        """Recoloca um item com erro na fila para tentativa imediata."""
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._lock:
            cur = self.db.conn.cursor()
            cur.execute(
                """
                UPDATE outbox
                SET status = 'pendente', tentativas = 0, proximo_envio = ?, erro_ultimo = '', atualizado_em = ?
                WHERE id = ?
                """,
                (agora_str, agora_str, int(item_id)),
            )
            self.db.conn.commit()
            changed = cur.rowcount > 0
        if changed:
            self._wake_event.set()
        return changed

    def processar_pendentes(self, now_dt: datetime | None = None, limit: int = 20) -> dict[str, int]:
        """
        Processa itens cujo proximo_envio <= now_dt.
        Nunca mantém o lock do SQLite durante chamadas de rede externas.
        """
        ref_dt = now_dt or datetime.now()
        ref_str = ref_dt.strftime("%Y-%m-%d %H:%M:%S")
        stats = {"processados": 0, "enviados": 0, "retentativas": 0, "erros_permanentes": 0}

        with self.db._lock:
            cur = self.db.conn.cursor()
            cur.execute(
                """
                SELECT id, tipo, payload, tentativas, max_tentativas
                FROM outbox
                WHERE status IN ('pendente', 'falha_temporaria')
                  AND proximo_envio <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (ref_str, int(limit)),
            )
            lote = cur.fetchall()
            if lote:
                ids = [int(r[0]) for r in lote]
                placeholders = ",".join("?" for _ in ids)
                cur.execute(
                    f"UPDATE outbox SET status = 'processando', atualizado_em = ? WHERE id IN ({placeholders})",
                    [ref_str, *ids],
                )
                self.db.conn.commit()

        for row in lote:
            item_id = int(row[0])
            tipo = str(row[1] or "").strip().lower()
            raw_payload = row[2]
            tentativas_atuais = int(row[3] or 0) + 1
            max_tentativas = max(1, int(row[4] or 5))

            stats["processados"] += 1
            try:
                payload = json.loads(raw_payload) if raw_payload else {}
            except Exception as exc:
                self._marcar_falha(
                    item_id,
                    tentativas_atuais,
                    max_tentativas,
                    f"Payload JSON inválido: {exc}",
                    ref_dt,
                    permanente=True,
                )
                stats["erros_permanentes"] += 1
                continue

            handler = self._handlers.get(tipo)
            if handler is None:
                self._marcar_falha(
                    item_id,
                    tentativas_atuais,
                    max_tentativas,
                    f"Handler não registrado para tipo '{tipo}'",
                    ref_dt,
                    permanente=True,
                )
                stats["erros_permanentes"] += 1
                continue

            ok = False
            detalhe = ""
            permanente = False
            try:
                res = handler(payload)
                if isinstance(res, tuple):
                    ok = bool(res[0])
                    detalhe = str(res[1]) if len(res) > 1 else ""
                    if len(res) > 2:
                        permanente = bool(res[2])
                else:
                    ok = bool(res)
            except Exception as exc:
                ok = False
                detalhe = str(exc)

            if ok:
                with self.db._lock:
                    self.db.conn.execute(
                        """
                        UPDATE outbox
                        SET status = 'enviado', tentativas = ?, erro_ultimo = '', atualizado_em = ?
                        WHERE id = ?
                        """,
                        (tentativas_atuais, ref_str, item_id),
                    )
                    self.db.conn.commit()
                stats["enviados"] += 1
            else:
                virou_permanente = self._marcar_falha(
                    item_id,
                    tentativas_atuais,
                    max_tentativas,
                    detalhe or "Falha desconhecida no envio",
                    ref_dt,
                    permanente=permanente,
                )
                if virou_permanente:
                    stats["erros_permanentes"] += 1
                else:
                    stats["retentativas"] += 1

        return stats

    def _marcar_falha(
        self,
        item_id: int,
        tentativas: int,
        max_tentativas: int,
        erro: str,
        ref_dt: datetime,
        permanente: bool = False,
    ) -> bool:
        esgotou = bool(permanente or tentativas >= max_tentativas)
        status_final = "erro_permanente" if esgotou else "falha_temporaria"
        backoff = self.calcular_backoff_seconds(tentativas)
        prox_dt = ref_dt if esgotou else (ref_dt + timedelta(seconds=backoff))
        agora_str = ref_dt.strftime("%Y-%m-%d %H:%M:%S")
        prox_str = prox_dt.strftime("%Y-%m-%d %H:%M:%S")

        with self.db._lock:
            self.db.conn.execute(
                """
                UPDATE outbox
                SET status = ?, tentativas = ?, proximo_envio = ?, erro_ultimo = ?, atualizado_em = ?
                WHERE id = ?
                """,
                (status_final, tentativas, prox_str, str(erro)[:500], agora_str, int(item_id)),
            )
            self.db.conn.commit()
        return esgotou

    def _handle_whatsapp(self, payload: dict[str, Any]) -> tuple[bool, str]:
        telefone = _normalize_phone_br(payload.get("telefone", ""))
        mensagem = str(payload.get("mensagem", "") or "").strip()
        if not telefone:
            return False, "Telefone inválido/ausente", True
        if not mensagem:
            return False, "Mensagem vazia", True

        if not self.connectivity_checker():
            return False, "Sem conexão com a internet para envio WhatsApp"

        # Se houver endpoint Evolution/Z-API/Webhook no payload, dispara via HTTP JSON
        api_url = str(payload.get("api_url", "") or "").strip()
        if api_url:
            return self._post_json(
                api_url,
                {"telefone": telefone, "mensagem": mensagem, "metadata": payload.get("metadata", {})},
                headers=payload.get("headers") or {},
            )

        url = f"https://wa.me/{telefone}?text={urllib.parse.quote(mensagem)}"
        opened = webbrowser.open(url)
        if opened:
            return True, "Aberto no WhatsApp Web/Desktop"
        return False, "Não foi possível abrir o navegador para WhatsApp"

    def _handle_email(self, payload: dict[str, Any]) -> tuple[bool, str]:
        destinatario = str(payload.get("destinatario", "") or "").strip()
        assunto = str(payload.get("assunto", "") or "Notificação Estoke Ao Cubo").strip()
        html = str(payload.get("html", "") or payload.get("mensagem", "") or "").strip()
        if not destinatario or "@" not in destinatario:
            return False, "Destinatário de e-mail inválido", True

        if not self.connectivity_checker():
            return False, "Sem conexão com a internet para envio de e-mail"

        try:
            import printer as printer_mod

            fn = getattr(printer_mod, "enviar_email_sincrono", None) or getattr(printer_mod, "enviar_email_assincrono", None)
            if callable(fn):
                res = fn(destinatario, assunto, html)
                if isinstance(res, tuple):
                    return bool(res[0]), str(res[1])
                return True, "E-mail encaminhado"
        except Exception as exc:
            return False, f"Erro ao despachar e-mail: {exc}"
        return False, "Serviço de e-mail indisponível"

    def _handle_webhook(self, payload: dict[str, Any]) -> tuple[bool, str]:
        url = str(payload.get("url", "") or "").strip()
        if not url.startswith(("https://", "http://")):
            return False, "URL de webhook inválida", True

        if not self.connectivity_checker():
            return False, "Sem conexão com a internet para webhook"

        body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        return self._post_json(url, body, headers=headers)

    def _post_json(self, url: str, data: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[bool, str]:
        req_headers = {"Content-Type": "application/json", "User-Agent": "EstokeAoCubo/4.5"}
        if headers:
            req_headers.update({str(k): str(v) for k, v in headers.items()})
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=raw, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = int(getattr(resp, "status", 200) or 200)
                if 200 <= code < 300:
                    return True, f"HTTP {code}"
                return False, f"HTTP {code}", (400 <= code < 500 and code != 429)
        except urllib.error.HTTPError as exc:
            perm = 400 <= exc.code < 500 and exc.code != 429
            return False, f"HTTPError {exc.code}: {exc.reason}", perm
        except Exception as exc:
            return False, f"Erro de rede: {exc}"

    def start_worker(self) -> threading.Thread:
        """Inicia thread daemon responsável por drenar a fila outbox em background."""
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return self._worker_thread

            self._stop_event.clear()

            def _loop() -> None:
                logger.info("Worker Outbox iniciado (intervalo=%.1fs).", self.poll_interval_seconds)
                while not self._stop_event.is_set():
                    try:
                        self.processar_pendentes()
                    except Exception as exc:
                        logger.warning("Falha no ciclo do worker Outbox: %s", exc)
                    self._wake_event.wait(timeout=self.poll_interval_seconds)
                    self._wake_event.clear()
                logger.info("Worker Outbox encerrado.")

            self._worker_thread = threading.Thread(target=_loop, name="EstokeOutboxWorker", daemon=True)
            self._worker_thread.start()
            return self._worker_thread

    def stop_worker(self, timeout: float = 2.0) -> None:
        """Solicita parada limpa da thread daemon da outbox."""
        self._stop_event.set()
        self._wake_event.set()
        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=timeout)
