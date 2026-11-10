param([Parameter(Mandatory)][string]$Path, [string]$Sheet = '1. Job Setup', [string[]]$Expr = @(), [string[]]$Cells = @())
# Read-only: opens a copy of the workbook in a private hidden Excel, evaluates expressions and reads cells. Saves nothing.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'excel-common.ps1')
$copy = Join-Path $env:TEMP ('evalxl-' + [IO.Path]::GetFileName($Path)); Copy-Item -LiteralPath $Path $copy -Force
$xl = New-HiddenExcel
try {
  [void]$xl.Workbooks.Open($copy)
  $wb = $xl.Workbooks.Item(1); $ws = $wb.Worksheets.Item($Sheet)
  foreach ($e in $Expr) { "$e => $($ws.Evaluate($e))" }
  foreach ($c in $Cells) { $s, $a = $c -split '!', 2; $r = $wb.Worksheets.Item($s).Range($a); "$c = [$($r.Text)]  $($r.Formula)" }
  $wb.Close($false)
} finally { Close-HiddenExcel $xl; Remove-Item $copy -ErrorAction SilentlyContinue }
