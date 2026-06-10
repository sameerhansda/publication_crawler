"""
IRINS Publication Scraper — IITD CARE Faculty  v3.0
=====================================================
INSTALL (once):
    pip install requests beautifulsoup4 playwright
    python -m playwright install chromium

RUN:
    python irins_scraper.py

⚠  Run on your own machine (campus/home). IRINS blocks cloud IPs.

HOW IT WORKS:
  • Names  — Playwright headless Chromium renders the Angular page and reads
             the <h1><strong> element at the exact XPath the user identified.
  • Pubs   — POST to /profile/get_publication (fast, no browser needed).
             All pages fetched until empty page returned.
  • Enrich — Each DOI looked up on CrossRef for authoritative authors,
             journal, volume, issue, pages, month.
  • Output — irins_publications/<Faculty_Name>.txt per person.
             Safe to Ctrl-C and resume; progress stored in _progress.json.
"""

import re, json, time, random, logging, traceback
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

FACULTY_IDS = [
    "70201", "70513", "70039", "70509", "70041",
    "70207", "204159", "439874", "150764", "495841",
    "508610", "637498",
]

BASE_URL     = "https://iitd.irins.org"
PUB_ENDPOINT = f"{BASE_URL}/profile/get_publication"
CROSSREF_URL = "https://api.crossref.org/works"
OUTPUT_DIR   = Path("irins_publications")
PROGRESS_FILE = OUTPUT_DIR / "_progress.json"

DELAY_PAGES    = (1.5, 3.5)   # between pagination calls (same profile)
DELAY_PROFILES = (4.0, 9.0)   # between different faculty profiles
DELAY_CROSSREF = (0.5, 1.5)   # between CrossRef lookups

# CSS selectors derived from user-supplied XPath:
#   /html/body/div[1]/div[2]/div/div[2]/div[2]/div[3]/div[1]/div[2]/div[1]/div/ul/li[1]/h1/strong
# We try most-specific first, fall back progressively.
NAME_SELECTORS = [
    "ul li:first-of-type h1 strong",  # exact XPath equivalent
    "ul li:first-child h1 strong",
    "ul li h1 strong",
    "li h1 strong",
    "h1 strong",
    "h1",
]

OUTPUT_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(OUTPUT_DIR / "scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  PLAYWRIGHT — NAME ONLY
#  (Publications use the faster POST endpoint; Playwright only for the name
#   because the Angular page must render before the <h1> is populated.)
# ══════════════════════════════════════════════════════════════════════════════

# We keep a single Playwright browser open for the whole run.
_pw_instance = None
_pw_browser  = None


def _get_browser():
    """Lazily launch Playwright Chromium (reused across profiles)."""
    global _pw_instance, _pw_browser
    if _pw_browser is None:
        from playwright.sync_api import sync_playwright
        _pw_instance = sync_playwright().start()
        _pw_browser  = _pw_instance.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        log.info("Playwright Chromium launched.")
    return _pw_browser


def _close_browser():
    global _pw_instance, _pw_browser
    try:
        if _pw_browser:
            _pw_browser.close()
        if _pw_instance:
            _pw_instance.stop()
    except Exception:
        pass
    _pw_browser  = None
    _pw_instance = None


def fetch_name_playwright(vid: str) -> str:
    """
    Load the IRINS profile page in headless Chromium, wait for Angular to
    render, then read the faculty name from the <h1><strong> element.

    Selector priority (derived from XPath supplied by user):
        ul li:first-of-type h1 strong   ← exact match
        ul li h1 strong
        h1 strong
        h1
        <title> tag                      ← last resort
    """
    browser = _get_browser()
    ctx  = None
    name = None

    try:
        ctx  = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"DNT": "1", "Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()

        url = f"{BASE_URL}/profile/{vid}"
        log.info(f"  [Playwright] Loading {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)

        # Wait for Angular to render the name element
        # Try each selector with a short timeout; stop at first hit.
        for sel in NAME_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=8_000)
                el   = page.query_selector(sel)
                text = (el.inner_text() if el else "").strip()
                text = re.sub(r"\s+", " ", text)
                if text and len(text) > 3 and not text.startswith("{"):
                    name = text
                    log.info(f"  [Playwright] Name via '{sel}': {name}")
                    break
            except Exception:
                continue

        # Fallback: <title> tag (set by server even before Angular boots)
        if not name:
            title = page.title().strip()
            # e.g. "Prof Arun Kumar - Indian Institute of Technology Delhi - IRINS"
            part = re.split(r"\s*[-|]\s*", title)[0].strip()
            if part and len(part) > 3:
                name = part
                log.info(f"  [Playwright] Name via <title>: {name}")

    except Exception as exc:
        log.error(f"  [Playwright] Error fetching name for {vid}: {exc}")
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass

    return name or f"Faculty_{vid}"

# ══════════════════════════════════════════════════════════════════════════════
#  REQUESTS SESSION — PUBLICATIONS + CROSSREF
# ══════════════════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "IITD-CARE-PubScraper/3.0 "
            "(mailto:care@iitd.ac.in; https://care.iitd.ac.in)"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _post_with_backoff(session, url, data, retries=4) -> requests.Response | None:
    waits = [5, 15, 30, 60]
    for attempt in range(retries):
        try:
            r = session.post(url, data=data, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                w = waits[min(attempt, len(waits)-1)] + random.uniform(0, 5)
                log.warning(f"  HTTP {r.status_code} → retry in {w:.0f}s")
                time.sleep(w)
                continue
            log.warning(f"  POST → HTTP {r.status_code}")
            return None
        except requests.RequestException as e:
            w = waits[min(attempt, len(waits)-1)]
            log.warning(f"  POST error: {e} – retry in {w}s")
            time.sleep(w)
    return None


def _get_with_backoff(session, url, retries=4) -> requests.Response | None:
    waits = [3, 10, 25, 60]
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                w = waits[min(attempt, len(waits)-1)] + random.uniform(0, 4)
                log.warning(f"  HTTP {r.status_code} → retry in {w:.0f}s")
                time.sleep(w)
                continue
            return None
        except requests.RequestException as e:
            w = waits[min(attempt, len(waits)-1)]
            log.warning(f"  GET error: {e} – retry in {w}s")
            time.sleep(w)
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  IRINS PUBLICATION FETCHER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all_publications(session: requests.Session, vid: str) -> list[dict]:
    """
    POST to /profile/get_publication with pagination until empty page.
    Returns list of raw pub dicts.
    """
    all_pubs  = []
    seen_keys = set()
    page      = 0

    while True:
        payload = {
            "expert_id"   : vid,
            "current_page": page,
            "sort_by"     : "year",
            "direction"   : "desc",
        }
        log.info(f"  Fetching publications page {page} …")
        r = _post_with_backoff(session, PUB_ENDPOINT, payload)

        if r is None:
            log.warning(f"  Failed to fetch page {page} — stopping")
            break

        soup  = BeautifulSoup(r.text, "html.parser")
        boxes = soup.find_all("div", class_="funny-boxes")

        if not boxes:
            log.info(f"  Page {page}: 0 boxes — end of publications")
            break

        new_count = 0
        for box in boxes:
            pub = _parse_box(box)
            if not pub:
                continue

            # Dedup by DOI → title → raw snippet
            dedup = (pub["doi"].lower() if pub["doi"]
                     else pub["title"].lower()[:80] if pub["title"]
                     else pub["raw_text"][:60].lower())
            if dedup in seen_keys:
                continue
            seen_keys.add(dedup)
            all_pubs.append(pub)
            new_count += 1

        log.info(f"  Page {page}: {len(boxes)} boxes, {new_count} new "
                 f"(total {len(all_pubs)})")

        # IRINS serves 10 per page; < 5 means we're on the last page
        if len(boxes) < 5:
            log.info("  Fewer than 5 results — last page reached")
            break

        page += 1
        time.sleep(random.uniform(*DELAY_PAGES))

    return all_pubs


def _parse_box(box) -> dict | None:
    """Extract structured fields from one IRINS publication <div.funny-boxes>."""
    raw = re.sub(r"\s+", " ", box.get_text(" ", strip=True))
    if len(raw) < 20:
        return None

    # Publication type
    type_span = box.find("span", class_="label-info")
    pub_type  = type_span.get_text(strip=True) if type_span else "Unknown"

    # DOI — strict CrossRef DOI pattern
    doi_m = re.search(r'\b(10\.\d{4,9}/[-._;()\/:A-Z0-9]+)', raw, re.IGNORECASE)
    doi   = doi_m.group(1).rstrip(".,);") if doi_m else ""

    # Title — IRINS wraps it in <b> or <strong>
    title = ""
    t_tag = box.find("b") or box.find("strong")
    if t_tag:
        title = t_tag.get_text(strip=True).strip('"\'')

    # Year
    yr_m = re.search(r'\b(19[6-9]\d|20[012]\d)\b', raw)
    year = yr_m.group(1) if yr_m else ""

    return {
        "doi"     : doi,
        "title"   : title,
        "pub_type": pub_type,
        "year"    : year,
        "raw_text": raw,
    }

# ══════════════════════════════════════════════════════════════════════════════
#  CROSSREF ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

def enrich_crossref(session: requests.Session, doi: str) -> dict | None:
    """Look up a DOI on CrossRef; return clean metadata dict or None."""
    if not doi:
        return None

    r = _get_with_backoff(session, f"{CROSSREF_URL}/{doi}")
    if r is None:
        return None

    try:
        msg = r.json().get("message", {})
    except Exception:
        return None

    # Authors
    authors = []
    for a in msg.get("author", []):
        given  = a.get("given", "").strip()
        family = a.get("family", "").strip()
        if family:
            initials = " ".join(p[0].upper() + "." for p in given.split()) if given else ""
            authors.append(f"{initials} {family}".strip())

    # Journal (prefer full title; fall back to short)
    journal = ""
    for key in ("container-title", "short-container-title"):
        lst = msg.get(key, [])
        if lst and lst[0].strip():
            journal = lst[0].strip()
            break

    # Published date (year + month)
    year = month = ""
    for dk in ("published", "published-print", "published-online", "issued"):
        dp = msg.get(dk, {}).get("date-parts", [[]])
        if dp and dp[0]:
            parts = dp[0]
            if parts[0]: year  = str(parts[0])
            if len(parts) > 1 and parts[1]: month = str(parts[1])
            break

    return {
        "authors": "; ".join(authors),
        "title"  : (msg.get("title") or [""])[0].strip(),
        "journal": journal,
        "volume" : msg.get("volume", ""),
        "issue"  : msg.get("issue", ""),
        "pages"  : msg.get("page", "").replace("-", "–"),
        "year"   : year,
        "month"  : month,
        "doi"    : doi,
    }

# ══════════════════════════════════════════════════════════════════════════════
#  HTML RENDERER
# ══════════════════════════════════════════════════════════════════════════════

MONTH_ABBR = {
    "1":"Jan","01":"Jan","2":"Feb","02":"Feb","3":"Mar","03":"Mar",
    "4":"Apr","04":"Apr","5":"May","05":"May","6":"Jun","06":"Jun",
    "7":"Jul","07":"Jul","8":"Aug","08":"Aug","9":"Sep","09":"Sep",
    "10":"Oct","11":"Nov","12":"Dec",
}


def render_html(pub: dict, cr: dict | None) -> str:
    """
    Merge IRINS raw pub + CrossRef metadata → HTML <li>.
    CrossRef is authoritative when available.

    Format:
      <li>A. Author; B. Author, "Title", <b>Journal</b>,
      vol. V, no. N, pp. P–Q, Mon YYYY.
      DOI: <a href='https://doi.org/…' target='_blank'>…</a></li>
    """
    if cr:
        authors = cr.get("authors", "")
        title   = cr.get("title")  or pub.get("title", "")
        journal = cr.get("journal", "")
        volume  = cr.get("volume", "")
        issue   = cr.get("issue", "")
        pages   = cr.get("pages", "")
        year    = cr.get("year")   or pub.get("year", "")
        month   = cr.get("month", "")
        doi     = cr.get("doi")    or pub.get("doi", "")
    else:
        authors = ""
        title   = pub.get("title", "")
        journal = volume = issue = pages = month = ""
        year    = pub.get("year", "")
        doi     = pub.get("doi", "")

    month_str = MONTH_ABBR.get(str(month), "")
    date_str  = " ".join(filter(None, [month_str, str(year)]))

    title_part  = f'"{title.strip()}"'         if title   else ""
    venue_part  = f"<b>{journal.strip()}</b>"  if journal else ""

    meta = ", ".join(filter(None, [
        f"vol. {volume}"  if volume else "",
        f"no. {issue}"    if issue  else "",
        f"pp. {pages}"    if pages  else "",
        date_str,
    ]))

    core = ", ".join(filter(None, [title_part, venue_part]))
    if meta:
        core = (core + ", " + meta) if core else meta

    authors_fmt = _fmt_authors(authors) if authors else ""
    body = ", ".join(filter(None, [authors_fmt, core]))

    if doi:
        body += f". DOI: <a href='https://doi.org/{doi}' target='_blank'>{doi}</a>"

    if not body.strip():
        body = pub.get("raw_text", "Unknown publication")

    return f"<li>{body}</li>"


def _fmt_authors(raw: str) -> str:
    """Normalise author string → 'A. Kumar; B. Sharma' style."""
    parts = re.split(r";\s*| and | AND ", raw)
    out = []
    for p in parts:
        p = p.strip().strip(".")
        if not p:
            continue
        if re.match(r"^([A-Z]\.)+\s+\S", p):      # already A. Kumar
            out.append(p); continue
        if "," in p:                                # Kumar, Arun B
            last, rest = p.split(",", 1)
            inits = " ".join(x[0].upper() + "." for x in rest.split() if x)
            out.append(f"{inits} {last.strip()}" if inits else last.strip())
        else:                                       # Arun B Kumar
            toks = p.split()
            if len(toks) >= 2:
                inits = " ".join(t[0].upper() + "." for t in toks[:-1])
                out.append(f"{inits} {toks[-1]}")
            else:
                out.append(p)
    return "; ".join(out)

# ══════════════════════════════════════════════════════════════════════════════
#  FILE OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def _safe_fname(name: str) -> str:
    n = re.sub(r"[^\w\s\-]", "", name).strip()
    n = re.sub(r"\s+", "_", n)
    return n[:80] or "Unknown"


def write_file(name: str, vid: str, lines: list[str]) -> Path:
    fpath = OUTPUT_DIR / (_safe_fname(name) + ".txt")
    header = (
        f"<!-- Faculty     : {name} -->\n"
        f"<!-- IRINS ID    : {vid} -->\n"
        f"<!-- Generated   : {datetime.now():%Y-%m-%d %H:%M} -->\n"
        f"<!-- Publications: {len(lines)} -->\n\n"
    )
    fpath.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return fpath

# ══════════════════════════════════════════════════════════════════════════════
#  PROGRESS / RESUME
# ══════════════════════════════════════════════════════════════════════════════

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_progress(prog: dict) -> None:
    PROGRESS_FILE.write_text(json.dumps(prog, indent=2), encoding="utf-8")

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_profile(session: requests.Session, vid: str) -> tuple[str, list[str]]:
    log.info(f"┌── {vid} {'─'*50}")

    # 1. Name via Playwright (Angular-rendered)
    name = fetch_name_playwright(vid)
    log.info(f"│  Name: {name}")
    time.sleep(random.uniform(1.0, 2.0))

    # 2. Publications via POST endpoint (all pages)
    raw_pubs = fetch_all_publications(session, vid)
    log.info(f"│  Raw publications: {len(raw_pubs)}")

    # 3. Enrich via CrossRef + render
    html_lines    = []
    doi_cache     = {}
    cr_ok = cr_miss = 0

    for i, pub in enumerate(raw_pubs, 1):
        doi = pub.get("doi", "")
        cr  = None
        if doi:
            if doi not in doi_cache:
                doi_cache[doi] = enrich_crossref(session, doi)
                time.sleep(random.uniform(*DELAY_CROSSREF))
            cr = doi_cache[doi]
        (cr_ok if cr else cr_miss).__class__  # dummy — counts below
        if cr: cr_ok   += 1
        else:  cr_miss += 1
        html_lines.append(render_html(pub, cr))
        if i % 10 == 0:
            log.info(f"│  Rendered {i}/{len(raw_pubs)} …")

    log.info(f"│  CrossRef: {cr_ok} enriched, {cr_miss} skipped/no-DOI")
    log.info(f"└── Done: {len(html_lines)} publications")
    return name, html_lines


def main():
    log.info("══════════════════════════════════════════════")
    log.info("  IRINS Scraper v3.0  —  IITD CARE Faculty")
    log.info(f"  Profiles : {len(FACULTY_IDS)}")
    log.info(f"  Output   : {OUTPUT_DIR.resolve()}")
    log.info("══════════════════════════════════════════════")

    prog    = load_progress()
    session = make_session()

    try:
        for vid in FACULTY_IDS:
            if prog.get(vid) == "done":
                log.info(f"Skipping {vid} (done — delete from _progress.json to redo)")
                continue

            try:
                name, lines = process_profile(session, vid)
                fpath = write_file(name, vid, lines)
                log.info(f"✓  {fpath.name}  ({len(lines)} pubs)")
                prog[vid] = "done"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error(f"✗  {vid}: {exc}\n{traceback.format_exc()}")
                prog[vid] = f"error: {exc}"

            save_progress(prog)
            delay = random.uniform(*DELAY_PROFILES)
            log.info(f"   waiting {delay:.1f}s …\n")
            time.sleep(delay)

    except KeyboardInterrupt:
        log.info("Interrupted — progress saved.")
    finally:
        save_progress(prog)
        _close_browser()

    log.info("══ All done ══")
    print(f"\n✅  Files written to: {OUTPUT_DIR.resolve()}/")


if __name__ == "__main__":
    main()

