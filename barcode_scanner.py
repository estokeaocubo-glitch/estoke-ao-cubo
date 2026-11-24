"""
Módulo de leitura de código de barras via webcam.
Usa OpenCV + pyzbar para captura em tempo real.
"""
import logging
import threading

logger = logging.getLogger('EstokeAoCubo.barcode_scanner')

CV2_AVAILABLE = None
PYZBAR_AVAILABLE = None

def _check_imports():
    global CV2_AVAILABLE, PYZBAR_AVAILABLE
    if CV2_AVAILABLE is not None:
        return
        
    try:
        import cv2
        CV2_AVAILABLE = True
    except ImportError:
        CV2_AVAILABLE = False
        logger.info("OpenCV não instalado. Scanner por câmera indisponível.")
        
    try:
        from pyzbar import pyzbar
        PYZBAR_AVAILABLE = True
    except ImportError:
        PYZBAR_AVAILABLE = False
        logger.info("pyzbar não instalado. Scanner por câmera indisponível.")


def scanner_disponivel():
    """Retorna True se as dependências do scanner estão instaladas."""
    _check_imports()
    return CV2_AVAILABLE and PYZBAR_AVAILABLE


class CameraScanner:
    """Scanner de código de barras usando webcam."""

    def __init__(self, callback, camera_index=0):
        """
        Args:
            callback: Função chamada com o código lido (str). Executada na thread principal.
            camera_index: Índice da câmera (0 = padrão).
        """
        self.callback = callback
        self.camera_index = camera_index
        self._running = False
        self._thread = None
        self._last_code = None

    def start(self):
        """Inicia a captura em background."""
        if not scanner_disponivel():
            return False
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Para a captura."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _capture_loop(self):
        import cv2
        from pyzbar import pyzbar
        
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            logger.error("Não foi possível abrir a câmera %d", self.camera_index)
            self._running = False
            return

        try:
            while self._running:
                ret, frame = cap.read()
                if not ret:
                    continue

                barcodes = pyzbar.decode(frame)
                for barcode in barcodes:
                    code_data = barcode.data.decode('utf-8', errors='ignore')
                    if code_data and code_data != self._last_code:
                        self._last_code = code_data
                        logger.info("Código lido: %s (%s)", code_data, barcode.type)
                        if self.callback:
                            self.callback(code_data)

                # Pequena pausa para não sobrecarregar CPU
                cv2.waitKey(50)
        except Exception as e:
            logger.error("Erro no scanner: %s", e)
        finally:
            cap.release()
            self._running = False


def scan_single(camera_index=0, timeout=30):
    """Escaneia um único código de barras e retorna o valor."""
    if not scanner_disponivel():
        return None

    result = [None]

    def on_code(code):
        result[0] = code

    scanner = CameraScanner(on_code, camera_index)
    scanner.start()

    import time
    start = time.time()
    while result[0] is None and (time.time() - start) < timeout:
        time.sleep(0.1)

    scanner.stop()
    return result[0]
