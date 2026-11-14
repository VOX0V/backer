"""
Surveillance du log du client OneDrive (abraunegg/onedrive).

Lit les nouvelles lignes du fichier de log (montage en lecture seule) et
retient, pour chaque archive .zip, l'heure de la ligne
« Uploading new file: <chemin> ... done ». La confirmation est gardée en
base (table onedrive_uploads) : elle survit à la coupure ou à la rotation
du log. Retient aussi l'heure du dernier « Sync with Microsoft OneDrive is complete ».

Fonctions réutilisables par l'API Homepage et, plus tard, par le dashboard :
scan_log() et upload_info().
"""
import os
import re
import threading
from datetime import datetime

from extensions import db
from models import OneDriveUpload

LOG_FILE = os.environ.get("ONEDRIVE_LOG_FILE", "/onedrive-log/onedrive.onedrive.log")

# Ex : [2026-Nov-14 10:04:45.9854377] Uploading new file: ./backups/netsec/X.zip ... done
_LINE_RE = re.compile(r"^\[(\d{4})-([A-Za-z]{3})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+\] (.*)$")
_UPLOAD_DONE_RE = re.compile(r"^Uploading new file: (.+) \.\.\. done$")
_SYNC_COMPLETE = "Sync with Microsoft OneDrive is complete"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_lock = threading.Lock()
_state = {"offset": 0, "inode": None, "last_sync": None, "readable": False}


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
        last_sync = None
        for raw in data[:end].decode("utf-8", errors="replace").splitlines():
            m = _LINE_RE.match(raw)
            if not m:
                continue
            ts = _parse_ts(m)
            if ts is None:
                continue
            msg = m.group(7)
            if msg == _SYNC_COMPLETE:
                last_sync = ts
                continue
            u = _UPLOAD_DONE_RE.match(msg)
            if u:
                name = os.path.basename(u.group(1))
                if name.endswith(".zip"):
                    uploads.append((name, ts))

        try:
            for name, ts in uploads:
                row = OneDriveUpload.query.filter_by(archive_name=name).first()
                if row:
                    row.uploaded_at = ts
                else:
                    db.session.add(OneDriveUpload(archive_name=name, uploaded_at=ts))
            if uploads:
                db.session.commit()
        except Exception:
            # Ex. course entre deux processus : on ne fait pas avancer la position,
            # la prochaine lecture réessaiera.
            db.session.rollback()
            return True

        _state["offset"] += end + 1
        if last_sync is not None:
            _state["last_sync"] = last_sync
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


def monitor_state():
    """État du moniteur : log lisible ? date de la dernière synchro terminée."""
    return {"readable": _state["readable"], "last_sync": _state["last_sync"]}
