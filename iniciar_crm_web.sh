#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Creando entorno virtual .venv ..."
  "$PYTHON" -m venv .venv
fi
VPY=.venv/bin/python
if ! "$VPY" -c "import flask, pandas, openpyxl, pdfplumber, docx, pptx, google.genai, openai, anthropic, chardet" >/dev/null 2>&1; then
  echo "Instalando dependencias ..."
  "$VPY" -m pip install --upgrade pip
  "$VPY" -m pip install -r requirements.txt
else
  echo "Dependencias OK."
fi
echo "CRM Web: http://localhost:${PORT:-5000}"
"$VPY" server.py
