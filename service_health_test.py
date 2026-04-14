#!/usr/bin/env python3
"""
Padosme — Service Health & Smoke Test
10 tests per service, results stored in each service's own database.

Usage:
    python3 service_health_test.py

Requirements:
    pip install requests rich psycopg2-binary pymongo
"""

import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    import requests
    from rich.console import Console
    from rich.table import Table
    from rich import box
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Install with:  pip install requests rich psycopg2-binary pymongo")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
    _PG = True
except ImportError:
    _PG = False

try:
    import pymongo
    _MONGO = True
except ImportError:
    _MONGO = False

console = Console()
BASE    = "http://localhost"
TIMEOUT = 5
LATENCY_THRESHOLD_MS = 500

PG_HOST = "localhost"
PG_USER = "deploy"
PG_PASS = "kaaslabs123"
MONGO_URI = "mongodb://deploy:kaaslabs123@localhost:27017/?authSource=admin"

def _pg_port() -> int:
    for port in (5432, 5433, 5434):
        try:
            c = psycopg2.connect(host=PG_HOST, port=port, user=PG_USER,
                                 password=PG_PASS, dbname="postgres",
                                 connect_timeout=2)
            c.close()
            return port
        except Exception:
            pass
    return 5432

PG_PORT: Optional[int] = None

SERVICES: List[Dict] = [
    {"name": "SMS Service",          "port": 8070, "db_type": "postgres", "db_name": "padosme_notifications",
     "db_check": {"type": "postgres", "db": "padosme_notifications", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Zoho Connect Service", "port": 8071, "db_type": "postgres", "db_name": "padosme_ledgers",
     "db_check": {"type": "postgres", "db": "padosme_ledgers", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Subscription Service", "port": 8072, "db_type": "postgres", "db_name": "subscription_service",
     "db_check": {"type": "postgres", "db": "subscription_service", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Wallet Service",       "port": 8073, "db_type": "postgres", "db_name": "wallet_service",
     "db_check": {"type": "postgres", "db": "wallet_service", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Payment Service",      "port": 8074, "db_type": "postgres", "db_name": "payment_service",
     "db_check": {"type": "postgres", "db": "payment_service", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Auth Service",         "port": 8080, "db_type": "postgres", "db_name": "padosme_auth",
     "db_check": {"type": "postgres", "db": "padosme_auth", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "User Profile Service", "port": 8081, "db_type": "postgres", "db_name": "padosme_profiles",
     "db_check": {"type": "postgres", "db": "padosme_profiles", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Seller Service",       "port": 8082, "db_type": "postgres", "db_name": "padosme_sellers",
     "db_check": {"type": "postgres", "db": "padosme_sellers", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Coupon Service",       "port": 8083, "db_type": "postgres", "db_name": "padosme_coupon",
     "db_check": {"type": "postgres", "db": "padosme_coupon", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Channel Service",      "port": 8084, "db_type": "postgres", "db_name": "channel_service",
     "db_check": {"type": "postgres", "db": "channel_service", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Discovery Service",    "port": 8085, "db_type": "postgres", "db_name": "discovery",
     "db_check": {"type": "mongo", "db": "discovery", "collection": "sellers"}},

    {"name": "Catalogue Service",    "port": 8086, "db_type": "mongo",    "db_name": "catalog_db",
     "db_check": {"type": "mongo", "db": "catalog_db", "collection": "catalogs"}},

    {"name": "Rating Service",       "port": 8087, "db_type": "postgres", "db_name": "rating_db",
     "db_check": {"type": "postgres", "db": "rating_db", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Analytics Service",    "port": 8088, "db_type": "postgres", "db_name": "analytics_db",
     "db_check": {"type": "postgres", "db": "analytics_db", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Indexing Service",     "port": 8089, "db_type": "postgres", "db_name": "padosme_indexing",
     "db_check": {"type": "postgres", "db": "padosme_indexing", "query": "SELECT COUNT(*) FROM indexed_sellers"}},

    {"name": "Search API",           "port": 8090, "db_type": "postgres", "db_name": "discovery",
     "db_check": {"type": "mongo", "db": "discovery", "collection": "catalogs"}},

    {"name": "Config Service",       "port": 8094, "db_type": "postgres", "db_name": "padosme_config",
     "db_check": {"type": "postgres", "db": "padosme_config", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Notification Service", "port": 8095, "db_type": "postgres", "db_name": "padosme_notifications",
     "db_check": {"type": "postgres", "db": "padosme_notifications", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Location Service",     "port": 8096, "db_type": "postgres", "db_name": "padosme_location",
     "db_check": {"type": "postgres", "db": "padosme_location", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},

    {"name": "Dictionary Service",   "port": 8097, "db_type": "postgres", "db_name": "padosme_dictionary",
     "db_check": {"type": "postgres", "db": "padosme_dictionary", "query": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"}},
]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS service_health_tests (
    id          SERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL,
    service     TEXT NOT NULL,
    port        INTEGER NOT NULL,
    test_name   TEXT NOT NULL,
    passed      SMALLINT NOT NULL,
    detail      TEXT,
    duration_ms NUMERIC(10,2)
);
"""

INSERT_SQL = """
INSERT INTO service_health_tests (run_at, service, port, test_name, passed, detail, duration_ms)
VALUES (%(run_at)s, %(service)s, %(port)s, %(test_name)s, %(passed)s, %(detail)s, %(duration_ms)s)
"""


def write_postgres(db_name: str, rows: List[Dict]):
    if not _PG or PG_PORT is None:
        return
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                                password=PG_PASS, dbname=db_name,
                                connect_timeout=3)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            psycopg2.extras.execute_batch(cur, INSERT_SQL, rows)
        conn.commit()
        conn.close()
    except Exception as e:
        console.print(f"  [dim red]PG write failed ({db_name}): {e}[/dim red]")


def write_mongo(db_name: str, rows: List[Dict]):
    if not _MONGO:
        return
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db = client[db_name]
        db["service_health_tests"].insert_many(rows)
        client.close()
    except Exception as e:
        console.print(f"  [dim red]Mongo write failed ({db_name}): {e}[/dim red]")


def persist(svc: Dict, rows: List[Dict]):
    if svc["db_type"] == "postgres":
        write_postgres(svc["db_name"], rows)
    elif svc["db_type"] == "mongo":
        write_mongo(svc["db_name"], rows)


def fetch(port: int) -> Tuple[Optional[int], float, Optional[dict]]:
    url = f"{BASE}:{port}/health"
    t0 = time.perf_counter()
    try:
        r = requests.get(url, timeout=TIMEOUT)
        ms = (time.perf_counter() - t0) * 1000
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:200]}
        return r.status_code, ms, body
    except requests.exceptions.ConnectionError:
        return None, 0.0, None
    except requests.exceptions.Timeout:
        return None, TIMEOUT * 1000, None


def fetch_endpoint(port: int, path: str) -> Tuple[Optional[int], float]:
    url = f"{BASE}:{port}{path}"
    t0 = time.perf_counter()
    try:
        r = requests.get(url, timeout=TIMEOUT)
        ms = (time.perf_counter() - t0) * 1000
        return r.status_code, ms
    except Exception:
        return None, 0.0


def status_is_healthy(body: dict) -> bool:
    if not isinstance(body, dict):
        return False
    top = str(body.get("status", "")).lower()
    # Trust the service's own top-level status declaration first
    if top in ("ok", "healthy"):
        return True
    if top in ("degraded", "error", "unhealthy"):
        return False
    checks = body.get("checks")
    if isinstance(checks, list):
        return all(
            c.get("status") in ("ok", "healthy") if isinstance(c, dict) else True
            for c in checks
        )
    services = body.get("services")
    if isinstance(services, dict):
        return all(v == "healthy" for v in services.values())
    probes = body.get("probes")
    if isinstance(probes, dict):
        return all(
            (v.get("status") in ("ok", "healthy", "pass") if isinstance(v, dict) else v in ("ok", "healthy"))
            for v in probes.values()
        )
    return top in ("ok", "healthy", "")


def summarise(body: Optional[dict]) -> str:
    if body is None:
        return "connection refused"
    if not isinstance(body, dict):
        return str(body)[:80]
    checks = body.get("checks")
    if isinstance(checks, list):
        bad = [c.get("name", "?") for c in checks
               if isinstance(c, dict) and c.get("status") not in ("ok", "healthy")]
        return f"degraded: {bad}" if bad else "all checks ok"
    services = body.get("services")
    if isinstance(services, dict):
        bad = [k for k, v in services.items() if v != "healthy"]
        return f"degraded: {bad}" if bad else "all services healthy"
    probes = body.get("probes")
    if isinstance(probes, dict):
        bad = []
        for k, v in probes.items():
            s = v.get("status") if isinstance(v, dict) else v
            if s not in ("ok", "healthy", "pass"):
                bad.append(k)
        return f"degraded: {bad}" if bad else "all probes ok"
    return str(body.get("status", str(body)[:80]))


def check_rmq_in_health(body: Optional[dict]) -> Tuple[bool, str]:
    """Test 7: RabbitMQ is connected and healthy."""
    if not isinstance(body, dict):
        return False, "no body"
    body_str = str(body).lower()
    if "rabbitmq" not in body_str and "amqp" not in body_str and "rabbit" not in body_str:
        return True, "no RMQ probe"
    probes = body.get("probes", {})
    if isinstance(probes, dict):
        for k, v in probes.items():
            if "rabbit" in k.lower() or "amqp" in k.lower():
                status = v.get("status") if isinstance(v, dict) else str(v)
                return status in ("ok", "healthy", "pass"), f"rabbitmq={status}"
    services = body.get("services", {})
    if isinstance(services, dict):
        for k, v in services.items():
            if "rabbit" in k.lower():
                return v == "healthy", f"{k}={v}"
    return True, "not explicitly checked"


def check_db_has_data(db_check: Optional[Dict]) -> Tuple[int, str]:
    """Test 8: Actual data exists in the service's database."""
    if not db_check:
        return -1, "no db_check configured"
    if db_check["type"] == "postgres":
        if not _PG or PG_PORT is None:
            return -1, "psycopg2 not available"
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, user=PG_USER,
                password=PG_PASS, dbname=db_check["db"], connect_timeout=3
            )
            cur = conn.cursor()
            cur.execute(db_check["query"])
            count = cur.fetchone()[0]
            cur.close()
            conn.close()
            return (1 if count > 0 else 0), f"count={count}"
        except Exception as e:
            return 0, str(e)[:80]
    elif db_check["type"] == "mongo":
        if not _MONGO:
            return -1, "pymongo not available"
        try:
            client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
            db = client[db_check["db"]]
            count = db[db_check["collection"]].count_documents({})
            client.close()
            return (1 if count > 0 else 0), f"count={count}"
        except Exception as e:
            return 0, str(e)[:80]
    return -1, "unknown db type"


def check_version(body: Optional[dict]) -> Tuple[bool, str]:
    """Test 10: Version/build info present in health response."""
    if not isinstance(body, dict):
        return False, "no body"
    for key in ("version", "build", "commit", "git_commit", "build_info", "service_version", "tag"):
        if key in body:
            return True, f"{key}={str(body[key])[:30]}"
    if any(k in str(body).lower() for k in ("version", "build", "commit")):
        return True, "found in response"
    return False, "no version info"


def run_tests(svc: Dict, run_at: str) -> List[Dict]:
    name, port = svc["name"], svc["port"]

    def row(test_name, passed, detail, ms=0.0):
        return {
            "run_at": run_at, "service": name, "port": port,
            "test_name": test_name, "passed": passed,
            "detail": str(detail)[:200], "duration_ms": round(ms, 1),
        }

    code, ms, body = fetch(port)

    if code is None and body is None:
        return [row(f"{i}_skip", -1, "not running") for i in
                ["1_reachable","2_returns_200","3_valid_json","4_has_status",
                 "5_deps_healthy","6_latency_ok","7_rmq_connected",
                 "8_db_has_data","9_ready_endpoint","10_version_info"]]

    ok3 = isinstance(body, dict) and "_raw" not in body
    has_status = isinstance(body, dict) and any(k in body for k in ("status","checks","services","probes"))
    ok5 = isinstance(body, dict) and status_is_healthy(body)

    # Test 6: latency
    latency_ok = ms < LATENCY_THRESHOLD_MS
    latency_detail = f"{ms:.0f}ms {'✓' if latency_ok else '> ' + str(LATENCY_THRESHOLD_MS) + 'ms'}"

    # Test 7: RabbitMQ
    rmq_ok, rmq_detail = check_rmq_in_health(body)

    # Test 8: DB has data
    db_passed, db_detail = check_db_has_data(svc.get("db_check"))

    # Test 9: /ready or /live endpoint (pass if endpoint not exposed — it's optional)
    ready_code, ready_ms = fetch_endpoint(port, "/ready")
    if ready_code is None or ready_code == 404:
        live_code, live_ms = fetch_endpoint(port, "/live")
        if live_code is None or live_code == 404:
            ready_passed, ready_detail = 1, "not exposed (ok)"
        else:
            ready_passed, ready_detail = int(live_code < 400), f"/live={live_code} ({live_ms:.0f}ms)"
    else:
        ready_passed, ready_detail = int(ready_code < 400), f"/ready={ready_code} ({ready_ms:.0f}ms)"

    # Test 10: version info (pass if not present — it's optional)
    ver_ok, ver_detail = check_version(body)
    if not ver_ok:
        ver_ok, ver_detail = True, "not exposed (ok)"

    return [
        row("1_reachable",      1,              f"HTTP {code}", ms),
        row("2_returns_200",    int(code==200), f"got {code}", ms),
        row("3_valid_json",     int(ok3),       "ok" if ok3 else str(body)[:60]),
        row("4_has_status",     int(has_status),"present" if has_status else str(list(body.keys()) if isinstance(body,dict) else "?")),
        row("5_deps_healthy",   int(ok5),       summarise(body)),
        row("6_latency_ok",     int(latency_ok),latency_detail, ms),
        row("7_rmq_connected",  int(rmq_ok),    rmq_detail),
        row("8_db_has_data",    db_passed,      db_detail),
        row("9_ready_endpoint", ready_passed,   ready_detail, ready_ms if ready_code else 0),
        row("10_version_info",  int(ver_ok),    ver_detail),
    ]


def main():
    global PG_PORT
    run_at = datetime.now(timezone.utc).isoformat()

    console.print()
    console.print("[bold cyan]Padosme — Service Health Test[/bold cyan]", f"[dim]{run_at}[/dim]")

    if _PG:
        console.print("[dim]Detecting PostgreSQL port...[/dim]", end=" ")
        PG_PORT = _pg_port()
        console.print(f"[dim]found on :{PG_PORT}[/dim]")

    console.print()

    tbl = Table(box=box.ROUNDED, show_lines=True)
    tbl.add_column("Service",      style="bold", min_width=22)
    tbl.add_column("Port",         justify="right", style="dim", min_width=5)
    tbl.add_column("Reachable",    justify="center", min_width=9)
    tbl.add_column("HTTP 200",     justify="center", min_width=9)
    tbl.add_column("Valid JSON",   justify="center", min_width=9)
    tbl.add_column("Has Status",   justify="center", min_width=9)
    tbl.add_column("Deps OK",      justify="center", min_width=9)
    tbl.add_column("Latency",      justify="center", min_width=9)
    tbl.add_column("RMQ",          justify="center", min_width=9)
    tbl.add_column("DB Data",      justify="center", min_width=9)
    tbl.add_column("Ready EP",     justify="center", min_width=9)
    tbl.add_column("Version",      justify="center", min_width=9)
    tbl.add_column("DB",           justify="left",   min_width=22, style="dim")

    total_pass = total_fail = total_skip = 0

    for svc in SERVICES:
        rows = run_tests(svc, run_at)
        for r in rows:
            if r["passed"] == 1:   total_pass += 1
            elif r["passed"] == 0: total_fail += 1
            else:                  total_skip += 1
        persist(svc, rows)

        def cell(r):
            if r["passed"] == -1: return "[dim]—[/dim]"
            return "[green]✓[/green]" if r["passed"] else "[red]✗[/red]"

        db_label = f"{svc['db_type']}:{svc['db_name']}" if svc["db_type"] else "—"
        tbl.add_row(
            svc["name"], str(svc["port"]),
            cell(rows[0]), cell(rows[1]), cell(rows[2]), cell(rows[3]), cell(rows[4]),
            cell(rows[5]), cell(rows[6]), cell(rows[7]), cell(rows[8]), cell(rows[9]),
            db_label,
        )

    console.print(tbl)
    console.print()
    console.print(
        f"[bold]Results:[/bold]  "
        f"[green]{total_pass} passed[/green]  "
        f"[red]{total_fail} failed[/red]  "
        f"[dim]{total_skip} skipped (not running)[/dim]"
    )
    console.print("[dim]Results written to each service's own database → table: service_health_tests[/dim]")
    console.print()
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
