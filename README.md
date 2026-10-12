# News digest (free, no API keys)

Pulls ~25 RSS feeds + Google Trends + live Sensex/Nifty/USDINR/Gold/Brent, merges duplicate
stories across outlets, ranks them, and writes `digest.md` + `digest.html`.

## Run it
    python news_digest.py                  # last 24h, top 8 per section
    python news_digest.py --hours 12 --top 10 --out out

## AI summaries (optional, free, no credit card)
Sign up at openrouter.ai (no card), create a key, then:  export OPENROUTER_API_KEY=...
The script then merges angles on the same story, picks the most important per section and writes
grounded 2-line summaries. It only ever calls FREE models (hard-coded guard + zero-price cap on every
request) in this order: nemotron-3-super:free -> openrouter/free -> laguna-s-2.1:free. Any failure
falls back to the plain rule-based digest. Never paste the key into the code; use the env var / a GitHub secret.
Free accounts have a daily request cap (about 50), far more than this needs (1-3 calls/day).

## Daily on your phone (free: GitHub Actions + GitHub Pages)
The workflow runs at 06:30 IST, publishes the page to GitHub Pages, keeps the last 7 days and deletes older ones.
Setup: push this folder to a public repo, add the secret OPENROUTER_API_KEY (Settings -> Secrets and variables ->
Actions), run the workflow once ("Run workflow"), then Settings -> Pages -> Source: branch gh-pages, folder / (root).
Your page: https://<username>.github.io/<repo>/

## Tuning (top of news_digest.py)
FEEDS (add/remove sources, set trust weight), BOOST (words that raise importance), JUNK (patterns that
push stories down), INDIA_RE / MARKET_RE (section relevance), QUOTES (market tickers).

## How the AI digest works (two passes, free models only)
1. **Select/merge**: one call groups the ~16 candidates per section into events.
2. **Write**: the script downloads the full text of up to 3 articles per major event and one more call writes labelled points (What happened / Key details / Reactions / What's next). Numbers and quotes not found in the source text are dropped.

Optional, for better article extraction: `pip install trafilatura`.
Only `:free` models are ever used (hard guard in `is_free_model`). Set `OPENROUTER_API_KEY` as an env var / GitHub secret; never put it in a file.
Known limits: the check can't catch outlets disagreeing on a figure; Google Trends is a stand-in for X.
