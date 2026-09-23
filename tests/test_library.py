"""Tests for library.py - subscribe/unsubscribe shared by the CLI and the TUI."""

from unittest.mock import patch

import pytest

from podcast_downloader.library import (
    LibraryError, folder_name_for, latest_episodes, refresh, subscribe, unsubscribe,
)
from podcast_downloader.feeds import FeedError
from podcast_downloader.store import LibraryStore


FEED = {
    "title": "Darknet Diaries",
    "author": "Jack Rhysider",
    "artwork_url": "http://art",
    "last_refreshed": "2026-01-01T00:00:00Z",
    "episodes": [
        {"guid": "g1", "title": "Ep 1", "audio_url": "http://a/1.mp3",
         "published": "2026-01-02", "duration": 60},
        {"guid": "g2", "title": "Ep 2", "audio_url": "http://a/2.mp3",
         "published": "2026-01-01", "duration": 90},
    ],
}


@pytest.fixture
def store(tmp_path):
    # Never the real ~/Podcasts.
    store = LibraryStore(str(tmp_path / "library.json"))
    store.update_config(download_dir=str(tmp_path / "Podcasts"))
    return store


class TestFolderNameFor:
    def test_lowercases_and_hyphenates(self):
        assert folder_name_for("The Rest Is History") == "the-rest-is-history"

    def test_replaces_underscores(self):
        assert folder_name_for("a_b c") == "a-b-c"

    def test_strips_path_separators_from_a_hostile_title(self):
        # A feed's title is attacker-controlled (whatever the RSS server
        # sends), so it must never be able to escape download_dir.
        assert "/" not in folder_name_for("../../../../etc")
        assert ".." not in folder_name_for("../../../../etc")

    def test_falls_back_when_title_is_nothing_but_separators(self):
        assert folder_name_for("../..") == "untitled"


class TestSubscribe:
    def test_adds_the_feed_and_its_episodes(self, store):
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            result = subscribe(store, "http://dd/rss")

        data = store.read()
        assert result.title == "Darknet Diaries"
        assert result.episode_count == 2
        assert [f["title"] for f in data["feeds"]] == ["Darknet Diaries"]
        assert len(data["episodes"]) == 2

    def test_records_the_feed_url_and_folder(self, store):
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            subscribe(store, "http://dd/rss")
        feed = store.read()["feeds"][0]
        assert feed["url"] == "http://dd/rss"
        assert feed["folder_name"] == "darknet-diaries"

    def test_rejects_a_duplicate(self, store):
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            subscribe(store, "http://dd/rss")
            with pytest.raises(LibraryError, match="already subscribed"):
                subscribe(store, "http://dd/rss")

    def test_rejects_a_feed_that_will_not_load_and_says_why(self, store):
        with patch("podcast_downloader.library.parse_feed", side_effect=FeedError("HTTP 404 fetching http://nope/rss")):
            with pytest.raises(LibraryError, match="Could not load feed: HTTP 404"):
                subscribe(store, "http://nope/rss")

    def test_does_not_add_anything_when_parsing_fails(self, store):
        with patch("podcast_downloader.library.parse_feed", side_effect=FeedError("bad")):
            with pytest.raises(LibraryError):
                subscribe(store, "http://nope/rss")
        assert store.read()["feeds"] == []

    def test_handles_a_feed_with_no_episodes(self, store):
        with patch("podcast_downloader.library.parse_feed", return_value={**FEED, "episodes": []}):
            result = subscribe(store, "http://dd/rss")
        assert result.episode_count == 0
        assert store.read()["episodes"] == []

    def test_shows_with_the_same_title_get_separate_folders(self, store):
        # Otherwise their episodes would be mixed together in one folder.
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            subscribe(store, "http://dd/rss")
            subscribe(store, "http://mirror/rss")
            subscribe(store, "http://another-mirror/rss")
        folders = [feed["folder_name"] for feed in store.read()["feeds"]]
        assert folders == ["darknet-diaries", "darknet-diaries-2", "darknet-diaries-3"]

    def test_writes_the_library_once_for_all_episodes(self, store):
        many = {**FEED, "episodes": [
            {"guid": f"g{i}", "title": f"Ep {i}", "audio_url": f"http://a/{i}.mp3"}
            for i in range(50)
        ]}
        with patch("podcast_downloader.library.parse_feed", return_value=many), \
             patch.object(store, "write", wraps=store.write) as write:
            subscribe(store, "http://dd/rss")
        assert write.call_count < 5
        assert len(store.read()["episodes"]) == 50


class TestUnsubscribe:
    def _subscribe(self, store):
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            return subscribe(store, "http://dd/rss")

    def test_removes_the_feed_and_its_episodes(self, store):
        result = self._subscribe(store)
        unsubscribe(store, result.feed_id)
        data = store.read()
        assert data["feeds"] == []
        assert data["episodes"] == []

    def test_returns_the_removed_feed(self, store):
        result = self._subscribe(store)
        feed = unsubscribe(store, result.feed_id)
        assert feed["title"] == "Darknet Diaries"

    def test_rejects_an_unknown_feed(self, store):
        with pytest.raises(LibraryError, match="not found"):
            unsubscribe(store, 999)

    def test_leaves_downloaded_files_on_disk(self, store, tmp_path):
        result = self._subscribe(store)
        data = store.read()
        data["config"]["download_dir"] = str(tmp_path)
        store.write(data)
        folder = tmp_path / "darknet-diaries"
        folder.mkdir()
        (folder / "ep.mp3").write_bytes(b"audio")

        unsubscribe(store, result.feed_id)
        assert (folder / "ep.mp3").exists()



def _subscribed(store, feed=FEED):
    with patch("podcast_downloader.library.parse_feed", return_value=feed):
        return subscribe(store, "http://dd/rss").feed_id


class TestRefresh:
    def test_adds_only_episodes_we_have_not_seen(self, store):
        feed_id = _subscribed(store)
        grown = dict(FEED, episodes=FEED["episodes"] + [
            {"guid": "g3", "title": "Ep 3", "audio_url": "http://a/3.mp3",
             "published": "2026-01-03", "duration": 30},
        ])
        with patch("podcast_downloader.library.parse_feed", return_value=grown):
            result = refresh(store, feed_id)
        assert len(result.new_episode_ids) == 1
        assert [e["title"] for e in store.get_episodes_by_feed_id(feed_id)] == ["Ep 1", "Ep 2", "Ep 3"]

    def test_is_a_no_op_when_nothing_is_new(self, store):
        feed_id = _subscribed(store)
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            result = refresh(store, feed_id)
        assert result.new_episode_ids == []
        assert len(store.get_episodes_by_feed_id(feed_id)) == 2

    def test_does_not_duplicate_a_guid_repeated_within_one_feed(self, store):
        feed_id = _subscribed(store)
        dupes = dict(FEED, episodes=[
            {"guid": "g9", "title": "New", "audio_url": "http://a/9.mp3", "published": "2026-02-01"},
            {"guid": "g9", "title": "New again", "audio_url": "http://a/9.mp3", "published": "2026-02-01"},
        ])
        with patch("podcast_downloader.library.parse_feed", return_value=dupes):
            result = refresh(store, feed_id)
        assert len(result.new_episode_ids) == 1

    def test_a_guid_used_by_another_feed_does_not_mask_a_new_episode(self, store):
        first = _subscribed(store)
        other = dict(FEED, title="Other Show", episodes=[
            {"guid": "g1", "title": "Their Ep 1", "audio_url": "http://b/1.mp3", "published": "2026-01-02"},
        ])
        with patch("podcast_downloader.library.parse_feed", return_value=other):
            second = subscribe(store, "http://other/rss").feed_id
        assert len(store.get_episodes_by_feed_id(second)) == 1
        assert len(store.get_episodes_by_feed_id(first)) == 2

    def test_records_a_real_refresh_timestamp(self, store):
        feed_id = _subscribed(store)
        with patch("podcast_downloader.library.parse_feed", return_value=FEED):
            refresh(store, feed_id)
        assert store.get_feed_by_id(feed_id)["last_refreshed"] is not None

    def test_rejects_an_unknown_feed(self, store):
        with pytest.raises(LibraryError):
            refresh(store, 999)

    def test_rejects_a_feed_that_will_not_parse(self, store):
        feed_id = _subscribed(store)
        with patch("podcast_downloader.library.parse_feed", side_effect=FeedError("offline")):
            with pytest.raises(LibraryError, match="offline"):
                refresh(store, feed_id)


class TestLatestEpisodes:
    def test_returns_the_newest_first(self, store):
        feed_id = _subscribed(store)
        assert [e["title"] for e in latest_episodes(store, feed_id, 2)] == ["Ep 1", "Ep 2"]

    def test_caps_at_the_requested_count(self, store):
        feed_id = _subscribed(store)
        assert len(latest_episodes(store, feed_id, 1)) == 1

    def test_returns_everything_when_the_feed_is_shorter(self, store):
        feed_id = _subscribed(store)
        assert len(latest_episodes(store, feed_id, 50)) == 2

    def test_undated_episodes_sort_last_and_still_count(self, store):
        undated = dict(FEED, episodes=[
            {"guid": "u1", "title": "No date", "audio_url": "http://a/u.mp3", "published": None},
            {"guid": "d1", "title": "Dated", "audio_url": "http://a/d.mp3", "published": "2026-01-01"},
        ])
        feed_id = _subscribed(store, undated)
        assert [e["title"] for e in latest_episodes(store, feed_id, 1)] == ["Dated"]
