# Registers a current-user Windows logon task when run explicitly.
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Repository,
    [Parameter(Mandatory)][string]$Uv,
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$TaskName = 'LiveRecorder',
    [ValidateSet('speech', 'speech-cuda')][string]$Extra = 'speech-cuda'
)

$ErrorActionPreference = 'Stop'
foreach ($value in @($Repository, $Uv, $Distribution, $TaskName)) {
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match '["\r\n]') {
        throw 'Invalid scheduled-task argument.'
    }
}
foreach ($path in @($Repository, $Uv)) {
    if (-not $path.StartsWith('/') -or $path.Contains('\')) {
        throw 'Repository and Uv must be absolute WSL paths.'
    }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'A task with that name already exists.'
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '--distribution "{0}" --cd "{1}" --exec "{2}" run --locked --extra {3} live-rec-lab start' -f $Distribution, $Repository, $Uv, $Extra
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wsl.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

if ($PSCmdlet.ShouldProcess($TaskName, 'Register Windows logon task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
}
