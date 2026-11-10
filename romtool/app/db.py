"""Single-file SQLite store. Each project is one row carrying its full model
as a JSON blob (floors / walls / elements / embedded floor images), per spec.
Phase 4 adds the same pattern for Class 1 estimates: definition JSON blob +
computed recap JSON blob per row. Phase E2 (v0.5.0) adds the governance
tables: estimate_revisions (immutable revision snapshots with diff
summaries), estimate_events (full audit log), assumptions (assumption
register, auto-populated from E-quality labor lines) and vendor_quotes
(quote register with binding/expiry dates). Phase E3 (v0.6.0) adds
estimate_scenarios (named what-if overlays; results recomputed against the
current base at diff time, never snapshotted). Phase E4 (v0.7.0) adds
estimate_actuals (per-estimate field rows feeding the drift report) and
refit_proposals (frozen review-only refit outputs; the company override
layer lives in config/company_labor_overrides.json, not SQL).
"""

import json
import os
import sqlite3
from contextlib import closing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    recap TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimate_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    definition TEXT NOT NULL,
    recap TEXT NOT NULL,
    diff TEXT,
    note TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (estimate_id, revision)
);
CREATE TABLE IF NOT EXISTS estimate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    revision INTEGER,
    changed_paths TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    catalog_id TEXT,
    location_id TEXT,
    line_key TEXT,
    text TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    review_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendor_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    vendor TEXT NOT NULL,
    scope TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    binding_until TEXT NOT NULL,
    inclusions TEXT NOT NULL DEFAULT '[]',
    exclusions TEXT NOT NULL DEFAULT '[]',
    linked_line_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimate_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    notes TEXT,
    overlay TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimate_actuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    catalog_id TEXT NOT NULL,
    location_id TEXT,
    qty REAL NOT NULL,
    hours REAL NOT NULL,
    cost REAL,
    date TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refit_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id INTEGER NOT NULL,
    proposal TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actuals_estimate ON estimate_actuals
    (estimate_id, id);
CREATE INDEX IF NOT EXISTS idx_events_estimate ON estimate_events
    (estimate_id, id);
CREATE INDEX IF NOT EXISTS idx_revisions_estimate ON estimate_revisions
    (estimate_id, revision);
"""

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)


def db_path():
    return os.environ.get("ROMTOOL_DB") or os.path.join(_REPO_ROOT, "data", "romtool.db")


def _connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """v0.4.0 DBs predate the governance columns; ALTER them in place."""
    cols = {r["name"] for r in
            conn.execute("PRAGMA table_info(estimates)").fetchall()}
    if "state" not in cols:
        conn.execute("ALTER TABLE estimates ADD COLUMN state TEXT"
                     " NOT NULL DEFAULT 'draft'")
    if "revision" not in cols:
        conn.execute("ALTER TABLE estimates ADD COLUMN revision INTEGER"
                     " NOT NULL DEFAULT 1")


def _now():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_project(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "model": json.loads(row["model"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_project(name, model):
    model = dict(model or {})
    model.setdefault("name", name)
    now = _now()
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, model, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(model), now, now),
        )
        conn.commit()
        return get_project(cur.lastrowid)


def list_projects():
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, name, model, created_at, updated_at FROM projects ORDER BY id"
        ).fetchall()
    out = []
    for row in rows:
        model = json.loads(row["model"])
        out.append({
            "id": row["id"],
            "name": row["name"],
            "example": bool(model.get("example")),
            "environment": model.get("environment"),
            "floors": len(model.get("floors", [])),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return out


def get_project(pid):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, name, model, created_at, updated_at FROM projects WHERE id = ?",
            (pid,),
        ).fetchone()
    return _row_to_project(row) if row else None


def save_model(pid, name, model):
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE projects SET name = ?, model = ?, updated_at = ? WHERE id = ?",
            (name, json.dumps(model), _now(), pid),
        )
    return get_project(pid)


def delete_project(pid):
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- estimates

def _row_to_estimate(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "state": row["state"],
        "revision": row["revision"],
        "definition": json.loads(row["definition"]),
        "recap": json.loads(row["recap"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _add_event(conn, estimate_id, actor, action, revision=None,
               changed_paths=None, summary=""):
    conn.execute(
        "INSERT INTO estimate_events (estimate_id, ts, actor, action, revision,"
        " changed_paths, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (estimate_id, _now(), actor, action, revision,
         json.dumps(changed_paths or []), summary))


def create_estimate(name, definition, recap, actor="api"):
    """Store a new estimate at revision 1 (state draft) + audit event."""
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO estimates (name, definition, recap, state, revision,"
            " created_at, updated_at) VALUES (?, ?, ?, 'draft', 1, ?, ?)",
            (name, json.dumps(definition), json.dumps(recap), now, now),
        )
        eid = cur.lastrowid
        conn.execute(
            "INSERT INTO estimate_revisions (estimate_id, revision, definition,"
            " recap, diff, note, actor, created_at)"
            " VALUES (?, 1, ?, ?, NULL, ?, ?, ?)",
            (eid, json.dumps(definition), json.dumps(recap),
             "initial compute", actor, now))
        _add_event(conn, eid, actor, "estimate.created", 1, ["estimate"],
                   "estimate created (revision 1, state draft)")
        conn.commit()
        return get_estimate(eid)


def list_estimates():
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, name, definition, recap, state, revision,"
            " created_at, updated_at"
            " FROM estimates ORDER BY id"
        ).fetchall()
    out = []
    for row in rows:
        recap = json.loads(row["recap"])
        out.append({
            "id": row["id"],
            "name": row["name"],
            "state": row["state"],
            "revision": row["revision"],
            "locations": len(recap.get("definition", {}).get("locations", [])),
            "lines": len(recap.get("lines", [])),
            "total_crew_hours_mid": recap.get("totals", {}).get("total_crew_hours_mid"),
            "grand_total_mid": recap.get("cost", {}).get("grand_total_usd", {})
                                     .get("mid"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return out


def get_estimate(eid):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, name, definition, recap, state, revision,"
            " created_at, updated_at"
            " FROM estimates WHERE id = ?",
            (eid,),
        ).fetchone()
    return _row_to_estimate(row) if row else None


def revise_estimate(eid, name, definition, recap, diff, note, actor):
    """Create revision N+1: update the estimate row, freeze an immutable
    snapshot with the diff summary, and log the audit event. Returns
    (estimate, new_revision)."""
    now = _now()
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT revision FROM estimates WHERE id = ?",
                           (eid,)).fetchone()
        if row is None:
            return None, None
        new_rev = int(row["revision"]) + 1
        conn.execute(
            "UPDATE estimates SET name = ?, definition = ?, recap = ?,"
            " revision = ?, updated_at = ? WHERE id = ?",
            (name, json.dumps(definition), json.dumps(recap), new_rev, now, eid))
        conn.execute(
            "INSERT INTO estimate_revisions (estimate_id, revision, definition,"
            " recap, diff, note, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, new_rev, json.dumps(definition), json.dumps(recap),
             json.dumps(diff), note, actor, now))
        _add_event(conn, eid, actor, "estimate.revised", new_rev,
                   diff.get("changed_paths"), "%s%s" % (
                       diff.get("summary", ""), (" [%s]" % note) if note else ""))
        conn.commit()
        return get_estimate(eid), new_rev


def set_estimate_state(eid, to_state, expected_from, actor, note=""):
    """Guarded state transition: the UPDATE only lands if the row is still in
    expected_from. Returns (estimate, from_state) or (None, expected_from)."""
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT state, revision FROM estimates WHERE id = ?",
            (eid,)).fetchone()
        if row is None:
            return None, None
        from_state = row["state"]
        if from_state != expected_from:
            return None, None
        conn.execute(
            "UPDATE estimates SET state = ?, updated_at = ? WHERE id = ? AND"
            " state = ?",
            (to_state, _now(), eid, from_state))
        _add_event(conn, eid, actor, "estimate.state_changed", row["revision"],
                   ["state"],
                   "%s -> %s%s" % (from_state, to_state,
                                   (": " + note) if note else ""))
        conn.commit()
        return get_estimate(eid), from_state


def delete_estimate(eid):
    """Remove the estimate and all governance children (revisions, audit
    events, assumptions, quotes, scenarios, actuals, refit proposals).
    Blocked states are enforced in routes."""
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM estimate_revisions WHERE estimate_id = ?", (eid,))
        conn.execute("DELETE FROM estimate_events WHERE estimate_id = ?", (eid,))
        conn.execute("DELETE FROM assumptions WHERE estimate_id = ?", (eid,))
        conn.execute("DELETE FROM vendor_quotes WHERE estimate_id = ?", (eid,))
        conn.execute("DELETE FROM estimate_scenarios WHERE estimate_id = ?", (eid,))
        conn.execute("DELETE FROM estimate_actuals WHERE estimate_id = ?", (eid,))
        conn.execute("DELETE FROM refit_proposals WHERE estimate_id = ?", (eid,))
        cur = conn.execute("DELETE FROM estimates WHERE id = ?", (eid,))
        return cur.rowcount > 0


# ------------------------------------------------------ estimate revisions

def list_revisions(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT revision, note, actor, diff, created_at"
            " FROM estimate_revisions WHERE estimate_id = ? ORDER BY revision",
            (eid,)).fetchall()
    return [{
        "revision": r["revision"], "note": r["note"], "actor": r["actor"],
        "diff": json.loads(r["diff"]) if r["diff"] else None,
        "created_at": r["created_at"],
    } for r in rows]


def get_revision(eid, n):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT revision, definition, recap, diff, note, actor, created_at"
            " FROM estimate_revisions WHERE estimate_id = ? AND revision = ?",
            (eid, n)).fetchone()
    if row is None:
        return None
    return {
        "revision": row["revision"],
        "definition": json.loads(row["definition"]),
        "recap": json.loads(row["recap"]),
        "diff": json.loads(row["diff"]) if row["diff"] else None,
        "note": row["note"], "actor": row["actor"],
        "created_at": row["created_at"],
    }


# -------------------------------------------------------------- audit log

def list_events(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, ts, actor, action, revision, changed_paths, summary"
            " FROM estimate_events WHERE estimate_id = ? ORDER BY id",
            (eid,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"],
             "action": r["action"], "revision": r["revision"],
             "changed_paths": json.loads(r["changed_paths"] or "[]"),
             "summary": r["summary"]} for r in rows]


# ----------------------------------------------------- assumption register

def _row_to_assumption(row):
    return {"id": row["id"], "estimate_id": row["estimate_id"],
            "revision": row["revision"], "catalog_id": row["catalog_id"],
            "location_id": row["location_id"], "line_key": row["line_key"],
            "text": row["text"], "owner": row["owner"], "status": row["status"],
            "review_date": row["review_date"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def sync_assumptions(eid, recap_lines, revision, owner, quality="E",
                     targets_fn=None, actor="api"):
    """Re-sync the register with the current revision's E-quality labor
    lines: add rows for new lines (status open), DROP open rows whose line
    vanished (verified/closed rows persist - their close-out evidence may
    still matter). One audit event per sync that changed something."""
    from romtool.governance import auto_assumption_targets

    fn = targets_fn or auto_assumption_targets
    want = {t["line_key"]: t for t in fn(recap_lines) }
    added, removed = [], []
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "SELECT id, line_key, status FROM assumptions WHERE estimate_id = ?",
            (eid,)).fetchall()
        existing = {}
        for r in rows:
            if r["line_key"]:
                existing[r["line_key"]] = r
        now = _now()
        for key, t in want.items():
            if key in existing:
                continue
            cur = conn.execute(
                "INSERT INTO assumptions (estimate_id, revision, catalog_id,"
                " location_id, line_key, text, owner, status, review_date,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?)",
                (eid, revision, t["catalog_id"], t["location_id"], key,
                 t["text"], owner, now, now))
            added.append({"assumption_id": cur.lastrowid, "line_key": key})
        for key, r in existing.items():
            if key not in want and r["status"] == "open":
                conn.execute("DELETE FROM assumptions WHERE id = ?", (r["id"],))
                removed.append({"assumption_id": r["id"], "line_key": key})
        if added or removed:
            _add_event(
                conn, eid, actor, "assumptions.synced", revision,
                ["assumptions#%s" % a["line_key"] for a in added]
                + ["-assumptions#%s" % r["line_key"] for r in removed],
                "register sync at revision %d: +%d / -%d rows (%d kept)"
                % (revision, len(added), len(removed),
                   len(existing) - len(removed)))
        conn.commit()
    return {"added": added, "removed": removed,
            "kept": len(existing) - len(removed)}


def list_assumptions(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM assumptions WHERE estimate_id = ? ORDER BY id",
            (eid,)).fetchall()
    return [_row_to_assumption(r) for r in rows]


def get_assumption(eid, aid):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM assumptions WHERE estimate_id = ? AND id = ?",
            (eid, aid)).fetchone()
    return _row_to_assumption(row) if row else None


def add_assumption(eid, text, owner, review_date, actor, revision,
                   catalog_id=None, location_id=None, line_key=None):
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO assumptions (estimate_id, revision, catalog_id,"
            " location_id, line_key, text, owner, status, review_date,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (eid, revision, catalog_id, location_id, line_key, text, owner,
             review_date, now, now))
        aid = cur.lastrowid
        _add_event(conn, eid, actor, "assumption.added", revision,
                   ["assumptions#%d" % aid],
                   "manual assumption added: %s" % text[:80])
        conn.commit()
    return get_assumption(eid, aid)


def update_assumption(eid, aid, changes, actor):
    """changes: dict with any of text/owner/review_date; logs per-field
    changed paths. Returns updated row or None when the row is missing."""
    allowed = ("text", "owner", "review_date")
    fields = [k for k in allowed if k in changes]
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT revision, status FROM assumptions"
                           " WHERE id = ? AND estimate_id = ?",
                           (aid, eid)).fetchone()
        if row is None:
            return None
        revision = row["revision"]
        sets = ", ".join("%s = ?" % k for k in fields)
        args = [changes[k] for k in fields] + [_now(), aid]
        conn.execute("UPDATE assumptions SET %s, updated_at = ? WHERE id = ?"
                     % sets, args)
        _add_event(conn, eid, actor, "assumption.updated", revision,
                   ["assumptions#%d.%s" % (aid, k) for k in fields],
                   "assumption %d updated (%s)" % (aid, ", ".join(fields)))
        conn.commit()
    return get_assumption(eid, aid)


def set_assumption_status(eid, aid, to_status, expected_from, actor):
    """Guarded register transition; returns (row, old_status) or (None, None)
    when the row is missing or no longer in expected_from."""
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT status, revision FROM assumptions WHERE id = ? AND"
            " estimate_id = ?", (aid, eid)).fetchone()
        if row is None or row["status"] != expected_from:
            return None, None if row is None else row["status"]
        conn.execute("UPDATE assumptions SET status = ?, updated_at = ?"
                     " WHERE id = ?", (to_status, _now(), aid))
        _add_event(conn, eid, actor, "assumption.status_changed",
                   row["revision"], ["assumptions#%d.status" % aid],
                   "assumption %d: %s -> %s" % (aid, expected_from, to_status))
        conn.commit()
        return get_assumption(eid, aid), expected_from


def delete_assumption(eid, aid, actor):
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT revision, text FROM assumptions WHERE id = ? AND"
            " estimate_id = ?", (aid, eid)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM assumptions WHERE id = ?", (aid,))
        _add_event(conn, eid, actor, "assumption.deleted", row["revision"],
                   ["-assumptions#%d" % aid],
                   "assumption %d deleted (%s)" % (aid, row["text"][:60]))
        conn.commit()
        return True


def assumption_summary(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM assumptions WHERE estimate_id = ?"
            " GROUP BY status", (eid,)).fetchall()
    counts = {s: 0 for s in ("open", "verified", "closed")}
    for r in rows:
        counts[r["status"]] = r["n"]
    counts["total"] = sum(counts.values())
    return counts


# ---------------------------------------------------------- quote register

def _row_to_quote(row):
    return {"id": row["id"], "estimate_id": row["estimate_id"],
            "vendor": row["vendor"], "scope": row["scope"],
            "amount": row["amount"], "currency": row["currency"],
            "binding_until": row["binding_until"],
            "inclusions": json.loads(row["inclusions"]),
            "exclusions": json.loads(row["exclusions"]),
            "linked_line_ids": json.loads(row["linked_line_ids"]),
            "status": row["status"], "notes": row["notes"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_quotes(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM vendor_quotes WHERE estimate_id = ? ORDER BY id",
            (eid,)).fetchall()
    return [_row_to_quote(r) for r in rows]


def get_quote(eid, qid):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM vendor_quotes WHERE estimate_id = ? AND id = ?",
            (eid, qid)).fetchone()
    return _row_to_quote(row) if row else None


def create_quote(eid, fields, actor, revision):
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO vendor_quotes (estimate_id, vendor, scope, amount,"
            " currency, binding_until, inclusions, exclusions, linked_line_ids,"
            " status, notes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, fields["vendor"], fields["scope"], fields["amount"],
             fields["currency"], fields["binding_until"],
             json.dumps(fields.get("inclusions", [])),
             json.dumps(fields.get("exclusions", [])),
             json.dumps(fields.get("linked_line_ids", [])),
             fields["status"], fields.get("notes"), now, now))
        qid = cur.lastrowid
        _add_event(conn, eid, actor, "quote.created", revision,
                   ["quotes#%d" % qid]
                   + ["quotes#%d.%s" % (qid, k) for k in fields],
                   "quote #%d from %s: %s %s (status %s, binding_until %s)"
                   % (qid, fields["vendor"], fields["currency"],
                      format(fields["amount"], ",.2f"), fields["status"],
                      fields["binding_until"]))
        conn.commit()
    return get_quote(eid, qid)


def update_quote(eid, qid, changes, actor, revision):
    allowed = ("vendor", "scope", "amount", "currency", "binding_until",
               "inclusions", "exclusions", "linked_line_ids", "notes")
    sets, args, paths = [], [], []
    for k in allowed:
        if k in changes:
            val = changes[k]
            if k in ("inclusions", "exclusions", "linked_line_ids"):
                val = json.dumps(val)
            sets.append("%s = ?" % k)
            args.append(val)
            paths.append("quotes#%d.%s" % (qid, k))
    if not sets:
        return get_quote(eid, qid)
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT id, vendor FROM vendor_quotes WHERE id = ?"
                           " AND estimate_id = ?", (qid, eid)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE vendor_quotes SET %s, updated_at = ? WHERE id = ?"
                     % ", ".join(sets), args + [_now(), qid])
        _add_event(conn, eid, actor, "quote.updated", revision, paths,
                   "quote #%d (%s) updated: %s" % (qid, row["vendor"],
                                                   ", ".join(
                                                       k for k in allowed
                                                       if k in changes)))
        conn.commit()
    return get_quote(eid, qid)


def set_quote_status(eid, qid, to_status, expected_from, actor, revision):
    """Guarded quote status transition (draft/received/bound/expired)."""
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT status, vendor FROM vendor_quotes WHERE id = ? AND"
            " estimate_id = ?", (qid, eid)).fetchone()
        if row is None or row["status"] != expected_from:
            return None
        conn.execute("UPDATE vendor_quotes SET status = ?, updated_at = ?"
                     " WHERE id = ?", (to_status, _now(), qid))
        _add_event(conn, eid, actor, "quote.status_changed", revision,
                   ["quotes#%d.status" % qid],
                   "quote #%d (%s): %s -> %s"
                   % (qid, row["vendor"], expected_from, to_status))
        conn.commit()
        return get_quote(eid, qid)


def delete_quote(eid, qid, actor, revision):
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT vendor FROM vendor_quotes WHERE id = ? AND estimate_id = ?",
            (qid, eid)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM vendor_quotes WHERE id = ?", (qid,))
        _add_event(conn, eid, actor, "quote.deleted", revision,
                   ["-quotes#%d" % qid],
                   "quote #%d (%s) deleted" % (qid, row["vendor"]))
        conn.commit()
        return True


def quote_summary(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM vendor_quotes WHERE"
            " estimate_id = ? GROUP BY status", (eid,)).fetchall()
    counts = {s: 0 for s in ("draft", "received", "bound", "expired")}
    for r in rows:
        counts[r["status"]] = r["n"]
    counts["total"] = sum(counts.values())
    return counts


# ------------------------------------------------------- what-if scenarios

def _row_to_scenario(row):
    return {"id": row["id"], "estimate_id": row["estimate_id"],
            "name": row["name"], "notes": row["notes"],
            "overlay": json.loads(row["overlay"]),
            "base_revision": row["base_revision"], "actor": row["actor"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def create_scenario(eid, name, notes, overlay, base_revision, actor):
    """Store a named what-if scenario (overlay only; results are recomputed
    against the current base at diff time) + audit event."""
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO estimate_scenarios (estimate_id, name, notes,"
            " overlay, base_revision, actor, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, name, notes, json.dumps(overlay), base_revision, actor,
             now, now))
        sid = cur.lastrowid
        _add_event(conn, eid, actor, "scenario.created", base_revision,
                   ["scenario#%s" % sid], "scenario %r created" % name)
        conn.commit()
        return get_scenario(eid, sid)


def list_scenarios(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM estimate_scenarios WHERE estimate_id = ?"
            " ORDER BY id", (eid,)).fetchall()
    return [_row_to_scenario(r) for r in rows]


def get_scenario(eid, sid):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM estimate_scenarios WHERE estimate_id = ? AND"
            " id = ?", (eid, sid)).fetchone()
    return _row_to_scenario(row) if row else None


def delete_scenario(eid, sid, actor):
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT name FROM estimate_scenarios WHERE"
                           " estimate_id = ? AND id = ?",
                           (eid, sid)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM estimate_scenarios WHERE estimate_id = ?"
                     " AND id = ?", (eid, sid))
        _add_event(conn, eid, actor, "scenario.deleted", None,
                   ["scenario#%s" % sid],
                   "scenario %r (id %s) deleted" % (row["name"], sid))
        conn.commit()
        return True


# ------------------------------------------------- actuals + refit proposals

def _row_to_actual(row):
    return {"id": row["id"], "estimate_id": row["estimate_id"],
            "catalog_id": row["catalog_id"], "location_id": row["location_id"],
            "qty": row["qty"], "hours": row["hours"], "cost": row["cost"],
            "date": row["date"], "note": row["note"],
            "created_at": row["created_at"]}


def add_event(eid, actor, action, revision=None, changed_paths=None,
              summary=""):
    """Public audit-log writer (one-shot, own connection)."""
    with closing(_connect()) as conn, conn:
        _add_event(conn, eid, actor, action, revision, changed_paths, summary)
        conn.commit()


def add_actual_rows(eid, rows, actor, revision=None):
    """Insert validated actual rows; one audit event per import."""
    now = _now()
    out = []
    with closing(_connect()) as conn, conn:
        for r in rows:
            cur = conn.execute(
                "INSERT INTO estimate_actuals (estimate_id, catalog_id,"
                " location_id, qty, hours, cost, date, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, r["catalog_id"], r.get("location_id"), r["qty"],
                 r["hours"], r.get("cost"), r["date"], r.get("note"), now))
            out.append(_row_to_actual(conn.execute(
                "SELECT * FROM estimate_actuals WHERE id = ?",
                (cur.lastrowid,)).fetchone()))
        n = len(out)
        cards = sorted({r["catalog_id"] for r in out})
        _add_event(conn, eid, actor, "actuals.imported", revision,
                   ["actuals#%d" % r["id"] for r in out],
                   "%d actual row(s) imported across %d card(s): %s; %s"
                   " total hours" % (n, len(cards), ", ".join(cards),
                                     round(sum(r["hours"] for r in out), 2)))
        conn.commit()
    return out


def list_actual_rows(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM estimate_actuals WHERE estimate_id = ?"
            " ORDER BY id", (eid,)).fetchall()
    return [_row_to_actual(r) for r in rows]


def save_refit_proposal(eid, proposal, actor):
    """Freeze a propose-refit result (review-only, never applied)."""
    now = _now()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO refit_proposals (estimate_id, proposal, actor,"
            " created_at) VALUES (?, ?, ?, ?)",
            (eid, json.dumps(proposal), actor, now))
        pid = cur.lastrowid
        _add_event(conn, eid, actor, "refit.proposed", None,
                   ["refit_proposal#%d" % pid],
                   "refit proposal #%d generated (%d card(s) proposed)"
                   % (pid, len(proposal.get("proposals") or [])))
        conn.commit()
    return get_refit_proposal(eid, pid)


def get_refit_proposal(eid, pid):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM refit_proposals WHERE estimate_id = ? AND id = ?",
            (eid, pid)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "estimate_id": row["estimate_id"],
            "proposal": json.loads(row["proposal"]),
            "actor": row["actor"], "created_at": row["created_at"]}


def list_refit_proposals(eid):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM refit_proposals WHERE estimate_id = ?"
            " ORDER BY id", (eid,)).fetchall()
    return [{"id": r["id"], "estimate_id": r["estimate_id"],
             "proposal": json.loads(r["proposal"]), "actor": r["actor"],
             "created_at": r["created_at"]} for r in rows]
