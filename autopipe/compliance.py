"""Conformite anti-demoetisation : garde QUALITE avant sauvegarde/generation.

Skill 2026 regle n°1 : la demoetisation tue plus de chaines que l'algorithme.
Ce module verifie, AVANT l'enregistrement d'une idee (ideas.py / remake.py) :

1. ORIGINALITE CROISEE : Jaccard titre+hook (et script) vs les Shorts DEJA
   produits de la chaine (data/ideas + data/remake). Le remake verifie
   deja la similitude vs la video SOURCE ; ici on traque la repetition
   intra-chaine (template farming) = cause officielle de demoetisation
   "contenu repetitif" (policy YouTube reused content).

2. FATIGUE DE FORMAT : trop d'idees consecutives avec la meme famille de
   hook (identity.hook_family) = monotonie visible par le viewer et par
   l'algorithme (viewed vs swiped away degrade).

3. POLICY GUARD : formats interdits par le skill (compilations, sub4sub,
   engagement bait, fear bait, disclaimers IA dans le script).

4. DIVULGATION CONTENU ALTERE : resolue depuis config [upload].
   altered_content -> status.containsSyntheticMedia (consomme par upload.py).

Design (decision 2026-10-31) :
- modes : "block" (defaut : l'idee n'est PAS sauvegardee), "warn"
  (sauvegardee mais marquee), "off" (aucun controle).
- JAMAIS d'exception remontee a l'appelant : tout echec interne est
  journalise dans data/compliance/defects.jsonl (type INSUFFICIENT_DATA /
  error) et l'idee passe (fail-open, le pipeline ne doit pas mourir pour
  un controle de qualite) - contrairement au geo-lock qui est fail-closed.
- Rapports audit : data/compliance/rejected.jsonl (idees rejetees en
  mode block).
"""
import datetime as dt
import json
import re

from .config import get_paths
from .identity import hook_family
from .util import as_script_text, get_logger, load_json

log = get_logger("autopipe.compliance")

# ---------------------------------------------------------------------------
# Patterns interdits (regle n°1 du skill : pas de formats a risque de
# demoetisation). Chaque regex -> (label, motif). Case-insensitive.
# ---------------------------------------------------------------------------
_BANNED_PATTERNS = (
    (r"\bcompilations?\b|top\s*(?:50|100)\s*(?:best|worst|moments)\b",
     "compilation"),
    (r"sub\s?4\s?sub|sub[- ]?for[- ]?sub|\bf\s?4\s?f\b|follow\s?4\s?follow|"
     r"like\s?4\s?like|subscribe\s*to\s*me\s*and\s*i",
     "sub4sub"),
    (r"\b(?:like\s*(?:and|&)?\s*subscribe|subscribe\s*(?:if|or|and)?\s*(?:you|i)?)\b|"
     r"\bsmash\s+that\b|\bhit\s+the\s+bell\b|\bcomment\s+below\b|\bcomment\s+down\b|"
     r"\bengagement\s+bait\b",
     "engagement_bait"),
    (r"\bgiveaways?\b|\bfree\s+(?:iphone|money|gift\s*card)s?\b",
     "giveaway_bait"),
    (r"\byou\s+(?:will|might|could)\s+die\b|\bwarning\s*:?\s*(?:shocking|disturbing)\b|"
     r"\bdelete\s+(?:this|the\s+video)\s+soon\b|\bbefore\s+it'?s\s+(?:deleted|too\s+late)\b|"
     r"\bthey\s+don'?t\s+want\s+you\s+to\s+(?:see|know)\s+this\b",
     "fear_bait"),
    (r"\bas\s+an\s+ai\b|\bi\s+am\s+an\s+ai\b|\bai\s+language\s+model\b|"
     r"\bgenerated\s+by\s+ai\b|\bhere'?s?\s+the\s+content\s+you\s+requested\b",
     "ai_slop_leak"),
    (r"\bmake\s+money\s+online\s+fast\b|\bget\s+rich\s+quick\b|"
     r"\bpassive\s+income\s+while\s+you\s+sleep\b",
     "get_rich_quick"),
)


def _cfg_section(cfg, name):
    """Section de config tolerante aux manques (tests cfg minimale)."""
    return cfg.get(name) or {}


def _token_set(text, min_len=2):
    """Ensemble de mots normalises (minuscules, alphanumeriques) pour Jaccard."""
    t = re.sub(r"[^\w\s]", " ", str(text or "").lower(), flags=re.UNICODE)
    return set(w for w in re.split(r"\s+", t) if len(w) >= min_len)


def _jaccard(a, b):
    """Similarite de Jaccard entre deux ensembles (0.0 a 1.0)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _idea_text(idea):
    """Texte titre+hook d'une idee (surface de comparaison principale)."""
    title = str(idea.get("title") or "")
    hook = str(idea.get("hook") or "")
    return title + " " + hook


def _idea_script(idea):
    """Script normalise en texte brut (contrat TTS via as_script_text)."""
    try:
        return as_script_text(idea.get("script"))
    except Exception:
        return str(idea.get("script") or "")


def _iter_json_files(directory, skip_names=("latest.json",)):
    """Fichiers *.json d'un repertoire, tries par mtime le plus recent d'abord."""
    if not directory or not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name in skip_names:
            continue
        out.append(p)
    return out


def load_published_corpus(cfg, limit=None):
    """Corpus des idees DEJA produites (ideas + remakes), les plus recentes
    d'abord. Sert de reference d'originalite croisee intra-chaine.

    Renvoie (list[dict], total_files) - la liste est tronquee au cap
    corpus_max pour garder un gate rapide.
    """
    comp = _cfg_section(cfg, "compliance")
    paths = get_paths(cfg)
    cap = int(limit or comp.get("corpus_max", 120))
    files = _iter_json_files(paths.ideas) + _iter_json_files(paths.remake)
    corpus = []
    for p in files[:max(1, cap)]:
        data = load_json(p)
        if not isinstance(data, dict):
            continue
        data["_src"] = str(p.name)
        corpus.append(data)
    return corpus, len(files)


# ---------------------------------------------------------------------------
# 1) Originalite croisee intra-chaine
# ---------------------------------------------------------------------------

def originality_report(idea, corpus, cfg):
    """Jaccard max du couple (titre+hook) et du script vs le corpus publie.

    Renvoie dict : {max_jaccard, max_script_jaccard, worst_match_src,
    threshold, threshold_script, corpus_size}. Le seuil est strict :
    au-dela de ~0.6 sur titre+hook, l'algorithme voit deux Shorts
    interchangeables (template farming) -> risque "reused content".
    """
    comp = _cfg_section(cfg, "compliance")
    th = float(comp.get("originality_max", 0.60))
    th_script = float(comp.get("script_originality_max", 0.65))

    idea_toks = _token_set(_idea_text(idea))
    idea_script_toks = _token_set(_idea_script(idea))
    max_j, max_js, worst = 0.0, 0.0, ""
    for pub in corpus:
        j = _jaccard(idea_toks, _token_set(_idea_text(pub)))
        if j > max_j:
            max_j, worst = j, str(pub.get("_src") or "")
        js = _jaccard(idea_script_toks, _token_set(_idea_script(pub)))
        if js > max_js:
            max_js = js
    return {
        "max_jaccard": round(max_j, 4),
        "max_script_jaccard": round(max_js, 4),
        "worst_match_src": worst,
        "threshold": th,
        "threshold_script": th_script,
        "corpus_size": len(corpus),
    }


# ---------------------------------------------------------------------------
# 2) Fatigue de format (monotonie de famille de hook)
# ---------------------------------------------------------------------------

def format_fatigue_report(idea, corpus, cfg):
    """Longueur de la serie CONSECUTIVE la plus recente d'idees publiees
    partageant la meme famille de hook que l'idee candidate.

    Une serie >= fatigue_max_consecutive (defaut 3) signifie : le viewer
    voit plusieurs Shorts avec le meme mecanisme d'accroche a la suite ->
    swipe-away monte, l'algorithme degrade. La nouvelle idee passerait en
    queue de serie si on la laissait partir.
    """
    comp = _cfg_section(cfg, "compliance")
    max_consec = int(comp.get("fatigue_max_consecutive", 3))
    fam = hook_family(idea.get("hook"))
    run = 0
    for pub in corpus:
        f = str(pub.get("_hook_family") or hook_family(pub.get("hook")))
        if f == fam:
            run += 1
        else:
            break
    return {
        "hook_family": fam,
        "consecutive_same_family": run,
        "max_consecutive": max_consec,
        # Contrat [compliance].fatigue_max_consecutive = 0 -> desactive
        # (docstring config.py). Sans ce garde, run >= 0 serait toujours
        # vrai et TOUTES les idees seraient rejetees.
        "fatigued": max_consec > 0 and run >= max_consec,
    }


# ---------------------------------------------------------------------------
# 3) Policy guard (formats interdits)
# ---------------------------------------------------------------------------

def policy_report(idea, cfg):
    """Detection des formats interdits dans titre+hook et script.

    Renvoie liste de violations [{pattern, source_field}].
    """
    fields = {"title": _idea_text(idea), "script": _idea_script(idea)}
    comp = _cfg_section(cfg, "compliance")
    extra = [(str(p), str(p)) for p in (comp.get("banned_patterns") or [])]
    violations = []
    for rx, label in list(_BANNED_PATTERNS) + extra:
        for field, text in fields.items():
            try:
                if re.search(rx, text, flags=re.IGNORECASE):
                    violations.append({"pattern": label, "source_field": field})
            except re.error:
                continue
    return violations


# ---------------------------------------------------------------------------
# 4) Divulgation contenu altere (Altered Content / AI)
# ---------------------------------------------------------------------------

def ai_disclosure_value(cfg):
    """Resout la valeur de status.containsSyntheticMedia depuis la config.

    Renvoie (value, configured) :
    - value None    -> champ omis de la requete API (comportement historique) ;
    - value True/False -> declare. Pour ce bot (TTS + scripts IA + stock
      clips), False est le declaratif correct : la video n'est pas un
      "contenu reel modifie pour paraitre reel" (pas de visage clone,
      pas d'evenement factice) - ce qui n'exige PAS la divulgation selon
      la policy YouTube, mais le champ doit exister et etre explicite
      pour l'audit et pour la transparen ce vis-a-vis de YouTube.
    """
    up = _cfg_section(cfg, "upload")
    val = up.get("altered_content")
    if val is None:
        return None, False
    if isinstance(val, bool):
        return val, True
    # Chaine "true"/"false"/"1"/"0" toleree (TOML bool natif sinon string).
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True, True
    if s in ("false", "0", "no", "off"):
        return False, True
    return None, False


# ---------------------------------------------------------------------------
# Journalisation (audit)
# ---------------------------------------------------------------------------

def _compliance_file(cfg, name):
    return get_paths(cfg).compliance / name


def _append_jsonl(path, row):
    """Append une ligne JSON (journal d'audit). Ne leve jamais."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False,
                                default=str) + "\n")
    except Exception as exc:
        log.warning("compliance: journal indisponible (%s): %s", path, exc)


def log_defect(cfg, defect_type, detail=None):
    """Journal de defauts data/compliance/defects.jsonl (auditable)."""
    row = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "type": defect_type,
        "detail": detail or {},
    }
    _append_jsonl(_compliance_file(cfg, "defects.jsonl"), row)


# ---------------------------------------------------------------------------
# Gate principal
# ---------------------------------------------------------------------------

def enforce_compliance(idea, cfg):
    """Verifie une idee, la marque (idea["compliance"]) et arbitre.

    Renvoie (ok, report_dict) : ok=False <=> l'idee doit etre REJETEE
    (mode block uniquement). En mode warn : ok=True + violations marquees.
    En mode off : ok=True, report minimal, aucun controle execute.
    Aucune exception ne remonte : echec interne -> defect INSUFFICIENT_DATA
    + ok=True (fail-open).
    """
    comp = _cfg_section(cfg, "compliance")
    mode = str(comp.get("mode", "block")).lower()
    report = {"mode": mode, "ok": True, "reasons": [],
              "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
    idea["compliance"] = report
    if mode == "off":
        return True, report
    try:
        corpus, _n_files = load_published_corpus(cfg)
        # 1) Originalite croisee
        orig = originality_report(idea, corpus, cfg)
        report["originality"] = orig
        if orig["corpus_size"] < int(comp.get("corpus_min", 3)):
            log_defect(cfg, "INSUFFICIENT_DATA", {
                "check": "originality",
                "corpus_size": orig["corpus_size"],
            })
        if (orig["max_jaccard"] >= orig["threshold"]
                or orig["max_script_jaccard"] >= orig["threshold_script"]):
            report["reasons"].append(
                "originality: jaccard=%.2f/%.2f vs %s"
                % (orig["max_jaccard"], orig["max_script_jaccard"],
                   orig["worst_match_src"]))

        # 2) Fatigue de format
        fatigue = format_fatigue_report(idea, corpus, cfg)
        report["fatigue"] = fatigue
        if fatigue["fatigued"]:
            report["reasons"].append(
                "fatigue: famille '%s' repetee %dx consecutif"
                % (fatigue["hook_family"],
                   fatigue["consecutive_same_family"]))

        # 3) Policy guard
        violations = policy_report(idea, cfg)
        report["policy"] = {"violations": violations}
        for v in violations:
            report["reasons"].append("policy: %s (%s)"
                                     % (v["pattern"], v["source_field"]))

        # 4) Divulgation AI (informatif : consomme par upload.py)
        disclosure, configured = ai_disclosure_value(cfg)
        report["ai_disclosure"] = {
            "contains_synthetic_media": disclosure,
            "configured": configured,
        }
        if not configured:
            report["reasons"].append(
                "disclosure: [upload].altered_content non configure "
                "-> status.containsSyntheticMedia omis")

        rejected = bool(report["reasons"])
        report["ok"] = not rejected
        if rejected:
            idea_id = str(idea.get("id") or idea.get("pair_id") or "?")
            title = str(idea.get("title") or "")[:80]
            log.warning("COMPLIANCE [%s] %s : %s", mode, idea_id,
                        "; ".join(report["reasons"]))
            if mode == "block":
                _append_jsonl(_compliance_file(cfg, "rejected.jsonl"), {
                    "ts": report["checked_at"],
                    "idea_id": idea_id,
                    "title": title,
                    "reasons": report["reasons"],
                    "report": report,
                })
        return (not rejected if mode == "block" else True), report
    except Exception as exc:  # jamais fatal
        log.warning("compliance: echec interne non fatal: %s", exc)
        report["status"] = "error"
        report["error"] = str(exc)[:200]
        log_defect(cfg, "INSUFFICIENT_DATA", {
            "check": "enforce_compliance", "error": str(exc)[:200]})
        return True, report


def rejected_ideas_recent(cfg, limit=20):
    """Relit les dernieres idees rejetees (pour weekly.py / supervision)."""
    path = _compliance_file(cfg, "rejected.jsonl")
    rows = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-500:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        log.warning("compliance: rejected.jsonl illisible: %s", exc)
    return rows[-limit:]
