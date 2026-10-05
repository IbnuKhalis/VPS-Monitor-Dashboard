import unittest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services.service_catalog import get_service_catalog, CANONICAL_SERVICES
from app.services.kuma_service import (
    parse_kuma_status_payload,
    get_kuma_status_summary,
    _cache,
)
from app.services.docker_service import sanitize_log_output, get_container_logs
from app.services.dineva_service import get_dineva_status, _extract_safe_last_activity
from app.auth import create_csrf_token
from app.services.operations_service import reset_operations_state_for_tests


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


class TestOperationsSecurityAndExecution(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        reset_operations_state_for_tests()

    def _login_and_get_session(self) -> str:
        login_resp = self.client.post("/api/verify-pin", json={"pin": settings.dashboard_pin})
        self.assertEqual(login_resp.status_code, 200)
        session_id = self.client.cookies.get(settings.session_cookie_name)
        self.assertIsNotNone(session_id)
        return session_id

    def test_operations_anonymous_access_denied(self):
        # All operation endpoints require authenticated session
        resp_allowed = self.client.get("/api/operations/allowed")
        self.assertEqual(resp_allowed.status_code, 401)

        resp_audit = self.client.get("/api/operations/audit")
        self.assertEqual(resp_audit.status_code, 401)

        resp_exec = self.client.post("/api/operations/execute", json={"target": "cv-builder", "action": "restart"})
        self.assertEqual(resp_exec.status_code, 401)

    def test_csrf_token_required_and_validated(self):
        self._login_and_get_session()

        # Missing CSRF header -> 403 Forbidden
        resp_missing = self.client.post(
            "/api/operations/execute",
            json={"target": "cv-builder", "action": "restart"},
        )
        self.assertEqual(resp_missing.status_code, 403)
        self.assertIn("CSRF", resp_missing.json()["detail"])

        # Invalid CSRF header -> 403 Forbidden
        resp_invalid = self.client.post(
            "/api/operations/execute",
            headers={"X-CSRF-Token": "invalid_fake_token_value_999"},
            json={"target": "cv-builder", "action": "restart"},
        )
        self.assertEqual(resp_invalid.status_code, 403)
        self.assertIn("CSRF", resp_invalid.json()["detail"])

    def test_non_allowlisted_target_and_action_rejected(self):
        session_id = self._login_and_get_session()
        csrf_token = create_csrf_token(session_id)

        # Non-allowlisted target -> 403 Forbidden
        for bad_target in ["caddy-proxy", "vaultwarden", "nine-router", "uptime-kuma", "unknown"]:
            resp = self.client.post(
                "/api/operations/execute",
                headers={"X-CSRF-Token": csrf_token},
                json={"target": bad_target, "action": "restart"},
            )
            self.assertEqual(resp.status_code, 403)
            self.assertIn("tidak diizinkan", resp.json()["detail"])

        # Disallowed action on valid target -> 400 Bad Request
        for bad_action in ["stop", "start", "destroy", "rm", "exec"]:
            resp = self.client.post(
                "/api/operations/execute",
                headers={"X-CSRF-Token": csrf_token},
                json={"target": "cv-builder", "action": bad_action},
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("tidak didukung", resp.json()["detail"])

    def test_successful_restart_execution_and_health_verification(self):
        session_id = self._login_and_get_session()
        csrf_token = create_csrf_token(session_id)

        # GET allowed operations
        resp_allowed = self.client.get("/api/operations/allowed")
        self.assertEqual(resp_allowed.status_code, 200)
        data_allowed = resp_allowed.json()
        self.assertIn("cv-builder", data_allowed["allowed_operations"])
        self.assertEqual(data_allowed["allowed_operations"]["cv-builder"], ["restart"])

        # Execute restart
        resp_exec = self.client.post(
            "/api/operations/execute",
            headers={"X-CSRF-Token": csrf_token},
            json={"target": "cv-builder", "action": "restart"},
        )
        self.assertEqual(resp_exec.status_code, 200)
        res_json = resp_exec.json()
        self.assertTrue(res_json["success"])
        self.assertTrue(res_json["health_verified"])
        self.assertEqual(res_json["target"], "cv-builder")
        self.assertEqual(res_json["action"], "restart")
        self.assertTrue(res_json["correlation_id"].startswith("op-"))
        self.assertGreaterEqual(res_json["duration_ms"], 0)

        # Verify audit trail
        resp_audit = self.client.get("/api/operations/audit")
        self.assertEqual(resp_audit.status_code, 200)
        audit_trail = resp_audit.json()["audit_trail"]
        self.assertGreaterEqual(len(audit_trail), 1)
        latest = audit_trail[0]
        self.assertEqual(latest["correlation_id"], res_json["correlation_id"])
        self.assertEqual(latest["target"], "cv-builder")
        self.assertEqual(latest["action"], "restart")
        self.assertEqual(latest["status"], "SUCCESS")
        self.assertTrue(latest["health_verified"])
        # Ensure no secrets leaked
        for secret_word in ["token", "secret", "password", "key"]:
            self.assertNotIn(secret_word, latest.get("error", "").lower())

    def test_rate_limiting_enforcement(self):
        session_id = self._login_and_get_session()
        csrf_token = create_csrf_token(session_id)

        # First execution -> Success
        resp1 = self.client.post(
            "/api/operations/execute",
            headers={"X-CSRF-Token": csrf_token},
            json={"target": "cv-builder", "action": "restart"},
        )
        self.assertEqual(resp1.status_code, 200)

        # Immediate second execution -> 429 Too Many Requests
        resp2 = self.client.post(
            "/api/operations/execute",
            headers={"X-CSRF-Token": csrf_token},
            json={"target": "cv-builder", "action": "restart"},
        )
        self.assertEqual(resp2.status_code, 429)
        self.assertIn("Batas waktu antar-operasi", resp2.json()["detail"])

    def test_html_index_includes_csrf_meta_and_restart_button(self):
        self._login_and_get_session()
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn('name="csrf-token"', html)
        self.assertIn('openOperationModal(c.name, \'restart\')', html)
        self.assertIn('Konfirmasi Operasi Produksi', html)
        self.assertIn('Audit Trail Operasi Produksi', html)


if __name__ == "__main__":
    unittest.main()

