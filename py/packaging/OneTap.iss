; 一键签 Windows 安装包（Inno Setup 6）
;   ISCC.exe /DAppVersion=0.1.0 packaging\OneTap.iss
; 前置：dist\一键签\（PyInstaller onedir 输出）、packaging\drivers\AppleMobileDeviceSupport64.msi
; 苹果手机驱动来自苹果官方 iTunes 安装包（iTunes64Setup.exe /extract 得到），只装驱动不装 iTunes。

#ifndef AppVersion
  #define AppVersion "0.2.5"
#endif

[Setup]
AppId={{8F3C1A52-7E4B-4C61-9B2A-6D1E0F4A7C93}
AppName=一键签
AppVersion={#AppVersion}
AppVerName=一键签 {#AppVersion}
AppPublisher=OneDuo
DefaultDirName={autopf}\OneTapInstall
DefaultGroupName=一键签
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=OneDuo-{#AppVersion}-windows-x64-setup
SetupIconFile=OneTap.ico
UninstallDisplayIcon={app}\一键签.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 驱动要装系统服务，必须管理员
PrivilegesRequired=admin
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chs.Driver=正在安装苹果手机驱动（Apple Mobile Device Support）…
en.Driver=Installing Apple Mobile Device Support…
chs.Bonjour=正在安装 Bonjour（让电脑通过 Wi‑Fi 找到手机）…
en.Bonjour=Installing Bonjour (find iPhone over Wi-Fi)…
chs.DesktopIcon=创建桌面快捷方式
en.DesktopIcon=Create a desktop shortcut
chs.AutoStart=开机自动启动（后台自动续签，推荐）
en.AutoStart=Start at login (auto-renew in background, recommended)
chs.Launch=打开一键签
en.Launch=Launch OneDuo

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"
Name: "autostart"; Description: "{cm:AutoStart}"

[Files]
Source: "..\dist\一键签\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "drivers\AppleMobileDeviceSupport64.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedAppleDriver
Source: "drivers\Bonjour64.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedBonjour

[Icons]
Name: "{autoprograms}\一键签"; Filename: "{app}\一键签.exe"
Name: "{autodesktop}\一键签"; Filename: "{app}\一键签.exe"; Tasks: desktopicon
Name: "{userstartup}\一键签"; Filename: "{app}\一键签.exe"; Parameters: "--background"; Tasks: autostart

[Run]
Filename: "msiexec.exe"; Parameters: "/i ""{tmp}\AppleMobileDeviceSupport64.msi"" /qn /norestart"; StatusMsg: "{cm:Driver}"; Flags: waituntilterminated; Check: NeedAppleDriver
Filename: "msiexec.exe"; Parameters: "/i ""{tmp}\Bonjour64.msi"" /qn /norestart"; StatusMsg: "{cm:Bonjour}"; Flags: waituntilterminated; Check: NeedBonjour
; 关键：苹果手机服务启动时 Bonjour 还没装，就永远不会去 Wi‑Fi 上找手机（实测：重启这个服务后 15 秒内认到）
Filename: "{sys}\cmd.exe"; Parameters: "/c net stop ""Apple Mobile Device Service"" & net start ""Apple Mobile Device Service"""; StatusMsg: "{cm:Bonjour}"; Flags: runhidden waituntilterminated
Filename: "{app}\一键签.exe"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent
; 一键签自己点「立即更新」时是静默安装：装完以原用户身份重新打开
Filename: "{app}\一键签.exe"; Flags: nowait runasoriginaluser; Check: WizardSilent

[InstallDelete]
; 改名前叫「一键装」：清掉旧快捷方式和旧主程序，免得开始菜单 / 桌面 / 开机启动里有两个
Type: files; Name: "{autoprograms}\一键装.lnk"
Type: files; Name: "{autodesktop}\一键装.lnk"
Type: files; Name: "{userstartup}\一键装.lnk"
Type: files; Name: "{app}\一键装.exe"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
{ 已经装了 iTunes / Apple 设备 App / 驱动：服务在就不重复装 }
function NeedAppleDriver: Boolean;
var
  S: String;
begin
  Result := not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service', 'ImagePath', S);
end;

{ Bonjour：Windows 上通过 Wi‑Fi 发现手机要靠它（iTunes 新版已不带，单独装苹果官方的 Bonjour64.msi） }
function NeedBonjour: Boolean;
var
  S: String;
begin
  Result := not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Services\Bonjour Service', 'ImagePath', S);
end;
