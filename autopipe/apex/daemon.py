# -*- coding: utf-8 -*-
"""Daemon d'apprentissage du paquet apex : cycle 24/7 et journal.

Un cycle (apex.daemon.run_cycle) :
1. verifie que l'apprentissage est actif (donnees reelles) ;
2. instantanise l'etat courant (garde-fou) ;
3. fait le post-mortem : bandit + base de connaissances, et journalise
   chaque nouvelle publication apprise (jsonl avec rotation) ;
4. lance la veille facultative (desactivee par defaut) ;
5. verifie la sante et restaure si besoin ;
6. injecte la directive d'apprentissage dans ideas/remake.
"""

import datetime
import json
import traceback

from ..config import get_config, get_paths
from ..util import get_logger
from . import bandit
from . import knowledge_base
from . import metrics
from . import post_mortem
from . import rollback
from . import scraper

log = get_logger("apex.daemon")


def journal_file(cfg=None):
    """Chemin du journal d'apprentissage (data/apex/journal.jsonl)."""
    cfg = cfg if cfg is not None else get_config()
    return get_paths(cfg).apex / "journal.jsonl"


def journal(cfg, line):
    """Ajoute une ligne JSON au journal puis applique la rotation."""
    try:
        path = journal_file(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("apex.daemon: journal inaccessible: %s", exc)
        return
    _rotate(cfg)


def _rotate(cfg):
    """Rotation : garde les journal_max dernieres lignes du journal."""
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    try:
        max_lines = int(apex_cfg.get("journal_max", 3000) or 3000)
    except (TypeError, ValueError):
        max_lines = 3000
    try:
        path = journal_file(cfg)
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= max_lines:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-max_lines:])
    except OSError as exc:
        log.warning("apex.daemon: rotation du journal impossible: %s", exc)


def active(cfg=None):
    """Vrai si l'apprentissage peut tourner (active + donnees reelles)."""
    cfg = cfg if cfg is not None else get_config()
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    if not apex_cfg.get("enabled", True):
        return False
    try:
        return bool(metrics.measured_rows(cfg))
    except Exception:
        return False


def inject(cfg, directive):
    """Injecte la directive apex dans ideas/remake (dedoublonnee).

    Meme convention que autopipe.research.inject_directive : jointure
    avec " | " et garde-fou anti-doublon.
    """
    if not directive:
        return False
    changed = False
    for key in ("ideas", "remake"):
        section = cfg.get(key)
        if not isinstance(section, dict):
            continue
        current = section.get("style_directive") or ""
        if directive in current:
            continue
        section["style_directive"] = ("%s | %s" % (current, directive)) if current else directive
        changed = True
    if changed:
        log.info("Directive apex injectee")
    return changed


def run_cycle(cfg=None, force=False):
    """Un cycle complet d'apprentissage. Jamais fatal.

    Retourne un resume ; mode passif {"active": False, ...} si
    desactive ou sans donnees d'engagement reelles (sauf force=True).
    """
    cfg = cfg if cfg is not None else get_config()
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    if not apex_cfg.get("enabled", True):
        return {"active": False, "reason": "desactive"}
    try:
        rows = metrics.measured_rows(cfg)
    except Exception:
        log.warning("apex.daemon: metriques indisponibles:\n%s",
                    traceback.format_exc())
        rows = []
    if not rows and not force:
        return {"active": False, "reason": "aucune donnee reelle"}
    result = {"active": True, "rows": len(rows)}
    # 1. Instantane de securite avant tout changement.
    try:
        result["snapshot"] = rollback.snapshot(cfg, label="cycle")
    except Exception:
        log.warning("apex.daemon: instantane echoue (non fatal):\n%s",
                    traceback.format_exc())
        result["snapshot"] = None
    # 2. Post-mortem : bandit + base de connaissances.
    try:
        pm = post_mortem.analyze(cfg, rows=rows)
        result["learned"] = pm.get("learned", 0)
        result["winners"] = pm.get("winners", {})
        result["losers"] = pm.get("losers", {})
    except Exception:
        log.warning("apex.daemon: post-mortem echoue (non fatal):\n%s",
                    traceback.format_exc())
        result["learned"] = 0
    # La journalisation est faite par le post-mortem lui-meme (une ligne
    # par publication apprise), ce qui evite tout double apprentissage.
    # 3. Veille facultative (desactivee par defaut).
    try:
        result["scraped"] = scraper.maybe_scrape(cfg)
    except Exception:
        log.warning("apex.daemon: veille echouee (non fatal):\n%s",
                    traceback.format_exc())
        result["scraped"] = None
    # 4. Garde-fou : retour arriere si la sante degrade.
    try:
        rb = rollback.maybe_rollback(cfg)
        if rb:
            result["rollback"] = rb
    except Exception:
        log.warning("apex.daemon: rollback echoue (non fatal):\n%s",
                    traceback.format_exc())
    # 5. Directive d'apprentissage depuis la base de connaissances.
    try:
        directive = knowledge_base.build_directive(cfg)
        if directive:
            inject(cfg, directive)
            result["directive"] = directive
    except Exception:
        log.warning("apex.daemon: injection echouee (non fatal):\n%s",
                    traceback.format_exc())
    return result


def report(cfg=None):
    """Rapport lisible pour la CLI (autopipe.cli cmd_apex)."""
    cfg = cfg if cfg is not None else get_config()
    try:
        rows_count = len(metrics.measured_rows(cfg))
    except Exception:
        rows_count = 0
    try:
        doc = knowledge_base.load(cfg) or {}
        patterns = doc.get("patterns") or []
        winners = [p for p in patterns
                   if p.get("active", True) and int(p.get("wins") or 0) > 0]
    except Exception:
        patterns, winners = [], []
    lines = 0
    try:
        path = journal_file(cfg)
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                lines = sum(1 for _ in fh)
    except OSError:
        lines = 0
    try:
        bandit_doc = bandit.summary(cfg)
    except Exception:
        bandit_doc = {}
    return {
        "active": active(cfg),
        "rows": rows_count,
        "health": rollback.health(cfg),
        "knowledge_patterns": len(patterns),
        "knowledge_winners": len(winners),
        "journal_lines": lines,
        "snapshots": rollback.list_snapshots(cfg),
        "bandit": bandit_doc.get("arms", {}),
    }
