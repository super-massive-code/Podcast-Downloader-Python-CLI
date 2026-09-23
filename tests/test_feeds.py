"""Tests for feeds.py - RSS parsing and iTunes search."""

import json
import re
from unittest.mock import Mock, patch

import httpx
import pytest

from podcast_downloader.feeds import FeedError, search_podcasts, parse_feed


class TestSearchPodcasts:
    @pytest.fixture
    def mock_response(self):
        response = Mock()
        response.json = Mock(return_value={})
        return response

    def test_search_podcasts_maps_fields(self, mock_response):
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "resultCount": 1,
            "results": [
                {
                    "collectionName": "Test Podcast",
                    "artistName": "Test Author",
                    "feedUrl": "https://example.com/feed.xml",
                    "artworkUrl600": "https://example.com/art.jpg",
                }
            ],
        }
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_podcasts("test")
        assert result[0]["title"] == "Test Podcast"
        assert result[0]["author"] == "Test Author"
        assert result[0]["feed_url"] == "https://example.com/feed.xml"
        assert result[0]["artwork_url"] == "https://example.com/art.jpg"

    def test_search_podcasts_skips_results_without_feed_url(self, mock_response):
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "resultCount": 3,
            "results": [
                {
                    "collectionName": "Valid Podcast",
                    "artistName": "Author",
                    "feedUrl": "https://example.com/feed.xml",
                    "artworkUrl600": "https://example.com/art.jpg",
                },
                {
                    "collectionName": "No Feed",
                    "artistName": "Author",
                    "artworkUrl600": "https://example.com/art.jpg",
                },
                {
                    "collectionName": "Another Valid",
                    "artistName": "Author 2",
                    "feedUrl": "https://example.com/feed2.xml",
                    "artworkUrl600": "https://example.com/art2.jpg",
                },
            ],
        }
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_podcasts("test")
        assert len(result) == 2
        urls = [r["feed_url"] for r in result]
        assert "https://example.com/feed.xml" in urls
        assert "https://example.com/feed2.xml" in urls

    def test_search_podcasts_empty_results(self, mock_response):
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "resultCount": 0,
            "results": [],
        }
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_podcasts("nonexistent")
        assert result == []

    # A failed search must not look like "no podcasts matched".
    def test_search_podcasts_raises_on_http_error(self, mock_response):
        mock_response.status_code = 500
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_response
            with pytest.raises(FeedError, match="HTTP 500"):
                search_podcasts("test")

    def test_search_podcasts_raises_on_json_decode_error(self, mock_response):
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError("test", "", 0)
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_response
            with pytest.raises(FeedError):
                search_podcasts("test")

    def test_search_podcasts_raises_on_connection_error(self):
        with patch("podcast_downloader.feeds.httpx.get", side_effect=httpx.ConnectError("Connection failed")):
            with pytest.raises(FeedError, match="Connection failed"):
                search_podcasts("test")


class TestParseFeed:
    @pytest.fixture
    def mock_rss_response(self):
        response = Mock()
        response.status_code = 200
        return response

    def _make_rss(self, episodes=None):
        if episodes is None:
            episodes = []
        rss_items = ""
        for ep in episodes:
            item = f'<item>\n                <guid>{ep.get("guid", "")}</guid>\n                <title>{ep["title"]}</title>'
            pub_date = ep.get("pub_date")
            if pub_date:
                item += f"\n                <pubDate>{pub_date}</pubDate>"
            enclosure_url = ep.get("enclosure_url")
            if enclosure_url:
                item += f'\n                <enclosure url="{enclosure_url}" type="{ep.get("enclosure_type", "audio/mpeg")}"/>'
            item += "\n            </item>"
            rss_items += item
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
    <channel>
        <title>Test Podcast</title>
        <itunes:author>Test Author</itunes:author>
        <link>https://example.com</link>
        {rss_items}
    </channel>
</rss>"""

    def test_parse_feed_extract_title_and_author(self, mock_rss_response):
        mock_rss_response.text = self._make_rss([])
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert result["title"] == "Test Podcast"
        assert result["author"] == "Test Author"

    def test_parse_feed_extract_artwork(self, mock_rss_response):
        rss = self._make_rss([]).replace(
            "<title>Test Podcast</title>",
            '<title>Test Podcast</title>\n        <itunes:image href="https://example.com/art.jpg"/>',
        )
        mock_rss_response.text = rss
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert result["artwork_url"] == "https://example.com/art.jpg"

    def test_parse_feed_parses_episodes(self, mock_rss_response):
        mock_rss_response.text = self._make_rss([
            {
                "guid": "urn:uuid:1",
                "title": "Episode 1",
                "pub_date": "Mon, 01 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep1.mp3",
                "enclosure_type": "audio/mpeg",
            },
            {
                "guid": "urn:uuid:2",
                "title": "Episode 2",
                "pub_date": "Tue, 02 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep2.mp3",
                "enclosure_type": "audio/mpeg",
            },
        ])
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert len(result["episodes"]) == 2

    def test_parse_feed_episode_guid_falls_back_to_enclosure_url(self, mock_rss_response):
        rss = self._make_rss([
            {
                "title": "Episode No GUID",
                "pub_date": "Mon, 01 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep-no-guid.mp3",
                "enclosure_type": "audio/mpeg",
            }
        ])
        # Remove <guid> tag
        rss = re.sub(r"<guid>\s*</guid>", "", rss)
        mock_rss_response.text = rss
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert len(result["episodes"]) == 1
        assert result["episodes"][0]["guid"] == "https://example.com/ep-no-guid.mp3"

    def test_parse_feed_episode_audio_url_from_enclosure(self, mock_rss_response):
        mock_rss_response.text = self._make_rss([
            {
                "guid": "urn:uuid:1",
                "title": "Episode 1",
                "pub_date": "Mon, 01 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep1.mp3",
                "enclosure_type": "audio/mpeg",
            }
        ])
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert result["episodes"][0]["audio_url"] == "https://example.com/ep1.mp3"

    def test_parse_feed_episode_published_date(self, mock_rss_response):
        mock_rss_response.text = self._make_rss([
            {
                "guid": "urn:uuid:1",
                "title": "Episode 1",
                "pub_date": "Mon, 21 Sep 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep1.mp3",
                "enclosure_type": "audio/mpeg",
            }
        ])
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert result["episodes"][0]["published"] == "2026-09-21"

    def test_parse_feed_episode_no_published_date(self, mock_rss_response):
        rss = self._make_rss([
            {
                "guid": "urn:uuid:1",
                "title": "Episode No Date",
                "enclosure_url": "https://example.com/ep1.mp3",
                "enclosure_type": "audio/mpeg",
            }
        ])
        # Remove <pubDate> tag
        rss = re.sub(r"<pubDate>[^<]*</pubDate>", "", rss)
        mock_rss_response.text = rss
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert result["episodes"][0]["published"] is None

    def test_parse_feed_uses_first_audio_enclosure(self, mock_rss_response):
        rss = self._make_rss([
            {
                "guid": "urn:uuid:1",
                "title": "Episode 1",
                "pub_date": "Mon, 01 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep1.mp3",
                "enclosure_type": "audio/mpeg",
            },
            {
                "guid": "urn:uuid:2",
                "title": "Episode 2",
                "pub_date": "Tue, 02 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/ep2.m4a",
                "enclosure_type": "audio/mp4",
            },
        ])
        mock_rss_response.text = rss
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        ep1 = result["episodes"][0]
        assert ep1["audio_url"] == "https://example.com/ep1.mp3"

    def test_parse_feed_falls_back_to_first_enclosure_if_no_audio(self, mock_rss_response):
        rss = self._make_rss([
            {
                "guid": "urn:uuid:1",
                "title": "Episode 1",
                "pub_date": "Mon, 01 Jan 2026 00:00:00 +0000",
                "enclosure_url": "https://example.com/attachment.pdf",
                "enclosure_type": "application/pdf",
            }
        ])
        mock_rss_response.text = rss
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            result = parse_feed("https://example.com/feed.xml")
        assert result["episodes"][0]["audio_url"] == "https://example.com/attachment.pdf"

    def test_parse_feed_rejects_invalid_rss(self, mock_rss_response):
        mock_rss_response.text = "not xml at all"
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            with pytest.raises(FeedError, match="not a readable podcast feed"):
                parse_feed("https://example.com/feed.xml")

    def test_parse_feed_reports_the_http_status(self, mock_rss_response):
        mock_rss_response.status_code = 404
        mock_rss_response.text = "Not Found"
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            with pytest.raises(FeedError, match="HTTP 404"):
                parse_feed("https://example.com/feed.xml")

    def test_parse_feed_reports_a_connection_error(self):
        with patch("podcast_downloader.feeds.httpx.get", side_effect=httpx.ConnectError("Connection failed")):
            with pytest.raises(FeedError, match="Connection failed"):
                parse_feed("https://example.com/feed.xml")

    @pytest.mark.parametrize("raw, seconds", [
        ("3723", 3723),
        ("62:03", 3723),
        ("1:02:03", 3723),
        ("", None),
        ("soon", None),
    ])
    def test_parse_feed_reads_itunes_duration(self, mock_rss_response, raw, seconds):
        mock_rss_response.text = (
            '<?xml version="1.0"?><rss version="2.0" '
            'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>T</title>'
            f'<item><title>E</title><guid>g</guid><itunes:duration>{raw}</itunes:duration>'
            '<enclosure url="http://x/1.mp3" type="audio/mpeg"/></item></channel></rss>'
        )
        with patch("podcast_downloader.feeds.httpx.get", return_value=mock_rss_response):
            result = parse_feed("https://example.com/feed.xml")
        assert result["episodes"][0]["duration"] == seconds

    def test_parse_feed_sets_user_agent(self, mock_rss_response):
        mock_rss_response.text = self._make_rss([])
        with patch("podcast_downloader.feeds.httpx.get") as mock_get:
            mock_get.return_value = mock_rss_response
            parse_feed("https://example.com/feed.xml")
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args
            assert "headers" in call_kwargs.kwargs
            assert "User-Agent" in call_kwargs.kwargs["headers"]
