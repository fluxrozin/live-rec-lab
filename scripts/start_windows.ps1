[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu",
    [string]$Repo = "~/projects/live-rec-lab",
    [string]$Uv = "~/.local/bin/uv",
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    [string]$InstanceName = "default",
    [switch]$Register
)

$ErrorActionPreference = "Stop"

function Quote-NativeArgument([string]$Value) {
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

if ($Repo -notmatch '^(~/|/)' -or $Uv -notmatch '^(~/|/)') {
    throw "-Repo と -Uv は WSL 内の絶対パス、または ~/ からのパスを指定してください。"
}

$TaskName = "LiveRecorder-Lab-$InstanceName"
$Wsl = Join-Path $env:SystemRoot "System32\wsl.exe"
$Launch = 'repo=$1; uv=$2; if [[ "$repo" == "~/"* ]]; then repo=$HOME/${repo:2}; fi; if [[ "$uv" == "~/"* ]]; then uv=$HOME/${uv:2}; fi; cd -- "$repo" && exec "$uv" run --locked live-rec-lab start'
$Arguments = (@("--distribution", $Distro, "--exec", "/bin/bash", "-c", $Launch, "live-rec-lab", $Repo, $Uv) |
    ForEach-Object { Quote-NativeArgument $_ }) -join " "

if (-not $Register) {
    Write-Output "プレビュー $TaskName"
    Write-Output ("{0} {1}" -f (Quote-NativeArgument $Wsl), $Arguments)
    exit 0
}

if (Get-ScheduledTask -TaskPath "\" -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "同名のタスク $TaskName が既に登録されています。"
}

$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $Wsl -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskPath "\" -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings -Description "Start LiveRecorder in WSL after Windows sign-in." |
    Out-Null
Write-Output "サインイン時のタスク $TaskName を登録しました。"
