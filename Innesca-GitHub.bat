@echo off
REM ============================================================
REM  Meteo allenamento Sharks - INNESCO DAL PC (toppa del 21/10/2026)
REM  Lo lancia il Task Scheduler di Windows "Meteo Sharks - innesco GitHub"
REM  lun e mer alle 15:32 e 17:32, dom e mar alle 23:02.
REM  Chiede a GitHub di far partire il workflow "meteo.yml": e' fotografa.py,
REM  lassu', a decidere cosa fare in base all'ora italiana.
REM  Perche' esiste: dal 14 al 21/10/2026 il cron di GitHub e' partito 23 volte
REM  invece di ~380, a orari sparsi; lunedi' 19/10 i controlli delle 15:30 e
REM  17:30 sono saltati. Funziona solo a PC acceso: e' una toppa.
REM  Usa il gh del PC, gia' autenticato come tremma47. Nessun segreto qui.
REM ============================================================
chcp 65001 >nul
set GH="C:\Program Files\GitHub CLI\gh.exe"
set LOG="%~dp0innesco.log"
echo %date% %time% >> %LOG%
%GH% workflow run meteo.yml -R tremma47/meteo-allenamento -f forza=false >> %LOG% 2>&1
echo esito %errorlevel% >> %LOG%
