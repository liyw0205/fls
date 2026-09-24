#define SourceDir GetEnv("FLS_SOURCE_DIR")
#define OutputDir GetEnv("FLS_OUTPUT_DIR")
#define AppVersion GetEnv("FLS_APP_VERSION")
#define ChineseMessagesFile GetEnv("FLS_CHINESE_MESSAGES_FILE")

[Setup]
AppId={{5C2A4EA8-2DAA-4B9C-AB68-6D3C0D0E0F15}
AppName=FLS Manager
AppVersion={#AppVersion}
AppPublisher=liyw0205
DefaultDirName={localappdata}\FLS
DefaultGroupName=FLS Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=FLS-Manager-Setup-Windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=FLS Manager
CloseApplications=yes
ChangesEnvironment=no

[Languages]
Name: "chinesesimp"; MessagesFile: "{#ChineseMessagesFile}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{tmp}\fls-payload"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\FLS Manager"; Filename: "{app}\fls.bat"; Parameters: "start"; WorkingDir: "{app}"
Name: "{group}\FLS Manager 状态"; Filename: "{app}\fls.bat"; Parameters: "status"; WorkingDir: "{app}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\fls-payload\install.ps1"" -InstallDir ""{app}"" -NoStart"; StatusMsg: "正在安装 Python 环境和 FLS 依赖..."; Flags: waituntilterminated runasoriginaluser
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\fls.ps1"" start"; StatusMsg: "正在启动 FLS Manager..."; Check: FLSPythonReady; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
function FLSPythonReady(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\.venv\Scripts\python.exe'));
end;
