"""Documentation pages smoke tests: GET /help and GET /how-it-works must serve
200 with the key sections, and the index page must link both."""

HTML_SNIPPETS_HELP = [
    # headline + step headings driving the documented workflow
    "How-To",
    "Calibrate the scale",
    "Draw walls",
    "critical-area",
    "Place the element chain",
    "Solve the cascade",
    "NFPA two-tier",
    "SS-RSRP",
    "Multiband composite",
    "BOM",
    # the 'drive it from Claude' section with worked curls on project 1
    "Drive it from Claude",
    "curl -s $BASE/health",
    "curl -s -X POST $BASE/projects/1/cascade/solve",
    "/projects/1/floors/exf1/predict",
    "/projects/1/floors/exf1/composite?bands=N71_600,N41_2500,N77_CBAND",
    "nfpa_report",
    "/exports/projects/1/bom.csv",
    # v0.6.0 phase E3 sections
    "Commercial wrap constants",
    "What-if scenarios",
    "Bid form export",
    "/estimates/1/scenarios",
    "bid_form.pdf",
    "/estimates/{id}/schedule",
    "/estimates/{id}/cashflow",
    # v0.7.0 phase E4 sections
    "Sanity gates",
    "missing-scope",
    "Actuals loop",
    "propose-refit",
    "apply-refit",
    "company_labor_overrides.json",
    "RF link",
    "draft-estimate",
    "AI endpoints",
    "/ai/rfp-scan",
    "ai gateway unreachable",
    # v0.8.0 phase E5 section
    "Stadium morning package",
    "scripts/build_stadium_package.py",
    "MORNING-REPORT.md",
    "/reports/stadium",
]

HTML_SNIPPETS_MATH = [
    # headline + section headings
    "How it works",
    "Cascade link budget",
    "ITU-R P.1238",
    "P.2040",
    "Wall crossings",
    "composite sum",
    "KPI math",
    "Multiband composite",
    "Validity limits",
    "Documented deviations",
    "Standards referenced",
    # standards named as references (not claims)
    "ITU-R P.2040",
    "COST 231 Multi-Wall",
    "3GPP TR 38.901",
    "NFPA 1225",
    # formulas / numbers cross-checked against romtool + tests
    "20 log10",
    "38.5 dB",           # FSPL(1 m, 2 GHz) anchor
    "58 dB",             # FSPL(10 m, 1900 MHz) anchor
    "15.4 dB",           # floor loss dB/floor
    "10 log10(1.1)",     # composite linear-sum example
    "-95 dBm",           # KPI threshold
    "99%",               # NFPA tier-2 min area
    "-80", "-90", "-100", "-110",   # SS-RSRP class table mins
    "1183.2 MHz",        # log-domain cable interpolation midpoint
    "12.93",             # concrete 15 dB scaled to 700 MHz
    "16.29",             # concrete scaled to 3840 MHz (n77)
    "33.33%",            # synthetic-room KPI hand example
    "11.11",             # RSRP class hand example percentages
    "22.22",
    "55.56",
    "-74.2 dBm",         # cascade hand example: repeater in
    "-49.2 dBm",         # cascade hand example: repeater out
    "-51.0 dBm",         # cascade hand example: A1 EIRP
    "no ray tracing",    # documented deviation verbatim
    # v0.6.0 phase E3: section 11, the documented $-chain wrap order
    "11. Commercial wrap order",
    "SMALL_TOOLS",
    "INSURANCE_BOND",
    "1 + 182/365",
    "&dollar;0.00",
    "&dollar;80,528.33",  # wrap-order hand example grand total
    "80.53",             # per-antenna-point unit price hand example
    # v0.7.0 phase E4: sections 12-13, sanity + actuals math
    "12. Sanity gate math",
    "13. Actuals drift math",
    "$110.20/task-hour",
    "3.0144",
    "92.31%",
    "1.1143",
    "2.16",
    "0.5&ndash;2.0",
    # v0.8.0 phase E5: section 14, the stadium package
    "14. The stadium package",
    "Crew-duration gate basis",
    "Class 4 at best",
    "260 DAS antenna points",
    "MORNING-REPORT.md",
]


def test_help_page_served(client):
    r = client.get("/help")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    for snippet in HTML_SNIPPETS_HELP:
        assert snippet in body, "missing on /help: %r" % snippet
    # step headings are anchor-linked and the page self-references the math page
    assert 'id="s3"' in body
    assert 'href="/how-it-works"' in body


def test_how_it_works_page_served(client):
    r = client.get("/how-it-works")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    for snippet in HTML_SNIPPETS_MATH:
        assert snippet in body, "missing on /how-it-works: %r" % snippet
    # reference-not-claim wording present for the standards
    assert "editable planning defaults" in body
    assert 'href="/help"' in body


def test_index_nav_links_both_docs(client):
    body = client.get("/").text
    assert 'href="/help"' in body
    assert 'href="/how-it-works"' in body


def test_config_numbers_match_doc_tables(client):
    """/how-it-works hard-codes shipped config values; pin them to /config."""
    cfg = client.get("/config").json()
    body = client.get("/how-it-works").text
    assert cfg["environments"]["office"]["n"] == 30.0
    assert cfg["environments"]["commercial"]["n"] == 22.0
    assert cfg["environments"]["residential"]["n"] == 28.0
    assert cfg["floor_loss_db_per_floor"] == 15.4
    assert cfg["wall_loss_ref_mhz"] == 2000.0
    assert cfg["wall_materials"]["concrete"]["loss_db"] == 15.0
    assert cfg["wall_materials"]["metal"]["loss_db"] == 30.0
    tiers = {t["tier"]: t for t in cfg["kpi_presets"]["nfpa_two_tier"]}
    assert tiers[1]["threshold_dbm"] == -95.0 and tiers[1]["min_area_pct"] == 95.0
    assert tiers[2]["threshold_dbm"] == -95.0 and tiers[2]["min_area_pct"] == 99.0
    classes = {c["name"]: c["min_dbm"]
               for c in cfg["kpi_presets"]["nr_ssrsp"]["class_table"]}
    assert classes == {"excellent": -80.0, "good": -90.0,
                       "fair": -100.0, "edge": -110.0}
    # the material table row values appear on the page
    for mat, db in (("Concrete", 15), ("CMU block", 10), ("Drywall", 4),
                    ("Metal cladding/door", 30)):
        assert ('>%d<' % db) in body, mat
