# Padosme — Test Cases & Seed Tools

Scripts for seeding, searching, and health-testing the Padosme platform.

---

## Files

| File | Purpose |
|------|---------|
| `seed_bangalore_sellers.py` | Seed real Bangalore sellers + interactive search shell |
| `service_health_test.py` | Automated health checks across all 20 microservices |
| `check_db.py` | View PostgreSQL and MongoDB seller data in table form |

---

## seed_bangalore_sellers.py

Seeds **170 verified real shops** across Bangalore into Redis, MongoDB, and PostgreSQL via the Padosme event pipeline, and provides an interactive search shell to query them.

### Install

```bash
pip install requests rich pika PyJWT redis pymongo psycopg2-binary
```

### Seed

```bash
# Seed all 170 real sellers (publishes seller.verified events)
python3 seed_bangalore_sellers.py --count 0 --skip-catalogue

# Seed sellers + create catalogues
python3 seed_bangalore_sellers.py --count 0
```

### Search (interactive shell)

```bash
python3 seed_bangalore_sellers.py --search
```

At the `search>` prompt you can:

```
koramangala          → switch location to Koramangala, show nearby shops
mango                → search sellers + catalogue items for "mango"
indiranagar          → switch to Indiranagar
:product biryani     → catalogue-only item search
:radius 5            → set radius to 5 km
:sort rating         → sort by rating
:category Food       → filter by category
:available           → only open shops
:reset               → change your location
:quit                → exit
```

**Default radius: 15 km.** Auto-expands to 25 → 50 → 100 km if nothing is found nearby.

### Areas supported

282 Bangalore localities are recognised by name — just type the area at the `search>` prompt:

> MG Road, Brigade Road, Shivajinagar, Richmond Town, Malleswaram, Rajajinagar,
> Vijayanagar, Jayanagar, JP Nagar, BTM Layout, Koramangala, HSR Layout,
> Indiranagar, Whitefield, Marathahalli, Hebbal, Yelahanka, Thanisandra,
> Hennur, RT Nagar, Rahamath Nagar, Sahakar Nagar, Kammanahalli, Nagawara,
> Sanjay Nagar, Ganganagar, Rajankunte and 250+ more localities.

### Real shops included

| Area | Examples |
|------|---------|
| MG Road / Central | Koshy's, MTR 1924, The Only Place, Apsara Ice Creams, Nilgiris |
| Malleswaram | CTR, Veena Stores, Janata Hotel, Brahmin's Coffee Bar |
| Koramangala | Toit Brewpub, Meghana Foods, Truffles, Decathlon |
| Indiranagar | B-Flat, The Bier Library, Hole in the Wall Cafe |
| Whitefield | Windmills Craftworks, Forum Mall, Decathlon |
| Jayanagar | Vidyarthi Bhavan, Pai Viceroy, Big Bazaar |
| RT Nagar | Empire Restaurant, J B Bakery, Kudla, Rahhams |
| Rahamath Nagar | Sichi Hotel, Al Noor Bakery, Rahmath Chicken Centre |
| Kammanahalli | Al Amanah Cafe, Hotel Anugraha, Bhavani Bakery |
| Hebbal | Hotel Dakshin, Columbia Asia Hospital, Om Sai Medical |
| Rajankunte | Shree Sagar Darshini, Bismillah Hotel, Life Care Pharmacy |
| + more | 170 real shops across all areas |

---

## service_health_test.py

Runs **10 automated checks** across all 20 Padosme microservices.

```bash
pip install requests rich psycopg2-binary pymongo
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

### Services covered

| Service | Port |
|---------|------|
| Auth | 8080 |
| User Profile | 8081 |
| Seller | 8082 |
| Coupon | 8083 |
| Discovery | 8085 |
| Catalogue | 8086 |
| Rating | 8087 |
| Analytics | 8088 |
| Indexing | 8089 |
| + 11 more | — |

**Total: 20 services × 10 tests = 200 test cases**

Results are saved to a `service_health_tests` table in each service's own database after every run.

---

## Requirements

- Python 3.10+
- All Padosme services running (locally or on the server)
- Redis, MongoDB, PostgreSQL, RabbitMQ accessible
