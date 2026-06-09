import configparser
import logging
import csv
import re
from typing import List, Dict
from shutil import copy2
from datetime import datetime
import os
import glob
try:
    import pandas as pd
except Exception:
    pd = None
try:
    from thefuzz import fuzz
except Exception:
    from difflib import SequenceMatcher
    class _FuzzFallback:
        @staticmethod
        def ratio(a, b):
            return int(SequenceMatcher(None, str(a or '').lower(), str(b or '').lower()).ratio() * 100)
    fuzz = _FuzzFallback()

class Config:
    """Clase para gestionar la configuración de la aplicación."""
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(self.base_dir, 'config.ini')
        self.config = configparser.ConfigParser()
        if not self.config.read(self.config_path):
            self._crear_config_por_defecto()
            
    def _crear_config_por_defecto(self):
        self.config['DATABASE'] = {'db_name': 'clientes_v2.db'}
        self.config['FUZZY_MATCHING'] = {'threshold': '80'}
        with open(self.config_path, 'w') as configfile:
            self.config.write(configfile)

    def get_db_name(self) -> str:
        db_name = self.config.get('DATABASE', 'db_name')
        if db_name != ':memory:' and not os.path.isabs(db_name):
            return os.path.join(self.base_dir, db_name)
        return db_name
    
    def get_fuzzy_threshold(self) -> int:
        return self.config.getint('FUZZY_MATCHING', 'threshold')

TLD_TO_COUNTRY = {
    'ar': 'Argentina', 'cl': 'Chile', 'uy': 'Uruguay', 'br': 'Brasil', 'py': 'Paraguay',
    'bo': 'Bolivia', 'pe': 'Perú', 'co': 'Colombia', 'mx': 'México', 'us': 'Estados Unidos',
    'uk': 'Reino Unido', 'es': 'España', 'it': 'Italia', 'fr': 'Francia', 'de': 'Alemania',
    'cn': 'China', 'jp': 'Japón', 'kr': 'Corea del Sur', 'ca': 'Canadá', 'au': 'Australia',
    'nz': 'Nueva Zelanda', 'pt': 'Portugal', 've': 'Venezuela', 'ec': 'Ecuador', 'cr': 'Costa Rica',
    'sv': 'El Salvador', 'gt': 'Guatemala', 'hn': 'Honduras', 'ni': 'Nicaragua', 'pa': 'Panamá',
    'do': 'República Dominicana', 'cu': 'Cuba', 'pr': 'Puerto Rico', 'ie': 'Irlanda', 'be': 'Bélgica',
    'ch': 'Suiza', 'nl': 'Países Bajos', 'se': 'Suecia', 'no': 'Noruega', 'dk': 'Dinamarca',
    'fi': 'Finlandia', 'pl': 'Polonia', 'cz': 'República Checa', 'gr': 'Grecia', 'hu': 'Hungría',
    'ro': 'Rumania', 'bg': 'Bulgaria', 'ru': 'Rusia', 'in': 'India', 'sg': 'Singapur',
    'za': 'Sudáfrica', 'ae': 'Emiratos Árabes Unidos', 'sa': 'Arabia Saudita', 'qa': 'Qatar',
    'kw': 'Kuwait', 'il': 'Israel', 'tr': 'Turquía'
}

logging.basicConfig(
    filename='clientes_app.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class BackupManager:
    @staticmethod
    def _backup_dir(db_name):
        base_dir = os.path.dirname(os.path.abspath(db_name)) or os.getcwd()
        return os.path.join(base_dir, 'backups')

    @staticmethod
    def hacer_backup(db_name):
        """
        Crea un backup usando la SQLite backup API.
        Esto es correcto con WAL mode: incluye todos los cambios commiteados
        sin importar si el -wal file está presente.
        """
        try:
            if not os.path.exists(db_name):
                logging.warning("No se encontró el archivo de base de datos para el backup.")
                return False
            backup_dir = BackupManager._backup_dir(db_name)
            os.makedirs(backup_dir, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            stem = os.path.splitext(os.path.basename(db_name))[0]
            backup_file = os.path.join(backup_dir, f"{stem}_{timestamp}.db")
            # Use SQLite backup API — handles WAL correctly
            import sqlite3 as _sq
            src  = _sq.connect(db_name, timeout=10)
            dst  = _sq.connect(backup_file)
            src.backup(dst)
            dst.close(); src.close()
            logging.info(f"Backup creado: {backup_file}")
            return True
        except Exception as e:
            logging.error(f"Error en backup: {str(e)}")
            return False

    @staticmethod
    def restaurar_backup(db_name):
        """
        Restaura el backup más reciente usando la SQLite backup API.
        Checkpoint WAL antes de restaurar para evitar que el -wal residual
        tome precedencia sobre el archivo restaurado.
        """
        try:
            backup_dir = BackupManager._backup_dir(db_name)
            backups = glob.glob(os.path.join(backup_dir, '*.db'))
            if not backups:
                return False
            latest = max(backups, key=os.path.getmtime)
            import sqlite3 as _sq
            src = _sq.connect(latest, timeout=10)
            dst = _sq.connect(db_name, timeout=10)
            # Checkpoint any pending WAL before overwriting
            try:
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            src.backup(dst)
            dst.close(); src.close()
            # After restoring, checkpoint the WAL into the main file
            # and truncate it so no stale transactions remain
            try:
                import sqlite3 as _sq2
                conn_ck = _sq2.connect(db_name, timeout=10)
                conn_ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn_ck.close()
            except Exception:
                pass
            logging.info(f"Backup restaurado: {latest}")
            return True
        except Exception as e:
            logging.error(f"Error al restaurar backup: {str(e)}")
            return False

class Exportador:
    """Clase para exportar datos a diferentes formatos."""
    @staticmethod
    def a_excel(datos: List[Dict], archivo: str) -> bool:
        try:
            if pd is None:
                logging.error("pandas/openpyxl no instalado: no se puede exportar Excel")
                return False
            df = pd.DataFrame(datos)
            df.to_excel(archivo, index=False)
            logging.info(f"Exportado a Excel: {archivo}")
            return True
        except Exception as e:
            logging.error(f"Error al exportar Excel: {str(e)}")
            return False

    @staticmethod
    def a_csv(datos: List[Dict], archivo: str) -> bool:
        try:
            if not datos:
                with open(archivo, 'w', encoding='utf-8') as f:
                    f.write('')
                logging.info(f"Exportado CSV vacío: {archivo}")
                return True
            with open(archivo, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=datos[0].keys())
                writer.writeheader()
                writer.writerows(datos)
            logging.info(f"Exportado a CSV: {archivo}")
            return True
        except Exception as e:
            logging.error(f"Error al exportar CSV: {str(e)}")
            return False

class CSVCleaner:
    """Limpia y valida archivos CSV de contactos con manejo especial para el formato provisto."""
    
    @staticmethod
    def clean_email(email: str) -> str:
        """Limpia y valida un email, incluso aquellos con comillas extrañas."""
        if not isinstance(email, str) or (pd is not None and pd.isna(email)):
            return ""
        
        email = re.sub(r'^"+|"+$', '', email.strip()).lower()
        
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            return ""
        return email

    @staticmethod
    def extract_empresa_from_email(email: str, org_name: str = "") -> str:
        """Extrae el nombre de empresa del dominio del email con lógica mejorada."""
        if not email or '@' not in email:
            return org_name or "Sin empresa"
        
        dominio = email.split('@')[1].lower()
        GENERIC_DOMAINS = {
            'gmail.com', 'hotmail.com', 'yahoo.com', 'outlook.com',
            'msn.com', 'live.com', 'sinectis.com.ar', 'infovia.com.ar', 'icloud.com', 'me.com'
        }
        
        if dominio in GENERIC_DOMAINS:
            return org_name or "Sin empresa"
        
        partes = dominio.split('.')
        if len(partes) >= 2:
            empresa = partes[-2] if len(partes) > 1 and partes[-1] in ('ar', 'com', 'net', 'org') else partes[0]
            
            empresa = re.sub(r'^(com|net|co|org|web|info|srl)$', '', empresa, flags=re.IGNORECASE).strip()
            empresa = empresa.replace('-', ' ').replace('_', ' ').title()
            
            empresa = re.sub(r'sa$|srl$|s.a$|s.r.l$', '', empresa, flags=re.IGNORECASE).strip()
            return empresa if empresa else org_name or dominio.split('.')[0].title()
        
        return org_name or dominio.title()
    

    @staticmethod
    def detect_encoding(filepath: str) -> str:
        for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    f.read(4096)
                return enc
            except UnicodeDecodeError:
                continue
        return 'latin-1'

    @staticmethod
    def clean_csv(filepath: str) -> List[Dict]:
        """Procesa el CSV con manejo especial para el formato provisto."""
        try:
            with open(filepath, 'r', encoding=CSVCleaner.detect_encoding(filepath), newline='') as f:
                contactos = []
                reader = csv.DictReader(f)
                
                email_fields = [f'E-mail {i} - Value' for i in range(1, 4)]
                phone_fields = [f'Phone {i} - Value' for i in range(1, 3)]
                
                for row in reader:
                    email = ""
                    for field in email_fields:
                        if field in row and row[field]:
                            temp_email = CSVCleaner.clean_email(row[field])
                            if temp_email:
                                email = temp_email
                                break
                    
                    if not email:
                        continue
                    
                    nombre = f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
                    nombre = re.sub(r"^'|'$", "", nombre)
                    
                    telefono = ""
                    for field in phone_fields:
                        if field in row and row[field]:
                            telefono = re.sub(r'(?<!^)[^\d]', '', row[field].strip())
                            if telefono:
                                break
                    
                    org_name = row.get('Organization Name', '').strip()
                    empresa = org_name if org_name else CSVCleaner.extract_empresa_from_email(email)
                    
                    contactos.append({
                        'nombre': nombre,
                        'email': email,
                        'telefono': telefono,
                        'empresa': empresa
                    })
                
                return contactos
                
        except Exception as e:
            logging.error(f"Error en clean_csv: {str(e)}")
            raise ValueError(f"Error al limpiar CSV: {str(e)}")

# ── formatear_fecha (única fuente de verdad) ──────────────────────────────────
def formatear_fecha(fecha_str: str) -> str:
    """Convierte 'YYYY-MM-DD HH:MM:SS' o 'YYYY-MM-DD' a 'DD-MM-YYYY'."""
    if not fecha_str:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(fecha_str, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return str(fecha_str)
