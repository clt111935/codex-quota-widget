param(
    [Parameter(Mandatory=$true)][ValidateSet('Status','Enable','Disable','Run')][string]$Operation,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$ScriptPath,
    [Parameter(Mandatory=$true)][string]$PythonwPath
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding

function Find-Task($Folder, $Name) {
    try { return $Folder.GetTask($Name) }
    catch {
        $cause = $_.Exception
        while ($cause.InnerException) { $cause = $cause.InnerException }
        $code = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$cause.HResult), 0)
        if ($code -eq 2147942402 -or $code -eq 2147942403 -or $code -eq 2147750671) { return $null }
        throw
    }
}

function Test-CurrentUser($UserId, $Sid) {
    if ($UserId -eq $Sid) { return $true }
    try {
        $account = New-Object System.Security.Principal.NTAccount($UserId)
        return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $Sid
    }
    catch { return $false }
}

try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User.Value
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    $task = Find-Task $folder $TaskName
    $arguments = '"' + $ScriptPath + '" --background'
    if ($Operation -eq 'Enable') {
        if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw 'Entry script is missing.' }
        if (-not (Test-Path -LiteralPath $PythonwPath -PathType Leaf)) { throw 'Python runtime is missing.' }
        $definition = $service.NewTask(0)
        $definition.RegistrationInfo.Description = 'Codex quota widget: current-user logon, independent of the Codex desktop process.'
        $definition.Principal.UserId = $sid
        $definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN; no stored password
        $definition.Principal.RunLevel = 0  # TASK_RUNLEVEL_LUA; never request elevation
        $trigger = $definition.Triggers.Create(9) # TASK_TRIGGER_LOGON
        $trigger.UserId = $sid
        $trigger.Delay = 'PT10S'
        $settings = $definition.Settings
        $settings.Enabled = $true
        $settings.AllowDemandStart = $true
        $settings.StartWhenAvailable = $true
        $settings.DisallowStartIfOnBatteries = $false
        $settings.StopIfGoingOnBatteries = $false
        $settings.ExecutionTimeLimit = 'PT0S'
        $settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
        $settings.RestartInterval = 'PT1M'
        $settings.RestartCount = 10
        $action = $definition.Actions.Create(0)
        $action.Path = $PythonwPath
        $action.Arguments = $arguments
        $action.WorkingDirectory = [System.IO.Path]::GetDirectoryName($ScriptPath)
        $task = $folder.RegisterTaskDefinition($TaskName, $definition, 6, $sid, $null, 3, $null)
    }
    elseif ($Operation -eq 'Disable') {
        if ($task) { $folder.DeleteTask($TaskName, 0) }
        $task = $null
    }
    elseif ($Operation -eq 'Run') {
        if (-not $task) { throw 'The startup task is not installed.' }
        $instance = $task.Run($null)
    }
    if ($task) {
        $definition = $task.Definition
        $action = $definition.Actions.Item(1)
        $trigger = $null
        if ($definition.Triggers.Count -gt 0) { $trigger = $definition.Triggers.Item(1) }
        $triggerMatches = $null -ne $trigger -and $trigger.Type -eq 9 -and $trigger.Enabled -and (Test-CurrentUser $trigger.UserId $sid)
        $matches = $action.Path -ieq $PythonwPath -and $action.Arguments -ceq $arguments -and
            $action.WorkingDirectory -ieq ([System.IO.Path]::GetDirectoryName($ScriptPath)) -and
            (Test-CurrentUser $definition.Principal.UserId $sid) -and
            $definition.Principal.LogonType -eq 3 -and $definition.Principal.RunLevel -eq 0 -and
            $definition.Settings.MultipleInstances -eq 2 -and $triggerMatches
        @{ installed=$true; enabled=([bool]$task.Enabled -and $matches); valid=$matches; mechanism='scheduled_task';
           task_name=$TaskName; state=[int]$task.State; last_result=[int]$task.LastTaskResult;
           run_level=[int]$definition.Principal.RunLevel; logon_type=[int]$definition.Principal.LogonType;
           multiple_instances=[int]$definition.Settings.MultipleInstances; restart_count=[int]$definition.Settings.RestartCount;
           restart_interval=$definition.Settings.RestartInterval; trigger_user_matches=[bool]$triggerMatches;
           command=$PythonwPath; arguments=$action.Arguments; working_directory=$action.WorkingDirectory } | ConvertTo-Json -Compress
    }
    else {
        @{ installed=$false; enabled=$false; mechanism='scheduled_task'; task_name=$TaskName } | ConvertTo-Json -Compress
    }
}
catch {
    $cause = $_.Exception
    while ($cause.InnerException) { $cause = $cause.InnerException }
    @{ error='task_scheduler_operation_failed'; operation=$Operation; hresult=('{0:X8}' -f $cause.HResult) } | ConvertTo-Json -Compress
    exit 1
}
