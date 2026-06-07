"""Pipeline de oportunidades para CRM Clientes.

Este modulo encapsula schema, metodos DB y endpoints Flask sin tocar todavia
cotizaciones ni catalogo. Se puede registrar desde server.py o via sitecustomize.
"""
import logging
from datetime import datetime

ETAPAS_VENTA = {
    "prospecto", "contactado", "a_visitar", "a_cotizar", "cotizado",
    "en_negociacion", "ganado", "perdido", "muerta",
}
ETAPAS_POSVENTA = {"en_proceso", "entregada", "finalizada"}
ETAPAS_TODAS = ETAPAS_VENTA | ETAPAS_POSVENTA


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalizar_etapa(etapa):
    etapa = str(etapa or "prospecto").strip().lower()
    return etapa if etapa in ETAPAS_TODAS else None


def fase_de_etapa(etapa):
    etapa = normalizar_etapa(etapa)
    if etapa in ETAPAS_POSVENTA:
        return "posventa"
    if etapa == "ganado":
        return "posventa"
    return "venta"


def _add_fase(row):
    if not row:
        return row
    d = dict(row)
    d["fase"] = fase_de_etapa(d.get("etapa"))
    return d


def ensure_oportunidades_schema(db):
    """Crea tabla e indices de oportunidades si no existen."""
    try:
        with db._write_lock:
            with db._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS oportunidades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id INTEGER NOT NULL,
                        titulo TEXT NOT NULL,
                        descripcion TEXT,
                        etapa TEXT NOT NULL DEFAULT 'prospecto',
                        monto_estimado REAL,
                        moneda TEXT DEFAULT 'ARS',
                        fecha_estimada_cierre TEXT,
                        fecha_creacion TEXT NOT NULL,
                        fecha_ultimo_cambio TEXT NOT NULL,
                        notas TEXT,
                        FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE
                    )
                """)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(oportunidades)").fetchall()]
                for col, ddl in [
                    ("descripcion", "ALTER TABLE oportunidades ADD COLUMN descripcion TEXT"),
                    ("moneda", "ALTER TABLE oportunidades ADD COLUMN moneda TEXT DEFAULT 'ARS'"),
                    ("fecha_estimada_cierre", "ALTER TABLE oportunidades ADD COLUMN fecha_estimada_cierre TEXT"),
                    ("notas", "ALTER TABLE oportunidades ADD COLUMN notas TEXT"),
                ]:
                    if col not in cols:
                        try:
                            conn.execute(ddl)
                        except Exception:
                            pass
                conn.execute("CREATE INDEX IF NOT EXISTS idx_oportunidades_empresa_id ON oportunidades (empresa_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_oportunidades_etapa ON oportunidades (etapa)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_oportunidades_fecha ON oportunidades (fecha_ultimo_cambio DESC)")
                conn.commit()
        return True
    except Exception as exc:
        logging.error(f"ensure_oportunidades_schema: {exc}")
        return False


def crear_oportunidad(self, empresa_id, titulo, descripcion="", etapa="prospecto",
                      monto_estimado=None, moneda="ARS", fecha_estimada_cierre="",
                      notas=""):
    if not empresa_id or not str(titulo or "").strip():
        return False
    etapa = normalizar_etapa(etapa)
    if not etapa:
        return False
    ensure_oportunidades_schema(self)
    fecha = now_str()
    try:
        with self._write_lock:
            with self._get_connection() as conn:
                emp = conn.execute("SELECT id FROM empresas WHERE id=?", (empresa_id,)).fetchone()
                if not emp:
                    return False
                conn.execute("""
                    INSERT INTO oportunidades
                    (empresa_id, titulo, descripcion, etapa, monto_estimado, moneda,
                     fecha_estimada_cierre, fecha_creacion, fecha_ultimo_cambio, notas)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (empresa_id, str(titulo).strip(), descripcion or "", etapa,
                      monto_estimado, moneda or "ARS", fecha_estimada_cierre or "",
                      fecha, fecha, notas or ""))
                conn.commit()
        return True
    except Exception as exc:
        logging.error(f"crear_oportunidad: {exc}")
        return False


def get_oportunidades(self, filtros=None):
    ensure_oportunidades_schema(self)
    filtros = filtros or {}
    where, params = [], []
    etapa = normalizar_etapa(filtros.get("etapa")) if filtros.get("etapa") else None
    if etapa:
        where.append("o.etapa=?"); params.append(etapa)
    if filtros.get("empresa_id"):
        where.append("o.empresa_id=?"); params.append(int(filtros["empresa_id"]))
    sql = """
        SELECT o.*, e.nombre AS empresa_nombre
        FROM oportunidades o
        JOIN empresas e ON e.id = o.empresa_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.fecha_ultimo_cambio DESC, o.id DESC"
    rows = [_add_fase(r) for r in self.fetchall(sql, tuple(params))]
    fase = str(filtros.get("fase") or "").strip().lower()
    if fase in ("venta", "posventa"):
        rows = [r for r in rows if r.get("fase") == fase]
    return rows


def get_oportunidades_empresa(self, empresa_id):
    return self.get_oportunidades({"empresa_id": empresa_id})


def get_oportunidad_por_id(self, oportunidad_id):
    ensure_oportunidades_schema(self)
    row = self.fetchone("""
        SELECT o.*, e.nombre AS empresa_nombre
        FROM oportunidades o
        JOIN empresas e ON e.id = o.empresa_id
        WHERE o.id=?
    """, (oportunidad_id,))
    return _add_fase(row) if row else None


def editar_oportunidad(self, oportunidad_id, **campos):
    if not oportunidad_id:
        return False
    ensure_oportunidades_schema(self)
    allowed = {
        "titulo", "descripcion", "etapa", "monto_estimado", "moneda",
        "fecha_estimada_cierre", "notas",
    }
    sets, vals = [], []
    for k, v in campos.items():
        if k not in allowed:
            continue
        if k == "titulo" and not str(v or "").strip():
            return False
        if k == "etapa":
            v = normalizar_etapa(v)
            if not v:
                return False
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    sets.append("fecha_ultimo_cambio=?")
    vals.append(now_str())
    vals.append(oportunidad_id)
    try:
        with self._write_lock:
            with self._get_connection() as conn:
                cur = conn.execute(
                    f"UPDATE oportunidades SET {', '.join(sets)} WHERE id=?",
                    tuple(vals))
                conn.commit()
                return cur.rowcount > 0
    except Exception as exc:
        logging.error(f"editar_oportunidad: {exc}")
        return False


def cambiar_etapa_oportunidad(self, oportunidad_id, nueva_etapa):
    etapa = normalizar_etapa(nueva_etapa)
    if not etapa:
        return False
    return self.editar_oportunidad(oportunidad_id, etapa=etapa)


def eliminar_oportunidad(self, oportunidad_id):
    if not oportunidad_id:
        return False
    ensure_oportunidades_schema(self)
    try:
        with self._write_lock:
            with self._get_connection() as conn:
                cur = conn.execute("DELETE FROM oportunidades WHERE id=?", (oportunidad_id,))
                conn.commit()
                return cur.rowcount > 0
    except Exception as exc:
        logging.error(f"eliminar_oportunidad: {exc}")
        return False


def patch_db_manager_class(DBManager):
    if getattr(DBManager, "_oportunidades_patched", False):
        return DBManager
    original_init = DBManager.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        ensure_oportunidades_schema(self)

    DBManager.__init__ = patched_init
    DBManager.crear_oportunidad = crear_oportunidad
    DBManager.get_oportunidades = get_oportunidades
    DBManager.get_oportunidades_empresa = get_oportunidades_empresa
    DBManager.get_oportunidad_por_id = get_oportunidad_por_id
    DBManager.editar_oportunidad = editar_oportunidad
    DBManager.cambiar_etapa_oportunidad = cambiar_etapa_oportunidad
    DBManager.eliminar_oportunidad = eliminar_oportunidad
    DBManager._oportunidades_patched = True
    return DBManager


def register_routes(app, db):
    if getattr(app, "_oportunidades_routes", False):
        return
    ensure_oportunidades_schema(db)
    from flask import request, jsonify

    def ok(data=None, **kw):
        return jsonify({"ok": True, "data": data, **kw})

    def err(msg, code=400):
        return jsonify({"ok": False, "error": str(msg)}), code

    def clean(v):
        return str(v or "").strip()

    def to_float(v, default=None):
        try:
            if v is None or v == "":
                return default
            return float(v)
        except Exception:
            return default

    @app.route("/api/oportunidades")
    def api_get_oportunidades():
        filtros = {}
        for key in ("etapa", "fase"):
            val = clean(request.args.get(key))
            if val:
                filtros[key] = val
        empresa_id = clean(request.args.get("empresa_id"))
        if empresa_id:
            filtros["empresa_id"] = int(empresa_id)
        return ok(db.get_oportunidades(filtros))

    @app.route("/api/oportunidades", methods=["POST"])
    def api_post_oportunidad():
        b = request.json or {}
        empresa_id = int(b.get("empresa_id") or 0)
        titulo = clean(b.get("titulo"))
        if not titulo:
            return err("El titulo es obligatorio")
        if not db.obtener_empresa_por_id(empresa_id):
            return err("Empresa no encontrada", 404)
        ok2 = db.crear_oportunidad(
            empresa_id=empresa_id,
            titulo=titulo,
            descripcion=clean(b.get("descripcion")),
            etapa=clean(b.get("etapa") or "prospecto"),
            monto_estimado=to_float(b.get("monto_estimado")),
            moneda=clean(b.get("moneda") or "ARS"),
            fecha_estimada_cierre=clean(b.get("fecha_estimada_cierre")),
            notas=clean(b.get("notas")),
        )
        if not ok2:
            return err("No se pudo crear")
        row = db.fetchone("SELECT id FROM oportunidades WHERE empresa_id=? AND titulo=? ORDER BY id DESC LIMIT 1", (empresa_id, titulo))
        return ok({"id": row["id"] if row else None}), 201

    @app.route("/api/oportunidades/<int:oid>")
    def api_get_oportunidad(oid):
        row = db.get_oportunidad_por_id(oid)
        if not row:
            return err("No encontrada", 404)
        return ok(row)

    @app.route("/api/oportunidades/<int:oid>", methods=["PUT"])
    def api_put_oportunidad(oid):
        b = request.json or {}
        campos = {}
        for key in ("titulo", "descripcion", "etapa", "moneda", "fecha_estimada_cierre", "notas"):
            if key in b:
                campos[key] = clean(b.get(key))
        if "monto_estimado" in b:
            campos["monto_estimado"] = to_float(b.get("monto_estimado"))
        ok2 = db.editar_oportunidad(oid, **campos)
        if not ok2:
            return err("No se pudo guardar", 404)
        return ok()

    @app.route("/api/oportunidades/<int:oid>", methods=["DELETE"])
    def api_delete_oportunidad(oid):
        ok2 = db.eliminar_oportunidad(oid)
        if not ok2:
            return err("No encontrada", 404)
        return ok()

    @app.route("/api/oportunidades/<int:oid>/etapa", methods=["PUT"])
    def api_put_oportunidad_etapa(oid):
        b = request.json or {}
        etapa = clean(b.get("etapa"))
        if not normalizar_etapa(etapa):
            return err("Etapa invalida", 400)
        ok2 = db.cambiar_etapa_oportunidad(oid, etapa)
        if not ok2:
            return err("No encontrada", 404)
        return ok()

    @app.route("/api/empresas/<int:eid>/oportunidades")
    def api_get_oportunidades_empresa(eid):
        if not db.obtener_empresa_por_id(eid):
            return err("Empresa no encontrada", 404)
        return ok(db.get_oportunidades_empresa(eid))

    app._oportunidades_routes = True
