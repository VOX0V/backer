"""
État calculé de chaque sauvegarde : une seule source de vérité pour le
dashboard, l'API, le voyant de santé et les alertes e-mail.

snapshot(job)  -> dictionnaire de faits (dernières exécutions, durées moyennes,
                  temps restants, état de l'upload, disque, source...)
health(job, s) -> (statut, raisons) avec statut "ok" / "warning" / "critical" / "disabled"
"""
import os
import shutil
from datetime import datetime, timedelta

import settings_store
from extensions import scheduler
from models import BackupHistory
from onedrive_monitor import upload_info, upload_times, upload_failure_info, log_status
from scheduler_jobs import is_job_running, _apscheduler_job_id
from utils import host_to_container_path, check_mount_status, list_job_archives, PathError

# Fenêtre (en jours) pour compter les échecs récents.
FAILURE_WINDOW_DAYS = 7


def alerts_cfg():
    return settings_store.settings["alerts"]


def average_duration(job_id, limit=10):
    """Durée moyenne (secondes) des dernières sauvegardes réussies, ou None."""
    rows = (
        BackupHistory.query.filter_by(job_id=job_id, status="success")
        .filter(BackupHistory.finished_at.isnot(None))
        .order_by(BackupHistory.finished_at.desc()).limit(limit).all()
    )
    durations = [
        (r.finished_at - r.started_at).total_seconds()
        for r in rows if r.started_at and r.finished_at
    ]
    return round(sum(durations) / len(durations)) if durations else None


def average_upload_delay(job_id, limit=10):
    """Délai moyen (secondes) entre la fin de l'archive et la confirmation de
    son upload, sur les dernières sauvegardes réussies dont l'upload est confirmé."""
    rows = (
        BackupHistory.query.filter_by(job_id=job_id, status="success")
        .filter(BackupHistory.finished_at.isnot(None), BackupHistory.archive_name.isnot(None))
        .order_by(BackupHistory.finished_at.desc()).limit(limit).all()
    )
    times = upload_times([r.archive_name for r in rows])
    delays = []
    for r in rows:
        uploaded = times.get(r.archive_name)
        if uploaded:
            delay = (uploaded - r.finished_at).total_seconds()
            if delay >= 0:
                delays.append(delay)
    return round(sum(delays) / len(delays)) if delays else None


def _mb_per_s(size_bytes, seconds):
    """Vitesse en Mo/s (1 Mo = 1 000 000 octets), ou None."""
    if not size_bytes or not seconds or seconds <= 0:
        return None
    return round(size_bytes / seconds / 1_000_000, 2)


def average_speeds(job_id, limit=10):
    """Vitesses moyennes (Mo/s) sur les dernières sauvegardes réussies :
    (création de l'archive, upload). Taille de l'archive / durée de la sauvegarde,
    et taille / délai jusqu'à la confirmation de l'upload (ce délai inclut le
    temps que le client met à détecter le fichier)."""
    rows = (
        BackupHistory.query.filter_by(job_id=job_id, status="success")
        .filter(BackupHistory.finished_at.isnot(None), BackupHistory.archive_name.isnot(None))
        .order_by(BackupHistory.finished_at.desc()).limit(limit).all()
    )
    times = upload_times([r.archive_name for r in rows])
    backup_size = backup_time = upload_size = upload_time = 0
    for r in rows:
        if not r.size_bytes or not r.started_at:
            continue
        duration = (r.finished_at - r.started_at).total_seconds()
        if duration > 0:
            backup_size += r.size_bytes
            backup_time += duration
        uploaded = times.get(r.archive_name)
        if uploaded:
            delay = (uploaded - r.finished_at).total_seconds()
            if delay > 0:
                upload_size += r.size_bytes
                upload_time += delay
    return _mb_per_s(backup_size, backup_time), _mb_per_s(upload_size, upload_time)


def upload_limit_seconds(upload_avg):
    """Au-delà de ce délai sans confirmation, l'upload est considéré comme non confirmé."""
    cfg = alerts_cfg()
    minimum = cfg["upload_unconfirmed_min_minutes"] * 60
    return max(minimum, (upload_avg or 0) * cfg["upload_unconfirmed_factor"])


def snapshot(job, now=None):
    now = now or datetime.now()
    cfg = alerts_cfg()

    base = BackupHistory.query.filter_by(job_id=job.id)
    last_run = base.order_by(BackupHistory.started_at.desc()).first()
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
    running = bool(is_job_running(job.id) or (last_run and last_run.status == "running"))

    # --- sauvegarde en cours : temps écoulé / restant
    backup_avg = average_duration(job.id)
    backup_elapsed = backup_remaining = None
    backup_overdue = False
    if running and last_run and last_run.status == "running":
        backup_elapsed = max(0, int((now - last_run.started_at).total_seconds()))
        if backup_avg:
            backup_remaining = max(0, backup_avg - backup_elapsed)
            backup_overdue = backup_elapsed > backup_avg

    # --- source, archives, disque
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
    dest_free_percent = round(du.free / du.total * 100, 1) if du and du.total else None

    # --- tailles
    last_size = last_ok.size_bytes if last_ok else None
    prev_size = prev_ok.size_bytes if prev_ok else None
    size_drop = bool(
        last_size is not None and prev_size
        and (prev_size - last_size) / prev_size * 100 >= cfg["size_drop_percent"]
    )

    # --- upload de la dernière archive
    upload_avg = average_upload_delay(job.id)
    upload_limit = upload_limit_seconds(upload_avg)
    log_state, log_age = log_status()
    uploaded_at = failed_at = None
    upload_elapsed = upload_remaining = None
    upload_overdue = False
    last_delay = None

    if last_ok is None or not last_ok.archive_name:
        upload_state = "none"
    else:
        uploaded_at = upload_info(last_ok.archive_name)
        if uploaded_at:
            upload_state = "uploaded"
            if last_ok.finished_at:
                last_delay = round((uploaded_at - last_ok.finished_at).total_seconds())
        else:
            failed_at = upload_failure_info(last_ok.archive_name)
            if failed_at:
                upload_state = "failed"
            elif log_state != "ok":
                upload_state = "unknown"  # log illisible ou figé : impossible de savoir
            else:
                upload_elapsed = max(0, int((now - last_ok.finished_at).total_seconds())) if last_ok.finished_at else 0
                if upload_elapsed <= upload_limit:
                    upload_state = "pending"
                    if upload_avg:
                        upload_remaining = max(0, upload_avg - upload_elapsed)
                        upload_overdue = upload_elapsed > upload_avg
                else:
                    upload_state = "unconfirmed"

    backup_speed = None
    if last_ok and last_ok.finished_at and last_ok.started_at:
        backup_speed = _mb_per_s(last_ok.size_bytes, (last_ok.finished_at - last_ok.started_at).total_seconds())
    upload_speed = _mb_per_s(last_ok.size_bytes, last_delay) if last_ok and last_delay else None
    backup_speed_avg, upload_speed_avg = average_speeds(job.id)

    return {
        "now": now,
        "log_state": log_state, "log_age": log_age,
        "backup_speed": backup_speed, "upload_speed": upload_speed,
        "backup_speed_avg": backup_speed_avg, "upload_speed_avg": upload_speed_avg,
        "last_run": last_run, "last_ok": last_ok, "prev_ok": prev_ok, "last_fail": last_fail,
        "recent_failures": recent_failures,
        "next_run": ap_job.next_run_time if ap_job else None,
        "running": running,
        "backup_avg": backup_avg, "backup_elapsed": backup_elapsed,
        "backup_remaining": backup_remaining, "backup_overdue": backup_overdue,
        "source_status": source_status,
        "archive_count": archive_count, "archives_total": archives_total,
        "disk": du, "dest_free_percent": dest_free_percent,
        "last_size": last_size, "prev_size": prev_size, "size_drop": size_drop,
        "upload_state": upload_state,  # none / uploaded / pending / unconfirmed / failed
        "uploaded_at": uploaded_at, "failed_at": failed_at,
        "upload_avg": upload_avg, "upload_limit": round(upload_limit),
        "upload_elapsed": upload_elapsed, "upload_remaining": upload_remaining,
        "upload_overdue": upload_overdue, "last_delay": last_delay,
    }


def health(job, snap):
    """Retourne (statut, raisons)."""
    if not job.enabled:
        return "disabled", []

    cfg = alerts_cfg()
    critical, warning = [], []
    now = snap["now"]
    run = snap["last_run"]

    if run and run.status == "failed":
        critical.append("last backup failed")
    if snap["upload_state"] == "failed":
        critical.append("upload failed")
    if snap["source_status"] != "ok":
        critical.append("source disconnected")

    pct = snap["dest_free_percent"]
    if pct is not None:
        if pct < cfg["disk_critical_percent"]:
            critical.append(f"disk almost full ({pct}% free)")
        elif pct < cfg["disk_free_percent"]:
            warning.append(f"disk space low ({pct}% free)")

    if job.cron_expression != "manual":
        last_ok = snap["last_ok"]
        if last_ok is None:
            warning.append("no successful backup yet")
        elif last_ok.finished_at:
            age_hours = (now - last_ok.finished_at).total_seconds() / 3600
            if age_hours > cfg["max_age_hours"]:
                critical.append(f"no successful backup for {int(age_hours)} h")

    if snap["upload_state"] == "unconfirmed":
        warning.append("upload not confirmed")
    if snap["size_drop"]:
        warning.append("archive much smaller than the previous one")
    if snap["log_state"] == "unreadable":
        warning.append("OneDrive log unreadable (upload status unknown)")
    elif snap["log_state"] == "stale":
        warning.append("OneDrive log not updated recently (upload status unknown)")

    if critical:
        return "critical", critical + warning
    if warning:
        return "warning", warning
    return "ok", []
