# -*- coding: utf-8 -*-
"""Paquet apex : predateur auto-ameliorant d'AutoPipe.

Boucle d'apprentissage 24/7 en 6 temps (tous non fatals) :
1. metriques : jointure memoire de hooks x registre (vrai engagement) ;
2. snapshot de securite de l'etat courant ;
3. post-mortem : gagnants vs perdants -> bandit de Thompson + base
   de connaissances de hooks ;
4. scrappe YouTube de nouveaux patterns (desactivee par defaut) ;
5. rollback automatique si la sante degrade par rapport au record ;
6. directive d'apprentissage (anglais) injectee dans ideas/remake.

Design preventif : aucune ecriture hors data/apex/, et aucune
exception ne remonte au pipeline principal (mode passif sans
donnees d'engagement reelles).
"""

import traceback

from ..util import get_logger

log = get_logger("apex")


def run(cfg=None, force=False):
    """Point d'entree unique (scheduler, etape 1.99). Jamais fatal."""
    try:
        from .daemon import run_cycle

        return run_cycle(cfg, force=force)
    except Exception:
        log.warning("Apex a echoue (non fatal):\n%s", traceback.format_exc())
        return {"active": False, "error": "exception"}


def report(cfg=None):
    """Rapport d'etat pour la CLI (cmd_apex). Jamais fatal."""
    try:
        from .daemon import report as _report

        return _report(cfg)
    except Exception:
        log.warning("Apex report a echoue (non fatal):\n%s",
                    traceback.format_exc())
        return {"active": False, "error": "exception"}


def rollback_cmd(cfg=None, name=None):
    """Restaure un instantane (le plus recent si name=None). Jamais fatal."""
    try:
        from . import rollback as _rollback

        return _rollback.restore(cfg, name)
    except Exception:
        log.warning("Apex rollback a echoue (non fatal):\n%s",
                    traceback.format_exc())
        return None


def scrape(cfg=None, force=False):
    """Veille manuelle : nouveaux patterns de la base. Jamais fatal."""
    try:
        from . import scraper as _scraper

        return _scraper.maybe_scrape(cfg, force=force)
    except Exception:
        log.warning("Apex scrape a echoue (non fatal):\n%s",
                    traceback.format_exc())
        return 0
