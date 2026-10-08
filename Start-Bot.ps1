param(
    [switch]$Demo,
    [switch]$Check,
    [switch]$CheckOnline,
    [switch]$Test,
    [switch]$RegistrationOnly,
    [switch]$SalesBot
)

$ErrorActionPreference = 'Stop'
$botRoot = $PSScriptRoot
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    $botPython = $bundledPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Не найден Python 3.11+. Требуется установить Python или использовать runtime Codex.'
    }
    $botPython = $pythonCommand.Source
}

Push-Location -LiteralPath $botRoot
try {
    if ($Test) {
        & $botPython -m unittest discover -s tests -v
    } else {
        $botArguments = @((Join-Path $botRoot 'run.py'))
        if ($Demo) { $botArguments += '--demo' }
        if ($Check) { $botArguments += '--check' }
        if ($CheckOnline) { $botArguments += '--check-online' }
        if ($RegistrationOnly) { $botArguments += '--registration-only' }
        if ($SalesBot) { $botArguments += '--sales-bot' }
        & $botPython @botArguments
    }
    $botExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExitCode
