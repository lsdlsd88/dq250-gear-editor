# Builds the windowed single-file executable into .\dist
$ErrorActionPreference = "Stop"

py -3 -m PyInstaller --noconfirm --clean dq250_gear_tool.spec

$exe = Join-Path $PSScriptRoot "dist\dq250_gear_tool.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "build failed: $exe was not produced"
}

# A one-file exe unpacks itself into %TEMP%. On locked-down systems a low
# integrity copy of the exe is refused there, so normalise the label.
try { icacls $exe /setintegritylevel Medium | Out-Null } catch {}

Get-Item -LiteralPath $exe | Select-Object FullName, Length, LastWriteTime
