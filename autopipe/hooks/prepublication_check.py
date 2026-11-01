# -*- coding: utf-8 -*-
"""Boucle pre-publication du skill hook-generator-upgrade (2026-11-01).

Lancement AUTOMATIQUE par le scheduler (etape 1.98), aucun geste humain
a T+72h. Un seul tour par run :

  1. construit la memoire de hooks (memory.build) ;
  2. liste les Shorts ages de >= 72h selon le registre ;
  3. interroge l'API Analytics (days=28) pour CES ids uniquement ;
  4. fusionne les lignes retenues dans hook_memory.json ;
  5. gate d'activation : le des des que l'API renvoie des VRAIS
     engagedViews (engaged_source == "engagedViews") pour au moins un
     Short age -> data/hooks/state.json {activated_at} + UN email Gmail
     (garde activation_email_sent, jamais deux fois).

En mode degrade (retention_latest.json absent, latence de report YouTube
48-72h, pas de Short >= 72h), le skill ne s'active pas : l'ideation A/B
historique continue a l'identique. C'est le contrat "auto_gated".
"""
import datetime as dt
import traceback

from ..alerts import send_gmail_alert
from ..config import get_config
from ..util import get_logger, load_json, save_json
from . import memory

log = get_logger("hooks.prepublication")

AGE_HOURS = 72


def state_path(cfg=None):
    return get_state_dir(cfg) / "state.json"


def get_state_dir(cfg=None):
    from ..config import get_paths
    return get_paths(cfg).hooks


def _opts(cfg):
    return (cfg or {}).get("hooks") or {}


def enabled(cfg=None):
    return bool(_opts(cfg or get_config()).get("enabled", True))


def is_active(cfg=None):
    """True si le skill est active (state.json contient activated_at).

    - [hooks].enabled=false ou mode "off" -> False ;
    - mode "always" -> True (bypass du gate, usage test/local) ;
    - mode "auto_gated" (defaut) -> gate sur activated_at.
    """
    cfg = cfg or get_config()
    if not enabled(cfg):
        return False
    mode = str(_opts(cfg).get("mode", "auto_gated"))
    if mode == "always":
        return True
    if mode == "off":
        return False
    try:
        state = load_json(state_path(cfg), {}) or {}
    except Exception:
        return False
    return bool(state.get("activated_at"))


def aged_ids(cfg=None, doc=None):
    """Video ids publies il y a >= 72h (published_at registre)."""
    cfg = cfg or get_config()
    doc = doc if doc is not None else memory.build(cfg)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=AGE_HOURS)
    out = []
    for e in memory._entries(doc):
        ts = memory.parse_ts(e.get("published_at"))
        if ts is not None and ts <= cutoff and e.get("video_id"):
            out.append(e["video_id"])
    return out


def _merge_rows(doc, rows):
    """Fusionne les lignes retention dans le doc memoire (sans ecraser)."""
    if not rows:
        return 0
    joined = 0
    for r in rows:
        vid = str(r.get("id") or "")
        entry = (doc.get("publisheds") or {}).get(vid)
        if not entry:
            continue
        joined += 1
        try:
            entry["views"] = int(r.get("views") or 0)
        except (TypeError, ValueError):
            entry["views"] = entry.get("views")
        try:
            entry["engagedViews"] = int(r.get("engagedViews") or 0)
        except (TypeError, ValueError):
            entry["engagedViews"] = entry.get("engagedViews")
        entry["engaged_ratio"] = r.get("engaged_ratio")
        entry["engaged_source"] = r.get("engaged_source")
        entry["avg_view_pct"] = r.get("avg_view_pct")
        try:
            from .. import analytics as _a
            entry["engagement"] = _a.engagement_value(r)
        except Exception:
            entry["engagement"] = None
    return joined


def _notify_activation(cfg, n_real):
    """Un seul email d'activation, garde par activation_email_sent."""
    try:
        state = load_json(state_path(cfg), {}) or {}
    except Exception:
        state = {}
    if state.get("activation_email_sent"):
        return
    subject = "[AutoPipe] Skill hook-generator-upgrade ACTIVE"
    body = (
        "Le skill hook-generator-upgrade vient de s'ACTIVER tout seul.\n"
        "\n"
        "Cause : l'API Analytics renvoie maintenant de VRAIS engagedViews\n"
        "(%s Short(s) ages de 72h+ mesures). Plus aucune action humaine\n"
        "n'est requise a T+72h : chaque publication suit desormais\n"
        "diversite 7 jours / famille gagnante / cycle exploration 1 sur %s.\n"
        "\n"
        "Si tu veux le couper : [hooks] enabled=false dans autopipe.toml.\n"
        % (n_real, _opts(cfg).get("explore_every", 5))
    )
    sent = send_gmail_alert(cfg, subject, body)
    if sent:
        state["activation_email_sent"] = True
        state["activation_email_ts"] = memory.now_iso()
        state.pop("activation_email_pending", None)
    else:
        # Pas configure (ou echec) : on retentera au prochain run.
        state["activation_email_pending"] = True
    try:
        save_json(state_path(cfg), state)
    except Exception:
        log.warning("[prepublication_check] state.json non sauvegarde "
                    "(non fatal)")


def run(cfg=None):
    """Point d'entree scheduler (etape 1.98). Jamais fatal."""
    cfg = cfg or get_config()
    if not enabled(cfg):
        log.info("[prepublication_check] desactive ([hooks].enabled=false)")
        return {"status": "disabled"}
    doc = memory.build(cfg)
    aged = aged_ids(cfg, doc)
    log.info("[prepublication_check] Shorts publies il y a 72h+ : %s",
             len(aged))
    rows = []
    if aged:
        try:
            from .. import analytics
            rows = analytics.retention_for(aged, cfg, days=28) or []
        except Exception:
            log.warning("[prepublication_check] retention_for a echoue "
                        "(non fatal):\n%s", traceback.format_exc())
    if rows:
        vals = ", ".join("%s=%s%%" % (r.get("id"), r.get("engaged_ratio"))
                         for r in rows)
        log.info("[prepublication_check] engagedViews recupere : %s", vals)
    else:
        log.info("[prepublication_check] engagedViews recupere : aucun "
                 "(latence YouTube 48-72h ou aucune video agee -> mode "
                 "degrade, l'ideation A/B continue telle quelle)")
    _merge_rows(doc, rows)
    doc["last_checked"] = memory.now_iso()
    entries = doc.get("publisheds") or {}
    try:
        memory.save(cfg, doc)
        log.info("[prepublication_check] hook_memory.json mis a jour : %s "
                 "entrees", len(entries))
    except Exception as exc:
        log.warning("[prepublication_check] sauvegarde memoire echouee "
                    "(non fatal): %s", exc)

    # Gate d'activation : >= 1 Short age avec VRAI engagedViews.
    try:
        state = load_json(state_path(cfg), {}) or {}
    except Exception:
        state = {}
    n_real = memory.n_with_engagement(cfg, doc)
    active = bool(state.get("activated_at"))
    if n_real > 0 and not active:
        state["activated_at"] = memory.now_iso()
        state["n_engaged_rows_first"] = n_real
        try:
            save_json(state_path(cfg), state)
            active = True
            log.info("[prepublication_check] ACTIVATION du skill "
                     "hook-generator-upgrade : %s Short(s) age(s) avec "
                     "engagedViews reel", n_real)
        except Exception as exc:
            log.warning("[prepublication_check] activation non persistee "
                        "(non fatal): %s", exc)
    if active:
        try:
            _notify_activation(cfg, n_real)
        except Exception:
            log.warning("[prepublication_check] email d'activation "
                        "impossible (non fatal):\n%s", traceback.format_exc())
    return {"status": "ok", "aged": len(aged), "rows": len(rows),
            "memory_entries": len(entries), "n_engaged": n_real,
            "active": active}
