"""Alertes AutoPipe : surveille la sante du token YouTube et previent par
email (Gmail SMTP) quand une re-autorisation est requise, ou quand un run
echoue. Conc pour GitHub Actions (secrets GMAIL_USER / GMAIL_APP_PASSWORD)
et pour un usage local (section [alerts] d'autopipe.toml)."""
import os
import time

from .config import get_paths, get_config
from .util import (apply_upload_proxy, fix_proxy_env, get_logger,
                   load_json, save_json)

log = get_logger("alerts")

REAUTH_DELAY_S = 24 * 3600   # anti-spam alerte re-autorisation : 1 email / 24 h
FAIL_DELAY_S = 2 * 3600      # anti-spam alerte echec de run : 1 email / 2 h


def _gmail_creds(cfg):
    """Identifiants Gmail : variables d'environnement d'abord (CI), sinon
    section [alerts] d'autopipe.toml (usage local)."""
    a = cfg.get("alerts") or {}
    user = os.environ.get("GMAIL_USER") or a.get("gmail_user", "")
    pwd = os.environ.get("GMAIL_APP_PASSWORD") or a.get("gmail_app_password", "")
    to = os.environ.get("GMAIL_ALERT_TO") or a.get("alert_to", "") or user
    return user, pwd, to


def send_gmail_alert(cfg, subject, body):
    """Envoie un email via Gmail SMTP (port 465 SSL). True si envoye."""
    user, pwd, to = _gmail_creds(cfg)
    if not user or not pwd:
        log.warning("Alerte non envoyee: Gmail non configure "
                    "(secrets GMAIL_USER/GMAIL_APP_PASSWORD ou [alerts] "
                    "dans autopipe.toml)")
        return False
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    # smtplib ne lit pas les variables proxy : connexion directe OK.
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)
    log.info("Email envoye -> %s : %s", to, subject)
    return True


def token_health(cfg):
    """Etat du token YouTube, SANS effet de bord et sans navigateur.

    status: ok | missing | invalid ; reason explicite si non-ok.
    Le refresh teste ici reste en memoire (le refresh_token durable du
    fichier n'est jamais modifie).
    """
    from .upload import SCOPES, _missing_scopes
    # Localisation US a tout prix : le refresh OAuth teste ici (et son
    # eventuel appel reseau a oauth2.googleapis.com) passe par le MEME
    # egress que la publication : upload_proxy (sortie US) si defini, sinon
    # proxy_mode local. Aucune exception pour check-token.
    cfg = cfg or get_config()
    net_cfg = cfg.get("network", {}) or {}
    upload_proxy = str(net_cfg.get("upload_proxy") or "").strip()
    if upload_proxy:
        apply_upload_proxy(upload_proxy)
    else:
        fix_proxy_env(str(net_cfg.get("proxy_mode", "direct") or "direct"))
    paths = get_paths(cfg)
    token_file = paths.root / cfg["upload"].get("token_file", "token.json")
    if not token_file.exists():
        return {"status": "missing", "reason": "token.json absent"}
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except Exception as exc:
        return {"status": "invalid", "reason": "token.json illisible: %s" % exc}
    missing = _missing_scopes(creds)
    if missing:
        return {"status": "invalid",
                "reason": "scopes manquants: %s" % ", ".join(missing)}
    if not creds.expired:
        return {"status": "ok"}
    if not creds.refresh_token:
        return {"status": "invalid", "reason": "aucun refresh_token"}
    try:
        creds.refresh(Request())
        return {"status": "ok", "refreshed": True}
    except Exception as exc:
        return {"status": "invalid", "reason": str(exc)[:300]}


def notify_if_action_needed(cfg, force=False, cause="reauth"):
    """Verifie le token et envoie l'email d'alerte si une action est requise.

    cause: "reauth" (token YouTube a re-autoriser) ou "runfail" (run echoue).
    Retourne (health, sent). Anti-spam par cause : 1 email / 24 h (reauth)
    ou / 2 h (runfail), sauf force=True.
    """
    health = token_health(cfg)
    if health["status"] == "ok" and not force:
        return health, False
    paths = get_paths(cfg)
    state_file = paths.root / "data" / "alerts_state.json"
    state = load_json(state_file, {}) or {}
    key = "last_%s_ts" % cause
    delay = REAUTH_DELAY_S if cause == "reauth" else FAIL_DELAY_S
    if not force and (time.time() - state.get(key, 0)) < delay:
        log.info("Alerte %s deja envoyee recemment (anti-spam)", cause)
        return health, False
    if cause == "reauth":
        subject = "[AutoPipe] ACTION REQUISE : re-autorisation YouTube"
        body = _reauth_body(health)
    else:
        subject = "[AutoPipe] Echec d'un run - action conseillee"
        body = _fail_body()
    sent = send_gmail_alert(cfg, subject, body)
    if sent:
        state[key] = time.time()
        state["last_cause"] = health.get("reason", cause)
        save_json(state_file, state)
    return health, sent


def _reauth_body(health):
    return (
        "Le bot AutoPipe ne peut plus publier sur YouTube.\n"
        "Cause: %s\n"
        "\n"
        "ACTION A FAIRE (2 minutes, sur ton PC Windows):\n"
        "1. Ouvre le dossier AutoPipe (Bureau > BOT TELEGRAM > AutoPipe)\n"
        "2. Double-clique sur  reauth_247.bat\n"
        "3. Une fenetre de navigateur s'ouvre: choisis ton compte Google\n"
        "   (celui de la chaine ghostshort) puis clique \"Autoriser\"\n"
        "4. Le script envoie tout seul le nouveau token sur GitHub.\n"
        "   La prochaine video repartira automatiquement.\n"
        "\n"
        "Tant que l'autorisation n'est pas refaite, la chaine ne publie plus\n"
        "(les runs continuent d'essayer sans succes, tu recevras cet email\n"
        "au maximum une fois par jour).\n"
        "\n"
        "Suivi du bot: https://github.com/Check913/autopipe/actions\n"
        % (health.get("reason") or "token YouTube expire/invalide")
    )


def _fail_body():
    url = "%s/%s/actions/runs/%s" % (
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", "Check913/autopipe"),
        os.environ.get("GITHUB_RUN_ID", "inconnu"))
    return (
        "Un run AutoPipe a echoue sur GitHub Actions.\n"
        "\n"
        "Log complet et details:\n%s\n"
        "\n"
        "Causes frequentes:\n"
        "- quota YouTube atteint (se reinitialise a minuit UTC)\n"
        "- token YouTube expire -> double-clique reauth_247.bat sur ton PC\n"
        "- erreur reseau temporaire (relance avec le bouton \"Run workflow\")\n"
        % url
    )