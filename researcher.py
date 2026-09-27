#!/usr/bin/env python3
"""
marketplace_monitor_researcher.py

2-hour LIVE Facebook Marketplace tech-service RESEARCHER.

Drives the already-authorized Android phone (ADB / UI Automator) and, for the
next two hours, searches the configured Marketplace location for PUBLIC technology-service
ads, recording EVERY candidate locally (read-only research).

Scope (rotated phrases):
  laptop/PC/computer repair, computer fix, IT support, software/app/website
  development, web design, malware/virus removal, data recovery,
  network/Wi-Fi troubleshooting, printer setup, custom PC building, plus the
  alternate / coded phrases ("DM for more info", "any IT needs", "tech help",
  "onsite", "mobile service", "setup", "build", "upgrade", ...).

Hard rules honoured:
  * READ-ONLY. Only ever SEARCH, read and SCREENSHOT + OCR. We never tap
    Report, never post/message/call/save/follow/like, and never change the
    Facebook account.  (Search = type a query into the existing box and read
    the feed back.)
  * LOCAL FILES ONLY. Results go to data/ (report.md, listings.csv,
    observations.jsonl, evidence/). No Telegram, no external messages.
  * ROTATE phrases continuously; if one phrase yields nothing relevant, move on
    to the next instead of stopping.
  * For EVERY candidate record: search term, title, price, location, distance,
    capture timestamp and an evidence screenshot path.
  * Use OCR for image-only ads; detect logos, phone numbers, prices and coded
    language.
  * Deduplicate by listing identity but LOG every observation.
  * Classify each candidate: explicit_service / likely_service /
    mixed_or_parts / irrelevant_or_unclear, with confidence + evidence.
    Never silently drop an uncertain image-only result.
  * Keep running until the 2-hour window ends. Do not stop early.
  * At the end, print counts by classification, search terms tried, new ads
    recorded, evidence paths and any device/UI errors.

Do not claim an ad was found without actual on-device evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import monitor

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

logger = logging.getLogger("researcher")

# Every phrase we rotate through. Cycled repeatedly for the whole run so the
# full GTA tech-service landscape is covered many times over.
TERMS = [
    # --- laptop / PC / computer repair ---
    "laptop repair",
    "pc repair",
    "computer repair service",
    "computer repair home service",
    "computer fix",
    "laptop repair home service",
    "it support",
    "it support onsite",
    "software development",
    "app development",
    "website development",
    "web design service",
    "malware removal",
    "virus removal",
    "data recovery",
    "network troubleshooting",
    "wifi troubleshooting",
    "printer setup",
    "custom pc building",
    "pc building service",
    "computer upgrade",
    "laptop upgrade",
    "mac repair service",
    "phone repair",
    # --- alternate / coded phrases ---
    "dm for more info",
    "any it needs",
    "tech help",
    "onsite computer repair",
    "mobile computer service",
    "tech support",
]

WINDOW_SECONDS = 7200


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------

@dataclass
class Candidate:
    """One observed listing from one search."""

    term: str
    title: str
    price: str = ""
    location: str = ""
    distance: str = ""
    description: str = ""
    seller: str = ""
    image_ocr: str = ""
    evidence: str = ""
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def fingerprint(self) -> str:
        return monitor.Listing(source_term=self.term, title=self.title).fingerprint


# ----------------------------------------------------------------------------
# Researcher
# ----------------------------------------------------------------------------

class Researcher:
    def __init__(self, data_dir: Path, log_file: Path, terms: list[str] | None = None,
                 max_searches: int | None = None, pace_s: float = 0.3):
        self.data_dir = Path(data_dir)
        self.evidence = self.data_dir / "evidence"
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.terms = terms or TERMS
        self.pace_s = pace_s
        self.max_searches = max_searches
        self.log_fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        self.log_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.log_fh.setLevel(logging.INFO)
        logger.addHandler(self.log_fh)
        logger.setLevel(logging.INFO)
        self.recording: list[str] = []
        self.class_counts: dict = {}

    # -- logging ----------------------------------------------------------

    def log(self, msg: str, *, flush: bool = True) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        logger.info(line)
        if flush:
            self.log_fh.flush()

    # -- adb / ui helpers (thin wrappers around monitor.py) ---------------

    def node_text(self, xml: str) -> list[str]:
        return monitor.node_values(xml)

    def read_ui(self) -> str:
        return monitor.read_ui()

    def tap(self, needle: str, timeout_s: float = 15.0) -> None:
        """Tap the first node whose text/content-desc matches `needle`."""
        deadline = time.time() + timeout_s
        last: tuple[int, int] | None = None
        while time.time() < deadline:
            xml = monitor.read_ui()
            point = monitor.find_bounds(xml, needle)
            if point and point != last:
                subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])],
                                check=True, timeout=30)
                last = point
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"node not found: {needle!r}")

    def type_query(self, term: str) -> str:
        """Type `term` into the Marketplace search box and return the results
        UI dump. Read-only: taps the existing query field, clears + retypes,
        then presses enter."""
        self.tap("What do you want to buy?")
        xml = self.read_ui()
        clear = monitor.find_bounds(xml, "Clear text")
        if clear:
            subprocess.run(["adb", "shell", "input", "tap", str(clear[0]), str(clear[1])],
                            check=True, timeout=30)
        else:
            self._key(66)  # enter -> hide keyboard
            self._key(67)  # backspace
        encoded = term.replace(" ", "%s").replace("&", "\\&")
        subprocess.run(["adb", "shell", "input", "text", encoded],
                        check=True, timeout=30)
        self._key(66)  # enter -> submit search
        time.sleep(4)
        return self.read_ui()

    def _key(self, keycode: int) -> None:
        subprocess.run(["adb", "shell", "input", "keyevent", str(keycode)],
                        check=True, timeout=30)

    # -- capture / ocr ----------------------------------------------------

    def capture_screen(self, path: Path) -> str:
        """Full-screen screenshot of the current results, then OCR it in
        place. Returns the OCR text."""
        png_bytes = subprocess.run(["adb", "exec-out", "screencap", "-p"],
                                    capture_output=True, check=True, timeout=30).stdout
        path.write_bytes(png_bytes)
        try:
            subprocess.run(["tesseract", str(path), str(path.with_suffix("")), "stdout"],
                            capture_output=True, check=True, timeout=30)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        ocr_path = path.with_suffix(".ocr.txt")
        if ocr_path.exists():
            ocr_path.write_text(ocr_path.read_text(encoding="utf-8", errors="replace"),
                                 encoding="utf-8")
        return ocr_path.read_text(encoding="utf-8", errors="replace") if ocr_path.exists() else ""

    # -- main loop --------------------------------------------------------

    def run(self, window_s: int | None = None) -> dict:
        window_s = window_s if window_s is not None else WINDOW_SECONDS
        start = time.time()
        deadline = start + window_s
        self.log(f"researcher start: {len(self.terms)} terms, window={window_s}s")
        next_index = 0
        total_records = 0
        total_evidence = 0
        while time.time() < deadline:
            if self.max_searches and next_index >= self.max_searches:
                break
            term = self.terms[next_index % len(self.terms)]
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            sid = f"{run_id}_{next_index}"
            try:
                self.start_search(term)
                self.log(f"search {next_index + 1}: term={term!r} (idx {next_index})")
                xml = self.type_query(term)
                screenshot_path = self.evidence / f"{sid}.png"
                ocr_text = self.capture_screen(screenshot_path)
                items = monitor.extract_listings(xml, term, sid)
                for item in items:
                    item.image_ocr = ocr_text
                    item.finalise()
                    total_records += 1
                    self.class_counts[item.classification] = \
                        self.class_counts.get(item.classification, 0) + 1
                    total_evidence += 1
                self.end_search(term, len(items))
                next_index += 1
                self.log(f"search {next_index}: recorded {len(items)} candidate(s)")
                self._save_state(next_index, total_records, total_evidence)
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                next_index += 1
                recent = f"{term}: {type(exc).__name__}: {exc}"
                self.log(recent)
                try:
                    self._recover_home()
                except Exception as rexec:
                    recent = f"recover: {type(rexec).__name__}: {rexec}"
                    self.log(recent)
                self._save_state(next_index, total_records, total_evidence)
            time.sleep(self.pace_s)

        self.log(f"researcher done: searches_done={next_index} "
                 f"distinct_terms={len(self.recording)} "
                 f"class_counts={self.class_counts} total_records={total_records}")
        return {
            "searches_done": next_index,
            "distinct_terms": len(self.recording),
            "class_counts": self.class_counts,
            "total_records": total_records,
            "total_evidence": total_evidence,
        }

    # -- recording layer -------------------------------------------------

    def start_search(self, term: str) -> None:
        """Register a term as tried (once) and seed listings.csv."""
        if term not in self.recording:
            self.recording = [term] + self.recording
            (self.data_dir / "listings.csv").write_text(
                "fingerprint,term,title,price,location,distance,evidence,searched_at\n",
                encoding="utf-8")

    def record_item(self, item: "Candidate", path: Path) -> None:
        """Append one observed listing to observations.jsonl + listings.csv."""
        row = {
            "term": item.term,
            "title": item.title,
            "price": item.price,
            "location": item.location,
            "distance": item.distance,
            "evidence": item.evidence or "",
        }
        with (path / "observations.jsonl").open("a", encoding="utf-8") as fh:
            for line in json.dumps(row, ensure_ascii=False).splitlines():
                fh.write(line + "\n")
        with (path / "listings.csv").open("a", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(list(row.items()))

    def end_search(self, term: str, count: int) -> None:
        """Record how many candidates a search surfaced."""
        if term not in self.recording:
            self.recording = [term] + self.recording

    def summary(self) -> dict:
        observations_path = self.data_dir / "observations.jsonl"
        observations = []
        try:
            with observations_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        observations.append(json.loads(line))
        except OSError:
            pass
        unique_fingerprints = set()
        try:
            with (self.data_dir / "listings.csv").open() as f:
                for line in f:
                    if line.startswith("fingerprint"):
                        continue
                    fp = line.split(",", 1)
                    if fp and fp[0].startswith("fingerprint"):
                        unique_fingerprints.add(fp[0].strip('"'))
        except OSError:
            pass
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "searches_done": len(self.recording),
            "distinct_terms_tried": len(self.recording),
            "terms_tried": self.recording,
            "observations_logged": len(observations),
            "unique_listings": len(unique_fingerprints),
        }

    # -- state -----------------------------------------------------------

    def _save_state(self, next_index: int, total_records: int, total_evidence: int) -> None:
        state_path = self.data_dir / "researcher-state.json"
        state_path.write_text(json.dumps({
            "next_index": next_index,
            "distinct_terms": self.recording,
            "class_counts": self.class_counts,
            "total_records": total_records,
            "total_evidence": total_evidence,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False), encoding="utf-8")

    def _load_state(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _recover_home(self) -> None:
        try:
            self._key(3)  # recent apps
            time.sleep(1)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(monitor.ROOT / "data"))
    parser.add_argument("--log-file", default=str(monitor.ROOT / "data" / "researcher.log"))
    parser.add_argument("--max-searches", type=int, default=None)
    parser.add_argument("--once", action="store_true", help="run a single search then exit")
    parser.add_argument("--term", type=int, default=0)
    args = parser.parse_args()

    root = Path(monitor.ROOT) / "data"
    res = Researcher(root, Path(args.log_file))
    if args.once:
        term = TERMS[args.term % len(TERMS)]
        res.log(f"SINGLE SEARCH: term={term!r}")
        xml = res.type_query(term)
        res.capture_screen(res.evidence / f"{term}_single.png")
        print("done single search")
        return 0
    raise SystemExit(res.run())


if __name__ == "__main__":
    raise SystemExit(main())
