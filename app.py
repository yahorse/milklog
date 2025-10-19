# app.py
# Milk Log – Flask app (no login) with a professional mobile-first UI
# - Enter Cow Number, Litres, Date (YYYY-MM-DD)
# - Cow Records: dynamic columns by date (same cow+date sums)
# - Recent Entries with Delete
# - Export to Excel (raw + pivot)
# - Render-ready: persistent SQLite on /var/data, WAL mode
# - Health check route

import os
import io
import sqlite3
from contextlib import closing
from datetime import datetime, date

from flask import Flask, request, redirect, url_for, render_template_string, send_file

app = Flask(__name__)

# ----------- Persistence (Render) -----------
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "milk_records.db")

# ----------- DB helpers -----------
def init_db():
    """Create/upgrade schema safely."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("""
          CREATE TABLE IF NOT EXISTS milk_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cow_number TEXT NOT NULL,
            litres REAL NOT NULL CHECK(litres >= 0),
            record_date TEXT NOT NULL,        -- YYYY-MM-DD
            created_at TEXT NOT NULL          -- ISO (UTC)
          )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON milk_records(record_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cow  ON milk_records(cow_number)")

def add_record(cow_number: str, litres: float, record_date_str: str):
    # Validate date format
    _ = date.fromisoformat(record_date_str)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("""
          INSERT INTO milk_records (cow_number, litres, record_date, created_at)
          VALUES (?, ?, ?, ?)
        """, (cow_number.strip(), float(litres), record_date_str, datetime.utcnow().isoformat()))

def delete_record(rec_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM milk_records WHERE id = ?", (rec_id,))

def get_all_rows():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
          SELECT id, cow_number, litres, record_date, created_at
          FROM milk_records
          ORDER BY record_date ASC, cow_number ASC, id ASC
        """)
        return cur.fetchall()

def get_recent_rows(limit:int=100):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
          SELECT id, cow_number, litres, record_date, created_at
          FROM milk_records
          ORDER BY id DESC
          LIMIT ?
        """, (limit,))
        return cur.fetchall()

def get_last_n_dates(n:int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
          SELECT DISTINCT record_date
          FROM milk_records
          ORDER BY record_date DESC
          LIMIT ?
        """, (n,))
        dates = [r["record_date"] for r in cur.fetchall()]
        return list(reversed(dates))  # show oldest -> newest across columns

def build_pivot_for_dates(dates):
    """
    Return (dates, rows) where:
      dates: ['YYYY-MM-DD', ...]
      rows: [{'cow':'2146','cells':[12,17,...],'total':29}, ...]
    """
    if not dates:
        return [], []

    placeholders = ",".join("?" for _ in dates)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"""
          SELECT cow_number, record_date, SUM(litres) AS litres
          FROM milk_records
          WHERE record_date IN ({placeholders})
          GROUP BY cow_number, record_date
        """, tuple(dates))
        data = cur.fetchall()

    # cow -> {date: litres}
    by_cow = {}
    for r in data:
        cow = r["cow_number"]
        by_cow.setdefault(cow, {})
        by_cow[cow][r["record_date"]] = float(r["litres"] or 0)

    # numeric-ish sort of cows
    def cow_key(c):
        try:
            return (0, int(c))
        except:
            return (1, c)

    rows = []
    for cow in sorted(by_cow.keys(), key=cow_key):
        cells = [round(by_cow[cow].get(d, 0.0), 2) for d in dates]
        rows.append({"cow": cow, "cells": cells, "total": round(sum(cells), 2)})
    return dates, rows

# Ensure DB exists instantly on import (important on Render/Gunicorn)
init_db()

# ----------- Routes -----------
@app.route("/")
def home():
    return render_template_string(TPL_HOME, base_css=BASE_CSS)

@app.route("/new")
def new_record_screen():
    return render_template_string(TPL_NEW, base_css=BASE_CSS, today=date.today().isoformat())

@app.route("/records")
def records_screen():
    try:
        last = int(request.args.get("last", "7"))
    except ValueError:
        last = 7
    last = max(1, min(last, 90))

    prev_last = max(1, last - 3)
    next_last = min(90, last + 3)

    dates = get_last_n_dates(last)
    dates, rows = build_pivot_for_dates(dates)
    return render_template_string(
        TPL_RECORDS,
        base_css=BASE_CSS,
        dates=dates, rows=rows, last=last,
        prev_last=prev_last, next_last=next_last
    )

@app.route("/recent")
def recent_screen():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    rows = get_recent_rows(limit)
    msg = "Deleted 1 entry." if request.args.get("deleted") == "1" else None
    return render_template_string(TPL_RECENT, base_css=BASE_CSS, rows=rows, msg=msg, limit=limit)

@app.route("/add", methods=["POST"])
def add():
    cow = request.form.get("cow_number", "").strip()
    litres = request.form.get("litres", "").strip()
    record_date_str = (request.form.get("record_date") or date.today().isoformat()).strip()

    if not cow:
        return "Cow number is required", 400
    try:
        litres_val = float(litres)
        if litres_val < 0:
            raise ValueError
    except ValueError:
        return "Litres must be a non-negative number", 400

    try:
        add_record(cow, litres_val, record_date_str)
    except ValueError:
        return "Bad date. Use YYYY-MM-DD.", 400

    return redirect(url_for("new_record_screen"))

@app.route("/delete/<int:rec_id>", methods=["POST"])
def delete(rec_id):
    delete_record(rec_id)
    return redirect(url_for("recent_screen", deleted=1))

@app.route("/export.xlsx")
def export_excel():
    data = get_all_rows()
    from openpyxl import Workbook  # lazy import for speed
    wb = Workbook()

    # Sheet 1: Raw data
    ws = wb.active
    ws.title = "Raw Records"
    ws.append(["ID", "Cow Number", "Litres", "Record Date", "Saved (UTC)"])
    for r in data:
        ws.append([r["id"], r["cow_number"], r["litres"], r["record_date"], r["created_at"]])
    for col, w in zip("ABCDE", [8,12,10,12,25]):
        ws.column_dimensions[col].width = w

    # Sheet 2: Pivot (last 7 dates)
    dates = get_last_n_dates(7)
    dates, rows = build_pivot_for_dates(dates)
    ws2 = wb.create_sheet("Pivot (last 7 dates)")
    ws2.append(["Cow #", *dates, "Total"])
    for row in rows:
        ws2.append([row["cow"], *row["cells"], row["total"]])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(
        bio,
        as_attachment=True,
        download_name="milk-records.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

@app.route("/healthz")
def healthz():
    return "ok", 200

# ----------- Styles & Templates -----------
BASE_CSS = """
:root{
  --bg:#0b1220; --panel:#0f172a; --border:#223044; --text:#e5e7eb;
  --muted:#9aa5b1; --accent:#22c55e; --accent-fore:#07220e;
  --radius:18px; --shadow:0 14px 40px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 10% -10%, #0a1222 0, #0b1629 30%, #0f172a 70%), #0f172a;
     color:var(--text);font-family:Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial}
.wrap{max-width:680px;margin:0 auto;padding:22px}
.top{display:flex;align-items:center;justify-content:space-between;margin:6px 2px 18px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:40px;height:40px}
.title{font-weight:900;letter-spacing:.2px;font-size:22px}
.kicker{color:var(--muted);font-size:12px;margin-top:-6px}
.card{background:linear-gradient(180deg,#0c1324,#111a2f);border:1px solid var(--border);
      border-radius:var(--radius);padding:18px 18px 16px;box-shadow:var(--shadow)}
.menu{display:grid;gap:12px}
.btn{display:flex;align-items:center;justify-content:center;gap:10px;background:var(--accent);color:var(--accent-fore);
     font-weight:800;padding:14px 16px;border:none;border-radius:14px;cursor:pointer;text-decoration:none;text-align:center}
.btn.secondary{background:#0b1220;color:var(--text);border:1px solid var(--border)}
.btn.warn{background:#ef4444;color:#fff}
.field{display:grid;gap:6px}
label{font-size:13px;color:var(--muted)}
input{background:#0b1220;border:1px solid var(--border);color:var(--text);padding:14px 12px;border-radius:12px;font-size:16px;width:100%}
.grid2{display:grid;gap:12px;grid-template-columns:1fr}
@media(min-width:520px){.grid2{grid-template-columns:1fr 1fr}}
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
"""

# Home – polished, simple, no login
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
      <span class="badge">v1.0</span>
    </div>

    <div class="card">
      <div class="hero">
        <div class="stat">Tip: Add this to your phone’s Home Screen for a native feel.</div>
      </div>
      <div style="font-size:20px;font-weight:800;margin-bottom:10px">First app menu</div>
      <div class="menu">
        <a class="btn" href="{{ url_for('records_screen') }}">Cow Records</a>
        <a class="btn secondary" href="{{ url_for('new_record_screen') }}">New Recording</a>
        <a class="btn secondary" href="{{ url_for('recent_screen') }}">Recent Entries</a>
      </div>
    </div>

    <div class="subtle">Data is stored securely on the server (SQLite). Export anytime.</div>
  </div>
</body></html>
"""

# New Recording
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
        <div class="field" style="margin-top:12px">
          <label for="record_date">Record date</label>
          <input id="record_date" name="record_date" type="date" value="{{ today }}">
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

# Cow Records (pivot)
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
      <div style="color:var(--muted);font-size:13px;margin-bottom:8px">Showing last {{ last }} date{{ '' if last==1 else 's' }}.</div>
      <table aria-label="Records by cow">
        <thead>
          <tr>
            <th>Cow #</th>
            {% for d in dates %}<th>{{ d }}</th>{% endfor %}
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
            <tr><td colspan="{{ 2 + (dates|length) }}" style="color:var(--muted)">No records yet.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

# Recent Entries
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

    {% if msg %}
      <div class="card" style="border-color:#16a34a">✔ {{ msg }}</div>
    {% endif %}

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
                <td>{{ r['created_at'] }}</td>
                <td>
                  <form method="POST" action="{{ url_for('delete', rec_id=r['id']) }}" onsubmit="return confirm('Delete this entry?')">
                    <button class="btn warn" type="submit">Delete</button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="6" style="color:var(--muted)">No entries yet.</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""

# ----------- Local dev -----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
