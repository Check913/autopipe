# -*- coding: utf-8 -*-
"""Post-mortem pour le paquet apex : apprendre de chaque publication.

Pour chaque ligne de mesure pas encore apprise (identifiee par le
journal) :
- mise a jour du bandit de Thompson avec la recompense reelle ;
- mise a jour de la base de connaissances (wins/losses des patterns,
  retrait des patterns a la deroute) ;
- ajout des hooks gagnants a la base (source "post_mortem") ;
- agregation des gagnants et perdants par groupe de decision.
"""

import datetime
import traceback

from ..config import get_config
from ..util import get_logger
from . import bandit
from . import knowledge_base
from . import metrics

log = get_logger("apex.post_mortem")

# Groupes de decision analyses (cles des lignes de mesure).
GROUPS = ("hook_family", "duration_format", "quiz")


def is_winner(row, threshold=0.7):
    """Vrai si le reward de la ligne atteint le seuil (echelle [0, 1])."""
    reward = row.get("reward")
    if reward is None:
        return False
    try:
        return float(reward) >= float(threshold)
    except (TypeError, ValueError):
        return False


def _threshold(cfg):
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    return float(apex_cfg.get("threshold", 70.0) or 70.0) / 100.0


def _retire_failures(cfg):
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    return int(apex_cfg.get("retire_failures", 10) or 10)


def analyze(cfg=None, rows=None, threshold=None, retire_failures=None):
    """Apprend des lignes mesurees non deja apprises.

    Retourne {"learned": n, "threshold": t, "winners": {...}, "losers":
    {...}} ; gagnants et perdants sont agreges par groupe. Jamais fatale.
    """
    cfg = cfg if cfg is not None else get_config()
    try:
        if threshold is None:
            threshold = _threshold(cfg)
        if retire_failures is None:
            retire_failures = _retire_failures(cfg)
        if rows is None:
            rows = metrics.measured_rows(cfg)
        already = metrics.fed_ids(cfg)
        doc = knowledge_base.load(cfg)
        result = {"learned": 0, "threshold": threshold,
                  "winners": {}, "losers": {}}
        for row in rows or []:
            vid = str(row.get("video_id") or "")
            if not vid or vid in already:
                continue
            try:
                reward = float(row.get("reward") or 0.0)
            except (TypeError, ValueError):
                continue
            arm_ids = metrics.arms_for_row(row)
            win = is_winner(row, threshold)
            try:
                bandit.update(cfg, arm_ids, reward)
            except Exception:
                log.warning("apex.post_mortem: bandit a echoue:\n%s",
                            traceback.format_exc())
            # Le pattern gagnant doit exister AVANT record_outcome pour
            # recevoir sa victoire dans le meme cycle.
            if win and row.get("hook") and row.get("hook_family"):
                try:
                    knowledge_base.add_pattern(
                        cfg, doc, row.get("hook"),
                        "family:%s" % row.get("hook_family"), "post_mortem")
                except Exception:
                    log.warning("apex.post_mortem: add_pattern a echoue:\n%s",
                                traceback.format_exc())
            try:
                knowledge_base.record_outcome(
                    doc, arm_ids, reward,
                    threshold=threshold, retire_failures=retire_failures)
            except Exception:
                log.warning("apex.post_mortem: record_outcome a echoue:\n%s",
                            traceback.format_exc())
            for group in GROUPS:
                value = row.get(group)
                if not value:
                    continue
                bucket = result["winners"] if win else result["losers"]
                counts = bucket.setdefault(group, {})
                counts[str(value)] = counts.get(str(value), 0) + 1
            result["learned"] += 1
            # Le journal est le registre unique des publications deja
            # apprises : chaque ligne apprise y est inscrite ici, pour
            # eviter tout double apprentissage (daemon ou appel direct).
            try:
                from . import daemon as _daemon

                _daemon.journal(cfg, {
                    "ts": datetime.datetime.now().isoformat(),
                    "video_id": vid,
                    "reward": row.get("reward"),
                    "hook_family": row.get("hook_family"),
                    "duration_format": row.get("duration_format"),
                    "quiz": row.get("quiz"),
                    "views": row.get("views"),
                })
            except Exception:
                log.warning("apex.post_mortem: journal a echoue:\n%s",
                            traceback.format_exc())
        try:
            knowledge_base.save(cfg, doc)
        except Exception:
            log.warning("apex.post_mortem: sauvegarde KB echouee:\n%s",
                        traceback.format_exc())
        return result
    except Exception:
        log.warning("apex.post_mortem: analyse impossible (non fatal):\n%s",
                    traceback.format_exc())
        return {"learned": 0, "threshold": threshold,
                "winners": {}, "losers": {}}
