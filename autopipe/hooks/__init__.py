# -*- coding: utf-8 -*-
"""Skill hook-generator-upgrade (2026-11-01) : package autopipe/hooks.

Trois gates de generation sur les hooks A/B publies :
  * prepublication_check : memoire + engagement T+72h + gate d'activation ;
  * diversity            : anti-doublon 7 jours (Jaccard) + anti-serie ;
  * performance          : famille gagnante sur engagedViews reels ;
  * exploration          : cycle deterministe 1/5 + taux aleatoire 0.3.

Point d'entree unique pour le scheduler (etape 1.98, automatique,
aucun geste humain a T+72h) :

    from . import hooks
    hooks.run(cfg)

run(cfg) est TOUJOURS non fatal : chaque etape est blindee, la memoire
n'est jamais corrompue par une erreur, et le mode degrade (pas encore
de Short age, pas de engagedViews reel) laisse l'ideation A/B
historique tourner a l'identique jusqu'a l'activation.
"""
import traceback

from ..config import get_config
from ..util import get_logger
from . import diversity, exploration, performance, prepublication_check

log = get_logger("hooks")


def run(cfg=None):
    """Orchestrateur scheduler : boucle pre-publication puis statuts.

    Toujours non fatal ; retourne un resume booleen.
    """
    cfg = cfg or get_config()
    try:
        res = prepublication_check.run(cfg) or {}
    except Exception:
        log.warning("[hooks] boucle pre-publication en echec (non fatal):\n%s",
                    traceback.format_exc())
        res = {"status": "error", "active": False}
    if not res.get("active"):
        log.info("[hooks] skill inactif (mode degrade, l'ideation A/B "
                 "historique continue) ; 0 changement de comportement")
    else:
        log.info("[hooks] skill actif : les gates de selection prennent "
                 "le relais dans ideas.py")
    for line_fn in (performance.line, exploration.line, diversity.line):
        try:
            line_fn(cfg)
        except Exception:
            log.warning("[hooks] ligne de statut en echec (non fatal):\n%s",
                        traceback.format_exc())
    return bool(res.get("active"))
