param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Block", "Unblock", "Status")]
    [string]$Action,

    # Chỉ dùng thủ công để xóa các entry Facebook cũ từng được Add-Content.
    [switch]$CleanupLegacy
)

$ErrorActionPreference = "Stop"

$HostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"

$BeginMarker = "# BEGIN CAMERA-AGENT-FACEBOOK"
$EndMarker   = "# END CAMERA-AGENT-FACEBOOK"

$ManagedEntries = @(
    "0.0.0.0 facebook.com",
    "0.0.0.0 www.facebook.com",
    "0.0.0.0 m.facebook.com"
)

# ---------------------------------------------------------
# ADMIN CHECK
# ---------------------------------------------------------

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()

    $principal = New-Object Security.Principal.WindowsPrincipal($identity)

    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}


# ---------------------------------------------------------
# READ HOSTS
# ---------------------------------------------------------

function Read-HostsFile {
    if (-not (Test-Path $HostsPath)) {
        throw "Hosts file not found: $HostsPath"
    }

    return [System.IO.File]::ReadAllText($HostsPath)
}


# ---------------------------------------------------------
# REMOVE OUR MANAGED BLOCK
# ---------------------------------------------------------

function Remove-ManagedBlock {
    param(
        [string]$Content
    )

    $lines = $Content -split "`r?`n"

    $result = New-Object System.Collections.Generic.List[string]

    $insideBlock = $false
    $foundBegin = $false
    $foundEnd = $false

    foreach ($line in $lines) {

        if ($line.Trim() -eq $BeginMarker) {

            if ($insideBlock) {
                throw "Invalid hosts file: duplicate BEGIN marker."
            }

            if ($foundBegin) {
                throw "Invalid hosts file: multiple managed blocks detected."
            }

            $insideBlock = $true
            $foundBegin = $true

            continue
        }

        if ($line.Trim() -eq $EndMarker) {

            if (-not $insideBlock) {
                throw "Invalid hosts file: END marker without BEGIN marker."
            }

            $insideBlock = $false
            $foundEnd = $true

            continue
        }

        if (-not $insideBlock) {
            $result.Add($line)
        }
    }

    if ($insideBlock) {
        throw "Invalid hosts file: BEGIN marker exists but END marker is missing."
    }

    if ($foundBegin -and -not $foundEnd) {
        throw "Invalid hosts file: incomplete managed block."
    }

    return ($result -join "`r`n")
}


# ---------------------------------------------------------
# REMOVE LEGACY FACEBOOK ENTRIES
#
# Chỉ chạy khi có -CleanupLegacy.
#
# Dùng để xử lý các dòng cũ từng được tạo bằng:
#
# Add-Content hosts "0.0.0.0 facebook.com"
# ---------------------------------------------------------

function Remove-LegacyFacebookEntries {
    param(
        [string]$Content
    )

    $lines = $Content -split "`r?`n"

    $result = New-Object System.Collections.Generic.List[string]

    foreach ($line in $lines) {

        $trimmed = $line.Trim()

        $isLegacy =
            ($trimmed -match '^0\.0\.0\.0\s+facebook\.com\s*$') -or
            ($trimmed -match '^0\.0\.0\.0\s+www\.facebook\.com\s*$') -or
            ($trimmed -match '^0\.0\.0\.0\s+m\.facebook\.com\s*$')

        if ($isLegacy) {
            Write-Host "Removing legacy entry: $trimmed" -ForegroundColor Yellow
            continue
        }

        $result.Add($line)
    }

    return ($result -join "`r`n")
}


# ---------------------------------------------------------
# NORMALIZE END OF FILE
# ---------------------------------------------------------

function Normalize-HostsContent {
    param(
        [string]$Content
    )

    return $Content.TrimEnd("`r", "`n") + "`r`n"
}


# ---------------------------------------------------------
# WRITE ONLY WHEN CONTENT ACTUALLY CHANGES
# ---------------------------------------------------------

function Save-HostsFile {
    param(
        [string]$OldContent,
        [string]$NewContent
    )

    $OldContent = Normalize-HostsContent $OldContent
    $NewContent = Normalize-HostsContent $NewContent

    if ($OldContent -eq $NewContent) {

        Write-Host "Hosts file already in requested state. No change required." `
            -ForegroundColor DarkGray

        return $false
    }

    # Tạo backup ban đầu đúng 1 lần.
    $backupPath = "$HostsPath.camera-agent.backup"

    if (-not (Test-Path $backupPath)) {

        Copy-Item `
            -Path $HostsPath `
            -Destination $backupPath

        Write-Host "Backup created:" -ForegroundColor DarkGray
        Write-Host "  $backupPath" -ForegroundColor DarkGray
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    [System.IO.File]::WriteAllText(
        $HostsPath,
        $NewContent,
        $utf8NoBom
    )

    return $true
}


# ---------------------------------------------------------
# FLUSH DNS
# ---------------------------------------------------------

function Flush-Dns {
    Write-Host "Flushing DNS cache..." -ForegroundColor Cyan

    & ipconfig /flushdns | Out-Null
}


# ---------------------------------------------------------
# STATUS
# ---------------------------------------------------------

function Show-Status {

    $content = Read-HostsFile

    $hasBegin = $content.Contains($BeginMarker)
    $hasEnd   = $content.Contains($EndMarker)

    Write-Host ""
    Write-Host "=== CAMERA AGENT FACEBOOK ENFORCER ==="
    Write-Host ""

    if ($hasBegin -and $hasEnd) {
        Write-Host "Managed block : ENABLED" -ForegroundColor Red
    }
    elseif ($hasBegin -or $hasEnd) {
        Write-Host "Managed block : BROKEN MARKERS" -ForegroundColor Yellow
    }
    else {
        Write-Host "Managed block : DISABLED" -ForegroundColor Green
    }

    # Khi block của CAMERA-AGENT hợp lệ,
    # bỏ block đó ra trước khi tìm legacy entries.
    if ($hasBegin -and $hasEnd) {
        $contentToCheck = Remove-ManagedBlock $content
    }
    else {
        $contentToCheck = $content
    }

    $legacyPatterns = @(
        '^\s*0\.0\.0\.0\s+facebook\.com\s*$',
        '^\s*0\.0\.0\.0\s+www\.facebook\.com\s*$',
        '^\s*0\.0\.0\.0\s+m\.facebook\.com\s*$'
    )

    $legacyCount = 0

    foreach ($line in ($contentToCheck -split "`r?`n")) {

        foreach ($pattern in $legacyPatterns) {

            if ($line -match $pattern) {
                $legacyCount++
                break
            }
        }
    }

    Write-Host "Legacy entries: $legacyCount"

    if ($legacyCount -gt 0) {

        Write-Host ""
        Write-Host "Legacy Facebook entries still exist." `
            -ForegroundColor Yellow

        Write-Host "They may keep Facebook blocked even after normal Unblock."
    }

    Write-Host ""
}


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

if ($Action -eq "Status") {

    Show-Status
    exit 0
}


if (-not (Test-IsAdministrator)) {

    Write-Host ""
    Write-Host "ERROR: Administrator privileges required." `
        -ForegroundColor Red

    Write-Host ""
    Write-Host "Open PowerShell using:"
    Write-Host "  Run as administrator"
    Write-Host ""

    exit 1
}


$originalContent = Read-HostsFile

$newContent = Remove-ManagedBlock $originalContent


if ($CleanupLegacy) {

    $newContent = Remove-LegacyFacebookEntries $newContent
}


if ($Action -eq "Block") {

    $newContent = Normalize-HostsContent $newContent

    $managedBlock = @"
$BeginMarker
$($ManagedEntries -join "`r`n")
$EndMarker
"@

    $newContent =
        $newContent.TrimEnd("`r", "`n") +
        "`r`n`r`n" +
        $managedBlock +
        "`r`n"

    $changed = Save-HostsFile `
        -OldContent $originalContent `
        -NewContent $newContent

    if ($changed) {
        Flush-Dns
    }

    Write-Host ""
    Write-Host "FACEBOOK BLOCKED" -ForegroundColor Red
    Write-Host ""

    Show-Status

    exit 0
}


if ($Action -eq "Unblock") {

    $newContent = Normalize-HostsContent $newContent

    $changed = Save-HostsFile `
        -OldContent $originalContent `
        -NewContent $newContent

    if ($changed) {
        Flush-Dns
    }

    Write-Host ""
    Write-Host "FACEBOOK UNBLOCKED" -ForegroundColor Green
    Write-Host ""

    Show-Status

    exit 0
}