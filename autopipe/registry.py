# -*- coding: utf-8 -*-
"""Registre permanent des videos publiees - memoire long-terme du bot.

FAILLE CORRIGEE (2026-10-29) : data/analytics/latest.json est ECRASE a chaque
run (analytics.record), donc une video publiee le 1er octobre devenait
invisible le 5 octobre : le scheduler ne mesurait plus son engagement, le
hook A/B ne la joignait plus et l'optimizer ne l'apprenait plus. Seule la
DERNIERE video etait suivie.

Ce module tient un registre append-only de TOUTES les videos jamais
publiees (data/registry/published.json), avec le lien idea_id -> youtube_id
qui manque partout ailleurs, et il le retro-alimente depuis toutes les
sources historiques encore disponibles (hook_ab/assignments.jsonl,
analytics/history.jsonl, last_run.json, ideas/*.json, remake/*.json).

API :
  register(idea, video_id, cfg)      -> enregistre une video publiee
  all_ids(cfg)                       -> union des ids video connus
  map_video_to_idea(cfg)             -> {video_id: idea_id}
  enrich_from_idea(video_id, idea)   -> fusionne les metadonnees d'une idee
  backfill(cfg)                      -> reconstruit l'historique perdu
  report(cfg)                        -> resume lisible (CLI `brain`)
"""
import datetime as dt
import json
import re

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("registry")

# Id video raisonnable : alphanumerique court, sans espace ni URL.
# Volontairement plus permissif que les 11 caracteres YouTube : les
# sources legacy (history.jsonl, latest.json) sont nos propres donnees
# et doivent toujours etre recuperees, meme si le format derivait.
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def _dir(cfg):
    d = get_paths(cfg).registry
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(cfg):
    return _dir(cfg) / "published.json"


def load(cfg=None):
    """Charge le registre ({videos: {video_id: {...}}, updated})."""
    cfg = cfg or get_config()
    doc = load_json(_file(cfg), {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    videos = doc.get("videos")
    if not isinstance(videos, dict):
        videos = {}
    return {"videos": videos, "updated": doc.get("updated")}


def save(cfg, doc):
    doc = dict(doc or {})
    doc["videos"] = doc.get("videos") or {}
    doc["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    save_json(_file(cfg), doc)


def _idea_snapshot(idea):
    """Extrait les metadonnees apprissables d'une idee (ideas.py/remake.py)."""
    idea = idea or {}
    originality = idea.get("originality")
    if isinstance(originality, dict):
        orig = originality.get("max_jaccard", originality.get("max"))
    else:
        orig = originality
    try:
        orig = float(orig) if orig is not None else None
    except (TypeError, ValueError):
        orig = None
    try:
        density = int(idea.get("density"))
    except (TypeError, ValueError):
        density = None
    return {
        "idea_id": str(idea.get("id") or "") or None,
        "title": str(idea.get("title") or "") or None,
        "hook": str(idea.get("hook") or "") or None,
        "hook_variant": idea.get("hook_variant"),
        "pair_id": idea.get("pair_id"),
        "duration_format": idea.get("duration_format"),
        "density": density,
        "duration_target": idea.get("duration_target"),
        "generator": idea.get("generator"),
        "source_topic": idea.get("source_topic"),
        "source_short_id": idea.get("source_short_id"),
        "originality_max_jaccard": orig,
        "tags": list(idea.get("tags") or [])[:12] or None,
    }


def register(idea, video_id, cfg=None, published_at=None):
    """Enregistre (ou enrichit) une video publiee dans le registre.

    Fusion douce : les champs deja presents (ex. d'un backfill) ne sont
    jamais effaces par une valeur None. Renvoie l'entree finale.
    """
    cfg = cfg or get_config()
    doc = load(cfg)
    vid = str(video_id)
    entry = doc["videos"].setdefault(vid, {})
    snap = _idea_snapshot(idea)
    for key, val in snap.items():
        if val is not None:
            entry[key] = val
        else:
            # Schema stable : chaque cle canonique existe toujours (None
            # si inconnue), sans jamais effacer une valeur deja connue.
            entry.setdefault(key, None)
    entry.setdefault("published_at", published_at or
                     dt.datetime.now(dt.timezone.utc).isoformat())
    entry["video_id"] = vid
    entry.setdefault("source", "runtime")
    doc["videos"][vid] = entry
    save(cfg, doc)
    log.info("Registry: video %s enregistree (%s)", vid, entry.get("title"))
    return entry


def enrich_from_idea(video_id, idea, cfg=None):
    """Fusionne les metadonnees d'une idee dans une entree existante.

    Utilise au backfill : un video_id retrouve via hook_ab/last_run se
    raccorde a son fichier idea pour recuperer hook, pair_id, duree...
    Renvoie l'entree mise a jour (ou None si la video n'y est pas).
    """
    cfg = cfg or get_config()
    doc = load(cfg)
    vid = str(video_id or "")
    if not vid or vid not in doc["videos"]:
        return None
    snap = _idea_snapshot(idea)
    entry = doc["videos"][vid]
    for key, val in snap.items():
        if val is not None and not entry.get(key):
            entry[key] = val
    entry["idea_id"] = snap["idea_id"] or entry.get("idea_id")
    save(cfg, doc)
    return entry


def _candidate_video_id(*values):
    """Premiere valeur ressemblant a un id video (alphanumerique court)."""
    for v in values:
        s = str(v or "").strip()
        if YT_ID_RE.match(s):
            return s
    return None


def _idea_files(cfg):
    """Tous les fichiers idees/remakes tries (remake en priorite recente)."""
    paths = get_paths(cfg)
    out = []
    for d in (paths.remake, paths.ideas):
        try:
            for f in sorted(d.glob("*.json")):
                if f.name.startswith(("hits_", "remakes_", "latest")):
                    continue
                out.append(f)
        except OSError:
            continue
    return out


def _index_idea_files(cfg):
    """{idea_id: data} + {title_norm: data} depuis ideas/ et remake/."""
    by_id, by_title = {}, {}
    for path in _idea_files(cfg):
        try:
            data = load_json(path) or {}
        except Exception:
            continue
        iid = str(data.get("id") or "")
        if iid:
            by_id.setdefault(iid, data)
        title = str(data.get("title") or "").strip().lower()
        if title:
            by_title.setdefault(title, data)
    return by_id, by_title


def backfill(cfg=None, force=False):
    """Reconstruit l'historique perdu dans le registre (idempotent).

    Sources scannees (en ordre de richesse) :
      hook_ab/assignments.jsonl (pair_id + video_id + topic)
      analytics/history.jsonl   (id + title + publishedAt)
      analytics/latest.json     (id + title)
      last_run.json             (youtube_id + idea_id + title)
    Chaque video retrouvee est enrichie depuis les fichiers ideas/remake
    correspondants. Renvoie {added, enriched, total}.
    """
    cfg = cfg or get_config()
    doc = load(cfg)
    videos = doc["videos"]
    paths = get_paths(cfg)
    added = 0
    updated = 0
    before = set(videos)

    def _merge(vid, title=None, published_at=None, source="backfill",
               pair_id=None, topic=None, idea_id=None):
        nonlocal added, updated
        entry = videos.setdefault(vid, {})
        if entry.get("title") and title and title != entry.get("title"):
            entry.setdefault("alt_titles", []).append(title)
        if title:
            entry.setdefault("title", title)
        entry.setdefault("published_at", published_at or
                         dt.datetime.now(dt.timezone.utc).isoformat())
        entry.setdefault("source", source)
        if pair_id:
            entry.setdefault("pair_id", pair_id)
        if idea_id:
            entry.setdefault("idea_id", idea_id)
        if topic:
            entry.setdefault("source_topic", topic)

    # 1) hook A/B : les paires publiques ont deja leur video_id rattache.
    try:
        from .hook_ab import _load_assignments
        for row in _load_assignments(cfg):
            vid = _candidate_video_id(row.get("video_id"))
            if vid:
                _merge(vid, title=row.get("topic"), source="hook_ab",
                       pair_id=row.get("pair_id"), topic=row.get("topic"))
    except Exception:
        log.warning("backfill hook_ab rate (non fatal):\n%s",
                    _trace())

    # 2) historique analytics (lignes conservees depuis le jour 1).
    for hist in ("history.jsonl",):
        f = paths.analytics / hist
        if not f.exists():
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    vid = _candidate_video_id(row.get("id"))
                    if vid:
                        _merge(vid, title=row.get("title"),
                               published_at=row.get("published"),
                               source="history")
        except Exception:
            log.warning("backfill %s rate (non fatal): %s", hist, _trace())

    # 3) dernier snapshot stats (liste OU dict {"rows": [...]}).
    try:
        latest = load_json(paths.analytics / "latest.json", []) or []
        rows = latest if isinstance(latest, list) else latest.get("rows", [])
        for row in rows:
            if isinstance(row, dict):
                vid = _candidate_video_id(row.get("id"))
                if vid:
                    _merge(vid, title=row.get("title"),
                           published_at=row.get("published"),
                           source="legacy")
    except Exception:
        log.warning("backfill latest.json rate (non fatal): %s", _trace())

    # 4) dernier run (le plus riche : idea_id explicite).
    try:
        last = load_json(paths.data / "last_run.json", {}) or {}
        vid = _candidate_video_id(last.get("youtube_id"))
        if vid:
            _merge(vid, title=last.get("title"), source="last_run",
                   idea_id=last.get("idea"))
    except Exception:
        log.warning("backfill last_run rate (non fatal): %s", _trace())

    # 5) enrichissement via les fichiers ideas/remake (id ou titre).
    by_id, by_title = _index_idea_files(cfg)
    for vid in list(videos):
        entry = videos[vid]
        if entry.get("idea_id") and entry["idea_id"] in by_id:
            _apply_idea(entry, by_id[entry["idea_id"]])
        elif entry.get("title") and entry["title"].lower() in by_title:
            data = by_title[entry["title"].lower()]
            _apply_idea(entry, data)
            if data.get("id"):
                entry["idea_id"] = data["id"]

    after = set(videos)
    added = len(after - before)
    updated = len(after & before) if added else len(after)
    save(cfg, doc)
    log.info("Registry: %s entrees (%s nouvelles via backfill)", len(videos),
             added)
    return {"added": added, "total": len(videos), "videos": videos}


def _apply_idea(entry, data):
    snap = _idea_snapshot(data)
    for key, val in snap.items():
        if val is not None and not entry.get(key):
            entry[key] = val


def _trace():
    import traceback
    return traceback.format_exc()


def all_ids(cfg=None, refresh=False):
    """Tous les ids YouTube jamais connus (registre + fallback legacy).

    Le registre fait foi ; les sources legacy ne servent qu'au tout
    premier all_ids avant le premier backfill reussi (ex. registre
    perdu/recree). Ordre stable -> un meme id ne change jamais de place.
    """
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    if refresh:
        backfill(cfg)
    ids = []
    seen = set()

    def _push(vid):
        s = str(vid or "").strip()
        if s and s not in seen:
            seen.add(s)
            ids.append(s)

    for vid in load(cfg)["videos"]:
        _push(vid)
    try:
        latest = load_json(paths.analytics / "latest.json", []) or []
        rows = latest if isinstance(latest, list) else latest.get("rows", [])
        for row in rows:
            if isinstance(row, dict):
                _push(row.get("id"))
    except Exception:
        pass
    try:
        last = load_json(paths.data / "last_run.json", {}) or {}
        _push(last.get("youtube_id"))
    except Exception:
        pass
    return ids


def map_video_to_idea(cfg=None):
    """{video_id: idea_id} pour joindre les stats aux fichiers idees.

    C'est le maillon manquant de l'optimizer : latest.json parle en
    youtube_id, les fichiers ideas/remake parlent en idea_id.
    """
    doc = load(cfg)
    out = {}
    for vid, entry in doc["videos"].items():
        if entry.get("idea_id"):
            out[vid] = entry["idea_id"]
    return out


def report(cfg=None):
    """Resume lisible du registre (CLI `brain`)."""
    cfg = cfg or get_config()
    doc = load(cfg)
    videos = doc["videos"]
    total = len(videos)
    with_idea = sum(1 for v in videos.values() if v.get("idea_id"))
    with_pair = sum(1 for v in videos.values() if v.get("pair_id"))
    first = min((v.get("published_at") or "" for v in videos.values()),
                default="")
    return {"total": total, "linked_to_idea": with_idea,
            "linked_to_ab_pair": with_pair, "first_published": first,
            "file": str(_file(cfg))}
