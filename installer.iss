#define MyAppName "Tilty"
#define MyAppVersion "1.4.0"
#define MyAppExeName "Tilty.exe"
[Setup]
AppId={{A2ED436B-946F-4FA2-B08B-6EE4A6F8493C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\Tilty
DefaultGroupName={#MyAppName}
OutputDir=output
OutputBaseFilename=Tilty-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=tilty.ico
[Files]
Source: "dist\Tilty\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Tasks]
Name: "desktopicon"; Description: "Buat shortcut di Desktop"; GroupDescription: "Shortcut tambahan:"
[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Jalankan {#MyAppName}"; Flags: nowait postinstall skipifsilent
