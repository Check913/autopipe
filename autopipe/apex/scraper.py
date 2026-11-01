# -*- coding: utf-8 -*-
"""Veille YouTube pour le paquet apex : decouverte de patterns viraux.

Desactivee par defaut (apex.scrape.enabled = false). Quand elle est
activee, une recherche plate yt-dlp est effectuee periodiquement sur la
niche, filtree par vues minimales et duree maximale, puis les titres
dedoublonnes sont classes en familles de hooks et ajoutes a la base de
connaissances (source "scrape"). Un cache evite de frapper YouTube a
chaque cycle (apex.scrape.cache_hours).
"""

import datetime
import traceback

from .. import identity
from ..config import get_config, get_paths
from ..util import get_logger, load_json, save_json
from . import knowledge_base

log = get_logger("apex.scraper")

# Familles de hooks reconnues (meme liste que autopipe.identity).
_FAMILIES = ("question", "number", "negation", "statement")

# Nombre de resultats demandes a la recherche plate.
_SEARCH_COUNT = 50


def _scrape_cfg(cfg):
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    return apex_cfg.get("scrape", {}) or {}


def cache_file(cfg=None):
    """Chemin du cache de derniere veille."""
    cfg = cfg if cfg is not None else get_config()
    return get_paths(cfg).apex / "scraper_last.json"


def _ydl_opts():
    """Options yt-dlp : recherche plate, aucun telechargement."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
        "noplaylist": True,
    }


def search(cfg=None, query=None):
    """Recherche plate yt-dlp filtree (vues, duree).

    Retourne une liste triee par vues decroissantes :
    [{id, title, views, duration_s, url}] ; [] si yt-dlp manque ou si
    la recherche echoue (la veille n'est jamais fatale).
    """
    cfg = cfg if cfg is not None else get_config()
    scfg = _scrape_cfg(cfg)
    query = str(query or scfg.get("query") or "shorts")
    min_views = int(scfg.get("min_views", 1000000) or 0)
    max_duration = int(scfg.get("max_duration", 90) or 0)
    top_n = int(scfg.get("top_n", 20) or 20)
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        log.info("apex.scraper: yt-dlp indisponible, veille ignoree")
        return []
    results = []
    try:
        with YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info("ytsearch%d:%s" % (_SEARCH_COUNT, query),
                                    download=False)
        for entry in ((info or {}).get("entries") or []):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            try:
                views = int(entry.get("view_count") or 0)
            except (TypeError, ValueError):
                views = 0
            duration = entry.get("duration")
            if min_views > 0 and views < min_views:
                continue
            if max_duration > 0 and duration and float(duration) > max_duration:
                continue
            results.append({
                "id": entry.get("id"),
                "title": str(entry.get("title") or "").strip(),
                "views": views,
                "duration_s": int(duration) if duration else None,
                "url": entry.get("url")
                or "https://www.youtube.com/watch?v=%s" % entry.get("id"),
            })
            if len(results) >= top_n:
                break
        results.sort(key=lambda r: r.get("views") or 0, reverse=True)
        return results
    except Exception:
        log.warning("apex.scraper: recherche echouee (non fatal):\n%s",
                    traceback.format_exc())
        return results


def _parse_iso(value):
    """Parse une date ISO ; None si impossible."""
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def pick_patterns(entries, max_patterns=10):
    """Classe des titres dedoublonnes en familles de hooks.

    Retourne [(titre, "family:<famille>")] ; les titres sans famille
    reconnue sont ignores.
    """
    seen = set()
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        key = " ".join(title.lower().split())
        if key in seen:
            continue
        seen.add(key)
        fam = identity.hook_family(title)
        if fam not in _FAMILIES:
            continue
        out.append((title, "family:%s" % fam))
        if len(out) >= int(max_patterns):
            break
    return out


def maybe_scrape(cfg=None, force=False):
    """Veille periodique vers la base de connaissances.

    Retourne None si la veille est desactivee, 0 si le cache est frais,
    sinon le nombre de nouveaux patterns ajoutes. Jamais fatale.
    """
    cfg = cfg if cfg is not None else get_config()
    scfg = _scrape_cfg(cfg)
    if not scfg.get("enabled", False):
        return None
    now = datetime.datetime.now()
    path = cache_file(cfg)
    hours = float(scfg.get("cache_hours", 72) or 72)
    if not force and path.exists():
        try:
            doc = load_json(path, None) or {}
            when = _parse_iso(doc.get("scraped_at"))
            if when is not None and (now - when).total_seconds() < hours * 3600.0:
                return 0
        except Exception:
            pass
    added = 0
    try:
        entries = search(cfg, scfg.get("query", "shorts"))
        patterns = pick_patterns(entries, int(scfg.get("max_patterns", 10) or 10))
        doc = knowledge_base.load(cfg)
        for text, arm in patterns:
            if knowledge_base.add_pattern(cfg, doc, text, arm, "scrape"):
                added += 1
        knowledge_base.save(cfg, doc)
    except Exception:
        log.warning("apex.scraper: veille echouee (non fatal):\n%s",
                    traceback.format_exc())
        return added
    try:
        save_json(path, {
            "scraped_at": now.isoformat(),
            "added": added,
            "query": scfg.get("query", "shorts"),
        })
    except Exception:
        log.warning("apex.scraper: cache non sauvegarde:\n%s", traceback.format_exc())
    return added
