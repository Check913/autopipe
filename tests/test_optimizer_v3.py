"""Tests V3 (2026-10-27) : kill switch, baseline 10 Shorts, patterns actifs.

Echantillons passes dans l'ordre _remake_files (du plus RECENT au plus
ancien) : _chronological les remet du plus ancien au plus recent.
"""
import sys

sys.path.insert(0, ".")

from autopipe import optimizer  # noqa: E402


def _sample(sid, ret=None, fam="question", topic="deep sea", eng=10.0):
    return {
        "id": sid,
        "hook_type": fam,
        "duration_format": "short",
        "source_topic": topic,
        "engagement": eng,
        "avg_view_pct": ret,
        "engaged_ratio": None,
    }


# ---------------------------------------------------------------------------
# Kill switch : famille
# ---------------------------------------------------------------------------

def test_kill_switch_ban_apres_3_fails_consecutifs(make_cfg):
    """3 Shorts consecutifs < 30 % dans la meme famille -> ban 20 Shorts."""
    cfg = make_cfg()
    samples = [
        _sample("v4", 20.0, fam="negation"),   # plus recent
        _sample("v3", 25.0, fam="negation"),
        _sample("v2", 10.0, fam="negation"),   # 3e fail consecutif
        _sample("v1", 80.0, fam="question"),
    ]
    state = optimizer.update_kill_switch(cfg, samples)
    assert state is not None
    assert "negation" in state["banned_families"]
    assert int(state["banned_families"]["negation"]["remaining"]) == 20
    directive = optimizer.kill_switch_directive(cfg)
    assert directive and "negation" in directive and "BANNED" in directive


def test_kill_switch_pas_de_ban_si_reprise(make_cfg):
    """Un Short >= 30 % casse la serie : 2 fails puis succes -> pas de ban."""
    cfg = make_cfg()
    samples = [
        _sample("v4", 20.0, fam="negation"),
        _sample("v3", 60.0, fam="negation"),  # reset
        _sample("v2", 10.0, fam="negation"),
        _sample("v1", 15.0, fam="negation"),
        _sample("v0", 90.0, fam="question"),
    ]
    state = optimizer.update_kill_switch(cfg, samples)
    assert state["banned_families"] == {}


def test_kill_switch_ban_decremente_et_leve(make_cfg):
    """Le decompte decremente par NOUVEAU Short publie analyse."""
    cfg = make_cfg()
    samples = [_sample("v%d" % i, 20.0, fam="negation") for i in range(3)]
    state = optimizer.update_kill_switch(cfg, samples)
    assert int(state["banned_families"]["negation"]["remaining"]) == 20
    # Nouveau Short publie (famille saine) -> -1
    state = optimizer.update_kill_switch(cfg, samples + [_sample("v9", 80.0)])
    assert int(state["banned_families"]["negation"]["remaining"]) == 19
    # Rien de nouveau -> pas de decrement supplementaire
    state = optimizer.update_kill_switch(cfg, samples + [_sample("v9", 80.0)])
    assert int(state["banned_families"]["negation"]["remaining"]) == 19


def test_kill_switch_famille_bannie_interdite_de_hook(make_cfg):
    """apply_config doit propager le ban dans style_directive."""
    cfg = make_cfg()
    cfg.setdefault("optimizer", {})["enabled"] = True
    samples = [_sample("v%d" % i, 20.0, fam="negation") for i in range(3)]
    optimizer.update_kill_switch(cfg, samples)
    applied = optimizer.apply_config(cfg)
    sd = cfg["ideas"]["style_directive"]
    assert applied
    assert "negation" in sd and "BANNED" in sd


# ---------------------------------------------------------------------------
# Kill switch : niche
# ---------------------------------------------------------------------------

def test_kill_switch_niche_alerte_apres_5_fails(make_cfg, monkeypatch):
    """5 Shorts consecutifs < 40 % meme niche -> alerte + directive."""
    sent = {}

    def fake_alert(c, subject, body):
        sent["subject"] = subject

    import autopipe.alerts as alerts
    monkeypatch.setattr(alerts, "send_gmail_alert", fake_alert)
    cfg = make_cfg()
    samples = [
        _sample("v4", 35.0, topic="deep sea"),
        _sample("v3", 20.0, topic="deep sea"),
        _sample("v2", 15.0, topic="deep sea"),
        _sample("v1", 30.0, topic="deep sea"),
        _sample("v0", 25.0, topic="deep sea"),  # 5e fail consecutif
    ]
    state = optimizer.update_kill_switch(cfg, samples)
    assert state["niche"]["alert"] is True
    assert state["niche"]["topic"] == "deep sea"
    assert int(state["niche"]["escape_remaining"]) == 10
    assert sent["subject"].startswith("AutoPipe kill switch")
    directive = optimizer.kill_switch_directive(cfg)
    assert directive and "deep sea" in directive and "switch" in directive.lower()


def test_kill_switch_niche_topics_differents_pas_d_alerte(make_cfg):
    """5 fails mais sur des niches differentes -> pas d'alerte."""
    cfg = make_cfg()
    samples = [_sample("v%d" % i, 20.0, topic="topic%d" % i)
               for i in range(5)]
    state = optimizer.update_kill_switch(cfg, samples)
    assert state["niche"]["alert"] is False


def test_kill_switch_niche_escape_estompe(make_cfg):
    """La pause de niche se termine apres kill_niche_shorts nouveaux Shorts."""
    cfg = make_cfg()
    samples = [_sample("v%d" % i, 20.0, fam="negation", topic="deep sea")
               for i in range(5)]
    state = optimizer.update_kill_switch(cfg, samples)
    assert state["niche"]["alert"] is True
    # 10 nouveaux Shorts sains -> escape_remaining tombe a 0
    for i in range(10):
        samples = [_sample("w%d" % i, 90.0, fam="statement", topic="deep sea")] \
            + samples
        state = optimizer.update_kill_switch(cfg, samples)
    assert state["niche"]["alert"] is False
    directive = optimizer.kill_switch_directive(cfg) or ""
    assert "different niche" not in directive  # pause niche terminee


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_baseline_verrouillee_a_10_shorts(make_cfg):
    """La baseline se verrouille aux 10 premiers Shorts publies."""
    cfg = make_cfg()
    samples = [_sample("v%02d" % i, 50.0 + i) for i in range(10)][::-1]
    doc = optimizer.update_baseline(cfg, samples)
    assert doc["locked"] is True
    assert doc["n"] == 10
    assert doc["metrics"]["n"] == 10
    # Idempotent : un Short deja baseline n'y revient jamais
    doc = optimizer.update_baseline(cfg, samples + [_sample("v99", 90.0)])
    assert doc["n"] == 10


def test_baseline_pas_verrouillee_avant_10(make_cfg):
    cfg = make_cfg()
    samples = [_sample("v%02d" % i, 50.0) for i in range(9)][::-1]
    doc = optimizer.update_baseline(cfg, samples)
    assert doc["locked"] is False


def test_apply_config_sans_baseline_pas_d_ajustement_numerique(make_cfg):
    """Avant verrouillage : AUCUN parametre numerique touche."""
    cfg = make_cfg()
    cfg.setdefault("video", {})["hook_duration"] = 3.0
    cfg.setdefault("llm", {})["temperature"] = 0.9
    patterns_doc = {
        "patterns": [{"dimension": "hook_type", "value": "question",
                      "relative_delta": 0.5, "label": "hooks question"}],
    }
    applied = optimizer.apply_config(cfg, patterns_doc=patterns_doc)
    assert cfg["video"]["hook_duration"] == 3.0  # intact
    assert cfg["llm"]["temperature"] == 0.9  # intact
    assert any("baseline" in a.lower() for a in applied)


def test_apply_config_avec_baseline_ajuste(make_cfg):
    """Apres verrouillage : les ajustements numeriques reprennent."""
    cfg = make_cfg()
    cfg.setdefault("video", {})["hook_duration"] = 3.0
    samples = [_sample("v%02d" % i, 50.0 + i) for i in range(10)][::-1]
    optimizer.update_baseline(cfg, samples)
    patterns_doc = {
        "patterns": [{"dimension": "hook_type", "value": "question",
                      "relative_delta": 0.5, "label": "hooks question"}],
    }
    applied = optimizer.apply_config(cfg, patterns_doc=patterns_doc)
    assert cfg["video"]["hook_duration"] != 3.0
    assert any("hook_duration" in a for a in applied)


def test_baseline_delta_compare_10_suivants(make_cfg):
    """baseline_delta compare les `baseline_size` Shorts SUIVANTS."""
    cfg = make_cfg()
    base = [_sample("v%02d" % i, 50.0) for i in range(10)]
    assert optimizer.baseline_delta(cfg, base) is None  # pas verrouillee
    optimizer.update_baseline(cfg, base[::-1])
    after = [_sample("w%02d" % i, 70.0) for i in range(10)]
    delta = optimizer.baseline_delta(cfg, base[::-1] + after)
    assert delta["status"] == "ok"
    assert delta["n_after"] == 10
    assert delta["delta"]["avg_view_pct_mean"] == 20.0
    assert delta["delta"]["engagement_mean"] == 0.0


# ---------------------------------------------------------------------------
# Patterns actifs (teardown des succes)
# ---------------------------------------------------------------------------

def test_active_patterns_force_70pct_sur_3(make_cfg):
    """Un pattern > 70 % sur >= 3 Shorts est force dans les 5 suivants."""
    cfg = make_cfg()
    samples = [
        _sample("v2", 85.0, fam="question"),
        _sample("v1", 80.0, fam="question"),
        _sample("v0", 75.0, fam="question"),
    ]
    doc = optimizer.update_active_patterns(cfg, samples)
    key = "hook_type:question"
    assert key in {a["key"] for a in doc["active"]}
    directive = optimizer.active_patterns_directive(cfg)
    assert directive and "FORCE pattern" in directive and "question" in directive


def test_active_patterns_pas_de_force_sous_70(make_cfg):
    cfg = make_cfg()
    samples = [_sample("v%d" % i, 60.0, fam="question") for i in range(5)]
    doc = optimizer.update_active_patterns(cfg, samples)
    assert doc["active"] == []
    assert optimizer.active_patterns_directive(cfg) is None


def test_active_patterns_pas_de_force_sous_3_shorts(make_cfg):
    cfg = make_cfg()
    samples = [_sample("v%d" % i, 90.0, fam="question") for i in range(2)]
    doc = optimizer.update_active_patterns(cfg, samples)
    assert doc["active"] == []


def test_active_patterns_decremente_et_rearme(make_cfg):
    """Decompte par nouveau Short ; re-armement seulement si groupe elargi."""
    cfg = make_cfg()
    samples = [_sample("v%d" % i, 85.0, fam="question") for i in range(3)]
    doc = optimizer.update_active_patterns(cfg, samples)
    assert int(doc["active"][0]["remaining"]) == 5
    # Nouveau Short publie -> -1
    doc = optimizer.update_active_patterns(cfg, samples + [_sample("v9", 50.0)])
    assert int(doc["active"][0]["remaining"]) == 4
    # Rien de neuf -> pas de double decrement
    doc = optimizer.update_active_patterns(cfg, samples + [_sample("v9", 50.0)])
    assert int(doc["active"][0]["remaining"]) == 4
    # Groupe elargi apres expiration -> re-armement
    for i in range(4):
        samples = samples + [_sample("w%d" % i, 50.0, fam="statement")]
        doc = optimizer.update_active_patterns(cfg, samples)
    doc = optimizer.update_active_patterns(
        cfg, samples + [_sample("w9", 88.0, fam="question")])
    entry = [a for a in doc["active"] if a["key"] == "hook_type:question"]
    assert entry and int(entry[0]["remaining"]) == 5


def test_active_patterns_injecte_dans_apply_config(make_cfg):
    """apply_config doit propager les patterns actifs dans style_directive."""
    cfg = make_cfg()
    cfg.setdefault("optimizer", {})["enabled"] = True
    samples = [_sample("v%d" % i, 85.0, fam="question") for i in range(3)]
    optimizer.update_active_patterns(cfg, samples)
    applied = optimizer.apply_config(cfg)
    sd = cfg["ideas"]["style_directive"]
    assert "FORCE pattern" in sd and "hook_type=question" in sd