"""
Surveillance du log du client OneDrive (abraunegg/onedrive).

Lit les nouvelles lignes du fichier de log (montage en lecture seule) et
retient :
- pour chaque archive .zip, l'heure de la ligne
  « Uploading new file: <chemin> ... done » (gardée en base, table
  onedrive_uploads : elle survit à la coupure ou à la rotation du log) ;
- en mémoire, les derniers événements utiles : fin de synchronisation,
  contact avec le service Microsoft, éléments traités, dernière suppression
  côté OneDrive, écart d'horloge, espace libre restant (affiché par le
  client à son démarrage en mode verbeux).

Fonctions réutilisables par l'API et, plus tard, par le dashboard :
scan_log(), upload_info(), upload_times(), monitor_state().
"""
import os
import re
import threading
from datetime import datetime

from extensions import db
from models import OneDriveUpload, OneDriveUploadFailure

LOG_FILE = os.environ.get("ONEDRIVE_LOG_FILE", "/onedrive-log/onedrive.onedrive.log")

# Ex : [2026-Nov-14 10:04:45.9854377] Uploading new file: ./backups/netsec/X.zip ... done
_LINE_RE = re.compile(r"^\[(\d{4})-([A-Za-z]{3})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+\] (.*)$")
_UPLOAD_DONE_RE = re.compile(r"^Uploading new file: (.+) \.\.\. done$")
_UPLOAD_FAILED_RE = re.compile(r"^Uploading new file: (.+) \.\.\. failed!$")
_FAILED_TO_UPLOAD_RE = re.compile(r"^Failed to upload: (.+)$")
_SYNC_COMPLETE = "Sync with Microsoft OneDrive is complete"
_REACH = "Successfully reached the Microsoft OneDrive Service"
_ITEMS_RE = re.compile(r"^Processed (\d+) applicable JSON items received from Microsoft OneDrive")
_DELETED_RE = re.compile(r"^Successfully deleted item from Microsoft OneDrive: (.+)$")
_SKEW_RE = re.compile(r"^Observed clock difference\s*=\s*([+-]?[\d.]+) seconds")
_FREE_RE = re.compile(r"^Remaining Free Space:\s+[\d.]+\s*[A-Za-z]+\s+\((\d+) bytes\)")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_lock = threading.Lock()
_state = {
    "offset": 0, "inode": None, "readable": False,
    "last_sync": None, "last_reach": None,
    "last_items": None,         # {"count": int, "at": datetime}
    "last_deletion": None,      # {"path": str, "at": datetime}
    "clock_skew_seconds": None,
    "free_space": None,         # {"bytes": int, "at": datetime}
}


def _parse_ts(m):
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    return datetime(int(m.group(1)), month, int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)))


def scan_log():
    """Lit les nouvelles lignes du log et enregistre les uploads terminés.
    À appeler dans un contexte d'application Flask.
    Retourne True si le log est lisible, False sinon."""
    with _lock:
        try:
            st = os.stat(LOG_FILE)
            # Fichier remplacé ou coupé : on relit depuis le début.
            if st.st_ino != _state["inode"] or st.st_size < _state["offset"]:
                _state["offset"] = 0
                _state["inode"] = st.st_ino
            with open(LOG_FILE, "rb") as f:
                f.seek(_state["offset"])
                data = f.read()
        except OSError:
            _state["readable"] = False
            return False

        _state["readable"] = True

        # Seules les lignes complètes sont traitées (la dernière peut être en cours d'écriture).
        end = data.rfind(b"\n")
        if end == -1:
            return True

        uploads = []
        failures = []
        updates = {}
        for raw in data[:end].decode("utf-8", errors="replace").splitlines():
            m = _LINE_RE.match(raw)
            if not m:
                continue
            ts = _parse_ts(m)
            if ts is None:
                continue
            msg = m.group(7)

            if msg == _SYNC_COMPLETE:
                updates["last_sync"] = ts
                continue
            if msg == _REACH:
                updates["last_reach"] = ts
                continue

            u = _UPLOAD_DONE_RE.match(msg)
            if u:
                name = os.path.basename(u.group(1))
                if name.endswith(".zip"):
                    uploads.append((name, ts))
                continue

            f1 = _UPLOAD_FAILED_RE.match(msg) or _FAILED_TO_UPLOAD_RE.match(msg)
            if f1:
                fname = os.path.basename(f1.group(1))
                if fname.endswith(".zip"):  # les fichiers temporaires de zip sont ignorés
                    failures.append((fname, ts))
                continue

            i = _ITEMS_RE.match(msg)
            if i:
                updates["last_items"] = {"count": int(i.group(1)), "at": ts}
                continue

            d = _DELETED_RE.match(msg)
            if d:
                updates["last_deletion"] = {"path": d.group(1), "at": ts}
                continue

            s = _SKEW_RE.match(msg)
            if s:
                try:
                    updates["clock_skew_seconds"] = float(s.group(1))
                except ValueError:
                    pass
                continue

            fr = _FREE_RE.match(msg)
            if fr:
                updates["free_space"] = {"bytes": int(fr.group(1)), "at": ts}

        try:
            for name, ts in failures:
                done = OneDriveUpload.query.filter_by(archive_name=name).first()
                if done and done.uploaded_at >= ts:
                    continue
                row = OneDriveUploadFailure.query.filter_by(archive_name=name).first()
                if row:
                    row.failed_at = ts
                else:
                    db.session.add(OneDriveUploadFailure(archive_name=name, failed_at=ts))
            for name, ts in uploads:
                row = OneDriveUpload.query.filter_by(archive_name=name).first()
                if row:
                    row.uploaded_at = ts
                else:
                    db.session.add(OneDriveUpload(archive_name=name, uploaded_at=ts))
                failed = OneDriveUploadFailure.query.filter_by(archive_name=name).first()
                if failed and failed.failed_at <= ts:
                    db.session.delete(failed)  # un envoi réussi annule l'échec précédent
            if uploads or failures:
                db.session.commit()
        except Exception:
            # Ex. course entre deux processus : on ne fait pas avancer la position,
            # la prochaine lecture réessaiera.
            db.session.rollback()
            return True

        _state["offset"] += end + 1
        _state.update(updates)
        return True


def scan_log_job(app):
    """Appelé périodiquement par APScheduler."""
    with app.app_context():
        scan_log()


def upload_info(archive_name):
    """Date d'upload confirmée (datetime) pour cette archive, ou None."""
    if not archive_name:
        return None
    row = OneDriveUpload.query.filter_by(archive_name=archive_name).first()
    return row.uploaded_at if row else None


def upload_failure_info(archive_name):
    """Date de l'échec d'envoi (datetime) pour cette archive, ou None."""
    if not archive_name:
        return None
    row = OneDriveUploadFailure.query.filter_by(archive_name=archive_name).first()
    return row.failed_at if row else None


def upload_times(archive_names):
    """Dictionnaire {nom d'archive: date d'upload} pour les archives confirmées."""
    names = [n for n in archive_names if n]
    if not names:
        return {}
    rows = OneDriveUpload.query.filter(OneDriveUpload.archive_name.in_(names)).all()
    return {r.archive_name: r.uploaded_at for r in rows}


def monitor_state():
    """Copie de l'état du moniteur (log lisible, derniers événements)."""
    with _lock:
        return {k: _state[k] for k in (
            "readable", "last_sync", "last_reach", "last_items",
            "last_deletion", "clock_skew_seconds", "free_space",
        )}
