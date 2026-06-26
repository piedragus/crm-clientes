"""
importer/single_file.py — punto de entrada para importar UN solo
archivo de punta a punta (Sprint F: lo necesita el watcher de OneDrive,
que recibe eventos de archivo-por-archivo, no de carpeta completa).

Antes de este módulo, toda la lógica de "crear empresa si no existe +
insertar cotización" vivía duplicada dentro de cada endpoint de
importación masiva en server.py — no había una función reutilizable
para el caso de un solo archivo. Esta es la primera; los endpoints de
server.py NO se refactorizaron para usarla en esta tanda (cambio de
mayor riesgo, fuera de alcance de Sprint F) — queda como deuda técnica
de unificación para una tanda futura si se quiere consolidar todo en
un solo lugar.
"""
from __future__ import annotations
import hashlib
import os

from .resolver import detect_pais, get_client_name
from .constants import IMPORT_EXTS


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def importar_archivo(db, path: str, root_path: str, extensions=None) -> dict:
    """
    Importa un solo archivo de punta a punta: detecta empresa/país a
    partir de la cadena de carpetas relativa a root_path, crea la
    empresa si no existe (no la duplica si ya existe con ese nombre
    exacto), e inserta la cotización. Anti-duplicado por ruta y por hash.

    No lanza excepciones — cualquier error queda en el dict de retorno
    como {'estado': 'error', 'mensaje': ...}, para que el caller (el
    watcher, que corre en loop indefinido) nunca se caiga por un
    archivo individual problemático.

    Devuelve:
        {'estado': 'importado'|'omitido'|'error',
         'motivo': str (solo si omitido/error),
         'empresa_id': int|None, 'empresa_nombre': str|None,
         'cotizacion_id': int|None}
    """
    extensions = extensions or IMPORT_EXTS
    resultado = {"estado": "error", "motivo": None,
                "empresa_id": None, "empresa_nombre": None,
                "cotizacion_id": None}

    try:
        if not os.path.isfile(path):
            resultado["motivo"] = "Archivo no encontrado"
            return resultado

        ext = os.path.splitext(path)[1].lower()
        if ext not in extensions:
            resultado["estado"] = "omitido"
            resultado["motivo"] = f"Extensión no soportada: {ext!r}"
            return resultado

        # Anti-duplicado por ruta exacta (ya importado antes)
        existente_ruta = db.fetchone(
            "SELECT id FROM cotizaciones WHERE ruta_archivo=?", (path,))
        if existente_ruta:
            resultado["estado"] = "omitido"
            resultado["motivo"] = "Ya importado anteriormente (misma ruta)"
            resultado["cotizacion_id"] = existente_ruta["id"]
            return resultado

        file_hash = file_sha256(path)
        existente_hash = db.fetchone(
            "SELECT id FROM cotizaciones WHERE archivo_hash=?", (file_hash,))
        if existente_hash:
            resultado["estado"] = "omitido"
            resultado["motivo"] = "Duplicado por hash (mismo contenido, otra ruta)"
            resultado["cotizacion_id"] = existente_hash["id"]
            return resultado

        rel_dir = os.path.relpath(os.path.dirname(path), root_path)
        folder_chain = [] if rel_dir == "." else rel_dir.replace("\\", "/").split("/")
        stem = os.path.splitext(os.path.basename(path))[0]

        nombre_empresa = get_client_name(folder_chain, stem)
        pais = detect_pais(folder_chain)

        empresa = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (nombre_empresa,))
        if empresa:
            empresa_id = empresa["id"]
        else:
            if not db.agregar_empresa(nombre_empresa, "", "", "", "", pais or "", ""):
                resultado["motivo"] = f"No se pudo crear la empresa {nombre_empresa!r}"
                return resultado
            nueva = db.fetchone("SELECT id FROM empresas WHERE nombre=?", (nombre_empresa,))
            empresa_id = nueva["id"] if nueva else None
            if not empresa_id:
                resultado["motivo"] = "Empresa creada pero no se pudo recuperar su id"
                return resultado

        ok_cot = db.agregar_cotizacion_con_ruta(
            empresa_id, "", 0, None, path, archivo_hash=file_hash)
        if not ok_cot:
            resultado["motivo"] = "No se pudo insertar la cotización"
            return resultado

        resultado.update(estado="importado", empresa_id=empresa_id,
                         empresa_nombre=nombre_empresa)
        cot = db.fetchone("SELECT id FROM cotizaciones WHERE ruta_archivo=?", (path,))
        resultado["cotizacion_id"] = cot["id"] if cot else None
        return resultado

    except Exception as e:
        resultado["motivo"] = str(e)
        return resultado
