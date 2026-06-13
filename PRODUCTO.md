# CRM Clientes — Especificación de Producto y Roadmap

**Repo:** `github.com/piedragus/crm-clientes`
**Stack:** Python / Flask / SQLite / HTML+CSS+JS
**Estado actual:** `develop` @ `07220f2` — Sprint B mergeado, listo para QA manual antes de `main`

---

## ¿Qué es este producto?

CRM de ventas industriales para gestionar el ciclo comercial completo: desde el primer contacto hasta el seguimiento post-cotización. Está diseñado para una operación donde el volumen de cotizaciones históricas es grande (13.000+ PDFs) y la relación con cada cliente se construye a lo largo de múltiples proyectos y proveedores.

**Usuario principal:** equipo comercial de ventas industriales.
**No es:** un ERP, un sistema de facturación, ni un CRM genérico tipo Salesforce.

---

## Datos actuales en producción

| Entidad | Cantidad |
|---------|----------|
| Empresas | ~2.786 |
| Contactos | ~1.325 |
| Cotizaciones importadas | ~12.676 |
| PDFs fuente | ~13.193 |

---

## Características implementadas

### Módulo Empresas
- [x] CRUD completo (crear, editar, eliminar)
- [x] Lista paginada server-side con búsqueda y filtros (país, rubro, tags)
- [x] Vista de detalle con tabs: Empresa / Cotizaciones / Actividad
- [x] Tags por empresa
- [x] Campo país y rubro
- [x] **Aliases** — nombres alternativos normalizados para resolución en importaciones y unificación (Sprint B)
- [x] Unificación de empresas duplicadas (merge con preservación de aliases)

### Módulo Contactos
- [x] CRUD de contactos por empresa
- [x] Campos: nombre, email, teléfono, país, cargo

### Módulo Cotizaciones
- [x] Listado por empresa con monto, moneda, fecha, proveedor
- [x] Import desde PDFs (extracción básica de metadatos)
- [x] Deduplicación por hash SHA256

### Módulo Pipeline / Oportunidades
- [x] Vista tabla con etapas: Prospecto → Cotizado → Negociación → Ganado / Perdido
- [x] Separación venta / posventa
- [x] Cambio de etapa inline

### Módulo Actividad
- [x] Timeline de notas y actividades por empresa
- [x] Tipos: llamada, reunión, email, nota, tarea

### Dashboard
- [x] Cotizaciones por mes (barras CSS)
- [x] Top empresas por monto
- [x] Pipeline por etapa
- [x] Métricas globales (stat cards interactivos)

### Importador
- [x] Importador masivo desde carpeta raíz (estructura `País/Cliente/archivo.pdf`)
- [x] Importador por subcarpetas seleccionables
- [x] Detección de país desde estructura de carpetas
- [x] Detección de nombre de empresa desde carpeta padre
- [x] Normalización de acentos y caracteres especiales
- [x] Skip de carpetas genéricas (`GENERIC_FOLDERS`)
- [x] Resolución por alias antes de crear empresa nueva (Sprint B)
- [x] Backup automático antes de cada importación

### Herramientas / Admin
- [x] Limpiar y unificar duplicados (fuzzy matching)
- [x] Renombrar empresa
- [x] Preview de duplicados antes de acción
- [x] Verificación de instalación de dependencias
- [x] Backup manual

---

## Deuda técnica conocida

| Item | Severidad | Sprint |
|------|-----------|--------|
| `fuzzywuzzy` → migrar a `rapidfuzz` | Media | C |
| `importer/` como módulo monolítico en `server.py` | Media | C |
| Sin staging de importaciones (no hay rollback por lote) | Alta | D |
| Extracción de PDF básica (solo metadatos de carpeta) | Alta | E |
| `jsonify(...)` directo mezclado con `err(...)` en aliases | Baja | limpieza futura |
| 5 tests fallando por `fuzzywuzzy` no instalado | Baja | C |

---

## Roadmap

### ✅ Sprint A — MVP + importación masiva
*Mergeado a `main`*

- CRUD empresas, contactos, cotizaciones
- Importador masivo de 13.000 PDFs
- Pipeline / oportunidades
- Dashboard con métricas
- Sistema de unificación de duplicados
- 511 tests

---

### ✅ Sprint B — Aliases de empresas
*Mergeado a `develop` @ `07220f2`*

- Tabla `empresa_aliases` con normalización (tildes, sufijos legales, case)
- Métodos DB: agregar, buscar, listar, eliminar, migrar aliases
- Endpoints: `GET/POST/DELETE /api/empresas/:id/aliases`
- UI: sección aliases dentro del tab Empresa
- Importadores resuelven por alias antes de crear empresa nueva (cache, sin N+1)
- Al unificar: nombre origen se guarda como alias de la empresa destino
- 522 tests, 517 OK

**Pendiente antes de merge a `main`:** QA manual
1. Agregar/borrar alias desde UI
2. Importar PDF con carpeta que matchea por alias
3. Unificar dos empresas y verificar aliases resultantes

---

### 🔜 Sprint C — Refactor importer + rapidfuzz
*Próximo*

- Extraer lógica de importación de `server.py` a package `importer/`
  - `importer/__init__.py`
  - `importer/scanner.py` — escaneo de archivos
  - `importer/resolver.py` — resolución empresa/país
  - `importer/normalizer.py` — normalización de nombres (unificar con `utils/normalizacion.py`)
  - `importer/constants.py` — `GENERIC_FOLDERS`, extensiones, etc.
- Reemplazar `fuzzywuzzy` por `rapidfuzz` (misma API, mejor performance)
- Eliminar los 5 tests fallando por `fuzzywuzzy`
- Sin cambios de comportamiento visible para el usuario

---

### 🔜 Sprint D — Staging de importaciones
*Requiere Sprint C*

- Tablas nuevas: `import_batches`, `import_items`
- Flujo: escanear → staging → revisar → confirmar → aplicar
- Rollback por lote: deshacer una importación completa
- Preview de qué se va a crear/modificar antes de confirmar
- Historial de lotes importados en UI

---

### 🔜 Sprint E — Extracción avanzada de PDF
*Requiere Sprint D*

- Extraer texto real del PDF (pdfplumber / pdfminer)
- Parsear: número de cotización, monto, moneda, proveedor, fecha desde el contenido
- Enriquecimiento con IA (Gemini / GPT) para campos no estructurados
- Confianza de extracción por campo
- Revisión manual de campos de baja confianza en UI

---

### 🔜 Sprint F — Watcher de carpeta OneDrive
*Requiere Sprint C (para tener entry point de importación por archivo único)*

Proceso Python con `watchdog` que detecta PDFs nuevos en la carpeta de OneDrive en tiempo real y los importa automáticamente al CRM.

**Features:**
- Proceso en consola (no Windows Service), arranca con `.bat`
- Detecta eventos `ON_CREATED` / `ON_MOVED` sobre la raíz de OneDrive
- Reutiliza importer refactorizado (Sprint C) + alias resolution (Sprint B)
- Deduplicación por `file_sha256` (sin reimportar lo ya conocido)
- Notificaciones en el CRM al llegar PDFs nuevos (badge / toast)

**Carpetas excluidas** (sincronizar con `GENERIC_FOLDERS` del importer):
- `00 COTIZACIONES TIPO DE MAQ` — plantillas por tipo de máquina
- `AA CODIGO DE MAQUINAS` — códigos internos
- `AAA PARTES DE MAQ` — partes y repuestos
- `ALAMBRES- BARRAS` — catálogos de producto
- `MANUAL` / `MANUAL MAQUINA` — manuales técnicos
- `FOTOS*` — imágenes
- `Estructura` — hojas membretadas
- `Prearmados de Maquinas` — plantillas de armado

**Decisión técnica pendiente:** verificar si el importer (post Sprint C) expone un entry point por archivo único (`import_single_file(path)`) o solo procesa directorios. Define si hay que wrappear o refactorizar antes de arrancar este sprint.

**Componentes planificados:**
- `watcher/watcher_service.py` — loop principal watchdog
- `watcher/watcher_config.py` — ruta base, filtros, carpetas excluidas
- `watcher/watcher_handler.py` — extrae país/empresa/filename, llama al importer
- `watcher/run_watcher.bat` — launcher Windows con activación de venv

---

### 🔜 Sprint G — Recorridos comerciales *(idea en maduración técnica)*
*Requiere definición + APIs externas*

Inspirado en el flujo del Gem de Gemini para planificación de visitas. La idea es integrar dentro del CRM la capacidad de armar recorridos optimizados usando la base de empresas existente.

**Features candidatas:**
- Campos `latitud` / `longitud` en empresas (o geocodificación desde dirección)
- Estados de visita: Confirmado / Dudoso / Cerrado / No relevante / Prospecto nuevo
- Campo `prioridad_visita`: Alta / Media / Baja
- Módulo recorridos: punto de inicio + punto final + paradas fijas opcionales → orden optimizado
- Registro de feedback post-visita (resultado, próxima acción, contacto)
- Búsqueda de prospectos nuevos por zona (Google Places API)

**Dependencias externas:**
- Google Maps / Places API (geocodificación + búsqueda de prospectos) — tiene costo por volumen
- Geocodificación masiva de ~2.786 empresas existentes — puede hacerse via Gem de Gemini como paso previo, exportando lat/lng para importar al CRM

**Estado actual:**
- Cubierto temporalmente por un Gem de Gemini con acceso a planilla de clientes por zona
- El Gem puede usarse para enriquecer las empresas existentes con lat/lng y estado de visita, y luego importar esos datos al CRM como alimentación inicial

**No arrancar hasta:** tener Sprint D (staging) funcionando, para poder importar el enriquecimiento del Gem de forma controlada y reversible.

---

### 🔜 Sprint H — Unificación inteligente con LLM local *(concepto)*
*Requiere Sprint D; LLM local (ollama) instalado*

El fuzzy matching con rapidfuzz cubre bien los casos claros (~90%), pero la "zona gris" (score 60–85%) genera tanto falsos positivos como falsos negativos. Un LLM local corriendo en tarea nocturna puede resolver esos casos con criterio semántico real.

**Problema que resuelve:**
- "RENAULT PFA", "RenaultPFA", "renaultpfa3001" → mismo cliente, sin reglas hardcodeadas
- Nombres con typos, abreviaturas, acentos mal codificados que el fuzzy no resuelve con certeza
- Unificaciones de empresas duplicadas que hoy requieren revisión manual caso por caso

**Arquitectura híbrida propuesta:**
```
Nombre de carpeta/archivo
        ↓
  normalize + rapidfuzz   ← rápido, cubre 90% de casos
        ↓ (si score < umbral configurable)
  LLM local (ollama)      ← solo para casos ambiguos
        ↓
  alias resolution (Sprint B)
```

**Flujo nocturno:**
1. Detecta pares de empresas en zona gris (score rapidfuzz 60–85%)
2. LLM evalúa: nombre, país, cotizaciones asociadas → ¿misma empresa?
3. Propuestas van a tabla `unificacion_sugerida` (pendiente aprobación humana)
4. UI de revisión en el CRM: aprobar / rechazar cada sugerencia al día siguiente

**Ventajas de nocturno vs. tiempo real:**
- Sin restricción de latencia (~1-3 seg/archivo con ollama es viable en batch)
- Reprocesamiento de las ~2.786 empresas existentes posible
- No bloquea el flujo normal de importación

**Stack candidato:** `ollama` + `llama3` o `mistral` (local, sin costo por token)

**Estado:** concepto aprobado, sin fecha. No arrancar antes de Sprint D.

---

### 🔜 Sprint I — Features comerciales (por definir)
*Ideas candidatas, sin priorizar*

- Recordatorios y follow-ups automáticos
- Filtro "empresas sin actividad en X días"
- Exportación a Excel / CSV del pipeline
- Búsqueda global (empresas + cotizaciones + contactos)
- Vista calendario de actividades
- Indicadores de conversión por etapa del pipeline
- Multi-usuario / roles (solo si el equipo crece)

---

## Principios de diseño

- **Sales tool, not config tool** — la interfaz principal es el flujo comercial. Las herramientas admin van en Herramientas, no en el sidebar.
- **Stat cards interactivos** — los números del dashboard llevan a las listas filtradas, no son solo display.
- **Sidebar operacional** — solo acciones del día a día, sin configuración.
- **Sin over-engineering** — SQLite es suficiente para el volumen actual. No agregar Redis, Celery, ni microservicios hasta que haya necesidad real.
- **Branch strategy:** `feature/*` → `develop` → `main`. Peer review entre GPT (code review) y Claude (implementación + git).

---

## Contexto técnico para onboarding de IAs

- `server.py` — Flask app monolítica, ~1.900 líneas. Todas las rutas y lógica de negocio.
- `db_manager.py` — DBManager con conexiones SQLite thread-safe. Todos los métodos de DB.
- `static/index.html` — SPA de ~3.200 líneas. Estado global en objeto `S`. Sin framework JS.
- `utils/` — package con `utils_legacy.py` (Config, BackupManager, Exportador, CSVCleaner), `normalizacion.py`, `excepciones.py`.
- `test_suite.py` — 522 tests unitarios e integración.
- `pruebas_stress.py` / `pruebas_http_stress.py` — suites de stress y endpoints HTTP.
- GitHub push requiere: `git remote set-url origin https://[TOKEN]@github.com/piedragus/crm-clientes.git`
