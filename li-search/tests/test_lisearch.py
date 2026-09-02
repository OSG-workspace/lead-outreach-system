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
