import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "scripts" / "balanced_funnel.py"
spec = importlib.util.spec_from_file_location("balanced_funnel", SCRIPT)
balanced_funnel = importlib.util.module_from_spec(spec)
sys.modules["balanced_funnel"] = balanced_funnel
spec.loader.exec_module(balanced_funnel)


def lead(**overrides):
    base = {
        "id": "lead-1",
        "name": "Acme Dental Clinic",
        "source": "google_maps",
        "url": "https://acmedental.ae",
        "email": "owner@acmedental.ae",
        "phone": "+971 55 000 0000",
        "location": "Dubai, United Arab Emirates",
        "raw": {
            "category": "Dental clinic",
            "review_count": 220,
            "rating": 4.7,
            "all_emails": ["owner@acmedental.ae"],
        },
    }
    base.update(overrides)
    return base


class BalancedFunnelTests(unittest.TestCase):
    def test_source_fit_drops_wrong_geo_and_directory_results(self):
        config = balanced_funnel.FunnelConfig()
        wrong_geo = lead(location="Muscat, Oman")
        directory = lead(
            name="Top 10 Best Dental Clinics in Dubai",
            source="web",
            url="https://example-directory.com/best-dental-clinics",
            raw={"page_title": "Top 10 Best Dental Clinics in Dubai"},
        )

        self.assertFalse(balanced_funnel.source_fit(wrong_geo, config).keep)
        self.assertEqual(
            balanced_funnel.source_fit(wrong_geo, config).reason,
            "outside_target_geo",
        )
        self.assertFalse(balanced_funnel.source_fit(directory, config).keep)
        self.assertEqual(
            balanced_funnel.source_fit(directory, config).reason,
            "directory_or_article",
        )

    def test_score_bands_send_ready_and_signal_rescue_are_distinct(self):
        config = balanced_funnel.FunnelConfig()
        send_ready = balanced_funnel.score_lead(lead(), config)
        rescue = balanced_funnel.score_lead(
            lead(
                email="owner@smallclinic.ae",
                url="https://smallclinic.ae",
                raw={
                    "category": "Dental clinic",
                    "review_count": 45,
                    "rating": 4.2,
                    "all_emails": ["owner@smallclinic.ae"],
                },
            ),
            config,
        )

        self.assertEqual(send_ready["qualification_status"], "send_ready")
        self.assertGreaterEqual(rescue["score"], config.rescue_floor)
        self.assertLess(rescue["score"], config.threshold_qualify)
        self.assertEqual(rescue["qualification_status"], "signal_rescue")
        self.assertFalse(rescue["send_eligible"])

    def test_role_inbox_requires_documented_85_point_threshold(self):
        config = balanced_funnel.FunnelConfig()
        scored = balanced_funnel.score_lead(
            lead(
                email="info@acmedental.ae",
                raw={
                    "category": "Dental clinic",
                    "review_count": 120,
                    "rating": 4.5,
                    "all_emails": ["info@acmedental.ae"],
                },
            ),
            config,
        )

        self.assertEqual(scored["email_class"], "role")
        self.assertLess(scored["score"], config.role_inbox_min_score)
        self.assertEqual(scored["qualification_status"], "drop")
        self.assertEqual(scored["disqualified_by"], "role_inbox_below_85")

    def test_build_signal_pool_keeps_full_ranked_pool_not_only_top_ten(self):
        config = balanced_funnel.FunnelConfig()
        scored = []
        for idx in range(15):
            scored.append(
                {
                    "lead_id": f"lead-{idx}",
                    "lead_slug": f"lead-{idx}",
                    "name": f"Lead {idx}",
                    "website": f"https://lead{idx}.ae",
                    "url": f"https://lead{idx}.ae",
                    "email": f"owner@lead{idx}.ae",
                    "email_class": "person",
                    "score": 70 + idx,
                    "qualification_status": "send_ready",
                    "send_eligible": True,
                }
            )

        pool = balanced_funnel.build_signal_pool(scored, config, sent_emails=set())

        self.assertEqual(len(pool), 15)
        self.assertEqual(pool[0]["score"], 84)
        self.assertEqual(pool[-1]["score"], 70)

    def test_build_signal_pool_does_not_collapse_freemail_across_businesses(self):
        config = balanced_funnel.FunnelConfig()
        scored = []
        for idx in range(3):
            scored.append(
                {
                    "lead_id": f"clinic-{idx}",
                    "lead_slug": f"clinic-{idx}",
                    "name": f"Clinic {idx}",
                    "website": f"https://clinic{idx}.ae",
                    "url": f"https://clinic{idx}.ae",
                    "email": f"owner{idx}@gmail.com",
                    "email_class": "gmail-personal",
                    "score": 72,
                    "qualification_status": "send_ready",
                    "send_eligible": True,
                }
            )

        pool = balanced_funnel.build_signal_pool(scored, config, sent_emails=set())

        self.assertEqual(len(pool), 3)

    def test_select_source_batch_caps_pre_score_work_to_target_multiplier(self):
        config = balanced_funnel.FunnelConfig(send_cap=3, source_multiplier=3)
        leads = []
        for idx in range(20):
            leads.append(
                lead(
                    id=f"lead-{idx}",
                    name=f"Dental Clinic {idx}",
                    raw={
                        "category": "Dental clinic",
                        "review_count": idx * 50,
                        "rating": 4.5,
                        "all_emails": [f"owner{idx}@clinic{idx}.ae"],
                    },
                    email=f"owner{idx}@clinic{idx}.ae",
                    url=f"https://clinic{idx}.ae",
                )
            )

        selected = balanced_funnel.select_source_batch(leads, config)

        self.assertEqual(len(selected), 9)
        self.assertGreater(
            selected[0]["raw"]["review_count"],
            selected[-1]["raw"]["review_count"],
        )


if __name__ == "__main__":
    unittest.main()
