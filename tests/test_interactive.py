"""Tests for interactive.py - the prompt_toolkit podcast browser.

These drive the real prompt_toolkit render/key pipeline rather than mocking it:
the fragment list the browser returns is only validated when prompt_toolkit
actually consumes it, which is where the 'tuple' object has no attribute
'split' crash came from.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text, split_lines
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from podcast_downloader.interactive import (
    MENU_ITEMS,
    PROGRESS_BAR_WIDTH,
    PodcastBrowser,
    _format_age,
    _format_duration,
    _format_size,
    _wrap,
    browse,
)
from podcast_downloader.store import LibraryStore


@pytest.fixture
def store(tmp_path):
    # Never the real ~/Podcasts: removal and folder-move tests touch files there.
    s = LibraryStore(str(tmp_path / "library.json"))
    s.update_config(download_dir=str(tmp_path / "Podcasts"))
    return s


@pytest.fixture
def populated_store(store):
    f1 = store.add_feed(url="http://a.example/rss", title="Alpha Cast", author="A")
    f2 = store.add_feed(url="http://b.example/rss", title="Beta Cast", author="B")
    fid1 = f1["id"] if isinstance(f1, dict) else f1
    fid2 = f2["id"] if isinstance(f2, dict) else f2
    store.add_episode(
        feed_id=fid1, guid="g1", title="Alpha Ep One",
        audio_url="http://a.example/1.mp3", published="2026-01-02T00:00:00Z", duration=65,
    )
    store.add_episode(
        feed_id=fid1, guid="g2", title="Alpha Ep Two",
        audio_url="http://a.example/2.mp3", published="2026-01-01T00:00:00Z", duration=3725,
    )
    store.add_episode(
        feed_id=fid2, guid="g3", title="Beta Ep One",
        audio_url="http://b.example/1.mp3", published=None, duration=None,
    )
    return store


def _join_tasks(timeout: float = 5.0) -> None:
    """Wait for the browser's one-off background threads (search, subscribe, sync)."""
    for thread in threading.enumerate():
        if thread.name in ("podcast-task", "podcast-downloader", "podcast-sync"):
            thread.join(timeout)


def _menu_keys(action: str) -> str:
    """Down-arrow presses that take the menu cursor from the top to `action`."""
    index = next(i for i, (key, _label, _hint) in enumerate(MENU_ITEMS) if key == action)
    return "\x1b[B" * index


def _browser(store, screen: str = "browse") -> PodcastBrowser:
    """A browser parked on a given screen (the app itself opens on the menu)."""
    browser = PodcastBrowser(store)
    browser.screen = screen
    return browser


def _line_text(fragments) -> str:
    return "".join(text for _style, text in fragments)


def _printed_text(mock_print) -> str:
    return " ".join(
        text for call in mock_print.call_args_list for _style, text in call.args[0]
    )


def _run_headless(browser, keys: str, timeout: float = 2.0) -> MagicMock:
    """Feed `keys` to the browser through a real prompt_toolkit input pipe.

    A key handler that raises leaves the app waiting on input forever, so a
    watchdog turns that hang into a test failure instead of a wedged suite.
    """
    timed_out = threading.Event()
    printed = MagicMock()

    def _force_exit() -> None:
        timed_out.set()
        app = browser.application
        if app is not None and app.is_running and app.loop is not None:
            app.loop.call_soon_threadsafe(app.exit)

    watchdog = threading.Timer(timeout, _force_exit)
    watchdog.start()
    try:
        with create_pipe_input() as inp, \
                patch("podcast_downloader.interactive.print_formatted_text", printed):
            inp.send_text(keys)
            browser.run(input=inp, output=DummyOutput())
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        raise AssertionError(f"browser did not exit within {timeout}s for keys {keys!r}")
    return printed


class TestFormatDuration:
    def test_none(self):
        assert _format_duration(None) == ""

    def test_under_an_hour(self):
        assert _format_duration(65) == "1:05"

    def test_over_an_hour(self):
        assert _format_duration(3725) == "1:02:05"


class TestRenderIsValidFormattedText:
    """Regression tests for: AttributeError: 'tuple' object has no attribute 'split'."""

    @pytest.mark.parametrize("screen", ["menu", "browse", "search", "remove", "settings", "about"])
    def test_every_screen_renders_flat_style_text_tuples(self, populated_store, screen):
        browser = _browser(populated_store, screen)
        browser._expand()
        browser.search_results = [{"title": "Darknet Diaries", "author": "J", "feed_url": "http://dd/rss"}]
        browser.search_focus = "results"
        browser.download_state[1] = {
            "status": "downloading", "done": 1, "total": 2, "started": None, "error": None,
        }
        for fragment in browser._render():
            assert isinstance(fragment, tuple), f"not a fragment tuple: {fragment!r}"
            assert len(fragment) == 2, f"fragment must be (style, text): {fragment!r}"
            style, text = fragment
            assert isinstance(style, str), f"style must be a str, got {style!r}"
            assert isinstance(text, str), f"text must be a str, got {text!r}"

    def test_render_survives_prompt_toolkit_pipeline(self, populated_store):
        """to_formatted_text + split_lines is exactly what FormattedTextControl runs."""
        browser = _browser(populated_store)
        fragments = to_formatted_text(browser._render())
        lines = list(split_lines(fragments))
        assert len(lines) > 1, "render must emit multiple lines"
        assert "Browse Subscriptions" in fragment_list_to_text(lines[0])

    def test_render_styles_parse(self, populated_store):
        """A reversed (text, style) tuple only shows up when styles are parsed."""
        from prompt_toolkit.styles import Style

        style = Style.from_dict({"title": "bold"})
        browser = _browser(populated_store)
        browser._expand()
        for style_str, _text in browser._render():
            style.get_attrs_for_style_str(style_str)

    def test_render_with_empty_store(self, store):
        browser = _browser(store)
        lines = list(split_lines(to_formatted_text(browser._render())))
        assert len(lines) > 1

    def test_render_all_rows_expanded(self, populated_store):
        browser = _browser(populated_store)
        browser.expanded_feed_ids = {f["id"] for f in populated_store.read()["feeds"]}
        browser._build_items()
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "Alpha Ep One" in text
        assert "Beta Ep One" in text


class TestBuildItems:
    def test_collapsed_shows_only_feeds(self, populated_store):
        browser = _browser(populated_store)
        assert [i["type"] for i in browser.items] == ["feed", "feed"]

    def test_episode_count_is_per_feed(self, populated_store):
        browser = _browser(populated_store)
        assert [i["episode_count"] for i in browser.items] == [2, 1]

    def test_expanded_nests_episodes_under_their_feed(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        assert [i["type"] for i in browser.items] == ["feed", "episode", "episode", "feed"]
        assert all(i["feed_id"] == browser.items[0]["id"] for i in browser.items[1:3])

    def test_expanded_sorts_episodes_newest_first(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        titles = [i["title"] for i in browser.items if i["type"] == "episode"]
        assert titles == ["Alpha Ep One", "Alpha Ep Two"]

    def test_rebuild_does_not_duplicate_episodes(self, populated_store):
        """_build_items used to append rather than rebuild, duplicating rows."""
        browser = _browser(populated_store)
        browser._expand()
        before = len(browser.items)
        browser._build_items()
        browser._build_items()
        assert len(browser.items) == before

    def test_toggle_select_does_not_duplicate_episodes(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        before = len(browser.items)
        browser.focus_index = 1
        browser._toggle_select()
        browser._toggle_select()
        assert len(browser.items) == before


class TestNavigation:
    def test_expand_then_collapse_restores_list(self, populated_store):
        browser = _browser(populated_store)
        before = list(browser.items)
        browser._expand()
        browser._collapse()
        assert browser.items == before

    def test_collapse_from_episode_focuses_parent_feed(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        browser.focus_index = 2
        browser._collapse()
        assert browser.items[browser.focus_index]["type"] == "feed"
        assert browser.focus_index < len(browser.items)

    def test_toggle_select_marks_episode(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        browser.focus_index = 1
        ep_id = browser.items[1]["id"]
        browser._toggle_select()
        assert ep_id in browser.selected_episode_ids
        browser._toggle_select()
        assert ep_id not in browser.selected_episode_ids

    def test_toggle_select_ignores_feed_rows(self, populated_store):
        browser = _browser(populated_store)
        browser.focus_index = 0
        browser._toggle_select()
        assert browser.selected_episode_ids == set()

    def test_focus_stays_in_range_after_collapse(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        browser.focus_index = len(browser.items) - 1
        browser._collapse()
        assert 0 <= browser.focus_index < len(browser.items)


class TestKeyHandling:
    """Drives the real key pipeline; event.key never existed on KeyPressEvent."""

    def test_quit_exits_cleanly(self, populated_store):
        browser = _browser(populated_store)
        _run_headless(browser, "q")
        assert browser.download_state == {}

    def test_arrow_down_moves_focus(self, populated_store):
        browser = _browser(populated_store)
        _run_headless(browser, "\x1b[Bq")
        assert browser.focus_index == 1

    def test_right_expands_feed(self, populated_store):
        browser = _browser(populated_store)
        _run_headless(browser, "\x1b[Cq")
        assert browser.expanded_feed_ids == {browser.items[0]["id"]}

    def test_space_selects_episode(self, populated_store):
        browser = _browser(populated_store)
        _run_headless(browser, "\x1b[C\x1b[B q")
        assert len(browser.selected_episode_ids) == 1

    def test_keys_on_empty_store_do_not_crash(self, store):
        browser = _browser(store)
        _run_headless(browser, "\r\x1b[B\x1b[C\x1b[D q")
        assert browser.items == []


class TestEnterActivation:
    """Enter opens a feed node, and downloads a focused episode."""

    def test_enter_expands_focused_feed(self, populated_store):
        browser = _browser(populated_store)
        _run_headless(browser, "\rq")
        assert browser.expanded_feed_ids == {browser.items[0]["id"]}
        assert [i["type"] for i in browser.items[:3]] == ["feed", "episode", "episode"]

    def test_enter_matches_right_arrow_on_a_feed(self, populated_store):
        by_enter = _browser(populated_store)
        _run_headless(by_enter, "\rq")
        by_arrow = _browser(populated_store)
        _run_headless(by_arrow, "\x1b[Cq")
        assert by_enter.items == by_arrow.items

    def test_enter_on_open_feed_collapses_it(self, populated_store):
        browser = _browser(populated_store)
        _run_headless(browser, "\r\rq")
        assert browser.expanded_feed_ids == set()
        assert [i["type"] for i in browser.items] == ["feed", "feed"]

    def test_enter_on_episode_downloads_just_that_episode(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode") as dl:
            _run_headless(browser, "\r\x1b[B\rq")
        dl.assert_called_once()
        assert dl.call_args.args[0] == browser.items[1]["id"]

    def test_enter_on_episode_does_not_need_selection(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode") as dl:
            _run_headless(browser, "\r\x1b[B\rq")
        assert browser.selected_episode_ids == set()
        assert dl.call_count == 1

    def test_enter_twice_does_not_queue_a_second_time(self, populated_store):
        """An in-flight episode must not be re-queued by an impatient second Enter."""
        browser = _browser(populated_store)
        browser._expand()
        browser.focus_index = 1
        release = threading.Event()

        with patch("podcast_downloader.downloader.download_episode",
                   side_effect=lambda *a, **k: release.wait(5.0)) as dl:
            browser._activate()
            browser._activate()
            browser._activate()
            release.set()
            browser._worker.join(5.0)
        assert dl.call_count == 1

    def test_second_enter_does_not_reset_progress_of_a_live_download(self, populated_store):
        """Re-queuing would overwrite the state and snap the progress bar back to 0%."""
        browser = _browser(populated_store)
        browser._expand()
        browser.focus_index = 1
        ep_id = browser.items[1]["id"]
        release = threading.Event()
        downloading = threading.Event()

        def blocked(_ep_id, _store, progress=None):
            progress(4096, 8192)
            downloading.set()
            release.wait(5.0)

        with patch("podcast_downloader.downloader.download_episode", side_effect=blocked):
            browser._activate()
            assert downloading.wait(5.0), "download never started"
            browser._activate()  # impatient second Enter
            state = browser.download_state[ep_id]
            status, done = state["status"], state["done"]
            release.set()
            browser._worker.join(5.0)

        assert status == "downloading", "second Enter re-queued a live download"
        assert done == 4096, "second Enter reset the progress bar"

    def test_worker_survives_a_missing_state_entry(self, populated_store):
        """A queued id whose state vanished must be skipped, not kill the worker."""
        browser = _browser(populated_store)
        browser._expand()
        ep_id = browser.items[1]["id"]
        with patch("podcast_downloader.downloader.download_episode") as dl:
            browser._start_downloads([ep_id])
            browser.download_state.pop(ep_id, None)
            browser._worker.join(5.0)
        assert not browser._worker.is_alive()
        assert dl.call_count <= 1

    def test_enter_re_downloads_after_a_finished_download(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        browser.focus_index = 1
        with patch("podcast_downloader.downloader.download_episode") as dl:
            browser._activate()
            browser._worker.join(5.0)
            browser._activate()
            browser._worker.join(5.0)
        assert dl.call_count == 2

    def test_enter_passes_a_progress_callback(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode") as dl:
            _run_headless(browser, "\r\x1b[B\rq")
        assert callable(dl.call_args.kwargs["progress"])


class TestDownloadQueue:
    def test_d_queues_every_selected_episode(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode") as dl:
            _run_headless(browser, "\r\x1b[B \x1b[B dq")
        assert dl.call_count == 2

    def test_d_with_no_selection_downloads_nothing(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode") as dl:
            _run_headless(browser, "dq")
        dl.assert_not_called()

    def test_d_clears_the_selection(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode"):
            _run_headless(browser, "\r\x1b[B dq")
        assert browser.selected_episode_ids == set()

    def test_successful_download_clears_progress_state(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode"):
            _run_headless(browser, "\r\x1b[B\rq")
        assert browser.download_state == {}

    def test_failed_download_is_recorded_not_raised(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.downloader.download_episode", side_effect=RuntimeError("boom")):
            _run_headless(browser, "\r\x1b[B\rq")
        states = list(browser.download_state.values())
        assert [s["status"] for s in states] == ["error"]
        assert states[0]["error"] == "boom"

    def test_store_level_error_is_surfaced(self, populated_store):
        """download_episode swallows errors into the store; the browser must read them back."""
        browser = _browser(populated_store)

        def mark_error(ep_id, store, progress=None):
            store.update_episode_status(ep_id, status="error", error="HTTP 404")

        with patch("podcast_downloader.downloader.download_episode", side_effect=mark_error):
            _run_headless(browser, "\r\x1b[B\rq")
        states = list(browser.download_state.values())
        assert [s["status"] for s in states] == ["error"]
        assert states[0]["error"] == "HTTP 404"

    def test_starting_a_download_does_not_block_the_caller(self, populated_store):
        """The download runs on a worker thread, so the UI thread returns at once."""
        browser = _browser(populated_store)
        browser._expand()
        release = threading.Event()
        ep_id = browser.items[1]["id"]

        with patch("podcast_downloader.downloader.download_episode",
                   side_effect=lambda *a, **k: release.wait(5.0)):
            began = time.monotonic()
            browser._start_downloads([ep_id])
            elapsed = time.monotonic() - began
            worker_alive = browser._worker.is_alive()
            release.set()
            browser._worker.join(5.0)

        assert elapsed < 0.5, "queuing a download blocked the UI thread"
        assert worker_alive

    def test_keys_still_move_focus_while_a_download_runs(self, populated_store):
        """A live worker must not stall the app's event loop."""
        browser = _browser(populated_store)
        browser._expand()
        release = threading.Event()
        # Released on a timer: run() waits for downloads, so the test thread can't.
        threading.Timer(0.3, release.set).start()

        with patch("podcast_downloader.downloader.download_episode",
                   side_effect=lambda *a, **k: release.wait(5.0)):
            browser.focus_index = 1
            browser._activate()
            _run_headless(browser, "\x1b[B\x1b[Bq", timeout=5.0)

        assert browser.focus_index == 3
        assert not browser._worker.is_alive()

    def test_run_waits_for_in_flight_downloads(self, populated_store):
        """Quitting mid-download must not truncate the file."""
        browser = _browser(populated_store)
        finished = threading.Event()

        def slow_download(*_args, **_kwargs):
            time.sleep(0.2)
            finished.set()

        with patch("podcast_downloader.downloader.download_episode", side_effect=slow_download):
            printed = _run_headless(browser, "\r\x1b[B\rq", timeout=5.0)
        assert finished.is_set()
        assert browser._worker is not None and not browser._worker.is_alive()
        assert "Finishing" in _printed_text(printed)


class TestProgressRendering:
    def _state(self, **kwargs):
        state = {"status": "downloading", "done": 0, "total": None, "started": None, "error": None}
        state.update(kwargs)
        return state

    def test_queued_line(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(status="queued")))
        assert "queued" in text

    def test_bar_fills_proportionally(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(done=50, total=100)))
        assert "\u2588" * (PROGRESS_BAR_WIDTH // 2) in text
        assert "50%" in text

    def test_bar_is_empty_at_zero(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(done=0, total=100)))
        assert "\u2588" not in text
        assert "\u2591" * PROGRESS_BAR_WIDTH in text
        assert "0%" in text

    def test_bar_is_full_at_completion(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(done=100, total=100)))
        assert "\u2588" * PROGRESS_BAR_WIDTH in text
        assert "100%" in text

    def test_bar_does_not_overflow_past_total(self, populated_store):
        """Servers lie about content-length; the bar must not exceed its width."""
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(done=500, total=100)))
        assert text.count("\u2588") == PROGRESS_BAR_WIDTH
        assert "100%" in text

    def test_shows_byte_counts(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(done=5 * 1024 * 1024, total=10 * 1024 * 1024)))
        assert "5.0 MB / 10.0 MB" in text

    def test_unknown_total_shows_bytes_without_a_bar(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(done=2048, total=None)))
        assert "2.0 KB downloaded" in text
        assert "\u2588" not in text

    def test_shows_speed_once_elapsed(self, populated_store):
        browser = _browser(populated_store)
        state = self._state(done=1024 * 1024, total=None, started=time.monotonic() - 2.0)
        assert "/s" in _line_text(browser._progress_line(state))

    def test_no_speed_before_half_a_second(self, populated_store):
        browser = _browser(populated_store)
        state = self._state(done=1024, total=None, started=time.monotonic())
        assert "/s" not in _line_text(browser._progress_line(state))

    def test_error_line_shows_message(self, populated_store):
        browser = _browser(populated_store)
        text = _line_text(browser._progress_line(self._state(status="error", error="HTTP 404")))
        assert "HTTP 404" in text

    def test_error_line_flattens_newlines(self, populated_store):
        browser = _browser(populated_store)
        line = browser._progress_line(self._state(status="error", error="bad\nthings"))
        assert "\n" not in _line_text(line)

    def test_progress_line_appears_in_the_full_render(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        ep_id = browser.items[1]["id"]
        browser.download_state[ep_id] = self._state(done=50, total=100)
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "50%" in text
        assert "LOADING" in text

    def test_no_progress_line_when_idle(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        before = len(list(split_lines(to_formatted_text(browser._render()))))
        browser.download_state[browser.items[1]["id"]] = self._state(done=1, total=2)
        after = len(list(split_lines(to_formatted_text(browser._render()))))
        assert after == before + 1

    def test_footer_pluralises_the_selection(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        browser.selected_episode_ids = {browser.items[1]["id"]}
        assert "1 episode " in fragment_list_to_text(to_formatted_text(browser._render()))
        browser.selected_episode_ids.add(browser.items[2]["id"])
        assert "2 episodes" in fragment_list_to_text(to_formatted_text(browser._render()))

    def test_footer_counts_active_downloads(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        browser.download_state[browser.items[1]["id"]] = self._state(status="queued")
        browser.download_state[browser.items[2]["id"]] = self._state(status="downloading")
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "Downloading: 2" in text


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(512) == "512 B"

    def test_kilobytes(self):
        assert _format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _format_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert _format_size(3 * 1024 ** 3) == "3.0 GB"

    def test_zero(self):
        assert _format_size(0) == "0 B"


class TestBrowse:
    def test_browse_opens_even_with_no_subscriptions(self, store):
        """The menu offers Search, so an empty library is still worth opening."""
        with patch.object(PodcastBrowser, "run") as run:
            browse(store)
        run.assert_called_once()

    def test_app_opens_on_the_menu(self, populated_store):
        assert PodcastBrowser(populated_store).screen == "menu"


class TestMenuScreen:
    def test_menu_shows_subscription_count(self, populated_store):
        browser = _browser(populated_store, "menu")
        assert "2 subscriptions" in fragment_list_to_text(to_formatted_text(browser._render()))

    def test_menu_pluralises_a_single_subscription(self, store):
        store.add_feed(url="http://a/rss", title="Only One", author="A")
        browser = _browser(store, "menu")
        browser._refresh()
        footer = fragment_list_to_text(to_formatted_text(browser._render())).splitlines()[-1]
        assert footer == "  1 subscription"

    def test_arrows_move_the_menu_cursor(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, "\x1b[B\x1b[Bq")
        assert browser.menu_index == 2

    def test_menu_cursor_stops_at_the_ends(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, "\x1b[A\x1b[A\x1b[Aq")
        assert browser.menu_index == 0

    def test_enter_opens_browse(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, "\x1b[B\rq")
        assert browser.screen == "browse"

    def test_enter_opens_search(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, "\r\x03")
        assert browser.screen == "search"

    def test_enter_opens_remove(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, _menu_keys("remove") + "\r\x03")
        assert browser.screen == "remove"

    def test_ctrl_c_exits_from_any_screen(self, populated_store):
        for screen in ("menu", "search", "remove", "browse"):
            browser = _browser(populated_store, screen)
            _run_headless(browser, "\x03")
            assert browser.screen == screen

    def test_enter_on_quit_exits(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, _menu_keys("quit") + "\r")
        assert browser.menu_index == len(MENU_ITEMS) - 1

    def test_q_quits_from_the_menu(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, "q")
        assert browser.screen == "menu"

    def test_escape_does_not_quit_from_the_menu(self, populated_store):
        browser = _browser(populated_store, "menu")
        # If Esc quit the app, the down arrow after it would never run.
        _run_headless(browser, "\x1b\x1b[Bq")
        assert browser.menu_index == 1

    def test_escape_returns_to_menu_from_browse(self, populated_store):
        browser = _browser(populated_store, "browse")
        _run_headless(browser, "\x1bq")
        assert browser.screen == "menu"

    def test_menu_reports_active_downloads(self, populated_store):
        browser = _browser(populated_store, "menu")
        browser.download_state[1] = {
            "status": "downloading", "done": 1, "total": 2, "started": None, "error": None
        }
        assert "Downloading: 1" in fragment_list_to_text(to_formatted_text(browser._render()))


class TestOpenDownloadFolder:
    def test_opens_the_configured_directory(self, populated_store, tmp_path):
        download_dir = tmp_path / "Podcasts"
        populated_store.update_config(download_dir=str(download_dir))
        browser = _browser(populated_store, "menu")
        with patch("subprocess.Popen") as popen:
            browser._open_download_folder()
        popen.assert_called_once()
        args = popen.call_args[0][0]
        assert args[0] in ("open", "xdg-open")
        assert args[1] == str(download_dir)
        assert download_dir.is_dir(), "should create the folder if it doesn't exist yet"
        assert browser.flash == f"Opened {download_dir}"

    def test_reports_a_missing_opener_command(self, populated_store, tmp_path):
        populated_store.update_config(download_dir=str(tmp_path / "Podcasts"))
        browser = _browser(populated_store, "menu")
        with patch("subprocess.Popen", side_effect=FileNotFoundError):
            browser._open_download_folder()
        assert "No 'open" in browser.flash or "No 'xdg-open" in browser.flash

    def test_stays_on_the_menu(self, populated_store, tmp_path):
        populated_store.update_config(download_dir=str(tmp_path / "Podcasts"))
        browser = _browser(populated_store, "menu")
        with patch("subprocess.Popen"):
            _run_headless(browser, _menu_keys("open_folder") + "\rq")
        assert browser.screen == "menu"


class TestAboutScreen:
    def _about_index(self):
        from podcast_downloader.interactive import SETTINGS_ITEMS
        return next(i for i, (key, _l, _h) in enumerate(SETTINGS_ITEMS) if key == "about")

    def test_enter_on_about_shows_the_version_and_license(self, populated_store):
        from podcast_downloader import __version__
        browser = _browser(populated_store, "settings")
        _run_headless(browser, "\x1b[B" * self._about_index() + "\r\x03")
        assert browser.screen == "about"
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert f"Podcast Downloader  v{__version__}" in text
        assert "MIT License" in text
        assert "http" not in text, "no repo link: a clone or fork would point at the wrong place"

    def test_enter_on_about_does_not_start_editing(self, populated_store):
        browser = _browser(populated_store, "settings")
        browser.settings_index = self._about_index()
        browser._on_key_settings("enter", MagicMock())
        assert browser.screen == "about"
        assert browser.settings_editing is False

    def test_escape_goes_back_to_settings(self, populated_store):
        browser = _browser(populated_store, "about")
        _run_headless(browser, "\x1b\x03")
        assert browser.screen == "settings"

    def test_q_quits_from_about(self, populated_store):
        browser = _browser(populated_store, "about")
        event = MagicMock()
        browser._on_key_about("q", event)
        event.app.exit.assert_called_once()


class TestSettingsScreen:
    def test_arrows_move_settings_cursor(self, populated_store):
        browser = _browser(populated_store, "settings")
        _run_headless(browser, "\x1b[B\x1bq")
        assert browser.settings_index == 1

    def test_escape_returns_to_menu_from_settings(self, populated_store):
        browser = _browser(populated_store, "settings")
        _run_headless(browser, "\x1bq")
        assert browser.screen == "menu"

    def test_enter_on_download_dir_prompts_and_saves(self, populated_store, tmp_path):
        new_dir = str(tmp_path / "NewPodcasts")
        browser = _browser(populated_store, "settings")
        browser.settings_index = 0
        _run_headless(browser, f"\r{new_dir}\x1b\x1bq")
        assert browser.screen == "menu"

    def test_enter_on_keep_latest_saves_number(self, populated_store):
        browser = _browser(populated_store, "settings")
        browser.settings_index = 1
        _run_headless(browser, "\r10\r\x1bq")
        assert browser.screen == "menu"
        data = browser.store.read()
        assert data["config"]["default_keep_latest"] == 10

    def test_keep_latest_rejects_non_numbers(self, populated_store):
        browser = _browser(populated_store, "settings")
        browser.settings_index = 1
        _run_headless(browser, "\rabc\r\x1bq")
        assert "Must be a number" in browser.flash

    def test_settings_from_menu_opens_settings_screen(self, populated_store):
        browser = _browser(populated_store, "menu")
        _run_headless(browser, _menu_keys("settings") + "\r\x03")
        assert browser.screen == "settings"

    def test_download_dir_change_prompts_move_confirmation(self, populated_store, tmp_path):
        new_dir = str(tmp_path / "NewPodcasts")
        browser = _browser(populated_store, "settings")
        browser.settings_index = 0
        _run_headless(browser, f"\r{new_dir}\rY\x1bq")
        assert browser.screen == "menu"
        assert "Moved" in browser.flash or "Cancelled" in browser.flash

    def test_download_dir_change_cancelled_on_no(self, populated_store, tmp_path):
        new_dir = str(tmp_path / "NewPodcasts")
        browser = _browser(populated_store, "settings")
        browser.settings_index = 0
        _run_headless(browser, f"\r{new_dir}\rn\x1bq")
        assert browser.screen == "menu"
        assert "Cancelled" in browser.flash

    def test_q_while_typing_a_path_is_just_a_letter(self, populated_store):
        browser = _browser(populated_store, "settings")
        browser.settings_editing = True
        event = MagicMock()
        for key in "/home/me/aqua/queue":
            browser._on_key_settings(key, event)
        event.app.exit.assert_not_called()
        assert browser.settings_edit_value == "/home/me/aqua/queue"

    def test_q_still_quits_when_not_typing(self, populated_store):
        browser = _browser(populated_store, "settings")
        event = MagicMock()
        browser._on_key_settings("q", event)
        event.app.exit.assert_called_once()

    def test_home_relative_download_dir_is_expanded(self, populated_store):
        browser = _browser(populated_store, "settings")
        browser.settings_editing = True
        browser.settings_edit_value = "~/Pods"
        browser._save_settings()
        expected = os.path.join(os.path.expanduser("~"), "Pods")
        assert populated_store.read()["config"]["download_dir"] == expected

    def test_move_prompt_names_the_old_directory_not_the_new_one_twice(self, populated_store, tmp_path):
        old_dir = populated_store.read()["config"]["download_dir"]
        new_dir = str(tmp_path / "NewPodcasts")
        browser = _browser(populated_store, "settings")
        browser.settings_editing = True
        browser.settings_edit_value = new_dir
        browser._save_settings()
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert f"from {old_dir} to {new_dir}" in text


class TestSearchScreen:
    RESULTS = [
        {"title": "Darknet Diaries", "author": "Jack Rhysider", "feed_url": "http://dd/rss"},
        {"title": "The Rest Is History", "author": "Goalhanger", "feed_url": "http://rih/rss"},
    ]

    def test_typing_builds_the_query(self, populated_store):
        browser = _browser(populated_store, "search")
        _run_headless(browser, "hello\x1bq")
        assert browser.search_query == "hello"

    def test_letters_that_are_shortcuts_elsewhere_are_typed(self, populated_store):
        """q and d must not quit or download while the search box has focus."""
        browser = _browser(populated_store, "search")
        _run_headless(browser, "qdn\x1bq")
        assert browser.search_query == "qdn"
        assert browser.screen == "menu"

    def test_spaces_are_typed_not_treated_as_select(self, populated_store):
        browser = _browser(populated_store, "search")
        _run_headless(browser, "a b\x1bq")
        assert browser.search_query == "a b"

    def test_backspace_deletes(self, populated_store):
        browser = _browser(populated_store, "search")
        _run_headless(browser, "abc\x7f\x1bq")
        assert browser.search_query == "ab"

    def test_backspace_on_empty_query_is_harmless(self, populated_store):
        browser = _browser(populated_store, "search")
        _run_headless(browser, "\x7f\x7f\x1bq")
        assert browser.search_query == ""

    def test_query_is_shown(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_query = "history"
        assert "history" in fragment_list_to_text(to_formatted_text(browser._render()))

    def test_enter_runs_the_search(self, populated_store):
        browser = _browser(populated_store, "search")
        with patch("podcast_downloader.feeds.search_podcasts", return_value=self.RESULTS) as search:
            _run_headless(browser, "history\r\x1bq")
            _join_tasks()
        search.assert_called_once_with("history")
        assert browser.search_results == self.RESULTS

    def test_search_runs_off_the_ui_thread(self, populated_store):
        browser = _browser(populated_store, "search")
        release = threading.Event()
        threading.Timer(0.3, release.set).start()
        with patch("podcast_downloader.feeds.search_podcasts",
                   side_effect=lambda _q: release.wait(5.0) or self.RESULTS):
            browser.search_query = "history"
            began = time.monotonic()
            browser._run_search()
            elapsed = time.monotonic() - began
            _join_tasks()
        assert elapsed < 0.2, "search blocked the UI thread"

    def test_empty_query_does_not_search(self, populated_store):
        browser = _browser(populated_store, "search")
        with patch("podcast_downloader.feeds.search_podcasts") as search:
            _run_headless(browser, "   \r\x1bq")
            _join_tasks()
        search.assert_not_called()

    def test_no_results_is_reported(self, populated_store):
        browser = _browser(populated_store, "search")
        with patch("podcast_downloader.feeds.search_podcasts", return_value=[]):
            _run_headless(browser, "zzz\r\x1bq")
            _join_tasks()
        assert "No results" in browser.search_status

    def test_search_failure_is_reported_not_raised(self, populated_store):
        browser = _browser(populated_store, "search")
        with patch("podcast_downloader.feeds.search_podcasts", side_effect=RuntimeError("offline")):
            _run_headless(browser, "zzz\r\x1bq")
            _join_tasks()
        assert "offline" in browser.search_status

    def test_results_are_listed(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = self.RESULTS
        browser.search_focus = "results"
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "Darknet Diaries" in text
        assert "Jack Rhysider" in text

    def test_already_subscribed_results_are_flagged(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = [{"title": "Alpha Cast", "author": "A", "feed_url": "http://a.example/rss"}]
        browser.search_focus = "results"
        assert "subscribed" in fragment_list_to_text(to_formatted_text(browser._render()))

    def test_arrows_move_through_results(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = self.RESULTS
        browser.search_focus = "results"
        _run_headless(browser, "\x1b[B\x1bq")
        assert browser.search_index == 1

    def test_result_cursor_stops_at_the_ends(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = self.RESULTS
        browser.search_focus = "results"
        _run_headless(browser, "\x1b[B\x1b[B\x1b[B\x1bq")
        assert browser.search_index == len(self.RESULTS) - 1

    def test_enter_on_a_result_subscribes(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = self.RESULTS
        browser.search_focus = "results"
        with patch("podcast_downloader.library.subscribe") as sub:
            sub.return_value = SimpleNamespace(feed_id=9, title="Darknet Diaries", episode_count=7)
            _run_headless(browser, "\r\x1bq")
            _join_tasks()
        sub.assert_called_once()
        assert sub.call_args.args[1] == "http://dd/rss"

    def test_enter_subscribes_to_the_highlighted_result(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = self.RESULTS
        browser.search_focus = "results"
        with patch("podcast_downloader.library.subscribe") as sub:
            sub.return_value = SimpleNamespace(feed_id=9, title="The Rest Is History", episode_count=7)
            _run_headless(browser, "\x1b[B\r\x1bq")
            _join_tasks()
        assert sub.call_args.args[1] == "http://rih/rss"

    def test_subscribing_really_adds_the_feed(self, populated_store):
        browser = _browser(populated_store, "search")
        feed = {
            "title": "Darknet Diaries",
            "author": "Jack Rhysider",
            "artwork_url": "",
            "last_refreshed": "2026-01-01",
            "episodes": [
                {"guid": "n1", "title": "Ep 1", "audio_url": "http://dd/1.mp3",
                 "published": "2026-01-01", "duration": 60},
            ],
        }
        with patch("podcast_downloader.library.parse_feed", return_value=feed):
            browser._subscribe_to(self.RESULTS[0])
            _join_tasks()
        titles = [f["title"] for f in populated_store.read()["feeds"]]
        assert "Darknet Diaries" in titles
        assert browser.store.exists_by_feed_url("http://dd/rss")

    def test_subscribe_reports_a_duplicate(self, populated_store):
        browser = _browser(populated_store, "search")
        browser._subscribe_to({"title": "Alpha Cast", "feed_url": "http://a.example/rss"})
        _join_tasks()
        assert "already subscribed" in browser.search_status

    def test_subscribe_reports_an_unparseable_feed(self, populated_store):
        browser = _browser(populated_store, "search")
        from podcast_downloader.feeds import FeedError
        with patch("podcast_downloader.library.parse_feed", side_effect=FeedError("HTTP 404 fetching x")):
            browser._subscribe_to(self.RESULTS[0])
            _join_tasks()
        assert "HTTP 404" in browser.search_status

    def test_subscribe_handles_a_result_with_no_url(self, populated_store):
        browser = _browser(populated_store, "search")
        browser._subscribe_to({"title": "Broken"})
        assert "no feed URL" in browser.search_status

    def test_subscribing_flashes_on_the_menu(self, populated_store):
        browser = _browser(populated_store, "search")
        feed = {"title": "Darknet Diaries", "author": "J", "artwork_url": "",
                "last_refreshed": "2026-01-01", "episodes": []}
        with patch("podcast_downloader.library.parse_feed", return_value=feed):
            browser._subscribe_to(self.RESULTS[0])
            _join_tasks()
        assert "Darknet Diaries" in browser.flash

    def test_escape_returns_to_the_menu(self, populated_store):
        browser = _browser(populated_store, "search")
        _run_headless(browser, "\x1bq")
        assert browser.screen == "menu"

    def test_typing_after_results_edits_the_query_again(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_query = "x"
        browser.search_results = self.RESULTS
        browser.search_focus = "results"
        _run_headless(browser, "y\x1bq")
        assert browser.search_focus == "query"
        assert browser.search_query == "xy"

    def test_a_successful_search_moves_focus_to_the_results(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_query = "history"
        with patch("podcast_downloader.feeds.search_podcasts", return_value=self.RESULTS):
            browser._run_search()
            _join_tasks()
        assert browser.search_focus == "results"
        assert browser.search_results == self.RESULTS


class TestRemoveScreen:
    def test_feeds_are_listed(self, populated_store):
        browser = _browser(populated_store, "remove")
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "Alpha Cast" in text
        assert "Beta Cast" in text

    def test_empty_library_says_so(self, store):
        browser = _browser(store, "remove")
        assert "No subscriptions" in fragment_list_to_text(to_formatted_text(browser._render()))

    def test_arrows_move_the_cursor(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\x1b[B\x1bq")
        assert browser.remove_index == 1

    def test_enter_asks_for_confirmation_before_removing(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\r\x1bq")
        assert len(populated_store.read()["feeds"]) == 2, "removed without confirming"

    def test_confirmation_prompt_names_the_feed(self, populated_store):
        browser = _browser(populated_store, "remove")
        browser.remove_confirm = browser.feeds[0]
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "Alpha Cast" in text
        assert "Remove" in text

    def test_y_removes_the_feed(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\ry\x1bq")
        titles = [f["title"] for f in populated_store.read()["feeds"]]
        assert titles == ["Beta Cast"]

    def test_removing_a_feed_drops_its_episodes(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\ry\x1bq")
        assert populated_store.read()["episodes"] == [
            ep for ep in populated_store.read()["episodes"] if ep["feed_id"] != 1
        ]
        assert all(ep["feed_id"] != 1 for ep in populated_store.read()["episodes"])

    def test_n_cancels(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\rn\x1bq")
        assert len(populated_store.read()["feeds"]) == 2
        assert browser.remove_confirm is None

    def test_escape_cancels_the_confirmation(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\r\x1b\x1bq")
        assert len(populated_store.read()["feeds"]) == 2

    def test_a_stray_d_on_the_confirmation_does_not_remove(self, populated_store):
        # D used to mean "remove and delete files"; it must not act as a yes now.
        browser = _browser(populated_store, "remove")
        with patch("podcast_downloader.library.unsubscribe") as unsub:
            _run_headless(browser, "\rd\x1b\x1bq")
        unsub.assert_not_called()

    def test_removal_flashes_on_the_menu(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\ry\x1bq")
        assert "Alpha Cast" in browser.flash

    def test_cursor_stays_in_range_after_removing_the_last_feed(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\x1b[B\ry\x1bq")
        assert 0 <= browser.remove_index < max(1, len(browser.feeds))

    def test_removing_an_expanded_feed_clears_its_expansion(self, populated_store):
        browser = _browser(populated_store, "browse")
        browser._expand()
        feed_id = browser.items[0]["id"]
        browser.screen = "remove"
        _run_headless(browser, "\ry\x1bq")
        assert feed_id not in browser.expanded_feed_ids

    def test_removing_refreshes_the_browse_list(self, populated_store):
        browser = _browser(populated_store, "remove")
        _run_headless(browser, "\ry\x1bq")
        assert [i["title"] for i in browser.items] == ["Beta Cast"]


class TestScreenPolish:
    def test_remove_list_shows_episode_counts(self, populated_store):
        browser = _browser(populated_store, "remove")
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "2 episodes" in text
        assert "1 episode" in text

    def test_episode_counts_are_pluralised(self, populated_store):
        """A feed with one episode reads '1 episode', not '1 episodes'."""
        for screen in ("browse", "remove"):
            browser = _browser(populated_store, screen)
            text = fragment_list_to_text(to_formatted_text(browser._render()))
            assert "1 episodes" not in text, screen
            assert "1 episode" in text, screen

    def test_search_status_does_not_hide_results(self, populated_store):
        """A 'subscribing…' message must not blank the list the user is working in."""
        browser = _browser(populated_store, "search")
        browser.search_results = TestSearchScreen.RESULTS
        browser.search_focus = "results"
        browser.search_status = "Subscribing to Darknet Diaries…"
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "Subscribing" in text
        assert "The Rest Is History" in text

    def test_screen_rows_fit_eighty_columns(self, populated_store):
        """Window clipping hides anything past the terminal width."""
        browser = _browser(populated_store, "search")
        browser.search_results = [
            {"title": "A" * 90, "author": "B" * 60, "feed_url": "http://x/rss"},
        ]
        browser.search_focus = "results"
        browser._expand()
        browser.download_state[browser.items[1]["id"]] = {
            "status": "downloading", "done": 18_400_000, "total": 42_100_000,
            "started": time.monotonic() - 12, "error": None,
        }
        browser.selected_episode_ids = {browser.items[1]["id"]}
        for screen in ("menu", "browse", "search", "remove"):
            browser.screen = screen
            for line in split_lines(to_formatted_text(browser._render())):
                width = len(fragment_list_to_text(line))
                assert width <= 80, f"{screen} row is {width} cols: {fragment_list_to_text(line)!r}"

    def test_long_episode_titles_wrap_rather_than_truncate(self, populated_store):
        """Episode names are never cut: whatever does not fit wraps onto an
        indented continuation line."""
        browser = _browser(populated_store, "browse")
        title = " ".join(f"word{n}" for n in range(30))
        data = populated_store.read()
        data["episodes"][0]["title"] = title
        populated_store.write(data)
        browser._refresh()
        browser._expand()
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "…" not in text, "an episode title was truncated"
        for word in title.split():
            assert word in text, f"{word} was dropped from the wrapped title"

    def test_long_titles_are_truncated_with_an_ellipsis(self, populated_store):
        browser = _browser(populated_store, "search")
        browser.search_results = [{"title": "A" * 90, "author": "B", "feed_url": "http://x/rss"}]
        browser.search_focus = "results"
        assert "…" in fragment_list_to_text(to_formatted_text(browser._render()))


class TestFeedSync:
    """'r' on browse and the Refresh all feeds menu item."""

    @staticmethod
    def _feed(*episodes):
        return {
            "title": "Alpha Cast", "author": "A", "artwork_url": "",
            "episodes": list(episodes),
        }

    @staticmethod
    def _episode(guid, title="New Ep"):
        return {"guid": guid, "title": title, "audio_url": f"http://a.example/{guid}.mp3",
                "published": "2026-02-01", "duration": 60}

    def test_r_refreshes_the_focused_feed(self, populated_store):
        browser = _browser(populated_store)
        browser.focus_index = 0
        with patch("podcast_downloader.library.refresh") as refresh:
            refresh.return_value = SimpleNamespace(feed_id=1, title="Alpha Cast", new_episode_ids=[])
            _run_headless(browser, "rq")
            _join_tasks()
        assert refresh.call_args.args[1] == browser.items[0]["id"]

    def test_r_on_an_episode_refreshes_its_parent_feed(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()                 # open Alpha Cast
        browser.focus_index = 1           # first episode row
        assert browser.items[1]["type"] == "episode"
        parent = browser.items[1]["feed_id"]
        with patch("podcast_downloader.library.refresh") as refresh:
            refresh.return_value = SimpleNamespace(feed_id=parent, title="Alpha Cast", new_episode_ids=[])
            _run_headless(browser, "rq")
            _join_tasks()
        assert refresh.call_args.args[1] == parent

    def test_r_refreshes_only_the_focused_feed(self, populated_store):
        browser = _browser(populated_store)
        browser.focus_index = 0
        with patch("podcast_downloader.library.refresh") as refresh:
            refresh.return_value = SimpleNamespace(feed_id=1, title="Alpha Cast", new_episode_ids=[])
            _run_headless(browser, "rq")
            _join_tasks()
        assert refresh.call_count == 1

    def test_new_episodes_really_appear(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()
        before = len(browser.items)
        with patch("podcast_downloader.library.parse_feed",
                   return_value=self._feed(self._episode("g1"), self._episode("brand-new"))):
            browser._sync_feeds([browser.items[0]["id"]])
            _join_tasks()
        browser._refresh()
        assert len(browser.items) == before + 1
        assert any(ep["guid"] == "brand-new" for ep in populated_store.read()["episodes"])

    def test_sync_does_not_download(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.library.parse_feed",
                   return_value=self._feed(self._episode("brand-new"))), \
                patch("podcast_downloader.downloader.download_episode") as download:
            browser._sync_feeds([browser.items[0]["id"]])
            _join_tasks()
        download.assert_not_called()
        statuses = {ep["status"] for ep in populated_store.read()["episodes"]}
        assert statuses == {"pending"}

    def test_sync_runs_off_the_ui_thread(self, populated_store):
        browser = _browser(populated_store)
        release = threading.Event()
        threading.Timer(0.3, release.set).start()

        def slow(_store, feed_id):
            release.wait(5.0)
            return SimpleNamespace(feed_id=feed_id, title="Alpha Cast", new_episode_ids=[])

        with patch("podcast_downloader.library.refresh", side_effect=slow):
            began = time.monotonic()
            browser._sync_feeds([browser.items[0]["id"]])
            elapsed = time.monotonic() - began
            _join_tasks()
        assert elapsed < 0.2, "sync blocked the UI thread"

    def test_a_failing_feed_is_reported_not_raised(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.library.refresh", side_effect=RuntimeError("offline")):
            browser._sync_feeds([browser.items[0]["id"]])
            _join_tasks()
        assert "1 feed failed" in browser.flash

    def test_one_dead_feed_does_not_abort_the_others(self, populated_store):
        browser = _browser(populated_store)
        ids = [item["id"] for item in browser.items if item["type"] == "feed"]

        def flaky(_store, feed_id):
            if feed_id == ids[0]:
                raise RuntimeError("offline")
            return SimpleNamespace(feed_id=feed_id, title="Beta Cast", new_episode_ids=[9])

        with patch("podcast_downloader.library.refresh", side_effect=flaky) as refresh:
            browser._sync_feeds(ids)
            _join_tasks()
        assert refresh.call_count == len(ids), "a dead feed aborted the run"
        assert "Added 1 episode" in browser.flash and "1 feed failed" in browser.flash

    def test_a_second_sync_is_ignored_while_one_is_running(self, populated_store):
        browser = _browser(populated_store)
        release = threading.Event()
        threading.Timer(0.3, release.set).start()

        def slow(_store, feed_id):
            release.wait(5.0)
            return SimpleNamespace(feed_id=feed_id, title="Alpha Cast", new_episode_ids=[])

        with patch("podcast_downloader.library.refresh", side_effect=slow) as refresh:
            browser._sync_feeds([browser.items[0]["id"]])
            browser._sync_feeds([browser.items[0]["id"]])   # ignored
            _join_tasks()
        assert refresh.call_count == 1

    def test_menu_item_refreshes_every_subscription(self, populated_store):
        browser = _browser(populated_store, "menu")
        with patch("podcast_downloader.library.refresh") as refresh:
            refresh.side_effect = lambda _s, fid: SimpleNamespace(
                feed_id=fid, title="x", new_episode_ids=[])
            _run_headless(browser, _menu_keys("refresh") + "\rq")
            _join_tasks()
        assert refresh.call_count == 2
        assert browser.screen == "menu", "refresh should stay on the menu"

    def test_r_on_an_empty_library_says_so(self, store):
        browser = _browser(store)
        _run_headless(browser, "rq")
        assert "Nothing to refresh" in browser.sync_status

    def test_sync_clears_its_status_when_done(self, populated_store):
        browser = _browser(populated_store)
        with patch("podcast_downloader.library.refresh") as refresh:
            refresh.return_value = SimpleNamespace(feed_id=1, title="a", new_episode_ids=[])
            browser._sync_feeds([browser.items[0]["id"]])
            _join_tasks()
        assert browser.sync_status == ""
        assert browser.flash == "No new episodes"

    def test_focus_stays_on_the_same_row_when_episodes_arrive(self, populated_store):
        browser = _browser(populated_store)
        browser._expand()                       # Alpha Cast open: feed, ep, ep, Beta
        beta_index = next(i for i, item in enumerate(browser.items)
                          if item["type"] == "feed" and item["title"] == "Beta Cast")
        browser.focus_index = beta_index
        with patch("podcast_downloader.library.parse_feed",
                   return_value=self._feed(self._episode("g1"),
                                           self._episode("new-1"), self._episode("new-2"))):
            browser._sync_feeds([browser.items[0]["id"]])
            _join_tasks()
        browser._refresh()
        assert browser.items[browser.focus_index]["title"] == "Beta Cast", \
            "new rows above the cursor moved the selection"


class TestDownloadLatest:
    """The Download latest menu item: refresh every show, then fetch its newest episodes."""

    FEEDS = {
        "http://a.example/rss": {
            "title": "Alpha Cast", "author": "A", "artwork_url": "",
            "episodes": [
                {"guid": "g-new", "title": "Alpha Ep Three", "audio_url": "http://a.example/3.mp3",
                 "published": "2026-03-01", "duration": 60},
                {"guid": "g1", "title": "Alpha Ep One", "audio_url": "http://a.example/1.mp3",
                 "published": "2026-01-02T00:00:00Z", "duration": 65},
                {"guid": "g2", "title": "Alpha Ep Two", "audio_url": "http://a.example/2.mp3",
                 "published": "2026-01-01T00:00:00Z", "duration": 3725},
            ],
        },
        "http://b.example/rss": {
            "title": "Beta Cast", "author": "B", "artwork_url": "",
            "episodes": [{"guid": "g3", "title": "Beta Ep One", "audio_url": "http://b.example/1.mp3",
                          "published": None, "duration": None}],
        },
    }

    def _run(self, store, parse=None):
        browser = _browser(store, "menu")
        with patch("podcast_downloader.library.parse_feed", side_effect=parse or self.FEEDS.__getitem__), \
             patch("podcast_downloader.downloader.download_episode", return_value=True) as download:
            browser._sync_feeds(download_latest=True)
            _join_tasks()
            _join_tasks()  # the download worker is started by the sync thread
        return browser, sorted(call.args[0] for call in download.call_args_list)

    def _id(self, store, guid):
        return next(ep["id"] for ep in store.read()["episodes"] if ep["guid"] == guid)

    def test_refreshes_first_so_a_brand_new_episode_is_fetched(self, populated_store):
        populated_store.update_config(default_keep_latest=2)
        _browser, downloaded = self._run(populated_store)
        s = populated_store
        assert downloaded == sorted([self._id(s, "g-new"), self._id(s, "g1"), self._id(s, "g3")])

    def test_skips_episodes_already_downloaded(self, populated_store):
        populated_store.update_config(default_keep_latest=2)
        populated_store.update_episode_status(self._id(populated_store, "g1"), status="done")
        _browser, downloaded = self._run(populated_store)
        assert self._id(populated_store, "g1") not in downloaded

    def test_reports_how_many_it_queued(self, populated_store):
        populated_store.update_config(default_keep_latest=1)
        browser, downloaded = self._run(populated_store)
        assert len(downloaded) == 2
        assert "downloading 2" in browser.flash

    def test_a_show_that_fails_to_refresh_does_not_stop_the_others(self, populated_store):
        from podcast_downloader.feeds import FeedError

        def parse(url):
            if url == "http://a.example/rss":
                raise FeedError("offline")
            return self.FEEDS[url]

        browser, downloaded = self._run(populated_store, parse)
        assert downloaded == [self._id(populated_store, "g3")]
        assert "1 feed failed" in browser.flash

    def test_the_menu_item_starts_it(self, populated_store):
        browser = _browser(populated_store, "menu")
        with patch("podcast_downloader.library.parse_feed", side_effect=self.FEEDS.__getitem__), \
             patch("podcast_downloader.downloader.download_episode", return_value=True) as download:
            _run_headless(browser, _menu_keys("download_latest") + "\rq", timeout=5.0)
            _join_tasks()
        assert browser.screen == "menu"
        assert download.call_count > 0


class TestFormatAge:
    def test_blank_when_missing_or_unparseable(self):
        assert _format_age(None) == ""
        assert _format_age("") == ""
        assert _format_age("not a timestamp") == ""

    def test_recent_reads_as_just_now(self):
        assert _format_age(datetime.now(timezone.utc).isoformat()) == "just now"

    def test_minutes_hours_and_days(self):
        now = datetime.now(timezone.utc)
        assert _format_age((now - timedelta(minutes=20)).isoformat()) == "20m ago"
        assert _format_age((now - timedelta(hours=2)).isoformat()) == "2h ago"
        assert _format_age((now - timedelta(days=3)).isoformat()) == "3d ago"

    def test_a_naive_timestamp_is_treated_as_utc(self):
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        assert _format_age(naive.isoformat()) == "just now"

    def test_a_future_timestamp_does_not_crash(self):
        ahead = datetime.now(timezone.utc) + timedelta(hours=5)
        assert _format_age(ahead.isoformat()) == "just now"

    def test_the_age_reaches_the_browse_screen(self, populated_store):
        browser = _browser(populated_store)
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "just now" in text


class TestFeedRowWidth:
    def test_a_long_title_with_the_age_column_fits_eighty_columns(self, store):
        """The age column and the title share one 80-col line; the general
        width test uses short fixture titles and would not catch a regression
        in the truncation limit."""
        feed_id = store.add_feed(url="http://x/rss", title="T" * 120, author="A" * 60)
        for n in range(999):
            store.add_episode(feed_id=feed_id, guid=f"g{n}", title="E",
                              audio_url="http://x/e.mp3")
        browser = _browser(store)
        for line in split_lines(to_formatted_text(browser._render())):
            width = len(fragment_list_to_text(line))
            assert width <= 80, f"{width} cols: {fragment_list_to_text(line)!r}"

    def test_a_long_show_name_wraps_rather_than_truncating(self, store):
        title = " ".join(f"show{n}" for n in range(20))
        store.add_feed(url="http://x/rss", title=title)
        browser = _browser(store)
        text = fragment_list_to_text(to_formatted_text(browser._render()))
        assert "…" not in text, "a show name was truncated"
        for word in title.split():
            assert word in text, f"{word} was dropped from the wrapped show name"

    def test_the_episode_count_follows_the_last_line_of_a_wrapped_name(self, store):
        feed_id = store.add_feed(url="http://x/rss",
                                 title=" ".join(f"show{n}" for n in range(20)))
        store.add_episode(feed_id=feed_id, guid="g", title="E", audio_url="http://x/e.mp3")
        browser = _browser(store)
        lines = [fragment_list_to_text(l)
                 for l in split_lines(to_formatted_text(browser._render()))]
        counted = [l for l in lines if "(1 episode)" in l]
        assert len(counted) == 1, "the episode count should appear exactly once"
        assert "show19" in counted[0], "the count should trail the last line of the name"

    def test_an_unbroken_show_name_still_fits(self, store):
        store.add_feed(url="http://x/rss", title="T" * 300)
        browser = _browser(store)
        for line in split_lines(to_formatted_text(browser._render())):
            width = len(fragment_list_to_text(line))
            assert width <= 80, f"{width} cols: {fragment_list_to_text(line)!r}"


class TestWrapAndWidth:
    """Episode titles wrap to the real terminal width instead of truncating."""

    @staticmethod
    def _long_title(words=30):
        return " ".join(f"word{n}" for n in range(words))

    def _expanded(self, store, title=None):
        data = store.read()
        data["episodes"][0]["title"] = title or self._long_title()
        store.write(data)
        browser = _browser(store, "browse")
        browser._refresh()
        browser._expand()
        return browser

    def _lines(self, browser):
        return [fragment_list_to_text(line)
                for line in split_lines(to_formatted_text(browser._render()))]

    @pytest.mark.parametrize("cols", [80, 100, 120, 160, 200])
    def test_no_row_exceeds_the_terminal_width(self, populated_store, cols):
        browser = self._expanded(populated_store)
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, cols)):
            for line in self._lines(browser):
                assert len(line) <= cols, f"{len(line)} cols at width {cols}: {line!r}"

    @pytest.mark.parametrize("cols", [80, 120, 160])
    def test_the_whole_title_survives_at_every_width(self, populated_store, cols):
        title = self._long_title()
        browser = self._expanded(populated_store, title)
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, cols)):
            text = "\n".join(self._lines(browser))
        for word in title.split():
            assert word in text

    def test_a_wider_terminal_uses_fewer_lines(self, populated_store):
        browser = self._expanded(populated_store)
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, 80)):
            narrow = len(self._lines(browser))
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, 200)):
            wide = len(self._lines(browser))
        assert wide < narrow, "extra width was not used for the title"

    def test_continuation_lines_align_under_the_title(self, populated_store):
        browser = self._expanded(populated_store)
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, 100)):
            lines = self._lines(browser)
        first = next(l for l in lines if "word0" in l)
        follow = next(l for l in lines if "word0" not in l and "word" in l)
        assert first.index("word0") == len(follow) - len(follow.lstrip())

    def test_an_unbroken_word_still_fits(self, populated_store):
        """A URL or a run-on title has no spaces to break on."""
        browser = self._expanded(populated_store, "X" * 300)
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, 100)):
            for line in self._lines(browser):
                assert len(line) <= 100, f"{len(line)} cols: {line!r}"

    def test_a_very_narrow_terminal_does_not_crash(self, populated_store):
        browser = self._expanded(populated_store)
        for cols in (20, 30, 40):
            with patch.object(PodcastBrowser, "_terminal_size", return_value=(24, cols)):
                assert self._lines(browser), f"no output at {cols} cols"

    def test_undated_episodes_keep_the_title_column_aligned(self, populated_store):
        """Beta Cast's episode has published=None, which used to shift its
        title left out of line with the dated rows."""
        browser = _browser(populated_store, "browse")
        for item in list(browser.items):
            if item["type"] == "feed":
                browser.expanded_feed_ids.add(item["id"])
        browser._refresh()
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(40, 120)):
            lines = self._lines(browser)
        alpha = next(l for l in lines if "Alpha Ep One" in l)
        beta = next(l for l in lines if "Beta Ep One" in l)
        assert alpha.index("Alpha Ep One") == beta.index("Beta Ep One")


class TestViewportFitsTheScreen:
    def test_the_list_never_pushes_the_footer_off_screen(self, populated_store):
        data = populated_store.read()
        for ep in data["episodes"]:
            ep["title"] = " ".join(f"word{n}" for n in range(40))
        populated_store.write(data)
        browser = _browser(populated_store, "browse")
        browser._refresh()
        browser._expand()
        for rows in (12, 20, 24, 40):
            with patch.object(PodcastBrowser, "_terminal_size", return_value=(rows, 80)):
                lines = [fragment_list_to_text(l)
                         for l in split_lines(to_formatted_text(browser._render()))]
            assert len(lines) <= rows, f"{len(lines)} lines on a {rows}-row screen"
            assert "Selected:" in lines[-1], "the footer was pushed off screen"

    def test_the_focused_row_is_always_rendered(self, populated_store):
        data = populated_store.read()
        for ep in data["episodes"]:
            ep["title"] = " ".join(f"word{n}" for n in range(40))
        populated_store.write(data)
        browser = _browser(populated_store, "browse")
        browser._refresh()
        browser._expand()
        with patch.object(PodcastBrowser, "_terminal_size", return_value=(16, 80)):
            for index in range(len(browser.items)):
                browser.focus_index = index
                start, end = browser._get_viewport()
                assert start <= index < end, f"item {index} scrolled out of view"


class TestWrapHelper:
    def test_breaks_on_spaces(self):
        assert _wrap("aaa bbb ccc", 7) == ["aaa bbb", "ccc"]

    def test_breaks_a_word_longer_than_the_width(self):
        assert all(len(line) <= 5 for line in _wrap("X" * 23, 5))

    def test_never_returns_an_empty_list(self):
        assert _wrap("", 10) == [""]

    def test_a_nonsense_width_is_survivable(self):
        assert _wrap("hello", 0) == ["hello"]
