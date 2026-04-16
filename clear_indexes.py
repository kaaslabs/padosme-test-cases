#!/usr/bin/env python3
"""
Padosme — Clear stale geo/search indexes.

Wipes:
  - MongoDB   catalog_db.seller_geo
  - MongoDB   discovery.sellers
  - PostgreSQL padosme_indexing.indexed_sellers
  - PostgreSQL padosme_indexing.indexed_catalog
  - PostgreSQL padosme_indexing.processed_events
  - Redis      all seller:* keys + FT index idx:sellers

Does NOT touch:  catalog_db.catalogs  catalog_db.items  (source data stays)

After running this, re-populate with:
    python3 ~/seed_bangalore_sellers.py --sync
"""
import sys

try:
    import psycopg2
    import pymongo
    import redis
    from rich.console import Console
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Install with: pip install pymongo psycopg2-binary redis rich")
    sys.exit(1)

MONGO_URL  = "mongodb://deploy:kaaslabs123@localhost:27017/"
PG_DSN     = "host=localhost port=5433 user=deploy password=kaaslabs123 dbname=padosme_indexing"
REDIS_HOST = "localhost"
REDIS_PORT = 6379

console = Console()


def clear_mongo():
    console.print("[dim]Connecting to MongoDB…[/dim]")
    client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)

    n1 = client["catalog_db"].seller_geo.count_documents({})
    client["catalog_db"].seller_geo.delete_many({})
    console.print(f"  [green]✓[/green] catalog_db.seller_geo   — deleted {n1} docs")

    n2 = client["discovery"].sellers.count_documents({})
    client["discovery"].sellers.delete_many({})
    console.print(f"  [green]✓[/green] discovery.sellers        — deleted {n2} docs")

    client.close()


def clear_postgres():
    console.print("[dim]Connecting to PostgreSQL…[/dim]")
    conn = psycopg2.connect(PG_DSN)
    cur  = conn.cursor()

    for table in ["indexed_sellers", "indexed_catalog", "processed_events"]:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur.fetchone()[0]
        cur.execute(f"TRUNCATE TABLE {table}")
        console.print(f"  [green]✓[/green] {table:<28} — deleted {n} rows")

    conn.commit()
    cur.close()
    conn.close()


def clear_redis():
    console.print("[dim]Connecting to Redis…[/dim]")
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    r.ping()

    keys = r.execute_command("KEYS", "seller:*")
    if keys:
        r.delete(*keys)
    console.print(f"  [green]✓[/green] seller:* keys             — deleted {len(keys)}")

    try:
        r.execute_command("FT.DROPINDEX", "idx:sellers", "DD")
        console.print("  [green]✓[/green] FT index idx:sellers      — dropped")
    except Exception:
        console.print("  [dim]  FT index idx:sellers      — not present, skipped[/dim]")


def main():
    console.print()
    console.print("[bold yellow]Padosme — Clearing stale indexes[/bold yellow]")
    console.print("[dim](catalogs and items are preserved)[/dim]\n")

    try:
        clear_mongo()
    except Exception as e:
        console.print(f"[red]MongoDB error: {e}[/red]")
        sys.exit(1)

    try:
        clear_postgres()
    except Exception as e:
        console.print(f"[red]PostgreSQL error: {e}[/red]")
        sys.exit(1)

    try:
        clear_redis()
    except Exception as e:
        console.print(f"[yellow]Redis skipped (not running?): {e}[/yellow]")

    console.print()
    console.print("[bold green]Done.[/bold green] Now run:")
    console.print("  [cyan]python3 ~/seed_bangalore_sellers.py --sync[/cyan]")
    console.print()


if __name__ == "__main__":
    main()
