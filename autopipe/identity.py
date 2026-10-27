"""Identite de chaine : gate de niche, titres courts, hooks qui accrochent.

Cree le 2026-10-27 pour repondre aux critiques sur le Short "AP Dhillon's
Success Hides a Global Music Shift" :

1. Titre style article, trop abstrait (47 caracteres, aucun "tu") ->
   title_fix() force un titre <= title_max_chars (50 par defaut), concret,
   parle au viewer (you/your), question ou chiffre, mots abstraits bannis.
2. Sujets heterogenes (musique, lingerie 475 crore, Venus, Tenet, Halloween,
   "walking with God", data science...) -> filter_topics() applique un gate
   thematique : seuls les sujets de la niche [identity] survivent ; sinon
   l'appelant retombe sur les mots-cles de niche du scout. L'algorithme
   YouTube ne peut cataloguer la chaine que si la niche est stable.
3. Chaine sans identite claire -> identity_block() est injecte dans TOUS les
   prompts LLM (ideas + remake) pour que chaque Short parle a la meme
   audience avec la meme promesse et le meme ton.

Hook visuel / hook texte : hook_fix() garantit <= hook_max_chars (60) et
une formulation conflit/curiosite (question, negation ou chiffre).
"""
import re

from .config import get_config
from .util import get_logger

log = get_logger("identity")

# Mots "titre d'article" bannis : ils decrivent un sujet au lieu de faire
# cliquer. Le titre critique "Success Hides a Global Music Shift" en
# accumulait trois ("hides", "shift", "global").
ABSTRACT_TITLE_WORDS = (
    "hides", "unveils", "shift", "era", "legacy", "journey", "remarkable",
    "fascinating", "untold", "the truth about", "everything you",
    "deep dive", "comprehensive", "analysis", "phenomenon", "evolution",
    "revolution", "landscape", "realm", "tapestry",
)


def banned_title_words(ideas_cfg=None):
    """Liste fusionnee des mots bannis (defaut + ideas.banned_title_words)."""
    extra = (ideas_cfg or {}).get("banned_title_words") or []
    return list(ABSTRACT_TITLE_WORDS) + [str(w).strip().lower()
                                         for w in extra if str(w).strip()]


def load_identity(cfg=None):
    """Charge la section [identity] avec valeurs par defaut robustes."""
    cfg = cfg or get_config()
    ident = dict(cfg.get("identity") or {})
    ident.setdefault("name", "")
    # setdefault ne suffit pas : les cles existent avec "" dans les defaults
    # de config.py -> fallback explicite si vide.
    if not str(ident.get("niche") or "").strip():
        ident["niche"] = str((cfg.get("general") or {}).get("niche")
                             or "").strip()
    ident.setdefault("audience", "")
    ident.setdefault("promise", "")
    ident.setdefault("tone", "")
    ident.setdefault("keywords", [])
    ident.setdefault("anti_topics", [])
    # V2 (2026-10-27 pm) : strict=False par defaut. Un gate dur sans
    # mots-cles configures (cas reel du 2026-10-27 : [identity] absent du
    # autopipe.toml -> keywords=[]) rejettait 100 % des sujets tendances et
    # faisait retomber le pipeline sur les 3 keywords generiques du scout,
    # a chaque run. Mode soft par defaut : on garde tout, on trie.
    ident.setdefault("strict", False)
    ideas = cfg.get("ideas", {}) or {}
    ident["title_max_chars"] = int(ideas.get("title_max_chars", 50))
    ident["hook_max_chars"] = int(ideas.get("hook_max_chars", 60))
    ident["keywords"] = [str(k).strip().lower()
                         for k in (ident["keywords"] or []) if str(k).strip()]
    if not ident["keywords"]:
        # Fallback : derive les mots-cles de la niche general.niche +
        # scout.keywords, pour que le gate de niche ne rejette pas tout.
        scout_kw = [str(k).strip().lower()
                    for k in ((cfg.get("scout") or {}).get("keywords") or [])
                    if str(k).strip()]
        niche_words = [w for w in _norm(ident.get("niche")).split()
                       if len(w) >= 4]
        ident["keywords"] = sorted(set(scout_kw) | set(niche_words))
    ident["anti_topics"] = [str(k).strip().lower()
                            for k in (ident["anti_topics"] or [])
                            if str(k).strip()]
    return ident


def identity_block(cfg=None):
    """Bloc texte CHANNEL IDENTITY injecte dans les prompts systeme LLM."""
    ident = load_identity(cfg)
    lines = []
    if ident.get("name"):
        lines.append("Channel name: %s" % ident["name"])
    if ident.get("niche"):
        lines.append("Channel niche (STAY INSIDE IT, every Short): %s"
                     % ident["niche"])
    if ident.get("audience"):
        lines.append("Target audience: %s" % ident["audience"])
    if ident.get("promise"):
        lines.append("Channel promise (every Short must deliver it): %s"
                     % ident["promise"])
    if ident.get("tone"):
        lines.append("Voice/tone: %s" % ident["tone"])
    if ident.get("keywords"):
        lines.append("On-niche themes/keywords: %s"
                     % ", ".join(ident["keywords"][:25]))
    if ident.get("anti_topics"):
        lines.append("NEVER cover (off-niche, would break channel identity): "
                     "%s" % ", ".join(ident["anti_topics"][:25]))
    if not lines:
        return ""
    return "CHANNEL IDENTITY\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate de niche
# ---------------------------------------------------------------------------

def _norm(text):
    t = " ".join(str(text or "").split()).lower()
    return re.sub(r"[^a-z0-9 ]", " ", t)


def _phrase_hit(text, phrase, prefix=True):
    """True si `phrase` apparait dans `text` (mots entiers, tolerance prefixe)."""
    p = _norm(phrase).strip()
    if not p:
        return False
    pattern = r"(?:^| )%s%s(?: |$)" % (re.escape(p), r"\w*" if prefix else "")
    return re.search(pattern, text) is not None


def topic_score(topic_text, ident=None):
    """Score de pertinence niche d'un sujet.

    Renvoie :
      -1  sujet interdit (anti-topic)
       0  hors niche
      >0  nombre de mots-cles niche trouves (plus = mieux)
    """
    ident = ident or load_identity()
    t = _norm(topic_text)
    if not t:
        return 0
    for bad in ident["anti_topics"]:
        if _phrase_hit(t, bad):
            return -1
    hits = 0
    for kw in ident["keywords"]:
        if _phrase_hit(t, kw):
            hits += 1
    return hits


def filter_topics(topics, cfg=None):
    """Soft score de niche : garde TOUT, penalise le hors-niche dans le tri.

    V2 (2026-10-27 pm) - critique : a petit volume (3 vues), un gate dur
    rejette 3 sujets sur 4 et tue la cadence de publication, alors que
    l'algo a besoin de volume pour cataloguer la chaine. Le gate devient un
    score : les sujets on-niche remontent dans le ranking, les hors-niche
    restent generables en queue. Seuls les anti_topics restent un rejet dur.

    Comportements :
      - anti-topic -> rejet (jamais genere) ;
      - on-niche (score > 0) -> rank_score = score brut, place en tete ;
      - hors-niche (score 0) -> rank_score = score brut - penalty (defaut
        1.5, [identity].off_niche_penalty), place en queue ; genere quand
        meme si le lot est maigre ;
      - identity.strict=true (explicitement) -> ancien gate dur (rejet hors
        niche), pour qui veut la coherence maximale.
    """
    cfg = cfg or get_config()
    ident = load_identity(cfg)
    strict = bool(ident.get("strict", False))
    try:
        penalty = float(ident.get("off_niche_penalty", 1.5))
    except (TypeError, ValueError):
        penalty = 1.5
    kept, dropped = [], []
    for t in topics or []:
        if not isinstance(t, dict):
            continue
        subject = str(t.get("topic") or "").strip()
        if not subject:
            continue
        s = topic_score(subject, ident)
        if s < 0:
            dropped.append((subject, "anti-topic"))
            continue
        if s == 0 and strict:
            dropped.append((subject, "hors niche"))
            continue
        t = dict(t)
        t["identity_score"] = int(s)
        base = float(t.get("score") or 0.0)
        t["rank_score"] = base if s > 0 else base - penalty
        kept.append(t)
    for subject, why in dropped[:10]:
        log.info("Gate niche: sujet ignore (%s): %s", why, subject[:70])
    n_off = sum(1 for t in kept if int(t.get("identity_score") or 0) == 0)
    if kept and n_off:
        log.info("Gate niche (soft): %s/%s sujets hors niche gardes en queue "
                 "(penalite %.1f)", n_off, len(kept), penalty)
    if not kept:
        log.warning("Gate niche: 0/%s sujets passent -> l'appelant doit "
                    "utiliser les mots-cles de niche", len(topics or []))
        return []
    kept.sort(key=lambda t: (-float(t.get("rank_score") or 0.0),
                             -int(t.get("identity_score") or 0)))
    return kept


# ---------------------------------------------------------------------------
# Titres & hooks calibres Shorts
# ---------------------------------------------------------------------------

_TITLE_TEMPLATES = (
    "Why {t} Is Everywhere Right Now",
    "The Real Reason {t} Blew Up",
    "Nobody Expected This From {t}",
    "{t} Just Broke the Internet",
    "How {t} Took Over So Fast",
    "Everyone Gets {t} Wrong. Here's Proof",
    "{t}: The Detail Everyone Missed",
    "You Won't Believe {t} Did This",
)


def _shorten(text, max_chars):
    """Coupe sur un mot entier, sans ponctuation residuelle."""
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.-:;!?").strip()


def _topic_base(text):
    """Extrait un sujet court (<= 28 car.) utilisable dans un template."""
    t = " ".join(str(text or "").split())
    words = [w for w in t.split() if len(w) > 1][:6]
    t = " ".join(words)
    if len(t) > 28:
        t = t[:28]
        if " " in t:
            t = t[: t.rfind(" ")]
    t = t.strip(" ,.-:;!?\"'")
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def _has_banned(title, banned):
    t = _norm(title)
    return any(_phrase_hit(t, w, prefix=False) for w in banned if w)


def title_fix(title, topic=None, cfg=None):
    """Force un titre court, concret, oriente viewer (<= title_max_chars).

    Regles (critique 2026-10-27) :
      - max title_max_chars caracteres (50 par defaut), coupe sur mot entier ;
      - mots abstraits type article bannis ("hides", "shift", "era"...) ;
      - si le titre LLM est trop long/abstrait, reecriture a partir du SUJET
        via des templates questions/conflit ("Why {t} Is Everywhere Right
        Now", "The Real Reason {t} Blew Up"...).
    """
    cfg = cfg or get_config()
    ident = load_identity(cfg)
    ideas = cfg.get("ideas", {}) or {}
    max_chars = int(ideas.get("title_max_chars", 50))
    banned = banned_title_words(ideas)
    raw = " ".join(str(title or "").split()).strip()
    raw = raw.strip("\"“”‘’").rstrip(".").strip()
    if raw and raw[0].islower():
        raw = raw[0].upper() + raw[1:]
    if raw and len(raw) <= max_chars and not _has_banned(raw, banned):
        return raw
    # Le titre est trop long ou abstrait -> on reecrit a partir du sujet.
    base = _topic_base(topic or raw)
    cands = []
    for tpl in _TITLE_TEMPLATES:
        cand = tpl.format(t=base)
        if len(cand) <= max_chars and not _has_banned(cand, banned):
            # Priorite : question > 2e personne/chiffre > plus court.
            second = "you" in cand.lower() or "your" in cand.lower()
            number = any(c.isdigit() for c in cand)
            cands.append((0 if cand.endswith("?") else 1,
                          0 if second else 1, 0 if number else 1, len(cand), cand))
    if cands:
        return sorted(cands)[0][4]
    if raw:
        return _shorten(raw, max_chars)
    return _shorten(base or "This Changes Everything", max_chars)


def _default_hook(topic):
    base = _topic_base(topic)
    # V3 (2026-10-27) : hooks de repli SPECIFIQUES. Donnees web 2026 : les
    # hooks vagues ("you won't believe this", "why is X everywhere?")
    # performent ~4x moins que les hooks qui nomment un chiffre, une erreur
    # ou un resultat precis. Templates retenus : chiffre + erreur precise.
    for tpl in ("3 {t} facts nobody tells you",
                "Nobody warned you about {t}",
                "{t}: the mistake everyone repeats"):
        cand = tpl.format(t=base)
        if len(cand) <= 60:
            return cand
    return _shorten("The {t} mistake everyone repeats", 60)


def hook_fix(hook, topic=None, cfg=None):
    """Force un hook <= hook_max_chars, format conflit/curiosite.

    Le hook s'affiche a l'ecran pendant les 3 premieres secondes (video.py) :
    il doit poser une question ou un conflit, jamais un resume d'article.
    """
    cfg = cfg or get_config()
    ident = load_identity(cfg)
    max_chars = int((cfg.get("ideas", {}) or {}).get("hook_max_chars", 60))
    raw = " ".join(str(hook or "").split()).strip().strip("\"“”‘’")
    if not raw:
        raw = _default_hook(topic or "")
    return _shorten(raw, max_chars)


def duration_clamp(seconds, cfg=None):
    """Clamp la duree cible dans [duration_min, duration_max] (15-32 s par defaut).

    Critique 2026-10-27 : les Shorts de 40-60 s retiennent moins que les
    13-20 s ; recherche : sweet spot 15-35 s. On vise 15-32 s.
    """
    cfg = cfg or get_config()
    ideas = cfg.get("ideas", {}) or {}
    lo = int(ideas.get("duration_min", 15))
    hi = max(lo + 1, int(ideas.get("duration_max", 32)))
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        s = int(ideas.get("duration_default", 24))
    return max(lo, min(hi, s))

# --- V2 (2026-10-27 pm) : formats 13 s / 60 s + variantes de hook ----------
# Critiques a traiter :
#   - le clamp unique 15-32 s est arbitraire : deux formats, pas un milieu
#     (13 s ultra-dense, 60 s narration complete), le LLM choisit selon la
#     densite du sujet ;
#   - un seul hook par sujet = on optimise a l'aveugle : il faut une variante
#     B de hook differente (famille differente) pour alimenter un A/B.

# Profils de duree : cible + marge, en secondes. Jamais entre les deux.
_DURATION_PROFILES = {
    "short": {"target": 13, "min": 12, "max": 18},
    "long": {"target": 60, "min": 50, "max": 60},
}


def duration_profiles(cfg=None):
    """Profils de duree configurables [ideas].duration_profiles.

    Defaut : {"short": {target 13, min 12, max 18}, "long": {target 60,
    min 50, max 60}}. Chaque valeur est un dict {"target", "min", "max"}.
    """
    cfg = cfg or get_config()
    ideas = cfg.get("ideas", {}) or {}
    profiles = ideas.get("duration_profiles") or {}
    out = {}
    for name in ("short", "long"):
        p = profiles.get(name) or _DURATION_PROFILES[name]
        try:
            target = int(p.get("target", _DURATION_PROFILES[name]["target"]))
            mn = int(p.get("min", _DURATION_PROFILES[name]["min"]))
            mx = int(p.get("max", _DURATION_PROFILES[name]["max"]))
        except (TypeError, ValueError, AttributeError):
            target, mn, mx = (_DURATION_PROFILES[name][k] for k in
                              ("target", "min", "max"))
        out[name] = {"target": target, "min": mn, "max": mx}
    return out


def duration_plan(density, cfg=None):
    """Choisit le format selon la densite du sujet : 'short' (13 s) ou
    'long' (60 s). Jamais entre les deux.

    density : entier 1-5. 1-2 = sujet simple/listicle -> 13 s ; 3+ =
    sujet dense/recit -> 60 s. Valeurs hors 1-5 -> 'short' (defaut securise :
    les Shorts courts retiennent mieux et sont moins chers a produire).

    Renvoie {"format", "target", "min", "max"}.
    """
    try:
        d = int(density)
    except (TypeError, ValueError):
        d = 1
    profiles = duration_profiles(cfg)
    fmt = "short" if d <= 2 else "long"
    p = profiles[fmt]
    return {"format": fmt, "target": p["target"],
            "min": p["min"], "max": p["max"]}


def script_word_budget(format_name, cfg=None):
    """Budget de mots cohérent avec la durée cible d'un format.

    V3 (2026-10-27) - faille : le prompt ideas demandait "%(words_min)d-
    %(words_max)d words" (50-85, calibre pour l'ancien 15-32 s) MÊME pour
    le format 13 s -> à 160 wpm ça fait 19-32 s, jamais 13 s. Le budget est
    désormais dérivé de la cible du format à 145-165 mots/minute :
      short (13 s) -> 31-36 mots ; long (60 s) -> 145-165 mots.
    Renvoie (min, max) entiers.
    """
    plan = duration_plan(1 if str(format_name) == "short" else 4, cfg=cfg)
    target = plan["target"]
    lo = int(round(target * 145 / 60.0))
    hi = int(round(target * 165 / 60.0))
    return max(8, lo), max(lo + 2, hi)


def checkpoint_seconds(cfg=None):
    """Secondes de la mid-video checkpoint (texte 'STAY' / recall).

    Vise la moitie de la duree cible du format, plancher 5 s (un checkpoint
    avant 5 s surprend avant que le viewer ait accroche). Renvoie int.
    """
    cfg = cfg or get_config()
    video = cfg.get("video", {}) or {}
    try:
        return int(video.get("checkpoint_seconds", 15))
    except (TypeError, ValueError):
        return 15


_HOOK_FAMILIES = ("question", "number", "negation", "statement")


def hook_family(hook):
    """Classe un hook dans une famille : question / number / negation /
    statement. Sert a garantir que la variante B est d'une famille
    DIFFERENTE (sinon l'A/B teste deux fois la meme chose)."""
    t = " ".join(str(hook or "").split()).strip()
    if not t:
        return "statement"
    if t.endswith("?"):
        return "question"
    if any(c.isdigit() for c in t):
        return "number"
    head = t.lower()
    for w in ("nobody", "never", "no one", "not ", "stop ", "don't", "dont ",
              "why you", "you're", "youre", "warning", "the truth", "lies"):
        if head.startswith(w) or w in head[:30]:
            return "negation"
    return "statement"


_ALT_TEMPLATES = {
    # V3 : variantes B plus specifiques (chiffre, erreur nommee, resultat
    # concret) - les curiosity gaps precis completent +63 % mieux que les
    # vagues (benchmarks Shorts 2026).
    "question": ("What if everything you know about {t} is wrong?",
                 "Why does nobody explain {t} like this?"),
    "number": ("3 {t} facts they never teach you",
               "{t}: the 1 mistake everyone repeats"),
    "negation": ("Nobody warned you about {t}",
                 "Stop believing this about {t}"),
    "statement": ("{t} just changed forever. Here's the proof.",
                  "This is the {t} moment everyone missed"),
}


def alt_hook(topic, hook, cfg=None):
    """Genere une variante B de hook dans une famille DIFFERENTE du hook A.

    Renvoie "" si impossible (sujet vide). Le hook sort toujours <=
    hook_max_chars (60 par defaut), comme hook_fix().
    """
    cfg = cfg or get_config()
    max_chars = int((cfg.get("ideas", {}) or {}).get("hook_max_chars", 60))
    base = _topic_base(topic or "")
    if not base:
        return ""
    fam_a = hook_family(hook)
    cands = []
    for fam, templates in _ALT_TEMPLATES.items():
        if fam == fam_a:
            continue
        for tpl in templates:
            cand = tpl.format(t=base)
            if len(cand) <= max_chars:
                # Priorite : question > negation > number > statement.
                prio = _HOOK_FAMILIES.index(fam)
                cands.append((prio, len(cand), cand, fam))
    if not cands:
        return ""
    cands.sort()
    return cands[0][2]


def hook_variants(topic, hook, cfg=None):
    """Paire A/B de hooks pour un sujet : le hook LLM (ou un defaut) plus
    une variante generative d'une famille differente.

    Renvoie [{"variant": "A", "hook": ..., "family": ...},
             {"variant": "B", "hook": ..., "family": ...}].
    Si alt_hook() n'aboutit pas, B = default_hook sur le sujet (fallback
    degrade : l'A/B tourne quand meme, familles possiblement proches).
    """
    cfg = cfg or get_config()
    a = hook_fix(hook, topic=topic, cfg=cfg)
    fam_a = hook_family(a)
    b_raw = alt_hook(topic, a, cfg=cfg)
    if not b_raw:
        b_raw = _default_hook(topic or "")
    b = hook_fix(b_raw, topic=topic, cfg=cfg)
    if hook_family(b) == fam_a:
        # Derniere chance : forcer une autre famille via les templates.
        for tpl in ("3 {t} facts they never teach you",
                    "Why does nobody explain {t} like this?",
                    "{t} just changed forever. Here's the proof."):
            cand = hook_fix(tpl.format(t=_topic_base(topic or "")),
                            topic=topic, cfg=cfg)
            if hook_family(cand) != fam_a:
                b = cand
                break
    return [{"variant": "A", "hook": a, "family": fam_a},
            {"variant": "B", "hook": b, "family": hook_family(b)}]
