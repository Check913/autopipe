# -*- coding: utf-8 -*-
"""Gate de diversite du skill hook-generator-upgrade (2026-11-01).

Deux regles de rejet, parametrees dans [hooks] :

  * recent_days (7 j) / dup_jaccard (0.7) : un hook propose dont la
    similitude de tokens Jaccard avec un hook publie dans la fenetre
    recente depasse le seuil est refuse ;
  * max_family_consecutive (3) : si le hook propose prolongerait une
    serie de N publications consecutives de la meme famille au-dela de
    la limite, il est refuse.

pick_alternative essaie les variantes A/B puis retombe sur des
templates de familles differentes (identite.hook_variants force deja
A/B de familles distinctes) ; si tout echoue le hook historique est
garde. Aucun appel reseaux ; lecture de hook_memory.json.
"""
import traceback

from ..config import get_config
from ..util import get_logger
from . import memory

log = get_logger("hooks.diversity")


def _opts(cfg):
    return (cfg or {}).get("hooks") or {}


def _opt(cfg, key, default):
    return (_opts(cfg) or {}).get(key, default)


def tokenize(text):
    """Tokens > 2 chars (les stopwords courts des hooks tombent)."""
    return set(w for w in memory.norm_hook(text).replace("'", " ").split()
               if len(w) > 2)


def jaccard(a, b):
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def is_dup(hook_text, recent, threshold=0.7):
    """True si similitude de tokens avec un hook recent >= threshold."""
    t = memory.norm_hook(hook_text)
    if not t:
        return False
    return any(jaccard(t, h) >= threshold for h in recent)


def family_would_break_streak(doc, family, limit):
    """True si la famille prolongerait une serie au-dela de la limite."""
    if not family or not limit or limit < 1:
        return False
    streak = memory.consecutive_family(doc, family)
    return streak + 1 > limit


def passes(topic, hook_text, cfg=None, doc=None):
    """(ok, raison). Raison vide quand le hook passe."""
    cfg = cfg or get_config()
    doc = doc if doc is not None else memory.load(cfg)
    fam = hook_family_safe(hook_text)
    recent = memory.used_hook_texts(cfg, doc)
    dup_thr = float(_opt(cfg, "dup_jaccard", 0.7))
    if is_dup(hook_text, recent, dup_thr):
        return False, "doublon (Jaccard>=%.2f sur %s j)" % (dup_thr,
                                                            _opt(cfg, "recent_days", 7))
    limit = int(_opt(cfg, "max_family_consecutive", 3) or 0)
    if family_would_break_streak(doc, fam, limit):
        return False, "famille '%s' : serie de %s consecutive(s) > limite %s" % (
            fam, memory.consecutive_family(doc, fam), limit)
    return True, ""


def hook_family_safe(hook_text):
    try:
        from ..identity import hook_family
        return hook_family(hook_text)
    except Exception:
        return None


def pick_alternative(topic, variants, cfg=None, doc=None):
    """Choisit le premier hook (variants A/B puis templates) qui passe.

    variants = sortie identite.hook_variants [{variant, hook, family}].
    Retour : (hook, family, variant_label, raison_refus_du_propose)
    ou (hook_propose, famille_proposee, None, raison) si rien ne passe
    (on garde le propose plutot que publier un hook vide).
    """
    cfg = cfg or get_config()
    doc = doc if doc is not None else memory.load(cfg)
    if not isinstance(variants, list) or not variants:
        return None
    proposed = variants[0]
    reason = None
    try:
        ok, reason = passes(topic, proposed.get("hook"), cfg=cfg, doc=doc)
        if ok:
            return proposed["hook"], proposed.get("family"), \
                proposed.get("variant"), ""
    except Exception:
        log.warning("[diversity] passes() a echoue (non fatal):\n%s",
                    traceback.format_exc())
        reason = "erreur interne (hook garde par prudence)"

    alternatives = []
    for v in variants[1:]:
        alternatives.append((v.get("hook"), v.get("family"),
                             v.get("variant")))
    for tmpl_hook, tmpl_family in _template_alternatives(topic, cfg):
        alternatives.append((tmpl_hook, tmpl_family, None))
    for h, fam, label in alternatives:
        if not h:
            continue
        try:
            ok, _ = passes(topic, h, cfg=cfg, doc=doc)
        except Exception:
            continue
        if ok:
            return h, fam, label, reason
    log.warning("[diversity] aucune alternative n'a passe les gates -> "
                "hook propose garde (non fatal)")
    return proposed["hook"], proposed.get("family"), \
        proposed.get("variant"), reason


def _template_alternatives(topic, cfg=None):
    """Replis de templates hors paires A/B : section [quiz] du TOML.

    hooks_a = famille 'question', hooks_b = famille 'identity' (meme
    mapping que quiz.py). Aucun placeholder dans ces templates (hooks
    generiques), donc pas de substitution de sujet ; chaque alternative
    repasse de toute facon les gates via passes() (anti-doublon inclus,
    au cas ou quiz.py viendrait de publier le meme hook).
    """
    cfg = cfg or get_config()
    quiz = cfg.get("quiz") or {}
    out = []
    hooks_a = [str(h) for h in (quiz.get("hooks_a") or []) if h]
    hooks_b = [str(h) for h in (quiz.get("hooks_b") or []) if h]
    if hooks_a:
        out.append((hooks_a[0], "question"))
    if hooks_b:
        out.append((hooks_b[0], "identity"))
    return out


def line(cfg=None):
    """Ligne de statut [diversity] pour le log scheduler (non fatal)."""
    try:
        cfg = cfg or get_config()
        doc = memory.load(cfg)
        recent = memory.used_hook_texts(cfg, doc)
        fams = memory.recent_families(cfg, doc)
        dup_thr = float(_opt(cfg, "dup_jaccard", 0.7))
        limit = int(_opt(cfg, "max_family_consecutive", 3) or 0)
        fam = fams[-1] if fams else None
        streak = memory.consecutive_family(doc, fam) if fam else 0
        return ("[diversity] %s hook(s) publies en %s j (rejet doublon "
                "Jaccard>=%.2f) ; derniere famille : %s (%s consecutif(s), "
                "max %s)") % (
            len(recent), _opt(cfg, "recent_days", 7), dup_thr,
            fam or "?", streak, limit or "inf")
    except Exception as exc:
        return "[diversity] statut indisponible (%s)" % exc
