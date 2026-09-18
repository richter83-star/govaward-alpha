Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

REM Launch resilient 24/7 VPS GovAward terminal tunnel watchdog in silent background mode (0 = SW_HIDE)
targetScript = scriptDir & "\scripts\tunnel_vps_terminal.py"
sysPython = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\python.exe"
venvPython = scriptDir & "\.venv\Scripts\python.exe"

If fso.FileExists(sysPython) Then
    WshShell.Run """" & sysPython & """ """ & targetScript & """", 0, False
ElseIf fso.FileExists(venvPython) Then
    WshShell.Run """" & venvPython & """ """ & targetScript & """", 0, False
Else
    WshShell.Run "python """ & targetScript & """", 0, False
End If
