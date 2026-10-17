; Inno Setup script: wraps the PyInstaller --onedir folder (dist\Photon Finish\) into a
; single "dist\Photon Finish Setup.exe". Compiled by packaging\build_app_windows.ps1, which
; passes /DAppVersion from seestar\__init__.py -- do not run ISCC on this file by hand without it.
;
; Why an installer and not a zip of the folder (2026-10-17): the folder build avoids the
; Defender Wacatac.B!ml detection the --onefile exe earned, but Explorer copies a downloaded
; zip's Mark of the Web onto every extracted file, and .NET Framework then refuses to load
; pythonnet's Python.Runtime.dll -- the window never opens. Files an installer writes carry
; no such mark. It also removes the "double-click the exe inside the zip" failure.
;
; Per-user install (PrivilegesRequired=lowest): no UAC prompt, installs to
; %LOCALAPPDATA%\Programs\Photon Finish. Unsigned, like the exe it wraps: SmartScreen still
; shows "Windows protected your PC" once for the installer.

#ifndef AppVersion
  #error AppVersion is not defined -- build through packaging\build_app_windows.ps1
#endif

#define AppName "Photon Finish"
#define AppExe "Photon Finish.exe"

[Setup]
; Never change AppId: Windows uses it to recognise an upgrade of an existing install.
AppId={{2C7FBF53-299B-46C7-AC82-ABF003D94400}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Photon Finish
AppPublisherURL=https://github.com/ColinB-WB/photon-finish
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename={#AppName} Setup
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade must not leave the previous version's libraries beside the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
