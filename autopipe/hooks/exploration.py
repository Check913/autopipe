# -*- coding: utf-8 -*-
"""Gate d'exploration du skill hook-generator-upgrade (2026-11-01).

Deux leviers d'exploration, parametres dans [hooks] :

  * explore_every (5) : cycle deterministe 1 Short explore tous les
    N (rang du Short : (n - 1) % N == 0, soit n % N == 1) ;
  * exploration_rate (0.3) : tirage aleatoire complementaire.

Un tour "explore" choisit la variante/la famille la plus loin de la
famille gagnante connue (sinon l'alternative A/B), pour continuer
d'apprendre. Un tour "exploit" garde la variante la plus performante.
Exploration/exploitation sur un cycle de 5 Shorts.
"""
import random

from ..util import get_logger
from . import memory
from .performance import best_family, min_n

log = get_logger("hooks.exploration")


def _opts(cfg):
    return (cfg or {}).get("hooks") or {}


def _opt(cfg, key, default):
    return (_opts(cfg) or {}).get(key, default)


def is_exploration_turn(cfg=None, doc=None, n=None):
    """True si ce rang de publication est un tour d'exploration.

    Cycle deterministe d'abord ((n - 1) % explore_every == 0), puis
    tirage exploration_rate pour les rangs hors cycle.
    """
    cfg = cfg or get_config()
    doc = doc if doc is not None else memory.load(cfg)
    if n is None:
        n = memory.next_seq(cfg, doc)
    every = int(_opt(cfg, "explore_every", 5) or 0)
    if every > 0 and (n - 1) % every == 0:
        return True
    rate = float(_opt(cfg, "exploration_rate", 0.3))
    if rate > 0 and random.random() < rate:
        return True
    return False


def choose_variant(topic, variants, cfg=None, doc=None, exploring=None):
    """Choisit la variante A/B selon le mode du tour.

    - exploration : famille la plus ELOIGNEE de la famille gagnante
      connue (sinon la 2e variante, la B) ;
    - exploitation : famille gagnante connue (sinon la 1re, la A).
    Retour : (hook, family, variant_label, exploring).
    """
    cfg = cfg or get_config()
    doc = doc if doc is not None else memory.load(cfg)
    if not isinstance(variants, list) or not variants:
        return None
    if exploring is None:
        exploring = is_exploration_turn(cfg, doc)
    best, _ = best_family(cfg, doc)
    if exploring:
        if best:
            far = [v for v in variants if v.get("family") != best]
            pick = far[0] if far else variants[-1]
        else:
            pick = variants[-1] if len(variants) > 1 else variants[0]
    else:
        if best:
            same = [v for v in variants if v.get("family") == best]
            pick = same[0] if same else variants[0]
        else:
            pick = variants[0]
    return pick["hook"], pick.get("family"), pick.get("variant"), exploring


def line(cfg=None):
    """Ligne [exploration] pour le log scheduler (non fatal)."""
    try:
        cfg = cfg or get_config()
        doc = memory.load(cfg)
        n = memory.next_seq(cfg, doc)
        every = int(_opt(cfg, "explore_every", 5) or 0)
        turn = "exploration" if is_exploration_turn(cfg, doc, n) else "exploitation"
        return ("[exploration] rang %s : tour %s (cycle %s, taux %s)") % (
            n, turn, every or "inf",
            _opt(cfg, "exploration_rate", 0.3))
    except Exception as exc:
        return "[exploration] statut indisponible (%s)" % exc
