@echo off
REM ============================================================
REM  Cree la tache planifiee "AutoPipe 24-7" : start_247.bat
REM  se lancera TOUT SEUL a chaque ouverture de session Windows
REM  (donc aussi apres un reboot si tu laisses la session ouverte
REM  ou l'ouvres automatiquement).
REM ============================================================
schtasks /Create /F /TN "AutoPipe 24-7" /SC ONLOGON /RL HIGHEST /TR "\"%~dp0start_247.bat\""
if %errorlevel%==0 (
  echo [OK] Tache creee. AutoPipe demarrera tout seul a l'ouverture de session.
  echo      Pour le lancer MAINTENANT : schtasks /Run /TN "AutoPipe 24-7"
  echo      Pour verifier            : schtasks /Query /TN "AutoPipe 24-7"
) else (
  echo [ECHEC] Relance ce fichier en tant qu'administrateur.
)
pause