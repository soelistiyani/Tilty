@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Instalasi gagal. Pastikan Python atau environment Anaconda sudah aktif.
  pause
  exit /b 1
)
echo Instalasi selesai. Jalankan run_monitor.bat.
pause
