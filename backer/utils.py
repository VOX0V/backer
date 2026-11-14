"""
Traduction chemin-hôte -> chemin-conteneur (basée sur les volumes montés),
et exécution effective d'une sauvegarde (zip natif + fichier log).
"""
import os
import re
import subprocess
from datetime import datetime

# clé -> chemin réel sur l'hôte / chemin monté dans le conteneur / écriture autorisée
MOUNT_ROOTS = {
    "srv":     {"host_path": "/home/srv",   "container_path": "/mnt/sources/srv",     "writable": False},
    "netsec":  {"host_path": "/mnt/netsec", "container_path": "/mnt/sources/netsec",  "writable": False},
    "piserv":  {"host_path": "/mnt/piserv", "container_path": "/mnt/sources/piserv",  "writable": False},
    "ssd":     {"host_path": "/mnt/ssd",    "container_path": "/mnt/sources/ssd",     "writable": True},
}


def format_size(num_bytes):
    """Formate un nombre d'octets en Ko/Mo/Go lisible (pour le fichier .log,
    qui n'a pas accès au filtre Jinja filesizeformat)."""
    if num_bytes is None:
        return "n/a"
    size = float(num_bytes)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024.0:
            return f"{int(size)} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} Po"


_JOURS_CRON = {
    "0": "dimanche", "1": "lundi", "2": "mardi", "3": "mercredi",
    "4": "jeudi", "5": "vendredi", "6": "samedi", "7": "dimanche",
    "sun": "dimanche", "mon": "lundi", "tue": "mardi", "wed": "mercredi",
    "thu": "jeudi", "fri": "vendredi", "sat": "samedi",
}


def translate_cron(cron_expression):
    """
    Traduit une expression cron courante en phrase française lisible.
    Ne couvre que les cas fréquents (quotidien, hebdo, toutes les N minutes/
    heures, mensuel) -- retombe sur l'expression brute si non reconnue,
    plutôt que d'afficher une traduction incorrecte.
    """
    if not cron_expression or cron_expression.strip().lower() == "manual":
        return "Manuel (pas de planification automatique)"

    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return cron_expression

    minute, hour, day, month, dow = parts

    try:
        if minute.isdigit() and hour.isdigit() and day == "*" and month == "*" and dow == "*":
            return f"Tous les jours à {int(hour):02d}h{int(minute):02d}"

        if minute.isdigit() and hour.isdigit() and day == "*" and month == "*" and dow != "*":
            jour = _JOURS_CRON.get(dow.lower(), dow)
            return f"Chaque {jour} à {int(hour):02d}h{int(minute):02d}"

        if minute.startswith("*/") and hour == "*" and day == "*" and month == "*" and dow == "*":
            return f"Toutes les {int(minute[2:])} minutes"

        if minute.isdigit() and hour.startswith("*/") and day == "*" and month == "*" and dow == "*":
            return f"Toutes les {int(hour[2:])} heures (à la minute {int(minute):02d})"

        if minute.isdigit() and hour.isdigit() and day.isdigit() and month == "*" and dow == "*":
            return f"Le {int(day)} de chaque mois à {int(hour):02d}h{int(minute):02d}"
    except ValueError:
        pass

    return cron_expression  # non reconnu -- on montre l'expression brute plutôt qu'une traduction fausse


class PathError(ValueError):
    pass


def check_mount_status(container_path):
    """
    Vérifie l'accessibilité d'un chemin monté : 'ok' si le dossier existe et
    contient au moins une entrée, 'empty' s'il existe mais est vide (signe
    fréquent d'un partage réseau démonté sans que Docker le remarque),
    'error' s'il est carrément inaccessible.
    """
    try:
        entries = os.listdir(container_path)
    except (FileNotFoundError, PermissionError, OSError):
        return "error"
    return "ok" if entries else "empty"


def host_to_container_path(host_path, require_writable=False):
    """
    Prend un chemin hôte complet (ex: /home/srv/sys/jellyfin, tel qu'écrit
    dans le .env) et retourne le chemin réel correspondant à l'intérieur du
    conteneur, en se basant sur les volumes montés dans MOUNT_ROOTS.
    Lève PathError si le chemin ne tombe sous aucune racine montée, ou si
    require_writable=True et que la racine trouvée est en lecture seule.
    """
    normalized = os.path.normpath(host_path or "")

    for key, cfg in MOUNT_ROOTS.items():
        root_host = os.path.normpath(cfg["host_path"])
        if normalized == root_host or normalized.startswith(root_host + os.sep):
            if require_writable and not cfg["writable"]:
                raise PathError(f"'{host_path}' est sous une racine en lecture seule ({root_host})")
            remainder = normalized[len(root_host):].lstrip(os.sep)
            container_path = os.path.normpath(os.path.join(cfg["container_path"], remainder))
            return container_path

    raise PathError(f"'{host_path}' n'est couvert par aucun volume monté (voir docker-compose.yml)")


def run_backup(source_container_path, dest_container_dir, job_name):
    """
    Zippe le contenu de source_container_path dans dest_container_dir, avec
    un nom horodaté, et écrit un fichier .log à côté de l'archive.

    Un code de retour non-zéro de `zip` (fichiers spéciaux type sockets,
    permissions refusées sur certains fichiers, etc.) n'est PAS traité comme
    un échec tant qu'une archive non vide a bien été produite -- ces cas
    sont fréquents en zippant un système de fichiers vivant, et l'archive
    reste utilisable. Les avertissements sont consignés dans le log.

    Retourne un dict : status, message, archive_name, log_name, size_bytes
    """
    if not os.path.isdir(source_container_path):
        return {
            "status": "failed",
            "message": f"Dossier source introuvable : {source_container_path}",
            "archive_name": None, "log_name": None, "size_bytes": None,
        }

    os.makedirs(dest_container_dir, exist_ok=True)

    safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", job_name).strip("_") or "backup"
    started_at = datetime.now()
    # Timestamp avec secondes -- réduit fortement le risque de collision.
    stamp = started_at.strftime("%Y_%m_%d-%H%M%S")
    archive_name = f"{safe_name}-{stamp}.zip"
    log_name = f"{safe_name}-{stamp}.log"
    archive_path = os.path.join(dest_container_dir, archive_name)
    log_path = os.path.join(dest_container_dir, log_name)

    # Garde-fou supplémentaire : si le nom existe déjà malgré tout (deux
    # exécutions dans la même seconde), on ajoute un suffixe plutôt que
    # d'écraser une archive existante.
    suffix = 2
    while os.path.exists(archive_path):
        archive_name = f"{safe_name}-{stamp}_{suffix}.zip"
        log_name = f"{safe_name}-{stamp}_{suffix}.log"
        archive_path = os.path.join(dest_container_dir, archive_name)
        log_path = os.path.join(dest_container_dir, log_name)
        suffix += 1

    cmd = ["zip", "-rq", archive_path, "."]
    try:
        result = subprocess.run(
            cmd, cwd=source_container_path,
            capture_output=True, text=True, timeout=6 * 3600,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failed", "message": "Délai dépassé lors de la création de l'archive",
            "archive_name": None, "log_name": None, "size_bytes": None,
        }
    except FileNotFoundError:
        return {
            "status": "failed", "message": "Binaire 'zip' introuvable dans le conteneur",
            "archive_name": None, "log_name": None, "size_bytes": None,
        }

    finished_at = datetime.now()
    archive_exists = os.path.exists(archive_path)
    size_bytes = os.path.getsize(archive_path) if archive_exists else None

    if archive_exists and size_bytes and size_bytes > 0:
        status = "success"
        if result.returncode == 0:
            message = "OK"
        else:
            message = f"OK avec avertissements (code zip {result.returncode}) -- voir le fichier log"
    else:
        status = "failed"
        message = f"zip a échoué (code {result.returncode}) : {result.stderr[-500:]}"
        archive_name = None

    _write_log_file(
        log_path, job_name=job_name, started_at=started_at, finished_at=finished_at,
        status=status, size_bytes=size_bytes, source=source_container_path,
        zip_returncode=result.returncode, zip_stderr=result.stderr,
    )

    return {
        "status": status, "message": message,
        "archive_name": archive_name,
        "log_name": log_name if archive_exists else None,
        "size_bytes": size_bytes,
    }


def _write_log_file(log_path, job_name, started_at, finished_at, status, size_bytes,
                     source, zip_returncode, zip_stderr):
    duration = (finished_at - started_at).total_seconds()
    lines = [
        f"Job : {job_name}",
        f"Source : {source}",
        f"Début : {started_at.isoformat(timespec='seconds')}",
        f"Fin : {finished_at.isoformat(timespec='seconds')}",
        f"Durée : {duration:.1f} s",
        f"Statut : {status}",
        f"Taille : {format_size(size_bytes)}" if size_bytes else "Taille : n/a",
        f"Code retour zip : {zip_returncode}",
    ]
    if zip_stderr and zip_stderr.strip():
        lines.append("")
        lines.append("--- Avertissements / erreurs zip ---")
        lines.append(zip_stderr.strip())

    try:
        with open(log_path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass  # ne jamais faire échouer le job à cause du log lui-même


def append_to_log_file(dest_container_dir, log_name, lines):
    if not log_name or not lines:
        return
    log_path = os.path.join(dest_container_dir, log_name)
    try:
        with open(log_path, "a") as f:
            f.write("\n" + "\n".join(lines) + "\n")
    except OSError:
        pass


def _safe_job_prefix(job_name):
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", job_name).strip("_") or "backup"


def list_job_archives(dest_container_dir, job_name):
    """
    Liste les archives .zip appartenant à cette tâche dans le dossier
    destination (identifiées par le préfixe du nom), triées de la plus
    récente à la plus vieille. Retourne aussi la taille totale.
    """
    prefix = _safe_job_prefix(job_name) + "-"
    archives = []
    try:
        entries = os.scandir(dest_container_dir)
    except FileNotFoundError:
        return [], 0

    for entry in entries:
        if entry.is_file() and entry.name.startswith(prefix) and entry.name.endswith(".zip"):
            stat = entry.stat()
            log_name = entry.name[:-4] + ".log"
            archives.append({
                "name": entry.name,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime),
                "log_name": log_name if os.path.exists(os.path.join(dest_container_dir, log_name)) else None,
            })

    archives.sort(key=lambda a: a["mtime"], reverse=True)
    total_size = sum(a["size_bytes"] for a in archives)
    return archives, total_size


def enforce_retention(dest_container_dir, job_name, retention_count, log_lines=None):
    """Supprime les archives les plus vieilles de cette tâche au-delà de
    retention_count. Ne fait rien si retention_count est None (illimité)."""
    if not retention_count or retention_count < 1:
        return

    archives, _ = list_job_archives(dest_container_dir, job_name)
    to_delete = archives[retention_count:]  # les plus vieilles, au-delà de la limite
    for archive in to_delete:
        zip_path = os.path.join(dest_container_dir, archive["name"])
        try:
            os.remove(zip_path)
            if log_lines is not None:
                log_lines.append(f"Rétention : archive supprimée ({archive['name']})")
        except OSError as exc:
            if log_lines is not None:
                log_lines.append(f"Rétention : échec de suppression de {archive['name']} ({exc})")
            continue
        if archive["log_name"]:
            log_path = os.path.join(dest_container_dir, archive["log_name"])
            try:
                os.remove(log_path)
            except OSError:
                pass


def delete_archive(dest_container_dir, archive_name):
    """Supprime manuellement une archive précise (+ son .log s'il existe).
    Valide que archive_name est un nom de fichier simple (pas de chemin)."""
    safe_name = os.path.basename(archive_name)
    if safe_name != archive_name or not safe_name.endswith(".zip"):
        raise PathError("Nom d'archive invalide")

    zip_path = os.path.join(dest_container_dir, safe_name)
    if not os.path.isfile(zip_path):
        raise PathError("Archive introuvable")
    os.remove(zip_path)

    log_path = os.path.join(dest_container_dir, safe_name[:-4] + ".log")
    if os.path.isfile(log_path):
        os.remove(log_path)


def read_log_file(dest_container_dir, log_name):
    """Lit le contenu d'un fichier .log précis, avec validation du nom."""
    safe_name = os.path.basename(log_name)
    if safe_name != log_name or not safe_name.endswith(".log"):
        raise PathError("Nom de log invalide")

    log_path = os.path.join(dest_container_dir, safe_name)
    if not os.path.isfile(log_path):
        raise PathError("Log introuvable")
    with open(log_path, "r") as f:
        return f.read()
