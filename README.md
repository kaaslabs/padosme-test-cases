# Padosme — Test Cases & Seed Tools

Scripts for seeding, health-testing, and managing the Padosme platform data pipeline.

---

## Files

| File | Purpose |
|------|---------|
| `seed_bangalore_sellers.py` | Google Maps seller discovery importer + seeding status report |
| `seed_databases.py` | Full database seeder for PostgreSQL + MongoDB integration test data |
| `full_stack_test.py` | End-to-end full-stack integration test suite |
| `service_health_test.py` | Automated health checks across all Padosme microservices |
| `check_db.py` | View PostgreSQL and MongoDB seller data in table form |
| `clear_indexes.py` | Wipe stale geo/search indexes from MongoDB, PostgreSQL, Redis |

---

## Install

```bash
pip install requests rich pika PyJWT redis pymongo psycopg2-binary
```

---

## seed_bangalore_sellers.py

Production-grade Google Maps seller discovery importer.

Fetches **real businesses** from Google Places API across Bangalore metropolitan region
and stores them as `DISCOVERED` sellers ready for platform onboarding.

**Data policy:** Uses ONLY Google Places API — zero synthetic data.
Stops safely on quota exhaustion with no fallback to fake data.

### Check seeding status

```bash
python3 seed_bangalore_sellers.py --status
```

Prints a full report across MongoDB, Redis, and PostgreSQL in one command.

### Run discovery import

```bash
python3 seed_bangalore_sellers.py \
    --google-api-key YOUR_KEY \
    --radius 45 \
    --count 3000 \
    --grid-size 10 \
    --threads 8 \
    --batch-size 100 \
    --use-db-mode \
    --include-phone \
    --include-ratings \
    --verbose
```

Or set the key as an environment variable:

```bash
export GOOGLE_API_KEY=YOUR_KEY
python3 seed_bangalore_sellers.py --use-db-mode --verbose
```

### CLI arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--google-api-key` | `$GOOGLE_API_KEY` | Google Places API key |
| `--radius` | `45` | Scan radius in km from Bangalore centre |
| `--grid-size` | `10` | Divide coverage into N×N grid cells |
| `--category` | all | Limit to one type: `restaurant`, `pharmacy`, `grocery_or_supermarket`, `electronics_store`, `bakery`, `clothing_store`, `salon`, `hospital`, `hardware_store`, `mobile_store` |
| `--count` | `3000` | Stop after inserting this many sellers |
| `--threads` | `8` | Parallel scanner threads |
| `--batch-size` | `100` | DB / API insert batch size |
| `--use-db-mode` | — | Write directly to MongoDB `discovery.sellers` |
| `--use-api-mode` | — | POST to catalogue-service REST API |
| `--mongo-url` | `mongodb://localhost:27017/catalogue_db` | MongoDB connection URL |
| `--api-url` | `http://localhost:8085` | Catalogue-service base URL |
| `--include-phone` | — | Store `phone_number` from Google |
| `--include-ratings` | — | Store `rating` and `user_ratings_total` |
| `--verbose` | — | Print per-cell scan results |
| `--status` | — | Show seeding status report then exit |

### Seller document stored

```json
{
  "seller_id": "uuid4",
  "place_id": "ChIJ...",
  "name": "Meghana Foods",
  "primary_category": "restaurant",
  "categories": ["restaurant", "food", "point_of_interest"],
  "latitude": 12.9716,
  "longitude": 77.5946,
  "address": "MG Road, Bengaluru, Karnataka",
  "city": "Bangalore",
  "location": { "type": "Point", "coordinates": [77.5946, 12.9716] },
  "location_es": { "lat": 12.9716, "lon": 77.5946 },
  "rating": 4.3,
  "user_ratings_total": 2847,
  "phone_number": "+91 98765 43210",
  "website": "https://...",
  "opening_hours": { "open_now": true, "weekday_text": ["Monday: 11 AM – 11 PM", "..."] },
  "business_status": "OPERATIONAL",
  "google_maps_url": "https://maps.google.com/?cid=...",
  "seller_status": "DISCOVERED",
  "invitation_sent": false,
  "verified": false,
  "onboarded": false,
  "discovered_at": "2026-04-16T10:30:00+00:00"
}
```

### Coverage

Grid-based scan covering the full Bangalore metropolitan region:

| Area | Included |
|------|----------|
| Central | MG Road, Shivajinagar, Commercial Street |
| South | Koramangala, HSR Layout, BTM Layout, JP Nagar, Jayanagar, Electronic City |
| East | Indiranagar, Whitefield, Marathahalli, Bellandur |
| North | Hebbal, Yelahanka, Thanisandra, Nagawara, Hennur |
| Northwest | Rajajinagar, Malleshwaram, Yeshwanthpur |
| Airport corridor | Devanahalli, Kempegowda International Airport |
| West | Kengeri, Vijayanagar |

---

## seed_databases.py

Seeds all PostgreSQL databases and MongoDB with baseline test data
used by the `full_stack_test.py` integration suite.

```bash
python3 seed_databases.py

# Drop and recreate all data
python3 seed_databases.py --reset
```

---

## full_stack_test.py

End-to-end integration test suite for the Padosme platform.

```bash
python3 full_stack_test.py
```

Runs automated tests across the full service stack and reports results.

---

## service_health_test.py

Runs automated health checks across all Padosme microservices.

```bash
python3 service_health_test.py
```

| # | Test | What it checks |
|---|------|----------------|
| 1 | Reachable | Service responds to HTTP |
| 2 | HTTP 200 | `/health` returns 200 |
| 3 | Valid JSON | Response body is valid JSON |
| 4 | Has Status | Response has `status` / `checks` field |
| 5 | Deps OK | DB, cache, queue are healthy |
| 6 | Latency | Response under 500ms |
| 7 | RMQ | RabbitMQ connection active |
| 8 | DB Data | Database contains data |
| 9 | Ready EP | `/ready` or `/live` responds |
| 10 | Version | Build/version info present |

Services covered: Auth (8080), User Profile (8081), Seller (8082), Coupon (8083),
Discovery (8085), Catalogue (8086), Rating (8087), Analytics (8088), Indexing (8089) + more.

Results are saved to a `service_health_tests` table in each service's database after every run.

---

## check_db.py

View seller data across stores in a formatted table.

```bash
python3 check_db.py
```

---

## clear_indexes.py

Wipe all stale geo/search indexes. Use before a full re-seed.

Clears:
- MongoDB `catalog_db.seller_geo`
- MongoDB `discovery.sellers`
- PostgreSQL `padosme_indexing.indexed_sellers`
- PostgreSQL `padosme_indexing.indexed_catalog`
- PostgreSQL `padosme_indexing.processed_events`

```bash
python3 clear_indexes.py
```

---

## Requirements

- Python 3.10+
- All Padosme services running (locally or on the server)
- Redis, MongoDB, PostgreSQL, RabbitMQ accessible on `localhost`
