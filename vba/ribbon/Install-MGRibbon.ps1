<#
.SYNOPSIS
  Embeds the "MG Macros" ribbon tab into an Office add-in or
  macro-enabled file, so no third-party ribbon editor is needed.

.DESCRIPTION
  Office files are zip packages. This script drops the matching
  customUI14.xml part (picked by the file's extension) into the
  package and registers the relationship Office looks for. It writes
  a .bak backup first and is safe to re-run (re-running replaces the
  ribbon part with the current XML).

  Close the file in Excel/PowerPoint before running.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File Install-MGRibbon.ps1 -File "C:\Users\me\AppData\Roaming\Microsoft\AddIns\MGMacros.xlam"

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File Install-MGRibbon.ps1 -File "C:\Users\me\Documents\MGMacros.ppam"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$File
)

$ErrorActionPreference = 'Stop'

$File = (Resolve-Path -LiteralPath $File).Path
$ext = [System.IO.Path]::GetExtension($File).ToLowerInvariant()

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
switch ($ext) {
    { $_ -in '.xlam', '.xlsm' } { $xmlSource = Join-Path $here 'Excel_MGMacros_customUI14.xml' }
    { $_ -in '.ppam', '.pptm' } { $xmlSource = Join-Path $here 'PPT_MGMacros_customUI14.xml' }
    default { throw "Unsupported file type '$ext' - expected .xlam/.xlsm (Excel) or .ppam/.pptm (PowerPoint)." }
}
if (-not (Test-Path -LiteralPath $xmlSource)) {
    throw "Ribbon XML not found next to this script: $xmlSource"
}

$backup = "$File.bak"
Copy-Item -LiteralPath $File -Destination $backup -Force
Write-Host "Backup written to $backup"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zip = [System.IO.Compression.ZipFile]::Open($File, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    # 1) The customUI14 part (replaced if a previous run added one).
    $existing = $zip.GetEntry('customUI/customUI14.xml')
    if ($null -ne $existing) { $existing.Delete() }
    $entry = $zip.CreateEntry('customUI/customUI14.xml')
    $writer = New-Object System.IO.StreamWriter($entry.Open())
    try { $writer.Write([System.IO.File]::ReadAllText($xmlSource)) }
    finally { $writer.Dispose() }

    # 2) The package relationship that tells Office the part exists.
    $relsEntry = $zip.GetEntry('_rels/.rels')
    if ($null -eq $relsEntry) {
        throw 'The file has no _rels/.rels part - is it really an Office file?'
    }
    $reader = New-Object System.IO.StreamReader($relsEntry.Open())
    try { $relsText = $reader.ReadToEnd() }
    finally { $reader.Dispose() }
    $relsXml = [xml]$relsText

    $relType = 'http://schemas.microsoft.com/office/2007/relationships/ui/extensibility'
    $already = @($relsXml.Relationships.Relationship) | Where-Object { $_.Type -eq $relType }
    if (-not $already) {
        $nsUri = 'http://schemas.openxmlformats.org/package/2006/relationships'
        $rel = $relsXml.CreateElement('Relationship', $nsUri)
        $rel.SetAttribute('Id', 'mgCustomUI14')
        $rel.SetAttribute('Type', $relType)
        $rel.SetAttribute('Target', 'customUI/customUI14.xml')
        [void]$relsXml.DocumentElement.AppendChild($rel)

        $relsEntry.Delete()
        $newRels = $zip.CreateEntry('_rels/.rels')
        $writer = New-Object System.IO.StreamWriter($newRels.Open())
        try { $writer.Write($relsXml.OuterXml) }
        finally { $writer.Dispose() }
    }
}
finally {
    $zip.Dispose()
}

Write-Host "MG Macros ribbon installed into $File"
Write-Host "Open the app - the 'MG Macros' tab appears next to Home."
