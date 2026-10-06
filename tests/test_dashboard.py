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
from app.main import reset_pin_rate_limit_for_tests


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
        reset_pin_rate_limit_for_tests()
        self.client = TestClient(app, base_url="https://testserver")

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
        reset_pin_rate_limit_for_tests()
        reset_operations_state_for_tests()
        self.client = TestClient(app, base_url="https://testserver")

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

    @patch("app.services.operations_service._poll_container_health", new_callable=AsyncMock)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_successful_restart_execution_and_health_verification(self, mock_post, mock_health):
        mock_post.return_value = unittest.mock.MagicMock(status_code=200, text='{"success": true}')
        mock_health.return_value = True

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

    @patch("app.services.operations_service._poll_container_health", new_callable=AsyncMock)
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_rate_limiting_enforcement(self, mock_post, mock_health):
        mock_post.return_value = unittest.mock.MagicMock(status_code=200, text='{"success": true}')
        mock_health.return_value = True

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

    @patch("app.services.docker_service.get_docker_client")
    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_executor_failure_returns_502_and_records_failed_audit_without_bypass(self, mock_post, mock_docker):
        # Codex P1.1: When executor fails, no direct Docker fallback is attempted
        mock_post.side_effect = Exception("Connection refused to ops-executor:9092")
        mock_docker_client = unittest.mock.MagicMock()
        mock_docker.return_value = mock_docker_client

        session_id = self._login_and_get_session()
        csrf_token = create_csrf_token(session_id)

        resp = self.client.post(
            "/api/operations/execute",
            headers={"X-CSRF-Token": csrf_token},
            json={"target": "cv-builder", "action": "restart"},
        )
        self.assertEqual(resp.status_code, 502)
        self.assertIn("Gagal mengeksekusi operasi", resp.json()["detail"])
        # Verify Docker client restart was NEVER called (no privilege bypass)
        mock_docker_client.containers.get.assert_not_called()

        # Verify audit trail records FAILED
        resp_audit = self.client.get("/api/operations/audit")
        self.assertEqual(resp_audit.status_code, 200)
        trail = resp_audit.json()["audit_trail"]
        self.assertGreaterEqual(len(trail), 1)
        self.assertEqual(trail[0]["status"], "FAILED")
        self.assertFalse(trail[0]["health_verified"])

    def test_audit_persistence_and_retention_limit(self):
        # Codex P2.6: Audit log must persist to disk and enforce 50-entry cap on all paths
        from app.services.operations_service import _record_audit_entry, get_audit_trail, MAX_AUDIT_ENTRIES
        import os

        # Add 55 entries (mix of SUCCESS and FAILED)
        for i in range(55):
            status = "SUCCESS" if i % 2 == 0 else "FAILED"
            _record_audit_entry({
                "correlation_id": f"op-test-{i}",
                "timestamp": "2026-10-06T12:00:00Z",
                "actor": "owner-session",
                "target": "cv-builder",
                "action": "restart",
                "status": status,
                "health_verified": (status == "SUCCESS"),
                "duration_ms": 100 + i,
            })

        trail = get_audit_trail()
        self.assertEqual(len(trail), MAX_AUDIT_ENTRIES)
        self.assertEqual(trail[0]["correlation_id"], "op-test-54")
        self.assertTrue(os.path.exists(settings.ops_audit_file))


class TestPinRateLimitingAndSecurity(unittest.TestCase):
    def setUp(self):
        reset_pin_rate_limit_for_tests()
        self.client = TestClient(app, base_url="https://testserver")

    def test_pin_rate_limiting_lockout_after_max_attempts(self):
        # Codex P1.2: 5 wrong attempts trigger 429 lockout
        for attempt in range(1, settings.pin_rate_limit_max_attempts):
            resp = self.client.post("/api/verify-pin", json={"pin": "wrong-pin"})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Sisa percobaan", resp.json()["detail"])

        # 5th attempt reaches max attempts -> 429 Too Many Requests
        resp_5th = self.client.post("/api/verify-pin", json={"pin": "wrong-pin"})
        self.assertEqual(resp_5th.status_code, 429)
        self.assertIn("Batas percobaan PIN tercapai", resp_5th.json()["detail"])
        self.assertIn("Retry-After", resp_5th.headers)

        # Subsequent attempt during lockout -> 429
        resp_locked = self.client.post("/api/verify-pin", json={"pin": settings.dashboard_pin})
        self.assertEqual(resp_locked.status_code, 429)
        self.assertIn("Terlalu banyak percobaan", resp_locked.json()["detail"])

    def test_untrusted_peer_cannot_bypass_rate_limit_with_spoofed_headers(self):
        # Codex R2: Untrusted peer changing X-Forwarded-For must be pinned to direct socket peer IP
        from app.main import get_client_ip
        from fastapi import Request

        # Simulate untrusted external peer changing XFF on each attempt
        for i in range(1, settings.pin_rate_limit_max_attempts):
            # Client peer overridden or simulated as untrusted IP
            with patch("app.main.is_trusted_proxy_peer", return_value=False):
                resp = self.client.post(
                    "/api/verify-pin",
                    json={"pin": "wrong-pin"},
                    headers={"X-Forwarded-For": f"198.51.100.{i}"},
                )
                self.assertEqual(resp.status_code, 400)

        with patch("app.main.is_trusted_proxy_peer", return_value=False):
            resp_5th = self.client.post(
                "/api/verify-pin",
                json={"pin": "wrong-pin"},
                headers={"X-Forwarded-For": "198.51.100.99"},
            )
            # Must trigger 429 on 5th attempt despite changing X-Forwarded-For!
            self.assertEqual(resp_5th.status_code, 429)

    def test_trusted_proxy_distinguishes_multiple_valid_clients(self):
        # Codex R2: Multiple distinct clients behind trusted proxy do not interfere
        with patch("app.main.is_trusted_proxy_peer", return_value=True):
            # Client A fails 4 times
            for _ in range(4):
                resp_a = self.client.post(
                    "/api/verify-pin",
                    json={"pin": "wrong-pin"},
                    headers={"CF-Connecting-IP": "203.0.113.10"},
                )
                self.assertEqual(resp_a.status_code, 400)

            # Client B makes 1 attempt
            resp_b = self.client.post(
                "/api/verify-pin",
                json={"pin": "wrong-pin"},
                headers={"CF-Connecting-IP": "203.0.113.20"},
            )
            self.assertEqual(resp_b.status_code, 400)
            self.assertIn("Sisa percobaan: 4", resp_b.json()["detail"])

    def test_production_mode_rejects_default_credentials(self):
        # Codex R5 & S1: Production startup rejects default placeholder secrets separately
        from app.config import validate_production_secrets
        orig_env = settings.environment
        orig_pin = settings.dashboard_pin
        orig_key = settings.secret_key
        orig_token = settings.ops_executor_token

        valid_prod_pin = "87261942"
        valid_prod_key = "prod-strong-entropy-secret-key-991823712893712"
        valid_prod_token = "prod-strong-executor-token-1123498712398"

        try:
            settings.environment = "production"

            # 1. PIN rejections (default, empty, short, zeros)
            for bad_pin in ("123456", "000000", "admin", "", "12345"):
                settings.dashboard_pin = bad_pin
                settings.secret_key = valid_prod_key
                settings.ops_executor_token = valid_prod_token
                with self.assertRaises(RuntimeError, msg=f"PIN {bad_pin} should be rejected"):
                    validate_production_secrets()

            # 2. Secret Key rejections (Compose fallback, placeholder, empty)
            for bad_key in (
                "",
                "super-secret-vps-monitoring-key-change-in-prod-78234",
                "vps-monitoring-secret-key-prod-random-89234",
                "change-this-in-production",
                "secret",
            ):
                settings.dashboard_pin = valid_prod_pin
                settings.secret_key = bad_key
                settings.ops_executor_token = valid_prod_token
                with self.assertRaises(RuntimeError, msg=f"Secret key {bad_key} should be rejected"):
                    validate_production_secrets()

            # 3. Ops Executor Token rejections (Compose fallback, default token, empty)
            for bad_token in (
                "",
                "vps-ops-executor-internal-secret-token-prod",
                "change-me",
                "default-token",
                "secret-token-prod",
            ):
                settings.dashboard_pin = valid_prod_pin
                settings.secret_key = valid_prod_key
                settings.ops_executor_token = bad_token
                with self.assertRaises(RuntimeError, msg=f"Executor token {bad_token} should be rejected"):
                    validate_production_secrets()

            # 4. Valid production settings succeed
            settings.dashboard_pin = valid_prod_pin
            settings.secret_key = valid_prod_key
            settings.ops_executor_token = valid_prod_token
            # Must not raise
            validate_production_secrets()

        finally:
            settings.environment = orig_env
            settings.dashboard_pin = orig_pin
            settings.secret_key = orig_key
            settings.ops_executor_token = orig_token

    def test_ops_executor_auth_and_config_validation(self):
        # Codex S1: Executor rejects unconfigured token and enforces auth headers
        import ops_executor

        # 1. Test validate_executor_configuration in production mode
        orig_exec_env = ops_executor.ENVIRONMENT
        orig_exec_token = ops_executor.AUTH_TOKEN
        try:
            ops_executor.ENVIRONMENT = "production"
            ops_executor.AUTH_TOKEN = ""
            with self.assertRaises(SystemExit):
                ops_executor.validate_executor_configuration()

            ops_executor.AUTH_TOKEN = "vps-ops-executor-internal-secret-token-prod"
            with self.assertRaises(SystemExit):
                ops_executor.validate_executor_configuration()

            # Valid token does not exit
            ops_executor.AUTH_TOKEN = "valid-secure-token-length-greater-than-16"
            ops_executor.validate_executor_configuration()
        finally:
            ops_executor.ENVIRONMENT = orig_exec_env
            ops_executor.AUTH_TOKEN = orig_exec_token

        # 2. Test _check_auth method
        class MockHandler:
            def __init__(self, headers, client_address=("127.0.0.1", 12345)):
                self.headers = headers
                self.client_address = client_address
                self.sent_status = None
                self.sent_payload = None

            def _send_json(self, status, payload):
                self.sent_status = status
                self.sent_payload = payload

        # When AUTH_TOKEN is empty: must reject with 500
        ops_executor.AUTH_TOKEN = ""
        handler_no_secret = MockHandler({"X-Executor-Token": "anything"})
        self.assertFalse(ops_executor.RestrictedExecutorHandler._check_auth(handler_no_secret))
        self.assertEqual(handler_no_secret.sent_status, 500)

        # When AUTH_TOKEN is set: reject wrong token with 401
        ops_executor.AUTH_TOKEN = "test-secret-token-998877"
        handler_wrong = MockHandler({"X-Executor-Token": "wrong-token"})
        self.assertFalse(ops_executor.RestrictedExecutorHandler._check_auth(handler_wrong))
        self.assertEqual(handler_wrong.sent_status, 401)

        # When AUTH_TOKEN matches: accept with True
        handler_correct = MockHandler({"X-Executor-Token": "test-secret-token-998877"})
        self.assertTrue(ops_executor.RestrictedExecutorHandler._check_auth(handler_correct))

    def test_proxy_allowlist_boundaries_and_spoof_defense(self):
        # Codex S2: Allowlist strictly covers 127.0.0.1, ::1, 172.16.0.0/12; rejects 10.0.0.0/8 and 192.168.0.0/16
        from app.main import is_trusted_proxy_peer, get_client_ip
        from unittest.mock import MagicMock

        # 1. Peer trust boundaries
        self.assertFalse(is_trusted_proxy_peer("10.123.45.67"))
        self.assertFalse(is_trusted_proxy_peer("10.0.0.1"))
        self.assertFalse(is_trusted_proxy_peer("192.168.1.100"))
        self.assertFalse(is_trusted_proxy_peer("8.8.8.8"))
        self.assertFalse(is_trusted_proxy_peer("203.0.113.50"))

        self.assertTrue(is_trusted_proxy_peer("127.0.0.1"))
        self.assertTrue(is_trusted_proxy_peer("::1"))
        self.assertTrue(is_trusted_proxy_peer("172.18.0.2"))  # Docker bridge network
        self.assertTrue(is_trusted_proxy_peer("172.17.0.1"))

        # 2. Spoof defense: Rogue private peer (10.123.45.67) cannot inject CF-Connecting-IP
        mock_rogue_req = MagicMock()
        mock_rogue_req.client.host = "10.123.45.67"
        mock_rogue_req.headers = {"cf-connecting-ip": "1.1.1.1", "x-forwarded-for": "1.1.1.1"}
        client_ip = get_client_ip(mock_rogue_req)
        self.assertEqual(client_ip, "10.123.45.67")

        # 3. Legitimate proxy (172.18.0.2): CF-Connecting-IP is honored
        mock_proxy_req = MagicMock()
        mock_proxy_req.client.host = "172.18.0.2"
        mock_proxy_req.headers = {"cf-connecting-ip": "203.0.113.44"}
        client_ip_legit = get_client_ip(mock_proxy_req)
        self.assertEqual(client_ip_legit, "203.0.113.44")

    def test_audit_preflight_check_blocks_mutation_when_storage_unwritable(self):
        # Codex R4: Unwritable storage triggers 503 before contacting executor
        reset_operations_state_for_tests()
        login_resp = self.client.post("/api/verify-pin", json={"pin": settings.dashboard_pin})
        token = login_resp.cookies.get(settings.session_cookie_name)
        csrf_token = create_csrf_token(token)

        with patch("app.services.operations_service._check_audit_writable", return_value=False):
            resp = self.client.post(
                "/api/operations/execute",
                headers={"X-CSRF-Token": csrf_token},
                json={"target": "cv-builder", "action": "restart"},
            )
            self.assertEqual(resp.status_code, 503)
            self.assertIn("Penyimpanan audit trail tidak dapat ditulis", resp.json()["detail"])

    def test_audit_durability_disk_serialization_and_flag(self):
        # Codex S4: Normal save serializes audit_persisted=True to disk; failure sets False
        from app.services.operations_service import (
            _record_audit_entry,
            _load_audit_trail_from_disk,
            reset_operations_state_for_tests,
        )
        import json

        reset_operations_state_for_tests()
        test_entry = {
            "correlation_id": "op-serialization-probe",
            "timestamp": "2026-10-06T12:00:00Z",
            "actor": "owner-session",
            "target": "cv-builder",
            "action": "restart",
            "status": "SUCCESS",
            "health_verified": True,
            "duration_ms": 150,
        }

        # 1. Normal save: disk copy must have audit_persisted: True
        persisted = _record_audit_entry(test_entry)
        self.assertTrue(persisted)
        self.assertTrue(test_entry["audit_persisted"])

        # Read back directly from disk file
        with open(settings.ops_audit_file, "r", encoding="utf-8") as f:
            disk_entries = json.load(f)
        self.assertTrue(len(disk_entries) > 0)
        self.assertTrue(disk_entries[0].get("audit_persisted"), "Stored entry on disk must include audit_persisted: True")

        # 2. Disk write failure scenario
        fail_entry = {
            "correlation_id": "op-fail-durability-probe",
            "timestamp": "2026-10-06T12:01:00Z",
            "actor": "owner-session",
            "target": "cv-builder",
            "action": "restart",
            "status": "SUCCESS",
            "health_verified": True,
            "duration_ms": 160,
        }
        with patch("app.services.operations_service._save_audit_trail_to_disk", return_value=False):
            persisted_fail = _record_audit_entry(fail_entry)
            self.assertFalse(persisted_fail)
            self.assertFalse(fail_entry["audit_persisted"])

    def test_html_modal_renders_durability_warning_and_mem_only_badge(self):
        # Codex S4: HTML template includes durability warning in modal and MEM-ONLY badge in audit table
        login_resp = self.client.post("/api/verify-pin", json={"pin": settings.dashboard_pin})
        self.assertEqual(login_resp.status_code, 200)
        resp = self.client.get("/")
        html = resp.text

        # Verify modal persistence warning exists
        self.assertIn("Peringatan Durabilitas:", html)
        self.assertIn("opResult?.audit_persisted === false", html)

        # Verify audit table MEM-ONLY badge exists
        self.assertIn("MEM-ONLY", html)
        self.assertIn("item.audit_persisted === false", html)

    def test_docker_telemetry_reads_from_ops_executor_without_raw_socket(self):
        # Codex R1: Web app queries ops-executor HTTP endpoints for telemetry
        from app.services.docker_service import get_containers_summary, get_container_logs
        mock_summary = {
            "available": True,
            "containers": [{"id": "c123", "name": "cv-builder", "status": "running", "is_running": True}],
            "running_count": 1,
            "total_count": 1,
        }
        with patch("app.services.docker_service._call_executor", return_value=mock_summary):
            res = get_containers_summary()
            self.assertTrue(res["available"])
            self.assertEqual(len(res["containers"]), 1)
            self.assertEqual(res["containers"][0]["name"], "cv-builder")

    def test_session_cookie_has_secure_attribute(self):
        # Codex P2.7: Cookie must have secure attribute in HTTPS/production config
        resp = self.client.post("/api/verify-pin", json={"pin": settings.dashboard_pin})
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("set-cookie", "").lower()
        self.assertIn("secure", set_cookie)
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=lax", set_cookie)



if __name__ == "__main__":
    unittest.main()

