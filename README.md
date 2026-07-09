# Rare Book Watcher

Checks AbeBooks and eBay for listings matching 5 target first editions, filters
by edition/year/publisher, and emails you a daily summary (new matches flagged
`[NEW]`, plus a full list of everything currently matching).

## Books tracked
1. Netter, *Atlas of Human Anatomy* — 1989 true 1st edition
2. Osler, *Principles and Practice of Medicine* — 1892 1st edition
3. Rachel Carson, *Silent Spring* — 1962 1st edition
4. E.O. Wilson, *Sociobiology* — 1975 1st edition
5. Cajal, *Histology of the Nervous System* — 1995 English 1st printing/edition

Edit the `BOOKS` list near the top of `book_watcher.py` to adjust keywords,
add exclusions, or add more books.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Create your config:
   ```bash
   cp config.example.json config.json
   ```
   Edit `config.json` with your email details. **For Gmail**, don't use your
   normal password — create an "App Password":
   Google Account → Security → 2-Step Verification → App passwords.
   Use that 16-character password in `sender_password`.

   (Any SMTP provider works — just change `smtp_server`/`smtp_port`.)

3. Test it manually:
   ```bash
   python3 book_watcher.py
   ```
   It prints the report to the console and emails it. First run will likely
   email you a big list since nothing is "seen" yet — that's expected.

## Scheduling it every morning

**Mac/Linux (cron):**
```bash
crontab -e
```
Add (runs 7:00 AM daily):
```
0 7 * * * cd /full/path/to/book_watcher && /usr/bin/python3 book_watcher.py >> run.log 2>&1
```

**Windows (Task Scheduler):**
Create a Basic Task → Trigger: Daily → Action: Start a program →
Program: `python`, Arguments: `book_watcher.py`, Start in: the folder path.

**Cloud option (no computer needs to stay on):** this can also run as a free
GitHub Actions scheduled workflow — let me know if you'd like that version
and I'll set up the `.yml` workflow file (you'd store your email credentials
as encrypted GitHub Secrets instead of `config.json`).

## Running it on GitHub Actions instead (no computer needs to stay on)

1. Create a new GitHub repo (public keeps Actions minutes 100% free and
   unlimited; private is also fine — this script's daily usage, ~1-2
   minutes/day, is well under the 2,000 free minutes/month private repos get).

2. Push all these files to it, **except `config.json`** (already excluded
   via `.gitignore` — never commit real credentials).

3. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret** and add each of these:
   - `SENDER_EMAIL` — your Gmail address
   - `SENDER_PASSWORD` — your Gmail App Password (not your normal password)
   - `RECIPIENT_EMAIL` — where you want the report sent
   - `SMTP_SERVER` — `smtp.gmail.com` (or your provider's)
   - `SMTP_PORT` — `465`

   The script automatically prefers these environment variables over
   `config.json`, so no code changes are needed.

4. The workflow file `.github/workflows/book_watch.yml` is already set up to:
   - run every day at 07:00 UTC (edit the `cron` line to shift the time —
     GitHub Actions schedules always run in UTC)
   - install dependencies and run the script
   - commit the updated `seen_listings.json` back to the repo, so it
     remembers what it already alerted you about

5. You can also trigger a run immediately for testing: go to the **Actions**
   tab → **Daily Rare Book Watch** → **Run workflow**.

6. Check the Actions tab's run logs if you don't get an email — it'll show
   whether the scrape or the email step failed.

## Important caveats

- **Biblio.com selectors are unverified**: Biblio's site blocked automated
  inspection while building this, so `fetch_biblio()` uses best-guess CSS
  selectors rather than ones confirmed against live markup. Run the script
  once and check the console output/email — if AbeBooks and eBay return
  matches but Biblio consistently returns zero, the selectors in
  `fetch_biblio()` likely need updating (open a Biblio search result page in
  a browser, inspect one listing, and adjust the `card.select_one(...)`
  lines).
- **Scraping fragility**: AbeBooks, eBay, and Biblio all change their page
  markup periodically, which can break the CSS selectors in the corresponding
  `fetch_*()` functions. If the script suddenly reports zero results
  everywhere, that's the likely cause — the selectors need a quick update.
- **eBay's official API**: for something more robust long-term, eBay offers
  a free Browse API (needs a developer account + App ID) that returns
  structured JSON instead of scraped HTML. I kept this version
  scrape-based to get you running immediately without an API signup, but
  I can swap it in if you want more reliability.
- **Rate limiting / blocking**: both sites may occasionally serve CAPTCHAs
  or block rapid/automated requests. Running once a day, as intended,
  should be fine.
- **Google search**: intentionally not included — Google blocks scraping of
  its results, and since your real targets (AbeBooks, eBay) are queried
  directly, the search step wasn't adding anything.
- **`seen_listings.json`**: created automatically after first run to avoid
  re-alerting on listings you've already seen. Delete it to reset.
