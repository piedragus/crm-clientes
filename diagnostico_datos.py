"""Genera un diagnóstico de datos del CRM y un CSV de problemas revisables."""
from __future__ import annotations
import csv
import os
import re
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reportes")
os.makedirs(REPORT_DIR, exist_ok=True)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PUBLIC_NAMES = {"gmail", "hotmail", "outlook", "yahoo", "icloud", "live", "msn"}


def _db_path() -> str:
    import configparser
    cfg = configparser.ConfigParser()
    cfg_path = os.path.join(BASE_DIR, "config.ini")
    cfg.read(cfg_path, encoding="utf-8")
    name = cfg.get("database", "name", fallback="clientes_v2.db")
    return name if os.path.isabs(name) else os.path.join(BASE_DIR, name)


def add(issues, gravedad, tipo, detalle, id_ref=""):
    issues.append({"gravedad": gravedad, "tipo": tipo, "id": id_ref, "detalle": detalle})


def main() -> int:
    from db_manager import DBManager
    db_path = _db_path()
    DBManager(db_path)  # asegura migraciones
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    issues = []
    try:
        for r in conn.execute("SELECT id, nombre FROM empresas WHERE nombre IS NULL OR trim(nombre)='' OR lower(trim(nombre)) IN ('gmail','hotmail','outlook','yahoo','icloud')"):
            add(issues, "alta", "empresa_nombre_sospechoso", f"Empresa con nombre sospechoso/vacío: {r['nombre']!r}", r["id"])
        for r in conn.execute("SELECT id, empresa_id, email FROM contactos"):
            if r["empresa_id"] is None:
                add(issues, "alta", "contacto_huerfano", f"Contacto sin empresa. Email={r['email'] or ''}", r["id"])
            if r["email"] and not EMAIL_RE.match(r["email"].strip()):
                add(issues, "media", "email_invalido", f"Email inválido: {r['email']}", r["id"])
        for r in conn.execute("SELECT id, empresa_id, ruta_archivo, nombre_archivo, fecha, monto FROM cotizaciones"):
            if r["empresa_id"] is None:
                add(issues, "alta", "cotizacion_huerfana", "Cotización sin empresa", r["id"])
            if r["fecha"] and str(r["fecha"]).startswith("1970-"):
                add(issues, "alta", "fecha_1970", f"Fecha sospechosa: {r['fecha']}", r["id"])
            # No verificamos existencia de rutas por defecto: en Windows/OneDrive/red
            # puede ser lento o bloquear si la unidad no está montada. Para auditar
            # archivos físicos conviene hacerlo en una herramienta dedicada.
            if r["monto"] is not None:
                try:
                    if float(r["monto"]) < 0:
                        add(issues, "media", "monto_negativo", f"Monto negativo: {r['monto']}", r["id"])
                except Exception:
                    add(issues, "media", "monto_invalido", f"Monto inválido: {r['monto']}", r["id"])
        # duplicados exactos por nombre normalizado
        seen = {}
        for r in conn.execute("SELECT id, nombre FROM empresas WHERE nombre IS NOT NULL"):
            key = re.sub(r"\s+", " ", r["nombre"].strip().lower())
            if not key:
                continue
            if key in seen:
                add(issues, "media", "empresa_duplicada_exacta", f"Duplicada exacta con id {seen[key]}: {r['nombre']}", r["id"])
            else:
                seen[key] = r["id"]
    finally:
        conn.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(REPORT_DIR, f"diagnostico_datos_{stamp}.csv")
    txt_path = os.path.join(REPORT_DIR, f"diagnostico_datos_{stamp}.txt")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["gravedad", "tipo", "id", "detalle"])
        writer.writeheader(); writer.writerows(issues)
    resumen = [
        "Diagnóstico de datos CRM",
        f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Problemas detectados: {len(issues)}",
        f"CSV: {csv_path}",
    ]
    counts = {}
    for i in issues:
        counts[i["tipo"]] = counts.get(i["tipo"], 0) + 1
    for k, v in sorted(counts.items()):
        resumen.append(f"- {k}: {v}")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(resumen) + "\n")
    print("\n".join(resumen))
    print(f"Reporte: {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
