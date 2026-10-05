import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
import docker

# Dedicated Restricted Ops Executor Daemon (Phase 5)
# Enforces strictly minimum necessary permissions for allowlisted container operations.
# NEVER exposed to public reverse proxy or outside network.

PORT = int(os.environ.get("OPS_EXECUTOR_PORT", "9092"))
HOST = os.environ.get("OPS_EXECUTOR_HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("OPS_EXECUTOR_TOKEN", "vps-ops-executor-internal-secret-token-prod")

ALLOWED_TARGETS = frozenset(["cv-builder"])
ALLOWED_ACTIONS = frozenset(["restart"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [OPS-EXECUTOR] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ops_executor")


def get_docker():
    try:
        return docker.from_env(timeout=5)
    except Exception as e:
        logger.error(f"Cannot connect to Docker socket: {e}")
        return None


class RestrictedExecutorHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        if not AUTH_TOKEN:
            return True
        token = self.headers.get("X-Executor-Token", "")
        if token != AUTH_TOKEN:
            logger.warning(f"Unauthorized executor call from {self.client_address}")
            self._send_json(401, {"error": "unauthorized", "message": "Invalid X-Executor-Token"})
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/healthz" or self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "service": "ops-executor",
                "allowed_targets": list(ALLOWED_TARGETS),
                "allowed_actions": list(ALLOWED_ACTIONS),
            })
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        if self.path != "/execute":
            self._send_json(404, {"error": "not_found", "message": f"Endpoint '{self.path}' not found"})
            return

        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._send_json(400, {"error": "bad_request", "message": "Empty request body"})
            return

        raw_body = self.rfile.read(content_len)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "invalid_json"})
            return

        target = payload.get("target", "").strip()
        action = payload.get("action", "").strip().lower()
        correlation_id = payload.get("correlation_id", "unknown")

        # 1. Enforce strict allowlist boundary
        if target not in ALLOWED_TARGETS:
            logger.warning(f"AUDIT DENIED [{correlation_id}]: Target '{target}' outside allowlist {list(ALLOWED_TARGETS)}")
            self._send_json(403, {
                "error": "target_not_allowed",
                "message": f"Target '{target}' is not in the approved allowlist",
                "target": target,
            })
            return

        if action not in ALLOWED_ACTIONS:
            logger.warning(f"AUDIT DENIED [{correlation_id}]: Action '{action}' outside allowlist {list(ALLOWED_ACTIONS)}")
            self._send_json(400, {
                "error": "unsupported_action",
                "message": f"Action '{action}' is not supported",
                "action": action,
            })
            return

        # 2. Execute Docker action
        client = get_docker()
        if not client:
            self._send_json(500, {"error": "docker_unavailable", "message": "Docker daemon unreachable"})
            return

        try:
            logger.info(f"AUDIT EXEC [{correlation_id}]: Executing '{action}' on allowlisted target '{target}'")
            container = client.containers.get(target)
            container.restart(timeout=10)
            logger.info(f"AUDIT SUCCESS [{correlation_id}]: Successfully restarted container '{target}'")
            self._send_json(200, {
                "success": True,
                "target": target,
                "action": action,
                "correlation_id": correlation_id,
                "status": "restarted",
            })
        except Exception as e:
            logger.error(f"AUDIT FAILURE [{correlation_id}]: Error restarting '{target}': {e}")
            self._send_json(500, {"success": False, "error": "restart_failed", "details": str(e)})


def run() -> None:
    server = HTTPServer((HOST, PORT), RestrictedExecutorHandler)
    logger.info(f"Restricted Ops Executor starting on {HOST}:{PORT}")
    logger.info(f"Allowed targets: {list(ALLOWED_TARGETS)}")
    logger.info(f"Allowed actions: {list(ALLOWED_ACTIONS)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Ops Executor...")
        server.server_close()


if __name__ == "__main__":
    run()
