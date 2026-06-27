"""
watcher/watcher_config.py — configuración del watcher de OneDrive (Sprint F).

Distinción importante con GENERIC_FOLDERS (importer/constants.py):
- GENERIC_FOLDERS le dice al resolver "esta carpeta no es el nombre
  del cliente, seguí buscando hacia arriba" — pero el archivo SÍ se
  importa, solo cambia de dónde se saca el nombre de empresa.
- EXCLUDED_FOLDERS de este módulo le dice al watcher "ESTE ARCHIVO NO
  ES UNA COTIZACIÓN, no lo importes en absoluto" — manuales, fotos,
  catálogos de partes, plantillas. Son conceptualmente distintos:
  ninguna de estas carpetas estaba en GENERIC_FOLDERS, y agregarlas
  ahí hubiera sido incorrecto (cambiaría el comportamiento de TODOS
  los importadores existentes, no solo el watcher).
"""
import os

# Coincidencia exacta (insensible a mayúsculas/tildes ya normalizadas
# por el caller) — carpetas que nunca contienen cotizaciones reales.
EXCLUDED_FOLDERS = {
    "00 cotizaciones tipo de maq",
    "aa codigo de maquinas",
    "aaa partes de maq",
    "alambres- barras",
    "manual",
    "manual maquina",
    "estructura",
    "prearmados de maquinas",
}

# Prefijos — para "FOTOS*" (FOTOS, FOTOS CLIENTE, FOTOS 2024, etc.)
EXCLUDED_PREFIXES = {
    "fotos",
}

# Extensiones que el watcher considera (mismas que el resto del
# importer — ver importer.constants.IMPORT_EXTS, no se duplica el
# valor: se importa directo para no desincronizarse si cambia ahí).
from importer.constants import IMPORT_EXTS as WATCHER_EXTS  # noqa: E402

# Cuánto esperar (segundos) después del último evento de un archivo
# antes de procesarlo — OneDrive puede disparar varios eventos
# (created, modified) mientras todavía está bajando el archivo; sin
# este margen se podría intentar leer un archivo a medio sincronizar.
DEBOUNCE_SECONDS = 5

# Intervalo de poll del observer (no es el tiempo real OS-nativo en
# todas las plataformas, watchdog ya lo maneja — esto es el timeout
# del loop principal entre chequeos de la cola de eventos pendientes).
LOOP_SLEEP_SECONDS = 1


def _normalizar(nombre: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFD", nombre.lower().strip()).encode(
        "ascii", "ignore").decode()


def carpeta_excluida(nombre_carpeta: str) -> bool:
    """True si una carpeta puntual (no la ruta completa) está excluida,
    por nombre exacto o por prefijo."""
    norm = _normalizar(nombre_carpeta)
    if norm in EXCLUDED_FOLDERS:
        return True
    return any(norm.startswith(p) for p in EXCLUDED_PREFIXES)


def ruta_excluida(path: str, root_path: str) -> bool:
    """True si CUALQUIER carpeta en la cadena entre root_path y el
    archivo está excluida — alcanza con una sola carpeta excluida en
    el camino para descartar todo el archivo."""
    rel = os.path.relpath(os.path.dirname(path), root_path)
    if rel == ".":
        return False
    partes = rel.replace("\\", "/").split("/")
    return any(carpeta_excluida(p) for p in partes)
