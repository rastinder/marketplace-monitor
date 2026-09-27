# marketplace-monitor

**What it's for:** Facebook Marketplace is meant for physical goods — but people keep
using it to sell *labour* (laptop repair, web design, tutoring, handyman work, 3D
printing). Those ads are against the platform's rules: to sell a service you have to
buy a promoted ad instead. This project finds those ads, works out which listings are
genuinely services rather than honest product sales, and files a report through
Facebook's own menu — keeping a screenshot and a reason for every single decision so
nothing happens silently.

It is a research/clean-up tool for people who care about keeping a marketplace honest:
sellers who want the category kept clean, community members, and people studying UI
automation on a real app.

**What it does not do:** it never posts, comments, messages, calls, follows, likes, or
changes the account. It only reads the screen and — where you allow it — taps
*Report listing*.

Two independent jobs share one phone through file locks:

| Job | What it does | Entry point |
|---|---|---|
| **mass-report** | sweeps Marketplace search results and reports ads that sell services | `run_mass_report_cron.sh` → `mass_report.py` |
| **researcher** | read-only 2-hour ad discovery run (never reports) | `run_opencode_2h.sh` → `opencode_2h_prompt.txt` |

### How one report happens

```
search "pc repair"  ──►  results grid
        │
        ├─ card labelled "Sponsored"  ─────────────────►  skipped (never reported)
        ├─ title looks like a product (brand/model/part) ─►  spared
        ├─ title is truncated ("...And R…") ─► open the listing, read the real title
        │
        └─ title/description offers labour  ─►  More actions
                                                  → Report listing
                                                  → Continue
                                                  → Selling or promoting restricted items
                                                  → Other restricted products
                                                  → Submit
                                                  → screenshot "Thanks for reporting"
```



> **Use responsibly.** Automating an app against its Terms of Service can get an
> account restricted. Use it on accounts and devices you are authorised to drive, at
> a human pace, and accept the risk before pointing this at anything that matters.

## Why it exists

Marketplace is meant for physical goods. Ads that sell *labour* (repair, web design,
tutoring, handyman work) are the target. A report is filed through Facebook's own
menu: **More actions → Report listing → Continue → Selling or promoting restricted
items → Other restricted products → Submit**.

The hard part is not the taps — it is deciding *what is a service ad* without
reporting a laptop someone is honestly selling. Most of this code exists because
early versions did exactly that.

## Architecture

```
ADB + uiautomator dump ──► monitor.py
   • node_values / extract_listings   parse the results grid into Listing objects
   • Listing.finalise()               classify: explicit_service | likely_service |
                                      mixed_or_parts | irrelevant_or_unclear
   • normalise_title / is_junk_label  strip "Seller · " prefixes, BOM/nbsp padding,
                                      collapse one card to one listing
                                      classify.py vocabulary lives in SERVICE_PATTERNS

mass_report.py
   • on_results_page()   refuse to work unless a search really landed
   • ensure_app_ready()  wake, keep awake, foreground the app, detect a logged-out UI
   • navigate_marketplace()  fb://marketplace deep link → tab tap → BACK (last resort)
   • card_tap_points()   title → clickable container → card image, so a service whose
                         title node is not tappable still opens
   • enrich_from_detail() read the full title + description when the grid truncates
   • report()            the verified report flow + confirmation screenshot
   • skips.jsonl / decisions.jsonl   every skip and every decision, with reasons

schedule.py
   • 12h night work window, wake randomised 45–80 min after it opens (deterministic
     per calendar day), minimum spacing between sweeps
```

## Classification, in one paragraph

A listing is a service when its **own title or description offers labour**: an action
word (`repair`, `handyman`, `cleaning`, `development`), a service phrase
(`Webflow expert`, `Wix website design`), or explicit offer language (`DM me`, `$30/hr`,
`starting at $…`, `I can/will`). A **bare device word is never evidence** — "iPhone LCD
screen" is goods. A **goods title gets a high bar**: product nouns, brand names and
model numbers make it skipped unless its own title names a service. And a paid
placement is never reported: sponsored cards are detected on the grid *and* re-checked
on the detail page.

## Requirements

- Android phone with USB debugging, `adb` on the host
- `tesseract` (OCR) and Python 3.11+
- A logged-in Facebook session on the phone (the tool never types credentials)

## Setup

```bash
git clone <this repo> && cd marketplace-monitor
python3 -m unittest discover -s tests      # 149 tests, no device required
```

Configuration is entirely environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `MMC_FB_PACKAGE` | `com.facebook.katana` | target app package |
| `MMC_WINDOW_OPEN_HOUR` | `20` | nominal night window open (local time) |
| `MMC_WINDOW_HOURS` | `12` | work window length |
| `MMC_WAKE_MIN_MIN` / `MMC_WAKE_MIN_MAX` | `45` / `80` | randomised wake offset after the window opens |
| `MMC_MIN_GAP_MIN` | `60` | minimum minutes between sweeps |
| `OPENCODE_BIN` / `OPENCODE_MODEL` | `~/.opencode/bin/opencode` / `mimo-v26-9b-mtp` | researcher job |
| `MARKETPLACE_KEY_FILE` | `~/.secrets/api.txt` | optional secret file (never committed) |
| `MMC_SKIP_CITIES` | *(empty)* | comma-separated bare city names to skip (e.g. your own metro area) |

## Running

```bash
# one sweep, all terms, uncapped
python3 mass_report.py --max-terms 0 --max-reports 0

# what would the scheduler do right now?
python3 schedule.py

# through the scheduler (locks, phone handover, night gate)
./run_mass_report_cron.sh
```

cron:

```cron
*/15 * * * * /path/to/marketplace-monitor/run_opencode_2h.sh
*/15 * * * * /path/to/marketplace-monitor/run_mass_report_cron.sh
```

The 15-minute entries are only heartbeats — `schedule.py` decides whether a tick may
actually drive the phone.

## Output (never committed)

```
data/read-only/report.sqlite3   dedup table: one row per reported listing
data/read-only/<fp>.png         confirmation screenshot per report
data/read-only/pages/*.png      one screenshot per search page  ← the audit trail
data/read-only/skips.jsonl      every skip: term, page, title, veto reason
data/read-only/decisions.jsonl  every report/skip/error: classification, signals
data/mass-report.log            cycle summaries, one JSON line per cycle
```

Useful queries:

```bash
sqlite3 data/read-only/report.sqlite3 'SELECT COUNT(*) FROM reported;'
python3 - <<'PY'
import json, collections
rows = [json.loads(l) for l in open('data/read-only/skips.jsonl')]
print(collections.Counter(r['reason'] for r in rows))          # why things were spared
print(sum(1 for r in rows if r['serviceish']))                 # over-skip canary
PY
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FacebookLoginRequired` | the visible UI is a real logged-out screen (detected from text, not the activity name). Sign in once; the next sweep resumes |
| `could not reach a Marketplace results surface` | the app opened somewhere unusual. `fb://marketplace` is tried first; check `adb shell dumpsys window | grep mCurrentFocus` |
| every term fails with a `uiautomator dump` error | the app was animating. `read_ui()` retries three times; if it persists the app is wedged |
| `min gap 60m not elapsed` / `12h work window finished` | the scheduler is sleeping — that is the intended night-only behaviour |
| `detail did not open` | the card had no tappable target; `card_tap_points()` is the place to extend |

## Privacy

`data/` is git-ignored and must stay that way: it contains screenshots of a real
account plus seller contact details scraped from public listings. The committed tree
contains no device ids, no absolute home paths, no tokens and no location
fingerprint. Location headers are skipped generically (`City, ON` and `City \u00b7 12 km`
patterns); bare city names are operator config via `MMC_SKIP_CITIES`.
