"""Module 3 - Boucle auto-apprenante (analytics -> patterns -> config).

Analyse les performances des videos publiees (data/analytics/latest.json,
YouTube Data API v3 via upload OAuth scopes youtube.readonly) et les
caracteristiques de chaque video generee (data/remake/*.json : type de
hook, longueur du titre, densite de retention, style, ...).

En croisant les deux, le module extrait des "patterns gagnants" (ex.
"les hooks en question font +15% d'engagement") et les convertit en
ajustements bornees de la configuration des lots suivants :
  - cfg["ideas"]["style_directive"] / prompt de generation,
  - cfg["video"]["hook_font_size"] / "hook_duration",
  - cfg["llm"]["temperature"],
  - cfg["remake"]["interrupt_interval"].

Tout est garde dans data/optimizer/patterns.json. Aucune exception ne
remonte : moins de min_samples videos -> apprentissage en attente, la
configuration reste intouchable (comportement par defaut preserve).
"""
import datetime as dt
import glob
import json
import math
import re
import statistics
from pathlib import Path

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("optimizer")

_QUESTION_RE = re.compile(r"^(who|what|why|how|when|where|which|can|do|does|did|is|are)\b", re.I)
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")


def _remake_files(cfg=None):
    """Fichiers remake JSON (ideas generees) tries du plus recent."""
    paths = get_paths(cfg)
    files = sorted(glob.glob(str(paths.remake / "*.json")))
    return [f for f in files
            if not Path(f).name.startswith(("hits_", "remakes_"))]


def _features_from_remake(path):
    """Caracteristiques editables d'une idee generatee (sans LLM)."""
    data = load_json(path) or {}
    title = str(data.get("title") or "")
    hook = str(data.get("hook") or "")
    analysis = data.get("analysis") or {}
    ret = analysis.get("retention") or {}
    script = str(data.get("script") or "")
    words = len(script.split())
    return {
        "id": data.get("id") or Path(path).stem,
        "title": title,
        "title_len": len(title),
        "hook_type": ("question" if _QUESTION_RE.search(hook.strip())
                      else ("number" if _NUMBER_RE.search(hook[:60])
                            else "statement")),
        "hook_len": len(hook),
        "title_has_number": bool(_NUMBER_RE.search(title)),
        "duration_target": float(data.get("duration_target") or 0.0),
        "score": float(data.get("score") or 0.0),
        "originality": float((data.get("originality") or {}).get("max", 0.0)
                             if isinstance(data.get("originality"), dict)
                             else data.get("originality") or 0.0),
        "retention_density": float(ret.get("density") or 0.0),
        "retention_spikes": len(ret.get("spikes_s") or []),
        "source_topic": str(data.get("source_topic") or ""),
    }


def _engagement_index(row):
    """Proxy d'engagement normalise d'une video publiee (0..100).

    Avec peu de vues au debut, likes+comments pesent plus : index =
    50 * log1p(views) / log1p(10000) + 25 * likes/max(views,1)*100 borne
    + 25 * comments/max(views,1)*100 borne. Simple, monotone, sans API
    Analytics payante.
    """
    views = float(row.get("views") or 0.0)
    likes = float(row.get("likes") or 0.0)
    comments = float(row.get("comments") or 0.0)
    reach = 50.0 * min(1.0, math.log1p(views) / math.log1p(10000.0))
    like_rate = 25.0 * min(1.0, (likes / views if views > 0 else 0.0) / 0.05)
    com_rate = 25.0 * min(1.0, (comments / views if views > 0 else 0.0) / 0.01)
    return round(reach + like_rate + com_rate, 2)


# ---------------------------------------------------------------------------
# Correlation features <-> performance -> patterns
# ---------------------------------------------------------------------------

def _join_samples(cfg=None):
    """Joint les remakes generes aux analytics publiees par id de video.

    Renvoie [{features..., engagement}] pour les videos retrouvees dans
    latest.json (les non publiees servent aux stats descriptives).
    """
    paths = get_paths(cfg)
    latest = load_json(paths.analytics / "latest.json") or []
    rows = latest if isinstance(latest, list) else latest.get("rows", [])
    by_id = {str(r.get("id")): r for r in rows if r.get("id")}
    # Retention (proxy Viewed vs Swiped Away, Analytics API scope separe).
    ret = {}
    try:
        rdoc = load_json(paths.analytics / "retention_latest.json") or {}
        ret = {str(r.get("id")): r for r in (rdoc.get("rows") or [])}
    except Exception:
        ret = {}
    samples = []
    for path in _remake_files(cfg):
        feats = _features_from_remake(path)
        row = by_id.get(str(feats["id"]))
        if row:
            feats["engagement"] = _engagement_index(row)
            rrow = ret.get(str(feats["id"]))
            if rrow:
                feats["avg_view_pct"] = float(rrow.get("avg_view_pct") or 0.0)
            samples.append(feats)
    return samples


def extract_patterns(samples):
    """Patterns gagnants = deltas d'engagement par caracteristique.

    Pour chaque dimension discrete (hook_type, title_has_number), compare
    l'engagement moyen du groupe au global : un ecart >= 15% relatif est
    un pattern. Renvoie {patterns: [...], stats: {...}}.
    """
    n = len(samples)
    if n == 0:
        return {"patterns": [], "stats": {"n_samples": 0}}
    eng = [s["engagement"] for s in samples]
    global_mean = sum(eng) / n
    patterns = []
    for field, label in (("hook_type", "hook type"), ("title_has_number",
                                                      "chiffre dans le titre")):
        groups = {}
        for s in samples:
            key = str(s.get(field))
            groups.setdefault(key, []).append(s["engagement"])
        for key, vals in groups.items():
            if len(vals) < 2 or len(groups) < 2:
                continue
            mean = sum(vals) / len(vals)
            rel = (mean - global_mean) / max(global_mean, 1.0)
            if abs(rel) >= 0.15:
                patterns.append({
                    "dimension": field,
                    "value": key,
                    "n": len(vals),
                    "mean_engagement": round(mean, 2),
                    "relative_delta": round(rel, 3),
                    "label": "%s=%s -> %s%.0f%% d'engagement" % (
                        label, key, "+" if rel > 0 else "", rel * 100),
                })
    patterns.sort(key=lambda p: -abs(p["relative_delta"]))
    return {
        "patterns": patterns,
        "stats": {
            "n_samples": n,
            "mean_engagement": round(global_mean, 2),
            "engagement_min": round(min(eng), 2),
            "engagement_max": round(max(eng), 2),
        },
    }


def _clip(value, base, max_delta):
    """Ajustement borne : value reste dans [base*(1-d), base*(1+d)]."""
    lo, hi = base * (1.0 - max_delta), base * (1.0 + max_delta)
    return max(lo, min(hi, value))


def apply_config(cfg, patterns_doc=None):
    """Ajuste cfg en place d'apres data/optimizer/patterns.json.

    Non-cassant : aucune cle n'est cree hors de celles deja lues par le
    pipeline ; les ajustements sont bornes par [optimizer].max_delta.
    Renvoie la liste des ajustements appliques (vide si rien).
    """
    oc = cfg.get("optimizer", {})
    if not oc.get("enabled", True):
        return []
    applied = []

    # Boucle retention (critique 2026-10-27) : directive derivee de la
    # metrique averageViewPercentage (proxy Viewed vs Swiped Away, cible
    # 70 %). Calculee AVANT le early-return : elle s'applique meme sans
    # pattern (des la 1re video publiee).
    try:
        from .analytics import hook_directive
        retention_directive = hook_directive(cfg)
    except Exception:
        retention_directive = ""

    if patterns_doc is None:
        paths = get_paths(cfg)
        file = oc.get("patterns_file") or ""
        src = Path(file) if file else paths.optimizer / "patterns.json"
        patterns_doc = load_json(src)
    if not patterns_doc or not patterns_doc.get("patterns"):
        if retention_directive:
            cfg.setdefault("ideas", {})["style_directive"] = retention_directive
            cfg.setdefault("remake", {})["style_directive"] = retention_directive
            applied.append("style_directive (retention hook discipline)")
        return applied
    max_delta = float(oc.get("max_delta", 0.35))

    def _num(section, key, factor, clamp=None):
        base = cfg.get(section, {}).get(key)
        if isinstance(base, (int, float)) and base > 0:
            new = _clip(float(base) * factor, float(base), max_delta)
            if clamp:
                new = max(clamp[0], min(clamp[1], new))
            cfg[section][key] = round(new, 2)
            applied.append("%s.%s: %s -> %s" % (section, key, base,
                                                cfg[section][key]))

    for p in patterns_doc["patterns"]:
        factor = 1.0 + max(-max_delta, min(max_delta, p["relative_delta"]))
        if p["dimension"] == "hook_type" and p["value"] == "question" \
                and p["relative_delta"] > 0:
            _num("video", "hook_duration", factor)
            # Hooks en question retenus -> remakes moins deviants (scripts plus
            # calibres sur la formule psychologique de l'original).
            _num("remake", "temperature", 2.0 - factor)
        elif p["dimension"] == "hook_type" and p["value"] == "number" \
                and p["relative_delta"] > 0:
            _num("llm", "temperature", 2.0 - factor)  # titles chiffres: moins creatif
        elif p["dimension"] == "title_has_number":
            _num("video", "hook_font_size", factor)
    labels = [p["label"] for p in patterns_doc["patterns"][:3]]
    if labels:
        directive = "Patterns appris (auto-optimisation): " + " | ".join(labels)
        if retention_directive:
            directive = retention_directive + " | " + directive
        cfg.setdefault("ideas", {})["style_directive"] = directive
        # Meme directive pour les prompts Super-Remake (_rewrite_user_prompt).
        cfg.setdefault("remake", {})["style_directive"] = directive
        applied.append("style_directive (ideas+remake): %d patterns" % len(labels))
    elif retention_directive:
        cfg.setdefault("ideas", {})["style_directive"] = retention_directive
        cfg.setdefault("remake", {})["style_directive"] = retention_directive
        applied.append("style_directive (retention hook discipline)")
    return applied


def run_optimizer(cfg=None):
    """Boucle complete : collecte -> correlation -> patterns.json.

    Renvoie {status: learned|pending|disabled|error, n_samples, patterns,
    applied_to_config?}. NE LEVE JAMAIS d'exception (garantie scheduler).
    """
    try:
        cfg = cfg or get_config()
        oc = cfg.get("optimizer", {})
        if not oc.get("enabled", True):
            return {"status": "disabled", "n_samples": 0, "patterns": []}
        paths = get_paths(cfg)
        min_samples = int(oc.get("min_samples", 3))
        samples = _join_samples(cfg)
        doc = extract_patterns(samples)
        out = {
            "fetched": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "learned" if len(samples) >= min_samples else "pending",
            "min_samples": min_samples,
            "n_samples": len(samples),
            **doc,
        }
        save_json(paths.optimizer / "patterns.json", out)
        if out["status"] == "learned":
            log.info("Optimizer: %d patterns appris sur %d videos",
                     len(doc["patterns"]), len(samples))
        else:
            log.info("Optimizer: %d/%d videos publiees analysees - "
                     "apprentissage en attente", len(samples), min_samples)
        return out
    except Exception as exc:
        log.warning("Optimizer degrade: %s", exc)
        return {"status": "error", "error": str(exc), "n_samples": 0,
                "patterns": []}


def hook_before_batch(cfg):
    """Point d'entree scheduler : apprend PUIS applique a la cfg du lot.

    Ordonne : run_optimizer (ecrit patterns.json) -> apply_config (mute
    cfg). Les deux etapes sont no-fail : le pipeline tourne toujours.
    """
    try:
        doc = run_optimizer(cfg)
        # apply_config aussi en 'pending' : la directive retention (hook
        # discipline) s'applique des la 1re video publiee, sans attendre
        # min_samples patterns.
        if doc.get("status") in ("learned", "pending"):
            applied = apply_config(cfg, patterns_doc=doc)
            if applied:
                log.info("Optimizer: config ajustee pour ce lot: %s",
                         "; ".join(applied))
        return doc
    except Exception as exc:  # double ceinture : jamais bloquer un batch
        log.warning("optimizer.hook_before_batch degrade: %s", exc)
        return {"status": "error", "error": str(exc)}
