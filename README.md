# PodcastDownloader

A CLI tool for managing podcast subscriptions and downloading episodes as named audio files.

Licensed under the [MIT License](LICENSE).

## Features

- Interactive menu-driven browser (run `podcast` with no command)
- Search iTunes for podcasts
- Subscribe to RSS feeds
- Download episodes with sensible filenames (`Episode Title - YYYY-MM-DD.mp3`)
- Refresh feeds and pull the newest episodes you don't already have
- Configurable number of newest episodes to fetch per show (default 3)
- Episodes tagged with title, show, author, date and cover art, so music
  players and podcast apps show them properly
- Configurable download directory (default: `~/Podcasts`)

## Installation

### With pipx (recommended)

Needs Python 3.13 or newer and [pipx](https://pipx.pypa.io):

```bash
pipx install git+https://github.com/super-massive-code/Podcast_Downloader_CLI.git
podcast --version
```

That puts `podcast` on your `PATH` in its own isolated environment. Upgrade
later with `pipx upgrade podcast-downloader`.

### From a clone

Get Python 3.13, then set up the virtualenv:

```bash
# macOS
brew install python@3.13

# Debian/Ubuntu
sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update
sudo apt install python3.13 python3.13-venv
```

```bash
cd PodcastDownloader
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Building a standalone binary

To build a single-file binary with [PyInstaller](https://pyinstaller.org) and
put `podcast` on your `PATH`, run this from a clone — no other setup needed:

```bash
scripts/install.sh
```

It finds Python 3.13+, creates `.venv` and installs the build
dependencies if they aren't there yet, builds `dist/podcast`, and symlinks
`/usr/local/bin/podcast` to it (asking for `sudo` if needed). To install
somewhere you own instead, without `sudo`:

```bash
PODCAST_BIN_DIR=~/.local/bin scripts/install.sh
```

Because it's a symlink, later `make build` runs update the installed command
automatically. Running the script again is safe and also picks up any new
dependencies.

The binary is for the OS/architecture you built it on (e.g. macOS arm64,
Linux x86_64). PyInstaller doesn't cross-compile, so build on macOS for the
Mac binary and on Linux (or in CI) for the Linux one.

## Interactive mode

```bash
podcast
```

Opens a menu with everything in one place:

```
  Podcast Downloader
  ↑/↓ navigate  ↵ select  Q quit
  ----------------------------------------------------------------------------

▸   Search podcasts         find a show and subscribe to it
    Browse subscriptions    expand shows, download episodes
    Refresh all feeds       check every show for new episodes
    Download latest         refresh every show and fetch its newest episodes
    Remove a subscription   unsubscribe from a show
    Open download folder    reveal the downloads directory
    ----------------------------------------------------------------------
    Settings                configure download directory, episodes to download
    Quit
```

- **Search podcasts** — type a query, `↵` searches, `↵` on a result subscribes.
  Shows you already follow are marked `subscribed`.
- **Browse subscriptions** — `↵` or `→` opens a show, `↵` on an episode downloads
  it with a progress bar in the list. `Space` checks episodes and `D` downloads
  every checked one. Downloads run in the background, so you can keep browsing.
  `R` re-checks the show under the cursor for new episodes.
- **Refresh all feeds** — re-checks every subscription in one go, counting up as
  it works. A feed that fails to load is reported and the rest carry on.
- **Download latest** — refreshes every show in the background and queues each
  one's newest episodes (the Settings count) as soon as that show is checked,
  skipping any you already have. The menu footer shows the downloads' progress.
- **Remove a subscription** — `↵` asks to confirm: `Y` unsubscribes, `N`
  cancels. The show's downloaded episodes stay on disk.
- **Open download folder** — reveals the configured download directory in the
  OS file manager (Finder on macOS, the default file manager elsewhere).
- **Settings** — change the download directory or the number of episodes
  downloaded per show. Changing the download directory offers to move any
  already-downloaded files to the new location (`Y`/`n`). **About**, at the
  bottom, shows the version and license.

Each show lists how long ago it was last checked (`just now`, `2h ago`, `3d ago`).
Refreshing only *adds* new episodes to the list — nothing downloads until you ask
for it with `↵` or `D`.

Episode names are never cut short: the list uses the full width of your terminal
and wraps anything longer onto an indented continuation line. Widen the window
and titles reflow to fit.

`Esc` goes back a screen, `Q` quits from the menu. Quitting waits for any
in-flight downloads to finish.

## Command-line usage

```bash
# Search for a podcast
podcast search "99% invisible"

# Add a feed by URL
podcast add "https://feeds.99percentinvisible.org/99invisible.rss"

# List all subscriptions
podcast list

# Show episodes for a feed
podcast show 1

# Refresh a feed and download its newest episodes (default: 3)
podcast download-latest 1

# Same for every subscription
podcast download-latest

# Keep more than the default
podcast download-latest 1 -n 10

# Download a single episode
podcast download 42

# Remove a subscription (its downloaded files stay on disk)
podcast remove 1

# Show current configuration
podcast config

# Change the download directory
podcast config --download-dir ~/Podcasts2

# Change how many of each show's newest episodes download-latest fetches
podcast config --keep-latest 5

# Both at once
podcast config --download-dir ~/Podcasts2 --keep-latest 5
```

Every command exits with a non-zero status when it fails (a download error, a
feed that won't load, an unknown ID), so `download-latest` is safe to run from
cron or a script — including while the interactive app is open: the library
file is locked while either one writes to it.

`podcast --version` prints the installed version.

## Configuration

Use `podcast config` (see above), the interactive Settings screen, or edit
`~/Podcasts/library.json` directly:

- `download_dir`: Where files are saved (default: `~/Podcasts`)
- `default_keep_latest`: How many of each show's newest episodes
  `download-latest` and the app's Download latest fetch (default: 3). Applies
  to every show, including ones you subscribed to before changing it.

`podcast config --download-dir` only updates the setting — unlike the
interactive Settings screen, it does not move already-downloaded files, and
it prints a note if any are left behind in the old directory.

## No warranty

This software is provided "as is", without warranty of any kind, express or
implied. Use it at your own risk: the authors are not liable for any claim,
damage or data loss arising from its use. The [MIT License](LICENSE) has the
full terms.
