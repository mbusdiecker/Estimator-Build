# Updates estimator rows (by Milestone text prefix) on Matt's personal task tracker. Token from env, never printed.
param([Parameter(Mandatory)][string]$UpdatesJson)
$ErrorActionPreference = 'Stop'
$tok = [Environment]::GetEnvironmentVariable('SMARTSHEET_API_TOKEN', 'User'); if (-not $tok) { $tok = $env:SMARTSHEET_API_TOKEN }
$h = @{ Authorization = "Bearer $tok"; 'Content-Type' = 'application/json' }
$sid = 2120989606629252; $MS = 6094914494173060; $DONE = 3843114680487812; $NOTES = 1980388706389892
$s = Invoke-RestMethod -Uri "https://api.smartsheet.com/2.0/sheets/$sid" -Headers $h
$upd = Get-Content $UpdatesJson -Raw -Encoding UTF8 | ConvertFrom-Json
$rows = foreach ($u in $upd) {
  $row = $s.rows | Where-Object { ($_.cells | Where-Object { $_.columnId -eq $MS }).value -like "$($u.prefix)*" } | Select-Object -First 1
  if (-not $row) { throw "row not found: $($u.prefix)" }
  $cells = @()
  if ($u.status) { $cells += @{ columnId = $DONE; value = $u.status } }
  if ($u.notes) { $cells += @{ columnId = $NOTES; value = $u.notes } }
  @{ id = $row.id; cells = $cells }
  "row $($row.rowNumber): $($u.prefix)" | Write-Host
}
$body = ConvertTo-Json -InputObject @($rows) -Depth 6
$r = Invoke-RestMethod -Method Put -Uri "https://api.smartsheet.com/2.0/sheets/$sid/rows" -Headers $h -Body ([Text.Encoding]::UTF8.GetBytes($body))
"result: $($r.message); rows updated: $(@($r.result).Count)"
