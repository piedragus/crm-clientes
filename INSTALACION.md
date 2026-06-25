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
