@echo off
REM ============================================================
REM  Meteo allenamento Sharks - doppio click: verdetto per i gruppi di oggi.
REM  Il testo resta anche in ultimo_esito.txt e in esiti\.
REM  Si possono aggiungere opzioni, es.:  Meteo-Allenamento.bat --gruppi tutti
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\fabri\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python
"%PY%" "%~dp0meteo_allenamento.py" %*
echo.
pause
