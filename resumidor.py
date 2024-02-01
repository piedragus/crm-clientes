"""
resumidor.py — Resume cotizaciones usando Gemini o Grok (fallback automático).

Dado el texto extraído de un archivo de cotización, devuelve un dict con:
  - resumen:    descripción breve del documento (1-2 líneas)
  - monto:      monto detectado como float (None si no hay)
  - moneda:     "ARS", "USD", "EUR", etc. (None si no hay)
  - tipo:       "Propuesta" | "Contrato" | "Factura" | "Presupuesto" | "Otro"
  - proveedor:  empresa que emite el documento (None si no detecta)
  - fecha_doc:  fecha del documento YYYY-MM-DD (None si no hay)
  - confianza:  0.0–1.0 (seguridad sobre el monto)
  - proveedor_ia: qué proveedor generó el resumen ("gemini"|"grok"|"none")

Variables de entorno:
  GEMINI_API_KEY  o  GOOGLE_API_KEY   → usa Gemini 2.0 Flash
  GROK_API_KEY    o  XAI_API_KEY      → usa Grok mini (fallback)

Orden: Gemini primero, Grok si Gemini no tiene key o falla.
Si ninguno está disponible devuelve defaults sin crashear.
"""
from __future__ import annotations

import json
import os
import re

# ── Modelos ───────────────────────────────────────────────────────────────────
from config_export import get_app_config as _cfg
GEMINI_MODEL = _cfg().get("gemini_model")
GROK_MODEL   = _cfg().get("grok_model")
MAX_TOKENS   = _cfg().get("ai_max_tokens")
TEXT_LIMIT   = 6000   # chars que se mandan al modelo

# ── Prompt compartido ─────────────────────────────────────────────────────────
PROMPT = """\
Analizá el siguiente texto extraído de un documento de cotización, propuesta \
o contrato comercial y devolvé ÚNICAMENTE un objeto JSON válido con estos campos:

{{
  "resumen":   "<1-2 líneas describiendo de qué trata el documento>",
  "monto":     <número sin comas ni símbolo de moneda, ej: 15000.00, o null>,
  "moneda":    "<ARS|USD|EUR|BRL|CLP|UYU|otro código ISO, o null>",
  "tipo":      "<Propuesta|Contrato|Factura|Presupuesto|Orden de compra|Otro>",
  "proveedor": "<empresa o persona que emite el documento, o null>",
  "fecha_doc": "<YYYY-MM-DD, o null>",
  "confianza": <0.0 a 1.0, qué tan seguro estás del monto detectado>
}}

Reglas:
- Si hay varios montos, usá el total o el más relevante.
- Si la moneda no está explícita pero el contexto es Argentina, asumí ARS.
- Devolvé SOLO el JSON. Sin markdown, sin texto extra.

Texto del documento:
---
{texto}
---"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_response(raw: str) -> dict:
    raw = _strip_fences(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: buscar el primer objeto JSON en el texto
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"Respuesta no parseable: {raw[:200]}")
        data = json.loads(m.group())

    monto = data.get("monto")
    if monto is not None:
        try:
            s = str(monto).strip()
            # Strip currency symbols and spaces
            for sym in ("$", "USD", "ARS", "EUR", "€", "£", " "):
                s = s.replace(sym, "")
            s = s.strip()
            # Detect numeric format:
            # Argentine/European: "1.500,00" (. = thousands, , = decimal)
            # US/standard:        "1,500.00" (, = thousands, . = decimal)
            if "," in s and "." in s:
                if s.rindex(",") > s.rindex("."):
                    # comma is decimal separator: "1.500,00" -> "1500.00"
                    s = s.replace(".", "").replace(",", ".")
                else:
                    # period is decimal separator: "1,500.00" -> "1500.00"
                    s = s.replace(",", "")
            elif "," in s:
                parts = s.split(",")
                # If 2 parts and last has exactly 3 digits -> thousands sep
                if len(parts) == 2 and len(parts[1]) == 3 and parts[1].isdigit():
                    s = s.replace(",", "")
                else:
                    s = s.replace(",", ".")
            monto = float(s)
        except (ValueError, TypeError):
            monto = None

    fecha = _parse_fecha(data.get("fecha_doc"))

    return {
        "resumen":   str(data.get("resumen")   or "").strip()[:300],
        "monto":     monto,
        "moneda":    (str(data.get("moneda") or "").upper()[:5] or None),
        "tipo":      str(data.get("tipo")      or "Otro").strip()[:50],
        "proveedor": (str(data.get("proveedor") or "").strip()[:100] or None),
        "fecha_doc": fecha,
        "confianza": min(1.0, max(0.0, float(data.get("confianza") or 0))),
    }


def _parse_fecha(s) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.match(r"(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return None


def _defaults(resumen: str, proveedor_ia: str = "none") -> dict:
    return {
        "resumen":      resumen,
        "monto":        None,
        "moneda":       None,
        "tipo":         "Otro",
        "proveedor":    None,
        "fecha_doc":    None,
        "confianza":    0.0,
        "proveedor_ia": proveedor_ia,
    }


# ── Gemini ────────────────────────────────────────────────────────────────────

def _llamar_gemini(texto: str) -> dict:
    try:
        from google import genai
        from google.genai import types as gt
    except ImportError:
        raise RuntimeError("Falta google-genai: pip install google-genai")

    api_key = (os.environ.get("GEMINI_API_KEY") or
               os.environ.get("GOOGLE_API_KEY") or "")
    if not api_key:
        raise RuntimeError("Sin GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)
    # Sin grounding (no necesitamos búsqueda web para analizar un documento local)
    # Forzamos JSON con response_mime_type — aquí SÍ es compatible porque no
    # hay google_search tool.
    cfg = gt.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1,
        max_output_tokens=MAX_TOKENS,
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=PROMPT.format(texto=texto[:TEXT_LIMIT]),
        config=cfg,
    )
    raw = resp.text or "{}"
    result = _parse_response(raw)
    result["proveedor_ia"] = "gemini"
    return result


# ── Grok ──────────────────────────────────────────────────────────────────────

def _llamar_grok(texto: str) -> dict:
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("Falta openai: pip install openai")

    api_key = (os.environ.get("GROK_API_KEY") or
               os.environ.get("XAI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("Sin GROK_API_KEY")

    client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    resp = client.chat.completions.create(
        model=GROK_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Sos un asistente que analiza documentos comerciales. "
                    "Siempre devolvés SOLO JSON válido, sin texto adicional."
                ),
            },
            {
                "role": "user",
                "content": PROMPT.format(texto=texto[:TEXT_LIMIT]),
            },
        ],
        temperature=0.1,
        max_tokens=MAX_TOKENS,
        # Pedimos JSON explícitamente
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    result = _parse_response(raw)
    result["proveedor_ia"] = "grok"
    return result


# ── API pública ───────────────────────────────────────────────────────────────

def resumir(texto: str) -> dict:
    """
    Intenta resumir con Gemini, luego con Grok como fallback.
    Nunca lanza excepciones — devuelve defaults si ambos fallan.
    """
    if not texto or not texto.strip():
        return _defaults("Archivo sin texto extraíble")

    # Intentar Gemini
    gemini_key = (os.environ.get("GEMINI_API_KEY") or
                  os.environ.get("GOOGLE_API_KEY") or "")
    if gemini_key:
        try:
            return _llamar_gemini(texto)
        except Exception as exc:
            print(f"[resumidor] Gemini falló ({exc}), intentando Grok...")

    # Fallback: Grok
    grok_key = (os.environ.get("GROK_API_KEY") or
                os.environ.get("XAI_API_KEY") or "")
    if grok_key:
        try:
            return _llamar_grok(texto)
        except Exception as exc:
            return _defaults(f"Error en Grok: {exc}", "grok-error")

    # Sin keys
    return _defaults(
        "Configurá GEMINI_API_KEY o GROK_API_KEY para activar resúmenes automáticos",
        "none",
    )
