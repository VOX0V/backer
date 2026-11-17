"""
Alertes e-mail (une seule fois par incident), vérifiées périodiquement.

Quatre cas : sauvegarde en échec, upload non confirmé (ou en échec), archive
dont la taille chute fortement, disque de destination presque plein.

Au tout premier passage, un repère de date est enregistré : seuls les
événements postérieurs à ce repère déclenchent un e-mail, pour ne pas
signaler d'anciens échecs déjà connus.
"""
from datetime import datetime

import jobs_config
import monitoring
import notifier
from extensions import db
from models import AlertState, BackupJob
from onedrive_monitor import scan_log, log_status
from utils import format_size


def _baseline():
    row = AlertState.query.filter_by(key="baseline").first()
    if row is None:
        row = AlertState(key="baseline", active=False, created_at=datetime.now())
        db.session.add(row)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            row = AlertState.query.filter_by(key="baseline").first()
    return row.created_at


def _send_once(key, subject, body):
    if AlertState.query.filter_by(key=key).first():
        return
    ok, error = notifier.send_email(subject, body)
    if not ok:
        print(f"[Backer] e-mail alert not sent ({key}): {error}")
        return
    try:
        db.session.add(AlertState(key=key, active=True, created_at=datetime.now()))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _check_disk(job, snap):
    pct = snap["dest_free_percent"]
    if pct is None:
        return
    low = pct < monitoring.alerts_cfg()["disk_free_percent"]
    key = f"disk:{job.name}"
    row = AlertState.query.filter_by(key=key).first()

    if low and (row is None or not row.active):
        du = snap["disk"]
        ok, error = notifier.send_email(
            f"[Backer] {job.name}: destination disk almost full",
            f"The destination disk of '{job.name}' is almost full: {pct}% free "
            f"({format_size(du.free)} free of {format_size(du.total)}).\n",
        )
        if not ok:
            print(f"[Backer] e-mail alert not sent ({key}): {error}")
            return
        try:
            if row is None:
                db.session.add(AlertState(key=key, active=True, created_at=datetime.now()))
            else:
                row.active = True
            db.session.commit()
        except Exception:
            db.session.rollback()
    elif not low and row is not None and row.active:
        row.active = False
        db.session.commit()


def _check_log():
    """Alerte quand le log OneDrive devient illisible ou figé (une seule fois par incident)."""
    state, age = log_status()
    bad = state != "ok"
    key = "onedrive_log"
    row = AlertState.query.filter_by(key=key).first()

    if bad and (row is None or not row.active):
        if state == "unreadable":
            detail = "Backer cannot read the OneDrive log file."
        else:
            detail = f"The OneDrive log has not been updated for {age // 60} minutes."
        ok, error = notifier.send_email(
            "[Backer] OneDrive log unavailable",
            f"{detail}\n\nUpload confirmations are suspended until the log is available again. "
            "Check that the OneDrive client is running and that the log folder mounted in Backer "
            "still exists (recreate the Backer container if the folder was deleted and recreated).\n",
        )
        if not ok:
            print(f"[Backer] e-mail alert not sent ({key}): {error}")
            return
        try:
            if row is None:
                db.session.add(AlertState(key=key, active=True, created_at=datetime.now()))
            else:
                row.active = True
            db.session.commit()
        except Exception:
            db.session.rollback()
    elif not bad and row is not None and row.active:
        row.active = False
        db.session.commit()


def check_alerts():
    """À appeler dans un contexte d'application."""
    if not notifier.is_configured() or jobs_config.status["state"] == "missing":
        return
    baseline = _baseline()
    now = datetime.now()
    scan_log()
    _check_log()

    for job in BackupJob.query.filter_by(enabled=True).all():
        snap = monitoring.snapshot(job, now)

        run = snap["last_run"]
        if run and run.status == "failed" and run.finished_at and run.finished_at > baseline:
            _send_once(
                f"backup_failed:{run.id}",
                f"[Backer] {job.name}: backup failed",
                f"Backup '{job.name}' failed.\n\nStarted: {run.started_at:%Y-%m-%d %H:%M:%S}\n"
                f"Message: {run.message}\n",
            )

        ok = snap["last_ok"]
        if ok and ok.finished_at and ok.finished_at > baseline and ok.archive_name:
            if snap["upload_state"] in ("unconfirmed", "failed"):
                _send_once(
                    f"upload:{ok.archive_name}",
                    f"[Backer] {job.name}: upload not confirmed",
                    f"The archive {ok.archive_name} of '{job.name}' is not confirmed on the offsite storage.\n"
                    f"Backup finished at {ok.finished_at:%Y-%m-%d %H:%M:%S}.\n"
                    f"State: {snap['upload_state']}\n",
                )
            if snap["size_drop"]:
                _send_once(
                    f"size:{ok.archive_name}",
                    f"[Backer] {job.name}: archive much smaller than the previous one",
                    f"The latest archive of '{job.name}' ({format_size(snap['last_size'])}) is much smaller "
                    f"than the previous one ({format_size(snap['prev_size'])}).\n"
                    "Check that the source is mounted and complete.\n",
                )

        _check_disk(job, snap)


def check_alerts_job(app):
    """Appelé périodiquement par APScheduler."""
    with app.app_context():
        try:
            check_alerts()
        except Exception as exc:  # noqa: BLE001
            print(f"[Backer] alert check failed: {exc}")
