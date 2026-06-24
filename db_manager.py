import os
import sqlite3
import json
import logging
import threading
from typing import List, Dict, Tuple, Optional

from utils.normalizacion import normalizar_alias_empresa
from utils.excepciones import (
    AliasValidationError,
    EmpresaNotFoundError,
    AliasConflictError,
)
try:
    from rapidfuzz import fuzz
except Exception:
    from difflib import SequenceMatcher
    class _FuzzFallback:
        @staticmethod
        def ratio(a, b):
            return int(SequenceMatcher(None, str(a or '').lower(),
                                       str(b or '').lower()).ratio() * 100)
    fuzz = _FuzzFallback()

# Process-wide lock registry: serializes writers across ALL DBManager instances
# pointing to the same file. Required for 3-user scenario where each user
# creates their own DBManager() but targets the same .db file.
_GLOBAL_WRITE_LOCKS: dict = {}
_GLOBAL_WRITE_LOCKS_GUARD = threading.Lock()


def _global_write_lock_for(db_name: str):
    """Returns a shared RLock for the given db path (or a fresh one for :memory:)."""
    if db_name == ":memory:":
        return threading.RLock()   # each :memory: DB is independent
    key = os.path.abspath(db_name)
    with _GLOBAL_WRITE_LOCKS_GUARD:
        if key not in _GLOBAL_WRITE_LOCKS:
            _GLOBAL_WRITE_LOCKS[key] = threading.RLock()
        return _GLOBAL_WRITE_LOCKS[key]
from datetime import datetime, timedelta

class _MemConn:
    """
    Wrapper para conexión :memory: que no se cierra al salir del context manager.

    Thread-safety: SQLite's :memory: connection is not thread-safe even with
    check_same_thread=False (the flag only disables the check, doesn't add safety).
    We add an RLock so concurrent threads serialize access to the single connection.
    RLock (re-entrant) allows the same thread to acquire it multiple times,
    which happens when methods call other methods internally.
    """
    def __init__(self, conn):
        self._conn = conn
        self._lock = __import__("threading").RLock()

    def __enter__(self):
        self._lock.acquire()
        return self._conn

    def __exit__(self, *args):
        try:
            self._conn.commit()   # commit pero NO close
        finally:
            self._lock.release()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class DBManager:
    """
    Gestor centralizado de la base de datos.

    Thread-safety:
    - WAL journal mode: múltiples readers concurrentes sin bloqueos.
    - _write_lock: serializa escrituras desde distintos threads (incluyendo
      background threads del resumidor). No aplica a reads: no hay overhead
      en el camino más frecuente.
    - Cada llamada a _get_connection() abre su propia conexión para DBs en
      archivo (SQLite soporta múltiples conexiones con WAL). Para :memory:
      se reutiliza una única conexión con check_same_thread=False.
    - lastrowid eliminado: antipatrón de estado compartido. Usar fetchone
      post-INSERT para obtener el id.
    """
    def __init__(self, db_name):
        self.db_name    = db_name
        # Shared lock across all DBManager instances for this db path
        self._write_lock = _global_write_lock_for(db_name)
        with self._write_lock:
            self._crear_tablas()
            self._crear_indices()
            self._enable_wal()
        
    def _get_connection(self):
        """Crea y devuelve una conexión con row_factory configurado.
        Para :memory:, reutiliza una única conexión persistente; el context manager
        no la cierra porque sobreescribimos __exit__ via _MemConn.
        """
        if self.db_name == ":memory:":
            if not hasattr(self, '_mem_conn') or self._mem_conn is None:
                raw = sqlite3.connect(":memory:", check_same_thread=False)
                raw.row_factory = sqlite3.Row
                raw.execute("PRAGMA foreign_keys = ON")
                self._mem_conn = _MemConn(raw)
            return self._mem_conn
        conn = sqlite3.connect(self.db_name, check_same_thread=False,
                               timeout=10)       # 10s timeout antes de "locked"
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout=5000") # ms de espera en contención
        return conn

    def _enable_wal(self):
        """
        Activa WAL (Write-Ahead Logging) para la DB en archivo.
        WAL permite lecturas concurrentes mientras hay un writer activo,
        eliminando los "database is locked" en escenarios de 2-3 usuarios.
        No aplica a :memory: (no tiene sentido).
        """
        if self.db_name == ":memory:":
            return
        try:
            conn = sqlite3.connect(self.db_name)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")   # safe + faster with WAL
            conn.execute("PRAGMA busy_timeout=5000")    # wait up to 5s on lock
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"No se pudo activar WAL: {e}")

    def _crear_tablas(self):
        """Crea las tablas necesarias y realiza migraciones de esquema si es necesario."""
        try:
            with self._get_connection() as conn:
                c = conn.cursor()
                tablas = [
                    '''CREATE TABLE IF NOT EXISTS empresas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        nombre TEXT NOT NULL UNIQUE,
                        direccion TEXT,
                        telefono TEXT,
                        email TEXT,
                        rubro TEXT,
                        pais TEXT)''',
                    '''CREATE TABLE IF NOT EXISTS contactos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id INTEGER,
                        nombre TEXT,
                        telefono TEXT,
                        email TEXT,
                        pais TEXT,
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE CASCADE)''',
                    '''CREATE TABLE IF NOT EXISTS cotizaciones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id INTEGER,
                        fecha TEXT,
                        descripcion TEXT,
                        monto REAL,
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE CASCADE)''',
                    '''CREATE TABLE IF NOT EXISTS oportunidades (
                        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id            INTEGER NOT NULL,
                        titulo                TEXT    NOT NULL,
                        descripcion           TEXT,
                        etapa                 TEXT    NOT NULL DEFAULT 'prospecto',
                        monto_estimado        REAL,
                        moneda                TEXT    DEFAULT 'ARS',
                        fecha_estimada_cierre TEXT,
                        fecha_creacion        TEXT    NOT NULL,
                        fecha_ultimo_cambio   TEXT    NOT NULL,
                        notas                 TEXT,
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id)
                        ON DELETE CASCADE)''',
                    '''CREATE TABLE IF NOT EXISTS actividades (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id  INTEGER NOT NULL,
                        fecha       TEXT NOT NULL,
                        tipo        TEXT,
                        texto       TEXT,
                        usuario     TEXT DEFAULT 'usuario',
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE CASCADE)''',
                    '''CREATE TABLE IF NOT EXISTS cambios (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id INTEGER,
                        campo TEXT,
                        valor_anterior TEXT,
                        valor_nuevo TEXT,
                        fecha TEXT,
                        fuente TEXT,
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE CASCADE)''',
                    '''CREATE TABLE IF NOT EXISTS tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tag TEXT UNIQUE NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS empresa_tags (
                        empresa_id INTEGER,
                        tag_id INTEGER,
                        PRIMARY KEY (empresa_id, tag_id),
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE CASCADE,
                        FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE)''',
                    '''CREATE TABLE IF NOT EXISTS import_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        origen TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        estado TEXT NOT NULL DEFAULT 'preview',
                        total_items INTEGER DEFAULT 0,
                        creados INTEGER DEFAULT 0,
                        actualizados INTEGER DEFAULT 0,
                        omitidos INTEGER DEFAULT 0,
                        errores INTEGER DEFAULT 0,
                        fecha_creacion TEXT DEFAULT CURRENT_TIMESTAMP,
                        fecha_commit TEXT,
                        metadata_json TEXT)''',
                    '''CREATE TABLE IF NOT EXISTS import_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        batch_id INTEGER NOT NULL,
                        file_path TEXT NOT NULL,
                        file_name TEXT,
                        file_hash TEXT,
                        empresa_detectada TEXT,
                        empresa_id INTEGER,
                        pais_detectado TEXT,
                        estado TEXT NOT NULL DEFAULT 'pendiente',
                        accion TEXT NOT NULL DEFAULT 'crear',
                        confianza INTEGER,
                        error TEXT,
                        metadata_json TEXT,
                        fecha_procesado TEXT,
                        UNIQUE(batch_id, file_path),
                        FOREIGN KEY (batch_id) REFERENCES import_batches (id) ON DELETE CASCADE,
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE SET NULL)''',
                    '''CREATE TABLE IF NOT EXISTS extraccion_campos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cotizacion_id INTEGER NOT NULL,
                        campo TEXT NOT NULL,
                        valor TEXT,
                        fuente TEXT NOT NULL,
                        confianza REAL,
                        estado TEXT NOT NULL DEFAULT 'pendiente_revision',
                        snapshot_texto TEXT,
                        fecha_actualizacion TEXT DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(cotizacion_id, campo),
                        FOREIGN KEY (cotizacion_id) REFERENCES cotizaciones (id) ON DELETE CASCADE)'''
                ]
                for tabla in tablas:
                    c.execute(tabla)
                c.execute("CREATE INDEX IF NOT EXISTS idx_import_items_batch ON import_items(batch_id, estado)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_import_items_hash ON import_items(file_hash)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_extraccion_campos_cot ON extraccion_campos(cotizacion_id)")
                    
                # Migración para añadir columna de país a contactos si no existe
                c.execute("PRAGMA table_info(contactos)")
                columnas = [col[1] for col in c.fetchall()]
                if 'pais' not in columnas:
                    c.execute("ALTER TABLE contactos ADD COLUMN pais TEXT")

                # Migración para añadir columna de monto a cotizaciones si no existe
                c.execute("PRAGMA table_info(cotizaciones)")
                columnas_cotizaciones = [col[1] for col in c.fetchall()]
                if 'monto' not in columnas_cotizaciones:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN monto REAL")

                # Migración para compatibilizar esquemas viejos de cotizaciones
                c.execute("PRAGMA table_info(cotizaciones)")
                info_cols = c.fetchall()
                cols = {col[1]: col for col in info_cols}

                if 'descripcion' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN descripcion TEXT")
                if 'monto' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN monto REAL")
                if 'fecha' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN fecha TEXT")
                if 'ruta_archivo' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN ruta_archivo TEXT")
                if 'nombre_archivo' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN nombre_archivo TEXT")
                if 'tipo' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN tipo TEXT")
                if 'moneda' not in cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN moneda TEXT")

                # Refrescar columnas luego de las migraciones simples.
                c.execute("PRAGMA table_info(cotizaciones)")
                info_cols = c.fetchall()
                cols = {col[1]: col for col in info_cols}

                # Si existen columnas heredadas NOT NULL (ruta_archivo / nombre_archivo), relajar el esquema.
                needs_rebuild = False
                for col_name in ('ruta_archivo', 'nombre_archivo'):
                    if col_name in cols and cols[col_name][3] == 1:
                        needs_rebuild = True

                # Reparar fechas heredadas antes de reconstruir la tabla, porque
                # algunos esquemas viejos tienen fecha_modificacion y luego esa columna se descarta.
                if 'fecha' in cols and 'fecha_modificacion' in cols:
                    c.execute("""
                        UPDATE cotizaciones
                        SET fecha = substr(fecha_modificacion, 1, 10)
                        WHERE (fecha IS NULL OR fecha = '' OR fecha LIKE '1970-%')
                          AND fecha_modificacion IS NOT NULL
                          AND fecha_modificacion != ''
                          AND fecha_modificacion NOT LIKE '1970-%'
                    """)

                if needs_rebuild:
                    c.execute("ALTER TABLE cotizaciones RENAME TO cotizaciones_old")
                    c.execute('''CREATE TABLE cotizaciones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id INTEGER,
                        fecha TEXT,
                        descripcion TEXT,
                        monto REAL,
                        moneda TEXT,
                        ruta_archivo TEXT,
                        nombre_archivo TEXT,
                        tipo TEXT,
                        resumen TEXT,
                        proveedor_ia TEXT,
                        estado_ia TEXT,
                        error_ia TEXT,
                        archivo_hash TEXT,
                        fecha_importacion TEXT,
                        FOREIGN KEY (empresa_id) REFERENCES empresas (id) ON DELETE CASCADE)''')
                    old_columns = [col[1] for col in c.execute("PRAGMA table_info(cotizaciones_old)").fetchall()]
                    def expr(name, fallback='NULL'):
                        return name if name in old_columns else fallback
                    c.execute(f'''
                        INSERT INTO cotizaciones (id, empresa_id, fecha, descripcion, monto, moneda, ruta_archivo, nombre_archivo, tipo, resumen, proveedor_ia, estado_ia, error_ia, archivo_hash, fecha_importacion)
                        SELECT
                            id,
                            empresa_id,
                            {expr('fecha', "datetime('now')")},
                            {expr('descripcion', "''")},
                            {expr('monto', '0')},
                            {expr('moneda')},
                            {expr('ruta_archivo')},
                            {expr('nombre_archivo')},
                            {expr('tipo')},
                            {expr('resumen')},
                            {expr('proveedor_ia')},
                            {expr('estado_ia', "CASE WHEN ruta_archivo IS NULL OR ruta_archivo='' THEN 'sin_archivo' ELSE 'pendiente' END")},
                            {expr('error_ia')},
                            {expr('archivo_hash')},
                            {expr('fecha_importacion')}
                        FROM cotizaciones_old
                    ''')
                    c.execute("DROP TABLE cotizaciones_old")

                # Leer columnas actuales (después de posible rebuild)
                c.execute("PRAGMA table_info(cotizaciones)")
                final_cols = [col[1] for col in c.fetchall()]

                # Agregar columnas nuevas si no existen
                if 'moneda' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN moneda TEXT")
                if 'resumen' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN resumen TEXT")
                if 'proveedor_ia' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN proveedor_ia TEXT")
                if 'estado_ia' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN estado_ia TEXT DEFAULT 'sin_archivo'")
                if 'error_ia' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN error_ia TEXT")
                if 'archivo_hash' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN archivo_hash TEXT")
                if 'fecha_importacion' not in final_cols:
                    c.execute("ALTER TABLE cotizaciones ADD COLUMN fecha_importacion TEXT")
                c.execute("CREATE INDEX IF NOT EXISTS idx_cotizaciones_archivo_hash ON cotizaciones(archivo_hash)")
                c.execute("UPDATE cotizaciones SET estado_ia='sin_archivo' WHERE (estado_ia IS NULL OR estado_ia='') AND (ruta_archivo IS NULL OR ruta_archivo='')")
                c.execute("UPDATE cotizaciones SET estado_ia='pendiente' WHERE (estado_ia IS NULL OR estado_ia='') AND ruta_archivo IS NOT NULL AND ruta_archivo!=''")

                # Reparar fechas heredadas 1970-01-01 usando fecha_modificacion si existe.
                if 'fecha' in final_cols and 'fecha_modificacion' in final_cols:
                    c.execute("""
                        UPDATE cotizaciones
                        SET fecha = substr(fecha_modificacion, 1, 10)
                        WHERE (fecha IS NULL OR fecha = '' OR fecha LIKE '1970-%')
                          AND fecha_modificacion IS NOT NULL
                          AND fecha_modificacion != ''
                          AND fecha_modificacion NOT LIKE '1970-%'
                    """)

                conn.commit()
            self._ensure_tags_schema()
            self._ensure_empresa_aliases_schema()
        except Exception as e:
            logging.error(f"Error al crear tablas: {e}")

    def _crear_indices(self):
        """Crea índices para optimizar las búsquedas."""
        try:
            with self._get_connection() as conn:
                c = conn.cursor()
                indices = [
                    "CREATE INDEX IF NOT EXISTS idx_empresas_nombre ON empresas (nombre)",
                    "CREATE INDEX IF NOT EXISTS idx_contactos_empresa_id ON contactos (empresa_id)",
                    "CREATE INDEX IF NOT EXISTS idx_oportunidades_empresa_id ON oportunidades (empresa_id)",
                    "CREATE INDEX IF NOT EXISTS idx_oportunidades_etapa ON oportunidades (etapa)",
                    "CREATE INDEX IF NOT EXISTS idx_oportunidades_fecha ON oportunidades (fecha_ultimo_cambio DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_actividades_empresa_fecha ON actividades (empresa_id, fecha DESC, id DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_actividades_fecha ON actividades (fecha DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_cambios_empresa_id ON cambios (empresa_id)",
                    "CREATE INDEX IF NOT EXISTS idx_cambios_fecha ON cambios (fecha DESC)",
                    "CREATE INDEX IF NOT EXISTS idx_cotizaciones_empresa_id ON cotizaciones (empresa_id)"
                ]
                for indice in indices:
                    c.execute(indice)
                conn.commit()
        except Exception as e:
            logging.error(f"Error al crear índices: {e}")
            
    def ejecutar(self, query: str, params: tuple = ()) -> bool:
        """
        Ejecuta una consulta DML (INSERT/UPDATE/DELETE).
        Thread-safe: adquiere _write_lock antes de escribir.
        No expone lastrowid — usá fetchone post-INSERT si necesitás el id.
        """
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    conn.execute(query, params)
                    conn.commit()
                return True
            except Exception as e:
                logging.error(f"Error al ejecutar la consulta: {e}")
                return False

    def fetchall(self, query: str, params: tuple = ()) -> List[Dict]:
        """Ejecuta una consulta y devuelve todos los resultados como una lista de diccionarios."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logging.error(f"Error al obtener todos los resultados: {e}")
            return []

    def fetchone(self, query: str, params: tuple = ()) -> Optional[Dict]:
        """Ejecuta una consulta y devuelve el primer resultado como un diccionario."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(query, params)
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logging.error(f"Error al obtener un solo resultado: {e}")
            return None


    def _tags_value_column(self) -> str:
        """Devuelve el nombre real de la columna de valor en la tabla tags."""
        try:
            with self._get_connection() as conn:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(tags)").fetchall()]
            if 'tag' in cols:
                return 'tag'
            if 'nombre' in cols:
                return 'nombre'
        except Exception:
            pass
        return 'tag'

    def _ensure_tags_schema(self):
        """Normaliza la tabla tags para soportar esquemas viejos con columna nombre."""
        try:
            with self._get_connection() as conn:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(tags)").fetchall()]
                if cols and 'tag' not in cols and 'nombre' in cols:
                    conn.execute("ALTER TABLE tags RENAME TO tags_old")
                    conn.execute('''CREATE TABLE tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tag TEXT UNIQUE NOT NULL)''')
                    conn.execute("INSERT INTO tags (id, tag) SELECT id, nombre FROM tags_old")
                    conn.execute("DROP TABLE tags_old")
                    conn.commit()
        except Exception as e:
            logging.error(f"Error al normalizar tabla tags: {e}")

    def _ensure_empresa_aliases_schema(self):
        """Crea tabla empresa_aliases e índices si no existen."""
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS empresa_aliases (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        empresa_id INTEGER NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
                        alias TEXT NOT NULL,
                        alias_norm TEXT NOT NULL UNIQUE,
                        origen TEXT DEFAULT 'manual',
                        confianza REAL DEFAULT 1.0,
                        fecha_creacion TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_empresa_aliases_empresa_id
                    ON empresa_aliases(empresa_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_empresa_aliases_alias_norm
                    ON empresa_aliases(alias_norm)
                """)
                conn.commit()
        except Exception as e:
            logging.error(f"Error al crear tabla empresa_aliases: {e}")

    def agregar_alias_empresa(
        self,
        empresa_id: int,
        alias: str,
        origen: str = "manual",
        confianza: float = 1.0,
    ) -> dict:
        norm = normalizar_alias_empresa(alias)
        if not norm:
            raise AliasValidationError("El alias no puede quedar vacío después de normalizarse.")

        with self._get_connection() as conn:
            emp = conn.execute(
                "SELECT id FROM empresas WHERE id = ?",
                (empresa_id,),
            ).fetchone()

            if not emp:
                raise EmpresaNotFoundError(f"La empresa con ID {empresa_id} no existe.")

            existente = conn.execute("""
                SELECT id, empresa_id
                FROM empresa_aliases
                WHERE alias_norm = ?
            """, (norm,)).fetchone()

            if existente:
                if existente["empresa_id"] == empresa_id:
                    return {
                        "status": "success",
                        "action": "existing",
                        "id": existente["id"],
                    }
                raise AliasConflictError(
                    f"El alias '{alias}' ya está asignado a otra empresa "
                    f"(ID: {existente['empresa_id']})."
                )

            conn.execute("""
                INSERT INTO empresa_aliases
                    (empresa_id, alias, alias_norm, origen, confianza)
                VALUES (?, ?, ?, ?, ?)
            """, (empresa_id, alias, norm, origen, confianza))
            conn.commit()

            row = conn.execute(
                "SELECT id FROM empresa_aliases WHERE alias_norm = ?", (norm,)
            ).fetchone()

            return {
                "status": "success",
                "action": "created",
                "id": row["id"],
            }

    def buscar_empresa_por_alias_norm(self, alias_norm: str) -> dict | None:
        if not alias_norm:
            return None
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT e.*
                FROM empresas e
                JOIN empresa_aliases a ON e.id = a.empresa_id
                WHERE a.alias_norm = ?
            """, (alias_norm,)).fetchone()
            return dict(row) if row else None

    def buscar_empresa_por_alias(self, alias: str) -> dict | None:
        norm = normalizar_alias_empresa(alias)
        return self.buscar_empresa_por_alias_norm(norm)

    def get_aliases_empresa(self, empresa_id: int) -> list:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT id, alias, alias_norm, origen, confianza, fecha_creacion
                FROM empresa_aliases
                WHERE empresa_id = ?
                ORDER BY fecha_creacion DESC, id DESC
            """, (empresa_id,)).fetchall()
            return [dict(r) for r in rows]

    def eliminar_alias_empresa(self, alias_id: int, empresa_id: int | None = None) -> bool:
        with self._get_connection() as conn:
            if empresa_id is not None:
                cursor = conn.execute("""
                    DELETE FROM empresa_aliases
                    WHERE id = ? AND empresa_id = ?
                """, (alias_id, empresa_id))
            else:
                cursor = conn.execute("""
                    DELETE FROM empresa_aliases
                    WHERE id = ?
                """, (alias_id,))
            conn.commit()
            return cursor.rowcount > 0

    def migrar_aliases_empresa(self, origen_id: int, destino_id: int) -> dict:
        """
        Migra aliases de empresa origen a empresa destino.
        En DB sana con alias_norm UNIQUE, lo normal es UPDATE directo.
        La lógica de conflictos es defensiva para estados legacy/corruptos.
        """
        reporte = {
            "migrados": 0,
            "existentes_destino": 0,
            "conflictos": 0,
        }

        if origen_id == destino_id:
            return reporte

        with self._get_connection() as conn:
            aliases_origen = conn.execute("""
                SELECT id, alias_norm
                FROM empresa_aliases
                WHERE empresa_id = ?
            """, (origen_id,)).fetchall()

            for item in aliases_origen:
                alias_id = item["id"]
                alias_norm = item["alias_norm"]

                colision = conn.execute("""
                    SELECT id, empresa_id
                    FROM empresa_aliases
                    WHERE alias_norm = ?
                      AND empresa_id != ?
                """, (alias_norm, origen_id)).fetchone()

                if colision:
                    if colision["empresa_id"] == destino_id:
                        reporte["existentes_destino"] += 1
                        conn.execute(
                            "DELETE FROM empresa_aliases WHERE id = ?",
                            (alias_id,),
                        )
                    else:
                        reporte["conflictos"] += 1
                    continue

                conn.execute("""
                    UPDATE empresa_aliases
                    SET empresa_id = ?
                    WHERE id = ?
                """, (destino_id, alias_id))

                reporte["migrados"] += 1

            conn.commit()

        return reporte

    def get_filtered_empresas(self, search_term: str, filtros: Dict = None) -> List[Dict]:
        filtros = filtros or {}
        """
        Obtiene una lista de empresas filtradas por un término de búsqueda y otros criterios.
        """
        query_parts = ["SELECT * FROM empresas"]
        where_clauses = []
        params = []
        
        # Filtro por término de búsqueda (nombre, email, etc.)
        if search_term:
            where_clauses.append("(nombre LIKE ? OR email LIKE ? OR rubro LIKE ? OR pais LIKE ?)")
            search_param = f"%{search_term}%"
            params.extend([search_param] * 4)

        # Filtro de contactos
        if filtros.get('contactos_cond') == 'con':
            where_clauses.append("EXISTS (SELECT 1 FROM contactos c WHERE c.empresa_id = empresas.id)")
        elif filtros.get('contactos_cond') == 'sin':
            where_clauses.append("NOT EXISTS (SELECT 1 FROM contactos c WHERE c.empresa_id = empresas.id)")

        # Filtro de cotizaciones
        if filtros.get('cotizaciones_cond') == 'con':
            where_clauses.append("EXISTS (SELECT 1 FROM cotizaciones c WHERE c.empresa_id = empresas.id)")
        elif filtros.get('cotizaciones_cond') == 'sin':
            where_clauses.append("NOT EXISTS (SELECT 1 FROM cotizaciones c WHERE c.empresa_id = empresas.id)")
            
        # Filtro de días desde la última cotización
        dias = filtros.get('dias_cotizacion', 0)
        if dias > 0:
            fecha_limite = datetime.now() - timedelta(days=dias)
            where_clauses.append("""
                id IN (
                    SELECT empresa_id FROM cotizaciones
                    GROUP BY empresa_id
                    HAVING MAX(fecha) < ?
                )
            """)
            params.append(fecha_limite.strftime('%Y-%m-%d %H:%M:%S'))

        # Filtro por país
        if filtros.get("pais"):
            where_clauses.append("pais = ?")
            params.append(filtros["pais"])

        # Filtro por rubro
        if filtros.get("rubro"):
            where_clauses.append("rubro = ?")
            params.append(filtros["rubro"])

        # Filtro por actividad reciente
        dias_actividad = int(filtros.get("dias_actividad") or 0)
        if dias_actividad > 0:
            fecha_lim = (datetime.now() - timedelta(days=dias_actividad)
                         ).strftime("%Y-%m-%d %H:%M:%S")
            where_clauses.append(
                "NOT EXISTS (SELECT 1 FROM actividades a "
                "WHERE a.empresa_id=empresas.id AND a.fecha>=?)")
            params.append(fecha_lim)

        # Filtro por tag
        if filtros.get("tag"):
            where_clauses.append("""id IN (
                SELECT et.empresa_id FROM empresa_tags et
                JOIN tags t ON et.tag_id = t.id
                WHERE t.tag = ?)""")
            params.append(filtros["tag"])

        # Unir las cláusulas WHERE si existen
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
        
        query_parts.append("ORDER BY nombre ASC")
        
        query = " ".join(query_parts)

        return self.fetchall(query, tuple(params))
        
    def get_contactos_por_empresa(self, empresa_id: int) -> List[Dict]:
        """Obtiene una lista de contactos para una empresa dada."""
        query = "SELECT * FROM contactos WHERE empresa_id = ?"
        return self.fetchall(query, (empresa_id,))
        
    def get_cotizaciones_por_empresa(self, empresa_id: int) -> List[Dict]:
        """Obtiene una lista de cotizaciones para una empresa dada."""
        query = "SELECT * FROM cotizaciones WHERE empresa_id = ? ORDER BY fecha DESC"
        return self.fetchall(query, (empresa_id,))

    def get_ultima_cotizacion(self, empresa_id: int) -> Optional[str]:
        """Obtiene la fecha de la última cotización de una empresa."""
        query = "SELECT MAX(fecha) AS ultima_fecha FROM cotizaciones WHERE empresa_id = ?"
        result = self.fetchone(query, (empresa_id,))
        return result['ultima_fecha'] if result and result['ultima_fecha'] else None

    def get_tags_de_empresa(self, empresa_id: int) -> List[str]:
        """Obtiene los tags de una empresa por su ID."""
        col = self._tags_value_column()
        query = f"""
            SELECT t.{col} AS tag FROM tags t
            JOIN empresa_tags et ON t.id = et.tag_id
            WHERE et.empresa_id = ?
        """
        return [row['tag'] for row in self.fetchall(query, (empresa_id,))]

    def vincular_empresa_con_tags(self, empresa_id: int, tags):
        """Vincula una empresa a una lista de tags, creando los tags si no existen."""
        if tags is None:
            tags = []
        self._ensure_tags_schema()
        col = self._tags_value_column()
        # Use _write_lock to prevent concurrent DELETE+INSERT races on same empresa_id
        with self._write_lock:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM empresa_tags WHERE empresa_id = ?", (empresa_id,))
                for tag_name in tags:
                    tag_name = (tag_name or "").strip()
                    if not tag_name:
                        continue
                    row = conn.execute(
                        f"SELECT id FROM tags WHERE {col} = ?", (tag_name,)).fetchone()
                    if not row:
                        conn.execute(f"INSERT INTO tags ({col}) VALUES (?)", (tag_name,))
                        row = conn.execute(
                            f"SELECT id FROM tags WHERE {col} = ?", (tag_name,)).fetchone()
                    conn.execute(
                        "INSERT OR IGNORE INTO empresa_tags (empresa_id, tag_id) VALUES (?, ?)",
                        (empresa_id, row['id']))
                conn.commit()

    def obtener_empresa_por_nombre(self, nombre: str) -> Optional[Dict]:
        """Busca una empresa por su nombre y devuelve la primera coincidencia."""
        query = "SELECT * FROM empresas WHERE nombre = ? COLLATE NOCASE"
        return self.fetchone(query, (nombre,))

    def get_similar_empresas(self, umbral: int) -> List[Dict]:
        """
        Busca empresas con nombres similares.
        Pre-filtro por longitud para reducir de O(n²) a ~O(n·k):
        solo compara pares cuya diferencia de largo es <= tolerancia.
        Con 2000 empresas pasa de ~2M comparaciones a ~50k.
        """
        empresas = self.fetchall("SELECT id, nombre FROM empresas ORDER BY nombre COLLATE NOCASE")
        # Pre-computar nombres en minúsculas y longitudes
        data = [(e["id"], e["nombre"], e["nombre"].lower(), len(e["nombre"]))
                for e in empresas if e.get("nombre")]
        # Tolerancia de longitud: nombres de largo muy distinto no pueden ser similares
        # Para umbral=80, la diferencia máx de largo es ~20% del largo del más corto
        duplicados = []
        for i in range(len(data)):
            id1, nom1, low1, len1 = data[i]
            for j in range(i + 1, len(data)):
                id2, nom2, low2, len2 = data[j]
                # Pre-filtro: si la diferencia de largo > 40% del promedio, skip
                avg = (len1 + len2) / 2
                if avg > 0 and abs(len1 - len2) / avg > 0.5:
                    continue
                # Pre-filtro: si los primeros 2 chars no coinciden en absoluto
                # (heurística rápida para nombres muy distintos)
                if len1 >= 2 and len2 >= 2 and low1[0] != low2[0]:
                    # Solo hacer el fuzzy si el umbral es bajo (< 70)
                    if umbral >= 70:
                        continue
                similitud = int(round(fuzz.ratio(low1, low2)))
                if similitud >= umbral:
                    duplicados.append({
                        "id1": id1, "nombre1": nom1,
                        "id2": id2, "nombre2": nom2,
                        "similitud": similitud,
                    })
        # Ordenar de más a menos similar
        duplicados.sort(key=lambda x: x["similitud"], reverse=True)
        return duplicados
        
    def unificar_empresas(self, id_origen: int, id_destino: int) -> bool:
        """Unifica dos empresas, moviendo contactos y cotizaciones, y eliminando la de origen."""
        try:
            with self._get_connection() as conn:
                conn.execute("UPDATE contactos SET empresa_id = ? WHERE empresa_id = ?", (id_destino, id_origen))
                conn.execute("UPDATE cotizaciones SET empresa_id = ? WHERE empresa_id = ?", (id_destino, id_origen))
                conn.execute("""
                    INSERT OR IGNORE INTO empresa_tags (empresa_id, tag_id)
                    SELECT ?, tag_id FROM empresa_tags WHERE empresa_id = ?
                """, (id_destino, id_origen))
                conn.execute("DELETE FROM empresa_tags WHERE empresa_id = ?", (id_origen,))
                conn.execute("DELETE FROM empresas WHERE id = ?",(id_origen,))
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error al unificar empresas: {e}")
            return False

    def obtener_empresa_por_id(self, empresa_id):
        """Obtiene los detalles de una empresa por su ID."""
        query = "SELECT * FROM empresas WHERE id = ?"
        return self.fetchone(query, (empresa_id,))

    def agregar_contacto(self, empresa_id, nombre, email, telefono, pais):
        """Agrega un nuevo contacto a la base de datos."""
        query = "INSERT INTO contactos (empresa_id, nombre, email, telefono, pais) VALUES (?, ?, ?, ?, ?)"
        return self.ejecutar(query, (empresa_id, nombre, email, telefono, pais))

    def editar_contacto(self, contacto_id, nombre, email, telefono, pais):
        """Edita un contacto existente."""
        query = "UPDATE contactos SET nombre = ?, email = ?, telefono = ?, pais = ? WHERE id = ?"
        return self.ejecutar(query, (nombre, email, telefono, pais, contacto_id))

    def eliminar_contacto(self, contacto_id):
        """Elimina un contacto."""
        query = "DELETE FROM contactos WHERE id = ?"
        return self.ejecutar(query, (contacto_id,))
        
    def agregar_empresa(self, nombre, direccion, telefono, email, rubro, pais, tags):
        """Agrega una nueva empresa a la base de datos y sus tags."""
        query = "INSERT INTO empresas (nombre, direccion, telefono, email, rubro, pais) VALUES (?, ?, ?, ?, ?, ?)"
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (nombre, direccion, telefono, email, rubro, pais))
                empresa_id = cursor.lastrowid
                self.lastrowid = empresa_id
                conn.commit()
            if empresa_id:
                if tags:
                    self.vincular_empresa_con_tags(empresa_id, [t.strip() for t in tags.split(',') if t.strip()])
                return True
            return False
        except Exception as e:
            logging.error(f"Error al agregar empresa: {e}")
            return False

    def editar_empresa(self, empresa_id, nombre, direccion, telefono,
                       email, rubro, pais, tags, fuente: str = "usuario"):
        """Edita empresa y registra en historial los campos que cambiaron."""
        # Capturar estado anterior para el historial
        anterior = self.obtener_empresa_por_id(empresa_id)
        query = "UPDATE empresas SET nombre=?, direccion=?, telefono=?, email=?, rubro=?, pais=? WHERE id=?"
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(query, (nombre, direccion, telefono, email, rubro, pais, empresa_id))
                if cursor.rowcount == 0:
                    return False
                conn.commit()
            if tags is not None:
                self.vincular_empresa_con_tags(empresa_id, [t.strip() for t in tags.split(',') if t.strip()])
            # Registrar en historial los campos que cambiaron
            if anterior:
                for campo, viejo, nuevo in [
                    ("nombre",    anterior.get("nombre")    or "", nombre    or ""),
                    ("direccion", anterior.get("direccion") or "", direccion or ""),
                    ("telefono",  anterior.get("telefono")  or "", telefono  or ""),
                    ("email",     anterior.get("email")     or "", email     or ""),
                    ("rubro",     anterior.get("rubro")     or "", rubro     or ""),
                    ("pais",      anterior.get("pais")      or "", pais      or ""),
                ]:
                    if str(viejo) != str(nuevo):
                        self.registrar_cambio(empresa_id, campo, viejo, nuevo, fuente)
            return True
        except Exception as e:
            logging.error(f"Error al editar empresa: {e}")
            return False


    def eliminar_empresa(self, empresa_id):
        """Elimina una empresa y sus datos relacionados."""
        query = "DELETE FROM empresas WHERE id=?"
        return self.ejecutar(query, (empresa_id,))

    def count(self, table_name: str) -> int:
        """Obtiene el número de filas en una tabla."""
        try:
            with self._get_connection() as conn:
                query = f"SELECT COUNT(*) FROM {table_name}"
                return conn.execute(query).fetchone()[0]
        except Exception as e:
            logging.error(f"Error al contar registros en {table_name}: {e}")
            return 0
            
    def count_by_empresa(self, table_name: str, empresa_id: int) -> int:
        """Obtiene el número de filas para una empresa específica."""
        try:
            with self._get_connection() as conn:
                query = f"SELECT COUNT(*) FROM {table_name} WHERE empresa_id = ?"
                return conn.execute(query, (empresa_id,)).fetchone()[0]
        except Exception as e:
            logging.error(f"Error al contar registros en {table_name} para empresa {empresa_id}: {e}")
            return 0

    def get_cotizacion_por_id(self, cotizacion_id: int) -> Optional[Dict]:
        """Obtiene los detalles de una cotización por su ID."""
        query = "SELECT * FROM cotizaciones WHERE id = ?"
        return self.fetchone(query, (cotizacion_id,))

    def agregar_cotizacion(self, empresa_id: int, descripcion: str, monto: float,
                           fecha: str = None) -> bool:
        """Agrega una nueva cotización a la base de datos, adaptándose al esquema disponible."""
        if not fecha:
            fecha = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._get_connection() as conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(cotizaciones)").fetchall()]
                tipo = None
                if isinstance(descripcion, str) and descripcion.startswith("[") and "]" in descripcion:
                    tipo = descripcion[1:descripcion.index("]")]
                data = {
                    'empresa_id': empresa_id,
                    'fecha': fecha,
                    'descripcion': descripcion,
                    'monto': monto,
                    'moneda': None,
                    'tipo': tipo,
                    'estado_ia': 'sin_archivo',
                    'error_ia': None,
                }
                if 'ruta_archivo' in columns:
                    data['ruta_archivo'] = None
                if 'nombre_archivo' in columns:
                    data['nombre_archivo'] = None

                insert_cols = [col for col in columns if col in data]
                placeholders = ", ".join(["?"] * len(insert_cols))
                col_sql = ", ".join(insert_cols)
                values = tuple(data[col] for col in insert_cols)
                conn.execute(f"INSERT INTO cotizaciones ({col_sql}) VALUES ({placeholders})", values)
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error al agregar cotización: {e}")
            return False
    

    def agregar_cotizacion_con_ruta(self, empresa_id: int, descripcion: str,
                                    monto: float, fecha: str, ruta: str,
                                    resumen: str = None, archivo_hash: str = None) -> bool:
        """Como agregar_cotizacion pero guarda la ruta del archivo."""
        if not fecha:
            fecha = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._get_connection() as conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(cotizaciones)").fetchall()]
                tipo = None
                desc_value = descripcion
                if isinstance(descripcion, str) and descripcion.startswith("[") and "]" in descripcion:
                    tipo = descripcion[1:descripcion.index("]")]
                    desc_value = descripcion[descripcion.index("]") + 1:].strip()
                data = {'empresa_id': empresa_id, 'fecha': fecha,
                        'descripcion': desc_value, 'monto': monto, 'moneda': None, 'tipo': tipo,
                        'estado_ia': 'pendiente', 'error_ia': None,
                        'archivo_hash': archivo_hash,
                        'fecha_importacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                if 'ruta_archivo' in columns:
                    data['ruta_archivo'] = ruta
                if 'nombre_archivo' in columns:
                    data['nombre_archivo'] = os.path.basename(ruta) if ruta else None
                insert_cols = [col for col in columns if col in data]
                placeholders = ", ".join(["?"] * len(insert_cols))
                col_sql = ", ".join(insert_cols)
                values = tuple(data[col] for col in insert_cols)
                conn.execute(f"INSERT INTO cotizaciones ({col_sql}) VALUES ({placeholders})", values)
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error al agregar cotización con ruta: {e}")
            return False

    # ── Sprint D: staging de importación (import_batches / import_items) ──────

    def crear_import_batch(self, origen: str, root_path: str, metadata: dict = None) -> int | None:
        """Crea un batch de importación en estado 'preview'. Devuelve el id."""
        try:
            with self._get_connection() as conn:
                cur = conn.execute(
                    "INSERT INTO import_batches (origen, root_path, metadata_json) VALUES (?, ?, ?)",
                    (origen, root_path, json.dumps(metadata) if metadata else None))
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logging.error(f"Error al crear import_batch: {e}")
            return None

    def agregar_import_item(self, batch_id: int, file_path: str, file_name: str,
                            file_hash: str = None, empresa_detectada: str = None,
                            empresa_id: int = None, pais_detectado: str = None,
                            estado: str = "pendiente", accion: str = "crear",
                            confianza: int = None, error: str = None,
                            metadata: dict = None) -> int | None:
        """Agrega un item al batch. UNIQUE(batch_id, file_path) evita
        duplicados si se re-escanea el mismo batch (no debería pasar, pero
        protege ante doble-click en 'Escanear')."""
        try:
            with self._get_connection() as conn:
                cur = conn.execute("""
                    INSERT INTO import_items
                        (batch_id, file_path, file_name, file_hash, empresa_detectada,
                         empresa_id, pais_detectado, estado, accion, confianza, error, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(batch_id, file_path) DO NOTHING
                """, (batch_id, file_path, file_name, file_hash, empresa_detectada,
                      empresa_id, pais_detectado, estado, accion, confianza, error,
                      json.dumps(metadata) if metadata else None))
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logging.error(f"Error al agregar import_item: {e}")
            return None

    def obtener_import_batch(self, batch_id: int) -> dict | None:
        row = self.fetchone("SELECT * FROM import_batches WHERE id=?", (batch_id,))
        return dict(row) if row else None

    def listar_import_items(self, batch_id: int, estado: str = None,
                            offset: int = 0, limit: int = 200) -> list:
        if estado:
            return self.fetchall(
                "SELECT * FROM import_items WHERE batch_id=? AND estado=? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (batch_id, estado, limit, offset))
        return self.fetchall(
            "SELECT * FROM import_items WHERE batch_id=? ORDER BY id LIMIT ? OFFSET ?",
            (batch_id, limit, offset))

    def contar_import_items_por_estado(self, batch_id: int) -> dict:
        rows = self.fetchall(
            "SELECT estado, COUNT(*) as n FROM import_items WHERE batch_id=? GROUP BY estado",
            (batch_id,))
        return {r["estado"]: r["n"] for r in rows}

    def obtener_import_item(self, item_id: int) -> dict | None:
        row = self.fetchone("SELECT * FROM import_items WHERE id=?", (item_id,))
        return dict(row) if row else None

    def actualizar_import_item(self, item_id: int, **campos) -> bool:
        """Actualiza columnas arbitrarias de un item (corrección manual:
        accion, empresa_id, pais_detectado, estado, error, file_hash)."""
        permitidos = {"accion", "empresa_id", "pais_detectado", "estado",
                      "error", "file_hash", "empresa_detectada", "confianza",
                      "fecha_procesado"}
        campos = {k: v for k, v in campos.items() if k in permitidos}
        if not campos:
            return False
        try:
            with self._get_connection() as conn:
                set_sql = ", ".join(f"{k}=?" for k in campos)
                conn.execute(f"UPDATE import_items SET {set_sql} WHERE id=?",
                            (*campos.values(), item_id))
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error al actualizar import_item {item_id}: {e}")
            return False

    def actualizar_import_batch(self, batch_id: int, **campos) -> bool:
        permitidos = {"estado", "total_items", "creados", "actualizados",
                      "omitidos", "errores", "fecha_commit", "metadata_json"}
        campos = {k: v for k, v in campos.items() if k in permitidos}
        if not campos:
            return False
        try:
            with self._get_connection() as conn:
                set_sql = ", ".join(f"{k}=?" for k in campos)
                conn.execute(f"UPDATE import_batches SET {set_sql} WHERE id=?",
                            (*campos.values(), batch_id))
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error al actualizar import_batch {batch_id}: {e}")
            return False

    def cotizacion_existe_por_hash(self, file_hash: str) -> bool:
        if not file_hash:
            return False
        row = self.fetchone(
            "SELECT id FROM cotizaciones WHERE archivo_hash=? LIMIT 1", (file_hash,))
        return row is not None

    # ── Sprint E: trazabilidad de extracción por campo ─────────────────────────

    def guardar_campo_extraido(self, cotizacion_id: int, campo: str, valor,
                               fuente: str, confianza: float = None,
                               estado: str = None, snapshot_texto: str = None,
                               forzar: bool = False) -> bool:
        """
        Guarda (o actualiza) el valor extraído de un campo, con su fuente
        y confianza. Si el campo ya está en estado 'manual_confirmado' y
        forzar=False, NO pisa el valor — esto es lo que protege las
        correcciones manuales de ser sobrescritas en un reprocesamiento
        automático (requisito explícito del issue de Sprint E).

        valor se guarda como TEXT (str(valor)) para soportar cualquier
        tipo de campo (número, fecha, texto) en una sola tabla genérica;
        el caller es responsable de convertir al tipo correcto al leer.
        """
        if estado is None:
            from extraccion.constants import UMBRAL_CONFIANZA_OK
            estado = "ok" if (confianza or 0) >= UMBRAL_CONFIANZA_OK else "pendiente_revision"
        try:
            with self._get_connection() as conn:
                if not forzar:
                    actual = conn.execute(
                        "SELECT estado FROM extraccion_campos WHERE cotizacion_id=? AND campo=?",
                        (cotizacion_id, campo)).fetchone()
                    if actual and actual["estado"] == "manual_confirmado":
                        return True  # no es un error, simplemente no se tocó a propósito
                conn.execute("""
                    INSERT INTO extraccion_campos
                        (cotizacion_id, campo, valor, fuente, confianza, estado, snapshot_texto, fecha_actualizacion)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(cotizacion_id, campo) DO UPDATE SET
                        valor=excluded.valor, fuente=excluded.fuente,
                        confianza=excluded.confianza, estado=excluded.estado,
                        snapshot_texto=excluded.snapshot_texto,
                        fecha_actualizacion=CURRENT_TIMESTAMP
                """, (cotizacion_id, campo, None if valor is None else str(valor),
                      fuente, confianza, estado, snapshot_texto))
                conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error al guardar campo extraído ({cotizacion_id}, {campo}): {e}")
            return False

    def corregir_campo_manual(self, cotizacion_id: int, campo: str, valor) -> bool:
        """Corrección humana: fuente='manual', estado='manual_confirmado'.
        Usa forzar=True porque una corrección manual siempre debe poder
        pisar lo que hubiera antes (incluso otra corrección manual previa)."""
        return self.guardar_campo_extraido(
            cotizacion_id, campo, valor, fuente="manual",
            confianza=1.0, estado="manual_confirmado", forzar=True)

    def obtener_campos_extraidos(self, cotizacion_id: int) -> list:
        return self.fetchall(
            "SELECT * FROM extraccion_campos WHERE cotizacion_id=? ORDER BY campo",
            (cotizacion_id,))

    def obtener_campo_extraido(self, cotizacion_id: int, campo: str) -> dict | None:
        row = self.fetchone(
            "SELECT * FROM extraccion_campos WHERE cotizacion_id=? AND campo=?",
            (cotizacion_id, campo))
        return dict(row) if row else None

    def editar_cotizacion(self, cotizacion_id: int, descripcion: str,
                          monto: float, tipo: str = None,
                          fecha: str = None) -> bool:
        """
        Edita una cotización existente.
        Si tipo está vacío pero descripcion tiene prefijo [Tipo], lo extrae.
        """
        if not cotizacion_id:
            return False
        # Normalizar tipo desde descripción si no se pasa explícitamente
        if not tipo and descripcion and descripcion.startswith("[") and "]" in descripcion:
            tipo = descripcion[1:descripcion.index("]")]
        elif tipo:
            # Asegurar que el prefijo está en la descripción
            if not descripcion.startswith(f"[{tipo}]"):
                descripcion = f"[{tipo}] {descripcion}"
        try:
            with self._get_connection() as conn:
                columns = [row[1] for row in
                           conn.execute("PRAGMA table_info(cotizaciones)").fetchall()]
                sets = ["descripcion=?", "monto=?"]
                vals = [descripcion, monto]
                if "tipo" in columns:
                    sets.append("tipo=?"); vals.append(tipo)
                if fecha and "fecha" in columns:
                    sets.append("fecha=?"); vals.append(fecha)
                vals.append(cotizacion_id)
                cursor = conn.execute(
                    f"UPDATE cotizaciones SET {', '.join(sets)} WHERE id=?",
                    tuple(vals))
                conn.commit()
                if cursor.rowcount == 0:
                    return False   # no row matched — cotización no existe
            return True
        except Exception as e:
            logging.error(f"Error al editar cotización {cotizacion_id}: {e}")
            return False

    def eliminar_cotizacion(self, cotizacion_id: int) -> bool:
        """Elimina una cotización por su ID."""
        if not cotizacion_id:
            return False
        return self.ejecutar("DELETE FROM cotizaciones WHERE id=?",
                             (cotizacion_id,))


    def actualizar_resumen_cotizacion_por_ruta(self, empresa_id: int, ruta: str,
                                               resumen: str = "",
                                               monto=None, moneda=None,
                                               proveedor_ia: str = "none",
                                               tipo: str = None) -> bool:
        """
        Actualiza campos IA de una cotización importada por ruta.
        Thread-safe: solo modifica columnas existentes para tolerar bases antiguas.
        Usado por el thread de resumen background.
        """
        if not empresa_id or not ruta:
            return False
        try:
            with self._get_connection() as conn:
                columns = [row[1] for row in
                           conn.execute("PRAGMA table_info(cotizaciones)").fetchall()]
                sets, vals = [], []
                if 'resumen' in columns:
                    sets.append('resumen=?'); vals.append(resumen or '')
                if 'proveedor_ia' in columns:
                    sets.append('proveedor_ia=?'); vals.append(proveedor_ia or 'none')
                if 'estado_ia' in columns:
                    sets.append('estado_ia=?'); vals.append('ok')
                if 'error_ia' in columns:
                    sets.append('error_ia=?'); vals.append(None)
                if 'moneda' in columns:
                    sets.append('moneda=?'); vals.append(moneda)
                if 'tipo' in columns and tipo:
                    sets.append('tipo=?'); vals.append(tipo)
                if 'monto' in columns and monto is not None:
                    try:
                        monto_f = float(monto)
                    except Exception:
                        monto_f = None
                    if monto_f is not None and monto_f > 0:
                        sets.append('monto=?'); vals.append(monto_f)
                if not sets:
                    return False
                vals.extend([empresa_id, ruta])
                cur = conn.execute(
                    f"UPDATE cotizaciones SET {', '.join(sets)} "
                    f"WHERE empresa_id=? AND ruta_archivo=?",
                    tuple(vals))
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logging.error(f"Error actualizando resumen IA: {e}")
            return False


    def set_estado_ia_cotizacion(self, cotizacion_id: int, estado: str, error: str = None) -> bool:
        """Actualiza el estado del resumen IA de una cotización."""
        if not cotizacion_id:
            return False
        try:
            with self._get_connection() as conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(cotizaciones)").fetchall()]
                sets, vals = [], []
                if 'estado_ia' in columns:
                    sets.append('estado_ia=?'); vals.append(estado)
                if 'error_ia' in columns:
                    sets.append('error_ia=?'); vals.append(error)
                if not sets:
                    return True
                vals.append(cotizacion_id)
                cur = conn.execute(f"UPDATE cotizaciones SET {', '.join(sets)} WHERE id=?", tuple(vals))
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logging.error(f"Error seteando estado IA: {e}")
            return False

    def actualizar_hash_cotizacion(self, cotizacion_id: int, archivo_hash: str) -> bool:
        if not cotizacion_id or not archivo_hash:
            return False
        try:
            with self._get_connection() as conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(cotizaciones)").fetchall()]
                if 'archivo_hash' not in columns:
                    return True
                cur = conn.execute("UPDATE cotizaciones SET archivo_hash=? WHERE id=?", (archivo_hash, cotizacion_id))
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logging.error(f"Error actualizando hash: {e}")
            return False

    def get_diagnostico_datos(self) -> dict:
        """Diagnóstico rápido para UI y pruebas."""
        def scalar(sql, params=()):
            r = self.fetchone(sql, params)
            return list(dict(r).values())[0] if r else 0
        data = {
            'empresas_sin_nombre': scalar("SELECT COUNT(*) n FROM empresas WHERE nombre IS NULL OR TRIM(nombre)=''"),
            'contactos_huerfanos': scalar("SELECT COUNT(*) n FROM contactos WHERE empresa_id IS NULL OR empresa_id NOT IN (SELECT id FROM empresas)"),
            'cotizaciones_huerfanas': scalar("SELECT COUNT(*) n FROM cotizaciones WHERE empresa_id IS NULL OR empresa_id NOT IN (SELECT id FROM empresas)"),
            'emails_contacto_invalidos': scalar("SELECT COUNT(*) n FROM contactos WHERE email IS NOT NULL AND TRIM(email)!='' AND email NOT LIKE '%_@_%._%'"),
            'cotizaciones_fecha_1970': scalar("SELECT COUNT(*) n FROM cotizaciones WHERE fecha LIKE '1970-%'"),
            'cotizaciones_sin_resumen': scalar("SELECT COUNT(*) n FROM cotizaciones WHERE ruta_archivo IS NOT NULL AND ruta_archivo!='' AND (resumen IS NULL OR TRIM(resumen)='')"),
            'ia_pendiente': scalar("SELECT COUNT(*) n FROM cotizaciones WHERE estado_ia='pendiente'"),
            'ia_procesando': scalar("SELECT COUNT(*) n FROM cotizaciones WHERE estado_ia='procesando'"),
            'ia_error': scalar("SELECT COUNT(*) n FROM cotizaciones WHERE estado_ia='error'"),
            'hashes_duplicados': scalar("SELECT COUNT(*) n FROM (SELECT archivo_hash FROM cotizaciones WHERE archivo_hash IS NOT NULL AND archivo_hash!='' GROUP BY archivo_hash HAVING COUNT(*)>1)"),
        }
        return data



    # ── Oportunidades / Pipeline ──────────────────────────────────────────────

    ETAPAS_VENTA    = {"prospecto","contactado","a_visitar","a_cotizar",
                       "cotizado","en_negociacion","ganado","perdido","muerta"}
    ETAPAS_POSVENTA = {"en_proceso","entregada","finalizada"}
    ETAPAS_TODAS    = ETAPAS_VENTA | ETAPAS_POSVENTA

    @staticmethod
    def _normalizar_etapa(etapa: str):
        e = str(etapa or "prospecto").strip().lower()
        todas = DBManager.ETAPAS_VENTA | DBManager.ETAPAS_POSVENTA
        return e if e in todas else None

    @staticmethod
    def _fase_de_etapa(etapa: str) -> str:
        e = str(etapa or "").strip().lower()
        if e in DBManager.ETAPAS_POSVENTA or e == "ganado":
            return "posventa"
        return "venta"

    def _add_fase(self, row) -> dict:
        if not row: return row
        d = dict(row)
        d["fase"] = self._fase_de_etapa(d.get("etapa",""))
        return d

    def crear_oportunidad(self, empresa_id: int, titulo: str,
                          descripcion: str = "", etapa: str = "prospecto",
                          monto_estimado=None, moneda: str = "ARS",
                          fecha_estimada_cierre: str = None,
                          notas: str = "") -> bool:
        if not empresa_id or not titulo or not str(titulo).strip():
            return False
        etapa = self._normalizar_etapa(etapa)
        if not etapa:
            return False
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    if not conn.execute(
                            "SELECT 1 FROM empresas WHERE id=?",
                            (empresa_id,)).fetchone():
                        return False
                    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "INSERT INTO oportunidades "
                        "(empresa_id,titulo,descripcion,etapa,monto_estimado,"
                        "moneda,fecha_estimada_cierre,fecha_creacion,"
                        "fecha_ultimo_cambio,notas) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (empresa_id, str(titulo).strip(),
                         str(descripcion or "").strip(), etapa,
                         monto_estimado,
                         str(moneda or "ARS").strip(),
                         fecha_estimada_cierre or None,
                         ahora, ahora,
                         str(notas or "").strip()))
                    conn.commit()
                    return True
            except Exception as e:
                logging.error(f"crear_oportunidad: {e}")
                return False

    def get_oportunidades(self, filtros: dict = None) -> List[Dict]:
        filtros = filtros or {}
        where, params = ["1=1"], []
        if filtros.get("empresa_id"):
            where.append("o.empresa_id=?"); params.append(filtros["empresa_id"])
        if filtros.get("etapa"):
            where.append("o.etapa=?"); params.append(filtros["etapa"])
        rows = self.fetchall(
            f"SELECT o.*, e.nombre empresa_nombre "
            f"FROM oportunidades o JOIN empresas e ON o.empresa_id=e.id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY o.fecha_ultimo_cambio DESC",
            tuple(params))
        return [self._add_fase(r) for r in rows]

    def get_oportunidades_empresa(self, empresa_id: int) -> List[Dict]:
        rows = self.fetchall(
            "SELECT o.*, e.nombre empresa_nombre "
            "FROM oportunidades o JOIN empresas e ON o.empresa_id=e.id "
            "WHERE o.empresa_id=? ORDER BY o.fecha_ultimo_cambio DESC",
            (empresa_id,))
        return [self._add_fase(r) for r in rows]

    def get_oportunidad_por_id(self, oid: int) -> Optional[Dict]:
        row = self.fetchone(
            "SELECT o.*, e.nombre empresa_nombre "
            "FROM oportunidades o JOIN empresas e ON o.empresa_id=e.id "
            "WHERE o.id=?", (oid,))
        return self._add_fase(row) if row else None

    def editar_oportunidad(self, oid: int, **campos) -> bool:
        if not oid: return False
        allowed = {"titulo","descripcion","etapa","monto_estimado",
                   "moneda","fecha_estimada_cierre","notas"}
        sets, vals = [], []
        for k, v in campos.items():
            if k not in allowed: continue
            if k == "etapa":
                v = self._normalizar_etapa(v)
                if not v: continue
            if k == "titulo" and not str(v or "").strip():
                continue  # reject empty titulo silently (caller checks rowcount)
            sets.append(f"{k}=?"); vals.append(v)
        if not sets: return False
        sets.append("fecha_ultimo_cambio=?")
        vals.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        vals.append(oid)
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    cur = conn.execute(
                        f"UPDATE oportunidades SET {','.join(sets)} WHERE id=?",
                        tuple(vals))
                    conn.commit()
                    return cur.rowcount > 0
            except Exception as e:
                logging.error(f"editar_oportunidad: {e}")
                return False

    def cambiar_etapa_oportunidad(self, oid: int, nueva_etapa: str) -> bool:
        etapa = self._normalizar_etapa(nueva_etapa)
        if not etapa: return False
        return self.editar_oportunidad(oid, etapa=etapa)

    def eliminar_oportunidad(self, oid: int) -> bool:
        if not oid: return False
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    cur = conn.execute(
                        "DELETE FROM oportunidades WHERE id=?", (oid,))
                    conn.commit()
                    return cur.rowcount > 0
            except Exception as e:
                logging.error(f"eliminar_oportunidad: {e}")
                return False

    # ── Actividades / Notas ───────────────────────────────────────────────────

    ACTIVIDAD_TIPOS_VALIDOS = {"nota", "llamada", "email", "reunion"}

    def _normalizar_tipo_actividad(self, tipo: str) -> str:
        tipo = (tipo or "nota").strip().lower()
        return tipo if tipo in self.ACTIVIDAD_TIPOS_VALIDOS else "nota"

    def agregar_actividad(self, empresa_id: int, tipo: str, texto: str,
                          usuario: str = "usuario") -> bool:
        if not empresa_id or not texto or not str(texto).strip():
            return False
        tipo    = self._normalizar_tipo_actividad(tipo)
        texto   = str(texto).strip()
        usuario = (usuario or "usuario").strip()[:80] or "usuario"
        fecha   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    # Verificar que la empresa existe
                    if not conn.execute(
                            "SELECT 1 FROM empresas WHERE id=?",
                            (empresa_id,)).fetchone():
                        return False
                    conn.execute(
                        "INSERT INTO actividades "
                        "(empresa_id, fecha, tipo, texto, usuario) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (empresa_id, fecha, tipo, texto, usuario))
                    conn.commit()
                    return True
            except Exception as e:
                logging.error(f"agregar_actividad: {e}")
                return False

    def get_actividades_empresa(self, empresa_id: int,
                                limit: int = 200,
                                offset: int = 0) -> List[Dict]:
        limit  = max(1, min(int(limit  or 200), 500))
        offset = max(0, int(offset or 0))
        return self.fetchall(
            "SELECT * FROM actividades WHERE empresa_id=? "
            "ORDER BY fecha DESC, id DESC LIMIT ? OFFSET ?",
            (empresa_id, limit, offset))

    def editar_actividad(self, actividad_id: int, tipo: str,
                         texto: str) -> bool:
        if not actividad_id or not texto or not str(texto).strip():
            return False
        tipo  = self._normalizar_tipo_actividad(tipo)
        texto = str(texto).strip()
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    cur = conn.execute(
                        "UPDATE actividades SET tipo=?, texto=? WHERE id=?",
                        (tipo, texto, actividad_id))
                    conn.commit()
                    return cur.rowcount > 0
            except Exception as e:
                logging.error(f"editar_actividad: {e}")
                return False

    def eliminar_actividad(self, actividad_id: int) -> bool:
        if not actividad_id:
            return False
        with self._write_lock:
            try:
                with self._get_connection() as conn:
                    cur = conn.execute(
                        "DELETE FROM actividades WHERE id=?",
                        (actividad_id,))
                    conn.commit()
                    return cur.rowcount > 0
            except Exception as e:
                logging.error(f"eliminar_actividad: {e}")
                return False

    def get_actividades_recientes(self, dias: int = 7,
                                  limit: int = 50) -> List[Dict]:
        dias  = max(1, min(int(dias  or 7),  365))
        limit = max(1, min(int(limit or 50), 200))
        desde = (datetime.now() - timedelta(days=dias)).strftime(
            "%Y-%m-%d %H:%M:%S")
        return self.fetchall(
            "SELECT a.*, e.nombre empresa_nombre "
            "FROM actividades a JOIN empresas e ON a.empresa_id=e.id "
            "WHERE a.fecha >= ? ORDER BY a.fecha DESC, a.id DESC LIMIT ?",
            (desde, limit))

    def registrar_cambio(self, empresa_id: int, campo: str,
                         valor_anterior: str, valor_nuevo: str,
                         fuente: str = "usuario") -> bool:
        """Registra un cambio en el historial."""
        if str(valor_anterior or "") == str(valor_nuevo or ""):
            return True   # sin cambio real, no registrar
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.ejecutar(
            "INSERT INTO cambios (empresa_id, campo, valor_anterior, valor_nuevo, fecha, fuente) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (empresa_id, campo, str(valor_anterior or ""),
             str(valor_nuevo or ""), fecha, fuente))

    def get_historial_empresa(self, empresa_id: int) -> List[Dict]:
        """Devuelve el historial de cambios de una empresa, más reciente primero."""
        return self.fetchall(
            "SELECT * FROM cambios WHERE empresa_id=? ORDER BY fecha DESC, id DESC, id DESC",
            (empresa_id,))

    def deshacer_ultimo_cambio(self, empresa_id: int) -> dict | None:
        """
        Devuelve el último cambio registrado para esa empresa (sin aplicarlo).
        El llamador decide si aplicar la reversión.
        """
        return self.fetchone(
            "SELECT * FROM cambios WHERE empresa_id=? ORDER BY fecha DESC, id DESC LIMIT 1",
            (empresa_id,))

    def eliminar_cambio(self, cambio_id: int) -> bool:
        return self.ejecutar("DELETE FROM cambios WHERE id=?", (cambio_id,))

    def get_all_empresas_with_cotizaciones(self) -> List[Dict]:
        """Obtiene todas las empresas y sus cotizaciones para el exportador."""
        query = """
            SELECT 
                e.id, 
                e.nombre, 
                e.pais, 
                e.rubro,
                GROUP_CONCAT(t.tag, ', ') AS tags,
                (SELECT fecha FROM cotizaciones WHERE empresa_id = e.id ORDER BY fecha DESC LIMIT 1) as ultima_cotizacion
            FROM empresas e
            LEFT JOIN empresa_tags et ON e.id = et.empresa_id
            LEFT JOIN tags t ON et.tag_id = t.id
            GROUP BY e.id
            ORDER BY e.nombre
        """
        return self.fetchall(query)