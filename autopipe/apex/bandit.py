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

Cold start (exploration forcee) : tant qu'un bras n'a pas ete essaye
au moins apex.cold_start_pulls fois (3 par defaut), il est retourne
en priorite par best() - le bandit ne peut pas exploiter un bras
unique biaise des le premier jour.
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

# Cold start : tirages obligatoires par bras avant exploitation
# (apex.cold_start_pulls dans la config).
_COLD_START = 3


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
        arm["pulls"] = int(arm.get("pulls", 0))
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
        # Un tirage mesure = un credit de plus pour le cold start.
        arm["pulls"] = int(arm.get("pulls", 0)) + 1
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


def cold_start_pulls(cfg=None):
    """Plancher de tirages par bras avant exploitation (cold start).

    Chaque bras doit etre essaye au moins N fois (apex.cold_start_pulls,
    3 par defaut) avant que le bandit puisse exploiter son gain
    posterieur ; 0 desactive l'exploration forcee.
    """
    cfg = cfg if cfg is not None else get_config()
    apex_cfg = (cfg or {}).get("apex", {}) or {}
    try:
        return max(0, int(apex_cfg.get("cold_start_pulls", _COLD_START)))
    except (TypeError, ValueError):
        return _COLD_START


def cold_start(cfg=None, group=None):
    """Ids des bras encore froids du groupe (moins de tirages que le
    plancher cold start)."""
    cfg = cfg if cfg is not None else get_config()
    doc = load(cfg)
    prefix = ("%s:" % group) if group else ""
    floor = cold_start_pulls(cfg)
    return [arm_id for arm_id, arm in doc["arms"].items()
            if (not prefix or arm_id.startswith(prefix))
            and int(arm.get("pulls", 0)) < floor]


def best(cfg=None, group=None):
    """Bras du groupe a essayer ensuite -> (arm_id, moyenne).

    group = "family" | "format" | "quiz" ; None = tous les bras.
    Cold start : tant qu'un bras du groupe compte moins de tirages que
    le plancher (apex.cold_start_pulls), le bras le moins essaye est
    force (egalite -> id, deterministe), meme face a un gagnant clair ;
    sinon le bras au meilleur gain posterieur (greedy sur la moyenne
    Beta). Retourne (None, None) si aucun bras ne correspond.
    """
    cfg = cfg if cfg is not None else get_config()
    doc = load(cfg)
    prefix = ("%s:" % group) if group else ""
    items = [(arm_id, arm) for arm_id, arm in doc["arms"].items()
             if not prefix or arm_id.startswith(prefix)]
    if not items:
        return None, None
    floor = cold_start_pulls(cfg)
    if floor > 0:
        cold = sorted(((arm_id, int(arm.get("pulls", 0)))
                       for arm_id, arm in items
                       if int(arm.get("pulls", 0)) < floor),
                      key=lambda t: (t[1], t[0]))
        if cold:
            arm_id = cold[0][0]
            return arm_id, mean(doc["arms"][arm_id])
    best_id, best_mean = None, None
    for arm_id, arm in items:
        m = mean(arm)
        if best_mean is None or m > best_mean:
            best_id, best_mean = arm_id, m
    return best_id, best_mean


def summary(cfg=None):
    """Etat lisible du bandit pour la CLI et le rapport.

    Chaque bras expose son nb de tirages (pulls) et son statut froid
    (cold) ; la cle cold_start rappelle le plancher applique.
    """
    cfg = cfg if cfg is not None else get_config()
    doc = load(cfg)
    floor = cold_start_pulls(cfg)
    arms = {}
    for arm_id, arm in doc["arms"].items():
        pulls = int(arm.get("pulls", 0))
        arms[arm_id] = {
            "label": label(arm_id),
            "alpha": round(float(arm.get("alpha", 1.0)), 4),
            "beta": round(float(arm.get("beta", 1.0)), 4),
            "mean": round(mean(arm), 4),
            "pulls": pulls,
            "cold": pulls < floor,
        }
    return {"updated": doc.get("updated"), "arms": arms,
            "cold_start": floor}


def label(arm_id):
    """Libelle humain d'un bras (retombe sur l'id brut)."""
    return _LABELS.get(arm_id, arm_id)
