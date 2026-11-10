# Read-only: columns and last rows of Matt's personal task tracker (sheet 2120989606629252). Token from env, never printed.
$ErrorActionPreference = 'Stop'
$tok = [Environment]::GetEnvironmentVariable('SMARTSHEET_API_TOKEN', 'User'); if (-not $tok) { $tok = $env:SMARTSHEET_API_TOKEN }
$h = @{ Authorization = "Bearer $tok" }
$s = Invoke-RestMethod -Uri 'https://api.smartsheet.com/2.0/sheets/2120989606629252' -Headers $h
$s.columns | ForEach-Object { "COL $($_.id) | $($_.title) | $($_.type) | $(($_.options) -join '/')" }
"rows: $($s.rows.Count)"
$cols = @{}; $s.columns | ForEach-Object { $cols[[string]$_.id] = $_.title }
$s.rows | Select-Object -Last 12 | ForEach-Object {
  $r = $_; $vals = $r.cells | Where-Object { $_.displayValue -or $_.value } | ForEach-Object { "$($cols[[string]$_.columnId])=$(if ($_.displayValue) { [string]$_.displayValue } else { [string]$_.value })" }
  "ROW $($r.rowNumber) id=$($r.id) | " + ($vals -join ' | ')
}
