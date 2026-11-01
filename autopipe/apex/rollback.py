# -*- coding: utf-8 -*-
"""Garde-fou du paquet apex : instantanes et retour arriere.

Avant chaque cycle, l'etat appris (bandit + base de connaissances) est
instantane dans data/apex/rollback/. Si la sante moyenne (reward des
dernieres publications mesurees) degrade de plus de apex.degrade_pts
points par rapport au record enregistre, le dernier instantane est
restaure automatiquement.
"""

import datetime
import traceback

from ..config import get_config, get_paths
from ..util import get_logger, load_json, save_json
from . import bandit
from . import knowledge_base
from . import metrics

log = get_logger("apex.rollback")

# Nombre d'instantanes conserves.
KEEP = 10
# Nb de dernieres lignes mesurees pour la sante.
HEALTH_K = 5


def _dir(cfg=None):
    """Repertoire des instantanes (data/apex/rollback)."""
    cfg = cfg if cfg is not None else get_config()
    return get_paths(cfg).apex / "rollback"


def snapshot(cfg=None, label="auto", reason=None):
    """Instantanise l'etat courant et purge les instantanes trop vieux.

    Retourne le nom du fichier cree, ou None en cas d'echec (non fatal).
    """
    cfg = cfg if cfg is not None else get_config()
    try:
        d = _dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        name = "%s-%s.json" % (stamp, label)
        doc = {
            "created": datetime.datetime.now().isoformat(),
            "label": label,
            "reason": reason,
            "bandit": bandit.load(cfg),
            "knowledge": knowledge_base.load(cfg),
        }
        save_json(d / name, doc)
        _prune(cfg)
        log.info("apex.rollback: instantane cree: %s", name)
        return name
    except Exception:
        log.warning("apex.rollback: instantane impossible (non fatal):\n%s",
                    traceback.format_exc())
        return None


def list_snapshots(cfg=None):
    """Noms des instantanes, tries par ordre chronologique."""
    cfg = cfg if cfg is not None else get_config()
    try:
        d = _dir(cfg)
        if not d.exists():
            return []
        return sorted(p.name for p in d.glob("*.json"))
    except Exception:
        log.warning("apex.rollback: liste impossible (non fatal):\n%s",
                    traceback.format_exc())
        return []


def _prune(cfg):
    """Supprime les instantanes au-dela de KEEP."""
    try:
        names = list_snapshots(cfg)
        extra = names[:-KEEP] if len(names) > KEEP else []
        for name in extra:
            try:
                (_dir(cfg) / name).unlink()
            except OSError:
                pass
    except Exception:
        log.warning("apex.rollback: purge impossible (non fatal):\n%s",
                    traceback.format_exc())


def restore(cfg=None, name=None):
    """Restaure un instantane (le plus recent si name=None).

    Retourne le nom restaure, ou None si rien n'a ete restaure.
    """
    cfg = cfg if cfg is not None else get_config()
    try:
        names = list_snapshots(cfg)
        if not names:
            return None
        if name is None:
            name = names[-1]
        if name not in names:
            log.warning("apex.rollback: instantane inconnu: %s", name)
            return None
        doc = load_json(_dir(cfg) / name, None) or {}
        if doc.get("bandit"):
            bandit.save(cfg, doc["bandit"])
        if doc.get("knowledge"):
            knowledge_base.save(cfg, doc["knowledge"])
        log.info("apex.rollback: restauration de %s", name)
        return name
    except Exception:
        log.warning("apex.rollback: restauration echouee (non fatal):\n%s",
                    traceback.format_exc())
        return None


def health(cfg=None, k=HEALTH_K):
    """Reward moyen des k dernieres lignes mesurees ; None si aucune."""
    cfg = cfg if cfg is not None else get_config()
    try:
        rows = metrics.measured_rows(cfg)
        if not rows:
            return None
        try:
            rows = sorted(rows, key=lambda r: r.get("published_at") or "")
        except (TypeError, ValueError):
            pass
        k = int(k or 0)
        recent = rows[-k:] if k > 0 else rows
        vals = [float(r.get("reward") or 0.0) for r in recent]
        if not vals:
            return None
        return sum(vals) / len(vals)
    except Exception:
        log.warning("apex.rollback: sante indisponible (non fatal):\n%s",
                    traceback.format_exc())
        return None


def baseline_file(cfg=None):
    """Chemin du record de sante (data/apex/baseline.json)."""
    cfg = cfg if cfg is not None else get_config()
    return get_paths(cfg).apex / "baseline.json"


def maybe_rollback(cfg=None):
    """Retour arriere automatique si la sante degrade du record.

    Retourne None si rien a faire, sinon une chaine decrivant
    l'action : "baseline:<sante>" pour un nouveau record,
    "restored:<nom>" pour une restauration. Jamais fatal.
    """
    cfg = cfg if cfg is not None else get_config()
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    if not apex_cfg.get("auto_rollback", True):
        return None
    try:
        h = health(cfg)
    except Exception:
        h = None
    if h is None:
        return None
    path = baseline_file(cfg)
    try:
        doc = load_json(path, None) or {}
    except Exception:
        doc = {}
    try:
        best = float(doc.get("best", 0.0) or 0.0)
    except (TypeError, ValueError):
        best = 0.0
    if h > best:
        try:
            save_json(path, {
                "best": h,
                "updated": datetime.datetime.now().isoformat(),
            })
        except Exception:
            log.warning("apex.rollback: record non sauvegarde:\n%s",
                        traceback.format_exc())
        return "baseline:%.3f" % h
    try:
        degrade = float(apex_cfg.get("degrade_pts", 10.0) or 10.0) / 100.0
    except (TypeError, ValueError):
        degrade = 0.10
    if best - h >= degrade and not doc.get("restored"):
        snapshot(cfg, label="pre-rollback", reason="degradation de la sante")
        name = restore(cfg)
        try:
            doc["restored"] = True
            doc["restored_health"] = h
            save_json(path, doc)
        except Exception:
            log.warning("apex.rollback: etat de restauration non enregistre:\n%s",
                        traceback.format_exc())
        return "restored:%s (sante %.3f < record %.3f)" % (name, h, best)
    return None
