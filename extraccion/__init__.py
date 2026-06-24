"""
Package extraccion — Sprint E: extracción de campos con trazabilidad.

Separa la extracción en capas, cada una con su propia "fuente" registrada:
  1. campos.py     — capa determinística (regex), sin LLM, sin red,
                      100% testeable, corre primero
  2. (futuro)       — capa OCR para PDFs escaneados (ver issue de
                      seguimiento — requiere tesseract/poppler a nivel
                      sistema operativo, no solo pip, y PDFs de prueba
                      reales para validar calidad)
  3. resumidor.py   — capa LLM (Gemini/Grok), ya existía antes de este
                      sprint, sigue siendo el fallback cuando la capa
                      determinística no encuentra nada con confianza
                      suficiente

constants.py define los nombres de campo y fuente válidos, compartidos
entre el módulo de extracción y los endpoints de server.py.
"""
from .constants import CAMPOS_VALIDOS, FUENTES_VALIDAS, ESTADOS_CAMPO_VALIDOS
from .campos import extraer_campos_deterministicos

__all__ = [
    "CAMPOS_VALIDOS", "FUENTES_VALIDAS", "ESTADOS_CAMPO_VALIDOS",
    "extraer_campos_deterministicos",
]
