# Adds rows from a JSON file to Matt's personal task tracker (sheet 2120989606629252), at the bottom.
# JSON: [{"status": "...", "milestone": "...", "notes": "..."}]. Token from env, never printed.
param([Parameter(Mandatory)][string]$TasksJson, [string]$Site = 'Estimator')
$ErrorActionPreference = 'Stop'
$tok = [Environment]::GetEnvironmentVariable('SMARTSHEET_API_TOKEN', 'User'); if (-not $tok) { $tok = $env:SMARTSHEET_API_TOKEN }
$h = @{ Authorization = "Bearer $tok"; 'Content-Type' = 'application/json' }
$C = @{ job = 7220814401015684; site = 1591314866802564; type = 5299649868763012; ms = 6094914494173060; resp = 5039160035151748; done = 3843114680487812; notes = 1980388706389892 }
$tasks = Get-Content $TasksJson -Raw -Encoding UTF8 | ConvertFrom-Json
$rows = foreach ($t in $tasks) {
  @{ toBottom = $true; cells = @(
      @{ columnId = $C.job; value = '26-00551' }, @{ columnId = $C.site; value = $Site },
      @{ columnId = $C.type; value = 'Automation' }, @{ columnId = $C.ms; value = $t.milestone; strict = $false },
      @{ columnId = $C.resp; value = 'mbusdiecker@anscorporate.com' }, @{ columnId = $C.done; value = $t.status },
      @{ columnId = $C.notes; value = $t.notes }) }
}
$body = ConvertTo-Json -InputObject @($rows) -Depth 6
$r = Invoke-RestMethod -Method Post -Uri 'https://api.smartsheet.com/2.0/sheets/2120989606629252/rows' -Headers $h -Body ([Text.Encoding]::UTF8.GetBytes($body))
"result: $($r.message); rows added: $(@($r.result).Count); row numbers: $((@($r.result) | ForEach-Object rowNumber) -join ',')"
