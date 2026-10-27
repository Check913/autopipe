# -*- coding: utf-8 -*-
"""Tests anti-doublon pour _next_window_start / _mark_window_served.

Contexte : le 2026-10-27 a 18:00:02 UTC la video a ete publiee, le pending
efface, puis la grace de 5 min a re-selectionne la MEME fenetre -> 2e
generation + 2e upload. Ces tests verrouillent le correctif.
"""
import datetime as dt

from autopipe.scheduler import _next_window_start, _mark_window_served

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 27, 18, 0, 5, tzinfo=UTC)  # 5 s apres 18:00
HOURS = [18]


def test_served_window_in_grace_returns_tomorrow():
    # Fenetre 18:00 deja servie -> pendant la grace, on NE repick PAS 18:00.
    served = {"2026-10-27T18:00:00+00:00"}
    nxt = _next_window_start(NOW, HOURS, served=served)
    assert nxt == dt.datetime(2026, 10, 28, 18, 0, 0, tzinfo=UTC)


def test_unserved_window_in_grace_returns_canonical_hour():
    # Fenetre non servie pendant la grace -> cible l'heure PILE (pas `now`),
    # sinon la publication serait decalee de quelques secondes a chaque run.
    nxt = _next_window_start(NOW, HOURS, served=set())
    assert nxt == dt.datetime(2026, 10, 27, 18, 0, 0, tzinfo=UTC)


def test_future_window_unaffected_by_served():
    # Fenetre future (pas encore servie) -> selectionnee normalement.
    now = dt.datetime(2026, 10, 27, 17, 30, 0, tzinfo=UTC)
    nxt = _next_window_start(now, HOURS, served=set())
    assert nxt == dt.datetime(2026, 10, 27, 18, 0, 0, tzinfo=UTC)


def test_served_window_after_grace_returns_tomorrow():
    # Hors grace (> 5 min) : comportement original (jour suivant).
    now = dt.datetime(2026, 10, 27, 18, 7, 0, tzinfo=UTC)
    served = {"2026-10-27T18:00:00+00:00"}
    nxt = _next_window_start(now, HOURS, served=served)
    assert nxt == dt.datetime(2026, 10, 28, 18, 0, 0, tzinfo=UTC)


def test_served_uses_canonical_iso_format():
    # Le marqueur sert l'ISO de l'heure pile : verifier la correspondance
    # exacte entre _mark_window_served et le test d'appartenance.
    state = {}
    _mark_window_served(state, "2026-10-27T18:00:00+00:00", today="2026-10-27")
    nxt = _next_window_start(NOW, HOURS, served=set(state["served_windows"]))
    assert nxt == dt.datetime(2026, 10, 28, 18, 0, 0, tzinfo=UTC)


def test_mark_window_served_prunes_other_days():
    state = {"served_windows": ["2026-10-26T15:00:00+00:00",
                                "2026-10-27T15:00:00+00:00"]}
    _mark_window_served(state, "2026-10-27T18:00:00+00:00", today="2026-10-27")
    assert state["served_windows"] == sorted(
        ["2026-10-27T15:00:00+00:00", "2026-10-27T18:00:00+00:00"])
