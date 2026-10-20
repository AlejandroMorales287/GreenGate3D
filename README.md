# GreenGate3D Lead Finder

A small local web app: pick a location, get an `.xlsx` of prospect makerspaces/shops
for GreenGate3D — with a short AI-written summary and fit assessment per company.
**Nothing is invented.** Columns that can't be verified automatically are left blank.

## Pipeline

```
DISCOVERY (two sources, merged)
  Google Places, many queries -> businesses with a Maps listing
  Claude web search           -> everything without one: university and library
                                 makerspaces, member co-ops, hackerspaces, and
                                 shops listed only in maker directories
  -> resolve a website        (Places websiteUri, else Claude web search)
  -> merge duplicates          (same website host, or same name across sources)
  -> DEEP CRAWL                (sitemap + breadth-first link walk, JS-rendered
                                when the page is an empty shell)
  -> Claude structured extract (key people w/ role + reason, equipment, summary + fit)
  -> Excel
```

The **Found Via** column records each row's provenance, so it is visible at a
glance which leads Google Maps would never have surfaced.

**Contact precedence is website-first, Maps-second — and both are kept.** The
website number reaches the shop directly; the Maps number is the fallback, so
both live in the **Phone** cell (each on its own labeled line) rather than
overwriting each other.

## Summary columns

| Column(s) | Source | Filled? |
|---|---|---|
| Organization, City, Type, Address, Hours | Google Places API | ✅ |
| Related Locations | Places that share this website (e.g. NYU MakerGarage under NYU MakerSpace) | ⚠️ when duplicates exist |
| Website, Website Source | Places `websiteUri`; when absent, found via Claude web search (source column records which) | ⚠️ |
| Phone | One cell, one line per source: the site's own number and the Maps number, each labeled (a source's line is omitted if it wasn't found) | ⚠️ |
| Email | The site itself — JSON-LD, `mailto:` links, header/footer text, de-obfuscated addresses | ⚠️ when published |
| Other Contacts | One cell per company: every named person with role, email, phone, LinkedIn, and a one-line reason they matter, plus any other site emails not tied to a named person. Decision-makers sort first. | ⚠️ when the site names staff |
| All Emails (CC) | Every email found for the company (primary, each named person's, and other site emails), deduped and separated by `; ` so the whole cell can be pasted straight into an email's CC line. | ⚠️ when emails are published |
| Equipment Highlights, 3D Printer Models | Claude, from the site's own text only | ⚠️ |
| Why GreenGate3D | Claude: a terse buying-signal note — member/student counts, printer models and whether they already run PETG, budget/procurement authority, mission language, growth signals — pulled from About/Mission/membership pages and grounded only in what the site states | ✅ |
| Priority | **Derived**: 3D evidence, reachability, named people, review count | ✅ |
| Last Verified | One cell, one line per source: when Places data was pulled and when the site was crawled (a source's line is omitted if it wasn't used) | ✅ |
| **Notes** | **Left blank on purpose — Rich's column** (shaded cream in the sheet) | ⬜ |

People are only listed when the site gives them a **stated position**. Someone
named incidentally — a member who owns a tool, a testimonial, a class post —
is deliberately excluded, so an empty Other Contacts cell means "the site names no
staff", not "the crawler missed it".

## Quick start (Windows, no coding)

1. Install Python 3.12 from https://www.python.org/downloads/ — on the first
   installer screen, tick **"Add python.exe to PATH"**.
2. Unzip this folder somewhere with a short path, e.g. `Documents\GreenGate3D`.
3. Double-click **`start.bat`**. The first run takes a few minutes to set up,
   then opens a `.env` file in Notepad: paste your API keys (see step 2 below)
   after the `=` signs, save, and double-click `start.bat` again.
4. From then on, double-click `start.bat` to run. Keep its window open while
   you use the app; close it to stop.

## Setup (one time, manual)

1. Install Python packages:
   ```
   pip install -r requirements.txt
   ```
   For JavaScript-only sites, also install the headless browser once:
   ```
   playwright install chromium
   ```
   (Skip it and set `ENABLE_JS_RENDER = False`; those sites will just yield less.)
2. Get two API keys:
   - **Google Places** — Google Cloud Console → enable **"Places API (New)"** → create an API key.
   - **Anthropic** — console.anthropic.com → API keys.
3. Copy `.env.example` to `.env` and paste your keys in. **Never share or commit `.env`.**
4. **Edit `config.py` → `COMPANY_BLURB`** so it accurately describes GreenGate3D.
   The "Why GreenGate3D" summary/fit assessment is only as accurate as that text.

## Run

```
python app.py
```

Your browser opens to http://127.0.0.1:8000 (set `PORT` to use another port).
Enter a location (e.g. `Knoxville, TN`), optionally change the business type
(default `makerspace`), max results, and search depth, then click **Generate
spreadsheet**. Progress is shown per company ("Scraping 7/20: Knox Makers")
and the `.xlsx` downloads when the run finishes.

### Search depth

Deeper crawling fills more boxes but takes proportionally longer — roughly
10-20 seconds per company on **Deep**, so a 20-company run is several minutes.

| Depth | Places queries | Per site | Web-search discovery | JS rendering | Gap search |
|---|---|---|---|---|---|
| Quick | 1 | 10 pages, 1 hop | ❌ | ❌ | ❌ |
| Standard | 8 | 25 pages, 2 hops | ✅ | ✅ | ❌ |
| Deep (default) | all (19) | 40 pages, 3 hops | ✅ | ✅ | ✅ |

**Budget for the time.** Deep spends roughly 60-100 seconds per company
(crawl + extraction + gap search), so 20 companies is around half an hour and
60 is a couple of hours. Set "Max results" to what you can actually work
through, and use Quick to scout a location first.

Use **Quick** to sanity-check a location cheaply; use **Deep** for the real run.

## Test without keys

Set `MOCK_MODE=1` in `.env` to see the UI and Excel output with sample data — no API
calls, no charges. (PowerShell one-off: `$env:MOCK_MODE = '1'; python app.py`)

## How the scraper works

**One Places query is not enough.** Places matches the literal phrase you send,
so it only returns businesses that describe themselves that way. `"makerspace
in Knoxville, TN"` returns **4 results**; running the same city across related
phrasings (`fab lab`, `hackerspace`, `3D printing service`, `additive
manufacturing`, `machine shop`, …) and deduping by place ID returns **60** —
including 3D Print Knox, CADform 3D, the UTK Fab Lab, and Oak Ridge National
Laboratory's Manufacturing Demonstration Facility, none of which matched the
original query. Every place found is then deep-crawled the same way.

The variant list (`PLACES_QUERY_VARIANTS`) is ordered best-first, because
`max_results` stops the fan-out partway — the terms most likely to name a
filament buyer are tried before the general ones. Each variant is a separate
billed text search, so Quick uses one query, Standard eight, and Deep all of
them. Google fuzzy-matches "printing", so a few paper-print shops come back;
they score LOW priority because their sites show no 3D-printing evidence.

**Finding the website.** Many small shops have no `websiteUri` in Places, which
used to leave them with no email or contact detail at all. When Places has none
(or only a Facebook/Yelp page), Claude web-searches for the real site. Set
`ENABLE_WEBSITE_SEARCH = False` in `config.py` to skip this and save the call.

**Merging extension spaces.** An annex and its parent (NYU MakerGarage / NYU
MakerSpace) publish one shared site, so Places returns them as two places that
would crawl to identical content. Places resolving to the same website host are
merged before crawling — the most-reviewed one becomes the row, the others are
listed under Related Locations. This also saves an AI call per duplicate.
Website-less places and shared social pages are never merged. Toggle with
`MERGE_SAME_WEBSITE`.

**Crawling: keywords rank pages, they never exclude them.** This is the single
most important rule here. Page names are unpredictable — real sites use
`/who-are-we`, `/make`, `/operations`, `/space` — so filtering the crawl to
keyword-matching URLs silently discards the pages that carry contact and staff
detail, which is what leaves boxes empty. Instead the crawler walks the whole
site breadth-first (`MAX_CRAWL_PAGES`, `MAX_CRAWL_DEPTH` in `config.py`) from
the full sitemap plus every internal link, visiting the most relevant pages
first and dropping only obvious junk (`/cart`, `/login`, assets).

Path matching is token-based, so word order and separators don't matter:
`/who-are-we`, `/who_we_are`, and `/whoWeAre` all match. Keywords are matched
against the path only — never the hostname, or a shop at `teamfab.com` would
score every page it has as a staff page.

**JavaScript-only sites.** Framer, Wix, Squarespace, and React sites often ship
an HTML shell with no text and no links until scripts run — a plain fetch sees
nothing at all. Those pages are detected and re-fetched through a headless
browser. Fetching stays parallel; rendering runs on one thread, because
Playwright's sync API has thread affinity. Disable with `ENABLE_JS_RENDER`.

**Reading contact details.** Emails come from `mailto:` links, JSON-LD, visible
text, Cloudflare's `data-cfemail` obfuscation, and hand-obfuscated forms like
`info [at] shop [dot] org`, preferring addresses on the company's own domain.
Phone numbers are mined from a text view that *keeps* header/footer/nav —
that's where sites put them — and a number sitting next to a "Phone:" label
beats a bare one elsewhere on the page.

**Awkward servers.** Sites that present a broken TLS certificate chain are
retried without verification (only public pages are read and no credentials are
ever sent — set `ALLOW_INSECURE_TLS_FALLBACK = False` to skip them instead).
Sites that refuse plain HTTP clients are retried through the headless browser.
Some servers still refuse every connection; when that happens the row's **Last
Verified (Website)** reads `site unreachable`, so an unreadable site is never
mistaken for a company that simply publishes no contact details.

**Equipment lives on wikis and subdomains.** Makerspaces routinely keep the
tool inventory on `wiki.<domain>`, which a strict same-host crawl never sees.
A short list of maker-ish subdomains (`wiki`, `makerspace`, `fablab`, `tools`,
…) is probed and crawled as part of the site. Only those names — following
every subdomain would drag a whole university's web estate into one lead.

**Printer models get two independent passes.** Claude reads the site text with
judgement, and a `\b`-anchored gazetteer of printer makes and models scans the
*untruncated* crawl. The two are unioned. The second pass matters because staff
pages outrank equipment pages, so on a large site the equipment text can be
truncated before Claude ever sees it — and because a plain substring match is
unsafe (`anet` matches inside "pl**anet**ary").

**Gap search: the last resort for empty boxes.** Crawling can only find what an
organization publishes on its own pages. Phone numbers, public inboxes, and
printer inventories often live elsewhere — directory listings, library guides,
news write-ups. On **Deep**, any lead still missing an email, phone, or printer
model gets one targeted web search for *only* those fields. Every value is
re-validated (emails against a pattern, printer models against the gazetteer)
and a blank is preferred over a guess. Anything found this way is labelled in
the **Found Via** column as `… via web search`, so a searched value is never
mistaken for one read off the company's own site.

**Performance.** Each page is parsed exactly once. Deep crawls are network-bound,
not CPU-bound, and run about 10-20 seconds per company.

**Extraction.** Structured outputs guarantee valid JSON, so a malformed reply
can't silently blank a row. One broken website or one failed AI call degrades
that single lead instead of aborting the run.

## What a run costs

Every run prints a breakdown when it finishes — Places requests, each type of
Claude call, tokens in/out, and a total with a per-lead figure. Read that
instead of guessing; the numbers move with depth and location.

**Add a Brave Search key.** It is the single biggest saving. Without one,
filling missing contacts uses Claude's server-side web search, which pulls
whole pages into the model's context — measured at ~47k input tokens per call
and **81% of a run's cost**. With a Brave key the app searches with Brave,
fetches the two or three relevant pages with the crawler it already has, and
reads them with the deterministic extractors (the same regexes that find
emails, phones, and printer models on a company's own site). That path uses
**no model call at all**. Brave's free tier is 2,000 queries/month.

Values taken off a third-party page are validated first: the page must
actually name the organization, and an email is only accepted if it sits on
the company's own domain — otherwise a directory listing would donate its own
sales address as the lead's contact.

Where the money goes, and the levers:

| Driver | Why it costs | Lever |
|---|---|---|
| Extraction (1 call per lead, carries the whole site text) | Highest volume, biggest inputs | Runs on **Haiku 4.5** ($1/$5), not the flagship — it is mechanical schema-filling, not reasoning |
| Gap search (web search per lead with a gap) | Several billed searches each | Skipped for leads with no sign of being a prospect; **Deep only** |
| Website lookup (per website-less place) | One search each | **Deep/Standard only** — Quick never runs it |
| Places fan-out (up to 19 queries) | Each variant is a billed request | Quick 1 query, Standard 8, Deep all |
| Site crawling | Free — just bandwidth | Depth presets bound the page count |

The single largest saving is the model split: extraction is ~5x cheaper on
Haiku than on Opus and runs once per lead, so it dominates. `EXTRACT_MODEL`
and `SEARCH_MODEL` in `config.py` are separate knobs — raise `EXTRACT_MODEL`
to `claude-sonnet-5` if extraction quality drops on your data, and re-check the
printed cost.

**Cheapest useful settings:** Quick depth with a small "Max results" scouts a
location for a few cents. Reserve Deep for locations you intend to work.

## Notes on cost & limits

- Google Places (New) text search and each extraction/pitch (one Claude call per
  company) both cost money per run. Keep "Max results" modest while testing.
- The tool only reads public data (Places listings + public company websites). It
  does **not** scrape Google Maps directly or LinkedIn.

## Deploy (free, on Render)

1. Push this folder to a GitHub repo (`.env` is git-ignored, so keep it that way).
2. On render.com: **New > Blueprint**, pick the repo. `render.yaml` sets up a
   free web service running `gunicorn` with one worker.
3. Open the URL. Each visitor enters their own Google Places / Anthropic /
   (optional) Brave keys in the **API keys** box. Keys are used for that run
   only and never stored on the server; "Remember keys" keeps them in that
   visitor's browser.

Optional shared keys: set `GOOGLE_PLACES_API_KEY`, `ANTHROPIC_API_KEY`,
`BRAVE_API_KEY` and `APP_PASSWORD` in the Render dashboard. With `HOSTED=1`
(set by the blueprint) those keys are used only when the visitor enters the
password, so a stranger who finds the URL can't spend them.

Limits of the free plan: the service sleeps after 15 minutes without traffic
(about 30-60 s to wake), and a run in progress is lost if the tab is closed
long enough for it to sleep, or on a redeploy. `MAX_CONCURRENT_JOBS`
(default 2) caps simultaneous runs to fit the 512 MB of memory.

**Not repeating earlier runs.** Every run's places are saved in `data/` and
skipped by later runs, before any crawl or AI cost (untick "Skip places found
in earlier runs" to include them). Visitors using their own keys each get a
separate history. The free plan's disk is wiped on restart, so there the
durable record is your past spreadsheets: upload them in "Earlier
spreadsheets to skip". For history that survives restarts, add a Render disk
(paid) and set `HISTORY_DIR` to its mount path.

## Tests

```
python run_tests.py
```

Runs offline unit tests (URL handling, email extraction, JSON-LD parsing, scoring
rules, Excel output) plus an optional live crawl check.
