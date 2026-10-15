#!/usr/bin/env python3
"""
book_watcher.py

Checks AbeBooks, eBay and Biblio each time it's run for listings matching a
set of target rare/first-edition books, filters out listings that don't match
the edition/publisher/year criteria, and emails a summary of matches.

Matching is synonym-aware: "1st", "first", "1st ed.", "first edition",
"1st/1st", "print" vs "printing", "advisor" vs "adviser", "Aequanimitas" vs
"Aequanimitas"/"Equanimitas" etc. are all normalised before comparison, and
each book is searched with several query permutations.

Run this once a day via cron / Task Scheduler / GitHub Actions.
See README.md for setup instructions.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SEEN_FILE = SCRIPT_DIR / "seen_listings.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

REQUEST_DELAY_SECONDS = 2  # be polite between requests


# ---------------------------------------------------------------------------
# TEXT NORMALISATION (this is what makes 1st == first, advisor == adviser...)
# ---------------------------------------------------------------------------

_ORDINALS = {
    "1": "first", "2": "second", "3": "third", "4": "fourth", "5": "fifth",
    "6": "sixth", "7": "seventh", "8": "eighth", "9": "ninth", "10": "tenth",
}
_ORD_WORDS = "first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"

# Spelling variants collapsed to one canonical form (applied to BOTH the
# listing text and our own keywords, so either spelling matches either).
_SPELLING = [
    (r"\badvisor\b", "adviser"),
    (r"\baequanimitas\b", "equanimitas"),   # also covers the "æ" ligature
    (r"\bdust ?wrapper\b", "dust jacket"),
]


def normalize(text: str) -> str:
    """Lower-case, strip accents/punctuation and unify edition wording."""
    t = (text or "").lower()
    t = t.replace("æ", "ae").replace("œ", "oe").replace("&", " and ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))

    # "1st/1st" is dealer shorthand for first edition, first printing
    t = re.sub(r"\b(?:1st|first)\s*/\s*(?:1st|first)\b",
               " first edition first printing ", t)

    # 1st -> first, 2nd -> second ...
    t = re.sub(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\b",
               lambda m: _ORDINALS.get(m.group(1), m.group(0)), t)

    # punctuation -> spaces ("ed." -> "ed", "E.O." -> "e o", "1849-1919" -> "1849 1919")
    t = re.sub(r"[^a-z0-9]+", " ", t)

    # first ed / first edn / first edition -> "first edition"
    t = re.sub(rf"\b({_ORD_WORDS})\s+(?:ed|edn|edition)\b", r"\1 edition", t)
    # first print / first printing / first impression -> "first printing"
    t = re.sub(rf"\b({_ORD_WORDS})\s+(?:print|printing|impression)\b",
               r"\1 printing", t)

    for pattern, repl in _SPELLING:
        t = re.sub(pattern, repl, t)

    return re.sub(r"\s+", " ", t).strip()


def contains(text: str, phrase: str) -> bool:
    """Whole-word phrase search on already-normalised text."""
    p = normalize(phrase)
    return bool(p) and re.search(rf"\b{re.escape(p)}\b", text) is not None


# "first edition", optionally "first american edition", "first us edition", ...
FIRST_EDITION_RE = re.compile(
    r"\bfirst (?:(?:american|us|uk|english|british|canadian|trade|hardcover|"
    r"hardback|true|printed) )?edition\b|\btrue first\b|\beditio princeps\b"
)
FIRST_PRINTING_RE = re.compile(r"\bfirst printing\b")


# ---------------------------------------------------------------------------
# TARGET BOOKS
# ---------------------------------------------------------------------------

EDITION_PHRASES = ["first edition", "1st edition"]


def make_queries(bases: List[str], phrases: Optional[List[str]] = None) -> List[str]:
    """Cross every base query with every edition phrasing ('first' AND '1st')."""
    phrases = EDITION_PHRASES if phrases is None else phrases
    return [f"{b} {p}".strip() for b in bases for p in phrases] if phrases else list(bases)


@dataclass
class BookTarget:
    key: str                              # short id used in seen_listings.json
    label: str                            # human-readable name for the report
    search_queries: List[str]             # every permutation we send to each site
    title_groups: List[List[str]]         # ALL groups must match; within a group ANY synonym
    edition_mode: str = "first_edition"   # "first_edition" | "first_edition_or_printing" | "none"
    years: List[str] = field(default_factory=list)   # acceptable publication years
    year_required: bool = True            # if False, a year match is only flagged
    publishers: List[str] = field(default_factory=list)  # bonus flag only
    exclude: List[str] = field(default_factory=list)     # ANY of these disqualifies


BOOKS: List[BookTarget] = [
    BookTarget(
        key="netter",
        label="Netter - Atlas of Human Anatomy (1989 true 1st ed.)",
        search_queries=make_queries(["Netter Atlas of Human Anatomy 1989"]),
        title_groups=[["netter"], ["atlas of human anatomy"]],
        years=["1989"],
        publishers=["ciba", "ciba-geigy"],
        exclude=[
            "icon learning", "second edition", "third edition", "fourth edition",
            "fifth edition", "sixth edition", "seventh edition", "eighth edition",
            "reprint", "facsimile", "student edition",
        ],
    ),
    BookTarget(
        key="osler",
        label="Osler - Principles and Practice of Medicine (1892 1st ed., 1st print)",
        search_queries=make_queries([
            "Osler Principles and Practice of Medicine 1892",
            "Osler Principles and Practice of Medicine",
        ]),
        title_groups=[["osler"], ["principles and practice of medicine"]],
        years=["1892"],
        year_required=False,   # many dealers omit the year; it's flagged instead
        publishers=["appleton", "d. appleton"],
        exclude=[
            "reprint", "facsimile", "classics of medicine library", "later edition",
            "revised", "second edition", "third edition", "fourth edition",
            "fifth edition", "sixth edition", "seventh edition", "eighth edition",
            "book club",
        ],
    ),
    BookTarget(
        key="osler_selection",
        label="Sir William Osler 1849-1919: A Selection for Medical Students",
        search_queries=[
            "Sir William Osler 1849-1919 A Selection for Medical Students",
            "Osler A Selection for Medical Students",
            "Sir William Osler 1849 1919 Selection Medical Students",
        ],
        title_groups=[["osler"], ["selection for medical students"]],
        edition_mode="none",   # no edition specified for this one - any copy
    ),
    BookTarget(
        key="aequanimitas",
        label="Osler - Aequanimitas and Other Addresses (1st ed., 1st print; 1904/1905)",
        search_queries=make_queries([
            "Osler Aequanimitas and other addresses",
            "Osler Aequanimitas with other addresses to medical students",
            "Osler Equanimitas other addresses",   # spelling variant
        ]),
        title_groups=[["osler"], ["equanimitas"]],
        # first ed. is 1904 (H.K. Lewis); the user also cited 1905, so accept both
        years=["1904", "1905"],
        year_required=False,
        publishers=["blakiston", "h k lewis", "lewis"],
        exclude=[
            "reprint", "facsimile", "classics of medicine library",
            "second edition", "third edition", "fourth edition", "book club",
        ],
    ),
    BookTarget(
        key="fishbein",
        label="Fishbein - Modern Home Medical Adviser (1st ed., 1st print)",
        search_queries=make_queries([
            "Fishbein Modern Home Medical Adviser",
            "Fishbein Modern Home Medical Advisor",   # spelling variant
        ]),
        title_groups=[["fishbein"], ["modern home medical adviser"]],
        years=["1935"],
        year_required=False,
        publishers=["doubleday"],
        exclude=[
            "reprint", "facsimile", "revised", "new revised", "second edition",
            "third edition", "fourth edition", "book club",
        ],
    ),
    BookTarget(
        key="silent_spring",
        label="Rachel Carson - Silent Spring (1962 1st ed.)",
        search_queries=make_queries(["Rachel Carson Silent Spring 1962"]),
        title_groups=[["silent spring"]],
        years=["1962"],
        publishers=["houghton mifflin"],
        exclude=[
            "book club", "bce", "reprint", "facsimile", "later printing",
            "second printing", "third printing",
        ],
    ),
    BookTarget(
        key="sociobiology",
        label="E.O. Wilson - Sociobiology: The New Synthesis (1975 1st ed.)",
        search_queries=make_queries(["Wilson Sociobiology New Synthesis 1975"]),
        title_groups=[["wilson"], ["sociobiology"]],
        years=["1975"],
        publishers=["belknap", "harvard university press"],
        exclude=[
            "reprint", "facsimile", "abridged", "second printing", "third printing",
        ],
    ),
    BookTarget(
        key="cajal",
        label="Cajal - Histology of the Nervous System (1995 English 1st printing)",
        search_queries=make_queries(
            ["Cajal Histology of the Nervous System 1995"],
            EDITION_PHRASES + ["first printing", "1st printing"],
        ),
        title_groups=[["cajal"], ["histology of the nervous system"]],
        edition_mode="first_edition_or_printing",
        years=["1995"],
        publishers=["oxford university press", "oxford"],
        exclude=["reprint", "facsimile", "paperback reprint"],
    ),
    BookTarget(
        key="laboratory_life",
        label="Latour & Woolgar - Laboratory Life (1st ed./1st print; Sage 1979 or Princeton 1986)",
        search_queries=make_queries(
            [
                "Latour Woolgar Laboratory Life Construction of Scientific Facts",
                "Latour Woolgar Laboratory Life Social Construction of Scientific Facts",
            ],
            EDITION_PHRASES + ["first printing"],
        ),
        title_groups=[["laboratory life"]],
        # "The Construction of Scientific Facts" is the 1986 Princeton (2nd) ed.;
        # the true 1st edition is the 1979 Sage "Social Construction". Sellers of the
        # Princeton book usually say "first printing", so accept either wording.
        edition_mode="first_edition_or_printing",
        years=["1979", "1986"],
        year_required=False,
        publishers=["sage", "princeton"],
        exclude=["reprint", "facsimile", "book club", "later printing", "second printing"],
    ),
]


# ---------------------------------------------------------------------------
# SCRAPERS
# ---------------------------------------------------------------------------

def fetch_abebooks(query: str) -> List[Dict]:
    """Scrape AbeBooks search results for a query."""
    url = "https://www.abebooks.com/servlet/SearchResults"
    params = {"kn": query, "sortby": "17"}  # sortby=17 -> most recently listed
    listings = []
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # AbeBooks result cards. Selectors may need updating if their
        # markup changes -- this targets the common result-item structure.
        cards = soup.select("li.cf")
        if not cards:
            cards = soup.select("[data-cy='listing-item']")

        for card in cards:
            title_el = card.select_one("span.title, [data-cy='listing-title']")
            price_el = card.select_one("p.item-price, [data-cy='listing-price']")
            link_el = card.select_one("a[href]")
            seller_desc_el = card.select_one("p.d-item-description, [data-cy='listing-description']")

            title = title_el.get_text(strip=True) if title_el else ""
            price = price_el.get_text(strip=True) if price_el else ""
            link = link_el["href"] if link_el else ""
            if link and link.startswith("/"):
                link = "https://www.abebooks.com" + link
            extra = seller_desc_el.get_text(strip=True) if seller_desc_el else ""

            if title:
                listings.append({
                    "source": "AbeBooks",
                    "title": title,
                    "price": price,
                    "link": link,
                    "text_blob": " ".join([title, extra]),
                })
    except requests.RequestException as e:
        print(f"[AbeBooks] request failed for '{query}': {e}", file=sys.stderr)

    return listings


def fetch_ebay(query: str) -> List[Dict]:
    """Scrape eBay search results for a query (active listings, Books category)."""
    url = "https://www.ebay.com/sch/i.html"
    params = {"_nkw": query, "_sacat": 267, "LH_ItemCondition": ""}  # 267 = Books category
    listings = []
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.select("li.s-item")
        for card in cards:
            title_el = card.select_one(".s-item__title")
            price_el = card.select_one(".s-item__price")
            link_el = card.select_one("a.s-item__link")
            subtitle_el = card.select_one(".s-item__subtitle")

            title = title_el.get_text(strip=True) if title_el else ""
            if not title or "shop on ebay" in title.lower():
                continue
            price = price_el.get_text(strip=True) if price_el else ""
            link = link_el["href"] if link_el else ""
            extra = subtitle_el.get_text(strip=True) if subtitle_el else ""

            listings.append({
                "source": "eBay",
                "title": title,
                "price": price,
                "link": link,
                "text_blob": " ".join([title, extra]),
            })
    except requests.RequestException as e:
        print(f"[eBay] request failed for '{query}': {e}", file=sys.stderr)

    return listings


def fetch_biblio(query: str) -> List[Dict]:
    """Scrape Biblio.com search results for a query.

    NOTE: Biblio.com runs bot-detection that blocked automated inspection of
    its live markup while building this script, so the selectors below are a
    best-effort guess based on Biblio's known result-page conventions, not a
    verified match. If this function returns 0 results while AbeBooks/eBay
    return results normally, that's the signal the selectors need updating --
    open the search URL in a real browser, inspect one result card, and
    adjust the `card.select_one(...)` lines below to match.
    """
    url = "https://www.biblio.com/bookstore/search.php"
    params = {"stage": "1", "keyisbn": query, "sortby": "17"}
    listings = []
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.select("div.result-item, div.result-info, li.search-result")
        for card in cards:
            title_el = card.select_one("a.result-title, .title, h3 a")
            price_el = card.select_one(".price, .result-price")
            link_el = card.select_one("a[href]")
            desc_el = card.select_one(".result-desc, .description, .edition")

            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue
            price = price_el.get_text(strip=True) if price_el else ""
            link = link_el["href"] if link_el else ""
            if link and link.startswith("/"):
                link = "https://www.biblio.com" + link
            extra = desc_el.get_text(strip=True) if desc_el else ""

            listings.append({
                "source": "Biblio",
                "title": title,
                "price": price,
                "link": link,
                "text_blob": " ".join([title, extra]),
            })
    except requests.RequestException as e:
        print(f"[Biblio] request failed for '{query}': {e}", file=sys.stderr)

    return listings


FETCHERS = (fetch_abebooks, fetch_ebay, fetch_biblio)


# ---------------------------------------------------------------------------
# MATCHING LOGIC
# ---------------------------------------------------------------------------

def match_listing(listing: Dict, book: BookTarget) -> Optional[List[str]]:
    """Return None if the listing doesn't match, else a list of info flags."""
    text = normalize(listing["text_blob"])

    # Every title group must match (any synonym within the group)
    for group in book.title_groups:
        if not any(contains(text, kw) for kw in group):
            return None

    # Year
    year_hits = [y for y in book.years if re.search(rf"\b{y}\b", text)]
    if book.years and book.year_required and not year_hits:
        return None

    # Disqualifying words (later editions, reprints, book-club copies...)
    if any(contains(text, kw) for kw in book.exclude):
        return None

    # Edition / printing
    first_ed = bool(FIRST_EDITION_RE.search(text))
    first_pr = bool(FIRST_PRINTING_RE.search(text))
    if book.edition_mode == "first_edition" and not first_ed:
        return None
    if book.edition_mode == "first_edition_or_printing" and not (first_ed or first_pr):
        return None

    flags = []
    if first_ed:
        flags.append("1st edition stated")
    if first_pr:
        flags.append("1st printing stated")
    if year_hits:
        flags.append("year " + "/".join(year_hits))
    pubs = [p for p in book.publishers if contains(text, p)]
    if pubs:
        flags.append("publisher: " + pubs[0])
    return flags


# ---------------------------------------------------------------------------
# STATE (avoid re-alerting on the same listing every day)
# ---------------------------------------------------------------------------

def load_seen() -> Dict[str, List[str]]:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: Dict[str, List[str]]) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def listing_id(listing: Dict) -> str:
    # link is the most stable unique identifier we have
    return listing["link"] or f"{listing['source']}|{listing['title']}"


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def send_email_report(config: Dict, report_text: str, has_new_matches: bool) -> None:
    if not report_text.strip():
        return

    subject = "Rare Book Watch: new listings found" if has_new_matches else "Rare Book Watch: daily check (no new matches)"

    msg = MIMEText(report_text)
    msg["Subject"] = subject
    msg["From"] = config["sender_email"]
    msg["To"] = config["recipient_email"]

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config["smtp_server"], config.get("smtp_port", 465), context=context) as server:
        server.login(config["sender_email"], config["sender_password"])
        server.sendmail(config["sender_email"], config["recipient_email"], msg.as_string())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    # Prefer environment variables (used by GitHub Actions / Secrets).
    # Falls back to config.json for local runs.
    if all(os.environ.get(v) for v in ["SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL"]):
        return {
            "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
            "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
            "sender_email": os.environ["SENDER_EMAIL"],
            "sender_password": os.environ["SENDER_PASSWORD"],
            "recipient_email": os.environ["RECIPIENT_EMAIL"],
        }

    if not CONFIG_FILE.exists():
        print(
            f"Missing {CONFIG_FILE} and no SENDER_EMAIL/SENDER_PASSWORD/RECIPIENT_EMAIL "
            "environment variables set. Either copy config.example.json to config.json "
            "and fill in your email details, or set those env vars (e.g. via GitHub Secrets).",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())


def collect_matches(book: BookTarget) -> List[Dict]:
    """Run every query permutation on every site; return de-duplicated matches."""
    found: Dict[str, Dict] = {}
    for query in book.search_queries:
        for fetch_fn in FETCHERS:
            try:
                raw = fetch_fn(query)
            except Exception as e:  # one broken site must not kill the whole run
                print(f"[{fetch_fn.__name__}] unexpected error for '{query}': {e}", file=sys.stderr)
                raw = []
            time.sleep(REQUEST_DELAY_SECONDS)

            for listing in raw:
                flags = match_listing(listing, book)
                if flags is None:
                    continue
                lid = listing_id(listing)
                if lid not in found:
                    listing["flags"] = flags
                    found[lid] = listing
    return list(found.values())


def main():
    config = load_config()
    seen = load_seen()

    all_new_sections = []
    all_current_sections = []

    for book in BOOKS:
        book_seen = set(seen.get(book.key, []))
        current_matches = collect_matches(book)
        new_listings = [l for l in current_matches if listing_id(l) not in book_seen]
        book_seen.update(listing_id(l) for l in current_matches)
        seen[book.key] = sorted(book_seen)

        if current_matches:
            lines = [f"\n== {book.label} ({len(current_matches)} matching listing(s) found) ==\n"]
            for listing in current_matches:
                marker = "[NEW] " if listing in new_listings else ""
                lines.append(f"{marker}{listing['source']}: {listing['title']}")
                lines.append(f"    Price: {listing['price'] or 'n/a'}")
                if listing.get("flags"):
                    lines.append(f"    Notes: {', '.join(listing['flags'])}")
                lines.append(f"    Link:  {listing['link']}\n")
            section = "\n".join(lines)
            all_current_sections.append(section)
            if new_listings:
                all_new_sections.append(section)

    save_seen(seen)

    if all_current_sections:
        report = (
            "Daily Rare Book Watch\n"
            "======================\n"
            + "\n".join(all_current_sections)
        )
    else:
        report = "Daily Rare Book Watch\n======================\n\nNo matching listings found today for any target book."

    print(report)
    send_email_report(config, report, has_new_matches=bool(all_new_sections))


if __name__ == "__main__":
    main()
