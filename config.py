"""Configuration and API keys for the GreenGate3D lead scraper."""
import os

# --- Load a local .env file (no external dependency) ---
def _load_env():
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_env()

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Brave Search. Optional, but it replaces the most expensive step in the
# pipeline: Claude's server-side web search pulled ~47k tokens of page content
# into context per call (81% of a measured run). With Brave we get the result
# URLs, fetch the few that matter with the crawler we already have, and read
# them with the deterministic extractors — usually with no model call at all.
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

# --- Per-run key overrides (hosted / bring-your-own-key mode) ----------------
# A hosted copy serves several people at once, each with their own keys, so
# keys can't live in module globals. A run sets its keys for its own thread
# with use_keys(); key() reads them, falling back to the globals above when no
# override is active (local use, tests).
import threading
from contextlib import contextmanager

_run_keys = threading.local()
KEY_NAMES = ("GOOGLE_PLACES_API_KEY", "ANTHROPIC_API_KEY", "BRAVE_API_KEY")


def key(name):
    keys = getattr(_run_keys, "keys", None)
    if keys is not None:
        return keys.get(name, "")
    return globals()[name]


@contextmanager
def use_keys(keys):
    """Run a block with exactly these keys (missing ones count as unset)."""
    prev = getattr(_run_keys, "keys", None)
    _run_keys.keys = dict(keys)
    try:
        yield
    finally:
        _run_keys.keys = prev


# Hosted mode: when set, visitors must enter this password to run on the keys
# configured above. Without it, anyone who finds the URL could spend them, so
# a hosted copy with no password only accepts visitors' own keys.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Set HOSTED=1 on the server. The server's own keys are then never used
# without APP_PASSWORD, even if they are present in the environment.
HOSTED = os.environ.get("HOSTED", "0") == "1"
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))

# Places found in earlier runs are skipped (before any crawl or AI spend).
# The history lives in this folder; on a host, point it at a persistent disk.
HISTORY_DIR = os.environ.get("HISTORY_DIR",
                             os.path.join(os.path.dirname(__file__), "data"))

# MOCK_MODE=1 skips all live API calls and uses canned sample data,
# so you can test the UI and Excel output without keys.
MOCK_MODE = os.environ.get("MOCK_MODE", "0") == "1"

# --- Models, split by task ---------------------------------------------------
# Cost is dominated by the per-lead extraction call, which runs once for every
# company and carries the whole crawled site text. That job is mechanical:
# pull stated facts out of supplied text into a fixed schema. A small model
# does it well, so it does not need the flagship.
#
# The search-backed jobs (finding a website, discovering places, filling gaps)
# reason over live web results and benefit from a stronger model, but they run
# far less often.
#
# Prices per million tokens (input / output), for the estimate printed after
# each run:
#     claude-haiku-4-5  $1 / $5
#     claude-sonnet-5   $3 / $15
#     claude-opus-5     $5 / $25
# MEASURED, not assumed. A 6-lead Deep run costs ~$1.14, split:
#     gap search        81%   (web results are ~47k tokens per call)
#     discovery search  11%
#     extraction         4%   <- once per lead, and barely registers
#     Google Places      4%
# Extraction was assumed to dominate and does not, so it is NOT the place to
# economise: running it on Haiku saved $0.20 and cost most of the people data
# (one university lead went from 32 named staff to zero). Keep a capable model
# here and take the savings out of the search calls instead.
EXTRACT_MODEL = "claude-sonnet-5"    # quality matters, cost barely moves
SEARCH_MODEL = "claude-sonnet-5"     # web-search calls: the real cost centre
CLAUDE_MODEL = EXTRACT_MODEL         # backwards-compatible alias

# NOTE: the ephemeral cache_control on the extraction system prompt is a
# no-op today — Haiku 4.5's minimum cacheable prefix is 4096 tokens and the
# prompt is far shorter. It is left in place so it starts working if the
# prompt grows or the model changes; it is not counted as a saving.
MODEL_PRICES = {                     # USD per million tokens (input, output)
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
}
# Google Places text search, per request. The field mask decides the SKU;
# rating/userRatingCount put it in the priciest tier.
PLACES_COST_PER_REQUEST = 0.04
# Anthropic server-side web search, per search performed.
WEB_SEARCH_COST_PER_USE = 0.01
# Brave Search: free tier allows 2,000 queries/month at 1 query/second.
# Set to your plan's rate if you are on a paid tier.
BRAVE_COST_PER_QUERY = 0.0
BRAVE_MIN_INTERVAL = 1.1     # seconds between queries (free tier is 1 QPS)
BRAVE_MAX_PAGES = 3          # result pages fetched and read per gap fill

# When Brave is configured, gap filling searches with Brave and reads the
# pages itself. Set False to force the (far more expensive) Claude web-search
# path even when a Brave key is present.
PREFER_BRAVE_SEARCH = True
# If the free path finds nothing, optionally fall back to Claude's web search.
# Off by default: that fallback is what made runs expensive.
GAP_SEARCH_LLM_FALLBACK = False

# Default business type searched around the chosen location. Editable in the UI.
DEFAULT_TARGET = "makerspace"

# Google Places matches the literal phrase you send, so a single query finds
# only businesses that describe themselves that way -- "makerspace in
# Knoxville, TN" returned just 4 results, while the same city has fab labs,
# hackerspaces, and machine shops that never matched. Each variant is a
# separate (billed) text search; results are deduped by place ID.
# Ordered best-first: `max_results` stops the fan-out partway, so the terms
# most likely to name a filament buyer must come before the general ones.
# "print shop"/"sign shop" are deliberately absent — they return paper
# printers (AlphaGraphics, digital print bureaus), not 3D-printing shops.
PLACES_QUERY_VARIANTS = [
    "makerspace", "maker space", "hackerspace", "fab lab", "fabrication lab",
    "3D printing service", "3D printing company", "additive manufacturing",
    "rapid prototyping", "prototyping lab", "university makerspace",
    "library makerspace", "community workshop", "tool library",
    "engineering lab", "machine shop", "woodworking shop",
    "metal fabrication shop", "craft studio",
]

# When Google Places returns no website for a place, use Claude's web search to
# find the official site (adds a search-billed call per website-less place).
# Without this, those places have no crawlable source and stay contact-less.
ENABLE_WEBSITE_SEARCH = True

# Discover prospects that are NOT on Google Maps (unlisted co-ops, university
# labs, library makerspaces, shops listed only in maker directories) by web
# searching the location as a second source. Costs one search-billed call per
# run, not per place.
ENABLE_SEARCH_DISCOVERY = True

# Render JavaScript-only sites with a headless browser before scraping. Many
# modern site builders (Framer, Wix, Squarespace, React) ship an empty HTML
# shell, so a plain fetch sees no text and no links at all.
# Requires: pip install playwright && playwright install chromium
ENABLE_JS_RENDER = False
JS_RENDER_MAX_PAGES = 8      # rendered pages per site (rendering is slow)

# Crawl depth. Pages are ranked by relevance, but nothing is excluded for
# failing to match a keyword — page names are unpredictable, and filtering
# them out is what leaves contact boxes empty.
MAX_CRAWL_PAGES = 40         # sub-pages fetched per site
MAX_CRAWL_DEPTH = 3          # link hops from the homepage

# Two-step sub-page scrape. Big institutions (a government agency, a
# university) put a broad homepage in front of the small labs and programs we
# actually want to contact. Step 1 reads the homepage and picks out links whose
# URL slug matches one of these; step 2 crawls those pages first and follows
# their links one hop further than the normal depth cap. Hyphenated entries
# match as a word set, so "exhibit-design" also matches "/design-exhibit".
PRIORITY_SLUGS = (
    "labs", "lab", "services", "service", "research", "technology",
    "maker", "makers", "makerspace", "exhibit-design",
)

# Makerspaces very often publish their equipment list on a wiki or tool
# subdomain rather than the main site. Only these specific maker-related
# subdomains are probed and crawled — following every subdomain would drag a
# university's entire web estate into one lead.
# After crawling, web-search for the fields the site itself never published
# (email, phone, printer models). Crawling can only find what an organization
# puts on its own pages; these often live in directory listings, library
# guides, or news write-ups instead. One search per lead that still has a gap.
ENABLE_GAP_SEARCH = True

# Drop leads that show no sign of 3D printing: no printer models AND no
# equipment highlights, and a priority listed in DROP_PRIORITIES. Applied after
# crawling and gap search. Leads whose site could not be read are always kept,
# since "unreadable" is not evidence of "no printers".
# LOW alone almost never triggers (reachability alone scores MEDIUM), so MEDIUM
# is included; remove it to be more conservative.
DROP_EMPTY_LEADS = True
DROP_PRIORITIES = ("LOW", "MEDIUM")
# Also skip the Claude extraction and gap search for a readable site whose crawl
# found no PRIORITY_SLUGS page and no mention of 3D printing at all. Saves cost;
# the lead is counted as dropped. Requires DROP_EMPTY_LEADS.
SKIP_UNPROMISING_SITES = True

CRAWL_MAKER_SUBDOMAINS = True
MAKER_SUBDOMAINS = ("wiki", "makerspace", "fablab", "make", "maker",
                    "tools", "equipment")

# Some servers (several university sites among them) ship an incomplete TLS
# certificate chain, so verification fails and the whole site yields nothing.
# When True, such a site is retried once WITHOUT certificate verification.
# We only ever issue GETs for public marketing pages and never send
# credentials, so the exposure is reading content from an impostor server
# rather than leaking anything. Set to False to skip those sites instead.
ALLOW_INSECURE_TLS_FALLBACK = True

# Merge places that resolve to the same website host into one row — an
# "extension" space (e.g. NYU MakerGarage) and its parent (NYU MakerSpace)
# share a site and should not appear as two separate leads.
MERGE_SAME_WEBSITE = True

# ---------------------------------------------------------------------------
# EDIT THIS: a short, factual description of GreenGate3D. Each lead's "Why
# GreenGate3D" summary/fit assessment is grounded on this blurb plus the
# verified facts scraped for that company. If any detail here is wrong, fix
# it — that assessment is only as accurate as this text.
# ---------------------------------------------------------------------------
COMPANY_BLURB = (
    "GreenGate3D makes recycled PETG 3D-printing filament in East Tennessee. "
    "The pitch is that shops printing PETG can switch to a locally made, "
    "recycled-material spool with comparable performance."
)
