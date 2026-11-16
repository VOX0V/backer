"""
Réglages globaux lus dans jobs.yml (hors liste des tâches).

Module volontairement sans dépendance : il est lu par scheduler_jobs et par
app, et rempli par jobs_config, sans créer d'import circulaire.
"""

settings = {
    "staging_dir": None,          # chemin hôte du dossier temporaire, ou None
    "offsite_storage_gb": None,   # taille totale du stockage en ligne (Go), ou None
}
