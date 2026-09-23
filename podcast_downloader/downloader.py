"""Episode naming, sanitisation, and download."""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, Set

import httpx
from mutagen import File as MutagenFile, MutagenError
from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from podcast_downloader import __version__

_USER_AGENT = f"PodcastDownloader/{__version__}"
_CONTROL_CHARS = set(chr(i) for i in range(32))
_MAX_ARTWORK_BYTES = 10 * 1024 * 1024

# Filesystems cap a single name at 255 bytes, and a download is written as
# "<name>.part" first, so that suffix has to fit too.
_MAX_NAME_BYTES = 255
_PART_SUFFIX = ".part"


def _truncate_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def sanitise(name: str) -> str:
    """Sanitise a string for use in filenames.

    - Strips /, :, \\, ?, %, *, |, ", <, > and ASCII control chars.
    - Collapses whitespace runs to single space.
    - Strips leading/trailing whitespace and dots.
    - Truncates to 255 bytes (UTF-8 encoded).
    - Returns 'Untitled' if result is empty.
    """
    # Strip reserved characters and control chars
    reserved = set(r"/:\?*%|\"<>")
    cleaned = "".join(
        c for c in name
        if c not in reserved and c not in _CONTROL_CHARS
    )

    # Collapse whitespace runs
    cleaned = re.sub(r"\s+", " ", cleaned)

    # Strip leading/trailing whitespace and dots
    cleaned = cleaned.strip(" .")

    cleaned = _truncate_utf8(cleaned, _MAX_NAME_BYTES)

    # If result is empty, fallback
    if not cleaned:
        return "Untitled"

    return cleaned


def _extension_from_url(audio_url: str) -> str:
    """Extract file extension from URL path."""
    if isinstance(audio_url, (list, tuple)):
        audio_url = audio_url[0]
    path = Path(audio_url.split("?")[0].split("#")[0])
    ext = path.suffix.lower()
    if ext in (".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac"):
        return ext
    return ""


def _extension_from_mime(content_type: str) -> str:
    """Map a Content-Type header to a file extension, or "" if it names no audio type."""
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/flac": ".flac",
        "audio/aac": ".aac",
    }
    return mapping.get(content_type.split(";")[0].strip().lower(), "")


def episode_filename(
    episode: dict[str, Any],
    feed: dict[str, Any],
    used_names: Optional[Set[str]] = None,
    ext: Optional[str] = None,
) -> str:
    """Generate a filename for an episode.

    Format: {sanitised_title} - {published}{ext} (with date)
    Or:    {sanitised_title}{ext} (without date)
    The title leads so the file matches the episode name; the date is
    appended to disambiguate re-uploads or repeated titles.
    Handles collisions by appending (2), (3), etc. `ext` defaults to the
    audio URL's extension, else .mp3.
    """
    if used_names is None:
        used_names = set()

    title = episode.get("title", "Untitled Episode")
    published = episode.get("published")
    if ext is None:
        ext = _extension_from_url(episode.get("audio_url", "")) or ".mp3"

    date = f" - {published}" if published else ""
    # Leave room for the date, a collision suffix, the extension and .part.
    reserved = len(f"{date} (99){ext}{_PART_SUFFIX}".encode("utf-8"))
    base = _truncate_utf8(sanitise(title), _MAX_NAME_BYTES - reserved).rstrip(" .") + date

    candidate = f"{base}{ext}"

    # Handle collisions - used_names may contain full paths, extract filenames
    used_basenames: Set[str] = {Path(n).name for n in used_names}
    counter = 1
    while candidate in used_basenames:
        counter += 1
        candidate = f"{base} ({counter}){ext}"

    return candidate


@functools.lru_cache(maxsize=16)
def _download_artwork(url: str) -> tuple[bytes, str]:
    """(image bytes, MIME type). Cached, so a batch of one show's episodes fetches it once."""
    response = httpx.get(url, headers={"User-Agent": _USER_AGENT}, follow_redirects=True, timeout=30.0)
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError(f"unsupported artwork type {mime!r}")
    if len(response.content) > _MAX_ARTWORK_BYTES:
        raise ValueError("artwork too large")
    return response.content, mime


def _artwork(url: str) -> tuple[bytes, str] | None:
    # lru_cache doesn't cache exceptions, so a failed fetch is retried next episode.
    if not url:
        return None
    try:
        return _download_artwork(url)
    except (httpx.HTTPError, ValueError):
        return None


def tag_episode(path: str, ext: str, episode: dict[str, Any], feed: dict[str, Any]) -> None:
    """Write title, show, author, date and artwork into the audio file's metadata.

    Text fields always follow the feed. Artwork is the show's, and only added
    when the file has none, so episode art a publisher embedded is kept.
    Best effort: a file that can't be tagged is still a good download.
    """
    title = episode.get("title") or ""
    show = feed.get("title") or ""
    author = feed.get("author") or show
    date = episode.get("published") or ""
    try:
        if ext == ".mp3":
            audio = MP3(path)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            tags.setall("TALB", [TALB(encoding=3, text=show)])
            tags.setall("TPE1", [TPE1(encoding=3, text=author)])
            tags.setall("TCON", [TCON(encoding=3, text="Podcast")])
            if date:
                tags.setall("TDRC", [TDRC(encoding=3, text=date)])
            if not tags.getall("APIC"):
                art = _artwork(feed.get("artwork_url", ""))
                if art:
                    tags.add(APIC(encoding=3, mime=art[1], type=3, desc="Cover", data=art[0]))
            # ID3v2.3 rather than mutagen's default 2.4: older players and
            # Windows Explorer only read 2.3.
            audio.save(v2_version=3)
        elif ext == ".m4a":
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            tags["\xa9nam"] = [title]
            tags["\xa9alb"] = [show]
            tags["\xa9ART"] = [author]
            tags["\xa9gen"] = ["Podcast"]
            if date:
                tags["\xa9day"] = [date]
            if "covr" not in tags:
                art = _artwork(feed.get("artwork_url", ""))
                if art:
                    fmt = MP4Cover.FORMAT_PNG if art[1] == "image/png" else MP4Cover.FORMAT_JPEG
                    tags["covr"] = [MP4Cover(art[0], imageformat=fmt)]
            audio.save()
        else:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                return
            if audio.tags is None:
                audio.add_tags()
            audio["title"] = [title]
            audio["album"] = [show]
            audio["artist"] = [author]
            audio["genre"] = ["Podcast"]
            if date:
                audio["date"] = [date]
            audio.save()
    except (MutagenError, OSError, KeyError, ValueError):
        pass


ProgressCallback = Callable[[int, Optional[int]], None]


def download_episode(
    episode_id: int,
    store,
    progress: Optional[ProgressCallback] = None,
) -> bool:
    """Download a single episode, returning whether it succeeded.

    - Streams to a .part file, then atomically renames.
    - On error: sets status='error', stores message, cleans up .part.
    - `progress`, if given, is called as progress(bytes_done, total_bytes) after
      each chunk. total_bytes is None when the server sends no content-length.
    """
    data = store.read()

    # Find the episode
    episode = None
    for ep in data["episodes"]:
        if ep["id"] == episode_id:
            episode = ep
            break

    if episode is None:
        print(f"Error: Episode {episode_id} not found")
        return False

    # Find the feed
    feed = None
    for f in data["feeds"]:
        if f["id"] == episode["feed_id"]:
            feed = f
            break

    if feed is None:
        store.update_episode_status(episode_id, status="error", error="Feed not found")
        return False

    download_dir = data["config"]["download_dir"]
    audio_url = episode["audio_url"]
    if isinstance(audio_url, (list, tuple)):
        audio_url = audio_url[0]

    # Other episodes' files are taken; this episode's own file is not, so a
    # re-download replaces it rather than leaving "Title (2).mp3" beside it.
    used_names: Set[str] = {
        Path(ep["file_path"]).name
        for ep in data["episodes"]
        if ep["feed_id"] == feed["id"] and ep.get("file_path") and ep["id"] != episode_id
    }

    part_path: Optional[str] = None
    try:
        show_folder = Path(download_dir) / feed["folder_name"]
        show_folder.mkdir(parents=True, exist_ok=True)

        # Download, streaming the body so progress reflects real bytes on disk
        with httpx.stream(
            "GET",
            audio_url,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=3600.0,  # 1 hour timeout for large files
        ) as response:
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code} downloading {audio_url}")

            # The URL's own extension wins: CDNs often send a generic
            # Content-Type (application/octet-stream) that says nothing.
            ext = (
                _extension_from_url(audio_url)
                or _extension_from_mime(response.headers.get("content-type", ""))
                or ".mp3"
            )
            filename = episode_filename(episode, feed, used_names=used_names, ext=ext)
            part_path = str(show_folder / f"{filename}{_PART_SUFFIX}")
            final_path = str(show_folder / filename)

            # Stream the file, reporting progress as bytes land
            total_header = response.headers.get("content-length")
            total = int(total_header) if total_header and total_header.isdigit() else None
            downloaded = 0
            if progress is not None:
                progress(0, total)
            with open(part_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, total)

            # Atomic rename
            # Tagged before the rename, so the finished file never appears untagged.
            tag_episode(part_path, ext, episode, feed)
            os.replace(part_path, final_path)

        store.update_episode_status(episode_id, status="done", file_path=final_path)
        return True

    except Exception as e:
        if part_path is not None:
            try:
                os.unlink(part_path)
            except OSError:
                pass
        store.update_episode_status(episode_id, status="error", error=str(e))
        return False
