from datetime import datetime, timedelta
from functools import wraps
import os
import re
from zoneinfo import ZoneInfo

import certifi
from dotenv import load_dotenv



load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from bson.errors import InvalidId
from bson.objectid import ObjectId
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from pymongo.collation import Collation
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing. Add your MongoDB Atlas connection string to .env.")

if "<" in MONGO_URI or ">" in MONGO_URI:
    raise RuntimeError(
        "MONGO_URI still contains placeholder brackets. Replace them with your real Atlas username/password."
    )

try:
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000,
        tls=True,
        tlsCAFile=certifi.where(),
    )
    client.admin.command("ping")
except ServerSelectionTimeoutError as exc:
    raise RuntimeError(
        "Could not connect to MongoDB. Check MONGO_URI in your .env file, or start MongoDB/Atlas first."
    ) from exc

db = client["taskflow"]
users_col = db["users"]
tasks_col = db["tasks"]
notifications_col = db["notifications"]
app_meta_col = db["app_meta"]

LOGIN_REWARD_POINTS = 100
DEFAULT_ADMIN_LOGIN_ID = os.environ.get("DEFAULT_ADMIN_LOGIN_ID", "").strip()
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "").strip()
DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME", "Akay Admin").strip()
RESET_ADMIN_PASSWORD_ON_BOOT = os.environ.get("RESET_ADMIN_PASSWORD_ON_BOOT", "0").strip() == "1"
DEBUG_ADMIN_TOKEN = os.environ.get("DEBUG_ADMIN_TOKEN", "").strip()
BUSINESS_TIMEZONE = os.environ.get("BUSINESS_TIMEZONE", "Asia/Calcutta").strip() or "Asia/Calcutta"
ACCESS_START_HOUR = int(os.environ.get("ACCESS_START_HOUR", "9"))
ACCESS_END_HOUR = int(os.environ.get("ACCESS_END_HOUR", "19"))
ENFORCE_ACCESS_WINDOW = os.environ.get("ENFORCE_ACCESS_WINDOW", "1").strip() == "1"
READ_NOTIFICATION_RETENTION_DAYS = int(os.environ.get("READ_NOTIFICATION_RETENTION_DAYS", "21"))
COMPLETED_TASK_RETENTION_DAYS = int(os.environ.get("COMPLETED_TASK_RETENTION_DAYS", "90"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "12"))
TASK_LEVELS = {
    "low": {"label": "Low", "points": 25},
    "medium": {"label": "Medium", "points": 50},
    "high": {"label": "High", "points": 100},
    "critical": {"label": "Critical", "points": 150},
    "very_critical": {"label": "Critical", "points": 200},
}
NOTIFICATION_TYPES = {
    "task": "New Task",
    "mention": "Mention",
    "assignment": "Taken",
    "completion": "Completed",
    "admin": "Admin",
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")

users_col.create_index("username", unique=True)
users_col.create_index("login_id", unique=True, sparse=True)
users_col.create_index("is_admin")
users_col.create_index("is_approved")
users_col.create_index("is_disabled")
tasks_col.create_index("giver_id")
tasks_col.create_index("taker_id")
tasks_col.create_index("status")
notifications_col.create_index([("user_id", 1), ("read", 1), ("created_at", -1)])
app_meta_col.create_index("updated_at")
print("MongoDB connected!")


def safe_object_id(raw_value):
    try:
        return ObjectId(raw_value)
    except (InvalidId, TypeError):
        return None


def now_local():
    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE))


def default_user_fields(user):
    username = user.get("username", "")
    created_at = user.get("created_at") or datetime.utcnow()
    return {
        "login_id": user.get("login_id") or username,
        "points": int(user.get("points", 0) or 0),
        "profile_description": user.get("profile_description", ""),
        "profile_picture_url": user.get("profile_picture_url", ""),
        "is_active": bool(user.get("is_active", True)),
        "is_admin": bool(user.get("is_admin", False)),
        "is_approved": bool(user.get("is_approved", True)),
        "is_disabled": bool(user.get("is_disabled", False)),
        "password_hash": user.get("password_hash", ""),
        "password_changed_by_user": bool(user.get("password_changed_by_user", False)),
        "first_login_reward_granted": bool(user.get("first_login_reward_granted", False)),
        "created_at": created_at,
    }


def merged_user_defaults(user):
    if not user:
        return None
    merged = dict(user)
    for key, value in default_user_fields(user).items():
        if key not in merged or merged.get(key) is None:
            merged[key] = value
    return merged


def ensure_user_defaults(user):
    if not user:
        return None
    defaults = merged_user_defaults(user)
    missing_fields = {
        key: value for key, value in defaults.items() if key not in user or user.get(key) is None
    }
    if missing_fields:
        users_col.update_one({"_id": user["_id"]}, {"$set": missing_fields})
        user.update(missing_fields)
    return user


def find_user_by_identity(identity):
    cleaned = (identity or "").strip()
    if not cleaned:
        return None

    user = ensure_user_defaults(users_col.find_one({"login_id": cleaned}))
    if user:
        return user

    user = ensure_user_defaults(users_col.find_one({"username": cleaned}))
    if user:
        return user

    # Fall back to case-insensitive exact matching for deployments that already
    # contain differently cased identifiers.
    identity_collation = Collation(locale="en", strength=2)
    user = ensure_user_defaults(
        users_col.find_one({"login_id": cleaned}, collation=identity_collation)
    )
    if user:
        return user

    return ensure_user_defaults(
        users_col.find_one({"username": cleaned}, collation=identity_collation)
    )


def access_window_active(current_time=None):
    current_time = current_time or now_local()
    return ACCESS_START_HOUR <= current_time.hour < ACCESS_END_HOUR


def get_cleanup_state():
    state = app_meta_col.find_one({"_id": "cleanup_state"}) or {}
    return {
        "last_cleanup_at": state.get("last_cleanup_at"),
        "last_reason": state.get("last_reason"),
        "notifications_deleted": int(state.get("notifications_deleted", 0) or 0),
        "tasks_deleted": int(state.get("tasks_deleted", 0) or 0),
        "updated_at": state.get("updated_at"),
    }


def run_data_cleanup(reason, force=False):
    state = get_cleanup_state()
    last_cleanup_at = state.get("last_cleanup_at")
    now_utc = datetime.utcnow()
    if (
        not force
        and last_cleanup_at
        and now_utc - last_cleanup_at < timedelta(hours=CLEANUP_INTERVAL_HOURS)
    ):
        return {
            "ran": False,
            "last_cleanup_at": last_cleanup_at,
            "notifications_deleted": 0,
            "tasks_deleted": 0,
        }

    notifications_cutoff = now_utc - timedelta(days=READ_NOTIFICATION_RETENTION_DAYS)
    tasks_cutoff = now_utc - timedelta(days=COMPLETED_TASK_RETENTION_DAYS)

    notifications_result = notifications_col.delete_many(
        {"read": True, "created_at": {"$lt": notifications_cutoff}}
    )
    tasks_result = tasks_col.delete_many(
        {"status": "completed", "updated_at": {"$lt": tasks_cutoff}}
    )

    summary = {
        "ran": True,
        "last_cleanup_at": now_utc,
        "notifications_deleted": notifications_result.deleted_count,
        "tasks_deleted": tasks_result.deleted_count,
    }
    app_meta_col.update_one(
        {"_id": "cleanup_state"},
        {
            "$set": {
                "last_cleanup_at": now_utc,
                "last_reason": reason,
                "notifications_deleted": notifications_result.deleted_count,
                "tasks_deleted": tasks_result.deleted_count,
                "updated_at": now_utc,
            }
        },
        upsert=True,
    )
    app.logger.info(
        "Data cleanup ran reason=%s notifications_deleted=%s tasks_deleted=%s",
        reason,
        notifications_result.deleted_count,
        tasks_result.deleted_count,
    )
    return summary


def bootstrap_existing_users():
    projected_users = list(
        users_col.find(
            {},
            {
                "username": 1,
                "login_id": 1,
                "points": 1,
                "profile_description": 1,
                "profile_picture_url": 1,
                "is_active": 1,
                "is_admin": 1,
                "is_approved": 1,
                "is_disabled": 1,
                "password_hash": 1,
                "password_changed_by_user": 1,
                "first_login_reward_granted": 1,
                "created_at": 1,
            },
        ).sort("created_at", 1)
    )

    for user in projected_users:
        ensure_user_defaults(user)

    if not DEFAULT_ADMIN_LOGIN_ID or not DEFAULT_ADMIN_PASSWORD:
        raise RuntimeError(
            "DEFAULT_ADMIN_LOGIN_ID and DEFAULT_ADMIN_PASSWORD must be set in the environment."
        )

    app.logger.info(
        "Bootstrapping admin account for login_id=%s reset_on_boot=%s",
        DEFAULT_ADMIN_LOGIN_ID,
        RESET_ADMIN_PASSWORD_ON_BOOT,
    )

    users_col.update_many(
        {"login_id": {"$ne": DEFAULT_ADMIN_LOGIN_ID}},
        {"$set": {"is_admin": False}},
    )

    admin_user = ensure_user_defaults(users_col.find_one({"login_id": DEFAULT_ADMIN_LOGIN_ID}))
    if not admin_user:
        admin_user = ensure_user_defaults(users_col.find_one({"username": DEFAULT_ADMIN_USERNAME}))
    if admin_user:
        update_fields = {
            "username": DEFAULT_ADMIN_USERNAME,
            "login_id": DEFAULT_ADMIN_LOGIN_ID,
            "is_admin": True,
            "is_approved": True,
            "is_disabled": False,
        }
        if RESET_ADMIN_PASSWORD_ON_BOOT or not admin_user.get("password_hash"):
            update_fields["password_hash"] = generate_password_hash(DEFAULT_ADMIN_PASSWORD)
            update_fields["password_changed_by_user"] = False
        users_col.update_one(
            {"_id": admin_user["_id"]},
            {"$set": update_fields},
        )
        app.logger.info(
            "Admin account synced for login_id=%s password_reset=%s",
            DEFAULT_ADMIN_LOGIN_ID,
            "password_hash" in update_fields,
        )
    else:
        users_col.insert_one(
            {
                "username": DEFAULT_ADMIN_USERNAME,
                "login_id": DEFAULT_ADMIN_LOGIN_ID,
                "password_hash": generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                "points": 0,
                "profile_description": "System administrator and access controller.",
                "profile_picture_url": "",
                "is_active": True,
                "is_admin": True,
                "is_approved": True,
                "is_disabled": False,
                "password_changed_by_user": False,
                "first_login_reward_granted": False,
                "created_at": datetime.utcnow(),
            }
        )
        app.logger.info("Admin account created for login_id=%s", DEFAULT_ADMIN_LOGIN_ID)


bootstrap_existing_users()


def current_user():
    raw_user_id = session.get("user_id")
    object_id = safe_object_id(raw_user_id)
    if not object_id:
        return None
    user = users_col.find_one({"_id": object_id})
    return ensure_user_defaults(user)


def active_users(exclude_user_id=None):
    query = {"is_active": True, "is_approved": True, "is_disabled": False}
    if exclude_user_id:
        query["_id"] = {"$ne": exclude_user_id}
    users = list(
        users_col.find(
            query,
            {
                "username": 1,
                "login_id": 1,
                "profile_picture_url": 1,
                "is_active": 1,
                "is_admin": 1,
                "is_approved": 1,
            },
        ).sort("username", 1)
    )
    return [merged_user_defaults(user) for user in users]


@app.before_request
def enforce_access_window_and_cleanup():
    if request.endpoint in {
        "static",
        "debug_admin",
        "debug_admin_reset_password",
    }:
        return None

    run_data_cleanup("scheduled_request")

    if not ENFORCE_ACCESS_WINDOW:
        return None

    if access_window_active():
        return None

    return (
        render_template(
            "closed.html",
            start_hour=ACCESS_START_HOUR,
            end_hour=ACCESS_END_HOUR,
            business_timezone=BUSINESS_TIMEZONE,
        ),
        200,
    )


@app.route("/debug-admin", methods=["GET"])
def debug_admin():
    provided_token = request.args.get("token", "").strip()
    if not DEBUG_ADMIN_TOKEN or provided_token != DEBUG_ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    admin_by_login = ensure_user_defaults(users_col.find_one({"login_id": DEFAULT_ADMIN_LOGIN_ID}))
    admin_by_username = ensure_user_defaults(users_col.find_one({"username": DEFAULT_ADMIN_USERNAME}))
    admin_user = admin_by_login or admin_by_username

    if admin_user:
        response = {
            "ok": True,
            "admin_exists": True,
            "matched_by": "login_id" if admin_by_login else "username",
            "env_default_admin_login_id": DEFAULT_ADMIN_LOGIN_ID,
            "env_default_admin_username": DEFAULT_ADMIN_USERNAME,
            "stored_user_id": str(admin_user["_id"]),
            "stored_login_id": admin_user.get("login_id"),
            "stored_username": admin_user.get("username"),
            "is_admin": admin_user.get("is_admin", False),
            "is_approved": admin_user.get("is_approved", False),
            "is_disabled": admin_user.get("is_disabled", False),
            "has_password_hash": bool(admin_user.get("password_hash")),
            "password_changed_by_user": admin_user.get("password_changed_by_user", False),
            "env_password_present": bool(DEFAULT_ADMIN_PASSWORD),
            "env_password_length": len(DEFAULT_ADMIN_PASSWORD),
            "env_password_matches_hash": (
                bool(admin_user.get("password_hash"))
                and bool(DEFAULT_ADMIN_PASSWORD)
                and check_password_hash(admin_user["password_hash"], DEFAULT_ADMIN_PASSWORD)
            ),
            "reset_admin_password_on_boot": RESET_ADMIN_PASSWORD_ON_BOOT,
        }
    else:
        response = {
            "ok": True,
            "admin_exists": False,
            "env_default_admin_login_id": DEFAULT_ADMIN_LOGIN_ID,
            "env_default_admin_username": DEFAULT_ADMIN_USERNAME,
            "reset_admin_password_on_boot": RESET_ADMIN_PASSWORD_ON_BOOT,
        }

    return jsonify(response)


@app.route("/debug-admin/reset-password", methods=["POST", "GET"])
def debug_admin_reset_password():
    provided_token = request.values.get("token", "").strip()
    if not DEBUG_ADMIN_TOKEN or provided_token != DEBUG_ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if not DEFAULT_ADMIN_LOGIN_ID or not DEFAULT_ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "missing_admin_env"}), 400

    admin_user = find_user_by_identity(DEFAULT_ADMIN_LOGIN_ID)
    if not admin_user:
        return jsonify({"ok": False, "error": "admin_not_found"}), 404

    users_col.update_one(
        {"_id": admin_user["_id"]},
        {
            "$set": {
                "username": DEFAULT_ADMIN_USERNAME,
                "login_id": DEFAULT_ADMIN_LOGIN_ID,
                "is_admin": True,
                "is_approved": True,
                "is_disabled": False,
                "password_hash": generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                "password_changed_by_user": False,
            }
        },
    )
    app.logger.warning(
        "Admin password force-reset through debug endpoint for login_id=%s",
        DEFAULT_ADMIN_LOGIN_ID,
    )
    return jsonify(
        {
            "ok": True,
            "message": "admin password reset from environment",
            "login_id": DEFAULT_ADMIN_LOGIN_ID,
        }
    )


def recent_notifications_for(user_id, limit=8):
    return list(
        notifications_col.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    )


def unread_notification_count(user_id):
    return notifications_col.count_documents({"user_id": user_id, "read": False})


def create_notification(user_id, message, link, kind="task"):
    notifications_col.insert_one(
        {
            "user_id": user_id,
            "message": message,
            "link": link,
            "kind": kind,
            "read": False,
            "created_at": datetime.utcnow(),
        }
    )


def notify_admins(message, link):
    for admin in users_col.find(
        {"is_admin": True, "is_approved": True, "is_disabled": False},
        {"_id": 1},
    ):
        create_notification(str(admin["_id"]), message, link, kind="admin")


def get_task_level_meta(level_key):
    return TASK_LEVELS.get(level_key, TASK_LEVELS["medium"])


def annotate_tasks(tasks):
    related_ids = set()
    for task in tasks:
        if task.get("giver_id"):
            related_ids.add(task["giver_id"])
        if task.get("taker_id"):
            related_ids.add(task["taker_id"])
        for mentioned_id in task.get("mentioned_user_ids", []):
            related_ids.add(mentioned_id)

    users_by_id = {
        str(user["_id"]): merged_user_defaults(user)
        for user in users_col.find(
            {"_id": {"$in": [ObjectId(uid) for uid in related_ids if safe_object_id(uid)]}},
            {
                "username": 1,
                "login_id": 1,
                "profile_picture_url": 1,
                "is_active": 1,
                "is_admin": 1,
                "is_approved": 1,
            },
        )
    }

    for task in tasks:
        task["level_meta"] = get_task_level_meta(task.get("level", "medium"))
        giver = users_by_id.get(task.get("giver_id"))
        taker = users_by_id.get(task.get("taker_id"))
        task["giver_name"] = giver["username"] if giver else "Unknown"
        task["taker_name"] = taker["username"] if taker else None
        task["mentioned_users"] = [
            users_by_id[user_id]["username"]
            for user_id in task.get("mentioned_user_ids", [])
            if user_id in users_by_id
        ]
    return tasks


def validate_identity(value, field_name):
    cleaned = value.strip()
    if not USERNAME_RE.match(cleaned):
        return None, (
            f"{field_name} must be 3-30 characters and use only letters, numbers, dots, "
            "underscores, or hyphens."
        )
    return cleaned, None


def login_reward_message(username):
    return (
        f"Welcome back, {username}! You received {LOGIN_REWARD_POINTS} starter credit points."
    )


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        if user.get("is_disabled"):
            session.clear()
            flash("Your account has been disabled by admin.", "error")
            return redirect(url_for("login"))
        if not user.get("is_approved"):
            session.clear()
            flash("Your account is waiting for admin approval.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        if user.get("is_disabled"):
            session.clear()
            flash("Your account has been disabled by admin.", "error")
            return redirect(url_for("login"))
        if not user.get("is_admin"):
            flash("Admin access only.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)

    return decorated


@app.context_processor
def inject_nav_context():
    user = current_user()
    cleanup_state = get_cleanup_state()
    if not user:
        return {
            "nav_user": None,
            "nav_notifications": [],
            "notification_count": 0,
            "notification_types": NOTIFICATION_TYPES,
            "task_levels": TASK_LEVELS,
            "pending_approval_count": 0,
            "cleanup_state": cleanup_state,
            "business_timezone": BUSINESS_TIMEZONE,
            "access_start_hour": ACCESS_START_HOUR,
            "access_end_hour": ACCESS_END_HOUR,
            "enforce_access_window": ENFORCE_ACCESS_WINDOW,
        }

    uid = str(user["_id"])
    return {
        "nav_user": user,
        "nav_notifications": recent_notifications_for(uid),
        "notification_count": unread_notification_count(uid),
        "notification_types": NOTIFICATION_TYPES,
        "task_levels": TASK_LEVELS,
        "pending_approval_count": 0,
        "cleanup_state": cleanup_state,
        "business_timezone": BUSINESS_TIMEZONE,
        "access_start_hour": ACCESS_START_HOUR,
        "access_end_hour": ACCESS_END_HOUR,
        "enforce_access_window": ENFORCE_ACCESS_WINDOW,
    }


@app.route("/")
def index():
    user = current_user()
    stats = {}
    if user and user.get("is_approved"):
        uid = str(user["_id"])
        stats["posted"] = tasks_col.count_documents({"giver_id": uid})
        stats["taken"] = tasks_col.count_documents({"taker_id": uid, "status": "taken"})
        stats["completed"] = tasks_col.count_documents(
            {"taker_id": uid, "status": "completed"}
        )
        stats["available"] = tasks_col.count_documents({"status": "open"})
    return render_template("index.html", user=user, stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    flash("User creation is admin-only. Sign in as admin to create accounts.", "warning")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_input = request.form.get("login_id", "").strip()
        password = request.form.get("password", "")
        user = find_user_by_identity(login_input)

        if not user:
            app.logger.warning("Login failed for input=%s reason=user_not_found", login_input)
            flash("Invalid user ID or password.", "error")
        elif user.get("is_disabled"):
            app.logger.warning(
                "Login blocked for input=%s user_id=%s reason=disabled",
                login_input,
                user.get("_id"),
            )
            flash("This account is disabled. Contact admin.", "error")
        elif not user.get("is_approved"):
            app.logger.warning(
                "Login blocked for input=%s user_id=%s reason=not_approved",
                login_input,
                user.get("_id"),
            )
            flash("Your account is still waiting for admin approval.", "warning")
        elif not user.get("password_hash"):
            app.logger.warning(
                "Login blocked for input=%s user_id=%s reason=no_password_hash",
                login_input,
                user.get("_id"),
            )
            flash("Admin has not issued your login password yet.", "warning")
        elif not check_password_hash(user["password_hash"], password):
            app.logger.warning(
                "Login failed for input=%s user_id=%s reason=password_mismatch",
                login_input,
                user.get("_id"),
            )
            flash("Invalid user ID or password.", "error")
        else:
            app.logger.info(
                "Login succeeded for input=%s user_id=%s is_admin=%s",
                login_input,
                user.get("_id"),
                user.get("is_admin", False),
            )
            session["user_id"] = str(user["_id"])
            session["username"] = user["username"]
            session["login_id"] = user["login_id"]
            session["is_admin"] = user.get("is_admin", False)

            if not user.get("first_login_reward_granted"):
                users_col.update_one(
                    {"_id": user["_id"]},
                    {
                        "$inc": {"points": LOGIN_REWARD_POINTS},
                        "$set": {
                            "first_login_reward_granted": True,
                            "last_login_at": datetime.utcnow(),
                        },
                    },
                )
                flash(login_reward_message(user["username"]), "success")
            else:
                users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"last_login_at": datetime.utcnow()}},
                )
                flash(f"Welcome back, {user['username']}!", "success")

            return redirect(url_for("tasks"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    admin_user = current_user()
    cleanup_state = get_cleanup_state()
    managed_users = list(
        users_col.find(
            {"is_admin": {"$ne": True}},
            {
                "username": 1,
                "login_id": 1,
                "is_admin": 1,
                "is_active": 1,
                "is_disabled": 1,
                "password_changed_by_user": 1,
                "created_at": 1,
            },
        ).sort("created_at", -1)
    )
    managed_users = [merged_user_defaults(user) for user in managed_users]
    return render_template(
        "admin.html",
        user=admin_user,
        managed_users=managed_users,
        cleanup_state=cleanup_state,
        notification_retention_days=READ_NOTIFICATION_RETENTION_DAYS,
        completed_task_retention_days=COMPLETED_TASK_RETENTION_DAYS,
    )


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def create_user():
    username_raw = request.form.get("username", "")
    login_id_raw = request.form.get("login_id", "")
    temp_password = request.form.get("temp_password", "")

    username, username_error = validate_identity(username_raw, "Username")
    login_id, login_id_error = validate_identity(login_id_raw, "User ID")

    if username_error:
        flash(username_error, "error")
        return redirect(url_for("admin_dashboard"))
    if login_id_error:
        flash(login_id_error, "error")
        return redirect(url_for("admin_dashboard"))
    if len(temp_password) < 6:
        flash("Temporary password must be at least 6 characters.", "error")
        return redirect(url_for("admin_dashboard"))
    if users_col.find_one({"username": username}):
        flash("Display name already taken.", "error")
        return redirect(url_for("admin_dashboard"))
    if users_col.find_one({"login_id": login_id}):
        flash("User ID already taken.", "error")
        return redirect(url_for("admin_dashboard"))

    result = users_col.insert_one(
        {
            "username": username,
            "login_id": login_id,
            "password_hash": generate_password_hash(temp_password),
            "points": 0,
            "profile_description": "",
            "profile_picture_url": "",
            "is_active": True,
            "is_admin": False,
            "is_approved": True,
            "is_disabled": False,
            "password_changed_by_user": False,
            "first_login_reward_granted": False,
            "created_at": datetime.utcnow(),
        }
    )
    create_notification(
        str(result.inserted_id),
        "Admin created your account. Sign in with your permanent user ID and the password given by admin.",
        url_for("login"),
        kind="admin",
    )
    flash(f"Created user {username}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/run-cleanup", methods=["POST"])
@admin_required
def run_cleanup_now():
    summary = run_data_cleanup("manual_admin", force=True)
    flash(
        "Cleanup complete. Removed "
        f"{summary['notifications_deleted']} old read notifications and "
        f"{summary['tasks_deleted']} old completed tasks.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/reset-password", methods=["POST"])
@admin_required
def reset_user_password(user_id):
    object_id = safe_object_id(user_id)
    temp_password = request.form.get("temp_password", "")
    if not object_id:
        flash("User not found.", "error")
        return redirect(url_for("admin_dashboard"))
    if len(temp_password) < 6:
        flash("Reset password must be at least 6 characters.", "error")
        return redirect(url_for("admin_dashboard"))

    target = ensure_user_defaults(
        users_col.find_one({"_id": object_id, "is_approved": True, "is_disabled": False})
    )
    if not target:
        flash("Approved user not found.", "error")
        return redirect(url_for("admin_dashboard"))
    if target.get("password_changed_by_user"):
        flash("Admin cannot reset this password because the user already changed it.", "warning")
        return redirect(url_for("admin_dashboard"))

    users_col.update_one(
        {"_id": object_id},
        {"$set": {"password_hash": generate_password_hash(temp_password)}},
    )
    create_notification(
        str(object_id),
        "Admin reset your account password. Sign in with the new credentials provided by admin.",
        url_for("login"),
        kind="admin",
    )
    flash(f"Password reset for {target['username']}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/disable", methods=["POST"])
@admin_required
def disable_user(user_id):
    current = current_user()
    object_id = safe_object_id(user_id)
    if not object_id:
        flash("User not found.", "error")
        return redirect(url_for("admin_dashboard"))

    target = ensure_user_defaults(users_col.find_one({"_id": object_id}))
    if not target:
        flash("User not found.", "error")
    elif str(target["_id"]) == str(current["_id"]):
        flash("You cannot disable your own admin account.", "warning")
    else:
        users_col.update_one(
            {"_id": object_id},
            {"$set": {"is_disabled": True, "is_active": False}},
        )
        create_notification(
            str(object_id),
            "Your account has been disabled by admin. You can no longer sign in.",
            url_for("login"),
            kind="admin",
        )
        flash(f"Disabled {target['username']}.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/enable", methods=["POST"])
@admin_required
def enable_user(user_id):
    object_id = safe_object_id(user_id)
    if not object_id:
        flash("User not found.", "error")
        return redirect(url_for("admin_dashboard"))

    target = ensure_user_defaults(users_col.find_one({"_id": object_id}))
    if not target:
        flash("User not found.", "error")
    else:
        users_col.update_one(
            {"_id": object_id},
            {"$set": {"is_disabled": False}},
        )
        create_notification(
            str(object_id),
            "Admin re-enabled your account. You can sign in again.",
            url_for("login"),
            kind="admin",
        )
        flash(f"Enabled {target['username']}.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    current = current_user()
    object_id = safe_object_id(user_id)
    if not object_id:
        flash("User not found.", "error")
        return redirect(url_for("admin_dashboard"))

    target = ensure_user_defaults(users_col.find_one({"_id": object_id}))
    if not target:
        flash("User not found.", "error")
    elif str(target["_id"]) == str(current["_id"]):
        flash("You cannot delete your own admin account.", "warning")
    else:
        notifications_col.delete_many({"user_id": str(object_id)})
        users_col.delete_one({"_id": object_id})
        flash(f"Deleted {target['username']} permanently.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/tasks")
@login_required
def tasks():
    user = current_user()
    uid = str(user["_id"])

    posted = annotate_tasks(list(tasks_col.find({"giver_id": uid}).sort("created_at", -1)))
    in_progress = annotate_tasks(
        list(tasks_col.find({"taker_id": uid, "status": "taken"}).sort("updated_at", -1))
    )
    completed = annotate_tasks(
        list(tasks_col.find({"taker_id": uid, "status": "completed"}).sort("updated_at", -1))
    )
    available = annotate_tasks(
        list(tasks_col.find({"status": "open", "giver_id": {"$ne": uid}}).sort("created_at", -1))
    )

    return render_template(
        "tasks.html",
        user=user,
        posted=posted,
        in_progress=in_progress,
        completed=completed,
        available=available,
    )


@app.route("/tasks/new", methods=["GET", "POST"])
@login_required
def new_task():
    user = current_user()
    mention_candidates = active_users(exclude_user_id=user["_id"])

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        level = request.form.get("level", "medium").strip().lower()
        mentioned_user_id = request.form.get("mentioned_user_id", "").strip()

        if not title or not description:
            flash("Title and description are required.", "error")
            return render_template(
                "new_task.html",
                user=user,
                mention_candidates=mention_candidates,
                form_data=request.form,
            )

        if level not in TASK_LEVELS:
            flash("Please choose a valid task level.", "error")
            return render_template(
                "new_task.html",
                user=user,
                mention_candidates=mention_candidates,
                form_data=request.form,
            )

        mentioned_user = None
        mentioned_user_ids = []
        if mentioned_user_id:
            mentioned_object_id = safe_object_id(mentioned_user_id)
            if not mentioned_object_id or mentioned_object_id == user["_id"]:
                flash("Please choose a valid active user to mention.", "error")
                return render_template(
                    "new_task.html",
                    user=user,
                    mention_candidates=mention_candidates,
                    form_data=request.form,
                )

            mentioned_user = ensure_user_defaults(
                users_col.find_one(
                    {"_id": mentioned_object_id, "is_active": True, "is_approved": True}
                )
            )
            if not mentioned_user:
                flash("Mentioned user must be active and approved.", "error")
                return render_template(
                    "new_task.html",
                    user=user,
                    mention_candidates=mention_candidates,
                    form_data=request.form,
                )
            mentioned_user_ids = [str(mentioned_user["_id"])]

        points = TASK_LEVELS[level]["points"]
        tasks_col.insert_one(
            {
                "title": title,
                "description": description,
                "level": level,
                "points": points,
                "giver_id": str(user["_id"]),
                "taker_id": None,
                "mentioned_user_ids": mentioned_user_ids,
                "status": "open",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        )

        task_link = url_for("tasks")
        for active_user in mention_candidates:
            recipient_id = str(active_user["_id"])
            if mentioned_user and recipient_id == str(mentioned_user["_id"]):
                create_notification(
                    recipient_id,
                    f"{user['username']} mentioned you on a {TASK_LEVELS[level]['label']} task: {title}",
                    task_link,
                    kind="mention",
                )
            else:
                create_notification(
                    recipient_id,
                    f"New {TASK_LEVELS[level]['label']} task from {user['username']}: {title}",
                    task_link,
                    kind="task",
                )

        if mentioned_user:
            flash(
                f'Task posted successfully and @{mentioned_user["username"]} was mentioned.',
                "success",
            )
        else:
            flash("Task posted successfully!", "success")

        return redirect(url_for("tasks"))

    return render_template(
        "new_task.html",
        user=user,
        mention_candidates=mention_candidates,
        form_data={},
    )


@app.route("/tasks/<task_id>/take", methods=["POST"])
@login_required
def take_task(task_id):
    user = current_user()
    uid = str(user["_id"])
    object_id = safe_object_id(task_id)
    if not object_id:
        flash("Task not found.", "error")
        return redirect(url_for("tasks"))

    task = tasks_col.find_one({"_id": object_id})
    if not task:
        flash("Task not found.", "error")
    elif task["status"] != "open":
        flash("This task is no longer available.", "warning")
    elif task["giver_id"] == uid:
        flash("You cannot take your own task.", "warning")
    else:
        result = tasks_col.update_one(
            {"_id": object_id, "status": "open"},
            {"$set": {"taker_id": uid, "status": "taken", "updated_at": datetime.utcnow()}},
        )
        if result.modified_count:
            create_notification(
                task["giver_id"],
                f'{user["username"]} took your task: {task["title"]}',
                url_for("tasks"),
                kind="assignment",
            )
            flash(f'You\'ve taken "{task["title"]}"! Get to work.', "success")
        else:
            flash("Someone else just grabbed that task.", "warning")

    return redirect(url_for("tasks"))


@app.route("/tasks/<task_id>/complete", methods=["POST"])
@login_required
def complete_task(task_id):
    user = current_user()
    uid = str(user["_id"])
    object_id = safe_object_id(task_id)
    if not object_id:
        flash("Task not found.", "error")
        return redirect(url_for("tasks"))

    task = tasks_col.find_one({"_id": object_id})
    if not task:
        flash("Task not found.", "error")
    elif task.get("taker_id") != uid:
        flash("You are not assigned to this task.", "warning")
    elif task["status"] == "completed":
        flash("This task is already completed.", "info")
    elif task["status"] != "taken":
        flash("Task cannot be completed from its current state.", "warning")
    else:
        tasks_col.update_one(
            {"_id": object_id},
            {"$set": {"status": "completed", "updated_at": datetime.utcnow()}},
        )
        users_col.update_one({"_id": user["_id"]}, {"$inc": {"points": task["points"]}})
        create_notification(
            task["giver_id"],
            f'{user["username"]} completed your task: {task["title"]}',
            url_for("tasks"),
            kind="completion",
        )
        flash(f"Task completed! You earned {task['points']} points.", "success")

    return redirect(url_for("tasks"))


@app.route("/tasks/<task_id>/delete", methods=["POST"])
@login_required
def delete_task(task_id):
    user = current_user()
    uid = str(user["_id"])
    object_id = safe_object_id(task_id)
    if not object_id:
        flash("Task not found.", "error")
        return redirect(url_for("tasks"))

    task = tasks_col.find_one({"_id": object_id})
    if not task:
        flash("Task not found.", "error")
    elif task["giver_id"] != uid:
        flash("You can only delete your own tasks.", "warning")
    elif task["status"] != "open":
        flash("You can only delete tasks that haven't been taken yet.", "warning")
    else:
        tasks_col.delete_one({"_id": object_id})
        flash("Task deleted.", "info")

    return redirect(url_for("tasks"))


@app.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    user = current_user()
    uid = str(user["_id"])
    notification_id = request.form.get("notification_id", "").strip()

    if notification_id == "all":
        notifications_col.update_many({"user_id": uid, "read": False}, {"$set": {"read": True}})
    else:
        object_id = safe_object_id(notification_id)
        if object_id:
            notifications_col.update_one(
                {"_id": object_id, "user_id": uid},
                {"$set": {"read": True}},
            )

    next_url = request.form.get("next") or url_for("tasks")
    return redirect(next_url)


@app.route("/leaderboard")
@login_required
def leaderboard():
    user = current_user()
    board = list(
        users_col.find(
            {"is_approved": True},
            {"username": 1, "points": 1, "profile_picture_url": 1, "is_active": 1},
        ).sort("points", -1).limit(20)
    )
    board = [merged_user_defaults(entry) for entry in board]
    uid = str(user["_id"])
    rank = next((index + 1 for index, entry in enumerate(board) if str(entry["_id"]) == uid), None)
    return render_template("leaderboard.html", user=user, board=board, rank=rank)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()

    if request.method == "POST":
        username_raw = request.form.get("username", "")
        profile_description = request.form.get("profile_description", "").strip()
        profile_picture_url = request.form.get("profile_picture_url", "").strip()
        is_active = request.form.get("is_active") == "on"
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        username, username_error = validate_identity(username_raw, "Username")

        if username_error:
            flash(username_error, "error")
        elif users_col.find_one({"username": username, "_id": {"$ne": user["_id"]}}):
            flash("Display name already taken.", "error")
        elif new_password and len(new_password) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new_password and new_password != confirm_password:
            flash("New password and confirm password do not match.", "error")
        else:
            update_fields = {
                "username": username,
                "profile_description": profile_description,
                "profile_picture_url": profile_picture_url,
                "is_active": is_active,
            }
            if new_password:
                update_fields["password_hash"] = generate_password_hash(new_password)
                update_fields["password_changed_by_user"] = True

            users_col.update_one({"_id": user["_id"]}, {"$set": update_fields})
            session["username"] = username
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))

    user = current_user()
    uid = str(user["_id"])
    completed_tasks = annotate_tasks(
        list(tasks_col.find({"taker_id": uid, "status": "completed"}).sort("updated_at", -1).limit(10))
    )
    total_posted = tasks_col.count_documents({"giver_id": uid})
    total_completed = tasks_col.count_documents({"taker_id": uid, "status": "completed"})
    total_open = tasks_col.count_documents({"giver_id": uid, "status": "open"})

    return render_template(
        "profile.html",
        user=user,
        completed_tasks=completed_tasks,
        total_posted=total_posted,
        total_completed=total_completed,
        total_open=total_open,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
