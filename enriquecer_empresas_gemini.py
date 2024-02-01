"""
enriquecer_empresas.py — Enriquece nombres de empresas con IA + búsqueda web.

Proveedores soportados (en orden de fallback):
  1. Gemini 2.5 Flash con Google Search grounding  (GEMINI_API_KEY)
  2. Grok con búsqueda web                          (GROK_API_KEY / XAI_API_KEY)

Flujo seguro:
  - Por defecto solo genera un CSV con sugerencias. NO modifica la DB.
  - Usá --apply para aplicar cambios con confianza >= --min-confidence (default 0.85).
  - Podés revisar el CSV antes de aplicar.

Uso:
  # Windows
  set GEMINI_API_KEY=tu_key
  py enriquecer_empresas.py --limit 50
  py enriquecer_empresas.py --only "roca, acme" --apply

  # Linux / Mac
  export GEMINI_API_KEY=tu_key
  python3 enriquecer_empresas.py --limit 50 --provider gemini

  # Usar Grok directamente
  export GROK_API_KEY=tu_key
  python3 enriquecer_empresas.py --provider grok

  # Fallback automático (Gemini → Grok → error)
  python3 enriquecer_empresas.py --provider auto
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from db_manager import DBManager
from utils import Config


# ── Helpers de texto ──────────────────────────────────────────────────────────

def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


SUSPICIOUS_EXACT = {
    "gmail", "hotmail", "outlook", "yahoo", "icloud", "live", "msn",
    "info", "mail", "email", "contacto", "ventas", "admin", "no",
    "sin empresa", "sin_empresa", "unknown", "n/a", "na",
    "protonmail", "proton",
}

SUSPICIOUS_PATTERNS = [
    r"^\d+$",                         # solo números
    r"^[\W_]+$",                       # solo símbolos
    r"^[a-z]{2,4}$",                   # TLD solo (com, net, org, ar…)
    r"^(www|ftp|mail|smtp|pop)\d*$",   # subdominios técnicos
]

def looks_suspicious(name: str) -> bool:
    n = normalize(name)
    if not n or len(n) <= 2:
        return True
    low = n.lower()
    if low in SUSPICIOUS_EXACT:
        return True
    for pat in SUSPICIOUS_PATTERNS:
        if re.fullmatch(pat, low):
            return True
    return False


def strip_json_fences(text: str) -> str:
    """Elimina bloques ```json ... ``` que algunos modelos agregan al JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def parse_json_response(text: str) -> list[dict]:
    """Parsea la respuesta del modelo, tolerando fences y texto extra."""
    text = strip_json_fences(text)
    # Intentar parseo directo
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        # A veces devuelve {"results": [...]}
        for key in ("results", "empresas", "data", "companies"):
            if isinstance(result, dict) and key in result:
                return result[key]
        return [result] if isinstance(result, dict) else []
    except json.JSONDecodeError:
        pass
    # Fallback: extraer el primer array JSON del texto
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return []


# ── Prompt ────────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """\
Sos un asistente de limpieza de datos de un CRM B2B latinoamericano.

Tarea: para cada empresa en la lista, determiná si el nombre actual es \
correcto, sospechoso o incompleto, y sugerí el nombre canónico usando \
búsqueda web cuando sea necesario.

Reglas:
- Si el nombre parece un dominio de email (gmail, hotmail, acmecorp, etc.), \
buscá la empresa real y corregilo.
- Si el nombre ya parece correcto y verificable, dejalo igual con confidence alto.
- Si no encontrás evidencia, dejá canonical_name igual al nombre actual \
y confidence <= 0.4.
- should_update = true solo si canonical_name es distinto al nombre actual \
Y tenés evidencia real.
- Devolvé ÚNICAMENTE un array JSON válido, sin texto adicional, sin markdown.

Formato de cada elemento:
{{
  "id": <número entero exacto del input>,
  "current_name": "<nombre actual>",
  "canonical_name": "<nombre sugerido>",
  "country": "<país ISO o nombre>",
  "website": "<url o vacío>",
  "confidence": <0.0 a 1.0>,
  "should_update": <true|false>,
  "reason": "<justificación breve>"
}}

IMPORTANTE: el campo "id" debe ser exactamente el mismo número que recibiste.
No inventes ids. Devolvé exactamente {n} elementos.

Empresas a analizar:
{data}
"""

def build_prompt(empresas: list[dict]) -> str:
    items = [
        {
            "id": e["id"],
            "nombre_actual": e.get("nombre") or "",
            "pais_actual":   e.get("pais")   or "",
            "rubro_actual":  e.get("rubro")  or "",
            "email_actual":  e.get("email")  or "",
        }
        for e in empresas
    ]
    return PROMPT_TEMPLATE.format(
        n=len(items),
        data=json.dumps(items, ensure_ascii=False, indent=2)
    )


# ── Retry con backoff exponencial ─────────────────────────────────────────────

def with_retry(fn, max_attempts: int = 4, base_delay: float = 4.0):
    """Llama fn(), reintenta con backoff en caso de rate-limit o error transitorio."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc).lower()
            # Errores de rate-limit o servidor
            is_retryable = any(k in msg for k in (
                "rate", "429", "quota", "too many", "503", "overloaded",
                "resource_exhausted", "server error"
            ))
            if not is_retryable or attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"  [!] Error transitorio: {exc}. Reintentando en {delay:.0f}s...")
            time.sleep(delay)


# ── Proveedor: Gemini ─────────────────────────────────────────────────────────

def call_gemini(empresas: list[dict], model: str) -> list[dict]:
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        raise RuntimeError(
            "Falta google-genai. Instalá con: pip install google-genai"
        )

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta GEMINI_API_KEY en las variables de entorno.\n"
            "Ejemplo: set GEMINI_API_KEY=tu_api_key"
        )

    client = genai.Client(api_key=api_key)

    # NOTA: google_search grounding y response_mime_type="application/json"
    # son INCOMPATIBLES en la API de Gemini. Usamos grounding sin mime_type
    # forzado y parseamos el JSON manualmente.
    grounding = gtypes.Tool(google_search=gtypes.GoogleSearch())
    gen_config = gtypes.GenerateContentConfig(
        tools=[grounding],
        temperature=0.1,
    )

    def _call():
        response = client.models.generate_content(
            model=model,
            contents=build_prompt(empresas),
            config=gen_config,
        )
        text = response.text or "[]"
        result = parse_json_response(text)
        if not result:
            raise RuntimeError(
                f"Gemini devolvió respuesta no parseable:\n{text[:300]}"
            )
        return result

    return with_retry(_call)


# ── Proveedor: Grok (xAI) ─────────────────────────────────────────────────────

def call_grok(empresas: list[dict], model: str) -> list[dict]:
    """
    Grok via OpenAI-compatible API de xAI.
    Modelos recomendados: grok-3-mini, grok-3
    Con search_parameters activa la búsqueda web en tiempo real.
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError(
            "Falta openai. Instalá con: pip install openai"
        )

    api_key = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta GROK_API_KEY en las variables de entorno.\n"
            "Ejemplo: set GROK_API_KEY=tu_api_key"
        )

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
    )

    def _call():
        # Grok soporta search_parameters para activar búsqueda web real
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sos un asistente de limpieza de datos de CRM. "
                        "Usás búsqueda web para verificar nombres de empresas. "
                        "Devolvés SOLO JSON válido sin markdown."
                    ),
                },
                {"role": "user", "content": build_prompt(empresas)},
            ],
            temperature=0.1,
            # Activar búsqueda web de Grok
            extra_body={
                "search_parameters": {
                    "mode": "auto",           # busca cuando lo considera útil
                    "return_citations": False,
                }
            },
        )
        text = response.choices[0].message.content or "[]"
        result = parse_json_response(text)
        if not result:
            raise RuntimeError(
                f"Grok devolvió respuesta no parseable:\n{text[:300]}"
            )
        return result

    return with_retry(_call)


# ── Dispatcher de proveedor ───────────────────────────────────────────────────

from config_export import get_app_config as _cfg
PROVIDERS = {
    "gemini": (call_gemini, _cfg().get("gemini_model"), "GEMINI_API_KEY"),
    "grok":   (call_grok,   _cfg().get("grok_model"),   "GROK_API_KEY"),
}

def call_provider(
    provider: str,
    empresas: list[dict],
    model: str | None,
) -> tuple[list[dict], str]:
    """
    Llama al proveedor indicado. Con provider='auto' intenta Gemini primero,
    luego Grok como fallback.
    Devuelve (resultados, nombre_proveedor_usado).
    """
    if provider == "auto":
        order = ["gemini", "grok"]
    elif provider in PROVIDERS:
        order = [provider]
    else:
        raise ValueError(f"Proveedor desconocido: {provider!r}. Opciones: {list(PROVIDERS.keys()) + ['auto']}")

    last_exc = None
    for p in order:
        fn, default_model, key_name = PROVIDERS[p]
        actual_model = model or default_model
        has_key = bool(os.environ.get(key_name) or
                       (p == "grok" and os.environ.get("XAI_API_KEY")) or
                       (p == "gemini" and os.environ.get("GOOGLE_API_KEY")))
        if not has_key:
            print(f"  [skip] {p}: no se encontró {key_name} en el entorno.")
            continue
        try:
            print(f"  Usando proveedor: {p} / modelo: {actual_model}")
            result = fn(empresas, actual_model)
            return result, p
        except Exception as exc:
            print(f"  [!] {p} falló: {exc}")
            last_exc = exc

    providers_tried = ", ".join(order)
    raise RuntimeError(
        f"Todos los proveedores fallaron ({providers_tried}). "
        f"Último error: {last_exc}"
    )


# ── Validación de resultados ──────────────────────────────────────────────────

def validate_results(
    results: list[dict],
    batch_ids: set[int],
) -> list[dict]:
    """
    Filtra resultados con ids que no estaban en el batch enviado.
    Evita que el modelo alucine ids y pise empresas que no corresponden.
    """
    valid = []
    for r in results:
        try:
            rid = int(r.get("id") or -1)
        except (ValueError, TypeError):
            print(f"  [WARN] Resultado con id inválido ignorado: {r.get('id')!r}")
            continue
        if rid not in batch_ids:
            print(f"  [WARN] id {rid} no estaba en el batch enviado — ignorado.")
            continue
        valid.append({**r, "id": rid})  # normalizar id a int
    return valid


# ── Carga de empresas ─────────────────────────────────────────────────────────

def load_empresas(
    db: DBManager,
    limit: int | None,
    only: str | None,
    all_companies: bool,
) -> list[dict]:
    params: list[Any] = []
    where: list[str] = []

    if only:
        terms = [t.strip() for t in only.split(",") if t.strip()]
        if terms:
            where.append(
                "(" + " OR ".join(["nombre LIKE ?"] * len(terms)) + ")"
            )
            params.extend(f"%{t}%" for t in terms)

    query = "SELECT id, nombre, pais, rubro, email FROM empresas"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY nombre COLLATE NOCASE"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = db.fetchall(query, tuple(params))

    if not all_companies:
        rows = [r for r in rows if looks_suspicious(r.get("nombre", ""))]

    return rows


# ── Aplicar cambios a la DB ───────────────────────────────────────────────────

def safe_apply(
    db: DBManager,
    results: list[dict],
    min_confidence: float,
    fuente: str = "ia",
) -> tuple[int, int]:
    updated = skipped = 0

    for r in results:
        try:
            confidence   = float(r.get("confidence") or 0)
            should_update = bool(r.get("should_update"))
            current      = normalize(r.get("current_name") or "")
            canonical    = normalize(r.get("canonical_name") or "")
            empresa_id   = r.get("id")

            if not empresa_id or not should_update:
                skipped += 1; continue
            if confidence < min_confidence:
                skipped += 1; continue
            if not canonical or canonical.lower() == current.lower():
                skipped += 1; continue

            empresa = db.obtener_empresa_por_id(empresa_id)
            if not empresa:
                print(f"  [WARN] Empresa id={empresa_id} no encontrada en DB.")
                skipped += 1; continue

            tags = ", ".join(db.get_tags_de_empresa(empresa_id))
            ok = db.editar_empresa(
                empresa_id,
                canonical,
                empresa.get("direccion") or "",
                empresa.get("telefono")  or "",
                empresa.get("email")     or "",
                empresa.get("rubro")     or "",
                r.get("country") or empresa.get("pais") or "",
                tags,
                fuente=fuente,
            )
            if ok:
                print(f"  ✓ [{empresa_id}] {current!r} → {canonical!r}")
                updated += 1
            else:
                skipped += 1

        except Exception as exc:
            print(f"  [WARN] Error aplicando id={r.get('id')}: {exc}")
            skipped += 1

    return updated, skipped


# ── Reporte CSV ───────────────────────────────────────────────────────────────

REPORT_FIELDS = [
    "id", "current_name", "canonical_name", "country",
    "website", "confidence", "should_update", "reason",
]

def write_report(results: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not results:
        print("Sin resultados para reportar.")
        # Still create empty file so callers can check existence
        output_path.touch()
        return
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in REPORT_FIELDS})
    print(f"Reporte guardado: {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Enriquece nombres de empresas con IA + búsqueda web.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  py enriquecer_empresas.py --limit 30
  py enriquecer_empresas.py --all --limit 200 --provider grok
  py enriquecer_empresas.py --only "acme, roca" --apply --min-confidence 0.9
  py enriquecer_empresas.py --provider auto --apply
        """,
    )
    p.add_argument("--provider", default="auto",
                   choices=["auto", "gemini", "grok"],
                   help="Proveedor de IA. 'auto' prueba Gemini, luego Grok.")
    p.add_argument("--model", default=None,
                   help="Modelo a usar (sobreescribe el default del proveedor).")
    p.add_argument("--limit", type=int, default=50,
                   help="Máximo de empresas a analizar (default: 50).")
    p.add_argument("--batch-size", type=int, default=_cfg().get("ai_batch_size"),
                   help="Empresas por llamada a la API (default: 10).")
    p.add_argument("--only", default=None,
                   help="Filtrar por nombres que contengan estos textos (coma-separados).")
    p.add_argument("--all", dest="all_companies", action="store_true",
                   help="Analizar TODAS las empresas, no solo las sospechosas.")
    p.add_argument("--apply", action="store_true",
                   help="Aplicar cambios a la DB (default: solo genera CSV).")
    p.add_argument("--min-confidence", type=float, default=0.85,
                   help="Confianza mínima para aplicar un cambio (default: 0.85).")
    p.add_argument("--db", default=None,
                   help="Ruta a la base de datos (default: la configurada en config.ini).")
    p.add_argument("--output", default=None,
                   help="Ruta del CSV de salida.")
    return p


def main() -> int:
    args = build_parser().parse_args()

    cfg     = Config()
    db_path = args.db or cfg.get_db_name()
    db      = DBManager(db_path)

    print(f"DB: {db_path}")
    empresas = load_empresas(
        db,
        limit=args.limit,
        only=args.only,
        all_companies=args.all_companies,
    )

    if not empresas:
        print("No hay empresas para analizar con los filtros indicados.")
        if not args.all_companies:
            print("Tip: usá --all para analizar todas las empresas.")
        return 0

    print(f"{len(empresas)} empresa(s) a analizar "
          f"({'todas' if args.all_companies else 'sospechosas'}).")
    if not args.apply:
        print("Modo seguro: se generará un CSV pero NO se modificará la DB.")
        print("Usá --apply para aplicar cambios confiables.\n")

    batch_size = max(1, args.batch_size)
    all_results: list[dict] = []
    provider_used = "?"

    for i in range(0, len(empresas), batch_size):
        batch    = empresas[i : i + batch_size]
        batch_ids = {int(e["id"]) for e in batch}
        start    = i + 1
        end      = i + len(batch)
        print(f"\nBatch {start}–{end} de {len(empresas)}...")

        try:
            raw, provider_used = call_provider(
                args.provider, batch, args.model)
            validated = validate_results(raw, batch_ids)

            # Completar con entries vacías para empresas que el modelo omitió
            returned_ids = {r["id"] for r in validated}
            for e in batch:
                if int(e["id"]) not in returned_ids:
                    validated.append({
                        "id":            e["id"],
                        "current_name":  e["nombre"],
                        "canonical_name":e["nombre"],
                        "country":       e.get("pais") or "",
                        "website":       "",
                        "confidence":    0.0,
                        "should_update": False,
                        "reason":        "No devuelto por el modelo",
                    })

            all_results.extend(validated)
            print(f"  OK — {len(validated)} resultado(s).")

        except Exception as exc:
            print(f"  ERROR en batch {start}–{end}: {exc}")
            # Agregar entries de error para no perder tracking
            for e in batch:
                all_results.append({
                    "id":            e["id"],
                    "current_name":  e["nombre"],
                    "canonical_name":e["nombre"],
                    "country":       e.get("pais") or "",
                    "website":       "",
                    "confidence":    0.0,
                    "should_update": False,
                    "reason":        f"Error: {exc}",
                })

        # Pausa entre batches para respetar rate limits
        if i + batch_size < len(empresas):
            time.sleep(2.0)

    # Reporte CSV
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output) if args.output else (
        Path("reportes") / f"empresas_{provider_used}_{ts}.csv"
    )
    write_report(all_results, out)

    # Aplicar si corresponde
    if args.apply:
        print(f"\nAplicando cambios (confianza >= {args.min_confidence})...")
        updated, skipped = safe_apply(db, all_results, args.min_confidence, fuente=provider_used)
        print(f"\nResultado: {updated} actualizada(s), {skipped} omitida(s).")
    else:
        candidates = sum(
            1 for r in all_results
            if r.get("should_update")
            and float(r.get("confidence") or 0) >= args.min_confidence
        )
        print(f"\n{candidates} empresa(s) listas para actualizar "
              f"(confianza >= {args.min_confidence}).")
        print(f"Revisá el CSV y usá --apply cuando estés conforme.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
