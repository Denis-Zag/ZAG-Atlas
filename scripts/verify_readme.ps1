# Проверяет curl-примеры на отдельном тестовом экземпляре Atlas.
param(
    [string]$BaseUrl = "http://127.0.0.1:18081",
    [string]$AdminPassword = "readme-admin-check"
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = Split-Path -Parent $PSScriptRoot
$cases = @()
$cookieFile = [System.IO.Path]::GetTempFileName()
$bodyTemp = [System.IO.Path]::GetTempFileName()
$script:Csrf = ""

function Invoke-CurlExample([string]$Route, [string]$BodyFile) {
    $bodyPath = if (!$BodyFile) { $null } elseif ([System.IO.Path]::IsPathRooted($BodyFile)) { $BodyFile } else { Join-Path $projectRoot ("tests_data/requests/" + $BodyFile) }
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    # Захватываем stdout/код curl явно: работает и в оболочках, перехватывающих
    # стандартный запуск нативных команд PowerShell.
    function ConfigValue([string]$Value) {
        return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
    }
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = Join-Path $env:SystemRoot "System32/curl.exe"
    $startInfo.Arguments = "--silent --show-error --fail-with-body --max-time 60 --config -"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $process.StandardInput.WriteLine('url = ' + (ConfigValue ($BaseUrl + $Route)))
        $process.StandardInput.WriteLine('cookie = ' + (ConfigValue $cookieFile))
        $process.StandardInput.WriteLine('cookie-jar = ' + (ConfigValue $cookieFile))
        $process.StandardInput.WriteLine('header = "X-Atlas-Request: 1"')
        if ($script:Csrf) { $process.StandardInput.WriteLine('header = ' + (ConfigValue ('X-CSRF-Token: ' + $script:Csrf))) }
        $process.StandardInput.WriteLine('header = "Content-Type: application/json"')
        if ($bodyPath) { $process.StandardInput.WriteLine('data-binary = ' + (ConfigValue ('@' + $bodyPath))) }
        $process.StandardInput.Close()
        $raw = $process.StandardOutput.ReadToEnd()
        $curlError = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "curl failed for $Route (exit $($process.ExitCode))" }
    } finally {
        $process.Dispose()
    }
    $watch.Stop()
    return @{ data = ($raw | ConvertFrom-Json); duration_ms = $watch.ElapsedMilliseconds }
}

try {
$encoding = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($bodyTemp, (@{ username='admin'; password=$AdminPassword } | ConvertTo-Json), $encoding)
$initial = Invoke-CurlExample '/auth/login' $bodyTemp
$script:Csrf = (Invoke-CurlExample '/session' $null).data.csrf_token
if ($initial.data.must_change_password) {
    $newPassword = 'verify-' + [guid]::NewGuid().ToString()
    [System.IO.File]::WriteAllText($bodyTemp, (@{ current_password=$AdminPassword; new_password=$newPassword } | ConvertTo-Json), $encoding)
    $null = Invoke-CurlExample '/auth/change-password' $bodyTemp
    $script:Csrf = (Invoke-CurlExample '/session' $null).data.csrf_token
}
$doc = Invoke-CurlExample "/kb/documents" "document.json"
if ($doc.data.status -ne "ok" -or !$doc.data.document_id) { throw "Document was not saved" }
$cases += @{ route = "/kb/documents"; passed = $true; duration_ms = $doc.duration_ms; document_id = $doc.data.document_id }

$qa = Invoke-CurlExample "/kb/ask" "question.json"
if ($qa.data.needs_review -ne $false -or @($qa.data.sources).Count -eq 0) {
    throw "Question needs review: $($qa.data.error)"
}
if ($qa.data.sources[0].document_id -ne $doc.data.document_id) { throw "Wrong document source" }
$cases += @{ route = "/kb/ask"; passed = $true; duration_ms = $qa.duration_ms; answer = $qa.data.answer; sources = $qa.data.sources }

$ai = Invoke-CurlExample "/ai/answer_with_sources" "answer_with_sources.json"
if ($ai.data.needs_review -ne $false -or @($ai.data.sources).Count -eq 0) { throw "AI answer needs review" }
$context = (Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot "tests_data/requests/answer_with_sources.json") | ConvertFrom-Json).context
foreach ($quote in $ai.data.sources) {
    if (!$context.Contains($quote.quote)) { throw "Quote does not match UTF-8 fixture" }
}
$cases += @{ route = "/ai/answer_with_sources"; passed = $true; duration_ms = $ai.duration_ms; answer = $ai.data.answer; sources = $ai.data.sources; confidence = $ai.data.confidence }

@{ powershell_version = $PSVersionTable.PSVersion.ToString(); cases = $cases; all_passed = $true } | ConvertTo-Json -Depth 8

} finally {
    Remove-Item -LiteralPath $cookieFile, $bodyTemp -Force -ErrorAction SilentlyContinue
}
