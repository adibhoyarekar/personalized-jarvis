"""Offline tests for network/OS-dependent tools, with their I/O mocked."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import Config
from tools import ToolContext, set_context


@pytest.fixture(autouse=True)
def ctx(tmp_path: Path) -> None:
    cfg = Config(
        {"weather": {"city": "Chhatrapati Sambhajinagar", "lat": 19.8762, "lon": 75.3433, "units": "celsius"},
         "news_feeds": ["https://a.example/rss", "https://b.example/rss"],
         "projects": {"cs-events": {"path": str(tmp_path), "dev_command": "npm run dev"}}},
        tmp_path,
    )
    set_context(ToolContext(config=cfg))


FORECAST = {
    "current": {"temperature_2m": 27.4, "apparent_temperature": 29.1, "relative_humidity_2m": 61,
                "weather_code": 2, "wind_speed_10m": 11.2},
    "daily": {"temperature_2m_max": [31.2, 30.0], "temperature_2m_min": [21.0, 20.4],
              "precipitation_probability_max": [20, 65], "weather_code": [2, 61]},
}


def test_weather_default_city(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import web_tools

    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **params: Any) -> Any:
        calls.append((url, params))
        return SimpleNamespace(json=lambda: FORECAST)

    monkeypatch.setattr(web_tools, "_get", fake_get)
    report = web_tools.get_weather()
    assert report["place"] == "Chhatrapati Sambhajinagar"
    assert report["now"]["temperature"] == "27°C" and report["now"]["conditions"] == "partly cloudy"
    assert report["tomorrow"]["conditions"] == "light rain" and report["tomorrow"]["chance_of_rain"] == "65%"
    assert calls[0][1]["latitude"] == 19.8762  # no geocoding for the configured city


def test_weather_other_city_geocodes(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import web_tools

    def fake_get(url: str, **params: Any) -> Any:
        if "geocoding" in url:
            return SimpleNamespace(json=lambda: {"results": [{"name": "Pune", "admin1": "Maharashtra",
                                                              "country": "India", "latitude": 18.5, "longitude": 73.8}]})
        assert params["latitude"] == 18.5
        return SimpleNamespace(json=lambda: FORECAST)

    monkeypatch.setattr(web_tools, "_get", fake_get)
    assert web_tools.get_weather("Pune")["place"] == "Pune, Maharashtra, India"


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>{src}</title>
<item><title>{a}</title><pubDate>Fri, 16 Oct 2026 07:00:00 GMT</pubDate></item>
<item><title>{b}</title><pubDate>Fri, 16 Oct 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_news_merges_feeds_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import web_tools

    feeds = {
        "https://a.example/rss": RSS.format(src="A News", a="Cricket final tonight", b="Markets rise"),
        "https://b.example/rss": RSS.format(src="B News", a="Rain expected", b="Markets rise"),
    }
    monkeypatch.setattr(web_tools, "_get", lambda url, **p: SimpleNamespace(content=feeds[url].encode()))
    news = web_tools.get_news(limit=5)["headlines"]
    titles = [h["title"] for h in news]
    assert titles[:2] in (["Cricket final tonight", "Rain expected"], ["Rain expected", "Cricket final tonight"])
    assert titles.count("Markets rise") == 1  # de-duplicated
    assert [h["title"] for h in web_tools.get_news(topic="cricket")["headlines"]] == ["Cricket final tonight"]


def test_news_all_feeds_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import web_tools

    def down(url: str, **p: Any) -> Any:
        raise RuntimeError("I can't reach the internet right now")

    monkeypatch.setattr(web_tools, "_get", down)
    with pytest.raises(RuntimeError, match="news feeds"):
        web_tools.get_news()


def test_start_menu_matching() -> None:
    from tools.system_tools import match_shortcut

    shortcuts = {n: Path(f"{n}.lnk") for n in ["excel", "visual studio code", "word", "wordpad", "spotify",
                                                "microsoft teams"]}
    assert match_shortcut("Excel", shortcuts).stem == "excel"
    assert match_shortcut("word", shortcuts).stem == "word"
    assert match_shortcut("visual", shortcuts).stem == "visual studio code"
    assert match_shortcut("teams", shortcuts).stem == "microsoft teams"
    assert match_shortcut("spotfy", shortcuts).stem == "spotify"  # fuzzy
    assert match_shortcut("photoshop", shortcuts) is None


def test_project_lookup_is_fuzzy_and_helpful(tmp_path: Path) -> None:
    from tools.dev_tools import find_project

    assert find_project("cs events").path == tmp_path
    assert find_project("CS-Events").name == "cs-events"
    with pytest.raises(ValueError, match="cs-events"):
        find_project("website")
