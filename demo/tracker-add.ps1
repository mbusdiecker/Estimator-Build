# Adds the estimator task rows to Matt's personal task tracker (sheet 2120989606629252), at the bottom.
# Matt asked for these rows on 2026-11-10. Token from env, never printed.
$ErrorActionPreference = 'Stop'
$tok = [Environment]::GetEnvironmentVariable('SMARTSHEET_API_TOKEN', 'User'); if (-not $tok) { $tok = $env:SMARTSHEET_API_TOKEN }
$h = @{ Authorization = "Bearer $tok"; 'Content-Type' = 'application/json' }
$C = @{ job = 7220814401015684; site = 1591314866802564; type = 5299649868763012; ms = 6094914494173060; resp = 5039160035151748; done = 3843114680487812; notes = 1980388706389892 }
$tasks = @(
  @('In Progress', 'Estimator: Rate card - apply Form-IBWBudget standard rates in v6; Matt supplies rates for lines with no standard', 'Template (11/10 copy): PM/survey/quoting/eng/field eng $70 cost, $120 bill; CM $70/$110; testing $55/$105; install $55 + 15% markup; warehousing $40/$40 + 15%; material and sub markup 15%. No standard in the template: labeling 1-580, closeout 1-590, travel labor bill, Lead/Foreman, Fiber Splicer, prevailing wage.'),
  @('In Progress', 'Estimator: Replace region services pricing with standard survey/design hours + travel', 'Matt 11/10: standard hours plus travel instead of the region services table (no NJ / Mid-Atlantic rows in it).'),
  @('Not Started', 'Estimator: Find the Intacct read-only drill-down on SharePoint (actual labor hours by job and cost code)', 'Needed to calibrate v6 labor units. Matt to locate; Claude cannot reach SharePoint.'),
  @('Not Started', 'Estimator: Buy the NECA Manual of Labor Units (licensed copy)', 'Fills the blank v6 pathway factors (cable tray, conduit pull, open ceiling, hard lid) and cable/conduit/tray/grounding units.'),
  @('Not Started', 'Estimator: RF operation minutes review with field lead or Wolf', 'After NECA and the rate card are in. Moves E-grade units (antenna, splitter, head end, sweep) to verified numbers.'),
  @('In Progress', 'Estimator: Job factors - use standard values, flag unknowns', 'Standard: closeout 40 h, order materials 8 h, design revisions 2 x 4 h. No standard yet: layout, material handling, working foreman %. Flagged on the Labor Summary tab.'),
  @('In Progress', 'Estimator: ROMtool OEM test and map ROMtool units to parts', 'Pass bar: antenna count and BOM within 15% of the OEM design on 8 of 10 past jobs. Claude lists about 15 candidates, Matt picks 10.'),
  @('In Progress', 'Estimator: Fix 3a Fiber Takeoff defaults', 'With the v6 lookup fix, 3a default answers add about 26.6 h of fiber labor on jobs with no fiber takeoff.'),
  @('Not Started', 'Estimator: Push estimator code to git', 'No repo exists yet for the estimating tool, Estimator Build or ROMtool. Repo and what to exclude (live cost data in reference/ and phase0/) to be decided.'),
  @('Not Started', 'Estimator: Test v6 on an ANS-installed job (queued)', 'Both test jobs so far were sub-installed, so v6 labor has not been checked against a real ANS-installed budget.'),
  @('Not Started', 'Estimator: Tauri desktop app (wait)', 'On hold per Matt 11/10. Early screen only for the Nov 23 week meeting if time allows.'),
  @('Not Started', 'Estimator: Concept memo refresh (wait)', 'On hold per Matt 11/10. Update with the Kean U demo results and v6 before the Nov 23 week meeting.'),
  @('Not Started', 'Estimator: Takeoff modules 3a-3e on labor assemblies (wait)', 'On hold per Matt 11/10. Modules still use one install type per part.')
)
$rows = foreach ($t in $tasks) {
  @{ toBottom = $true; cells = @(
      @{ columnId = $C.job; value = '26-00551' }, @{ columnId = $C.site; value = 'Estimator' },
      @{ columnId = $C.type; value = 'Automation' }, @{ columnId = $C.ms; value = $t[1]; strict = $false },
      @{ columnId = $C.resp; value = 'mbusdiecker@anscorporate.com' }, @{ columnId = $C.done; value = $t[0] },
      @{ columnId = $C.notes; value = $t[2] }) }
}
$body = ConvertTo-Json -InputObject @($rows) -Depth 6
$r = Invoke-RestMethod -Method Post -Uri 'https://api.smartsheet.com/2.0/sheets/2120989606629252/rows' -Headers $h -Body ([Text.Encoding]::UTF8.GetBytes($body))
"result: $($r.message); rows added: $(@($r.result).Count); row numbers: $((@($r.result) | ForEach-Object rowNumber) -join ',')"
