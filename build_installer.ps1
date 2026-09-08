$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectDir
python -m pip install -r requirements-build.txt
python -m unittest -v test_d701_monitor.py
python -m PyInstaller --noconfirm --clean --windowed --onedir --name Tilty --icon tilty.ico `
  --add-data "tilty.ico;." --add-data "tilty_logo_64.png;." `
  --exclude-module pandas --exclude-module scipy --exclude-module PyQt5 --exclude-module PyQt6 `
  --exclude-module PySide2 --exclude-module PySide6 d701_monitor.py
$candidates = @("$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
$iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 tidak ditemukan." }
& $iscc "installer.iss"
Write-Host "Installer selesai: output\Tilty-Setup.exe"
