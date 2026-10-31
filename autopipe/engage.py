"""Engagement premiere heure (MAX 2026-10-31).

Les 60 premieres minutes ponderent le plus la distribution Shorts : un
commentaire repondu vite double la chance d'une reponse en cascade et
pousse les signaux "engagement precoce" que l'algo sait detecter.

Design (REPLY_POLICY des notes de recherche) :
- fenetre utile : window_min (60) apres publication du commentaire ;
- plafonds : max_replies (par passe/video), max_per_video (vie entiere),
  max_replies_daily (quota global/jour, protege le quota API 50 u/insert) ;
- min_gap_s entre deux reponses (thread d'arriere-plan uniquement ;
  les passes synchrones utilisent un gap court fixe pour ne pas bloquer
  le pipeline) ;
- variete DETERMINISTE : le template est choisi par hachage du
  commentaire (pas de random -> reproductible, pas de boucle) ;
- personnalisation : score "X/10" parse dans le commentaire ;
- idempotence : data/engage/state.json memorise chaque commentaire
  traite. Meme apres une coupure, un commentaire deja repondu n'est
  jamais re-repondu ;
- non fatal : TOUTE exception est logguee, jamais propagee (le pipeline
  ne doit pas echouer a cause de l'engagement).

Depuis Actions (serve --once toutes les 6 h) : sweep() rattrape les
commentaires des dernieres 24 h. Depuis le demon 24/7 : first_hour()
ouvre un thread d'arriere-plan qui repond en continu pendant la
fenetre, en plus de la passe immediate.
"""
import datetime as dt
import hashlib
import re
import threading
import time
import traceback

from .config import get_paths
from .upload import get_service
from .util import get_logger, load_json, save_json

log = get_logger("engage")

_SCORE_RE = re.compile(r"(\d{1,2})\s*/\s*10")

# Templates EN (le bot produit en anglais). Courts (<150 c), varies,
# aucun lien, aucune promesse. 3 familles de score + generique.
_SCORE_TEMPLATES = {
    "low": [
        "{s}/10 happens to everyone - which question got you?",
        "Ouch, {s}/10. These are meant to be hard. Round 2 tomorrow?",
        "{s}/10 - that final question is a score killer for most people.",
        "Rough one at {s}/10. The next quiz plays a bit fairer.",
    ],
    "mid": [
        "Solid {s}/10! The trap question got you, right?",
        "{s}/10 - mid-table. Beat that score tomorrow.",
        "Not bad, {s}/10. The decoys did their job on you.",
        "{s}/10 is a decent run - most land lower. Which was hardest?",
    ],
    "high": [
        "{s}/10 is strong. Which one almost got you?",
        "Nice, {s}/10! You dodged the trap question.",
        "{s}/10 - top tier. Can you repeat that tomorrow?",
        "Sharp run, {s}/10. The 4th question ends most streaks.",
    ],
    "perfect": [
        "{s}/10?! You are in the 1%. New quiz drops tomorrow.",
        "Near perfect - {s}/10. Respect.",
        "{s}/10, clean sweep. Tomorrow's quiz is harder, you warned.",
    ],
}
_GENERIC_TEMPLATES = [
    "Which question got you?",
    "Good - drop your score so we can compare.",
    "This one splits the comments 50/50.",
    "Most people miss the same question here.",
    "The score check at the end is where it gets real.",
    "Curious: did the timer make you rush any answer?",
    "That trap answer caught a lot of players.",
    "Your reaction is the best stat I get all day.",
]


def _state_path(cfg):
    return get_paths(cfg).data / "engage" / "state.json"


def _load_state(cfg):
    st = load_json(_state_path(cfg), {}) or {}
    st.setdefault("replied", {})
    st.setdefault("videos", {})
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if (st.get("daily") or {}).get("date") != today:
        st["daily"] = {"date": today, "count": 0}
    return st


def _save_state(cfg, st):
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    save_json(p, st)


def _own_channel(yt, st):
    """ID de la chaine (cache module + state, 1 appel API au maximum/jour)."""
    if st.get("own_channel_id"):
        return st["own_channel_id"]
    try:
        resp = yt.channels().list(part="id", mine=True).execute()
        items = resp.get("items") or []
        cid = items[0]["id"] if items else None
        if cid:
            st["own_channel_id"] = cid
        return cid
    except Exception as exc:
        log.warning("channels.list(mine) echoue: %s", exc)
        return None


def _parse_iso(ts):
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _bucket(score):
    if score >= 9:
        return "perfect"
    if score >= 6:
        return "high"
    if score >= 3:
        return "mid"
    return "low"


def _template(comment_id, text):
    """Template deterministe : hash(comment_id) -> pas de random, jamais
    deux fois le meme template d'affilee pour un meme commentaire."""
    m = _SCORE_RE.search(text or "")
    if m:
        s = max(0, min(10, int(m.group(1))))
        pool = _SCORE_TEMPLATES[_bucket(s)]
        tmpl = pool[int(hashlib.sha256(comment_id.encode()).hexdigest(), 16)
                    % len(pool)]
        return tmpl.format(s=s)
    pool = _GENERIC_TEMPLATES
    return pool[int(hashlib.sha256(comment_id.encode()).hexdigest(), 16)
                % len(pool)]


def _pass(cfg, video_id, yt=None, max_replies=None, window_min=None,
          gap_s=2.0):
    """Une passe de reponse sur une video. Retourne le nb de reponses."""
    eng = cfg.get("engage", {}) or {}
    max_replies = int(max_replies or eng.get("max_replies", 20))
    window_min = int(window_min or eng.get("window_min", 60))
    max_per_video = int(eng.get("max_per_video", 20))
    max_daily = int(eng.get("max_replies_daily", 30))

    if yt is None:
        yt = get_service(cfg)
    st = _load_state(cfg)
    own = _own_channel(yt, st)
    replied = st["replied"]
    vstate = st["videos"].setdefault(str(video_id), {"replies": 0})
    if int(vstate.get("replies", 0)) >= max_per_video:
        return {"replies": 0, "reason": "per_video_cap"}
    if int(st["daily"]["count"]) >= max_daily:
        return {"replies": 0, "reason": "daily_cap"}

    now = dt.datetime.now(dt.timezone.utc)
    resp = yt.commentThreads().list(
        part="snippet", videoId=str(video_id), maxResults=50,
        order="time").execute()
    sent = 0
    for item in resp.get("items", []):
        if sent >= max_replies:
            break
        if int(st["daily"]["count"]) >= max_daily:
            break
        tlc = ((item.get("snippet") or {}).get("topLevelComment") or {})
        sn = tlc.get("snippet") or {}
        cid = tlc.get("id") or item.get("id")
        if not cid or cid in replied:
            continue
        published = _parse_iso(sn.get("publishedAt"))
        if published is None or (now - published) > dt.timedelta(minutes=window_min):
            continue
        author = (sn.get("authorChannelId") or {}).get("value")
        if own and author == own:
            continue
        text = _template(cid, sn.get("textOriginal", ""))
        try:
            yt.comments().insert(
                part="snippet",
                body={"snippet": {"parentId": cid,
                                  "textOriginal": text}}).execute()
        except Exception as exc:
            # Marque quand meme : evite les boucles de reponse sur un
            # commentaire qui rejetterait toujours (doublon rare assume).
            log.warning("comments.insert %s echoue (marque quand meme): %s",
                        cid, exc)
        replied[cid] = now.isoformat()
        vstate["replies"] = int(vstate.get("replies", 0)) + 1
        st["daily"]["count"] = int(st["daily"]["count"]) + 1
        sent += 1
        _save_state(cfg, st)
        if sent < max_replies and gap_s > 0:
            time.sleep(gap_s)
    if sent:
        _save_state(cfg, st)
    return {"replies": sent}


def _watch(cfg, video_id, deadline):
    """Thread d'arriere-plan : repend pendant toute la fenetre (demon)."""
    eng = cfg.get("engage", {}) or {}
    cycle_s = int(eng.get("cycle_s", 300))
    window_min = int(eng.get("window_min", 60))
    gap_s = float(eng.get("min_gap_s", 45))
    try:
        yt = get_service(cfg)
        while time.time() < deadline:
            remaining_s = deadline - time.time()
            if remaining_s <= 0:
                break
            _pass(cfg, video_id, yt=yt,
                  max_replies=int(eng.get("max_replies", 20)),
                  window_min=window_min, gap_s=gap_s)
            if time.time() + cycle_s >= deadline:
                break
            time.sleep(cycle_s)
    except Exception:
        log.warning("engage thread arrete (non fatal):\n%s",
                    traceback.format_exc())


def first_hour(cfg, video_id):
    """A appeler JUSTE apres la publication (scheduler).

    Passe immediate bornee (3 reponses max, pas de longs sleeps) puis
    thread daemon pour le reste de la fenetre : en Actions one-shot le
    thread meurt avec le process (sans bloquer la sortie), en demon
    24/7 il couvre les minutes 3-60 ou les signaux comptent le plus.
    """
    eng = cfg.get("engage", {}) or {}
    if not eng.get("enabled", True):
        return {"skipped": True}
    video_id = str(video_id)
    immediate = _pass(cfg, video_id, max_replies=3, gap_s=2.0)
    window_min = int(eng.get("window_min", 60))
    threading.Thread(
        target=_watch, name="engage-first-hour",
        args=(cfg, video_id, time.time() + window_min * 60),
        daemon=True).start()
    log.info("engage.first_hour: %s reponse(s) immediate(s), thread %d min",
             (immediate or {}).get("replies", 0), window_min)
    return {"immediate": immediate, "window_min": window_min}


def _recent_uploads(yt, hours=24):
    """Videos publiées dans les N dernieres heures [(video_id, dt), ...]."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items") or []
    if not items:
        return []
    uploads = ((items[0].get("contentDetails") or {})
               .get("relatedPlaylists") or {}).get("uploads")
    if not uploads:
        return []
    pl = yt.playlistItems().list(
        part="contentDetails", playlistId=uploads,
        maxResults=25).execute()
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for it in pl.get("items", []):
        cd = it.get("contentDetails") or {}
        vid = cd.get("videoId")
        published = _parse_iso(cd.get("videoPublishedAt")
                               or (it.get("snippet") or {}).get("publishedAt"))
        if vid and published and (now - published) <= dt.timedelta(hours=hours):
            out.append((vid, published))
    return out


def sweep(cfg):
    """Rattrapage pour les lancements one-shot (Actions toutes les 6 h).

    Repond aux commentaires encore dans la fenetre sur les videos des
    dernieres 24 h. Idempotent (state) et plafonne (quota journalier).
    """
    eng = cfg.get("engage", {}) or {}
    if not eng.get("enabled", True):
        return {"skipped": True}
    yt = get_service(cfg)
    out = {}
    for vid, _published in _recent_uploads(yt, hours=24):
        try:
            out[vid] = _pass(cfg, vid, yt=yt, gap_s=2.0)
        except Exception as exc:
            log.warning("engage.sweep %s echoue (non fatal): %s", vid, exc)
    return out
