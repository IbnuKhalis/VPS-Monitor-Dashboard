import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import docker

# Dedicated Restricted Ops Executor Daemon (Phase 5 / Re-audit R1)
# Enforces strictly minimum necessary permissions for allowlisted container operations
# and provides isolated read-only Docker telemetry so web dashboard NEVER mounts raw docker.sock.
# NEVER exposed to public reverse proxy or outside network.

PORT = int(os.environ.get("OPS_EXECUTOR_PORT", "9092"))
HOST = os.environ.get("OPS_EXECUTOR_HOST", "0.0.0.0")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").lower()
AUTH_TOKEN = os.environ.get("OPS_EXECUTOR_TOKEN", "")

DISALLOWED_TOKENS = {
    "",
    "vps-ops-executor-internal-secret-token-prod",
    "change-me",
    "default-token",
    "secret-token-prod",
}


def validate_executor_configuration() -> None:
    """Validate executor auth token in production mode (Codex S1)."""
    if ENVIRONMENT in ("production", "prod"):
        if not AUTH_TOKEN or AUTH_TOKEN in DISALLOWED_TOKENS or "secret-token-prod" in AUTH_TOKEN.lower() or len(AUTH_TOKEN) < 16:
            logger.critical("CRITICAL: OPS_EXECUTOR_TOKEN cannot be empty or default fallback in production mode.")
            sys.exit(1)


ALLOWED_TARGETS = frozenset(["cv-builder"])
ALLOWED_ACTIONS = frozenset(["restart"])

MAX_LOG_CHARS = 50000
CONTAINER_NAME_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")

SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.\~]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:X-Ops-Token|OPS_PROXY_TOKEN)\s*[:=]\s*)[A-Za-z0-9_\-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|token|api_?key|private_?key))(\s*[:=]\s*)(['\"]?)([^\'\"\s\r\n]{4,})\3"), r"\1\2\3[REDACTED]\3"),
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"), "[PRIVATE KEY REDACTED]"),
]

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


def sanitize_log_output(text: str) -> str:
    if not text:
        return ""
    sanitized = text
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def calculate_cpu_percent(stats: dict) -> float:
    try:
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - precpu_stats.get(
            "cpu_usage", {}
        ).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        online_cpus = cpu_stats.get("online_cpus") or len(
            cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or [1]
        )
        if online_cpus == 0:
            online_cpus = 1
        if system_delta > 0.0 and cpu_delta > 0.0:
            return round((cpu_delta / system_delta) * online_cpus * 100.0, 1)
    except Exception:
        pass
    return 0.0


def calculate_memory_stats(stats: dict) -> dict:
    try:
        mem_stats = stats.get("memory_stats", {})
        usage = mem_stats.get("usage", 0)
        limit = mem_stats.get("limit", 1)
        stats_detail = mem_stats.get("stats", {})
        cache = stats_detail.get("inactive_file", stats_detail.get("total_inactive_file", 0))
        actual_usage = max(0, usage - cache)
        percent = round((actual_usage / limit) * 100.0, 1) if limit > 0 else 0.0
        return {
            "used_mb": round(actual_usage / (1024 * 1024), 1),
            "limit_mb": round(limit / (1024 * 1024), 1),
            "percent": percent,
        }
    except Exception:
        return {"used_mb": 0.0, "limit_mb": 0.0, "percent": 0.0}


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
            logger.error(f"Executor misconfiguration: OPS_EXECUTOR_TOKEN is not set. Rejecting call from {self.client_address}")
            self._send_json(500, {"error": "server_misconfiguration", "message": "OPS_EXECUTOR_TOKEN is not configured on executor"})
            return False
        token = self.headers.get("X-Executor-Token", "")
        if not token or token != AUTH_TOKEN:
            logger.warning(f"Unauthorized executor call from {self.client_address}")
            self._send_json(401, {"error": "unauthorized", "message": "Invalid or missing X-Executor-Token"})
            return False
        return True

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        # 1. Health check
        if path in ("/healthz", "/health"):
            self._send_json(200, {
                "status": "ok",
                "service": "ops-executor",
                "allowed_targets": list(ALLOWED_TARGETS),
                "allowed_actions": list(ALLOWED_ACTIONS),
            })
            return

        if not self._check_auth():
            return

        client = get_docker()
        if not client:
            self._send_json(503, {
                "error": "docker_unavailable",
                "message": "Docker socket not accessible on executor host",
            })
            return

        # 2. Read-only list containers (/containers)
        if path == "/containers":
            try:
                containers = client.containers.list(all=True)
                result = []
                running_count = 0
                for c in containers:
                    status = c.status.lower()
                    is_running = status == "running"
                    if is_running:
                        running_count += 1
                    health = "unknown"
                    state_dict = c.attrs.get("State", {})
                    if "Health" in state_dict:
                        health = state_dict["Health"].get("Status", "unknown")

                    mem_data = {"used_mb": 0.0, "limit_mb": 0.0, "percent": 0.0}
                    cpu_pct = 0.0
                    if is_running:
                        try:
                            stats = c.stats(stream=False)
                            mem_data = calculate_memory_stats(stats)
                            cpu_pct = calculate_cpu_percent(stats)
                        except Exception:
                            pass

                    ports_raw = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                    ports_summary = []
                    if ports_raw:
                        for port_proto, bindings in ports_raw.items():
                            if bindings:
                                for b in bindings:
                                    ports_summary.append(f"{b.get('HostPort')}:{port_proto}")
                            else:
                                ports_summary.append(port_proto)

                    image_str = c.attrs.get("Config", {}).get("Image", "")
                    try:
                        if c.image and c.image.tags:
                            image_str = c.image.tags[0]
                        elif c.image:
                            image_str = c.image.short_id
                    except Exception:
                        pass
                    if not image_str:
                        image_str = "unknown"

                    result.append({
                        "id": c.short_id,
                        "name": c.name,
                        "image": image_str,
                        "status": status,
                        "health": health,
                        "is_running": is_running,
                        "created": c.attrs.get("Created", "")[:19].replace("T", " "),
                        "restart_count": state_dict.get("RestartCount", 0),
                        "ports": ", ".join(ports_summary) if ports_summary else "-",
                        "cpu_percent": cpu_pct,
                        "memory": mem_data,
                    })

                result.sort(key=lambda x: (not x["is_running"], x["name"]))
                self._send_json(200, {
                    "available": True,
                    "containers": result,
                    "running_count": running_count,
                    "total_count": len(containers),
                })
            except Exception as e:
                logger.error(f"Error listing containers: {e}")
                self._send_json(500, {"available": False, "error": str(e), "containers": [], "running_count": 0, "total_count": 0})
            return

        # 3. Read-only single container logs (/containers/{name}/logs)
        logs_match = re.match(r"^/containers/([^/]+)/logs$", path)
        if logs_match:
            container_name = logs_match.group(1)
            if not CONTAINER_NAME_REGEX.match(container_name):
                self._send_json(400, {"success": False, "error": f"Invalid container name: '{container_name}'"})
                return

            tail = 60
            if "tail" in query_params:
                try:
                    tail = max(1, min(500, int(query_params["tail"][0])))
                except ValueError:
                    pass

            try:
                container = client.containers.get(container_name)
                logs_bytes = container.logs(tail=tail, timestamps=True)
                logs_text = logs_bytes.decode("utf-8", errors="replace")

                truncated_notice = ""
                if len(logs_text) > MAX_LOG_CHARS:
                    logs_text = logs_text[-MAX_LOG_CHARS:]
                    truncated_notice = "[... Log dipotong demi keamanan & batas ukuran (maks 50KB) ...]\n"

                sanitized = truncated_notice + sanitize_log_output(logs_text)
                self._send_json(200, {
                    "success": True,
                    "name": container.name,
                    "status": container.status,
                    "logs": sanitized,
                })
            except docker.errors.NotFound:
                self._send_json(404, {"success": False, "error": f"Container '{container_name}' not found", "logs": ""})
            except Exception as e:
                self._send_json(500, {"success": False, "error": str(e), "logs": ""})
            return

        # 4. Read-only single container inspect (/containers/{name}/inspect)
        inspect_match = re.match(r"^/containers/([^/]+)/inspect$", path)
        if inspect_match:
            container_name = inspect_match.group(1)
            if not CONTAINER_NAME_REGEX.match(container_name):
                self._send_json(400, {"success": False, "error": f"Invalid container name: '{container_name}'"})
                return

            try:
                container = client.containers.get(container_name)
                state = container.attrs.get("State", {})
                is_running = container.status.lower() == "running"
                health = state.get("Health", {}).get("Status", "unknown").lower()
                self._send_json(200, {
                    "success": True,
                    "id": container.short_id,
                    "name": container.name,
                    "status": container.status,
                    "is_running": is_running,
                    "health": health,
                    "state": state,
                })
            except docker.errors.NotFound:
                self._send_json(404, {"success": False, "error": f"Container '{container_name}' not found"})
            except Exception as e:
                self._send_json(500, {"success": False, "error": str(e)})
            return

        self._send_json(404, {"error": "not_found", "message": f"Endpoint '{path}' not found"})

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
    validate_executor_configuration()
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
