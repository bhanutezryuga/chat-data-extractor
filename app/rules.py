"""Rules engine — classify a URL into a content type by matching rules in priority order."""
import re
from urllib.parse import urlparse


def load_rules(con):
    return con.execute(
        "SELECT * FROM rules WHERE enabled = 1 ORDER BY priority ASC").fetchall()


def classify(con, url):
    """Return the first matching rule row, or None (-> unknown)."""
    host = urlparse(url).netloc.lower()
    for r in load_rules(con):
        kind, pat = r["matcher_kind"], r["matcher"]
        if kind == "url_regex" and re.search(pat, url, re.I):
            return r
        if kind == "host" and re.search(pat, host, re.I):
            return r
        # 'mime' rules are matched at fetch time, not here
    return None
