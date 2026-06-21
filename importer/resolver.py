"""
Resolución de empresa/país a partir de la cadena de carpetas y el
nombre de archivo — extraído de server.py (Sprint C).

Funciones puras, sin dependencia de Flask ni de la DB: reciben
listas de strings y devuelven strings. Pensadas para ser reutilizadas
tal cual por el watcher de OneDrive (Sprint E).
"""
import re

from .utils import normalizar_basico
from .constants import GENERIC_FOLDERS, PAISES_CONOCIDOS_NORM


def detect_pais(folder_chain: list) -> str:
    """Detecta país desde la cadena de carpetas (tolerante a tildes y mayúsculas)."""
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
    for folder in reversed(folder_chain):
        if folder.lower().strip() not in GENERIC_FOLDERS:
            return folder
    return extract_client_from_stem(filename_stem)
