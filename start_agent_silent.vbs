' Shuiyuan Agent one-click launcher (silent version)
' Double-click to run: no console window pops up.
' If the service is not running, it starts in a minimized window
' and opens the browser automatically once the service is ready.
' To stop the service: close the "Shuiyuan Agent Service" window.
Option Explicit

Dim fso, ws, dir, i

Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = dir

' Start the service in a minimized window if port 8000 is not listening
If Not IsPortListening() Then
    ws.Run "cmd /c ""cd /d """ & dir & """ && python web_app.py""", 7, False
End If

' Wait up to 60 seconds for the service to become ready, then open the browser
For i = 1 To 60
    WScript.Sleep 1000
    If IsPortListening() Then Exit For
Next

ws.Run """http://127.0.0.1:8000"""

' Check whether port 8000 is listening
Function IsPortListening()
    Dim exec, out
    Set exec = ws.Exec("cmd /c netstat -ano | findstr ""LISTENING"" | findstr "":8000""")
    out = LCase(exec.StdOut.ReadAll())
    IsPortListening = (InStr(out, ":8000") > 0)
End Function
