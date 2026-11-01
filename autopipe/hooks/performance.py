# -*- coding: utf-8 -*-
"""Gate de performance du skill hook-generator-upgrade (2026-11-01).

Classe les familles de hooks par engagement reel
(engagedViews / views, engagement_value d'analytics : engaged_ratio,
repli avg_view_pct). performance_min_n (3) Short mesures par famille
sont requis avant qu'une famille soit declaree "gagnante" ; tant que
l'echantillon n'est pas suffisant, best_family retourne None et l'idee
explique que les decisions restent exploration.

Aucun appel reseaux ici : performance lit hook_memory.json (alimente
par prepublication_check chaque run).
"""
from ..config import get_config
from ..util import get_logger
from . import memory

log = get_logger("hooks.performance")


def _opts(cfg):
    return (cfg or {}).get("hooks") or {}


def _opt(cfg, key, default):
    return (_opts(cfg) or {}).get(key, default)


def min_n(cfg=None):
    return int(_opt(cfg, "performance_min_n", 3))


def best_family(cfg=None, doc=None):
    """(famille, stats) de la famille la mieux classee, ou (None, stats).

    Exige >= performance_min_n Shorts mesures (engaged_source ==
    "engagedViews", views > 0) pour designer une gagnante. Sur des
    echantillons insuffisants, les moyennes restent informatives.
    """
    cfg = cfg or get_config()
    doc = doc if doc is not None else memory.load(cfg)
    stats = memory.stats_by_family(cfg, doc)
    n_req = min_n(cfg)
    eligible = {fam: s for fam, s in stats.items() if s["n"] >= n_req}
    if not eligible:
        return None, stats
    fam = max(eligible, key=lambda f: eligible[f]["engagement_mean"])
    return fam, stats


def line(cfg=None):
    """Ligne [performance] pour le log scheduler (non fatal)."""
    try:
        cfg = cfg or get_config()
        doc = memory.load(cfg)
        n = memory.n_with_engagement(cfg, doc)
        fam, stats = best_family(cfg, doc)
        n_req = min_n(cfg)
        if fam:
            return ("[performance] %s Short(s) mesures ; famille gagnante : "
                    "%s (engagement moyen %s%% sur %s Shorts)") % (
                n, fam, stats[fam]["engagement_mean"], stats[fam]["n"])
        detail = ", ".join("%s=%s%% (%s)" % (f, s["engagement_mean"], s["n"])
                           for f, s in sorted(stats.items()))
        return ("[performance] %s Short(s) mesures (< %s requis pour "
                "designer une gagnante) ; moyennes par famille : %s") % (
            n, n_req, detail or "aucune")
    except Exception as exc:
        return "[performance] statut indisponible (%s)" % exc
