; 智账 · PathOrbit AI Ledger 0.11.0-rc.5 Inno Setup Script
; 安装到 %LOCALAPPDATA%\Programs\UsageLedger（不需要管理员权限）
; 卸载只删除程序文件，不删 %LOCALAPPDATA%\UsageLedger\（用户数据）

[Setup]
AppId={{A3F8B2C1-4D5E-6F70-8A9B-0C1D2E3F4A5B}
AppName=智账
AppVersion=0.11.0-rc.5
AppPublisher=PathOrbit
DefaultDirName={localappdata}\Programs\UsageLedger
DefaultGroupName=智账
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=ZhiZhang-0.11.0-rc.5-win-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\UsageLedger.exe

[Files]
Source: "dist\UsageLedger\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\智账"; Filename: "{app}\UsageLedger.exe"
Name: "{group}\停止智账后台"; Filename: "{app}\UsageLedger.exe"; Parameters: "--stop"
Name: "{autodesktop}\智账"; Filename: "{app}\UsageLedger.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[UninstallDelete]
; 不删除 {localappdata}\UsageLedger —— 用户账本永久保留

[Run]
Filename: "{app}\UsageLedger.exe"; Description: "启动智账"; Flags: postinstall skipifsilent nowait
