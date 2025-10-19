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
import requests  # not used directly, but handy for debugging provider calls

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
  google_sub TEXT,             -- no UNIQUE here; enforce via index
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
    # IMPORTANT: add without UNIQUE; uniqueness is enforced by an index below
    conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
if "name" not in ucols:
    conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
if "avatar" not in ucols:
    conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")

# Uniqueness: email is already unique; make google_sub unique via an index
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub)")


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
        if "session"   not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN session TEXT DEFAULT 'AM'")
        if "note"      not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN note TEXT")
        if "tags"      not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN tags TEXT")
        if "deleted"   not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN deleted INTEGER DEFAULT 0")
        if "owner_id"  not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN owner_id INTEGER")
        if "edited_at" not in mcols: conn.execute("ALTER TABLE milk_records ADD COLUMN edited_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_date ON milk_records(record_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_cow  ON milk_records(cow_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_sess ON milk_records(session)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_milk_del  ON milk_records(deleted)")
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
        if "name"           not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN name TEXT")
        if "breed"          not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN breed TEXT")
        if "parity"         not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN parity INTEGER")
        if "dob"            not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN dob TEXT")
        if "latest_calving" not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN latest_calving TEXT")
        if "group_name"     not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN group_name TEXT")
        if "owner_id"       not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN owner_id INTEGER")
        if "created_at"     not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN created_at TEXT")
        if "edited_at"      not in ccols: conn.execute("ALTER TABLE cows ADD COLUMN edited_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cows_tag   ON cows(tag)")
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
        ccy   = (request.form.get("currency") or "€").strip()[:3]
        exec_write("UPDATE users SET milk_price_per_litre=?, currency=? WHERE id=?",
                   (price, ccy, current_user.id))
        flash("Finance settings saved.", "ok")
    except Exception as e:
        flash(f"Save failed: {e}", "error")
    return redirect(url_for("home"))

# -------- Core data ops --------
def add_record(cow_number, litres, record_date_str, session_val, note, tags, owner_id):
    _ = date.fromisoformat(record_date_str)  # validate
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
        to_display_litres=to_display_litres, unit_pref_for=unit_pref_for,
        get_csrf_token=get_csrf_token
    )

@app.route("/new")
@login_required
def new_record_screen():
    return render_template_string(TPL_NEW, base_css=BASE_CSS, today=today_str(), get_csrf_token=get_csrf_token)

@app.route("/add", methods=["POST"])
@login_required
def add():
    cow = (request.form.get("cow_number") or "").strip()
    litres = (request.form.get("litres") or "").strip()
    session_val = (request.form.get("session") or "AM").strip()
    note = (request.form.get("note") or "").strip()
    tags = (request.form.get("tags") or "").strip()
    record_date_str = (request.form.get("record_date") or today_str()).strip()
    if not cow:
        flash("Cow number is required", "error")
        return redirect(url_for("new_record_screen"))
    try:
        litres_val = float(litres)
        if litres_val < 0: raise ValueError
    except ValueError:
        flash("Litres must be a non-negative number", "error")
        return redirect(url_for("new_record_screen"))
    try:
        add_record(cow, litres_val, record_date_str, session_val, note, tags, current_owner_id())
    except ValueError:
        flash("Bad date. Use YYYY-MM-DD.", "error")
        return redirect(url_for("new_record_screen"))
    except Exception as e:
        flash(f"Error saving: {e}", "error")
        return redirect(url_for("new_record_screen"))
    flash("Saved!", "ok")
    return redirect(url_for("new_record_screen"))

@app.route("/records")
@login_required
def records_screen():
    try:
        last = int(request.args.get("last", "7"))
    except ValueError:
        last = 7
    last = max(1, min(last, 90))
    dates_desc = query("""
        SELECT DISTINCT record_date
        FROM milk_records
        WHERE deleted=0 AND owner_id=?
        ORDER BY record_date DESC
        LIMIT ?
    """, (current_owner_id(), last))
    dates = list(reversed([r["record_date"] for r in dates_desc]))
    sessions = ["AM","PM"]
    rows=[]
    if dates:
        placeholders = ",".join("?" * len(dates))
        data = query(f"""
          SELECT cow_number, record_date, session, SUM(litres) AS litres
          FROM milk_records
          WHERE deleted=0 AND owner_id=? AND record_date IN ({placeholders})
          GROUP BY cow_number, record_date, session
        """, (current_owner_id(), *dates))
        by_cow={}
        for r in data:
            by_cow.setdefault(r["cow_number"],{})
            by_cow[r["cow_number"]][(r["record_date"], r["session"])] = float(r["litres"] or 0)
        def cow_key(c):
            try: return (0,int(c))
            except: return (1,c)
        for cow in sorted(by_cow.keys(), key=cow_key):
            vals=[]; tot=0.0
            for d in dates:
                for s in sessions:
                    v = by_cow[cow].get((d,s),0.0)
                    vals.append(round(v,2)); tot+=v
            rows.append({"cow":cow,"cells":vals,"total":round(tot,2)})
    return render_template_string(TPL_RECORDS,
        base_css=BASE_CSS, dates=dates, sessions=sessions, rows=rows,
        last=last, get_csrf_token=get_csrf_token
    )

@app.route("/recent")
@login_required
def recent_screen():
    try:
        limit = int(request.args.get("limit","150"))
    except ValueError:
        limit = 150
    limit = max(1, min(limit, 500))
    rows = query("""
      SELECT id, cow_number, litres, record_date, session, note, tags, created_at, edited_at, deleted
      FROM milk_records
      WHERE owner_id=?
      ORDER BY id DESC
      LIMIT ?
    """, (current_owner_id(), limit))
    msg = request.args.get("msg")
    return render_template_string(TPL_RECENT, base_css=BASE_CSS, rows=rows, limit=limit, msg=msg, get_csrf_token=get_csrf_token)

@app.route("/update/<int:rec_id>", methods=["POST"])
@login_required
def update(rec_id):
    litres = request.form.get("litres")
    session_val = request.form.get("session")
    note = request.form.get("note","")
    tags = request.form.get("tags","")
    try:
        litres_val = float(litres) if litres is not None else None
        update_record(rec_id, litres_val, session_val, note, tags, current_owner_id())
        return redirect(url_for("recent_screen", msg="Updated."))
    except Exception as e:
        return redirect(url_for("recent_screen", msg=f"Update failed: {e}"))

@app.route("/delete/<int:rec_id>", methods=["POST"])
@login_required
def delete(rec_id):
    soft_delete_record(rec_id, current_owner_id())
    return redirect(url_for("recent_screen", msg="Deleted 1 entry (soft delete)."))

@app.route("/restore/<int:rec_id>", methods=["POST"])
@login_required
def restore(rec_id):
    restore_record(rec_id, current_owner_id())
    return redirect(url_for("recent_screen", msg="Restored 1 entry."))

# ----- CSV Import/Export -----
@app.route("/import", methods=["GET", "POST"])
@login_required
def import_csv():
    info=None
    if request.method=="POST" and "file" in request.files:
        f = request.files["file"]
        try:
            text = f.stream.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(text))
            count=0
            for row in reader:
                try:
                    add_record(
                        row["cow_number"], float(row["litres"]),
                        row["record_date"], row.get("session","AM"),
                        row.get("note","") or "", row.get("tags","") or "",
                        current_owner_id()
                    )
                    count+=1
                except Exception:
                    pass
            info=f"Imported {count} records."
        except Exception as e:
            info=f"Import failed: {e}"
    return render_template_string(TPL_IMPORT, base_css=BASE_CSS, info=info, get_csrf_token=get_csrf_token)

@app.route("/export.csv")
@login_required
def export_csv():
    rows = query("""
      SELECT id, cow_number, litres, record_date, session, note, tags, created_at, edited_at, deleted
      FROM milk_records
      WHERE owner_id=?
      ORDER BY record_date DESC, id DESC
    """, (current_owner_id(),))
    out = io.StringIO(); w = csv.writer(out)
    headers = ["id","cow_number","litres","record_date","session","note","tags","created_at","edited_at","deleted"]
    w.writerow(headers)
    for r in rows: w.writerow([r[h] for h in headers])
    out.seek(0)
    return send_file(io.BytesIO(out.read().encode("utf-8")), as_attachment=True,
                     download_name="milk_records.csv", mimetype="text/csv")

@app.route("/export.xlsx")
@login_required
def export_excel():
    if Workbook is None:
        return "Excel export not available (openpyxl not installed).", 503
    data = query("""
      SELECT id, cow_number, litres, record_date, session, note, tags, created_at
      FROM milk_records
      WHERE owner_id=? AND deleted=0
      ORDER BY record_date ASC, cow_number ASC, id ASC
    """, (current_owner_id(),))
    wb = Workbook()
    ws = wb.active; ws.title = "Raw Records"
    ws.append(["ID","Cow #","Litres","Date","Session","Note","Tags","Saved (UTC)"])
    for r in data:
        ws.append([r["id"], r["cow_number"], r["litres"], r["record_date"], r["session"], r["note"] or "", r["tags"] or "", r["created_at"]])
    for col, wdt in zip("ABCDEFGH", [8,10,10,12,10,25,25,25]): ws.column_dimensions[col].width = wdt
    bio = io.BytesIO(); wb.save(bio); bio.seek(0)
    return send_file(bio, as_attachment=True, download_name="milk-records.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/healthz")
def healthz():
    return "ok", 200

# -------- Styles & Templates --------
BASE_CSS = """
:root{
  --bg:#0b1220; --panel:#0f172a; --border:#223044; --text:#e5e7eb;
  --muted:#9aa5b1; --accent:#22c55e; --accent-fore:#07220e;
  --radius:18px; --shadow:0 14px 40px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 10% -10%, #0a1222 0, #0b1629 30%, #0f172a 70%), #0f172a;
     color:var(--text);font-family:Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial}
.wrap{max-width:880px;margin:0 auto;padding:22px}
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
a.link{color:#86efac;text-decoration:underline}
"""

TPL_LOGIN = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="card" style="max-width:520px;margin:40px auto">
      <div class="top" style="margin-bottom:8px">
        <div class="brand">
          <svg class="logo" viewBox="0 0 24 24" fill="none"><path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/></svg>
          <div class="title">Milk Log</div>
        </div>
      </div>
      {% with msgs = get_flashed_messages(with_categories=true) %}{% if msgs %}{% for cat,m in msgs %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}{% endif %}{% endwith %}

      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:4px 0 14px">
        <a class="btn" style="background:#ffffff;color:#111;border:1px solid #ccc"
           href="{{ url_for('login_google') }}">
          <svg width="18" height="18" viewBox="0 0 48 48" style="margin-right:6px">
            <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.8 32.6 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3 0 5.7 1.1 7.8 3l5.7-5.7C34.6 6.1 29.6 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 19-8.9 19-20c0-1.2-.1-2.3-.4-3.5z"/>
            <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.5 16.3 18.9 13 24 13c3 0 5.7 1.1 7.8 3l5.7-5.7C34.6 6.1 29.6 4 24 4 16.2 4 9.5 8.3 6.3 14.7z"/>
            <path fill="#4CAF50" d="M24 44c5.2 0 10-2 13.5-5.2l-6.2-5.2C29.3 36 26.8 37 24 37c-5.2 0-9.6-3.4-11.2-8.1l-6.6 5.1C9.4 39.7 16.1 44 24 44z"/>
            <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-1 2.9-3.2 5.2-6 6.5l6.2 5.2C37.1 41.9 43 36.9 43 28c0-2.5-.5-4.8-1.4-7.5z"/>
          </svg>
          Continue with Google
        </a>
      </div>

      <form method="POST" class="grid2">
        <input type="hidden" name="_csrf" value="{{ get_csrf_token() }}">
        <div class="field"><label>Email</label><input name="email" type="email" required></div>
        <div class="field"><label>Password</label><input name="password" type="password" required></div>
        <div><button class="btn" type="submit">Sign in</button></div>
      </form>
      <div class="subtle" style="margin-top:10px">No account? <a class="link" href="{{ url_for('register') }}">Create one</a>.</div>
    </div>
  </div>
</body></html>
"""

TPL_REGISTER = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Register</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="card" style="max-width:520px;margin:40px auto">
      <div class="top" style="margin-bottom:8px">
        <div class="brand">
          <svg class="logo" viewBox="0 0 24 24" fill="none"><path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/></svg>
          <div class="title">Create Account</div>
        </div>
        <span class="badge">{{ 'First user will be admin' if default_role=='admin' else 'Role: user' }}</span>
      </div>
      {% with msgs = get_flashed_messages(with_categories=true) %}{% if msgs %}{% for cat,m in msgs %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}{% endif %}{% endwith %}
      <form method="POST" class="grid2">
        <input type="hidden" name="_csrf" value="{{ get_csrf_token() }}">
        <div class="field"><label>Email</label><input name="email" type="email" required></div>
        <div class="field"><label>Password</label><input name="password" type="password" required></div>
        <div><button class="btn" type="submit">Create account</button></div>
      </form>
      <div class="subtle" style="margin-top:10px">Already have an account? <a class="link" href="{{ url_for('login') }}">Sign in</a>.</div>
    </div>
  </div>
</body></html>
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
      <span class="badge">Finance: price/L</span>
    </div>

    <div class="hero">
      {% set v, u = to_display_litres(k.tot_litres) %}
      <div class="stat"><div class="big">{{ v }} {{ u }}</div><div>Total today</div></div>

      {% set v2, u2 = to_display_litres(k.milk_per_cow) %}
      <div class="stat"><div class="big">{{ v2 }} {{ u2 }}</div><div>Milk per cow</div></div>

      <div class="stat"><div class="big">{{ k.currency }} {{ '%.2f' % k.revenue_today }}</div><div>Revenue today</div></div>
    </div>

    <div class="card">
      <div style="font-size:20px;font-weight:800;margin-bottom:10px">Main menu</div>
      <div class="menu">
        <a class="btn" href="{{ url_for('records_screen') }}">Cow Records</a>
        <a class="btn secondary" href="{{ url_for('new_record_screen') }}">New Recording</a>
        <a class="btn secondary" href="{{ url_for('recent_screen') }}">Recent Entries</a>
        <a class="btn secondary" href="{{ url_for('import_csv') }}">Import CSV</a>
        <a class="btn secondary" href="{{ url_for('export_csv') }}">Export CSV</a>
        <a class="btn secondary" href="{{ url_for('export_excel') }}">Export Excel</a>
        <a class="btn warn" href="{{ url_for('logout') }}">Logout</a>
      </div>
    </div>

    <div class="card">
      <div style="font-size:18px;font-weight:800;margin-bottom:6px">Finance</div>
      <form method="POST" action="{{ url_for('save_finance') }}" class="grid2">
        <input type="hidden" name="_csrf" value="{{ get_csrf_token() }}">
        <div class="field">
          <label>Milk price ({{ k.currency }}/L)</label>
          <input type="number" step="0.001" name="milk_price_per_litre" placeholder="e.g. 0.52">
        </div>
        <div class="field">
          <label>Currency (symbol or ISO)</label>
          <input name="currency" maxlength="3" placeholder="€">
        </div>
        <div><button class="btn secondary" type="submit">Save</button></div>
      </form>
      <div class="hint">7-day avg revenue/day: <strong>{{ k.currency }} {{ '%.2f' % k.avg7_revenue_day }}</strong></div>
    </div>

    {% with msgs = get_flashed_messages(with_categories=true) %}{% if msgs %}{% for cat,m in msgs %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}{% endif %}{% endwith %}
    <div class="subtle">Set your price per litre to see revenue figures on this dashboard.</div>
  </div>
</body></html>
"""

TPL_NEW = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>New Recording</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top">
      <div class="brand"><svg class="logo" viewBox="0 0 24 24" fill="none"><path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/></svg><div class="title">New Recording</div></div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>
    {% with msgs = get_flashed_messages(with_categories=true) %}{% if msgs %}{% for cat,m in msgs %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}{% endif %}{% endwith %}

    <div class="card">
      <form method="POST" action="{{ url_for('add') }}" class="grid2">
        <input type="hidden" name="_csrf" value="{{ get_csrf_token() }}">
        <div class="field"><label>Cow number</label><input name="cow_number" required></div>
        <div class="field"><label>Litres</label><input name="litres" type="number" step="0.01" min="0" required></div>
        <div class="field"><label>Date</label><input id="record_date" name="record_date" type="date" value="{{ today }}" required></div>
        <div class="field"><label>Session</label>
          <select name="session"><option>AM</option><option>PM</option></select>
        </div>
        <div class="field"><label>Tags (comma)</label><input name="tags" placeholder="fresh, high"></div>
        <div class="field" style="grid-column:1/-1"><label>Note</label><textarea name="note" rows="3"></textarea></div>
        <div style="grid-column:1/-1"><button class="btn" type="submit">Save</button></div>
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
      <div class="brand"><svg class="logo" viewBox="0 0 24 24" fill="none"><path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/></svg><div class="title">Cow Records</div></div>
      <div class="header-actions">
        <a class="btn secondary" href="{{ url_for('records_screen', last=(request.args.get('last',7)|int - 3 if request.args.get('last') else 4)) }}">-3 days</a>
        <span class="badge">Window: {{ last }} days</span>
        <a class="btn secondary" href="{{ url_for('records_screen', last=(request.args.get('last',7)|int + 3 if request.args.get('last') else 10)) }}">+3 days</a>
        <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
      </div>
    </div>
    <div class="card">
      <table>
        <thead>
          <tr><th>Cow #</th>{% for d in dates %}{% for s in sessions %}<th>{{ d }} {{ s }}</th>{% endfor %}{% endfor %}<th>Total</th></tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr><td>{{ r.cow }}</td>{% for v in r.cells %}<td>{{ '%.2f'|format(v) }}</td>{% endfor %}<td><strong>{{ '%.2f'|format(r.total) }}</strong></td></tr>
          {% endfor %}
        </tbody>
      </table>
      {% if not rows %}<div class="hint">No data yet for this window.</div>{% endif %}
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
      <div class="brand"><svg class="logo" viewBox="0 0 24 24" fill="none"><path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/></svg><div class="title">Recent Entries</div></div>
      <a class="btn secondary" href="{{ url_for('home') }}">Back</a>
    </div>
    {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
    <div class="card">
      <table>
        <thead><tr><th>ID</th><th>Cow</th><th>Litres</th><th>Date</th><th>Session</th><th>Tags</th><th>Note</th><th>Created</th><th>Edited</th><th>Actions</th></tr></thead>
        <tbody>
        {% for r in rows %}
          <tr>
            <td>{{ r['id'] }}</td>
            <td>{{ r['cow_number'] }}</td>
            <td>
              <form method="POST" action="{{ url_for('update', rec_id=r['id']) }}" style="display:flex;gap:6px;align-items:center">
                <input type="hidden" name="_csrf" value="{{ get_csrf_token() }}">
                <input name="litres" type="number" step="0.01" min="0" value="{{ '%.2f'|format(r['litres']) }}" style="width:90px">
            </td>
            <td>{{ r['record_date'] }}</td>
            <td>
                <select name="session"><option {% if r['session']=='AM' %}selected{% endif %}>AM</option><option {% if r['session']=='PM' %}selected{% endif %}>PM</option></select>
            </td>
            <td><input name="tags" value="{{ r['tags'] or '' }}" style="width:140px"></td>
            <td><input name="note" value="{{ r['note'] or '' }}" style="width:180px"></td>
            <td><small class="muted">{{ r['created_at'][:16] }}</small></td>
            <td><small class="muted">{{ (r['edited_at'] or '')[:16] }}</small></td>
            <td style="display:flex;gap:6px">
                <button class="btn secondary" type="submit">Update</button></form>
                {% if r['deleted']==0 %}
                <form method="POST" action="{{ url_for('delete', rec_id=r['id']) }}"><input type="hidden" name="_csrf" value="{{ get_csrf_token() }}"><button class="btn warn" type="submit">Delete</button></form>
                {% else %}
                <form method="POST" action="{{ url_for('restore', rec_id=r['id']) }}"><input type="hidden" name="_csrf" value="{{ get_csrf_token() }}"><button class="btn" type="submit">Restore</button></form>
                {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      {% if not rows %}<div class="hint">No entries yet.</div>{% endif %}
    </div>
  </div>
</body></html>
"""

TPL_IMPORT = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import CSV</title><style>{{ base_css }}</style></head><body>
  <div class="wrap">
    <div class="top"><div class="brand"><svg class="logo" viewBox="0 0 24 24" fill="none"><path d="M4 10c0-4 3-7 8-7s8 3 8 7v6a3 3 0 0 1-3 3h-2l-1 2h-4l-1-2H7a3 3 0 0 1-3-3v-6Z" stroke="#22c55e" stroke-width="1.6"/></svg><div class="title">Import CSV</div></div><a class="btn secondary" href="{{ url_for('home') }}">Back</a></div>
    {% if info %}<div class="flash ok">{{ info }}</div>{% endif %}
    <div class="card">
      <form method="POST" enctype="multipart/form-data">
        <input type="hidden" name="_csrf" value="{{ get_csrf_token() }}">
        <div class="field"><label>CSV file</label><input type="file" name="file" accept=".csv" required></div>
        <button class="btn" type="submit">Upload & Import</button>
      </form>
      <div class="hint">Headers: cow_number, litres, record_date, session, note, tags</div>
    </div>
  </div>
</body></html>
"""

# -------- Local run --------
if __name__ == "__main__":
    app.config.update(SESSION_COOKIE_SAMESITE="Lax")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
