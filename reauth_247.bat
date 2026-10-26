@echo off
chcp 65001 >nul
title AutoPipe - Re-autorisation YouTube
cd /d "%~dp0"
echo.
echo   ============================================
echo    AutoPipe : re-autorisation YouTube
echo   ============================================
echo.
echo   Une fenetre de navigateur va s'ouvrir pour autoriser Google.
echo   Ne ferme pas cette fenetre pendant l'operation.
echo.
py -3.10 reauth_youtube.py
if errorlevel 1 (
  echo.
  echo   *** Echec *** Lis le message au-dessus et reessaie.
  echo   Si ca recommence, verifie ta connexion internet.
  pause
  exit /b 1
)
echo.
echo   ============================================
echo   Termine ! Le bot 24/7 repart tout seul.
echo   Tu peux fermer cette fenetre.
echo   ============================================
pause >nul