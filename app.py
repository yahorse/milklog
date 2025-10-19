# app.py
# Milk Log – Flask app with Google Login, per-user data, and schema migration
# - Google OAuth (Authlib), per-user isolation via owner_sub
# - Migration-safe DB init (adds owner_sub if missing, creates indexes)
# - Claim legacy rows (NULL owner_sub) for first logged-in user
# - Render-ready: persistent SQLite on /var/data, WAL mode
# - Features: add, pivot-by-date, recent+delete, export.xlsx, optional backup, healthz

import os
import io
import sqlite3
from contextlib import closing
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    send_file, session, abort
)
from openpyxl import Workbook
from authlib.integrations.flask_client import OAuth

app = Flask(__name__)

# ---------- Config & Persistence ----------
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-please-change")

DATA_DIR = os.getenv("DATA_DIR", "/var/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "milk_records.db")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI")
BACKUP_TOKEN = os.getenv("BACKUP_TOKEN")

# ---------- OAuth (Google) ----------
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

def current_owner_sub() -> str:
    u = session.get("user")
    return u["sub"] if u else None

# ---------- DB helpers ----------
def init_db():
    """Create/upgrade schema safely (handles legacy DB without owner_sub)."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Ensure table exists (owner_sub nullable so we can migrate legacy rows)
        conn.execute("""
          CREATE TABLE IF NOT EXISTS milk_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_sub TEXT,
            cow_number TEXT NOT NULL,
            litres REAL NOT NULL CHECK(litres >= 0),
            record_date TEXT NOT NULL,       -- YYYY-MM-DD
            created_at TEXT NOT NULL         -- ISO (UTC)
          )
        """)

        # MIGRATION: add owner_sub column if missing on very old DBs
        cols = [r[1] for r in conn.execute("PRAGMA table_info(milk_records)").fetchall()]
        if "owner_sub" not in cols:
            conn.execute("ALTER TABLE milk_records ADD COLUMN owner_sub TEXT")

        # Indexes (safe to create repeatedly)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_owner_date ON milk_records(owner_sub, record_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_owner_cow  ON milk_records(owner_sub, cow_number)")

def add_record(owner_sub: str, cow_number: str, litres: float, record_date_str: str):
    _ = date.fromisoformat(record_date_str)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("""
          INSERT INTO milk_records (owner_sub, cow_number, litres, record_date, created_at)
          VALUES (?, ?, ?, ?, ?)
        """, (owner_sub, cow_number.strip(), float(litres), record_date_str, datetime.utcnow().isoformat()))

def delete_record(owner_sub: str, rec_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        cur = conn.execute("DELETE FROM milk_records WHERE id = ? AND owner_sub = ?", (rec_id, owner_sub))
        return cur.rowcount

def get_all_rows(owner_sub: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
          SELECT id, cow_number, litres, record_date, created_at
          FROM milk_records
          WHERE owner_sub = ?
          ORDER BY record_date ASC, cow_number ASC, id ASC
        """, (owner_sub,))
        return cur.fetchall()

def get_recent_rows(owner_sub: str, limit:int=100):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
          SELECT id, cow_number, litres, record_date, created_at
          FROM milk_records
          WHERE owner_sub = ?
          ORDER BY id DESC
          LIMIT ?
        """, (owner_sub, limit))
        return cur.fetchall()

def get_last_n_dates(owner_sub: str, n:int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
          SELECT DISTINCT record_date
          FROM milk_records
          WHERE owner_sub = ?
          ORDER BY record_date DESC
          LIMIT ?
        """, (owner_sub, n))
        dates = [r["record_date"] for r in cur.fetchall()]
        return list(reversed(dates))  # oldest -> newest across columns

def build_pivot_for_dates(owner_sub: str, dates):
    if not dates:
        return [], []
    placeholders = ",".join("?" for _ in dates)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        # owner_sub plus date placeholders
        cur = conn.execute(f"""
          SELECT cow_number, record_date, SUM(litres) AS litres
          FROM milk_records
          WHERE owner_sub = ?
            AND record_date IN ({placeholders})
          GROUP BY cow_number, record_date
        """, tuple([owner_sub] + dates))
        data = cur.fetchall()

    by_cow = {}
    for r in data:
        cow = r["cow_number"]
        by_cow.setdefault(cow, {})
        by_cow[cow][r["record_date"]] = float(r["litres"] or 0)

    def cow_key(c):
        try: return (0, int(c))
        except: return (1, c)

    rows = []
    for cow in sorted(by_cow.keys(), key=cow_key):
        cells = [round(by_cow[cow].get(d, 0.0), 2) for d in dates]
        rows.append({"cow": cow, "cells": cells, "total": round(sum(cells), 2)})
    return dates, rows

def claim_legacy_rows_for(owner_sub: str):
    """Assign any legacy rows (NULL owner_sub) to this user once."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("UPDATE milk_records SET owner_sub = ? WHERE owner_sub IS NULL", (owner_sub,))

# Ensure DB exists / migrated at import time (important for Gunicorn/Render)
init_db()

# ---------- Auth routes ----------
@app.route("/login")
def login():
    if "google" not in oauth._clients:
        return "Google OAuth not configured.", 500
    session["post_login_redirect"] = request.args.get("next") or url_for("home")
    return oauth.google.authorize_redirect(redirect_uri=OAUTH_REDIRECT_URI)

@app.route("/auth/callback")
def auth_callback():
    if "google" not in oauth._clients:
        return "Google OAuth not configured.", 500
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.parse_id_token(token)
    if not userinfo or "sub" not in userinfo:
        return "Login failed.", 400

    session["user"] = {
        "sub": userinfo["sub"],
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
    }

    # OPTIONAL: restrict to a domain
    # email = session["user"].get("email", "")
    # if not email.endswith("@yourfarm.ie"):
    #     session.clear()
    #     return "Unauthorized domain", 403

    # Claim legacy rows so the user can see pre-existing data
    try:
        claim_legacy_rows_for(session["user"]["sub"])
    except Exception:
        pass

    return redirect(session.pop("post_login_redirect", url_for("home")))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# ---------- App routes ----------
@app.route("/")
def home():
    return render_template_string(TPL_HOME, base_css=BASE_CSS, user=session.get("user"))

@app.route("/new")
@login_required
def new_record_screen():
    return render_template_string(TPL_NEW, base_css=BASE_CSS, today=date.today().isoformat(), user=session.get("user"))

@app.route("/records")
@login_required
def records_screen():
    try:
        last = int(request.args.get("last", "7"))
    except ValueError:
        last = 7
    last = max(1, min(last, 90))
    prev_last = max(1, last - 3)
    next_last = min(90, last + 3)

    owner = current_owner_sub()
    dates = get_last_n_dates(owner, last)
    dates, rows = build_pivot_for_dates(owner, dates)
    return render_template_string(
        TPL_RECORDS,
        base_css=BASE_CSS, user=session.get("user"),
        dates=dates, rows=rows, last=last,
        prev_last=prev_last, next_last=next_last
    )

@app.route("/recent")
@login_required
def recent_screen():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))

    owner = current_owner_sub()
    rows = get_recent_rows(owner, limit)
    msg = "Deleted 1 entry." if request.args.get("deleted") == "1" else None
    return render_template_string(TPL_RECENT, base_css=BASE_CSS, rows=rows, msg=msg, limit=limit, user=session.get("user"))

@app.route("/add", methods=["POST"])
@login_required
def add():
    owner = current_owner_sub()
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
        add_record(owner, cow, litres_val, record_date_str)
    except ValueError:
        return "Bad date. Use YYYY-MM-DD.", 400

    return redirect(url_for("new_record_screen"))

@app.route("/delete/<int:rec_id>", methods=["POST"])
@login_required
def delete(rec_id):
    owner = current_owner_sub()
    deleted = delete_record(owner, rec_id)
    if deleted == 0:
        abort(404)
    return redirect(url_for("recent_screen", deleted=1))

@app.route("/export.xlsx")
@login_required
def export_excel():
    owner = current_owner_sub()
    data = get_all_rows(owner)
    wb = Workbook()

    ws = wb.active
    ws.title = "Raw Records"
    ws.append(["ID", "Cow Number", "Litres", "Record Date", "Saved (UTC)"])
    for r in data:
        ws.append([r["id"], r["cow_number"], r["litres"], r["record_date"], r["created_at"]])
    for col, w in zip("ABCDE", [8,12,10,12,25]):
        ws.column_dimensions[col].width = w

    dates = get_last_n_dates(owner, 7)
    dates, rows = build_pivot_for_dates(owner, dates)
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

@app.route("/backup.db")
@login_required
def backup_db():
    if not BACKUP_TOKEN:
        return "Not enabled", 404
    if request.args.get("token") != BACKUP_TOKEN:
        return "Forbidden", 403
    return send_file(DB_PATH, as_attachment=True, download_name="milk_records.db")

@app.route("/healthz")
def healthz():
    return "ok", 200

# ---------- Styles & Templates ----------
BASE_CSS = """
:root{
  --bg:#0b1220; --panel:#0f172a; --border:#1f2937; --text:#e5e7eb;
  --muted:#94a3b8; --accent:#22c55e; --radius:18px; --shadow:0 10px 30px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#08101d,#0f172a);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
.wrap{max-width:520px;margin:0 auto;padding:18px}
.top{display:flex;align-items:center;justify-content:space-between;margin:10px 2px 16px}
.title{font-weight:800;letter-spacing:.3px}
.card{background:linear-gradient(180deg,#0b1220,#121a2e);border:1px solid var(--border);border-radius:var(--radius);padding:16px;box-shadow:var(--shadow)}
.menu{display:grid;gap:12px}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;background:var(--accent);color:#05220f;font-weight:800;padding:14px 16px;border:none;border-radius:14px;cursor:pointer;text-decoration:none;text-align:center}
.btn.secondary{background:#0b1220;color:var(--text);border:1px solid var(--border)}
.btn.warn{background:#ef4444;color:#fff}
.field{display:grid;gap:6px}
label{font-size:13px;color:var(--muted)}
input{background:#0b1220;border:1px solid var(--border);color:var(--text);padding:14px 12px;border-radius:12px;font-size:16px;width:100%}
.grid2{display:grid;gap:12px;grid-template-columns:1fr}
@media(min-width:420px){.grid2{grid-template-columns:1fr 1fr}}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px;overflow-x:auto;display:block}
thead, tbody { display: table; width: 100%; }
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:600;background:#0b1220;position:sticky;top:0}
tr:hover td{background:rgba(96,165,250,.06)}
.hint{color:var(--muted);font-size:12px;text-align:center;margin-top:10px}
.userbar{display:flex;gap:10px;align-items:center}
.userbar img{width:28px;height:28px;border-radius:50%}
"""

TPL_HOME = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Milk Log</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="title">Milk Log</div>
      <div class="userbar">
        {% if user %}
          {% if user.picture %}<img src="{{ user.picture }}" alt="avatar">{% endif %}
          <span>{{ user.email or user.name }}</span>
          <a class="btn secondary" href="{{ url_for('logout') }}">Logout</a>
        {% else %}
          <a class="btn" href="{{ url_for('login') }}">Login with Google</a>
        {% endif %}
      </div>
    </div>
    <div class="card">
      <div style="font-size:18px;font-weight:700;margin-bottom:8px">First app menu</div>
      <div class="menu">
        <a class="btn" href="{{ url_for('records_screen') }}">Cow Records</a>
        <a class="btn secondary" href="{{ url_for('new_record_screen') }}">New Recording</a>
        <a class="btn secondary" href="{{ url_for('recent_screen') }}">Recent Entries</a>
      </div>
    </div>
    <div class="hint">Only logged-in users can add/view their records.</div>
  </div>
</body></html>
"""

TPL_NEW = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>New Recording</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
      <div class="title">New Recording</div>
      <a class="btn secondary" href="{{ url_for('logout') }}">Logout</a>
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

TPL_RECORDS = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cow Records</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
      <div class="title">Cow Records</div>
      <div style="display:flex;gap:8px;align-items:center">
        <a class="btn secondary" href="{{ url_for('records_screen', last=prev_last) }}">-3d</a>
        <a class="btn secondary" href="{{ url_for('records_screen', last=next_last) }}">+3d</a>
        <a class="btn" href="{{ url_for('export_excel') }}">Export</a>
      </div>
    </div>
    <div class="card">
      <div style="color:var(--muted);font-size:13px;margin-bottom:8px">
        Showing last {{ last }} date{{ '' if last==1 else 's' }}.
      </div>
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

TPL_RECENT = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recent Entries</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
      <div class="title">Recent Entries</div>
      <a class="btn secondary" href="{{ url_for('logout') }}">Logout</a>
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

# ---------- Local dev ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
