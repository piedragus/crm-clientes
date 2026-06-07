"""Auto hooks para Pipeline de oportunidades.

Se carga automaticamente al iniciar Python desde el repo. Mantiene la feature
encapsulada sin modificar server.py/db_manager.py hasta que se aplique un patch
inline en una segunda pasada.
"""
import json
import sys
from urllib.parse import parse_qs

try:
    import pipeline_runtime as _pipeline
    from db_manager import DBManager
    _pipeline.patch_db_manager_class(DBManager)
except Exception:
    _pipeline = None


def _get_server_db():
    for mod_name in ("server", "__main__"):
        mod = sys.modules.get(mod_name)
        db = getattr(mod, "db", None) if mod else None
        if db is not None:
            return db
    return None


def _send_json(start_response, payload, status="200 OK"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def _read_json(environ):
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        size = 0
    if size <= 0:
        return {}
    raw = environ["wsgi.input"].read(size)
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _clean(v):
    return str(v or "").strip()


def _to_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _handle_pipeline(environ, start_response):
    if _pipeline is None:
        return None
    db = _get_server_db()
    if db is None:
        return None
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    qs = parse_qs(environ.get("QUERY_STRING", ""))

    def ok(data=None, status="200 OK", **kw):
        return _send_json(start_response, {"ok": True, "data": data, **kw}, status)

    def err(msg, code="400 Bad Request"):
        return _send_json(start_response, {"ok": False, "error": str(msg)}, code)

    try:
        if path == "/api/oportunidades" and method == "GET":
            filtros = {}
            for key in ("etapa", "fase", "empresa_id"):
                val = _clean((qs.get(key) or [""])[0])
                if val:
                    filtros[key] = val
            return ok(db.get_oportunidades(filtros))

        if path == "/api/oportunidades" and method == "POST":
            b = _read_json(environ)
            empresa_id = int(b.get("empresa_id") or 0)
            titulo = _clean(b.get("titulo"))
            if not titulo:
                return err("El titulo es obligatorio")
            if not db.obtener_empresa_por_id(empresa_id):
                return err("Empresa no encontrada", "404 Not Found")
            created = db.crear_oportunidad(
                empresa_id=empresa_id,
                titulo=titulo,
                descripcion=_clean(b.get("descripcion")),
                etapa=_clean(b.get("etapa") or "prospecto"),
                monto_estimado=_to_float(b.get("monto_estimado")),
                moneda=_clean(b.get("moneda") or "ARS"),
                fecha_estimada_cierre=_clean(b.get("fecha_estimada_cierre")),
                notas=_clean(b.get("notas")),
            )
            if not created:
                return err("No se pudo crear")
            row = db.fetchone(
                "SELECT id FROM oportunidades WHERE empresa_id=? AND titulo=? ORDER BY id DESC LIMIT 1",
                (empresa_id, titulo))
            return ok({"id": row["id"] if row else None}, "201 Created")

        if path.startswith("/api/oportunidades/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) < 3:
                return None
            oid = int(parts[2])
            if len(parts) == 3 and method == "GET":
                row = db.get_oportunidad_por_id(oid)
                if not row:
                    return err("No encontrada", "404 Not Found")
                return ok(row)
            if len(parts) == 3 and method == "PUT":
                b = _read_json(environ)
                campos = {}
                for key in ("titulo", "descripcion", "etapa", "moneda", "fecha_estimada_cierre", "notas"):
                    if key in b:
                        campos[key] = _clean(b.get(key))
                if "monto_estimado" in b:
                    campos["monto_estimado"] = _to_float(b.get("monto_estimado"))
                if not db.editar_oportunidad(oid, **campos):
                    return err("No se pudo guardar", "404 Not Found")
                return ok()
            if len(parts) == 3 and method == "DELETE":
                if not db.eliminar_oportunidad(oid):
                    return err("No encontrada", "404 Not Found")
                return ok()
            if len(parts) == 4 and parts[3] == "etapa" and method == "PUT":
                b = _read_json(environ)
                etapa = _clean(b.get("etapa"))
                if not _pipeline.normalizar_etapa(etapa):
                    return err("Etapa invalida", "400 Bad Request")
                if not db.cambiar_etapa_oportunidad(oid, etapa):
                    return err("No encontrada", "404 Not Found")
                return ok()

        if path.startswith("/api/empresas/") and path.endswith("/oportunidades") and method == "GET":
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4:
                eid = int(parts[2])
                if not db.obtener_empresa_por_id(eid):
                    return err("Empresa no encontrada", "404 Not Found")
                return ok(db.get_oportunidades_empresa(eid))
    except Exception as exc:
        return err(exc, "500 Internal Server Error")
    return None


try:
    from flask import Flask
    _orig_wsgi_app = Flask.wsgi_app

    def _patched_wsgi_app(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith("/api/oportunidades") or (path.startswith("/api/empresas/") and path.endswith("/oportunidades")):
            handled = _handle_pipeline(environ, start_response)
            if handled is not None:
                return handled
        return _orig_wsgi_app(self, environ, start_response)

    if not getattr(Flask, "_pipeline_wsgi_patched", False):
        Flask.wsgi_app = _patched_wsgi_app
        Flask._pipeline_wsgi_patched = True
except Exception:
    pass
