"""CLI entry point for PodcastDownloader."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from podcast_downloader import __version__
from podcast_downloader.store import LibraryStore, expand_dir
from podcast_downloader.feeds import search_podcasts
from podcast_downloader.library import LibraryError, latest_episodes, refresh, subscribe, unsubscribe
from podcast_downloader.downloader import download_episode
from podcast_downloader.interactive import browse


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="podcast",
        description="Podcast library manager - subscribe, search, and download podcasts.",
        epilog="Run with no command to open the interactive menu.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # search
    search_parser = subparsers.add_parser("search", help="Search iTunes for podcasts")
    search_parser.add_argument("term", help="Search term")

    # add
    add_parser = subparsers.add_parser("add", help="Add a podcast feed")
    add_parser.add_argument("url", help="Feed URL")

    # list
    subparsers.add_parser("list", help="List subscribed feeds")

    # show
    show_parser = subparsers.add_parser("show", help="Show episodes for a feed")
    show_parser.add_argument("feed_id", type=int, help="Feed ID")

    # download
    download_parser = subparsers.add_parser("download", help="Download a single episode")
    download_parser.add_argument("episode_id", type=int, help="Episode ID")

    # download-latest
    latest_parser = subparsers.add_parser(
        "download-latest",
        help="Refresh feeds and download their newest episodes",
    )
    latest_parser.add_argument(
        "feed_id", type=int, nargs="?",
        help="Feed ID (omit for every subscription)",
    )
    latest_parser.add_argument(
        "-n", "--count", type=int, default=None,
        help="How many of the newest episodes to keep (default: the 'podcast config --keep-latest' value, 3 unless changed)",
    )

    # remove
    remove_parser = subparsers.add_parser("remove", help="Remove a subscription")
    remove_parser.add_argument("feed_id", type=int, help="Feed ID")

    # config
    config_parser = subparsers.add_parser(
        "config", help="View or change the download directory and episode count"
    )
    config_parser.add_argument(
        "--download-dir", dest="download_dir", default=None,
        help="Set the download directory",
    )
    config_parser.add_argument(
        "--keep-latest", dest="keep_latest", type=int, default=None,
        help="Set how many of each show's newest episodes download-latest fetches",
    )

    return parser


def _get_store() -> LibraryStore:
    """Get or create a LibraryStore instance."""
    return LibraryStore()


def cmd_search(args: argparse.Namespace) -> None:
    """Handle the 'search' command."""
    results = search_podcasts(args.term)
    if not results:
        print("No results found.")
        return

    print(f"Found {len(results)} podcast(s):\n")
    print(f"{'':<4}{'Title':<40}{'Author':<25}{'Feed URL'}")
    print("-" * 80)
    for i, r in enumerate(results, 1):
        title = r["title"][:38] + ".." if len(r["title"]) > 40 else r["title"]
        author = r["author"][:23] + ".." if len(r["author"]) > 25 else r["author"]
        print(f"{i:<4}{title:<40}{author:<25}{r['feed_url']}")
    print("\nUse 'podcast add <URL>' to subscribe.")


def cmd_add(args: argparse.Namespace) -> int | None:
    """Handle the 'add' command."""
    store = _get_store()

    print(f"Fetching feed: {args.url}")
    try:
        result = subscribe(store, args.url)
    except LibraryError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Found: {result.title} by {result.author}")
    print(f"Episodes: {result.episode_count}")
    print(f"Added '{result.title}' (ID: {result.feed_id}) with {result.episode_count} episodes.")


def cmd_list(_args: argparse.Namespace) -> None:
    """Handle the 'list' command."""
    store = _get_store()
    data = store.read()
    feeds = data["feeds"]

    if not feeds:
        print("No subscriptions.")
        return

    print(f"{'ID':<4}{'Title':<40}{'Episodes':<12}{'Folder'}")
    print("-" * 80)
    for feed in feeds:
        feed_id = feed["id"]
        title = feed["title"][:38] + ".." if len(feed["title"]) > 40 else feed["title"]
        ep_count = len(store.get_episodes_by_feed_id(feed_id))
        folder = feed.get("folder_name", "")
        print(f"{feed_id:<4}{title:<40}{ep_count:<12}{folder}")


def cmd_show(args: argparse.Namespace) -> int | None:
    """Handle the 'show' command."""
    store = _get_store()
    feed = store.get_feed_by_id(args.feed_id)

    if feed is None:
        print(f"Error: Feed ID {args.feed_id} not found.")
        return 1

    episodes = store.get_episodes_by_feed_id(args.feed_id)
    print(f"Episodes for '{feed['title']}' ({len(episodes)}):\n")
    print(f"{'ID':<6}{'Status':<10}{'Published':<14}{'Title'}")
    print("-" * 80)
    for ep in episodes:
        ep_id = ep["id"]
        status = ep["status"]
        published = ep.get("published", "") or "N/A"
        title = ep["title"][:50] + ".." if len(ep["title"]) > 50 else ep["title"]
        print(f"{ep_id:<6}{status:<10}{published:<14}{title}")


def cmd_download(args: argparse.Namespace) -> int | None:
    """Handle the 'download' command."""
    store = _get_store()
    episode = store.get_episode_by_id(args.episode_id)

    if episode is None:
        print(f"Error: Episode {args.episode_id} not found.")
        return 1

    print(f"Downloading: {episode['title']}")
    if not download_episode(args.episode_id, store):
        _report_failed_download(store, args.episode_id)
        return 1


def _report_failed_download(store, episode_id: int) -> None:
    episode = store.get_episode_by_id(episode_id) or {}
    reason = episode.get("error") or "download failed"
    print(f"Error: '{episode.get('title', episode_id)}': {reason}")


def _download_latest_for(store, feed: dict, wanted: int, verbose: bool) -> bool:
    """Refresh one feed and download whichever of its newest episodes we lack.

    Returns False if the refresh or any download failed.
    """
    title = feed["title"]
    if verbose:
        print(f"Refreshing '{title}'...")

    try:
        result = refresh(store, feed["id"])
    except LibraryError as exc:
        print(f"{title}: error: {exc}" if not verbose else f"Error: {exc}")
        return False

    newest = latest_episodes(store, feed["id"], wanted)
    missing = [ep for ep in newest if ep["status"] != "done"]
    new_count = len(result.new_episode_ids)

    if verbose:
        print(f"{new_count} new episode(s)")
        if not missing:
            print(f"Already have the newest {len(newest)}.")
        else:
            held = len(newest) - len(missing)
            extra = f" ({held} already downloaded)" if held else ""
            print(f"Downloading {len(missing)} of the newest {len(newest)}{extra}")
    else:
        if not missing:
            print(f"  {title}: {new_count} new, up to date")
        else:
            print(f"  {title}: {new_count} new, downloading {len(missing)}")

    ok = True
    for episode in missing:
        if not download_episode(episode["id"], store):
            _report_failed_download(store, episode["id"])
            ok = False
    return ok


def cmd_download_latest(args: argparse.Namespace) -> int | None:
    """Handle the 'download-latest' command."""
    store = _get_store()
    data = store.read()

    if args.feed_id is None:
        feeds = data["feeds"]
        if not feeds:
            print("No subscriptions.")
            return
    else:
        feed = store.get_feed_by_id(args.feed_id)
        if feed is None:
            print(f"Error: Feed ID {args.feed_id} not found.")
            return 1
        feeds = [feed]

    wanted = args.count if args.count is not None else data["config"].get("default_keep_latest", 3)
    verbose = args.feed_id is not None
    results = [_download_latest_for(store, feed, wanted, verbose) for feed in feeds]
    if not all(results):
        return 1


def cmd_remove(args: argparse.Namespace) -> int | None:
    """Handle the 'remove' command."""
    store = _get_store()
    feed = store.get_feed_by_id(args.feed_id)

    if feed is None:
        print(f"Error: Feed ID {args.feed_id} not found.")
        return 1

    try:
        unsubscribe(store, args.feed_id)
    except LibraryError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"'{feed['title']}' removed. Its downloaded files were left in place.")


def cmd_config(args: argparse.Namespace) -> int | None:
    """Handle the 'config' command."""
    store = _get_store()

    if args.download_dir is None and args.keep_latest is None:
        config = store.read()["config"]
        print(f"download_dir: {config.get('download_dir')}")
        print(f"default_keep_latest: {config.get('default_keep_latest')}")
        return

    if args.keep_latest is not None and args.keep_latest < 1:
        print("Error: --keep-latest must be at least 1")
        return 1

    if args.download_dir is not None:
        old_dir = store.read()["config"].get("download_dir", "")
        new_dir = expand_dir(args.download_dir)
        store.update_config(download_dir=new_dir)
        print(f"download_dir set to {new_dir}")
        if old_dir and old_dir != new_dir:
            existing = [
                ep for ep in store.read().get("episodes", [])
                if ep.get("file_path") and Path(ep["file_path"]).exists()
            ]
            if existing:
                print(
                    f"Note: {len(existing)} downloaded episode(s) remain in "
                    f"{old_dir}. Move them manually if needed."
                )

    if args.keep_latest is not None:
        store.update_config(default_keep_latest=args.keep_latest)
        print(f"default_keep_latest set to {args.keep_latest}")


def cmd_browse(_args: argparse.Namespace) -> None:
    """Open the interactive menu. This is what running with no command does."""
    store = _get_store()
    browse(store)


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "search": cmd_search,
        "add": cmd_add,
        "list": cmd_list,
        "show": cmd_show,
        "download": cmd_download,
        "download-latest": cmd_download_latest,
        "remove": cmd_remove,
        "config": cmd_config,
    }

    # No subcommand: open the interactive menu rather than printing help.
    handler = cmd_browse if args.command is None else commands.get(args.command)
    if handler:
        try:
            exit_code = handler(args)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
        if exit_code:
            sys.exit(exit_code)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
