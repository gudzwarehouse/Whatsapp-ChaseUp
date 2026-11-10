@echo off
REM Starts the WhatsApp bridge (port 3001) and restarts it if it ever exits.
REM Uses the saved session in whatsapp-service\auth_info -- no QR needed.
title WhatsApp Service
cd /d "%~dp0whatsapp-service"

:loop
echo [%date% %time%] Starting WhatsApp service...
node index.js
echo [%date% %time%] Service exited (code %errorlevel%). Restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
