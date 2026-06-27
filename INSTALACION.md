# CRM Clientes v25 — Interfaz Web

## Windows

Doble click en:

```bat
iniciar_crm_web.bat
```

El script crea `.venv`, instala dependencias si faltan y abre el navegador en `http://localhost:5000`.

## Linux / Mac

```bash
./iniciar_crm_web.sh
```

## OCR para PDFs escaneados (opcional)

Para que la extracción de campos funcione también con PDFs escaneados
(sin texto seleccionable), hace falta instalar dos binarios a nivel
sistema operativo — `pip install` solo no alcanza:

**Windows:**
1. Tesseract: [instalador oficial](https://github.com/UB-Mannheim/tesseract/wiki) (agregar a PATH)
2. Poppler: descargar [binarios para Windows](https://github.com/oschwartz10612/poppler-windows/releases), agregar la carpeta `bin/` al PATH

**Linux:**
```bash
sudo apt-get install tesseract-ocr tesseract-ocr-spa poppler-utils
```

**Mac:**
```bash
brew install tesseract tesseract-lang poppler
```

Si estos binarios no están instalados, la app sigue funcionando
normalmente — los PDFs escaneados simplemente quedan con sus campos
en estado "pendiente de revisión" para completar a mano, en vez de
intentar OCR.

## Watcher de OneDrive (opcional, Sprint F)

Proceso de consola separado que vigila una carpeta de OneDrive y
importa cotizaciones nuevas automáticamente al CRM, sin que tengas
que usar el importador manual. Corre en paralelo al servidor web (son
2 procesos independientes que comparten la misma base de datos).

**Windows:**
```bat
watcher\run_watcher.bat "C:\Users\TuUsuario\OneDrive\Cotizaciones"
```
(o doble click en `run_watcher.bat` y te pide la ruta)

**Linux/Mac:**
```bash
python watcher/watcher_service.py "/ruta/a/OneDrive/Cotizaciones"
```

Carpetas que el watcher ignora a propósito (manuales, fotos,
catálogos de partes, plantillas — ver `watcher/watcher_config.py` para
la lista completa). Cuando importa algo nuevo, aparece un toast en el
CRM web la próxima vez que esté abierto (polling cada 30s) — no hace
falta que el watcher y el navegador estén abiertos al mismo tiempo,
los archivos quedan importados igual, el aviso es solo para que te
enteres si tenías el CRM abierto en ese momento.

## Variables de entorno IA

```text
GEMINI_API_KEY=...   # Gemini
GROK_API_KEY=...     # xAI / Grok
ANTHROPIC_API_KEY=... # Claude, si se usa ese proveedor
```

## Multiusuario

Por seguridad, por defecto la app abre solo en esta PC. Para usarla en red, iniciar con `HOST=0.0.0.0` y acceder desde otras máquinas con:

```text
http://IP-DE-LA-PC:5000
```

Para cambiar puerto:

```bash
HOST=0.0.0.0 PORT=8080 ./iniciar_crm_web.sh
```

## Verificar instalación

```bash
.venv\Scripts\python.exe verificar_instalacion.py
```

En Linux/Mac:

```bash
.venv/bin/python verificar_instalacion.py
```
