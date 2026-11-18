$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$checkpointPath = Join-Path $projectPath '_rollback\before-qdrant-20261115'
$composePath = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker-compose.exe'
Push-Location -LiteralPath $projectPath
try {
 & $composePath stop
 if ($LASTEXITCODE -ne 0) { throw 'Ошибка остановки' }
 Expand-Archive -LiteralPath (Join-Path $checkpointPath 'source-without-qdrant.zip') -DestinationPath $projectPath -Force
 $privateEnv = Join-Path $checkpointPath '.env.private'
 if (Test-Path -LiteralPath $privateEnv) { Copy-Item -LiteralPath $privateEnv -Destination (Join-Path $projectPath '.env') -Force }
 & $composePath up -d --build
 if ($LASTEXITCODE -ne 0) { throw 'Исходники восстановлены, ошибка запуска' }
 Write-Host 'Версия без Qdrant восстановлена; текущие данные сохранены.'
} finally { Pop-Location }
