import json
import tempfile
import unittest
from pathlib import Path

import funnel_lib


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


class FunnelLibTests(unittest.TestCase):
    def test_builds_config_from_icp_thresholds_and_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "icp.yaml").write_text(
                """
target:
  geo:
    countries:
      - "United Arab Emirates"
      - "Saudi Arabia"
  size_signal: "5-30 locations / branches"
scoring:
  threshold_qualify: 70
  hard_floor: 60
buyer_profile:
  generic_inbox_policy:
    rule: "Generic inboxes are SKIPPED unless score >= 85"
run_settings:
  send_cap: 100
  qualified_target: 100
"""
            )

            cfg = funnel_lib.build_funnel_config(run_dir)

            self.assertEqual(cfg["target_sends"], 100)
            self.assertEqual(cfg["source_target"], 300)
            self.assertEqual(cfg["max_sourcing_passes"], 3)
            self.assertEqual(cfg["threshold_qualify"], 70)
            self.assertEqual(cfg["rescue_min_score"], 60)
            self.assertEqual(cfg["role_inbox_threshold"], 85)
            self.assertEqual(cfg["target_countries"], ["AE", "SA"])
            self.assertEqual(cfg["min_branches"], 5)
            self.assertEqual(cfg["max_branches"], 30)

    def test_config_uses_generic_inbox_threshold_not_other_score_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "icp.yaml").write_text(
                """
buyer_profile:
  fallback_titles:
    - "COO (only if CEO unfindable AND lead score >= 80)"
  generic_inbox_policy:
    rule: "Generic inboxes (info@, contact@) are SKIPPED unless score >= 85 AND no person-mapped email exists."
run_settings:
  send_cap: 20
"""
            )

            cfg = funnel_lib.build_funnel_config(run_dir)

            self.assertEqual(cfg["role_inbox_threshold"], 85)

    def test_source_fit_drops_out_of_scope_before_scoring(self):
        cfg = {
            "target_countries": ["AE", "SA"],
            "target_verticals": ["clinic", "fitness"],
            "min_branches": 5,
            "max_branches": 30,
            "require_website": True,
            "require_contact": True,
        }
        rows = [
            {
                "id": "good",
                "name": "GCC Dental Group",
                "url": "https://gcc-dental.ae",
                "email": "ceo@gcc-dental.ae",
                "location": "Dubai, United Arab Emirates",
                "raw": {"category": "Dental clinic", "review_count": 300},
                "branches": 8,
            },
            {
                "id": "single",
                "name": "Single Gym",
                "url": "https://single-gym.ae",
                "email": "owner@single-gym.ae",
                "location": "Dubai, United Arab Emirates",
                "raw": {"category": "Gym"},
                "branches": 1,
            },
            {
                "id": "no_site",
                "name": "No Site Clinic",
                "email": "owner@example.ae",
                "location": "Dubai, United Arab Emirates",
                "raw": {"category": "Dental clinic"},
                "branches": 6,
            },
        ]

        kept, drops = funnel_lib.apply_source_fit(rows, cfg)

        self.assertEqual([row["id"] for row in kept], ["good"])
        self.assertEqual(drops["below_min_branches"], 1)
        self.assertEqual(drops["no_website"], 1)

    def test_source_fit_rejects_social_directory_and_government_websites(self):
        cfg = {
            "target_countries": ["AE"],
            "target_verticals": ["clinic"],
            "min_branches": 1,
            "max_branches": 30,
            "require_website": True,
            "require_contact": True,
        }
        rows = [
            {
                "id": "social",
                "name": "Social Dental Clinic",
                "url": "https://www.instagram.com/socialdental",
                "email": "owner@socialdental.ae",
                "location": "Dubai, United Arab Emirates",
                "raw": {"category": "Dental clinic"},
            },
            {
                "id": "directory",
                "name": "Directory Dental Clinic",
                "url": "https://www.edarabia.com/best-dental-clinics-uae/",
                "email": "owner@directorydental.ae",
                "location": "Dubai, United Arab Emirates",
                "raw": {"category": "Dental clinic"},
            },
            {
                "id": "gov",
                "name": "Government Medical Center",
                "url": "https://www.moh.gov.ae/",
                "email": "owner@govmedical.ae",
                "location": "Dubai, United Arab Emirates",
                "raw": {"category": "Medical clinic"},
            },
        ]

        kept, drops = funnel_lib.apply_source_fit(rows, cfg)

        self.assertEqual(kept, [])
        self.assertEqual(drops["no_website"], 3)

    def test_best_email_prefers_same_domain_signal_email_over_role_inbox(self):
        lead = {
            "id": "signal-email",
            "name": "Signal Clinic",
            "url": "https://signalclinic.ae",
            "email": "info@signalclinic.ae",
            "signals": {"emails": ["founder@signalclinic.ae", "random@other.com"]},
            "raw": {"all_emails": ["contact@signalclinic.ae"]},
        }

        self.assertEqual(funnel_lib.best_email(lead), "founder@signalclinic.ae")

    def test_best_email_rejects_unrelated_corporate_domains_but_allows_freemail(self):
        unrelated = {
            "id": "unrelated",
            "name": "Unrelated Clinic",
            "url": "https://unrelatedclinic.ae",
            "email": "ceo@other-business.ae",
            "signals": {"emails": ["owner@gmail.com"]},
        }

        self.assertEqual(funnel_lib.best_email(unrelated), "owner@gmail.com")

    def test_resolve_email_pool_keeps_clean_contacts_and_reports_reasons(self):
        rows = [
            {
                "id": "good",
                "name": "Good Clinic",
                "url": "https://goodclinic.ae",
                "email": "info@goodclinic.ae",
                "signals": {"emails": ["ceo@goodclinic.ae"]},
                "country": "AE",
                "vertical": "clinic",
            },
            {
                "id": "missing",
                "name": "Missing Clinic",
                "url": "https://missingclinic.ae",
                "country": "AE",
                "vertical": "clinic",
            },
        ]

        resolved, drops = funnel_lib.resolve_email_pool(rows)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["email"], "ceo@goodclinic.ae")
        self.assertEqual(resolved[0]["email_class"], "person")
        self.assertEqual(drops["no_clean_email"], 1)

    def test_balanced_score_enforces_bands_and_role_inbox_rule(self):
        cfg = {
            "target_countries": ["AE"],
            "target_verticals": ["clinic"],
            "min_branches": 5,
            "max_branches": 30,
            "threshold_qualify": 70,
            "rescue_min_score": 60,
            "role_inbox_threshold": 85,
        }
        direct = {
            "id": "direct",
            "name": "Direct Dental",
            "url": "https://direct.ae",
            "email": "sara@direct.ae",
            "location": "Dubai, United Arab Emirates",
            "raw": {"category": "Dental clinic", "review_count": 80, "rating": 4.6},
            "branches": 5,
            "signals": {"mode": "whatsapp"},
        }
        role = {
            **direct,
            "id": "role",
            "email": "info@roleclinic.ae",
            "url": "https://roleclinic.ae",
            "raw": {"category": "Dental clinic", "review_count": 80, "rating": 4.6},
        }

        direct_score = funnel_lib.score_lead(direct, cfg)
        role_score = funnel_lib.score_lead(role, cfg)

        self.assertIn(direct_score["funnel_status"], {"send_ready", "signal_rescue"})
        self.assertIn("concrete_signal", direct_score["matched_criteria"])
        self.assertEqual(role_score["funnel_status"], "drop")
        self.assertEqual(role_score["drop_reason"], "role_inbox_below_threshold")

    def test_signal_qualification_iterates_ranked_batches_until_target(self):
        scored = [
            {"id": "no-signal-1", "score": 95, "funnel_status": "send_ready"},
            {"id": "no-signal-2", "score": 94, "funnel_status": "send_ready"},
            {"id": "signal-1", "score": 93, "funnel_status": "send_ready", "signals": {"mode": "form"}},
            {"id": "signal-2", "score": 92, "funnel_status": "signal_rescue", "signals": {"branches": 8}},
        ]

        result = funnel_lib.qualify_ranked_signals(scored, target=2, batch_size=1)

        self.assertEqual(result["tested"], 4)
        self.assertEqual([lead["id"] for lead in result["qualified"]], ["signal-1", "signal-2"])
        self.assertEqual(result["exhausted"], False)

    def test_send_gate_rejects_drafts_without_required_qualification(self):
        draft = {
            "lead_id": "bad",
            "to_email": "ceo@example.ae",
            "score": 91,
            "qualification_status": "send_ready",
            "signal_used": "",
        }

        ok, reason = funnel_lib.validate_send_draft(draft, sent_emails=set(), domain_counts={})

        self.assertFalse(ok)
        self.assertEqual(reason, "missing_concrete_signal")

    def test_build_draft_carries_qualification_metadata_for_send_gate(self):
        lead = {
            "id": "qualified",
            "name": "Qualified Clinic",
            "email": "ceo@qualifiedclinic.ae",
            "score": 86,
            "vertical": "clinic",
            "country": "AE",
            "qualification_status": "send_ready",
            "signals": {"mode": "form"},
        }

        draft = funnel_lib.build_outreach_draft(lead, run_slug="test-run")
        ok, reason = funnel_lib.validate_send_draft(draft, sent_emails=set(), domain_counts={})

        self.assertTrue(ok, reason)
        self.assertEqual(draft["qualification_status"], "send_ready")
        self.assertEqual(draft["signal_used"], "mode")
        self.assertEqual(draft["to_email"], "ceo@qualifiedclinic.ae")


if __name__ == "__main__":
    unittest.main()
