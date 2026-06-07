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
    q = request.args.get("q","")
    filtros = {}
    for k in ("pais","rubro","tag","cotizaciones_cond","contactos_cond"):
        v = request.args.get(k)
        if v: filtros[k] = v
    dias = to_int(request.args.get("dias_cotizacion"), 0, lo=0)
    if dias: filtros["dias_cotizacion"] = dias
    dias_act = to_int(request.args.get("dias_actividad"), 0, lo=0)
    if dias_act: filtros["dias_actividad"] = dias_act

    rows   = db.get_filtered_empresas(q, filtros)
    result = []
    for r in rows:
        d = dict(r)
        d["ultima"] = formatear_fecha(db.get_ultima_cotizacion(r["id"]) or "")
        d["ncot"]   = db.count_by_empresa("cotizaciones", r["id"])
        d["tags"]   = db.get_tags_de_empresa(r["id"])
        result.append(d)
    return ok(result)

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
    return ok(db.get_oportunidades(filtros))


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
@app.route("/api/duplicados")
def get_duplicados():
    umbral = to_int(request.args.get("umbral", 85), 85, lo=50, hi=100)
    return ok(db.get_similar_empresas(umbral))

@app.route("/api/duplicados/merge", methods=["POST"])
def post_merge():
    b = request.json or {}
    origen  = to_int(b.get("origen_id"),  0, lo=1)
    destino = to_int(b.get("destino_id"), 0, lo=1)
    if not origen or not destino: return err("IDs inválidos")
    if origen == destino: return err("Origen y destino son iguales")
    ok2 = db.unificar_empresas(origen, destino)
    if not ok2: return err("No se pudo fusionar")
    return ok()

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

    ok_count = skip_count = err_count = 0
    for item in items:
        eid   = to_int(item.get("empresa_id"), 0, lo=1)
        fpath = clean(item.get("file_path"))
        fname = clean(item.get("file_name"))
        if not eid or not fpath: err_count += 1; continue
        if fpath in existing_rutas: skip_count += 1; continue
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
    return ok({"importados": ok_count, "omitidos": skip_count, "errores": err_count})

# ── Escanear carpeta ──────────────────────────────────────────────────────────
@app.route("/api/escanear", methods=["POST"])
def escanear_carpeta():
    b    = request.json or {}
    path = clean(b.get("path"))
    if not path or not os.path.isdir(path):
        return err(f"Carpeta no encontrada: {path!r}")
    EXTS = {".pdf",".docx",".xlsx",".doc",".xls",".pptx",".txt"}
    try:
        from fuzzywuzzy import process as fz_process
    except ImportError:
        from difflib import SequenceMatcher
        class fz_process:
            @staticmethod
            def extract(q, choices, limit=1):
                s = [(c, int(SequenceMatcher(None,str(q or "").lower(),
                      str(c or "").lower()).ratio()*100)) for c in choices]
                return sorted(s, key=lambda x:-x[1])[:limit]

    empresas     = db.fetchall("SELECT id, nombre FROM empresas ORDER BY nombre")
    comp_names   = [e["nombre"] for e in empresas]
    comp_by_name = {e["nombre"]: e["id"] for e in empresas}
    existing     = {r["ruta_archivo"] for r in
                    db.fetchall("SELECT ruta_archivo FROM cotizaciones")
                    if r.get("ruta_archivo")}
    existing_hashes = {r["archivo_hash"] for r in
                       db.fetchall("SELECT archivo_hash FROM cotizaciones")
                       if r.get("archivo_hash")}
    found = []
    for root, _, files in os.walk(path):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTS: continue
            fpath = os.path.join(root, fname)
            if fpath in existing: continue
            try:
                ahash = file_sha256(fpath)
                if ahash in existing_hashes: continue
            except Exception:
                ahash = None
            rel      = os.path.relpath(fpath, path)
            carpeta  = rel.split(os.sep)[0] if os.sep in rel else ""
            src      = carpeta or os.path.splitext(fname)[0]
            sugerida, sim, eid = "", 0, None
            if comp_names:
                hits = fz_process.extract(src, comp_names, limit=1)
                if hits:
                    sugerida, sim = hits[0][0], hits[0][1]
                    eid = comp_by_name.get(sugerida)
            found.append({
                "file_path": fpath, "file_name": fname,
                "carpeta": carpeta, "sugerida": sugerida,
                "sim": sim, "eid": eid, "archivo_hash": ahash,
            })
    return ok(found, total=len(found))

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
GENERIC_FOLDERS = {
    'argentina','bolivia','chile','colombia','estados unidos','mexico',
    'peru','uruguay','nicaragua','paraguay','ecuador','brasil',
    'comercial','equipos','aspiradores','cabinas','pipe (tubos)',
    'repuestos tolva','hojas membretadas especiales',
    'pedidos presupuestos internacionales','datos de catalogo tolvas',
    'tg 70','tg100','tg250','tg500',
    'cartas','carta','cotizaciones','cotizacion','clientes','clientes x zona',
    'planos','mantenimiento','nueva carpeta','mto','mto-2011','mto-2012',
    'mto-2013','mto2011','a- representantes en el exterior',
    '00 cotizaciones tipo de maq',
    'enero 2011','febrero 2011','julio 2011','junio 2011','diciembre 2011',
    'abril2011','noviembre-2011','agosto','mayo','2013',
    '500p','800p','1000p','1000s','1200p','1200s','1500p','500s','600p','600s',
    '6 ca','1 ca','1 cl','12 ca','147 m 20 hp','maquina nueva',
    'aaa-con o sin competencia','aa cotizaciones e informes tipicas',
}

IMPORT_EXTS = {'.pdf','.docx','.xlsx','.doc','.xls','.pptx','.txt'}

def _extract_client_from_stem(stem):
    """'ALBANESI02' → 'ALBANESI', 'Alejandro Garaggiola01' → 'Alejandro Garaggiola'"""
    import re
    name = re.sub(r'[\s_-]*\d+\s*$', '', stem).strip()
    return name if name else stem

def _get_client_name(folder_chain, filename_stem):
    """Walk folders deepest-first, skip generic ones; fallback to filename."""
    for folder in reversed(folder_chain):
        if folder.lower().strip() not in GENERIC_FOLDERS:
            return folder
    return _extract_client_from_stem(filename_stem)


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
    for root, dirs, files in os.walk(path):
        # Build folder chain relative to base path
        rel = os.path.relpath(root, path)
        if rel == '.':
            folder_chain = []
        else:
            folder_chain = rel.replace('\\', '/').replace('\\', '/').split('/')

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext != '.pdf':  # Only PDF
                continue

            fpath = os.path.join(root, fname)

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
            eid = emp_by_nombre.get(client_name.lower())
            if not eid:
                # Try case-insensitive match
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

    for root, dirs, files in os.walk(path):
        rel = os.path.relpath(root, path)
        folder_chain = [] if rel == '.' else rel.replace('\\', '/').split('/')

        for fname in files:
            if os.path.splitext(fname)[1].lower() != '.pdf':
                continue
            total_pdf += 1
            fpath = os.path.join(root, fname)
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
    mods = ["flask","fuzzywuzzy","pandas","openpyxl","pdfplumber","docx","pptx","google.genai","openai"]
    dep  = {m: importlib.util.find_spec(m) is not None for m in mods}
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

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  CRM v26  →  http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
