import os
import re
import secrets
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
from scheduler_jobs import (
    register_job, unregister_job, load_all_jobs_on_startup,
    run_job_async, parse_cron_or_none,
)

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


@main_bp.route("/")
@login_required
def dashboard():
    jobs = BackupJob.query.order_by(BackupJob.name.asc()).all()
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

        jobs_view.append({
            "job": job, "last_run": last_run, "total_size": total_size,
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


@main_bp.route("/job/<int:job_id>/schedule", methods=["POST"])
@login_required
def job_schedule(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    new_cron = request.form.get("cron_expression", "manual").strip() or "manual"

    try:
        parse_cron_or_none(new_cron)  # valide le format avant d'enregistrer
    except ValueError as exc:
        flash(f"Expression cron invalide pour « {job.name} » : {exc}", "error")
        return redirect(url_for("main.dashboard"))

    job.cron_expression = new_cron
    db.session.commit()
    register_job(job, current_app_ref()) if job.enabled else unregister_job(job.id)
    flash(f"Planification de « {job.name} » mise à jour.", "success")
    return redirect(url_for("main.dashboard"))


@main_bp.route("/job/<int:job_id>/toggle", methods=["POST"])
@login_required
def job_toggle(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    job.enabled = not job.enabled
    db.session.commit()
    register_job(job, current_app_ref()) if job.enabled else unregister_job(job.id)
    return redirect(url_for("main.dashboard"))


@main_bp.route("/job/<int:job_id>/retention", methods=["POST"])
@login_required
def job_retention(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    raw = request.form.get("retention_count", "").strip()

    if raw == "":
        job.retention_count = None
    else:
        try:
            value = int(raw)
            if value < 1:
                raise ValueError
        except ValueError:
            flash("La rétention doit être un nombre entier positif, ou vide pour illimité.", "error")
            return redirect(url_for("main.dashboard"))
        job.retention_count = value

    db.session.commit()
    flash(f"Rétention de « {job.name} » mise à jour.", "success")
    return redirect(url_for("main.dashboard"))


@main_bp.route("/job/<int:job_id>/run", methods=["POST"])
@login_required
def job_run(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    started = run_job_async(job.id, current_app_ref())
    if started:
        flash(f"Sauvegarde « {job.name} » lancée en arrière-plan.", "success")
    else:
        flash(f"Sauvegarde « {job.name} » déjà en cours.", "error")
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
    return render_template("archives.html", job=job, archives=archives, total_size=total_size)


@main_bp.route("/job/<int:job_id>/archives/delete", methods=["POST"])
@login_required
def job_archive_delete(job_id):
    job = db.session.get(BackupJob, job_id) or abort(404)
    archive_name = request.form.get("archive_name", "")
    try:
        dest_dir = host_to_container_path(job.dest_path)
        delete_archive(dest_dir, archive_name)
        flash(f"Archive « {archive_name} » supprimée.", "success")
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
        flash("Nom d'utilisateur et mot de passe requis.", "error")
        return redirect(url_for("main.admin_home"))

    if User.query.filter_by(username=username).first():
        flash("Ce nom d'utilisateur existe déjà.", "error")
        return redirect(url_for("main.admin_home"))

    user = User(username=username, is_admin=is_admin)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"Utilisateur « {username} » créé.", "success")
    return redirect(url_for("main.admin_home"))


@main_bp.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_user_toggle(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("Impossible de te désactiver toi-même.", "error")
        return redirect(url_for("main.admin_home"))

    user.is_active_ = not user.is_active_
    user.session_version += 1  # révoque immédiatement toute session ouverte
    db.session.commit()
    flash(f"Compte « {user.username} » {'réactivé' if user.is_active_ else 'désactivé'}.", "success")
    return redirect(url_for("main.admin_home"))


@main_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("Impossible de te supprimer toi-même.", "error")
        return redirect(url_for("main.admin_home"))
    db.session.delete(user)
    db.session.commit()
    flash("Utilisateur supprimé.", "success")
    return redirect(url_for("main.admin_home"))


@main_bp.route("/admin/bans/<int:ban_id>/clear", methods=["POST"])
@admin_required
def admin_ban_clear(ban_id):
    ban = db.session.get(IPBan, ban_id) or abort(404)
    ban.fail_count = 0
    ban.banned_until = None
    db.session.commit()
    flash("Bannissement levé.", "success")
    return redirect(url_for("main.admin_home"))


# ---------------------------------------------------------------------------
# Synchronisation des jobs depuis le .env (BACK1_NAME/SOURCE/DEST/CRON, ...)
# ---------------------------------------------------------------------------

_ENV_JOB_RE = re.compile(r"^BACK(\d+)_NAME$")
DEFAULT_CRON = "0 3 * * *"  # tous les jours à 3h, utilisé seulement à la création
FORCED_RETENTION_ON_STARTUP = 30  # appliqué à TOUTES les tâches à chaque démarrage du conteneur


def _parse_env_jobs():
    indices = sorted({m.group(1) for k in os.environ if (m := _ENV_JOB_RE.match(k))}, key=int)
    jobs = []
    for i in indices:
        name = os.environ.get(f"BACK{i}_NAME", "").strip()
        source = os.environ.get(f"BACK{i}_SOURCE", "").strip()
        dest = os.environ.get(f"BACK{i}_DEST", "").strip()
        cron = os.environ.get(f"BACK{i}_CRON", "").strip()
        if not name or not source or not dest:
            print(f"[Backer] BACK{i}_* incomplet dans le .env (name/source/dest requis) -- ignoré")
            continue
        jobs.append({"name": name, "source": source, "dest": dest, "cron": cron or None})
    return jobs


def _sync_jobs_from_env():
    """Crée/actualise les jobs définis dans le .env. Ne touche jamais à la
    planification (cron_expression) ou à l'état (enabled) d'un job déjà
    existant -- ça reste géré depuis le dashboard. Un job env_managed dont
    l'entrée .env disparaît est désactivé (pas supprimé -- historique gardé).

    La rétention est forcée à FORCED_RETENTION_ON_STARTUP pour TOUTES les
    tâches à chaque démarrage du conteneur (demande explicite : éviter
    qu'une tâche oubliée en illimité sature le disque -- la valeur reste
    ensuite ajustable depuis le dashboard jusqu'au prochain redémarrage)."""
    env_jobs = _parse_env_jobs()
    seen_names = set()

    for ej in env_jobs:
        seen_names.add(ej["name"])
        existing = BackupJob.query.filter_by(name=ej["name"]).first()
        if existing:
            existing.source_path = ej["source"]
            existing.dest_path = ej["dest"]
            existing.env_managed = True
            existing.retention_count = FORCED_RETENTION_ON_STARTUP
        else:
            db.session.add(BackupJob(
                name=ej["name"], source_path=ej["source"], dest_path=ej["dest"],
                cron_expression=ej["cron"] or DEFAULT_CRON,
                retention_count=FORCED_RETENTION_ON_STARTUP,
                enabled=True, env_managed=True,
            ))
    db.session.commit()

    orphans = BackupJob.query.filter_by(env_managed=True).all()
    for job in orphans:
        if job.name not in seen_names:
            job.enabled = False
    db.session.commit()


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
        return {"app_version": "v4"}

    @app.before_request
    def block_banned_ips():
        if request.endpoint == "static":
            return
        ban = get_active_ban(_client_ip())
        if ban:
            return "Accès temporairement bloqué (trop de tentatives échouées).", 403

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
        _sync_jobs_from_env()
        # Le fichier SQLite contient des hachages de mots de passe -- mêmes
        # permissions restrictives que secret_key, pas seulement l'umask
        # par défaut.
        if os.path.exists(DB_FILE):
            os.chmod(DB_FILE, 0o600)

    _app_singleton = app

    if not scheduler.running:
        scheduler.init_app(app)
        scheduler.start()
    load_all_jobs_on_startup(app)

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
