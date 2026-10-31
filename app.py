"""A Bright Children's School - Middle Result Portal.

Local run: py app.py
Browser:   http://127.0.0.1:5000

This app imports the school's Google Sheets CSV export. Student data is stored
locally in data/middle_results.db and is not bundled in the project ZIP.
"""
from __future__ import annotations

import csv
import io
import os
import secrets
import sqlite3
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask, Response, flash, redirect, render_template, request, session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "middle_results.db"

SCHOOL_NAME = "A Bright Children's School"
SCHOOL_LOCATION = "Lahar"
PORTAL_TITLE = "Middle Result Portal"
ACADEMIC_YEAR = "2026–27"  # Change this in app.py if required.
MAX_MARKS_PER_SUBJECT = 50
DEMO_MODE = os.environ.get("DEMO_MODE", "false").lower() == "true"

# These are the eight subject columns in the uploaded Google Sheets CSV.
SUBJECTS = ["Science", "Hindi", "English", "Sanskrit", "Math", "SST", "Computer", "GK"]
TOTAL_MAX_MARKS = len(SUBJECTS) * MAX_MARKS_PER_SUBJECT

# Local practice password only. Change before any internet deployment.
ADMIN_PASSWORD = os.environ.get("RESULT_ADMIN_PASSWORD", "schooladmin123")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "local-only-change-before-deployment")

# Refuse unsafe defaults on Render. Set both secrets in the Render dashboard.
if os.environ.get("RENDER", "").lower() == "true":
    if ADMIN_PASSWORD == "schooladmin123" or len(ADMIN_PASSWORD) < 16:
        raise RuntimeError("Set RESULT_ADMIN_PASSWORD to a unique password of at least 16 characters in Render.")
    if FLASK_SECRET_KEY == "local-only-change-before-deployment":
        raise RuntimeError("Set FLASK_SECRET_KEY in Render before starting the public demo.")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB CSV limit

# Actual CSV names are mapped to internal field names after trimming spaces
# and ignoring capitalization. The extra Total column is accepted but ignored;
# totals are recalculated from the eight subject marks.
HEADER_ALIASES = {
    "roll number": "roll_number",
    "roll no": "roll_number",
    "roll no.": "roll_number",
    "student's name": "student_name",
    "students name": "student_name",
    "student name": "student_name",
    "father's name": "father_name",
    "fathers name": "father_name",
    "father name": "father_name",
    "mother's name": "mother_name",
    "mothers name": "mother_name",
    "mother name": "mother_name",
    "dob": "dob",
    "date of birth": "dob",
    "science": "science",
    "hindi": "hindi",
    "english": "english",
    "sanskrit": "sanskrit",
    "math": "math",
    "mathematics": "math",
    "sst": "sst",
    "social science": "sst",
    "computer": "computer",
    "gk": "gk",
    "general knowledge": "gk",
    "total": "source_total",
    "total marks": "source_total",
}

MARK_COLUMNS = {subject: f"mark_{subject.lower()}" for subject in SUBJECTS}
MISSING_MARK_VALUES = {"", "NA", "N/A", "-", "—", "–"}


@contextmanager
def get_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    mark_sql = ",\n".join(
        f'"{MARK_COLUMNS[s]}" REAL NULL CHECK ("{MARK_COLUMNS[s]}" IS NULL OR '
        f'("{MARK_COLUMNS[s]}" >= 0 AND "{MARK_COLUMNS[s]}" <= {MAX_MARKS_PER_SUBJECT}))'
        for s in SUBJECTS
    )
    with get_db() as db:
        db.execute(
            f"""CREATE TABLE IF NOT EXISTS students (
                roll_number TEXT PRIMARY KEY,
                student_name TEXT NOT NULL,
                father_name TEXT NOT NULL DEFAULT '',
                mother_name TEXT NOT NULL DEFAULT '',
                dob TEXT NOT NULL DEFAULT '',
                {mark_sql},
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )


init_db()


def seed_demo_records() -> None:
    """Insert clearly fictional records for the public demo only."""
    if not DEMO_MODE:
        return

    demo_rows = [
        ("DEMO101", "Sample Student One", "", "", "", 45, 44, 48, 42, 47, 43, 46, 49),
        ("DEMO102", "Sample Student Two", "", "", "", 38, 41, 40, 39, 42, 37, 44, 43),
        ("DEMO103", "Sample Student Three", "", "", "", 32, 35, 34, 31, 36, 33, 30, 38),
    ]
    columns = ["roll_number", "student_name", "father_name", "mother_name", "dob"] + [MARK_COLUMNS[s] for s in SUBJECTS]
    quoted_columns = ", ".join('"' + col + '"' for col in columns)
    placeholders = ", ".join("?" for _ in columns)
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        if count == 0:
            db.executemany(
                f"INSERT INTO students ({quoted_columns}) VALUES ({placeholders})",
                demo_rows,
            )


seed_demo_records()


def is_admin() -> bool:
    return session.get("is_admin") is True


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def get_result_data(record: sqlite3.Row) -> dict[str, Any]:
    marks = {subject: record[MARK_COLUMNS[subject]] for subject in SUBJECTS}
    entered = [value for value in marks.values() if value is not None]
    missing_subjects = [subject for subject, value in marks.items() if value is None]
    complete = not missing_subjects
    total = sum(float(value) for value in entered) if complete else None
    percentage = (total / TOTAL_MAX_MARKS * 100) if complete else None
    return {
        "marks": marks,
        "complete": complete,
        "entered_count": len(entered),
        "missing_subjects": missing_subjects,
        "total": total,
        "maximum": TOTAL_MAX_MARKS,
        "percentage": percentage,
    }


def normalize_header(value: str) -> str:
    return " ".join((value or "").strip().split()).casefold()


def parse_mark(raw: str | None, row_number: int, subject: str, errors: list[str]):
    value = (raw or "").strip()
    if value.upper() in MISSING_MARK_VALUES:
        return None
    try:
        mark = float(value)
    except ValueError:
        errors.append(f"CSV row {row_number}: {subject} must be a number from 0 to {MAX_MARKS_PER_SUBJECT}, or blank/NA.")
        return None
    if not 0 <= mark <= MAX_MARKS_PER_SUBJECT:
        errors.append(f"CSV row {row_number}: {subject} mark {value!r} is outside 0–{MAX_MARKS_PER_SUBJECT}.")
        return None
    return mark


def import_csv_bytes(raw_bytes: bytes) -> tuple[int, int, list[str]]:
    """Validate the whole CSV, then upsert valid student rows atomically."""
    try:
        text_data = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV encoding could not be read. Download it again as CSV from Google Sheets.")

    reader = csv.DictReader(io.StringIO(text_data))
    if not reader.fieldnames:
        raise ValueError("The uploaded CSV is empty or has no heading row.")

    field_map: dict[str, str] = {}
    for original in reader.fieldnames:
        key = normalize_header(original)
        if key in HEADER_ALIASES:
            field_map[HEADER_ALIASES[key]] = original

    required = ["roll_number", "student_name", *[s.lower() for s in SUBJECTS]]
    missing = [field for field in required if field not in field_map]
    if missing:
        friendly = {
            "roll_number": "Roll number", "student_name": "Student's name",
            **{s.lower(): s for s in SUBJECTS},
        }
        raise ValueError("Missing required column(s): " + ", ".join(friendly[x] for x in missing))

    parsed_rows: list[tuple[Any, ...]] = []
    errors: list[str] = []
    skipped_blank_rows = 0
    seen_rolls: set[str] = set()

    for line_number, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            skipped_blank_rows += 1
            continue

        def get_value(field: str) -> str:
            source_header = field_map.get(field)
            return (row.get(source_header, "") or "").strip() if source_header else ""

        roll = get_value("roll_number")
        name = get_value("student_name")
        if not roll or not name:
            errors.append(f"CSV row {line_number}: Roll number and Student's name are required.")
            continue
        if roll in seen_rolls:
            errors.append(f"CSV row {line_number}: duplicate roll number {roll!r} appears more than once in this file.")
            continue
        seen_rolls.add(roll)

        marks = [parse_mark(get_value(subject.lower()), line_number, subject, errors) for subject in SUBJECTS]
        parsed_rows.append((
            roll, name, get_value("father_name"), get_value("mother_name"), get_value("dob"), *marks
        ))

    if errors:
        raise ValueError("\n".join(errors[:12]) + (f"\nAnd {len(errors)-12} more issue(s). No data was imported." if len(errors) > 12 else "\nNo data was imported. Please correct the CSV and upload again."))
    if not parsed_rows:
        raise ValueError("No student records were found. Check that Roll number and Student's name columns contain data.")

    columns = ["roll_number", "student_name", "father_name", "mother_name", "dob"] + [MARK_COLUMNS[s] for s in SUBJECTS]
    quoted_columns = ", ".join('"' + col + '"' for col in columns)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f'"{col}" = excluded."{col}"' for col in columns[1:])
    updates += ", updated_at = CURRENT_TIMESTAMP"

    with get_db() as db:
        db.executemany(
            f"INSERT INTO students ({quoted_columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(roll_number) DO UPDATE SET {updates}",
            parsed_rows,
        )
    return len(parsed_rows), skipped_blank_rows, []


@app.route("/", methods=["GET", "POST"])
def home():
    result = None
    calculation = None
    searched_roll = ""
    if request.method == "POST":
        searched_roll = request.form.get("roll_number", "").strip()
        if not searched_roll:
            flash("Please enter your roll number.", "error")
        else:
            with get_db() as db:
                result = db.execute("SELECT * FROM students WHERE roll_number = ?", (searched_roll,)).fetchone()
            if result is None:
                flash("No result found for this roll number. Please check it or contact the school office.", "error")
            else:
                calculation = get_result_data(result)
    return render_template(
        "index.html", school_name=SCHOOL_NAME, school_location=SCHOOL_LOCATION,
        portal_title=PORTAL_TITLE, academic_year=ACADEMIC_YEAR, subjects=SUBJECTS,
        result=result, calculation=calculation, searched_roll=searched_roll,
    )


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if is_admin():
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        entered = request.form.get("password", "")
        if secrets.compare_digest(entered, ADMIN_PASSWORD):
            session.clear()
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect admin password.", "error")
    return render_template("admin_login.html", school_name=SCHOOL_NAME)


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        complete_count = db.execute(
            "SELECT COUNT(*) FROM students WHERE " + " AND ".join(f'"{MARK_COLUMNS[s]}" IS NOT NULL' for s in SUBJECTS)
        ).fetchone()[0]
    return render_template(
        "admin_dashboard.html", school_name=SCHOOL_NAME, portal_title=PORTAL_TITLE,
        subjects=SUBJECTS, student_count=count, complete_count=complete_count,
        pending_count=count - complete_count,
    )


@app.route("/admin/import", methods=["POST"])
@admin_required
def import_results():
    if DEMO_MODE:
        flash("CSV import is disabled on this public demo. Do not upload real student records.", "error")
        return redirect(url_for("admin_dashboard"))
    uploaded = request.files.get("csv_file")
    if uploaded is None or not uploaded.filename:
        flash("Please select your Google Sheets CSV file first.", "error")
        return redirect(url_for("admin_dashboard"))
    if not uploaded.filename.lower().endswith(".csv"):
        flash("Please upload a .csv file. In Google Sheets use File → Download → Comma-separated values (.csv).", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        imported, skipped, _ = import_csv_bytes(uploaded.read())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard"))
    message = f"Successfully imported/updated {imported} student record(s)."
    if skipped:
        message += f" Skipped {skipped} completely blank row(s)."
    message += " Blank/NA/- subject marks are saved as pending, not converted to zero."
    flash(message, "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export")
@admin_required
def export_backup():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Roll number", "Student's name", "Father's Name", "Mother's Name", "DOB", *SUBJECTS, "Total"])
    with get_db() as db:
        records = db.execute("SELECT * FROM students ORDER BY roll_number").fetchall()
    for record in records:
        marks = [record[MARK_COLUMNS[s]] for s in SUBJECTS]
        complete = all(mark is not None for mark in marks)
        total = sum(marks) if complete else ""
        writer.writerow([
            record["roll_number"], record["student_name"], record["father_name"],
            record["mother_name"], record["dob"],
            *["NA" if mark is None else mark for mark in marks], total,
        ])
    return Response(
        "\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=school_results_backup.csv"},
    )


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


@app.errorhandler(413)
def file_too_large(_error):
    flash("The CSV file is too large. Maximum upload size is 2 MB.", "error")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    print("\nA Bright Children's School - Middle Result Portal")
    print("Open in Chrome: http://127.0.0.1:5000")
    print("Admin panel:    http://127.0.0.1:5000/admin")
    print("Keep this window open while using the website.\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
