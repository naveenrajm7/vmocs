param(
    [Parameter(Position=0)]
    [AllowEmptyString()]
    [string]$ArgEmpty,
    [Parameter(Position=1)]
    [string]$ArgSpace,
    [Parameter(Position=2)]
    [string]$ArgQuote,
    [Parameter(Position=3)]
    [string]$ArgTrailingSlash,
    [Parameter(Position=4)]
    [string]$ArgMetacharacters,
    [switch]$Grandchild
)

$snowman = [char]0x2603
$expectedToken = 'line one' + "`r`n" + `
    'line two ''"`$() % & | < > ^ snowman ' + $snowman

if ($Grandchild) {
    if ($env:SLURM_GHA_CLAIM_TOKEN -cne $expectedToken) {
        exit 61
    }
    exit 0
}

function Assert-EnvironmentValue {
    param(
        [string]$Name,
        [string]$Expected,
        [int]$ExitCode
    )

    $actual = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($actual -cne $Expected) {
        [Console]::Error.WriteLine("unexpected value for $Name")
        exit $ExitCode
    }
}

Assert-EnvironmentValue 'SLURM_GHA_SERVICE_URL' `
    'https://slurm-gha-host:8443/healthz?x=1&y=2' 41
Assert-EnvironmentValue 'SLURM_GHA_LEASE_ID' `
    ('lease-"quoted"-' + $snowman) 42
Assert-EnvironmentValue 'SLURM_GHA_CLASS' 'windows rocm' 43
Assert-EnvironmentValue 'SLURM_GHA_CLAIM_TOKEN' $expectedToken 44
Assert-EnvironmentValue 'SLURM_JOB_ID' '99003001' 45
Assert-EnvironmentValue 'SLURM_GHA_CA_FILE' `
    'C:\slurm-gha\callback-ca.cer' 46

if (-not $PSBoundParameters.ContainsKey('ArgEmpty') -or $ArgEmpty -cne '' -or
        $ArgSpace -cne 'space value' -or $ArgQuote -cne 'quote"value' -or
        $ArgTrailingSlash -cne 'trailing\' -or
        $ArgMetacharacters -cne 'meta&chars|<>^%') {
    [Console]::Error.WriteLine('child arguments were not preserved')
    exit 52
}

$environment = [Environment]::GetEnvironmentVariables('Process')
if (-not $environment.Contains('VMOCS_EMPTY') -or
        $environment['VMOCS_EMPTY'] -cne '') {
    [Console]::Error.WriteLine('empty environment value was not preserved')
    exit 47
}

$stageRoot = Join-Path `
    ([Environment]::GetFolderPath('LocalApplicationData')) 'VMoCS'
if (Test-Path -LiteralPath $stageRoot) {
    $remaining = Get-ChildItem -LiteralPath $stageRoot -Directory `
        -Filter 'vmocs-env-*' -ErrorAction Stop | Select-Object -First 1
    if ($null -ne $remaining) {
        [Console]::Error.WriteLine('staging directory still exists')
        exit 48
    }
}

$tokenInCommandLine = Get-CimInstance Win32_Process -ErrorAction Stop |
    Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($expectedToken)
    } | Select-Object -First 1
if ($null -ne $tokenInCommandLine) {
    [Console]::Error.WriteLine('claim token appeared in a process command line')
    exit 49
}

& powershell.exe -NoLogo -NoProfile -NonInteractive `
    -File $PSCommandPath -Grandchild
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine('grandchild did not inherit the environment')
    exit 50
}

$stdinValue = [Console]::In.ReadLine()
if ($stdinValue -cne 'child stdin remains available') {
    [Console]::Error.WriteLine('child stdin was not preserved')
    exit 51
}

[Console]::Out.WriteLine('WINDOWS_FORWARD_ENV_OK')
exit 37
