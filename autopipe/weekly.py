# -*- coding: utf-8 -*-
"""Rapport hebdomadaire winner amplification (MODULE 7).

Principe 2026 : une chaine Shorts decolle quand elle REPLIQUE ce qui
marche (top performers) et ARRETE ce qui ne marche pas (dead shorts,
familles sous seuil, fatigue de format). Ce module fusionne, sans aucun
appel reseau :

  - data/analytics/latest.json        (vues/likes/commentaires snapshot)
  - data/analytics/retention_latest.json (engaged_ratio / avg_view_pct)
  - data/registry/published.json      (hook -> famille, format, source)
  - data/optimizer/patterns.json      (patterns appris actifs)
  - data/optimizer/killswitch.json    (familles bannies + alerte niche)
  - data/compliance/rejected.jsonl    (rejets de conformite recents)

API :
  weekly_report(cfg, days)   -> dict complet (join de toutes les sources)
  render(report)             -> texte lisible pour le terminal / mail
  run_weekly(cfg, apply)     -> ecrit data/weekly/report.json (+ history)
  apply_directives(cfg, rep) -> OPT-IN : ban des familles des dead shorts

`--apply` ecrit dans data/optimizer/killswitch.json le schema EXACT de
optimizer.update_kill_switch : banned_families[fam] = {"remaining": N,
"banned_at": iso, "last_ids": [...]}. Un ban existant n'est JAMAIS
ecrase (on garde le "remaining" le plus long). Le decompte reste
decremente par l'optimizer a chaque Short publie analyse : le module
hebdo n'ajoute que des bans manuels, aucun autre fichier n'est touche.

Aucune exception ne remonte : en cas de donnee manquante ou corrompue,
le rapport est degrade (totaux a zero, directive "donnees insuffisantes")
mais reste lisible. Conformement au playbook : aucun bot ne garantit la
monetisation - le rapport maximise les chances, il ne les garantit pas.
"""

import datetime as dt
import json

from .config import get_config, get_paths
from .identity import hook_family
from .util import get_logger, load_json, save_json

log = get_logger("weekly")

DEFAULT_DAYS = 7
DEFAULT_TOP_N = 5
DEFAULT_DEAD_DAYS = 60
DEFAULT_DEAD_VIEWS = 0
DEFAULT_MAX_BAN_FAMILIES = 3


# --------------------------------------------------------------------------
# Helpers robustes
# --------------------------------------------------------------------------
def _section(cfg):
    sec = (cfg or {}).get("weekly")
    return sec if isinstance(sec, dict) else {}


def _num(sec, key, default, cast=float):
    try:
        return cast(sec.get(key, default))
    except (TypeError, ValueError):
        return cast(default)


def _int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _round(value, ndigits=1):
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _parse_dt(value):
    """Parse un timestamp ISO (avec ou sans Z / timezone). None si invalide."""
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _age_days(published):
    d = _parse_dt(published)
    if d is None:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - d).total_seconds()
               / 86400.0)


def _engagement_value(rrow):
    """engaged_ratio, sinon avg_view_pct (compat), None si absent.

    Miroir local de analytics.engagement_value sans le 0.0 par defaut :
    sans mesure retention le rapport affiche n/d, pas un faux 0 %.
    """
    if not isinstance(rrow, dict):
        return None
    for key in ("engaged_ratio", "avg_view_pct"):
        v = rrow.get(key)
        if isinstance(v, (int, float)):
            return round(float(v), 1)
    return None


def _append_jsonl(path, row):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False,
                                separators=(",", ":")) + "\n")
    except OSError as exc:
        log.warning("weekly: ecriture %s ratee: %s", path, exc)


# --------------------------------------------------------------------------
# Sources de donnees (chaque lecture est isolee)
# --------------------------------------------------------------------------
def _analytics_rows(cfg):
    try:
        rows = load_json(get_paths(cfg).analytics / "latest.json", []) or []
    except Exception as exc:
        log.warning("weekly: latest.json illisible: %s", exc)
        return []
    return [r for r in rows if isinstance(r, dict)]


def _retention_map(cfg):
    try:
        doc = load_json(get_paths(cfg).analytics / "retention_latest.json") \
            or {}
    except Exception as exc:
        log.warning("weekly: retention illisible: %s", exc)
        return {}
    out = {}
    for r in (doc.get("rows") or []):
        if isinstance(r, dict) and r.get("id"):
            out[str(r["id"])] = r
    return out


def _registry_videos(cfg):
    try:
        from . import registry
        return registry.load(cfg).get("videos") or {}
    except Exception as exc:
        log.warning("weekly: registre indisponible: %s", exc)
        return {}


def _kill_switch(cfg):
    try:
        return load_json(get_paths(cfg).optimizer / "killswitch.json") or {}
    except Exception as exc:
        log.warning("weekly: killswitch.json illisible: %s", exc)
        return {}


def _patterns_doc(cfg):
    try:
        return load_json(get_paths(cfg).optimizer / "patterns.json") or {}
    except Exception as exc:
        log.warning("weekly: patterns.json illisible: %s", exc)
        return {}


def _compliance_rejections(cfg, limit=50):
    try:
        from .compliance import rejected_ideas_recent
        return rejected_ideas_recent(cfg, limit=limit) or []
    except Exception as exc:
        log.warning("weekly: rejets conformite indisponibles: %s", exc)
        return []


# --------------------------------------------------------------------------
# Join principal
# --------------------------------------------------------------------------
def _joined_rows(cfg):
    """Fusion analytics latest + registre + retention, par video.

    Les vues/likes/commentaires viennent du snapshot analytics (fraiche
    donnee API), le hook/format/source du registre, l'engagement de la
    derniere mesure retention (analytics.engagement_value).
    """
    paths = get_paths(cfg)
    out = []
    reg = _registry_videos(cfg)
    ret = _retention_map(cfg)
    for row in _analytics_rows(cfg):
        vid = str(row.get("id") or "")
        if not vid:
            continue
        entry = reg.get(vid) or {}
        rrow = ret.get(vid) or {}
        hook = entry.get("hook")
        title = entry.get("title") or row.get("title") or ""
        published = row.get("published") or entry.get("published_at")
        out.append({
            "video_id": vid,
            "title": title,
            "views": _int(row.get("views")),
            "likes": _int(row.get("likes")),
            "comments": _int(row.get("comments")),
            "published": published,
            "age_days": _age_days(published),
            "engagement_pct": _engagement_value(rrow),
            "hook": hook,
            "hook_family": hook_family(hook or title),
            "hook_variant": entry.get("hook_variant"),
            "duration_format": entry.get("duration_format"),
            "generator": entry.get("generator"),
            "source": entry.get("source"),
            "idea_id": entry.get("idea_id"),
        })
    return out


def _cta_gaps(cfg, rows):
    """CTA manquant : le CTA AutoPipe = commentaire epingle (upload).

    Signal global : auto_comment desactive -> TOUTE la chaine perd le
    levier commentaires (session-time, signal d'engagement 2026).
    Signal par video : 0 commentaire apres 7 jours sur une video avec
    des vues (l'audience n'engage pas -> CTA absent ou sans effet).
    """
    gaps = []
    uc = (cfg or {}).get("upload") or {}
    if not uc.get("auto_comment", True):
        gaps.append({"scope": "global",
                     "reason": "auto_comment desactive dans [upload]"})
    for r in rows:
        if r["views"] > 0 and r["comments"] == 0 and \
                (r["age_days"] or 0) >= 7:
            gaps.append({"scope": "video", "video_id": r["video_id"],
                         "title": r["title"], "views": r["views"],
                         "age_days": _round(r["age_days"], 0),
                         "reason": "0 commentaire apres 7 jours "
                                   "(CTA absent ou sans effet)"})
    return gaps


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------
def weekly_report(cfg=None, days=None):
    """Rapport hebdo complet : tops, dead shorts, CTA, patterns, directives."""
    cfg = cfg or get_config()
    sec = _section(cfg)
    if days is None:
        days = _num(sec, "days", DEFAULT_DAYS, int)
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    now = dt.datetime.now(dt.timezone.utc)

    rows = _joined_rows(cfg)
    for r in rows:
        r["in_window"] = bool(r["age_days"] is not None and
                              r["age_days"] <= days)

    published_in_window = [r for r in rows if r["in_window"]]
    totals = {
        "videos": len(rows),
        "published_in_window": len(published_in_window),
        "views": sum(r["views"] for r in rows),
        "views_in_window": sum(r["views"] for r in published_in_window),
        "likes": sum(r["likes"] for r in rows),
        "comments": sum(r["comments"] for r in rows),
    }

    # Winner amplification : d'abord la fenetre, sinon tout l'historique
    top_n = _num(sec, "top_n", DEFAULT_TOP_N, int)
    pool = published_in_window or rows
    ranked = sorted(pool, key=lambda r: (r["views"], r["engagement_pct"] or 0),
                    reverse=True)
    top_performers = [r for r in ranked if r["views"] > 0][:top_n]

    # Dead shorts : 0 vue apres dead_days (formulaire sature ou sujet mort)
    dead_days = _num(sec, "dead_days", DEFAULT_DEAD_DAYS, float)
    dead_views = _num(sec, "dead_views", DEFAULT_DEAD_VIEWS, int)
    dead_shorts = [r for r in rows
                   if r["views"] <= dead_views
                   and r["age_days"] is not None
                   and r["age_days"] >= dead_days]
    dead_shorts.sort(key=lambda r: (r["age_days"] or 0), reverse=True)

    cta_gaps = _cta_gaps(cfg, rows)

    pdoc = _patterns_doc(cfg)
    patterns = {
        "mode": pdoc.get("mode"),
        "lessons": _int(pdoc.get("lessons")),
        "active": [str(p) for p in (pdoc.get("patterns") or []) if p][:10],
        "baseline_delta": pdoc.get("baseline_delta") or {},
    }

    ks = _kill_switch(cfg)
    niche = ks.get("niche") or {}
    kill_switch = {
        "banned_families": {str(k): dict(v or {})
                            for k, v in (ks.get("banned_families")
                                         or {}).items()},
        "niche_alert": bool(niche.get("alert")),
        "niche_topic": niche.get("topic"),
        "niche_escape_remaining": _int(niche.get("escape_remaining")),
    }

    rej_rows = _compliance_rejections(cfg, limit=50)
    by_reason = {}
    for rr in rej_rows:
        rr = rr or {}
        # Schema REEL de compliance.rejected.jsonl : "reasons" = liste de
        # raisons detaillees ("originality: jaccard=0.71 ..."). On regroupe
        # par famille (texte avant ":") pour un resume lisible ; une cle
        # "reason" (string) reste acceptee pour compatibilite.
        reasons = rr.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        if not reasons and rr.get("reason"):
            reasons = [str(rr["reason"])]
        for reason in (reasons or ["unknown"]):
            fam = str(reason).split(":", 1)[0].strip() or "unknown"
            by_reason[fam] = by_reason.get(fam, 0) + 1

    report = {
        "generated": now.isoformat(timespec="seconds"),
        "days": days,
        "thresholds": {"dead_days": dead_days, "dead_views": dead_views,
                       "top_n": top_n},
        "totals": totals,
        "top_performers": top_performers,
        "dead_shorts": dead_shorts,
        "cta_gaps": cta_gaps,
        "patterns": patterns,
        "kill_switch": kill_switch,
        "compliance_rejections": {
            "recent": len(rej_rows),
            "by_reason": by_reason,
        },
        "directives": [],
    }
    report["directives"] = _directives(report)
    return report


def _directives(report):
    """Directives d'amplification : repliquer les tops, tuer les deads."""
    out = []
    for t in report["top_performers"]:
        fmt = t.get("duration_format") or "short"
        eng = t.get("engagement_pct")
        eng_s = ("%s%% engagement" % eng) if eng is not None else \
            "engagement n/d"
        out.append(
            "AMPLIFIER: repliquer famille '%s' + format %s (%s vues, %s) - "
            "memes structures de hook et de boucle, sujet renouvele"
            % (t["hook_family"], fmt, t["views"], eng_s))
    for d in report["dead_shorts"][:5]:
        out.append(
            "STOP: dead short %.0f j / %s vues (famille '%s') - lancer "
            "`python -m autopipe.cli weekly --apply` pour bannir la famille"
            % (d["age_days"] or 0, d["views"], d["hook_family"]))
    for fam, meta in report["kill_switch"]["banned_families"].items():
        out.append("EVITER: famille '%s' bannie par le kill switch "
                   "(%s Shorts restants)" % (fam, _int(meta.get("remaining"))))
    if report["kill_switch"]["niche_alert"]:
        out.append("NICHE: alerte active sur '%s' - changer de sujet pour "
                   "%s Shorts (escape restant)"
                   % (report["kill_switch"]["niche_topic"],
                      report["kill_switch"]["niche_escape_remaining"]))
    if report["totals"]["published_in_window"] and \
            report["totals"]["views_in_window"] == 0:
        out.append("ALERTE: Shorts publies dans la fenetre mais 0 vue - "
                   "verifier la fenetre de publication (14/18/22/02 UTC) "
                   "et la geo US")
    if not report["top_performers"] and not report["dead_shorts"]:
        out.append("DONNEES INSUFFISANTES: lancer `analytics` puis "
                   "`optimize` pour alimenter le rapport")
    if report["compliance_rejections"]["recent"]:
        out.append("CONFORMITE: %s rejet(s) recents (%s) - resserrer la "
                   "generation sur les patterns actifs"
                   % (report["compliance_rejections"]["recent"],
                      ", ".join(sorted(report["compliance_rejections"]
                                       ["by_reason"])) or "n/d"))
    for gap in report["cta_gaps"]:
        if gap.get("scope") == "global":
            out.append("CTA: reactiver auto_comment (commentaire epingle) - "
                       "signal d'engagement 2026")
    return out[:12]


# --------------------------------------------------------------------------
# Render + run
# --------------------------------------------------------------------------
def render(report):
    """Texte lisible du rapport (terminal / mail hebdo)."""
    t = report["totals"]
    lines = ["RAPPORT HEBDO WINNER AMPLIFICATION (%s, fenetre %s j)"
             % (report["generated"][:10], report["days"]),
             "Videos: %s (%s dans la fenetre) | Vues: %s (%s fenetre) | "
             "Likes: %s | Commentaires: %s"
             % (t["videos"], t["published_in_window"], t["views"],
                t["views_in_window"], t["likes"], t["comments"])]
    lines.append("\nTOP PERFORMERS (a repliquer):")
    if report["top_performers"]:
        for r in report["top_performers"]:
            lines.append("  %-15s vues=%-7s eng=%-6s fam=%-9s fmt=%-8s %s"
                         % (r["video_id"], r["views"],
                            (("%s%%" % r["engagement_pct"])
                             if r["engagement_pct"] is not None else "n/d"),
                            r["hook_family"],
                            (r["duration_format"] or "?"),
                            (r["title"] or "")[:48]))
    else:
        lines.append("  (aucun top performer dans la donnee)")
    th = report.get("thresholds") or {}
    lines.append("\nDEAD SHORTS (a arreter, seuil %s vues / %s j):"
                 % (_int(th.get("dead_views"), DEFAULT_DEAD_VIEWS),
                    _int(th.get("dead_days"), DEFAULT_DEAD_DAYS)))
    if report["dead_shorts"]:
        for r in report["dead_shorts"][:10]:
            lines.append("  %-15s age=%-4.0f j vues=%-4s fam=%-9s %s"
                         % (r["video_id"], r["age_days"] or 0, r["views"],
                            r["hook_family"], (r["title"] or "")[:48]))
    else:
        lines.append("  (aucun dead short)")
    lines.append("\nDIRECTIVES:")
    for d in report["directives"]:
        lines.append("  - %s" % d)
    return "\n".join(lines)


def apply_directives(cfg, report):
    """OPT-IN : ban manuel des familles des dead shorts dans le kill switch.

    Schema identique a optimizer.update_kill_switch. Un ban existant
    n'est jamais ecrase (on garde le remaining courant). Renvoie la
    liste des familles effectivement bannies.
    """
    cfg = cfg or get_config()
    sec = _section(cfg)
    oc = (cfg or {}).get("optimizer") or {}
    try:
        ban_len = int(oc.get("kill_ban_shorts", 20))
    except (TypeError, ValueError):
        ban_len = 20
    max_bans = _num(sec, "max_ban_families", DEFAULT_MAX_BAN_FAMILIES, int)

    fam_ids = {}
    for d in (report or {}).get("dead_shorts") or []:
        fam = str(d.get("hook_family") or "statement")
        fam_ids.setdefault(fam, []).append(str(d.get("video_id") or ""))

    paths = get_paths(cfg)
    file = paths.optimizer / "killswitch.json"
    state = load_json(file) or {}
    banned = state.setdefault("banned_families", {})
    applied = []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for fam, ids in list(fam_ids.items())[:max_bans]:
        if fam in banned:
            continue  # jamais d'ecrasement : l'optimizer decremente deja
        banned[fam] = {"remaining": ban_len, "banned_at": now,
                       "last_ids": ids[-3:], "source": "weekly"}
        applied.append(fam)
    if applied:
        events = list(state.get("events") or [])
        events.append("WEEKLY BAN %s (dead shorts > seuil)"
                      % ", ".join(applied))
        state["events"] = events[-50:]
        state["updated"] = now
        save_json(file, state)
        _append_jsonl(paths.weekly / "actions.jsonl",
                      {"ts": now, "action": "ban_families",
                       "families": applied, "ban_shorts": ban_len})
        log.warning("WEEKLY --apply: familles bannies %s (%s Shorts)",
                    ", ".join(applied), ban_len)
    return applied


def run_weekly(cfg=None, apply=False, days=None):
    """Calcule + sauvegarde le rapport ; --apply ajoute les bans manuels."""
    cfg = cfg or get_config()
    paths = get_paths(cfg)
    report = weekly_report(cfg, days=days)
    file = paths.weekly / "report.json"
    save_json(file, report)
    _append_jsonl(paths.weekly / "history.jsonl",
                  {"generated": report["generated"], "days": report["days"],
                   "totals": report["totals"],
                   "top_ids": [r["video_id"] for r in
                               report["top_performers"]],
                   "dead_ids": [r["video_id"] for r in
                                report["dead_shorts"]]})
    applied = apply_directives(cfg, report) if apply else []
    log.info("Rapport hebdo ecrit: %s", file)
    return {"report": report, "applied": applied, "file": str(file)}
