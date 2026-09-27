import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import monitor
from monitor import Listing, MonitorStore, extract_listings, normalise_title


class MonitorTests(unittest.TestCase):
    def test_explicit_service_is_high_confidence(self):
        item = Listing(source_term="pc repair", title="PC and Laptop repair", description="Computer repair service", image_ocr="COMPUTER REPAIR").finalise()
        self.assertEqual(item.classification, "explicit_service")
        self.assertEqual(item.confidence, "high")

    def test_image_only_service_is_likely(self):
        item = Listing(source_term="laptop repair", title="I.T Specialist", description="", image_ocr="RET TECH computer repair").finalise()
        self.assertEqual(item.classification, "likely_service")
        self.assertIn("image_only_or_minimal_text", item.signals)

    def test_physical_product_is_not_service(self):
        item = Listing(source_term="pc repair", title="Hp Laptop for sale", description="Used laptop, accessories included").finalise()
        self.assertEqual(item.classification, "mixed_or_parts")

    def test_handyman_trade_word_is_service(self):
        item = Listing(source_term="handyman", title="Handyman", description="").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_it_solutions_is_service(self):
        item = Listing(source_term="pc repair", title="IT Solutions", description="Managed IT for small business").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_laser_engraving_is_service(self):
        item = Listing(source_term="data recovery", title="Live laser engraving for events, personalized gifts", description="").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_calendar_management_is_service(self):
        item = Listing(source_term="app development", title="Let Muse manage your calendar", description="Virtual assistant services").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_fall_cleanup_is_service(self):
        item = Listing(source_term="pc repair", title="Free · Fall cleanup", description="Lawn and yard work, leaf removal").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_hourly_rate_is_service(self):
        item = Listing(source_term="web design service", title="Professional Web Development, Automation & Digital Branding | $30/hr", description="").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_product_dominant_listing_is_flagged(self):
        item = Listing(source_term="laptop repair", title="Acer Predator Helios 300 (RTX 2060/i7) AS-IS for parts or repair", description="As tested").finalise()
        self.assertIn("product_dominant", item.signals)
        self.assertEqual(item.classification, "mixed_or_parts")

    def test_grow_business_offer_is_service(self):
        item = Listing(source_term="website development", title="Grow your business online with professionals", description="We handle marketing end to end").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_offer_language_alone_is_service(self):
        item = Listing(source_term="pc repair", title="Will do any odd jobs around Riverton", description="").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_used_laptop_stays_not_service(self):
        item = Listing(source_term="pc repair", title="Used ThinkPad T480 for sale", description="Good battery, charger included, 16GB RAM").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")

    def test_ssd_for_sale_stays_not_service(self):
        item = Listing(source_term="data recovery", title="1TB Samsung 860 EVO SSD drive", description="Brand new sealed, $50").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")

    def test_household_item_stays_not_service(self):
        item = Listing(source_term="pc repair", title="Kitchen table solid wood", description="Pick up only, moving out sale").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")

    def test_device_word_alone_is_not_a_service(self):
        # regression guard: a bare device word must not read as "selling a service"
        item = Listing(source_term="pc repair", title="iPhone LCD replacement screen", description="All models available").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")

    def test_device_sale_wording_stays_not_service(self):
        item = Listing(source_term="laptop repair", title="Phone screen on sale samsung apple all models", description="").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")

    def test_product_dominant_listing_is_flagged(self):
        item = Listing(source_term="laptop repair", title="Acer Predator Helios 300 (RTX 2060/i7) AS-IS for parts or repair", description="As tested").finalise()
        self.assertIn("product_dominant", item.signals)
        self.assertEqual(item.classification, "mixed_or_parts")

    def test_grow_business_offer_is_service(self):
        item = Listing(source_term="website development", title="Grow your business online", description="").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_ocr_promotion_rejects_spec_heavy_product_titles(self):
        # whole-screen OCR must not turn a hardware listing into a "service"
        item = Listing(source_term="data recovery", title="Hawking 8-Port 10/100Mbps Ethernet Switch", description="", image_ocr="COMPUTER REPAIR SERVICE AVAILABLE").finalise()
        self.assertNotEqual(item.classification, "explicit_service")
        self.assertNotEqual(item.classification, "likely_service")

    def test_ocr_promotion_needs_topical_title(self):
        item = Listing(source_term="pc repair", title="Antique chair", description="", image_ocr="LAPTOP REPAIR SETUP DEVELOPMENT").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")

    def test_action_menu_label_is_junk(self):
        for label in ("More actions", "See more options", "Message seller", "Seller information"):
            self.assertTrue(monitor.is_junk_label(label), label)

    def test_ocr_promotion_needs_an_action_word(self):
        # screen OCR often carries a device word; only labour words promote
        item = Listing(source_term="pc repair", title="Dell Latitude 5400", description="", image_ocr="Laptop and desktop for sale, service warranty included").finalise()
        self.assertNotEqual(item.classification, "likely_service")

    def test_service_history_is_not_a_service_offer(self):
        item = Listing(source_term="pc repair", title="2016 Volvo XC90 T6 AWD *LOW KMS* FULL SERVICE HISTORY* CLEAN CARFAX", description="").finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")
        self.assertIn("service_word_in_non_service_context", item.signals)

    def test_real_service_still_passes_context_veto(self):
        item = Listing(source_term="pc repair", title="Laptop repair service with warranty", description="Full service, same day").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_product_image_chrome_is_junk(self):
        for label in ("Product Image,1 of 1", "Product Image,1 of 2", "Image", "Seller image for X"):
            self.assertTrue(monitor.is_junk_label(label), label)

    def test_broken_tablet_sold_for_parts_is_a_product(self):
        item = Listing(source_term="laptop repair", title="Lenovo X1 Tab Gen 3 | Core i5 8th Gen - Parts/Fix - Not Working", description="").finalise()
        self.assertIn("product_dominant", item.signals)

    def test_development_kit_is_a_product(self):
        for title in ("PROJECT TANGO TABLET DEVELOPMENT KIT",
                      "Intel RealSense R200 3D Depth Camera Developer Kit",
                      "Estimote Location Beacons Development Kit"):
            item = Listing(source_term="pc repair", title=title, description="").finalise()
            self.assertIn("product_dominant", item.signals, title)

    def test_pc_case_brand_title_is_a_product(self):
        item = Listing(source_term="pc repair", title="Fractal Design Computer Case and power case", description="").finalise()
        self.assertIn("product_dominant", item.signals)

    def test_brand_name_with_service_word_still_reports(self):
        item = Listing(source_term="pc repair", title="Dell i7 Touchscreen Laptop / Home Service", description="We come to you").finalise()
        self.assertNotIn("product_dominant", item.signals)
        self.assertEqual(item.classification, "explicit_service")

    def test_bare_design_is_not_a_service_word(self):
        item = Listing(source_term="web design service", title="Diamond Design Lamp", description="").finalise()
        self.assertNotIn("product_dominant", item.signals)

    def test_qualified_design_is_a_service(self):
        item = Listing(source_term="web design service", title="Professional web design services", description="").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_replacement_screen_is_a_product(self):
        # must never read as a service, with or without page OCR attached
        for title in ("Laptop Premium Quality Screen Replacement Available",
                      "iPhone 13 Screen Replacement - OEM", "Dell LCD Replacement Panel"):
            item = Listing(source_term="laptop repair", title=title, description="",
                           image_ocr="COMPUTER REPAIR SERVICE laptop screen repair setup").finalise()
            self.assertEqual(item.classification, "irrelevant_or_unclear", title)

    def test_website_builder_service_is_service(self):
        ads = [
            "Wix website design package - $499, 5 day delivery",
            "Custom Shopify store setup and product upload",
            "Webflow expert - landing page live in 3 days",
            "I will redesign your Squarespace site",
            "SEO services for local business, Google My Business setup",
            "Need a website? Custom web development, affordable rates",
            "WordPress expert - fix, migrate and speed up your site",
        ]
        for title in ads:
            item = Listing(source_term="website development", title=title, description="").finalise()
            self.assertIn(item.classification, ("explicit_service", "likely_service"), title)

    def test_website_builder_goods_are_not_services(self):
        goods = [
            "WordPress theme bundle - 12 premium themes",
            "Shopify app plugin license, lifetime access",
            "Elementor template pack for WordPress",
            "Wix premium plan 1 year subscription",
        ]
        for title in goods:
            item = Listing(source_term="website development", title=title, description="").finalise()
            self.assertNotIn(item.classification, ("explicit_service", "likely_service"), title)

    def test_website_builder_terms_are_searched(self):
        joined = " ".join(monitor.TERMS).lower()
        for word in ("webflow", "wix", "shopify", "landing page", "website"):
            self.assertIn(word, joined)

    def test_sponsored_label_variants_are_recognised(self):
        for label in ("Sponsored", "Sponsored \u00b7", "Ad from seller", "Ad", "Ads",
                      "Promoted", "Paid partnership", "Sponsored listing", "Boosted",
                      "Sponsoris\u00e9", "Anuncio", "Gesponsert"):
            self.assertTrue(monitor.is_sponsored_label(label), label)

    def test_sponsored_card_is_flagged_from_its_adjacent_label(self):
        xml = (
            '<node text="Sponsored"/>'
            '<node text="Handyman available today"/>'
            '<node text="Riverton \u00b7 5 km"/>'
        )
        items = monitor.extract_listings(xml, "pc repair", "t", capture=False)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].sponsored)

    def test_unsponsored_card_is_not_flagged(self):
        xml = '<node text="Handyman available today"/><node text="Riverton \u00b7 5 km"/>'
        items = monitor.extract_listings(xml, "pc repair", "t", capture=False)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].sponsored)

    def test_sponsored_listing_is_never_a_service(self):
        item = Listing(source_term="pc repair", title="Handyman available today", description="").finalise()
        item.sponsored = True
        item.finalise()
        self.assertEqual(item.classification, "irrelevant_or_unclear")
        self.assertIn("sponsored", item.signals)

    def test_read_ui_retries_transient_dump_failure(self):
        # uiautomator dump intermittently fails ("could not get idle state"),
        # which silently killed two whole cycles; retry before giving up
        calls = {"n": 0}

        class _Done:
            stdout = "<node text=\"ok\"/>"

        def runner(cmd, **_kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise subprocess.CalledProcessError(1, cmd)
            return _Done()

        xml = monitor.read_ui(runner=runner, sleep_fn=lambda _s: None)
        self.assertEqual(xml, "<node text=\"ok\"/>")
        self.assertEqual(calls["n"], 4)  # 2 failed dumps + dump + cat of the 3rd attempt

    def test_read_ui_gives_up_after_the_attempt_budget(self):
        def runner(cmd, **_kw):
            raise subprocess.CalledProcessError(1, cmd)

        with self.assertRaises(subprocess.SubprocessError):
            monitor.read_ui(runner=runner, attempts=2, sleep_fn=lambda _s: None)

    def test_textbook_titles_are_products(self):
        item = Listing(source_term="software development",
                       title="Textbooks - Anthropology, Human Development, and Software Engineering",
                       description="").finalise()
        self.assertIn("product_dominant", item.signals)

    def test_goods_title_is_not_a_service_just_from_its_description(self):
        # a RAM/parts listing whose detail text mentions "support" is still goods
        item = Listing(source_term="pc repair", title="Assorted laptop sodimm ram DDR3 and DDR4",
                       description="Ships from our store. Support available Monday to Friday.").finalise()
        self.assertNotIn(item.classification, ("explicit_service", "likely_service"))

    def test_docking_station_is_goods(self):
        item = Listing(source_term="pc repair", title="j5create JCD543 USB-C Triple Display Docking Station",
                       description="").finalise()
        self.assertNotIn(item.classification, ("explicit_service", "likely_service"))

    def test_genuine_service_still_reports_with_a_soft_description(self):
        item = Listing(source_term="pc repair", title="Handyman",
                       description="I fix sinks and taps, call me for a quote.").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_goods_with_only_a_weak_action_verb_is_spared(self):
        # "case", "kit", "theme", "screen" etc. + a weak verb (setup/build/
        # design/development) is still a product listing
        goods = [
            "PC fan setup - 120mm, brand new",
            "Laptop cooling pad build quality, like new",
            "Web design theme for WordPress",
            "Phone case development kit for testing",
        ]
        for title in goods:
            item = Listing(source_term="pc repair", title=title, description="").finalise()
            self.assertNotIn(item.classification, ("explicit_service", "likely_service"), title)

    def test_goods_with_a_real_service_noun_in_the_title_still_reports(self):
        for title in ("Dell i7 Touchscreen Laptop / Home Service",
                      "Laptop hinge replacement and repair service",
                      "Screen repair - same day, parts available"):
            item = Listing(source_term="pc repair", title=title, description="").finalise()
            self.assertIn(item.classification, ("explicit_service", "likely_service"), title)

    def test_goods_with_explicit_offer_language_still_reports(self):
        item = Listing(source_term="pc repair", title="Laptop RAM upgrade kit",
                       description="DM me, I can install and test it for you, $30/hr").finalise()
        self.assertEqual(item.classification, "explicit_service")

    def test_store_deduplicates_listing_and_keeps_observations(self):
        with tempfile.TemporaryDirectory() as d:
            store = MonitorStore(Path(d))
            item = Listing(source_term="pc repair", title="PC repair", description="Laptop repair service")
            self.assertTrue(store.save(item))
            self.assertFalse(store.save(item))
            self.assertEqual(len(store.export()), 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
            report = store.write_reports([item], [])
            self.assertIn("PC repair", report.read_text())

    def test_extract_listings_ignores_controls_and_cards(self):
        xml = '<node text="Riverton, ON"/><node text="Filters"/><node text="$10"/><node text="PC and Laptop repair"/><node text="Riverton · 5 km"/><node text="$20"/><node text="Web design service"/><node text="Lakeside · 30 km"/>'
        items = extract_listings(xml, "pc repair", "test", capture=False)
        self.assertEqual([x.title for x in items], ["PC and Laptop repair", "Web design service"])

    def test_extract_listings_ignores_notification_controls(self):
        xml = (
            '<node text="close"/><node text="Notify me"/>'
            '<node text="Get notified when new listings match this criteria."/>'
            '<node text="HP laptop repair shop"/>'
        )
        items = extract_listings(xml, "laptop repair", "test", capture=False)
        self.assertEqual([x.title for x in items], ["HP laptop repair shop"])

    def test_ui_chrome_labels_are_dropped(self):
        chrome = [
            "Just updated", "See More Options", "See more options for Handyman",
            "Know before you click", "Start free. 30 days, no credit card.",
            "Seller image for Dex AI Agent", "Related to what you viewed",
            "Hide these listings", "Deal of the day", "Sponsored", "Just listed",
        ]
        for label in chrome:
            xml = f'<node text="{label}"/><node text="Handyman"/>'
            items = extract_listings(xml, "pc repair", "test", capture=False)
            self.assertEqual([x.title for x in items], ["Handyman"], f"chrome kept: {label}")

    def test_seller_and_price_prefix_is_stripped(self):
        self.assertEqual(normalise_title("Free · Fall cleanup"), "Fall cleanup")
        self.assertEqual(normalise_title("Muse · Let Muse manage your calendar"), "Let Muse manage your calendar")
        self.assertEqual(normalise_title("$199 · Website design"), "Website design")

    def test_boM_padding_is_removed(self):
        self.assertEqual(normalise_title("Handyman﻿﻿"), "Handyman")
        self.assertEqual(normalise_title("PC\xa0repair\xa0service"), "PC repair service")

    def test_card_fragments_collapse_to_one_listing(self):
        xml = (
            '<node text="Replit"/><node text="Replit, replit.com"/><node text="replit.com"/>'
            '<node text="Handyman"/><node text="Handyman services"/>'
        )
        items = extract_listings(xml, "pc repair", "test", capture=False)
        self.assertEqual([x.title for x in items], ["Replit, replit.com", "Handyman services"])

    def test_collapsed_fragments_keep_longest_title(self):
        xml = '<node text="Atlas Computers"/><node text="Atlas Computers &amp; Electronics"/>'
        items = extract_listings(xml, "pc repair", "test", capture=False)
        self.assertEqual([x.title for x in items], ["Atlas Computers & Electronics"])


if __name__ == "__main__":
    unittest.main()
