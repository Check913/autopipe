# -*- coding: utf-8 -*-
"""Registre memoire : jointure idea_id <-> youtube_id (aucun reseau)."""


def test_register_creates_entry_and_map(make_cfg):
    from autopipe import registry
    cfg = make_cfg()
    idea = {"id": "20261029-004004-01", "title": "Test Hook",
            "pair_id": "pair-1", "hook_variant": "question",
            "originality": {"max_jaccard": 0.12}}
    entry = registry.register(idea, "abc123VID", cfg=cfg,
                              published_at="2026-10-29T12:00:00")
    assert entry["idea_id"] == "20261029-004004-01"
    assert entry["title"] == "Test Hook"
    assert entry["pair_id"] == "pair-1"
    assert entry["source"] == "runtime"
    doc = registry.load(cfg)
    assert "abc123VID" in doc["videos"]
    assert registry.map_video_to_idea(cfg)["abc123VID"] == "20261029-004004-01"
    assert registry.all_ids(cfg) == ["abc123VID"]


def test_register_merge_never_eraser_false(make_cfg):
    """Fusion douce : backfill puis runtime, aucune valeur None n'efface."""
    from autopipe import registry
    cfg = make_cfg()
    registry.backfill(cfg)  # registre vide : source legacy only
    idea = {"id": "idee-1", "title": "Titre A", "pair_id": "p"}
    registry.register(idea, "vid1", cfg=cfg)
    # Deuxieme enregistrement sans idea_id (publish_pending_video minimal)
    registry.register({"id": None, "title": "Titre A"}, "vid1", cfg=cfg)
    doc = registry.load(cfg)
    e = doc["videos"]["vid1"]
    assert e["idea_id"] == "idee-1"          # conserver
    assert e["title"] == "Titre A"            # ecrase par valeur non-None


def test_all_ids_union_legacy(make_cfg):
    """all_ids : registre + sources legacy (latest.json, last_run)."""
    from autopipe import registry
    from autopipe.util import save_json
    cfg = make_cfg()
    registry.register({"id": "i1", "title": "T"}, "vid1", cfg=cfg)
    paths = registry.get_paths(cfg)
    save_json(paths.analytics / "latest.json",
              {"rows": [{"id": "vid2"}, {"id": "vid1"}]})
    save_json(paths.data / "last_run.json", {"youtube_id": "vid3"})
    ids = registry.all_ids(cfg)
    assert ids == ["vid1", "vid2", "vid3"]  # ordre stable + dedup


def test_backfill_merges_legacy_rows(make_cfg, monkeypatch):
    """Le backfill source legacy latest.json -> entrees source=legacy."""
    from autopipe import registry
    from autopipe.util import save_json
    cfg = make_cfg()
    paths = registry.get_paths(cfg)
    save_json(paths.analytics / "latest.json", {"rows": [
        {"id": "vidX", "title": "Legacy Title", "published": "2026-10-24T10:00:00Z"}]})
    registry.backfill(cfg)
    doc = registry.load(cfg)
    e = doc["videos"]["vidX"]
    assert e["title"] == "Legacy Title"
    assert e["source"] in ("legacy", "backfill", "runtime")
    assert e["published_at"]


def test_report_counts(make_cfg):
    from autopipe import registry
    cfg = make_cfg()
    registry.register({"id": "i1", "title": "A", "pair_id": "p1"},
                      "vid1", cfg=cfg)
    registry.register({"id": "i2", "title": "B"}, "vid2", cfg=cfg)
    rep = registry.report(cfg)
    assert rep["total"] == 2
    assert rep["linked_to_idea"] == 2
    assert rep["linked_to_ab_pair"] == 1
    assert rep["file"].endswith("published.json")


def test_register_minimal_pending_idea(make_cfg):
    """publish_pending_video passe un dict minimal {"id","title"} -> OK."""
    from autopipe import registry
    cfg = make_cfg()
    entry = registry.register({"id": "idee-9", "title": "Min"}, "vid9",
                              cfg=cfg)
    assert entry["idea_id"] == "idee-9"
    assert entry["hook"] is None
