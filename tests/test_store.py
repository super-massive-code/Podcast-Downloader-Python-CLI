"""Tests for store.py - JSON library store."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from podcast_downloader.store import LibraryStore


class TestLibraryStore:
    @pytest.fixture
    def store_path(self, tmp_path):
        return str(tmp_path / "library.json")

    @pytest.fixture
    def store(self, store_path):
        return LibraryStore(store_path)

    def test_create_new_store_creates_file(self, store):
        assert os.path.exists(store.path)

    def test_create_new_store_has_default_config(self, store):
        data = store.read()
        assert "config" in data
        assert data["config"]["default_keep_latest"] == 3
        assert data["config"]["download_dir"] == os.path.expanduser("~/Podcasts")
        assert data["feeds"] == []
        assert data["episodes"] == []

    def test_load_existing_store(self, store):
        store.write({
            "config": {
                "download_dir": "/custom/path",
                "default_policy": "all",
                "default_keep_latest": 5,
            },
            "feeds": [
                {
                    "id": 1,
                    "url": "https://example.com/feed.xml",
                    "title": "Test Show",
                    "author": "Test Author",
                    "artwork_url": "https://example.com/art.jpg",
                    "folder_name": "test-show",
                    "policy": "all",
                    "keep_latest": 5,
                    "added_at": "2026-09-21T00:00:00",
                    "last_refreshed": "2026-09-21T00:00:00",
                }
            ],
            "episodes": [
                {
                    "id": 1,
                    "feed_id": 1,
                    "guid": "test-guid-1",
                    "title": "Episode 1",
                    "published": "2026-09-21",
                    "audio_url": "https://example.com/ep1.mp3",
                    "duration": 1800,
                    "status": "done",
                    "file_path": "/custom/path/test-show/2026-09-21 - Episode 1.mp3",
                    "error": None,
                }
            ],
        })

        loaded = LibraryStore(store.path)
        data = loaded.read()
        assert len(data["feeds"]) == 1
        assert data["feeds"][0]["title"] == "Test Show"
        assert len(data["episodes"]) == 1
        assert data["episodes"][0]["title"] == "Episode 1"

    def test_add_feed(self, store):
        feed_id = store.add_feed(
            url="https://example.com/feed.xml",
            title="Test Show",
            author="Test Author",
            artwork_url="https://example.com/art.jpg",
            folder_name="test-show",
        )
        assert feed_id == 1
        data = store.read()
        assert len(data["feeds"]) == 1
        assert data["feeds"][0]["id"] == 1
        assert data["feeds"][0]["url"] == "https://example.com/feed.xml"
        assert "policy" not in data["feeds"][0]
        assert "keep_latest" not in data["feeds"][0]

    def test_add_feed_generates_incremental_ids(self, store):
        id1 = store.add_feed(url="https://example.com/feed1.xml", title="Show 1", folder_name="show-1")
        id2 = store.add_feed(url="https://example.com/feed2.xml", title="Show 2", folder_name="show-2")
        assert id1 == 1
        assert id2 == 2

    def test_add_episode(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        ep_id = store.add_episode(
            feed_id=feed_id,
            guid="ep-1",
            title="Episode 1",
            published="2026-09-21",
            audio_url="https://example.com/ep1.mp3",
            duration=1800,
        )
        assert ep_id == 1
        data = store.read()
        assert len(data["episodes"]) == 1
        assert data["episodes"][0]["guid"] == "ep-1"
        assert data["episodes"][0]["status"] == "pending"

    def test_get_feed_by_id(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        feed = store.get_feed_by_id(feed_id)
        assert feed is not None
        assert feed["id"] == feed_id
        assert feed["title"] == "Show"

    def test_get_feed_by_id_not_found(self, store):
        feed = store.get_feed_by_id(999)
        assert feed is None

    def test_get_episodes_by_feed_id(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        store.add_episode(feed_id=feed_id, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")
        store.add_episode(feed_id=feed_id, guid="ep-2", title="E2", audio_url="https://example.com/ep2.mp3")

        episodes = store.get_episodes_by_feed_id(feed_id)
        assert len(episodes) == 2

    def test_get_episodes_by_feed_id_wrong_feed(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        store.add_episode(feed_id=feed_id, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")

        episodes = store.get_episodes_by_feed_id(999)
        assert len(episodes) == 0

    def test_update_episode_status(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        ep_id = store.add_episode(feed_id=feed_id, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")

        store.update_episode_status(ep_id, status="done", file_path="/path/to/ep.mp3")
        data = store.read()
        ep = data["episodes"][0]
        assert ep["status"] == "done"
        assert ep["file_path"] == "/path/to/ep.mp3"

    def test_update_episode_status_with_error(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        ep_id = store.add_episode(feed_id=feed_id, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")

        store.update_episode_status(ep_id, status="error", error="Network error")
        data = store.read()
        ep = data["episodes"][0]
        assert ep["status"] == "error"
        assert ep["error"] == "Network error"

    def test_remove_feed(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        store.add_episode(feed_id=feed_id, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")

        store.remove_feed(feed_id)
        data = store.read()
        assert len(data["feeds"]) == 0
        assert len(data["episodes"]) == 0

    def test_remove_feed_keeps_other_feeds(self, store):
        feed_id1 = store.add_feed(url="https://example.com/feed1.xml", title="Show 1", folder_name="show-1")
        feed_id2 = store.add_feed(url="https://example.com/feed2.xml", title="Show 2", folder_name="show-2")
        store.add_episode(feed_id=feed_id1, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")
        store.add_episode(feed_id=feed_id2, guid="ep-2", title="E2", audio_url="https://example.com/ep2.mp3")

        store.remove_feed(feed_id1)
        data = store.read()
        assert len(data["feeds"]) == 1
        assert data["feeds"][0]["id"] == feed_id2
        assert len(data["episodes"]) == 1
        assert data["episodes"][0]["feed_id"] == feed_id2

    def test_update_feed_last_refreshed(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        store.update_feed_last_refreshed(feed_id, "2026-09-21T12:00:00")
        data = store.read()
        assert data["feeds"][0]["last_refreshed"] == "2026-09-21T12:00:00"

    def test_update_config(self, store):
        store.update_config(download_dir="/custom/path", default_keep_latest=10)
        data = store.read()
        assert data["config"]["download_dir"] == "/custom/path"
        assert data["config"]["default_keep_latest"] == 10

    def test_get_episode_by_id(self, store):
        feed_id = store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        ep_id = store.add_episode(feed_id=feed_id, guid="ep-1", title="E1", audio_url="https://example.com/ep1.mp3")

        ep = store.get_episode_by_id(ep_id)
        assert ep is not None
        assert ep["id"] == ep_id

    def test_get_episode_by_id_not_found(self, store):
        ep = store.get_episode_by_id(999)
        assert ep is None

    def test_exists_by_feed_url(self, store):
        result = store.exists_by_feed_url("https://example.com/feed.xml")
        assert result is False

        store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        result = store.exists_by_feed_url("https://example.com/feed.xml")
        assert result is True

    def test_get_feed_by_url(self, store):
        store.add_feed(url="https://example.com/feed.xml", title="Show", folder_name="show")
        feed = store.get_feed_by_url("https://example.com/feed.xml")
        assert feed is not None
        assert feed["title"] == "Show"

    def test_get_feed_by_url_not_found(self, store):
        feed = store.get_feed_by_url("https://example.com/nonexistent.xml")
        assert feed is None


class TestConcurrentWrites:
    """Every mutator is a read-modify-write of the whole document, so two of
    them interleaving must not lose one of the writes."""

    @pytest.fixture
    def store_path(self, tmp_path):
        return str(tmp_path / "library.json")

    def test_concurrent_add_episode_keeps_every_episode(self, store_path):
        store = LibraryStore(store_path)
        feed_id = store.add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")

        errors = []

        def add(n):
            try:
                store.add_episode(
                    feed_id=feed_id, guid=f"ep-{n}", title=f"E{n}",
                    audio_url=f"https://example.com/{n}.mp3",
                )
            except Exception as exc:  # pragma: no cover - a failure is the assertion
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(n,)) for n in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        episodes = store.get_episodes_by_feed_id(feed_id)
        assert len(episodes) == 25, "a concurrent write was lost"
        assert len({ep["id"] for ep in episodes}) == 25, "duplicate episode ids minted"

    def test_separate_instances_over_one_file_share_a_lock(self, store_path):
        """cli._get_store() returns a fresh LibraryStore every call, so the
        lock cannot live on the instance."""
        assert LibraryStore(store_path)._lock is LibraryStore(store_path)._lock

    def test_add_episode_does_not_lose_a_concurrent_status_update(self, store_path):
        """The refresh worker's add_episode loop runs alongside the download
        worker marking episodes done."""
        store = LibraryStore(store_path)
        feed_id = store.add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")
        existing = store.add_episode(
            feed_id=feed_id, guid="old", title="Old", audio_url="https://example.com/old.mp3",
        )

        def add_many():
            for n in range(15):
                store.add_episode(
                    feed_id=feed_id, guid=f"new-{n}", title=f"N{n}",
                    audio_url=f"https://example.com/{n}.mp3",
                )

        def mark_done():
            for _ in range(15):
                store.update_episode_status(existing, status="done", file_path="/tmp/old.mp3")

        adder, marker = threading.Thread(target=add_many), threading.Thread(target=mark_done)
        adder.start()
        marker.start()
        adder.join()
        marker.join()

        assert store.get_episode_by_id(existing)["status"] == "done", "status was resurrected"
        assert len(store.get_episodes_by_feed_id(feed_id)) == 16


class TestCrossProcessLock:
    """A cron-driven `podcast download-latest` and the open app are separate processes."""

    WRITER = """
import sys
from podcast_downloader.store import LibraryStore
store = LibraryStore(sys.argv[1])
for i in range(40):
    store.add_episode(feed_id=1, guid=f"{sys.argv[2]}-{i}", title="t", audio_url="u")
"""

    def test_separate_processes_do_not_lose_each_others_writes(self, tmp_path):
        path = str(tmp_path / "library.json")
        LibraryStore(path).add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")
        repo_root = Path(__file__).resolve().parents[1]
        writers = [
            subprocess.Popen([sys.executable, "-c", self.WRITER, path, tag], cwd=repo_root)
            for tag in ("a", "b", "c")
        ]
        for writer in writers:
            assert writer.wait(timeout=120) == 0
        episodes = LibraryStore(path).read()["episodes"]
        assert len(episodes) == 120, "a write from another process was lost"
        assert len({ep["id"] for ep in episodes}) == 120

    def test_racing_refreshes_cannot_add_the_same_episode_twice(self, tmp_path):
        store = LibraryStore(str(tmp_path / "library.json"))
        feed_id = store.add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")
        new = [{"guid": "g1", "title": "Ep", "audio_url": "u"}]
        assert len(store.add_episodes(feed_id, new)) == 1
        assert store.add_episodes(feed_id, new) == []
        assert len(store.read()["episodes"]) == 1

    def test_update_file_paths_changes_only_the_paths(self, tmp_path):
        store = LibraryStore(str(tmp_path / "library.json"))
        feed_id = store.add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")
        ep_id = store.add_episode(feed_id=feed_id, guid="g", title="Ep", audio_url="u")
        store.update_episode_status(ep_id, status="done", file_path="/old/ep.mp3")
        store.update_file_paths({ep_id: "/new/ep.mp3"})
        episode = store.get_episode_by_id(ep_id)
        assert episode["file_path"] == "/new/ep.mp3"
        assert episode["status"] == "done"


class TestAtomicWrite:
    @pytest.fixture
    def store_path(self, tmp_path):
        return str(tmp_path / "library.json")

    def test_failed_write_leaves_the_previous_file_intact(self, store_path):
        store = LibraryStore(store_path)
        store.add_feed(url="https://example.com/f.xml", title="Keep Me", folder_name="keep-me")
        before = Path(store_path).read_text(encoding="utf-8")

        class Unserialisable:
            pass

        with pytest.raises(TypeError):
            store.write({"config": {}, "feeds": [], "episodes": [Unserialisable()]})

        assert Path(store_path).read_text(encoding="utf-8") == before
        assert store.get_feed_by_url("https://example.com/f.xml")["title"] == "Keep Me"

    def test_failed_write_leaves_no_temp_file_behind(self, store_path):
        store = LibraryStore(store_path)

        class Unserialisable:
            pass

        with pytest.raises(TypeError):
            store.write({"config": {}, "feeds": [], "episodes": [Unserialisable()]})

        leftovers = list(Path(store_path).parent.glob(Path(store_path).name + ".*.tmp"))
        assert leftovers == [], f"temp files left behind: {leftovers}"

    def test_write_preserves_the_file_mode(self, store_path):
        store = LibraryStore(store_path)
        os.chmod(store_path, 0o640)
        store.add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")
        assert os.stat(store_path).st_mode & 0o777 == 0o640

    def test_a_reader_never_sees_a_torn_file(self, store_path):
        """os.replace swaps the inode, so a concurrent read gets either the
        whole old document or the whole new one - never half of either."""
        store = LibraryStore(store_path)
        feed_id = store.add_feed(url="https://example.com/f.xml", title="Show", folder_name="show")
        for n in range(40):
            store.add_episode(
                feed_id=feed_id, guid=f"ep-{n}", title="E" * 200,
                audio_url=f"https://example.com/{n}.mp3",
            )

        failures = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    json.loads(Path(store_path).read_text(encoding="utf-8"))
                except Exception as exc:
                    failures.append(exc)
                    return

        watcher = threading.Thread(target=reader)
        watcher.start()
        try:
            for n in range(40, 80):
                store.add_episode(
                    feed_id=feed_id, guid=f"ep-{n}", title="E" * 200,
                    audio_url=f"https://example.com/{n}.mp3",
                )
        finally:
            stop.set()
            watcher.join()

        assert failures == [], f"reader saw a torn file: {failures[:1]}"
