#!/usr/bin/env python3
"""
news_digest.py - free, key-less daily news digest for India / Tamil Nadu / Markets / World.

What it does
  1. Fetches ~25 RSS feeds in parallel (one broken feed never stops the run).
  2. Keeps only items from the last N hours.
  3. Merges duplicate stories across sources (fuzzy title match), remembering who covered it.
  4. Ranks stories: more independent sources + trusted source + fresh + important keywords,
     minus junk (stock tips, listicles, horoscopes, ...), with a per-source cap for variety.
  5. Optional free AI step (OpenRouter free models): reads the full articles and writes a detailed, structured digest.
  6. Adds live market numbers (Sensex, Nifty, USD/INR, Gold, Brent) from Yahoo's free endpoint.
  7. Writes digest.md and digest.html, prints to the console.

Only the Python standard library is used. Python 3.9+.

Usage
  python news_digest.py                      # last 24h, top 8 per section
  python news_digest.py --hours 12 --top 10
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------------------
# FEEDS: (section, source name, url, trust weight 0-1)
# Verified live on 12 Oct 2026. Edit freely - add or remove lines.
# --------------------------------------------------------------------------------------
GN = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
FEEDS = [
    # India
    ("India", "The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", 1.0),
    ("India", "Hindustan Times", "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", 0.9),
    ("India", "Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", 0.8),
    ("India", "NDTV", "https://feeds.feedburner.com/ndtvnews-top-stories", 0.8),
    ("India", "Indian Express", "https://indianexpress.com/section/india/feed/", 0.9),
    ("India", "Livemint", "https://www.livemint.com/rss/news", 0.8),
    ("India", "Google News India", "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en", 0.7),
    # Tamil Nadu
    ("Tamil Nadu", "The Hindu", "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss", 1.0),
    ("Tamil Nadu", "The Hindu", "https://www.thehindu.com/news/cities/chennai/feeder/default.rss", 1.0),
    ("Tamil Nadu", "TOI Chennai", "https://timesofindia.indiatimes.com/rssfeeds/2950623.cms", 0.8),
    ("Tamil Nadu", "Google News TN", GN + urllib.parse.quote("Tamil Nadu"), 0.7),
    ("Tamil Nadu", "Google News Chennai", GN + "Chennai", 0.7),
    # Markets & economy
    ("Markets", "Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", 0.9),
    ("Markets", "Economic Times", "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms", 0.9),
    ("Markets", "Business Standard", "https://www.business-standard.com/rss/markets-106.rss", 1.0),
    ("Markets", "Business Standard", "https://www.business-standard.com/rss/economy-102.rss", 1.0),
    ("Markets", "Livemint", "https://www.livemint.com/rss/markets", 0.8),
    ("Markets", "BusinessLine", "https://www.thehindubusinessline.com/markets/feeder/default.rss", 0.9),
    ("Markets", "RBI", "https://www.rbi.org.in/pressreleases_rss.xml", 0.6),
    # World
    ("World", "BBC", "https://feeds.bbci.co.uk/news/world/rss.xml", 1.0),
    ("World", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", 0.9),
    ("World", "France 24", "https://www.france24.com/en/rss", 0.8),
    ("World", "NYT", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", 1.0),
    ("World", "Reuters", GN + "when:1d+source:reuters", 1.0),
]
TRENDS = [
    ("India", "https://trends.google.com/trending/rss?geo=IN"),
    ("Tamil Nadu", "https://trends.google.com/trending/rss?geo=IN-TN"),
]
QUOTES = [  # label, Yahoo symbol, decimals
    ("Sensex", "^BSESN", 2), ("Nifty 50", "^NSEI", 2), ("USD/INR", "USDINR=X", 2),
    ("Gold (USD/oz)", "GC=F", 0), ("Brent (USD)", "BZ=F", 2),
]
SECTION_ORDER = ["India", "World", "Tamil Nadu", "Markets"]
TIE_PRIORITY = {"Tamil Nadu": 0, "Markets": 1, "World": 2, "India": 3}

# --------------------------------------------------------------------------------------
# Ranking knobs
# --------------------------------------------------------------------------------------
BOOST = {  # words that signal important news (matched against the title)
    3.0: ["supreme court", "rbi", "repo", "sensex", "nifty", "killed", "dead", "blast", "attack",
          "strike", "election commission", "inflation", "rate hike", "ceasefire", "earthquake",
          "cyclone", "flood", "arrested", "verdict", "budget", "war"],
    2.0: ["modi", "vijay", "stalin", "rahul gandhi", "chief minister", "cm ", "parliament",
          "high court", "rupee", "crude", "oil", "gold", "ipo", "q2", "results", "dividend",
          "tamil nadu", "chennai", "cauvery", "metro", "trump", "iran", "saudi", "russia",
          "ukraine", "china", "pakistan", "protest", "police"],
}
JUNK = [  # low-value patterns: pushed down hard
    r"\bstocks? to (buy|watch)\b", r"\btrading guide\b", r"\bstock recommendations?\b",
    r"\btarget price\b", r"\bhoroscope\b", r"\bopinion\b", r"\bin pics?\b", r"\bphotos?\b",
    r"\bwatch:", r"\bquiz\b", r"\bviral\b", r"\bpodcast\b", r"\bmeet [a-z]+, \d+\b",
    r"\bhow to (watch|download|check)\b", r"\bwhat it means\b", r"\bportfolio\b",
    r"\bhey \w+, hello\b", r"\bbirthday\b", r"\bwedding\b", r"\bbox office\b",
    r"\bwhich (stock|fmcg)\b", r"\bjailer 2\b", r"\b(gold|silver|petrol|diesel) (rate|price)s?\b",
    r"\btoday.s (gold|silver|petrol)\b", r"\bweather (today|update)\b",
]
JUNK_RE = re.compile("|".join(JUNK), re.I)
MARKET_RE = re.compile(
    r"sensex|nifty|rupee|rbi|sebi|stock|share|market|bond|yield|inflation|gdp|ipo|crude|oil|gold|"
    r"silver|fii|dii|q[1-4]|result|profit|dividend|bank|trade|tariff|fed|rate|tax|gst|economy|"
    r"export|import|fund|mutual|sip|crore|billion|dollar|currency|forex", re.I)
INDIA_RE = re.compile(
    r"india|delhi|mumbai|kolkata|chennai|bengaluru|bangalore|hyderabad|pune|modi|bjp|congress|"
    r"rahul|kharge|parliament|lok sabha|rajya sabha|supreme court|high court|cji|cbi|\bed\b|nia\b|"
    r"army|navy|\biaf\b|isro|rbi|sebi|election commission|\bcec\b|\bsir\b|kerala|karnataka|tamil|"
    r"andhra|telangana|punjab|haryana|bihar|assam|gujarat|maharashtra|odisha|uttar pradesh|rajasthan|"
    r"jammu|kashmir|manipur|bengal|goa|uttarakhand|jharkhand|chhattisgarh|madhya pradesh|himachal|"
    r"pakistan|bangladesh|china|nepal|lanka|indian|\bloc\b|jantar|cjp|dmk|aiadmk|tvk|ndtv|"
    r"saudi|riyadh|gulf|umrah|nri|visa|h-1b", re.I)
STOP = set("""a an the and or of to in on at for by with from as is are was were be been it its this that
these those after over amid into out up down than then about not no new says say said will would could
may might more most less has have had can also but if so how why what when who whom which while you your
their his her they them our we us vs via per one two day today live updates update latest news""".split())


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------
def http_get(url, timeout=20, tries=2, ua=UA):
    """urllib first; fall back to curl (works behind some proxies); return bytes or None.
    HTTP errors (403/429/...) count as failures, never as content."""
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if data:
                    return data
        except Exception:
            pass
    if shutil.which("curl"):
        try:
            r = subprocess.run(["curl", "-sS", "-f", "-L", "-m", str(timeout), "-A", ua, url],
                               capture_output=True, timeout=timeout + 5)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
    return None


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw):
    """Return list of dicts {title, desc, link, date} from RSS or Atom bytes."""
    out = []
    root = ET.fromstring(raw)
    for it in root.iter():
        if local(it.tag) not in ("item", "entry"):
            continue
        f = {"title": "", "desc": "", "link": "", "date": None}
        for ch in it:
            t = local(ch.tag)
            if t == "title":
                f["title"] = clean(ch.text)
            elif t in ("description", "summary", "content") and not f["desc"]:
                f["desc"] = clean(ch.text)
            elif t == "link":
                f["link"] = (ch.attrib.get("href") or ch.text or "").strip()
            elif t in ("pubDate", "published", "updated", "date") and not f["date"]:
                f["date"] = parse_date(ch.text)
        if f["title"]:
            out.append(f)
    return out


def split_google_title(title):
    """Google News titles look like 'Headline - Source'. Return (headline, source)."""
    if " - " in title:
        h, s = title.rsplit(" - ", 1)
        if 2 <= len(s) <= 40:
            return h.strip(), s.strip()
    return title, None


def fetch_one(feed, cutoff):
    section, source, url, weight = feed
    raw = http_get(url)
    if not raw:
        return feed, [], "no response"
    try:
        items = parse_feed(raw)
    except Exception as e:
        return feed, [], f"parse error: {e}"
    now = dt.datetime.now(dt.timezone.utc)
    res = []
    for f in items:
        d = f["date"]
        if d is None or d < cutoff or d > now + dt.timedelta(hours=1):
            continue
        title, src = f["title"], source
        if "news.google.com" in url:
            title, gsrc = split_google_title(title)
            if gsrc:
                src = gsrc
            f["desc"] = ""  # Google News descriptions are just link markup
        res.append({"section": section, "source": src, "weight": weight, "title": title,
                    "desc": f["desc"], "link": f["link"], "date": d})
    return feed, res, None


# --------------------------------------------------------------------------------------
# Dedup: cluster near-identical stories across sources
# --------------------------------------------------------------------------------------
def tokens(title):
    words = re.findall(r"[\w₹$%.]+", title.lower())
    toks = set()
    for w in words:
        w = w.strip(".")
        if len(w) < 3 or w in STOP:
            continue
        if w.endswith("s") and len(w) > 4 and not w.endswith("ss"):
            w = w[:-1]
        toks.add(w)
    return toks


def similar(a, b):
    """Two token sets describe the same story?"""
    if not a or not b:
        return False
    inter = len(a & b)
    if inter < 3:
        return False
    jacc = inter / len(a | b)
    contain = inter / min(len(a), len(b))
    # duplicate wording, or the same event reported from another angle
    return jacc >= 0.45 or (contain >= 0.75 and inter >= 4) or (contain >= 0.42 and inter >= 4)


def cluster(items):
    """Greedy single-link clustering via union-find (items sorted newest first)."""
    toks = [tokens(i["title"]) for i in items]
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if similar(toks[i], toks[j]):
                parent[find(j)] = find(i)
    groups = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(items[i])
    return list(groups.values())


# --------------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------------
def story_from_group(group, now):
    # Representative headline = the most "central" member (shares most words with the rest),
    # nudged by source trust and by having a real description. Guards against stray chain-merges.
    tk = [tokens(g["title"]) for g in group]

    def centrality(idx):
        t = tk[idx]
        if not t:
            return 0.0
        return sum(len(t & o) / len(t) for j, o in enumerate(tk) if j != idx) / max(len(group) - 1, 1)

    ranked = sorted(range(len(group)),
                    key=lambda i: -(centrality(i) * 3 + group[i]["weight"] * 0.8
                                    + (0.4 if len(group[i]["desc"]) > 40 else 0)))
    best = dict(group[ranked[0]])
    group = [group[i] for i in ranked]   # best-first, used below for links
    # prefer a direct article link over a Google News redirect
    if "news.google.com" in best["link"]:
        for g in group:
            if g["link"] and "news.google.com" not in g["link"]:
                best["link"] = g["link"]
                break
    sources = []
    for g in group:
        if g["source"] not in sources:
            sources.append(g["source"])
    newest = max(g["date"] for g in group)
    secs = {}
    for g in group:
        secs[g["section"]] = secs.get(g["section"], 0) + 1
    section = sorted(secs, key=lambda s: (-secs[s], TIE_PRIORITY[s]))[0]
    if "Tamil Nadu" in secs and secs["Tamil Nadu"] >= 1 and section == "India":
        section = "Tamil Nadu"
    text = best["title"].lower()
    age_h = max((now - newest).total_seconds() / 3600, 0)
    score = min(len(sources), 6) * 2.0                       # coverage by independent outlets
    score += max(g["weight"] for g in group) * 1.5           # source trust
    score += math.exp(-age_h / 12.0) * 2.0                   # freshness
    for pts, words in BOOST.items():
        if any(w in text for w in words):
            score += pts
    if JUNK_RE.search(best["title"]):
        score -= 6.0
    if section == "India" and not INDIA_RE.search(text):
        score -= 6.0   # global lifestyle / US sports leaking in from general feeds
    if section == "Markets" and not MARKET_RE.search(text):
        score -= 6.0   # Nobel prizes, tennis, etc. leaking in from economy feeds
    if len(best["desc"]) < 40:
        score -= 0.5
    return {"section": section, "title": best["title"], "desc": best["desc"], "link": best["link"],
            "sources": sources, "newest": newest, "score": score, "n": len(group),
            "members": [{"source": g["source"], "title": g["title"], "link": g["link"], "desc": g["desc"]}
                        for g in group]}


def pick_top(stories, top, per_source_cap=3):
    """Greedy pick, best first, with two guards:
       - per-source cap, so one outlet can't fill the section;
       - diversity penalty, so 4 angles on the same event don't take 4 slots
         (the extra angles are counted as 'related' on the story that was kept)."""
    pool = sorted(stories, key=lambda s: -s["score"])
    for s in pool:
        s["_t"] = tokens(s["title"])
        s["related"] = 0
    chosen, used = [], {}
    while pool and len(chosen) < top:
        def adj(s):
            overlap = max((len(s["_t"] & c["_t"]) / max(min(len(s["_t"]), len(c["_t"])), 1)
                           for c in chosen), default=0.0)
            return s["score"] - (8.0 * overlap if overlap >= 0.3 else 0.0)
        best = max(pool, key=adj)
        pool.remove(best)
        if len(best["sources"]) == 1 and used.get(best["sources"][0], 0) >= per_source_cap:
            continue
        for c in chosen:  # same event as something already chosen -> note as related
            if len(best["_t"] & c["_t"]) / max(min(len(best["_t"]), len(c["_t"])), 1) >= 0.5:
                c["related"] += 1
                c["members"].extend(best["members"])
                break
        else:
            chosen.append(best)
            if len(best["sources"]) == 1:
                used[best["sources"][0]] = used.get(best["sources"][0], 0) + 1
    return chosen


# --------------------------------------------------------------------------------------
# Trends and quotes
# --------------------------------------------------------------------------------------
def fetch_trends(top=8):
    out = {}
    for region, url in TRENDS:
        raw = http_get(url)
        rows = []
        if raw:
            try:
                root = ET.fromstring(raw)
                for it in root.iter("item"):
                    row = {"term": "", "traffic": "", "headline": "", "src": ""}
                    for ch in it:
                        t = local(ch.tag)
                        if t == "title":
                            row["term"] = clean(ch.text)
                        elif t == "approx_traffic":
                            row["traffic"] = clean(ch.text)
                        elif t == "news_item" and not row["headline"]:
                            for n in ch:
                                if local(n.tag) == "news_item_title":
                                    row["headline"] = clean(n.text)
                                elif local(n.tag) == "news_item_source":
                                    row["src"] = clean(n.text)
                    rows.append(row)
            except Exception:
                pass
        def vol(r):
            digits = re.sub(r"\D", "", r["traffic"] or "")
            return int(digits) if digits else 0
        rows.sort(key=vol, reverse=True)
        out[region] = rows[:top]
    return out


def fetch_quotes():
    def one(q):
        label, sym, nd = q
        # Yahoo rejects long browser user-agents (429) but accepts a plain one.
        for host in ("query1", "query2"):
            raw = http_get(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                           + urllib.parse.quote(sym) + "?range=1d&interval=1d",
                           timeout=15, tries=1, ua="Mozilla/5.0")
            try:
                m = json.loads(raw)["chart"]["result"][0]["meta"]
                price, prev = m["regularMarketPrice"], m.get("chartPreviousClose")
                chg = (price - prev) / prev * 100 if prev else None
                return label, f"{price:,.{nd}f}" + (f" ({chg:+.2f}%)" if chg is not None else "")
            except Exception:
                continue
        return label, None

    with cf.ThreadPoolExecutor(3) as ex:  # gentle: Yahoo rate-limits bursts
        return [(l, v) for l, v in ex.map(one, QUOTES) if v]


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# AI writing step - OpenRouter, FREE MODELS ONLY, no credit card.
#   Pass 1  select + merge: which events matter most per section (headlines/blurbs only)
#   Pass 2  write: full article text for those events -> labelled, detailed points
# Every number and every quotation in the output is checked against the source text;
# points that fail the check are dropped. Any failure falls back to the plain digest.
# Key comes from the OPENROUTER_API_KEY environment variable - never put it in this file.
# Benchmarked 12 Oct 2026: nemotron-3-super (8s, valid JSON, covered key stories, 0 invented numbers),
# openrouter/free works with reasoning off, laguna-s-2.1 good but slower, nemotron-ultra missed a top
# story, gemma-4 was rate-limited upstream.
# --------------------------------------------------------------------------------------
AI_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
    "poolside/laguna-s-2.1:free",
]
# per section: (long write-ups, one-paragraph items, minor one-liners)
SECTION_PLAN = {"India": (2, 3, 5), "Tamil Nadu": (1, 6, 3), "Markets": (0, 8, 0), "World": (1, 5, 3)}
BOILER = re.compile(r"subscribe|also read|read more|telegram feeds|follow us|sign up|copyright|all rights"
                    r" reserved|click here|e-?paper|download the app|whatsapp channel|advertisement|"
                    r"^\s*(photo|image|file photo)|published -|top trending stocks|what.s moving sensex", re.I)


def is_free_model(m):
    """Hard guard: only ':free' variants or OpenRouter's free router may ever be called."""
    return m.endswith(":free") or m == "openrouter/free"


def ai_call(model, prompt, key, timeout=150):
    if not is_free_model(model):
        raise ValueError(f"refusing non-free model {model}")
    body = json.dumps({
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 7000, "temperature": 0.2,
        "reasoning": {"enabled": False},                      # reasoning models otherwise burn the budget
        "provider": {"max_price": {"prompt": 0, "completion": 0}},   # second guard: zero price or fail
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "news-digest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception:
        if not shutil.which("curl"):
            raise
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as hf, tempfile.NamedTemporaryFile("wb", delete=False) as bf:
            hf.write(f"Authorization: Bearer {key}"); hf.close(); bf.write(body); bf.close()
            r = subprocess.run(["curl", "-sS", "-f", "-m", str(timeout), "-H", "Content-Type: application/json",
                                "-H", f"@{hf.name}", "-d", f"@{bf.name}",
                                "https://openrouter.ai/api/v1/chat/completions"], capture_output=True)
        os.unlink(hf.name); os.unlink(bf.name)
        if r.returncode != 0:
            raise RuntimeError("request failed")
        raw = r.stdout
    j = json.loads(raw)
    return j["choices"][0]["message"].get("content") or ""


def ai_json(prompt, key, check=None):
    """Try each free model in order; return (parsed_json, model) for the first valid answer."""
    for model in AI_MODELS:
        try:
            text = ai_call(model, prompt, key)
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            obj = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            if check is None or check(obj):
                return obj, model
        except Exception as e:
            print(f"  AI model {model} failed: {str(e)[:70]}", file=sys.stderr)
    return None, None


# ---- article text ---------------------------------------------------------------------
def extract_text(raw):
    page = raw.decode("utf-8", "ignore")
    try:
        import trafilatura                       # optional: pip install trafilatura (better extraction)
        t = trafilatura.extract(page, include_comments=False, include_tables=False)
        if t and len(t) > 300:
            return t
    except Exception:
        pass
    from html.parser import HTMLParser
    skip_tags = ("script", "style", "nav", "footer", "aside", "header", "form", "noscript")

    class P(HTMLParser):
        def __init__(s):
            super().__init__()
            s.skip, s.in_p, s.buf, s.paras = 0, False, [], []

        def handle_starttag(s, tag, a):
            if tag in skip_tags:
                s.skip += 1
            if tag == "p" and not s.skip:
                s.in_p, s.buf = True, []

        def handle_endtag(s, tag):
            if tag in skip_tags:
                s.skip = max(0, s.skip - 1)
            if tag == "p" and s.in_p:
                s.in_p = False
                t = " ".join("".join(s.buf).split())
                if len(t) >= 60:
                    s.paras.append(t)

        def handle_data(s, d):
            if s.in_p and not s.skip:
                s.buf.append(d)
    p = P()
    try:
        p.feed(page)
    except Exception:
        pass
    return "\n".join(p.paras)


def article_text(url, limit):
    if not url or "news.google.com" in url:
        return ""
    raw = http_get(url, timeout=15, tries=1)
    if not raw:
        return ""
    lines = [l.strip() for l in extract_text(raw).split("\n") if l.strip() and not BOILER.search(l)]
    t = "\n".join(lines)
    return t[:limit] if len(t) >= 400 else ""


def ev_links(members, n=4):
    """Distinct outlets, preferring direct article links (not Google redirects)."""
    seen, out = set(), []
    direct = [m for m in members if m["link"] and "news.google.com" not in m["link"]]
    for m in (direct or members):          # Google News redirect links only as a last resort
        if m["source"] in seen or not m["link"]:
            continue
        seen.add(m["source"])
        out.append((m["source"], m["link"]))
        if len(out) == n:
            break
    return out


def make_event(stories):
    """Merge one or more pool stories (same event) into an event record."""
    members = [m for s in stories for m in s["members"]]
    srcs = []
    for s in stories:
        for x in s["sources"]:
            if x not in srcs:
                srcs.append(x)
    return {"title": stories[0]["title"], "members": members, "sources": srcs,
            "score": max(s["score"] for s in stories) + 0.8 * len(srcs),
            "newest": max(s["newest"] for s in stories), "links": ev_links(members),
            "texts": [], "blurbs": ""}


# ---- pass 1: select and merge ---------------------------------------------------------
def ai_select(pool, key):
    cands, lines = [], []
    for sec in SECTION_ORDER:
        lines.append(f"\n## {sec}")
        for st in pool[sec]:
            lines.append(f'[{len(cands)}] ({len(st["sources"])} outlets) {st["title"]} || {short(st["desc"], 160)}')
            cands.append(st)
    plan = "\n".join(
        f'- {sec}: "main" = the {d + f} most important events for this reader, most important first; '
        f'"minor" = {m} further short items' if m else
        f'- {sec}: "main" = the {f} most market-moving events'
        for sec, (d, f, m) in SECTION_PLAN.items())
    prompt = (
        "You are the editor of a daily news digest for a reader in Tamil Nadu, India.\n"
        "Candidates are listed per section as: [id] (number of outlets) headline || blurb.\n"
        "1. Several candidates may describe the SAME event from different angles: group their ids together.\n"
        "   Be aggressive about merging: three or more candidates about one event (e.g. a march, a court case, an "
        "attack, a market close) must become ONE group.\n"
        "2. Pick events per section (an id may be used only once overall). Skip stock tips, listicles, trivia. "
        "For Markets pick events covering DIFFERENT facets: index close, RBI/rupee/bonds, inflation or data, "
        "company results, IPOs, oil/gold, global markets.\n"
        f"{plan}\n"
        'Return ONLY JSON like {"India":{"main":[[3,7],[1]],"minor":[[9]]},"Tamil Nadu":{...},'
        '"Markets":{"main":[...]},"World":{...}} where each inner list holds the ids of ONE event.\n'
        + "\n".join(lines))

    def ok(o):
        return isinstance(o, dict) and sum(len(o.get(s, {}).get("main", [])) for s in SECTION_ORDER) >= 8
    obj, model = ai_json(prompt, key, ok)
    if not obj:
        return None, None
    used, out = set(), {}
    for sec in SECTION_ORDER:
        d, f, m = SECTION_PLAN[sec]
        res = {}
        for kind, cap in (("main", d + f), ("minor", m)):
            evs = []
            for grp in obj.get(sec, {}).get(kind, []):
                ids = [i for i in (grp if isinstance(grp, list) else [grp])
                       if isinstance(i, int) and 0 <= i < len(cands) and i not in used]
                if not ids:
                    continue
                used.update(ids)
                evs.append(make_event([cands[i] for i in ids]))
                if len(evs) == cap:
                    break
            if kind == "main":   # most-covered / highest-scoring first, regardless of the model's order
                evs.sort(key=lambda e: -e["score"])
            res[kind] = evs
        out[sec] = res
    return out, model


def select_plain(pool):
    out = {}
    for sec in SECTION_ORDER:
        d, f, m = SECTION_PLAN[sec]
        st = pool[sec]
        out[sec] = {"main": [make_event([s]) for s in st[:d + f]],
                    "minor": [make_event([s]) for s in st[d + f:d + f + m]]}
    return out


def fetch_event_texts(selection):
    """Download article bodies for the chosen events (parallel). Fills ev['texts'] and ev['blurbs']."""
    jobs = []
    for sec, res in selection.items():
        d, f, m = SECTION_PLAN[sec]
        for idx, ev in enumerate(res["main"]):
            deep = idx < d
            k, lim = (3, 3500) if deep else (2, 2500)
            ev["blurbs"] = "\n".join(f'[{x["source"]}] {x["title"]}: {short(x["desc"], 300)}'
                                     for x in ev["members"][:4])
            seen = set()
            for mem in sorted(ev["members"], key=lambda x: "news.google.com" in (x["link"] or "")):
                if mem["source"] in seen or not mem["link"] or "news.google.com" in mem["link"]:
                    continue
                seen.add(mem["source"])
                jobs.append((ev, mem, lim))
                if len(seen) == k:
                    break
        for ev in res["minor"]:
            ev["blurbs"] = "\n".join(f'[{x["source"]}] {x["title"]}: {short(x["desc"], 300)}'
                                     for x in ev["members"][:2])
    with cf.ThreadPoolExecutor(10) as ex:
        texts = list(ex.map(lambda j: article_text(j[1]["link"], j[2]), jobs))
    n = 0
    for (ev, mem, _), t in zip(jobs, texts):
        if t:
            ev["texts"].append((mem["source"], mem["title"], t))
            n += 1
    return n


# ---- pass 2: write --------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^0-9a-zऀ-෿]+", "", s.lower())


NOINFO = re.compile(r"\b(no|not)\b[^.]{0,40}\b(data|information|details|mention|provided|specified|available)\b|"
                    r"\bnot provided\b|\bremains unclear\b|\bwill (take effect|emerge)\b", re.I)


DATELINE = re.compile(r"^\s*[A-Z][A-Z .,/'-]{2,30}:\s+")


def tidy(text, max_sent=None, max_words=None):
    text = DATELINE.sub("", clean(text))
    if max_sent or max_words:
        sents = re.split(r"(?<=[.!?;])\s+(?=[A-Z\"“₹])", text)
        if max_sent:
            sents = sents[:max_sent]
        out, n = [], 0
        for x in sents:
            w = len(x.split())
            if max_words and out and n + w > max_words:
                break
            out.append(x)
            n += w
        text = " ".join(out).rstrip(";")
        if text and text[-1] not in ".!?\"”)":
            text += "."
    return text


def point_ok(text, corpus_plain, corpus_norm):
    """Reject filler, and points containing a number or quotation that is not in the source text."""
    if NOINFO.search(text):
        return False
    # whole-number match (not substring): "99" must not be accepted because "0.99" or "95.99" appears
    nums = [x.replace(",", "").rstrip(".") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    have = set(re.findall(r"\d+(?:\.\d+)?", corpus_plain))
    if any(len(x) > 1 and x not in have for x in nums):
        return False
    for q in re.findall(r'[“"]([^”"]{15,})[”"]', text):
        if norm(q) not in corpus_norm:
            return False
    return True


def event_corpus(ev):
    return ev["blurbs"] + "\n" + "\n".join(t for _, _, t in ev["texts"]) + "\n" + ev["title"]


def event_block(ev, label):
    parts = [f"{label}  (outlets: {', '.join(ev['sources'][:6])})"]
    for src, title, text in ev["texts"]:
        parts.append(f"--- [{src}] {title}\n{text}")
    parts.append(f"--- headlines/blurbs:\n{ev['blurbs']}")
    return "\n".join(parts)


def write_section(sec, res, quotes_line, now_ist):
    """Returns (section_dict, model, dropped) or (None, None, 0)."""
    d, f, m = SECTION_PLAN[sec]
    events = res["main"]
    if not events:
        return None, None, 0
    if sec == "Markets":
        blocks = "\n\n".join(event_block(ev, f"MATERIAL {i + 1}") for i, ev in enumerate(events))
        corpus = quotes_line + "\n" + "\n".join(event_corpus(ev) for ev in events)
        prompt = (
            f"You are writing the Markets & economy section of a daily India news digest. Today is {now_ist}.\n"
            "Use ONLY the LIVE NUMBERS and the MATERIAL below. Never add facts, numbers, names or quotes that are "
            "not in them. If sources give different figures for the same thing, say so.\n"
            "Write 8-10 points, each {\"label\": 1-3 words, \"text\": 1-2 sentences, max 45 words, with concrete numbers}. "
            "Skip a topic entirely if the material has nothing on it - never write that data is missing. "
            "Cover as applicable, in this order: Close (index levels, moves, breadth), Why (the drivers), RBI / "
            "policy, Rupee, Inflation / data, Earnings, IPOs and listings, Commodities (oil, gold), Global.\n"
            'Return ONLY JSON: {"points":[{"label":"...","text":"..."}]}\n\n'
            f"LIVE NUMBERS: {quotes_line}\n\n{blocks}")
        obj, model = ai_json(prompt, os.environ["OPENROUTER_API_KEY"].strip(),
                             lambda o: len(o.get("points", [])) >= 4)
        if not obj:
            return None, None, 0
        cp, cn = corpus.lower().replace(",", ""), norm(corpus)
        pts, dropped = [], 0
        for p in obj["points"]:
            t = clean(str(p.get("text", "")))
            if len(t.split()) >= 5 and point_ok(t, cp, cn):
                pts.append({"label": clean(str(p.get("label", ""))) or "Note", "text": t})
            else:
                dropped += 1
        return ({"events": [], "minor": [], "points": pts,
                 "links": [x for ev in events for x in ev["links"][:1]][:6]}, model, dropped) if len(pts) >= 4 else (None, None, 0)

    blocks = []
    for i, ev in enumerate(events):
        blocks.append(event_block(ev, f"EVENT {i + 1} ({'LONG write-up' if i < d else 'SHORT paragraph'})"))
    minors = "\n".join(f"MINOR {j + 1}: {ev['title']} || {short(ev['members'][0]['desc'], 220)}"
                       for j, ev in enumerate(res["minor"]))
    prompt = (
        f"You are writing the {sec} section of a daily news digest for a reader in Tamil Nadu, India. Today is {now_ist}.\n"
        "Use ONLY the source text below. Never add facts, numbers, names or quotes that are not in it; if outlets "
        "disagree (including different figures for the same thing), say so in the point. Be concrete: who, what, numbers, dates, short exact quotes.\n"
        f"For each EVENT write: \"label\" (2-4 word topic), \"headline\" (clear English, max 14 words) and \"points\". "
        f"LONG events (the first {d}) get 4-7 points of max 40 words each; SHORT events get exactly 1 point of max 60 words. "
        "Only write a point if the text really contains that information - never pad, speculate or write generic "
        "filler such as 'details will emerge'. "
        "Each point is {\"label\": 1-3 words such as The march / Reaction / What's next / Background / Numbers / "
        "Impact, \"text\": 1-2 sentences}. Order a long event: what happened, key details, reactions, what's next.\n"
        "For each MINOR item write {\"label\": short topic or place, \"text\": one sentence of max 30 words}. "
        "Paraphrase in your own words, never copy sentences verbatim, and never start with a dateline such as 'CHENNAI:'. "
        "If two items cover the same event, write it once and leave the duplicate out of the JSON.\n"
        'Return ONLY JSON: {"events":[{"k":1,"label":"","headline":"","points":[{"label":"","text":""}]}],'
        '"minor":[{"j":1,"label":"","text":""}]}\n\n' + "\n\n".join(blocks) + "\n\n" + minors)
    obj, model = ai_json(prompt, os.environ["OPENROUTER_API_KEY"].strip(),
                         lambda o: len(o.get("events", [])) >= max(1, len(events) // 2))
    if not obj:
        return None, None, 0
    out_ev, dropped = [], 0
    by_k = {e.get("k"): e for e in obj.get("events", []) if isinstance(e, dict)}
    for i, ev in enumerate(events):
        e = by_k.get(i + 1)
        corpus = event_corpus(ev)
        cp, cn = corpus.lower().replace(",", ""), norm(corpus)
        pts = []
        for p in (e or {}).get("points", []):
            t = tidy(str(p.get("text", "")), 3 if i < d else 4, 60 if i < d else 65)
            if len(t.split()) >= 4 and point_ok(t, cp, cn):
                pts.append({"label": clean(str(p.get("label", ""))), "text": t})
            else:
                dropped += 1
        if not pts:
            pts = [{"label": "", "text": short(tidy(ev["members"][0]["desc"]), 300)}] if ev["members"][0]["desc"] else []
        if not pts:
            continue
        out_ev.append({"label": clean(str((e or {}).get("label", ""))), "headline": clean(str((e or {}).get("headline", ""))) or ev["title"],
                       "points": pts, "deep": i < d and len(pts) >= 3, "sources": ev["sources"],
                       "links": ev["links"], "newest": ev["newest"]})
    minor = []
    by_j = {e.get("j"): e for e in obj.get("minor", []) if isinstance(e, dict)}
    for j, ev in enumerate(res["minor"]):
        e = by_j.get(j + 1)
        if not e:
            continue
        t = tidy(str(e.get("text", "")), 1, 32)
        corpus = ev["title"] + " " + ev["members"][0]["desc"]
        if len(t.split()) >= 4 and point_ok(t, corpus.lower().replace(",", ""), norm(corpus)):
            if any(similar(tokens(t), tokens(m["text"])) for m in minor) or \
               any(similar(tokens(t), tokens(" ".join(p["text"] for p in o["points"]))) for o in out_ev):
                continue
            minor.append({"label": clean(str(e.get("label", ""))) or ev["title"][:30], "text": t,
                          "link": ev["links"][0][1] if ev["links"] else ""})
        else:
            dropped += 1
    return {"events": out_ev, "minor": minor, "points": []}, model, dropped


def plain_section(selection_sec):
    """No-AI representation: publisher blurbs only."""
    evs = []
    for ev in selection_sec["main"]:
        m0 = ev["members"][0]
        evs.append({"label": "", "headline": ev["title"],
                    "points": [{"label": "", "text": short(m0["desc"], 300)}] if m0["desc"] else [],
                    "deep": False, "sources": ev["sources"], "links": ev["links"], "newest": ev["newest"]})
    minor = [{"label": short(ev["title"], 70), "text": short(ev["members"][0]["desc"], 160) if ev["members"][0]["desc"].strip()[:30] != ev["title"].strip()[:30] else "",
              "link": ev["links"][0][1] if ev["links"] else ""} for ev in selection_sec["minor"]]
    return {"events": evs, "minor": minor, "points": []}


def ai_digest(pool, quotes_line, now):
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    now_ist = now.astimezone(IST).strftime("%A %d %B %Y")
    print("  AI pass 1: selecting and merging events...", file=sys.stderr)
    sel, m1 = ai_select(pool, key)
    if not sel:
        sel, m1 = select_plain(pool), None
    print("  fetching article text...", file=sys.stderr)
    n_art = fetch_event_texts(sel)
    print(f"  AI pass 2: writing ({n_art} articles read)...", file=sys.stderr)
    models, dropped, sections = set(), 0, {}
    if m1:
        models.add(m1)
    with cf.ThreadPoolExecutor(4) as ex:
        futs = {sec: ex.submit(write_section, sec, sel[sec], quotes_line, now_ist) for sec in SECTION_ORDER}
        for sec, fu in futs.items():
            try:
                sd, model, dr = fu.result()
            except Exception as e:
                print(f"  {sec} failed: {e}", file=sys.stderr)
                sd, model, dr = None, None, 0
            if sd:
                models.add(model)
                dropped += dr
                sections[sec] = sd
            else:
                sections[sec] = plain_section(sel[sec])
    ok = any(sections[s].get("points") or any(e["deep"] or len(e["points"]) for e in sections[s]["events"])
             for s in SECTION_ORDER) and models
    return sections, sorted(models), {"articles": n_art, "dropped": dropped}


# --------------------------------------------------------------------------------------
# Rendering (markdown for console, HTML for reading)
# --------------------------------------------------------------------------------------
def ago(d, now):
    m = int((now - d).total_seconds() / 60)
    return f"{m}m ago" if m < 60 else f"{m // 60}h ago"


def short(text, n=230):
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "..."


def srcline_md(ev):
    if ev["links"]:
        return ", ".join(f"[{s}]({u})" for s, u in ev["links"])
    return ", ".join(ev["sources"][:4])


def render_md(data, now):
    ist = now.astimezone(IST).strftime("%a %d %b %Y, %I:%M %p IST")
    L = [f"# News digest - {ist}", ""]
    if data["quotes"]:
        L += ["**Markets now:** " + " | ".join(f"{l} {v}" for l, v in data["quotes"]), ""]
    glance = []
    for sec in SECTION_ORDER:
        sd = data["sections"][sec]
        if sd["events"]:
            glance.append(f"- **{sec}:** {sd['events'][0]['headline']}")
        elif sd["points"]:
            gp = next((p for p in sd["points"] if p["label"].lower().startswith("why")), sd["points"][0])
            glance.append(f"- **{sec}:** {short(gp['text'], 120)}")
    if glance:
        L += ["**At a glance**"] + glance + [""]
    for sec in SECTION_ORDER:
        sd = data["sections"][sec]
        if not (sd["events"] or sd["points"]):
            continue
        L += [f"## {sec}"]
        for p in sd["points"]:
            L.append(f"- **{p['label']}:** {p['text']}")
        if sd["points"] and sd.get("links"):
            L.append("- Sources: " + ", ".join(f"[{s}]({u})" for s, u in sd["links"]))
        for ev in sd["events"]:
            pts = ev["points"]
            if ev["deep"]:
                L += ["", f"### {ev['headline']}"]
                for p in pts:
                    L.append(f"- **{p['label']}:** {p['text']}")
                L.append(f"- Sources: {srcline_md(ev)} - {ago(ev['newest'], data['now'])}")
                L.append("")
            else:
                text = " ".join(p["text"] for p in pts)
                lab = ev["label"] or ev["headline"]
                L.append(f"- **{lab}:** {text} ({srcline_md(ev)})")
        if sd["minor"]:
            L += ["", "**More stories**"]
            for mnr in sd["minor"]:
                L.append(f"- **{mnr['label']}:** {mnr['text']}")
        L.append("")
    L += ["## Trending (Google Trends, X substitute)", ""]
    for region, rows in data["trends"].items():
        if rows:
            L.append(f"**{region}**")
            for r in rows:
                t = f" ({r['traffic']})" if r["traffic"] else ""
                h = f" - {short(r['headline'], 110)}" if r["headline"] else ""
                L.append(f"- {r['term']}{t}{h}")
            L.append("")
    st = data.get("ai_stats") or {}
    if data.get("ai_models"):
        L += ["---", f"Written by {', '.join(data['ai_models'])} (free tier) from {st.get('articles', 0)} full articles. "
              f"Numbers and quotes were checked against the sources; {st.get('dropped', 0)} unverifiable point(s) dropped."]
    else:
        L += ["---", "Summaries: publisher blurbs (AI step off or unavailable)"]
    if data["failed"]:
        L += ["Feeds that failed this run: " + ", ".join(data["failed"])]
    return "\n".join(L)


def render_html(data, now):
    e = html.escape
    ist = now.astimezone(IST).strftime("%A %d %B %Y, %I:%M %p IST")

    def chips(ev):
        if ev["links"]:
            inner = " ".join(f'<a class=chip href="{e(u)}">{e(s)}</a>' for s, u in ev["links"])
        else:
            inner = " ".join(f'<span class=chip>{e(s)}</span>' for s in ev["sources"][:4])
        return f"<details class=srcs><summary>Sources ({max(len(ev['links']), len(ev['sources'][:4]))})</summary>{inner}</details>"

    def sid(sec):
        return "s-" + re.sub(r"\W+", "", sec.lower())

    def bullets(pts):
        return "".join(f"<li><b>{e(p['label'])}</b>{': ' if p['label'] else ''}{e(p['text'])}</li>" for p in pts)

    H = [f"<h1>News digest</h1><p class=sub>{e(ist)}</p>"]
    nav = "".join(f'<a href="#{sid(sec)}">{e(sec)}</a>' for sec in SECTION_ORDER
                  if data["sections"][sec]["events"] or data["sections"][sec]["points"])
    nav += '<a href="#s-trending">Trending</a>'
    H.append(f"<nav>{nav}</nav>")
    if data["quotes"]:
        H.append('<div class=quotes>' + "".join(
            f"<div><span>{e(l)}</span><b>{e(v)}</b></div>" for l, v in data["quotes"]) + "</div>")
    glance = []
    for sec in SECTION_ORDER:
        sd = data["sections"][sec]
        if sd["events"]:
            glance.append(f"<li><a href='#{sid(sec)}'><b>{e(sec)}</b>{e(sd['events'][0]['headline'])}</a></li>")
        elif sd["points"]:
            gp = next((p for p in sd["points"] if p["label"].lower().startswith("why")), sd["points"][0])
            glance.append(f"<li><a href='#{sid(sec)}'><b>{e(sec)}</b>{e(short(gp['text'], 120))}</a></li>")
    if glance:
        H.append("<section class=glance><h2>At a glance</h2><ul>" + "".join(glance) + "</ul></section>")
    for sec in SECTION_ORDER:
        sd = data["sections"][sec]
        if not (sd["events"] or sd["points"]):
            continue
        H.append(f'<section id="{sid(sec)}"><h2>{e(sec)}</h2>')
        if sd["points"]:
            H.append(f"<ul>{bullets(sd['points'])}</ul>")
            if sd.get("links"):
                H.append('<p class=src>' + " ".join(f'<a class=chip href="{e(u)}">{e(s)}</a>' for s, u in sd["links"]) + "</p>")
        flat = []
        for ev in sd["events"]:
            if ev["deep"]:
                H.append(f"<article><h3>{e(ev['headline'])}</h3><ul>{bullets(ev['points'])}</ul>"
                         f"<div class=src>{chips(ev)}<span class=ago>{e(ago(ev['newest'], data['now']))}</span></div></article>")
            else:
                text = " ".join(p["text"] for p in ev["points"])
                lab = ev["label"] or ev["headline"]
                flat.append(f"<li><b>{e(lab)}</b><p>{e(text)}</p>{chips(ev)}</li>")
        if flat:
            H.append("<ul class='flat cards'>" + "".join(flat) + "</ul>")
        if sd["minor"]:
            H.append("<h4>More stories</h4><ul>" + "".join(
                f'<li><b>{e(m["label"])}:</b> {e(m["text"])}'
                + (f' <a class=chip href="{e(m["link"])}">link</a>' if m.get("link") else "") + "</li>"
                for m in sd["minor"]) + "</ul>")
        H.append("</section>")
    tr = []
    for region, rows in data["trends"].items():
        if rows:
            tr.append(f"<h4>{e(region)}</h4><ul>" + "".join(
                f"<li><b>{e(r['term'])}</b>" + (f" <span class=ago>{e(r['traffic'])}</span>" if r["traffic"] else "")
                + (f" - {e(short(r['headline'], 110))}" if r["headline"] else "") + "</li>" for r in rows) + "</ul>")
    if tr:
        H.append("<section id=s-trending><h2>Trending (Google Trends, X substitute)</h2>" + "".join(tr) + "</section>")
    st = data.get("ai_stats") or {}
    foot = (f"Written by {', '.join(data['ai_models'])} (free tier) from {st.get('articles', 0)} full articles. "
            f"Numbers and quotes were checked against the sources; {st.get('dropped', 0)} unverifiable point(s) dropped."
            if data.get("ai_models") else "Summaries: publisher blurbs (AI step off or unavailable).")
    if data["failed"]:
        foot += " Feeds that failed: " + ", ".join(data["failed"])
    H.append(f"<footer>{e(foot)}</footer>")
    css = (":root{--bg:#fff;--fg:#1b1d21;--mut:#6b7280;--card:#f6f7f9;--line:#e5e7eb;--acc:#1d4ed8}"
           "@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaed;--mut:#9aa0a6;--card:#1c1f25;"
           "--line:#2b2f37;--acc:#8ab4f8}}"
           "*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:56px;-webkit-text-size-adjust:100%}"
           "body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}"
           "main{max-width:720px;margin:0 auto;padding:16px 16px 64px}"
           "h1{margin:.2rem 0 0;font-size:1.6rem}.sub{margin:.1rem 0 .8rem;color:var(--mut);font-size:.9rem}"
           "nav{position:sticky;top:0;z-index:5;display:flex;gap:8px;overflow-x:auto;margin:0 -16px;padding:8px 16px;"
           "background:var(--bg);border-bottom:1px solid var(--line);scrollbar-width:none}nav::-webkit-scrollbar{display:none}"
           "nav a{flex:none;padding:8px 14px;border-radius:99px;background:var(--card);border:1px solid var(--line);"
           "color:var(--fg);text-decoration:none;font-size:.9rem;font-weight:600}"
           "h2{margin:1.8rem 0 .7rem;font-size:1.3rem}"
           "h3{margin:0 0 .5rem;font-size:1.1rem;line-height:1.35}"
           "h4{margin:1.2rem 0 .3rem;color:var(--mut);font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}"
           "ul{margin:.3rem 0;padding-left:0;list-style:none}"
           "article ul li,.cards>li{margin:.6rem 0}article ul li b{display:block;font-size:.78rem;color:var(--acc);"
           "text-transform:uppercase;letter-spacing:.04em}"
           "section>ul:not(.cards):not(.glance ul) li{margin:.6rem 0}"
           "article,.cards>li{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0}"
           ".cards>li>b{display:block;margin-bottom:.2rem}.cards p{margin:.1rem 0 .3rem}"
           ".quotes{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:.5rem 0 1rem}"
           ".quotes div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}"
           ".quotes span{display:block;font-size:.72rem;color:var(--mut)}.quotes b{font-size:.95rem}"
           ".glance ul{padding:0}.glance li{border-bottom:1px solid var(--line)}"
           ".glance a{display:block;padding:10px 0;color:var(--fg);text-decoration:none}"
           ".glance b{display:block;font-size:.75rem;color:var(--acc);text-transform:uppercase;letter-spacing:.05em}"
           ".src{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:.6rem 0 0;font-size:.82rem}"
           ".srcs summary{cursor:pointer;color:var(--mut);font-size:.82rem;padding:6px 0;list-style:none}"
           ".srcs summary::before{content:'\\25B8 '}.srcs[open] summary::before{content:'\\25BE '}"
           ".chip{display:inline-block;padding:6px 12px;border:1px solid var(--line);border-radius:99px;color:var(--acc);"
           "text-decoration:none;font-size:.8rem;margin:2px 4px 2px 0}"
           ".ago{color:var(--mut);font-size:.8rem;margin-left:auto}"
           "footer{margin-top:2.5rem;color:var(--mut);font-size:.8rem;border-top:1px solid var(--line);padding-top:.8rem}"
           "@media(min-width:600px){body{font-size:16px}.quotes{grid-template-columns:repeat(5,1fr)}}")
    return ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,"
            "initial-scale=1'><title>News digest</title><style>" + css + "</style><body><main>"
            + "".join(H) + "</main></body></html>")


# --------------------------------------------------------------------------------------
def build(hours, top, use_ai=True):
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    items, failed = [], []
    with cf.ThreadPoolExecutor(12) as ex:
        for feed, res, err in ex.map(lambda f: fetch_one(f, cutoff), FEEDS):
            if err:
                failed.append(f"{feed[1]} ({err})")
            items += res
    items.sort(key=lambda i: i["date"], reverse=True)
    stories = [story_from_group(g, now) for g in cluster(items)]
    by_sec = {}
    for s in stories:
        by_sec.setdefault(s["section"], []).append(s)
    pool = {sec: pick_top(by_sec.get(sec, []), 16) for sec in SECTION_ORDER}
    quotes, trends = fetch_quotes(), fetch_trends()
    quotes_line = " | ".join(f"{l} {v}" for l, v in quotes)
    sections, models, stats = None, [], {}
    if use_ai and os.environ.get("OPENROUTER_API_KEY", "").strip():
        try:
            sections, models, stats = ai_digest(pool, quotes_line, now)
        except Exception as e:
            print("AI step failed, using plain digest:", e, file=sys.stderr)
            sections = None
    if not sections:
        models = []
        sections = {sec: plain_section({"main": [make_event([s]) for s in pool[sec][:top]], "minor": []})
                    for sec in SECTION_ORDER}
    return {"sections": sections, "trends": trends, "quotes": quotes, "failed": failed, "now": now,
            "raw": len(items), "stories": len(stories), "ai_models": models, "ai_stats": stats}, now


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--hours", type=int, default=24, help="look-back window (default 24)")
    ap.add_argument("--top", type=int, default=8, help="stories per section in the plain (no-AI) digest")
    ap.add_argument("--out", default=".", help="output folder for digest.md / digest.html")
    ap.add_argument("--no-ai", action="store_true", help="skip the AI step even if OPENROUTER_API_KEY is set")
    ap.add_argument("--quiet", action="store_true", help="don't print the digest")
    a = ap.parse_args()
    data, now = build(a.hours, a.top, use_ai=not a.no_ai)
    md = render_md(data, now)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "digest.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(a.out, "digest.html"), "w", encoding="utf-8") as f:
        f.write(render_html(data, now))
    if not a.quiet:
        print(md)
    print(f"\n[{data['raw']} items -> {data['stories']} unique stories; "
          f"{len(data['failed'])} feed(s) failed]", file=sys.stderr)


if __name__ == "__main__":
    main()
