# app.py
# Milk Log v4 — Single-file Flask app for Render (gunicorn app:app)
# Features:
# - Users: register/login/logout (Flask-Login, hashed passwords)
# - First user becomes admin
# - owner_id on milk rows; all queries scoped to current_user
# - Add/Delete milk entries, tags/notes, per-day pivot, CSV export
# - Admin page to claim legacy rows missing owner_id
# - PWA: manifest + service worker
# - SQLite with WAL; idempotent schema bootstrap/migrations

import os
import csv
import sqlite3
from contextlib import closing
from datetime import datetime, date
from typing import Iterable, Tuple, Any, Optional, Dict

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    Response, flash, jsonify
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# -----------------------------------------------------------------------------
# App / Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DB_PATH = os.environ.get("DATABASE_PATH", "milklog.db")

_db_dir = os.path.dirname(DB_PATH)
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def exec_sql(sql: str, args: Tuple[Any, ...] = ()) -> None:
    with closing(get_db()) as conn, conn:
        conn.execute(sql, args)

def exec_many(sql: str, rows: Iterable[Tuple[Any, ...]]) -> None:
    with closing(get_db()) as conn, conn:
        conn.executemany(sql, list(rows))

def query_all(sql: str, args: Tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with closing(get_db()) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall()

def query_one(sql: str, args: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
    with closing(get_db()) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchone()

def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}

# -----------------------------------------------------------------------------
# Schema bootstrap / migrations (idempotent)
# -----------------------------------------------------------------------------
def init_db() -> None:
    with closing(get_db()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # users table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            unit_pref TEXT NOT NULL DEFAULT 'L',
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        ucols = table_columns(conn, "users")
        if "role" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user';")
        if "unit_pref" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN unit_pref TEXT NOT NULL DEFAULT 'L';")
        if "is_admin" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;")

        # milk table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS milk (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            day DATE NOT NULL,
            am_litres REAL NOT NULL DEFAULT 0,
            pm_litres REAL NOT NULL DEFAULT 0,
            cow TEXT,
            tags TEXT,
            notes TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_owner_day ON milk(owner_id, day);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_deleted ON milk(deleted);")

# Call at import so workers are ready
init_db()

# -----------------------------------------------------------------------------
# User model / loader
# -----------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id: int, email: str, role: str = "user",
                 unit_pref: str = "L", is_admin: bool = False):
        self.id = id
        self.email = email
        self.role = role
        self.unit_pref = unit_pref
        self.is_admin = bool(is_admin)

    @staticmethod
    def from_row(r: sqlite3.Row) -> "User":
        return User(
            id=r["id"],
            email=r["email"],
            role=r["role"] if "role" in r.keys() else "user",
            unit_pref=r["unit_pref"] if "unit_pref" in r.keys() else "L",
            is_admin=bool(r["is_admin"]) if "is_admin" in r.keys() else False,
        )

@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    try:
        r = query_one(
            "SELECT id, email, role, unit_pref, is_admin FROM users WHERE id=?",
            (user_id,)
        )
    except sqlite3.OperationalError:
        # If startup race, try to bootstrap
        init_db()
        r = None
    return User.from_row(r) if r else None

# -----------------------------------------------------------------------------
# Templates
# -----------------------------------------------------------------------------
TPL_BASE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MilkLog</title>
  <link rel="manifest" href="{{ url_for('manifest') }}">
  <meta name="theme-color" content="#0f172a">
  <style>
    :root { color-scheme: light dark; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#0b1220; color:#e5e7eb;}
    a { color:#93c5fd; text-decoration:none;}
    header { background:#0a0f1a; border-bottom:1px solid #1f2937; position:sticky; top:0; z-index:10;}
    .wrap { max-width: 1000px; margin: 0 auto; padding: 1rem; }
    .nav { display:flex; gap:1rem; align-items:center; }
    .nav .grow { flex:1; }
    .btn { background:#111827; border:1px solid #374151; padding:0.45rem 0.8rem; border-radius:10px; color:#e5e7eb; cursor:pointer;}
    .btn:hover { background:#0f172a; }
    .card { background:#0b1324; border:1px solid #1f2937; border-radius:14px; padding:1rem; }
    input, select, textarea { width:100%; background:#0b1220; color:#e5e7eb; border:1px solid #334155; border-radius:10px; padding:0.5rem;}
    table { width:100%; border-collapse: collapse; }
    th, td { text-align:left; padding:0.5rem; border-bottom:1px solid #1f2937; vertical-align:top;}
    .grid { display:grid; gap:1rem; }
    .grid-2 { grid-template-columns: 1fr 1fr; }
    .muted { color:#94a3b8; }
    .danger { color:#fda4af; }
    .success { color:#86efac; }
    .tag { display:inline-block; padding:0.15rem 0.5rem; border:1px solid #334155; border-radius:999px; margin-right:0.25rem; font-size:0.8rem; color:#93c5fd;}
    footer { color:#94a3b8; font-size:0.9rem; padding:2rem 0; text-align:center;}
    .flash { padding:0.5rem 0.75rem; border-radius:10px; margin: 0.25rem 0; }
    .flash-ok { background:#052e16; border:1px solid #064e3b;}
    .flash-err { background:#3f1d1d; border:1px solid #7f1d1d;}
    .row-actions { display:flex; gap:0.5rem; }
  </style>
</head>
<body>
  <header>
    <div class="wrap nav">
      <div><strong>🥛 MilkLog</strong></div>
      <div class="grow"></div>
      {% if current_user.is_authenticated %}
        <a class="btn" href="{{ url_for('index') }}">Home</a>
        <a class="btn" href="{{ url_for('pivot') }}">Pivot</a>
        <a class="btn" href="{{ url_for('export_csv') }}">Export CSV</a>
        {% if current_user.is_admin %}
          <a class="btn" href="{{ url_for('admin') }}">Admin</a>
        {% endif %}
        <form method="post" action="{{ url_for('logout') }}" style="display:inline;">
          <button class="btn" type="submit">Logout ({{ current_user.email }})</button>
        </form>
      {% else %}
        <a class="btn" href="{{ url_for('login') }}">Login</a>
        <a class="btn" href="{{ url_for('register') }}">Register</a>
      {% endif %}
    </div>
  </header>

  <main class="wrap">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}
          <div class="flash {% if cat=='ok' %}flash-ok{% else %}flash-err{% endif %}">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    {% block body %}{% endblock %}
  </main>

  <footer>
    <div class="wrap">PWA ready. <span class="muted">Add to Home Screen for quick entry.</span></div>
  </footer>

  <script>
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('{{ url_for("service_worker") }}');
    }
  </script>
</body>
</html>
"""

TPL_HOME = r"""
{% extends "base.html" %}
{% block body %}
  <div class="grid" style="gap:1.5rem;">
    <div class="card">
      <h2>Add / Edit Milk</h2>
      <form method="post" action="{{ url_for('add_milk') }}" class="grid grid-2">
        <div>
          <label>Date</label>
          <input type="date" name="day" value="{{ today }}">
        </div>
        <div>
          <label>Cow (optional)</label>
          <input name="cow" placeholder="e.g. #12">
        </div>
        <div>
          <label>AM litres</label>
          <input type="number" step="0.01" name="am_litres" value="0">
        </div>
        <div>
          <label>PM litres</label>
          <input type="number" step="0.01" name="pm_litres" value="0">
        </div>
        <div>
          <label>Tags (comma separated)</label>
          <input name="tags" placeholder="e.g. freshened, mastitis, high-yield">
        </div>
        <div>
          <label>Notes</label>
          <input name="notes" placeholder="Optional notes">
        </div>
        <div>
          <button class="btn" type="submit">Save</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Recent Entries</h2>
      {% if rows %}
      <table>
        <thead>
          <tr>
            <th>Date</th><th>Cow</th><th>AM</th><th>PM</th><th>Total</th><th>Tags</th><th>Notes</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.day }}</td>
            <td>{{ r.cow or '' }}</td>
            <td>{{ "%.2f"|format(r.am_litres) }}</td>
            <td>{{ "%.2f"|format(r.pm_litres) }}</td>
            <td>{{ "%.2f"|format(r.am_litres + r.pm_litres) }}</td>
            <td>
              {% for t in (r.tags or '').split(',') if t.strip() %}
                <span class="tag">{{ t.strip() }}</span>
              {% endfor %}
            </td>
            <td class="muted">{{ r.notes or '' }}</td>
            <td class="row-actions">
              <form method="post" action="{{ url_for('delete_milk', mid=r.id) }}">
                <button class="btn danger" type="submit" onclick="return confirm('Delete entry?');">Delete</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <p class="muted">No entries yet. Add your first above.</p>
      {% endif %}
    </div>
  </div>
{% endblock %}
"""

TPL_LOGIN = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card" style="max-width:480px;">
    <h2>Login</h2>
    <form method="post" class="grid">
      <div>
        <label>Email</label>
        <input name="email" type="email" required>
      </div>
      <div>
        <label>Password</label>
        <input name="password" type="password" required>
      </div>
      <div>
        <button class="btn" type="submit">Login</button>
      </div>
    </form>
    <p class="muted">No account? <a href="{{ url_for('register') }}">Register</a></p>
  </div>
{% endblock %}
"""

TPL_REGISTER = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card" style="max-width:480px;">
    <h2>Create account</h2>
    <form method="post" class="grid">
      <div>
        <label>Email</label>
        <input name="email" type="email" required>
      </div>
      <div>
        <label>Password</label>
        <input name="password" type="password" required>
      </div>
      <div>
        <button class="btn" type="submit">Register</button>
      </div>
    </form>
  </div>
{% endblock %}
"""

TPL_PIVOT = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card">
    <h2>Pivot (Daily Totals)</h2>
    {% if rows %}
      <table>
        <thead><tr><th>Date</th><th>AM</th><th>PM</th><th>Total</th></tr></thead>
        <tbody>
          {% for r in rows %}
          <tr>
            <td>{{ r.day }}</td>
            <td>{{ "%.2f"|format(r.am_sum or 0) }}</td>
            <td>{{ "%.2f"|format(r.pm_sum or 0) }}</td>
            <td>{{ "%.2f"|format((r.am_sum or 0) + (r.pm_sum or 0)) }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No data.</p>
    {% endif %}
  </div>
{% endblock %}
"""

TPL_ADMIN = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card">
    <h2>Admin — Claim Legacy Rows</h2>
    <p class="muted">Rows with NULL owner_id will appear here. You can claim them.</p>
    {% if rows %}
      <form method="post" action="{{ url_for('admin_claim') }}">
        <table>
          <thead><tr><th>ID</th><th>Date</th><th>AM</th><th>PM</th><th>Cow</th><th>Tags</th><th>Notes</th><th>Owner</th><th>Claim?</th></tr></thead>
          <tbody>
            {% for r in rows %}
            <tr>
              <td>{{ r.id }}</td>
              <td>{{ r.day }}</td>
              <td>{{ r.am_litres }}</td>
              <td>{{ r.pm_litres }}</td>
              <td>{{ r.cow or '' }}</td>
              <td>{{ r.tags or '' }}</td>
              <td class="muted">{{ r.notes or '' }}</td>
              <td>{{ r.owner_id if r.owner_id is not none else 'NULL' }}</td>
              <td><input type="checkbox" name="claim_ids" value="{{ r.id }}"></td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        <p><button class="btn" type="submit">Claim Selected</button></p>
      </form>
    {% else %}
      <p class="muted">No legacy rows to claim.</p>
    {% endif %}
  </div>
{% endblock %}
"""

# Wire base layout for string templates
app.jinja_loader.mapping = {"base.html": TPL_BASE}

# -----------------------------------------------------------------------------
# Auth routes
# -----------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required.", "err")
            return render_template_string(TPL_REGISTER)

        existing = query_one("SELECT 1 FROM users WHERE email=?", (email,))
        if existing:
            flash("Email already registered.", "err")
            return render_template_string(TPL_REGISTER)

        count_row = query_one("SELECT COUNT(*) AS c FROM users")
        is_admin = 1 if (count_row and count_row["c"] == 0) else 0
        exec_sql(
            "INSERT INTO users(email, password_hash, role, unit_pref, is_admin) VALUES(?,?,?,?,?)",
            (email, generate_password_hash(password), "user", "L", is_admin)
        )
        user_row = query_one("SELECT id, email, role, unit_pref, is_admin FROM users WHERE email=?", (email,))
        login_user(User.from_row(user_row))
        flash("Welcome to MilkLog!", "ok")
        return redirect(url_for("index"))
    return render_template_string(TPL_REGISTER)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        row = query_one("SELECT id, email, password_hash, role, unit_pref, is_admin FROM users WHERE email=?", (email,))
        if row and check_password_hash(row["password_hash"], password):
            user = User(
                id=row["id"], email=row["email"],
                role=row["role"], unit_pref=row["unit_pref"], is_admin=bool(row["is_admin"])
            )
            login_user(user)
            flash("Logged in.", "ok")
            return redirect(url_for("index"))
        flash("Invalid credentials.", "err")
    return render_template_string(TPL_LOGIN)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Logged out.", "ok")
    return redirect(url_for("login"))

# -----------------------------------------------------------------------------
# App routes
# -----------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    rows = query_all("""
        SELECT id, day, am_litres, pm_litres, cow, tags, notes
        FROM milk
        WHERE deleted=0 AND owner_id=?
        ORDER BY day DESC, id DESC
        LIMIT 200
    """, (current_user.id,))
    ctx: Dict[str, Any] = {"rows": rows, "today": date.today().isoformat()}
    return render_template_string(TPL_HOME, **ctx)

@app.route("/add", methods=["POST"])
@login_required
def add_milk():
    day_str = (request.form.get("day") or date.today().isoformat()).strip()
    try:
        _ = datetime.strptime(day_str, "%Y-%m-%d").date()
    except Exception:
        flash("Invalid date.", "err")
        return redirect(url_for("index"))

    def to_float(x, default=0.0):
        try: return float(x)
        except Exception: return default

    am = to_float(request.form.get("am_litres", "0"))
    pm = to_float(request.form.get("pm_litres", "0"))
    cow = (request.form.get("cow") or "").strip()
    tags = (request.form.get("tags") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    exec_sql("""
        INSERT INTO milk(owner_id, day, am_litres, pm_litres, cow, tags, notes, updated_at)
        VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
    """, (current_user.id, day_str, am, pm, cow, tags, notes))
    flash("Saved.", "ok")
    return redirect(url_for("index"))

@app.route("/delete/<int:mid>", methods=["POST"])
@login_required
def delete_milk(mid: int):
    row = query_one("SELECT id FROM milk WHERE id=? AND owner_id=? AND deleted=0", (mid, current_user.id))
    if not row:
        flash("Not found.", "err")
        return redirect(url_for("index"))
    exec_sql("UPDATE milk SET deleted=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (mid,))
    flash("Deleted.", "ok")
    return redirect(url_for("index"))

@app.route("/pivot")
@login_required
def pivot():
    rows = query_all("""
        SELECT day,
               SUM(am_litres) AS am_sum,
               SUM(pm_litres) AS pm_sum
        FROM milk
        WHERE deleted=0 AND owner_id=?
        GROUP BY day
        ORDER BY day DESC
        LIMIT 365
    """, (current_user.id,))
    return render_template_string(TPL_PIVOT, rows=rows)

@app.route("/export.csv")
@login_required
def export_csv():
    def generate():
        yield "id,day,am_litres,pm_litres,cow,tags,notes,created_at,updated_at\n"
        with closing(get_db()) as conn:
            cur = conn.execute("""
                SELECT id, day, am_litres, pm_litres, cow, tags, notes, created_at, updated_at
                FROM milk
                WHERE deleted=0 AND owner_id=?
                ORDER BY day ASC, id ASC
            """, (current_user.id,))
            for r in cur:
                row = [
                    r["id"],
                    r["day"],
                    f"{r['am_litres']:.2f}",
                    f"{r['pm_litres']:.2f}",
                    (r["cow"] or "").replace(",", " "),
                    (r["tags"] or "").replace(",", " "),
                    (r["notes"] or "").replace("\n", " ").replace(",", " "),
                    r["created_at"],
                    r["updated_at"] or "",
                ]
                yield ",".join(map(str, row)) + "\n"
    headers = {"Content-Disposition": f'attachment; filename="milk_export_{date.today().isoformat()}.csv"'}
    return Response(generate(), mimetype="text/csv", headers=headers)

# -----------------------------------------------------------------------------
# Admin — claim legacy rows
# -----------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin():
    if not getattr(current_user, "is_admin", False):
        flash("Admin only.", "err")
        return redirect(url_for("index"))
    rows = query_all("""
        SELECT *
        FROM milk
        WHERE owner_id IS NULL OR owner_id=0
        ORDER BY created_at DESC
        LIMIT 500
    """)
    return render_template_string(TPL_ADMIN, rows=rows)

@app.route("/admin/claim", methods=["POST"])
@login_required
def admin_claim():
    if not getattr(current_user, "is_admin", False):
        flash("Admin only.", "err")
        return redirect(url_for("index"))
    ids = [x for x in request.form.getlist("claim_ids") if x.isdigit()]
    if not ids:
        flash("Nothing selected.", "err")
        return redirect(url_for("admin"))
    qmarks = ",".join("?" for _ in ids)
    with closing(get_db()) as conn, conn:
        conn.execute(
            f"UPDATE milk SET owner_id=?, updated_at=CURRENT_TIMESTAMP WHERE id IN ({qmarks})",
            (current_user.id, *map(int, ids))
        )
    flash(f"Claimed {len(ids)} rows.", "ok")
    return redirect(url_for("admin"))

# -----------------------------------------------------------------------------
# PWA / health
# -----------------------------------------------------------------------------
@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "MilkLog",
        "short_name": "MilkLog",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b1220",
        "theme_color": "#0f172a",
        "icons": [
            {"src": "/static/icon-192.png", "type": "image/png", "sizes": "192x192"},
            {"src": "/static/icon-512.png", "type": "image/png", "sizes": "512x512"}
        ]
    })

@app.route("/sw.js")
def service_worker():
    resp = Response(
        """
self.addEventListener('install', event => { self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(clients.claim()); });
self.addEventListener('fetch', event => {
  event.respondWith(fetch(event.request).catch(() => new Response('', {status: 200})));
});
        """,
        mimetype="application/javascript",
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/healthz")
def healthz():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

# -----------------------------------------------------------------------------
# WSGI entry
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
