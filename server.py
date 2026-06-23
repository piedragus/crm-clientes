"""
server.py — CRM Clientes v26 — Flask REST API
Arrancar: python server.py  →  http://localhost:5000
"""
import os, sys, json, threading, tempfile, logging, hashlib, importlib.util, time
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import DBManager
from utils import Config, BackupManager, Exportador, formatear_fecha
from utils.excepciones import (
    AliasValidationError,
    EmpresaNotFoundError,
    AliasConflictError,
)
from config_export import get_app_config

# ── Logging to file ───────────────────────────────────────────────────────────
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "crm.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload

# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return response

@app.route("/api/<path:p>", methods=["OPTIONS"])
def options_handler(p):
    return jsonify({}), 200

cfg  = Config()
acfg = get_app_config()
db   = DBManager(cfg.get_db_name())

# ── Rate limiting for IA endpoints ────────────────────────────────────────────
_ia_lock     = threading.Lock()
_ia_last     = 0.0
_IA_MIN_GAP  = 5.0   # seconds between enricher calls

def _ia_ratelimit():
    global _ia_last
    with _ia_lock:
        now = time.time()
        wait = _IA_MIN_GAP - (now - _ia_last)
        if wait > 0:
            return False, f"Esperá {wait:.0f}s antes de otra llamada IA"
        _ia_last = now
        return True, ""

# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(data=None, **kw):
    return jsonify({"ok": True, "data": data, **kw})

def err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code

def rows_to_list(rs):
    return [dict(r) for r in (rs or [])]

def clean(v):
    return str(v or "").strip()

def to_int(v, default=0, lo=None, hi=None):
    try: n = int(v)
    except: n = default
    if lo is not None: n = max(lo, n)
    if hi is not None: n = min(hi, n)
    return n

def to_float(v, default=None):
    try:
        if v is None or v == "": return default
        return float(v)
    except: return default

def _safe_find_spec(module_name: str) -> bool:
    """importlib.util.find_spec can raise ModuleNotFoundError for submodules."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

def crear_backup_seguro(motivo="manual"):
    try: return BackupManager.hacer_backup(cfg.get_db_name())
    except Exception as exc:
        logging.warning(f"Backup omitido ({motivo}): {exc}")
        return False

# ── Background IA ─────────────────────────────────────────────────────────────
def ejecutar_resumen_bg(cotizacion_id):
    row = db.get_cotizacion_por_id(cotizacion_id)
    if not row:
        return
    ruta = row.get("ruta_archivo")
    if not ruta or not os.path.isfile(ruta):
        db.set_estado_ia_cotizacion(cotizacion_id, "error", "Archivo no encontrado")
        return
    db.set_estado_ia_cotizacion(cotizacion_id, "procesando", None)
    try:
        if not row.get("archivo_hash"):
            try: db.actualizar_hash_cotizacion(cotizacion_id, file_sha256(ruta))
            except Exception as exc: logging.warning(f"Hash: {exc}")
        from extractor_texto import extraer
        from resumidor import resumir
        texto = extraer(ruta)
        data  = resumir(texto)
        ok2   = db.actualizar_resumen_cotizacion_por_ruta(
            row["empresa_id"], ruta,
            resumen=data.get("resumen",""),
            monto=data.get("monto"),
            moneda=data.get("moneda"),
            proveedor_ia=data.get("proveedor_ia","none"),
            tipo=data.get("tipo"))
        if not ok2:
            db.set_estado_ia_cotizacion(cotizacion_id, "error", "No se pudo guardar")
    except Exception as exc:
        logging.error(f"resumen bg {cotizacion_id}: {exc}")
        db.set_estado_ia_cotizacion(cotizacion_id, "error", str(exc)[:500])

def lanzar_resumen(cid):
    threading.Thread(target=ejecutar_resumen_bg, args=(cid,), daemon=True).start()

# ── SPA ───────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def stats():
    hace30 = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    r = db.fetchone(
        "SELECT COUNT(DISTINCT empresa_id) n FROM cotizaciones WHERE fecha>=?", (hace30,))
    return ok({
        "empresas":     db.count("empresas"),
        "contactos":    db.count("contactos"),
        "cotizaciones": db.count("cotizaciones"),
        "activas_30d":  r["n"] if r else 0,
    })

# ── Empresas ──────────────────────────────────────────────────────────────────
@app.route("/api/empresas")
def get_empresas():
    q       = request.args.get("q", "")
    filtros = {}
    for k in ("pais","rubro","tag","cotizaciones_cond","contactos_cond"):
        v = request.args.get(k)
        if v: filtros[k] = v
    dias = to_int(request.args.get("dias_cotizacion"), 0, lo=0)
    if dias: filtros["dias_cotizacion"] = dias
    dias_act = to_int(request.args.get("dias_actividad"), 0, lo=0)
    if dias_act: filtros["dias_actividad"] = dias_act

    page      = max(0, to_int(request.args.get("page", 0), 0))
    page_size = max(10, min(to_int(request.args.get("page_size", 250), 250), 500))

    all_rows  = db.get_filtered_empresas(q, filtros)
    total     = len(all_rows)
    pages     = max(1, -(-total // page_size))
    rows      = all_rows[page * page_size : (page + 1) * page_size]

    if not rows:
        return ok([], total=total, page=page, pages=pages)

    # Bulk fetch stats — 2 queries instead of N*3
    ids           = [r["id"] for r in rows]
    placeholders  = ",".join("?" * len(ids))

    # Get ncot + ultima_fecha in one query
    base_stats = {r["empresa_id"]: r for r in db.fetchall(f"""
        SELECT empresa_id, COUNT(*) ncot, MAX(fecha) ultima_fecha
        FROM cotizaciones
        WHERE empresa_id IN ({placeholders})
        GROUP BY empresa_id
    """, tuple(ids))}

    # Get last cotizacion (monto+moneda) per empresa using ORDER BY fecha DESC, id DESC
    # This avoids the MAX(CASE WHEN) bug when two rows share the same max fecha
    last_cot = {}
    for eid2 in ids:
        row = db.fetchone(
            "SELECT monto, moneda FROM cotizaciones "
            "WHERE empresa_id=? ORDER BY fecha DESC, id DESC LIMIT 1",
            (eid2,))
        if row:
            last_cot[eid2] = row

    # Merge into cot_stats
    cot_stats = {}
    for eid2, stat in base_stats.items():
        lc = last_cot.get(eid2)
        cot_stats[eid2] = {
            "empresa_id":   eid2,
            "ncot":         stat["ncot"],
            "ultima_fecha": stat["ultima_fecha"],
            "ultimo_monto": float(lc["monto"] or 0) if lc else 0,
            "ultima_moneda": (lc["moneda"] or "")   if lc else "",
        }

    tags_map = {}
    for r in db.fetchall(f"""
        SELECT et.empresa_id, GROUP_CONCAT(t.tag, ',') tags
        FROM empresa_tags et
        JOIN tags t ON et.tag_id = t.id
        WHERE et.empresa_id IN ({placeholders})
        GROUP BY et.empresa_id
    """, tuple(ids)):
        tags_map[r["empresa_id"]] = [t for t in (r["tags"] or "").split(",") if t]

    result = []
    for r in rows:
        d    = dict(r)
        eid  = r["id"]
        stat = cot_stats.get(eid)
        d["ncot"]          = stat["ncot"]           if stat else 0
        d["ultima"]        = formatear_fecha(stat["ultima_fecha"] or "") if stat else ""
        d["ultimo_monto"]  = stat["ultimo_monto"]  if stat else 0
        d["ultima_moneda"] = stat["ultima_moneda"] if stat else ""
        d["tags"]          = tags_map.get(eid, [])
        result.append(d)

    return ok(result, total=total, page=page, pages=pages)

@app.route("/api/empresas/<int:eid>")
def get_empresa(eid):
    e = db.obtener_empresa_por_id(eid)
    if not e: return err("No encontrada", 404)
    d = dict(e)
    d["tags"] = db.get_tags_de_empresa(eid)
    return ok(d)

@app.route("/api/empresas", methods=["POST"])
def post_empresa():
    b = request.json or {}
    nombre = clean(b.get("nombre"))
    if not nombre: return err("El nombre es obligatorio")
    ok2 = db.agregar_empresa(nombre, clean(b.get("direccion")),
                              clean(b.get("telefono")), clean(b.get("email")),
                              clean(b.get("rubro")),    clean(b.get("pais")),
                              clean(b.get("tags")))
    if not ok2: return err("No se pudo crear")
    e = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (nombre,))
    return ok({"id": e["id"] if e else None}), 201

@app.route("/api/empresas/<int:eid>", methods=["PUT"])
def put_empresa(eid):
    b = request.json or {}
    nombre = clean(b.get("nombre"))
    if not nombre: return err("El nombre es obligatorio")
    ok2 = db.editar_empresa(eid, nombre, clean(b.get("direccion")),
                             clean(b.get("telefono")), clean(b.get("email")),
                             clean(b.get("rubro")),    clean(b.get("pais")),
                             clean(b.get("tags")),     fuente=clean(b.get("fuente","usuario")))
    if not ok2: return err("No se pudo guardar")
    return ok()

@app.route("/api/empresas/<int:eid>", methods=["DELETE"])
def delete_empresa(eid):
    db.eliminar_empresa(eid)
    return ok()

# ── Contactos ─────────────────────────────────────────────────────────────────
@app.route("/api/empresas/<int:eid>/contactos")
def get_contactos(eid):
    return ok(rows_to_list(db.get_contactos_por_empresa(eid)))

@app.route("/api/empresas/<int:eid>/contactos", methods=["POST"])
def post_contacto(eid):
    b = request.json or {}
    if not clean(b.get("nombre")) or not clean(b.get("email")):
        return err("Nombre y email son obligatorios")
    ok2 = db.agregar_contacto(eid, clean(b.get("nombre")), clean(b.get("email")),
                               clean(b.get("telefono")), clean(b.get("pais")))
    if not ok2: return err("No se pudo crear")
    return ok(), 201

@app.route("/api/contactos/<int:cid>", methods=["PUT"])
def put_contacto(cid):
    b = request.json or {}
    ok2 = db.editar_contacto(cid, clean(b.get("nombre")), clean(b.get("email")),
                              clean(b.get("telefono")), clean(b.get("pais")))
    if not ok2: return err("No se pudo guardar")
    return ok()

@app.route("/api/contactos/<int:cid>", methods=["DELETE"])
def delete_contacto(cid):
    db.eliminar_contacto(cid)
    return ok()

# ── Cotizaciones ──────────────────────────────────────────────────────────────
@app.route("/api/empresas/<int:eid>/cotizaciones")
def get_cotizaciones(eid):
    rows   = db.get_cotizaciones_por_empresa(eid)
    result = []
    for r in rows:
        d = dict(r)
        d["fecha_fmt"] = formatear_fecha(d.get("fecha",""))
        result.append(d)
    return ok(result)

@app.route("/api/cotizaciones/<int:cid>")
def get_cotizacion(cid):
    r = db.get_cotizacion_por_id(cid)
    if not r: return err("No encontrada", 404)
    return ok(dict(r))

@app.route("/api/empresas/<int:eid>/cotizaciones", methods=["POST"])
def post_cotizacion(eid):
    b    = request.json or {}
    desc = clean(b.get("descripcion"))
    tipo = clean(b.get("tipo"))
    if not desc: return err("La descripción es obligatoria")
    if tipo: desc = f"[{tipo}] {desc}"
    monto = to_float(b.get("monto"), 0.0)
    ok2 = db.agregar_cotizacion(eid, desc, monto)
    if not ok2: return err("No se pudo crear")
    return ok(), 201

@app.route("/api/cotizaciones/<int:cid>", methods=["PUT"])
def put_cotizacion(cid):
    b = request.json or {}
    monto = to_float(b.get("monto"), 0.0)
    ok2 = db.editar_cotizacion(cid, clean(b.get("descripcion")),
                                monto, b.get("tipo"), b.get("fecha"))
    if not ok2: return err("No se pudo guardar")
    return ok()

@app.route("/api/cotizaciones/<int:cid>", methods=["DELETE"])
def delete_cotizacion(cid):
    db.eliminar_cotizacion(cid)
    return ok()

@app.route("/api/cotizaciones/<int:cid>/resumen", methods=["POST"])
def post_resumen(cid):
    row = db.get_cotizacion_por_id(cid)
    if not row: return err("No encontrada", 404)
    ruta = row.get("ruta_archivo")
    if not ruta or not os.path.isfile(ruta):
        return err("Sin archivo adjunto")
    lanzar_resumen(cid)
    return ok({"mensaje": "Generando resumen"})

@app.route("/api/cotizaciones/<int:cid>/archivo")
def get_cotizacion_archivo(cid):
    """Sirve el archivo adjunto de una cotización para abrirlo en el browser."""
    row = db.get_cotizacion_por_id(cid)
    if not row: return err("No encontrada", 404)
    ruta = row.get("ruta_archivo")
    if not ruta or not os.path.isfile(ruta):
        return err("Archivo no disponible en este servidor", 404)
    return send_file(ruta, as_attachment=False)



# ── Oportunidades / Pipeline ──────────────────────────────────────────────────
@app.route("/api/oportunidades")
def get_oportunidades():
    filtros = {}
    for k in ("etapa","empresa_id"):
        v = request.args.get(k)
        if v: filtros[k] = v
    rows = db.get_oportunidades(filtros)
    # Filter by fase post-query (fase is derived, not stored)
    fase = request.args.get("fase","").strip().lower()
    if fase in ("venta","posventa"):
        rows = [r for r in rows if r.get("fase") == fase]
    return ok(rows)


@app.route("/api/oportunidades", methods=["POST"])
def post_oportunidad():
    b = request.json or {}
    eid = to_int(b.get("empresa_id"), 0)
    titulo = clean(b.get("titulo"))
    if not titulo: return err("El título es obligatorio")
    if not db.obtener_empresa_por_id(eid):
        return err("Empresa no encontrada", 404)
    ok2 = db.crear_oportunidad(
        eid, titulo,
        descripcion          = clean(b.get("descripcion","")),
        etapa                = clean(b.get("etapa","prospecto")),
        monto_estimado       = to_float(b.get("monto_estimado"), None),
        moneda               = clean(b.get("moneda","ARS")),
        fecha_estimada_cierre= clean(b.get("fecha_estimada_cierre","")),
        notas                = clean(b.get("notas","")),
    )
    if not ok2: return err("No se pudo crear")
    row = db.fetchone(
        "SELECT id FROM oportunidades WHERE empresa_id=? AND titulo=? "
        "ORDER BY id DESC LIMIT 1", (eid, titulo))
    return ok({"id": row["id"] if row else None}), 201


@app.route("/api/oportunidades/<int:oid>")
def get_oportunidad(oid):
    row = db.get_oportunidad_por_id(oid)
    if not row: return err("No encontrada", 404)
    return ok(row)


@app.route("/api/oportunidades/<int:oid>", methods=["PUT"])
def put_oportunidad(oid):
    b = request.json or {}
    campos = {}
    for k in ("titulo","descripcion","etapa","moneda",
              "fecha_estimada_cierre","notas"):
        if k in b: campos[k] = clean(b[k])
    if "monto_estimado" in b:
        campos["monto_estimado"] = to_float(b["monto_estimado"], None)
    ok2 = db.editar_oportunidad(oid, **campos)
    if not ok2: return err("No encontrada", 404)
    return ok()


@app.route("/api/oportunidades/<int:oid>", methods=["DELETE"])
def delete_oportunidad(oid):
    ok2 = db.eliminar_oportunidad(oid)
    if not ok2: return err("No encontrada", 404)
    return ok()


@app.route("/api/oportunidades/<int:oid>/etapa", methods=["PUT"])
def put_oportunidad_etapa(oid):
    b = request.json or {}
    etapa = clean(b.get("etapa",""))
    if not db._normalizar_etapa(etapa):
        return err("Etapa inválida", 400)
    ok2 = db.cambiar_etapa_oportunidad(oid, etapa)
    if not ok2: return err("No encontrada", 404)
    return ok()


@app.route("/api/empresas/<int:eid>/oportunidades")
def get_oportunidades_empresa(eid):
    if not db.obtener_empresa_por_id(eid):
        return err("Empresa no encontrada", 404)
    return ok(db.get_oportunidades_empresa(eid))


# ── Actividades ───────────────────────────────────────────────────────────────
_ACT_TIPOS = {"nota","llamada","email","reunion"}

def _clean_tipo_act(v):
    v = clean(v or "").lower()
    return v if v in _ACT_TIPOS else "nota"


@app.route("/api/empresas/<int:eid>/actividades")
def get_actividades(eid):
    if not db.obtener_empresa_por_id(eid):
        return err("Empresa no encontrada", 404)
    limit  = to_int(request.args.get("limit",  200), 200, lo=1, hi=500)
    offset = to_int(request.args.get("offset",   0),   0, lo=0)
    return ok(rows_to_list(db.get_actividades_empresa(eid,
                                                      limit=limit,
                                                      offset=offset)))


@app.route("/api/empresas/<int:eid>/actividades", methods=["POST"])
def post_actividad(eid):
    if not db.obtener_empresa_por_id(eid):
        return err("Empresa no encontrada", 404)
    b     = request.json or {}
    texto = clean(b.get("texto"))
    if not texto: return err("El texto es obligatorio")
    usuario = (clean(b.get("usuario","")) or "usuario")[:80]
    ok2 = db.agregar_actividad(eid, _clean_tipo_act(b.get("tipo")),
                               texto, usuario)
    if not ok2: return err("No se pudo guardar")
    return ok(), 201


@app.route("/api/actividades/<int:aid>", methods=["PUT"])
def put_actividad(aid):
    b     = request.json or {}
    texto = clean(b.get("texto"))
    if not texto: return err("El texto es obligatorio")
    ok2 = db.editar_actividad(aid, _clean_tipo_act(b.get("tipo")), texto)
    if not ok2: return err("Actividad no encontrada", 404)
    return ok()


@app.route("/api/actividades/<int:aid>", methods=["DELETE"])
def delete_actividad(aid):
    ok2 = db.eliminar_actividad(aid)
    if not ok2: return err("Actividad no encontrada", 404)
    return ok()


@app.route("/api/actividades/recientes")
def get_actividades_recientes():
    dias  = to_int(request.args.get("dias",  7),  7, lo=1, hi=365)
    limit = to_int(request.args.get("limit", 50), 50, lo=1, hi=200)
    return ok(rows_to_list(db.get_actividades_recientes(dias, limit)))

# ── Historial ─────────────────────────────────────────────────────────────────
@app.route("/api/empresas/<int:eid>/historial")
def get_historial(eid):
    return ok(rows_to_list(db.get_historial_empresa(eid)))

@app.route("/api/historial/<int:hid>", methods=["DELETE"])
def delete_historial(hid):
    db.eliminar_cambio(hid)
    return ok()

@app.route("/api/historial/<int:hid>/revertir", methods=["POST"])
def revertir_historial(hid):
    cambio  = db.fetchone("SELECT * FROM cambios WHERE id=?", (hid,))
    if not cambio: return err("Cambio no encontrado", 404)
    campo    = cambio["campo"]
    anterior = cambio["valor_anterior"]
    eid      = cambio["empresa_id"]
    empresa  = db.obtener_empresa_por_id(eid)
    if not empresa: return err("Empresa no encontrada", 404)
    campos = {k: empresa.get(k,"") for k in
              ("nombre","direccion","telefono","email","rubro","pais")}
    if campo not in campos:
        return err(f"Campo '{campo}' no reversible")
    campos[campo] = anterior
    tags = ", ".join(db.get_tags_de_empresa(eid))
    ok2  = db.editar_empresa(eid, campos["nombre"], campos["direccion"],
                              campos["telefono"], campos["email"],
                              campos["rubro"],    campos["pais"],
                              tags, fuente="usuario (reversión)")
    if not ok2: return err("No se pudo revertir")
    return ok()

# ── Duplicados ────────────────────────────────────────────────────────────────
# ── Búsqueda global ───────────────────────────────────────────────────────────
@app.route("/api/buscar")
def buscar():
    q         = request.args.get("q","")
    empresa   = request.args.get("empresa","")
    tipo_cot  = request.args.get("tipo","")
    monto_min = to_float(request.args.get("monto_min"))
    monto_max = to_float(request.args.get("monto_max"))
    periodo   = request.args.get("periodo")
    page      = to_int(request.args.get("page"), 0, lo=0)
    page_size = to_int(request.args.get("page_size"), 200, lo=1, hi=1000)

    where, params = ["1=1"], []
    if q:
        where.append("(c.descripcion LIKE ? OR e.nombre LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if empresa:
        where.append("e.nombre LIKE ?"); params.append(f"%{empresa}%")
    if tipo_cot:
        where.append("(c.tipo = ? OR c.descripcion LIKE ?)")
        params += [tipo_cot, f"[{tipo_cot}]%"]
    if monto_min is not None:
        where.append("c.monto >= ?"); params.append(monto_min)
    if monto_max is not None:
        where.append("c.monto <= ?"); params.append(monto_max)
    if periodo:
        dias_map = {"7":7,"30":30,"90":90,"365":365}
        dias = dias_map.get(str(periodo))
        if dias:
            desde = (datetime.now()-timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
            where.append("c.fecha >= ?"); params.append(desde)

    sql = (f"SELECT c.id cid, c.empresa_id, c.fecha, c.descripcion, c.monto, "
           f"c.tipo, c.moneda, c.resumen, c.estado_ia, e.nombre empresa_nombre "
           f"FROM cotizaciones c JOIN empresas e ON c.empresa_id=e.id "
           f"WHERE {' AND '.join(where)} ORDER BY c.fecha DESC")

    all_rows    = rows_to_list(db.fetchall(sql, tuple(params)))
    total       = len(all_rows)
    total_monto = sum(float(r.get("monto") or 0) for r in all_rows)
    page_rows   = all_rows[page*page_size:(page+1)*page_size]
    for r in page_rows:
        r["fecha_fmt"] = formatear_fecha(r.get("fecha",""))
    pages = max(1, (total + page_size - 1) // page_size)
    return ok(page_rows, total=total, total_monto=total_monto, pages=pages)

# ── Meta ──────────────────────────────────────────────────────────────────────
@app.route("/api/meta/paises")
def get_paises():
    rows = db.fetchall(
        "SELECT DISTINCT pais FROM empresas WHERE pais IS NOT NULL AND pais!='' ORDER BY pais")
    return ok([r["pais"] for r in rows])

@app.route("/api/meta/rubros")
def get_rubros():
    rows = db.fetchall(
        "SELECT DISTINCT rubro FROM empresas WHERE rubro IS NOT NULL AND rubro!='' ORDER BY rubro")
    return ok([r["rubro"] for r in rows])

@app.route("/api/meta/tags")
def get_tags():
    rows = db.fetchall("SELECT tag FROM tags ORDER BY tag COLLATE NOCASE")
    return ok([r["tag"] for r in rows])

@app.route("/api/meta/tipos_cotizacion")
def get_tipos_cotizacion():
    rows = db.fetchall(
        "SELECT DISTINCT tipo FROM cotizaciones WHERE tipo IS NOT NULL AND tipo!='' ORDER BY tipo")
    static_tipos = ["Equipos","Comercial","Servicio","Mantenimiento","Otro"]
    dynamic = [r["tipo"] for r in rows]
    combined = list(dict.fromkeys(static_tipos + dynamic))
    return ok(combined)

# ── Importar CSV ──────────────────────────────────────────────────────────────
@app.route("/api/importar/csv", methods=["POST"])
def importar_csv():
    f = request.files.get("file")
    if not f: return err("Sin archivo")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        f.save(tmp.name); path = tmp.name
    try:
        from csv_utils import _open_csv, _find_col, EMAIL_COLS, FNAME_COLS, LNAME_COLS, PUBLIC_DOMAINS, TLD_TO_COUNTRY
        import csv as _csv
        fh, enc, sep = _open_csv(path)
        with fh:
            rows = list(_csv.DictReader(fh, delimiter=sep))
        if not rows: return err("CSV vacío")
        keys       = rows[0].keys()
        email_col  = _find_col(keys, EMAIL_COLS)
        fname_col  = _find_col(keys, FNAME_COLS)
        lname_col  = _find_col(keys, LNAME_COLS)
        if not email_col:
            return err(f"Sin columna email. Columnas: {', '.join(keys)}")

        ok_count = skip = err_count = 0
        crear_backup_seguro("antes_importar_csv")
        for row in rows:
            email = clean(row.get(email_col))
            if not email or "@" not in email: continue
            parts = email.split("@")
            if len(parts) != 2: continue
            domain = parts[1].lower()
            if domain in PUBLIC_DOMAINS: skip += 1; continue
            nombre = ""
            if fname_col and lname_col:
                nombre = f"{clean(row.get(fname_col))} {clean(row.get(lname_col))}".strip()
            if not nombre: nombre = parts[0]
            empresa = domain.split(".")[0].capitalize()
            pais    = TLD_TO_COUNTRY.get(domain.split(".")[-1], "Desconocido")
            emp = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (empresa,))
            if emp:
                eid = emp["id"]
            else:
                db.agregar_empresa(empresa,"","","","",pais,"")
                r2 = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (empresa,))
                eid = r2["id"] if r2 else None
            if not eid: err_count += 1; continue
            if db.fetchone("SELECT id FROM contactos WHERE email=? AND empresa_id=?",
                           (email, eid)):
                skip += 1; continue
            if db.agregar_contacto(eid, nombre, email, "", pais):
                ok_count += 1
            else:
                err_count += 1
    finally:
        os.unlink(path)
    return ok({"importados": ok_count, "omitidos": skip, "errores": err_count})

# ── Importar carpeta ──────────────────────────────────────────────────────────
@app.route("/api/importar/carpeta", methods=["POST"])
def importar_carpeta():
    b     = request.json or {}
    items = b.get("items", [])
    if not items: return err("Sin archivos")

    existing_rutas  = {r["ruta_archivo"] for r in
                       db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                       if r.get("ruta_archivo")}
    existing_hashes = {r["archivo_hash"] for r in
                       db.fetchall("SELECT archivo_hash FROM cotizaciones")
                       if r.get("archivo_hash")}
    crear_backup_seguro("antes_importar_carpeta")

    # Cache empresa names for auto-create
    emp_by_nombre   = {e["nombre"].lower(): e["id"]
                       for e in db.fetchall("SELECT id, nombre FROM empresas")}
    ok_count = skip_count = err_count = empresas_creadas = 0

    for item in items:
        eid   = to_int(item.get("empresa_id"), 0)  # 0 = not provided, triggers auto-create
        fpath = clean(item.get("file_path"))
        fname = clean(item.get("file_name"))
        if not fpath: err_count += 1; continue
        if fpath in existing_rutas: skip_count += 1; continue

        # Auto-create empresa if no empresa_id provided
        if not eid:
            # Try name sources in order of reliability
            for src in (
                clean(item.get("empresa_nombre_detectada")),
                clean(item.get("sugerida")),
                clean(item.get("carpeta")),
                os.path.splitext(fname)[0] if fname else None,
            ):
                if src and len(src.strip()) >= 2:
                    nombre = src.strip()
                    eid = emp_by_nombre.get(nombre.lower())
                    if not eid:
                        db.agregar_empresa(nombre, "", "", "", "", "", "")
                        row = db.fetchone(
                            "SELECT id FROM empresas WHERE nombre=?", (nombre,))
                        if row:
                            eid = row["id"]
                            emp_by_nombre[nombre.lower()] = eid
                            empresas_creadas += 1
                    break

        if not eid: err_count += 1; continue

        # Reuse hash from scan if provided — never recalculate unnecessarily
        ahash = clean(item.get("archivo_hash")) or None
        if ahash and ahash in existing_hashes:
            skip_count += 1; continue
        # Only compute hash if not provided by scan
        if not ahash:
            try:
                ahash = file_sha256(fpath)
            except Exception:
                ahash = None
        if ahash and ahash in existing_hashes: skip_count += 1; continue
        try:
            mtime = datetime.fromtimestamp(
                os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")
        except:
            mtime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ok2 = db.agregar_cotizacion_con_ruta(eid, fname, 0.0, mtime, fpath,
                                              archivo_hash=ahash)
        if ok2:
            ok_count += 1
            existing_rutas.add(fpath)
            if ahash: existing_hashes.add(ahash)
            row = db.fetchone(
                "SELECT id FROM cotizaciones WHERE empresa_id=? AND ruta_archivo=? "
                "ORDER BY id DESC LIMIT 1", (eid, fpath))
            if row:
                lanzar_resumen(row["id"])
        else:
            err_count += 1
    return ok({"importados": ok_count, "omitidos": skip_count, "errores": err_count, "empresas_creadas": empresas_creadas})

# ── Escanear carpeta ──────────────────────────────────────────────────────────
@app.route("/api/escanear", methods=["POST"])
def escanear_carpeta():
    """
    Escanea carpeta con paginación. NO calcula hash por defecto (muy lento).
    Deduplicación por ruta_archivo (O(1)). Hash opcional con {hash:true}.
    Soporta offset/limit — solo hace fuzzy matching sobre la página actual.
    """
    b      = request.json or {}
    path   = clean(b.get("path"))
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")

    offset     = max(0, to_int(b.get("offset", 0),  0))
    limit      = max(1, min(to_int(b.get("limit", 200), 200), 500))
    calc_hash  = bool(b.get("hash", False))  # OFF by default
    thresh     = max(0, min(to_int(b.get("thresh", 60), 60), 100))

    EXTS = {".pdf",".docx",".xlsx",".doc",".xls",".pptx",".txt"}

    try:
        from rapidfuzz import process as fz_process
    except ImportError:
        from difflib import SequenceMatcher
        class fz_process:
            @staticmethod
            def extract(q, choices, limit=1):
                s = [(ch, int(SequenceMatcher(None, str(q or "").lower(),
                      str(ch or "").lower()).ratio()*100)) for ch in choices]
                return sorted(s, key=lambda x:-x[1])[:limit]

    empresas     = db.fetchall("SELECT id, nombre FROM empresas ORDER BY nombre")
    comp_names   = [e["nombre"] for e in empresas]
    comp_by_name = {e["nombre"]: e["id"] for e in empresas}

    # Dedup by ruta only — fast, no disk reads
    existing_rutas = {r["ruta_archivo"] for r in
                      db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                      if r.get("ruta_archivo")}

    # Only load existing hashes if caller requested hash dedup
    existing_hashes = set()
    if calc_hash:
        existing_hashes = {r["archivo_hash"] for r in
                           db.fetchall("SELECT archivo_hash FROM cotizaciones")
                           if r.get("archivo_hash")}

    # Phase 1: walk with early exit — collect offset+limit+1 items max
    # The extra "+1" tells us has_more without walking the whole tree
    need       = offset + limit + 1
    all_paths  = []   # (fpath, fname, folder_chain)
    total_approx = 0

    for fpath, fname, folder_chain in walk_files(path, EXTS):
        if fpath in existing_rutas:
            continue
        total_approx += 1
        all_paths.append((fpath, fname, folder_chain))
        # Early exit after collecting enough for pagination
        # (only on first page — deeper pages need full walk for correct offset)
        if offset == 0 and len(all_paths) >= need:
            break

    has_more   = len(all_paths) > offset + limit
    page_paths = all_paths[offset: offset + limit]

    # Phase 3: fuzzy match + optional hash — only for current page
    results = []
    for fpath, fname, folder_chain in page_paths:
        rel     = os.path.relpath(fpath, path)
        carpeta = rel.split(os.sep)[0] if os.sep in rel else ""
        src     = carpeta or os.path.splitext(fname)[0]
        # Detect empresa name (same logic as importar_masivo)
        empresa_nombre_detectada = _get_client_name(folder_chain,
                                                     os.path.splitext(fname)[0])

        sugerida, sim, eid = "", 0, None
        if comp_names:
            hits = fz_process.extract(src, comp_names, limit=1)
            if hits:
                sugerida, sim = hits[0][0], int(round(hits[0][1]))
                eid = comp_by_name.get(sugerida) if sim >= thresh else None

        # Hash only if explicitly requested
        ahash = None
        if calc_hash:
            try:
                ahash = file_sha256(fpath)
                if ahash in existing_hashes:
                    continue  # skip duplicate by hash
            except Exception:
                ahash = None

        results.append({
            "file_path":               fpath,
            "file_name":               fname,
            "carpeta":                 carpeta,
            "sugerida":                sugerida,
            "sim":                     sim,
            "eid":                     eid,
            "archivo_hash":            ahash,
            "empresa_nombre_detectada":empresa_nombre_detectada,
        })

    return ok(results,
              total          = total_approx,
              total_aproximado = total_approx,
              total_exacto   = False,
              offset         = offset,
              limit          = limit,
              has_more       = has_more,
              pages          = None)


@app.route("/api/importar/empresas-desde-carpeta", methods=["POST"])
def importar_empresas_desde_carpeta():
    """
    Detecta nombres de empresa desde una carpeta y las crea si no existen.
    NO importa cotizaciones. NO calcula hashes.
    Útil como paso previo al escaneo para maximizar el matching.
    """
    b    = request.json or {}
    path = clean(b.get("path") or b.get("ruta_carpeta",""))
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")

    EXTS = {".pdf",".docx",".xlsx",".doc",".xls",".pptx",".txt"}

    # Load existing empresas
    emp_by_nombre = {e["nombre"].lower(): e["id"]
                     for e in db.fetchall("SELECT id, nombre FROM empresas")}

    detectadas = set()
    creadas    = []
    existentes = 0
    errores    = 0

    for fpath, fname, folder_chain in walk_files(path, EXTS):
        stem   = os.path.splitext(fname)[0]
        nombre = _get_client_name(folder_chain, stem)
        if not nombre or len(nombre.strip()) < 2:
            errores += 1
            continue
        nombre = nombre.strip()
        detectadas.add(nombre)

    for nombre in sorted(detectadas):
        if nombre.lower() in emp_by_nombre:
            existentes += 1
        else:
            ok2 = db.agregar_empresa(nombre, "", "", "", "", "", "")
            if ok2:
                creadas.append(nombre)
                emp_by_nombre[nombre.lower()] = True
            else:
                errores += 1

    return ok({
        "detectadas":   len(detectadas),
        "creadas":      len(creadas),
        "existentes":   existentes,
        "errores":      errores,
        "sample_creadas": creadas[:20],
    })


# ── Aliases de empresas ───────────────────────────────────────────────────────
@app.route("/api/empresas/<int:empresa_id>/aliases", methods=["GET"])
def api_get_aliases_empresa(empresa_id):
    if not db.obtener_empresa_por_id(empresa_id):
        return err("Empresa no encontrada", 404)
    try:
        data = db.get_aliases_empresa(empresa_id)
        return jsonify({"ok": True, "data": data}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/empresas/<int:empresa_id>/aliases", methods=["POST"])
def api_post_alias_empresa(empresa_id):
    if not db.obtener_empresa_por_id(empresa_id):
        return err("Empresa no encontrada", 404)
    body = request.json or {}
    alias_raw = (body.get("alias") or "").strip()
    origen = body.get("origen") or "manual"
    try:
        confianza = float(body.get("confianza", 1.0))
    except (TypeError, ValueError):
        return err("confianza debe ser numérica", 400)
    try:
        resultado = db.agregar_alias_empresa(
            empresa_id=empresa_id,
            alias=alias_raw,
            origen=origen,
            confianza=confianza,
        )
        return jsonify({"ok": True, "data": resultado}), 201
    except AliasValidationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except EmpresaNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except AliasConflictError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception:
        return jsonify({"ok": False, "error": "Error interno inesperado."}), 500


@app.route("/api/empresas/<int:empresa_id>/aliases/<int:alias_id>", methods=["DELETE"])
def api_delete_alias_empresa(empresa_id, alias_id):
    try:
        eliminado = db.eliminar_alias_empresa(
            alias_id=alias_id,
            empresa_id=empresa_id,
        )
        if not eliminado:
            return jsonify({"ok": False, "error": "Alias no encontrado."}), 404
        return jsonify({"ok": True, "data": {"eliminado": True}}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Limpieza y unificación post-importación ───────────────────────────────────
@app.route("/api/limpiar/preview")
def limpiar_preview():
    """
    Muestra grupos de empresas con nombres similares para unificación.
    Usa el mismo fuzzy matching de get_similar_empresas.
    """
    umbral = max(50, min(to_int(request.args.get("umbral", 85), 85), 100))
    pares  = db.get_similar_empresas(umbral)

    # Group pairs into clusters using union-find
    parent = {}
    def find(x):
        if x not in parent: parent[x] = x
        if parent[x] != x: parent[x] = find(parent[x])
        return parent[x]
    def union(a, b):
        pa, pb = find(a), find(b)
        if pa != pb: parent[pa] = pb

    for p in pares:
        union(p["id1"], p["id2"])

    clusters = {}
    for p in pares:
        root = find(p["id1"])
        if root not in clusters:
            clusters[root] = set()
        clusters[root].add(p["id1"])
        clusters[root].add(p["id2"])

    empresas_by_id = {e["id"]: e for e in db.fetchall(
        "SELECT id, nombre, pais, rubro FROM empresas")}

    groups = []
    for root, ids in clusters.items():
        members = []
        for eid in sorted(ids):
            emp = empresas_by_id.get(eid)
            if not emp: continue
            ncot = db.count_by_empresa("cotizaciones", eid)
            ncon = db.count_by_empresa("contactos", eid)
            members.append({
                "id":     eid,
                "nombre": emp["nombre"],
                "pais":   emp.get("pais",""),
                "rubro":  emp.get("rubro",""),
                "ncot":   ncot,
                "ncon":   ncon,
            })
        if len(members) >= 2:
            # Suggest canonical: most data wins (cotizaciones + contactos)
            canonical = max(members, key=lambda m: m["ncot"] + m["ncon"])
            groups.append({
                "id":        root,
                "members":   members,
                "canonical": canonical["id"],
            })

    return ok(groups, total=len(groups), umbral=umbral)


@app.route("/api/limpiar/unificar", methods=["POST"])
def limpiar_unificar():
    """
    Fusiona una lista de empresas hacia un nombre canónico.
    body: { destino_id: int, origen_ids: [int, ...] }
    """
    b         = request.json or {}
    destino   = to_int(b.get("destino_id"), 0)
    origenes  = [to_int(x, 0) for x in (b.get("origen_ids") or []) if to_int(x,0)]

    if not destino: return err("destino_id es obligatorio")
    if not origenes: return err("origen_ids es obligatorio")
    if destino in origenes:
        return err("No se puede unificar una empresa consigo misma", 400)
    if not db.obtener_empresa_por_id(destino):
        return err("Empresa destino no encontrada", 404)

    ok_count = err_count = 0
    aliases_reporte_total = {
        "aliases_creados": 0,
        "aliases_migrados": 0,
        "aliases_existentes_destino": 0,
        "aliases_conflictos": 0,
    }
    for origen in origenes:
        if origen == destino: continue

        # Preservar aliases antes de unificar
        reporte_aliases = {
            "aliases_creados": 0,
            "aliases_migrados": 0,
            "aliases_existentes_destino": 0,
            "aliases_conflictos": 0,
            "aliases_error": None,
        }
        try:
            empresa_origen = db.obtener_empresa_por_id(origen)
            nombre_origen = (empresa_origen or {}).get("nombre")
            if nombre_origen:
                try:
                    res_alias = db.agregar_alias_empresa(
                        empresa_id=destino,
                        alias=nombre_origen,
                        origen="merge",
                        confianza=1.0,
                    )
                    if res_alias.get("action") == "created":
                        reporte_aliases["aliases_creados"] = 1
                except (AliasValidationError, EmpresaNotFoundError, AliasConflictError) as e:
                    reporte_aliases["aliases_error"] = str(e)

            res_mig = db.migrar_aliases_empresa(origen_id=origen, destino_id=destino)
            reporte_aliases["aliases_migrados"]           = res_mig.get("migrados", 0)
            reporte_aliases["aliases_existentes_destino"] = res_mig.get("existentes_destino", 0)
            reporte_aliases["aliases_conflictos"]         = res_mig.get("conflictos", 0)
        except Exception as e:
            reporte_aliases["aliases_error"] = str(e)

        for k in ("aliases_creados", "aliases_migrados", "aliases_existentes_destino", "aliases_conflictos"):
            aliases_reporte_total[k] += reporte_aliases[k]

        ok2 = db.unificar_empresas(origen, destino)
        if ok2: ok_count += 1
        else:   err_count += 1

    return ok({"fusionadas": ok_count, "errores": err_count, **aliases_reporte_total})


@app.route("/api/limpiar/renombrar", methods=["POST"])
def limpiar_renombrar():
    """
    Renombra una empresa sin fusionar (útil para limpiar variantes de nombre).
    body: { empresa_id: int, nombre: str }
    """
    b     = request.json or {}
    eid   = to_int(b.get("empresa_id"), 0)
    nuevo = clean(b.get("nombre",""))
    if not eid or not nuevo: return err("empresa_id y nombre son obligatorios")
    emp = db.obtener_empresa_por_id(eid)
    if not emp: return err("Empresa no encontrada", 404)
    ok2 = db.editar_empresa(
        eid, nuevo,
        emp.get("direccion",""), emp.get("telefono",""),
        emp.get("email",""), emp.get("rubro",""),
        emp.get("pais",""), ", ".join(db.get_tags_de_empresa(eid)),
        fuente="limpieza")
    if not ok2: return err("No se pudo renombrar")
    return ok()


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/api/dashboard")
def get_dashboard():
    meses = max(1, min(to_int(request.args.get("meses", 12), 12), 60))
    pais  = clean(request.args.get("pais",""))

    from datetime import timedelta
    desde = (datetime.now() - timedelta(days=meses*30)).strftime("%Y-%m-%d")

    pais_filter = "AND e.pais=?" if pais else ""
    pais_params = (pais,) if pais else ()

    # Cotizaciones por mes (últimos N meses)
    cot_mes = db.fetchall(f"""
        SELECT strftime('%Y-%m', c.fecha) mes,
               COUNT(*) cantidad,
               COALESCE(SUM(c.monto),0) monto_total
        FROM cotizaciones c
        JOIN empresas e ON c.empresa_id=e.id
        WHERE c.fecha >= ? {pais_filter}
        GROUP BY mes ORDER BY mes DESC LIMIT ?
    """, (desde,) + pais_params + (meses,))

    # Empresas por país
    emp_pais = db.fetchall(f"""
        SELECT COALESCE(NULLIF(pais,''),'Sin país') pais, COUNT(*) cantidad
        FROM empresas GROUP BY pais ORDER BY cantidad DESC LIMIT 15
    """)

    # Pipeline por etapa
    pipeline = db.fetchall(f"""
        SELECT o.etapa, COUNT(*) cantidad,
               COALESCE(SUM(o.monto_estimado),0) monto_total
        FROM oportunidades o
        JOIN empresas e ON o.empresa_id=e.id
        WHERE 1=1 {pais_filter}
        GROUP BY o.etapa ORDER BY cantidad DESC
    """, pais_params)

    # Top 10 empresas por monto cotizado
    top_emp = db.fetchall(f"""
        SELECT e.nombre, COUNT(c.id) ncot,
               COALESCE(SUM(c.monto),0) monto_total
        FROM empresas e
        LEFT JOIN cotizaciones c ON c.empresa_id=e.id
        WHERE 1=1 {pais_filter}
        GROUP BY e.id ORDER BY monto_total DESC LIMIT 10
    """, pais_params)

    # Actividad por tipo (últimos 30 días)
    act_tipo = db.fetchall("""
        SELECT COALESCE(tipo,'nota') tipo, COUNT(*) cantidad
        FROM actividades
        WHERE fecha >= ?
        GROUP BY tipo ORDER BY cantidad DESC
    """, ((datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d"),))

    # Resumen general
    oport_rows = db.fetchall("""
        SELECT etapa, COUNT(*) n FROM oportunidades GROUP BY etapa
    """)
    etapas_abiertas = {"prospecto","contactado","a_visitar","a_cotizar",
                       "cotizado","en_negociacion","en_proceso","entregada"}
    abiertas = sum(r["n"] for r in oport_rows
                   if r["etapa"] in etapas_abiertas)
    ganadas  = sum(r["n"] for r in oport_rows if r["etapa"] == "ganado")

    monto_total = db.fetchone(
        f"SELECT COALESCE(SUM(c.monto),0) t FROM cotizaciones c "
        f"JOIN empresas e ON c.empresa_id=e.id WHERE 1=1 {pais_filter}",
        pais_params)

    resumen = {
        "empresas":                 db.count("empresas"),
        "contactos":                db.count("contactos"),
        "cotizaciones":             db.count("cotizaciones"),
        "monto_total_cotizaciones": float(monto_total["t"] if monto_total else 0),
        "oportunidades_abiertas":   abiertas,
        "oportunidades_ganadas":    ganadas,
    }

    return ok({
        "cotizaciones_por_mes": rows_to_list(cot_mes),
        "empresas_por_pais":    rows_to_list(emp_pais),
        "pipeline_por_etapa":   rows_to_list(pipeline),
        "top_empresas":         rows_to_list(top_emp),
        "actividad_por_tipo":   rows_to_list(act_tipo),
        "resumen":              resumen,
    })


# ── Importador por subcarpetas ────────────────────────────────────────────────
@app.route("/api/importar/subcarpetas/listar", methods=["POST"])
def listar_subcarpetas():
    """
    Lista las subcarpetas de primer nivel con conteo de PDFs nuevos.
    No procesa archivos, solo cuenta.
    """
    b    = request.json or {}
    path = clean(b.get("path",""))
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")

    existing = {r["ruta_archivo"] for r in
                db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                if r.get("ruta_archivo")}

    EXTS = {".pdf",".docx",".xlsx",".doc",".xls",".pptx",".txt"}
    result = []

    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except PermissionError as exc:
        return err(str(exc))

    for entry in entries:
        if not entry.is_dir():
            continue
        total = ya = 0
        for root, _, files in os.walk(entry.path):
            for fname in files:
                if os.path.splitext(fname)[1].lower() in EXTS:
                    total += 1
                    if os.path.join(root, fname) in existing:
                        ya += 1
        if total > 0:
            result.append({
                "nombre":        entry.name,
                "path":          entry.path,
                "total_pdf":     total,
                "ya_importados": ya,
                "a_importar":    total - ya,
            })

    # Also count loose files in root
    loose_total = loose_ya = 0
    for fname in os.listdir(path):
        fpath = os.path.join(path, fname)
        if os.path.isfile(fpath) and                 os.path.splitext(fname)[1].lower() in EXTS:
            loose_total += 1
            if fpath in existing:
                loose_ya += 1
    if loose_total > 0:
        result.insert(0, {
            "nombre":        "(archivos sueltos)",
            "path":          path,
            "total_pdf":     loose_total,
            "ya_importados": loose_ya,
            "a_importar":    loose_total - loose_ya,
        })

    return ok(result, total_subcarpetas=len(result))


@app.route("/api/importar/subcarpetas/importar", methods=["POST"])
def importar_subcarpetas():
    """
    Importa PDFs de las subcarpetas seleccionadas de a 'lote' archivos.
    Detecta empresa y país. Crea empresas si no existen.
    Archivos con nombre raro → sin_empresa:True, no se saltean.
    """
    b           = request.json or {}
    path          = clean(b.get("path",""))
    subcarpetas   = b.get("subcarpetas", [])  # list of paths
    lote          = max(1, min(to_int(b.get("lote", 500), 500), 500))
    offset        = max(0, to_int(b.get("offset", 0), 0))
    pais_override = clean(b.get("pais_override",""))  # país fijo para toda la importación
    if pais_override:
        norm_key = _norm(pais_override)
        if norm_key not in PAISES_CONOCIDOS_NORM:
            return err(f"País no reconocido: {pais_override!r}. "
                       f"Usá uno de: {', '.join(sorted(set(PAISES_CONOCIDOS_NORM.values())))}")
        pais_override = PAISES_CONOCIDOS_NORM[norm_key]  # forma canónica

    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")
    if not subcarpetas:
        return err("Seleccioná al menos una subcarpeta")

    EXTS = {".pdf"}  # Solo PDF en importador por subcarpetas

    existing_rutas = {r["ruta_archivo"] for r in
                      db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                      if r.get("ruta_archivo")}
    emp_by_nombre  = {e["nombre"].lower(): e["id"]
                      for e in db.fetchall("SELECT id, nombre FROM empresas")}

    from utils.normalizacion import normalizar_alias_empresa as _norm_alias
    alias_cache = {
        row["alias_norm"]: row["empresa_id"]
        for row in db.fetchall("SELECT alias_norm, empresa_id FROM empresa_aliases")
    }

    crear_backup_seguro("antes_importar_subcarpetas")

    # Collect all files from selected subcarpetas
    all_files = []
    for sub_path in subcarpetas:
        if not os.path.isdir(sub_path):
            continue
        for fpath, fname, folder_chain in walk_files(
                sub_path, EXTS, relative_to=path, sort_files=True):
            if fpath in existing_rutas:
                continue
            all_files.append((fpath, fname, folder_chain))

    total_pendientes = len(all_files)
    lote_files       = all_files[offset: offset + lote]

    ok_count = err_count = empresas_creadas = sin_empresa = 0
    sin_empresa_items = []

    for fpath, fname, folder_chain in lote_files:
        stem   = os.path.splitext(fname)[0]
        nombre = _get_client_name(folder_chain, stem)

        import re as _re
        nombre_valido = (nombre and len(nombre.strip()) >= 2
                         and not _re.match(r'^\d+$', nombre.strip()))

        pais = pais_override or _detect_pais(folder_chain)

        if not nombre_valido:
            # No crear empresa fantasma — reportar y saltear
            sin_empresa += 1
            sin_empresa_items.append({
                "file_path": fpath,
                "file_name": fname,
                "carpeta":   folder_chain[-1] if folder_chain else "",
                "pais_detectado": pais,
            })
            continue

        nombre = nombre.strip()

        # 1. Resolver por alias exacto normalizado (cache, sin N+1)
        norm = _norm_alias(nombre)
        eid = alias_cache.get(norm)
        if not eid:
            # 2. Flujo viejo: nombre exacto case-insensitive
            eid = emp_by_nombre.get(nombre.lower())
        if not eid:
            ok2 = db.agregar_empresa(nombre, "", "", "", "", pais, "")
            if ok2:
                row = db.fetchone(
                    "SELECT id FROM empresas WHERE nombre=?", (nombre,))
                if row:
                    eid = row["id"]
                    emp_by_nombre[nombre.lower()] = eid
                    empresas_creadas += 1

        if not eid:
            err_count += 1
            continue

        try:
            mtime = datetime.fromtimestamp(
                os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        ok2 = db.agregar_cotizacion_con_ruta(eid, fname, 0.0, mtime, fpath)
        if ok2:
            ok_count += 1
            existing_rutas.add(fpath)
            row = db.fetchone(
                "SELECT id FROM cotizaciones WHERE empresa_id=? "
                "AND ruta_archivo=? ORDER BY id DESC LIMIT 1",
                (eid, fpath))
            if row:
                lanzar_resumen(row["id"])
        else:
            err_count += 1

    completado = (offset + lote) >= total_pendientes

    return ok({
        "importados":         ok_count,
        "empresas_creadas":   empresas_creadas,
        "sin_empresa":        sin_empresa,
        "sin_empresa_items":  sin_empresa_items[:50],  # max 50 en respuesta
        "errores":            err_count,
        "offset":             offset,
        "lote":               lote,
        "total_pendientes":   total_pendientes,
        "completado":         completado,
        "siguiente_offset":   offset + lote if not completado else None,
    })

# ── Exportar ──────────────────────────────────────────────────────────────────
@app.route("/api/exportar")
def exportar():
    fmt   = request.args.get("fmt","xlsx")
    if fmt not in ("xlsx","csv"): return err("Formato inválido")
    tipo  = request.args.get("tipo","empresas")   # empresas | contactos | cotizaciones

    if tipo == "contactos":
        datos = rows_to_list(db.fetchall(
            "SELECT e.nombre empresa, c.nombre contacto, c.email, c.telefono, c.pais "
            "FROM contactos c JOIN empresas e ON c.empresa_id=e.id ORDER BY e.nombre"))
    elif tipo == "cotizaciones":
        datos = rows_to_list(db.fetchall(
            "SELECT e.nombre empresa, c.fecha, c.descripcion, c.monto, c.moneda, "
            "c.tipo, c.resumen, c.proveedor_ia, c.estado_ia "
            "FROM cotizaciones c JOIN empresas e ON c.empresa_id=e.id ORDER BY e.nombre, c.fecha"))
    else:
        datos = db.get_all_empresas_with_cotizaciones()

    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
        path = tmp.name
    exp = Exportador()
    ok2 = exp.a_excel(datos, path) if fmt=="xlsx" else exp.a_csv(datos, path)
    if not ok2: return err("Error al exportar")
    with open(path,"rb") as f: data = f.read()
    os.unlink(path)
    mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if fmt=="xlsx" else "text/csv")
    fname = f"crm_{tipo}.{fmt}"
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ── Importador masivo inteligente ─────────────────────────────────────────────
from importer import (
    GENERIC_FOLDERS, IMPORT_EXTS, PAISES_CONOCIDOS_NORM,
    normalizar_basico as _norm,
    detect_pais as _detect_pais,
    extract_client_from_stem as _extract_client_from_stem,
    get_client_name as _get_client_name,
)
from importer.scanner import walk_files


@app.route("/api/importar/masivo", methods=["POST"])
def importar_masivo():
    """
    Importación masiva inteligente:
    - Solo PDF (ignora ODT, imágenes, etc.)
    - Detecta nombre de empresa desde carpeta o nombre de archivo
    - Crea empresas automáticamente si no existen
    - No reimporta archivos ya importados (por ruta)
    - Devuelve resumen: empresas creadas, cotizaciones importadas
    """
    b    = request.json or {}
    path = b.get("path","").strip()
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")

    dry_run = bool(b.get("dry_run", False))

    # Load existing rutas to skip duplicates
    existing = {r["ruta_archivo"] for r in
                db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                if r.get("ruta_archivo")}

    # Load existing empresas for fuzzy matching
    empresas_existentes = db.fetchall("SELECT id, nombre FROM empresas")
    emp_by_nombre = {e["nombre"].lower(): e["id"] for e in empresas_existentes}

    from utils.normalizacion import normalizar_alias_empresa as _norm_alias
    alias_cache = {
        row["alias_norm"]: row["empresa_id"]
        for row in db.fetchall("SELECT alias_norm, empresa_id FROM empresa_aliases")
    }

    from datetime import datetime as _dt
    import re

    stats = {
        "empresas_creadas": 0,
        "cotizaciones_importadas": 0,
        "ya_existian": 0,
        "errores": 0,
        "empresas_nuevas": [],
    }

    # Walk the folder tree
    for fpath, fname, folder_chain in walk_files(path, {'.pdf'}):
        if fpath in existing:
            stats["ya_existian"] += 1
            continue

        stem = os.path.splitext(fname)[0]
        client_name = _get_client_name(folder_chain, stem)

        if not client_name or len(client_name.strip()) < 2:
            stats["errores"] += 1
            continue

        client_name = client_name.strip()

        if dry_run:
            stats["cotizaciones_importadas"] += 1
            continue

        # Find or create empresa
        # 1. Resolver por alias exacto normalizado (cache, sin N+1)
        norm = _norm_alias(client_name)
        eid = alias_cache.get(norm)
        if not eid:
            # 2. Flujo viejo: nombre exacto case-insensitive
            eid = emp_by_nombre.get(client_name.lower())
            if not eid:
                for en, eid_try in emp_by_nombre.items():
                    if en == client_name.lower():
                        eid = eid_try
                        break

        if not eid:
            # Create new empresa
            ok2 = db.agregar_empresa(
                client_name, "", "", "", "", "", "")
            if ok2:
                row = db.fetchone(
                    "SELECT id FROM empresas WHERE nombre=?",
                    (client_name,))
                if row:
                    eid = row["id"]
                    emp_by_nombre[client_name.lower()] = eid
                    stats["empresas_creadas"] += 1
                    stats["empresas_nuevas"].append(client_name)

        if not eid:
            stats["errores"] += 1
            continue

        # Get file date
        try:
            mtime = _dt.fromtimestamp(
                os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        ok2 = db.agregar_cotizacion_con_ruta(
            eid, fname, 0.0, mtime, fpath)
        if ok2:
            stats["cotizaciones_importadas"] += 1
            existing.add(fpath)

            # Launch background resumen
            def _bg(fp=fpath, ei=eid):
                try:
                    import sqlite3 as _sq
                    from extractor_texto import extraer
                    from resumidor import resumir
                    texto = extraer(fp)
                    data  = resumir(texto)
                    conn  = _sq.connect(cfg.get_db_name(), timeout=10)
                    conn.execute("PRAGMA busy_timeout=5000")
                    cols = [r[1] for r in
                            conn.execute("PRAGMA table_info(cotizaciones)")]
                    sets, vals = [], []
                    if "resumen"      in cols:
                        sets.append("resumen=?")
                        vals.append(data.get("resumen",""))
                    if "proveedor_ia" in cols:
                        sets.append("proveedor_ia=?")
                        vals.append(data.get("proveedor_ia","none"))
                    if "monto" in cols and data.get("monto",0) and                                 float(data.get("monto",0)) > 0:
                        sets.append("monto=?")
                        vals.append(float(data["monto"]))
                    if sets:
                        vals += [ei, fp]
                        conn.execute(
                            f"UPDATE cotizaciones SET {','.join(sets)} "
                            f"WHERE empresa_id=? AND ruta_archivo=?", vals)
                        conn.commit()
                    conn.close()
                except Exception as exc:
                    logging.error(f"bg resumen masivo: {exc}")
            threading.Thread(target=_bg, daemon=True).start()
        else:
            stats["errores"] += 1

    return ok(stats)


@app.route("/api/importar/masivo/preview", methods=["POST"])
def importar_masivo_preview():
    """
    Preview sin importar: devuelve cuántas empresas y cotizaciones
    se crearían, con una muestra de los primeros 50 archivos.
    """
    b    = request.json or {}
    path = b.get("path","").strip()
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")

    existing = {r["ruta_archivo"] for r in
                db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                if r.get("ruta_archivo")}
    empresas_existentes = {e["nombre"].lower() for e in
                           db.fetchall("SELECT nombre FROM empresas")}

    import re
    clients_new = set()
    clients_existing = set()
    total_pdf = 0
    ya_importados = 0
    sample = []

    for fpath, fname, folder_chain in walk_files(path, {'.pdf'}):
        total_pdf += 1
        if fpath in existing:
            ya_importados += 1
            continue
        stem  = os.path.splitext(fname)[0]
        client = _get_client_name(folder_chain, stem).strip()
        if client.lower() in empresas_existentes:
            clients_existing.add(client)
        else:
            clients_new.add(client)
        if len(sample) < 50:
            sample.append({
                "file": fname,
                "client": client,
                "is_new": client.lower() not in empresas_existentes,
            })

    return ok({
        "total_pdf":         total_pdf,
        "ya_importados":     ya_importados,
        "a_importar":        total_pdf - ya_importados,
        "empresas_nuevas":   len(clients_new),
        "empresas_existentes": len(clients_existing),
        "sample":            sample,
    })

# ── Backup ────────────────────────────────────────────────────────────────────
@app.route("/api/backup", methods=["POST"])
def backup():
    ok2 = BackupManager.hacer_backup(cfg.get_db_name())
    if not ok2: return err("Error al crear backup")
    return ok({"mensaje": "Backup creado"})

@app.route("/api/backup/restaurar", methods=["POST"])
def restaurar():
    crear_backup_seguro("antes_restaurar")
    ok2 = BackupManager.restaurar_backup(cfg.get_db_name())
    if not ok2: return err("No hay backup disponible")
    return ok({"mensaje": "Backup restaurado"})

# ── Config ────────────────────────────────────────────────────────────────────
@app.route("/api/config")
def get_config():
    return ok({k: acfg.get(k) for k in
               ("theme","duplicados_umbral","busqueda_page_size",
                "importer_thresh","ai_provider","gemini_model","grok_model",
                "filtros_guardados")})

@app.route("/api/config", methods=["PUT"])
def put_config():
    b = request.json or {}
    for k, v in b.items():
        acfg.set(k, v)
    return ok()

@app.route("/api/config/filtros", methods=["POST"])
def save_filtro():
    b = request.json or {}
    nombre = clean(b.get("nombre"))
    if not nombre: return err("Nombre requerido")
    acfg.guardar_filtro(nombre, b.get("filtros", {}))
    return ok()

@app.route("/api/config/filtros/<nombre>", methods=["DELETE"])
def delete_filtro(nombre):
    acfg.eliminar_filtro(nombre)
    return ok()

# ── Enriquecedor ──────────────────────────────────────────────────────────────
@app.route("/api/enriquecer", methods=["POST"])
def enriquecer():
    allowed, msg = _ia_ratelimit()
    if not allowed: return err(msg, 429)
    b        = request.json or {}
    provider = b.get("provider","auto")
    limit    = to_int(b.get("limit", 50), 50, lo=1, hi=1000)
    apply_   = bool(b.get("apply", False))
    min_conf = to_float(b.get("min_confidence", 0.85), 0.85)
    only     = b.get("only")

    try:
        from enriquecer_empresas_gemini import (
            load_empresas, call_provider, validate_results, safe_apply)
        empresas = load_empresas(db, limit=limit, only=only, all_companies=False)
        if not empresas:
            return ok({"results":[], "candidates":0,
                       "mensaje":"Sin empresas sospechosas para enriquecer"})
        all_results = []
        for i in range(0, len(empresas), 10):
            batch     = empresas[i:i+10]
            batch_ids = {int(e["id"]) for e in batch}
            try:
                raw, _ = call_provider(provider, batch, None)
                all_results.extend(validate_results(raw, batch_ids))
            except Exception as exc:
                for e in batch:
                    all_results.append({
                        "id": e["id"], "current_name": e["nombre"],
                        "canonical_name": e["nombre"], "confidence": 0.0,
                        "should_update": False, "reason": str(exc)})
        if apply_:
            updated, skipped = safe_apply(db, all_results, min_conf)
            return ok({"updated":updated,"skipped":skipped,"results":all_results})
        candidates = sum(1 for r in all_results
                         if r.get("should_update") and
                         float(r.get("confidence",0)) >= min_conf)
        return ok({"results": all_results, "candidates": candidates})
    except Exception as exc:
        return err(str(exc))

# ── Diagnóstico / Verificación ────────────────────────────────────────────────
@app.route("/api/diagnostico")
def api_diagnostico():
    data  = db.get_diagnostico_datos()
    rutas = rows_to_list(db.fetchall(
        "SELECT id, ruta_archivo FROM cotizaciones "
        "WHERE ruta_archivo IS NOT NULL AND ruta_archivo!=''"))
    data["rutas_rotas"]        = sum(1 for r in rutas if not os.path.isfile(r.get("ruta_archivo") or ""))
    data["total_rutas_archivo"] = len(rutas)
    data["timestamp"]          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return ok(data)

@app.route("/api/verificar")
def api_verificar():
    mods = ["flask","rapidfuzz","pandas","openpyxl","pdfplumber","docx","pptx","google.genai","openai"]
    dep  = {m: _safe_find_spec(m) for m in mods}
    cols = [r.get("name") for r in db.fetchall("PRAGMA table_info(cotizaciones)")]
    req  = ["moneda","resumen","proveedor_ia","estado_ia","error_ia","archivo_hash","fecha_importacion"]
    return ok({
        "python":                    sys.version.split()[0],
        "dependencias":              dep,
        "db_path":                   cfg.get_db_name(),
        "db_existe":                 os.path.isfile(cfg.get_db_name()),
        "cotizaciones_columnas_ok":  all(c in cols for c in req),
        "faltan_columnas":           [c for c in req if c not in cols],
        "diagnostico":               db.get_diagnostico_datos(),
    })

# ── SSE — polling estado IA ───────────────────────────────────────────────────
@app.route("/api/cotizaciones/<int:cid>/estado_ia")
def get_estado_ia(cid):
    """Polling endpoint: devuelve estado_ia actual de la cotización."""
    row = db.get_cotizacion_por_id(cid)
    if not row: return err("No encontrada", 404)
    return ok({
        "estado_ia":    row.get("estado_ia",""),
        "error_ia":     row.get("error_ia",""),
        "resumen":      row.get("resumen",""),
        "proveedor_ia": row.get("proveedor_ia",""),
        "monto":        row.get("monto"),
        "moneda":       row.get("moneda",""),
    })


# ── Sprint D: staging de importación (preview/corrección/commit) ─────────────
IMPORT_BATCH_EXTS = {".pdf", ".docx", ".xlsx", ".doc", ".xls", ".pptx", ".txt"}
ESTADOS_BATCH_VALIDOS = {"preview", "corrigiendo", "committing", "completado", "error", "cancelado"}
ESTADOS_ITEM_VALIDOS  = {"pendiente", "match_auto", "requiere_revision", "omitido", "importado", "error"}
ACCIONES_ITEM_VALIDAS = {"crear", "usar_existente", "omitir"}


@app.route("/api/import_batches/scan", methods=["POST"])
def import_batch_scan():
    """
    Escanea una carpeta y crea un batch + sus items en estado 'preview'.
    NO toca cotizaciones ni crea empresas todavía — solo registra qué se
    detectó, para que el usuario corrija antes de confirmar (commit).
    """
    b    = request.json or {}
    path = clean(b.get("path"))
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")

    thresh = max(0, min(to_int(b.get("thresh", 85), 85), 100))

    try:
        from rapidfuzz import process as fz_process
    except ImportError:
        from difflib import SequenceMatcher
        class fz_process:
            @staticmethod
            def extract(q, choices, limit=1):
                s = [(ch, int(SequenceMatcher(None, str(q or "").lower(),
                      str(ch or "").lower()).ratio()*100)) for ch in choices]
                return sorted(s, key=lambda x:-x[1])[:limit]

    empresas     = db.fetchall("SELECT id, nombre FROM empresas ORDER BY nombre")
    comp_names   = [e["nombre"] for e in empresas]
    comp_by_name = {e["nombre"]: e["id"] for e in empresas}

    from utils.normalizacion import normalizar_alias_empresa as _norm_alias
    alias_cache = {
        row["alias_norm"]: row["empresa_id"]
        for row in db.fetchall("SELECT alias_norm, empresa_id FROM empresa_aliases")
    }

    existing_rutas  = {r["ruta_archivo"] for r in
                       db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                       if r.get("ruta_archivo")}

    batch_id = db.crear_import_batch("masivo_staging", path, metadata={"thresh": thresh})
    if not batch_id:
        return err("No se pudo crear el batch")

    counts = {"requiere_revision": 0, "match_auto": 0, "omitido": 0}
    total = 0

    for fpath, fname, folder_chain in walk_files(path, IMPORT_BATCH_EXTS):
        total += 1
        stem = os.path.splitext(fname)[0]
        empresa_nombre = _get_client_name(folder_chain, stem)
        pais_detectado = _detect_pais(folder_chain)

        # Ya importado antes (por ruta) -> se previsualiza como omitido,
        # no se vuelve a tocar al hacer commit.
        if fpath in existing_rutas:
            counts["omitido"] += 1
            db.agregar_import_item(
                batch_id, fpath, fname,
                empresa_detectada=empresa_nombre, pais_detectado=pais_detectado,
                estado="omitido", accion="omitir",
                error="Ya importado anteriormente (misma ruta)")
            continue

        # 1. Alias exacto (Sprint B) gana siempre sobre fuzzy
        alias_norm = _norm_alias(empresa_nombre) if empresa_nombre else None
        eid = alias_cache.get(alias_norm) if alias_norm else None
        sim = 100 if eid else 0

        # 2. Si no hay alias, fuzzy match contra nombres de empresa existentes
        if not eid and comp_names and empresa_nombre:
            hits = fz_process.extract(empresa_nombre, comp_names, limit=1)
            if hits:
                sugerida, score = hits[0][0], int(round(hits[0][1]))
                if score >= thresh:
                    eid, sim = comp_by_name.get(sugerida), score

        if eid:
            counts["match_auto"] += 1
            db.agregar_import_item(
                batch_id, fpath, fname,
                empresa_detectada=empresa_nombre, empresa_id=eid,
                pais_detectado=pais_detectado, estado="match_auto",
                accion="usar_existente", confianza=sim)
        else:
            counts["requiere_revision"] += 1
            db.agregar_import_item(
                batch_id, fpath, fname,
                empresa_detectada=empresa_nombre,
                pais_detectado=pais_detectado, estado="requiere_revision",
                accion="crear", confianza=sim)

    db.actualizar_import_batch(batch_id, total_items=total,
                               omitidos=counts["omitido"])
    return ok({"batch_id": batch_id, "total_items": total, **counts})


@app.route("/api/import_batches/<int:batch_id>")
def import_batch_get(batch_id):
    batch = db.obtener_import_batch(batch_id)
    if not batch: return err("Batch no encontrado", 404)
    batch["conteo_por_estado"] = db.contar_import_items_por_estado(batch_id)
    return ok(batch)


@app.route("/api/import_batches/<int:batch_id>/items")
def import_batch_items(batch_id):
    if not db.obtener_import_batch(batch_id):
        return err("Batch no encontrado", 404)
    estado = request.args.get("estado")
    if estado and estado not in ESTADOS_ITEM_VALIDOS:
        return err(f"Estado inválido: {estado!r}")
    offset = max(0, to_int(request.args.get("offset", 0), 0))
    limit  = max(1, min(to_int(request.args.get("limit", 200), 200), 500))
    items  = db.listar_import_items(batch_id, estado=estado, offset=offset, limit=limit)
    return ok(items, offset=offset, limit=limit)


@app.route("/api/import_items/<int:item_id>", methods=["PATCH"])
def import_item_patch(item_id):
    """Corrección manual de un item antes del commit: elegir empresa
    existente, cambiar país, marcar para omitir, etc."""
    item = db.obtener_import_item(item_id)
    if not item: return err("Item no encontrado", 404)

    b = request.json or {}
    campos = {}
    if "accion" in b:
        accion = b["accion"]
        if accion not in ACCIONES_ITEM_VALIDAS:
            return err(f"Acción inválida: {accion!r}")
        campos["accion"] = accion
        # Si pasa a 'omitir', el estado refleja eso; si se corrige, ya no
        # 'requiere_revision' por defecto (el usuario decidió qué hacer).
        if accion == "omitir":
            campos["estado"] = "omitido"
        elif item["estado"] == "omitido":
            campos["estado"] = "pendiente"
    if "empresa_id" in b:
        eid = to_int(b.get("empresa_id"), 0, lo=0)
        if eid and not db.obtener_empresa_por_id(eid):
            return err("Empresa no encontrada")
        campos["empresa_id"] = eid or None
    if "pais_detectado" in b:
        campos["pais_detectado"] = clean(b.get("pais_detectado")) or None

    if not campos:
        return err("Nada para actualizar")
    if not db.actualizar_import_item(item_id, **campos):
        return err("No se pudo actualizar el item")
    return ok(db.obtener_import_item(item_id))


def _ejecutar_commit_batch(batch_id):
    """Lógica real de commit, sin el guard de estado de la ruta pública —
    la usan tanto /commit como /retry_errors (que necesita poder re-correr
    el commit sobre un batch ya 'completado' con errores pendientes)."""
    db.actualizar_import_batch(batch_id, estado="committing")
    items = db.listar_import_items(batch_id, limit=100000)

    creados = actualizados = errores = 0
    for item in items:
        if item["estado"] in ("omitido", "importado"):
            continue
        try:
            # Anti-duplicado por hash (si se calculó) además de por ruta
            if item["file_hash"] and db.cotizacion_existe_por_hash(item["file_hash"]):
                db.actualizar_import_item(item["id"], estado="omitido",
                                          error="Duplicado por hash")
                continue

            empresa_id = item["empresa_id"]
            if item["accion"] == "crear":
                nombre = item["empresa_detectada"] or os.path.splitext(item["file_name"])[0]
                # Por si dos items del mismo batch detectan la misma empresa nueva
                existente = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (nombre,))
                if existente:
                    empresa_id = existente["id"]
                else:
                    if not db.agregar_empresa(nombre, "", "", "", "",
                                              item["pais_detectado"] or "", ""):
                        raise RuntimeError(f"No se pudo crear empresa {nombre!r}")
                    nueva = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (nombre,))
                    empresa_id = nueva["id"] if nueva else None
                    creados += 1
            elif item["accion"] == "usar_existente":
                if not empresa_id:
                    raise RuntimeError("accion='usar_existente' sin empresa_id asignado")
                actualizados += 1
            else:
                continue  # 'omitir' ya filtrado arriba, no debería llegar acá

            if not empresa_id:
                raise RuntimeError("No se pudo resolver la empresa destino")

            ok_cot = db.agregar_cotizacion_con_ruta(
                empresa_id, "", 0, None, item["file_path"],
                archivo_hash=item["file_hash"])
            if not ok_cot:
                raise RuntimeError("No se pudo insertar la cotización")

            db.actualizar_import_item(item["id"], estado="importado",
                                      empresa_id=empresa_id,
                                      fecha_procesado=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            errores += 1
            db.actualizar_import_item(item["id"], estado="error", error=str(e))

    # Acumula sobre los contadores previos (relevante para retry_errors,
    # que vuelve a llamar esto sobre un batch que ya tenía creados/actualizados
    # de una corrida anterior).
    batch_prev = db.obtener_import_batch(batch_id)
    db.actualizar_import_batch(
        batch_id, estado="completado",
        creados=(batch_prev["creados"] or 0) + creados,
        actualizados=(batch_prev["actualizados"] or 0) + actualizados,
        errores=errores,  # errores es un conteo actual, no acumulado: si se
                          # corrigieron, no deben seguir contando como error
        fecha_commit=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return db.obtener_import_batch(batch_id)


@app.route("/api/import_batches/<int:batch_id>/commit", methods=["POST"])
def import_batch_commit(batch_id):
    """
    Confirma el batch: para cada item no omitido, crea la empresa (si
    accion='crear') o usa la existente (si accion='usar_existente'), e
    inserta la cotización. Un error en un item no frena el resto —
    el item queda en estado 'error' con el mensaje, y se puede reintentar
    con /retry_errors sin tocar los que ya se importaron bien.
    """
    batch = db.obtener_import_batch(batch_id)
    if not batch: return err("Batch no encontrado", 404)
    if batch["estado"] in ("completado", "committing"):
        return err(f"El batch ya está en estado {batch['estado']!r}")
    return ok(_ejecutar_commit_batch(batch_id))


@app.route("/api/import_batches/<int:batch_id>/retry_errors", methods=["POST"])
def import_batch_retry_errors(batch_id):
    """Reintenta solo los items en estado 'error' del batch, sin tocar
    los que ya están 'importado' u 'omitido'."""
    batch = db.obtener_import_batch(batch_id)
    if not batch: return err("Batch no encontrado", 404)

    errores_items = db.listar_import_items(batch_id, estado="error", limit=100000)
    if not errores_items:
        return ok({"reintentados": 0, "mensaje": "No hay items en error"})

    for item in errores_items:
        db.actualizar_import_item(item["id"], estado="pendiente", error=None)

    return ok(_ejecutar_commit_batch(batch_id))


@app.route("/api/import_batches/<int:batch_id>/cancel", methods=["POST"])
def import_batch_cancel(batch_id):
    batch = db.obtener_import_batch(batch_id)
    if not batch: return err("Batch no encontrado", 404)
    if batch["estado"] == "completado":
        return err("No se puede cancelar un batch ya completado")
    if not db.actualizar_import_batch(batch_id, estado="cancelado"):
        return err("No se pudo cancelar")
    return ok(db.obtener_import_batch(batch_id))


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  CRM v26  →  http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
