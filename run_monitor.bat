@echo off
cd /d "%~dp0"
python d701_monitor.py
if errorlevel 1 pause
