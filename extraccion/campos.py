"""
Extracción determinística de campos vía regex — capa 1 del pipeline
(Sprint E). Sin red, sin LLM, 100% testeable y reproducible.

Cubre monto/moneda/fecha_doc con buena confianza cuando el texto tiene
un formato reconocible (la mayoría de las cotizaciones industriales:
"Total: $ 150.000,00", "USD 1,250.00", fechas dd/mm/yyyy). Los campos
que requieren comprensión semántica del documento (cliente, proveedor,
tipo de documento) no se intentan acá — quedan para la capa LLM
(resumidor.py) o para corrección manual. Es preferible no adivinar
("pendiente_revision") a inventar un valor con apariencia de certeza.
"""
import re

# Símbolos/códigos de moneda reconocidos, de más a menos específico
# (para que "U$S" no se confunda con "U" + "S" sueltos, etc.)
_MONEDA_PATTERNS = [
    # Nota: el borde derecho usa (?![A-Za-z]) en vez de \b — \b no
    # distingue letra de dígito, así que "USD3.200" (típico de OCR sin
    # espacio) no matcheaba con \bUSD\b porque "D" y "3" son ambos
    # caracteres "word" para \b. (?![A-Za-z]) permite que siga un
    # dígito pero sigue rechazando "EURO" como si fuera "EUR".
    (r"\bU\$S\b", "USD"),
    (r"\bUSD(?![A-Za-z])", "USD"),
    (r"\bDOLARES?\b", "USD"),
    (r"\bAR\$", "ARS"),
    (r"\bARS(?![A-Za-z])", "ARS"),
    (r"€", "EUR"),
    (r"\bEUR(?![A-Za-z])", "EUR"),
    (r"\bBRL(?![A-Za-z])", "BRL"),
    (r"\bR\$", "BRL"),
    (r"\bCLP(?![A-Za-z])", "CLP"),
    (r"\bUYU(?![A-Za-z])", "UYU"),
    (r"\$"  , "ARS"),  # "$" suelto: default razonable para cotizaciones AR
]

# Número con separadores de miles/decimales en cualquiera de los dos
# formatos (AR: 1.500,00 — US: 1,500.00) o sin separadores (15000).
# Importante: capturar TODO el run de dígitos/separadores de una, no por
# alternancia de sub-patrones — la alternancia con backtracking limitado
# de Python puede quedarse con el primer match corto (ej. "150" de
# "15000") en vez de seguir hasta el final del número completo.
_NUM_RE = r"(\d[\d.,]*\d|\d)"

_FECHA_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")


def _normalizar_monto(s: str) -> float | None:
    """Misma lógica base que resumidor.py._parse_response (formato AR vs
    US), con un caso extra: un solo '.' con exactamente 3 dígitos después
    es separador de miles en formato AR sin decimales ('2.500' -> 2500),
    no un punto decimal ('2.50' -> 2.5, eso sí queda como decimal)."""
    s = s.strip()
    try:
        if "," in s and "." in s:
            if s.rindex(",") > s.rindex("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            partes = s.split(",")
            if len(partes) == 2 and len(partes[1]) == 3 and partes[1].isdigit():
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
        elif "." in s:
            partes = s.split(".")
            if len(partes) == 2 and len(partes[1]) == 3 and partes[1].isdigit():
                s = s.replace(".", "")
            # si no, lo dejamos tal cual (punto decimal estándar: "150.50")
        return float(s)
    except (ValueError, TypeError):
        return None


def _detectar_moneda(texto: str) -> tuple[str, float] | None:
    for patron, codigo in _MONEDA_PATTERNS:
        if re.search(patron, texto, re.IGNORECASE):
            # "$" suelto es más ambiguo (podría ser cualquier país) -> menos confianza
            confianza = 0.6 if patron == r"\$" else 0.9
            return codigo, confianza
    return None


def _detectar_monto(texto: str) -> tuple[float, float] | None:
    """Busca números cerca de un símbolo de moneda o de palabras clave
    como 'Total'/'Monto'/'Importe', priorizando esos sobre números sueltos
    en el documento (que podrían ser códigos de producto, teléfonos, etc.)."""
    candidatos = []

    # Prioridad 1: número inmediatamente después de un símbolo de moneda
    for patron, _ in _MONEDA_PATTERNS:
        for m in re.finditer(patron + r"\s*" + _NUM_RE, texto, re.IGNORECASE):
            valor = _normalizar_monto(m.group(1))
            if valor is not None:
                candidatos.append((valor, 0.85))

    # Prioridad 2: número después de palabras clave de total
    for m in re.finditer(
            r"\b(?:total|monto|importe)\b[:\s]*\$?\s*" + _NUM_RE,
            texto, re.IGNORECASE):
        valor = _normalizar_monto(m.group(1))
        if valor is not None:
            candidatos.append((valor, 0.75))

    if not candidatos:
        return None
    # El de mayor confianza primero; si hay empate, el de mayor monto
    # (suele ser el total final, no un subtotal o un ítem individual)
    candidatos.sort(key=lambda c: (c[1], c[0]), reverse=True)
    return candidatos[0]


def _detectar_fecha(texto: str) -> tuple[str, float] | None:
    m = _FECHA_RE.search(texto)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        d, mo = int(d), int(mo)
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return None
        return f"{y}-{mo:02d}-{d:02d}", 0.7
    except ValueError:
        return None


# Frases típicas de cotizaciones industriales en español para la
# validez de la oferta. El número de días siempre aparece DESPUÉS de
# la palabra clave en estas variantes ("Validez: 15 días", "Oferta
# válida por 30 días", "Vigencia de la oferta: 10 días corridos").
_VALIDEZ_RE = re.compile(
    r"\b(?:validez|vigencia|v[aá]lid[oa])\b"
    r"[^\d\n]{0,45}?"          # conector flexible: "de la oferta:", "de", ": ", "por ", etc.
    r"(\d{1,3})\s*d[ií]as?",
    re.IGNORECASE)


def _detectar_validez_dias(texto: str) -> tuple[int, float] | None:
    m = _VALIDEZ_RE.search(texto)
    if not m:
        return None
    try:
        dias = int(m.group(1))
        if 1 <= dias <= 365:  # fuera de ese rango, más probable que sea
                              # otra cosa (ej. "garantía de 730 días")
            return dias, 0.85
    except ValueError:
        pass
    return None


def extraer_campos_deterministicos(texto: str) -> dict:
    """
    Recibe el texto crudo extraído de un archivo y devuelve un dict
    {campo: (valor, confianza)} solo para los campos que se pudieron
    detectar con la capa determinística. Campos no encontrados no
    aparecen en el resultado — el caller decide qué hacer (capa
    siguiente, o 'pendiente_revision').

    Tolerante a texto vacío/incompleto: nunca lanza excepción, en el
    peor caso devuelve {}.
    """
    if not texto or not isinstance(texto, str):
        return {}

    resultado = {}

    moneda = _detectar_moneda(texto)
    if moneda:
        resultado["moneda"] = moneda

    monto = _detectar_monto(texto)
    if monto:
        resultado["monto"] = monto

    fecha = _detectar_fecha(texto)
    if fecha:
        resultado["fecha_doc"] = fecha

    validez = _detectar_validez_dias(texto)
    if validez:
        resultado["validez_dias"] = validez

    return resultado
