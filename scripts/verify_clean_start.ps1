# Проверяет отдельную установку и удаляет только созданные ею Docker-ресурсы.
param([switch]$SelfTest, [ValidateRange(1024,65535)][int]$Port = 18080)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:Utf8 = [System.Text.UTF8Encoding]::new($false)
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Quote-NativeArgument([string]$Value) {
    if ($Value -match '[\r\n\x00]') { throw 'Недопустимый аргумент процесса.' }
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function New-IsolatedCompose([string]$Source, [string]$Name, [int]$TestPort) {
    if ($Name -notmatch '^atlas-clean-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$' -or $TestPort -eq 8000) {
        throw 'Защита изоляции: недопустимое имя проекта или порт.'
    }
    if ([regex]::Matches($Source, '(?m)^name: zag-atlas\s*$').Count -ne 1 -or
        [regex]::Matches($Source, '127\.0\.0\.1:8000:8000').Count -ne 1 -or
        $Source -match '(?m)^\s+(external|container_name|name):' -or
        $Source -notmatch 'atlas-data:/app/data' -or $Source -notmatch 'qdrant-data:/qdrant/storage') {
        throw 'Compose изменился: требуется повторно проверить изоляцию перед запуском.'
    }
    return $Source.Replace('name: zag-atlas', 'name: ' + $Name).Replace(
        '127.0.0.1:8000:8000', ('127.0.0.1:' + $TestPort + ':8000'))
}

if ($SelfTest) {
    $source = [System.IO.File]::ReadAllText((Join-Path $projectRoot 'compose.yaml'))
    $testName = 'atlas-clean-20261117-120000-1234abcd'
    $isolated = New-IsolatedCompose $source $testName 18080
    if ($isolated -match 'name: zag-atlas' -or $isolated -match '127\.0\.0\.1:8000:8000') { throw 'Изоляция не применена.' }
    foreach ($bad in @('zag-atlas', 'atlas-clean-../../zag-atlas')) {
        $rejected = $false
        try { $null = New-IsolatedCompose $source $bad 18080 } catch { $rejected = $true }
        if (!$rejected) { throw 'Не отклонено опасное имя проекта.' }
    }
    $rejected = $false
    try { $null = New-IsolatedCompose $source $testName 8000 } catch { $rejected = $true }
    if (!$rejected) { throw 'Разрешён рабочий порт.' }
    $rejected = $false
    try { $null = New-IsolatedCompose ($source + "`n  extra:`n    external: true") $testName 18080 } catch { $rejected = $true }
    if (!$rejected) { throw 'Разрешены внешние ресурсы.' }
    if ((Quote-NativeArgument 'a b') -ne '"a b"' -or
        (Quote-NativeArgument 'C:\folder name\') -ne '"C:\folder name\\"' -or
        (Quote-NativeArgument 'a"b') -ne '"a\"b"') { throw 'Ошибка экранирования аргументов.' }
    Write-Host 'SELFTEST PASSED: separate project/port, external-resource rejection, Windows argument quoting.'
    return
}

$script:DockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
if (!(Test-Path -LiteralPath $script:DockerExe)) { throw 'Не найден Docker CLI. Нужен установленный Docker Desktop.' }
$script:ContextArgs = @()
$script:RunDirectory = $projectRoot
$script:PrivateDirectory = $null
$script:CommandNumber = 0

function Invoke-DockerCommand([string[]]$Arguments, [int]$TimeoutSeconds = 60, [string]$LogName = '') {
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $script:DockerExe
    $info.Arguments = (($script:ContextArgs + $Arguments) | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    $info.WorkingDirectory = $script:RunDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = $script:Utf8
    $info.StandardErrorEncoding = $script:Utf8
    # Явные -f/-p и локальный контекст не зависят от окружения рабочей установки.
    foreach ($key in @($info.EnvironmentVariables.Keys)) {
        if ($key -like 'COMPOSE_*' -or $key -in @('DOCKER_HOST','DOCKER_CONTEXT')) {
            $info.EnvironmentVariables.Remove($key)
        }
    }
    $info.EnvironmentVariables['COMPOSE_ANSI'] = 'never'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $lastNotice = 0
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        while (!$process.WaitForExit(1000)) {
            if ($watch.Elapsed.TotalSeconds -gt $TimeoutSeconds) {
                $process.Kill()
                throw ('Превышено время команды Docker: ' + $TimeoutSeconds + ' с.')
            }
            if ($LogName -and $watch.Elapsed.TotalSeconds -ge ($lastNotice + 30)) {
                $lastNotice = [int]$watch.Elapsed.TotalSeconds
                Write-Host ('  ' + $LogName + ': выполняется ' + $lastNotice + ' с...')
            }
        }
        $result = @{code=$process.ExitCode;output=$stdout.Result;error=$stderr.Result;seconds=$watch.Elapsed.TotalSeconds}
        if ($LogName -and $script:PrivateDirectory) {
            [System.IO.File]::WriteAllText((Join-Path $script:PrivateDirectory ($LogName + '.log')),
                $result.output + "`n" + $result.error, $script:Utf8)
        }
        return $result
    } finally { $process.Dispose() }
}

function Require-Docker([string[]]$Arguments, [int]$TimeoutSeconds = 60, [string]$LogName = '') {
    $result = Invoke-DockerCommand $Arguments $TimeoutSeconds $LogName
    if ($result.code -ne 0) {
        if ($LogName) { throw ('Команда ' + $LogName + ' завершилась с ошибкой. Подробности в приватном журнале проверки.') }
        throw 'Команда Docker завершилась с ошибкой. Проверьте доступ к Docker Desktop.'
    }
    return $result
}

function Read-DockerJson([string[]]$Arguments) {
    $result = Require-Docker $Arguments
    foreach ($line in ($result.output -split '\r?\n')) {
        if ($line.Trim()) { $line | ConvertFrom-Json }
    }
}

function Get-ProtectedSnapshot {
    $ids = (Require-Docker @('ps','-aq','--no-trunc','--filter','label=com.docker.compose.project=zag-atlas')).output
    $snapshots = @()
    foreach ($id in ($ids -split '\r?\n' | Where-Object { $_.Trim() } | Sort-Object)) {
        $format = '{{.Id}}|{{.Name}}|{{.Image}}|{{.State.Status}}|{{json .Mounts}}'
        $parts = ((Require-Docker @('container','inspect',('--format=' + $format),$id.Trim())).output.Trim()) -split '\|',5
        $item = [ordered]@{id=$parts[0];name=$parts[1];image=$parts[2];state=$parts[3];mounts=$parts[4];data_counts=$null}
        if ($item.name -match '^/zag-atlas-atlas-\d+$' -and $item.state -eq 'running') {
            $query = "import sqlite3,json; c=sqlite3.connect('file:/app/data/atlas.sqlite3?mode=ro',uri=True); print(json.dumps({t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('documents','snippets','qa_runs','users')}))"
            $item.data_counts = (Require-Docker @('exec',$id.Trim(),'python','-c',$query)).output.Trim()
        }
        $snapshots += $item
    }
    return $snapshots
}

# Проверяем доступ до создания файлов или Docker-ресурсов.
$contexts = @(Read-DockerJson @('context','ls','--format=json'))
$current = @($contexts | Where-Object { $_.Current -eq $true -or $_.Current -eq '*' })
if ($current.Count -ne 1 -or $current[0].DockerEndpoint -notmatch '^(npipe|unix):') {
    throw 'Для проверки нужен локальный Docker-контекст. Удалённые серверы не используются.'
}
$script:ContextArgs = @('--context',$current[0].Name)
$null = Require-Docker @('info','--format={{.ServerVersion}}')
$composeVersion = (Require-Docker @('compose','version','--short')).output.Trim()
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$Port)
try { $listener.Start() } catch { throw ('Порт ' + $Port + ' занят. Выберите другой через -Port.') }
finally { $listener.Stop() }
$protectedBefore = @(Get-ProtectedSnapshot)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$suffix = [guid]::NewGuid().ToString('N').Substring(0,8)
$name = 'atlas-clean-' + $stamp + '-' + $suffix
$privateRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ('_rollback\clean-start-' + $stamp + '-' + $suffix)))
if (!$privateRoot.StartsWith(($projectRoot + '\_rollback\'),[System.StringComparison]::OrdinalIgnoreCase) -or
    (Test-Path -LiteralPath $privateRoot)) { throw 'Не удалось выбрать новую отдельную папку проверки.' }
$sourceDir = Join-Path $privateRoot 'source'
$script:PrivateDirectory = $privateRoot
$reportPath = Join-Path $projectRoot ('verification\CLEAN_START_' + $stamp + '-' + $suffix + '.json')
$summaryPath = [System.IO.Path]::ChangeExtension($reportPath,'.txt')
$report = [ordered]@{checked_at=(Get-Date).ToUniversalTime().ToString('o');status='running';project=$name;
    compose_version=$composeVersion;port=$Port;method='Fresh source copy and empty isolated SQLite/Qdrant volumes; build --no-cache --pull';
    limits='Simulation on an existing Docker host. Docker/WSL installation and manual configuration time are excluded. Cached base/Qdrant image layers may remain.';
    qdrant_image_was_cached=$null;timing=[ordered]@{};checks=$null;initial=$null;failure=$null;
    protected_before=$protectedBefore;protected_after=@();original_unchanged=$null;cleanup=[ordered]@{passed=$false}}
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$composeArgs = @()
$isolationApproved = $false

try {
    Write-Host '1/5. Подготовка отдельной установки. Рабочий zag-atlas не останавливается.'
    New-Item -ItemType Directory -Path $sourceDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $reportPath) -Force | Out-Null
    $script:RunDirectory = $sourceDir
    $rootFiles = @('Dockerfile','compose.yaml','requirements.txt','preflight.py','.dockerignore','.env.example','README.md')
    foreach ($file in $rootFiles) { Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination (Join-Path $sourceDir $file) }
    foreach ($folder in @('app','static')) {
        $folderRoot = Join-Path $projectRoot $folder
        foreach ($file in (Get-ChildItem -LiteralPath $folderRoot -File -Recurse | Where-Object {
            $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc'
        })) {
            if ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'В исходниках обнаружена ссылка вместо обычного файла.' }
            $relative = $file.FullName.Substring($projectRoot.Length + 1)
            $target = Join-Path $sourceDir $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $file.FullName -Destination $target
        }
    }
    $composePath = Join-Path $sourceDir 'compose.yaml'
    $composeArgs = @('compose','-p',$name,'-f',$composePath)
    $isolated = New-IsolatedCompose ([System.IO.File]::ReadAllText($composePath)) $name $Port
    [System.IO.File]::WriteAllText($composePath,$isolated,$script:Utf8)
    $keyLines = @(Get-Content -LiteralPath (Join-Path $projectRoot '.env') -Encoding UTF8 | Where-Object { $_ -match '^PROXYAPI_API_KEY\s*=' })
    if ($keyLines.Count -ne 1 -or !($keyLines[0] -replace '^PROXYAPI_API_KEY\s*=','').Trim().Trim('"',"'")) {
        throw 'В рабочем .env нужен один непустой PROXYAPI_API_KEY.'
    }
    $bytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $temporary = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
    $envText = [System.IO.File]::ReadAllText((Join-Path $sourceDir '.env.example'))
    $envLines = foreach ($line in ($envText -split '\r?\n')) {
        if ($line -match '^PROXYAPI_API_KEY=') { $keyLines[0] }
        elseif ($line -match '^ADMIN_PASSWORD=') { 'ADMIN_PASSWORD=' + $temporary }
        else { $line }
    }
    [System.IO.File]::WriteAllText((Join-Path $sourceDir '.env'),($envLines -join "`n"),$script:Utf8)
    $temporary = $null; $keyLines = $null; $envLines = $null
    $script:RunDirectory = $sourceDir
    $composeArgs = @('compose','--project-name',$name,'--file',$composePath)
    if ((Require-Docker @('ps','-aq','--filter',('label=com.docker.compose.project=' + $name))).output.Trim() -or
        (Require-Docker @('volume','ls','-q','--filter',('label=com.docker.compose.project=' + $name))).output.Trim()) {
        throw 'Имя отдельной установки уже используется.'
    }
    # Полный config содержит секрет: проверяем только в памяти, не сохраняем его.
    $rendered = (Require-Docker ($composeArgs + @('config','--format','json'))).output | ConvertFrom-Json
    if ($rendered.name -ne $name -or
        (($rendered.services.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'atlas,qdrant' -or
        (($rendered.volumes.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'atlas-data,qdrant-data' -or
        $rendered.volumes.'atlas-data'.name -ne ($name + '_atlas-data') -or
        $rendered.volumes.'qdrant-data'.name -ne ($name + '_qdrant-data') -or
        [System.IO.Path]::GetFullPath($rendered.services.atlas.build.context) -ne $sourceDir -or
        @($rendered.services.atlas.ports).Count -ne 1 -or
        $rendered.services.atlas.ports[0].host_ip -ne '127.0.0.1' -or
        [int]$rendered.services.atlas.ports[0].published -ne $Port -or
        @($rendered.services.qdrant.ports | Where-Object { $null -ne $_ }).Count -ne 0) { throw 'Итоговая конфигурация нарушает изоляцию.' }
    foreach ($service in @('atlas','qdrant')) {
        $mounts = @($rendered.services.$service.volumes)
        $expected = if ($service -eq 'atlas') { 'atlas-data' } else { 'qdrant-data' }
        $destination = if ($service -eq 'atlas') { '/app/data' } else { '/qdrant/storage' }
        if ($mounts.Count -ne 1 -or $mounts[0].type -ne 'volume' -or
            $mounts[0].source -ne $expected -or $mounts[0].target -ne $destination) { throw 'Небезопасное подключение данных.' }
    }
    if ($rendered.services.atlas.image -and $rendered.services.atlas.image -notin @(($name+'-atlas'),($name+'-atlas:latest'))) {
        throw 'Образ отдельной установки должен иметь собственное имя.'
    }
    $rendered = $null
    $composeHash = (Get-FileHash -LiteralPath $composePath -Algorithm SHA256).Hash
    $isolationApproved = $true
    $report.timing.preparation_seconds = [math]::Round($watch.Elapsed.TotalSeconds,3)
    Write-Host '2/5. Проверка загрузки Qdrant и сборка Atlas без кеша. Это может занять несколько минут.'
    $report.qdrant_image_was_cached = (Invoke-DockerCommand @('image','inspect','qdrant/qdrant:v1.17.1','--format={{.Id}}')).code -eq 0
    $pull = Require-Docker ($composeArgs + @('pull','qdrant')) 600 'qdrant-pull'
    $report.timing.qdrant_pull_seconds = [math]::Round($pull.seconds,3)
    $build = Require-Docker ($composeArgs + @('build','--no-cache','--pull','atlas')) 1800 'atlas-build'
    $report.timing.build_seconds = [math]::Round($build.seconds,3)
    Write-Host '3/5. Запуск отдельных контейнеров и проверка пустой базы, Qdrant, OCR.'
    $null = Require-Docker ($composeArgs + @('up','-d','--no-build','--wait','--wait-timeout','120')) 150 'compose-up'
    $health = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/health') -TimeoutSec 15
    if ($health.status -ne 'ok') { throw 'Опубликованный порт новой установки недоступен.' }
    $null = Require-Docker ($composeArgs + @('cp',(Join-Path $PSScriptRoot 'clean_start_probe.py'),'atlas:/tmp/clean_start_probe.py'))
    $probe = Invoke-DockerCommand ($composeArgs + @('exec','-T','-e',('ATLAS_CLEAN_START='+$name),'atlas','python','-X','utf8','/tmp/clean_start_probe.py','initial')) 90 'initial-probe'
    $report.initial = $probe.output | ConvertFrom-Json
    if ($probe.code -ne 0 -or !$report.initial.passed) { throw 'Не пройдена проверка начального состояния.' }
    $report.timing.infrastructure_ready_seconds = [math]::Round($watch.Elapsed.TotalSeconds,3)
    Write-Host '4/5. Проверка входа, источников, ручной проверки, аудита и экспорта. Выполняются платные запросы ProxyAPI.'
    $probe = Invoke-DockerCommand ($composeArgs + @('exec','-T','-e',('ATLAS_CLEAN_START='+$name),'atlas','python','-X','utf8','/tmp/clean_start_probe.py','smoke')) 900 'functional-probe'
    $report.checks = $probe.output | ConvertFrom-Json
    if ($probe.code -ne 0 -or !$report.checks.passed) { throw 'Не пройдена функциональная проверка. Причина сохранена в отчёте.' }
    $report.timing.functional_ready_seconds = [math]::Round($watch.Elapsed.TotalSeconds,3)
    $report.timing.within_10_minutes = $watch.Elapsed.TotalSeconds -le 600
    $report.status = if ($report.timing.within_10_minutes) { 'passed' } else { 'time_limit_exceeded' }
} catch {
    $report.status = 'failed'
    $report.failure = $_.Exception.Message
} finally {
    Write-Host '5/5. Сохранение результата и удаление только временных Docker-ресурсов.'
    if ($isolationApproved) {
        try {
            if ((Get-FileHash -LiteralPath $composePath -Algorithm SHA256).Hash -ne $composeHash -or
                $name -notmatch '^atlas-clean-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$') { throw 'Проверка владения временным Compose не пройдена.' }
            $null = Invoke-DockerCommand ($composeArgs + @('logs','--no-color','--tail','100','atlas','qdrant')) 30 'containers'
            $down = Require-Docker ($composeArgs + @('down','--volumes','--remove-orphans','--timeout','10')) 90 'compose-down'
            $remaining = (Require-Docker @('ps','-aq','--filter',('label=com.docker.compose.project=' + $name))).output.Trim()
            $volumes = (Require-Docker @('volume','ls','-q','--filter',('label=com.docker.compose.project=' + $name))).output.Trim()
            $imageReference = $name + '-atlas:latest'
            $imageExists = (Invoke-DockerCommand @('image','inspect',$imageReference,'--format={{.Id}}')).code -eq 0
            $imageRemoved = $true
            if ($imageExists) { $imageRemoved = (Invoke-DockerCommand @('image','rm','--no-prune',$imageReference)).code -eq 0 }
            $report.cleanup = [ordered]@{passed=(!$remaining -and !$volumes -and $imageRemoved);containers_removed=(!$remaining);
                volumes_removed=(!$volumes);test_image_removed=$imageRemoved;build_cache_removed=$false}
        } catch { $report.cleanup = [ordered]@{passed=$false;error=$_.Exception.Message} }
    }
    try {
        $report.protected_after = @(Get-ProtectedSnapshot)
        $beforeJson = ConvertTo-Json -InputObject $protectedBefore -Depth 8 -Compress
        $afterJson = ConvertTo-Json -InputObject $report.protected_after -Depth 8 -Compress
        $report.original_unchanged = $beforeJson -eq $afterJson
    } catch { $report.original_unchanged = $false }
    if (!$report.cleanup.passed -or !$report.original_unchanged) { $report.status = 'failed' }
    $report.timing.total_with_cleanup_seconds = [math]::Round($watch.Elapsed.TotalSeconds,3)
    [System.IO.File]::WriteAllText($reportPath,($report | ConvertTo-Json -Depth 16),$script:Utf8)
    $summary = @('ZAG Atlas — проверка отдельной чистой установки',('Результат: ' + $report.status),
        ('До готовности инфраструктуры, с: ' + $report.timing.infrastructure_ready_seconds),
        ('До успешной проверки функций, с: ' + $report.timing.functional_ready_seconds),
        ('Уложились в 10 минут: ' + $report.timing.within_10_minutes),
        ('Рабочая установка не изменилась: ' + $report.original_unchanged),
        ('Тестовые контейнеры, тома и тег образа убраны: ' + $report.cleanup.passed),
        'Метод: новые исходники и пустые тома; сборка --no-cache --pull.',
        'Это имитация на установленном Docker. Слои базовых образов могут быть в кеше.',
        ('JSON-отчёт: ' + $reportPath))
    if ($report.failure) { $summary += 'Причина: ' + $report.failure }
    [System.IO.File]::WriteAllText($summaryPath,($summary -join "`r`n"),$script:Utf8)
    Write-Host ''; $summary | ForEach-Object { Write-Host $_ }
    Write-Host 'Сделайте скриншот этих итоговых строк и пришлите их текстом. Пароли и ключ на экран не выводились.'
}
if ($report.status -ne 'passed') { exit 1 }
