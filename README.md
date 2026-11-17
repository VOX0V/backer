# Backer

Interface web pour gérer des jobs de sauvegarde (zip natif Linux) sur un
serveur Linux, à la place de scripts shell manuels.

## Démarrage rapide (nouvelle installation)

```bash
git clone https://github.com/<pseudo>/backer.git
cd backer
cp .env.example .env
nano .env   # remplir ADMIN_USERNAME / ADMIN_PASSWORD
cp jobs.yml.example data/jobs.yml
nano data/jobs.yml   # décrire tes sauvegardes
docker compose up -d
```

Le premier démarrage crée le compte admin défini dans `.env` (si aucun
utilisateur n'existe déjà) et applique les sauvegardes décrites dans
`jobs.yml`.

## Comment ça marche

Les sauvegardes sont définies dans le fichier `jobs.yml` du dossier de
données (`/app/data/jobs.yml` dans le conteneur). Ce fichier se modifie
depuis l'éditeur web (menu **Jobs**, réservé aux admins) ou directement
dans le fichier. Chaque enregistrement dans l'éditeur est vérifié puis
appliqué immédiatement, sans redémarrage.

## Format de jobs.yml

```yaml
jobs:
  - name: DebServ
    source: /home/srv
    dest: /mnt/hdd/onedrive/backups/debserv
    cron: "0 3 * * *"
    retention: 30
    enabled: true
```

- `name` : nom unique de la tâche (utilisé aussi dans les noms d'archives).
- `source` doit être sous un des volumes montés en lecture seule (voir
  `docker-compose.yml` — par défaut `/home/srv`, `/mnt/netsec`,
  `/mnt/piserv`, à adapter à ton infra).
- `dest` doit être sous `/mnt/hdd` (seule racine montée en écriture par
  défaut dans `docker-compose.yml`) — adapte ce chemin et le volume associé
  si un service de synchronisation cloud (OneDrive, rclone, ...) surveille
  déjà un dossier particulier chez toi.
- `cron` : format standard 5 champs (`minute heure jour mois jour_semaine`)
  — génère-le sur crontab.guru si besoin. `manual` (ou absent) = pas de
  planification automatique (bouton « Run » uniquement).
- `retention` : nombre d'archives à garder (absent ou `null` = illimité).
- `enabled` : `true` (par défaut) ou `false`.
- `staging_dir` (optionnel, en haut du fichier, hors de la liste) : dossier
  temporaire où l'archive est construite avant d'être déplacée d'un coup dans
  `dest`. Il doit être sur le même disque que les `dest` et en dehors de tout
  dossier `source` ou `dest`. Ça évite qu'un client de synchronisation
  (OneDrive...) voie des fichiers à moitié écrits. Sans cette ligne, l'archive
  est écrite directement dans `dest`.
- `offsite_storage_gb` (optionnel) : taille totale de ton stockage en ligne,
  en Go (1 Go = 1024³ octets, comme le client OneDrive). Sert à afficher
  l'espace restant sur le dashboard (seules les archives de Backer sont
  comptées).
- Sans fichier `jobs.yml`, Backer démarre sans aucune sauvegarde et affiche
  un message sur le dashboard.
- Si le fichier est invalide au démarrage, la dernière configuration valide
  continue de tourner et les erreurs sont affichées.
- Une tâche retirée du fichier est **supprimée** de Backer avec son
  historique ; les archives déjà présentes sur le disque ne sont jamais
  touchées.

## Fonctionnement du dashboard

- **Run** exécute la sauvegarde en arrière-plan (ne bloque pas la page) ;
  le dashboard se rafraîchit automatiquement toutes les 10s tant qu'une tâche
  est en cours, pour voir l'état évoluer sans recharger manuellement.
- **Cron**, **Keep** (rétention) et l'état activé/désactivé s'affichent
  mais se modifient uniquement dans `jobs.yml`.
- **Rétention** : nombre d'archives à garder par tâche. Une fois la limite
  atteinte, les plus vieilles sont supprimées automatiquement après chaque
  sauvegarde réussie (archive + son `.log`). Absente = illimitée.
- Le nom du dernier log est cliquable → page en texte brut avec le détail
  de l'exécution (durée, taille, avertissements zip éventuels).
- **History** (bouton par tâche) : liste toutes les archives conservées
  (nom, date, taille, confirmation d'upload OneDrive), avec suppression
  manuelle possible par archive.
- Taille totale affichée par tâche et globalement en haut du dashboard.
- Durée moyenne des 10 dernières sauvegardes réussies, et pendant une
  sauvegarde une barre de progression avec le temps restant estimé.
- Un camembert montre l'espace pris par chaque tâche, et un encadré
  l'espace restant du stockage en ligne.

## Voyant de santé et alertes e-mail

Chaque tâche a un voyant (`ok`, `warning`, `critical`, ou `disabled`) affiché
dans la colonne Status et dans l'API. Il passe en `critical` si la dernière
sauvegarde a échoué, si l'envoi OneDrive a échoué, si la source est
déconnectée, si le disque de destination a moins de 5 % d'espace libre ou
s'il n'y a eu aucune réussite depuis 36 h. Il passe en `warning` si l'upload
n'est pas confirmé après le seuil, si la taille de l'archive a chuté ou si le
disque a moins de 10 % d'espace libre. Les seuils se règlent dans le bloc
`alerts:` de `jobs.yml` (voir `jobs.yml.example`).

Quatre événements envoient un e-mail, une seule fois par incident :
sauvegarde en échec, upload non confirmé, archive beaucoup plus petite que la
précédente, disque presque plein. L'envoi se règle dans le `.env`
(`SMTP_USER`, `SMTP_PASSWORD`, `ALERT_TO`, voir `.env.example`) ; avec Gmail il
faut un mot de passe d'application. Le bouton « Send test email » de la page
Jobs vérifie la configuration. Au premier passage, seuls les événements
postérieurs à l'activation des alertes déclenchent un e-mail.

## Vitesses et connexion au log

Backer estime la vitesse de création des archives (taille de l'archive /
durée de la sauvegarde) et la vitesse d'upload (taille / délai jusqu'à la
confirmation OneDrive, qui inclut le temps de détection du fichier par le
client), en Mo/s (1 Mo = 1 000 000 octets), sur les 10 dernières sauvegardes.

La connexion au log du client OneDrive est vérifiée : `ok`, `stale` (pas
écrit depuis plus de 15 minutes : client arrêté ou dossier remplacé) ou
`unreadable` (fichier introuvable). Dans ces deux derniers cas, l'état des
uploads passe à `unknown` (plus de fausses alertes), la santé passe en
`warning`, un bandeau s'affiche et un e-mail est envoyé. Si le dossier du log
a été supprimé puis recréé, il faut recréer le conteneur Backer pour le
raccrocher.

## Courbes (Trends)

La page Trends montre, pour chaque tâche, la taille des archives et la durée
des sauvegardes sur les 30 dernières réussites.

## API de statut

`GET /api/status` (sans login, réseau local seulement) renvoie en JSON, pour
chaque tâche, l'état de la source, la dernière sauvegarde et la prochaine,
la durée, les tailles, le nombre d'échecs récents, l'espace disque de la
destination et la confirmation d'upload OneDrive, plus un bloc
`onedrive_client` (lu dans le log du client OneDrive) et un bloc `backer`.

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
- Scheduler persistant : les crons sont relus depuis `jobs.yml` et
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
