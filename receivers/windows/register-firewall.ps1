# Run elevated only after choosing the single trusted HA host address.
param(
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$HomeAssistantAddress
)
$ErrorActionPreference = 'Stop'
$address = $null
if (-not [System.Net.IPAddress]::TryParse($HomeAssistantAddress, [ref]$address)) {
    throw 'HomeAssistantAddress must be one literal HA IP address, not a subnet, hostname or Any'
}
if ($address.Equals([System.Net.IPAddress]::Any) -or $address.Equals([System.Net.IPAddress]::IPv6Any)) {
    throw 'An unspecified address is not a trusted HA host'
}
$name = 'Windows-BLE-Proxy-HomeAssistant'
$old = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($old) { throw 'Firewall rule already exists; inspect it instead of replacing' }
New-NetFirewallRule -Name $name -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort 16053 -RemoteAddress $address.ToString() -Profile Private | Select-Object Name,Enabled,Direction,Action
