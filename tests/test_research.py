# -*- coding: utf-8 -*-
"""MODULE 4 : benchmarks quantitatifs de la niche (aucun reseau)."""
import time


def _entry(eid, views, title, dur, days_ago=1.0):
    return {"id": eid, "title": title, "channel": "x", "views": views,
            "duration": dur, "upload_date": "",
            "timestamp": time.time() - days_ago * 86400.0}


# ---------------------------------------------------------------- _ydl_opts
def test_ydl_opts_direct_forces_no_proxy(make_cfg):
    from autopipe import research
    cfg = make_cfg(network={"proxy_mode": "direct"})
    opts = research._ydl_opts(cfg)
    assert opts["proxy"] == ""
    assert opts["extract_flat"] == "in_playlist"
    opts2 = research._ydl_opts(None)
    assert "proxy" not in opts2 or opts2["proxy"] != ""


def test_ydl_opts_extra_keys(make_cfg):
    from autopipe import research
    cfg = make_cfg(network={"proxy_mode": "direct"},
                   research={"ydl_extra": {"sleep_interval": 2}})
    assert research._ydl_opts(cfg)["sleep_interval"] == 2


# ------------------------------------------------------------------ winners
def test_winners_threshold_excludes_small(make_cfg):
    from autopipe import research
    cfg = make_cfg(research={"min_views": 100000, "top_n": 20})
    entries = [_entry("a", 200000, "Number one fact", 35),
               _entry("b", 5000, "low views", 40)]
    winners = research._winners(entries, cfg)
    assert [w["id"] for w in winners] == ["a"]


def test_winners_fallback_top_n(make_cfg):
    """Petite niche : si < 5 gagnants, top_n par vues (pas de vide)."""
    from autopipe import research
    cfg = make_cfg(research={"min_views": 100000, "top_n": 20})
    entries = [_entry("a", 10, "x", 30), _entry("b", 5, "y", 31)]
    winners = research._winners(entries, cfg)
    assert len(winners) == 2


def test_winners_dedup_and_no_views(make_cfg):
    from autopipe import research
    cfg = make_cfg(research={"min_views": 100000, "top_n": 20})
    entries = [_entry("a", 10, "x", 30), {"id": "a", "views": 10},
               {"id": "b", "views": None}]
    winners = research._winners(entries, cfg)
    assert [w["id"] for w in winners] == ["a"]


# ------------------------------------------------------------------ analyze
def test_analyze_benchmarks(make_cfg):
    from autopipe import research
    cfg = make_cfg(research={"min_views": 100000})
    entries = [
        _entry("a", 300000, "10 secrets you did not know", 35, 2),
        _entry("b", 250000, "Why do we dream?", 42, 3),
        _entry("c", 150000, "Scientists found this", 50, 4),
        _entry("d", 120000, "5 minute rule works", 33, 1),
        _entry("e", 110000, "Nobody expected this", 44, 5),
        _entry("f", 90000, "small channel", 60, 1),  # exclu (min_views)
    ]
    doc, winners = research._analyze(entries, cfg)
    assert doc["n_scraped"] == 6
    assert doc["n_winners"] == 5
    assert doc["duration_median_s"] == 42.0  # mediane [33,35,42,44,50]
    assert doc["duration_p25_s"] < doc["duration_p75_s"]
    assert doc["pct_number_title"] == 40.0  # 2/5 titres avec chiffre
    assert doc["hook_families_pct"]
    assert doc["velocity_median_vpd"] > 0
    assert len(doc["top_titles"]) == 5
    assert winners[0]["views"] == 300000  # trie par vues desc


# ------------------------------------------------------------------ directive
def test_directive_empty_without_insights(make_cfg):
    from autopipe import research
    assert research.research_directive(make_cfg()) == ""


def test_directive_built_from_insights(make_cfg):
    from autopipe import research
    cfg = make_cfg(research={"cache_hours": 12})
    doc = {"generated_at": "2026-10-29T00:00:00", "n_winners": 8,
           "duration_median_s": 40, "duration_p25_s": 30,
           "duration_p75_s": 50,
           "hook_families_pct": {"question": 55.0, "listicle": 20.0},
           "pct_number_title": 40.0, "velocity_median_vpd": 25000,
           "top_titles": [{"title": "Most viral title now",
                           "views": 900000, "duration": 35}]}
    research.save_json(research._insights_file(cfg), doc)
    d = research.research_directive(cfg)
    assert "30-50s" in d
    assert "question" in d
    assert "views/day" in d
    assert "Most viral title now" in d
    assert d.endswith("Match these numbers exactly.")


def test_inject_directive_concatenates_after_optimizer(make_cfg):
    """inject_directive s'APPLIQUE APRES hook_before_batch (ecraserait sinon)."""
    from autopipe import research
    cfg = make_cfg(research={"cache_hours": 12})
    doc = {"generated_at": "2026-10-29T00:00:00", "n_winners": 8,
           "duration_median_s": 40, "duration_p25_s": 30,
           "duration_p75_s": 50, "hook_families_pct": {"question": 55.0},
           "pct_number_title": 40.0, "velocity_median_vpd": 25000,
           "top_titles": []}
    research.save_json(research._insights_file(cfg), doc)
    cfg["ideas"] = {"style_directive": "APPRENDED LESSON"}
    cfg["remake"] = {"style_directive": ""}
    research.inject_directive(cfg)
    assert cfg["ideas"]["style_directive"].startswith("APPRENDED LESSON | ")
    assert "APPRENDED LESSON" not in cfg["remake"]["style_directive"]
    assert "Benchmark research" in cfg["remake"]["style_directive"]
    # Idempotent : pas de double injection
    research.inject_directive(cfg)
    assert cfg["ideas"]["style_directive"].count("Benchmark research") == 1


# -------------------------------------------------------------- run_research
def test_run_research_history_and_cache(monkeypatch, make_cfg):
    import datetime as dt
    from autopipe import research
    cfg = make_cfg(research={"cache_hours": 12, "keywords": ["kw"]})
    # generated_at FRAIS (horloge reelle) : sinon le TTL 12h depend de
    # l'heure de la machine et le test devient non determinant.
    fake = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "n_scraped": 6, "n_winners": 6, "duration_median_s": 40}
    calls = []

    def fake_collect(c):
        calls.append(1)
        return ([_entry("a", 100, "x", 30)], 1)

    monkeypatch.setattr(research, "_collect", fake_collect)
    monkeypatch.setattr(research, "_analyze",
                        lambda entries, c: (dict(fake), []))
    out = research.run_research(cfg, force=True)
    assert out["n_winners"] == 6
    assert len(calls) == 1
    hist = (research.get_paths(cfg).research / "history.jsonl") \
        .read_text(encoding="utf-8").strip().splitlines()
    assert len(hist) == 1 and '"n_winners": 6' in hist[0]

    # Cache frais -> pas de re-scrape
    research.run_research(cfg)
    assert len(calls) == 1


def test_run_research_empty_scrape_keeps_cache(monkeypatch, make_cfg):
    from autopipe import research
    cfg = make_cfg(research={"cache_hours": 12, "keywords": ["kw"]})
    fake = {"generated_at": "2026-10-29T00:00:00", "n_winners": 6}
    monkeypatch.setattr(research, "_collect", lambda c: ([], 0))
    monkeypatch.setattr(research, "_analyze",
                        lambda entries, c: (dict(fake), []))
    assert research.run_research(cfg, force=True)["n_winners"] == 6
    # Second run : 0 entrees -> insights conserves (jamais ecrase par vide)
    out = research.run_research(cfg, force=True)
    assert out["n_winners"] == 6


def test_run_research_disabled_without_sources(make_cfg):
    from autopipe import research
    cfg = make_cfg(research={"enabled": True, "keywords": [],
                             "competitors": []})
    cfg["scout"] = {"keywords": []}
    out = research.run_research(cfg, force=True)
    assert out == {}


def test_report_readability(make_cfg):
    from autopipe import research
    cfg = make_cfg()
    assert "Aucun insight" in research.report(cfg)
