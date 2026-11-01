# -*- coding: utf-8 -*-
"""Bandit de Thompson pour le paquet apex (auto-amelioration).

Trois groupes de bras sont suivis en parallele, chacun avec une loi
Beta(alpha, beta) mise a jour a chaque publication mesuree :
- family : question | number | negation | statement (familles de hooks) ;
- format : short | long (duree de la source) ;
- quiz   : on | off (presentation quiz ou classique).

L'etat est sauvegarde dans data/apex/bandit.json. Le choix d'un bras
se fait par echantillonnage de Thompson : on tire un echantillon Gamma
de chaque bras du groupe et on garde le maximum.
"""

import random
from datetime import datetime, timezone

from ..config import get_config, get_paths
from ..util import get_logger, load_json, save_json

log = get_logger("apex.bandit")

# Bras par defaut : (id, libelle sans accent).
DEFAULT_ARMS = [
    ("family:question", "Hook question"),
    ("family:number", "Hook chiffre"),
    ("family:negation", "Hook negation"),
    ("family:statement", "Hook affirmation"),
    ("format:short", "Format court (<=60s)"),
    ("format:long", "Format long (>60s)"),
    ("quiz:on", "Presentation quiz"),
    ("quiz:off", "Presentation classique"),
]

_LABELS = dict(DEFAULT_ARMS)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def state_file(cfg=None):
    """Chemin de l'etat du bandit."""
    cfg = cfg if cfg is not None else get_config()
    return get_paths(cfg).apex / "bandit.json"


def load(cfg=None):
    """Charge l'etat en re-injectant les bras manquants (prior Beta(1,1))."""
    cfg = cfg if cfg is not None else get_config()
    doc = load_json(state_file(cfg), None)
    if not isinstance(doc, dict):
        doc = {}
    arms = doc.get("arms")
    if not isinstance(arms, dict):
        arms = {}
    for arm_id, _label in DEFAULT_ARMS:
        arm = arms.get(arm_id)
        if not isinstance(arm, dict):
            arm = {}
        arm["alpha"] = float(arm.get("alpha", 1.0))
        arm["beta"] = float(arm.get("beta", 1.0))
        arms[arm_id] = arm
    doc["arms"] = arms
    return doc


def save(cfg, doc):
    """Sauvegarde l'etat du bandit."""
    cfg = cfg if cfg is not None else get_config()
    if not isinstance(doc, dict):
        doc = {"arms": {}}
    doc.setdefault("arms", {})
    doc["updated"] = _now_iso()
    save_json(state_file(cfg), doc)


def update(cfg=None, arm_ids=None, reward=0.0):
    """Mise a jour des bras cites avec une recompense dans [0, 1].

    Recompense continue : alpha += r et beta += (1 - r), des
    pseudo-comptes Bernoulli fractionnaires qui restent des lois Beta
    valides (plus la recompense est haute, plus le bras monte).
    """
    cfg = cfg if cfg is not None else get_config()
    arm_ids = list(arm_ids or [])
    if not arm_ids:
        return None
    try:
        reward = max(0.0, min(1.0, float(reward)))
    except (TypeError, ValueError):
        reward = 0.0
    doc = load(cfg)
    for arm_id in arm_ids:
        arm = doc["arms"].get(arm_id)
        if not isinstance(arm, dict):
            arm = {"alpha": 1.0, "beta": 1.0}
            doc["arms"][arm_id] = arm
        arm["alpha"] = float(arm.get("alpha", 1.0)) + reward
        arm["beta"] = float(arm.get("beta", 1.0)) + (1.0 - reward)
    save(cfg, doc)
    return doc


def mean(arm):
    """Moyenne posterieure du bras : alpha / (alpha + beta)."""
    try:
        alpha = float(arm.get("alpha", 1.0))
        beta = float(arm.get("beta", 1.0))
    except (AttributeError, TypeError, ValueError):
        return 0.5
    total = alpha + beta
    return (alpha / total) if total > 0 else 0.5


def sample_arm(arm):
    """Echantillon de Thompson : Gamma(a) / (Gamma(a) + Gamma(b))."""
    try:
        alpha = max(float(arm.get("alpha", 1.0)), 1e-6)
        beta = max(float(arm.get("beta", 1.0)), 1e-6)
    except (AttributeError, TypeError, ValueError):
        return 0.5
    a = random.gammavariate(alpha, 1.0)
    b = random.gammavariate(beta, 1.0)
    total = a + b
    return (a / total) if total > 0 else 0.5


def best(cfg=None, group=None):
    """Bras du groupe au meilleur gain posterieur -> (arm_id, moyenne).

    group = "family" | "format" | "quiz" ; None = tous les bras.
    Retourne (None, None) si aucun bras ne correspond.
    """
    cfg = cfg if cfg is not None else get_config()
    doc = load(cfg)
    prefix = ("%s:" % group) if group else ""
    best_id, best_mean = None, None
    for arm_id, arm in doc["arms"].items():
        if prefix and not arm_id.startswith(prefix):
            continue
        m = mean(arm)
        if best_mean is None or m > best_mean:
            best_id, best_mean = arm_id, m
    return best_id, best_mean


def summary(cfg=None):
    """Etat lisible du bandit pour la CLI et le rapport."""
    cfg = cfg if cfg is not None else get_config()
    doc = load(cfg)
    arms = {}
    for arm_id, arm in doc["arms"].items():
        arms[arm_id] = {
            "label": label(arm_id),
            "alpha": round(float(arm.get("alpha", 1.0)), 4),
            "beta": round(float(arm.get("beta", 1.0)), 4),
            "mean": round(mean(arm), 4),
        }
    return {"updated": doc.get("updated"), "arms": arms}


def label(arm_id):
    """Libelle humain d'un bras (retombe sur l'id brut)."""
    return _LABELS.get(arm_id, arm_id)
