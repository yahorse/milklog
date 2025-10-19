# app.py
# Milk Log v2 – Single-file Flask app with expanded features & mobile-first UI (no login)
# Features:
# - Add milk records (cow, litres, date, session AM/PM, note)
# - Pivot: last N dates × sessions, totals per cow
# - Recent entries with Delete
# - CSV import/export, Excel export (raw + 7-day pivot)
# - Cows registry, Health events, Breeding events
# - SQLite with WAL + idempotent migrations
# - Render-ready; health check

import os
import io
import csv
import sqlite3
from contextlib import closing
from datetime import datetime, date
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    send_file, flash
)

try:
    from openpyxl import Workbook
except Exception:
    Workbook = None  # Excel export will 503 if lib is missing

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
    """Create/upgrade schema safely; idempotent migrations."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Base table (with session + note)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS milk_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_number TEXT NOT NULL,
          litres REAL NOT NULL CHECK(litres >= 0),
          record_date TEXT NOT NULL,       -- YYYY-MM-DD
          session TEXT DEFAULT 'AM' CHECK(session IN ('AM','PM')),
          note TEXT,
          created_at TEXT NOT NULL
        )""")
        # Add columns if missing (legacy support)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(milk_records)").fetchall()]
        if "session" not in cols:
            conn.execute("ALTER TABLE milk_records ADD COLUMN session TEXT DEFAULT 'AM'")
        if "note" not in cols:
            conn.execute("ALTER TABLE milk_records ADD COLUMN note TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_date ON milk_records(record_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_cow  ON milk_records(cow_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_sess ON milk_records(session)")

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
          created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_tag ON cows(tag)")

        # Health events
        conn.execute("""
        CREATE TABLE IF NOT EXISTS health_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,            -- e.g., mastitis, treatment, injury, SCC
          details TEXT,
          withdrawal_until TEXT,               -- YYYY-MM-DD if milk hold
          created_at TEXT NOT NULL
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
          created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_breed_cowdate ON breeding_events(cow_tag, event_date)")

def query(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()

def exec_write(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(sql, args)

def add_record(cow_number, litres, record_date_str, session_val, note):
    # validate date
    _ = date.fromisoformat(record_date_str)
    # validate session
    if session_val not in ("AM", "PM"):
        session_val = "AM"
    exec_write("""
      INSERT INTO milk_records (cow_number, litres, record_date, session, note, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
    """, (cow_number.strip(), float(litres), record_date_str, session_val, note.strip(), datetime.utcnow().isoformat()))

def delete_record_row(rec_id: int):
    exec_write("DELETE FROM milk_records WHERE id = ?", (rec_id,))

# Ensure DB ready on import (Render/Gunicorn)
init_db()

# ---------- Routes ----------
@app.route("/")
def home():
    return render_template_string(TPL_HOME, base_css=BASE_CSS)

@app.route("/new")
def new_record_screen():
    return render_template_string(TPL_NEW, base_css=BASE_CSS, today=date.today().isoformat())

@app.route("/add", methods=["POST"])
def add():
    cow = request.form.get("cow_number", "").strip()
    litres = request.form.get("litres", "").strip()
    session_val = request.form.get("session", "AM").strip()
    note = request.form.get("note", "").strip()
    record_date_str = (request.form.get("record_date") or date.today().isoformat()).strip()

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
        add_record(cow, litres_val, record_date_str, session_val, note)
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
            WHERE record_date IN ({placeholders})
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
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    rows = query("""
        SELECT id, cow_number, litres, record_date, session, note, created_at
        FROM milk_records
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    msg = request.args.get("msg")
    return render_template_string(TPL_RECENT, base_css=BASE_CSS, rows=rows, limit=limit, msg=msg)

@app.route("/delete/<int:rec_id>", methods=["POST"])
def delete(rec_id):
    delete_record_row(rec_id)
    return redirect(url_for("recent_screen", msg="Deleted 1 entry."))

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
                        row["record_date"], row.get("session", "AM"),
                        row.get("note", "")
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
      SELECT id, cow_number, litres, record_date, session, note, created_at
      FROM milk_records
      ORDER BY record_date DESC, id DESC
    """)
    out = io.StringIO()
    w = csv.writer(out)
    headers = ["id", "cow_number", "litres", "record_date", "session", "note", "created_at"]
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
      SELECT id, cow_number, litres, record_date, session, note, created_at
      FROM milk_records
      ORDER BY record_date ASC, cow_number ASC, id ASC
    """)

    wb = Workbook()
    ws = wb.active
    ws.title = "Raw Records"
    ws.append(["ID", "Cow #", "Litres", "Date", "Session", "Note", "Saved (UTC)"])
    for r in data:
        ws.append([r["id"], r["cow_number"], r["litres"], r["record_date"], r["session"], r["note"] or "", r["created_at"]])
    for col, w in zip("ABCDEFG", [8,10,10,12,10,25,25]):
        ws.column_dimensions[col].width = w

    # Pivot (last 7 dates × sessions)
    dates_desc = query("SELECT DISTINCT record_date FROM milk_records ORDER BY record_date DESC LIMIT 7")
    dates = list(reversed([r["record_date"] for r in dates_desc]))
    sessions = ["AM", "PM"]
    ws2 = wb.create_sheet("Pivot (last 7 dates)")
    ws2.append(["Cow #", *[f"{d} {s}" for d in dates for s in sessions], "Total"])

    if dates:
        placeholders = ",".join("?" * len(dates))
        data2 = query(f"""
          SELECT cow_number, record_date, session, SUM(litres) AS litres
          FROM milk_records
          WHERE record_date IN ({placeholders})
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
              INSERT OR IGNORE INTO cows (tag, name, breed, parity, dob, latest_calving, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                tag,
                request.form.get("name") or None,
                request.form.get("breed") or None,
                int(request.form.get("parity") or 1),
                request.form.get("dob") or None,
                request.form.get("latest_calving") or None,
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
              INSERT INTO health_events (cow_tag, event_date, event_type, details, withdrawal_until, created_at)
              VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cow_tag,
                event_date,
                event_type,
                request.form.get("details") or None,
                request.form.get("withdrawal_until") or None,
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
.wrap{max-width:780px;margin:0 auto;padding:22px}
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
@media(min-width:580px){.grid2{grid-template-columns:1fr 1fr}}
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
.hero{display:grid;grid-template-columns:1fr;gap:16px;margin-bottom:14px}
.hero .stat{background:#0b1220;border:1px dashed #1b2a3e;border-radius:12px;padding:10px 12px;color:var(--muted);font-size:12px}
.flash{margin:8px 0;padding:10px;border-radius:10px}
.flash.ok{background:#0e3821;border:1px solid #1c7f4b}
.flash.error{background:#3b0e0e;border:1px solid #7f1d1d}
small.muted{color:var(--muted)}
"""

TPL_HOME = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
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
      <span class="badge">v2</span>
    </div>

    <div class="card">
      <div class="hero">
        <div class="stat">Tip: Add to your phone’s Home Screen for an app-like feel.</div>
      </div>
      <div style="font-size:20px;font-weight:800;margin-bottom:10px">Main menu</div>
      <div class="menu">
        <a class="btn" href="{{ url_for('records_screen') }}">Cow Records</a>
        <a class="btn secondary" href="{{ url_for('new_record_screen') }}">New Recording</a>
        <a class="btn secondary" href="{{ url_for('recent_screen') }}">Recent Entries</a>
        <a class="btn secondary" href="{{ url_for('cows_screen') }}">Cows</a>
        <a class="btn secondary" href="{{ url_for('health_screen') }}">Health</a>
        <a class="btn secondary" href="{{ url_for('breeding_screen') }}">Breeding</a>
        <a class="btn secondary" href="{{ url_for('import_csv') }}">Import CSV</a>
        <a class="btn secondary" href="{{ url_for('export_csv') }}">Export CSV</a>
        <a class="btn secondary" href="{{ url_for('export_excel') }}">Export Excel</a>
      </div>
    </div>

    <div class="subtle">Data is stored securely on the server (SQLite). Export anytime.</div>
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
            <select id="session" name="session">
              <option>AM</option>
              <option>PM</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label for="note">Note (optional)</label>
          <input id="note" name="note" placeholder="short note e.g., fresh / mastitis watch">
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
        Showing latest {{ rows|length }} (limit {{ limit }}).
      </div>
      <table aria-label="Recent raw records">
        <thead>
          <tr>
            <th>ID</th>
            <th>Cow #</th>
            <th>Litres</th>
            <th>Date</th>
            <th>Session</th>
            <th>Note</th>
            <th>Saved (UTC)</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% if rows %}
            {% for r in rows %}
              <tr>
                <td>{{ r['id'] }}</td>
                <td>{{ r['cow_number'] }}</td>
                <td>{{ '%.2f'|format(r['litres']) }}</td>
                <td>{{ r['record_date'] }}</td>
                <td>{{ r['session'] }}</td>
                <td>{{ r['note'] or '' }}</td>
                <td>{{ r['created_at'] }}</td>
                <td>
                  <form method="POST" action="{{ url_for('delete', rec_id=r['id']) }}" onsubmit="return confirm('Delete this entry?')">
                    <button class="btn warn" type="submit">Delete</button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="8" style="color:var(--muted)">No entries yet.</td></tr>
          {% endif %}
        </tbody>
      </table>
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
      <p>Upload a CSV with headers: <code>cow_number, litres, record_date, session (AM/PM), note</code>.</p>
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
        <div><button class="btn" type="submit">Save Cow</button></div>
      </form>
    </div>

    <div class="card">
      <table aria-label="Cows">
        <thead><tr><th>Tag</th><th>Name</th><th>Breed</th><th>Parity</th><th>DOB</th><th>Latest Calving</th></tr></thead>
        <tbody>
          {% for r in rows %}
            <tr>
              <td>{{ r['tag'] }}</td>
              <td>{{ r['name'] or '' }}</td>
              <td>{{ r['breed'] or '' }}</td>
              <td>{{ r['parity'] if r['parity'] is not none else '' }}</td>
              <td>{{ r['dob'] or '' }}</td>
              <td>{{ r['latest_calving'] or '' }}</td>
            </tr>
          {% else %}
            <tr><td colspan="6" class="muted">No cows yet.</td></tr>
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
        <div class="field" style="grid-column:1/-1"><label>Details</label><textarea name="details" rows="3" placeholder="notes"></textarea></div>
        <div><button class="btn" type="submit">Save Event</button></div>
      </form>
    </div>

    <div class="card">
      <table aria-label="Health events">
        <thead><tr><th>Date</th><th>Cow</th><th>Type</th><th>Withdrawal</th><th>Details</th></tr></thead>
        <tbody>
          {% for r in rows %}
            <tr>
              <td>{{ r['event_date'] }}</td>
              <td>{{ r['cow_tag'] }}</td>
              <td>{{ r['event_type'] }}</td>
              <td>{{ r['withdrawal_until'] or '' }}</td>
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

# ---------- Local run ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
