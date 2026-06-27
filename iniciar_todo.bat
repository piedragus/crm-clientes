@echo off
REM iniciar_todo.bat — arranca el servidor web Y el watcher de OneDrive
REM en 2 ventanas separadas (peer review PR #34, punto 3: aislar los
REM procesos en código, pero unirlos en el despliegue para no tener
REM que acordarse de arrancar 2 cosas a mano).
REM
REM Uso: doble click, o desde consola:
REM   iniciar_todo.bat "C:\Users\TuUsuario\OneDrive\Cotizaciones"

setlocal
cd /d "%~dp0"

if "%~1"=="" (
    set /p ONEDRIVE_PATH="Ruta de la carpeta de OneDrive a vigilar: "
) else (
    set ONEDRIVE_PATH=%~1
)

echo Iniciando servidor web...
start "CRM - Servidor web" cmd /k iniciar_crm_web.bat

echo Iniciando watcher de OneDrive...
start "CRM - Watcher OneDrive" cmd /k watcher\run_watcher.bat "%ONEDRIVE_PATH%"

echo.
echo Listo — 2 ventanas abiertas (servidor + watcher).
echo Cerrar cada una por separado para detenerlas.
