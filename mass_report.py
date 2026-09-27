from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import monitor

Listing = monitor.Listing

READONLY = Path(monitor.ROOT).resolve() / "data" / "read-only"
READONLY.mkdir(parents=True, exist_ok=True)
DB_PATH = READONLY / "report.sqlite3"
PAGES_DIR = READONLY / "pages"
DECISIONS_PATH = READONLY / "decisions.jsonl"
SKIPS_PATH = READONLY / "skips.jsonl"

# Verified live against the Facebook app (2026-09-25) on an authorized device.
# There is NO "selling services" reason anywhere in the report UI; the closest
# match to "selling services is not allowed" is:
#   Selling or promoting restricted items -> Other restricted products
REPORT_FLOW = [
    "More actions",                       # overflow menu on the listing detail page
    "Report listing",                     # menu entry: "Report a listing to Facebook for review."
    "Continue",                           # "Reporting prohibited content helps keep our community safe."
    "Selling or promoting restricted items",  # "Why are you reporting this listing?"
    "Other restricted products",          # "What is being sold or promoted?"
    "Submit",                             # "You're about to submit a report" confirmation
]
# The full flow shows "Thanks for reporting"; the one-tap reason paths show
# "Thanks for letting us know." - accept either.
CONFIRM_MARKERS = ("Thanks for reporting", "Thanks for letting us know")
RESULTS_MARKERS = ("Filters", "Save this search")
STEP_SLEEP_S = 1.6
CONFIRM_TIMEOUT_S = 8.0
DETAIL_TIMEOUT_S = 5.0
MAX_BACKS = 6
PAGES_PER_TERM = 3  # initial results screen + 2 scroll pages


class ReportFlowError(RuntimeError):
    """A step of the verified report flow could not be completed."""


@dataclass
class Reported:
    fingerprint: str
    term: str
    title: str
    location: str = ""
    distance: str = ""
    price: str = ""
    reported_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    evidence: str = ""


FB_PACKAGE = os.environ.get("MMC_FB_PACKAGE", "com.facebook.katana")
FB_ACTIVITY = f"{FB_PACKAGE}/.MainActivity"
# Direct route into the Marketplace surface. Verified live on this build:
#   am start -a android.intent.action.VIEW -d fb://marketplace
# lands on "Search Marketplace" / "Filters". The fbmarket:// scheme does NOT
# resolve here, and BACK from an immersive Reels/story task exits the app.
MARKETPLACE_URI = "fb://marketplace"
MARKETPLACE_INTENT = [
    "adb", "shell", "am", "start",
    "-n", f"{FB_PACKAGE}/.IntentUriHandler",
    "-a", "android.intent.action.VIEW",
    "-d", MARKETPLACE_URI,
]


class DeviceUnavailable(RuntimeError):
    """No usable phone on ADB (unplugged, charging, offline, unauthorised)."""


class AppNotReady(RuntimeError):
    """The Facebook app could not be brought to the foreground."""


DEVICE_MISSING_MARKERS = (
    "no devices/emulators found", "device not found", "device offline",
    "device unauthorized", "no devices found", "not found",
)


def _raise_for_missing_device(exc: Exception) -> None:
    """Turn an adb 'no such device' failure into a clean, explainable error."""
    detail = f"{getattr(exc, 'stderr', '') or ''} {exc}".lower()
    if any(marker in detail for marker in DEVICE_MISSING_MARKERS):
        raise DeviceUnavailable(
            f"no usable phone on adb ({getattr(exc, 'stderr', '') or exc}). "
            "Plug the phone in, enable USB debugging and accept the RSA prompt."
        ) from exc
    raise exc


class FacebookLoginRequired(RuntimeError):
    """Facebook is showing a login screen - a human has to sign in again."""


def _adb_shell(*args: str) -> str:
    proc = subprocess.run(["adb", "shell", *args], check=True, capture_output=True,
                          text=True, timeout=30)
    return proc.stdout or ""


def _launch_fb(shell=None) -> None:
    """Bring the Facebook app forward using its real launcher activity.

    Naming ".MainActivity" fails on some builds (exit 1), so the launcher
    intent is resolved first and monkey is the fallback.
    """
    shell = shell or _adb_shell
    try:
        out = shell("cmd", "package", "resolve-activity", "--brief",
                    "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER", FB_PACKAGE)
        activity = (out or "").strip().splitlines()[-1].strip()
        if "/" in activity:
            shell("am", "start", "-n", activity)
            return
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    shell("monkey", "-p", FB_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")


# Strong, unambiguous logged-out markers. A single generic word is never
# enough: the feed legitimately contains words like "password" or "log in".
LOGIN_UI_STRONG = (
    "create new account", "log in to facebook", "email or phone number",
    "new to facebook", "forgot password", "remember me", "log in with phone number",
)


def login_screen_visible(xml: str) -> bool:
    """True only when the visible UI is a real logged-out screen."""
    values = [v.strip().lower() for v in monitor.node_values(xml or "")]
    if any(any(marker == v for marker in LOGIN_UI_STRONG) or v.startswith("create new account")
           for v in values):
        return True
    joined = " ".join(values)
    return ("log in" in joined and "password" in joined and "create new account" in joined)


def login_required_from_focus(info: str) -> bool:
    """Deliberately always False.

    Facebook names an authenticated task's base activity "LoginActivity", so
    the activity name can never be used to infer a logged-out session - that
    inference once aborted every sweep on a perfectly healthy account.
    """
    return False


def ensure_app_ready(shell=None, focus=None, ui_probe=None, sleep_fn=time.sleep) -> str:
    """Make the phone usable for a sweep, whatever state it was left in.

    Cron can fire with the phone on the home screen, screen off, or the app
    closed. This wakes it, keeps it awake for the whole cycle, foregrounds the
    app, and fails loudly on a logged-out session instead of letting every
    term die with "search field not found".
    """
    shell = shell or _adb_shell
    focus = focus or (lambda: shell("dumpsys", "window"))
    ui_probe = ui_probe or monitor.read_ui
    try:
        shell("input", "keyevent", "KEYCODE_WAKEUP")
        shell("svc", "power", "stayon", "true")
    except (OSError, subprocess.SubprocessError) as exc:
        _raise_for_missing_device(exc)
    try:
        shell("wm", "dismiss-keyguard")
    except (OSError, subprocess.SubprocessError):
        pass
    info = focus()
    if FB_PACKAGE not in info:
        try:
            _launch_fb(shell)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppNotReady(f"could not launch {FB_PACKAGE}: {exc}") from exc
        sleep_fn(4)
        info = focus()
    if FB_PACKAGE not in info:
        raise AppNotReady(f"could not foreground {FB_PACKAGE}")
    try:
        logged_out = login_screen_visible(ui_probe())
    except (OSError, subprocess.SubprocessError, RuntimeError):
        logged_out = False
    if logged_out:
        # one retry: the launcher intent sometimes shows the login screen even
        # while a saved session is still valid
        try:
            _launch_fb(shell)
        except (OSError, subprocess.SubprocessError):
            pass
        sleep_fn(4)
        if login_screen_visible(ui_probe()):
            raise FacebookLoginRequired(
                "Facebook shows a logged-out screen: sign in once on the phone, "
                "then the next cron sweep resumes automatically"
            )
    return info


def _tap_label(xml: str, label: str) -> bool:
    point = _find_exact(xml, label) or _find_node(xml, label, exact=False)
    if not point:
        return False
    subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])],
                   check=True, timeout=30)
    time.sleep(2.5)
    return True


FB_SURFACE_HINTS = ("facebook", "search", "marketplace", "message", "home",
                    "friends", "watch", "menu", "create story", "reels")


def _looks_like_facebook(xml: str) -> bool:
    """Cheap check that the dump really is the Facebook app and not the launcher."""
    values = [v.lower() for v in monitor.node_values(xml or "")]
    return any(any(hint in v for hint in FB_SURFACE_HINTS) for v in values)


def navigate_marketplace(max_back: int = 2) -> None:
    """Bring up a Marketplace results surface, whatever screen we started on.

    Primary route is the fb://marketplace deep link (works from the launcher, a
    cold app, the feed, and immersive Reels/story viewers). If that fails we tap
    the Marketplace tab, and only then unwind with BACK - pressing BACK from a
    Reels task leaves the app entirely, so it is the last resort.
    """
    ensure_app_ready()
    for attempt in range(max_back + 2):
        xml = monitor.read_ui()
        if _find_exact(xml, "Search Marketplace"):
            return
        try:
            subprocess.run(MARKETPLACE_INTENT, check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(3)
        xml = monitor.read_ui()
        if _find_exact(xml, "Search Marketplace"):
            return
        if not _looks_like_facebook(xml):
            try:
                _launch_fb()
            except (OSError, subprocess.SubprocessError) as exc:
                raise AppNotReady(f"could not relaunch Facebook: {exc}") from exc
            time.sleep(3)
            continue
        for label in ("Marketplace", "Marketplace tab"):
            if _tap_label(xml, label) and _find_exact(monitor.read_ui(), "Search Marketplace"):
                return
        if attempt >= max_back:
            break
        try:
            subprocess.run(["adb", "shell", "input", "keyevent", "4"], check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppNotReady(f"could not navigate to Marketplace: {exc}") from exc
        time.sleep(2)
    raise AppNotReady("could not reach a Marketplace results surface")


def _find_node(xml: str, needle: str, exact: bool = True) -> tuple[int, int] | None:
    """Match a node by its text/content-desc value, unescaping entities first.

    The UI dump encodes emoji as numeric entities (&#128187;) while
    node_values() unescapes them - so titles from node_values() never match
    raw XML unless we unescape here.  Both attributes are checked per node.
    Matching stays case-sensitive so "Submit" cannot hit the heading
    "You're about to submit a report".
    """
    for tag in re.finditer(r"<(?:node|view)[^>]*>", xml):
        source = tag.group(0)
        bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', source)
        if not bounds:
            continue
        for attr in re.finditer(r'(?:text|content-desc)="([^"]*)"', source):
            value = html.unescape(attr.group(1))
            if (value == needle) if exact else (needle in value):
                x1, y1, x2, y2 = map(int, bounds.groups())
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def _find_exact(xml: str, needle: str) -> tuple[int, int] | None:
    return _find_node(xml, needle, exact=True)


def _tap_step(step: str) -> None:
    point = _find_exact(monitor.read_ui(), step)
    if not point:
        raise ReportFlowError(f"step not found: {step!r}")
    subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])], check=True, timeout=30)
    time.sleep(STEP_SLEEP_S)


NODE_TAG_RE = re.compile(r"<(?:node|view)([^>]*)>")
BOUNDS_RE = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def _centre(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = rect
    return (x1 + x2) // 2, (y1 + y2) // 2


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int], slack: int = 0) -> bool:
    return (outer[0] - slack <= inner[0] and outer[1] - slack <= inner[1]
            and outer[2] + slack >= inner[2] and outer[3] + slack >= inner[3])


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _area(rect: tuple[int, int, int, int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def card_tap_points(xml: str, title: str) -> list[tuple[int, int]]:
    """Ordered tap targets for a result card: title, then the card itself.

    Titles are often emitted as plain text runs that are not tappable, which
    made `_open_listing` give up and lose the service.  We therefore also
    offer the innermost clickable/containing container and, for image-first
    cards, the card's image node - but only nodes that geometrically contain
    or overlap the title rect, so we never tap a neighbouring card.
    """
    nodes: list[tuple[tuple[int, int, int, int], bool, str]] = []
    exact_rect: tuple[int, int, int, int] | None = None
    loose_rect: tuple[int, int, int, int] | None = None
    for tag in NODE_TAG_RE.finditer(xml):
        attrs = tag.group(1)
        bounds = BOUNDS_RE.search(attrs)
        if not bounds:
            continue
        rect = tuple(int(v) for v in bounds.groups())
        clickable = 'clickable="true"' in attrs
        values = [html.unescape(v) for v in re.findall(r'(?:text|content-desc)="([^"]*)"', attrs)]
        joined = " ".join(values)
        nodes.append((rect, clickable, joined))
        if not values:
            continue
        if exact_rect is None and any(v == title for v in values):
            exact_rect = rect
        elif (loose_rect is None and title and title in joined
              and not joined.lower().startswith("seller image for")):
            loose_rect = rect
    title_rect = exact_rect or loose_rect
    # no title rect -> refuse to guess. Tapping an arbitrary card could open
    # (and report!) the wrong listing, which is worse than skipping.
    if title_rect is None:
        return []
    points: list[tuple[int, int]] = [_centre(title_rect)]
    containers = [n for n in nodes if _area(n[0]) > _area(title_rect) and _contains(n[0], title_rect, slack=8)]
    containers.sort(key=lambda n: (_area(n[0]), not n[1]))
    points += [_centre(rect) for rect, _c, _t in containers[:3]]
    # card image: only when it is provably inside the same card container
    for card in containers[:1]:
        for rect, _c, text in nodes:
            if text.lower().startswith("seller image for") and _contains(card[0], rect):
                points.append(_centre(rect))
                break
    seen: set[tuple[int, int]] = set()
    ordered = [p for p in points if not (p in seen or seen.add(p))]
    return ordered


DETAIL_NOISE_RE = re.compile(
    r"(?:\b\d+\s+of\s+\d+\b|product\s+image|see\s+more|show\s+more|more\s+actions|"
    r"seller\s+image|related\s+to|hide\s+these|save\s+this\s+search|\bfilters\b|"
    r"\bjust\s+updated\b|\bjust\s+listed\b|\bprice\s+drop\b)", re.I)
TRUNCATION_MARK = "\u2026"
# stems that survive FB's "…" truncation, so a clipped service title is still
# worth opening: the detail page carries the full title + description
TRUNCATED_SERVICE_STEM = re.compile(
    r"\b(?:rep|fix|serv|clean|hand|build|design|set\s?up|setup|help|dev|prog|install|"
    r"teach|tutor|coach|manage|print|engrav|weld|plumb|electr|paint|mov|landscap|"
    r"photograph|videograph|edit|translat|bookkeep|account|resume|cv|market|seo)\w*\b", re.I)


def parse_detail(xml: str) -> tuple[str, str]:
    """Read the full title and description from an opened listing detail page.

    Results grid truncates long titles ("...And R\u2026") which hides the service
    words; the detail page has the whole title plus the seller's description.
    """
    values = monitor.node_values(xml)
    if not values:
        return "", ""
    title = next((v for v in values if not monitor.is_junk_label(v)
                  and not DETAIL_NOISE_RE.search(v)
                  and len(v) > 3 and not re.fullmatch(r"\$?[\d.,]+", v)
                  and not re.search(r"\b\d+\s*km\b", v, re.I)), "")
    description = ""
    for value in values:
        if value == title or len(value) < 40:
            continue
        if (monitor.is_junk_label(value) or DETAIL_NOISE_RE.search(value)
                or re.search(r"\b\d+\s*km\b|\b(?:19|20)\d{2}\b", value)):
            continue
        if len(value) > len(description) and " " in value:
            description = value
    return title, description[:1000]


def enrich_from_detail(item: monitor.Listing) -> bool:
    """Open the listing, read full title + description, re-classify.

    Returns True when the detail page made it reportable. Any device error
    leaves the item untouched so the caller can just skip it.
    """
    try:
        _open_listing(item.title)
        full_title, description = parse_detail(monitor.read_ui())
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
        try:
            _back_to_results()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        return False
    if full_title and TRUNCATION_MARK in (item.title or ""):
        item.title = full_title
    item.description = description
    item.finalise()
    return is_reportable(item)


def _code_fingerprint() -> str:
    """Which code is actually running - settles stale-module confusion fast."""
    import hashlib
    parts = []
    for path in (Path(__file__), Path(monitor.__file__)):
        try:
            parts.append(f"{path.name}={hashlib.sha256(path.read_bytes()).hexdigest()[:10]}")
        except OSError:
            parts.append(f"{path.name}=?")
    return " ".join(parts)


def _say(message: str) -> None:
    """Print progress without ever breaking the run.

    A closed stdout (cron pipe gone) raised BrokenPipeError out of `print`,
    which the report loop caught and logged as a report error.
    """
    try:
        print(message, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


RESULTS_PAGE_MARKERS = ("Filters", "Save this search", "Sort by", "Location filter")
def cycle_terms(start: int) -> list[str]:
    """Search order for one cycle: website builders first, then the rest.

    Website-builder services are the priority target, so they must not be
    starved by the 20 tech-repair terms; rotation is still respected because
    `start` is applied inside each group.
    """
    terms = monitor.TERMS
    n = len(terms)
    website = [t for t in terms if t in monitor.WEBSITE_BUILDER_TERMS]
    other = [t for t in terms if t not in monitor.WEBSITE_BUILDER_TERMS]
    rotate = lambda group: group[start % len(group):] + group[:start % len(group)] if group else []
    return rotate(website) + rotate(other)


ENRICH_PER_PAGE = 3


def on_results_page(xml: str) -> bool:
    """True when the search actually landed on a results list.

    Without this guard a failed search leaves the phone on the landing page and
    its chrome ("Navigate to Search", "Browse") is extracted as if it were
    listings - which is how UI labels got reported.
    """
    values = set(monitor.node_values(xml))
    return any(marker in values for marker in RESULTS_PAGE_MARKERS)


def should_enrich(item: monitor.Listing, used: int) -> bool:
    """Detail-page enrichment is expensive, so it is budgeted per page."""
    return used < ENRICH_PER_PAGE and needs_detail(item)


def needs_detail(item: monitor.Listing) -> bool:
    """Should we open this listing to read the full title/description?

    Yes for truncated titles (the visible part is incomplete) and for mixed
    listings that show a service word - not for clear products.
    """
    title = (getattr(item, "title", "") or "").strip()
    if not title:
        return False
    if TRUNCATION_MARK in title:
        return True
    classification = getattr(item, "classification", "irrelevant_or_unclear")
    if classification == "mixed_or_parts" and TRUNCATED_SERVICE_STEM.search(title):
        return True
    return False


def detail_is_sponsored(xml: str) -> bool:
    """True when the opened listing shows a paid-placement label."""
    return any(monitor.is_sponsored_label(v) for v in monitor.node_values(xml or ""))


def skip_reason(item: monitor.Listing) -> str:
    """Why was this listing not reported? (audit label, stable strings)."""
    if getattr(item, "sponsored", False):
        return "sponsored_ad"
    classification = getattr(item, "classification", "irrelevant_or_unclear")
    if classification == "mixed_or_parts":
        return "product_veto"
    if classification in ("explicit_service", "likely_service"):
        return "not_reported_but_serviceish"
    return "no_service_signal"


def is_serviceish_title(title: str) -> bool:
    """Self-audit: would a human call this a service ad?

    Used to measure over-skipping; must stay consistent with the vocabulary in
    monitor.SERVICE_PATTERNS / monitor.OFFER_PATTERNS.
    """
    low = (title or "").lower()
    # must mirror the report rule: action word or offer language, never a bare
    # device word, and never a goods ad that merely says "for parts or repair"
    if monitor.product_dominant(low):
        return False
    return bool(
        re.search(monitor.SERVICE_PATTERNS[0], low, re.I)
        or any(re.search(p, low, re.I) for p in monitor.OFFER_PATTERNS)
    )


def page_screenshot_path(run_id: str, offset: int, page: int) -> Path:
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    return PAGES_DIR / f"{run_id}_{offset}_{page}.png"


def log_decision(path: Path, item: monitor.Listing, decision: str, term: str,
                 page: int, run_id: str, note: str = "") -> dict:
    """One line per decision (report, skip or error) with the evidence behind it.

    Makes the pipeline auditable: which rule fired, on what text, and whether a
    detail page was consulted.
    """
    record = {
        "run_id": run_id,
        "term": term,
        "page": page,
        "decision": decision,
        "title": item.title,
        "classification": getattr(item, "classification", ""),
        "signals": list(getattr(item, "signals", [])),
        "serviceish": is_serviceish_title(item.title),
        "description_chars": len(getattr(item, "description", "") or ""),
        "description_excerpt": (getattr(item, "description", "") or "")[:160],
        "ocr_chars": len(getattr(item, "image_ocr", "") or ""),
        "note": note,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def log_skip(path: Path, item: monitor.Listing, term: str, page: int, run_id: str) -> dict:
    """Append one auditable skip record (so over/under-skipping is reviewable)."""
    record = {
        "run_id": run_id,
        "term": term,
        "page": page,
        "title": item.title,
        "classification": getattr(item, "classification", ""),
        "reason": skip_reason(item),
        "serviceish": is_serviceish_title(item.title),
        "ocr_excerpt": (getattr(item, "image_ocr", "") or "")[:200],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def summarise_skips(skips: list[dict]) -> dict:
    reasons: dict[str, int] = {}
    for record in skips:
        reasons[record["reason"]] = reasons.get(record["reason"], 0) + 1
    return {
        "skipped_total": len(skips),
        "skipped_but_serviceish": sum(1 for r in skips if r.get("serviceish")),
        "skips_by_reason": reasons,
    }


def _open_listing(title: str) -> None:
    """Tap a result card and wait for the detail page to render.

    Tries every card tap target in order (title text run, clickable container,
    card image) so service cards whose title node is not tappable still open.
    """
    deadline = time.time() + DETAIL_TIMEOUT_S
    attempted: set[tuple[int, int]] = set()
    while time.time() < deadline:
        xml = monitor.read_ui()
        for point in card_tap_points(xml, title):
            if point in attempted:
                continue
            attempted.add(point)
            subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])], check=True, timeout=30)
            time.sleep(2.5)
            if any("More actions" in v for v in monitor.node_values(monitor.read_ui())):
                return
        if not attempted:
            time.sleep(1)
    raise ReportFlowError(f"listing detail did not open for {title!r}")


def _wait_confirm(timeout_s: float = CONFIRM_TIMEOUT_S) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        vals = monitor.node_values(monitor.read_ui())
        if any(marker in v for marker in CONFIRM_MARKERS for v in vals):
            return
        time.sleep(1.0)
    raise ReportFlowError("confirmation screen not seen after Submit")


def _back_to_results(max_backs: int = MAX_BACKS) -> None:
    for _ in range(max_backs):
        vals = monitor.node_values(monitor.read_ui())
        if any(m in vals for m in RESULTS_MARKERS):
            return
        subprocess.run(["adb", "shell", "input", "keyevent", "4"], check=True, timeout=30)
        time.sleep(1.2)


def _store(conn: sqlite3.Connection, item: monitor.Listing) -> str:
    item.finalise()
    now = item.captured_at
    conn.execute(
        "INSERT OR REPLACE INTO reported(fingerprint,title,first_seen,last_seen,payload) VALUES(?,?,?,?,?)",
        (
            item.fingerprint,
            item.title,
            now,
            now,
            json.dumps({"term": item.source_term, "reason": "Selling or promoting restricted items -> Other restricted products"}, ensure_ascii=False),
        ),
    )
    conn.commit()
    return item.fingerprint


def _already_reported(conn: sqlite3.Connection, item: monitor.Listing) -> bool:
    item.finalise()
    return conn.execute("SELECT 1 FROM reported WHERE fingerprint=?", (item.fingerprint,)).fetchone() is not None


def is_reportable(item: monitor.Listing) -> bool:
    """Report every service ad; only pure physical products are spared.

    Per Marketplace T&C services must be advertised via paid ads, so:
      * explicit_service / likely_service -> report
      * mixed_or_parts -> report ONLY when the listing's own title or
        description contains an ACTION word (repair, service, setup,
        development, ...) - i.e. it actually pitches labor/services.
        A plain product with stray device words stays unreported.
      * irrelevant_or_unclear / pure products -> skip
    """
    item.finalise()
    if "product_dominant" in item.signals:
        return False  # sells an item, not labour
    if item.classification in ("explicit_service", "likely_service"):
        return True
    if item.classification == "mixed_or_parts":
        own_text = f"{item.title} {item.description}".lower()
        return bool(re.search(monitor.SERVICE_PATTERNS[0], own_text, re.I))
    return False


def report(conn: sqlite3.Connection, item: monitor.Listing, already_open: bool = False) -> Reported | None:
    """Report one listing through the verified flow.

    Returns a Reported record after the confirmation screen is observed,
    None if the listing was already reported in a previous run, and raises
    ReportFlowError (or an adb error) if the flow could not be completed.
    On any failure the phone is walked back to the results screen so the
    caller can continue with the next listing.
    """
    item.finalise()
    if monitor.is_junk_label(item.title):
        raise ReportFlowError(f"refusing to report UI chrome: {item.title!r}")
    if getattr(item, "sponsored", False):
        raise ReportFlowError(f"refusing to report a sponsored ad: {item.title!r}")
    if _already_reported(conn, item):
        if already_open:
            # enrichment left us on the detail page: return to results anyway
            _back_to_results()
        return None
    term = item.source_term
    title = item.title
    try:
        if not already_open:
            _open_listing(title)
        if detail_is_sponsored(monitor.read_ui()):
            # the card is a paid placement after all: leave without reporting
            _back_to_results()
            return None
        for step in REPORT_FLOW:
            _tap_step(step)
        _wait_confirm()
        monitor.capture_screenshot(READONLY / f"{item.fingerprint}.png")
        # Best-effort dismiss of the confirmation screen; the walk-back
        # below covers the case where Close is not present.
        point = _find_exact(monitor.read_ui(), "Close")
        if point:
            subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])], check=True, timeout=30)
            time.sleep(1.5)
    except (OSError, subprocess.SubprocessError, ReportFlowError):
        _back_to_results()
        raise
    _back_to_results()
    fingerprint = _store(conn, item)
    return Reported(
        fingerprint=fingerprint,
        term=term,
        title=title,
        location=item.location,
        distance=item.distance,
        price=item.price,
        evidence=f"{READONLY}/{fingerprint}.png",
    )


def _scroll_results() -> None:
    """Swipe the results feed up to reveal the next batch of listings."""
    subprocess.run(["adb", "shell", "input", "swipe", "540", "1800", "540", "700", "400"], check=True, timeout=30)
    time.sleep(1.5)


def apply_page_ocr(items: list[monitor.Listing], ocr_text: str) -> int:
    """Give every candidate on this page the page's screenshot text.

    The lazy path used to screenshot + tesseract per skipped item, i.e. ~130
    identical captures per cycle. The page screenshot is captured anyway for
    the audit trail, and its OCR sidecar is shared by every card on screen.
    """
    if not ocr_text:
        return 0
    for item in items:
        if not item.image_ocr:
            item.image_ocr = ocr_text[:1000]
            item.finalise()
    return len(items)


def page_ocr_text(path: Path) -> str:
    sidecar = path.with_suffix(".ocr.txt")
    try:
        return sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else ""
    except OSError:
        return ""


def _refresh_ocr(item: monitor.Listing) -> None:
    """Capture the current screen and OCR it into the listing (lazy path).

    Used only for candidates that are NOT already reportable from their
    title/description, so image-only service ads can still be promoted to
    likely_service while clear services skip the screenshot cost.
    """
    if not item.image_path:
        return
    path = Path(item.image_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    monitor.capture_screenshot(path)
    ocr = path.with_suffix(".ocr.txt")
    item.image_ocr = ocr.read_text(encoding="utf-8", errors="replace")[:1000] if ocr.exists() else ""
    item.finalise()


def cycle_preflight(run_id: str) -> dict | None:
    """Make the phone usable before a cycle, or explain why it cannot run.

    Returns an abort record (phone missing / app not usable / logged out) or
    None when the sweep may proceed.
    """
    try:
        ensure_app_ready()
    except (DeviceUnavailable, FacebookLoginRequired, AppNotReady) as exc:
        return {"run_id": run_id, "aborted_because": type(exc).__name__,
                "detail": str(exc), "reported": 0}
    return None


def main(max_terms: int = 0, max_reports: int = 0) -> int:
    """One cycle: nested loops over terms x scroll-pages x listings.

    max_terms=0 sweeps ALL search terms; max_reports=0 has no report cap.
    Already-reported listings are skipped via the fingerprint dedup table,
    so re-running sweeps only reports NEW service ads.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS reported(fingerprint TEXT PRIMARY KEY, title TEXT, first_seen TEXT, last_seen TEXT, payload TEXT)")
    conn.commit()
    errors: list[str] = []
    reported: list[Reported] = []
    skipped = 0
    skipped_not_service = 0
    skip_records: list[dict] = []
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _say(f"code: {_code_fingerprint()}")
    abort = cycle_preflight("preflight")
    if abort is not None:
        _say(json.dumps(abort, ensure_ascii=False))
        return 2
    state_path = READONLY / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"next_term": 0}
    start = int(state.get("next_term", 0)) % len(monitor.TERMS)
    term_count = len(monitor.TERMS) if max_terms <= 0 else min(max_terms, len(monitor.TERMS))
    stop = False
    terms_run = 0
    last_offset = 0
    order = cycle_terms(start)
    for offset in range(term_count):
        if stop:
            break
        last_offset = offset
        term = order[offset % len(order)]
        try:
            navigate_marketplace()
            monitor.search(term)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            errors.append(f"{term}: {type(exc).__name__}: {exc}")
            continue
        if not on_results_page(monitor.read_ui()):
            # the search did not land on results: retry once, then give up on
            # this term rather than reporting landing-page chrome
            try:
                navigate_marketplace()
                monitor.search(term)
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                pass
            if not on_results_page(monitor.read_ui()):
                errors.append(f"{term}: search did not reach the results page")
                continue
        terms_run += 1
        for page in range(PAGES_PER_TERM):
            if page:
                try:
                    _scroll_results()
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append(f"{term} page{page}: {type(exc).__name__}: {exc}")
                    break
            try:
                items = monitor.extract_listings(monitor.read_ui(), term, f"{run_id}_{offset}_{page}", capture=False)
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                errors.append(f"{term} page{page}: {type(exc).__name__}: {exc}")
                continue
            # one screenshot per search page -> every sweep is auditable later
            page_shot = page_screenshot_path(run_id, offset, page)
            try:
                monitor.capture_screenshot(page_shot)
            except (OSError, subprocess.SubprocessError):
                pass
            page_ocr = page_ocr_text(page_shot)
            enriched = 0
            for item in items:
                already_open = False
                if not is_reportable(item):
                    # title/description alone say "not a service" - use this
                    # page's screenshot OCR before ruling it out (image-only ads)
                    if not item.image_ocr and page_ocr:
                        item.image_ocr = page_ocr[:1000]
                        item.finalise()
                    if not is_reportable(item) and should_enrich(item, enriched):
                        # truncated or ambiguous title: read the detail page
                        # (full title + description) before ruling it out
                        enriched += 1
                        try:
                            if enrich_from_detail(item):
                                already_open = True
                            else:
                                _back_to_results()
                        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                            already_open = False
                            try:
                                _back_to_results()
                            except (OSError, subprocess.SubprocessError, RuntimeError):
                                pass
                    if not is_reportable(item):
                        skip_records.append(log_skip(SKIPS_PATH, item, term, page, run_id))
                        skipped_not_service += 1
                        continue
                try:
                    rec = report(conn, item, already_open=already_open)
                    log_decision(DECISIONS_PATH, item, "report", term, page, run_id,
                                 note="already_open" if already_open else "")
                    already_open = False
                    if rec is None:
                        skipped += 1
                    else:
                        reported.append(rec)
                        _say(f"  reported: {rec.title!r} ({rec.fingerprint})")
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                    errors.append(f"{item.title}: {type(exc).__name__}: {exc}")
                    log_decision(DECISIONS_PATH, item, "error", term, page, run_id,
                                 note=f"{type(exc).__name__}: {exc}"[:200])
                if max_reports and len(reported) >= max_reports:
                    stop = True
                    break
    # rotation: resume the interrupted term on cap-stop; nudge forward after
    # a full sweep; otherwise advance past the terms that ran
    rotated_back = order.index(order[0]) if order else 0
    rotation_base = (start + 0) % len(monitor.TERMS)
    if stop:
        resumed = (rotation_base + last_offset) % len(monitor.TERMS)
        next_term = resumed
    elif terms_run >= len(monitor.TERMS):
        next_term = (start + 1) % len(monitor.TERMS)
    else:
        next_term = (start + max(1, terms_run)) % len(monitor.TERMS)
    state_path.write_text(json.dumps({"next_term": next_term, "last_run": run_id}), encoding="utf-8")
    _say(json.dumps({"run_id": run_id, "reported": len(reported), "skipped_already_reported": skipped, "skipped_not_service": skipped_not_service, "terms_run": terms_run, "capped": stop, **summarise_skips(skip_records), "errors": errors}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-terms", type=int, default=0, help="0 = sweep ALL search terms in one cycle")
    parser.add_argument("--max-reports", type=int, default=0, help="0 = no cap on reports per cycle")
    args = parser.parse_args()
    raise SystemExit(main(args.max_terms, args.max_reports))
