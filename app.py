# app.py — Milk Log v5: Google Sign-In + Edit + Dashboard + Cow Management
import os
import csv
import base64
import hashlib
import secrets
import sqlite3
import requests
from contextlib import closing
from datetime import datetime, date, timedelta
from typing import Iterable, Tuple, Any, Optional, Dict
from urllib.parse import urlencode

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    Response, flash, jsonify, session
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from jinja2 import DictLoader

# -----------------------------------------------------------------------------
# App / Config
# -----------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DB_PATH = os.environ.get("DATABASE_PATH", "milklog.db")

# Google OAuth / OIDC
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")  # e.g. https://milklog.onrender.com/auth/google/callback

# Google endpoints
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

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

        # users table (+ google fields)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            unit_pref TEXT NOT NULL DEFAULT 'L',
            is_admin INTEGER NOT NULL DEFAULT 0,
            google_sub TEXT,
            name TEXT,
            picture TEXT,
            last_login TIMESTAMP,
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
        if "google_sub" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT;")
        if "name" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT;")
        if "picture" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN picture TEXT;")
        if "last_login" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN last_login TIMESTAMP;")
        if "password_hash" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT;")

        # cows table (NEW)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            tag TEXT,
            breed TEXT,
            birth_date DATE,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_owner_active ON cows(owner_id, active);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_owner_name ON cows(owner_id, name);")

        # milk table (+ cow_id migration)
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
        mcols = table_columns(conn, "milk")
        if "cow_id" not in mcols:
            conn.execute("ALTER TABLE milk ADD COLUMN cow_id INTEGER;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_owner_day ON milk(owner_id, day);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_deleted ON milk(deleted);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_cow_id ON milk(cow_id);")

# Call at import so workers are ready
init_db()

# -----------------------------------------------------------------------------
# Templates (base + pages)
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
    .wrap { max-width: 1100px; margin: 0 auto; padding: 1rem; }
    .nav { display:flex; gap:1rem; align-items:center; flex-wrap:wrap;}
    .nav .grow { flex:1; }
    .btn { background:#111827; border:1px solid #374151; padding:0.45rem 0.8rem; border-radius:10px; color:#e5e7eb; cursor:pointer;}
    .btn:hover { background:#0f172a; }
    .btn-google { background:#1f2937; border:1px solid #374151; padding:0.55rem 0.9rem; border-radius:10px; display:inline-flex; align-items:center; gap:.5rem;}
    .card { background:#0b1324; border:1px solid #1f2937; border-radius:14px; padding:1rem; }
    input, select, textarea { width:100%; background:#0b1220; color:#e5e7eb; border:1px solid #334155; border-radius:10px; padding:0.5rem;}
    table { width:100%; border-collapse: collapse; }
    th, td { text-align:left; padding:0.5rem; border-bottom:1px solid #1f2937; vertical-align:top;}
    .grid { display:grid; gap:1rem; }
    .grid-2 { grid-template-columns: 1fr 1fr; }
    .grid-3 { grid-template-columns: 1fr 1fr 1fr; }
    .muted { color:#94a3b8; }
    .danger { color:#fda4af; }
    .success { color:#86efac; }
    .tag { display:inline-block; padding:0.15rem 0.5rem; border:1px solid #334155; border-radius:999px; margin-right:0.25rem; font-size:0.8rem; color:#93c5fd;}
    footer { color:#94a3b8; font-size:0.9rem; padding:2rem 0; text-align:center;}
    .flash { padding:0.5rem 0.75rem; border-radius:10px; margin: 0.25rem 0; }
    .flash-ok { background:#052e16; border:1px solid #064e3b;}
    .flash-err { background:#3f1d1d; border:1px solid #7f1d1d;}
    .row-actions { display:flex; gap:0.5rem; }
    .center { text-align:center; }
  </style>
</head>
<body>
  <header>
    <div class="wrap nav">
      <div><strong>🥛 MilkLog</strong></div>
      <a class="btn" href="{{ url_for('index') }}">Home</a>
      <a class="btn" href="{{ url_for('dashboard') }}">Dashboard</a>
      <a class="btn" href="{{ url_for('pivot') }}">Pivot</a>
      <a class="btn" href="{{ url_for('cows') }}">Cows</a>
      <a class="btn" href="{{ url_for('export_csv') }}">Export CSV</a>
      <div class="grow"></div>
      {% if current_user.is_authenticated %}
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
      <h2>Add Milk</h2>
      <form method="post" action="{{ url_for('add_milk') }}" class="grid grid-3">
        <div>
          <label>Date</label>
          <input type="date" name="day" value="{{ today }}">
        </div>
        <div>
          <label>Cow</label>
          <select name="cow_id">
            <option value="">— None —</option>
            {% for c in cows %}
              <option value="{{ c.id }}">{{ c.name }}{% if c.tag %} ({{ c.tag }}){% endif %}</option>
            {% endfor %}
          </select>
          <div class="muted" style="margin-top:.25rem;">
            <a href="{{ url_for('cows') }}">Manage cows</a>
          </div>
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
            <td>
              {% if r.cow_name %}
                <a href="{{ url_for('cow_dashboard', cid=r.cow_id) }}">{{ r.cow_name }}</a>
                {% if r.cow_tag %}<span class="muted">({{ r.cow_tag }})</span>{% endif %}
              {% else %}
                {{ r.cow or '' }}
              {% endif %}
            </td>
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
              <a class="btn" href="{{ url_for('edit_milk', mid=r.id) }}">Edit</a>
              <form method="post" action="{{ url_for('delete_milk', mid=r.id) }}" style="display:inline;">
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

TPL_EDIT = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card" style="max-width:700px;">
    <h2>Edit Entry</h2>
    <form method="post" class="grid grid-3">
      <div>
        <label>Date</label>
        <input type="date" name="day" value="{{ row.day }}">
      </div>
      <div>
        <label>Cow</label>
        <select name="cow_id">
          <option value="">— None —</option>
          {% for c in cows %}
            <option value="{{ c.id }}" {% if row.cow_id == c.id %}selected{% endif %}>
              {{ c.name }}{% if c.tag %} ({{ c.tag }}){% endif %}
            </option>
          {% endfor %}
        </select>
      </div>
      <div>
        <label>AM litres</label>
        <input type="number" step="0.01" name="am_litres" value="{{ row.am_litres }}">
      </div>
      <div>
        <label>PM litres</label>
        <input type="number" step="0.01" name="pm_litres" value="{{ row.pm_litres }}">
      </div>
      <div>
        <label>Tags</label>
        <input name="tags" value="{{ row.tags or '' }}">
      </div>
      <div>
        <label>Notes</label>
        <input name="notes" value="{{ row.notes or '' }}">
      </div>
      <div>
        <button class="btn" type="submit">Save Changes</button>
        <a class="btn" href="{{ url_for('index') }}">Cancel</a>
      </div>
    </form>
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
        <input name="email" type="email" autocomplete="username" required>
      </div>
      <div>
        <label>Password</label>
        <input name="password" type="password" autocomplete="current-password" required>
      </div>
      <div>
        <button class="btn" type="submit">Login</button>
      </div>
    </form>
    <div class="center" style="margin-top:0.75rem;">
      <a class="btn-google" href="{{ url_for('google_login') }}">
        <img alt="" src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" style="height:18px;width:18px;">
        <span>Continue with Google</span>
      </a>
    </div>
    <p class="muted center">No account? <a href="{{ url_for('register') }}">Register</a></p>
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
        <input name="email" type="email" autocomplete="username" required>
      </div>
      <div>
        <label>Password</label>
        <input name="password" type="password" autocomplete="new-password" required>
      </div>
      <div>
        <button class="btn" type="submit">Register</button>
      </div>
    </form>
    <div class="center" style="margin-top:0.75rem;">
      <a class="btn-google" href="{{ url_for('google_login') }}">
        <img alt="" src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" style="height:18px;width:18px;">
        <span>Sign up with Google</span>
      </a>
    </div>
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

TPL_DASHBOARD = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card">
    <h2>Dashboard — 90 Day Trend</h2>
    {% if labels|length == 0 %}
      <p class="muted">No data yet.</p>
    {% else %}
      <canvas id="milkChart" width="900" height="400"></canvas>
      <p class="muted">Totals are AM+PM per day. Use Pivot for table view; Export CSV for spreadsheets.</p>
      <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
      <script>
        const labels = {{ labels|tojson }};
        const amData = {{ am|tojson }};
        const pmData = {{ pm|tojson }};
        const totalData = {{ total|tojson }};
        const ctx = document.getElementById('milkChart').getContext('2d');
        new Chart(ctx, {
          type: 'line',
          data: {
            labels: labels,
            datasets: [
              { label: 'AM', data: amData, tension: 0.25 },
              { label: 'PM', data: pmData, tension: 0.25 },
              { label: 'Total', data: totalData, tension: 0.25 }
            ]
          },
          options: {
            responsive: true,
            interaction: { mode: 'index', intersect: false },
            scales: {
              y: { beginAtZero: true, title: { display: true, text: 'Litres' } },
              x: { title: { display: true, text: 'Date' } }
            }
          }
        });
      </script>
    {% endif %}
  </div>
{% endblock %}
"""

TPL_COWS = r"""
{% extends "base.html" %}
{% block body %}
  <div class="grid" style="gap:1.5rem;">
    <div class="card">
      <h2>Your Cows</h2>
      <form method="get" action="{{ url_for('cows') }}" class="grid" style="grid-template-columns: 1fr auto;">
        <input name="q" placeholder="Search by name or tag" value="{{ q or '' }}">
        <button class="btn" type="submit">Search</button>
      </form>
      <p class="muted" style="margin:.5rem 0;"><a class="btn" href="{{ url_for('cow_new') }}">+ Add Cow</a></p>
      {% if rows %}
        <table>
          <thead><tr><th>Name</th><th>Tag</th><th>Breed</th><th>Birth</th><th>Active</th><th></th></tr></thead>
          <tbody>
            {% for c in rows %}
            <tr>
              <td><a href="{{ url_for('cow_dashboard', cid=c.id) }}">{{ c.name }}</a></td>
              <td>{{ c.tag or '' }}</td>
              <td>{{ c.breed or '' }}</td>
              <td>{{ c.birth_date or '' }}</td>
              <td>{{ 'Yes' if c.active else 'No' }}</td>
              <td class="row-actions">
                <a class="btn" href="{{ url_for('cow_edit', cid=c.id) }}">Edit</a>
                {% if c.active %}
                  <form method="post" action="{{ url_for('cow_archive', cid=c.id) }}" style="display:inline;">
                    <button class="btn danger" type="submit" onclick="return confirm('Archive this cow?');">Archive</button>
                  </form>
                {% else %}
                  <form method="post" action="{{ url_for('cow_unarchive', cid=c.id) }}" style="display:inline;">
                    <button class="btn" type="submit">Unarchive</button>
                  </form>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="muted">No cows yet. <a href="{{ url_for('cow_new') }}">Add your first.</a></p>
      {% endif %}
    </div>
  </div>
{% endblock %}
"""

TPL_COW_FORM = r"""
{% extends "base.html" %}
{% block body %}
  <div class="card" style="max-width:720px;">
    <h2>{{ 'Edit Cow' if cow else 'Add Cow' }}</h2>
    <form method="post" class="grid grid-2">
      <div><label>Name</label><input name="name" value="{{ cow.name if cow else '' }}" required></div>
      <div><label>Tag</label><input name="tag" value="{{ cow.tag if cow else '' }}"></div>
      <div><label>Breed</label><input name="breed" value="{{ cow.breed if cow else '' }}"></div>
      <div><label>Birth Date</label><input type="date" name="birth_date" value="{{ cow.birth_date if cow else '' }}"></div>
      <div class="grid" style="grid-template-columns: 1fr;">
        <label>Notes</label>
        <textarea name="notes" rows="3">{{ cow.notes if cow else '' }}</textarea>
      </div>
      <div>
        <button class="btn" type="submit">Save</button>
        <a class="btn" href="{{ url_for('cows') }}">Cancel</a>
      </div>
    </form>
  </div>
{% endblock %}
"""

TPL_COW_DASH = r"""
{% extends "base.html" %}
{% block body %}
  <div class="grid" style="gap:1.5rem;">
    <div class="card">
      <h2>Cow: {{ cow.name }} {% if cow.tag %}<span class="muted">({{ cow.tag }})</span>{% endif %}</h2>
      <p class="muted">Breed: {{ cow.breed or '—' }} • Birth: {{ cow.birth_date or '—' }} • Active: {{ 'Yes' if cow.active else 'No' }}</p>
      <p class="muted"><a class="btn" href="{{ url_for('cow_edit', cid=cow.id) }}">Edit Cow</a></p>
    </div>

    <div class="card">
      <h3>90 Day Trend</h3>
      {% if labels|length == 0 %}
        <p class="muted">No milk data for this cow.</p>
      {% else %}
        <canvas id="cowChart" width="900" height="350"></canvas>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
        <script>
          const labels = {{ labels|tojson }};
          const amData = {{ am|tojson }};
          const pmData = {{ pm|tojson }};
          const totalData = {{ total|tojson }};
          const ctx = document.getElementById('cowChart').getContext('2d');
          new Chart(ctx, {
            type: 'line',
            data: {
              labels: labels,
              datasets: [
                { label: 'AM', data: amData, tension: 0.25 },
                { label: 'PM', data: pmData, tension: 0.25 },
                { label: 'Total', data: totalData, tension: 0.25 }
              ]
            },
            options: {
              responsive: true,
              interaction: { mode: 'index', intersect: false },
              scales: {
                y: { beginAtZero: true, title: { display: true, text: 'Litres' } },
                x: { title: { display: true, text: 'Date' } }
              }
            }
          });
        </script>
      {% endif %}
    </div>

    <div class="card">
      <h3>Recent Entries</h3>
      {% if recent %}
        <table>
          <thead><tr><th>Date</th><th>AM</th><th>PM</th><th>Total</th><th>Notes</th><th></th></tr></thead>
          <tbody>
            {% for r in recent %}
            <tr>
              <td>{{ r.day }}</td>
              <td>{{ "%.2f"|format(r.am_litres) }}</td>
              <td>{{ "%.2f"|format(r.pm_litres) }}</td>
              <td>{{ "%.2f"|format(r.am_litres + r.pm_litres) }}</td>
              <td class="muted">{{ r.notes or '' }}</td>
              <td class="row-actions">
                <a class="btn" href="{{ url_for('edit_milk', mid=r.id) }}">Edit</a>
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p class="muted">No entries yet.</p>
      {% endif %}
    </div>
  </div>
{% endblock %}
"""

# Proper Jinja loader
app.jinja_loader = DictLoader({"base.html": TPL_BASE})

# -----------------------------------------------------------------------------
# User model / loader
# -----------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id: int, email: str, role: str = "user",
                 unit_pref: str = "L", is_admin: bool = False,
                 name: str = "", picture: str = ""):
        self.id = id
        self.email = email
        self.role = role
        self.unit_pref = unit_pref
        self.is_admin = bool(is_admin)
        self.name = name
        self.picture = picture

    @staticmethod
    def from_row(r: sqlite3.Row) -> "User":
        return User(
            id=r["id"],
            email=r["email"],
            role=r["role"] if "role" in r.keys() else "user",
            unit_pref=r["unit_pref"] if "unit_pref" in r.keys() else "L",
            is_admin=bool(r["is_admin"]) if "is_admin" in r.keys() else False,
            name=r["name"] if "name" in r.keys() and r["name"] else "",
            picture=r["picture"] if "picture" in r.keys() and r["picture"] else "",
        )

@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    try:
        r = query_one(
            "SELECT id, email, role, unit_pref, is_admin, name, picture FROM users WHERE id=?",
            (user_id,)
        )
    except sqlite3.OperationalError:
        init_db()
        r = None
    return User.from_row(r) if r else None

# -----------------------------------------------------------------------------
# Auth routes — Email/Password
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
            "INSERT INTO users(email, password_hash, role, unit_pref, is_admin, last_login) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
            (email, generate_password_hash(password), "user", "L", is_admin)
        )
        user_row = query_one("SELECT id, email, role, unit_pref, is_admin, name, picture FROM users WHERE email=?", (email,))
        login_user(User.from_row(user_row))
        flash("Welcome to MilkLog!", "ok")
        return redirect(url_for("index"))
    return render_template_string(TPL_REGISTER)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        row = query_one("SELECT id, email, password_hash, role, unit_pref, is_admin, name, picture FROM users WHERE email=?", (email,))
        if row and row["password_hash"] and check_password_hash(row["password_hash"], password):
            exec_sql("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
            login_user(User.from_row(row))
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
# Auth routes — Google OIDC
# -----------------------------------------------------------------------------
def _require_google_env() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI)

@app.route("/auth/google")
def google_login():
    if not _require_google_env():
        flash("Google Sign-In not configured.", "err")
        return redirect(url_for("login"))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    code_verifier = secrets.token_urlsafe(64)
    session["code_verifier"] = code_verifier
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent"
    }
    return redirect(f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}")

@app.route("/auth/google/callback")
def google_callback():
    if not _require_google_env():
        flash("Google Sign-In not configured.", "err")
        return redirect(url_for("login"))

    state = request.args.get("state", "")
    if not state or state != session.get("oauth_state"):
        flash("OAuth state mismatch.", "err")
        return redirect(url_for("login"))

    code = request.args.get("code", "")
    if not code:
        flash("Missing authorization code.", "err")
        return redirect(url_for("login"))

    code_verifier = session.get("code_verifier", "")
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    try:
        tok = requests.post(GOOGLE_TOKEN_ENDPOINT, data=data, timeout=10)
        tok.raise_for_status()
        token_json = tok.json()
    except Exception:
        flash("Token exchange failed.", "err")
        return redirect(url_for("login"))

    access_token = token_json.get("access_token")
    if not access_token:
        flash("No access token from Google.", "err")
        return redirect(url_for("login"))

    try:
        userinfo = requests.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        ).json()
    except Exception:
        flash("Failed to fetch user info.", "err")
        return redirect(url_for("login"))

    sub = userinfo.get("sub")
    email = (userinfo.get("email") or "").lower()
    name = userinfo.get("name") or ""
    picture = userinfo.get("picture") or ""

    if not sub or not email:
        flash("Google account missing email or subject.", "err")
        return redirect(url_for("login"))

    row = query_one("SELECT * FROM users WHERE google_sub=?", (sub,))
    if not row:
        row = query_one("SELECT * FROM users WHERE email=?", (email,))
        if row:
            exec_sql(
                "UPDATE users SET google_sub=?, name=?, picture=?, last_login=CURRENT_TIMESTAMP WHERE id=?",
                (sub, name, picture, row["id"])
            )
        else:
            count_row = query_one("SELECT COUNT(*) AS c FROM users")
            is_admin = 1 if (count_row and count_row["c"] == 0) else 0
            exec_sql(
                "INSERT INTO users(email, google_sub, name, picture, role, unit_pref, is_admin, last_login) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (email, sub, name, picture, "user", "L", is_admin)
            )
            row = query_one("SELECT * FROM users WHERE email=?", (email,))
    else:
        exec_sql("UPDATE users SET name=?, picture=?, last_login=CURRENT_TIMESTAMP WHERE id=?", (name, picture, row["id"]))

    r = query_one("SELECT id, email, role, unit_pref, is_admin, name, picture FROM users WHERE id=?", (row["id"],))
    login_user(User.from_row(r))
    flash("Signed in with Google.", "ok")
    return redirect(url_for("index"))

# -----------------------------------------------------------------------------
# App routes — Home / Milk
# -----------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    cows = query_all("SELECT id, name, tag FROM cows WHERE owner_id=? AND active=1 ORDER BY name ASC", (current_user.id,))
    rows = query_all("""
        SELECT m.id, m.day, m.am_litres, m.pm_litres, m.cow, m.tags, m.notes, m.cow_id,
               c.name as cow_name, c.tag as cow_tag
          FROM milk m
          LEFT JOIN cows c ON c.id = m.cow_id
         WHERE m.deleted=0 AND m.owner_id=?
         ORDER BY m.day DESC, m.id DESC
         LIMIT 200
    """, (current_user.id,))
    ctx: Dict[str, Any] = {"rows": rows, "today": date.today().isoformat(), "cows": cows}
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
    cow_id = request.form.get("cow_id")
    cow_id_val = int(cow_id) if cow_id and cow_id.isdigit() else None
    tags = (request.form.get("tags") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    exec_sql("""
        INSERT INTO milk(owner_id, day, am_litres, pm_litres, cow_id, tags, notes, updated_at)
        VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
    """, (current_user.id, day_str, am, pm, cow_id_val, tags, notes))
    flash("Saved.", "ok")
    return redirect(url_for("index"))

@app.route("/edit/<int:mid>", methods=["GET", "POST"])
@login_required
def edit_milk(mid: int):
    row = query_one("SELECT * FROM milk WHERE id=? AND owner_id=? AND deleted=0", (mid, current_user.id))
    if not row:
        flash("Not found.", "err")
        return redirect(url_for("index"))
    cows = query_all("SELECT id, name, tag FROM cows WHERE owner_id=? ORDER BY active DESC, name ASC", (current_user.id,))

    if request.method == "POST":
        day = (request.form.get("day") or row["day"]).strip()
        try:
            _ = datetime.strptime(day, "%Y-%m-%d").date()
        except Exception:
            day = row["day"]

        def to_float(x, default=0.0):
            try: return float(x)
            except Exception: return default

        am = to_float(request.form.get("am_litres", row["am_litres"]), row["am_litres"])
        pm = to_float(request.form.get("pm_litres", row["pm_litres"]), row["pm_litres"])
        cow_id = request.form.get("cow_id")
        cow_id_val = int(cow_id) if cow_id and cow_id.isdigit() else None
        tags = (request.form.get("tags") or row["tags"] or "").strip()
        notes = (request.form.get("notes") or row["notes"] or "").strip()

        exec_sql("""
            UPDATE milk
               SET day=?, am_litres=?, pm_litres=?, cow_id=?, tags=?, notes=?, updated_at=CURRENT_TIMESTAMP
             WHERE id=? AND owner_id=?
        """, (day, am, pm, cow_id_val, tags, notes, mid, current_user.id))
        flash("Updated.", "ok")
        return redirect(url_for("index"))

    return render_template_string(TPL_EDIT, row=row, cows=cows)

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

# -----------------------------------------------------------------------------
# Pivot & Global Dashboard
# -----------------------------------------------------------------------------
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

@app.route("/dashboard")
@login_required
def dashboard():
    since = (date.today() - timedelta(days=89)).isoformat()
    rows = query_all("""
        SELECT day,
               SUM(am_litres) AS am_sum,
               SUM(pm_litres) AS pm_sum
          FROM milk
         WHERE deleted=0 AND owner_id=? AND day>=?
         GROUP BY day
         ORDER BY day ASC
    """, (current_user.id, since))
    labels = [r["day"] for r in rows]
    am = [round(r["am_sum"] or 0, 2) for r in rows]
    pm = [round(r["pm_sum"] or 0, 2) for r in rows]
    total = [round((r["am_sum"] or 0) + (r["pm_sum"] or 0), 2) for r in rows]
    return render_template_string(TPL_DASHBOARD, labels=labels, am=am, pm=pm, total=total)

# -----------------------------------------------------------------------------
# Cow Management
# -----------------------------------------------------------------------------
@app.route("/cows")
@login_required
def cows():
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        rows = query_all("""
            SELECT * FROM cows
             WHERE owner_id=? AND (name LIKE ? OR tag LIKE ?)
             ORDER BY active DESC, name ASC
        """, (current_user.id, like, like))
    else:
        rows = query_all("""
            SELECT * FROM cows
             WHERE owner_id=?
             ORDER BY active DESC, name ASC
        """, (current_user.id,))
    return render_template_string(TPL_COWS, rows=rows, q=q)

@app.route("/cows/new", methods=["GET", "POST"])
@login_required
def cow_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Name is required.", "err")
            return render_template_string(TPL_COW_FORM, cow=None)
        tag = (request.form.get("tag") or "").strip()
        breed = (request.form.get("breed") or "").strip()
        birth_date = (request.form.get("birth_date") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        exec_sql("""
            INSERT INTO cows(owner_id, name, tag, breed, birth_date, notes, active, updated_at)
            VALUES(?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
        """, (current_user.id, name, tag, breed, birth_date if birth_date else None, notes))
        flash("Cow added.", "ok")
        return redirect(url_for('cows'))
    return render_template_string(TPL_COW_FORM, cow=None)

@app.route("/cows/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def cow_edit(cid: int):
    cow = query_one("SELECT * FROM cows WHERE id=? AND owner_id=?", (cid, current_user.id))
    if not cow:
        flash("Cow not found.", "err")
        return redirect(url_for('cows'))
    if request.method == "POST":
        name = (request.form.get("name") or cow["name"]).strip()
        if not name:
            flash("Name is required.", "err")
            return render_template_string(TPL_COW_FORM, cow=cow)
        tag = (request.form.get("tag") or "").strip()
        breed = (request.form.get("breed") or "").strip()
        birth_date = (request.form.get("birth_date") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        exec_sql("""
            UPDATE cows
               SET name=?, tag=?, breed=?, birth_date=?, notes=?, updated_at=CURRENT_TIMESTAMP
             WHERE id=? AND owner_id=?
        """, (name, tag, breed, birth_date if birth_date else None, notes, cid, current_user.id))
        flash("Cow updated.", "ok")
        return redirect(url_for('cows'))
    return render_template_string(TPL_COW_FORM, cow=cow)

@app.route("/cows/<int:cid>/archive", methods=["POST"])
@login_required
def cow_archive(cid: int):
    cow = query_one("SELECT id FROM cows WHERE id=? AND owner_id=?", (cid, current_user.id))
    if not cow:
        flash("Cow not found.", "err")
        return redirect(url_for('cows'))
    exec_sql("UPDATE cows SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    flash("Cow archived.", "ok")
    return redirect(url_for('cows'))

@app.route("/cows/<int:cid>/unarchive", methods=["POST"])
@login_required
def cow_unarchive(cid: int):
    cow = query_one("SELECT id FROM cows WHERE id=? AND owner_id=?", (cid, current_user.id))
    if not cow:
        flash("Cow not found.", "err")
        return redirect(url_for('cows'))
    exec_sql("UPDATE cows SET active=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    flash("Cow unarchived.", "ok")
    return redirect(url_for('cows'))

@app.route("/cows/<int:cid>")
@login_required
def cow_dashboard(cid: int):
    cow = query_one("SELECT * FROM cows WHERE id=? AND owner_id=?", (cid, current_user.id))
    if not cow:
        flash("Cow not found.", "err")
        return redirect(url_for('cows'))

    since = (date.today() - timedelta(days=89)).isoformat()
    rows = query_all("""
        SELECT day, SUM(am_litres) AS am_sum, SUM(pm_litres) AS pm_sum
          FROM milk
         WHERE deleted=0 AND owner_id=? AND cow_id=? AND day>=?
         GROUP BY day
         ORDER BY day ASC
    """, (current_user.id, cid, since))
    labels = [r["day"] for r in rows]
    am = [round(r["am_sum"] or 0, 2) for r in rows]
    pm = [round(r["pm_sum"] or 0, 2) for r in rows]
    total = [round((r["am_sum"] or 0) + (r["pm_sum"] or 0), 2) for r in rows]

    recent = query_all("""
        SELECT id, day, am_litres, pm_litres, notes
          FROM milk
         WHERE deleted=0 AND owner_id=? AND cow_id=?
         ORDER BY day DESC, id DESC
         LIMIT 50
    """, (current_user.id, cid))

    return render_template_string(TPL_COW_DASH, cow=cow, labels=labels, am=am, pm=pm, total=total, recent=recent)

# -----------------------------------------------------------------------------
# Export
# -----------------------------------------------------------------------------
@app.route("/export.csv")
@login_required
def export_csv():
    def generate():
        yield "id,day,am_litres,pm_litres,cow_id,cow_name,tags,notes,created_at,updated_at\n"
        with closing(get_db()) as conn:
            cur = conn.execute("""
                SELECT m.id, m.day, m.am_litres, m.pm_litres, m.cow_id,
                       c.name as cow_name, m.tags, m.notes, m.created_at, m.updated_at
                  FROM milk m
             LEFT JOIN cows c ON c.id = m.cow_id
                 WHERE m.deleted=0 AND m.owner_id=?
              ORDER BY m.day ASC, m.id ASC
            """, (current_user.id,))
            for r in cur:
                row = [
                    r["id"],
                    r["day"],
                    f"{r['am_litres']:.2f}",
                    f"{r['pm_litres']:.2f}",
                    r["cow_id"] or "",
                    (r["cow_name"] or "").replace(",", " "),
                    (r["tags"] or "").replace(",", " "),
                    (r["notes"] or "").replace("\n", " ").replace(",", " "),
                    r["created_at"],
                    r["updated_at"] or "",
                ]
                yield ",".join(map(str, row)) + "\n"
    headers = {"Content-Disposition": f'attachment; filename="milk_export_{date.today().isoformat()}.csv"'}
    return Response(generate(), mimetype="text/csv", headers=headers)

# -----------------------------------------------------------------------------
# Admin — claim legacy rows (unchanged placeholder)
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
    return render_template_string(TPL_PIVOT.replace("Pivot (Daily Totals)", "Admin — Claim Legacy Rows"), rows=rows)

# -----------------------------------------------------------------------------
# PWA / health / debug
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
     {"src": "/static/icon-512.png", "type": "image/png", "sizes": "512x512"},
            {
                "src": "/static/icon-512-maskable.png",
                "type": "image/png",
                "sizes": "512x512",
                "purpose": "maskable",
            },
        ],
        "screenshots": [
            {
                "src": "/static/screens/home-portrait.png",
                "sizes": "1080x1920",
                "type": "image/png",
                "form_factor": "narrow",
                "label": "Home & recent entries",
            },
            {
                "src": "/static/screens/dashboard-portrait.png",
                "sizes": "1080x1920",
                "type": "image/png",
                "form_factor": "narrow",
                "label": "90-day dashboard",
            },
            },  
        ],
        
@app.route("/sw.js")
def service_worker():
    # No fetch interception to avoid blank-page caching
    resp = Response(
        """
self.addEventListener('install', event => { self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(clients.claim()); });
// No fetch handler
        """,
        mimetype="application/javascript",
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/healthz")
def healthz():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

@app.route("/ping")
def ping():
    return "ok"

@app.route("/__whoami")
def whoami():
    if current_user.is_authenticated:
        return {"auth": True, "email": current_user.email, "admin": bool(getattr(current_user, "is_admin", False))}
    return {"auth": False}

@app.route("/__env")
def env():
    return {"db_path": DB_PATH, "cwd": os.getcwd(), "google_configured": bool(GOOGLE_CLIENT_ID)}

# -----------------------------------------------------------------------------
# WSGI entry
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
