from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active_ = db.Column("is_active", db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login_at = db.Column(db.DateTime, nullable=True)

    # Incrémenté à la désactivation ou au changement de mot de passe pour
    # invalider immédiatement toute session existante (révocation).
    session_version = db.Column(db.Integer, default=0, nullable=False)

    def set_password(self, raw_password):
        # method="pbkdf2:sha256" -> même famille de hash que printer_remote
        self.password_hash = generate_password_hash(raw_password, method="pbkdf2:sha256")

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_active(self):
        return self.is_active_

    def get_id(self):
        # Encode l'id + la version de session -> une désactivation ou un
        # changement de mdp invalide immédiatement les sessions ouvertes.
        return f"{self.id}:{self.session_version}"


class LoginHistory(db.Model):
    __tablename__ = "login_history"

    id = db.Column(db.Integer, primary_key=True)
    username_attempted = db.Column(db.String(80), nullable=False)
    ip_address = db.Column(db.String(64), nullable=False)
    success = db.Column(db.Boolean, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    # True une fois l'alerte "effacée" depuis le dashboard -- reste dans
    # l'historique complet visible sous /admin, juste masquée en haut du
    # dashboard jusqu'à la prochaine tentative échouée.
    dismissed = db.Column(db.Boolean, default=False, nullable=False)


class IPBan(db.Model):
    __tablename__ = "ip_bans"

    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(64), unique=True, nullable=False, index=True)
    fail_count = db.Column(db.Integer, default=0, nullable=False)
    banned_until = db.Column(db.DateTime, nullable=True)
    last_attempt_at = db.Column(db.DateTime, default=datetime.now)


class BackupJob(db.Model):
    __tablename__ = "backup_jobs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

    # Chemins hôte complets (tels qu'écrits dans le .env, ex: /home/srv) --
    # traduits en chemin conteneur au moment de l'exécution (voir utils.py).
    source_path = db.Column(db.String(1024), nullable=False)
    dest_path = db.Column(db.String(1024), nullable=False)

    # Expression cron standard à 5 champs (ex: "0 3 * * *"), ou "manual"
    # pour un job sans planification automatique. Éditable depuis le
    # dashboard, persisté en DB -- survit aux redémarrages du conteneur.
    cron_expression = db.Column(db.String(64), nullable=False, default="manual")

    enabled = db.Column(db.Boolean, default=True, nullable=False)

    # Nombre d'archives à conserver pour cette tâche (les plus vieilles sont
    # supprimées automatiquement après chaque sauvegarde réussie).
    # NULL = pas de limite (aucune suppression automatique).
    retention_count = db.Column(db.Integer, nullable=True)

    # True si ce job vient du .env (source/dest resynchronisées à chaque
    # démarrage). Un job dont l'entrée .env disparaît est désactivé
    # automatiquement plutôt que supprimé (historique conservé).
    env_managed = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.now)

    history = db.relationship(
        "BackupHistory", backref="job", lazy=True,
        order_by="desc(BackupHistory.started_at)", cascade="all, delete-orphan",
    )


class BackupHistory(db.Model):
    __tablename__ = "backup_history"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("backup_jobs.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.now)
    finished_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="running")  # running / success / failed
    message = db.Column(db.Text, nullable=True)
    archive_name = db.Column(db.String(255), nullable=True)
    log_name = db.Column(db.String(255), nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)


class OneDriveUpload(db.Model):
    """Archive dont l'envoi vers OneDrive est confirmé par le log du client OneDrive."""
    __tablename__ = "onedrive_uploads"

    id = db.Column(db.Integer, primary_key=True)
    archive_name = db.Column(db.String(255), unique=True, nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False)
