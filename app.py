import sqlite3
import calendar
import csv
import io
import re
import os
import shutil
from urllib.parse import urlsplit
import threading
import time as time_module
import base64
import binascii
import hashlib
import hmac
import uuid
from dotenv import load_dotenv

load_dotenv()
import json
from functools import wraps
from datetime import datetime, date, time, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, g, session, jsonify, Response, send_file, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from markupsafe import Markup, escape

try:
    from authlib.integrations.flask_client import OAuth
    from authlib.integrations.base_client.errors import OAuthError
except ImportError:  # Keeps the setup page usable until requirements are installed.
    OAuth = None
    OAuthError = Exception

try:
    from flask_sock import Sock
    from simple_websocket import ConnectionClosed
except ImportError:  # Without it the app still runs; pages ask for updates every few seconds instead.
    Sock = None
    ConnectionClosed = Exception

from invoice_pdf import build_invoice_pdf

DB_PATH = "leads.db"
ROLES = ["telecaller", "field_sales", "manager"]
# QuantumLoop operates on India Standard Time. A fixed offset avoids depending on
# the optional Windows IANA timezone database while remaining exact (IST has no DST).
APP_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
FIELD_WORK_AUTO_END = time(18, 30)
NO_CARD_REASONS = ["No card available", "Forgot to ask", "Contact declined", "Other"]
VISIT_STATUS_LABELS = {
    "scheduled": "Upcoming", "checked_in": "In progress", "review_pending": "Review pending",
    "completed": "Completed", "needs_correction": "Needs correction",
}
VISIT_OUTCOMES = ["Interested", "Follow-up Required", "Proposal Requested", "Not Interested", "No Decision", "Contact Unavailable", "Other"]

# ---------- New pipeline ----------
STAGES = ["New", "Attempted", "Contacted", "Follow Up", "Interested",
          "Qualified", "Proposal Sent", "Negotiation", "Won", "Lost"]
# Stages a manager can see across all owners (escalated / advanced pipeline)
ESCALATE_STAGES = {"Interested", "Qualified", "Proposal Sent", "Negotiation", "Won", "Lost"}
# Map legacy 6-stage values to the new pipeline (run once as a migration)
LEGACY_STAGE_MAP = {
    "Not Interested": "Lost",
    "Converted": "Won",
}

LOST_REASONS = ["No Budget", "Already Has Vendor", "No Requirement", "Wrong Contact",
                "No Response", "Competitor Won", "Other"]

SERVICES = [
    "Website Design", "Website Redesign", "Ecommerce", "SEO", "Google Ads",
    "Social Media Marketing", "Branding", "Graphic Designing", "ERP", "CRM",
    "Mobile App", "Custom Software", "Hardware Supply", "Cyber Security",
    "Server Services", "Networking", "Structured Cabling", "Cloud Services",
    "CCTV & Surveillance", "AMC", "Other",
]

WEBSITE_STATUS = ["No Website", "Has Website", "Under Development"]
WEBSITE_QUALITY = ["Excellent", "Good", "Average", "Poor", "Broken"]
GOOGLE_PRESENCE = ["Excellent", "Average", "Poor", "Not Found"]
SOCIAL_PRESENCE = ["Active", "Moderate", "Poor", "None"]
DECISION_MAKER_OPTIONS = ["Yes", "No", "Unknown"]
INTEREST_LEVELS = ["Hot", "Warm", "Cold"]
PRIORITIES = ["High", "Medium", "Low"]
BUDGET_RANGES = ["Under 25k", "25k-50k", "50k-1L", "1L-2L", "2L-5L", "5L+"]
COMPANY_STATUSES = ["Prospect", "Customer", "Inactive"]
PROJECT_STATUSES = ["Planning", "In Progress", "On Hold", "Client Review", "Completed", "Cancelled"]
PROJECT_TYPES = ["Website", "SEO", "Ads", "Custom Web App", "Other IT Services"]

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"},
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(
        days=max(1, int(os.environ.get("LOGIN_SESSION_DAYS", "3650")))
    ),
    SESSION_REFRESH_EACH_REQUEST=True,
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
)
# Only trust forwarded host/protocol headers when the deployment has a known proxy.
if os.environ.get("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Keep-alive pings stop proxies from closing an idle WebSocket.
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app) if Sock else None

oauth = OAuth(app) if OAuth else None
google = None
if oauth and os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    google = oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _add_cols(db, table, coldefs):
    """coldefs: list of (name, sql_type_and_default)"""
    existing = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    for name, decl in coldefs:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db():
    """Create or upgrade the database. Safe to run from several server workers at once.

    Every worker runs this when it starts. Taking the write lock first makes them go one at a
    time, and doing all the work in a single transaction means a failed upgrade leaves the
    database exactly as it was. The first upgrade to the Tasks version also keeps a copy of
    the database file next to it.
    """
    db = sqlite3.connect(DB_PATH, timeout=120, isolation_level=None)
    try:
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # so the file copy below is complete
        db.execute("BEGIN IMMEDIATE")
        try:
            _backup_before_upgrade(db)
            _init_schema(db)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    finally:
        db.close()


def _backup_before_upgrade(db):
    """Copy the database file aside before the first upgrade that adds the Tasks tables.

    Runs while this process holds the write lock and before it has changed anything, so the
    copy is a consistent snapshot. Later starts skip it because the Tasks tables exist.
    """
    def has_table(name):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()

    if has_table("users") and not has_table("work_tasks"):
        stamp = datetime.now(APP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(DB_PATH, f"{DB_PATH}.before-upgrade-{stamp}.bak")


def _init_schema(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _add_cols(db, "users", [
        ("email", "TEXT"),
        ("google_sub", "TEXT"),
        ("last_login_at", "TEXT"),
    ])
    # Older installations sometimes used an email address as the username.
    db.execute(
        "UPDATE users SET email=LOWER(TRIM(username)) "
        "WHERE (email IS NULL OR TRIM(email)='') AND username LIKE '%@%'"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase "
        "ON users(email COLLATE NOCASE) WHERE email IS NOT NULL AND TRIM(email) <> ''"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub "
        "ON users(google_sub) WHERE google_sub IS NOT NULL AND TRIM(google_sub) <> ''"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            remote_addr TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            attempted_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_login_attempts_lookup "
        "ON admin_login_attempts(username, remote_addr, attempted_at)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            source TEXT,
            stage TEXT NOT NULL DEFAULT 'New',
            tags TEXT,
            follow_up_date TEXT,
            owner_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            outcome TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            industry TEXT,
            phone TEXT,
            email TEXT,
            website TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            designation TEXT,
            phone TEXT,
            email TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies (id) ON DELETE CASCADE
        )
        """
    )
    # Invoice module migrations and tables
    company_cols = [r[1] for r in db.execute("PRAGMA table_info(companies)")]
    for col in ("billing_address", "gstin", "pan"):
        if col not in company_cols:
            db.execute(f"ALTER TABLE companies ADD COLUMN {col} TEXT")

    db.execute("""
        CREATE TABLE IF NOT EXISTS company_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT, address_lines TEXT, website TEXT, gstin TEXT, pan TEXT,
            bank_name TEXT, ifsc TEXT, account_no TEXT, bank_branch TEXT, upi_id TEXT,
            default_terms TEXT, default_notes TEXT, invoice_prefix TEXT DEFAULT 'QL'
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT NOT NULL UNIQUE, invoice_date TEXT NOT NULL,
            company_id INTEGER, bill_name TEXT NOT NULL, bill_address TEXT, bill_gstin TEXT, bill_pan TEXT, bill_website TEXT,
            ship_name TEXT, ship_address TEXT, ship_gstin TEXT, ship_pan TEXT, ship_website TEXT,
            notes TEXT, terms TEXT, cgst_pct REAL DEFAULT 9, sgst_pct REAL DEFAULT 9, received_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0, created_by INTEGER, created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, item_name TEXT NOT NULL, code TEXT,
            qty REAL NOT NULL, rate REAL NOT NULL, tax_pct REAL NOT NULL,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        )
    """)
    if not db.execute("SELECT 1 FROM company_settings WHERE id=1").fetchone():
        db.execute("""INSERT INTO company_settings
            (id,name,address_lines,website,gstin,pan,bank_name,ifsc,account_no,bank_branch,upi_id,default_terms,default_notes,invoice_prefix)
            VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            'QUANTUMLOOP PRIVATE LIMITED',
            'H.No: 1-4-214/26P, P NO26, Sangamitra Enclave, Sainikpuri Road, Hyderabad, Telangana, 500062',
            'www.quantumloop.online', '36AABCQ2756M1Z2', 'AABCQ2756M', 'QuantumLoop Private Limited',
            'IDIB000A135', '8316619617', 'As Rao Nagar', 'quantumloop@okhdfcbank',
            'Quotation valid for 30 days from issue date.\n50% advance required to initiate development.\nDelivery timelines commence upon receipt of advance payment.\nInfrastructure subscriptions are billed annually.\nAdditional features not listed in this quotation will be charged separately.\nGoods/Services once sold will not be taken back.\nSubject to Hyderabad jurisdiction only.',
            '', 'QL'))

    # migrate: add owner_id to leads if this db predates it
    cols = [r[1] for r in db.execute("PRAGMA table_info(leads)")]
    if "owner_id" not in cols:
        db.execute("ALTER TABLE leads ADD COLUMN owner_id INTEGER")

    # ---------- New CRM: Company becomes the center of everything ----------

    _add_cols(db, "companies", [
        ("alternate_phone", "TEXT"),
        ("address", "TEXT"),
        ("city", "TEXT"),
        ("state", "TEXT"),
        ("pincode", "TEXT"),
        ("country", "TEXT DEFAULT 'India'"),
        ("cin", "TEXT"),
        ("status", "TEXT DEFAULT 'Prospect'"),
        ("account_owner_id", "INTEGER"),
        ("updated_at", "TEXT"),
        ("gbp_link", "TEXT"),
    ])

    _add_cols(db, "contacts", [
        ("alternate_phone", "TEXT"),
        ("linkedin", "TEXT"),
        ("is_primary", "INTEGER DEFAULT 0"),
        ("updated_at", "TEXT"),
    ])

    # A follow-up a telecaller (or anyone without manager rights) has handed to a manager.
    _add_cols(db, "leads", [
        ("followup_manager_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
        ("followup_requested_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
        ("followup_note", "TEXT"),
        ("followup_requested_at", "TEXT"),
    ])
    _add_cols(db, "leads", [
        ("company_id", "INTEGER"),
        ("contact_id", "INTEGER"),
        ("industry", "TEXT"),
        ("designation", "TEXT"),
        ("alternate_phone", "TEXT"),
        ("website_status", "TEXT"),
        ("website_url", "TEXT"),
        ("website_quality", "TEXT"),
        ("google_presence", "TEXT"),
        ("social_presence", "TEXT"),
        ("services_required", "TEXT"),
        ("other_service_description", "TEXT"),
        ("decision_maker", "TEXT"),
        ("interest_level", "TEXT"),
        ("priority", "TEXT"),
        ("budget_range", "TEXT"),
        ("estimated_project_value", "TEXT"),
        ("expected_timeline", "TEXT"),
        ("lost_reason", "TEXT"),
        ("qualification_notes", "TEXT"),
        ("requirement_notes", "TEXT"),
        ("internal_notes", "TEXT"),
        ("updated_at", "TEXT"),
    ])

    _add_cols(db, "call_logs", [
        ("user_id", "INTEGER"),
    ])

    _add_cols(db, "invoices", [
        ("bill_contact_name", "TEXT"),
        ("bill_contact_email", "TEXT"),
        ("bill_contact_phone", "TEXT"),
        ("ship_state", "TEXT"),
        ("tax_mode", "TEXT DEFAULT 'INTRA'"),
        ("igst_pct", "REAL DEFAULT 0"),
    ])

    # Migrate legacy stage names -> new pipeline (idempotent; no-op after first run)
    for old_stage, new_stage in LEGACY_STAGE_MAP.items():
        db.execute("UPDATE leads SET stage = ? WHERE stage = ?", (new_stage, old_stage))
        if old_stage == "Not Interested":
            db.execute(
                "UPDATE leads SET lost_reason = COALESCE(lost_reason, 'Other') WHERE stage = 'Lost' AND (lost_reason IS NULL OR lost_reason = '')"
            )

    # Future-proof tables (section 12 of the spec) — created now even if lightly used today.
    db.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            entity_label TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS lead_activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            detail TEXT,
            user_id INTEGER,
            user_name TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
        )
    """)

    # Access grants: gives a user (telecaller or manager) view+edit access to a lead
    # they don't own, WITHOUT changing ownership or copying the record. Ownership
    # (owner_id on leads) never changes via this table.
    db.execute("""
        CREATE TABLE IF NOT EXISTS lead_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            granted_by INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(lead_id, user_id),
            FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    """)

    # Field Sales daily work sessions and visits. Visit assignment is deliberately
    # separate from lead ownership: telecallers/managers keep CRM responsibility
    # while an executive receives access to the visit and its related record.
    db.execute("""
        CREATE TABLE IF NOT EXISTS field_work_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            work_date TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            end_source TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            UNIQUE(user_id, work_date),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS field_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            company_id INTEGER,
            assigned_to INTEGER,
            created_by INTEGER,
            origin TEXT NOT NULL,
            scheduled_date TEXT NOT NULL,
            scheduled_time TEXT,
            location_address TEXT NOT NULL,
            purpose TEXT NOT NULL,
            contact_person TEXT,
            contact_designation TEXT,
            contact_phone TEXT,
            contact_email TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            session_id INTEGER,
            checkin_at TEXT,
            checkin_lat REAL,
            checkin_lng REAL,
            checkin_accuracy REAL,
            location_status TEXT,
            completed_at TEXT,
            people_met TEXT,
            discussion TEXT,
            outcome TEXT,
            notes TEXT,
            next_action TEXT,
            follow_up_date TEXT,
            no_card_reason TEXT,
            no_card_explanation TEXT,
            submitted_at TEXT,
            review_status TEXT,
            reviewed_by INTEGER,
            reviewed_at TEXT,
            review_comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE SET NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE SET NULL,
            FOREIGN KEY (assigned_to) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (session_id) REFERENCES field_work_sessions(id) ON DELETE SET NULL,
            FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS field_visit_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visit_id INTEGER NOT NULL,
            photo_type TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (visit_id) REFERENCES field_visits(id) ON DELETE CASCADE
        )
    """)
    _add_cols(db, "field_visits", [
        ("contact_designation", "TEXT"),
        ("contact_phone", "TEXT"),
        ("contact_email", "TEXT"),
    ])
    db.execute("CREATE INDEX IF NOT EXISTS idx_field_visits_assignee_date ON field_visits(assigned_to, scheduled_date)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_field_visits_review ON field_visits(review_status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_field_sessions_user_date ON field_work_sessions(user_id, work_date)")

    # Project management: a project may be created from a won lead or independently.
    # Project membership controls visibility for staff; managers retain company-wide access.
    db.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            lead_id INTEGER,
            company_id INTEGER,
            project_manager_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Planning',
            priority TEXT NOT NULL DEFAULT 'Medium',
            start_date TEXT,
            target_date TEXT,
            budget REAL,
            description TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE SET NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE SET NULL,
            FOREIGN KEY (project_manager_id) REFERENCES users(id) ON DELETE RESTRICT,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_members (
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            assignee_id INTEGER,
            due_date TEXT,
            priority TEXT NOT NULL DEFAULT 'Medium',
            status TEXT NOT NULL DEFAULT 'To Do',
            estimated_hours REAL,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (assignee_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_personal_notes (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            user_id INTEGER,
            event TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_projects_manager ON projects(project_manager_id, status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_project_tasks_assignee ON project_tasks(assignee_id, status, due_date)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id)")
    _add_cols(db, "project_tasks", [("start_date", "TEXT")])
    # company_id is the client the work is for; NULL means an internal project.
    _add_cols(db, "projects", [("project_type", "TEXT")])
    _init_work_tasks(db)
    db.execute("""CREATE TABLE IF NOT EXISTS personal_reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        remind_at TEXT NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_time ON personal_reminders(user_id, remind_at)")
    # repeat_unit: '' (one-off), day, week, month or year. repeat_days holds the chosen weekdays
    # (0 = Monday) for weekly repeats, or the day of the month to aim for on monthly and yearly ones.
    _add_cols(db, "personal_reminders", [
        ("notes", "TEXT"),
        ("repeat_unit", "TEXT NOT NULL DEFAULT ''"),
        ("repeat_every", "INTEGER NOT NULL DEFAULT 1"),
        ("repeat_days", "TEXT NOT NULL DEFAULT ''"),
        ("repeat_until", "TEXT"),
        ("snoozed_until", "TEXT"),
    ])
    db.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        client_id TEXT NOT NULL,
        CHECK (sender_id != recipient_id),
        UNIQUE (sender_id, client_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_chat_pair ON chat_messages(sender_id,recipient_id,id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_chat_unread ON chat_messages(recipient_id,sender_id,id) WHERE is_read=0")



def now_str():
    return datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d")


def month_start_str():
    return datetime.now(APP_TIMEZONE).date().replace(day=1).strftime("%Y-%m-%d")


def time_ago(ts):
    if not ts:
        return ""
    try:
        then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts
    delta = datetime.now(APP_TIMEZONE).replace(tzinfo=None) - then
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}d ago"
    if seconds < 86400 * 365:
        return f"{int(seconds // (86400 * 30))}mo ago"
    return f"{int(seconds // (86400 * 365))}y ago"


# ---------- Activity logging ----------

def log_activity(action, entity_type, entity_id=None, entity_label="", details=""):
    db = get_db()
    user = current_user()
    db.execute(
        "INSERT INTO activity_logs (user_id, user_name, action, entity_type, entity_id, entity_label, details, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user["id"] if user else None, user["name"] if user else "System", action, entity_type, entity_id, entity_label, details, now_str()),
    )
    db.commit()


def log_lead_event(lead_id, event, detail=""):
    db = get_db()
    user = current_user()
    db.execute(
        "INSERT INTO lead_activities (lead_id, event, detail, user_id, user_name, created_at) VALUES (?,?,?,?,?,?)",
        (lead_id, event, detail, user["id"] if user else None, user["name"] if user else "System", now_str()),
    )
    db.commit()


# ---------- Auth helpers ----------

def current_user():
    if "user_id" not in session:
        return None
    if "user" not in g:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return g.user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def manager_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "manager":
            flash("Only Customer Relations Managers can access that.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def field_sales_or_manager_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] not in {"manager", "field_sales"}:
            flash("Only managers and Field Sales Executives can access that.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def _reconcile_field_sessions(db):
    """Close active sessions at 18:30 local time, even if reconciliation runs later."""
    local_now = datetime.now(APP_TIMEZONE)
    rows = db.execute("SELECT * FROM field_work_sessions WHERE status = 'active'").fetchall()
    changed = False
    for row in rows:
        work_day = datetime.strptime(row["work_date"], "%Y-%m-%d").date()
        cutoff = datetime.combine(work_day, FIELD_WORK_AUTO_END).replace(tzinfo=APP_TIMEZONE)
        if local_now >= cutoff:
            db.execute(
                "UPDATE field_work_sessions SET ended_at=?, end_source='system_assumed', status='closed' WHERE id=?",
                (cutoff.strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
            )
            changed = True
    if changed:
        db.commit()


@app.before_request
def require_login():
    open_endpoints = {"login", "setup", "google_login", "google_callback", "manual_admin_login", "static", "favicon"}
    if request.endpoint in open_endpoints:
        return
    if request.endpoint is None:
        # Unknown URLs (e.g. the browser's automatic /favicon.ico request) get a plain 404
        # instead of being remembered as the post-login destination.
        return
    db = get_db()
    _reconcile_field_sessions(db)
    has_users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if not has_users and request.endpoint != "setup":
        return redirect(url_for("setup"))
    if has_users and not current_user():
        if request.method == "GET":
            return redirect(url_for("login", next=request.path))
        return redirect(url_for("login"))


def visible_leads_clause(user):
    """Returns (sql_fragment, params) restricting leads to what this user may see.

    Rules:
    - A lead's owner (owner_id) always sees it. Ownership never moves except via
      /leads/<id>/assign for leads that don't have an owner yet.
    - Every telecaller's leads are always visible to every manager, tagged with the
      telecaller's name (managers never "own" a telecaller's lead by seeing it).
    - Anyone explicitly granted access via lead_shares (a "transfer") sees the lead
      too, without it changing owner_id — this is how a manager's own leads, or a
      telecaller's leads, get shared with another telecaller/manager.
    """
    shared_clause = "leads.id IN (SELECT lead_id FROM lead_shares WHERE user_id = ?)"
    visit_clause = "leads.id IN (SELECT lead_id FROM field_visits WHERE assigned_to = ?)"
    if user["role"] == "manager":
        return (
            f"(owner_id = ? OR owner_id IN (SELECT id FROM users WHERE role IN ('telecaller','field_sales')) OR owner_id IS NULL OR {shared_clause})",
            [user["id"], user["id"]],
        )
    if user["role"] == "field_sales":
        return f"(owner_id = ? OR {shared_clause} OR {visit_clause})", [user["id"], user["id"], user["id"]]
    return f"(owner_id = ? OR {shared_clause})", [user["id"], user["id"]]


# ---------- Project management helpers ----------

def visible_projects_clause(user, alias="projects"):
    """Return a visibility clause for projects.

    Managers can see the complete delivery portfolio. Everyone else sees a
    project only when they own it, belong to its team, or have a task on it.
    """
    if user["role"] == "manager":
        return "1=1", []
    return (
        f"({alias}.project_manager_id=? OR "
        f"EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id={alias}.id AND pm.user_id=?) OR "
        f"EXISTS (SELECT 1 FROM work_tasks wt JOIN work_task_assignees wa ON wa.task_id=wt.id "
        f"WHERE wt.project_id={alias}.id AND wa.user_id=?))",
        [user["id"], user["id"], user["id"]],
    )


def can_manage_project(user, project):
    return bool(user and project and (user["role"] == "manager" or project["project_manager_id"] == user["id"]))


def log_project_event(db, project_id, event, detail=""):
    user = current_user()
    db.execute(
        "INSERT INTO project_activities (project_id,user_id,event,detail,created_at) VALUES (?,?,?,?,?)",
        (project_id, user["id"] if user else None, event, detail, now_str()),
    )


# ---------- Setup / Auth ----------

def _normalize_email(value):
    return (value or "").strip().lower()


def _valid_email(value):
    email = _normalize_email(value)
    return bool(email and "@" in email and not email.startswith("@") and not email.endswith("@"))


def _safe_next_url(value):
    return value if value and value.startswith("/") and not value.startswith("//") else None


def _bootstrap_emails():
    raw = os.environ.get("GOOGLE_BOOTSTRAP_EMAILS", "")
    return {_normalize_email(value) for value in raw.replace(";", ",").split(",") if value.strip()}


def _google_redirect_uri():
    return os.environ.get("GOOGLE_REDIRECT_URI", "").strip() or url_for("google_callback", _external=True)


def _manual_admin_login_enabled():
    return os.environ.get("MANUAL_ADMIN_LOGIN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _manual_login_remote_addr():
    return (request.remote_addr or "unknown")[:100]


def _manual_login_is_locked(db, username, remote_addr):
    cutoff = (datetime.now(APP_TIMEZONE) - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    failures = db.execute(
        """SELECT COUNT(*) c FROM admin_login_attempts
           WHERE success=0 AND attempted_at>=? AND (username=? OR remote_addr=?)""",
        (cutoff, username, remote_addr),
    ).fetchone()["c"]
    return failures >= 5


def _record_manual_login_attempt(db, username, remote_addr, success):
    prune_before = (datetime.now(APP_TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM admin_login_attempts WHERE attempted_at<?", (prune_before,))
    db.execute(
        "INSERT INTO admin_login_attempts (username,remote_addr,success,attempted_at) VALUES (?,?,?,?)",
        (username, remote_addr, 1 if success else 0, now_str()),
    )
    if success:
        db.execute(
            "DELETE FROM admin_login_attempts WHERE success=0 AND (username=? OR remote_addr=?)",
            (username, remote_addr),
        )
    db.commit()


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    has_users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if has_users:
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = _normalize_email(request.form.get("email"))
        if not name or not _valid_email(email):
            flash("Enter your name and a valid Google account email.", "error")
            return redirect(url_for("setup"))
        db.execute(
            "INSERT INTO users (username, password_hash, name, role, email, created_at) "
            "VALUES (?, 'google-only', ?, 'manager', ?, ?)",
            (email, name, email, now_str()),
        )
        db.commit()
        flash("Manager access created. Continue with Google using that email.", "success")
        return redirect(url_for("login"))

    return render_template("setup.html")


@app.route("/login")
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    next_url = _safe_next_url(request.args.get("next"))
    if next_url:
        session["post_login_next"] = next_url
    return render_template(
        "login.html",
        google_ready=google is not None,
        authlib_ready=OAuth is not None,
        manual_admin_enabled=_manual_admin_login_enabled(),
    )


@app.route("/admin-login", methods=["GET", "POST"])
def manual_admin_login():
    if not _manual_admin_login_enabled():
        abort(404)
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        db = get_db()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        remote_addr = _manual_login_remote_addr()

        if _manual_login_is_locked(db, username, remote_addr):
            flash("Too many failed admin login attempts. Try again in 15 minutes.", "error")
            return redirect(url_for("manual_admin_login"))

        user = db.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE AND role='manager'",
            (username,),
        ).fetchone()
        valid_legacy_password = bool(
            user and user["password_hash"] != "google-only"
            and check_password_hash(user["password_hash"], password)
        )

        recovery_username = os.environ.get("MANUAL_ADMIN_USERNAME", "").strip().lower()
        recovery_hash = os.environ.get("MANUAL_ADMIN_PASSWORD_HASH", "").strip()
        valid_recovery_password = bool(
            recovery_username and recovery_hash and username == recovery_username
            and check_password_hash(recovery_hash, password)
        )
        if valid_recovery_password and not user:
            user = db.execute("SELECT * FROM users WHERE role='manager' ORDER BY id LIMIT 1").fetchone()

        if not user or not (valid_legacy_password or valid_recovery_password):
            _record_manual_login_attempt(db, username, remote_addr, False)
            flash("Invalid admin username or password.", "error")
            return redirect(url_for("manual_admin_login"))

        _record_manual_login_attempt(db, username, remote_addr, True)
        next_url = _safe_next_url(session.get("post_login_next")) or url_for("users_list")
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        g.user = user
        log_activity(
            "Manual admin login",
            "auth",
            user["id"],
            user["name"],
            "Environment recovery credential" if valid_recovery_password else "Legacy manager credential",
        )
        flash("Admin access granted. You can now assign Google emails to users.", "success")
        return redirect(next_url)

    return render_template("manual_admin_login.html")


@app.route("/auth/google")
def google_login():
    if current_user():
        return redirect(url_for("dashboard"))
    if google is None:
        if OAuth is None:
            flash("Google login dependency is not installed. Run pip install -r requirements.txt.", "error")
        else:
            flash("Google login is not configured yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "error")
        return redirect(url_for("login"))
    return google.authorize_redirect(_google_redirect_uri())


@app.route("/auth/google/callback")
def google_callback():
    if google is None:
        flash("Google login is not configured.", "error")
        return redirect(url_for("login"))

    try:
        token = google.authorize_access_token()
        userinfo = token.get("userinfo") or google.userinfo(token=token)
    except OAuthError:
        flash("Google sign-in was cancelled or could not be verified. Please try again.", "error")
        return redirect(url_for("login"))
    except Exception:
        app.logger.exception("Google sign-in callback failed")
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("login"))

    email = _normalize_email(userinfo.get("email"))
    google_sub = (userinfo.get("sub") or "").strip()
    email_verified = userinfo.get("email_verified") in {True, "true", "True", 1, "1"}
    if not email or not google_sub or not email_verified:
        flash("Google did not provide a verified email address.", "error")
        return redirect(url_for("login"))

    allowed_domain = os.environ.get("GOOGLE_ALLOWED_DOMAIN", "").strip().lower().lstrip("@")
    if allowed_domain and email.rsplit("@", 1)[-1] != allowed_domain:
        flash(f"Use your @{allowed_domain} Google account.", "error")
        return redirect(url_for("login"))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
    if user and _normalize_email(user["email"]) != email:
        flash("This Google account no longer matches its authorized CRM email. Ask a manager to update it.", "error")
        return redirect(url_for("login"))

    if not user:
        user = db.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if user and user["google_sub"] and user["google_sub"] != google_sub:
            flash("That CRM email is already linked to another Google account.", "error")
            return redirect(url_for("login"))

    # Recovery/migration path for an existing manager whose old username was not
    # an email. It only activates for an explicitly configured bootstrap address.
    if not user and email in _bootstrap_emails():
        user = db.execute(
            "SELECT * FROM users WHERE role='manager' AND (email IS NULL OR TRIM(email)='') ORDER BY id LIMIT 1"
        ).fetchone()
        if not user and db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            display_name = (userinfo.get("name") or email.split("@", 1)[0]).strip()
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, name, role, email, google_sub, last_login_at, created_at) "
                "VALUES (?, 'google-only', ?, 'manager', ?, ?, ?, ?)",
                (email, display_name, email, google_sub, now_str(), now_str()),
            )
            user = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()

    if not user:
        flash("This Google account is not authorized for QuantumLoop CRM. Ask a manager to add its email.", "error")
        return redirect(url_for("login"))

    try:
        db.execute(
            "UPDATE users SET email=?, google_sub=?, last_login_at=? WHERE id=?",
            (email, google_sub, now_str(), user["id"]),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("This Google account conflicts with another CRM user. Ask a manager to correct the email.", "error")
        return redirect(url_for("login"))

    next_url = _safe_next_url(session.get("post_login_next")) or url_for("dashboard")
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["role"] = user["role"]
    g.user = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    log_activity("Login with Google", "auth", user["id"], user["name"])
    return redirect(next_url)


@app.route("/logout")
def logout():
    user = current_user()
    if user:
        log_activity("Logout", "auth", user["id"], user["name"])
    session.clear()
    return redirect(url_for("login"))


# ---------- Users (manager only) ----------

@app.route("/users")
@manager_required
def users_list():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY role, name").fetchall()
    return render_template("users.html", users=users)


@app.route("/users/add", methods=["POST"])
@manager_required
def add_user():
    db = get_db()
    name = request.form.get("name", "").strip()
    email = _normalize_email(request.form.get("email"))
    role = request.form.get("role", "telecaller")

    if not name or not _valid_email(email) or role not in ROLES:
        flash("Enter a name, valid Google email and role.", "error")
        return redirect(url_for("users_list"))

    existing = db.execute(
        "SELECT id FROM users WHERE email=? COLLATE NOCASE OR username=? COLLATE NOCASE",
        (email, email),
    ).fetchone()
    if existing:
        flash(f"Google email '{email}' is already assigned. Update the existing user instead.", "error")
        return redirect(url_for("users_list"))

    db.execute(
        "INSERT INTO users (username, password_hash, name, role, email, created_at) "
        "VALUES (?, 'google-only', ?, ?, ?, ?)",
        (email, name, role, email, now_str()),
    )
    db.commit()
    flash(f"{name} can now sign in with {email}.", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<int:user_id>/google-email", methods=["POST"])
@manager_required
def update_user_google_email(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("users_list"))

    email = _normalize_email(request.form.get("email"))
    if not _valid_email(email):
        flash("Enter a valid Google account email.", "error")
        return redirect(url_for("users_list"))
    existing = db.execute(
        "SELECT id FROM users WHERE email=? COLLATE NOCASE AND id<>?",
        (email, user_id),
    ).fetchone()
    if existing:
        flash(f"Google email '{email}' is already assigned to another user.", "error")
        return redirect(url_for("users_list"))

    old_email = _normalize_email(user["email"])
    unlink_google = old_email != email
    db.execute(
        "UPDATE users SET email=?, google_sub=CASE WHEN ? THEN NULL ELSE google_sub END WHERE id=?",
        (email, 1 if unlink_google else 0, user_id),
    )
    db.commit()
    flash(f"Google login email updated for {user['name']}.", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@manager_required
def delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("You can't delete your own account while logged in.", "error")
        return redirect(url_for("users_list"))
    db = get_db()
    db.execute("UPDATE leads SET owner_id = NULL WHERE owner_id = ?", (user_id,))
    # A project always needs an accountable owner. Move any projects owned by a
    # departing user to the manager performing the removal; task assignees and
    # membership are safely cleared by their foreign-key rules.
    db.execute("UPDATE projects SET project_manager_id=?,updated_at=? WHERE project_manager_id=?",
               (session["user_id"], now_str(), user_id))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash("User removed. Their leads are kept, now unassigned.", "success")
    return redirect(url_for("users_list"))


# ---------- Dashboard ----------

@app.route("/")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)

    stage_counts = {s: 0 for s in STAGES}
    for row in db.execute(f"SELECT stage, COUNT(*) c FROM leads WHERE {clause} GROUP BY stage", params):
        if row["stage"] in stage_counts:
            stage_counts[row["stage"]] = row["c"]

    total_leads = db.execute(f"SELECT COUNT(*) c FROM leads WHERE {clause}", params).fetchone()["c"]

    today = today_str()
    week_ago = (datetime.now(APP_TIMEZONE).date() - timedelta(days=7)).strftime("%Y-%m-%d")

    added_today = db.execute(
        f"SELECT COUNT(*) c FROM leads WHERE {clause} AND date(created_at) = ?", [*params, today]
    ).fetchone()["c"]
    added_week = db.execute(
        f"SELECT COUNT(*) c FROM leads WHERE {clause} AND date(created_at) >= ?", [*params, week_ago]
    ).fetchone()["c"]

    won = stage_counts.get("Won", 0)
    conversion_rate = round((won / total_leads) * 100, 1) if total_leads else 0

    # Leads added per day for the dashboard trend chart (last 30 days, oldest first).
    trend_days = 30
    trend_start = datetime.now(APP_TIMEZONE).date() - timedelta(days=trend_days - 1)
    trend_counts = {
        row["d"]: row["c"]
        for row in db.execute(
            f"SELECT date(created_at) d, COUNT(*) c FROM leads WHERE {clause} AND date(created_at) >= ? GROUP BY d",
            [*params, trend_start.strftime("%Y-%m-%d")],
        )
    }
    trend_max = max(trend_counts.values(), default=0)
    lead_trend = []
    for i in range(trend_days):
        day = trend_start + timedelta(days=i)
        count = trend_counts.get(day.strftime("%Y-%m-%d"), 0)
        lead_trend.append({
            "label": day.strftime("%d %b"),
            "count": count,
            # Chart coordinates in a 0-1000 by 0-240 box; 12 units of headroom above the peak.
            "x": round(i * 1000 / (trend_days - 1), 1),
            "y": round(240 - (count / trend_max) * 228, 1) if trend_max else 240,
        })

    followups_today = db.execute(
        f"""SELECT leads.*, companies.name AS company_name FROM leads
            LEFT JOIN companies ON companies.id = leads.company_id
            WHERE {clause} AND follow_up_date = ? ORDER BY leads.name""", [*params, today]
    ).fetchall()
    followups_overdue = db.execute(
        f"""SELECT leads.*, companies.name AS company_name FROM leads
            LEFT JOIN companies ON companies.id = leads.company_id
            WHERE {clause} AND follow_up_date < ? AND follow_up_date IS NOT NULL AND follow_up_date != ''
            ORDER BY follow_up_date""",
        [*params, today],
    ).fetchall()

    team_stats = None
    monthly_stats = None
    field_visit_stats = None
    field_session_rows = None
    field_outcome_stats = None
    if user["role"] == "manager":
        team_stats = []
        telecallers = db.execute("SELECT * FROM users WHERE role = 'telecaller' ORDER BY name").fetchall()
        for tc in telecallers:
            calls_today = db.execute(
                "SELECT COUNT(*) c FROM call_logs WHERE user_id = ? AND date(created_at) = ?", (tc["id"], today)
            ).fetchone()["c"]
            leads_added = db.execute("SELECT COUNT(*) c FROM leads WHERE owner_id = ?", (tc["id"],)).fetchone()["c"]
            interested = db.execute(
                "SELECT COUNT(*) c FROM leads WHERE owner_id = ? AND stage = 'Interested'", (tc["id"],)
            ).fetchone()["c"]
            qualified = db.execute(
                "SELECT COUNT(*) c FROM leads WHERE owner_id = ? AND stage = 'Qualified'", (tc["id"],)
            ).fetchone()["c"]
            won_c = db.execute(
                "SELECT COUNT(*) c FROM leads WHERE owner_id = ? AND stage = 'Won'", (tc["id"],)
            ).fetchone()["c"]
            team_stats.append({
                "name": tc["name"], "calls_today": calls_today, "leads_added": leads_added,
                "interested": interested, "qualified": qualified, "won": won_c,
            })

        m_start = month_start_str()
        m_total = db.execute("SELECT COUNT(*) c FROM leads WHERE date(created_at) >= ?", (m_start,)).fetchone()["c"]
        m_interested = db.execute("SELECT COUNT(*) c FROM leads WHERE date(created_at) >= ? AND stage='Interested'", (m_start,)).fetchone()["c"]
        m_qualified = db.execute("SELECT COUNT(*) c FROM leads WHERE date(created_at) >= ? AND stage='Qualified'", (m_start,)).fetchone()["c"]
        m_won = db.execute("SELECT COUNT(*) c FROM leads WHERE date(created_at) >= ? AND stage='Won'", (m_start,)).fetchone()["c"]
        monthly_stats = {
            "total_leads": m_total,
            "interested_pct": round(m_interested / m_total * 100, 1) if m_total else 0,
            "qualified_pct": round(m_qualified / m_total * 100, 1) if m_total else 0,
            "won_pct": round(m_won / m_total * 100, 1) if m_total else 0,
            "conversion_rate": round(m_won / m_total * 100, 1) if m_total else 0,
        }

        field_visit_stats = {
            "scheduled_today": db.execute(
                "SELECT COUNT(*) c FROM field_visits WHERE scheduled_date=?", (today,)
            ).fetchone()["c"],
            "completed_today": db.execute(
                "SELECT COUNT(*) c FROM field_visits WHERE date(completed_at)=?", (today,)
            ).fetchone()["c"],
            "review_pending": db.execute(
                "SELECT COUNT(*) c FROM field_visits WHERE review_status='pending'"
            ).fetchone()["c"],
            "followups_due": db.execute(
                "SELECT COUNT(*) c FROM field_visits WHERE follow_up_date<=? AND follow_up_date!='' AND review_status='accepted'",
                (today,),
            ).fetchone()["c"],
        }
        field_session_rows = db.execute(
            """SELECT s.*, users.name AS executive_name,
                      COUNT(v.id) AS visit_count
               FROM field_work_sessions s
               LEFT JOIN users ON users.id=s.user_id
               LEFT JOIN field_visits v ON v.session_id=s.id
               WHERE s.work_date=?
               GROUP BY s.id ORDER BY s.started_at DESC""",
            (today,),
        ).fetchall()
        field_outcome_stats = db.execute(
            """SELECT outcome,COUNT(*) AS count FROM field_visits
               WHERE outcome IS NOT NULL AND outcome!='' AND date(completed_at)>=?
               GROUP BY outcome ORDER BY count DESC,outcome""",
            (m_start,),
        ).fetchall()

    return render_template(
        "dashboard.html",
        stage_counts=stage_counts,
        stages=STAGES,
        total_leads=total_leads,
        added_today=added_today,
        added_week=added_week,
        conversion_rate=conversion_rate,
        lead_trend=lead_trend,
        trend_max=trend_max,
        trend_total=sum(trend_counts.values()),
        followups_today=followups_today,
        followups_overdue=followups_overdue,
        team_stats=team_stats,
        monthly_stats=monthly_stats,
        field_visit_stats=field_visit_stats,
        field_session_rows=field_session_rows,
        field_outcome_stats=field_outcome_stats,
    )


# ---------- Lead list ----------

@app.route("/leads")
@login_required
def leads_list():
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)

    stage_filter = request.args.get("stage", "")
    search = request.args.get("q", "").strip()
    tag_filter = request.args.get("tag", "").strip()
    interest_filter = request.args.get("interest", "").strip()
    mine_filter = request.args.get("mine") == "1"
    handed_to_me = db.execute(
        f"SELECT COUNT(*) FROM leads WHERE {clause} AND followup_manager_id = ?", [*params, user["id"]]).fetchone()[0]

    # Everything except the stage filter, so the stage tabs can show a count for each stage.
    scope = f"""
        FROM leads
        LEFT JOIN users ON leads.owner_id = users.id
        LEFT JOIN companies ON leads.company_id = companies.id
        LEFT JOIN users AS follow_manager ON leads.followup_manager_id = follow_manager.id
        WHERE {clause}
    """
    if mine_filter:
        scope += " AND leads.followup_manager_id = ?"
        params.append(user["id"])
    if interest_filter:
        scope += " AND interest_level = ?"
        params.append(interest_filter)
    if search:
        scope += " AND (leads.name LIKE ? OR leads.phone LIKE ? OR leads.email LIKE ? OR companies.name LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if tag_filter:
        scope += " AND tags LIKE ?"
        params.append(f"%{tag_filter}%")

    stage_counts = {stage: 0 for stage in STAGES}
    for row in db.execute(f"SELECT stage, COUNT(*) c {scope} GROUP BY stage", params):
        if row["stage"] in stage_counts:
            stage_counts[row["stage"]] = row["c"]

    query = f"""SELECT leads.*, users.name AS owner_name, companies.name AS company_name,
        follow_manager.name AS followup_manager_name {scope}"""
    if stage_filter:
        query += " AND stage = ?"
        params.append(stage_filter)
    query += " ORDER BY leads.created_at DESC"

    leads = []
    for row in db.execute(query, params):
        lead = dict(row)
        lead["follow"] = _due_view(row["follow_up_date"], row["stage"] in ("Won", "Lost"))
        lead["initials"] = _initials(row["name"])
        lead["owner_initials"] = _initials(row["owner_name"]) if row["owner_name"] else ""
        leads.append(lead)

    return render_template(
        "leads_list.html",
        leads=leads,
        stages=STAGES,
        stage_filter=stage_filter,
        stage_counts=stage_counts,
        total_all=sum(stage_counts.values()),
        search=search,
        tag_filter=tag_filter,
        interest_filter=interest_filter,
        interest_levels=INTEREST_LEVELS,
        user=user,
        mine_filter=mine_filter,
        handed_to_me=handed_to_me,
    )


# ---------- Kanban ----------

@app.route("/leads/kanban")
@login_required
def leads_kanban():
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)

    query = f"""
        SELECT leads.*, users.name AS owner_name, companies.name AS company_name
        FROM leads
        LEFT JOIN users ON leads.owner_id = users.id
        LEFT JOIN companies ON leads.company_id = companies.id
        WHERE {clause}
        ORDER BY leads.created_at DESC
    """
    leads = db.execute(query, params).fetchall()

    columns = {s: [] for s in STAGES}
    for lead in leads:
        columns.setdefault(lead["stage"], []).append(lead)

    return render_template("kanban.html", columns=columns, stages=STAGES, user=user, time_ago=time_ago)


@app.route("/leads/<int:lead_id>/stage", methods=["POST"])
@login_required
def set_lead_stage(lead_id):
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)
    lead = db.execute(f"SELECT * FROM leads WHERE id = ? AND {clause}", [lead_id, *params]).fetchone()
    if lead is None:
        return jsonify({"ok": False, "error": "Lead not found or not visible to you."}), 404

    stage = request.json.get("stage") if request.is_json else request.form.get("stage")
    if stage not in STAGES:
        return jsonify({"ok": False, "error": "Invalid stage."}), 400
    if stage == "Lost":
        return jsonify({"ok": False, "error": "Open this lead to set a Lost reason before moving it to Lost."}), 400

    old_stage = lead["stage"]
    db.execute("UPDATE leads SET stage = ?, updated_at = ? WHERE id = ?", (stage, now_str(), lead_id))
    db.commit()
    log_lead_event(lead_id, "Stage Changed", f"{old_stage} → {stage}")
    log_activity("Stage Changed", "lead", lead_id, lead["name"], f"{old_stage} → {stage}")
    return jsonify({"ok": True})


# ---------- Company duplicate-check (Add Lead workflow, step 1) ----------

@app.route("/api/companies/check")
@login_required
def api_companies_check():
    db = get_db()
    name = request.args.get("name", "").strip()
    if not name or len(name) < 2:
        return jsonify({"matches": []})
    like = f"%{name}%"
    rows = db.execute(
        "SELECT id, name, industry, city FROM companies WHERE name LIKE ? ORDER BY name LIMIT 8", (like,)
    ).fetchall()
    return jsonify({"matches": [dict(r) for r in rows]})


@app.route("/api/companies/<int:company_id>/contacts")
@login_required
def api_company_contacts(company_id):
    db = get_db()
    rows = db.execute(
        "SELECT id, name, designation, phone, email, is_primary FROM contacts WHERE company_id = ? ORDER BY is_primary DESC, name",
        (company_id,),
    ).fetchall()
    return jsonify({"contacts": [dict(r) for r in rows]})


# ---------- Add lead (new company-centric workflow) ----------

@app.route("/leads/add", methods=["GET", "POST"])
@login_required
def add_lead():
    db = get_db()
    if request.method == "POST":
        user = current_user()
        f = request.form
        next_target = f.get("next_target", "").strip()

        company_id = f.get("company_id", "").strip()
        company_name = f.get("company_name", "").strip()
        contact_id = f.get("contact_id", "").strip()
        contact_name = f.get("contact_name", "").strip()

        if not company_id and not company_name:
            flash("Company name is required.", "error")
            return redirect(url_for("add_lead"))
        if not contact_id and not contact_name:
            flash("Contact name is required.", "error")
            return redirect(url_for("add_lead"))

        # Step 1/2 of the Add Lead workflow: reuse an existing company, or create one.
        gbp_link = f.get("gbp_link", "").strip()
        if company_id:
            company = db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
            if company is None:
                flash("Selected company not found.", "error")
                return redirect(url_for("add_lead"))
            company_id = company["id"]
            if gbp_link and not company["gbp_link"]:
                db.execute("UPDATE companies SET gbp_link = ? WHERE id = ?", (gbp_link, company_id))
        else:
            company = db.execute(
                "SELECT * FROM companies WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) LIMIT 1", (company_name,)
            ).fetchone()
            if company:
                company_id = company["id"]
                company_name = company["name"]
                if gbp_link and not company["gbp_link"]:
                    db.execute("UPDATE companies SET gbp_link=? WHERE id=?", (gbp_link, company_id))
            else:
                cur = db.execute(
                    "INSERT INTO companies (name, industry, gbp_link, status, account_owner_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (company_name, f.get("industry", "").strip(), gbp_link, "Prospect", user["id"], now_str(), now_str()),
                )
                company_id = cur.lastrowid
                log_activity("Company Created", "company", company_id, company_name)

        # Contact: reuse or create
        contact_phone = f.get("phone", "").strip()
        contact_alt_phone = f.get("alternate_phone", "").strip()
        contact_email = f.get("email", "").strip()
        designation = f.get("designation", "").strip()

        if contact_id:
            contact = db.execute("SELECT * FROM contacts WHERE id = ? AND company_id = ?", (contact_id, company_id)).fetchone()
            if contact is None:
                flash("Selected contact not found for this company.", "error")
                return redirect(url_for("add_lead"))
            contact_id = contact["id"]
            contact_name = contact["name"]
            contact_phone = contact_phone or contact["phone"]
            contact_email = contact_email or contact["email"]
            designation = designation or contact["designation"]
        else:
            contact = db.execute(
                """SELECT * FROM contacts WHERE company_id=? AND
                   (LOWER(TRIM(name))=LOWER(TRIM(?)) OR (?!='' AND phone=?) OR (?!='' AND LOWER(email)=LOWER(?)))
                   ORDER BY is_primary DESC LIMIT 1""",
                (company_id, contact_name, contact_phone, contact_phone, contact_email, contact_email),
            ).fetchone()
            if contact:
                contact_id = contact["id"]
                contact_name = contact["name"]
                contact_phone = contact_phone or contact["phone"]
                contact_email = contact_email or contact["email"]
                designation = designation or contact["designation"]
                db.execute(
                    "UPDATE contacts SET designation=?,phone=?,alternate_phone=?,email=?,updated_at=? WHERE id=?",
                    (designation, contact_phone, contact_alt_phone or contact["alternate_phone"], contact_email, now_str(), contact_id),
                )
            else:
                cur = db.execute(
                    "INSERT INTO contacts (company_id, name, designation, phone, alternate_phone, email, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (company_id, contact_name, designation, contact_phone, contact_alt_phone, contact_email, 0, now_str()),
                )
                contact_id = cur.lastrowid
                log_activity("Contact Created", "contact", contact_id, contact_name)

        existing_lead = db.execute(
            """SELECT id,name FROM leads WHERE company_id=? AND
               (contact_id=? OR (?!='' AND phone=?)) ORDER BY id DESC LIMIT 1""",
            (company_id, contact_id, contact_phone, contact_phone),
        ).fetchone()
        if existing_lead:
            db.commit()
            flash(f"An existing lead for '{existing_lead['name']}' was reused to avoid a duplicate.", "success")
            if next_target == "field_visit" and user["role"] == "field_sales":
                return redirect(url_for("field_visit_new", lead_id=existing_lead["id"]))
            return redirect(url_for("lead_detail", lead_id=existing_lead["id"]))

        services = f.getlist("services_required")
        other_service = f.get("other_service_description", "").strip() if "Other" in services else ""

        cur = db.execute(
            """INSERT INTO leads (
                name, phone, email, source, stage, tags, follow_up_date, owner_id, created_at,
                company_id, contact_id, industry, designation, alternate_phone,
                website_status, website_url, website_quality, google_presence, social_presence,
                services_required, other_service_description, decision_maker, interest_level, priority,
                budget_range, estimated_project_value, expected_timeline,
                qualification_notes, requirement_notes, internal_notes, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?,?)""",
            (
                contact_name, contact_phone, contact_email, f.get("source", "").strip(), "New",
                f.get("tags", "").strip(), f.get("follow_up_date", "").strip(), user["id"], now_str(),
                company_id, contact_id, f.get("industry", "").strip(), designation, contact_alt_phone,
                f.get("website_status", ""), f.get("website_url", "").strip(), f.get("website_quality", ""),
                f.get("google_presence", ""), f.get("social_presence", ""),
                ",".join(services), other_service, f.get("decision_maker", ""), f.get("interest_level", ""),
                f.get("priority", ""), f.get("budget_range", ""), f.get("estimated_project_value", "").strip(),
                f.get("expected_timeline", "").strip(), f.get("qualification_notes", "").strip(),
                f.get("requirement_notes", "").strip(), f.get("internal_notes", "").strip(), now_str(),
            ),
        )
        db.commit()
        lead_id = cur.lastrowid
        log_lead_event(lead_id, "Lead Created")
        log_activity("Lead Created", "lead", lead_id, contact_name, f"Company: {company_name or company_id}")
        flash(f"Lead for '{contact_name}' added.", "success")
        if next_target == "field_visit" and user["role"] == "field_sales":
            return redirect(url_for("field_visit_new", lead_id=lead_id))
        return redirect(url_for("lead_detail", lead_id=lead_id))

    companies = db.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    return render_template(
        "add_lead.html", stages=STAGES, companies=companies, services=SERVICES,
        website_status_opts=WEBSITE_STATUS, website_quality_opts=WEBSITE_QUALITY,
        google_presence_opts=GOOGLE_PRESENCE, social_presence_opts=SOCIAL_PRESENCE,
        decision_maker_opts=DECISION_MAKER_OPTIONS, interest_levels=INTEREST_LEVELS,
        priorities=PRIORITIES, budget_ranges=BUDGET_RANGES,
        next_target=request.args.get("next", ""),
    )


# ---------- CSV import ----------

REQUIRED_IMPORT_COLS = {"name", "phone", "company_name"}
OPTIONAL_IMPORT_COLS = {
    "email", "source", "tags", "industry", "designation", "alternate_phone",
    "interest_level", "priority", "budget_range", "decision_maker",
    "follow_up_date", "expected_timeline", "estimated_project_value",
    "requirement_notes", "internal_notes", "qualification_notes",
}


@app.route("/leads/import", methods=["POST"])
@login_required
def import_leads():
    db = get_db()
    user = current_user()
    file = request.files.get("csv_file")

    if not file or file.filename == "":
        flash("Please choose a CSV file to import.", "error")
        return redirect(url_for("leads_list"))

    if not file.filename.lower().endswith(".csv"):
        flash("That file doesn't look like a CSV. Export as .csv and try again.", "error")
        return redirect(url_for("leads_list"))

    try:
        raw = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Couldn't read that file — please save it as UTF-8 CSV and try again.", "error")
        return redirect(url_for("leads_list"))

    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        flash("That CSV looks empty.", "error")
        return redirect(url_for("leads_list"))

    headers = {(h or "").strip().lower(): h for h in reader.fieldnames}
    missing = REQUIRED_IMPORT_COLS - set(headers.keys())
    if missing:
        flash(
            f"CSV is missing required column(s): {', '.join(sorted(missing))}. "
            f"Download the template below to see the expected format.",
            "error",
        )
        return redirect(url_for("leads_list"))

    def normalize_choice(value, valid_options):
        """Case-insensitive match against a controlled option list; blank if no match."""
        if not value:
            return ""
        for opt in valid_options:
            if opt.lower() == value.lower():
                return opt
        return ""

    imported = 0
    skipped_rows = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        def get(col):
            key = headers.get(col)
            return (row.get(key) or "").strip() if key else ""

        name = get("name")
        phone = get("phone")
        company_name = get("company_name")
        if not name or not phone or not company_name:
            skipped_rows.append(str(i))
            continue

        # Reuse an existing company by name (case-insensitive), or create one — same as Add Lead.
        company = db.execute(
            "SELECT * FROM companies WHERE LOWER(name) = LOWER(?)", (company_name,)
        ).fetchone()
        industry = get("industry")
        if company is None:
            cur = db.execute(
                "INSERT INTO companies (name, industry, status, account_owner_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (company_name, industry, "Prospect", user["id"], now_str(), now_str()),
            )
            company_id = cur.lastrowid
            log_activity("Company Created", "company", company_id, company_name, "Imported via CSV")
        else:
            company_id = company["id"]

        designation = get("designation")
        alt_phone = get("alternate_phone")
        email = get("email")

        cur = db.execute(
            "INSERT INTO contacts (company_id, name, designation, phone, alternate_phone, email, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (company_id, name, designation, phone, alt_phone, email, 0, now_str()),
        )
        contact_id = cur.lastrowid

        cur = db.execute(
            """INSERT INTO leads (
                name, phone, email, source, stage, tags, follow_up_date, owner_id, created_at,
                company_id, contact_id, industry, designation, alternate_phone,
                decision_maker, interest_level, priority, budget_range,
                estimated_project_value, expected_timeline,
                qualification_notes, requirement_notes, internal_notes, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?)""",
            (
                name, phone, email, get("source"), "New", get("tags"), get("follow_up_date"),
                user["id"], now_str(),
                company_id, contact_id, industry, designation, alt_phone,
                normalize_choice(get("decision_maker"), DECISION_MAKER_OPTIONS),
                normalize_choice(get("interest_level"), INTEREST_LEVELS),
                normalize_choice(get("priority"), PRIORITIES),
                normalize_choice(get("budget_range"), BUDGET_RANGES),
                get("estimated_project_value"), get("expected_timeline"),
                get("qualification_notes"), get("requirement_notes"), get("internal_notes"), now_str(),
            ),
        )
        log_lead_event(cur.lastrowid, "Lead Created", "Imported via CSV")
        imported += 1

    db.commit()
    if imported:
        log_activity("Lead Created", "lead", None, "", f"Imported {imported} lead(s) via CSV")
        msg = f"Imported {imported} lead(s)."
        if skipped_rows:
            msg += f" Skipped {len(skipped_rows)} row(s) missing name/phone/company_name (row {', '.join(skipped_rows)})."
        flash(msg, "success")
    else:
        flash(f"No leads imported — every row was missing a name, phone, or company_name (rows {', '.join(skipped_rows)}).", "error")

    return redirect(url_for("leads_list"))


@app.route("/leads/template")
@login_required
def download_template():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads Template"

    headers = [
        "name", "phone", "company_name", "email", "source", "tags",
        "industry", "designation", "alternate_phone",
        "interest_level", "priority", "budget_range", "decision_maker",
        "follow_up_date", "expected_timeline", "estimated_project_value",
        "requirement_notes", "internal_notes", "qualification_notes",
    ]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="252D52", end_color="252D52", fill_type="solid")

    example = [
        "Ravi Kumar", "9876543210", "Acme Traders", "ravi@example.com", "Facebook", "hot, budget",
        "Retail", "Owner", "9876500000",
        "Hot", "High", "50k-1L", "Yes",
        "2026-08-20", "1-2 months", "75000",
        "Needs an online store", "Called twice, interested", "Budget confirmed",
    ]
    for col, value in enumerate(example, start=1):
        cell = ws.cell(row=2, column=col, value=value)
        cell.font = Font(name="Arial", italic=True, color="9497B5")

    ws.cell(row=4, column=1, value="name, phone, and company_name are required for every row. All other columns are optional.").font = Font(name="Arial", size=10, italic=True)
    ws.cell(row=5, column=1, value=f"interest_level must be one of: {', '.join(INTEREST_LEVELS)}").font = Font(name="Arial", size=10, italic=True)
    ws.cell(row=6, column=1, value=f"priority must be one of: {', '.join(PRIORITIES)}").font = Font(name="Arial", size=10, italic=True)
    ws.cell(row=7, column=1, value=f"budget_range must be one of: {', '.join(BUDGET_RANGES)}").font = Font(name="Arial", size=10, italic=True)
    ws.cell(row=8, column=1, value="A company_name that already exists will be reused; otherwise a new company is created.").font = Font(name="Arial", size=10, italic=True)
    ws.cell(row=9, column=1, value="Delete the example row (row 2) before importing your real leads.").font = Font(name="Arial", size=10, italic=True)

    widths = [18, 14, 20, 24, 14, 18, 14, 16, 16, 12, 10, 12, 14, 14, 16, 20, 22, 22, 22]
    for col, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + col)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment;filename=leads_import_template.xlsx"},
    )


# ---------- Export CSV ----------

@app.route("/leads/export")
@login_required
def export_leads():
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)
    leads = db.execute(
        f"""SELECT leads.*, companies.name AS company_name FROM leads
            LEFT JOIN companies ON companies.id = leads.company_id
            WHERE {clause} ORDER BY leads.created_at DESC""", params
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["company", "contact_name", "phone", "email", "source", "stage", "interest_level",
                      "priority", "budget_range", "tags", "follow_up_date", "created_at"])
    for l in leads:
        writer.writerow([l["company_name"], l["name"], l["phone"], l["email"], l["source"], l["stage"],
                          l["interest_level"], l["priority"], l["budget_range"], l["tags"],
                          l["follow_up_date"], l["created_at"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=leads_export.csv"},
    )


# ---------- Lead detail ----------

@app.route("/leads/<int:lead_id>")
@login_required
def lead_detail(lead_id):
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)
    lead = db.execute(
        f"""
        SELECT leads.*, users.name AS owner_name, companies.name AS company_name, companies.id AS company_pk
        FROM leads
        LEFT JOIN users ON leads.owner_id = users.id
        LEFT JOIN companies ON leads.company_id = companies.id
        WHERE leads.id = ? AND {clause}
        """,
        [lead_id, *params],
    ).fetchone()
    if lead is None:
        flash("Lead not found, or it's not visible to your account.", "error")
        return redirect(url_for("leads_list"))

    contact = None
    if lead["contact_id"]:
        contact = db.execute("SELECT * FROM contacts WHERE id = ?", (lead["contact_id"],)).fetchone()

    company = None
    company_contacts = []
    if lead["company_id"]:
        company = db.execute("SELECT * FROM companies WHERE id = ?", (lead["company_id"],)).fetchone()
        company_contacts = db.execute(
            "SELECT * FROM contacts WHERE company_id = ? ORDER BY is_primary DESC, name", (lead["company_id"],)
        ).fetchall()

    call_logs = db.execute(
        "SELECT * FROM call_logs WHERE lead_id = ? ORDER BY created_at DESC", (lead_id,)
    ).fetchall()

    timeline_events = db.execute(
        "SELECT * FROM lead_activities WHERE lead_id = ? ORDER BY created_at DESC, id DESC", (lead_id,)
    ).fetchall()

    services_selected = (lead["services_required"] or "").split(",") if lead["services_required"] else []
    telecallers = db.execute("SELECT id, name FROM users WHERE role = 'telecaller' ORDER BY name").fetchall()

    # Opening the lead clears its "asked you to follow up" notification.
    if db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND lead_id=? AND read_at IS NULL",
                  (now_str(), user["id"], lead_id)).rowcount:
        db.commit()
        _live_touch()
    followup = None
    if lead["followup_manager_id"]:
        names = {r["id"]: r["name"] for r in db.execute(
            "SELECT id, name FROM users WHERE id IN (?, ?)", (lead["followup_manager_id"], lead["followup_requested_by"] or 0))}
        followup = {
            "manager": names.get(lead["followup_manager_id"], "a manager"),
            "requester": names.get(lead["followup_requested_by"], "A former user"),
            "for_me": lead["followup_manager_id"] == user["id"],
            "by_me": lead["followup_requested_by"] == user["id"],
            "note": lead["followup_note"], "date": lead["follow_up_date"],
        }
    followup_managers = db.execute(
        "SELECT id, name FROM users WHERE role = 'manager' AND id <> ? ORDER BY name", (user["id"],)).fetchall()

    is_owner = lead["owner_id"] == user["id"]
    shared_with = db.execute(
        """SELECT lead_shares.user_id, users.name, users.role
           FROM lead_shares JOIN users ON lead_shares.user_id = users.id
           WHERE lead_shares.lead_id = ? ORDER BY users.name""",
        (lead_id,),
    ).fetchall()
    shareable_users = []
    if is_owner:
        already_shared_ids = {row["user_id"] for row in shared_with}
        if user["role"] == "manager":
            # Managers already see every telecaller's leads automatically, so sharing
            # to a telecaller here is only useful for handing a manager's own lead to
            # a telecaller to work; sharing to another manager is also allowed.
            all_users = db.execute("SELECT id, name, role FROM users ORDER BY name").fetchall()
        else:
            # Telecallers can only send leads to other telecallers. Managers already
            # see every telecaller lead, so a telecaller "sharing" to a manager would
            # be meaningless — leave managers out of the list entirely.
            all_users = db.execute("SELECT id, name, role FROM users WHERE role = 'telecaller' ORDER BY name").fetchall()
        shareable_users = [u for u in all_users if u["id"] != user["id"] and u["id"] not in already_shared_ids]

    return render_template(
        "lead_detail.html", lead=lead, contact=contact, company=company, company_contacts=company_contacts,
        call_logs=call_logs, stages=STAGES,
        timeline_events=timeline_events, services=SERVICES, services_selected=services_selected,
        website_status_opts=WEBSITE_STATUS, website_quality_opts=WEBSITE_QUALITY,
        google_presence_opts=GOOGLE_PRESENCE, social_presence_opts=SOCIAL_PRESENCE,
        decision_maker_opts=DECISION_MAKER_OPTIONS, interest_levels=INTEREST_LEVELS,
        priorities=PRIORITIES, budget_ranges=BUDGET_RANGES, lost_reasons=LOST_REASONS,
        telecallers=telecallers, is_owner=is_owner, shared_with=shared_with, shareable_users=shareable_users,
        followup=followup, followup_managers=followup_managers,
    )


@app.route("/leads/<int:lead_id>/link-company", methods=["POST"])
@login_required
def link_company_to_lead(lead_id):
    db = get_db()
    user = current_user()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))

    f = request.form
    company_id = f.get("company_id", "").strip()
    company_name = f.get("company_name", "").strip()

    if not company_id and not company_name:
        flash("Enter a company name to link.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    if company_id:
        company = db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if company is None:
            flash("Selected company not found.", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        company_id = company["id"]
    else:
        cur = db.execute(
            "INSERT INTO companies (name, status, account_owner_id, created_at, updated_at) VALUES (?,?,?,?,?)",
            (company_name, "Prospect", user["id"], now_str(), now_str()),
        )
        company_id = cur.lastrowid
        log_activity("Company Created", "company", company_id, company_name)

    # This lead already has a name/phone/email of its own — turn that into (or reuse) a
    # contact record under the newly linked company, so the contacts list is populated too.
    contact_id = lead["contact_id"]
    if not contact_id:
        cur = db.execute(
            "INSERT INTO contacts (company_id, name, designation, phone, alternate_phone, email, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (company_id, lead["name"], lead["designation"], lead["phone"], lead["alternate_phone"], lead["email"], 1, now_str()),
        )
        contact_id = cur.lastrowid
        log_activity("Contact Created", "contact", contact_id, lead["name"])

    db.execute("UPDATE leads SET company_id = ?, contact_id = ?, updated_at = ? WHERE id = ?", (company_id, contact_id, now_str(), lead_id))
    db.commit()
    log_lead_event(lead_id, "Linked to Company", company_name or "")
    flash("Lead linked to company.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/update", methods=["POST"])
@login_required
def update_lead(lead_id):
    db = get_db()
    user = current_user()
    f = request.form
    clause, params = visible_leads_clause(user)
    lead = db.execute(f"SELECT * FROM leads WHERE id = ? AND {clause}", [lead_id, *params]).fetchone()
    if lead is None:
        flash("Lead not found, or it's not visible to your account.", "error")
        return redirect(url_for("leads_list"))

    stage = f.get("stage", lead["stage"])
    lost_reason = f.get("lost_reason", "").strip()
    if stage == "Lost" and not lost_reason:
        flash("Please choose a Lost reason before setting this lead to Lost.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    if stage != "Lost":
        lost_reason = ""

    services = f.getlist("services_required")
    other_service = f.get("other_service_description", "").strip() if "Other" in services else ""

    db.execute(
        """UPDATE leads SET
            stage=?, tags=?, follow_up_date=?, industry=?, designation=?, alternate_phone=?,
            website_status=?, website_url=?, website_quality=?, google_presence=?, social_presence=?,
            services_required=?, other_service_description=?, decision_maker=?, interest_level=?, priority=?,
            budget_range=?, estimated_project_value=?, expected_timeline=?, lost_reason=?,
            qualification_notes=?, requirement_notes=?, internal_notes=?, updated_at=?
        WHERE id=?""",
        (
            stage, f.get("tags", "").strip(), f.get("follow_up_date", "").strip(),
            f.get("industry", "").strip(), f.get("designation", "").strip(), f.get("alternate_phone", "").strip(),
            f.get("website_status", ""), f.get("website_url", "").strip(), f.get("website_quality", ""),
            f.get("google_presence", ""), f.get("social_presence", ""),
            ",".join(services), other_service, f.get("decision_maker", ""), f.get("interest_level", ""),
            f.get("priority", ""), f.get("budget_range", ""), f.get("estimated_project_value", "").strip(),
            f.get("expected_timeline", "").strip(), lost_reason,
            f.get("qualification_notes", "").strip(), f.get("requirement_notes", "").strip(),
            f.get("internal_notes", "").strip(), now_str(), lead_id,
        ),
    )
    db.commit()

    if stage != lead["stage"]:
        detail = f"{lead['stage']} → {stage}" + (f" ({lost_reason})" if stage == "Lost" else "")
        log_lead_event(lead_id, "Stage Changed", detail)
        log_activity("Stage Changed", "lead", lead_id, lead["name"], detail)
    else:
        log_lead_event(lead_id, "Lead Edited")
        log_activity("Lead Edited", "lead", lead_id, lead["name"])

    flash("Lead updated.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/call", methods=["POST"])
@login_required
def add_call_log(lead_id):
    db = get_db()
    user = current_user()
    clause, params = visible_leads_clause(user)
    lead_ok = db.execute(f"SELECT 1 FROM leads WHERE id = ? AND {clause}", [lead_id, *params]).fetchone()
    if lead_ok is None:
        flash("Lead not found, or it's not visible to your account.", "error")
        return redirect(url_for("leads_list"))
    outcome = request.form.get("outcome", "").strip()
    note = request.form.get("note", "").strip()

    db.execute(
        "INSERT INTO call_logs (lead_id, outcome, note, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (lead_id, outcome, note, user["id"], now_str()),
    )
    db.commit()
    log_lead_event(lead_id, "Called", f"{outcome}" + (f" — {note}" if note else ""))
    lead = db.execute("SELECT name FROM leads WHERE id=?", (lead_id,)).fetchone()
    log_activity("Lead Edited", "lead", lead_id, lead["name"] if lead else "", f"Call logged: {outcome}")
    flash("Call logged.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
@login_required
def delete_lead(lead_id):
    db = get_db()
    user = current_user()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    if lead["owner_id"] != user["id"] and user["role"] != "manager":
        flash("Only the owner or a manager can delete this lead.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    db.execute("DELETE FROM lead_shares WHERE lead_id = ?", (lead_id,))
    db.execute("DELETE FROM call_logs WHERE lead_id = ?", (lead_id,))
    db.execute("DELETE FROM lead_activities WHERE lead_id = ?", (lead_id,))
    db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    db.commit()
    log_activity("Lead Deleted", "lead", lead_id, lead["name"] if lead else "")
    flash("Lead deleted.", "success")
    return redirect(url_for("leads_list"))


@app.route("/leads/<int:lead_id>/assign", methods=["POST"])
@manager_required
def assign_lead(lead_id):
    """Sets initial ownership for a lead that doesn't have an owner yet (e.g. a
    freshly imported/unassigned lead). Once a lead has an owner, ownership stays
    put — use /leads/<id>/share to grant another person view+edit access instead."""
    db = get_db()
    lead = db.execute("SELECT name, owner_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    if lead["owner_id"]:
        flash("This lead already has an owner. Use \u201cShare access\u201d to give someone else access instead of reassigning it.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    owner_id = request.form.get("owner_id") or None
    db.execute("UPDATE leads SET owner_id = ? WHERE id = ?", (owner_id, lead_id))
    db.commit()
    owner = db.execute("SELECT name FROM users WHERE id = ?", (owner_id,)).fetchone() if owner_id else None
    log_lead_event(lead_id, "Lead Assigned", owner["name"] if owner else "Unassigned")
    log_activity("Lead Assigned", "lead", lead_id, lead["name"] if lead else "", owner["name"] if owner else "Unassigned")
    flash("Lead assignment updated.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


def _visible_lead(db, user, lead_id):
    clause, params = visible_leads_clause(user)
    return db.execute(f"SELECT leads.* FROM leads WHERE leads.id = ? AND {clause}", [lead_id, *params]).fetchone()


@app.post("/leads/<int:lead_id>/request-followup")
@login_required
def request_lead_followup(lead_id):
    """Hand a lead's next follow-up to a manager, for example after a call.

    The lead moves to the Follow Up stage with a follow-up date, the manager is notified,
    and it shows under "Handed to me" in their lead list. Ownership does not change.
    """
    db, user = get_db(), current_user()
    lead = _visible_lead(db, user, lead_id)
    if lead is None:
        flash("Lead not found, or it's not visible to your account.", "error")
        return redirect(url_for("leads_list"))
    back = redirect(url_for("lead_detail", lead_id=lead_id))
    manager = db.execute("SELECT id, name FROM users WHERE id = ? AND role = 'manager'",
                         (request.form.get("manager_id", type=int),)).fetchone()
    if manager is None or manager["id"] == user["id"]:
        flash("Choose a manager to follow up on this lead.", "error")
        return back
    try:
        when = date.fromisoformat(request.form.get("follow_up_date", "") or today_str()).isoformat()
    except ValueError:
        flash("Enter a valid follow-up date.", "error")
        return back
    if when < today_str():
        flash("Choose a follow-up date of today or later.", "error")
        return back
    note = " ".join(request.form.get("note", "").split())[:500]
    stage = lead["stage"] if lead["stage"] in ("Won", "Lost") else "Follow Up"
    db.execute(
        """UPDATE leads SET stage=?, follow_up_date=?, followup_manager_id=?, followup_requested_by=?,
           followup_note=?, followup_requested_at=?, updated_at=? WHERE id=?""",
        (stage, when, manager["id"], user["id"], note, now_str(), now_str(), lead_id))
    # A new request replaces any earlier unread one for this lead.
    db.execute("DELETE FROM notifications WHERE lead_id=? AND read_at IS NULL", (lead_id,))
    db.execute(
        "INSERT INTO notifications (user_id, kind, lead_id, actor_id, title, body, created_at) VALUES (?,?,?,?,?,?,?)",
        (manager["id"], "lead_followup", lead_id, user["id"], f"{user['name']} asked you to follow up",
         f"{lead['name']}: {note}" if note else lead["name"], now_str()))
    db.commit()
    _live_touch()
    detail = f"Handed to {manager['name']} for {when}" + (f": {note}" if note else "")
    log_lead_event(lead_id, "Follow-up Requested", detail)
    log_activity("Follow-up Requested", "lead", lead_id, lead["name"], detail)
    flash(f"{manager['name']} has been asked to follow up on {when}.", "success")
    return back


@app.post("/leads/<int:lead_id>/followup-done")
@login_required
def finish_lead_followup(lead_id):
    """Close a handed-over follow-up. The manager it was handed to, any manager, or the
    person who asked can close it."""
    db, user = get_db(), current_user()
    lead = _visible_lead(db, user, lead_id)
    if lead is None:
        flash("Lead not found, or it's not visible to your account.", "error")
        return redirect(url_for("leads_list"))
    if not lead["followup_manager_id"]:
        return redirect(url_for("lead_detail", lead_id=lead_id))
    if user["role"] != "manager" and lead["followup_requested_by"] != user["id"]:
        flash("Only a manager or the person who asked can close this follow-up request.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    done = user["role"] == "manager"
    db.execute("""UPDATE leads SET followup_manager_id=NULL, followup_requested_by=NULL, followup_note=NULL,
                  followup_requested_at=NULL, updated_at=? WHERE id=?""", (now_str(), lead_id))
    db.execute("UPDATE notifications SET read_at=? WHERE lead_id=? AND read_at IS NULL", (now_str(), lead_id))
    db.commit()
    _live_touch()
    log_lead_event(lead_id, "Follow-up Handled" if done else "Follow-up Request Withdrawn", "")
    flash("Follow-up marked as handled." if done else "Follow-up request withdrawn.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/share", methods=["POST"])
@login_required
def share_lead(lead_id):
    """Grants another user (telecaller or manager) view+edit access to this lead,
    without changing who owns it and without creating a copy. Only the lead's
    owner can grant access to it."""
    db = get_db()
    user = current_user()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    if lead["owner_id"] != user["id"]:
        flash("Only the lead's owner can share access to it.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    grantee_id = request.form.get("user_id")
    if not grantee_id:
        flash("Choose a person to share this lead with.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    grantee_id = int(grantee_id)
    if grantee_id == user["id"]:
        flash("You already have access to your own lead.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    grantee = db.execute("SELECT id, name, role FROM users WHERE id = ?", (grantee_id,)).fetchone()
    if grantee is None:
        flash("That user doesn't exist.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    if user["role"] == "telecaller" and grantee["role"] == "manager":
        flash("Telecallers can only share leads with other telecallers — managers already see every telecaller's leads.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    db.execute(
        "INSERT OR IGNORE INTO lead_shares (lead_id, user_id, granted_by, created_at) VALUES (?, ?, ?, ?)",
        (lead_id, grantee_id, user["id"], now_str()),
    )
    db.commit()
    log_lead_event(lead_id, "Access Granted", grantee["name"])
    log_activity("Access Granted", "lead", lead_id, lead["name"], f"Shared with {grantee['name']}")
    flash(f"{grantee['name']} can now see and edit this lead.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/unshare/<int:user_id>", methods=["POST"])
@login_required
def unshare_lead(lead_id, user_id):
    """Revokes a previously granted access. Only the lead's owner can revoke it."""
    db = get_db()
    user = current_user()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    if lead["owner_id"] != user["id"]:
        flash("Only the lead's owner can revoke access.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    revoked = db.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    db.execute("DELETE FROM lead_shares WHERE lead_id = ? AND user_id = ?", (lead_id, user_id))
    db.commit()
    log_lead_event(lead_id, "Access Revoked", revoked["name"] if revoked else "")
    log_activity("Access Revoked", "lead", lead_id, lead["name"], f"Revoked from {revoked['name']}" if revoked else "")
    flash("Access revoked.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


# ---------- Project management ----------

@app.route("/projects")
@login_required
def projects_list():
    db = get_db()
    user = current_user()
    clause, params = visible_projects_clause(user, "p")
    clients = db.execute(
        f"""SELECT DISTINCT c.id, c.name FROM projects p JOIN companies c ON c.id=p.company_id
            WHERE {clause} ORDER BY c.name COLLATE NOCASE""", params).fetchall()
    type_filter, client_filter = request.args.get("type", ""), request.args.get("client", "")
    if type_filter in PROJECT_TYPES:
        clause, params = clause + " AND p.project_type=?", [*params, type_filter]
    else:
        type_filter = ""
    if client_filter == "internal":
        clause += " AND p.company_id IS NULL"
    elif client_filter.isdigit():
        clause, params = clause + " AND p.company_id=?", [*params, int(client_filter)]
    else:
        client_filter = ""
    projects = db.execute(
        f"""SELECT p.*, pm.name AS project_manager_name, l.name AS lead_name, c.name AS company_name,
                   COUNT(t.id) AS task_count, SUM(CASE WHEN t.status='Complete' THEN 1 ELSE 0 END) AS completed_task_count
            FROM projects p LEFT JOIN users pm ON pm.id=p.project_manager_id
            LEFT JOIN leads l ON l.id=p.lead_id LEFT JOIN companies c ON c.id=p.company_id
            LEFT JOIN work_tasks t ON t.project_id=p.id WHERE {clause}
            GROUP BY p.id
            ORDER BY CASE p.status WHEN 'In Progress' THEN 0 WHEN 'Planning' THEN 1 WHEN 'On Hold' THEN 2 ELSE 3 END,
                     p.target_date IS NULL, p.target_date, p.updated_at DESC""", params,
    ).fetchall()
    summary = {
        "active": sum(1 for p in projects if p["status"] in {"Planning", "In Progress", "Client Review"}),
        "at_risk": sum(1 for p in projects if p["target_date"] and p["target_date"] < today_str() and p["status"] not in {"Completed", "Cancelled"}),
        "completed": sum(1 for p in projects if p["status"] == "Completed"),
        "tasks": sum(p["task_count"] or 0 for p in projects),
    }
    view = session.get("projects_view", "cards")
    requested_view = request.args.get("view")
    if requested_view in ("cards", "details") and requested_view != view:
        view = session["projects_view"] = requested_view
    return render_template("projects.html", projects=projects, summary=summary, view=view, clients=clients,
                           project_types=PROJECT_TYPES, type_filter=type_filter, client_filter=client_filter)


@app.route("/projects/new", methods=["GET", "POST"])
@manager_required
def project_new():
    db = get_db()
    user = current_user()
    won_leads = db.execute("""SELECT l.id,l.name,l.company_id,c.name AS company_name FROM leads l
        LEFT JOIN companies c ON c.id=l.company_id WHERE l.stage='Won' ORDER BY c.name,l.name""").fetchall()
    team_users = db.execute("SELECT id,name,role FROM users ORDER BY name").fetchall()
    selected_lead_id = request.values.get("lead_id", type=int)
    if request.method == "POST":
        _check_task_token()
        name = request.form.get("name", "").strip()
        lead_id = request.form.get("lead_id", type=int)
        manager_id = request.form.get("project_manager_id", type=int)
        status, priority = request.form.get("status", "Planning"), request.form.get("priority", "Medium")
        project_type = request.form.get("project_type", "")
        if not name or not manager_id or status not in PROJECT_STATUSES or priority not in PRIORITIES:
            flash("Enter a project name, owner, valid status and priority.", "error")
            return redirect(url_for("project_new", lead_id=lead_id or ""))
        if project_type not in PROJECT_TYPES:
            flash("Choose a project type.", "error")
            return redirect(url_for("project_new", lead_id=lead_id or ""))
        if not db.execute("SELECT id FROM users WHERE id=?", (manager_id,)).fetchone():
            flash("Choose a valid project owner.", "error")
            return redirect(url_for("project_new"))
        lead = None
        if lead_id:
            lead = db.execute("SELECT id,company_id,stage FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not lead or lead["stage"] != "Won":
                flash("Projects can only be linked to a won lead.", "error")
                return redirect(url_for("project_new"))
        # A won lead decides the client; otherwise the manager picks one, or leaves it internal.
        company_id = request.form.get("company_id", type=int)
        if lead and lead["company_id"]:
            company_id = lead["company_id"]
        elif company_id and not db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
            flash("Choose a valid client company.", "error")
            return redirect(url_for("project_new", lead_id=lead_id or ""))
        try:
            budget_raw = request.form.get("budget", "").strip()
            budget = float(budget_raw) if budget_raw else None
        except ValueError:
            flash("Budget must be a valid number.", "error")
            return redirect(url_for("project_new", lead_id=lead_id or ""))
        now = now_str()
        cursor = db.execute("""INSERT INTO projects
            (name,lead_id,company_id,project_type,project_manager_id,status,priority,start_date,target_date,budget,description,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, lead_id or None, company_id or None, project_type, manager_id, status, priority,
             request.form.get("start_date") or None, request.form.get("target_date") or None, budget,
             request.form.get("description", "").strip(), user["id"], now, now))
        project_id = cursor.lastrowid
        selected_ids = {manager_id}
        for value in request.form.getlist("member_ids"):
            try: selected_ids.add(int(value))
            except ValueError: pass
        valid_ids = {row["id"] for row in team_users}
        for member_id in selected_ids & valid_ids:
            db.execute("INSERT INTO project_members (project_id,user_id,added_at) VALUES (?,?,?)", (project_id, member_id, now))
        log_project_event(db, project_id, "Project created", f"Owner: {manager_id}; team: {len(selected_ids & valid_ids)}")
        db.commit()
        log_activity("Created project", "project", project_id, name, "Project management")
        flash("Project created and team assigned.", "success")
        return redirect(url_for("project_detail", project_id=project_id))
    return render_template("project_form.html", won_leads=won_leads, team_users=team_users,
        companies=_project_client_options(db), project_types=PROJECT_TYPES,
        statuses=PROJECT_STATUSES, priorities=PRIORITIES, selected_lead_id=selected_lead_id,
        selected_company_id=next((lead["company_id"] for lead in won_leads if lead["id"] == selected_lead_id), None),
        project=None, member_ids={user["id"]}, token=_task_token())


def _project_client_options(db):
    return db.execute("SELECT id,name FROM companies ORDER BY name COLLATE NOCASE").fetchall()


def _project_detail_row(db, project_id, user):
    clause, params = visible_projects_clause(user, "p")
    return db.execute(f"""SELECT p.*,pm.name AS project_manager_name,l.name AS lead_name,c.name AS company_name
        FROM projects p LEFT JOIN users pm ON pm.id=p.project_manager_id LEFT JOIN leads l ON l.id=p.lead_id
        LEFT JOIN companies c ON c.id=p.company_id WHERE p.id=? AND {clause}""", [project_id, *params]).fetchone()


@app.route("/projects/<int:project_id>", methods=["GET", "POST"])
@login_required
def project_detail(project_id):
    db = get_db()
    user = current_user()
    if request.method == "POST":
        expected = session.get("project_note_token")
        if not expected or not hmac.compare_digest(expected.encode(), request.form.get("token", "").encode()):
            abort(403)
        # Check assignment and revision in the same transaction as the write.
        db.execute("BEGIN IMMEDIATE")
    project = _project_detail_row(db, project_id, user)
    if not project:
        if request.method == "POST":
            abort(403)
        flash("Project not found, or it is not assigned to you.", "error")
        return redirect(url_for("projects_list"))
    members = db.execute("SELECT u.id,u.name,u.role FROM project_members pm JOIN users u ON u.id=pm.user_id WHERE pm.project_id=? ORDER BY u.name", (project_id,)).fetchall()
    tasks = _project_task_views(db, project_id)
    every_task = tasks + [sub for task in tasks for sub in task["subtasks"]]
    assigned = (project["project_manager_id"] == user["id"]
                or any(member["id"] == user["id"] for member in members)
                or any(person["id"] == user["id"] for task in every_task for person in task["assignees"]))
    note = None
    draft = ""
    conflict = False
    if assigned:
        note = db.execute("SELECT body, revision, updated_at FROM project_personal_notes WHERE project_id=? AND user_id=?",
                          (project_id, user["id"])).fetchone()
        draft = note["body"] if note else ""
        session.setdefault("project_note_token", uuid.uuid4().hex)
    if request.method == "POST":
        if not assigned:
            abort(403)
        body = request.form["notes"].replace("\r\n", "\n")
        body = body if body.strip() else ""
        revision = note["revision"] if note else 0
        if body != draft and request.form.get("revision", type=int) != revision:
            db.rollback()
            draft, conflict = body, True
        else:
            if body != draft:
                db.execute("""INSERT INTO project_personal_notes(project_id,user_id,body,revision,updated_at)
                              VALUES (?,?,?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET
                              body=excluded.body, revision=excluded.revision, updated_at=excluded.updated_at""",
                           (project_id, user["id"], body, revision + 1, now_str()))
            db.commit()
            return redirect(url_for("project_detail", project_id=project_id, _anchor="my-project-notes"), code=303)
    activities = db.execute("""SELECT a.*,u.name AS user_name FROM project_activities a LEFT JOIN users u ON u.id=a.user_id
        WHERE a.project_id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 30""", (project_id,)).fetchall()
    all_users = db.execute("SELECT id,name,role FROM users ORDER BY name").fetchall()
    spent = db.execute(
        """SELECT u.name, SUM(t.seconds) AS seconds FROM work_task_time t JOIN work_tasks w ON w.id = t.task_id
           LEFT JOIN users u ON u.id = t.user_id WHERE w.project_id=? AND t.ended_at IS NOT NULL
           GROUP BY t.user_id ORDER BY seconds DESC""", (project_id,)).fetchall()
    tracked = sum(row["seconds"] for row in spent)
    estimated = sum((task["estimate_minutes"] or 0) for task in every_task) * 60
    invoices = []
    if user["role"] == "manager" and project["company_id"]:
        invoices = db.execute("""SELECT id, invoice_no, invoice_date, total_amount, received_amount FROM invoices
                                 WHERE company_id=? ORDER BY id DESC LIMIT 20""", (project["company_id"],)).fetchall()
    billing = {
        "tracked": _duration_label(tracked) if tracked else "", "estimated": _duration_label(estimated) if estimated else "",
        "over": bool(estimated) and tracked > estimated,
        "people": [{"name": row["name"] or "Former user", "label": _duration_label(row["seconds"])} for row in spent],
        "invoices": invoices, "invoiced": sum(row["total_amount"] or 0 for row in invoices),
        "received": sum(row["received_amount"] or 0 for row in invoices),
    }
    response = app.make_response(render_template("project_detail.html", project=project, members=members, tasks=tasks, activities=activities,
        billing=billing,
        assigned=assigned, note=note, note_draft=draft, note_conflict=conflict,
        users=all_users, can_manage=can_manage_project(user, project),
        companies=_project_client_options(db) if can_manage_project(user, project) else [], project_types=PROJECT_TYPES,
        statuses=WORK_STATUSES, priorities=WORK_PRIORITIES, token=_task_token(), here=request.path,
        task_projects=[{"id": project["id"], "name": project["name"]}], project_filter=project["id"],
        task_total=len(every_task), completed_count=sum(1 for task in every_task if task["status"] == "Complete")))
    response.cache_control.no_store = True
    response.status_code = 409 if conflict else 200
    return response


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id):
    db, user = get_db(), current_user()
    project = _project_detail_row(db, project_id, user)
    if not project or not can_manage_project(user, project):
        flash("Only the project owner or a manager can edit this project.", "error")
        return redirect(url_for("projects_list"))
    team_users = db.execute("SELECT id,name,role FROM users ORDER BY name").fetchall()
    members = {row["user_id"] for row in db.execute("SELECT user_id FROM project_members WHERE project_id=?", (project_id,))}
    here = redirect(url_for("project_edit", project_id=project_id))
    if request.method == "POST":
        _check_task_token()
        form = request.form
        name = form.get("name", "").strip()
        manager_id = form.get("project_manager_id", type=int)
        status, priority, project_type = form.get("status"), form.get("priority"), form.get("project_type", "")
        company_id = form.get("company_id", type=int) or None
        valid_ids = {row["id"] for row in team_users}
        if (not name or manager_id not in valid_ids or status not in PROJECT_STATUSES or priority not in PRIORITIES
                or project_type not in PROJECT_TYPES):
            flash("Enter a project name, owner, type, status and priority.", "error")
            return here
        if company_id and not db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
            flash("Choose a valid client company.", "error")
            return here
        try:
            budget = float(form["budget"]) if form.get("budget", "").strip() else None
            start = date.fromisoformat(form["start_date"]).isoformat() if form.get("start_date") else None
            target = date.fromisoformat(form["target_date"]).isoformat() if form.get("target_date") else None
        except ValueError:
            flash("Enter a valid budget and dates.", "error")
            return here
        if start and target and target < start:
            flash("Choose a target date on or after the start date.", "error")
            return here
        description = form.get("description", "").strip()
        wanted = ({uid for uid in form.getlist("member_ids", type=int)} | {manager_id}) & valid_ids
        new = {"name": name, "project_type": project_type, "company_id": company_id, "project_manager_id": manager_id,
               "status": status, "priority": priority, "budget": budget, "start_date": start, "target_date": target,
               "description": description}
        labels = {"name": "name", "project_type": "type", "company_id": "client", "project_manager_id": "owner",
                  "status": "status", "priority": "priority", "budget": "budget", "start_date": "start date",
                  "target_date": "target date", "description": "scope notes"}
        changed = [labels[key] for key, value in new.items() if value != (project[key] if key != "description" else project[key] or "")]
        if wanted != members:
            changed.append("team")
        if changed:
            now = now_str()
            db.execute("""UPDATE projects SET name=?,project_type=?,company_id=?,project_manager_id=?,status=?,priority=?,
                          budget=?,start_date=?,target_date=?,description=?,updated_at=? WHERE id=?""",
                       (*new.values(), now, project_id))
            for uid in wanted - members:
                db.execute("INSERT INTO project_members (project_id,user_id,added_at) VALUES (?,?,?)", (project_id, uid, now))
            for uid in members - wanted:
                db.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (project_id, uid))
            log_project_event(db, project_id, "Project edited", "Changed: " + ", ".join(changed))
            db.commit()
            if "name" in changed:
                _file_task_files(db, [row["id"] for row in db.execute("SELECT id FROM work_tasks WHERE project_id=?", (project_id,))])
            log_activity("Edited project", "project", project_id, name, ", ".join(changed))
            flash("Project updated.", "success")
        return redirect(url_for("project_detail", project_id=project_id))
    return render_template("project_form.html", project=project, member_ids=members, team_users=team_users,
        won_leads=[], companies=_project_client_options(db), project_types=PROJECT_TYPES, statuses=PROJECT_STATUSES,
        priorities=PRIORITIES, selected_lead_id=None, selected_company_id=project["company_id"], token=_task_token())


@app.route("/projects/<int:project_id>/status", methods=["POST"])
@login_required
def project_update_status(project_id):
    _check_task_token()
    db = get_db(); user = current_user(); project = _project_detail_row(db, project_id, user)
    if not project or not can_manage_project(user, project):
        flash("Only the project owner or a manager can update this project.", "error")
        return redirect(url_for("projects_list"))
    status = request.form.get("status")
    if status not in PROJECT_STATUSES:
        flash("Choose a valid project status.", "error")
    elif status != project["status"]:
        db.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", (status, now_str(), project_id))
        log_project_event(db, project_id, "Project status updated", f"{project['status']} → {status}")
        db.commit(); log_activity("Updated project status", "project", project_id, project["name"], status)
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/attribution", methods=["POST"])
@login_required
def project_update_attribution(project_id):
    """Set who the project is for (a client company, or internal) and what kind of work it is."""
    _check_task_token()
    db = get_db(); user = current_user(); project = _project_detail_row(db, project_id, user)
    if not project or not can_manage_project(user, project):
        flash("Only the project owner or a manager can update this project.", "error")
        return redirect(url_for("projects_list"))
    company_id = request.form.get("company_id", type=int) or None
    project_type = request.form.get("project_type", "")
    company = db.execute("SELECT id,name FROM companies WHERE id=?", (company_id,)).fetchone() if company_id else None
    if (company_id and not company) or project_type not in PROJECT_TYPES:
        flash("Choose a valid client and project type.", "error")
        return redirect(url_for("project_detail", project_id=project_id))
    changes = []
    if company_id != project["company_id"]:
        changes.append(f"Client: {project['company_name'] or 'Internal'} → {company['name'] if company else 'Internal'}")
    if project_type != project["project_type"]:
        changes.append(f"Type: {project['project_type'] or 'Not set'} → {project_type}")
    if changes:
        db.execute("UPDATE projects SET company_id=?,project_type=?,updated_at=? WHERE id=?",
                   (company_id, project_type, now_str(), project_id))
        log_project_event(db, project_id, "Project details updated", "; ".join(changes))
        db.commit(); log_activity("Updated project details", "project", project_id, project["name"], "; ".join(changes))
        flash("Project details updated.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


# Project tasks now live in the Tasks module. These keep old bookmarks working.
@app.get("/my-tasks")
@login_required
def my_tasks():
    return redirect(url_for("tasks_list", assignee="me"))


@app.get("/planner")
@login_required
def team_planner():
    return redirect(url_for("tasks_board"))


# ---------- Private reminders ----------

def reminder_day(value):
    try:
        day = date.fromisoformat(value or "")
        if 1 < day.year < 9999:
            return day
    except ValueError:
        pass
    return date.fromisoformat(today_str())


REMINDER_PRESETS = [("", "Does not repeat"), ("daily", "Every day"), ("weekdays", "Every weekday (Mon to Fri)"),
                    ("weekly", "Every week"), ("monthly", "Every month"), ("yearly", "Every year"), ("custom", "Custom…")]
REMINDER_UNITS = [("day", "days"), ("week", "weeks"), ("month", "months"), ("year", "years")]
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
REMINDER_TIME = "%Y-%m-%dT%H:%M"


def _reminder_days(row):
    return [int(part) for part in (row["repeat_days"] or "").split(",") if part.strip().isdigit()]


def _next_reminder_time(when, unit, every, days):
    """The occurrence after `when` for a repeating reminder, or None."""
    every = max(1, every or 1)
    if unit == "day":
        return when + timedelta(days=every)
    if unit == "week":
        chosen = sorted({d for d in days if 0 <= d <= 6}) or [when.weekday()]
        first_week = when.date() - timedelta(days=when.weekday())
        for step in range(1, 7 * every + 8):
            candidate = when + timedelta(days=step)
            weeks = ((candidate.date() - timedelta(days=candidate.weekday())) - first_week).days // 7
            if candidate.weekday() in chosen and weeks % every == 0:
                return candidate
        return when + timedelta(weeks=every)
    if unit in ("month", "year"):
        # Aim for the original day each time, so the 31st does not drift to the 28th for good.
        target_day = days[0] if days else when.day
        months = when.year * 12 + when.month - 1 + (every if unit == "month" else 12 * every)
        year, month = months // 12, months % 12 + 1
        if year > 9998:
            return None
        return when.replace(year=year, month=month, day=min(target_day, calendar.monthrange(year, month)[1]))
    return None


def _reminder_occurrences(row, first, last):
    """Later occurrences of a repeating reminder that fall on first..last (dates), as datetimes."""
    if not row["repeat_unit"] or row["completed"]:
        return []
    until = row["repeat_until"] or "9999-12-31"
    days, found = _reminder_days(row), []
    when = datetime.strptime(row["remind_at"], REMINDER_TIME)
    for _ in range(800):
        when = _next_reminder_time(when, row["repeat_unit"], row["repeat_every"], days)
        if when is None or when.date() > last or when.date().isoformat() > until:
            break
        if when.date() >= first:
            found.append(when)
    return found


def _reminder_repeat_label(row):
    unit, every = row["repeat_unit"], row["repeat_every"] or 1
    if not unit:
        return ""
    days = _reminder_days(row)
    if unit == "week" and every == 1 and days == [0, 1, 2, 3, 4]:
        label = "Every weekday"
    else:
        label = f"Every {unit}" if every == 1 else f"Every {every} {unit}s"
        if unit == "week" and days:
            label += " on " + ", ".join(WEEKDAY_NAMES[d] for d in days if 0 <= d <= 6)
    if row["repeat_until"]:
        end = date.fromisoformat(row["repeat_until"])
        label += f" until {end.day} {end.strftime('%b %Y')}"
    return label


def _reminder_preset(row, when):
    """Which option of the Repeats menu matches a saved reminder."""
    if not row or not row["repeat_unit"]:
        return ""
    unit, every, days = row["repeat_unit"], row["repeat_every"] or 1, _reminder_days(row)
    if every == 1:
        if unit == "day":
            return "daily"
        if unit == "week" and days == [0, 1, 2, 3, 4]:
            return "weekdays"
        if unit == "week" and days in ([], [when.weekday()]):
            return "weekly"
        if unit == "month":
            return "monthly"
        if unit == "year":
            return "yearly"
    return "custom"


def _reminder_now():
    return datetime.now(APP_TIMEZONE).strftime(REMINDER_TIME)


@app.get("/reminders")
@login_required
def reminders():
    day = reminder_day(request.args.get("day"))
    first = day.replace(day=1)
    last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    db, uid, now = get_db(), current_user()["id"], _reminder_now()
    by_day = {}

    def show(row, when, upcoming):
        by_day.setdefault(when[:10], []).append({
            "id": row["id"], "title": row["title"], "notes": row["notes"], "remind_at": when, "time": when[11:],
            "completed": row["completed"], "repeat": _reminder_repeat_label(row), "upcoming": upcoming,
            "due": not upcoming and not row["completed"] and when <= now,
            "snoozed": bool(row["snoozed_until"] and row["snoozed_until"] > now and not upcoming),
        })

    for row in db.execute("""SELECT * FROM personal_reminders WHERE user_id=?
            AND remind_at>=? AND remind_at<? ORDER BY remind_at,id""",
            (uid, first.isoformat(), (last + timedelta(days=1)).isoformat())):
        show(row, row["remind_at"], False)
    # Repeating reminders are stored once, at their next occurrence; the later ones are worked out here.
    for row in db.execute("""SELECT * FROM personal_reminders WHERE user_id=? AND repeat_unit<>'' AND completed=0
            AND remind_at<?""", (uid, (last + timedelta(days=1)).isoformat())):
        for when in _reminder_occurrences(row, first, last):
            show(row, when.strftime(REMINDER_TIME), True)
    for items in by_day.values():
        items.sort(key=lambda item: (item["remind_at"], item["id"]))
    return render_template("reminders.html", day=day, weeks=calendar.Calendar().monthdayscalendar(day.year,day.month),
        by_day=by_day, previous=first-timedelta(days=1), following=last+timedelta(days=1))


@app.route("/reminders/new", methods=["GET", "POST"])
@app.route("/reminders/<int:reminder_id>/edit", methods=["GET", "POST"])
@login_required
def edit_reminder(reminder_id=None):
    db, uid = get_db(), current_user()["id"]
    row = None
    if reminder_id is not None:
        row = db.execute("SELECT * FROM personal_reminders WHERE id=? AND user_id=?", (reminder_id, uid)).fetchone()
        if not row:
            abort(404)
    day = reminder_day(request.args.get("day"))
    when = row["remind_at"] if row else day.isoformat() + "T09:00"
    started = datetime.strptime(when, REMINDER_TIME)
    values = {
        "title": row["title"] if row else "", "notes": (row["notes"] or "") if row else "",
        "repeat": _reminder_preset(row, started),
        "unit": (row["repeat_unit"] if row and row["repeat_unit"] else "week"),
        "every": (row["repeat_every"] if row else 1) or 1,
        "days": _reminder_days(row) if row and row["repeat_unit"] == "week" else [started.weekday()],
        "until": (row["repeat_until"] or "") if row else "",
    }
    if request.method == "POST":
        form = request.form
        when = form.get("remind_at", "")
        values.update(title=form.get("title", "").strip()[:300], notes=form.get("notes", "").strip()[:2000],
                      repeat=form.get("repeat", ""), unit=form.get("repeat_unit", "week"),
                      every=form.get("repeat_every", type=int) or 1,
                      days=sorted({d for d in form.getlist("repeat_days", type=int) if 0 <= d <= 6}),
                      until=form.get("repeat_until", "").strip())
        error = None
        try:
            parsed = datetime.strptime(when, REMINDER_TIME)
            if not (1 < parsed.year < 9999) or not values["title"]:
                raise ValueError
        except ValueError:
            parsed, error = None, "Enter a reminder and a valid date and time."
        unit, every, days, until = "", 1, [], None
        if parsed and values["repeat"]:
            preset = values["repeat"]
            if preset == "custom":
                unit, every = values["unit"], values["every"]
                if unit not in dict(REMINDER_UNITS) or not 1 <= every <= 365:
                    error = "Choose how often the reminder repeats: every 1 to 365 days, weeks, months or years."
                days = values["days"] if unit == "week" else []
            elif preset in ("daily", "weekdays", "weekly", "monthly", "yearly"):
                unit = {"daily": "day", "weekdays": "week", "weekly": "week", "monthly": "month", "yearly": "year"}[preset]
                days = [0, 1, 2, 3, 4] if preset == "weekdays" else []
            else:
                error = "Choose how often the reminder repeats."
            if unit == "week" and not days:
                days = [parsed.weekday()]
            if unit in ("month", "year"):
                days = [parsed.day]
            if values["until"]:
                try:
                    until = date.fromisoformat(values["until"]).isoformat()
                    if until < parsed.date().isoformat():
                        error = "Choose an end date on or after the reminder's date."
                except ValueError:
                    error = "Enter a valid end date."
            # A weekly reminder starts on the first chosen weekday on or after the date entered.
            if not error and unit == "week" and parsed.weekday() not in days:
                while parsed.weekday() not in days:
                    parsed += timedelta(days=1)
        if not error:
            when = parsed.strftime(REMINDER_TIME)
            fields = (values["title"], when, values["notes"], unit, every, ",".join(str(d) for d in days), until)
            if row:
                db.execute("""UPDATE personal_reminders SET title=?,remind_at=?,notes=?,repeat_unit=?,repeat_every=?,
                    repeat_days=?,repeat_until=?,snoozed_until=NULL WHERE id=? AND user_id=?""", fields + (reminder_id, uid))
            else:
                db.execute("""INSERT INTO personal_reminders(title,remind_at,notes,repeat_unit,repeat_every,repeat_days,
                    repeat_until,user_id) VALUES(?,?,?,?,?,?,?,?)""", fields + (uid,))
            db.commit()
            return redirect(url_for("reminders", day=when[:10]))
    else:
        error = None
    # The error is shown inside the form, so it also appears when the form is open in the pop-up.
    return render_template("edit_reminder.html", values=values, when=when, editing=row is not None, day=day, error=error,
        presets=REMINDER_PRESETS, units=REMINDER_UNITS, weekdays=list(enumerate(WEEKDAY_NAMES))), (422 if request.method == "POST" else 200)


@app.post("/reminders/<int:reminder_id>/update")
@login_required
def update_reminder(reminder_id):
    db, uid = get_db(), current_user()["id"]
    row = db.execute("SELECT * FROM personal_reminders WHERE id=? AND user_id=?", (reminder_id, uid)).fetchone()
    if not row:
        abort(404)
    action = request.form.get("action")
    now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    if action == "delete":
        db.execute("DELETE FROM personal_reminders WHERE id=? AND user_id=?", (reminder_id, uid))
    elif action == "complete" and row["repeat_unit"] and not row["completed"]:
        # Keep this occurrence as a finished entry, then move the reminder on to its next time.
        # Occurrences that were missed while it sat undone are skipped.
        db.execute("INSERT INTO personal_reminders(user_id,title,notes,remind_at,completed) VALUES(?,?,?,?,1)",
                   (uid, row["title"], row["notes"], row["remind_at"]))
        when, days = datetime.strptime(row["remind_at"], REMINDER_TIME), _reminder_days(row)
        for _ in range(800):
            when = _next_reminder_time(when, row["repeat_unit"], row["repeat_every"], days)
            if when is None or when > now:
                break
        if when is None or (row["repeat_until"] and when.date().isoformat() > row["repeat_until"]):
            db.execute("DELETE FROM personal_reminders WHERE id=?", (reminder_id,))   # the series has ended
        else:
            db.execute("UPDATE personal_reminders SET remind_at=?,snoozed_until=NULL WHERE id=?",
                       (when.strftime(REMINDER_TIME), reminder_id))
    elif action in ("complete", "reopen"):
        db.execute("UPDATE personal_reminders SET completed=?,snoozed_until=NULL WHERE id=? AND user_id=?",
                   (action == "complete", reminder_id, uid))
    elif action in ("snooze-hour", "snooze-tomorrow"):
        later = now + timedelta(hours=1) if action == "snooze-hour" else (now + timedelta(days=1)).replace(hour=9, minute=0)
        db.execute("UPDATE personal_reminders SET snoozed_until=? WHERE id=?", (later.strftime(REMINDER_TIME), reminder_id))
    else:
        abort(400)
    db.commit()
    return redirect(url_for("reminders", day=row["remind_at"][:10]))


@app.get("/reminders/due")
@login_required
def due_reminders():
    cutoff = _reminder_now()
    row = get_db().execute("""SELECT count(*) AS total,min(remind_at) AS earliest FROM personal_reminders
        WHERE user_id=? AND completed=0 AND remind_at<=? AND (snoozed_until IS NULL OR snoozed_until<=?)""",
        (current_user()["id"], cutoff, cutoff)).fetchone()
    response = jsonify(count=row["total"], url=url_for("reminders", day=row["earliest"][:10]) if row["total"] else None)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- Employee chat ----------

CHAT_PAGE_SIZE = 50
CHAT_ROLE_NAMES = {"manager": "Customer Relations Manager", "telecaller": "Telecaller",
                   "field_sales": "Field Sales Executive"}


def chat_employee(peer_id):
    if peer_id == current_user()["id"]:
        abort(404)
    employee = get_db().execute("SELECT id,name,role,email FROM users WHERE id=?", (peer_id,)).fetchone()
    if not employee:
        abort(404)
    return employee


def chat_contacts(uid):
    return get_db().execute("""SELECT u.id,u.name,u.role,substr(m.body,1,100) AS preview,m.sent_at,
        m.sender_id AS last_sender,
        (SELECT count(*) FROM chat_messages WHERE recipient_id=? AND sender_id=u.id AND is_read=0) AS unread
        FROM users u LEFT JOIN chat_messages m ON m.id=max(
            coalesce((SELECT max(id) FROM chat_messages WHERE sender_id=? AND recipient_id=u.id),0),
            coalesce((SELECT max(id) FROM chat_messages WHERE sender_id=u.id AND recipient_id=?),0))
        WHERE u.id<>? ORDER BY m.id DESC,u.name COLLATE NOCASE,u.id""", (uid, uid, uid, uid)).fetchall()


def chat_history(uid, peer_id, *, after=None, before=None):
    condition, params = "", [uid, peer_id, peer_id, uid]
    if after is not None:
        condition = " AND id>?"
        params.append(after)
    elif before is not None:
        condition = " AND id<?"
        params.append(before)
    order = "ASC" if after is not None else "DESC"
    rows = get_db().execute(f"""SELECT id,sender_id,body,sent_at FROM chat_messages
        WHERE ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?)){condition}
        ORDER BY id {order} LIMIT ?""", (*params, CHAT_PAGE_SIZE + 1)).fetchall()
    batch = rows[:CHAT_PAGE_SIZE]
    return (batch if after is not None else batch[::-1]), len(rows) > CHAT_PAGE_SIZE


@app.get("/chat")
@app.get("/chat/<int:peer_id>")
@login_required
def chat(peer_id=None):
    uid = current_user()["id"]
    peer = chat_employee(peer_id) if peer_id is not None else None
    contacts = chat_contacts(uid)
    view = request.args.get("view")
    directory = view == "people" or (view != "recent" and not any(p["sent_at"] for p in contacts))
    messages, older = chat_history(uid, peer_id) if peer else ([], False)
    token = session.setdefault("chat_csrf", uuid.uuid4().hex)
    response = app.make_response(render_template("chat.html", peer=peer, contacts=contacts, directory=directory,
        messages=messages, older=older, roles=CHAT_ROLE_NAMES, chat_token=token, client_id=uuid.uuid4().hex))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/chat/updates")
@login_required
def chat_updates():
    uid = current_user()["id"]
    peer_id = request.args.get("peer", type=int)
    peer = chat_employee(peer_id) if peer_id is not None else None
    before = request.args.get("before", type=int)
    after = request.args.get("after", default=0, type=int)
    rows, more = (chat_history(uid, peer_id, before=before, after=None if before is not None else after)
                  if peer else ([], False))
    payload = dict(messages=render_template("_chat_messages.html", messages=rows, peer=peer),
                   first=rows[0]["id"] if rows else None, last=rows[-1]["id"] if rows else None, more=more)
    if before is None:
        payload["contacts"] = render_template("_chat_contacts.html", contacts=chat_contacts(uid), peer=peer,
            directory=request.args.get("view") == "people", roles=CHAT_ROLE_NAMES)
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def check_chat_token():
    expected = session.get("chat_csrf")
    if not expected or not hmac.compare_digest(expected.encode(), request.form.get("token", "").encode()):
        abort(403)


@app.post("/chat/<int:peer_id>/send")
@login_required
def send_chat_message(peer_id):
    check_chat_token()
    chat_employee(peer_id)
    uid = current_user()["id"]
    body = request.form.get("body", "").strip()
    client_id = request.form.get("client_id", "")
    if not body or len(body) > 4000:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(error="Enter a message of up to 4,000 characters."), 400
        flash("Enter a message of up to 4,000 characters.", "error")
        return redirect(url_for("chat", peer_id=peer_id), code=303)
    try:
        client_id = uuid.UUID(client_id).hex
    except ValueError:
        abort(400)
    db = get_db()
    db.execute("""INSERT INTO chat_messages(sender_id,recipient_id,body,sent_at,client_id) VALUES(?,?,?,?,?)
        ON CONFLICT(sender_id,client_id) DO NOTHING""", (uid, peer_id, body, now_str(), client_id))
    saved = db.execute("SELECT recipient_id,body FROM chat_messages WHERE sender_id=? AND client_id=?", (uid,client_id)).fetchone()
    if saved["recipient_id"] != peer_id or saved["body"] != body:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(next_client_id=uuid.uuid4().hex), 409
        abort(409)
    db.commit()
    _live_touch()
    if request.accept_mimetypes.best == "application/json":
        return jsonify(next_client_id=uuid.uuid4().hex)
    return redirect(url_for("chat", peer_id=peer_id), code=303)


@app.post("/chat/<int:peer_id>/read")
@login_required
def read_chat_messages(peer_id):
    check_chat_token()
    through = request.form.get("through", type=int)
    if through is None:
        abort(400)
    db = get_db()
    db.execute("""UPDATE chat_messages SET is_read=1
        WHERE recipient_id=? AND sender_id=? AND id<=? AND is_read=0""", (current_user()["id"],peer_id,through))
    db.commit()
    _live_touch()
    return "", 204


# ---------- Companies (the center of everything) ----------

@app.route("/contacts")
@login_required
def contacts_list():
    db = get_db()
    user = current_user()
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    lead_clause, lead_params = visible_leads_clause(user)

    where, where_params = "", []
    if search:
        where = " WHERE (companies.name LIKE ? OR companies.industry LIKE ? OR companies.city LIKE ?)"
        where_params = [f"%{search}%"] * 3
    status_counts = {status: 0 for status in COMPANY_STATUSES}
    for row in db.execute(f"SELECT COALESCE(status, 'Prospect') s, COUNT(*) c FROM companies{where} GROUP BY s", where_params):
        if row["s"] in status_counts:
            status_counts[row["s"]] = row["c"]

    query = f"""
        SELECT companies.*, COUNT(DISTINCT contacts.id) AS contact_count, users.name AS owner_name,
               (SELECT COUNT(*) FROM leads WHERE leads.company_id = companies.id AND {lead_clause}) AS lead_count
        FROM companies
        LEFT JOIN contacts ON contacts.company_id = companies.id
        LEFT JOIN users ON users.id = companies.account_owner_id
        {where}
    """
    params = [*lead_params, *where_params]
    if status_filter in COMPANY_STATUSES:
        query += (" AND" if where else " WHERE") + " COALESCE(companies.status, 'Prospect') = ?"
        params.append(status_filter)
    query += " GROUP BY companies.id ORDER BY companies.name COLLATE NOCASE"
    companies = []
    for row in db.execute(query, params):
        company = dict(row)
        company["initials"] = _initials(row["name"])
        company["owner_initials"] = _initials(row["owner_name"]) if row["owner_name"] else ""
        companies.append(company)
    users = db.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    return render_template("contacts_list.html", companies=companies, search=search,
                            statuses=COMPANY_STATUSES, status_filter=status_filter,
                            status_counts=status_counts, total_all=sum(status_counts.values()), users=users)


@app.route("/contacts/companies/add", methods=["POST"])
@login_required
def add_company():
    db = get_db()
    user = current_user()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Company name is required.", "error")
        return redirect(url_for("contacts_list"))

    f = request.form
    db.execute(
        """INSERT INTO companies (
            name, industry, phone, alternate_phone, email, website, gbp_link, notes, billing_address, address,
            city, state, pincode, country, gstin, pan, cin, status, account_owner_id, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name, f.get("industry", "").strip(), f.get("phone", "").strip(), f.get("alternate_phone", "").strip(),
            f.get("email", "").strip(), f.get("website", "").strip(), f.get("gbp_link", "").strip(), f.get("notes", "").strip(),
            f.get("billing_address", "").strip(), f.get("address", "").strip(), f.get("city", "").strip(),
            _invoice_state(f.get("state")), f.get("pincode", "").strip(), f.get("country", "India").strip() or "India",
            f.get("gstin", "").strip().upper(), f.get("pan", "").strip().upper(), f.get("cin", "").strip().upper(),
            f.get("status", "Prospect"), f.get("account_owner_id") or user["id"], now_str(), now_str(),
        ),
    )
    db.commit()
    company_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    log_activity("Company Created", "company", company_id, name)
    flash(f"Company '{name}' added.", "success")
    return redirect(url_for("contacts_list"))


@app.route("/contacts/companies/<int:company_id>")
@login_required
def company_detail(company_id):
    db = get_db()
    company = db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if company is None:
        flash("Company not found.", "error")
        return redirect(url_for("contacts_list"))
    contacts = db.execute(
        "SELECT * FROM contacts WHERE company_id = ? ORDER BY is_primary DESC, name", (company_id,)
    ).fetchall()
    leads = db.execute(
        """SELECT leads.*, users.name AS owner_name FROM leads
           LEFT JOIN users ON users.id = leads.owner_id
           WHERE company_id = ? ORDER BY leads.created_at DESC""", (company_id,)
    ).fetchall()
    invoices = db.execute(
        "SELECT * FROM invoices WHERE company_id = ? ORDER BY id DESC", (company_id,)
    ).fetchall()
    activity = db.execute(
        "SELECT * FROM activity_logs WHERE entity_type = 'company' AND entity_id = ? ORDER BY created_at DESC LIMIT 50",
        (company_id,),
    ).fetchall()
    clause, params = visible_projects_clause(current_user(), "p")
    projects = db.execute(
        f"""SELECT p.id, p.name, p.project_type, p.status, p.target_date FROM projects p
            WHERE p.company_id=? AND {clause} ORDER BY p.id DESC""", [company_id, *params]).fetchall()
    return render_template(
        "company_detail.html", company=company, contacts=contacts, leads=leads, invoices=invoices,
        activity=activity, statuses=COMPANY_STATUSES, projects=projects,
    )


@app.route("/contacts/companies/<int:company_id>/edit", methods=["POST"])
@login_required
def edit_company(company_id):
    db = get_db()
    company = db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if company is None:
        flash("Company not found.", "error")
        return redirect(url_for("contacts_list"))
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        flash("Company name is required.", "error")
        return redirect(url_for("company_detail", company_id=company_id))
    db.execute(
        """UPDATE companies SET name=?, industry=?, phone=?, alternate_phone=?, email=?, website=?, gbp_link=?, notes=?,
            billing_address=?, address=?, city=?, state=?, pincode=?, country=?, gstin=?, pan=?, cin=?, status=?, updated_at=?
        WHERE id=?""",
        (
            name, f.get("industry", "").strip(), f.get("phone", "").strip(), f.get("alternate_phone", "").strip(),
            f.get("email", "").strip(), f.get("website", "").strip(), f.get("gbp_link", "").strip(), f.get("notes", "").strip(),
            f.get("billing_address", "").strip(), f.get("address", "").strip(), f.get("city", "").strip(),
            _invoice_state(f.get("state"), company["state"]), f.get("pincode", "").strip(), f.get("country", "India").strip() or "India",
            f.get("gstin", "").strip().upper(), f.get("pan", "").strip().upper(), f.get("cin", "").strip().upper(),
            f.get("status", "Prospect"), now_str(), company_id,
        ),
    )
    db.commit()
    log_activity("Company Updated", "company", company_id, name)
    flash("Company updated.", "success")
    return redirect(url_for("company_detail", company_id=company_id))


@app.route("/contacts/companies/<int:company_id>/delete", methods=["POST"])
@login_required
def delete_company(company_id):
    db = get_db()
    company = db.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()
    db.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
    db.execute("UPDATE leads SET company_id = NULL WHERE company_id = ?", (company_id,))
    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    db.commit()
    log_activity("Company Deleted", "company", company_id, company["name"] if company else "")
    flash("Company and its contacts deleted.", "success")
    return redirect(url_for("contacts_list"))


@app.route("/contacts/companies/<int:company_id>/contacts/add", methods=["POST"])
@login_required
def add_contact(company_id):
    db = get_db()
    name = request.form.get("name", "").strip()
    next_lead_id = request.form.get("next_lead_id", "").strip()
    redirect_target = url_for("lead_detail", lead_id=next_lead_id) if next_lead_id else url_for("company_detail", company_id=company_id)

    if not name:
        flash("Contact name is required.", "error")
        return redirect(redirect_target)

    f = request.form
    is_primary = 1 if f.get("is_primary") else 0
    if is_primary:
        db.execute("UPDATE contacts SET is_primary = 0 WHERE company_id = ?", (company_id,))

    db.execute(
        """INSERT INTO contacts (company_id, name, designation, phone, alternate_phone, email, linkedin, is_primary, notes, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            company_id, name, f.get("designation", "").strip(), f.get("phone", "").strip(),
            f.get("alternate_phone", "").strip(), f.get("email", "").strip(), f.get("linkedin", "").strip(),
            is_primary, f.get("notes", "").strip(), now_str(),
        ),
    )
    db.commit()
    contact_id = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    log_activity("Contact Created", "contact", contact_id, name)
    flash(f"Contact '{name}' added.", "success")
    return redirect(redirect_target)


@app.route("/contacts/<int:contact_id>/delete", methods=["POST"])
@login_required
def delete_contact(contact_id):
    db = get_db()
    row = db.execute("SELECT company_id, name FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    next_lead_id = request.form.get("next_lead_id", "").strip()
    db.execute("UPDATE leads SET contact_id = NULL WHERE contact_id = ?", (contact_id,))
    db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    db.commit()
    if row:
        log_activity("Contact Deleted", "contact", contact_id, row["name"])
    flash("Contact deleted.", "success")
    if next_lead_id:
        return redirect(url_for("lead_detail", lead_id=next_lead_id))
    return redirect(url_for("company_detail", company_id=row["company_id"] if row else 0))


# ---------- Global search ----------

@app.route("/search")
@login_required
def global_search():
    db = get_db()
    q = request.args.get("q", "").strip()
    results = {"companies": [], "contacts": [], "leads": [], "invoices": []}
    if q and len(q) >= 2:
        like = f"%{q}%"
        results["companies"] = db.execute(
            "SELECT * FROM companies WHERE name LIKE ? OR gstin LIKE ? OR email LIKE ? OR phone LIKE ? ORDER BY name LIMIT 25",
            (like, like, like, like),
        ).fetchall()
        results["contacts"] = db.execute(
            """SELECT contacts.*, companies.name AS company_name FROM contacts
               LEFT JOIN companies ON companies.id = contacts.company_id
               WHERE contacts.name LIKE ? OR contacts.phone LIKE ? OR contacts.email LIKE ? ORDER BY contacts.name LIMIT 25""",
            (like, like, like),
        ).fetchall()
        results["leads"] = db.execute(
            """SELECT leads.*, companies.name AS company_name FROM leads
               LEFT JOIN companies ON companies.id = leads.company_id
               WHERE leads.name LIKE ? OR leads.phone LIKE ? OR leads.email LIKE ? OR companies.name LIKE ?
               ORDER BY leads.created_at DESC LIMIT 25""",
            (like, like, like, like),
        ).fetchall()
        user = current_user()
        if user["role"] == "manager":
            results["invoices"] = db.execute(
                "SELECT * FROM invoices WHERE invoice_no LIKE ? OR bill_name LIKE ? ORDER BY id DESC LIMIT 25",
                (like, like),
            ).fetchall()
    return render_template("search_results.html", q=q, results=results)


# ---------- Activity Logs page (manager only) ----------

@app.route("/activity-logs")
@manager_required
def activity_logs_page():
    db = get_db()
    user_filter = request.args.get("user", "").strip()
    action_filter = request.args.get("action", "").strip()
    date_filter = request.args.get("date", "").strip()

    query = "SELECT * FROM activity_logs WHERE 1=1"
    params = []
    if user_filter:
        query += " AND user_id = ?"
        params.append(user_filter)
    if action_filter:
        query += " AND action = ?"
        params.append(action_filter)
    if date_filter:
        query += " AND date(created_at) = ?"
        params.append(date_filter)
    query += " ORDER BY created_at DESC LIMIT 300"

    logs = db.execute(query, params).fetchall()
    users = db.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    actions = [r["action"] for r in db.execute("SELECT DISTINCT action FROM activity_logs ORDER BY action")]

    return render_template(
        "activity_logs.html", logs=logs, users=users, actions=actions,
        user_filter=user_filter, action_filter=action_filter, date_filter=date_filter,
    )


# ---------- Field Sales ----------

def _active_field_session(db, user_id):
    _reconcile_field_sessions(db)
    return db.execute(
        "SELECT * FROM field_work_sessions WHERE user_id=? AND work_date=? AND status='active'",
        (user_id, today_str()),
    ).fetchone()


def _session_duration(started_at, ended_at=None):
    if not started_at:
        return "—"
    start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(ended_at, "%Y-%m-%d %H:%M:%S") if ended_at else datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    total_minutes = max(0, int((end - start).total_seconds() // 60))
    return f"{total_minutes // 60}h {total_minutes % 60}m"


def _get_field_visit(db, visit_id):
    return db.execute(
        """SELECT v.*, leads.name AS lead_name, leads.phone AS lead_phone,
                  leads.email AS lead_email, leads.designation AS lead_designation, leads.contact_id AS lead_contact_id,
                  companies.name AS company_name, users.name AS executive_name,
                  creators.name AS creator_name
           FROM field_visits v
           LEFT JOIN leads ON leads.id=v.lead_id
           LEFT JOIN companies ON companies.id=v.company_id
           LEFT JOIN users ON users.id=v.assigned_to
           LEFT JOIN users creators ON creators.id=v.created_by
           WHERE v.id=?""",
        (visit_id,),
    ).fetchone()


def _can_access_field_visit(user, visit):
    return bool(visit and (user["role"] == "manager" or visit["assigned_to"] == user["id"]))


def _log_field_visit_event(visit, event, detail=""):
    if visit["lead_id"]:
        log_lead_event(visit["lead_id"], event, detail)
    if visit["company_id"]:
        log_activity(event, "company", visit["company_id"], visit["company_name"] or "", detail)
    log_activity(event, "field_visit", visit["id"], visit["company_name"] or visit["lead_name"] or f"Visit {visit['id']}", detail)


def _photo_exists(db, visit_id, photo_type):
    return db.execute(
        "SELECT 1 FROM field_visit_photos WHERE visit_id=? AND photo_type=? LIMIT 1",
        (visit_id, photo_type),
    ).fetchone() is not None


def _field_photo_dir():
    return os.environ.get("FIELD_PHOTO_DIR", "").strip() or os.path.join(app.instance_path, "field_visit_photos")


PHOTO_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _resolve_photo_file(stored_name):
    """Path of a stored visit photo, or None.

    Photos may have been converted to another format on disk after they were recorded
    (for example .jpg to .webp), so fall back to a file with the same name and a
    different image extension.
    """
    folder = _field_photo_dir()
    path = os.path.join(folder, stored_name)
    if os.path.isfile(path):
        return path
    stem = os.path.splitext(stored_name)[0]
    for extension in PHOTO_MIME_TYPES:
        candidate = os.path.join(folder, stem + extension)
        if os.path.isfile(candidate):
            return candidate
    return None


def _to_webp(raw):
    """Re-encode a captured JPEG as WebP to save space. Returns None if that is not possible."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            output = io.BytesIO()
            image.convert("RGB").save(output, "WEBP", quality=80, method=4)
            return output.getvalue()
    except Exception:
        return None


def _save_camera_photo(db, visit_id, photo_type, data_url):
    if not data_url:
        return None
    prefix = "data:image/jpeg;base64,"
    if not data_url.startswith(prefix):
        raise ValueError("Camera evidence must be a JPEG captured by the visit form.")
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("The captured camera image could not be read.")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("The captured image is larger than 8 MB.")
    if len(raw) < 4 or not raw.startswith(b"\xff\xd8\xff"):
        raise ValueError("The captured evidence is not a valid JPEG image.")

    # Store as WebP when possible (smaller for the same quality); otherwise keep the JPEG.
    extension, mime_type = ".jpg", "image/jpeg"
    webp = _to_webp(raw)
    if webp:
        raw, extension, mime_type = webp, ".webp", "image/webp"

    upload_dir = _field_photo_dir()
    os.makedirs(upload_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{extension}"
    with open(os.path.join(upload_dir, stored_name), "wb") as output:
        output.write(raw)
    db.execute(
        """INSERT INTO field_visit_photos
           (visit_id,photo_type,stored_name,mime_type,byte_size,sha256,captured_at,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (visit_id, photo_type, stored_name, mime_type, len(raw), hashlib.sha256(raw).hexdigest(), now_str(), now_str()),
    )
    return stored_name


@app.route("/field-visits")
@field_sales_or_manager_required
def field_visits():
    db = get_db()
    user = current_user()
    session_filter = request.args.get("session", "").strip()
    query = """SELECT v.*, leads.name AS lead_name, companies.name AS company_name,
                      users.name AS executive_name
               FROM field_visits v
               LEFT JOIN leads ON leads.id=v.lead_id
               LEFT JOIN companies ON companies.id=v.company_id
               LEFT JOIN users ON users.id=v.assigned_to"""
    params = []
    if user["role"] == "field_sales":
        query += " WHERE v.assigned_to=?"
        params.append(user["id"])
    elif session_filter:
        query += " WHERE v.session_id=?"
        params.append(session_filter)
    query += " ORDER BY v.scheduled_date DESC, v.scheduled_time DESC, v.id DESC"
    every_visit = db.execute(query, params).fetchall()

    # Cards or list; the choice is remembered for the rest of the session.
    view = session.get("visits_view", "cards")
    requested_view = request.args.get("view")
    if requested_view in ("cards", "list") and requested_view != view:
        view = session["visits_view"] = requested_view

    status_counts = {status: 0 for status in VISIT_STATUS_LABELS}
    for row in every_visit:
        if row["status"] in status_counts:
            status_counts[row["status"]] += 1
    status_filter = request.args.get("status", "")
    if status_filter not in VISIT_STATUS_LABELS:
        status_filter = ""
    visits = []
    for row in every_visit:
        if status_filter and row["status"] != status_filter:
            continue
        visit = dict(row)
        visit["title"] = row["company_name"] or row["lead_name"] or "Field visit"
        visit["initials"] = _initials(visit["title"])
        visit["executive_initials"] = _initials(row["executive_name"]) if row["executive_name"] else ""
        visits.append(visit)

    session_today = None
    sessions = []
    if user["role"] == "field_sales":
        session_today = db.execute(
            "SELECT * FROM field_work_sessions WHERE user_id=? AND work_date=?", (user["id"], today_str())
        ).fetchone()
    else:
        sessions = db.execute(
            """SELECT s.*, users.name AS executive_name, COUNT(v.id) AS visit_count
               FROM field_work_sessions s
               LEFT JOIN users ON users.id=s.user_id
               LEFT JOIN field_visits v ON v.session_id=s.id
               GROUP BY s.id ORDER BY s.work_date DESC, s.started_at DESC LIMIT 100"""
        ).fetchall()
    return render_template(
        "field_visits.html", visits=visits, session_today=session_today, sessions=sessions,
        session_duration=_session_duration, today=today_str(), session_filter=session_filter,
        view=view, status_filter=status_filter, status_counts=status_counts,
        status_labels=VISIT_STATUS_LABELS, total_all=len(every_visit),
    )


@app.route("/field-work/start", methods=["POST"])
@field_sales_or_manager_required
def field_work_start():
    user = current_user()
    if user["role"] != "field_sales":
        flash("Only a Field Sales Executive can start field work.", "error")
        return redirect(url_for("field_visits"))
    db = get_db()
    existing = db.execute(
        "SELECT * FROM field_work_sessions WHERE user_id=? AND work_date=?", (user["id"], today_str())
    ).fetchone()
    if existing:
        flash("Today's field-work session has already been started.", "error")
        return redirect(url_for("field_visits"))
    if datetime.now(APP_TIMEZONE).time() >= FIELD_WORK_AUTO_END:
        flash("A field-work session cannot be started after the 6:30 p.m. automatic close time.", "error")
        return redirect(url_for("field_visits"))
    db.execute(
        "INSERT INTO field_work_sessions (user_id,work_date,started_at,status,created_at) VALUES (?,?,?,?,?)",
        (user["id"], today_str(), now_str(), "active", now_str()),
    )
    db.commit()
    log_activity("Field Work Started", "field_session", None, user["name"], today_str())
    flash("Field work started. Check in when you arrive at your first visit.", "success")
    return redirect(url_for("field_visits"))


@app.route("/field-work/end", methods=["POST"])
@field_sales_or_manager_required
def field_work_end():
    user = current_user()
    if user["role"] != "field_sales":
        flash("Only a Field Sales Executive can end field work.", "error")
        return redirect(url_for("field_visits"))
    db = get_db()
    work_session = _active_field_session(db, user["id"])
    if not work_session:
        flash("There is no active field-work session.", "error")
        return redirect(url_for("field_visits"))
    open_visit = db.execute(
        "SELECT id FROM field_visits WHERE session_id=? AND status='checked_in'", (work_session["id"],)
    ).fetchone()
    if open_visit:
        flash("Complete your checked-in visit before ending field work.", "error")
        return redirect(url_for("field_visit_detail", visit_id=open_visit["id"]))
    db.execute(
        "UPDATE field_work_sessions SET ended_at=?,end_source='executive',status='closed' WHERE id=?",
        (now_str(), work_session["id"]),
    )
    db.commit()
    log_activity("Field Work Ended", "field_session", work_session["id"], user["name"], today_str())
    flash("Field work ended for today.", "success")
    return redirect(url_for("field_visits"))


@app.route("/field-visits/new", methods=["GET", "POST"])
@field_sales_or_manager_required
def field_visit_new():
    db = get_db()
    user = current_user()
    if request.method == "POST":
        f = request.form
        if user["role"] == "manager":
            assigned_to = f.get("assigned_to", "").strip()
            executive = db.execute("SELECT * FROM users WHERE id=? AND role='field_sales'", (assigned_to,)).fetchone()
            if not executive:
                flash("Choose a valid Field Sales Executive.", "error")
                return redirect(url_for("field_visit_new"))
            origin = "manager_assigned"
        else:
            if not _active_field_session(db, user["id"]):
                flash("Start Field Work before creating an independent visit.", "error")
                return redirect(url_for("field_visits"))
            assigned_to = user["id"]
            origin = "executive_initiated"

        lead_id = f.get("lead_id", "").strip() or None
        company_id = f.get("company_id", "").strip() or None
        lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone() if lead_id else None
        if lead_id and not lead:
            flash("The selected lead was not found.", "error")
            return redirect(url_for("field_visit_new"))
        if lead and lead["company_id"]:
            company_id = lead["company_id"]
        company = db.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone() if company_id else None
        if not lead and not company:
            flash("Select an existing lead or company.", "error")
            return redirect(url_for("field_visit_new"))

        scheduled_date = f.get("scheduled_date", "").strip() or today_str()
        scheduled_time = f.get("scheduled_time", "").strip() or datetime.now(APP_TIMEZONE).strftime("%H:%M")
        address = f.get("location_address", "").strip()
        purpose = f.get("purpose", "").strip()
        if not address or not purpose:
            flash("Visit location and purpose are required.", "error")
            return redirect(url_for("field_visit_new", lead_id=lead_id or "", company_id=company_id or ""))

        cur = db.execute(
            """INSERT INTO field_visits
               (lead_id,company_id,assigned_to,created_by,origin,scheduled_date,scheduled_time,
                location_address,purpose,contact_person,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lead_id, company_id, assigned_to, user["id"], origin, scheduled_date, scheduled_time,
             address, purpose, f.get("contact_person", "").strip(), "scheduled", now_str(), now_str()),
        )
        db.commit()
        visit = _get_field_visit(db, cur.lastrowid)
        _log_field_visit_event(visit, "Field Visit Created", f"{origin.replace('_', ' ').title()} · {scheduled_date} {scheduled_time}")
        flash("Field visit created.", "success")
        return redirect(url_for("field_visit_detail", visit_id=visit["id"]))

    executives = db.execute("SELECT id,name FROM users WHERE role='field_sales' ORDER BY name").fetchall()
    leads = db.execute(
        """SELECT leads.id,leads.name,leads.phone,leads.company_id,companies.name AS company_name,
                  companies.address,companies.billing_address
           FROM leads LEFT JOIN companies ON companies.id=leads.company_id
           ORDER BY leads.created_at DESC"""
    ).fetchall()
    companies = db.execute(
        "SELECT id,name,address,billing_address,city,state FROM companies ORDER BY name"
    ).fetchall()
    return render_template(
        "field_visit_form.html", executives=executives, leads=leads, companies=companies,
        selected_lead=request.args.get("lead_id", ""), selected_company=request.args.get("company_id", ""),
        now_time=datetime.now(APP_TIMEZONE).strftime("%H:%M"),
    )


@app.route("/field-visits/<int:visit_id>")
@field_sales_or_manager_required
def field_visit_detail(visit_id):
    db = get_db()
    user = current_user()
    visit = _get_field_visit(db, visit_id)
    if not _can_access_field_visit(user, visit):
        flash("Visit not found or not available to your account.", "error")
        return redirect(url_for("field_visits"))
    photos = db.execute(
        "SELECT * FROM field_visit_photos WHERE visit_id=? ORDER BY id", (visit_id,)
    ).fetchall()
    work_session = db.execute(
        "SELECT * FROM field_work_sessions WHERE id=?", (visit["session_id"],)
    ).fetchone() if visit["session_id"] else None
    active_session = _active_field_session(db, user["id"]) if user["role"] == "field_sales" else None
    return render_template(
        "field_visit_detail.html", visit=visit, photos=photos, work_session=work_session,
        active_session=active_session, outcomes=VISIT_OUTCOMES, no_card_reasons=NO_CARD_REASONS,
        session_duration=_session_duration, status_labels=VISIT_STATUS_LABELS,
        title=visit["company_name"] or visit["lead_name"] or "Field visit",
        initials=_initials(visit["company_name"] or visit["lead_name"] or "Field visit"),
    )


@app.route("/field-visits/<int:visit_id>/check-in", methods=["POST"])
@field_sales_or_manager_required
def field_visit_checkin(visit_id):
    db = get_db()
    user = current_user()
    visit = _get_field_visit(db, visit_id)
    if not visit or user["role"] != "field_sales" or visit["assigned_to"] != user["id"]:
        flash("Only the assigned executive can check in.", "error")
        return redirect(url_for("field_visits"))
    if visit["status"] != "scheduled":
        flash("This visit cannot be checked in again.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))
    work_session = _active_field_session(db, user["id"])
    if not work_session:
        flash("Start Field Work before checking in.", "error")
        return redirect(url_for("field_visits"))

    location_status = request.form.get("location_status", "unavailable")
    allowed_statuses = {"captured", "denied", "unavailable", "unsupported", "error"}
    if location_status not in allowed_statuses:
        location_status = "error"
    def optional_float(name):
        try:
            return float(request.form.get(name))
        except (TypeError, ValueError):
            return None
    lat = optional_float("latitude")
    lng = optional_float("longitude")
    accuracy = optional_float("accuracy")
    coordinates_valid = (
        lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180
        and accuracy is not None and 0 <= accuracy <= 100000
    )
    if location_status == "captured" and not coordinates_valid:
        location_status = "error"
        lat = lng = accuracy = None
    elif location_status != "captured":
        lat = lng = accuracy = None

    db.execute(
        """UPDATE field_visits SET status='checked_in',session_id=?,checkin_at=?,checkin_lat=?,checkin_lng=?,
           checkin_accuracy=?,location_status=?,updated_at=? WHERE id=?""",
        (work_session["id"], now_str(), lat, lng, accuracy, location_status, now_str(), visit_id),
    )
    db.commit()
    visit = _get_field_visit(db, visit_id)
    _log_field_visit_event(visit, "Field Visit Check-in", f"Location: {location_status}")
    flash("Checked in. Complete the visit before ending field work.", "success")
    return redirect(url_for("field_visit_detail", visit_id=visit_id))


@app.route("/field-visits/<int:visit_id>/complete", methods=["POST"])
@field_sales_or_manager_required
def field_visit_complete(visit_id):
    db = get_db()
    user = current_user()
    visit = _get_field_visit(db, visit_id)
    if not visit or user["role"] != "field_sales" or visit["assigned_to"] != user["id"]:
        flash("Only the assigned executive can complete this visit.", "error")
        return redirect(url_for("field_visits"))
    if visit["status"] not in {"checked_in", "needs_correction"}:
        flash("Check in before completing this visit.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))
    if visit["status"] == "checked_in":
        active_session = _active_field_session(db, user["id"])
        if not active_session or active_session["id"] != visit["session_id"]:
            flash("The field-work session used for this check-in is no longer active.", "error")
            return redirect(url_for("field_visit_detail", visit_id=visit_id))

    f = request.form
    people_met = f.get("people_met", "").strip()
    discussion = f.get("discussion", "").strip()
    outcome = f.get("outcome", "").strip()
    contact_name = f.get("contact_name", "").strip()
    contact_designation = f.get("contact_designation", "").strip()
    contact_phone = f.get("contact_phone", "").strip()
    contact_email = f.get("contact_email", "").strip()
    if not people_met or not discussion or not outcome or not contact_name:
        flash("Contact name, people met, discussion, and visit outcome are required.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))

    office_data = f.get("office_photo_data", "")
    card_data = f.get("card_photo_data", "")
    has_office = bool(office_data) or _photo_exists(db, visit_id, "office")
    has_card = bool(card_data) or _photo_exists(db, visit_id, "visiting_card")
    no_card_reason = f.get("no_card_reason", "").strip()
    no_card_explanation = f.get("no_card_explanation", "").strip()
    if not has_office:
        flash("Capture an office photo with the camera before completing the visit.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))
    if not has_card and (no_card_reason not in NO_CARD_REASONS or not no_card_explanation):
        flash("Capture the visiting card, or choose a reason and enter an explanation.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))

    try:
        if office_data:
            _save_camera_photo(db, visit_id, "office", office_data)
        if card_data:
            _save_camera_photo(db, visit_id, "visiting_card", card_data)
            no_card_reason = ""
            no_card_explanation = ""
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))

    completed_at = visit["completed_at"] or now_str()
    follow_up_date = f.get("follow_up_date", "").strip()
    db.execute(
        """UPDATE field_visits SET status='review_pending',completed_at=?,contact_person=?,contact_designation=?,contact_phone=?,contact_email=?,
           people_met=?,discussion=?,outcome=?,notes=?,
           next_action=?,follow_up_date=?,no_card_reason=?,no_card_explanation=?,submitted_at=?,review_status='pending',
           reviewed_by=NULL,reviewed_at=NULL,updated_at=? WHERE id=?""",
        (completed_at, contact_name, contact_designation, contact_phone, contact_email,
         people_met, discussion, outcome, f.get("notes", "").strip(),
         f.get("next_action", "").strip(), follow_up_date, no_card_reason, no_card_explanation,
         now_str(), now_str(), visit_id),
    )
    if visit["lead_id"]:
        db.execute(
            """UPDATE leads SET name=?,designation=?,phone=?,email=?,
               follow_up_date=CASE WHEN ?!='' THEN ? ELSE follow_up_date END,updated_at=? WHERE id=?""",
            (contact_name, contact_designation, contact_phone, contact_email,
             follow_up_date, follow_up_date, now_str(), visit["lead_id"]),
        )
        if visit["lead_contact_id"]:
            db.execute(
                "UPDATE contacts SET name=?,designation=?,phone=?,email=?,updated_at=? WHERE id=?",
                (contact_name, contact_designation, contact_phone, contact_email, now_str(), visit["lead_contact_id"]),
            )
    elif visit["company_id"]:
        existing_contact = db.execute(
            "SELECT id FROM contacts WHERE company_id=? AND LOWER(name)=LOWER(?) LIMIT 1",
            (visit["company_id"], contact_name),
        ).fetchone()
        if existing_contact:
            db.execute(
                """UPDATE contacts SET designation=?,
                   phone=CASE WHEN ?!='' THEN ? ELSE phone END,
                   email=CASE WHEN ?!='' THEN ? ELSE email END,updated_at=? WHERE id=?""",
                (contact_designation, contact_phone, contact_phone, contact_email, contact_email, now_str(), existing_contact["id"]),
            )
        else:
            db.execute(
                """INSERT INTO contacts (company_id,name,designation,phone,email,is_primary,created_at,updated_at)
                   VALUES (?,?,?,?,?,0,?,?)""",
                (visit["company_id"], contact_name, contact_designation, contact_phone, contact_email, now_str(), now_str()),
            )
    db.commit()
    visit = _get_field_visit(db, visit_id)
    _log_field_visit_event(visit, "Field Visit Submitted", f"Outcome: {outcome}" + (f" · Next: {f.get('next_action', '').strip()}" if f.get("next_action", "").strip() else ""))
    flash("Visit submitted for manager review.", "success")
    return redirect(url_for("field_visit_detail", visit_id=visit_id))


@app.route("/field-visits/<int:visit_id>/review", methods=["POST"])
@manager_required
def field_visit_review(visit_id):
    db = get_db()
    visit = _get_field_visit(db, visit_id)
    if not visit or visit["review_status"] != "pending":
        flash("This visit is not waiting for review.", "error")
        return redirect(url_for("field_visits"))
    action = request.form.get("action")
    comment = request.form.get("review_comment", "").strip()
    if action == "return" and not comment:
        flash("Enter a correction comment before returning the visit.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))
    if action not in {"accept", "return"}:
        flash("Choose Accept or Return for correction.", "error")
        return redirect(url_for("field_visit_detail", visit_id=visit_id))
    status = "completed" if action == "accept" else "needs_correction"
    review_status = "accepted" if action == "accept" else "returned"
    db.execute(
        """UPDATE field_visits SET status=?,review_status=?,reviewed_by=?,reviewed_at=?,review_comment=?,updated_at=?
           WHERE id=?""",
        (status, review_status, current_user()["id"], now_str(), comment, now_str(), visit_id),
    )
    db.commit()
    visit = _get_field_visit(db, visit_id)
    event = "Field Visit Accepted" if action == "accept" else "Field Visit Returned"
    _log_field_visit_event(visit, event, comment)
    flash("Visit accepted." if action == "accept" else "Visit returned for correction.", "success")
    return redirect(url_for("field_visit_detail", visit_id=visit_id))


@app.route("/field-visit-photos/<int:photo_id>")
@field_sales_or_manager_required
def field_visit_photo(photo_id):
    db = get_db()
    row = db.execute("SELECT * FROM field_visit_photos WHERE id=?", (photo_id,)).fetchone()
    visit = _get_field_visit(db, row["visit_id"]) if row else None
    if not row or not _can_access_field_visit(current_user(), visit):
        return "Not found", 404
    path = _resolve_photo_file(row["stored_name"])
    if not path:
        return "Image not found", 404
    extension = os.path.splitext(path)[1].lower()
    return send_file(path, mimetype=PHOTO_MIME_TYPES.get(extension, row["mime_type"]), as_attachment=False,
                     download_name=f"{row['photo_type']}{extension}")


@app.cli.command("sync-photos")
def sync_photos_command():
    """Update photo records after the files were converted to another format on disk.

    Run this once after converting the photo folder (for example .jpg to .webp):
        flask --app app sync-photos
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    updated = unchanged = missing = 0
    for row in db.execute("SELECT id, stored_name FROM field_visit_photos").fetchall():
        path = _resolve_photo_file(row["stored_name"])
        if not path:
            missing += 1
            continue
        name = os.path.basename(path)
        if name == row["stored_name"]:
            unchanged += 1
            continue
        with open(path, "rb") as handle:
            raw = handle.read()
        extension = os.path.splitext(name)[1].lower()
        db.execute(
            "UPDATE field_visit_photos SET stored_name=?, mime_type=?, byte_size=?, sha256=? WHERE id=?",
            (name, PHOTO_MIME_TYPES.get(extension, "application/octet-stream"), len(raw), hashlib.sha256(raw).hexdigest(), row["id"]),
        )
        updated += 1
    db.commit()
    db.close()
    print(f"Photo records: {updated} updated, {unchanged} already correct, {missing} with no file on disk.")


# ---------- Invoices (manager only) ----------

def _invoice_settings(db):
    return db.execute("SELECT * FROM company_settings WHERE id=1").fetchone()


def _next_invoice_no(db, prefix):
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    fy = f"{str(fy_start)[-2:]}-{str(fy_start + 1)[-2:]}"
    stem = f"{prefix or 'QL'}/{fy}/"
    rows = db.execute("SELECT invoice_no FROM invoices WHERE invoice_no LIKE ?", (stem + '%',)).fetchall()
    nums = []
    for r in rows:
        try: nums.append(int(r['invoice_no'].rsplit('/',1)[1]))
        except Exception: pass
    return f"{stem}{(max(nums, default=0)+1):05d}"


@app.route('/invoices')
@manager_required
def invoices_list():
    db=get_db()
    invoices=db.execute("SELECT * FROM invoices ORDER BY id DESC").fetchall()
    return render_template('invoices_list.html', invoices=invoices)


# States and union territories offered for an invoice's shipping state. GST is
# worked out from this value, so it is a fixed list rather than free text.
INVOICE_STATES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
    "Outside India",
)

app.jinja_env.globals["state_options"] = INVOICE_STATES


def _invoice_state(value, keep=""):
    """The listed spelling of a state. `keep` lets an invoice hold on to a value typed before the list existed."""
    value = (value or "").strip()
    for state in INVOICE_STATES:
        if state.casefold() == value.casefold():
            return state
    return value if value and value == (keep or "").strip() else ""


def _read_invoice_form(f, keep_state=""):
    """Return (fields, items, error) for a submitted invoice form; fields maps invoices columns to values."""
    names=f.getlist('item_name[]'); codes=f.getlist('item_code[]'); qtys=f.getlist('item_qty[]'); rates=f.getlist('item_rate[]'); taxes=f.getlist('item_tax[]')
    items=[]
    for i,name in enumerate(names):
        if not name.strip(): continue
        try: qty=float(qtys[i]); rate=float(rates[i]); tax=float(taxes[i])
        except (ValueError,IndexError):
            return None, None, 'Every invoice item needs valid quantity, rate and tax values.'
        items.append((name.strip(), codes[i].strip() if i < len(codes) else '', qty, rate, tax))
    if not items or not f.get('bill_name','').strip():
        return None, None, 'Billing company and at least one item are required.'

    # GST is determined from the shipping state.
    # Telangana = intra-State (CGST + SGST); any other state = inter-State (IGST).
    ship_state=_invoice_state(f.get('ship_state'), keep_state)
    is_interstate=bool(ship_state) and ship_state.casefold() != 'telangana'
    try:
        if is_interstate:
            tax_mode='INTER'; cgst=0.0; sgst=0.0; igst=float(f.get('igst_pct') or 18)
        else:
            tax_mode='INTRA'; cgst=float(f.get('cgst_pct') or 9); sgst=float(f.get('sgst_pct') or 9); igst=0.0
        received=float(f.get('received_amount') or 0)
    except ValueError:
        return None, None, 'Tax percentages and the amount received must be numbers.'
    total_tax_pct = igst if is_interstate else (cgst + sgst)
    total=sum(q*r*(1+total_tax_pct/100) for _,_,q,r,_ in items)

    text=lambda key: f.get(key,'').strip()
    fields={
        'invoice_no': text('invoice_no'), 'invoice_date': text('invoice_date'), 'company_id': f.get('company_id') or None,
        'bill_name': text('bill_name'), 'bill_address': text('bill_address'), 'bill_gstin': text('bill_gstin').upper(),
        'bill_pan': text('bill_pan').upper(), 'bill_website': text('bill_website'),
        'ship_name': text('ship_name'), 'ship_address': text('ship_address'), 'ship_gstin': text('ship_gstin').upper(),
        'ship_pan': text('ship_pan').upper(), 'ship_website': text('ship_website'), 'ship_state': ship_state,
        'notes': text('notes'), 'terms': text('terms'), 'cgst_pct': cgst, 'sgst_pct': sgst, 'igst_pct': igst, 'tax_mode': tax_mode,
        'received_amount': received, 'total_amount': total,
        'bill_contact_name': text('bill_contact_name'), 'bill_contact_email': text('bill_contact_email'),
        'bill_contact_phone': text('bill_contact_phone'),
    }
    return fields, items, None


def _invoice_form_page(db, settings, invoice=None, items=()):
    companies=db.execute("SELECT * FROM companies ORDER BY name").fetchall()
    saved_state=invoice['ship_state'] if invoice else ''
    return render_template('invoice_form.html', companies=companies, settings=settings, invoice=invoice, items=items,
                           states=INVOICE_STATES, ship_state=_invoice_state(saved_state, saved_state),
                           next_invoice_no='' if invoice else _next_invoice_no(db, settings['invoice_prefix']))


@app.route('/invoices/new', methods=['GET','POST'])
@manager_required
def invoice_new():
    db=get_db(); settings=_invoice_settings(db)
    if request.method == 'GET':
        return _invoice_form_page(db, settings)
    fields, items, error = _read_invoice_form(request.form)
    if error:
        flash(error,'error'); return redirect(url_for('invoice_new'))
    fields['invoice_no']=fields['invoice_no'] or _next_invoice_no(db,settings['invoice_prefix'])
    columns=[*fields, 'created_by', 'created_at']
    try:
        cur=db.execute(f"INSERT INTO invoices ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                       [*fields.values(), current_user()['id'], now_str()])
        iid=cur.lastrowid
        db.executemany("INSERT INTO invoice_items (invoice_id,item_name,code,qty,rate,tax_pct) VALUES (?,?,?,?,?,?)",[(iid,*x) for x in items]); db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash('That invoice number already exists. Please use a different number.','error'); return redirect(url_for('invoice_new'))
    log_activity('Invoice Created', 'invoice', iid, fields['invoice_no'], fields['bill_name'])
    flash(f"Invoice {fields['invoice_no']} created.",'success'); return redirect(url_for('invoice_detail', invoice_id=iid))


@app.route('/invoices/<int:invoice_id>/edit', methods=['GET','POST'])
@manager_required
def invoice_edit(invoice_id):
    db=get_db(); settings=_invoice_settings(db)
    inv=db.execute("SELECT * FROM invoices WHERE id=?",(invoice_id,)).fetchone()
    if not inv: flash('Invoice not found.','error'); return redirect(url_for('invoices_list'))
    if request.method == 'GET':
        items=db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id",(invoice_id,)).fetchall()
        return _invoice_form_page(db, settings, inv, items)
    fields, items, error = _read_invoice_form(request.form, keep_state=inv['ship_state'])
    if error:
        flash(error,'error'); return redirect(url_for('invoice_edit', invoice_id=invoice_id))
    fields['invoice_no']=fields['invoice_no'] or inv['invoice_no']
    try:
        db.execute(f"UPDATE invoices SET {','.join(column + '=?' for column in fields)} WHERE id=?", [*fields.values(), invoice_id])
        db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
        db.executemany("INSERT INTO invoice_items (invoice_id,item_name,code,qty,rate,tax_pct) VALUES (?,?,?,?,?,?)",[(invoice_id,*x) for x in items]); db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash('That invoice number already exists. Please use a different number.','error'); return redirect(url_for('invoice_edit', invoice_id=invoice_id))
    log_activity('Invoice Updated', 'invoice', invoice_id, fields['invoice_no'], fields['bill_name'])
    flash(f"Invoice {fields['invoice_no']} updated.",'success'); return redirect(url_for('invoice_detail', invoice_id=invoice_id))


@app.route('/invoices/<int:invoice_id>')
@manager_required
def invoice_detail(invoice_id):
    db=get_db(); inv=db.execute("SELECT * FROM invoices WHERE id=?",(invoice_id,)).fetchone()
    if not inv: flash('Invoice not found.','error'); return redirect(url_for('invoices_list'))
    items=db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id",(invoice_id,)).fetchall()
    return render_template('invoice_detail.html', invoice=inv, items=items)


@app.route('/invoices/<int:invoice_id>/pdf')
@manager_required
def invoice_pdf(invoice_id):
    db=get_db(); inv=db.execute("SELECT * FROM invoices WHERE id=?",(invoice_id,)).fetchone(); settings=_invoice_settings(db)
    if not inv: flash('Invoice not found.','error'); return redirect(url_for('invoices_list'))
    rows=db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id",(invoice_id,)).fetchall()
    company={'name':settings['name'],'address_lines':settings['address_lines'],'website':settings['website'],'gstin':settings['gstin'],'pan':settings['pan']}
    bill={'name':inv['bill_name'],'address_lines':inv['bill_address'],'gstin':inv['bill_gstin'],'pan':inv['bill_pan'],'website':inv['bill_website']}
    ship={'name':inv['ship_name'] or inv['bill_name'],'address_lines':inv['ship_address'] or inv['bill_address'],'gstin':inv['ship_gstin'] or inv['bill_gstin'],'pan':inv['ship_pan'] or inv['bill_pan'],'website':inv['ship_website'] or inv['bill_website'],'state':inv['ship_state'] if 'ship_state' in inv.keys() else ''}
    items=[{'name':r['item_name'],'code':r['code'],'qty':r['qty'],'rate':r['rate'],'tax_pct':r['tax_pct']} for r in rows]
    bank={'name':settings['bank_name'],'ifsc':settings['ifsc'],'account_no':settings['account_no'],'bank':settings['bank_branch'],'upi_id':settings['upi_id']}
    pdf=build_invoice_pdf(company,bill,ship,inv['invoice_no'],inv['invoice_date'],items,inv['notes'],(inv['terms'] or '').splitlines(),bank,inv['cgst_pct'],inv['sgst_pct'],inv['received_amount'],inv['igst_pct'] if 'igst_pct' in inv.keys() else 0,inv['tax_mode'] if 'tax_mode' in inv.keys() else 'INTRA',inv['ship_state'] if 'ship_state' in inv.keys() else '')
    safe=inv['invoice_no'].replace('/','-')
    log_activity('Invoice Downloaded', 'invoice', invoice_id, inv['invoice_no'])
    return send_file(pdf,mimetype='application/pdf',as_attachment=True,download_name=f'invoice_{safe}.pdf')


@app.route('/invoices/<int:invoice_id>/delete', methods=['POST'])
@manager_required
def delete_invoice(invoice_id):
    db=get_db()
    inv=db.execute("SELECT * FROM invoices WHERE id=?",(invoice_id,)).fetchone()
    if not inv:
        flash('Invoice not found.','error'); return redirect(url_for('invoices_list'))
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    db.commit()
    log_activity('Invoice Deleted', 'invoice', invoice_id, inv['invoice_no'])
    flash(f"Invoice {inv['invoice_no']} deleted.", 'success')
    return redirect(url_for('invoices_list'))


@app.route('/settings/company', methods=['GET','POST'])
@manager_required
def company_settings():
    db=get_db()
    if request.method=='POST':
        f=request.form
        db.execute("""UPDATE company_settings SET name=?,address_lines=?,website=?,gstin=?,pan=?,bank_name=?,ifsc=?,account_no=?,bank_branch=?,upi_id=?,default_terms=?,default_notes=?,invoice_prefix=? WHERE id=1""",(
            f.get('name','').strip(),f.get('address_lines','').strip(),f.get('website','').strip(),f.get('gstin','').strip().upper(),f.get('pan','').strip().upper(),
            f.get('bank_name','').strip(),f.get('ifsc','').strip().upper(),f.get('account_no','').strip(),f.get('bank_branch','').strip(),f.get('upi_id','').strip(),
            f.get('default_terms','').strip(),f.get('default_notes','').strip(),f.get('invoice_prefix','').strip().upper() or 'QL')); db.commit()
        flash('Company invoice settings saved.','success'); return redirect(url_for('company_settings'))
    return render_template('company_settings.html', settings=_invoice_settings(db))


# ---------- Team tasks (status-grouped list with assignees, tags, subtasks and activity) ----------

WORK_STATUSES = ["To Do", "Planning", "In Progress", "At Risk", "Update Required", "On Hold", "Complete"]
WORK_PRIORITIES = ["Urgent", "High", "Normal", "Low"]


def _init_work_tasks(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'To Do',
            priority TEXT,
            start_date TEXT,
            due_date TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (parent_id) REFERENCES work_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_task_assignees (
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (task_id, user_id),
            FOREIGN KEY (task_id) REFERENCES work_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_task_tags (
            task_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (task_id, tag),
            FOREIGN KEY (task_id) REFERENCES work_tasks(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER,
            kind TEXT NOT NULL DEFAULT 'activity',
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES work_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_work_tasks_status ON work_tasks(status, parent_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_work_task_events_task ON work_task_events(task_id, id)")
    _add_cols(db, "work_tasks", [
        ("project_id", "INTEGER REFERENCES projects(id) ON DELETE SET NULL"),
        ("legacy_project_task_id", "INTEGER"),
        # Optional times of day (HH:MM) that go with start_date and due_date.
        ("start_time", "TEXT"),
        ("due_time", "TEXT"),
        ("estimate_minutes", "INTEGER"),   # how long the task is expected to take
        ("repeat", "TEXT"),                # daily, weekly or monthly: completing it opens the next one
        ("next_task_id", "INTEGER"),       # the occurrence that completing this task opened
    ])
    db.execute("CREATE INDEX IF NOT EXISTS idx_work_tasks_project ON work_tasks(project_id)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_task_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES work_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_work_task_files_task ON work_task_files(task_id, id)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_task_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            label TEXT NOT NULL,
            user_id INTEGER,
            created_at TEXT NOT NULL,
            removed_at TEXT,
            UNIQUE (task_id, url),
            FOREIGN KEY (task_id) REFERENCES work_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    _init_task_time(db)
    _migrate_project_tasks(db)
    _init_notifications(db)


def _migrate_project_tasks(db):
    """Copy tasks from the old per-project table into the Tasks module, once per task.

    The old project_tasks table is left in place as a record; legacy_project_task_id
    marks what has already been copied so restarts never duplicate anything.
    """
    status_map = {"To Do": "To Do", "In Progress": "In Progress", "Internal Review": "Update Required",
                  "Client Review": "Update Required", "Completed": "Complete"}
    priority_map = {"High": "High", "Medium": "Normal", "Low": "Low"}
    rows = db.execute(
        """SELECT id, project_id, title, description, assignee_id, start_date, due_date, priority, status,
                  created_by, created_at, updated_at, completed_at
           FROM project_tasks
           WHERE id NOT IN (SELECT legacy_project_task_id FROM work_tasks WHERE legacy_project_task_id IS NOT NULL)
           ORDER BY id""").fetchall()
    for (old_id, project_id, title, description, assignee_id, start_date, due_date, priority, status,
         created_by, created_at, updated_at, completed_at) in rows:
        new_status = status_map.get(status, "To Do")
        cur = db.execute(
            """INSERT INTO work_tasks (title, description, status, priority, start_date, due_date, created_by,
                                       created_at, updated_at, completed_at, project_id, legacy_project_task_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, description, new_status, priority_map.get(priority), start_date, due_date, created_by,
             created_at, updated_at, completed_at if new_status == "Complete" else None, project_id, old_id))
        if assignee_id and db.execute("SELECT 1 FROM users WHERE id=?", (assignee_id,)).fetchone():
            db.execute("INSERT OR IGNORE INTO work_task_assignees (task_id, user_id) VALUES (?,?)", (cur.lastrowid, assignee_id))
        db.execute("INSERT INTO work_task_events (task_id, user_id, kind, body, created_at) VALUES (?,?,?,?,?)",
                   (cur.lastrowid, created_by, "activity", "created this task", created_at))


# ---- time tracking ----
# One row per stretch of work on a task. ended_at is empty while the timer runs. A person
# has at most one running timer: starting another stops the first.
def _init_task_time(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS work_task_time (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            seconds INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES work_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_work_task_time_task ON work_task_time(task_id, id)")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_work_task_time_running
                  ON work_task_time(user_id) WHERE ended_at IS NULL""")


def _parse_duration(text):
    """Minutes from what a person types: 2h, 90m, 1.5h, 2h 30m, or a bare number of hours.

    Returns None for an empty box and raises ValueError for anything it cannot read.
    """
    text = (text or "").strip().lower()
    if not text:
        return None
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", text)
    if match and (match.group(1) or match.group(2)):
        minutes = float(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    else:
        minutes = float(text) * 60
    minutes = round(minutes)
    if not 0 < minutes <= 60 * 1000:
        raise ValueError("out of range")
    return minutes


def _seconds_since(stamp):
    started = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    return max(0, int((datetime.now(APP_TIMEZONE).replace(tzinfo=None) - started).total_seconds()))


def _duration_label(seconds):
    """A length of time as people say it: 45s, 12m, 2h 5m."""
    seconds = int(seconds or 0)
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _clock_label(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _running_timer(db, user):
    """The signed-in person's running timer, if any."""
    row = db.execute(
        """SELECT t.id, t.task_id, t.started_at, w.title FROM work_task_time t
           JOIN work_tasks w ON w.id = t.task_id WHERE t.user_id=? AND t.ended_at IS NULL""", (user["id"],)).fetchone()
    if not row:
        return None
    elapsed = _seconds_since(row["started_at"])
    return {"id": row["id"], "task_id": row["task_id"], "title": row["title"], "elapsed": elapsed, "clock": _clock_label(elapsed)}


def _stop_running_timer(db, user):
    timer = _running_timer(db, user)
    if timer:
        db.execute("UPDATE work_task_time SET ended_at=?, seconds=? WHERE id=?", (now_str(), timer["elapsed"], timer["id"]))
        _log_task_event(db, timer["task_id"], f"tracked {_duration_label(timer['elapsed'])}")
    return timer


def _task_time_view(db, task_id, user):
    """Time logged on one task: the entries, their total, and this person's running timer."""
    entries, total, running = [], 0, None
    for row in db.execute(
        """SELECT t.*, u.name AS user_name FROM work_task_time t
           LEFT JOIN users u ON u.id = t.user_id WHERE t.task_id=? ORDER BY t.id DESC""", (task_id,)):
        live = row["ended_at"] is None
        if live and row["user_id"] is None:
            continue  # a timer left running by someone whose account was removed
        seconds = _seconds_since(row["started_at"]) if live else row["seconds"]
        if live and row["user_id"] == user["id"]:
            running = {"elapsed": seconds, "clock": _clock_label(seconds)}
        if not live:
            total += seconds
        started = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
        entries.append({
            "id": row["id"], "user": row["user_name"] or "Former user", "initials": _initials(row["user_name"]),
            "tone": (row["user_id"] or 0) % 5, "day": f"{started.day} {started.strftime('%b')}",
            "label": _duration_label(seconds), "live": live,
            "can_remove": not live and (user["role"] == "manager" or row["user_id"] == user["id"]),
        })
    mine = _running_timer(db, user)
    return {
        "entries": entries[:30], "total_label": _duration_label(total) if total else "",
        "running": running, "elsewhere": mine if mine and mine["task_id"] != task_id else None,
    }


# ---- task attachments and links ----
# Files are kept outside the code folder (instance/task_files, or TASK_FILE_DIR) in
# Project name/Task name/ folders (a subtask's folder sits inside its parent task's), under the
# name they were uploaded with. stored_name is that path relative to the folder, with "/".
TASK_FILE_TYPES = {
    ".png": ("image/png", "Image"), ".jpg": ("image/jpeg", "Image"), ".jpeg": ("image/jpeg", "Image"),
    ".webp": ("image/webp", "Image"), ".gif": ("image/gif", "Image"),
    ".pdf": ("application/pdf", "PDF"),
    ".xls": ("application/vnd.ms-excel", "Excel"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Excel"),
    ".csv": ("text/csv", "CSV"),
    ".ppt": ("application/vnd.ms-powerpoint", "PowerPoint"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "PowerPoint"),
    ".doc": ("application/msword", "Word"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word"),
    ".txt": ("text/plain", "Text"),
}
TASK_FILE_MAX_BYTES = 10 * 1024 * 1024
TASK_FILE_MAX_PER_TASK = 30
_URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)


def _task_file_dir():
    return os.environ.get("TASK_FILE_DIR", "").strip() or os.path.join(app.instance_path, "task_files")


def _file_size_label(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _find_links(text):
    """(shown text, address) for each web link in the text, without duplicates."""
    links, seen = [], set()
    for match in _URL_PATTERN.finditer(text or ""):
        shown = match.group(0).rstrip(".,;:!?)]}")
        href = shown if shown.lower().startswith("http") else "https://" + shown
        if href not in seen:
            seen.add(href)
            links.append((shown, href))
    return links


@app.template_filter("linkify")
def linkify_filter(text):
    """Escape the text and turn web addresses in it into links."""
    text, out, position = text or "", [], 0
    for match in _URL_PATTERN.finditer(text):
        shown = match.group(0).rstrip(".,;:!?)]}")
        href = shown if shown.lower().startswith("http") else "https://" + shown
        out.append(escape(text[position:match.start()]))
        out.append(Markup('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>') % (href, shown))
        position = match.start() + len(shown)
    out.append(escape(text[position:]))
    return Markup("").join(out)


def _task_file_views(db, task, user):
    files = []
    for row in db.execute(
        """SELECT f.*, u.name AS user_name FROM work_task_files f
           LEFT JOIN users u ON u.id = f.user_id WHERE f.task_id=? ORDER BY f.id""", (task["id"],)):
        extension = os.path.splitext(row["stored_name"])[1].lower()
        kind = TASK_FILE_TYPES.get(extension, ("", "File"))[1]
        files.append({
            "id": row["id"], "name": row["original_name"], "kind": kind, "is_image": kind == "Image",
            "extension": extension.lstrip(".").upper(), "size": _file_size_label(row["size"]),
            "by": row["user_name"] or "Former user",
            "can_remove": user["role"] == "manager" or user["id"] in (row["user_id"], task["created_by"]),
        })
    return files


_FOLDER_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_FOLDER_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def _folder_name(text, fallback, limit=40):
    """A name that is safe as one folder or file-name part on Windows and Linux."""
    name = re.sub(r"\s+", " ", _FOLDER_UNSAFE.sub(" ", text or "")).strip(" .")[:limit].strip(" .")
    return fallback if not name or name.upper() in _FOLDER_RESERVED else name


def _task_folder(db, task):
    """Where a task's attachments live, relative to the attachments folder."""
    project = db.execute("SELECT name FROM projects WHERE id=?", (task["project_id"],)).fetchone() if task["project_id"] else None
    parts = [_folder_name(project["name"] if project else "", "No project")]
    if task["parent_id"]:
        parent = db.execute("SELECT title FROM work_tasks WHERE id=?", (task["parent_id"],)).fetchone()
        if parent:
            parts.append(_folder_name(parent["title"], f"Task {task['parent_id']}"))
    parts.append(_folder_name(task["title"], f"Task {task['id']}"))
    return "/".join(parts)


def _task_file_path(stored_name):
    """Full path of a stored attachment, or None when the name would leave the attachments folder."""
    base = os.path.realpath(_task_file_dir())
    path = os.path.realpath(os.path.join(base, *stored_name.split("/")))
    return path if path.startswith(base + os.sep) else None


def _free_stored_name(db, folder, original_name, extension, own_id=None):
    """A stored name in the folder that no other attachment, and no file on disk, already has."""
    stem = _folder_name(os.path.splitext(original_name)[0], "file", limit=60)
    for count in range(1, 1000):
        stored = f"{folder}/{stem}{'' if count == 1 else f' ({count})'}{extension}"
        taken = db.execute("SELECT 1 FROM work_task_files WHERE stored_name=? COLLATE NOCASE AND id IS NOT ?",
                           (stored, own_id)).fetchone()
        path = _task_file_path(stored)
        if not taken and path and not os.path.exists(path):
            return stored
    return f"{folder}/{uuid.uuid4().hex}{extension}"


def _prune_task_folders(folder):
    """Remove the folder, and the folders above it, while they are empty."""
    base = os.path.realpath(_task_file_dir())
    folder = os.path.realpath(folder)
    while folder.startswith(base + os.sep):
        try:
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def _file_task_files(db, task_ids=None):
    """Move attachments into their task's folder: after a rename, a move to another project, or an upgrade.

    Safe to repeat. A file that cannot be moved (already gone, path too long) stays where it is.
    """
    query, params = """SELECT f.id AS file_id, f.original_name, f.stored_name, t.id, t.title, t.project_id, t.parent_id
                       FROM work_task_files f JOIN work_tasks t ON t.id = f.task_id""", ()
    if task_ids is not None:
        task_ids = list(task_ids)
        if not task_ids:
            return
        query, params = query + f" WHERE t.id IN ({','.join('?' * len(task_ids))})", task_ids
    for row in db.execute(query + " ORDER BY f.id", params).fetchall():
        folder = _task_folder(db, row)
        if row["stored_name"].rpartition("/")[0] == folder:
            continue
        old = _task_file_path(row["stored_name"])
        if not old or not os.path.isfile(old):
            continue
        stored = _free_stored_name(db, folder, row["original_name"], os.path.splitext(row["stored_name"])[1].lower(), row["file_id"])
        new = _task_file_path(stored)
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            os.replace(old, new)
        except OSError:
            continue
        db.execute("UPDATE work_task_files SET stored_name=? WHERE id=?", (stored, row["file_id"]))
        db.commit()   # the row must not point at the old place once the file has moved
        _prune_task_folders(os.path.dirname(old))


def _refile_task_files():
    """At start-up, move attachments saved by older versions (flat, random names) into folders."""
    db = sqlite3.connect(DB_PATH, timeout=120)
    db.row_factory = sqlite3.Row
    try:
        _file_task_files(db)
    finally:
        db.close()


def _remove_task_files(stored_names):
    """Delete stored attachment files from disk. Missing files are ignored."""
    for name in stored_names:
        path = _task_file_path(name)
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass
        _prune_task_folders(os.path.dirname(path))


def _task_token():
    return session.setdefault("tasks_csrf", uuid.uuid4().hex)


def _check_task_token():
    expected = session.get("tasks_csrf")
    if not expected or not hmac.compare_digest(request.form.get("token", ""), expected):
        abort(400)


def _tasks_back(default):
    """Return the caller's /tasks or /projects URL when it supplied one, so filters survive a save."""
    target = request.form.get("next", "")
    return target if target.startswith(("/tasks", "/projects/")) and not target.startswith("//") else default


def _log_task_event(db, task_id, body, kind="activity"):
    db.execute(
        "INSERT INTO work_task_events (task_id,user_id,kind,body,created_at) VALUES (?,?,?,?,?)",
        (task_id, current_user()["id"], kind, body, now_str()),
    )


def _initials(name):
    words = (name or "").split()
    parts = [w for w in words if w[:1].isalnum()] or words or ["?"]
    return (parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")).upper()


def _due_view(value, done=False):
    """Short label for a due date, and whether it is overdue."""
    if not value:
        return None
    try:
        due = date.fromisoformat(value)
    except ValueError:
        return {"label": value, "overdue": False}
    today = datetime.now(APP_TIMEZONE).date()
    delta = (due - today).days
    if delta == 0:
        label = "Today"
    elif delta == 1:
        label = "Tomorrow"
    elif delta == -1:
        label = "Yesterday"
    elif -7 <= delta < 0:
        label = f"{-delta} days ago"
    elif due.year == today.year:
        label = f"{due.day} {due.strftime('%b')}"
    else:
        label = f"{due.day} {due.strftime('%b %Y')}"
    return {"label": label, "overdue": delta < 0 and not done}


def _task_views(db, rows):
    """Turn work_tasks rows into dicts carrying assignees, tags and display values."""
    ids = [row["id"] for row in rows]
    people, tags, tracked = {}, {}, {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in db.execute(
            f"""SELECT a.task_id, u.id, u.name FROM work_task_assignees a
                JOIN users u ON u.id = a.user_id WHERE a.task_id IN ({marks}) ORDER BY u.name""", ids):
            people.setdefault(r["task_id"], []).append(
                {"id": r["id"], "name": r["name"], "initials": _initials(r["name"]), "tone": r["id"] % 5})
        for r in db.execute(f"SELECT task_id, tag FROM work_task_tags WHERE task_id IN ({marks}) ORDER BY tag", ids):
            tags.setdefault(r["task_id"], []).append({"name": r["tag"], "tone": sum(map(ord, r["tag"])) % 6})
        for r in db.execute(
            f"""SELECT task_id, SUM(seconds) AS seconds,
                       SUM(ended_at IS NULL AND user_id IS NOT NULL) AS running
                FROM work_task_time WHERE task_id IN ({marks}) GROUP BY task_id""", ids):
            tracked[r["task_id"]] = (r["seconds"] or 0, bool(r["running"]))
    project_names = {}
    project_ids = sorted({row["project_id"] for row in rows if row["project_id"]})
    if project_ids:
        marks = ",".join("?" * len(project_ids))
        project_names = {r["id"]: r["name"] for r in db.execute(f"SELECT id, name FROM projects WHERE id IN ({marks})", project_ids)}
    views = []
    for row in rows:
        item = dict(row)
        item["project"] = ({"id": row["project_id"], "name": project_names[row["project_id"]]}
                           if row["project_id"] in project_names else None)
        item["assignees"] = people.get(row["id"], [])
        item["tags"] = tags.get(row["id"], [])
        item["due"] = _due_view(row["due_date"], row["status"] == "Complete")
        seconds, item["timer_running"] = tracked.get(row["id"], (0, False))
        item["tracked"] = _duration_label(seconds) if seconds else ""
        item["estimate"] = _duration_label(row["estimate_minutes"] * 60) if row["estimate_minutes"] else ""
        item["subtasks"] = []
        views.append(item)
    return views


def _visible_project_ids(db, user):
    clause, params = visible_projects_clause(user, "p")
    return {row["id"] for row in db.execute(f"SELECT p.id FROM projects p WHERE {clause}", params)}


def _can_see_task(db, user, task):
    """Who may open a task.

    Managers see every task. Everyone else sees tasks that belong to no project, tasks in a
    project they can see, and any task they created or are assigned to.
    """
    if user["role"] == "manager" or not task["project_id"] or task["created_by"] == user["id"]:
        return True
    if db.execute("SELECT 1 FROM work_task_assignees WHERE task_id=? AND user_id=?", (task["id"], user["id"])).fetchone():
        return True
    return task["project_id"] in _visible_project_ids(db, user)


def _get_work_task(db, task_id):
    row = db.execute("SELECT * FROM work_tasks WHERE id=?", (task_id,)).fetchone()
    if not row or not _can_see_task(db, current_user(), row):
        abort(404)   # the same answer for "no such task" and "not yours to see"
    return row


TASK_REPEATS = [("", "Does not repeat"), ("daily", "Every day"), ("weekly", "Every week"), ("monthly", "Every month")]


def _next_repeat_date(day, repeat):
    if repeat == "daily":
        return day + timedelta(days=1)
    if repeat == "weekly":
        return day + timedelta(days=7)
    year, month = day.year + (day.month == 12), day.month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _spawn_next_task(db, task):
    """When a repeating task is completed, open its next occurrence with the dates moved on.

    It happens once per task: reopening and completing it again does not make another copy.
    """
    if not task["repeat"] or task["next_task_id"]:
        return
    today = datetime.now(APP_TIMEZONE).date()

    def parse(value):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    start, due = parse(task["start_date"]), parse(task["due_date"])
    anchor = due or start or today
    following = _next_repeat_date(anchor, task["repeat"])
    while following <= today:
        following = _next_repeat_date(following, task["repeat"])
    shift = following - anchor
    new_start = (start + shift).isoformat() if start else None
    new_due = (due + shift).isoformat() if due else None if start else following.isoformat()
    now = now_str()
    new_id = db.execute(
        """INSERT INTO work_tasks (parent_id, project_id, title, description, status, priority, start_date, start_time,
                                   due_date, due_time, estimate_minutes, repeat, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task["parent_id"], task["project_id"], task["title"], task["description"], "To Do", task["priority"], new_start,
         task["start_time"], new_due, task["due_time"], task["estimate_minutes"], task["repeat"], task["created_by"], now, now)).lastrowid
    db.execute("INSERT INTO work_task_tags (task_id, tag) SELECT ?, tag FROM work_task_tags WHERE task_id=?", (new_id, task["id"]))
    for row in db.execute("SELECT user_id FROM work_task_assignees WHERE task_id=?", (task["id"],)).fetchall():
        db.execute("INSERT INTO work_task_assignees (task_id, user_id) VALUES (?,?)", (new_id, row["user_id"]))
        _notify_task_assigned(db, row["user_id"], new_id, task["title"])
    db.execute("UPDATE work_tasks SET next_task_id=? WHERE id=?", (new_id, task["id"]))
    label = dict(TASK_REPEATS)[task["repeat"]].lower()
    _log_task_event(db, new_id, f"created this task, which repeats {label}")
    _log_task_event(db, task["id"], f"opened the next one for {_moment_label(new_due or new_start)}")


def _set_task_status(db, task, status):
    if status == task["status"]:
        return
    now = now_str()
    db.execute("UPDATE work_tasks SET status=?, updated_at=?, completed_at=? WHERE id=?",
               (status, now, now if status == "Complete" else None, task["id"]))
    _log_task_event(db, task["id"], f"changed status from {task['status']} to {status}")
    _note_on_project(db, task["project_id"], "Task status updated", f"{task['title']}: {task['status']} → {status}")
    if status == "Complete":
        _spawn_next_task(db, task)


def _note_on_project(db, project_id, event, detail):
    """Mirror a task change into its project's activity feed."""
    if project_id:
        log_project_event(db, project_id, event, detail)
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_str(), project_id))


def _visible_project_options(db, user, always=None):
    """Projects this user may file a task under, as id/name pairs."""
    clause, params = visible_projects_clause(user, "p")
    options = [dict(r) for r in db.execute(f"SELECT p.id, p.name FROM projects p WHERE {clause} ORDER BY p.name", params)]
    if always and always["id"] not in {o["id"] for o in options}:
        options.append(always)
    return options


def _project_task_views(db, project_id):
    """Top-level tasks of one project in workflow order, each carrying its subtasks."""
    views = _task_views(db, db.execute("SELECT * FROM work_tasks WHERE project_id=? ORDER BY id", (project_id,)).fetchall())
    by_id = {item["id"]: item for item in views}
    top = []
    for item in views:
        parent = by_id.get(item["parent_id"])
        (parent["subtasks"] if parent else top).append(item)
    return sorted(top, key=lambda item: (WORK_STATUSES.index(item["status"]) if item["status"] in WORK_STATUSES else 0, item["id"]))


def _task_scope(db, user):
    """Tasks after the assignee and closed filters, shared by the list, board and calendar."""
    views = _task_views(db, db.execute("SELECT * FROM work_tasks ORDER BY id").fetchall())
    if user["role"] != "manager":
        open_projects = _visible_project_ids(db, user)
        views = [item for item in views
                 if not item["project_id"] or item["project_id"] in open_projects or item["created_by"] == user["id"]
                 or any(person["id"] == user["id"] for person in item["assignees"])]
    by_id = {item["id"]: item for item in views}
    top = []
    for item in views:
        parent = by_id.get(item["parent_id"])
        (parent["subtasks"] if parent else top).append(item)

    assignee = request.args.get("assignee", "")
    assignee_id = user["id"] if assignee == "me" else (int(assignee) if assignee.isdigit() else None)
    project_id = request.args.get("project", type=int)

    def involves(item):
        return not assignee_id or any(p["id"] == assignee_id for p in item["assignees"])

    def in_project(item):
        return not project_id or item["project_id"] == project_id

    show_closed = request.args.get("closed", "1") != "0"
    return {
        "top": [item for item in top if in_project(item) and (involves(item) or any(involves(sub) for sub in item["subtasks"]))],
        "all": [item for item in views if in_project(item) and involves(item)],
        "assignee": assignee,
        "project": project_id,
        "projects": _visible_project_options(db, user),
        "show_closed": show_closed,
        "users": db.execute("SELECT id, name FROM users ORDER BY name").fetchall(),
        "visible": views,
    }


def _tasks_page(template, scope, **extra):
    return render_template(
        template, users=scope["users"], assignee=scope["assignee"], show_closed=scope["show_closed"],
        token=_task_token(), statuses=WORK_STATUSES, priorities=WORK_PRIORITIES,
        project_filter=scope["project"], task_projects=scope["projects"],
        here=request.full_path.rstrip("?"), running_timer=_running_timer(get_db(), current_user()), **extra,
    )


@app.get("/tasks")
@login_required
def tasks_list():
    scope = _task_scope(get_db(), current_user())
    groups = []
    for status in reversed(WORK_STATUSES):
        if status == "Complete" and not scope["show_closed"]:
            continue
        items = [item for item in scope["top"] if item["status"] == status]
        if items or status == "To Do":
            groups.append({"status": status, "tasks": items})
    return _tasks_page("tasks.html", scope, view="list", month=None, groups=groups,
                       total=sum(len(g["tasks"]) for g in groups))


@app.get("/tasks/board")
@login_required
def tasks_board():
    scope = _task_scope(get_db(), current_user())
    columns = [
        {"status": status, "tasks": [item for item in scope["top"] if item["status"] == status]}
        for status in WORK_STATUSES
        if status != "Complete" or scope["show_closed"]
    ]
    return _tasks_page("tasks_board.html", scope, view="board", month=None, columns=columns,
                       total=sum(len(c["tasks"]) for c in columns))


@app.get("/tasks/calendar")
@login_required
def tasks_calendar():
    scope = _task_scope(get_db(), current_user())
    today = datetime.now(APP_TIMEZONE).date()
    try:
        year, month_no = map(int, request.args.get("month", "").split("-"))
        first = date(year, month_no, 1)
    except ValueError:
        first = today.replace(day=1)
    visible = [item for item in scope["all"] if scope["show_closed"] or item["status"] != "Complete"]
    by_day = {}
    for item in visible:
        if item["due_date"]:
            by_day.setdefault(item["due_date"], []).append(item)
    weeks = [
        [{
            "iso": day.isoformat(), "day": day.day, "label": f"{day.strftime('%A')} {day.day} {day.strftime('%B')}",
            "in_month": day.month == first.month, "is_today": day == today,
            "tasks": by_day.get(day.isoformat(), []),
        } for day in week]
        for week in calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    ]
    previous = (first - timedelta(days=1)).replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    open_items = [item for item in scope["all"] if item["status"] != "Complete"]
    return _tasks_page(
        "tasks_calendar.html", scope, view="calendar", month=first.strftime("%Y-%m"),
        weeks=weeks, month_label=first.strftime("%B %Y"),
        prev_month=previous.strftime("%Y-%m"), next_month=following.strftime("%Y-%m"),
        this_month=today.strftime("%Y-%m"),
        overdue=sorted((i for i in open_items if i["due_date"] and i["due_date"] < today.isoformat()), key=lambda i: i["due_date"]),
        unscheduled=[i for i in open_items if not i["due_date"]],
        total=sum(len(cell["tasks"]) for week in weeks for cell in week if cell["in_month"]),
    )


GANTT_DAY_WIDTHS = {"day": 16, "week": 36, "month": 16, "quarter": 6}   # "day" is the width of 15 minutes


def _clock(value):
    """A time of day from a form as HH:MM, or None when it was left empty."""
    value = (value or "").strip()
    return datetime.strptime(value[:5], "%H:%M").strftime("%H:%M") if value else None


def _moment_label(day, clock=None):
    if not day:
        return ""
    when = date.fromisoformat(day)
    text = f"{when.day} {when.strftime('%b')}"
    if clock:
        text += " " + datetime.strptime(clock, "%H:%M").strftime("%I:%M %p").lstrip("0")
    return text


def _schedule_label(start, start_time, due, due_time):
    parts = [part for part in (_moment_label(start, start_time), _moment_label(due, due_time)) if part]
    return " to ".join(parts[:1] if len(set(parts)) == 1 else parts) or "no dates"


def _task_moments(item):
    """When a task starts and ends, to the minute, or None if it has no dates.

    A start date with no time begins that day and a due date with no time runs to the end
    of it. A task with a single date takes that whole day, or one hour if it has a time.
    """
    def moment(day, clock, at_end):
        try:
            midnight = datetime.combine(date.fromisoformat(day), datetime.min.time())
        except (TypeError, ValueError):
            return None
        try:
            hours = datetime.strptime(clock, "%H:%M")
        except (TypeError, ValueError):
            return midnight + timedelta(days=1 if at_end else 0)
        return midnight + timedelta(hours=hours.hour, minutes=hours.minute)

    start = moment(item["start_date"], item["start_time"], False)
    end = moment(item["due_date"], item["due_time"], True)
    if start is None and end is None:
        return None
    if start is None:
        start = end - (timedelta(hours=1) if item["due_time"] else timedelta(days=1))
    if end is None:
        end = start + (timedelta(hours=1) if item["start_time"] else timedelta(days=1))
    return start, max(end, start + timedelta(minutes=15))


def _gantt_grouped(rows):
    """Put Gantt rows under a heading per project, each with a bar spanning its tasks."""
    blocks = []
    for row in rows:
        if row["sub"] and blocks:
            blocks[-1].append(row)
        else:
            blocks.append([row])
    groups = {}
    for block in blocks:
        project = block[0]["task"]["project"]
        groups.setdefault(project["name"] if project else None, []).extend(block)
    out = []
    for name in sorted(groups, key=lambda value: (value is None, (value or "").lower())):
        members = groups[name]
        bars = [row["bar"] for row in members if row["bar"]]
        summary = None
        if bars:
            start = min(bar["start"] for bar in bars)
            summary = {"start": start, "days": max(bar["start"] + bar["days"] for bar in bars) - start}
        out.append({"group": name or "No project", "count": len(members), "summary": summary,
                    "task": None, "bar": None, "sub": False, "dated": True})
        out.extend(members)
    return out


def _tasks_gantt_day(scope, today, group):
    """The Gantt at its closest scale: two weeks around today, in 15-minute steps."""
    step, width, day_count = timedelta(minutes=15), GANTT_DAY_WIDTHS["day"], 14
    now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    # ?start= pages the two-week window; without it the window sits around today.
    try:
        opening, paged = date.fromisoformat(request.args.get("start", "")), True
    except ValueError:
        opening, paged = today - timedelta(days=3), False
    first = datetime.combine(opening, datetime.min.time())
    last = first + timedelta(days=day_count)

    def shown(item):
        return scope["show_closed"] or item["status"] != "Complete"

    def order(item):
        when = _task_moments(item)
        return (when is None, when[0] if when else now, item["id"])

    def stamp(when):
        return f"{when.day} {when.strftime('%b')} " + when.strftime("%I:%M %p").lstrip("0")

    rows = []
    for item in sorted(filter(shown, scope["top"]), key=order):
        for task, is_sub in [(item, False)] + [(sub, True) for sub in sorted(filter(shown, item["subtasks"]), key=order)]:
            when, bar = _task_moments(task), None
            if when and when[1] > first and when[0] < last:
                start, end = max(when[0], first), min(when[1], last)
                offset = (start - first) // step
                bar = {"start": offset, "days": max(1, -((first - end) // step) - offset),
                       "label": f"{stamp(when[0])} to {stamp(when[1])}", "has": "both", "clipped": (start, end) != when}
            rows.append({"task": task, "sub": is_sub, "bar": bar, "dated": bool(when)})

    if group:
        rows = _gantt_grouped(rows)
    days = [first.date() + timedelta(days=offset) for offset in range(day_count)]
    return _tasks_page(
        "tasks_gantt.html", scope, view="gantt", month=None, rows=rows, zoom="day", day_width=width, day_cells=[],
        days=day_count * 96, unit_minutes=15, first_day=first.strftime("%Y-%m-%dT%H:%M"),
        grouped=group, day_paged=paged, prev_start=(opening - timedelta(days=7)).isoformat(),
        next_start=(opening + timedelta(days=7)).isoformat(),
        months=[{"days": 96, "label": f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"} for day in days],
        weeks=[{"label": f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}", "current": day == today and hour == now.hour}
               for day in days for hour in range(24)],
        today_offset=round((now - first) / step - 0.5, 2) if first <= now < last else "",
        range_label=f"{days[0].day} {days[0].strftime('%B')} to {days[-1].day} {days[-1].strftime('%B %Y')}",
        total=sum(1 for row in rows if row["bar"]), unscheduled=sum(1 for row in rows if row["task"] and not row["bar"]),
    )


@app.get("/tasks/gantt")
@login_required
def tasks_gantt():
    scope = _task_scope(get_db(), current_user())
    today = datetime.now(APP_TIMEZONE).date()
    zoom = request.args.get("zoom")
    if zoom in GANTT_DAY_WIDTHS:
        session["tasks_gantt_zoom"] = zoom
    else:
        zoom = session.get("tasks_gantt_zoom", "day")
        zoom = zoom if zoom in GANTT_DAY_WIDTHS else "day"
    group = request.args.get("group")
    if group in ("project", "none"):
        session["tasks_gantt_group"] = group
    group = session.get("tasks_gantt_group") == "project"
    if zoom == "day":
        return _tasks_gantt_day(scope, today, group)
    day_width = GANTT_DAY_WIDTHS[zoom]

    def span(item):
        """First and last day of a task. A task with one date takes that single day."""
        days = []
        for value in (item["start_date"], item["due_date"]):
            try:
                days.append(date.fromisoformat(value))
            except (TypeError, ValueError):
                pass
        return (min(days), max(days)) if days else None

    def shown(item):
        return scope["show_closed"] or item["status"] != "Complete"

    def order(item):
        when = span(item)
        return (when is None, when[0] if when else today, item["id"])

    listed = []
    for item in sorted(filter(shown, scope["top"]), key=order):
        listed.append((item, False))
        listed.extend((sub, True) for sub in sorted(filter(shown, item["subtasks"]), key=order))

    # The timeline covers every dated task plus some room either side, but never more
    # than a year back or two years ahead, so one stray date cannot stretch it.
    spans = [when for when in (span(item) for item, _ in listed) if when]
    earliest, latest = today - timedelta(days=365), today + timedelta(days=730)
    first = max(min([when[0] for when in spans] + [today]) - timedelta(days=14), earliest)
    last = min(max([when[1] for when in spans] + [today]) + timedelta(days=28), latest)
    last = max(last, first + timedelta(days=1600 // day_width))
    first -= timedelta(days=(first.weekday() + 1) % 7)   # back to Sunday
    last += timedelta(days=(5 - last.weekday()) % 7)     # on to Saturday
    days = (last - first).days + 1

    rows = []
    for item, is_sub in listed:
        when, bar = span(item), None
        if when and when[1] >= first and when[0] <= last:
            start, end = max(when[0], first), min(when[1], last)
            label = f"{when[0].day} {when[0].strftime('%b')}"
            if when[1] != when[0]:
                label += f" to {when[1].day} {when[1].strftime('%b')}"
            bar = {"start": (start - first).days, "days": (end - start).days + 1, "label": label,
                   # Which dates the task has, so moving a one-date task keeps it one-date; a bar
                   # cut off by the edge of the timeline cannot be dragged.
                   "has": "both" if item["start_date"] and item["due_date"] else "start" if item["start_date"] else "due",
                   "clipped": (start, end) != when}
        rows.append({"task": item, "sub": is_sub, "bar": bar, "dated": bool(when)})

    if group:
        rows = _gantt_grouped(rows)
    months, cursor = [], first
    while cursor <= last:
        following = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        length = (min(following - timedelta(days=1), last) - cursor).days + 1
        months.append({"days": length, "label": cursor.strftime("%b %Y") if length * day_width >= 64 else ""})
        cursor = following
    weeks = []
    for offset in range(0, days, 7):
        start = first + timedelta(days=offset)
        end = start + timedelta(days=6)
        label = f"W{int(start.strftime('%U'))} {start.day}–{end.day}" if day_width >= 16 else str(start.day)
        weeks.append({"label": label, "current": start <= today <= end})
    day_cells = []
    if zoom == "week":   # wide enough to name every day
        for offset in range(days):
            day = first + timedelta(days=offset)
            day_cells.append({"label": f"{day.strftime('%a')[0]} {day.day}", "off": day.weekday() >= 5, "today": day == today})
    return _tasks_page(
        "tasks_gantt.html", scope, view="gantt", month=None, rows=rows, zoom=zoom, day_width=day_width, day_cells=day_cells,
        grouped=group,
        days=days, months=months, weeks=weeks, today_offset=(today - first).days, first_day=first.isoformat(), unit_minutes=1440,
        range_label=f"{first.day} {first.strftime('%B %Y')} to {last.day} {last.strftime('%B %Y')}",
        total=sum(1 for row in rows if row["bar"]), unscheduled=sum(1 for row in rows if row["task"] and not row["bar"]),
    )


@app.post("/tasks/new")
@login_required
def task_create():
    _check_task_token()
    db, user = get_db(), current_user()
    title = request.form.get("title", "").strip()
    status = request.form.get("status", "To Do")
    parent_id = request.form.get("parent_id", type=int)
    project_id = request.form.get("project_id", type=int)
    if parent_id:
        parent = _get_work_task(db, parent_id)
        if parent["parent_id"]:
            abort(400)  # one level of subtasks only
        project_id = parent["project_id"]  # a subtask always belongs to its parent's project
    elif project_id and project_id not in _visible_project_ids(db, user):
        abort(400)
    default = url_for("task_detail", task_id=parent_id) if parent_id else url_for("tasks_list")
    if not title or len(title) > 200 or status not in WORK_STATUSES:
        flash("Enter a task name of up to 200 characters.", "error")
        return redirect(_tasks_back(default))
    due = request.form.get("due_date") or None
    try:
        due = date.fromisoformat(due).isoformat() if due else None
    except ValueError:
        flash("Enter a valid due date.", "error")
        return redirect(_tasks_back(default))
    now = now_str()
    cur = db.execute(
        "INSERT INTO work_tasks (parent_id,project_id,title,status,due_date,created_by,created_at,updated_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (parent_id, project_id or None, title, status, due, user["id"], now, now, now if status == "Complete" else None),
    )
    _note_on_project(db, project_id, "Task added", title)
    _log_task_event(db, cur.lastrowid, "created this task")
    if parent_id:
        _log_task_event(db, parent_id, f"added subtask {title}")
    db.commit()
    log_activity("Created task", "task", cur.lastrowid, title)
    return redirect(_tasks_back(default))


_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TASK_DATE_EVENTS = ("scheduled this task for ", "set start date to ", "set due date to ", "updated the description")


def _plain_day(match):
    """2026-11-26 inside an activity line reads as 26 Nov 2026."""
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return match.group(0)
    return f"{day.day} {day.strftime('%b %Y')}"


@app.get("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    db, user = get_db(), current_user()
    task = _task_views(db, [_get_work_task(db, task_id)])[0]
    # Opening a task clears its "assigned to you" notification.
    if db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND task_id=? AND read_at IS NULL",
                  (now_str(), user["id"], task_id)).rowcount:
        db.commit()
        _live_touch()
    parent = db.execute("SELECT id, title FROM work_tasks WHERE id=?", (task["parent_id"],)).fetchone() if task["parent_id"] else None
    task["subtasks"] = _task_views(db, db.execute("SELECT * FROM work_tasks WHERE parent_id=? ORDER BY id", (task_id,)).fetchall())
    days = []
    for row in db.execute(
        """SELECT e.*, u.name AS user_name FROM work_task_events e
           LEFT JOIN users u ON u.id = e.user_id WHERE e.task_id=? ORDER BY e.id""", (task_id,)):
        stamp = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        label = f"{stamp.strftime('%A')}, {stamp.day} {stamp.strftime('%B')}"
        if not days or days[-1]["label"] != label:
            days.append({"label": label, "events": []})
        body, topic = row["body"], None
        if row["kind"] != "comment":
            topic = next((p for p in _TASK_DATE_EVENTS if body.startswith(p)), None)
            if topic and " date to " in topic:   # only date lines; a file name can hold a date too
                body = _ISO_DAY.sub(_plain_day, body)
            # Several date changes in a row by one person read as a single line: keep the latest.
            last = days[-1]["events"][-1] if days[-1]["events"] else None
            if topic and last and last["topic"] == topic and last["user_id"] == row["user_id"] \
                    and (stamp - last["stamp"]).total_seconds() <= 600:
                days[-1]["events"].pop()
        days[-1]["events"].append({
            "topic": topic, "user_id": row["user_id"], "stamp": stamp,
            "kind": row["kind"], "body": body, "user": row["user_name"] or "Former user",
            "initials": _initials(row["user_name"]), "tone": (row["user_id"] or 0) % 5,
            "time": stamp.strftime("%I:%M %p").lstrip("0"),
        })
    created = datetime.strptime(task["created_at"], "%Y-%m-%d %H:%M:%S")
    return render_template(
        "task_detail.html", task=task, parent=parent, days=days,
        created_label=f"{created.day} {created.strftime('%b %Y')}",
        users=db.execute("SELECT id, name FROM users ORDER BY name").fetchall(),
        statuses=WORK_STATUSES, priorities=WORK_PRIORITIES, token=_task_token(),
        task_projects=_visible_project_options(db, user, task["project"]),
        can_delete=user["role"] == "manager" or task["created_by"] == user["id"],
        files=_task_file_views(db, task, user), time=_task_time_view(db, task_id, user), today=today_str(),
        repeats=TASK_REPEATS,
        links=db.execute("SELECT id, url, label FROM work_task_links WHERE task_id=? AND removed_at IS NULL ORDER BY id",
                         (task_id,)).fetchall(),
        file_accept=",".join(sorted(TASK_FILE_TYPES)), file_max_mb=TASK_FILE_MAX_BYTES // (1024 * 1024),
    )


@app.post("/tasks/<int:task_id>/files")
@login_required
def task_file_upload(task_id):
    _check_task_token()
    db, user = get_db(), current_user()
    task = _get_work_task(db, task_id)
    folder = _task_folder(db, task)
    have = db.execute("SELECT count(*) FROM work_task_files WHERE task_id=?", (task_id,)).fetchone()[0]
    saved, problems = [], []
    for upload in request.files.getlist("files"):
        name = os.path.basename((upload.filename or "").replace("\\", "/")).strip()[:150]
        if not name:
            continue
        extension = os.path.splitext(name)[1].lower()
        if extension not in TASK_FILE_TYPES:
            problems.append(f"{name}: this file type is not supported.")
            continue
        if have + len(saved) >= TASK_FILE_MAX_PER_TASK:
            problems.append(f"{name}: a task can hold up to {TASK_FILE_MAX_PER_TASK} attachments.")
            continue
        data = upload.read(TASK_FILE_MAX_BYTES + 1)
        if not data:
            problems.append(f"{name}: the file is empty.")
            continue
        if len(data) > TASK_FILE_MAX_BYTES:
            problems.append(f"{name}: files can be up to {TASK_FILE_MAX_BYTES // (1024 * 1024)} MB.")
            continue
        stored = _free_stored_name(db, folder, name, extension)
        path = _task_file_path(stored)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError:
            problems.append(f"{name}: the file could not be saved.")
            continue
        db.execute(
            "INSERT INTO work_task_files (task_id,user_id,original_name,stored_name,size,created_at) VALUES (?,?,?,?,?,?)",
            (task_id, user["id"], name, stored, len(data), now_str()))
        _log_task_event(db, task_id, f"attached {name}")
        saved.append(name)
    if saved:
        db.execute("UPDATE work_tasks SET updated_at=? WHERE id=?", (now_str(), task_id))
        db.commit()
    for problem in problems:
        flash(problem, "error")
    if request.accept_mimetypes.best == "application/json":
        return jsonify(saved=len(saved), problems=problems)
    return redirect(url_for("task_detail", task_id=task_id))


@app.get("/tasks/files/<int:file_id>")
@login_required
def task_file(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM work_task_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        abort(404)
    _get_work_task(db, row["task_id"])
    path = _task_file_path(row["stored_name"])
    if not path or not os.path.isfile(path):
        abort(404)
    extension = os.path.splitext(row["stored_name"])[1].lower()
    mime, kind = TASK_FILE_TYPES.get(extension, ("application/octet-stream", "File"))
    # Only images and PDFs open in the browser; everything else downloads.
    inline = kind in ("Image", "PDF") and "download" not in request.args
    response = send_file(path, mimetype=mime, as_attachment=not inline, download_name=row["original_name"])
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.post("/tasks/<int:task_id>/links")
@login_required
def task_link_add(task_id):
    """Add the web addresses typed into the Links field of a task."""
    _check_task_token()
    db, user = get_db(), current_user()
    _get_work_task(db, task_id)
    added = 0
    for typed in request.form.get("url", "").split()[:10]:
        shown = typed.strip(".,;")[:300]
        href = shown if re.match(r"(?i)https?://", shown) else "https://" + shown
        try:
            host = urlsplit(href).hostname or ""
        except ValueError:
            continue
        if "." not in host.strip(".") or not _URL_PATTERN.fullmatch(href):
            continue
        row = db.execute("SELECT id, removed_at FROM work_task_links WHERE task_id=? AND url=?", (task_id, href)).fetchone()
        if row is None:
            db.execute("INSERT INTO work_task_links (task_id,url,label,user_id,created_at) VALUES (?,?,?,?,?)",
                       (task_id, href, shown, user["id"], now_str()))
        elif row["removed_at"]:
            db.execute("UPDATE work_task_links SET removed_at=NULL WHERE id=?", (row["id"],))
        else:
            added += 1   # already on the task: nothing to do, and not an error
            continue
        _log_task_event(db, task_id, f"added link {shown[:120]}")
        added += 1
    if added:
        db.commit()
    else:
        flash("Enter a web address, such as https://example.com.", "error")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/links/<int:link_id>/delete")
@login_required
def task_link_delete(link_id):
    _check_task_token()
    db = get_db()
    row = db.execute("SELECT * FROM work_task_links WHERE id=? AND removed_at IS NULL", (link_id,)).fetchone()
    if not row:
        abort(404)
    _get_work_task(db, row["task_id"])
    db.execute("UPDATE work_task_links SET removed_at=? WHERE id=?", (now_str(), link_id))
    _log_task_event(db, row["task_id"], f"removed link {row['label'][:120]}")
    db.commit()
    return redirect(url_for("task_detail", task_id=row["task_id"]))


@app.post("/tasks/files/<int:file_id>/delete")
@login_required
def task_file_delete(file_id):
    _check_task_token()
    db, user = get_db(), current_user()
    row = db.execute("SELECT * FROM work_task_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        abort(404)
    task = _get_work_task(db, row["task_id"])
    if user["role"] != "manager" and user["id"] not in (row["user_id"], task["created_by"]):
        flash("Only a manager, the task's creator or the person who attached a file can remove it.", "error")
        return redirect(url_for("task_detail", task_id=task["id"]))
    db.execute("DELETE FROM work_task_files WHERE id=?", (file_id,))
    _log_task_event(db, task["id"], f"removed attachment {row['original_name']}")
    db.commit()
    _remove_task_files([row["stored_name"]])
    return redirect(url_for("task_detail", task_id=task["id"]))


@app.post("/tasks/<int:task_id>/update")
@login_required
def task_update(task_id):
    _check_task_token()
    db = get_db()
    task = _get_work_task(db, task_id)
    here = redirect(url_for("task_detail", task_id=task_id))
    form = request.form
    title = form.get("title", "").strip()
    status = form.get("status", task["status"])
    priority = form.get("priority", "") or None
    try:
        if not title or len(title) > 200:
            raise ValueError("Enter a task name of up to 200 characters.")
        if status not in WORK_STATUSES or (priority and priority not in WORK_PRIORITIES):
            raise ValueError("Choose a valid status and priority.")
        try:
            start = date.fromisoformat(form["start_date"]).isoformat() if form.get("start_date") else None
            due = date.fromisoformat(form["due_date"]).isoformat() if form.get("due_date") else None
            start_time = _clock(form["start_time"]) if "start_time" in form else task["start_time"]
            due_time = _clock(form["due_time"]) if "due_time" in form else task["due_time"]
        except ValueError:
            raise ValueError("Enter a valid start date and due date.")
        if start and due and due < start:
            raise ValueError("Choose a due date on or after the start date.")
        start_time, due_time = start_time if start else None, due_time if due else None
        if start and start == due and start_time and due_time and due_time <= start_time:
            raise ValueError("Choose a due time after the start time.")
        try:
            estimate = _parse_duration(form["estimate"]) if "estimate" in form else task["estimate_minutes"]
        except ValueError:
            raise ValueError("Enter the estimate as hours and minutes, like 2h 30m.")
        repeat = form.get("repeat", task["repeat"] or "")
        if repeat not in dict(TASK_REPEATS):
            raise ValueError("Choose how often the task repeats.")
    except ValueError as error:
        flash(str(error), "error")
        return here

    description = form.get("description", "").strip()[:20000]
    names = {u["id"]: u["name"] for u in db.execute("SELECT id, name FROM users")}
    wanted = {uid for uid in form.getlist("assignees", type=int) if uid in names}
    current = {r["user_id"] for r in db.execute("SELECT user_id FROM work_task_assignees WHERE task_id=?", (task_id,))}
    new_tags = []
    for raw in form.get("tags", "").split(","):
        tag = " ".join(raw.split()).lower()[:40]
        if tag and tag not in new_tags:
            new_tags.append(tag)
    new_tags = new_tags[:12]
    old_tags = [r["tag"] for r in db.execute("SELECT tag FROM work_task_tags WHERE task_id=?", (task_id,))]

    if title != task["title"]:
        _log_task_event(db, task_id, f"renamed this task to {title}")
    if description != (task["description"] or ""):
        _log_task_event(db, task_id, "updated the description")
    if priority != task["priority"]:
        _log_task_event(db, task_id, f"set priority to {priority}" if priority else "cleared the priority")
    if start != task["start_date"]:
        _log_task_event(db, task_id, f"set start date to {start}" if start else "cleared the start date")
    if due != task["due_date"]:
        _log_task_event(db, task_id, f"set due date to {due}" if due else "cleared the due date")
    if repeat != (task["repeat"] or ""):
        _log_task_event(db, task_id, f"set this task to repeat {dict(TASK_REPEATS)[repeat].lower()}" if repeat else "stopped this task repeating")
    if estimate != task["estimate_minutes"]:
        _log_task_event(db, task_id, f"set the estimate to {_duration_label(estimate * 60)}" if estimate else "cleared the estimate")
    if (start_time, due_time) != (task["start_time"], task["due_time"]) and (start, due) == (task["start_date"], task["due_date"]):
        _log_task_event(db, task_id, "set the time to " + _schedule_label(start, start_time, due, due_time))
    for uid in sorted(wanted - current):
        db.execute("INSERT INTO work_task_assignees (task_id,user_id) VALUES (?,?)", (task_id, uid))
        _log_task_event(db, task_id, f"assigned this task to {names[uid]}")
        _notify_task_assigned(db, uid, task_id, title)
    for uid in sorted(current - wanted):
        db.execute("DELETE FROM work_task_assignees WHERE task_id=? AND user_id=?", (task_id, uid))
        _log_task_event(db, task_id, f"unassigned {names.get(uid, 'a former user')}")
    for tag in new_tags:
        if tag not in old_tags:
            db.execute("INSERT INTO work_task_tags (task_id,tag) VALUES (?,?)", (task_id, tag))
            _log_task_event(db, task_id, f"added tag {tag}")
    for tag in old_tags:
        if tag not in new_tags:
            db.execute("DELETE FROM work_task_tags WHERE task_id=? AND tag=?", (task_id, tag))
            _log_task_event(db, task_id, f"removed tag {tag}")
    if "project_id" in form and not task["parent_id"]:
        new_project = form.get("project_id", type=int)
        if new_project and new_project != task["project_id"] and new_project not in _visible_project_ids(db, current_user()):
            new_project = task["project_id"]
        project_row = db.execute("SELECT id, name FROM projects WHERE id=?", (new_project,)).fetchone() if new_project else None
        new_project = project_row["id"] if project_row else None
        if new_project != task["project_id"]:
            db.execute("UPDATE work_tasks SET project_id=? WHERE id=? OR parent_id=?", (new_project, task_id, task_id))
            _log_task_event(db, task_id, f"moved this task to project {project_row['name']}" if project_row
                            else "removed this task from its project")
            _note_on_project(db, task["project_id"], "Task removed", title)
            _note_on_project(db, new_project, "Task added", title)
    db.execute(
        """UPDATE work_tasks SET title=?, description=?, priority=?, start_date=?, start_time=?, due_date=?, due_time=?,
                  estimate_minutes=?, repeat=?, updated_at=? WHERE id=?""",
        (title, description, priority, start, start_time, due, due_time, estimate, repeat or None, now_str(), task_id),
    )
    _set_task_status(db, task, status)
    db.commit()
    # A new title or project means a new folder for this task's files and its subtasks' files.
    _file_task_files(db, [row["id"] for row in db.execute("SELECT id FROM work_tasks WHERE id=? OR parent_id=?", (task_id, task_id))])
    _live_touch()
    return here


@app.post("/tasks/<int:task_id>/dates")
@login_required
def task_set_dates(task_id):
    """Save when a task happens after its bar was dragged on the Gantt.

    The week, month and quarter scales send whole days and leave any times as they are.
    The day scale sends exact moments (start_at, end_at) in 15-minute steps.
    """
    _check_task_token()
    db = get_db()
    task = _get_work_task(db, task_id)
    form = request.form
    start_time, due_time = task["start_time"], task["due_time"]
    try:
        if form.get("start_at") or form.get("end_at"):
            begin = datetime.strptime(form["start_at"], "%Y-%m-%dT%H:%M")
            finish = datetime.strptime(form["end_at"], "%Y-%m-%dT%H:%M")
            if finish <= begin:
                abort(400)
            # Midnight is stored as "no time": a day that starts at 0:00 or ends at 24:00 is just that day.
            start, start_time = begin.date().isoformat(), begin.strftime("%H:%M") if begin.hour or begin.minute else None
            if finish.hour or finish.minute:
                due, due_time = finish.date().isoformat(), finish.strftime("%H:%M")
            else:
                due, due_time = (finish - timedelta(days=1)).date().isoformat(), None
        else:
            start = date.fromisoformat(form["start_date"]).isoformat() if form.get("start_date") else None
            due = date.fromisoformat(form["due_date"]).isoformat() if form.get("due_date") else None
            if start and due and due < start:
                abort(400)
            start_time, due_time = start_time if start else None, due_time if due else None
            if start and start == due and start_time and due_time and due_time <= start_time:
                start_time = due_time = None   # squeezed onto one day where the old times no longer fit
    except (KeyError, ValueError):
        abort(400)
    if (start, start_time, due, due_time) != (task["start_date"], task["start_time"], task["due_date"], task["due_time"]):
        _log_task_event(db, task_id, "scheduled this task for " + _schedule_label(start, start_time, due, due_time)
                        if start or due else "took this task off the schedule")
        db.execute("UPDATE work_tasks SET start_date=?, start_time=?, due_date=?, due_time=?, updated_at=? WHERE id=?",
                   (start, start_time, due, due_time, now_str(), task_id))
        db.commit()
        _live_touch()
    return "", 204


TIME_PERIODS = [("week", "This week"), ("last_week", "Last week"), ("month", "This month"), ("last_month", "Last month")]


def _period_range(period, today):
    monday = today - timedelta(days=today.weekday())
    if period == "last_week":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if period == "month":
        return today.replace(day=1), today
    if period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return monday, monday + timedelta(days=6)


@app.get("/tasks/time")
@login_required
def tasks_time():
    """Where the tracked time went: by person, project, client and task for a chosen period.

    Managers see everyone and can narrow to one person. Everyone else sees their own time.
    """
    db, user = get_db(), current_user()
    scope = _task_scope(db, user)
    today = datetime.now(APP_TIMEZONE).date()
    period = request.args.get("period")
    if period in dict(TIME_PERIODS):
        session["tasks_time_period"] = period
    else:
        period = session.get("tasks_time_period", "week")
        period = period if period in dict(TIME_PERIODS) else "week"
    first, last = _period_range(period, today)
    if user["role"] != "manager":
        person = user["id"]
    else:
        person = user["id"] if scope["assignee"] == "me" else int(scope["assignee"]) if scope["assignee"].isdigit() else None
    tasks = {item["id"]: item for item in scope["visible"]}
    clients = {row["id"]: row["company_name"] for row in db.execute(
        "SELECT p.id, c.name AS company_name FROM projects p LEFT JOIN companies c ON c.id = p.company_id")}
    ever = {row["task_id"]: row["seconds"] for row in db.execute(
        "SELECT task_id, SUM(seconds) AS seconds FROM work_task_time WHERE ended_at IS NOT NULL GROUP BY task_id")}

    people, projects, companies, by_task, days, total = {}, {}, {}, {}, [], 0
    for row in db.execute(
        """SELECT t.id, t.task_id, t.user_id, t.started_at, t.seconds, u.name AS user_name FROM work_task_time t
           LEFT JOIN users u ON u.id = t.user_id
           WHERE t.ended_at IS NOT NULL AND substr(t.started_at, 1, 10) BETWEEN ? AND ?
           ORDER BY t.started_at DESC, t.id DESC""", (first.isoformat(), last.isoformat())):
        task = tasks.get(row["task_id"])
        if not task or (person and row["user_id"] != person) or (scope["project"] and task["project_id"] != scope["project"]):
            continue
        seconds, who = row["seconds"], row["user_name"] or "Former user"
        project = task["project"]["name"] if task["project"] else "No project"
        client = (clients.get(task["project_id"]) or "Internal") if task["project"] else "No project"
        total += seconds
        people[who] = people.get(who, 0) + seconds
        projects[project] = projects.get(project, 0) + seconds
        companies[client] = companies.get(client, 0) + seconds
        by_task[task["id"]] = by_task.get(task["id"], 0) + seconds
        started = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
        label = f"{started.strftime('%A')}, {started.day} {started.strftime('%B')}"
        if not days or days[-1]["label"] != label:
            days.append({"label": label, "seconds": 0, "entries": []})
        days[-1]["seconds"] += seconds
        days[-1]["entries"].append({"who": who, "initials": _initials(row["user_name"]), "tone": (row["user_id"] or 0) % 5,
                                    "task": task, "project": project, "label": _duration_label(seconds)})

    def ranked(sums):
        top = max(sums.values(), default=0)
        return [{"name": name, "label": _duration_label(seconds), "share": round(100 * seconds / top) if top else 0}
                for name, seconds in sorted(sums.items(), key=lambda pair: -pair[1])]

    task_rows = []
    for task_id, seconds in sorted(by_task.items(), key=lambda pair: -pair[1]):
        task, estimate = tasks[task_id], (tasks[task_id]["estimate_minutes"] or 0) * 60
        task_rows.append({"task": task, "label": _duration_label(seconds), "estimate": task["estimate"],
                          "over": bool(estimate) and ever.get(task_id, 0) > estimate,
                          "ever": _duration_label(ever.get(task_id, 0))})
    for day in days:
        day["total"] = _duration_label(day["seconds"])
    return _tasks_page(
        "tasks_time.html", scope, view="time", month=None, total=sum(len(day["entries"]) for day in days),
        periods=TIME_PERIODS, period=period, total_label=_duration_label(total) if total else "No time",
        range_label=f"{first.day} {first.strftime('%b')} to {last.day} {last.strftime('%b %Y')}",
        people=ranked(people), by_project=ranked(projects), by_client=ranked(companies), task_rows=task_rows, days=days,
        own_only=user["role"] != "manager",
    )


@app.post("/tasks/<int:task_id>/time")
@login_required
def task_time_add(task_id):
    """Log time by hand, for work done without the timer running."""
    _check_task_token()
    db, user = get_db(), current_user()
    _get_work_task(db, task_id)
    today = datetime.now(APP_TIMEZONE).date()
    try:
        minutes = _parse_duration(request.form.get("duration"))
        day = date.fromisoformat(request.form["day"]) if request.form.get("day") else today
    except ValueError:
        minutes, day = None, today
    if not minutes or day > today:
        flash("Enter how long you worked, like 1h 30m, on a day that is not in the future.", "error")
    else:
        started = datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
        db.execute("INSERT INTO work_task_time (task_id, user_id, started_at, ended_at, seconds) VALUES (?,?,?,?,?)",
                   (task_id, user["id"], started.strftime("%Y-%m-%d %H:%M:%S"),
                    (started + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"), minutes * 60))
        _log_task_event(db, task_id, f"logged {_duration_label(minutes * 60)} for {day.day} {day.strftime('%b')}")
        db.commit()
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/timer/start")
@login_required
def task_timer_start(task_id):
    _check_task_token()
    db, user = get_db(), current_user()
    _get_work_task(db, task_id)
    _stop_running_timer(db, user)
    db.execute("INSERT INTO work_task_time (task_id, user_id, started_at) VALUES (?,?,?)", (task_id, user["id"], now_str()))
    db.commit()
    return redirect(_tasks_back(url_for("task_detail", task_id=task_id)))


@app.post("/tasks/timer/stop")
@login_required
def task_timer_stop():
    _check_task_token()
    db = get_db()
    timer = _stop_running_timer(db, current_user())
    db.commit()
    return redirect(_tasks_back(url_for("task_detail", task_id=timer["task_id"]) if timer else url_for("tasks_list")))


@app.post("/tasks/time/<int:entry_id>/delete")
@login_required
def task_time_delete(entry_id):
    _check_task_token()
    db, user = get_db(), current_user()
    entry = db.execute("SELECT * FROM work_task_time WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        abort(404)
    _get_work_task(db, entry["task_id"])
    if entry["ended_at"] is None or (user["role"] != "manager" and entry["user_id"] != user["id"]):
        abort(403)
    db.execute("DELETE FROM work_task_time WHERE id=?", (entry_id,))
    _log_task_event(db, entry["task_id"], f"removed a time entry of {_duration_label(entry['seconds'])}")
    db.commit()
    return redirect(url_for("task_detail", task_id=entry["task_id"]))


@app.post("/tasks/<int:task_id>/status")
@login_required
def task_set_status(task_id):
    _check_task_token()
    db = get_db()
    task = _get_work_task(db, task_id)
    status = request.form.get("status")
    if status not in WORK_STATUSES:
        abort(400)
    _set_task_status(db, task, status)
    db.commit()
    return redirect(_tasks_back(url_for("tasks_list")))


@app.post("/tasks/<int:task_id>/comment")
@login_required
def task_comment(task_id):
    _check_task_token()
    db = get_db()
    task = _get_work_task(db, task_id)
    body = request.form.get("body", "").strip()[:5000]
    if body:
        _log_task_event(db, task_id, body, kind="comment")
        _notify_task_comment(db, task, body)
        db.commit()
        _live_touch()
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/delete")
@login_required
def task_delete(task_id):
    _check_task_token()
    db, user = get_db(), current_user()
    task = _get_work_task(db, task_id)
    if user["role"] != "manager" and task["created_by"] != user["id"]:
        flash("Only a manager or the person who created a task can delete it.", "error")
        return redirect(url_for("task_detail", task_id=task_id))
    stored = [r["stored_name"] for r in db.execute(
        """SELECT stored_name FROM work_task_files
           WHERE task_id=? OR task_id IN (SELECT id FROM work_tasks WHERE parent_id=?)""", (task_id, task_id))]
    db.execute("DELETE FROM work_tasks WHERE id=?", (task_id,))
    db.commit()
    _remove_task_files(stored)
    log_activity("Deleted task", "task", task_id, task["title"])
    flash("Task deleted.", "success")
    return redirect(url_for("task_detail", task_id=task["parent_id"]) if task["parent_id"] else url_for("tasks_list"))


# ---------- Live notifications (unread counts, toasts, realtime chat) ----------
# Each open page keeps one WebSocket to the server, and the server pushes a small update
# whenever the signed-in person's unread counts change. A change made in this process wakes
# its sockets at once; every socket also re-checks the database each second, so changes made
# by another server process still arrive without a message broker. Where a socket cannot be
# used (library missing, non-threaded server, proxy not passing upgrades), the page falls
# back to asking /live/state every few seconds.

LIVE_CHECK_SECONDS = 1.0       # how often a socket re-checks the database for other processes' changes
LIVE_WAKE_SECONDS = 0.25       # how quickly a socket notices a change made in this process
LIVE_MAX_SOCKETS = max(1, int(os.environ.get("REALTIME_MAX_SOCKETS", "200")))
_live_lock = threading.Lock()
_live_sockets = 0
_live_version = 0


def _live_touch():
    """Note that something changed, so sockets in this process push without waiting."""
    global _live_version
    _live_version += 1


def _init_notifications(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            task_id INTEGER REFERENCES work_tasks(id) ON DELETE CASCADE,
            actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            body TEXT,
            created_at TEXT NOT NULL,
            read_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(user_id, read_at, id)")
    # lead_id is set on notifications about a lead (a follow-up handed to a manager).
    _add_cols(db, "notifications", [("lead_id", "INTEGER REFERENCES leads(id) ON DELETE CASCADE"),
                                    ("due_on", "TEXT")])   # the due date a due-date reminder was about


def _notify_task_assigned(db, user_id, task_id, task_title):
    """Tell someone a task was assigned to them, unless they assigned it to themselves."""
    actor = current_user()
    if not actor or actor["id"] == user_id:
        return
    db.execute(
        "INSERT INTO notifications (user_id, kind, task_id, actor_id, title, body, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, "task_assigned", task_id, actor["id"], f"{actor['name']} assigned you a task", task_title, now_str()),
    )


def _notify_task_comment(db, task, comment):
    """Tell the people on a task that someone else commented on it."""
    actor = current_user()
    people = {row["user_id"] for row in db.execute("SELECT user_id FROM work_task_assignees WHERE task_id=?", (task["id"],))}
    people.add(task["created_by"])
    for user_id in people - {actor["id"], None}:
        if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            continue
        db.execute(
            "INSERT INTO notifications (user_id, kind, task_id, actor_id, title, body, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, "task_comment", task["id"], actor["id"], f"{actor['name']} commented on {task['title'][:60]}",
             comment[:140], now_str()))


def _notify_due_tasks(db, user_id):
    """Remind someone about their open tasks that are due tomorrow, due today, or just went overdue.

    Each reminder is made once per task and due date. Tasks more than three days overdue stay
    quiet, so the first run does not bury anyone. Returns how many were made.
    """
    today = datetime.now(APP_TIMEZONE).date()
    made = 0
    for row in db.execute(
        """SELECT w.id, w.title, w.due_date FROM work_tasks w JOIN work_task_assignees a ON a.task_id = w.id
           WHERE a.user_id=? AND w.status != 'Complete' AND w.due_date BETWEEN ? AND ?""",
        (user_id, (today - timedelta(days=3)).isoformat(), (today + timedelta(days=1)).isoformat())).fetchall():
        try:
            due = date.fromisoformat(row["due_date"])
        except ValueError:
            continue
        if due > today:
            kind, title = "task_due_soon", "Due tomorrow"
        elif due == today:
            kind, title = "task_due", "Due today"
        else:
            kind, title = "task_overdue", f"Overdue since {due.day} {due.strftime('%b')}"
        if db.execute("SELECT 1 FROM notifications WHERE user_id=? AND kind=? AND task_id=? AND due_on=?",
                      (user_id, kind, row["id"], row["due_date"])).fetchone():
            continue
        db.execute("INSERT INTO notifications (user_id, kind, task_id, title, body, created_at, due_on) VALUES (?,?,?,?,?,?,?)",
                   (user_id, kind, row["id"], title, row["title"], now_str(), row["due_date"]))
        made += 1
    return made


@app.before_request
def remind_about_due_tasks():
    """Once a day per signed-in browser, on an ordinary page load, check for due tasks."""
    if request.method != "GET" or request.endpoint in (None, "static", "favicon", "live_state", "live_socket"):
        return
    today = today_str()
    if "user_id" not in session or session.get("due_checked") == today:
        return
    user = current_user()
    if not user:
        return
    session["due_checked"] = today
    db = get_db()
    if _notify_due_tasks(db, user["id"]):
        db.commit()
        _live_touch()


def _live_counts(db, user_id):
    chat = db.execute("SELECT COUNT(*) FROM chat_messages WHERE recipient_id=? AND is_read=0", (user_id,)).fetchone()[0]
    tasks, leads = db.execute(
        """SELECT COALESCE(SUM(lead_id IS NULL), 0), COALESCE(SUM(lead_id IS NOT NULL), 0)
           FROM notifications WHERE user_id=? AND read_at IS NULL""", (user_id,)).fetchone()
    return {"chat": chat, "tasks": tasks, "leads": leads, "all": chat + tasks}


def _live_state(db, user_id, chat_after, note_after):
    """Unread counts plus anything new since the given cursors. A cursor below zero means
    "just tell me where things stand", so a freshly opened page never replays old items."""
    state = _live_counts(db, user_id)
    state["chats"], state["notes"] = [], []
    if chat_after >= 0:
        state["chats"] = [
            {"id": r[0], "from": r[1], "name": r[2], "body": r[3]}
            for r in db.execute(
                """SELECT m.id, m.sender_id, u.name, substr(m.body, 1, 140) FROM chat_messages m
                   JOIN users u ON u.id = m.sender_id
                   WHERE m.recipient_id=? AND m.id>? AND m.is_read=0 ORDER BY m.id LIMIT 20""", (user_id, chat_after))
        ]
    if note_after >= 0:
        state["notes"] = [
            {"id": r[0], "task": r[1], "title": r[2], "body": r[3], "lead": r[4]}
            for r in db.execute(
                """SELECT id, task_id, title, body, lead_id FROM notifications
                   WHERE user_id=? AND id>? AND read_at IS NULL ORDER BY id LIMIT 20""", (user_id, note_after))
        ]
    state["chat_cursor"] = db.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE recipient_id=?", (user_id,)).fetchone()[0]
    state["note_cursor"] = db.execute("SELECT COALESCE(MAX(id), 0) FROM notifications WHERE user_id=?", (user_id,)).fetchone()[0]
    return state


def _live_cursors():
    """The newest chat message and notification the page has already seen."""
    return (request.args.get("chat_after", default=-1, type=int),
            request.args.get("note_after", default=-1, type=int))


@app.get("/live/state")
@login_required
def live_state():
    """One-off check, used when a WebSocket is not available."""
    response = jsonify(_live_state(get_db(), current_user()["id"], *_live_cursors()))
    response.headers["Cache-Control"] = "no-store"
    return response


def _live_origin_ok():
    """Only accept sockets opened by this site's own pages."""
    origin = request.headers.get("Origin")
    if not origin:
        return True  # non-browser clients send none; they still need a signed-in session
    # Behind a proxy the app may see an internal host name, so also accept the public
    # address the proxy forwards and the one configured for Google sign-in.
    allowed = {request.host, request.headers.get("X-Forwarded-Host", ""),
               urlsplit(os.environ.get("GOOGLE_REDIRECT_URI", "")).netloc}
    return urlsplit(origin).netloc in allowed - {""}


def live_socket(ws):
    global _live_sockets
    user = current_user()
    # The socket outlives a normal request, so release the per-request database connection now.
    request_db = g.pop("db", None)
    if request_db is not None:
        request_db.close()
    if not user or not _live_origin_ok():
        ws.close(1008)
        return
    # A server that handles one request at a time per process would be frozen by an open
    # socket. Closing with 1013 ("try again later") tells the page to ask periodically instead.
    if os.environ.get("REALTIME", "auto").strip().lower() == "off" or not request.environ.get("wsgi.multithread"):
        ws.close(1013)
        return
    with _live_lock:
        if _live_sockets >= LIVE_MAX_SOCKETS:
            ws.close(1013)
            return
        _live_sockets += 1

    user_id = user["id"]
    chat_after, note_after = _live_cursors()
    connection = sqlite3.connect(DB_PATH, timeout=5)
    try:
        sent, seen_version, checked = None, -1, 0.0
        while True:
            now = time_module.monotonic()
            if _live_version != seen_version or now - checked >= LIVE_CHECK_SECONDS:
                seen_version, checked = _live_version, now
                state = _live_state(connection, user_id, chat_after, note_after)
                fingerprint = (state["chat"], state["tasks"], state["leads"], state["chat_cursor"], state["note_cursor"])
                if fingerprint != sent:
                    sent = fingerprint
                    chat_after, note_after = state["chat_cursor"], state["note_cursor"]
                    ws.send(json.dumps(state))
            # This is the wait between checks. It raises ConnectionClosed as soon as the
            # tab closes, so finished sockets are cleaned up straight away.
            ws.receive(timeout=LIVE_WAKE_SECONDS)
    except ConnectionClosed:
        pass
    finally:
        connection.close()
        with _live_lock:
            _live_sockets -= 1


if sock:
    sock.route("/live/socket")(live_socket)


@app.get("/favicon.ico")
def favicon():
    return send_file(os.path.join(app.static_folder, "favicon.ico"), mimetype="image/x-icon", max_age=86400)


@app.context_processor
def inject_globals():
    user = current_user()
    counts = _live_counts(get_db(), user["id"]) if user else {"chat": 0, "tasks": 0, "all": 0}
    return {"today": today_str(), "current_user": user, "live_counts": counts, "live_socket_enabled": bool(sock)}


@app.template_filter("slug")
def slug_filter(value):
    return (value or "").replace(" ", "-")


if __name__ == "__main__":
    init_db()
    _refile_task_files()
    app.run(host="0.0.0.0", port=5000, debug=True)
else:
    init_db()
    _refile_task_files()
