# -*- coding: utf-8 -*-
"""Veille YouTube pour le paquet apex : decouverte de patterns viraux.

Desactivee par defaut (apex.scrape.enabled = false). Quand elle est
activee, une recherche plate yt-dlp est effectuee periodiquement sur
PLUSIEURS requetes de la vraie niche (derivees de scout.keywords avec
un suffixe "#shorts", comme research.py : une recherche generique
"shorts" renvoie surtout des compilations longues, jamais filtrees),
puis les titres dedoublonnes sont classes en familles de hooks et
ajoutes a la base de connaissances (source "scrape"). Les filtres
(vues, duree) forment une echelle : si rien ne passe au filtre
strict, on retient les meilleurs candidats pour ne jamais renvoyer 0
quand YouTube a repondu. Un cache evite de frapper YouTube a chaque
cycle (apex.scrape.cache_hours).
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

# Nombre de resultats demandes a la recherche plate (par requete).
_SEARCH_COUNT = 50

# Nb max de requetes de niche par veille (3 x 50 resultats).
_MAX_QUERIES = 3


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


def _queries(cfg, scfg, raw=None):
    """Resout la liste des requetes de veille, par priorite :

    1. valeur passee (str ou liste) ; 2. scrape.query si elle decrit
    une vraie niche ; 3. mots-cles de niche (scout.keywords, puis
    research.keywords, dedoublonnes) avec suffixe "#shorts" : le
    format deja prouve par research.py, car la recherche generique
    "shorts" ramene surtout des compilations longues ;
    4. "shorts" en dernier recours.
    """
    if raw is None:
        raw = (scfg or {}).get("query")
    if isinstance(raw, (list, tuple)):
        qs = []
        for q in raw:
            q = str(q).strip()
            if q and q not in qs:
                qs.append(q)
        if qs:
            return qs[:_MAX_QUERIES]
        raw = None
    if raw is not None and str(raw).strip() \
            and str(raw).strip().lower() != "shorts":
        return [str(raw).strip()]
    kw = []
    for src in ((cfg or {}).get("scout"), (cfg or {}).get("research")):
        for k in ((src or {}).get("keywords") or []):
            k = str(k).strip()
            if k and k not in kw:
                kw.append(k)
    if kw:
        return ["%s #shorts" % k for k in kw[:_MAX_QUERIES]]
    return ["shorts"]


def _filter_entries(entries, min_views=0, max_duration=0):
    """Filtre des entrees brutes yt-dlp (vues min, duree max).

    min_views ou max_duration = 0 desactivent le filtre. Retourne
    [{id, title, views, duration_s, url}] triee par vues
    decroissantes (titres vides et entrees sans id exclus).
    """
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        try:
            views = int(entry.get("view_count") or 0)
        except (TypeError, ValueError):
            views = 0
        duration = entry.get("duration")
        if min_views > 0 and views < min_views:
            continue
        if max_duration > 0 and duration \
                and float(duration) > max_duration:
            continue
        out.append({
            "id": entry.get("id"),
            "title": title,
            "views": views,
            "duration_s": int(duration) if duration else None,
            "url": entry.get("url")
            or "https://www.youtube.com/watch?v=%s" % entry.get("id"),
        })
    out.sort(key=lambda r: r.get("views") or 0, reverse=True)
    return out


def search(cfg=None, query=None):
    """Recherche plate yt-dlp multi-requetes, filtres progressifs.

    Requetes resolues par _queries (vraie niche). Les filtres
    (min_views, max_duration) forment une echelle : stricts d'abord,
    puis vues seules, puis aucun filtre ; la premiere etape qui
    produit des candidats gagne (jamais 0 si YouTube a repondu).
    Retourne une liste triee par vues decroissantes :
    [{id, title, views, duration_s, url}] ; [] si yt-dlp manque ou si
    la recherche echoue (la veille n'est jamais fatale).
    """
    cfg = cfg if cfg is not None else get_config()
    scfg = _scrape_cfg(cfg)
    queries = _queries(cfg, scfg, query)
    min_views = int(scfg.get("min_views", 1000000) or 0)
    max_duration = int(scfg.get("max_duration", 120) or 0)
    top_n = int(scfg.get("top_n", 20) or 20)
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        log.info("apex.scraper: yt-dlp indisponible, veille ignoree")
        return []
    raw = []
    seen = set()
    try:
        with YoutubeDL(_ydl_opts()) as ydl:
            for q in queries:
                try:
                    info = ydl.extract_info(
                        "ytsearch%d:%s" % (_SEARCH_COUNT, q), download=False)
                except Exception:
                    log.warning("apex.scraper: requete echouee: %r", q)
                    continue
                for entry in ((info or {}).get("entries") or []):
                    key = entry.get("id") if isinstance(entry, dict) else None
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    raw.append(entry)
        for min_v, max_d, label in (
                (min_views, max_duration, "vues+duree"),
                (min_views, 0, "vues seules"),
                (0, 0, "aucun filtre")):
            results = _filter_entries(raw, min_v, max_d)
            if results:
                log.info("apex.scraper: %d candidats sur %d resultats "
                         "(filtres: %s)", len(results), len(raw), label)
                return results[:top_n]
        log.info("apex.scraper: 0 candidat sur %d resultats bruts "
                 "(yt-dlp a repondu mais tout est filtre ou vide)", len(raw))
        return []
    except Exception:
        log.warning("apex.scraper: recherche echouee (non fatal):\n%s",
                    traceback.format_exc())
        return []


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
    queries = []
    entries = []
    try:
        queries = _queries(cfg, scfg, scfg.get("query"))
        entries = search(cfg, queries)
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
    if not entries:
        # Resultat vide : rien n'est mis en cache, le prochain cycle
        # retentera (un cache "0 pattern" bloquerait la veille pendant
        # cache_hours).
        return added
    try:
        save_json(path, {
            "scraped_at": now.isoformat(),
            "added": added,
            "query": queries[0] if queries else "",
            "queries": queries,
        })
    except Exception:
        log.warning("apex.scraper: cache non sauvegarde:\n%s", traceback.format_exc())
    return added
