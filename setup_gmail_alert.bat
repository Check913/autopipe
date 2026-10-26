@echo off
chcp 65001 >nul
title AutoPipe - Configuration alerte Gmail
cd /d "%~dp0"
echo.
echo   ============================================
echo    AutoPipe : configuration de l'alerte Gmail
echo   ============================================
echo.
echo   Il te faut un mot de passe d'application Google (16 caracteres):
echo   https://myaccount.google.com/apppasswords
echo   (active d'abord la validation en 2 etapes si ce n'est pas fait)
echo.
py -3.10 setup_gmail_alert.py
if errorlevel 1 (
  echo.
  echo   *** Configuration incomplete *** Lis le message au-dessus.
  pause
  exit /b 1
)
echo.
pause >nul