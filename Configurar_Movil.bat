@echo off
title Configurar Nino AI (primera vez)
echo ==================================================
echo   Configurando acceso desde el movil...
echo ==================================================
netsh advfirewall firewall delete rule name="Nino AI - Puerto 8080" >nul 2>&1
netsh advfirewall firewall add rule name="Nino AI - Puerto 8080" dir=in action=allow protocol=TCP localport=8080
echo.
echo Listo. Ya puedes usar Iniciar_Nino.bat para conectarte desde el movil.
echo.
pause
