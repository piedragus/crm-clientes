"""
Primitivas de texto del importador — sin dependencias dentro del
package, para evitar imports circulares entre constants.py y
resolver.py (ambos la necesitan).
"""
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
