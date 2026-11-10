param([Parameter(Mandatory)][string]$Path)
# Opens a copy in a private hidden Excel, saves it, and reopens it. Reports whether Excel can reopen its own save.
. (Join-Path $PSScriptRoot 'excel-common.ps1')
$t = Join-Path $env:TEMP ('rt-' + [IO.Path]::GetFileName($Path)); Copy-Item -LiteralPath $Path $t -Force
$xl = New-HiddenExcel
try {
  [void]$xl.Workbooks.Open($t); $wb = $xl.Workbooks.Item(1); $wb.Save(); $wb.Close($false)
  try { [void]$xl.Workbooks.Open($t); "ROUNDTRIP OK: sheets=$($xl.Workbooks.Item(1).Worksheets.Count)"; $xl.Workbooks.Item(1).Close($false) }
  catch { "ROUNDTRIP FAIL: $($_.Exception.Message)" }
} finally { Close-HiddenExcel $xl }
