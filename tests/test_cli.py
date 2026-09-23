"""Tests for cli.py - argparse CLI entry point."""

import os
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from podcast_downloader.cli import (
    build_parser, cmd_add, cmd_list, cmd_show, cmd_search, cmd_download,
    cmd_download_latest, cmd_remove, cmd_config, main,
)


def _make_args(command: str, **kwargs) -> Namespace:
    """Create an argparse.Namespace from command and kwargs."""
    parser = build_parser()
    # Map command names to their positional args
    positional_map = {
        "search": ["term"],
        "add": ["url"],
        "show": ["feed_id"],
        "download": ["episode_id"],
        "download-latest": ["feed_id"],
        "remove": ["feed_id"],
    }
    positional_names = positional_map.get(command, [])
    argv = [command]
    for name in positional_names:
        val = kwargs.get(name)
        if val is not None:
            argv.append(str(val))
    return parser.parse_args(argv)


class TestCmdSearch:
    def test_cmd_search_prints_results(self, capsys):
        mock_results = [
            {"title": "Test Podcast", "author": "Author", "feed_url": "https://example.com/feed.xml", "artwork_url": ""},
        ]
        with patch("podcast_downloader.cli.search_podcasts", return_value=mock_results):
            cmd_search(_make_args("search", term="test"))
        captured = capsys.readouterr()
        assert "Test Podcast" in captured.out

    def test_cmd_search_no_results(self, capsys):
        with patch("podcast_downloader.cli.search_podcasts", return_value=[]):
            cmd_search(_make_args("search", term="nonexistent"))
        captured = capsys.readouterr()
        assert "No results found" in captured.out


class TestCmdAdd:
    def test_cmd_add_existing_feed(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.exists_by_feed_url.return_value = True
            cmd_add(_make_args("add", url="https://example.com/feed.xml"))
        captured = capsys.readouterr()
        assert "already subscribed" in captured.out.lower() or "exists" in captured.out.lower()

    def test_cmd_add_new_feed(self, capsys):
        with (
            patch("podcast_downloader.cli.LibraryStore") as MockStore,
            patch("podcast_downloader.library.parse_feed") as mock_parse,
        ):
            mock_store = MockStore.return_value
            mock_store.exists_by_feed_url.return_value = False
            mock_store.add_feed.return_value = 1
            mock_store.exists_by_feed_url.return_value = False
            mock_store.read.return_value = {"config": {"default_policy": "latest", "default_keep_latest": 3}, "feeds": [], "episodes": []}
            mock_result = {
                "title": "New Show",
                "author": "Author",
                "artwork_url": "https://example.com/art.jpg",
                "episodes": [
                    {"guid": "ep1", "title": "Episode 1", "published": "2026-09-21", "audio_url": "https://example.com/ep1.mp3", "duration": 1800},
                ],
            }
            mock_parse.return_value = mock_result
            cmd_add(_make_args("add", url="https://example.com/feed.xml"))
        captured = capsys.readouterr()
        assert "New Show" in captured.out


class TestCmdList:
    def test_cmd_list_empty(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.read.return_value = {"config": {}, "feeds": [], "episodes": []}
            cmd_list([])
        captured = capsys.readouterr()
        assert "No subscriptions" in captured.out

    def test_cmd_list_with_feeds(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.read.return_value = {
                "config": {},
                "feeds": [
                    {"id": 1, "title": "Show 1", "folder_name": "show-1"},
                    {"id": 2, "title": "Show 2", "folder_name": "show-2"},
                ],
                "episodes": [
                    {"feed_id": 1, "status": "done"},
                    {"feed_id": 1, "status": "pending"},
                    {"feed_id": 2, "status": "done"},
                ],
            }
            cmd_list([])
        captured = capsys.readouterr()
        assert "Show 1" in captured.out
        assert "Show 2" in captured.out


class TestCmdShow:
    def test_cmd_show_no_episodes(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.get_feed_by_id.return_value = None
            cmd_show(_make_args("show", feed_id=1))
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_cmd_show_with_episodes(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.get_feed_by_id.return_value = {"id": 1, "title": "Show 1"}
            mock_store.get_episodes_by_feed_id.return_value = [
                {"id": 1, "title": "Episode 1", "published": "2026-09-21", "status": "done"},
            ]
            cmd_show(_make_args("show", feed_id=1))
        captured = capsys.readouterr()
        assert "Episode 1" in captured.out


class TestCmdDownload:
    def test_cmd_episode_not_found(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.get_episode_by_id.return_value = None
            cmd_download(_make_args("download", episode_id=42))
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_failed_download_reports_why_and_exits_nonzero(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.download_episode", return_value=False):
            MockStore.return_value.get_episode_by_id.return_value = {
                "id": 42, "title": "Ep", "error": "HTTP 500 downloading http://x",
            }
            with pytest.raises(SystemExit) as exit_info:
                main(["download", "42"])
        assert exit_info.value.code == 1
        assert "HTTP 500" in capsys.readouterr().out

    def test_successful_download_does_not_exit_nonzero(self):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.download_episode", return_value=True):
            MockStore.return_value.get_episode_by_id.return_value = {"id": 42, "title": "Ep"}
            main(["download", "42"])


class TestCmdDownloadLatest:
    def _feed(self, **kw):
        return dict({"id": 1, "title": "Show 1", "url": "http://s/rss", "keep_latest": 3}, **kw)

    def _episodes(self, *statuses):
        return [{"id": i, "status": st} for i, st in enumerate(statuses, start=1)]

    def test_reports_an_unknown_feed(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            MockStore.return_value.get_feed_by_id.return_value = None
            cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert "not found" in capsys.readouterr().out.lower()

    def test_refreshes_before_choosing_episodes(self):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes", return_value=[]), \
             patch("podcast_downloader.cli.download_episode"):
            MockStore.return_value.get_feed_by_id.return_value = self._feed()
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert mock_refresh.call_count == 1

    def test_downloads_only_the_episodes_we_lack(self):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes") as mock_latest, \
             patch("podcast_downloader.cli.download_episode") as mock_download:
            MockStore.return_value.get_feed_by_id.return_value = self._feed()
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[9])
            mock_latest.return_value = self._episodes("done", "pending", "error")
            cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert [c.args[0] for c in mock_download.call_args_list] == [2, 3]

    def test_says_nothing_to_do_when_we_have_them_all(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes") as mock_latest, \
             patch("podcast_downloader.cli.download_episode") as mock_download:
            MockStore.return_value.get_feed_by_id.return_value = self._feed()
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            mock_latest.return_value = self._episodes("done", "done")
            cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert mock_download.call_count == 0
        assert "already have" in capsys.readouterr().out.lower()

    def test_uses_the_configured_keep_latest_not_the_copy_taken_at_subscribe(self):
        # Changing the setting must reach shows subscribed before the change.
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes", return_value=[]) as mock_latest, \
             patch("podcast_downloader.cli.download_episode"):
            MockStore.return_value.read.return_value = {"config": {"default_keep_latest": 7}, "feeds": []}
            MockStore.return_value.get_feed_by_id.return_value = self._feed(keep_latest=2)
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert mock_latest.call_args.args[2] == 7

    def test_count_flag_overrides_keep_latest(self):
        args = build_parser().parse_args(["download-latest", "1", "-n", "2"])
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes", return_value=[]) as mock_latest, \
             patch("podcast_downloader.cli.download_episode"):
            MockStore.return_value.read.return_value = {"config": {"default_keep_latest": 7}, "feeds": []}
            MockStore.return_value.get_feed_by_id.return_value = self._feed()
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            cmd_download_latest(args)
        assert mock_latest.call_args.args[2] == 2

    def test_defaults_to_three_when_nothing_is_configured(self):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes", return_value=[]) as mock_latest, \
             patch("podcast_downloader.cli.download_episode"):
            MockStore.return_value.read.return_value = {"config": {}, "feeds": []}
            MockStore.return_value.get_feed_by_id.return_value = self._feed()
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert mock_latest.call_args.args[2] == 3

    def test_no_feed_id_covers_every_subscription(self):
        args = build_parser().parse_args(["download-latest"])
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes", return_value=[]), \
             patch("podcast_downloader.cli.download_episode"):
            MockStore.return_value.read.return_value = {
                "config": {},
                "feeds": [self._feed(id=1, title="A"), self._feed(id=2, title="B")],
            }
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            cmd_download_latest(args)
        assert [c.args[1] for c in mock_refresh.call_args_list] == [1, 2]

    def test_no_feed_id_with_no_subscriptions(self, capsys):
        args = build_parser().parse_args(["download-latest"])
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            MockStore.return_value.read.return_value = {"feeds": []}
            cmd_download_latest(args)
        assert "no subscriptions" in capsys.readouterr().out.lower()

    def test_a_failing_feed_does_not_stop_the_others(self, capsys):
        from podcast_downloader.cli import LibraryError
        args = build_parser().parse_args(["download-latest"])
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes", return_value=[]), \
             patch("podcast_downloader.cli.download_episode"):
            MockStore.return_value.read.return_value = {
                "config": {},
                "feeds": [self._feed(id=1, title="A"), self._feed(id=2, title="B")],
            }
            mock_refresh.side_effect = [LibraryError("boom"), SimpleNamespace(new_episode_ids=[])]
            exit_code = cmd_download_latest(args)
        assert mock_refresh.call_count == 2
        assert "boom" in capsys.readouterr().out
        assert exit_code == 1

    def test_a_failed_download_is_reported_and_exits_nonzero(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes") as mock_latest, \
             patch("podcast_downloader.cli.download_episode", return_value=False):
            store = MockStore.return_value
            store.read.return_value = {"config": {}, "feeds": []}
            store.get_feed_by_id.return_value = self._feed()
            store.get_episode_by_id.return_value = {"title": "Ep 1", "error": "HTTP 404"}
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            mock_latest.return_value = self._episodes("pending")
            exit_code = cmd_download_latest(_make_args("download-latest", feed_id=1))
        assert exit_code == 1
        assert "HTTP 404" in capsys.readouterr().out

    def test_all_downloads_succeeding_exits_zero(self):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore, \
             patch("podcast_downloader.cli.refresh") as mock_refresh, \
             patch("podcast_downloader.cli.latest_episodes") as mock_latest, \
             patch("podcast_downloader.cli.download_episode", return_value=True):
            MockStore.return_value.read.return_value = {"config": {}, "feeds": []}
            MockStore.return_value.get_feed_by_id.return_value = self._feed()
            mock_refresh.return_value = SimpleNamespace(new_episode_ids=[])
            mock_latest.return_value = self._episodes("pending")
            assert not cmd_download_latest(_make_args("download-latest", feed_id=1))


class TestCmdRemove:
    def test_cmd_remove_success(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.get_feed_by_id.return_value = {"id": 1, "title": "Show 1"}
            cmd_remove(_make_args("remove", feed_id=1))
        captured = capsys.readouterr()
        assert "removed" in captured.out.lower() or "removed" in captured.err.lower()


class TestCmdConfig:
    def _args(self, download_dir=None, keep_latest=None):
        argv = ["config"]
        if download_dir is not None:
            argv += ["--download-dir", download_dir]
        if keep_latest is not None:
            argv += ["--keep-latest", str(keep_latest)]
        return build_parser().parse_args(argv)

    def test_prints_current_config_when_no_flags_given(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            MockStore.return_value.read.return_value = {
                "config": {"download_dir": "/home/x/Podcasts", "default_keep_latest": 3},
            }
            cmd_config(self._args())
        captured = capsys.readouterr()
        assert "/home/x/Podcasts" in captured.out
        assert "3" in captured.out
        assert MockStore.return_value.update_config.call_count == 0

    def test_sets_download_dir(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.read.return_value = {"config": {"download_dir": "/old"}, "episodes": []}
            cmd_config(self._args(download_dir="/new"))
        mock_store.update_config.assert_called_once_with(download_dir="/new")
        assert "/new" in capsys.readouterr().out

    def test_expands_a_home_relative_download_dir(self):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.read.return_value = {"config": {"download_dir": "/old"}, "episodes": []}
            cmd_config(self._args(download_dir="~/Pods"))
        mock_store.update_config.assert_called_once_with(
            download_dir=os.path.join(os.path.expanduser("~"), "Pods"),
        )

    def test_sets_keep_latest(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            cmd_config(self._args(keep_latest=7))
        mock_store.update_config.assert_called_once_with(default_keep_latest=7)
        assert "7" in capsys.readouterr().out

    def test_rejects_keep_latest_below_one(self, capsys):
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            cmd_config(self._args(keep_latest=0))
        assert mock_store.update_config.call_count == 0
        assert "at least 1" in capsys.readouterr().out.lower()

    def test_notes_files_left_behind_when_download_dir_changes(self, tmp_path, capsys):
        leftover = tmp_path / "episode.mp3"
        leftover.write_bytes(b"x")
        with patch("podcast_downloader.cli.LibraryStore") as MockStore:
            mock_store = MockStore.return_value
            mock_store.read.return_value = {
                "config": {"download_dir": "/old"},
                "episodes": [{"file_path": str(leftover)}],
            }
            cmd_config(self._args(download_dir="/new"))
        assert "1 downloaded episode" in capsys.readouterr().out


class TestMainDefaultCommand:
    def test_no_command_opens_the_interactive_menu(self):
        with patch("podcast_downloader.cli.browse") as mock_browse, \
             patch("podcast_downloader.cli.LibraryStore"):
            main([])
        assert mock_browse.call_count == 1

    def test_version_flag_prints_the_version(self, capsys):
        from podcast_downloader import __version__
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert capsys.readouterr().out.strip() == f"podcast {__version__}"

    def test_other_commands_are_unaffected(self, capsys):
        with patch("podcast_downloader.cli.browse") as mock_browse, \
             patch("podcast_downloader.cli.LibraryStore") as MockStore:
            MockStore.return_value.read.return_value = {"feeds": [], "episodes": []}
            main(["list"])
        assert mock_browse.call_count == 0
