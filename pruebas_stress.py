"""Pruebas de stress/concurrencia/lógica para CRM v25 web, sin abrir navegador."""
from __future__ import annotations
import csv
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from db_manager import DBManager
from csv_utils import _open_csv, _find_col, EMAIL_COLS
from utils import BackupManager, Exportador


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    print("[stress] inicio", flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="crm_v25_web_stress_"))
    db_path = tmp / "stress.db"
    try:
        db = DBManager(str(db_path))
        print("[stress] db creada", flush=True)

        cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(cotizaciones)")}
        required = {"moneda", "resumen", "proveedor_ia", "ruta_archivo", "nombre_archivo", "tipo"}
        assert_true(required <= cols, f"faltan columnas: {required-cols}")
        print("[stress] schema ok", flush=True)

        # Migración desde schema viejo mínimo.
        old = tmp / "old.db"
        con = sqlite3.connect(old)
        con.executescript("""
        CREATE TABLE empresas(id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE, direccion TEXT, telefono TEXT, email TEXT, rubro TEXT, pais TEXT);
        CREATE TABLE contactos(id INTEGER PRIMARY KEY AUTOINCREMENT, empresa_id INTEGER, nombre TEXT, telefono TEXT, email TEXT, pais TEXT);
        CREATE TABLE cotizaciones(id INTEGER PRIMARY KEY AUTOINCREMENT, empresa_id INTEGER, fecha TEXT, descripcion TEXT, monto REAL);
        CREATE TABLE cambios(id INTEGER PRIMARY KEY AUTOINCREMENT, empresa_id INTEGER, campo TEXT, valor_anterior TEXT, valor_nuevo TEXT, fecha TEXT, fuente TEXT);
        CREATE TABLE tags(id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT UNIQUE NOT NULL);
        CREATE TABLE empresa_tags(empresa_id INTEGER, tag_id INTEGER, PRIMARY KEY(empresa_id, tag_id));
        INSERT INTO empresas(nombre) VALUES('Vieja SA');
        INSERT INTO cotizaciones(empresa_id, fecha, descripcion, monto) VALUES(1, '1970-01-01', 'legacy', 10);
        """)
        con.commit(); con.close()
        old_db = DBManager(str(old))
        old_cols = {r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(cotizaciones)")}
        assert_true(required <= old_cols, "migración incompleta desde schema viejo")
        print("[stress] migración ok", flush=True)

        # CRUD con valores límite.
        long_name = "Empresa Ñandú 🚀 " + "X" * 500
        assert_true(db.agregar_empresa(long_name, "Dir", "Tel", "mail@empresa.com", "Rubro", "AR", "uno, dos"), "no agregó empresa unicode/larga")
        eid = db.obtener_empresa_por_nombre(long_name)["id"]
        assert_true(db.agregar_contacto(eid, "Contacto", "contacto@empresa.com", "+54 11", "AR"), "no agregó contacto")
        test_file = tmp / "cotizacion.txt"
        test_file.write_text("Cotización por equipos industriales. Total USD 1234.", encoding="utf-8")
        assert_true(db.agregar_cotizacion_con_ruta(eid, test_file.name, 0, "2026-04-28 10:00:00", str(test_file)), "no agregó cotización con ruta")
        assert_true(db.actualizar_resumen_cotizacion_por_ruta(eid, str(test_file), "Resumen OK", 1234, "USD", "none", "Equipos"), "no actualizó resumen")
        row = db.fetchone("SELECT resumen,monto,moneda,tipo,nombre_archivo FROM cotizaciones WHERE empresa_id=?", (eid,))
        assert_true(row["moneda"] == "USD" and row["tipo"] == "Equipos" and row["nombre_archivo"] == test_file.name, "campos IA/archivo mal guardados")
        print("[stress] crud ia ok", flush=True)

        # CSV encoding/separador/header flexible.
        csv_path = tmp / "contactos_cp1252.csv"
        csv_path.write_bytes("Nombre;Apellido;Email\nJosé;Pérez;jose@acme.com\n".encode("cp1252"))
        fh, enc, sep = _open_csv(str(csv_path))
        with fh:
            rows = list(csv.DictReader(fh, delimiter=sep))
        assert_true(enc in ("cp1252", "latin-1", "utf-8-sig", "utf-8") and sep == ";", "CSV encoding/separador no detectado")
        assert_true(_find_col(rows[0].keys(), EMAIL_COLS), "CSV email header no detectado")
        print("[stress] csv ok", flush=True)

        # Concurrencia con varios managers: writes + reads.
        errors = []
        def writer(t):
            local = DBManager(str(db_path))
            for i in range(12):
                if not local.agregar_empresa(f"Thread {t} Empresa {i}", "", "", "", "", "AR", ""):
                    errors.append(("write", t, i))
                if i % 6 == 0:
                    local.count("empresas")
        def reader(t):
            local = DBManager(str(db_path))
            for _ in range(15):
                local.count("empresas")
                local.fetchall("SELECT id, nombre FROM empresas LIMIT 20")
        threads = [threading.Thread(target=writer, args=(t,)) for t in range(3)] + [threading.Thread(target=reader, args=(t,)) for t in range(2)]
        for th in threads: th.start()
        for th in threads: th.join(timeout=30)
        alive = [th.name for th in threads if th.is_alive()]
        assert_true(not alive, f"threads colgados: {alive}")
        assert_true(not errors, f"errores concurrencia: {errors[:5]}")
        assert_true(db.count("empresas") >= 37, "faltan empresas tras stress")
        print("[stress] concurrencia ok", flush=True)

        # Merge con tags compartidos.
        assert_true(db.agregar_empresa("Merge A", "", "", "", "", "AR", "x, y"), "merge a")
        assert_true(db.agregar_empresa("Merge B", "", "", "", "", "AR", "y, z"), "merge b")
        a = db.obtener_empresa_por_nombre("Merge A")["id"]
        b = db.obtener_empresa_por_nombre("Merge B")["id"]
        assert_true(db.unificar_empresas(a, b), "merge falló")
        tags = set(db.get_tags_de_empresa(b))
        assert_true({"x", "y", "z"} <= tags, f"tags merge incompletos: {tags}")
        print("[stress] merge ok", flush=True)

        # Backup/restauración y export vacío/no vacío.
        assert_true(BackupManager.hacer_backup(str(db_path)), "backup falló")
        xlsx = tmp / "export.xlsx"
        csv_out = tmp / "export.csv"
        datos = db.get_all_empresas_with_cotizaciones()
        assert_true(Exportador.a_csv(datos, str(csv_out)), "export csv falló")
        assert_true(csv_out.exists(), "csv no existe")
        print("[stress] backup/export ok", flush=True)

        print("PASS pruebas_stress v25 web: schema, migración, CRUD, IA, CSV, concurrencia, merge, backup/export")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
