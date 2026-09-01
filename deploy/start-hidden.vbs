Option Explicit
' Chat Data Extractor - silent autostart launcher.
' Starts "py -m app" with NO visible window and appends all output to data\app.log.
' Used by a Task Scheduler "at log on" task, or dropped in the Startup folder.
' Edit ROOT below if you move the project.

Dim shell, fso, root, logPath, cmd
root = "C:\Users\bhanu\Projects\chat-data-extractor"

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
logPath = root & "\data\app.log"

' Roll the log over once it passes ~5 MB so it never grows without bound.
If fso.FileExists(logPath) Then
  If fso.GetFile(logPath).Size > 5242880 Then
    If fso.FileExists(logPath & ".old") Then fso.DeleteFile(logPath & ".old")
    fso.MoveFile logPath, logPath & ".old"
  End If
End If

' cmd /c keeps a (hidden) console so stdout/stderr exist and are captured to the log.
cmd = "cmd /c cd /d """ & root & """ && py -m app >> """ & logPath & """ 2>&1"

' Window style 0 = hidden; False = don't wait (launcher exits immediately).
shell.Run cmd, 0, False
