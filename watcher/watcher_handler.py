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


class CotizacionHandler(FileSystemEventHandler):
    """
    Reacciona a ON_CREATED y ON_MOVED (un archivo que se renombra o se
    mueve dentro de la carpeta también cuenta como "nuevo" a los fines
    de detectarlo — OneDrive a veces sincroniza así en vez de un create
    directo). Hace debounce por archivo: si llegan varios eventos para
    la misma ruta en poco tiempo (común mientras OneDrive todavía está
    bajando el contenido), solo se procesa una vez, después de que
    pasen DEBOUNCE_SECONDS sin nuevos eventos para esa ruta.
    """

    def __init__(self, db, root_path, on_resultado=None):
        super().__init__()
        self.db = db
        self.root_path = root_path
        self.on_resultado = on_resultado or (lambda r: None)
        self._timers = {}
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._programar(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._programar(event.dest_path)

    def _programar(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext not in WATCHER_EXTS:
            return  # ni vale la pena debounce-ar algo que se va a descartar

        with self._lock:
            timer_viejo = self._timers.get(path)
            if timer_viejo:
                timer_viejo.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._procesar, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _procesar(self, path):
        with self._lock:
            self._timers.pop(path, None)

        if ruta_excluida(path, self.root_path):
            logging.info(f"Watcher: omitido (carpeta excluida) {path}")
            return

        if not os.path.isfile(path):
            # Se borró/movió de nuevo antes de que venza el debounce
            logging.info(f"Watcher: {path} ya no existe, se salta")
            return

        resultado = importar_archivo(self.db, path, self.root_path)
        resultado["path"] = path
        nivel = logging.INFO if resultado["estado"] == "importado" else logging.WARNING
        logging.log(nivel, f"Watcher: {resultado['estado']} — {path} — {resultado.get('motivo','')}")
        self.db.registrar_evento_watcher(
            path, resultado["estado"],
            empresa_nombre=resultado.get("empresa_nombre"),
            motivo=resultado.get("motivo"))
        self.on_resultado(resultado)
