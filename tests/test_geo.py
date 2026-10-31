"""Tests du verrou geo US (autopipe/geo.py).

Tout est isole : la fonction de fetch reseau est monkeypatchee, aucune
requete n'est emise. On verifie :
- pays US -> ok (publication autorisee) ;
- pays non-US -> GeoError en strict, simple warning en warn ;
- service injoignable -> fail-closed (refus en strict) ;
- geo_check=off -> aucun controle, aucune requete ;
- cache respecte dans le TTL, re-interrogation avec --force et quand
  la sortie (proxy/direct) change ;
- token_health applique le meme egress que la publication.
"""
import pytest

from autopipe import alerts, geo
from autopipe.geo import GeoError, check_exit_country, enforce_exit_country

US_PAYLOAD = {"status": "ok", "countryCode": "US", "query": "1.2.3.4",
              "isp": "US-ISP"}
FR_PAYLOAD = {"status": "ok", "countryCode": "FR", "query": "9.9.9.9",
              "isp": "FR-ISP"}


@pytest.fixture
def no_env_mutation(monkeypatch):
    """Neutralise la mutation des variables d'environnement proxy."""
    monkeypatch.setattr(geo, "apply_upload_proxy", lambda url: url)
    monkeypatch.setattr(geo, "fix_proxy_env", lambda mode="direct": mode)


@pytest.fixture
def fake_fetch(monkeypatch):
    """Remplace geo._fetch : repond selon l'URL et enregistre les appels."""
    calls = []

    def _install(payload=US_PAYLOAD, error_for=()):
        def _fetch(url, timeout):
            calls.append(url)
            if url in error_for:
                raise OSError("service injoignable")
            return payload
        monkeypatch.setattr(geo, "_fetch", _fetch)
        return calls

    return _install


def _cfg(make_cfg, **net):
    base = {"geo_check": "strict",
            "geo_check_url": "http://ip-api.com/json/",
            "geo_check_ttl_min": 30,
            "geo_expected_country": "US"}
    base.update(net)
    return make_cfg(network=base)


def test_us_ok_check_and_enforce(make_cfg, no_env_mutation, fake_fetch):
    calls = fake_fetch()
    cfg = _cfg(make_cfg)
    state = check_exit_country(cfg, force=True)
    assert state["ok"] is True
    assert state["country"] == "US"
    assert state["ip"] == "1.2.3.4"
    assert state["cached"] is False
    # Le service principal repond : le fallback ipinfo n'est pas interroge.
    assert len(calls) == 1
    state2 = enforce_exit_country(cfg)
    assert state2["ok"] is True


def test_non_us_strict_refuses(make_cfg, no_env_mutation, fake_fetch):
    fake_fetch(payload=FR_PAYLOAD)
    cfg = _cfg(make_cfg)
    with pytest.raises(GeoError):
        enforce_exit_country(cfg)


def test_non_us_warn_continues(make_cfg, no_env_mutation, fake_fetch):
    fake_fetch(payload=FR_PAYLOAD)
    cfg = _cfg(make_cfg, geo_check="warn")
    state = enforce_exit_country(cfg)
    assert state["ok"] is False
    assert state["country"] == "FR"


def test_unreachable_fails_closed(make_cfg, no_env_mutation, fake_fetch):
    calls = fake_fetch(error_for=("http://ip-api.com/json/",
                                  "https://ipinfo.io/json"))
    cfg = _cfg(make_cfg)
    state = check_exit_country(cfg, force=True)
    assert state["ok"] is False
    assert state["country"] is None
    assert len(calls) == 2  # service principal + fallback
    with pytest.raises(GeoError):
        enforce_exit_country(cfg)


def test_off_skips_everything(make_cfg, no_env_mutation, fake_fetch):
    calls = fake_fetch()
    cfg = _cfg(make_cfg, geo_check="off")
    state = check_exit_country(cfg)
    assert state.get("skipped") is True
    assert state["ok"] is None
    assert enforce_exit_country(cfg).get("skipped") is True
    assert calls == []


def test_cache_hit_within_ttl(make_cfg, no_env_mutation, fake_fetch):
    calls = fake_fetch()
    cfg = _cfg(make_cfg)
    first = check_exit_country(cfg, force=True)
    second = check_exit_country(cfg)
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["country"] == "US"
    assert second["ok"] is True
    assert len(calls) == 1


def test_force_bypasses_cache(make_cfg, no_env_mutation, fake_fetch):
    calls = fake_fetch()
    cfg = _cfg(make_cfg)
    check_exit_country(cfg, force=True)
    check_exit_country(cfg, force=True)
    assert len(calls) == 2


def test_egress_change_refetches(make_cfg, no_env_mutation, fake_fetch):
    """La clee de cache est la SORTIE (proxy/direct), pas le temps seul."""
    calls = fake_fetch()
    cfg_direct = _cfg(make_cfg)
    first = check_exit_country(cfg_direct)
    assert first["egress"] == "direct"
    cfg_proxy = _cfg(make_cfg, upload_proxy="http://us-proxy:8080")
    second = check_exit_country(cfg_proxy)
    assert second["egress"] == "proxy http://us-proxy:8080"
    assert second["cached"] is False  # sortie differente -> verifiee a neuf
    assert len(calls) == 2


def test_country_payload_parsing():
    assert geo._country_of({"countryCode": "us"})[0] == "US"
    assert geo._country_of({"country": "United States"})[0] == "US"
    assert geo._country_of({"country": "US", "ip": "5.6.7.8"})[1] == "5.6.7.8"
    assert geo._country_of({"status": "ok"}) is None
    assert geo._country_of("not-a-dict") is None


def test_token_health_uses_upload_proxy(make_cfg, monkeypatch):
    seen = {"apply": [], "fix": []}
    monkeypatch.setattr(alerts, "apply_upload_proxy",
                        lambda url: seen["apply"].append(url) or url)
    monkeypatch.setattr(alerts, "fix_proxy_env",
                        lambda mode="direct": seen["fix"].append(mode) or mode)
    cfg = make_cfg(upload={"token_file": "absent_test.json"},
                   network={"upload_proxy": "http://us-proxy:8080",
                            "proxy_mode": "direct"})
    health = alerts.token_health(cfg)
    assert seen["apply"] == ["http://us-proxy:8080"]
    assert seen["fix"] == []
    # fichier de token absent du root -> status missing, sans reseau.
    assert health["status"] == "missing"


def test_token_health_direct_uses_fix(make_cfg, monkeypatch):
    seen = {"apply": [], "fix": []}
    monkeypatch.setattr(alerts, "apply_upload_proxy",
                        lambda url: seen["apply"].append(url) or url)
    monkeypatch.setattr(alerts, "fix_proxy_env",
                        lambda mode="direct": seen["fix"].append(mode) or mode)
    cfg = make_cfg(upload={"token_file": "absent_test.json"},
                   network={"upload_proxy": "", "proxy_mode": "direct"})
    health = alerts.token_health(cfg)
    assert seen["apply"] == []
    assert seen["fix"] == ["direct"]
    assert health["status"] == "missing"


def test_upload_video_guard_wired():
    """upload_video doit appeler enforce_exit_country avant get_service."""
    import inspect

    from autopipe import upload
    src = inspect.getsource(upload.upload_video)
    assert "enforce_exit_country(cfg)" in src
    body = src.split("enforce_exit_country(cfg)", 1)[1]
    assert "get_service" in body  # le verrou AVANT toute connexion YouTube
