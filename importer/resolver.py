"""
Resolución de empresa/país a partir de la cadena de carpetas y el
nombre de archivo — extraído de server.py (Sprint C).

Funciones puras, sin dependencia de Flask ni de la DB: reciben
listas de strings y devuelven strings. Pensadas para ser reutilizadas
tal cual por el watcher de OneDrive (Sprint E).
"""
import re
import unicodedata


def normalizar_basico(s: str) -> str:
    """Normaliza a minúsculas sin acentos para comparación simple.

    No confundir con utils.normalizacion.normalizar_alias_empresa,
    que además quita puntuación y sufijos legales (SA, SRL, etc.)
    para matching de alias — esta función es más liviana, usada
    para comparar nombres de carpeta contra listas conocidas
    (países, GENERIC_FOLDERS).
    """
    return unicodedata.normalize("NFD", str(s or "").lower().strip()).encode(
        "ascii", "ignore").decode()


def detect_pais(folder_chain: list) -> str:
    """Detecta país desde la cadena de carpetas (tolerante a tildes y mayúsculas)."""
    from .constants import PAISES_CONOCIDOS_NORM
    for folder in folder_chain:
        key = normalizar_basico(folder)
        if key in PAISES_CONOCIDOS_NORM:
            return PAISES_CONOCIDOS_NORM[key]
    return ""


def extract_client_from_stem(stem: str) -> str:
    """'ALBANESI02' → 'ALBANESI', 'Alejandro Garaggiola01' → 'Alejandro Garaggiola'"""
    name = re.sub(r'[\s_-]*\d+\s*$', '', stem).strip()
    return name if name else stem


def get_client_name(folder_chain: list, filename_stem: str) -> str:
    """Walk folders deepest-first, skip generic ones; fallback to filename."""
    from .constants import GENERIC_FOLDERS
    for folder in reversed(folder_chain):
        if folder.lower().strip() not in GENERIC_FOLDERS:
            return folder
    return extract_client_from_stem(filename_stem)
