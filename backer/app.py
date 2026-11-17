import math
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
    format_duration,
    host_to_container_path, list_job_archives, delete_archive, read_log_file,
    translate_cron, check_mount_status, PathError,
)
from scheduler_jobs import run_job_async, is_job_running, _apscheduler_job_id
import alerts
import jobs_config
import monitoring
import notifier
import settings_store
from jobs_config import (
    JOBS_FILE, EXAMPLE_YAML, parse_jobs_yaml, read_jobs_file, write_jobs_file,
    apply_jobs, init_jobs,
)
from onedrive_monitor import (
    scan_log, scan_log_job, upload_info, upload_times, monitor_state, log_status,
)

APP_VERSION = "v4.7"

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


_PIE_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16"]


def _build_pie(items):
    """items : liste de (nom, taille). Retourne les parts d'un camembert SVG
    (cercle de rayon 1 centré en 0,0)."""
    items = [(name, size) for name, size in items if size and size > 0]
    total = sum(size for _, size in items)
    pie = []
    angle = -math.pi / 2
    for i, (name, size) in enumerate(items):
        fraction = size / total
        entry = {
            "name": name, "size": size, "percent": round(fraction * 100, 1),
            "color": _PIE_COLORS[i % len(_PIE_COLORS)], "path": None,
        }
        if len(items) > 1:
            end = angle + fraction * 2 * math.pi
            x1, y1 = math.cos(angle), math.sin(angle)
            x2, y2 = math.cos(end), math.sin(end)
            large = 1 if fraction > 0.5 else 0
            entry["path"] = f"M 0 0 L {x1:.5f} {y1:.5f} A 1 1 0 {large} 1 {x2:.5f} {y2:.5f} Z"
            angle = end
        pie.append(entry)
    return pie


def _sparkline(values, width=320, height=90, pad=8):
    """Courbe SVG simple. Retourne None s'il y a moins de deux valeurs."""
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    span = hi - lo
    n = len(values)
    points = []
    for i, value in enumerate(values):
        x = pad + i * (width - 2 * pad) / (n - 1)
        y = height / 2 if span == 0 else height - pad - (value - lo) / span * (height - 2 * pad)
        points.append((round(x, 1), round(y, 1)))
    return {
        "width": width, "height": height,
        "points": " ".join(f"{x},{y}" for x, y in points),
        "last": points[-1], "min": lo, "max": hi, "latest": values[-1],
    }


def _offsite_summary(used_bytes):
    """Espace du stockage en ligne : total saisi dans jobs.yml, pris par les
    archives de Backer, restant. None si le total n'est pas renseigné."""
    gb = settings_store.settings.get("offsite_storage_gb")
    if not gb:
        return None
    total = int(gb * 1024 ** 3)
    return {
        "total_bytes": total,
        "used_bytes": used_bytes,
        "remaining_bytes": total - used_bytes,
        "percent_used": min(100, round(used_bytes / total * 100, 1)),
    }


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


_ONEDRIVE_LABELS = {
    "none": "no backup", "uploaded": "uploaded", "pending": "pending",
    "unconfirmed": "unconfirmed", "failed": "failed", "unknown": "unknown",
}
_HEALTH_RANK = {"ok": 0, "disabled": 0, "warning": 1, "critical": 2}


@main_bp.route("/api/status")
def api_status():
    """Statut des sauvegardes en JSON, pour tout outil externe (ex. un widget
    Homepage). Sans login -- réseau local seulement. Une clé par tâche (nom de
    la tâche), plus « onedrive_client », « offsite_storage » et « backer »."""
    scan_log()
    now = datetime.now()

    jobs = _visible_jobs()
    data = {}
    grand_total = 0
    worst = "ok"

    for job in jobs:
        snap = monitoring.snapshot(job, now)
        health_status, health_reasons = monitoring.health(job, snap)
        grand_total += snap["archives_total"]
        if _HEALTH_RANK[health_status] > _HEALTH_RANK[worst]:
            worst = health_status

        last_ok, last_fail, du = snap["last_ok"], snap["last_fail"], snap["disk"]
        last_size, prev_size = snap["last_size"], snap["prev_size"]

        data[job.name] = {
            "health": health_status,  # ok / warning / critical / disabled
            "health_reasons": health_reasons,
            "enabled": job.enabled,
            "running": snap["running"],
            "source_status": snap["source_status"],  # ok / empty / error
            "cron": job.cron_expression,
            "cron_readable": translate_cron(job.cron_expression),
            "retention": job.retention_count,
            "last_success": _iso_local(last_ok.finished_at) if last_ok else None,
            "last_success_age_seconds": _seconds_between(now, last_ok.finished_at) if last_ok else None,
            "next_run": _iso_local(snap["next_run"]),
            "last_duration_seconds": _seconds_between(last_ok.finished_at, last_ok.started_at) if last_ok else None,
            "average_duration_seconds": snap["backup_avg"],
            "backup_elapsed_seconds": snap["backup_elapsed"],
            "backup_remaining_seconds": snap["backup_remaining"],
            "backup_overdue": snap["backup_overdue"],
            "last_size_bytes": last_size,
            "previous_size_bytes": prev_size,
            "size_change_bytes": (last_size - prev_size) if last_size is not None and prev_size is not None else None,
            "size_drop": snap["size_drop"],
            "archive_count": snap["archive_count"],
            "archives_total_bytes": snap["archives_total"],
            "failures_last_7_days": snap["recent_failures"],
            "last_failure_at": _iso_local(last_fail.started_at) if last_fail else None,
            "last_failure_message": last_fail.message if last_fail else None,
            "dest_free_bytes": du.free if du else None,
            "dest_total_bytes": du.total if du else None,
            "dest_used_bytes": du.used if du else None,
            "dest_free_percent": snap["dest_free_percent"],
            "onedrive": _ONEDRIVE_LABELS[snap["upload_state"]],
            "onedrive_uploaded_at": _iso_local(snap["uploaded_at"]),
            "onedrive_failed_at": _iso_local(snap["failed_at"]),
            "onedrive_delay_seconds": snap["last_delay"],
            "average_upload_seconds": snap["upload_avg"],
            "backup_speed_mb_s": snap["backup_speed"],
            "average_backup_speed_mb_s": snap["backup_speed_avg"],
            "upload_speed_mb_s": snap["upload_speed"],
            "average_upload_speed_mb_s": snap["upload_speed_avg"],
            "source_connected": snap["source_status"] == "ok",
            "upload_elapsed_seconds": snap["upload_elapsed"],
            "upload_remaining_seconds": snap["upload_remaining"],
            "upload_overdue": snap["upload_overdue"],
        }

    log_state, log_age = log_status()
    state = monitor_state()
    items = state["last_items"]
    deletion = state["last_deletion"]
    free = state["free_space"]
    data["onedrive_client"] = {
        "log": log_state,  # ok / stale (plus mis à jour) / unreadable
        "log_age_seconds": log_age,
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
    sources = {
        name: entry["source_status"]
        for name, entry in data.items() if isinstance(entry, dict) and "source_status" in entry
    }
    data["connections"] = {
        "onedrive_log": log_state,
        "onedrive_service_contact_age_seconds": _seconds_between(now, state["last_reach"]),
        "sources": sources,
        "all_sources_ok": all(v == "ok" for v in sources.values()),
    }
    offsite = _offsite_summary(grand_total)
    data["offsite_storage"] = {
        "total_bytes": offsite["total_bytes"] if offsite else None,
        "used_by_backer_bytes": grand_total,
        "remaining_bytes": offsite["remaining_bytes"] if offsite else None,
    }
    data["backer"] = {
        "version": APP_VERSION,
        "health": worst,  # le pire état parmi les tâches
        "staging_dir": settings_store.settings.get("staging_dir"),
        "email_alerts_configured": notifier.is_configured(),
        "jobs_file": jobs_config.status["state"],  # ok / missing / invalid
        "jobs_total": len(jobs),
        "jobs_enabled": sum(1 for j in jobs if j.enabled),
    }
    return jsonify(data)


@main_bp.route("/")
@login_required
def dashboard():
    now = datetime.now()
    log_state, log_age = log_status()
    jobs = _visible_jobs()
    jobs_view = []
    grand_total = 0
    any_active = False

    for job in jobs:
        snap = monitoring.snapshot(job, now)
        health_status, health_reasons = monitoring.health(job, snap)
        if snap["running"] or snap["upload_state"] == "pending":
            any_active = True

        total_size = snap["archives_total"]
        archive_count = snap["archive_count"]
        grand_total += total_size

        estimated_total = None
        if archive_count > 0 and job.retention_count:
            estimated_total = round((total_size / archive_count) * job.retention_count)

        jobs_view.append({
            "job": job, "snap": snap, "last_run": snap["last_run"],
            "total_size": total_size, "archive_count": archive_count,
            "estimated_total": estimated_total,
            "cron_readable": translate_cron(job.cron_expression),
            # "empty" et "error" sont tous deux affichés comme Disconnected --
            # dans les deux cas la source n'est pas fiable pour une sauvegarde.
            "source_connected": snap["source_status"] == "ok",
            "health": health_status, "health_reasons": health_reasons,
            "avg_text": format_duration(snap["backup_avg"]) if snap["backup_avg"] else None,
            "avg_upload_text": format_duration(snap["upload_avg"]) if snap["upload_avg"] else None,
        })

    last_failed_login = (
        LoginHistory.query.filter_by(success=False, dismissed=False)
        .order_by(LoginHistory.timestamp.desc()).first()
    )

    return render_template(
        "dashboard.html", jobs_view=jobs_view, grand_total=grand_total,
        any_running=any_active, last_failed_login=last_failed_login,
        jobs_status=jobs_config.status, jobs_file=JOBS_FILE,
        pie=_build_pie([(i["job"].name, i["total_size"]) for i in jobs_view]),
        offsite=_offsite_summary(grand_total),
        log_state=log_state, log_age=log_age,
    )


@main_bp.route("/trends")
@login_required
def trends():
    """Courbes de la taille des archives et de la durée des sauvegardes
    (30 dernières réussites de chaque tâche)."""
    charts = []
    for job in _visible_jobs():
        rows = (
            BackupHistory.query.filter_by(job_id=job.id, status="success")
            .filter(BackupHistory.finished_at.isnot(None))
            .order_by(BackupHistory.finished_at.desc()).limit(30).all()
        )
        rows.reverse()
        sizes = [r.size_bytes for r in rows if r.size_bytes is not None]
        durations = [(r.finished_at - r.started_at).total_seconds() for r in rows if r.started_at]
        charts.append({
            "job": job, "count": len(rows),
            "first": rows[0].finished_at if rows else None,
            "last": rows[-1].finished_at if rows else None,
            "size": _sparkline(sizes), "duration": _sparkline(durations),
        })
    return render_template("trends.html", charts=charts)


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
        jobs, new_settings, errors = parse_jobs_yaml(text)
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
        removed = apply_jobs(jobs, current_app_ref(), new_settings)
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


@main_bp.route("/admin/test-email", methods=["POST"])
@admin_required
def admin_test_email():
    ok, error = notifier.send_email(
        "[Backer] Test e-mail",
        "This is a test e-mail from Backer. If you can read it, e-mail alerts are working.\n",
    )
    if ok:
        flash("Test e-mail sent.", "success")
    else:
        flash(f"Test e-mail failed: {error}", "error")
    return redirect(url_for("main.admin_jobs"))


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
    app.jinja_env.filters["duration"] = format_duration

    @app.context_processor
    def inject_app_version():
        return {"app_version": APP_VERSION, "smtp_configured": notifier.is_configured()}

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

    # Vérification périodique des alertes e-mail.
    scheduler.add_job(
        id="backer_alert_check", func=alerts.check_alerts_job, args=[app],
        trigger="interval", seconds=120, replace_existing=True,
    )

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
