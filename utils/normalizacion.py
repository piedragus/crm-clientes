import re
import unicodedata


def normalizar_alias_empresa(nombre: str | None) -> str:
    """
    Normaliza nombres de empresas para búsqueda por alias.

    Reglas:
    - None -> ""
    - trim
    - lowercase
    - quitar tildes
    - reemplazar puntuación por espacios
    - colapsar espacios
    - quitar sufijos legales al final
    - repetir hasta estabilizar
    - no destruir nombres que quedarían vacíos, ej. "SA" -> "sa"
    """
    if nombre is None:
        return ""

    s = str(nombre).strip().lower()
    if not s:
        return ""

    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )

    # Puntuación a espacios: "S.A." -> "s a"
    s = re.sub(r"[^a-z0-9]", " ", s)
    s = " ".join(s.split())

    original_normalizado = s

    sufijos = [
        r"\bs\s*a\b",
        r"\bs\s*r\s*l\b",
        r"\bs\s*a\s*s\b",
        r"\bltda\b",
        r"\blimitada\b",
        r"\binc\b",
        r"\bllc\b",
        r"\bcorp\b",
        r"\bcorporation\b",
        r"\bcia\b",
        r"\bcompania\b",
        r"\bco\b",
    ]

    cambio = True
    while cambio:
        cambio = False
        for sufijo in sufijos:
            nuevo = re.sub(sufijo + r"$", "", s).strip()
            if nuevo != s:
                s = nuevo
                cambio = True

    if not s:
        return original_normalizado

    return s
