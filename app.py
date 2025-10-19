# app.py
# Milk Log v3 — Single-file Flask app with full feature set & mobile-first UI (no login)
# Features:
# - Milk records: cow, litres, date, session (AM/PM), note, tags, inline edit, soft delete
# - Pivot: last N dates × sessions, totals per cow
# - Recent entries: edit/delete
# - Bulk Add from pasted lines
# - CSV import/export, Excel export (raw + 7-day pivot)
# - Cows registry (tag,name,breed,parity,DOB,latest_calving,group_name)
# - Health events (withdrawal), Breeding events
# - KPI dashboard (milk/cow/day, coverage), Alerts (missing today, >20% drop vs avg7, active withdrawals)
# - SQLite with WAL + idempotent migrations, edited_at timestamps
# - PWA (manifest + service worker) served inline
# - Render-ready; /healthz

import os
import io
import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime, date, timedelta
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    send_file, flash, Response
)

try:
    from openpyxl import Workbook
except Exception:
    Workbook = None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-please-change")

# ---------- Persistence ----------
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "milk_records.db")

# ---------- DB init & helpers ----------
def init_db():
    """Create/upgrade schema safely; idempotent migrations for old DBs."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # --- milk_records (base create + backfill columns) ---
        conn.execute("""
        CREATE TABLE IF NOT EXISTS milk_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_number TEXT NOT NULL,
          litres REAL NOT NULL CHECK(litres >= 0),
          record_date TEXT NOT NULL,
          session TEXT DEFAULT 'AM' CHECK(session IN ('AM','PM')),
          note TEXT,
          tags TEXT,
          deleted INTEGER DEFAULT 0 CHECK(deleted IN (0,1)),
          created_at TEXT NOT NULL,
          edited_at TEXT
        )""")

        cols = [r[1] for r in conn.execute("PRAGMA table_info(milk_records)").fetchall()]
        if "session"   not in cols: conn.execute("ALTER TABLE milk_records ADD COLUMN session TEXT DEFAULT 'AM'")
        if "note"      not in cols: conn.execute("ALTER TABLE milk_records ADD COLUMN note TEXT")
        if "tags"      not in cols: conn.execute("ALTER TABLE milk_records ADD COLUMN tags TEXT")
        if "deleted"   not in cols: conn.execute("ALTER TABLE milk_records ADD COLUMN deleted INTEGER DEFAULT 0")
        if "edited_at" not in cols: conn.execute("ALTER TABLE milk_records ADD COLUMN edited_at TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_date ON milk_records(record_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_cow  ON milk_records(cow_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_sess ON milk_records(session)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_del  ON milk_records(deleted)")

        # --- cows (base create + backfill columns) ---
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cows (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tag TEXT UNIQUE NOT NULL,
          name TEXT,
          breed TEXT,
          parity INTEGER,
          dob TEXT,
          latest_calving TEXT,
          group_name TEXT,
          created_at TEXT,
          edited_at TEXT
        )""")

        cows_cols = [r[1] for r in conn.execute("PRAGMA table_info(cows)").fetchall()]
        # add any missing columns from older schemas
        if "name"           not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN name TEXT")
        if "breed"          not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN breed TEXT")
        if "parity"         not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN parity INTEGER")
        if "dob"            not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN dob TEXT")
        if "latest_calving" not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN latest_calving TEXT")
        if "group_name"     not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN group_name TEXT")
        if "created_at"     not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN created_at TEXT")
        if "edited_at"      not in cows_cols: conn.execute("ALTER TABLE cows ADD COLUMN edited_at TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_tag   ON cows(tag)")
        # this index must be created AFTER ensuring the column exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_group ON cows(group_name)")

        # --- health_events (base create + backfill) ---
        conn.execute("""
        CREATE TABLE IF NOT EXISTS health_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,
          details TEXT,
          withdrawal_until TEXT,
          protocol TEXT,
          created_at TEXT NOT NULL,
          edited_at TEXT
        )""")

        h_cols = [r[1] for r in conn.execute("PRAGMA table_info(health_events)").fetchall()]
        if "details"          not in h_cols: conn.execute("ALTER TABLE health_events ADD COLUMN details TEXT")
        if "withdrawal_until" not in h_cols: conn.execute("ALTER TABLE health_events ADD COLUMN withdrawal_until TEXT")
        if "protocol"         not in h_cols: conn.execute("ALTER TABLE health_events ADD COLUMN protocol TEXT")
        if "created_at"       not in h_cols: conn.execute("ALTER TABLE health_events ADD COLUMN created_at TEXT")
        if "edited_at"        not in h_cols: conn.execute("ALTER TABLE health_events ADD COLUMN edited_at TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_cowdate ON health_events(cow_tag, event_date)")

        # --- breeding_events (base create + backfill) ---
        conn.execute("""
        CREATE TABLE IF NOT EXISTS breeding_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,
          sire TEXT,
          details TEXT,
          created_at TEXT NOT NULL,
          edited_at TEXT
        )""")

        b_cols = [r[1] for r in conn.execute("PRAGMA table_info(breeding_events)").fetchall()]
        if "sire"       not in b_cols: conn.execute("ALTER TABLE breeding_events ADD COLUMN sire TEXT")
        if "details"    not in b_cols: conn.execute("ALTER TABLE breeding_events ADD COLUMN details TEXT")
        if "created_at" not in b_cols: conn.execute("ALTER TABLE breeding_events ADD COLUMN created_at TEXT")
        if "edited_at"  not in b_cols: conn.execute("ALTER TABLE breeding_events ADD COLUMN edited_at TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_breed_cowdate ON breeding_events(cow_tag, event_date)")


        # Cows registry
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cows (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tag TEXT UNIQUE NOT NULL,
          name TEXT,
          breed TEXT,
          parity INTEGER DEFAULT 1 CHECK(parity >= 0),
          dob TEXT,
          latest_calving TEXT,
          group_name TEXT,
          created_at TEXT NOT NULL,
          edited_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_tag ON cows(tag)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_group ON cows(group_name)")

        # Health events
        conn.execute("""
        CREATE TABLE IF NOT EXISTS health_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,            -- mastitis, treatment, injury, SCC
          details TEXT,
          withdrawal_until TEXT,               -- YYYY-MM-DD if milk hold
          protocol TEXT,                       -- optional template name
          created_at TEXT NOT NULL,
          edited_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_cowdate ON health_events(cow_tag, event_date)")

        # Breeding events
        conn.execute("""
        CREATE TABLE IF NOT EXISTS breeding_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,            -- heat, service, PD+, PD-, calving
          sire TEXT,
          details TEXT,
          created_at TEXT NOT NULL,
          edited_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_breed_cowdate ON breeding_events(cow_tag, event_date)")

def query(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()

def exec_write(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(sql, args)

def add_record(cow_number, litres, record_date_str, session_val, note, tags):
    _ = date.fromisoformat(record_date_str)  # validate
    if session_val not in ("AM", "PM"):
        session_val = "AM"
    exec_write("""
      INSERT INTO milk_records (cow_number, litres, record_date, session, note, tags, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (cow_number.strip(), float(litres), record_date_str, session_val, note.strip() or None,
          (tags.strip() or None), datetime.utcnow().isoformat()))

def update_record(rec_id, litres, session_val, note, tags):
    fields = []
    args = []
    if litres is not None:
        fields.append("litres=?")
        args.append(float(litres))
    if session_val:
        fields.append("session=?")
        args.append(session_val if session_val in ("AM", "PM") else "AM")
    fields.append("note=?"); args.append(note.strip() or None)
    fields.append("tags=?"); args.append((tags.strip() or None))
    fields.append("edited_at=?"); args.append(datetime.utcnow().isoformat())
    args.append(rec_id)
    exec_write(f"UPDATE milk_records SET {', '.join(fields)} WHERE id=?", tuple(args))

def soft_delete_record(rec_id: int):
    exec_write("UPDATE milk_records SET deleted=1, edited_at=? WHERE id=?",
               (datetime.utcnow().isoformat(), rec_id))

def restore_record(rec_id: int):
    exec_write("UPDATE milk_records SET deleted=0, edited_at=? WHERE id=?",
               (datetime.utcnow().isoformat(), rec_id))

# Ensure DB ready on import
init_db()

# ---------- Utilities ----------
def today_str():
    return date.today().isoformat()

def kpis_for_home():
    t = today_str()
    # total litres today (not deleted)
    row = query("SELECT COALESCE(SUM(litres),0) AS tot FROM milk_records WHERE deleted=0 AND record_date=?", (t,))
    tot = float(row[0]["tot"]) if row else 0.0
    # distinct cows recorded today
    cows = query("SELECT COUNT(DISTINCT cow_number) AS n FROM milk_records WHERE deleted=0 AND record_date=?", (t,))
    n_cows = int(cows[0]["n"]) if cows else 0
    # AM/PM coverage
    am = query("SELECT COUNT(DISTINCT cow_number) AS n FROM milk_records WHERE deleted=0 AND record_date=? AND session='AM'", (t,))
    pm = query("SELECT COUNT(DISTINCT cow_number) AS n FROM milk_records WHERE deleted=0 AND record_date=? AND session='PM'", (t,))
    am_n = int(am[0]["n"]) if am else 0
    pm_n = int(pm[0]["n"]) if pm else 0
    milk_per_cow = round(tot / n_cows, 2) if n_cows else 0.0
    return {
        "tot_litres": round(tot, 2),
        "cows_recorded": n_cows,
        "milk_per_cow": milk_per_cow,
        "am_coverage": am_n,
        "pm_coverage": pm_n
    }

def alerts_compute():
    t = today_str()
    # 1) Missing today: cows with history in last 14d but no entry today
    hist_cows = query("""
      SELECT DISTINCT cow_number FROM milk_records
      WHERE deleted=0 AND record_date BETWEEN date(?, '-14 day') AND date(?, '-1 day')
    """, (t, t))
    hist_set = {r["cow_number"] for r in hist_cows}
    today_cows = query("SELECT DISTINCT cow_number FROM milk_records WHERE deleted=0 AND record_date=?", (t,))
    today_set = {r["cow_number"] for r in today_cows}
    missing = sorted(list(hist_set - today_set), key=lambda x: (0,int(x)) if str(x).isdigit() else (1,x))

    # 2) Drop >20% vs 7-day avg (need at least 3 prior days)
    drops = []
    # per-cow today total
    today_rows = query("""
      SELECT cow_number, SUM(litres) AS litres
      FROM milk_records
      WHERE deleted=0 AND record_date=?
      GROUP BY cow_number
    """, (t,))
    for r in today_rows:
        cow = r["cow_number"]
        today_sum = float(r["litres"] or 0)
        # prior 7 days total avg per day
        prior = query("""
          SELECT record_date, SUM(litres) AS litres
          FROM milk_records
          WHERE deleted=0 AND cow_number=? AND record_date BETWEEN date(?, '-7 day') AND date(?, '-1 day')
          GROUP BY record_date
          ORDER BY record_date DESC
        """, (cow, t, t))
        if len(prior) >= 3:
            avg7 = sum(float(p["litres"] or 0) for p in prior) / len(prior)
            if avg7 > 0 and today_sum < 0.8 * avg7:
                drops.append({"cow": cow, "today": round(today_sum,2), "avg7": round(avg7,2), "pct": round(100.0 * today_sum/avg7, 1)})
    drops.sort(key=lambda d: d["pct"])

    # 3) Active withdrawals (health events with withdrawal_until >= today)
    holds = query("""
      SELECT cow_tag, event_date, withdrawal_until, event_type
      FROM health_events
      WHERE withdrawal_until IS NOT NULL AND date(withdrawal_until) >= date(?)
      ORDER BY withdrawal_until ASC
    """, (t,))

    return missing, drops, holds

# ---------- Routes ----------
@app.route("/")
def home():
    k = kpis_for_home()
    return render_template_string(TPL_HOME, base_css=BASE_CSS, k=k)

@app.route("/manifest.json")
def manifest():
    data = {
        "name": "Milk Log",
        "short_name": "MilkLog",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#22c55e",
        "icons": [
            {"src": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='192' height='192'><rect width='100%' height='100%' fill='%230f172a'/><text x='50%' y='55%' font-size='100' text-anchor='middle' fill='%2322c55e'>🐄</text></svg>", "sizes": "192x192", "type": "image/svg+xml"}
        ]
    }
    return Response(json.dumps(data), mimetype="application/json")

@app.route("/sw.js")
def service_worker():
    js = """
const CACHE = "milklog-v3";
const ASSETS = ["/","/new","/records","/recent","/cows","/health","/breeding","/import","/export.csv","/manifest.json"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS))));
self.addEventListener("fetch", e => {
  e.respondWith(
    caches.match(e.request).then(res => res || fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(()=>{});
      return r;
    }))
  );
});
"""
    return Response(js, mimetype="application/javascript")

@app.route("/new")
def new_record_screen():
    return render_template_string(TPL_NEW, base_css=BASE_CSS, today=today_str())

@app.route("/add", methods=["POST"])
def add():
    cow = request.form.get("cow_number", "").strip()
    litres = request.form.get("litres", "").strip()
    session_val = request.form.get("session", "AM").strip()
    note = request.form.get("note", "").strip()
    tags = request.form.get("tags", "").strip()
    record_date_str = (request.form.get("record_date") or today_str()).strip()

    if not cow:
        flash("Cow number is required", "error")
        return redirect(url_for("new_record_screen"))

    try:
        litres_val = float(litres)
        if litres_val < 0:
            raise ValueError
    except ValueError:
        flash("Litres must be a non-negative number", "error")
        return redirect(url_for("new_record_screen"))

    try:
        add_record(cow, litres_val, record_date_str, session_val, note, tags)
    except ValueError:
        flash("Bad date. Use YYYY-MM-DD.", "error")
        return redirect(url_for("new_record_screen"))
    except Exception as e:
        flash(f"Error saving: {e}", "error")
        return redirect(url_for("new_record_screen"))

    flash("Saved!", "ok")
    return redirect(url_for("new_record_screen"))

@app.route("/records")
def records_screen():
    # How many distinct dates to show
    try:
        last = int(request.args.get("last", "7"))
    except ValueError:
        last = 7
    last = max(1, min(last, 90))
    prev_last = max(1, last - 3)
    next_last = min(90, last + 3)

    # Pull last N dates (DESC), then reverse to show oldest->newest across columns
    dates_desc = query("""
        SELECT DISTINCT record_date
        FROM milk_records
        WHERE deleted=0
        ORDER BY record_date DESC
        LIMIT ?
    """, (last,))
    dates = list(reversed([r["record_date"] for r in dates_desc]))

    sessions = ["AM", "PM"]
    rows = []
    if dates:
        placeholders = ",".join("?" * len(dates))
        data = query(f"""
            SELECT cow_number, record_date, session, SUM(litres) AS litres
            FROM milk_records
            WHERE deleted=0 AND record_date IN ({placeholders})
            GROUP BY cow_number, record_date, session
        """, tuple(dates))

        # Build cow -> {(date, session): litres}
        by_cow = {}
        for r in data:
            cow = r["cow_number"]
            by_cow.setdefault(cow, {})
            by_cow[cow][(r["record_date"], r["session"])] = float(r["litres"] or 0)

        def cow_key(c):
            try:
                return (0, int(c))
            except:
                return (1, c)

        for cow in sorted(by_cow.keys(), key=cow_key):
            row_vals = []
            total = 0.0
            for d in dates:
                for s in sessions:
                    v = by_cow[cow].get((d, s), 0.0)
                    row_vals.append(round(v, 2))
                    total += v
            rows.append({"cow": cow, "cells": row_vals, "total": round(total, 2)})

    return render_template_string(
        TPL_RECORDS,
        base_css=BASE_CSS,
        dates=dates, sessions=sessions, rows=rows,
        last=last, prev_last=prev_last, next_last=next_last
    )

@app.route("/recent")
def recent_screen():
    try:
        limit = int(request.args.get("limit", "150"))
    except ValueError:
        limit = 150
    limit = max(1, min(limit, 500))
    rows = query("""
        SELECT id, cow_number, litres, record_date, session, note, tags, created_at, edited_at, deleted
        FROM milk_records
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    msg = request.args.get("msg")
    return render_template_string(TPL_RECENT, base_css=BASE_CSS, rows=rows, limit=limit, msg=msg)

@app.route("/update/<int:rec_id>", methods=["POST"])
def update(rec_id):
    litres = request.form.get("litres")
    session_val = request.form.get("session")
    note = request.form.get("note", "")
    tags = request.form.get("tags", "")
    try:
        litres_val = float(litres) if litres is not None else None
        update_record(rec_id, litres_val, session_val, note, tags)
        return redirect(url_for("recent_screen", msg="Updated."))
    except Exception as e:
        return redirect(url_for("recent_screen", msg=f"Update failed: {e}"))

@app.route("/delete/<int:rec_id>", methods=["POST"])
def delete(rec_id):
    soft_delete_record(rec_id)
    return redirect(url_for("recent_screen", msg="Deleted 1 entry (soft delete)."))

@app.route("/restore/<int:rec_id>", methods=["POST"])
def restore(rec_id):
    restore_record(rec_id)
    return redirect(url_for("recent_screen", msg="Restored 1 entry."))

# ----- Bulk Add -----
@app.route("/bulk", methods=["GET", "POST"])
def bulk_add():
    info = None
    sample = "2146 12.3\n2147 10.8 PM\n2148 11.2 2025-10-19 AM fresh"
    if request.method == "POST":
        text = request.form.get("lines", "")
        default_date = request.form.get("record_date") or today_str()
        default_session = request.form.get("session") or "AM"
        count = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Parse: cow litres [date] [session] [tags...]
            parts = line.split()
            try:
                cow = parts[0]
                litres = float(parts[1])
                d = default_date
                s = default_session
                tags = ""
                # Look for optional parts by value
                for p in parts[2:]:
                    if p in ("AM", "PM"):
                        s = p
                    elif len(p) == 10 and p[4] == "-" and p[7] == "-":
                        d = p
                    else:
                        tags = (tags + "," + p) if tags else p
                add_record(cow, litres, d, s, note="", tags=tags)
                count += 1
            except Exception:
                continue
        info = f"Imported {count} lines."
    return render_template_string(TPL_BULK, base_css=BASE_CSS, today=today_str(), info=info, sample=sample)

# ----- CSV Import/Export -----
@app.route("/import", methods=["GET", "POST"])
def import_csv():
    info = None
    if request.method == "POST" and "file" in request.files:
        f = request.files["file"]
        try:
            text = f.stream.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(text))
            count = 0
            for row in reader:
                try:
                    add_record(
                        row["cow_number"], float(row["litres"]),
                        row["record_date"],
                        row.get("session", "AM"),
                        row.get("note", "") or "",
                        row.get("tags", "") or "",
                    )
                    count += 1
                except Exception:
                    pass
            info = f"Imported {count} records."
        except Exception as e:
            info = f"Import failed: {e}"
    return render_template_string(TPL_IMPORT, base_css=BASE_CSS, info=info)

@app.route("/export.csv")
def export_csv():
    rows = query("""
      SELECT id, cow_number, litres, record_date, session, note, tags, created_at, edited_at, deleted
      FROM milk_records
      ORDER BY record_date DESC, id DESC
    """)
    out = io.StringIO()
    w = csv.writer(out)
    headers = ["id","cow_number","litres","record_date","session","note","tags","created_at","edited_at","deleted"]
    w.writerow(headers)
    for r in rows:
        w.writerow([r[h] for h in headers])
    out.seek(0)
    return send_file(
        io.BytesIO(out.read().encode("utf-8")),
        as_attachment=True,
        download_name="milk_records.csv",
        mimetype="text/csv"
    )

@app.route("/export.xlsx")
def export_excel():
    if Workbook is None:
        return "Excel export not available (openpyxl not installed).", 503

    data = query("""
      SELECT id, cow_number, litres, record_date, session, note, tags, created_at
      FROM milk_records
      WHERE deleted=0
      ORDER BY record_date ASC, cow_number ASC, id ASC
    """)

    wb = Workbook()
    ws = wb.active
    ws.title = "Raw Records"
    ws.append(["ID", "Cow #", "Litres", "Date", "Session", "Note", "Tags", "Saved (UTC)"])
    for r in data:
        ws.append([r["id"], r["cow_number"], r["litres"], r["record_date"], r["session"], r["note"] or "", r["tags"] or "", r["created_at"]])
    for col, w in zip("ABCDEFGH", [8,10,10,12,10,25,25,25]):
        ws.column_dimensions[col].width = w

    # Pivot (last 7 dates × sessions)
    dates_desc = query("SELECT DISTINCT record_date FROM milk_records WHERE deleted=0 ORDER BY record_date DESC LIMIT 7")
    dates = list(reversed([r["record_date"] for r in dates_desc]))
    sessions = ["AM", "PM"]
    ws2 = wb.create_sheet("Pivot (last 7 dates)")
    ws2.append(["Cow #", *[f"{d} {s}" for d in dates for s in sessions], "Total"])

    if dates:
        placeholders = ",".join("?" * len(dates))
        data2 = query(f"""
          SELECT cow_number, record_date, session, SUM(litres) AS litres
          FROM milk_records
          WHERE deleted=0 AND record_date IN ({placeholders})
          GROUP BY cow_number, record_date, session
        """, tuple(dates))
        by_cow = {}
        for r in data2:
            by_cow.setdefault(r["cow_number"], {})
            by_cow[r["cow_number"]][(r["record_date"], r["session"])] = float(r["litres"] or 0)
        for cow, _ in sorted(by_cow.items(), key=lambda kv: (0, int(kv[0])) if str(kv[0]).isdigit() else (1, kv[0])):
            vals = []
            total = 0.0
            for d in dates:
                for s in sessions:
                    v = by_cow[cow].get((d, s), 0.0)
                    vals.append(round(v, 2))
                    total += v
            ws2.append([cow, *vals, round(total, 2)])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(
        bio,
        as_attachment=True,
        download_name="milk-records.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# ----- Cows -----
@app.route("/cows", methods=["GET", "POST"])
def cows_screen():
    info = None
    if request.method == "POST":
        tag = request.form.get("tag", "").strip()
        if not tag:
            info = "Tag is required."
        else:
            exec_write("""
              INSERT OR IGNORE INTO cows (tag, name, breed, parity, dob, latest_calving, group_name, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tag,
                request.form.get("name") or None,
                request.form.get("breed") or None,
                int(request.form.get("parity") or 1),
                request.form.get("dob") or None,
                request.form.get("latest_calving") or None,
                request.form.get("group_name") or None,
                datetime.utcnow().isoformat(),
            ))
            info = "Cow saved."
    rows = query("SELECT * FROM cows ORDER BY tag COLLATE NOCASE")
    return render_template_string(TPL_COWS, base_css=BASE_CSS, rows=rows, info=info)

# ----- Health -----
@app.route("/health", methods=["GET", "POST"])
def health_screen():
    info = None
    if request.method == "POST":
        cow_tag = request.form.get("cow_tag", "").strip()
        event_date = request.form.get("event_date") or ""
        event_type = request.form.get("event_type") or ""
        try:
            _ = date.fromisoformat(event_date)
            if not cow_tag or not event_type:
                raise ValueError("Cow tag and event type required.")
            exec_write("""
              INSERT INTO health_events (cow_tag, event_date, event_type, details, withdrawal_until, protocol, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                cow_tag,
                event_date,
                event_type,
                request.form.get("details") or None,
                request.form.get("withdrawal_until") or None,
                request.form.get("protocol") or None,
                datetime.utcnow().isoformat(),
            ))
            info = "Health event saved."
        except Exception as e:
            info = f"Error: {e}"
    rows = query("SELECT * FROM health_events ORDER BY event_date DESC, id DESC LIMIT 200")
    return render_template_string(TPL_HEALTH, base_css=BASE_CSS, rows=rows, info=info)

# ----- Breeding -----
@app.route("/breeding", methods=["GET", "POST"])
def breeding_screen():
    info = None
    if request.method == "POST":
        cow_tag = request.form.get("cow_tag", "").strip()
        event_date = request.form.get("event_date") or ""
        event_type = request.form.get("event_type") or ""
        try:
            _ = date.fromisoformat(event_date)
            if not cow_tag or not event_type:
                raise ValueError("Cow tag and event type required.")
            exec_write("""
              INSERT INTO breeding_events (cow_tag, event_date, event_type, sire, details, created_at)
              VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cow_tag,
                event_date,
                event_type,
                request.form.get("sire") or None,
                request.form.get("details") or None,
                datetime.utcnow().isoformat(),
            ))
            info = "Breeding event saved."
        except Exception as e:
            info = f"Error: {e}"
    rows = query("SELECT * FROM breeding_events ORDER BY event_date DESC, id DESC LIMIT 200")
    return render_template_string(TPL_BREEDING, base_css=BASE_CSS, rows=rows, info=info)

# ----- Alerts -----
@app.route("/alerts")
def alerts_screen():
    missing, drops, holds = alerts_compute()
    return render_template_string(TPL_ALERTS, base_css=BASE_CSS, missing=missing, drops=drops, holds=holds, today=today_str())

@app.route("/healthz")
def healthz():
    return "ok", 200

# ---------- Styles & Templates ----------
BASE_CSS = """
:root{
  --bg:#0b1220; --panel:#0f172a; --border:#223044; --text:#e5e7eb;
  --muted:#9aa5b1; --accent:#22c55e; --accent-fore:#07220e;
  --radius:18px; --shadow:0 14px 40px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 10% -10%, #0a1222 0, #0b1629 30%, #0f172a 70%), #0f172a;
     color:var(--text);font-family:Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial}
.wrap{max-width:820px;margin:0 auto;padding:22px}
.top{display:flex;align-items:center;justify-content:space-between;margin:6px 2px 18px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:40px;height:40px}
.title{font-weight:900;letter-spacing:.2px;font-size:22px}
.kicker{color:var(--muted);font-size:12px;margin-top:-6px}
.card{background:linear-gradient(180deg,#0c1324,#111a2f);border:1px solid var(--border);
      border-radius:var(--radius);padding:18px 18px 16px;box-shadow:var(--shadow);margin-bottom:16px}
.menu{display:grid;gap:12px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:10px;background:var(--accent);color:var(--accent-fore);
     font-weight:800;padding:12px 16px;border:none;border-radius:14px;cursor:pointer;text-decoration:none;text-align:center}
.btn.secondary{background:#0b1220;color:var(--text);border:1px solid var(--border)}
.btn.warn{background:#ef4444;color:#fff}
.field{display:grid;gap:6px}
label{font-size:13px;color:var(--muted)}
input,select,textarea{background:#0b1220;border:1px solid var(--border);color:var(--text);padding:12px;border-radius:12px;font-size:16px;width:100%}
.grid2{display:grid;gap:12px;grid-template-columns:1fr}
@media(min-width:620px){.grid2{grid-template-columns:1fr 1fr}}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px;overflow-x:auto;display:block}
thead, tbody { display: table; width: 100%; }
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:600;background:#0b1220;position:sticky;top:0}
tr:hover td{background:rgba(120,190,255,.06)}
.hint{color:var(--muted);font-size:12px;text-align:center;margin-top:12px}
.badge{display:inline-block;background:#0b1220;border:1px solid var(--border);color:var(--text);
       border-radius:12px;padding:4px 10px;font-size:12px}
.subtle{color:var(--muted);font-size:12px;text-align:center;margin-top:14px}
.header-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.hero{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.stat{background:#0b1220;border:1px dashed #1b2a3e;border-radius:12px;padding:12px}
.stat .big{font-weight:900;font-size:22px}
.flash{margin:8px 0;padding:10px;border-radius:10px}
.flash.ok{background:#0e3821;border:1px solid #1c7f4b}
.flash.error{background:#3b0e0e;border:1px solid #7f1d1d}
small.muted{color:var(--muted)}
"""

TPL_HOME = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<script>navigator.serviceWorker?.register('/sw.js');</script>
<title>Milk Log</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.7"/>
          <circle cx="9" cy="11" r="1.6" fill="#22c55e"/><circle cx="15" cy="11" r="1.6" fill="#22c55e"/>
        </svg>
        <div>
          <div class="title">Milk Log</div>
          <div class="kicker">Fast, clean milk recording</div>
        </div>
      </div>
      <span class="badge">v3</span>
    </div>

    <div class="hero">
      <div class="stat"><div class="big">{{ k.tot_litres }}</div><div>Total litres today</div></div>
      <div class="stat"><div class="big">{{ k.milk_per_cow }}</div><div>Milk per cow (L)</div></div>
      <div class="stat"><div class="big">{{ k.cows_recorded }} <small class="muted">({{k.am_coverage}} AM / {{k.pm_coverage}} PM)</small></div><div>Cows recorded today</div></div>
    </div>

    <div class="card">
      <div style="font-size:20px;font-weight:800;margin-bottom:10px">Main menu</div>
      <div class="menu">
        <a class="btn" href="{{ url_for('records_screen') }}">Cow Records</a>
        <a class="btn secondary" href="{{ url_for('new_record_screen') }}">New Recording</a>
        <a class="btn secondary" href="{{ url_for('recent_screen') }}">Recent Entries</a>
        <a class="btn secondary" href="{{ url_for('bulk_add') }}">Bulk Add</a>
        <a class="btn secondary" href="{{ url_for('cows_screen') }}">Cows</a>
        <a class="btn secondary" href="{{ url_for('health_screen') }}">Health</a>
        <a class="btn secondary" href="{{ url_for('breeding_screen') }}">Breeding</a>
        <a class="btn secondary" href="{{ url_for('alerts_screen') }}">Alerts</a>
        <a class="btn secondary" href="{{ url_for('import_csv') }}">Import CSV</a>
        <a class="btn secondary" href="{{ url_for('export_csv') }}">Export CSV</a>
        <a class="btn secondary" href="{{ url_for('export_excel') }}">Export Excel</a>
      </div>
    </div>

    <div class="subtle">Install to Home Screen for an app-like experience • Data stored in SQLite on server • Export anytime.</div>
  </div>
</body></html>
"""

TPL_NEW = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>New Recording</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">New Recording</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% with msgs = get_flashed_messages(with_categories=true) %}
      {% if msgs %}
        {% for cat, m in msgs %}<div class="flash {{cat}}">{{ m }}</div>{% endfor %}
      {% endif %}
    {% endwith %}

    <div class="card">
      <form method="POST" action="{{ url_for('add') }}" autocomplete="off">
        <div class="grid2">
          <div class="field">
            <label for="cow_number">Cow Number</label>
            <input id="cow_number" name="cow_number" inputmode="numeric" pattern="[0-9]*" placeholder="e.g., 2146" required>
          </div>
          <div class="field">
            <label for="litres">Litres given</label>
            <input id="litres" name="litres" type="number" step="0.01" min="0" placeholder="e.g., 11.8" required>
          </div>
        </div>
        <div class="grid2">
          <div class="field">
            <label for="record_date">Record date</label>
            <input id="record_date" name="record_date" type="date" value="{{ today }}">
          </div>
          <div class="field">
            <label for="session">Session</label>
            <select id="session" name="session"><option>AM</option><option>PM</option></select>
          </div>
        </div>
        <div class="grid2">
          <div class="field">
            <label for="note">Note (optional)</label>
            <input id="note" name="note" placeholder="e.g., fresh / mastitis watch">
          </div>
          <div class="field">
            <label for="tags">Tags (comma-separated)</label>
            <input id="tags" name="tags" placeholder="fresh,heifer">
          </div>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">
          <button class="btn" type="submit">Finish Recording</button>
          <a class="btn secondary" href="{{ url_for('records_screen') }}">View Records</a>
        </div>
      </form>
    </div>
  </div>
</body></html>
"""

TPL_RECORDS = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cow Records</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Cow Records</div>
      </div>
      <div class="header-actions">
        <a class="btn secondary" href="{{ url_for('records_screen', last=prev_last) }}">-3d</a>
        <a class="btn secondary" href="{{ url_for('records_screen', last=next_last) }}">+3d</a>
        <a class="btn" href="{{ url_for('export_excel') }}">Export</a>
        <a class="btn secondary" href="{{ url_for('new_record_screen') }}">Add</a>
      </div>
    </div>

    <div class="card">
      <div style="color:var(--muted);font-size:13px;margin-bottom:8px">
        Showing last {{ last }} date{{ '' if last==1 else 's' }}. Columns are date × session.
      </div>
      <table aria-label="Records by cow">
        <thead>
          <tr>
            <th>Cow #</th>
            {% for d in dates %}{% for s in sessions %}<th>{{ d }} {{ s }}</th>{% endfor %}{% endfor %}
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {% if rows %}
            {% for r in rows %}
              <tr>
                <td>{{ r.cow }}</td>
                {% for v in r.cells %}<td>{{ '%.2f'|format(v) }}</td>{% endfor %}
                <td>{{ '%.2f'|format(r.total) }}</td>
              </tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="{{ 2 + (dates|length) * (sessions|length) }}" style="color:var(--muted)">No records yet.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

TPL_RECENT = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recent Entries</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Recent Entries</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% if msg %}<div class="flash ok">✔ {{ msg }}</div>{% endif %}

    <div class="card">
      <div style="color:var(--muted);font-size:13px;margin-bottom:8px">
        Showing latest {{ rows|length }} (limit {{ limit }}). Click Update to save inline edits.
      </div>
      <table aria-label="Recent raw records">
        <thead>
          <tr>
            <th>ID</th><th>Cow #</th><th>Litres</th><th>Date</th><th>Session</th><th>Note</th><th>Tags</th><th>Saved</th><th>Edited</th><th>Deleted</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% if rows %}
            {% for r in rows %}
              <tr>
                <td>{{ r['id'] }}</td>
                <td>{{ r['cow_number'] }}</td>
                <form method="POST" action="{{ url_for('update', rec_id=r['id']) }}">
                  <td><input name="litres" type="number" step="0.01" min="0" value="{{ '%.2f'|format(r['litres']) }}"></td>
                  <td>{{ r['record_date'] }}</td>
                  <td>
                    <select name="session">
                      <option value="AM" {% if r['session']=='AM' %}selected{% endif %}>AM</option>
                      <option value="PM" {% if r['session']=='PM' %}selected{% endif %}>PM</option>
                    </select>
                  </td>
                  <td><input name="note" value="{{ r['note'] or '' }}"></td>
                  <td><input name="tags" value="{{ r['tags'] or '' }}"></td>
                  <td><small class="muted">{{ r['created_at'] }}</small></td>
                  <td><small class="muted">{{ r['edited_at'] or '' }}</small></td>
                  <td>{{ r['deleted'] }}</td>
                  <td style="display:flex;gap:6px">
                    <button class="btn secondary" type="submit">Update</button>
                </form>
                <form method="POST" action="{{ url_for('delete', rec_id=r['id']) }}" onsubmit="return confirm('Delete this entry?')">
                    <button class="btn warn" type="submit">Delete</button>
                </form>
                {% if r['deleted'] == 1 %}
                <form method="POST" action="{{ url_for('restore', rec_id=r['id']) }}">
                    <button class="btn" type="submit">Restore</button>
                </form>
                {% endif %}
                  </td>
              </tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="11" style="color:var(--muted)">No entries yet.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

TPL_BULK = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bulk Add</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Bulk Add</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% if info %}<div class="flash ok">{{ info }}</div>{% endif %}

    <div class="card">
      <p>Paste one entry per line: <code>cow litres [date] [AM|PM] [tags...]</code>.</p>
      <form method="POST" class="grid2">
        <div class="field"><label>Default date</label><input type="date" name="record_date" value="{{ today }}"></div>
        <div class="field"><label>Default session</label>
          <select name="session"><option>AM</option><option>PM</option></select>
        </div>
        <div class="field" style="grid-column:1/-1">
          <label>Lines</label>
          <textarea name="lines" rows="10" placeholder="{{ sample }}">{{ sample }}</textarea>
        </div>
        <div><button class="btn" type="submit">Import</button></div>
      </form>
    </div>
  </div>
</body></html>
"""

TPL_IMPORT = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import CSV</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Import CSV</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% if info %}<div class="flash ok">{{ info }}</div>{% endif %}

    <div class="card">
      <p>Headers: <code>cow_number, litres, record_date, session (AM/PM), note, tags</code>.</p>
      <form method="POST" enctype="multipart/form-data">
        <input type="file" name="file" accept=".csv" required>
        <div style="margin-top:10px"><button class="btn" type="submit">Import</button></div>
      </form>
    </div>
  </div>
</body></html>
"""

TPL_COWS = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cows</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Cows</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% if info %}<div class="flash ok">{{ info }}</div>{% endif %}

    <div class="card">
      <h3 style="margin:0 0 10px 0">Add / Update Cow</h3>
      <form method="POST" autocomplete="off" class="grid2">
        <div class="field"><label>Tag *</label><input name="tag" placeholder="e.g., 2146" required></div>
        <div class="field"><label>Name</label><input name="name" placeholder="optional"></div>
        <div class="field"><label>Breed</label><input name="breed" placeholder="e.g., Friesian"></div>
        <div class="field"><label>Parity</label><input name="parity" type="number" min="0" value="1"></div>
        <div class="field"><label>DOB</label><input name="dob" type="date"></div>
        <div class="field"><label>Latest Calving</label><input name="latest_calving" type="date"></div>
        <div class="field"><label>Group</label><input name="group_name" placeholder="fresh, high"></div>
        <div><button class="btn" type="submit">Save Cow</button></div>
      </form>
    </div>

    <div class="card">
      <table aria-label="Cows">
        <thead><tr><th>Tag</th><th>Name</th><th>Breed</th><th>Parity</th><th>DOB</th><th>Latest Calving</th><th>Group</th></tr></thead>
        <tbody>
          {% for r in rows %}
            <tr>
              <td>{{ r['tag'] }}</td>
              <td>{{ r['name'] or '' }}</td>
              <td>{{ r['breed'] or '' }}</td>
              <td>{{ r['parity'] if r['parity'] is not none else '' }}</td>
              <td>{{ r['dob'] or '' }}</td>
              <td>{{ r['latest_calving'] or '' }}</td>
              <td>{{ r['group_name'] or '' }}</td>
            </tr>
          {% else %}
            <tr><td colspan="7" class="muted">No cows yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

TPL_HEALTH = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Health Events</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Health Events</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% if info %}<div class="flash ok">{{ info }}</div>{% endif %}

    <div class="card">
      <h3 style="margin:0 0 10px 0">Add Health Event</h3>
      <form method="POST" class="grid2">
        <div class="field"><label>Cow Tag *</label><input name="cow_tag" required></div>
        <div class="field"><label>Date *</label><input name="event_date" type="date" required></div>
        <div class="field"><label>Type *</label>
          <select name="event_type">
            <option>mastitis</option><option>treatment</option><option>injury</option><option>SCC</option>
          </select>
        </div>
        <div class="field"><label>Withdrawal Until</label><input name="withdrawal_until" type="date"></div>
        <div class="field"><label>Protocol</label><input name="protocol" placeholder="e.g., Mastitis ABX A"></div>
        <div class="field" style="grid-column:1/-1"><label>Details</label><textarea name="details" rows="3" placeholder="notes"></textarea></div>
        <div><button class="btn" type="submit">Save Event</button></div>
      </form>
    </div>

    <div class="card">
      <table aria-label="Health events">
        <thead><tr><th>Date</th><th>Cow</th><th>Type</th><th>Withdrawal</th><th>Protocol</th><th>Details</th></tr></thead>
        <tbody>
          {% for r in rows %}
            <tr>
              <td>{{ r['event_date'] }}</td>
              <td>{{ r['cow_tag'] }}</td>
              <td>{{ r['event_type'] }}</td>
              <td>{{ r['withdrawal_until'] or '' }}</td>
              <td>{{ r['protocol'] or '' }}</td>
              <td>{{ r['details'] or '' }}</td>
            </tr>
          {% else %}
            <tr><td colspan="6" class="muted">No events yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

TPL_BREEDING = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Breeding Events</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Breeding Events</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    {% if info %}<div class="flash ok">{{ info }}</div>{% endif %}

    <div class="card">
      <h3 style="margin:0 0 10px 0">Add Breeding Event</h3>
      <form method="POST" class="grid2">
        <div class="field"><label>Cow Tag *</label><input name="cow_tag" required></div>
        <div class="field"><label>Date *</label><input name="event_date" type="date" required></div>
        <div class="field"><label>Type *</label>
          <select name="event_type">
            <option>heat</option><option>service</option><option>PD+</option><option>PD-</option><option>calving</option>
          </select>
        </div>
        <div class="field"><label>Sire</label><input name="sire" placeholder="bull/AI ID"></div>
        <div class="field" style="grid-column:1/-1"><label>Details</label><textarea name="details" rows="3" placeholder="notes"></textarea></div>
        <div><button class="btn" type="submit">Save Event</button></div>
      </form>
    </div>

    <div class="card">
      <table aria-label="Breeding events">
        <thead><tr><th>Date</th><th>Cow</th><th>Type</th><th>Sire</th><th>Details</th></tr></thead>
        <tbody>
          {% for r in rows %}
            <tr>
              <td>{{ r['event_date'] }}</td>
              <td>{{ r['cow_tag'] }}</td>
              <td>{{ r['event_type'] }}</td>
              <td>{{ r['sire'] or '' }}</td>
              <td>{{ r['details'] or '' }}</td>
            </tr>
          {% else %}
            <tr><td colspan="5" class="muted">No events yet.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

TPL_ALERTS = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alerts</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/>
        </svg>
        <div class="title">Alerts</div>
      </div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Missing today ({{ today }})</h3>
      <div>
        {% if missing %}
          {% for c in missing %}<span class="badge" style="margin:4px;display:inline-block">{{ c }}</span>{% endfor %}
        {% else %}
          <span class="muted">No missing cows 🎉</span>
        {% endif %}
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Drops vs 7-day avg (today &lt; 80%)</h3>
      <table aria-label="Drops">
        <thead><tr><th>Cow</th><th>Today</th><th>Avg7</th><th>%</th></tr></thead>
        <tbody>
          {% if drops %}
            {% for r in drops %}
              <tr><td>{{ r.cow }}</td><td>{{ '%.2f'|format(r.today) }}</td><td>{{ '%.2f'|format(r.avg7) }}</td><td>{{ r.pct }}%</td></tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="4" class="muted">No significant drops.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0">Active withdrawal holds</h3>
      <table aria-label="Holds">
        <thead><tr><th>Cow</th><th>Type</th><th>Event</th><th>Withdrawal until</th></tr></thead>
        <tbody>
          {% if holds %}
            {% for h in holds %}
              <tr><td>{{ h['cow_tag'] }}</td><td>{{ h['event_type'] }}</td><td>{{ h['event_date'] }}</td><td>{{ h['withdrawal_until'] }}</td></tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="4" class="muted">No holds.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>

  </div>
</body></html>
"""

# ---------- Local run ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
