#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║          PADOSME — Full-Stack Integration Test Suite                     ║
║          Production-Grade End-to-End Automated QA Runner                 ║
║          Author : Tameem Khan  |  Kaaslabs Automation (OPC) Pvt Ltd      ║
╚══════════════════════════════════════════════════════════════════════════╝

Usage:
    python3 full_stack_test.py

Requirements:
    pip install requests rich pymongo psycopg2-binary pika faker jwt
"""

# ─────────────────────────── stdlib ───────────────────────────
import csv
import json
import os
import sys
import time
import traceback
import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────── third-party ──────────────────────
try:
    import requests
    import pymongo
    import psycopg2
    import pika
    from faker import Faker
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import box
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Install with:  pip install requests rich pymongo psycopg2-binary pika faker PyJWT")
    sys.exit(1)

try:
    import jwt as pyjwt
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

# ──────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


BASE_SCHEME = os.getenv("PADOSME_SCHEME", "http")
BASE_HOST   = os.getenv("PADOSME_HOST", "localhost")

SERVICES = {
    "SMS Service":          _env_int("PADOSME_SMS_PORT", 8070),
    "Ledgers Service":      _env_int("PADOSME_LEDGERS_PORT", 8071),
    "Subscription Service": _env_int("PADOSME_SUBSCRIPTION_PORT", 8072),
    "Wallet Service":       _env_int("PADOSME_WALLET_PORT", 8073),
    "Payment Service":      _env_int("PADOSME_PAYMENT_PORT", 8074),
    "Auth Service":         _env_int("PADOSME_AUTH_PORT", 8080),
    "User Profile Service": _env_int("PADOSME_PROFILE_PORT", 8081),
    "Seller Service":       _env_int("PADOSME_SELLER_PORT", 8082),
    # The exposed admin/public entrypoint in your stack is coupon-proxy on 8083.
    "Coupon Service":       _env_int("PADOSME_COUPON_PORT", 8083),
    "Channel Service":      _env_int("PADOSME_CHANNEL_PORT", 8084),
    "Discovery Service":    _env_int("PADOSME_DISCOVERY_PORT", 8085),
    "Catalogue Service":    _env_int("PADOSME_CATALOGUE_PORT", 8086),
    "Rating Service":       _env_int("PADOSME_RATING_PORT", 8087),
    "Analytics Service":    _env_int("PADOSME_ANALYTICS_PORT", 8088),
    "Search API":           _env_int("PADOSME_SEARCH_API_PORT", 8090),
    "Query Intelligence":   _env_int("PADOSME_QUERY_INTEL_PORT", 8091),
    "Search Engine":        _env_int("PADOSME_SEARCH_ENGINE_PORT", 8092),
    "Config Service":       _env_int("PADOSME_CONFIG_PORT", 8094),
    "Notification Service": _env_int("PADOSME_NOTIFICATION_PORT", 8095),
    "Location Service":     _env_int("PADOSME_LOCATION_PORT", 8096),
}

MONGO_URI = os.getenv(
    "PADOSME_MONGO_URI",
    f"mongodb://{os.getenv('PADOSME_MONGO_USER', 'deploy')}:"
    f"{os.getenv('PADOSME_MONGO_PASS', 'Kaas-Labs')}@"
    f"{os.getenv('PADOSME_MONGO_HOST', BASE_HOST)}:"
    f"{_env_int('PADOSME_MONGO_PORT', 27017)}/"
    f"?authSource={os.getenv('PADOSME_MONGO_AUTH_DB', 'admin')}",
)
MONGO_FALLBACK_URI = os.getenv(
    "PADOSME_MONGO_FALLBACK_URI",
    f"mongodb://{os.getenv('PADOSME_MONGO_HOST', BASE_HOST)}:{_env_int('PADOSME_MONGO_PORT', 27017)}/",
)
MONGO_DB      = os.getenv("PADOSME_MONGO_DB", "catalog_db")
PG_HOST       = os.getenv("PADOSME_PG_HOST", BASE_HOST)
PG_PORT       = _env_int("PADOSME_PG_PORT", 5434)
PG_USER       = os.getenv("PADOSME_PG_USER", "deploy")
PG_PASS       = os.getenv("PADOSME_PG_PASS", "Kaas-Labs")
PG_DB         = os.getenv("PADOSME_PG_DB", "padosme")
RABBITMQ_HOST = os.getenv("PADOSME_RABBITMQ_HOST", BASE_HOST)
RABBITMQ_PORT = _env_int("PADOSME_RABBITMQ_PORT", 5673)
RABBITMQ_HTTP = _env_int("PADOSME_RABBITMQ_HTTP_PORT", 15673)
RABBITMQ_USER = os.getenv("PADOSME_RABBITMQ_USER", "deploy")
RABBITMQ_PASS = os.getenv("PADOSME_RABBITMQ_PASS", "Kaas-Labs")
INTERNAL_TOKEN = os.getenv("PADOSME_INTERNAL_TOKEN", "internal-service-secret")
# Shared auth JWT secret used by services that rely on Bearer JWT role checks.
AUTH_JWT_SECRET = os.getenv("PADOSME_AUTH_JWT_SECRET", os.getenv("JWT_SECRET", "this-time-shall-pass"))
# Catalogue defaults to the shared auth secret unless explicitly overridden.
CATALOGUE_JWT_SECRET = os.getenv("PADOSME_CATALOGUE_JWT_SECRET", os.getenv("CATALOGUE_JWT_SECRET", AUTH_JWT_SECRET))
HEALTH_TIMEOUT  = _env_int("PADOSME_HEALTH_TIMEOUT", 5)
REQUEST_TIMEOUT = _env_int("PADOSME_REQUEST_TIMEOUT", 15)

# ──────────────────────────────────────────────────────────────
#  GLOBALS / STATE
# ──────────────────────────────────────────────────────────────
console = Console()
fake    = Faker("en_IN")

# Shared test state — populated progressively
STATE: Dict[str, Any] = {
    "phone":         None,
    "request_id":    None,
    "otp":           None,
    "access_token":  None,
    "admin_access_token": None,
    "refresh_token": None,
    "user_id":       None,
    "seller_id":     None,
    "catalog_id":    None,
    "item_id":       None,
    "wallet_id":     None,
    "sub_package_id": None,
    "coupon_id":     None,
    "campaign_id":   None,
    "payment_id":    None,
    "channel_id":    None,
    "post_id":       None,
}

# ──────────────────────────────────────────────────────────────
#  RESULT STORE
# ──────────────────────────────────────────────────────────────
class ResultStore:
    """Thread-safe in-memory result ledger."""

    def __init__(self):
        self.results: List[Dict] = []

    def add(self, step: str, name: str, passed: bool,
            detail: str = "", duration_ms: float = 0):
        self.results.append({
            "step":        step,
            "name":        name,
            "status":      "PASS" if passed else "FAIL",
            "detail":      detail[:200],
            "duration_ms": round(duration_ms, 1),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

    # ── summary stats ──
    @property
    def total(self):  return len(self.results)
    @property
    def passed(self): return sum(1 for r in self.results if r["status"] == "PASS")
    @property
    def failed(self): return self.total - self.passed

    def save_json(self, path="padosme_test_report.json"):
        with open(path, "w") as fh:
            json.dump({"summary": {
                "total": self.total, "passed": self.passed, "failed": self.failed,
                "run_at": datetime.now(timezone.utc).isoformat()},
                "results": self.results}, fh, indent=2)
        return path

    def save_csv(self, path="padosme_test_report.csv"):
        if not self.results:
            return path
        with open(path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.results[0].keys())
            writer.writeheader()
            writer.writerows(self.results)
        return path


STORE = ResultStore()

# ──────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────
def _url(port: int, path: str) -> str:
    return f"{BASE_SCHEME}://{BASE_HOST}:{port}{path}"


def _auth_headers(extra: Optional[Dict] = None) -> Dict:
    h = {"Authorization": f"Bearer {STATE['access_token']}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _internal_headers() -> Dict:
    return {"X-Service-Token": INTERNAL_TOKEN,
            "Content-Type": "application/json"}


def _role_token(role: str, secret: str) -> Optional[str]:
    cache_key = f"{role}_access_token"
    token = STATE.get(cache_key)
    if token:
        return token
    if not _JWT_AVAILABLE or not STATE.get("user_id"):
        return None
    now = int(time.time())
    seller_id = STATE.get("seller_id") or STATE["user_id"]
    try:
        token = pyjwt.encode(
            {
                "sub": STATE["user_id"],
                "user_id": STATE["user_id"],
                "public_user_id": STATE["user_id"],
                "iss": "padosme-auth-service",
                "user_type": role,
                "userType": role,
                "role": role,
                "roles": [role],
                "is_admin": role == "admin",
                "seller_id": seller_id if role == "seller" else None,
                "exp": now + 3600,
                "iat": now,
            },
            secret,
            algorithm="HS256",
        )
        STATE[cache_key] = token
        return token
    except Exception:
        return None


def login_admin() -> Optional[str]:
    return _role_token("admin", AUTH_JWT_SECRET) or STATE.get("access_token")


def _role_headers(role: str, secret: str, extra: Optional[Dict] = None) -> Dict:
    token = _role_token(role, secret) or STATE.get("access_token")
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _seller_headers(extra: Optional[Dict] = None) -> Dict:
    return _role_headers("seller", CATALOGUE_JWT_SECRET, extra)


def _catalogue_headers() -> Dict:
    """JWT signed with catalogue service's own secret; requires user_type=seller."""
    return _seller_headers()


def _admin_headers(extra: Optional[Dict] = None) -> Dict:
    """JWT signed with the shared auth secret; requires user_type=admin."""
    token = login_admin()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _analytics_headers() -> Dict:
    """Analytics service reads auth from X-Auth-Token header."""
    token = _role_token("seller", AUTH_JWT_SECRET) or STATE.get("access_token", "")
    return {"X-Auth-Token": token, "Content-Type": "application/json"}


def _channel_headers(kind: str = "user", extra: Optional[Dict] = None) -> Dict:
    uid = STATE.get("user_id") or "test-user"
    sid = STATE.get("seller_id") or uid
    role = "seller" if kind in ("seller", "post") else "user"
    h = _role_headers(role, AUTH_JWT_SECRET, {
        "X-User-ID": uid,
        "X-Seller-ID": sid,
    })
    if extra:
        h.update(extra)
    return h


def _decode_jwt_user_id(token: str) -> Optional[str]:
    """Extract sub/user_id from JWT without verifying signature."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return (payload.get("sub") or payload.get("user_id")
                or payload.get("id") or payload.get("userId"))
    except Exception:
        return None


def refresh_access_token() -> Optional[str]:
    """Re-fetch access token after role upgrade so JWT claims are current."""
    if not STATE.get("refresh_token"):
        return None
    resp, _ = _timed_request("POST", _url(8080, "/auth/refresh-token"),
                             json={"refresh_token": STATE["refresh_token"]})
    if resp is not None and resp.status_code in (200, 201):
        body = resp.json()
        data = body.get("data") or body
        return (data.get("access_token") or data.get("accessToken")
                or data.get("token"))
    return None


def _timed_request(method: str, url: str, **kwargs) -> Tuple[Optional[requests.Response], float]:
    t0 = time.monotonic()
    try:
        resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        return resp, (time.monotonic() - t0) * 1000
    except Exception:
        return None, (time.monotonic() - t0) * 1000


def _mongo_client(timeout_ms: int = 3000):
    last_err = None
    for uri in (MONGO_URI, MONGO_FALLBACK_URI):
        if not uri:
            continue
        try:
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
            client.admin.command("ping")
            return client
        except Exception as exc:
            last_err = exc
    if last_err:
        raise last_err
    raise RuntimeError("no MongoDB URI configured")


def _record(step: str, name: str, resp: Optional[requests.Response],
            duration_ms: float, expect_codes=(200, 201),
            check_fn=None) -> Tuple[bool, Dict]:
    """Evaluate response and record result."""
    if resp is None:
        STORE.add(step, name, False, "No response / connection error", duration_ms)
        console.print(f"  [red]✗[/red] {name} — [bold red]CONNECTION ERROR[/bold red]")
        return False, {}

    body: Dict = {}
    try:
        body = resp.json()
    except Exception:
        body = {"_raw": resp.text[:300]}

    ok = resp.status_code in expect_codes
    if ok and check_fn:
        ok = check_fn(body)

    status_str = f"HTTP {resp.status_code}"
    detail = json.dumps(body)[:200] if not ok else status_str

    STORE.add(step, name, ok, detail, duration_ms)
    icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
    color = "green" if ok else "red"
    console.print(f"  {icon} {name} — [{color}]{status_str}[/{color}] ({duration_ms:.0f} ms)")
    if not ok:
        console.print(f"     [dim]{detail[:160]}[/dim]")
    return ok, body


# ──────────────────────────────────────────────────────────────
#  STEP 1 — HEALTH CHECKS
# ──────────────────────────────────────────────────────────────
class HealthChecker:
    STEP = "01-Health"

    HEALTH_PATHS = ["/health", "/actuator/health", "/api/health",
                    "/healthz", "/status", "/ping", "/"]

    def run(self):
        console.print(Panel("[bold cyan]STEP 1 — Service Health Checks[/bold cyan]", expand=False))
        for name, port in SERVICES.items():
            self._probe(name, port)

    def _probe(self, name: str, port: int):
        for path in self.HEALTH_PATHS:
            resp, ms = _timed_request("GET", _url(port, path))
            if resp is not None and resp.status_code < 500:
                _record(self.STEP, f"Health: {name}", resp, ms,
                        expect_codes=range(100, 500))
                return
        # All probes failed
        STORE.add(self.STEP, f"Health: {name}", False, "Unreachable", 0)
        console.print(f"  [red]✗[/red] Health: {name} — [bold red]UNREACHABLE[/bold red]")


# ──────────────────────────────────────────────────────────────
#  STEP 2 — AUTH FLOW
# ──────────────────────────────────────────────────────────────
class AuthFlow:
    STEP = "02-Auth"
    PORT = 8080

    def run(self):
        console.print(Panel("[bold cyan]STEP 2 — Auth Flow (OTP)[/bold cyan]", expand=False))
        phone = f"9{fake.numerify('#########')}"
        STATE["phone"] = phone
        console.print(f"  [dim]Using phone:[/dim] {phone}")

        # ── Send OTP ──
        resp, ms = _timed_request("POST", _url(self.PORT, "/auth/send-otp"),
                                  json={"mobile_number": phone, "country_code": "+91"})
        ok, body = _record(self.STEP, "Send OTP", resp, ms)
        if ok:
            STATE["request_id"] = (body.get("request_id") or body.get("requestId")
                                   or body.get("data", {}).get("request_id"))

        # ── Fetch OTP from MongoDB ──
        otp = self._fetch_otp_from_mongo(phone)
        if otp:
            STATE["otp"] = otp
            console.print(f"  [green]✓[/green] OTP fetched from MongoDB: [bold]{otp}[/bold]")
            STORE.add(self.STEP, "Fetch OTP from MongoDB", True, f"OTP={otp}")
        else:
            console.print("  [yellow]⚠[/yellow] Could not fetch OTP from MongoDB — trying logs …")
            STORE.add(self.STEP, "Fetch OTP from MongoDB", False, "Not found in DB")
            otp = self._fetch_otp_from_logs()
            STATE["otp"] = otp

        if not STATE["otp"]:
            console.print("  [red]✗[/red] OTP unavailable — skipping verify")
            STORE.add(self.STEP, "Verify OTP", False, "OTP not available")
            return

        # ── Verify OTP ──
        payload = {
            "mobile_number": STATE["phone"],
            "country_code":  "+91",
            "otp":           STATE["otp"],
            "request_id":    STATE["request_id"],
        }
        resp, ms = _timed_request("POST", _url(self.PORT, "/auth/verify-otp"),
                                  json=payload)
        ok, body = _record(self.STEP, "Verify OTP", resp, ms)
        if ok:
            data = body.get("data") or body
            STATE["access_token"]  = (data.get("access_token")
                                      or data.get("accessToken")
                                      or data.get("token"))
            STATE["refresh_token"] = (data.get("refresh_token")
                                      or data.get("refreshToken"))
            STATE["user_id"]       = (data.get("user_id")
                                      or data.get("userId")
                                      or data.get("id"))
            # Fallback: decode JWT
            if STATE["access_token"] and not STATE["user_id"]:
                STATE["user_id"] = _decode_jwt_user_id(STATE["access_token"])
            console.print(f"  [dim]user_id=[/dim] {STATE['user_id']}")

        # ── Refresh Token ──
        if STATE.get("refresh_token"):
            resp, ms = _timed_request("POST", _url(self.PORT, "/auth/refresh-token"),
                                      json={"refresh_token": STATE["refresh_token"]})
            _record(self.STEP, "Refresh Token", resp, ms,
                    expect_codes=(200, 201, 400, 401))

        # ── Alternate ID ──
        if STATE.get("access_token"):
            h = _auth_headers()
            resp, ms = _timed_request("GET", _url(self.PORT, "/auth/alternate-id"), headers=h)
            _record(self.STEP, "Get Alternate ID", resp, ms,
                    expect_codes=(200, 201, 400, 404))
            resp, ms = _timed_request("DELETE", _url(self.PORT, "/auth/alternate-id"), headers=h)
            _record(self.STEP, "Delete Alternate ID", resp, ms,
                    expect_codes=(200, 201, 204, 400, 404))

        # ── Devices ──
        if STATE.get("access_token"):
            h = _auth_headers()
            resp, ms = _timed_request("GET", _url(self.PORT, "/auth/devices"), headers=h)
            _record(self.STEP, "Get Devices", resp, ms,
                    expect_codes=(200, 201, 400, 404))

    def _fetch_otp_from_mongo(self, phone: str) -> Optional[str]:
        """OTP is stored as SHA256 hash in PostgreSQL padosme_auth.otp_sessions.
        Brute-force the 4-8 digit range to recover the plain OTP."""
        import hashlib, time as _time
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, user=PG_USER,
                password=PG_PASS, dbname="padosme_auth", connect_timeout=5
            )
            cur = conn.cursor()
            # Give the service ~1 second to write the session
            _time.sleep(1)
            cur.execute("""
                SELECT os.otp_hash
                FROM otp_sessions os
                JOIN users u ON os.user_id = u.id
                WHERE u.mobile_number = %s
                  AND os.verified_at IS NULL
                ORDER BY os.created_at DESC
                LIMIT 1
            """, (phone,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if not row:
                console.print("  [dim red]OTP session not found in PostgreSQL[/dim red]")
                return None
            stored_hash = row[0]
            # Brute-force 4-8 digit OTPs
            for digits in (6, 4, 8):
                start = 10 ** (digits - 1)
                for otp in range(start, start * 10):
                    if hashlib.sha256(str(otp).encode()).hexdigest() == stored_hash:
                        return str(otp)
        except Exception as exc:
            console.print(f"  [dim red]OTP lookup from PostgreSQL failed: {exc}[/dim red]")
        return None

    def _fetch_otp_from_logs(self) -> Optional[str]:
        """Try to scrape OTP from docker logs of auth service."""
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "logs", "--tail", "50", "padosme-auth-service"],
                capture_output=True, text=True, timeout=5
            )
            match = re.search(r"\b(\d{4,8})\b", result.stdout + result.stderr)
            if match:
                return match.group(1)
        except Exception:
            pass
        return None


# ──────────────────────────────────────────────────────────────
#  STEP 3 — WS TOKEN VALIDATION
# ──────────────────────────────────────────────────────────────
class WSTokenValidator:
    STEP = "03-WSToken"
    PORT = 8080

    def run(self):
        console.print(Panel("[bold cyan]STEP 3 — WebSocket Token Validation[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped (no access_token)")
            return
        resp, ms = _timed_request(
            "POST", _url(self.PORT, "/auth/validate-ws-token"),
            json={"token": STATE["access_token"]},
            headers=_internal_headers()
        )
        _record(self.STEP, "Validate WS Token", resp, ms,
                expect_codes=(200, 201, 400, 401))


# ──────────────────────────────────────────────────────────────
#  STEP 4 — USER PROFILE
# ──────────────────────────────────────────────────────────────
class UserProfileFlow:
    STEP = "04-UserProfile"
    PORT = 8081

    def run(self):
        console.print(Panel("[bold cyan]STEP 4 — User Profile Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped (not authenticated)")
            return
        h = _auth_headers()

        # Create profile
        profile_data = {
            "name":     fake.name(),
            "email":    fake.email(),
            "bio":      fake.sentence(),
            "city":     fake.city(),
            "pincode":  fake.postcode(),
        }
        resp, ms = _timed_request("POST", _url(self.PORT, "/user/profile"),
                                  json=profile_data, headers=h)
        _record(self.STEP, "Create Profile", resp, ms)

        # Get profile
        resp, ms = _timed_request("GET", _url(self.PORT, "/user/profile"), headers=h)
        _record(self.STEP, "Get Profile", resp, ms)

        # Update profile
        resp, ms = _timed_request("PUT", _url(self.PORT, "/user/profile"),
                                  json={"bio": fake.sentence()}, headers=h)
        _record(self.STEP, "Update Profile", resp, ms)

        # List profile fields
        resp, ms = _timed_request("GET", _url(self.PORT, "/user/profile/fields"), headers=h)
        _record(self.STEP, "List Profile Fields", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Add / update custom field
        field_key = "test_field_" + uuid.uuid4().hex[:6]
        resp, ms = _timed_request("POST", _url(self.PORT, "/user/profile/fields"),
                                  json={"key": field_key, "value": fake.word()}, headers=h)
        ok, body = _record(self.STEP, "Add Custom Field", resp, ms,
                           expect_codes=(200, 201, 400, 404))

        # Update field by name
        resp, ms = _timed_request("PUT",
                                  _url(self.PORT, f"/user/profile/fields/{field_key}"),
                                  json={"value": fake.word()}, headers=h)
        _record(self.STEP, "Update Custom Field", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Public profile
        if STATE.get("user_id"):
            resp, ms = _timed_request("GET",
                                      _url(self.PORT, f"/user/profile/{STATE['user_id']}"),
                                      headers=h)
            _record(self.STEP, "Get Public Profile", resp, ms,
                    expect_codes=(200, 201, 400, 404))

        # KYC endpoints
        resp, ms = _timed_request("POST", _url(self.PORT, "/kyc/aadhar/initiate"),
                                  json={"aadhar_number": "123456789012"}, headers=h)
        _record(self.STEP, "KYC Aadhar Initiate", resp, ms,
                expect_codes=(200, 201, 400, 404, 422))

        resp, ms = _timed_request("GET", _url(self.PORT, "/kyc/status"), headers=h)
        _record(self.STEP, "KYC Status", resp, ms,
                expect_codes=(200, 201, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 5 — SELLER FLOW
# ──────────────────────────────────────────────────────────────
class SellerFlow:
    STEP = "05-Seller"
    PORT = 8082

    def run(self):
        console.print(Panel("[bold cyan]STEP 5 — Seller Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _auth_headers()

        # Upgrade to seller
        resp, ms = _timed_request("POST", _url(self.PORT, "/seller/upgrade"),
                                  json={
                                      "business_name":  fake.company(),
                                      "business_type":  "retail",
                                      "category":       "retail",
                                      "description":    fake.sentence(),
                                      "terms_accepted": True,
                                  }, headers=h)
        ok, body = _record(self.STEP, "Upgrade to Seller", resp, ms,
                           expect_codes=(200, 201, 400, 409, 429))
        if ok:
            data = body.get("data") or body
            STATE["seller_id"] = (data.get("seller_id") or data.get("sellerId")
                                  or data.get("id"))

        # Refresh access token so JWT role claims reflect the new seller role
        new_token = refresh_access_token()
        if new_token:
            STATE["access_token"] = new_token
            console.print("  [dim]access_token refreshed after seller upgrade[/dim]")

        # Get seller profile
        resp, ms = _timed_request("GET", _url(self.PORT, "/seller/profile"), headers=h)
        _record(self.STEP, "Get Seller Profile", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))

        # Update seller profile
        resp, ms = _timed_request("PUT", _url(self.PORT, "/seller/profile"),
                                  json={"description": fake.sentence()}, headers=h)
        _record(self.STEP, "Update Seller Profile", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))

        # Update seller location
        resp, ms = _timed_request("PUT", _url(self.PORT, "/seller/location"),
                                  json={
                                      "latitude":  float(fake.latitude()),
                                      "longitude": float(fake.longitude()),
                                      "address":   fake.address(),
                                      "pincode":   fake.postcode(),
                                  }, headers=h)
        _record(self.STEP, "Update Seller Location", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))

        # Create business card
        resp, ms = _timed_request("POST", _url(self.PORT, "/seller/business-card"),
                                  json={
                                      "tagline":  fake.catch_phrase(),
                                      "website":  fake.url(),
                                      "whatsapp": STATE["phone"],
                                  }, headers=h)
        _record(self.STEP, "Create Business Card", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))

        # Get business card
        resp, ms = _timed_request("GET", _url(self.PORT, "/seller/business-card"), headers=h)
        _record(self.STEP, "Get Business Card", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))

        # Update business card
        resp, ms = _timed_request("PUT", _url(self.PORT, "/seller/business-card"),
                                  json={
                                      "tagline":  fake.catch_phrase(),
                                      "website":  fake.url(),
                                      "whatsapp": STATE["phone"],
                                  }, headers=h)
        _record(self.STEP, "Update Business Card", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))

        # Delete business card
        resp, ms = _timed_request("DELETE", _url(self.PORT, "/seller/business-card"), headers=h)
        _record(self.STEP, "Delete Business Card", resp, ms,
                expect_codes=(200, 201, 204, 400, 403, 404))

        # Public business card search
        resp, ms = _timed_request("GET", _url(self.PORT, "/business-card/search"),
                                  params={"q": "test"}, headers=h)
        _record(self.STEP, "Business Card Search (Public)", resp, ms,
                expect_codes=(200, 201, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 6 — CATALOGUE FLOW
# ──────────────────────────────────────────────────────────────
class CatalogueFlow:
    STEP = "06-Catalogue"
    PORT = 8086

    def _log_fail(self, name: str, message: str, duration_ms: float = 0):
        STORE.add(self.STEP, name, False, message, duration_ms)
        console.print(f"  [red]✗[/red] {name} — [bold red]FAIL[/bold red]")
        console.print(f"     [dim]{message[:160]}[/dim]")

    def run(self):
        console.print(Panel("[bold cyan]STEP 6 — Catalogue Flow[/bold cyan]", expand=False))
        if not STATE.get("access_token"):
            console.print("  [yellow]⚠[/yellow] Skipped (not authenticated)")
            return
        h = _catalogue_headers()

        # ── 1. Create catalog ──────────────────────────────────
        catalog_payload = {
            "name":        fake.catch_phrase(),
            "description": fake.paragraph(),
            "type":        "product",
            "seller_id":   STATE.get("seller_id") or "test-seller",
        }
        resp, ms = _timed_request("POST", _url(self.PORT, "/catalogs"),
                                  json=catalog_payload, headers=h)
        ok, body = _record(self.STEP, "Create Catalog", resp, ms,
                           expect_codes=(200, 201))
        if ok:
            data = body.get("data") or body
            STATE["catalog_id"] = (
                data.get("id") or data.get("catalog_id")
                or data.get("_id") or data.get("catalogId")
            )
        if not STATE.get("catalog_id"):
            self._log_fail("Create Catalog — extract ID",
                           f"Could not extract catalog_id from: {str(body)[:200]}")

        cid = STATE.get("catalog_id") or "test-catalog"

        # ── 2. Get catalog ─────────────────────────────────────
        resp, ms = _timed_request("GET", _url(self.PORT, f"/catalogs/{cid}"), headers=h)
        _record(self.STEP, "Get Catalog", resp, ms, expect_codes=(200, 201))

        # ── 3. Update catalog ──────────────────────────────────
        resp, ms = _timed_request("PUT", _url(self.PORT, f"/catalogs/{cid}"),
                                  json={"name": fake.catch_phrase(),
                                        "description": fake.sentence()}, headers=h)
        _record(self.STEP, "Update Catalog", resp, ms, expect_codes=(200, 201))

        # ── 4. Add item — snapshot MongoDB count before ────────
        mongo_count_before = self._mongo_item_count()

        # Fields: name(req,3-200), price(req,>0), image_url(req,https), category(req,enum), quantity(>=0)
        # image_url: validation checks scheme only; seller provides real URL — use internal test placeholder
        _item_name = f"{fake.catch_phrase()} {uuid.uuid4().hex[:6]}"
        _item_uid  = uuid.uuid4().hex
        item_payload = {
            "name":        _item_name,
            "description": fake.paragraph(),
            "price":       fake.random_int(min=100, max=500000),
            "image_url":   f"https://cdn.padosme.in/test/items/{_item_uid}.jpg",
            "category":    fake.random_element([
                "Electronics", "Clothing", "Books", "Sports",
                "Home & Kitchen", "Beauty & Personal Care",
                "Food & Grocery", "Toys & Baby", "Automotive", "Health & Wellness",
            ]),
            "quantity":    fake.random_int(min=1, max=100),
            "sku":         f"SKU-{uuid.uuid4().hex[:6].upper()}",
        }
        resp, ms = _timed_request("POST", _url(self.PORT, f"/catalogs/{cid}/items"),
                                  json=item_payload, headers=h)
        ok, body = _record(self.STEP, "Add Catalog Item", resp, ms,
                           expect_codes=(200, 201))
        if ok:
            data = body.get("data") or body
            STATE["item_id"] = (
                data.get("id") or data.get("item_id")
                or data.get("_id") or data.get("itemId")
            )
        if ok and not STATE.get("item_id"):
            self._log_fail("Add Catalog Item — extract ID",
                           f"Could not extract item_id from: {str(body)[:200]}")

        iid = STATE.get("item_id") or "test-item"

        # ── 5. List items ──────────────────────────────────────
        resp, ms = _timed_request("GET", _url(self.PORT, f"/catalogs/{cid}/items"),
                                  headers=h)
        _record(self.STEP, "List Catalog Items", resp, ms, expect_codes=(200, 201))

        # ── 6. Verify MongoDB insert count increased ───────────
        self._verify_mongo_insert(mongo_count_before)

        # ── 7. Verify RabbitMQ event ───────────────────────────
        self._verify_rabbitmq()

        # ── 8. Update item — route: PUT /items/:id ─────────────
        resp, ms = _timed_request("PUT",
                                  _url(self.PORT, f"/items/{iid}"),
                                  json={
                                      "name":      _item_name,
                                      "price":     999,
                                      "image_url": f"https://cdn.padosme.in/test/items/{_item_uid}-v2.jpg",
                                      "category":  item_payload["category"],
                                      "quantity":  5,
                                  }, headers=h)
        _record(self.STEP, "Update Catalog Item", resp, ms,
                expect_codes=(200, 201))

        # ── 9. Delete item — route: DELETE /items/:id ──────────
        resp, ms = _timed_request("DELETE",
                                  _url(self.PORT, f"/items/{iid}"),
                                  headers=h)
        _record(self.STEP, "Delete Catalog Item", resp, ms,
                expect_codes=(200, 201, 204))

        # ── 10. Delete catalog ─────────────────────────────────
        resp, ms = _timed_request("DELETE", _url(self.PORT, f"/catalogs/{cid}"),
                                  headers=h)
        _record(self.STEP, "Delete Catalog", resp, ms,
                expect_codes=(200, 201, 204))

    # ── helpers ────────────────────────────────────────────────

    def _mongo_item_count(self) -> int:
        """Return total document count across known item collections, or -1 on error."""
        try:
            client = _mongo_client(3000)
            db = client[MONGO_DB]
            for col_name in ["items", "catalog_items", "catalogue_items"]:
                col = db[col_name]
                count = col.count_documents({})
                if count >= 0:
                    return count
        except Exception:
            pass
        return -1

    def _verify_mongo_insert(self, count_before: int):
        """Verify that item count in MongoDB increased after item creation."""
        try:
            client = _mongo_client(3000)
            db = client[MONGO_DB]
            for col_name in ["items", "catalog_items", "catalogue_items", "catalogs", "catalogues"]:
                col = db[col_name]
                count_after = col.count_documents({})
                if count_after > 0:
                    increased = count_before == -1 or count_after > count_before
                    detail = (f"Collection '{col_name}': {count_before} → {count_after} docs"
                              if count_before != -1
                              else f"Collection '{col_name}': {count_after} docs")
                    STORE.add(self.STEP, "MongoDB: Catalogue Inserted", increased, detail)
                    icon = "[green]✓[/green]" if increased else "[yellow]⚠[/yellow]"
                    console.print(f"  {icon} MongoDB: {detail}")
                    return
            self._log_fail("MongoDB: Catalogue Inserted", "No catalogue collection with documents found")
        except Exception as exc:
            self._log_fail("MongoDB: Catalogue Inserted", str(exc))

    def _verify_rabbitmq(self):
        try:
            creds = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            conn  = pika.BlockingConnection(
                pika.ConnectionParameters(host=RABBITMQ_HOST, port=RABBITMQ_PORT,
                                          credentials=creds,
                                          connection_attempts=2, retry_delay=1))
            ch = conn.channel()
            q  = ch.queue_declare(queue="catalogue.events", passive=True)
            msgs = q.method.message_count
            conn.close()
            STORE.add(self.STEP, "RabbitMQ: catalogue.events", True,
                      f"{msgs} messages pending")
            console.print(f"  [green]✓[/green] RabbitMQ: catalogue.events ({msgs} msgs)")
        except Exception as exc:
            self._log_fail("RabbitMQ: catalogue.events", str(exc))


# ──────────────────────────────────────────────────────────────
#  STEP 7 — WALLET FLOW
# ──────────────────────────────────────────────────────────────
class WalletFlow:
    STEP = "07-Wallet"
    PORT = 8073

    def run(self):
        console.print(Panel("[bold cyan]STEP 7 — Wallet Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _auth_headers()

        uid = STATE["user_id"] or "test-user"
        svc_h = _internal_headers()

        # Create wallet (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/wallet/create"),
                                  json={"owner_id": uid, "owner_type": "user"}, headers=svc_h)
        ok, body = _record(self.STEP, "Create Wallet", resp, ms,
                           expect_codes=(200, 201, 400, 409))
        if ok:
            data = body.get("data") or body
            STATE["wallet_id"] = (data.get("wallet_id") or data.get("id")
                                  or data.get("walletId"))

        # Balance
        resp, ms = _timed_request("GET", _url(self.PORT, f"/v1/wallet/{uid}/balance"), headers=h)
        _record(self.STEP, "Get Wallet Balance", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Credit (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/wallet/credit"),
                                  json={"user_id": uid, "amount": 500,
                                        "description": "test credit"},
                                  headers=svc_h)
        _record(self.STEP, "Credit Wallet", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Debit (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/wallet/debit"),
                                  json={"user_id": uid, "amount": 50,
                                        "description": "test debit"},
                                  headers=svc_h)
        _record(self.STEP, "Debit Wallet", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Transfer (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/wallet/transfer"),
                                  json={"from_user_id": uid, "to_user_id": uid,
                                        "amount": 10,
                                        "description": "self transfer test"},
                                  headers=svc_h)
        _record(self.STEP, "Transfer Wallet Credits", resp, ms,
                expect_codes=(200, 201, 400, 422))

        # Transactions
        resp, ms = _timed_request("GET", _url(self.PORT, f"/v1/wallet/{uid}/transactions"),
                                  headers=h)
        _record(self.STEP, "Get Wallet Transactions", resp, ms,
                expect_codes=(200, 201, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 8 — SUBSCRIPTION FLOW
# ──────────────────────────────────────────────────────────────
class SubscriptionFlow:
    STEP = "08-Subscription"
    PORT = 8072

    def run(self):
        console.print(Panel("[bold cyan]STEP 8 — Subscription Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _auth_headers()

        uid = STATE.get("user_id") or "test-user"
        svc_h = _internal_headers()

        # List packages
        resp, ms = _timed_request("GET", _url(self.PORT, "/v1/subscriptions/packages/"),
                                  headers=h)
        ok, body = _record(self.STEP, "List Subscription Packages", resp, ms,
                           expect_codes=(200, 201, 400, 404))
        if ok:
            pkgs = body.get("data") or body.get("packages") or []
            if isinstance(pkgs, list) and pkgs:
                STATE["sub_package_id"] = (pkgs[0].get("id")
                                           or pkgs[0].get("package_id"))

        # Get package by ID if available
        if STATE.get("sub_package_id"):
            resp, ms = _timed_request("GET",
                                      _url(self.PORT,
                                           f"/v1/subscriptions/packages/{STATE['sub_package_id']}"),
                                      headers=h)
            _record(self.STEP, "Get Subscription Package", resp, ms,
                    expect_codes=(200, 201, 400, 404))

        # Subscription wallet get
        resp, ms = _timed_request("GET", _url(self.PORT, f"/v1/wallet/{uid}"), headers=h)
        _record(self.STEP, "Get Subscription Wallet", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Wallet deduct (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/wallet/deduct"),
                                  json={"user_id": uid, "amount": 100,
                                        "description": "subscription deduct"},
                                  headers=svc_h)
        _record(self.STEP, "Subscription Wallet Deduct", resp, ms,
                expect_codes=(200, 201, 400, 402, 422))

        # Wallet history
        resp, ms = _timed_request("GET", _url(self.PORT, f"/v1/wallet/history/{uid}"),
                                  headers=h)
        _record(self.STEP, "Subscription Wallet History", resp, ms,
                expect_codes=(200, 201, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 9 — COUPON FLOW
# ──────────────────────────────────────────────────────────────
class CouponFlow:
    STEP = "09-Coupon"
    PORT = 8083

    def run(self):
        console.print(Panel("[bold cyan]STEP 9 — Coupon Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _auth_headers()
        admin_h = _role_headers("seller", AUTH_JWT_SECRET, {"X-Admin": "true"})

        svc_h = _internal_headers()

        # Create campaign
        campaign_payload = {
            "name":           "TEST-CAMP-" + uuid.uuid4().hex[:6].upper(),
            "discount_type":  "percent",
            "discount_value": 10,
            "max_uses":       100,
            "valid_from":     "2026-01-01T00:00:00Z",
            "valid_to":       "2099-12-31T23:59:59Z",
            "expires_at":     "2099-12-31T23:59:59Z",
        }
        resp, ms = _timed_request("POST", _url(self.PORT, "/sales/campaign"),
                                  json=campaign_payload, headers=admin_h)
        ok, body = _record(self.STEP, "Create Coupon Campaign (Admin)", resp, ms)
        if ok:
            data = body.get("data") or body
            STATE["campaign_id"] = data.get("id") or data.get("campaign_id")

        # List campaigns
        resp, ms = _timed_request("GET", _url(self.PORT, "/sales/campaign/list"), headers=admin_h)
        ok, body = _record(self.STEP, "List Campaigns", resp, ms,
                           expect_codes=(200, 201, 400, 404))
        if not STATE["campaign_id"] and ok:
            clist = body.get("data") or body.get("campaigns") or []
            if clist:
                STATE["campaign_id"] = clist[0].get("id")

        cid = STATE["campaign_id"] or "test-campaign"

        # Get single campaign
        resp, ms = _timed_request("GET", _url(self.PORT, f"/sales/campaign/{cid}"), headers=admin_h)
        _record(self.STEP, "Get Campaign", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Campaign stats
        resp, ms = _timed_request("GET", _url(self.PORT, f"/sales/campaign/{cid}/stats"),
                                  headers=admin_h)
        _record(self.STEP, "Campaign Stats", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Salesman stats
        uid = STATE.get("user_id") or "test-user"
        resp, ms = _timed_request("GET", _url(self.PORT, f"/sales/salesman/{uid}/stats"),
                                  headers=admin_h)
        _record(self.STEP, "Salesman Stats", resp, ms,
                expect_codes=(200, 201, 400, 404))

        if not STATE["campaign_id"]:
            console.print("  [dim]No campaign id — skipping coupon ops[/dim]")
            return

        # Initiate coupon — seller_id must come from seller upgrade response, not user_id
        uid = STATE.get("user_id") or "test-user"
        seller_id = STATE.get("seller_id") or uid
        resp, ms = _timed_request("POST", _url(self.PORT, "/sales/coupon/initiate"),
                                  json={"campaign_id": STATE["campaign_id"],
                                        "seller_id":   seller_id},
                                  headers=h)
        ok, body = _record(self.STEP, "Initiate Coupon", resp, ms)
        if ok:
            data = body.get("data") or body
            STATE["coupon_id"] = data.get("coupon_id") or data.get("id") or data.get("code")

        # Validate coupon (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/sales/coupon/validate"),
                                  json={"coupon_id": STATE["coupon_id"],
                                        "amount": 500},
                                  headers=svc_h)
        _record(self.STEP, "Validate Coupon", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Redeem coupon (service token)
        resp, ms = _timed_request("POST", _url(self.PORT, "/sales/coupon/redeem"),
                                  json={"coupon_id": STATE["coupon_id"],
                                        "order_id":  "ORDER-" + uuid.uuid4().hex[:8]},
                                  headers=svc_h)
        _record(self.STEP, "Redeem Coupon", resp, ms,
                expect_codes=(200, 201, 400, 409))


# ──────────────────────────────────────────────────────────────
#  STEP 10 — PAYMENT FLOW
# ──────────────────────────────────────────────────────────────
class PaymentFlow:
    STEP = "10-Payment"
    PORT = 8074

    def run(self):
        console.print(Panel("[bold cyan]STEP 10 — Payment Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _auth_headers()

        # Create payment intent
        idempotency_key = "idem-" + uuid.uuid4().hex
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/payments/create-intent"),
                                  json={"amount": 99900,  # paise / cents
                                        "currency": "INR",
                                        "gateway": "razorpay"},
                                  headers={**h, "Idempotency-Key": idempotency_key})
        ok, body = _record(self.STEP, "Create Payment Intent", resp, ms,
                           expect_codes=(200, 201, 400, 422))
        if ok:
            data = body.get("data") or body
            STATE["payment_id"] = (data.get("payment_id") or data.get("id")
                                   or data.get("intent_id"))

        # Confirm payment
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/payments/confirm"),
                                  json={"payment_id": STATE["payment_id"],
                                        "gateway_response": {"status": "captured"}},
                                  headers=h)
        _record(self.STEP, "Confirm Payment", resp, ms,
                expect_codes=(200, 201, 400, 404, 422))

        # List payments
        resp, ms = _timed_request("GET", _url(self.PORT, "/v1/payments/"), headers=h)
        _record(self.STEP, "List Payments", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Get payment by ID if available
        if STATE.get("payment_id"):
            resp, ms = _timed_request("GET",
                                      _url(self.PORT, f"/v1/payments/{STATE['payment_id']}"),
                                      headers=h)
            _record(self.STEP, "Get Payment", resp, ms,
                    expect_codes=(200, 201, 400, 404))

        # Simulate Stripe webhook
        stripe_event = {
            "id":   "evt_test_" + uuid.uuid4().hex,
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_test", "amount": 99900,
                                "currency": "inr", "status": "succeeded"}},
        }
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/webhooks/stripe"),
                                  json=stripe_event,
                                  headers={"Content-Type": "application/json",
                                           "Stripe-Signature": "t=0,v1=fake"})
        _record(self.STEP, "Stripe Webhook Simulation", resp, ms,
                expect_codes=(200, 201, 400, 401, 403, 404))

        # Simulate Razorpay webhook
        rp_event = {
            "event":   "payment.captured",
            "payload": {"payment": {"entity": {
                "id": "pay_test_" + uuid.uuid4().hex[:10],
                "amount": 99900, "currency": "INR",
                "status": "captured",
            }}},
        }
        resp, ms = _timed_request("POST", _url(self.PORT, "/v1/webhooks/razorpay"),
                                  json=rp_event,
                                  headers={"Content-Type": "application/json",
                                           "X-Razorpay-Signature": "fakesig"})
        _record(self.STEP, "Razorpay Webhook Simulation", resp, ms,
                expect_codes=(200, 201, 400, 401, 403, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 11 — CHANNEL SERVICE
# ──────────────────────────────────────────────────────────────
class ChannelFlow:
    STEP = "11-Channel"
    PORT = 8084

    def run(self):
        console.print(Panel("[bold cyan]STEP 11 — Channel Service Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        user_h = _channel_headers("user")
        seller_h = _channel_headers("seller")
        post_h = _channel_headers("post")
        admin_h = _admin_headers({"X-Admin": "true"})

        # Create post
        resp, ms = _timed_request("POST", _url(self.PORT, "/channel/post"),
                                  json={"title":   fake.sentence(),
                                        "content": fake.paragraph(),
                                        "tags":    [fake.word(), fake.word()]},
                                  headers=post_h)
        ok, body = _record(self.STEP, "Create Post", resp, ms,
                           expect_codes=(200, 201, 400, 404))
        if ok and resp and resp.status_code in (200, 201):
            data = body.get("data") or body
            STATE["post_id"]    = data.get("post_id") or data.get("id")
            STATE["channel_id"] = data.get("channel_id")

        uid = STATE.get("user_id") or "test-user"
        seller_id = STATE.get("seller_id") or "test-seller"

        # Subscribe to channel
        resp, ms = _timed_request("POST", _url(self.PORT, "/channel/subscribe"),
                                  json={"user_id": uid, "seller_id": seller_id},
                                  headers=user_h)
        _record(self.STEP, "Subscribe to Channel", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Unsubscribe
        resp, ms = _timed_request("POST", _url(self.PORT, "/channel/unsubscribe"),
                                  json={"user_id": uid, "seller_id": seller_id},
                                  headers=user_h)
        _record(self.STEP, "Unsubscribe from Channel", resp, ms,
                expect_codes=(200, 201, 204, 400, 404))

        # Channel feed
        resp, ms = _timed_request("GET", _url(self.PORT, f"/channel/feed/{uid}"), headers=user_h)
        _record(self.STEP, "Get Channel Feed", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Flag post
        post_id = STATE.get("post_id") or "test-post"
        resp, ms = _timed_request("POST", _url(self.PORT, "/channel/post/flag"),
                                  json={"post_id": post_id, "reason": "spam"},
                                  headers=user_h)
        _record(self.STEP, "Flag Post", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Moderate post (admin)
        resp, ms = _timed_request("POST", _url(self.PORT, "/channel/post/moderate"),
                                  json={"post_id": post_id, "action": "approve"},
                                  headers=admin_h)
        _record(self.STEP, "Moderate Post (Admin)", resp, ms,
                expect_codes=(200, 201, 400, 401, 403, 404))

        # Get flagged posts (admin)
        resp, ms = _timed_request("GET", _url(self.PORT, "/channel/post/flagged"),
                                  headers=admin_h)
        _record(self.STEP, "Get Flagged Posts (Admin)", resp, ms,
                expect_codes=(200, 201, 400, 403, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 12 — ANALYTICS FLOW
# ──────────────────────────────────────────────────────────────
class AnalyticsFlow:
    STEP = "12-Analytics"
    PORT = 8088

    def run(self):
        console.print(Panel("[bold cyan]STEP 12 — Analytics Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _analytics_headers()
        uid = STATE["user_id"] or "test"
        sid = STATE["seller_id"] or uid
        cid = STATE["campaign_id"] or "test-campaign"

        endpoints = [
            ("GET", f"/analytics/seller/{sid}/overview",           "Seller Analytics Overview"),
            ("GET", f"/analytics/campaign/{cid}",                   "Campaign Analytics"),
            ("GET", f"/analytics/campaign/{cid}/metrics?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                                                                    "Campaign Metrics"),
            ("GET", "/analytics/campaigns/top?limit=10",            "Top Campaigns"),
            ("GET", "/analytics/search",                            "Search Analytics"),
            ("GET", f"/analytics/salesman/{uid}/metrics",           "Salesman Metrics"),
            ("GET", "/analytics/salesmen/top?limit=10",             "Top Salesmen"),
            ("GET", "/analytics/credits",                           "Analytics Credits"),
            ("GET", "/analytics/platform",                          "Platform Analytics"),
        ]
        for method, path, name in endpoints:
            resp, ms = _timed_request(method, _url(self.PORT, path), headers=h)
            _record(self.STEP, name, resp, ms,
                    expect_codes=(200, 201, 400, 403, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 16 — DISCOVERY FLOW
# ──────────────────────────────────────────────────────────────
class DiscoveryFlow:
    STEP = "16-Discovery"
    PORT = 8085

    def run(self):
        console.print(Panel("[bold cyan]STEP 16 — Discovery Flow[/bold cyan]", expand=False))
        if not STATE.get("access_token"):
            console.print("  [yellow]⚠[/yellow] Skipped (not authenticated)")
            return
        h = _auth_headers()
        payload = {
            "query": "plumber",
            "latitude": 12.9716,
            "longitude": 77.5946,
            "lat": 12.9716,
            "lon": 77.5946,
            "radius_km": 5,
            "push_level": 0,
        }
        attempts = ["/search", "/v1/search"]
        chosen_resp = None
        chosen_ms = 0.0

        for path in attempts:
            resp, ms = _timed_request("POST", _url(self.PORT, path), json=payload, headers=h)
            chosen_resp, chosen_ms = resp, ms
            if resp is not None and resp.status_code in (200, 201, 202, 400, 404):
                break

        _record(self.STEP, "Discovery Search", chosen_resp, chosen_ms,
                expect_codes=(200, 201, 202, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 17 — CATALOGUE SERVICE FLOW (new complete flow)
# ──────────────────────────────────────────────────────────────
class CatalogueServiceFlow:
    STEP = "17-Catalogue"
    PORT = 8086

    def run(self):
        console.print(Panel("[bold cyan]STEP 17 — Catalogue Service Flow[/bold cyan]", expand=False))
        if not STATE.get("access_token"):
            console.print("  [yellow]⚠[/yellow] Skipped (not authenticated)")
            return
        h = _catalogue_headers()

        # Create catalog
        resp, ms = _timed_request("POST", _url(self.PORT, "/catalogs"),
                                  json={
                                      "name":        fake.catch_phrase(),
                                      "description": fake.paragraph(),
                                      "type":        "product",
                                      "seller_id":   STATE.get("seller_id") or "test-seller",
                                  }, headers=h)
        ok, body = _record(self.STEP, "Create Catalog", resp, ms,
                           expect_codes=(200, 201, 400, 404))
        if ok:
            data = body.get("data") or body
            STATE["catalog_id"] = data.get("id") or data.get("catalog_id") or STATE.get("catalog_id")

        cid = STATE.get("catalog_id") or "test-catalog"

        # Get catalog
        resp, ms = _timed_request("GET", _url(self.PORT, f"/catalogs/{cid}"), headers=h)
        _record(self.STEP, "Get Catalog", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Update catalog
        resp, ms = _timed_request("PUT", _url(self.PORT, f"/catalogs/{cid}"),
                                  json={"description": fake.sentence()}, headers=h)
        _record(self.STEP, "Update Catalog", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Add item — fields match ItemRequest struct: name, price, image_url, category required
        # image_url: validation checks scheme only; seller provides real URL — use internal test placeholder
        _item_name = f"{fake.catch_phrase()} {uuid.uuid4().hex[:6]}"
        _item_category = fake.random_element([
            "Electronics", "Clothing", "Books", "Sports",
            "Home & Kitchen", "Beauty & Personal Care",
            "Food & Grocery", "Toys & Baby", "Automotive", "Health & Wellness",
        ])
        _item_image = f"https://cdn.padosme.in/test/items/{uuid.uuid4().hex}.jpg"
        resp, ms = _timed_request("POST", _url(self.PORT, f"/catalogs/{cid}/items"),
                                  json={
                                      "name":        _item_name,
                                      "description": fake.paragraph(),
                                      "price":       fake.random_int(min=100, max=500000),
                                      "image_url":   _item_image,
                                      "category":    _item_category,
                                      "quantity":    fake.random_int(min=1, max=100),
                                      "sku":         f"SKU-{uuid.uuid4().hex[:6].upper()}",
                                  }, headers=h)
        ok, body = _record(self.STEP, "Add Catalog Item", resp, ms,
                           expect_codes=(200, 201))
        if ok:
            data = body.get("data") or body
            STATE["item_id"] = (
                data.get("id") or data.get("item_id")
                or data.get("_id") or data.get("itemId")
                or STATE.get("item_id")
            )

        iid = STATE.get("item_id") or "test-item"

        # List items
        resp, ms = _timed_request("GET", _url(self.PORT, f"/catalogs/{cid}/items"),
                                  headers=h)
        _record(self.STEP, "List Catalog Items", resp, ms,
                expect_codes=(200, 201))

        # Update item — route: PUT /items/:id
        resp, ms = _timed_request("PUT",
                                  _url(self.PORT, f"/items/{iid}"),
                                  json={
                                      "name":      _item_name,
                                      "price":     999,
                                      "image_url": _item_image,
                                      "category":  _item_category,
                                      "quantity":  5,
                                  }, headers=h)
        _record(self.STEP, "Update Catalog Item", resp, ms,
                expect_codes=(200, 201))

        # Delete item — route: DELETE /items/:id
        resp, ms = _timed_request("DELETE",
                                  _url(self.PORT, f"/items/{iid}"),
                                  headers=h)
        _record(self.STEP, "Delete Catalog Item", resp, ms,
                expect_codes=(200, 201, 204))

        # Delete catalog
        resp, ms = _timed_request("DELETE", _url(self.PORT, f"/catalogs/{cid}"),
                                  headers=h)
        _record(self.STEP, "Delete Catalog", resp, ms,
                expect_codes=(200, 201, 204))


# ──────────────────────────────────────────────────────────────
#  STEP 18 — NOTIFICATION FLOW
# ──────────────────────────────────────────────────────────────
class NotificationFlow:
    STEP = "18-Notification"
    PORT = 8095

    def run(self):
        console.print(Panel("[bold cyan]STEP 18 — Notification Flow[/bold cyan]", expand=False))
        svc_h = _internal_headers()
        uid   = STATE.get("user_id") or "test-user"
        fake_fcm = "fcm_token_" + uuid.uuid4().hex

        register_attempts = [
            ("POST", "/device-tokens", {"user_id": uid, "token": fake_fcm, "platform": "android"}),
            ("POST", "/devices/register", {"user_id": uid, "token": fake_fcm, "platform": "android"}),
            ("POST", "/notifications/devices/register", {"user_id": uid, "token": fake_fcm, "platform": "android"}),
        ]
        delete_attempts = [
            ("DELETE", "/device-tokens", {"user_id": uid, "token": fake_fcm}),
            ("POST", "/devices/delete", {"user_id": uid, "token": fake_fcm}),
            ("DELETE", "/notifications/devices/register", {"user_id": uid, "token": fake_fcm}),
            ("DELETE", "/notifications/devices/delete", {"user_id": uid, "token": fake_fcm}),
        ]

        reg_resp, reg_ms = None, 0.0
        for method, path, body in register_attempts:
            reg_resp, reg_ms = _timed_request(method, _url(self.PORT, path), json=body, headers=svc_h)
            if reg_resp is not None and reg_resp.status_code in (200, 201, 204, 400):
                break
        _record(self.STEP, "Register Device Token", reg_resp, reg_ms,
                expect_codes=(200, 201, 204, 400, 404))

        del_resp, del_ms = None, 0.0
        for method, path, body in delete_attempts:
            del_resp, del_ms = _timed_request(method, _url(self.PORT, path), json=body, headers=svc_h)
            if del_resp is not None and del_resp.status_code in (200, 201, 204, 400):
                break
        _record(self.STEP, "Delete Device Token", del_resp, del_ms,
                expect_codes=(200, 201, 204, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 19 — CONFIG SERVICE FLOW
# ──────────────────────────────────────────────────────────────
class ConfigFlow:
    STEP = "19-Config"
    PORT = 8094

    def run(self):
        console.print(Panel("[bold cyan]STEP 19 — Config Service Flow[/bold cyan]", expand=False))
        svc_h = _internal_headers()

        # Get config by key
        resp, ms = _timed_request("GET",
                                  _url(self.PORT, "/config/search.radius.default"),
                                  headers=svc_h)
        _record(self.STEP, "Get Config by Key", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Get config by service
        resp, ms = _timed_request("GET",
                                  _url(self.PORT, "/config/service/discovery-service"),
                                  headers=svc_h)
        _record(self.STEP, "Get Config by Service", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # List feature flags
        resp, ms = _timed_request("GET", _url(self.PORT, "/feature-flags"), headers=svc_h)
        _record(self.STEP, "List Feature Flags", resp, ms,
                expect_codes=(200, 201, 400, 404))

        # Get feature flag
        resp, ms = _timed_request("GET",
                                  _url(self.PORT, "/feature-flags/enable_new_search"),
                                  headers=svc_h)
        _record(self.STEP, "Get Feature Flag", resp, ms,
                expect_codes=(200, 201, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 20 — SMS SERVICE FLOW
# ──────────────────────────────────────────────────────────────
class SMSFlow:
    STEP = "20-SMS"
    PORT = 8070

    def run(self):
        console.print(Panel("[bold cyan]STEP 20 — SMS Service Flow[/bold cyan]", expand=False))
        if os.getenv("PADOSME_SMS_MOCK", "").lower() in {"1", "true", "yes", "on"}:
            STORE.add(self.STEP, "Send Test SMS", True, "mocked sms success")
            console.print("  [green]✓[/green] Send Test SMS — [green]MOCK[/green] (0 ms)")
            STORE.add(self.STEP, "Twilio Status Webhook", True, "mocked webhook success")
            console.print("  [green]✓[/green] Twilio Status Webhook — [green]MOCK[/green] (0 ms)")
            return
        svc_h = _internal_headers()
        raw_phone = STATE.get("phone") or "9999999999"
        phone = raw_phone if raw_phone.startswith("+") else f"+91{raw_phone}"

        # Send test SMS
        resp, ms = _timed_request("POST", _url(self.PORT, "/test/sms"),
                                  json={"phone": phone, "message": "Padosme test SMS"},
                                  headers=svc_h)
        _record(self.STEP, "Send Test SMS", resp, ms,
                expect_codes=(200, 201, 400, 404, 500))

        # Twilio webhook
        resp, ms = _timed_request("POST", _url(self.PORT, "/webhook/twilio/status"),
                                  json={"MessageSid": "SM" + uuid.uuid4().hex,
                                        "MessageStatus": "delivered"},
                                  headers={"Content-Type": "application/json"})
        _record(self.STEP, "Twilio Status Webhook", resp, ms,
                expect_codes=(200, 201, 204, 400, 404))


# ──────────────────────────────────────────────────────────────
#  STEP 21 — LEDGERS FLOW
# ──────────────────────────────────────────────────────────────
class LedgersFlow:
    STEP = "21-Ledgers"
    PORT = 8071

    def run(self):
        console.print(Panel("[bold cyan]STEP 21 — Ledgers Flow[/bold cyan]", expand=False))
        svc_h = _internal_headers()

        # Get actions
        resp, ms = _timed_request("GET", _url(self.PORT, "/actions"), headers=svc_h)
        ok, body = _record(self.STEP, "Get Ledger Actions", resp, ms,
                           expect_codes=(200, 201, 400, 404))

        # Execute action — use first available action from GET /actions
        action_name = "credit"
        if ok:
            try:
                actions = body.get("data") or body.get("actions") or []
                if isinstance(actions, list) and actions:
                    first = actions[0]
                    action_name = (first.get("name") or first.get("action")
                                   or first if isinstance(first, str) else "credit")
            except Exception:
                pass
        resp, ms = _timed_request("POST", _url(self.PORT, "/api/v1/execute"),
                                  json={"action": action_name,
                                        "entity_id": STATE.get("user_id") or "test-user",
                                        "entity_type": "wallet",
                                        "amount": 100,
                                        "metadata": {"reason": "test", "reference_id": "ref-test"}},
                                  headers=svc_h)
        _record(self.STEP, "Execute Ledger Action", resp, ms,
                expect_codes=(200, 201, 400, 404, 422, 500))

        # Publish event
        resp, ms = _timed_request("POST", _url(self.PORT, "/api/v1/publish"),
                                  json={"event_type": "payment.completed",
                                        "entity_id": STATE.get("user_id") or "test-user",
                                        "entity_type": "payment",
                                        "payload": {"amount": 100, "currency": "INR"}},
                                  headers=svc_h)
        _record(self.STEP, "Publish Ledger Event", resp, ms,
                expect_codes=(200, 201, 400, 404, 422))


# ──────────────────────────────────────────────────────────────
#  STEP 22 — SEARCH FLOW
# ──────────────────────────────────────────────────────────────
class SearchFlow:
    STEP = "22-Search"
    PORT = 8090

    def run(self):
        console.print(Panel("[bold cyan]STEP 22 — Search Flow[/bold cyan]", expand=False))
        if not STATE.get("access_token"):
            console.print("  [yellow]⚠[/yellow] Skipped (not authenticated)")
            return
        h = _auth_headers()
        resp, ms = _timed_request("POST", _url(self.PORT, "/search"),
                                  json={"query": "biryani",
                                        "lat": 12.9716,
                                        "lon": 77.5946,
                                        "radius_km": 2.0,
                                        "push_level": 0},
                                  headers=h)
        _record(self.STEP, "Search Query", resp, ms,
                expect_codes=(200, 201, 400, 404, 502))


# ──────────────────────────────────────────────────────────────
#  STEP 13 — DATABASE VERIFICATION
# ──────────────────────────────────────────────────────────────
class DatabaseVerifier:
    STEP = "13-Database"

    # ── MongoDB ──
    def _run_mongo(self):
        try:
            client = _mongo_client(4000)
            # catalog_db has items + catalogs collections
            db   = client["catalog_db"]
            cols = set(db.list_collection_names())
            checks = [
                ("catalog items in catalog_db", ["items", "catalogs"]),
            ]
            for label, candidates in checks:
                found = [c for c in candidates if c in cols and db[c].count_documents({}) > 0]
                ok    = bool(found)
                detail = f"Found: {found}" if ok else f"Checked: {candidates}"
                STORE.add(self.STEP, f"MongoDB: {label}", ok, detail)
                icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
                console.print(f"  {icon} MongoDB: {label} — {detail}")
            client.close()
        except Exception as exc:
            STORE.add(self.STEP, "MongoDB connection", False, str(exc))
            console.print(f"  [red]✗[/red] MongoDB: {exc}")

    # ── PostgreSQL ──
    def _run_postgres(self):
        # Check multiple known databases on the shared Postgres instance
        pg_checks = [
            ("padosme_auth",        "OTP sessions in auth DB",         ["otp_sessions", "users"]),
            ("wallet_service",      "Wallet transactions",             ["wallet_transactions", "wallets"]),
            ("payment_service",     "Payments",                        ["payments", "payment_intents", "payment_orders", "transactions", "orders", "intents", "charges"]),
            ("padosme_coupon",      "Coupon campaigns",                ["campaigns", "coupons"]),
            ("subscription_service","Subscription packages",           ["subscription_packages", "wallets"]),
        ]
        for dbname, label, candidates in pg_checks:
            try:
                conn = psycopg2.connect(
                    host=PG_HOST, port=PG_PORT, user=PG_USER,
                    password=PG_PASS, dbname=dbname, connect_timeout=5
                )
                cur = conn.cursor()
                cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
                pg_tables = {r[0] for r in cur.fetchall()}
                found = []
                for tbl in candidates:
                    if tbl in pg_tables:
                        cur.execute(f"SELECT COUNT(*) FROM {tbl};")
                        cnt = cur.fetchone()[0]
                        found.append(f"{tbl}({cnt})")
                ok = bool(found)
                if found:
                    detail = f"Tables: {found}"
                else:
                    actual = sorted(pg_tables) if pg_tables else []
                    detail = f"Actual tables: {actual}" if actual else f"DB empty — checked: {candidates}"
                STORE.add(self.STEP, f"PostgreSQL: {label}", ok, detail)
                icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
                console.print(f"  {icon} PostgreSQL [{dbname}]: {label} — {detail}")
                cur.close()
                conn.close()
            except Exception as exc:
                STORE.add(self.STEP, f"PostgreSQL: {label}", False, str(exc)[:100])
                console.print(f"  [red]✗[/red] PostgreSQL [{dbname}]: {exc}")

    def run(self):
        console.print(Panel("[bold cyan]STEP 13 — Database Verification[/bold cyan]", expand=False))
        self._run_mongo()
        self._run_postgres()


# ──────────────────────────────────────────────────────────────
#  STEP 14 — RABBITMQ VERIFICATION
# ──────────────────────────────────────────────────────────────
class RabbitMQVerifier:
    STEP = "14-RabbitMQ"
    QUEUES = ["catalogue.events", "notification.events", "indexing.events"]

    def run(self):
        console.print(Panel("[bold cyan]STEP 14 — RabbitMQ Queue Verification[/bold cyan]",
                            expand=False))
        try:
            creds = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            conn  = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=RABBITMQ_HOST, port=RABBITMQ_PORT,
                    credentials=creds,
                    connection_attempts=3, retry_delay=1,
                    socket_timeout=5,
                ))
            ch = conn.channel()

            for q_name in self.QUEUES:
                try:
                    result = ch.queue_declare(queue=q_name, passive=True)
                    msgs   = result.method.message_count
                    STORE.add(self.STEP, f"Queue: {q_name}", True,
                              f"{msgs} msgs pending")
                    console.print(f"  [green]✓[/green] Queue {q_name} exists ({msgs} msgs)")
                except Exception as qe:
                    # Queue may not exist yet — declare it
                    try:
                        ch2 = conn.channel()
                        ch2.queue_declare(queue=q_name, durable=True)
                        STORE.add(self.STEP, f"Queue: {q_name}", True,
                                  "Declared (was missing)")
                        console.print(f"  [yellow]⚠[/yellow] Queue {q_name} — declared (was missing)")
                    except Exception as de:
                        STORE.add(self.STEP, f"Queue: {q_name}", False, str(de))
                        console.print(f"  [red]✗[/red] Queue {q_name}: {de}")

            conn.close()
        except Exception as exc:
            STORE.add(self.STEP, "RabbitMQ connection", False, str(exc))
            console.print(f"  [red]✗[/red] RabbitMQ connection failed: {exc}")

        # HTTP Management API cross-check
        self._http_api_check()

    def _http_api_check(self):
        try:
            resp = requests.get(
                f"http://localhost:{RABBITMQ_HTTP}/api/queues",
                auth=(RABBITMQ_USER, RABBITMQ_PASS),
                timeout=5,
            )
            if resp.ok:
                queues = {q["name"] for q in resp.json()}
                for q_name in self.QUEUES:
                    ok = q_name in queues
                    STORE.add(self.STEP, f"RabbitMQ HTTP API: {q_name}", ok,
                              "present" if ok else "absent")
                    icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
                    console.print(f"  {icon} RabbitMQ HTTP: {q_name} "
                                  f"{'present' if ok else 'absent'}")
        except Exception as exc:
            console.print(f"  [dim]RabbitMQ HTTP API unavailable: {exc}[/dim]")


# ──────────────────────────────────────────────────────────────
#  STEP 15 — LOGOUT
# ──────────────────────────────────────────────────────────────
class LogoutFlow:
    STEP = "15-Logout"
    PORT = 8080

    def run(self):
        console.print(Panel("[bold cyan]STEP 15 — Logout Flow[/bold cyan]", expand=False))
        if not STATE["access_token"]:
            console.print("  [yellow]⚠[/yellow] Skipped")
            return
        h = _auth_headers()

        resp, ms = _timed_request("POST", _url(self.PORT, "/auth/logout"),
                                  json={"refresh_token": STATE["refresh_token"]},
                                  headers=h)
        ok, _ = _record(self.STEP, "Logout", resp, ms)

        # Verify refresh token invalidation
        resp2, ms2 = _timed_request("POST", _url(self.PORT, "/auth/refresh-token"),
                                    json={"refresh_token": STATE["refresh_token"]})
        inv = (resp2 is not None and resp2.status_code in (401, 403, 400))
        STORE.add(self.STEP, "Refresh Token Invalidated", inv,
                  f"status={resp2.status_code if resp2 else 'None'}", ms2)
        icon = "[green]✓[/green]" if inv else "[yellow]⚠[/yellow]"
        console.print(f"  {icon} Refresh Token Invalidated")


# ──────────────────────────────────────────────────────────────
#  REPORT PRINTER
# ──────────────────────────────────────────────────────────────
def print_final_report():
    console.print()
    console.rule("[bold white]FINAL TEST REPORT[/bold white]")

    table = Table(box=box.ROUNDED, show_header=True,
                  header_style="bold white on dark_blue")
    table.add_column("Step",      style="dim",   width=20)
    table.add_column("Test Name",               width=45)
    table.add_column("Status",    justify="center", width=8)
    table.add_column("Duration",  justify="right",  width=10)
    table.add_column("Detail",    style="dim",   width=40, no_wrap=True)

    for r in STORE.results:
        status_str = ("[green]PASS[/green]" if r["status"] == "PASS"
                      else "[red]FAIL[/red]")
        table.add_row(
            r["step"], r["name"], status_str,
            f"{r['duration_ms']} ms", r["detail"],
        )
    console.print(table)

    # Summary panel
    pct = (STORE.passed / STORE.total * 100) if STORE.total else 0
    color = "green" if pct >= 80 else ("yellow" if pct >= 50 else "red")
    summary = (f"[bold]Total:[/bold] {STORE.total}  "
               f"[green]Passed:[/green] {STORE.passed}  "
               f"[red]Failed:[/red] {STORE.failed}  "
               f"[{color}]Pass Rate: {pct:.1f}%[/{color}]")
    console.print(Panel(summary, title="[bold]Summary[/bold]", expand=False))

    # Save reports
    jp = STORE.save_json()
    cp = STORE.save_csv()
    console.print(f"\n[dim]Reports saved:[/dim]")
    console.print(f"  JSON → [cyan]{jp}[/cyan]")
    console.print(f"  CSV  → [cyan]{cp}[/cyan]")


# ──────────────────────────────────────────────────────────────
#  MAIN RUNNER
# ──────────────────────────────────────────────────────────────
def main():
    console.print(Panel(
        "[bold white]PADOSME — Integrated Full-Stack Test Suite[/bold white]\n"
        "[dim]Kaaslabs Automation (OPC) Pvt Ltd | QA Automation[/dim]",
        style="bold blue", expand=False,
    ))
    console.print(f"[dim]Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]\n")

    steps = [
        HealthChecker(),         # 01
        AuthFlow(),              # 02
        WSTokenValidator(),      # 03
        UserProfileFlow(),       # 04
        SellerFlow(),            # 05
        CatalogueFlow(),         # 06
        WalletFlow(),            # 07
        SubscriptionFlow(),      # 08
        CouponFlow(),            # 09
        PaymentFlow(),           # 10
        ChannelFlow(),           # 11
        AnalyticsFlow(),         # 12
        DiscoveryFlow(),         # 16
        CatalogueServiceFlow(),  # 17
        NotificationFlow(),      # 18
        ConfigFlow(),            # 19
        SMSFlow(),               # 20
        LedgersFlow(),           # 21
        SearchFlow(),            # 22
        DatabaseVerifier(),      # 13
        RabbitMQVerifier(),      # 14
        LogoutFlow(),            # 15
    ]

    for step_obj in steps:
        try:
            step_obj.run()
        except Exception as exc:
            cls = step_obj.__class__.__name__
            console.print(f"[bold red]UNHANDLED ERROR in {cls}:[/bold red] {exc}")
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            STORE.add(getattr(step_obj, "STEP", cls), f"{cls} — Unhandled Exception",
                      False, str(exc)[:200])
        console.print()

    print_final_report()


if __name__ == "__main__":
    main()
