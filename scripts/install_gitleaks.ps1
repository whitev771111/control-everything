param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$version = '8.30.1'
$expected = 'd29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$target = [IO.Path]::GetFullPath((Join-Path $root $Destination))
$allowed = [IO.Path]::GetFullPath((Join-Path $root 'work')) + [IO.Path]::DirectorySeparatorChar
if (-not $target.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Destination must be a new directory under ignored work/.'
}
$cursor = $target
while ($cursor.Length -ge $root.Length) {
    if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Linked destination is not allowed.'
    }
    if ($cursor -eq $root) { break }
    $cursor = Split-Path -Parent $cursor
}
if (Test-Path -LiteralPath $target) { throw 'Destination already exists; choose a new directory.' }
$staging = Join-Path $root ('work/gitleaks-download-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$archive = Join-Path $staging 'asset.zip'
try {
    Invoke-WebRequest -Uri "https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_windows_x64.zip" -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw 'Gitleaks archive checksum mismatch.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $target
    if (-not (Test-Path -LiteralPath (Join-Path $target 'gitleaks.exe'))) { throw 'Gitleaks executable missing.' }
    Write-Output "Gitleaks $version installed; archive SHA-256 verified."
} catch {
    Write-Error 'Gitleaks installation failed; no unverified executable was run.'
    exit 1
}
