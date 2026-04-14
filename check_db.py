import pymongo
import psycopg2
from rich.table import Table
from rich.console import Console
from rich import box

console = Console()
MONGO_URL = "mongodb://deploy:kaaslabs123@localhost:27017/?authSource=admin"
PG_DSN    = "host=localhost user=deploy password=kaaslabs123 dbname=padosme_indexing"

# ── PostgreSQL ────────────────────────────────────────────────────────────────
try:
    conn = psycopg2.connect(PG_DSN)
    cur  = conn.cursor()
    cur.execute("SELECT seller_id, name, latitude, longitude, status, last_indexed_at FROM indexed_sellers LIMIT 20")
    rows = cur.fetchall()
    cur.close(); conn.close()

    t = Table(title="PostgreSQL — indexed_sellers", box=box.ROUNDED)
    t.add_column("seller_id",  style="dim", width=14)
    t.add_column("name",       min_width=22)
    t.add_column("lat",        justify="right", width=10)
    t.add_column("lon",        justify="right", width=10)
    t.add_column("status",     width=8)
    t.add_column("last_indexed_at", width=20)
    for r in rows:
        t.add_row(str(r[0])[:12], str(r[1]), f"{r[2]:.4f}", f"{r[3]:.4f}", str(r[4]), str(r[5])[:19])
    console.print(t)
except Exception as e:
    console.print(f"[red]PG error: {e}[/red]")

# ── MongoDB discovery.sellers ─────────────────────────────────────────────────
try:
    c    = pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    docs = list(c["discovery"].sellers.find().limit(20))

    t = Table(title="MongoDB — discovery.sellers", box=box.ROUNDED)
    t.add_column("seller_id",  style="dim", width=14)
    t.add_column("name",       min_width=22)
    t.add_column("category",   width=18)
    t.add_column("city",       width=10)
    t.add_column("status",     width=8)
    t.add_column("available",  width=9)
    for d in docs:
        t.add_row(str(d.get("seller_id",""))[:12], d.get("name",""), d.get("category",""), d.get("city",""), d.get("status",""), str(d.get("available","")))
    console.print(t)
except Exception as e:
    console.print(f"[red]Mongo discovery error: {e}[/red]")

# ── MongoDB catalog_db.seller_geo ─────────────────────────────────────────────
try:
    docs = list(c["catalog_db"].seller_geo.find().limit(20))

    t = Table(title="MongoDB — catalog_db.seller_geo", box=box.ROUNDED)
    t.add_column("seller_id",  style="dim", width=14)
    t.add_column("name",       min_width=22)
    t.add_column("category",   width=18)
    t.add_column("rating",     justify="right", width=7)
    t.add_column("address",    min_width=28)
    for d in docs:
        t.add_row(str(d.get("seller_id",""))[:12], d.get("name",""), d.get("category",""), str(d.get("rating","")), d.get("address",""))
    console.print(t)
except Exception as e:
    console.print(f"[red]Mongo catalog error: {e}[/red]")
