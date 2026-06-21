"""
Constantes del importador — extraídas de server.py (Sprint C).

GENERIC_FOLDERS: nombres de carpeta que no representan un cliente
(países, categorías de producto, meses, plantillas, etc.) — se
saltean al buscar el nombre de empresa en la cadena de carpetas.

IMPORT_EXTS: extensiones de archivo que el importador masivo procesa.

PAISES_CONOCIDOS_NORM: diccionario {nombre_normalizado: forma_canónica}
de países reconocidos automáticamente desde nombres de carpeta.
"""
from .utils import normalizar_basico

GENERIC_FOLDERS = {
    'argentina', 'bolivia', 'chile', 'colombia', 'estados unidos', 'mexico',
    'peru', 'uruguay', 'nicaragua', 'paraguay', 'ecuador', 'brasil',
    'comercial', 'equipos', 'aspiradores', 'cabinas', 'pipe (tubos)',
    'repuestos tolva', 'hojas membretadas especiales',
    'pedidos presupuestos internacionales', 'datos de catalogo tolvas',
    'tg 70', 'tg100', 'tg250', 'tg500',
    'cartas', 'carta', 'cotizaciones', 'cotizacion', 'clientes', 'clientes x zona',
    'planos', 'mantenimiento', 'nueva carpeta', 'mto', 'mto-2011', 'mto-2012',
    'mto-2013', 'mto2011', 'a- representantes en el exterior',
    '00 cotizaciones tipo de maq',
    'enero 2011', 'febrero 2011', 'julio 2011', 'junio 2011', 'diciembre 2011',
    'abril2011', 'noviembre-2011', 'agosto', 'mayo', '2013',
    '500p', '800p', '1000p', '1000s', '1200p', '1200s', '1500p', '500s', '600p', '600s',
    '6 ca', '1 ca', '1 cl', '12 ca', '147 m 20 hp', 'maquina nueva',
    'aaa-con o sin competencia', 'aa cotizaciones e informes tipicas',
}

IMPORT_EXTS = {'.pdf', '.docx', '.xlsx', '.doc', '.xls', '.pptx', '.txt'}

PAISES_CONOCIDOS_NORM = {
    normalizar_basico(k): v for k, v in {
        "Argentina": "Argentina", "Chile": "Chile", "Bolivia": "Bolivia",
        "Colombia": "Colombia", "Perú": "Perú", "Peru": "Perú",
        "Uruguay": "Uruguay", "Nicaragua": "Nicaragua", "Paraguay": "Paraguay",
        "Ecuador": "Ecuador", "Brasil": "Brasil", "Brazil": "Brasil",
        "México": "México", "Mexico": "México",
        "Estados Unidos": "Estados Unidos", "USA": "Estados Unidos",
        "EEUU": "Estados Unidos",
        "Venezuela": "Venezuela", "Panamá": "Panamá", "Panama": "Panamá",
        "Guatemala": "Guatemala", "Cuba": "Cuba", "España": "España",
        "Espana": "España", "Dubai": "Dubai", "Canadá": "Canadá",
        "Canada": "Canadá", "Australia": "Australia",
    }.items()
}
