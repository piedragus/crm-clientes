"""
Escaneo de archivos — extraído de server.py (Sprint C).

Unifica los ~6 os.walk duplicados que vivían en server.py
(escanear_carpeta, importar_empresas_desde_carpeta, importar_subcarpetas,
importar_masivo, importar_masivo_preview), todos con la misma lógica de
"recorrer un árbol y calcular la cadena de carpetas relativa a una base"
pero con detalles sutiles distintos entre sí (orden de archivos, base de
la ruta relativa). walk_files() expone esos detalles como parámetros
explícitos en vez de hardcodearlos, para que cada call site mantenga su
comportamiento exacto.

IMPORTANTE: no asumir que todos los call sites quieren el mismo orden.
Antes de esta extracción, algunos no ordenaban `files` (dependían del
orden que devuelve el filesystem) y uno sí (`sorted(files)`). Cambiar
ese orden sin querer altera qué aparece primero en listas paginadas de
la UI — por eso `sort_files` es explícito y por defecto False (orden
de filesystem, igual que el código original sin sorted()).

NOTA (issue #12): `sort_files=True` también ordena `dirs` in-place,
para que el recorrido de subcarpetas sea 100% determinístico entre
sistemas operativos (Linux y Windows pueden devolver el orden de
`os.walk` distinto). Esto es un cambio de comportamiento real respecto
al código original (que nunca ordenaba `dirs`, ver server.py antes de
Sprint C) — se aplica solo bajo el mismo flag `sort_files` para no
afectar a los call sites que dependen del orden de filesystem.
"""
import os


def walk_files(walk_root, extensions=None, *, relative_to=None, sort_files=False):
    """Recorre walk_root y genera (fpath, fname, folder_chain) por cada archivo.

    - walk_root: carpeta a recorrer con os.walk.
    - extensions: set de extensiones en minúscula (con punto, ej. {'.pdf'}).
      Si es None, no filtra por extensión.
    - relative_to: carpeta respecto a la cual se calcula folder_chain.
      Si no se especifica, es walk_root (el caso más común). Se usa
      distinto de walk_root cuando se camina una subcarpeta puntual
      pero la cadena de carpetas debe incluir su propio nombre relativo
      a un ancestro común (ver importar_subcarpetas en server.py).
    - sort_files: si True, ordena los archivos de cada carpeta
      alfabéticamente antes de iterarlos, Y ordena las subcarpetas
      in-place para que el recorrido completo sea determinístico
      entre sistemas operativos. Default False: respeta el orden que
      devuelve el filesystem (igual que os.walk sin sorted()).
    """
    base = relative_to or walk_root
    for root, dirs, files in os.walk(walk_root):
        if sort_files:
            dirs.sort()  # determinismo también en el orden de subcarpetas
        rel_root = os.path.relpath(root, base)
        folder_chain = [] if rel_root == "." else rel_root.replace("\\", "/").split("/")
        names = sorted(files) if sort_files else files
        for fname in names:
            if extensions is not None and os.path.splitext(fname)[1].lower() not in extensions:
                continue
            fpath = os.path.join(root, fname)
            yield fpath, fname, folder_chain
