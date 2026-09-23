"""JSON library store for podcast management."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from podcast_downloader.library import folder_name_for


DEFAULT_CONFIG = {
    "download_dir": os.path.expanduser("~/Podcasts"),
    "default_keep_latest": 3,
}


class _LibraryLock:
    """Exclusive access to one library file, across threads and processes.

    Each mutator below is a read-modify-write of the whole document, so two
    of them interleaving loses whichever write lands first. The app's download
    and refresh threads contend for it, and so does a cron-driven
    `podcast download-latest` running while the app is open.

    Re-entrant (mutators hold it while calling read() and write(), which take
    it again). The flock is taken only at the outermost level: flock locks
    belong to an open file, so a second open-and-lock from the same process
    would deadlock against the first.
    """

    def __init__(self, library_path: str):
        self._path = library_path + ".lock"
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def __enter__(self) -> "_LibraryLock":
        self._thread_lock.acquire()
        try:
            if self._depth == 0:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._thread_lock.release()


# One lock per library file, shared by every LibraryStore pointing at it:
# cli._get_store() mints a fresh LibraryStore on every call. Keyed by realpath
# so two genuinely different libraries - every test fixture has its own -
# never serialise against each other.
_PATH_LOCKS: dict[str, _LibraryLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def expand_dir(path: str) -> str:
    """Absolute form of a user-typed directory, so "~/Pods" never means "./~/Pods"."""
    return os.path.abspath(os.path.expanduser(path.strip()))


def _lock_for(path: str) -> _LibraryLock:
    key = os.path.realpath(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = _LibraryLock(key)
        return lock


class LibraryStore:
    """Manages podcast library stored as a single JSON file."""

    def __init__(self, path: str | None = None):
        if path is None:
            path = os.path.join(os.path.expanduser("~"), "Podcasts", "library.json")
        self.path = path
        self._lock = _lock_for(self.path)
        self._data: dict[str, Any] = {
            "config": dict(DEFAULT_CONFIG),
            "feeds": [],
            "episodes": [],
        }
        self._ensure_file()

    def _ensure_file(self) -> None:
        with self._lock:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            if not os.path.exists(self.path):
                self.write(self._data)

    def read(self) -> dict[str, Any]:
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return dict(self._data)
                return json.loads(content)

    def write(self, data: dict[str, Any]) -> None:
        """Replace the library file atomically.

        The browser's background workers are daemon threads, killed outright
        when the app quits, so an in-place truncating write can leave the
        library unparseable. Writing a sibling temp file and renaming it over
        the target mirrors the .part + os.replace idiom in downloader.py.

        No fsync: the failure being defended against is a thread dying, and
        os.replace covers that completely - process death never loses the page
        cache. fsync would only add power-loss durability, at the cost of one
        sync per write (hundreds during a large subscribe).
        """
        target = Path(self.path)
        with self._lock:
            # Must run before mkstemp, which needs the directory to exist.
            target.parent.mkdir(parents=True, exist_ok=True)
            # dir= is load-bearing: os.replace is only atomic within one
            # filesystem, and the library may live on a mounted volume.
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent), prefix=target.name + ".", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                if target.exists():
                    # mkstemp creates 0600; keep whatever mode the library had.
                    os.chmod(tmp_path, target.stat().st_mode & 0o777)
                os.replace(tmp_path, self.path)
            except BaseException:
                # BaseException, not Exception: a KeyboardInterrupt at quit is
                # exactly when a stray .tmp must not be left behind.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def _next_feed_id(self, data: dict[str, Any]) -> int:
        if not data["feeds"]:
            return 1
        return max(f["id"] for f in data["feeds"]) + 1

    def _next_episode_id(self, data: dict[str, Any]) -> int:
        if not data["episodes"]:
            return 1
        return max(e["id"] for e in data["episodes"]) + 1

    def add_feed(
        self,
        url: str,
        title: str,
        author: str | None = None,
        artwork_url: str | None = None,
        folder_name: str | None = None,
    ) -> int:
        with self._lock:
            data = self.read()
            feed_id = self._next_feed_id(data)
            now = datetime.now(timezone.utc).isoformat()
            feed = {
                "id": feed_id,
                "url": url,
                "title": title,
                "author": author or "",
                "artwork_url": artwork_url or "",
                "folder_name": folder_name or folder_name_for(title),
                "added_at": now,
                "last_refreshed": now,
            }
            data["feeds"].append(feed)
            self.write(data)
            return feed_id

    def add_episode(
        self,
        feed_id: int,
        guid: str,
        title: str,
        audio_url: str,
        published: str | None = None,
        duration: int | None = None,
    ) -> int:
        with self._lock:
            data = self.read()
            ep_id = self._next_episode_id(data)
            episode = {
                "id": ep_id,
                "feed_id": feed_id,
                "guid": guid,
                "title": title,
                "published": published,
                "audio_url": audio_url,
                "duration": duration,
                "status": "pending",
                "file_path": None,
                "error": None,
            }
            data["episodes"].append(episode)
            self.write(data)
            return ep_id

    def add_episodes(self, feed_id: int, episodes: list[dict[str, Any]]) -> list[int]:
        """Add the episodes this feed does not already have, in one read-modify-write.

        One write per episode made subscribing to a feed with a long back
        catalogue quadratic: every write re-serialises the whole library.
        Episodes are matched on guid within the feed, and the check happens
        under the lock, so two refreshes racing (the app and a cron job) can't
        both add the same new episode.
        """
        with self._lock:
            data = self.read()
            seen = {ep["guid"] for ep in data["episodes"] if ep["feed_id"] == feed_id}
            next_id = self._next_episode_id(data)
            ids: list[int] = []
            for episode in episodes:
                if episode["guid"] in seen:
                    continue
                seen.add(episode["guid"])
                data["episodes"].append({
                    "id": next_id,
                    "feed_id": feed_id,
                    "guid": episode["guid"],
                    "title": episode["title"],
                    "published": episode.get("published"),
                    "audio_url": episode["audio_url"],
                    "duration": episode.get("duration"),
                    "status": "pending",
                    "file_path": None,
                    "error": None,
                })
                ids.append(next_id)
                next_id += 1
            if ids:
                self.write(data)
            return ids

    def get_feed_by_id(self, feed_id: int) -> dict[str, Any] | None:
        data = self.read()
        for feed in data["feeds"]:
            if feed["id"] == feed_id:
                return feed
        return None

    def get_feed_by_url(self, url: str) -> dict[str, Any] | None:
        data = self.read()
        for feed in data["feeds"]:
            if feed["url"] == url:
                return feed
        return None

    def exists_by_feed_url(self, url: str) -> bool:
        return self.get_feed_by_url(url) is not None

    def get_episodes_by_feed_id(self, feed_id: int) -> list[dict[str, Any]]:
        data = self.read()
        return [ep for ep in data["episodes"] if ep["feed_id"] == feed_id]

    def get_episode_by_id(self, episode_id: int) -> dict[str, Any] | None:
        data = self.read()
        for ep in data["episodes"]:
            if ep["id"] == episode_id:
                return ep
        return None

    def update_episode_status(
        self,
        episode_id: int,
        status: str = "pending",
        file_path: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            data = self.read()
            for ep in data["episodes"]:
                if ep["id"] == episode_id:
                    ep["status"] = status
                    if file_path is not None:
                        ep["file_path"] = file_path
                    if error is not None:
                        ep["error"] = error
                    break
            self.write(data)

    def update_feed_last_refreshed(self, feed_id: int, timestamp: str) -> None:
        with self._lock:
            data = self.read()
            for feed in data["feeds"]:
                if feed["id"] == feed_id:
                    feed["last_refreshed"] = timestamp
                    break
            self.write(data)

    def update_file_paths(self, paths: dict[int, str]) -> None:
        """Point episodes at moved files, touching nothing else."""
        with self._lock:
            data = self.read()
            for ep in data["episodes"]:
                if ep["id"] in paths:
                    ep["file_path"] = paths[ep["id"]]
            self.write(data)

    def update_config(
        self,
        download_dir: str | None = None,
        default_keep_latest: int | None = None,
    ) -> None:
        with self._lock:
            data = self.read()
            if download_dir is not None:
                data["config"]["download_dir"] = expand_dir(download_dir)
            if default_keep_latest is not None:
                data["config"]["default_keep_latest"] = default_keep_latest
            self.write(data)

    def remove_feed(self, feed_id: int) -> None:
        with self._lock:
            data = self.read()
            data["feeds"] = [f for f in data["feeds"] if f["id"] != feed_id]
            data["episodes"] = [ep for ep in data["episodes"] if ep["feed_id"] != feed_id]
            self.write(data)
