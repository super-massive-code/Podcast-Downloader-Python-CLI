"""RSS feed parsing and iTunes podcast search."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from podcast_downloader import __version__

_USER_AGENT = f"PodcastDownloader/{__version__}"

_SEARCH_URL = "https://itunes.apple.com/search"


class FeedError(Exception):
    """A search or feed fetch that failed, with a reason fit to show the user."""


def search_podcasts(term: str) -> list[dict[str, str]]:
    """Search iTunes for podcasts matching term.

    Returns a list of dicts with keys: title, author, feed_url, artwork_url.
    Results without a feedUrl are skipped. Raises FeedError if the search
    itself fails, so an outage is never mistaken for "no results".
    """
    try:
        response = httpx.get(
            _SEARCH_URL,
            params={"term": term, "entity": "podcast", "limit": "25"},
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise FeedError(f"could not reach iTunes search: {exc}") from exc
    if response.status_code != 200:
        raise FeedError(f"iTunes search returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise FeedError("iTunes search returned an unreadable response") from exc

    results: list[dict[str, str]] = []
    for item in data.get("results", []):
        feed_url = item.get("feedUrl")
        if not feed_url:
            continue
        results.append({
            "title": item.get("collectionName", ""),
            "author": item.get("artistName", ""),
            "feed_url": feed_url,
            "artwork_url": item.get("artworkUrl600", ""),
        })
    return results


def _parse_duration(value: Any) -> int | None:
    """itunes:duration as seconds; feeds use plain seconds, MM:SS or HH:MM:SS."""
    if not value:
        return None
    try:
        seconds = 0
        for part in str(value).strip().split(":"):
            seconds = seconds * 60 + int(float(part))
        return seconds
    except ValueError:
        return None


def parse_feed(url: str) -> dict[str, Any]:
    """Fetch and parse an RSS podcast feed.

    Returns a dict with keys: title, author, artwork_url, episodes.
    Each episode has: guid, title, published (str or None), audio_url, duration.
    Raises FeedError, carrying the reason, if the feed cannot be fetched or read.
    """
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise FeedError(f"could not fetch {url}: {exc}") from exc
    if response.status_code != 200:
        raise FeedError(f"HTTP {response.status_code} fetching {url}")

    feed = feedparser.parse(response.text)
    if feed.bozo and not feed.entries:
        raise FeedError(f"{url} is not a readable podcast feed")

    try:
        channel = feed.feed
        title = channel.get("title", "")
        author = channel.get("itunes_author", channel.get("author", ""))
        artwork_url = ""
        itunes_image = channel.get("itunes_image")
        if itunes_image:
            artwork_url = itunes_image.get("href", "")
        if not artwork_url:
            channel_image = channel.get("image")
            if channel_image:
                artwork_url = channel_image.get("href", "")

        episodes: list[dict[str, Any]] = []
        for entry in feed.entries:
            # GUID: entry.id, fall back to enclosure URL
            guid = entry.get("id") or ""
            if not guid:
                enclosures = entry.get("enclosures", [])
                if isinstance(enclosures, (list, tuple)) and enclosures:
                    first = enclosures[0]
                    if isinstance(first, dict):
                        guid = first.get("url", "")

            # Title
            ep_title = entry.get("title", "Untitled Episode")

            # Published date: published_parsed -> YYYY-MM-DD
            published = None
            published_parsed = entry.get("published_parsed")
            if published_parsed:
                try:
                    dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                    published = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    published = None

            # Audio URL: first enclosure with type starting with 'audio/', else first enclosure
            audio_url = ""
            enclosures = entry.get("enclosures", [])
            if not isinstance(enclosures, (list, tuple)):
                enclosures = []
            for enc in enclosures:
                if isinstance(enc, dict) and enc.get("type", "").startswith("audio/"):
                    audio_url = enc.get("url", "")
                    break
            if not audio_url and isinstance(enclosures, (list, tuple)) and enclosures:
                first = enclosures[0]
                if isinstance(first, dict):
                    audio_url = first.get("url", "")

            episodes.append({
                "guid": guid,
                "title": ep_title,
                "published": published,
                "audio_url": audio_url,
                "duration": _parse_duration(entry.get("itunes_duration")),
            })
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise FeedError(f"could not read {url}: {exc}") from exc

    return {
        "title": title,
        "author": author,
        "artwork_url": artwork_url,
        "episodes": episodes,
    }
