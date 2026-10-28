Option Explicit
Dim appShell, fileSystem, projectRoot, command
Set appShell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & projectRoot & "\Start-Widget.ps1"""
appShell.Run command, 0, False
