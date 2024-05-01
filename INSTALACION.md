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
