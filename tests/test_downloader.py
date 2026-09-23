"""Tests for downloader.py - sanitisation, naming, and download."""

import os
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from podcast_downloader.downloader import (
    _artwork,
    _download_artwork,
    sanitise,
    episode_filename,
    download_episode,
    tag_episode,
)



def _stream_response(status_code=200, chunks=(b"fake audio data",), headers=None):
    """Mock for httpx.stream(...): a context manager yielding a streaming response."""
    response = Mock()
    response.status_code = status_code
    response.iter_bytes.return_value = list(chunks)
    response.headers = headers if headers is not None else {"content-type": "audio/mpeg"}
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = False
    return ctx


class TestSanitise:
    def test_strips_slash(self):
        assert "/" not in sanitise("AC/DC: The Story")

    def test_strips_colon(self):
        assert ":" not in sanitise("AC/DC: The Story")

    def test_strips_reserved_chars(self):
        result = sanitise("File? Name * Here | \" < > %")
        assert all(c not in result for c in "?*|\"<>%")

    def test_strips_control_chars(self):
        result = sanitise("Normal\x00Name\x07With\x1fControls")
        assert "\x00" not in result and "\x07" not in result and "\x1f" not in result

    def test_collapse_whitespace(self):
        assert sanitise("too   many    spaces") == "too many spaces"

    def test_strips_trailing_whitespace(self):
        assert sanitise("Episode 12 ") == "Episode 12"

    def test_strips_leading_trailing_dots(self):
        assert sanitise("...Episode...") == "Episode"

    def test_empty_result_becomes_untitled(self):
        assert sanitise("...") == "Untitled"

    def test_truncates_to_255_bytes(self):
        long_name = "A" * 300
        result = sanitise(long_name)
        assert len(result.encode("utf-8")) <= 255

    def test_emoji_truncated_by_bytes_not_chars(self):
        emoji_title = "🎙️" * 100 + " Episode"
        result = sanitise(emoji_title)
        assert len(result.encode("utf-8")) <= 255
        assert "🎙️" in result  # should still have some emoji

    def test_preserves_unicode_characters(self):
        result = sanitise("Café: Résumé")
        assert "Café" in result and "Résumé" in result

    def test_normal_string_unchanged(self):
        assert sanitise("2026-09-21 - Great Episode Title") == "2026-09-21 - Great Episode Title"


class TestEpisodeFilename:
    def _make_episode(self, **kwargs):
        defaults = {
            "title": "Great Episode",
            "published": "2026-09-21",
            "audio_url": "https://example.com/episode.mp3",
        }
        defaults.update(kwargs)
        return defaults

    def _make_feed(self, **kwargs):
        defaults = {
            "folder_name": "test-show",
            "title": "Test Show",
        }
        defaults.update(kwargs)
        return defaults

    def test_title_leads_and_date_is_appended(self):
        """The filename should match the episode name, with the date at the end."""
        ep = self._make_episode()
        feed = self._make_feed()
        result = episode_filename(ep, feed)
        assert result == "Great Episode - 2026-09-21.mp3"

    def test_without_date(self):
        ep = self._make_episode(published=None)
        feed = self._make_feed()
        result = episode_filename(ep, feed)
        assert "2026-09-21" not in result
        assert "Great Episode" in result

    def test_m4a_extension(self):
        ep = self._make_episode(audio_url="https://example.com/episode.m4a")
        feed = self._make_feed()
        result = episode_filename(ep, feed)
        assert result.endswith(".m4a")

    def test_default_extension_when_none(self):
        ep = self._make_episode(audio_url="https://example.com/episode")
        feed = self._make_feed()
        result = episode_filename(ep, feed)
        assert result.endswith(".mp3")

    def test_collision_handling(self, tmp_path):
        ep = self._make_episode()
        feed = self._make_feed()
        test_dir = tmp_path / "test-show"
        test_dir.mkdir()

        # First file
        name1 = episode_filename(ep, feed)
        (test_dir / name1).touch()

        # Second file with same name should get (2)
        name2 = episode_filename(ep, feed, used_names={str(test_dir / name1)})
        assert " (2)" in name2

    def test_collision_sequence(self, tmp_path):
        ep = self._make_episode()
        feed = self._make_feed()
        test_dir = tmp_path / "test-show"
        test_dir.mkdir()

        name1 = episode_filename(ep, feed)
        (test_dir / name1).touch()

        used = {str(test_dir / name1)}
        name2 = episode_filename(ep, feed, used_names=used)
        used.add(str(test_dir / name2))

        name3 = episode_filename(ep, feed, used_names=used)
        assert " (3)" in name3


    def test_long_title_leaves_room_for_date_extension_and_part_suffix(self):
        ep = self._make_episode(title="x" * 300)
        name = episode_filename(ep, self._make_feed(), used_names={"placeholder"})
        assert len(f"{name}.part".encode("utf-8")) <= 255
        assert name.endswith(" - 2026-09-21.mp3")

    def test_long_multibyte_title_is_cut_on_a_character_boundary(self):
        ep = self._make_episode(title="é" * 300)
        name = episode_filename(ep, self._make_feed())
        assert len(f"{name}.part".encode("utf-8")) <= 255
        name.encode("utf-8").decode("utf-8")  # no split characters

    def test_explicit_extension_wins(self):
        ep = self._make_episode(audio_url="https://example.com/episode")
        assert episode_filename(ep, self._make_feed(), ext=".ogg").endswith(".ogg")


class TestDownloadEpisode:
    def _make_store(self, download_dir, episode=None, feed=None):
        store = Mock()
        store.read.return_value = {
            "config": {"download_dir": str(download_dir)},
            "feeds": [feed] if feed else [],
            "episodes": [episode] if episode else [],
        }
        return store

    def _make_episode(self, **kwargs):
        defaults = {
            "id": 1,
            "feed_id": 1,
            "guid": "test-guid",
            "title": "Test Episode",
            "published": "2026-09-21",
            "audio_url": "https://example.com/ep.mp3",
            "status": "pending",
            "file_path": None,
            "error": None,
        }
        defaults.update(kwargs)
        return defaults

    def _make_feed(self, **kwargs):
        defaults = {
            "id": 1,
            "url": "https://example.com/feed.xml",
            "title": "Test Show",
            "folder_name": "test-show",
        }
        defaults.update(kwargs)
        return defaults

    def test_download_episode_marks_done(self, tmp_path):
        ep = self._make_episode()
        feed = self._make_feed()
        store = self._make_store(tmp_path, episode=ep, feed=feed)

        stream = _stream_response(status_code=200)

        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store)

        store.update_episode_status.assert_called_once()
        call_kwargs = store.update_episode_status.call_args[1]
        assert call_kwargs["status"] == "done"
        assert call_kwargs["file_path"] is not None
        assert call_kwargs["file_path"].endswith(".mp3")

    def test_download_episode_creates_folder(self, tmp_path):
        ep = self._make_episode()
        feed = self._make_feed()
        download_dir = str(tmp_path / "Podcasts")
        store = self._make_store(tmp_path, episode=ep, feed=feed)
        store.read.return_value["config"]["download_dir"] = download_dir

        stream = _stream_response(status_code=200)

        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store)

        # Check that the show folder was created
        show_folder = Path(download_dir) / "test-show"
        assert show_folder.exists()

    def test_download_episode_handles_error(self, tmp_path):
        ep = self._make_episode()
        feed = self._make_feed()
        store = self._make_store(tmp_path, episode=ep, feed=feed)

        stream = _stream_response(status_code=404)

        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store)

        store.update_episode_status.assert_called_once()
        call_kwargs = store.update_episode_status.call_args[1]
        assert call_kwargs["status"] == "error"

    def test_download_episode_cleans_part_file_on_error(self, tmp_path):
        ep = self._make_episode()
        feed = self._make_feed()
        store = self._make_store(tmp_path, episode=ep, feed=feed)

        stream = _stream_response(status_code=500)

        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store)

        # No .part files should remain
        for root, dirs, files in os.walk(str(tmp_path)):
            for f in files:
                assert not f.endswith(".part"), f"Found leftover .part file: {f}"

class TestDownloadNaming:
    """Real files on disk, via a real store, for what the name ends up as."""

    @pytest.fixture
    def store(self, tmp_path):
        from podcast_downloader.store import LibraryStore
        store = LibraryStore(str(tmp_path / "library.json"))
        store.update_config(download_dir=str(tmp_path / "dl"))
        feed_id = store.add_feed(url="http://a/rss", title="Show", folder_name="show")
        store.add_episode(feed_id=feed_id, guid="g", title="Ep",
                          audio_url="https://cdn.example/ep.m4a", published="2026-01-01")
        return store

    def _download(self, store, content_type):
        stream = _stream_response(headers={"content-type": content_type})
        with patch("podcast_downloader.downloader.httpx.stream", return_value=stream):
            return download_episode(1, store)

    def _files(self, store):
        return sorted(os.listdir(Path(store.read()["config"]["download_dir"]) / "show"))

    @pytest.mark.parametrize("content_type", [
        "application/octet-stream", "binary/octet-stream", "audio/mpeg", "",
    ])
    def test_url_extension_beats_the_content_type(self, store, content_type):
        self._download(store, content_type)
        assert self._files(store) == ["Ep - 2026-01-01.m4a"]

    def test_content_type_parameters_are_ignored(self, store):
        data = store.read()
        data["episodes"][0]["audio_url"] = "https://cdn.example/ep"
        store.write(data)
        self._download(store, "audio/mp4; codecs=mp4a.40.2")
        assert self._files(store) == ["Ep - 2026-01-01.m4a"]

    def test_downloading_again_replaces_the_file_instead_of_duplicating_it(self, store):
        self._download(store, "audio/mp4")
        self._download(store, "audio/mp4")
        assert self._files(store) == ["Ep - 2026-01-01.m4a"]

    def test_returns_true_on_success_and_false_on_failure(self, store):
        assert self._download(store, "audio/mp4") is True
        stream = _stream_response(status_code=404)
        with patch("podcast_downloader.downloader.httpx.stream", return_value=stream):
            assert download_episode(1, store) is False


class TestTagging:
    """Title, show, author, date and artwork written into the audio file."""

    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: enough real frames for mutagen.
    MP3_BYTES = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 20
    M4A_FIXTURE = Path(__file__).parent / "fixtures" / "silence.m4a"
    ART = (b"\x89PNG\r\n\x1a\nnot-really-a-png", "image/png")
    EPISODE = {"title": "The Episode", "published": "2026-03-04"}
    FEED = {"title": "The Show", "author": "The Host", "artwork_url": "http://art/cover.png"}

    @pytest.fixture(autouse=True)
    def fresh_artwork_cache(self):
        _download_artwork.cache_clear()
        yield
        _download_artwork.cache_clear()

    def _mp3(self, tmp_path):
        path = tmp_path / "ep.mp3"
        path.write_bytes(self.MP3_BYTES)
        return str(path)

    def test_mp3_gets_title_show_author_date_and_artwork(self, tmp_path):
        path = self._mp3(tmp_path)
        with patch("podcast_downloader.downloader._artwork", return_value=self.ART):
            tag_episode(path, ".mp3", self.EPISODE, self.FEED)
        tags = ID3(path)
        assert tags["TIT2"].text == ["The Episode"]
        assert tags["TALB"].text == ["The Show"]
        assert tags["TPE1"].text == ["The Host"]
        assert str(tags["TDRC"].text[0]).startswith("2026")
        assert tags["TCON"].text == ["Podcast"]
        assert tags.getall("APIC")[0].data == self.ART[0]

    def test_mp3_keeps_artwork_the_publisher_embedded(self, tmp_path):
        path = self._mp3(tmp_path)
        audio = MP3(path)
        audio.add_tags()
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"episode art"))
        audio.save()
        with patch("podcast_downloader.downloader._artwork") as artwork:
            tag_episode(path, ".mp3", self.EPISODE, self.FEED)
        artwork.assert_not_called()
        assert [apic.data for apic in ID3(path).getall("APIC")] == [b"episode art"]

    def test_m4a_gets_tags_and_artwork(self, tmp_path):
        path = tmp_path / "ep.m4a"
        path.write_bytes(self.M4A_FIXTURE.read_bytes())
        with patch("podcast_downloader.downloader._artwork", return_value=self.ART):
            tag_episode(str(path), ".m4a", self.EPISODE, self.FEED)
        tags = MP4(str(path)).tags
        assert tags["\xa9nam"] == ["The Episode"]
        assert tags["\xa9alb"] == ["The Show"]
        assert tags["\xa9ART"] == ["The Host"]
        assert tags["\xa9day"] == ["2026-03-04"]
        assert bytes(tags["covr"][0]) == self.ART[0]

    def test_missing_artwork_still_writes_the_text_tags(self, tmp_path):
        path = self._mp3(tmp_path)
        with patch("podcast_downloader.downloader._artwork", return_value=None):
            tag_episode(path, ".mp3", self.EPISODE, self.FEED)
        tags = ID3(path)
        assert tags["TIT2"].text == ["The Episode"]
        assert tags.getall("APIC") == []

    def test_a_file_that_is_not_audio_is_left_alone(self, tmp_path):
        path = tmp_path / "ep.mp3"
        path.write_bytes(b"this is not audio")
        tag_episode(str(path), ".mp3", self.EPISODE, self.FEED)
        assert path.read_bytes() == b"this is not audio"

    def test_artwork_is_fetched_once_per_show(self):
        response = Mock(status_code=200, headers={"content-type": "image/jpeg"}, content=b"img")
        with patch("podcast_downloader.downloader.httpx.get", return_value=response) as get:
            assert _artwork("http://art/a.jpg") == (b"img", "image/jpeg")
            assert _artwork("http://art/a.jpg") == (b"img", "image/jpeg")
        assert get.call_count == 1

    def test_artwork_that_is_not_an_image_is_ignored(self):
        response = Mock(status_code=200, headers={"content-type": "text/html"}, content=b"<html>")
        with patch("podcast_downloader.downloader.httpx.get", return_value=response):
            assert _artwork("http://art/a.jpg") is None

    def test_a_download_is_tagged_before_it_appears(self, tmp_path):
        from podcast_downloader.store import LibraryStore
        store = LibraryStore(str(tmp_path / "library.json"))
        store.update_config(download_dir=str(tmp_path / "dl"))
        feed_id = store.add_feed(url="http://a/rss", title="The Show", author="The Host",
                                 artwork_url="http://art/cover.png", folder_name="show")
        ep_id = store.add_episode(feed_id=feed_id, guid="g", title="The Episode",
                                  audio_url="http://a/ep.mp3", published="2026-03-04")
        stream = _stream_response(chunks=[self.MP3_BYTES])
        with patch("podcast_downloader.downloader.httpx.stream", return_value=stream), \
             patch("podcast_downloader.downloader._artwork", return_value=self.ART):
            assert download_episode(ep_id, store)
        tags = ID3(store.get_episode_by_id(ep_id)["file_path"])
        assert tags["TIT2"].text == ["The Episode"]
        assert tags.getall("APIC")


class TestDownloadProgress:
    """The progress callback backing the browser's inline progress bar."""

    def _make_store(self, episode, feed, download_dir):
        store = Mock()
        store.read.return_value = {
            "config": {"download_dir": download_dir},
            "feeds": [feed],
            "episodes": [episode],
        }
        return store

    def _fixtures(self, tmp_path):
        episode = {
            "id": 1,
            "feed_id": 1,
            "title": "Test Episode",
            "audio_url": "http://example.com/ep.mp3",
            "published": "2026-01-01",
            "status": "new",
            "file_path": None,
        }
        feed = {"id": 1, "title": "Test Show", "folder_name": "test-show"}
        return episode, feed, self._make_store(episode, feed, str(tmp_path))

    def test_progress_reports_cumulative_bytes_and_total(self, tmp_path):
        episode, feed, store = self._fixtures(tmp_path)
        stream = _stream_response(
            chunks=[b"a" * 10, b"b" * 10, b"c" * 5],
            headers={"content-type": "audio/mpeg", "content-length": "25"},
        )
        calls = []
        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store, progress=lambda done, total: calls.append((done, total)))

        assert calls == [(0, 25), (10, 25), (20, 25), (25, 25)]

    def test_progress_total_is_none_without_content_length(self, tmp_path):
        episode, feed, store = self._fixtures(tmp_path)
        stream = _stream_response(chunks=[b"abc"], headers={"content-type": "audio/mpeg"})
        calls = []
        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store, progress=lambda done, total: calls.append((done, total)))

        assert calls == [(0, None), (3, None)]

    def test_progress_total_is_none_for_malformed_content_length(self, tmp_path):
        episode, feed, store = self._fixtures(tmp_path)
        stream = _stream_response(
            chunks=[b"abc"],
            headers={"content-type": "audio/mpeg", "content-length": "chunked"},
        )
        calls = []
        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store, progress=lambda done, total: calls.append((done, total)))

        assert calls == [(0, None), (3, None)]

    def test_download_works_without_progress_callback(self, tmp_path):
        episode, feed, store = self._fixtures(tmp_path)
        stream = _stream_response(chunks=[b"abc"])
        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream),
        ):
            download_episode(1, store)
        assert store.update_episode_status.call_args[1]["status"] == "done"

    def test_body_is_not_buffered_before_writing(self, tmp_path):
        """httpx.stream, not httpx.get: progress must reflect bytes as they arrive."""
        episode, feed, store = self._fixtures(tmp_path)
        stream = _stream_response(chunks=[b"abc"])
        with (
            patch("podcast_downloader.downloader.httpx.stream", return_value=stream) as mock_stream,
        ):
            download_episode(1, store)
        assert mock_stream.call_args.args[0] == "GET"
