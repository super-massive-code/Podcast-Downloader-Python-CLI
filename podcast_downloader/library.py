"""Subscribe and unsubscribe operations shared by the CLI and the interactive app."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple

from podcast_downloader.downloader import sanitise
from podcast_downloader.feeds import FeedError, parse_feed


class LibraryError(Exception):
    """A subscribe/unsubscribe request that failed for a reportable reason."""


class Subscription(NamedTuple):
    feed_id: int
    title: str
    author: str
    episode_count: int


class Refresh(NamedTuple):
    feed_id: int
    title: str
    new_episode_ids: list[int]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def folder_name_for(title: str) -> str:
    """Folder name used for a show's downloads.

    Sanitised the same way as episode filenames: a feed title comes from
    whatever RSS feed the user subscribes to, so a hostile or careless
    "../../.." title must not turn into a path that escapes download_dir.
    """
    return sanitise(title).lower().replace(" ", "-").replace("_", "-")


def _fetch(url: str) -> dict[str, Any]:
    try:
        return parse_feed(url)
    except FeedError as exc:
        raise LibraryError(f"Could not load feed: {exc}") from exc


def _unique_folder_name(store, title: str) -> str:
    """A folder no other show uses, so two shows' episodes never mix in one folder."""
    taken = {feed.get("folder_name") for feed in store.read()["feeds"]}
    base = candidate = folder_name_for(title)
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def subscribe(store, url: str) -> Subscription:
    """Parse a feed and add it, with its episodes, to the library.

    Raises LibraryError if the feed is already subscribed or cannot be loaded.
    """
    if store.exists_by_feed_url(url):
        raise LibraryError(f"'{url}' is already subscribed.")

    feed_data = _fetch(url)
    title = feed_data["title"]
    author = feed_data.get("author", "")
    episodes = feed_data.get("episodes", [])

    feed_id = store.add_feed(
        url=url,
        title=title,
        author=author,
        artwork_url=feed_data.get("artwork_url", ""),
        folder_name=_unique_folder_name(store, title),
    )
    added = store.add_episodes(feed_id, episodes)
    store.update_feed_last_refreshed(feed_id, _now())
    return Subscription(feed_id, title, author, len(added))


def unsubscribe(store, feed_id: int) -> dict[str, Any]:
    """Remove a feed and its episodes from the library.

    Downloaded files are deliberately left on disk. Raises LibraryError if
    the feed does not exist.
    """
    feed = store.get_feed_by_id(feed_id)
    if feed is None:
        raise LibraryError(f"Feed ID {feed_id} not found.")
    store.remove_feed(feed_id)
    return feed


def refresh(store, feed_id: int) -> Refresh:
    """Re-fetch a feed and add any episodes we have not seen before.

    Episodes are matched on guid within this feed, so a guid another show
    happens to reuse will not mask a new episode. Raises LibraryError if the
    feed does not exist or cannot be loaded.
    """
    feed = store.get_feed_by_id(feed_id)
    if feed is None:
        raise LibraryError(f"Feed ID {feed_id} not found.")

    feed_data = _fetch(feed["url"])
    new_ids = store.add_episodes(feed_id, feed_data.get("episodes", []))
    store.update_feed_last_refreshed(feed_id, _now())
    return Refresh(feed_id, feed["title"], new_ids)


def latest_episodes(store, feed_id: int, count: int) -> list[dict[str, Any]]:
    """The newest `count` episodes of a feed, newest first.

    Undated episodes sort last rather than being exempt from the count, so a
    feed with unparseable dates can never pull down more than `count`.
    """
    episodes = store.get_episodes_by_feed_id(feed_id)
    episodes.sort(key=lambda e: ((e.get("published") or ""), e["id"]), reverse=True)
    return episodes[:count]


def episodes_to_fetch(store, feed_id: int, count: int) -> list[dict[str, Any]]:
    """Which of a feed's newest `count` episodes are not downloaded yet."""
    return [ep for ep in latest_episodes(store, feed_id, count) if ep["status"] != "done"]
