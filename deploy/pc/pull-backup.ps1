# Off-host copy on the operator's PC (CE-ADR-018): pulls the newest engine backup from the
# VPS, verifies it byte for byte (SHA-256), keeps 30 days, and records the verified copy in
# ce_controls ('last_off_host_copy') for the cutover report.
#
# Run daily by a Windows scheduled task (see docs/RUNBOOK_COMMERCIAL_ENGINE.md). The PC pulls;
# the VPS never connects to the PC. The dumps hold contact data: keep $Dest private.
param(
    [string]$Dest = "D:\BoRo\Backups\boro-gtm",
    [string]$HostAlias = "boro-vps",
    [int]$KeepDays = 30
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$log = Join-Path $Dest "pull.log"
function Say([string]$message) {
    "$(Get-Date -Format s) $message" | Add-Content -Path $log -Encoding utf8
}
$ssh = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=20")

try {
    $remote = (& ssh @ssh $HostAlias "ls -1t /opt/boro_gtm/backups/gtm_core_*.dump | head -1") -join ""
    if ($LASTEXITCODE -ne 0) { throw "cannot list backups on $HostAlias" }
    $remote = $remote.Trim()
    if ($remote -notmatch '^/opt/boro_gtm/backups/(gtm_core_(\d{8}T\d{6}Z)\.dump)$') {
        throw "unexpected backup name: '$remote'"
    }
    $name, $stamp = $Matches[1], $Matches[2]
    $remoteSha = ((& ssh @ssh $HostAlias "sha256sum $remote") -join "").Split(" ")[0].ToLower()
    if ($LASTEXITCODE -ne 0 -or $remoteSha -notmatch '^[0-9a-f]{64}$') { throw "cannot hash $remote" }

    $local = Join-Path $Dest $name
    if (-not (Test-Path $local)) {
        & scp -q @ssh "${HostAlias}:$remote" "$local.part"
        if ($LASTEXITCODE -ne 0) { throw "scp failed for $remote" }
        Move-Item -Force "$local.part" $local
    }
    $localSha = (Get-FileHash -Algorithm SHA256 $local).Hash.ToLower()
    if ($localSha -ne $remoteSha) {
        Remove-Item -Force $local
        throw "SHA-256 mismatch for $name (copy removed)"
    }

    Get-ChildItem -Path $Dest -Filter "gtm_core_*.dump" |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
        Remove-Item -Force

    $backupAt = [datetime]::ParseExact($stamp, "yyyyMMdd'T'HHmmss'Z'",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]"AssumeUniversal,AdjustToUniversal").ToString("yyyy-MM-ddTHH:mm:ss+00:00")
    $bytes = (Get-Item $local).Length
    $sql = "INSERT INTO ce_controls (key, value, updated_at) VALUES ('last_off_host_copy', " +
           "jsonb_build_object('at', now(), 'backup_at', '$backupAt', 'file', 'backups/$name', " +
           "'sha256', '$localSha', 'bytes', $bytes, 'verified', true, 'dest', 'pc'), now()) " +
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();"
    $sql | & ssh @ssh $HostAlias "cd /opt/boro_gtm && docker compose exec -T db psql -U gtm -d gtm_core -q -v ON_ERROR_STOP=1"
    if ($LASTEXITCODE -ne 0) { throw "copy verified but not recorded on $HostAlias" }
    Say "ok $name bytes=$bytes sha256=$localSha"
} catch {
    Say "FAILED: $_"
    exit 1
}
