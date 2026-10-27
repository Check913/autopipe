"""A/B test de hook : tracker variant_id -> retention (V2 2026-10-27).

Pourquoi : jusqu'ici un seul hook par sujet -> aucune donnee sur CE QUI
accroche. Ici chaque sujet genere deux hooks (famille A != famille B via
identity.hook_variants), un seul est publie (alternance deterministic),
et summarize() joint les variantes aux ratios d'engagement
(data/analytics/retention_latest.json) pour savoir quelle famille retient.

Fichiers (sous data/hook_ab/) :
  assignments.jsonl  une ligne par sujet : {ts, pair_id, topic, variant,
                     hook, family, hook_a, hook_b, video_id}
  summary.json       dernier resultat de summarize()
"""
import datetime as dt
import json
import os

from .config import get_config, get_paths
from .util import get_logger, load_json, save_json

log = get_logger("hook_ab")


def enabled(cfg=None):
    return bool((cfg or get_config()).get("hook_ab", {}).get("enabled", True))


def _dir(cfg):
    d = get_paths(cfg).data / "hook_ab"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _assign_file(cfg):
    return _dir(cfg) / "assignments.jsonl"


def _load_assignments(cfg):
    f = _assign_file(cfg)
    rows = []
    if f.exists():
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _write_assignments(cfg, rows):
    f = _assign_file(cfg)
    with open(f, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def assign(topic, variants, cfg=None):
    """Enregistre la paire A/B d'un sujet et choisit la variante publiee.

    variants : sortie de identity.hook_variants() = [{variant, hook,
    family}, ...] (A et B). Alternance deterministic : le compteur global
    de paires alterne A/B, donc ~50/50 sans dependre du hasard.

    Renvoie le dict de la variante CHOISIE, augmente de pair_id et
    topic (None si variants invalide ou hook_ab desactive).
    """
    cfg = cfg or get_config()
    if not enabled(cfg) or not variants or len(variants) < 2:
        return None
    hook_a = str(variants[0].get("hook") or "")
    hook_b = str(variants[1].get("hook") or "")
    if not hook_a and not hook_b:
        return None
    rows = _load_assignments(cfg)
    pair_id = "pair_%s_%s" % (
        dt.datetime.now().strftime("%Y%m%d"),
        "%04d" % (len(rows) + 1))
    variant = "A" if len(rows) % 2 == 0 else "B"
    chosen = variants[0] if variant == "A" else variants[1]
    row = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "pair_id": pair_id,
        "topic": str(topic or ""),
        "variant": variant,
        "hook": chosen.get("hook") or "",
        "family": chosen.get("family") or "",
        "family_other": variants[0 if variant == "B" else 1].get("family") or "",
        "hook_a": hook_a,
        "hook_b": hook_b,
        "video_id": None,
    }
    rows.append(row)
    _write_assignments(cfg, rows)
    log.info("Hook A/B: paire %s -> variante %s publiee (%s)",
             pair_id, variant, row["hook"][:50])
    out = dict(chosen)
    out["pair_id"] = pair_id
    out["topic"] = row["topic"]
    return out


def attach_video(pair_id, video_id, cfg=None):
    """Attache l'id YouTube d'un Short a sa paire A/B (apres upload)."""
    cfg = cfg or get_config()
    if not pair_id or not video_id:
        return False
    rows = _load_assignments(cfg)
    hit = False
    for r in rows:
        if r.get("pair_id") == pair_id and not r.get("video_id"):
            r["video_id"] = str(video_id)
            hit = True
    if hit:
        _write_assignments(cfg, rows)
        log.info("Hook A/B: %s -> video %s", pair_id, video_id)
    return hit


def _norm(s):
    return " ".join(str(s or "").lower().split())


def summarize(cfg=None):
    """Joint variantes et engagement : quelle famille de hook retient ?

    Source engagement : data/analytics/retention_latest.json (ratio
    engagedViews/views, repli averageViewPercentage). Jointure par
    video_id d'abord, puis par titre (fallback heuristique : le titre du
    Short contient le sujet). Agrège par variante : n, mean engagement,
    mean views. Ecrit data/hook_ab/summary.json et renvoie le dict.
    """
    cfg = cfg or get_config()
    from .analytics import engagement_value
    rows = [r for r in _load_assignments(cfg) if r.get("variant")]
    if not rows:
        return {"status": "no_assignments", "variants": {}}
    ret_rows = (load_json(get_paths(cfg).analytics / "retention_latest.json")
                or {}).get("rows") or []
    by_id = {str(r.get("id")): r for r in ret_rows if r.get("id")}
    by_title = {_norm(r.get("title")): r for r in ret_rows if r.get("title")}
    joined = 0
    for r in rows:
        row = None
        vid = r.get("video_id")
        if vid and str(vid) in by_id:
            row = by_id[str(vid)]
        else:
            t = _norm(r.get("topic"))
            if t:
                for title, cand in by_title.items():
                    if t and t in title:
                        row = cand
                        break
        if row:
            r["engagement"] = engagement_value(row)
            r["views"] = int(row.get("views") or 0)
            joined += 1
        else:
            r["engagement"] = None
            r["views"] = None
    stats = {}
    for variant in ("A", "B"):
        vals = [r["engagement"] for r in rows
                if r["variant"] == variant and r["engagement"] is not None]
        views = [r["views"] for r in rows
                 if r["variant"] == variant and r["views"]]
        fams = {}
        for r in rows:
            if r["variant"] == variant and r.get("family"):
                fams[r["family"]] = fams.get(r["family"], 0) + 1
        stats[variant] = {
            "assigned": sum(1 for r in rows if r["variant"] == variant),
            "measured": len(vals),
            "mean_engagement": round(sum(vals) / len(vals), 1) if vals else None,
            "mean_views": round(sum(views) / len(views)) if views else None,
            "families": fams,
        }
    out = {"status": "ok", "pairs": len(rows), "joined": joined,
           "variants": stats, "rows": rows,
           "fetched": dt.datetime.now().isoformat(timespec="seconds")}
    save_json(_dir(cfg) / "summary.json", out)
    if joined:
        a, b = stats["A"], stats["B"]
        log.info("Hook A/B: %s paires jointes - A: %s%% (%s) vs "
                 "B: %s%% (%s)", joined, a["mean_engagement"],
                 a["families"], b["mean_engagement"], b["families"])
    else:
        log.info("Hook A/B: %s paires enregistrees, aucune encore jointe "
                 "aux analytics", len(rows))
    return out


def best_family(cfg=None):
    """Famille de hook la mieux retenue (None si donnees insuffisantes).

    Sert a l'optimizer : si une famille domine clairement (>= 3 mesures,
    ecart >= 5 pts), les prompts futurs peuvent la privilegie.
    """
    s = summarize(cfg)
    a, b = s.get("variants", {}).get("A", {}), \
        s.get("variants", {}).get("B", {})
    if not a.get("mean_engagement") or not b.get("mean_engagement"):
        return None
    if min(a.get("measured", 0), b.get("measured", 0)) < 3:
        return None
    if abs(a["mean_engagement"] - b["mean_engagement"]) < 5.0:
        return None
    fam = None
    if a["mean_engagement"] > b["mean_engagement"]:
        fam = max(a.get("families", {}), key=lambda k: a["families"][k],
                  default=None)
    else:
        fam = max(b.get("families", {}), key=lambda k: b["families"][k],
                  default=None)
    return fam