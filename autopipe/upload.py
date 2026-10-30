"""YouTube publishing via the official API (resumable upload + OAuth)."""
from pathlib import Path

from .config import get_config, get_paths
from .util import get_logger

log = get_logger("upload")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def _missing_scopes(creds):
    """Scopes requis absents du token charge (liste vide si tout est present)."""
    have = set(creds.scopes or [])
    return sorted(set(SCOPES) - have)


def _build_service(creds, mode="direct"):
    """Construit le client YouTube avec un httplib2 maitrise (pas de proxy).

    build() refuse credentials + http ensemble : on fabrique donc nous-meme
    le http autorise (google_auth_httplib2.AuthorizedHttp). En mode 'direct',
    le proxy_info explicite a None garantit que l'upload et l'OAuth via
    httplib2 ignorent tout proxy systeme, cause racine du ProxyError
    WRONG_VERSION_NUMBER observe le 2026-10-23 (voir util.fix_proxy_env).
    """
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    if mode == "direct":
        # httplib2 appelle proxy_info(method) ; None retourne -> aucun proxy.
        proxy_info = lambda *args, **kwargs: None  # noqa: E731
    else:
        # Modes 'fix'/'keep' : on respecte les variables d'env (fix_proxy_env
        # a deja reecrit les schemas https:// mal declares en http://).
        proxy_info = httplib2.proxy_info_from_environment
    http = httplib2.Http(timeout=120, proxy_info=proxy_info)
    # httplib2 0.32.0 inclut 308 dans REDIRECT_CODES et leve
    # RedirectMissingLocation car la reponse "308 Resume Incomplete" des
    # uploads resumables (YouTube, Drive) n'emporte pas de header Location.
    # Correctif canonique de googleapiclient.discovery.build_http() (issue
    # googleapis/google-api-python-client#891) : on retire 308 des codes de
    # redirection, dans tous les modes (direct/fix/keep).
    http.redirect_codes = set(http.redirect_codes) - {308}
    return build("youtube", "v3", http=AuthorizedHttp(creds, http=http),
                 cache_discovery=False)


def get_service(cfg=None):
    cfg = cfg or get_config()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    paths = get_paths(cfg)
    secrets = paths.root / cfg["upload"].get("client_secrets", "client_secret.json")
    token_file = paths.root / cfg["upload"].get("token_file", "token.json")
    mode = cfg.get("network", {}).get("proxy_mode", "direct")

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    # Un vieux token sans youtube.force-ssl permettait l'upload mais cassait le
    # commentaire epingle (403 apres coup). On relance une autorisation
    # complete AVANT l'upload plutot qu'echouer apres avoir publie la video.
    missing = _missing_scopes(creds) if creds else None
    if missing:
        log.warning("token.json sans les scopes requis (%s) -> nouvelle "
                    "autorisation avec: %s", ", ".join(missing), ", ".join(SCOPES))
        creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            log.warning("Refresh du token impossible (%s) -> re-autorisation", exc)
            creds = None
    if not creds or not creds.valid:
        if not secrets.exists():
            raise FileNotFoundError(
                "client_secret.json not found (%s). See README OAuth section." % secrets)
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        if hasattr(flow, "run_console"):
            try:
                creds = flow.run_console()
            except Exception:
                creds = None
        if not creds:
            creds = flow.run_local_server(open_browser=True, port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        log.info("OAuth token saved -> %s", token_file)
    return _build_service(creds, mode)


DEFAULT_COMMENT = ("Which fact surprised you the most? Drop it in the comments 👇 "
                   "and subscribe for a new Short every day!")


def pinned_comment_text(idea, cfg=None):
    """Construit le texte du commentaire a publier sous la video."""
    cfg = cfg or get_config()
    if not cfg["upload"].get("auto_comment", True):
        return None
    tpl = str(cfg["upload"].get("comment_template") or DEFAULT_COMMENT)
    try:
        return tpl.format(title=idea.get("title", ""), hook=idea.get("hook", ""),
                          tags=", ".join(str(t) for t in (idea.get("tags") or [])[:5]))
    except Exception:
        return tpl


def post_pinned_comment(video_id, text, cfg=None):
    """Publie un commentaire au nom de la chaine sous la video publiee.

    L'API officielle ne permet pas d'epingler: le commentaire du proprietaire
    apparait en tete. Epinglage manuel en 1 clic dans YouTube Studio si voulu.
    """
    cfg = cfg or get_config()
    if not text:
        return None
    try:
        yt = get_service(cfg)
        body = {"snippet": {"videoId": video_id,
                            "topLevelComment": {"snippet": {
                                "videoId": video_id,
                                "textOriginal": str(text)[:9000]}}}}
        resp = yt.commentThreads().insert(part="snippet", body=body).execute()
        cid = resp.get("id")
        log.info("Commentaire publie: %s", cid)
        return cid
    except Exception as exc:
        log.warning("Commentaire non publie: %s", exc)
        return None


def ensure_channel_country(cfg=None):
    """Aligne la localisation de la chaine (brandingSettings.channel.country).

    Google a retire la geolocalisation par video (videos.insert
    recordingDetails, depuis 2017) et regionCode n'est qu'un filtre de
    recherche/charts : le seul levier API officiel pour que la page YouTube
    apparaisse "localisee" est le pays de la chaine
    (brandingSettings.channel.country). Ecriture = 50 unites de quota
    (lecture = 1), donc idempotent : on n'ecrit que si la valeur differe.
    Retourne {country, changed, previous} ou None en cas d'echec.
    """
    cfg = cfg or get_config()
    country = str(cfg["upload"].get("channel_country", "US"))
    # Fix 2026-10-27 : YouTube renvoie parfois un 500 INTERNAL_ERROR
    # transient sur channels.list/update -> on retente 2 fois (pause 5/10 s)
    # avant de declarer l'echec.
    import time
    from googleapiclient.errors import HttpError
    try:
        yt = get_service(cfg)
        resp = None
        for attempt in range(3):
            try:
                resp = yt.channels().list(part="brandingSettings", mine=True).execute()
                break
            except HttpError as exc:
                status = getattr(exc.resp, "status", None) or 0
                if status >= 500 and attempt < 2:
                    log.warning("Localisation chaine: HTTP %s transient, "
                                "tentative %d/3", status, attempt + 1)
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        items = resp.get("items") or []
        if not items:
            log.warning("Localisation chaine: aucune chaine pour ce token")
            return None
        # On ne renvoie que la sous-partie "channel" : "image"/"hints" sont
        # deprecated et leur aller-retour provoque des 400 Invalid value.
        channel = dict((items[0].get("brandingSettings") or {}).get("channel") or {})
        previous = channel.get("country")
        if previous == country:
            log.info("Localisation de la chaine deja '%s' (rien a faire)", country)
            return {"country": country, "changed": False, "previous": previous}
        channel["country"] = country
        for attempt in range(3):
            try:
                yt.channels().update(part="brandingSettings",
                                     body={"brandingSettings": {"channel": channel}}).execute()
                break
            except HttpError as exc:
                status = getattr(exc.resp, "status", None) or 0
                if status >= 500 and attempt < 2:
                    log.warning("Localisation chaine: HTTP %s transient sur "
                                "update, tentative %d/3", status, attempt + 1)
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        log.info("Localisation de la chaine: %s -> '%s'", previous, country)
        return {"country": country, "changed": True, "previous": previous}
    except Exception as exc:
        log.warning("Localisation de la chaine non appliquee: %s", exc)
        return None


def upload_video(video_path, title, description, tags=None, cfg=None, privacy=None,
                 pinned_comment=None):
    """Resumable upload. Returns the YouTube video ID."""
    cfg = cfg or get_config()
    yt = get_service(cfg)
    tags = [str(t)[:100] for t in (tags or [])][:30]
    body = {
        "snippet": {
            "title": str(title)[:100],
            "description": str(description)[:4900],
            "tags": tags,
            "categoryId": str(cfg["upload"].get("category_id", "24")),
            "defaultLanguage": str(cfg["general"].get("language", "en")),
            "defaultAudioLanguage": str(cfg["general"].get("language", "en")),
        },
        "status": {
            "privacyStatus": privacy or cfg["upload"].get("privacy", "public"),
            "selfDeclaredMadeForKids": bool(cfg["upload"].get("made_for_kids", False)),
        },
    }
    from googleapiclient.http import MediaFileUpload
    # Fix 2026-10-30 : upload 48 Mo interrompu par ssl.SSLEOFError (TLS ferme
    # en pleine transaction -> aucun video_id). Chunks de 1 Mo (fenetre TLS
    # plus courte) + num_retries=10 sur next_chunk : googleapiclient retente
    # avec backoff exponentiel et reprend depuis la derniere reponse 308 au
    # lieu d'abandonner tout l'upload.
    media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                            chunksize=1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=10)
        if status:
            log.info("Upload: %.0f%%", status.progress() * 100)
    video_id = response.get("id")
    log.info("Video published: https://youtu.be/%s", video_id)
    if cfg["upload"].get("enforce_channel_country", True):
        # A chaque publication, re-aligne le pays de la chaine (US par defaut) :
        # idempotent, 1 unite de quota si deja correct, 50 sinon.
        ensure_channel_country(cfg)
    if pinned_comment:
        post_pinned_comment(video_id, pinned_comment, cfg)
    return video_id
