@echo off
REM run_watcher.bat — Sprint F: vigila una carpeta de OneDrive y
REM importa cotizaciones nuevas automaticamente al CRM.
REM
REM Uso: doble click, o desde consola:
REM   run_watcher.bat "C:\Users\TuUsuario\OneDrive\Cotizaciones"
REM
REM Si no se pasa una ruta, pide que la escribas.

cd /d "%~dp0\.."

if not exist ".venv\Scripts\activate.bat" (
    echo No se encontro el entorno virtual .venv
    echo Corre primero iniciar_crm_web.bat para crearlo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if "%~1"=="" (
    set /p ONEDRIVE_PATH="Ruta de la carpeta de OneDrive a vigilar: "
) else (
    set ONEDRIVE_PATH=%~1
)

echo.
echo Iniciando watcher sobre: %ONEDRIVE_PATH%
echo Cerrar esta ventana (o Ctrl+C) para detenerlo.
echo.

python watcher\watcher_service.py "%ONEDRIVE_PATH%"

pause
