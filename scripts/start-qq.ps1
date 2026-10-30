param([switch]$NoBrowser, [switch]$Restart)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$dataRoot = Join-Path $projectRoot '.heybuddy\napcat'
$runtimeRoot = Join-Path $dataRoot 'runtime'
$nodePath = Join-Path $runtimeRoot 'node.exe'
$dashboardUrl = 'http://127.0.0.1:8765'

function Stop-Startup($Message) {
    $failure = New-Object System.InvalidOperationException($Message)
    $failure.Data['HeyBuddyStartup'] = $true
    throw $failure
}

function Get-RunningProcess($PidFile, $ExpectedPath) {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    try {
        $savedId = [int](Get-Content -LiteralPath $PidFile -Raw).Trim()
        $existing = Get-Process -Id $savedId -ErrorAction SilentlyContinue
        if ($existing -and $existing.Path -eq $ExpectedPath) { return $existing }
    } catch { }
    return $null
}

function Test-Dashboard {
    try {
        $status = Invoke-RestMethod -Uri "$dashboardUrl/api/status" -TimeoutSec 2
        return ($status.qq.gateway -eq 'ready' -and $status.qq.consumer -eq 'running')
    } catch { return $false }
}

function Test-OneBot($Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync('127.0.0.1', $Port)
        return ($connection.Wait(500) -and $client.Connected)
    } catch { return $false }
    finally { $client.Dispose() }
}

$pathHash = [System.Security.Cryptography.SHA256]::Create()
try {
    $launchId = [BitConverter]::ToString(
        $pathHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))
    ).Replace('-', '')
} finally { $pathHash.Dispose() }
$launchMutex = New-Object System.Threading.Mutex($false, ('Local\HeyBuddyStart_' + $launchId))
$acquired = $false
Push-Location -LiteralPath $projectRoot
try {
    try { $acquired = $launchMutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) {
        Write-Host 'HeyBuddy startup is already in progress. Use the existing startup window.'
        return
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        Stop-Startup 'Missing Python environment. Run uv sync once in the project directory.'
    }
    if (-not (Test-Path -LiteralPath $nodePath)) {
        Stop-Startup 'NapCat is not installed in .heybuddy\napcat\runtime. See docs\napcat-setup.md.'
    }
    if ($Restart) {
        $oldService = Get-RunningProcess (Join-Path $dataRoot 'heybuddy.pid') $pythonPath
        if ($oldService) {
            $children = @(Get-CimInstance Win32_Process -Filter (
                'ParentProcessId=' + $oldService.Id
            ) | Where-Object {
                $_.CommandLine -match '-m\s+personal_agent\.main\s+napcat' -and
                $_.ExecutablePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)
            })
            $targetIds = @([string]$oldService.Id) + @($children | ForEach-Object { [string]$_.ProcessId })
            $stopHelper = Join-Path $PSScriptRoot 'request-stop.py'
            Write-Host 'Requesting normal HeyBuddy shutdown...'
            $helper = Start-Process -FilePath $pythonPath -ArgumentList (
                @(('"' + $stopHelper + '"')) + $targetIds
            ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -Wait
            if ($helper.ExitCode -ne 0) {
                Stop-Startup 'Unable to safely signal this service. No process was force-killed.'
            }
            $stopDeadline = [DateTime]::UtcNow.AddSeconds(30)
            while (@($targetIds | Where-Object {
                Get-Process -Id ([int]$_) -ErrorAction SilentlyContinue
            }).Count -gt 0) {
                if ([DateTime]::UtcNow -ge $stopDeadline) {
                    Stop-Startup 'Shutdown is still pending. No duplicate instance was started.'
                }
                Start-Sleep -Milliseconds 200
            }
        }
    }
    # Validate configuration through the existing Host loader; never print private inputs.
    $configCode = @'
import json
import sys
from personal_agent.config import load_settings
try:
    settings = load_settings()
    settings.require_napcat()
    from urllib.parse import urlsplit
    endpoint = urlsplit(settings.napcat_ws_url)
    if endpoint.hostname != '127.0.0.1':
        raise ValueError('This launcher requires IPv4 loopback')
except Exception:
    print('Invalid local configuration. Check .env and docs/napcat-setup.md.', file=sys.stderr)
    sys.exit(2)
print(json.dumps({'self_id': settings.napcat_self_id, 'port': endpoint.port}))
'@
    $configJson = & $pythonPath -c $configCode
    if ($LASTEXITCODE -ne 0) { Stop-Startup 'HeyBuddy configuration validation failed.' }
    $config = $configJson | ConvertFrom-Json

    $napcatPid = Join-Path $dataRoot 'runtime.pid'
    $napcat = Get-RunningProcess $napcatPid $nodePath
    if (-not $napcat) {
        Write-Host 'Starting NapCat...'
        $napcat = Start-Process -FilePath $nodePath -ArgumentList @(
            'index.js', '-q', $config.self_id
        ) -WorkingDirectory $runtimeRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataRoot 'runtime.stdout.log') `
            -RedirectStandardError (Join-Path $dataRoot 'runtime.stderr.log')
        $napcat.Id | Set-Content -LiteralPath $napcatPid
    }

    $loginOpened = $false
    $loginDeadline = [DateTime]::UtcNow.AddMinutes(3)
    while (-not (Test-OneBot $config.port)) {
        $napcat.Refresh()
        if ($napcat.HasExited) { Stop-Startup 'NapCat stopped. Check .heybuddy\napcat\runtime.stderr.log.' }
        if (-not $loginOpened) {
            Write-Host 'Waiting for secondary-account login. Confirm the login in mobile QQ.'
            $webuiPath = Join-Path $runtimeRoot 'napcat\config\webui.json'
            if (Test-Path -LiteralPath $webuiPath) {
                $webui = Get-Content -LiteralPath $webuiPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($webui.host -eq '127.0.0.1' -and $webui.port -eq 6099 -and
                    (Test-OneBot 6099)) {
                    $loginUrl = 'http://127.0.0.1:6099/webui?token=' +
                        [Uri]::EscapeDataString($webui.token)
                    if (-not $NoBrowser) { Start-Process $loginUrl }
                    $loginOpened = $true
                }
            }
        }
        if ([DateTime]::UtcNow -ge $loginDeadline) {
            Stop-Startup 'Login timed out. Complete QQ login, then double-click start-heybuddy.cmd again.'
        }
        Start-Sleep -Seconds 1
    }

    $heybuddyPid = Join-Path $dataRoot 'heybuddy.pid'
    $heybuddy = Get-RunningProcess $heybuddyPid $pythonPath
    if (-not $heybuddy) {
        Write-Host 'Starting HeyBuddy...'
        $heybuddy = Start-Process -FilePath $pythonPath -ArgumentList @(
            '-u', '-m', 'personal_agent.main', 'napcat', '--dashboard'
        ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataRoot 'heybuddy.stdout.log') `
            -RedirectStandardError (Join-Path $dataRoot 'heybuddy.stderr.log')
        $heybuddy.Id | Set-Content -LiteralPath $heybuddyPid
    }

    $readyDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Dashboard)) {
        $heybuddy.Refresh()
        if ($heybuddy.HasExited) {
            Stop-Startup 'HeyBuddy stopped. Close other instances using this database and check .heybuddy\napcat\heybuddy.stderr.log.'
        }
        if ([DateTime]::UtcNow -ge $readyDeadline) {
            Stop-Startup 'HeyBuddy is still connecting. Check the local logs or double-click again later.'
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "HeyBuddy is ready. Send a friend message from your main QQ account."
    Write-Host "Observatory: $dashboardUrl"
    if (-not $NoBrowser) { Start-Process $dashboardUrl }
} catch {
    # Only our fixed diagnostics are displayed; third-party exceptions may contain secrets.
    if ($_.Exception.Data['HeyBuddyStartup']) {
        Write-Host $_.Exception.Message -ForegroundColor Red
    } else {
        Write-Host 'Unable to start the local service. Check paths, permissions and local logs.' -ForegroundColor Red
    }
    exit 1
} finally {
    Pop-Location
    if ($acquired) { $launchMutex.ReleaseMutex() }
    $launchMutex.Dispose()
}
