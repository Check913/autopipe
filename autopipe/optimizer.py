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


def _hook_family(hook):
    """Taxonomie de hook commune avec identity.hook_family (V3).

    Repli historique sur les regexes locales si l'import echoue.
    """
    try:
        from .identity import hook_family
        return hook_family(hook)
    except Exception:
        h = str(hook or "")
        return ("question" if _QUESTION_RE.search(h.strip())
                else ("number" if _NUMBER_RE.search(h[:60])
                      else "statement"))


def _remake_files(cfg=None):
    """Fichiers remake JSON (ideas generees) tries du plus recent."""
    paths = get_paths(cfg)
    files = sorted(glob.glob(str(paths.remake / "*.json")))
    # FAILLE CORRIGEE 2026-10-29 : les idees sources vivent aussi dans
    # data/ideas/ (<idea_id>-<slug>.json). Sans elles, les shorts publies
    # sans remake correspondant etaient invisibles de la boucle
    # d'apprentissage. Les deux dossiers sont fusionnes, deduplies plus
    # bas dans _join_samples par idea id.
    try:
        files += sorted(glob.glob(str(paths.ideas / "*.json")))
    except Exception:
        pass
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
    # Bug 2026-10-27 : les vrais remakes ont density = LISTE de buckets
    # (matrice retention s/s de remake.py) - float(list) levait et tuait
    # TOUT _join_samples (optimizer degrade en silence).
    _density = ret.get("density")
    if isinstance(_density, (list, tuple)):
        _vals = [float(x) for x in _density if isinstance(x, (int, float))]
        _density = (sum(_vals) / len(_vals)) if _vals else 0.0
    try:
        _density = float(_density or 0.0)
    except (TypeError, ValueError):
        _density = 0.0
    return {
        "id": data.get("id") or Path(path).stem,
        "title": title,
        # MAX : texte brut du hook, requis pour le few-shot prompt (lecons
        # TOP/WORST citees verbatim dans les prompts LLM).
        "hook": hook,
        "title_len": len(title),
        # V3 : meme taxonomie que identity.hook_family (question/number/
        # negation/statement) - les regexes locales ne distinguaient pas la
        # negation et desapprenaient des patterns incohérents avec le A/B.
        "hook_type": _hook_family(hook),
        # V2 : format de duree et variante A/B de hook -> dimensions
        # apprenables par extract_patterns (le bot sait desormais SI le
        # format 13 s bat le 60 s, et SI la variante A ou B retient mieux).
        "duration_format": str(data.get("duration_format") or ""),
        "hook_variant": str(data.get("hook_variant") or ""),
        "hook_len": len(hook),
        "title_has_number": bool(_NUMBER_RE.search(title)),
        "duration_target": float(data.get("duration_target") or 0.0),
        "score": float(data.get("score") or 0.0),
        "originality": float((data.get("originality") or {}).get("max", 0.0)
                             if isinstance(data.get("originality"), dict)
                             else data.get("originality") or 0.0),
        "retention_density": _density,
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
    # Pont youtube_id -> idea_id (registre permanent). En echec, on
    # retombe sur la jointure legacy par youtube id (au pire 0 jointure,
    # jamais d'exception levee).
    idea_by_video = {}
    try:
        from .registry import map_video_to_idea
        idea_by_video = map_video_to_idea(cfg) or {}
    except Exception as exc:
        log.warning("registre indisponible pour la jointure: %s", exc)
    by_id = {}
    for r in rows:
        vid = str(r.get("id") or "")
        if not vid:
            continue
        by_id[vid] = r
        iid = str(idea_by_video.get(vid, vid))
        if iid:
            by_id[iid] = r  # idea id ET youtube id -> meme ligne de stats
    # Retention (proxy Viewed vs Swiped Away, Analytics API scope separe).
    ret = {}
    try:
        rdoc = load_json(paths.analytics / "retention_latest.json") or {}
        ret = {str(r.get("id")): r for r in (rdoc.get("rows") or [])}
    except Exception:
        ret = {}
    samples = []
    # Une meme idee peut exister en data/remake ET data/ideas -> dedup
    # par idea id (priorite au data/remake = version finale produite).
    seen_ids = set()
    for path in _remake_files(cfg):
        try:
            feats = _features_from_remake(path)
        except Exception as exc:
            # Un remake corrompu ne doit pas tuer toute la boucle.
            log.warning("remake ignore (%s): %s", path, exc)
            continue
        fid = str(feats["id"])
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        row = by_id.get(fid)
        if row:
            feats["engagement"] = _engagement_index(row)
            feats["views"] = int(row.get("views") or 0)
            feats["published"] = str(row.get("published") or "")
            rrow = ret.get(fid)
            if rrow:
                feats["avg_view_pct"] = float(rrow.get("avg_view_pct") or 0.0)
                # V2 : vrai ratio engagedViews/views (engage_ratio) si
                # disponible (sinon fallback averageViewPercentage).
                from .analytics import engagement_value
                feats["engaged_ratio"] = engagement_value(rrow)
            samples.append(feats)
    return samples


# ---------------------------------------------------------------------------
# MAX (2026-10-29) : lecons persistantes + few-shot prompt.
# data/optimizer/lessons.jsonl = memoire d'apprentissage brute,
# dedupliqúee par idea id, survivant a la purge des snapshots analytics.
# few_shot_directive en tire des exemples reels TOP/WORST injectes dans
# les prompts LLM via apply_config.
# ---------------------------------------------------------------------------

def _lessons_file(cfg):
    """Chemin du journal d'apprentissage persistant."""
    return _optimizer_file(cfg, "lessons.jsonl")


def _append_lessons(cfg, samples):
    """Upsert des lecons d'apprentissage (les stats les plus recentes

    gagnent). Deduplique par idea id : une re-analyse avec plus de vues
    ecrase l'ancienne ligne au lieu de dupliquer. Renvoie le nb de lecons.
    """
    path = _lessons_file(cfg)
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    by_id = {str(r.get("id")): r for r in rows if r.get("id")}
    now = dt.datetime.now().isoformat(timespec="seconds")
    for s in samples or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        by_id[str(s["id"])] = {
            "id": str(s["id"]),
            "title": str(s.get("title") or ""),
            "hook": str(s.get("hook") or ""),
            "hook_type": str(s.get("hook_type") or ""),
            "duration_format": str(s.get("duration_format") or ""),
            "hook_variant": str(s.get("hook_variant") or ""),
            "engagement": s.get("engagement"),
            "avg_view_pct": s.get("avg_view_pct"),
            "views": s.get("views"),
            "published": str(s.get("published") or ""),
            "updated": now,
        }
    doc = [by_id[k] for k in sorted(by_id)]
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in doc)
    path.write_text((text + "\n") if text else "", encoding="utf-8")
    return len(doc)


def _load_lessons(cfg):
    """Lecons chargees (lignes corrompues ignorees, jamais d'exception)."""
    path = _lessons_file(cfg)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
    except Exception:
        return []
    return out


def few_shot_directive(cfg=None, k=2):
    """Directive few-shot : lecons reelles TOP k vs WORST k.

    Ne rank que les videos avec engagement numerique ET assez de vues
    (optimizer.few_shot_min_views, defaut 100) pour qu'une video publiee
    la veille a 0 vue ne soit pas elue "pire performer" par artefact.
    Retourne "" tant qu'il n'y a pas au moins 2 lecons mesurables.
    """
    cfg = cfg or get_config()
    oc = cfg.get("optimizer", {})
    try:
        min_views = int(oc.get("few_shot_min_views", 100))
    except (TypeError, ValueError):
        min_views = 100
    lessons = []
    for r in _load_lessons(cfg):
        eng = r.get("engagement")
        views = r.get("views")
        if not isinstance(eng, (int, float)):
            continue
        if not isinstance(views, (int, float)) or float(views) < min_views:
            continue
        lessons.append(r)
    if len(lessons) < 2:
        return ""
    ranked = sorted(lessons, key=lambda r: -float(r["engagement"]))
    top = ranked[:k]
    worst = list(reversed(ranked[-k:]))

    def _clean(text):
        return " ".join(str(text or "").replace('"', " ").split())

    parts = []
    for r in top:
        parts.append(
            'TOP performer: "%s" (%s hook, %s views, engagement %.0f/100) '
            "- IMITATE its structure exactly: same hook style, same pacing."
            % (_clean(r.get("title")), _clean(r.get("hook_type")) or "n/a",
               int(r.get("views") or 0), float(r["engagement"])))
    for r in worst:
        parts.append(
            'WORST performer: "%s" (%s hook, %s views, engagement %.0f/100) '
            "- do NOT copy its pattern."
            % (_clean(r.get("title")), _clean(r.get("hook_type")) or "n/a",
               int(r.get("views") or 0), float(r["engagement"])))
    return " ".join(parts)


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
    # V2 : duration_format (13 s vs 60 s) et hook_variant (A/B) sont des
    # dimensions apprenables a part entiere.
    for field, label in (("hook_type", "hook type"), ("title_has_number",
                                                      "chiffre dans le titre"),
                         ("duration_format", "format duree"),
                         ("hook_variant", "hook A/B")):
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
    # V2 A/B hooks : si une famille de hook domine clairement (>= 3 mesures,
    # ecart >= 5 pts), le lot suivant l'utilise comme directive de style.
    try:
        from .hook_ab import best_family
        ab_family = best_family(cfg)
    except Exception:
        ab_family = None
    ab_directive = ""
    if ab_family:
        ab_directive = (
            "Learned hook A/B: family '%s' retains best - open EVERY script "
            "with a %s-style hook (question = end with ?, number = leading "
            "digit, negation = stop/never/nobody, statement = blunt claim)."
            % (ab_family, ab_family))

    # V3 : directives FORCEES au-dessus des patterns statistiques -
    # d'abord les patterns actifs (teardown des succes, wording FORCE),
    # puis le kill switch (familles bannies, pause de niche).
    prefix_parts = [retention_directive, ab_directive]
    try:
        prefix_parts.append(active_patterns_directive(cfg) or "")
    except Exception:
        pass
    try:
        prefix_parts.append(kill_switch_directive(cfg) or "")
    except Exception:
        pass
    # MAX : few-shot reel (lecons TOP/WORST verbatim) - s'applique des
    # qu'il y a 2 shorts mesurables, independamment des patterns stats.
    try:
        prefix_parts.append(few_shot_directive(cfg) or "")
    except Exception:
        pass
    prefix = " | ".join(p for p in prefix_parts if p)

    # V3 baseline : aucun ajustement NUMERIQUE tant que la baseline des
    # `baseline_size` premiers Shorts publies n'est pas verrouillee
    # ("sans baseline, tu optimises a l'aveugle"). Les directives
    # textuelles restent actives (elles ne touchent aucun parametre).
    baseline_locked = True
    bdoc = {}
    try:
        bdoc = load_json(_optimizer_file(cfg, "baseline.json")) or {}
        baseline_locked = bool(bdoc.get("locked"))
    except Exception:
        baseline_locked = True

    if patterns_doc is None:
        paths = get_paths(cfg)
        file = oc.get("patterns_file") or ""
        src = Path(file) if file else paths.optimizer / "patterns.json"
        patterns_doc = load_json(src)
    if not patterns_doc or not patterns_doc.get("patterns"):
        if prefix:
            cfg.setdefault("ideas", {})["style_directive"] = prefix
            cfg.setdefault("remake", {})["style_directive"] = prefix
            applied.append("style_directive (directives V3)")
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

    if not baseline_locked:
        applied.append("baseline non verrouillee (%d/%d Shorts publies) - "
                       "ajustements numeriques suspendus"
                       % (int(bdoc.get("n") or 0),
                          int(oc.get("baseline_size", 10))))
    for p in (patterns_doc["patterns"] if baseline_locked else []):
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
        if prefix:
            directive = prefix + " | " + directive
        cfg.setdefault("ideas", {})["style_directive"] = directive
        # Meme directive pour les prompts Super-Remake (_rewrite_user_prompt).
        cfg.setdefault("remake", {})["style_directive"] = directive
        applied.append("style_directive (ideas+remake): %d patterns" % len(labels))
    elif prefix:
        cfg.setdefault("ideas", {})["style_directive"] = prefix
        cfg.setdefault("remake", {})["style_directive"] = prefix
        applied.append("style_directive (directives V3)")
    return applied


# ---------------------------------------------------------------------------
# V3 : kill switch, baseline, patterns actifs
# ---------------------------------------------------------------------------

_ACTIVE_DIMS = ("hook_type", "duration_format", "hook_variant",
                "title_has_number")


def _optimizer_file(cfg, name):
    """Chemin d'un fichier d'etat V3 dans data/optimizer/."""
    return get_paths(cfg).optimizer / name


def _norm_topic(topic):
    """Cle de niche normalisee (minuscules, espaces colapses)."""
    return " ".join(str(topic or "").lower().split())


def _chronological(samples):
    """Echantillons du plus ancien au plus recent (_remake_files est
    triee du plus recent au plus ancien)."""
    return list(reversed(list(samples or [])))


def _new_samples(samples, seen_ids):
    """Echantillons jamais analyses (ordre chronologique) + ensemble
    complet des ids publies."""
    chrono = _chronological(samples)
    seen = set(str(i) for i in (seen_ids or ()))
    new = [s for s in chrono if str(s.get("id")) not in seen]
    return new, {str(s.get("id")) for s in chrono}


def update_kill_switch(cfg=None, samples=None):
    """Kill switch V3 (data/optimizer/killswitch.json).

    Regles (critique : une famille morte ne doit PAS se propager) :
      - 3 Shorts publies consecutifs d'une meme famille de hook <
        kill_family_pct (30 %) -> famille BANIE pour les
        kill_ban_shorts (20) Shorts publies suivants ;
      - 5 Shorts consecutifs de la meme niche < kill_niche_pct (40 %)
        -> alerte Gmail + directive changement de niche pendant
        kill_niche_shorts (10) Shorts.
    Le decompte decremente par NOUVEAU Short publie analyse (le lag
    publication -> analyse est acceptable). Jamais d'exception.
    """
    try:
        cfg = cfg or get_config()
        oc = cfg.get("optimizer", {})
        if not oc.get("kill_switch", True):
            return None
        fail_fam = float(oc.get("kill_family_pct", 30.0))
        fail_niche = float(oc.get("kill_niche_pct", 40.0))
        ban_len = int(oc.get("kill_ban_shorts", 20))
        niche_len = int(oc.get("kill_niche_shorts", 10))
        file = _optimizer_file(cfg, "killswitch.json")
        state = load_json(file) or {}
        fam_fails = state.setdefault("family_fails", {})
        banned = state.setdefault("banned_families", {})
        niche = state.setdefault("niche", {})
        niche.setdefault("streak", 0)
        niche.setdefault("topic", "")
        niche.setdefault("alert", False)
        niche.setdefault("escape_remaining", 0)
        state["thresholds"] = {"family_pct": fail_fam, "niche_pct": fail_niche}
        events = list(state.get("events") or [])
        new, all_ids = _new_samples(samples, state.get("seen_ids"))
        for s in new:
            ret = s.get("avg_view_pct")
            if ret is None:
                continue  # pas de donnee retention -> pas de verdict
            ret = float(ret)
            pre_banned = set(banned)
            alert_before = bool(niche.get("alert"))
            # Familles de hook : 3 < fail_fam consecutifs -> ban
            fam = str(s.get("hook_type") or "statement")
            ff = fam_fails.setdefault(fam, {"streak": 0, "ids": []})
            if ret < fail_fam:
                ff["streak"] = int(ff.get("streak", 0)) + 1
                ff.setdefault("ids", []).append(str(s.get("id")))
                if ff["streak"] >= 3 and fam not in banned:
                    banned[fam] = {
                        "remaining": ban_len,
                        "banned_at": dt.datetime.now().isoformat(
                            timespec="seconds"),
                        "last_ids": list(ff["ids"])[-3:],
                    }
                    ff["streak"] = 0
                    ff["ids"] = []
                    events.append("FAMILY BAN %s (%d Shorts < %s%%)"
                                  % (fam, ban_len, fail_fam))
                    log.warning("KILL SWITCH: famille '%s' bannie %d Shorts "
                                "(3 Shorts consecutifs < %s%%)",
                                fam, ban_len, fail_fam)
            else:
                ff["streak"] = 0
                ff["ids"] = []
            # Niche : 5 < fail_niche consecutifs sur le meme sujet
            topic = _norm_topic(s.get("source_topic"))
            if topic and topic != niche.get("topic"):
                niche["streak"] = 0
                niche["topic"] = topic
            if topic:
                if ret < fail_niche:
                    niche["streak"] = int(niche.get("streak", 0)) + 1
                    if niche["streak"] >= 5 and not niche.get("alert"):
                        niche["alert"] = True
                        niche["escape_remaining"] = niche_len
                        events.append("NICHE ALERT %s (%d Shorts < %s%%)"
                                      % (topic, niche_len, fail_niche))
                        log.warning("KILL SWITCH: niche '%s' : %d Shorts "
                                    "consecutifs < %s%% - changement de "
                                    "niche requis", topic, niche_len,
                                    fail_niche)
                        try:
                            from .alerts import send_gmail_alert
                            send_gmail_alert(
                                cfg,
                                "AutoPipe kill switch - niche en echec",
                                "5 Shorts consecutifs de la niche '%s' sont "
                                "sous %s%% de retention. Change de niche "
                                "pour les %d prochains Shorts."
                                % (topic, fail_niche, niche_len))
                        except Exception:
                            pass  # alerte best-effort
                else:
                    niche["streak"] = 0
                    if niche.get("alert"):
                        niche["alert"] = False
                        niche["escape_remaining"] = 0
                        events.append("NICHE RECOVERY %s" % topic)
            # Decompte des bans/pauses (par Short publie analyse ; les
            # declencheurs du jour ne comptent pas contre leur propre ban)
            for fam_b in list(pre_banned):
                if fam_b in banned:
                    banned[fam_b]["remaining"] = (int(banned[fam_b].get(
                        "remaining", 0)) - 1)
                    if banned[fam_b]["remaining"] <= 0:
                        del banned[fam_b]
                        events.append("FAMILY UNBAN %s" % fam_b)
            if alert_before and niche.get("escape_remaining"):
                niche["escape_remaining"] = (int(niche["escape_remaining"])
                                             - 1)
                if niche["escape_remaining"] <= 0:
                    niche["alert"] = False
                    niche["streak"] = 0
                    events.append("NICHE ESCAPE END")
        state["seen_ids"] = sorted(all_ids)[-400:]
        state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        state["events"] = events[-50:]
        save_json(file, state)
        return state
    except Exception as exc:
        log.warning("kill switch degrade: %s", exc)
        return None


def kill_switch_directive(cfg=None):
    """Directive du kill switch (None si rien d'actif).

    Injectee dans style_directive par apply_config : familles bannies
    interdites d'ouverture, pause de niche imposee.
    """
    try:
        state = load_json(_optimizer_file(cfg, "killswitch.json")) or {}
    except Exception:
        return None
    parts = []
    banned = state.get("banned_families") or {}
    if banned:
        items = ", ".join(
            "%s (%d Shorts restants)" % (k, int(v.get("remaining", 0)))
            for k, v in sorted(banned.items()))
        parts.append("KILL SWITCH: hook families BANNED - do NOT open any "
                     "script with: %s. Use another hook family." % items)
    niche = state.get("niche") or {}
    if niche.get("alert"):
        thr = (state.get("thresholds") or {}).get("niche_pct", 40.0)
        parts.append("KILL SWITCH: niche '%s' failed %d consecutive Shorts "
                     "(< %s%% retention) - STOP this topic and switch to a "
                     "different niche topic for the next %d Shorts."
                     % (niche.get("topic"), int(niche.get("streak", 0)),
                        thr, int(niche.get("escape_remaining", 0))))
    return " | ".join(parts) if parts else None


def _baseline_metrics(samples):
    """Moyennes d'engagement/retention d'une liste d'echantillons."""
    eng = [float(s["engagement"]) for s in samples
           if s.get("engagement") is not None]
    ret = [float(s["avg_view_pct"]) for s in samples
           if s.get("avg_view_pct") is not None]
    by_family = {}
    for s in samples:
        r = s.get("avg_view_pct")
        if r is None:
            continue
        fam = str(s.get("hook_type") or "statement")
        by_family.setdefault(fam, []).append(float(r))
    return {
        "n": len(samples),
        "engagement_mean": round(sum(eng) / len(eng), 2) if eng else None,
        "avg_view_pct_mean": round(sum(ret) / len(ret), 2) if ret else None,
        "by_family": {k: round(sum(v) / len(v), 2)
                      for k, v in sorted(by_family.items())},
    }


def update_baseline(cfg=None, samples=None):
    """Baseline V3 (data/optimizer/baseline.json).

    Verrouille les `baseline_size` (defaut 10) premiers Shorts PUBLIES :
    engagement et retention moyens, par famille de hook. Tant que
    `locked` est False, apply_config n'ajuste AUCUN parametre numerique
    ("sans baseline, tu optimises a l'aveugle"). Idempotent : un Short
    deja baseline n'y revient jamais.
    """
    try:
        cfg = cfg or get_config()
        oc = cfg.get("optimizer", {})
        target = max(1, int(oc.get("baseline_size", 10)))
        file = _optimizer_file(cfg, "baseline.json")
        doc = load_json(file) or {}
        doc.setdefault("locked", False)
        doc["target"] = target
        have = {str(s.get("id")) for s in (doc.get("samples") or [])}
        for s in _chronological(samples):
            sid = str(s.get("id"))
            if sid in have or s.get("engagement") is None:
                continue
            doc.setdefault("samples", []).append({
                "id": sid,
                "engagement": s.get("engagement"),
                "avg_view_pct": s.get("avg_view_pct"),
                "hook_type": s.get("hook_type"),
            })
            have.add(sid)
        doc["samples"] = (doc.get("samples") or [])[:target]
        doc["n"] = len(doc["samples"])
        if not doc.get("locked") and doc["n"] >= target:
            doc["locked"] = True
            doc["locked_at"] = dt.datetime.now().isoformat(timespec="seconds")
            doc["metrics"] = _baseline_metrics(doc["samples"])
            m = doc["metrics"]
            log.info("Baseline verrouillee (%d Shorts): engagement moyen %s, "
                     "retention moyenne %s%%", doc["n"],
                     m.get("engagement_mean"), m.get("avg_view_pct_mean"))
        doc["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        save_json(file, doc)
        return doc
    except Exception as exc:
        log.warning("baseline degradee: %s", exc)
        return None


def baseline_delta(cfg=None, samples=None):
    """Comparaison des `baseline_size` Shorts SUIVANTS vs la baseline.

    Renvoie None tant que la baseline n'est pas verrouillee, sinon
    {status, n_after, baseline, current, delta}. Les Shorts post-baseline
    sont les premiers publies dont l'id n'est PAS dans la baseline.
    """
    try:
        doc = load_json(_optimizer_file(cfg, "baseline.json")) or {}
        if not doc.get("locked"):
            return None
        target = int(doc.get("target") or 10)
        base_ids = {str(s.get("id")) for s in (doc.get("samples") or [])}
        after = [s for s in _chronological(samples)
                 if str(s.get("id")) not in base_ids
                 and s.get("engagement") is not None][:target]
        if not after:
            return {"status": "pending", "n_after": 0, "target": target}
        base = doc.get("metrics") or {}
        cur = _baseline_metrics(after)
        delta = {}
        for key in ("engagement_mean", "avg_view_pct_mean"):
            b, c = base.get(key), cur.get(key)
            delta[key] = (round(c - b, 2)
                          if isinstance(b, (int, float))
                          and isinstance(c, (int, float)) else None)
        return {"status": "ok", "n_after": len(after), "target": target,
                "baseline": base, "current": cur, "delta": delta}
    except Exception as exc:
        log.warning("baseline delta degrade: %s", exc)
        return None


def update_active_patterns(cfg=None, samples=None):
    """Patterns ACTIFS V3 (data/optimizer/active_patterns.json).

    Teardown des succes : un pattern (valeur d'une dimension de
    _ACTIVE_DIMS) dont la retention moyenne depasse active_min_pct (70 %)
    sur >= active_min_n (3) Shorts est FORCE dans les active_shorts (5)
    Shorts suivants via active_patterns_directive. Decompte par nouveau
    Short publie analyse ; re-armement seulement si de nouveaux Shorts du
    groupe sont arrives (sinon le pattern se re-force en boucle).
    """
    try:
        cfg = cfg or get_config()
        oc = cfg.get("optimizer", {})
        min_pct = float(oc.get("active_min_pct", 70.0))
        min_n = int(oc.get("active_min_n", 3))
        duration = max(1, int(oc.get("active_shorts", 5)))
        file = _optimizer_file(cfg, "active_patterns.json")
        doc = load_json(file) or {}
        active = {str(a.get("key")): a for a in (doc.get("active") or [])}
        samples = list(samples or [])
        new, all_ids = _new_samples(samples, doc.get("seen_ids"))
        # Statistiques groupees sur TOUT l'historique publie.
        groups = {}
        for s in samples:
            r = s.get("avg_view_pct")
            if r is None:
                continue
            for dim in _ACTIVE_DIMS:
                key = "%s:%s" % (dim, str(s.get(dim)))
                groups.setdefault(key, []).append(float(r))
        entries = []
        for key, vals in sorted(groups.items()):
            n, mean = len(vals), sum(vals) / len(vals)
            if n < min_n or mean <= min_pct:
                continue
            dim, value = key.split(":", 1)
            prev = active.get(key)
            if prev:
                remaining = int(prev.get("remaining", 0)) - len(new)
                if remaining <= 0:
                    if n <= int(prev.get("group_n", 0)):
                        continue  # rien de neuf -> pas de re-armement
                    remaining = duration
            else:
                remaining = duration
            entry = {"key": key, "dimension": dim, "value": value,
                     "n": n, "mean_avg_view_pct": round(mean, 1),
                     "remaining": min(remaining, duration), "group_n": n}
            entries.append(entry)
            if prev is None or int(prev.get("remaining", 0)) <= 0:
                log.info("Pattern ACTIF: %s (retention moyenne %.0f%% sur "
                         "%d Shorts) force dans les %d prochains",
                         key, mean, n, entry["remaining"])
        doc["active"] = entries
        doc["seen_ids"] = sorted(all_ids)[-400:]
        doc["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        save_json(file, doc)
        return doc
    except Exception as exc:
        log.warning("patterns actifs degrades: %s", exc)
        return None


def active_patterns_directive(cfg=None):
    """Directive des patterns actifs (None si aucun actif).

    Wording FORCE : ces patterns ont PROUVE (> active_min_pct sur
    active_min_n Shorts), ils s'imposent dans le prompt des prochains
    Shorts, au-dessus des ajustements statistiques.
    """
    try:
        doc = load_json(_optimizer_file(cfg, "active_patterns.json")) or {}
    except Exception:
        return None
    parts = []
    for a in doc.get("active") or []:
        if int(a.get("remaining", 0)) <= 0:
            continue
        parts.append("FORCE pattern %s=%s: %.0f%% avg retention over %d "
                     "Shorts - replicate this pattern in EVERY script "
                     "(%d Shorts remaining)."
                     % (a.get("dimension"), a.get("value"),
                        float(a.get("mean_avg_view_pct") or 0.0),
                        int(a.get("n") or 0), int(a.get("remaining", 0))))
    return " | ".join(parts) if parts else None


def run_optimizer(cfg=None, samples=None):
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
        samples = samples if samples is not None else _join_samples(cfg)
        doc = extract_patterns(samples)
        out = {
            "fetched": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "learned" if len(samples) >= min_samples else "pending",
            "min_samples": min_samples,
            "n_samples": len(samples),
            **doc,
        }
        # MAX : lecons persistantes (surviennent aux purges de snapshots)
        # + nb expose dans patterns.json pour suivi.
        try:
            out["lessons"] = _append_lessons(cfg, samples)
        except Exception as exc:
            log.warning("lessons degradees: %s", exc)
        # V3 : etats persistants (kill switch, baseline, patterns actifs).
        # Chacun est no-fail et renvoie None en cas de souci ; ils restent
        # alimentes meme en 'pending' (des le 1er Short publie).
        try:
            ks = update_kill_switch(cfg, samples)
            if ks:
                out["kill_switch"] = {
                    "banned_families": {k: int(v.get("remaining", 0))
                                        for k, v in
                                        (ks.get("banned_families") or
                                         {}).items()},
                    "niche_alert": bool((ks.get("niche") or {}).get("alert")),
                    "niche_topic": (ks.get("niche") or {}).get("topic") or "",
                }
        except Exception as exc:
            log.warning("kill switch degrade: %s", exc)
        try:
            bl = update_baseline(cfg, samples)
            if bl is not None:
                out["baseline"] = {"locked": bool(bl.get("locked")),
                                   "n": int(bl.get("n") or 0),
                                   "target": int(bl.get("target") or 0)}
        except Exception as exc:
            log.warning("baseline degradee: %s", exc)
        try:
            delta = baseline_delta(cfg, samples)
            if delta:
                out["baseline_delta"] = delta
        except Exception as exc:
            log.warning("baseline delta degrade: %s", exc)
        try:
            ap = update_active_patterns(cfg, samples)
            if ap is not None:
                out["active_patterns"] = len(ap.get("active") or [])
        except Exception as exc:
            log.warning("patterns actifs degrades: %s", exc)
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
