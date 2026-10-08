param([switch]$VerifyLiveDeal, [string]$RepairRequisiteRequest, [switch]$RecoverDiagnostic, [string]$FinishConfirmedRequest, [switch]$ShowReminderPreview, [switch]$HideReminderCode, [switch]$StartIfStopped)
$ErrorActionPreference = 'Stop'
$botRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$hostingPath = Join-Path $botRoot 'data/server-hosting.json'
if (Test-Path -LiteralPath $hostingPath) {
    $hosting = Get-Content -LiteralPath $hostingPath -Raw | ConvertFrom-Json
    if ($hosting.active -eq $true) { throw 'Бот работает на VPS. Используйте systemctl --user restart fom-bitrix-bot.service на сервере.' }
}
$recordPath = Join-Path $botRoot 'data/registration-process.json'
$recordText = Get-Content -LiteralPath $recordPath -Raw
$record = $recordText | ConvertFrom-Json
$runtimePath = 'C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if ($record.workspace -ne $botRoot -or $record.executable -ne $runtimePath -or $record.mode -ne 'sales-bot') { throw 'Unexpected bot process record' }
$worker = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)"
$runPath = Join-Path $botRoot 'run.py'
$quotedCommand = '"{0}" "{1}" --sales-bot' -f $runtimePath, $runPath
$plainCommand = '"{0}" {1} --sales-bot' -f $runtimePath, $runPath
$expectedCommands = @($quotedCommand, $plainCommand)
if ($worker) {
    if ($worker.ExecutablePath -ne $runtimePath -or $worker.CommandLine -notin $expectedCommands) { throw 'Bot process identity did not match' }
    # Read the literal ISO timestamp before ConvertFrom-Json can coerce its timezone.
    $startedLiteral = [regex]::Match($recordText, '"started_at"\s*:\s*"([^"]+)"')
    if (-not $startedLiteral.Success) { throw 'Bot process timestamp is missing' }
    $started = [datetimeoffset]::Parse($startedLiteral.Groups[1].Value)
    $observed = [datetimeoffset]::Parse((Get-Process -Id $record.pid).StartTime.ToString('o'))
    if ([math]::Abs(($observed - $started).TotalSeconds) -gt 2) { throw 'Bot process creation time did not match' }
    Stop-Process -Id $record.pid
    Wait-Process -Id $record.pid -Timeout 10 -ErrorAction SilentlyContinue
} elseif (-not $StartIfStopped) {
    throw 'Bot process identity did not match; use StartIfStopped only after confirming no worker exists'
}
$qaFailure = $null
if ($HideReminderCode) {
    try {
        & $runtimePath -X utf8 (Join-Path $botRoot 'tools/hide_confirmed_reminder_code.py') --apply
        if ($LASTEXITCODE -ne 0) { $qaFailure = 'Reminder metadata cleanup was not confirmed; the bot will be restarted' }
    } catch {
        $qaFailure = 'Reminder metadata cleanup was not confirmed; the bot will be restarted'
    }
}
if ($ShowReminderPreview) {
    try {
        & $runtimePath -X utf8 (Join-Path $botRoot 'tools/launch_reminder_preview.py') --send-preview
        if ($LASTEXITCODE -ne 0) { $qaFailure = 'Reminder launch check was not confirmed; the bot will be restarted' }
    } catch {
        $qaFailure = 'Reminder launch check was not confirmed; the bot will be restarted'
    }
}
if ($FinishConfirmedRequest) {
    try {
        & $runtimePath -X utf8 (Join-Path $botRoot 'tools/finish_confirmed_sale.py') --request $FinishConfirmedRequest
        if ($LASTEXITCODE -ne 0) { $qaFailure = 'Confirmed sale remains partial; the bot will be restarted' }
    } catch {
        $qaFailure = 'Confirmed sale remains partial; the bot will be restarted'
    }
}
if ($RepairRequisiteRequest) {
    try {
        $repairArguments = @('--request', $RepairRequisiteRequest)
        if ($RecoverDiagnostic) { $repairArguments += '--recover-diagnostic' }
        & $runtimePath -X utf8 (Join-Path $botRoot 'tools/repair_confirmed_requisite.py') @repairArguments
        if ($LASTEXITCODE -ne 0) { $qaFailure = 'Confirmed requisite could not finish; the bot will be restarted' }
    } catch {
        $qaFailure = 'Confirmed requisite could not finish; the bot will be restarted'
    }
}
if ($VerifyLiveDeal) {
    try {
        & $runtimePath -X utf8 (Join-Path $botRoot 'tools/verify_live_deal.py') --create-and-check
        if ($LASTEXITCODE -ne 0) { $qaFailure = 'Live test was not fully confirmed; see output and saved state' }
    } catch {
        $qaFailure = 'Live test could not finish; the bot will be restarted'
    }
}
$logTag = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdoutPath = Join-Path $botRoot "data/sales-okb-$logTag.stdout.log"
$stderrPath = Join-Path $botRoot "data/sales-okb-$logTag.stderr.log"
$otherWorkers = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq $runtimePath -and $_.CommandLine -in $expectedCommands })
if ($otherWorkers.Count -ne 0) { throw 'Another sales bot process exists; startup cancelled' }
$runArguments = @(('"' + $runPath + '"'), '--sales-bot')
$newWorker = Start-Process -FilePath $runtimePath -ArgumentList $runArguments -WorkingDirectory $botRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
$restartTime = (Get-Process -Id $newWorker.Id).StartTime.ToUniversalTime()
$newRecord = @{pid=$newWorker.Id; started_at=$restartTime.ToString('o'); mode='sales-bot'; executable=$runtimePath; workspace=$botRoot; stdout=$stdoutPath; stderr=$stderrPath; simulation_enabled=$true; okb_enabled=$true; work_inbox_enabled=$true; support_stages_enabled=$true; clean_user_view=$true; languages=@('ru','uz'); support_term='request'; deal_term=$true; region_cities_enabled=$true; crm_statistics_enabled=$true}
$newRecord | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
Set-Content -LiteralPath (Join-Path $botRoot 'data/registration.pid') -Value $newWorker.Id -Encoding ascii
Write-Output "Sales bot restarted: PID $($newWorker.Id)"
if ($qaFailure) { throw $qaFailure }
