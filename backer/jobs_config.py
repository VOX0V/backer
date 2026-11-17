"""
Configuration des sauvegardes depuis un fichier YAML (jobs.yml).

Le fichier vit dans le dossier de données (volume persistant). Il est lu au
démarrage et peut être modifié depuis l'éditeur web (/admin/jobs) : chaque
enregistrement est vérifié puis appliqué immédiatement, sans redémarrage.

Règles :
- fichier absent  -> aucune sauvegarde n'est planifiée ni affichée (la base
  n'est pas touchée), un message l'indique sur le dashboard ;
- fichier invalide au démarrage -> la dernière configuration valide (base)
  continue de tourner, les erreurs sont affichées ;
- sauvegarde retirée du fichier -> supprimée de Backer avec son historique
  (les archives sur le disque ne sont jamais touchées).
"""
import os
import posixpath
import re

import yaml

import settings_store
from extensions import db
from models import BackupJob
from scheduler_jobs import parse_cron_or_none, register_job, unregister_job
from utils import host_to_container_path, PathError

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
JOBS_FILE = os.environ.get("JOBS_FILE", os.path.join(DATA_DIR, "jobs.yml"))

# Noms de clés déjà utilisés au premier niveau de /api/status.
RESERVED_NAMES = {"backer", "onedrive_client", "offsite_storage", "connections"}
_ALLOWED_KEYS = {"name", "source", "dest", "cron", "retention", "enabled"}

EXAMPLE_YAML = """\
# Backer configuration. Edit this text and save: the file is checked, then
# applied immediately (no restart).
#
# staging_dir         (optional) folder where archives are built before being
#                     moved into each job's dest. Must be on the same disk as
#                     the dest folders and outside every source/dest folder.
#                     Keeps half-written files away from a sync client.
# offsite_storage_gb  (optional) total size of your online storage, in GB
#                     (1 GB = 1024^3 bytes, like the OneDrive client). Used to
#                     show the remaining space.
# alerts              (optional) thresholds of the health indicator and of the
#                     e-mail alerts; the defaults are shown in the commented
#                     block below. E-mail settings (Gmail...) go in the .env file.
#
# Jobs:
#
# name       unique name of the job (also used in archive file names)
# source     host path to back up, under a read-only mounted volume
# dest       host path where archives are written, under the writable volume
# cron       5 fields (minute hour day month weekday), or "manual" for no
#            automatic schedule (default if omitted: manual)
# retention  number of archives to keep (omit or null = unlimited)
# enabled    true or false (default if omitted: true)
#
# A job removed from this file is deleted from Backer together with its
# history. Archives already on disk are never touched.

staging_dir: /mnt/hdd/backer_tmp
offsite_storage_gb: 1029

# alerts:
#   max_age_hours: 36                  # critical if no successful backup for this long
#   size_drop_percent: 50              # warn if an archive loses at least this % of size
#   disk_free_percent: 10              # warn below this % of free space
#   disk_critical_percent: 5           # critical below this % of free space
#   upload_unconfirmed_factor: 3       # upload not confirmed after N x the average delay...
#   upload_unconfirmed_min_minutes: 10 # ...with this minimum

jobs:
  - name: DebServ
    source: /home/srv
    dest: /mnt/hdd/onedrive/backups/debserv
    cron: "0 3 * * *"
    retention: 30
    enabled: true

  - name: NetSec
    source: /mnt/netsec
    dest: /mnt/hdd/onedrive/backups/netsec
    cron: "0 4 * * *"
    retention: 30
    enabled: true

  - name: PiServ
    source: /mnt/piserv
    dest: /mnt/hdd/onedrive/backups/piserv
    cron: "0 5 * * *"
    retention: 30
    enabled: true
"""

# État courant du fichier : "missing" | "invalid" | "ok".
status = {"state": "missing", "errors": []}


def _safe_name(name):
    """Même nettoyage que pour les noms d'archives (deux noms qui donnent le
    même résultat mélangeraient leurs archives)."""
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or "backup"


def _inside(path, parent):
    """path est parent ou se trouve dedans (chemins hôte normalisés)."""
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _mount_root(container_path):
    """Racine de montage d'un chemin conteneur (/mnt/sources/<clé>)."""
    return "/".join(container_path.split("/")[:4])


def parse_jobs_yaml(text):
    """Vérifie le texte YAML. Retourne (jobs, settings, errors) : jobs est la
    liste normalisée, settings les réglages globaux (staging_dir,
    offsite_storage_gb), errors la liste de messages (vide si tout est valide)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [], {}, [f"Invalid YAML: {exc}"]

    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return [], {}, ["The file must contain a 'jobs:' list."]
    if not data["jobs"]:
        return [], {}, ["The 'jobs:' list is empty: define at least one job."]

    errors = []
    extra_top = set(data) - {"jobs", "staging_dir", "offsite_storage_gb", "alerts"}
    if extra_top:
        errors.append("Unknown top-level key(s): " + ", ".join(sorted(map(str, extra_top))))

    jobs = []
    seen_names = set()
    seen_safe = {}

    for idx, raw in enumerate(data["jobs"], start=1):
        label = f"Job #{idx}"
        if not isinstance(raw, dict):
            errors.append(f"{label}: must be a mapping (name, source, dest, ...).")
            continue

        name = raw.get("name")
        if isinstance(name, str) and name.strip():
            label = f"Job #{idx} ('{name.strip()}')"

        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            errors.append(f"{label}: unknown key(s): {', '.join(sorted(map(str, unknown)))}.")

        # --- name
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label}: 'name' is required (text).")
            continue
        name = name.strip()
        if len(name) > 120:
            errors.append(f"{label}: 'name' is too long (120 characters max).")
        if name.lower() in RESERVED_NAMES:
            errors.append(f"{label}: '{name}' is a reserved name.")
        if name in seen_names:
            errors.append(f"{label}: duplicate name.")
        seen_names.add(name)
        safe = _safe_name(name)
        if safe in seen_safe and seen_safe[safe] != name:
            errors.append(
                f"{label}: archive name clashes with '{seen_safe[safe]}' "
                f"(both become '{safe}')."
            )
        seen_safe.setdefault(safe, name)

        # --- source / dest
        source = raw.get("source")
        dest = raw.get("dest")
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{label}: 'source' is required (text).")
        else:
            source = source.strip()
            try:
                host_to_container_path(source)
            except PathError as exc:
                errors.append(f"{label}: source: {exc}")
        if not isinstance(dest, str) or not dest.strip():
            errors.append(f"{label}: 'dest' is required (text).")
        else:
            dest = dest.strip()
            try:
                host_to_container_path(dest, require_writable=True)
            except PathError as exc:
                errors.append(f"{label}: dest: {exc}")

        # --- cron
        cron = raw.get("cron")
        if cron is None:
            cron = "manual"
        elif not isinstance(cron, str):
            errors.append(f"{label}: 'cron' must be text, e.g. \"0 3 * * *\" or \"manual\".")
            cron = "manual"
        else:
            cron = cron.strip() or "manual"
            try:
                parse_cron_or_none(cron)
            except ValueError as exc:
                errors.append(f"{label}: invalid cron expression: {exc}")

        # --- retention
        retention = raw.get("retention")
        if retention is not None:
            if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
                errors.append(f"{label}: 'retention' must be a whole number >= 1 (or omitted for unlimited).")
                retention = None

        # --- enabled
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: 'enabled' must be true or false.")
            enabled = True

        jobs.append({
            "name": name, "source": source, "dest": dest, "cron": cron,
            "retention": retention, "enabled": enabled,
        })

    settings = {"staging_dir": None, "offsite_storage_gb": None}

    # --- staging_dir
    staging = data.get("staging_dir")
    if staging is not None:
        if not isinstance(staging, str) or not staging.strip():
            errors.append("'staging_dir' must be a folder path (text).")
        else:
            staging = posixpath.normpath(staging.strip())
            try:
                staging_c = host_to_container_path(staging, require_writable=True)
            except PathError as exc:
                errors.append(f"staging_dir: {exc}")
            else:
                settings["staging_dir"] = staging
                for j in jobs:
                    for key in ("source", "dest"):
                        other = j.get(key)
                        if not isinstance(other, str):
                            continue
                        other = posixpath.normpath(other)
                        if _inside(staging, other) or _inside(other, staging):
                            errors.append(
                                f"staging_dir overlaps with the {key} of '{j['name']}' ({other})."
                            )
                    dest = j.get("dest")
                    if not isinstance(dest, str):
                        continue
                    try:
                        dest_c = host_to_container_path(dest, require_writable=True)
                    except PathError:
                        continue
                    if _mount_root(staging_c) != _mount_root(dest_c):
                        errors.append(
                            f"staging_dir must be on the same disk as the dest of '{j['name']}'."
                        )
                    elif os.path.exists(staging_c) and os.path.exists(dest_c) \
                            and os.stat(staging_c).st_dev != os.stat(dest_c).st_dev:
                        errors.append(
                            f"staging_dir is on a different disk than the dest of '{j['name']}'."
                        )

    # --- offsite_storage_gb
    offsite = data.get("offsite_storage_gb")
    if offsite is not None:
        if isinstance(offsite, bool) or not isinstance(offsite, (int, float)) or offsite <= 0:
            errors.append("'offsite_storage_gb' must be a number greater than 0 (size in GB).")
        else:
            settings["offsite_storage_gb"] = float(offsite)

    # --- alerts (seuils du voyant de santé et des e-mails)
    alerts_cfg = dict(settings_store.DEFAULT_ALERTS)
    alerts = data.get("alerts")
    if alerts is not None:
        if not isinstance(alerts, dict):
            errors.append("'alerts' must be a mapping (see the comments at the top of the file).")
        else:
            for key, value in alerts.items():
                if key not in settings_store.DEFAULT_ALERTS:
                    errors.append(f"alerts: unknown key '{key}'.")
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                    errors.append(f"alerts.{key} must be a number greater than 0.")
                elif key.endswith("_percent") and value > 100:
                    errors.append(f"alerts.{key} cannot exceed 100.")
                else:
                    alerts_cfg[key] = value
    settings["alerts"] = alerts_cfg

    if errors:
        return [], {}, errors
    return jobs, settings, []


def read_jobs_file():
    with open(JOBS_FILE, "r", encoding="utf-8") as f:
        return f.read()


def write_jobs_file(text):
    """Écriture atomique : un fichier temporaire puis remplacement."""
    os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
    tmp = JOBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, JOBS_FILE)


def apply_jobs(jobs, app, settings=None):
    """Applique la liste (déjà vérifiée) : crée/met à jour les tâches, supprime
    celles qui ne sont plus dans le fichier (historique compris) et
    réenregistre la planification. À appeler dans un contexte d'application.
    Retourne la liste des noms supprimés."""
    names = {j["name"] for j in jobs}

    for j in jobs:
        row = BackupJob.query.filter_by(name=j["name"]).first()
        if row is None:
            row = BackupJob(name=j["name"])
            db.session.add(row)
        row.source_path = j["source"]
        row.dest_path = j["dest"]
        row.cron_expression = j["cron"]
        row.retention_count = j["retention"]
        row.enabled = j["enabled"]
        row.env_managed = False

    removed = []
    for row in BackupJob.query.all():
        if row.name not in names:
            removed.append(row.name)
            unregister_job(row.id)
            db.session.delete(row)  # supprime aussi l'historique (cascade)
    db.session.commit()

    for row in BackupJob.query.all():
        register_job(row, app)  # retire la planification si désactivée

    if settings is not None:
        settings_store.settings.update(settings)

    status.update(state="ok", errors=[])
    return removed


def _register_existing(app):
    """Replanifie les tâches actuellement en base (dernière config valide)."""
    for row in BackupJob.query.filter_by(enabled=True).all():
        register_job(row, app)


def init_jobs(app):
    """Au démarrage (contexte d'application, scheduler déjà démarré)."""
    if not os.path.exists(JOBS_FILE):
        status.update(state="missing", errors=[])
        return

    try:
        text = read_jobs_file()
    except OSError as exc:
        status.update(state="invalid", errors=[f"Cannot read {JOBS_FILE}: {exc}"])
        _register_existing(app)
        return

    jobs, settings, errors = parse_jobs_yaml(text)
    if errors:
        status.update(state="invalid", errors=errors)
        _register_existing(app)
        return

    apply_jobs(jobs, app, settings)
