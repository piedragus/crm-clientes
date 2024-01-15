"""csv_utils.py — Funciones puras de parseo CSV, sin dependencias GUI."""
import csv, os

PUBLIC_DOMAINS = {
    "gmail.com","google.com","yahoo.com","hotmail.com","outlook.com",
    "live.com","aol.com","proton.me","protonmail.com","icloud.com",
    "yahoo.com.ar","hotmail.com.ar","yahoo.es",
}

TLD_TO_COUNTRY = {
    'ar':'Argentina','cl':'Chile','uy':'Uruguay','br':'Brasil','py':'Paraguay',
    'bo':'Bolivia','pe':'Peru','co':'Colombia','mx':'Mexico','us':'Estados Unidos',
    'es':'Espana','uk':'Reino Unido','ca':'Canada','de':'Alemania','fr':'Francia',
    'it':'Italia','pt':'Portugal','au':'Australia','jp':'Japon','cn':'China',
}

EMAIL_COLS    = ['e-mail 1 - value','email','e-mail','correo','mail','email address']
FNAME_COLS    = ['first name','nombre','given name','primer nombre','name']
LNAME_COLS    = ['last name','apellido','family name','surname','segundo nombre']
FULLNAME_COLS = ['nombre completo','full name','display name','nombre']
PHONE_COLS    = ['phone 1 - value','phone','telefono','tel','mobile','celular']


def _find_col(row_keys, candidates):
    """
    Retorna el primer key del dict que coincida con alguna candidata.
    Case-insensitive. Strips BOM (\ufeff) and extra whitespace from headers.
    """
    keys_lower = {k.lower().strip().lstrip("\ufeff"): k for k in row_keys}
    for c in candidates:
        if c in keys_lower:
            return keys_lower[c]
    return None


def _detect_separator(path: str, encoding: str) -> str:
    """Detecta si el CSV usa coma o punto y coma como separador."""
    try:
        with open(path, "r", encoding=encoding, newline="") as f:
            first_line = f.readline()
        comma_count     = first_line.count(",")
        semicolon_count = first_line.count(";")
        return ";" if semicolon_count > comma_count else ","
    except Exception:
        return ","

def _open_csv(path):
    """
    Abre un CSV probando utf-8-sig, utf-8, latin-1, cp1252.
    Devuelve (file_handle, encoding, separator).
    El separador se detecta automáticamente (coma o punto y coma).
    """
    if os.path.getsize(path) == 0:
        return open(path, 'r', encoding='utf-8', newline=''), 'utf-8', ','
    for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
        try:
            with open(path, 'r', encoding=enc, newline='') as f:
                f.read(1024)
            sep = _detect_separator(path, enc)
            return open(path, 'r', encoding=enc, newline=''), enc, sep
        except (UnicodeDecodeError, LookupError, UnicodeError):
            continue
    sep = _detect_separator(path, 'utf-8')
    return open(path, 'r', encoding='utf-8', errors='replace', newline=''), 'utf-8(replace)', sep
