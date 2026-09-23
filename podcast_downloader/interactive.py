"""Interactive podcast browser with tree navigation and episode selection."""

from __future__ import annotations

import subprocess
import shutil
import sys
import textwrap
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import KEY_ALIASES
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style

from podcast_downloader import __version__


MENU_ITEMS = [
    ("search", "Search podcasts", "find a show and subscribe to it"),
    ("browse", "Browse subscriptions", "expand shows, download episodes"),
    ("refresh", "Refresh all feeds", "check every show for new episodes"),
    ("download_latest", "Download latest", "refresh every show and fetch its newest episodes"),
    ("remove", "Remove a subscription", "unsubscribe from a show"),
    ("open_folder", "Open download folder", "reveal the downloads directory"),
    ("settings", "Settings", "configure download directory, episodes to download"),
    ("quit", "Quit", ""),
]

SETTINGS_ITEMS = [
    ("set_download_dir", "Download directory", "where episodes are saved"),
    ("set_keep_latest", "Episodes to download per show", "how many of the newest episodes to fetch"),
    ("about", "About", "version and license"),
]

_STATUS_LABELS = {
    "downloading": "LOADING",
    "queued": "QUEUED",
}

PROGRESS_BAR_WIDTH = 14

# Used when no Application is running (tests calling _render directly) and as
# the floor for a terminal so narrow that a title column would vanish.
FALLBACK_WIDTH = 80
MIN_TITLE_WIDTH = 20


def _wrap(text: str, width: int) -> list[str]:
    """Split text into lines of at most `width` columns, breaking on spaces.

    textwrap drops a word longer than the width onto its own overlong line, so
    long_words are broken instead: a URL or an unspaced title must not push the
    row past the terminal edge and get clipped.
    """
    if width < 1:
        return [text]
    return textwrap.wrap(
        text, width=width, break_long_words=True, break_on_hyphens=False,
    ) or [""]


def _pluralise(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _format_size(num_bytes: float) -> str:
    """Human-readable byte count, e.g. 12.1 MB."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(num_bytes)} {unit}"
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def _format_age(timestamp: str | None) -> str:
    """How long ago an ISO timestamp was, e.g. 'just now', '2h ago', '3d ago'.

    Feeds subscribed before last_refreshed was written, or with a value we
    cannot parse, render as blank rather than guessing.
    """
    if not timestamp:
        return ""
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 0:
        return "just now"  # clock skew; claiming the future helps nobody
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _sync_summary(
    added: int,
    errors: list[str],
    updated_feeds: list[str] | None = None,
    queued: int | None = None,
) -> str:
    """The one-line result of a finished refresh; `queued` is set for Download latest."""
    if errors:
        suffix = f" ({_pluralise(len(errors), 'feed')} failed)"
    else:
        suffix = ""
    if added:
        if updated_feeds:
            feed_list = ", ".join(updated_feeds)
            summary = f"Added {_pluralise(added, 'episode')} from {feed_list}"
        else:
            summary = f"Added {_pluralise(added, 'episode')}"
    else:
        summary = "No new episodes"
    if queued is not None:
        summary += f" · downloading {queued}" if queued else " · already have the latest"
    return summary + suffix


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class PodcastBrowser:
    """Interactive tree-view browser for podcasts and episodes."""

    VISIBLE_LINES = 20

    def __init__(self, store):
        self.store = store
        self.scroll_offset: int = 0
        self.focus_index: int = 0
        self.expanded_feed_ids: set[int] = set()
        self.selected_episode_ids: set[int] = set()
        self.application: Application | None = None
        # ep_id -> {"status", "done", "total", "started", "error"}; written by the
        # download worker thread, read by the render pass on the UI thread.
        self.download_state: dict[int, dict[str, Any]] = {}
        self._queue: deque[int] = deque()
        self._queue_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        # Feed refresh runs on its own one-off thread; sync_status is a scalar
        # written from it and read by the render pass.
        self._sync_thread: threading.Thread | None = None
        self.sync_status: str = ""

        # Screen state: "menu" | "browse" | "search" | "remove" | "settings".
        self.screen: str = "menu"
        self.menu_index: int = 0
        self.settings_index: int = 0
        self.settings_editing: bool = False
        self.settings_edit_value: str = ""
        self.flash: str = ""
        # Search screen
        self.search_query: str = ""
        self.search_results: list[dict[str, str]] = []
        self.search_index: int = 0
        self.search_status: str = ""
        self.search_focus: str = "query"  # "query" while typing, "results" after
        # Remove screen
        self.remove_index: int = 0
        self.remove_confirm: dict[str, Any] | None = None
        # Move confirmation (triggered by download directory change)
        self._move_confirm: tuple[str, str, int, int] | None = None
        self._move_waiting: bool = False

        self._build_items()

    def _build_items(self) -> None:
        """Rebuild the flat item list, nesting episodes under expanded feeds."""
        data = self.store.read()
        self.feeds = data.get("feeds", [])
        episodes = data.get("episodes", [])
        self.items: list[dict[str, Any]] = []
        for feed in self.feeds:
            feed_episodes = [ep for ep in episodes if ep["feed_id"] == feed["id"]]
            self.items.append({
                "type": "feed",
                "id": feed["id"],
                "title": feed["title"],
                "author": feed.get("author", ""),
                "episode_count": len(feed_episodes),
                "last_refreshed": feed.get("last_refreshed"),
            })
            if feed["id"] not in self.expanded_feed_ids:
                continue
            feed_episodes.sort(key=lambda e: e.get("published") or "", reverse=True)
            for ep in feed_episodes:
                self.items.append({
                    "type": "episode",
                    "id": ep["id"],
                    "feed_id": feed["id"],
                    "title": ep["title"],
                    "status": ep["status"],
                    "published": ep.get("published") or "",
                    "duration": ep.get("duration"),
                    "selected": ep["id"] in self.selected_episode_ids,
                })
        self._clamp_focus()

    def _index_of_feed(self, feed_id: int) -> int:
        for i, item in enumerate(self.items):
            if item["type"] == "feed" and item["id"] == feed_id:
                return i
        return self.focus_index

    def _terminal_size(self) -> tuple[int, int]:
        """(rows, columns) of the real terminal, or a sane default.

        Read per render rather than cached: the user can resize the window at
        any time, and prompt_toolkit redraws when they do.
        """
        app = self.application
        if app is not None and app.output is not None:
            try:
                size = app.output.get_size()
                if size.columns > 0 and size.rows > 0:
                    return size.rows, size.columns
            except Exception:
                pass  # DummyOutput and friends; fall through to the default
        return 24, FALLBACK_WIDTH

    def _width(self) -> int:
        return self._terminal_size()[1]

    def _list_line_budget(self) -> int:
        """How many lines the item list may occupy.

        The screen is chrome (3) + list + separator and footer (2). Overflowing
        it would push the footer - which carries the sync status and the
        selected count - off the bottom, where a plain Window clips rather
        than scrolls.
        """
        rows = self._terminal_size()[0]
        return max(1, rows - 6)

    def _item_height(self, index: int) -> int:
        """How many rendered lines item `index` takes at the current width."""
        return len(self._render_item(index))

    def _get_viewport(self) -> tuple[int, int]:
        """Return (start_index, end_index) of the items to draw.

        Line-aware, not item-aware: a wrapped episode title or an inline
        progress bar makes a row taller than one line, so a fixed item count
        would overflow the screen.
        """
        if not self.items:
            return 0, 0
        budget = self._list_line_budget()

        # Walk back from the focused item so it stays on screen, keeping a few
        # rows of context above it where the budget allows.
        start = self.focus_index
        used = self._item_height(start)
        context = 0
        while start > 0 and context < 5:
            height = self._item_height(start - 1)
            if used + height > budget:
                break
            start -= 1
            used += height
            context += 1

        # Then fill downwards with whatever is left.
        end = self.focus_index + 1
        while end < len(self.items):
            height = self._item_height(end)
            if used + height > budget:
                break
            used += height
            end += 1

        # And back up again if that left room above.
        while start > 0:
            height = self._item_height(start - 1)
            if used + height > budget:
                break
            start -= 1
            used += height
        return start, end

    def _render(self) -> StyleAndTextTuples:
        """Build one flat fragment list; FormattedTextControl needs explicit newlines."""
        renderers = {
            "menu": self._render_menu,
            "search": self._render_search,
            "remove": self._render_remove,
            "settings": self._render_settings,
            "about": self._render_about,
        }
        lines = renderers.get(self.screen, self._render_browse)()

        fragments: StyleAndTextTuples = []
        for line_no, line in enumerate(lines):
            if line_no:
                fragments.append(("", "\n"))
            fragments.extend(line)
        return fragments

    def _chrome(self, title: str, help_text: str) -> list[StyleAndTextTuples]:
        """Title bar, key hints and a rule, shared by every screen."""
        return [
            [("class:title", f"  {title} ")],
            [("class:help", f"  {help_text}")],
            [("class:separator", "  " + "-" * (self._width() - 4))],
        ]

    def _render_menu(self) -> list[StyleAndTextTuples]:
        lines = self._chrome("Podcast Downloader", "↑/↓ navigate  ↵ select  Q quit")
        lines.append([("", "")])
        for i, (key, label, hint) in enumerate(MENU_ITEMS):
            if key == "settings":
                lines.append([("class:separator", "  " + "-" * (self._width() - 4))])
            focused = (i == self.menu_index)
            lines.append([
                ("class:marker", "▸ " if focused else "  "),
                ("class:menu-item-selected" if focused else "class:menu-item", f"  {label:<24}"),
                ("class:muted", hint),
            ])
        lines.append([("", "")])
        lines.append([("class:separator", "  " + "-" * (self._width() - 4))])

        feeds = len(self.feeds)
        summary: StyleAndTextTuples = [
            ("class:footer", "  "),
            ("class:count", str(feeds)),
            ("class:footer", " subscription" if feeds == 1 else " subscriptions"),
        ]
        active = self._active_download_count()
        if active:
            summary.append(("class:download", f"   Downloading: {active}"))
        if self.sync_status:
            summary.append(("class:download", f"   {self.sync_status}"))
        if self.flash:
            summary.append(("class:done", f"   {self.flash}"))
        lines.append(summary)
        return lines

    def _render_search(self) -> list[StyleAndTextTuples]:
        if self.search_focus == "results":
            help_text = "↑/↓ navigate  ↵ subscribe  type to edit search  Esc back"
        else:
            help_text = "type a search  ↵ search  Esc back"
        lines = self._chrome("Search Podcasts", help_text)

        cursor = "█" if self.search_focus == "query" else ""
        lines.append([
            ("class:footer", "  Search: "),
            ("class:query", self.search_query or ""),
            ("class:marker", cursor),
        ])
        lines.append([("", "")])

        if self.search_status:
            style = "class:error" if self.search_status.startswith("✗") else "class:muted"
            lines.append([("", "  "), (style, self.search_status)])
            lines.append([("", "")])

        # Hoisted out of the loop: this used to be a store read per result per
        # frame, and frames fire on every download progress chunk.
        subscribed_urls = {feed["url"] for feed in self.feeds}
        for i, result in enumerate(self.search_results[:self.VISIBLE_LINES]):
            focused = (i == self.search_index and self.search_focus == "results")
            title = result["title"][:38]
            if len(result["title"]) > 38:
                title += "…"
            author = (result.get("author") or "")[:20]
            subscribed = result.get("feed_url", "") in subscribed_urls
            lines.append([
                ("class:marker", "▸ " if focused else "  "),
                ("class:episode-selected" if focused else "class:episode", f"  {title:<40}"),
                ("class:muted", f"{author:<21}"),
                ("class:done", "subscribed" if subscribed else ""),
            ])
        return lines

    def _render_remove(self) -> list[StyleAndTextTuples]:
        if self.remove_confirm is not None:
            lines = self._chrome("Remove Subscription", "Y remove  N cancel")
            title = self.remove_confirm["title"]
            lines.append([("", "")])
            lines.append([("class:error", f"  Remove '{title[:50]}'?")])
            lines.append([("class:muted", "  Downloaded episodes stay on disk.")])
            return lines

        lines = self._chrome("Remove Subscription", "↑/↓ navigate  ↵ remove  Esc back")
        if not self.feeds:
            lines.append([("class:muted", "  No subscriptions.")])
            return lines

        counts = {row["id"]: row["episode_count"] for row in self.items if row["type"] == "feed"}
        for i, feed in enumerate(self.feeds[:self.VISIBLE_LINES]):
            focused = (i == self.remove_index)
            title = feed["title"][:38]
            if len(feed["title"]) > 38:
                title += "…"
            lines.append([
                ("class:marker", "▸ " if focused else "  "),
                ("class:feed-selected" if focused else "class:feed", f"  {title:<40}"),
                ("class:muted", f"{(feed.get('author') or '')[:20]:<21}"),
                ("class:muted", _pluralise(counts.get(feed["id"], 0), "episode")),
            ])
        return lines

    def _render_about(self) -> list[StyleAndTextTuples]:
        lines = self._chrome("About", "Esc back")
        lines.append([("", "")])
        lines.append([("class:menu-item-selected", "  Podcast Downloader  "), ("class:version", f"v{__version__}")])
        lines.append([("class:muted", "  Subscribe to podcasts and download episodes as named, tagged audio files.")])
        lines.append([("", "")])
        lines.append([("class:muted", "  MIT License")])
        return lines

    def _render_settings(self) -> list[StyleAndTextTuples]:
        if self.settings_editing:
            lines = self._chrome("Settings", "type value  Esc cancel  ↑/↓ navigate")
            lines.append([("", "")])
            if self.settings_index == 0 and self._move_waiting and self._move_confirm:
                # From the snapshot taken at save time: the config already
                # holds the new directory, and stat-ing every file on every
                # redraw would be slow.
                old_dir, new_dir, count, total_size = self._move_confirm
                lines.append([("", "")])
                lines.append([(
                    "class:settings-footer",
                    f"  Moving {count} episodes ({_format_size(total_size)}) from {old_dir} to {new_dir}.",
                )])
                lines.append([("", "")])
                lines.append([("class:settings-footer", "  Move files? (Y/n)  Esc=no")])
                return lines
            if self.settings_index == 0:
                data = self.store.read()
                old_dir = data["config"].get("download_dir", "")
                episodes = [ep for ep in data.get("episodes", []) if ep.get("file_path")]
                existing = [ep for ep in episodes if Path(ep["file_path"]).exists()]
                total_size = sum(
                    Path(ep["file_path"]).stat().st_size for ep in existing
                )
                lines.append([("class:settings-label", f"  Current: {old_dir} ({len(existing)} episodes, {_format_size(total_size)})")])
                lines.append([("", "")])
                lines.append([
                    ("class:settings-label", "  New: "),
                    ("class:query", self.settings_edit_value),
                    ("class:marker", "█"),
                ])
            if self.settings_index == 1:
                data = self.store.read()
                current = data["config"].get("default_keep_latest", 3)
                lines.append([
                    ("class:settings-label", f"  Current: {current}  New: "),
                    ("class:query", self.settings_edit_value),
                    ("class:marker", "█"),
                ])
            lines.append([("class:settings-footer", "  Enter to save  Esc to cancel")])
            return lines

        lines = self._chrome("Settings", "↑/↓ navigate  ↵ edit  Esc back")
        lines.append([("", "")])
        label_width = max(24, max(len(label) for _key, label, _hint in SETTINGS_ITEMS) + 2)
        for i, (key, label, hint) in enumerate(SETTINGS_ITEMS):
            if key == "about":
                lines.append([("class:separator", "  " + "-" * (self._width() - 4))])
            focused = (i == self.settings_index)
            lines.append([
                ("class:marker", "▸ " if focused else "  "),
                ("class:menu-item-selected" if focused else "class:menu-item", f"  {label:<{label_width}}"),
                ("class:settings-label", hint),
            ])
        return lines



    # Fixed furniture on an episode row: marker, checkbox, gap, [id], date,
    # status column and the gap before the title. Measured, not guessed - see
    # _render_item, which lays them out in this order.
    def _episode_prefix_width(self, ep_id: int) -> int:
        return 2 + 2 + 2 + len(f"[{ep_id}]") + 12 + 9 + 2

    def _render_item(self, index: int) -> list[StyleAndTextTuples]:
        """Render one item as one or more lines.

        An episode title is wrapped rather than truncated, so a row can be
        several lines tall. _get_viewport measures rows through this method,
        so the two can never disagree about how much space a row takes.
        """
        item = self.items[index]
        is_focused = (index == self.focus_index)
        marker = "▸ " if is_focused else "  "
        check = "✓ " if item.get("selected") else "  "
        width = self._width()

        if item["type"] == "feed":
            expanded = "▼" if item["id"] in self.expanded_feed_ids else "▶"
            style = "class:feed-selected" if is_focused else "class:feed"
            count = item["episode_count"]
            ep_info = f" ({count} episode)" if count == 1 else f" ({count} episodes)"
            age = _format_age(item.get("last_refreshed"))
            age_text = f"  {age}" if age else ""

            # Marker plus "  ▶ ". Wrapped at the tighter budget throughout, so
            # that wherever the last line ends, the counts and age still fit
            # after it.
            prefix_width = 6
            budget = max(MIN_TITLE_WIDTH,
                         width - prefix_width - len(ep_info) - len(age_text))
            wrapped = _wrap(item["title"], budget)

            lines: list[StyleAndTextTuples] = [[
                ("class:marker", marker),
                (style, f"  {expanded} "),
                (style, wrapped[0]),
            ]]
            if len(wrapped) == 1:
                lines[0].append(("class:muted", ep_info))
                lines[0].append(("class:muted", age_text))
            for n, continuation in enumerate(wrapped[1:], start=1):
                parts: StyleAndTextTuples = [
                    ("", " " * prefix_width),
                    (style, continuation),
                ]
                if n == len(wrapped) - 1:
                    parts.append(("class:muted", ep_info))
                    parts.append(("class:muted", age_text))
                lines.append(parts)
            return lines

        ep_id = item["id"]
        state = self.download_state.get(ep_id)
        status = state["status"] if state else item["status"]
        published = item["published"] or "N/A"
        dur = _format_duration(item.get("duration"))
        dur_text = f"  ({dur})" if dur else ""

        status_style = {
            "done": "class:done",
            "error": "class:error",
            "downloading": "class:download",
            "queued": "class:download",
        }.get(status, "class:muted")
        status_label = _STATUS_LABELS.get(status, status.upper())
        title_style = "class:episode-selected" if is_focused else "class:episode"

        prefix_width = self._episode_prefix_width(ep_id)
        budget = max(MIN_TITLE_WIDTH, width - prefix_width - len(dur_text))
        wrapped = _wrap(item["title"], budget)

        lines: list[StyleAndTextTuples] = [[
            ("class:marker", marker),
            ("class:check", check),
            ("", "  "),
            ("class:id", f"[{ep_id}]"),
            # Padded, not just truncated: an undated episode shows "N/A" and
            # would otherwise pull the title column left, out of alignment.
            ("class:muted", f"  {published[:10]:<10}"),
            (status_style, f"  {status_label:<7}"),
            (title_style, f"  {wrapped[0]}"),
        ]]
        # The duration trails the last line of the title, not the first, so it
        # never lands in the middle of a wrapped sentence.
        if len(wrapped) == 1 and dur_text:
            lines[0].append(("class:muted", dur_text))

        for n, continuation in enumerate(wrapped[1:], start=1):
            parts: StyleAndTextTuples = [
                ("", " " * prefix_width),
                (title_style, continuation),
            ]
            if n == len(wrapped) - 1 and dur_text:
                parts.append(("class:muted", dur_text))
            lines.append(parts)

        # Inline progress on its own line, so it can't be clipped by a long
        # episode title.
        if state is not None and status != "done":
            lines.append(self._progress_line(state))
        return lines

    def _render_browse(self) -> list[StyleAndTextTuples]:
        lines: list[StyleAndTextTuples] = []
        start, end = self._get_viewport()
        self.scroll_offset = start

        lines.extend(self._chrome(
            "Browse Subscriptions",
            "↑/↓ move  ↵ open/download  Space select  D download  R refresh  Esc back",
        ))

        # Visible items
        for i in range(start, end):
            lines.extend(self._render_item(i))

        # Footer (always visible at bottom)
        lines.append([("class:separator", "  " + "-" * (self._width() - 4))])
        selected = len(self.selected_episode_ids)
        footer: StyleAndTextTuples = [
            ("class:footer", "  Selected: "),
            ("class:count", str(selected)),
            ("class:footer", " episode" if selected == 1 else " episodes"),
        ]
        active = self._active_download_count()
        if active:
            footer.append(("class:download", f"   Downloading: {active}"))
        if self.sync_status:
            footer.append(("class:download", f"   {self.sync_status}"))
        elif self.flash:
            footer.append(("class:done", f"   {self.flash}"))
        else:
            footer.append(("class:footer", "   [D] download selected  [Esc] menu"))
        lines.append(footer)
        return lines

    def _progress_line(self, state: dict[str, Any]) -> StyleAndTextTuples:
        """One indented line showing a queued/downloading/failed episode's progress."""
        indent = ("", " " * 14)
        status = state["status"]

        if status == "queued":
            return [indent, ("class:muted", "queued…")]

        if status == "error":
            message = (state.get("error") or "download failed").replace("\n", " ")
            return [indent, ("class:error", f"✗ {message[:58]}")]

        done = state.get("done") or 0
        total = state.get("total")
        parts: StyleAndTextTuples = [indent]

        if total:
            fraction = min(1.0, done / total)
            filled = int(fraction * PROGRESS_BAR_WIDTH)
            parts.append(("class:bar", "█" * filled))
            parts.append(("class:bar-empty", "░" * (PROGRESS_BAR_WIDTH - filled)))
            parts.append(("class:download", f"  {fraction * 100:3.0f}%"))
            parts.append(("class:muted", f"  {_format_size(done)} / {_format_size(total)}"))
        else:
            parts.append(("class:download", f"{_format_size(done)} downloaded"))

        elapsed = time.monotonic() - (state.get("started") or time.monotonic())
        if elapsed > 0.5 and done:
            parts.append(("class:muted", f"  {_format_size(done / elapsed)}/s"))
        return parts

    def _active_download_count(self) -> int:
        return sum(
            1 for state in self.download_state.values()
            if state["status"] in ("queued", "downloading")
        )

    # ------------------------------------------------------------------ menu

    def _on_key_menu(self, key: str, event) -> None:
        if key in ("q", "c-c"):
            event.app.exit()
        elif key == "up":
            self.menu_index = max(0, self.menu_index - 1)
        elif key == "down":
            self.menu_index = min(len(MENU_ITEMS) - 1, self.menu_index + 1)
        elif key == "enter":
            self._choose_menu_item(MENU_ITEMS[self.menu_index][0], event)

    def _execute_move(self) -> None:
        if self._move_confirm is None:
            self._move_waiting = False
            return
        old_dir, new_dir, count, _ = self._move_confirm
        try:
            Path(new_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.flash = f"✗ Could not create {new_dir}: {exc}"
            self._move_confirm = None
            self._move_waiting = False
            return

        moved: dict[int, str] = {}
        for ep in self.store.read().get("episodes", []):
            old_path = ep.get("file_path")
            if not old_path or not Path(old_path).exists():
                continue
            try:
                folder = Path(old_path).parent.name
                new_path = Path(new_dir) / folder / Path(old_path).name
                new_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_path), str(new_path))
                moved[ep["id"]] = str(new_path)
            except OSError:
                pass

        # Only the paths: the moves can take a while, and saving the snapshot
        # read above would undo anything a download or refresh wrote meanwhile.
        self.store.update_file_paths(moved)
        self.flash = f"✓ Moved {len(moved)} episodes to {new_dir}"
        self._move_confirm = None
        self._move_waiting = False
        self._cancel_settings_edit()
        self._refresh()

    def _choose_menu_item(self, action: str, event) -> None:
        self.flash = ""
        if action == "quit":
            event.app.exit()
        elif action == "search":
            self.screen = "search"
            self.search_focus = "query"
            self.search_status = ""
        elif action == "browse":
            self._refresh()
            self.screen = "browse"
        elif action == "refresh":
            # Stay on the menu so the progress counter is visible.
            self._sync_feeds()
        elif action == "download_latest":
            self._sync_feeds(download_latest=True)
        elif action == "remove":
            self._refresh()
            self.remove_index = 0
            self.remove_confirm = None
            self.screen = "remove"
        elif action == "open_folder":
            self._open_download_folder()
        elif action == "settings":
            self.settings_index = 0
            self.settings_editing = False
            self.screen = "settings"

    def _on_key_settings(self, key: str, event) -> None:
        if key == "c-c":
            event.app.exit()
        elif key == "escape":
            if self.settings_editing:
                self._cancel_settings_edit()
            else:
                self._to_menu()
        elif self.settings_editing and self._move_waiting:
            self._handle_move_confirmation(key)
        elif self.settings_editing:
            # Before the "q" check: q is an ordinary letter in a typed path.
            self._handle_settings_edit(key, event)
        elif key == "q":
            event.app.exit()
        elif key == "up":
            self.settings_index = max(0, self.settings_index - 1)
        elif key == "down":
            self.settings_index = min(len(SETTINGS_ITEMS) - 1, self.settings_index + 1)
        elif key == "enter":
            if SETTINGS_ITEMS[self.settings_index][0] == "about":
                self.screen = "about"
            else:
                self.settings_editing = True
                self.settings_edit_value = ""

    def _on_key_about(self, key: str, event) -> None:
        if key in ("q", "c-c"):
            event.app.exit()
        elif key == "escape":
            self.screen = "settings"

    def _cancel_settings_edit(self) -> None:
        self.settings_editing = False
        self.settings_edit_value = ""
        self._move_confirm = None
        self._move_waiting = False

    def _handle_move_confirmation(self, key: str) -> None:
        if key in ("y", "Y"):
            self._execute_move()
        elif key in ("n", "N", "escape"):
            self._move_confirm = None
            self._move_waiting = False
            self._cancel_settings_edit()
            self.flash = "Cancelled"
        elif key == "backspace":
            pass  # ignore backspace during move confirmation

    def _handle_settings_edit(self, key: str, event) -> None:
        if key == "enter":
            self._save_settings()
        elif key in ("backspace", "c-h"):
            self.settings_edit_value = self.settings_edit_value[:-1]
        elif len(key) == 1:
            self.settings_edit_value += key
        elif key == "end":
            pass  # cursor at end, no-op
        elif key == "home":
            pass  # cursor at start, no-op
        elif key == "left":
            pass  # cursor movement, no-op in simple editor
        elif key == "right":
            pass  # cursor movement, no-op in simple editor

    def _save_settings(self) -> None:
        new_value = self.settings_edit_value.strip()
        if not new_value:
            self.flash = "✗ Value cannot be empty"
            return

        data = self.store.read()
        setting_key = SETTINGS_ITEMS[self.settings_index][0]

        if setting_key == "set_download_dir":
            from podcast_downloader.store import expand_dir

            old_dir = data["config"].get("download_dir", "")
            new_value = expand_dir(new_value)
            if old_dir == new_value:
                self.flash = "✓ Download directory unchanged"
                self._cancel_settings_edit()
                return
            self.store.update_config(download_dir=new_value)
            self._prompt_move_files(old_dir, new_value)
        elif setting_key == "set_keep_latest":
            try:
                n = int(new_value)
                if n < 1:
                    self.flash = "✗ Must be at least 1"
                    return
            except ValueError:
                self.flash = "✗ Must be a number"
                return
            self.store.update_config(default_keep_latest=n)
            self.flash = f"✓ Episodes to download per show changed to {n}"
            self._cancel_settings_edit()

    def _prompt_move_files(self, old_dir: str, new_dir: str) -> None:
        episodes = [ep for ep in self.store.read().get("episodes", []) if ep.get("file_path")]
        existing = [ep for ep in episodes if Path(ep["file_path"]).exists()]
        total_size = sum(
            Path(ep["file_path"]).stat().st_size for ep in existing
        )
        count = len(existing)
        self.flash = (
            f"Moving {count} episodes ({_format_size(total_size)}) from {old_dir} to {new_dir}. Move? (Y/n)"
        )
        self._move_confirm = (old_dir, new_dir, count, total_size)
        self._move_waiting = True

    def _on_key(self, event) -> None:
        key = event.key_sequence[-1].key
        # prompt_toolkit delivers Enter as its ControlM alias (Keys is a str enum).
        if key == KEY_ALIASES["enter"]:
            key = "enter"

        handlers = {
            "menu": self._on_key_menu,
            "search": self._on_key_search,
            "remove": self._on_key_remove,
            "settings": self._on_key_settings,
            "about": self._on_key_about,
        }
        handlers.get(self.screen, self._on_key_browse)(key, event)

    def _to_menu(self) -> None:
        self._refresh()
        self.screen = "menu"

    def _open_download_folder(self) -> None:
        """Reveal the configured download directory in the OS file manager.

        Stays on the menu screen (like refresh) so the flash message showing
        success or failure is visible without an extra keypress.
        """
        download_dir = self.store.read()["config"]["download_dir"]
        path = Path(download_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.flash = f"✗ Could not create {download_dir}: {exc}"
            return

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen(
                [opener, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.flash = f"✗ No '{opener}' command found"
            return
        self.flash = f"Opened {download_dir}"

    # ---------------------------------------------------------------- search

    def _on_key_search(self, key: str, event) -> None:
        if key == "escape":
            self._to_menu()
        elif key == "c-c":
            event.app.exit()
        elif key == "enter":
            if self.search_focus == "results" and self.search_results:
                self._subscribe_to(self.search_results[self.search_index])
            else:
                self._run_search()
        elif key == "up" and self.search_focus == "results":
            self.search_index = max(0, self.search_index - 1)
        elif key == "down" and self.search_focus == "results":
            self.search_index = min(len(self.search_results) - 1, self.search_index + 1)
        elif key in ("backspace", "c-h"):
            self.search_focus = "query"
            self.search_query = self.search_query[:-1]
        elif len(str(event.data)) == 1 and str(event.data).isprintable():
            self.search_focus = "query"
            self.search_query += str(event.data)

    def _run_search(self) -> None:
        query = self.search_query.strip()
        if not query or self.search_status == "searching\u2026":
            return
        self.search_status = "searching\u2026"
        self.search_results = []
        self.search_index = 0
        self._invalidate()

        def work() -> None:
            from podcast_downloader.feeds import search_podcasts
            try:
                results = search_podcasts(query)
            except Exception as exc:
                self.search_status = f"\u2717 search failed: {exc}"
                self._invalidate()
                return
            self.search_results = results
            self.search_index = 0
            if results:
                self.search_status = ""
                self.search_focus = "results"
            else:
                self.search_status = "No results."
            self._invalidate()

        self._run_in_background(work)

    def _subscribe_to(self, result: dict[str, str]) -> None:
        url = result.get("feed_url", "")
        if not url:
            self.search_status = "\u2717 That result has no feed URL."
            return
        title = result.get("title", url)
        self.search_status = f"Subscribing to {title[:40]}\u2026"
        self._invalidate()

        def work() -> None:
            from podcast_downloader.library import LibraryError, subscribe
            try:
                added = subscribe(self.store, url)
            except LibraryError as exc:
                self.search_status = f"\u2717 {exc}"
                self._invalidate()
                return
            except Exception as exc:
                self.search_status = f"\u2717 {exc}"
                self._invalidate()
                return
            self.search_status = ""
            self.flash = f"Added '{added.title}' ({added.episode_count} episodes)"
            self._call_on_ui_thread(self._refresh)

        self._run_in_background(work)

    # ------------------------------------------------------------------ sync

    def _syncing(self) -> bool:
        thread = self._sync_thread
        return thread is not None and thread.is_alive()

    def _sync_feeds(self, feed_ids: list[int] | None = None, download_latest: bool = False) -> None:
        """Re-fetch feeds in the background, adding episodes we have not seen.

        Passing None refreshes every subscription. With download_latest, each
        show's newest episodes (the Settings count) are queued for download
        as soon as that show has been refreshed. Only the UI thread calls
        this, so the _syncing() check cannot race with another launch.
        """
        if self._syncing():
            return
        if feed_ids is None:
            feeds = self.feeds
        else:
            wanted = set(feed_ids)
            feeds = [f for f in self.feeds if f["id"] in wanted]
        # Snapshot (id, title) on the UI thread so the worker needs no store
        # read just to label its progress line.
        targets = [(f["id"], f["title"]) for f in feeds]
        if not targets:
            self.sync_status = "Nothing to refresh"
            self._invalidate()
            return

        self.sync_status = f"Checking {_pluralise(len(targets), 'feed')}…"
        self._invalidate()
        self._sync_thread = self._run_in_background(
            lambda: self._sync_worker(targets, download_latest), name="podcast-sync",
        )

    def _sync_worker(self, targets: list[tuple[int, str]], download_latest: bool = False) -> None:
        """Refresh each feed in turn, reporting progress as we go.

        Runs off the UI thread. Every failure becomes a status string: an
        exception escaping here is an unhandled thread exception, which
        pytest.ini turns into a suite error. One dead feed must not abort the
        rest, so failures are collected and reported in the summary - writing
        them to sync_status directly would be overwritten milliseconds later
        by the next feed's progress line.
        """
        from podcast_downloader.library import LibraryError, episodes_to_fetch, refresh

        total = len(targets)
        added = 0
        queued = 0
        errors: list[str] = []
        updated_feeds: list[str] = []
        try:
            count = self.store.read()["config"].get("default_keep_latest", 3)
            for position, (feed_id, title) in enumerate(targets, start=1):
                if total > 1:
                    self.sync_status = f"Checking {position}/{total} · {title[:28]}…"
                else:
                    self.sync_status = f"Checking {title[:28]}…"
                self._invalidate()
                try:
                    result = refresh(self.store, feed_id)
                    if result.new_episode_ids:
                        added += len(result.new_episode_ids)
                        updated_feeds.append(title)
                        # Rebuilding touches self.items, so it belongs on the
                        # UI thread. Per feed, never per episode:
                        # _build_items is a full store read.
                        self._call_on_ui_thread(self._refresh)
                    if download_latest:
                        wanted = [ep["id"] for ep in episodes_to_fetch(self.store, feed_id, count)]
                        if wanted:
                            queued += len(wanted)
                            self._call_on_ui_thread(lambda ids=wanted: self._start_downloads(ids))
                except LibraryError as exc:
                    errors.append(str(exc))
                except Exception as exc:  # defensive: never kill this thread
                    errors.append(f"{title[:28]}: {exc}")
        finally:
            self.sync_status = ""
            self.flash = _sync_summary(added, errors, updated_feeds,
                                       queued if download_latest else None)
            self._call_on_ui_thread(self._refresh)

    # ---------------------------------------------------------------- remove

    def _on_key_remove(self, key: str, event) -> None:
        if self.remove_confirm is not None:
            if key in ("y", "Y"):
                self._confirm_removal()
            elif key in ("n", "N", "escape"):
                self.remove_confirm = None
            elif key == "c-c":
                event.app.exit()
            return

        if key == "escape":
            self._to_menu()
        elif key in ("q", "c-c"):
            event.app.exit()
        elif key == "up":
            self.remove_index = max(0, self.remove_index - 1)
        elif key == "down":
            self.remove_index = min(len(self.feeds) - 1, self.remove_index + 1)
        elif key == "enter" and self.feeds:
            self.remove_confirm = self.feeds[self.remove_index]

    def _confirm_removal(self) -> None:
        from podcast_downloader.library import LibraryError, unsubscribe

        feed = self.remove_confirm
        self.remove_confirm = None
        if feed is None:
            return
        try:
            unsubscribe(self.store, feed["id"])
        except LibraryError as exc:
            self.flash = f"\u2717 {exc}"
            return

        # Forget any UI state that referred to the feed we just dropped.
        self.expanded_feed_ids.discard(feed["id"])
        self._refresh()
        self.remove_index = max(0, min(self.remove_index, len(self.feeds) - 1))
        self.flash = f"Removed '{feed['title'][:40]}'"

    # ---------------------------------------------------------------- browse

    def _on_key_browse(self, key: str, event) -> None:
        if key == "escape":
            self._to_menu()
            return
        if key in ("q", "c-c"):
            event.app.exit()
            return
        if key == "r":
            # Before the empty-library guard, so 'r' can say why nothing happened.
            self._sync_focused_feed()
            return
        if not self.items:
            return
        if key == "enter":
            self._activate()
        elif key == "d":
            self._download_selected()
        elif key == "up":
            self.focus_index = max(0, self.focus_index - 1)
        elif key == "down":
            self.focus_index = min(len(self.items) - 1, self.focus_index + 1)
        elif key == "right":
            self._expand()
        elif key == "left":
            self._collapse()
        elif key == " ":
            self._toggle_select()

    def _sync_focused_feed(self) -> None:
        """'r' on browse: refresh the focused show, or an episode's own show."""
        if not self.items:
            self.sync_status = "Nothing to refresh"
            self._invalidate()
            return
        item = self.items[self.focus_index]
        feed_id = item["id"] if item["type"] == "feed" else item["feed_id"]
        self._sync_feeds([feed_id])

    def _run_in_background(self, work, name: str = "podcast-task") -> threading.Thread:
        """Run a one-off network task off the UI thread."""
        thread = threading.Thread(target=work, name=name, daemon=True)
        thread.start()
        return thread

    def _activate(self) -> None:
        """Enter: open/close a feed, or download the focused episode."""
        if self.focus_index < 0 or self.focus_index >= len(self.items):
            return
        item = self.items[self.focus_index]
        if item["type"] == "feed":
            if item["id"] in self.expanded_feed_ids:
                self._collapse()
            else:
                self._expand()
        else:
            self._start_downloads([item["id"]])

    def _expand(self) -> None:
        if self.focus_index < 0 or self.focus_index >= len(self.items):
            return
        item = self.items[self.focus_index]
        if item["type"] != "feed" or item["id"] in self.expanded_feed_ids:
            return
        feed_id = item["id"]
        self.expanded_feed_ids.add(feed_id)
        self._build_items()
        self.focus_index = self._index_of_feed(feed_id)

    def _collapse(self) -> None:
        if self.focus_index < 0 or self.focus_index >= len(self.items):
            return
        item = self.items[self.focus_index]
        if item["type"] == "episode":
            feed_id = item["feed_id"]
        elif item["id"] in self.expanded_feed_ids:
            feed_id = item["id"]
        else:
            return
        self.expanded_feed_ids.discard(feed_id)
        self._build_items()
        self.focus_index = self._index_of_feed(feed_id)

    def _toggle_select(self) -> None:
        if self.focus_index < 0 or self.focus_index >= len(self.items):
            return
        item = self.items[self.focus_index]
        if item["type"] == "episode":
            ep_id = item["id"]
            if ep_id in self.selected_episode_ids:
                self.selected_episode_ids.discard(ep_id)
            else:
                self.selected_episode_ids.add(ep_id)
            index = self.focus_index
            self._build_items()
            self.focus_index = min(index, len(self.items) - 1)

    def _clamp_focus(self) -> None:
        self.focus_index = max(0, min(self.focus_index, len(self.items) - 1))

    def _download_selected(self) -> None:
        """D: queue every checked episode."""
        if not self.selected_episode_ids:
            return
        self._start_downloads(sorted(self.selected_episode_ids))
        self.selected_episode_ids.clear()
        self._refresh()

    def _start_downloads(self, episode_ids: list[int]) -> None:
        """Queue episodes and make sure the worker thread is running."""
        queued = []
        for ep_id in episode_ids:
            state = self.download_state.get(ep_id)
            if state and state["status"] in ("queued", "downloading"):
                continue  # already in flight
            self.download_state[ep_id] = {
                "status": "queued",
                "done": 0,
                "total": None,
                "started": None,
                "error": None,
            }
            queued.append(ep_id)

        if not queued:
            return

        with self._queue_lock:
            self._queue.extend(queued)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._download_worker, name="podcast-downloader", daemon=True
                )
                self._worker.start()
        self._invalidate()

    def _download_worker(self) -> None:
        """Drain the queue off the UI thread so the browser stays responsive."""
        from podcast_downloader.downloader import download_episode

        while True:
            with self._queue_lock:
                if not self._queue:
                    return
                ep_id = self._queue.popleft()

            state = self.download_state.get(ep_id)
            if state is None:
                continue  # cancelled or already finished under us
            state["status"] = "downloading"
            state["started"] = time.monotonic()
            self._invalidate()

            def on_progress(done: int, total: int | None, _state=state) -> None:
                _state["done"] = done
                _state["total"] = total
                self._invalidate()  # prompt_toolkit collapses repeated invalidations

            try:
                download_episode(ep_id, self.store, progress=on_progress)
            except Exception as exc:  # defensive: download_episode records its own errors
                state["status"] = "error"
                state["error"] = str(exc)
                self._invalidate()
                continue

            episode = self.store.get_episode_by_id(ep_id)
            if episode is not None and episode.get("status") == "error":
                state["status"] = "error"
                state["error"] = episode.get("error") or "download failed"
            else:
                state["status"] = "done"
                # Drop finished downloads; the row's own DONE status takes over.
                self.download_state.pop(ep_id, None)
            self._call_on_ui_thread(self._refresh)

    def _refresh(self) -> None:
        """Rebuild rows from the store, keeping focus on the same row.

        Anchored on the focused item's identity, not its index: a background
        sync can insert episodes above the cursor, which would otherwise slide
        the selection out from under whoever is reading.
        """
        anchor = None
        if self.items and 0 <= self.focus_index < len(self.items):
            focused = self.items[self.focus_index]
            anchor = (focused["type"], focused["id"])
        index = self.focus_index

        self._build_items()

        if anchor is not None:
            for i, item in enumerate(self.items):
                if (item["type"], item["id"]) == anchor:
                    index = i
                    break
        self.focus_index = max(0, min(index, len(self.items) - 1))
        self._invalidate()

    def _invalidate(self) -> None:
        """Ask for a redraw; Application.invalidate is thread safe and self-throttling."""
        app = self.application
        if app is not None:
            app.invalidate()

    def _call_on_ui_thread(self, func) -> None:
        """Run func on the app's event loop, or inline when no app is running."""
        app = self.application
        if app is not None and app.is_running and app.loop is not None:
            try:
                app.loop.call_soon_threadsafe(func)
                return
            except RuntimeError:
                pass  # loop closed while we were mid-download; fall through
        func()

    def _await_downloads(self) -> None:
        """After the UI exits, let in-flight downloads finish rather than truncating them."""
        worker = self._worker
        if worker is None or not worker.is_alive():
            return
        remaining = self._active_download_count()
        print_formatted_text(FormattedText([
            ("class:download", f"  Finishing {remaining} download(s)\u2026 Ctrl-C to abandon."),
        ]))
        try:
            worker.join()
        except KeyboardInterrupt:
            print_formatted_text(FormattedText([
                ("class:error", "  Abandoned. Partial files are left as .part and will be retried."),
            ]))

    def run(self, input=None, output=None) -> None:
        """Run the browser. input/output are injectable so tests can drive it headlessly."""
        # One palette for every screen: no background fills anywhere, so the
        # menu, search, remove and browse screens all read the same. Focus is
        # shown by the ▸ marker plus bold white text, never by a highlight bar.
        style = Style.from_dict({
            "title": "bold #44aaff",
            "help": "italic #888888",
            "separator": "#444444",
            "marker": "#ffcc00",
            "check": "#00ff00",
            "id": "#888888",
            "muted": "#666666",
            "footer": "#888888",
            "version": "#777777",
            "settings-label": "bold #666666",
            "settings-footer": "bold #888888",
            "count": "bold #00ff00",
            "done": "#00cc66",
            "error": "#ff4444",
            "download": "#44aaff",
            "bar": "#44aaff",
            "bar-empty": "#333333",
            # Rows: feeds accent blue, everything else neutral; focus is bold white.
            # NB: "menu-item", not "menu" — prompt_toolkit ships a default
            # "menu" class with bg:#888888, and styles merge per attribute, so
            # a foreground-only override would leave that grey background on.
            "feed": "#44aaff",
            "feed-selected": "bold #ffffff",
            "episode": "#cccccc",
            "episode-selected": "bold #ffffff",
            "menu-item": "#cccccc",
            "menu-item-selected": "bold #ffffff",
            "query": "bold #ffffff",
        })

        kb = KeyBindings()

        @kb.add("<any>")
        def _(_event):
            self._on_key(_event)

        text = self._render

        layout = Layout(Window(content=FormattedTextControl(text=text)))
        application = self.application = Application(
            layout=layout,
            key_bindings=kb,
            style=style,
            mouse_support=True,
            input=input,
            output=output,
        )

        application.run()
        self._await_downloads()


def browse(store) -> None:
    """Launch the interactive app on its top-level menu."""
    PodcastBrowser(store).run()
