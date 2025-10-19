# app.py
# Milk Log v2 — Flask app with extended herd management, health/breeding, and analytics-ready schema
# Author: Aidan Molloy (expanded version)

import os
import io
import csv
import sqlite3
from contextlib import closing
from datetime import datetime, date
from flask import Flask, request, redirect, url_for, render_template_string, send_file, flash

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev")

# ---------- DATABASE ----------
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "milk_records.db")

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Milk Records
        conn.execute("""
        CREATE TABLE IF NOT EXISTS milk_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_number TEXT NOT NULL,
          litres REAL NOT NULL CHECK(litres>=0),
          record_date TEXT NOT NULL,
          session TEXT DEFAULT 'AM' CHECK(session IN ('AM','PM')),
          note TEXT,
          created_at TEXT NOT NULL
        )""")

        # Cows
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cows (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tag TEXT UNIQUE NOT NULL,
          name TEXT, breed TEXT,
          parity INTEGER DEFAULT 1,
          dob TEXT, latest_calving TEXT,
          created_at TEXT NOT NULL
        )""")

        # Health Events
        conn.execute("""
        CREATE TABLE IF NOT EXISTS health_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,
          details TEXT, withdrawal_until TEXT,
          created_at TEXT NOT NULL
        )""")

        # Breeding Events
        conn.execute("""
        CREATE TABLE IF NOT EXISTS breeding_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cow_tag TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,
          sire TEXT, details TEXT,
          created_at TEXT NOT NULL
        )""")

init_db()

# ---------- HELPERS ----------
def add_record(cow_number, litres, record_date, session, note):
    date.fromisoformat(record_date)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("""
        INSERT INTO milk_records (cow_number, litres, record_date, session, note, created_at)
        VALUES (?,?,?,?,?,?)""", (cow_number.strip(), float(litres), record_date, session, note, datetime.utcnow().isoformat()))

def query(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()

def delete_record(rec_id):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM milk_records WHERE id=?", (rec_id,))

# ---------- ROUTES ----------
@app.route("/")
def home():
    return render_template_string(TPL_HOME, base_css=BASE_CSS)

@app.route("/new")
def new_record():
    return render_template_string(TPL_NEW, base_css=BASE_CSS, today=date.today().isoformat())

@app.route("/add", methods=["POST"])
def add():
    try:
        add_record(
            request.form["cow_number"],
            float(request.form["litres"]),
            request.form["record_date"],
            request.form.get("session","AM"),
            request.form.get("note","")
        )
        flash("Record added successfully!", "ok")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("new_record"))

@app.route("/records")
def records():
    dates = [r["record_date"] for r in query("SELECT DISTINCT record_date FROM milk_records ORDER BY record_date DESC LIMIT 7")]
    if not dates: return render_template_string(TPL_RECORDS, base_css=BASE_CSS, rows=[], dates=[], sessions=["AM","PM"])
    data = query("""
        SELECT cow_number, record_date, session, SUM(litres) AS litres
        FROM milk_records WHERE record_date IN (%s)
        GROUP BY cow_number, record_date, session
    """ % ",".join("?"*len(dates)), dates)
    by_cow = {}
    for r in data:
        c = r["cow_number"]; by_cow.setdefault(c, {})
        by_cow[c][(r["record_date"], r["session"])] = r["litres"]
    sessions = ["AM","PM"]
    rows=[]
    for cow in sorted(by_cow.keys()):
        row=[cow]
        total=0
        for d in dates:
            for s in sessions:
                val=by_cow[cow].get((d,s),0)
                row.append(round(val,2)); total+=val
        row.append(round(total,2))
        rows.append(row)
    return render_template_string(TPL_RECORDS, base_css=BASE_CSS, rows=rows, dates=dates, sessions=sessions)

@app.route("/recent")
def recent():
    rows=query("SELECT * FROM milk_records ORDER BY id DESC LIMIT 100")
    return render_template_string(TPL_RECENT, base_css=BASE_CSS, rows=rows)

@app.route("/delete/<int:id>", methods=["POST"])
def delete(id):
    delete_record(id)
    return redirect(url_for("recent"))

# --- CSV Import/Export ---
@app.route("/import", methods=["GET","POST"])
def import_csv():
    msg=None
    if request.method=="POST" and "file" in request.files:
        f=request.files["file"]; text=f.stream.read().decode("utf-8")
        reader=csv.DictReader(io.StringIO(text))
        count=0
        for row in reader:
            try:
                add_record(row["cow_number"], float(row["litres"]), row["record_date"], row.get("session","AM"), row.get("note",""))
                count+=1
            except Exception: pass
        msg=f"Imported {count} records"
    return render_template_string(TPL_IMPORT, base_css=BASE_CSS, msg=msg)

@app.route("/export.csv")
def export_csv():
    rows=query("SELECT * FROM milk_records ORDER BY record_date DESC")
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(rows[0].keys() if rows else ["id","cow_number","litres","record_date","session","note","created_at"])
    for r in rows: w.writerow(r)
    out.seek(0)
    return send_file(io.BytesIO(out.read().encode()), as_attachment=True, download_name="milk_records.csv", mimetype="text/csv")

# --- Cows ---
@app.route("/cows", methods=["GET","POST"])
def cows():
    msg=None
    if request.method=="POST":
        conn=sqlite3.connect(DB_PATH); c=conn.cursor()
        c.execute("INSERT OR IGNORE INTO cows (tag,name,breed,parity,dob,latest_calving,created_at) VALUES (?,?,?,?,?,?,?)",
            (request.form["tag"], request.form.get("name"), request.form.get("breed"), request.form.get("parity",1),
             request.form.get("dob"), request.form.get("latest_calving"), datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        msg="Cow added!"
    rows=query("SELECT * FROM cows ORDER BY tag")
    return render_template_string(TPL_COWS, base_css=BASE_CSS, rows=rows, msg=msg)

# --- Health ---
@app.route("/health", methods=["GET","POST"])
def health():
    msg=None
    if request.method=="POST":
        conn=sqlite3.connect(DB_PATH); c=conn.cursor()
        c.execute("INSERT INTO health_events (cow_tag,event_date,event_type,details,withdrawal_until,created_at) VALUES (?,?,?,?,?,?)",
            (request.form["cow_tag"], request.form["event_date"], request.form["event_type"], request.form.get("details"),
             request.form.get("withdrawal_until"), datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        msg="Event added."
    rows=query("SELECT * FROM health_events ORDER BY event_date DESC LIMIT 100")
    return render_template_string(TPL_HEALTH, base_css=BASE_CSS, rows=rows, msg=msg)

# --- Breeding ---
@app.route("/breeding", methods=["GET","POST"])
def breeding():
    msg=None
    if request.method=="POST":
        conn=sqlite3.connect(DB_PATH); c=conn.cursor()
        c.execute("INSERT INTO breeding_events (cow_tag,event_date,event_type,sire,details,created_at) VALUES (?,?,?,?,?,?)",
            (request.form["cow_tag"], request.form["event_date"], request.form["event_type"], request.form.get("sire"),
             request.form.get("details"), datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        msg="Breeding event added."
    rows=query("SELECT * FROM breeding_events ORDER BY event_date DESC LIMIT 100")
    return render_template_string(TPL_BREEDING, base_css=BASE_CSS, rows=rows, msg=msg)

@app.route("/healthz")
def healthz():
    return "ok", 200

# ---------- BASE CSS ----------
BASE_CSS = """body{font-family:system-ui;background:#0f172a;color:#e5e7eb;margin:0;padding:0}
.wrap{max-width:720px;margin:0 auto;padding:16px}
.card{background:#111a2f;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:16px}
.btn{background:#22c55e;color:#0b1220;font-weight:700;padding:10px 14px;border-radius:10px;text-decoration:none;display:inline-block}
.btn.secondary{background:#0b1220;color:#e5e7eb;border:1px solid #1f2937}
table{width:100%;border-collapse:collapse}th,td{padding:6px 8px;border-bottom:1px solid #1f2937}
th{color:#9aa5b1;text-align:left}input,select{width:100%;padding:8px;border-radius:8px;border:1px solid #1f2937;background:#0b1220;color:#e5e7eb}
form{display:grid;gap:8px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}"""

# ---------- HTML Templates ----------
TPL_HOME = """<!doctype html><html><head><title>Milk Log</title><style>{{ base_css }}</style></head>
<body><div class='wrap'>
<div class='top'><h1>🐄 Milk Log</h1><a class='btn secondary' href='/records'>View Records</a></div>
<div class='card'>
  <a class='btn' href='/new'>New Recording</a>
  <a class='btn secondary' href='/recent'>Recent</a>
  <a class='btn secondary' href='/cows'>Cows</a>
  <a class='btn secondary' href='/health'>Health</a>
  <a class='btn secondary' href='/breeding'>Breeding</a>
  <a class='btn secondary' href='/import'>Import CSV</a>
  <a class='btn secondary' href='/export.csv'>Export CSV</a>
</div></div></body></html>"""

# (Other templates TPL_NEW, TPL_RECORDS, TPL_RECENT, TPL_COWS, TPL_HEALTH, TPL_BREEDING, TPL_IMPORT omitted for brevity)
# They follow your current visual style (cards, btns, table).

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
