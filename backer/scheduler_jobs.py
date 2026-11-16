"""
Enregistrement des BackupJob dans APScheduler (via une expression cron
unique et éditable) et exécution effective d'une sauvegarde.
"""
import threading
from datetime import datetime
from apscheduler.triggers.cron import CronTrigger

import settings_store
from extensions import db, scheduler
from models import BackupJob, BackupHistory
from utils import host_to_container_path, run_backup, enforce_retention, append_to_log_file, PathError


def _apscheduler_job_id(job_id):
    return f"backup_job_{job_id}"


def parse_cron_or_none(cron_expression):
    """Retourne un CronTrigger si l'expression est valide et != 'manual', sinon None.
    Lève ValueError si l'expression n'est ni 'manual' ni un cron valide."""
    if not cron_expression or cron_expression.strip().lower() == "manual":
        return None
    return CronTrigger.from_crontab(cron_expression.strip())


def is_job_running(job_id):
    """Vrai si la dernière exécution connue de cette tâche est encore 'running'."""
    last = (
        BackupHistory.query.filter_by(job_id=job_id)
        .order_by(BackupHistory.id.desc()).first()
    )
    return bool(last and last.status == "running")


def execute_backup_job(job_id, app):
    """Exécute réellement une sauvegarde. Appelé par APScheduler, ou en
    tâche de fond via run_job_async -- jamais dans le fil d'une requête web."""
    with app.app_context():
        job = db.session.get(BackupJob, job_id)
        if job is None or not job.enabled:
            return

        history = BackupHistory(job_id=job.id, status="running")
        db.session.add(history)
        db.session.commit()

        try:
            source_path = host_to_container_path(job.source_path)
            dest_dir = host_to_container_path(job.dest_path, require_writable=True)
            staging_host = settings_store.settings.get("staging_dir")
            staging_dir = (
                host_to_container_path(staging_host, require_writable=True) if staging_host else None
            )
        except PathError as exc:
            history.status = "failed"
            history.message = f"Invalid path: {exc}"
            history.finished_at = datetime.now()
            db.session.commit()
            return

        result = run_backup(source_path, dest_dir, job.name, staging_dir)

        history.status = result["status"]
        history.message = result["message"]
        history.archive_name = result["archive_name"]
        history.log_name = result["log_name"]
        history.size_bytes = result["size_bytes"]
        history.finished_at = datetime.now()
        db.session.commit()

        if result["status"] == "success":
            retention_lines = []
            enforce_retention(dest_dir, job.name, job.retention_count, log_lines=retention_lines)
            if retention_lines and result["log_name"]:
                append_to_log_file(dest_dir, result["log_name"], retention_lines)


def run_job_async(job_id, app):
    """Lance execute_backup_job dans un thread séparé pour ne jamais bloquer
    une requête web -- le clic sur « Lancer » revient tout de suite.
    Retourne False sans rien lancer si cette tâche est déjà en cours."""
    if is_job_running(job_id):
        return False
    thread = threading.Thread(target=execute_backup_job, args=(job_id, app), daemon=True)
    thread.start()
    return True


def register_job(job: BackupJob, app):
    """(Ré)enregistre un job dans APScheduler selon son cron_expression actuel."""
    apsched_id = _apscheduler_job_id(job.id)
    if scheduler.get_job(apsched_id):
        scheduler.remove_job(apsched_id)

    if not job.enabled:
        return

    try:
        trigger = parse_cron_or_none(job.cron_expression)
    except ValueError:
        return  # expression invalide -- laissé en manuel plutôt que de planter

    if trigger is None:
        return  # "manual" -> rien à planifier, exécution via le bouton seulement

    scheduler.add_job(
        id=apsched_id,
        func=execute_backup_job,
        args=[job.id, app],
        trigger=trigger,
        replace_existing=True,
    )


def unregister_job(job_id):
    apsched_id = _apscheduler_job_id(job_id)
    if scheduler.get_job(apsched_id):
        scheduler.remove_job(apsched_id)


def load_all_jobs_on_startup(app):
    """Relit tous les jobs actifs en DB au démarrage et les réenregistre dans
    APScheduler -- sans ça, toute planification est perdue à chaque
    redémarrage du conteneur."""
    with app.app_context():
        jobs = BackupJob.query.filter_by(enabled=True).all()
        for job in jobs:
            register_job(job, app)
