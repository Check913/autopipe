# -*- coding: utf-8 -*-
"""Metriques reelles pour le paquet apex (auto-amelioration).

Joint la memoire de hooks (autopipe.hooks.memory, qui fusionne deja
registre + retention) avec le registre des publications pour produire
des lignes de mesure homogenes : reward (ratio de vues engagees),
famille de hook, format et quiz - les trois groupes de bras du bandit.

Seules les lignes provenant de vraies vues engagees
("engagedViews") comptent. Sans elles, la liste reste vide et le
paquet apex reste passif (mode degrade).
"""

import json
import traceback

from .. import identity
from .. import registry
from ..config import get_config, get_paths
from ..hooks import memory
from ..util import get_logger

log = get_logger("apex.metrics")

# Valeur de engaged_source prouvant des vues engagees reelles (pas un
# fallback sur la moyenne de la niche).
REAL_ENGAGEMENT_SOURCE = "engagedViews"

# Valeurs acceptees pour un quiz actif dans le registre.
_QUIZ_ON = (True, 1, "1", "on", "true", "True", "oui")


def has_real_engagement(entry):
    """Vrai si l'entree de memoire porte des vues engagees reelles."""
    if not isinstance(entry, dict):
        return False
    return entry.get("engaged_source") == REAL_ENGAGEMENT_SOURCE


def _norm_ratio(value):
    """Ramene un ratio en [0.0, 1.0] ; les pourcentages (> 1.5) sont /100."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1.5:
        v = v / 100.0
    if v < 0.0:
        v = 0.0
    if v > 1.0:
        v = 1.0
    return v


def _quiz_label(value):
    """Normalise l'etat du quiz en 'on' / 'off'."""
    if value in _QUIZ_ON:
        return "on"
    if isinstance(value, str) and value.strip().lower() in ("on", "true", "1", "oui"):
        return "on"
    return "off"


def measured_rows(cfg=None):
    """Construit les lignes mesurees (memoire x registre), vues reelles seulement.

    Retourne [] si aucune donnee reelle n'est disponible (mode degrade)
    ou si la jointure echoue : l'apprentissage reste alors passif.
    """
    cfg = cfg if cfg is not None else get_config()
    try:
        reg = registry.load(cfg) or {}
        videos = reg.get("videos") or {}
        doc = memory.build(cfg) or {}
        raw = doc.get("publisheds") or {}
        entries = raw.values() if isinstance(raw, dict) else raw
        rows = []
        for e in entries:
            if not has_real_engagement(e):
                continue
            reward = _norm_ratio(e.get("engaged_ratio"))
            if reward is None:
                continue
            vid = e.get("video_id")
            info = (videos or {}).get(vid) or {}
            fam = e.get("family") or e.get("hook_family") or info.get("hook_family")
            if not fam:
                hook = e.get("hook") or info.get("hook") or ""
                fam = identity.hook_family(hook) if hook else None
            fmt = str(info.get("duration_format") or "short").strip().lower() or "short"
            try:
                views = int(e.get("views") or 0)
            except (TypeError, ValueError):
                views = 0
            rows.append({
                "video_id": vid,
                "published_at": e.get("published_at") or "",
                "views": views,
                "reward": reward,
                "hook_family": fam,
                "duration_format": fmt,
                "quiz": _quiz_label(info.get("quiz")),
                "hook": e.get("hook") or info.get("hook") or "",
            })
        return rows
    except Exception:
        log.warning("apex.metrics: jointure impossible (mode degrade):\n%s",
                    traceback.format_exc())
        return []


def arms_for_row(row):
    """Identifiants des bras concernes par une ligne de mesure."""
    ids = []
    fam = row.get("hook_family")
    if fam:
        ids.append("family:%s" % fam)
    fmt = row.get("duration_format")
    if fmt:
        ids.append("format:%s" % fmt)
    quiz = row.get("quiz")
    if quiz:
        ids.append("quiz:%s" % quiz)
    return ids


def fed_ids(cfg=None):
    """video_id deja presents dans le journal (journal.jsonl)."""
    cfg = cfg if cfg is not None else get_config()
    try:
        path = get_paths(cfg).apex / "journal.jsonl"
    except Exception:
        return set()
    if not path.exists():
        return set()
    ids = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("video_id"):
                    ids.add(str(obj["video_id"]))
    except OSError as exc:
        log.warning("apex.metrics: journal illisible: %s", exc)
        return set()
    return ids
