import os
import sys
import unittest
import tempfile
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_manager import DBManager
from pipeline_runtime import fase_de_etapa


def fresh_db():
    return DBManager(":memory:")


def make_emp(db, nombre="Empresa Test"):
    db.agregar_empresa(nombre, "", "", "", "", "Argentina", "")
    row = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (nombre,))
    return row["id"] if row else None


class TestOportunidades(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.eid = make_emp(self.db)

    def test_crear_oportunidad_basico(self):
        ok = self.db.crear_oportunidad(self.eid, "Venta tolva", descripcion="Necesidad detectada")
        self.assertTrue(ok)
        rows = self.db.get_oportunidades_empresa(self.eid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["titulo"], "Venta tolva")
        self.assertEqual(rows[0]["fase"], "venta")

    def test_oportunidad_empresa_inexistente_rechazada(self):
        self.assertFalse(self.db.crear_oportunidad(99999, "No existe"))

    def test_etapa_invalida_rechazada(self):
        self.assertFalse(self.db.crear_oportunidad(self.eid, "Mala", etapa="inventada"))

    def test_cambiar_etapa_valida(self):
        self.db.crear_oportunidad(self.eid, "Seguimiento")
        oid = self.db.get_oportunidades_empresa(self.eid)[0]["id"]
        self.assertTrue(self.db.cambiar_etapa_oportunidad(oid, "contactado"))
        row = self.db.get_oportunidad_por_id(oid)
        self.assertEqual(row["etapa"], "contactado")

    def test_cambiar_etapa_ganado_a_posventa(self):
        self.db.crear_oportunidad(self.eid, "Ganable")
        oid = self.db.get_oportunidades_empresa(self.eid)[0]["id"]
        self.assertTrue(self.db.cambiar_etapa_oportunidad(oid, "ganado"))
        self.assertEqual(self.db.get_oportunidad_por_id(oid)["fase"], "posventa")
        self.assertTrue(self.db.cambiar_etapa_oportunidad(oid, "en_proceso"))
        row = self.db.get_oportunidad_por_id(oid)
        self.assertEqual(row["etapa"], "en_proceso")
        self.assertEqual(row["fase"], "posventa")

    def test_fase_derivada_correctamente(self):
        self.assertEqual(fase_de_etapa("prospecto"), "venta")
        self.assertEqual(fase_de_etapa("ganado"), "posventa")
        self.assertEqual(fase_de_etapa("entregada"), "posventa")

    def test_cascade_delete_con_empresa(self):
        self.db.crear_oportunidad(self.eid, "Se borra")
        self.db.eliminar_empresa(self.eid)
        self.assertEqual(self.db.get_oportunidades_empresa(self.eid), [])

    def test_multiples_oportunidades_misma_empresa(self):
        self.db.crear_oportunidad(self.eid, "Uno")
        self.db.crear_oportunidad(self.eid, "Dos")
        self.assertEqual(len(self.db.get_oportunidades_empresa(self.eid)), 2)

    def test_monto_none_permitido(self):
        self.assertTrue(self.db.crear_oportunidad(self.eid, "Sin monto", monto_estimado=None))

    def test_sql_injection_en_titulo_notas(self):
        payload = "'; DROP TABLE oportunidades;--"
        self.assertTrue(self.db.crear_oportunidad(self.eid, payload, notas=payload))
        self.assertIsNotNone(self.db.fetchall("SELECT * FROM oportunidades"))


class TestOportunidadesHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        os.environ["CRM_DB_NAME"] = self.tmp.name
        if "server" in sys.modules:
            importlib.reload(sys.modules["server"])
        else:
            import server  # noqa: F401
        self.server = sys.modules["server"]
        self.server.app.config["TESTING"] = True
        self.client = self.server.app.test_client()
        self.db = self.server.db
        self.eid = make_emp(self.db, "HTTP Empresa")

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_post_oportunidad(self):
        r = self.client.post("/api/oportunidades", json={"empresa_id": self.eid, "titulo": "HTTP venta"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["ok"])

    def test_get_oportunidades(self):
        self.db.crear_oportunidad(self.eid, "Listado")
        r = self.client.get("/api/oportunidades")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["data"]), 1)

    def test_get_oportunidades_empresa(self):
        self.db.crear_oportunidad(self.eid, "Por empresa")
        r = self.client.get(f"/api/empresas/{self.eid}/oportunidades")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_put_etapa_valida(self):
        self.db.crear_oportunidad(self.eid, "Mover")
        oid = self.db.get_oportunidades_empresa(self.eid)[0]["id"]
        r = self.client.put(f"/api/oportunidades/{oid}/etapa", json={"etapa": "contactado"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_oportunidad_por_id(oid)["etapa"], "contactado")

    def test_put_etapa_invalida_400(self):
        self.db.crear_oportunidad(self.eid, "Mover mal")
        oid = self.db.get_oportunidades_empresa(self.eid)[0]["id"]
        r = self.client.put(f"/api/oportunidades/{oid}/etapa", json={"etapa": "xyz"})
        self.assertEqual(r.status_code, 400)

    def test_delete_oportunidad(self):
        self.db.crear_oportunidad(self.eid, "Borrar")
        oid = self.db.get_oportunidades_empresa(self.eid)[0]["id"]
        r = self.client.delete(f"/api/oportunidades/{oid}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.db.get_oportunidad_por_id(oid))

    def test_oportunidad_empresa_inexistente_404(self):
        r = self.client.post("/api/oportunidades", json={"empresa_id": 999999, "titulo": "No"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
