@echo off
REM ============================================================
REM  AutoPipe 24/7 - mode serve avec AUTO-REDEMARRAGE
REM  Si le bot plante, il est relance 60 s plus tard, seul.
REM  Journal dedie : autopipe_247.log (a cote de ce fichier).
REM  Fermer cette fenetre = arreter le bot.
REM ============================================================
title AutoPipe 24/7
cd /d "%~dp0"
set LOG=autopipe_247.log

:loop
echo [%date% %time%] === Demarrage AutoPipe (mode serve) === >> "%LOG%"
py -3.10 -m autopipe.cli serve >> "%LOG%" 2>&1
echo [%date% %time%] === Arret inattendu (code %errorlevel%) - redemarrage dans 60 s === >> "%LOG%"
timeout /t 60 /nobreak >nul
goto loop