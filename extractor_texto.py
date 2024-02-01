"""
extractor_texto.py — Extrae texto plano de PDF, DOCX, XLSX, PPTX, TXT.
Sin dependencias de GUI. Devuelve el texto o lanza excepción si no puede.
"""
from __future__ import annotations
import os, re

MAX_CHARS = 8000   # límite de texto a extraer (suficiente para un resumen)


def extraer(path: str) -> str:
    """
    Extrae texto del archivo indicado.
    Devuelve string (puede estar vacío si el archivo no tiene texto seleccionable).
    Lanza FileNotFoundError si el archivo no existe.
    Lanza ValueError si la extensión no está soportada.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Archivo no encontrado: {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return _pdf(path)
    elif ext in (".docx", ".doc"):
        return _docx(path)
    elif ext in (".xlsx", ".xls"):
        return _xlsx(path)
    elif ext == ".pptx":
        return _pptx(path)
    elif ext == ".txt":
        return _txt(path)
    else:
        raise ValueError(f"Extensión no soportada: {ext}")


# ── Extractores por formato ───────────────────────────────────────────────────

def _pdf(path: str) -> str:
    import pdfplumber
    texts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:15]:   # primeras 15 páginas es más que suficiente
            t = page.extract_text()
            if t:
                texts.append(t)
    return _clean("\n".join(texts))


def _docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text.strip())
    # Incluir tablas
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
            if row_text:
                parts.append(row_text)
    return _clean("\n".join(parts))


def _xlsx(path: str) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets[:3]:   # primeras 3 hojas
        for row in ws.iter_rows(values_only=True):
            row_text = " | ".join(
                str(c).strip() for c in row if c is not None and str(c).strip()
            )
            if row_text:
                parts.append(row_text)
    return _clean("\n".join(parts))


def _pptx(path: str) -> str:
    from pptx import Presentation
    prs = Presentation(path)
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                parts.append(shape.text.strip())
    return _clean("\n".join(parts))


def _txt(path: str) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            with open(path, "r", encoding=enc) as f:
                return _clean(f.read())
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _clean(f.read())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """Normaliza whitespace y trunca al límite de caracteres."""
    # Colapsar líneas en blanco múltiples
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Colapsar espacios múltiples en la misma línea
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:MAX_CHARS]
