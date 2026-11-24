; Script único Inno Setup 6 para o Estoke Ao Cubo (Full e Trial)
; Recebe parametros via linha de comando ISCC.exe:
;   /DMyAppVersion=4.6.0
;   /DMyAppEdition=full|trial

#ifndef MyAppVersion
  #define MyAppVersion "4.6.0"
#endif

#ifndef MyAppEdition
  #define MyAppEdition "full"
#endif

#if MyAppEdition == "trial"
  #define MyAppName "Estoke Ao Cubo (Trial)"
  #define MyDistFolder "EstokeAoCubo_Trial"
  #define MyExeName "EstokeAoCubo_Trial.exe"
  #define MyOutputBaseFilename "EstokeAoCubo_Trial_Setup_v" + MyAppVersion
#else
  #define MyAppName "Estoke Ao Cubo"
  #define MyDistFolder "EstokeAoCubo"
  #define MyExeName "EstokeAoCubo.exe"
  #define MyOutputBaseFilename "EstokeAoCubo_Setup_v" + MyAppVersion
#endif

#ifndef MyDistSource
  #define MyDistSource "dist\" + MyDistFolder
#endif

[Setup]
; Mesmo AppId para que instalar a versao Full substitua o Trial sem duplicar entrada no Painel de Controle
AppId={{D3E83C56-B1A4-4A68-9A7B-9A3E4562B2F4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=Estoke Ao Cubo
AppPublisherURL=https://dommotta.com
DefaultDirName={autopf}\Estoke Ao Cubo
DefaultGroupName=Estoke Ao Cubo
AllowNoIcons=yes
LicenseFile=dist_package\TERMOS_DE_USO.txt
OutputDir=release
OutputBaseFilename={#MyOutputBaseFilename}
SetupIconFile=icon\icon-cubo.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyExeName}
VersionInfoVersion=4.6.0.0
VersionInfoCompany=Estoke Ao Cubo
VersionInfoDescription=Estoke Ao Cubo - Sistema de Gestao Comercial e PDV
VersionInfoCopyright=Copyright (C) 2026 Estoke Ao Cubo
VersionInfoProductName=Estoke Ao Cubo
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "firewallrule"; Description: "Liberar porta 5050 TCP no Firewall do Windows (App Mobile / PDV Web no Wi-Fi)"; GroupDescription: "Rede e Integracoes:"; Flags: unchecked

[Files]
; Pacote completo --onedir gerado pelo PyInstaller
Source: "{#MyDistSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Visual C++ 2015-2022 Redistributable x64 (>= 14.44 para Python 3.14 MSVC v1944)
Source: "build_assets\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Estoke Ao Cubo"; Filename: "{app}\{#MyExeName}"; IconFilename: "{app}\{#MyExeName}"
Name: "{group}\Guia do Usuario"; Filename: "{app}\_internal\GUIA_DO_USUARIO.txt"
Name: "{group}\Desinstalar Estoke Ao Cubo"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Estoke Ao Cubo"; Filename: "{app}\{#MyExeName}"; Tasks: desktopicon

[Run]
; Instala VC++ Redistributable x64 silenciosamente se ausente ou < 14.44
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Instalando Microsoft Visual C++ Redistributable (x64)..."; Check: NeedsVCRedist; Flags: waituntilterminated

; Regra idempotente de Firewall (remove anterior antes de adicionar para evitar duplicatas)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Estoke Ao Cubo API (TCP 5050)"""; Tasks: firewallrule; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Estoke Ao Cubo API (TCP 5050)"" dir=in action=allow protocol=TCP localport=5050"; Tasks: firewallrule; Flags: runhidden waituntilterminated

; Iniciar apos concluir
Filename: "{app}\{#MyExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Estoke Ao Cubo API (TCP 5050)"""; Flags: runhidden waituntilterminated; RunOnceId: "DelFirewallEstoke5050"

[UninstallDelete]
; Preserva 100% dos bancos de dados, backups e configs em {localappdata}\EstokeAoCubo
Type: files; Name: "{app}\*.log"

[Code]
function NeedsVCRedist(): Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKEY_LOCAL_MACHINE, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then
  begin
    if (Installed = 1) and
       RegQueryDWordValue(HKEY_LOCAL_MACHINE, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major) and
       RegQueryDWordValue(HKEY_LOCAL_MACHINE, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor) then
    begin
      if (Major > 14) or ((Major = 14) and (Minor >= 44)) then
        Result := False;
    end;
  end;
end;
