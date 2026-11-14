# Backer

Interface web pour gérer des jobs de sauvegarde (zip natif Linux) sur DebServ,
à la place des scripts shell manuels.

## Comment ça marche maintenant

Les jobs sont définis dans le `.env` (nom / source / destination), **pas**
via l'interface web. La planification (cron) se change depuis le dashboard
et est conservée en base — elle survit aux redémarrages du conteneur.

## Déploiement

1. Copier ce dossier dans `/home/srv/sys/backer` (déjà prévu ainsi).
2. Le `.env` du service `backer` doit se trouver au même niveau que le
   `docker-compose.yml` de la stack, donc `/home/srv/sys/.env` (le même
   fichier qui contient déjà `CFTOK` pour le tunnel Cloudflare). Voir
   `backer/.env.example` pour le format complet, notamment :
   - `ADMIN_USERNAME` / `ADMIN_PASSWORD` : compte admin créé automatiquement
     au tout premier démarrage (seulement si aucun utilisateur n'existe déjà).
   - `BACK1_NAME` / `BACK1_SOURCE` / `BACK1_DEST` / `BACK1_CRON` (et `BACK2_*`,
     `BACK3_*`, ...) : un bloc par job de sauvegarde.
3. `docker compose build --no-cache backer && docker compose up -d backer`
4. Se connecter, ajuster le cron de chaque job depuis le dashboard si besoin.
5. Dans `/admin` : changer le mot de passe admin ou créer de nouveaux comptes
   pour les personnes avec qui tu partages l'accès.

## Format des jobs dans le .env

```
BACK1_NAME=DebServ
BACK1_SOURCE=/home/srv
BACK1_DEST=/mnt/ssd/onedrive/backup-nas/backups
BACK1_CRON=0 3 * * *
```

- `SOURCE` doit être sous un des volumes montés en lecture seule
  (`/home/srv`, `/mnt/netsec`, `/mnt/piserv`).
- `DEST` doit être sous `/mnt/ssd` (seule racine montée en écriture) —
  pointer vers `/mnt/ssd/onedrive/backup-nas/backups` fait que le conteneur
  `onedrive` existant synchronise automatiquement les archives, sans rien
  à configurer côté Backer.
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

## Sécurité — ce qui a changé par rapport au prototype d'origine

- **Plus de `privileged: true` / `user: "0:0"` / montage `/:/host`.** Le
  conteneur tourne en utilisateur non-root (UID/GID 1000:1000, doit
  correspondre à ton utilisateur sur DebServ) et n'a accès qu'à des chemins
  précis, montés explicitement.
- **Sources en lecture seule** (`/home/srv`, `/mnt/netsec`, `/mnt/piserv`) :
  Backer ne peut jamais les modifier ou les effacer, seulement les lire pour
  créer une archive.
- **`/mnt/ssd` est la seule racine en écriture**, utilisée comme destination
  des sauvegardes.
- **`SECRET_KEY`** généré aléatoirement au premier démarrage et persisté dans
  `data/secret_key` (permissions 600) — plus de valeur par défaut `dev_key`.
- **Comptes multi-utilisateurs**, mots de passe hachés en PBKDF2/SHA-256.
- **Bannissement d'IP** : 3 échecs de connexion → blocage de l'IP pendant 1h
  (visible et levable manuellement depuis `/admin`).
- **Révocation de session** : désactiver un compte invalide immédiatement
  toute session déjà ouverte pour ce compte.
- **Cookie de session** : `Secure` (si `CONNECTION_TYPE=https`), `HttpOnly`,
  `SameSite=Lax`.
- **Scheduler persistant** : les crons sont relus depuis la base de données
  et réenregistrés dans APScheduler à chaque démarrage du conteneur.
- **Healthcheck** intégré (`/health`), surveillé par le conteneur `autoheal`
  déjà présent dans la stack `System`.
- **Gestion réaliste des erreurs zip** : un fichier illisible (socket,
  permission refusée) ne fait plus échouer tout le job tant qu'une archive
  non vide a été produite — l'avertissement est consigné dans le fichier
  `.log` généré à côté de chaque archive plutôt que de bloquer la sauvegarde.

## Fichier log

Chaque exécution produit un fichier `.log` à côté de l'archive (même dossier,
même horodatage) : nom du job, source, début/fin, durée, statut, taille, et
les avertissements/erreurs de `zip` le cas échéant.

## Limitation connue

Pas de confirmation automatique que l'upload OneDrive a réellement abouti —
Backer écrit l'archive dans le dossier synchronisé, la synchro suit derrière
via le conteneur `onedrive` existant, mais Backer ne vérifie pas ce second
upload (le conteneur `onedrive` ne logue que sur stdout, pas dans un fichier
lisible par Backer sans lui redonner accès au socket Docker). À revisiter
plus tard si besoin d'une vraie confirmation de bout en bout.

## Migration depuis une version antérieure

Le schéma de la base de données a changé plusieurs fois pendant le
développement. Si `backer.db` existe déjà d'une version précédente,
supprime-le avant de relancer (une nouvelle base vide est recréée
automatiquement, les tâches sont re-synchronisées depuis le `.env`,
mais l'historique des exécutions précédentes sera perdu) :
```bash
sudo rm /home/srv/sys/backer/data/backer.db
docker compose build --no-cache backer
docker compose up -d backer
```

