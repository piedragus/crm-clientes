# Guía para agentes IA — CRM Clientes

## Stack
- **Backend:** Python 3.11, Flask, SQLite (WAL mode)
- **Frontend:** HTML/JS vanilla, sin frameworks, sin build step
- **Tests:** unittest (389 tests), pruebas_stress.py, pruebas_http_stress.py
- **IA:** Gemini 2.0 Flash + Grok 3 mini (fallback automático)

## Regla de oro
**Antes de cualquier cambio:** correr los tres test suites y que pasen.
**Después de cualquier cambio:** ídem. Nunca bajar de 389 tests verdes.

```bash
python3 test_suite.py
python3 pruebas_stress.py
python3 pruebas_http_stress.py
```

## Arquitectura
```
server.py          — Flask REST API (35 endpoints)
static/index.html  — SPA completa (HTML/CSS/JS en un solo archivo)
db_manager.py      — Toda la lógica de DB (WAL + global write lock)
utils.py           — BackupManager, Exportador, CSVCleaner, Config
resumidor.py       — Gemini/Grok → resumen estructurado de cotizaciones
enriquecer_empresas_gemini.py — Normalización de nombres con IA
extractor_texto.py — PDF/DOCX/XLSX/PPTX/TXT → texto plano
csv_utils.py       — Parsing CSV con detección de encoding y separador
config_export.py   — ConfigManager singleton (app_config.json)
```

## Responsabilidades por modelo
- **Claude:** arquitectura, db_manager, server, test_suite, thread-safety
- **GPT:** stress tests, scripts de instalación, documentación
- **Gemini:** resumidor, enriquecedor, integración APIs de IA

## Decisiones técnicas importantes (no revertir sin discutir)
- WAL mode + `_global_write_lock_for()` → serializa escrituras entre instancias
- `_MemConn` con RLock → thread-safety para :memory: (tests)
- Exportación con `?tipo=empresas|contactos|cotizaciones`
- Resumen IA con estado_ia: pendiente→procesando→ok|error
- `_ia_ratelimit()` en /api/enriquecer → un solo llamado IA a la vez

## Variables de entorno
```
GEMINI_API_KEY   → Gemini 2.0 Flash
GROK_API_KEY     → Grok 3 mini (fallback)
HOST             → default 0.0.0.0
PORT             → default 5000
```
# Develop branch
