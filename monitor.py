from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "monitor.sqlite3"
JSONL_PATH = DATA / "observations.jsonl"
CSV_PATH = DATA / "listings.csv"
REPORT_PATH = DATA / "report.md"
EVIDENCE = DATA / "evidence"
# Location-name nodes that are not listings. Facebook renders "City, ON" (caught
# generically by PROVINCE_SUFFIX_RE) and "City \u00b7 12 km" (caught by the distance
# rule), so this list is only for bare city names: set your own market's cities.
SKIP_CITIES = tuple(
    c.strip() for c in os.environ.get("MMC_SKIP_CITIES", "").split(",") if c.strip()
)
WEBSITE_BUILDER_TERMS = [
    "webflow expert",
    "wix website designer",
    "shopify store setup",
    "website builder service",
    "landing page design",
    "wordpress developer",
    "squarespace website",
    "seo services",
]
TERMS = [
    "laptop repair",
    "pc repair",
    "computer repair service",
    "computer fix",
    "laptop repair home service",
    "mac repair service",
    "software development",
    "app development",
    "website development",
    "web design service",
    "malware removal",
    "data recovery",
] + WEBSITE_BUILDER_TERMS
SERVICE_PHRASE_PATTERN = (
    r"(?:webflow|wix|squarespace|shopify|wordpress|web\s?builder|website\s?builder|"
    r"web\s?design|web\s?development|website\s?design|website\s?development|"
    r"ecommerce\s?(?:site|website|store)|online\s?store|landing\s?pages?|seo|"
    r"custom\s+websites?|responsive\s+design|google\s+my\s+business|web\s?presence)"
    r"[^.]{0,40}?"
    r"\b(?:design|designer|designs|build|built|builder|building|create|created|make|"
    r"setup|set\s?up|expert|specialist|developer|developers|dev|agency|studio|"
    r"freelance|freelancer|redesign|re-?design|migrate|migration|"
    r"fix|repair|seo|site|website|store|shop|landing|page|pages|help|hire|service|services)\b"
    r"|\b(?:custom|professional|affordable|budget|small\s+business|local)\b[^.]{0,24}?"
    r"\b(?:websites?|web\s?designs?|web\s?developers?|web\s?apps?|stores?)\b"
)
SERVICE_PATTERNS = [
    # offer / action words: someone is selling labour, time or know-how
    r"\b(?:repair|fix|service|services|support|maintenance|installation|install|installing|setup|set\s?up|development|developer|developing|build|building|programming|programmer|troubleshoot(?:ing)?|renovat(?:ion|e|ing)|remodel(?:ling|ing)?|contract(?:or|ing)|blueprint|architect(?:ural|s)?|permits?\b|(?:web|website|graphic|logo|interior|3d|ui|ux|video|floral|product|print)\s+design(?:er|ers)?|design\s+(?:services?|service|company|studio|agency|freelancers?|help)|handyman|handy\s?man|cleaning|cleanup|clean\s?up|deep\s?clean|car\s?detailing|pressure\s?washing|lawn|yard\s?work|landscap(?:e|ing)|junk\s?removal|mov(?:e|ing)\s?help|plumb(?:ing|er)?|electric(?:al|ian)|paint(?:ing|er)?|roof(?:ing|er)?|tutor(?:ing)?|tutoring|lesson(?:s)?|coaching|photograph(?:y|er)?|photo\s?booth|videograph(?:y|er)?|video\s?editing|photo\s?editing|graphic\s?design|transcri(?:be|ption|ptionist)|translat(?:e|ion|or)|bookkeep(?:ing|er)?|accounting|tax\s?prep|resume\s?writing|cv\s?writing|web\s?design|web\s?development|marketing|seo|social\s?media\s?management|it\s?support|tech\s?support|it\s?solutions|tech\s?solutions|managed\s?it|virtual\s?assistant|calendar\s?management|data\s?recovery|virus\s?removal|malware\s?removal|software\s?installation|engraving|engrave(?:ment|ing)?|sign\s?making|3d\s?printing|laser\s?cutting|plc\s?programming|robotics?\s?(?:build|program|project)|home\s?service|on\s?site|door\s?to\s?door|errands?|odd\s?jobs?|gutter\s?cleaning|pressure\s?washing|carpentry|renovat(?:ion|e)|assembly|furniture\s?assembly|mount(?:ing|ed)?\s?(?:tv|wall)|cabling|wiring|it\s?help|tech\s?help|computer\s?help|pc\s?help|web\s?builder|website\s?builder|landing\s?pages?|(?:build|create|make|design|fix|redesign|migrate|repair|maintain|manage)\s+(?:me\s+)?(?:a\s+|your\s+)?websites?|web\s?design|web\s?development|ecommerce\s?(?:site|website|store)|online\s?store|seo|google\s+my\s+business|web\s?presence|digital\s+branding|logo|custom\s+websites?|responsive\s+design|mobile\s?friendly|host(?:ing)?\s+(?:setup|support|migration)|domain\s+(?:setup|transfer))\b",
    # device / artefact words: the object of the work (not proof of a sale by itself)
    r"\b(?:laptop|computer|pc|desktop|mac|software|website|web|app|network|wi-?fi|printer|malware|virus|data|server|router|modem|phone|iphone|android|tv|console|camera|drone|speaker|guitar|piano|engine|tire|roof|fence|deck|painting|plumbing|electrical)\b",
]
# Offer language = the seller is advertising themselves/their labour/time.
OFFER_PATTERNS = [
    r"\b(?:i\s+will|we\s+will|i'?ll|we'?ll|i\s+can|we\s+can|i\s+do|we\s+do|do\s+you\s+need|need\s+help|i'?m\s+available|now\s+available|available\s+for)\b",
    r"\b(?:dm\s+me|message\s+me|text\s+me|call\s+me|whatsapp\s+me|reach\s+(?:me|out)|contact\s+me|inbox\s+me|email\s+me)\b",
    r"(?:starting\s+(?:at|from)|rates?\s+from|priced?\s+from|quotes?\s*[:=]?|\$[\d.,]+\s*(?:/|per\s+)\s*(?:hr|hour)|per\s+hour|hourly|\bhourly\s+rate|\$\d+\s*/\s*hr)\b",
    r"\b(?:free\s+(?:estimate|quote|consultation)|no\s+obligation|fully\s+insured|licensed\s+and\s+insured|call\s+now|book\s+now|get\s+a\s+quote|turnaround\s+time|same\s?day\s+service|will\s+do|can\s+do|any\s+(?:job|task|odd)|odd\s+jobs?)\b",
    r"\b(?:help\s+wanted|looking\s+for\s+(?:work|someone|help)|any\s+one|can\s+(?:someone|anybody)|guy(?:s)?\s+who)\b",
    r"\b(?:grow\s+your\s+business|scale\s+your\s+business|boost\s+your\s+(?:business|sales)|increase\s+your\s+(?:sales|followers))\b",
]

CODED_PATTERNS = [
    r"\b(?:dm|msg|text|call|whatsapp|inbox|reach|contact)\b",
    r"\b(?:any|all)\s+(?:it|tech|computer|repair|needs?)\b",
    r"\b(?:looking for|needed|available|open to|got work|side hustle)\b",
]
# Phrases where a service word is NOT an offer of work
CONTEXT_VETO = re.compile(
    r"\b(?:full\s+service|service\s+history|service\s+records?|service\s+record|"
    r"maintenance\s+records?|carfax|low\s+kms?|low\s+mileage|one\s+owner|"
    r"dealer\s+shipped|service\s+included|warranty\s+remaining)\b", re.I)
PRICE_RE = re.compile(r"(?:[$€£]\s?\d[\d,]*(?:\.\d{2})?|free|price on request)", re.I)
# Marketplace UI chrome: never a listing, never a report target.
JUNK_LABELS = {
    "filters", "save this search", "brampton, on", "just listed", "just updated",
    "nearby", "price drop", "back", "close", "notify me", "sponsored", "ads", "see all",
    "get notified when new listings match this criteria.",
    "get notified when new listings match this criteria",
    "related to what you viewed", "hide these listings", "know before you click",
    "deal of the day", "see more options", "message seller", "seller information",
    # landing page / search bar chrome
    "navigate to search", "search marketplace", "search", "browse", "sell",
    "notifications", "messages", "marketplace", "see all results", "sort by",
    "back to top", "log in", "create new account", "see more", "show more",
    # card action menus (these are flow buttons, never listings)
    "more actions", "message seller", "seller information", "share",
    "report listing", "hide this listing", "turn on notifications",
    "product image", "image", "photo", "video", "1 of 1", "1 of 2", "2 of 2",
}
# e.g. "Springfield, ON" - the location header of a results page, any city
PROVINCE_SUFFIX_RE = re.compile(
    r"^[A-Z][\w .'\u00e0-\u00ff-]{1,24},\s?(?:ON|ON\.|BC|AB|QC|MB|SK|NB|NL|PE|NT|NS|YT)\b")
JUNK_PREFIXES = (
    "what do you want", "clear", "search suggestion", "ad from ", "seller image for ",
    "see more options for", "get notified", "start free", "listing unavailable",
    "deal of the day", "price drop", "just updated", "see more options",
    "product image", "image", "photo", "video", "seller image",
)
PRICE_PREFIX_RE = re.compile(
    r"^(?:free|c\$?\s?\d[\d,]*(?:\.\d{2})?|[$€£]\s?\d[\d,]*(?:\.\d{2})?|price on request)\s*[·\-–]?\s+",
    re.I,
)


PRODUCT_MARKERS = [
    r"\bfor\s+parts\b", r"\bas[\s-]?is\b", r"\btool\s?kit\b", r"\bbrand\s+new\b",
    r"\bsealed\b", r"\bfor\s+sale\b", r"\bon\s+sale\b", r"\bpre[\s-]?owned\b",
    r"\bused\b", r"\bparts?\b", r"\baccessor", r"\breplacement\s+part\b",
    r"\bas\s+tested\b", r"\bb\s*o\s*d\b", r"\bfully\s+tested\b",
]
SERVICE_WORD_RE = re.compile(
    r"\b(?:services|service|support|help|assistance|repair|repairs|repairing|installation|"
    r"install|setup|development|design|cleaning|cleanups?|lessons|tutoring|coaching|"
    r"management|editing|printing|engraving|plumbing|electrical|landscaping|removal|"
    r"maintenance|troubleshooting|fix|fixes|fixed|fixing|developer|developers)\b"
    r"|(?:" + SERVICE_PHRASE_PATTERN + r")", re.I)
PLURAL_SERVICE_RE = re.compile(r"\b(?:services|service|support|assistance)\b", re.I)
# a single decisive goods marker is enough on its own
HARD_PRODUCT_RE = re.compile(
    r"\b(?:tool\s?kit|kit|for\s+parts|as[\s-]?is|brand\s+new|sealed|replacement\s+part|b\s*o\s*d"
    r"|premium\s+(?:plan|subscription|template|theme|licen[cs]e)|(?:wix|shopify|webflow|squarespace|wordpress)\s+(?:plan|subscription|licen[cs]e)|(?:repair|spare|replacement|oem|aftermarket|phone|laptop|pc)\s+parts?|replacement\s+(?:part|screen|display|battery|keyboard|charger|touch|lcd|panel|unit|kit|cable)|(?:screen|display|battery|keyboard|charger|touch|lcd)\s+replacement)\b", re.I)
PRODUCT_NOUNS = re.compile(
    r"\b(?:case|cases|cover|covers|shell|enclosure|fan|psu|gpu|cpu|cooler|motherboard|"
    r"battery|batteries|charger|cable|cables|adapter|dock|keyboard|mouse|webcam|"
    r"reel|reels|rod|rods|tackle|tent|chair|chairs|desk|desks|table|tables|couch|"
    r"mattress|stroller|printer|scanner|router|modem|monitor|keyboard|theme|themes|template|templates|plugin|plugins|extension|extensions|licence|license|subscription|plan|plans|package|packages|e-?book|template\s+pack|sodimm|dimm|\bram\b|memory\s+module|docking\s+station|\bpads?\b|\bscreens?\b|\bglass\b|\bpanels?\b|text\s?books?|handbook|study\s+guides?|manuals?)\b", re.I)
BRANDS = re.compile(
    r"\b(?:fractal|thermaltake|antec|nzxt|corsair|coolermaster|asrock|gigabyte|msi|"
    r"evga|zotac|logitech|steelseries|seagate|crucial|kingston|sandisk|wd\b|"
    r"toshiba|apple|samsung|lg|sony|bosch|makita|dewalt|milwaukee|ryobi|canon|"
    r"nikon|dyson|kohler|moen|puma|adidas|nike|xiaomi|oneplus|anker|belkin)\b", re.I)
# unambiguous offers of labour: a product word cannot veto these
STRONG_SERVICE_RE = re.compile(
    r"\b(?:services?|support|repairs?|repairing|fix|fixes|fixing|install(?:ation)?|"
    r"setup|maintenance|troubleshoot(?:ing)?|handyman|cleaning|cleanup|tutoring|"
    r"lessons|coaching|plumbing|electrical|landscaping|engraving|printing)\b", re.I)
GENERIC_SERVICE_TITLE_RE = re.compile(
    r"\b(?:i\.?\s?t\.?|tech|technician|expert|specialist|professional|pro|freelance|"
    r"consultant|help|support|service|services|repair|fix|available|mobile|onsite|on[\s-]?site)\b", re.I)
SPEC_RE = re.compile(r"\b\d+\b|\b(?:ssd|sodimm|dimm|psu|rtx|gtx|ryzen|thunderbolt|ddr\d?)\b", re.I)


def product_dominant(text: str) -> bool:
    """True when the ad sells an ITEM, even if the title also says "repair".

    "Acer ... RTX 2060 AS-IS for parts or repair" and "Precision Repair Tool Kit"
    are goods. A real service ad either offers labour in its own words
    ("Repair Services", "$30/hr", "DM me") or has no product markers at all.
    """
    low = (text or "").lower()
    if not low:
        return False
    if any(re.search(p, low, re.I) for p in OFFER_PATTERNS):
        return False  # explicit offer language wins over product wording
    phrase = re.search(SERVICE_PHRASE_PATTERN, low, re.I)
    if not SERVICE_WORD_RE.search(low) and not phrase:
        return False
    if phrase:
        return False  # "Webflow expert", "Wix website design" is labour
    if PLURAL_SERVICE_RE.search(low):
        return False  # "Repair Services" is a service pitch
    if HARD_PRODUCT_RE.search(low):
        return True
    markers = sum(1 for p in PRODUCT_MARKERS if re.search(p, low, re.I))
    if markers and (SPEC_RE.search(low) or markers >= 2):
        return True
    if STRONG_SERVICE_RE.search(low):
        return False  # real offer of labour; goods wording alone is not enough
    return bool(BRANDS.search(low) or PRODUCT_NOUNS.search(low) or markers)


SPONSORED_RE = re.compile(
    r"\bsponsored\b|\bpromoted\b|\bpaid\s+partnership\b|\bboosted\b|"
    r"^ads?$|\bad\s+from\s+seller\b|^ad$|"
    r"\bsponsoris|\bpatrocinad|\bgesponsert|\banuncios?\b|\bannonce\b", re.I)


def is_sponsored_label(text: str) -> bool:
    """True for any paid-placement label Facebook renders on a card.

    Sponsored cards must never be reported, and the label comes in many forms
    ("Sponsored", "Sponsored \u00b7", "Ad from seller", "Promoted", localized
    variants), so matching is substring based, not equality.
    """
    cleaned = normalise_title(text or "")
    return bool(cleaned) and bool(SPONSORED_RE.search(cleaned))


def is_junk_label(text: str) -> bool:
    """True for Marketplace UI chrome that must never become a listing."""
    cleaned = normalise_title(text)
    low = cleaned.lower()
    if not cleaned or low in JUNK_LABELS:
        return True
    if PROVINCE_SUFFIX_RE.match(cleaned):
        return True
    return low.startswith(JUNK_PREFIXES)


def normalise_title(text: str) -> str:
    """Strip seller/price prefixes, BOM/nbsp padding and collapse whitespace.

    FB results emit one node per text run, so a single card shows up as
    "Free · Fall cleanup" plus a bare "Fall cleanup" node. Normalising both to
    the tail after the first " · " lets card fragments collapse to one listing.
    """
    if not text:
        return ""
    cleaned = html.unescape(text).replace("\ufeff", " ").replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if " · " in cleaned:
        tail = cleaned.split(" · ", 1)[1].strip()
        if tail:
            cleaned = tail
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = PRICE_PREFIX_RE.sub("", cleaned).strip()
    return re.sub(r"\s+", " ", cleaned).strip()



@dataclass
class Listing:
    source_term: str
    title: str
    price: str = ""
    location: str = ""
    distance: str = ""
    description: str = ""
    seller: str = ""
    image_ocr: str = ""
    image_path: str = ""
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fingerprint: str = ""
    classification: str = "unknown"
    confidence: str = "low"
    signals: list[str] = field(default_factory=list)
    sponsored: bool = False

    def finalise(self) -> "Listing":
        if self.sponsored:
            # a paid placement is never a reportable service ad
            self.classification = "irrelevant_or_unclear"
            self.confidence = "low"
            if "sponsored" not in self.signals:
                self.signals.append("sponsored")
            if not self.fingerprint:
                basis = "|".join([self.title.lower(), self.location.lower(), self.distance.lower()])
                self.fingerprint = hashlib.sha256(basis.encode()).hexdigest()[:24]
            return self
        title_text = " ".join([self.title, self.description]).lower()
        own_text = title_text
        ocr_text = re.sub(re.escape(self.source_term), " ", self.image_ocr.lower())
        text = f"{title_text} {ocr_text}"
        # a service word inside "full service history" / "carfax" is not an offer
        stripped_own = CONTEXT_VETO.sub(" ", own_text)
        stripped_text = CONTEXT_VETO.sub(" ", text)
        own_hits = [p for p in SERVICE_PATTERNS if re.search(p, stripped_own, re.I)]
        own_action_raw = bool(re.search(SERVICE_PATTERNS[0], own_text, re.I))
        own_action = bool(re.search(SERVICE_PATTERNS[0], stripped_own, re.I))
        own_offer = any(re.search(p, stripped_own, re.I) for p in OFFER_PATTERNS)
        own_device = bool(re.search(SERVICE_PATTERNS[1], stripped_own, re.I))
        text_hits = [p for p in SERVICE_PATTERNS if re.search(p, stripped_text, re.I)]
        text_offer = any(re.search(p, stripped_text, re.I) for p in OFFER_PATTERNS)
        coded_hits = [p for p in CODED_PATTERNS if re.search(p, text, re.I)]
        product_like = bool(re.search(r"\b(?:for sale|for parts|clearance|we buy|buy computers|shipping|condition|accessories|brand new|parts|new|ssd|sata|precision set|ram|dimm|drive|disk|ryzen|gb|inch|inches)\b", text, re.I))
        title_product_like = bool(re.search(r"\b(?:for sale|for parts|clearance|we buy|buy computers|shipping|condition|accessories|brand new|parts|new|ssd|sata|precision set|ram|dimm|drive|disk|ryzen|gb|inch|inches|book|books|ebook|collection|guide|pdf|dvd|audiobook|kindle|novel|course)\b", title_text, re.I) or re.search(r"\b(?:hp|dell|lenovo|acer|asus)\s+laptop\b", title_text, re.I))
        brand_title = bool(re.search(r"\b(?:hp|dell|lenovo|acer|asus|macbook|macbook air)\b", self.title, re.I))
        # whole-screen OCR is shared by every card on the page, so promotion to a
        # service demands a topical, spec-free title of its own
        topical_title = bool(
            re.search(SERVICE_PATTERNS[0], self.title, re.I)
            or re.search(SERVICE_PATTERNS[1], self.title, re.I)
            or GENERIC_SERVICE_TITLE_RE.search(self.title)
        )
        spec_title = bool(SPEC_RE.search(self.title))
        # a title that is itself a part/brand/goods noun must not be promoted by
        # the *page's* OCR text (other cards' words leak in)
        goods_title = bool(HARD_PRODUCT_RE.search(self.title)
                           or PRODUCT_NOUNS.search(self.title)
                           or BRANDS.search(self.title))
        # for a GOODS title the bar is high: a real service noun in the title
        # (repair/service/handyman/cleaning...) or explicit offer language.
        # A weak verb alone (setup/build/design/development) means it is goods.
        title_service_noun = bool(SERVICE_WORD_RE.search(self.title, re.I))
        title_phrase = bool(re.search(SERVICE_PHRASE_PATTERN, self.title, re.I))
        if CONTEXT_VETO.search(own_text) and own_action_raw and not own_action:
            # "2016 Volvo ... FULL SERVICE HISTORY * CLEAN CARFAX" is a car sale:
            # every service word sat inside a non-service phrase
            self.signals.append("service_word_in_non_service_context")
        if product_dominant(own_text):
            self.classification = "mixed_or_parts"
            self.confidence = "medium"
            self.signals.append("product_dominant")
        elif goods_title and not (title_service_noun or title_phrase or own_offer):
            # goods whose detail text merely says "support"/"service" are not
            # service ads: a goods title only counts when its OWN title offers
            # work, or when the description carries explicit offer language
            self.classification = "irrelevant_or_unclear"
            self.confidence = "low"
            self.signals.append("goods_title")
        elif own_action or own_offer:
            # the listing's own title/description sells labour, time or know-how.
            # A bare device word is NOT evidence - "iPhone LCD screen" is goods.
            if title_product_like and not own_action and not own_offer:
                self.classification = "mixed_or_parts"
                self.confidence = "medium"
            else:
                self.classification = "explicit_service"
                self.confidence = "high" if (len(own_hits) >= 2 or (own_hits and own_offer)) else "medium"
        elif own_device and (product_like or title_product_like):
            self.classification = "mixed_or_parts"
            self.confidence = "medium"
        elif (re.search(SERVICE_PATTERNS[0], stripped_text, re.I) and len(text_hits) >= 2
              and topical_title and not spec_title
              and not product_like and not title_product_like and not brand_title
              and not goods_title):
            # service evidence only from the screenshot OCR
            self.classification = "likely_service"
            self.confidence = "medium"
        elif text_hits and (text_offer or product_like):
            self.classification = "mixed_or_parts"
            self.confidence = "medium"
        else:
            self.classification = "irrelevant_or_unclear"
            self.confidence = "low"
        if coded_hits:
            self.signals.append("coded_language")
        if self.image_ocr and not self.description:
            self.signals.append("image_only_or_minimal_text")
        if not self.price:
            self.signals.append("price_missing")
        if not self.fingerprint:
            basis = "|".join([self.title.lower(), self.location.lower(), self.distance.lower()])
            self.fingerprint = hashlib.sha256(basis.encode()).hexdigest()[:24]
        return self


class MonitorStore:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.data = root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.evidence = self.data / "evidence"
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.db = root / "data" / "monitor.sqlite3"
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("CREATE TABLE IF NOT EXISTS listings (fingerprint TEXT PRIMARY KEY, title TEXT, first_seen TEXT, last_seen TEXT, payload TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, fingerprint TEXT, seen_at TEXT, payload TEXT)")
        self.conn.commit()

    def save(self, item: Listing) -> bool:
        item.finalise()
        payload = json.dumps(asdict(item), ensure_ascii=False)
        now = item.captured_at
        prior = self.conn.execute("SELECT first_seen FROM listings WHERE fingerprint=?", (item.fingerprint,)).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO listings(fingerprint,title,first_seen,last_seen,payload) VALUES(?,?,?,?,?)", (item.fingerprint, item.title, prior[0] if prior else now, now, payload))
        self.conn.execute("INSERT INTO observations(fingerprint,seen_at,payload) VALUES(?,?,?)", (item.fingerprint, now, payload))
        self.conn.commit()
        with (self.data / "observations.jsonl").open("a", encoding="utf-8") as f:
            f.write(payload + "\n")
        return prior is None

    def export(self) -> list[Listing]:
        rows = self.conn.execute("SELECT payload FROM listings ORDER BY last_seen DESC").fetchall()
        return [Listing(**json.loads(row[0])) for row in rows]

    def write_reports(self, new_items: list[Listing], errors: list[str]) -> Path:
        rows = self.export()
        with (self.data / "listings.csv").open("w", newline="", encoding="utf-8") as f:
            fields = ["fingerprint", "classification", "confidence", "title", "price", "location", "distance", "description", "seller", "image_ocr", "image_path", "source_term", "captured_at", "signals"]
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))
        lines = [f"# Marketplace service-post report", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", f"Total unique listings: {len(rows)}", f"New service-related listings this run: {len(new_items)}", ""]
        for label, selected in (("NEW", new_items), ("ALL CURRENTLY KNOWN", rows)):
            lines += [f"## {label}"]
            for item in selected:
                if item.classification in {"explicit_service", "likely_service", "mixed_or_parts"}:
                    lines += [f"- **{item.title}** — {item.price or 'no price'} — {item.location or 'location unavailable'} — {item.distance or ''} — {item.classification}/{item.confidence}", f"  Description: {item.description or 'not exposed'}", f"  OCR: {item.image_ocr or 'none'}", f"  Evidence: `{item.image_path or 'none'}`", f"  Fingerprint: `{item.fingerprint}`"]
            lines.append("")
        if errors:
            lines += ["## Errors"] + [f"- {e}" for e in errors]
        path = self.data / "report.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


DUMP_PATH = "/sdcard/marketplace-monitor.xml"


def read_ui(runner=None, attempts: int = 3, sleep_fn=time.sleep) -> str:
    """uiautomator dump with a retry budget.

    The dump intermittently fails with "could not get idle state" once the app
    is animating; before the retry this killed every term of a cycle silently.
    """
    runner = runner or subprocess.run
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            runner(["adb", "shell", "uiautomator", "dump", DUMP_PATH], check=True,
                   stdout=subprocess.DEVNULL, timeout=30)
            done = runner(["adb", "shell", "cat", DUMP_PATH], check=True,
                          capture_output=True, text=True, timeout=30)
            return getattr(done, "stdout", "") or ""
        except (subprocess.SubprocessError, OSError) as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep_fn(2.0)
    assert last is not None
    raise last


def node_values(xml: str) -> list[str]:
    return [html.unescape(x) for x in re.findall(r'(?:text|content-desc)="([^"]+)"', xml) if html.unescape(x).strip()]


def capture_screenshot(path: Path) -> None:
    raw = subprocess.check_output(["adb", "exec-out", "screencap", "-p"], timeout=30)
    path.write_bytes(raw)
    try:
        subprocess.run(["tesseract", str(path), str(path.with_suffix("")), "stdout"], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        return
    text_path = path.with_suffix(".txt")
    if text_path.exists():
        path.with_suffix(".ocr.txt").write_text(text_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")


def find_bounds(xml: str, needle: str) -> tuple[int, int] | None:
    for match in re.finditer(r'<(?:node|view)[^>]*(?:text|content-desc)="[^"]*' + re.escape(needle) + r'[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml, re.I):
        x1, y1, x2, y2 = map(int, match.groups())
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def tap(node: str) -> None:
    xml = read_ui()
    point = find_bounds(xml, node)
    if point:
        subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])], check=True, timeout=30)
        time.sleep(1)


def search(term: str) -> str:
    tap("Search Marketplace")
    time.sleep(1)
    xml = read_ui()
    point = find_bounds(xml, "What do you want to buy?")
    if not point:
        raise RuntimeError("Marketplace search field was not found")
    subprocess.run(["adb", "shell", "input", "tap", str(point[0]), str(point[1])], check=True, timeout=30)
    xml = read_ui()
    clear = find_bounds(xml, "Clear text")
    if clear:
        subprocess.run(["adb", "shell", "input", "tap", str(clear[0]), str(clear[1])], check=True, timeout=30)
    else:
        subprocess.run(["adb", "shell", "input", "keyevent", "123"], check=True, timeout=30)
        subprocess.run(["adb", "shell", "input", "keyevent", "67"], check=True, timeout=30)
    encoded = term.replace(" ", "%s").replace("&", "\\&")
    subprocess.run(["adb", "shell", "input", "text", encoded], check=True, timeout=30)
    subprocess.run(["adb", "shell", "input", "keyevent", "66"], check=True, timeout=30)
    time.sleep(4)
    return read_ui()


def extract_listings(xml: str, term: str, run_id: str, capture: bool = True) -> list[Listing]:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    values = node_values(xml)
    result: list[Listing] = []
    seen_normalised: list[str] = []
    for idx, raw in enumerate(values):
        # distance nodes ("City \u00b7 5 km", "30 km") are not titles
        if re.search(r"·\s*\d+\s*km\b", raw, re.I) or re.match(r"^\d+\s*km\b", raw.strip(), re.I):
            continue
        if any(raw.startswith(city) for city in SKIP_CITIES):
            continue
        title = normalise_title(raw)
        if is_junk_label(title) or is_junk_label(raw):
            continue
        if title.lower() == term.lower() or len(title) < 5:
            continue
        low = title.lower()
        # one card -> one listing: keep the longest variant of a fragmented card
        duplicate = None
        replace_idx = None
        for kept_idx, kept in enumerate(seen_normalised):
            if kept in low:
                if len(low) > len(kept):
                    replace_idx = kept_idx
                duplicate = kept_idx
                break
            if low in kept:
                duplicate = "skip"
                break
        if replace_idx is not None:
            result[replace_idx].title = title
            seen_normalised[replace_idx] = low
            continue
        if duplicate is not None:
            continue
        if title in {v for item in result for v in (item.title, item.location, item.price)}:
            continue
        if title.startswith(("$", "·")) or any(title.startswith(city) for city in SKIP_CITIES):
            continue
        price = next((v for v in values[max(0, idx - 2):idx + 3] if v == "Free" or PRICE_RE.fullmatch(v)), "")
        location = next((v for v in values[idx:idx + 6] if re.search(r"\b\d+\s*km\b|·\s*\d+\s*km", v, re.I)), "")
        sponsored = any(is_sponsored_label(v) for v in values[max(0, idx - 4):idx]
                         if v not in {title, price, location})
        result.append(Listing(source_term=term, title=title, price=price, location=location,
                              distance=location, sponsored=sponsored))
        seen_normalised.append(low)
        if len(result) >= 12:
            break
    for item in result:
        item.image_path = str(EVIDENCE / f"{run_id}_{re.sub(r'[^a-z0-9]+', '_', item.title.lower()).strip('_')[:50]}.png")
        if not capture:
            continue
        capture_screenshot(Path(item.image_path))
        ocr = Path(item.image_path).with_suffix(".ocr.txt")
        item.image_ocr = ocr.read_text(encoding="utf-8", errors="replace")[:1000] if ocr.exists() else ""
    return result


def send_telegram(text: str) -> str:
    key_file = Path(os.environ.get("MARKETPLACE_KEY_FILE", Path.home() / ".secrets" / "api.txt"))
    token = os.environ.get("MARKETPLACE_TELEGRAM_TOKEN")
    chat_id = os.environ.get("MARKETPLACE_TELEGRAM_CHAT_ID")
    if not token:
        for line in key_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("telegram_bot_pc_opencode="):
                token = line.split("=", 1)[1].strip()
                break
    if not chat_id:
        for line in key_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("TELEGRAM_CHAT_ID="):
                chat_id = line.split("=", 1)[1].strip()
                break
    if not token or not chat_id:
        return "not-sent: Telegram credentials not found"
    payload = f"chat_id={chat_id}&text={subprocess.list2cmdline([text])}"
    result = subprocess.run(["curl", "-sS", "-X", "POST", f"https://api.telegram.org/bot{token}/sendMessage", "-d", payload], capture_output=True, text=True, timeout=30)
    return "sent" if '"ok":true' in result.stdout.replace(" ", "").lower() else f"not-sent: {result.stdout[:200]}"


def run_once(max_terms: int = 1, send: bool = True) -> tuple[int, list[Listing], list[str]]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    store = MonitorStore()
    new_items: list[Listing] = []
    errors: list[str] = []
    state_path = store.data / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"next_term": 0}
    start = int(state.get("next_term", 0)) % len(TERMS)
    for offset in range(min(max_terms, len(TERMS))):
        term = TERMS[(start + offset) % len(TERMS)]
        try:
            xml = search(term)
            for item in extract_listings(xml, term, f"{run_id}_{offset}"):
                if item.finalise().classification in {"explicit_service", "likely_service", "mixed_or_parts"} and store.save(item):
                    new_items.append(item)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            errors.append(f"{term}: {type(exc).__name__}: {exc}")
    state_path.write_text(json.dumps({"next_term": (start + max_terms) % len(TERMS), "last_run": run_id}))
    report = store.write_reports(new_items, errors)
    if send:
        message = f"Marketplace monitor report {run_id}\nNew service-related listings: {len(new_items)}\nReport: {report}\n" + ("\n".join(f"- {x.title} | {x.price or 'no price'} | {x.location}" for x in new_items[:20]) or "No new service-related listings.")
        result = send_telegram(message)
        if result.startswith("not-sent:"):
            errors.append(result)
    return 0 if not errors or all(e.startswith("not-sent:") for e in errors) else 1, new_items, errors


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-terms", type=int, default=1)
    parser.add_argument("--no-send", action="store_true")
    args = parser.parse_args()
    code, items, errors = run_once(args.max_terms, not args.no_send)
    print(json.dumps({"exit": code, "new_count": len(items), "items": [asdict(x) for x in items], "errors": errors}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
