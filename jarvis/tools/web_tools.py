"""Web tools: search, opening sites, weather (Open-Meteo) and news (RSS)."""

from __future__ import annotations

import datetime as dt
import logging
import time
import webbrowser
from typing import Any
from urllib.parse import quote_plus, urlparse

from tools import setting, tool

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 8
USER_AGENT = "Jarvis-desktop-assistant/1.0"


def _get(url: str, **params: Any) -> Any:
    """GET with a timeout and a clear "no internet" error."""
    import requests

    try:
        resp = requests.get(url, params=params or None, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp
    except requests.ConnectionError as exc:
        raise RuntimeError("I can't reach the internet right now") from exc
    except requests.Timeout as exc:
        raise RuntimeError("The request timed out; the connection may be slow") from exc
    except requests.HTTPError as exc:
        raise RuntimeError(f"The service answered with an error ({exc.response.status_code})") from exc


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not urlparse(url).scheme:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Not a web address: {url!r}")
    return url


@tool
def open_url(url: str) -> str:
    """Open a website in the default browser.

    Use for "go to github.com", "open YouTube", or to show a page found by web_search.

    Args:
        url: Full URL or bare domain, e.g. "https://github.com" or "youtube.com".
    """
    target = _normalize_url(url)
    if not webbrowser.open(target):
        raise RuntimeError("No web browser available")
    return f"Opened {target}."


@tool
def web_search(query: str, max_results: int = 5) -> list | str:
    """Search the web with DuckDuckGo and return the top results (title, snippet, link).

    Use for facts you don't know, current events, prices, or anything recent.
    Summarise the answer in a sentence or two; don't read out links.

    Args:
        query: What to search for.
        max_results: Number of results to return (1-10).
    """
    max_results = max(1, min(int(max_results), 10))
    try:
        from ddgs import DDGS
    except ImportError:  # older package name
        try:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError("Web search needs the 'ddgs' package: pip install ddgs") from exc
    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 - network/rate-limit errors vary by version
        log.warning("web_search failed: %s", exc)
        raise RuntimeError(
            f"Search failed ({type(exc).__name__}); the internet may be down. "
            f"Fallback link: https://duckduckgo.com/?q={quote_plus(query)}"
        ) from exc
    if not results:
        return f"No results for '{query}'."
    return [
        {"title": r.get("title", ""), "snippet": r.get("body", ""), "url": r.get("href", "")}
        for r in results[:max_results]
    ]


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def geocode(city: str) -> tuple[float, float, str]:
    """City name -> (lat, lon, display name) via Open-Meteo's free geocoder."""
    data = _get("https://geocoding-api.open-meteo.com/v1/search", name=city, count=1, language="en").json()
    results = data.get("results") or []
    if not results:
        raise ValueError(f"I couldn't find a place called {city}")
    top = results[0]
    label = ", ".join(x for x in (top.get("name"), top.get("admin1"), top.get("country")) if x)
    return float(top["latitude"]), float(top["longitude"]), label


def weather_report(city: str = "") -> dict:
    """Current conditions and today's/tomorrow's forecast (used by the tool and the briefing)."""
    if city.strip():
        lat, lon, label = geocode(city)
    else:
        lat, lon = setting("weather.lat"), setting("weather.lon")
        label = setting("weather.city", "")
        if lat is None or lon is None:
            if not label:
                raise ValueError("No city given and no default city in config.yaml (weather.city)")
            lat, lon, label = geocode(label)
    unit = str(setting("weather.units", "celsius")).lower()
    data = _get(
        "https://api.open-meteo.com/v1/forecast",
        latitude=lat,
        longitude=lon,
        current="temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        daily="temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        timezone="auto",
        forecast_days=2,
        temperature_unit="fahrenheit" if unit.startswith("f") else "celsius",
    ).json()
    cur, daily = data.get("current", {}), data.get("daily", {})
    sym = "°F" if unit.startswith("f") else "°C"

    def day(i: int) -> dict:
        return {
            "conditions": WMO_CODES.get(int(daily["weather_code"][i]), "unknown"),
            "high": f"{round(daily['temperature_2m_max'][i])}{sym}",
            "low": f"{round(daily['temperature_2m_min'][i])}{sym}",
            "chance_of_rain": f"{daily['precipitation_probability_max'][i]}%",
        }

    return {
        "place": label,
        "now": {
            "temperature": f"{round(cur['temperature_2m'])}{sym}",
            "feels_like": f"{round(cur['apparent_temperature'])}{sym}",
            "conditions": WMO_CODES.get(int(cur.get("weather_code", -1)), "unknown"),
            "humidity": f"{cur.get('relative_humidity_2m')}%",
            "wind": f"{round(cur.get('wind_speed_10m', 0))} km/h",
        },
        "today": day(0),
        "tomorrow": day(1),
    }


@tool
def get_weather(city: str = "") -> dict:
    """Current weather and the forecast for today and tomorrow.

    Leave city empty for the user's home city from config.

    Args:
        city: A city name, e.g. "Pune" or "London". Empty for the default city.
    """
    return weather_report(city)


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def headlines(limit: int = 5, topic: str = "") -> list[dict]:
    """Newest headlines across the configured RSS feeds (used by the tool and the briefing)."""
    import feedparser

    feeds = setting("news_feeds") or ["https://feeds.bbci.co.uk/news/world/rss.xml"]
    items: list[tuple[float, dict]] = []
    errors = 0
    for url in feeds:
        try:
            parsed = feedparser.parse(_get(url).content)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("News feed %s failed: %s", url, exc)
            continue
        source = parsed.feed.get("title", urlparse(url).netloc)
        for entry in parsed.entries[:30]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            ts = time.mktime(stamp) if stamp else 0.0
            items.append((ts, {"title": title, "source": source, "summary": (entry.get("summary") or "")[:200]}))
    if not items and errors:
        raise RuntimeError("I couldn't load any news feeds; the internet may be down")
    if topic.strip():
        words = topic.lower().split()
        items = [i for i in items if any(w in (i[1]["title"] + " " + i[1]["summary"]).lower() for w in words)]
    items.sort(key=lambda i: i[0], reverse=True)
    seen, out = set(), []
    for _, item in items:
        if item["title"].lower() in seen:
            continue
        seen.add(item["title"].lower())
        out.append(item)
        if len(out) >= limit:
            break
    return out


@tool
def get_news(topic: str = "", limit: int = 5) -> dict:
    """Latest news headlines from the user's RSS feeds.

    Read out headlines briefly; don't read the summaries unless asked.

    Args:
        topic: Optional keyword filter, e.g. "cricket" or "technology".
        limit: Number of headlines (1-10).
    """
    limit = max(1, min(int(limit), 10))
    found = headlines(limit, topic)
    return {
        "as_of": dt.datetime.now().strftime("%H:%M"),
        "headlines": found or f"No headlines matching '{topic}'.",
    }
