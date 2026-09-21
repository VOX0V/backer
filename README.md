# Backer

Interface web pour gérer des jobs de sauvegarde (zip natif Linux) sur un
serveur Linux, à la place de scripts shell manuels.

## Démarrage rapide (nouvelle installation)

```bash
git clone https://github.com/<pseudo>/backer.git
cd backer
cp .env.example .env
nano .env   # remplir ADMIN_USERNAME/PASSWORD et tes BACKN_*
docker compose up -d
```

Le premier démarrage crée le compte admin défini dans `.env` (si aucun
utilisateur n'existe déjà) et synchronise les jobs de sauvegarde décrits par
les blocs `BACKN_*`.

## Comment ça marche

Les jobs sont définis dans le `.env` (nom / source / destination), **pas**
via l'interface web. La planification (cron) se change ensuite depuis le
dashboard et est conservée en base — elle survit aux redémarrages du
conteneur.

## Format des jobs dans le .env

```
BACK1_NAME=DebServ
BACK1_SOURCE=/home/srv
BACK1_DEST=/mnt/hdd/onedrive/backups/debserv
BACK1_CRON=0 3 * * *
```

- `SOURCE` doit être sous un des volumes montés en lecture seule (voir
  `docker-compose.yml` — par défaut `/home/srv`, `/mnt/netsec`,
  `/mnt/piserv`, à adapter à ton infra).
- `DEST` doit être sous `/mnt/hdd` (seule racine montée en écriture par
  défaut dans `docker-compose.yml`) — adapte ce chemin et le volume associé
  si un service de synchronisation cloud (OneDrive, rclone, ...) surveille
  déjà un dossier particulier chez toi.
- `CRON` est optionnel, au format standard 5 champs (`minute heure jour mois
  jour_semaine`) — génère-le sur crontab.guru si besoin. Il ne sert qu'à
  l'**initialisation** de la tâche : une fois créée, modifier `BACK1_CRON`
  dans le `.env` n'a plus d'effet, la planification se change depuis le
  dashboard. Utilise `manual` pour une tâche sans planification automatique
  (bouton « Lancer » uniquement).
- Si un bloc `BACKN_*` est retiré du `.env`, la tâche correspondante est
  **désactivée** au redémarrage suivant (pas supprimée) — son historique
  reste consultable.

## Fonctionnement du dashboard

- **Lancer** exécute la sauvegarde en arrière-plan (ne bloque pas la page) ;
  le dashboard se rafraîchit automatiquement toutes les 5s tant qu'une tâche
  est en cours, pour voir l'état évoluer sans recharger manuellement.
- **Cron** et **Conserver** (rétention) sont éditables directement sur
  chaque ligne, en base — persistent aux redémarrages.
- **Rétention** : nombre d'archives à garder par tâche. Une fois la limite
  atteinte, les plus vieilles sont supprimées automatiquement après chaque
  sauvegarde réussie (archive + son `.log`). Champ vide = illimité.
- Le nom du dernier log est cliquable → page en texte brut avec le détail
  de l'exécution (durée, taille, avertissements zip éventuels).
- **Historique** (bouton par tâche) : liste toutes les archives conservées
  (nom, date, taille), avec suppression manuelle possible par archive.
- Taille totale affichée par tâche et globalement en haut du dashboard.

## Sécurité

- Le conteneur tourne en utilisateur non-root (UID/GID 1000:1000) et n'a
  accès qu'à des chemins précis, montés explicitement.
- Sources montées en **lecture seule** : Backer ne peut jamais les modifier
  ou les effacer, seulement les lire pour créer une archive.
- Une seule racine montée en écriture, utilisée comme destination des
  sauvegardes.
- `SECRET_KEY` générée aléatoirement au premier démarrage et persistée dans
  `data/secret_key` (permissions 600) ; la base SQLite (`data/backer.db`,
  qui contient les hachages de mots de passe) reçoit les mêmes permissions
  restrictives.
- Comptes multi-utilisateurs, mots de passe hachés en PBKDF2/SHA-256,
  comparaison en temps constant, hachage factice systématique sur `/login`
  quand le compte n'existe pas (anti-énumération par timing).
- Bannissement d'IP : 3 échecs de connexion → blocage de l'IP pendant 1h
  (visible et levable manuellement depuis `/admin`). L'IP cliente est
  résolue via `CF-Connecting-IP` (Cloudflare) puis le premier maillon de
  `X-Forwarded-For`, jamais depuis une IP d'infrastructure codée en dur.
- Révocation de session : désactiver un compte invalide immédiatement toute
  session déjà ouverte pour ce compte.
- Cookie de session : `Secure` (si `CONNECTION_TYPE=https`), `HttpOnly`,
  `SameSite=Lax`.
- Taille de requête plafonnée (64 Ko) pour éviter un déni de service basique
  par `Content-Length` mensonger.
- `X-Frame-Options: DENY` + `Content-Security-Policy: frame-ancestors 'none'`
  + `Cache-Control: no-store` sur `/login` et `/admin` uniquement (pas
  globalement).
- Scheduler persistant : les crons sont relus depuis la base de données et
  réenregistrés dans APScheduler à chaque démarrage du conteneur.
- Healthcheck intégré (`/health`).
- Gestion réaliste des erreurs zip : un fichier illisible (socket,
  permission refusée) ne fait plus échouer tout le job tant qu'une archive
  non vide a été produite — l'avertissement est consigné dans le fichier
  `.log` généré à côté de chaque archive plutôt que de bloquer la
  sauvegarde.

Les sauvegardes produites (fichiers `.zip`) ne sont **pas chiffrées** — à
garder en tête si la destination est ensuite synchronisée vers un service
cloud tiers.

## Fichier log

Chaque exécution produit un fichier `.log` à côté de l'archive (même
dossier, même horodatage) : nom du job, source, début/fin, durée, statut,
taille, et les avertissements/erreurs de `zip` le cas échéant.

## Limitation connue

Pas de confirmation automatique qu'une synchronisation cloud placée derrière
le dossier de destination (OneDrive, rclone, etc.) a réellement abouti —
Backer écrit l'archive, la synchro éventuelle suit ensuite de façon
indépendante et n'est pas vérifiée par Backer.

## Variables d'environnement

Voir `.env.example` pour la liste complète et commentée.
