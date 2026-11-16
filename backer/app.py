import os
import secrets
import shutil
from datetime import datetime, timedelta

from flask import Flask, Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user

from extensions import db, login_manager, scheduler
from models import User, BackupJob, BackupHistory, LoginHistory, IPBan
from auth import auth_bp, admin_required, get_active_ban, _client_ip
from utils import (
    host_to_container_path, list_job_archives, delete_archive, read_log_file,
    translate_cron, check_mount_status, PathError,
)
from scheduler_jobs import run_job_async, is_job_running, _apscheduler_job_id
import jobs_config
from jobs_config import (
    JOBS_FILE, EXAMPLE_YAML, parse_jobs_yaml, read_jobs_file, write_jobs_file,
    apply_jobs, init_jobs,
)
from onedrive_monitor import (
    scan_log, scan_log_job, upload_info, upload_times, monitor_state,
)

APP_VERSION = "v4.4"

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SECRET_KEY_FILE = os.path.join(DATA_DIR, "secret_key")
DB_FILE = os.path.join(DATA_DIR, "backer.db")

# Aucune route de Backer n'a besoin d'un corps de requête volumineux (le
# plus gros formulaire est la création d'un compte admin) -- une limite
# basse évite qu'un Content-Length annoncé énorme épuise la mémoire du
# conteneur avant même d'avoir authentifié qui que ce soit.
MAX_CONTENT_LENGTH = 64 * 1024  # 64 Ko

# Routes sensibles sur lesquelles on ajoute des headers défensifs
# (anti-clickjacking + anti-cache) sans les imposer globalement, pour ne
# jamais gêner un usage légitime ailleurs dans l'app.
_PROTECTED_ENDPOINT_PREFIXES = ("auth.login", "main.admin_")

main_bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# Routes principales (dashboard + gestion des jobs)
# ---------------------------------------------------------------------------

@main_bp.route("/health")
def health():
    return jsonify({"status": "ok"})


def _iso_local(dt):
    """Date ISO avec fuseau (les dates en DB sont naïves, heure locale du conteneur)."""
    return dt.astimezone().isoformat() if dt else None


def _seconds_between(later, earlier):
    return round((later - earlier).total_seconds()) if later and earlier else None


def _visible_jobs():
    """Aucune tâche affichée ni planifiée tant que jobs.yml n'existe pas."""
    if jobs_config.status["state"] == "missing":
        return []
    return BackupJob.query.order_by(BackupJob.name.asc()).all()


# Fenêtre (en jours) pour compter les échecs récents dans /api/status.
FAILURE_WINDOW_DAYS = 7


@main_bp.route("/api/status")
def api_status():
    """Statut des sauvegardes en JSON, pour tout outil externe (ex. un widget
    Homepage). Sans login -- réseau local seulement. Une clé par tâche (nom de
    la tâche), plus « onedrive_client » (log du client OneDrive) et « backer »."""
    scan_log()
    now = datetime.now()

    jobs = _visible_jobs()
    data = {}
    for job in jobs:
        base = BackupHistory.query.filter_by(job_id=job.id)
        successes = (
            base.filter_by(status="success")
            .order_by(BackupHistory.finished_at.desc()).limit(2).all()
        )
        last_ok = successes[0] if successes else None
        prev_ok = successes[1] if len(successes) > 1 else None
        last_fail = base.filter_by(status="failed").order_by(BackupHistory.started_at.desc()).first()
        recent_failures = base.filter(
            BackupHistory.status == "failed",
            BackupHistory.started_at >= now - timedelta(days=FAILURE_WINDOW_DAYS),
        ).count()

        ap_job = scheduler.get_job(_apscheduler_job_id(job.id))

        try:
            source_status = check_mount_status(host_to_container_path(job.source_path))
        except PathError:
            source_status = "error"

        archive_count, archives_total, du = 0, 0, None
        try:
            dest_dir = host_to_container_path(job.dest_path)
            archives, archives_total = list_job_archives(dest_dir, job.name)
            archive_count = len(archives)
            if os.path.isdir(dest_dir):
                du = shutil.disk_usage(dest_dir)
        except (PathError, OSError):
            pass

        if last_ok is None:
            onedrive_status, uploaded_at = "no backup", None
        else:
            uploaded_at = upload_info(last_ok.archive_name)
            onedrive_status = "uploaded" if uploaded_at else "pending"

        last_size = last_ok.size_bytes if last_ok else None
        prev_size = prev_ok.size_bytes if prev_ok else None

        data[job.name] = {
            "enabled": job.enabled,
            "running": is_job_running(job.id),
            "source_status": source_status,  # ok / empty / error
            "cron": job.cron_expression,
            "cron_readable": translate_cron(job.cron_expression),
            "retention": job.retention_count,
            "last_success": _iso_local(last_ok.finished_at) if last_ok else None,
            "last_success_age_seconds": _seconds_between(now, last_ok.finished_at) if last_ok else None,
            "next_run": _iso_local(ap_job.next_run_time) if ap_job else None,
            "last_duration_seconds": _seconds_between(last_ok.finished_at, last_ok.started_at) if last_ok else None,
            "last_size_bytes": last_size,
            "previous_size_bytes": prev_size,
            "size_change_bytes": (last_size - prev_size) if last_size is not None and prev_size is not None else None,
            "archive_count": archive_count,
            "archives_total_bytes": archives_total,
            "failures_last_7_days": recent_failures,
            "last_failure_at": _iso_local(last_fail.started_at) if last_fail else None,
            "last_failure_message": last_fail.message if last_fail else None,
            "dest_free_bytes": du.free if du else None,
            "dest_total_bytes": du.total if du else None,
            "dest_used_bytes": du.used if du else None,
            "onedrive": onedrive_status,
            "onedrive_uploaded_at": _iso_local(uploaded_at),
            "onedrive_delay_seconds": _seconds_between(uploaded_at, last_ok.finished_at) if last_ok and uploaded_at else None,
        }

    state = monitor_state()
    items = state["last_items"]
    deletion = state["last_deletion"]
    free = state["free_space"]
    data["onedrive_client"] = {
        "log": "ok" if state["readable"] else "unreadable",
        "last_sync": _iso_local(state["last_sync"]),
        "last_service_contact": _iso_local(state["last_reach"]),
        "last_items_processed": items["count"] if items else None,
        "last_items_processed_at": _iso_local(items["at"]) if items else None,
        "last_deleted_path": deletion["path"] if deletion else None,
        "last_deleted_at": _iso_local(deletion["at"]) if deletion else None,
        "clock_skew_seconds": state["clock_skew_seconds"],
        # Espace restant affiché par le client à son démarrage (mode verbeux seulement).
        "remaining_free_bytes": free["bytes"] if free else None,
        "remaining_free_at": _iso_local(free["at"]) if free else None,
    }
    data["backer"] = {
        "version": APP_VERSION,
        "jobs_file": jobs_config.status["state"],  # ok / missing / invalid
        "jobs_total": len(jobs),
        "jobs_enabled": sum(1 for j in jobs if j.enabled),
    }
    return jsonify(data)


@main_bp.route("/")
@login_required
def dashboard():
    jobs = _visible_jobs()
    jobs_view = []
    grand_total = 0
    any_running = False

    for job in jobs:
        last_run = job.history[0] if job.history else None
        if last_run and last_run.status == "running":
            any_running = True

        total_size = 0
        archive_count = 0
        source_status = "error"
        try:
            dest_dir = host_to_container_path(job.dest_path)
            archives, total_size = list_job_archives(dest_dir, job.name)
            archive_count = len(archives)
        except PathError:
            pass
        try:
            source_container_path = host_to_container_path(job.source_path)
            source_status = check_mount_status(source_container_path)
        except PathError:
            source_status = "error"
        # "empty" et "error" sont tous deux affichés comme Déconnecté --
        # dans les deux cas la source n'est pas fiable pour une sauvegarde.
        source_connected = source_status == "ok"
        grand_total += total_size

        estimated_total = None
        if archive_count > 0 and job.retention_count:
            estimated_total = round((total_size / archive_count) * job.retention_count)

        onedrive_uploaded_at = None
        if last_run and last_run.status == "success":
            onedrive_uploaded_at = upload_info(last_run.archive_name)

        jobs_view.append({
            "job": job, "last_run": last_run, "total_size": total_size,
            "onedrive_uploaded_at": onedrive_uploaded_at,
            "archive_count": archive_count, "estimated_total": estimated_total,
            "cron_readable": translate_cron(job.cron_expression),
            "source_connected": source_connected,
        })

    last_failed_login = (
        LoginHistory.query.filter_by(success=False, dismissed=False)
        .order_by(LoginHistory.timestamp.desc()).first()
    )

    return render_template(
        "dashboard.html", jobs_view=jobs_view, grand_total=grand_total,
        any_running=any_running, last_failed_login=last_failed_login,
        jobs_status=jobs_config.status, jobs_file=JOBS_FILE,
    )


@main_bp.route("/security/alert/dismiss", methods=["POST"])
@login_required
def security_alert_dismiss():
    login_id = request.form.get("login_id", "")
    entry = db.session.get(LoginHistory, login_id) if login_id.isdigit() else None
    if entry:
        entry.dismissed = True
        db.session.commit()
    return redirect(url_for("main.dashboard"))


@main_bp.route("/job/<int:job_id>/run", methods=["POST"])
@login_required
def job_run(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    started = run_job_async(job.id, current_app_ref())
    if started:
        flash(f"Backup '{job.name}' started in the background.", "success")
    else:
        flash(f"Backup '{job.name}' is already running.", "error")
    return redirect(url_for("main.dashboard"))


@main_bp.route("/job/<int:job_id>/archives")
@login_required
def job_archives(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    try:
        dest_dir = host_to_container_path(job.dest_path)
        archives, total_size = list_job_archives(dest_dir, job.name)
    except PathError as exc:
        flash(str(exc), "error")
        archives, total_size = [], 0
    uploaded = upload_times([a["name"] for a in archives])
    for a in archives:
        a["onedrive_at"] = uploaded.get(a["name"])
    return render_template("archives.html", job=job, archives=archives, total_size=total_size)


@main_bp.route("/job/<int:job_id>/archives/delete", methods=["POST"])
@login_required
def job_archive_delete(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    archive_name = request.form.get("archive_name", "")
    try:
        dest_dir = host_to_container_path(job.dest_path)
        delete_archive(dest_dir, archive_name)
        flash(f"Archive '{archive_name}' deleted.", "success")
    except PathError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.job_archives", job_id=job.id))


@main_bp.route("/job/<int:job_id>/log/<path:log_name>")
@login_required
def job_log_view(job_id, log_name):
    job = db.session.get(BackupJob, job_id) or abort(404)
    try:
        dest_dir = host_to_container_path(job.dest_path)
        content = read_log_file(dest_dir, log_name)
    except PathError as exc:
        abort(404, str(exc))
    return render_template("log_view.html", job=job, log_name=log_name, content=content)


# ---------------------------------------------------------------------------
# Administration (comptes utilisateurs, historique de connexions)
# ---------------------------------------------------------------------------

@main_bp.route("/admin")
@admin_required
def admin_home():
    users = User.query.order_by(User.created_at.asc()).all()
    recent_logins = LoginHistory.query.order_by(LoginHistory.timestamp.desc()).limit(50).all()
    active_bans = IPBan.query.filter(IPBan.banned_until.isnot(None)).all()
    active_bans = [b for b in active_bans if b.banned_until and b.banned_until > datetime.now()]
    return render_template("admin.html", users=users, recent_logins=recent_logins, active_bans=active_bans)


@main_bp.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_user_add():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_admin = bool(request.form.get("is_admin"))

    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect(url_for("main.admin_home"))

    if User.query.filter_by(username=username).first():
        flash("This username already exists.", "error")
        return redirect(url_for("main.admin_home"))

    user = User(username=username, is_admin=is_admin)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"User '{username}' created.", "success")
    return redirect(url_for("main.admin_home"))


@main_bp.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_user_toggle(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You cannot disable yourself.", "error")
        return redirect(url_for("main.admin_home"))

    user.is_active_ = not user.is_active_
    user.session_version += 1  # révoque immédiatement toute session ouverte
    db.session.commit()
    flash(f"Account '{user.username}' {'re-enabled' if user.is_active_ else 'disabled'}.", "success")
    return redirect(url_for("main.admin_home"))


@main_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You cannot delete yourself.", "error")
        return redirect(url_for("main.admin_home"))
    db.session.delete(user)
    db.session.commit()
    flash("User deleted.", "success")
    return redirect(url_for("main.admin_home"))


@main_bp.route("/admin/bans/<int:ban_id>/clear", methods=["POST"])
@admin_required
def admin_ban_clear(ban_id):
    ban = db.session.get(IPBan, ban_id) or abort(404)
    ban.fail_count = 0
    ban.banned_until = None
    db.session.commit()
    flash("Ban lifted.", "success")
    return redirect(url_for("main.admin_home"))


# ---------------------------------------------------------------------------
# Éditeur du fichier de configuration des sauvegardes (jobs.yml)
# ---------------------------------------------------------------------------

@main_bp.route("/admin/jobs", methods=["GET", "POST"])
@admin_required
def admin_jobs():
    file_exists = os.path.exists(JOBS_FILE)

    if request.method == "POST":
        text = request.form.get("yaml", "").replace("\r\n", "\n")
        jobs, errors = parse_jobs_yaml(text)
        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "admin_jobs.html", yaml_text=text, jobs_file=JOBS_FILE, file_exists=file_exists,
            ), 400
        try:
            write_jobs_file(text)
        except OSError as exc:
            flash(f"Cannot write {JOBS_FILE}: {exc}", "error")
            return render_template(
                "admin_jobs.html", yaml_text=text, jobs_file=JOBS_FILE, file_exists=file_exists,
            ), 500
        removed = apply_jobs(jobs, current_app_ref())
        message = f"Saved and applied: {len(jobs)} job(s)."
        if removed:
            message += f" Removed with their history: {', '.join(removed)}."
        flash(message, "success")
        return redirect(url_for("main.admin_jobs"))

    yaml_text = EXAMPLE_YAML
    if file_exists:
        try:
            yaml_text = read_jobs_file()
        except OSError as exc:
            flash(f"Cannot read {JOBS_FILE}: {exc}", "error")
    return render_template(
        "admin_jobs.html", yaml_text=yaml_text, jobs_file=JOBS_FILE, file_exists=file_exists,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

_app_singleton = None


def current_app_ref():
    """Le scheduler a besoin d'une référence à l'app pour créer un app_context
    lors de l'exécution différée d'un job."""
    return _app_singleton


def _load_or_create_secret_key():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(SECRET_KEY_FILE, 0o600)
    return key


def create_app():
    global _app_singleton
    app = Flask(__name__)

    app.config["SECRET_KEY"] = _load_or_create_secret_key()
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_FILE}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    is_https = os.environ.get("CONNECTION_TYPE", "https").lower() == "https"
    app.config.update(
        SESSION_COOKIE_SECURE=is_https,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )

    app.config["SCHEDULER_API_ENABLED"] = False

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_app_version():
        return {"app_version": APP_VERSION}

    @app.before_request
    def block_banned_ips():
        if request.endpoint == "static":
            return
        ban = get_active_ban(_client_ip())
        if ban:
            return "Access temporarily blocked (too many failed attempts).", 403

    @app.after_request
    def add_defensive_headers(response):
        # Anti-clickjacking + anti-cache, uniquement sur /login et les
        # routes /admin/* -- pas globalement, pour ne jamais gêner un
        # usage légitime du dashboard ailleurs (ex: iframe personnelle).
        endpoint = request.endpoint or ""
        if endpoint.startswith(_PROTECTED_ENDPOINT_PREFIXES):
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            response.headers["Cache-Control"] = "no-store"
        return response

    with app.app_context():
        db.create_all()
        _bootstrap_admin_if_needed()
        # Le fichier SQLite contient des hachages de mots de passe -- mêmes
        # permissions restrictives que secret_key, pas seulement l'umask
        # par défaut.
        if os.path.exists(DB_FILE):
            os.chmod(DB_FILE, 0o600)

    _app_singleton = app

    if not scheduler.running:
        scheduler.init_app(app)
        scheduler.start()
    with app.app_context():
        init_jobs(app)

    # Lecture périodique du log OneDrive (confirmation des uploads).
    scheduler.add_job(
        id="onedrive_log_scan", func=scan_log_job, args=[app],
        trigger="interval", seconds=60, replace_existing=True,
    )

    return app


def _bootstrap_admin_if_needed():
    """Crée le premier compte admin depuis les variables d'env si la table est vide."""
    if User.query.count() > 0:
        return
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        return
    user = User(username=username, is_admin=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
