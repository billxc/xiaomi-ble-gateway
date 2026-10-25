# Adapted runtime-only installer. Does not start capture, PM2 or firewall rules.
param(
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$PythonBase,
    [string]$Root = 'C:\ProgramData\WindowsBLEProxy',
    [string]$Uv = "$env:USERPROFILE\.local\bin\uv.exe"
)
$ErrorActionPreference = 'Stop'
$privateRoot = [System.IO.Path]::GetFullPath($Root)
if (Test-Path -LiteralPath $privateRoot) {
    if ((Get-Item -LiteralPath $privateRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Installation root must not be a reparse point'
    }
    $marker = Join-Path $privateRoot 'installation.json'
    if (-not (Test-Path -LiteralPath $marker) -or
        (Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).application -ne 'WindowsBLEProxy') {
        throw 'Target exists without this application marker; not overwriting'
    }
}
New-Item -ItemType Directory -Force -Path $privateRoot | Out-Null
# Protect the installation before creating children or any secret configuration.
# Only the installing user, LocalSystem and built-in Administrators inherit access.
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($identity in @($sid, [System.Security.Principal.SecurityIdentifier]'S-1-5-18', [System.Security.Principal.SecurityIdentifier]'S-1-5-32-544')) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $privateRoot -AclObject $acl
New-Item -ItemType Directory -Force -Path "$privateRoot\src", "$privateRoot\src\protocol" | Out-Null
@{ application='WindowsBLEProxy'; version='0.1.0'; python_base=$PythonBase; installed_at=[DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content "$privateRoot\installation.json" -Encoding UTF8
foreach ($name in @('main.py','native_api.py','advertisement.py','winrt_capture.py','requirements.txt','requirements-lock.txt')) {
    Copy-Item -LiteralPath "$PSScriptRoot\$name" -Destination "$privateRoot\src\$name" -Force
}
# Explicit files only: never recursively copy protocol caches or runtime artifacts.
foreach ($name in @('__init__.py','api_pb2.py','api_options_pb2.py','LICENSE','SOURCE.json')) {
    Copy-Item -LiteralPath "$PSScriptRoot\protocol\$name" -Destination "$privateRoot\src\protocol\$name" -Force
}
foreach ($name in @('ecosystem.config.cjs','config.example.json')) {
    Copy-Item -LiteralPath "$PSScriptRoot\$name" -Destination "$privateRoot\$name" -Force
}
if (-not (Test-Path "$privateRoot\venv\Scripts\python.exe")) {
    if (Test-Path -LiteralPath $Uv) { & $Uv venv --python $PythonBase "$privateRoot\venv" }
    else { & $PythonBase -m venv "$privateRoot\venv" }
    if ($LASTEXITCODE -ne 0) { throw 'Application venv creation failed' }
}
$python = "$privateRoot\venv\Scripts\python.exe"
if (Test-Path -LiteralPath $Uv) { & $Uv pip sync --python $python --only-binary :all: "$privateRoot\src\requirements-lock.txt" }
else { & $python -m pip install --only-binary=:all: -r "$privateRoot\src\requirements-lock.txt" }
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
# Use an environment variable, not Python source interpolation of a local path.
$previousSource = $env:WINDOWS_BLE_INSTALL_SOURCE
try {
    $env:WINDOWS_BLE_INSTALL_SOURCE = "$privateRoot\src"
    & $python -c "import os,sys; sys.path.insert(0,os.environ['WINDOWS_BLE_INSTALL_SOURCE']); import protocol.api_pb2,noise.connection,winrt.windows.devices.bluetooth,winrt.windows.devices.radios; print('runtime_ready',sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) { throw 'Runtime import check failed' }
} finally {
    $env:WINDOWS_BLE_INSTALL_SOURCE = $previousSource
}
