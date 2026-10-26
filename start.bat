@echo off
REM AutoPipe - continuous 24/7 mode (posts at US peak hours: 15 & 21 UTC)
cd /d "%~dp0"
python -m autopipe.cli serve
pause
