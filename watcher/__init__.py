"""
Package watcher — Sprint F: vigila una carpeta de OneDrive y dispara
la importación automática de cotizaciones nuevas.

Componentes:
- watcher_config.py  — exclusiones, extensiones, debounce
- watcher_handler.py — maneja eventos de filesystem (watchdog)
- watcher_service.py — loop principal, punto de entrada por consola
"""
