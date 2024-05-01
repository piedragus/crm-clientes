"""Pruebas HTTP/lógica para CRM web v25+.
No abre navegador: usa Flask test_client y una DB temporal aislada.
"""
import os, tempfile, json, threading, time
from pathlib import Path

import server
from db_manager import DBManager

class FakeCfg:
    def __init__(self, db_path): self.db_path = db_path
    def get_db_name(self): return self.db_path

def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)

def main():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmpdir.name, 'crm_http_stress.db')
    server.cfg = FakeCfg(db_path)
    server.db = DBManager(db_path)
    app = server.app
    app.config['TESTING'] = True
    c = app.test_client()

    # Smoke endpoints
    for url in ['/api/stats','/api/verificar','/api/diagnostico','/api/meta/paises','/api/meta/rubros','/api/meta/tags']:
        r = c.get(url)
        assert_true(r.status_code == 200 and r.json.get('ok'), f'{url} no OK')

    # Inputs ilógicos no deben tirar 500
    weird = [
        '/api/buscar?page=x&page_size=y&monto_min=abc&monto_max=def',
        '/api/duplicados?umbral=x',
    ]
    for url in weird:
        r = c.get(url)
        assert_true(r.status_code < 500, f'{url} devolvió 500')

    # Empresa inválida debe ser 400, válida 201
    r = c.post('/api/empresas', json={'nombre':'   '})
    assert_true(r.status_code == 400, 'empresa vacía aceptada')
    r = c.post('/api/empresas', json={'nombre':'Acme Stress SA','pais':'Argentina','tags':'stress, test'})
    assert_true(r.status_code == 201 and r.json.get('ok'), 'no creó empresa')
    eid = r.json['data']['id']

    # Contacto inválido y válido
    r = c.post(f'/api/empresas/{eid}/contactos', json={'nombre':'','email':''})
    assert_true(r.status_code == 400, 'contacto vacío aceptado')
    r = c.post(f'/api/empresas/{eid}/contactos', json={'nombre':'Ana','email':'ana@acme.com'})
    assert_true(r.status_code == 201, 'contacto válido falló')

    # Cotización monto inválido no debe romper
    r = c.post(f'/api/empresas/{eid}/cotizaciones', json={'descripcion':'Cotización inválida','monto':'abc'})
    assert_true(r.status_code == 201, 'monto inválido debería normalizar a 0')

    # Importar carpeta: dedupe por hash
    folder = Path(tmpdir.name) / 'archivos'
    folder.mkdir()
    f1 = folder/'cotizacion1.txt'; f1.write_text('Oferta equipos USD 1234', encoding='utf-8')
    f2 = folder/'cotizacion2.txt'; f2.write_text('Oferta equipos USD 1234', encoding='utf-8')
    scan = c.post('/api/escanear', json={'path':str(folder)})
    assert_true(scan.status_code == 200 and scan.json.get('ok'), 'scan falló')
    items = [{'empresa_id':eid, 'file_path':str(f1), 'file_name':f1.name}, {'empresa_id':eid, 'file_path':str(f2), 'file_name':f2.name}]
    r = c.post('/api/importar/carpeta', json={'items':items})
    assert_true(r.status_code == 200 and r.json.get('ok'), 'import carpeta falló')
    data = r.json['data']
    assert_true(data['importados'] == 1 and data['omitidos'] == 1, f'dedupe hash no funcionó: {data}')

    # Regenerar resumen de archivo inexistente no debe 500
    row = server.db.fetchone('SELECT id FROM cotizaciones WHERE ruta_archivo IS NOT NULL LIMIT 1')
    assert_true(row is not None, 'no quedó cotización importada')
    r = c.post(f'/api/cotizaciones/{row["id"]}/resumen')
    assert_true(r.status_code == 200 and r.json.get('ok'), 'resumen endpoint no aceptó archivo')

    # Concurrencia HTTP simple: crear empresas paralelas
    errors = []
    def worker(i):
        try:
            cc = app.test_client()
            rr = cc.post('/api/empresas', json={'nombre':f'Empresa concurrente {i}'})
            if rr.status_code not in (200,201): errors.append((i, rr.status_code, rr.get_data(as_text=True)))
        except Exception as exc:
            errors.append((i, repr(exc)))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert_true(not errors, f'errores concurrencia HTTP: {errors[:3]}')

    # Diagnóstico debe responder y tener claves nuevas
    r = c.get('/api/diagnostico')
    assert_true(r.status_code == 200 and 'ia_pendiente' in r.json['data'] and 'hashes_duplicados' in r.json['data'], 'diagnóstico incompleto')

    # Backup endpoint
    r = c.post('/api/backup')
    assert_true(r.status_code == 200 and r.json.get('ok'), 'backup endpoint falló')

    print('PASS pruebas_http_stress: endpoints, inputs ilógicos, dedupe hash, diagnóstico, backup, concurrencia HTTP')
    tmpdir.cleanup()

if __name__ == '__main__':
    main()

# ── Tests adicionales v26 ─────────────────────────────────────────────────────
def test_endpoints_adicionales(client, db):
    """Cubre los 9 endpoints sin test en v25."""
    import json as _json, time as _time
    _uid = str(int(_time.time()*1000))[-6:]
    # Create empresa via API to use the same db instance as the server
    r = client.post("/api/empresas",
                    data=_json.dumps({"nombre": f"HistTest_SA_{_uid}", "pais": "AR"}),
                    content_type="application/json")
    rj = r.get_json()
    assert rj and rj.get("ok"), f"No se pudo crear empresa: {rj}"
    eid = rj["data"]["id"]
    # Edit to generate historial
    client.put(f"/api/empresas/{eid}",
               data=_json.dumps({"nombre": f"HistTest_SA_v2_{_uid}", "pais": "Chile", "direccion": "", "telefono": "", "email": "", "rubro": "", "tags": ""}),
               content_type="application/json")
    hist_r = client.get(f"/api/empresas/{eid}/historial")
    hist = hist_r.get_json().get("data",[])
    assert hist, "Debe haber historial"
    hid = hist[0]["id"]

    # DELETE /api/historial/:id
    r = client.delete(f"/api/historial/{hid}")
    assert r.status_code == 200 and r.json["ok"], f"DELETE historial: {r.json}"

    # POST /api/historial/:id/revertir (revertir cambio existente)
    db.editar_empresa(eid,"Hist SA v3","","","","","AR","")
    hist2 = db.get_historial_empresa(eid)
    hid2  = hist2[0]["id"]
    r = client.post(f"/api/historial/{hid2}/revertir",
                    json={}, content_type="application/json")
    assert r.status_code == 200 and r.json["ok"], f"POST revertir: {r.json}"

    # POST /api/duplicados/merge
    ra = client.post("/api/empresas", data=_json.dumps({"nombre": f"DupA_{_uid}"}),
                     content_type="application/json")
    rb = client.post("/api/empresas", data=_json.dumps({"nombre": f"DupB_{_uid}"}),
                     content_type="application/json")
    eid_a = ra.json["data"]["id"]
    eid_b = rb.json["data"]["id"]
    r = client.post("/api/duplicados/merge",
                    json={"origen_id": eid_a, "destino_id": eid_b},
                    content_type="application/json")
    assert r.status_code == 200 and r.json["ok"], f"POST merge: {r.json}"
    assert not db.obtener_empresa_por_id(eid_a), "Origen debe eliminarse"

    # POST /api/importar/csv — archivo vacío debe dar error legible
    import io
    r = client.post("/api/importar/csv",
                    data={"file": (io.BytesIO(b""), "vacio.csv")},
                    content_type="multipart/form-data")
    # Empty CSV returns 400 with error message
    assert not r.get_json().get("ok"), f"CSV vacío debería dar error: {r.json}"

    # POST /api/importar/csv — CSV válido mínimo
    csv_content = b"nombre,email\nPedro,pedro@acmecorp.com\n"
    r = client.post("/api/importar/csv",
                    data={"file": (io.BytesIO(csv_content), "test.csv")},
                    content_type="multipart/form-data")
    assert r.get_json().get("ok"), f"CSV válido falló: {r.json}"

    # GET /api/exportar — empresas xlsx
    r = client.get("/api/exportar?fmt=xlsx&tipo=empresas")
    assert r.status_code == 200, f"Exportar xlsx: {r.status_code}"

    # GET /api/exportar — contactos csv
    r = client.get("/api/exportar?fmt=csv&tipo=contactos")
    assert r.status_code == 200, f"Exportar csv contactos: {r.status_code}"

    # GET /api/exportar — cotizaciones csv
    r = client.get("/api/exportar?fmt=csv&tipo=cotizaciones")
    assert r.status_code == 200, f"Exportar csv cotizaciones: {r.status_code}"

    # POST /api/backup/restaurar (sin backup disponible → error controlado)
    r = client.post("/api/backup/restaurar")
    # Either ok (if backup exists from earlier test) or error — no crash
    assert "ok" in r.json, f"Restaurar: respuesta inesperada {r.json}"

    # POST /api/config/filtros
    r = client.post("/api/config/filtros",
                    json={"nombre": "test_preset", "filtros": {"pais": "AR"}},
                    content_type="application/json")
    assert r.get_json().get("ok"), f"Save filtro: {r.json}"

    # DELETE /api/config/filtros/:nombre
    r = client.delete("/api/config/filtros/test_preset")
    assert r.get_json().get("ok"), f"Delete filtro: {r.json}"

    # POST /api/enriquecer — sin API keys → error controlado, no 500
    r = client.post("/api/enriquecer",
                    json={"provider":"gemini","limit":1,"apply":False},
                    content_type="application/json")
    # Either ok (no suspicious companies) or error (no key) — no crash
    assert "ok" in r.json, f"Enriquecer: respuesta inesperada: {r.json}"

    # GET /api/cotizaciones/:id/estado_ia
    rc = client.post(f"/api/empresas/{eid}/cotizaciones",
                     data=_json.dumps({"descripcion":"Test cotiz","monto":1000.0}),
                     content_type="application/json")
    cid_r = client.get(f"/api/empresas/{eid}/cotizaciones")
    cid = cid_r.get_json()["data"][0]["id"] if cid_r.json.get("data") else None
    assert cid, "Debe haber cotizacion"
    r = client.get(f"/api/cotizaciones/{cid}/estado_ia")
    assert r.get_json().get("ok"), f"Estado IA: {r.json}"

    # GET /api/meta/tipos_cotizacion
    r = client.get("/api/meta/tipos_cotizacion")
    assert r.get_json().get("ok"), f"Tipos cotizacion: {r.json}"

    # GET /api/cotizaciones/:id/archivo — sin archivo → 404 controlado
    r = client.get(f"/api/cotizaciones/{cid}/archivo")
    assert r.status_code in (404, 400), f"Archivo sin ruta debería dar 404: {r.status_code}"

    # Rate limiting: segunda llamada a enriquecer en menos de 5s
    r2 = client.post("/api/enriquecer",
                     json={"provider":"gemini","limit":1,"apply":False},
                     content_type="application/json")
    assert r2.status_code == 429, f"Rate limit debería dar 429: {r2.status_code}"

    print("[test_endpoints_adicionales] OK")


def test_validaciones_inputs(client, db):
    """Verifica que inputs malformados dan errores HTTP, no 500."""
    cases = [
        # (method, url, body, expected_status)
        ("POST", "/api/empresas", {"nombre": ""}, 400),
        ("POST", "/api/empresas", {}, 400),
        ("POST", "/api/duplicados/merge", {"origen_id": 0, "destino_id": 0}, 400),
        ("POST", "/api/duplicados/merge", {"origen_id": 1, "destino_id": 1}, 400),
        ("GET",  "/api/exportar?fmt=xml", None, 400),
        ("POST", "/api/config/filtros", {"nombre": "", "filtros": {}}, 400),
        ("GET",  "/api/empresas/999999", None, 404),
        ("GET",  "/api/cotizaciones/999999", None, 404),
    ]
    import json as _json
    for method, url, body, expected in cases:
        if method == "GET":
            r = client.get(url)
        else:
            r = client.post(url, data=_json.dumps(body),
                            content_type="application/json")
        assert r.status_code == expected, \
            f"{method} {url} body={body}: esperaba {expected}, got {r.status_code} — {r.data[:200]}"
    print("[test_validaciones_inputs] OK")


# Patch main runner to include new tests
_original_main = None
import sys as _sys

def _run_all():
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("TESTING", "1")

    import config_export as ce
    ce._instance = None

    # Re-import server to get a fresh app with fresh db for our tests
    import importlib
    import server as _srv
    importlib.reload(_srv)
    flask_app = _srv.app
    flask_db  = _srv.db
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()

    test_endpoints_adicionales(client, flask_db)
    test_validaciones_inputs(client, flask_db)
    print("PASS pruebas_http_stress v26: todos los endpoints cubiertos + validaciones")

if __name__ == "__main__":
    _run_all()
