# --- app.py ---
from flask import Flask, request, redirect, url_for, render_template, send_file
import sqlite3
from contextlib import closing
from datetime import datetime, date
import io
from openpyxl import Workbook
import os

app = Flask(__name__)

DB_PATH = os.path.join(app.root_path, "milk_records.db")

# ----- DB -----
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS milk_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cow_number TEXT NOT NULL,
                litres REAL NOT NULL CHECK(litres >= 0),
                record_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

def add_record(cow_number, litres, record_date):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("""
            INSERT INTO milk_records (cow_number, litres, record_date, created_at)
            VALUES (?, ?, ?, ?)
        """, (cow_number, float(litres), record_date, datetime.utcnow().isoformat()))

def get_records():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM milk_records ORDER BY id DESC")
        return cur.fetchall()

# ----- Routes -----
@app.route("/")
def index():
    return render_template("home.html")

@app.route("/new")
def new():
    return render_template("new.html", today=date.today().isoformat())

@app.route("/records")
def records():
    rows = get_records()
    return render_template("records.html", rows=rows)

@app.route("/add", methods=["POST"])
def add():
    cow = request.form["cow_number"]
    litres = request.form["litres"]
    record_date = request.form.get("record_date", date.today().isoformat())
    add_record(cow, litres, record_date)
    return redirect(url_for("records"))

@app.route("/export.xlsx")
def export_xlsx():
    rows = get_records()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Cow Number", "Litres", "Record Date", "Created At (UTC)"])
    for r in rows:
        ws.append([r["id"], r["cow_number"], r["litres"], r["record_date"], r["created_at"]])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio,
                     as_attachment=True,
                     download_name="milk-records.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
