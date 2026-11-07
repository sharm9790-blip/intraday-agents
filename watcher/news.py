"""Fresh-news check for one NSE stock from Google News RSS (no key needed).

    python3 watcher/news.py "Tata Steel"

Returns the last ~24 h of headlines and flags event risk (results, board
meetings, regulators, rating changes...). Idea adapted from the MIT-licensed
trader-news skill (github.com/Jakeschincariol/trader-skill).
"""
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

HIGH = re.compile(
    r"\b(results?|earnings|q[1-4]\b|quarterly|board meet\w*|sebi|raid\w*|probe|fraud|"
    r"default\w*|f&o ban|ban period|insolvency|resign\w*|cbi|ed\b|penalt\w*)", re.I)
MEDIUM = re.compile(
    r"\b(dividend|bonus|split|buyback|block deal|bulk deal|ofs|qip|stake sale|downgrade\w*|"
    r"upgrade\w*|target price|rating|merger|acquisition|pledge\w*|guidance)", re.I)


def clean_name(name):
    """'Tata Steel Limited' -> 'Tata Steel' (for a precise search)."""
    name = re.sub(r"\b(limited|ltd\.?)\s*$", "", name or "", flags=re.I).strip(" .,")
    return name


def headlines(name, hours=24):
    q = f'"{clean_name(name)}" when:2d'
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        xml = r.read().decode("utf-8", "replace")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(.*?)</title>", item, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        if not t or not d:
            continue
        try:
            when = parsedate_to_datetime(d.group(1))
        except (TypeError, ValueError):
            continue
        if when >= since:
            title = unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))).strip()
            out.append((when, title))
    out.sort(reverse=True)
    return out


def check(name, hours=24):
    """{"level": "high"|"medium"|"none"|"unknown", "flagged": [titles], "count": n}."""
    try:
        items = headlines(name, hours)
    except Exception as e:  # network / feed change: say so, never block an alert
        return {"level": "unknown", "flagged": [], "count": 0, "error": str(e)[:80]}
    high = [t for _, t in items if HIGH.search(t)]
    med = [t for _, t in items if MEDIUM.search(t) and t not in high]
    level = "high" if high else ("medium" if med else "none")
    return {"level": level, "flagged": (high + med)[:3], "count": len(items)}


def lines(res):
    """Telegram lines for a news check (already HTML-escaped)."""
    from html import escape
    if res["level"] == "unknown":
        return ["📰 News check unavailable — check manually"]
    if res["level"] == "none":
        return [f"📰 News 24h: ✅ nothing risky ({res['count']} headlines)"]
    head = "⚠️ <b>EVENT RISK in news</b>" if res["level"] == "high" else "📰 News to check:"
    return [head] + ["• " + escape(t[:70], quote=False) for t in res["flagged"]]


if __name__ == "__main__":
    import json
    print(json.dumps(check(" ".join(sys.argv[1:]) or "Tata Steel"), indent=1))
