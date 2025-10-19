# app.py
# Milk Log — single-file Flask app
# Features in this build:
# - Email/password auth + Google Sign-In (OIDC), multi-tenant isolation, admin role
# - Add/update/delete milk records; recent table; records pivot; CSV/Excel export
# - PWA (manifest + service worker)
# - CSRF protection (lightweight) + rate limiting on auth
# - Finance: per-user price-per-litre + currency; dashboard shows Revenue today & 7-day avg revenue/day

import os, io, csv, json, time, secrets, sqlite3
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, date

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    send_file, flash, Response, session, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)

# Google OAuth
from authlib.integrations.flask_client import OAuth
import requests # not used directly, but handy for debugging provider calls

try:
    from openpyxl import Workbook
except Exception:
    Workbook = None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-please-change")

# -------- Storage paths --------
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "milk_records.db")

# -------- Auth (Flask-Login) --------
login_manager = LoginManager(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.email = row["email"]
        self.role = row["role"]
        self.unit_pref = row["unit_pref"]

    @property
    def is_admin(self):
        return self.role == "admin"

@login_manager.user_loader
def load_user(user_id):
    row = query_one("SELECT id, email, role, COALESCE(unit_pref,'L') AS unit_pref FROM users WHERE id=?", (user_id,))
    return User(row) if row else None

# -------- Google OAuth (Authlib) --------
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# -------- DB helpers --------
def connect():
    return sqlite3.connect(DB_PATH)

def query(sql, args=()):
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()

def query_one(sql, args=()):
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, args)
        return cur.fetchone()

def exec_write(sql, args=()):
    with closing(connect()) as conn, conn:
        conn.execute(sql, args)

def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

# -------- Schema & migrations --------
def init_db():
    with closing(connect()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # users (auth + finance + google fields)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('admin','user')),
          created_at TEXT NOT NULL,
          unit_pref TEXT DEFAULT 'L',
          milk_price_per_litre REAL DEFAULT 0.0,
          currency TEXT DEFAULT '€',
          google_sub TEXT UNIQUE,
          name TEXT,
          avatar TEXT
        )""")
        ucols = columns(conn, "users")
        if "unit_pref" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN unit_pref TEXT DEFAULT 'L'")
        if "milk_price_per_litre" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN milk_price_per_litre REAL DEFAULT 0.0")
        if "currency" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN currency TEXT DEFAULT '€'")
        if "google_sub" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT UNIQUE")
        if "name" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
        if "avatar" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")

        # milk_records
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
          owner_id INTEGER,
          created_at TEXT NOT NULL,
          edited_at TEXT,
          FOREIGN KEY(owner_id) REFERENCES users(id)
        )""")
        mcols = columns(conn, "milk_records")
        if "session" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN session TEXT DEFAULT 'AM'")
        if "note" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN note TEXT")
        if "tags" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN tags TEXT")
        if "deleted" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN deleted INTEGER DEFAULT 0")
        if "owner_id" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN owner_id INTEGER")
        if "edited_at" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN edited_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_date ON milk_records(record_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_cow ON milk_records(cow_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_sess ON milk_records(session)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_del ON milk_records(deleted)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_owner ON milk_records(owner_id)")

        # cows (kept for compatibility)
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
          owner_id INTEGER,
          created_at TEXT,
          edited_at TEXT,
          FOREIGN KEY(owner_id) REFERENCES users(id)
        )""")
        ccols = columns(conn, "cows")
        if "name" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN name TEXT")
        if "breed" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN breed TEXT")
        if "parity" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN parity INTEGER")
        if "dob" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN dob TEXT")
        if "latest_calving" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN latest_calving TEXT")
        if "group_name" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN group_name TEXT")
        if "owner_id" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN owner_id INTEGER")
        if "created_at" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN created_at TEXT")
        if "edited_at" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN edited_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_tag ON cows(tag)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_group ON cows(group_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_owner ON cows(owner_id)")

init_db()

# -------- Utilities --------
def today_str():
    return date.today().isoformat()

def current_owner_id():
    return current_user.id if current_user.is_authenticated else None

def unit_pref_for():
    if current_user.is_authenticated:
        r = query_one("SELECT COALESCE(unit_pref,'L') AS u FROM users WHERE id=?", (current_user.id,))
        return r["u"] if r else "L"
    return "L"

def to_display_litres(litres):
    if unit_pref_for() == "gal":
        return round(litres * 0.264172, 2), "gal"
    return round(litres, 2), "L"

def finance_prefs():
    if not current_user.is_authenticated:
        return {"price": 0.0, "ccy": "€"}
    r = query_one("SELECT COALESCE(milk_price_per_litre,0.0) AS price, COALESCE(currency,'€') AS ccy FROM users WHERE id=?",
                  (current_user.id,))
    return {"price": float(r["price"] or 0.0), "ccy": r["ccy"] or "€"}

def kpis_for_home(owner_id):
    t = today_str()
    row = query("""
      SELECT COALESCE(SUM(litres),0) AS tot
      FROM milk_records
      WHERE deleted=0 AND record_date=? AND owner_id=?
    """, (t, owner_id))
    tot_l = float(row[0]["tot"]) if row else 0.0

    cows = query("""
      SELECT COUNT(DISTINCT cow_number) AS n
      FROM milk_records
      WHERE deleted=0 AND record_date=? AND owner_id=?
    """, (t, owner_id))
    n_cows = int(cows[0]["n"]) if cows else 0

    am = query("""
      SELECT COUNT(DISTINCT cow_number) AS n
      FROM milk_records
      WHERE deleted=0 AND record_date=? AND session='AM' AND owner_id=?
    """, (t, owner_id))
    pm = query("""
      SELECT COUNT(DISTINCT cow_number) AS n
      FROM milk_records
      WHERE deleted=0 AND record_date=? AND session='PM' AND owner_id=?
    """, (t, owner_id))
    am_n = int(am[0]["n"]) if am else 0
    pm_n = int(pm[0]["n"]) if pm else 0

    mlk_per_cow = round(tot_l / n_cows, 2) if n_cows else 0.0

    fp = finance_prefs()
    revenue_today = round(tot_l * fp["price"], 2)

    # 7-day average revenue/day (only days with any milk)
    days = query("""
      SELECT record_date, SUM(litres) AS litres
      FROM milk_records
      WHERE deleted=0 AND owner_id=? AND record_date BETWEEN date(?,'-6 day') AND ?
      GROUP BY record_date ORDER BY record_date ASC
    """, (owner_id, t, t))
    rev_list = [float(d["litres"] or 0.0) * fp["price"] for d in days if float(d["litres"] or 0.0) > 0]
    avg7_rev = round(sum(rev_list) / len(rev_list), 2) if rev_list else 0.0

    return {
        "tot_litres": round(tot_l, 2),
        "cows_recorded": n_cows,
        "milk_per_cow": round(mlk_per_cow, 2),
        "am_coverage": am_n,
        "pm_coverage": pm_n,
        "currency": fp["ccy"],
        "revenue_today": revenue_today,
        "avg7_revenue_day": avg7_rev
    }

# -------- CSRF + throttling --------
def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]

def require_csrf():
    token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    if not token or token != session.get("csrf_token"):
        abort(400, description="CSRF token invalid")

_auth_hits = defaultdict(lambda: deque(maxlen=20))
def throttle(ip, limit, per_seconds):
    now = time.time()
    q = _auth_hits[ip]
    while q and now - q[0] > per_seconds:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now); return False

@app.before_request
def guards():
    if request.endpoint in {"login", "register"} and request.method == "POST":
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "x").split(",")[0].strip()
        if throttle(ip, limit=10, per_seconds=300):
            abort(429, description="Too many attempts. Please wait a moment.")
    if request.method in ("POST","PUT","PATCH","DELETE"):
        if request.endpoint in {"service_worker", "manifest", "healthz"}:
            return
        require_csrf()

# -------- Email/Password Auth --------
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        row = query_one("SELECT id, email, password_hash, role, COALESCE(unit_pref,'L') AS unit_pref FROM users WHERE email=?", (email,))
        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row))
            return redirect(url_for("home"))
        flash("Invalid email or password", "error")
    return render_template_string(TPL_LOGIN, base_css=BASE_CSS, get_csrf_token=get_csrf_token)

@app.route("/register", methods=["GET","POST"])
def register():
    any_user = query_one("SELECT id FROM users LIMIT 1")
    default_role = "admin" if not any_user else "user"
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Email and password required", "error")
            return redirect(url_for("register"))
        try:
            exec_write("""
              INSERT INTO users (email, password_hash, role, created_at)
              VALUES (?, ?, ?, ?)
            """, (email, generate_password_hash(password), default_role, datetime.utcnow().isoformat()))
            row = query_one("SELECT id, email, role, COALESCE(unit_pref,'L') AS unit_pref FROM users WHERE email=?", (email,))
            login_user(User(row))
            flash("Account created.", "ok")
            return redirect(url_for("home"))
        except Exception as e:
            flash(f"Registration failed: {e}", "error")
            return redirect(url_for("register"))
    return render_template_string(TPL_REGISTER, base_css=BASE_CSS, default_role=default_role, get_csrf_token=get_csrf_token)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# -------- Google Sign-In routes --------
@app.route("/login/google")
def login_google():
    redirect_uri = os.getenv("OAUTH_REDIRECT_URI") or url_for("auth_google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def auth_google_callback():
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            resp = oauth.google.get("userinfo")
            userinfo = resp.json()
        g_sub = userinfo.get("sub")
        email = (userinfo.get("email") or "").lower()
        name = userinfo.get("name") or ""
        picture = userinfo.get("picture") or ""

        if not g_sub or not email:
            flash("Google login failed: missing identity claims.", "error")
            return redirect(url_for("login"))

        # Prefer linking by Google subject
        row = query_one("""
            SELECT id, email, role, COALESCE(unit_pref,'L') AS unit_pref
            FROM users WHERE google_sub=?
        """, (g_sub,))

        if not row:
            # If account exists by email, link it
            row = query_one("""
                SELECT id, email, role, COALESCE(unit_pref,'L') AS unit_pref
                FROM users WHERE email=?
            """, (email,))
            if row:
                exec_write("UPDATE users SET google_sub=?, name=?, avatar=? WHERE id=?",
                           (g_sub, name, picture, row["id"]))
            else:
                # Create new user
                any_user = query_one("SELECT id FROM users LIMIT 1")
                role = "admin" if not any_user else "user"
                dummy_pw = generate_password_hash(secrets.token_hex(16))
                exec_write("""
                    INSERT INTO users (email, password_hash, role, created_at, google_sub, name, avatar)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (email, dummy_pw, role, datetime.utcnow().isoformat(), g_sub, name, picture))
                row = query_one("""
                    SELECT id, email, role, COALESCE(unit_pref,'L') AS unit_pref
                    FROM users WHERE google_sub=?
                """, (g_sub,))

        login_user(User(row))
        flash("Signed in with Google.", "ok")
        return redirect(url_for("home"))
    except Exception as e:
        flash(f"Google login error: {e}", "error")
        return redirect(url_for("login"))

# -------- Finance settings (price/L & currency) --------
@app.route("/settings/finance", methods=["POST"])
@login_required
def save_finance():
    require_csrf()
    try:
        price = float(request.form.get("milk_price_per_litre") or 0)
        ccy = (request.form.get("currency") or "€").strip()[:3]
        exec_write("UPDATE users SET milk_price_per_litre=?, currency=? WHERE id=?",
                   (price, ccy, current_user.id))
        flash("Finance settings saved.", "ok")
    except Exception as e:
        flash(f"Save failed: {e}", "error")
    return redirect(url_for("home"))

# -------- Core data ops --------
def add_record(cow_number, litres, record_date_str, session_val, note, tags, owner_id):
    _ = date.fromisoformat(record_date_str) # validate
    if session_val not in ("AM","PM"):
        session_val = "AM"
    exec_write("""
      INSERT INTO milk_records (cow_number, litres, record_date, session, note, tags, owner_id, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (cow_number.strip(), float(litres), record_date_str, session_val,
          (note or "").strip() or None, (tags or "").strip() or None,
          owner_id, datetime.utcnow().isoformat()))

def update_record(rec_id, litres, session_val, note, tags, owner_id):
    fields, args = [], []
    if litres is not None:
        fields.append("litres=?"); args.append(float(litres))
    if session_val:
        fields.append("session=?"); args.append(session_val if session_val in ("AM","PM") else "AM")
    fields += ["note=?", "tags=?", "edited_at=?"]
    args += [(note or "").strip() or None, (tags or "").strip() or None, datetime.utcnow().isoformat(), owner_id, rec_id]
    exec_write(f"UPDATE milk_records SET {', '.join(fields)} WHERE owner_id=? AND id=?", tuple(args))

def soft_delete_record(rec_id, owner_id):
    exec_write("UPDATE milk_records SET deleted=1, edited_at=? WHERE owner_id=? AND id=?",
               (datetime.utcnow().isoformat(), owner_id, rec_id))

def restore_record(rec_id, owner_id):
    exec_write("UPDATE milk_records SET deleted=0, edited_at=? WHERE owner_id=? AND id=?",
               (datetime.utcnow().isoformat(), owner_id, rec_id))

# -------- PWA bits --------
@app.route("/manifest.json")
def manifest():
    data = {
        "name":"Milk Log","short_name":"MilkLog","start_url":"/","display":"standalone",
        "background_color":"#0f172a","theme_color":"#22c55e",
        "icons":[{"src":"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='192' height='192'><rect width='100%' height='100%' fill='%230f172a'/><text x='50%' y='55%' font-size='100' text-anchor='middle' fill='%2322c55e'>🐄</text></svg>","sizes":"192x192","type":"image/svg+xml"}]
    }
    return Response(json.dumps(data), mimetype="application/json")

@app.route("/sw.js")
def service_worker():
    js = """
const CACHE="milklog-v43-google";
const ASSETS=["/","/new","/records","/recent","/import","/export.csv","/manifest.json","/login","/register"];
self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS))));
self.addEventListener("fetch",e=>{e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(x=>{const y=x.clone();caches.open(CACHE).then(c=>c.put(e.request,y));return x;})));});
"""
    return Response(js, mimetype="application/javascript")

# -------- Views --------
@app.route("/")
@login_required
def home():
    k = kpis_for_home(current_owner_id())
    return render_template_string(TPL_HOME,
        base_css=BASE_CSS, k=k,
        to_d
