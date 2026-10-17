' 校园网自动登录 —— 开机自启（启动文件夹方式）
' 静默拉起后台守护进程（pythonw，无控制台窗口、不弹任何对话框）。
' 与计划任务 CampusNetAutoLogin 并存；守护进程有单实例互斥，重复启动会立即退出，不会重复登录。
' 卸载：删除本文件，并移除计划任务 CampusNetAutoLogin。
'
' 注意：本文件不做任何路径硬编码 —— 脚本位置由自身所在目录推导，
' pythonw.exe 依次按常见安装位置和 PATH 查找，换电脑/换 Python 版本都不用改。

Option Explicit

Dim fso, ws, script, pyw, candidates, i

Set fso = CreateObject("Scripting.FileSystemObject")
Set ws  = CreateObject("WScript.Shell")

' 本文件位于 <项目根>\tools\ ，主脚本在项目根目录
script = fso.BuildPath(fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)), "campus_login.py")

If Not fso.FileExists(script) Then
    ' 找不到主脚本就安静退出，不要弹窗
    WScript.Quit 1
End If

pyw = ""
candidates = Array( _
    ws.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"), _
    ws.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
    ws.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"), _
    ws.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"), _
    ws.ExpandEnvironmentStrings("%ProgramFiles%\Python313\pythonw.exe"), _
    ws.ExpandEnvironmentStrings("%ProgramFiles%\Python312\pythonw.exe"), _
    ws.ExpandEnvironmentStrings("%ProgramFiles%\Python311\pythonw.exe"), _
    "C:\Python313\pythonw.exe", _
    "C:\Python312\pythonw.exe", _
    "C:\Python311\pythonw.exe", _
    "C:\Python310\pythonw.exe" _
)

For i = 0 To UBound(candidates)
    If fso.FileExists(candidates(i)) Then
        pyw = candidates(i)
        Exit For
    End If
Next

' 退回到 PATH 里的 pythonw.exe（多数安装方式会写进 PATH）
If pyw = "" Then
    pyw = "pythonw.exe"
End If

ws.Run Chr(34) & pyw & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
