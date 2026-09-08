"""Offline tests. No network, no keys, no provider contact.

The two that matter most are the compliance ones: they are the only thing
standing between this tool and the failure mode the research brief was written
to prevent.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lisearch.fire as _F
_F.AUTO_PRUNE = False          # tests must never touch the operator's real cache

from lisearch import audience as aud
from lisearch.adapters.base import split_headline
from lisearch.compliance import (ComplianceError, apply_suppression,
                                 assert_allowed_host, assert_no_session_auth, is_eea)
from lisearch.fire import excluded, score
from lisearch.lead import canonical_account, dedupe, make_lead, merge


class TestCompliance(unittest.TestCase):
    def test_refuses_linkedin_host(self):
        for u in ("https://www.linkedin.com/in/x",
                  "https://linkedin.com/search/results/people",
                  "http://LINKEDIN.COM/in/y"):
            with self.assertRaises(ComplianceError):
                assert_allowed_host(u)

    def test_allows_providers(self):
        for u in ("https://api.exa.ai/search",
                  "https://api.peopledatalabs.com/v5/person/search",
                  "https://api.coresignal.com/cdapi/v2/x"):
            assert_allowed_host(u)

    def test_lookalike_host_not_confused_for_linkedin(self):
        assert_allowed_host("https://notlinkedin.com.example.org/x")

    def test_refuses_session_cookie(self):
        for h in ({"Cookie": "li_at=abc"}, {"cookie": "JSESSIONID=x"},
                  {"X-Auth": "bscookie=zz"}):
            with self.assertRaises(ComplianceError):
                assert_no_session_auth("evil", h)

    def test_api_key_header_is_fine(self):
        assert_no_session_auth("exa", {"x-api-key": "sk-live-123"})

    def test_eea_flag(self):
        self.assertTrue(is_eea("BG"))
        self.assertTrue(is_eea("gb"))
        self.assertFalse(is_eea("AE"))
        self.assertFalse(is_eea(""))


class TestAccountIdentity(unittest.TestCase):
    def test_extracts_slug(self):
        for url in ("https://www.linkedin.com/in/loaifakir",
                    "https://linkedin.com/in/loaifakir/",
                    "https://ae.linkedin.com/in/loaifakir?trk=x",
                    "linkedin.com/in/loaifakir#about"):
            self.assertEqual(canonical_account(url), "loaifakir")

    def test_rejects_non_profiles(self):
        for url in ("https://www.linkedin.com/company/acme",
                    "https://www.linkedin.com/school/aub",
                    "https://www.linkedin.com/posts/x-activity-123",
                    "https://example.com/in/bob",
                    "", None, "https://www.linkedin.com/in/me"):
            self.assertIsNone(canonical_account(url))

    def test_bare_slug_passes_through(self):
        self.assertEqual(canonical_account("hamedgh"), "hamedgh")


class TestHeadline(unittest.TestCase):
    def test_three_part(self):
        r = split_headline("Sami Haddad - Managing Director - Cedar Hotels | LinkedIn")
        self.assertEqual(r["full_name"], "Sami Haddad")
        self.assertEqual(r["title"], "Managing Director")
        self.assertEqual(r["company"], "Cedar Hotels")

    def test_at_form(self):
        r = split_headline("Asa Khezri - Owner at ASA Real Estate | LinkedIn")
        self.assertEqual(r["title"], "Owner")
        self.assertEqual(r["company"], "ASA Real Estate")


class TestMergeAndDedupe(unittest.TestCase):
    def test_merge_unions_sources_and_fills_gaps(self):
        a = make_lead("exa", "bob", full_name="Bob", title="CEO")
        b = make_lead("pdl", "bob", full_name="Bob", company="Acme", email="b@acme.com")
        m = merge(a, b)
        self.assertEqual(m["sources"], ["exa", "pdl"])
        self.assertEqual(m["company"], "Acme")
        self.assertEqual(m["email"], "b@acme.com")

    def test_dedupe_collapses_by_account(self):
        leads = [make_lead("exa", "bob"), make_lead("pdl", "bob"), make_lead("exa", "sue")]
        out = dedupe(leads)
        self.assertEqual(len(out), 2)

    def test_freshest_claim_wins(self):
        a = make_lead("coresignal", "bob", freshness_days=100)
        b = make_lead("exa", "bob", freshness_days=0)
        self.assertEqual(merge(a, b)["freshness"], "live")


class TestScoringAndExclusion(unittest.TestCase):
    def setUp(self):
        self.a = aud.new_audience(
            "t", industry="real estate", countries=["AE"], cities=["Dubai"],
            keywords=["brokerage"], exclude_titles=["Agent"])

    def test_owner_in_target_city_outranks_bare_slug(self):
        good = make_lead("exa", "a", full_name="X", title="Owner",
                         company="Dubai Brokerage", location="Dubai, UAE")
        bare = make_lead("openweb", "b")
        self.assertGreater(score(good, self.a), score(bare, self.a))

    def test_corroboration_raises_score(self):
        one = make_lead("exa", "a", full_name="X", title="Owner")
        two = dict(one, sources=["exa", "pdl"])
        self.assertGreater(score(two, self.a), score(one, self.a))

    def test_excluded_title_is_filtered(self):
        self.assertTrue(excluded(make_lead("exa", "a", title="Real Estate Agent"), self.a))
        self.assertFalse(excluded(make_lead("exa", "b", title="Owner"), self.a))


class TestSuppression(unittest.TestCase):
    def test_suppression_is_applied_when_present(self):
        leads = [make_lead("exa", "keepme"), make_lead("exa", "dropme")]
        import lisearch.compliance as C
        orig = C.load_suppression
        C.load_suppression = lambda: {"dropme"}
        try:
            out = apply_suppression(leads)
        finally:
            C.load_suppression = orig
        self.assertEqual([l["linkedin_account"] for l in out], ["keepme"])


class TestAudience(unittest.TestCase):
    def test_owner_preset_and_geo(self):
        a = aud.new_audience("Dubai owners", countries=["AE"], cities=["Dubai"])
        self.assertIn("Owner", a.titles())
        self.assertIn("General Manager", a.titles())      # Gulf/Levant equivalent
        self.assertEqual(a.geo_terms()[0], "Dubai")       # cities lead
        self.assertIn("United Arab Emirates", a.geo_terms())

    def test_explicit_titles_replace_preset(self):
        a = aud.new_audience("x", titles=["Head of Revenue"])
        self.assertEqual(a.titles(), ["Head of Revenue"])

    def test_slug(self):
        self.assertEqual(aud.new_audience("Dubai Real Estate!! Owners")["slug"],
                         "dubai-real-estate-owners")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBlockedDetection(unittest.TestCase):
    """A rate-limit interstitial must never read as 'this market is empty'."""

    def test_detects_interstitial(self):
        from lisearch.adapters.openweb import looks_blocked
        self.assertTrue(looks_blocked("<html>...anomaly detected...</html>"))
        self.assertTrue(looks_blocked("<div class='challenge-form'>"))

    def test_real_results_are_not_blocked(self):
        from lisearch.adapters.openweb import looks_blocked
        self.assertFalse(looks_blocked('<a class="result__a" href="x">Bob</a>'))

    def test_empty_body_is_not_a_block(self):
        from lisearch.adapters.openweb import looks_blocked
        self.assertFalse(looks_blocked(""))


class TestNoDestructiveOverwrite(unittest.TestCase):
    """A zero-lead fire must not clobber a previous good run for that audience."""

    def test_refuses_to_overwrite_prior_leads(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        import lisearch.fire as F

        tmp = Path(tempfile.mkdtemp())
        orig_runs = F.RUNS
        F.RUNS = tmp
        try:
            a = aud.new_audience("overwrite probe", countries=["AE"])
            run_dir = tmp / ("%s-%s" % (__import__("time").strftime("%Y-%m-%d"), a["slug"]))
            run_dir.mkdir(parents=True)
            (run_dir / "leads.json").write_text(json.dumps(
                {"leads": [make_lead("exa", "precious")]}))

            res = F.fire(a, [], {}, limit=10)          # no providers -> 0 leads
            self.assertEqual(res["run_dir"], None)
            self.assertEqual(res["preserved_prior"], 1)
            kept = json.loads((run_dir / "leads.json").read_text())["leads"]
            self.assertEqual(kept[0]["linkedin_account"], "precious")
        finally:
            F.RUNS = orig_runs
            shutil.rmtree(tmp, ignore_errors=True)


class TestCountryFromUrl(unittest.TestCase):
    def test_regional_subdomain(self):
        from lisearch.lead import country_from_url
        self.assertEqual(country_from_url("https://ae.linkedin.com/in/x"), "AE")
        self.assertEqual(country_from_url("https://uk.linkedin.com/in/x"), "GB")
        self.assertEqual(country_from_url("sa.linkedin.com/in/x"), "SA")

    def test_bare_hosts_have_no_country(self):
        from lisearch.lead import country_from_url
        for u in ("https://www.linkedin.com/in/x", "https://linkedin.com/in/x", "", None):
            self.assertEqual(country_from_url(u), "")


class TestWebQueryMatrix(unittest.TestCase):
    def setUp(self):
        self.a = aud.new_audience(
            "t", industry="real estate brokerage", countries=["AE"],
            cities=["Dubai", "Sharjah"], keywords=["real estate", "property"])

    def test_uses_country_subdomain_site_filter(self):
        from lisearch.adapters.base import web_queries
        qs = web_queries(self.a, 50)
        self.assertTrue(qs)
        self.assertTrue(all(q.startswith("site:ae.linkedin.com/in ") for q in qs))

    def test_one_subject_per_query_and_cities_covered_first(self):
        from lisearch.adapters.base import web_queries
        qs = web_queries(self.a, 12)          # 6 titles x 2 cities = tier 1 exactly
        self.assertEqual(len(qs), 12)
        self.assertTrue(all("real estate brokerage" in q for q in qs))
        self.assertTrue(any(q.endswith("Sharjah") for q in qs))
        self.assertFalse(any("," in q for q in qs))

    def test_us_falls_back_to_bare_host(self):
        from lisearch.adapters.base import web_queries
        a = aud.new_audience("us", countries=["US"], cities=["Austin"])
        self.assertTrue(web_queries(a, 3)[0].startswith("site:linkedin.com/in "))


class TestQualification(unittest.TestCase):
    def setUp(self):
        self.a = aud.new_audience(
            "t", industry="real estate brokerage", countries=["AE"], cities=["Dubai"],
            keywords=["real estate"], exclude_titles=["Agent"])

    def test_word_boundary_title_match(self):
        from lisearch.fire import title_hit
        self.assertIsNone(title_hit(make_lead("x", "a", title="Partnerships Manager"), self.a))
        self.assertIsNone(title_hit(make_lead("x", "a", title="Vice President Sales"), self.a))
        self.assertIsNone(title_hit(make_lead("x", "a", title="Assistant to the CEO"), self.a))
        self.assertIsNotNone(title_hit(make_lead("x", "a", title="Owner & Managing Partner"), self.a))
        self.assertIsNotNone(title_hit(make_lead("x", "a", title="Co-Founder"), self.a))

    def test_qualified_needs_title_geo_and_industry(self):
        from lisearch.fire import qualify
        full = make_lead("ddgs", "a", full_name="X", title="Owner",
                         company="Castles Real Estate", country="AE")
        qualify(full, self.a)
        self.assertTrue(full["qualified"])
        self.assertIn("geo=country", full["match"])
        no_geo = make_lead("ddgs", "b", title="Owner", company="Castles Real Estate")
        qualify(no_geo, self.a)
        self.assertFalse(no_geo["qualified"])
        no_ind = make_lead("ddgs", "c", title="Owner", company="Castles Trading", country="AE")
        qualify(no_ind, self.a)
        self.assertFalse(no_ind["qualified"])

    def test_city_in_text_outranks_subdomain_country(self):
        from lisearch.fire import geo_hit
        self.assertEqual(geo_hit(make_lead("x", "a", summary="based in Dubai", country="AE"), self.a), "city")
        self.assertEqual(geo_hit(make_lead("x", "a", country="AE"), self.a), "country")


class TestCarryOver(unittest.TestCase):
    """A fire folds every prior run of the audience in, so the pool only grows."""

    def test_prior_runs_are_merged_and_new_counted(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        import lisearch.fire as F
        from lisearch.adapters import base as B

        class Fake(B.Adapter):
            name = "fake"
            def search(self, audience, limit, ttl=0):
                return [make_lead("fake", "newbie", title="Owner", country="AE",
                                  company="New Real Estate"),
                        make_lead("fake", "precious", title="Owner", country="AE")]

        tmp = Path(tempfile.mkdtemp())
        orig_runs, orig_build = F.RUNS, F.build
        F.RUNS = tmp
        F.build = lambda names, cfg: [Fake({})]
        try:
            a = aud.new_audience("carry probe", industry="real estate", countries=["AE"])
            old = tmp / ("2020-01-01-%s" % a["slug"])
            old.mkdir(parents=True)
            (old / "leads.json").write_text(json.dumps(
                {"leads": [make_lead("exa", "precious", title="Owner", company="Old Real Estate")]}))
            res = F.fire(a, ["fake"], {}, limit=10)
            accts = {l["linkedin_account"] for l in res["leads"]}
            self.assertEqual(accts, {"precious", "newbie"})
            self.assertEqual(res["new"], 1)
            self.assertEqual(res["carried_over"], 1)
            merged = [l for l in res["leads"] if l["linkedin_account"] == "precious"][0]
            self.assertEqual(merged["sources"], ["exa", "fake"])
            self.assertEqual(merged["company"], "Old Real Estate")   # gap filled from prior
            self.assertTrue(merged["qualified"])
        finally:
            F.RUNS, F.build = orig_runs, orig_build
            shutil.rmtree(tmp, ignore_errors=True)


class TestHeadlineCompanyOnlyShape(unittest.TestCase):
    def test_lone_company_segment_is_company_not_title(self):
        r = split_headline("Mary Joy Omisol - Rocky Real Estate Brokerage LLC | LinkedIn")
        self.assertEqual(r["title"], "")
        self.assertEqual(r["company"], "Rocky Real Estate Brokerage LLC")

    def test_lone_role_segment_stays_title(self):
        r = split_headline("Reza Jilani - Dubai Luxury Real Estate Strategist | LinkedIn")
        self.assertEqual(r["title"], "Dubai Luxury Real Estate Strategist")
        self.assertEqual(r["company"], "")

    def test_at_sign_form(self):
        r = split_headline("Ahmed Mahgoub - Chief Executive Officer @ FZ Real Estate")
        self.assertEqual(r["title"], "Chief Executive Officer")
        self.assertEqual(r["company"], "FZ Real Estate")

    def test_location_lifted_from_snippet(self):
        from lisearch.adapters.base import location_from_snippet
        self.assertEqual(location_from_snippet("· Experience: X · Location: Dubai, United Arab Emirates · 500+"),
                         "Dubai, United Arab Emirates")
        self.assertEqual(location_from_snippet("no location here"), "")


class TestPriorRunNormalisation(unittest.TestCase):
    def test_company_filed_as_title_is_repaired(self):
        from lisearch.fire import normalise
        l = normalise(make_lead("ddgs", "a", title="Rocky Real Estate Brokerage LLC"))
        self.assertEqual(l["title"], "")
        self.assertEqual(l["company"], "Rocky Real Estate Brokerage LLC")

    def test_real_title_untouched(self):
        from lisearch.fire import normalise
        l = normalise(make_lead("ddgs", "a", title="Managing Director", company="Cedar"))
        self.assertEqual(l["title"], "Managing Director")

    def test_title_equal_to_company_is_cleared(self):
        from lisearch.fire import normalise
        l = normalise(make_lead("ddgs", "a", title="Qurrix", company="Qurrix"))
        self.assertEqual((l["title"], l["company"]), ("", "Qurrix"))


class TestSessionArtifacts(unittest.TestCase):
    """A fire leaves a one-line status and a small summary so a session never
    has to read leads.json to report."""

    def test_summary_status_and_done_line(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path

        import lisearch.fire as F
        from lisearch.adapters import base as B

        class Fake(B.Adapter):
            name = "fake"
            def search(self, audience, limit, ttl=0):
                return [make_lead("fake", "someone", title="Owner", country="AE",
                                  company="X Real Estate")]

        tmp = Path(tempfile.mkdtemp())
        orig_runs, orig_build = F.RUNS, F.build
        F.RUNS, F.build = tmp, (lambda names, cfg: [Fake({})])
        try:
            a = aud.new_audience("artifact probe", industry="real estate", countries=["AE"])
            res = F.fire(a, ["fake"], {}, limit=10)
            run = Path(res["run_dir"])
            summ = json.loads((run / "summary.json").read_text())
            self.assertNotIn("leads", summ)
            self.assertEqual(summ["qualified"], 1)
            self.assertEqual(summ["top"][0]["account"], "someone")
            self.assertIn("DONE: artifact-probe", (run / "status.txt").read_text())
            line = F.done_line(res)
            self.assertIn("kept=1 qualified=1 strong=1 new=1", line)
            self.assertIn("providers=fake:1", line)
        finally:
            F.RUNS, F.build = orig_runs, orig_build
            shutil.rmtree(tmp, ignore_errors=True)


class TestHeadlineEllipsisArtifact(unittest.TestCase):
    def test_text_after_ellipsis_is_dropped(self):
        r = split_headline("Najib Taousse - Founder & CEO Nara Real Estate ...Mohammed Ilyas - Co-Founder")
        self.assertEqual(r["full_name"], "Najib Taousse")
        self.assertEqual(r["title"], "Founder & CEO Nara Real Estate")
        self.assertEqual(r["company"], "")


class TestSeeds(unittest.TestCase):
    def setUp(self):
        self.a = aud.new_audience(
            "lb", industry="digital marketing agency", countries=["LB"], cities=["Beirut"],
            companies=["Microbits", "Bluemoon"], people=["Kaysar Daou @ Leoceros", "Hala Jaber"])

    def test_seed_queries_come_first_and_geo_has_country_name(self):
        from lisearch.adapters.base import web_queries
        qs = web_queries(self.a, 40)
        self.assertIn('site:linkedin.com/in "Kaysar Daou" "Leoceros"', qs[0])
        self.assertTrue(qs[3].startswith('site:lb.linkedin.com/in "Microbits" (Founder OR'))
        self.assertTrue(any(q.endswith(" Lebanon") and '"Owner"' in q for q in qs))
        self.assertTrue(any(q.endswith(" Beirut") and '"Owner"' in q for q in qs))

    def test_named_person_qualifies_only_with_corroboration(self):
        from lisearch.fire import qualify
        l = make_lead("ddgs", "kaysar", full_name="Kaysar Daou", company="Leoceros")
        qualify(l, self.a)
        self.assertTrue(l["qualified"])
        self.assertEqual(l["match"], "person=Kaysar Daou;via=company")
        bare = make_lead("ddgs", "x", full_name="Kaysar Daou", company="Bombardier")
        qualify(bare, self.a)
        self.assertFalse(bare["qualified"])
        self.assertIn("person?=Kaysar Daou", bare["match"])
        namesake = make_lead("ddgs", "y", full_name="Hala Jaber", title="Founder", country="LB")
        qualify(namesake, self.a)
        self.assertTrue(namesake["qualified"])          # bare seed + title evidence
        self.assertEqual(namesake["match"], "person=Hala Jaber;via=title;title=Founder")
        near = make_lead("ddgs", "z", full_name="Kaysar Daoud", company="Leoceros")
        qualify(near, self.a)
        self.assertFalse(near["qualified"])

    def test_person_seed_query_uses_company(self):
        from lisearch.adapters.base import web_queries
        qs = web_queries(self.a, 10)
        self.assertEqual(qs[0], 'site:linkedin.com/in "Kaysar Daou" "Leoceros"')
        self.assertEqual(qs[1], 'site:linkedin.com/in "Kaysar Daou" Lebanon')

    def test_seed_company_counts_as_industry_evidence(self):
        from lisearch.fire import qualify
        l = make_lead("ddgs", "a", full_name="X", title="Founder", company="Microbits", country="LB")
        qualify(l, self.a)
        self.assertTrue(l["qualified"])
        self.assertIn("industry=company:Microbits", l["match"])


class TestCompanySeedIsWholeWord(unittest.TestCase):
    def test_short_or_substring_seed_does_not_match(self):
        from lisearch.fire import qualify
        a = aud.new_audience("t", industry="software", countries=["LB"],
                             companies=["tar", "Voxire"], people=["Tarek Abou Rjeily @ tar"])
        l = make_lead("ddgs", "x", full_name="Tarek Abou Rjeily", company="FTC Qatar")
        qualify(l, a)
        self.assertFalse(l["qualified"])
        ok = make_lead("ddgs", "y", full_name="Abed Amouneh", title="Founder", company="Voxire", country="LB")
        qualify(ok, a)
        self.assertTrue(ok["qualified"])
        self.assertIn("company:Voxire", ok["match"])


class TestExclusionScope(unittest.TestCase):
    def test_sector_keyword_in_summary_does_not_exclude(self):
        a = aud.new_audience("t", industry="marketing agency", countries=["LB"],
                             exclude_keywords=["hotel", "food"], exclude_titles=["Business Partner"])
        l = make_lead("ddgs", "a", title="Founder", company="Leoceros",
                      summary="we shot a campaign for a hotel and a food brand")
        self.assertFalse(excluded(l, a))
        self.assertTrue(excluded(make_lead("ddgs", "b", title="Founder", company="Beirut Hotel"), a))
        self.assertTrue(excluded(make_lead("ddgs", "c", title="HR Business Partner"), a))
        self.assertFalse(excluded(make_lead("ddgs", "d", title="Managing Partner"), a))


class TestStrength(unittest.TestCase):
    def test_industry_in_headline_is_strong_posts_only_is_weak(self):
        from lisearch.fire import qualify
        a = aud.new_audience("t", industry="digital marketing agency", countries=["LB"], keywords=["PR agency"])
        strong = make_lead("ddgs", "a", full_name="X", title="Founder", company="Lemonade Digital", country="LB",
                           summary="a digital marketing agency in Beirut")
        qualify(strong, a); self.assertEqual(strong["strength"], "strong")
        weak = make_lead("ddgs", "b", full_name="Y", title="Owner", company="Nouna Resto", country="LB",
                         summary="we hosted a digital marketing agency event")
        qualify(weak, a); self.assertTrue(weak["qualified"]); self.assertEqual(weak["strength"], "weak")


class TestCachePrune(unittest.TestCase):
    def test_expired_and_orphaned_pages_are_deleted_useful_kept(self):
        import json, shutil, tempfile, time
        from pathlib import Path
        import lisearch.cache as C
        tmp = Path(tempfile.mkdtemp()); orig = C.CACHE; C.CACHE = tmp
        try:
            C.put("ddgs", {"q": "useful"}, {"results": []})
            C.put("ddgs", {"q": "orphan"}, {"results": []})
            C.put("ddgs", {"q": "old"}, {"results": []})
            old = C.key_path("ddgs", {"q": "old"})
            old.write_text(json.dumps({"_at": time.time() - 40 * 86400, "value": {}}))
            C.put("exa", {"q": "keyed-provider-page"}, {"results": []})   # not orphan-pruned
            r = C.prune({C.key_path("ddgs", {"q": "useful"})}, ttl=30 * 86400)
            self.assertEqual((r["expired"], r["orphaned"], r["kept"]), (1, 1, 2))
            self.assertTrue(C.key_path("ddgs", {"q": "useful"}).exists())
            self.assertFalse(C.key_path("ddgs", {"q": "orphan"}).exists())
            self.assertFalse(old.exists())
        finally:
            C.CACHE = orig; shutil.rmtree(tmp, ignore_errors=True)


class TestVerifyGate(unittest.TestCase):
    """The delivery gate. No network: `ask` is injected, which is also how the
    asymmetry gets tested — the rule that only a confident, IDENTIFIED negative
    may refuse a row is the whole safety property here."""

    def _ans(self, **kw):
        base = {"identified": True, "is_owner": True, "in_sector": True,
                "confidence": "high", "role": "Founder", "company": "X",
                "company_sector": "marketing agency", "source_url": "https://x/", "reason": ""}
        base.update(kw)
        return base

    def test_only_a_confident_identified_negative_rejects(self):
        from lisearch.verify import decide, CONFIRMED, REJECTED, UNVERIFIED
        self.assertEqual(decide(self._ans()), CONFIRMED)
        # off-sector and off-role, confidently: the two real rejections
        self.assertEqual(decide(self._ans(in_sector=False)), REJECTED)
        self.assertEqual(decide(self._ans(is_owner=False)), REJECTED)
        self.assertEqual(decide(self._ans(in_sector=False, confidence="medium")), REJECTED)
        # silence is never evidence
        self.assertEqual(decide(self._ans(identified=False)), UNVERIFIED)
        self.assertEqual(decide(self._ans(in_sector=False, confidence="low")), UNVERIFIED)
        self.assertEqual(decide({}), UNVERIFIED)
        self.assertEqual(decide(self._ans(identified=False, is_owner=False)), UNVERIFIED)

    def test_spec_is_built_from_the_stored_audience_and_geo_is_context_only(self):
        """Geography must reach the model as CONTEXT, never as a test.

        The first live run refused a creative studio and a branding agency,
        both with "not a Lebanon/Batroun-based agency" in the reason, because
        the spec folded location into the sector question. geo_hit() already
        settles location off the profile's own country subdomain."""
        from lisearch.verify import target_sentence
        a = aud.new_audience("t", industry="digital marketing agency", countries=["LB"],
                             cities=["Beirut"], keywords=["PR agency", "وكالة إعلانات"])
        t = target_sentence([a])
        self.assertIn("digital marketing agency", t)
        self.assertIn("Lebanon", t)
        self.assertIn("OWN or LEAD", t)
        self.assertIn("do NOT judge it", t)
        self.assertLess(t.index("SECTOR"), t.index("LOCATION"))
        self.assertNotIn("وكالة", t)      # query fodder, not English prose

    def test_sector_is_the_whole_brief_and_the_ladder_is_per_audience(self):
        """A row the PR audience surfaced can be an ad-agency owner; it is still
        a lead the brief asked for. Judging sector per audience refused four
        good rows on the second live run."""
        from lisearch.verify import Spec, specs, spec_for, target_sentence
        agencies = aud.new_audience("ag", industry="creative agency", countries=["LB"])
        devshops = aud.new_audience("dev", industry="software development shop", countries=["LB"])
        by_slug = specs([agencies, devshops])
        one = spec_for({"audience": agencies["slug"]}, by_slug, Spec("fallback"))
        self.assertIn("creative agency", one.text)
        self.assertIn("software development shop", one.text)     # the whole brief
        self.assertIs(one.audience, agencies)                    # its own title ladder
        self.assertNotIn("...", one.text)                        # never truncated
        # an unknown audience falls back rather than judging against nothing
        self.assertEqual(spec_for({"audience": "gone"}, by_slug, Spec("fb")).text, "fb")

    def test_a_custom_title_list_never_narrows_who_counts_as_the_decider(self):
        """All four Lebanon audiences dropped "Chief Executive Officer" from
        their title list in favour of "CEO" plus Arabic titles — which is a
        SEARCH choice. The gate refused Impact BBDO's "Chief Executive Officer
        Levant" and Quantum's "Chief Executive" until the ladder fell back to
        audience.DEFAULT_TITLES."""
        from lisearch.verify import Spec, decide, CONFIRMED, REJECTED
        narrow = aud.new_audience("t", industry="creative agency", countries=["LB"],
                                  titles=["Owner", "CEO", "مؤسس"])
        spec = Spec("...", narrow)
        for role in ("Chief Executive Officer Levant & Head Of Regional Services MENA",
                     "Chief Executive", "Managing Partner", "Proprietor"):
            self.assertEqual(decide(self._ans(role=role), spec), CONFIRMED, role)
        for role in ("Vice President", "Head of Social Media", "Account Director"):
            self.assertEqual(decide(self._ans(role=role), spec), REJECTED, role)

    def test_seniority_is_decided_by_this_tools_ladder_not_by_the_model(self):
        """The first live run refused two Managing Directors — correctly
        identified, in sector — because the model reads "owner" as equity while
        audience.DEFAULT_TITLES treats a Levant SME's MD as the decider."""
        from lisearch.verify import Spec, decide, CONFIRMED, REJECTED
        a = aud.new_audience("t", industry="creative agency", countries=["LB"])
        spec = Spec("...", a)
        md = self._ans(role="Managing Director", is_owner=False)
        self.assertEqual(decide(md, spec), CONFIRMED)          # ladder overrules the model
        self.assertEqual(decide(md), REJECTED)                 # no ladder: the model is all there is
        # the ladder still refuses what it always refused
        self.assertEqual(decide(self._ans(role="Head of Social Media"), spec), REJECTED)
        self.assertEqual(decide(self._ans(role="Vice President"), spec), REJECTED)
        self.assertEqual(decide(self._ans(role="Former Owner"), spec), REJECTED)
        self.assertEqual(decide(self._ans(role=""), spec), REJECTED)
        # and an in-ladder role still fails on the sector
        self.assertEqual(decide(self._ans(role="Founder", in_sector=False), spec), REJECTED)

    def test_a_stub_answer_never_reaches_the_operators_cache(self):
        """ttl<=0 means "do not use the cache" on WRITE as well as on read.
        The gate tests below run against the real cache dir; before this, their
        stub answers ("company X, sector hotel") were written into it."""
        import lisearch.cache as C
        from lisearch.verify import verify_row
        row = {"linkedin_account": "stub-must-not-persist", "full_name": "P"}
        verify_row(row, "t", key="k", ttl=0, ask=lambda r, t, **kw: (self._ans(), 0.005))
        self.assertFalse(C.key_path("verify", {"account": row["linkedin_account"],
                                               "target": "t", "model": "perplexity/sonar"}).exists())

    def test_gate_refuses_off_spec_and_backfills_from_the_next_candidate(self):
        from lisearch.export import gate
        from lisearch.verify import REJECTED
        rows = [{"linkedin_account": "a%d" % i, "full_name": "P%d" % i} for i in range(6)]
        calls = []

        def ask(row, target, **kw):
            calls.append(row["linkedin_account"])
            bad = row["linkedin_account"] in ("a0", "a2")
            return self._ans(in_sector=not bad, company_sector="hotel" if bad else "agency"), 0.005

        kept, rejected, st = gate(rows, 2, "t", key="k", model="m", ttl=0,
                                  workers=2, budget=10, ask=ask)
        self.assertEqual([k["linkedin_account"] for k in kept], ["a1", "a3"])
        self.assertEqual([r["linkedin_account"] for r in rejected], ["a0", "a2"])
        self.assertEqual((st["confirmed"], st["rejected"]), (2, 2))
        self.assertEqual(st["cost"], 0.02)
        self.assertLess(len(calls), len(rows))       # never verifies the whole pool

    def test_gate_delivers_short_rather_than_unchecked_when_the_budget_runs_out(self):
        from lisearch.export import gate
        rows = [{"linkedin_account": "a%d" % i, "full_name": "P%d" % i} for i in range(20)]

        def ask(row, target, **kw):
            return self._ans(in_sector=False), 0.005     # everything is off-spec

        kept, rejected, st = gate(rows, 10, "t", key="k", model="m", ttl=0,
                                  workers=4, budget=8, ask=ask)
        self.assertEqual(kept, [])
        self.assertLessEqual(st["calls"], 12)            # budget honoured (+ one block)
        self.assertEqual(len(rejected), st["calls"])

    def test_a_failed_call_is_unverified_and_still_delivered(self):
        from lisearch.export import gate
        def ask(row, target, **kw):
            return {"identified": False, "confidence": "low", "reason": "call failed"}, 0.0
        kept, rejected, st = gate([{"linkedin_account": "a", "full_name": "P"}], 1, "t",
                                  key="k", model="m", ttl=0, workers=1, budget=4, ask=ask)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["_verify"]["verdict"], "unverified")
        self.assertEqual(rejected, [])

    def test_verdicts_are_cached_so_a_re_export_pays_nothing(self):
        import shutil, tempfile
        from pathlib import Path
        import lisearch.cache as C
        from lisearch.verify import verify_row
        tmp = Path(tempfile.mkdtemp()); orig = C.CACHE; C.CACHE = tmp
        n = []
        try:
            def ask(row, target, **kw):
                n.append(1)
                return self._ans(), 0.005
            row = {"linkedin_account": "a", "full_name": "P"}
            first = verify_row(row, "t", key="k", ask=ask)
            again = verify_row(row, "t", key="k", ask=ask)
            self.assertEqual(len(n), 1)
            self.assertFalse(first["cached"]); self.assertTrue(again["cached"])
            self.assertEqual(again["cost"], 0.0)
            self.assertEqual(again["verdict"], first["verdict"])
            # a DIFFERENT brief is a different question, so it is asked again
            verify_row(row, "other target", key="k", ask=ask)
            self.assertEqual(len(n), 2)
        finally:
            C.CACHE = orig; shutil.rmtree(tmp, ignore_errors=True)

    def test_the_gate_never_contacts_linkedin(self):
        from lisearch.compliance import assert_allowed_host
        from lisearch import verify as V
        assert_allowed_host(V.ENDPOINT)                  # the only host it posts to
        with self.assertRaises(ComplianceError):
            assert_allowed_host("https://www.linkedin.com/in/x")


class TestTitleAliases(unittest.TestCase):
    """A stored audience's title spelling must not decide whether a headline
    matches. lb-dev-shops-ai-builders stores "CTO", so "CTO, Messaging" matched
    and "Chief Technology Officer" did not — three in-sector leads refused."""

    def test_abbreviation_and_long_form_are_the_same_title(self):
        from lisearch.fire import title_hit
        a = aud.new_audience("t", industry="software", countries=["LB"], titles=["CTO", "CEO"])
        for role in ("CTO", "CTO, Messaging", "Chief Technology Officer",
                     "Group Chief Technical Officer", "CEO", "Chief Executive Officer",
                     "Chief Executive"):
            self.assertIsNotNone(title_hit({"title": role}, a), role)

    def test_expansion_widens_spelling_never_seniority(self):
        from lisearch.fire import title_hit
        owners = aud.new_audience("t", industry="software", countries=["LB"],
                                  titles=["Owner", "Founder"])
        # this audience never asked for a CTO, so no spelling of one matches
        for role in ("CTO", "Chief Technology Officer", "Chief Operating Officer"):
            self.assertIsNone(title_hit({"title": role}, owners), role)
        # and the demoters still win over any alias
        md = aud.new_audience("t2", industry="software", countries=["LB"], titles=["MD"])
        self.assertIsNone(title_hit({"title": "Deputy Managing Director"}, md))


class TestCachedVerdictsFollowPolicy(unittest.TestCase):
    def test_a_ladder_change_moves_cached_rows_without_paying_again(self):
        """The day the ladder learned "Chief Technology Officer" == "CTO",
        three already-paid-for rows had to flip from rejected to confirmed."""
        import shutil, tempfile
        from pathlib import Path
        import lisearch.cache as C
        from lisearch.verify import Spec, verify_row, CONFIRMED, REJECTED
        tmp = Path(tempfile.mkdtemp()); orig = C.CACHE; C.CACHE = tmp
        calls = []
        try:
            def ask(row, target, **kw):
                calls.append(1)
                return {"identified": True, "role": "Chief Technology Officer",
                        "is_owner": False, "in_sector": True, "confidence": "high",
                        "company": "Tippikl Labs", "company_sector": "AI"}, 0.005
            row = {"linkedin_account": "a", "full_name": "P"}
            narrow = aud.new_audience("no-cto", industry="software", countries=["LB"],
                                      titles=["Owner", "Founder"])
            wide = aud.new_audience("cto-ok", industry="software", countries=["LB"],
                                    titles=["Owner", "Founder", "CTO"])
            # same spec TEXT both times, so it is the same cache entry
            first = verify_row(row, Spec("spec", narrow), key="k", ask=ask)
            self.assertEqual(first["verdict"], REJECTED)
            second = verify_row(row, Spec("spec", wide), key="k", ask=ask)
            self.assertEqual(second["verdict"], CONFIRMED)
            self.assertTrue(second["cached"])
            self.assertEqual(second["cost"], 0.0)
            self.assertEqual(len(calls), 1)             # never asked twice
        finally:
            C.CACHE = orig; shutil.rmtree(tmp, ignore_errors=True)


class TestRecheckTargeting(unittest.TestCase):
    def test_batch_filter_targets_one_delivery(self):
        """Without it, "check the batch I just delivered" spends on row 1."""
        import csv, shutil, tempfile
        from pathlib import Path
        import lisearch.cache as C
        import lisearch.export as E
        # Belt AND braces on the operator's cache: verify_ttl=0 below stops the
        # read and the write, and CACHE is redirected in case a future edit
        # drops that argument. An earlier version of this test had neither and
        # filed four stub verdicts under cache/verify.
        tmp = Path(tempfile.mkdtemp()); orig = E.RESULTS; E.RESULTS = tmp
        orig_cache = C.CACHE; C.CACHE = tmp / "cache"
        try:
            d = tmp / "brief"; d.mkdir()
            with (d / "owners.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=E.FIELDS); w.writeheader()
                for n, b in ((1, "1"), (2, "1"), (3, "10"), (4, "10")):
                    w.writerow({"n": n, "batch": b, "linkedin_account": "a%d" % n,
                                "full_name": "P%d" % n, "audience": "x"})
            seen = []

            def ask(row, target, **kw):
                seen.append(row["linkedin_account"])
                return {"identified": True, "role": "Owner", "in_sector": True,
                        "is_owner": True, "confidence": "high"}, 0.005

            res = E.recheck("brief", [], limit=50, batch="10", verify_ttl=0, ask=ask)
            self.assertEqual(sorted(seen), ["a3", "a4"])
            self.assertEqual(res["checked"], 2)
        finally:
            E.RESULTS = orig; C.CACHE = orig_cache
            shutil.rmtree(tmp, ignore_errors=True)


class TestOwnersCsvMigration(unittest.TestCase):
    def test_new_columns_are_appended_and_existing_rows_keep_their_values(self):
        import csv, shutil, tempfile
        from pathlib import Path
        from lisearch.export import FIELDS, _migrate_master
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "owners.csv"
            old = ["n", "batch", "delivered_at", "audience", "linkedin_account", "linkedin_url",
                   "full_name", "title", "company", "location", "strength", "match", "score",
                   "sources", "status"]
            with f.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=old); w.writeheader()
                w.writerow({"n": 1, "batch": 1, "linkedin_account": "kaysardaou",
                            "full_name": "Kaysar Daou", "title": "Co-Founder", "score": 8.98})
            self.assertEqual(_migrate_master(f), 1)
            rows = list(csv.DictReader(f.open(encoding="utf-8")))
            self.assertEqual(list(rows[0].keys()), FIELDS)
            self.assertEqual(rows[0]["full_name"], "Kaysar Daou")
            self.assertEqual(rows[0]["score"], "8.98")
            self.assertEqual(rows[0]["verified"], "")
            self.assertEqual(_migrate_master(f), 0)      # idempotent
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
