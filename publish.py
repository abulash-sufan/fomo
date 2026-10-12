"""Builds the GitHub Pages site folder: today's digest, a 7-day archive, and an index.

usage: python publish.py out/digest.html site
`site` is a checkout of the gh-pages branch (may be empty). Dated pages older than KEEP_DAYS are deleted.
"""
import datetime as dt
import pathlib
import re
import shutil
import sys

KEEP_DAYS = 7
src, site = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
site.mkdir(exist_ok=True)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
today = dt.datetime.now(IST).date()

shutil.copy(src, site / f"{today}.html")                      # today's page (overwrites a re-run)
for f in site.glob("????-??-??.html"):                        # prune old days
    try:
        if (today - dt.date.fromisoformat(f.stem)).days >= KEEP_DAYS:
            f.unlink()
    except ValueError:
        pass

days = sorted((f.stem for f in site.glob("????-??-??.html")), reverse=True)
past = [d for d in days if d != str(today)]
html = (site / f"{today}.html").read_text(encoding="utf-8")
if past:
    links = "".join(f'<a class=chip href="{d}.html">{dt.date.fromisoformat(d):%a %d %b}</a>' for d in past)
    block = f"<section><h2>Earlier this week</h2><p class=src>{links}</p></section>"
    html = html.replace("</main>", block + "</main>")
(site / "index.html").write_text(html, encoding="utf-8")
(site / ".nojekyll").write_text("")
(site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
print("published", today, "| archive:", ", ".join(past) or "none")