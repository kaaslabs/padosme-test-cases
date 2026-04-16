#!/usr/bin/env python3
"""
Padosme — Database State Checker
Shows counts and health across MongoDB, PostgreSQL, and Redis.
"""
import sys

try:
    import psycopg2
    import pymongo
    import redis
    from rich.console import Console
    from rich.table import Table
    from rich import box
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Install with: pip install pymongo psycopg2-binary redis rich")
    sys.exit(1)

MONGO_URL  = "mongodb://deploy:kaaslabs123@localhost:27017/"
PG_DSN     = "host=localhost port=5433 user=deploy password=kaaslabs123 dbname=padosme_indexing"
REDIS_HOST = "localhost"
REDIS_PORT = 6379

console = Console()


def check_mongo():
    rows = []
    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=3000)
        client.admin.command("ping")

        for db_name, collections in [
            ("catalog_db", ["sellers", "catalogs", "items", "seller_geo"]),
            ("discovery",  ["sellers"]),
        ]:
            db = client[db_name]
            for col in collections:
                try:
                    count = db[col].count_documents({})
                    rows.append((f"MongoDB/{db_name}", col, str(count), "[green]ok[/green]"))
                except Exception as e:
                    rows.append((f"MongoDB/{db_name}", col, "?", f"[red]{e}[/red]"))
        client.close()
    except Exception as e:
        rows.append(("MongoDB", "—", "—", f"[red]unreachable: {e}[/red]"))
    return rows


def check_postgres():
    rows = []
    try:
        conn = psycopg2.connect(PG_DSN)
        cur  = conn.cursor()
        for table in ["indexed_sellers", "indexed_catalog", "processed_events", "index_jobs"]:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                rows.append(("PostgreSQL/padosme_indexing", table, str(count), "[green]ok[/green]"))
            except Exception as e:
                rows.append(("PostgreSQL/padosme_indexing", table, "?", f"[red]{e}[/red]"))
        cur.close()
        conn.close()
    except Exception as e:
        rows.append(("PostgreSQL", "—", "—", f"[red]unreachable: {e}[/red]"))
    return rows


def check_redis():
    rows = []
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        r.ping()

        seller_keys = r.execute_command("KEYS", "seller:*")
        rows.append(("Redis", "seller:* keys", str(len(seller_keys)), "[green]ok[/green]"))

        try:
            info = r.execute_command("FT.INFO", "idx:sellers")
            info_dict = dict(zip(info[::2], info[1::2]))
            num_docs = info_dict.get("num_docs", "?")
            rows.append(("Redis", "FT index idx:sellers", str(num_docs), "[green]ok[/green]"))
        except Exception:
            rows.append(("Redis", "FT index idx:sellers", "0", "[yellow]not created[/yellow]"))

    except Exception as e:
        rows.append(("Redis", "—", "—", f"[red]unreachable: {e}[/red]"))
    return rows


def main():
    console.print()
    console.print("[bold cyan]Padosme — Database State[/bold cyan]\n")

    table = Table(box=box.ROUNDED, show_header=True)
    table.add_column("Store",      style="dim",   min_width=30)
    table.add_column("Collection / Table", min_width=25)
    table.add_column("Rows", justify="right", min_width=10)
    table.add_column("Status", min_width=15)

    all_rows = check_mongo() + check_postgres() + check_redis()
    for store, col, count, status in all_rows:
        table.add_row(store, col, count, status)

    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
