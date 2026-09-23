#define AppName "FactorPunch Sync"
#define AppVersion "0.3.4"
#define AppPublisher "FactorPunch"
#define AppExeName "FactorPunch-Sync-Setup.exe"

[Setup]
AppId={{7EF1C889-A767-49D3-A29E-A85E990727B4}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\FactorPunch Sync
DefaultGroupName=FactorPunch Sync
OutputDir=..\..\output\windows
OutputBaseFilename=FactorPunch-Sync-Setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\app\frontend\public\favicon.svg
SetupLogging=yes

[Files]
Source: "..\stage\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\stage\mariadb\*"; DestDir: "{app}\mariadb"; Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall
Source: "..\stage\tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Configure.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "Backup.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\FactorPunch Sync\Logs"; Permissions: users-modify
Name: "{commonappdata}\FactorPunch Sync\Backups"; Permissions: users-modify

[Icons]
Name: "{group}\Open FactorPunch Sync"; Filename: "http://localhost:8000"
Name: "{group}\Run Backup Now"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Backup.ps1"" -InstallDir ""{app}"""; WorkingDir: "{app}"
Name: "{group}\Repair Configuration"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Configure.ps1"" -InstallDir ""{app}"" -Repair"; WorkingDir: "{app}"
Name: "{group}\Logs"; Filename: "{commonappdata}\FactorPunch Sync\Logs"
Name: "{group}\Uninstall FactorPunch Sync"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\tools\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Windows runtime prerequisites..."; Flags: waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Configure.ps1"" -InstallDir ""{app}"""; StatusMsg: "Configuring FactorPunch Sync..."; Flags: waituntilterminated
Filename: "http://localhost:8000"; Description: "Open FactorPunch Sync"; Flags: postinstall shellexec skipifsilent nowait

[UninstallRun]
Filename: "{sys}\sc.exe"; Parameters: "stop FactorPunchSync"; Flags: runhidden; RunOnceId: "StopAppService"
Filename: "{app}\tools\nssm.exe"; Parameters: "remove FactorPunchSync confirm"; Flags: runhidden; RunOnceId: "RemoveAppService"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""FactorPunch Sync"""; Flags: runhidden; RunOnceId: "RemoveFirewall"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := IsAdminInstallMode;
  if not Result then
    MsgBox('FactorPunch Sync must be installed as Administrator.', mbError, MB_OK);
end;
