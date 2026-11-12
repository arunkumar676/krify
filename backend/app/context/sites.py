"""Which site a URL belongs to."""

from urllib.parse import urlparse


def site_of(url):
    """'https://www.amazon.in/s?k=x' -> 'amazon.in' ('' for new tabs and browser pages)."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.hostname or ""
    return host[4:] if host.startswith("www.") else host


def is_sheet(url):
    return "docs.google.com/spreadsheets/" in (url or "")
