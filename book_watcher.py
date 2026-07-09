#!/usr/bin/env python3
"""
book_watcher.py

Checks AbeBooks and eBay each time it's run for listings matching a set of
target rare/first-edition books, filters out listings that don't match the
edition/publisher/year criteria, and emails a summary of new matches.

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
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Dict, Optional

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


@dataclass
class BookTarget:
    key: str                       # short id, e.g. "netter"
    label: str                     # human-readable name for reports
    search_query: str              # query sent to AbeBooks / eBay
    title_keywords: List[str]      # ALL must appear (case-insensitive)
    year: str                      # expected year, as string, e.g. "1989"
    edition_keywords: List[str]    # ANY of these should appear
    publisher_keywords: List[str] = field(default_factory=list)  # ANY of these (bonus, not required)
    exclude_keywords: List[str] = field(default_factory=list)    # ANY of these disqualifies


# ---------------------------------------------------------------------------
# TARGET BOOKS
# Edit this list to tune matching. exclude_keywords is your main defense
# against later reprints / facsimiles / student editions showing up.
# ---------------------------------------------------------------------------

BOOKS: List[BookTarget] = [
    BookTarget(
        key="netter",
        label="Netter — Atlas of Human Anatomy (1989 true 1st ed.)",
        search_query="Netter Atlas of Human Anatomy 1989 first edition",
        title_keywords=["netter", "atlas of human anatomy"],
        year="1989",
        edition_keywords=["first edition", "1st edition", "1st ed"],
        publisher_keywords=["ciba", "ciba-geigy"],
        exclude_keywords=[
            "icon learning", "2nd edition", "second edition", "3rd edition",
            "third edition", "4th edition", "5th edition", "6th edition",
            "7th edition", "reprint", "facsimile", "student edition",
        ],
    ),
    BookTarget(
        key="osler",
        label="Osler — Principles and Practice of Medicine (1892 1st ed.)",
        search_query="Osler Principles and Practice of Medicine 1892 first edition",
        title_keywords=["osler", "principles and practice of medicine"],
        year="1892",
        edition_keywords=["first edition", "1st edition", "1st ed"],
        publisher_keywords=["appleton", "d. appleton"],
        exclude_keywords=[
            "reprint", "facsimile", "later edition", "revised",
            "2nd edition", "second edition", "book club",
        ],
    ),
    BookTarget(
        key="silent_spring",
        label="Rachel Carson — Silent Spring (1962 1st ed.)",
        search_query="Rachel Carson Silent Spring 1962 first edition",
        title_keywords=["silent spring"],
        year="1962",
        edition_keywords=["first edition", "1st edition", "1st ed"],
        publisher_keywords=["houghton mifflin"],
        exclude_keywords=[
            "book club", "bce", "reprint", "facsimile", "later printing",
            "2nd printing", "second printing",
        ],
    ),
    BookTarget(
        key="sociobiology",
        label="E.O. Wilson — Sociobiology (1975 1st ed.)",
        search_query="E.O. Wilson Sociobiology 1975 first edition",
        title_keywords=["wilson", "sociobiology"],
        year="1975",
        edition_keywords=["first edition", "1st edition", "1st ed"],
        publisher_keywords=["belknap", "harvard university press"],
        exclude_keywords=[
            "reprint", "facsimile", "abridged", "new synthesis reprint",
            "2nd printing", "second printing",
        ],
    ),
    BookTarget(
        key="cajal",
        label="Cajal — Histology of the Nervous System (1995 English 1st printing)",
        search_query="Cajal Histology of the Nervous System 1995 first edition English",
        title_keywords=["cajal", "histology of the nervous system"],
        year="1995",
        edition_keywords=["first edition", "1st edition", "1st printing", "first printing"],
        publisher_keywords=["oxford university press", "oxford"],
        exclude_keywords=["reprint", "facsimile", "paperback reprint"],
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
                    "text_blob": " ".join([title, extra]).lower(),
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
                "text_blob": " ".join([title, extra]).lower(),
            })
    except requests.RequestException as e:
        print(f"[eBay] request failed for '{query}': {e}", file=sys.stderr)

    return listings


# ---------------------------------------------------------------------------
# MATCHING LOGIC
# ---------------------------------------------------------------------------

def matches_book(listing: Dict, book: BookTarget) -> bool:
    text = listing["text_blob"]

    # All title keywords must appear
    if not all(kw in text for kw in book.title_keywords):
        return False

    # Year must appear somewhere
    if book.year not in text:
        return False

    # Any exclude keyword present -> reject
    if any(kw in text for kw in book.exclude_keywords):
        return False

    # At least one edition keyword should appear (guards against
    # later/undated reprints with no edition info at all being treated
    # as a match -- if the listing genuinely doesn't mention edition,
    # it's better to surface it as "unclear" than silently include it).
    has_edition_signal = any(kw in text for kw in book.edition_keywords)

    # Publisher is a bonus signal, not required (many listings omit it),
    # but if publisher keywords are given AND clearly a *different*
    # well-known publisher appears, that's suspicious -- we don't try to
    # enumerate all wrong publishers, so we just don't penalize here.

    return has_edition_signal


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
    return listing["link"] or listing["title"]


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


def main():
    config = load_config()
    seen = load_seen()

    all_new_sections = []
    all_current_sections = []

    for book in BOOKS:
        book_seen = set(seen.get(book.key, []))
        new_listings = []
        current_matches = []

        for fetch_fn in (fetch_abebooks, fetch_ebay):
            raw_listings = fetch_fn(book.search_query)
            time.sleep(REQUEST_DELAY_SECONDS)

            for listing in raw_listings:
                if matches_book(listing, book):
                    current_matches.append(listing)
                    lid = listing_id(listing)
                    if lid not in book_seen:
                        new_listings.append(listing)
                        book_seen.add(lid)

        seen[book.key] = list(book_seen)

        if current_matches:
            lines = [f"\n== {book.label} ({len(current_matches)} matching listing(s) found) ==\n"]
            for listing in current_matches:
                marker = "[NEW] " if listing in new_listings else ""
                lines.append(f"{marker}{listing['source']}: {listing['title']}")
                lines.append(f"    Price: {listing['price'] or 'n/a'}")
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
