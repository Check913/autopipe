# -*- coding: utf-8 -*-
"""Base de connaissances de hooks pour le paquet apex.

Document JSON data/apex/knowledge.json :
{"patterns": [{"id": ..., "arm": "family:question", "text": ...,
               "norm": ..., "wins": 0, "losses": 0, "active": true,
               "source": "scrape"|"manual", "created": ...}],
 "updated": ...}

Les patterns gagnants finissent dans la directive de prompt
(anglais, meme convention que les directives du module research).
Un pattern retire (trop de pertes) passe a active=False : c'est le
rollback doux de la base, sans destruction de donnees.
"""

import uuid
from datetime import datetime, timezone

from ..config import get_config, get_paths
from ..util import get_logger, load_json, save_json

log = get_logger("apex.knowledge")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def kb_file(cfg=None):
    """Chemin de la base de connaissances."""
    cfg = cfg if cfg is not None else get_config()
    return get_paths(cfg).apex / "knowledge.json"


def load(cfg=None):
    """Charge la base, avec une forme minimale valide en secours."""
    cfg = cfg if cfg is not None else get_config()
    doc = load_json(kb_file(cfg), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("patterns"), list):
        doc = {"patterns": []}
    return doc


def save(cfg, doc):
    """Sauvegarde la base de connaissances."""
    cfg = cfg if cfg is not None else get_config()
    if not isinstance(doc, dict):
        doc = {"patterns": []}
    doc.setdefault("patterns", [])
    doc["updated"] = _now_iso()
    save_json(kb_file(cfg), doc)


def _norm(text):
    """Cle de dedoublonnage : texte mis en minuscules, espaces compactes."""
    return " ".join(str(text or "").lower().split())


def add_pattern(cfg=None, doc=None, text="", arm=None, source="manual"):
    """Ajoute un pattern (dedoublonne sur le texte normalise).

    Retourne True si le pattern a ete ajoute, False sinon (vide ou
    deja connu, actif ou retire).
    """
    cfg = cfg if cfg is not None else get_config()
    doc = doc if doc is not None else load(cfg)
    text = str(text or "").strip()
    key = _norm(text)
    if not text or not key:
        return False
    for pat in doc.get("patterns") or []:
        if _norm(pat.get("text")) == key:
            return False
    doc.setdefault("patterns", []).append({
        "id": uuid.uuid4().hex[:12],
        "arm": arm,
        "text": text,
        "norm": key,
        "wins": 0,
        "losses": 0,
        "active": True,
        "source": source,
        "created": _now_iso(),
    })
    return True


def active_patterns(doc):
    """Liste des patterns encore actifs (non retires)."""
    return [p for p in ((doc or {}).get("patterns") or [])
            if p.get("active", True)]


def record_outcome(doc=None, arm_ids=None, reward=0.0, threshold=0.7,
                   retire_failures=10):
    """Compte un win ou une loss pour les patterns du bras cite.

    reward et threshold vivent sur l'echelle [0, 1] (ratio
    d'engagement). Un pattern retire (losses >= retire_failures) passe
    a active=False et cesse d'etre propose dans la directive.
    """
    doc = doc if doc is not None else {"patterns": []}
    arm_set = set(arm_ids or [])
    win = float(reward or 0.0) >= float(threshold)
    for pat in doc.get("patterns") or []:
        if pat.get("arm") not in arm_set:
            continue
        if win:
            pat["wins"] = int(pat.get("wins") or 0) + 1
        else:
            pat["losses"] = int(pat.get("losses") or 0) + 1
        if int(pat.get("losses") or 0) >= int(retire_failures):
            if pat.get("active", True):
                log.info("Apex KB: pattern retire apres %s pertes: %s",
                         pat["losses"], str(pat.get("text"))[:60])
            pat["active"] = False
    doc["updated"] = _now_iso()
    return doc


def build_directive(cfg=None, doc=None, max_patterns=5):
    """Directive anglaise avec les patterns gagnants actifs ("" si aucun).

    Seuls les patterns avec au moins un win sont cites, tries par
    bilan (wins - losses) decroissant.
    """
    doc = doc if doc is not None else load(cfg)
    pats = [p for p in active_patterns(doc) if int(p.get("wins") or 0) > 0]
    pats.sort(key=lambda p: ((int(p.get("wins") or 0)) - (int(p.get("losses") or 0)),
                             int(p.get("wins") or 0)), reverse=True)
    texts = [str(p.get("text") or "").strip() for p in pats[:max_patterns]]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    return "Learn from these hooks that performed best: " + " | ".join(texts)
