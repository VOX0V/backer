from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db, login_manager
from models import User, LoginHistory, IPBan

auth_bp = Blueprint("auth", __name__)

BAN_THRESHOLD = 3
BAN_DURATION_MINUTES = 60

# Hachage factice calculé une seule fois au démarrage, utilisé quand le
# nom d'utilisateur soumis n'existe pas -- sans ça, aucun calcul PBKDF2
# n'a lieu dans ce cas (court-circuit du "and" plus bas), ce qui rend le
# temps de réponse mesurablement différent d'un mauvais mot de passe sur
# un compte existant et permet de deviner quels comptes existent
# (énumération par timing). Le mot de passe factice n'a pas besoin d'être
# secret : il ne sert jamais à authentifier qui que ce soit.
_DUMMY_HASH = generate_password_hash(
    "dummy_password_never_used_for_auth", method="pbkdf2:sha256"
)


@login_manager.user_loader
def load_user(compound_id):
    # get_id() renvoie "id:session_version" -> on vérifie que la version
    # correspond toujours, sinon la session a été révoquée (désactivation,
    # changement de mot de passe).
    try:
        user_id, version = compound_id.split(":")
        user_id, version = int(user_id), int(version)
    except (ValueError, AttributeError):
        return None

    user = db.session.get(User, user_id)
    if user is None or user.session_version != version or not user.is_active_:
        return None
    return user


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _client_ip():
    # Cloudflare Tunnel transmet la vraie IP du visiteur dans CF-Connecting-IP.
    # X-Forwarded-For, derrière un tunnel, ne contient souvent que l'IP interne
    # du conteneur cloudflared (ex: 172.20.0.1, la passerelle du réseau Docker).
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def get_active_ban(ip_address):
    ban = IPBan.query.filter_by(ip_address=ip_address).first()
    if ban and ban.banned_until and ban.banned_until > datetime.now():
        return ban
    return None


def _register_failed_attempt(ip_address):
    ban = IPBan.query.filter_by(ip_address=ip_address).first()
    if ban is None:
        ban = IPBan(ip_address=ip_address, fail_count=0)
        db.session.add(ban)

    ban.fail_count += 1
    ban.last_attempt_at = datetime.now()
    if ban.fail_count >= BAN_THRESHOLD:
        ban.banned_until = datetime.now() + timedelta(minutes=BAN_DURATION_MINUTES)
    db.session.commit()


def _register_successful_attempt(ip_address):
    ban = IPBan.query.filter_by(ip_address=ip_address).first()
    if ban:
        ban.fail_count = 0
        ban.banned_until = None
        db.session.commit()


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    ip_address = _client_ip()
    active_ban = get_active_ban(ip_address)

    if request.method == "POST":
        if active_ban:
            flash("Too many failed attempts. Try again later.", "error")
            return render_template("login.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()

        # Un hachage tourne systématiquement, que le compte existe ou non,
        # pour que le temps de réponse ne révèle jamais quels comptes
        # existent (mitigation de la fuite de timing).
        if user is not None:
            password_ok = check_password_hash(user.password_hash, password)
        else:
            check_password_hash(_DUMMY_HASH, password)
            password_ok = False

        success = bool(user and user.is_active_ and password_ok)

        db.session.add(LoginHistory(
            username_attempted=username, ip_address=ip_address, success=success,
        ))
        db.session.commit()

        if success:
            _register_successful_attempt(ip_address)
            user.last_login_at = datetime.now()
            db.session.commit()
            login_user(user)
            return redirect(url_for("main.dashboard"))

        _register_failed_attempt(ip_address)
        flash("Invalid credentials.", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
