param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8766
)

$ErrorActionPreference = 'Stop'
$workspaceUrl = "http://127.0.0.1:$Port/workspace"
try {
    $response = Invoke-WebRequest -Uri $workspaceUrl -UseBasicParsing -TimeoutSec 5
    if ($response.StatusCode -ne 200) { throw 'Unexpected HTTP status.' }
} catch {
    Write-Error "工作台尚不可访问：$workspaceUrl 。请先启动对应端口的面板服务，再打开 Edge 应用窗口。"
    exit 1
}

$edgePath = $null
foreach ($programDirectory in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
    if (-not $programDirectory) { continue }
    $candidate = Join-Path $programDirectory 'Microsoft/Edge/Application/msedge.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $edgePath = $candidate
        break
    }
}
if (-not $edgePath) {
    Write-Error "没有在标准安装位置找到 Microsoft Edge。可在浏览器打开 $workspaceUrl 。"
    exit 1
}

# This visible app window is the interactive desktop entry requested by the user.
Start-Process -FilePath $edgePath -ArgumentList "--app=$workspaceUrl" -WindowStyle Normal
Write-Output "已请求打开 Edge 应用窗口：$workspaceUrl"
