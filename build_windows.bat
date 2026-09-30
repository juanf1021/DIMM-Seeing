@echo off
REM Genera dist\DIMM-Seeing.exe (ejecutar en Windows con Python 3.11+ instalado)
setlocal
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :error

pyinstaller --noconfirm --onefile --name DIMM-Seeing ^
    --add-data "dimm_app\static;dimm_app\static" ^
    --collect-submodules uvicorn ^
    --hidden-import win32com.client --hidden-import pythoncom ^
    run.py || goto :error

echo.
echo Listo: dist\DIMM-Seeing.exe
goto :eof

:error
echo Error durante la compilacion.
exit /b 1
