param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [switch]$Repair
)

$ErrorActionPreference = 'Stop'
$serviceName = 'FactorPunchSync'
$databaseService = 'FactorPunchMariaDB'
$appDir = Join-Path $InstallDir 'app'
$dataRoot = Join-Path $env:ProgramData 'FactorPunch Sync'
$databaseDir = Join-Path $dataRoot 'Database'
$backupDir = Join-Path $dataRoot 'Backups'
$logDir = Join-Path $dataRoot 'Logs'
$envFile = Join-Path $appDir '.env'
$mariaBin = Join-Path $InstallDir 'mariadb\bin'
$nssm = Join-Path $InstallDir 'tools\nssm.exe'

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-HexSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) }
    finally { $generator.Dispose() }
    return ($buffer | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Read-RequiredSecret([string]$Prompt) {
    while ($true) {
        $secure = Read-Host $Prompt -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        if ($plain.Length -ge 10) { return $plain }
        Write-Host 'Use at least 10 characters.' -ForegroundColor Yellow
    }
}

if (-not (Test-Administrator)) { throw 'Run this installer as Administrator.' }
New-Item -ItemType Directory -Force -Path $dataRoot, $databaseDir, $backupDir, $logDir | Out-Null

$appPort = 8000
$databasePort = 3307
$computerName = $env:COMPUTERNAME
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress)
$allowedHosts = (@('localhost', '127.0.0.1', $computerName) + $addresses | Select-Object -Unique) -join ','

if (-not (Test-Path $envFile)) {
    Write-Host ''
    Write-Host 'FactorPunch Sync first-time configuration' -ForegroundColor Cyan
    $restaurant = Read-Host 'Restaurant name (for your records)'
    $adminUser = Read-Host 'Administrator username [admin]'
    if ([string]::IsNullOrWhiteSpace($adminUser)) { $adminUser = 'admin' }
    $adminPassword = Read-RequiredSecret 'Temporary administrator password'
    $databaseRootPassword = New-HexSecret 24
    $databaseAppPassword = New-HexSecret 24
    $appSecret = New-HexSecret 32

    if (-not (Get-Service -Name $databaseService -ErrorAction SilentlyContinue)) {
        & (Join-Path $mariaBin 'mariadb-install-db.exe') `
            "--datadir=$databaseDir" "--service=$databaseService" `
            "--password=$databaseRootPassword" "--port=$databasePort" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'MariaDB initialization failed.' }
    }
    Set-Service -Name $databaseService -StartupType Automatic
    Start-Service -Name $databaseService

    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 2
        $ready = (Test-NetConnection -ComputerName 127.0.0.1 -Port $databasePort -WarningAction SilentlyContinue).TcpTestSucceeded
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw 'MariaDB did not become ready within 60 seconds.' }

    $sql = @"
CREATE DATABASE IF NOT EXISTS zkteco_sync CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'factorpunch_app'@'127.0.0.1' IDENTIFIED BY '$databaseAppPassword';
GRANT ALL PRIVILEGES ON zkteco_sync.* TO 'factorpunch_app'@'127.0.0.1';
FLUSH PRIVILEGES;
"@
    $env:MYSQL_PWD = $databaseRootPassword
    $sql | & (Join-Path $mariaBin 'mariadb.exe') --protocol=tcp --host=127.0.0.1 --port=$databasePort --user=root
    Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) { throw 'Creating the application database account failed.' }

    $environmentText = @"
# FactorPunch Sync - generated locally. Do not share this file.
RESTAURANT_NAME=$restaurant
APP_HOST=0.0.0.0
APP_PORT=$appPort
APP_ENV=production
API_USERNAME=$adminUser
API_PASSWORD=$adminPassword
SECRET_KEY=$appSecret
ALLOWED_HOSTS=$allowedHosts
TRUSTED_PROXIES=127.0.0.1,::1
COOKIE_SECURE=false
SESSION_IDLE_MINUTES=60
SESSION_ABSOLUTE_HOURS=12
LOGIN_MAX_ATTEMPTS=5
LOGIN_LOCKOUT_MINUTES=15
ENABLE_DOCS=false
MAX_REQUEST_BYTES=2097152
ADMS_PAIRING_MINUTES=15
DEFAULT_DEVICE_TIMEZONE=Asia/Riyadh
DB_ENGINE=mariadb
DB_HOST=127.0.0.1
DB_PORT=$databasePort
DB_NAME=zkteco_sync
DB_USER=factorpunch_app
DB_PASSWORD=$databaseAppPassword
DB_ODBC_DRIVER=ODBC Driver 17 for SQL Server
"@
    [IO.File]::WriteAllText($envFile, $environmentText, [Text.UTF8Encoding]::new($false))
}

if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    & $nssm stop $serviceName | Out-Null
    & $nssm remove $serviceName confirm | Out-Null
}
$python = Join-Path $appDir 'runtime\python.exe'
& $nssm install $serviceName $python (Join-Path $appDir 'run.py') | Out-Null
& $nssm set $serviceName AppDirectory $appDir | Out-Null
& $nssm set $serviceName AppStdout (Join-Path $logDir 'stdout.log') | Out-Null
& $nssm set $serviceName AppStderr (Join-Path $logDir 'stderr.log') | Out-Null
& $nssm set $serviceName AppRotateFiles 1 | Out-Null
& $nssm set $serviceName AppRotateBytes 10485760 | Out-Null
& $nssm set $serviceName AppNoConsole 1 | Out-Null
& $nssm set $serviceName Start SERVICE_AUTO_START | Out-Null

netsh advfirewall firewall delete rule name='FactorPunch Sync' | Out-Null
netsh advfirewall firewall add rule name='FactorPunch Sync' dir=in action=allow protocol=TCP localport=$appPort profile=private | Out-Null

$backupScript = Join-Path $InstallDir 'installer\Backup.ps1'
$taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$backupScript`" -InstallDir `"$InstallDir`""
schtasks.exe /Create /TN 'FactorPunch Sync Daily Backup' /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /TR $taskCommand /F | Out-Null

& $nssm start $serviceName | Out-Null
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    try { $response = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:$appPort/login" -TimeoutSec 5 }
    catch { $response = $_.Exception.Response }
} until ($response -or (Get-Date) -gt $deadline)
if (-not $response) { throw "FactorPunch Sync did not answer on port $appPort. Check $logDir." }

Write-Host ''
Write-Host 'FactorPunch Sync is ready.' -ForegroundColor Green
Write-Host "Open locally: http://localhost:$appPort"
if ($addresses.Count -gt 0) { Write-Host "LAN address:  http://$($addresses[0]):$appPort" }
Write-Host 'The first login requires changing the temporary administrator password.'
