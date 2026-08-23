# One-time: register a Task Scheduler task that starts Chat Data Extractor at every log on.
# You run this yourself (no admin needed for a per-user "at log on" task):
#
#     powershell -ExecutionPolicy Bypass -File deploy\register-autostart.ps1
#
# It only *registers* the task — it does not change any other Windows settings.
# Inspect it first if you like. Undo any time with:
#     Unregister-ScheduledTask -TaskName ChatDataExtractor -Confirm:$false

$ErrorActionPreference = "Stop"
$root = "C:\Users\bhanu\Projects\chat-data-extractor"
$vbs  = Join-Path $root "deploy\start-hidden.vbs"
if (-not (Test-Path $vbs)) { throw "launcher not found: $vbs" }

$action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3 `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "ChatDataExtractor" `
    -Description "Starts the Chat Data Extractor capture app at log on (hidden window; logs to data\app.log)." `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered task 'ChatDataExtractor' — it will run at your next log on."
Write-Host "Start it now without rebooting:  Start-ScheduledTask -TaskName ChatDataExtractor"
Write-Host "Watch the log:                   Get-Content data\app.log -Wait -Tail 20"
