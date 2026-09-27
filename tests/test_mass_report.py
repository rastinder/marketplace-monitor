import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mass_report
import monitor


class _FakeListing:
    def __init__(self, title, classification="irrelevant_or_unclear"):
        self.title = title
        self.classification = classification
        self.image_ocr = ""


def _listing(title, classification="irrelevant_or_unclear"):
    return _FakeListing(title, classification)


class ReportFlowTests(unittest.TestCase):
    def test_flow_matches_verified_device_ui(self):
        self.assertEqual(
            mass_report.REPORT_FLOW,
            [
                "More actions",
                "Report listing",
                "Continue",
                "Selling or promoting restricted items",
                "Other restricted products",
                "Submit",
            ],
        )

    def test_done_is_not_a_flow_step(self):
        # "Done" is tapped best-effort after the confirmation marker is seen,
        # never as a flow step (it leaves the confirmation screen).
        self.assertNotIn("Done", mass_report.REPORT_FLOW)

    def test_find_exact_rejects_substring_heading(self):
        # monitor.find_bounds("Submit") would match this heading first;
        # exact matching must skip it and return the button.
        xml = (
            '<node text="You\'re about to submit a report" bounds="[0,300][1080,480]"/>'
            '<node text="Submit" bounds="[70,2280][1010,2400]"/>'
        )
        self.assertEqual(mass_report._find_exact(xml, "Submit"), (540, 2340))

    def test_report_flow_error_is_runtime_error(self):
        self.assertTrue(issubclass(mass_report.ReportFlowError, RuntimeError))

    def test_confirmation_markers_match_device_text(self):
        # full flow says "Thanks for reporting"; one-tap paths say the other
        self.assertIn("Thanks for reporting", mass_report.CONFIRM_MARKERS)
        self.assertIn("Thanks for letting us know", mass_report.CONFIRM_MARKERS)

    def test_find_node_unescapes_entity_encoded_emoji(self):
        # UI dumps encode emoji as &#128187; but titles come from node_values()
        xml = '<node text="placeholder" content-desc="Free&#128187; We Buy Computers" bounds="[0,100][1080,300]"/>'
        self.assertEqual(mass_report._find_node(xml, "Free💻 We Buy Computers"), (540, 200))


class WebsitePriorityTests(unittest.TestCase):
    def test_website_terms_are_covered_every_cycle(self):
        terms = mass_report.cycle_terms(0)
        for word in ("webflow", "wix", "shopify"):
            self.assertTrue(any(word in x for x in terms), word)

    def test_website_terms_run_first(self):
        terms = mass_report.cycle_terms(0)
        first_website = min(i for i, x in enumerate(terms) if x in monitor.WEBSITE_BUILDER_TERMS)
        first_other = min((i for i, x in enumerate(terms) if x not in monitor.WEBSITE_BUILDER_TERMS), default=99)
        self.assertLess(first_website, first_other)

    def test_cycle_preserves_every_term_exactly_once(self):
        terms = mass_report.cycle_terms(3)
        self.assertEqual(sorted(terms), sorted(monitor.TERMS))
        self.assertEqual(len(terms), len(set(terms)))

    def test_rotation_is_applied_inside_each_group(self):
        # start rotates both groups independently; the website group stays first
        start = 3
        terms = mass_report.cycle_terms(start)
        website = [t for t in monitor.TERMS if t in monitor.WEBSITE_BUILDER_TERMS]
        other = [t for t in monitor.TERMS if t not in monitor.WEBSITE_BUILDER_TERMS]
        rot = lambda g: g[start % len(g):] + g[:start % len(g)]
        self.assertEqual(terms, rot(website) + rot(other))


class AppReadinessTests(unittest.TestCase):
    """Cron may start from the home screen, a locked screen or a cold app."""

    def _shell(self, log, responses=None):
        responses = responses or {}

        def run(*args):
            log.append(" ".join(args))
            for key, value in responses.items():
                if key in " ".join(args):
                    return value
            return ""
        return run

    def test_wakes_screen_and_launches_app_when_on_home_screen(self):
        log = []
        focus = {"n": 0}

        def reader():
            focus["n"] += 1
            return "" if focus["n"] == 1 else "mCurrentFocus=Window{1 u0 com.facebook.katana/.MainActivity}"

        shell = self._shell(log)
        mass_report.ensure_app_ready(shell=shell, focus=reader, sleep_fn=lambda _s: None)
        joined = "\n".join(log)
        self.assertIn("KEYCODE_WAKEUP", joined)          # screen on
        self.assertIn("stayon true", joined)             # stays on for the whole cycle
        # the launcher activity is resolved at runtime (".MainActivity" does
        # not exist on this build), with monkey as the fallback
        self.assertTrue(
            "resolve-activity" in joined or "monkey -p com.facebook.katana" in joined,
            f"app was never launched: {joined}",
        )

    def test_login_ui_fails_loudly_instead_of_burning_every_term(self):
        # only the VISIBLE UI decides whether the session is gone; the task's
        # activity name is not evidence (see LoginDetectionTests)
        log = []
        shell = self._shell(log)
        with self.assertRaises(mass_report.FacebookLoginRequired):
            mass_report.ensure_app_ready(shell=shell,
                                        focus=lambda: "mCurrentFocus=Window{9 u0 com.facebook.katana/.MainActivity",
                                        ui_probe=lambda: '<node text="Log in"/><node text="Create new account"/><node content-desc="Password"/>',
                                        sleep_fn=lambda _s: None)

    def test_app_cannot_be_foregrounded_is_reported(self):
        log = []
        shell = self._shell(log)
        with self.assertRaises(mass_report.AppNotReady):
            mass_report.ensure_app_ready(shell=shell, focus=lambda: "mCurrentFocus=Window{1 u0 com.android.launcher}",
                                        sleep_fn=lambda _s: None)

    def test_already_ready_app_is_left_alone(self):
        log = []
        shell = self._shell(log)
        mass_report.ensure_app_ready(shell=shell,
                                     focus=lambda: "mCurrentFocus=Window{1 u0 com.facebook.katana/.MainActivity",
                                     sleep_fn=lambda _s: None)
        self.assertFalse(any("resolve-activity" in line or "monkey" in line for line in log))

    def test_login_errors_are_runtime_errors(self):
        self.assertTrue(issubclass(mass_report.FacebookLoginRequired, RuntimeError))
        self.assertTrue(issubclass(mass_report.AppNotReady, RuntimeError))

    def test_navigate_opens_marketplace_from_the_home_feed(self):
        # "Search Marketplace" is absent on the home feed, so the Marketplace
        # tab must be opened before a search is possible
        feed = '<node text="Marketplace" bounds="[0,2200][300,2400]"/><node text="Friends"/>'
        results = '<node text="Search Marketplace" bounds="[0,100][1080,200]"/><node text="Filters"/>'
        taps = []
        screens = {"i": 0}
        originals = (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
                     mass_report.subprocess.run, mass_report.ensure_app_ready)
        try:
            def fake_read_ui():
                return feed if screens["i"] == 0 else results

            def fake_run(args, *a, **kw):
                if "tap" in args:
                    taps.append(args[-2:])
                    screens["i"] = 1
                class _P:
                    stdout = ""
                return _P()

            mass_report.subprocess.run = fake_run
            monitor.read_ui = fake_read_ui
            mass_report._find_exact = lambda xml, needle: (540, 100) if needle in xml else None
            mass_report.time.sleep = lambda _s: None
            mass_report.ensure_app_ready = lambda **_kw: None
            def advance_tap(*_a, **_k):  # legacy tap() path is not used any more
                screens["i"] = 1
            mass_report.navigate_marketplace()
        finally:
            (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
             mass_report.subprocess.run, mass_report.ensure_app_ready) = originals
        self.assertTrue(taps, "expected a tap on the Marketplace tab")
        # second read (after the tab tap) must have been the results surface
        self.assertEqual(screens["i"], 1)


class LoginDetectionTests(unittest.TestCase):
    """Regression: an authenticated Facebook task can be named LoginActivity."""

    LOGGED_OUT = ('<node text="Log in"/><node text="Create new account"/>'
                  '<node content-desc="Password"/><node text="Email or phone number"/>')
    FEED = ('<node text="facebook"/><node text="Whats on your mind"/>' 
            '<node text="Search"/><node content-desc="Messaging, 14 unread messages"/>'
            '<node text="Meet Muse, your personal travel agent"/>')

    def test_logged_out_screen_is_detected(self):
        self.assertTrue(mass_report.login_screen_visible(self.LOGGED_OUT))

    def test_authenticated_feed_is_not_called_logged_out(self):
        self.assertFalse(mass_report.login_screen_visible(self.FEED))

    def test_activity_name_alone_never_implies_logout(self):
        # the exact string that caused a false "logged out" verdict
        info = "mCurrentFocus=Window{957985 u0 com.facebook.katana/com.facebook.katana.LoginActivity"
        self.assertFalse(mass_report.login_required_from_focus(info))

    def test_ensure_app_ready_keeps_going_on_a_logged_in_loginactivity(self):
        log = []

        def shell(*args):
            log.append(" ".join(args))
            return ""

        screens = iter([self.FEED, self.FEED])
        info = mass_report.ensure_app_ready(shell=shell,
                                           focus=lambda: "mCurrentFocus=Window{1 u0 com.facebook.katana/com.facebook.katana.LoginActivity",
                                           ui_probe=lambda: next(screens),
                                           sleep_fn=lambda _s: None)
        self.assertIn("LoginActivity", info)  # returned, not raised

    def test_ensure_app_ready_raises_only_on_real_login_ui(self):
        def shell(*args):
            return ""

        with self.assertRaises(mass_report.FacebookLoginRequired):
            mass_report.ensure_app_ready(shell=shell,
                                        focus=lambda: "mCurrentFocus=Window{1 u0 com.facebook.katana/.MainActivity",
                                        ui_probe=lambda: self.LOGGED_OUT,
                                        sleep_fn=lambda _s: None)


class ViewerUnwindTests(unittest.TestCase):
    STORY = ('<node text="Menu"/><node text="Meet Muse, your personal travel agent"/>'
             '<node text="Try Muse"/><node text="facebook"/>')
    FEED_WITH_TAB = '<node text="facebook"/><node text="Marketplace" content-desc="Marketplace"/>'
    RESULTS = '<node text="Search Marketplace"/><node text="Filters"/>'

    def test_back_is_pressed_until_a_marketplace_surface_appears(self):
        screens = [self.STORY, self.STORY, self.FEED_WITH_TAB, self.RESULTS]
        back = []
        taps = []
        originals = (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
                     mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb)
        try:
            state = {"i": 0}

            def fake_read_ui():
                return screens[min(state["i"], len(screens) - 1)]

            def fake_run(args, *a, **kw):
                if "keyevent" in args and args[-1] == "4":
                    back.append(args)
                    state["i"] += 1
                elif "tap" in args:
                    taps.append(args[-2:])
                    state["i"] = len(screens) - 1

            mass_report.subprocess.run = fake_run
            monitor.read_ui = fake_read_ui
            mass_report._find_exact = lambda xml, needle: (540, 100) if needle in xml else None
            mass_report.time.sleep = lambda _s: None
            mass_report.ensure_app_ready = lambda **_kw: None
            mass_report._launch_fb = lambda *a, **kw: None
            mass_report.navigate_marketplace()
        finally:
            (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
             mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb) = originals
        self.assertTrue(back, "expected BACK presses to leave the story viewer")
        self.assertTrue(taps, "expected a tap on the Marketplace tab")

    LAUNCHER = '<node text="Page 1 of 8."/><node text="Instagram"/><node text="Gallery"/>'

    def test_backing_out_of_the_app_relaunches_it(self):
        # BACK can leave Facebook entirely; the next pass must bring it back
        # instead of pressing BACK on the launcher forever
        screens = [self.STORY, self.LAUNCHER, self.FEED_WITH_TAB, self.RESULTS]
        launches = []
        originals = (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
                     mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb)
        try:
            state = {"i": 0}

            def fake_read_ui():
                return screens[min(state["i"], len(screens) - 1)]

            def fake_run(args, *a, **kw):
                if "keyevent" in args and args[-1] == "4":
                    state["i"] = 1
                elif "tap" in args:
                    state["i"] = len(screens) - 1

            mass_report.subprocess.run = fake_run
            monitor.read_ui = fake_read_ui
            mass_report._find_exact = lambda xml, needle: (540, 100) if needle in xml else None
            mass_report.time.sleep = lambda _s: None
            mass_report.ensure_app_ready = lambda **_kw: None
            def fake_launch(*_a, **_kw):
                launches.append(1)
                state["i"] = 2  # the app is back (feed with tab)

            mass_report._launch_fb = fake_launch
            mass_report.navigate_marketplace()
        finally:
            (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
             mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb) = originals
        self.assertTrue(launches, "expected the app to be relaunched after BACK left it")

    LAUNCHER_NODES = '<node text="Page 1 of 8."/><node text="Instagram"/>'

    def test_deep_link_is_used_before_any_back_press(self):
        # BACK exits the app from an immersive Reels/story task, so the
        # fb://marketplace deep link is the primary route
        calls = []
        originals = (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
                     mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb)
        try:
            state = {"linked": False}

            def fake_read_ui():
                return self.RESULTS if state["linked"] else self.LAUNCHER_NODES

            def fake_run(args, *a, **kw):
                if args[:2] == ["adb", "shell"] and "am" in args and "start" in args:
                    if any("fb://marketplace" in str(x) for x in args):
                        state["linked"] = True
                        calls.append("deep-link")
                elif "keyevent" in args and args[-1] == "4":
                    calls.append("back")

            mass_report.subprocess.run = fake_run
            monitor.read_ui = fake_read_ui
            mass_report._find_exact = lambda xml, needle: (540, 100) if needle in xml else None
            mass_report.time.sleep = lambda _s: None
            mass_report.ensure_app_ready = lambda **_kw: None
            mass_report._launch_fb = lambda *a, **kw: None
            mass_report.navigate_marketplace()
        finally:
            (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
             mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb) = originals
        self.assertEqual(calls[0], "deep-link", f"expected the deep link first, got {calls}")

    def test_falls_back_to_the_marketplace_tab_when_the_link_fails(self):
        screens = [self.LAUNCHER_NODES, self.FEED_WITH_TAB, self.RESULTS]
        taps = []
        state = {"i": 0}
        originals = (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
                     mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb)
        try:
            def fake_run(args, *a, **kw):
                if "tap" in args:
                    taps.append(args[-2:])
                    state["i"] = 2
                elif "am" in args and "start" in args:
                    pass  # deep link does not help in this scenario
                elif "keyevent" in args and args[-1] == "4":
                    state["i"] = min(state["i"] + 1, len(screens) - 1)

            mass_report.subprocess.run = fake_run
            monitor.read_ui = lambda: screens[state["i"]]
            mass_report._find_exact = lambda xml, needle: (540, 100) if needle in xml else None
            mass_report.time.sleep = lambda _s: None
            mass_report.ensure_app_ready = lambda **_kw: None
            mass_report._launch_fb = lambda *a, **kw: state.__setitem__("i", 1)  # relaunch -> feed
            mass_report.navigate_marketplace()
        finally:
            (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
             mass_report.subprocess.run, mass_report.ensure_app_ready, mass_report._launch_fb) = originals
        self.assertTrue(taps, "expected the Marketplace tab tap as a fallback")

    def test_gives_up_with_a_clear_error_when_nowhere_to_go(self):
        originals = (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
                     mass_report.subprocess.run, mass_report.ensure_app_ready)
        try:
            mass_report.subprocess.run = lambda *a, **kw: type("P", (), {"stdout": ""})()
            monitor.read_ui = lambda: self.STORY
            mass_report._find_exact = lambda xml, needle: None
            mass_report.time.sleep = lambda _s: None
            mass_report.ensure_app_ready = lambda **_kw: None
            with self.assertRaises(mass_report.AppNotReady):
                mass_report.navigate_marketplace()
        finally:
            (monitor.read_ui, mass_report._find_exact, mass_report.time.sleep,
             mass_report.subprocess.run, mass_report.ensure_app_ready) = originals


class SponsoredGuardTests(unittest.TestCase):
    def test_sponsored_listing_is_never_reportable(self):
        item = monitor.Listing(source_term="pc repair", title="Handyman available today", description="")
        item.sponsored = True
        self.assertFalse(mass_report.is_reportable(item))

    def test_skip_reason_names_sponsored(self):
        item = _listing("Handyman available today")
        item.sponsored = True
        item.classification = "irrelevant_or_unclear"
        self.assertEqual(mass_report.skip_reason(item), "sponsored_ad")

    def test_detail_page_sponsored_label_is_detected(self):
        self.assertTrue(mass_report.detail_is_sponsored('<node text="Sponsored"/><node text="Report listing"/>'))
        self.assertFalse(mass_report.detail_is_sponsored('<node text="More actions"/><node text="Report listing"/>'))


class DeviceTests(unittest.TestCase):
    """The phone is often unplugged (charging) when cron fires."""

    def test_missing_device_raises_device_unavailable(self):
        def shell(*args):
            raise subprocess.CalledProcessError(1, list(args), stderr="adb: no devices/emulators found")

        with self.assertRaises(mass_report.DeviceUnavailable):
            mass_report.ensure_app_ready(shell=shell, focus=lambda: "", sleep_fn=lambda _s: None)

    def test_device_unavailable_is_a_runtime_error(self):
        self.assertTrue(issubclass(mass_report.DeviceUnavailable, RuntimeError))

    def test_preflight_returns_a_clean_abort_for_a_missing_device(self):
        original = mass_report.ensure_app_ready
        mass_report.ensure_app_ready = lambda **_kw: (_ for _ in ()).throw(
            mass_report.DeviceUnavailable("no device attached"))
        try:
            abort = mass_report.cycle_preflight("RUN1")
        finally:
            mass_report.ensure_app_ready = original
        self.assertIsNotNone(abort)
        self.assertEqual(abort["aborted_because"], "DeviceUnavailable")
        self.assertIn("no device", abort["detail"])

    def test_preflight_returns_none_when_the_app_is_ready(self):
        original = mass_report.ensure_app_ready
        mass_report.ensure_app_ready = lambda **_kw: "mCurrentFocus=com.facebook.katana"
        try:
            self.assertIsNone(mass_report.cycle_preflight("RUN1"))
        finally:
            mass_report.ensure_app_ready = original

    def test_preflight_reports_a_logged_out_app(self):
        original = mass_report.ensure_app_ready
        mass_report.ensure_app_ready = lambda **_kw: (_ for _ in ()).throw(
            mass_report.FacebookLoginRequired("sign in"))
        try:
            abort = mass_report.cycle_preflight("RUN1")
        finally:
            mass_report.ensure_app_ready = original
        self.assertEqual(abort["aborted_because"], "FacebookLoginRequired")


class CardTapTests(unittest.TestCase):
    CARD = (
        '<node bounds="[0,0][1080,2400]">'
        '  <node content-desc="Seller image for Handyman" bounds="[10,700][200,900]"/>'
        '  <node bounds="[0,650][1080,1100]" clickable="true">'
        '    <node text="Handyman" bounds="[20,950][1060,1010]"/>'
        '    <node text="Riverton · 5 km" bounds="[20,1020][400,1060]"/>'
        "  </node>"
        "</node>"
    )

    def test_title_point_is_first(self):
        points = mass_report.card_tap_points(self.CARD, "Handyman")
        self.assertEqual(points[0], (540, 980))

    def test_clickable_container_is_offered(self):
        points = mass_report.card_tap_points(self.CARD, "Handyman")
        # the clickable card container [0,650][1080,1100] must be a fallback
        self.assertIn((540, 875), points)

    def test_image_node_is_offered_as_fallback_for_the_same_card(self):
        # title run is not clickable; the card container wraps image + title
        xml = (
            '<node bounds="[0,650][1080,1100]" clickable="true">'
            '  <node content-desc="Seller image for Yoga classes" bounds="[10,700][200,900]"/>'
            '  <node text="Yoga classes" bounds="[20,950][1060,1010]"/>'
            "</node>"
        )
        points = mass_report.card_tap_points(xml, "Yoga classes")
        self.assertEqual(points[0], (540, 980))
        self.assertIn((540, 875), points)   # clickable container
        self.assertIn((105, 800), points)   # card image inside the same container

    def test_contains_fallback_does_not_leak_other_cards(self):
        xml = (
            '<node text="Handyman" bounds="[20,950][1060,1010]"/>'
            '<node content-desc="Seller image for Other listing" bounds="[10,1700][200,1900]"/>'
        )
        points = mass_report.card_tap_points(xml, "Handyman")
        self.assertNotIn((105, 1800), points)

    def test_returns_empty_when_title_absent(self):
        self.assertEqual(mass_report.card_tap_points(self.CARD, "Nothing here"), [])


class ReportabilityTests(unittest.TestCase):
    def test_explicit_service_is_reportable(self):
        item = monitor.Listing(source_term="pc repair", title="PC and Laptop repair", description="Computer repair service", image_ocr="COMPUTER REPAIR")
        self.assertTrue(mass_report.is_reportable(item))

    def test_product_listing_is_not_reportable(self):
        # Marketplace allows product sales - only services get reported
        item = monitor.Listing(source_term="pc repair", title="Hp Laptop for sale", description="Used laptop, accessories included")
        self.assertFalse(mass_report.is_reportable(item))

    def test_image_only_service_is_reportable(self):
        item = monitor.Listing(source_term="laptop repair", title="I.T Specialist", description="", image_ocr="RET TECH computer repair")
        self.assertTrue(mass_report.is_reportable(item))

    def test_book_collection_with_service_ocr_is_not_reportable(self):
        # screen-wide OCR contains service words; a book title must not ride it
        item = monitor.Listing(
            source_term="software development",
            title="AI Book Collection | Artificial Intelligence",
            description="",
            image_ocr="computer repair service laptop support near me",
        )
        self.assertFalse(mass_report.is_reportable(item))

    def test_course_title_is_reportable(self):
        # courses are not physical products -> reported (T&C: services need ads)
        item = monitor.Listing(source_term="software development", title="Software Development Course for Beginners", description="")
        self.assertTrue(mass_report.is_reportable(item))

    def test_mixed_listing_with_action_word_is_reportable(self):
        item = monitor.Listing(source_term="pc repair", title="Laptop Repair or Sale - used units", description="")
        self.assertTrue(mass_report.is_reportable(item))

    def test_pure_product_with_device_words_stays_unreported(self):
        # device word alone ("laptop") is not a service action
        item = monitor.Listing(source_term="pc repair", title="Dell OptiPlex desktop", description="like new, stickers applied")
        self.assertFalse(mass_report.is_reportable(item))

    def test_junk_ui_label_is_skipped_by_extract(self):
        with tempfile.TemporaryDirectory() as d:
            old_evidence = monitor.EVIDENCE
            monitor.EVIDENCE = Path(d)
            try:
                xml = '<node text="Ad from seller"/><node text="Sponsored"/><node text="Remote IT Support Service"/>'
                items = monitor.extract_listings(xml, "it support", "junktest", capture=False)
            finally:
                monitor.EVIDENCE = old_evidence
            self.assertEqual([x.title for x in items], ["Remote IT Support Service"])


class NestedCycleTests(unittest.TestCase):
    def test_main_defaults_sweep_everything(self):
        import inspect
        params = inspect.signature(mass_report.main).parameters
        self.assertEqual(params["max_terms"].default, 0)   # 0 = all terms
        self.assertEqual(params["max_reports"].default, 0)  # no cap

    def test_scroll_pages_per_term(self):
        # nested loop: terms x scroll pages x listings
        self.assertGreaterEqual(mass_report.PAGES_PER_TERM, 2)

    def test_extract_without_capture_writes_no_screenshots(self):
        with tempfile.TemporaryDirectory() as d:
            old_evidence = monitor.EVIDENCE
            monitor.EVIDENCE = Path(d)
            try:
                xml = '<node text="PC and Laptop repair service"/>'
                items = monitor.extract_listings(xml, "pc repair", "nestedtest", capture=False)
            finally:
                monitor.EVIDENCE = old_evidence
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].image_ocr, "")
            self.assertEqual(list(Path(d).glob("*")), [])  # no screenshot/ocr written


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE reported(fingerprint TEXT PRIMARY KEY, title TEXT, first_seen TEXT, last_seen TEXT, payload TEXT)"
        )

    def _item(self):
        return monitor.Listing(
            source_term="pc repair",
            title="PC and Laptop repair",
            description="Computer repair service",
            image_ocr="COMPUTER REPAIR",
        )

    def test_already_reported_false_for_new_listing(self):
        self.assertFalse(mass_report._already_reported(self.conn, self._item()))

    def test_store_then_already_reported_true(self):
        item = self._item()
        fp = mass_report._store(self.conn, item)
        self.assertTrue(mass_report._already_reported(self.conn, item))
        self.assertEqual(fp, item.fingerprint)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM reported").fetchone()[0], 1)

    def test_store_is_idempotent(self):
        item = self._item()
        mass_report._store(self.conn, item)
        mass_report._store(self.conn, item)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM reported").fetchone()[0], 1)

    def test_payload_records_reason(self):
        mass_report._store(self.conn, self._item())
        payload = self.conn.execute("SELECT payload FROM reported").fetchone()[0]
        self.assertIn("Selling or promoting restricted items", payload)


class ResultsPageGuardTests(unittest.TestCase):
    RESULTS = '<node text="Filters"/><node text="Save this search"/><node text="Handyman"/>'
    LANDING = '<node text="Navigate to Search"/><node text="Browse"/><node text="Sell"/>'

    def test_results_page_is_detected(self):
        self.assertTrue(mass_report.on_results_page(self.RESULTS))

    def test_landing_page_is_not_results(self):
        # the search box never took us to results - extracting here yields
        # search-bar chrome as if it were listings
        self.assertFalse(mass_report.on_results_page(self.LANDING))

    def test_landing_chrome_is_junk(self):
        for label in ("Navigate to Search", "Browse", "Sell", "Notifications", "Messages", "Search Marketplace"):
            self.assertTrue(monitor.is_junk_label(label), label)

    def test_report_refuses_ui_chrome(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE reported(fingerprint TEXT PRIMARY KEY, title TEXT, first_seen TEXT, last_seen TEXT, payload TEXT)")
        item = monitor.Listing(source_term="pc repair", title="Navigate to Search", description="")
        with self.assertRaises(mass_report.ReportFlowError):
            mass_report.report(conn, item)


class AlreadyOpenTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE reported(fingerprint TEXT PRIMARY KEY, title TEXT, first_seen TEXT, last_seen TEXT, payload TEXT)")
        self._orig = (mass_report._already_reported, mass_report._back_to_results, mass_report._open_listing)
        mass_report._already_reported = lambda conn, item: True   # dedup hit
        mass_report._back_to_results = lambda: self.calls.append("back")
        mass_report._open_listing = lambda title: self.calls.append("open")

    def tearDown(self):
        (mass_report._already_reported, mass_report._back_to_results, mass_report._open_listing) = self._orig

    def test_dedup_on_open_detail_page_walks_back(self):
        # we are already on the detail page after enrichment; a dedup skip must
        # still return the phone to the results screen
        item = monitor.Listing(source_term="pc repair", title="Handyman", description="")
        item.finalise()
        rec = mass_report.report(self.conn, item, already_open=True)
        self.assertIsNone(rec)
        self.assertIn("back", self.calls)
        self.assertNotIn("open", self.calls)


class SkipAuditTests(unittest.TestCase):
    def test_page_screenshot_path_is_per_search_page(self):
        path = mass_report.page_screenshot_path("RUN1", 3, 2)
        self.assertEqual(path.name, "RUN1_3_2.png")
        self.assertEqual(path.parent.name, "pages")

    def test_parse_detail_extracts_full_title_and_description(self):
        xml = (
            '<node text="Laptop And Desktop Sale Parts And Repair Services" bounds="[0,200][1080,300]"/>'
            '<node text="We repair laptops and desktops same day. Bring your machine to 5 Main St." bounds="[0,400][1080,900]"/>'
        )
        title, description = mass_report.parse_detail(xml)
        self.assertEqual(title, "Laptop And Desktop Sale Parts And Repair Services")
        self.assertIn("repair laptops and desktops", description)

    def test_parse_detail_ignores_page_chrome(self):
        xml = (
            '<node text="Product Image,1 of 1"/>'
            '<node text="More actions"/>'
            '<node text="Filters"/>'
            '<node text="Handyman services available"/>'
            '<node text="We fix sinks, toilets, leaks and installs. Free estimates in Riverton."/>'
        )
        title, description = mass_report.parse_detail(xml)
        self.assertEqual(title, "Handyman services available")
        self.assertIn("Free estimates", description)

    def test_truncated_service_title_needs_detail(self):
        item = _listing("Laptop And Desktop Sale Parts And R\u2026")
        self.assertTrue(mass_report.needs_detail(item))

    def test_truncated_product_title_still_needs_detail_review(self):
        # unknown tail: opening the detail page is how we find out
        item = _listing("Laptop Screen Replacement - New & \u2026")
        self.assertTrue(mass_report.needs_detail(item))

    def test_enrichment_is_budgeted_per_page(self):
        # opening detail pages is ~10s each, so only a few per results page
        self.assertTrue(mass_report.should_enrich(_listing("Handyman work\u2026"), used=0))
        self.assertFalse(mass_report.should_enrich(_listing("Handyman work\u2026"), used=mass_report.ENRICH_PER_PAGE))

    def test_enrichment_budget_skips_plain_products(self):
        self.assertFalse(mass_report.should_enrich(_listing("Kitchen table solid wood"), used=0))

    def test_plain_product_does_not_need_detail(self):
        item = _listing("Kitchen table solid wood")
        self.assertFalse(mass_report.needs_detail(item))

    def test_page_screenshot_directory_exists(self):
        path = mass_report.page_screenshot_path("RUNDIR", 0, 0)
        self.assertTrue(path.parent.is_dir(), "pages dir must exist for per-search screenshots")

    def test_skip_reason_names_the_veto(self):
        self.assertEqual(mass_report.skip_reason(_listing("Kitchen table solid wood", "irrelevant_or_unclear")), "no_service_signal")
        self.assertEqual(mass_report.skip_reason(_listing("Hp Laptop for sale", "mixed_or_parts")), "product_veto")

    def test_page_ocr_is_shared_by_every_candidate(self):
        items = [monitor.Listing(source_term="pc repair", title="Computer tower"),
                 monitor.Listing(source_term="pc repair", title="Antique chair")]
        n = mass_report.apply_page_ocr(items, "COMPUTER REPAIR SERVICE on this page")
        self.assertEqual(n, 2)
        self.assertTrue(all(i.image_ocr for i in items))
        # topical title may be promoted; an off-topic one must NOT be
        self.assertEqual(items[0].classification, "likely_service")
        self.assertEqual(items[1].classification, "irrelevant_or_unclear")

    def test_repair_parts_ad_is_not_reportable(self):
        item = monitor.Listing(source_term="laptop repair", title="iPhone Phone Repair Parts", description="")
        self.assertFalse(mass_report.is_reportable(item))

    def test_parts_ad_is_not_serviceish(self):
        # "for parts or repair" sells a broken item, not labour: the audit
        # metric must not cry over-skip here
        self.assertFalse(mass_report.is_serviceish_title("Lenovo Yoga C740-15IML for parts or repair"))
        self.assertFalse(mass_report.is_serviceish_title("Dell Latitude 5410 i7-10th Gen-Not Working-Parts/Fix"))

    def test_serviceish_skip_is_detected(self):
        self.assertTrue(mass_report.is_serviceish_title("Handyman"))
        self.assertTrue(mass_report.is_serviceish_title("Professional Web Development | $30/hr"))
        self.assertFalse(mass_report.is_serviceish_title("1TB Samsung 860 EVO SSD drive"))

    def test_repair_tool_kit_product_is_not_reportable(self):
        item = monitor.Listing(source_term="laptop repair", title="iFixit Precision Repair Tool Kit - Electronics, Phone & Laptop Repair", description="")
        self.assertFalse(mass_report.is_reportable(item))

    def test_laptop_sold_for_parts_is_not_reportable(self):
        item = monitor.Listing(source_term="pc repair", title="Acer Predator Helios 300 (RTX 2060/i7) AS-IS for parts or repair", description="As tested, boots fine")
        self.assertFalse(mass_report.is_reportable(item))

    def test_broken_tablet_parts_ad_is_not_reportable(self):
        item = monitor.Listing(source_term="laptop repair", title="Lenovo X1 Tab Gen 3 | Core i5 8th Gen - Parts/Fix - Not Working", description="")
        self.assertFalse(mass_report.is_reportable(item))

    def test_closed_stdout_does_not_break_a_report(self):
        import io
        mass_report._say("before")
        # simulate a closed pipe: _say must swallow it
        class _Broken:
            def write(self, *_a): raise BrokenPipeError(32, "Broken pipe")
            def flush(self): pass
        original = sys.stdout
        sys.stdout = _Broken()
        try:
            mass_report._say("after")
        finally:
            sys.stdout = original

    def test_repair_services_ad_is_still_reportable(self):
        item = monitor.Listing(source_term="pc repair", title="Laptop And Desktop Sale Parts And Repair Services", description="We fix and repair same day")
        self.assertTrue(mass_report.is_reportable(item))

    def test_device_wording_is_not_serviceish(self):
        # the audit metric must agree with the report rule, or it flags products
        self.assertFalse(mass_report.is_serviceish_title("iPhone LCD replacement screen"))
        self.assertFalse(mass_report.is_serviceish_title("Phone screen on sale samsung apple all models"))

    def test_log_skip_writes_auditable_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "skips.jsonl"
            item = _listing("Kitchen table solid wood")
            rec = mass_report.log_skip(path, item, term="pc repair", page=1, run_id="RUN1")
            line = json.loads(path.read_text().strip())
            self.assertEqual(line["title"], "Kitchen table solid wood")
            self.assertEqual(line["term"], "pc repair")
            self.assertEqual(line["page"], 1)
            self.assertEqual(line["reason"], rec["reason"])
            self.assertFalse(line["serviceish"])

    def test_serviceish_skip_is_flagged_in_the_log(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "skips.jsonl"
            mass_report.log_skip(path, _listing("Handyman"), term="t", page=0, run_id="R")
            self.assertTrue(json.loads(path.read_text().strip())["serviceish"])

    def test_summarise_skips_reports_reason_counts(self):
        skips = [
            {"reason": "no_service_signal", "serviceish": False},
            {"reason": "no_service_signal", "serviceish": True},
            {"reason": "product_veto", "serviceish": False},
        ]
        summary = mass_report.summarise_skips(skips)
        self.assertEqual(summary["skipped_total"], 3)
        self.assertEqual(summary["skipped_but_serviceish"], 1)
        self.assertEqual(summary["skips_by_reason"]["no_service_signal"], 2)


if __name__ == "__main__":
    unittest.main()
