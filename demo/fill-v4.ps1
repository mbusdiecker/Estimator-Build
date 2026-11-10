param(
  [string]$Job = '25-00151',
  [string]$Scenario = 'A',
  [string]$Version = 'v4'
)
$Src = "C:\Users\MattBusdiecker\Documents\IBW-Estimating-Tool-HANDOFF-Matt-Busdiecker-2026-10-05\IBW-Estimating-Tool\IBW-Estimating-Tool-DRAFT-$Version.xlsx"
# Scenarios: A = tool defaults + job facts; B = A + estimator hours; C = B + the rates and markups used on the job (v5 only)
# Fills a COPY of the generated v4 workbook from job inputs in a hidden Excel instance.
# Writes input cells only; hashes every formula before and after and aborts on any change.
$ErrorActionPreference = 'Stop'
$dir  = Join-Path $PSScriptRoot $Job
$bom  = Get-Content (Join-Path $dir 'bom.json') -Raw | ConvertFrom-Json
$out  = Join-Path $dir "$Job-$Version-$Scenario.xlsx"
Copy-Item $Src $out -Force

$Overtype = @{}   # designed overtype cells on Job Setup (formula answers the estimator may replace), keyed "sheet!row,col"
function Get-FormulaHash($wb) {
  $sb = New-Object System.Text.StringBuilder; $n = 0
  foreach ($ws in @($wb.Worksheets)) {
    $ur = $ws.UsedRange; $f = $ur.Formula
    if ($f -is [array]) {
      $r0 = $ur.Row; $c0 = $ur.Column
      for ($i = 1; $i -le $f.GetLength(0); $i++) { for ($j = 1; $j -le $f.GetLength(1); $j++) {
        $v = [string]$f[$i, $j]; if ($Overtype.ContainsKey("$($ws.Name)!$($r0+$i-1),$($c0+$j-1)")) { continue }
        if ($v.StartsWith('=')) { [void]$sb.Append("$($ws.Name)!$($r0+$i-1),$($c0+$j-1)=$v`n"); $n++ } } }
    } elseif (([string]$f).StartsWith('=')) { [void]$sb.Append("$($ws.Name)!$($ur.Address())=$f`n"); $n++ }
  }
  $sha = [Security.Cryptography.SHA256]::Create()
  $h = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString()))).Replace('-', '')
  return @($h, $n)
}

. (Join-Path $PSScriptRoot 'excel-common.ps1')   # COM retry filter + private hidden Excel helpers
$xl = New-HiddenExcel
try {
  [void]$xl.Workbooks.Open($out)
  $wb = $xl.Workbooks.Item(1)
  if ($wb.Worksheets.Count -lt 30) { throw "workbook not populated: $($wb.Worksheets.Count) sheets" }
  $xl.ScreenUpdating = $false
  $xl.Calculation = -4135   # manual: v6 recalculates the labor library on every write; one full rebuild at the end
  $js = $wb.Worksheets.Item('1. Job Setup')
  if ($Scenario -eq 'C') {
    for ($r = 9; $r -le 80; $r++) { $a = [string]$js.Cells.Item($r, 1).Value2
      if ($a.StartsWith('Daily hotel cost') -or $a.StartsWith('Per diem rate')) { $Overtype["1. Job Setup!$r,2"] = 1 } }
  }
  $before = Get-FormulaHash $wb
  function Set-Answer($label, $value) {
    for ($r = 9; $r -le 70; $r++) {
      $a = [string]$js.Cells.Item($r, 1).Value2
      if ($a.Trim().ToLower().StartsWith($label.ToLower())) { $cell = $js.Cells.Item($r, 2); [void]$cell.GetType().InvokeMember('Value2', [Reflection.BindingFlags]::SetProperty, $null, $cell, @($value)); return }
    }
    throw "Job Setup label not found: $label"
  }
  # ERP paste strip (job facts from the tracker)
  $js.Range('B6').Value2 = $Job
  $js.Range('C6').Value2 = 'Kean University'
  $js.Range('D6').Value2 = 'Verizon Wireless'
  $js.Range('I6').Value2 = 'NJ'
  $js.Range('L6').Value2 = 'LOB-415 - Wireless - In-Building'
  $js.Range('M6').Value2 = 'DAS'
  $js.Range('Q6').Value2 = 'DRAN (passive DAS + Samsung radios)'
  $js.Range('R6').Value2 = 'Competitive Bid'
  $js.Range('V6').Value2 = 'No'
  # Job facts
  Set-Answer 'Quote type' 'Firm price'
  Set-Answer 'Technology' '4G + 5G'
  Set-Answer 'Square footage' 88215
  Set-Answer 'Install labor performed by' 'Subcontractor'
  Set-Answer 'Testing performed by' 'Subcontractor'
  Set-Answer 'Benchmarking performed by' 'None'
  if ($Scenario -in 'B','C') {
    # Estimator judgment inputs, as entered on the real budget
    Set-Answer 'Site survey basis' 'Internal hours'
    Set-Answer 'Site survey hours' 9
    Set-Answer 'iBwave design basis' 'Internal hours'
    Set-Answer 'Design hours' 46
    Set-Answer 'PM hours override' 40
    Set-Answer 'Warehousing / staging required?' 'Yes'
    Set-Answer 'Number of travelers' 1
    Set-Answer 'One-way travel days' 1
    if ($Version -ne 'v4') {
      Set-Answer 'Quoting hours' 24
      Set-Answer 'Warehousing hours override' 4
      Set-Answer 'Staff trip nights' 2
      Set-Answer 'Number of travelers' 0
      Set-Answer 'Fiber priced from' 'BOM lines'
    }
  }
  if ($Scenario -eq 'C') {
    # Rates and markups as used on the job (rates in, price out)
    Set-Answer 'Daily hotel cost' 150
    Set-Answer 'Per diem rate' 0
    $rm = $wb.Worksheets.Item('Rates & Markups')
    $rates = @{ 'Site Survey' = @(175, 55); 'Quoting' = @(175, 55); 'Engineering / Design' = @(175, 55); 'Project Management' = @(205, 65); 'Warehousing' = @(145, 45) }
    for ($i = 6; $i -le 40; $i++) {
      $n = [string]$rm.Cells.Item($i, 1).Value2
      if ($rates.ContainsKey($n)) { $c3 = $rm.Cells.Item($i, 3); $c4 = $rm.Cells.Item($i, 4)
        [void]$c3.GetType().InvokeMember('Value2', [Reflection.BindingFlags]::SetProperty, $null, $c3, @([double]$rates[$n][0]))
        [void]$c4.GetType().InvokeMember('Value2', [Reflection.BindingFlags]::SetProperty, $null, $c4, @([double]$rates[$n][1])) }
      if ($n -in 'Material markup', 'Subcontractor markup') { $c2 = $rm.Cells.Item($i, 2)
        [void]$c2.GetType().InvokeMember('Value2', [Reflection.BindingFlags]::SetProperty, $null, $c2, @([double]0.15)) }
    }
  }
  # Testing: cellular bands only; sub performs sweep/PIM, so no ANS testing phases
  $ts = $wb.Worksheets.Item('5. Testing')
  $ts.Range('B6').Value2 = 0; $ts.Range('B7').Value2 = 0; $ts.Range('B8').Value2 = 0; $ts.Range('B9').Value2 = 1
  $ts.Range('B30').Value2 = 0; $ts.Range('B31').Value2 = 0
  # BOM raw paste zone from row 8: A type, B mfr, D model, E description, I qty, U unit cost override
  $bm = $wb.Worksheets.Item('2. BOM Import'); $r = 8
  foreach ($p in $bom) {
    $mpn = if ([string]::IsNullOrWhiteSpace($p.mpn)) { [string]$p.description } else { [string]$p.mpn }
    if ([string]::IsNullOrWhiteSpace($mpn)) { $mpn = [string]$p.type }
    $bm.Cells.Item($r, 1).Value2 = [string]$p.type
    $bm.Cells.Item($r, 2).Value2 = [string]$p.mfr
    $bm.Cells.Item($r, 4).Value2 = $mpn
    $bm.Cells.Item($r, 5).Value2 = [string]$p.description
    $bm.Cells.Item($r, 9).Value2 = [double]$p.qty
    if ($p.unit_cost -ne $null) { $bm.Cells.Item($r, 21).Value2 = [double]$p.unit_cost }
    if ($Version -ne 'v4' -and $p.supplied_by -eq 'Customer') { $bm.Cells.Item($r, 28).Value2 = 'Customer' }
    # lines with no part number (grounding, rack): the estimator picks the install type, as on the real budget
    if ($Version -eq 'v6' -and [string]::IsNullOrWhiteSpace($p.mpn) -and $p.install_type) { $bm.Cells.Item($r, 18).Value2 = [string]$p.install_type }
    $r++
  }
  # Subcontractor quote (an input the estimator had). DAS install sub belongs on 5-160.
  $sub = $wb.Worksheets.Item('7. Subcontractors')
  $sub.Range('A5').Value2 = 'DAS field installation (sub quote)'
  $sub.Range('D5').Value2 = 31430
  $wb.Worksheets.Item('BUDGET').Range('M34').Value2 = 2

  $after = Get-FormulaHash $wb
  "formulas before=$($before[1]) after=$($after[1]) hash_match=$($before[0] -eq $after[0])"
  if ($before[0] -ne $after[0]) { throw 'TAMPER CHECK FAILED: a formula changed' }

  $xl.CalculateFullRebuild()
  # Error scan over populated sheets
  $errs = @(); $sheets = 0
  foreach ($ws in @($wb.Worksheets)) {
    $sheets++
    $v = $ws.UsedRange.Value2
    if ($v -is [array]) { foreach ($x in $v) { if ($x -is [int] -and $x -lt -2146820000) { $errs += $ws.Name } } }
  }
  "sheets=$sheets error_cells=$($errs.Count) $((($errs | Group-Object | ForEach-Object { "$($_.Name):$($_.Count)" }) -join ' '))"
  # Dump BUDGET and BOM summary
  $bd = $wb.Worksheets.Item('BUDGET'); $lines = @()
  for ($i = 1; $i -le 64; $i++) {
    $cells = @(); for ($c = 1; $c -le 13; $c++) { $t = $bd.Cells.Item($i, $c).Text; if ($t -ne '') { $cells += "$([char](64+$c))=$t" } }
    if ($cells) { $lines += "R$i " + ($cells -join ' | ') }
  }
  $lines | Set-Content (Join-Path $dir "budget-$Version-$Scenario.txt") -Encoding utf8
  if ($Version -ne 'v4') { $bdc = $wb.Worksheets.Item('BUDGET'); "checks: " + ((57..62 | ForEach-Object { "$($bdc.Cells.Item($_,12).Text)=$($bdc.Cells.Item($_,13).Text)" }) -join '; ') }
  "BOM lines=$($bm.Range('B4').Text) material=$($bm.Range('E4').Text) hrs=$($bm.Range('H4').Text) missing_price=$($bm.Range('B5').Text) missing_type=$($bm.Range('E5').Text)"
  $wb.Save()
  $wb.Close($false)
} finally {
  Close-HiddenExcel $xl
}
