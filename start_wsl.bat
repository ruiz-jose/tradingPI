@echo off
:: ============================================================
:: start_wsl.bat  —  Lanza el trading bot en WSL
:: Usado con Windows Task Scheduler para arranque automático
::
:: Configura las dos variables de abajo antes de usar:
::   DISTRO  = nombre de tu distro WSL  (ejecuta: wsl --list)
::   USUARIO = tu usuario dentro de WSL  (ejecuta en WSL: whoami)
:: ============================================================

set DISTRO=Ubuntu
set USUARIO=tuusuario

wsl -d %DISTRO% -- bash -c "cd /home/%USUARIO%/tradingPI && source venv/bin/activate && python3 bot.py >> /home/%USUARIO%/tradingPI/bot.log 2>&1"
