"""
config_export.py — Fuente única de verdad para toda la configuración de la app.

Todos los módulos deben importar get_app_config() en lugar de tener
constantes hardcodeadas. El archivo app_config.json se crea automáticamente
con defaults al primer arranque y se guarda junto a la DB.

Uso:
    from config_export import get_app_config
    cfg = get_app_config()
    umbral = cfg.get("duplicados_umbral")   # 85
    cfg.set("duplicados_umbral", 90)        # persiste en disco
"""
from __future__ import annotations
import json, os, shutil, sys
from datetime import datetime
from pathlib import Path

CONFIG_VERSION = 1

DEFAULT_CONFIG: dict = {
    "version":              CONFIG_VERSION,

    # DB
    "db_name":              "clientes_v2.db",

    # UI
    "theme":                "flatly",
    "font_family":          "Segoe UI",

    # Duplicados
    "duplicados_umbral":    85,

    # Búsqueda global
    "busqueda_page_size":   200,

    # Importador de carpetas
    "importer_thresh":      90,

    # IA — resumidor y enriquecedor
    "ai_provider":          "auto",           # gemini | grok | auto
    "gemini_model":         "gemini-2.0-flash",
    "grok_model":           "grok-3-mini",
    "ai_max_tokens":        512,
    "ai_batch_size":        10,               # empresas por llamada al enriquecedor
    "ai_retry_base_delay":  4.0,              # segundos para backoff exponencial
    "ai_batch_delay":       2.0,              # pausa entre batches

    # Filtros persistidos
    "filtros_guardados":    {},

    # Carpetas recientes
    "carpetas_recientes":   [],
}


def _find_config_dir() -> Path:
    """
    Determina el directorio donde guardar app_config.json.
    Reglas de prioridad:
      1. Directorio del script principal (normal)
      2. Directorio del ejecutable (PyInstaller)
      3. CWD como fallback
    """
    # PyInstaller sets sys.frozen
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # Normal execution: use the directory of the entry point
    main = sys.modules.get("__main__")
    if main and hasattr(main, "__file__") and main.__file__:
        return Path(main.__file__).resolve().parent
    return Path.cwd()


class ConfigManager:
    """
    Gestor de configuración persistente. Singleton — usar get_app_config().

    Lee/escribe app_config.json en el mismo directorio que la DB.
    Fusiona con DEFAULT_CONFIG para que nuevas keys aparezcan sin romper
    configs viejas.
    """

    def __init__(self):
        self._dir  = _find_config_dir()
        self._path = self._dir / "app_config.json"
        self._data = self._load()

    # ── Leer / escribir ───────────────────────────────────────────────────────
    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                merged = dict(DEFAULT_CONFIG)
                merged.update(saved)
                return merged
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)

    def save(self) -> bool:
        try:
            self._data["version"] = CONFIG_VERSION
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            import logging; logging.error(f"[ConfigManager] No se pudo guardar: {e}")
            return False

    def get(self, key: str, default=None):
        return self._data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value) -> bool:
        self._data[key] = value
        return self.save()

    # ── Filtros guardados ─────────────────────────────────────────────────────
    def guardar_filtro(self, nombre: str, filtros: dict) -> bool:
        self._data.setdefault("filtros_guardados", {})[nombre] = filtros
        return self.save()

    def eliminar_filtro(self, nombre: str) -> bool:
        self._data.get("filtros_guardados", {}).pop(nombre, None)
        return self.save()

    def get_filtros_guardados(self) -> dict:
        return self._data.get("filtros_guardados", {})

    # ── Carpetas recientes ────────────────────────────────────────────────────
    def agregar_carpeta_reciente(self, path: str) -> bool:
        recientes = self._data.setdefault("carpetas_recientes", [])
        if path in recientes:
            recientes.remove(path)
        recientes.insert(0, path)
        self._data["carpetas_recientes"] = recientes[:5]
        return self.save()

    def get_carpetas_recientes(self) -> list:
        return [p for p in self._data.get("carpetas_recientes", [])
                if os.path.isdir(p)]

    # ── Exportar / importar / resetear ───────────────────────────────────────
    def exportar(self, dest_path: str) -> bool:
        try:
            export = dict(self._data)
            export["exportado_en"] = datetime.now().isoformat()
            export["host"] = (os.environ.get("COMPUTERNAME") or
                              os.environ.get("HOSTNAME") or "desconocido")
            with open(dest_path, "w", encoding="utf-8") as f:
                json.dump(export, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            import logging; logging.error(f"[ConfigManager] exportar: {e}")
            return False

    def importar(self, src_path: str, backup: bool = True) -> tuple[bool, str]:
        try:
            with open(src_path, "r", encoding="utf-8") as f:
                nueva = json.load(f)
        except Exception as e:
            return False, f"No se pudo leer el archivo: {e}"

        if nueva.get("version", 0) > CONFIG_VERSION:
            return False, (f"Config de versión más nueva (v{nueva['version']}). "
                           "Actualizá la app primero.")

        if backup and self._path.exists():
            bak = self._path.with_suffix(
                f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            shutil.copy2(self._path, bak)

        # Nunca sobreescribir la ruta de DB al importar config de otra máquina
        nueva["db_name"] = self._data.get("db_name", DEFAULT_CONFIG["db_name"])
        for k in ("exportado_en", "host"):
            nueva.pop(k, None)

        merged = dict(DEFAULT_CONFIG)
        merged.update(nueva)
        self._data = merged
        ok = self.save()
        return ok, ("Config importada correctamente." if ok
                    else "Error al guardar la config.")

    def resetear(self) -> bool:
        db = self._data.get("db_name", DEFAULT_CONFIG["db_name"])
        self._data = dict(DEFAULT_CONFIG)
        self._data["db_name"] = db
        return self.save()


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: ConfigManager | None = None

def get_app_config() -> ConfigManager:
    global _instance
    if _instance is None:
        _instance = ConfigManager()
    return _instance
