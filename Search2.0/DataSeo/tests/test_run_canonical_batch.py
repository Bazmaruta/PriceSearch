import os
import sys
import time
import unittest
from unittest import mock

os.environ["DATABASE_URL"] = "postgresql://postgres@localhost:5432/pricesearch_test"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import bd_store_search as b
import run_canonical_batch as rcb


TEST_ITEMS = [
    {"store": "Woolworths", "url": "https://www.woolworths.com.au/search?searchTerm=x"},
    {"store": "Coles", "url": "https://www.coles.com.au/search?q=x"},
    {"store": "ALDI", "url": "https://www.aldi.com.au/results?q=x"},
    {"store": "IGA", "url": "https://www.igashop.com.au/search/1?q=x"},
    {"store": "Harris Farm", "url": "https://harrisfarm.com.au", "base": "https://harrisfarm.com.au"},
]

DS_ITEMS = [it for it in TEST_ITEMS if "base" not in it]
DCA_ITEMS = [it for it in TEST_ITEMS if "base" in it]


def seed_canonical(ids):
    with db.get_conn() as conn:
        for cid in ids:
            conn.execute(
                "INSERT INTO canonical (canonical_id, country_code, canonical_name) VALUES (%s, %s, %s) "
                "ON CONFLICT (canonical_id, country_code) DO NOTHING",
                (cid, "AU", f"{cid} test product"),
            )


def clear_tables():
    with db.get_conn() as conn:
        conn.execute("TRUNCATE canonical, scrape_runs, stores, woolworths, coles, aldi, harris_farm, iga, pending_jobs, job_history RESTART IDENTITY CASCADE")


def fake_download_records(api_key, snapshot_id):
    records = []
    for it in DS_ITEMS:
        records.append({"input": {"url": it["url"]}, "markdown": f"markdown for {it['store']}"})
    return records


def fake_extract_products(store, markdown):
    return [{"name": f"{store} Test Product", "price": 1.23, "url": f"https://{store}.example/x", "image_url": f"https://{store}.example/i.jpg"}]


def fake_dca_rows(api_key, collection_id):
    return [{"product_name": "Harris Farm Test Product", "price": {"value": 4.56, "currency": "AUD"}, "product_url": "https://harrisfarm.com.au/p/x", "image_url": "https://harrisfarm.com.au/i.jpg"}]


def fake_dca_record_to_product(rec):
    return {"name": rec["product_name"], "price": 4.56, "url": rec["product_url"], "image_url": rec["image_url"]}


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        db.init_schema()
        clear_tables()

    def run_main(self, argv, patch_network=True, extract_fn=None, dca_fn=None):
        with mock.patch.object(sys, "argv", argv):
            if patch_network:
                patchers = [
                    mock.patch.object(b, "build_urls", return_value=TEST_ITEMS),
                    mock.patch.object(b, "trigger", return_value={"snapshot_id": "sd_test"}),
                    mock.patch.object(b, "trigger_dca", return_value="j_test"),
                    mock.patch.object(b, "poll", return_value=None),
                    mock.patch.object(b, "poll_dca", return_value=None),
                    mock.patch.object(b, "download", side_effect=fake_download_records),
                    mock.patch.object(b, "download_dca", side_effect=fake_dca_rows),
                    mock.patch.object(b, "extract_products", side_effect=extract_fn or fake_extract_products),
                    mock.patch.object(b, "dca_record_to_product", side_effect=dca_fn or fake_dca_record_to_product),
                ]
                for p in patchers:
                    p.start()
                try:
                    rcb.main()
                finally:
                    for p in patchers:
                        p.stop()
            else:
                rcb.main()


class TestEligibility(BaseTestCase):
    def test_never_run_first_then_oldest(self):
        seed_canonical(["p1", "p2", "p3"])
        rows = db.get_eligible(batch_size=2)
        self.assertEqual([r["canonical_id"] for r in rows], ["p1", "p2"])
        db.mark_scraped("p1")
        rows = db.get_eligible(batch_size=2)
        # p1 just scraped (<24h) -> skipped; never-run p2,p3 come first
        self.assertEqual([r["canonical_id"] for r in rows], ["p2", "p3"])

    def test_24h_elapsed_becomes_eligible(self):
        seed_canonical(["p1", "p2"])
        db.mark_scraped("p1")
        with db.get_conn() as conn:
            conn.execute("UPDATE scrape_runs SET last_scraped_at = now() - interval '25 hours' WHERE canonical_id = 'p1'")
        rows = db.get_eligible(batch_size=5)
        ids = {r["canonical_id"] for r in rows}
        self.assertIn("p1", ids)
        self.assertIn("p2", ids)

    def test_force_bypasses_24h(self):
        seed_canonical(["p1"])
        db.mark_scraped("p1")
        self.assertEqual(db.get_eligible(batch_size=5), [])
        rows = db.get_eligible(batch_size=5, force=True)
        self.assertEqual([r["canonical_id"] for r in rows], ["p1"])


class TestBatchSelection(BaseTestCase):
    def test_test_mode_one_batch_of_five_then_stop(self):
        seed_canonical([f"p{i}" for i in range(6)])
        self.run_main(["run_canonical_batch.py"])
        with db.get_conn() as conn:
            marked = [r["canonical_id"] for r in conn.execute("SELECT canonical_id FROM scrape_runs ORDER BY canonical_id").fetchall()]
        self.assertEqual(len(marked), 5)

    def test_all_mode_processes_all(self):
        seed_canonical([f"p{i}" for i in range(11)])
        self.run_main(["run_canonical_batch.py", "--all"])
        with db.get_conn() as conn:
            marked = conn.execute("SELECT count(*) AS c FROM scrape_runs").fetchone()["c"]
        self.assertEqual(marked, 11)


class TestRepeatAndIds(BaseTestCase):
    def test_repeat_reruns_last_batch(self):
        seed_canonical([f"p{i}" for i in range(10)])
        self.run_main(["run_canonical_batch.py"])  # p0..p4 done
        with db.get_conn() as conn:
            before = [r["canonical_id"] for r in conn.execute("SELECT canonical_id FROM scrape_runs ORDER BY canonical_id").fetchall()]
        self.run_main(["run_canonical_batch.py", "--repeat"])
        with db.get_conn() as conn:
            after = [r["canonical_id"] for r in conn.execute("SELECT canonical_id FROM scrape_runs ORDER BY canonical_id").fetchall()]
        # repeat must not add new products
        self.assertEqual(sorted(after), sorted(before))

    def test_ids_reruns_specific(self):
        seed_canonical([f"p{i}" for i in range(10)])
        self.run_main(["run_canonical_batch.py", "--ids", "p3,p7"])
        with db.get_conn() as conn:
            marked = {r["canonical_id"] for r in conn.execute("SELECT canonical_id FROM scrape_runs").fetchall()}
        self.assertEqual(marked, {"p3", "p7"})


class TestPersistence(BaseTestCase):
    def test_results_saved_to_store_tables(self):
        seed_canonical(["p1"])
        self.run_main(["run_canonical_batch.py"])
        with db.get_conn() as conn:
            ww = conn.execute("SELECT product_name, price, product_url, match_source FROM woolworths WHERE canonical_id = 'p1'").fetchone()
            hf = conn.execute("SELECT product_name, price, product_url, match_source FROM harris_farm WHERE canonical_id = 'p1'").fetchone()
        self.assertEqual(ww["product_name"], "Woolworths Test Product")
        self.assertEqual(ww["price"], 1.23)
        self.assertEqual(ww["match_source"], "brand ok")
        self.assertEqual(hf["product_name"], "Harris Farm Test Product")
        self.assertEqual(hf["price"], 4.56)
        self.assertEqual(hf["match_source"], "brand ok")

    def test_no_match_still_saves_empty_row(self):
        seed_canonical(["p1"])

        def garbage(store, markdown):
            return [{"name": "Strawberries 250g", "price": 6.29, "url": "u", "image_url": "i"}]

        def garbage_dca(rec):
            return {"name": "Strawberries 250g", "price": 6.29, "url": "u", "image_url": "i"}

        self.run_main(["run_canonical_batch.py"], extract_fn=garbage, dca_fn=garbage_dca)
        with db.get_conn() as conn:
            for tbl in ["woolworths", "coles", "aldi", "iga"]:
                row = conn.execute(f"SELECT price, product_name, product_url FROM {tbl} WHERE canonical_id = 'p1'").fetchone()
                self.assertIsNotNone(row, f"{tbl} should still have a row")
                self.assertIsNone(row["price"], f"{tbl} price should be NULL when no match")
                self.assertIsNone(row["product_name"])
                self.assertIsNone(row["product_url"])

    def test_dry_run_no_save(self):
        seed_canonical(["p1"])
        self.run_main(["run_canonical_batch.py", "--no-save"])
        with db.get_conn() as conn:
            ww = conn.execute("SELECT count(*) AS c FROM woolworths WHERE canonical_id = 'p1'").fetchone()["c"]
            marked = conn.execute("SELECT count(*) AS c FROM scrape_runs").fetchone()["c"]
        self.assertEqual(ww, 0)
        self.assertEqual(marked, 0)

    def test_repeat_clears_existing_store_rows_before_rerun(self):
        seed_canonical(["p1"])
        # first run saves rows + marks scraped
        self.run_main(["run_canonical_batch.py"])
        # mutate store rows to a stale sentinel price to prove they existed
        with db.get_conn() as conn:
            conn.execute("UPDATE woolworths SET price = 999 WHERE canonical_id = 'p1'")
            n_before = conn.execute("SELECT count(*) AS c FROM woolworths WHERE canonical_id = 'p1'").fetchone()["c"]
        self.assertEqual(n_before, 1)
        # repeat must clear + re-save with fresh mocked values (1.23)
        self.run_main(["run_canonical_batch.py", "--repeat"])
        with db.get_conn() as conn:
            row = conn.execute("SELECT price FROM woolworths WHERE canonical_id = 'p1'").fetchone()
        self.assertEqual(row["price"], 1.23)


class TestCrashRecovery(BaseTestCase):
    def test_unmarked_product_is_retried_marked_is_skipped(self):
        seed_canonical(["p1", "p2"])
        db.mark_scraped("p1")  # simulates p1 completed before the crash
        with db.get_conn() as conn:
            conn.execute("UPDATE scrape_runs SET last_scraped_at = now() - interval '25 hours' WHERE canonical_id = 'p1'")
        # p1 now eligible again (25h), p2 never run
        rows = db.get_eligible(batch_size=5)
        self.assertEqual({r["canonical_id"] for r in rows}, {"p1", "p2"})


class TestResume(BaseTestCase):
    def test_resume_reuses_pending_job_without_trigger(self):
        seed_canonical(["p1"])
        db.save_pending_job("p1", "sd_paid", "j_paid")

        with mock.patch.object(sys, "argv", ["run_canonical_batch.py", "--resume"]), \
             mock.patch.object(b, "build_urls", return_value=TEST_ITEMS), \
             mock.patch.object(b, "trigger") as m_trig, \
             mock.patch.object(b, "trigger_dca") as m_dca, \
             mock.patch.object(b, "poll", return_value=None), \
             mock.patch.object(b, "poll_dca", return_value=None), \
             mock.patch.object(b, "download", side_effect=fake_download_records), \
             mock.patch.object(b, "download_dca", side_effect=fake_dca_rows), \
             mock.patch.object(b, "extract_products", side_effect=fake_extract_products), \
             mock.patch.object(b, "dca_record_to_product", side_effect=fake_dca_record_to_product):
            rcb.main()

        self.assertEqual(m_trig.call_count, 0, "must not re-trigger dataset on --resume")
        self.assertEqual(m_dca.call_count, 0, "must not re-trigger DCA on --resume")
        with db.get_conn() as conn:
            ww = conn.execute("SELECT product_name, price FROM woolworths WHERE canonical_id = 'p1'").fetchone()
        self.assertEqual(ww["product_name"], "Woolworths Test Product")
        self.assertEqual(ww["price"], 1.23)
        self.assertIsNone(db.get_pending_job("p1"))

    def test_resume_triggers_when_no_pending_job(self):
        seed_canonical(["p1"])
        with mock.patch.object(sys, "argv", ["run_canonical_batch.py", "--resume"]), \
             mock.patch.object(b, "build_urls", return_value=TEST_ITEMS), \
             mock.patch.object(b, "trigger", return_value={"snapshot_id": "sd_new"}) as m_trig, \
             mock.patch.object(b, "trigger_dca", return_value="j_new") as m_dca, \
             mock.patch.object(b, "poll", return_value=None), \
             mock.patch.object(b, "poll_dca", return_value=None), \
             mock.patch.object(b, "download", side_effect=fake_download_records), \
             mock.patch.object(b, "download_dca", side_effect=fake_dca_rows), \
             mock.patch.object(b, "extract_products", side_effect=fake_extract_products), \
             mock.patch.object(b, "dca_record_to_product", side_effect=fake_dca_record_to_product):
            rcb.main()
        self.assertEqual(m_trig.call_count, 1)
        self.assertEqual(m_dca.call_count, 1)


class TestJobHistory(BaseTestCase):
    def test_keeps_newest_five_and_updates_status(self):
        seed_canonical(["p1"])
        for i in range(7):
            db.save_job_history("p1", f"sd_{i}", f"j_{i}", status="partial")
        rows = db.get_job_history(["p1"])
        self.assertEqual(len(rows), 5)
        snapshots = [r["snapshot_id"] for r in rows]
        self.assertEqual(snapshots, ["sd_6", "sd_5", "sd_4", "sd_3", "sd_2"])

        db.set_job_status("p1", "sd_6", "ok")
        rows = db.get_job_history(["p1"])
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["snapshot_id"], "sd_6")

    def test_multiple_products_independent(self):
        seed_canonical(["p1", "p2"])
        for i in range(3):
            db.save_job_history("p1", f"a_{i}", f"ja_{i}")
        db.save_job_history("p2", "b_0", "jb_0")
        self.assertEqual(len(db.get_job_history(["p1"])), 3)
        self.assertEqual(len(db.get_job_history(["p2"])), 1)
        self.assertEqual(len(db.get_job_history()), 4)


class TestParallelism(BaseTestCase):
    def test_batch_runs_products_in_parallel_and_waits(self):
        seed_canonical(["p1", "p2", "p3"])

        def slow_poll(api_key, snapshot_id, *args, **kwargs):
            time.sleep(0.4)
            return None

        batch = [
            {"canonical_id": f"p{i}", "canonical_name": f"p{i} test product", "brand": None, "product_name": None,
             "size_value": None, "size_unit": None, "size_basis": None, "pack_count": None, "variant": None}
            for i in range(1, 4)
        ]
        with mock.patch.object(b, "build_urls", return_value=TEST_ITEMS), \
             mock.patch.object(b, "trigger", return_value={"snapshot_id": "sd_test"}), \
             mock.patch.object(b, "trigger_dca", return_value="j_test"), \
             mock.patch.object(b, "poll", side_effect=slow_poll), \
             mock.patch.object(b, "poll_dca", return_value=None), \
             mock.patch.object(b, "download", side_effect=fake_download_records), \
             mock.patch.object(b, "download_dca", side_effect=fake_dca_rows), \
             mock.patch.object(b, "extract_products", side_effect=fake_extract_products), \
             mock.patch.object(b, "dca_record_to_product", side_effect=fake_dca_record_to_product):
            start = time.time()
            results = rcb.process_batch("KEY", batch)
            elapsed = time.time() - start
        self.assertEqual(len(results), 3)
        # 3 products * 0.4s poll; parallel => ~0.4s total, serial would be ~1.2s+
        self.assertLess(elapsed, 1.0)
        with db.get_conn() as conn:
            marked = conn.execute("SELECT count(*) AS c FROM scrape_runs").fetchone()["c"]
        self.assertEqual(marked, 3)


class TestTokenNearMatch(unittest.TestCase):
    def test_plural_near_match(self):
        self.assertTrue(rcb.tokens_near("banana", "bananas"))
        self.assertTrue(rcb.tokens_near("tomato", "tomatoes"))
        self.assertTrue(rcb.tokens_near("cookie", "cookies"))
        self.assertFalse(rcb.tokens_near("banana", "banana-bread"))
        self.assertFalse(rcb.tokens_near("apple", "apricot"))

    def test_abbreviation_near_match(self):
        # "antibac" (Sard variant) must match "antibacterial" (store name)
        self.assertTrue(rcb.tokens_near("antibac", "antibacterial"))
        self.assertTrue(rcb.tokens_near("antibact", "antibacterial"))

    def test_single_token_canonical_full_coverage_survives_distractors(self):
        # canonical "Chokoes" vs "Fresh Chokoes each": all canonical tokens present,
        # so the extra words must not sink the score below threshold
        c = {"canonical_id": "chokoes", "canonical_name": "Chokoes",
             "brand": None, "product_name": "Chokoes", "size_value": None, "size_unit": None,
             "size_basis": None, "pack_count": None, "variant": None}
        cand = {"name": "Fresh Chokoes each", "price": 2.21}
        self.assertGreater(rcb.score_match(c, cand), rcb.MIN_MATCH_SCORE)

    def test_cavendish_banana_scores_above_threshold(self):
        c = {"canonical_id": "cavendish banana", "canonical_name": "Cavendish Banana",
             "brand": None, "product_name": "Cavendish Banana", "size_value": None, "size_unit": None,
             "size_basis": None, "pack_count": None, "variant": "Cavendish"}
        cand = {"name": "Cavendish Bananas each", "price": 0.83}
        self.assertGreater(rcb.score_match(c, cand), rcb.MIN_MATCH_SCORE)


class TestFirstActualPrice(unittest.TestCase):
    def test_iga_was_price_skipped(self):
        chunk = "\nwas $8.35\n\n$5.20\n\n$0.74 per 100g\n"
        self.assertEqual(b.first_actual_price(chunk), "5.20")

    def test_woolworths_save_price_skipped(self):
        chunk = "\nSAVE $1.00\n\n$2.00\n\n$3.00 $2.00 / 1L\n"
        self.assertEqual(b.first_actual_price(chunk), "2.00")

    def test_plain_price(self):
        self.assertEqual(b.first_actual_price("$7.30"), "7.30")


class TestBestMatch(BaseTestCase):
    def setUp(self):
        pass  # matcher is pure logic, no DB needed

    def canonical(self, **kw):
        base = {
            "canonical_name": "a2 full cream milk 2l",
            "brand": "a2",
            "product_name": "Full Cream Milk",
            "size_value": 2.0,
            "size_unit": "L",
            "size_basis": "total",
            "pack_count": None,
            "variant": None,
        }
        base.update(kw)
        return base

    def test_exact_size_beats_wrong_size(self):
        c = self.canonical()
        exact = {"name": "a2 Milk Full Cream Milk 2L", "price": 7.30}
        wrong = {"name": "a2 Milk Full Cream Uht Milk 1L", "price": 3.90}
        self.assertGreater(rcb.score_match(c, exact), rcb.score_match(c, wrong))
        self.assertEqual(rcb.pick_best(c, [wrong, exact], "Coles")[0]["name"], "a2 Milk Full Cream Milk 2L")

    def test_brand_match_beats_other_brand(self):
        c = self.canonical()
        a2 = {"name": "a2 Milk Full Cream Milk 2L", "price": 7.30}
        other = {"name": "Pauls Full Cream Milk 2L", "price": 4.50}
        self.assertGreater(rcb.score_match(c, a2), rcb.score_match(c, other))
        self.assertEqual(rcb.pick_best(c, [other, a2], "Coles")[0]["name"], "a2 Milk Full Cream Milk 2L")

    def test_name_only_canonical_parses_size(self):
        c = self.canonical(canonical_name="so good almond milk 1l", size_value=None, size_unit=None, brand=None, product_name=None)
        exact = {"name": "So Good Almond Milk 1L", "price": 3.00}
        wrong = {"name": "So Good Almond Milk 3L", "price": 9.00}
        self.assertGreater(rcb.score_match(c, exact), rcb.score_match(c, wrong))
        self.assertEqual(rcb.pick_best(c, [wrong, exact], "Coles")[0]["name"], "So Good Almond Milk 1L")

    def test_irrelevant_fallback_rejected_below_threshold(self):
        c = self.canonical(brand=None)  # no brand gate so we test the score threshold
        garbage = [
            {"name": "Strawberries 250g", "price": 6.29},
            {"name": "Cucumber Lebanese Each", "price": 1.34},
            {"name": "Broccoli Head Each", "price": 2.50},
        ]
        best, score, _ = rcb.pick_best(c, garbage, "Harris Farm")
        self.assertIsNone(best)
        self.assertIsNotNone(score)
        self.assertLess(score, rcb.MIN_MATCH_SCORE)

    def test_wrong_brand_rejected_hard_without_ai(self):
        c = self.canonical(canonical_name="a2 full cream uht milk 1l", product_name="Full Cream UHT Milk",
                           size_value=1.0, size_unit="L")
        wrong = {"name": "Black & Gold Uht Dairy Full Milk 1L", "price": 1.85}
        right = {"name": "A2 Milk Full Cream UHT Milk 1L", "price": 3.90}
        best, _, decision = rcb.pick_best(c, [wrong], "IGA", confirm_fn=None)
        self.assertIsNone(best)  # wrong brand must NOT be saved
        self.assertEqual(decision, "wrong brand")
        best, _, _ = rcb.pick_best(c, [wrong, right], "IGA", confirm_fn=None)
        self.assertEqual(best["name"], "A2 Milk Full Cream UHT Milk 1L")

    def test_ai_confirms_unnamed_brand(self):
        c = self.canonical(canonical_name="3m mini command hooks 6 pack", brand="3M",
                           size_value=6.0, size_unit="pack")
        cand = {"name": "Command Clear Mini Hooks | 6 Pack", "price": 8.30, "url": "u"}
        best, _, decision = rcb.pick_best(c, [cand], "Coles", confirm_fn=lambda *a: (True, "it's 3M"))
        self.assertIs(best, cand)
        self.assertEqual(decision, "ai confirmed")

    def test_ai_rejects_wrong_brand(self):
        c = self.canonical(canonical_name="a2 full cream uht milk 1l", product_name="Full Cream UHT Milk",
                           size_value=1.0, size_unit="L")
        wrong = {"name": "Black & Gold Uht Dairy Full Milk 1L", "price": 1.85}
        best, _, decision = rcb.pick_best(c, [wrong], "IGA", confirm_fn=lambda *a: (False, "different brand"))
        self.assertIsNone(best)
        self.assertEqual(decision, "ai rejected")

    def test_variant_doubt_escalates_to_ai_when_brand_matches(self):
        # brand matches (a2), but 'uht' is missing from the candidate -> AI consulted
        c = self.canonical(canonical_name="a2 full cream uht milk 1l", product_name="Full Cream UHT Milk",
                           size_value=1.0, size_unit="L")
        fresh = {"name": "a2 Milk Full Cream Milk 1L", "price": 4.10, "url": "u"}
        self.assertIn("uht", rcb.missing_descriptor_tokens(c, fresh["name"]))
        # AI says NO -> not saved
        best, _, decision = rcb.pick_best(c, [fresh], "Woolworths", confirm_fn=lambda *a: (False, "fresh, not UHT"))
        self.assertIsNone(best)
        self.assertEqual(decision, "ai rejected")
        # AI says YES -> saved
        best, _, decision = rcb.pick_best(c, [fresh], "Woolworths", confirm_fn=lambda *a: (True, "same product"))
        self.assertIs(best, fresh)
        self.assertEqual(decision, "ai confirmed")

    def test_generic_brand_token_alone_is_not_a_match(self):
        # "Coach House Dairy" vs "Bethune Lane Dairy": shared generic "dairy" must NOT pass
        c = self.canonical(canonical_name="Coach House Dairy Chocolate Milk 300mL", brand="Coach House Dairy",
                           product_name="Chocolate Milk", size_value=300.0, size_unit="mL")
        self.assertFalse(rcb.brand_matches(c, "Bethune Lane Dairy Milk Chocolate 300ml"))
        self.assertTrue(rcb.brand_matches(c, "Coach House Dairy Chocolate Milk 300mL"))

    def test_single_token_brand_still_required(self):
        c = self.canonical(canonical_name="Bega Tasty Cheese Block 500g", brand="Bega",
                           product_name="Tasty Cheese Block", size_value=500.0, size_unit="g")
        self.assertTrue(rcb.brand_matches(c, "Bega Tasty Cheese Block 500g"))
        self.assertFalse(rcb.brand_matches(c, "Tasty Cheese Block 500g"))  # store-brand generic

    def test_possessive_apostrophe_not_a_brand_match(self):
        # "Abbott's" vs "Jesse's" must NOT match on the stray 's' token
        c = self.canonical(canonical_name="Abbott's Sourdough Rye Bread 760g", brand="Abbott's",
                           product_name="Sourdough Rye Bread", size_value=760.0, size_unit="g")
        self.assertFalse(rcb.brand_matches(c, "Jesse's Rye Sourdough Bread 900g"))
        self.assertTrue(rcb.brand_matches(c, "Abbott's Bakery Sourdough Rye Bread 760g"))
        self.assertNotIn("s", rcb.tokenize_name("Abbott's"))

    def test_no_variant_doubt_no_ai_call(self):
        # correct UHT candidate has all descriptor tokens -> no AI, saved as brand ok
        c = self.canonical(canonical_name="a2 full cream uht milk 1l", product_name="Full Cream UHT Milk",
                           size_value=1.0, size_unit="L")
        good = {"name": "a2 Milk Full Cream Long Life UHT 1L", "price": 3.90, "url": "u"}
        self.assertEqual(rcb.missing_descriptor_tokens(c, good["name"]), set())
        calls = []
        confirm_fn = lambda *a: calls.append(1) or (True, "x")
        best, _, decision = rcb.pick_best(c, [good], "Woolworths", confirm_fn=confirm_fn)
        self.assertIs(best, good)
        self.assertEqual(decision, "brand ok")
        self.assertEqual(len(calls), 0)

    def test_real_match_kept_when_threshold_met(self):
        c = self.canonical()
        good = {"name": "a2 Milk Full Cream Milk 2L", "price": 7.30}
        best, score, _ = rcb.pick_best(c, [good], "Coles")
        self.assertIs(best, good)
        self.assertGreaterEqual(score, rcb.MIN_MATCH_SCORE)

    def test_closest_size_saved_when_exact_not_stocked(self):
        c = self.canonical(canonical_name="Blackberries 170g", product_name="Blackberries",
                           size_value=170.0, size_unit="g", variant=None, brand=None)
        cands = [
            {"name": "Coles Blackberries | 125g", "price": 6.50, "url": "u1"},
            {"name": "Frozen Blackberries 500g", "price": 5.00, "url": "u2"},
        ]
        best, _, decision = rcb.pick_best(c, cands, "Coles")
        self.assertEqual(best["name"], "Coles Blackberries | 125g")
        self.assertEqual(decision, "closest size")

    def test_exact_size_beats_closest_size(self):
        c = self.canonical(canonical_name="Blackberries 170g", product_name="Blackberries",
                           size_value=170.0, size_unit="g", variant=None, brand=None)
        cands = [
            {"name": "Woolworths Blackberries 125g", "price": 7.00, "url": "u3"},
            {"name": "Woolworths Blackberries 170g", "price": 8.00, "url": "u4"},
        ]
        best, _, decision = rcb.pick_best(c, cands, "Woolworths")
        self.assertEqual(best["name"], "Woolworths Blackberries 170g")
        self.assertEqual(decision, "brand ok")

    def test_head_noun_rejects_different_product_kind(self):
        # "Custard & Pink Lady Apple Scrolls" is a pastry, not the custard apple fruit
        c = {"canonical_id": "custard apple", "canonical_name": "Custard Apple",
             "brand": None, "product_name": "Custard Apple", "size_value": None, "size_unit": None,
             "size_basis": None, "pack_count": None, "variant": None}
        pastry = {"name": "Woolworths Custard & Pink Lady Apple Scrolls 2 pack", "price": 4.10, "url": "u1"}
        fruit = {"name": "Apple Custard Each", "price": 6.99, "url": "u2"}
        self.assertFalse(rcb.head_compatible(c, pastry["name"]))
        self.assertTrue(rcb.head_compatible(c, fruit["name"]))
        best, _, decision = rcb.pick_best(c, [pastry], "Woolworths", confirm_fn=lambda *a, **k: (False, "pastry, not fruit"))
        self.assertIsNone(best)
        self.assertEqual(decision, "ai rejected")
        best, _, decision = rcb.pick_best(c, [fruit], "Harris Farm", confirm_fn=lambda *a, **k: (True, "the fruit"))
        self.assertEqual(best["name"], "Apple Custard Each")

    def test_packaging_word_head_does_not_block_same_product(self):
        # "Block" / "Gel" / glued size ("Orange100g") are packaging/form words,
        # not product-kind signals: the same product must reach the AI doubt loop.
        c = {"canonical_id": "lindt orange intense excellence chocolate 100g",
             "canonical_name": "Lindt Orange Intense Excellence Chocolate 100g",
             "brand": "Lindt", "product_name": "Excellence Chocolate", "variant": "Orange Intense",
             "size_value": 100.0, "size_unit": "g"}
        self.assertEqual(rcb.head_token("Lindt Excellence Orange Dark Chocolate Block | 100g", ["lindt"]), "chocolate")
        self.assertTrue(rcb.head_compatible(c, "Lindt Excellence Orange Dark Chocolate Block | 100g"))
        self.assertEqual(rcb.head_token("Lindt Excellence Intense Orange100g", ["lindt"]), "orange")
        self.assertTrue(rcb.head_compatible(c, "Lindt Excellence Intense Orange100g"))

        lp = {"canonical_id": "liquid-plumr urgent clear clog remover 502ml",
              "canonical_name": "Liquid-Plumr Urgent Clear Clog Remover 502ml",
              "brand": "Liquid-Plumr", "product_name": "Urgent Clear Clog Remover", "variant": None,
              "size_value": 502.0, "size_unit": "ml"}
        self.assertEqual(rcb.head_token("Liquid-Plumr Urgent Clear Gel 502mL", ["liquid", "plumr"]), "clear")
        self.assertTrue(rcb.head_compatible(lp, "Liquid-Plumr Urgent Clear Gel 502mL"))

    def test_compound_word_head_matches_split_descriptor(self):
        # "Chickpeas" (one word) is the same product as "Chick Peas" (two tokens)
        c = {"canonical_id": "macro organic chick peas 425g",
             "canonical_name": "Macro Organic Chick Peas 425g",
             "brand": "Macro Organic", "product_name": "Chick Peas", "variant": None,
             "size_value": 425.0, "size_unit": "g"}
        self.assertTrue(rcb.head_compatible(c, "Macro Organic Chickpeas 425g"))
        # but a genuinely different head noun must still be rejected
        c2 = {"canonical_id": "custard apple", "canonical_name": "Custard Apple",
              "brand": None, "product_name": "Custard Apple", "variant": None,
              "size_value": None, "size_unit": None}
        self.assertFalse(rcb.head_compatible(c2, "Woolworths Custard & Pink Lady Apple Scrolls 2 pack"))

    def test_compound_brand_matches_spaced_variant(self):
        # "MyEcoBag" (one token) is the same brand as "My Eco Bag" (three tokens)
        c = {"canonical_id": "myecobag compostable bin bags 25 x 8l",
             "canonical_name": "MyEcoBag Compostable Bin Bags 25 x 8L",
             "brand": "MyEcoBag", "product_name": "Compostable Bin Bags", "variant": "Compostable",
             "size_value": 8.0, "size_unit": "L"}
        self.assertTrue(rcb.brand_matches(c, "My Eco Bag Compostable Bin Liners Mini 25 pack"))
        # unrelated brands still fail
        c2 = {"canonical_id": "x", "canonical_name": "x", "brand": "Coach House Dairy",
              "product_name": "x", "variant": None, "size_value": None, "size_unit": None}
        self.assertFalse(rcb.brand_matches(c2, "Bethune Lane Dairy Milk Chocolate 300ml"))
        c3 = {"canonical_id": "x", "canonical_name": "x", "brand": "Bega",
              "product_name": "x", "variant": None, "size_value": None, "size_unit": None}
        self.assertFalse(rcb.brand_matches(c3, "Tasty Cheese Block 500g"))

    def test_closest_size_runs_size_tolerant_ai_confirm(self):
        c = self.canonical(canonical_name="Blackberries 170g", product_name="Blackberries",
                           size_value=170.0, size_unit="g", variant=None, brand=None)
        cands = [{"name": "Frozen Blackberries 500g", "price": 5.00, "url": "uf"}]

        def reject(canonical, product, store, size_tolerant=False):
            self.assertTrue(size_tolerant)  # must use the size-tolerant prompt
            return False, "frozen != fresh"

        best, _, decision = rcb.pick_best(c, cands, "Coles", confirm_fn=reject)
        self.assertIsNone(best)
        self.assertEqual(decision, "ai rejected")

        best, _, decision = rcb.pick_best(c, cands, "Coles", confirm_fn=lambda *a, **k: (True, "same product"))
        self.assertEqual(best["name"], "Frozen Blackberries 500g")
        self.assertEqual(decision, "closest size")

    def test_parse_size_variants(self):
        self.assertEqual(rcb.parse_size("a2 Milk 2L"), (2.0, "l"))
        self.assertEqual(rcb.parse_size("Milk 2 L"), (2.0, "l"))
        self.assertEqual(rcb.parse_size("Cheese 500g"), (500.0, "g"))
        self.assertEqual(rcb.parse_size("Biscuits 6 pack"), (6.0, "pack"))
        self.assertIsNone(rcb.parse_size("Milk Full Cream"))


if __name__ == "__main__":
    unittest.main()
