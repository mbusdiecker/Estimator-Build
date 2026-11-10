param([string]$Gen = 'C:\Users\MattBusdiecker\Documents\IBW-Estimating-Tool-HANDOFF-Matt-Busdiecker-2026-10-05\IBW-Estimating-Tool\IBW-Estimating-Tool-DRAFT-v6.xlsx',
      [string[]]$Only = @())
# For each suspect feature: copy the generated v6, clear that feature's formulas in a private hidden Excel, save, reopen.
. (Join-Path $PSScriptRoot 'excel-common.ps1')
$cases = [ordered]@{
  'bom-labor-cols'   = { param($wb) $wb.Worksheets.Item('2. BOM Import').Range('AE8:AP407').ClearContents() }
  'ops-job-cols'     = { param($wb) $wb.Worksheets.Item('Labor Library').Range('O32:R228').ClearContents() }
  'areas-calc'       = { param($wb) $wb.Worksheets.Item('Labor Areas').Range('E6:I16').ClearContents() }
  'areas-E'          = { param($wb) $wb.Worksheets.Item('Labor Areas').Range('E6:E16').ClearContents() }
  'areas-F'          = { param($wb) $wb.Worksheets.Item('Labor Areas').Range('F6:F16').ClearContents() }
  'areas-G'          = { param($wb) $wb.Worksheets.Item('Labor Areas').Range('G6:G16').ClearContents() }
  'areas-H'          = { param($wb) $wb.Worksheets.Item('Labor Areas').Range('H6:H16').ClearContents() }
  'areas-I'          = { param($wb) $wb.Worksheets.Item('Labor Areas').Range('I6:I16').ClearContents() }
  'areas-FH'         = { param($wb) $s = $wb.Worksheets.Item('Labor Areas'); $s.Range('F6:F16').ClearContents(); $s.Range('H6:H16').ClearContents() }
  'labor-summary'    = { param($wb) $s = $wb.Worksheets.Item('Labor Summary'); $s.Range('B3:B4').ClearContents(); $s.Range('G8:H13').ClearContents(); $s.Range('B17:K20').ClearContents(); $s.Range('B23:B31').ClearContents() }
  'factor-calc'      = { param($wb) $wb.Worksheets.Item('Labor Library').Range('I5:J17').ClearContents() }
  'class-rates'      = { param($wb) $wb.Worksheets.Item('Labor Library').Range('C5:D8').ClearContents() }
}
foreach ($k in $cases.Keys) {
  if ($Only.Count -and $Only -notcontains $k) { continue }
  $t = Join-Path $env:TEMP "bisect-$k.xlsx"; Copy-Item -LiteralPath $Gen $t -Force
  $xl = New-HiddenExcel
  try {
    [void]$xl.Workbooks.Open($t); $wb = $xl.Workbooks.Item(1)
    & $cases[$k] $wb
    $wb.Save(); $wb.Close($false)
    try { [void]$xl.Workbooks.Open($t); "$k cleared: REOPEN OK"; $xl.Workbooks.Item(1).Close($false) } catch { "$k cleared: REOPEN FAIL" }
  } catch { "$k : setup error $($_.Exception.Message)" } finally { Close-HiddenExcel $xl }
}
