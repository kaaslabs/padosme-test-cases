#!/usr/bin/env python3
"""
Padosme — Publish seller.verified Events
=========================================
Reads all sellers from MongoDB catalog_db.seller_geo and publishes
a seller.verified RabbitMQ event for each one.

The indexing-service consumes these events and geo-indexes each seller
into Redis — making them discoverable via the discovery-service.

Use this when:
  - Redis geo index is missing sellers that exist in MongoDB
  - After a fresh environment setup
  - After clearing indexes and re-seeding

Usage:
    python3 publish_seller_events.py
    python3 publish_seller_events.py --mongo-url mongodb://deploy:kaaslabs123@localhost:27017/
    python3 publish_seller_events.py --rabbit-url amqp://deploy:kaaslabs123@localhost:5672/

Requirements:
    pip install pymongo pika
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    import pika
    import pymongo
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nInstall: pip install pymongo pika")


EXCHANGE    = "padosme.events"
ROUTING_KEY = "seller.verified"


def publish_events(mongo_url: str, rabbit_url: str, batch_print: int) -> None:

    # ── Load sellers from MongoDB ─────────────────────────────────────────────
    print("Connecting to MongoDB...")
    try:
        client  = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        sellers = list(client["catalog_db"].seller_geo.find(
            {},
            {"_id": 0, "seller_id": 1, "name": 1, "address": 1,
             "lat": 1, "lon": 1, "category": 1, "subscription_tier": 1,
             "rating": 1, "review_count": 1, "h3_cell": 1}
        ))
        client.close()
    except Exception as e:
        sys.exit(f"MongoDB error: {e}")

    print(f"Loaded {len(sellers)} sellers from catalog_db.seller_geo")

    if not sellers:
        print("Nothing to publish.")
        return

    # ── Connect to RabbitMQ ───────────────────────────────────────────────────
    print("Connecting to RabbitMQ...")
    try:
        conn = pika.BlockingConnection(pika.URLParameters(rabbit_url))
        ch   = conn.channel()
        ch.exchange_declare(EXCHANGE, exchange_type="topic", durable=True, passive=True)
    except Exception as e:
        sys.exit(f"RabbitMQ error: {e}")

    print(f"Publishing seller.verified events to exchange '{EXCHANGE}'...")

    props = pika.BasicProperties(
        content_type  = "application/json",
        delivery_mode = 2,   # persistent
    )
    now       = datetime.now(timezone.utc).isoformat()
    published = 0
    skipped   = 0

    for s in sellers:
        if not s.get("seller_id") or not s.get("lat") or not s.get("lon"):
            skipped += 1
            continue

        event = {
            "event_id":          str(uuid.uuid4()),
            "event_type":        "seller.verified",
            "timestamp":         now,
            "seller_id":         s["seller_id"],
            "name":              s.get("name", ""),
            "address":           s.get("address", "Bangalore"),
            "latitude":          float(s["lat"]),
            "longitude":         float(s["lon"]),
            "h3_cell":           s.get("h3_cell", ""),
            "rating":            float(s.get("rating") or 0),
            "review_count":      int(s.get("review_count") or 0),
            "subscription_tier": s.get("subscription_tier", "free"),
            "available":         True,
            "status":            "active",
            "categories":        [s["category"]] if s.get("category") else [],
            "products":          [],
        }

        ch.basic_publish(
            exchange    = EXCHANGE,
            routing_key = ROUTING_KEY,
            body        = json.dumps(event),
            properties  = props,
        )
        published += 1

        if published % batch_print == 0:
            print(f"  Published {published} seller.verified events...")
            time.sleep(0.3)   # small pause so indexing-service can keep up

    conn.close()

    print(f"\nDone.")
    print(f"  Published : {published}")
    print(f"  Skipped   : {skipped} (missing seller_id or coordinates)")
    print(f"\nThe indexing-service will now process these events and update Redis.")
    print("Run --status to verify once indexing completes (usually within 30s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish seller.verified events to RabbitMQ for all sellers in MongoDB"
    )
    parser.add_argument(
        "--mongo-url",
        default="mongodb://deploy:kaaslabs123@localhost:27017/",
        help="MongoDB connection URL (default: mongodb://deploy:kaaslabs123@localhost:27017/)",
    )
    parser.add_argument(
        "--rabbit-url",
        default="amqp://deploy:kaaslabs123@localhost:5672/",
        help="RabbitMQ connection URL (default: amqp://deploy:kaaslabs123@localhost:5672/)",
    )
    parser.add_argument(
        "--batch-print",
        type=int,
        default=500,
        help="Print progress every N events (default: 500)",
    )
    args = parser.parse_args()

    try:
        publish_events(args.mongo_url, args.rabbit_url, args.batch_print)
    except KeyboardInterrupt:
        print("\nInterrupted. Events published so far have been delivered.")
        sys.exit(130)


if __name__ == "__main__":
    main()
