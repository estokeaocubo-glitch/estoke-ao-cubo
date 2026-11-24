"""
Serviço de periféricos de hardware — balança serial (protocolo Toledo/Filizola ENQ)
e leitor de código de barras via webcam.

Regra estrita de inicialização (preparação F8):
Bibliotecas pesadas (cv2, pyzbar, serial) são importadas EXCLUSIVAMENTE dentro dos métodos.
Importar este módulo nunca carrega cv2, pyzbar ou pyserial.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger("EstokeAoCubo.services.hardware")


class HardwareError(RuntimeError):
    """Erro base de comunicação com periféricos de hardware."""


class HardwareIndisponivelError(HardwareError):
    """Periférico ausente, desconectado ou driver/biblioteca não instalada."""


class HardwareTimeoutError(TimeoutError, HardwareError):
    """Tempo limite excedido aguardando resposta do periférico."""


class HardwareService:
    """Lógica de negócio pura para balança serial e leitura via webcam."""

    def ler_peso_balanca(
        self,
        porta: str | None = None,
        timeout: float = 0.6,
    ) -> float:
        """
        Lê peso (em kg) de uma balança serial via comando ENQ (0x05).

        Args:
            porta: Porta COM/tty específica (opcional; se None, varre portas COM disponíveis).
            timeout: Tempo máximo em segundos para leitura.

        Returns:
            Peso em quilogramas (> 0).

        Raises:
            ValueError: timeout inválido.
            HardwareIndisponivelError: pyserial indisponível ou nenhuma porta encontrada.
            HardwareTimeoutError: nenhuma resposta válida dentro do timeout.
        """
        if timeout is None or float(timeout) <= 0:
            raise ValueError("Timeout da balança deve ser maior que zero.")
        timeout_s = float(timeout)

        try:
            import serial
            import serial.tools.list_ports
        except ImportError as exc:
            raise HardwareIndisponivelError(
                "Biblioteca pyserial não instalada para leitura da balança."
            ) from exc

        if porta:
            portas = [str(porta)]
        else:
            portas = [p.device for p in serial.tools.list_ports.comports()]

        if not portas:
            raise HardwareIndisponivelError("Nenhuma porta serial/COM encontrada para a balança.")

        deadline = time.monotonic() + timeout_s
        ultimo_erro: Exception | None = None

        for dev in portas:
            restante = deadline - time.monotonic()
            if restante <= 0:
                raise HardwareTimeoutError(
                    f"Timeout ({timeout_s:.2f}s) ao aguardar peso da balança serial."
                )
            try:
                with serial.Serial(
                    dev,
                    baudrate=9600,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=min(restante, timeout_s),
                ) as ser:
                    ser.write(b"\x05")
                    raw_bytes = ser.read(16)
                    if time.monotonic() > deadline and not raw_bytes:
                        raise HardwareTimeoutError(
                            f"Timeout ({timeout_s:.2f}s) na porta {dev}."
                        )
                    raw = raw_bytes.decode("ascii", errors="ignore") if raw_bytes else ""
                    m = re.search(r"(\d{5})", raw)
                    if m:
                        peso = int(m.group(1)) / 1000.0
                        if peso > 0:
                            return peso
            except HardwareTimeoutError:
                raise
            except Exception as exc:
                ultimo_erro = exc
                continue

        if ultimo_erro is not None and porta:
            raise HardwareError(f"Falha ao comunicar com a balança na porta {porta}: {ultimo_erro}") from ultimo_erro

        raise HardwareTimeoutError(
            f"Balança não retornou leitura válida dentro de {timeout_s:.2f}s."
        )

    def capturar_codigo_webcam(
        self,
        timeout: float = 15.0,
        camera_index: int = 0,
    ) -> str:
        """
        Captura um código de barras/QR pela webcam até atingir o timeout.

        Args:
            timeout: Tempo máximo em segundos aguardando um código válido.
            camera_index: Índice da câmera (padrão 0).

        Returns:
            Texto do código de barras decodificado.

        Raises:
            ValueError: timeout inválido.
            HardwareIndisponivelError: OpenCV/pyzbar ausente ou câmera indisponível.
            HardwareTimeoutError: nenhum código detectado dentro do timeout.
        """
        if timeout is None or float(timeout) <= 0:
            raise ValueError("Timeout da webcam deve ser maior que zero.")
        timeout_s = float(timeout)

        try:
            import cv2
            from pyzbar import pyzbar
        except ImportError as exc:
            raise HardwareIndisponivelError(
                "Instale opencv-python e pyzbar para usar o scanner por webcam."
            ) from exc

        cap = (
            cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
            if os.name == "nt" and hasattr(cv2, "CAP_DSHOW")
            else cv2.VideoCapture(camera_index)
        )
        if cap is None or not cap.isOpened():
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            raise HardwareIndisponivelError("Câmera não disponível ou não pôde ser aberta.")

        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.01)
                    continue
                barcodes = pyzbar.decode(frame)
                for bc in barcodes:
                    code_data = bc.data.decode("utf-8", errors="ignore").strip()
                    if code_data:
                        return code_data
                time.sleep(0.01)
        finally:
            try:
                cap.release()
            except Exception:
                pass

        raise HardwareTimeoutError(
            f"Nenhum código de barras detectado na webcam em {timeout_s:.1f}s."
        )

    def decodificar_frame(self, frame: Any) -> tuple[str | None, list[Any]]:
        """Decodifica códigos de barras em um único frame OpenCV (uso na UI de preview)."""
        from pyzbar import pyzbar

        barcodes = pyzbar.decode(frame)
        for bc in barcodes:
            code_data = bc.data.decode("utf-8", errors="ignore").strip()
            if code_data:
                return code_data, list(barcodes)
        return None, list(barcodes)
