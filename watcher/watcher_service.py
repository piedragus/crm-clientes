"""
watcher/watcher_service.py — proceso de consola que vigila la carpeta
de OneDrive y dispara importar_archivo() para cada PDF/DOCX/etc. nuevo
(Sprint F). Se arranca con run_watcher.bat, queda corriendo en una
ventana de consola (no es un Windows Service) hasta que se cierre con
Ctrl+C.

Uso:
    python watcher/watcher_service.py /ruta/a/OneDrive/Cotizaciones
"""
import logging
import os
import sys
import time

from watchdog.observers import Observer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_manager import DBManager
from watcher.watcher_handler import CotizacionHandler
from watcher.watcher_config import LOOP_SLEEP_SECONDS


def iniciar(root_path: str, db_name: str = "clientes_v2.db"):
    if not os.path.isdir(root_path):
        print(f"ERROR: la carpeta {root_path!r} no existe.")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(os.path.join("logs", "watcher.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ])

    db = DBManager(db_name)
    handler = CotizacionHandler(db, root_path)
    observer = Observer()
    observer.schedule(handler, root_path, recursive=True)
    observer.start()

    print(f"👀 Watcher activo sobre: {root_path}")
    print("   Ctrl+C para detener.\n")
    logging.info(f"Watcher iniciado sobre {root_path}")

    try:
        while True:
            time.sleep(LOOP_SLEEP_SECONDS)
    except KeyboardInterrupt:
        observer.stop()
        handler.detener()
        print("\nWatcher detenido.")
        logging.info("Watcher detenido por el usuario (Ctrl+C)")
    observer.join()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python watcher_service.py /ruta/a/carpeta/OneDrive")
        sys.exit(1)
    iniciar(sys.argv[1])
