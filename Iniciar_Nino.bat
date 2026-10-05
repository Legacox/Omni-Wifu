@echo off
title Nino AI
cd /d "%~dp0"

echo ==================================================
echo   Abriendo firewall para acceso desde movil...
echo ==================================================
netsh advfirewall firewall delete rule name="Nino AI - Puerto 8080" >nul 2>&1
netsh advfirewall firewall add rule name="Nino AI - Puerto 8080" dir=in action=allow protocol=TCP localport=8080 >nul 2>&1

echo ==================================================
echo   Iniciando Nino AI...
echo ==================================================
".\venv\Scripts\python.exe" main.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ocurrio un error al iniciar. Presiona cualquier tecla para salir.
    pause >nul
)
