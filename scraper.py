"""Core discovery + scraping logic for the GreenGate3D lead tool.

Pipeline
--------
  1. DISCOVERY (multi-source, so prospects off Google Maps are still found)
       a. Google Places (New) text search
       b. Claude web search of the location  -> unlisted co-ops, university and
          library makerspaces, shops listed only in maker directories
     Sources are merged and deduped by website host and normalized name.
  2. WEBSITE RESOLUTION   Places websiteUri, else Claude web search
  3. MERGE                places sharing a website host collapse into one row
                          (an annex and its parent publish one site)
  4. DEEP CRAWL           sitemap + breadth-first link walk, JS-rendered when
                          the page is an empty shell
  5. EXTRACTION           Claude structured outputs: key people (role + reason),
                          equipment, printer models, a summary + fit assessment

Contact precedence is WEBSITE FIRST, MAPS SECOND, and both are kept.

Crawl principle: keywords RANK pages, they never EXCLUDE them. Page names are
unpredictable ("/who-are-we", "/make", "/operations"), so filtering on keywords
silently discards the very pages that carry contact and staff detail.

Nothing is invented: every extracted fact must appear in the site's own text.
"""
import re
import json
import time
import datetime
import threading
import concurrent.futures
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

import config

# The unverified-TLS retry in _fetch is deliberate and logged in config;
# silence the per-request warning so it doesn't drown the progress output.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ",".join([
    "places.id",  # stable key for deduping results across query variants
    "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri", "places.regularOpeningHours", "places.rating",
    "places.userRatingCount", "places.primaryTypeDisplayName", "places.primaryType",
    "places.addressComponents", "places.businessStatus",
])
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_BLOCKLIST = (
    "example.com", "example.org", "domain.com", "yourdomain", "yourcompany",
    "yourname", "user@domain", "name@domain", "email@domain", "@email.com",
    "@company.com", "test@", "sentry.io", "wixpress.com", "@2x", ".png", ".jpg",
    ".gif", ".webp", "no-reply", "noreply", "godaddy.com", "@sentry",
)
UA = {"User-Agent": "Mozilla/5.0 (compatible; GreenGate3D-lead-tool/4.0)"}
OK_CONTENT_TYPES = ("text/html", "application/xhtml")

# Keyword TOKEN SETS. Matching is token-based so word order and separators do
# not matter: "/who-are-we" and "/who-we-are" both match {who, we, are}.
PEOPLE_KEYWORDS = [
    {"team"}, {"staff"}, {"people"}, {"leadership"}, {"board"}, {"officers"},
    {"management"}, {"directory"}, {"faculty"}, {"instructors"}, {"volunteers"},
    {"crew"}, {"founders"}, {"bios"}, {"meet"}, {"who", "we", "are"},
    {"our", "team"}, {"about", "us"},
]
INFO_KEYWORDS = [
    {"about"}, {"mission"}, {"story"}, {"contact"}, {"equipment"}, {"tools"},
    {"facilities"}, {"capabilities"}, {"members"}, {"membership"}, {"shop"},
    {"space"}, {"machines"}, {"services"}, {"operations"}, {"make"}, {"studio"},
    {"lab"},
    # Printer inventories live on pages named for the process, not the gear:
    # a real fab lab kept its models at /studios-learning-labs/fab-lab/3d-printing/.
    {"3d"}, {"printing"}, {"printer"}, {"printers"}, {"fab"}, {"fabrication"},
    {"prototyping"}, {"resources"}, {"studios"}, {"digital"},
]
# Paths that never carry contact or staff detail — skipped to protect the budget.
SKIP_PATH_RE = re.compile(
    r"/(cart|checkout|basket|login|signin|sign-in|register|account|admin|"
    r"wp-admin|wp-login|feed|rss|privacy|terms|tos|legal|cookie|sitemap|"
    r"search|tag|tags|category|categories|archive|author|share|print)(/|$)"
    r"|\.(pdf|jpe?g|png|gif|svg|webp|mp4|mp3|zip|css|js|ico|xml|json)$",
    re.I,
)
MAX_SITE_CHARS = 30000
MAX_FRONTIER = 400          # cap on discovered URLs held in memory per site
NOISE_TAGS = ("script", "style", "noscript", "svg", "form", "header", "footer", "nav")
DEOBF_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+)\s*"
    r"([A-Za-z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*([A-Za-z]{2,})", re.I)
SOCIAL_DOMAINS = ("linkedin.com", "instagram.com", "facebook.com",
                  "twitter.com", "x.com", "youtube.com")
PHONE_TEXT_RE = re.compile(
    r"(?<![\d-])(?:\+?1[\s.\-]*)?\(?([2-9]\d{2})\)?[\s.\-]+([2-9]\d{2})[\s.\-]+(\d{4})(?![\d-])")
PHONE_CONTEXT = ("phone", "call", "tel", "text", "contact", "office", "reach")
GENERIC_HOSTS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "yelp.com", "google.com", "sites.google.com", "linktr.ee",
    "eventbrite.com", "meetup.com", "wixsite.com", "square.site",
)


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------

class LeadList(list):
    """A list of leads that can also carry the run's cost report.

    Plain lists reject attribute assignment, so returning one meant the spend
    report was silently dropped.
    """
    spend = None
    dropped = 0
    seen_before = 0
    processed = ()


def is_empty_lead(lead):
    """True for a lead with no 3D-printing evidence and a droppable priority.

    Leads whose site was unreadable are kept: no text means we don't know.
    """
    if lead.get("verified_web") == "site unreachable":
        return False
    if (lead.get("printer_models") or "").strip():
        return False
    if (lead.get("equipment_highlights") or "").strip():
        return False
    return lead.get("priority") in config.DROP_PRIORITIES


class Spend:
    """Tracks what a run actually costs, per call type.

    Written because the pipeline's cost was being guessed at. Every Claude
    call and Places request is recorded here, so the run can print where the
    money went instead of leaving it to be discovered on a bill.
    """

    def __init__(self):
        self.calls = {}      # label -> {n, model, input, output, searches}
        self.places = 0
        self.brave = 0
        self._lock = threading.Lock()

    def add_places(self, n=1):
        with self._lock:
            self.places += n

    def add_brave(self, n=1):
        with self._lock:
            self.brave += n

    def add_call(self, label, model, msg, searches=0):
        try:
            usage = msg.usage
            inp = (getattr(usage, "input_tokens", 0) or 0)
            inp += (getattr(usage, "cache_read_input_tokens", 0) or 0)
            inp += (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            out = getattr(usage, "output_tokens", 0) or 0
        except AttributeError:
            inp = out = 0
        with self._lock:
            e = self.calls.setdefault(label, {"n": 0, "model": model, "input": 0,
                                              "output": 0, "searches": 0})
            e["n"] += 1
            e["input"] += inp
            e["output"] += out
            e["searches"] += searches

    def _cost(self, e):
        pin, pout = config.MODEL_PRICES.get(e["model"], (5.0, 25.0))
        return (e["input"] / 1e6 * pin + e["output"] / 1e6 * pout
                + e["searches"] * config.WEB_SEARCH_COST_PER_USE)

    def total(self):
        return (sum(self._cost(e) for e in self.calls.values())
                + self.places * config.PLACES_COST_PER_REQUEST
                + self.brave * config.BRAVE_COST_PER_QUERY)

    def report(self, leads=0):
        lines = ["", "COST ESTIMATE", "-" * 62]
        places_cost = self.places * config.PLACES_COST_PER_REQUEST
        lines.append(f"  {'Google Places':<26} {self.places:>4} req"
                     f"{'':>18} ${places_cost:6.2f}")
        if self.brave:
            lines.append(f"  {'Brave search':<26} {self.brave:>4} q"
                         f"{'':>20} ${self.brave * config.BRAVE_COST_PER_QUERY:6.2f}")
        for label, e in sorted(self.calls.items(),
                               key=lambda kv: -self._cost(kv[1])):
            lines.append(f"  {label:<26} {e['n']:>4} calls  "
                         f"{e['input']//1000:>5}k in {e['output']//1000:>4}k out"
                         f"  ${self._cost(e):6.2f}")
        total = self.total()
        lines.append("-" * 62)
        lines.append(f"  {'TOTAL':<26}{'':>29} ${total:6.2f}")
        if leads:
            lines.append(f"  {'per lead':<26}{'':>29} ${total / leads:6.2f}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _origin(url):
    """'https://x.com/page?utm=1' -> 'https://x.com' ('' if unparseable)."""
    if url and "://" not in url:
        url = "https://" + url
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


def _host(url):
    """Hostname with any leading 'www.' removed, lowercased."""
    if url and "://" not in url:
        url = "https://" + url
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def _same_site(url, site_url):
    """True when both URLs share a hostname, ignoring www./scheme differences."""
    h1, h2 = _host(url), _host(site_url)
    return bool(h1) and h1 == h2


# Two-part public suffixes. Without these, "shop.co.uk" reduces to "co.uk" and
# every .co.uk address looks like it belongs to the same organization, which
# silently disables the foreign-address filter for UK/AU/NZ leads.
MULTI_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "org.nz", "net.nz", "ac.nz", "co.za", "org.za",
    "com.br", "com.mx", "co.jp", "or.jp", "ne.jp", "co.in", "co.kr",
    "com.sg", "com.hk", "com.tw", "co.il", "com.tr",
}


def _registrable(host):
    """Registrable domain: 'wiki.knoxmakers.org' -> 'knoxmakers.org'.

    Handles two-part suffixes so 'shop.co.uk' stays 'shop.co.uk' rather than
    collapsing to the suffix itself.
    """
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) < 2:
        return host or ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def maker_subdomains(site_url):
    """Live maker-related subdomains of the site's domain.

    Makerspaces routinely keep the equipment list on a wiki subdomain, which a
    strict same-host crawl never sees. Only the specific maker-ish names are
    probed — following every subdomain would pull an entire university's web
    estate into a single lead.
    """
    if not config.CRAWL_MAKER_SUBDOMAINS:
        return []
    host = _host(site_url)
    if not host:
        return []
    base = _registrable(host)
    found = []
    for sub in config.MAKER_SUBDOMAINS:
        candidate = f"{sub}.{base}"
        if candidate == host:
            continue
        url = f"https://{candidate}/"
        try:
            r = requests.head(url, headers=UA, timeout=6,
                              allow_redirects=True, verify=False)
            if r.status_code < 400 and _host(r.url) not in (host, ""):
                found.append(_origin(r.url) or url)
        except requests.RequestException:
            continue
    return found


def _is_generic_host(url):
    """True for social/aggregator hosts that aren't a company's own website."""
    h = _host(url)
    return bool(h) and any(h == g or h.endswith("." + g) for g in GENERIC_HOSTS)


def _url_path(url):
    """Path + query, lowercased — never the host.

    Keywords must not be matched against the hostname: a shop at 'teamfab.com'
    would otherwise score every page as a 'team' page.
    """
    p = urlparse(url if "://" in (url or "") else "https://" + (url or ""))
    return (p.path + " " + p.query).lower()


def _path_tokens(url):
    """Path split into word tokens, so separators and word order don't matter."""
    return set(re.split(r"[^a-z0-9]+", _url_path(url))) - {""}


# Words too generic to identify one group inside a larger institution.
FOCUS_STOPWORDS = {
    "university", "college", "school", "institute", "department", "center",
    "centre", "community", "public", "county", "city", "state", "national",
    "the", "and", "for", "inc", "llc", "ltd", "company", "services", "group",
}


def focus_tokens(name, site_url=""):
    """Distinctive words from a lead's name, for steering the crawl.

    A lead like "Medbery Makerspace (Hodges Library)" may sit on a site
    covering an entire university library system. Its own pages are the ones
    whose URLs echo its name, so those words are worth boosting.

    Whether a word is useful depends on the site, not the word. "makerspace"
    distinguishes nothing on knoxmakers.org but is precisely what separates the
    makerspace's pages from the rest of lib.utk.edu — so the test is whether
    the word already appears in the hostname (substring, since hosts run words
    together: "3dprintknox" contains both "print" and "knox"). A lead sitting
    on its own domain ends up with no focus tokens, which is correct: every
    page there is already theirs.
    """
    host = _host(site_url)
    toks = set(re.split(r"[^a-z0-9]+", (name or "").lower())) - {""}
    return {t for t in toks
            if len(t) >= 4 and t not in FOCUS_STOPWORDS and t not in host}


def is_priority_page(url):
    """True when the URL path contains a PRIORITY_SLUGS keyword (/labs, /research…).

    Token-based like the other keyword sets, and path-only so a host such as
    'labsupply.com' does not make every page a priority page.
    """
    toks = _path_tokens(url)
    return any(set(re.split(r"[^a-z0-9]+", s.lower())) - {""} <= toks
               for s in config.PRIORITY_SLUGS)


def priority_links(urls):
    """Step 1 of the sub-page scrape: the homepage links worth going deeper on."""
    return sorted(u for u in urls if is_priority_page(u))


def _page_relevance(url, focus=()):
    """Rank pages: the lead's own section first, then staff, then general info.

    This orders the crawl and the assembled text. It never excludes a page —
    unpredictable page names are exactly why boxes come back empty.
    """
    toks = _path_tokens(url)
    people = sum(1 for kw in PEOPLE_KEYWORDS if kw <= toks)
    info = sum(1 for kw in INFO_KEYWORDS if kw <= toks)
    # Outweighs the people bonus: on a shared institutional site, a staff
    # directory for the wrong department is worse than useless.
    own = 4 * sum(1 for t in focus if t in toks)
    # Above the people bonus: a /labs or /research section is what tells us
    # whether the organization prints at all; staff pages come next.
    priority = 6 if is_priority_page(url) else 0
    return own + 3 * people + info + priority


def _should_skip(url):
    # Match the bare path: _url_path appends " <query>", and that trailing
    # space would defeat the "/ or end-of-string" anchors below.
    return bool(SKIP_PATH_RE.search(_url_path(url).strip()))


def _norm(url):
    return url.rstrip("/").lower()


# ---------------------------------------------------------------------------
# Fetching, JS rendering, parsing
# ---------------------------------------------------------------------------

def _clean_text(html):
    """Strip nav/footer/script boilerplate so extraction sees real content."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(NOISE_TAGS)):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _contact_text(html):
    """Text view for mining contact details.

    Unlike _clean_text this KEEPS header/footer/nav — that is exactly where
    sites put their phone number and address, so stripping them (correct for
    reducing prose noise) would hide the number we are looking for.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _fetch(url, retries=2, verify=True):
    """GET a page with retries on transient errors. Returns (html, clean_text).

    A TLS chain failure retries once unverified when allowed: several sites
    (universities especially) omit their intermediate certificate, and without
    the fallback the entire site silently yields nothing. Only public pages are
    requested and no credentials are ever sent.
    """
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=12, allow_redirects=True,
                             verify=verify)
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and any(t in ctype for t in OK_CONTENT_TYPES):
                return r.text, _clean_text(r.text)
            if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(1.0 + attempt)
                continue
            return "", ""
        except requests.exceptions.SSLError:
            if verify and config.ALLOW_INSECURE_TLS_FALLBACK:
                return _fetch(url, retries=1, verify=False)
            return "", ""
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep(0.5)
                continue
            return "", ""
    return "", ""


def looks_like_shell(html, text, links=None):
    """True when a page is a JavaScript shell with no server-rendered content.

    Framer/Wix/React sites commonly return a large HTML document that contains
    almost no text and no links until scripts run — a plain fetch sees nothing.

    `links` may be supplied by a caller that has already parsed the page;
    counting anchors with a regex here avoids a third full parse per page
    (the crawl already parses once for text and once for signals).
    """
    if not html:
        return False
    if links is None:
        links = len(re.findall(r"<a\s[^>]*href=", html, re.I))
    if len(text) < 200 and links <= 2:
        return True
    # Lots of markup, almost no prose, and few links to follow.
    return len(html) > 20000 and len(text) < 1200 and links < 8


class Renderer:
    """Headless browser reused across one site's pages.

    Started lazily and only when a page looks like a JS shell, because
    rendering costs seconds per page while a plain fetch costs milliseconds.

    IMPORTANT: Playwright's sync API has thread affinity — it must be driven
    from the thread that started it. Rendering is therefore done on the calling
    (main) thread only; parallel fetching happens first, and the pages that
    came back as shells are rendered afterwards, serially.
    """

    def __init__(self, enabled=True, budget=None):
        self.enabled = enabled
        self.budget = config.JS_RENDER_MAX_PAGES if budget is None else budget
        self._pw = self._browser = None
        self._failed = False
        self._owner = None

    def _ensure(self):
        if self._browser or self._failed:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception:
            self._failed = True      # playwright missing or browser unavailable
            self._browser = None
        return self._browser

    def html(self, url):
        """Rendered HTML, or '' when rendering is off, budgeted out, or failed.

        Must be called from the thread that owns the browser (see class note).
        """
        if not self.enabled or self.budget <= 0:
            return ""
        me = threading.get_ident()
        if self._owner is None:
            self._owner = me
        elif self._owner != me:
            return ""            # wrong thread: skip rather than corrupt the browser
        browser = self._ensure()
        if not browser:
            return ""
        self.budget -= 1
        page = None
        try:
            # ignore_https_errors mirrors the _fetch TLS fallback: sites with a
            # broken certificate chain would otherwise fail here too.
            page = browser.new_page(
                user_agent=UA["User-Agent"],
                ignore_https_errors=config.ALLOW_INSECURE_TLS_FALLBACK)
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass                     # networkidle is best-effort
            return page.content()
        except Exception:
            return ""
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._browser = self._pw = None


def fetch_page(url, renderer=None):
    """Fetch a page, falling back to the browser when plain HTTP can't read it.

    Two distinct failures need the browser:
      - a JS shell: HTML arrives but the content is script-generated;
      - no response at all: some servers reject non-browser clients outright
        (connection reset) or present a broken TLS chain. A real browser
        negotiates and identifies normally, so it gets the page.
    """
    html, text = _fetch(url)
    if renderer is not None and (not html or looks_like_shell(html, text)):  # noqa: E501
        rendered = renderer.html(url)
        if rendered:
            rtext = _clean_text(rendered)
            if len(rtext) > len(text):
                return rendered, rtext, True
    return html, text, False


def _sitemap_urls(origin):
    """Every same-site URL in the sitemap(s), including nested sitemap indexes.

    Not keyword-filtered: a sitemap is an authoritative list of real pages, and
    filtering it was discarding most of a site's crawlable surface.
    """
    found, seen_maps = set(), set()
    queue = deque(["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"])
    while queue and len(found) < MAX_FRONTIER:
        path = queue.popleft()
        url = path if path.startswith("http") else origin + path
        if _norm(url) in seen_maps:
            continue
        seen_maps.add(_norm(url))
        try:
            r = requests.get(url, headers=UA, timeout=10)
            if r.status_code != 200 or "xml" not in r.headers.get("Content-Type", ""):
                continue
            locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
        except requests.RequestException:
            continue
        nested = "<sitemapindex" in r.text[:2000].lower()
        for loc in locs:
            loc = loc.strip()
            if not _same_site(loc, origin):
                continue
            if nested or loc.lower().endswith(".xml"):
                if len(seen_maps) < 12:
                    queue.append(loc)
            elif not _should_skip(loc):
                found.add(loc.split("#")[0])
    return found


def _internal_links(page_url, html, site_url):
    """EVERY same-site link on a page (minus obvious junk paths).

    Deliberately unfiltered by keyword — ranking decides what gets crawled
    first, but a page is never dropped just because its name is unusual.
    """
    return _links_from_soup(BeautifulSoup(html, "html.parser"), page_url, site_url)[0]


def _decode_cfemail(hexstr):
    """Decode a Cloudflare data-cfemail attribute back to the real address.

    Cloudflare rewrites published emails to a hex blob plus the literal text
    "[email protected]"; without decoding, the address is lost and the
    placeholder can be mistaken for a real one.
    """
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key)
                       for i in range(2, len(hexstr), 2))
    except (ValueError, IndexError):
        return ""


def _cfemails(html):
    out = []
    for m in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html or ""):
        addr = _decode_cfemail(m)
        if EMAIL_RE.fullmatch(addr):
            out.append(addr)
    return out


def _jsonld_from_soup(soup):
    """Emails, phones, and social links from schema.org JSON-LD blocks."""
    emails, phones, socials = [], [], []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, val in node.items():
                    lk = key.lower()
                    if lk == "email" and isinstance(val, str):
                        emails.append(val.replace("mailto:", "").strip())
                    elif lk == "telephone" and isinstance(val, str):
                        phones.append(val.strip())
                    elif lk == "sameas":
                        vals = val if isinstance(val, list) else [val]
                        socials.extend(v for v in vals if isinstance(v, str))
                    elif isinstance(val, (dict, list)):
                        stack.append(val)
            elif isinstance(node, list):
                stack.extend(node)
    return emails, phones, socials


def _extract_jsonld(html):
    """String-input wrapper around _jsonld_from_soup."""
    return _jsonld_from_soup(BeautifulSoup(html, "html.parser"))


def _links_from_soup(soup, page_url, site_url):
    """Same-site links (minus junk paths) plus any social profile links."""
    urls, socials = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")) or href.startswith("#"):
            continue
        full = urljoin(page_url, href).split("#")[0] if page_url else href
        if any(d in full for d in SOCIAL_DOMAINS):
            socials.append(full)
        # site_url=None means "return every link" and let the caller decide
        # scope — needed when a site spans several allowed hosts (a wiki
        # subdomain), since filtering here would drop the cross-host links.
        in_scope = site_url is None or _same_site(full, site_url)
        if in_scope and full.startswith("http") and not _should_skip(full):
            urls.add(full)
    return urls, socials


def _page_signals(html, page_url="", site_url=""):
    """Parse a page ONCE and derive everything downstream needs from it.

    Parsing dominates CPU on a deep crawl. Concatenating every page's HTML and
    re-parsing that blob for JSON-LD and contact text — on top of parsing each
    page for its text and links — made deep runs CPU-bound rather than
    network-bound. Here a single soup yields all four outputs, by stripping
    progressively: JSON-LD and links need the full tree, contact text keeps
    header/footer (that is where phone numbers live), and clean text drops them.
    """
    soup = BeautifulSoup(html, "html.parser")
    jl_emails, jl_phones, jl_socials = _jsonld_from_soup(soup)
    links, link_socials = _links_from_soup(soup, page_url, site_url)

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    contact_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    for tag in soup(["form", "header", "footer", "nav"]):
        tag.decompose()
    clean = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    return {
        "clean_text": clean, "contact_text": contact_text, "links": links,
        "jl_emails": jl_emails, "jl_phones": jl_phones,
        "socials": jl_socials + link_socials,
    }


def _best_email(raw_html, text, jsonld_emails, host=""):
    """Most credible email: JSON-LD > mailto: > visible text; prefers the
    company's own domain and drops placeholder/asset junk."""
    candidates = {}

    def add(addr, score):
        addr = addr.strip().strip(".,;:")
        low = addr.lower()
        if not EMAIL_RE.fullmatch(addr) or any(b in low for b in EMAIL_BLOCKLIST):
            return
        candidates[low] = max(candidates.get(low, 0), score)

    for m in jsonld_emails:
        add(m, 3)
    for m in re.findall(r'mailto:([^"\'?>\s]+)', raw_html, re.I):
        add(m, 2)
    for m in EMAIL_RE.findall(raw_html + " " + text):
        add(m, 1)
    deob = DEOBF_RE.search(text)
    if deob:
        add(f"{deob.group(1)}@{deob.group(2)}.{deob.group(3)}", 1)

    if not candidates:
        return ""

    def rank(item):
        addr, score = item
        return score + (2 if host and addr.endswith("@" + host) else 0)

    return max(candidates.items(), key=rank)[0]


def _best_phone(raw_html, jsonld_phones, text=""):
    """Phone published on the company's own site.

    JSON-LD telephone > tel: link > visible text. Most small-shop sites print
    the number as plain text with no markup at all, so the text pass is what
    actually fills this column; it prefers a number sitting near words like
    "phone" or "call" over a bare number elsewhere on the page.
    """
    for p in jsonld_phones:
        if p.strip():
            return p.strip()
    for m in re.findall(r'tel:([+\d\-\.\s()]{7,})', raw_html, re.I):
        cleaned = m.strip()
        if sum(ch.isdigit() for ch in cleaned) >= 7:
            return cleaned

    best, best_score = "", -1
    for m in PHONE_TEXT_RE.finditer(text):
        # Look only at what PRECEDES the number: labels come before it
        # ("Phone: 718..."). Including trailing text would let one number
        # claim the label belonging to the number after it.
        window = text[max(0, m.start() - 40):m.start()].lower()
        score = 1 if any(w in window for w in PHONE_CONTEXT) else 0
        if score > best_score:
            best, best_score = f"({m.group(1)}) {m.group(2)}-{m.group(3)}", score
        if best_score == 1:
            break
    return best


# Free mail providers: a staffer legitimately using one is common at small
# shops, so these are never treated as "belongs to a different employer".
FREEMAIL = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
            "icloud.com", "protonmail.com", "me.com", "comcast.net", "att.net")


def _is_foreign_domain(email, org_host):
    """True when an address clearly belongs to some other organization.

    Deep crawls of shared or institutional sites pick up outside people — a
    hotel's catering manager from an events page, an accountant from a partner
    listing. Those addresses are worse than blank on a lead row.
    """
    if not email or not org_host:
        return False
    dom = email.rsplit("@", 1)[-1].lower()
    if any(dom == f or dom.endswith("." + f) for f in FREEMAIL):
        return False                      # can't tell; don't discard
    return _registrable(dom) != _registrable(org_host)


def _prefer_own_domain(emails, org_host):
    """Drop addresses that plainly belong to a different organization.

    There is deliberately no "keep everything if nothing survives" fallback.
    Free mail providers are already exempt above, so the only way to filter
    everything out is for every address to sit on another company's domain —
    which is the shared-site contamination this exists to prevent, not a small
    shop publishing a Gmail account. The lead's primary Email is chosen
    separately, so reachability does not depend on this list.
    """
    if not org_host:
        return emails
    return [e for e in emails if not _is_foreign_domain(e, org_host)]


def _all_emails(raw_html, text, jsonld_emails):
    """Every credible email on the site — role inboxes plus personal ones."""
    out = []
    for m in list(jsonld_emails) + re.findall(r'mailto:([^"\'?>\s]+)', raw_html, re.I) \
            + EMAIL_RE.findall(raw_html + " " + text):
        addr = m.strip().strip(".,;:").lower()
        if EMAIL_RE.fullmatch(addr) and not any(b in addr for b in EMAIL_BLOCKLIST) \
                and addr not in out:
            out.append(addr)
    return out[:12]


def crawl_site(website, renderer=None, max_pages=None, max_depth=None, focus=()):
    """Breadth-first crawl of a company's site.

    The frontier is the sitemap plus every internal link found while crawling.
    Pages are visited in relevance order (staff/leadership first) up to a page
    budget, and JS-shell pages are rendered before scraping. Text is assembled
    most-relevant first so people detail survives the size cap.

    Returns {"text", "email", "emails", "phone", "socials", "pages", "rendered"}.
    """
    empty = {"text": "", "email": "", "emails": [], "phone": "", "socials": [],
             "pages": 0, "rendered": 0, "printer_models": [], "hosts": [],
             "priority_pages": []}
    if not website:
        return empty
    origin = _origin(website)
    if not origin:
        return empty
    budget = config.MAX_CRAWL_PAGES if max_pages is None else max_pages
    depth_cap = config.MAX_CRAWL_DEPTH if max_depth is None else max_depth

    home_html, home_text, home_rendered = fetch_page(website, renderer)
    if not home_html and _norm(website) != _norm(origin):
        home_html, home_text, home_rendered = fetch_page(origin, renderer)
    if not home_html:
        return empty

    visited = {_norm(website), _norm(origin)}
    rendered_count = 1 if home_rendered else 0
    pages = []          # (relevance, url, clean_text)
    contact_chunks = []  # header/footer-preserving text, for phones/emails
    raw_chunks = []      # small HTML slices kept only for mailto:/tel: mining
    jl_emails, jl_phones, socials = [], [], []

    # Equipment lists often live on a wiki/tools subdomain; treat those as
    # part of the same site for this crawl.
    allowed = [origin] + maker_subdomains(website)

    def _in_scope(u):
        return any(_same_site(u, a) for a in allowed)

    def absorb(html, url, is_home=False):
        """Parse a page once and fold its signals into the site-level totals."""
        sig = _page_signals(html, url, None)   # collect all, scope below
        sig["links"] = {u for u in sig["links"] if _in_scope(u)}
        contact_chunks.append(sig["contact_text"])
        # Keep only the parts of the raw HTML that carry contact markup, so
        # mailto:/tel: mining stays cheap instead of holding whole documents.
        raw_chunks.extend(re.findall(r'(?:mailto|tel):[^"\'>\s]+', html, re.I))
        # Cloudflare-obfuscated addresses appear only as a hex attribute.
        jl_emails.extend(_cfemails(html))
        jl_emails.extend(sig["jl_emails"])
        jl_phones.extend(sig["jl_phones"])
        for s in sig["socials"]:
            if s not in socials:
                socials.append(s)
        if not is_home:
            pages.append((_page_relevance(url, focus), url, sig["clean_text"]))
        return sig["links"]

    home_links = absorb(home_html, website, is_home=True)

    # Frontier: sitemap URLs, homepage links, and any maker subdomain roots.
    frontier = {}   # url -> depth
    for extra_origin in allowed[1:]:
        frontier[extra_origin + "/"] = 1
        for u in _sitemap_urls(extra_origin):
            frontier.setdefault(u, 1)
    for u in _sitemap_urls(origin):
        if _norm(u) not in visited:
            frontier[u] = 1
    for u in home_links:
        if _norm(u) not in visited:
            frontier.setdefault(u, 1)

    while frontier and budget > 0:
        # Take the most promising slice, fetch it in parallel, then let the
        # links it reveals compete for what is left of the budget.
        batch = sorted(frontier.items(),
                       key=lambda kv: (-_page_relevance(kv[0], focus),
                                       kv[1]))[:min(budget, 8)]
        for url, _d in batch:
            frontier.pop(url, None)
        batch = [(u, d) for u, d in batch if _norm(u) not in visited]
        if not batch:
            continue
        visited.update(_norm(u) for u, _ in batch)
        budget -= len(batch)

        # Fetch in parallel (plain HTTP only — never render off-thread).
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch, u): (u, d) for u, d in batch}
            for fut in concurrent.futures.as_completed(futures):
                url, depth = futures[fut]
                try:
                    html, text = fut.result()
                except Exception:
                    continue
                results.append((url, depth, html, text))

        # Then render the shells here, on the owning thread. Sub-pages that
        # returned nothing are left alone — most are dead links, and the
        # render budget is better spent on pages that did respond.
        if renderer is not None:
            for idx, (url, depth, html, text) in enumerate(results):
                if html and looks_like_shell(html, text):
                    rendered = renderer.html(url)
                    if rendered:
                        rtext = _clean_text(rendered)
                        if len(rtext) > len(text):
                            results[idx] = (url, depth, rendered, rtext)
                            rendered_count += 1

        for url, depth, html, text in results:
            if not text:
                continue
            links = absorb(html, url)
            # Step 2 of the sub-page scrape: a priority page (/labs, /research…)
            # is followed one hop past the normal cap, so the individual lab
            # pages it lists are reachable even on a shallow preset.
            if depth >= depth_cap + (1 if is_priority_page(url) else 0):
                continue
            for link in links:
                if _norm(link) not in visited and link not in frontier:
                    frontier[link] = depth + 1
            # Cap by EVICTING the least promising entries, not by refusing new
            # ones. Blocking additions at the cap means a staff page discovered
            # late loses to junk that happened to be queued first.
            if len(frontier) > MAX_FRONTIER:
                ranked = sorted(frontier.items(),
                                key=lambda kv: (-_page_relevance(kv[0], focus), kv[1]))
                frontier = dict(ranked[:MAX_FRONTIER])

    pages.sort(key=lambda p: p[0], reverse=True)

    # People pages outrank equipment pages 3:1, so on a big site staff bios
    # would fill the whole character budget and the equipment text — where
    # printer models live — got truncated away before extraction ever saw it.
    # Reserve a share of the budget for equipment-bearing pages.
    equip_kw = ({"equipment"}, {"tools"}, {"machines"}, {"shop"}, {"lab"},
                {"facilities"}, {"capabilities"}, {"make"})
    def _is_equip(url):
        toks = _path_tokens(url)
        return any(kw <= toks for kw in equip_kw)

    # Pages in the lead's own section are never demoted by this reshuffle.
    own_pages = [p for p in pages if focus and (focus & _path_tokens(p[1]))]
    rest = [p for p in pages if p not in own_pages]
    # Priority sub-pages lead the assembled text so they survive the size cap.
    prio_pages = [p for p in rest if is_priority_page(p[1])]
    rest = [p for p in rest if p not in prio_pages]
    pages = own_pages + prio_pages + rest

    equip_pages = [p for p in pages if _is_equip(p[1])]
    other_pages = [p for p in pages if not _is_equip(p[1])]
    ordered_texts = ([t for _, _, t in equip_pages[:6]]
                     + [t for _, _, t in other_pages]
                     + [t for _, _, t in equip_pages[6:]]
                     + [home_text])
    # Full text (untruncated) is what the deterministic model scan reads.
    full_text = " ".join(t for _, _, t in pages) + " " + home_text
    contact_text = " ".join(contact_chunks)
    contact_html = " ".join(raw_chunks)

    # Dedupe boilerplate repeated across pages, keeping relevance order.
    seen, lines, total = set(), [], 0
    for chunk in ordered_texts:
        for sent in chunk.split(". "):
            key = sent.strip().lower()
            if key and key not in seen:
                seen.add(key)
                lines.append(sent.strip())
                total += len(sent) + 2
        if total > MAX_SITE_CHARS:
            break
    site_text = ". ".join(lines)[:MAX_SITE_CHARS]

    return {
        "text": site_text,
        "email": _best_email(contact_html, contact_text, jl_emails, host=_host(origin)),
        "emails": _all_emails(contact_html, contact_text, jl_emails),
        "phone": _best_phone(contact_html, jl_phones, contact_text),
        "socials": socials[:6],
        "pages": len(pages) + 1,
        "rendered": rendered_count,
        # Scanned over the untruncated crawl, so models named deep in an
        # equipment wiki survive even when the text sent to Claude is cut.
        "printer_models": find_printer_models(full_text),
        "hosts": [_host(a) for a in allowed],
        "priority_pages": [u for _, u, _ in pages if is_priority_page(u)][:10],
    }


# ---------------------------------------------------------------------------
# Google Places
# ---------------------------------------------------------------------------

def places_search(query, max_results=20, spend=None):
    """Return a list of raw place dicts from Google Places (New) text search."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": config.key("GOOGLE_PLACES_API_KEY"),
        "X-Goog-FieldMask": PLACES_FIELDS,
    }
    places, token = [], None
    while len(places) < max_results:
        # pageSize and textQuery must stay identical across paginated calls,
        # otherwise Places (New) returns 400 INVALID_ARGUMENT.
        body = {"textQuery": query, "pageSize": 20}
        if token:
            body["pageToken"] = token
        resp = requests.post(PLACES_URL, headers=headers, json=body, timeout=30)
        if spend:
            spend.add_places()
        if not resp.ok:
            raise RuntimeError(f"Places API {resp.status_code}: {resp.text}")
        data = resp.json()
        places.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(2)  # required before using nextPageToken
    return places[:max_results]


def _place_key(p):
    """Stable identity for a place: its Places ID, else name+address."""
    return (p.get("id")
            or (p.get("displayName", {}).get("text", "").lower() + "|"
                + p.get("formattedAddress", "").lower()))


def places_search_all(target, location, max_results=20, max_queries=None,
                      progress=None, spend=None, skip=None):
    """Search Places across several phrasings of the same kind of business.

    Places matches the literal phrase, so one query only finds businesses that
    self-describe that way. Running the target plus related phrasings and
    deduping by place ID surfaces the fab labs, hackerspaces, and machine shops
    a single query silently misses.

    `skip(place)` drops places found in earlier runs. Applied here rather than
    afterwards so `max_results` counts only new places: later phrasings keep
    filling the quota instead of the run coming back short.
    """
    queries = [target]
    for q in config.PLACES_QUERY_VARIANTS:
        if q.lower() != target.lower() and q not in queries:
            queries.append(q)
    if max_queries:
        queries = queries[:max_queries]

    seen, out = set(), []
    for i, q in enumerate(queries):
        if len(out) >= max_results:
            break
        if progress:
            progress(i + 1, len(queries), f"Google Places: “{q}”")
        try:
            found = places_search(f"{q} in {location}", max_results=20,
                                  spend=spend)
        except RuntimeError:
            if not out:
                raise          # first query failing means the API is unusable
            continue           # a later variant failing shouldn't kill the run
        for p in found:
            key = _place_key(p)
            if key not in seen:
                seen.add(key)
                if skip and skip(p):
                    continue
                out.append(p)
    return out[:max_results]


CITY_TYPES = ("locality", "postal_town", "sublocality_level_1", "sublocality",
              "administrative_area_level_3", "administrative_area_level_2")
STATE_ZIP_RE = re.compile(r"^[A-Z]{2}\b")


def _city_from_place(place):
    comps = place.get("addressComponents", [])
    for want in CITY_TYPES:
        for comp in comps:
            if want in comp.get("types", []):
                return comp.get("longText", "")
    parts = [p.strip() for p in place.get("formattedAddress", "").split(",")]
    for i, part in enumerate(parts):
        if STATE_ZIP_RE.match(part) and i > 0:
            return parts[i - 1]
    return parts[-3] if len(parts) >= 3 else ""


def _place_type(p, target):
    disp = p.get("primaryTypeDisplayName", {}).get("text", "")
    if disp:
        return disp
    raw = p.get("primaryType", "")
    if raw and raw not in ("point_of_interest", "establishment"):
        return raw.replace("_", " ").title()
    return target.title()


# ---------------------------------------------------------------------------
# Claude-backed discovery and website lookup
# ---------------------------------------------------------------------------

URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')\]]+")

DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "places": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "website": {"type": "string"},
                    "city": {"type": "string"},
                    "address": {"type": "string"},
                    "kind": {"type": "string"},
                },
                "required": ["name", "website", "city", "address", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["places"],
    "additionalProperties": False,
}


def discover_via_search(client, location, target, known_names=(), limit=20,
                        spend=None):
    """Find prospects that Google Places does not list.

    Places only knows businesses with a Maps listing, which misses university
    and library makerspaces, member co-ops, and shops that exist only in maker
    directories. This searches the web for them as a second source.
    """
    known = ", ".join(sorted(known_names)[:40]) or "(none yet)"
    prompt = (
        f"Search the web for {target}s and similar workshops in {location}.\n\n"
        "Include ones that often have no Google Maps listing: university and "
        "college makerspaces or fab labs, public library maker programs, "
        "community/member workshops and co-ops, hackerspaces, tool libraries, "
        "and shops listed in maker directories such as hackerspaces.org, "
        "fablabs.io, or Nation of Makers.\n\n"
        f"Already found, do not repeat: {known}\n\n"
        f"Return up to {limit} DISTINCT real organizations that actually exist "
        "and are in or near that location. For each, give the official website "
        "URL if you can find one (empty string if not — never guess a URL), the "
        "city, a street address if known (empty string otherwise), and a short "
        "'kind' label such as 'University makerspace' or 'Hackerspace'. Do not "
        "invent organizations."
    )
    try:
        msg = client.messages.create(
            model=config.SEARCH_MODEL,
            max_tokens=6000,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": DISCOVERY_SCHEMA},
            },
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return []
    if spend:
        spend.add_call("discovery search", config.SEARCH_MODEL, msg, searches=5)
    if msg.stop_reason not in ("end_turn", "stop_sequence"):
        return []
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for p in data.get("places", []):
        if not isinstance(p, dict) or not p.get("name", "").strip():
            continue
        site = (p.get("website") or "").strip()
        if site and _is_generic_host(site):
            site = ""
        out.append({
            "name": p["name"].strip(),
            "website": _origin(site) if site else "",
            "city": (p.get("city") or "").strip(),
            "address": (p.get("address") or "").strip(),
            "kind": (p.get("kind") or "").strip(),
        })
    return out[:limit]


BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_brave_gate = threading.Lock()
_brave_last = [0.0]


def brave_search(query, count=5, spend=None):
    """Brave Search. Returns [(title, url, snippet)]; [] when unavailable.

    Throttled to the free tier's 1 query/second — exceeding it returns 429s
    that would look like "nothing found" rather than "slow down".
    """
    if not config.key("BRAVE_API_KEY"):
        return []
    with _brave_gate:
        wait = config.BRAVE_MIN_INTERVAL - (time.monotonic() - _brave_last[0])
        if wait > 0:
            time.sleep(wait)
        _brave_last[0] = time.monotonic()
    try:
        r = requests.get(
            BRAVE_URL,
            headers={"X-Subscription-Token": config.key("BRAVE_API_KEY"),
                     "Accept": "application/json"},
            params={"q": query, "count": count, "country": "us"},
            timeout=15)
    except requests.RequestException:
        return []
    if spend:
        spend.add_brave()
    if not r.ok:
        return []
    try:
        results = r.json().get("web", {}).get("results", []) or []
    except ValueError:
        return []
    return [(w.get("title", ""), w.get("url", ""), w.get("description", ""))
            for w in results if w.get("url")]


def _mentions_org(text, name, org_host=""):
    """Is this page actually about the organization we're researching?

    A search result can easily be a directory of many businesses or a similarly
    named company; harvesting an address off one of those is how a lead ends up
    with somebody else's phone number.
    """
    low = (text or "").lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower())
            if len(t) >= 4 and t not in FOCUS_STOPWORDS]
    if org_host and _registrable(org_host).split(".")[0] in low:
        return True
    if not toks:
        return False
    hits = sum(1 for t in toks if t in low)
    return hits >= max(1, min(2, len(toks)))


def _phone_near_mention(text, name, window=400):
    """A phone number that sits near a mention of the organization.

    On a company's own site any published number is theirs, so _best_phone can
    take the best one anywhere on the page. On somebody else's page — a
    directory, an arts council, a news article — the number in the header or
    footer belongs to the page's owner, not to the organization being written
    about. Requiring the number to appear near the org's name is what keeps a
    listing site's switchboard out of the lead row.
    """
    low = (text or "").lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower())
            if len(t) >= 4 and t not in FOCUS_STOPWORDS]
    if not toks:
        return ""
    spots = [m.start() for t in toks for m in re.finditer(re.escape(t), low)]
    if not spots:
        return ""
    for m in PHONE_TEXT_RE.finditer(text or ""):
        if any(abs(m.start() - s) <= window for s in spots):
            return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    return ""


def enrich_gaps_free(name, city, website, want, spend=None):
    """Fill gaps with Brave results read by our own crawler and extractors.

    This replaces the LLM web-search call for the common cases. Everything we
    need from a third-party page — an address, a phone number, printer models —
    already has a deterministic extractor, so no model has to read the page.
    """
    if not config.key("BRAVE_API_KEY"):
        return {}
    org_host = _host(website)
    queries = []
    if "email" in want or "phone" in want:
        queries.append(f"{name} {city} contact email phone".strip())
    if "printer_models" in want:
        queries.append(f"{name} 3D printers equipment list".strip())

    found, sources, seen = {}, [], set()
    for q in queries:
        for _title, url, _snip in brave_search(q, count=5, spend=spend):
            if len(seen) >= config.BRAVE_MAX_PAGES:
                break
            if _is_generic_host(url) or _norm(url) in seen:
                continue
            seen.add(_norm(url))
            html, text = _fetch(url)
            if not text or not _mentions_org(text, name, org_host):
                continue
            ctext = _contact_text(html)
            jl_e, jl_p, _ = _extract_jsonld(html)

            if "email" in want and "email" not in found:
                cand = _best_email(html, ctext, jl_e, host=org_host)
                # On someone else's page, only an address on the company's own
                # domain can be trusted to be the company's.
                if cand and org_host and not _is_foreign_domain(cand, org_host):
                    found["email"] = cand
                elif cand and not org_host and _mentions_org(cand, name):
                    found["email"] = cand
            if "phone" in want and "phone" not in found:
                own_page = bool(org_host) and _same_site(url, website)
                # Trust any number on the company's own site; on a third
                # party's page only one printed beside the company's name.
                cand = (_best_phone(html, jl_p, ctext) if own_page
                        else _phone_near_mention(ctext, name))
                if cand:
                    found["phone"] = cand
            if "printer_models" in want and "printer_models" not in found:
                models = find_printer_models(text)
                if models:
                    found["printer_models"] = "; ".join(models)
            if url not in sources:
                sources.append(url)
            if all(f in found for f in want):
                break
        if all(f in found for f in want):
            break
    if found:
        found["source"] = "; ".join(sources[:3])[:200]
    return found


ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "printer_models": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["email", "phone", "printer_models", "source"],
    "additionalProperties": False,
}


def enrich_gaps(client, name, city, website, want, spend=None):
    """Targeted web search for fields the site itself did not publish.

    Crawling can only find what an organization puts on its own pages. Phone
    numbers, public inboxes, and printer inventories frequently live elsewhere:
    directory listings, library guides, news write-ups, grant announcements,
    social media profiles. This asks for ONLY the missing fields, one search
    per lead, and every value is validated before use.
    """
    if not want:
        return {}
    asks = {
        "email": "a public contact email address for the organization",
        "phone": "a public phone number for the organization",
        "printer_models": ("the specific 3D printer makes/models they own "
                           "(e.g. 'Prusa MK4', 'Bambu Lab X1C')"),
    }
    wanted = "\n".join(f"- {asks[w]}" for w in want if w in asks)
    prompt = (
        f"Search the web for this organization and find ONLY the following:\n"
        f"{wanted}\n\n"
        f"Organization: {name}\nCity: {city}\nWebsite: {website or 'unknown'}\n\n"
        "Rules: report a value only if you actually saw it on a credible page "
        "about THIS organization (its own site, a library guide, a directory "
        "listing, a news article). Return an empty string for anything you did "
        "not find or are unsure about — a blank is far better than a wrong "
        "number. Never guess or construct an address from a pattern. In "
        "'source', name the page or site each value came from."
    )
    try:
        msg = client.messages.create(
            model=config.SEARCH_MODEL,
            max_tokens=4000,
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": ENRICH_SCHEMA}},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return {}
    if spend:
        spend.add_call("gap search", config.SEARCH_MODEL, msg, searches=2)
    if msg.stop_reason not in ("end_turn", "stop_sequence"):
        return {}
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return {}

    out = {}
    email = (data.get("email") or "").strip().lower()
    if "email" in want and EMAIL_RE.fullmatch(email) \
            and not any(b in email for b in EMAIL_BLOCKLIST):
        out["email"] = email
    phone = (data.get("phone") or "").strip()
    if "phone" in want and sum(c.isdigit() for c in phone) >= 10:
        out["phone"] = phone
    if "printer_models" in want:
        # Re-run the gazetteer on the answer so a prose reply can't smuggle
        # in something that isn't a recognizable printer model.
        models = find_printer_models(data.get("printer_models") or "")
        if models:
            out["printer_models"] = "; ".join(models)
    if out:
        out["source"] = (data.get("source") or "web search").strip()[:200]
    return out


def find_website(client, name, city, address, spend=None):
    """Use Claude web search to find one place's official website."""
    prompt = (
        f"Find the official website for this business:\n"
        f"Name: {name}\nCity: {city}\nAddress: {address}\n\n"
        "Search the web, then reply with ONLY the homepage URL (e.g. "
        "https://example.org). If it has no official website of its own, or you "
        "are not confident, reply with exactly NONE. Do not reply with a "
        "Facebook, Instagram, Yelp, or directory-listing URL."
    )
    try:
        msg = client.messages.create(
            model=config.SEARCH_MODEL,
            max_tokens=1500,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return ""
    if spend:
        spend.add_call("find website", config.SEARCH_MODEL, msg, searches=2)
    if msg.stop_reason == "refusal":
        return ""
    text = " ".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    if "NONE" in text.upper() and not URL_IN_TEXT_RE.search(text):
        return ""
    for url in URL_IN_TEXT_RE.findall(text):
        url = url.rstrip(".,;:)\"'")
        if not _is_generic_host(url) and _host(url):
            return _origin(url)
    return ""


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

PRINT3D_RE = re.compile(
    r"3[\s-]*d[\s-]*print|additive\s+manufactur|filament|resin\s+print"
    r"|\bfdm\b|\bsla\b|\bsls\b|prusa|bambu\s*lab|creality|ender\s*\d"
    r"|ultimaker|formlabs|elegoo|anycubic|raise3d|lulzbot|markforged",
    re.I,
)

# Deterministic printer gazetteer. Claude only sees the truncated site text, so
# a model named on page 30 of a big site can be cut before extraction ever runs;
# this scans the FULL crawled text instead. Every alternative is \b-anchored --
# an unanchored "anet" matches inside "planetary", which is exactly the kind of
# false positive that puts a fictional printer in a sales sheet.
PRINTER_MODEL_RE = re.compile(
    r"\b("
    r"prusa(?:\s+(?:mk[234]s?|xl|mini\+?|core\s*one|i3))?"
    r"|bambu(?:\s*lab)?(?:\s+(?:x1[ce]?(?:\s*carbon)?|p1[sp]|a1(?:\s*mini)?|h2d))?"
    r"|creality(?:\s+(?:ender\s*\d(?:\s*(?:pro|neo|v\d|s1|max|plus))?"
    r"|cr-?\d+(?:\s*\S+)?|k1(?:\s*max)?))?"
    # Bounded suffixes only: an open \S* swallows wiki heading numbers and
    # turns "Ender 3" in a section headed 3.3 into a phantom "Ender 3.3".
    r"|ender\s*\d(?:\s*(?:pro|neo|v\d|s1|max|plus))?|cr-?10(?:\s*s?\d?)?"
    r"|ultimaker(?:\s+(?:s[357]|2\+?|3))?"
    r"|formlabs(?:\s+form\s*\d\+?)?|form\s*[234]\+?\b"
    r"|elegoo(?:\s+(?:mars\s*\d?\S*|saturn\s*\d?\S*|neptune\s*\d?\S*))?"
    r"|anycubic(?:\s+(?:photon\S*|kobra\S*|vyper))?"
    r"|raise\s*3d(?:\s+\S+)?|lulzbot(?:\s+taz\s*\d*)?|taz\s*\d"
    r"|markforged(?:\s+\S+)?|makerbot(?:\s+(?:replicator\S*|method\S*|sketch))?"
    r"|flashforge(?:\s+\S+)?|snapmaker(?:\s+\S+)?|voron(?:\s*[\d.]+)?"
    r"|stratasys(?:\s+\S+)?|objet\s*\d+|fortus\s*\d+|uprint\s*\S*"
    r"|qidi(?:\s+\S+)?|sovol(?:\s+\S+)?|zortrax(?:\s+\S+)?|craftbot"
    r"|dremel\s+digilab\S*|monoprice(?:\s+\S+)?|artillery(?:\s+\S+)?"
    r"|tronxy|anet\s+a\d|photon\s+mono\S*|glowforge"
    r")\b",
    re.I,
)
# Bare brand names carry no model, so they're only worth reporting when the
# text gives nothing more specific.
_BARE_BRANDS = {"prusa", "bambu", "bambu lab", "creality", "ultimaker",
                "formlabs", "elegoo", "anycubic", "markforged", "makerbot",
                "flashforge", "snapmaker", "stratasys", "qidi", "sovol",
                "zortrax", "monoprice", "artillery", "raise3d", "raise 3d",
                "lulzbot", "voron", "glowforge", "craftbot", "tronxy"}


# Slicers, firmware, and post-processing gear share brand names with printers
# but are not printers — listing them as owned hardware would misinform outreach.
NOT_A_PRINTER_RE = re.compile(
    r"slicer|studio\b|cura|firmware|klipper|octoprint|marlin|software|"
    r"\bwash\b|\bcure\b|curing|filament\s+dryer|\bspool", re.I)


def find_printer_models(text, limit=12):
    """Printer models named anywhere in the crawled text, most specific first.

    Runs over the full crawl rather than the truncated slice sent to Claude,
    so a model listed deep in an equipment wiki still surfaces.
    """
    found = []
    for m in PRINTER_MODEL_RE.finditer(text or ""):
        raw = re.sub(r"\s+", " ", m.group(0).strip())
        # "PrusaSlicer" and "Bambu Studio" are software; a wash/cure station is
        # not a printer. Check the match and the words right after it.
        if NOT_A_PRINTER_RE.search(text[m.start():m.end() + 12]):
            continue
        if not any(raw.lower() == f.lower() for f in found):
            found.append(raw)

    # Drop any entry wholly contained in another: "Ender 3" is redundant beside
    # "Creality Ender 3", and a bare "Prusa" beside "Prusa MK4". Keying by brand
    # instead would wrongly keep only one model per brand — a shop with both a
    # Prusa MK3 and a Prusa XL owns two printers, and both matter for outreach.
    keep = [a for a in found
            if not any(a.lower() != b.lower() and a.lower() in b.lower()
                       for b in found)]
    # Most specific first.
    keep.sort(key=lambda s: (s.lower() in _BARE_BRANDS, -len(s)))
    return keep[:limit]


def derive_priority(lead):
    """HIGH / MEDIUM / LOW from real collected signals.

    Score: printer models (+2) or other 3D evidence (+1), reachable (+1),
    a named person (+1), >=20 reviews (+1)  ->  HIGH(>=3) MEDIUM(>=1) LOW(0)
    """
    equip = lead.get("equipment_highlights") or ""
    printers = (lead.get("printer_models") or "").strip()
    reachable = bool(lead.get("email") or lead.get("phone_web") or lead.get("phone_maps"))

    score = 2 if printers else (1 if PRINT3D_RE.search(equip) else 0)
    score += 1 if reachable else 0
    score += 1 if lead.get("contacts") else 0
    score += 1 if (lead.get("reviews") or 0) >= 20 else 0
    return "HIGH" if score >= 3 else "MEDIUM" if score >= 1 else "LOW"


# ---------------------------------------------------------------------------
# Claude extraction (structured outputs)
# ---------------------------------------------------------------------------

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "linkedin": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "role", "email", "phone", "linkedin", "reason"],
                "additionalProperties": False,
            },
        },
        "equipment_highlights": {"type": "string"},
        "printer_models": {"type": "string"},
        "pitch": {"type": "string"},
    },
    "required": ["people", "equipment_highlights", "printer_models", "pitch"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = (
    "You extract facts from a company's own website text and write a short "
    "assessment for a 3D-printing filament seller deciding whether to reach out.\n"
    "STRICT RULES:\n"
    "- people, equipment_highlights, printer_models: use ONLY information explicitly "
    "present in WEBSITE TEXT. If absent, use an empty string or empty list. Never "
    "infer, guess, or use outside knowledge. Do not list the seller's own staff.\n"
    "- people: named individuals who hold a STATED position at THIS organization — "
    "staff, leadership, board officers, managers, instructors. Order them by who "
    "would influence a materials/equipment purchase (directors, shop and facility "
    "managers, technical or operations leads, owners, founders, purchasing staff) "
    "first. 'name' is required; role/email/phone/linkedin only if shown on the "
    "site, else an empty string.\n"
    "- Do NOT list someone whose only appearance is incidental — a member named as "
    "owning a tool, quoted in a testimonial, tagged in a photo or event, or "
    "mentioned in a forum or class post. If the page does not give the person a "
    "position at the organization, leave them out. Returning nobody is correct "
    "when the site names no staff.\n"
    "- The WEBSITE TEXT may span a shared or institutional site covering many "
    "groups (a whole university, a library system, a venue used by outside "
    "organizations). List ONLY people employed by the PROSPECT named below. "
    "Exclude anyone who works for a different employer — a hotel's catering "
    "manager on an events page, a partner firm's accountant, staff of a sibling "
    "department unrelated to the workshop. An email address on another "
    "company's domain is a strong sign the person is not theirs.\n"
    "- reason: ONE short clause on why this person is worth contacting about "
    "filament, based ONLY on their stated role — e.g. 'runs the shop floor and its "
    "equipment' or 'signs off on facility purchases'. If the role is unclear or "
    "unrelated to purchasing or operations, use an empty string. Do not speculate "
    "about authority the site does not state.\n"
    "- printer_models: specific 3D printer models named on the site (e.g. 'Prusa MK4').\n"
    "- pitch: a terse internal note for the seller to scan while scoring leads — "
    "NOT sales copy, NOT addressed to the prospect, no greeting/sign-off. Written "
    "as short telegraphic clauses separated by '; ', dropping filler words "
    "('a', 'the', 'is') the way a busy analyst jots a note, e.g. "
    "'350+ members burning through filament monthly; recycled PETG aligns with "
    "maker ethos' or '7 industrial Raise3D Pro2s that eat PETG; additive mfg "
    "research program = recurring high-volume buyer'. Aim for 1-2 clauses, "
    "under ~160 characters.\n"
    "  Pull the buying-signal facts from wherever the site states them — About/"
    "Mission/Our Story pages, membership or facility pages, news/announcements — "
    "using ONLY what the text says, never invented or estimated: concrete "
    "numbers (member/student count, printer count, sq ft of space, budget), "
    "specific printer models and whether they already run PETG or a similar "
    "material, who holds purchasing/procurement authority, stated mission or "
    "values language that signals interest in recycled/sustainable material, "
    "and any growth signal (a stated expansion, move, new program, or launch "
    "date) that means rising future demand.\n"
    "  Then connect the strongest 1-2 facts to why they matter for a recycled-"
    "PETG filament seller, in the same terse style (e.g. 'university procurement "
    "budget', 'natural switch to recycled PETG at competitive price', 'get in "
    "early'). If the site gives too little to say anything concrete, keep it "
    "honest and say so briefly rather than padding or inventing a fit."
)


def _empty_extraction():
    return {"people": [], "equipment_highlights": "", "printer_models": "", "pitch": ""}


def extract_and_pitch(client, name, place_type, city, site_text, website="",
                      spend=None):
    """One Claude call: extract key people + equipment + a summary/fit assessment.

    The prospect's own domain is included so the model can tell its staff from
    outsiders who appear on the same pages.
    """
    org_host = _host(website)
    user = (
        f"SELLER:\n{config.COMPANY_BLURB}\n\n"
        f"PROSPECT: {name} | category: {place_type or 'unknown'} | city: {city or 'unknown'}\n"
        f"PROSPECT DOMAIN: {org_host or 'unknown'}\n"
        "Only people employed by this prospect belong in `people`.\n\n"
        f"WEBSITE TEXT (may be empty):\n{site_text or '(no website text available)'}"
    )
    msg = client.messages.create(
        model=config.EXTRACT_MODEL,
        max_tokens=4000,
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        system=[{"type": "text", "text": EXTRACT_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if spend:
        spend.add_call("extract + pitch", config.EXTRACT_MODEL, msg)
    if msg.stop_reason not in ("end_turn", "stop_sequence"):
        return _empty_extraction()
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return _empty_extraction()
    people = []
    for p in data.get("people", []):
        if not isinstance(p, dict) or not p.get("name", "").strip():
            continue
        # Sites that obfuscate emails render the literal text
        # "[email protected]"; drop anything that isn't a real address rather
        # than publishing a placeholder as a contact.
        addr = (p.get("email") or "").strip().lower()
        if not EMAIL_RE.fullmatch(addr):
            p["email"] = ""
        elif _is_foreign_domain(addr, org_host):
            # Belongs to another employer — keep the person (the prompt is the
            # judgement call) but never present someone else's work address as
            # this prospect's contact.
            p["email"] = ""
        else:
            p["email"] = addr
        people.append(p)
    return {
        "people": people,
        "equipment_highlights": data.get("equipment_highlights", "").strip(),
        "printer_models": data.get("printer_models", "").strip(),
        "pitch": data.get("pitch", "").strip(),
    }


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _norm_name(name):
    """Lowercased alphanumeric words, minus filler, for name-based dedupe."""
    toks = set(re.split(r"[^a-z0-9]+", (name or "").lower())) - {""}
    return toks - {"the", "a", "an", "of", "and", "inc", "llc", "co", "ltd"}


def _names_match(a, b):
    """Same organization by name, tolerating extra words from another source.

    "UT Fab Lab" and "UT College of Architecture + Design Fab Lab" are one
    org. A one-word name must match exactly, though: "Makerspace" is contained
    in every makerspace's name and would otherwise swallow them all.
    """
    ta, tb = _norm_name(a), _norm_name(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 2 and small <= big:
        return True
    overlap = len(ta & tb)
    return overlap >= 2 and overlap / len(small) >= 0.75


def _page_key(url):
    """'https://www.x.org/Lab/index.html?utm=gmb' -> 'x.org/lab'.

    Google Places often appends tracking queries (?utm_source=gmb) to one
    listing's website and not another's; keying on the raw path+query kept
    those as two rows.
    """
    if not url or _is_generic_host(url):
        return ""
    host = _host(url)
    if not host:
        return ""
    path = urlparse(url if "://" in url else "https://" + url).path.lower()
    path = re.sub(r"/(index|default|home)\.(html?|php|aspx?)$", "", path)
    return host + path.rstrip("/")


STREET_ABBR = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr",
    "boulevard": "blvd", "lane": "ln", "highway": "hwy", "parkway": "pkwy",
    "place": "pl", "court": "ct", "circle": "cir", "suite": "", "ste": "",
    "unit": "", "north": "n", "south": "s", "east": "e", "west": "w",
}


def _norm_addr(addr):
    """Street line only, abbreviated, minus suite/unit: '' if no house number."""
    street = (addr or "").split(",")[0].lower()
    street = re.sub(r"(#|\b(suite|ste|unit|apt|room|rm)\b\.?)\s*\w+", "", street)
    toks = [STREET_ABBR.get(t, t) for t in re.split(r"[^a-z0-9]+", street) if t]
    toks = [t for t in toks if t]
    return " ".join(toks) if toks and toks[0][:1].isdigit() else ""


def _norm_phone(phone):
    digits = re.sub(r"\D", "", phone or "")[-10:]
    return digits if len(digits) == 10 else ""


def _same_prospect(a, b):
    """Are two prospects the same organization / location?

    Distinct locations of a chain share a name, so a name match only counts
    when it cannot be two branches: one side has no website, and their street
    addresses don't disagree. A shared address or phone alone is not enough
    either — a campus building houses several labs on one switchboard — so
    those need a name match too.
    """
    if a.get("place_id") and a.get("place_id") == b.get("place_id"):
        return True
    pa, pb = _page_key(a.get("website", "")), _page_key(b.get("website", ""))
    if pa and pa == pb:
        return True
    if not _names_match(a.get("name", ""), b.get("name", "")):
        return False
    aa, ab = _norm_addr(a.get("address")), _norm_addr(b.get("address"))
    if aa and aa == ab:
        return True
    fa, fb = _norm_phone(a.get("phone_maps")), _norm_phone(b.get("phone_maps"))
    if fa and fa == fb:
        return True
    if aa and ab:
        return False                     # same name, two street addresses
    return not (pa and pb)               # two different own sites = two orgs


def _group(items, same):
    """Partition items into groups where `same` links any two (union-find)."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if find(i) != find(j) and same(items[i], items[j]):
                parent[find(j)] = find(i)
    groups = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)
    return list(groups.values())


def merge_prospects(prospects):
    """Merge prospects that are the same organization.

    Signals (see _same_prospect): the same web page (host + path, ignoring
    tracking queries), or a matching name backed by the same street address,
    the same Maps phone, or the absence of a conflicting website/address.

    The most-reviewed place becomes the row; the others are recorded as related
    locations. Website-less places with different names, and places sharing only
    a social page, are never merged.
    """
    if not config.MERGE_SAME_WEBSITE:
        return prospects
    merged = []
    for members in _group(prospects, _same_prospect):
        if len(members) == 1:
            merged.append(members[0])
            continue
        members.sort(key=lambda m: -(m.get("reviews") or 0))
        primary, extras = members[0], members[1:]
        primary["related"] = [e["name"] for e in extras if e["name"] != primary["name"]]
        primary["related_addresses"] = [e["address"] for e in extras if e.get("address")]
        for field in ("phone_maps", "website", "address", "city", "hours"):
            if not primary.get(field):
                primary[field] = next((e[field] for e in extras if e.get(field)), "")
        if not primary.get("website_source"):
            primary["website_source"] = next(
                (e.get("website_source") for e in extras if e.get("website_source")), "")
        sources = []
        for m in members:
            for s in (m.get("discovered_via") or "").split(" + "):
                if s and s not in sources:
                    sources.append(s)
        primary["discovered_via"] = " + ".join(sources)
        primary["reviews"] = max((m.get("reviews") or 0) for m in members)
        merged.append(primary)
    order = {id(p): i for i, p in enumerate(prospects)}
    merged.sort(key=lambda p: order.get(id(p), 0))
    return merged


def _same_lead(a, b):
    """_same_prospect, plus the evidence a crawl adds: email and site phone."""
    email = (a.get("email") or "").lower()
    # An address on some third party's domain (a crawl picking up a
    # partner's contact) is contamination, not proof of one org — unless
    # the names agree too, as with an alias domain (knoxmakers.com).
    if email and email == (b.get("email") or "").lower() and (
            _names_match(a.get("name", ""), b.get("name", ""))
            or not (_is_foreign_domain(email, _host(a.get("website", "")))
                    or _is_foreign_domain(email, _host(b.get("website", ""))))):
        return True
    pa, pb = _norm_phone(a.get("phone_web")), _norm_phone(b.get("phone_web"))
    if pa and pa == pb and _names_match(a.get("name", ""), b.get("name", "")):
        return True
    return _same_prospect(a, b)


def dedupe_leads(leads):
    """Final pass after crawling: drop leads that turned out to be one org.

    Before crawling, two listings can look unrelated — different domains that
    redirect to one site, or a Maps listing and a web-search find with no
    shared field. The crawl reveals what they publish; the same primary email,
    or a matching name with the same published phone, means one contact and
    would otherwise be pitched twice.
    """
    out = []
    for members in _group(leads, _same_lead):
        # Keep the lead with the most to work with; the rest become related.
        members.sort(key=lambda L: (-len(L.get("contacts") or []),
                                    -(L.get("pages_crawled") or 0),
                                    -(L.get("reviews") or 0)))
        primary = members[0]
        for extra in members[1:]:
            if extra["name"] != primary["name"] and extra["name"] not in primary.setdefault("related", []):
                primary["related"].append(extra["name"])
            if extra.get("address") and extra["address"] != primary.get("address") \
                    and extra["address"] not in primary.setdefault("related_addresses", []):
                primary["related_addresses"].append(extra["address"])
        out.append(primary)
    order = {id(L): i for i, L in enumerate(leads)}
    out.sort(key=lambda L: order.get(id(L), 0))
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

# Crawl presets: deeper fills more boxes but costs proportionally more time.
# "queries" caps how many Places phrasings are tried. One query finds only
# businesses that self-describe with that exact phrase, so breadth here is what
# decides how many places exist to deep-crawl at all.
DEPTH_PRESETS = {
    "quick":    {"pages": 10, "depth": 1, "render": False,
                 "discover": False, "enrich": False, "queries": 1},
    "standard": {"pages": 25, "depth": 2, "render": True,
                 "discover": True,  "enrich": False, "queries": 8},
    "deep":     {"pages": 40, "depth": 3, "render": True,
                 "discover": True,  "enrich": True,  "queries": None},
}


def collect(location, target, max_results=20, progress=None, depth="deep",
            exclude=()):
    """Return a list of normalized lead dicts for the given location + target.

    `depth` selects a DEPTH_PRESETS entry: how many pages per site to crawl,
    how many link hops to follow, whether to render JS sites, and whether to
    run the second (web-search) discovery source.

    `exclude` holds places from earlier runs (history.py records). They are
    skipped at discovery, before any crawl or AI call is paid for, and again
    after crawling, when a shared email can reveal one under a new name.
    """
    preset = DEPTH_PRESETS.get(depth, DEPTH_PRESETS["deep"])
    crawl_pages, crawl_depth = preset["pages"], preset["depth"]
    use_render = preset["render"] and config.ENABLE_JS_RENDER
    use_discovery = preset["discover"] and config.ENABLE_SEARCH_DISCOVERY
    use_enrich = preset["enrich"] and config.ENABLE_GAP_SEARCH

    if config.MOCK_MODE:
        leads = _mock_leads(location)
        for L in leads:
            L["priority"] = derive_priority(L)
        return leads

    import anthropic
    client = anthropic.Anthropic(api_key=config.key("ANTHROPIC_API_KEY"))
    today = datetime.date.today().strftime("%b %d, %Y")
    spend = Spend()

    def say(done, total, msg):
        if progress:
            progress(done, total, msg)

    # --- Source 1: Google Places, across several phrasings -----------------
    exclude = list(exclude)
    seen_before = [0]

    def _seen(candidate):
        if any(_same_prospect(candidate, old) for old in exclude):
            seen_before[0] += 1
            return True
        return False

    def _seen_place(p):
        return _seen({"name": p.get("displayName", {}).get("text", ""),
                      "place_id": p.get("id", ""),
                      "website": p.get("websiteUri", ""),
                      "address": p.get("formattedAddress", ""),
                      "phone_maps": p.get("nationalPhoneNumber", "")})

    raw = places_search_all(target, location, max_results=max_results,
                            max_queries=preset["queries"], progress=say,
                            spend=spend, skip=_seen_place if exclude else None)
    prospects = []
    for p in raw:
        prospects.append({
            "name": p.get("displayName", {}).get("text", ""),
            "place_id": p.get("id", ""),
            "related": [], "related_addresses": [],
            "city": _city_from_place(p),
            "type": _place_type(p, target),
            "address": p.get("formattedAddress", ""),
            "phone_maps": (p.get("nationalPhoneNumber")
                           or p.get("internationalPhoneNumber", "")),
            "website": "" if _is_generic_host(p.get("websiteUri", ""))
                       else p.get("websiteUri", ""),
            "website_source": "" if _is_generic_host(p.get("websiteUri", ""))
                              or not p.get("websiteUri") else "Google Places",
            "discovered_via": "Google Places",
            "hours": "; ".join(p.get("regularOpeningHours", {})
                               .get("weekdayDescriptions", [])),
            "rating": p.get("rating"),
            "reviews": p.get("userRatingCount") or 0,
            "status": p.get("businessStatus", ""),
        })

    # --- Source 2: web search, for places with no Maps listing --------------
    if use_discovery:
        say(0, 0, "Searching the web for places not on Maps…")
        try:
            extra = discover_via_search(
                client, location, target,
                known_names=[p["name"] for p in prospects]
                            + [e["name"] for e in exclude],
                limit=max(8, max_results // 2), spend=spend)
        except Exception as e:
            extra = []
            # Don't fail the run, but don't hide it either: a silent empty
            # result looks identical to "there was nothing to find".
            say(0, 0, f"Web discovery unavailable ({type(e).__name__}) — "
                      f"continuing with Google Places only")
            time.sleep(1.5)
        if not extra:
            say(0, 0, "Web discovery added no new places")
        known_hosts = {_host(p["website"]) for p in prospects if p["website"]}
        known_names = [_norm_name(p["name"]) for p in prospects]

        def _same_org(name):
            """Names from two sources rarely match exactly.

            "UT Fab Lab" and "UT College of Architecture + Design Fab Lab" are
            one organization; requiring identical token sets let both through
            as separate leads, each paying a full crawl.
            """
            toks = _norm_name(name)
            if not toks:
                return False
            for known in known_names:
                if not known:
                    continue
                overlap = len(toks & known)
                # One name containing the other, or a strong majority shared.
                if overlap and (toks <= known or known <= toks):
                    return True
                if overlap >= 2 and overlap / min(len(toks), len(known)) >= 0.75:
                    return True
            return False

        for e in extra:
            host = _host(e["website"])
            if (host and host in known_hosts) or _same_org(e["name"]):
                continue
            if exclude and _seen({"name": e["name"], "website": e["website"],
                                  "address": e["address"]}):
                continue
            known_names.append(_norm_name(e["name"]))
            if host:
                known_hosts.add(host)
            prospects.append({
                "name": e["name"], "related": [], "related_addresses": [],
                "city": e["city"] or location, "type": e["kind"] or target.title(),
                "address": e["address"], "phone_maps": "",
                "website": e["website"],
                "website_source": "Web search" if e["website"] else "",
                "discovered_via": "Web search",
                "hours": "", "rating": None, "reviews": 0, "status": "",
            })

    # --- Resolve any still-missing websites ---------------------------------
    # Gated by the preset like the other searches: Quick advertises "Google
    # Maps only", so it must not quietly bill a web search per website-less
    # place.
    missing = ([p for p in prospects if not p["website"]]
               if use_discovery and config.ENABLE_WEBSITE_SEARCH else [])
    for i, pr in enumerate(missing):
        say(i + 1, len(missing), f"Finding website: {pr['name']}")
        pr["website"] = find_website(client, pr["name"], pr["city"],
                                     pr["address"], spend=spend)
        if pr["website"]:
            pr["website_source"] = "Web search"

    # --- Merge duplicates before spending crawl/AI calls --------------------
    prospects = merge_prospects(prospects)

    # --- Crawl + extract ----------------------------------------------------
    renderer = Renderer(enabled=use_render)
    leads = []
    processed = []
    early_dropped = 0
    try:
        for i, pr in enumerate(prospects):
            say(i + 1, len(prospects), pr["name"])
            renderer.budget = config.JS_RENDER_MAX_PAGES  # per-site render budget

            try:
                site = crawl_site(pr["website"], renderer=renderer,
                                  max_pages=crawl_pages, max_depth=crawl_depth,
                                  focus=focus_tokens(pr["name"], pr["website"]))
            except Exception:
                site = {"text": "", "email": "", "emails": [], "phone": "",
                        "socials": [], "pages": 0, "rendered": 0,
                        "printer_models": [], "hosts": []}
            processed.append(dict(pr, email=site["email"]))
            processed.extend({"name": rel, "website": pr["website"]}
                             for rel in pr.get("related") or [])

            # Early exit: a readable site with no priority sub-page and no trace
            # of 3D printing anywhere in the crawl won't pass the later filter
            # either, so skip the extraction and gap-search spend.
            if (config.DROP_EMPTY_LEADS and config.SKIP_UNPROMISING_SITES
                    and site["text"] and not site.get("priority_pages")
                    and not site.get("printer_models")
                    and not PRINT3D_RE.search(site["text"])):
                early_dropped += 1
                continue

            extract_err = ""
            try:
                ex = extract_and_pitch(client, pr["name"], pr["type"],
                                       pr["city"], site["text"], pr["website"],
                                       spend=spend)
            except anthropic.APIStatusError as e:
                ex, extract_err = _empty_extraction(), f"AI extraction failed ({e.status_code})"
            except Exception as e:
                ex, extract_err = _empty_extraction(), f"AI extraction failed ({type(e).__name__})"

            email = site["email"] or next(
                (p.get("email") for p in ex["people"] if p.get("email")), "")
            linkedin = next((s for s in site["socials"] if "linkedin.com" in s), "")

            # Union the two printer sources: Claude reads the (truncated) text
            # with judgement, the regex scan reads everything without it.
            models = [m.strip() for m in re.split(r"[;,]",
                      ex["printer_models"] or "") if m.strip()]
            for m in site["printer_models"]:
                if not any(m.lower() in e.lower() or e.lower() in m.lower()
                           for e in models):
                    models.append(m)

            lead = dict(pr)
            lead.update({
                "email": email,
                "other_emails": [e for e in _prefer_own_domain(
                    site["emails"], _host(pr["website"])) if e != email],
                "phone_web": site["phone"],
                "contacts": ex["people"],
                "equipment_highlights": ex["equipment_highlights"],
                "printer_models": "; ".join(models),
                "pitch": ex["pitch"],
                "socials": site["socials"],
                "org_linkedin": linkedin,
                "pages_crawled": site["pages"],
                "pages_rendered": site["rendered"],
                "enriched": "", "enrich_source": "",
                "verified_maps": today if pr["discovered_via"] == "Google Places" else "",
                # Distinguish "the site says nothing" from "we could not read
                # the site" — otherwise an unreachable host looks identical to
                # a company that simply publishes no contact details.
                "verified_web": (today if site["text"]
                                 else ("site unreachable" if pr["website"] else "")),
                "error": extract_err,
            })
            # Gap fill. Measured at ~$0.18 a call (web results run ~47k tokens),
            # this is by far the most expensive step in the pipeline, so it
            # asks only for fields that would actually change the outcome.
            if use_enrich:
                has_phone = bool(lead["phone_web"] or lead["phone_maps"])
                prints_3d = bool(lead["printer_models"]) or bool(
                    PRINT3D_RE.search((lead["equipment_highlights"] or "")
                                      + " " + lead["type"] + " " + lead["name"]))
                want = []
                if not lead["email"]:
                    # No email is the gap worth paying to close; a missing
                    # phone alongside a working email is not.
                    want.append("email")
                    if not has_phone:
                        want.append("phone")
                elif not has_phone and not lead["contacts"]:
                    want.append("phone")          # otherwise hard to reach
                if not lead["printer_models"] and prints_3d:
                    # Searching a pottery studio for printer models returns
                    # nothing, every time, at full price.
                    want.append("printer_models")
                if want:
                    say(i + 1, len(prospects), f"Searching for missing details: {pr['name']}")
                    found = {}
                    # Free path first: Brave finds the pages, our crawler
                    # reads them, deterministic extractors pull the values.
                    if config.PREFER_BRAVE_SEARCH and config.key("BRAVE_API_KEY"):
                        try:
                            found = enrich_gaps_free(pr["name"], pr["city"],
                                                     pr["website"], want,
                                                     spend=spend)
                        except Exception:
                            found = {}
                    # The LLM web search is the expensive path; only used when
                    # explicitly enabled, or when Brave isn't configured.
                    if not found and (config.GAP_SEARCH_LLM_FALLBACK
                                      or not config.key("BRAVE_API_KEY")):
                        try:
                            found = enrich_gaps(client, pr["name"], pr["city"],
                                                pr["website"], want, spend=spend)
                        except Exception:
                            found = {}
                    if found:
                        if found.get("email"):
                            lead["email"] = found["email"]
                        if found.get("phone"):
                            lead["phone_web"] = found["phone"]
                        if found.get("printer_models"):
                            lead["printer_models"] = found["printer_models"]
                        # Keep provenance distinct: these were not verified on
                        # the company's own site.
                        lead["enriched"] = ", ".join(
                            f for f in want if found.get(f))
                        lead["enrich_source"] = found.get("source", "")

            lead["priority"] = derive_priority(lead)
            leads.append(lead)
    finally:
        renderer.close()

    if config.MERGE_SAME_WEBSITE:
        leads = dedupe_leads(leads)
    if exclude:
        fresh = [L for L in leads
                 if not any(_same_lead(L, old) for old in exclude)]
        seen_before[0] += len(leads) - len(fresh)
        leads = fresh

    dropped = early_dropped
    if config.DROP_EMPTY_LEADS:
        kept = [L for L in leads if not is_empty_lead(L)]
        dropped += len(leads) - len(kept)
        leads = kept

    # Attach the run's cost so callers can show it rather than discover it on
    # a bill. A plain list cannot carry attributes, so return a list subclass
    # — callers that just iterate are unaffected.
    leads = LeadList(leads)
    leads.spend = spend
    leads.dropped = dropped
    leads.seen_before = seen_before[0]
    # Everything this run paid to look at, kept or dropped, so the next run
    # skips it too: a lead dropped for "no printers" would be dropped again.
    leads.processed = processed
    say(len(prospects), len(prospects),
        f"Done — estimated cost ${spend.total():.2f} for {len(leads)} leads"
        + (f" ({dropped} dropped: no printers or equipment found)" if dropped else "")
        + (f" ({seen_before[0]} skipped: found in earlier runs)" if seen_before[0] else ""))
    print(spend.report(len(leads)))
    return leads


def _mock_leads(location):
    """Canned data so the UI + Excel output can be tested without API keys."""
    today = datetime.date.today().strftime("%b %d, %Y")
    return [
        {"name": "Knox Makers", "related": ["Knox Makers Annex"],
         "related_addresses": ["200 Sample Rd, Knoxville TN"],
         "city": location or "Knoxville", "type": "Makerspace",
         "address": "116 Childress St SW, Knoxville TN 37920",
         "phone_maps": "(865) 385-2773", "phone_web": "(865) 385-2773",
         "email": "board@knoxmakers.org", "other_emails": ["info@knoxmakers.org"],
         "website": "https://knoxmakers.org",
         "website_source": "Google Places", "discovered_via": "Google Places",
         "hours": "Tuesday: 6:00 - 9:00 PM; open member access",
         "pitch": "[MOCK] 3 Prusa MK4s already running PETG; member-run space with "
                  "a Board President who signs off on equipment spending",
         "rating": 4.9, "reviews": 42,
         "contacts": [
             {"name": "Jane Doe", "role": "Board President",
              "email": "jane@knoxmakers.org", "phone": "", "linkedin": "",
              "reason": "approves facility and equipment spending"},
             {"name": "Sam Lee", "role": "Technical Director",
              "email": "", "phone": "(865) 555-0142",
              "linkedin": "https://linkedin.com/in/samlee",
              "reason": "runs the shop floor and its printers"},
         ],
         "equipment_highlights": "[MOCK] laser cutter, CNC, 3D printers",
         "printer_models": "[MOCK] Prusa MK4", "socials": [],
         "org_linkedin": "", "verified_maps": today, "verified_web": today,
         "pages_crawled": 12, "pages_rendered": 0,
         "status": "OPERATIONAL", "error": ""},
        {"name": "Volunteer State Fab Lab", "related": [], "related_addresses": [],
         "city": location or "Knoxville", "type": "University makerspace",
         "address": "525 N Gay St, Knoxville TN 37902",
         "phone_maps": "", "phone_web": "",
         "email": "", "other_emails": [], "website": "https://example-fablab.org",
         "website_source": "Web search", "discovered_via": "Web search",
         "hours": "",
         "pitch": "[MOCK] University fab lab; site names no printer models or "
                  "materials in use — too little to size the opportunity yet",
         "rating": None, "reviews": 0,
         "contacts": [], "equipment_highlights": "", "printer_models": "",
         "socials": [], "org_linkedin": "", "verified_maps": "",
         "verified_web": "", "pages_crawled": 4, "pages_rendered": 2,
         "status": "", "error": ""},
    ]
