"""Constantes compartidas del pipeline de extracción (Sprint E)."""

CAMPOS_VALIDOS = {
    "monto", "moneda", "fecha_doc", "numero_documento",
    "cliente_detectado", "pais_detectado", "proveedor", "tipo",
    "validez_dias",
}

FUENTES_VALIDAS = {
    "texto_directo",   # regex sobre el texto extraído del archivo (capa determinística)
    "ocr",              # texto extraído por OCR (futuro, ver issue de seguimiento)
    "nombre_archivo",   # heurística sobre el nombre del archivo (ya existía, importer/)
    "carpeta",           # heurística sobre la cadena de carpetas (ya existía, importer/)
    "ia_llm",            # Gemini/Grok vía resumidor.py
    "manual",            # corrección humana
}

ESTADOS_CAMPO_VALIDOS = {
    "ok",                  # valor confiable, no necesita revisión
    "pendiente_revision",  # confianza baja o no se encontró nada — NO es un valor falso
    "manual_confirmado",   # corregido a mano, no se pisa en reprocesamientos futuros
}

# Confianza mínima para considerar "ok" un campo extraído por la capa
# determinística sin pasar por revisión manual.
UMBRAL_CONFIANZA_OK = 0.7
