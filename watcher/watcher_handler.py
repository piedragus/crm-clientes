"""
watcher/watcher_handler.py — maneja los eventos de filesystem y dispara
la importación de archivos nuevos (Sprint F).
"""
import logging
import os
import threading
import time

from watchdog.events import FileSystemEventHandler

from importer import importar_archivo
from watcher.watcher_config import ruta_excluida, WATCHER_EXTS, DEBOUNCE_SECONDS

# Tipos de excepción que indican un problema TRANSITORIO de acceso al
# archivo (típicamente: OneDrive todavía lo está sincronizando y lo
# tiene bloqueado), no un error permanente. Vale la pena reintentar
# estos; no tiene sentido reintentar un FileNotFoundError o un PDF
# corrupto, que van a fallar siempre igual.
ERRORES_TRANSITORIOS = {"PermissionError", "OSError", "BlockingIOError"}
MAX_REINTENTOS = 5
REINTENTO_BACKOFF_SECONDS = 10  # espera antes de cada reintento


class CotizacionHandler(FileSystemEventHandler):
    """
    Reacciona a ON_CREATED y ON_MOVED (un archivo que se renombra o se
    mueve dentro de la carpeta también cuenta como "nuevo" a los fines
    de detectarlo — OneDrive a veces sincroniza así en vez de un create
    directo). Hace debounce por archivo: si llegan varios eventos para
    la misma ruta en poco tiempo (común mientras OneDrive todavía está
    bajando el contenido), solo se procesa una vez, después de que
    pasen DEBOUNCE_SECONDS sin nuevos eventos para esa ruta.

    Patrón "single-worker queue" (peer review PR #34): un solo thread
    de background revisa cada `_poll_interval` segundos qué archivos
    llevan más de DEBOUNCE_SECONDS sin recibir un evento nuevo, y los
    procesa — en vez de un threading.Timer por archivo. Con un Timer
    por archivo, una sincronización inicial de OneDrive con miles de
    PDFs históricos lanzaría miles de threads del sistema operativo
    simultáneamente (riesgo real de agotar memoria/threads); con este
    patrón, sea 1 archivo o 10.000, siempre es el mismo único thread.

    Retry de errores transitorios (peer review intensivo): si OneDrive
    todavía tiene el archivo bloqueado (sincronización en curso) cuando
    se intenta procesar, importar_archivo() ya no se cae (la excepción
    se atrapaba desde antes), pero SIN retry el archivo quedaba
    abandonado para siempre como 'error' — el handler solo escucha
    on_created/on_moved, no on_modified, así que cuando OneDrive libera
    el lock no llega ningún evento nuevo que dispare un segundo intento.
    Ahora, si el error es de un tipo transitorio conocido, se reprograma
    automáticamente con backoff, hasta MAX_REINTENTOS veces.
    """

    def __init__(self, db, root_path, on_resultado=None, poll_interval=0.5):
        super().__init__()
        self.db = db
        self.root_path = root_path
        self.on_resultado = on_resultado or (lambda r: None)
        self._pendientes = {}   # path -> timestamp del último evento
        self._reintentos = {}   # path -> cantidad de reintentos ya hechos
        self._lock = threading.Lock()
        self._poll_interval = poll_interval
        self._detener = threading.Event()
        self._worker = threading.Thread(target=self._loop_worker, daemon=True)
        self._worker.start()

    def detener(self):
        """Para el thread worker — llamar al apagar el watcher."""
        self._detener.set()
        self._worker.join(timeout=2)

    def on_created(self, event):
        if not event.is_directory:
            self._marcar_pendiente(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._marcar_pendiente(event.dest_path)

    def _marcar_pendiente(self, path, debounce=None):
        ext = os.path.splitext(path)[1].lower()
        if ext not in WATCHER_EXTS:
            return  # ni vale la pena trackear algo que se va a descartar
        with self._lock:
            self._pendientes[path] = time.time() - DEBOUNCE_SECONDS + (debounce or DEBOUNCE_SECONDS)

    def _loop_worker(self):
        """Único thread de background: cada poll_interval revisa qué
        archivos ya pasaron el debounce y los procesa, uno por vez."""
        while not self._detener.is_set():
            ahora = time.time()
            listos = []
            with self._lock:
                for path, marca in list(self._pendientes.items()):
                    if ahora - marca >= DEBOUNCE_SECONDS:
                        listos.append(path)
                        del self._pendientes[path]
            for path in listos:
                self._procesar(path)
            self._detener.wait(self._poll_interval)

    def _procesar(self, path):
        if ruta_excluida(path, self.root_path):
            logging.info(f"Watcher: omitido (carpeta excluida) {path}")
            return

        if not os.path.isfile(path):
            # Se borró/movió de nuevo antes de que venza el debounce
            logging.info(f"Watcher: {path} ya no existe, se salta")
            return

        resultado = importar_archivo(self.db, path, self.root_path)
        resultado["path"] = path

        if (resultado["estado"] == "error"
                and resultado.get("error_tipo") in ERRORES_TRANSITORIOS):
            intentos = self._reintentos.get(path, 0) + 1
            if intentos <= MAX_REINTENTOS:
                self._reintentos[path] = intentos
                logging.warning(
                    f"Watcher: error transitorio en {path} "
                    f"({resultado.get('error_tipo')}) — reintento {intentos}/{MAX_REINTENTOS} "
                    f"en {REINTENTO_BACKOFF_SECONDS}s")
                with self._lock:
                    self._pendientes[path] = time.time() - DEBOUNCE_SECONDS + REINTENTO_BACKOFF_SECONDS
                return
            logging.error(f"Watcher: {path} agotó los {MAX_REINTENTOS} reintentos, "
                          f"queda como error definitivo: {resultado.get('motivo')}")

        self._reintentos.pop(path, None)
        nivel = logging.INFO if resultado["estado"] == "importado" else logging.WARNING
        logging.log(nivel, f"Watcher: {resultado['estado']} — {path} — {resultado.get('motivo','')}")
        self.db.registrar_evento_watcher(
            path, resultado["estado"],
            empresa_nombre=resultado.get("empresa_nombre"),
            motivo=resultado.get("motivo"))
        self.on_resultado(resultado)
