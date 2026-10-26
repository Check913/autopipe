@echo off
schtasks /Delete /TN "AutoPipe 24-7" /F
echo Tache planifiee supprimee (AutoPipe ne demarrera plus tout seul).
pause