import pytest

from capture.urls import extract_url, normalize_url


@pytest.mark.parametrize("raw", [
    "https://www.instagram.com/reel/C9xYz12AbC/?igsh=MTRhZ2x5",
    "https://instagram.com/p/C9xYz12AbC/",
    "https://m.instagram.com/reels/C9xYz12AbC",
    "https://www.instagram.com/chef.anna/reel/C9xYz12AbC/?utm_source=ig_web_copy_link",
])
def test_instagram_forms_collapse(raw):
    assert normalize_url(raw) == "https://instagram.com/p/C9xYz12AbC"


@pytest.mark.parametrize("raw", ["https://youtu.be/dQw4w9WgXcQ?si=abc", "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                                 "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share"])
def test_youtube_forms_collapse(raw):
    assert normalize_url(raw) == "https://youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_live_collapses():
    assert normalize_url("https://www.youtube.com/live/dQw4w9WgXcQ") == "https://youtube.com/watch?v=dQw4w9WgXcQ"


def test_web_url_keeps_meaningful_params_sorted():
    assert normalize_url("http://Blog.Example.com/post/?b=2&utm_medium=x&a=1#top") == "https://blog.example.com/post?a=1&b=2"


def test_unparseable_input_does_not_raise():
    assert normalize_url("  http://[bad  ") == "http://[bad"


def test_extract_url_from_instagram_share_text():
    t = "Check out this reel by @chef.anna https://www.instagram.com/reel/C9xYz12AbC/?igsh=MTRh."
    assert extract_url(t) == "https://www.instagram.com/reel/C9xYz12AbC/?igsh=MTRh"


def test_extract_url_strips_wrapping_punctuation():
    assert extract_url('see (https://example.com/a), ok') == "https://example.com/a"
    assert extract_url("go https://example.com/a…") == "https://example.com/a"


def test_extract_url_none():
    assert extract_url("buy milk") is None


def test_extract_url_keeps_balanced_parens():
    u = "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert extract_url(u) == u
    assert extract_url(f"see ({u}).") == u


@pytest.mark.parametrize("tail", ["🔥", "。", "”"])
def test_extract_url_strips_unicode_trailers(tail):
    assert extract_url(f"https://x.com/a{tail}") == "https://x.com/a"


def test_extract_url_stops_at_space_before_emoji():
    assert extract_url("https://x.com/a 🔥") == "https://x.com/a"
