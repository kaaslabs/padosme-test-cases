# Padosme — Service Health Tests

Runs **10 automated checks** across all 20 Padosme microservices and saves results to each service's own database.

---

## Quick Start

```bash
# Install dependencies
pip install requests rich psycopg2-binary pymongo

# Run all tests
python3 service_health_test.py
```

---

## What It Tests

Each service gets 10 checks:

| # | Test | What it checks |
|---|------|----------------|
| 1 | Reachable | Service responds to HTTP requests |
| 2 | HTTP 200 | `/health` returns status code 200 |
| 3 | Valid JSON | Response body is valid JSON |
| 4 | Has Status | Response contains a `status` / `checks` field |
| 5 | Deps OK | All dependencies (DB, cache, queue) are healthy |
| 6 | Latency | Response time is under 500ms |
| 7 | RMQ | RabbitMQ connection is active |
| 8 | DB Data | Service database contains data |
| 9 | Ready EP | `/ready` or `/live` endpoint responds (optional) |
| 10 | Version | Build/version info present in response (optional) |

---

## Services Covered

| Service | Port | Database |
|---------|------|----------|
| SMS Service | 8070 | padosme_notifications |
| Zoho Connect | 8071 | padosme_ledgers |
| Subscription | 8072 | subscription_service |
| Wallet | 8073 | wallet_service |
| Payment | 8074 | payment_service |
| Auth | 8080 | padosme_auth |
| User Profile | 8081 | padosme_profiles |
| Seller | 8082 | padosme_sellers |
| Coupon | 8083 | padosme_coupon |
| Channel | 8084 | channel_service |
| Discovery | 8085 | MongoDB: discovery |
| Catalogue | 8086 | MongoDB: catalog_db |
| Rating | 8087 | rating_db |
| Analytics | 8088 | analytics_db |
| Indexing | 8089 | padosme_indexing |
| Search API | 8090 | MongoDB: discovery |
| Config | 8094 | padosme_config |
| Notification | 8095 | padosme_notifications |
| Location | 8096 | padosme_location |
| Dictionary | 8097 | padosme_dictionary |

**Total: 20 services × 10 tests = 200 test cases**

---

## Sample Output

```
Padosme — Service Health Test

╭──────────────────────┬──────┬──────────┬──────────┬───────────┬────────────┬─────────┬─────────┬─────┬─────────┬──────────┬─────────╮
│ Service              │ Port │ Reachable│ HTTP 200 │ Valid JSON│ Has Status │ Deps OK │ Latency │ RMQ │ DB Data │ Ready EP │ Version │
├──────────────────────┼──────┼──────────┼──────────┼───────────┼────────────┼─────────┼─────────┼─────┼─────────┼──────────┼─────────┤
│ Auth Service         │ 8080 │    ✓     │    ✓     │     ✓     │     ✓      │    ✓    │    ✓    │  ✓  │    ✓    │    ✓     │    ✓    │
│ Discovery Service    │ 8085 │    ✓     │    ✓     │     ✓     │     ✓      │    ✓    │    ✓    │  ✓  │    ✓    │    ✓     │    ✓    │
│ ...                  │  ... │   ...    │   ...    │    ...    │    ...     │   ...   │   ...   │ ... │   ...   │   ...    │   ...   │
╰──────────────────────┴──────┴──────────┴──────────┴───────────┴────────────┴─────────┴─────────┴─────┴─────────┴──────────┴─────────╯

Results:  200 passed  0 failed  0 skipped (not running)
Results written to each service's own database → table: service_health_tests
```

---

## Where Results Are Stored

After every run, results are saved to a `service_health_tests` table inside each service's own database.

```sql
-- Example: check Auth Service test history
SELECT run_at, test_name, passed, detail
FROM service_health_tests
ORDER BY run_at DESC
LIMIT 10;
```

---

## Requirements

- Python 3.8+
- Services running on `localhost` (or the production server)
- PostgreSQL and MongoDB accessible on default ports
