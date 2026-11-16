"""
Réglages globaux lus dans jobs.yml (hors liste des tâches).

Module volontairement sans dépendance : il est lu par scheduler_jobs, monitoring
et app, et rempli par jobs_config, sans créer d'import circulaire.
"""

# Seuils par défaut du voyant de santé et des alertes e-mail (bloc `alerts:` de jobs.yml).
DEFAULT_ALERTS = {
    "max_age_hours": 36,                 # critique si aucune réussite depuis ce nombre d'heures
    "size_drop_percent": 50,             # alerte si l'archive perd au moins ce % de taille
    "disk_free_percent": 10,             # alerte si moins de ce % d'espace libre
    "disk_critical_percent": 5,          # critique sous ce % d'espace libre
    "upload_unconfirmed_factor": 3,      # upload non confirmé après N fois le délai moyen...
    "upload_unconfirmed_min_minutes": 10,  # ...avec ce minimum (minutes)
}

settings = {
    "staging_dir": None,          # chemin hôte du dossier temporaire, ou None
    "offsite_storage_gb": None,   # taille totale du stockage en ligne (Go), ou None
    "alerts": dict(DEFAULT_ALERTS),
}
