param([Parameter(Mandatory = $true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
$envPath = Join-Path $InstallDir 'app\.env'
if (-not (Test-Path $envPath)) { throw 'FactorPunch Sync is not configured.' }
$settings = @{}
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') { $settings[$matches[1]] = $matches[2] }
}
$backupDir = Join-Path $env:ProgramData 'FactorPunch Sync\Backups'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$output = Join-Path $backupDir "factorpunch-$stamp.sql"
$dump = Join-Path $InstallDir 'mariadb\bin\mariadb-dump.exe'
$env:MYSQL_PWD = $settings.DB_PASSWORD
& $dump --protocol=tcp --host=$($settings.DB_HOST) --port=$($settings.DB_PORT) --user=$($settings.DB_USER) `
    --single-transaction --routines --events $settings.DB_NAME | Set-Content -Path $output -Encoding UTF8
Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0) { Remove-Item $output -ErrorAction SilentlyContinue; throw 'Backup failed.' }
Get-ChildItem $backupDir -Filter 'factorpunch-*.sql' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 30 | Remove-Item -Force
Write-Host "Backup created: $output"

