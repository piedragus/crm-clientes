@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set "PY=python"
  ) else (
    echo No se encontro Python. Instala Python 3.10+ desde https://www.python.org/downloads/
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto error
)

set "VPY=.venv\Scripts\python.exe"
set "PIP=.venv\Scripts\python.exe -m pip"

%VPY% -c "import flask, pandas, openpyxl, pdfplumber, docx, pptx, google.genai, openai, anthropic, chardet" >nul 2>nul
if errorlevel 1 (
  echo Instalando dependencias ...
  %PIP% install --upgrade pip
  %PIP% install -r requirements.txt
  if errorlevel 1 goto error
) else (
  echo Dependencias OK.
)

echo.
echo Abriendo CRM Web en http://localhost:5000
start "" http://localhost:5000
%VPY% server.py
goto end

:error
echo.
echo Error iniciando CRM. Revisa la salida anterior.
pause
exit /b 1

:end
pause
