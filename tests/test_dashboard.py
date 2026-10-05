import unittest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services.service_catalog import get_service_catalog, CANONICAL_SERVICES
from app.services.kuma_service import (
    parse_kuma_status_payload,
    build_unknown_summary,
    get_kuma_status_summary,
    _cache,
)
from app.services.docker_service import sanitize_log_output, get_container_logs
from app.services.dineva_service import get_dineva_status, _extract_safe_last_activity


SAMPLE_PAGE_DATA = {
    "config": {
        "slug": "default",
        "title": "DigitalNeeds System Status",
        "published": True,
    },
    "incident": None,
    "publicGroupList": [
        {
            "id": 1,
            "name": "Situs & Aplikasi Publik",
            "weight": 1,
            "monitorList": [
                {"id": 1, "name": "Main Domain", "sendUrl": 1, "type": "http", "url": "https://digitalneeds.my.id"},
                {"id": 2, "name": "Graduance", "sendUrl": 1, "type": "http", "url": "https://graduance.digitalneeds.my.id"},
                {"id": 4, "name": "ATS CV Builder", "sendUrl": 1, "type": "http", "url": "https://cv.digitalneeds.my.id"},
            ],
        },
        {
            "id": 2,
            "name": "Layanan Operasional",
            "weight": 2,
            "monitorList": [
                {"id": 3, "name": "Mission Control Dashboard", "sendUrl": 0, "type": "http"},
            ],
        },
    ],
}

SAMPLE_HEARTBEAT_HEALTHY = {
    "heartbeatList": {
        "1": [{"status": 1, "time": "2026-04-12 18:15:00.000", "msg": "200 - OK", "ping": 120}],
        "2": [{"status": 1, "time": "2026-04-12 18:15:02.000", "msg": "200 - OK", "ping": 185}],
        "3": [{"status": 1, "time": "2026-04-12 18:15:03.000", "msg": "200 - OK", "ping": 95}],
        "4": [{"status": 1, "time": "2026-04-12 18:15:05.000", "msg": "200 - OK", "ping": 140}],
    },
    "uptimeList": {
        "1_24": 1.0,
        "2_24": 0.9995,
        "3_24": 1.0,
        "4_24": 1.0,
    },
}


class TestServiceCatalog(unittest.TestCase):
    def test_nine_canonical_domains_present_without_www_duplicates(self):
        catalog = get_service_catalog()
        self.assertEqual(catalog["total"], 9)
        domains = [s["domain"] for s in catalog["services"]]
        expected_domains = [
            "digitalneeds.my.id",
            "graduance.digitalneeds.my.id",
            "cv.digitalneeds.my.id",
            "status.digitalneeds.my.id",
            "dashboard.digitalneeds.my.id",
            "router.digitalneeds.my.id",
            "vault.digitalneeds.my.id",
            "sync.digitalneeds.my.id",
            "brain.digitalneeds.my.id",
        ]
        self.assertEqual(domains, expected_domains)
        for d in domains:
            self.assertFalse(d.startswith("www."), f"Duplicate www domain found: {d}")

    def test_categories_and_current_dashboard_marker(self):
        catalog = get_service_catalog()
        category_names = [c["name"] for c in catalog["categories"]]
        self.assertEqual(
            category_names,
            ["Situs dan aplikasi", "Operasional", "Integrasi dan API"],
        )
        current_services = [s for s in CANONICAL_SERVICES if s.get("is_current")]
        self.assertEqual(len(current_services), 1)
        self.assertEqual(current_services[0]["domain"], "dashboard.digitalneeds.my.id")


class TestKumaContractAndFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cache["data"] = None
        _cache["timestamp"] = 0.0

    def test_normal_healthy_response(self):
        summary = parse_kuma_status_payload(SAMPLE_PAGE_DATA, SAMPLE_HEARTBEAT_HEALTHY)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["overall_status"], "HEALTHY")
        self.assertEqual(summary["overall_label"], "Semua Layanan Sehat")
        self.assertEqual(summary["total_monitors"], 4)
        self.assertEqual(summary["up_monitors"], 4)
        self.assertEqual(summary["down_monitors"], 0)
        self.assertEqual(len(summary["degraded_monitors"]), 0)

    def test_one_service_down_marks_degraded(self):
        hb_down = {
            "heartbeatList": {
                **SAMPLE_HEARTBEAT_HEALTHY["heartbeatList"],
                "2": [{"status": 0, "time": "2026-04-12 18:16:00.000", "msg": "502 Bad Gateway", "ping": 0}],
            },
            "uptimeList": SAMPLE_HEARTBEAT_HEALTHY["uptimeList"],
        }
        summary = parse_kuma_status_payload(SAMPLE_PAGE_DATA, hb_down)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["overall_status"], "DEGRADED")
        self.assertEqual(summary["up_monitors"], 3)
        self.assertEqual(summary["down_monitors"], 1)
        self.assertEqual(len(summary["degraded_monitors"]), 1)
        self.assertEqual(summary["degraded_monitors"][0]["name"], "Graduance")
        self.assertEqual(summary["degraded_monitors"][0]["label"], "Gangguan")

    def test_unexpected_response_shape_returns_unknown_never_healthy(self):
        for bad_page, bad_hb in [
            (None, None),
            ({}, {}),
            ({"config": {}, "publicGroupList": []}, {"heartbeatList": {}, "uptimeList": {}}),
            ("invalid_string", []),
        ]:
            summary = parse_kuma_status_payload(bad_page, bad_hb)
            self.assertFalse(summary["available"])
            self.assertEqual(summary["overall_status"], "UNKNOWN")
            self.assertEqual(summary["overall_label"], "Tidak diketahui")
            self.assertNotEqual(summary["overall_status"], "HEALTHY")

    async def test_timeout_or_404_returns_unknown_fallback(self):
        with patch("app.services.kuma_service._fetch_from_base_url", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = None
            summary = await get_kuma_status_summary(force_refresh=True)
            self.assertFalse(summary["available"])
            self.assertEqual(summary["overall_status"], "UNKNOWN")
            self.assertEqual(summary["overall_label"], "Tidak diketahui")


class TestDashboardPrivacyAndRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_anonymous_get_root_hides_nine_domains_and_dashboard_shell(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Masukkan PIN untuk membuka dashboard produksi", html)
        self.assertNotIn("Direktori Layanan Aktif (9 Domain Kanonik)", html)
        for private_domain in [
            "vault.digitalneeds.my.id",
            "sync.digitalneeds.my.id",
            "brain.digitalneeds.my.id",
            "router.digitalneeds.my.id",
            "cv.digitalneeds.my.id",
        ]:
            self.assertNotIn(private_domain, html)
        self.assertIn("Kembali ke DigitalNeeds Hub", html)
        self.assertIn("https://digitalneeds.my.id", html)

    def test_anonymous_api_endpoints_require_auth(self):
        private_endpoints = [
            "/api/services",
            "/api/kuma-summary",
            "/api/metrics",
            "/api/containers",
            "/api/containers/digitalneeds04-runtime/logs",
            "/api/backup",
            "/api/health",
            "/api/dineva",
        ]
        for endpoint in private_endpoints:
            resp = self.client.get(endpoint)
            self.assertEqual(resp.status_code, 401, f"Endpoint {endpoint} should return 401 when unauthenticated")

    def test_authenticated_session_renders_all_nine_domains_and_kuma_card(self):
        login_resp = self.client.post("/api/verify-pin", json={"pin": settings.dashboard_pin})
        self.assertEqual(login_resp.status_code, 200)

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Direktori Layanan Aktif (9 Domain Kanonik)", html)
        self.assertIn("Status Layanan Publik (Uptime Kuma)", html)
        self.assertIn("Status Agen Otonom Dineva", html)
        self.assertIn("Hub Beranda", html)
        self.assertIn("Dashboard ini", html)
        self.assertIn('rel="noopener noreferrer"', html)

        for svc in CANONICAL_SERVICES:
            self.assertIn(svc["domain"], html)

        svc_resp = self.client.get("/api/services")
        self.assertEqual(svc_resp.status_code, 200)
        self.assertEqual(svc_resp.json()["total"], 9)

        dineva_resp = self.client.get("/api/dineva")
        self.assertEqual(dineva_resp.status_code, 200)
        dineva_data = dineva_resp.json()
        self.assertEqual(dineva_data["mode"], "READ_ONLY")
        self.assertFalse(dineva_data["mutations_allowed"])

    def test_log_sanitization_and_container_name_validation(self):
        raw_log_with_secrets = (
            "2026-10-06 01:00:00 INFO Initializing service\n"
            "Authorization: Bearer secret_bearer_token_xyz123\n"
            "X-Ops-Token: ops_secret_token_abc\n"
            "POSTGRES_PASSWORD=super_secret_db_pass\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
        )
        sanitized = sanitize_log_output(raw_log_with_secrets)
        self.assertNotIn("secret_bearer_token_xyz123", sanitized)
        self.assertNotIn("ops_secret_token_abc", sanitized)
        self.assertNotIn("super_secret_db_pass", sanitized)
        self.assertNotIn("MIIEowIBAAKCAQEA", sanitized)
        self.assertIn("Bearer [REDACTED]", sanitized)
        self.assertIn("X-Ops-Token: [REDACTED]", sanitized)
        self.assertIn("POSTGRES_PASSWORD=[REDACTED]", sanitized)
        self.assertIn("[PRIVATE KEY REDACTED]", sanitized)

        # Invalid container name validation
        for bad_name in ["", "../../etc/shadow", "test;rm -rf", "name with spaces"]:
            result = get_container_logs(bad_name)
            self.assertFalse(result["success"])
            self.assertIn("Invalid container name format", result["error"])

    def test_dineva_read_only_adapter_boundary(self):
        status = get_dineva_status()
        self.assertIn("agent_id", status)
        self.assertEqual(status["agent_id"], "digitalneeds04")
        self.assertEqual(status["mode"], "READ_ONLY")
        self.assertFalse(status["mutations_allowed"])
        self.assertIn("status", status)
        self.assertIn("last_activity", status)

        # Safe activity parsing from sample logs
        sample_log = (
            "2026-10-05 17:01:03,521 WARNING cron.scheduler: Job 'fc4dff40a0ab': target={'platform': 'discord'}\n"
        )
        activity = _extract_safe_last_activity(sample_log)
        self.assertEqual(activity["timestamp"], "2026-10-05 17:01:03")
        self.assertEqual(activity["component"], "cron.scheduler")
        self.assertNotIn("fc4dff40a0ab", activity["summary"])
        self.assertIn("Cron Scheduler", activity["summary"])


if __name__ == "__main__":
    unittest.main()
