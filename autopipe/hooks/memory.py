# -*- coding: utf-8 -*-
"""Memoire de hooks (SKILL hook-generator-upgrade, 2026-11-01).

Construit data/hooks/hook_memory.json : le passe complet de chaque Short
publie, utile aux trois gates de generation (diversite / performance /
exploration). Trois sources sont fusionnees :

  1. registre data/registry/published.json (source de verite : ce qui a
     ete publie, avec published_at) ;
  2. data/hook_ab/assignments.jsonl (hook / famille / variante A-B,
     rattachable au video_id apres publication - jointure video_id puis
     pair_id pour rattraper les entrees legacy sans hook) ;
  3. data/analytics/retention_latest.json (engagement engagedViews/views
     par video_id, deja rafraichi par analytics.refresh_retention en
     debut de run scheduler).

Le document ne contient AUCUN secret et reste rechargeable en lecture
seule par ideas.py (memory.load) sans refaire les jointures.
"""
import datetime as dt
import re

from ..config import get_config, get_paths
from ..hook_ab import _load_assignments
from ..identity import hook_family
from ..registry import load as registry_load
from ..util import get_logger, load_json, save_json

log = get_logger("hooks.memory")

# Fenetre "recent" pour la memoire des hooks (jours). Recalculee depuis
# [hooks].recent_days mais conservée comme valeur defaut du module.
RECENT_DAYS = 7


def _opts(cfg):
    return (cfg or {}).get("hooks") or {}


def _opt(cfg, key, default):
    return (_opts(cfg) or {}).get(key, default)


def path(cfg=None):
    return get_paths(cfg).hooks / "hook_memory.json"


def _now():
    return dt.datetime.now(dt.timezone.utc)


def now_iso():
    return _now().isoformat(timespec="seconds")


def parse_ts(value):
    """Parse un timestamp ISO en datetime aware (UTC sinon). None si invalide."""
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def norm_hook(text):
    return " ".join(str(text or "").lower().split())


def _entries(doc):
    """Entres triees chronologiquement (published_at inconnu a la fin)."""
    out = [v for v in ((doc or {}).get("publisheds") or {}).values()
           if isinstance(v, dict)]
    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    out.sort(key=lambda e: parse_ts(e.get("published_at")) or far)
    return out


def load(cfg=None):
    """Charge la memoire depuis le disque (doc vide si absente)."""
    cfg = cfg or get_config()
    doc = load_json(path(cfg), {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("publisheds"), dict):
        doc["publisheds"] = {}
    return doc


def save(cfg, doc):
    cfg = cfg or get_config()
    save_json(path(cfg), doc or {})


def build(cfg=None):
    """Construit le document memoire complet (lecture seule, sans ecrire).

    Fusion registre + assignments.jsonl + retention_latest.json. Les
    champs deja connus ne sont jamais ecrases ; les familles manquantes
    sont deduites du texte du hook (identity.hook_family).
    """
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    publisheds = {}

    # 1) Registre : source de verite des videos publiees.
    try:
        reg = registry_load(cfg)
    except Exception:
        reg = {"videos": {}}
    for vid, entry in ((reg.get("videos") or {}).items()):
        vid = str(vid)
        hook = entry.get("hook")
        family = entry.get("hook_family") or (hook_family(hook) if hook else None)
        publisheds[vid] = {
            "video_id": vid,
            "published_at": entry.get("published_at"),
            "hook": hook,
            "family": family,
            "hook_variant": entry.get("hook_variant"),
            "pair_id": entry.get("pair_id"),
            "title": entry.get("title"),
            "topic": entry.get("source_topic"),
            "views": None,
            "engagedViews": None,
            "engaged_ratio": None,
            "engaged_source": None,
            "avg_view_pct": None,
            "engagement": None,
        }

    # 2) assignments.jsonl : backfill hook/famille/variante pour les
    #    entrees legacy du registre qui n'ont pas de hook, et creation
    #    d'entrees squelettes pour les paires attachees a une video
    #    absente du registre (published_at inconnu -> jamais "agee").
    try:
        rows = _load_assignments(cfg) or []
    except Exception:
        rows = []
    by_vid = {str(v): e for v, e in publisheds.items()}
    by_pair = {}
    for vid, e in publisheds.items():
        if e.get("pair_id") and e["pair_id"] not in by_pair:
            by_pair[e["pair_id"]] = e
    for row in rows:
        if not isinstance(row, dict):
            continue
        target = by_vid.get(str(row.get("video_id") or "")) \
            or by_pair.get(row.get("pair_id"))
        if target is None and row.get("video_id"):
            target = {
                "video_id": str(row.get("video_id")),
                "published_at": None,
                "hook": None,
                "family": None,
                "hook_variant": None,
                "pair_id": row.get("pair_id"),
                "title": None,
                "topic": row.get("topic"),
                "views": None, "engagedViews": None, "engaged_ratio": None,
                "engaged_source": None, "avg_view_pct": None,
                "engagement": None,
            }
            publisheds[target["video_id"]] = target
            by_vid[target["video_id"]] = target
        if target is None:
            continue
        for key, val in (("hook", row.get("hook")),
                         ("family", row.get("family")),
                         ("hook_variant", row.get("variant")),
                         ("pair_id", row.get("pair_id")),
                         ("topic", row.get("topic"))):
            if val and not target.get(key):
                target[key] = val
        if target.get("hook") and not target.get("family"):
            target["family"] = hook_family(target["hook"])

    # 3) retention_latest.json : engagement par video_id.
    try:
        ret_doc = load_json(paths.analytics / "retention_latest.json", {}) or {}
        ret_rows = ret_doc.get("rows") or []
    except Exception:
        ret_rows = []
    by_ret = {str(r.get("id")): r for r in ret_rows if r.get("id")}
    if by_ret:
        from .. import analytics as _analytics  # import tardif (google libs)
        for vid, entry in publisheds.items():
            r = by_ret.get(vid)
            if not r:
                continue
            try:
                entry["views"] = int(r.get("views") or 0)
            except (TypeError, ValueError):
                entry["views"] = None
            try:
                entry["engagedViews"] = int(r.get("engagedViews") or 0)
            except (TypeError, ValueError):
                entry["engagedViews"] = None
            entry["engaged_ratio"] = r.get("engaged_ratio")
            entry["engaged_source"] = r.get("engaged_source")
            entry["avg_view_pct"] = r.get("avg_view_pct")
            entry["engagement"] = _analytics.engagement_value(r)

    doc = {"updated": now_iso(), "publisheds": publisheds,
           "last_checked": None}
    return doc


def used_hook_texts(cfg=None, doc=None, days=None):
    """Hooks normalises publies dans la fenetre recente (jours).

    Une entree sans published_at (age inconnu, backfill legacy) est
    traitee comme potentiellement recente -> incluse (dette prudente :
    mieux vaut refuser un hook par exces que republier un doublon).
    """
    cfg = cfg or get_config()
    doc = doc if doc is not None else load(cfg)
    days = int(days if days is not None else _opt(cfg, "recent_days", RECENT_DAYS))
    cutoff = _now() - dt.timedelta(days=days)
    out = []
    for e in _entries(doc):
        ts = parse_ts(e.get("published_at"))
        if ts is not None and ts < cutoff:
            continue
        h = norm_hook(e.get("hook"))
        if h:
            out.append(h)
    return out


def recent_families(cfg=None, doc=None, days=None):
    """Familles de hooks publiees dans la fenetre recente, dans l'ordre."""
    cfg = cfg or get_config()
    doc = doc if doc is not None else load(cfg)
    days = int(days if days is not None else _opt(cfg, "recent_days", RECENT_DAYS))
    cutoff = _now() - dt.timedelta(days=days)
    fams = []
    for e in _entries(doc):
        ts = parse_ts(e.get("published_at"))
        if ts is not None and ts < cutoff:
            continue
        if e.get("family"):
            fams.append(e["family"])
    return fams


def consecutive_family(doc, family=None):
    """Nb d'entrees les plus recentes partageant cette famille.

    family=None -> famille de la derniere entree connue. Compte sur
    l'historique complet (semantique "consecutif", comme compliance
    fatigue_max_consecutive).
    """
    fams = [e.get("family") for e in _entries(doc) if e.get("family")]
    if family is None:
        family = fams[-1] if fams else None
    if not family:
        return 0
    streak = 0
    for fam in reversed(fams):
        if fam == family:
            streak += 1
        else:
            break
    return streak


def stats_by_family(cfg=None, doc=None):
    """{famille: {n, engagement_mean, views_mean}} sur les Shorts mesures."""
    cfg = cfg or get_config()
    doc = doc if doc is not None else load(cfg)
    acc = {}
    for e in _entries(doc):
        fam = e.get("family")
        eng = e.get("engagement")
        if not fam or not isinstance(eng, (int, float)):
            continue
        s = acc.setdefault(fam, {"n": 0, "eng_sum": 0.0, "views_sum": 0})
        s["n"] += 1
        s["eng_sum"] += float(eng)
        s["views_sum"] += int(e.get("views") or 0)
    out = {}
    for fam, s in acc.items():
        out[fam] = {"n": s["n"],
                    "engagement_mean": round(s["eng_sum"] / s["n"], 1),
                    "views_mean": round(s["views_sum"] / s["n"])}
    return out


def n_with_engagement(cfg=None, doc=None):
    """Nb d'entrees avec VRAI engagedViews (engaged_source == engagedViews)."""
    cfg = cfg or get_config()
    doc = doc if doc is not None else load(cfg)
    return sum(1 for e in _entries(doc)
               if e.get("engaged_source") == "engagedViews"
               and (e.get("views") or 0) > 0)


def next_seq(cfg=None, doc=None):
    """Rang (1-based) du prochain Short pour le cycle exploration."""
    cfg = cfg or get_config()
    doc = doc if doc is not None else load(cfg)
    return len(_entries(doc)) + 1
