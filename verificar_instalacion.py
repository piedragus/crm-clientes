"""
Verifica dependencias, base de datos y migraciones básicas del CRM.
No modifica datos, salvo crear tablas/columnas faltantes a través de DBManager.
"""
from __future__ import annotations
import importlib
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reportes")
os.makedirs(REPORT_DIR, exist_ok=True)

CHECKS = [
    ("ttkbootstrap", "ttkbootstrap"),
    ("fuzzywuzzy", "fuzzywuzzy"),
    ("python-Levenshtein", "Levenshtein"),
    ("thefuzz", "thefuzz"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("google-genai", "google.genai"),
    ("openai", "openai"),
    ("pdfplumber", "pdfplumber"),
    ("python-docx", "docx"),
    ("python-pptx", "pptx"),
]



def _db_path() -> str:
    import configparser
    cfg = configparser.ConfigParser()
    cfg_path = os.path.join(BASE_DIR, "config.ini")
    cfg.read(cfg_path, encoding="utf-8")
    name = cfg.get("database", "name", fallback="clientes_v2.db")
    return name if os.path.isabs(name) else os.path.join(BASE_DIR, name)

REQUIRED_COT_COLS = {
    "id", "empresa_id", "fecha", "descripcion", "monto", "moneda",
    "ruta_archivo", "nombre_archivo", "tipo", "resumen", "proveedor_ia",
}


def _ok(msg: str) -> str:
    return f"OK   {msg}"


def _bad(msg: str) -> str:
    return f"FAIL {msg}"


def main() -> int:
    lines = ["Verificación de instalación CRM", f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    failures = 0

    lines.append("Dependencias:")
    for pkg, mod in CHECKS:
        try:
            importlib.import_module(mod)
            lines.append(_ok(pkg))
        except Exception as exc:
            failures += 1
            lines.append(_bad(f"{pkg}: {exc}"))

    lines.append("")
    lines.append("Base de datos:")
    try:
        from db_manager import DBManager
        db_path = _db_path()
        DBManager(db_path)  # ejecuta migraciones seguras
        lines.append(_ok(f"DB accesible: {db_path}"))
        conn = sqlite3.connect(db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ["empresas", "contactos", "cotizaciones", "cambios", "tags", "empresa_tags"]:
                if table in tables:
                    lines.append(_ok(f"tabla {table}"))
                else:
                    failures += 1
                    lines.append(_bad(f"falta tabla {table}"))
            cot_cols = {r[1] for r in conn.execute("PRAGMA table_info(cotizaciones)")}
            missing = sorted(REQUIRED_COT_COLS - cot_cols)
            if missing:
                failures += 1
                lines.append(_bad("faltan columnas cotizaciones: " + ", ".join(missing)))
            else:
                lines.append(_ok("schema cotizaciones completo"))
            for table in ["empresas", "contactos", "cotizaciones"]:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                lines.append(_ok(f"{table}: {n} filas"))
        finally:
            conn.close()
    except Exception as exc:
        failures += 1
        lines.append(_bad(f"DB: {exc}"))

    lines.append("")
    lines.append("Resultado: " + ("OK" if failures == 0 else f"{failures} problema(s)"))
    report = "\n".join(lines) + "\n"
    out = os.path.join(REPORT_DIR, f"verificacion_{datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"Reporte: {out}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
