"""
Package importer — lógica de importación de archivos/cotizaciones,
extraída de server.py (Sprint C).

Módulos:
- constants.py  — GENERIC_FOLDERS, IMPORT_EXTS, PAISES_CONOCIDOS_NORM
- resolver.py   — detección de empresa/país a partir de la cadena de
                   carpetas y el nombre de archivo

Sin cambios de comportamiento respecto a las funciones que vivían
inline en server.py — son las mismas, solo movidas. Las funciones
quedan re-exportadas acá para no romper imports existentes.
"""
from .constants import GENERIC_FOLDERS, IMPORT_EXTS, PAISES_CONOCIDOS_NORM
from .resolver import (
    normalizar_basico,
    detect_pais,
    extract_client_from_stem,
    get_client_name,
)

__all__ = [
    "GENERIC_FOLDERS",
    "IMPORT_EXTS",
    "PAISES_CONOCIDOS_NORM",
    "normalizar_basico",
    "detect_pais",
    "extract_client_from_stem",
    "get_client_name",
]
