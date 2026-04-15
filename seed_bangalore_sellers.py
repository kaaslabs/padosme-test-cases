#!/usr/bin/env python3
"""
Padosme — Bangalore Seller Seeder & Search
===========================================
Three modes in one script:

  SYNC mode  (--sync)   ← USE THIS
    Reads existing sellers from the catalogue MongoDB,
    assigns Bangalore GPS coordinates, and publishes seller.verified
    events so the indexing service geo-indexes them in Redis.
    Use this to make your existing 1000 catalogue sellers searchable.

  SEED mode  (default)
    Creates brand-new sellers and pushes them through the pipeline.

  SEARCH mode  (--search)
    Query the Redis geo index interactively by area or GPS.

Usage — sync existing catalogue sellers:
    python3 seed_bangalore_sellers.py --sync
    python3 seed_bangalore_sellers.py --sync --workers 20

Usage — search:
    python3 seed_bangalore_sellers.py --search

Requirements:
    pip install requests rich pika PyJWT faker redis pymongo
"""

import argparse
import json
import logging
import math
import random
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

# ── Third-party imports ────────────────────────────────────────────────────────
try:
    import jwt as pyjwt
    import pika
    import psycopg2
    import psycopg2.extras
    import pymongo
    import redis
    import requests
    from faker import Faker
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TimeElapsedColumn
    from rich.table import Table
    from rich import box
    from rich.text import Text
except ImportError as exc:
    print(f"[ERROR] Missing dependency: {exc}")
    print("Install with:  pip install requests rich pika PyJWT faker redis pymongo psycopg2-binary")
    raise SystemExit(1)

# ── Constants ──────────────────────────────────────────────────────────────────
BASE_LAT        = 12.9716          # Bangalore city centre latitude
BASE_LON        = 77.5946          # Bangalore city centre longitude
RADIUS_KM       = 10.0             # Seed within this radius

# Bangalore locality → (lat, lon) for area-based search
BANGALORE_AREAS = {
    # Central
    "mg road":                    (12.9756, 77.6071),
    "brigade road":               (12.9716, 77.6080),
    "commercial street":          (12.9833, 77.6081),
    "shivajinagar":               (12.9857, 77.6006),
    "cunningham road":            (12.9898, 77.5944),
    "richmond town":              (12.9634, 77.6028),
    "lavelle road":               (12.9685, 77.5994),
    "residency road":             (12.9711, 77.6116),
    "infantry road":              (12.9819, 77.6018),
    "cox town":                   (12.9936, 77.6160),
    "frazer town":                (12.9836, 77.6177),
    "ulsoor":                     (12.9826, 77.6205),
    "halasuru":                   (12.9826, 77.6243),
    "richmond circle":            (12.9622, 77.5973),

    # South
    "jayanagar":                  (12.9308, 77.5828),
    "jp nagar":                   (12.9102, 77.5850),
    "btm layout":                 (12.9165, 77.6101),
    "basavanagudi":               (12.9422, 77.5748),
    "banashankari":               (12.9256, 77.5466),
    "uttarahalli":                (12.8922, 77.5399),
    "kanakapura road":            (12.8949, 77.5661),
    "hulimavu":                   (12.8909, 77.6121),
    "arekere":                    (12.8782, 77.6156),
    "gottigere":                  (12.8620, 77.5979),
    "bannerghatta road":          (12.8936, 77.5974),
    "electronic city":            (12.8399, 77.6770),
    "electronic city phase 1":    (12.8452, 77.6602),
    "electronic city phase 2":    (12.8320, 77.6760),
    "hongasandra":                (12.8945, 77.6241),
    "begur":                      (12.8764, 77.6341),
    "harlur":                     (12.8996, 77.6628),
    "haralur road":               (12.9031, 77.6700),
    "carmelaram":                 (12.8921, 77.7105),
    "sarjapur":                   (12.8596, 77.7847),
    "sarjapur road":              (12.9121, 77.6862),
    "attibele":                   (12.7783, 77.7657),

    # Southeast
    "koramangala":                (12.9279, 77.6271),
    "hsr layout":                 (12.9116, 77.6389),
    "bellandur":                  (12.9258, 77.6762),
    "domlur":                     (12.9609, 77.6389),
    "ejipura":                    (12.9490, 77.6281),
    "vivek nagar":                (12.9643, 77.6428),
    "indiranagar":                (12.9784, 77.6408),
    "cv raman nagar":             (12.9854, 77.6603),
    "defence colony":             (12.9796, 77.6481),
    "hal airport road":           (12.9668, 77.6603),
    "murugeshpalya":              (12.9666, 77.6604),
    "marathahalli":               (12.9591, 77.6974),
    "brookefield":                (12.9630, 77.7108),
    "hoodi":                      (12.9907, 77.7145),
    "whitefield":                 (12.9698, 77.7500),
    "varthur":                    (12.9380, 77.7350),
    "kadugodi":                   (12.9896, 77.7558),
    "mahadevapura":               (12.9942, 77.7146),
    "itpl":                       (12.9851, 77.7268),
    "kundalahalli":               (12.9817, 77.7148),
    "kr puram":                   (13.0059, 77.6932),
    "tin factory":                (12.9996, 77.6710),

    # North
    "hebbal":                     (13.0354, 77.5970),
    "yelahanka":                  (13.1007, 77.5963),
    "yelahanka new town":         (13.1079, 77.5946),
    "rt nagar":                   (13.0218, 77.5972),
    "sahakar nagar":              (13.0387, 77.5882),
    "jakkur":                     (13.0673, 77.6003),
    "thanisandra":                (13.0579, 77.6244),
    "nagavara":                   (13.0438, 77.6195),
    "hennur":                     (13.0422, 77.6391),
    "horamavu":                   (13.0244, 77.6621),
    "banaswadi":                  (13.0175, 77.6459),
    "kalyan nagar":               (13.0270, 77.6470),
    "kammanahalli":               (13.0142, 77.6478),
    "ramamurthy nagar":           (13.0101, 77.6612),
    "kogilu":                     (13.0708, 77.6122),
    "vidyaranyapura":             (13.0625, 77.5618),
    "bagalur":                    (13.1510, 77.6797),
    "devanahalli":                (13.2464, 77.7124),
    "doddaballapur road":         (13.1303, 77.5820),

    # Northwest
    "rajajinagar":                (12.9902, 77.5560),
    "malleswaram":                (13.0035, 77.5681),
    "seshadripuram":              (13.0002, 77.5706),
    "yeshwanthpur":               (13.0261, 77.5503),
    "peenya":                     (13.0289, 77.5196),
    "jalahalli":                  (13.0444, 77.5222),
    "mathikere":                  (13.0218, 77.5593),
    "dasarahalli":                (13.0433, 77.5090),
    "chikkabanavara":             (13.0761, 77.4892),
    "tumkur road":                (13.0500, 77.5200),

    # West
    "rajajinagar extension":      (12.9800, 77.5350),
    "vijayanagar":                (12.9718, 77.5348),
    "nagarbhavi":                 (12.9601, 77.5072),
    "kengeri":                    (12.9139, 77.4824),
    "kengeri satellite town":     (12.9087, 77.4907),
    "rajarajeshwari nagar":       (12.9240, 77.5060),
    "mysore road":                (12.9410, 77.5180),
    "nayandahalli":               (12.9394, 77.5295),
    "girinagar":                  (12.9343, 77.5565),
    "chamrajpet":                 (12.9625, 77.5665),
    "chickpet":                   (12.9684, 77.5765),
    "v v puram":                  (12.9514, 77.5762),
    "sultanpete":                 (12.9705, 77.5789),

    # East
    "whitefield main road":       (12.9764, 77.7310),
    "hope farm":                  (12.9800, 77.7530),
    "pattandur agrahara":         (12.9960, 77.7370),
    "panathur":                   (12.9413, 77.7044),
    "kadubeesanahalli":           (12.9393, 77.7199),
    "budigere":                   (13.0525, 77.7831),

    # Old areas / landmarks
    "richmond circle":            (12.9622, 77.5973),
    "wilson garden":              (12.9533, 77.6002),
    "langford town":              (12.9573, 77.5954),
    "cambridge layout":           (12.9905, 77.6351),
    "murphy town":                (12.9944, 77.6229),
    "shanthinagar":               (12.9593, 77.5918),
    "gandhi nagar":               (12.9773, 77.5735),
    "cottonpet":                  (12.9759, 77.5723),
    "majestic":                   (12.9773, 77.5707),
    "city market":                (12.9665, 77.5757),
    "rajiv gandhi nagar":         (12.9540, 77.5590),

    # North Bangalore — Nagawara / Manyata area
    "nagawara":                   (13.0467, 77.6218),
    "manyata tech park":          (13.0474, 77.6208),
    "manyata":                    (13.0474, 77.6208),
    "crystal palace":             (13.0438, 77.6195),
    "hebbal flyover":             (13.0394, 77.5970),
    "nh 7":                       (13.0500, 77.6100),
    "rachenahalli":               (13.0589, 77.6185),
    "lottegollahalli":            (13.0590, 77.6040),
    "sahakara nagar":             (13.0387, 77.5882),
    "bytarayanapura":             (13.0490, 77.5923),
    "kalkere":                    (13.0387, 77.6623),
    "singapura":                  (13.0618, 77.5948),

    # RT Nagar / Hebbal / Sahakar Nagar cluster
    "r t nagar":                  (13.0218, 77.5972),  # alternate spelling of rt nagar
    "r.t nagar":                  (13.0218, 77.5972),  # alternate spelling of rt nagar
    "r.t. nagar":                 (13.0218, 77.5972),  # alternate spelling of rt nagar
    "rahamath nagar":             (13.0285, 77.5940),
    "kamanahalli":                (13.0142, 77.6478),  # alternate spelling of kammanahalli
    "rajanukunte":                 (13.1265, 77.5965),
    "rajanukunte area":            (13.1265, 77.5965),
    "kodigehalli":                (13.0512, 77.5907),
    "dollars colony":             (13.0380, 77.6040),
    "kaval byrasandra":           (13.0190, 77.6080),
    "aramane nagar":              (12.9986, 77.5498),
    "benson town":                (12.9942, 77.6143),
    "pulakeshinagar":             (12.9920, 77.6200),
    "jalahalli cross":            (13.0480, 77.5180),
    "saneguruvanahalli":          (13.0350, 77.5050),
    "hessarghatta":               (13.1020, 77.4838),
    "hessarghatta road":          (13.0700, 77.5200),

    # Palya localities (north/east Bangalore)
    "palya":                      (13.0155, 77.6495),  # Kammanahalli Palya area
    "nagawara palya":             (13.0450, 77.6250),
    "banaswadi palya":            (13.0140, 77.6510),
    "tc palya":                   (13.0175, 77.6510),
    "laggere":                    (13.0350, 77.5150),

    # RT Nagar sub-areas and adjacent localities
    "sanjay nagar":               (13.0298, 77.5742),
    "ganganagar":                 (13.0196, 77.5782),
    "rajgopal nagar":             (13.0150, 77.5700),
    "sadashivanagar":             (13.0071, 77.5807),
    "kamanahalli main road":      (13.0160, 77.6490),
    "nagawara main road":         (13.0460, 77.6220),
    "kogilu cross":               (13.0700, 77.6050),
    "new airport road":           (13.0580, 77.6100),
    "ballari road":               (13.0490, 77.5980),
    "hebbal kempapura":           (13.0430, 77.5870),
    "hebbal lake":                (13.0430, 77.5920),
    "maruthi nagar":              (13.0220, 77.5830),
    "annapurneshwari nagar":      (13.0270, 77.5680),
    "defence layout":             (13.0310, 77.5920),
    "cholanayakanahalli":         (13.0540, 77.5830),
    "yelahanka satellite town":   (13.0980, 77.5870),
    "attur layout":               (13.1080, 77.5770),
    "sahakar nagar extension":    (13.0420, 77.5860),
    "abbigere":                   (13.0650, 77.5070),
    "nagarabhavi 2nd stage":      (12.9550, 77.4980),
    "devasandra":                 (13.0110, 77.7050),
    "kammanahalli junction":      (13.0170, 77.6500),
    "sarakki":                    (12.9120, 77.5690),

    # Rajanukunte sub-areas and corridor
    "rajanukunte main road":       (13.1265, 77.5965),
    "rajanukunte bus stand":       (13.1270, 77.5958),
    "rajanukunte market":          (13.1268, 77.5963),
    "nh 44 rajanukunte":           (13.1255, 77.5972),
    "doddaballapur road rajanukunte": (13.1310, 77.5822),
}
EARTH_RADIUS_KM = 6371.0

CATALOGUE_URL   = "http://localhost:8086"
INDEXING_URL    = "http://localhost:8089"
RABBITMQ_URL    = "amqp://deploy:kaaslabs123@localhost:5672/"
MONGO_URL       = "mongodb://deploy:kaaslabs123@localhost:27017/"
MONGO_DB        = "catalog_db"
MONGO_DISCOVERY_DB = "discovery"
REDIS_HOST      = "localhost"
REDIS_PORT      = 6379
REDIS_PASSWORD  = None
PG_DSN          = "host=localhost port=5433 user=deploy password=kaaslabs123 dbname=padosme_indexing"

JWT_SECRET      = "this-time-shall-pass"
EXCHANGE_SELLER = "padosme.events"
EXCHANGE_CAT    = "catalog.events"

DEFAULT_COUNT   = 500   # 50 localities × 10 sellers each = guaranteed coverage
DEFAULT_WORKERS = 15
MAX_RETRIES     = 3
RETRY_BACKOFF   = 0.5    # seconds (doubles each retry)

# Realistic Bangalore shop names
SHOP_PREFIXES = [
    "Sharma", "Gupta", "Patel", "Singh", "Kumar", "Verma", "Joshi", "Mehta",
    "Agarwal", "Mishra", "Yadav", "Reddy", "Nair", "Iyer", "Pillai", "Bhat",
    "Chopra", "Malhotra", "Kapoor", "Sinha", "Bansal", "Goel", "Arora", "Rao",
    "Desai", "Shah", "Jain", "Pandey", "Tiwari", "Dubey", "Saxena", "Srivastava",
    "Chauhan", "Bhatt", "Kulkarni", "Patil", "Naik", "Kaur", "Anand", "Khanna",
    "Choudhary", "Trivedi", "Awasthi", "Shukla", "Rastogi", "Bajaj", "Mittal",
    "Goyal", "Tandon", "Mathur",
]

SHOP_TYPES = [
    "General Store", "Kirana", "Provisions", "Mart", "Emporium", "Traders",
    "Brothers Store", "& Sons", "Supermart", "Bazaar", "Enterprise", "Shop",
    "Depot", "Centre", "Plaza", "Collection", "Gallery", "Hub", "World",
    "Corner Store", "Daily Needs", "Fresh Mart", "Mega Store", "Mini Mart",
    "Family Store", "Super Store", "Cash & Carry", "Wholesale", "Retail Store",
    "Fancy Store", "Gift House", "Variety Store", "Quick Shop", "Smart Shop",
]

# Bangalore localities for realistic addresses
BANGALORE_LOCALITIES = [
    "Koramangala", "Indiranagar", "Jayanagar", "JP Nagar", "HSR Layout",
    "Whitefield", "Marathahalli", "Bellandur", "Sarjapur Road", "Electronic City",
    "BTM Layout", "Bannerghatta Road", "Hebbal", "Yelahanka", "Rajajinagar",
    "Malleswaram", "Basavanagudi", "Banashankari", "Vijayanagar", "Nagarbhavi",
    "RT Nagar", "Kammanahalli", "Frazer Town", "Shivajinagar", "MG Road",
    "Cunningham Road", "Lavelle Road", "Richmond Town", "Ulsoor", "CV Raman Nagar",
    "Domlur", "Ejipura", "Varthur", "Kadugodi", "Brookefield",
    "Hoodi", "KR Puram", "Tin Factory", "Banaswadi", "Kalyan Nagar",
    "Hennur", "Jakkur", "Thanisandra", "Kogilu", "Sahakar Nagar",
    "Devanahalli", "Doddaballapur Road", "Nagavara", "Kalkere", "Horamavu",
    # North Bangalore cluster
    "Rahamath Nagar", "Sanjay Nagar", "Ganganagar", "Sadashivanagar",
    "Rajanukunte", "Hebbal Kempapura", "Nagawara Palya", "TC Palya",
    "Kogilu Cross", "New Airport Road", "Maruthi Nagar", "Defence Layout",
    "Ballari Road", "Kammanahalli Main Road", "Banaswadi Palya",
    "Annapurneshwari Nagar", "Cholanayakanahalli", "Yelahanka Satellite Town",
]

CATEGORIES = [
    "Electronics", "Clothing", "Books", "Sports", "Home & Kitchen",
    "Beauty & Personal Care", "Food & Grocery", "Toys & Baby",
    "Automotive", "Health & Wellness", "Stationery", "Hardware",
]

SUBSCRIPTION_TIERS = ["free", "bronze", "silver", "gold", "platinum"]
TIER_WEIGHTS       = [40, 25, 20, 10, 5]   # probability weights

# ── Real named shops — verified via Magicpin / Zomato / Justdial ─────────────
# Format: (name, address, latitude, longitude, category, tier, rating, reviews, phone)
REAL_SELLERS = [
    # Real shops fetched from OpenStreetMap -- covers all Bangalore zones
    ("Koshy's", "39, Saint Mark's Road, Shanthala Nagar, Bangalore", 12.975632, 77.601476, 'Food & Grocery', 'silver', 3.4, 190, ''),
    ('Konārk', 'MG Road, Bangalore', 12.96881, 77.602143, 'Food & Grocery', 'free', 2.8, 13, '+918041248812'),
    ('Old Madras Baking Company', "Saint Mark's Road, MG Road, Bangalore", 12.969738, 77.600377, 'Food & Grocery', 'silver', 4.3, 94, ''),
    ('Hard Rock Cafe', "Saint Mark's Road, MG Road, Bangalore", 12.976226, 77.601481, 'Food & Grocery', 'platinum', 4.6, 760, ''),
    ('The Only Place', '13, Museum Road, MG Road, Bangalore', 12.973244, 77.60339, 'Food & Grocery', 'free', 2.9, 64, '+918032718989'),
    ('Temple of The Senses', 'Museum Road, Shanthala Nagar, Bangalore', 12.975103, 77.602844, 'Food & Grocery', 'silver', 4.0, 416, ''),
    ('Indian Coffee House', 'Church Street, MG Road, Bangalore', 12.97454, 77.60525, 'Food & Grocery', 'silver', 3.9, 162, ''),
    ('Blossom', 'Church Street, MG Road, Bangalore', 12.975174, 77.604825, 'Books', 'bronze', 3.3, 21, ''),
    ('Juice Junction', "Saint Mark's Road, MG Road, Bangalore", 12.972032, 77.600931, 'Food & Grocery', 'silver', 3.6, 266, ''),
    ('Bowring Stall', "Saint Mark's Road, MG Road, Bangalore", 12.975115, 77.601415, 'Food & Grocery', 'bronze', 3.2, 106, ''),
    ('Subway', 'Kasturba Road, MG Road, Bangalore', 12.975824, 77.597543, 'Food & Grocery', 'silver', 3.5, 244, ''),
    ("McDonald's", 'Kasturba Road, MG Road, Bangalore', 12.975768, 77.59845, 'Food & Grocery', 'gold', 3.8, 1835, ''),
    ('Hotel Empire', 'MG Road, Bangalore', 12.979839, 77.602791, 'Food & Grocery', 'bronze', 3.3, 31, ''),
    ("Levi's", 'Lavelle Road, MG Road, Bangalore', 12.971011, 77.598276, 'Clothing', 'silver', 3.9, 243, ''),
    ('Espirit', 'Lavelle Road, MG Road, Bangalore', 12.970875, 77.599004, 'Clothing', 'free', 3.0, 80, ''),
    ('Giovani', "Saint Mark's Road, MG Road, Bangalore", 12.970718, 77.600529, 'Clothing', 'silver', 4.3, 345, ''),
    ('Amruth Vegetarian Restaurant', "Saint Mark's Road, MG Road, Bangalore", 12.971194, 77.600779, 'Food & Grocery', 'free', 2.8, 29, ''),
    ('Hotel Vinitha', 'MG Road, Bangalore', 12.982738, 77.603055, 'Food & Grocery', 'silver', 4.4, 487, ''),
    ('Ebony', '84, Mahatma Gandhi Road, MG Road, Bangalore', 12.975362, 77.605164, 'Food & Grocery', 'free', 2.8, 35, '+918025136611;+918041783344'),
    ('Ullas Refreshment', 'MG Road, Bangalore', 12.974538, 77.609603, 'Food & Grocery', 'bronze', 3.8, 61, ''),
    ('Highgates Hotel', 'Church Street, MG Road, Bangalore', 12.975375, 77.602894, 'Food & Grocery', 'bronze', 3.2, 88, ''),
    ('Olive Beach', 'MG Road, Bangalore', 12.966942, 77.60796, 'Food & Grocery', 'silver', 4.1, 86, ''),
    ('Gem Plaza', 'Infantry Road, MG Road, Bangalore', 12.980388, 77.604616, 'Food & Grocery', 'silver', 3.6, 423, ''),
    ('The Cash Pharmacy', "74/5, Saint Mark's Road, MG Road, Bangalore", 12.968315, 77.600437, 'Health & Wellness', 'free', 3.2, 34, '+918033618456'),
    ('Baby Center', 'Commercial Street, MG Road, Bangalore', 12.982599, 77.607025, 'Clothing', 'platinum', 4.6, 2299, ''),
    ('Bata', 'Commercial Street, MG Road, Bangalore', 12.982174, 77.60817, 'Clothing', 'silver', 4.2, 447, ''),
    ('KFC', 'Commercial Street, MG Road, Bangalore', 12.982389, 77.607536, 'Food & Grocery', 'gold', 3.8, 1783, ''),
    ('Westside', 'Commercial Street, MG Road, Bangalore', 12.982629, 77.606854, 'Clothing', 'gold', 3.7, 746, ''),
    ('Ruby Tuesday', 'MG Road, Bangalore', 12.974685, 77.605772, 'Food & Grocery', 'bronze', 3.1, 165, ''),
    ('Purple Haze', 'MG Road, Bangalore', 12.972963, 77.608605, 'Food & Grocery', 'gold', 4.0, 1442, ''),
    ('Paris Panini', 'Church Street, Ashok Nagar, Bangalore', 12.974756, 77.605137, 'Food & Grocery', 'bronze', 3.9, 184, ''),
    ('Sri Vijaylakshmi Silks And Sarees', 'MG Road, Bangalore', 12.974991, 77.607219, 'Clothing', 'bronze', 3.3, 83, ''),
    ('Max', '119/120, Commercial Street, MG Road, Bangalore', 12.982745, 77.606977, 'Clothing', 'silver', 3.9, 432, ''),
    ('The Brown Shop', '20, Commercial Street, MG Road, Bangalore', 12.981787, 77.609277, 'Clothing', 'silver', 4.3, 254, ''),
    ('Lee Showroom', 'Commercial Street, MG Road, Bangalore', 12.981646, 77.609593, 'Clothing', 'bronze', 4.0, 55, ''),
    ('Cool Joint', 'Kamaraj Road, MG Road, Bangalore', 12.982545, 77.61043, 'Food & Grocery', 'bronze', 3.1, 32, ''),
    ('Paramount Restaurant', 'MG Road, Bangalore', 12.971167, 77.605779, 'Food & Grocery', 'gold', 3.9, 427, ''),
    ('ShowOff', 'MG Road, Bangalore', 12.967965, 77.606495, 'Clothing', 'silver', 3.8, 82, ''),
    ('Airlines', 'MG Road, Bangalore', 12.972731, 77.599718, 'Food & Grocery', 'bronze', 3.6, 139, ''),
    ('Rice Bowl', 'Lavelle Road, MG Road, Bangalore', 12.974825, 77.599433, 'Food & Grocery', 'bronze', 4.0, 22, ''),
    ('Mavalli Tiffin Rooms', 'MG Road, Bangalore', 12.972176, 77.600954, 'Food & Grocery', 'silver', 3.5, 324, ''),
    ('Bombay Dyeing', 'MG Road, Bangalore', 12.975322, 77.606441, 'Clothing', 'silver', 4.2, 224, ''),
    ('Panasonic', 'MG Road, Bangalore', 12.975952, 77.614773, 'Electronics', 'free', 3.1, 58, ''),
    ('Palm Grove', 'MG Road, Bangalore', 12.97159, 77.608498, 'Food & Grocery', 'free', 3.4, 33, ''),
    ('Sangam', 'MG Road, Bangalore', 12.971893, 77.606567, 'Food & Grocery', 'platinum', 4.8, 4658, ''),
    ('F & B', 'MG Road, Bangalore', 12.971312, 77.599727, 'Food & Grocery', 'gold', 4.6, 711, ''),
    ('Sreeraj lassi bar', 'MG Road, Bangalore', 12.982804, 77.610527, 'Food & Grocery', 'gold', 4.2, 507, ''),
    ('Harima', 'MG Road, Bangalore', 12.967621, 77.600178, 'Food & Grocery', 'free', 3.5, 69, ''),
    ('Coffee All Day', 'Brigade Road, Ashok Nagar, Bangalore', 12.97339, 77.607292, 'Food & Grocery', 'platinum', 4.9, 504, ''),
    ('Toscano', 'Museum Road, Shanthala Nagar, Bangalore', 12.97506, 77.602857, 'Food & Grocery', 'silver', 3.9, 107, ''),
    ('Three Quarter Chinese', '22, Church Street, MG Road, Bangalore', 12.974503, 77.605464, 'Food & Grocery', 'gold', 4.6, 1803, ''),
    ('Blossom Book House', 'Church Street, Ashok Nagar, Bangalore', 12.974817, 77.605225, 'Books', 'gold', 3.9, 593, ''),
    ('Tamanna', 'Dispensary Road, MG Road, Bangalore', 12.981686, 77.608308, 'Clothing', 'gold', 4.6, 275, ''),
    ('Kalyan Silks', 'MG Road, Bangalore', 12.981451, 77.607778, 'Clothing', 'silver', 4.2, 439, ''),
    ('Chutney Chang', 'Museum Road, MG Road, Bangalore', 12.97378, 77.602882, 'Food & Grocery', 'bronze', 3.1, 188, '+918040001234'),
    ('Empire', '36, Church Street, MG Road, Bangalore', 12.975404, 77.602778, 'Food & Grocery', 'bronze', 3.5, 87, '+918040414141'),
    ("Matteo's Coffee", 'MG Road, Bangalore', 12.974351, 77.607073, 'Food & Grocery', 'bronze', 3.6, 74, ''),
    ('Hae Kum Gang', '20, Castle Street, MG Road, Bangalore', 12.967939, 77.607429, 'Food & Grocery', 'gold', 4.5, 1512, ''),
    ('Sweet Chariot', 'MG Road, Bangalore', 12.968487, 77.600802, 'Food & Grocery', 'free', 3.0, 47, ''),
    ('Coconut Grove', 'Church Street, MG Road, Bangalore', 12.975285, 77.604324, 'Food & Grocery', 'bronze', 3.5, 50, ''),
    ('Nutan Silks', 'Dispensary Road, MG Road, Bangalore', 12.981492, 77.608333, 'Clothing', 'free', 2.8, 2, ''),
    ('Lake N Bake', 'Kamaraj Road, MG Road, Bangalore', 12.98111, 77.610225, 'Food & Grocery', 'silver', 3.6, 162, ''),
    ('Hotel barbies', 'Kamaraj Road, MG Road, Bangalore', 12.981106, 77.610258, 'Food & Grocery', 'free', 3.4, 7, ''),
    ('Inmark', 'Main Guard Cross Road, MG Road, Bangalore', 12.98054, 77.607256, 'Clothing', 'free', 3.6, 42, ''),
    ('Cafe Coffe Day', 'Commercial Street, MG Road, Bangalore', 12.981828, 77.609208, 'Food & Grocery', 'free', 2.9, 62, ''),
    ('Mysore Saree Udyog', 'Kamaraj Road, MG Road, Bangalore', 12.98146, 77.610415, 'Clothing', 'free', 2.8, 73, ''),
    ('Sterling House', 'Commercial Street, MG Road, Bangalore', 12.981953, 77.608885, 'Clothing', 'silver', 3.6, 292, ''),
    ('Shiv Sagar', 'Commercial Street, MG Road, Bangalore', 12.981936, 77.608222, 'Food & Grocery', 'gold', 3.9, 298, ''),
    ('Shezan', '68, Lavelle Road, MG Road, Bangalore', 12.973681, 77.59871, 'Food & Grocery', 'silver', 3.8, 260, ''),
    ('Tafe Access - Skoda', 'MG Road, Bangalore', 12.974511, 77.601677, 'Automotive', 'bronze', 3.7, 192, ''),
    ("Woody's", 'Commercial Street, MG Road, Bangalore', 12.981872, 77.609493, 'Food & Grocery', 'silver', 4.0, 81, ''),
    ('Church Street Social', '46/1, Church Street, Ashok Nagar, Bangalore', 12.975762, 77.602608, 'Food & Grocery', 'bronze', 3.3, 47, ''),
    ('Ayda', 'Church Street, MG Road, Bangalore', 12.974273, 77.607273, 'Food & Grocery', 'free', 2.9, 57, ''),
    ('Sapna Book House', 'MG Road, Bangalore', 12.973107, 77.608281, 'Books', 'free', 2.9, 59, ''),
    ('KSIC Seconds Showroom', 'MG Road, Bangalore', 12.974056, 77.609206, 'Clothing', 'free', 3.6, 56, ''),
    ('Sara: The Silk Store', 'MG Road, Bangalore', 12.975886, 77.602623, 'Clothing', 'gold', 4.6, 300, ''),
    ('Cafe Coffee Day', 'MG Road, Bangalore', 12.975745, 77.604615, 'Food & Grocery', 'silver', 3.5, 326, ''),
    ('The Raymond Shop', 'MG Road, Bangalore', 12.975696, 77.604916, 'Clothing', 'gold', 4.7, 1997, ''),
    ("Domino's", 'MG Road, Bangalore', 12.973002, 77.617305, 'Food & Grocery', 'gold', 4.5, 584, ''),
    ("Nilgiri's", 'MG Road, Bangalore', 12.972863, 77.607439, 'Food & Grocery', 'free', 3.2, 27, ''),
    ('Planet Fashion', 'Residency Road, MG Road, Bangalore', 12.97294, 77.608544, 'Clothing', 'gold', 4.6, 437, ''),
    ('Starbucks', 'MG Road, Bangalore', 12.974552, 77.607336, 'Food & Grocery', 'gold', 4.1, 899, ''),
    ('Lakeview Milk Bar', 'MG Road, Bangalore', 12.97597, 77.603724, 'Food & Grocery', 'free', 3.5, 58, ''),
    ('Meghana Foods', '454, Field Marshal Cariappa Road, MG Road, Bangalore', 12.972822, 77.609182, 'Food & Grocery', 'free', 3.4, 71, ''),
    ('Smoke House Deli', 'Lavelle Road, MG Road, Bangalore', 12.97151, 77.598326, 'Food & Grocery', 'silver', 3.9, 147, ''),
    ('Select Bookshop', 'MG Road, Bangalore', 12.973525, 77.608123, 'Books', 'free', 3.7, 74, '+917829803202'),
    ('National Motors', 'MG Road, Bangalore', 12.981425, 77.601538, 'Automotive', 'silver', 3.5, 210, ''),
    ('Santosh Chat', 'MG Road, Bangalore', 12.981386, 77.601419, 'Food & Grocery', 'free', 3.3, 64, ''),
    ('Manyavar', 'MG Road, Bangalore', 12.975209, 77.606842, 'Clothing', 'gold', 4.2, 216, ''),
    ('Soch', 'MG Road, Bangalore', 12.9752, 77.606899, 'Clothing', 'platinum', 4.1, 2022, ''),
    ('Marico Cotton Industries Emporium', 'MG Road, Bangalore', 12.975661, 77.604955, 'Clothing', 'free', 2.8, 30, ''),
    ('Titan World', 'MG Road, Bangalore', 12.975458, 77.605981, 'Health & Wellness', 'gold', 4.1, 1923, ''),
    ('Nandini', 'MG Road, Bangalore', 12.975432, 77.60682, 'Food & Grocery', 'silver', 4.0, 70, ''),
    ('Hum India', 'MG Road, Bangalore', 12.975175, 77.603711, 'Clothing', 'silver', 3.8, 348, ''),
    ("Bheema's", 'MG Road, Bangalore', 12.975186, 77.603588, 'Food & Grocery', 'silver', 3.7, 183, ''),
    ('Mainland China', 'MG Road, Bangalore', 12.975237, 77.603421, 'Food & Grocery', 'free', 3.4, 30, ''),
    ('Oh! Calcutta', 'MG Road, Bangalore', 12.975223, 77.60352, 'Food & Grocery', 'free', 2.8, 38, ''),
    ('Desi Vdesi', 'MG Road, Bangalore', 12.975533, 77.603275, 'Food & Grocery', 'bronze', 3.9, 38, ''),
    ('C# chicken', 'MG Road, Bangalore', 12.975538, 77.603247, 'Food & Grocery', 'free', 3.3, 72, ''),
    ('Hooked', 'MG Road, Bangalore', 12.975558, 77.603129, 'Food & Grocery', 'platinum', 4.1, 2246, ''),
    ('Matsya', 'MG Road, Bangalore', 12.975544, 77.603196, 'Food & Grocery', 'bronze', 3.1, 109, ''),
    ('KaatiZone', 'MG Road, Bangalore', 12.975553, 77.603152, 'Food & Grocery', 'gold', 4.6, 856, ''),
    ('The Entertainment Store', 'MG Road, Bangalore', 12.975565, 77.603022, 'Toys & Baby', 'free', 3.1, 69, ''),
    ('Bundle Company', 'MG Road, Bangalore', 12.975615, 77.602925, 'Food & Grocery', 'silver', 4.0, 463, ''),
    ('Khanate', 'MG Road, Bangalore', 12.975724, 77.602403, 'Clothing', 'silver', 3.4, 468, ''),
    ('Mobile Shop', 'MG Road, Bangalore', 12.975725, 77.602374, 'Electronics', 'silver', 4.3, 103, ''),
    ('Mulani Optics', 'MG Road, Bangalore', 12.9762, 77.602761, 'Health & Wellness', 'gold', 3.8, 336, ''),
    ('Paresh Lamba Signatures', 'MG Road, Bangalore', 12.976191, 77.602641, 'Clothing', 'gold', 4.4, 418, ''),
    ('Karavalli', '66, Residency Road, MG Road, Bangalore', 12.972069, 77.60891, 'Food & Grocery', 'free', 3.3, 43, '+919620162387'),
    ('Udupi Park', 'MG Road, Bangalore', 12.97285, 77.609197, 'Food & Grocery', 'free', 3.3, 33, ''),
    ('Nagarjuna', '44/1, Residency Road, MG Road, Bangalore', 12.97323, 77.609195, 'Food & Grocery', 'bronze', 3.3, 33, '+9125592233'),
    ('Chai Point', 'MG Road, Bangalore', 12.973192, 77.616735, 'Food & Grocery', 'free', 3.1, 35, ''),
    ('Caesars', 'MG Road, Bangalore', 12.973384, 77.61639, 'Food & Grocery', 'free', 3.0, 16, ''),
    ('Chung Wah', 'MG Road, Bangalore', 12.974067, 77.609249, 'Food & Grocery', 'silver', 3.7, 429, ''),
    ('Donne Biryani House', 'MG Road, Bangalore', 12.973298, 77.609358, 'Food & Grocery', 'bronze', 3.7, 163, ''),
    ('Colorplus', 'MG Road, Bangalore', 12.972091, 77.607796, 'Clothing', 'free', 2.8, 19, ''),
    ('Check In', 'MG Road, Bangalore', 12.972062, 77.607812, 'Clothing', 'bronze', 3.8, 169, ''),
    ('Nike', 'MG Road, Bangalore', 12.974463, 77.607584, 'Clothing', 'gold', 4.3, 980, ''),
    ("Mel's Korner", 'MG Road, Bangalore', 12.97437, 77.606975, 'Food & Grocery', 'free', 3.0, 5, ''),
    ('Tandoor', 'MG Road, Bangalore', 12.973672, 77.61318, 'Food & Grocery', 'gold', 3.9, 611, ''),
    ('Beef Market', 'Hazrat Kambal Posh Road, MG Road, Bangalore', 12.986024, 77.605311, 'Food & Grocery', 'silver', 3.8, 336, ''),
    ('CLS Book Shop', 'MG Road, Bangalore', 12.974383, 77.615002, 'Books', 'gold', 4.1, 1371, ''),
    ('Bhaiya Ji Food Court', 'MG Road, Bangalore', 12.975146, 77.614881, 'Food & Grocery', 'silver', 4.3, 171, ''),
    ('Vibhas', 'MG Road, Bangalore', 12.975133, 77.614953, 'Food & Grocery', 'gold', 4.7, 1760, ''),
    ('MumbaiStyle', 'MG Road, Bangalore', 12.975135, 77.61497, 'Food & Grocery', 'free', 3.1, 22, ''),
    ('Page3', 'MG Road, Bangalore', 12.975097, 77.61508, 'Books', 'silver', 3.7, 260, ''),
    ('Paparazzi', 'MG Road, Bangalore', 12.974895, 77.615365, 'Food & Grocery', 'gold', 4.6, 1760, ''),
    ('Pinxx', 'MG Road, Bangalore', 12.974843, 77.615369, 'Food & Grocery', 'free', 2.9, 13, ''),
    ('SR Gopal Rao', 'MG Road, Bangalore', 12.967633, 77.600056, 'Health & Wellness', 'bronze', 3.0, 140, ''),
    ('Chetak Pharmacy', 'MG Road, Bangalore', 12.967805, 77.600286, 'Health & Wellness', 'free', 3.5, 58, ''),
    ('Khan Saheb Grills and Rolls', 'MG Road, Bangalore', 12.982679, 77.61051, 'Food & Grocery', 'bronze', 3.8, 78, ''),
    ('Wrangler', 'Commercial Street, MG Road, Bangalore', 12.9816, 77.610173, 'Clothing', 'free', 3.4, 51, ''),
    ('Moksh Studio', 'Kamaraj Road, MG Road, Bangalore', 12.981953, 77.610323, 'Clothing', 'bronze', 3.9, 91, ''),
    ('Moksh', 'Kamaraj Road, MG Road, Bangalore', 12.98173, 77.610447, 'Clothing', 'bronze', 3.5, 193, ''),
    ('SodaBottleOpenerWala', '25/4, Lavelle Road, MG Road, Bangalore', 12.970658, 77.597535, 'Food & Grocery', 'platinum', 4.5, 726, '+9107022255299'),
    ('Dasaprakash', 'MG Road, Bangalore', 12.976517, 77.602975, 'Food & Grocery', 'free', 3.7, 22, ''),
    ('Vijayalakshmi Silks', 'MG Road, Bangalore', 12.975005, 77.6076, 'Clothing', 'silver', 4.4, 69, ''),
    ('The Book Room', 'MG Road, Bangalore', 12.976414, 77.601206, 'Books', 'free', 3.1, 40, ''),
    ('The Bangalore Ham Shop', 'MG Road, Bangalore', 12.976189, 77.60284, 'Food & Grocery', 'bronze', 4.0, 49, ''),
    ('Nanak', 'MG Road, Bangalore', 12.967557, 77.606123, 'Food & Grocery', 'bronze', 3.6, 85, ''),
    ('Sundeep Medical Shop', 'MG Road, Bangalore', 12.967613, 77.606184, 'Health & Wellness', 'free', 3.1, 66, ''),
    ('Reebok', 'MG Road, Bangalore', 12.967538, 77.60621, 'Clothing', 'silver', 4.3, 325, ''),
    ('Brigade Dreams', 'MG Road, Bangalore', 12.968541, 77.606131, 'Food & Grocery', 'silver', 4.3, 427, ''),
    ('Model Medical Stores', 'MG Road, Bangalore', 12.968793, 77.606368, 'Health & Wellness', 'silver', 3.8, 85, ''),
    ('Chariot', 'MG Road, Bangalore', 12.971891, 77.606684, 'Food & Grocery', 'gold', 4.6, 1376, ''),
    ("Dunkin'", 'MG Road, Bangalore', 12.972057, 77.606792, 'Food & Grocery', 'bronze', 3.8, 96, ''),
    ('Tibet Bazaar', 'MG Road, Bangalore', 12.97224, 77.606883, 'Clothing', 'bronze', 3.7, 103, ''),
    ('InMark Factory Outlet', 'MG Road, Bangalore', 12.981025, 77.602433, 'Clothing', 'bronze', 3.3, 52, ''),
    ('Redington', 'MG Road, Bangalore', 12.980856, 77.602201, 'Electronics', 'free', 3.4, 48, ''),
    ('Green Apple', 'MG Road, Bangalore', 12.981408, 77.601268, 'Health & Wellness', 'platinum', 4.7, 1925, ''),
    ('Sultania Arts Emporium', 'MG Road, Bangalore', 12.981211, 77.601822, 'Clothing', 'silver', 3.7, 330, ''),
    ('PC Mallappa & Co', 'MG Road, Bangalore', 12.981065, 77.601564, 'Hardware', 'gold', 4.0, 530, ''),
    ('Stationery shop', 'MG Road, Bangalore', 12.981777, 77.601394, 'Stationery', 'bronze', 3.6, 187, ''),
    ('Aman Trading Co', 'MG Road, Bangalore', 12.982643, 77.601371, 'Hardware', 'bronze', 3.4, 192, ''),
    ('Best Auto Traders', 'MG Road, Bangalore', 12.982643, 77.601371, 'Automotive', 'free', 3.2, 21, ''),
    ('Hotel Metropole', 'MG Road, Bangalore', 12.980636, 77.602787, 'Food & Grocery', 'silver', 3.7, 389, ''),
    ('Light & Style', 'MG Road, Bangalore', 12.98079, 77.602481, 'Electronics', 'silver', 3.7, 469, ''),
    ('Sree Balaji Plywoods', 'MG Road, Bangalore', 12.980744, 77.602658, 'Hardware', 'platinum', 4.2, 3042, ''),
    ('Twist of Tadka', 'MG Road, Bangalore', 12.96788, 77.606622, 'Food & Grocery', 'free', 2.9, 3, ''),
    ('Sacred Heart Church Religious Stall', 'MG Road, Bangalore', 12.966453, 77.609333, 'Books', 'free', 3.7, 78, ''),
    ('Fruit xtracts', 'MG Road, Bangalore', 12.966515, 77.606521, 'Food & Grocery', 'gold', 3.8, 948, ''),
    ('Sangeetha', 'MG Road, Bangalore', 12.967053, 77.606802, 'Electronics', 'gold', 4.3, 1571, ''),
    ('Wolfish', 'Church street, MG Road, Bangalore', 12.974367, 77.607176, 'Food & Grocery', 'silver', 3.9, 174, ''),
    ('Wiliam Penn', 'Ashok Nagar, 15 Magrath Road, MG Road, Bangalore', 12.970294, 77.609785, 'Stationery', 'free', 3.4, 13, '+918041127993'),
    ('CIS', 'Magrath Rd, Ashok Nagar, Bengaluru,, Magrath Road, MG Road, Bangalore', 12.970278, 77.609878, 'Clothing', 'silver', 3.6, 461, ''),
    ('Sunglass Hot', 'Ashok Naga, Magrath Road, MG Road, Bangalore', 12.970084, 77.609578, 'Health & Wellness', 'platinum', 4.5, 911, '+918040698857'),
    ('Sands', '15  Ashok Nagar, Magrath Road, MG Road, Bangalore', 12.97018, 77.609528, 'Beauty & Personal Care', 'silver', 4.3, 112, '+918040954128'),
    ('Cookie Man', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970312, 77.609411, 'Food & Grocery', 'bronze', 3.8, 190, ''),
    ('Inglot', '14 Ashoknagar, Magrath Road, MG Road, Bangalore', 12.97022, 77.609706, 'Beauty & Personal Care', 'bronze', 3.6, 101, '+918040698857'),
    ('Colourbar U.S.A', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970157, 77.609787, 'Beauty & Personal Care', 'platinum', 4.9, 4635, '+918041132837'),
    ("Clarie's", 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970333, 77.609506, 'Clothing', 'bronze', 3.9, 134, '+918040698857'),
    ('Tommy Hilfiger', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970126, 77.609458, 'Clothing', 'gold', 4.4, 1072, '+918041573443'),
    ('Accessorize', '15, Magrath Road, MG Road, Bangalore', 12.970076, 77.610043, 'Clothing', 'bronze', 3.8, 183, '+918041511639'),
    ('Arrow', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970337, 77.609576, 'Clothing', 'free', 3.5, 62, '+918041658804'),
    ('Estee Lauder', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970311, 77.609374, 'Clothing', 'silver', 3.7, 89, '+918040698857'),
    ('ShopperStop', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970218, 77.609962, 'Clothing', 'silver', 3.6, 221, '+918043401317'),
    ('Mothercare', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970177, 77.609705, 'Toys & Baby', 'bronze', 3.5, 55, '+918040698857'),
    ('Mustard', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970142, 77.609903, 'Clothing', 'free', 3.1, 19, '+918041113391'),
    ('SteveMaden', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970161, 77.609841, 'Clothing', 'silver', 3.5, 258, '+918040698857'),
    ('Forever', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970122, 77.609514, 'Clothing', 'bronze', 3.5, 35, '+918041104473'),
    ('Swarovski', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970263, 77.609399, 'Clothing', 'free', 3.1, 74, '+918066641088'),
    ('U.S. Polo Assn.', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970137, 77.60964, 'Clothing', 'gold', 3.7, 1903, '+918040698857'),
    ('Adidas', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970072, 77.609501, 'Sports', 'gold', 4.5, 879, '+918041712312'),
    ('Allen Solly', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970111, 77.609651, 'Clothing', 'bronze', 3.9, 96, '+918041076734'),
    ('Women(POI)', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970237, 77.609453, 'Clothing', 'silver', 4.3, 477, '+918040698857'),
    ('Chemistry', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970216, 77.609592, 'Clothing', 'bronze', 3.7, 159, '+918040698857'),
    ('Fabindia', 'Garuda Mall, Magrath Road, MG Road, Bangalore', 12.970306, 77.60975, 'Clothing', 'silver', 4.2, 162, '+918032216151'),
    ('Enamor', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.97033, 77.609467, 'Clothing', 'bronze', 3.3, 144, '+918041126141'),
    ('Forever New', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970247, 77.609767, 'Clothing', 'free', 3.0, 51, '+918041126994'),
    ('Health & Glow', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970254, 77.609498, 'Beauty & Personal Care', 'silver', 4.2, 115, '+91180030008866'),
    ('Mint(POI)', 'Ashoknagar, Magrath Road, MG Road, Bangalore', 12.970281, 77.609714, 'Clothing', 'platinum', 4.5, 3727, '+918041239142'),
    ('J S Homoeo House', 'Near Bowring Hospital , hospital Road , 22/2, Central St, Shivaji Nagar, MG Road, Bangalore', 12.982224, 77.602928, 'Health & Wellness', 'silver', 4.1, 92, '+919880246426'),
    ('V L Nathan & Co', '33, Quadrant Road, Sulthangunta, Shivaji Nagar, MG Road, Bangalore', 12.985581, 77.607109, 'Health & Wellness', 'silver', 3.5, 286, '+918025512993'),
    ('Nathans Vet Pharma Centre', '32, Quadrant Road, Shivaji Nagar, Near Taj Hotel, Shanthala Nagar, Ashok Nagar, MG Road, Bangalore', 12.976282, 77.599102, 'Health & Wellness', 'free', 3.0, 41, '+918022861740'),
    ('Mahaveer Ayur & Homoeo', '23, Opposite Bowring Hospital, Lady Curzon Rd, Tasker Town, Shivaji Nagar, MG Road, Bangalore', 12.981332, 77.604106, 'Health & Wellness', 'free', 3.0, 48, '+918025593299'),
    ('Balaji Vaidyasala', '4/5, Central Square Complex, 47, Central Street, Tasker Town, Shivaji Nagar, Tasker Town, Shivaji Nagar, MG Road, Bangalore', 12.98138, 77.602996, 'Health & Wellness', 'free', 3.7, 53, '+919845501642'),
    ('Medinova', '55, Infantry Road, Shivaji Nagar, MG Road, Bangalore', 12.981604, 77.601106, 'Health & Wellness', 'free', 2.8, 2, '+918022868423'),
    ('Sundeep Medicals', '137, Brigade Road, Richmond Town, Shanthala Nagar, Ashok Nagar, Shanthala Nagar, Ashok Nagar, MG Road, Bangalore', 12.967421, 77.605845, 'Health & Wellness', 'silver', 3.5, 229, '+918022212968'),
    ('Hitesh Medicals & General Stor', 'C007, Ashok Nagar, Bangalore', 12.971362, 77.606244, 'Health & Wellness', 'free', 2.8, 5, '+918025584302'),
    ('The Grand Taj', 'Lady Curzon Road,Shivajinagar, MG Road, Bangalore', 12.981442, 77.603013, 'Food & Grocery', 'silver', 4.3, 152, '+918025591838'),
    ('K. V.', '156, Veerapillai Street,Shivajinagar, MG Road, Bangalore', 12.983648, 77.609005, 'Food & Grocery', 'gold', 4.3, 588, '+919886079972'),
    ('Impering', '42/5, Near Bowring Hospital,, Central Street, Shivaji Nagar, MG Road, Bangalore', 12.982148, 77.603223, 'Food & Grocery', 'free', 3.4, 72, '+918022863639'),
    ('Canteen', 'Museum Road, MG Road, Bangalore', 12.971216, 77.603808, 'Food & Grocery', 'gold', 4.2, 624, ''),
    ('Meraki Spa And Boutique', 'no:8, Pappana Street, MG Road, Bangalore', 12.97124, 77.600457, 'Beauty & Personal Care', 'silver', 3.6, 360, '+919611733222'),
    ('Swan And Beyonce Unisex Salon', 'NO.30, Rest House Road, MG Road, Bangalore', 12.973926, 77.60686, 'Beauty & Personal Care', 'platinum', 4.7, 1841, '+918033642234'),
    ('Rakhra Sports', '22, Haridwar Plaza, Opposite West side, Kamaraj Rd, Bhadrappa Layout, Tasker Town, Shivaji Nagar, MG Road, Bangalore', 12.982893, 77.609985, 'Sports', 'platinum', 4.1, 710, '+918025588776'),
    ('Sporting Goods Shop', '15, Dispensary Rd, Bhadrappa Layout, Tasker Town, Shivaji Nagar, MG Road, Bangalore', 12.982594, 77.605798, 'Sports', 'gold', 4.3, 1959, ''),
    ('Acme Fitness', 'Shop No. 17 & 18, Devatha Plaza, Residency Road, Opp. Bishop Cotton Boys School, Ashok Nagar, MG Road, Bangalore', 12.969102, 77.599782, 'Sports', 'platinum', 4.4, 2124, '+918041125656'),
    ('South Indian Sports Industries', '300, Vishnu Complex, Near-Meg Centre, Kamaraj Road, MG Road, Bangalore', 12.98111, 77.6099, 'Sports', 'free', 3.4, 80, ''),
    ('Twenty Twenty sports', '44, Ground Floor, 80/1 Shrungar Shopping Center,, MG Road, Shanthala Nagar, Ashok Nagar,, MG Road, Bangalore', 12.975418, 77.605498, 'Sports', 'free', 3.4, 38, '+919845414162'),
    ('O2 Spa', 'No.5, Vittal Mallya Road, MG Road, Bangalore', 12.970624, 77.599082, 'Beauty & Personal Care', 'gold', 4.3, 347, '+919902331966'),
    ('Flicks Salon And Spa', 'Garuda Mall, Ashok Nagar, MG Road, Bangalore', 12.969247, 77.610129, 'Beauty & Personal Care', 'silver', 4.0, 71, '+918041272304'),
    ('Texs Mart', 'MG Road, Bangalore', 12.982362, 77.609783, 'Clothing', 'bronze', 3.4, 114, ''),
    ('Occaision', 'MG Road, Bangalore', 12.982563, 77.607783, 'Clothing', 'free', 3.3, 1, ''),
    ('Sagar Chats', 'MG Road, Bangalore', 12.982767, 77.608801, 'Food & Grocery', 'gold', 4.5, 316, ''),
    ('Woodys', 'MG Road, Bangalore', 12.98245, 77.610231, 'Food & Grocery', 'bronze', 3.4, 137, ''),
    ('Gopalan Corner', 'MG Road, Bangalore', 12.982318, 77.60995, 'Clothing', 'silver', 3.8, 425, ''),
    ('Kopas Shack', 'MG Road, Bangalore', 12.973118, 77.603338, 'Food & Grocery', 'bronze', 3.7, 177, ''),
    ('Burger King', 'MG Road, Bangalore', 12.983186, 77.607124, 'Food & Grocery', 'silver', 4.2, 325, ''),
    ('ethik', 'MG Road, Bangalore', 12.97543, 77.603683, 'Clothing', 'silver', 3.9, 472, ''),
    ('Krispy Kreme', 'MG Road, Bangalore', 12.966801, 77.611446, 'Food & Grocery', 'silver', 3.7, 486, '+918041254402'),
    ('Taj Hotel', 'MG Road, Bangalore', 12.98531, 77.606988, 'Food & Grocery', 'free', 3.6, 35, ''),
    ('Shri Venkateshwara Motor Works', 'MG Road, Bangalore', 12.969023, 77.606442, 'Automotive', 'gold', 3.9, 1051, ''),
    ("Brahmin's Veg Restaurant", 'MG Road, Bangalore', 12.969587, 77.60636, 'Food & Grocery', 'silver', 4.1, 222, ''),
    ('Snack dine meet', 'MG Road, Bangalore', 12.97074, 77.607081, 'Food & Grocery', 'free', 3.6, 23, ''),
    ('Brigade Prince', 'MG Road, Bangalore', 12.968965, 77.606439, 'Food & Grocery', 'bronze', 3.4, 86, ''),
    ('Laludas', 'MG Road, Bangalore', 12.968924, 77.606423, 'Clothing', 'bronze', 3.9, 199, ''),
    ('The Grill house', 'MG Road, Bangalore', 12.969092, 77.606184, 'Food & Grocery', 'gold', 4.3, 1158, ''),
    ('Shangri-la Plaza', 'MG Road, Bangalore', 12.972895, 77.607099, 'Food & Grocery', 'gold', 3.8, 1574, ''),
    ('Nexa', 'MG Road, Bangalore', 12.973051, 77.602549, 'Automotive', 'bronze', 3.6, 81, ''),
    ("Rama's", 'MG Road, Bangalore', 12.981765, 77.609343, 'Clothing', 'silver', 4.0, 301, ''),
    ('Ritikas', 'MG Road, Bangalore', 12.981925, 77.609364, 'Clothing', 'bronze', 3.0, 95, ''),
    ('Trendz shoes', 'MG Road, Bangalore', 12.981775, 77.609303, 'Clothing', 'free', 3.4, 39, ''),
    ('Rocia', 'MG Road, Bangalore', 12.981865, 77.609106, 'Clothing', 'silver', 3.8, 333, ''),
    ('Van Heusen', 'MG Road, Bangalore', 12.982428, 77.607444, 'Clothing', 'bronze', 3.4, 160, ''),
    ('Poshaak', 'MG Road, Bangalore', 12.98241, 77.607484, 'Clothing', 'bronze', 3.7, 89, ''),
    ('Bollywood', 'MG Road, Bangalore', 12.981754, 77.609451, 'Clothing', 'bronze', 3.2, 69, ''),
    ("Western Store's", 'MG Road, Bangalore', 12.981819, 77.609504, 'Clothing', 'bronze', 3.7, 196, ''),
    ('Blackberrys', 'MG Road, Bangalore', 12.982342, 77.607818, 'Clothing', 'free', 2.9, 61, ''),
    ("Children's corner", 'MG Road, Bangalore', 12.981881, 77.609466, 'Clothing', 'free', 3.3, 67, ''),
    ('Favourite shop', 'MG Road, Bangalore', 12.981799, 77.609244, 'Clothing', 'silver', 4.4, 476, ''),
    ('Jewel', 'MG Road, Bangalore', 12.982292, 77.608295, 'Clothing', 'free', 2.9, 22, ''),
    ('Brown shop', 'MG Road, Bangalore', 12.981768, 77.609426, 'Clothing', 'bronze', 3.7, 52, ''),
    ('Meena bazaar', 'MG Road, Bangalore', 12.982427, 77.607496, 'Clothing', 'free', 3.7, 70, ''),
    ('Driven Cafe', 'MG Road, Bangalore', 12.972908, 77.608499, 'Food & Grocery', 'free', 3.6, 62, ''),
    ('Marzipan Cafe', 'Halasuru Road, Ulsoor, Bangalore', 12.975662, 77.617092, 'Food & Grocery', 'free', 2.7, 36, '+919844422724'),
    ('Fablore', 'MG Road, Bangalore', 12.981053, 77.607681, 'Clothing', 'bronze', 3.4, 67, ''),
    ('Dulhan', 'MG Road, Bangalore', 12.98242, 77.607876, 'Clothing', 'platinum', 4.3, 4413, ''),
    ('Vero Moda', 'MG Road, Bangalore', 12.981759, 77.609882, 'Clothing', 'free', 2.8, 62, ''),
    ('Wearhouse', 'MG Road, Bangalore', 12.981824, 77.607954, 'Clothing', 'free', 3.3, 6, ''),
    ('MS kidswear', 'MG Road, Bangalore', 12.981236, 77.607741, 'Clothing', 'free', 3.5, 38, ''),
    ('Melange', 'MG Road, Bangalore', 12.982576, 77.607457, 'Clothing', 'free', 2.9, 71, ''),
    ('Kyara', 'MG Road, Bangalore', 12.982389, 77.607583, 'Clothing', 'silver', 4.0, 454, ''),
    ('Anglo American Optical Center', 'MG Road, Bangalore', 12.982581, 77.607423, 'Health & Wellness', 'silver', 4.2, 244, ''),
    ('Woodlands', 'MG Road, Bangalore', 12.982176, 77.60848, 'Clothing', 'bronze', 3.4, 170, ''),
    ('Tasva', 'MG Road, Bangalore', 12.981994, 77.608714, 'Clothing', 'platinum', 4.3, 993, ''),
    ('Brigade Garden', 'MG Road, Bangalore', 12.97314, 77.607456, 'Food & Grocery', 'silver', 4.1, 440, ''),
    ('Style Hunt', 'MG Road, Bangalore', 12.982031, 77.608229, 'Clothing', 'free', 2.9, 10, ''),
    ('Zenith', 'MG Road, Bangalore', 12.982519, 77.607659, 'Clothing', 'free', 2.9, 9, ''),
    ('Sri Balaji exports', 'MG Road, Bangalore', 12.982345, 77.608156, 'Clothing', 'free', 3.1, 76, ''),
    ('Metro', 'MG Road, Bangalore', 12.981394, 77.610395, 'Clothing', 'bronze', 3.0, 93, ''),
    ('Converse', 'MG Road, Bangalore', 12.972953, 77.60741, 'Clothing', 'silver', 4.1, 282, ''),
    ('Blackberry', 'MG Road, Bangalore', 12.982471, 77.60774, 'Clothing', 'free', 2.9, 33, ''),
    ('Show off', 'MG Road, Bangalore', 12.982421, 77.607519, 'Clothing', 'silver', 4.0, 388, ''),
    ('Qualified opticians', 'MG Road, Bangalore', 12.98248, 77.607849, 'Health & Wellness', 'gold', 3.9, 335, ''),
    ('Mann', 'MG Road, Bangalore', 12.98173, 77.60956, 'Clothing', 'bronze', 3.6, 88, ''),
    ('Golden touch', 'MG Road, Bangalore', 12.981708, 77.610081, 'Clothing', 'gold', 3.8, 439, ''),
    ('Ramson', 'MG Road, Bangalore', 12.981202, 77.610202, 'Health & Wellness', 'silver', 4.0, 472, ''),
    ('Audi', 'MG Road, Bangalore', 12.970089, 77.600475, 'Automotive', 'silver', 3.7, 113, ''),
    ('Bose', 'Mahatma Gandhi Road, MG Road, Bangalore', 12.974736, 77.608315, 'Electronics', 'bronze', 3.3, 123, ''),
    ('Hosmat Pharmacy', 'MG Road, Bangalore', 12.967146, 77.610396, 'Health & Wellness', 'gold', 4.2, 1111, ''),
    ('Samyakk', 'MG Road, Bangalore', 12.966864, 77.611179, 'Clothing', 'bronze', 3.6, 130, ''),
    ('Nalli Next', 'MG Road, Bangalore', 12.970393, 77.610679, 'Clothing', 'silver', 4.0, 63, ''),
    ('BMW Showroom', 'MG Road, Bangalore', 12.972959, 77.599474, 'Automotive', 'free', 3.7, 73, ''),
    ('Salvadores', 'MG Road, Bangalore', 12.973506, 77.611315, 'Food & Grocery', 'silver', 3.4, 441, '+918049653492'),
    ('Portland', 'MG Road, Bangalore', 12.970226, 77.614658, 'Food & Grocery', 'silver', 3.7, 70, ''),
    ('eat.fit', 'MG Road, Bangalore', 12.975122, 77.604007, 'Food & Grocery', 'silver', 3.6, 315, ''),
    ('Brik Oven', 'MG Road, Bangalore', 12.974726, 77.60532, 'Food & Grocery', 'silver', 4.3, 142, ''),
    ('Lacoste', 'MG Road, Bangalore', 12.974667, 77.606948, 'Clothing', 'platinum', 4.4, 4528, ''),
    ('Ilahui', 'MG Road, Bangalore', 12.97454, 77.607412, 'Beauty & Personal Care', 'platinum', 4.5, 3845, ''),
    ('Coast II Coast', 'MG Road, Bangalore', 12.974625, 77.607377, 'Food & Grocery', 'bronze', 3.7, 61, ''),
    ('Skechers', 'MG Road, Bangalore', 12.974612, 77.607899, 'Clothing', 'bronze', 3.7, 93, ''),
    ('Mochi', 'MG Road, Bangalore', 12.974562, 77.607885, 'Clothing', 'silver', 3.8, 439, ''),
    ('Jockey', 'MG Road, Bangalore', 12.974487, 77.607867, 'Clothing', 'silver', 3.9, 211, ''),
    ('Puma', 'MG Road, Bangalore', 12.97469, 77.607933, 'Clothing', 'gold', 4.0, 337, ''),
    ('saatvikk', 'MG Road, Bangalore', 12.974364, 77.607851, 'Food & Grocery', 'platinum', 4.4, 4715, ''),
    ('Raymonds', 'MG Road, Bangalore', 12.97431, 77.607835, 'Clothing', 'gold', 3.7, 1880, ''),
    ('Indian Terrain', 'MG Road, Bangalore', 12.973991, 77.607728, 'Clothing', 'bronze', 3.4, 68, ''),
    ('Soles', 'MG Road, Bangalore', 12.973897, 77.607693, 'Clothing', 'bronze', 3.6, 147, ''),
    ('Ruosh', 'MG Road, Bangalore', 12.97393, 77.607699, 'Clothing', 'silver', 4.2, 154, ''),
    ('Spykar', 'MG Road, Bangalore', 12.973691, 77.607634, 'Clothing', 'free', 2.8, 36, ''),
    ('Florsheim', 'MG Road, Bangalore', 12.973179, 77.607453, 'Clothing', 'bronze', 3.7, 51, ''),
    ('Hush Puppies', 'MG Road, Bangalore', 12.972816, 77.60744, 'Clothing', 'free', 3.3, 30, ''),
    ('Tea Trails', 'MG Road, Bangalore', 12.97293, 77.607477, 'Food & Grocery', 'silver', 3.7, 57, ''),
    ('Frozen Bottle', 'MG Road, Bangalore', 12.972829, 77.607387, 'Food & Grocery', 'silver', 3.5, 480, ''),
    ('Brigade Foot Wear', 'MG Road, Bangalore', 12.9718, 77.606549, 'Clothing', 'gold', 4.2, 340, ''),
    ('Fee Chu Beauty Parlour', 'C009, Field Marshal Cariappa Road, Ashok Nagar, Bangalore', 12.971368, 77.606179, 'Beauty & Personal Care', 'silver', 3.6, 416, ''),
    ('Decathlon', 'MG Road, Bangalore', 12.970918, 77.607163, 'Sports', 'platinum', 4.3, 2739, ''),
    ("Cafe Mor'ish", 'MG Road, Bangalore', 12.965936, 77.60357, 'Food & Grocery', 'bronze', 3.5, 140, ''),
    ('Bamburies', 'MG Road, Bangalore', 12.96672, 77.608296, 'Food & Grocery', 'free', 3.3, 49, ''),
    ('Fatima Bakery', 'MG Road, Bangalore', 12.966891, 77.609325, 'Food & Grocery', 'free', 3.3, 17, ''),
    ('ASEAN - On The Edge', '84, Mahatma Gandhi Road, MG Road, Bangalore', 12.975465, 77.605062, 'Food & Grocery', 'gold', 4.0, 1717, '+918025136611'),
    ('Deepam', 'MG Road, Bangalore', 12.975139, 77.607098, 'Clothing', 'gold', 4.0, 1713, ''),
    ('Green Theory', '15, Convent Road, Indiranagar, Bangalore', 12.96835, 77.602715, 'Food & Grocery', 'bronze', 3.8, 92, '+918861435956'),
    ('Tinge Custom Makeup Studio', '17, Hanumanthappa Layout Rd, Hanumanthappa Layout, Sivanchetti Gardens, Halasuru, Karnataka 560042, MG Road, Bangalore', 12.977147, 77.617263, 'Food & Grocery', 'silver', 4.2, 346, ''),
    ("Yak's Kitchen", 'MG Road, Bangalore', 12.973842, 77.606845, 'Food & Grocery', 'platinum', 4.5, 1717, ''),
    ('Mandarin Box', 'MG Road, Bangalore', 12.9753, 77.603196, 'Food & Grocery', 'bronze', 3.5, 105, ''),
    ('Burger Seigneur', 'Church Street, MG Road, Bangalore', 12.975217, 77.602497, 'Food & Grocery', 'silver', 3.9, 283, ''),
    ('Pākaśāla', '46, Mahatma Gandhi Road, MG Road, Bangalore', 12.974733, 77.608387, 'Food & Grocery', 'gold', 4.6, 1528, '+919380909958'),
    ('Indira Canteen', 'MG Road, Bangalore', 12.968707, 77.610477, 'Food & Grocery', 'free', 3.1, 52, ''),
    ('DCR Cafe & Restaurant', 'MG Road, Bangalore', 12.976586, 77.602628, 'Food & Grocery', 'free', 3.4, 48, ''),
    ('Cafe Azzure', 'MG Road, Bangalore', 12.97501, 77.607473, 'Food & Grocery', 'bronze', 3.7, 186, ''),
    ('Amintri Cafe and Pastries', 'MG Road, Bangalore', 12.973722, 77.603247, 'Food & Grocery', 'platinum', 4.5, 803, ''),
    ("Koshy's Bakery", 'MG Road, Bangalore', 12.9654, 77.604808, 'Food & Grocery', 'free', 3.7, 42, ''),
    ('All Saints Bakery', 'MG Road, Bangalore', 12.966867, 77.606789, 'Food & Grocery', 'gold', 4.6, 1001, ''),
    ('Bombay Borough', 'MG Road, Bangalore', 12.966912, 77.60818, 'Food & Grocery', 'free', 3.6, 1, ''),
    ('Theobroma', 'MG Road, Bangalore', 12.971297, 77.59757, 'Food & Grocery', 'silver', 3.8, 385, ''),
    ('Lavonne Cafe', 'MG Road, Bangalore', 12.969507, 77.600352, 'Food & Grocery', 'platinum', 4.1, 2671, ''),
    ('Brassa', 'MG Road, Bangalore', 12.97052, 77.600639, 'Food & Grocery', 'bronze', 3.7, 186, ''),
    ('The Bookhive', 'MG Road, Bangalore', 12.974672, 77.606802, 'Books', 'free', 3.0, 68, ''),
    ("Goobe's Books", 'Church Street, MG Road, Bangalore', 12.975814, 77.602239, 'Books', 'bronze', 3.3, 144, ''),
    ('The Bookworm', '62, Church Street, MG Road, Bangalore', 12.974215, 77.605596, 'Books', 'gold', 3.7, 240, '+919845076757'),
    ('Raahi Neo Kitchen and Bar', 'MG Road, Bangalore', 12.973109, 77.602346, 'Food & Grocery', 'free', 3.4, 36, ''),
    ('Luckhnow Street', 'MG Road, Bangalore', 12.97585, 77.601898, 'Food & Grocery', 'platinum', 4.7, 4055, ''),
    ('Forest Essentials', 'MG Road, Bangalore', 12.971689, 77.598056, 'Beauty & Personal Care', 'platinum', 4.8, 1323, ''),
    ('Royanna Military Canteen', 'MG Road, Bangalore', 12.971294, 77.606515, 'Food & Grocery', 'bronze', 3.7, 27, ''),
    ('Utse Kitchen', 'MG Road, Bangalore', 12.984898, 77.611694, 'Food & Grocery', 'free', 3.5, 37, ''),
    ('BN Sambaiah Shetty Tiffin Room', 'MG Road, Bangalore', 12.984851, 77.609387, 'Food & Grocery', 'bronze', 3.4, 82, ''),
    ('Maverick and Farmer Coffee', 'MG Road, Bangalore', 12.981157, 77.614152, 'Food & Grocery', 'bronze', 3.6, 66, ''),
    ('Boteco - Restaurante Brasileiro', 'Mahatma Gandhi Road, MG Road, Bangalore', 12.973351, 77.614477, 'Food & Grocery', 'free', 2.8, 48, '+918792045444'),
    ('Ground Up Coffee', 'MG Road, Bangalore', 12.968292, 77.602754, 'Food & Grocery', 'silver', 3.6, 348, ''),
    ('Under Armour', 'MG Road, Bangalore', 12.974266, 77.60756, 'Clothing', 'free', 3.2, 32, ''),
    ('Just BLR', 'MG Road, Bangalore', 12.97328, 77.607258, 'Food & Grocery', 'bronze', 3.7, 139, ''),
    ('Brooks Running', "prestige delta, Saint Mark's Road, MG Road, Bangalore", 12.968412, 77.600474, 'Sports', 'gold', 4.4, 423, '+919845875026'),
    ('Cinnamon', '24, Gangadhar Chetty Road, MG Road, Bangalore', 12.981438, 77.612665, 'Clothing', 'free', 3.6, 75, '+91(+91)9972813015'),
    ('Cafe Stone', 'MG Road, Bangalore', 12.98131, 77.612613, 'Food & Grocery', 'free', 3.7, 32, ''),
    ('Family Shopping Mart', 'MG Road, Bangalore', 12.971867, 77.614955, 'Food & Grocery', 'bronze', 3.3, 118, ''),
    ('Varana', 'MG Road, Bangalore', 12.971866, 77.614475, 'Clothing', 'gold', 3.8, 881, ''),
    ('Suzuki', 'MG Road, Bangalore', 12.981875, 77.600086, 'Automotive', 'silver', 4.0, 408, ''),
    ('Blue Tokai', '36, Infantry Road, MG Road, Bangalore', 12.981152, 77.603212, 'Food & Grocery', 'free', 3.7, 50, ''),
    ('Hamza Hotel', 'MG Road, Bangalore', 12.986104, 77.604839, 'Food & Grocery', 'free', 3.3, 6, ''),
    ('Bilal Bakery', 'MG Road, Bangalore', 12.984955, 77.602247, 'Food & Grocery', 'gold', 4.4, 1805, ''),
    ('Vee Bake', 'MG Road, Bangalore', 12.983952, 77.608261, 'Food & Grocery', 'gold', 4.0, 1736, ''),
    ('Paakashala', 'MG Road, Bangalore', 12.976019, 77.614801, 'Food & Grocery', 'free', 3.5, 28, ''),
    ('Artzo', 'MG Road, Bangalore', 12.975746, 77.602259, 'Stationery', 'silver', 4.0, 397, ''),
    ('Zed The Baker', 'MG Road, Bangalore', 12.976001, 77.602614, 'Food & Grocery', 'silver', 4.2, 398, ''),
    ('Anand Sweets and Savouries', 'Commercial Street, MG Road, Bangalore', 12.981624, 77.609781, 'Food & Grocery', 'gold', 4.3, 1950, ''),
    ('Zudio', 'MG Road, Bangalore', 12.982743, 77.606614, 'Clothing', 'silver', 3.4, 453, ''),
    ('Nysha', 'MG Road, Bangalore', 12.982904, 77.606588, 'Clothing', 'bronze', 3.6, 53, ''),
    ('SR and Sons', 'MG Road, Bangalore', 12.983218, 77.606493, 'Food & Grocery', 'free', 3.0, 53, ''),
    ('Brandmart', 'MG Road, Bangalore', 12.982869, 77.606669, 'Clothing', 'gold', 3.9, 370, ''),
    ('Lenskart', 'MG Road, Bangalore', 12.9827, 77.606714, 'Health & Wellness', 'silver', 4.3, 237, ''),
    ('Y&O', 'MG Road, Bangalore', 12.982609, 77.606924, 'Clothing', 'bronze', 3.9, 62, ''),
    ('Louise Phillipe', 'MG Road, Bangalore', 12.982634, 77.607308, 'Clothing', 'free', 3.5, 61, ''),
    ('Peter England', 'MG Road, Bangalore', 12.982403, 77.60756, 'Clothing', 'platinum', 4.3, 3274, ''),
    ('J Emporio', 'MG Road, Bangalore', 12.982306, 77.608265, 'Clothing', 'gold', 4.2, 254, ''),
    ('Volkswagen', 'MG Road, Bangalore', 12.966236, 77.60569, 'Automotive', 'free', 3.7, 50, ''),
    ('Veebake', 'MG Road, Bangalore', 12.968759, 77.601299, 'Food & Grocery', 'platinum', 4.8, 3496, ''),
    ('IDC', 'MG Road, Bangalore', 12.968722, 77.601235, 'Food & Grocery', 'free', 3.1, 33, ''),
    ('Luknow chikan kari', 'MG Road, Bangalore', 12.982472, 77.609814, 'Beauty & Personal Care', 'bronze', 3.5, 192, ''),
    ('Star Bazaar', '47/48, Residency Road, MG Road, Bangalore', 12.973432, 77.611285, 'Food & Grocery', 'silver', 3.7, 245, ''),
    ('OnePlus Experience Store', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972538, 77.606724, 'Electronics', 'gold', 4.6, 321, ''),
    ('Nandini Milk Parlour', 'MG Road, Bangalore', 12.973024, 77.605416, 'Food & Grocery', 'silver', 3.9, 367, ''),
    ('Third Wave Coffee Roasters', 'MG Road, Bangalore', 12.974683, 77.606711, 'Food & Grocery', 'gold', 4.3, 1974, ''),
    ('The Pizza Bakery', 'MG Road, Bangalore', 12.975247, 77.604584, 'Food & Grocery', 'silver', 4.0, 375, ''),
    ('Pit Stop', 'MG Road, Bangalore', 12.975399, 77.603619, 'Food & Grocery', 'gold', 4.6, 718, ''),
    ('Cricket Den', 'MG Road, Bangalore', 12.981067, 77.613906, 'Sports', 'silver', 3.5, 73, ''),
    ('Kamat Vegetarian Restaurant', '495/496, Jumma Masjid Road, MG Road, Bangalore', 12.981913, 77.605754, 'Food & Grocery', 'gold', 4.0, 1108, '+919353346114'),
    ('Silk Garden', 'MG Road, Bangalore', 12.981507, 77.605882, 'Clothing', 'free', 2.9, 68, ''),
    ('Sony Heritage', 'MG Road, Bangalore', 12.981236, 77.605997, 'Clothing', 'free', 3.2, 69, ''),
    ('Virinchi Cafe', 'MG Road, Bangalore', 12.968854, 77.601449, 'Food & Grocery', 'bronze', 3.7, 59, ''),
    ('Fuga', 'MG Road, Bangalore', 12.968808, 77.60137, 'Food & Grocery', 'gold', 4.4, 1807, ''),
    ('One Stop Party Shop', 'MG Road, Bangalore', 12.980345, 77.604812, 'Toys & Baby', 'bronze', 3.4, 91, ''),
    ('Mega Brands', 'MG Road, Bangalore', 12.980437, 77.604611, 'Clothing', 'free', 3.1, 56, ''),
    ('Jain Medicals', 'MG Road, Bangalore', 12.981878, 77.604078, 'Health & Wellness', 'bronze', 3.2, 112, ''),
    ('Hotel Swapna Sagara', 'MG Road, Bangalore', 12.982037, 77.603266, 'Food & Grocery', 'free', 3.4, 69, ''),
    ('SR Bakery & Fast Food', 'MG Road, Bangalore', 12.981902, 77.603968, 'Food & Grocery', 'gold', 4.3, 224, ''),
    ('Delistic', 'MG Road, Bangalore', 12.968848, 77.600275, 'Food & Grocery', 'bronze', 3.2, 51, ''),
    ('Sapphire Toys', 'MG Road, Bangalore', 12.966814, 77.610898, 'Toys & Baby', 'gold', 4.5, 287, ''),
    ('Kurta Planet', 'MG Road, Bangalore', 12.983042, 77.60834, 'Clothing', 'silver', 4.0, 355, ''),
    ('Dupatta Centre', 'MG Road, Bangalore', 12.982895, 77.608384, 'Clothing', 'platinum', 4.1, 3232, ''),
    ("Karnataka State Police Officers' Mess", 'MG Road, Bangalore', 12.971525, 77.608854, 'Food & Grocery', 'free', 2.8, 72, ''),
    ('Cantan', 'MG Road, Bangalore', 12.970601, 77.597521, 'Food & Grocery', 'free', 3.5, 70, ''),
    ('Ritu Kumar', 'MG Road, Bangalore', 12.970151, 77.597715, 'Clothing', 'free', 2.9, 29, ''),
    ('Kuuraku', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972793, 77.606745, 'Food & Grocery', 'bronze', 3.1, 172, ''),
    ('Sly Granny', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972689, 77.607019, 'Food & Grocery', 'free', 3.6, 18, ''),
    ('Dhaba - Estd 1986 Delhi', '202, Brigade Road, Ashok Nagar, Bangalore', 12.97266, 77.607009, 'Food & Grocery', 'platinum', 4.5, 1929, '+919555805296'),
    ('Cafe Delhi Heights', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972679, 77.606957, 'Food & Grocery', 'free', 3.6, 16, ''),
    ('Foo', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972938, 77.606404, 'Food & Grocery', 'free', 3.5, 30, '+919321707545'),
    ('Burma Burma', '202, Brigade Road, Ashok Nagar, Bangalore', 12.973003, 77.606137, 'Food & Grocery', 'silver', 3.4, 185, ''),
    ("T'ART Cafe", '202, Brigade Road, Ashok Nagar, Bangalore', 12.972791, 77.606667, 'Food & Grocery', 'free', 3.4, 67, '+919380357634'),
    ('Punjab Grill', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972984, 77.606417, 'Food & Grocery', 'free', 2.8, 57, ''),
    ("Nando's", '202, Brigade Road, Ashok Nagar, Bangalore', 12.97292, 77.606478, 'Food & Grocery', 'silver', 3.9, 105, ''),
    ('Forster Pharma', 'MG Road, Bangalore', 12.972088, 77.60094, 'Health & Wellness', 'bronze', 3.2, 177, ''),
    ('Truffles', 'MG Road, Bangalore', 12.971474, 77.600831, 'Food & Grocery', 'free', 3.5, 66, ''),
    ('Raymond', 'MG Road, Bangalore', 12.970862, 77.598392, 'Clothing', 'bronze', 3.6, 27, ''),
    ('Bombay Shirt Company', '25/5, Lavelle Road, Ashok Nagar, Bangalore', 12.971918, 77.598199, 'Clothing', 'free', 3.2, 51, '+9108043743309'),
    ('Nykaa Luxe', 'MG Road, Bangalore', 12.971667, 77.598363, 'Beauty & Personal Care', 'bronze', 3.1, 133, ''),
    ("Chick Bun's", 'B014, Brigade Road, Ashok Nagar, Bangalore', 12.970699, 77.606655, 'Food & Grocery', 'free', 2.8, 77, ''),
    ("Simpli Namdhari's", 'MG Road, Bangalore', 12.969941, 77.609707, 'Food & Grocery', 'free', 2.8, 79, ''),
    ('Misu', 'MG Road, Bangalore', 12.970645, 77.60066, 'Food & Grocery', 'silver', 3.9, 216, ''),
    ('No. 10 Fort Cochin', "Saint Mark's Road, MG Road, Bangalore", 12.970673, 77.600667, 'Food & Grocery', 'bronze', 3.6, 95, '+917777876000'),
    ('The Konkan', 'MG Road, Bangalore', 12.97593, 77.601802, 'Food & Grocery', 'bronze', 3.6, 45, ''),
    ('Nandhini Deluxe', 'MG Road, Bangalore', 12.971816, 77.600895, 'Food & Grocery', 'silver', 3.5, 385, ''),
    ('SLAY Coffee', 'MG Road, Bangalore', 12.971403, 77.600816, 'Food & Grocery', 'silver', 4.2, 419, ''),
    ('Louis Philippe', 'MG Road, Bangalore', 12.970575, 77.600656, 'Clothing', 'gold', 4.1, 1918, ''),
    ('Jaisalmer', 'MG Road, Bangalore', 12.971113, 77.600618, 'Food & Grocery', 'free', 3.0, 58, ''),
    ('Lamborghini', 'MG Road, Bangalore', 12.976101, 77.599369, 'Automotive', 'bronze', 3.7, 100, ''),
    ('Blini Bistro', 'MG Road, Bangalore', 12.975061, 77.604032, 'Food & Grocery', 'bronze', 3.7, 115, ''),
    ('Nasi and Mee', 'Convent Road, Ashok Nagar, Bangalore', 12.968672, 77.602599, 'Food & Grocery', 'platinum', 4.7, 4385, ''),
    ('Pride Cafe', 'MG Road, Bangalore', 12.973234, 77.608862, 'Food & Grocery', 'free', 3.5, 11, ''),
    ('LUPA', '86, MG Road, MG Road, Bangalore', 12.975598, 77.604579, 'Food & Grocery', 'bronze', 3.7, 115, ''),
    ('Avantra by Trends', 'MG Road, Bangalore', 12.98166, 77.609517, 'Clothing', 'gold', 4.3, 1301, ''),
    ("Fia's Lounge", 'Richmond Road, MG Road, Bangalore', 12.965841, 77.602809, 'Food & Grocery', 'platinum', 4.6, 1501, ''),
    ("O'ver Coffee", 'MG Road, Bangalore', 12.974959, 77.599563, 'Food & Grocery', 'bronze', 3.9, 128, ''),
    ('Karims', 'MG Road, Bangalore', 12.972187, 77.600873, 'Food & Grocery', 'gold', 4.4, 689, ''),
    ('MAP Cafe', 'MG Road, Bangalore', 12.974642, 77.596743, 'Food & Grocery', 'silver', 3.8, 345, ''),
    ('Naughtee', 'MG Road, Bangalore', 12.981895, 77.609863, 'Clothing', 'bronze', 3.2, 143, ''),
    ('Flying Machine', 'MG Road, Bangalore', 12.982325, 77.608242, 'Clothing', 'free', 2.8, 71, ''),
    ('Klava', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972746, 77.606786, 'Food & Grocery', 'bronze', 3.8, 166, ''),
    ('Truefitt & Hill', 'MG Road, Bangalore', 12.971617, 77.598416, 'Beauty & Personal Care', 'free', 3.1, 71, ''),
    ('Bath & Body Works', 'Ashok Nagar, Bangalore', 12.970061, 77.609892, 'Beauty & Personal Care', 'platinum', 4.8, 715, ''),
    ('Gopizza', 'MG Road, Bangalore', 12.970207, 77.610013, 'Food & Grocery', 'silver', 4.1, 405, ''),
    ('Ancestry', 'MG Road, Bangalore', 12.970053, 77.609771, 'Clothing', 'free', 2.9, 39, '+917304477926'),
    ('Canopy Restaurant', 'MG Road, Bangalore', 12.974458, 77.609943, 'Food & Grocery', 'gold', 4.1, 471, ''),
    ('Biggies Burger', 'MG Road, Bangalore', 12.975716, 77.602884, 'Food & Grocery', 'gold', 4.3, 920, ''),
    ('Daily Sushi', 'MG Road, Bangalore', 12.975963, 77.602613, 'Food & Grocery', 'free', 3.4, 3, ''),
    ('The Caffeine Baar', '46, Church Street, MG Road, Bangalore', 12.975761, 77.602407, 'Food & Grocery', 'free', 3.2, 48, ''),
    ('Suryawanshi', 'Church Street, MG Road, Bangalore', 12.975504, 77.601917, 'Food & Grocery', 'bronze', 3.3, 114, ''),
    ('Toni & Guy', 'Church Street, MG Road, Bangalore', 12.975315, 77.602461, 'Beauty & Personal Care', 'bronze', 3.3, 165, ''),
    ('Paragon', 'Church Street, MG Road, Bangalore', 12.97525, 77.602356, 'Food & Grocery', 'silver', 4.3, 129, ''),
    ('Chef Pillai', 'MG Road, Bangalore', 12.970697, 77.607135, 'Food & Grocery', 'free', 3.3, 10, ''),
    ("Brahmin's", 'MG Road, Bangalore', 12.969584, 77.60633, 'Food & Grocery', 'free', 3.4, 62, ''),
    ('Cumulus', 'MG Road, Bangalore', 12.974485, 77.596795, 'Food & Grocery', 'silver', 3.8, 160, '+919609996687'),
    ('Birkenstock', 'MG Road, Bangalore', 12.974712, 77.607961, 'Clothing', 'silver', 3.5, 270, ''),
    ('Thenga Manga', 'MG Road, Bangalore', 12.970624, 77.607264, 'Food & Grocery', 'free', 3.4, 75, ''),
    ('ASICS', 'MG Road, Bangalore', 12.974213, 77.607954, 'Clothing', 'bronze', 3.7, 31, ''),
    ('Crocs', 'MG Road, Bangalore', 12.974324, 77.607969, 'Clothing', 'free', 3.7, 7, ''),
    ('Qube Cafe', 'MG Road, Bangalore', 12.974807, 77.606276, 'Food & Grocery', 'free', 3.0, 27, ''),
    ('The Doner Company', 'Church Street, MG Road, Bangalore', 12.975455, 77.602147, 'Food & Grocery', 'silver', 4.2, 198, ''),
    ('Rolls Mania', 'Church Street, MG Road, Bangalore', 12.975475, 77.602054, 'Food & Grocery', 'bronze', 3.0, 130, ''),
    ('The Crepe Cafe', 'Church Street, MG Road, Bangalore', 12.97549, 77.60198, 'Food & Grocery', 'free', 3.1, 29, ''),
    ("Teju Fry'd", 'Church Street, MG Road, Bangalore', 12.975444, 77.602197, 'Food & Grocery', 'bronze', 3.8, 110, ''),
    ('Tadka Singh', 'Church Street, MG Road, Bangalore', 12.975465, 77.602099, 'Food & Grocery', 'free', 3.6, 5, ''),
    ('Shenzhen Kitchen', 'Church Street, MG Road, Bangalore', 12.975482, 77.60202, 'Food & Grocery', 'bronze', 3.5, 39, ''),
    ('Roomali', 'MG Road, Bangalore', 12.975208, 77.603183, 'Food & Grocery', 'gold', 4.3, 1274, ''),
    ('ZEST', 'MG Road, Bangalore', 12.974726, 77.59941, 'Food & Grocery', 'bronze', 3.6, 94, ''),
    ('HOPCOMS', 'MG Road, Bangalore', 12.973065, 77.605353, 'Food & Grocery', 'free', 2.7, 41, ''),
    ('Muro', 'MG Road, Bangalore', 12.97456, 77.603132, 'Food & Grocery', 'free', 3.6, 58, ''),
    ('22 Luna', 'MG Road, Bangalore', 12.974536, 77.604172, 'Beauty & Personal Care', 'gold', 4.6, 280, ''),
    ('Lime Light', 'MG Road, Bangalore', 12.976072, 77.60261, 'Food & Grocery', 'bronze', 3.1, 131, ''),
    ('Smoor', 'MG Road, Bangalore', 12.972872, 77.600275, 'Food & Grocery', 'silver', 3.9, 252, ''),
    ('Dishkeyaun', 'MG Road, Bangalore', 12.972754, 77.606849, 'Food & Grocery', 'gold', 4.4, 553, ''),
    ('Ebisu', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972821, 77.606676, 'Food & Grocery', 'bronze', 3.2, 150, '+917303743863'),
    ('Falafel Jee', 'MG Road, Bangalore', 12.9746, 77.607226, 'Food & Grocery', 'silver', 3.9, 149, ''),
    ('Rare Rabbit', 'MG Road, Bangalore', 12.974063, 77.607522, 'Clothing', 'gold', 4.0, 1589, ''),
    ('Louis Phillipe', 'MG Road, Bangalore', 12.973889, 77.60748, 'Clothing', 'platinum', 4.6, 1709, ''),
    ('American Corner', 'MG Road, Bangalore', 12.972283, 77.606796, 'Clothing', 'free', 2.8, 25, ''),
    ('Handicraft House Kashmiri Shawls', 'MG Road, Bangalore', 12.972315, 77.606681, 'Clothing', 'free', 2.9, 9, ''),
    ('Lucknowwala', 'MG Road, Bangalore', 12.972111, 77.606634, 'Clothing', 'free', 3.5, 63, ''),
    ('Cane City', 'A010, Brigade Road, Ashok Nagar, Bangalore', 12.971391, 77.606817, 'Food & Grocery', 'bronze', 3.6, 168, ''),
    ("Queen's Restaurant", 'MG Road, Bangalore', 12.974731, 77.606721, 'Food & Grocery', 'bronze', 3.9, 164, ''),
    ('Cafe Levista', 'MG Road, Bangalore', 12.975148, 77.606719, 'Food & Grocery', 'silver', 4.4, 215, ''),
    ('Vinaya Sāgar', 'MG Road, Bangalore', 12.983769, 77.608335, 'Food & Grocery', 'gold', 4.3, 409, '+919900644644'),
    ('Bangalore Thindies', 'MG Road, Bangalore', 12.981187, 77.604242, 'Food & Grocery', 'bronze', 3.5, 181, '+919591541023'),
    ('JCL', 'MG Road, Bangalore', 12.983305, 77.609984, 'Food & Grocery', 'bronze', 3.3, 34, '+918123112030'),
    ('Ouro', '171-172, Brigade Road, Ashok Nagar, Bangalore', 12.972775, 77.606803, 'Food & Grocery', 'bronze', 3.1, 138, '+918951738555'),
    ('bychance', 'MG Road, Bangalore', 12.972836, 77.607539, 'Food & Grocery', 'bronze', 3.1, 93, ''),
    ('Moglu', 'MG Road, Bangalore', 12.975278, 77.601778, 'Food & Grocery', 'free', 3.6, 11, ''),
    ('BBlunt', '3, Magrath Road, MG Road, Bangalore', 12.971332, 77.608001, 'Beauty & Personal Care', 'silver', 3.9, 286, ''),
    ('Bastian', "4, St. Mark's Road, Shanthala Nagar, Bangalore", 12.9698, 77.600063, 'Food & Grocery', 'silver', 4.4, 428, ''),
    ('Sozo Skyline', 'MG Road, Bangalore', 12.974698, 77.597054, 'Food & Grocery', 'gold', 4.1, 1758, ''),
    ('Bucket Biryani', 'MG Road, Bangalore', 12.967064, 77.608926, 'Food & Grocery', 'silver', 3.6, 359, ''),
    ('Rukinder Kumar', 'MG Road, Bangalore', 12.96776, 77.607331, 'Clothing', 'bronze', 3.2, 35, ''),
    ('The Living Room', 'MG Road, Bangalore', 12.973771, 77.609242, 'Food & Grocery', 'bronze', 3.8, 107, ''),
    ('Cake Land', 'MG Road, Bangalore', 12.972874, 77.60914, 'Food & Grocery', 'platinum', 4.1, 1913, ''),
    ('Selec Hypermart', 'MG Road, Bangalore', 12.972815, 77.607616, 'Food & Grocery', 'free', 3.4, 56, ''),
    ('Zodiac', 'MG Road, Bangalore', 12.975003, 77.607782, 'Clothing', 'bronze', 3.6, 113, ''),
    ('23rd Street Pizza', '19/1, Castle Street, Ashok Nagar, Bangalore', 12.968056, 77.60719, 'Food & Grocery', 'bronze', 3.3, 124, '+919902866933'),
    ('Ramji Chaiwale', 'Residency Road, MG Road, Bangalore', 12.967467, 77.59992, 'Food & Grocery', 'silver', 4.1, 76, ''),
    ('Cauvery Silk International', 'MG Road, Bangalore', 12.97446, 77.609792, 'Clothing', 'silver', 4.0, 83, ''),
    ('Bhartiya Jalpan', '14, Commercial Street, Shivajinagar, Bangalore', 12.981692, 77.609557, 'Food & Grocery', 'bronze', 3.6, 118, ''),
    ('Hotel Aaradhya Donne Biryani', 'MG Road, Bangalore', 12.975383, 77.603679, 'Food & Grocery', 'free', 3.4, 77, ''),
    ('T Super Market', 'MG Road, Bangalore', 12.975075, 77.604001, 'Food & Grocery', 'gold', 4.0, 1293, ''),
    ('Tea Lounge Cafe', 'MG Road, Bangalore', 12.967035, 77.608989, 'Food & Grocery', 'silver', 4.3, 208, ''),
    ('General Items', '24, Gangadhar Chetty Road, Halasuru, Bangalore', 12.981787, 77.61221, 'Food & Grocery', 'platinum', 4.7, 3710, ''),
    ('Bonkers Corner', 'MG Road, Bangalore', 12.974747, 77.607717, 'Clothing', 'free', 3.4, 10, ''),
    ('New Balance', 'MG Road, Bangalore', 12.974613, 77.607681, 'Clothing', 'bronze', 3.4, 104, ''),
    ('The Twinkle', 'MG Road, Bangalore', 12.974399, 77.609796, 'Clothing', 'gold', 4.4, 1797, ''),
    ('Street Storyss', '202, Brigade Road, Ashok Nagar, Bangalore', 12.9727, 77.606916, 'Food & Grocery', 'gold', 4.7, 1968, ''),
    ('Aptronix', 'MG Road, Bangalore', 12.975683, 77.604456, 'Electronics', 'bronze', 3.6, 128, ''),
    ('MG', 'MG Road, Bangalore', 12.975209, 77.599521, 'Automotive', 'bronze', 3.0, 99, ''),
    ('Electronic Devices', 'MG Road, Bangalore', 12.976121, 77.602819, 'Electronics', 'free', 2.9, 62, ''),
    ('Rainbow General Store', 'MG Road, Bangalore', 12.974173, 77.598551, 'Food & Grocery', 'free', 3.7, 19, ''),
    ('Jeane Claude Biguine', 'MG Road, Bangalore', 12.971867, 77.598188, 'Beauty & Personal Care', 'free', 3.5, 12, ''),
    ('Nicobar', 'MG Road, Bangalore', 12.971667, 77.597927, 'Clothing', 'bronze', 3.5, 154, ''),
    ('Bounce', 'MG Road, Bangalore', 12.971376, 77.597565, 'Beauty & Personal Care', 'free', 3.0, 79, ''),
    ('Raw Mango', 'MG Road, Bangalore', 12.971509, 77.597602, 'Clothing', 'free', 3.1, 20, ''),
    ("Asha's Store", 'MG Road, Bangalore', 12.972546, 77.599019, 'Food & Grocery', 'free', 3.4, 79, ''),
    ('India Chai', 'MG Road, Bangalore', 12.973473, 77.607319, 'Food & Grocery', 'gold', 3.9, 996, ''),
    ('Ghee Roast Company', 'MG Road, Bangalore', 12.975413, 77.602349, 'Food & Grocery', 'free', 3.1, 30, ''),
    ('Kazan Japanese Ramen', 'MG Road, Bangalore', 12.97583, 77.602066, 'Food & Grocery', 'platinum', 4.6, 4177, ''),
    ('Botany Brew and Kitchen', 'Kasturba Road, Shanthala Nagar, Bangalore', 12.974652, 77.597073, 'Food & Grocery', 'free', 2.9, 60, ''),
    ('Bootstrapped Cafe', 'MG Road, Bangalore', 12.973015, 77.615546, 'Food & Grocery', 'gold', 3.9, 1489, ''),
    ('Pachu’s Rolls', 'MG Road, Bangalore', 12.974673, 77.605845, 'Food & Grocery', 'gold', 4.7, 1045, ''),
    ('Varaha Cafe', 'MG Road, Bangalore', 12.967465, 77.60662, 'Food & Grocery', 'silver', 4.2, 307, ''),
    ('Sakura Matcha Bar', '33/12, Victoria Road, MG Road, Bangalore', 12.966469, 77.612681, 'Food & Grocery', 'bronze', 4.0, 71, ''),
    ('11grams Coffee Roasters', 'MG Road, Bangalore', 12.971271, 77.608012, 'Food & Grocery', 'gold', 3.8, 612, ''),
    ("Good Flippin' Burgers", 'MG Road, Bangalore', 12.974324, 77.607203, 'Food & Grocery', 'free', 3.2, 47, ''),
    ('The Antiquarian Bookworm', 'Church Street, MG Road, Bangalore', 12.974139, 77.605649, 'Books', 'silver', 3.5, 483, '+919845076757'),
    ('Park View', 'MG Road, Bangalore', 12.972839, 77.616818, 'Food & Grocery', 'bronze', 3.1, 41, ''),
    ('Green Onion', 'MG Road, Bangalore', 12.973428, 77.609345, 'Food & Grocery', 'silver', 3.7, 312, ''),
    ("Si Nonna's", "61, St. Mark's Road, Shanthala Nagar, Bangalore", 12.973305, 77.601921, 'Food & Grocery', 'free', 3.1, 28, '+919742346979'),
    ('Excelsior', 'MG Road, Bangalore', 12.983554, 77.610815, 'Food & Grocery', 'gold', 4.6, 154, ''),
    ('The White Room Coffee & Kitchen', 'MG Road, Bangalore', 12.975154, 77.603168, 'Food & Grocery', 'bronze', 3.4, 115, ''),
    ('Kaippunyam Kerala Restaurant', 'MG Road, Bangalore', 12.970748, 77.606645, 'Food & Grocery', 'free', 3.7, 47, ''),
    ('Olive', 'MG Road, Bangalore', 12.96939, 77.607996, 'Food & Grocery', 'free', 3.0, 12, ''),
    ('Sando Club', 'MG Road, Bangalore', 12.971647, 77.612647, 'Food & Grocery', 'gold', 4.3, 1720, ''),
    ('Peekay mini mart', 'MG Road, Bangalore', 12.966251, 77.603869, 'Food & Grocery', 'free', 2.7, 60, ''),
    ('Tree Tops', 'MG Road, Bangalore', 12.974698, 77.59953, 'Food & Grocery', 'silver', 3.5, 411, ''),
    ('Magnolia Bakery', 'MG Road, Bangalore', 12.973462, 77.601927, 'Food & Grocery', 'bronze', 3.6, 40, ''),
    ('Kerala Cafe', 'MG Road, Bangalore', 12.973501, 77.60798, 'Food & Grocery', 'free', 2.9, 19, ''),
    ('Brigade Luxury Spa * Salon', 'MG Road, Bangalore', 12.973496, 77.607911, 'Beauty & Personal Care', 'bronze', 3.7, 155, ''),
    ('Brigade Dine Cafe', 'MG Road, Bangalore', 12.973493, 77.607861, 'Food & Grocery', 'bronze', 3.8, 80, ''),
    ('Brigade Tea Centre', 'MG Road, Bangalore', 12.973529, 77.607632, 'Food & Grocery', 'bronze', 3.0, 127, ''),
    ('Shooketts', 'MG Road, Bangalore', 12.975047, 77.617932, 'Food & Grocery', 'silver', 4.0, 82, ''),
    ("Atty's Express Cafe", 'MG Road, Bangalore', 12.97539, 77.602846, 'Food & Grocery', 'free', 3.6, 76, ''),
    ('Biryani Blues', 'MG Road, Bangalore', 12.974677, 77.60581, 'Food & Grocery', 'bronze', 3.6, 167, ''),
    ('Levista Gourmet Studio', 'MG Road, Bangalore', 12.97477, 77.606415, 'Food & Grocery', 'free', 2.8, 54, ''),
    ('Ferrara', '171-172, Brigade Road, Ashok Nagar, Bangalore', 12.972711, 77.606963, 'Food & Grocery', 'free', 3.1, 73, ''),
    ('Karma & Kurry', 'MG Road, Bangalore', 12.97002, 77.609329, 'Food & Grocery', 'bronze', 3.7, 41, ''),
    ('Kwality Fresh Juice Bar', 'A004, Brigade Road, Ashok Nagar, Bangalore', 12.971173, 77.606834, 'Food & Grocery', 'bronze', 3.4, 154, ''),
    ('Lusitania', 'A003, Brigade Road, Ashok Nagar, Bangalore', 12.97114, 77.60682, 'Food & Grocery', 'bronze', 3.8, 149, ''),
    ('KoMaama', 'A002, Brigade Road, Ashok Nagar, Bangalore', 12.971112, 77.60682, 'Food & Grocery', 'silver', 3.8, 290, ''),
    ('The Falcon Cafe', '19, Brunton Road, Ashok Nagar, Bangalore', 12.971296, 77.614384, 'Food & Grocery', 'free', 3.1, 47, '+91falconcafe@prestigeconstructions.com'),
    ('Kalakruti', 'MG Road, Bangalore', 12.981801, 77.608074, 'Clothing', 'free', 2.7, 8, ''),
    ('PartTwo', '20/2, Vittal Mallaya Road, MG Road, Bangalore', 12.970907, 77.597548, 'Food & Grocery', 'bronze', 3.7, 180, ''),
    ('Trim N Shape', 'MG Road, Bangalore', 12.97304, 77.607135, 'Beauty & Personal Care', 'free', 3.3, 42, ''),
    ('Kef', 'MG Road, Bangalore', 12.972127, 77.607764, 'Food & Grocery', 'free', 3.1, 43, ''),
    ('Beyond Bean Cafe', 'MG Road, Bangalore', 12.972749, 77.607541, 'Food & Grocery', 'gold', 3.9, 1695, ''),
    ('koskii', 'MG Road, Bangalore', 12.982153, 77.610366, 'Clothing', 'silver', 4.4, 295, ''),
    ('Taste of Tibet', 'MG Road, Bangalore', 12.973508, 77.606878, 'Food & Grocery', 'silver', 4.2, 82, ''),
    ('Basaveshwar Khanavali', 'MG Road, Bangalore', 12.972868, 77.609416, 'Food & Grocery', 'silver', 4.3, 68, ''),
    ('The Boba King', 'MG Road, Bangalore', 12.973206, 77.606804, 'Food & Grocery', 'free', 2.7, 25, ''),
    ('Hatti Kaapi', 'MG Road, Bangalore', 12.980281, 77.605045, 'Food & Grocery', 'gold', 4.0, 735, ''),
    ('Vodafone Idea', 'MG Road, Bangalore', 12.975308, 77.602978, 'Electronics', 'bronze', 4.0, 159, ''),
    ('Sri Seshamahal Restaurant', 'MG Road, Bangalore', 12.975737, 77.602293, 'Food & Grocery', 'bronze', 3.0, 185, ''),
    ('Tasty Food', 'MG Road, Bangalore', 12.975743, 77.602379, 'Food & Grocery', 'free', 3.1, 6, ''),
    ('Santosh Chats', 'MG Road, Bangalore', 12.975775, 77.60238, 'Food & Grocery', 'gold', 4.0, 354, ''),
    ('Foodstories', 'MG Road, Bangalore', 12.972736, 77.600274, 'Food & Grocery', 'silver', 3.8, 254, ''),
    ('Dhanalakshmi Tiffin Room', 'MG Road, Bangalore', 12.976001, 77.602289, 'Food & Grocery', 'silver', 4.3, 247, ''),
    ('Shree Lakshmi Ganesha Cafe', 'MG Road, Bangalore', 12.972098, 77.615552, 'Food & Grocery', 'bronze', 3.2, 197, ''),
    ('Paragon Quick Bites', 'MG Road, Bangalore', 12.9751, 77.602531, 'Food & Grocery', 'bronze', 3.9, 152, ''),
    ('The Chatpata Affair', 'MG Road, Bangalore', 12.975319, 77.602546, 'Food & Grocery', 'free', 2.8, 54, ''),
    ('Appam & Co. by Paragon', 'MG Road, Bangalore', 12.975523, 77.602368, 'Food & Grocery', 'free', 3.3, 23, ''),
    ('Boss Burger', 'MG Road, Bangalore', 12.971363, 77.598348, 'Food & Grocery', 'bronze', 3.3, 40, ''),
    ('Easy Bites by Empire', 'MG Road, Bangalore', 12.967308, 77.608956, 'Food & Grocery', 'bronze', 3.3, 134, ''),
    ('Snacks Corner', 'B008, Brigade Road, Ashok Nagar, Bangalore', 12.970492, 77.606563, 'Food & Grocery', 'silver', 3.8, 403, ''),
    ('Krok Burgers', '202, Brigade Road, Ashok Nagar, Bangalore', 12.972801, 77.606799, 'Food & Grocery', 'bronze', 3.4, 110, ''),
    ('Downtown Diner', '43, 43, Field Marshall Cariappa Road, Galaxy, Shanthala Nagar, MG Road, Bangalore', 12.973002, 77.608678, 'Food & Grocery', 'silver', 3.8, 377, ''),
    ('Trent/WestSide/Zudio', '47,48, No 47, 48, Residency Road, Ashok Nagar, MG Road, Bangalore', 12.973389, 77.611132, 'Clothing', 'gold', 4.7, 217, ''),
    ('Luscious', 'MG Road, Bangalore', 12.969251, 77.600317, 'Food & Grocery', 'free', 3.3, 46, ''),
    ('Kohinoor', 'MG Road, Bangalore', 12.969268, 77.600183, 'Food & Grocery', 'bronze', 3.7, 60, ''),
    ('4/1 Kitchen', 'MG Road, Bangalore', 12.969294, 77.600043, 'Food & Grocery', 'platinum', 4.1, 4089, ''),
    ('The Rogue Elephant Cafe', '262, Kamaraj Road, Shivajinagar, Bangalore', 12.983313, 77.611239, 'Food & Grocery', 'free', 2.8, 46, ''),
    ('Kalmane Koffees', 'MG Road, Bangalore', 12.970114, 77.609342, 'Food & Grocery', 'bronze', 3.9, 28, ''),
    ('Beyondburg Inc.', "16/1, St. Mark's Road, Shanthala Nagar, Bangalore", 12.971535, 77.600845, 'Food & Grocery', 'silver', 4.1, 239, ''),
    ('Yan Yan', 'MG Road, Bangalore', 12.967275, 77.601894, 'Food & Grocery', 'bronze', 3.8, 166, ''),
    ('Tribal Brew Coffee', 'MG Road, Bangalore', 12.974669, 77.605879, 'Food & Grocery', 'free', 3.1, 40, ''),
    ('Kokoro', 'MG Road, Bangalore', 12.975486, 77.602, 'Food & Grocery', 'silver', 3.6, 108, ''),
    ('Pinocchio', '202, Brigade Road, Ashok Nagar, Bangalore', 12.973017, 77.606288, 'Food & Grocery', 'free', 2.9, 66, ''),
    ("Juliana's", 'MG Road, Bangalore', 12.979908, 77.607018, 'Food & Grocery', 'bronze', 3.6, 87, ''),
    ('Siorai', 'MG Road, Bangalore', 12.972502, 77.598661, 'Clothing', 'silver', 4.1, 159, ''),
    ('Burger & Strawberries', 'MG Road, Bangalore', 12.97539, 77.602523, 'Food & Grocery', 'platinum', 4.3, 4523, ''),
    ('Nandhini Delux', "Saint Mark's Road, MG Road, Bangalore", 12.971649, 77.600898, 'Food & Grocery', 'free', 2.8, 9, '+9108069059999'),
    ('Maserati', 'MG Road, Bangalore', 12.973542, 77.601725, 'Automotive', 'bronze', 3.9, 133, ''),
    ('Designer', 'MG Road, Bangalore', 12.975175, 77.604982, 'Clothing', 'platinum', 4.8, 3118, ''),
    ('Fairy Beauty Parlour', "St. Patrick's Square, Ashok Nagar, Bangalore", 12.971336, 77.606465, 'Beauty & Personal Care', 'silver', 4.1, 331, ''),
    ('Faye', 'A006, Brigade Road, Ashok Nagar, Bangalore', 12.971236, 77.606851, 'Clothing', 'bronze', 3.9, 60, ''),
    ('Threadworks', 'A201, Brigade Road, Ashok Nagar, Bangalore', 12.971103, 77.606735, 'Clothing', 'silver', 4.3, 376, '+918025590077,+919008943333'),
    ('Sri Krishna Grand', 'Brigade Road, Ashok Nagar, Bangalore', 12.971385, 77.606715, 'Food & Grocery', 'free', 3.1, 28, ''),
    ('Wave Tech', 'A005, Brigade Road, Ashok Nagar, Bangalore', 12.9712, 77.606841, 'Stationery', 'free', 2.9, 17, ''),
    ("Kolhapuri's", 'Brigade Road, Ashok Nagar, Bangalore', 12.971384, 77.606677, 'Clothing', 'free', 3.2, 46, ''),
    ('Fashion Zone', 'B016, Brigade Road, Ashok Nagar, Bangalore', 12.970787, 77.606689, 'Clothing', 'silver', 3.8, 461, ''),
    ('Unity Center', 'G007, Brigade Road, Ashok Nagar, Bangalore', 12.970414, 77.606543, 'Stationery', 'platinum', 4.1, 1206, ''),
    ('Maruthi Medicals', 'B004, Brigade Road, Ashok Nagar, Bangalore', 12.970356, 77.606527, 'Health & Wellness', 'free', 3.1, 61, ''),
    ('Hari Super Sandwich', 'G012, Brigade Road, Ashok Nagar, Bangalore', 12.970634, 77.606632, 'Food & Grocery', 'bronze', 3.8, 167, ''),
    ('Fresh & Live', 'B011, Brigade Road, Ashok Nagar, Bangalore', 12.970585, 77.606619, 'Food & Grocery', 'free', 3.7, 9, ''),
    ('Olive Cafe', 'B001, Brigade Road, Ashok Nagar, Bangalore', 12.970259, 77.606498, 'Food & Grocery', 'bronze', 3.7, 108, ''),
    ('Sirisha Fast Food', 'B001, Brigade Road, Ashok Nagar, Bangalore', 12.970235, 77.606489, 'Food & Grocery', 'free', 3.5, 70, ''),
    ('Kanti Bakes & Flakes', 'B002,B003, Brigade Road, Ashok Nagar, Bangalore', 12.970299, 77.606509, 'Food & Grocery', 'silver', 3.6, 116, ''),
    ('Wardy & Co', 'B015, Brigade Road, Ashok Nagar, Bangalore', 12.970755, 77.606673, 'Clothing', 'bronze', 3.9, 34, ''),
    ('Zaika', 'B013, Brigade Road, Ashok Nagar, Bangalore', 12.970676, 77.606647, 'Food & Grocery', 'gold', 4.2, 722, ''),
    ("Brahmin's Cafe", 'B005,B006, Brigade Road, Ashok Nagar, Bangalore', 12.97038, 77.606533, 'Food & Grocery', 'free', 3.0, 28, ''),
    ('Bling Fashion', 'MG Road, Bangalore', 12.974653, 77.606706, 'Clothing', 'bronze', 3.9, 92, ''),
    ('Vintage Adda', 'MG Road, Bangalore', 12.973699, 77.606628, 'Food & Grocery', 'gold', 4.0, 1400, ''),
    ('Zarqash', 'MG Road, Bangalore', 12.967548, 77.601723, 'Food & Grocery', 'platinum', 4.3, 2980, ''),
    ('Purpose Match and Coffee House', 'MG Road, Bangalore', 12.968775, 77.60261, 'Food & Grocery', 'silver', 3.5, 378, ''),
    ('Cocat Cafe', 'MG Road, Bangalore', 12.972812, 77.61715, 'Food & Grocery', 'free', 3.6, 19, ''),
    ('Little Break', 'MG Road, Bangalore', 12.972815, 77.617137, 'Food & Grocery', 'free', 3.3, 77, ''),
    ('Iyengar Bakery', 'MG Road, Bangalore', 12.984557, 77.612165, 'Food & Grocery', 'bronze', 3.9, 30, ''),
    ('Hey Habibi', 'MG Road, Bangalore', 12.975885, 77.605517, 'Food & Grocery', 'gold', 3.7, 193, ''),
    ('Koriken', 'MG Road, Bangalore', 12.975808, 77.602125, 'Food & Grocery', 'gold', 4.5, 642, ''),
    ('Yazu', 'MG Road, Bangalore', 12.973316, 77.601705, 'Food & Grocery', 'silver', 4.2, 263, ''),
    ('Cha Hong Kong Eating House', 'Church Street, MG Road, Bangalore', 12.975497, 77.601936, 'Food & Grocery', 'silver', 3.4, 371, ''),
    ('Araku', 'MG Road, Bangalore', 12.970685, 77.610674, 'Food & Grocery', 'bronze', 3.6, 97, ''),
    ('Ishaara', 'MG Road, Bangalore', 12.975267, 77.601976, 'Food & Grocery', 'bronze', 3.8, 195, ''),
    ('MTR 1924', 'MG Road, Bangalore', 12.972186, 77.600921, 'Food & Grocery', 'bronze', 3.5, 196, ''),
    ('City Walk', 'MG Road, Bangalore', 12.974363, 77.607298, 'Food & Grocery', 'free', 3.1, 61, ''),
    ('Hotel The Curzon Court', 'MG Road, Bangalore', 12.974421, 77.607862, 'Food & Grocery', 'bronze', 3.3, 56, ''),
    ('Trippy Goat', 'MG Road, Bangalore', 12.972803, 77.61481, 'Food & Grocery', 'bronze', 3.7, 108, ''),
    ('Toms', 'MG Road, Bangalore', 12.967018, 77.606365, 'Food & Grocery', 'platinum', 4.1, 3535, ''),
    ('Fatima', 'MG Road, Bangalore', 12.968512, 77.606087, 'Food & Grocery', 'bronze', 3.1, 81, ''),
    ('The Filter Coffee', 'Church Street, MG Road, Bangalore', 12.975417, 77.602294, 'Food & Grocery', 'bronze', 3.3, 83, ''),
    ('Pizza No Cap', 'MG Road, Bangalore', 12.975356, 77.616824, 'Food & Grocery', 'free', 2.8, 49, ''),
    ('Nāvu', 'MG Road, Bangalore', 12.973261, 77.614853, 'Food & Grocery', 'gold', 4.1, 1872, '+919686978053'),
    ('Homiga', '202, Brigade Road, Ashok Nagar, Bangalore', 12.973051, 77.606152, 'Food & Grocery', 'gold', 4.5, 1283, '+919611339046'),
    ('Hyderbad Biryani House', 'Victoria Road, Brigade Road, Bangalore', 12.966554, 77.614103, 'Food & Grocery', 'silver', 3.6, 131, ''),
    ('Hotel Fanoos', 'Brigade Road, Bangalore', 12.964505, 77.606532, 'Food & Grocery', 'bronze', 3.5, 147, ''),
    ('Residency Pharmacy', 'Brigade Road, Bangalore', 12.966891, 77.598627, 'Health & Wellness', 'gold', 4.2, 284, ''),
    ('Richmond Supermarket', 'Brigade Road, Bangalore', 12.962264, 77.604096, 'Food & Grocery', 'gold', 4.2, 593, ''),
    ('Karnataka Supermarket', 'Brigade Road, Bangalore', 12.964419, 77.60623, 'Food & Grocery', 'free', 3.1, 6, ''),
    ('Daysie', '18, MG Road, Ashok Nagar, Bangalore', 12.972242, 77.618442, 'Food & Grocery', 'free', 3.3, 60, '+9108047250000'),
    ('Big Kids Kemp', 'Mahatma Gandhi Road, Brigade Road, Bangalore', 12.972382, 77.618595, 'Clothing', 'free', 2.7, 13, ''),
    ("Faasos's", 'Brigade Road, Bangalore', 12.967457, 77.599797, 'Food & Grocery', 'bronze', 3.9, 113, ''),
    ('Balaji Pharma', 'Brigade Road, Bangalore', 12.967419, 77.599753, 'Health & Wellness', 'silver', 3.8, 255, ''),
    ('Siddiqu Kabab Center', 'Brigade Road, Bangalore', 12.9631, 77.605785, 'Food & Grocery', 'free', 3.3, 46, ''),
    ('Avon', 'Brigade Road, Bangalore', 12.965537, 77.602749, 'Beauty & Personal Care', 'silver', 3.5, 308, ''),
    ('Lakme Salon', 'Brigade Road, Bangalore', 12.96554, 77.60281, 'Beauty & Personal Care', 'bronze', 3.5, 179, ''),
    ('Bangalore Fetal Medicine Centre', '5/1, 2nd Floor, Rich Homes, Richmond Road, Shanthala Nagar, Ashok Nagar, Shanthala Nagar, Ashok Nagar, Brigade Road, Bangalore', 12.965863, 77.599881, 'Health & Wellness', 'silver', 3.5, 99, '+918022210540'),
    ('Aina Lounge', 'Mother Teresa Road, Brigade Road, Bangalore', 12.961847, 77.612658, 'Beauty & Personal Care', 'bronze', 3.8, 106, '+919632037895'),
    ('Reeta Enterprises', 'Brigade Road, Bangalore', 12.964672, 77.614358, 'Stationery', 'silver', 3.8, 123, ''),
    ('Sai Stores', 'Brigade Road, Bangalore', 12.964666, 77.614324, 'Stationery', 'free', 3.2, 64, ''),
    ('Lusitania Meat Shop', 'Brigade Road, Bangalore', 12.964636, 77.614174, 'Food & Grocery', 'free', 2.7, 42, ''),
    ('Coorg Products', 'Brigade Road, Bangalore', 12.964623, 77.613989, 'Food & Grocery', 'gold', 4.2, 405, ''),
    ('MS Restaurant', 'Brigade Road, Bangalore', 12.964627, 77.61391, 'Food & Grocery', 'silver', 3.9, 217, ''),
    ('Kamath Stores', 'Brigade Road, Bangalore', 12.964494, 77.613541, 'Food & Grocery', 'gold', 4.0, 904, ''),
    ('Little Chef', 'Brigade Road, Bangalore', 12.964358, 77.613531, 'Food & Grocery', 'platinum', 4.7, 2951, ''),
    ('Chaitanya Delicacy', 'Brigade Road, Bangalore', 12.963822, 77.614491, 'Food & Grocery', 'silver', 3.9, 310, ''),
    ('Aubree', 'Brigade Road, Bangalore', 12.972387, 77.618556, 'Food & Grocery', 'bronze', 3.7, 96, ''),
    ('Comal', 'Mahatma Gandhi Road, Ashok Nagar, Bangalore', 12.972337, 77.618762, 'Food & Grocery', 'bronze', 3.0, 104, '+918123240555'),
    ('Makkah Cafe', 'Brigade Road, Bangalore', 12.963928, 77.60648, 'Food & Grocery', 'silver', 4.4, 348, ''),
    ('Miyajima Bento', 'Brigade Road, Bangalore', 12.965438, 77.614443, 'Food & Grocery', 'bronze', 3.9, 196, ''),
    ('Ashas Store', 'Brigade Road, Bangalore', 12.970495, 77.61697, 'Food & Grocery', 'silver', 4.0, 185, ''),
    ('Dindigul Thalappakatti', 'Brigade Road, Bangalore', 12.972361, 77.618644, 'Food & Grocery', 'platinum', 4.8, 2364, ''),
    ('Chianti', 'Brigade Road, Bangalore', 12.972345, 77.618718, 'Food & Grocery', 'silver', 4.0, 137, ''),
    ('Pathi Silks', 'Brigade Road, Bangalore', 12.96581, 77.602151, 'Clothing', 'bronze', 3.7, 117, ''),
    ('Ravindu Toyota', 'Brigade Road, Bangalore', 12.96652, 77.613135, 'Automotive', 'free', 3.4, 4, ''),
    ('Khazana Food Paradise', 'Brigade Road, Bangalore', 12.964189, 77.605534, 'Food & Grocery', 'silver', 4.1, 147, ''),
    ("Glen's Bakehouse", 'Brigade Road, Bangalore', 12.969789, 77.597404, 'Food & Grocery', 'free', 3.0, 19, ''),
    ('BarberCo', '9, Myrtle Lane, Richmond Town, Bangalore', 12.963796, 77.603658, 'Beauty & Personal Care', 'bronze', 3.2, 148, ''),
    ('Shear Genius', 'Brigade Road, Bangalore', 12.964646, 77.613053, 'Beauty & Personal Care', 'silver', 4.3, 496, ''),
    ('Sunrise Mart', 'Brigade Road, Bangalore', 12.96435, 77.613474, 'Food & Grocery', 'gold', 4.4, 1545, ''),
    ('Daily Needs', 'Brigade Road, Bangalore', 12.964474, 77.613721, 'Food & Grocery', 'free', 2.9, 41, ''),
    ('Nesto Fresh Mart', 'Palm Grove Road, Brigade Road, Bangalore', 12.964373, 77.614578, 'Food & Grocery', 'platinum', 4.5, 3586, ''),
    ('Shanti Hotel', 'Brigade Road, Bangalore', 12.967231, 77.614165, 'Food & Grocery', 'bronze', 3.2, 156, ''),
    ('Ithaca', 'Brigade Road, Bangalore', 12.965966, 77.598727, 'Food & Grocery', 'bronze', 3.4, 194, ''),
    ('In Wok', 'Brigade Road, Bangalore', 12.965419, 77.602752, 'Food & Grocery', 'silver', 4.1, 362, ''),
    ('Mad Fern', 'Brigade Road, Bangalore', 12.968779, 77.597589, 'Beauty & Personal Care', 'bronze', 4.0, 91, ''),
    ('Beijing Bites', '11/2, Hayes Road Off Richmond Road, Landmark Diagonally, opposite to Baldwin Girls High School, Brigade Road, Bangalore', 12.965737, 77.601572, 'Food & Grocery', 'silver', 3.6, 202, '+918041246933'),
    ('Kerala Pavilion', 'Brigade Road, Bangalore', 12.972207, 77.618417, 'Food & Grocery', 'silver', 4.1, 403, ''),
    ("Thom's Bakery", '# 1/2, Wheeler Road, Commercial Street, Bangalore', 12.991547, 77.614181, 'Food & Grocery', 'silver', 3.7, 228, ''),
    ('Sanman Veg', 'Cunningham Road, Commercial Street, Bangalore', 12.984734, 77.597126, 'Food & Grocery', 'silver', 4.3, 419, ''),
    ('Eden Park', '21/1, Cunningham Road, Commercial Street, Bangalore', 12.983156, 77.598711, 'Food & Grocery', 'free', 2.8, 69, ''),
    ('Al-Siddique Kabab Center', "Saint John's Church Road, Commercial Street, Bangalore", 12.993111, 77.608919, 'Food & Grocery', 'bronze', 4.0, 108, ''),
    ('Siddique Kabab Center', "Saint John's Church Road, Commercial Street, Bangalore", 12.993416, 77.608356, 'Food & Grocery', 'platinum', 4.8, 2879, ''),
    ('Mutton Shop', "Saint John's Church Road, Commercial Street, Bangalore", 12.993617, 77.608377, 'Food & Grocery', 'free', 3.7, 10, ''),
    ('Chicken Shop', 'Commercial Street, Bangalore', 12.993615, 77.608434, 'Food & Grocery', 'bronze', 3.4, 85, ''),
    ('Garden care', '59, Osbourne Road, Commercial Street, Bangalore', 12.987298, 77.617833, 'Food & Grocery', 'silver', 3.6, 473, ''),
    ('T. K. Diagnostic Centre', '22, 1st Floor, NG Chambers, Venkataswamy Naidu Rd, Shivaji Nagar, Commercial Street, Bangalore', 12.984324, 77.598475, 'Health & Wellness', 'bronze', 3.9, 198, '+918022860567'),
    ('Al Shifa Unani & Ayurvedic Dawa Khana', '28, Charminar Masjid Road, Near-Russel Market, Shivaji Nagar, Sulthangunta, Shivaji Nagar, Commercial Street, Bangalore', 12.987316, 77.605987, 'Health & Wellness', 'free', 2.8, 75, '+918025302786'),
    ('Tiger Trail', '11, Park Road, Shivaji Nagar, Commercial Street, Bangalore', 12.98395, 77.598533, 'Food & Grocery', 'free', 2.8, 67, '+918022865566'),
    ('Hilal Restaurant', '10, HKP Road \xa0Shivajinagar, Commercial Street, Bangalore', 12.985772, 77.60209, 'Food & Grocery', 'free', 2.9, 12, '+918022867300'),
    ('Sports Line and Fitness', '14, NExt To Sanjevani,, 5, Queens Rd, Bhadrappa Layout, Swamy Shivanandapuram, Vasanth Nagar, Commercial Street, Bangalore', 12.987206, 77.598574, 'Sports', 'bronze', 3.7, 45, '+918025903106'),
    ('Pepper Cafe', 'Commercial Street, Bangalore', 12.985305, 77.615238, 'Food & Grocery', 'silver', 3.5, 331, ''),
    ('Tasty food corner', 'Commercial Street, Bangalore', 12.984139, 77.614266, 'Food & Grocery', 'platinum', 4.2, 4396, ''),
    ('Royal Sports', 'Commercial Street, Bangalore', 12.993963, 77.607739, 'Sports', 'bronze', 3.2, 93, ''),
    ('Fathima Store', 'Commercial Street, Bangalore', 12.992206, 77.604458, 'Food & Grocery', 'bronze', 3.6, 42, ''),
    ('The French Loaf', 'Commercial Street, Bangalore', 12.993695, 77.607556, 'Food & Grocery', 'silver', 3.6, 428, ''),
    ('Ambara', 'Annaswamy Mudaliar Road, Commercial Street, Bangalore', 12.98406, 77.617327, 'Clothing', 'silver', 4.3, 139, ''),
    ("Shivajinagar Gardi Ustad Pehlwan Kale Bhai's - Pahilwani Handi", 'Commercial Street, Bangalore', 12.985572, 77.601914, 'Food & Grocery', 'bronze', 3.8, 29, ''),
    ('Chai Shop', 'Commercial Street, Bangalore', 12.985648, 77.601727, 'Food & Grocery', 'platinum', 4.4, 4558, ''),
    ('Toyota', 'Commercial Street, Bangalore', 12.985478, 77.597855, 'Automotive', 'free', 3.4, 37, ''),
    ('Urban Solace', '32, Annaswamy Mudaliar Road, Ulsoor, Bangalore', 12.985053, 77.618296, 'Food & Grocery', 'gold', 3.7, 1263, ''),
    ('ARK Tyres', 'Commercial Street, Bangalore', 12.99345, 77.607043, 'Automotive', 'silver', 4.3, 221, ''),
    ('Amagat Garden Cafe', '22, Annaswamy Mudaliar Road, Commercial Street, Bangalore', 12.984016, 77.617405, 'Food & Grocery', 'free', 3.7, 69, ''),
    ('KGN Biriyani Point', 'Commercial Street, Bangalore', 12.989602, 77.599358, 'Food & Grocery', 'bronze', 3.9, 54, ''),
    ('Al Madiinah Biryani Store', 'Commercial Street, Bangalore', 12.989298, 77.599285, 'Food & Grocery', 'gold', 4.2, 494, ''),
    ('Shahi Kabab Roll Centre', 'Commercial Street, Bangalore', 12.987816, 77.598775, 'Food & Grocery', 'gold', 4.0, 1597, ''),
    ('Santosh Pan Paradise and Fresh Juices', 'Commercial Street, Bangalore', 12.987497, 77.598657, 'Food & Grocery', 'bronze', 3.3, 67, ''),
    ('Citrus Cafe', 'Commercial Street, Bangalore', 12.987286, 77.616834, 'Food & Grocery', 'free', 3.0, 37, ''),
    ('Bombay Gulshan', '42, Charminar Masjid Road, Commercial Street, Bangalore', 12.988225, 77.606165, 'Clothing', 'silver', 4.2, 139, '+916281876685'),
    ("Syed's Shop", '5 & 6, Sultan Gunta Road, Commercial Street, Bangalore', 12.989093, 77.606222, 'Clothing', 'silver', 4.0, 257, ''),
    ('Carnival De Goa', 'Frazer Town, Bangalore', 12.975005, 77.620003, 'Food & Grocery', 'gold', 4.2, 277, ''),
    ('Shanti Sagar', "Saint John's Church Road, Frazer Town, Bangalore", 12.987086, 77.618997, 'Food & Grocery', 'bronze', 3.1, 55, ''),
    ('Lake View Supermarket', 'Frazer Town, Bangalore', 12.987547, 77.618967, 'Food & Grocery', 'bronze', 3.9, 21, ''),
    ('Moritz Nano Restaurant', 'Frazer Town, Bangalore', 12.987583, 77.618893, 'Food & Grocery', 'free', 2.9, 34, ''),
    ('Cafe Thulp', 'Frazer Town, Bangalore', 12.990826, 77.619319, 'Food & Grocery', 'platinum', 4.3, 593, ''),
    ('Manchester United Store', '1, Swami Vivekanand Road, Bhadrappa Layout, Someshwarpura, Ulsoor, Frazer Town, Bangalore', 12.973665, 77.620386, 'Sports', 'free', 3.1, 30, '+918032569278'),
    ('Zeven', 'A1, Silver Oaks, Kensington Cross Road, Ulsoor, Kensington Gardens, Kalhalli, Ulsoor, Frazer Town, Bangalore', 12.982525, 77.623585, 'Sports', 'free', 2.8, 39, '+918041709090'),
    ('Udupi Vaibhav', 'car street, Frazer Town, Bangalore', 12.97598, 77.625268, 'Food & Grocery', 'free', 3.4, 39, ''),
    ('Suneel Pharma', 'Frazer Town, Bangalore', 12.9754, 77.622391, 'Health & Wellness', 'silver', 4.0, 201, ''),
    ('Adyar Anand Bhawan', 'Frazer Town, Bangalore', 12.987799, 77.618514, 'Food & Grocery', 'bronze', 3.2, 54, ''),
    ('Cafe Felix', 'Frazer Town, Bangalore', 12.973745, 77.62041, 'Food & Grocery', 'bronze', 3.5, 175, ''),
    ('Kingdom Garments', 'Frazer Town, Bangalore', 12.97543, 77.624661, 'Clothing', 'bronze', 3.7, 140, ''),
    ('Baby Doll Fashions', 'Frazer Town, Bangalore', 12.975361, 77.624652, 'Clothing', 'silver', 4.2, 472, ''),
    ('Shoppee Paradise', '1, Bazaar Street, Frazer Town, Bangalore', 12.975262, 77.624635, 'Clothing', 'free', 2.9, 76, ''),
    ("Jasmine's Beauty Salon", '46, 3rd Cross Road, Frazer Town, Bangalore', 12.977786, 77.617985, 'Beauty & Personal Care', 'gold', 3.8, 1014, '+918025592498'),
    ('Food Hall', '1MG Mall, Mahatma Gandhi Road, Frazer Town, Bangalore', 12.973545, 77.620417, 'Food & Grocery', 'bronze', 3.4, 164, ''),
    ('Pretty Dan Good Salon', 'Promenade Road, Frazer Town, Bangalore', 12.991131, 77.616223, 'Beauty & Personal Care', 'free', 3.5, 64, '+917338475554'),
    ('Kaara by Lake', 'Frazer Town, Bangalore', 12.986531, 77.619465, 'Food & Grocery', 'free', 3.2, 21, ''),
    ('Lassi Corner', 'Frazer Town, Bangalore', 12.975044, 77.61915, 'Food & Grocery', 'bronze', 3.5, 49, ''),
    ('Yauatcha', 'Mahatma Gandhi Road, Frazer Town, Bangalore', 12.973293, 77.620365, 'Food & Grocery', 'silver', 4.1, 300, '+919222222800'),
    ('Kaati King', 'Frazer Town, Bangalore', 12.976059, 77.624873, 'Food & Grocery', 'free', 3.2, 7, ''),
    ('Sri Sai Med Pharma', 'Frazer Town, Bangalore', 12.976161, 77.624466, 'Health & Wellness', 'bronze', 3.5, 136, '+919844385199'),
    ("Nature's Basket", 'Frazer Town, Bangalore', 12.974229, 77.61981, 'Food & Grocery', 'silver', 4.0, 394, ''),
    ('Italia', 'Frazer Town, Bangalore', 12.974056, 77.619517, 'Food & Grocery', 'gold', 4.4, 910, ''),
    ('Indian Durbar', 'Frazer Town, Bangalore', 12.975392, 77.620593, 'Food & Grocery', 'free', 2.7, 27, ''),
    ('Bruno', 'Frazer Town, Bangalore', 12.974906, 77.619451, 'Food & Grocery', 'silver', 3.4, 226, ''),
    ('Kabab Manor', 'Hebbal, Bangalore', 13.027312, 77.593572, 'Food & Grocery', 'silver', 3.9, 80, ''),
    ('Medplus', 'Hebbal, Bangalore', 13.03831, 77.607234, 'Health & Wellness', 'silver', 4.3, 85, ''),
    ('Punjabi Rasoi', 'Hebbal, Bangalore', 13.025697, 77.593135, 'Food & Grocery', 'platinum', 4.5, 2851, ''),
    ('Emporium', 'Hebbal, Bangalore', 13.025451, 77.593103, 'Clothing', 'bronze', 3.8, 184, ''),
    ('More', 'Hebbal, Bangalore', 13.039221, 77.600054, 'Food & Grocery', 'platinum', 4.6, 3943, ''),
    ('Mythri', 'Hebbal, Bangalore', 13.039165, 77.599182, 'Food & Grocery', 'bronze', 3.9, 134, ''),
    ('Saaga', 'Bellary Road, Hebbal, Bangalore', 13.037153, 77.589669, 'Food & Grocery', 'gold', 4.6, 869, ''),
    ('Shukrutha Chicken Center', 'Hebbal, Bangalore', 13.027435, 77.604271, 'Food & Grocery', 'free', 3.7, 69, ''),
    ('Prem Fancy Store', 'Hebbal, Bangalore', 13.027658, 77.604326, 'Food & Grocery', 'free', 3.6, 15, ''),
    ('Sri Vinayaka Hot Chips', 'Hebbal, Bangalore', 13.027681, 77.604344, 'Food & Grocery', 'free', 3.2, 67, ''),
    ('Goli Vadapav No.1', 'Hebbal, Bangalore', 13.027594, 77.604353, 'Food & Grocery', 'free', 3.7, 0, ''),
    ('Shree Sai Appliances', 'Hebbal, Bangalore', 13.027651, 77.604371, 'Electronics', 'silver', 4.4, 287, ''),
    ('Chitra Darshini', 'Hebbal, Bangalore', 13.027677, 77.604371, 'Food & Grocery', 'silver', 3.9, 322, ''),
    ('Shree Basaveswara Frame Works', 'Hebbal, Bangalore', 13.027632, 77.604326, 'Food & Grocery', 'bronze', 3.2, 137, ''),
    ('PN Tools Hardware', 'Hebbal, Bangalore', 13.027842, 77.604419, 'Hardware', 'bronze', 3.3, 48, ''),
    ('Sangam Ladies Tailor', 'Hebbal, Bangalore', 13.027778, 77.60439, 'Electronics', 'free', 3.4, 78, ''),
    ('Khushi Beauty Parlour', 'Hebbal, Bangalore', 13.027598, 77.604325, 'Beauty & Personal Care', 'silver', 4.3, 58, ''),
    ('Chaitra Pan', 'Hebbal, Bangalore', 13.027719, 77.604403, 'Food & Grocery', 'free', 2.8, 76, ''),
    ('KGN FootWear', 'Hebbal, Bangalore', 13.027764, 77.604407, 'Clothing', 'free', 3.3, 6, ''),
    ('A1 Mobile Service Center', 'Hebbal, Bangalore', 13.027796, 77.604407, 'Electronics', 'free', 2.7, 56, ''),
    ('Durga Pan Bhandar', 'Hebbal, Bangalore', 13.027465, 77.604281, 'Food & Grocery', 'free', 2.9, 7, ''),
    ('Mummy Daddy Garments', 'Hebbal, Bangalore', 13.02757, 77.604333, 'Clothing', 'free', 3.7, 29, ''),
    ('Anagha Pharma', 'Hebbal, Bangalore', 13.027542, 77.604333, 'Health & Wellness', 'gold', 4.4, 753, ''),
    ('Sleep Good Matress', 'Hebbal, Bangalore', 13.027668, 77.604345, 'Food & Grocery', 'silver', 4.2, 469, ''),
    ('Pavithra Enterprises', 'Hebbal, Bangalore', 13.027335, 77.604295, 'Food & Grocery', 'bronze', 3.3, 56, ''),
    ('Cool Corner', 'Hebbal, Bangalore', 13.027575, 77.604312, 'Food & Grocery', 'platinum', 4.5, 3887, ''),
    ('Mobile Hut', 'Hebbal, Bangalore', 13.027409, 77.60427, 'Electronics', 'bronze', 3.1, 171, ''),
    ('Sri Srinivasa Medicals', '16, Hmt Layout, Near-R T Nagar Bus Depot, R T Nagar, HMT Layout, Dinnur, RT Nagar, Hebbal, Bangalore', 13.02722, 77.593535, 'Health & Wellness', 'gold', 4.6, 1382, '+918023535499'),
    ('Al Khair Restaurant', '20, 1st Main Rd, Kanaka Nagar, Hebbal,, Hebbal, Bangalore', 13.037306, 77.605201, 'Food & Grocery', 'silver', 4.0, 74, '+918023651666'),
    ('Shiva Homoeo Medicals', '#56/2, Sulthanpalya Main Road, 1st Floor,R.T.Nagar Post, Hebbal, Bangalore', 13.031417, 77.604328, 'Health & Wellness', 'platinum', 4.9, 3622, '+919916521079'),
    ('Wildcraft Basecamp', '127/128, Anandnagar Main Road,Esteem Mall, Shop, 1st Floor, Hebbal,, Hebbal, Bangalore', 13.034505, 77.589575, 'Sports', 'silver', 3.7, 263, '+918041648242'),
    ("Daisy's Beauty Parlour", 'Kanaka Nagar 1st Main Road, Hebbal, Bangalore', 13.040063, 77.606172, 'Beauty & Personal Care', 'bronze', 3.3, 67, '+919740007553'),
    ('Ashok bakery', 'Hebbal, Bangalore', 13.026363, 77.599665, 'Food & Grocery', 'silver', 3.9, 484, ''),
    ('Hebbal Indira Canteen', 'Hebbal, Bangalore', 13.030419, 77.587227, 'Food & Grocery', 'free', 3.6, 18, ''),
    ('Ford', 'Hebbal, Bangalore', 13.041662, 77.594839, 'Automotive', 'gold', 4.6, 1251, ''),
    ('Udupi Brahmashree', 'Hebbal, Bangalore', 13.037057, 77.589646, 'Food & Grocery', 'bronze', 3.4, 155, ''),
    ('Udupi Park Inn', 'Hebbal, Bangalore', 13.035177, 77.589022, 'Food & Grocery', 'free', 2.9, 26, ''),
    ('Varun Maruthi', 'Hebbal, Bangalore', 13.042883, 77.59878, 'Automotive', 'bronze', 3.1, 135, ''),
    ('Car Trade', 'Hebbal, Bangalore', 13.042375, 77.594994, 'Automotive', 'gold', 3.8, 1583, ''),
    ('Maruti', 'Hebbal, Bangalore', 13.041637, 77.595459, 'Automotive', 'gold', 3.8, 872, ''),
    ('Star Hotel', 'Hebbal, Bangalore', 13.038616, 77.589948, 'Food & Grocery', 'silver', 4.0, 497, ''),
    ('Shiv shakthi medicals', 'Hebbal, Bangalore', 13.03314, 77.599364, 'Health & Wellness', 'free', 3.5, 75, ''),
    ('All Fun Chai', 'Hebbal, Bangalore', 13.042269, 77.593951, 'Food & Grocery', 'silver', 4.0, 295, ''),
    ('Juice Place', 'Hebbal, Bangalore', 13.03533, 77.589415, 'Food & Grocery', 'free', 3.6, 38, ''),
    ('Venkatam Cafe', 'Hebbal, Bangalore', 13.043968, 77.601056, 'Food & Grocery', 'platinum', 4.0, 2056, ''),
    ('Chintu Chit Chat Centre Bangarapet Pani Puri', 'Hebbal, Bangalore', 13.026009, 77.601629, 'Food & Grocery', 'platinum', 4.1, 4423, ''),
    ('Smart point', 'Hebbal, Bangalore', 13.039031, 77.597549, 'Food & Grocery', 'gold', 4.4, 1541, ''),
    ('Maa collections', 'Hebbal, Bangalore', 13.039015, 77.597859, 'Clothing', 'free', 3.6, 70, ''),
    ('Sai Medicals And General Stores', 'Hebbal, Bangalore', 13.029501, 77.594972, 'Health & Wellness', 'bronze', 3.3, 139, ''),
    ('Pizza Hut', '18, 80 Feet Road, RT Nagar, Hebbal, Bangalore', 13.027425, 77.59358, 'Food & Grocery', 'gold', 4.2, 835, ''),
    ('Med Plus', 'Hebbal, Bangalore', 13.037297, 77.590599, 'Health & Wellness', 'bronze', 3.4, 147, ''),
    ('Medical store', 'Hebbal, Bangalore', 13.037241, 77.591053, 'Health & Wellness', 'bronze', 3.8, 140, ''),
    ('Cafe Amudham', 'Hebbal, Bangalore', 13.028233, 77.593452, 'Food & Grocery', 'silver', 3.7, 154, ''),
    ('Manjunath Stores', 'Sahakar Nagar, Bangalore', 13.031866, 77.580016, 'Food & Grocery', 'bronze', 3.4, 164, ''),
    ('Biryani House', 'Nagawara, Bangalore', 13.041389, 77.622383, 'Food & Grocery', 'gold', 4.4, 133, ''),
    ('Adyar Ananda Bhavan', 'Thanisandra Main Road, Nagawara, Bangalore', 13.04098, 77.624395, 'Food & Grocery', 'free', 3.6, 74, '+91918025448999'),
    ('Reliance Digital', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045429, 77.626925, 'Electronics', 'gold', 4.4, 704, '+918067294052'),
    ('I centre', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045532, 77.626818, 'Electronics', 'free', 3.0, 48, '+918067294099'),
    ('Payless Shoe Store', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045662, 77.626738, 'Clothing', 'silver', 3.6, 331, '+918067294444'),
    ('Reliance trendz', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045651, 77.626682, 'Clothing', 'silver', 4.1, 181, '+918067294066'),
    ('Wood Land', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045649, 77.626725, 'Clothing', 'silver', 3.9, 420, '+918022682216'),
    ('Reliance Foot Prints', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045586, 77.62672, 'Clothing', 'silver', 3.9, 136, '+919035055654'),
    ('Health And Glow', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045471, 77.626816, 'Beauty & Personal Care', 'silver', 3.8, 121, '+918032562821'),
    ('Spar', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045434, 77.626775, 'Food & Grocery', 'silver', 4.1, 300, '+918067294041'),
    ('Mom And Me', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.04539, 77.626856, 'Clothing', 'free', 3.6, 7, ''),
    ('levis', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045327, 77.626717, 'Clothing', 'bronze', 3.8, 39, ''),
    ('Gravity', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045335, 77.626837, 'Clothing', 'gold', 4.4, 1665, '+918067294041'),
    ('people', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045241, 77.626588, 'Clothing', 'free', 2.8, 80, '+918067294034'),
    ('Louis Phillppe', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045269, 77.626708, 'Clothing', 'free', 3.4, 1, '+918067294004'),
    ('Aurelia', 'Survey No. 132P, 133P, & 134P, Nagawara Junction, Manayata Tech Park, Thanisandra Main Road, Nagawara, Bangalore', 13.045246, 77.626697, 'Clothing', 'free', 3.2, 7, '+918067294081'),
    ('HKGN Stores', 'Nagawara, Bangalore', 13.040001, 77.62569, 'Food & Grocery', 'silver', 4.1, 365, ''),
    ('Ibaco', 'Thanisandra Main Road, Nagawara, Bangalore', 13.045057, 77.626579, 'Food & Grocery', 'gold', 4.4, 132, ''),
    ('Meghana Biryani', 'Nagawara, Bangalore', 13.040899, 77.620734, 'Food & Grocery', 'free', 3.3, 1, ''),
    ('Idea', 'Nagawara, Bangalore', 13.041096, 77.623894, 'Electronics', 'free', 3.4, 68, ''),
    ('Diamond Beauty Parlour', 'Nagawara, Bangalore', 13.041018, 77.624008, 'Beauty & Personal Care', 'platinum', 4.0, 4027, ''),
    ("Chef Baker's", 'Nagawara, Bangalore', 13.041261, 77.624554, 'Food & Grocery', 'gold', 4.6, 319, ''),
    ('Felicita', 'Nagawara, Bangalore', 13.045392, 77.627013, 'Food & Grocery', 'gold', 4.2, 593, ''),
    ('Sri Devi Grand Veg', 'Nagawara, Bangalore', 13.046677, 77.627903, 'Food & Grocery', 'free', 3.2, 45, ''),
    ('Shell Select', 'Nagawara, Bangalore', 13.051646, 77.630498, 'Food & Grocery', 'free', 3.1, 47, ''),
    ('California Burrito', 'Nagawara, Bangalore', 13.051007, 77.621221, 'Food & Grocery', 'platinum', 4.5, 2492, ''),
    ('WOW momo', 'Nagawara, Bangalore', 13.05084, 77.621178, 'Food & Grocery', 'platinum', 4.2, 1160, ''),
    ('Kinara', 'Nagawara, Bangalore', 13.05077, 77.621169, 'Food & Grocery', 'silver', 4.3, 438, ''),
    ('Sizzling Tandoor', 'Nagawara, Bangalore', 13.051133, 77.621544, 'Food & Grocery', 'free', 3.1, 48, ''),
    ('Aaha Andhra', 'Nagawara, Bangalore', 13.050685, 77.62114, 'Food & Grocery', 'silver', 3.5, 54, ''),
    ('Pathankot', 'Nagawara, Bangalore', 13.051086, 77.621697, 'Food & Grocery', 'silver', 3.5, 482, ''),
    ('Zaica Dine & Wine', 'Nagawara, Bangalore', 13.041119, 77.624478, 'Food & Grocery', 'bronze', 3.8, 164, ''),
    ('Baithaa', 'Nagawara, Bangalore', 13.041224, 77.624516, 'Food & Grocery', 'platinum', 5.0, 4826, ''),
    ('Pooja Medicals', 'Nagawara, Bangalore', 13.048229, 77.628944, 'Health & Wellness', 'gold', 3.9, 1945, ''),
    ('Star Look', 'Nagawara, Bangalore', 13.050359, 77.630229, 'Clothing', 'gold', 4.6, 1073, ''),
    ('Nandini Parlour', 'Nagawara, Bangalore', 13.053614, 77.621021, 'Food & Grocery', 'free', 3.6, 35, ''),
    ('Burwinger', 'Nagawara, Bangalore', 13.040627, 77.623606, 'Food & Grocery', 'gold', 4.3, 167, ''),
    ('Lassi Shop', 'Nagawara, Bangalore', 13.041433, 77.620731, 'Food & Grocery', 'free', 3.6, 2, ''),
    ('La Zizone', 'Nagawara, Bangalore', 13.040925, 77.622542, 'Food & Grocery', 'platinum', 4.6, 3762, ''),
    ('Ancient Hyderabad', 'Nagawara, Bangalore', 13.040834, 77.61972, 'Food & Grocery', 'gold', 4.0, 1254, ''),
    ('Food Castle', 'Nagawara, Bangalore', 13.04137, 77.621941, 'Food & Grocery', 'free', 3.5, 49, ''),
    ('Orange Leaf', 'Nagawara, Bangalore', 13.041403, 77.622749, 'Food & Grocery', 'silver', 3.9, 246, ''),
    ('Fuzion Plate', 'Nagawara, Bangalore', 13.048347, 77.629393, 'Food & Grocery', 'silver', 3.5, 402, ''),
    ('Biriyani Zone', 'Nagawara, Bangalore', 13.041409, 77.621577, 'Food & Grocery', 'silver', 3.9, 227, ''),
    ('The Woodpegger', 'Nagawara, Bangalore', 13.040585, 77.619666, 'Food & Grocery', 'free', 3.1, 9, ''),
    ('Poorvika', 'Nagawara, Bangalore', 13.046885, 77.628087, 'Electronics', 'bronze', 3.8, 172, ''),
    ('Dry Fruit House', 'Nagawara, Bangalore', 13.046769, 77.628023, 'Food & Grocery', 'bronze', 3.8, 27, ''),
    ("Heaven's Saloon", 'Thanisandra Main Road, Nagawara, Bangalore', 13.048167, 77.628888, 'Beauty & Personal Care', 'silver', 4.2, 280, '+919019830117'),
    ('Nagavara Social', 'Nagawara, Bangalore', 13.041937, 77.619654, 'Food & Grocery', 'bronze', 3.4, 116, ''),
    ('Bombay Curry House', 'Nagawara, Bangalore', 13.041854, 77.619647, 'Food & Grocery', 'bronze', 3.7, 189, ''),
    ('Medishop', 'Nagawara, Bangalore', 13.04195, 77.624779, 'Health & Wellness', 'bronze', 3.8, 95, ''),
    ('Sanchez', 'Nagawara, Bangalore', 13.041607, 77.619626, 'Food & Grocery', 'silver', 4.1, 92, '+919606071430'),
    ('Bombay Brasserie', 'Nagawara, Bangalore', 13.042033, 77.619441, 'Food & Grocery', 'free', 3.1, 19, ''),
    ('Sri Iyyengar Bakery', 'Nagawara, Bangalore', 13.038197, 77.623546, 'Food & Grocery', 'silver', 3.7, 104, ''),
    ('Green Mobille Enterprises', 'Nagawara, Bangalore', 13.03924, 77.623734, 'Electronics', 'free', 3.6, 56, ''),
    ('A1 Power Tools And Hardware', 'Nagawara, Bangalore', 13.039272, 77.623901, 'Hardware', 'bronze', 3.3, 53, ''),
    ('Delux Hardware And Paints', 'Nagawara, Bangalore', 13.039377, 77.624008, 'Hardware', 'free', 3.1, 71, ''),
    ('Sree Keerthi Glass And Plywood', 'Nagawara, Bangalore', 13.045883, 77.627514, 'Hardware', 'silver', 3.8, 63, ''),
    ('Phono Range', 'Nagawara, Bangalore', 13.046795, 77.628389, 'Electronics', 'free', 3.3, 76, ''),
    ('Al Zam Zam', 'Nagawara, Bangalore', 13.048816, 77.629364, 'Food & Grocery', 'silver', 3.7, 247, ''),
    ('Payal Medical', 'Nagawara, Bangalore', 13.049349, 77.630047, 'Health & Wellness', 'free', 3.1, 10, ''),
    ('Sri Rameshwara Cafe', 'Nagawara, Bangalore', 13.051846, 77.631222, 'Food & Grocery', 'silver', 4.0, 174, ''),
    ('The Taste of Bengal', 'Nagawara, Bangalore', 13.041386, 77.621659, 'Food & Grocery', 'silver', 3.6, 244, ''),
    ('Kakatiya Foods', 'Nagawara, Bangalore', 13.041387, 77.621779, 'Food & Grocery', 'free', 3.0, 34, ''),
    ('Yelhanka Cafe Cofee Day', 'Yelahanka, Bangalore', 13.099244, 77.588049, 'Food & Grocery', 'bronze', 3.1, 131, ''),
    ('Chandra cafe', 'Yelahanka, Bangalore', 13.099742, 77.588778, 'Food & Grocery', 'free', 3.0, 9, ''),
    ('CornerHouse', 'Yelahanka, Bangalore', 13.0989, 77.587649, 'Food & Grocery', 'bronze', 3.9, 180, ''),
    ('Idli Express', 'Yelahanka, Bangalore', 13.095043, 77.594212, 'Food & Grocery', 'bronze', 3.6, 137, ''),
    ('Rathna Kushka Center', 'Yelahanka, Bangalore', 13.100573, 77.588172, 'Food & Grocery', 'free', 3.0, 50, ''),
    ('Ganesh Uphaar', 'Shivanahalli main road, Yelahanka, Bangalore', 13.093388, 77.600726, 'Food & Grocery', 'bronze', 3.6, 104, ''),
    ('Subway / Polar Bear', 'Yelahanka, Bangalore', 13.099474, 77.589129, 'Food & Grocery', 'silver', 3.8, 270, ''),
    ('Mast Kalandar', 'Yelahanka, Bangalore', 13.099424, 77.589182, 'Food & Grocery', 'gold', 4.6, 1317, ''),
    ('Skanda Sagar', 'Yelahanka, Bangalore', 13.098795, 77.596965, 'Food & Grocery', 'free', 3.0, 25, ''),
    ("Papa John's", '366/4, Bhagyada Bagilu Road, Yelahanka, Bangalore', 13.099858, 77.59764, 'Food & Grocery', 'bronze', 3.2, 121, '+918197906655'),
    ('Popeyes', 'Yelahanka, Bangalore', 13.094315, 77.595033, 'Food & Grocery', 'platinum', 4.3, 4522, ''),
    ('Plum Goodness', 'Yelahanka, Bangalore', 13.094527, 77.59538, 'Beauty & Personal Care', 'platinum', 4.8, 3169, ''),
    ('Pai International', 'Yelahanka, Bangalore', 13.093505, 77.594013, 'Electronics', 'bronze', 3.8, 120, ''),
    ('Sri Kalabyraveshwara Tea Stall', 'Yelahanka, Bangalore', 13.105174, 77.600636, 'Food & Grocery', 'bronze', 3.9, 71, ''),
    ("Naati Style Gowda's Hotel", 'Yelahanka, Bangalore', 13.105164, 77.600604, 'Food & Grocery', 'silver', 3.9, 399, ''),
    ('Shopbox', 'Yelahanka, Bangalore', 13.106961, 77.600219, 'Food & Grocery', 'silver', 3.4, 422, ''),
    ('Alakapuri Veg Restaurant', 'Yelahanka, Bangalore', 13.103602, 77.599884, 'Food & Grocery', 'bronze', 3.7, 132, ''),
    ('CC Provision Store', 'Yelahanka, Bangalore', 13.106811, 77.601598, 'Food & Grocery', 'silver', 4.2, 85, ''),
    ('New Star & Style Hairdresser', 'Yelahanka, Bangalore', 13.106818, 77.601618, 'Beauty & Personal Care', 'gold', 4.5, 937, ''),
    ('Janani Upahar', 'Yelahanka, Bangalore', 13.106024, 77.598914, 'Food & Grocery', 'silver', 3.5, 205, ''),
    ('Nandini Cafe', 'Yelahanka, Bangalore', 13.106055, 77.598906, 'Food & Grocery', 'free', 3.4, 54, ''),
    ('Maa Sarala Vegetables and Fruits', 'Yelahanka, Bangalore', 13.1061, 77.598894, 'Food & Grocery', 'silver', 3.5, 356, ''),
    ('Santhosh Medicals', 'Yelahanka, Bangalore', 13.102321, 77.594802, 'Health & Wellness', 'bronze', 3.4, 149, ''),
    ('Charan Stores', 'Yelahanka, Bangalore', 13.102586, 77.595991, 'Clothing', 'gold', 4.6, 956, ''),
    ('Afiya Fashion Zari Works', 'Yelahanka, Bangalore', 13.102585, 77.595962, 'Clothing', 'bronze', 3.8, 112, ''),
    ('VM Collections', 'Yelahanka, Bangalore', 13.102508, 77.595932, 'Clothing', 'silver', 4.2, 354, ''),
    ('Goa Tailors', 'Yelahanka, Bangalore', 13.102445, 77.595649, 'Clothing', 'silver', 3.5, 443, ''),
    ('Indian Gents Tailor', 'Yelahanka, Bangalore', 13.102505, 77.595543, 'Clothing', 'free', 3.4, 21, ''),
    ('Royal Hair Style', 'Yelahanka, Bangalore', 13.102505, 77.595531, 'Beauty & Personal Care', 'silver', 3.4, 380, ''),
    ('Hemant Textiles', 'Yelahanka, Bangalore', 13.102287, 77.595266, 'Clothing', 'silver', 3.8, 435, ''),
    ('Sri Yashaswini Tindi Mane', 'Yelahanka, Bangalore', 13.102287, 77.595242, 'Food & Grocery', 'bronze', 3.8, 47, ''),
    ('Royal Medical', 'Yelahanka, Bangalore', 13.102311, 77.595063, 'Health & Wellness', 'free', 3.0, 65, ''),
    ('Ayesha Fashion Centre', 'Yelahanka, Bangalore', 13.10231, 77.594929, 'Clothing', 'silver', 4.0, 402, ''),
    ('specsmakers', 'Yelahanka, Bangalore', 13.098077, 77.59647, 'Health & Wellness', 'silver', 3.8, 250, ''),
    ('Biryani Paradise', 'Yelahanka, Bangalore', 13.098271, 77.596617, 'Food & Grocery', 'bronze', 3.7, 196, ''),
    ('Brahma Sri Sai Tea & Juice center', 'Yelahanka, Bangalore', 13.093478, 77.589403, 'Food & Grocery', 'bronze', 3.3, 26, ''),
    ('64 Chai Bar', 'Yelahanka, Bangalore', 13.093539, 77.589478, 'Food & Grocery', 'bronze', 3.3, 41, ''),
    ('Apollo Pharmacy', 'Yelahanka, Bangalore', 13.099627, 77.597945, 'Health & Wellness', 'silver', 3.5, 423, ''),
    ('Bluebrick Cafe', 'Yelahanka, Bangalore', 13.09534, 77.588724, 'Food & Grocery', 'free', 3.0, 58, ''),
    ('Sri Manjunatha Tiffen Center', 'Yelahanka, Bangalore', 13.10285, 77.59985, 'Food & Grocery', 'silver', 3.9, 228, ''),
    ('Donne Biriyani Mess', 'Yelahanka, Bangalore', 13.10265, 77.599775, 'Food & Grocery', 'bronze', 3.4, 198, ''),
    ('H.K.G.N Car Accessories', 'Yelahanka, Bangalore', 13.100358, 77.599233, 'Automotive', 'bronze', 3.1, 31, ''),
    ('Continental', 'Yelahanka, Bangalore', 13.100024, 77.599085, 'Automotive', 'gold', 3.8, 1839, ''),
    ('S.A Puncture Shop', 'Yelahanka, Bangalore', 13.099055, 77.598826, 'Automotive', 'bronze', 3.1, 62, ''),
    ('Supreme Gardenia', 'Yelahanka, Bangalore', 13.098998, 77.598809, 'Food & Grocery', 'gold', 4.4, 1156, ''),
    ('Skyway', 'Yelahanka, Bangalore', 13.097514, 77.598281, 'Automotive', 'free', 2.9, 71, ''),
    ('Mahindra', 'Yelahanka, Bangalore', 13.09502, 77.597287, 'Automotive', 'bronze', 3.5, 123, ''),
    ('Vishnu Medicals', 'Yelahanka, Bangalore', 13.102283, 77.595341, 'Health & Wellness', 'silver', 3.6, 386, ''),
    ('VSB Prasad Gents Beauty Saloon', 'Yelahanka, Bangalore', 13.102839, 77.596801, 'Beauty & Personal Care', 'bronze', 3.0, 177, ''),
    ('Ilyas', 'Yelahanka, Bangalore', 13.102765, 77.596947, 'Clothing', 'gold', 4.1, 979, ''),
    ('Beauty Castle', 'Yelahanka, Bangalore', 13.102817, 77.597206, 'Beauty & Personal Care', 'bronze', 3.7, 72, ''),
    ('Mahalakshmi Medical', 'Yelahanka, Bangalore', 13.102825, 77.597258, 'Health & Wellness', 'free', 3.4, 8, ''),
    ('A.R Footware', 'Yelahanka, Bangalore', 13.103094, 77.597693, 'Clothing', 'silver', 4.2, 324, ''),
    ('Nisha Bakery & Sweets', 'Yelahanka, Bangalore', 13.10311, 77.597758, 'Food & Grocery', 'free', 3.0, 58, ''),
    ('Yashwini Ladies Tailors', 'Yelahanka, Bangalore', 13.102982, 77.597826, 'Clothing', 'free', 3.4, 62, ''),
    ('Shuddh', 'Yelahanka, Bangalore', 13.103184, 77.59791, 'Food & Grocery', 'bronze', 3.6, 172, ''),
    ('Hot Chips', 'Yelahanka, Bangalore', 13.103049, 77.598062, 'Food & Grocery', 'bronze', 3.4, 49, ''),
    ('Mataji Medical and General Stores', 'Yelahanka, Bangalore', 13.103069, 77.598093, 'Health & Wellness', 'bronze', 3.8, 178, ''),
    ('Simple Tiffins', 'Yelahanka, Bangalore', 13.103278, 77.598638, 'Food & Grocery', 'free', 3.4, 38, ''),
    ('Halal Chicken And Mutton Shop', 'Yelahanka, Bangalore', 13.104437, 77.593209, 'Food & Grocery', 'gold', 4.3, 1476, ''),
    ('Chai Victory', 'Yelahanka, Bangalore', 13.104964, 77.593142, 'Food & Grocery', 'free', 2.8, 39, ''),
    ('Mashallah Biryani Hotel', 'Yelahanka, Bangalore', 13.105082, 77.592873, 'Food & Grocery', 'free', 3.1, 18, ''),
    ('Smart Cafe', 'Yelahanka, Bangalore', 13.104342, 77.595093, 'Food & Grocery', 'bronze', 3.4, 55, ''),
    ("Decent Men's Saloon", 'Yelahanka, Bangalore', 13.10442, 77.594893, 'Beauty & Personal Care', 'silver', 3.8, 299, ''),
    ('Ganesh Tea Stall', 'Yelahanka, Bangalore', 13.105659, 77.592381, 'Food & Grocery', 'silver', 4.1, 380, ''),
    ('Taj Chicken Center', 'Yelahanka, Bangalore', 13.104066, 77.602441, 'Food & Grocery', 'gold', 3.9, 441, ''),
    ('Jan Aushadhi', 'Yelahanka, Bangalore', 13.099848, 77.596204, 'Health & Wellness', 'bronze', 3.1, 100, ''),
    ('Dolphins Bar and Kitchen 1977', 'Yelahanka, Bangalore', 13.092761, 77.59374, 'Food & Grocery', 'gold', 4.3, 1872, ''),
    ('Udupi Grand', 'Yelahanka, Bangalore', 13.109145, 77.602978, 'Food & Grocery', 'bronze', 3.0, 54, ''),
    ('AMC Used Cars', 'Yelahanka, Bangalore', 13.104762, 77.601518, 'Automotive', 'free', 3.1, 71, ''),
    ('Cromā', 'Yelahanka, Bangalore', 13.092013, 77.593858, 'Electronics', 'bronze', 3.5, 195, ''),
    ('Absolute Barbeque', 'Yelahanka, Bangalore', 13.092018, 77.593693, 'Food & Grocery', 'bronze', 3.7, 133, ''),
    ('Aishwarya Bakery', 'Hennur, Bangalore', 13.037202, 77.64116, 'Food & Grocery', 'bronze', 3.1, 27, ''),
    ('DMart', 'Hennuru - Bagaluru Road, Hennur, Bangalore', 13.033446, 77.637452, 'Food & Grocery', 'gold', 4.5, 551, ''),
    ('Bake n Bistro', 'Hennur, Bangalore', 13.049136, 77.645391, 'Food & Grocery', 'free', 3.0, 68, ''),
    ('Adishwar', 'Hennur, Bangalore', 13.03379, 77.638741, 'Electronics', 'free', 3.5, 72, ''),
    ('Yofee Beauty Salon', 'Hennur, Bangalore', 13.037665, 77.641569, 'Beauty & Personal Care', 'silver', 4.2, 331, ''),
    ('Moriz', 'Hennur, Bangalore', 13.046662, 77.644581, 'Food & Grocery', 'bronze', 3.6, 88, ''),
    ('Star Market', 'Hennur, Bangalore', 13.048137, 77.645531, 'Food & Grocery', 'silver', 4.2, 237, ''),
    ('Reliance Fresh', 'Hennur, Bangalore', 13.047525, 77.644399, 'Food & Grocery', 'bronze', 3.8, 132, ''),
    ('Karnataka Biryani Point', 'Hennur, Bangalore', 13.049076, 77.645719, 'Food & Grocery', 'bronze', 4.0, 46, ''),
    ('Pearl Vision Optics', 'Hennur, Bangalore', 13.050082, 77.645346, 'Health & Wellness', 'silver', 4.1, 176, ''),
    ('Ayurcentral', 'Hennur, Bangalore', 13.03187, 77.635908, 'Food & Grocery', 'free', 3.7, 57, ''),
    ('Chinese Bowl', 'Hennur, Bangalore', 13.036923, 77.640821, 'Food & Grocery', 'free', 3.6, 11, ''),
    ('Brahma Sri Udupi Veg', 'Hennur, Bangalore', 13.038103, 77.641294, 'Food & Grocery', 'bronze', 4.0, 80, ''),
    ('Leela Bakery', 'Hennur, Bangalore', 13.04954, 77.645238, 'Food & Grocery', 'free', 3.4, 44, ''),
    ('Nadumuttam', 'Hennur, Bangalore', 13.049933, 77.645364, 'Food & Grocery', 'gold', 4.3, 976, ''),
    ('Sagar Veg', 'Hennur, Bangalore', 13.047709, 77.644541, 'Food & Grocery', 'free', 2.8, 26, ''),
    ('My Chicken and More', 'Hennur, Bangalore', 13.047843, 77.644633, 'Food & Grocery', 'gold', 4.5, 1267, ''),
    ('Skoda', 'Hennur, Bangalore', 13.046761, 77.644817, 'Automotive', 'bronze', 3.3, 157, ''),
    ('Coffee Mechanics', 'Hennur, Bangalore', 13.034005, 77.638335, 'Food & Grocery', 'free', 3.7, 37, ''),
    ('Andhra Spice', 'Hennur, Bangalore', 13.047577, 77.644476, 'Food & Grocery', 'free', 3.0, 65, ''),
    ('Ambur Hot Dum Biryani', 'Hennur, Bangalore', 13.047966, 77.645269, 'Food & Grocery', 'platinum', 4.7, 1280, ''),
    ('Zatar', 'Hennur, Bangalore', 13.048576, 77.64573, 'Food & Grocery', 'free', 3.5, 52, ''),
    ('Huddle Coffee Co.', 'Hennur, Bangalore', 13.040128, 77.634317, 'Food & Grocery', 'gold', 4.7, 1875, ''),
    ('Krishna Priya Bakery', 'Hennur, Bangalore', 13.051347, 77.634892, 'Food & Grocery', 'silver', 4.1, 117, ''),
    ('Moba', 'Hennur, Bangalore', 13.039295, 77.641594, 'Food & Grocery', 'free', 3.4, 31, ''),
    ('Cafe Nuvio', 'Hennur, Bangalore', 13.04882, 77.645764, 'Food & Grocery', 'silver', 4.1, 300, ''),
    ('Geojit Financial Services', 'Margosa Road, Malleswaram, Bangalore', 13.00379, 77.569187, 'Electronics', 'free', 3.3, 80, ''),
    ('Hallimane', 'Malleswaram, Bangalore', 12.995526, 77.57177, 'Food & Grocery', 'bronze', 3.5, 182, ''),
    ("Adiga's", 'Sampige Road, Malleswaram, Bangalore', 13.00557, 77.571351, 'Food & Grocery', 'silver', 4.2, 256, ''),
    ('Reliance smart', 'Malleswaram, Bangalore', 13.006198, 77.569374, 'Food & Grocery', 'bronze', 3.3, 114, ''),
    ('Kolkata Chaat', 'Sampige Road, Malleswaram, Bangalore', 12.995328, 77.571293, 'Food & Grocery', 'bronze', 3.5, 168, ''),
    ('Udupi Sri Krishnarajathadri', 'Sampige Road, Malleshwaram, Malleswaram, Bangalore', 13.0041, 77.571391, 'Food & Grocery', 'free', 3.5, 11, ''),
    ("Chung's", 'Malleswaram, Bangalore', 13.00654, 77.569283, 'Food & Grocery', 'silver', 3.9, 390, ''),
    ('Sai Chats', 'Malleswaram, Bangalore', 13.006727, 77.564034, 'Food & Grocery', 'silver', 4.3, 457, ''),
    ('Pushyamee', '8th Main, Malleswaram, Bangalore', 13.000625, 77.565242, 'Health & Wellness', 'bronze', 3.2, 129, ''),
    ("Baker's Hut", 'Malleswaram, Bangalore', 13.000206, 77.569766, 'Food & Grocery', 'gold', 4.2, 761, ''),
    ('Manipal North Side Pharmacy', 'Malleswaram, Bangalore', 13.000972, 77.564026, 'Health & Wellness', 'gold', 4.0, 1283, ''),
    ('Rasa', 'Sampige Road, Malleswaram, Bangalore', 12.994359, 77.571176, 'Food & Grocery', 'free', 3.6, 43, ''),
    ('Naturals Unisex Salon', 'Malleswaram, Bangalore', 13.007567, 77.578061, 'Beauty & Personal Care', 'gold', 4.5, 194, ''),
    ('Keshava Medical & General Stores', 'Malleswaram, Bangalore', 13.005908, 77.578507, 'Health & Wellness', 'free', 3.5, 58, ''),
    ('Shree Sagar CTR', 'Malleswaram, Bangalore', 12.998257, 77.569495, 'Food & Grocery', 'free', 3.6, 20, ''),
    ('Olympic Sports', 'Malleswaram, Bangalore', 13.004475, 77.571194, 'Sports', 'bronze', 3.0, 129, ''),
    ('Parota Point', 'Malleswaram, Bangalore', 13.0088, 77.56341, 'Food & Grocery', 'free', 3.4, 16, ''),
    ('Bun World', 'Malleswaram, Bangalore', 13.007503, 77.563566, 'Food & Grocery', 'gold', 4.0, 1890, ''),
    ('Sri Ganesh Medicals', '8th Main Road, Malleswaram, Bangalore', 13.007711, 77.56404, 'Health & Wellness', 'gold', 4.0, 1440, ''),
    ('Homely Organic', 'Malleswaram, Bangalore', 13.004309, 77.565331, 'Food & Grocery', 'bronze', 3.1, 190, ''),
    ('Food Camp', 'Malleswaram, Bangalore', 13.00093, 77.572369, 'Food & Grocery', 'gold', 3.9, 911, ''),
    ('Sri Sai Dosa Center', 'Malleswaram, Bangalore', 13.001083, 77.572139, 'Food & Grocery', 'silver', 4.1, 436, ''),
    ("Iyengar's Bakery", 'Malleswaram, Bangalore', 13.001104, 77.5723, 'Food & Grocery', 'free', 3.4, 12, ''),
    ('Amrut Ice-cream', 'Malleswaram, Bangalore', 13.001853, 77.567194, 'Food & Grocery', 'free', 3.2, 16, ''),
    ('Sai Shakti', 'Malleswaram, Bangalore', 12.999295, 77.568061, 'Food & Grocery', 'silver', 3.9, 277, ''),
    ('Sri Udupi Swaadista', '1st Main Road, Malleswaram, Bangalore', 13.005511, 77.578757, 'Food & Grocery', 'free', 3.4, 15, ''),
    ('Maha Bazaar', 'Malleswaram, Bangalore', 13.004927, 77.578734, 'Food & Grocery', 'platinum', 4.4, 1582, ''),
    ('Rajesh Hotel', 'Malleswaram, Bangalore', 13.004618, 77.578455, 'Food & Grocery', 'bronze', 3.2, 42, ''),
    ('Religare Medicals', 'Malleswaram, Bangalore', 13.000222, 77.568817, 'Health & Wellness', 'silver', 3.5, 210, ''),
    ('Hotel Janatha', 'Malleswaram, Bangalore', 12.999143, 77.571094, 'Food & Grocery', 'bronze', 3.3, 117, ''),
    ("Sunil's", 'Malleswaram, Bangalore', 13.00571, 77.571356, 'Clothing', 'silver', 4.2, 118, ''),
    ('Bajji', 'Malleswaram, Bangalore', 13.003732, 77.565332, 'Food & Grocery', 'silver', 4.1, 85, ''),
    ('Manjunatha Provisions', 'Malleswaram, Bangalore', 12.999291, 77.567148, 'Food & Grocery', 'bronze', 3.0, 186, ''),
    ('Ganesha Provisions', 'Malleswaram, Bangalore', 12.999286, 77.567003, 'Food & Grocery', 'free', 3.1, 27, ''),
    ('Safal', 'Malleswaram, Bangalore', 12.99914, 77.567835, 'Food & Grocery', 'silver', 3.6, 260, ''),
    ('Apple Pharma', 'Malleswaram, Bangalore', 12.999782, 77.568189, 'Health & Wellness', 'platinum', 4.6, 4124, ''),
    ('New Pushyamee', 'Malleswaram, Bangalore', 13.003771, 77.565322, 'Health & Wellness', 'gold', 3.8, 305, ''),
    ('Janhavi Medicals', 'Malleswaram, Bangalore', 13.004516, 77.578588, 'Health & Wellness', 'free', 2.8, 49, ''),
    ('Maiyas', 'Malleswaram, Bangalore', 13.002028, 77.570746, 'Food & Grocery', 'bronze', 3.4, 55, ''),
    ('Sri Mukambika', 'Malleswaram, Bangalore', 12.996297, 77.571925, 'Food & Grocery', 'free', 3.3, 31, ''),
    ('Aditya Hotel', 'Malleswaram, Bangalore', 13.001933, 77.57844, 'Food & Grocery', 'silver', 4.0, 361, ''),
    ('Five Star Veg', 'Malleswaram, Bangalore', 13.006993, 77.569322, 'Food & Grocery', 'free', 3.6, 3, ''),
    ('Leela medicals', 'Malleswaram, Bangalore', 13.000323, 77.569374, 'Health & Wellness', 'gold', 4.4, 717, ''),
    ('Kanchipuram Varamahalakshmi silks', 'Malleswaram, Bangalore', 12.999115, 77.569533, 'Clothing', 'free', 3.3, 24, ''),
    ('Five Star Chicken', 'Malleswaram, Bangalore', 13.007093, 77.569317, 'Food & Grocery', 'silver', 3.7, 77, ''),
    ('Mohan Bhandar', 'Malleswaram, Bangalore', 13.001859, 77.571135, 'Food & Grocery', 'silver', 4.2, 172, ''),
    ('Airtel Express', 'Malleswaram, Bangalore', 13.001455, 77.572218, 'Electronics', 'bronze', 3.1, 147, ''),
    ('Atithis', 'Malleswaram, Bangalore', 13.00176, 77.578323, 'Food & Grocery', 'silver', 3.5, 56, ''),
    ("Shree Yummy's Palace", 'Sampige Road, Malleswaram, Bangalore', 13.007601, 77.571072, 'Food & Grocery', 'platinum', 4.9, 1581, ''),
    ('Al Bek', 'Sampige Road, Malleswaram, Bangalore', 12.994274, 77.571582, 'Food & Grocery', 'bronze', 3.8, 127, '+918023461077'),
    ('Asha Sweets', 'Malleswaram, Bangalore', 12.99952, 77.571152, 'Food & Grocery', 'bronze', 3.7, 140, ''),
    ('Shristi Sagar', '8th Main Road, Malleswaram, Bangalore', 13.007676, 77.564052, 'Food & Grocery', 'platinum', 4.8, 636, ''),
    ('Recharge Only', '8th Main Road, Malleswaram, Bangalore', 13.007887, 77.563961, 'Electronics', 'silver', 3.4, 160, ''),
    ('Pakvan', 'Malleswaram, Bangalore', 13.003392, 77.578368, 'Food & Grocery', 'free', 2.8, 50, ''),
    ('Oye Lassi', 'Malleswaram, Bangalore', 13.005357, 77.571241, 'Food & Grocery', 'bronze', 3.6, 148, ''),
    ('Reddy Electricals', '596, 15th Cross Road, Malleswaram, Bangalore', 13.005564, 77.571391, 'Electronics', 'silver', 3.8, 255, ''),
    ('Udupi Upachaar Veg', '33, 15th Cross Road, Malleswaram, Bangalore', 13.005408, 77.569105, 'Food & Grocery', 'free', 3.2, 78, ''),
    ("Cut 'N' style", 'Malleswaram, Bangalore', 13.008478, 77.563691, 'Beauty & Personal Care', 'free', 2.8, 10, ''),
    ('Mumbai Spice', '18/2, 10th Cross, Malleswaram, Malleswaram, Bangalore', 13.000287, 77.571847, 'Food & Grocery', 'bronze', 3.2, 59, '+919880055571'),
    ('Asha Sweet Center', '405, Sampige Road, Malleswaram, Bangalore', 13.000756, 77.572369, 'Food & Grocery', 'bronze', 3.4, 182, '+918042067320'),
    ("Chandu's Deluxe", '10, 3, 8th Cross Road, Malleswaram, Bangalore', 12.999211, 77.572276, 'Food & Grocery', 'silver', 4.0, 379, '+918065996565'),
    ('Sri Venkateshwara Hotel', '2nd, Temple Street, Malleswaram, Bangalore', 13.002675, 77.572053, 'Food & Grocery', 'free', 3.4, 30, '+918049652659'),
    ('New Fishland', '59, 3rd Temple Road, Malleswaram, Bangalore', 12.997296, 77.571834, 'Food & Grocery', 'free', 2.8, 55, '+918553464945'),
    ('Srushti Coffee', 'food court,3rd floor,mantri square mall, 8th Main Road, Malleswaram, Bangalore', 13.00764, 77.564017, 'Food & Grocery', 'free', 3.3, 7, '+919845687778'),
    ("Chetty's Corner", '35/2, 16th Cross Road, Malleswaram, Bangalore', 13.006733, 77.564266, 'Food & Grocery', 'bronze', 3.7, 64, '+919844095576'),
    ('Hotel Ayodhya Upahar', 'Opposite Vyatikal Police Station, Vyalikaval Main Road, Malleswaram, Bangalore', 13.001894, 77.578475, 'Food & Grocery', 'free', 3.4, 1, '+919620056546'),
    ('Brahmins Thatte Idli', '133, 8th Main Road,Malleshwaram, Malleswaram, Bangalore', 13.008246, 77.563189, 'Food & Grocery', 'free', 3.7, 66, '+919900644777'),
    ('1947 Restaurant', '5, 4th Floor, Above GIRIAS, 15th Cross Road, Malleswaram, Bangalore', 13.005657, 77.569098, 'Food & Grocery', 'bronze', 3.8, 86, '+918026791213'),
    ('Keshava Medicals And General Stores', '33, 2nd Main Road, Near-Pizza Hut, Vyalikaval, Malleswaram, Bangalore', 13.005735, 77.576393, 'Health & Wellness', 'free', 2.8, 34, '+918041138233'),
    ('Sri Ganesh Medicals And Surgicals', '112, 8th Main, 18th Cross, H V Nanjundaiah Rd, Malleshwaram, Malleswaram, Bangalore', 13.008189, 77.563848, 'Health & Wellness', 'silver', 3.5, 65, '+918023312346'),
    ('Pushyamee Medicals', '19, 8th Main Rd, Malleshwaram, Malleswaram, Bangalore', 13.000855, 77.56317, 'Health & Wellness', 'bronze', 3.4, 154, '+918023565016'),
    ('Hotel Moon Light', '49, 5th Cross, 4th Main,Malleshwaram, Malleswaram, Bangalore', 12.996694, 77.568723, 'Food & Grocery', 'free', 3.4, 73, '+918023442993'),
    ('Pizza stop', '268, Near 18th Cross, Sampige Road, Malleshwaram, Malleswaram, Bangalore', 13.008186, 77.570985, 'Food & Grocery', 'silver', 3.9, 311, '+917760830320'),
    ('Megha Medicals', '40, 12th Cross, Kodandarampura, Near-Prasidh Clinic, Malleshwaram, Malleswaram, Bangalore', 13.002329, 77.571917, 'Health & Wellness', 'bronze', 3.5, 165, '+918023347034'),
    ('Iyer Mess', '4, Near Devi Prasad Home Appliances, 7th & 8th Cross, West Park RD, Malleshwaram, Malleswaram, Bangalore', 12.998707, 77.569905, 'Food & Grocery', 'free', 2.9, 13, '+918023343418'),
    ('Isha Diagnostics', '311, 16th Cross, Sampige Road,, Malleswaram, Bangalore', 13.005889, 77.571338, 'Health & Wellness', 'gold', 4.4, 336, '+918023562111'),
    ('Antarastriya', '7th Cross, Sampige Road, Malleshwaram, Malleswaram, Bangalore', 12.998234, 77.570518, 'Food & Grocery', 'free', 3.2, 5, '+919741412613'),
    ('Big Straw Boba Bistro', '139, 4th Floor, 8th Main, Above Nilgiris, Near Bumble Bee Studio, Malleshwaram, Malleswaram, Bangalore', 13.005, 77.565424, 'Food & Grocery', 'gold', 3.7, 194, '+919945164425'),
    ('Sri Raghavendra Store', 'Opposite Manipal North Side Hospital, Near Malleshwaram Railway Station, Malleshwaram, Malleswaram, Bangalore', 13.000757, 77.563817, 'Food & Grocery', 'bronze', 3.4, 58, '+918023348477'),
    ('Satvik Grand', '147, 7th Cross, Near Malleswaram Association, Below Axis Bank, Malleshwaram, Malleswaram, Bangalore', 12.998541, 77.569532, 'Food & Grocery', 'free', 3.7, 4, ''),
    ('Suchi ruchi veg', '92, 1st Main, 10th Cross Temple Road, Sampige Road, Malleshwaram, Malleswaram, Bangalore', 13.001248, 77.572348, 'Food & Grocery', 'silver', 4.3, 429, '+91919448804902'),
    ('Janhavi Medicals Malleshwaram', '42, Vyalikaval, 2nd Main, Opposite-Rajesh Hotel, Sadashiv Nagar, Malleswaram, Bangalore', 13.001945, 77.571624, 'Health & Wellness', 'silver', 3.6, 177, '+918023366503'),
    ('K C G Pharma And Generals', '17, 1, 3rd Cross Rd, Malleshwaram, Malleswaram, Bangalore', 13.002008, 77.571391, 'Health & Wellness', 'bronze', 3.7, 89, '+919448511024'),
    ('A1 Biryani Restaurant', '502, 3rd Cross, Sampige Road, Malleshwaram, Malleswaram, Bangalore', 12.996195, 77.571592, 'Food & Grocery', 'gold', 4.3, 344, '+917411144480'),
    ('Medall Clumax Diagnostics', 'Gayathri Devi Park Extension, Kodandarampura, Malleswaram, Bangalore', 13.005882, 77.576159, 'Health & Wellness', 'bronze', 3.9, 65, '+918049337777'),
    ('Holige Mane', '27, 10th Cross Rd, Yalappa Garden, Malleshwaram, Bengaluru, Malleswaram, Bangalore', 13.001128, 77.572406, 'Food & Grocery', 'free', 3.2, 5, '+919731731105'),
    ('Roots Advanced Hair Transplant Center', '16, 12th cross, Palace Guttahalli main road, Malleshwaram, Malleswaram, Bangalore', 13.002228, 77.573922, 'Health & Wellness', 'platinum', 4.1, 4310, '+919482166333'),
    ('viking', '136, Near MES College, 8th Main, 15th Cross,\xa0Malleshwaram, Malleswaram, Bangalore', 13.006544, 77.564774, 'Food & Grocery', 'gold', 4.5, 736, '+918023444411'),
    ('Lawrence and Mayo', '40, Margosa Road, Malleswaram, Bangalore', 12.997715, 77.569541, 'Health & Wellness', 'bronze', 3.6, 22, '+(91)8023311392'),
    ('Rakhra Sports House', '72/4, 16th Cross, Vyalikaval, Malleswaram, Near Sankey Tank And Chowdiah Memorial Hall, Bhadrappa Layout, Vyalikaval, Kodandarampura, Malleshwaram, Bengaluru, Karnataka 560003, Malleswaram, Bangalore', 13.006221, 77.574224, 'Sports', 'bronze', 3.3, 97, '+918023342909'),
    ('olympic the sports shop', '224, Sampige Road, Opposite Sai Temple, Sampige Road, Bhadrappa Layout, Malleshwaram, Malleswaram, Bangalore', 12.997418, 77.572179, 'Sports', 'silver', 3.6, 238, '+918023464839'),
    ('Roots Beauty Clinic', 'No23, Palace Guttahalli Main Road, Malleswaram, Malleswaram, Bangalore', 12.999352, 77.576678, 'Beauty & Personal Care', 'silver', 3.9, 445, '+918023441974'),
    ('Bismilla Cycle Mart', '2904, Mahakavi Kuvempu Rd, Bhadrappa Layout, Mariappanapalya, Rajaji Nagar, Malleswaram, Bangalore', 12.998231, 77.558632, 'Sports', 'silver', 4.1, 329, '+919686254320'),
    ('Girias', 'Malleswaram, Bangalore', 13.005641, 77.569136, 'Electronics', 'silver', 3.6, 53, ''),
    ('Al-Bek', 'Malleswaram, Bangalore', 12.996698, 77.561332, 'Food & Grocery', 'gold', 3.7, 1209, ''),
    ('ಬಿಸ್ಕ', '15th Cross Road, Malleswaram, Bangalore', 13.005537, 77.571118, 'Food & Grocery', 'bronze', 3.8, 22, ''),
    ('Veena Stores', '183, 15th Cross, Malleshwaram, Margosa Road, Malleshwaram West, Bengaluru, Karnataka 560003, Malleswaram, Bangalore', 13.00567, 77.569289, 'Food & Grocery', 'platinum', 4.0, 3582, '+918023344838'),
    ('Boston Cotton', 'Malleswaram, Bangalore', 13.008727, 77.564085, 'Clothing', 'gold', 4.4, 312, ''),
    ('Tan Shoes', 'Malleswaram, Bangalore', 12.99696, 77.561356, 'Clothing', 'free', 3.7, 53, ''),
    ('SLN Food Point', 'Malleswaram, Bangalore', 12.996199, 77.565134, 'Food & Grocery', 'bronze', 3.1, 56, ''),
    ("Malgudi's Donne Biryani", 'Malleswaram, Bangalore', 12.996993, 77.56134, 'Food & Grocery', 'silver', 3.5, 168, ''),
    ('Variety Footware', 'Malleswaram, Bangalore', 12.996174, 77.564329, 'Clothing', 'gold', 4.2, 1535, ''),
    ('New Karavali Bar & Restaurant', 'Malleswaram, Bangalore', 12.993553, 77.571296, 'Food & Grocery', 'gold', 4.2, 1631, ''),
    ('Maruti Suzuki Arena', 'Malleswaram, Bangalore', 13.005396, 77.567103, 'Automotive', 'free', 2.8, 17, ''),
    ('Aramane Donne Biriyani', 'Malleswaram, Bangalore', 12.997907, 77.569536, 'Food & Grocery', 'silver', 3.8, 353, ''),
    ('Rachoteshwar Communication', 'Malleswaram, Bangalore', 12.999913, 77.571399, 'Electronics', 'silver', 3.8, 116, '+918041626111'),
    ('Malavalli Metal Mart', 'Malleswaram, Bangalore', 12.99617, 77.564103, 'Hardware', 'free', 3.0, 40, ''),
    ('Coffee Break', 'Malleswaram, Bangalore', 13.008752, 77.569395, 'Food & Grocery', 'gold', 4.1, 1803, ''),
    ('Green Trends', 'Malleswaram, Bangalore', 13.003575, 77.569103, 'Beauty & Personal Care', 'silver', 3.7, 189, ''),
    ('Savera Hang Out', 'Malleswaram, Bangalore', 12.995986, 77.568535, 'Food & Grocery', 'free', 3.6, 17, ''),
    ('Grill Biryanis', 'Malleswaram, Bangalore', 12.996759, 77.562864, 'Food & Grocery', 'silver', 4.0, 129, ''),
    ('Naturals', 'Malleswaram, Bangalore', 13.002906, 77.56921, 'Beauty & Personal Care', 'free', 3.1, 40, ''),
    ('Prashanti', 'Malleswaram, Bangalore', 12.995382, 77.571585, 'Clothing', 'bronze', 3.5, 73, ''),
    ('Hop Salon', 'Malleswaram, Bangalore', 12.994627, 77.571327, 'Beauty & Personal Care', 'bronze', 3.5, 50, ''),
    ('Rangachari Textiles', 'Malleswaram, Bangalore', 12.994476, 77.571618, 'Clothing', 'free', 3.5, 38, ''),
    ('Hyderabadi Biryani', 'Malleswaram, Bangalore', 12.993852, 77.571378, 'Food & Grocery', 'gold', 4.4, 182, ''),
    ('Tynimo', 'Malleswaram, Bangalore', 12.993568, 77.571371, 'Food & Grocery', 'free', 3.6, 18, ''),
    ('Reliance Trends', 'Malleswaram, Bangalore', 13.001737, 77.572393, 'Clothing', 'free', 2.9, 45, ''),
    ('Kanchee-Co Swetha Silks', 'Malleswaram, Bangalore', 12.997057, 77.571548, 'Clothing', 'bronze', 3.0, 96, ''),
    ('Cut N Style', 'Malleswaram, Bangalore', 13.000195, 77.567912, 'Beauty & Personal Care', 'free', 3.7, 3, ''),
    ('Bhairaeshwara Idli Center', 'Malleswaram, Bangalore', 13.00083, 77.563794, 'Food & Grocery', 'platinum', 4.9, 2546, ''),
    ('Sri Raghavendra Dosa Camp', 'Malleswaram, Bangalore', 13.000853, 77.56391, 'Food & Grocery', 'free', 2.8, 17, ''),
    ('Third Wave Coffee', 'Malleswaram, Bangalore', 13.007009, 77.569026, 'Food & Grocery', 'free', 2.8, 28, ''),
    ('Honey Bee Bakers', 'Malleswaram, Bangalore', 13.007424, 77.578393, 'Food & Grocery', 'free', 3.5, 47, ''),
    ('Harmakki Coffee Company', '111, 8th Main Road, Malleswaram, Bangalore', 13.008555, 77.563546, 'Food & Grocery', 'silver', 4.3, 93, ''),
    ('Sri Angala Parameshwari Vegetables', 'Malleswaram, Bangalore', 12.998872, 77.573052, 'Food & Grocery', 'silver', 3.7, 466, ''),
    ('Sri Venkateswara Garments', 'Malleswaram, Bangalore', 12.999365, 77.571607, 'Clothing', 'free', 3.7, 25, ''),
    ('Goldwinn Textile', 'Malleswaram, Bangalore', 12.999369, 77.571566, 'Clothing', 'gold', 4.1, 363, ''),
    ("Namdhari's Fresh", 'Malleswaram, Bangalore', 13.003153, 77.578266, 'Food & Grocery', 'free', 3.5, 42, ''),
    ('Fresho Choice', 'Malleswaram, Bangalore', 13.001495, 77.578336, 'Food & Grocery', 'free', 3.6, 6, ''),
    ('Ramraj', 'Malleswaram, Bangalore', 12.998836, 77.571547, 'Clothing', 'free', 3.3, 50, ''),
    ('My La Pure', 'Malleswaram, Bangalore', 13.005734, 77.578841, 'Food & Grocery', 'bronze', 3.4, 129, ''),
    ('New Janardhan Bakery', 'Malleswaram, Bangalore', 12.997393, 77.573301, 'Food & Grocery', 'free', 2.9, 23, ''),
    ("La Pino'z Pizza", 'Malleswaram, Bangalore', 13.006125, 77.564702, 'Food & Grocery', 'free', 3.1, 19, ''),
    ('Namma MTR', 'Malleswaram, Bangalore', 13.001102, 77.571821, 'Food & Grocery', 'free', 3.3, 31, ''),
    ('Basaveshwara Khanavali', 'Malleswaram, Bangalore', 12.999394, 77.57225, 'Food & Grocery', 'silver', 3.9, 331, ''),
    ('Surya Book Stall', 'Malleswaram, Bangalore', 13.001086, 77.571242, 'Books', 'bronze', 3.1, 118, ''),
    ('Specs Makers', 'Malleswaram, Bangalore', 13.001439, 77.571435, 'Health & Wellness', 'free', 2.9, 67, ''),
    ('Paakashaala', 'Malleswaram, Bangalore', 13.005539, 77.571352, 'Food & Grocery', 'free', 3.7, 69, ''),
    ('P. N. Rao Suites', 'Malleswaram, Bangalore', 13.005701, 77.568832, 'Clothing', 'silver', 4.4, 450, ''),
    ('Laptop World', 'Malleswaram, Bangalore', 13.005339, 77.57065, 'Electronics', 'silver', 3.9, 450, ''),
    ("Ravi's Stationary", 'Malleswaram, Bangalore', 13.005346, 77.570737, 'Stationery', 'bronze', 3.9, 148, ''),
    ('By2Coffee', 'Malleswaram, Bangalore', 12.998396, 77.569534, 'Food & Grocery', 'bronze', 3.3, 26, ''),
    ('Vijaya Lakshmi', 'Malleswaram, Bangalore', 13.000978, 77.56924, 'Food & Grocery', 'bronze', 3.2, 91, ''),
    ('amani sarees', 'Malleswaram, Bangalore', 13.000729, 77.569218, 'Clothing', 'free', 3.7, 67, ''),
    ('Cottonking', 'Sampige Road, Malleswaram, Bangalore', 13.001803, 77.571216, 'Clothing', 'bronze', 3.6, 79, ''),
    ('Plants Garments', 'Malleswaram, Bangalore', 13.001786, 77.571206, 'Clothing', 'platinum', 4.1, 2475, ''),
    ('Nandini Stall', 'Sampige Road, Malleswaram, Bangalore', 13.002098, 77.5712, 'Food & Grocery', 'free', 3.5, 7, ''),
    ('Bosch', 'Sampige Road, Malleswaram, Bangalore', 13.006474, 77.571136, 'Electronics', 'silver', 4.3, 344, ''),
    ('Trends Unisex Salon', 'Malleswaram, Bangalore', 12.995804, 77.573642, 'Beauty & Personal Care', 'gold', 4.5, 826, ''),
    ('Sri Lankappa Provision Store', 'Malleswaram, Bangalore', 12.998423, 77.569495, 'Food & Grocery', 'gold', 4.7, 455, ''),
    ('Temple Meals', 'Malleswaram, Bangalore', 12.998429, 77.569537, 'Food & Grocery', 'free', 3.3, 45, ''),
    ('A.K. Hardware, Paints & Sanitary', 'Malleswaram, Bangalore', 12.998496, 77.569515, 'Hardware', 'gold', 3.7, 1534, ''),
    ('Purple Olive', 'Malleswaram, Bangalore', 12.998515, 77.569531, 'Clothing', 'bronze', 3.6, 56, ''),
    ('Fancy Enterprises', 'Malleswaram, Bangalore', 12.998661, 77.569588, 'Clothing', 'silver', 3.6, 464, ''),
    ('Kancheepuram VRK Silks', 'Malleswaram, Bangalore', 12.998901, 77.569503, 'Clothing', 'gold', 4.2, 732, ''),
    ('Sri Gayathri Mini Hall', 'Malleswaram, Bangalore', 12.998981, 77.569495, 'Clothing', 'free', 2.7, 7, ''),
    ("Sri Srinivas Iyengar's Bakery", 'Malleswaram, Bangalore', 13.009085, 77.563284, 'Food & Grocery', 'free', 2.9, 67, ''),
    ('Purity Prayag', 'Malleswaram, Bangalore', 13.00898, 77.563896, 'Food & Grocery', 'bronze', 3.3, 177, ''),
    ('Narayana Pharma', 'Malleswaram, Bangalore', 13.008497, 77.568503, 'Health & Wellness', 'free', 2.8, 22, ''),
    ('Stylo Boutique', 'Malleswaram, Bangalore', 12.993286, 77.568192, 'Clothing', 'gold', 4.5, 762, ''),
    ('Kavery Bakery', 'Malleswaram, Bangalore', 12.994936, 77.566292, 'Food & Grocery', 'free', 2.8, 18, ''),
    ('Adukale Malleshwaram Experience Store', 'Malleswaram, Bangalore', 12.998434, 77.570059, 'Food & Grocery', 'silver', 4.2, 490, ''),
    ('Airtel', 'Malleswaram, Bangalore', 13.008166, 77.571286, 'Electronics', 'bronze', 3.1, 200, ''),
    ('Nalli Silks', 'Malleswaram, Bangalore', 13.006991, 77.571305, 'Clothing', 'gold', 4.1, 306, ''),
    ('Five Star', 'Malleswaram, Bangalore', 12.996633, 77.568059, 'Food & Grocery', 'bronze', 3.3, 60, ''),
    ("Sathya's Food Joint", 'Malleswaram, Bangalore', 13.008157, 77.571289, 'Food & Grocery', 'silver', 3.8, 377, ''),
    ('Sri Sai Medicals', 'Malleswaram, Bangalore', 13.00312, 77.571373, 'Health & Wellness', 'platinum', 4.6, 1927, ''),
    ('Kati Roll And Momos', 'Malleswaram, Bangalore', 13.00667, 77.569295, 'Food & Grocery', 'silver', 4.0, 364, ''),
    ('Crazy Bites', 'Malleswaram, Bangalore', 13.007032, 77.569277, 'Food & Grocery', 'silver', 3.6, 351, ''),
    ('Sri Rajrajeshwari Iyer Mess', 'Malleswaram, Bangalore', 13.006676, 77.569294, 'Food & Grocery', 'gold', 4.1, 1603, ''),
    ('Royal Challenge Cotton Export', 'Malleswaram, Bangalore', 12.999158, 77.569502, 'Clothing', 'silver', 3.5, 423, ''),
    ("Women's Wear", 'Malleswaram, Bangalore', 12.999151, 77.569566, 'Clothing', 'free', 3.4, 73, ''),
    ('Sugar Cosmetics', 'Malleswaram, Bangalore', 13.001778, 77.571193, 'Beauty & Personal Care', 'bronze', 4.0, 165, ''),
    ('First Cry', 'Malleswaram, Bangalore', 13.002935, 77.569208, 'Toys & Baby', 'free', 3.3, 78, ''),
    ('Himalaya Wellness Company', 'Malleswaram, Bangalore', 13.006859, 77.571325, 'Beauty & Personal Care', 'free', 3.3, 63, ''),
    ("Haldiram's", 'Malleswaram, Bangalore', 13.003564, 77.571349, 'Food & Grocery', 'silver', 3.5, 431, ''),
    ('Sarvam Upahara', 'Malleswaram, Bangalore', 13.002433, 77.571175, 'Food & Grocery', 'free', 3.0, 40, ''),
    ('Asus Showroom', 'Malleswaram, Bangalore', 13.008195, 77.569111, 'Electronics', 'free', 3.6, 11, ''),
    ('Lenovo Computers', 'Malleswaram, Bangalore', 13.008126, 77.569114, 'Electronics', 'gold', 4.4, 1593, ''),
    ('Invogue Salon', 'Malleswaram, Bangalore', 13.00752, 77.569099, 'Beauty & Personal Care', 'gold', 4.1, 643, ''),
    ('ESN World Laptop Service Centre', 'Malleswaram, Bangalore', 13.007488, 77.569102, 'Electronics', 'free', 3.4, 44, ''),
    ('Golden Glam Costume Rental Company', 'Malleswaram, Bangalore', 13.007452, 77.569095, 'Clothing', 'free', 2.8, 0, ''),
    ('Yashvini Ayurveda', 'Malleswaram, Bangalore', 13.00536, 77.571289, 'Health & Wellness', 'free', 3.1, 53, ''),
    ('Giriram Foods ,Dairy Products And Edible Oil', 'Malleswaram, Bangalore', 13.003187, 77.569426, 'Food & Grocery', 'platinum', 4.9, 4539, ''),
    ('Liberty Exclusive Showroom', 'Malleswaram, Bangalore', 12.999858, 77.571578, 'Clothing', 'gold', 4.1, 868, ''),
    ('Zivame', 'Malleswaram, Bangalore', 13.001606, 77.571213, 'Clothing', 'free', 3.2, 73, ''),
    ('Good Colors', 'Malleswaram, Bangalore', 13.001614, 77.571191, 'Clothing', 'silver', 4.1, 133, ''),
    ('Arvind Store', 'Malleswaram, Bangalore', 13.002326, 77.571429, 'Clothing', 'bronze', 3.8, 28, ''),
    ('Manyavar Mohey', 'Malleswaram, Bangalore', 13.004772, 77.569382, 'Food & Grocery', 'free', 3.0, 13, ''),
    ('Preethi Textiles', 'Malleswaram, Bangalore', 12.999567, 77.571316, 'Clothing', 'free', 2.9, 48, ''),
    ('Arunodaya Apparel', 'Malleswaram, Bangalore', 12.998926, 77.571277, 'Clothing', 'free', 2.8, 11, ''),
    ('GKB Opticals', 'Malleswaram, Bangalore', 12.9982, 77.571514, 'Health & Wellness', 'bronze', 4.0, 143, ''),
    ('Udupi Gardens', 'Malleswaram, Bangalore', 12.997736, 77.571514, 'Food & Grocery', 'silver', 3.8, 384, ''),
    ('Yuvarani Design Studio', 'Malleswaram, Bangalore', 12.999092, 77.569283, 'Clothing', 'free', 3.6, 20, ''),
    ('Computech', 'Malleswaram, Bangalore', 12.998455, 77.569261, 'Electronics', 'free', 3.0, 24, ''),
    ('Varmilan', 'Malleswaram, Bangalore', 13.00004, 77.571432, 'Clothing', 'bronze', 3.6, 158, ''),
    ('Kamakshi Silk', 'Malleswaram, Bangalore', 13.000604, 77.569453, 'Food & Grocery', 'free', 3.0, 30, ''),
    ('Ratnadeep', 'Malleswaram, Bangalore', 13.000216, 77.569023, 'Food & Grocery', 'free', 3.1, 70, ''),
    ('NE Pharmacy', 'Malleswaram, Bangalore', 12.99326, 77.571151, 'Health & Wellness', 'gold', 3.9, 838, ''),
    ('Kamath Thindi mane', 'Malleswaram, Bangalore', 12.998304, 77.567114, 'Food & Grocery', 'free', 2.8, 80, ''),
    ('Acharyas Amruth Tea And Coffee', 'Malleswaram, Bangalore', 13.00623, 77.574335, 'Food & Grocery', 'silver', 3.6, 63, ''),
    ('Malleshwaram Dose Corner', 'Malleswaram, Bangalore', 13.007103, 77.570855, 'Food & Grocery', 'free', 3.3, 10, ''),
    ('Kerbside Bistro', '601, 3rd Main Road, Malleswaram, Bangalore', 13.007781, 77.577939, 'Food & Grocery', 'bronze', 3.1, 121, ''),
    ('GoHappy', 'Malleswaram, Bangalore', 13.00496, 77.565424, 'Food & Grocery', 'bronze', 3.9, 87, ''),
    ('Hermitage', 'Malleswaram, Bangalore', 13.008566, 77.563258, 'Food & Grocery', 'free', 3.2, 23, ''),
    ('Chikoos', 'Malleswaram, Bangalore', 13.009638, 77.559449, 'Food & Grocery', 'bronze', 3.4, 67, ''),
    ('Clean Slate Cafe', 'Malleswaram, Bangalore', 13.0044, 77.570632, 'Food & Grocery', 'bronze', 3.3, 139, ''),
    ('Silaa - The Garden Cafe', 'Malleswaram, Bangalore', 13.004393, 77.570992, 'Food & Grocery', 'free', 3.5, 72, ''),
    ('Namdhari', 'Malleswaram, Bangalore', 13.00467, 77.578455, 'Food & Grocery', 'free', 3.5, 12, ''),
    ('Eat Raja', 'Malleswaram, Bangalore', 13.004738, 77.571323, 'Food & Grocery', 'free', 3.0, 69, ''),
    ('Bengaluru Cafe', 'Malleswaram, Bangalore', 13.006728, 77.571074, 'Food & Grocery', 'bronze', 3.8, 78, ''),
    ('Benji Coffee', 'Malleswaram, Bangalore', 13.000954, 77.5697, 'Food & Grocery', 'silver', 4.2, 469, ''),
    ('Shanti Beauty Parlour', 'Malleswaram, Bangalore', 13.006087, 77.565098, 'Beauty & Personal Care', 'bronze', 4.0, 195, ''),
    ('Satyashila Electronics', 'Malleswaram, Bangalore', 13.006223, 77.564977, 'Electronics', 'gold', 4.2, 1905, ''),
    ('Sri Rama Veg', 'Malleswaram, Bangalore', 13.008837, 77.56342, 'Food & Grocery', 'free', 3.0, 50, ''),
    ('The Lassi Corner', 'Malleswaram, Bangalore', 13.00741, 77.578418, 'Food & Grocery', 'gold', 4.4, 1128, ''),
    ('The Indies Cafe', 'Malleswaram, Bangalore', 13.004438, 77.578693, 'Food & Grocery', 'silver', 3.9, 342, ''),
    ('Desi Shop', 'Malleswaram, Bangalore', 12.996545, 77.567627, 'Clothing', 'silver', 4.0, 111, ''),
    ('Total Superstore', '1st Main Road, Rajajinagar, Bangalore', 12.982781, 77.559204, 'Food & Grocery', 'gold', 4.1, 442, ''),
    ('Shanti Sagar Hotel', 'Rajajinagar, Bangalore', 12.997029, 77.553972, 'Food & Grocery', 'platinum', 4.8, 1533, ''),
    ('Gangasagar Chats', 'Rajajinagar, Bangalore', 12.987234, 77.549974, 'Food & Grocery', 'free', 2.8, 55, ''),
    ('Swati Deluxe', '5, Modi Hospital Main Road, Rajajinagar, Bangalore', 12.997825, 77.549984, 'Food & Grocery', 'free', 3.5, 18, '+(91)8023595565'),
    ('Sri Rama Medicals and General Stores', 'Rajajinagar, Bangalore', 12.997123, 77.551298, 'Health & Wellness', 'gold', 4.0, 1965, ''),
    ('Gokul Vegetarian', '399/34, 19th Main (Goorur Ramaswamy Iyengar Road), Rajajinagar, Bangalore', 12.998275, 77.550863, 'Food & Grocery', 'silver', 4.2, 94, '+918023423955'),
    ('My Looks', 'Rajajinagar, Bangalore', 12.998026, 77.549946, 'Clothing', 'bronze', 3.4, 103, ''),
    ('Sree Nandini Palace Restaurant', 'Rajajinagar, Bangalore', 12.987281, 77.549417, 'Food & Grocery', 'gold', 4.2, 1978, ''),
    ('Jalpaan', '5 & 6, Dr. Rajkumar Road, Rajajinagar, Bangalore', 12.99892, 77.552547, 'Food & Grocery', 'gold', 4.5, 1376, '+919591996822'),
    ('1947', 'Rajajinagar, Bangalore', 12.982985, 77.548638, 'Food & Grocery', 'silver', 4.2, 107, ''),
    ('Just Bake', 'Rajajinagar, Bangalore', 12.983657, 77.548764, 'Food & Grocery', 'bronze', 3.1, 170, ''),
    ('FBB', 'Dr Rajkumar Road, Rajajinagar, Bangalore', 12.982993, 77.559385, 'Clothing', 'silver', 4.4, 101, ''),
    ('New Prashanth Hotel', 'Rajajinagar, Bangalore', 12.998548, 77.551041, 'Food & Grocery', 'gold', 4.3, 306, ''),
    ('Crane Crush', 'Rajajinagar, Bangalore', 12.985581, 77.555685, 'Food & Grocery', 'silver', 4.3, 500, ''),
    ('O. G. Variar and Sons Bakery', 'Rajajinagar, Bangalore', 12.99146, 77.554623, 'Food & Grocery', 'bronze', 3.4, 118, ''),
    ('Nandini Milk Booth', 'Rajajinagar, Bangalore', 12.989528, 77.557408, 'Food & Grocery', 'silver', 4.0, 292, ''),
    ('Mahesh Opticals', 'Rajajinagar, Bangalore', 12.989866, 77.554497, 'Health & Wellness', 'free', 3.0, 18, ''),
    ('New Sagar Fast Food & Party Hall', 'Rajajinagar, Bangalore', 12.989934, 77.55268, 'Food & Grocery', 'silver', 4.1, 255, ''),
    ('Nandini General Stores', 'Rajajinagar, Bangalore', 12.989316, 77.557058, 'Food & Grocery', 'free', 3.6, 42, ''),
    ('Artway tailors - Ladies and Gents', 'Rajajinagar, Bangalore', 12.989653, 77.557055, 'Clothing', 'silver', 3.4, 293, ''),
    ('Ganesh Jeans Alteration Center', 'Rajajinagar, Bangalore', 12.98932, 77.556847, 'Clothing', 'gold', 4.6, 141, ''),
    ('Marvel Abrasive Tools', 'Rajajinagar, Bangalore', 12.989342, 77.556055, 'Hardware', 'bronze', 3.8, 99, ''),
    ('Standard Medicals', 'Rajajinagar, Bangalore', 12.98963, 77.556021, 'Health & Wellness', 'free', 3.2, 12, ''),
    ('Khadi Gramodyog Bhavan', 'Rajajinagar, Bangalore', 12.989494, 77.556007, 'Clothing', 'platinum', 4.2, 2961, ''),
    ('Classic Alteration Centre', 'Rajajinagar, Bangalore', 12.989387, 77.555041, 'Clothing', 'platinum', 4.4, 3128, ''),
    ("Toddler's Den", 'Rajajinagar, Bangalore', 12.989413, 77.556096, 'Clothing', 'free', 3.1, 11, ''),
    ('Lidkar Leather Emporium', 'Rajajinagar, Bangalore', 12.989575, 77.554503, 'Clothing', 'free', 2.9, 52, ''),
    ("Stella's Beauty Salon", 'Rajajinagar, Bangalore', 12.989431, 77.554456, 'Beauty & Personal Care', 'silver', 4.2, 494, ''),
    ('Hi Tech Junction - Jeans Alteration', 'Rajajinagar, Bangalore', 12.989273, 77.555136, 'Clothing', 'bronze', 3.1, 60, ''),
    ('Ganesh Enterprises', 'Rajajinagar, Bangalore', 12.989277, 77.555923, 'Stationery', 'free', 3.4, 51, ''),
    ('Sairam Medicals and General Stores', 'Rajajinagar, Bangalore', 12.98916, 77.555912, 'Health & Wellness', 'free', 3.6, 72, ''),
    ('Bombay Fashions - Ladies Tailors', 'Rajajinagar, Bangalore', 12.989107, 77.555901, 'Clothing', 'free', 2.9, 25, ''),
    ('Sri Raghavendra Condiments', 'Rajajinagar, Bangalore', 12.989339, 77.555994, 'Food & Grocery', 'gold', 3.9, 1034, ''),
    ('M N Tailors', 'Rajajinagar, Bangalore', 12.989146, 77.555975, 'Clothing', 'free', 3.1, 25, ''),
    ('Touch One Stop Repair Services - Laptop, Desktop, Tablet, Accessories', 'Rajajinagar, Bangalore', 12.989259, 77.555983, 'Electronics', 'free', 3.5, 64, ''),
    ('Sri Charan stores', 'Rajajinagar, Bangalore', 12.989064, 77.555971, 'Food & Grocery', 'bronze', 3.9, 194, ''),
    ('Soundarya Beauty Parlour', 'Rajajinagar, Bangalore', 12.989083, 77.555974, 'Beauty & Personal Care', 'free', 3.1, 76, ''),
    ('Sri Manjunath Stores', 'Rajajinagar, Bangalore', 12.989005, 77.555961, 'Food & Grocery', 'bronze', 4.0, 141, ''),
    ('Mobile and More', 'Rajajinagar, Bangalore', 12.989, 77.555963, 'Electronics', 'silver', 3.7, 133, ''),
    ('Gyan Exports', 'Rajajinagar, Bangalore', 12.98898, 77.55596, 'Clothing', 'gold', 4.0, 1801, ''),
    ('Sri Prasamshi Medical Stores', 'Rajajinagar, Bangalore', 12.990464, 77.556926, 'Health & Wellness', 'silver', 3.7, 247, ''),
    ('Parmar', '16, Modi Hospital Main Road, Rajajinagar, Bangalore', 12.997705, 77.549515, 'Stationery', 'free', 3.6, 27, '+(91)8023496604'),
    ('Simply Organics', 'Rajajinagar, Bangalore', 12.993919, 77.551327, 'Food & Grocery', 'gold', 4.3, 1487, ''),
    ('Universal Pharma', 'Rajajinagar, Bangalore', 12.998872, 77.549813, 'Health & Wellness', 'free', 3.4, 65, ''),
    ("Dr. Suresh's Dia Care", '723/B, 11th Main Rd, 3rd Block, Rajaji Nagar, Rajajinagar, Bangalore', 12.989838, 77.555057, 'Health & Wellness', 'free', 3.5, 1, '+918023382867'),
    ('Amrutha Medicals', '403, Dr. Rajkumar Road, 6th Block, 80 Feet Rd, 4th Block, Ramchandrapuram, Rajaji Nagar, Rajajinagar, Bangalore', 12.986147, 77.559854, 'Health & Wellness', 'bronze', 3.1, 64, '+918023117929'),
    ('Health Shopee by Manjushree Medicals', '2941/5, Mahakavi Kuvempu Road, 2nd Stage, Rajajinagar, Near Varalaxmi Nursing Home, D-Block, Gayatrinagar, Rajaji Nagar, Rajajinagar, Bangalore', 12.998881, 77.555288, 'Health & Wellness', 'silver', 3.4, 179, '+919886114333'),
    ('Nandhi Medical And General Stores', '2/835/64/1, 4th Block, Amulya Corner, 3rd Main, 3rd Main Road, 4th Block, Rajaji Nagar, Rajajinagar, Bangalore', 12.988246, 77.558427, 'Health & Wellness', 'silver', 4.3, 414, '+918023145334'),
    ('Navarang Kabab Corner', '1000, 4th Block, Dr Rajkumar Road,Rajajinagar, Rajajinagar, Bangalore', 12.990826, 77.557884, 'Food & Grocery', 'platinum', 4.6, 3971, '+918041487575'),
    ('Kolkata Biryani And Rolls', 'GEF Block,\xa0RajajinagarIndustrial Town, Rajajinaga, Rajajinagar, Bangalore', 12.984516, 77.547804, 'Food & Grocery', 'gold', 4.6, 1040, '+917676471371'),
    ('Kudla beach', '73, Kantha Complex, Dr. Rajkumar Road, 3rd Stage,\xa0Rajajinaga, Rajajinagar, Bangalore', 12.990393, 77.558444, 'Food & Grocery', 'free', 3.2, 66, '+918032409844'),
    ('Sagar Caterers and Hotel', 'Dr Rajkumar Road, Opposite KLE College, Near Navrang Theatre,Rajajinaga, Rajajinagar, Bangalore', 12.996735, 77.554317, 'Food & Grocery', 'gold', 4.1, 309, '+918023325917'),
    ('Shree Nandana Palace', '1, 5th Main, West of Chord Road, Shivanagar,\xa0Rajajinagar, Rajajinagar, Bangalore', 12.989155, 77.550001, 'Food & Grocery', 'free', 3.7, 47, '+918064444455'),
    ('Nandhi Medicals', '2, 46th cross, Rajajinagar 4th Block, Rajajinagar, Bangalore', 12.988327, 77.554971, 'Health & Wellness', 'silver', 4.0, 194, '+918023145334'),
    ('Sri Venkateshwara Medical & General Store', '955, 3rd Block, Rajajinagar, Rajajinagar, Bangalore', 12.989857, 77.554231, 'Health & Wellness', 'free', 2.8, 8, '+919449168854'),
    ('Prasad Medicals', '529, 10th Main, 2nd Block, Rajajinagar, Rajajinagar, Bangalore', 12.990624, 77.563316, 'Health & Wellness', 'bronze', 3.3, 103, '+918050782573'),
    ('Kanva Diagnostics', '2/10, Dr Rajkumar Rd, 4N Block, Rajajinagar, Bangalore', 12.986961, 77.560226, 'Health & Wellness', 'gold', 4.0, 1556, '+918023133840'),
    ('Sridhar Medicals', '13, ,2nd Main Rd, Malleshwaram, Rajajinagar, Bangalore', 12.987704, 77.553094, 'Health & Wellness', 'platinum', 4.7, 2043, '+918023363765'),
    ('Hotel Nalapaka', '28, 12th Main, Near Navarang Theatre, Rajajinagar, Bengaluru, Rajajinagar, Bangalore', 12.998514, 77.552323, 'Food & Grocery', 'free', 3.5, 7, '+918023523108'),
    ('Shree Nandini Palace', '5, 1st Stage, West Shivanahalli, Chord Road, Shivanagar, Rajaji Nagar, Rajajinagar, Bangalore', 12.9898, 77.550034, 'Food & Grocery', 'gold', 4.5, 1328, '+918023350055'),
    ('Ambika Veg Restaurant', '638, 2nd Main, Near-Navarang Theater, Dr Rajkumar Rd, D-Block, 2nd Stage, Rajaji Nagar,, Rajajinagar, Bangalore', 12.98783, 77.555849, 'Food & Grocery', 'bronze', 3.3, 55, '+918041214708'),
    ('Patel Fancy Store', 'Rajajinagar, Bangalore', 12.997722, 77.548818, 'Stationery', 'silver', 4.1, 201, ''),
    ('Sai Tiffin Centre', '7th Main Road, Rajajinagar, Bangalore', 12.989504, 77.54581, 'Food & Grocery', 'free', 3.1, 49, ''),
    ('Karnataka Fish Stall', 'Rajajinagar, Bangalore', 12.99205, 77.546102, 'Food & Grocery', 'silver', 4.0, 342, ''),
    ('Boots Bazaar', 'Rajajinagar, Bangalore', 12.997569, 77.55058, 'Clothing', 'gold', 4.0, 1560, ''),
    ('reenix sports india pvt ltd', '101, 7th cross, 11th Main Rd, Bhadrappa Layout, Prakash Nagar, Rajaji Nagar, Rajajinagar, Bangalore', 12.992647, 77.55708, 'Sports', 'free', 3.2, 60, '+919886811390'),
    ('Snippetts Unisex Salon and Spa', 'No.808/14,, 14th Main,50th Cross, Rajajinagar 3rd Block,, Rajajinagar, Bangalore', 12.988183, 77.553445, 'Beauty & Personal Care', 'gold', 4.4, 1138, '+9191)9900777522'),
    ('Scent Spa', 'Marriott Hotel, Sujatha Circle, Rajajinagar,, Rajajinagar, Bangalore', 12.983366, 77.559101, 'Beauty & Personal Care', 'bronze', 3.2, 188, '+9191)9742212700'),
    ('Priya Tiffin Room', 'Rajajinagar, Bangalore', 12.988959, 77.555959, 'Food & Grocery', 'gold', 3.9, 299, ''),
    ('Shree Venkateshwara Traders', '712, Modi Hospital Main Road, Rajajinagar, Bangalore', 12.997705, 77.548286, 'Hardware', 'bronze', 3.7, 163, '+919036092531'),
    ('Rolls Kitchen', '1, Modi Hospital Main Road, Rajajinagar, Bangalore', 12.997497, 77.55012, 'Food & Grocery', 'silver', 3.6, 398, '+917022164745'),
    ('Empire Plaza', '32, Mahakavi Kuvempu Road, Rajajinagar, Bangalore', 12.99786, 77.551967, 'Food & Grocery', 'gold', 4.4, 555, ''),
    ('Nisarga Family Resturant', '2961, Mahakavi Kuvempu Road, Rajajinagar, Bangalore', 12.998435, 77.553066, 'Food & Grocery', 'bronze', 3.7, 98, '+918023130006'),
    ('Aruna Silks', 'Rajajinagar, Bangalore', 12.99771, 77.548391, 'Clothing', 'bronze', 3.9, 115, ''),
    ('Ram Medicals', 'Rajajinagar, Bangalore', 12.99727, 77.548401, 'Health & Wellness', 'free', 3.0, 15, ''),
    ('Geetha Medicals', 'Rajajinagar, Bangalore', 12.99745, 77.548714, 'Health & Wellness', 'bronze', 3.9, 169, ''),
    ('New Feet Fashion', 'Rajajinagar, Bangalore', 12.997709, 77.549559, 'Clothing', 'silver', 4.0, 291, ''),
    ('Manjunath Gobi Corner', 'Rajajinagar, Bangalore', 12.998361, 77.549109, 'Food & Grocery', 'gold', 4.2, 1876, ''),
    ("Annappa's Naati Mane Hassan Style", 'Rajajinagar, Bangalore', 12.998125, 77.553279, 'Food & Grocery', 'silver', 3.9, 261, ''),
    ('Sri Eshwar Fruit Juice Center', 'Rajajinagar, Bangalore', 12.998129, 77.55793, 'Food & Grocery', 'free', 3.6, 17, ''),
    ('Prakash Electronics', 'Rajajinagar, Bangalore', 12.997481, 77.549828, 'Electronics', 'platinum', 4.3, 4248, ''),
    ('Bharathi Garments', 'Rajajinagar, Bangalore', 12.997711, 77.549795, 'Clothing', 'free', 3.6, 23, ''),
    ('Nandini Grand', 'Rajajinagar, Bangalore', 12.997704, 77.549443, 'Food & Grocery', 'silver', 3.9, 373, ''),
    ('Khalandar Mutton Stall', 'Rajajinagar, Bangalore', 12.997479, 77.549277, 'Food & Grocery', 'platinum', 4.3, 975, ''),
    ('Kasturi Pharma and General Stores', 'Rajajinagar, Bangalore', 12.989936, 77.552843, 'Health & Wellness', 'free', 3.0, 21, ''),
    ('Prakash Medicals', 'Rajajinagar, Bangalore', 12.987399, 77.55608, 'Health & Wellness', 'silver', 3.6, 452, ''),
    ('Mahaveer Hardware', 'Rajajinagar, Bangalore', 12.997352, 77.550135, 'Hardware', 'silver', 3.5, 364, ''),
    ('7 Colors', 'Rajajinagar, Bangalore', 12.997728, 77.551447, 'Clothing', 'bronze', 3.4, 129, ''),
    ('Vivo', 'Rajajinagar, Bangalore', 12.997795, 77.551693, 'Electronics', 'platinum', 4.4, 809, ''),
    ('HP World', 'Rajajinagar, Bangalore', 12.998233, 77.557495, 'Electronics', 'bronze', 3.7, 23, ''),
    ('Santhosh Bar & Restaurant', 'Rajajinagar, Bangalore', 12.997507, 77.550027, 'Food & Grocery', 'gold', 4.5, 1187, ''),
    ('Al Ambur Dum Biryani', 'Rajajinagar, Bangalore', 12.998362, 77.552844, 'Food & Grocery', 'free', 3.4, 80, ''),
    ("Gopal's Pure Veg and Sweets", 'Rajajinagar, Bangalore', 12.998344, 77.55309, 'Food & Grocery', 'silver', 3.6, 499, ''),
    ('Sri Ganesha Stationeries', 'Rajajinagar, Bangalore', 12.998567, 77.555389, 'Stationery', 'bronze', 3.1, 118, ''),
    ('Shiva Vision Care', 'Rajajinagar, Bangalore', 12.99877, 77.556168, 'Health & Wellness', 'bronze', 3.3, 36, ''),
    ('Sree Ganesh Fruit Juice Center', 'Rajajinagar, Bangalore', 12.997449, 77.548856, 'Food & Grocery', 'gold', 4.0, 695, ''),
    ('Sri Ranga Silks', 'Rajajinagar, Bangalore', 12.99782, 77.548821, 'Clothing', 'bronze', 3.5, 154, ''),
    ('KAP Stationery', 'Rajajinagar, Bangalore', 12.986349, 77.55581, 'Stationery', 'bronze', 3.9, 54, ''),
    ('Sri Venkateshwara Utsav', 'Rajajinagar, Bangalore', 12.986157, 77.555793, 'Food & Grocery', 'bronze', 3.9, 111, ''),
    ('Sindhu Swadeshi Distribution Centre', 'Rajajinagar, Bangalore', 12.986, 77.555791, 'Stationery', 'silver', 3.6, 465, ''),
    ('Krishna Traders', 'Rajajinagar, Bangalore', 12.985899, 77.555767, 'Hardware', 'bronze', 3.2, 53, ''),
    ('Manasa Enterprises', 'Rajajinagar, Bangalore', 12.985837, 77.555767, 'Clothing', 'free', 2.7, 64, ''),
    ('V. G. Provision Store', 'Rajajinagar, Bangalore', 12.987765, 77.555848, 'Food & Grocery', 'free', 3.1, 4, ''),
    ('Naveenraj Fruits Stall', 'Rajajinagar, Bangalore', 12.987345, 77.555846, 'Food & Grocery', 'bronze', 3.6, 156, ''),
    ('Himalaya Provision Store', 'Rajajinagar, Bangalore', 12.987218, 77.555841, 'Food & Grocery', 'silver', 4.0, 74, ''),
    ('LJB Bakery', 'Rajajinagar, Bangalore', 12.987193, 77.555838, 'Food & Grocery', 'silver', 4.1, 60, ''),
    ('Samsung - Seema Enterprises', 'Rajajinagar, Bangalore', 12.987045, 77.555838, 'Electronics', 'gold', 3.7, 337, ''),
    ('Jagger Tailors', 'Rajajinagar, Bangalore', 12.986736, 77.55584, 'Clothing', 'bronze', 3.4, 160, ''),
    ('Madhok Ladies Tailors', 'Rajajinagar, Bangalore', 12.986709, 77.555838, 'Clothing', 'free', 3.4, 26, ''),
    ('Bijapur Stores', 'Rajajinagar, Bangalore', 12.986669, 77.55583, 'Food & Grocery', 'silver', 4.0, 204, ''),
    ('Sri Sai Fashion', 'Rajajinagar, Bangalore', 12.989865, 77.554564, 'Clothing', 'bronze', 3.1, 153, ''),
    ('Aadinath Medicals', 'Rajajinagar, Bangalore', 12.989848, 77.555154, 'Health & Wellness', 'free', 3.3, 17, ''),
    ('Graduate Textiles & Tailors', 'Rajajinagar, Bangalore', 12.989848, 77.555315, 'Clothing', 'bronze', 3.9, 150, ''),
    ('Bombay Modern Tailors', 'Rajajinagar, Bangalore', 12.989857, 77.555601, 'Clothing', 'silver', 3.5, 108, ''),
    ('N Rich Fashions', 'Rajajinagar, Bangalore', 12.989817, 77.555829, 'Clothing', 'silver', 3.9, 361, ''),
    ('Party N Toys Zone', 'Rajajinagar, Bangalore', 12.989577, 77.555913, 'Toys & Baby', 'free', 3.0, 66, ''),
    ('The Mobile Track', 'Rajajinagar, Bangalore', 12.989527, 77.555913, 'Electronics', 'platinum', 4.4, 3856, ''),
    ('One Touch', 'Rajajinagar, Bangalore', 12.988544, 77.555885, 'Electronics', 'free', 2.7, 63, ''),
    ('GP Footwear', 'Rajajinagar, Bangalore', 12.988463, 77.555701, 'Clothing', 'gold', 4.4, 1084, ''),
    ('Sudha Book House', 'Rajajinagar, Bangalore', 12.988216, 77.55586, 'Books', 'bronze', 3.0, 191, ''),
    ('Sri Sai Cake Palace', 'Rajajinagar, Bangalore', 12.988128, 77.555846, 'Food & Grocery', 'free', 2.8, 73, ''),
    ('S. K. Fashions', 'Rajajinagar, Bangalore', 12.988095, 77.555846, 'Clothing', 'silver', 3.5, 96, ''),
    ('Sree Prabhu Medical Centre', 'Rajajinagar, Bangalore', 12.988022, 77.555846, 'Health & Wellness', 'silver', 3.5, 420, ''),
    ('Rostar Mottan Stall', 'Rajajinagar, Bangalore', 12.994309, 77.550819, 'Food & Grocery', 'free', 3.0, 29, ''),
    ('Hotel Park Inn', 'Rajajinagar, Bangalore', 12.99287, 77.550834, 'Food & Grocery', 'bronze', 4.0, 87, ''),
    ('Trident Hyundai', 'Rajajinagar, Bangalore', 12.992127, 77.550794, 'Automotive', 'bronze', 3.1, 64, ''),
    ('Sun Car Decore', 'Rajajinagar, Bangalore', 12.991113, 77.550751, 'Automotive', 'gold', 4.6, 837, ''),
    ('V. R. Opticians', 'Rajajinagar, Bangalore', 12.989904, 77.553573, 'Health & Wellness', 'gold', 4.0, 1612, ''),
    ('Annapoorneshwari Cars', 'Rajajinagar, Bangalore', 12.989911, 77.553509, 'Automotive', 'free', 3.6, 5, ''),
    ('Sri Durga Grand Veg', 'Rajajinagar, Bangalore', 12.989911, 77.553221, 'Food & Grocery', 'free', 2.8, 21, ''),
    ('Seetha Medicals & General Store', 'Rajajinagar, Bangalore', 12.98991, 77.553114, 'Health & Wellness', 'bronze', 3.9, 179, ''),
    ('Hotel Kaveri', 'Rajajinagar, Bangalore', 12.989935, 77.552798, 'Food & Grocery', 'free', 3.4, 47, ''),
    ('J.P. Lights and Appliances', 'Rajajinagar, Bangalore', 12.987454, 77.549575, 'Electronics', 'gold', 3.9, 920, ''),
    ("Nala's Kitchen", 'Rajajinagar, Bangalore', 12.987359, 77.549556, 'Food & Grocery', 'bronze', 3.5, 118, ''),
    ('Udupi Upachar', 'Rajajinagar, Bangalore', 12.990465, 77.550535, 'Food & Grocery', 'silver', 3.9, 265, ''),
    ('True Cars', 'Rajajinagar, Bangalore', 12.995442, 77.550473, 'Automotive', 'silver', 4.2, 107, ''),
    ("Khadim's", 'Rajajinagar, Bangalore', 12.997698, 77.549384, 'Clothing', 'free', 3.4, 20, ''),
    ('The Nobel House', 'Rajajinagar, Bangalore', 12.997469, 77.549141, 'Clothing', 'free', 3.6, 61, ''),
    ('New Shanthi Sagar', '12th Main Road, Rajajinagar, Bangalore', 12.986098, 77.55435, 'Food & Grocery', 'gold', 4.4, 471, ''),
    ('Prashanthi Ayurvedic Centre', 'Rajajinagar, Bangalore', 12.996383, 77.550896, 'Health & Wellness', 'silver', 4.2, 363, ''),
    ('Al-Taj Restaurant', 'Rajajinagar, Bangalore', 12.989929, 77.552992, 'Food & Grocery', 'free', 3.4, 16, ''),
    ('Roop Niketan', 'Rajajinagar, Bangalore', 12.987967, 77.556072, 'Clothing', 'gold', 4.2, 1099, ''),
    ('by 2 coffy', 'Rajajinagar, Bangalore', 12.986919, 77.556213, 'Food & Grocery', 'free', 3.1, 5, ''),
    ('Homoeo Mart', 'Rajajinagar, Bangalore', 12.999318, 77.550205, 'Health & Wellness', 'free', 3.4, 1, ''),
    ('Shree Sai Silk Sarees', 'Rajajinagar, Bangalore', 12.990097, 77.554549, 'Clothing', 'silver', 4.1, 442, ''),
    ('Wellness Forever', 'Rajajinagar, Bangalore', 12.988293, 77.556106, 'Health & Wellness', 'silver', 4.1, 164, ''),
    ('Batte Angadi', 'Rajajinagar, Bangalore', 12.997799, 77.551773, 'Clothing', 'platinum', 4.7, 3271, ''),
    ('Hyundai', 'Rajajinagar, Bangalore', 12.99494, 77.554936, 'Automotive', 'bronze', 3.7, 178, ''),
    ('Saanvi Delicacy', 'Rajajinagar, Bangalore', 12.984324, 77.548609, 'Food & Grocery', 'free', 3.4, 31, ''),
    ('Cafe 916 & Bake', 'Rajajinagar, Bangalore', 12.997647, 77.552365, 'Food & Grocery', 'bronze', 3.7, 184, ''),
    ('Karavali Fine Dine', 'Rajajinagar, Bangalore', 12.992188, 77.554649, 'Food & Grocery', 'bronze', 3.8, 162, ''),
    ('Spicy Secret', 'Rajajinagar, Bangalore', 12.988307, 77.555862, 'Food & Grocery', 'gold', 4.3, 123, ''),
    ('Harshitha Medical & General Store', 'Rajajinagar, Bangalore', 12.990616, 77.550583, 'Health & Wellness', 'silver', 3.6, 484, ''),
    ('Jaguar Lighting', 'Rajajinagar, Bangalore', 12.985867, 77.54912, 'Electronics', 'bronze', 3.5, 62, ''),
    ('Coffee Day Essentials', '19th Main (Goorur Ramaswamy Iyengar Road), Rajajinagar, Bangalore', 12.998658, 77.550801, 'Food & Grocery', 'free', 3.2, 30, ''),
    ('Probe Tailors', 'Rajajinagar, Bangalore', 12.999728, 77.550792, 'Clothing', 'bronze', 3.3, 149, ''),
    ('Samsung SmartCafe', 'Rajajinagar, Bangalore', 12.999403, 77.550874, 'Electronics', 'gold', 3.9, 1417, ''),
    ('Dreams Mens Salon', 'Rajajinagar, Bangalore', 12.999266, 77.550918, 'Beauty & Personal Care', 'bronze', 3.8, 40, ''),
    ('Sree Gajendra Juice and Chaats Center', 'Rajajinagar, Bangalore', 12.999134, 77.550935, 'Food & Grocery', 'bronze', 3.8, 134, ''),
    ('Galaxy Opticals', 'Rajajinagar, Bangalore', 12.998936, 77.550731, 'Health & Wellness', 'free', 3.2, 40, ''),
    ('Cake and Pastries', 'Rajajinagar, Bangalore', 12.998781, 77.550764, 'Food & Grocery', 'free', 3.2, 2, '+918041571512'),
    ('FD Outlet', 'Rajajinagar, Bangalore', 12.98381, 77.558199, 'Clothing', 'free', 3.1, 71, ''),
    ('Malnad Aroma', 'Rajajinagar, Bangalore', 12.987201, 77.556077, 'Food & Grocery', 'bronze', 3.8, 174, ''),
    ('Sri Hema Book World', 'Rajajinagar, Bangalore', 12.987809, 77.556088, 'Books', 'platinum', 4.6, 4664, ''),
    ('Stay Happi Pharmacy', 'Rajajinagar, Bangalore', 12.99004, 77.555738, 'Health & Wellness', 'silver', 4.1, 283, ''),
    ('Ashirwad Market', 'Rajajinagar, Bangalore', 12.986261, 77.550853, 'Food & Grocery', 'platinum', 4.4, 944, ''),
    ('City Lights and Appliances', 'Rajajinagar, Bangalore', 12.988508, 77.549939, 'Electronics', 'gold', 4.5, 779, ''),
    ('Car World', 'Rajajinagar, Bangalore', 12.991549, 77.550788, 'Automotive', 'silver', 4.0, 292, ''),
    ('Rajbhog', 'Rajajinagar, Bangalore', 12.998971, 77.552179, 'Food & Grocery', 'silver', 3.8, 281, ''),
    ('Udupi Veg', 'Rajajinagar, Bangalore', 13.000225, 77.551961, 'Food & Grocery', 'free', 3.5, 28, ''),
    ('Akshaya Tailors', 'Rajajinagar, Bangalore', 12.999916, 77.55171, 'Clothing', 'free', 3.1, 37, ''),
    ('Gobi Corner', 'Rajajinagar, Bangalore', 12.984008, 77.558159, 'Food & Grocery', 'bronze', 3.4, 47, ''),
    ('Uttara Computers', 'Rajajinagar, Bangalore', 12.984485, 77.557338, 'Electronics', 'free', 2.9, 17, ''),
    ('Matrix Salon', 'Rajajinagar, Bangalore', 12.989765, 77.555319, 'Beauty & Personal Care', 'gold', 4.4, 938, ''),
    ('Prakriti Deluxe', 'Rajajinagar, Bangalore', 12.988311, 77.556248, 'Food & Grocery', 'free', 3.3, 24, ''),
    ('Hasanamba Benne Dosa', 'Rajajinagar, Bangalore', 12.997468, 77.549184, 'Food & Grocery', 'free', 3.6, 27, ''),
    ('Pradhanmantri Bharatiya Janaushadhi Kendra', '624, Rajajinagar, Bangalore', 12.995386, 77.554627, 'Health & Wellness', 'bronze', 3.8, 106, ''),
    ('Aum Zone', 'Rajajinagar, Bangalore', 12.998306, 77.549882, 'Food & Grocery', 'free', 3.1, 75, ''),
    ('Burger Elite', 'Rajajinagar, Bangalore', 12.998952, 77.549746, 'Food & Grocery', 'free', 3.6, 58, ''),
    ('Naseeb Fruits Stall', 'Rajajinagar, Bangalore', 12.985132, 77.556643, 'Food & Grocery', 'silver', 4.4, 118, ''),
    ('Jain Bakes', 'Rajajinagar, Bangalore', 12.987621, 77.55547, 'Food & Grocery', 'platinum', 4.0, 897, ''),
    ('Bharath Book Center', 'Rajajinagar, Bangalore', 12.988576, 77.55263, 'Books', 'silver', 3.8, 349, ''),
    ('The Signman', 'Rajajinagar, Bangalore', 12.989501, 77.552649, 'Hardware', 'bronze', 3.5, 65, ''),
    ('Kempegowda Vegetable Market', 'Rajajinagar, Bangalore', 12.986772, 77.556286, 'Food & Grocery', 'silver', 3.6, 467, ''),
    ('Tandoor Chai Point', 'Rajajinagar, Bangalore', 12.989517, 77.556168, 'Food & Grocery', 'free', 3.1, 76, ''),
    ('Le-Studio', 'Rajajinagar, Bangalore', 12.997939, 77.549944, 'Beauty & Personal Care', 'silver', 3.7, 246, ''),
    ('Royal Cakes and Cookies', 'Rajajinagar, Bangalore', 12.984417, 77.55745, 'Food & Grocery', 'bronze', 3.6, 197, ''),
    ('Malnad Cafe', 'Rajajinagar, Bangalore', 12.989362, 77.55612, 'Food & Grocery', 'silver', 3.7, 225, ''),
    ('Kadambari', 'Rajajinagar, Bangalore', 12.994169, 77.551364, 'Clothing', 'platinum', 4.5, 1592, ''),
    ('Kaappi Ready', 'Rajajinagar, Bangalore', 12.997737, 77.549374, 'Food & Grocery', 'free', 3.3, 65, ''),
    ('Vivek Tailors', 'Rajajinagar, Bangalore', 12.987184, 77.550121, 'Clothing', 'free', 3.4, 40, ''),
    ('Raj Lakshmi Momos Corner', 'Rajajinagar, Bangalore', 12.998289, 77.549129, 'Food & Grocery', 'silver', 4.3, 373, ''),
    ('Vaibhav Biryani', 'Rajajinagar, Bangalore', 12.998607, 77.55034, 'Food & Grocery', 'free', 2.9, 37, ''),
    ('Swagat', 'Rajajinagar, Bangalore', 12.989433, 77.554045, 'Food & Grocery', 'silver', 4.3, 117, ''),
    ('Kim Lee Beauty Parlour', 'Rajajinagar, Bangalore', 12.984635, 77.553674, 'Beauty & Personal Care', 'platinum', 4.7, 1956, ''),
    ('Kim Lee Restaurant', 'Rajajinagar, Bangalore', 12.9847, 77.55361, 'Food & Grocery', 'bronze', 3.7, 102, ''),
    ('Sicilia Point', 'Rajajinagar, Bangalore', 12.985765, 77.55176, 'Food & Grocery', 'silver', 4.2, 337, ''),
    ('Ganapa Thatte Idli', 'Rajajinagar, Bangalore', 12.997727, 77.549666, 'Food & Grocery', 'bronze', 3.6, 190, ''),
    ('Specscart', 'Rajajinagar, Bangalore', 12.998767, 77.556238, 'Health & Wellness', 'silver', 4.1, 480, ''),
    ('Navarang Opticals', 'Rajajinagar, Bangalore', 12.998458, 77.553499, 'Health & Wellness', 'bronze', 3.9, 128, ''),
    ('Mataji Mobile and Accessories', 'Rajajinagar, Bangalore', 12.998284, 77.552708, 'Electronics', 'gold', 4.2, 151, ''),
    ('New Meenakshi Coffee Bar', 'Rajajinagar, Bangalore', 12.997886, 77.552304, 'Food & Grocery', 'bronze', 3.1, 34, ''),
    ('New Shetty Lunch Home', 'Rajajinagar, Bangalore', 12.998138, 77.551979, 'Food & Grocery', 'free', 3.5, 20, ''),
    ('Suman Pharma and Opticals', 'Rajajinagar, Bangalore', 12.997589, 77.55086, 'Health & Wellness', 'platinum', 4.7, 1699, ''),
    ('Vanillaa Bakery and Cafe', 'Rajajinagar, Bangalore', 12.998931, 77.550312, 'Food & Grocery', 'gold', 4.4, 704, ''),
    ('Taj Biryani Paradise', 'Rajajinagar, Bangalore', 12.994838, 77.555293, 'Food & Grocery', 'free', 3.2, 45, ''),
    ('Chinnu Photo Books', 'Rajajinagar, Bangalore', 12.998463, 77.552717, 'Books', 'gold', 3.8, 1877, ''),
    ('Foodworld', 'Jayanagar, Bangalore', 12.932818, 77.583043, 'Food & Grocery', 'silver', 4.1, 337, ''),
    ('Ganesh Darshan', 'Jayanagar, Bangalore', 12.93092, 77.584032, 'Food & Grocery', 'gold', 4.4, 1249, ''),
    ('Upahara Darshini', 'Jayanagar, Bangalore', 12.933058, 77.584422, 'Food & Grocery', 'bronze', 3.5, 198, ''),
    ('Nagarjuna Chimney', 'Jayanagar, Bangalore', 12.933043, 77.584304, 'Food & Grocery', 'free', 3.4, 48, ''),
    ('La Casa', 'Jayanagar, Bangalore', 12.926179, 77.584824, 'Food & Grocery', 'bronze', 4.0, 195, ''),
    ('Web Nirnaya Laptop Service Station', 'Jayanagar, Bangalore', 12.932881, 77.583529, 'Electronics', 'bronze', 3.6, 23, ''),
    ('U D Residency', '19/2, South End Main Road, Jayanagar, Bangalore', 12.936326, 77.576763, 'Food & Grocery', 'silver', 3.6, 494, ''),
    ('Ramdev Medicals', 'Jayanagar, Bangalore', 12.93545, 77.592594, 'Health & Wellness', 'silver', 4.3, 474, ''),
    ('Addidas', '11th main road, Jayanagar, Bangalore', 12.932438, 77.586254, 'Clothing', 'free', 3.3, 47, ''),
    ('Shanthi Sagar', '36 Cross, Jayanagar, Bangalore', 12.923151, 77.589394, 'Food & Grocery', 'silver', 3.9, 358, ''),
    ('Brews & Bites', 'Jayanagar, Bangalore', 12.928433, 77.59006, 'Food & Grocery', 'free', 3.4, 24, ''),
    ('Nandini Milk Diary', '2nd Cross LIC Colony, Jayanagar, Bangalore', 12.930973, 77.590134, 'Food & Grocery', 'free', 2.9, 9, ''),
    ('United Colors of Benetton', '11th main, Jayanagar, Bangalore', 12.932278, 77.586251, 'Clothing', 'free', 2.9, 65, ''),
    ('she', '11th main, Jayanagar, Bangalore', 12.930589, 77.586206, 'Clothing', 'free', 2.7, 35, ''),
    ('Lassi Park', 'elephant cave road, Jayanagar, Bangalore', 12.933096, 77.582315, 'Food & Grocery', 'silver', 3.5, 349, ''),
    ('New Shanti Sagar', 'Jayanagar, Bangalore', 12.931487, 77.576137, 'Food & Grocery', 'gold', 3.7, 1055, ''),
    ('SLV Swagath', 'Tilak Nagar Main Road, Jayanagar, Bangalore', 12.928422, 77.589782, 'Food & Grocery', 'silver', 3.6, 128, ''),
    ('Kalamandir', 'Jayanagar, Bangalore', 12.927117, 77.586161, 'Clothing', 'gold', 4.3, 1684, ''),
    ('Shanti Sagara', 'Jayanagar, Bangalore', 12.928781, 77.585667, 'Food & Grocery', 'gold', 4.3, 1455, ''),
    ('Pearl Optics', 'Jayanagar, Bangalore', 12.927545, 77.580764, 'Health & Wellness', 'bronze', 3.8, 160, ''),
    ('Polar Bear', 'Jayanagar, Bangalore', 12.927636, 77.577924, 'Food & Grocery', 'bronze', 3.8, 101, ''),
    ('Gelato Italiano', 'Jayanagar, Bangalore', 12.927847, 77.578207, 'Food & Grocery', 'bronze', 4.0, 153, ''),
    ('Sukh Sagar', 'Jayanagar, Bangalore', 12.928528, 77.582645, 'Food & Grocery', 'free', 3.2, 53, ''),
    ('Jayanagar 4th Block Fish market', 'Jayanagar, Bangalore', 12.929531, 77.584573, 'Food & Grocery', 'bronze', 3.4, 109, ''),
    ('Sri Devi International', 'Jayanagar, Bangalore', 12.936871, 77.584743, 'Electronics', 'free', 2.9, 42, ''),
    ('Subz', 'Jayanagar, Bangalore', 12.932996, 77.585095, 'Food & Grocery', 'bronze', 3.3, 178, ''),
    ('Awesome', 'Jayanagar, Bangalore', 12.931337, 77.58623, 'Clothing', 'bronze', 4.0, 186, ''),
    ('Boots Bazaaar', 'Jayanagar, Bangalore', 12.929453, 77.586147, 'Clothing', 'free', 3.4, 57, ''),
    ('Vasudev Adigas', 'Jayanagar, Bangalore', 12.930637, 77.58629, 'Food & Grocery', 'free', 3.3, 79, ''),
    ('Iyengar bakers', 'Jayanagar, Bangalore', 12.926201, 77.586456, 'Food & Grocery', 'free', 2.7, 41, ''),
    ('Kalanjali', 'Jayanagar, Bangalore', 12.925831, 77.585848, 'Clothing', 'bronze', 3.8, 49, ''),
    ('Limelight', 'Jayanagar, Bangalore', 12.923189, 77.585319, 'Beauty & Personal Care', 'bronze', 3.5, 92, ''),
    ('Kalaniketan', 'Jayanagar, Bangalore', 12.928945, 77.585939, 'Clothing', 'gold', 3.7, 1158, ''),
    ('Roop sangam', 'Jayanagar, Bangalore', 12.929033, 77.585912, 'Clothing', 'gold', 4.1, 1744, ''),
    ('Titan Eye +', 'Jayanagar, Bangalore', 12.929297, 77.586149, 'Health & Wellness', 'gold', 4.2, 1096, ''),
    ('Book World', 'Jayanagar, Bangalore', 12.928386, 77.586087, 'Books', 'free', 3.6, 24, ''),
    ('Lakshmi Medicals', 'Jayanagar, Bangalore', 12.923396, 77.588428, 'Health & Wellness', 'bronze', 3.1, 44, ''),
    ('Sun Times', 'Jayanagar, Bangalore', 12.926194, 77.586185, 'Electronics', 'free', 3.7, 68, ''),
    ('Amrutesh Darshini', 'Byrasandra Main Road, Jayanagar, Bangalore', 12.934392, 77.589505, 'Food & Grocery', 'bronze', 3.6, 73, ''),
    ('Sudha Parimala Medical Store', 'Jayanagar, Bangalore', 12.934547, 77.589542, 'Health & Wellness', 'silver', 4.2, 492, ''),
    ('The Guha', 'Jayanagar, Bangalore', 12.933467, 77.583327, 'Food & Grocery', 'silver', 4.2, 155, ''),
    ('Luois Phillepe', 'Jayanagar, Bangalore', 12.930613, 77.586223, 'Clothing', 'bronze', 3.7, 131, ''),
    ('Pin Ball', 'Jayanagar, Bangalore', 12.929452, 77.585028, 'Clothing', 'silver', 4.3, 432, ''),
    ('Sufiyan Dry Fruits', 'Jayanagar, Bangalore', 12.92743, 77.578205, 'Food & Grocery', 'silver', 3.4, 417, ''),
    ('Venezia', 'Jayanagar, Bangalore', 12.927421, 77.577112, 'Food & Grocery', 'bronze', 3.1, 33, ''),
    ('Dosa Corner', 'Jayanagar, Bangalore', 12.927382, 77.576486, 'Food & Grocery', 'free', 3.0, 9, ''),
    ('W', '27th Cross, Jayanagar, Bangalore', 12.930628, 77.585741, 'Clothing', 'silver', 3.6, 182, ''),
    ('Goli Vada Pav', 'Jayanagar, Bangalore', 12.930665, 77.584134, 'Food & Grocery', 'bronze', 3.5, 123, ''),
    ('Corner House', 'Jayanagar, Bangalore', 12.922882, 77.584978, 'Food & Grocery', 'bronze', 3.0, 192, ''),
    ('Samsung', 'Jayanagar, Bangalore', 12.927502, 77.586143, 'Electronics', 'gold', 3.8, 112, ''),
    ('Big Bazar', '#253, 10th cross, Jayanagar, Bangalore', 12.930274, 77.587832, 'Food & Grocery', 'silver', 4.0, 437, ''),
    ('Athithyam', 'Jayanagar, Bangalore', 12.921045, 77.583561, 'Food & Grocery', 'silver', 3.4, 484, ''),
    ('Ample Mart Supermarket', 'Jayanagar, Bangalore', 12.924995, 77.588211, 'Food & Grocery', 'free', 3.6, 13, ''),
    ('Cuppa', 'Jayanagar, Bangalore', 12.940984, 77.585043, 'Food & Grocery', 'free', 3.4, 77, ''),
    ('Reliance digital Xpress', 'Jayanagar, Bangalore', 12.938643, 77.584944, 'Electronics', 'gold', 3.8, 1667, ''),
    ('QuickConnect', 'Jayanagar, Bangalore', 12.930612, 77.584041, 'Electronics', 'gold', 4.1, 1783, ''),
    ("Amma's Pastries", 'Jayanagar, Bangalore', 12.929312, 77.581694, 'Food & Grocery', 'silver', 3.9, 323, ''),
    ('Eden Park Restaurant', 'Jayanagar, Bangalore', 12.923475, 77.585264, 'Food & Grocery', 'free', 3.4, 69, ''),
    ('Karnataka Bhel House', 'Jayanagar, Bangalore', 12.939505, 77.583916, 'Food & Grocery', 'free', 3.3, 38, ''),
    ('Uttara Karnataka Speciality Food Stores', 'Jayanagar, Bangalore', 12.924591, 77.586757, 'Food & Grocery', 'silver', 4.3, 457, ''),
    ('provision store', '2nd Cross KV Colony, Jayanagar, Bangalore', 12.930218, 77.589087, 'Food & Grocery', 'gold', 4.3, 122, ''),
    ('V V Silks and Sarees', 'Jayanagar, Bangalore', 12.936518, 77.583828, 'Clothing', 'silver', 4.0, 113, ''),
    ('Nandini Milk Shoppe', '9th Main Road, Jayanagar, Bangalore', 12.940277, 77.58412, 'Food & Grocery', 'free', 3.3, 50, ''),
    ('Suhas Medicals', 'Jayanagar, Bangalore', 12.939741, 77.582953, 'Health & Wellness', 'bronze', 3.9, 98, ''),
    ('Raghavendra Medicals', 'Jayanagar, Bangalore', 12.937986, 77.582827, 'Health & Wellness', 'free', 3.4, 44, ''),
    ('Amul', 'Jayanagar, Bangalore', 12.928435, 77.584879, 'Food & Grocery', 'free', 3.6, 65, ''),
    ('Cakewalla', 'Jayanagar, Bangalore', 12.928985, 77.586202, 'Food & Grocery', 'free', 2.9, 78, ''),
    ('PSR Silks', 'Jayanagar, Bangalore', 12.936989, 77.58107, 'Clothing', 'gold', 3.9, 471, ''),
    ('Nandini Silks', 'Jayanagar, Bangalore', 12.937235, 77.581058, 'Clothing', 'silver', 4.0, 77, ''),
    ('Paani Kum Chai', 'Tilak Nagar Main Road, Jayanagar, Bangalore', 12.928601, 77.592838, 'Food & Grocery', 'gold', 4.6, 1224, ''),
    ('Mane Holige Kuruk thindi', '7 Main, Jayanagar, Bangalore', 12.928899, 77.581881, 'Food & Grocery', 'bronze', 3.5, 94, '+919742672871'),
    ('Presto', 'Jayanagar, Bangalore', 12.937079, 77.575229, 'Food & Grocery', 'silver', 3.7, 151, ''),
    ('Madhura Bhavan', 'Jayanagar, Bangalore', 12.922158, 77.576215, 'Food & Grocery', 'silver', 3.9, 219, ''),
    ('Cafe Mondo', '2/2, Pattalamma Temple Road, 3rd Block, Jaya Nagar East, Jayanagar, Bangalore', 12.939475, 77.577667, 'Food & Grocery', 'bronze', 3.7, 80, '+918042088686'),
    ('Thindi Katte', '49/1, S Kariyappa Road, South End Road, Basavanagudi, Opposite To Balaji Medicals, Jayanagar, Bangalore', 12.937915, 77.574867, 'Food & Grocery', 'free', 3.3, 24, '+918026622058'),
    ('Imperial Plaza', 'S End Rd, Basavanagudi, Jayanagar, Bangalore', 12.937747, 77.575806, 'Food & Grocery', 'bronze', 3.5, 177, ''),
    ('Vybhava', '35-1, Kanakpur Road, Basavanagudi, Jayanagar, Bangalore', 12.936574, 77.573953, 'Food & Grocery', 'silver', 4.1, 128, '+918026653198'),
    ('The Other Crescent', '49, Nittoor Bhavana, S Kariyappa Road, Basavanagudi, Jayanagar, Bangalore', 12.937044, 77.575326, 'Food & Grocery', 'silver', 3.8, 211, '+918026622058'),
    ('Hotel Sanman', '5, RV Road, South End Circle, Opp Bangalore Hospital, Basavanagudi, Jayanagar, Bangalore', 12.93734, 77.579911, 'Food & Grocery', 'platinum', 4.2, 3065, '+918026570065'),
    ('Shriniwas Medicals', '#16/47/1-C, 34th Cross Road,Jaya Nagar, Jayanagar, Bangalore', 12.925787, 77.586165, 'Health & Wellness', 'free', 2.9, 55, '+918026632258'),
    ('Sudhir Medicals', '#265/B, 36 Cross, near Nethradama Hospital, 7th Block, Jayanagar,, Jayanagar, Bangalore', 12.924316, 77.577029, 'Health & Wellness', 'free', 3.1, 42, '+918022444382'),
    ('Patanjali Chikitsalayas', '#36-44, Krishna Rajendra Road,Yadiyur,7th Block, Jayanagara, Jayanagar, Bangalore', 12.930817, 77.573946, 'Health & Wellness', 'gold', 4.2, 834, '+918026768560'),
    ('Prashanth Pharma', 'rajajinagar, Jayanagar, Bangalore', 12.9349, 77.573964, 'Health & Wellness', 'silver', 3.9, 105, '+918022512593'),
    ('Star Homeopathy', '#771, 34th Cross Road,  4th Block, Jayanagara Jaya Nagar, Jayanagar, Bangalore', 12.926777, 77.583836, 'Health & Wellness', 'free', 3.3, 35, '+919100960066'),
    ('Himalaya Health Care', '# 49, 34th Cross Road, 34th T Block East, Pattabhirama Nagar, Jayanagar, Bangalore', 12.925594, 77.586154, 'Health & Wellness', 'gold', 3.8, 1203, '+918022440364'),
    ('Lifeken Medicines', '#1524/29, 36 A Cross Road, 4th T Block East, Jayanagara, Jayanagar, Bangalore', 12.923142, 77.590042, 'Health & Wellness', 'free', 3.4, 80, '+918022956229'),
    ('Kabab Magic', '31, Opposite Vijaya College, RV Road, Basavanagudi, Jayanagar, Bangalore', 12.940312, 77.579832, 'Food & Grocery', 'free', 2.8, 2, '+918026570381'),
    ('Leanin Tree Art Cafe', '23, Patalamma Street, Basavanagudi, Jayanagar, Bangalore', 12.937724, 77.579454, 'Food & Grocery', 'gold', 4.3, 1764, '+919886218518'),
    ('Gandharva Family Restaurant', 'S End Rd, Yediyur, Basavanagudi, Jayanagar, Bangalore', 12.937512, 77.575998, 'Food & Grocery', 'silver', 3.8, 352, '+918026631181'),
    ('South Thindies', 'Kankapura Road, Yediyur, Basavanagudi, Jayanagar, Bangalore', 12.938888, 77.575579, 'Food & Grocery', 'free', 2.7, 33, ''),
    ('Sri Balaji medicals', '72, 73 24th Main End Puttenahalli J. P Nagar 7th Phase,, Jayanagar, Bangalore', 12.931418, 77.591375, 'Health & Wellness', 'bronze', 4.0, 140, '+918026535363'),
    ('BN Sreekantaiah and Sons', 'Jayanagar, Bangalore', 12.930632, 77.584158, 'Food & Grocery', 'silver', 3.5, 250, ''),
    ('Java City Jayanagar', 'Jayanagar 3rd block, Jayanagar, Bangalore', 12.934917, 77.582902, 'Food & Grocery', 'free', 3.5, 37, ''),
    ('Jayanagar Sport Depot', '24, 9th Main, 4th block, 4th T Block East, Jayanagara Jaya Nagar, Jayanagar, Bangalore', 12.930984, 77.583646, 'Sports', 'free', 3.2, 11, '+918026637998'),
    ('Sportz Arena', '51, 5th Main, Opp Syndicate Bank, 36th Cross Rd, 5th Block, Jayanagara Jaya Nagar, Jayanagar, Bangalore', 12.923593, 77.581337, 'Sports', 'bronze', 3.5, 189, '+918041307068'),
    ('Shama Sports', '1, 1st Floor, Lakshmi Venkateshwara Arcade, 11th Main Road, Off 33rd Cross Road, Opp. KFC, Jayanagar 4th Block, Jayanagar, Bangalore', 12.926179, 77.585864, 'Sports', 'free', 3.7, 78, '+919880149509'),
    ('Sangam Traders', '262, OPP Upahara Darshini, 11th ‘A’ Main, 22nd Cross, 3rd Block,, Jayanagar, Jayanagara Jaya Nagar,, Jayanagar, Bangalore', 12.932987, 77.579259, 'Sports', 'silver', 4.2, 121, '+919844329118'),
    ('Cadence90', '264, 36th B Cross Road, 7th Block, Jayanagar, 7th Block, Jayanagara Jaya Nagar, Jayanagar, Bangalore', 12.924406, 77.577365, 'Sports', 'bronze', 3.2, 164, ''),
    ('Gold Fingers', 'No 138, 1st floor, 6th C main road, Jayanagar 4th Block East, Jayanagar, Bangalore', 12.925765, 77.582415, 'Beauty & Personal Care', 'silver', 4.4, 426, '+919686762189'),
    ('Green Trends Unisex Hair & Style Salon', '57/151, Sharada Plaza, 9th main,, Opposite to State Bank of Mysore, Jayanagar East, Jaya Nagar 1st Block, Jayanagara Jaya Nagar, Jayanagar, Bangalore', 12.93393, 77.583662, 'Beauty & Personal Care', 'silver', 4.3, 70, '+918041231313'),
    ('Bamboo Salon And Spa', 'No 764/44, First Floor, 37th Cross,, 4th Block, Jayanagar, Jayanagar, Bangalore', 12.921616, 77.58749, 'Beauty & Personal Care', 'free', 3.2, 8, '+918040962085'),
    ('Vodafone', 'Jayanagar, Bangalore', 12.930929, 77.583168, 'Electronics', 'free', 2.9, 26, ''),
    ('Guru medicals', 'Jayanagar, Bangalore', 12.935703, 77.59262, 'Health & Wellness', 'bronze', 3.8, 119, ''),
    ('Empire restaurant', 'Jayanagar, Bangalore', 12.933059, 77.581753, 'Food & Grocery', 'gold', 4.2, 1031, ''),
    ('Chowdeshwari', 'Jayanagar, Bangalore', 12.92519, 77.58802, 'Food & Grocery', 'gold', 3.7, 1428, ''),
    ('Kobe Sizzlers', 'Jayanagar, Bangalore', 12.929016, 77.591222, 'Food & Grocery', 'bronze', 3.7, 153, ''),
    ('Satya Medicals', 'Jayanagar, Bangalore', 12.923124, 77.589436, 'Health & Wellness', 'silver', 3.7, 429, ''),
    ('Manish Pharmacy', 'Jayanagar, Bangalore', 12.926353, 77.588043, 'Health & Wellness', 'free', 3.2, 22, ''),
    ('VLCC', 'South End Road, Jayanagar, Bangalore', 12.936943, 77.579146, 'Beauty & Personal Care', 'bronze', 3.4, 170, ''),
    ('Ramraj Cotton', 'Jayanagar, Bangalore', 12.928496, 77.585698, 'Clothing', 'silver', 4.2, 314, ''),
    ('Patanjali Mega Store', 'Jayanagar, Bangalore', 12.925275, 77.58825, 'Food & Grocery', 'free', 3.7, 28, ''),
    ('Acer Lounge', 'Jayanagar, Bangalore', 12.932777, 77.585042, 'Electronics', 'bronze', 3.4, 108, ''),
    ('Cotton Blossom', 'Jayanagar, Bangalore', 12.925367, 77.588229, 'Clothing', 'bronze', 3.5, 174, ''),
    ('Ample Mart', 'Jayanagar, Bangalore', 12.925098, 77.588268, 'Food & Grocery', 'free', 3.6, 56, ''),
    ('Vakulaa Veg Restaurant', 'Jayanagar, Bangalore', 12.930644, 77.586328, 'Food & Grocery', 'free', 2.8, 35, ''),
    ('Choice Fancy And Stationary', 'Jayanagar, Bangalore', 12.931866, 77.589048, 'Stationery', 'free', 2.9, 46, ''),
    ('Srushti Boutique', 'Jayanagar, Bangalore', 12.931836, 77.589027, 'Clothing', 'silver', 4.2, 388, ''),
    ('Sri Bharani Medical and General Store', 'Jayanagar, Bangalore', 12.932285, 77.589363, 'Health & Wellness', 'free', 3.5, 14, ''),
    ('Lakshmi Provisions Store', 'Jayanagar, Bangalore', 12.932317, 77.589391, 'Food & Grocery', 'bronze', 3.5, 73, ''),
    ('Jayanagar Thindi Mane', '36th Cross Road, Jayanagar, Bangalore', 12.923354, 77.58506, 'Food & Grocery', 'silver', 4.0, 260, ''),
    ('MDP Coffee House', 'Jayanagar, Bangalore', 12.921779, 77.585246, 'Food & Grocery', 'free', 2.7, 73, ''),
    ("Pataka Mannar's", '3rd block, Jayanagar, Bangalore', 12.934241, 77.582878, 'Food & Grocery', 'silver', 3.5, 312, ''),
    ('Mi Home', 'Jayanagar, Bangalore', 12.928808, 77.583755, 'Electronics', 'free', 3.3, 7, ''),
    ('Bangarpet Chats Center', 'Jayanagar, Bangalore', 12.921845, 77.585342, 'Food & Grocery', 'free', 3.6, 66, ''),
    ('Balaji Medicals', 'South End Road, Jayanagar, Bangalore', 12.937033, 77.575604, 'Health & Wellness', 'platinum', 4.6, 652, ''),
    ('Karma Kaapi – Caffeine Studio', 'Jayanagar, Bangalore', 12.923256, 77.581408, 'Food & Grocery', 'gold', 4.4, 1782, ''),
    ('Go Native Jayanagar', '10A Main Road, Jayanagar, Bangalore', 12.922608, 77.584646, 'Food & Grocery', 'free', 2.8, 57, ''),
    ('GM home automation center', 'Jayanagar, Bangalore', 12.92855, 77.583066, 'Hardware', 'silver', 3.9, 296, ''),
    ('Basaveshvara Khanavali', 'Jayanagar, Bangalore', 12.932771, 77.584069, 'Food & Grocery', 'free', 3.5, 76, ''),
    ('Tender Coconut Stall', 'Jayanagar, Bangalore', 12.923394, 77.578061, 'Food & Grocery', 'free', 3.3, 23, ''),
    ('KUBO', 'Jayanagar, Bangalore', 12.935214, 77.582936, 'Food & Grocery', 'silver', 3.9, 135, ''),
    ('Maya', 'Jayanagar, Bangalore', 12.921997, 77.584587, 'Food & Grocery', 'silver', 3.7, 282, ''),
    ('Now Boarding Cafe', '68/150/4, 9th Main Road, Jayanagar, Bangalore', 12.933848, 77.583773, 'Food & Grocery', 'silver', 3.5, 243, '+919663568855'),
    ('DYCE', '36th Cross Road, Jayanagar, Bangalore', 12.923586, 77.581457, 'Food & Grocery', 'bronze', 3.1, 71, ''),
    ('Alchemy Coffee Roasters', 'Jayanagar, Bangalore', 12.92327, 77.581033, 'Food & Grocery', 'bronze', 3.9, 195, ''),
    ('Tea Villa Cafe Jayanagar', 'Jayanagar, Bangalore', 12.927579, 77.580895, 'Food & Grocery', 'bronze', 3.1, 44, ''),
    ('Chat Bandi', 'Jayanagar, Bangalore', 12.921903, 77.584976, 'Food & Grocery', 'free', 3.6, 76, ''),
    ('Eat First', 'Jayanagar, Bangalore', 12.921615, 77.584661, 'Food & Grocery', 'free', 3.5, 64, ''),
    ('Isobel', 'Jayanagar, Bangalore', 12.939503, 77.584612, 'Food & Grocery', 'silver', 3.5, 387, ''),
    ('Blue Tokai Coffee Roasters', '303/A-38, 10th Main Road, Jayanagar, Bangalore', 12.92237, 77.584598, 'Food & Grocery', 'free', 3.3, 0, ''),
    ('Fruits shop', 'Jayanagar, Bangalore', 12.939435, 77.583862, 'Food & Grocery', 'gold', 4.5, 1429, ''),
    ('Elaneeru(Tender coconut)', 'Jayanagar, Bangalore', 12.939446, 77.583895, 'Food & Grocery', 'bronze', 3.4, 31, ''),
    ('Kongsi Tea Bar', '62, 11th Main Road, Jayanagar, Bangalore', 12.927356, 77.58615, 'Food & Grocery', 'silver', 3.8, 460, ''),
    ('Evo World Veg Cuisine', 'Jayanagar, Bangalore', 12.926213, 77.577845, 'Food & Grocery', 'bronze', 3.8, 109, ''),
    ('Ishtaa', '60, 30th Cross Road, Jayanagar, Bangalore', 12.928947, 77.581433, 'Food & Grocery', 'silver', 4.1, 439, ''),
    ('BuildTrack', 'Jayanagar, Bangalore', 12.930159, 77.58284, 'Electronics', 'gold', 4.3, 971, ''),
    ('Juice And Chats', 'Jayanagar, Bangalore', 12.928797, 77.581723, 'Food & Grocery', 'free', 3.2, 29, ''),
    ('Ashirwad Supermarket', '18, Patalamma Road, Jayanagar, Bangalore', 12.938074, 77.579268, 'Food & Grocery', 'gold', 3.8, 1325, ''),
    ('Flaunt Salon and Spa', 'Jayanagar, Bangalore', 12.922217, 77.589395, 'Beauty & Personal Care', 'gold', 4.1, 487, ''),
    ('Namma SLN', 'Jayanagar, Bangalore', 12.938527, 77.581979, 'Food & Grocery', 'free', 3.4, 0, ''),
    ('Gayatri Coffee Kendra', 'Jayanagar, Bangalore', 12.929586, 77.576408, 'Food & Grocery', 'silver', 3.5, 53, ''),
    ('Katte Kulture', '425, 18th Main Road, Jayanagar, Bangalore', 12.925876, 77.589426, 'Food & Grocery', 'bronze', 3.2, 100, ''),
    ('Sunnys', '10th Main Road, Jayanagar, Bangalore', 12.922194, 77.585037, 'Food & Grocery', 'bronze', 3.1, 129, ''),
    ('SpiceKlub - Jayanagar', '1st Floor, 125, 10th Cross Road, 1st Block, Jayanagar, Jayanagar, Bangalore', 12.939605, 77.585386, 'Food & Grocery', 'silver', 3.7, 487, '+919108940777'),
    ('Quattro Ristorante', '125, 1st Floor, 10th Cross Road, 1st Block, Jayanagar, Jayanagar, Bangalore', 12.939608, 77.585367, 'Food & Grocery', 'bronze', 3.6, 176, '+919108940777'),
    ('Comfort Collect', 'Jayanagar, Bangalore', 12.926269, 77.582983, 'Clothing', 'free', 3.4, 26, ''),
    ('7th Main Pharma', 'Jayanagar, Bangalore', 12.926154, 77.581871, 'Health & Wellness', 'bronze', 3.8, 143, ''),
    ('The Collective Outlet', 'Jayanagar, Bangalore', 12.925236, 77.58368, 'Clothing', 'free', 3.3, 61, ''),
    ('Food Spring', 'Jayanagar, Bangalore', 12.926303, 77.582145, 'Food & Grocery', 'free', 3.0, 44, ''),
    ('Perimeter Salon', 'Jayanagar, Bangalore', 12.9263, 77.582063, 'Beauty & Personal Care', 'silver', 3.8, 120, ''),
    ('Ice Cream Wonderland And Pizzeria', 'Jayanagar, Bangalore', 12.925784, 77.583949, 'Food & Grocery', 'gold', 4.4, 1631, ''),
    ('Huber And Holly', 'Jayanagar, Bangalore', 12.925696, 77.583944, 'Clothing', 'free', 3.5, 20, ''),
    ('Chaayos', 'Jayanagar, Bangalore', 12.924855, 77.58392, 'Food & Grocery', 'free', 3.2, 43, ''),
    ('Krishna Cafe', 'Jayanagar, Bangalore', 12.923194, 77.585199, 'Food & Grocery', 'gold', 4.3, 1501, ''),
    ('Oppo', 'Jayanagar, Bangalore', 12.932726, 77.583758, 'Electronics', 'silver', 3.6, 375, ''),
    ('The Bloom', 'Jayanagar, Bangalore', 12.926111, 77.589836, 'Food & Grocery', 'free', 2.8, 18, ''),
    ('block two coffee', 'Jayanagar, Bangalore', 12.940161, 77.583947, 'Food & Grocery', 'bronze', 3.6, 42, ''),
    ('Dakshin Thindi', 'Jayanagar, Bangalore', 12.939649, 77.585302, 'Food & Grocery', 'free', 2.9, 10, ''),
    ('Crossword', 'Jayanagar, Bangalore', 12.920727, 77.583538, 'Books', 'silver', 4.0, 420, ''),
    ("Uncle Peter's Pancakes", 'Jayanagar, Bangalore', 12.920632, 77.583534, 'Food & Grocery', 'bronze', 4.0, 177, ''),
    ('Chikmagalur Estate Coffee', 'Jayanagar, Bangalore', 12.925223, 77.58467, 'Food & Grocery', 'silver', 3.7, 355, ''),
    ('Kaapi Corner', 'Jayanagar, Bangalore', 12.925408, 77.583606, 'Food & Grocery', 'free', 3.5, 22, ''),
    ('Karavali Kitchen', 'Jayanagar, Bangalore', 12.929719, 77.576756, 'Food & Grocery', 'silver', 3.8, 153, ''),
    ('Om Sri Biryani Point', '42, Krishna Rajendra Road, Jayanagar, Bangalore', 12.926938, 77.57372, 'Food & Grocery', 'gold', 4.6, 1161, ''),
    ('Azorte', 'Jayanagar, Bangalore', 12.940216, 77.584893, 'Clothing', 'free', 2.7, 76, ''),
    ('Umesh Dosa Point', 'Jayanagar, Bangalore', 12.939485, 77.583912, 'Food & Grocery', 'bronze', 3.7, 177, ''),
    ('Moai', 'Jayanagar, Bangalore', 12.924984, 77.583948, 'Food & Grocery', 'free', 2.7, 30, ''),
    ('Garci', 'Jayanagar, Bangalore', 12.928279, 77.576614, 'Food & Grocery', 'silver', 4.4, 94, ''),
    ('Desi Masala', 'Jayanagar, Bangalore', 12.928159, 77.583821, 'Food & Grocery', 'platinum', 4.8, 3392, ''),
    ('MDRegion', 'Jayanagar, Bangalore', 12.927596, 77.575906, 'Sports', 'silver', 3.8, 152, '+918041139083'),
    ('Coffee Naa Tea Naa', 'Jayanagar, Bangalore', 12.921666, 77.580865, 'Food & Grocery', 'bronze', 3.7, 37, ''),
    ('Milano Pizza', 'Jayanagar, Bangalore', 12.922718, 77.58393, 'Food & Grocery', 'gold', 4.2, 1587, ''),
    ('Chai Days', 'Jayanagar, Bangalore', 12.930974, 77.579566, 'Food & Grocery', 'gold', 4.6, 812, ''),
    ('Iris Cafe', 'Jayanagar, Bangalore', 12.923106, 77.585279, 'Food & Grocery', 'bronze', 3.8, 77, ''),
    ('Edge Cafe', 'Jayanagar, Bangalore', 12.935039, 77.585919, 'Food & Grocery', 'silver', 3.6, 256, ''),
    ('Juicy Bites', 'Jayanagar, Bangalore', 12.935227, 77.585729, 'Food & Grocery', 'silver', 3.7, 258, ''),
    ('Andhra Ruchulu', 'Jayanagar, Bangalore', 12.933225, 77.583378, 'Food & Grocery', 'bronze', 3.6, 148, ''),
    ('Snuzzles', 'Jayanagar, Bangalore', 12.926911, 77.584297, 'Food & Grocery', 'free', 3.6, 65, ''),
    ('Autocorsa', 'Jayanagar, Bangalore', 12.92323, 77.581985, 'Automotive', 'platinum', 4.9, 2549, ''),
    ('Om Stationery and Snacks', 'Jayanagar, Bangalore', 12.924385, 77.578299, 'Stationery', 'silver', 4.4, 338, ''),
    ('Chalukya Samrat', 'Jayanagar, Bangalore', 12.92856, 77.581973, 'Food & Grocery', 'bronze', 3.2, 106, ''),
    ('First Coffee', 'Jayanagar, Bangalore', 12.921862, 77.584975, 'Food & Grocery', 'platinum', 4.5, 4274, ''),
    ('Fast Coffee', 'Jayanagar, Bangalore', 12.922741, 77.584664, 'Food & Grocery', 'gold', 4.6, 1087, ''),
    ('Acharya Cafe', 'Jayanagar, Bangalore', 12.925747, 77.573709, 'Food & Grocery', 'free', 2.8, 28, ''),
    ('Raha You', '35th A Cross St, 1,, 15th Main Road, Jayanagar, Bangalore', 12.923837, 77.587612, 'Food & Grocery', 'silver', 3.6, 211, '+916364461575'),
    ('Smart Bazaar', 'Marenahalli Road, JP Nagar, Bangalore', 12.91722, 77.592314, 'Food & Grocery', 'bronze', 3.4, 157, ''),
    ('Prasiddhi', 'JP Nagar, Bangalore', 12.910984, 77.579122, 'Food & Grocery', 'free', 3.5, 31, ''),
    ('Incara', 'JP Nagar, Bangalore', 12.902773, 77.583754, 'Food & Grocery', 'free', 3.1, 43, ''),
    ('Pantaloons Onloop', 'JP Nagar, Bangalore', 12.916532, 77.59237, 'Clothing', 'silver', 3.5, 141, ''),
    ('Karnataka Hardware Shop', 'JP Nagar, Bangalore', 12.916876, 77.590387, 'Hardware', 'bronze', 3.4, 166, ''),
    ('Shiv Sagar Signature', 'JP Nagar, Bangalore', 12.91661, 77.591458, 'Food & Grocery', 'free', 3.6, 24, ''),
    ('Food Court', 'JP Nagar, Bangalore', 12.916353, 77.592129, 'Food & Grocery', 'bronze', 3.2, 81, ''),
    ('Venkateshwara Stationary', 'JP Nagar, Bangalore', 12.912204, 77.578499, 'Stationery', 'gold', 3.9, 515, ''),
    ('Vidya Medicals', 'JP Nagar, Bangalore', 12.913553, 77.578787, 'Health & Wellness', 'gold', 4.5, 829, ''),
    ('Angadi Silks', 'JP Nagar, Bangalore', 12.916943, 77.582235, 'Clothing', 'free', 2.8, 10, ''),
    ('Cable Car Restaurant', 'JP Nagar, Bangalore', 12.916841, 77.585148, 'Food & Grocery', 'silver', 4.1, 364, ''),
    ('Adithya', 'JP Nagar, Bangalore', 12.911864, 77.586761, 'Food & Grocery', 'free', 3.6, 5, ''),
    ('Raghavendra Upahara', 'JP Nagar, Bangalore', 12.917046, 77.593152, 'Food & Grocery', 'gold', 4.1, 745, ''),
    ('Pai Princess', 'JP Nagar, Bangalore', 12.91668, 77.589844, 'Food & Grocery', 'bronze', 4.0, 167, ''),
    ('UpSouth', 'JP Nagar, Bangalore', 12.91704, 77.59344, 'Food & Grocery', 'gold', 4.2, 1665, ''),
    ('The Onyx', '15th Cross, JP Nagar, Bangalore', 12.906499, 77.593019, 'Food & Grocery', 'gold', 4.0, 554, ''),
    ('Posh Little', 'JP Nagar, Bangalore', 12.9017, 77.582379, 'Toys & Baby', 'silver', 3.5, 319, ''),
    ('Royal Supermarket', 'JP Nagar, Bangalore', 12.900724, 77.586966, 'Food & Grocery', 'silver', 3.9, 326, ''),
    ('Grills and Rolls', 'JP Nagar, Bangalore', 12.900454, 77.585929, 'Food & Grocery', 'free', 3.0, 22, ''),
    ('Greengold Store', 'JP Nagar, Bangalore', 12.906138, 77.595186, 'Toys & Baby', 'bronze', 3.1, 152, ''),
    ('Cakewala', 'JP Nagar, Bangalore', 12.913357, 77.585931, 'Food & Grocery', 'bronze', 3.7, 23, ''),
    ('Siddhas Veg', 'JP Nagar, Bangalore', 12.912321, 77.585591, 'Food & Grocery', 'silver', 3.8, 101, ''),
    ('Yash Technologies', 'JP Nagar, Bangalore', 12.912401, 77.585601, 'Electronics', 'bronze', 3.6, 86, ''),
    ('Viveks', 'JP Nagar, Bangalore', 12.914979, 77.585913, 'Electronics', 'free', 3.0, 34, ''),
    ('Linen Club - The Ramp', 'JP Nagar, Bangalore', 12.905957, 77.58558, 'Clothing', 'bronze', 3.6, 113, ''),
    ('Wangs Kitchen', 'JP Nagar, Bangalore', 12.90612, 77.592608, 'Food & Grocery', 'free', 3.1, 53, ''),
    ('Nammoora', '58, 22nd Main, Marenahalli Extension, 2nd Phase, JP Nagar, JP Nagar, Bangalore', 12.9045, 77.588826, 'Food & Grocery', 'silver', 3.8, 487, '+918026490077'),
    ('Eye World Opticians', 'JP Nagar, Bangalore', 12.905148, 77.585838, 'Health & Wellness', 'bronze', 3.5, 166, ''),
    ('Funky Punjab', 'JP Nagar, Bangalore', 12.900156, 77.585937, 'Food & Grocery', 'free', 3.3, 29, ''),
    ('Vasantha Grand', 'JP Nagar, Bangalore', 12.904381, 77.5859, 'Food & Grocery', 'gold', 4.4, 1746, ''),
    ('Kidzon', 'JP Nagar, Bangalore', 12.903341, 77.585602, 'Toys & Baby', 'free', 3.1, 48, ''),
    ('Vinaya Cafe', 'JP Nagar, Bangalore', 12.905896, 77.580298, 'Food & Grocery', 'free', 3.5, 19, ''),
    ('Just Kids', 'JP Nagar, Bangalore', 12.899926, 77.585919, 'Toys & Baby', 'bronze', 3.0, 127, ''),
    ('White Lotus', '24th Main Road, JP Nagar, Bangalore', 12.900927, 77.585955, 'Health & Wellness', 'bronze', 4.0, 181, ''),
    ('Karavali Garden Restaurant', 'JP Nagar, Bangalore', 12.904466, 77.580246, 'Food & Grocery', 'bronze', 3.7, 144, ''),
    ('Sri Upahara', 'JP Nagar, Bangalore', 12.910502, 77.581613, 'Food & Grocery', 'bronze', 3.6, 165, ''),
    ('Hot N Cool', 'JP Nagar, Bangalore', 12.910501, 77.581848, 'Food & Grocery', 'free', 2.8, 20, ''),
    ('H.S.M Iyengar', 'JP Nagar, Bangalore', 12.910505, 77.582081, 'Food & Grocery', 'free', 3.5, 60, ''),
    ('Spicy Tree', 'JP Nagar, Bangalore', 12.910614, 77.58059, 'Food & Grocery', 'gold', 3.8, 1624, ''),
    ('Life Care Medicals', 'JP Nagar, Bangalore', 12.910805, 77.578669, 'Health & Wellness', 'bronze', 3.4, 163, ''),
    ('Hello Baby', 'JP Nagar, Bangalore', 12.910803, 77.578713, 'Toys & Baby', 'silver', 4.2, 226, ''),
    ('Rolls of heaven', 'JP Nagar, Bangalore', 12.911023, 77.578561, 'Food & Grocery', 'free', 2.8, 77, ''),
    ('Sri Devi', 'JP Nagar, Bangalore', 12.910846, 77.578283, 'Food & Grocery', 'free', 3.3, 68, ''),
    ('Sunrise Restaurant', 'JP Nagar, Bangalore', 12.911074, 77.57804, 'Food & Grocery', 'free', 3.5, 45, ''),
    ('Nayana Fabco', 'JP Nagar, Bangalore', 12.911074, 77.578095, 'Clothing', 'bronze', 3.5, 134, ''),
    ('Shree Sai Ram Drug House', 'JP Nagar, Bangalore', 12.910414, 77.581808, 'Health & Wellness', 'gold', 4.1, 686, ''),
    ('Sri Krishna Cake Palace', '1st floor, Sarakki Main Road (9th Cross), JP Nagar, Bangalore', 12.910505, 77.581862, 'Food & Grocery', 'bronze', 4.0, 62, ''),
    ('Omika Bridal Hair & Skin Care', 'JP Nagar, Bangalore', 12.910499, 77.581829, 'Beauty & Personal Care', 'silver', 4.3, 265, ''),
    ('Mithai Mane', 'JP Nagar, Bangalore', 12.906617, 77.585881, 'Food & Grocery', 'silver', 4.0, 484, ''),
    ('Crunch Sip', 'Sarakki Main Road (9th Cross), JP Nagar, Bangalore', 12.910578, 77.580708, 'Food & Grocery', 'gold', 3.8, 803, ''),
    ('24th Main Pan Indian Bistro', 'JP Nagar, Bangalore', 12.908561, 77.585911, 'Food & Grocery', 'free', 3.0, 37, '+919900092564'),
    ('Zaitoon', '24th Main, JP Nagar, Bangalore', 12.903081, 77.585579, 'Food & Grocery', 'silver', 4.0, 252, ''),
    ('Sea Salt', '24th Main, JP Nagar, Bangalore', 12.901729, 77.585763, 'Food & Grocery', 'free', 3.1, 46, ''),
    ('Egg Factory', 'Outer Ring Road, JP Nagar, Bangalore', 12.906146, 77.590622, 'Food & Grocery', 'silver', 3.9, 280, ''),
    ('Pipe Yard', 'Outer Ring Road, JP Nagar, Bangalore', 12.906103, 77.592015, 'Food & Grocery', 'bronze', 3.2, 25, ''),
    ('Garden By Su', '499/32-4, 9th Main, 46th Cross Rd, 5th Block, Jayanagar, JP Nagar, Bangalore', 12.916671, 77.583421, 'Food & Grocery', 'free', 2.9, 32, '+918971228404'),
    ('Namma Adda', 'JP Nagar, Bangalore', 12.911833, 77.585913, 'Food & Grocery', 'gold', 4.4, 263, ''),
    ('Durga Medicals', '53/A, 9th Cross, Near-Oxford Collage, J P Nagar 1st Phase, JP Nagar, Bangalore', 12.910553, 77.581109, 'Health & Wellness', 'free', 3.1, 12, '+918026911062'),
    ('Trinity Diagnostic And Imaging Research Centre', '213, 9th Cross Road, JP Nagar, Bangalore', 12.91032, 77.593563, 'Health & Wellness', 'free', 3.6, 70, '+918026581757'),
    ('Hercules Sport International', '664, 11th A Main Rd, 4th T Block East, Pattabhirama Nagar, Jayanagara Jaya Nagar, JP Nagar, Bangalore', 12.91672, 77.586452, 'Sports', 'free', 3.5, 67, '+919741095147'),
    ('SportsBunke.in', '1, Sudarsanamma Arcade Opp R.V.Dental Collage J.P., Marenahalli, 2nd Phase, JP Nagar, JP Nagar, Bangalore', 12.911641, 77.586084, 'Sports', 'silver', 3.5, 184, '+918041233530'),
    ('Aveda Oasis', 'no:52, 17th Main Road, JP Nagar, Bangalore', 12.906582, 77.590597, 'Beauty & Personal Care', 'silver', 3.5, 249, '+918026592930'),
    ('Trans studio', 'No.166,Ground Floor, 4th Cross,Dollars Colony, Jp Nagar 4th Phase, Jp Nagar, JP Nagar, Bangalore', 12.918551, 77.585631, 'Beauty & Personal Care', 'bronze', 3.2, 92, '+918892183101'),
    ('Adishwar Electronics', 'JP Nagar, Bangalore', 12.919845, 77.583641, 'Electronics', 'bronze', 3.1, 154, ''),
    ('Ranga Shankara Cafe', 'JP Nagar, Bangalore', 12.911453, 77.587088, 'Food & Grocery', 'gold', 3.7, 1458, ''),
    ('ಶ್ರೀ ಕೃಷ್ಣ ದರ್ಶಿನಿ', '18th Cross Road, JP Nagar, Bangalore', 12.900692, 77.586878, 'Food & Grocery', 'bronze', 3.9, 141, ''),
    ('Banagalore Fresh & Fresh', '92, 15th Main Road, JP Nagar, Bangalore', 12.903726, 77.591772, 'Food & Grocery', 'bronze', 3.9, 89, ''),
    ('Mandarthi Veg', 'JP Nagar, Bangalore', 12.917394, 77.578114, 'Food & Grocery', 'bronze', 3.1, 198, ''),
    ('Hyderabad Biryani', 'JP Nagar, Bangalore', 12.916865, 77.585947, 'Food & Grocery', 'bronze', 3.6, 136, ''),
    ('Kakal Kai Ruchi', '18th cross road, JP Nagar, Bangalore', 12.900769, 77.587275, 'Food & Grocery', 'silver', 4.0, 338, ''),
    ('Sri Lakshmi Medicals', '23rd main, Ramana Road, Ayodhya Nagar, JP Nagar, Bangalore', 12.900645, 77.586604, 'Health & Wellness', 'bronze', 3.6, 156, ''),
    ('Once In Nature (Organic Cafe)', '24th Main, JP Nagar, 5th phase, JP Nagar, Bangalore', 12.900145, 77.58592, 'Food & Grocery', 'bronze', 3.3, 158, ''),
    ('Prem Pharmacy', '22nd B Main Road, JP Nagar, Bangalore', 12.900699, 77.587267, 'Health & Wellness', 'free', 2.8, 39, ''),
    ('LJ Iyengars Bakery', '18th Cross Road, JP Nagar, Bangalore', 12.9007, 77.58729, 'Food & Grocery', 'bronze', 3.3, 97, ''),
    ('Hotel Sankaranarayana Tiffanes', '24th Main Road, JP Nagar, Bangalore', 12.900104, 77.585916, 'Food & Grocery', 'free', 3.2, 17, ''),
    ("Adam & Eve's Unisex Salon", '24th main road, 5th Phase, JP Nagar, JP Nagar, Bangalore', 12.899818, 77.585916, 'Beauty & Personal Care', 'bronze', 3.5, 22, ''),
    ('Arogya Organic', '24th Main Road, JP Nagar, Bangalore', 12.900139, 77.585932, 'Food & Grocery', 'silver', 3.9, 436, ''),
    ('Chulha Chauki da Dhaba', 'JP Nagar, Bangalore', 12.912271, 77.586098, 'Food & Grocery', 'bronze', 3.0, 130, ''),
    ('Bengaluru Drug House Pharmacy', 'JP Nagar, Bangalore', 12.901702, 77.585795, 'Health & Wellness', 'silver', 4.0, 457, ''),
    ('Kolkata Famous Kati Roll', 'JP Nagar, Bangalore', 12.899703, 77.585928, 'Food & Grocery', 'free', 3.1, 52, ''),
    ('Cheesiano Pizza', 'JP Nagar, Bangalore', 12.902109, 77.585773, 'Food & Grocery', 'gold', 4.1, 611, ''),
    ('Lenskart.com', 'JP Nagar, Bangalore', 12.899612, 77.585916, 'Health & Wellness', 'free', 3.4, 49, ''),
    ('Vidya Cafe', 'JP Nagar, Bangalore', 12.912341, 77.591384, 'Food & Grocery', 'gold', 3.9, 262, ''),
    ('Kia', 'Aurobindo Marg (24th Main), JP Nagar, Bangalore', 12.908327, 77.58552, 'Automotive', 'bronze', 3.2, 176, ''),
    ('SN Refreshment', '12th Cross Road, JP Nagar, Bangalore', 12.908326, 77.587122, 'Food & Grocery', 'gold', 4.4, 683, ''),
    ('Kesariya', 'JP Nagar, Bangalore', 12.906605, 77.58962, 'Food & Grocery', 'silver', 3.9, 187, ''),
    ('Neemsi', 'JP Nagar, Bangalore', 12.906174, 77.582544, 'Food & Grocery', 'bronze', 3.9, 66, ''),
    ('Terrace by House of Commons', 'JP Nagar, Bangalore', 12.914271, 77.585974, 'Food & Grocery', 'free', 2.8, 58, ''),
    ('Bramble', 'JP Nagar, Bangalore', 12.916968, 77.579251, 'Food & Grocery', 'silver', 3.7, 368, ''),
    ('Rathna Medical Store', 'JP Nagar, Bangalore', 12.912416, 77.57502, 'Health & Wellness', 'bronze', 3.3, 58, ''),
    ('Farm & Co.', 'JP Nagar, Bangalore', 12.919705, 77.584112, 'Food & Grocery', 'free', 3.2, 23, ''),
    ('The Rameshwaram Cafe', 'JP Nagar, Bangalore', 12.906509, 77.590586, 'Food & Grocery', 'platinum', 4.3, 1524, ''),
    ('The Kind Roastery and Brewroom', 'JP Nagar, Bangalore', 12.910594, 77.589615, 'Food & Grocery', 'silver', 4.1, 169, '+9108042112800'),
    ('Kumaran Silk', 'JP Nagar, Bangalore', 12.916869, 77.586183, 'Clothing', 'free', 2.7, 27, ''),
    ('YLG', 'JP Nagar, Bangalore', 12.916818, 77.586658, 'Beauty & Personal Care', 'bronze', 3.3, 147, ''),
    ('Bombay Kulfi and Rabdi', 'JP Nagar, Bangalore', 12.917245, 77.585982, 'Food & Grocery', 'platinum', 4.5, 759, ''),
    ('Kamakshi Silks', 'JP Nagar, Bangalore', 12.917273, 77.584285, 'Clothing', 'bronze', 3.8, 57, ''),
    ('Mugdha', 'JP Nagar, Bangalore', 12.91688, 77.582502, 'Clothing', 'free', 3.5, 9, ''),
    ('Thirumal', 'JP Nagar, Bangalore', 12.917267, 77.581988, 'Clothing', 'bronze', 3.2, 84, ''),
    ('Merwans', 'JP Nagar, Bangalore', 12.916686, 77.585665, 'Food & Grocery', 'gold', 4.5, 349, ''),
    ('Vyana World Cuisine', 'JP Nagar, Bangalore', 12.910027, 77.595377, 'Food & Grocery', 'platinum', 4.7, 3652, ''),
    ('Khmer Kitchen', 'JP Nagar, Bangalore', 12.906532, 77.58664, 'Food & Grocery', 'silver', 3.8, 427, ''),
    ('Societe Rangoon', 'JP Nagar, Bangalore', 12.906707, 77.589686, 'Food & Grocery', 'platinum', 4.2, 1269, ''),
    ('Eastern Edition', 'JP Nagar, Bangalore', 12.916671, 77.593798, 'Clothing', 'bronze', 4.0, 141, ''),
    ('Twin Birds', 'JP Nagar, Bangalore', 12.916664, 77.593704, 'Clothing', 'bronze', 4.0, 102, ''),
    ('Vision Express', 'JP Nagar, Bangalore', 12.916671, 77.593616, 'Health & Wellness', 'free', 3.4, 70, ''),
    ('Biryani Mane', 'JP Nagar, Bangalore', 12.916668, 77.593032, 'Food & Grocery', 'bronze', 3.3, 183, ''),
    ('babyhug', 'JP Nagar, Bangalore', 12.906125, 77.593416, 'Toys & Baby', 'free', 3.1, 6, ''),
    ('Escape', 'JP Nagar, Bangalore', 12.906119, 77.593415, 'Clothing', 'gold', 3.9, 331, ''),
    ('Kimchi', '312, 15th Cross Road, JP Nagar 5th Phase, Bangalore', 12.906114, 77.593415, 'Food & Grocery', 'free', 3.1, 1, '+917022450852'),
    ('Sattvam', 'JP Nagar, Bangalore', 12.906084, 77.586789, 'Food & Grocery', 'free', 3.0, 15, ''),
    ('Sahukar Chai', 'JP Nagar, Bangalore', 12.906512, 77.590438, 'Food & Grocery', 'bronze', 3.2, 30, ''),
    ('SLV Refreshments', 'JP Nagar, Bangalore', 12.906463, 77.590135, 'Food & Grocery', 'silver', 4.1, 402, ''),
    ('Jayanagara Donne Biryani', 'JP Nagar, Bangalore', 12.906565, 77.590458, 'Food & Grocery', 'silver', 3.8, 95, ''),
    ('Peek-a-Boo', 'JP Nagar, Bangalore', 12.90613, 77.588434, 'Food & Grocery', 'free', 3.2, 79, ''),
    ('Yuki', 'JP Nagar, Bangalore', 12.906056, 77.589807, 'Food & Grocery', 'bronze', 4.0, 180, ''),
    ('154 Breakfast Club', 'JP Nagar, Bangalore', 12.906563, 77.59256, 'Food & Grocery', 'free', 3.1, 19, ''),
    ('Welcome Upahara', 'JP Nagar, Bangalore', 12.914956, 77.594941, 'Food & Grocery', 'silver', 4.2, 114, ''),
    ('Arogya Ahaara', 'JP Nagar, Bangalore', 12.910806, 77.582129, 'Food & Grocery', 'bronze', 3.3, 162, ''),
    ('Bendakaluru Donne Briyani', 'JP Nagar, Bangalore', 12.906788, 77.585878, 'Food & Grocery', 'silver', 4.2, 462, ''),
    ('Top Store', 'JP Nagar, Bangalore', 12.906462, 77.590084, 'Food & Grocery', 'gold', 4.4, 143, ''),
    ('Skyline Pizzeria', 'JP Nagar, Bangalore', 12.906255, 77.57787, 'Food & Grocery', 'silver', 3.8, 118, ''),
    ('Paradise Biryani', 'JP Nagar, Bangalore', 12.910301, 77.585887, 'Food & Grocery', 'free', 2.7, 56, ''),
    ('Renault', 'JP Nagar, Bangalore', 12.906627, 77.580506, 'Automotive', 'silver', 3.9, 300, ''),
    ('Friends Automotives', 'JP Nagar, Bangalore', 12.906099, 77.587459, 'Automotive', 'free', 2.9, 44, ''),
    ('Eagle Bar & Restaurant', 'JP Nagar, Bangalore', 12.906155, 77.590331, 'Food & Grocery', 'bronze', 3.5, 173, ''),
    ('Sri Venkateshwara', 'JP Nagar, Bangalore', 12.906151, 77.590893, 'Food & Grocery', 'gold', 4.3, 241, ''),
    ('Truly', 'JP Nagar, Bangalore', 12.906954, 77.580307, 'Food & Grocery', 'bronze', 3.4, 87, ''),
    ('Kashyapi Naturals', 'JP Nagar, Bangalore', 12.907426, 77.582455, 'Food & Grocery', 'bronze', 3.5, 183, ''),
    ('Harsitha Medicals', 'JP Nagar, Bangalore', 12.907214, 77.583, 'Health & Wellness', 'bronze', 3.7, 24, ''),
    ('Pharma One', 'JP Nagar, Bangalore', 12.910669, 77.583537, 'Health & Wellness', 'silver', 3.8, 367, '+919811789001'),
    ('Howzzat Sports', 'JP Nagar, Bangalore', 12.910472, 77.582256, 'Sports', 'free', 3.4, 68, ''),
    ('Aata Oota Maathu', 'JP Nagar, Bangalore', 12.910476, 77.582203, 'Food & Grocery', 'gold', 3.9, 1499, ''),
    ('Cream And Fudge', 'JP Nagar, Bangalore', 12.910476, 77.582203, 'Food & Grocery', 'bronze', 3.6, 176, ''),
    ('Maruit Suzuki Nexa', 'JP Nagar, Bangalore', 12.90608, 77.587813, 'Automotive', 'free', 3.1, 41, ''),
    ('The organic world', 'JP Nagar, Bangalore', 12.906584, 77.588634, 'Food & Grocery', 'free', 3.2, 8, ''),
    ("Hair Sessions Men's Saloon", 'JP Nagar, Bangalore', 12.908629, 77.583479, 'Beauty & Personal Care', 'free', 3.6, 22, ''),
    ('Lakeview milk bar since 1930', 'JP Nagar, Bangalore', 12.906129, 77.578027, 'Food & Grocery', 'gold', 3.9, 1094, ''),
    ('Royal Lifestyle', 'JP Nagar, Bangalore', 12.906287, 77.57652, 'Automotive', 'gold', 3.9, 165, ''),
    ('Casa Piccosa', 'JP Nagar, Bangalore', 12.906062, 77.585524, 'Food & Grocery', 'silver', 4.2, 386, ''),
    ('Global Hardware & Power Tools', 'JP Nagar, Bangalore', 12.906127, 77.586054, 'Hardware', 'bronze', 3.6, 171, ''),
    ('NI YU', 'JP Nagar, Bangalore', 12.906567, 77.586408, 'Food & Grocery', 'free', 3.1, 77, ''),
    ('Kinderdreams Baby Store', 'JP Nagar, Bangalore', 12.910203, 77.587849, 'Toys & Baby', 'silver', 3.4, 156, ''),
    ('Chetan Hardware', 'JP Nagar, Bangalore', 12.906458, 77.589953, 'Hardware', 'silver', 4.3, 476, ''),
    ('Van Heusen Inner Wear', 'JP Nagar, Bangalore', 12.911687, 77.585943, 'Clothing', 'free', 3.5, 7, ''),
    ('Benki Coffee', 'JP Nagar, Bangalore', 12.910314, 77.587576, 'Food & Grocery', 'bronze', 3.7, 126, ''),
    ('Nandini Shoppe', 'JP Nagar, Bangalore', 12.908373, 77.584856, 'Food & Grocery', 'gold', 4.5, 219, ''),
    ('The Big Market', 'JP Nagar, Bangalore', 12.906371, 77.577755, 'Food & Grocery', 'gold', 4.0, 183, ''),
    ('Baba Punjabi', 'JP Nagar, Bangalore', 12.910576, 77.588019, 'Food & Grocery', 'silver', 4.2, 89, ''),
    ('Motors', 'JP Nagar, Bangalore', 12.906481, 77.590995, 'Automotive', 'gold', 4.7, 298, ''),
    ('Tafe Access Ltd', 'JP Nagar, Bangalore', 12.906135, 77.591157, 'Automotive', 'free', 2.9, 49, ''),
    ('enrich', 'JP Nagar, Bangalore', 12.906124, 77.591285, 'Beauty & Personal Care', 'free', 3.2, 31, ''),
    ('Ramdev Stationery', 'JP Nagar, Bangalore', 12.910352, 77.581813, 'Stationery', 'silver', 4.3, 335, ''),
    ("New Classic Men's Parlour", 'JP Nagar, Bangalore', 12.910429, 77.581829, 'Beauty & Personal Care', 'silver', 3.8, 104, '+919900615439'),
    ('Ramdev Stationers', 'JP Nagar, Bangalore', 12.910429, 77.581829, 'Electronics', 'silver', 4.0, 395, ''),
    ('VRK Heritage', 'JP Nagar, Bangalore', 12.916872, 77.583406, 'Clothing', 'bronze', 3.7, 46, ''),
    ('Bengaluru Drug House', 'JP Nagar, Bangalore', 12.916697, 77.592964, 'Health & Wellness', 'free', 2.9, 51, ''),
    ('Mojo Pizza', 'JP Nagar, Bangalore', 12.916747, 77.585669, 'Food & Grocery', 'bronze', 3.7, 156, ''),
    ('Vishranti Bhavan', 'JP Nagar, Bangalore', 12.916836, 77.58546, 'Food & Grocery', 'platinum', 4.4, 3386, ''),
    ('Sri Vinayaka Juice', 'JP Nagar, Bangalore', 12.919771, 77.580932, 'Food & Grocery', 'gold', 4.3, 764, ''),
    ('Atelier', 'JP Nagar, Bangalore', 12.917194, 77.586517, 'Clothing', 'gold', 4.1, 1396, ''),
    ('JP Nagara Social', 'Outer Ring Road, JP Nagar, Bangalore', 12.906176, 77.580505, 'Food & Grocery', 'bronze', 3.7, 175, ''),
    ('Eating Love', '69, 9th Main Road, Jayanagar, Bangalore', 12.91936, 77.583861, 'Food & Grocery', 'free', 3.2, 48, ''),
    ('Suay', '7, Outer Ring Road, JP Nagar, Bangalore', 12.906053, 77.5891, 'Food & Grocery', 'silver', 4.3, 180, '+918884552022'),
    ('Take A Break', 'JP Nagar, Bangalore', 12.906036, 77.588943, 'Food & Grocery', 'bronze', 3.5, 121, ''),
    ('Samskruthi Sathkara', 'JP Nagar, Bangalore', 12.90305, 77.585911, 'Food & Grocery', 'free', 2.7, 36, ''),
    ('Brewing Untold Stories', 'JP Nagar, Bangalore', 12.919896, 77.589806, 'Food & Grocery', 'silver', 3.9, 204, ''),
    ('Nenapu', 'JP Nagar, Bangalore', 12.906071, 77.59436, 'Food & Grocery', 'free', 3.7, 27, ''),
    ('Cafe Petriqor', 'JP Nagar, Bangalore', 12.906682, 77.585137, 'Food & Grocery', 'gold', 3.7, 1783, ''),
    ('Gaia', 'JP Nagar, Bangalore', 12.906049, 77.586435, 'Food & Grocery', 'gold', 4.5, 1982, ''),
    ('Drip and Drop Coffee', 'No 289, 15th Cross, 5th Phose, JP Nagar, JP Nagar, Bangalore', 12.906136, 77.590501, 'Food & Grocery', 'silver', 4.0, 342, '+919538238355'),
    ('Siyara Spa in JP Nagar', '24th Main Road, JP Nagar, Bangalore', 12.905234, 77.585597, 'Beauty & Personal Care', 'free', 2.7, 45, '+919148112300'),
    ('Iris Cafe and Kitchen', 'JP Nagar, Bangalore', 12.905705, 77.583182, 'Food & Grocery', 'free', 3.0, 74, ''),
    ('Trot - The Republic of Taste', 'JP Nagar, Bangalore', 12.910428, 77.591071, 'Food & Grocery', 'free', 3.0, 38, ''),
    ('Puncture Star Services', 'JP Nagar, Bangalore', 12.910345, 77.594915, 'Automotive', 'free', 3.1, 27, '+919886337871'),
    ('Sri Deep a Retail', 'JP Nagar, Bangalore', 12.910098, 77.592184, 'Food & Grocery', 'silver', 4.1, 455, ''),
    ('Mamta Super Bazar', 'JP Nagar, Bangalore', 12.910441, 77.590555, 'Food & Grocery', 'bronze', 3.5, 196, ''),
    ('SRK Silks & Sarees', 'JP Nagar, Bangalore', 12.910447, 77.590484, 'Clothing', 'silver', 4.2, 98, ''),
    ('Premium Optical Studio', 'JP Nagar, Bangalore', 12.910481, 77.590301, 'Health & Wellness', 'silver', 3.7, 424, ''),
    ('Kalpaney', 'JP Nagar, Bangalore', 12.906053, 77.589742, 'Food & Grocery', 'gold', 4.1, 1697, ''),
    ('Tuk Tuk Thai', 'JP Nagar, Bangalore', 12.906051, 77.582548, 'Food & Grocery', 'free', 3.5, 44, ''),
    ('Mayuri', 'JP Nagar, Bangalore', 12.912849, 77.585578, 'Food & Grocery', 'gold', 4.1, 1877, ''),
    ('Jewel Sizzle', 'JP Nagar, Bangalore', 12.901001, 77.585715, 'Food & Grocery', 'free', 3.5, 77, ''),
    ('Cherry Bean Coffee', 'JP Nagar, Bangalore', 12.906059, 77.594509, 'Food & Grocery', 'gold', 4.3, 1394, ''),
    ('IKOI', 'JP Nagar, Bangalore', 12.903619, 77.585992, 'Food & Grocery', 'bronze', 3.3, 30, ''),
    ('Trends Woman', 'BTM Layout, Bangalore', 12.913044, 77.609933, 'Clothing', 'bronze', 3.6, 103, ''),
    ('Amrutha Condiments', 'BTM Layout, Bangalore', 12.913948, 77.613629, 'Food & Grocery', 'bronze', 3.2, 32, ''),
    ('Sangama Bakery', 'BTM Layout, Bangalore', 12.914157, 77.613076, 'Food & Grocery', 'silver', 4.4, 278, ''),
    ('Sri Murti medicals', 'BTM Layout, Bangalore', 12.914173, 77.613495, 'Health & Wellness', 'silver', 4.0, 96, ''),
    ('Sri Ganesh Food Point', '29th Main, BTM Layout, Bangalore', 12.914963, 77.614991, 'Food & Grocery', 'free', 3.0, 50, ''),
    ('Cashify', 'BTM Layout, Bangalore', 12.913682, 77.607707, 'Electronics', 'free', 3.0, 60, ''),
    ('Mom & Kid', 'BTM Layout, Bangalore', 12.91361, 77.610203, 'Clothing', 'silver', 3.4, 254, ''),
    ('Punjabi Swag', 'BTM Layout, Bangalore', 12.913669, 77.60995, 'Food & Grocery', 'free', 3.1, 55, ''),
    ('Aravind Medicals and General stores', 'BTM Layout, Bangalore', 12.911594, 77.606282, 'Health & Wellness', 'silver', 3.5, 119, ''),
    ("Sri Devi Baker's", 'BTM Layout, Bangalore', 12.912521, 77.606423, 'Food & Grocery', 'bronze', 3.2, 121, ''),
    ('Homeneeds', '107, 7th Main Road, BTM Layout, Bangalore', 12.910655, 77.606309, 'Food & Grocery', 'silver', 3.6, 365, '+918033618025'),
    ('Juice Burg Cafe', 'BTM Layout, Bangalore', 12.916853, 77.603454, 'Food & Grocery', 'platinum', 4.5, 2485, ''),
    ('Tasty Restaurant', 'BTM Layout, Bangalore', 12.922771, 77.614208, 'Food & Grocery', 'free', 3.5, 41, ''),
    ('Food City Super Market', 'BTM Layout, Bangalore', 12.923215, 77.612864, 'Food & Grocery', 'silver', 4.0, 107, ''),
    ('Sweet Revenge Cafe', 'BTM Layout, Bangalore', 12.919825, 77.612782, 'Food & Grocery', 'silver', 3.8, 228, ''),
    ('Preethi Bakery & Sweets', 'BTM Layout, Bangalore', 12.916783, 77.618493, 'Food & Grocery', 'free', 3.0, 3, ''),
    ('Kataria Home Food', 'BTM Layout, Bangalore', 12.916425, 77.616411, 'Food & Grocery', 'silver', 3.6, 229, ''),
    ('Priya Bakery', 'BTM Layout, Bangalore', 12.916202, 77.615331, 'Food & Grocery', 'silver', 4.4, 276, ''),
    ('Aswini Hotel', 'Chocolate Factory Road, BTM Layout, Bangalore', 12.922904, 77.613461, 'Food & Grocery', 'silver', 3.8, 468, ''),
    ('Andra Curry Point', 'Chocolate Factory Road, BTM Layout, Bangalore', 12.92317, 77.612641, 'Food & Grocery', 'free', 3.4, 19, ''),
    ('Bake O Fresh Bakery', 'BTM Layout, Bangalore', 12.923468, 77.611734, 'Food & Grocery', 'silver', 4.3, 107, ''),
    ('Ashirwad Super Market', '284, 7th Main Road, BTM Layout, Bangalore', 12.915182, 77.605959, 'Food & Grocery', 'silver', 3.6, 367, '+91(91)8065333340'),
    ('Swadista Ahara', 'BTM Layout, Bangalore', 12.913916, 77.610211, 'Food & Grocery', 'gold', 4.6, 1779, ''),
    ('Pongal Veg Cafe', 'BTM Layout, Bangalore', 12.920673, 77.612359, 'Food & Grocery', 'free', 3.0, 14, ''),
    ('New Surya Sweets', 'Maruthi Nagar Main Road, BTM Layout, Bangalore', 12.925029, 77.614362, 'Food & Grocery', 'bronze', 3.5, 104, ''),
    ('Udupi Krishna Vihar', '2nd Main, BTM Layout, Bangalore', 12.911778, 77.600649, 'Food & Grocery', 'free', 3.2, 26, ''),
    ('Holy Smoke', 'BTM Layout, Bangalore', 12.910346, 77.609799, 'Food & Grocery', 'bronze', 3.8, 160, ''),
    ("Reddy's Pharma", 'BTM Layout, Bangalore', 12.907952, 77.606231, 'Health & Wellness', 'bronze', 3.7, 56, ''),
    ('Prerana Motors', 'BTM Layout, Bangalore', 12.916687, 77.614051, 'Automotive', 'silver', 4.1, 242, ''),
    ('North Indian Restaurant', '12, 17th Cross, 13th Main Road, 2nd Stage, BTM, BTM Layout, Bangalore', 12.907705, 77.608036, 'Food & Grocery', 'silver', 4.1, 72, ''),
    ('Paratha Junction', '1st Main Road, Opp Vijaya Bank, Bannerghatta Road, Bangalore, BTM Layout, Bangalore', 12.915399, 77.601035, 'Food & Grocery', 'free', 2.8, 25, ''),
    ('My Barber', '458, 7th Cross Road, BTM Layout, Bangalore', 12.913367, 77.604114, 'Beauty & Personal Care', 'silver', 4.0, 77, ''),
    ('Anand Diagnostic Laboratory', '94/1, 16th Main Road, BTM Layout, IInd Stage, Near Udupi Garden Hotel, BTM 2nd Stage, Kuvempu Nagar, Stage 2, BTM Layout, BTM Layout, Bangalore', 12.916282, 77.61301, 'Health & Wellness', 'bronze', 3.9, 37, '+918026683415'),
    ('Rohit Sports', 'Maruthi Nagar Main Road, BTM Layout, Bangalore', 12.922061, 77.613897, 'Sports', 'bronze', 3.3, 188, '+919632829008'),
    ('M.N.C SPORTS', '28/1, 4th Main Road, N.S Palya, BTM Layout, Bangalore', 12.910288, 77.603994, 'Sports', 'bronze', 3.1, 94, '+919591696131'),
    ('R R International Bikes', '69/1, HOSUR MAIN ROAD, BTM Layout, Bangalore', 12.922002, 77.619397, 'Sports', 'gold', 4.3, 573, '+918041280307'),
    ('The Sport Mall', '10/2, 1st Floor, 24th Main Road, Sarakki Village, J.P Nagar 6th Phase, Opp. Crystal Castle Hotel, BTM Layout, Bangalore', 12.918921, 77.616516, 'Sports', 'platinum', 4.5, 1315, '+918041713131'),
    ('Om sports', '726, 7th Cross Road, BTM Layout, Bangalore', 12.913652, 77.607649, 'Sports', 'gold', 4.2, 1322, '+919980383846'),
    ('Ramdev Sports & Stationary', '46, 7th Main Road, BTM Layout, Bangalore', 12.910956, 77.606561, 'Sports', 'bronze', 3.2, 148, '+919620656041'),
    ('B T M Sports And Stationery', '#429, 7th Main Road, BTM Layout, Bangalore', 12.913302, 77.606476, 'Sports', 'platinum', 4.6, 676, '+919972419321'),
    ('Ramdev Sports', '#45, 7th Main Road, N S Palya, BTM Layout 2nd Stage,, BTM Layout, Bangalore', 12.909048, 77.606239, 'Sports', 'free', 3.6, 39, '+917090955586'),
    ('Allday Supermarket', '152, btm layout, BTM Layout, Bangalore', 12.91935, 77.615854, 'Food & Grocery', 'bronze', 3.4, 60, '+91(91)8042332422'),
    ('Pick N Save The Wholesale Market', '15/2-1, Hosur Road, BTM Layout, Bangalore', 12.922887, 77.617492, 'Food & Grocery', 'free', 3.4, 45, '+91(91)9738439315'),
    ('Revive Fitness And Beauty Pvt Ltd', 'No.352 And 353,1st Floor, 6th Cross,29th Main, Btm Layout 2nd Stage, BTM Layout, Bangalore', 12.913836, 77.611496, 'Beauty & Personal Care', 'silver', 3.6, 306, '+919663810893'),
    ('Continental Tyre Park', 'BTM Layout, Bangalore', 12.913168, 77.606117, 'Automotive', 'free', 2.8, 32, ''),
    ('Mayur Andra Style Restaraunt', 'BTM Layout, Bangalore', 12.920951, 77.620137, 'Food & Grocery', 'gold', 4.6, 1768, ''),
    ('Couchpotato', 'BTM Layout, Bangalore', 12.916283, 77.613264, 'Food & Grocery', 'platinum', 4.9, 2795, ''),
    ('Sudharsan Medicals', 'BTM Layout, Bangalore', 12.916216, 77.615416, 'Health & Wellness', 'free', 3.0, 50, ''),
    ('Disney Bakery', 'BTM Layout, Bangalore', 12.913616, 77.607218, 'Food & Grocery', 'silver', 3.5, 245, ''),
    ('Nandi Pharma', 'BTM Layout, Bangalore', 12.913836, 77.607426, 'Health & Wellness', 'bronze', 3.7, 180, ''),
    ('Dhansagar', 'BTM Layout, Bangalore', 12.91354, 77.606539, 'Food & Grocery', 'silver', 3.8, 343, ''),
    ('Shri Shirdi Sai Stationery', 'BTM Layout, Bangalore', 12.913591, 77.606773, 'Stationery', 'free', 3.7, 77, ''),
    ('Poorvika Mobiles', 'BTM Layout, Bangalore', 12.913742, 77.606353, 'Electronics', 'bronze', 3.5, 72, ''),
    ('Pathan Sir', 'BTM Layout, Bangalore', 12.914043, 77.610199, 'Food & Grocery', 'silver', 4.0, 400, ''),
    ('99 Varieties Dosa', 'BTM Layout, Bangalore', 12.914082, 77.610592, 'Food & Grocery', 'gold', 4.6, 732, ''),
    ('Dry Fruit Gallery', 'BTM Layout, Bangalore', 12.913753, 77.608902, 'Food & Grocery', 'silver', 4.3, 119, ''),
    ('Cell Kafe', 'BTM Layout, Bangalore', 12.913766, 77.608843, 'Electronics', 'bronze', 3.9, 57, ''),
    ('Sri Krishna Sagar', 'BTM Layout, Bangalore', 12.913893, 77.606096, 'Food & Grocery', 'silver', 4.0, 130, ''),
    ('Om Medicals', 'BTM Layout, Bangalore', 12.913661, 77.607614, 'Health & Wellness', 'bronze', 3.6, 50, ''),
    ('Star Mobiles', 'BTM Layout, Bangalore', 12.913647, 77.607362, 'Electronics', 'free', 3.5, 18, ''),
    ('Herbal Ambience', 'BTM Layout, Bangalore', 12.913813, 77.606763, 'Beauty & Personal Care', 'silver', 4.2, 500, ''),
    ('Laddoos', 'BTM Layout, Bangalore', 12.913658, 77.610198, 'Food & Grocery', 'platinum', 5.0, 2985, ''),
    ('Asus', 'BTM Layout, Bangalore', 12.913633, 77.60995, 'Electronics', 'bronze', 3.9, 183, ''),
    ('Wink Unisex Salon', 'BTM Layout, Bangalore', 12.913483, 77.609883, 'Beauty & Personal Care', 'gold', 4.0, 1695, ''),
    ('Eat Again', '246, 7th Cross Road, BTM Layout, Bangalore', 12.913646, 77.604994, 'Food & Grocery', 'free', 2.8, 3, ''),
    ('Preethi General Stores', 'BTM Layout, Bangalore', 12.915424, 77.609941, 'Food & Grocery', 'silver', 4.0, 186, ''),
    ('Acer mall', 'BTM Layout, Bangalore', 12.913793, 77.60955, 'Electronics', 'silver', 4.0, 355, ''),
    ('TestApp', 'BTM Layout, Bangalore', 12.925489, 77.605096, 'Electronics', 'free', 3.4, 45, ''),
    ('Taco Bell', 'BTM Layout, Bangalore', 12.914174, 77.599589, 'Food & Grocery', 'free', 3.5, 17, ''),
    ('FB Cakes', 'BTM Layout, Bangalore', 12.916441, 77.606511, 'Food & Grocery', 'bronze', 3.6, 186, ''),
    ('Krishna Vaibhava', 'BTM Layout, Bangalore', 12.916427, 77.605636, 'Food & Grocery', 'bronze', 3.9, 145, ''),
    ('SLV Juice & Chats', 'BTM Layout, Bangalore', 12.916505, 77.602323, 'Food & Grocery', 'bronze', 3.2, 77, ''),
    ("Neena's Steakhouse and Burgers", 'BTM Layout, Bangalore', 12.916505, 77.602082, 'Food & Grocery', 'free', 3.5, 1, ''),
    ('Brahmalingeshwara Juice & Condiments', 'BTM Layout, Bangalore', 12.914475, 77.612174, 'Food & Grocery', 'gold', 4.7, 555, ''),
    ('Bamul Nandini', 'BTM Layout, Bangalore', 12.914543, 77.612372, 'Food & Grocery', 'silver', 4.4, 437, ''),
    ('Swaraj Supermarket', 'BTM Layout, Bangalore', 12.914298, 77.612691, 'Food & Grocery', 'free', 2.9, 56, ''),
    ('Glamvie Unisex Salon & Spa', 'BTM Layout, Bangalore', 12.913619, 77.607277, 'Beauty & Personal Care', 'gold', 4.2, 765, ''),
    ('Damensch', 'BTM Layout, Bangalore', 12.913736, 77.605835, 'Clothing', 'gold', 4.6, 975, ''),
    ('The Badminton Shop', 'BTM Layout, Bangalore', 12.913534, 77.606104, 'Sports', 'bronze', 3.0, 117, ''),
    ('Roti Wala', 'BTM Layout, Bangalore', 12.913607, 77.604006, 'Food & Grocery', 'silver', 3.8, 430, ''),
    ("Traits Men's Salon", 'BTM Layout, Bangalore', 12.913347, 77.606165, 'Beauty & Personal Care', 'silver', 3.9, 378, ''),
    ('Traits Salon', 'BTM Layout, Bangalore', 12.913383, 77.606163, 'Beauty & Personal Care', 'gold', 4.5, 1166, ''),
    ('Reborn Beauty', 'BTM Layout, Bangalore', 12.913132, 77.606105, 'Beauty & Personal Care', 'free', 3.2, 5, ''),
    ('Sangeetha Mobiles', 'BTM Layout, Bangalore', 12.913897, 77.607956, 'Electronics', 'gold', 3.9, 1201, ''),
    ('Durga Condiments', 'BTM Layout, Bangalore', 12.913824, 77.609354, 'Food & Grocery', 'bronze', 3.7, 162, ''),
    ('Creative Scissors', 'BTM Layout, Bangalore', 12.913874, 77.610207, 'Beauty & Personal Care', 'bronze', 3.8, 32, ''),
    ('Juice & Sandwich Center', 'BTM Layout, Bangalore', 12.912839, 77.610165, 'Food & Grocery', 'free', 3.1, 38, ''),
    ('Lenovo', 'BTM Layout, Bangalore', 12.9137, 77.608255, 'Electronics', 'silver', 3.7, 135, ''),
    ('Kullad Cafe', 'BTM Layout, Bangalore', 12.912643, 77.609917, 'Food & Grocery', 'bronze', 3.6, 33, ''),
    ('Pai Mobiles', 'BTM Layout, Bangalore', 12.913803, 77.606847, 'Electronics', 'silver', 4.3, 287, ''),
    ('Laptop Splutions', 'BTM Layout, Bangalore', 12.91382, 77.609652, 'Electronics', 'gold', 4.2, 1579, ''),
    ('Olive Street Food Cafe', 'BTM Layout, Bangalore', 12.913932, 77.610439, 'Food & Grocery', 'bronze', 3.2, 169, ''),
    ('Puneri Katta', '11th Main Road, BTM Layout, Bangalore', 12.913421, 77.608345, 'Food & Grocery', 'gold', 4.4, 377, ''),
    ('Sri Eye Vision Care', 'BTM Layout, Bangalore', 12.916207, 77.615354, 'Health & Wellness', 'bronze', 3.3, 26, ''),
    ('Ni O Ven', 'BTM Layout, Bangalore', 12.916273, 77.612705, 'Food & Grocery', 'free', 3.3, 25, ''),
    ('Star Biryani', 'BTM Layout, Bangalore', 12.916316, 77.611342, 'Food & Grocery', 'gold', 3.8, 763, ''),
    ('Laptop Zone', 'BTM Layout, Bangalore', 12.916284, 77.614672, 'Electronics', 'silver', 4.3, 457, ''),
    ('Buddies Food Corner', 'BTM Layout, Bangalore', 12.916218, 77.615319, 'Food & Grocery', 'free', 3.4, 11, ''),
    ('Paperkraft', 'BTM Layout, Bangalore', 12.916251, 77.615069, 'Stationery', 'free', 3.1, 46, ''),
    ('Shri Vishnu Park Veg', 'BTM Layout, Bangalore', 12.916699, 77.615087, 'Food & Grocery', 'free', 3.4, 65, ''),
    ('Muthanna Military Hotel', 'BTM Layout, Bangalore', 12.916413, 77.616617, 'Food & Grocery', 'silver', 3.6, 331, ''),
    ('Shristi Silks', '947, 16th Main Road, BTM Layout, Bangalore', 12.914224, 77.610206, 'Clothing', 'bronze', 3.8, 105, ''),
    ('whami', '731, 7th Cross Road, BTM Layout, Bangalore', 12.913682, 77.607925, 'Electronics', 'silver', 4.1, 126, ''),
    ('Preethi Bakery', '787, 16th Main Road, BTM Layout, Bangalore', 12.915523, 77.609955, 'Food & Grocery', 'free', 2.8, 6, ''),
    ('Turning Heads', '945, 16th Main Road, BTM Layout, Bangalore', 12.914436, 77.610218, 'Beauty & Personal Care', 'bronze', 3.3, 134, ''),
    ('Tulasi', '943, 16th Main Road, BTM Layout, Bangalore', 12.914656, 77.610235, 'Clothing', 'free', 2.9, 16, ''),
    ('Titan Eye+', '944, 16th Main Road, BTM Layout, Bangalore', 12.914542, 77.610219, 'Health & Wellness', 'gold', 4.7, 107, ''),
    ('Hair Speak Family Salon', '780, 16th Main Road, BTM Layout, Bangalore', 12.914741, 77.609901, 'Beauty & Personal Care', 'silver', 3.7, 284, ''),
    ('IRITZ', '944, 16th Main Road, BTM Layout, Bangalore', 12.914543, 77.610268, 'Beauty & Personal Care', 'bronze', 3.5, 21, ''),
    ('Lili Ladies Beauty Parlour', 'BTM Layout, Bangalore', 12.910378, 77.609907, 'Beauty & Personal Care', 'silver', 3.9, 161, ''),
    ('Sri Annapurneswari Restaurant', 'BTM Layout, Bangalore', 12.912169, 77.606458, 'Food & Grocery', 'silver', 3.9, 259, ''),
    ('Sri Amulya Medicals', 'BTM Layout, Bangalore', 12.916524, 77.60323, 'Health & Wellness', 'bronze', 3.6, 21, ''),
    ('Salt Lake', 'BTM Layout, Bangalore', 12.906, 77.612656, 'Food & Grocery', 'free', 3.2, 79, ''),
    ('Athenian Salon and Academy', 'BTM Layout, Bangalore', 12.911276, 77.60989, 'Beauty & Personal Care', 'silver', 4.2, 156, ''),
    ('Frisson Family Mart', 'BTM Layout, Bangalore', 12.912918, 77.610164, 'Food & Grocery', 'free', 2.8, 44, ''),
    ('Absolute Barbecues', 'BTM Layout, Bangalore', 12.916419, 77.604738, 'Food & Grocery', 'free', 3.6, 73, ''),
    ("Muthashy's Restaurant", 'BTM Layout, Bangalore', 12.920769, 77.613218, 'Food & Grocery', 'gold', 4.0, 1984, ''),
    ('Nahdi Mandi', 'BTM Layout, Bangalore', 12.920774, 77.620164, 'Food & Grocery', 'free', 2.8, 22, ''),
    ('Ninan Steak house and Burgers', 'BTM Layout, Bangalore', 12.916484, 77.602273, 'Food & Grocery', 'free', 3.0, 76, ''),
    ('IBO', 'BTM Layout, Bangalore', 12.914543, 77.599578, 'Hardware', 'silver', 4.0, 283, ''),
    ('Lavish Locks', '7th Cross Road, BTM Layout, Bangalore', 12.913828, 77.606864, 'Beauty & Personal Care', 'silver', 3.8, 68, ''),
    ('Sutta Brake', '20th Main Road, BTM Layout, Bangalore', 12.916042, 77.612294, 'Food & Grocery', 'free', 3.0, 47, ''),
    ('Gyani Da Punjabi Dhaba', 'BTM Layout, Bangalore', 12.916186, 77.612279, 'Food & Grocery', 'gold', 4.5, 1653, ''),
    ('Cremeux', '7th Cross Road, BTM Layout, Bangalore', 12.91358, 77.604365, 'Food & Grocery', 'bronze', 3.6, 94, ''),
    ('The Koi', '7th Cross Road, BTM Layout, Bangalore', 12.913724, 77.605189, 'Food & Grocery', 'bronze', 3.1, 90, ''),
    ('Chill N Waffle', 'Tank Bund Road, BTM Layout, Bangalore', 12.911395, 77.615695, 'Food & Grocery', 'bronze', 3.4, 73, ''),
    ('Corn Insta', 'Tank Bund Road, BTM Layout, Bangalore', 12.911217, 77.615644, 'Food & Grocery', 'silver', 4.0, 105, ''),
    ('Irani Chai Biscuit', 'Tank Bund Road, BTM Layout, Bangalore', 12.911404, 77.615646, 'Food & Grocery', 'silver', 4.4, 320, ''),
    ('The Green Fruits', 'Tavarekere Main Road, BTM Layout, Bangalore', 12.92668, 77.608894, 'Food & Grocery', 'platinum', 4.5, 2852, ''),
    ('KPN Fresh', '16th Main Road, BTM Layout, Bangalore', 12.918806, 77.610441, 'Food & Grocery', 'bronze', 3.8, 86, ''),
    ('happen blr', 'BTM Layout, Bangalore', 12.911975, 77.609868, 'Food & Grocery', 'bronze', 4.0, 110, ''),
    ('The Anjanadri Cafe', 'BTM Layout, Bangalore', 12.919674, 77.61108, 'Food & Grocery', 'gold', 4.3, 1116, ''),
    ('Thata Tea', 'BTM Layout, Bangalore', 12.919667, 77.611146, 'Food & Grocery', 'bronze', 3.6, 146, ''),
    ("Shiva's Mess", 'BTM Layout, Bangalore', 12.919662, 77.611321, 'Food & Grocery', 'gold', 4.6, 576, ''),
    ("Palace Men's Parlour", 'BTM Layout, Bangalore', 12.919663, 77.61143, 'Beauty & Personal Care', 'silver', 4.4, 50, ''),
    ('Sri Gajana Fruit Juice Center', 'BTM Layout, Bangalore', 12.919654, 77.611483, 'Food & Grocery', 'free', 3.2, 15, ''),
    ('Ambur Hot Sum Biryani', 'BTM Layout, Bangalore', 12.919652, 77.611564, 'Food & Grocery', 'silver', 3.6, 365, ''),
    ('Enhance Salon', 'BTM Layout, Bangalore', 12.91962, 77.61226, 'Beauty & Personal Care', 'platinum', 4.4, 3234, ''),
    ('Udupi Sagar', 'BTM Layout, Bangalore', 12.919602, 77.613059, 'Food & Grocery', 'bronze', 3.8, 74, ''),
    ("Sanju's Kitchen", 'BTM Layout, Bangalore', 12.919519, 77.614283, 'Food & Grocery', 'free', 2.9, 36, '+918660567052'),
    ('Smart Cart Mobiles', 'BTM Layout, Bangalore', 12.9181, 77.614277, 'Electronics', 'free', 2.8, 38, ''),
    ('JK Lasso And Juice Center', 'BTM Layout, Bangalore', 12.91802, 77.614329, 'Food & Grocery', 'bronze', 3.3, 21, ''),
    ('Renuka Medicines And General stores', 'BTM Layout, Bangalore', 12.917877, 77.614322, 'Health & Wellness', 'free', 3.0, 50, '+919945024244'),
    ('Instakart', 'BTM Layout, Bangalore', 12.917196, 77.614413, 'Food & Grocery', 'silver', 4.0, 64, ''),
    ('VMart Supermarket', '256/1-4, 7th Main Road, BTM Layout, Bangalore', 12.907263, 77.606098, 'Food & Grocery', 'gold', 3.9, 1807, ''),
    ('J&J Eye Care Optical', 'BTM Layout, Bangalore', 12.916356, 77.610233, 'Health & Wellness', 'bronze', 3.7, 141, ''),
    ('Shree Ganesha Fruit Juice Shop', 'BTM Layout, Bangalore', 12.911832, 77.606463, 'Food & Grocery', 'silver', 4.0, 296, ''),
    ('Castle Multicuisine Restaurant', 'BTM Layout, Bangalore', 12.92272, 77.614432, 'Food & Grocery', 'free', 3.2, 68, ''),
    ('Snack Corner', 'Bannerghatta Road, Bannerghatta Road, Bangalore', 12.89846, 77.601069, 'Food & Grocery', 'gold', 4.2, 1767, ''),
    ('Athicas Canteen', 'Bannerghatta Road, Bangalore', 12.894213, 77.602568, 'Food & Grocery', 'bronze', 3.7, 153, ''),
    ('Chai-Unchai', 'Bannerghatta Road, Bangalore', 12.894436, 77.602447, 'Food & Grocery', 'free', 3.4, 40, ''),
    ('Aishwarya Parkland', 'Bannerghatta Road, Bangalore', 12.884617, 77.596194, 'Food & Grocery', 'silver', 3.4, 345, ''),
    ('Benison', 'Bannerghatta Road, Bannerghatta Road, Bangalore', 12.883658, 77.595996, 'Food & Grocery', 'silver', 4.2, 457, ''),
    ('Ocean Fresh Fish shop', 'Bannerghatta Road, Bangalore', 12.886861, 77.592975, 'Food & Grocery', 'bronze', 4.0, 91, ''),
    ('Iyengars Tyre care', 'Bannerghatta Road, Bangalore', 12.893434, 77.586476, 'Automotive', 'gold', 4.0, 868, ''),
    ('Guru Garden Restaurant', 'Bannerghatta Road, Bangalore', 12.89119, 77.598133, 'Food & Grocery', 'silver', 4.2, 342, ''),
    ('Ammis Biriyani', 'Bannerghatta Road, Bangalore', 12.892425, 77.598595, 'Food & Grocery', 'silver', 3.6, 109, ''),
    ('Bejing Bites', 'Bannerghatta Road, Bangalore', 12.892283, 77.598238, 'Food & Grocery', 'gold', 4.5, 1544, ''),
    ('Samsung Smart Plaza', 'Bannerghatta Road, Bangalore', 12.892385, 77.597904, 'Electronics', 'gold', 4.6, 1553, ''),
    ('Trinity Eye Vision', 'Bannerghatta Road, Bangalore', 12.892303, 77.598182, 'Health & Wellness', 'free', 3.1, 63, ''),
    ('9 to 9 Sight Zone Optics', 'Bannerghatta Road, Bangalore', 12.892038, 77.598458, 'Health & Wellness', 'gold', 4.1, 1798, ''),
    ('Athithi Andhra Style Restaurant', 'Bannerghatta Road, Bangalore', 12.888582, 77.597049, 'Food & Grocery', 'platinum', 4.2, 2976, ''),
    ('Bengaliana', 'Bannerghatta Road, Bangalore', 12.8897, 77.59749, 'Food & Grocery', 'free', 3.1, 4, ''),
    ("Dadi's Dum Biryani", '7, 2nd Floor, Krish Towers, Arekere, Bannerghatta Road, Bannerghatta Road, Bangalore', 12.889716, 77.597503, 'Food & Grocery', 'silver', 4.3, 277, '+917090377777'),
    ('Mahalakshmi Tyres', 'Bannerghatta Road, Bangalore', 12.890185, 77.59808, 'Automotive', 'bronze', 3.8, 148, ''),
    ('Trust', 'Bannerghatta Road, Bangalore', 12.889666, 77.597479, 'Health & Wellness', 'free', 3.0, 61, ''),
    ('UniverCell', 'Bannerghatta Road, Bangalore', 12.88953, 77.597448, 'Electronics', 'silver', 3.9, 447, ''),
    ('Zanghs Dynasty', 'Bannerghatta Road, Bangalore', 12.890143, 77.597694, 'Food & Grocery', 'bronze', 3.8, 45, ''),
    ('Sufiyan', 'Bannerghatta Road, Bangalore', 12.89226, 77.598361, 'Food & Grocery', 'gold', 4.6, 1485, ''),
    ('Hot Burg', 'Bannerghatta Road, Bangalore', 12.892252, 77.59834, 'Food & Grocery', 'platinum', 4.2, 799, ''),
    ('Yaksheswar Bakery', 'Bannerghatta Road, Bangalore', 12.892056, 77.598377, 'Food & Grocery', 'bronze', 3.7, 153, ''),
    ('Anant Cars', 'Bannerghatta Road, Bangalore', 12.89834, 77.600146, 'Automotive', 'free', 3.0, 5, ''),
    ('Kalyani Motors', 'Bannerghatta Road, Bangalore', 12.902341, 77.601438, 'Automotive', 'silver', 4.0, 94, ''),
    ('Xiaomi MI Service Center', 'Bannerghatta Road, Bannerghatta Road, Bangalore', 12.896924, 77.599785, 'Electronics', 'bronze', 3.9, 46, ''),
    ('Vishal Mega Mart', '448, Bannerghatta Road, Bannerghatta Road, Bangalore', 12.897494, 77.599851, 'Food & Grocery', 'bronze', 3.7, 99, ''),
    ('South India Shopping Mall', '1402, Bilekahalli Village, Bannerghatta Road, Bangalore', 12.890867, 77.597713, 'Food & Grocery', 'free', 3.5, 26, ''),
    ('Reliance kumar sports', '193/5, Bannerghatta Road, Bannerghatta Road, Bangalore', 12.890946, 77.595561, 'Sports', 'bronze', 3.2, 90, '+919901052012'),
    ('Hakuna Matata', '1231/35/2, 24th Main,, Opposite Brigade Palm Springs, Brigade Millenium Rd, JP Nagar 7th Phase,, Bannerghatta Road, Bangalore', 12.894081, 77.586617, 'Food & Grocery', 'bronze', 3.8, 191, '+919741133866'),
    ('Elite Mess', 'Bannerghatta Road, Bangalore', 12.899229, 77.602648, 'Food & Grocery', 'free', 2.8, 35, ''),
    ('Fattoush', 'Bannerghatta Road, Bangalore', 12.893157, 77.598748, 'Food & Grocery', 'free', 3.7, 71, ''),
    ("Prabhakar's", 'IIMB, Bannerghatta Road, Bangalore', 12.893926, 77.601161, 'Food & Grocery', 'bronze', 3.2, 109, ''),
    ('Yellow Submarine', 'Bannerghatta Road, Bangalore', 12.897605, 77.5997, 'Food & Grocery', 'bronze', 3.3, 200, ''),
    ('Fresh n More', 'Bannerghatta Road, Bangalore', 12.897108, 77.596413, 'Food & Grocery', 'gold', 4.3, 450, ''),
    ('Eber Fresh Greens', 'Bannerghatta Road, Bangalore', 12.897228, 77.596217, 'Food & Grocery', 'free', 3.3, 43, ''),
    ('Zak Cafe', 'Bannerghatta Road, Bangalore', 12.89892, 77.588839, 'Food & Grocery', 'bronze', 3.1, 27, ''),
    ('Brahma Brews', '24th Main Road, Bannerghatta Road, Bangalore', 12.893235, 77.586472, 'Food & Grocery', 'bronze', 3.7, 47, ''),
    ('Sri Laksmi Upahara', '31, 4th Cross, Bannerghatta Road, Bangalore', 12.891759, 77.605994, 'Food & Grocery', 'bronze', 3.1, 120, '+918197750435'),
    ('Bangalore Bakery And Sweets', '268, 8th Cross Road, Bannerghatta Road, Bangalore', 12.891748, 77.606423, 'Food & Grocery', 'free', 3.3, 75, ''),
    ('Brahmin Tiffins and Coffee', 'Bannerghatta Road, Bangalore', 12.891804, 77.606443, 'Food & Grocery', 'silver', 4.1, 278, '+917975356028'),
    ('Ashraya Mediacals', '513, Bannerghatta Road, Bangalore', 12.891181, 77.606483, 'Health & Wellness', 'silver', 3.7, 468, ''),
    ("Green's Fresh Supermarket", '7, Arekere Main Road, Bannerghatta Road, Bangalore', 12.888943, 77.60276, 'Food & Grocery', 'free', 3.5, 68, ''),
    ('Toy Station', 'Arekere Main Road, Bannerghatta Road, Bangalore', 12.8889, 77.602832, 'Toys & Baby', 'free', 3.6, 59, ''),
    ('Soya Chaap Restaurant', 'Bannerghatta Road, Bangalore', 12.884675, 77.59126, 'Food & Grocery', 'silver', 3.5, 460, ''),
    ('High Brew', 'Bannerghatta Road, Bangalore', 12.899581, 77.594435, 'Food & Grocery', 'gold', 4.2, 1033, ''),
    ("Kritunga The Palegar's Cuisine", 'Bannerghatta Road, Bangalore', 12.892238, 77.598535, 'Food & Grocery', 'bronze', 3.6, 139, ''),
    ('Bonheur Cafe', 'Bannerghatta Road, Bangalore', 12.902908, 77.594743, 'Food & Grocery', 'bronze', 3.9, 56, ''),
    ('Gangothri Restaurant', '80 Feet Road, Koramangala, Bangalore', 12.937765, 77.626755, 'Food & Grocery', 'gold', 4.3, 1969, ''),
    ('Barista', 'Koramangala, Bangalore', 12.934944, 77.62935, 'Food & Grocery', 'silver', 3.8, 143, ''),
    ('Pai Electronics', 'Koramangala, Bangalore', 12.933782, 77.623257, 'Electronics', 'silver', 4.1, 453, ''),
    ('Kota Kachori', 'Koramangala, Bangalore', 12.936231, 77.625431, 'Food & Grocery', 'gold', 4.0, 1098, ''),
    ('Coast to Coast Sea food restaurant', 'Koramangala, Bangalore', 12.937042, 77.624591, 'Food & Grocery', 'free', 3.2, 44, ''),
    ('Tamrind Restaurant', 'Koramangala, Bangalore', 12.934247, 77.623472, 'Food & Grocery', 'bronze', 3.8, 92, ''),
    ('Kubey Seafood Restaurant', 'Koramangala, Bangalore', 12.9371, 77.624542, 'Food & Grocery', 'silver', 4.0, 115, ''),
    ('Lazeej', 'Koramangala, Bangalore', 12.93382, 77.619366, 'Food & Grocery', 'bronze', 3.4, 47, ''),
    ('Golmal Paratha', 'Koramangala, Bangalore', 12.93383, 77.619466, 'Food & Grocery', 'bronze', 3.6, 97, ''),
    ('Ping', 'Koramangala, Bangalore', 12.933633, 77.622557, 'Food & Grocery', 'bronze', 3.4, 125, ''),
    ('Tunday Kabab', 'Koramangala, Bangalore', 12.933823, 77.619302, 'Food & Grocery', 'free', 2.9, 20, ''),
    ('KC Das', 'Koramangala, Bangalore', 12.933797, 77.618997, 'Food & Grocery', 'platinum', 4.4, 3291, ''),
    ('Tandoor Hut', 'Koramangala, Bangalore', 12.933886, 77.619417, 'Food & Grocery', 'free', 3.4, 55, ''),
    ('China Pearl', 'Koramangala, Bangalore', 12.936198, 77.621467, 'Food & Grocery', 'silver', 3.8, 453, ''),
    ('Madurai Idly shop', 'Koramangala, Bangalore', 12.937202, 77.624452, 'Food & Grocery', 'bronze', 3.7, 64, ''),
    ('Maharaja Restaurant', 'Koramangala, Bangalore', 12.934295, 77.630079, 'Food & Grocery', 'silver', 3.5, 414, ''),
    ('Lifestyle', 'Near SonyWorld Signal, Koramangala, Bangalore, Koramangala, Bangalore', 12.937548, 77.627993, 'Clothing', 'gold', 4.3, 1096, ''),
    ('Fiorano Ristorante', 'Koramangala, Bangalore', 12.933141, 77.622851, 'Food & Grocery', 'free', 3.3, 41, ''),
    ('Nilgiris', 'Koramangala, Bangalore', 12.935716, 77.62182, 'Food & Grocery', 'silver', 4.0, 470, ''),
    ('Mystique Saloons', 'Koramangala, Bangalore, Koramangala, Bangalore', 12.935243, 77.624868, 'Beauty & Personal Care', 'bronze', 3.3, 123, ''),
    ("Levi's and Nike", 'Koramangala, Bangalore', 12.933744, 77.630184, 'Clothing', 'gold', 3.8, 322, ''),
    ('Sowbhagya Traders', 'Koramangala, Bangalore', 12.935203, 77.622895, 'Food & Grocery', 'silver', 4.0, 469, ''),
    ('Navya shree sarees', '17th C Main, Koramangala, Bangalore', 12.937138, 77.624139, 'Clothing', 'bronze', 3.5, 189, ''),
    ('Nandhana Grand', 'Koramangala, Bangalore', 12.936298, 77.621353, 'Food & Grocery', 'free', 2.9, 17, ''),
    ('fulki', '55, 6th block koramangala, Koramangala, Bangalore', 12.936638, 77.620652, 'Clothing', 'gold', 4.6, 1402, ''),
    ('U Fashion store', '100, 5th cross 5th block koramangala, Koramangala, Bangalore', 12.936544, 77.620577, 'Clothing', 'gold', 4.5, 1453, ''),
    ('Bhojohori manna', 'Koramangala, Bangalore', 12.937369, 77.624314, 'Food & Grocery', 'free', 3.2, 49, ''),
    ('Imperial Hotel', 'Koramangala, Bangalore', 12.936408, 77.62508, 'Food & Grocery', 'silver', 3.8, 155, ''),
    ('heritage of Bengal', 'Koramangala, Bangalore', 12.937323, 77.624368, 'Food & Grocery', 'silver', 3.7, 341, ''),
    ('Ocean restaurant', 'Koramangala, Bangalore', 12.936871, 77.62487, 'Food & Grocery', 'silver', 4.1, 272, ''),
    ('Naati oota', 'Koramangala, Koramangala, Bangalore', 12.936869, 77.624723, 'Food & Grocery', 'silver', 4.3, 95, ''),
    ('Swiss Embroideries', 'Koramangala, Bangalore', 12.935766, 77.621724, 'Clothing', 'silver', 4.0, 311, ''),
    ('Chinese Cottage', 'Koramangala, Bangalore', 12.936885, 77.624621, 'Food & Grocery', 'bronze', 3.1, 95, ''),
    ('HMS Mutton center', 'Koramangala, Bangalore', 12.936754, 77.624744, 'Food & Grocery', 'free', 3.6, 6, ''),
    ('Chalo Punjab', 'Koramangala, Bangalore', 12.937794, 77.626697, 'Food & Grocery', 'free', 3.3, 45, ''),
    ('Ambrosia restaurant', 'Koramangala, Bangalore', 12.936184, 77.625864, 'Food & Grocery', 'free', 3.0, 3, ''),
    ('OM Stationery and xerox', 'Koramangala, Bangalore', 12.936445, 77.625586, 'Food & Grocery', 'free', 3.4, 50, ''),
    ('Kwality Walls', 'Koramangala, Bangalore', 12.938058, 77.626144, 'Food & Grocery', 'silver', 4.1, 257, ''),
    ('Brown Tree Super', 'Koramangala, Bangalore', 12.937363, 77.626659, 'Food & Grocery', 'bronze', 3.5, 189, ''),
    ('Mahindra showroom', 'Koramangala, Bangalore', 12.937827, 77.626639, 'Automotive', 'free', 2.8, 71, ''),
    ('MAS', 'Hosur Road, Koramangala, Bangalore', 12.925875, 77.617061, 'Food & Grocery', 'bronze', 3.9, 126, ''),
    ('Gia Garments', 'Koramangala, Bangalore', 12.933818, 77.619244, 'Clothing', 'free', 3.7, 16, ''),
    ('Mama mia', 'Koramangala, Bangalore', 12.9338, 77.619424, 'Food & Grocery', 'free', 2.8, 2, ''),
    ('Oye Amritsar', 'Koramangala, Bangalore', 12.936475, 77.621053, 'Food & Grocery', 'free', 3.6, 55, ''),
    ('Auto Fusion', 'Koramangala, Bangalore', 12.936499, 77.624037, 'Electronics', 'platinum', 4.1, 1624, ''),
    ('New Sapna Store', 'Koramangala, Bangalore', 12.918475, 77.632155, 'Food & Grocery', 'silver', 3.8, 283, ''),
    ('Suma Provision Store', 'Koramangala, Bangalore', 12.918067, 77.630671, 'Food & Grocery', 'gold', 4.4, 971, ''),
    ('Vijaya Shree Diagnostics', 'Koramangala, Bangalore', 12.927108, 77.622609, 'Health & Wellness', 'silver', 3.9, 267, ''),
    ('Kanyaka Clothes', 'Koramangala, Bangalore', 12.92524, 77.633716, 'Clothing', 'silver', 3.9, 231, ''),
    ('Madiwala Market', 'Koramangala, Bangalore', 12.922615, 77.622572, 'Food & Grocery', 'free', 3.1, 0, ''),
    ('Himalaya Health care - druggists', 'Koramangala, Bangalore', 12.937753, 77.626648, 'Health & Wellness', 'silver', 4.2, 171, ''),
    ('Arathi medicals', 'Koramangala, Bangalore', 12.935395, 77.623343, 'Health & Wellness', 'silver', 3.7, 329, ''),
    ('Polynation', 'Koramangala, Bangalore', 12.937554, 77.628118, 'Food & Grocery', 'silver', 3.4, 69, ''),
    ('Roots Multiclean', 'Koramangala, Bangalore', 12.936734, 77.624985, 'Electronics', 'silver', 4.1, 107, ''),
    ('Home Lifestyle', 'Koramangala, Bangalore', 12.937392, 77.627994, 'Electronics', 'gold', 4.3, 1514, ''),
    ('Taste of Bihar', 'Koramangala, Bangalore', 12.937674, 77.626437, 'Food & Grocery', 'free', 3.4, 67, ''),
    ('Priya Cafe', 'Koramangala, Bangalore', 12.918326, 77.632128, 'Food & Grocery', 'platinum', 5.0, 2789, ''),
    ('Nandini Andra Style', 'Mahayogi Vemana Road, Koramangala, Bangalore', 12.931734, 77.622859, 'Food & Grocery', 'bronze', 3.1, 176, ''),
    ('Ohris', 'Hosur road, Koramangala, Bangalore', 12.922039, 77.620302, 'Food & Grocery', 'free', 3.2, 9, ''),
    ('Silver Metro', 'Hosur road, Koramangala, Bangalore', 12.921876, 77.620493, 'Food & Grocery', 'silver', 3.9, 346, ''),
    ('Sumith Provision & Condiments', 'Koramangala, Bangalore', 12.937283, 77.624514, 'Food & Grocery', 'bronze', 3.1, 107, ''),
    ('Olio', 'Koramangala, Bangalore', 12.936551, 77.62049, 'Food & Grocery', 'bronze', 3.0, 136, ''),
    ('Crystal Stationeries', 'Koramangala, Bangalore', 12.93746, 77.624271, 'Food & Grocery', 'bronze', 3.5, 104, ''),
    ('Adigas', 'Hosur road, Koramangala, Bangalore', 12.921875, 77.620121, 'Food & Grocery', 'bronze', 4.0, 95, ''),
    ('Utsav', 'Koramangala, Bangalore', 12.931197, 77.622854, 'Food & Grocery', 'free', 2.7, 76, ''),
    ('Great Indian Thali', 'Koramangala, Bangalore', 12.936516, 77.627412, 'Food & Grocery', 'silver', 4.1, 338, ''),
    ('Barbeque Nation', 'Koramangala, Bangalore', 12.925515, 77.637076, 'Food & Grocery', 'gold', 4.0, 1388, ''),
    ('Ajit Medicals', 'Koramangala, Bangalore', 12.929547, 77.629129, 'Health & Wellness', 'bronze', 3.2, 33, ''),
    ('Trustx', 'Koramangala, Bangalore', 12.935622, 77.631044, 'Health & Wellness', 'silver', 3.7, 427, ''),
    ('Pharma Plus', '924, 1st Cross Road, Koramangala, Bangalore', 12.935306, 77.630622, 'Health & Wellness', 'silver', 3.7, 92, ''),
    ('Supriya Stores', '1st Cross Road, Koramangala, Bangalore', 12.935504, 77.631457, 'Food & Grocery', 'free', 2.8, 35, ''),
    ('Food Mall', 'Koramangala, Bangalore', 12.933402, 77.629802, 'Food & Grocery', 'free', 3.3, 35, ''),
    ('Sagar Fast Food', 'Koramangala, Bangalore', 12.930592, 77.622972, 'Food & Grocery', 'gold', 3.7, 328, ''),
    ('Food Triangle', 'Koramangala, Bangalore', 12.930864, 77.623075, 'Food & Grocery', 'silver', 4.3, 428, ''),
    ('Ambur Biriyani', 'BDA Complex, Mahayogi Vemana Road, Koramangala, Bangalore', 12.930844, 77.622641, 'Food & Grocery', 'free', 3.1, 22, ''),
    ('Market Square Value Mall', 'Koramangala, Bangalore', 12.921904, 77.620311, 'Food & Grocery', 'free', 3.4, 62, ''),
    ('Black Pepper Lounge', '17th E Main, Koramangala, Bangalore', 12.93632, 77.622203, 'Food & Grocery', 'free', 3.2, 4, ''),
    ('Map Sales Office', 'Koramangala, Bangalore', 12.926373, 77.622252, 'Books', 'bronze', 3.1, 57, ''),
    ('Cross Words', 'Koramangala, Bangalore', 12.925313, 77.619276, 'Books', 'silver', 4.2, 478, ''),
    ('Food Basics', 'Koramangala, Bangalore', 12.931966, 77.617918, 'Food & Grocery', 'bronze', 4.0, 118, ''),
    ('Sanjeevanam', 'Koramangala, Bangalore', 12.925422, 77.619453, 'Food & Grocery', 'free', 3.1, 21, ''),
    ('Ichin Restaurant', 'Koramangala, Bangalore', 12.937762, 77.627112, 'Food & Grocery', 'free', 3.4, 44, ''),
    ("Mani's Dum Biriyani", 'Koramangala, Bangalore', 12.927491, 77.637371, 'Food & Grocery', 'free', 3.0, 14, '+918065321900'),
    ('Naiyanar Bakery', 'Koramangala, Bangalore', 12.927344, 77.638155, 'Food & Grocery', 'free', 3.2, 60, ''),
    ('Trust Chemists & Druggists Ltd', 'Hosur Rd, Zuzuvadi, Madiwala, 1st Stage, BTM Layout, Koramangala, Bangalore', 12.926338, 77.616857, 'Health & Wellness', 'bronze', 3.7, 156, '+918025630022'),
    ('Clumax Diagnostic Center', '1543, Ground Floor, 19th Main, Opposite to BMTC bus depot, Sector 1, HSR Layout, 1st Sector, HSR Layout,, Koramangala, Bangalore', 12.923887, 77.6196, 'Health & Wellness', 'free', 3.4, 49, '+918069407777'),
    ('Excel Diagnostics', '358/1-10, 7th Cross, Venkatapura Main Rd, 1st Block Koramangala, HSR Layout 5th Sector, Koramangala, Bangalore', 12.921796, 77.633717, 'Health & Wellness', 'bronze', 3.1, 131, '+918025500182'),
    ('Ruth Medicals', 'Koramangala BDA, 6th B Cross Road, Koramangala 3 Block, Koramangala, Koramangala, Bangalore', 12.931731, 77.623098, 'Health & Wellness', 'free', 3.6, 57, '+918025537424'),
    ('Maxwell Pharma', 'BDA Complex, Koramangala 4th Block, Koramangala, Koramangala, Bangalore', 12.934069, 77.625222, 'Health & Wellness', 'bronze', 3.3, 155, '+918025531177'),
    ('Dialogues Cafe', '41, 100 feet road, Koramangala 4th Block, Koramangala, Bangalore', 12.935349, 77.62496, 'Food & Grocery', 'silver', 3.4, 322, '+919811974842'),
    ('Ira Residency', '39/13,, 5th Cross, Near Ibblur Bus Stop, Outer Ring Rd, Agara Village, Sector 4, HSR Layou, Koramangala, Bangalore', 12.92338, 77.634128, 'Food & Grocery', 'silver', 4.4, 271, '+918025727656'),
    ('Proline Fitness', '99, 5th Cross Road, Koramangala, Bangalore', 12.936407, 77.621398, 'Sports', 'silver', 4.1, 189, '+918041643592'),
    ('Naturals Unisex Salon And Spa', 'No.24, Madiwala Sarjapura Road, Koramangala, Bangalore', 12.921752, 77.62056, 'Beauty & Personal Care', 'bronze', 3.3, 45, '+918033100461'),
    ('Charisma Spa Pvt Ltd', 'No.107,2nd Floor,Art Arcade, 80 Feet Road;7th Main Road, Koramangala, Bangalore', 12.935904, 77.628437, 'Beauty & Personal Care', 'free', 3.3, 59, '+919742009417'),
    ('The Craftery by Subko, BLR', '374, Sarjapura Road, Koramangala, Bangalore', 12.926081, 77.625409, 'Food & Grocery', 'silver', 4.1, 425, ''),
    ('Essence Unisex Salon And Spa', '305, 1st Main Rd, Koramangala 1st Block, Jakkasandra extension, HSR Layout, 1st Block Koramangala,, Koramangala, Bangalore', 12.928083, 77.63731, 'Beauty & Personal Care', 'silver', 4.2, 316, '+918040953697'),
    ('Renaissance Unisex Salon & Spa', '#638, 2nd Floor, 12th Main Road, Koramangala 4 Block,, Koramangala, Koramangala 4th Block, Koramangala, Bangalore', 12.933624, 77.629628, 'Beauty & Personal Care', 'bronze', 3.7, 66, '+918861350829'),
    ('Isthaa spa', 'Varthur Sarjapur Road, Near Govt. School, Gunjur, 239, 1st Cross Rd, 1st Block Koramangala, Koramangala, Bangalore', 12.925215, 77.633872, 'Beauty & Personal Care', 'bronze', 3.0, 22, '+919035352000'),
    ('Fringes Unisex Salon', 'Koramangala, Bangalore', 12.934037, 77.627249, 'Beauty & Personal Care', 'gold', 3.7, 300, ''),
    ("St.John's Utility Complex", 'Sarjapur Road, Koramangala, Bangalore', 12.930817, 77.618044, 'Food & Grocery', 'bronze', 3.7, 109, ''),
    ('William Penn', 'Koramangala, Bangalore', 12.93381, 77.619986, 'Stationery', 'free', 3.0, 24, ''),
    ('The Tower Of Pizza', '63, 100 feet Road, Koramangala, Bangalore', 12.933191, 77.623059, 'Food & Grocery', 'free', 3.3, 7, '+919148971958'),
    ('Kushals', 'Koramangala, Bangalore', 12.937841, 77.627242, 'Clothing', 'gold', 4.4, 1811, ''),
    ('Maruti Suzuki', 'Mahayogi Vemana Road, Koramangala, Bangalore', 12.931532, 77.622807, 'Automotive', 'free', 3.3, 15, ''),
    ('Cilantro', 'Koramangala, Bangalore', 12.936067, 77.631433, 'Food & Grocery', 'silver', 3.5, 488, ''),
    ('St Johns opticians and hearing aid', 'Koramangala, Bangalore', 12.930656, 77.6179, 'Health & Wellness', 'silver', 3.8, 441, ''),
    ('ಬೇಕ್ಸ್ ಅಂಡ್ ಶೇಕ್ಸ್', 'Koramangala, Bangalore', 12.930759, 77.617931, 'Food & Grocery', 'gold', 4.3, 478, ''),
    ('ಆಶೀರ್ವಾದ್ ರೆಸ್ಟೋರಂಟ್ - ಸಸ್ಯಾಹಾರಿ', 'Koramangala, Bangalore', 12.931021, 77.61808, 'Food & Grocery', 'gold', 4.4, 618, ''),
    ('ಕೋಸ್ಟಲ್ ಬೇ - ಮಾಂಸಾಹಾರಿ', 'Koramangala, Bangalore', 12.930848, 77.617972, 'Food & Grocery', 'free', 3.4, 9, ''),
    ('Naati Manae', '#462, 17 C Cross, Koramangala, Bangalore', 12.934572, 77.619954, 'Food & Grocery', 'silver', 4.3, 191, ''),
    ('Casinova', 'Koramangala, Bangalore', 12.93368, 77.622276, 'Food & Grocery', 'silver', 4.1, 391, '+918025526363'),
    ('Java City', 'Koramangala, Bangalore', 12.933497, 77.621281, 'Food & Grocery', 'silver', 3.7, 132, ''),
    ('Via Milano', '8, near Sapphire toy store, Koramangala, Bangalore', 12.937011, 77.627526, 'Food & Grocery', 'silver', 3.7, 81, '+918041309997'),
    ('Kottakkal Aruna Vaidya Sala', 'Koramangala, Bangalore', 12.92738, 77.616201, 'Health & Wellness', 'bronze', 3.3, 35, ''),
    ('Bakasur', '23, Koramangala, Bangalore', 12.938061, 77.627557, 'Food & Grocery', 'free', 3.5, 22, '+918792000390'),
    ('Pingara', 'Koramangala, Bangalore', 12.925157, 77.633874, 'Food & Grocery', 'bronze', 3.5, 71, ''),
    ('BSR Lassi Packers Shop', 'Koramangala, Bangalore', 12.934439, 77.629535, 'Food & Grocery', 'silver', 3.6, 301, ''),
    ('Bistro Claytopia', 'Koramangala, Bangalore', 12.925546, 77.633333, 'Food & Grocery', 'free', 3.2, 55, ''),
    ('Magnolia', 'Koramangala, Bangalore', 12.926004, 77.633286, 'Food & Grocery', 'free', 3.3, 56, ''),
    ('iService - iPhone, MacBook, Oneplus, Xiaomi Repairs and Service Center', 'Koramangala, Bangalore', 12.926672, 77.633571, 'Electronics', 'silver', 3.5, 87, ''),
    ('Pragathi Homeo Pharmacy', 'Koramangala, Bangalore', 12.925508, 77.634356, 'Health & Wellness', 'free', 3.4, 57, ''),
    ('Kiran Medical Store', 'Koramangala, Bangalore', 12.933257, 77.629659, 'Health & Wellness', 'free', 3.2, 0, ''),
    ('The Tiffin Express', 'Koramangala, Bangalore', 12.936761, 77.627684, 'Food & Grocery', 'gold', 4.4, 645, ''),
    ('Davanam Sarovar Portico Suites', 'Koramangala, Bangalore', 12.921697, 77.620318, 'Food & Grocery', 'free', 3.0, 50, ''),
    ('Škoda', 'Koramangala, Bangalore', 12.917261, 77.628134, 'Automotive', 'silver', 3.9, 444, ''),
    ('Lounge 189', 'Koramangala, Bangalore', 12.91791, 77.62388, 'Food & Grocery', 'bronze', 3.4, 37, ''),
    ('The Aroma', 'Koramangala, Bangalore', 12.917854, 77.624107, 'Food & Grocery', 'bronze', 3.0, 196, ''),
    ('Cafe Awesome', 'Koramangala, Bangalore', 12.917827, 77.623798, 'Food & Grocery', 'gold', 4.2, 1261, ''),
    ('Sour House', 'Koramangala, Bangalore', 12.935075, 77.624929, 'Food & Grocery', 'bronze', 4.0, 105, ''),
    ('La Arabia', 'Koramangala, Bangalore', 12.933337, 77.622692, 'Food & Grocery', 'silver', 3.8, 373, ''),
    ("What's In a Name", 'Koramangala, Bangalore', 12.933843, 77.6198, 'Food & Grocery', 'platinum', 4.6, 3492, ''),
    ("Moplah's", 'Koramangala, Bangalore', 12.933459, 77.621047, 'Food & Grocery', 'bronze', 4.0, 40, ''),
    ('Cafe Malabars', 'Koramangala, Bangalore', 12.93348, 77.621003, 'Food & Grocery', 'gold', 4.5, 693, ''),
    ('Tim Tai', 'Koramangala, Bangalore', 12.933693, 77.622465, 'Food & Grocery', 'silver', 3.6, 70, ''),
    ('Green Grocer', '8th Cross Road, Koramangala, Bangalore', 12.93202, 77.633368, 'Food & Grocery', 'platinum', 4.7, 2948, ''),
    ('Skanda Medicals', '991, 7th Main Road, Koramangala, Bangalore', 12.935037, 77.633295, 'Health & Wellness', 'silver', 4.3, 372, ''),
    ('Safa Provision Store', 'Koramangala, Bangalore', 12.934755, 77.633309, 'Food & Grocery', 'silver', 3.9, 373, ''),
    ('iCure Medicals', '4th Main Road, Koramangala, Bangalore', 12.935207, 77.631545, 'Health & Wellness', 'bronze', 3.2, 29, ''),
    ('Login2Eat.com', 'Koramangala, Bangalore', 12.935321, 77.629935, 'Food & Grocery', 'free', 3.4, 75, ''),
    ('KM Shukla Store', 'Koramangala, Bangalore', 12.935277, 77.629963, 'Food & Grocery', 'free', 3.1, 69, ''),
    ('Alt Pizza', '974, 80 Feet Road, Koramangala, Bangalore', 12.935111, 77.629216, 'Food & Grocery', 'silver', 3.9, 477, '+918123892268'),
    ('Patel Condiments', 'Koramangala, Bangalore', 12.919849, 77.62125, 'Food & Grocery', 'gold', 4.7, 133, ''),
    ('Maffei Kitchen', '23, 5th Cross Road, Koramangala, Bangalore', 12.935119, 77.622637, 'Food & Grocery', 'free', 2.8, 5, '+918005471058'),
    ('The Tea Toast Co.', 'Koramangala, Bangalore', 12.933387, 77.622352, 'Food & Grocery', 'bronze', 3.2, 187, ''),
    ('Coorg Fresh Vegetables and Fruits', '39, 8th Main Road, Koramangala, Bangalore', 12.935046, 77.624994, 'Food & Grocery', 'silver', 3.9, 397, '+919620848497'),
    ('Pasta Street', 'Koramangala, Bangalore', 12.932859, 77.630986, 'Food & Grocery', 'gold', 4.2, 1617, '+919266844026'),
    ('De Shack', 'Koramangala, Bangalore', 12.934515, 77.623246, 'Food & Grocery', 'silver', 4.3, 68, ''),
    ("Shyamji's Chole Bature", 'Koramangala, Bangalore', 12.935446, 77.621255, 'Food & Grocery', 'silver', 3.9, 59, ''),
    ('Chaska Bun', 'Koramangala, Bangalore', 12.935887, 77.621426, 'Food & Grocery', 'bronze', 3.9, 31, ''),
    ('Fresh Pressery', 'Koramangala, Bangalore', 12.935628, 77.621039, 'Food & Grocery', 'free', 3.3, 1, ''),
    ('Churoland', 'Koramangala, Bangalore', 12.93376, 77.618829, 'Food & Grocery', 'free', 2.7, 39, ''),
    ('Bookstop', 'Koramangala, Bangalore', 12.933796, 77.618941, 'Books', 'gold', 3.8, 1351, ''),
    ('PizzaExpress', 'Koramangala, Bangalore', 12.93378, 77.621495, 'Food & Grocery', 'free', 3.3, 13, ''),
    ('Fermentation Stories', 'Koramangala, Bangalore', 12.933433, 77.621654, 'Food & Grocery', 'free', 3.3, 78, ''),
    ('Hello Mobile', 'Koramangala, Bangalore', 12.918348, 77.632001, 'Electronics', 'free', 3.5, 54, ''),
    ('The Verandah', 'Koramangala, Bangalore', 12.92916, 77.62738, 'Food & Grocery', 'bronze', 3.8, 193, ''),
    ('Chai Fast', 'Koramangala, Bangalore', 12.927133, 77.633203, 'Food & Grocery', 'bronze', 3.1, 195, ''),
    ('Mercedes Benz', 'Koramangala, Bangalore', 12.935546, 77.62827, 'Automotive', 'silver', 3.5, 478, ''),
    ('Sony Centre', 'Koramangala, Bangalore', 12.93664, 77.627762, 'Electronics', 'platinum', 4.2, 873, ''),
    ('imagine', 'Koramangala, Bangalore', 12.936378, 77.627478, 'Electronics', 'silver', 3.7, 111, ''),
    ('Amona', 'Koramangala, Bangalore', 12.936354, 77.627423, 'Beauty & Personal Care', 'free', 3.5, 21, ''),
    ('Lavish Pharma', 'Koramangala, Bangalore', 12.937452, 77.626582, 'Health & Wellness', 'silver', 4.0, 274, ''),
    ('Satyam Office Suppliers', 'Koramangala, Bangalore', 12.937405, 77.626627, 'Stationery', 'free', 3.3, 17, ''),
    ('Mega IT Store', 'Koramangala, Bangalore', 12.938193, 77.626, 'Electronics', 'free', 3.5, 78, ''),
    ('JusBake', 'Koramangala, Bangalore', 12.926838, 77.633267, 'Food & Grocery', 'bronze', 3.2, 144, ''),
    ('Electronics Bazaar', 'Koramangala, Bangalore', 12.933798, 77.629961, 'Electronics', 'free', 2.7, 11, ''),
    ('LG', 'Koramangala, Bangalore', 12.926602, 77.633281, 'Electronics', 'silver', 4.0, 195, ''),
    ('Tatvika Cafe', 'Koramangala, Bangalore', 12.926807, 77.633273, 'Food & Grocery', 'bronze', 3.8, 141, ''),
    ('Cut & Style Salon', 'Koramangala, Bangalore', 12.929503, 77.632663, 'Beauty & Personal Care', 'silver', 3.6, 377, ''),
    ('Gabru Di Chaap', 'Koramangala, Bangalore', 12.930954, 77.63307, 'Food & Grocery', 'free', 3.2, 30, ''),
    ('YLG Salon', 'Koramangala, Bangalore', 12.93115, 77.632586, 'Beauty & Personal Care', 'gold', 4.3, 177, ''),
    ('Al Taza', '1025, 80 Feet Road, Koramangala, Bangalore', 12.930826, 77.633078, 'Food & Grocery', 'silver', 4.1, 363, ''),
    ('Royal Tandoor', 'Koramangala, Bangalore', 12.936757, 77.62761, 'Food & Grocery', 'platinum', 4.6, 2967, ''),
    ('iworld', 'Koramangala, Bangalore', 12.936993, 77.627399, 'Electronics', 'bronze', 3.0, 80, ''),
    ('Daily Bakehouse', '82, 7th Cross Road, Koramangala, Bangalore', 12.931396, 77.623472, 'Food & Grocery', 'free', 3.7, 12, ''),
    ('Just Loaf', '82, 7th Cross Road, Koramangala, Bangalore', 12.931346, 77.62346, 'Food & Grocery', 'silver', 4.3, 326, ''),
    ('Cafe Reset', 'Koramangala, Bangalore', 12.93762, 77.62438, 'Food & Grocery', 'bronze', 3.5, 180, ''),
    ('Roastea', '612, 80 Feet Road, Koramangala, Bangalore', 12.935795, 77.628055, 'Food & Grocery', 'silver', 3.7, 149, ''),
    ('Mahe Global Kitchen', 'Koramangala, Bangalore', 12.933693, 77.622146, 'Food & Grocery', 'free', 2.8, 60, ''),
    ('Volkswagen City Store', 'Koramangala, Bangalore', 12.929337, 77.621892, 'Automotive', 'bronze', 3.2, 31, ''),
    ('Roma Italian Deli', 'Koramangala, Bangalore', 12.934051, 77.619651, 'Food & Grocery', 'free', 3.1, 26, ''),
    ('Helloo Dilli', 'Koramangala, Bangalore', 12.931798, 77.632117, 'Food & Grocery', 'bronze', 3.3, 136, ''),
    ('Zeiss Vision Center', 'Koramangala, Bangalore', 12.931509, 77.632421, 'Health & Wellness', 'free', 3.4, 68, ''),
    ('Thai Basil', 'Koramangala, Bangalore', 12.931399, 77.632905, 'Food & Grocery', 'free', 3.5, 62, ''),
    ('Onesta', '80 Feet Road, Koramangala, Bangalore', 12.93333, 77.631056, 'Food & Grocery', 'bronze', 3.1, 80, ''),
    ('Noodira Salon', '80 Feet Road, Koramangala, Bangalore', 12.932325, 77.632087, 'Beauty & Personal Care', 'silver', 4.0, 216, ''),
    ('Cafe OTW On The Way', '7th Cross Road, Koramangala, Bangalore', 12.929535, 77.630815, 'Food & Grocery', 'bronze', 3.7, 103, ''),
    ('Nanesh Unisex Salon', '7th Cross Road, Koramangala, Bangalore', 12.929533, 77.632015, 'Beauty & Personal Care', 'free', 2.8, 29, ''),
    ('Kaffe 399', '7th Cross Road, Koramangala, Bangalore', 12.929526, 77.631923, 'Food & Grocery', 'free', 3.4, 25, ''),
    ('Belgian Waffle', '80 Feet Road, Koramangala, Bangalore', 12.930586, 77.63317, 'Food & Grocery', 'bronze', 3.2, 140, ''),
    ('Brunch Project', 'Koramangala, Bangalore', 12.93331, 77.627007, 'Food & Grocery', 'silver', 3.5, 273, ''),
    ('Mitra Cafe', 'Koramangala, Bangalore', 12.93678, 77.624912, 'Food & Grocery', 'free', 3.6, 58, ''),
    ('Sunshine Cafe', 'Koramangala, Bangalore', 12.925846, 77.618438, 'Food & Grocery', 'free', 3.1, 58, ''),
    ('Kebapci', '74, 17th E Main Road, Koramangala, Bangalore', 12.936162, 77.622471, 'Food & Grocery', 'silver', 4.0, 188, ''),
    ('Aloe Spa', 'Hosur Road, Koramangala, Bangalore', 12.921826, 77.620557, 'Beauty & Personal Care', 'free', 2.8, 72, '+919986511272'),
    ('Nirmaya Spa in Madiwala', 'Hosur Road, Koramangala, Bangalore', 12.925855, 77.617019, 'Beauty & Personal Care', 'free', 2.8, 59, '+918217590197'),
    ('Southern Spa Madiwala', 'Hosur Road, Koramangala, Bangalore', 12.921729, 77.620269, 'Beauty & Personal Care', 'bronze', 3.2, 107, '+919108612001'),
    ('Friendz Flavour Kitchen', '10, 4th cross street, Green Leaf Extension, 80 ft road, 4th Block, Koramangala, Koramangala, Bangalore', 12.935355, 77.62955, 'Food & Grocery', 'free', 3.0, 20, '+918971192097'),
    ('Pancharangi Cafe', 'Koramangala, Bangalore', 12.918099, 77.630602, 'Food & Grocery', 'silver', 4.0, 363, ''),
    ('Karavali Restaurant', 'Koramangala, Bangalore', 12.922395, 77.633979, 'Food & Grocery', 'gold', 4.3, 1232, ''),
    ('7th Cross', 'Koramangala, Bangalore', 12.92186, 77.633712, 'Food & Grocery', 'gold', 3.8, 1855, ''),
    ('The High Joint', 'Koramangala, Bangalore', 12.926147, 77.633274, 'Food & Grocery', 'bronze', 3.9, 75, ''),
    ('Original Burger Co.', 'Koramangala, Bangalore', 12.933742, 77.618698, 'Food & Grocery', 'silver', 4.2, 192, ''),
    ('Big Guys: Wings & More', 'Koramangala, Bangalore', 12.925648, 77.625096, 'Food & Grocery', 'free', 3.1, 31, ''),
    ('Puneri Amrutalaya', 'Koramangala, Bangalore', 12.918266, 77.631985, 'Food & Grocery', 'free', 3.1, 28, ''),
    ('Nandini Condiments', 'Koramangala, Bangalore', 12.920464, 77.632894, 'Food & Grocery', 'gold', 4.3, 405, ''),
    ('SLV Hair Dress', 'Koramangala, Bangalore', 12.920517, 77.632908, 'Beauty & Personal Care', 'gold', 4.6, 955, ''),
    ('Maa Kali Fish Centre', 'Koramangala, Bangalore', 12.920573, 77.632918, 'Food & Grocery', 'platinum', 4.0, 4355, '+9184728005'),
    ('V. K Iyengar Bakery', 'Koramangala, Bangalore', 12.920593, 77.633, 'Food & Grocery', 'silver', 3.9, 267, ''),
    ('Y. M. Pork Shop', 'Koramangala, Bangalore', 12.920612, 77.632959, 'Food & Grocery', 'bronze', 3.5, 198, ''),
    ('Hap Daily', 'Koramangala, Bangalore', 12.920821, 77.633603, 'Food & Grocery', 'gold', 4.2, 592, ''),
    ('Krust Cafe', 'Koramangala, Bangalore', 12.928391, 77.636165, 'Food & Grocery', 'free', 3.5, 52, ''),
    ('Bao To Me', 'Koramangala, Bangalore', 12.9288, 77.635334, 'Food & Grocery', 'gold', 3.8, 705, ''),
    ('Crave by Leena', 'Koramangala, Bangalore', 12.934041, 77.630296, 'Food & Grocery', 'free', 3.7, 2, ''),
    ("Bamey's Gastro Bar", 'Koramangala, Bangalore', 12.933894, 77.619828, 'Food & Grocery', 'bronze', 3.3, 200, ''),
    ('By The Blue', 'Koramangala, Bangalore', 12.929265, 77.627847, 'Food & Grocery', 'bronze', 3.6, 169, ''),
    ('Vurve Salon', '791, 9th Main Road, Koramangala, Bangalore', 12.926778, 77.628183, 'Beauty & Personal Care', 'free', 3.2, 17, '+918925814881'),
    ('Shoba super market', 'Koramangala, Bangalore', 12.923905, 77.61832, 'Food & Grocery', 'gold', 4.1, 1717, ''),
    ('Bagelstein', 'Koramangala, Bangalore', 12.935619, 77.625281, 'Food & Grocery', 'gold', 4.3, 1541, ''),
    ('Grill N Chill', 'Koramangala, Bangalore', 12.929557, 77.630891, 'Food & Grocery', 'silver', 4.3, 86, ''),
    ('Champaran Handiwale', '8, 16th Main Road, Koramangala, Bangalore', 12.934586, 77.62577, 'Food & Grocery', 'bronze', 3.7, 155, '+919473227777'),
    ('Brooks And Bonds', 'Koramangala, Bangalore', 12.934054, 77.623296, 'Food & Grocery', 'free', 3.1, 34, ''),
    ('Kund', '100 Feet Road, Indiranagar, Bangalore', 12.980115, 77.640551, 'Food & Grocery', 'silver', 4.0, 344, ''),
    ('Bandhej', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978717, 77.643786, 'Clothing', 'silver', 4.2, 91, ''),
    ('Woodland', 'Indiranagar, Bangalore', 12.973748, 77.641263, 'Clothing', 'silver', 3.7, 78, ''),
    ('Chat', 'Indiranagar, Bangalore', 12.97111, 77.634217, 'Food & Grocery', 'bronze', 3.9, 113, ''),
    ("Sue's Kitchen", 'Indiranagar, Bangalore', 12.97935, 77.642141, 'Food & Grocery', 'gold', 3.8, 848, ''),
    ('M K Retail', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978728, 77.64493, 'Food & Grocery', 'bronze', 3.0, 113, ''),
    ('Chai Patti Teafe', 'Indiranagar, Bangalore', 12.969561, 77.641109, 'Food & Grocery', 'free', 3.3, 8, ''),
    ('Chakum Chukum', 'Indiranagar, Bangalore', 12.972301, 77.639201, 'Food & Grocery', 'silver', 4.2, 120, ''),
    ('Baan Phad Thai', 'Indiranagar, Bangalore', 12.969822, 77.637717, 'Food & Grocery', 'platinum', 4.2, 1430, ''),
    ('Daily Bread', 'Indiranagar, Bangalore', 12.972461, 77.639182, 'Food & Grocery', 'silver', 4.1, 435, ''),
    ('Kobe Sizzler', 'Indiranagar, Bangalore', 12.978234, 77.64261, 'Food & Grocery', 'silver', 4.4, 157, ''),
    ('Beanlore Coffee Roasters', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978729, 77.643906, 'Food & Grocery', 'silver', 4.2, 214, ''),
    ('P N Rao', 'Indiranagar, Bangalore', 12.978645, 77.642524, 'Clothing', 'bronze', 3.3, 194, ''),
    ('Suvasa', '100 Feet Road, Indiranagar, Bangalore', 12.968923, 77.641585, 'Clothing', 'bronze', 3.1, 115, ''),
    ('Trust Medicals', 'Indiranagar, Bangalore', 12.978632, 77.642362, 'Health & Wellness', 'bronze', 3.5, 154, ''),
    ('Sri Kirshna Pharmacy', 'Indiranagar, Bangalore', 12.978351, 77.645634, 'Health & Wellness', 'gold', 4.2, 1189, '+919164345443'),
    ('Saundh', '100 Feet Road, Indiranagar, Bangalore', 12.968948, 77.641627, 'Clothing', 'silver', 3.9, 199, ''),
    ('Erwan Thai SPA', '100 Feet Road( S K Karim Khan Road), Indiranagar, Bangalore', 12.970977, 77.641046, 'Beauty & Personal Care', 'bronze', 3.4, 192, ''),
    ("Gloria Jean's", '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.976128, 77.641214, 'Food & Grocery', 'gold', 4.5, 1908, ''),
    ('Reebok Junior', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.976208, 77.641203, 'Electronics', 'silver', 3.5, 284, ''),
    ('Karachi Bakery', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.977386, 77.641132, 'Food & Grocery', 'free', 3.1, 22, ''),
    ('Best Bakes', 'Indiranagar, Bangalore', 12.968364, 77.639757, 'Food & Grocery', 'free', 3.7, 13, ''),
    ('Jealous21', 'Indiranagar, Bangalore', 12.978592, 77.641093, 'Clothing', 'free', 2.8, 18, ''),
    ('Mobile Store', 'Indiranagar, Bangalore', 12.978597, 77.641646, 'Food & Grocery', 'bronze', 3.9, 153, ''),
    ('99 Dosa', 'Old Syndicate Bank Road, Indiranagar, Bangalore', 12.977699, 77.641143, 'Food & Grocery', 'platinum', 4.0, 3912, ''),
    ('Mast Kalander', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978669, 77.644042, 'Food & Grocery', 'platinum', 4.5, 1581, ''),
    ('Momo', '1st Cross Road, Indiranagar, Bangalore', 12.977661, 77.641629, 'Food & Grocery', 'bronze', 3.4, 185, ''),
    ('Sri Sai Pharma', 'Indiranagar, Bangalore', 12.978697, 77.638473, 'Health & Wellness', 'free', 2.9, 2, ''),
    ('Khan Saheb', 'Indiranagar, Bangalore', 12.971444, 77.635067, 'Food & Grocery', 'gold', 3.9, 1164, ''),
    ('Smart Line', 'Indiranagar, Bangalore', 12.978115, 77.636967, 'Clothing', 'silver', 3.8, 225, ''),
    ('Time Pass', 'Indiranagar, Bangalore', 12.981226, 77.636821, 'Food & Grocery', 'silver', 4.2, 298, ''),
    ('The Mobile Store Lounge', 'Indiranagar, Bangalore', 12.97857, 77.640693, 'Electronics', 'silver', 4.1, 481, ''),
    ('Spice Hotspot', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.97825, 77.640567, 'Electronics', 'bronze', 3.5, 165, ''),
    ('Bridgestone', 'Indiranagar, Bangalore', 12.981023, 77.637136, 'Automotive', 'free', 3.5, 21, ''),
    ('Chinita', '218, Indiranagar Double Road, Indiranagar, Bangalore', 12.981047, 77.637144, 'Food & Grocery', 'silver', 3.6, 240, '+919686551896'),
    ('First Look Color & Style Salon', 'Indiranagar, Bangalore', 12.978991, 77.63937, 'Beauty & Personal Care', 'platinum', 4.8, 887, '+918041222623'),
    ('Dosa Camp', 'Indiranagar, Bangalore', 12.97977, 77.639343, 'Food & Grocery', 'free', 3.0, 21, ''),
    ('Sri Lakshmi Venkateshwara Stationery & Condiments', 'Indiranagar, Bangalore', 12.979695, 77.639347, 'Stationery', 'silver', 3.6, 324, ''),
    ('Adupadi', 'Indiranagar, Bangalore', 12.978816, 77.636594, 'Food & Grocery', 'free', 3.7, 19, ''),
    ('Famous Electronics', 'Indiranagar, Bangalore', 12.978385, 77.63661, 'Electronics', 'free', 2.8, 40, ''),
    ('Himalaya', 'Indiranagar, Bangalore', 12.978182, 77.642666, 'Health & Wellness', 'silver', 3.8, 239, ''),
    ('Lajawaab', 'Indiranagar, Bangalore', 12.978287, 77.642663, 'Food & Grocery', 'free', 3.3, 4, ''),
    ('Rasa India', 'Indiranagar, Bangalore', 12.978206, 77.643158, 'Food & Grocery', 'silver', 4.4, 125, ''),
    ('Nakshatra Condiments Mess & Fast Food', 'Indiranagar, Bangalore', 12.981623, 77.639248, 'Food & Grocery', 'bronze', 3.5, 61, ''),
    ('Cafe Max', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978399, 77.644311, 'Food & Grocery', 'bronze', 3.1, 26, ''),
    ('Esplanade', 'Indiranagar, Bangalore', 12.978961, 77.636588, 'Food & Grocery', 'bronze', 3.5, 57, ''),
    ('The Wearhouse', 'Indiranagar, Bangalore', 12.978415, 77.637062, 'Clothing', 'gold', 4.6, 419, ''),
    ('Dhanya', 'Indiranagar, Bangalore', 12.978604, 77.638772, 'Stationery', 'free', 3.4, 64, ''),
    ('YLG Salon & Spa', 'Indiranagar, Bangalore', 12.97826, 77.644857, 'Beauty & Personal Care', 'bronze', 3.7, 141, ''),
    ('Cut & Create Unisex Salon', 'Indiranagar, Bangalore', 12.982142, 77.639569, 'Beauty & Personal Care', 'gold', 4.7, 1644, ''),
    ('Istanbul Doner Turkish Grill', 'Indiranagar, Bangalore', 12.982128, 77.639572, 'Food & Grocery', 'platinum', 4.6, 1704, ''),
    ('N.R. Bombay Ladies Taylor', '264, 6th Cross, Indiranagar, Bangalore', 12.981671, 77.640223, 'Clothing', 'silver', 4.1, 276, ''),
    ('Sakhin', 'Indiranagar, Bangalore', 12.973794, 77.643512, 'Clothing', 'bronze', 3.1, 142, ''),
    ('Durga Andhra Mess (Veg/Non-Veg)', 'Indiranagar, Bangalore', 12.980442, 77.639147, 'Food & Grocery', 'gold', 3.8, 1228, ''),
    ('Food Town', 'Indiranagar, Bangalore', 12.982211, 77.639106, 'Food & Grocery', 'free', 2.7, 79, ''),
    ('Sagar Ayur Pharma', 'Indiranagar, Bangalore', 12.982058, 77.639699, 'Health & Wellness', 'gold', 4.1, 289, ''),
    ('Cross Currents', '6th Main Road, Indiranagar, Bangalore', 12.97398, 77.641418, 'Clothing', 'bronze', 3.6, 69, ''),
    ('Five Star Cafe', 'Indiranagar, Bangalore', 12.980819, 77.641, 'Food & Grocery', 'silver', 3.6, 442, ''),
    ('Positive Homeopathy', 'Indiranagar, Bangalore', 12.978651, 77.640122, 'Health & Wellness', 'free', 2.7, 68, ''),
    ('Sparha', 'Indiranagar, Bangalore', 12.981038, 77.641008, 'Beauty & Personal Care', 'silver', 4.2, 396, ''),
    ('Imperial Restaurant', '1314, Indiranagar Double Road (Paramahansa Yogananda road), Indiranagar, Bangalore', 12.975513, 77.636065, 'Food & Grocery', 'silver', 3.5, 312, ''),
    ('Annapoorani', 'Indiranagar, Bangalore', 12.97475, 77.635825, 'Food & Grocery', 'bronze', 3.5, 174, ''),
    ('Healing Touch Pharma', 'Indiranagar, Bangalore', 12.974223, 77.635994, 'Health & Wellness', 'silver', 4.3, 391, ''),
    ('Barebones', 'Indiranagar, Bangalore', 12.980171, 77.640592, 'Food & Grocery', 'bronze', 3.1, 129, ''),
    ('Oxford office solutions', 'Indiranagar, Bangalore', 12.974259, 77.636174, 'Stationery', 'bronze', 3.4, 92, ''),
    ('Indira Food Court', 'Indiranagar, Bangalore', 12.982613, 77.638292, 'Food & Grocery', 'bronze', 3.3, 134, ''),
    ('FR Bakery', 'Indiranagar, Bangalore', 12.982843, 77.64049, 'Food & Grocery', 'gold', 3.8, 1590, ''),
    ('Roll Khazana', 'Indiranagar, Bangalore', 12.9822, 77.638333, 'Food & Grocery', 'gold', 4.4, 951, ''),
    ('Curries and Pickles', 'Indiranagar, Bangalore', 12.982201, 77.638294, 'Food & Grocery', 'gold', 4.0, 1750, ''),
    ('Annapoorneshwari Mess', 'Indiranagar, Bangalore', 12.981925, 77.639507, 'Food & Grocery', 'silver', 4.3, 423, ''),
    ("Chetty's Coffee", 'Indiranagar, Bangalore', 12.981244, 77.63664, 'Food & Grocery', 'gold', 3.8, 1302, ''),
    ('Chocolate Lounge', 'Indiranagar, Bangalore', 12.970543, 77.642076, 'Food & Grocery', 'silver', 3.5, 205, ''),
    ('Ashoka Tyre Centre', 'Indiranagar, Bangalore', 12.982198, 77.638026, 'Automotive', 'bronze', 3.9, 105, ''),
    ('Sree Manjunatha Coffee Bar', 'Indiranagar, Bangalore', 12.982402, 77.637685, 'Food & Grocery', 'free', 3.7, 48, ''),
    ('Kolkata Famous Chats', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978037, 77.633104, 'Food & Grocery', 'free', 3.1, 34, ''),
    ('Neha Fancy', 'Indiranagar, Bangalore', 12.978031, 77.633152, 'Clothing', 'bronze', 3.6, 165, ''),
    ('Swathi Medical Store', 'Indiranagar, Bangalore', 12.978045, 77.633244, 'Health & Wellness', 'silver', 3.5, 145, ''),
    ('Padmavathi Bakers', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978045, 77.633935, 'Food & Grocery', 'gold', 4.0, 1104, ''),
    ('Shabari Electricity Company', 'Indiranagar, Bangalore', 12.978031, 77.633786, 'Electronics', 'free', 3.5, 74, ''),
    ('Shoes City', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978287, 77.633543, 'Clothing', 'bronze', 3.6, 138, ''),
    ('Car Museum', 'Indiranagar, Bangalore', 12.982639, 77.638141, 'Automotive', 'free', 3.5, 19, ''),
    ('Cuts & Curls Beauty Parlor', 'Indiranagar, Bangalore', 12.98253, 77.63829, 'Beauty & Personal Care', 'free', 3.7, 60, ''),
    ('Rebeni Beauty Salon', 'Indiranagar, Bangalore', 12.982701, 77.63814, 'Beauty & Personal Care', 'free', 3.4, 36, ''),
    ('Juicea- Fresh juice and ice creams', 'Indiranagar, Bangalore', 12.982223, 77.639035, 'Food & Grocery', 'bronze', 3.5, 196, ''),
    ('Moies Beauty and Hair Salon', 'Indiranagar, Bangalore', 12.982149, 77.638143, 'Beauty & Personal Care', 'silver', 3.4, 441, ''),
    ('Profile Unisex Salon', 'Indiranagar, Bangalore', 12.98224, 77.638815, 'Beauty & Personal Care', 'free', 3.7, 8, ''),
    ('Nokia', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978056, 77.634088, 'Electronics', 'silver', 3.8, 296, ''),
    ('Jockey Clotes', 'Indiranagar, Bangalore', 12.978372, 77.63581, 'Clothing', 'silver', 4.3, 371, ''),
    ('MyToyz', 'Indiranagar, Bangalore', 12.978428, 77.637856, 'Toys & Baby', 'silver', 4.4, 468, ''),
    ('Classic Polo', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978097, 77.634707, 'Clothing', 'free', 3.2, 73, ''),
    ('Deepa Stores', 'Indiranagar, Bangalore', 12.97811, 77.634879, 'Food & Grocery', 'free', 3.0, 74, ''),
    ("Aafreen's Beauty and hair salon", 'Indiranagar, Bangalore', 12.980126, 77.63754, 'Beauty & Personal Care', 'free', 3.0, 17, ''),
    ('Sree Dhanalakshmi Stores', 'Indiranagar, Bangalore', 12.980077, 77.638586, 'Food & Grocery', 'bronze', 3.1, 112, ''),
    ('Sree sai rice corner', 'Indiranagar, Bangalore', 12.980055, 77.638502, 'Food & Grocery', 'bronze', 3.9, 111, ''),
    ('RR ladies tailor', 'Indiranagar, Bangalore', 12.981191, 77.638421, 'Clothing', 'free', 3.0, 9, ''),
    ('Sree Maruthi Medicals', 'Indiranagar, Bangalore', 12.980044, 77.63804, 'Health & Wellness', 'bronze', 3.8, 66, ''),
    ('Ethic Attic', 'Indiranagar, Bangalore', 12.980378, 77.636191, 'Clothing', 'silver', 4.0, 135, ''),
    ('Deen Dayal Cloth Store', 'Indiranagar, Bangalore', 12.978136, 77.636471, 'Clothing', 'bronze', 3.9, 195, ''),
    ('Fashion Line', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978136, 77.636318, 'Clothing', 'silver', 3.7, 407, ''),
    ('Moto Adda', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978114, 77.635497, 'Automotive', 'silver', 3.5, 346, ''),
    ('Philips Store', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978101, 77.635205, 'Electronics', 'bronze', 3.1, 55, ''),
    ('Venkateshwara Tyre Center', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978105, 77.635083, 'Automotive', 'bronze', 3.4, 144, ''),
    ('Al Zaid', 'Indiranagar, Bangalore', 12.982257, 77.639312, 'Food & Grocery', 'bronze', 3.9, 61, ''),
    ('Now Fashion Lounge', 'Indiranagar, Bangalore', 12.978734, 77.633122, 'Clothing', 'free', 2.9, 28, ''),
    ('Arman Motors', 'Indiranagar, Bangalore', 12.979011, 77.633211, 'Automotive', 'gold', 4.5, 1244, ''),
    ('DJ Seafoods', 'Indiranagar, Bangalore', 12.979137, 77.633216, 'Food & Grocery', 'silver', 4.2, 319, ''),
    ('Eggies', 'Indiranagar, Bangalore', 12.978808, 77.633151, 'Food & Grocery', 'gold', 4.4, 1538, ''),
    ('Le Cremosa', 'Indiranagar, Bangalore', 12.980095, 77.639819, 'Food & Grocery', 'silver', 3.5, 285, ''),
    ('Sony', 'Indiranagar, Bangalore', 12.978602, 77.636265, 'Electronics', 'gold', 3.8, 368, ''),
    ('Fruit Face', '483, 13th Cross Road, Indiranagar, Bangalore', 12.981157, 77.636618, 'Food & Grocery', 'gold', 4.2, 738, ''),
    ('Padam Pharma', 'Indiranagar, Bangalore', 12.982779, 77.640526, 'Health & Wellness', 'silver', 3.5, 276, ''),
    ('The Bangalore Pantry', 'Indiranagar, Bangalore', 12.97946, 77.640631, 'Food & Grocery', 'gold', 4.3, 1663, ''),
    ('Anjappar Chettinad Restaurant', '100 Feet Road, Indiranagar, Bangalore', 12.975535, 77.641369, 'Food & Grocery', 'platinum', 4.2, 2995, ''),
    ('Mobile Care', 'Indiranagar, Bangalore', 12.975502, 77.641296, 'Electronics', 'free', 3.5, 28, ''),
    ('Biba', 'Indiranagar, Bangalore', 12.977444, 77.640805, 'Clothing', 'free', 3.4, 29, ''),
    ('Creme & Crust', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.974988, 77.64124, 'Food & Grocery', 'bronze', 3.2, 40, ''),
    ('Peacock Fine Dining', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.975063, 77.641242, 'Food & Grocery', 'bronze', 3.8, 48, ''),
    ('Carlton', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.974273, 77.641325, 'Clothing', 'free', 3.5, 55, ''),
    ('Fanzart', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.974188, 77.641315, 'Electronics', 'free', 2.8, 65, ''),
    ('Gimi and Jony', 'Indiranagar, Bangalore', 12.974187, 77.641381, 'Clothing', 'silver', 3.9, 179, ''),
    ('Jack & Jones', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.972924, 77.641322, 'Clothing', 'free', 3.0, 1, ''),
    ('Ellee Dress', 'Indiranagar, Bangalore', 12.970876, 77.641015, 'Clothing', 'silver', 4.2, 358, ''),
    ('Guess', 'Indiranagar, Bangalore', 12.970037, 77.641111, 'Clothing', 'free', 3.6, 23, ''),
    ('iPlanet', 'Indiranagar, Bangalore', 12.969963, 77.641114, 'Electronics', 'silver', 3.4, 124, ''),
    ('Hungry Walker', 'Indiranagar, Bangalore', 12.9696, 77.641102, 'Food & Grocery', 'gold', 4.6, 1287, ''),
    ('The Body Shop', 'Indiranagar, Bangalore', 12.9701, 77.641107, 'Clothing', 'bronze', 3.2, 148, ''),
    ('Little Italy', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.977395, 77.640698, 'Food & Grocery', 'silver', 4.0, 308, ''),
    ('Cosmetic Studio', '100 Feet Road, Indiranagar, Bangalore', 12.972333, 77.641422, 'Beauty & Personal Care', 'silver', 3.5, 346, ''),
    ('Pepe Jeans', 'Indiranagar, Bangalore', 12.969799, 77.641633, 'Clothing', 'silver', 3.9, 416, ''),
    ('Poorika Mobile World', '100 Feet Road, Indiranagar, Bangalore', 12.969631, 77.641609, 'Electronics', 'free', 3.0, 68, ''),
    ('pakwan', 'Indiranagar, Bangalore', 12.974266, 77.640904, 'Food & Grocery', 'free', 3.4, 27, ''),
    ('Sri Udupi Park', '100 Feet Road( S K Karim Khan Road), Indiranagar, Bangalore', 12.973672, 77.640914, 'Food & Grocery', 'gold', 4.6, 1072, ''),
    ('SS Store', 'Indiranagar, Bangalore', 12.981648, 77.642258, 'Food & Grocery', 'bronze', 3.6, 132, ''),
    ('New glamour spa', '100 Feet Road( S K Karim Khan Road), Indiranagar, Bangalore', 12.971987, 77.640917, 'Beauty & Personal Care', 'silver', 3.5, 477, ''),
    ('Pantaloons', '100 Feet Road, Indiranagar, Bangalore', 12.971735, 77.641, 'Clothing', 'silver', 3.8, 244, ''),
    ('Soma shop', '100 Feet Road, Indiranagar, Bangalore', 12.972012, 77.640981, 'Clothing', 'bronze', 3.5, 124, ''),
    ('Brooks Brothers', 'Indiranagar, Bangalore', 12.971245, 77.640971, 'Clothing', 'free', 3.7, 15, ''),
    ('Kaya skin bar', 'Indiranagar, Bangalore', 12.971566, 77.641, 'Beauty & Personal Care', 'bronze', 3.5, 146, ''),
    ('The Thai spa', '100 Feet Road, Indiranagar, Bangalore', 12.971188, 77.640976, 'Beauty & Personal Care', 'silver', 4.1, 335, ''),
    ('BiteMe Cupcakes', 'Indiranagar, Bangalore', 12.970601, 77.641557, 'Food & Grocery', 'silver', 3.5, 302, ''),
    ('Thai Refresh', '100 Feet Road(S K Karim Khan Road), Indiranagar, Bangalore', 12.970615, 77.641501, 'Food & Grocery', 'free', 3.0, 76, ''),
    ('The Biriyani World', '100 Feet Road, Indiranagar, Bangalore', 12.969458, 77.641589, 'Food & Grocery', 'silver', 4.1, 480, ''),
    ('IZOD', '100 Feet Road, Indiranagar, Bangalore', 12.968986, 77.641706, 'Clothing', 'bronze', 3.6, 94, ''),
    ('Chai Gali', 'Indiranagar, Bangalore', 12.981682, 77.639488, 'Food & Grocery', 'platinum', 4.4, 1070, ''),
    ('Ashwini Car Bazar', 'Indiranagar, Bangalore', 12.984144, 77.641265, 'Automotive', 'gold', 4.5, 1490, ''),
    ('Superior Motors', 'Indiranagar, Bangalore', 12.984101, 77.641091, 'Automotive', 'bronze', 3.0, 192, ''),
    ("Brahmin's Tatte Idli", 'Indiranagar, Bangalore', 12.980191, 77.637244, 'Food & Grocery', 'bronze', 3.1, 132, ''),
    ('Biryani Palace', 'Indiranagar, Bangalore', 12.979151, 77.641132, 'Food & Grocery', 'bronze', 3.7, 50, ''),
    ('Hum India Fashion', 'Indiranagar, Bangalore', 12.979458, 77.641106, 'Clothing', 'free', 3.1, 75, ''),
    ('Lee', 'Indiranagar, Bangalore', 12.967631, 77.641131, 'Clothing', 'platinum', 4.7, 1882, ''),
    ('Vans', 'Indiranagar, Bangalore', 12.967648, 77.641131, 'Clothing', 'free', 3.7, 43, ''),
    ('Shri Sai Designer Boutique', 'Indiranagar, Bangalore', 12.979459, 77.635184, 'Clothing', 'free', 3.5, 51, ''),
    ('New Life Kerala Mess', 'Indiranagar, Bangalore', 12.972645, 77.638268, 'Food & Grocery', 'free', 3.5, 63, ''),
    ('Coastal Delight', 'Indiranagar, Bangalore', 12.978365, 77.636205, 'Food & Grocery', 'free', 3.4, 74, ''),
    ('Raaga', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978146, 77.636488, 'Food & Grocery', 'silver', 4.2, 460, ''),
    ('Wanley Restaurant', 'Sri Krishna Temple Road, Indiranagar, Bangalore', 12.979271, 77.642644, 'Food & Grocery', 'silver', 3.8, 293, ''),
    ("Viceroy's Sarathi", 'Sri Krishna Temple Road, Indiranagar, Bangalore', 12.979283, 77.642517, 'Food & Grocery', 'silver', 4.4, 298, ''),
    ('New Roll Centre', '6th Main, Indiranagar, Bangalore', 12.973036, 77.635691, 'Food & Grocery', 'bronze', 3.1, 136, ''),
    ('Sharon Tea Stall', '7, Thippasandra Main Road, Indiranagar, Bangalore', 12.973167, 77.647047, 'Food & Grocery', 'free', 3.0, 63, ''),
    ('Pathaan Sir', 'Indiranagar, Bangalore', 12.970293, 77.635769, 'Food & Grocery', 'free', 3.6, 3, ''),
    ('Sri Vasavi Super Market', 'Indiranagar, Bangalore', 12.972602, 77.634627, 'Food & Grocery', 'free', 2.8, 8, ''),
    ('Karnataka Bakery', 'Indiranagar, Bangalore', 12.973054, 77.635192, 'Food & Grocery', 'bronze', 3.0, 65, ''),
    ('Nagasree Bakery', 'Indiranagar, Bangalore', 12.972892, 77.634771, 'Food & Grocery', 'bronze', 3.7, 59, ''),
    ('Santosh Tailors', '1100, Indiranagar, Bangalore', 12.973074, 77.635192, 'Clothing', 'bronze', 3.6, 39, ''),
    ("Siri's Camille's Ice Cream Bar", '80 Feet Road(Sir C.V. Raman Hospital Road), Indiranagar, Bangalore', 12.97022, 77.647687, 'Food & Grocery', 'silver', 3.9, 302, ''),
    ('The House of Fooba Wooba', 'Indiranagar, Bangalore', 12.970264, 77.647678, 'Clothing', 'silver', 3.6, 388, ''),
    ('C Store', '80 Feet Road(Sir C.V. Raman Hospital Road), Indiranagar, Bangalore', 12.970466, 77.64765, 'Electronics', 'bronze', 3.5, 158, ''),
    ('Kay Clinic', '80 Feet Road(Sir C.V. Raman Hospital Road), Indiranagar, Bangalore', 12.970301, 77.647352, 'Beauty & Personal Care', 'bronze', 3.9, 159, ''),
    ('Chicco', '12th Main Road, Indiranagar, Bangalore', 12.970581, 77.646475, 'Toys & Baby', 'free', 3.5, 55, ''),
    ('Signora', 'Indiranagar, Bangalore', 12.970589, 77.646547, 'Beauty & Personal Care', 'gold', 3.8, 1021, ''),
    ('Bounce Style Lounge', '12th Main Road, Indiranagar, Bangalore', 12.970574, 77.645247, 'Beauty & Personal Care', 'bronze', 3.4, 137, ''),
    ('Lenovo Authorised Mobile Service Center', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978113, 77.635754, 'Electronics', 'free', 2.9, 38, ''),
    ('Hotel Dil Coorg', '17th E Cross, Indiranagar, Bangalore', 12.977913, 77.632896, 'Food & Grocery', 'gold', 3.9, 982, ''),
    ('Safa Super Bazar', 'Indiranagar, Bangalore', 12.97286, 77.634775, 'Food & Grocery', 'free', 3.4, 62, ''),
    ('Nilgiris 1905', '6th Main Road, Indiranagar, Bangalore', 12.974101, 77.6449, 'Food & Grocery', 'silver', 4.2, 341, ''),
    ('Lazy Suzy', '80 Feet Road, Indiranagar, Bangalore', 12.970851, 77.647489, 'Food & Grocery', 'silver', 4.1, 400, ''),
    ('Onestà', '501, Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978728, 77.643536, 'Food & Grocery', 'bronze', 3.5, 163, '+918049653087'),
    ('Indian Biryani', 'Indiranagar, Bangalore', 12.979258, 77.64151, 'Food & Grocery', 'gold', 4.2, 901, ''),
    ('Halli Mani Rotti', 'Indiranagar, Bangalore', 12.979266, 77.641762, 'Food & Grocery', 'free', 3.3, 48, ''),
    ('Chaithanya', 'Indiranagar, Bangalore', 12.979242, 77.641417, 'Food & Grocery', 'silver', 3.8, 195, ''),
    ('Mubarak Mutton Stall', 'Indiranagar, Bangalore', 12.979238, 77.641876, 'Food & Grocery', 'bronze', 3.0, 31, ''),
    ('3G Medicals', 'Indiranagar, Bangalore', 12.979213, 77.64133, 'Health & Wellness', 'free', 3.1, 74, ''),
    ('Pawan Tailor', 'Indiranagar, Bangalore', 12.979084, 77.643899, 'Clothing', 'free', 3.6, 43, ''),
    ('Bogineni', 'Indiranagar, Bangalore', 12.979316, 77.644033, 'Clothing', 'bronze', 3.4, 111, ''),
    ('Hotel Orange tree Residency', 'Indiranagar, Bangalore', 12.979318, 77.643535, 'Food & Grocery', 'bronze', 3.1, 82, ''),
    ('Spoonful Sugar Pastisserie & cafè', 'Indiranagar, Bangalore', 12.979302, 77.643659, 'Food & Grocery', 'free', 3.3, 75, ''),
    ('Four Fountains Spa', 'Indiranagar, Bangalore', 12.978667, 77.641164, 'Beauty & Personal Care', 'free', 3.4, 39, ''),
    ('Lakme', 'Indiranagar, Bangalore', 12.978633, 77.641161, 'Beauty & Personal Care', 'free', 2.9, 68, ''),
    ('Oliva Salon', '496, CMH road, Indiranagar, Bangalore', 12.978714, 77.64275, 'Beauty & Personal Care', 'bronze', 3.5, 128, ''),
    ('The Rolling Pin', 'Indiranagar, Bangalore', 12.978775, 77.641555, 'Food & Grocery', 'silver', 4.3, 186, ''),
    ('Coorg Restaurant', 'Indiranagar, Bangalore', 12.97902, 77.641976, 'Food & Grocery', 'free', 3.2, 2, ''),
    ('Hingley Stationers', 'Indiranagar, Bangalore', 12.97898, 77.641325, 'Stationery', 'platinum', 4.4, 3697, ''),
    ('Silitech Computers', 'Indiranagar, Bangalore', 12.978976, 77.64128, 'Electronics', 'bronze', 3.1, 145, ''),
    ('Cake Palace', 'Indiranagar, Bangalore', 12.97926, 77.641563, 'Food & Grocery', 'free', 3.4, 43, ''),
    ('Pinnacle Computers', 'Indiranagar, Bangalore', 12.979269, 77.64132, 'Electronics', 'bronze', 3.8, 190, ''),
    ('Punjab Khazana', 'Indiranagar, Bangalore', 12.979248, 77.642191, 'Food & Grocery', 'bronze', 3.6, 113, ''),
    ('Cacoons Fashion', 'Indiranagar, Bangalore', 12.978543, 77.641512, 'Clothing', 'platinum', 4.2, 1117, ''),
    ('The Kabab Company', 'Indiranagar Double Road (Paramahansa Yogananda road), Indiranagar, Bangalore', 12.978771, 77.636549, 'Food & Grocery', 'gold', 4.2, 688, ''),
    ('Royal Bakery', '1051/A, 2nd Main Road, Indiranagar, Bangalore', 12.974313, 77.649838, 'Food & Grocery', 'free', 3.0, 73, ''),
    ('Tech Smart', '1st Cross Road, Indiranagar, Bangalore', 12.977425, 77.641717, 'Electronics', 'bronze', 3.2, 43, ''),
    ('Tiffin', 'Indiranagar, Bangalore', 12.969574, 77.645944, 'Food & Grocery', 'silver', 4.0, 353, ''),
    ('Sutradhar', 'Indiranagar, Bangalore', 12.977164, 77.6393, 'Books', 'bronze', 3.1, 155, ''),
    ("D'zoa", 'Indiranagar, Bangalore', 12.970474, 77.642078, 'Clothing', 'bronze', 3.2, 39, ''),
    ('Sri Raghavendra Darshini', 'Indiranagar, Bangalore', 12.974453, 77.638897, 'Food & Grocery', 'silver', 3.7, 461, ''),
    ('Moon Star Mobiles', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978352, 77.636175, 'Electronics', 'free', 3.4, 40, ''),
    ('Select Bakery and Stores', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978378, 77.63577, 'Food & Grocery', 'free', 3.1, 79, ''),
    ('Future Fitness', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978382, 77.636209, 'Sports', 'bronze', 3.7, 160, ''),
    ('Ozone', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978396, 77.635365, 'Beauty & Personal Care', 'free', 2.7, 66, '+918040972353'),
    ('Borg', '47, Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.9781, 77.634733, 'Electronics', 'gold', 4.3, 536, ''),
    ('T.K. News Agencies', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978117, 77.635378, 'Books', 'silver', 4.2, 289, ''),
    ('Dlecta Foods Private Limited', 'Indiranagar, Bangalore', 12.978015, 77.633433, 'Food & Grocery', 'free', 3.3, 78, '+917411177799'),
    ('Limra Fashion for Ladies and Kids', 'Indiranagar, Bangalore', 12.978053, 77.633097, 'Clothing', 'gold', 4.6, 1259, ''),
    ('Satin Soft Salon and Boutique', 'Indiranagar, Bangalore', 12.977719, 77.633336, 'Clothing', 'silver', 3.5, 152, ''),
    ('Sri Krishna Akarshan Boutique', 'Indiranagar, Bangalore', 12.977936, 77.633275, 'Clothing', 'bronze', 3.1, 58, ''),
    ('Eye life', 'Indiranagar, Bangalore', 12.972936, 77.648104, 'Health & Wellness', 'silver', 4.2, 482, ''),
    ('Annapurneshwari rice corner', '22, lakshmipuram 1st main road, Indiranagar, Bangalore', 12.977059, 77.631895, 'Food & Grocery', 'bronze', 3.3, 36, ''),
    ('Jayana', '18th cross, Indiranagar, Bangalore', 12.977856, 77.632265, 'Hardware', 'silver', 4.2, 186, ''),
    ('Mithra vegetable and fruit stall', '12, lakshmipuram 1st main road, Indiranagar, Bangalore', 12.977204, 77.631632, 'Food & Grocery', 'gold', 4.3, 625, ''),
    ('Shree Guru Ganesh Pharma', 'Indiranagar, Bangalore', 12.971994, 77.635835, 'Health & Wellness', 'silver', 3.6, 172, ''),
    ('Laban Palace', 'Indiranagar, Bangalore', 12.972935, 77.635724, 'Food & Grocery', 'free', 2.9, 31, ''),
    ('The Homeo Centre', 'Indiranagar, Bangalore', 12.972998, 77.63573, 'Health & Wellness', 'silver', 3.7, 298, ''),
    ('Srinidhi Sagar Foodline', 'Indiranagar, Bangalore', 12.973384, 77.636004, 'Food & Grocery', 'silver', 3.8, 381, ''),
    ("Rayaru's Kitchen", 'Indiranagar, Bangalore', 12.972879, 77.635724, 'Food & Grocery', 'gold', 3.9, 658, ''),
    ('Byte', 'Indiranagar, Bangalore', 12.97319, 77.635961, 'Food & Grocery', 'free', 3.0, 35, ''),
    ('SLV Sagar Pharma', 'Indiranagar, Bangalore', 12.973781, 77.635928, 'Health & Wellness', 'silver', 3.6, 486, ''),
    ('Madurai Idli Shop', 'Indiranagar, Bangalore', 12.971496, 77.647056, 'Food & Grocery', 'free', 3.4, 2, ''),
    ('Dinner Friends', 'Indiranagar, Bangalore', 12.970892, 77.647119, 'Food & Grocery', 'free', 3.6, 40, ''),
    ('Treat', 'Indiranagar, Bangalore', 12.971754, 77.647042, 'Food & Grocery', 'silver', 4.3, 258, ''),
    ("Chung's Pavilion", 'Indiranagar, Bangalore', 12.971748, 77.647043, 'Food & Grocery', 'gold', 4.4, 1913, ''),
    ('Tasty Stories', 'Indiranagar, Bangalore', 12.972029, 77.647002, 'Food & Grocery', 'free', 3.6, 17, ''),
    ('Green Mart', 'Indiranagar, Bangalore', 12.972128, 77.64699, 'Food & Grocery', 'free', 3.3, 64, ''),
    ('Autoline', 'Indiranagar, Bangalore', 12.969727, 77.637507, 'Automotive', 'gold', 4.2, 1797, ''),
    ('Phobidden Fruit', 'Indiranagar, Bangalore', 12.969924, 77.639632, 'Food & Grocery', 'silver', 4.2, 318, ''),
    ('H K Hearing Wellness Clinic', 'Indiranagar, Bangalore', 12.970318, 77.647353, 'Beauty & Personal Care', 'free', 3.6, 30, ''),
    ('Nailbox', 'Indiranagar, Bangalore', 12.970506, 77.645141, 'Beauty & Personal Care', 'silver', 4.2, 210, ''),
    ('Salt Canvas', 'Indiranagar, Bangalore', 12.970614, 77.646461, 'Food & Grocery', 'bronze', 3.6, 119, ''),
    ('The Fatty Bao', 'Indiranagar, Bangalore', 12.970492, 77.645222, 'Food & Grocery', 'gold', 4.4, 966, ''),
    ('Sree VIdya Stores', 'Indiranagar, Bangalore', 12.972284, 77.637214, 'Food & Grocery', 'free', 2.8, 27, ''),
    ('Cool Club', 'Indiranagar, Bangalore', 12.970102, 77.637009, 'Clothing', 'platinum', 4.4, 2583, ''),
    ('House of Tamara', 'Indiranagar, Bangalore', 12.970119, 77.637144, 'Clothing', 'gold', 3.8, 1192, ''),
    ('Slurp Cooking Studio', 'Indiranagar, Bangalore', 12.970175, 77.638267, 'Food & Grocery', 'bronze', 3.9, 101, ''),
    ('Citrus', 'Indiranagar, Bangalore', 12.970202, 77.638909, 'Food & Grocery', 'bronze', 3.5, 83, ''),
    ('Suta', 'Indiranagar, Bangalore', 12.970234, 77.639197, 'Food & Grocery', 'platinum', 4.9, 1687, ''),
    ('Delhi Highway', 'Indiranagar, Bangalore', 12.970306, 77.639434, 'Food & Grocery', 'bronze', 3.7, 98, ''),
    ('Lakme Saloon', 'Indiranagar, Bangalore', 12.970316, 77.640032, 'Beauty & Personal Care', 'free', 3.7, 77, ''),
    ('Saloon mousse', 'Indiranagar, Bangalore', 12.970338, 77.640845, 'Beauty & Personal Care', 'free', 3.4, 6, ''),
    ('Pot-O-Noodles', 'Indiranagar, Bangalore', 12.96847, 77.640486, 'Food & Grocery', 'silver', 4.2, 172, ''),
    ('GreenTrends Salon', 'Indiranagar, Bangalore', 12.9787, 77.641641, 'Beauty & Personal Care', 'free', 3.1, 46, ''),
    ('Buttercups', 'Indiranagar, Bangalore', 12.979873, 77.640373, 'Clothing', 'gold', 4.4, 1195, ''),
    ('Shraddha Ponappa', 'Indiranagar, Bangalore', 12.979876, 77.640314, 'Clothing', 'gold', 4.6, 895, ''),
    ('Shri Siddhanth Pharma', '3155, Service Road, Hal 2nd Stage, Near-ESI Hospital, Indiranagar, Indiranagar, Bangalore', 12.970238, 77.635654, 'Health & Wellness', 'free', 3.7, 32, '+918025200422'),
    ('Padma Medicals', '292, CMH Road, 100 Feet Road, Indira Nagar II Stage, Hoysala Nagar, Indira Nagar, Indiranagar, Bangalore', 12.978544, 77.640549, 'Health & Wellness', 'bronze', 3.9, 80, '+919448542503'),
    ('GlamShack', 'Indiranagar, Bangalore', 12.980047, 77.637953, 'Clothing', 'free', 3.6, 20, ''),
    ('Juice Cafe', 'Indiranagar, Bangalore', 12.982417, 77.638339, 'Food & Grocery', 'gold', 4.3, 1901, ''),
    ('Thai Chy', 'Indiranagar, Bangalore', 12.977951, 77.636476, 'Food & Grocery', 'gold', 4.3, 1716, ''),
    ('Kilol', 'Indiranagar, Bangalore', 12.977483, 77.640803, 'Clothing', 'free', 3.6, 62, ''),
    ('Kryolan - Professional Make-up', 'Indiranagar, Bangalore', 12.978217, 77.640563, 'Beauty & Personal Care', 'bronze', 3.4, 105, ''),
    ('Ghar Ki Thali', 'Indiranagar, Bangalore', 12.973071, 77.647385, 'Food & Grocery', 'silver', 4.3, 469, ''),
    ('NH 8', 'Indiranagar, Bangalore', 12.973296, 77.64682, 'Food & Grocery', 'free', 2.9, 33, ''),
    ('The Waffle House', 'Indiranagar, Bangalore', 12.970558, 77.647138, 'Food & Grocery', 'bronze', 3.4, 174, ''),
    ('Teal Door Cafe', 'Indiranagar, Bangalore', 12.982486, 77.639338, 'Food & Grocery', 'silver', 3.9, 217, ''),
    ('Noah Sports Store', '3192, HAL 2nd Stage 7th Main, Shirdi Sai Baba Mandir Rd, Indiranagar,, Indiranagar, Bangalore', 12.971123, 77.634246, 'Sports', 'bronze', 3.0, 33, '+919845812465'),
    ('Prakrida', '696, 9th A Main Road, First Stage, Indiranagar,, Landmark-Road next to 1st main defence colony 100feet road, Indiranagar, Bangalore', 12.977688, 77.644374, 'Sports', 'silver', 3.6, 277, '+919663029058'),
    ('Roox Sportswear', '3, 9th Cross Rd, 2nd Stage, Indira Nagar II Stage, Hoysala Nagar, Indiranagar, Indiranagar, Bangalore', 12.98206, 77.638754, 'Sports', 'silver', 3.5, 255, '+918048050633'),
    ('Citi Nest Sports Centre', '91/72, 7th Cross Road, 2nd Stage, Eshwara Layout, Indiranagar, Indiranagar, Bangalore', 12.974752, 77.632997, 'Sports', 'free', 3.0, 73, '+918025258287'),
    ('Aarthi diagnostics', '500, Chinmaya Mission Hospital Rd, Bhadrappa Layout, Indira Nagar 1st Stage, Stage 1, Indiranagar, Indiranagar, Bangalore', 12.978674, 77.64328, 'Health & Wellness', 'free', 2.9, 66, '+918041132068'),
    ('Spencers Supermarket', '8, 80 Feet Road(Sir C.V. Raman Hospital Road), Indiranagar, Bangalore', 12.973344, 77.646819, 'Food & Grocery', 'silver', 4.2, 382, '+91(91)8040365555'),
    ('Aurah Spa And Salon', '2726, indranagar, Indiranagar, Bangalore', 12.979704, 77.646255, 'Beauty & Personal Care', 'silver', 4.2, 401, '+919986835830'),
    ('Blanca Paloma', '620, Indiranagar Double Road, Indiranagar, Bangalore', 12.982213, 77.638687, 'Beauty & Personal Care', 'bronze', 3.9, 135, '+918904174087'),
    ('The Edelweiss', '613, 2nd Main,1st Stage, Indiranagar, Indiranagar, Bangalore', 12.970043, 77.64147, 'Beauty & Personal Care', 'bronze', 3.7, 86, '+918033673623'),
    ('Body Raaga Wellness Spa', '1096, 12th A Main Rd, Near Corporation Bank, , Doopanahalli, Indira Nagar, Indiranagar, Bangalore', 12.969665, 77.638319, 'Beauty & Personal Care', 'free', 3.3, 41, '+918050002828'),
    ('Cloud 9 Unisex Salon and Spa', 'C/O Indiranagar Club, 9th Main,4th Cross, Indiranagar, Indiranagar, Bangalore', 12.976403, 77.642929, 'Beauty & Personal Care', 'silver', 3.6, 262, '+918:00to21:00'),
    ('The Glomour Suit Beauty Salon and Spa', '688,, 1st Main, 1st Stage,Indira Nagar 1st Stage, Indiranagar, Bangalore', 12.977481, 77.643621, 'Beauty & Personal Care', 'bronze', 3.4, 116, '+919980798818'),
    ('Lovly Salon and Spa', 'No.19/1, 12th Cross, Park View Road, Indira Nagar 1st Stage, Indiranagar, Bangalore', 12.981154, 77.638122, 'Beauty & Personal Care', 'gold', 4.6, 559, '+919986555433'),
    ('Kolors', 'Indiranagar, Bangalore', 12.970471, 77.64154, 'Beauty & Personal Care', 'free', 2.7, 71, ''),
    ('Saka Vada Pav', 'Indiranagar, Bangalore', 12.973068, 77.646739, 'Food & Grocery', 'silver', 4.0, 95, ''),
    ('V. P. Bakery', 'Indiranagar, Bangalore', 12.968598, 77.64354, 'Food & Grocery', 'platinum', 4.2, 1882, ''),
    ('Alfaham Grilled Chicken and Shawarma', 'Indiranagar, Bangalore', 12.970002, 77.647406, 'Food & Grocery', 'free', 3.3, 76, ''),
    ('Empire Juice & Dosa Chicken', 'Indiranagar, Bangalore', 12.969925, 77.647424, 'Food & Grocery', 'free', 2.8, 47, ''),
    ('Hema Book Store', 'Indiranagar, Bangalore', 12.973071, 77.647109, 'Books', 'free', 3.3, 1, ''),
    ('Raj Kumar Selections', 'Indiranagar, Bangalore', 12.97273, 77.648192, 'Clothing', 'silver', 3.8, 282, ''),
    ('The IT store', 'Indiranagar, Bangalore', 12.972857, 77.647982, 'Electronics', 'gold', 4.2, 559, ''),
    ('Krish Punjabi Kitchen', 'Indiranagar, Bangalore', 12.974339, 77.648279, 'Food & Grocery', 'silver', 3.6, 491, ''),
    ('Aandal Mess', 'Indiranagar, Bangalore', 12.971244, 77.647146, 'Food & Grocery', 'gold', 4.0, 1628, ''),
    ('The madteapot cafe', 'Indiranagar, Bangalore', 12.97895, 77.640549, 'Food & Grocery', 'bronze', 3.1, 72, ''),
    ('Urban Turban', 'Indiranagar, Bangalore', 12.978896, 77.640498, 'Food & Grocery', 'silver', 4.4, 194, ''),
    ('New Punjabi Hotel', 'Indiranagar, Bangalore', 12.97263, 77.646877, 'Food & Grocery', 'silver', 3.6, 142, ''),
    ('three dots and a dash', '100 Feeet Road, Indiranagar, Bangalore', 12.980692, 77.641005, 'Food & Grocery', 'bronze', 3.1, 94, ''),
    ('Vasavi Super Sandwich', 'Indiranagar, Bangalore', 12.979251, 77.641798, 'Food & Grocery', 'platinum', 4.2, 4194, ''),
    ('Vishal Eye Wear', 'Indiranagar, Bangalore', 12.972488, 77.649001, 'Health & Wellness', 'gold', 4.1, 1501, ''),
    ('Chaithanya Express', 'Indiranagar, Bangalore', 12.982289, 77.639466, 'Food & Grocery', 'silver', 3.5, 290, ''),
    ('Veeranjaneya Swamy Tiffin Centre', 'Thippasandra Main Road, Indiranagar, Bangalore', 12.973167, 77.64702, 'Food & Grocery', 'free', 3.0, 50, ''),
    ('Lokesh Vada Pav Stall', 'Indiranagar, Bangalore', 12.977698, 77.641108, 'Food & Grocery', 'silver', 4.1, 412, ''),
    ('Juicemaker', 'Indiranagar, Bangalore', 12.98249, 77.639473, 'Food & Grocery', 'bronze', 3.6, 38, ''),
    ('Aishwarya Departmental Stores', 'Indiranagar, Bangalore', 12.973711, 77.65075, 'Food & Grocery', 'free', 2.8, 42, ''),
    ('Cross Roads Cafe', 'Indiranagar, Bangalore', 12.975128, 77.64721, 'Food & Grocery', 'gold', 4.2, 825, ''),
    ('NS Medicals', 'Indiranagar, Bangalore', 12.980733, 77.646034, 'Health & Wellness', 'platinum', 4.9, 872, ''),
    ('Madhuram Andhra Restaurant', 'Indiranagar, Bangalore', 12.981241, 77.646201, 'Food & Grocery', 'free', 3.2, 48, ''),
    ('AK Medicals', 'Indiranagar, Bangalore', 12.981074, 77.645969, 'Health & Wellness', 'bronze', 3.4, 114, ''),
    ('S.M Electricals', 'Indiranagar, Bangalore', 12.972492, 77.648983, 'Electronics', 'free', 3.6, 5, ''),
    ('Papergrid', 'Indiranagar, Bangalore', 12.972482, 77.649082, 'Stationery', 'silver', 3.7, 307, ''),
    ('Sri Venkateswara Enterprices', 'Indiranagar, Bangalore', 12.972544, 77.648784, 'Electronics', 'bronze', 3.3, 139, ''),
    ('Venus', 'Indiranagar, Bangalore', 12.972526, 77.648821, 'Electronics', 'free', 3.0, 67, ''),
    ('Ganesh Electricals Enterprices', 'Indiranagar, Bangalore', 12.972519, 77.648836, 'Electronics', 'bronze', 3.3, 100, ''),
    ('Syska LED', 'Indiranagar, Bangalore', 12.972511, 77.64886, 'Electronics', 'free', 2.8, 35, ''),
    ('Globe Electronics', 'Indiranagar, Bangalore', 12.972513, 77.648887, 'Electronics', 'silver', 4.3, 194, ''),
    ('Srivaru Glass', 'Indiranagar, Bangalore', 12.972498, 77.648926, 'Hardware', 'gold', 4.0, 1798, ''),
    ('Sri Viru Glass And Plywoods', 'Indiranagar, Bangalore', 12.972496, 77.648945, 'Hardware', 'silver', 3.7, 210, ''),
    ('Makeover', 'Indiranagar, Bangalore', 12.972323, 77.648969, 'Clothing', 'bronze', 3.2, 145, ''),
    ('Menakshi Electricals', 'Indiranagar, Bangalore', 12.972471, 77.648617, 'Clothing', 'silver', 3.6, 486, ''),
    ('China Bajar', 'Indiranagar, Bangalore', 12.972477, 77.648598, 'Food & Grocery', 'gold', 4.3, 1001, ''),
    ('New Metro Fasion Stores', 'Indiranagar, Bangalore', 12.97264, 77.648343, 'Stationery', 'free', 2.8, 29, ''),
    ('Sri Manjunada Agency', 'Indiranagar, Bangalore', 12.972716, 77.64823, 'Hardware', 'free', 3.2, 20, ''),
    ('Millinions clothings', 'Indiranagar, Bangalore', 12.972775, 77.648054, 'Clothing', 'silver', 3.8, 303, ''),
    ('Bhuvaneswary Ladies Tailors', 'Indiranagar, Bangalore', 12.972852, 77.647996, 'Clothing', 'free', 3.1, 71, ''),
    ('Vinayaka Electricals and Electronics', 'Indiranagar, Bangalore', 12.972871, 77.647953, 'Electronics', 'bronze', 3.5, 57, ''),
    ('Sri Chinmaya Medical and General Store', 'Indiranagar, Bangalore', 12.972882, 77.647929, 'Health & Wellness', 'free', 2.8, 36, ''),
    ('S.M general store', 'Indiranagar, Bangalore', 12.973013, 77.648011, 'Food & Grocery', 'free', 2.9, 65, ''),
    ('Pebbles', 'Indiranagar, Bangalore', 12.972582, 77.648696, 'Toys & Baby', 'gold', 4.3, 1458, ''),
    ("Karthik's Mithai Shoppe", 'Indiranagar, Bangalore', 12.972625, 77.648633, 'Food & Grocery', 'silver', 4.0, 52, ''),
    ('Kamal Selections', 'Indiranagar, Bangalore', 12.972649, 77.648606, 'Clothing', 'bronze', 3.6, 126, ''),
    ('Vee Pee Trendz', 'Indiranagar, Bangalore', 12.972665, 77.648584, 'Clothing', 'gold', 3.9, 1120, ''),
    ('Sri balaji Tailors', 'Indiranagar, Bangalore', 12.972678, 77.648565, 'Clothing', 'silver', 4.3, 63, ''),
    ('Plants', 'Indiranagar, Bangalore', 12.972729, 77.648494, 'Clothing', 'free', 3.7, 15, ''),
    ("Begum's Biriyani", 'Indiranagar, Bangalore', 12.972782, 77.648428, 'Food & Grocery', 'bronze', 3.6, 47, ''),
    ('Liberty Choice Collections', 'Indiranagar, Bangalore', 12.97283, 77.648308, 'Clothing', 'gold', 3.9, 266, ''),
    ('IDNM', 'Indiranagar, Bangalore', 12.972849, 77.648251, 'Clothing', 'bronze', 3.7, 142, ''),
    ('Emperor Family Restaurant', 'Indiranagar, Bangalore', 12.972874, 77.6483, 'Food & Grocery', 'bronze', 4.0, 172, ''),
    ('Sri Sai Mobiles', 'Indiranagar, Bangalore', 12.972292, 77.649381, 'Electronics', 'silver', 3.7, 72, ''),
    ('Pragathi Fashions', 'Indiranagar, Bangalore', 12.972298, 77.649317, 'Clothing', 'silver', 4.4, 216, ''),
    ('Black and White Fashions', 'Indiranagar, Bangalore', 12.972293, 77.649356, 'Clothing', 'gold', 4.7, 1957, ''),
    ('AIWA Fashion', 'Indiranagar, Bangalore', 12.972297, 77.649289, 'Clothing', 'platinum', 4.6, 2231, ''),
    ('Planet Care', 'Indiranagar, Bangalore', 12.97213, 77.642226, 'Electronics', 'gold', 4.5, 679, ''),
    ('Bahn Mi & Wok', '295,296, 100 Feet Road, Indiranagar, Bangalore', 12.978908, 77.640546, 'Food & Grocery', 'bronze', 3.1, 38, ''),
    ('Naaris', 'Indiranagar, Bangalore', 12.981205, 77.645822, 'Clothing', 'bronze', 3.6, 43, ''),
    ('Organic Farm Shop', 'Indiranagar, Bangalore', 12.976117, 77.638818, 'Food & Grocery', 'bronze', 3.2, 122, ''),
    ('Madhurima Bakery', 'Indiranagar, Bangalore', 12.975953, 77.638716, 'Food & Grocery', 'free', 3.6, 20, ''),
    ('Raghavendra Fruits and Vegetables', 'Indiranagar, Bangalore', 12.975961, 77.638693, 'Food & Grocery', 'bronze', 3.4, 185, ''),
    ('Bramalingeshwara Vegetables', 'Indiranagar, Bangalore', 12.976078, 77.637865, 'Food & Grocery', 'silver', 4.1, 111, ''),
    ('Sai Ram Medicals', 'Indiranagar, Bangalore', 12.976085, 77.637795, 'Health & Wellness', 'free', 3.5, 39, ''),
    ('Nikki Bakery', 'Indiranagar, Bangalore', 12.975915, 77.637114, 'Food & Grocery', 'gold', 4.3, 835, ''),
    ('Bismillah Chicken Centre', 'Indiranagar, Bangalore', 12.974259, 77.637825, 'Food & Grocery', 'silver', 4.2, 312, ''),
    ('Sri Raghavendra Hotel', 'Indiranagar, Bangalore', 12.974184, 77.637835, 'Food & Grocery', 'free', 3.1, 49, ''),
    ('Bengali Food Court', 'Indiranagar, Bangalore', 12.974162, 77.637817, 'Food & Grocery', 'bronze', 3.6, 128, ''),
    ('Bengaluru Fruits and Vegetables', 'Indiranagar, Bangalore', 12.974578, 77.639297, 'Food & Grocery', 'free', 3.1, 47, ''),
    ('Amar Medicals', 'Indiranagar, Bangalore', 12.974447, 77.639451, 'Health & Wellness', 'silver', 3.8, 268, ''),
    ('Fishmonger', 'Indiranagar, Bangalore', 12.974559, 77.638893, 'Food & Grocery', 'free', 3.2, 59, ''),
    ('Chicken Stall', 'Indiranagar, Bangalore', 12.974544, 77.638862, 'Food & Grocery', 'silver', 4.0, 415, ''),
    ('Super Mutton Stall', 'Indiranagar, Bangalore', 12.974711, 77.639097, 'Food & Grocery', 'free', 3.2, 80, ''),
    ('New Royal Mutton and Chicken Centre', 'Indiranagar, Bangalore', 12.973439, 77.639512, 'Food & Grocery', 'silver', 3.5, 289, ''),
    ('Pushpa Glass and Plywood', 'Indiranagar, Bangalore', 12.973052, 77.635266, 'Hardware', 'bronze', 3.9, 142, ''),
    ('Kotakkal Arya Vaidyasala', 'Indiranagar, Bangalore', 12.972623, 77.648796, 'Health & Wellness', 'bronze', 4.0, 195, ''),
    ('Mehek Beauty Parlour', '1, 1st Cross Road, Indiranagar, Bangalore', 12.97725, 77.640456, 'Beauty & Personal Care', 'gold', 4.5, 1171, ''),
    ('Sudha Provisions', 'Indiranagar, Bangalore', 12.974477, 77.639074, 'Food & Grocery', 'bronze', 3.6, 161, ''),
    ('Pooja Electronics', 'Indiranagar, Bangalore', 12.971842, 77.635826, 'Electronics', 'free', 2.7, 13, ''),
    ('Sri Bharani Mixtures', 'Indiranagar, Bangalore', 12.972037, 77.649618, 'Food & Grocery', 'bronze', 3.4, 138, ''),
    ('Burma Burma Restaurant & Tea Room', '607, 12th Main Road, 7th Cross, Indiranagar, Bangalore', 12.970557, 77.644731, 'Food & Grocery', 'silver', 4.0, 179, '+918043008120'),
    ('MM Fast Food', 'Indiranagar, Bangalore', 12.973418, 77.639611, 'Food & Grocery', 'bronze', 3.6, 148, ''),
    ('Nature Fresh Fruits & Vegetables', '361, 6th Main, Indiranagar, Bangalore', 12.973418, 77.639581, 'Food & Grocery', 'free', 3.4, 59, ''),
    ('Aishwarya Condiments', 'Indiranagar, Bangalore', 12.973414, 77.639771, 'Food & Grocery', 'free', 2.9, 50, ''),
    ('Books & Books', 'Indiranagar, Bangalore', 12.973327, 77.639253, 'Stationery', 'free', 3.1, 38, ''),
    ('Calcutta Roll Centre', 'Indiranagar, Bangalore', 12.973421, 77.639733, 'Food & Grocery', 'bronze', 3.7, 191, ''),
    ('KS Murthy Gents Beauty Parlour', '112, Indiranagar, Bangalore', 12.97342, 77.639652, 'Beauty & Personal Care', 'bronze', 3.8, 107, ''),
    ('Monte Shoppe', 'Indiranagar, Bangalore', 12.973334, 77.639555, 'Stationery', 'platinum', 4.9, 2033, ''),
    ('Shabeena Provision Store', '302, Indiranagar, Bangalore', 12.973419, 77.639534, 'Food & Grocery', 'free', 2.8, 77, ''),
    ('Hotel Sree Cauvery', 'Indiranagar, Bangalore', 12.974315, 77.636618, 'Food & Grocery', 'free', 3.4, 55, ''),
    ('Third Wave', 'Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978657, 77.645291, 'Food & Grocery', 'bronze', 3.0, 49, ''),
    ('Indel Motors', 'Indiranagar, Bangalore', 12.976566, 77.636237, 'Automotive', 'free', 3.4, 58, ''),
    ('1311 Restaurant', '1311, 100 Feet Road, Indiranagar, Bangalore', 12.969963, 77.641527, 'Food & Grocery', 'silver', 4.3, 431, ''),
    ('Pulimanchi', 'Indiranagar, Bangalore', 12.980679, 77.640511, 'Food & Grocery', 'silver', 3.9, 366, ''),
    ('First Choice Mens Salon', 'Indiranagar, Bangalore', 12.972771, 77.634825, 'Beauty & Personal Care', 'gold', 4.3, 1514, ''),
    ('Dosa shop', 'Indiranagar, Bangalore', 12.977819, 77.630318, 'Food & Grocery', 'silver', 4.1, 252, ''),
    ('Suresh Pharma', 'Indiranagar, Bangalore', 12.977787, 77.630297, 'Health & Wellness', 'gold', 3.8, 376, ''),
    ('Snacks shop', 'Indiranagar, Bangalore', 12.977807, 77.630347, 'Food & Grocery', 'free', 2.8, 2, ''),
    ('Shri Vinayaka Hot Chips', 'Indiranagar, Bangalore', 12.980866, 77.646019, 'Food & Grocery', 'silver', 3.6, 63, ''),
    ('Bharatiya Jalpan', 'Indiranagar, Bangalore', 12.96877, 77.641613, 'Food & Grocery', 'free', 3.3, 30, ''),
    ('Soumya Nandivada', 'Indiranagar, Bangalore', 12.970741, 77.642016, 'Clothing', 'bronze', 3.6, 157, ''),
    ('Kaapi Right', 'Indiranagar, Bangalore', 12.982273, 77.63925, 'Food & Grocery', 'free', 3.0, 59, ''),
    ('Sendhoor Coffee', 'Indiranagar, Bangalore', 12.982247, 77.639034, 'Food & Grocery', 'free', 3.1, 14, ''),
    ('Superstore', 'Indiranagar, Bangalore', 12.976143, 77.639121, 'Food & Grocery', 'free', 2.9, 56, ''),
    ('Pani Puri', 'Indiranagar, Bangalore', 12.976016, 77.639121, 'Food & Grocery', 'platinum', 4.5, 4182, ''),
    ('Sharief Bhai', 'Indiranagar, Bangalore', 12.981197, 77.636873, 'Food & Grocery', 'silver', 3.6, 341, ''),
    ('Sri Racha', 'Indiranagar, Bangalore', 12.970504, 77.644917, 'Food & Grocery', 'gold', 4.1, 783, ''),
    ('N C Condiments', '5th Cross Road, Indiranagar, Bangalore', 12.981222, 77.646495, 'Food & Grocery', 'free', 3.3, 55, ''),
    ("A Naidu's Ootada Mane", 'Indiranagar, Bangalore', 12.982568, 77.63937, 'Food & Grocery', 'platinum', 4.8, 2882, ''),
    ('Oven Springs', '194, 7th Cross Road, Indiranagar, Bangalore', 12.978976, 77.639655, 'Food & Grocery', 'free', 3.1, 65, ''),
    ('Deivee', '/458, 2nd cross, 7th main, Indrangar 2nd stage, Bangalore 560038, Indiranagar, Bangalore', 12.971562, 77.635268, 'Clothing', 'silver', 4.0, 397, ''),
    ('Fame Balaji', '2nd cross, Indiranagar, Bangalore', 12.978176, 77.64295, 'Health & Wellness', 'gold', 4.6, 574, ''),
    ('Saapaad', 'Indiranagar, Bangalore', 12.981203, 77.636904, 'Food & Grocery', 'silver', 3.9, 266, ''),
    ('A S General Stores', 'Indiranagar, Bangalore', 12.982702, 77.634677, 'Food & Grocery', 'silver', 4.0, 187, ''),
    ('Happy Belly Bakes', 'Sri Krishna Temple Road, Indiranagar, Bangalore', 12.979014, 77.642347, 'Food & Grocery', 'gold', 4.1, 1069, ''),
    ('Copper + Cloves @ the organic world', '12th Main, 7th Cross Rd, HAL 2nd Stage, Indiranagar, Indiranagar, Bangalore', 12.970476, 77.645067, 'Food & Grocery', 'free', 3.3, 44, '+919731930303'),
    ('Ainmane', '420, 9th Main Road, Indiranagar, Bangalore', 12.971937, 77.641916, 'Food & Grocery', 'bronze', 3.4, 61, ''),
    ('Salt Mango Tree', 'Indiranagar, Bangalore', 12.970404, 77.639913, 'Food & Grocery', 'free', 2.9, 44, ''),
    ('Moriyo Artisanal Linen', 'Indiranagar, Bangalore', 12.970516, 77.639056, 'Clothing', 'bronze', 3.5, 170, ''),
    ('Dr. Karishma Cosmetics', 'Indiranagar, Bangalore', 12.970547, 77.639547, 'Beauty & Personal Care', 'gold', 4.0, 1333, ''),
    ('Maison du', 'Indiranagar, Bangalore', 12.97094, 77.640244, 'Food & Grocery', 'gold', 4.4, 1059, ''),
    ('Fullyfilled', 'Indiranagar, Bangalore', 12.976174, 77.638866, 'Food & Grocery', 'free', 3.0, 49, ''),
    ('iConnect Care Technology', '6th Cross Road, 290, Chinmaya Mission Hospital Road, 1st Stage, Indiranagar, Indiranagar, Bangalore', 12.978656, 77.640309, 'Electronics', 'platinum', 4.8, 3451, '+917676400900'),
    ('Wakame', 'Indiranagar, Bangalore', 12.969949, 77.639492, 'Food & Grocery', 'silver', 4.2, 482, ''),
    ('Bhola & Blonde', 'Indiranagar, Bangalore', 12.969924, 77.639363, 'Food & Grocery', 'free', 2.9, 11, ''),
    ('Secret Story Bar & Kitchen', 'Indiranagar, Bangalore', 12.969875, 77.639205, 'Food & Grocery', 'silver', 3.5, 152, ''),
    ('Indiranagar Social', 'Indiranagar, Bangalore', 12.970764, 77.647581, 'Food & Grocery', 'free', 3.2, 53, ''),
    ('Bohemians', 'Indiranagar, Bangalore', 12.969907, 77.639494, 'Food & Grocery', 'gold', 4.2, 580, ''),
    ('Umesh Refreshments', 'Indiranagar, Bangalore', 12.968483, 77.640951, 'Food & Grocery', 'free', 2.7, 2, ''),
    ("Thin Tony's Pizza", 'Indiranagar, Bangalore', 12.970241, 77.639087, 'Food & Grocery', 'bronze', 3.3, 193, ''),
    ('Vegan Vogue', 'Indiranagar, Bangalore', 12.971271, 77.64751, 'Food & Grocery', 'free', 3.5, 64, ''),
    ('Outpost Kitchen', 'Indiranagar, Bangalore', 12.970512, 77.63915, 'Food & Grocery', 'free', 2.8, 55, ''),
    ('Avoke: The Avocado Cafe', 'Indiranagar, Bangalore', 12.969894, 77.638637, 'Food & Grocery', 'free', 2.9, 71, ''),
    ('infinitea', 'Indiranagar, Bangalore', 12.969035, 77.636166, 'Food & Grocery', 'free', 3.0, 43, ''),
    ('Labonel Fine Baking', 'Indiranagar, Bangalore', 12.970135, 77.643369, 'Food & Grocery', 'silver', 4.2, 444, ''),
    ('MisoSexy', 'Indiranagar, Bangalore', 12.969967, 77.639893, 'Food & Grocery', 'gold', 4.0, 409, ''),
    ('Kailash Parbat', 'Indiranagar, Bangalore', 12.971533, 77.647456, 'Food & Grocery', 'gold', 4.6, 1337, ''),
    ("Eddy's Cafe", '314, 6th Main Road, Indiranagar, Bangalore', 12.973846, 77.645071, 'Food & Grocery', 'silver', 4.1, 274, '+916361292968'),
    ('Surabhi Salon', 'Indiranagar, Bangalore', 12.973395, 77.64561, 'Beauty & Personal Care', 'silver', 4.4, 436, ''),
    ('Bologna', 'Indiranagar, Bangalore', 12.972041, 77.6409, 'Food & Grocery', 'free', 2.8, 78, ''),
    ('Charlie Chaplain La Vegano', 'Indiranagar, Bangalore', 12.970301, 77.636099, 'Food & Grocery', 'silver', 4.3, 102, ''),
    ('Cafe Noir', 'Indiranagar, Bangalore', 12.970107, 77.636261, 'Food & Grocery', 'gold', 3.7, 315, ''),
    ('Zama', '303, 100 Feet Road, Indiranagar, Bangalore', 12.98029, 77.640548, 'Food & Grocery', 'free', 2.8, 26, ''),
    ('Baking Bad', 'Indiranagar, Bangalore', 12.979019, 77.643803, 'Food & Grocery', 'gold', 3.9, 698, ''),
    ('New Fruit Land', 'Indiranagar, Bangalore', 12.977924, 77.642696, 'Food & Grocery', 'silver', 4.2, 305, ''),
    ('Dofu', 'Indiranagar, Bangalore', 12.980126, 77.640597, 'Food & Grocery', 'silver', 3.5, 381, ''),
    ('Paper & Pie', 'Indiranagar, Bangalore', 12.980973, 77.641004, 'Food & Grocery', 'bronze', 3.8, 51, ''),
    ('Spa Nation', 'Indiranagar, Bangalore', 12.968996, 77.640571, 'Beauty & Personal Care', 'gold', 4.1, 951, ''),
    ('Nava Spa and Salon', 'Indiranagar, Bangalore', 12.975944, 77.639325, 'Beauty & Personal Care', 'silver', 3.4, 477, ''),
    ('Machleez sea food', 'Indiranagar, Bangalore', 12.973271, 77.635996, 'Food & Grocery', 'gold', 4.0, 1134, ''),
    ('Travellers Bungalow', 'Indiranagar, Bangalore', 12.970229, 77.645815, 'Food & Grocery', 'free', 3.0, 53, ''),
    ('Burgerman', 'Indiranagar, Bangalore', 12.970155, 77.637143, 'Food & Grocery', 'bronze', 3.7, 58, ''),
    ('Chowman', 'Indiranagar, Bangalore', 12.979064, 77.636639, 'Food & Grocery', 'silver', 3.9, 453, ''),
    ('Araku Coffee', 'Indiranagar, Bangalore', 12.969914, 77.639208, 'Food & Grocery', 'free', 3.5, 52, ''),
    ('Bergamot', 'Indiranagar, Bangalore', 12.981247, 77.646237, 'Food & Grocery', 'gold', 4.6, 1845, ''),
    ('German BrezelHaus', 'Koramangala, Bangalore', 12.98246, 77.638451, 'Food & Grocery', 'bronze', 3.7, 27, '+916363785582'),
    ('Cafe Terra', 'Indiranagar, Bangalore', 12.979878, 77.639358, 'Food & Grocery', 'bronze', 3.8, 152, ''),
    ('Bento Bento', 'Indiranagar, Bangalore', 12.97793, 77.636486, 'Food & Grocery', 'gold', 3.8, 744, ''),
    ('Yo Colombo', 'Indiranagar, Bangalore', 12.977869, 77.636389, 'Food & Grocery', 'bronze', 3.8, 131, ''),
    ('Honore', 'Indiranagar, Bangalore', 12.977718, 77.63699, 'Food & Grocery', 'free', 3.5, 19, ''),
    ('Copper + Cloves', '549A, 9th A Main Road, Indiranagar, Bangalore', 12.977852, 77.637093, 'Food & Grocery', 'gold', 3.8, 1393, ''),
    ('No Nasties', '549A, 9th A Main Road, Indiranagar, Bangalore', 12.977794, 77.637146, 'Clothing', 'platinum', 4.8, 4758, ''),
    ('Dal Rotti Sabji', 'Indiranagar, Bangalore', 12.980986, 77.64621, 'Food & Grocery', 'platinum', 4.4, 2993, ''),
    ('Paratha Plaza', '5th Main Road, Indiranagar, Bangalore', 12.980119, 77.64048, 'Food & Grocery', 'silver', 3.7, 226, '+919740258348'),
    ('HumbleBean Coffee', '3165, 12th Cross Road, Indiranagar, Bangalore', 12.969133, 77.635859, 'Food & Grocery', 'free', 3.0, 67, ''),
    ('Ganesh Juice Center', '80 Feet Road, Indiranagar, Bangalore', 12.980998, 77.64621, 'Food & Grocery', 'silver', 3.5, 256, ''),
    ('Hokkaido', '10/1, 1st Cross Road, Indiranagar, Bangalore', 12.972362, 77.637045, 'Food & Grocery', 'bronze', 3.2, 159, ''),
    ('NR Bombay Tailors', 'Indiranagar, Bangalore', 12.977357, 77.639519, 'Clothing', 'silver', 4.0, 276, ''),
    ('iFix India', '#54, MSK Plaza, 100ft Road, Below Cafe Coffee Day, Indiranagar, Bangalore', 12.975497, 77.641361, 'Electronics', 'silver', 4.3, 450, ''),
    ('Nuage Pâtisserie & Café', '787, 1st Cross Road, Indiranagar, Bangalore', 12.96996, 77.640826, 'Food & Grocery', 'silver', 4.2, 204, '+08048520831'),
    ('Lucky Chan', 'Indiranagar, Bangalore', 12.970514, 77.642792, 'Food & Grocery', 'free', 3.2, 49, '+918296031044'),
    ('Playful Palette Bistro', 'Indiranagar, Bangalore', 12.981311, 77.640526, 'Food & Grocery', 'silver', 4.1, 114, ''),
    ('Klaa Kitchen', '4th Main Road, Indiranagar, Bangalore', 12.980355, 77.63726, 'Food & Grocery', 'bronze', 3.5, 74, ''),
    ('Chitra Bakery', '80 Feet Road, Indiranagar, Bangalore', 12.981109, 77.645991, 'Food & Grocery', 'free', 3.3, 66, ''),
    ("Marvel Men's Beauty Saloon", 'Sri Krishna Temple Road, Indiranagar, Bangalore', 12.979225, 77.642095, 'Beauty & Personal Care', 'bronze', 3.6, 25, ''),
    ('Cafe Muziris', 'Indiranagar, Bangalore', 12.96887, 77.635891, 'Food & Grocery', 'free', 3.5, 75, ''),
    ('Panditji - Ras Bana Rahe', 'Indiranagar, Bangalore', 12.972156, 77.642174, 'Food & Grocery', 'free', 3.5, 38, ''),
    ('Mango', 'Indiranagar, Bangalore', 12.97217, 77.641439, 'Clothing', 'silver', 4.4, 106, ''),
    ("Shambhu's Cafe", 'Sai Baba Mandira Road, Indiranagar, Bangalore', 12.97278, 77.640893, 'Food & Grocery', 'silver', 4.2, 182, ''),
    ('Conçu', 'Indiranagar, Bangalore', 12.969814, 77.637912, 'Food & Grocery', 'silver', 3.8, 74, ''),
    ('Nomad Pizza', 'Indiranagar, Bangalore', 12.970432, 77.643458, 'Food & Grocery', 'silver', 4.3, 279, ''),
    ('Pishon Pizza', 'Indiranagar, Bangalore', 12.974012, 77.645094, 'Food & Grocery', 'free', 3.4, 58, ''),
    ('Angel & Rocket', 'Indiranagar, Bangalore', 12.970064, 77.641084, 'Clothing', 'bronze', 3.2, 168, ''),
    ('Nadira', 'Indiranagar, Bangalore', 12.969736, 77.64109, 'Clothing', 'silver', 4.3, 371, ''),
    ('Café coffee day', 'Indiranagar, Bangalore', 12.975542, 77.64125, 'Food & Grocery', 'silver', 4.2, 169, ''),
    ('Spettacolare', '208, Indiranagar Double Road, Indiranagar, Bangalore', 12.979819, 77.636774, 'Food & Grocery', 'bronze', 3.3, 166, ''),
    ('Atta Galatta', '178, 5th Main Road, Indiranagar, Bangalore', 12.980096, 77.63937, 'Books', 'silver', 4.1, 316, ''),
    ('isle of salon', 'Indiranagar, Bangalore', 12.981903, 77.639243, 'Beauty & Personal Care', 'silver', 3.5, 314, ''),
    ('Sri Sai Paharmacy', 'Indiranagar, Bangalore', 12.978706, 77.638013, 'Health & Wellness', 'silver', 4.2, 234, ''),
    ('Hotel Gowdru', 'Indiranagar, Bangalore', 12.973997, 77.635899, 'Food & Grocery', 'bronze', 3.1, 128, ''),
    ('Backyard Burgers and Grill', 'Indiranagar, Bangalore', 12.974844, 77.641251, 'Food & Grocery', 'gold', 3.8, 227, ''),
    ('Universal Sportzbiz Pvt Ltd', 'Indiranagar, Bangalore', 12.978278, 77.643719, 'Sports', 'bronze', 3.1, 74, ''),
    ('WoW China', 'Indiranagar, Bangalore', 12.980184, 77.646479, 'Food & Grocery', 'bronze', 3.4, 154, ''),
    ('Shraddha Cafe', 'Indiranagar, Bangalore', 12.985729, 77.644488, 'Food & Grocery', 'bronze', 3.6, 121, ''),
    ('Indrani Hotel', 'Indiranagar, Bangalore', 12.982828, 77.634645, 'Food & Grocery', 'gold', 4.3, 1403, ''),
    ('Trust Pharmacy Counter CMH', '726, Chinmaya Mission Hospital Road, Indiranagar, Bangalore', 12.978298, 77.645905, 'Health & Wellness', 'bronze', 3.4, 145, ''),
    ('J24 Brothers Store', 'Indiranagar, Bangalore', 12.9697, 77.638703, 'Food & Grocery', 'silver', 3.8, 133, ''),
    ('W for Women', 'Indiranagar, Bangalore', 12.968476, 77.641176, 'Clothing', 'free', 2.8, 58, ''),
    ('Decathalon', 'Indiranagar, Bangalore', 12.970248, 77.647742, 'Sports', 'free', 3.4, 67, ''),
    ('Mochi crocs', 'Indiranagar, Bangalore', 12.972837, 77.640897, 'Clothing', 'free', 2.9, 60, ''),
    ('Anaia Cafe', 'Indiranagar, Bangalore', 12.977047, 77.640678, 'Food & Grocery', 'free', 2.9, 71, ''),
    ('Bagh', 'Indiranagar, Bangalore', 12.977575, 77.640717, 'Clothing', 'bronze', 4.0, 174, ''),
    ('Anglo American Opticals', 'Indiranagar, Bangalore', 12.97758, 77.64065, 'Health & Wellness', 'gold', 3.8, 856, ''),
    ('Lawrence & Mayo', 'Indiranagar, Bangalore', 12.978653, 77.645535, 'Health & Wellness', 'silver', 3.4, 128, ''),
    ('Medicine House', 'Indiranagar, Bangalore', 12.978658, 77.645398, 'Health & Wellness', 'free', 3.0, 47, ''),
    ('The Hair Square', 'Indiranagar, Bangalore', 12.970345, 77.639917, 'Beauty & Personal Care', 'bronze', 3.5, 56, ''),
    ("Pizza 4P's", '3275/A, 12th Main Road, Indiranagar, Bangalore', 12.970054, 77.636107, 'Food & Grocery', 'free', 3.3, 6, '+918951940444'),
    ('Leon Grill', 'Indiranagar, Bangalore', 12.980475, 77.641051, 'Food & Grocery', 'gold', 4.0, 680, ''),
    ('Mon Cheri', 'Indiranagar, Bangalore', 12.978077, 77.637637, 'Food & Grocery', 'free', 3.5, 62, ''),
    ('Pijja', 'Indiranagar, Bangalore', 12.980087, 77.646118, 'Food & Grocery', 'free', 3.2, 8, ''),
    ('Blue Diamond Hair Dresses', 'Indiranagar, Bangalore', 12.978428, 77.630459, 'Beauty & Personal Care', 'gold', 4.1, 1273, ''),
    ('Saint Gobain Glass', 'Indiranagar, Bangalore', 12.978431, 77.630411, 'Hardware', 'bronze', 3.5, 106, ''),
    ('B.P Bakery', 'Indiranagar, Bangalore', 12.978213, 77.630682, 'Food & Grocery', 'silver', 3.9, 418, ''),
    ('New Krishna Automobiles', 'Indiranagar, Bangalore', 12.978243, 77.630375, 'Automotive', 'gold', 3.8, 383, ''),
    ('DJ Glass Hardware Studio', 'Indiranagar, Bangalore', 12.978255, 77.630359, 'Hardware', 'bronze', 3.5, 135, ''),
    ('Maruti Suzuki Genuine Parts', 'Indiranagar, Bangalore', 12.97854, 77.630379, 'Automotive', 'silver', 3.5, 422, ''),
    ('Eshwar Pharma', 'Indiranagar, Bangalore', 12.978409, 77.630885, 'Health & Wellness', 'bronze', 3.8, 190, '+919164345443'),
    ('Ambur Hot Dum Biriyani', 'Indiranagar, Bangalore', 12.97849, 77.629963, 'Food & Grocery', 'silver', 3.8, 252, ''),
    ('bodycraft', 'Indiranagar, Bangalore', 12.977574, 77.640576, 'Beauty & Personal Care', 'bronze', 3.0, 191, ''),
    ('Looks Salon', 'Indiranagar, Bangalore', 12.974709, 77.641312, 'Beauty & Personal Care', 'gold', 4.4, 1422, ''),
    ('fashiontv Salon', 'Indiranagar, Bangalore', 12.974552, 77.641344, 'Beauty & Personal Care', 'free', 2.9, 3, ''),
    ('Louis Phillippe', 'Indiranagar, Bangalore', 12.97206, 77.640923, 'Clothing', 'silver', 4.2, 412, ''),
    ('Hamleys', 'Indiranagar, Bangalore', 12.971219, 77.641476, 'Toys & Baby', 'bronze', 3.6, 184, ''),
    ('sculpt', 'Indiranagar, Bangalore', 12.970824, 77.640923, 'Beauty & Personal Care', 'silver', 3.6, 366, ''),
    ('Samosa Party', 'Indiranagar, Bangalore', 12.970192, 77.638627, 'Food & Grocery', 'bronze', 3.8, 165, ''),
    ('Superdry', 'Indiranagar, Bangalore', 12.971019, 77.640983, 'Clothing', 'bronze', 3.8, 148, ''),
    ('Taiki', 'Indiranagar, Bangalore', 12.976865, 77.640688, 'Food & Grocery', 'gold', 3.8, 1189, ''),
    ('Sri Brahmalingeshwara Juice And Condiments', 'Indiranagar, Bangalore', 12.97836, 77.631338, 'Food & Grocery', 'free', 2.7, 26, ''),
    ("G.M Men's Beauty Salon", 'Indiranagar, Bangalore', 12.976746, 77.632192, 'Beauty & Personal Care', 'free', 3.4, 74, '+919880912306'),
    ('Miracle Look Beauty Salon', 'Indiranagar, Bangalore', 12.976805, 77.632225, 'Beauty & Personal Care', 'bronze', 3.8, 175, '+918073615071'),
    ('Iyengars Amma Pastries', 'Indiranagar, Bangalore', 12.977012, 77.631938, 'Food & Grocery', 'bronze', 3.6, 145, ''),
    ('Bharath Hardware Center', 'Indiranagar, Bangalore', 12.978347, 77.631489, 'Hardware', 'gold', 3.7, 995, ''),
    ('Sree Srinivasa Glass & Plywoods', 'Indiranagar, Bangalore', 12.97839, 77.630959, 'Hardware', 'free', 2.7, 67, ''),
    ('Bake Decor', 'Indiranagar, Bangalore', 12.978105, 77.634858, 'Food & Grocery', 'bronze', 3.3, 49, ''),
    ('Karnataka Ham Shop', 'Indiranagar, Bangalore', 12.978271, 77.630245, 'Food & Grocery', 'platinum', 4.5, 2837, '+919019083337'),
    ('Mutton Stall', 'Indiranagar, Bangalore', 12.977986, 77.629901, 'Food & Grocery', 'free', 3.0, 11, ''),
    ('Ramson Ladies Tailor', 'Indiranagar, Bangalore', 12.977946, 77.629996, 'Clothing', 'free', 3.7, 21, ''),
    ('Jamie Oliver Kitchen', '950, 12th Main Road, Indiranagar, Bangalore', 12.970291, 77.639354, 'Food & Grocery', 'free', 3.7, 23, ''),
    ('idylll', 'Indiranagar, Bangalore', 12.970501, 77.64473, 'Food & Grocery', 'free', 2.8, 49, ''),
    ('Amma & Appa Idli Stall', 'Indiranagar, Bangalore', 12.976148, 77.639351, 'Food & Grocery', 'free', 2.8, 41, ''),
    ('Ouzo by Fire', '655, 100 Feet Road, Indiranagar, Bangalore', 12.977169, 77.640672, 'Food & Grocery', 'free', 3.6, 15, ''),
    ('Mon Cherie', 'Indiranagar, Bangalore', 12.970121, 77.636871, 'Clothing', 'free', 2.9, 76, ''),
    ('natuf', 'Indiranagar, Bangalore', 12.969146, 77.636128, 'Food & Grocery', 'silver', 3.9, 435, ''),
    ('Kopitiam Lah', '1088, 12th Main Road, Indiranagar, Bangalore', 12.969825, 77.638077, 'Food & Grocery', 'bronze', 3.9, 88, '+917259543888'),
    ('Vanamo', '368, 100 Feet Road, Indiranagar, Bangalore', 12.973204, 77.64144, 'Food & Grocery', 'free', 3.2, 52, ''),
    ('Mezzaluna', 'Indiranagar, Bangalore', 12.977965, 77.635712, 'Food & Grocery', 'free', 2.7, 29, ''),
    ('La Gioia', '1085, 12th Main, Indiranagar, Bangalore', 12.969792, 77.638397, 'Food & Grocery', 'gold', 3.7, 1234, ''),
    ('Neon Market', 'Indiranagar, Bangalore', 12.972767, 77.639759, 'Food & Grocery', 'platinum', 4.7, 2517, ''),
    ('Los Cavos', '769;770, 12th Main Road, Indiranagar, Bangalore', 12.969959, 77.641025, 'Food & Grocery', 'silver', 4.1, 114, '+919620207743'),
    ('The Kind', '2985, 12th Main Road, Indiranagar, Bangalore', 12.970207, 77.644802, 'Food & Grocery', 'bronze', 3.4, 41, '+919035045328'),
    ('colorbar', 'Indiranagar, Bangalore', 12.977722, 77.639031, 'Beauty & Personal Care', 'gold', 4.2, 1703, ''),
    ('Hatsu', 'Indiranagar, Bangalore', 12.969916, 77.63937, 'Clothing', 'platinum', 4.4, 1877, ''),
    ('Brookes Brothers', 'Indiranagar, Bangalore', 12.971043, 77.641057, 'Clothing', 'silver', 3.9, 266, ''),
    ('Cottons Jaipur', 'Indiranagar, Bangalore', 12.96971, 77.636811, 'Clothing', 'platinum', 4.6, 842, ''),
    ('Tovo', 'Indiranagar, Bangalore', 12.98066, 77.641084, 'Food & Grocery', 'free', 3.2, 79, ''),
    ('Muru Muru', '33, 12th Cross Road, Indiranagar, Bangalore', 12.982184, 77.637846, 'Food & Grocery', 'free', 3.7, 0, ''),
    ('Izanagi', '311, 100 Feet Road, Indiranagar, Bangalore', 12.981168, 77.640483, 'Food & Grocery', 'free', 3.6, 34, ''),
    ('Desire Salon', 'Indiranagar, Bangalore', 12.970494, 77.639148, 'Beauty & Personal Care', 'silver', 4.2, 244, ''),
    ('Meyaa', 'Indiranagar, Bangalore', 12.970286, 77.636402, 'Clothing', 'platinum', 4.8, 2242, ''),
    ('Cakes Corner', 'Indiranagar, Bangalore', 12.970389, 77.637358, 'Food & Grocery', 'silver', 3.9, 73, ''),
    ('Koshe Kosha', 'Indiranagar, Bangalore', 12.978104, 77.638, 'Food & Grocery', 'free', 3.2, 8, ''),
    ('Panetteria Bengaluru', '205, Double Road, Indiranagar, Bangalore', 12.979228, 77.636645, 'Food & Grocery', 'bronze', 3.4, 51, ''),
    ('Sri Lakshmi Pork Shop', 'Indiranagar, Bangalore', 12.977323, 77.631453, 'Food & Grocery', 'gold', 4.4, 672, ''),
    ('Tribe Roots', 'Indiranagar, Bangalore', 12.977272, 77.631431, 'Food & Grocery', 'gold', 3.9, 1133, ''),
    ('Crackle Kitchen', '303, 100 Feet Road, Indiranagar, Bangalore', 12.980257, 77.64047, 'Food & Grocery', 'platinum', 4.3, 3306, ''),
    ('Koreyaki', '3274/A, 11th Main Road, Indiranagar, Bangalore', 12.970248, 77.636144, 'Food & Grocery', 'silver', 4.4, 412, ''),
    ('Niva Bakery N Juice', 'Indiranagar, Bangalore', 12.978327, 77.633049, 'Food & Grocery', 'bronze', 3.2, 102, ''),
    ('Xerox & Stationary', 'Indiranagar, Bangalore', 12.978343, 77.633051, 'Stationery', 'bronze', 3.5, 173, ''),
    ('United Glass & Plywood', 'Indiranagar, Bangalore', 12.978105, 77.632054, 'Hardware', 'bronze', 3.9, 54, ''),
    ('Laxmi Hardwares', 'Indiranagar, Bangalore', 12.97812, 77.632, 'Hardware', 'silver', 3.4, 293, ''),
    ('ESS Sanitary Hardware', 'Indiranagar, Bangalore', 12.978141, 77.631764, 'Hardware', 'free', 2.7, 22, ''),
    ('Dhamu Electricals', 'Indiranagar, Bangalore', 12.978139, 77.631774, 'Electronics', 'bronze', 3.9, 200, ''),
    ('The Veg & Veg', '611, 12th Main Road, Indiranagar, Bangalore', 12.97057, 77.645413, 'Food & Grocery', 'bronze', 3.4, 40, ''),
    ('Bumshum - Eco-friendly Baby Store', 'Indiranagar, Bangalore', 12.972261, 77.649674, 'Toys & Baby', 'silver', 4.0, 89, ''),
    ("New Frosty's", '5, 80feet Road, Michael Palya, Indiranagar, Indiranagar, Bangalore', 12.980573, 77.645984, 'Food & Grocery', 'bronze', 3.9, 191, ''),
    ("Neeman's", 'Indiranagar, Bangalore', 12.970124, 77.635795, 'Clothing', 'bronze', 3.1, 116, ''),
    ('Kitchen Ministry', 'Indiranagar, Bangalore', 12.970821, 77.640996, 'Food & Grocery', 'gold', 4.1, 462, ''),
    ('ilumi', 'Indiranagar, Bangalore', 12.980278, 77.640471, 'Food & Grocery', 'gold', 4.5, 117, ''),
    ('Superkicks', '1079, 12th Main Road, Indiranagar, Bangalore', 12.970249, 77.638264, 'Clothing', 'free', 3.1, 24, ''),
    ('Love Me Twice', 'Indiranagar, Bangalore', 12.977791, 77.637102, 'Clothing', 'bronze', 3.2, 25, ''),
    ('Champaca', '549A, 9th A Main Road, Indiranagar, Bangalore', 12.97785, 77.637142, 'Books', 'silver', 4.1, 413, ''),
    ('fcom india', 'No. 12, 2nd Floor, 8th Cross Road - Zain Burj, Indiranagar, Bangalore', 12.982004, 77.639348, 'Clothing', 'bronze', 3.1, 177, '+919108902222'),
    ('Nashville Fried Chicken', 'Indiranagar, Bangalore', 12.976167, 77.6405, 'Food & Grocery', 'silver', 4.2, 163, ''),
    ('Smash Guys', 'Indiranagar, Bangalore', 12.970306, 77.639049, 'Food & Grocery', 'free', 3.2, 46, ''),
    ('Tronic Rolls', 'Indiranagar, Bangalore', 12.971163, 77.634358, 'Food & Grocery', 'free', 3.2, 77, ''),
    ('TruBrew Cafe', '3163, Indiranagar Double Road, Indiranagar, Bangalore', 12.969335, 77.635843, 'Food & Grocery', 'free', 2.9, 50, ''),
    ('The Estate Deli', '3162, Indiranagar Double Road, Indiranagar, Bangalore', 12.969471, 77.635834, 'Food & Grocery', 'free', 3.0, 64, '+916363721915'),
    ('Katana', 'Indiranagar, Bangalore', 12.968809, 77.635952, 'Food & Grocery', 'bronze', 3.6, 188, ''),
    ('The Coffee Theatre', 'Indiranagar, Bangalore', 12.970245, 77.639366, 'Food & Grocery', 'silver', 4.1, 358, ''),
    ('Maiz Mexican Kitchen', 'Indiranagar, Bangalore', 12.970613, 77.644689, 'Food & Grocery', 'silver', 3.5, 115, ''),
    ('Harvest Salad Cafe', 'Indiranagar, Bangalore', 12.970611, 77.644731, 'Food & Grocery', 'silver', 3.5, 341, ''),
    ('Cosmo', 'Indiranagar, Bangalore', 12.978474, 77.639452, 'Health & Wellness', 'gold', 4.6, 1592, ''),
    ('Hunaan', 'Indiranagar, Bangalore', 12.969557, 77.63583, 'Food & Grocery', 'silver', 3.6, 397, ''),
    ('Mai Mai', 'Indiranagar, Bangalore', 12.970615, 77.646846, 'Food & Grocery', 'free', 3.7, 68, ''),
    ('Shree Panjurli Cafe', 'Indiranagar, Bangalore', 12.97845, 77.638061, 'Food & Grocery', 'silver', 4.1, 282, ''),
    ('Pibbles Bakery', 'Indiranagar, Bangalore', 12.979464, 77.636755, 'Food & Grocery', 'free', 3.1, 6, ''),
    ('Casa Fresco', 'Indiranagar, Bangalore', 12.982602, 77.640363, 'Food & Grocery', 'gold', 4.0, 1829, ''),
    ('Revive Janata', 'Indiranagar, Bangalore', 12.977718, 77.630673, 'Health & Wellness', 'platinum', 4.3, 4745, ''),
    ('Circa 11', '957, 12th Main, Indiranagar, Bangalore', 12.97041, 77.640328, 'Food & Grocery', 'bronze', 3.4, 31, ''),
    ('Mountain Bee kombucha Hive', '2nd Main Road, Indiranagar, Bangalore', 12.981259, 77.636247, 'Food & Grocery', 'gold', 3.9, 669, '+918792743820'),
    ('Turtle Matcha', 'Indiranagar, Bangalore', 12.975549, 77.635765, 'Food & Grocery', 'gold', 4.2, 747, ''),
    ('Biryani biryani', 'Indiranagar, Bangalore', 12.98227, 77.639346, 'Food & Grocery', 'free', 2.9, 10, ''),
    ('1966 - The Mumbai Cafe', 'Indiranagar, Bangalore', 12.982284, 77.639574, 'Food & Grocery', 'gold', 4.5, 1610, ''),
    ('Thatha Tea', 'Indiranagar, Bangalore', 12.982043, 77.639575, 'Food & Grocery', 'free', 3.4, 9, ''),
    ('Namma Hallimane', 'Indiranagar, Bangalore', 12.982266, 77.639199, 'Food & Grocery', 'gold', 3.9, 774, ''),
    ('Al Baik Biryani', 'Indiranagar, Bangalore', 12.982192, 77.639192, 'Food & Grocery', 'bronze', 3.6, 44, ''),
    ('Wrapafella', 'Indiranagar, Bangalore', 12.97882, 77.640605, 'Food & Grocery', 'free', 3.1, 3, ''),
    ('Eleven Bakehouse', 'Indiranagar, Bangalore', 12.978948, 77.640675, 'Food & Grocery', 'silver', 4.0, 221, ''),
    ('Sandowitch', 'Indiranagar, Bangalore', 12.978887, 77.640646, 'Food & Grocery', 'gold', 3.9, 1069, ''),
    ('Sharetea', 'Indiranagar, Bangalore', 12.97877, 77.640564, 'Food & Grocery', 'free', 3.0, 6, ''),
    ('Quarter Peter', 'Indiranagar, Bangalore', 12.969675, 77.641085, 'Food & Grocery', 'gold', 4.2, 584, ''),
    ('TEKA', '79, 11th Cross Road, Indiranagar, Bangalore', 12.979836, 77.638443, 'Food & Grocery', 'gold', 4.5, 1368, ''),
    ('Kaori by Chiran', 'Indiranagar, Bangalore', 12.981217, 77.640431, 'Food & Grocery', 'bronze', 3.1, 134, ''),
    ('Granthapura', 'Indiranagar, Bangalore', 12.978076, 77.640313, 'Books', 'silver', 3.5, 81, ''),
    ('Fresh Vegetables & Fruits', 'Indiranagar, Bangalore', 12.978521, 77.629722, 'Food & Grocery', 'gold', 4.1, 1922, ''),
    ('Phurr', 'Indiranagar, Bangalore', 12.97058, 77.645837, 'Food & Grocery', 'gold', 3.8, 361, ''),
    ('Teresita', 'Indiranagar, Bangalore', 12.96996, 77.639225, 'Food & Grocery', 'free', 3.5, 7, ''),
    ('Plente', 'Indiranagar, Bangalore', 12.971049, 77.647529, 'Food & Grocery', 'bronze', 3.6, 36, ''),
    ('Sable Cafe', 'Indiranagar, Bangalore', 12.979444, 77.646652, 'Food & Grocery', 'free', 2.8, 6, ''),
    ('Grumpy Girl Coffee', 'Indiranagar, Bangalore', 12.974919, 77.636264, 'Food & Grocery', 'free', 3.1, 12, ''),
    ('Celio', 'Indiranagar, Bangalore', 12.97305, 77.641426, 'Clothing', 'silver', 3.8, 492, ''),
    ('GK Vale studio', '22 cross Hsr2, HSR Layout, Bangalore', 12.908852, 77.646273, 'Electronics', 'silver', 4.1, 55, ''),
    ('Mom & Me', '17 cross, Hsr3, HSR Layout, Bangalore', 12.912428, 77.639518, 'Toys & Baby', 'bronze', 3.5, 129, ''),
    ('Coffee Bean', 'HSR Layout, Bangalore', 12.912397, 77.640767, 'Food & Grocery', 'gold', 4.3, 795, ''),
    ('MK Supermart', 'HSR Layout, Bangalore', 12.912091, 77.64868, 'Food & Grocery', 'silver', 4.1, 63, ''),
    ('Jharoka', 'HSR Layout, Bangalore', 12.912052, 77.637978, 'Food & Grocery', 'bronze', 3.3, 69, ''),
    ('Kottayam', '23, 14th main, HSR Layout, Bangalore', 12.914355, 77.63832, 'Food & Grocery', 'free', 2.7, 8, ''),
    ('Safa', 'HSR Layout, Bangalore', 12.913486, 77.63745, 'Food & Grocery', 'silver', 4.2, 343, ''),
    ('Tharavad', 'Hosur road, HSR Layout, Bangalore', 12.906263, 77.629835, 'Food & Grocery', 'free', 3.2, 45, ''),
    ('My Baby Store', 'HSR Layout, Bangalore', 12.91648, 77.635197, 'Toys & Baby', 'bronze', 3.7, 133, ''),
    ('Fooddays', 'HSR Layout, Bangalore', 12.915434, 77.632171, 'Food & Grocery', 'free', 2.8, 5, ''),
    ('PPS Motors', 'HSR Layout, Bangalore', 12.916528, 77.6354, 'Automotive', 'free', 3.0, 55, ''),
    ('Addlabs', 'HSR Layout, Bangalore', 12.906731, 77.63117, 'Electronics', 'bronze', 3.7, 111, ''),
    ('Veg Sagar', 'HSR Layout, Bangalore', 12.905602, 77.644496, 'Food & Grocery', 'bronze', 3.9, 57, ''),
    ('Grand Supermarket', 'HSR Layout, Bangalore', 12.906347, 77.644555, 'Food & Grocery', 'free', 3.0, 43, ''),
    ('KidZone', 'HSR Layout, Bangalore', 12.912293, 77.644209, 'Toys & Baby', 'free', 3.7, 66, ''),
    ('Manjunatha Refreshments', 'HSR Layout, Bangalore', 12.917111, 77.633741, 'Food & Grocery', 'silver', 4.1, 79, ''),
    ('Swathi Spice and Ice', 'HSR Layout, Bangalore', 12.916515, 77.635522, 'Food & Grocery', 'free', 3.5, 71, ''),
    ('King Ambur Hot Biriyani', 'HSR Layout, Bangalore', 12.915303, 77.644858, 'Food & Grocery', 'gold', 4.1, 500, ''),
    ('Mahindra First Choice - Genuine Cars', 'HSR Layout, Bangalore', 12.915364, 77.644864, 'Automotive', 'platinum', 4.6, 2148, ''),
    ('Just momo', '17th Cross, HSR Layout, Bangalore', 12.912101, 77.648561, 'Food & Grocery', 'silver', 3.7, 391, ''),
    ('Calvins', 'HSR Layout, Bangalore', 12.912258, 77.637597, 'Food & Grocery', 'bronze', 3.7, 139, ''),
    ('Nandini Cool Joint', 'HSR Layout, Bangalore', 12.911006, 77.644495, 'Food & Grocery', 'silver', 3.7, 346, ''),
    ('Southinn', 'HSR Layout, Bangalore', 12.910561, 77.63269, 'Food & Grocery', 'silver', 3.7, 203, ''),
    ('Aishwarya Supermarket', 'HSR Layout, Bangalore', 12.908347, 77.644691, 'Food & Grocery', 'bronze', 3.2, 136, ''),
    ('Earth Plate', '14, 17th Cross, Sector 7, HSR Layout, Bengaluru, HSR Layout, Bangalore', 12.912059, 77.637908, 'Food & Grocery', 'gold', 4.6, 1115, '+918025724114'),
    ('The Punjabi Rasoi', '1082, 18th Cross, 3rd Sector, HSR, HSR Layout, Bangalore', 12.911827, 77.638433, 'Food & Grocery', 'free', 2.7, 28, '+918025720999'),
    ('Helloo Delhi', '17, 18th Cross Road, Near Reliance Digital, Sector 3, HSR layout, HSR Layout, Bangalore', 12.911789, 77.643267, 'Food & Grocery', 'silver', 3.9, 159, '+918065351717;+918065351818'),
    ('PVR Sports & Trophies', '519, 24th Main Road, HSR Layout, Bangalore', 12.910226, 77.649339, 'Sports', 'free', 3.4, 42, '+919972148887'),
    ('Shoba supermarket', '9th Main Road, HSR Layout, Bangalore', 12.914994, 77.63521, 'Food & Grocery', 'gold', 4.1, 919, '+919448879294'),
    ('Kimera Wellness Spa And Salon', 'Bhagyalakshmi Square, 3rd Floor, 18th Cross, 17th Cross, HSR Layout, Bangalore', 12.911784, 77.643406, 'Beauty & Personal Care', 'free', 3.7, 46, '+919740538447'),
    ('Magic Hands', 'No.806,Pgr Tower, 19th Main, Hsr Layout Sector 2,, Hsr Layout Sector 2, HSR Layout, Bangalore', 12.907824, 77.644769, 'Beauty & Personal Care', 'free', 3.1, 71, '+918033640908'),
    ('Food Palace Supermarket', 'No.1195/1276/82,, Suraamy Plaza, Outer Ring Road Bellandur, Bellandur,, HSR Layout, Bangalore', 12.913818, 77.649175, 'Food & Grocery', 'free', 3.0, 24, '+918033530209'),
    ('Agarwal Bhavan', 'HSR Layout, Bangalore', 12.913249, 77.635144, 'Food & Grocery', 'silver', 4.3, 234, ''),
    ('Hotel Tanish', '1811, 13th Cross Road, HSR Layout, Bangalore', 12.915631, 77.646853, 'Food & Grocery', 'free', 2.7, 25, ''),
    ('Arogya Ahaara Takeaway', '13th Main Road,jakkasandra, 1st Block Koramangala, 5th Sector,HSR Layout, HSR Layout, Bangalore', 12.91806, 77.63786, 'Food & Grocery', 'free', 2.9, 33, ''),
    ('SRK weavers', 'HSR Layout, Bangalore', 12.911944, 77.635348, 'Clothing', 'silver', 3.7, 333, ''),
    ('Srirangam', 'HSR Layout, Bangalore', 12.912168, 77.635394, 'Clothing', 'free', 2.9, 22, ''),
    ('Kapas', 'HSR Layout, Bangalore', 12.912086, 77.647847, 'Clothing', 'gold', 4.6, 142, ''),
    ('Udaan', 'HSR Layout, Bangalore', 12.91167, 77.643421, 'Food & Grocery', 'gold', 3.8, 1441, ''),
    ('Dindigul Thalapakkatti', '1, 17th Cross Road, HSR Layout, Bangalore', 12.912108, 77.637973, 'Food & Grocery', 'silver', 3.5, 157, '+918043781500;+917825890008'),
    ('Pappu Chaiwala', 'HSR Layout, Bangalore', 12.913213, 77.644523, 'Food & Grocery', 'bronze', 3.2, 115, ''),
    ('Rebarbiq', 'HSR Layout, Bangalore', 12.912345, 77.642592, 'Food & Grocery', 'silver', 4.3, 459, ''),
    ('Lassi Treat', 'HSR Layout, Bangalore', 12.916354, 77.644931, 'Food & Grocery', 'platinum', 4.9, 1035, ''),
    ('Meisterwurst German Sausages and Cuts', 'HSR Layout, Bangalore', 12.914337, 77.632578, 'Food & Grocery', 'free', 3.6, 16, ''),
    ('Go Native HSR', '14th Main Road, HSR Layout, Bangalore', 12.91333, 77.638324, 'Food & Grocery', 'silver', 3.6, 243, ''),
    ('Louis Philippe Factory Outlet', 'HSR Layout, Bangalore', 12.912294, 77.633334, 'Clothing', 'silver', 3.7, 269, ''),
    ('The Pet People Cafe', 'HSR Layout, Bangalore', 12.912741, 77.644505, 'Food & Grocery', 'platinum', 4.2, 878, ''),
    ('Annapurna Andhra Veg', 'HSR Layout, Bangalore', 12.91459, 77.639903, 'Food & Grocery', 'silver', 4.0, 487, ''),
    ('Apollo Pharamacy', 'HSR Layout, Bangalore', 12.913357, 77.6491, 'Health & Wellness', 'bronze', 3.8, 103, ''),
    ('Sri Muthanahalli Veg', 'HSR Layout, Bangalore', 12.916607, 77.644593, 'Food & Grocery', 'free', 3.3, 35, ''),
    ('Mirchi Maharaj', 'HSR Layout, Bangalore', 12.912453, 77.638659, 'Food & Grocery', 'gold', 4.6, 1296, ''),
    ('Once Upon a Flame', 'HSR Layout, Bangalore', 12.911853, 77.637909, 'Food & Grocery', 'gold', 3.9, 266, ''),
    ('Gustom', 'HSR Layout, Bangalore', 12.911391, 77.637889, 'Food & Grocery', 'silver', 4.2, 364, ''),
    ('For Gods Sake', 'HSR Layout, Bangalore', 12.911244, 77.638259, 'Food & Grocery', 'free', 3.2, 65, ''),
    ('Chinese Wok', 'HSR Layout, Bangalore', 12.909461, 77.638627, 'Food & Grocery', 'bronze', 3.5, 65, ''),
    ("Hebe's Cafe", 'HSR Layout, Bangalore', 12.909184, 77.640191, 'Food & Grocery', 'bronze', 3.4, 197, ''),
    ('Sanjeevani Medicals', 'HSR Layout, Bangalore', 12.914171, 77.638375, 'Health & Wellness', 'bronze', 4.0, 141, ''),
    ('Utsa', 'HSR Layout, Bangalore', 12.914045, 77.638339, 'Clothing', 'free', 3.1, 74, ''),
    ('Andhra Vilas', 'HSR Layout, Bangalore', 12.914219, 77.644915, 'Food & Grocery', 'silver', 3.6, 153, ''),
    ('Banoffee', 'HSR Layout, Bangalore', 12.914894, 77.638368, 'Food & Grocery', 'bronze', 3.2, 75, ''),
    ('Beanlore', 'HSR Layout, Bangalore', 12.912355, 77.641764, 'Food & Grocery', 'bronze', 3.9, 166, ''),
    ('Le Crostini', 'HSR Layout, Bangalore', 12.911711, 77.641376, 'Food & Grocery', 'free', 3.5, 32, ''),
    ('Sai Ram Hot Chips', '24th Main Road, HSR Layout, Bangalore', 12.91398, 77.649146, 'Food & Grocery', 'silver', 3.5, 93, ''),
    ('HSR Fresh Cane Juice', '25333, 24th Main Rd, Vanganahalli, 1st Sector, HSR Layout 5th Sector, Bengaluru,, HSR Layout, Bangalore', 12.9122, 77.648903, 'Food & Grocery', 'free', 3.2, 29, ''),
    ('CBNB', 'HSR Layout, Bangalore', 12.916761, 77.647116, 'Food & Grocery', 'silver', 3.9, 368, ''),
    ('Nagashree Juice', '24th Main Rd, Vanganahalli, 1st Sector, HSR Layout 5th Sector, Bengaluru,, HSR Layout, Bangalore', 12.913766, 77.648984, 'Food & Grocery', 'bronze', 3.5, 130, ''),
    ('Shree Muthahalli Veg', '19th Main Road, HSR Layout, Bangalore', 12.914841, 77.644626, 'Food & Grocery', 'bronze', 3.8, 101, ''),
    ('Coconut Seller', 'HSR Layout, Bangalore', 12.915607, 77.647289, 'Food & Grocery', 'free', 2.7, 56, ''),
    ('Sri Lakshmi Venkateshwara Papers', 'HSR Layout, Bangalore', 12.915292, 77.647137, 'Food & Grocery', 'free', 3.3, 45, ''),
    ('Green Fresh', 'HSR Layout, Bangalore', 12.90917, 77.639263, 'Food & Grocery', 'gold', 4.0, 1127, ''),
    ('Shades of Coffee', 'HSR Layout, Bangalore', 12.909427, 77.63933, 'Food & Grocery', 'bronze', 3.6, 90, ''),
    ('N K Waris Salon', 'HSR Layout, Bangalore', 12.912479, 77.636396, 'Beauty & Personal Care', 'gold', 3.8, 246, ''),
    ('Raj Tailor', '314, 6th Main Road, HSR Layout, Bangalore', 12.920651, 77.635969, 'Clothing', 'bronze', 3.5, 105, ''),
    ('Yazhi Silks', 'HSR Layout, Bangalore', 12.91241, 77.643787, 'Clothing', 'free', 3.0, 38, ''),
    ('Kapdachar', 'HSR Layout, Bangalore', 12.912311, 77.644082, 'Clothing', 'silver', 4.2, 482, ''),
    ('Ownd', 'HSR Layout, Bangalore', 12.911762, 77.649396, 'Clothing', 'free', 3.4, 17, ''),
    ('Adco', 'HSR Layout, Bangalore', 12.912749, 77.632539, 'Clothing', 'platinum', 4.8, 4949, ''),
    ('Sufiyan Dates and Nuts', 'HSR Layout, Bangalore', 12.914481, 77.632554, 'Food & Grocery', 'gold', 3.7, 713, ''),
    ('Fitwell - Car Seat Cover Makers', 'HSR Layout, Bangalore', 12.913426, 77.632509, 'Automotive', 'free', 3.0, 51, ''),
    ('Titan Eyeplus', 'HSR Layout, Bangalore', 12.913181, 77.632285, 'Health & Wellness', 'gold', 4.6, 1783, ''),
    ('Khadims', 'HSR Layout, Bangalore', 12.912582, 77.633006, 'Clothing', 'silver', 4.1, 291, ''),
    ('Eye World Optician', 'HSR Layout, Bangalore', 12.912585, 77.632563, 'Health & Wellness', 'silver', 4.4, 126, ''),
    ('Easyday Club', 'HSR Layout, Bangalore', 12.914311, 77.632217, 'Food & Grocery', 'gold', 3.9, 1581, ''),
    ('Atthemane Restaurants', 'HSR Layout, Bangalore', 12.913956, 77.632201, 'Food & Grocery', 'silver', 3.9, 314, ''),
    ('Curries N Fries', 'HSR Layout, Bangalore', 12.914491, 77.632927, 'Food & Grocery', 'silver', 4.0, 289, ''),
    ('Axion Computers', 'HSR Layout, Bangalore', 12.915999, 77.632174, 'Electronics', 'silver', 3.9, 455, ''),
    ('Celebrations Bake', 'HSR Layout, Bangalore', 12.914442, 77.632947, 'Food & Grocery', 'free', 3.5, 54, ''),
    ('The Smoke Store', 'HSR Layout, Bangalore', 12.915359, 77.632193, 'Food & Grocery', 'silver', 4.1, 157, ''),
    ('Blissclub', 'HSR Layout, Bangalore', 12.914697, 77.632248, 'Clothing', 'bronze', 3.9, 27, ''),
    ('Tawa Ghar Ka', 'HSR Layout, Bangalore', 12.915267, 77.632184, 'Food & Grocery', 'bronze', 3.6, 52, ''),
    ('A Arun Vegetables and Fruits', 'HSR Layout, Bangalore', 12.91731, 77.631771, 'Food & Grocery', 'bronze', 3.7, 147, ''),
    ('Copper Kitchen', 'HSR Layout, Bangalore', 12.914952, 77.632197, 'Food & Grocery', 'gold', 4.5, 795, ''),
    ('Sporto', 'HSR Layout, Bangalore', 12.915091, 77.632193, 'Clothing', 'free', 3.1, 51, ''),
    ('Hair n Shape', 'HSR Layout, Bangalore', 12.917255, 77.631759, 'Beauty & Personal Care', 'silver', 4.1, 203, ''),
    ('Lassi n Snacks', 'HSR Layout, Bangalore', 12.917447, 77.631797, 'Food & Grocery', 'bronze', 3.0, 28, ''),
    ('Express Hypermarket', 'HSR Layout, Bangalore', 12.917022, 77.631602, 'Food & Grocery', 'gold', 3.9, 776, ''),
    ('Slurp it Soup Store', 'HSR Layout, Bangalore', 12.917991, 77.631929, 'Food & Grocery', 'silver', 4.2, 361, ''),
    ('Sagar Stationery', 'HSR Layout, Bangalore', 12.918265, 77.632111, 'Stationery', 'silver', 3.8, 245, ''),
    ('Babai Tiffins HSR High Street', 'HSR Layout, Bangalore', 12.912748, 77.635114, 'Food & Grocery', 'free', 3.1, 68, ''),
    ('Indian Sweet House', 'HSR Layout, Bangalore', 12.912818, 77.635109, 'Food & Grocery', 'bronze', 3.5, 104, ''),
    ('Quickbites', 'HSR Layout, Bangalore', 12.912794, 77.635115, 'Food & Grocery', 'silver', 3.9, 205, ''),
    ('Mr. Shawarma & Miss Grills', 'HSR Layout, Bangalore', 12.912771, 77.635113, 'Food & Grocery', 'free', 2.8, 70, ''),
    ('Fab.to.lab', '525, 2nd A Main Road, HSR Layout, Bangalore', 12.91345, 77.628668, 'Electronics', 'free', 3.5, 77, '+916361942383'),
    ('Raghavendra Tiffins', 'HSR Layout, Bangalore', 12.912755, 77.635386, 'Food & Grocery', 'bronze', 3.2, 37, ''),
    ('Honey Special Cake', 'HSR Layout, Bangalore', 12.912518, 77.635356, 'Food & Grocery', 'gold', 4.1, 521, ''),
    ('Teatings', 'HSR Layout, Bangalore', 12.913013, 77.635407, 'Food & Grocery', 'free', 3.4, 8, '+919666836335'),
    ('Aromas of Biryani', 'HSR Layout, Bangalore', 12.913358, 77.635162, 'Food & Grocery', 'bronze', 3.4, 22, ''),
    ('Anands Kitchen', 'HSR Layout, Bangalore', 12.908656, 77.64459, 'Food & Grocery', 'free', 3.6, 52, ''),
    ('Juicy Fresh', 'HSR Layout, Bangalore', 12.912742, 77.635356, 'Food & Grocery', 'bronze', 4.0, 53, ''),
    ('Litti Chokha corner', 'HSR Layout, Bangalore', 12.912839, 77.635363, 'Food & Grocery', 'bronze', 3.5, 37, ''),
    ('Fruit Bae', 'HSR Layout, Bangalore', 12.910822, 77.63794, 'Food & Grocery', 'bronze', 3.9, 72, ''),
    ('Coffeetales', 'HSR Layout, Bangalore', 12.912059, 77.647963, 'Food & Grocery', 'free', 2.9, 55, ''),
    ('My Affair with food', 'HSR Layout, Bangalore', 12.906898, 77.64417, 'Food & Grocery', 'gold', 4.4, 425, ''),
    ('Basil Bistro', 'HSR Layout, Bangalore', 12.915512, 77.648912, 'Food & Grocery', 'silver', 4.3, 350, ''),
    ('Miss Misal', 'HSR Layout, Bangalore', 12.915397, 77.649163, 'Food & Grocery', 'free', 3.4, 58, ''),
    ('Rush Pharma', 'HSR Layout, Bangalore', 12.908993, 77.635169, 'Health & Wellness', 'free', 2.7, 18, ''),
    ('iTouch Restore', 'HSR Layout, Bangalore', 12.908863, 77.649463, 'Electronics', 'bronze', 3.8, 174, ''),
    ('HSR Juice Point', 'HSR Layout, Bangalore', 12.909061, 77.649455, 'Food & Grocery', 'gold', 4.2, 1964, ''),
    ('Fish Mart', 'HSR Layout, Bangalore', 12.916558, 77.635631, 'Food & Grocery', 'silver', 3.9, 286, ''),
    ('New Udupi Grand', 'HSR Layout, Bangalore', 12.917045, 77.629909, 'Food & Grocery', 'silver', 4.1, 197, ''),
    ("LJ Iyengar's Bakery", 'HSR Layout, Bangalore', 12.908986, 77.649168, 'Food & Grocery', 'silver', 4.3, 227, ''),
    ("King's & Queens's Premium Salon", 'HSR Layout, Bangalore', 12.908818, 77.649498, 'Beauty & Personal Care', 'silver', 4.1, 202, ''),
    ('Ganpati Collection', 'HSR Layout, Bangalore', 12.908971, 77.649501, 'Clothing', 'bronze', 3.8, 64, ''),
    ('New Fashion Ladies Tailor', 'HSR Layout, Bangalore', 12.908989, 77.649501, 'Clothing', 'bronze', 3.2, 178, ''),
    ('Aster Pharmacy', 'HSR Layout, Bangalore', 12.910827, 77.64925, 'Health & Wellness', 'silver', 3.9, 387, ''),
    ('Imperio', 'HSR Layout, Bangalore', 12.911711, 77.649737, 'Food & Grocery', 'free', 3.3, 50, ''),
    ('Iris cafe HSR', 'HSR Layout, Bangalore', 12.911746, 77.642802, 'Food & Grocery', 'silver', 3.4, 495, ''),
    ('Ratnadeep supermarket', 'HSR Layout, Bangalore', 12.911838, 77.642169, 'Food & Grocery', 'free', 3.6, 68, ''),
    ('Arogya Aahara', '4, 14th Main Rd, next to HDFC Bank, Jakkasandra, Sector 5, HSR Layout, HSR Layout, Bangalore', 12.916823, 77.63804, 'Food & Grocery', 'free', 3.6, 27, ''),
    ('Gochick', 'HSR Layout, Bangalore', 12.911768, 77.649564, 'Food & Grocery', 'gold', 4.1, 1660, ''),
    ('Style Up', 'HSR Layout, Bangalore', 12.911694, 77.649794, 'Clothing', 'silver', 4.4, 89, ''),
    ('Greenmart', 'HSR Layout, Bangalore', 12.911445, 77.649262, 'Food & Grocery', 'silver', 3.9, 449, ''),
    ('OR Sports', 'HSR Layout, Bangalore', 12.908993, 77.649518, 'Sports', 'bronze', 3.5, 84, ''),
    ('Optical Corner', 'HSR Layout, Bangalore', 12.910418, 77.649088, 'Health & Wellness', 'free', 2.7, 3, ''),
    ('Amma Pastries', 'HSR Layout, Bangalore', 12.910666, 77.649334, 'Food & Grocery', 'free', 3.4, 59, ''),
    ('House Of Dakshina', 'HSR Layout, Bangalore', 12.912132, 77.649365, 'Food & Grocery', 'bronze', 3.7, 69, ''),
    ("Green's Unisex Salon", 'HSR Layout, Bangalore', 12.908652, 77.649216, 'Beauty & Personal Care', 'bronze', 3.0, 49, ''),
    ('Wax Touch', 'HSR Layout, Bangalore', 12.915638, 77.649145, 'Beauty & Personal Care', 'silver', 3.5, 393, ''),
    ('HSR Donne Biryani', 'HSR Layout, Bangalore', 12.914695, 77.648901, 'Food & Grocery', 'silver', 3.9, 84, ''),
    ('Farm2fresh', 'HSR Layout, Bangalore', 12.914489, 77.648887, 'Food & Grocery', 'free', 3.4, 56, ''),
    ('Decathlon Connect', 'HSR Layout, Bangalore', 12.91753, 77.644685, 'Sports', 'platinum', 4.1, 2786, ''),
    ('Le Kéne', 'HSR Layout, Bangalore', 12.915498, 77.644592, 'Food & Grocery', 'gold', 3.9, 136, ''),
    ('Mylapore Cafe', 'HSR Layout, Bangalore', 12.918031, 77.638177, 'Food & Grocery', 'silver', 3.6, 113, ''),
    ('Wagh Bakri Tea Lounge', 'HSR Layout, Bangalore', 12.911347, 77.638256, 'Food & Grocery', 'bronze', 3.4, 39, ''),
    ("Mani's Dum Biryani", 'HSR Layout, Bangalore', 12.914753, 77.638411, 'Food & Grocery', 'silver', 3.4, 189, ''),
    ('Ganesh Medicals', '24th Main Road, HSR Layout, Bangalore', 12.907546, 77.649034, 'Health & Wellness', 'free', 3.1, 27, ''),
    ('Galatta', 'HSR Layout, Bangalore', 12.915076, 77.649141, 'Food & Grocery', 'silver', 4.3, 322, ''),
    ('North Star', 'HSR Layout, Bangalore', 12.911755, 77.638978, 'Food & Grocery', 'free', 3.4, 66, ''),
    ('Kesari', 'HSR Layout, Bangalore', 12.911801, 77.639029, 'Food & Grocery', 'silver', 4.1, 102, ''),
    ('Nammura Banasiga', "13th Cross Road, Teacher's Colony 5th Sector, Opposite Arogya Ahar takeaway, Jakkasandra, 1st Block Koramangala, HSR Layout, HSR Layout, Bangalore", 12.917936, 77.637844, 'Food & Grocery', 'bronze', 3.3, 33, ''),
    ('Manju Departmental Store', '51/1, 24th Main Road, HSR Layout, Bangalore', 12.907749, 77.649228, 'Food & Grocery', 'silver', 3.4, 243, ''),
    ('Hustle', 'HSR Layout, Bangalore', 12.909097, 77.642541, 'Food & Grocery', 'silver', 4.3, 241, ''),
    ('HSR Rice Traders', '5/3, 24th Main Road, HSR Layout, Bangalore', 12.907878, 77.649119, 'Food & Grocery', 'gold', 3.9, 132, ''),
    ('Cult.Sports', 'HSR Layout, Bangalore', 12.917792, 77.645042, 'Sports', 'bronze', 4.0, 106, ''),
    ('Hotel Thirthahalli', 'HSR Layout, Bangalore', 12.918121, 77.644732, 'Food & Grocery', 'gold', 4.4, 229, ''),
    ('goodgudi', 'HSR Layout, Bangalore', 12.911759, 77.632345, 'Stationery', 'free', 3.6, 21, ''),
    ("Roshini's Studio", 'HSR Layout, Bangalore', 12.91146, 77.630592, 'Beauty & Personal Care', 'silver', 3.4, 312, ''),
    ('Cuppa Redifined', 'HSR Layout, Bangalore', 12.911975, 77.632328, 'Food & Grocery', 'free', 3.0, 71, ''),
    ('Asha Tiffins', 'HSR Layout, Bangalore', 12.911861, 77.632612, 'Food & Grocery', 'silver', 4.0, 327, ''),
    ('Tea House', 'HSR Layout, Bangalore', 12.910743, 77.632645, 'Food & Grocery', 'bronze', 3.4, 79, ''),
    ('SKP Donne Biriyani', 'HSR Layout, Bangalore', 12.910538, 77.632737, 'Food & Grocery', 'free', 3.1, 22, ''),
    ('Bake N Joy', '1461, 17th cross road, HSR Layout, Bangalore', 12.911548, 77.629783, 'Food & Grocery', 'free', 3.4, 1, ''),
    ('Society', 'HSR Layout, Bangalore', 12.909101, 77.642652, 'Beauty & Personal Care', 'bronze', 3.5, 36, ''),
    ('China Town', 'HSR Layout, Bangalore', 12.915876, 77.63265, 'Food & Grocery', 'silver', 3.7, 369, ''),
    ('Samosa Point', 'HSR Layout, Bangalore', 12.910373, 77.632812, 'Food & Grocery', 'bronze', 3.1, 75, ''),
    ('Cevi', 'HSR Layout, Bangalore', 12.916885, 77.645002, 'Food & Grocery', 'bronze', 3.8, 147, ''),
    ('The  Coffee Brewery', 'HSR Layout, Bangalore', 12.911831, 77.642302, 'Food & Grocery', 'platinum', 4.1, 4892, ''),
    ('Aligargh House', 'HSR Layout, Bangalore', 12.908452, 77.635247, 'Food & Grocery', 'silver', 4.3, 341, ''),
    ('Somras Bar and Kitchen', 'HSR Layout, Bangalore', 12.912168, 77.649531, 'Food & Grocery', 'free', 3.7, 76, ''),
    ('MM Nati Mane', 'HSR Layout, Bangalore', 12.909786, 77.632596, 'Food & Grocery', 'bronze', 3.6, 119, ''),
    ('Bismilla Biriyani', 'HSR Layout, Bangalore', 12.910672, 77.629498, 'Food & Grocery', 'silver', 4.4, 405, ''),
    ('Mara Coffee & Roasters', '1073, 24th A Cross Road, HSR Layout, Bangalore', 12.907903, 77.647524, 'Food & Grocery', 'free', 2.9, 40, '+917353075761'),
    ('Fish fry day', 'HSR Layout, Bangalore', 12.91226, 77.633597, 'Food & Grocery', 'platinum', 4.7, 1053, ''),
    ('Godavari Cafe', 'HSR Layout, Bangalore', 12.915686, 77.63251, 'Food & Grocery', 'bronze', 3.6, 169, ''),
    ('Yewale', 'HSR Layout, Bangalore', 12.908673, 77.646939, 'Food & Grocery', 'bronze', 3.5, 79, ''),
    ('North East Store', 'HSR Layout, Bangalore', 12.905753, 77.638784, 'Food & Grocery', 'bronze', 3.7, 23, ''),
    ('Stories 2.0', 'HSR Layout, Bangalore', 12.915988, 77.632662, 'Food & Grocery', 'free', 3.3, 15, ''),
    ('Dhanraj', 'Sector 3, HSR Layout, HSR Layout, Bangalore', 12.909424, 77.63876, 'Food & Grocery', 'bronze', 3.7, 82, ''),
    ('Puncher shop', 'HSR Layout, Bangalore', 12.919723, 77.643199, 'Automotive', 'silver', 3.5, 241, ''),
    ('Amintiri', 'HSR Layout, Bangalore', 12.911733, 77.644463, 'Food & Grocery', 'bronze', 3.2, 116, ''),
    ('2Day', 'HSR Layout, Bangalore', 12.918217, 77.632109, 'Food & Grocery', 'free', 2.7, 76, ''),
    ('Viba Chic', 'HSR Layout, Bangalore', 12.918196, 77.631977, 'Clothing', 'platinum', 4.4, 3192, ''),
    ('Sri Balaji Provisional Store', 'HSR Layout, Bangalore', 12.917961, 77.632064, 'Food & Grocery', 'free', 3.0, 5, ''),
    ('S. R. Enterprises', 'HSR Layout, Bangalore', 12.918172, 77.632089, 'Clothing', 'bronze', 4.0, 185, ''),
    ('Sri Sai Ram', 'HSR Layout, Bangalore', 12.918007, 77.632065, 'Food & Grocery', 'gold', 4.5, 569, ''),
    ('Face Look', 'HSR Layout, Bangalore', 12.917891, 77.632038, 'Beauty & Personal Care', 'gold', 4.0, 596, ''),
    ('Piro', 'HSR Layout, Bangalore', 12.908878, 77.646112, 'Food & Grocery', 'free', 2.8, 31, ''),
    ('SM Grens', 'HSR Layout, Bangalore', 12.908794, 77.647131, 'Food & Grocery', 'gold', 4.4, 555, ''),
    ('The Snug Cuppa', 'HSR Layout, Bangalore', 12.908112, 77.648657, 'Food & Grocery', 'silver', 3.8, 214, ''),
    ('Harvest Salad Co', 'HSR Layout, Bangalore', 12.90602, 77.644057, 'Food & Grocery', 'free', 3.3, 8, ''),
    ('GC Global Computers', 'HSR Layout, Bangalore', 12.915996, 77.632568, 'Electronics', 'gold', 3.8, 494, ''),
    ('Mast Marathi', 'HSR Layout, Bangalore', 12.911408, 77.644306, 'Food & Grocery', 'free', 3.7, 26, '+918884919043'),
    ('Mannheim', 'HSR Layout, Bangalore', 12.912174, 77.647164, 'Food & Grocery', 'free', 2.9, 70, ''),
    ('Optics Nest', 'HSR Layout, Bangalore', 12.91205, 77.647896, 'Health & Wellness', 'gold', 4.6, 583, ''),
    ('Make Di Hatti', 'HSR Layout, Bangalore', 12.912136, 77.649666, 'Food & Grocery', 'bronze', 3.4, 139, ''),
    ('The Faith Cafe', 'HSR Layout, Bangalore', 12.912439, 77.641951, 'Food & Grocery', 'bronze', 3.7, 82, ''),
    ('Tribes Coffee', 'HSR Layout, Bangalore', 12.908973, 77.64348, 'Food & Grocery', 'bronze', 3.6, 97, ''),
    ('Oyster, Bar & Kitchen', 'HSR Layout, Bangalore', 12.911904, 77.637905, 'Food & Grocery', 'free', 3.1, 25, ''),
    ('Silaa The Garden Cafe', 'HSR Layout, Bangalore', 12.9125, 77.63937, 'Food & Grocery', 'free', 3.5, 10, ''),
    ('Brewtroots Cafe', 'HSR Layout, Bangalore', 12.911888, 77.639285, 'Food & Grocery', 'platinum', 4.4, 3594, ''),
    ('Pizza Hut Delivery', 'HSR Layout, Bangalore', 12.914725, 77.638364, 'Food & Grocery', 'gold', 3.9, 1003, ''),
    ('MyMarket', 'HSR Layout, Bangalore', 12.908902, 77.632657, 'Food & Grocery', 'bronze', 3.6, 130, ''),
    ('NVM Protection', 'HSR Layout, Bangalore', 12.908963, 77.632656, 'Automotive', 'silver', 4.2, 456, ''),
    ("Shopper's Value", 'HSR Layout, Bangalore', 12.909041, 77.632659, 'Clothing', 'bronze', 3.8, 129, ''),
    ("Nishy's Neer Dosa", 'HSR Layout, Bangalore', 12.908825, 77.632668, 'Food & Grocery', 'gold', 4.5, 1987, ''),
    ("Mama Jay's Baking Studio", 'HSR Layout, Bangalore', 12.915856, 77.634353, 'Food & Grocery', 'bronze', 3.5, 161, ''),
    ('Dome Cafe', 'HSR Layout, Bangalore', 12.912167, 77.635789, 'Food & Grocery', 'silver', 3.7, 287, ''),
    ('Babai Tiffins', '8, 18th Main Road, HSR Layout, Bangalore', 12.918248, 77.64473, 'Food & Grocery', 'bronze', 3.3, 163, '+918050000612'),
    ('Tim Hortons', 'HSR Layout, Bangalore', 12.912021, 77.647761, 'Food & Grocery', 'silver', 3.5, 119, ''),
    ('Yo China', '90/3, Outer Ring Road (Opp. Innovative Multiplex), Marathahalli, Bangalore', 12.950455, 77.699925, 'Food & Grocery', 'silver', 3.6, 147, '+918040980666'),
    ('The Coffee Brewery', 'Outer Ring Road, Marathahalli, Bangalore', 12.962267, 77.701464, 'Food & Grocery', 'free', 3.6, 47, ''),
    ('Auto Sound', '95/3, Outer Ring Road, Marathahalli, Bangalore', 12.964742, 77.701796, 'Automotive', 'bronze', 3.9, 121, '+98065838277'),
    ('Infusion Restaurant', '95/3, Outer Ring Road, Marathahalli, Bangalore', 12.965627, 77.701835, 'Food & Grocery', 'platinum', 4.4, 4510, ''),
    ('Happy Mart', 'Varthur Road, Marathahalli, Bangalore', 12.956906, 77.698972, 'Food & Grocery', 'bronze', 4.0, 38, ''),
    ('Lilliput', 'Varthur Road, Marathahalli, Bangalore', 12.956869, 77.698702, 'Clothing', 'bronze', 3.0, 51, ''),
    ('Hitech Communication', 'Marathahalli, Bangalore', 12.953113, 77.699804, 'Electronics', 'bronze', 3.6, 69, ''),
    ('Citizen Bakery', 'Marathahalli, Bangalore', 12.956518, 77.69793, 'Food & Grocery', 'silver', 4.4, 444, ''),
    ('Megha Restaurant', 'Marathahalli, Bangalore', 12.955452, 77.697866, 'Food & Grocery', 'gold', 4.4, 1143, ''),
    ('Little Flower Bakery', 'Tuasi theater rd, 3rd cross, Marathalli east, Marathahalli, Bangalore', 12.954856, 77.698123, 'Food & Grocery', 'silver', 4.0, 238, ''),
    ('Girias Electronics', 'Marathahalli, Bangalore', 12.952964, 77.700417, 'Electronics', 'silver', 4.1, 267, ''),
    ('Odia Mess', 'Marathahalli, Bangalore', 12.953599, 77.698024, 'Food & Grocery', 'silver', 3.6, 67, ''),
    ("Nizam's Biryani House", 'Marathahalli, Bangalore', 12.960501, 77.701682, 'Food & Grocery', 'gold', 4.1, 1547, ''),
    ('Reliance Mobile Store', 'Marathahalli, Bangalore', 12.961167, 77.701293, 'Electronics', 'silver', 4.1, 380, ''),
    ('Shogun', 'Marathahalli, Bangalore', 12.965162, 77.70172, 'Food & Grocery', 'free', 3.3, 18, ''),
    ('ORR Dhaba', 'Marathahalli, Bangalore', 12.965205, 77.701729, 'Food & Grocery', 'free', 3.3, 20, ''),
    ('New Tandoori Point', 'Marathahalli, Bangalore', 12.956998, 77.703298, 'Food & Grocery', 'free', 3.5, 49, ''),
    ('Minakshi', 'Marathahalli, Bangalore', 12.949786, 77.699076, 'Food & Grocery', 'bronze', 3.6, 40, ''),
    ('Anjappar', 'Marathahalli, Bangalore', 12.950621, 77.699906, 'Food & Grocery', 'bronze', 3.2, 128, ''),
    ('Spice World', 'Marathahalli, Bangalore', 12.950449, 77.69996, 'Food & Grocery', 'free', 3.1, 38, ''),
    ("Jimi's", 'Marathahalli, Bangalore', 12.950495, 77.699942, 'Food & Grocery', 'bronze', 3.5, 117, ''),
    ('Barkaas Indo Arabic Restaurant', 'Marathahalli, Bangalore', 12.951206, 77.699405, 'Food & Grocery', 'silver', 4.1, 435, ''),
    ('Wareabouts Grill & Lounge', 'Marathahalli, Bangalore', 12.951733, 77.699436, 'Food & Grocery', 'gold', 4.4, 282, ''),
    ('New Vishwas', 'Marathahalli, Bangalore', 12.953056, 77.699793, 'Hardware', 'free', 3.3, 44, ''),
    ('Karnataka Home appliances', 'Marathahalli, Bangalore', 12.953029, 77.699787, 'Electronics', 'gold', 4.0, 529, ''),
    ('Chef Bakers Express', 'Marathahalli, Bangalore', 12.952536, 77.700296, 'Food & Grocery', 'gold', 4.1, 730, ''),
    ('Computer Store', 'Marathahalli, Bangalore', 12.952198, 77.700234, 'Electronics', 'free', 2.9, 62, ''),
    ('digiTechh', 'Marathahalli, Bangalore', 12.952158, 77.700225, 'Electronics', 'free', 2.8, 53, ''),
    ('Tandoori Junction', 'Marathahalli, Bangalore', 12.951782, 77.7001, 'Food & Grocery', 'bronze', 3.7, 130, ''),
    ("Nanda's", 'Marathahalli, Bangalore', 12.951588, 77.700071, 'Food & Grocery', 'free', 3.1, 17, ''),
    ('v3 slim care', 'Marathahalli, Bangalore', 12.951569, 77.700049, 'Beauty & Personal Care', 'free', 2.9, 14, ''),
    ('Unilet', 'Marathahalli, Bangalore', 12.951098, 77.700027, 'Electronics', 'bronze', 3.8, 67, ''),
    ('Slim Talk Studio', 'Marathahalli, Bangalore', 12.951056, 77.700008, 'Beauty & Personal Care', 'free', 3.4, 24, ''),
    ('Body Cover', 'Marathahalli, Bangalore', 12.950923, 77.700033, 'Beauty & Personal Care', 'free', 3.6, 52, ''),
    ('New Jagat Associates', 'Marathahalli, Bangalore', 12.957104, 77.700875, 'Electronics', 'bronze', 3.9, 47, ''),
    ('Hot Spot', 'Marathahalli, Bangalore', 12.957095, 77.700903, 'Electronics', 'gold', 3.9, 700, ''),
    ('Grand Vision', 'Marathahalli, Bangalore', 12.957114, 77.700705, 'Health & Wellness', 'free', 2.8, 8, ''),
    ('Veda Optical Gallery', 'Marathahalli, Bangalore', 12.956564, 77.700765, 'Health & Wellness', 'bronze', 3.6, 142, ''),
    ('Nandi Hardware & Sanitaryware', 'Marathahalli, Bangalore', 12.956742, 77.7019, 'Hardware', 'bronze', 3.8, 22, ''),
    ('Eye Life Opticians', 'Marathahalli, Bangalore', 12.957099, 77.701727, 'Health & Wellness', 'free', 3.5, 0, ''),
    ('SmartCafe', 'Marathahalli, Bangalore', 12.957145, 77.700646, 'Electronics', 'silver', 4.2, 71, ''),
    ('Farico', 'Marathahalli, Bangalore', 12.957199, 77.70028, 'Clothing', 'free', 3.2, 0, ''),
    ('The Raymonds Shop', 'Marathahalli, Bangalore', 12.957189, 77.70034, 'Clothing', 'bronze', 3.5, 88, ''),
    ('Srinivasa Enterprises', 'Marathahalli, Bangalore', 12.956657, 77.701389, 'Hardware', 'gold', 4.2, 790, ''),
    ('Satis Tiffins', 'Marathahalli, Bangalore', 12.955736, 77.691838, 'Food & Grocery', 'silver', 3.7, 399, ''),
    ('Bhagini Deluxe', 'Marathahalli, Bangalore', 12.95713, 77.701865, 'Food & Grocery', 'gold', 4.7, 1772, ''),
    ('Vaikuntam', 'Marathahalli, Bangalore', 12.957133, 77.701889, 'Food & Grocery', 'free', 3.1, 51, ''),
    ('Shoba', 'Marathahalli, Bangalore', 12.957114, 77.701818, 'Food & Grocery', 'bronze', 3.1, 111, ''),
    ('Udupi Vaibhava', 'Marathahalli, Bangalore', 12.957292, 77.700917, 'Food & Grocery', 'gold', 4.5, 106, ''),
    ('Lotto', 'Marathahalli, Bangalore', 12.956735, 77.698217, 'Clothing', 'bronze', 3.7, 55, ''),
    ('Fila', 'Marathahalli, Bangalore', 12.956764, 77.698265, 'Clothing', 'free', 2.8, 67, ''),
    ('Proline', 'Marathahalli, Bangalore', 12.956764, 77.698243, 'Clothing', 'free', 3.4, 31, ''),
    ('Gini & Jony Factory Outlet', 'Marathahalli, Bangalore', 12.956769, 77.698303, 'Clothing', 'bronze', 3.6, 188, ''),
    ('Identiti Factory Outlet', 'Marathahalli, Bangalore', 12.95679, 77.698271, 'Clothing', 'gold', 4.2, 1452, ''),
    ('Feet World', 'Marathahalli, Bangalore', 12.956824, 77.698515, 'Clothing', 'free', 3.7, 41, ''),
    ('Woodland Factory Outlet', 'Marathahalli, Bangalore', 12.956845, 77.698632, 'Clothing', 'free', 3.1, 5, ''),
    ('Park Avenue', 'Marathahalli, Bangalore', 12.956849, 77.6986, 'Clothing', 'platinum', 4.7, 4139, ''),
    ('Deepika Pharma and General Stores', 'Marathahalli, Bangalore', 12.956791, 77.698706, 'Health & Wellness', 'free', 3.2, 78, ''),
    ('Blak', 'Marathahalli, Bangalore', 12.956864, 77.698938, 'Clothing', 'silver', 3.7, 293, ''),
    ('Lacoste Outlet', 'Marathahalli, Bangalore', 12.956891, 77.698953, 'Clothing', 'platinum', 4.9, 1322, ''),
    ('Eat Eroo', 'Marathahalli, Bangalore', 12.957461, 77.700883, 'Food & Grocery', 'silver', 3.7, 149, ''),
    ('Jeanne Medicals', 'Marathahalli, Bangalore', 12.957989, 77.700975, 'Health & Wellness', 'silver', 4.3, 498, ''),
    ('Samsung Mobile Service Center', 'Marathahalli, Bangalore', 12.958104, 77.700971, 'Electronics', 'gold', 4.3, 1854, ''),
    ('Kala Karni', 'Marathahalli, Bangalore', 12.957781, 77.700952, 'Clothing', 'silver', 3.4, 304, ''),
    ('Iyengar Food Ex', 'Marathahalli, Bangalore', 12.956208, 77.694667, 'Food & Grocery', 'silver', 3.7, 394, ''),
    ('Nobel Chemist', 'Marathahalli, Bangalore', 12.956202, 77.694499, 'Health & Wellness', 'platinum', 5.0, 4324, ''),
    ('Kumkum India', 'Marathahalli, Bangalore', 12.956208, 77.694649, 'Clothing', 'free', 3.1, 14, ''),
    ('Sri Lakshmi Medical Stores', 'Marathahalli, Bangalore', 12.956214, 77.694598, 'Health & Wellness', 'bronze', 3.5, 82, ''),
    ('Madurai Cafe', 'Marathahalli, Bangalore', 12.959009, 77.701692, 'Food & Grocery', 'free', 3.5, 11, ''),
    ('Tabla Restaurant', '1, Soul space Paradigm, Marathahalli, Bangalore', 12.951384, 77.699432, 'Food & Grocery', 'free', 3.7, 3, '+918049652740'),
    ('Sanjha Chulha Restaurant', 'Purva Riviera Shopping Complex,\xa0Marathahalli, Marathahalli, Bangalore', 12.957346, 77.707593, 'Food & Grocery', 'free', 3.6, 49, '+919686678120'),
    ('Thalassery Restaurant', 'Chandra Layout, Marathahalli, Bengaluru,, Marathahalli, Bangalore', 12.956661, 77.702657, 'Food & Grocery', 'silver', 3.6, 181, '+918032713030'),
    ('Absolute Barbecue', '90/4, Marathahalli Outer Ring Road, Bengaluru, Marathahalli, Bangalore', 12.94987, 77.699077, 'Food & Grocery', 'silver', 4.4, 393, '+918088922203'),
    ('Shanmukha Restaurant', '93/7, Near Home Town, Outer Ring Rd, Marathahalli, Bengaluru, Marathahalli, Bangalore', 12.955538, 77.700421, 'Food & Grocery', 'free', 2.8, 68, '+918042287405'),
    ('Nandhana Palace', 'Marathahalli, Bangalore', 12.949091, 77.699658, 'Food & Grocery', 'silver', 3.9, 457, '+918064444455'),
    ('Mathru Homoeo Medical', '69, 5th Cross,Outer Ring Road, Marathahalli, Behind Venus Bakery Opposite Kalamandir, Marathahalli, Bangalore', 12.960102, 77.700286, 'Health & Wellness', 'silver', 4.3, 344, '+918025400618'),
    ('Sri Tulsi Medicals General Store', '130, 2nd Main, 7th Cross, 6th Cross Road, Aswath Nagar, Marathahalli,Karnataka, Marathahalli, Bangalore', 12.958796, 77.702692, 'Health & Wellness', 'silver', 4.0, 379, '+918861061489'),
    ('Greenhouse Pharma', 'Tulasi Theatre Road, Marathahalli Village, Marathahalli, Karnataka, Marathahalli, Bangalore', 12.953955, 77.697931, 'Health & Wellness', 'bronze', 3.9, 117, '+917353848217'),
    ('Lions Airport City Medicals', '103p, Munnekolala Varthur Road, Marathahalli, Bangalore', 12.955785, 77.705085, 'Health & Wellness', 'silver', 4.2, 488, '+919060448812'),
    ('Delhi Walle', 'Next to US Pizza, Ring Road,Marathahalli, Marathahalli, Bangalore', 12.961794, 77.701346, 'Food & Grocery', 'bronze', 3.2, 86, '+918064539513'),
    ('Punjabi Dawat', 'SGR Dental, College Road, Munnekollal,\xa0Marathahalli, Marathahalli, Bangalore', 12.949953, 77.700234, 'Food & Grocery', 'free', 2.8, 11, '+917338330821'),
    ('Malgudi Southern Cuisine', '3, Marathahalli-Sarjapur Outer Ring Road, Marathahalli, Marathahalli, Bangalore', 12.965119, 77.701727, 'Food & Grocery', 'bronze', 3.6, 68, '+918025401351'),
    ('WheelSports', '280, Chinnappanahalli Road, Marathahalli, Bangalore', 12.961217, 77.704243, 'Sports', 'silver', 3.5, 238, '+918861564439'),
    ('Ma Green Fresh', '9th main, Tulsi Theatre Road, Marathahalli, Bangalore', 12.952397, 77.697957, 'Food & Grocery', 'free', 3.0, 48, '+91(91)8025401309'),
    ('Food Mart Supermarket', 'Varthur Road, Marathahalli, Bangalore', 12.955836, 77.69308, 'Food & Grocery', 'silver', 3.4, 81, '+91(91)8041264215'),
    ('Sports365.in', 'JCR Tower, 1st A Main Road, Aswath Nagar, Chinnapanna Halli, Anand Nagar, Aswath Nagar, Marathahalli, Bangalore', 12.965078, 77.702508, 'Sports', 'silver', 3.6, 248, '+918065555956'),
    ('Mahadev Medical and General Store', 'Marathahalli, Bangalore', 12.969186, 77.693647, 'Health & Wellness', 'platinum', 4.3, 3490, ''),
    ('Utensils and Private Cylender Shop', 'Marathahalli, Bangalore', 12.969114, 77.693623, 'Food & Grocery', 'silver', 4.3, 114, ''),
    ('Iris Eye Care', 'Marathahalli, Bangalore', 12.968948, 77.693604, 'Health & Wellness', 'bronze', 3.6, 76, ''),
    ('Max Shoppee', 'Marathahalli, Bangalore', 12.968798, 77.693571, 'Food & Grocery', 'free', 2.7, 58, ''),
    ('Momo Center', 'Marathahalli, Bangalore', 12.968777, 77.693564, 'Food & Grocery', 'bronze', 3.8, 95, ''),
    ('Gopal Dhaba', 'Marathahalli, Bangalore', 12.969003, 77.693499, 'Food & Grocery', 'gold', 4.2, 1033, ''),
    ('Sri Venkat Satyanarayana Tedared', 'Marathahalli, Bangalore', 12.95467, 77.702778, 'Food & Grocery', 'free', 3.0, 4, ''),
    ("Bancharam's", 'Varthur Road, Marathahalli, Bangalore', 12.955652, 77.691617, 'Food & Grocery', 'silver', 3.7, 402, ''),
    ('Rolls on Wheels', 'Marathahalli, Bangalore', 12.962024, 77.701373, 'Food & Grocery', 'free', 3.7, 73, ''),
    ('KLM Fashion Mall', 'Marathahalli, Bangalore', 12.956771, 77.700771, 'Food & Grocery', 'bronze', 3.8, 146, '+918043746553'),
    ('Brand Factory', 'Marathahalli, Bangalore', 12.95652, 77.697688, 'Food & Grocery', 'free', 3.7, 7, ''),
    ('Green Mantra, Online zero waste store', 'Rohan Vasantha Apartment, Marathahalli, Bangalore', 12.958313, 77.70671, 'Food & Grocery', 'platinum', 4.3, 2225, ''),
    ('Maya Clinic Pharmacy', 'Marathahalli, Bangalore', 12.969016, 77.693624, 'Health & Wellness', 'gold', 4.6, 560, ''),
    ('Kabab Mirchi', 'Marathahalli, Bangalore', 12.95564, 77.691885, 'Food & Grocery', 'silver', 3.5, 493, ''),
    ('SLR Tailors', 'Marathahalli, Bangalore', 12.969657, 77.696216, 'Clothing', 'silver', 4.3, 422, ''),
    ('Gopis Stores', 'Marathahalli, Bangalore', 12.969635, 77.697157, 'Food & Grocery', 'bronze', 4.0, 188, ''),
    ('Vishal Opticals', 'Marathahalli, Bangalore', 12.956388, 77.696015, 'Health & Wellness', 'gold', 3.9, 144, ''),
    ('Lenovo Service Center', 'Marathahalli, Bangalore', 12.95643, 77.696283, 'Electronics', 'bronze', 3.3, 193, ''),
    ('Dell', 'Marathahalli, Bangalore', 12.956448, 77.696317, 'Electronics', 'bronze', 3.3, 120, ''),
    ('Asus Service Center', 'Marathahalli, Bangalore', 12.956455, 77.696352, 'Electronics', 'bronze', 3.5, 92, ''),
    ('Sudarshan Silks and Sarees', 'Marathahalli, Bangalore', 12.956321, 77.696794, 'Clothing', 'free', 3.6, 70, ''),
    ('Unitea Hub Cafe', 'Marathahalli, Bangalore', 12.956562, 77.697236, 'Food & Grocery', 'bronze', 3.1, 168, ''),
    ('Plum Health and Beauty', 'Marathahalli, Bangalore', 12.956809, 77.698454, 'Beauty & Personal Care', 'gold', 3.8, 1553, ''),
    ('Hyderabad House', 'Marathahalli, Bangalore', 12.948784, 77.698844, 'Food & Grocery', 'bronze', 3.7, 167, ''),
    ('Foxtrot Dhaba', 'Marathahalli, Bangalore', 12.949434, 77.69915, 'Food & Grocery', 'free', 3.4, 37, ''),
    ('Carry Mart', 'Marathahalli, Bangalore', 12.954837, 77.697982, 'Food & Grocery', 'gold', 3.9, 1747, ''),
    ('Patanjali Chikitsalaya & Store', 'Marathahalli, Bangalore', 12.958367, 77.700949, 'Food & Grocery', 'silver', 4.1, 54, ''),
    ('Le Arabia', 'Marathahalli, Bangalore', 12.953357, 77.700445, 'Food & Grocery', 'bronze', 3.9, 46, ''),
    ('Kalanikethan', 'Marathahalli, Bangalore', 12.961159, 77.701819, 'Clothing', 'gold', 4.4, 1858, ''),
    ('Ambara Silks', 'Marathahalli, Bangalore', 12.960046, 77.701625, 'Clothing', 'free', 3.7, 54, ''),
    ('Health Care Pharmacy', 'Marathahalli, Bangalore', 12.958566, 77.702813, 'Health & Wellness', 'silver', 3.6, 78, ''),
    ('Sri Balaji Medicals & Surgicals', 'Marathahalli, Bangalore', 12.95924, 77.701565, 'Health & Wellness', 'free', 3.7, 22, ''),
    ('Ramdev Medicrest', 'Marathahalli, Bangalore', 12.959341, 77.7017, 'Health & Wellness', 'gold', 3.8, 1155, ''),
    ('Kundana', 'Marathahalli, Bangalore', 12.959719, 77.702904, 'Food & Grocery', 'silver', 4.0, 126, ''),
    ('The Big Barbeque', '88, Outer Ring Road, Marathahalli, Bangalore', 12.948575, 77.698637, 'Food & Grocery', 'silver', 3.8, 67, '+918880807878'),
    ('Sendhoor', 'Marathahalli, Bangalore', 12.951715, 77.699384, 'Food & Grocery', 'free', 3.1, 38, ''),
    ('Europa 360', 'Marathahalli, Bangalore', 12.950282, 77.698987, 'Food & Grocery', 'silver', 4.4, 384, ''),
    ('Big Baadshaah', 'Marathahalli, Bangalore', 12.948569, 77.698559, 'Food & Grocery', 'silver', 4.2, 457, ''),
    ('La Melano', 'Marathahalli, Bangalore', 12.954945, 77.700311, 'Food & Grocery', 'silver', 3.5, 287, ''),
    ('Manasi Roof Top Restaurant', '223/95, Varthur Road, Marathahalli, Bangalore', 12.956337, 77.695845, 'Food & Grocery', 'gold', 4.4, 663, '+917090955777'),
    ("Mr. Philly's American Cheeseburgers", 'Marathahalli, Bangalore', 12.95064, 77.69999, 'Food & Grocery', 'silver', 3.8, 55, ''),
    ('Sri Udupi Park Cafe', 'Marathahalli, Bangalore', 12.955724, 77.697915, 'Food & Grocery', 'free', 2.8, 62, ''),
    ('The Ramanatheswaram Cafe', 'Marathahalli, Bangalore', 12.951875, 77.700219, 'Food & Grocery', 'silver', 4.3, 165, ''),
    ('Brothers Chemist', '91, Tulsi theater road, Marathahalli, Marathahalli, Bangalore', 12.951511, 77.697874, 'Health & Wellness', 'free', 3.3, 73, ''),
    ('Malnad Spicy', 'Marathahalli, Bangalore', 12.957352, 77.700857, 'Food & Grocery', 'bronze', 3.7, 44, ''),
    ('Jeevika Pharmacy', 'Marathahalli, Bangalore', 12.965855, 77.70251, 'Health & Wellness', 'silver', 3.6, 297, ''),
    ('Kumar Karam Dosa', 'Marathahalli, Bangalore', 12.950982, 77.703444, 'Food & Grocery', 'platinum', 4.7, 2944, ''),
    ('7-Eleven', 'Marathahalli, Bangalore', 12.949421, 77.70164, 'Food & Grocery', 'silver', 3.5, 82, ''),
    ('Ramdev Fashion Zone', 'Marathahalli, Bangalore', 12.950038, 77.700756, 'Clothing', 'silver', 4.3, 325, ''),
    ('Forever 21', 'Marathahalli, Bangalore', 12.956773, 77.70039, 'Clothing', 'free', 3.5, 2, ''),
    ("Helen's Place", 'Marathahalli, Bangalore', 12.950865, 77.700087, 'Food & Grocery', 'bronze', 3.8, 155, ''),
    ('Foot Fashion', 'Marathahalli, Bangalore', 12.954477, 77.700195, 'Clothing', 'bronze', 3.5, 169, ''),
    ('Sri Guntu Ruchulu', 'Marathahalli, Bangalore', 12.954692, 77.700256, 'Food & Grocery', 'bronze', 3.8, 186, ''),
    ('Sanndhi Bakery', 'Marathahalli, Bangalore', 12.95476, 77.700275, 'Food & Grocery', 'free', 3.5, 15, ''),
    ('Fabio', 'Marathahalli, Bangalore', 12.954736, 77.700268, 'Clothing', 'free', 3.3, 23, ''),
    ('Flash Gents Wear', 'Marathahalli, Bangalore', 12.954811, 77.700301, 'Clothing', 'free', 3.1, 40, ''),
    ("Flash Women's Wear", 'Marathahalli, Bangalore', 12.954826, 77.70031, 'Clothing', 'free', 2.8, 26, ''),
    ('ASF - Ancient Stone Flames', 'Marathahalli, Bangalore', 12.951235, 77.699344, 'Food & Grocery', 'silver', 3.5, 181, ''),
    ('Andhra Chillies', 'Marathahalli, Bangalore', 12.951973, 77.700184, 'Food & Grocery', 'gold', 3.7, 355, ''),
    ('Print IT Solutions', 'Marathahalli, Bangalore', 12.958901, 77.701714, 'Electronics', 'bronze', 3.4, 159, ''),
    ('Sri Balaji Creations', '97/2, Varthur Road, Marathahalli, Bangalore', 12.957337, 77.700606, 'Clothing', 'free', 3.7, 0, ''),
    ('5 Centrigrade', 'Forum Value Mall, Whitefield, Bangalore', 12.959829, 77.747597, 'Food & Grocery', 'free', 2.7, 66, ''),
    ('Sisley Outlet Clothes', 'Forum Value Mall, Whitefield, Bangalore', 12.9594, 77.748273, 'Clothing', 'gold', 4.0, 1193, ''),
    ('Pantaloon', 'Forum Value Mall, Whitefield, Bangalore', 12.959475, 77.747896, 'Clothing', 'bronze', 3.6, 197, ''),
    ('MTR', 'Forum Value Mall, Whitefield, Bangalore', 12.959816, 77.748138, 'Food & Grocery', 'bronze', 3.9, 110, ''),
    ('Cream n Desserts', 'Forum Value Mall, Whitefield, Bangalore', 12.959914, 77.748448, 'Food & Grocery', 'silver', 3.9, 402, ''),
    ('Taj Express', 'Forum Value Mall, Whitefield, Bangalore', 12.95983, 77.747465, 'Food & Grocery', 'free', 3.5, 30, ''),
    ('Liberty Silks', 'Forum Value Mall, Whitefield, Bangalore', 12.959531, 77.748071, 'Clothing', 'free', 2.8, 27, ''),
    ('Ala Dakshinapuramlo', 'Forum Value Mall, Whitefield, Bangalore', 12.959832, 77.747338, 'Food & Grocery', 'platinum', 4.6, 2269, ''),
    ('Rajdhani Snacklet', 'Forum Value Mall, Whitefield, Bangalore', 12.959816, 77.74856, 'Food & Grocery', 'silver', 3.7, 423, ''),
    ('The Time Factory', 'Forum Value Mall, Whitefield, Bangalore', 12.959538, 77.74852, 'Clothing', 'silver', 3.5, 180, ''),
    ('Pind Balluchi', 'Forum Value Mall, Whitefield, Bangalore', 12.959823, 77.747936, 'Food & Grocery', 'free', 3.3, 20, ''),
    ('Calvin Klein', 'Forum Value Mall, Whitefield, Bangalore', 12.959465, 77.747418, 'Clothing', 'bronze', 3.1, 85, ''),
    ('Access 2 Future', 'Forum Value Mall, Whitefield, Bangalore', 12.959991, 77.747794, 'Electronics', 'silver', 4.3, 123, ''),
    ('The Book Shelf', 'Forum Value Mall, Whitefield, Bangalore', 12.959952, 77.748391, 'Books', 'bronze', 3.1, 88, ''),
    ('Mega Mart', 'Forum Value Mall, Whitefield, Bangalore', 12.959399, 77.748519, 'Clothing', 'platinum', 4.7, 2078, ''),
    ('Rattrap', '62, Whitefield, Bangalore', 12.959542, 77.748401, 'Clothing', 'silver', 4.0, 381, '+918041521236'),
    ('Vivah', 'Forum Value Mall, Whitefield, Bangalore', 12.959634, 77.748517, 'Clothing', 'gold', 4.3, 959, ''),
    ('Neeru’S Fashion', 'Forum Value Mall, Whitefield, Bangalore', 12.959722, 77.747598, 'Clothing', 'gold', 4.0, 1713, ''),
    ('Stanza', 'Forum Value Mall, Whitefield, Bangalore', 12.959624, 77.748488, 'Clothing', 'bronze', 3.0, 174, ''),
    ('Bulchee', 'Forum Value Mall, Whitefield, Bangalore', 12.959724, 77.747947, 'Clothing', 'free', 3.0, 68, ''),
    ('WHIZZ', 'Forum Value Mall, Whitefield, Bangalore', 12.959979, 77.74816, 'Electronics', 'free', 3.2, 8, ''),
    ('ONKYO', 'Forum Value Mall, Whitefield, Bangalore', 12.959979, 77.748245, 'Electronics', 'bronze', 3.1, 105, ''),
    ('Kalmane Koffee', 'Forum Value Mall, Whitefield, Bangalore', 12.959724, 77.748452, 'Food & Grocery', 'silver', 3.5, 458, ''),
    ('Zaari', 'Forum Value Mall, Whitefield, Bangalore', 12.959732, 77.748057, 'Clothing', 'free', 2.7, 48, ''),
    ("Toscano's", 'Forum Value Mall, Whitefield, Bangalore', 12.959894, 77.748129, 'Food & Grocery', 'platinum', 4.2, 1360, ''),
    ('Alen Solly', 'Forum Value Mall, Whitefield, Bangalore', 12.959724, 77.748301, 'Clothing', 'silver', 3.5, 319, ''),
    ('Mayura Bakery & Sweets', 'Whitefield Main Road, Whitefield, Bangalore', 12.969896, 77.750165, 'Food & Grocery', 'silver', 3.7, 470, ''),
    ("Brother's Bakery", 'Whitefield Main Road, Whitefield, Bangalore', 12.97017, 77.749826, 'Food & Grocery', 'gold', 3.8, 814, ''),
    ('Pushpa Medicals', '284, Whitefield Main Road, Whitefield, Bangalore', 12.968875, 77.749629, 'Health & Wellness', 'silver', 4.3, 332, ''),
    ('Bhagini Park', 'Whitefield, Bangalore', 12.963214, 77.748342, 'Food & Grocery', 'free', 2.8, 6, ''),
    ('Mayuri Bar And Restaurant', 'Whitefield, Bangalore', 12.970027, 77.749777, 'Food & Grocery', 'free', 3.6, 49, ''),
    ('Shri Lakshmi Vaibhava', 'Whitefield Main Road, Whitefield, Bangalore', 12.969287, 77.749686, 'Food & Grocery', 'free', 3.5, 68, ''),
    ("Kolkata's", 'Whitefield, Bangalore', 12.963978, 77.748712, 'Food & Grocery', 'silver', 4.3, 256, ''),
    ('Medi House Pharma', 'Whitefield, Bangalore', 12.963602, 77.748149, 'Health & Wellness', 'bronze', 3.1, 22, ''),
    ('National Pharma', 'Whitefield, Bangalore', 12.968319, 77.749497, 'Health & Wellness', 'silver', 4.1, 386, ''),
    ('Mayura Bakery and Sweets', 'Whitefield, Bangalore', 12.969609, 77.750088, 'Food & Grocery', 'free', 3.1, 34, ''),
    ('S V Sushma Hair Dressers', 'Whitefield, Bangalore', 12.969727, 77.750394, 'Beauty & Personal Care', 'free', 3.4, 47, ''),
    ('Whitefield Fish and Chicken Centre', 'Immadihalli Road, Whitefield, Bangalore', 12.969719, 77.750228, 'Food & Grocery', 'silver', 3.6, 361, ''),
    ("Aaina Womens' Salon", 'Whitefield, Bangalore', 12.968689, 77.748034, 'Beauty & Personal Care', 'bronze', 3.5, 132, ''),
    ('Bliss Chocolate Lounge', 'Whitefield, Bangalore', 12.959615, 77.747575, 'Food & Grocery', 'platinum', 4.8, 4614, ''),
    ('Krspy Kreme Doughnuts', 'Whitefield, Bangalore', 12.95977, 77.747663, 'Food & Grocery', 'free', 3.7, 66, ''),
    ('Best Little Hair House', 'Whitefield, Bangalore', 12.969697, 77.748997, 'Beauty & Personal Care', 'gold', 4.6, 1767, ''),
    ('Blend - Royal Orchid Suites', 'Whitefield, Bangalore', 12.966775, 77.751231, 'Food & Grocery', 'free', 3.3, 11, ''),
    ('Bangalore Mandarin', 'Whitefield, Bangalore', 12.970088, 77.750248, 'Food & Grocery', 'gold', 4.1, 740, ''),
    ('goodwill enterprises', '25, Forum Value Mall, Whitefield Main Road, Near-B B M P Whitefild, Whitefield, Prestige Ozone, Whitefield, Whitefield, Bangalore', 12.959907, 77.748256, 'Sports', 'gold', 4.3, 257, '+918040961202'),
    ('Loyal World Market', 'Whitefield, Bangalore', 12.959591, 77.74741, 'Food & Grocery', 'silver', 4.3, 134, ''),
    ('Chin Chin', 'Whitefield, Bangalore', 12.96295, 77.750724, 'Food & Grocery', 'silver', 3.5, 196, ''),
    ('Roots', 'Whitefield, Bangalore', 12.969068, 77.740164, 'Beauty & Personal Care', 'silver', 3.9, 497, ''),
    ('Udupi Adithya', 'Whitefield, Bangalore', 12.961785, 77.747918, 'Food & Grocery', 'free', 3.6, 65, ''),
    ('Mitti Cafe', 'Whitefield, Bangalore', 12.959514, 77.748365, 'Food & Grocery', 'gold', 3.8, 1119, ''),
    ('Orzuv', 'Whitefield Main Road, Whitefield, Bangalore', 12.962909, 77.747738, 'Food & Grocery', 'bronze', 4.0, 156, '+919755564656'),
    ('Madeena Restaurant', 'Whitefield, Bangalore', 12.968579, 77.747993, 'Food & Grocery', 'silver', 3.8, 365, ''),
    ('Emperor', 'Whitefield, Bangalore', 12.969035, 77.749665, 'Food & Grocery', 'free', 3.5, 0, ''),
    ('Poco A Poco', 'Whitefield, Bangalore', 12.980377, 77.751021, 'Food & Grocery', 'silver', 4.1, 433, ''),
    ('Planet Sports (Yonex Dealership)', 'Whitefield, Bangalore', 12.976882, 77.753489, 'Sports', 'bronze', 4.0, 189, ''),
    ('Sportfit', 'Whitefield, Bangalore', 12.967766, 77.75717, 'Sports', 'platinum', 4.8, 4826, ''),
    ("Garner's Bakery", 'Whitefield, Bangalore', 12.9728, 77.74892, 'Food & Grocery', 'silver', 3.4, 279, ''),
    ('Herbs & Spices', 'Whitefield, Bangalore', 12.962275, 77.748842, 'Food & Grocery', 'platinum', 4.7, 4788, '+919036691010'),
    ('Habba Kadal Kashmiri Heritage Hub', 'Whitefield, Bangalore', 12.972076, 77.74936, 'Clothing', 'gold', 3.9, 851, ''),
    ('Aroma Cafe', 'Whitefield, Bangalore', 12.969414, 77.748022, 'Food & Grocery', 'gold', 4.2, 204, ''),
    ("Sardarji's Kitchen", 'Whitefield, Bangalore', 12.969428, 77.747717, 'Food & Grocery', 'bronze', 3.4, 115, ''),
    ('Tarangini Multi-Cuisine Outdoor', 'Whitefield, Bangalore', 12.972101, 77.749423, 'Food & Grocery', 'platinum', 4.3, 2546, ''),
    ('Habba Kadal Kashmiri Restaurant', 'Whitefield, Bangalore', 12.972198, 77.749432, 'Food & Grocery', 'gold', 4.6, 1856, ''),
    ('Bismallah Chicken Center', 'Whitefield, Bangalore', 12.967584, 77.74936, 'Food & Grocery', 'free', 3.0, 37, ''),
    ('Preferred Store', 'Whitefield, Bangalore', 12.979374, 77.751328, 'Clothing', 'free', 2.7, 80, ''),
    ('Top Brother Tailors', 'Whitefield, Bangalore', 12.969135, 77.749636, 'Clothing', 'silver', 3.6, 206, ''),
    ('Smart Club', 'Whitefield, Bangalore', 12.969184, 77.749638, 'Electronics', 'gold', 4.7, 1155, ''),
    ('Fone Case', 'Whitefield, Bangalore', 12.969364, 77.749691, 'Electronics', 'gold', 3.7, 1048, ''),
    ('Sree Sai Opticals', 'Whitefield, Bangalore', 12.970128, 77.749828, 'Health & Wellness', 'gold', 4.5, 217, ''),
    ('Desi Cuisine', 'Whitefield, Bangalore', 12.968258, 77.749489, 'Food & Grocery', 'bronze', 3.6, 104, ''),
    ('Lemon Krust', 'Whitefield, Bangalore', 12.968375, 77.749485, 'Food & Grocery', 'free', 2.8, 44, ''),
    ('Vision World', 'Whitefield, Bangalore', 12.963689, 77.748163, 'Health & Wellness', 'gold', 3.9, 826, ''),
    ('Titan Eye', 'Whitefield, Bangalore', 12.964339, 77.748718, 'Health & Wellness', 'gold', 3.7, 982, ''),
    ('Street Chai', 'Whitefield, Bangalore', 12.973135, 77.750533, 'Food & Grocery', 'bronze', 3.1, 151, ''),
    ('Mustache', 'Whitefield, Bangalore', 12.974994, 77.750965, 'Beauty & Personal Care', 'gold', 4.1, 1970, ''),
    ('Udupi Vibhava', 'Whitefield, Bangalore', 12.965671, 77.749135, 'Food & Grocery', 'bronze', 3.2, 188, ''),
    ('Mayur Bangle Store', 'Whitefield, Bangalore', 12.970055, 77.750236, 'Beauty & Personal Care', 'free', 3.2, 47, ''),
    ('Tony And Guy', 'Whitefield, Bangalore', 12.979078, 77.751697, 'Beauty & Personal Care', 'free', 3.1, 23, ''),
    ('New Priyanka Stationery', 'Whitefield, Bangalore', 12.97121, 77.750338, 'Stationery', 'bronze', 3.3, 132, ''),
    ('Bhangarpet Snacks Point', 'Whitefield, Bangalore', 12.971089, 77.750318, 'Food & Grocery', 'silver', 3.8, 121, ''),
    ('Franke Faber', 'Whitefield, Bangalore', 12.970135, 77.750296, 'Electronics', 'bronze', 3.4, 111, ''),
    ('Sumiz Makeup Studio & Academy', 'Whitefield, Bangalore', 12.970085, 77.750289, 'Beauty & Personal Care', 'gold', 4.4, 326, '+917019751429'),
    ('Envy Aesthetics', 'Whitefield, Bangalore', 12.970031, 77.750272, 'Beauty & Personal Care', 'free', 2.7, 50, '+919611885577;+919611887755'),
    ('Krishna Electricals And Hardware', 'Whitefield, Bangalore', 12.969933, 77.750168, 'Hardware', 'bronze', 3.3, 28, ''),
    ('Mothi Dauran', 'Whitefield, Bangalore', 12.969748, 77.750104, 'Food & Grocery', 'free', 2.9, 61, ''),
    ('Shivam Enterprises', 'Whitefield, Bangalore', 12.969693, 77.750095, 'Electronics', 'platinum', 5.0, 1473, ''),
    ('Sana Enterprises', 'Whitefield, Bangalore', 12.969535, 77.750067, 'Stationery', 'bronze', 3.6, 194, ''),
    ('Smart Kids', 'Whitefield, Bangalore', 12.969476, 77.750062, 'Clothing', 'silver', 3.7, 441, ''),
    ('Mob Care', 'Whitefield, Bangalore', 12.969435, 77.75005, 'Electronics', 'silver', 3.8, 421, ''),
    ('KNK Fashions', 'Whitefield, Bangalore', 12.969399, 77.750041, 'Clothing', 'gold', 4.5, 896, ''),
    ('Popular Footwear', 'Whitefield, Bangalore', 12.969362, 77.750039, 'Clothing', 'free', 3.7, 2, ''),
    ('Kerala Home Kitchen', 'Whitefield, Bangalore', 12.969216, 77.750164, 'Food & Grocery', 'free', 3.0, 1, ''),
    ('Mahadev Electricals & Hardware', 'Whitefield Main Road, Whitefield, Bangalore', 12.969099, 77.749995, 'Hardware', 'silver', 3.6, 397, ''),
    ('Hello Fresh', 'Whitefield Main Road, Whitefield, Bangalore', 12.969041, 77.749988, 'Food & Grocery', 'gold', 4.1, 1322, ''),
    ("C8 Men's Wear", 'Whitefield Main Road, Whitefield, Bangalore', 12.969009, 77.749979, 'Clothing', 'silver', 3.8, 198, ''),
    ('X Zone', 'Whitefield, Bangalore', 12.968608, 77.749938, 'Clothing', 'free', 2.8, 54, ''),
    ('Kids Zone', 'Whitefield, Bangalore', 12.968547, 77.749932, 'Clothing', 'free', 2.9, 65, ''),
    ('Darjeeling Dotma Fast Food', 'Whitefield, Bangalore', 12.968265, 77.749887, 'Food & Grocery', 'free', 3.0, 14, ''),
    ('Homoeo Pharma', 'Whitefield, Bangalore', 12.969065, 77.75002, 'Health & Wellness', 'gold', 4.7, 606, ''),
    ('St Chaitanya Enterprises', 'Whitefield, Bangalore', 12.975517, 77.743712, 'Stationery', 'silver', 3.8, 292, ''),
    ('Happins', 'Whitefield, Bangalore', 12.975345, 77.745969, 'Food & Grocery', 'silver', 4.2, 408, ''),
    ('Wah Paratha', 'Bellandur, Bangalore', 12.926276, 77.669835, 'Food & Grocery', 'silver', 4.0, 146, ''),
    ('Families Hypermarket', 'Bellandur, Bangalore', 12.918667, 77.670841, 'Food & Grocery', 'platinum', 4.6, 1718, ''),
    ('Ring View Restaurant', 'Bellandur, Bangalore, Bellandur, Bangalore', 12.926062, 77.676001, 'Food & Grocery', 'free', 2.8, 4, ''),
    ('Sinan Provision Store', 'Bellandur, Bangalore', 12.921812, 77.676157, 'Food & Grocery', 'free', 2.9, 18, ''),
    ('Fresco', 'Bellandur, Bangalore', 12.925459, 77.669887, 'Food & Grocery', 'bronze', 3.6, 43, ''),
    ('K. M. Bakery', 'Bellandur, Bangalore', 12.928312, 77.671989, 'Food & Grocery', 'gold', 4.1, 1175, ''),
    ('Happy Land Supermarket', 'Bellandur, Bangalore', 12.927368, 77.671066, 'Food & Grocery', 'bronze', 3.6, 79, ''),
    ('Exide Battery Inverter shop', 'Bellandur, Bangalore', 12.922285, 77.677415, 'Food & Grocery', 'free', 3.1, 7, ''),
    ('Posh Palates', 'Bellandur, Bangalore', 12.925305, 77.66988, 'Food & Grocery', 'bronze', 3.6, 97, ''),
    ('G-Mart now VILLAGE', 'Bellandur, Bangalore', 12.922822, 77.673062, 'Food & Grocery', 'free', 3.1, 44, ''),
    ('Gopal Jee', 'Bellandur, Bangalore', 12.924549, 77.674334, 'Food & Grocery', 'silver', 3.6, 74, ''),
    ('Happy Endings', 'Bellandur, Bangalore', 12.924576, 77.672909, 'Food & Grocery', 'free', 2.7, 41, ''),
    ('Punjabi Times', 'Bellandur, Bangalore', 12.92411, 77.671697, 'Food & Grocery', 'free', 3.2, 77, ''),
    ('Ruh', 'Bellandur, Bangalore', 12.924543, 77.672869, 'Food & Grocery', 'free', 2.9, 62, ''),
    ('Super Battery Center', 'Bellandur, Bangalore', 12.922518, 77.669712, 'Automotive', 'bronze', 3.3, 44, ''),
    ('Super Tyre Center', 'Bellandur, Bangalore', 12.922494, 77.669667, 'Automotive', 'silver', 4.1, 444, ''),
    ('Sunrise Enterprises', 'Bellandur, Bangalore', 12.925441, 77.674653, 'Clothing', 'gold', 4.0, 1618, ''),
    ('Oh Wow', 'Bellandur, Bangalore', 12.924712, 77.674526, 'Beauty & Personal Care', 'gold', 4.4, 1306, ''),
    ('Philips Light Lounge', 'Bellandur, Bangalore', 12.924423, 77.673984, 'Electronics', 'bronze', 3.4, 186, ''),
    ("Rosh Roy's", 'Bellandur, Bangalore', 12.924338, 77.673772, 'Beauty & Personal Care', 'free', 3.3, 11, ''),
    ('Idea Device;Street Lamp', 'Bellandur, Bangalore', 12.924231, 77.671991, 'Electronics', 'gold', 3.8, 1620, ''),
    ('Classic Biriyani Center', 'Bellandur, Bangalore', 12.92478, 77.674625, 'Food & Grocery', 'silver', 3.4, 385, ''),
    ('Little Sparrows', 'Bellandur, Bangalore', 12.918473, 77.670746, 'Toys & Baby', 'silver', 4.2, 148, ''),
    ('Uber Unisex salon & Med spa', 'Bellandur, Bangalore', 12.918745, 77.670034, 'Beauty & Personal Care', 'silver', 4.3, 88, ''),
    ('Cloud 9 unisex salon', 'Bellandur, Bangalore', 12.918788, 77.669767, 'Beauty & Personal Care', 'gold', 4.3, 717, ''),
    ('Adwaith Hyundai', 'Bellandur, Bangalore', 12.929894, 77.683839, 'Automotive', 'silver', 4.4, 153, ''),
    ('Blue Terrain', 'Bellandur, Bangalore', 12.930035, 77.683453, 'Food & Grocery', 'bronze', 3.2, 52, ''),
    ('The Square', 'Bellandur, Bangalore', 12.930133, 77.683347, 'Food & Grocery', 'free', 3.3, 32, ''),
    ('Delice', 'Bellandur, Bangalore', 12.929853, 77.683147, 'Food & Grocery', 'gold', 4.5, 1832, ''),
    ('Ayyappa Bakery', '1, Kariyammana Agrahara Rd, Kadubeesanahalli, Marathahalli, Bellandur, Bangalore', 12.927496, 77.676602, 'Food & Grocery', 'gold', 4.7, 1932, ''),
    ('Sports Planet', 'Shop No.#64, #351, Sarjapur Road, Bellandur, Bangalore', 12.918446, 77.67082, 'Sports', 'bronze', 3.6, 50, '+919900454672'),
    ('Cloud Nine Beauty Salon', 'No.629, Sarjapur Road, Bellandur, Bangalore', 12.918907, 77.66998, 'Beauty & Personal Care', 'gold', 3.8, 958, '+919901111264'),
    ('Velvete Lounge Unisex Salon & Spa', '164, Green Glen Layout, Bellandur, Bangalore', 12.92751, 77.669637, 'Beauty & Personal Care', 'bronze', 4.0, 25, '+918041483355'),
    ('Jawed Habib Hair And Beauty Parlour', 'Outer Ring Road, Bellandur, Bangalore', 12.922514, 77.668372, 'Beauty & Personal Care', 'free', 3.0, 16, '+918033724205'),
    ('Style in 7 Wella', 'Kothandaraman Complex, Bellandur Main Road, Bellandur,, Bellandur, Bangalore', 12.922834, 77.673129, 'Beauty & Personal Care', 'free', 3.7, 7, '+919148100961'),
    ('Louise Philip', 'Bellandur, Bangalore', 12.920091, 77.667075, 'Clothing', 'silver', 4.1, 429, '+918088933969'),
    ('Purani Dilli', 'Bellandur, Bangalore', 12.916721, 77.673276, 'Food & Grocery', 'gold', 4.2, 1190, ''),
    ('Nayi Dilli', 'Bellandur, Bangalore', 12.91666, 77.673331, 'Food & Grocery', 'gold', 4.7, 438, ''),
    ('Aiswarya Bakery and Sweets', 'Bellandur, Bangalore', 12.918042, 77.671554, 'Food & Grocery', 'bronze', 3.5, 163, ''),
    ('Sri Venkateshwara Bakery', 'Bellandur, Bangalore', 12.92371, 77.676029, 'Food & Grocery', 'silver', 3.8, 189, ''),
    ('Moritz', 'Bellandur, Bangalore', 12.918645, 77.670711, 'Food & Grocery', 'free', 2.7, 73, ''),
    ('DS Supermarket', 'Bellandur, Bangalore', 12.928169, 77.671773, 'Food & Grocery', 'free', 3.4, 65, ''),
    ('Udupi Athithiya', 'Bellandur Main Road, Bellandur, Bangalore', 12.925583, 77.676266, 'Food & Grocery', 'bronze', 3.7, 154, ''),
    ("Chili's", 'Bellandur, Bangalore', 12.920174, 77.684503, 'Food & Grocery', 'free', 3.6, 37, ''),
    ('Cera Ceramics', 'Bellandur, Bangalore', 12.924786, 77.674751, 'Hardware', 'silver', 4.0, 137, ''),
    ('Zaatar', 'Bellandur, Bangalore', 12.923312, 77.669703, 'Food & Grocery', 'free', 3.2, 37, ''),
    ('Big Bazaar', 'Bellandur, Bangalore', 12.9193, 77.668908, 'Food & Grocery', 'gold', 3.7, 403, '+917795976949'),
    ('Fab India Experience Centre', 'Bellandur, Bangalore', 12.917766, 77.670956, 'Clothing', 'gold', 4.0, 372, ''),
    ('Thyme & Whisk', 'Bellandur, Bangalore', 12.916175, 77.673017, 'Food & Grocery', 'free', 3.5, 31, '+919741616018'),
    ('Beardo', 'Ground Floor, Bellanduru Main Road, Bellandur, Bangalore', 12.922855, 77.67573, 'Beauty & Personal Care', 'platinum', 4.2, 1451, ''),
    ('Coffee Brewery', 'Bellandur, Bangalore', 12.925845, 77.66963, 'Food & Grocery', 'free', 3.0, 77, ''),
    ('Public Bar and Kitchen', 'Bellandur, Bangalore', 12.929972, 77.685929, 'Food & Grocery', 'platinum', 4.3, 4999, ''),
    ('Prequel', 'Bellandur, Bangalore', 12.920522, 77.684782, 'Food & Grocery', 'free', 2.8, 78, ''),
    ('Kampot', 'Bellandur, Bangalore', 12.920659, 77.684527, 'Food & Grocery', 'free', 3.7, 80, ''),
    ('Sauce Code', 'Bellandur, Bangalore', 12.920146, 77.684919, 'Food & Grocery', 'gold', 4.5, 549, ''),
    ('Gongura Grand', 'Bellandur, Bangalore', 12.919053, 77.670987, 'Food & Grocery', 'silver', 3.7, 181, ''),
    ('The Eye Needs', 'Bellandur, Bangalore', 12.919037, 77.671023, 'Health & Wellness', 'platinum', 4.5, 1371, ''),
    ('The Eye Foundation', 'Bellandur, Bangalore', 12.9237, 77.6725, 'Health & Wellness', 'silver', 4.1, 384, ''),
    ('Susswadu', 'Bellandur, Bangalore', 12.923192, 77.671201, 'Food & Grocery', 'free', 3.3, 77, ''),
    ("Sharief Bhai's", 'Bellandur, Bangalore', 12.919195, 77.668639, 'Food & Grocery', 'silver', 4.2, 162, '+918095752222'),
    ('Sundha Medicals', 'Bellandur, Bangalore', 12.919746, 77.667489, 'Health & Wellness', 'bronze', 3.2, 181, ''),
    ('Upahar Kerala Mess', 'Bellandur, Bangalore', 12.92595, 77.677852, 'Food & Grocery', 'gold', 4.5, 418, ''),
    ('PI - Lounge Bar', 'Bellandur, Bangalore', 12.926347, 77.682899, 'Food & Grocery', 'bronze', 3.7, 131, ''),
    ('KPN Fresh Supermarket', 'Bellandur, Bangalore', 12.92732, 77.669641, 'Food & Grocery', 'bronze', 3.9, 43, ''),
    ('Green Land Supermarket', 'Bellandur, Bangalore', 12.928133, 77.669243, 'Food & Grocery', 'bronze', 3.5, 65, ''),
    ('Trippy Goat Cafe', 'Bellandur, Bangalore', 12.920124, 77.685153, 'Food & Grocery', 'free', 3.2, 23, ''),
    ('Xiaomi', 'Bellandur, Bangalore', 12.926158, 77.676183, 'Electronics', 'silver', 3.4, 133, ''),
    ('Sri Basaveshwara Sugar Cane', 'Bellandur, Bangalore', 12.925921, 77.669833, 'Food & Grocery', 'gold', 4.5, 1507, ''),
    ('Chef Bakers', 'Bellandur, Bangalore', 12.926317, 77.669837, 'Food & Grocery', 'silver', 3.5, 86, ''),
    ('Nailashes', 'Bellandur, Bangalore', 12.926834, 77.669664, 'Beauty & Personal Care', 'bronze', 3.5, 185, ''),
    ('Nail Nexus', 'Bellandur, Bangalore', 12.92735, 77.669401, 'Beauty & Personal Care', 'platinum', 4.1, 4815, ''),
    ('Sri Krishna Bhog', 'Bellandur, Bangalore', 12.927297, 77.66924, 'Food & Grocery', 'free', 3.5, 1, ''),
    ('Shahi Paneer', 'Bellandur, Bangalore', 12.927435, 77.669236, 'Food & Grocery', 'silver', 3.8, 60, ''),
    ('Fifty50', 'Bellandur, Bangalore', 12.920494, 77.68462, 'Food & Grocery', 'free', 2.7, 5, ''),
    ('Zepto Cafe', 'Bellandur, Bangalore', 12.91653, 77.671406, 'Food & Grocery', 'free', 3.5, 34, ''),
    ('New Ambur Hot Biriyani', 'Bellandur, Bangalore', 12.926851, 77.678935, 'Food & Grocery', 'free', 3.3, 1, ''),
    ('Maher Botique', 'Bellandur, Bangalore', 12.927443, 77.669404, 'Clothing', 'silver', 3.4, 455, ''),
    ('A2B', 'Bellandur, Bangalore', 12.927476, 77.671751, 'Food & Grocery', 'free', 3.4, 43, ''),
    ('Udupi Gokula', 'Bellandur, Bangalore', 12.929764, 77.685181, 'Food & Grocery', 'gold', 3.8, 385, ''),
    ('Aroma Restaurant', 'Bellandur, Bangalore', 12.925298, 77.674517, 'Food & Grocery', 'bronze', 3.6, 133, ''),
    ('Malabar Kitchen', 'Bellandur, Bangalore', 12.925988, 77.675912, 'Food & Grocery', 'gold', 4.0, 1080, ''),
    ("Leon's Burgers & Wings", 'Outer Ring Road, Bellandur, Bangalore', 12.924294, 77.673768, 'Food & Grocery', 'free', 2.8, 34, '+919731123856'),
    ('Nalapaka Express', 'Bellandur, Bangalore', 12.931697, 77.684848, 'Food & Grocery', 'silver', 3.5, 472, ''),
    ('Adhira & Appa', 'Bellandur, Bangalore', 12.919571, 77.668258, 'Food & Grocery', 'free', 3.2, 13, ''),
    ('Damadhish Fine Dine - Family Restaurant', '35, Devasthanagalu, Varthur, Bengaluru, Varthur, Bangalore', 12.936194, 77.744206, 'Food & Grocery', 'bronze', 3.8, 56, '+919986383800'),
    ('Udupi Garden Veg', 'Varthur, Bangalore', 12.939394, 77.726184, 'Food & Grocery', 'free', 2.9, 29, ''),
    ('New Look', 'Varthur, Bangalore', 12.939879, 77.72433, 'Beauty & Personal Care', 'bronze', 3.1, 187, ''),
    ('Ashapurna Medicals', 'Varthur, Bangalore', 12.93946, 77.734801, 'Health & Wellness', 'free', 3.6, 49, ''),
    ('Sri Ramdev Medicals', 'Varthur, Bangalore', 12.936494, 77.74451, 'Health & Wellness', 'silver', 4.1, 107, ''),
    ('Udupi Pakaruchi', 'Varthur, Bangalore', 12.938383, 77.744282, 'Food & Grocery', 'bronze', 4.0, 77, ''),
    ('Top in Town', 'Balagere Rd, Balagere, Varthur, Bangalore', 12.939675, 77.726797, 'Food & Grocery', 'free', 3.7, 45, ''),
    ('The Banaras Cafe', 'Varthur, Bangalore', 12.928601, 77.739703, 'Food & Grocery', 'free', 3.0, 46, ''),
    ('RR Dum Biriyani', 'Varthur, Bangalore', 12.939085, 77.74065, 'Food & Grocery', 'gold', 4.6, 1934, ''),
    ('Aarya Vision Care', 'Varthur, Bangalore', 12.933081, 77.742633, 'Health & Wellness', 'free', 2.9, 54, ''),
    ('Hair Story', 'Varthur, Bangalore', 12.933129, 77.742647, 'Beauty & Personal Care', 'bronze', 3.6, 193, ''),
    ('Sufyan Dates and Nuts', 'Varthur, Bangalore', 12.932196, 77.742501, 'Food & Grocery', 'bronze', 3.9, 195, ''),
    ('Chandran Gurukkal Ayurveda Clinics', 'Varthur, Bangalore', 12.931467, 77.741899, 'Food & Grocery', 'bronze', 3.8, 109, ''),
    ('SS Hardware', 'Varthur, Bangalore', 12.938381, 77.743699, 'Hardware', 'gold', 4.0, 1122, ''),
    ('Vikas Medicals', 'Vijayanagar, Bangalore', 12.967245, 77.542765, 'Health & Wellness', 'silver', 4.3, 465, ''),
    ('SLV Fast Food', 'Vijayanagar, Bangalore', 12.967292, 77.542611, 'Food & Grocery', 'gold', 4.5, 1715, ''),
    ('Durga Chats', 'Vijayanagar, Bangalore', 12.972426, 77.538865, 'Food & Grocery', 'free', 3.6, 19, ''),
    ('Paratha Point', 'Vijayanagar, Bangalore', 12.967203, 77.541844, 'Food & Grocery', 'silver', 3.5, 125, ''),
    ("LJB Iyengar's Bakery", 'Vijayanagar, Bangalore', 12.967619, 77.541608, 'Food & Grocery', 'bronze', 3.7, 142, ''),
    ('Nature Fresh', 'Vijayanagar, Bangalore', 12.967302, 77.542144, 'Food & Grocery', 'gold', 3.8, 975, ''),
    ('Mamatha Departmental Store', 'Vijayanagar, Bangalore', 12.967313, 77.541708, 'Food & Grocery', 'platinum', 4.3, 1240, ''),
    ('Seetha General Store', 'Vijayanagar, Bangalore', 12.967308, 77.542551, 'Food & Grocery', 'bronze', 3.1, 118, ''),
    ('Indraprastha', 'Vijayanagar, Bangalore', 12.971657, 77.537278, 'Food & Grocery', 'bronze', 3.5, 143, ''),
    ('Ganga Sagar', 'Vijayanagar, Bangalore', 12.967276, 77.542286, 'Food & Grocery', 'silver', 3.8, 456, ''),
    ('Devi Medicals', 'Vijayanagar, Bangalore', 12.967465, 77.541375, 'Health & Wellness', 'silver', 3.5, 384, ''),
    ('Nisarga Andhra Style', 'Vijayanagar, Bangalore', 12.968329, 77.538048, 'Food & Grocery', 'gold', 4.1, 1225, ''),
    ('Reliance Mart', 'Vijayanagar, Bangalore', 12.965943, 77.534734, 'Food & Grocery', 'bronze', 3.5, 156, ''),
    ('Ayodhya Food Line', 'Nagarbhavi Main Road, Vijayanagar, Bangalore', 12.976064, 77.531513, 'Food & Grocery', 'bronze', 3.2, 41, ''),
    ("Baker's Pride", 'Chord Road, Vijayanagar, Bangalore', 12.971304, 77.536529, 'Food & Grocery', 'silver', 3.8, 345, ''),
    ('Amarland Departmental', 'Nagarbhavi Main Road, Vijayanagar, Bangalore', 12.972982, 77.529496, 'Food & Grocery', 'silver', 4.2, 162, ''),
    ('Go Green Vegetables', 'Nagarbhavi Main Road, Vijayanagar, Bangalore', 12.972803, 77.529614, 'Food & Grocery', 'silver', 3.6, 51, ''),
    ('Vijaya Bank, Mudalapalya Branch', 'Vijayanagar, Bangalore', 12.968625, 77.524462, 'Food & Grocery', 'free', 3.6, 46, ''),
    ('Karanth Vihar', 'Pattegarara Palya Main Road, Vijayanagar, Bangalore', 12.976126, 77.530764, 'Food & Grocery', 'silver', 4.4, 106, ''),
    ('Hopcoms Mangalore Store', 'Nagarbhavi Main Road, Vijayanagar, Bangalore', 12.972914, 77.52854, 'Food & Grocery', 'free', 3.0, 17, ''),
    ('Shopper City', 'Nagarbhavi Main Road, Vijayanagar, Bangalore', 12.97625, 77.532044, 'Food & Grocery', 'free', 2.8, 70, ''),
    ('Akshata Nati Style', 'Vijayanagar, Bangalore', 12.972957, 77.539731, 'Food & Grocery', 'platinum', 4.9, 2486, ''),
    ('Park View Bar & Restaurant', 'Vijayanagar, Bangalore', 12.972914, 77.539535, 'Food & Grocery', 'free', 2.9, 17, ''),
    ('M K Ahmed Super Shoppy', '6th Main Road, Vijayanagar, Bangalore', 12.972595, 77.539513, 'Food & Grocery', 'free', 3.4, 18, ''),
    ('Amaravati Andhra Style', 'Magadi Road, Vijayanagar, Bangalore', 12.980818, 77.535785, 'Food & Grocery', 'bronze', 3.0, 128, ''),
    ('Shobha Hospital Medical Store', 'Vijayanagar, Bangalore', 12.975486, 77.531294, 'Health & Wellness', 'bronze', 3.7, 189, ''),
    ('Canara Juice Center', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.974926, 77.530864, 'Food & Grocery', 'silver', 3.9, 354, ''),
    ('DIVA Boutique', 'No.6, 5th Cross, Amarjyothi Nagar, Vijayanagar, Bangalore', 12.974752, 77.530265, 'Clothing', 'gold', 3.7, 848, ''),
    ('Liberty Footwear', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.97515, 77.530919, 'Clothing', 'free', 2.9, 1, ''),
    ('Techno Products', '65, 2nd Cross, Vijayanagar, Bangalore', 12.972637, 77.527873, 'Hardware', 'silver', 4.2, 126, '+918023381707'),
    ('Mylara Home Appliances', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.974664, 77.532434, 'Electronics', 'silver', 3.9, 121, ''),
    ('HOPCOMS Vegetables Store', '3rd Main Saraswathi nagar, Vijayanagar, Bangalore', 12.972547, 77.529541, 'Food & Grocery', 'silver', 4.1, 203, ''),
    ('Piggy Wiggy', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.974476, 77.533482, 'Toys & Baby', 'bronze', 3.7, 133, ''),
    ('Rishi Medicals', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.974626, 77.53267, 'Health & Wellness', 'silver', 4.1, 196, ''),
    ("Baby's Planet", '20th Main, Vijayanagar, Bangalore', 12.968342, 77.537957, 'Toys & Baby', 'free', 2.8, 4, ''),
    ('New L M Silks', 'Chord Road, Vijayanagar, Bangalore', 12.970877, 77.536498, 'Clothing', 'silver', 3.6, 132, ''),
    ('Frank Ross Pharmacy', 'Chord Road, Vijayanagar, Bangalore', 12.970961, 77.537023, 'Health & Wellness', 'free', 3.5, 58, ''),
    ('Aadishwar', 'Chord Road, Vijayanagar, Bangalore', 12.97005, 77.536756, 'Electronics', 'silver', 4.2, 342, ''),
    ("Sheema's Shop Right", 'Chord Road, Vijayanagar, Bangalore', 12.97106, 77.536669, 'Food & Grocery', 'bronze', 3.7, 128, ''),
    ('Nanjudeshwara Stationary', 'Chord Road, Vijayanagar, Bangalore', 12.971003, 77.536932, 'Stationery', 'gold', 4.0, 288, ''),
    ('Saviruchi Fast Food', 'Chord Road, Vijayanagar, Bangalore', 12.971029, 77.536798, 'Food & Grocery', 'bronze', 4.0, 52, ''),
    ("Royal's Coffe House", 'Chord Road, Vijayanagar, Bangalore', 12.970992, 77.536868, 'Food & Grocery', 'silver', 3.8, 78, ''),
    ('Miracle Touch Beauty Parlour', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.975145, 77.530943, 'Beauty & Personal Care', 'free', 2.9, 71, ''),
    ('Patanjali Arogya Kendra', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.973773, 77.536098, 'Health & Wellness', 'silver', 3.5, 404, ''),
    ('Kottkkal Arya Vaidya Shala', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.973855, 77.535926, 'Health & Wellness', 'bronze', 3.3, 184, ''),
    ('Benaka Sweets', 'Chord Road, Vijayanagar, Bangalore', 12.972655, 77.537828, 'Food & Grocery', 'bronze', 3.4, 33, ''),
    ('Panchavati', 'Chord Road, Vijayanagar, Bangalore', 12.972822, 77.537934, 'Food & Grocery', 'gold', 4.3, 498, ''),
    ('Hotel Karavali', '5th Main, Vijayanagar, Bangalore', 12.972882, 77.540013, 'Food & Grocery', 'free', 2.8, 46, ''),
    ('Alemane Cane Juice', 'Vijayanagar, Bangalore', 12.97286, 77.539287, 'Food & Grocery', 'silver', 4.2, 64, ''),
    ('Gokul Veg', 'Service Road, Vijayanagar, Bangalore', 12.972749, 77.538981, 'Food & Grocery', 'free', 2.9, 13, ''),
    ('Shri Guru Upahara', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.970414, 77.524937, 'Food & Grocery', 'free', 3.3, 39, ''),
    ('Yashhas Electronics', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.970482, 77.52475, 'Electronics', 'free', 2.9, 4, ''),
    ("Hotel Savaji's", 'Chord Road, Vijayanagar, Bangalore', 12.97373, 77.543367, 'Food & Grocery', 'free', 2.8, 72, ''),
    ('Tejas Ayurvedic Center', '10th Main Road, Vijayanagar, Bangalore', 12.972774, 77.537117, 'Health & Wellness', 'silver', 4.0, 454, ''),
    ('Arya Sweets & Bakers', 'Chord Road, Vijayanagar, Bangalore', 12.973417, 77.541865, 'Food & Grocery', 'bronze', 3.0, 43, ''),
    ('Adalia Boutique', 'Vinayaka Layout Main Road, Vijayanagar, Bangalore', 12.964903, 77.530272, 'Clothing', 'gold', 3.7, 830, ''),
    ('Behtar Super Market', 'Marenhalli Main Road, Vijayanagar, Bangalore', 12.968212, 77.530341, 'Food & Grocery', 'silver', 4.4, 306, ''),
    ('Zil Mil', 'Marenhalli Main Road, Vijayanagar, Bangalore', 12.968178, 77.530419, 'Clothing', 'free', 3.5, 45, ''),
    ('By-two Coffee', '5th Main, Vijayanagar, Vijayanagar, Bangalore', 12.964924, 77.538739, 'Food & Grocery', 'platinum', 4.7, 1297, '+919844086711/9880428819'),
    ('Butter Sponge', 'vijayanagar, Vijayanagar, Bangalore', 12.964299, 77.540663, 'Food & Grocery', 'bronze', 3.9, 148, ''),
    ('Aspen Chinese Beauty Parlour', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.974643, 77.532533, 'Beauty & Personal Care', 'free', 3.5, 19, ''),
    ('Viseage', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.974466, 77.533511, 'Health & Wellness', 'free', 3.5, 61, ''),
    ('Sri Raghavendra Medicals', 'Vijayanagar Water Tank Road, Vijayanagar, Bangalore', 12.974265, 77.534553, 'Health & Wellness', 'silver', 4.3, 198, ''),
    ('Megha Medicals and General Stores', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.973905, 77.530529, 'Health & Wellness', 'free', 3.4, 10, ''),
    ('Chaitanya Uphara', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.974534, 77.530915, 'Food & Grocery', 'gold', 3.8, 1571, ''),
    ('Saraswati Medicals', '3rd Main Saraswathi nagar, Vijayanagar, Bangalore', 12.972469, 77.529537, 'Health & Wellness', 'free', 2.9, 18, ''),
    ('Star Bazar', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.976048, 77.531966, 'Food & Grocery', 'silver', 3.6, 306, ''),
    ('By Two Coffe', 'Nagarabhavi Road, Vijayanagar, Bangalore', 12.975748, 77.537327, 'Food & Grocery', 'bronze', 3.7, 186, ''),
    ('Hot Coffe', 'Chord Road, Vijayanagar, Bangalore', 12.973792, 77.543318, 'Food & Grocery', 'bronze', 3.8, 134, ''),
    ('Co Optex', 'Chord Road, Vijayanagar, Bangalore', 12.97386, 77.543904, 'Clothing', 'silver', 3.7, 234, ''),
    ('Apollo', 'Subbanna Garden Main road, Vijayanagar, Bangalore', 12.964602, 77.530699, 'Health & Wellness', 'silver', 4.3, 70, ''),
    ('Nisarga restaurant', '111, 1st Main, Club Road, Near Food World, Hampi Nagar, RPC Layout,Vijay Nagar, Vijayanagar, Bangalore', 12.968226, 77.537579, 'Food & Grocery', 'gold', 4.5, 893, '+918023400974'),
    ('Swathi Green Land', 'Jaimunirao Circle, Dasarahalli Magadi Road Junction,\xa0Vijay Nagar, Vijayanagar, Bangalore', 12.978029, 77.542688, 'Food & Grocery', 'free', 3.7, 77, '+918023502055'),
    ('Sai Prasadam', '15, 5th Main Road Rpc Layout, Vijayanagar, Vijayanagar, Bangalore', 12.964326, 77.540406, 'Food & Grocery', 'free', 2.9, 80, '+918023200916'),
    ('Classic inn veg and non veg', '210, 20th Main Road,\xa0Vijay Naga, Vijayanagar, Bangalore', 12.969889, 77.532754, 'Food & Grocery', 'platinum', 4.1, 622, '+918023505748'),
    ('Sagar Restaurant', '827, Vijay Nagar\xa0Main Road, Vijay Nagar, Vijayanagar, Bangalore', 12.973123, 77.540397, 'Food & Grocery', 'free', 3.6, 70, '+918880803832'),
    ('Big Bite', '1714, 17th Cross, 21st Main, Maruthi Mandir,\xa0Vijay Nagar, Vijayanagar, Bangalore', 12.967384, 77.535704, 'Food & Grocery', 'gold', 4.5, 1933, '+918049653144'),
    ('Rotti Mane', "1590, I Main, 'A' Cross, III Main Road, Chandra Layout, Near Adishwara TV Showroom,\xa0Vijay Nagar, Vijayanagar, Bangalore", 12.969586, 77.534663, 'Food & Grocery', 'free', 2.8, 40, '+919916556311'),
    ('The square plate', '30, 1st Floor, SV Complex, Near Raheja Apartments,\xa0Magadi Road, Vijayanagar, Bangalore', 12.97874, 77.540554, 'Food & Grocery', 'free', 3.0, 8, '+919742201125'),
    ('Spoorthy dosa point', '4, Prashanth Nagar Main Road,Vijay Nagar,, Vijayanagar, Bangalore', 12.975743, 77.533593, 'Food & Grocery', 'free', 3.1, 11, '+919632143413'),
    ('Vijaynagar Orthopedic Center', '770, , 5th main, 8th Cross Rd, Govindaraja Nagar Ward, Anubhav Nagar, Vijayanagar, Vijayanagar, Bangalore', 12.975774, 77.542131, 'Health & Wellness', 'free', 3.4, 58, '+9108023308644'),
    ('Health Heal Physio', "2260, 4th 'A' cross, 1st Main Road Vijayanagar, Club Ave, Vijay nagar, Hosahalli Extension, Vijayanagar, Vijayanagar, Bangalore", 12.967564, 77.539088, 'Health & Wellness', 'free', 3.2, 32, '+918880004422'),
    ('Khema Medical Corporation', '315/H, 9k, Vijaya Nagar, Vijaya Nagar, MRCR Layout, MC Layout, Vijayanagar, Bangalore', 12.969906, 77.533151, 'Health & Wellness', 'silver', 3.8, 121, '+918023116416'),
    ('Mukesh Medical', '7th Main Rd, Hoshalli Extension, Stage 1, Vijayanagar, Vijayanagar, Bangalore', 12.971535, 77.543906, 'Health & Wellness', 'gold', 4.7, 1099, '+919844156552'),
    ('Sri Ganesh Gandhi Medicals', '141, Chbs Layout, 8th Main, Vijaya Nagar, CHBS Layout, MC Layout,, Vijayanagar, Bangalore', 12.973491, 77.537483, 'Health & Wellness', 'free', 3.6, 51, '+918023382668'),
    ('Arvind Homoeo Medicals', '2, 1st Floor, 14th Main, Next To Kodandaram Temple, Vijayanagar, SBI Staff Colony, Hoshalli Extension, RPC Layout, Vijayanagar, Bangalore', 12.970381, 77.537568, 'Health & Wellness', 'bronze', 3.6, 194, '+918023503413'),
    ('Green Trends Unisex Hair and Style Salon', 'No 2420/B, 1st Main Road,SBI Staff Colony, Vijayanagar,, Vijayanagar, Bangalore', 12.968463, 77.540573, 'Beauty & Personal Care', 'free', 3.1, 79, '+9191)8033532031'),
    ('Miracle Touch', 'No.1, Amarjyothi Nagar,6th Cross, Vijayanagar, Vijayanagar, Bangalore', 12.977495, 77.530962, 'Beauty & Personal Care', 'platinum', 4.4, 3884, '+9191)8033722582'),
    ('Modicare DP Vijayanagar', '#75, 7th main, Subbanna Garden Main road, Vijayanagar, Bangalore', 12.964734, 77.531357, 'Food & Grocery', 'bronze', 3.3, 194, '+919164258999'),
    ('Bramhashree Condiments', 'Vijayanagar, Bangalore', 12.970879, 77.543503, 'Food & Grocery', 'free', 3.7, 7, ''),
    ('Vijayanagar Vaibhav', 'Vijayanagar, Bangalore', 12.966235, 77.535194, 'Food & Grocery', 'silver', 3.6, 470, ''),
    ('Madhu Hotel', 'Vijayanagar, Bangalore', 12.973541, 77.544397, 'Food & Grocery', 'silver', 3.6, 103, ''),
    ('Janatha Upahara', 'Vijayanagar, Bangalore', 12.971329, 77.543851, 'Food & Grocery', 'free', 2.9, 51, ''),
    ('Sim City', 'Vijayanagar, Bangalore', 12.967889, 77.535087, 'Electronics', 'gold', 3.9, 1890, ''),
    ('Sri Sai Telecom Carl', '39/2, Sha Shi Marulayya Road, Vijayanagar, Bangalore', 12.96482, 77.53526, 'Electronics', 'bronze', 3.1, 139, '+918048658105'),
    ('Sri Raghavendra Upaahar', 'Vijayanagar, Bangalore', 12.968166, 77.529295, 'Food & Grocery', 'bronze', 3.5, 139, ''),
    ('Gangotri', 'Vijayanagar, Bangalore', 12.968418, 77.532447, 'Food & Grocery', 'free', 2.9, 13, ''),
    ('Mangalore Kabab Korner', 'Vijayanagar, Bangalore', 12.972803, 77.528494, 'Food & Grocery', 'gold', 4.6, 1673, ''),
    ('Samudra', 'Vijayanagar, Bangalore', 12.97619, 77.533116, 'Food & Grocery', 'bronze', 3.4, 36, ''),
    ('Brahmi Ruchi', 'Vijayanagar, Bangalore', 12.96646, 77.534428, 'Food & Grocery', 'bronze', 4.0, 144, ''),
    ('Udupi Aruna Grand', 'Vijayanagar, Bangalore', 12.966501, 77.534313, 'Food & Grocery', 'free', 3.4, 14, ''),
    ('South spicy bites', 'Vijayanagar, Bangalore', 12.966261, 77.535061, 'Food & Grocery', 'free', 2.7, 19, ''),
    ('Anusuya Silks', 'Vijayanagar, Bangalore', 12.971587, 77.538018, 'Clothing', 'gold', 3.8, 537, ''),
    ('Daffodils', 'Vijayanagar, Bangalore', 12.972978, 77.538167, 'Clothing', 'bronze', 3.0, 173, ''),
    ('Boots Bazar', 'Vijayanagar, Bangalore', 12.973796, 77.543745, 'Clothing', 'silver', 3.5, 463, ''),
    ('Pappu Chaiwalla', 'Vijayanagar, Bangalore', 12.968297, 77.536114, 'Food & Grocery', 'bronze', 3.4, 141, ''),
    ('Hi-Tech Fashion', 'Vijayanagar, Bangalore', 12.97254, 77.537729, 'Clothing', 'free', 2.9, 2, ''),
    ('Haircraft Salon and Spa', 'Vijayanagar, Bangalore', 12.964738, 77.530733, 'Beauty & Personal Care', 'platinum', 4.7, 2710, ''),
    ('Khushal Medicals', 'Vijayanagar, Bangalore', 12.975938, 77.531497, 'Health & Wellness', 'free', 2.9, 18, ''),
    ('Sowbagya Enterprises', '21st Main Road, Vijayanagar, Bangalore', 12.968267, 77.533551, 'Stationery', 'free', 3.1, 38, ''),
    ('Ranganath Iyyangar Bakery', '21st Main Road, Vijayanagar, Bangalore', 12.968354, 77.533586, 'Food & Grocery', 'gold', 4.2, 959, ''),
    ('Gandhi Emporium', 'Vijayanagar, Bangalore', 12.973979, 77.536057, 'Clothing', 'bronze', 3.7, 86, ''),
    ('Advika medicals', 'Vijayanagar, Bangalore', 12.973993, 77.536022, 'Health & Wellness', 'free', 2.8, 54, ''),
    ('Shree Radhakrishna Fashion', 'Vijayanagar, Bangalore', 12.974027, 77.53595, 'Clothing', 'bronze', 3.8, 62, ''),
    ('The Bunt Kitchen', 'Number 38,, Ground Floor, 8th Main Rd, M C Layout, Vijayanagar, Vijayanagar, Bangalore', 12.974076, 77.534503, 'Food & Grocery', 'free', 3.6, 61, ''),
    ('Rajanna Military Hotel', 'Vijayanagar, Bangalore', 12.96624, 77.535132, 'Food & Grocery', 'bronze', 3.7, 61, ''),
    ('Fresho', 'Vijayanagar, Bangalore', 12.97093, 77.536605, 'Food & Grocery', 'platinum', 4.4, 3687, ''),
    ('Desi Store', 'Vijayanagar, Bangalore', 12.967875, 77.53859, 'Clothing', 'gold', 4.3, 1930, ''),
    ("Preity's Silks", 'Vijayanagar, Bangalore', 12.96529, 77.537203, 'Clothing', 'silver', 3.5, 391, ''),
    ('Oxford Saloon', 'Vijayanagar, Bangalore', 12.9654, 77.537065, 'Beauty & Personal Care', 'silver', 3.5, 390, ''),
    ('Shivani Organics', 'Vijayanagar, Bangalore', 12.965424, 77.53783, 'Food & Grocery', 'platinum', 4.9, 3092, ''),
    ('Rajajinagar Bakery', 'Vijayanagar, Bangalore', 12.965374, 77.538378, 'Food & Grocery', 'free', 3.6, 18, ''),
    ('Sri Balaji General Store & Stationery', 'Vijayanagar, Bangalore', 12.965482, 77.538393, 'Stationery', 'bronze', 3.3, 192, ''),
    ('Maruti Medicals', 'Vijayanagar, Bangalore', 12.963286, 77.534605, 'Health & Wellness', 'bronze', 3.5, 20, ''),
    ("Odeon - Men's Beauty Salon", 'Vijayanagar, Bangalore', 12.975647, 77.536065, 'Beauty & Personal Care', 'platinum', 4.9, 1529, ''),
    ('Devi Medical', 'Vijayanagar, Bangalore', 12.973816, 77.536535, 'Health & Wellness', 'silver', 4.0, 346, ''),
    ('Suraksha Medicals', 'Vijayanagar, Bangalore', 12.971203, 77.536518, 'Health & Wellness', 'free', 2.9, 24, ''),
    ('Mobile House', 'Vijayanagar, Bangalore', 12.970948, 77.53702, 'Electronics', 'bronze', 3.5, 62, ''),
    ('Madhur Milan', 'Vijayanagar, Bangalore', 12.969969, 77.536748, 'Clothing', 'silver', 3.6, 157, ''),
    ('Dear Foods', 'Vijayanagar, Bangalore', 12.975243, 77.536694, 'Food & Grocery', 'platinum', 5.0, 617, ''),
    ('Tejas Computers', 'Vijayanagar, Bangalore', 12.975476, 77.535926, 'Electronics', 'bronze', 3.4, 97, ''),
    ('Horizon Medicals', 'Vijayanagar, Bangalore', 12.975468, 77.535692, 'Health & Wellness', 'gold', 4.2, 1877, ''),
    ('Hari Om Electricals', 'Vijayanagar, Bangalore', 12.975503, 77.535563, 'Electronics', 'free', 3.2, 58, ''),
    ('Sri Ranga Medicals', 'Vijayanagar, Bangalore', 12.975647, 77.53582, 'Health & Wellness', 'bronze', 3.4, 158, '+919916050298'),
    ("Surya Iyengar's Bakery", 'Vijayanagar, Bangalore', 12.975669, 77.535531, 'Food & Grocery', 'bronze', 4.0, 105, ''),
    ('Sri Krishna Vaibhav', 'Vijayanagar, Bangalore', 12.967169, 77.535572, 'Food & Grocery', 'free', 2.9, 54, ''),
    ('Seema Shoppe', 'Vijayanagar, Bangalore', 12.974265, 77.535387, 'Food & Grocery', 'bronze', 3.6, 61, ''),
    ('Kamal collections', '775, 5th Main Road, Vijayanagar, Bangalore', 12.971159, 77.544999, 'Clothing', 'bronze', 3.4, 188, '+919945737503'),
    ('MK Ahmed Super Market', 'Mallathahalli Road, Nagarbhavi, Bangalore', 12.960756, 77.507006, 'Food & Grocery', 'silver', 3.8, 195, ''),
    ('Shoprite', 'Nagarbhavi 80 Feet Road, Nagarbhavi, Bangalore', 12.960669, 77.512225, 'Food & Grocery', 'silver', 4.3, 488, ''),
    ('Ron-Inn Bar & Restaurant', 'Nagarbhavi, Bangalore', 12.960313, 77.513258, 'Food & Grocery', 'free', 2.8, 13, ''),
    ('Nammura Tindi', 'Nagarbhavi, Bangalore', 12.960672, 77.51251, 'Food & Grocery', 'free', 2.7, 52, ''),
    ('Swathi Restaurant', 'Nagarbhavi, Bangalore', 12.960897, 77.507509, 'Food & Grocery', 'free', 3.4, 79, ''),
    ('Greens Unisex Salon And Spa Professionals', '#626, Mallathahalli Road, Nagarbhavi, Bangalore', 12.961106, 77.508928, 'Beauty & Personal Care', 'gold', 3.8, 1009, '+918033566078'),
    ('Green Trends Unisex Salon and Spa', 'No 690,, 1st Floor, 80 Feet Road,Vinayaka Layout,\xa0Nagarbhavi,, Nagarbhavi, Bangalore', 12.962612, 77.508385, 'Beauty & Personal Care', 'bronze', 3.8, 190, '+9191)8033700880'),
    ('Canteen NLSIU', 'Nagarbhavi, Bangalore', 12.955298, 77.51712, 'Food & Grocery', 'free', 3.1, 28, ''),
    ('Hafele Appliances', 'Nagarbhavi, Bangalore', 12.959385, 77.515674, 'Hardware', 'silver', 4.0, 108, ''),
    ('Cafe 363 x Neu Leaves', 'Nagarbhavi, Bangalore', 12.966304, 77.509073, 'Food & Grocery', 'free', 3.2, 15, ''),
    ('AIT Canteen', 'Nagarbhavi, Bangalore', 12.964599, 77.505218, 'Food & Grocery', 'bronze', 3.8, 138, ''),
    ('IOC Puncture shop', 'Kengeri, Bangalore', 12.915198, 77.482014, 'Automotive', 'bronze', 3.8, 200, ''),
    ('Shanthala Bakery', 'Kengeri, Bangalore', 12.910245, 77.483097, 'Food & Grocery', 'silver', 3.8, 241, ''),
    ('TEAGO Cafe', 'Kengeri, Bangalore', 12.921299, 77.483858, 'Food & Grocery', 'silver', 3.9, 187, ''),
    ('Sarasa Japanese Kitchen', 'Kengeri, Bangalore', 12.923581, 77.484704, 'Food & Grocery', 'gold', 4.0, 1817, ''),
    ('Raj Cafe', 'Kengeri, Bangalore', 12.907871, 77.476983, 'Food & Grocery', 'gold', 4.6, 1478, ''),
    ('Kadamba Veg', 'Kengeri, Bangalore', 12.913884, 77.487012, 'Food & Grocery', 'free', 3.6, 53, ''),
    ("Jack's Town", 'Kengeri, Bangalore', 12.906955, 77.475269, 'Food & Grocery', 'free', 2.7, 47, ''),
]

# Product catalogue per category — used when creating catalogue items
CATEGORY_PRODUCTS = {
    "Food & Grocery": [
        # ── Generic search aliases ───────────────────────────────────────────────
        "Fruits", "Fruit", "Vegetables", "Vegetable", "Veggies",
        "Groceries", "Grocery", "Staples", "Dairy", "Eggs",
        "Snacks", "Beverages", "Drinks", "Sweets", "Spices",
        "Bread", "Bakery", "Noodles", "Pasta",
        "Puja Items", "Agarbatti", "Incense", "Tobacco", "Pan",

        # ── Fruits ──────────────────────────────────────────────────────────────
        # Mango varieties
        "Mango", "Alphonso Mango", "Badami Mango", "Totapuri Mango", "Dasheri Mango",
        "Langra Mango", "Kesar Mango", "Neelam Mango", "Rajapuri Mango", "Chausa Mango",
        "Dussehari Mango", "Safeda Mango", "Himayat Mango", "Sindura Mango",
        "Banganapalli Mango", "Raspuri Mango", "Imam Pasand Mango",
        # Banana varieties
        "Banana", "Robusta Banana", "Nendran Banana", "Red Banana", "Green Banana",
        "Elaichi Banana (Yelakki)", "Cavendish Banana", "Poovan Banana", "Plantain",
        # Apple varieties
        "Apple", "Shimla Apple", "Royal Gala Apple", "Fuji Apple", "Green Apple",
        "Granny Smith Apple", "Golden Delicious Apple", "Pink Lady Apple",
        "Braeburn Apple", "Honeycrisp Apple", "McIntosh Apple",
        # Grapes
        "Grapes", "Green Grapes", "Black Grapes", "Red Grapes", "Seedless Grapes",
        "Muscat Grapes", "Thompson Seedless Grapes", "Flame Seedless Grapes",
        # Melons
        "Watermelon", "Seedless Watermelon", "Yellow Watermelon",
        "Muskmelon", "Honeydew Melon", "Canary Melon", "Galia Melon",
        # Citrus
        "Orange", "Mosambi (Sweet Lime)", "Mandarin", "Tangerine", "Clementine",
        "Grapefruit", "Pomelo", "Blood Orange", "Lemon", "Lime", "Kaffir Lime",
        # Tropical
        "Papaya", "Raw Papaya", "Green Papaya",
        "Pineapple", "Jackfruit", "Raw Jackfruit", "Tender Jackfruit",
        "Coconut", "Tender Coconut", "Dried Coconut",
        "Guava", "Pink Guava", "White Guava",
        "Pomegranate", "Custard Apple (Sitaphal)", "Sapota (Chikoo)",
        "Dragon Fruit (Red)", "Dragon Fruit (White)", "Dragon Fruit (Yellow)",
        "Star Fruit (Carambola)", "Passion Fruit", "Mangosteen",
        "Rambutan", "Longan", "Lychee (Litchi)", "Breadfruit",
        "Durian", "Soursop", "Wood Apple (Bel)", "Rose Apple",
        # Berries & small fruits
        "Berry", "Berries",
        "Strawberry", "Wild Strawberry", "Garden Strawberry",
        "Raspberry", "Red Raspberry", "Golden Raspberry", "Black Raspberry",
        "Blueberry", "Wild Blueberry", "Highbush Blueberry",
        "Blackberry", "Thornless Blackberry",
        "Mulberry", "Mulberry (White)", "Mulberry (Black)", "Mulberry (Red)",
        "Gooseberry", "Indian Gooseberry", "Cape Gooseberry (Physalis)",
        "Cranberry", "Dried Cranberry",
        "Boysenberry", "Loganberry", "Marionberry", "Tayberry",
        "Elderberry", "Elderflower",
        "Huckleberry", "Bilberry", "Lingonberry", "Cloudberry",
        "Barberry (Zereshk)", "Wolfberry (Goji Berry)", "Goji Berry",
        "Acai Berry", "Maqui Berry", "Miracle Berry",
        "Sea Buckthorn Berry", "Sea Buckthorn",
        "Amla (Indian Gooseberry)", "Amla",
        "Jamun (Black Plum)", "Jamun", "Karonda", "Karonda (Natal Plum)",
        "Falsa (Phalsa Berry)", "Phalsa",
        "Kiwi Berry", "Mini Kiwi",
        "Currant", "Black Currant", "Red Currant", "White Currant",
        "Dewberry", "Salmonberry", "Thimbleberry",
        # Stone fruits
        "Plum", "Red Plum", "Yellow Plum", "Green Plum",
        "Peach", "White Peach", "Yellow Peach",
        "Apricot", "Cherry", "Red Cherry", "Black Cherry",
        "Nectarine", "Damson",
        # Pome fruits
        "Pear", "Williams Pear", "Bosc Pear", "Asian Pear",
        "Quince", "Loquat",
        # Dried / other fruits
        "Avocado", "Kiwi", "Kiwi Berry",
        "Dates (Khajur)", "Medjool Dates", "Dry Dates",
        "Fig (Anjeer)", "Fresh Fig", "Dried Fig",
        "Tamarind", "Kokum", "Bael Fruit",
        "Ber (Indian Jujube)", "Indian Plum",
        "Persimmon", "Kumquat", "Finger Lime",
        "Water Chestnut", "Lotus Fruit", "Lotus Seeds",
        "Acai Berry", "Sea Buckthorn", "Mulberry (White)",

        # ── Vegetables ──────────────────────────────────────────────────────────
        # Nightshades & fruiting
        "Tomato", "Cherry Tomato", "Roma Tomato", "Heirloom Tomato",
        "Capsicum (Green)", "Capsicum (Red)", "Capsicum (Yellow)", "Capsicum (Orange)",
        "Brinjal (Baingan)", "Small Brinjal", "Long Brinjal", "White Brinjal",
        "Green Chilli", "Red Chilli", "Bird's Eye Chilli", "Kashmiri Chilli",
        "Yellow Chilli", "Banana Pepper",
        # Alliums
        "Onion", "Red Onion", "White Onion", "Shallots", "Spring Onion",
        "Leek", "Garlic", "Black Garlic", "Elephant Garlic",
        "Chives", "Welsh Onion",
        # Root & tuber vegetables
        "Potato", "Baby Potato", "Purple Potato", "Sweet Potato",
        "Carrot", "Purple Carrot", "Baby Carrot",
        "Radish", "White Radish (Mooli)", "Red Radish", "Daikon Radish",
        "Beetroot", "Golden Beetroot", "Turnip", "Parsnip",
        "Ginger", "Galangal", "Turmeric (Fresh)",
        "Yam (Suran)", "Elephant Foot Yam", "Purple Yam (Ratalu)",
        "Taro Root (Arbi)", "Chinese Potato (Kochu)", "Cassava (Tapioca)",
        "Jerusalem Artichoke", "Celeriac", "Rutabaga",
        # Gourds
        "Bottle Gourd (Lauki)", "Ridge Gourd (Turai)", "Snake Gourd",
        "Bitter Gourd (Karela)", "Ash Gourd (Winter Melon)", "Pumpkin",
        "Ivy Gourd (Tindora / Kundru)", "Round Gourd (Tinda)",
        "Pointed Gourd (Parwal)", "Spine Gourd (Kantola / Kakrol)",
        "Sponge Gourd (Gilki)", "Chow Chow (Chayote)", "Zucchini",
        "Butternut Squash", "Acorn Squash", "Kabocha Squash",
        "Yellow Squash", "Spaghetti Squash",
        # Brassicas
        "Cabbage", "Red Cabbage", "Savoy Cabbage", "Napa Cabbage",
        "Cauliflower", "Purple Cauliflower", "Orange Cauliflower",
        "Broccoli", "Broccolini", "Romanesco Broccoli",
        "Brussels Sprouts", "Kohlrabi (Knol Khol)", "Kale",
        "Bok Choy (Pak Choi)", "Choy Sum", "Gai Lan",
        "Collard Greens", "Mustard Greens",
        # Leafy greens
        "Spinach", "Baby Spinach", "Red Spinach",
        "Methi (Fenugreek Leaves)", "Kasuri Methi",
        "Amaranth Leaves (Rajgira / Chauli Saag)",
        "Coriander (Dhania)", "Mint Leaves (Pudina)", "Curry Leaves",
        "Dill (Shepu / Suva)", "Parsley", "Basil (Tulsi)",
        "Lemongrass", "Pandan Leaves",
        "Drumstick Leaves (Moringa)", "Colocasia Leaves (Arbi Patta)",
        "Banana Leaves", "Banana Flower (Vazhaipoo)", "Banana Stem",
        "Sorrel (Gongura)", "Red Sorrel",
        "Water Spinach (Kangkong)", "Purslane",
        "Iceberg Lettuce", "Romaine Lettuce", "Butterhead Lettuce",
        "Arugula (Rocket)", "Watercress", "Swiss Chard", "Rainbow Chard",
        "Endive", "Radicchio",
        # Stems, shoots & pods
        "Asparagus", "Celery", "Fennel", "Artichoke",
        "Lotus Stem", "Bamboo Shoots", "Baby Corn", "Corn (Maize)",
        # Beans & peas
        "Peas (Matar)", "Snow Peas", "Sugar Snap Peas", "Mangetout",
        "Lady Finger (Okra / Bhindi)", "French Beans", "Runner Beans",
        "Flat Beans", "Broad Beans", "Cluster Beans (Guar / Gawar)",
        "Hyacinth Bean (Papdi / Val)", "Winged Bean", "Sword Bean",
        "Edamame", "Fava Bean", "Lima Bean", "Butter Bean",
        "Green Pigeon Peas (Fresh Toor)", "Green Bengal Gram (Hara Chana)",
        # Cucumbers
        "Cucumber", "English Cucumber", "Persian Cucumber",
        "Japanese Cucumber", "Lemon Cucumber",
        # Mushrooms
        "Button Mushroom", "Oyster Mushroom", "Shiitake Mushroom",
        "Portobello Mushroom", "Cremini Mushroom",
        "Milky Mushroom", "Straw Mushroom", "King Oyster Mushroom",
        # Raw / unripe forms
        "Raw Mango", "Raw Banana", "Raw Papaya", "Raw Jackfruit",
        "Drumstick (Moringa Pods)",

        # ── Staples ─────────────────────────────────────────────────────────────
        "Rice", "Basmati Rice", "Sona Masuri Rice", "Idli Rice", "Brown Rice",
        "Wheat Flour (Atta)", "Maida (Refined Flour)", "Sooji (Semolina)", "Besan (Chickpea Flour)",
        "Poha (Flattened Rice)", "Puffed Rice (Murmura)", "Sabudana (Tapioca)", "Ragi Flour",
        "Toor Dal", "Chana Dal", "Moong Dal", "Urad Dal", "Masoor Dal", "Rajma",
        "Kabuli Chana", "Green Moong", "Black Chana", "Peas Dal",

        # ── Oils & Fats ─────────────────────────────────────────────────────────
        "Sunflower Oil", "Coconut Oil", "Mustard Oil", "Groundnut Oil",
        "Olive Oil", "Rice Bran Oil", "Palm Oil", "Sesame Oil",
        "Butter", "Ghee (Cow)", "Ghee (Buffalo)", "Margarine",

        # ── Dairy & Eggs ─────────────────────────────────────────────────────────
        "Milk (Full Cream)", "Milk (Toned)", "Milk (Skimmed)", "Flavoured Milk",
        "Curd", "Greek Yogurt", "Buttermilk (Chaas)", "Lassi",
        "Paneer", "Cheese Slice", "Cheese Block", "Cream Cheese",
        "Fresh Cream", "Whipping Cream", "Condensed Milk",
        "Eggs (White)", "Eggs (Brown)", "Quail Eggs",

        # ── Snacks & Namkeen ─────────────────────────────────────────────────────
        "Lays Chips", "Bingo Mad Angles", "Kurkure", "Haldiram's Bhujia",
        "Haldiram's Aloo Bhujia", "Haldiram's Mixture", "Haldiram's Namkeen",
        "Chakli", "Murukku", "Ribbon Pakoda", "Thattai", "Kodubale",
        "Roasted Peanuts", "Masala Peanuts", "Cashews", "Almonds",
        "Walnuts", "Pistachios", "Raisins", "Mixed Dry Fruits",
        "Pringles", "Doritos", "Nachos",

        # ── Biscuits & Cookies ───────────────────────────────────────────────────
        "Parle-G Biscuits", "Marie Gold Biscuits", "Good Day Biscuits",
        "Bourbon Biscuits", "Hide & Seek Biscuits", "Oreo Biscuits",
        "Monaco Biscuits", "Krackjack Biscuits", "Digestive Biscuits",
        "Milkikis Biscuits", "50-50 Biscuits", "Nutrichoice Biscuits",
        "Cream Biscuits", "Jim Jam Biscuits", "Unibic Cookies",

        # ── Bread & Bakery ───────────────────────────────────────────────────────
        "White Bread", "Brown Bread", "Multigrain Bread", "Whole Wheat Bread",
        "Pav (Dinner Rolls)", "Burger Buns", "Hot Dog Buns",
        "Rusk", "Bread Crumbs", "Pita Bread", "Sandwich Loaf",
        "Cake (Chocolate)", "Cake (Vanilla)", "Muffins", "Cookies",

        # ── Noodles, Pasta & Ready Mix ───────────────────────────────────────────
        "Maggi Masala Noodles", "Maggi Chicken Noodles", "Yippee Noodles",
        "Top Ramen Noodles", "Knorr Soupy Noodles",
        "Pasta (Penne)", "Pasta (Spaghetti)", "Pasta (Fusilli)",
        "Sewai (Vermicelli)", "Hakka Noodles",
        "Upma Mix", "Poha Mix", "Idli Mix", "Dosa Mix", "Khichdi Mix",

        # ── Beverages ────────────────────────────────────────────────────────────
        "Tea (Tata)", "Tea (Red Label)", "Tea (Taj Mahal)", "Green Tea", "Masala Tea",
        "Coffee (Nescafe)", "Coffee (Bru)", "Filter Coffee Powder", "Cold Coffee",
        "Fruit Juice (Tropicana)", "Fruit Juice (Real)", "Mango Juice", "Orange Juice",
        "Soft Drink (Coca-Cola)", "Soft Drink (Pepsi)", "Sprite", "Fanta", "Thums Up",
        "Limca", "Maaza", "Slice", "Frooti", "Paper Boat Aamras",
        "Energy Drink (Red Bull)", "Energy Drink (Monster)",
        "Coconut Water", "Packaged Drinking Water", "Soda Water",
        "Horlicks", "Bournvita", "Complan", "Milo",

        # ── Sweets, Chocolate & Confectionery ────────────────────────────────────
        "Cadbury Dairy Milk", "Cadbury 5 Star", "Kit Kat", "Munch",
        "Perk", "Snickers", "Bounty", "Toblerone", "Ferrero Rocher",
        "Polo Mints", "Mentos", "Eclairs", "Hajmola", "Pan Masala",
        "Chewing Gum (Orbit)", "Lollipop",
        "Gulab Jamun (Pack)", "Rasgulla (Can)", "Halwa Mix",
        "Mithai Box (Assorted)",

        # ── Spices & Masalas ─────────────────────────────────────────────────────
        "Turmeric Powder", "Red Chilli Powder", "Coriander Powder",
        "Cumin Seeds", "Mustard Seeds", "Pepper (Black)", "Cardamom (Elaichi)",
        "Cinnamon", "Cloves", "Bay Leaves", "Star Anise",
        "Garam Masala", "Biryani Masala", "Sambar Powder", "Rasam Powder",
        "Chaat Masala", "Kitchen King Masala", "Chicken Masala", "Pav Bhaji Masala",
        "Chole Masala", "Fish Curry Masala",

        # ── Condiments & Sauces ──────────────────────────────────────────────────
        "Tomato Ketchup (Kissan)", "Tomato Ketchup (Heinz)", "Chilli Sauce",
        "Soy Sauce", "Vinegar", "Worcestershire Sauce",
        "Mango Pickle (Avakaya)", "Lemon Pickle", "Mixed Vegetable Pickle",
        "Jam (Strawberry)", "Jam (Mixed Fruit)", "Honey", "Peanut Butter",
        "Mayonnaise", "Mustard Sauce", "Salad Dressing",

        # ── Household & Cleaning ─────────────────────────────────────────────────
        "Detergent Powder (Surf Excel)", "Detergent Powder (Ariel)", "Detergent Liquid",
        "Dishwash Bar (Vim)", "Dishwash Liquid (Pril)", "Dishwash Gel",
        "Floor Cleaner (Phenyl)", "Floor Cleaner (Lizol)", "Toilet Cleaner (Harpic)",
        "Glass Cleaner (Colin)", "Multi-Surface Cleaner",
        "Washing Soap (Rin)", "Fabric Softener",
        "Room Freshener (Odonil)", "Air Freshener Spray", "Mosquito Repellent (All Out)",
        "Mosquito Coil", "Cockroach Killer", "Rat Poison",
        "Garbage Bags", "Tissue Paper", "Toilet Paper Rolls", "Kitchen Towels",
        "Aluminium Foil", "Cling Wrap", "Zip Lock Bags", "Paper Plates",
        "Matchbox", "Candles",

        # ── Packaged & Frozen Foods ──────────────────────────────────────────────
        "Cornflakes (Kellogg's)", "Oats (Quaker)", "Muesli", "Granola",
        "Frozen Peas", "Frozen Corn", "Frozen Mixed Vegetables",
        "Ready to Eat Rajma", "Ready to Eat Dal Makhani", "Ready to Eat Palak Paneer",
        "Canned Chickpeas", "Canned Tomatoes", "Tomato Puree",
        "Ice Cream (Vanilla)", "Ice Cream (Chocolate)", "Kulfi",

        # ── Puja / Religious Items (provision & kirana stores) ────────────────
        "Agarbatti (Cycle Brand)", "Agarbatti (Hem Gold)", "Agarbatti (Morning Fresh)",
        "Agarbatti (Nandita)", "Agarbatti (Satya Sai Baba Nag Champa)",
        "Dhoop Sticks", "Dhoop Cones", "Sambrani (Loban)",
        "Camphor Tablets", "Camphor Powder (Pacha Karpooram)",
        "Kumkum (Sindoor)", "Turmeric Powder (Puja)", "Vibhuti (Holy Ash)",
        "Chandan Powder (Sandalwood)", "Haldi-Kumkum Pack",
        "Puja Flowers (Loose)", "Marigold Garland", "Rose Petals (Dry)",
        "Coconut (Puja)", "Banana (Puja Bunch)", "Banana Leaf",
        "Betel Leaves (Pan Patta)", "Betel Nut (Supari)",
        "Kasturi Manjal (Wild Turmeric)", "Neem Leaves (Dry)", "Tulsi Leaves (Dry)",
        "Matchbox (Agni)", "Wick (Batti) Cotton", "Ghee Deepam Wick",
        "Oil Lamp (Deepa)", "Clay Diya (Pack of 10)", "Brass Diya",
        "Puja Thali Set", "Kalasha (Pot)", "Samarani Vessel",
        "Holy Ganga Water", "Panchamrit Mix",
        "God Idol (Ganesha Small)", "God Idol (Lakshmi Small)",
        "Puja Book (Satyanarayana Katha)", "Almanac (Panchanga)",

        # ── Pan / Mouth Freshener ─────────────────────────────────────────────
        "Fennel Seeds (Saunf) Roasted", "Mishri (Rock Sugar)", "Mukhwas Mix",
        "Cardamom (Green)", "Clove (Lavang)", "Pan Masala (Rajnigandha Silver)",
        "Pan Masala (Baba Silver Karat)", "Tulsi Pan Masala",
        "Chewing Tobacco (Zarda)", "Khaini Tobacco",
        "Cigarettes (Gold Flake Kings)", "Cigarettes (Classic Milds)",
        "Cigarettes (India Kings)", "Cigarettes (Bristol)",
        "Cigarettes (Four Square)", "Beedi (Ganesh)", "Beedi (501)",

        # ── Loose / Bulk Dry Goods (provision stores) ─────────────────────────
        "Loose Basmati Rice (per kg)", "Loose Sona Masuri (per kg)",
        "Loose Toor Dal (per kg)", "Loose Moong Dal (per kg)",
        "Loose Chana Dal (per kg)", "Loose Urad Dal (per kg)",
        "Loose Masoor Dal (per kg)", "Loose Rajma (per kg)",
        "Loose Groundnuts (per kg)", "Loose Sesame Seeds (per kg)",
        "Loose Dry Coconut (per kg)", "Loose Tamarind (per kg)",
        "Jaggery Block", "Jaggery Powder", "Palm Jaggery",
        "Rock Salt (per kg)", "Iodised Salt (loose)", "Sea Salt",
        "Sugar (Loose)", "Brown Sugar", "Icing Sugar",
        "Cardamom (Bulk)", "Pepper (Bulk)", "Dry Red Chilli (Bulk)",
    ],

    "Electronics": [
        # ── Generic search aliases (enable plain-text queries like "phones", "laptop") ──
        "Smartphones", "Mobile Phones", "Phones", "Phone",
        "Laptops", "Laptop", "Computers", "Computer",
        "Tablets", "Tablet",
        "Headphones", "Earphones", "Earbuds",
        "Cameras", "Camera",
        "Smartwatches", "Smartwatch",
        "TVs", "Television",
        "Chargers", "Accessories",

        # ── Smartphones ──────────────────────────────────────────────────────────
        "Samsung Galaxy S24", "Samsung Galaxy S24 Ultra", "Samsung Galaxy A55",
        "Samsung Galaxy A35", "Samsung Galaxy M34", "Samsung Galaxy F55",
        "iPhone 15", "iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 14", "iPhone 13",
        "OnePlus 12", "OnePlus 12R", "OnePlus Nord CE4", "OnePlus Nord 4",
        "Xiaomi 14", "Xiaomi 14 Ultra", "Redmi Note 13 Pro", "Redmi Note 13",
        "Redmi 13C", "Redmi 13", "Poco X6 Pro", "Poco F6",
        "Realme 12 Pro", "Realme 12 Pro+", "Realme Narzo 70 Pro", "Realme C67",
        "Vivo V30", "Vivo V30 Pro", "Vivo T3 Pro", "Vivo Y200", "Vivo Y58",
        "Oppo Reno 12 Pro", "Oppo F27 Pro", "Oppo A3 Pro", "Oppo A60",
        "Nothing Phone 2a", "Nothing Phone 2", "Nothing Phone 1",
        "Google Pixel 8a", "Google Pixel 8",
        "Motorola Edge 50 Pro", "Motorola Edge 50", "Motorola G85", "Motorola G64",
        "iQOO Z9 Pro", "iQOO Neo 9 Pro",
        "Tecno Spark 20 Pro", "Infinix Note 40 Pro",

        # ── Laptops ──────────────────────────────────────────────────────────────
        "HP Laptop 15", "HP Pavilion", "HP EliteBook",
        "Dell Inspiron 15", "Dell Vostro", "Dell XPS 13",
        "Lenovo IdeaPad", "Lenovo ThinkPad", "Lenovo Legion",
        "Asus VivoBook", "Asus ZenBook", "Asus ROG",
        "Acer Aspire", "Acer Swift", "Acer Nitro",
        "MacBook Air M2", "MacBook Pro 14",
        "Microsoft Surface Laptop",

        # ── Tablets ──────────────────────────────────────────────────────────────
        "iPad Air", "iPad Mini", "Samsung Galaxy Tab A9", "Lenovo Tab M10",
        "Realme Pad 2", "Xiaomi Pad 6",

        # ── Audio ────────────────────────────────────────────────────────────────
        "Wireless Earbuds (boAt)", "Wireless Earbuds (Sony)", "Wireless Earbuds (JBL)",
        "Noise Cancelling Headphones", "Over-ear Headphones (Sony WH-1000XM5)",
        "Gaming Headset", "Bluetooth Speaker (JBL)", "Bluetooth Speaker (boAt)",
        "Soundbar", "Smart Speaker (Amazon Echo)", "Smart Speaker (Google Nest)",

        # ── TV & Display ─────────────────────────────────────────────────────────
        "Smart TV 43 inch", "Smart TV 55 inch", "OLED TV 65 inch",
        "Monitor 24 inch", "Monitor 27 inch", "Projector",

        # ── Accessories ──────────────────────────────────────────────────────────
        "USB-C Charger 65W", "MagSafe Charger", "Wireless Charger",
        "Power Bank 10000mAh", "Power Bank 20000mAh",
        "Phone Case", "Screen Protector (Tempered Glass)",
        "HDMI Cable", "USB Hub", "SD Card", "Pen Drive 64GB",
        "Keyboard (Wireless)", "Mouse (Wireless)", "Webcam",
        "Router (Wi-Fi 6)", "Smart Watch", "Fitness Band",
        "Action Camera (GoPro)", "DSLR Camera",
    ],

    "Clothing": [
        # ── Men's Tops ────────────────────────────────────────────────────────
        "Men's Round Neck T-Shirt", "Men's V-Neck T-Shirt", "Men's Polo T-Shirt",
        "Men's Graphic T-Shirt", "Men's Oversized T-Shirt", "Men's Full Sleeve T-Shirt",
        "Men's Formal Shirt (White)", "Men's Formal Shirt (Blue)", "Men's Casual Shirt",
        "Men's Linen Shirt", "Men's Denim Shirt", "Men's Flannel Shirt",
        "Men's Sweatshirt", "Men's Hoodie (Zip-up)", "Men's Hoodie (Pullover)",
        "Men's Crew Neck Sweater", "Men's Cardigan",
        # ── Men's Bottoms ─────────────────────────────────────────────────────
        "Men's Jeans (Slim Fit)", "Men's Jeans (Regular)", "Men's Jeans (Skinny)",
        "Men's Jeans (Straight Cut)", "Men's Jeans (Bootcut)",
        "Men's Chinos (Khaki)", "Men's Chinos (Olive)", "Men's Chinos (Navy)",
        "Men's Formal Trousers (Black)", "Men's Formal Trousers (Grey)",
        "Men's Cargo Pants", "Men's Track Pants", "Men's Shorts (Regular)",
        "Men's Joggers", "Men's Linen Trousers",
        # ── Men's Ethnic ─────────────────────────────────────────────────────
        "Men's Kurta (Cotton White)", "Men's Kurta (Silk)", "Men's Kurta (Linen)",
        "Men's Kurta Pyjama Set (Cotton)", "Men's Kurta Pyjama Set (Silk)",
        "Men's Sherwani", "Men's Nehru Jacket", "Men's Bandhgala Suit",
        "Men's Dhoti (White Cotton)", "Men's Panche (Veshti)",
        "Men's Lungi (Checked)", "Men's Lungi (Plain White)",
        "Men's Pathani Suit", "Men's Kaftan",
        # ── Men's Outerwear ───────────────────────────────────────────────────
        "Men's Blazer (Black)", "Men's Blazer (Navy)", "Men's Blazer (Grey Check)",
        "Men's Suit (2-piece)", "Men's Suit (3-piece)", "Men's Waistcoat",
        "Men's Bomber Jacket", "Men's Windcheater", "Men's Denim Jacket",
        "Men's Leather Jacket", "Men's Padded Jacket",
        # ── Men's Innerwear ───────────────────────────────────────────────────
        "Men's Briefs (Rupa Frontline)", "Men's Briefs (Lux Cozi)",
        "Men's Trunks (VIP)", "Men's Trunks (Dollar Ultra)",
        "Men's Boxer Shorts (Cotton)", "Men's Vest (Baniyan)",
        "Men's Thermal Vest", "Men's Thermal Bottom",
        # ── Men's Socks, Belts & Accessories ─────────────────────────────────
        "Men's Ankle Socks (Pack of 3)", "Men's Calf Socks (Pack of 3)",
        "Men's Formal Socks (Black)", "Men's Sports Socks",
        "Men's Belt (Formal Leather)", "Men's Belt (Casual Canvas)",
        "Men's Tie (Silk)", "Men's Tie (Polyester)", "Men's Pocket Square",
        "Men's Suspenders", "Men's Cufflinks Set",
        "Men's Wallet (Leather)", "Men's Wallet (Slim)",
        "Men's Sunglasses", "Men's Cap (Baseball)", "Men's Cap (Solid)",
        # ── Women's Kurtis & Ethnic ───────────────────────────────────────────
        "Women's Kurti (Cotton Printed)", "Women's Kurti (Rayon)",
        "Women's Kurti (Silk)", "Women's Kurti (Georgette)",
        "Women's Kurti (A-line)", "Women's Kurti (Straight Cut)",
        "Women's Kurta Set (Cotton)", "Women's Kurta Set (Silk)",
        "Women's Anarkali (Georgette)", "Women's Anarkali (Net)",
        "Women's Salwar Kameez (Punjabi)", "Women's Salwar Kameez (Patiala)",
        "Women's Palazzo Suit Set", "Women's Sharara Set",
        "Women's Dupatta (Chiffon)", "Women's Dupatta (Cotton Block Print)",
        # ── Women's Sarees ─────────────────────────────────────────────────────
        "Women's Saree (Mysore Silk)", "Women's Saree (Kanjivaram Silk)",
        "Women's Saree (Banarasi Silk)", "Women's Saree (Chanderi Silk)",
        "Women's Saree (Cotton Handloom)", "Women's Saree (Tant Cotton)",
        "Women's Saree (Chiffon Printed)", "Women's Saree (Georgette Printed)",
        "Women's Saree (Tussar Silk)", "Women's Saree (Organza)",
        "Women's Saree (Linen Cotton)", "Women's Saree (Sambalpuri)",
        "Women's Saree (Patola)", "Women's Saree (Pochampally Ikat)",
        "Women's Half Saree Set (Langa Voni)", "Women's Pavada Davani Set",
        "Women's Lehenga Choli (Bridal)", "Women's Lehenga Choli (Party)",
        "Women's Blouse (Readymade)", "Women's Blouse (Fall Saree)",
        # ── Women's Western ──────────────────────────────────────────────────
        "Women's Top (Casual)", "Women's Top (Printed)", "Women's Crop Top",
        "Women's Tank Top", "Women's Off-Shoulder Top", "Women's Peplum Top",
        "Women's Blouse (Western)", "Women's Shirt", "Women's Tunic",
        "Women's Sweatshirt", "Women's Hoodie",
        "Women's Jeans (Skinny)", "Women's Jeans (Flared)", "Women's Jeans (Mom Fit)",
        "Women's Jeggings", "Women's Palazzo", "Women's Culottes",
        "Women's Skirt (A-line)", "Women's Skirt (Midi)", "Women's Mini Skirt",
        "Women's Shorts (Casual)", "Women's Bermuda Shorts",
        "Women's Dress (Casual)", "Women's Maxi Dress", "Women's Bodycon Dress",
        "Women's Wrap Dress", "Women's Shirt Dress",
        "Women's Dungaree", "Women's Co-ord Set",
        "Women's Jumpsuit", "Women's Playsuit",
        "Women's Cardigan", "Women's Sweater", "Women's Shrug",
        "Women's Blazer", "Women's Jacket (Denim)", "Women's Puffer Jacket",
        # ── Women's Innerwear ─────────────────────────────────────────────────
        "Women's Bra (T-shirt Bra)", "Women's Bra (Underwire)",
        "Women's Bra (Sports Bra)", "Women's Bra (Push-up)",
        "Women's Bra (Strapless)", "Women's Bra (Padded)",
        "Women's Panty (Brief)", "Women's Panty (Hipster)",
        "Women's Panty (Thong)", "Women's Panty (Boyshort)",
        "Women's Shapewear (Tummy Tucker)", "Women's Night Suit (Cotton)",
        "Women's Nighty (Long)", "Women's Nighty (Short)",
        "Women's Slip Dress (Innerwear)", "Women's Camisole",
        # ── Kids Clothing ─────────────────────────────────────────────────────
        "Kids T-Shirt (Boys)", "Kids T-Shirt (Girls)", "Kids Graphic Tee",
        "Kids Shirt", "Kids Frock (Casual)", "Kids Frock (Party)",
        "Kids Shorts", "Kids Jeans", "Kids Cargo Pants",
        "Kids Track Suit", "Kids Jogger Set", "Kids Leggings",
        "School Uniform Shirt (White)", "School Uniform Shirt (Blue)",
        "School Uniform Trouser (Grey)", "School Uniform Skirt (Checked)",
        "School Uniform Pinafore", "School Tie",
        "Kids Kurta Pyjama Set", "Kids Sherwani", "Girls Lehenga (Party)",
        "Baby Romper (0-6 months)", "Baby Onesie (Snap Button)",
        "Baby Frock Set", "Baby Pyjama Set", "Newborn Gift Set",
        # ── Footwear ──────────────────────────────────────────────────────────
        "Men's Formal Shoes (Oxford)", "Men's Formal Shoes (Derby)",
        "Men's Loafers (Leather)", "Men's Loafers (Suede)",
        "Men's Casual Sneakers", "Men's Running Shoes",
        "Men's Chappals (Rubber)", "Men's Sandals (Leather)",
        "Men's Kolhapuri Chappals", "Men's Hawai Chappals",
        "Women's Heels (Block Heel)", "Women's Heels (Stiletto)",
        "Women's Flats (Ballerina)", "Women's Wedge Sandals",
        "Women's Kolhapuri Sandals", "Women's Casual Sneakers",
        "Women's Casual Chappals", "Women's Platform Sandals",
        "Kids School Shoes", "Kids Sports Shoes", "Kids Sandals",
        "Bata Sandals (Men)", "Bata Sandals (Women)",
        "Woodland Shoes (Men)", "Red Tape Formal Shoes",
        # ── Scarves, Stoles & Head Accessories ───────────────────────────────
        "Scarf (Cotton)", "Stole (Chiffon)", "Dupatta (Plain Cotton)",
        "Shawl (Woollen)", "Muffler", "Pashmina Shawl",
        "Cap (Baseball)", "Cap (Sports Mesh)", "Cap (Solid Colour)",
        "Hat (Sun Hat)", "Hat (Fedora)", "Beanie (Woollen)",
        "Women's Hijab (Jersey)", "Women's Hijab (Printed)",
        # ── Ethnic Accessories ────────────────────────────────────────────────
        "Bindi Pack (Round)", "Bindi Pack (Fancy Stones)",
        "Mehndi Cone (Ready-to-use)", "Mehndi Powder",
        "Bangles Set (Glass)", "Bangles Set (Lac)", "Bangles Set (Metal)",
        "Anklet (Silver-plated)", "Toe Ring Set", "Maang Tikka",
        "Necklace Set (Imitation Gold)", "Earrings (Jhumka)",
        "Nose Pin (Stud)", "Payals (Anklet with Bells)",
    ],

    "Home & Kitchen": [
        # Cookware
        "Pressure Cooker (3L)", "Pressure Cooker (5L)", "Non-stick Kadai",
        "Non-stick Tawa", "Non-stick Pan", "Cast Iron Skillet",
        "Stainless Steel Kadai", "Idli Maker", "Appam Pan",
        "Steamer", "Colander/Strainer", "Mixing Bowl Set",
        # Appliances
        "Mixer Grinder", "Juicer Mixer Grinder", "Hand Blender",
        "Electric Kettle", "Toaster (Pop-up)", "Sandwich Maker",
        "Induction Cooktop", "Microwave Oven", "OTG Oven",
        "Air Fryer", "Rice Cooker", "Slow Cooker",
        "Water Purifier (RO)", "Water Filter Jug",
        "Refrigerator (Single Door)", "Washing Machine (Front Load)",
        # Storage & Dining
        "Steel Tiffin Box", "Lunch Box (3 Tier)", "Water Bottle (Steel)",
        "Water Bottle (Plastic)", "Thermos Flask",
        "Dinner Set (12 Piece)", "Casserole Set", "Serving Bowl Set",
        "Glass Set", "Mug Set", "Plate Set", "Cup and Saucer",
        "Airtight Container Set", "Fridge Organiser",
        # Cleaning
        "Mop", "Broom", "Dustpan", "Scrubbing Brush", "Sponge",
        "Bucket", "Dustbin (10L)", "Dustbin (25L)", "Recycling Bin",
        # Home Decor & Furnishing
        "Bedsheet (Double)", "Bedsheet (Single)", "Pillow",
        "Pillow Cover Set", "Blanket", "Quilt", "Mattress Protector",
        "Curtains (Pack of 2)", "Doormat", "Bath Mat", "Towel Set",
        "Wall Clock", "Photo Frame", "Table Lamp", "Floor Lamp",
        "Storage Box (Foldable)", "Wardrobe Organiser", "Shoe Rack",
        "Ironing Board", "Clothes Drying Stand",
    ],

    "Health & Wellness": [
        # ── OTC Pain / Fever / Cold ──────────────────────────────────────────
        "Paracetamol 500mg (Crocin)", "Dolo 650 Tablet", "Calpol Tablet",
        "Ibuprofen 400mg (Brufen)", "Combiflam Tablet", "Combiflam Plus",
        "Aspirin 75mg (Ecosprin)", "Disprin Tablet",
        "Nimesulide 100mg (Nimulid)", "Mefenamic Acid (Meftal Spas)",
        "D-Cold Total Tablet", "Sinarest Tablet", "Cetrizine 10mg",
        "Allegra 120mg (Fexofenadine)", "Levocetrizine 5mg",
        "Benadryl Cough Syrup", "Corex Cough Syrup", "Ascoril Syrup",
        "Honitus Cough Syrup (Dabur)", "Koflet Syrup (Himalaya)",
        "Vicks Vaporub (50g)", "Vicks Inhaler", "Nasivion Nasal Drops",
        "Otrivin Nasal Spray", "Betadine Gargle",
        # ── Antacids & Digestive ─────────────────────────────────────────────
        "Gelusil MPS Tablet", "Digene Tablet", "Digene Gel (Orange)",
        "Eno Fruit Salt (Regular)", "Eno Fruit Salt (Lemon)",
        "Pan 40 Tablet (Pantoprazole)", "Omeprazole 20mg", "Rabeprazole",
        "Ranitidine 150mg", "Famotidine 20mg",
        "Cremaffin Syrup", "Dulcolax Tablet", "Lactulose Syrup",
        "Isabgol Husk (Sat Isabgol)", "Metrogyl 400 (Metronidazole)",
        "Eldoper Capsule", "Norflox-TZ Tablet",
        "Velgut Probiotic", "Sporlac Probiotic Sachet",
        # ── Antibiotics (common OTC-dispensed in India) ─────────────────────
        "Amoxicillin 500mg", "Amoxicillin + Clavulanic Acid (Augmentin)",
        "Azithromycin 500mg (Zithromax)", "Azithromycin 250mg",
        "Ciprofloxacin 500mg", "Ofloxacin 200mg",
        "Doxycycline 100mg", "Fluconazole 150mg",
        # ── Topical / Skin Medicines ─────────────────────────────────────────
        "Betadine Ointment (Povidone-Iodine)", "Soframycin Cream",
        "Boroline Cream", "Boroline Night Cream",
        "Lacto Calamine Lotion", "Candid Cream (Clotrimazole)",
        "Panderm Cream", "Quadriderm Cream",
        "Fucidin Cream", "Dermiford Cream",
        "Volini Spray", "Volini Gel", "Moov Cream", "Moov Spray",
        "Iodex Cream", "Relispray",
        "Calamine Lotion (SBL)", "Burnol Cream",
        # ── Eye / Ear / Nose ─────────────────────────────────────────────────
        "Refresh Tears Eye Drops", "Systane Eye Drops",
        "Cipla Eye Drops (Ciplox)", "Tobramycin Eye Drops",
        "Waxsol Ear Drops", "Sofradex Ear Drops", "Otigo Ear Drops",
        "Otrivin Xtra Nasal Spray", "Flomist Nasal Spray",
        # ── Vitamins & Supplements ───────────────────────────────────────────
        "Vitamin C 500mg (Limcee)", "Vitamin C 1000mg Effervescent",
        "Vitamin D3 60000 IU Sachet", "Vitamin D3 1000 IU Tablet",
        "Vitamin B12 500mcg (Methylcobalamin)", "Vitamin B Complex Tablet",
        "Calcium + D3 (Shelcal)", "Calcium Sandoz Syrup",
        "Folic Acid 5mg Tablet", "Iron + Folic Acid (Ferrous Sulphate)",
        "Zinc Tablet 50mg", "Magnesium Tablet", "Potassium Chloride Tablet",
        "Omega-3 Fish Oil 1000mg", "Flaxseed Oil Capsule",
        "Biotin 10000mcg Tablet", "Collagen Supplement",
        "Multivitamin (Supradyn)", "Multivitamin (Becosules)",
        "Multivitamin (Centrum)", "Multivitamin (A to Z)",
        "Protein Powder (Whey Isolate)", "Protein Powder (Casein)",
        "Protein Powder (Plant Based)", "BCAA Powder",
        "Mass Gainer (Endura)", "Creatine Monohydrate",
        "Pre-Workout Supplement", "L-Carnitine Tablet",
        # ── Ayurvedic & Herbal ───────────────────────────────────────────────
        "Ashwagandha Tablet (Himalaya)", "Ashwagandha KSM-66 Capsule",
        "Triphala Churna (Dabur)", "Triphala Tablet",
        "Chyawanprash (Dabur)", "Chyawanprash (Baidyanath)",
        "Giloy Juice (Patanjali)", "Amla Juice (Patanjali)",
        "Aloe Vera Juice", "Wheatgrass Powder", "Moringa Capsule",
        "Tulsi Drops (Himalaya)", "Neem Tablet (Himalaya)",
        "Karela Jamun Juice", "Shilajit Resin",
        "Arjuna Tablet (Himalaya)", "Gokshura Tablet",
        "Brahmi Tablet (Bacopa)", "Shankhapushpi Syrup",
        "Pudin Hara Liquid", "Pudinhara Pearls",
        "Hajmola Regular", "Hajmola Imli", "Hajmola Anardana",
        "Liv 52 DS Tablet (Himalaya)", "Liv 52 Syrup",
        "Abana Tablet (Himalaya)", "Pilex Tablet (Himalaya)",
        "Cystone Tablet (Himalaya)", "Tentex Royal Capsule",
        "Septilin Tablet (Himalaya)", "Immufort Tablet",
        "Patanjali Divya Medha Vati", "Patanjali Giloy Ghanvati",
        "Baidyanath Makardhwaj Vati", "Baidyanath Chyawanprash",
        "Zandu Balm", "Zandu Pancharishta", "Zandu Kesari Jivan",
        # ── Women's Health ───────────────────────────────────────────────────
        "Condoms (Durex Naturals, 10s)", "Condoms (Durex Extra Safe, 10s)",
        "Condoms (KamaSutra Dotted, 10s)", "Condoms (Manforce Strawberry, 10s)",
        "Condoms (Skore Dotted, 10s)", "Condoms (Moods Ribbed, 10s)",
        "Condoms (One Ultra Thin, 10s)", "Condoms (Kohinoor Black, 10s)",
        "Condoms (Playgard Extra Dotted, 10s)", "Female Condom",
        "i-Pill Emergency Contraceptive", "Unwanted 72 Tablet",
        "Mala-N Oral Contraceptive Pill", "Femilon OCP",
        "Pregacare Tablet (Vitabiotics)", "Materna Prenatal Vitamin",
        "Folic Acid 400mcg Tablet", "Iron Sucrose Injection",
        "Meftal Spas Tablet (Menstrual Pain)", "Cyclopam Tablet",
        "Whisper Sanitary Pads (Regular)", "Whisper Sanitary Pads (XL)",
        "Stayfree Secure (Regular)", "Stayfree Secure (Heavy)",
        "Sofy Antibacterial Pad", "Carefree Panty Liner",
        "Tampons (o.b. Regular)", "Menstrual Cup (Silicone)",
        # ── Diabetes Care ─────────────────────────────────────────────────────
        "Glucometer (Accu-Chek Active)", "Glucometer (OneTouch Select)",
        "Glucometer Test Strips (Accu-Chek)", "Lancets (Accu-Chek Softclix)",
        "Insulin Syringe 1ml (BD)", "Insulin Pen Needle (31G)",
        "Metformin 500mg Tablet", "Glimepiride 1mg Tablet",
        "Sitagliptin 100mg (Januvia)", "Voglibose 0.3mg Tablet",
        "Diabetes Footcare Cream",
        # ── Cardiac & BP ─────────────────────────────────────────────────────
        "Amlodipine 5mg Tablet", "Telmisartan 40mg Tablet",
        "Atenolol 50mg Tablet", "Losartan 50mg Tablet",
        "Atorvastatin 10mg Tablet", "Rosuvastatin 10mg Tablet",
        "Ecosprin AV 75mg Capsule", "Clopidogrel 75mg Tablet",
        "Blood Pressure Monitor (Omron HEM-7120)", "Sphygmomanometer (Manual)",
        # ── Medical Devices & Instruments ────────────────────────────────────
        "Digital Thermometer (Omron)", "Infrared Thermometer",
        "Pulse Oximeter (Finger Tip)", "Stethoscope (Basic)",
        "Weighing Scale (Personal)", "Weighing Scale (Baby)",
        "Nebulizer Machine (Omron)", "Nebulization Mask",
        "Heating Pad (Electric)", "Hot Water Bag (Rubber)",
        "Ice Gel Pack", "Cold Compress Pack",
        "Knee Support (Elastic)", "Lumbar Support Belt",
        "Cervical Collar (Soft)", "Wrist Support Brace",
        "Ankle Support Brace", "Elbow Support",
        "Walking Stick (Aluminium)", "Crutches (Underarm)",
        "Wheelchair (Manual, Folding)", "Bedpan (Stainless Steel)",
        "Urine Bag", "Catheter Tube", "IV Stand",
        # ── First Aid & Surgical Supplies ────────────────────────────────────
        "First Aid Kit Box", "Bandage Crepe 10cm", "Bandage Crepe 15cm",
        "Adhesive Plaster (Elastoplast)", "Adhesive Bandage (Band-Aid)",
        "Cotton Roll 100g (Absorbent)", "Gauze Swabs (Pack of 12)",
        "Surgical Blade (Size 22)", "Disposable Syringe 2ml",
        "Disposable Syringe 5ml", "Disposable Syringe 10ml",
        "IV Cannula 20G", "IV Cannula 22G", "IV Cannula 24G",
        "Micropore Tape (3M)", "Elastic Adhesive Bandage",
        "Antiseptic Solution (Savlon)", "Antiseptic Liquid (Dettol)",
        "Hydrogen Peroxide 3%", "Spirit (Isopropyl Alcohol 70%)",
        "Betadine Solution (500ml)", "Normal Saline Sachet",
        "Surgical Gloves (Size 7)", "Examination Gloves (Latex, Box)",
        "Examination Gloves (Nitrile, Box)", "Face Mask (3-ply Surgical)",
        "N95 Respirator Mask", "KN95 Mask", "Hand Sanitizer (500ml)",
        # ── Oral Rehydration & Electrolytes ──────────────────────────────────
        "ORS Sachet (Electral)", "ORS Sachet (WHO Formula)",
        "Enerzal Powder (Orange)", "Glucon-D Powder",
        "Pedialyte Sachet", "Electrolyte Drink (Fast&Up)",
        # ── Sleep & Mental Wellness ───────────────────────────────────────────
        "Melatonin 5mg Tablet", "Sleep Supplement (Natural)",
        "Magnesium Glycinate Tablet", "L-Theanine Capsule",
        "Stress Relief Tablet (Jatamansi Himalaya)",
        # ── Baby & Child Health ───────────────────────────────────────────────
        "Calpol Suspension (Paracetamol 120mg/5ml)",
        "Zincovit Syrup (Vitamin + Zinc)", "Kidglim Syrup",
        "Cerelac Baby Food (Wheat)", "Cerelac (Wheat + Honey)",
        "Pediasure Vanilla Powder", "Similac Advance Powder",
        "Baby ORS (Electrolyte Sachets)", "Gripe Water (Woodward's)",
        "Baby Vitamin D3 Drops", "Ferrous Ammonium Citrate Syrup",
    ],

    "Beauty & Personal Care": [
        # Hair Care
        "Shampoo (Head & Shoulders)", "Shampoo (Pantene)", "Shampoo (Dove)",
        "Shampoo (Clinic Plus)", "Anti-Dandruff Shampoo",
        "Conditioner", "Hair Mask", "Hair Serum", "Hair Gel",
        "Coconut Hair Oil", "Amla Hair Oil", "Onion Hair Oil",
        "Hair Colour (Black)", "Hair Colour (Brown)", "Henna Powder",
        "Comb", "Hair Brush", "Hair Clip Set", "Hair Dryer", "Straightener",
        # Skin Care
        "Face Wash (Himalaya)", "Face Wash (Ponds)", "Face Wash (Garnier)",
        "Face Scrub", "Face Pack", "Clay Mask",
        "Moisturiser (Lakme)", "Moisturiser (Vaseline)", "Moisturiser (Olay)",
        "Sunscreen SPF 30", "Sunscreen SPF 50", "Night Cream", "Eye Cream",
        "Aloe Vera Gel", "Vitamin C Serum", "Retinol Serum",
        "Toner", "Micellar Water", "Makeup Remover Wipes",
        # Makeup
        "Lipstick", "Lip Gloss", "Kajal", "Eyeliner", "Mascara",
        "Foundation", "Compact Powder", "BB Cream", "Concealer",
        "Blush", "Eyeshadow Palette", "Setting Spray",
        # Body Care
        "Body Lotion (Dove)", "Body Lotion (Nivea)", "Body Scrub",
        "Soap (Dettol)", "Soap (Dove)", "Soap (Lifebuoy)", "Soap (Pears)",
        "Shower Gel", "Talcum Powder",
        # Oral Care
        "Toothpaste (Colgate)", "Toothpaste (Pepsodent)", "Toothpaste (Sensodyne)",
        "Toothbrush", "Electric Toothbrush", "Mouthwash", "Floss",
        "Teeth Whitening Strips",
        # Grooming
        "Deodorant (Axe)", "Deodorant (Dove)", "Deodorant (Fogg)", "Perfume",
        "Shaving Cream", "Shaving Gel", "After Shave Lotion",
        "Razor (Gillette)", "Razor (Schick)", "Trimmer (Philips)", "Beard Oil",
        "Nail Polish", "Nail Polish Remover", "Nail Cutter",
    ],

    "Sports": [
        # Cricket
        "Cricket Bat (English Willow)", "Cricket Bat (Kashmir Willow)",
        "Cricket Ball (Leather)", "Cricket Ball (Tennis)", "Cricket Helmet",
        "Cricket Gloves", "Cricket Pads", "Cricket Kit Bag",
        # Football & Other Ball Sports
        "Football (Size 5)", "Football (Size 4)", "Football Goal Post",
        "Basketball", "Volleyball", "Handball",
        # Badminton & Tennis
        "Badminton Racket", "Badminton Shuttlecock", "Badminton Net",
        "Tennis Racket", "Tennis Ball", "Table Tennis Set",
        # Fitness
        "Yoga Mat", "Exercise Mat", "Foam Roller",
        "Dumbbells (2kg pair)", "Dumbbells (5kg pair)", "Barbell Set",
        "Resistance Bands", "Pull-up Bar", "Ab Roller", "Kettlebell",
        "Skipping Rope", "Treadmill (Manual)", "Cycle (Exercise Bike)",
        # Cycling & Outdoor
        "Cycling Helmet", "Cycling Gloves", "Cycling Shorts",
        "Sports Shoes (Running)", "Sports Shoes (Football Studs)",
        "Gym Bag", "Water Bottle (Sports)", "Protein Shaker",
        # Protective Gear
        "Knee Support", "Elbow Support", "Ankle Support", "Gym Gloves",
        "Swimming Goggles", "Swimming Cap", "Badminton Shoes",
    ],

    "Books": [
        # School
        "NCERT Class 10 Maths", "NCERT Class 10 Science", "NCERT Class 12 Physics",
        "NCERT Class 12 Chemistry", "NCERT Class 12 Biology", "NCERT Class 12 Maths",
        # Competitive Exams
        "JEE Main Preparation Guide", "JEE Advanced Mathematics",
        "NEET Biology Guide", "UPSC General Studies",
        "Bank PO Quantitative Aptitude", "SSC CGL Guide",
        "CAT Preparation Book", "GRE Preparation Guide",
        # Language
        "English Grammar (Wren & Martin)", "Spoken English Book",
        "Kannada Primer", "Kannada-English Dictionary", "Hindi Grammar",
        # Fiction & Literature
        "Malgudi Days (R.K. Narayan)", "The Guide (R.K. Narayan)",
        "Wings of Fire (APJ Abdul Kalam)", "My Experiments with Truth (Gandhi)",
        "Midnight's Children (Rushdie)", "The White Tiger (Adiga)",
        "Harry Potter Set", "The Alchemist", "Atomic Habits",
        # Reference
        "Cookbook (Tarla Dalal)", "Indian Recipes Book",
        "Atlas (World)", "General Knowledge 2025",
        "Illustrated Children's Encyclopedia", "Children's Story Book Set",
    ],

    "Stationery": [
        # Writing
        "Notebook (Single Line)", "Notebook (Ruled)", "Spiral Notebook",
        "Register 200 Pages", "Graph Book", "Drawing Book",
        "Ball Pen (Reynolds)", "Gel Pen", "Pilot Pen",
        "Pencil (HB)", "Pencil (2B)", "Mechanical Pencil",
        "Eraser", "Sharpener", "Pencil Sharpener (Electric)",
        # Art
        "Colour Pencils (12 Set)", "Colour Pencils (24 Set)",
        "Sketch Pens (Set)", "Watercolour Set", "Acrylic Paint Set",
        "Drawing Pencils Set", "Paintbrushes Set", "Palette",
        # Office
        "A4 Paper (500 Sheets)", "Sticky Notes", "Highlighters (Set)",
        "Whiteboard Marker", "Permanent Marker",
        "Stapler", "Stapler Pins", "Hole Punch", "Paper Clips",
        "Scissors", "Cutter", "Glue Stick", "Fevicol", "Fevistick",
        "Sellotape", "Double-sided Tape", "Binding Clips",
        "File Folder", "Plastic Cover", "Envelope Pack",
        "Calculator (Scientific)", "Calculator (Basic)",
        "Geometry Box", "Scale (30cm)", "Protractor Set",
    ],

    "Hardware": [
        # Hand Tools
        "Hammer", "Claw Hammer", "Rubber Mallet",
        "Screwdriver Set (Flathead + Phillips)", "Ratchet Screwdriver",
        "Pliers Set", "Combination Pliers", "Needle Nose Pliers",
        "Adjustable Wrench", "Spanner Set", "Allen Key Set",
        "Hacksaw", "Hand Saw", "Wood Chisel Set",
        "Measuring Tape (5m)", "Measuring Tape (10m)", "Spirit Level",
        "Utility Knife", "Wire Cutter", "Wire Stripper",
        # Power Tools
        "Drill Machine (Electric)", "Drill Machine (Cordless)",
        "Angle Grinder", "Jigsaw", "Circular Saw",
        # Electrical
        "LED Bulb 9W", "LED Bulb 12W", "LED Strip Light",
        "Tube Light", "CFL Bulb", "Batten Light",
        "Switch Board", "Switch (5A)", "Socket (5A)", "Socket (15A)",
        "Extension Cord (5m)", "Extension Cord (10m)", "Multi-Plug Adapter",
        "MCB (Miniature Circuit Breaker)", "Wire (1.5mm)", "Wire (2.5mm)",
        # Plumbing
        "PVC Pipe (Half Inch)", "PVC Pipe (One Inch)", "PVC Elbow Fitting",
        "Tap", "Basin Mixer", "Shower Head", "Drain Cover",
        "Teflon Tape", "PVC Adhesive", "Silicon Sealant",
        # Paints & Finishing
        "Wall Paint (White)", "Wall Paint (Colour)", "Wood Paint", "Metal Paint",
        "Paint Brush (2 inch)", "Paint Roller", "Painter's Tape",
        "Sand Paper (Set)", "Wall Putty", "Primer",
        # Fasteners
        "Nails Assorted Pack", "Wood Screws Set", "Wall Screws with Plugs",
        "Nuts and Bolts Set", "Safety Pins", "S-Hooks",
    ],

    "Automotive": [
        # Car Maintenance
        "Engine Oil (Castrol)", "Engine Oil (Mobil)", "Gear Oil",
        "Car Shampoo", "Car Polish (Turtle Wax)", "Car Wax",
        "Tyre Shine Spray", "Dashboard Polish", "Windscreen Cleaner",
        "Car Air Freshener", "Car Perfume", "Hanging Air Freshener",
        # Car Accessories
        "Car Seat Cover (Full Set)", "Steering Wheel Cover",
        "Car Floor Mat (Rubber)", "Car Floor Mat (Velvet)",
        "Car Sunshade (Windscreen)", "Rear Sunshade",
        "Car Organiser", "Boot Organiser", "Cup Holder",
        "Phone Mount (Dashboard)", "Car Charger (USB-C)",
        # Safety & Emergency
        "Tyre Inflator (Portable)", "Tyre Pressure Gauge",
        "Jump Starter Cable", "Emergency Warning Triangle",
        "Fire Extinguisher (Small)", "Tow Rope",
        "Puncture Repair Kit",
        # Wiper & Lighting
        "Wiper Blade (Driver Side)", "Wiper Blade Set",
        "Headlight Bulb", "Fog Light", "LED Indicator",
        # Bike Accessories
        "Bike Helmet (Full Face)", "Bike Helmet (Half Face)",
        "Riding Gloves", "Knee Guard", "Elbow Guard",
        "Chain Lubricant", "Chain Cleaner",
        "Bike Cover", "Bike Lock", "Rear Carrier Box",
    ],

    "Toys & Baby": [
        # Toys
        "Lego Classic Set", "Lego City Set", "Remote Control Car",
        "Remote Control Helicopter", "Barbie Doll", "Action Figure",
        "Building Blocks (100 pcs)", "Magnetic Tiles",
        "Jigsaw Puzzle (500 pc)", "Jigsaw Puzzle (1000 pc)",
        "Board Game (Ludo)", "Board Game (Snakes & Ladders)", "Board Game (Scrabble)",
        "Chess Set", "Carrom Board", "UNO Cards", "Playing Cards",
        "Soft Toy (Teddy Bear)", "Stuffed Animal", "Rag Doll",
        "Colouring Book + Crayons Set", "Play-Doh Set", "Slime Kit",
        "Science Kit", "Telescope (Kids)", "Microscope (Kids)",
        "Bicycle (Kids 16 inch)", "Tricycle", "Scooter (Kids)",
        "Skipping Rope", "Cricket Set (Kids)", "Football (Kids)",
        # Baby
        "Baby Diapers (Pampers)", "Baby Diapers (Huggies)", "Baby Pull-ups",
        "Baby Wipes", "Wet Wipes",
        "Feeding Bottle (Glass)", "Feeding Bottle (Plastic)",
        "Sipper Cup", "Baby Bowl Set", "Baby Spoon Set",
        "Baby Lotion (Johnson's)", "Baby Powder (Johnson's)",
        "Baby Shampoo", "Baby Soap", "Nappy Rash Cream",
        "Baby Carrier", "Baby Rocker", "Baby Walker",
        "Nursing Pillow", "Breast Pump", "Steriliser",
        "Teether", "Baby Monitor", "Swaddle Blanket",
    ],
}

# ── Real-time product prices (in paise = 1/100 of ₹) ─────────────────────────
# Prices reflect current MRP on Amazon.in / BigBasket / 1mg (April 2026).
# Lookup used by seed_items_for_catalog; missing items fall back to category range.
PRODUCT_PRICES: dict[str, int] = {
    # ── Food & Grocery ────────────────────────────────────────────────────────
    "Parle-G Biscuits": 1000,               # ₹10
    "Marie Gold Biscuits": 2000,            # ₹20
    "Good Day Biscuits": 3500,              # ₹35
    "Bourbon Biscuits": 2000,               # ₹20
    "Oreo Biscuits": 2000,                  # ₹20
    "Monaco Biscuits": 2000,                # ₹20
    "Maggi Masala Noodles": 1400,           # ₹14
    "Yippee Noodles": 1400,                 # ₹14
    "Lays Chips": 2000,                     # ₹20
    "Kurkure": 2000,                        # ₹20
    "Haldiram's Bhujia": 9900,              # ₹99
    "Basmati Rice": 19900,                  # ₹199 (1kg)
    "Sona Masuri Rice": 6900,               # ₹69 (1kg)
    "Toor Dal": 18000,                      # ₹180 (1kg)
    "Chana Dal": 13000,                     # ₹130 (1kg)
    "Moong Dal": 16000,                     # ₹160 (1kg)
    "Sunflower Oil": 17900,                 # ₹179 (1L)
    "Coconut Oil": 22000,                   # ₹220 (1L)
    "Mustard Oil": 18000,                   # ₹180 (1L)
    "Ghee (Cow)": 69900,                    # ₹699 (1kg Amul)
    "Butter": 5500,                         # ₹55 (100g Amul)
    "Milk (Full Cream)": 3100,              # ₹31 (500ml Nandini)
    "Milk (Toned)": 2700,                   # ₹27 (500ml)
    "Curd": 3500,                           # ₹35 (400g)
    "Paneer": 11500,                        # ₹115 (200g)
    "Eggs (White)": 8900,                   # ₹89 (12 eggs)
    "Turmeric Powder": 7900,                # ₹79 (200g)
    "Red Chilli Powder": 9900,              # ₹99 (200g)
    "Garam Masala": 8000,                   # ₹80 (100g)
    "Tomato Ketchup (Kissan)": 14900,       # ₹149 (1kg)
    "Mango Pickle (Avakaya)": 9900,         # ₹99 (500g)
    "Honey": 24900,                         # ₹249 (500g Dabur)
    "Peanut Butter": 27900,                 # ₹279 (750g)
    "Detergent Powder (Surf Excel)": 32900, # ₹329 (3kg)
    "Dishwash Bar (Vim)": 3500,             # ₹35
    "Floor Cleaner (Lizol)": 22900,         # ₹229 (1L)
    "Toilet Cleaner (Harpic)": 14900,       # ₹149 (750ml)
    "Mosquito Repellent (All Out)": 24900,  # ₹249 (starter kit)
    "Tissue Paper": 17900,                  # ₹179 (200 sheets)
    "Toilet Paper Rolls": 24900,            # ₹249 (6 rolls)
    "Tea (Red Label)": 28900,               # ₹289 (500g)
    "Coffee (Nescafe)": 25900,              # ₹259 (200g)
    "Fruit Juice (Tropicana)": 9900,        # ₹99 (1L)
    "Soft Drink (Coca-Cola)": 4500,         # ₹45 (750ml)
    "Energy Drink (Red Bull)": 12500,       # ₹125 (250ml)
    "Coconut Water": 4500,                  # ₹45 (200ml)
    "Horlicks": 34900,                      # ₹349 (500g)
    "Bournvita": 34900,                     # ₹349 (500g)
    "Cadbury Dairy Milk": 4500,             # ₹45 (45g)
    "Kit Kat": 3000,                        # ₹30
    "Snickers": 5500,                       # ₹55
    "Cornflakes (Kellogg's)": 27900,        # ₹279 (500g)
    "Oats (Quaker)": 22900,                 # ₹229 (1kg)
    "Ice Cream (Vanilla)": 14900,           # ₹149 (750ml)
    "Jaggery Block": 9900,                  # ₹99 (1kg)
    "Agarbatti (Cycle Brand)": 3500,        # ₹35 (100 sticks)
    "Agarbatti (Satya Sai Baba Nag Champa)": 6500, # ₹65
    "Camphor Tablets": 4500,                # ₹45
    "Pan Masala (Rajnigandha Silver)": 500, # ₹5
    "Cigarettes (Gold Flake Kings)": 1500,  # ₹15 (per stick, sold individually)

    # ── Electronics ───────────────────────────────────────────────────────────
    "Samsung Galaxy S24": 7999900,          # ₹79,999
    "Samsung Galaxy S24 Ultra": 13499900,   # ₹1,34,999
    "iPhone 15": 7999900,                   # ₹79,999
    "iPhone 15 Pro": 13499900,              # ₹1,34,999
    "iPhone 15 Pro Max": 15999900,          # ₹1,59,999
    "OnePlus 12": 6499900,                  # ₹64,999
    "OnePlus Nord CE4": 2499900,            # ₹24,999
    "Redmi Note 13 Pro": 2699900,           # ₹26,999
    "Redmi 13C": 1099900,                   # ₹10,999
    "Poco X6 Pro": 2699900,                 # ₹26,999
    "Realme 12 Pro": 2699900,               # ₹26,999
    "Vivo V30": 3499900,                    # ₹34,999
    "Nothing Phone 2a": 2499900,            # ₹24,999
    "Google Pixel 8a": 5299900,             # ₹52,999
    "MacBook Air M2": 11490000,             # ₹1,14,900
    "MacBook Pro 14": 16990000,             # ₹1,69,900
    "Dell Inspiron 15": 5499900,            # ₹54,999
    "HP Laptop 15": 4999900,                # ₹49,999
    "Lenovo IdeaPad": 4499900,              # ₹44,999
    "iPad Air": 7499900,                    # ₹74,999
    "Samsung Galaxy Tab A9": 1999900,       # ₹19,999
    "Wireless Earbuds (boAt)": 199900,      # ₹1,999
    "Wireless Earbuds (Sony)": 899900,      # ₹8,999
    "Wireless Earbuds (JBL)": 499900,       # ₹4,999
    "Over-ear Headphones (Sony WH-1000XM5)": 2799900, # ₹27,999
    "Bluetooth Speaker (JBL)": 549900,      # ₹5,499
    "Smart TV 43 inch": 2499900,            # ₹24,999
    "Smart TV 55 inch": 4499900,            # ₹44,999
    "Power Bank 10000mAh": 149900,          # ₹1,499
    "Power Bank 20000mAh": 249900,          # ₹2,499
    "USB-C Charger 65W": 199900,            # ₹1,999
    "Wireless Charger": 149900,             # ₹1,499
    "Router (Wi-Fi 6)": 399900,             # ₹3,999
    "HDMI Cable": 59900,                    # ₹599
    "USB Hub": 149900,                      # ₹1,499
    "Pen Drive 64GB": 59900,                # ₹599
    "SD Card": 79900,                       # ₹799
    "Screen Protector (Tempered Glass)": 19900, # ₹199

    # ── Clothing ─────────────────────────────────────────────────────────────
    "Men's Round Neck T-Shirt": 39900,      # ₹399
    "Men's Polo T-Shirt": 59900,            # ₹599
    "Men's Formal Shirt (White)": 99900,    # ₹999
    "Men's Casual Shirt": 79900,            # ₹799
    "Men's Jeans (Slim Fit)": 149900,       # ₹1,499
    "Men's Jeans (Regular)": 129900,        # ₹1,299
    "Men's Chinos (Khaki)": 119900,         # ₹1,199
    "Men's Formal Trousers (Black)": 119900,# ₹1,199
    "Men's Track Pants": 79900,             # ₹799
    "Men's Hoodie (Pullover)": 99900,       # ₹999
    "Men's Sweatshirt": 89900,              # ₹899
    "Men's Blazer (Black)": 299900,         # ₹2,999
    "Men's Suit (2-piece)": 699900,         # ₹6,999
    "Men's Kurta (Cotton White)": 79900,    # ₹799
    "Men's Kurta Pyjama Set (Cotton)": 149900, # ₹1,499
    "Men's Sherwani": 699900,               # ₹6,999
    "Men's Dhoti (White Cotton)": 29900,    # ₹299
    "Men's Lungi (Checked)": 24900,         # ₹249
    "Men's Briefs (Rupa Frontline)": 29900, # ₹299 (3-pack)
    "Men's Boxer Shorts (Cotton)": 39900,   # ₹399
    "Men's Formal Shoes (Oxford)": 249900,  # ₹2,499
    "Men's Casual Sneakers": 199900,        # ₹1,999
    "Men's Chappals (Rubber)": 29900,       # ₹299
    "Women's Kurti (Cotton Printed)": 59900,# ₹599
    "Women's Kurti (Rayon)": 79900,         # ₹799
    "Women's Kurta Set (Cotton)": 119900,   # ₹1,199
    "Women's Salwar Kameez (Punjabi)": 149900, # ₹1,499
    "Women's Saree (Mysore Silk)": 499900,  # ₹4,999
    "Women's Saree (Kanjivaram Silk)": 999900, # ₹9,999
    "Women's Saree (Banarasi Silk)": 699900,# ₹6,999
    "Women's Saree (Cotton Handloom)": 149900, # ₹1,499
    "Women's Saree (Chiffon Printed)": 79900, # ₹799
    "Women's Lehenga Choli (Bridal)": 1999900, # ₹19,999
    "Women's Lehenga Choli (Party)": 499900,# ₹4,999
    "Women's Heels (Block Heel)": 199900,   # ₹1,999
    "Women's Flats (Ballerina)": 79900,     # ₹799
    "Women's Casual Sneakers": 169900,      # ₹1,699
    "Women's Bra (T-shirt Bra)": 49900,     # ₹499
    "Women's Sports Bra": 59900,            # ₹599
    "Women's Night Suit (Cotton)": 79900,   # ₹799
    "Kids T-Shirt (Boys)": 29900,           # ₹299
    "Kids Frock (Casual)": 39900,           # ₹399
    "School Uniform Shirt (White)": 34900,  # ₹349
    "School Uniform Trouser (Grey)": 44900, # ₹449
    "Kids School Shoes": 59900,             # ₹599
    "Bangles Set (Glass)": 9900,            # ₹99

    # ── Health & Wellness ─────────────────────────────────────────────────────
    "Paracetamol 500mg (Crocin)": 1700,     # ₹17 (10 tabs)
    "Dolo 650 Tablet": 3200,                # ₹32 (15 tabs)
    "Ibuprofen 400mg (Brufen)": 2800,       # ₹28 (10 tabs)
    "Combiflam Tablet": 3500,               # ₹35 (10 tabs)
    "Aspirin 75mg (Ecosprin)": 1900,        # ₹19 (14 tabs)
    "Azithromycin 500mg (Zithromax)": 8500, # ₹85 (3 tabs)
    "Gelusil MPS Tablet": 1800,             # ₹18 (12 tabs)
    "Eno Fruit Salt (Regular)": 6500,       # ₹65 (100g)
    "Pan 40 Tablet (Pantoprazole)": 5500,   # ₹55 (15 tabs)
    "Metrogyl 400 (Metronidazole)": 5500,   # ₹55 (15 tabs)
    "Benadryl Cough Syrup": 9900,           # ₹99 (100ml)
    "Vicks Vaporub (50g)": 14900,           # ₹149
    "Betadine Ointment (Povidone-Iodine)": 12900, # ₹129 (20g)
    "Volini Spray": 24900,                  # ₹249
    "Moov Cream": 12900,                    # ₹129
    "Iodex Cream": 9900,                    # ₹99
    "Vitamin C 500mg (Limcee)": 2500,       # ₹25 (15 tabs)
    "Vitamin D3 60000 IU Sachet": 3500,     # ₹35 (1 sachet)
    "Vitamin B12 500mcg (Methylcobalamin)": 9500, # ₹95 (10 tabs)
    "Omega-3 Fish Oil 1000mg": 49900,       # ₹499 (60 caps)
    "Multivitamin (Supradyn)": 24900,       # ₹249 (15 tabs)
    "Multivitamin (Becosules)": 12900,      # ₹129 (30 caps)
    "Protein Powder (Whey Isolate)": 299900,# ₹2,999 (1kg)
    "Ashwagandha Tablet (Himalaya)": 24900, # ₹249 (60 tabs)
    "Chyawanprash (Dabur)": 22900,          # ₹229 (500g)
    "Giloy Juice (Patanjali)": 18900,       # ₹189 (1L)
    "Digital Thermometer (Omron)": 19900,   # ₹199
    "Blood Pressure Monitor (Omron HEM-7120)": 199900, # ₹1,999
    "Glucometer (Accu-Chek Active)": 129900, # ₹1,299
    "Pulse Oximeter (Finger Tip)": 69900,   # ₹699
    "Nebulizer Machine (Omron)": 299900,    # ₹2,999
    "Heating Pad (Electric)": 89900,        # ₹899
    "Knee Support (Elastic)": 29900,        # ₹299
    "Condoms (Durex Naturals, 10s)": 29900, # ₹299
    "Condoms (KamaSutra Dotted, 10s)": 14900, # ₹149
    "Condoms (Manforce Strawberry, 10s)": 14900, # ₹149
    "Condoms (Skore Dotted, 10s)": 14900,   # ₹149
    "i-Pill Emergency Contraceptive": 10900, # ₹109
    "Unwanted 72 Tablet": 9900,             # ₹99
    "Whisper Sanitary Pads (Regular)": 4900, # ₹49 (8 pads)
    "Whisper Sanitary Pads (XL)": 6900,     # ₹69 (7 pads)
    "Stayfree Secure (Regular)": 4900,      # ₹49
    "ORS Sachet (Electral)": 2500,          # ₹25 (per sachet)
    "Calpol Suspension (Paracetamol 120mg/5ml)": 7900, # ₹79 (60ml)
    "Gripe Water (Woodward's)": 11900,      # ₹119 (130ml)

    # ── Beauty & Personal Care ────────────────────────────────────────────────
    "Shampoo (Head & Shoulders)": 37900,    # ₹379 (400ml)
    "Shampoo (Pantene)": 34900,             # ₹349 (400ml)
    "Shampoo (Dove)": 37900,                # ₹379 (400ml)
    "Conditioner": 29900,                   # ₹299
    "Coconut Hair Oil": 11900,              # ₹119 (200ml)
    "Onion Hair Oil": 19900,                # ₹199 (200ml)
    "Amla Hair Oil": 8900,                  # ₹89 (200ml)
    "Hair Dryer": 149900,                   # ₹1,499
    "Straightener": 249900,                 # ₹2,499
    "Face Wash (Himalaya)": 14900,          # ₹149 (150ml)
    "Face Wash (Garnier)": 17900,           # ₹179 (150ml)
    "Moisturiser (Lakme)": 19900,           # ₹199 (100ml)
    "Moisturiser (Vaseline)": 14900,        # ₹149 (100ml)
    "Sunscreen SPF 50": 29900,              # ₹299
    "Aloe Vera Gel": 14900,                 # ₹149 (300g)
    "Vitamin C Serum": 39900,               # ₹399
    "Lipstick": 24900,                      # ₹249
    "Kajal": 14900,                         # ₹149
    "Foundation": 59900,                    # ₹599
    "Eyeshadow Palette": 49900,             # ₹499
    "Body Lotion (Dove)": 24900,            # ₹249 (400ml)
    "Body Lotion (Nivea)": 22900,           # ₹229 (400ml)
    "Soap (Dove)": 6900,                    # ₹69 (100g)
    "Soap (Dettol)": 4500,                  # ₹45 (75g)
    "Shower Gel": 34900,                    # ₹349 (250ml)
    "Toothpaste (Colgate)": 14900,          # ₹149 (200g)
    "Toothpaste (Sensodyne)": 22900,        # ₹229 (100g)
    "Toothbrush": 4900,                     # ₹49
    "Electric Toothbrush": 149900,          # ₹1,499
    "Mouthwash": 22900,                     # ₹229 (250ml)
    "Deodorant (Axe)": 24900,               # ₹249
    "Deodorant (Dove)": 27900,              # ₹279
    "Perfume": 99900,                       # ₹999
    "Razor (Gillette)": 24900,              # ₹249
    "Trimmer (Philips)": 149900,            # ₹1,499
    "Nail Polish": 9900,                    # ₹99

    # ── Home & Kitchen ────────────────────────────────────────────────────────
    "Pressure Cooker (3L)": 149900,         # ₹1,499
    "Pressure Cooker (5L)": 199900,         # ₹1,999
    "Non-stick Kadai": 99900,               # ₹999
    "Non-stick Tawa": 79900,                # ₹799
    "Mixer Grinder": 349900,                # ₹3,499
    "Electric Kettle": 149900,              # ₹1,499
    "Toaster (Pop-up)": 149900,             # ₹1,499
    "Induction Cooktop": 249900,            # ₹2,499
    "Microwave Oven": 999900,               # ₹9,999
    "Air Fryer": 599900,                    # ₹5,999
    "Water Purifier (RO)": 1499900,         # ₹14,999
    "Steel Tiffin Box": 49900,              # ₹499
    "Water Bottle (Steel)": 59900,          # ₹599
    "Dinner Set (12 Piece)": 149900,        # ₹1,499
    "Bedsheet (Double)": 99900,             # ₹999
    "Bedsheet (Single)": 69900,             # ₹699
    "Pillow": 49900,                        # ₹499
    "Blanket": 99900,                       # ₹999
    "Curtains (Pack of 2)": 149900,         # ₹1,499
    "Doormat": 29900,                       # ₹299
    "Towel Set": 79900,                     # ₹799
    "Mop": 39900,                           # ₹399
    "Dustbin (10L)": 29900,                 # ₹299

    # ── Sports ────────────────────────────────────────────────────────────────
    "Cricket Bat (English Willow)": 299900, # ₹2,999
    "Cricket Bat (Kashmir Willow)": 99900,  # ₹999
    "Cricket Ball (Leather)": 49900,        # ₹499
    "Cricket Helmet": 199900,               # ₹1,999
    "Football (Size 5)": 79900,             # ₹799
    "Badminton Racket": 79900,              # ₹799
    "Badminton Shuttlecock": 34900,         # ₹349 (12-pack)
    "Yoga Mat": 79900,                      # ₹799
    "Dumbbells (2kg pair)": 59900,          # ₹599
    "Dumbbells (5kg pair)": 89900,          # ₹899
    "Resistance Bands": 49900,              # ₹499
    "Skipping Rope": 29900,                 # ₹299
    "Sports Shoes (Running)": 249900,       # ₹2,499
    "Cycling Helmet": 199900,               # ₹1,999
    "Knee Support": 29900,                  # ₹299
    "Swimming Goggles": 39900,              # ₹399

    # ── Books ─────────────────────────────────────────────────────────────────
    "NCERT Class 10 Maths": 9900,           # ₹99
    "NCERT Class 12 Physics": 12900,        # ₹129
    "JEE Main Preparation Guide": 89900,    # ₹899
    "NEET Biology Guide": 99900,            # ₹999
    "Wings of Fire (APJ Abdul Kalam)": 19900, # ₹199
    "Atomic Habits": 39900,                 # ₹399
    "The Alchemist": 24900,                 # ₹249
    "Harry Potter Set": 249900,             # ₹2,499 (7-book set)
    "English Grammar (Wren & Martin)": 44900, # ₹449
    "Cookbook (Tarla Dalal)": 29900,        # ₹299

    # ── Stationery ────────────────────────────────────────────────────────────
    "Notebook (Single Line)": 4900,         # ₹49
    "Notebook (Ruled)": 4900,               # ₹49
    "Spiral Notebook": 8900,                # ₹89
    "Register 200 Pages": 12900,            # ₹129
    "Ball Pen (Reynolds)": 1000,            # ₹10
    "Gel Pen": 2000,                        # ₹20
    "Pencil (HB)": 500,                     # ₹5
    "Eraser": 500,                          # ₹5
    "A4 Paper (500 Sheets)": 34900,         # ₹349
    "Sticky Notes": 9900,                   # ₹99
    "Stapler": 14900,                       # ₹149
    "Scissors": 9900,                       # ₹99
    "Glue Stick": 5900,                     # ₹59
    "Geometry Box": 14900,                  # ₹149
    "Calculator (Scientific)": 79900,       # ₹799
    "Colour Pencils (12 Set)": 9900,        # ₹99
    "Watercolour Set": 29900,               # ₹299

    # ── Hardware ──────────────────────────────────────────────────────────────
    "Hammer": 29900,                        # ₹299
    "Screwdriver Set (Flathead + Phillips)": 49900, # ₹499
    "Pliers Set": 49900,                    # ₹499
    "Adjustable Wrench": 34900,             # ₹349
    "Measuring Tape (5m)": 14900,           # ₹149
    "Drill Machine (Electric)": 299900,     # ₹2,999
    "Angle Grinder": 249900,                # ₹2,499
    "LED Bulb 9W": 8900,                    # ₹89
    "LED Bulb 12W": 9900,                   # ₹99
    "Extension Cord (5m)": 39900,           # ₹399
    "PVC Pipe (Half Inch)": 6900,           # ₹69 (per meter)
    "Wall Paint (White)": 139900,           # ₹1,399 (per litre Birla White)
    "Paint Roller": 14900,                  # ₹149
    "Sand Paper (Set)": 9900,               # ₹99
    "Silicon Sealant": 24900,               # ₹249
    "Nails Assorted Pack": 9900,            # ₹99
    "Wood Screws Set": 14900,               # ₹149

    # ── Automotive ────────────────────────────────────────────────────────────
    "Engine Oil (Castrol)": 59900,          # ₹599 (1L)
    "Car Shampoo": 29900,                   # ₹299
    "Car Seat Cover (Full Set)": 299900,    # ₹2,999
    "Car Floor Mat (Rubber)": 149900,       # ₹1,499
    "Phone Mount (Dashboard)": 39900,       # ₹399
    "Car Charger (USB-C)": 29900,           # ₹299
    "Tyre Inflator (Portable)": 199900,     # ₹1,999
    "Jump Starter Cable": 59900,            # ₹599
    "Bike Helmet (Full Face)": 199900,      # ₹1,999
    "Bike Helmet (Half Face)": 99900,       # ₹999
    "Chain Lubricant": 19900,               # ₹199
    "Bike Lock": 39900,                     # ₹399

    # ── Toys & Baby ───────────────────────────────────────────────────────────
    "Lego Classic Set": 249900,             # ₹2,499
    "Remote Control Car": 149900,           # ₹1,499
    "Barbie Doll": 79900,                   # ₹799
    "Building Blocks (100 pcs)": 59900,     # ₹599
    "Jigsaw Puzzle (500 pc)": 49900,        # ₹499
    "Board Game (Ludo)": 29900,             # ₹299
    "Chess Set": 49900,                     # ₹499
    "Carrom Board": 149900,                 # ₹1,499
    "Soft Toy (Teddy Bear)": 49900,         # ₹499
    "Science Kit": 99900,                   # ₹999
    "Baby Diapers (Pampers)": 74900,        # ₹749 (56 pcs)
    "Baby Diapers (Huggies)": 69900,        # ₹699 (56 pcs)
    "Baby Wipes": 29900,                    # ₹299 (72 wipes)
    "Feeding Bottle (Glass)": 59900,        # ₹599
    "Baby Lotion (Johnson's)": 24900,       # ₹249 (400ml)
    "Baby Powder (Johnson's)": 22900,       # ₹229 (400g)
    "Baby Carrier": 299900,                 # ₹2,999
    "Gripe Water (Woodward's)": 11900,      # ₹119
}


def _product_price(name: str, category: str, rng: random.Random) -> int:
    """Return price in paise — exact from PRODUCT_PRICES table, else category-range random."""
    if name in PRODUCT_PRICES:
        # Add ±5 % jitter so the same product varies slightly between shops
        base = PRODUCT_PRICES[name]
        return max(100, int(base * rng.uniform(0.95, 1.05)))
    # Fallback category ranges
    if category == "Electronics":
        return rng.randint(50000, 500000)
    if category in ("Food & Grocery",):
        return rng.randint(1000, 50000)
    if category in ("Clothing",):
        return rng.randint(29900, 299900)
    if category in ("Health & Wellness",):
        return rng.randint(500, 200000)
    return rng.randint(5000, 200000)


# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(show_path=False, show_time=False)],
)
log = logging.getLogger("seeder")
# Suppress pika's verbose connection logs
logging.getLogger("pika").setLevel(logging.WARNING)
console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Geo helpers
# ─────────────────────────────────────────────────────────────────────────────

def random_point_within_radius(center_lat: float, center_lon: float, radius_km: float) -> tuple[float, float]:
    """Return a uniformly random (lat, lon) within radius_km of the centre."""
    bearing  = random.uniform(0, 2 * math.pi)
    # sqrt for uniform area distribution
    distance = radius_km * math.sqrt(random.random())

    lat1 = math.radians(center_lat)
    lon1 = math.radians(center_lon)
    d_r  = distance / EARTH_RADIUS_KM

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d_r)
        + math.cos(lat1) * math.sin(d_r) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance between two points in km."""
    r = EARTH_RADIUS_KM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _nearest_area_name(lat: float, lon: float, max_dist_km: float = 2.5) -> str:
    """Return the closest BANGALORE_AREAS name for the given GPS coordinates.
    Returns '—' if no known area is within max_dist_km."""
    best_name, best_dist = "—", float("inf")
    for name, (a_lat, a_lon) in BANGALORE_AREAS.items():
        d = haversine_km(lat, lon, a_lat, a_lon)
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name.title() if best_dist <= max_dist_km else f"~{best_name.title()}"


# Expansion steps used when a search returns 0 results
_RADIUS_EXPAND_STEPS = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0, 50.0]


def _expand_radii(base_km: float) -> list[float]:
    """Return [base_km, next1, next2, ...] expansion ladder."""
    steps = [base_km] + [r for r in _RADIUS_EXPAND_STEPS if r > base_km]
    # Always include at least two more steps beyond base
    if len(steps) < 3:
        extra = [r for r in [20.0, 30.0, 50.0] if r > steps[-1]]
        steps += extra[:3 - len(steps)]
    return steps


# ─────────────────────────────────────────────────────────────────────────────
# JWT / Auth
# ─────────────────────────────────────────────────────────────────────────────

def make_jwt(seller_id: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pyjwt.encode(
            {
                "user_id":   seller_id,
                "user_type": "seller",
                "exp":       int(time.time()) + 7200,
                "iat":       int(time.time()),
            },
            JWT_SECRET,
            algorithm="HS256",
        )


def auth_headers(seller_id: str) -> dict:
    return {"Authorization": f"Bearer {make_jwt(seller_id)}", "Content-Type": "application/json"}


# ─────────────────────────────────────────────────────────────────────────────
# Seller data generation
# ─────────────────────────────────────────────────────────────────────────────

def locality_gps(locality_name: str, rng: random.Random) -> tuple[float, float]:
    """
    Return GPS for a locality name with small jitter (~300m).
    Falls back to random point within city if name not in BANGALORE_AREAS.
    """
    key = locality_name.strip().lower()
    # exact or partial match in BANGALORE_AREAS
    if key in BANGALORE_AREAS:
        base_lat, base_lon = BANGALORE_AREAS[key]
    else:
        matches = [(name, coords) for name, coords in BANGALORE_AREAS.items() if key in name]
        if matches:
            base_lat, base_lon = matches[0][1]
        else:
            return random_point_within_radius(BASE_LAT, BASE_LON, RADIUS_KM)
    # jitter ±100m so shops stay tightly within their locality
    lat, lon = random_point_within_radius(base_lat, base_lon, 0.1)
    return lat, lon


def fetch_osm_sellers_for_seed(radius_km: float = 25.0, limit: int = 500) -> list[dict]:
    """
    Query OpenStreetMap Overpass API for real named shops across Bangalore.
    Returns seller dicts in seed format (latitude/longitude floats, seller_id UUID).
    Falls back to empty list on network/timeout errors.
    """
    radius_m = int(min(radius_km, 40.0) * 1000)
    overpass_query = f"""
[out:json][timeout:60];
(
  node(around:{radius_m},{BASE_LAT},{BASE_LON})[shop][name];
  node(around:{radius_m},{BASE_LAT},{BASE_LON})[amenity~"restaurant|cafe|fast_food|pharmacy|marketplace"][name];
  way(around:{radius_m},{BASE_LAT},{BASE_LON})[shop][name];
  way(around:{radius_m},{BASE_LAT},{BASE_LON})[amenity~"restaurant|cafe|fast_food|pharmacy|marketplace"][name];
);
out center;
"""
    log.info("Fetching real shops from OpenStreetMap (radius=%d km)…", radius_km)
    try:
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data=overpass_query.strip(),
            timeout=65,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("OSM Overpass fetch failed: %s — no OSM sellers added", exc)
        return []

    seen_ids: set[str] = set()
    results: list[dict] = []

    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:en") or tags.get("brand")
        if not name:
            continue

        # Nodes have lat/lon directly; ways have a "center" sub-object
        if el["type"] == "node":
            lat = el.get("lat")
            lon = el.get("lon")
        else:
            center = el.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")
        if lat is None or lon is None:
            continue

        # Stable seller_id from OSM element id
        osm_key = f"osm:{el['type']}:{el['id']}"
        seller_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, osm_key))
        if seller_id in seen_ids:
            continue
        seen_ids.add(seller_id)

        shop_type = tags.get("shop", "").lower()
        amenity   = tags.get("amenity", "").lower()
        category  = (_OSM_SHOP_CATEGORY_MAP.get(shop_type)
                     or _OSM_AMENITY_CATEGORY_MAP.get(amenity)
                     or "Food & Grocery")

        addr_parts = [
            tags.get("addr:housenumber", ""),
            tags.get("addr:street", ""),
            tags.get("addr:suburb", "") or tags.get("addr:neighbourhood", ""),
            tags.get("addr:city", "Bangalore"),
        ]
        address = ", ".join(p for p in addr_parts if p) or "Bangalore"

        phone = tags.get("phone") or tags.get("contact:phone") or ""

        results.append({
            "seller_id":         seller_id,
            "name":              name,
            "address":           address,
            "latitude":          round(float(lat), 6),
            "longitude":         round(float(lon), 6),
            "category":          category,
            "subscription_tier": "free",
            "rating":            0.0,
            "review_count":      0,
            "phone":             phone,
        })

        if len(results) >= limit:
            break

    log.info("OSM returned %d named shops", len(results))
    return results


def generate_real_sellers() -> list[dict]:
    """Return seller dicts for every entry in REAL_SELLERS with tiny GPS jitter."""
    sellers = []
    rng = random.Random(777)
    for row in REAL_SELLERS:
        name, address, lat, lon, category, tier, rating, reviews = row[:8]
        phone = row[8] if len(row) > 8 else ""
        # ±50 m jitter so the same shop run twice doesn't land on the exact same pixel
        jlat, jlon = random_point_within_radius(lat, lon, 0.05)
        sellers.append({
            "seller_id":         str(uuid.uuid5(uuid.NAMESPACE_DNS, name + address)),
            "name":              name,
            "address":           address,
            "latitude":          round(jlat, 6),
            "longitude":         round(jlon, 6),
            "category":          category,
            "subscription_tier": tier,
            "rating":            rating,
            "review_count":      reviews,
            "phone":             phone,
        })
    return sellers


# ─────────────────────────────────────────────────────────────────────────────
# RabbitMQ helpers  (connection per thread — pika is not thread-safe)
# ─────────────────────────────────────────────────────────────────────────────

import threading
_rmq_local = threading.local()


def get_rmq_channel():
    """Return a per-thread pika channel, creating it on first use."""
    if not hasattr(_rmq_local, "channel") or _rmq_local.channel.is_closed:
        params = pika.URLParameters(RABBITMQ_URL)
        params.heartbeat = 60
        _rmq_local.conn    = pika.BlockingConnection(params)
        _rmq_local.channel = _rmq_local.conn.channel()
        # Declare exchanges (idempotent)
        _rmq_local.channel.exchange_declare(EXCHANGE_SELLER, "topic", durable=True)
        _rmq_local.channel.exchange_declare(EXCHANGE_CAT,    "topic", durable=True)
    return _rmq_local.channel


def publish_seller_verified(seller: dict) -> bool:
    """Publish seller.verified event to padosme.events exchange."""
    event = {
        "event_id":         str(uuid.uuid4()),
        "event_type":       "seller.verified",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "seller_id":        seller["seller_id"],
        "name":             seller["name"],
        "address":          seller["address"],
        "latitude":         seller["latitude"],
        "longitude":        seller["longitude"],
        "h3_cell":          "",           # indexing service computes this if empty
        "rating":           seller["rating"],
        "review_count":     seller["review_count"],
        "subscription_tier": seller["subscription_tier"],
        "available":        True,
        "status":           "active",
        "categories":       [seller["category"]],
        "products":         [],
    }
    props = pika.BasicProperties(delivery_mode=2, content_type="application/json")
    for attempt in range(MAX_RETRIES):
        try:
            ch = get_rmq_channel()
            ch.basic_publish(
                exchange=EXCHANGE_SELLER,
                routing_key="seller.verified",
                body=json.dumps(event),
                properties=props,
            )
            return True
        except Exception as exc:
            log.debug("seller.verified publish failed (attempt %d): %s", attempt + 1, exc)
            _rmq_local.channel = None   # force reconnect
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue API
# ─────────────────────────────────────────────────────────────────────────────

_session_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_session_local, "s"):
        s = requests.Session()
        s.mount("http://", requests.adapters.HTTPAdapter(max_retries=2))
        _session_local.s = s
    return _session_local.s


def create_catalogue(seller: dict) -> Optional[str]:
    """POST /catalogs and return catalog_id, or None on failure."""
    payload = {
        "name":        seller["name"],
        "description": f"Quality {seller['category'].lower()} products in {seller['address']}",
        "cover_image": f"https://cdn.padosme.in/seed/{seller['seller_id']}.jpg",
        "type":        "product",
        "visibility":  "public",
        "seller_id":   seller["seller_id"],   # running binary reads seller_id from body
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = get_session().post(
                f"{CATALOGUE_URL}/catalogs",
                json=payload,
                headers=auth_headers(seller["seller_id"]),
                timeout=15,
            )
            if r.status_code in (200, 201):
                data = r.json()
                return data.get("id") or data.get("catalog_id") or data.get("_id")
            log.debug("POST /catalogs status=%d (attempt %d)", r.status_code, attempt + 1)
        except Exception as exc:
            log.debug("POST /catalogs exception (attempt %d): %s", attempt + 1, exc)
        time.sleep(RETRY_BACKOFF * (2 ** attempt))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Per-seller worker
# ─────────────────────────────────────────────────────────────────────────────

def seed_one(seller: dict, skip_catalogue: bool) -> dict:
    result = {
        "seller_id":       seller["seller_id"],
        "event_published": False,
        "catalogue_ok":    False,
        "lat":             seller["latitude"],
        "lon":             seller["longitude"],
        "distance_km":     haversine_km(BASE_LAT, BASE_LON, seller["latitude"], seller["longitude"]),
    }

    result["event_published"] = publish_seller_verified(seller)

    if not skip_catalogue:
        catalog_id = create_catalogue(seller)
        result["catalogue_ok"] = catalog_id is not None

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Verification via Redis FT.SEARCH
# ─────────────────────────────────────────────────────────────────────────────

def verify_via_redis(radius_km: float = RADIUS_KM, wait_secs: float = 3.0) -> dict:
    """
    Wait briefly for the indexing service to process events, then query
    Redis FT.SEARCH idx:sellers with a geo filter and return counts.
    """
    console.print(f"\n[dim]Waiting {wait_secs}s for indexing service to process events…[/dim]")
    time.sleep(wait_secs)

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)

    # Total sellers in Redis
    try:
        ft_info = r.execute_command("FT.INFO", "idx:sellers")
        info_dict = {}
        for i in range(0, len(ft_info) - 1, 2):
            info_dict[ft_info[i]] = ft_info[i + 1]
        total_indexed = int(info_dict.get("num_docs", 0))
    except Exception as exc:
        log.warning("Could not read FT.INFO: %s", exc)
        total_indexed = -1

    # Sellers within radius_km of Bangalore
    # FT.SEARCH geo filter: @location:[lon lat radius km]
    geo_query = f"@location:[{BASE_LON} {BASE_LAT} {radius_km} km]"
    try:
        result = r.execute_command(
            "FT.SEARCH", "idx:sellers", geo_query,
            "LIMIT", "0", "0",   # 0 results returned — only total count
        )
        # result[0] is the total count
        within_radius = int(result[0]) if result else 0
    except Exception as exc:
        log.warning("FT.SEARCH geo query failed: %s", exc)
        within_radius = -1

    return {
        "total_indexed":  total_indexed,
        "within_radius":  within_radius,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Search — Redis FT.SEARCH helpers
# ─────────────────────────────────────────────────────────────────────────────

TIER_COLORS = {
    "platinum": "bright_cyan",
    "gold":     "yellow",
    "silver":   "bright_white",
    "bronze":   "orange3",
    "free":     "dim",
}


def _parse_ft_response(response: list) -> tuple[int, list[dict]]:
    """FT.SEARCH returns [total, key, [f,v,...], key, [f,v,...], ...]"""
    if not response:
        return 0, []
    total = int(response[0])
    docs, i = [], 1
    while i < len(response):
        key = response[i]
        fields_raw = response[i + 1] if i + 1 < len(response) else []
        i += 2
        doc = {"_key": key}
        if isinstance(fields_raw, list):
            for j in range(0, len(fields_raw) - 1, 2):
                doc[fields_raw[j]] = fields_raw[j + 1]
        docs.append(doc)
    return total, docs


def _build_ft_query(keyword: str, category: str, radius_km: float,
                    available_only: bool, tier: str,
                    user_lat: float, user_lon: float) -> str:
    parts = []
    if keyword:
        # Split multi-word keywords; each word is searched independently with wildcard
        words = [w.replace("-", "\\-").replace(".", "\\.").replace("&", "\\&")
                 for w in keyword.strip().split() if w]
        if words:
            # Match if ANY word appears in name or address
            word_clauses = " | ".join(
                f"(@name:{w}* | @address:{w}*)" for w in words
            )
            parts.append(f"({word_clauses})")
    if category:
        safe_cat = category.replace(" ", "\\ ").replace("&", "\\&")
        parts.append(f"@category:{{{safe_cat}}}")
    if available_only:
        parts.append("@available:{true}")
    if tier:
        parts.append(f"@subscription_tier:{{{tier}}}")
    parts.append(f"@location:[{user_lon} {user_lat} {radius_km} km]")
    return " ".join(parts)


def run_search(
    r: redis.Redis,
    keyword: str = "",
    category: str = "",
    radius_km: float = 10.0,
    sort_by: str = "rating",
    available_only: bool = False,
    tier: str = "",
    limit: int = 20,
    user_lat: float = BASE_LAT,
    user_lon: float = BASE_LON,
) -> tuple[int, list[dict]]:
    query = _build_ft_query(keyword, category, radius_km, available_only, tier, user_lat, user_lon)
    cmd   = ["FT.SEARCH", "idx:sellers", query, "LIMIT", "0", str(limit)]
    if sort_by in ("rating", "popularity", "review_count") and not keyword:
        cmd += ["SORTBY", sort_by, "DESC"]
    try:
        response = r.execute_command(*cmd)
    except Exception as exc:
        console.print(f"[red]FT.SEARCH failed: {exc}[/red]")
        return 0, []
    total, docs = _parse_ft_response(response)
    for doc in docs:
        try:
            doc["_dist"] = haversine_km(user_lat, user_lon,
                                        float(doc.get("lat", user_lat)),
                                        float(doc.get("lon", user_lon)))
        except (ValueError, TypeError):
            doc["_dist"] = 0.0
    if sort_by == "distance":
        docs.sort(key=lambda d: d["_dist"])
    elif sort_by in ("rating", "popularity") and keyword:
        docs.sort(key=lambda d: float(d.get(sort_by, 0)), reverse=True)
    return total, docs


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL geo fallback — used when Redis TTL has expired
# ─────────────────────────────────────────────────────────────────────────────

def run_search_pg(
    keyword: str = "",
    category: str = "",
    radius_km: float = 10.0,
    sort_by: str = "rating",
    available_only: bool = False,
    tier: str = "",
    limit: int = 20,
    user_lat: float = BASE_LAT,
    user_lon: float = BASE_LON,
) -> tuple[int, list[dict]]:
    """
    Fallback geo search against PostgreSQL indexed_sellers + catalog_db.seller_geo.
    Called when Redis FT.SEARCH returns 0 results (TTL expired).
    """
    try:
        conn = psycopg2.connect(PG_DSN)
        cur  = conn.cursor()

        # Use the Haversine formula inside Postgres to filter by radius
        cur.execute(
            """
            SELECT seller_id, name, latitude, longitude, status
            FROM   indexed_sellers
            WHERE  status = 'active'
              AND  (
                6371 * acos(
                  cos(radians(%s)) * cos(radians(latitude)) *
                  cos(radians(longitude) - radians(%s)) +
                  sin(radians(%s)) * sin(radians(latitude))
                )
              ) <= %s
            LIMIT %s
            """,
            (user_lat, user_lon, user_lat, radius_km, limit * 3),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return 0, []

        # Enrich with category/rating/address from MongoDB seller_geo
        try:
            mc   = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=2000)
            geo  = mc["catalog_db"].seller_geo
            meta = {d["seller_id"]: d for d in geo.find(
                {"seller_id": {"$in": [r[0] for r in rows]}},
                {"_id": 0, "seller_id": 1, "category": 1, "rating": 1,
                 "review_count": 1, "subscription_tier": 1, "address": 1},
            )}
            mc.close()
        except Exception:
            meta = {}

        docs = []
        for row in rows:
            sid, name, lat, lon, status = row
            m    = meta.get(sid, {})
            cat  = m.get("category", "")
            dist = haversine_km(user_lat, user_lon, lat, lon)

            # Apply filters not in SQL
            if category and cat.lower() != category.lower():
                continue
            if tier and m.get("subscription_tier", "") != tier:
                continue

            docs.append({
                "seller_id":         sid,
                "name":              name,
                "lat":               str(lat),
                "lon":               str(lon),
                "category":          cat,
                "rating":            str(m.get("rating", 0)),
                "review_count":      str(m.get("review_count", 0)),
                "subscription_tier": m.get("subscription_tier", "free"),
                "available":         "true" if status == "active" else "false",
                "address":           m.get("address", ""),
                "_dist":             dist,
            })

        if sort_by == "distance":
            docs.sort(key=lambda d: d["_dist"])
        elif sort_by in ("rating", "popularity"):
            docs.sort(key=lambda d: float(d.get("rating", 0)), reverse=True)

        # keyword filter (client-side)
        if keyword:
            kw = keyword.lower()
            docs = [d for d in docs if kw in d["name"].lower() or kw in d["address"].lower()]

        docs = docs[:limit]
        return len(docs), docs

    except Exception as exc:
        log.warning("PostgreSQL fallback search failed: %s", exc)
        return 0, []


# ─────────────────────────────────────────────────────────────────────────────
# Product search — find specific items in nearby shops
# ─────────────────────────────────────────────────────────────────────────────

def search_products(
    r: redis.Redis,
    product_query: str,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    limit: int = 20,
) -> list[dict]:
    """
    Find nearby sellers that carry the product.
    1. FT.SEARCH Redis for sellers in matching categories within radius.
    2. Batch-query MongoDB items for those seller_ids where name matches.
    3. Fall back to MongoDB seller_geo geo-filter if Redis is empty.
    """
    product_lower = product_query.lower().strip()

    # 1. Which categories carry this product?
    category_matches: dict[str, list[str]] = {}
    for cat, products in CATEGORY_PRODUCTS.items():
        matched = [p for p in products if product_lower in p.lower()]
        if matched:
            category_matches[cat] = matched

    if not category_matches:
        return []

    seller_docs: dict[str, dict] = {}   # seller_id → redis doc

    # 2. FT.SEARCH Redis for nearby sellers in each matching category
    for category in category_matches:
        safe_cat = (category
                    .replace(" ", "\\ ")
                    .replace("&", "\\&")
                    .replace("-", "\\-"))
        ft_query = (f"@category:{{{safe_cat}}} "
                    f"@location:[{user_lon} {user_lat} {radius_km} km]")
        try:
            response = r.execute_command(
                "FT.SEARCH", "idx:sellers", ft_query,
                "LIMIT", "0", str(limit * 5),
            )
        except Exception as exc:
            console.print(f"[dim]FT.SEARCH error for {category}: {exc}[/dim]")
            continue

        _, docs = _parse_ft_response(response)
        for doc in docs:
            key = doc.get("_key", "")
            sid = key.replace("seller:", "") if key.startswith("seller:") else key
            if sid and sid not in seller_docs:
                doc["_category"] = category
                seller_docs[sid] = doc

    # 3. Batch-fetch real items from MongoDB for those seller_ids
    items_by_seller: dict[str, list[dict]] = {}
    if seller_docs:
        try:
            mc = pymongo.MongoClient(MONGO_URL, authSource="admin",
                                     serverSelectionTimeoutMS=3000)
            mdb = mc[MONGO_DB]
            cursor = mdb.items.find(
                {
                    "seller_id": {"$in": list(seller_docs.keys())},
                    "name": {"$regex": product_lower, "$options": "i"},
                },
                {"_id": 0, "seller_id": 1, "name": 1, "category": 1,
                 "price": 1, "quantity": 1, "tags": 1},
            )
            for item in cursor:
                sid = str(item.get("seller_id", ""))
                items_by_seller.setdefault(sid, []).append(item)
            mc.close()
        except Exception:
            pass

    results: list[dict] = []
    seen: set[str] = set()

    for sid, doc in seller_docs.items():
        if sid in seen:
            continue
        seen.add(sid)

        try:
            s_lat = float(doc.get("lat", user_lat))
            s_lon = float(doc.get("lon", user_lon))
            dist  = haversine_km(user_lat, user_lon, s_lat, s_lon)
        except (ValueError, TypeError):
            dist = 0.0

        category = doc.get("_category", "")

        # Use real MongoDB items if available, else fall back to category list
        real_items = items_by_seller.get(sid, [])
        if real_items:
            item_list = [
                {
                    "name":     it.get("name", "—"),
                    "category": it.get("category", category),
                    "price":    int(it.get("price", 0)),
                    "quantity": int(it.get("quantity", 0)),
                    "tags":     it.get("tags", []),
                }
                for it in real_items[:6]
            ]
        else:
            # Seller is in the right category but has no matching items in DB
            # — skip entirely so we only show sellers that truly stock the product
            continue

        results.append({
            "seller_id":    sid,
            "name":         doc.get("name", sid[:8]),
            "address":      doc.get("address", "—"),
            "rating":       float(doc.get("rating", 0)),
            "review_count": doc.get("review_count", "0"),
            "tier":         doc.get("subscription_tier", "free"),
            "available":    doc.get("available", "true"),
            "category":     category,
            "lat":          doc.get("lat", ""),
            "lon":          doc.get("lon", ""),
            "_dist":        dist,
            "_items":       item_list,
        })

    # 4. If Redis returned nothing, fall back to MongoDB seller_geo geo-filter
    if not results:
        try:
            mc2 = pymongo.MongoClient(MONGO_URL, authSource="admin",
                                      serverSelectionTimeoutMS=3000)
            mdb2 = mc2[MONGO_DB]
            target_cats = list(category_matches.keys())

            # Fetch sellers in the right categories
            for gdoc in mdb2.seller_geo.find(
                {"category": {"$in": target_cats}},
                {"_id": 0, "seller_id": 1, "name": 1, "address": 1,
                 "lat": 1, "lon": 1, "rating": 1, "review_count": 1,
                 "subscription_tier": 1, "category": 1},
            ):
                gsid = str(gdoc.get("seller_id", ""))
                if not gsid or gsid in seen:
                    continue
                glat = float(gdoc.get("lat", BASE_LAT))
                glon = float(gdoc.get("lon", BASE_LON))
                dist = haversine_km(user_lat, user_lon, glat, glon)
                if dist > radius_km:
                    continue
                seen.add(gsid)

                # Fetch real items for this seller
                real_items = list(mdb2.items.find(
                    {"seller_id": gsid,
                     "name": {"$regex": product_lower, "$options": "i"}},
                    {"_id": 0, "name": 1, "category": 1, "price": 1,
                     "quantity": 1, "tags": 1},
                    limit=6,
                ))
                if not real_items:
                    continue

                cat = gdoc.get("category", "")
                item_list = [
                    {
                        "name":     it.get("name", "—"),
                        "category": it.get("category", cat),
                        "price":    int(it.get("price", 0)),
                        "quantity": int(it.get("quantity", 0)),
                        "tags":     it.get("tags", []),
                    }
                    for it in real_items
                ]
                results.append({
                    "seller_id":    gsid,
                    "name":         gdoc.get("name", gsid[:8]),
                    "address":      gdoc.get("address", "—"),
                    "rating":       float(gdoc.get("rating", 0)),
                    "review_count": str(gdoc.get("review_count", 0)),
                    "tier":         gdoc.get("subscription_tier", "free"),
                    "available":    "true",
                    "category":     cat,
                    "lat":          str(glat),
                    "lon":          str(glon),
                    "_dist":        dist,
                    "_items":       item_list,
                })
            mc2.close()
        except Exception:
            pass

    results.sort(key=lambda d: d["_dist"])
    return results[:limit]


def _search_products_impl(
    r: redis.Redis,
    product_query: str,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    limit: int = 20,
) -> list[dict]:
    return search_products.__wrapped__(r, product_query, user_lat, user_lon, radius_km, limit)  # type: ignore


def render_product_results(results: list[dict], product_query: str,
                           user_lat: float, user_lon: float, radius_km: float,
                           expanded: bool = False) -> None:
    console.print()
    if not results:
        console.print(Panel(
            f"[yellow]No shops near you sell '[bold]{product_query}[/bold]' within {radius_km:.0f} km.[/yellow]\n"
            "[dim]Try :radius 5 to widen the search, or check the spelling.[/dim]",
            border_style="yellow",
        ))
        console.print()
        return

    expanded_note = f"  [yellow](radius auto-expanded to {radius_km:.0f} km)[/yellow]" if expanded else ""
    console.print(Panel(
        f"[bold]{len(results)}[/bold] shop{'s' if len(results) != 1 else ''} near you sell "
        f"[bold cyan]{product_query}[/bold cyan]  "
        f"[dim](within {radius_km:.0f} km, nearest first)[/dim]{expanded_note}",
        border_style="cyan", padding=(0, 1),
    ))

    for i, shop in enumerate(results, 1):
        rating = shop["rating"]
        stars  = "★" * int(rating) + "☆" * (5 - int(rating))
        tier   = shop["tier"]
        avail  = "[green]open[/green]" if shop["available"] == "true" else "[dim]closed[/dim]"
        tier_color = TIER_COLORS.get(tier, "dim")
        s_lat  = shop.get("lat", "")
        s_lon  = shop.get("lon", "")

        console.print(
            f"  [bold]{i}.[/bold] [bold]{shop['name']}[/bold]  "
            f"[dim]{shop['address']}[/dim]  "
            f"[cyan]{shop['_dist']:.2f} km[/cyan]  "
            f"{rating:.1f} [dim]{stars}[/dim]  "
            f"[{tier_color}]{tier}[/{tier_color}]  {avail}"
        )
        if s_lat and s_lon:
            gmaps_url  = f"https://www.google.com/maps?q={s_lat},{s_lon}"
            apple_url  = f"https://maps.apple.com/?q={s_lat},{s_lon}"
            console.print(
                f"     [dim]📍 {s_lat}, {s_lon}  [/dim]"
                f"[link={gmaps_url}][bold cyan]Google Maps ↗[/bold cyan][/link]"
                f"[dim]  ·  [/dim]"
                f"[link={apple_url}][bold cyan]Apple Maps ↗[/bold cyan][/link]"
            )

        # Show matching items
        item_tbl = Table(box=box.SIMPLE, show_header=True, pad_edge=False,
                         show_edge=False, padding=(0, 2))
        item_tbl.add_column("Item",        style="bold white", min_width=28)
        item_tbl.add_column("Category",    style="cyan",       min_width=16)
        item_tbl.add_column("Price (₹)",   justify="right",    width=12)
        item_tbl.add_column("Qty",         justify="right",    width=6)
        item_tbl.add_column("Tags",        style="dim",        min_width=20)

        for item in shop["_items"]:
            price_paise = item.get("price", 0)
            price_str   = f"{price_paise / 100:.2f}" if price_paise else "—"
            tags        = ", ".join(item.get("tags", [])[:4]) or "—"
            item_tbl.add_row(
                item.get("name", "—"),
                item.get("category", "—"),
                price_str,
                str(item.get("quantity", "—")),
                tags,
            )
        console.print(item_tbl)
        console.print()


def render_search_results(total: int, docs: list[dict], desc: str, limit: int,
                          expanded: bool = False) -> None:
    shown = len(docs)
    expanded_note = "  [yellow](radius auto-expanded)[/yellow]" if expanded else ""
    header = (
        f"[bold]{shown}[/bold] result{'s' if shown != 1 else ''}"
        + (f"  [dim](of {total} total)[/dim]" if total > shown else "")
        + f"  [dim]— {desc}[/dim]"
        + expanded_note
    )
    console.print()
    console.print(Panel(header, border_style="cyan", padding=(0, 1)))

    if not docs:
        console.print("[dim]  No sellers found.[/dim]\n")
        return

    tbl = Table(box=box.SIMPLE_HEAVY, show_lines=False, pad_edge=False)
    tbl.add_column("#",         style="dim",   width=4,  justify="right")
    tbl.add_column("Name",      style="bold",  min_width=26)
    tbl.add_column("Category",  style="cyan",  min_width=16)
    tbl.add_column("Dist (km)", justify="right", width=10)
    tbl.add_column("Rating",    justify="right", width=10)
    tbl.add_column("Reviews",   justify="right", width=8)
    tbl.add_column("Tier",      justify="center", width=10)
    tbl.add_column("Status",    justify="center", width=8)
    tbl.add_column("Address",   style="dim",   min_width=28)
    tbl.add_column("GPS / Maps",style="dim",   min_width=20)
    tbl.add_column("Phone",     style="green", min_width=14)

    for i, doc in enumerate(docs, 1):
        rating  = float(doc.get("rating", 0))
        tier    = doc.get("subscription_tier", "free")
        avail   = doc.get("available", "false")
        stars   = "★" * int(rating) + ("½" if (rating % 1) >= 0.5 else "") + "☆" * (5 - int(rating) - (1 if (rating % 1) >= 0.5 else 0))
        d_lat   = doc.get("lat", "")
        d_lon   = doc.get("lon", "")
        if d_lat and d_lon:
            gmaps_url = f"https://www.google.com/maps?q={d_lat},{d_lon}"
            apple_url = f"https://maps.apple.com/?q={d_lat},{d_lon}"
            gps_cell  = (
                f"[link={gmaps_url}][cyan]{d_lat},{d_lon}[/cyan][/link]\n"
                f"[link={gmaps_url}][dim]Google Maps ↗[/dim][/link]  "
                f"[link={apple_url}][dim]Apple Maps ↗[/dim][/link]"
            )
        else:
            gps_cell = "—"
        tbl.add_row(
            str(i),
            doc.get("name", "—"),
            doc.get("category", "—").replace(",", ", "),
            f"{doc['_dist']:.2f}",
            f"{rating:.1f} [dim]{stars}[/dim]",
            str(doc.get("review_count", "—")),
            f"[{TIER_COLORS.get(tier, 'dim')}]{tier}[/{TIER_COLORS.get(tier, 'dim')}]",
            "[green]open[/green]" if avail == "true" else "[dim]closed[/dim]",
            doc.get("address", "—"),
            gps_cell,
            doc.get("phone", "—") or "—",
        )
    console.print(tbl)
    console.print()


def _resolve_location(raw: str) -> tuple[float, float] | None:
    """
    Try to resolve a location string to (lat, lon).
    Accepts:
      - Area name  e.g. "Koramangala"
      - Coordinates e.g. "12.9352 77.6245" or "12.9352, 77.6245"
    Returns None if unrecognised.
    """
    # Try area name lookup first
    key = raw.strip().lower()
    if key in BANGALORE_AREAS:
        return BANGALORE_AREAS[key]
    # Partial match
    matches = [(name, coords) for name, coords in BANGALORE_AREAS.items() if key in name]
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        names = ", ".join(m[0].title() for m in matches[:5])
        console.print(f"[yellow]Multiple matches: {names} — be more specific.[/yellow]")
        return None

    # Try numeric coordinates
    parts = raw.replace(",", " ").split()
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except ValueError:
            pass
    return None


def _ask_location(default_lat: float, default_lon: float) -> tuple[float, float, bool]:
    """Prompt user for area name or GPS coordinates at startup."""
    console.print()
    # Show available areas
    # Show key areas grouped by zone so user can see RT Nagar cluster too
    _key_areas = [
        # Central
        "MG Road", "Brigade Road", "Shivajinagar", "Richmond Town",
        # North cluster
        "RT Nagar", "Rahamath Nagar", "Hebbal", "Sahakar Nagar",
        "Kammanahalli", "Nagawara", "Sanjay Nagar", "Ganganagar",
        "Rajanukunte", "Yelahanka", "Thanisandra", "Hennur",
        # South
        "Koramangala", "Jayanagar", "JP Nagar", "BTM Layout",
        # East
        "Indiranagar", "Whitefield", "Marathahalli", "HSR Layout",
        # West
        "Rajajinagar", "Malleswaram", "Vijayanagar",
    ]
    area_list = "  ".join(a for a in _key_areas) + "  …"
    console.print(Panel(
        "[bold]Where are you?[/bold]\n"
        "[dim]Type an area name or GPS coordinates (lat lon).\n"
        f"Areas: [cyan]{area_list}[/cyan]\n"
        "Or get GPS from Google Maps → long-press your spot.\n"
        "Press [bold]Enter[/bold] to use Bangalore centre.[/dim]",
        border_style="yellow",
        padding=(0, 1),
    ))
    while True:
        try:
            raw = console.input("[yellow]Your area or location[/yellow]: ").strip()
        except (KeyboardInterrupt, EOFError):
            return default_lat, default_lon

        if not raw:
            console.print(f"[dim]Using Bangalore centre: {default_lat}, {default_lon}[/dim]")
            return default_lat, default_lon, False

        result = _resolve_location(raw)
        if result:
            lat, lon = result
            is_area = raw.strip().lower() in BANGALORE_AREAS
            gmaps_url = f"https://www.google.com/maps?q={lat},{lon}"
            console.print(
                f"[green]Location set: [cyan]{lat:.4f}, {lon:.4f}[/cyan]  "
                f"[link={gmaps_url}][dim]Google Maps ↗[/dim][/link][/green]"
            )
            return lat, lon, is_area
        console.print("[red]Not recognised. Try area name like 'Koramangala' or coords like '12.9352 77.6245'[/red]")


# ─────────────────────────────────────────────────────────────────────────────
# OpenStreetMap Overpass — live real-world shop data (no API key needed)
# ─────────────────────────────────────────────────────────────────────────────

_OSM_SHOP_CATEGORY_MAP = {
    "supermarket": "Food & Grocery",
    "convenience": "Food & Grocery",
    "grocery":     "Food & Grocery",
    "greengrocer": "Food & Grocery",
    "bakery":      "Food & Grocery",
    "butcher":     "Food & Grocery",
    "seafood":     "Food & Grocery",
    "dairy":       "Food & Grocery",
    "deli":        "Food & Grocery",
    "general":     "Food & Grocery",
    "kirana":      "Food & Grocery",
    "mall":        "Food & Grocery",
    "electronics": "Electronics",
    "computer":    "Electronics",
    "mobile":      "Electronics",
    "clothes":     "Clothing",
    "fashion":     "Clothing",
    "shoes":       "Clothing",
    "books":       "Books",
    "sports":      "Sports",
    "hardware":    "Hardware",
    "pharmacy":    "Health & Wellness",
    "chemist":     "Health & Wellness",
    "medical":     "Health & Wellness",
    "toys":        "Toys & Baby",
    "stationery":  "Stationery",
    "car_parts":   "Automotive",
    "car":         "Automotive",
    "beauty":      "Beauty & Personal Care",
    "cosmetics":   "Beauty & Personal Care",
}

_OSM_AMENITY_CATEGORY_MAP = {
    "restaurant":  "Food & Grocery",
    "cafe":        "Food & Grocery",
    "fast_food":   "Food & Grocery",
    "marketplace": "Food & Grocery",
    "pharmacy":    "Health & Wellness",
    "hospital":    "Health & Wellness",
    "clinic":      "Health & Wellness",
    "school":      "Books",
    "library":     "Books",
}


def osm_nearby_shops(
    user_lat: float, user_lon: float,
    radius_m: int = 2000,
    keyword: str = "",
    limit: int = 30,
) -> list[dict]:
    """
    Query OpenStreetMap Overpass API for real shops near (user_lat, user_lon).
    Returns a list of dicts compatible with render_search_results.
    Falls back to empty list on any error (network, timeout, etc.).
    """
    radius_m = min(radius_m, 50_000)   # cap at 50 km for sanity
    overpass_query = f"""
[out:json][timeout:10];
(
  node(around:{radius_m},{user_lat},{user_lon})[shop];
  node(around:{radius_m},{user_lat},{user_lon})[amenity~"restaurant|cafe|fast_food|pharmacy|marketplace"];
);
out body;
"""
    try:
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data=overpass_query.strip(),
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug("OSM Overpass query failed: %s", exc)
        return []

    results = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:en") or tags.get("brand")
        if not name:
            continue
        lat = el.get("lat", user_lat)
        lon = el.get("lon", user_lon)
        dist = haversine_km(user_lat, user_lon, lat, lon)

        # Determine category
        shop_type   = tags.get("shop", "").lower()
        amenity     = tags.get("amenity", "").lower()
        category    = (_OSM_SHOP_CATEGORY_MAP.get(shop_type)
                       or _OSM_AMENITY_CATEGORY_MAP.get(amenity)
                       or "Food & Grocery")

        # Build a human-readable address from OSM tags
        addr_parts = [
            tags.get("addr:housenumber", ""),
            tags.get("addr:street", ""),
            tags.get("addr:suburb", "") or tags.get("addr:city_block", ""),
            tags.get("addr:city", "Bangalore"),
        ]
        address = ", ".join(p for p in addr_parts if p) or tags.get("addr:full", "Bangalore")

        phone = tags.get("phone") or tags.get("contact:phone") or ""

        # Apply keyword filter if given
        if keyword and keyword.lower() not in name.lower():
            continue

        results.append({
            "seller_id":         el.get("id", ""),
            "name":              name,
            "address":           address,
            "category":          category,
            "subscription_tier": "free",
            "rating":            0.0,
            "review_count":      "—",
            "available":         "true",
            "phone":             phone,
            "lat":               str(lat),
            "lon":               str(lon),
            "_dist":             dist,
            "_source":           "OpenStreetMap (live)",
        })

    results.sort(key=lambda d: d["_dist"])
    return results[:limit]


def _radius_expand_steps(current_km: float, max_km: float = 15.0) -> list[float]:
    """
    Return surrounding-area radii to try when no results found at current_km.
    Expands in 5 km steps outward from the current boundary, up to max_km (default 15 km).
    Only searches radii strictly larger than current_km.
    """
    steps = []
    # Start from the next 5-km boundary above current_km
    start = math.ceil((current_km + 0.1) / 5) * 5
    r = start
    while r <= max_km:
        steps.append(float(r))
        r += 5
    # Always include max_km as final step if not already covered
    if not steps or steps[-1] < max_km:
        steps.append(float(max_km))
    return steps


# Zone → list of (display_name, key_in_BANGALORE_AREAS)
_AREA_ZONES: dict[str, list[tuple[str, str]]] = {
    "Central": [
        ("MG Road",          "mg road"),
        ("Brigade Road",     "brigade road"),
        ("Commercial St",    "commercial street"),
        ("Shivajinagar",     "shivajinagar"),
        ("Richmond Town",    "richmond town"),
        ("Lavelle Road",     "lavelle road"),
        ("Ulsoor",           "ulsoor"),
        ("Frazer Town",      "frazer town"),
    ],
    "North": [
        ("RT Nagar",         "rt nagar"),
        ("Rahamath Nagar",   "rahamath nagar"),
        ("Hebbal",           "hebbal"),
        ("Sahakar Nagar",    "sahakar nagar"),
        ("Nagawara",         "nagawara"),
        ("Manyata",          "manyata tech park"),
        ("Sanjay Nagar",     "sanjay nagar"),
        ("Ganganagar",       "ganganagar"),
        ("Yelahanka",        "yelahanka"),
        ("Thanisandra",      "thanisandra"),
        ("Hennur",           "hennur"),
        ("Jakkur",           "jakkur"),
        ("Rajanukunte",      "rajanukunte"),
        ("Devanahalli",      "devanahalli"),
        ("Kammanahalli",     "kammanahalli"),
        ("Banaswadi",        "banaswadi"),
    ],
    "South": [
        ("Jayanagar",        "jayanagar"),
        ("JP Nagar",         "jp nagar"),
        ("BTM Layout",       "btm layout"),
        ("Basavanagudi",     "basavanagudi"),
        ("Banashankari",     "banashankari"),
        ("Bannerghatta Rd",  "bannerghatta road"),
        ("Electronic City",  "electronic city"),
        ("Sarjapur",         "sarjapur"),
        ("Sarjapur Road",    "sarjapur road"),
        ("Hulimavu",         "hulimavu"),
        ("Kanakapura Rd",    "kanakapura road"),
    ],
    "East": [
        ("Indiranagar",      "indiranagar"),
        ("Koramangala",      "koramangala"),
        ("HSR Layout",       "hsr layout"),
        ("Domlur",           "domlur"),
        ("HAL Airport Rd",   "hal airport road"),
        ("Marathahalli",     "marathahalli"),
        ("Whitefield",       "whitefield"),
        ("Bellandur",        "bellandur"),
        ("Varthur",          "varthur"),
        ("KR Puram",         "kr puram"),
        ("Hoodi",            "hoodi"),
        ("CV Raman Nagar",   "cv raman nagar"),
    ],
    "West": [
        ("Rajajinagar",      "rajajinagar"),
        ("Malleswaram",      "malleswaram"),
        ("Vijayanagar",      "vijayanagar"),
        ("Nagarbhavi",       "nagarbhavi"),
        ("Kengeri",          "kengeri"),
        ("Mysore Road",      "mysore road"),
        ("Chamrajpet",       "chamrajpet"),
        ("Yeshwanthpur",     "yeshwanthpur"),
        ("Peenya",           "peenya"),
        ("Sadashivanagar",   "sadashivanagar"),
    ],
}


def _show_area_map() -> tuple[float, float, str] | None:
    """
    Display a numbered area picker grouped by zone.
    Returns (lat, lon, area_name) of the chosen area, or None if cancelled.
    """
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Area Map — Bangalore[/bold cyan]\n"
        "[dim]Pick a number to jump to that area, or press Enter to cancel.[/dim]",
        border_style="cyan",
    ))

    all_entries: list[tuple[str, str, str]] = []  # (zone, display_name, area_key)
    for zone, entries in _AREA_ZONES.items():
        for display, key in entries:
            all_entries.append((zone, display, key))

    # Print in columns grouped by zone
    for zone, entries in _AREA_ZONES.items():
        tbl = Table(box=box.SIMPLE, show_header=False, pad_edge=False,
                    show_edge=False, padding=(0, 1))
        tbl.add_column("Num",  style="bold cyan", width=5,  justify="right")
        tbl.add_column("Area", style="white",     min_width=20)
        tbl.add_column("GPS",  style="dim",       min_width=22)

        for display, key in entries:
            idx  = next(i + 1 for i, e in enumerate(all_entries) if e[2] == key)
            coords = BANGALORE_AREAS.get(key)
            gps_str = f"{coords[0]:.4f}, {coords[1]:.4f}" if coords else "—"
            tbl.add_row(str(idx), display, gps_str)

        console.print(f"\n  [bold yellow]{zone}[/bold yellow]")
        console.print(tbl)

    console.print()
    while True:
        try:
            raw = console.input("[cyan]Enter area number (or Enter to cancel):[/cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not raw:
            return None
        try:
            choice = int(raw)
            if 1 <= choice <= len(all_entries):
                zone, display, key = all_entries[choice - 1]
                coords = BANGALORE_AREAS.get(key)
                if coords:
                    console.print(f"[green]Selected: {display} ({coords[0]:.4f}, {coords[1]:.4f})[/green]")
                    return coords[0], coords[1], display
        except ValueError:
            pass
        console.print(f"[red]Enter a number between 1 and {len(all_entries)}.[/red]")


def _ask_and_search_custom_radius(
    r: redis.Redis,
    product_query: str,
    user_lat: float,
    user_lon: float,
    limit: int,
) -> bool:
    """
    After failing within 15 km, ask the user if they want to try a different radius.
    Returns True if results were found and rendered, False otherwise.
    """
    console.print(Panel(
        f"[yellow]'[bold]{product_query}[/bold]' not found within 15 km.[/yellow]\n"
        "[dim]Enter a custom radius (km) to search further, or press Enter to skip.[/dim]",
        border_style="yellow",
        padding=(0, 1),
    ))
    try:
        raw = console.input("[bold cyan]  Radius (km) >[/bold cyan] ").strip()
    except (KeyboardInterrupt, EOFError):
        return False

    if not raw:
        return False

    try:
        custom_km = float(raw)
        if custom_km <= 0:
            console.print("[red]Radius must be > 0[/red]")
            return False
    except ValueError:
        console.print("[red]Enter a number, e.g. 20[/red]")
        return False

    console.print(f"[dim]  Searching within {custom_km:.0f} km…[/dim]")
    results = search_products(r, product_query, user_lat, user_lon, custom_km, limit)
    if results:
        render_product_results(results, product_query, user_lat, user_lon,
                               custom_km, expanded=True)
        return True

    console.print(Panel(
        f"[red]'[bold]{product_query}[/bold]' still not found within {custom_km:.0f} km.[/red]\n"
        "[dim]Try a different area with :area <name> or a different spelling.[/dim]",
        border_style="red",
    ))
    return False


def interactive_search(r: redis.Redis, start_lat: float = None, start_lon: float = None) -> None:
    # If location not passed via CLI, ask the user
    is_area = False
    if start_lat is None or start_lon is None:
        start_lat, start_lon, is_area = _ask_location(BASE_LAT, BASE_LON)

    console.print()
    console.print(Panel.fit(
        "[bold cyan]Padosme — Seller Search[/bold cyan]\n"
        "[dim]Type a seller name/keyword to search, or use commands:\n"
        "  [bold]product <name>[/bold]        e.g. product condom  (search items across shops)\n"
        "  [bold]:map[/bold]                  pick an area from a numbered zone map\n"
        "  [bold]:area <name>[/bold]          e.g. :area Koramangala\n"
        "  [bold]:location <lat> <lon>[/bold]  e.g. :location 12.9352 77.6245\n"
        "  [bold]:category <name>[/bold]   [bold]:radius <km>[/bold]   "
        "[bold]:sort rating|distance|popularity[/bold]\n"
        "  [bold]:tier gold|silver|...[/bold]   "
        "[bold]:available[/bold]   [bold]:limit <n>[/bold]   "
        "[bold]:reset[/bold]   [bold]:quit[/bold]\n"
        "Radius auto-expands when no results found at the current setting.[/dim]",
        border_style="cyan",
    ))

    state = dict(keyword="", category="", radius_km=2.0 if is_area else 10.0,
                 sort_by="distance" if is_area else "rating",
                 available_only=False, tier="", limit=20,
                 user_lat=start_lat, user_lon=start_lon)

    def _desc():
        parts = []
        if state["keyword"]:        parts.append(f'name~"{state["keyword"]}"')
        if state["category"]:       parts.append(f'category="{state["category"]}"')
        if state["tier"]:           parts.append(f'tier={state["tier"]}')
        if state["available_only"]: parts.append("available=true")
        parts.append(f"radius≤{state['radius_km']:.0f}km")
        parts.append(f"sort={state['sort_by']}")
        parts.append(f"📍 {state['user_lat']:.4f},{state['user_lon']:.4f}")
        return "  ".join(parts)

    def _run():
        total, docs = run_search(r, **state)

        if total == 0:
            # Redis TTL expired — fall back to PostgreSQL geo search
            total, docs = run_search_pg(**state)
            if total > 0:
                console.print("[dim]  (results from persistent store — Redis index expired)[/dim]")

        if total == 0 and state["keyword"]:
            # Try product catalogue search at current radius first
            product_results = search_products(
                r, state["keyword"],
                state["user_lat"], state["user_lon"],
                state["radius_km"], state["limit"],
            )
            if product_results:
                render_product_results(
                    product_results, state["keyword"],
                    state["user_lat"], state["user_lon"],
                    state["radius_km"],
                )
                return
            # Auto-expand product search up to 15 km
            _expand_steps = _radius_expand_steps(state["radius_km"], max_km=15.0)
            for r_try in _expand_steps:
                console.print(
                    f"[dim]  '[bold]{state['keyword']}[/bold]' not found within "
                    f"{state['radius_km']:.0f} km — expanding to {r_try:.0f} km…[/dim]"
                )
                product_results = search_products(
                    r, state["keyword"],
                    state["user_lat"], state["user_lon"],
                    r_try, state["limit"],
                )
                if product_results:
                    render_product_results(
                        product_results, state["keyword"],
                        state["user_lat"], state["user_lon"],
                        r_try, expanded=True,
                    )
                    return
            # Not found within 15 km — ask user if they want a custom radius
            _ask_and_search_custom_radius(
                r, state["keyword"],
                state["user_lat"], state["user_lon"],
                state["limit"],
            )
            return

        # Auto-expand radius when no seller results at the requested radius
        expanded_radius = None
        if total == 0:
            _expand_steps = _radius_expand_steps(state["radius_km"], max_km=15.0)
            for r_try in _expand_steps:
                console.print(
                    f"[dim]  Nothing within {state['radius_km']:.0f} km — "
                    f"searching surrounding areas up to {r_try:.0f} km…[/dim]"
                )
                expanded_state = {**state, "radius_km": r_try}
                total, docs = run_search(r, **expanded_state)
                if total == 0:
                    total, docs = run_search_pg(**expanded_state)
                if total > 0:
                    expanded_radius = r_try
                    break

        # Last resort: query live OpenStreetMap data for real nearby shops
        osm_docs: list[dict] = []
        if total == 0:
            console.print("[dim]  Local index empty — querying live OpenStreetMap data…[/dim]")
            final_radius = expanded_radius or state["radius_km"]
            osm_docs = osm_nearby_shops(
                state["user_lat"], state["user_lon"],
                radius_m=int(final_radius * 1000),
                keyword=state["keyword"],
                limit=state["limit"],
            )
            if osm_docs:
                console.print("[dim]  (results from OpenStreetMap — live map data)[/dim]")
                # Render as a plain table using osm_docs directly
                tbl = Table(box=box.SIMPLE_HEAVY, show_lines=False, pad_edge=False)
                tbl.add_column("#",         style="dim",  width=4,  justify="right")
                tbl.add_column("Name",      style="bold", min_width=26)
                tbl.add_column("Category",  style="cyan", min_width=16)
                tbl.add_column("Dist (km)", justify="right", width=10)
                tbl.add_column("Address",   style="dim",  min_width=30)
                tbl.add_column("GPS / Maps",style="dim",  min_width=22)
                tbl.add_column("Phone",     style="green",min_width=14)
                for idx, shop in enumerate(osm_docs, 1):
                    d_lat = shop.get("lat", "")
                    d_lon = shop.get("lon", "")
                    gps   = f"{d_lat},{d_lon}" if d_lat and d_lon else "—"
                    tbl.add_row(
                        str(idx),
                        shop["name"],
                        shop["category"],
                        f"{shop['_dist']:.2f}",
                        shop["address"],
                        gps,
                        shop.get("phone", "—") or "—",
                    )
                console.print()
                console.print(Panel(
                    f"[bold]{len(osm_docs)}[/bold] real shop{'s' if len(osm_docs) != 1 else ''} "
                    f"found via [cyan]OpenStreetMap[/cyan]  "
                    f"[dim](within {final_radius:.0f} km, nearest first)[/dim]"
                    + ("  [yellow](radius expanded)[/yellow]" if expanded_radius else ""),
                    border_style="cyan", padding=(0, 1),
                ))
                console.print(tbl)
                console.print()
                return

        # Still nothing after OSM fallback — ask user for a custom radius
        if total == 0 and not osm_docs:
            label = state["keyword"] if state["keyword"] else "sellers"
            console.print(Panel(
                f"[yellow]No results for '[bold]{label}[/bold]' within 15 km.[/yellow]\n"
                "[dim]Enter a custom radius (km) to search further, or press Enter to skip.[/dim]",
                border_style="yellow",
                padding=(0, 1),
            ))
            try:
                raw_r = console.input("[bold cyan]  Radius (km) >[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                raw_r = ""
            if raw_r:
                try:
                    custom_km = float(raw_r)
                    if custom_km > 0:
                        custom_state = {**state, "radius_km": custom_km}
                        total, docs = run_search(r, **custom_state)
                        if total == 0:
                            total, docs = run_search_pg(**custom_state)
                        if total > 0:
                            expanded_radius = custom_km
                        else:
                            console.print(f"[red]Still no results within {custom_km:.0f} km.[/red]")
                except ValueError:
                    console.print("[red]Invalid number.[/red]")

        desc = _desc()
        if expanded_radius is not None:
            desc = desc.replace(f"radius≤{state['radius_km']:.0f}km",
                                f"radius≤{expanded_radius:.0f}km")
        render_search_results(total, docs, desc, state["limit"],
                              expanded=expanded_radius is not None)

    _run()

    while True:
        try:
            raw = console.input("[bold cyan]search>[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        if not raw:
            continue
        if raw in (":quit", ":q", "exit", "quit"):
            console.print("[dim]Bye.[/dim]")
            break
        elif raw == ":reset":
            new_lat, new_lon, new_is_area = _ask_location(BASE_LAT, BASE_LON)
            state.update(keyword="", category="",
                         radius_km=2.0 if new_is_area else 10.0,
                         sort_by="distance" if new_is_area else "rating",
                         available_only=False, tier="",
                         user_lat=new_lat, user_lon=new_lon)
            _run()
        elif raw.startswith(":product ") or raw.lower().startswith("product "):
            product_query = raw.split(" ", 1)[1].strip() if " " in raw else ""
            if product_query:
                # Search at current radius first
                results = search_products(
                    r, product_query,
                    state["user_lat"], state["user_lon"],
                    state["radius_km"], state["limit"],
                )
                if results:
                    render_product_results(results, product_query,
                                           state["user_lat"], state["user_lon"],
                                           state["radius_km"])
                else:
                    # Auto-expand in 5 km steps up to 15 km
                    found = False
                    for r_try in _radius_expand_steps(state["radius_km"], max_km=15.0):
                        console.print(
                            f"[dim]  '[bold]{product_query}[/bold]' not found within "
                            f"{state['radius_km']:.0f} km — expanding to {r_try:.0f} km…[/dim]"
                        )
                        results = search_products(
                            r, product_query,
                            state["user_lat"], state["user_lon"],
                            r_try, state["limit"],
                        )
                        if results:
                            render_product_results(results, product_query,
                                                   state["user_lat"], state["user_lon"],
                                                   r_try, expanded=True)
                            found = True
                            break
                    if not found:
                        _ask_and_search_custom_radius(
                            r, product_query,
                            state["user_lat"], state["user_lon"],
                            state["limit"],
                        )
            else:
                console.print("[red]Usage: :product mango[/red]")
        elif raw == ":map":
            result = _show_area_map()
            if result:
                state["user_lat"], state["user_lon"] = result[0], result[1]
                state["radius_km"] = 2.0
                state["sort_by"]   = "distance"
                _run()
        elif raw.startswith(":area "):
            query = raw.split(" ", 1)[1].strip()
            result = _resolve_location(query)
            if result:
                state["user_lat"], state["user_lon"] = result
                state["radius_km"] = 2.0   # tight radius — only that locality
                state["sort_by"]   = "distance"
                gmaps_url = f"https://www.google.com/maps?q={result[0]},{result[1]}"
                console.print(
                    f"[green]Area set to '[bold]{query.title()}[/bold]' — "
                    f"GPS: [cyan]{result[0]:.4f}, {result[1]:.4f}[/cyan]  "
                    f"[link={gmaps_url}][dim]Google Maps ↗[/dim][/link]  "
                    f"— showing shops within 2 km, nearest first.[/green]"
                )
                _run()
            else:
                console.print("[red]Not recognised. Try: :area Koramangala  or  :area Indiranagar[/red]")
        elif raw.startswith(":location "):
            query = raw.split(" ", 1)[1].strip()
            result = _resolve_location(query)
            if result:
                state["user_lat"], state["user_lon"] = result
                gmaps_url2 = f"https://www.google.com/maps?q={result[0]},{result[1]}"
                console.print(
                    f"[green]Location → [cyan]{result[0]:.6f}, {result[1]:.6f}[/cyan]  "
                    f"[link={gmaps_url2}][dim]Google Maps ↗[/dim][/link][/green]"
                )
                _run()
            else:
                console.print("[red]Not recognised. Try: :location 12.9352 77.6245[/red]")
        elif raw.startswith(":category"):
            state["category"] = raw[len(":category"):].strip()
            _run()
        elif raw.startswith(":radius "):
            try:
                state["radius_km"] = float(raw.split()[1])
                _run()
            except ValueError:
                console.print("[red]Usage: :radius <km>[/red]")
        elif raw.startswith(":sort "):
            val = raw.split()[1].lower()
            if val in ("rating", "distance", "popularity"):
                state["sort_by"] = val
                _run()
            else:
                console.print("[red]Options: rating | distance | popularity[/red]")
        elif raw.startswith(":tier"):
            val = raw[len(":tier"):].strip().lower()
            if val in ("", "free", "bronze", "silver", "gold", "platinum"):
                state["tier"] = val
                _run()
            else:
                console.print("[red]Options: free | bronze | silver | gold | platinum[/red]")
        elif raw == ":available":
            state["available_only"] = not state["available_only"]
            console.print(f"[dim]available filter: {state['available_only']}[/dim]")
            _run()
        elif raw.startswith(":limit "):
            try:
                state["limit"] = int(raw.split()[1])
                _run()
            except ValueError:
                console.print("[red]Usage: :limit <n>[/red]")
        elif raw.startswith(":"):
            console.print(f"[red]Unknown command: {raw}[/red]")
        else:
            # Check if it looks like a product search (no sellers named this)
            # Try product search first, fall back to seller name search
            pq = raw.strip()
            product_results = search_products(
                r, pq,
                state["user_lat"], state["user_lon"],
                state["radius_km"], state["limit"],
            )
            if product_results:
                render_product_results(product_results, pq,
                                       state["user_lat"], state["user_lon"],
                                       state["radius_km"])
            else:
                state["keyword"] = raw
                _run()


def _restore_redis_from_mongo(r: redis.Redis) -> int:
    """
    Push all sellers from MongoDB seller_geo into Redis hashes and rebuild
    the FT.SEARCH index.  Called automatically when the index is missing.
    Returns number of sellers restored.
    """
    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
        shops  = list(client[MONGO_DB].seller_geo.find({}, {"_id": 0}))
        client.close()
    except Exception as exc:
        console.print(f"[yellow]  Cannot reach MongoDB to restore sellers: {exc}[/yellow]")
        return 0

    if not shops:
        return 0

    console.print(f"[dim]  Restoring {len(shops)} sellers from MongoDB → Redis…[/dim]")
    pipe = r.pipeline(transaction=False)
    for s in shops:
        sid = s.get("seller_id") or str(uuid.uuid4())
        lat = float(s.get("lat", BASE_LAT))
        lon = float(s.get("lon", BASE_LON))
        pipe.hset(f"seller:{sid}", mapping={
            "seller_id":         sid,
            "name":              s.get("name", ""),
            "address":           s.get("address", "Bangalore"),
            "category":          s.get("category", "Food & Grocery"),
            "subscription_tier": s.get("subscription_tier", "free"),
            "rating":            str(s.get("rating", 3.5)),
            "review_count":      str(s.get("review_count", 0)),
            "popularity":        str(float(s.get("rating", 3.5)) * float(s.get("review_count", 0))),
            "available":         "true",
            "phone":             s.get("phone", ""),
            "lat":               str(lat),
            "lon":               str(lon),
            "location":          f"{lon},{lat}",
            "product":           "",
        })
    pipe.execute()
    return len(shops)


def _ensure_ft_index(r: redis.Redis) -> None:
    """
    Ensure idx:sellers FT.SEARCH index exists with data.
    If missing or empty, restores from MongoDB and recreates the index.
    """
    index_ok   = False
    seller_cnt = 0
    try:
        info       = dict(zip(*[iter(r.execute_command("FT.INFO", "idx:sellers"))]*2))
        seller_cnt = int(info.get("num_docs", 0))
        index_ok   = True
    except Exception:
        pass

    # Restore seller hashes from MongoDB if Redis lost them
    if seller_cnt == 0:
        restored = _restore_redis_from_mongo(r)
        if restored == 0:
            return
        # Drop stale index (if it exists but is empty)
        if index_ok:
            try:
                r.execute_command("FT.DROPINDEX", "idx:sellers")
            except Exception:
                pass
        index_ok = False

    if not index_ok:
        console.print("[dim]  Creating FT.SEARCH index…[/dim]")
        try:
            r.execute_command(
                "FT.CREATE", "idx:sellers",
                "ON", "HASH", "PREFIX", "1", "seller:",
                "SCHEMA",
                "name",              "TEXT",    "WEIGHT", "5",
                "address",           "TEXT",
                "category",          "TAG",
                "subscription_tier", "TAG",
                "available",         "TAG",
                "rating",            "NUMERIC", "SORTABLE",
                "review_count",      "NUMERIC", "SORTABLE",
                "popularity",        "NUMERIC", "SORTABLE",
                "location",          "GEO",
                "lat",               "NUMERIC",
                "lon",               "NUMERIC",
                "phone",             "TEXT",
                "product",           "TEXT",
            )
            console.print("[dim]  Index ready.[/dim]")
        except Exception as exc:
            console.print(f"[yellow]  Warning: could not create FT index: {exc}[/yellow]")


def search_mode(args) -> None:
    """One-shot or interactive search."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    try:
        r.ping()
    except Exception as exc:
        console.print(f"[red]Cannot connect to Redis: {exc}[/red]")
        raise SystemExit(1)
    _ensure_ft_index(r)

    # If lat/lon passed via CLI use them; otherwise interactive_search will prompt
    user_lat = args.lat   # may be None — interactive_search handles that
    user_lon = args.lon

    # If no filters provided → interactive shell (will prompt for location)
    no_flags = not any([args.query, args.category, args.tier, args.available,
                        args.radius != 10.0, args.lat, args.lon])
    if no_flags:
        interactive_search(r, start_lat=user_lat, start_lon=user_lon)
        return

    # One-shot: fall back to Bangalore centre if no coords given
    user_lat = user_lat if user_lat is not None else BASE_LAT
    user_lon = user_lon if user_lon is not None else BASE_LON

    # One-shot
    desc_parts = []
    if args.query:     desc_parts.append(f'name~"{args.query}"')
    if args.category:  desc_parts.append(f'category="{args.category}"')
    if args.tier:      desc_parts.append(f'tier={args.tier}')
    if args.available: desc_parts.append("available=true")
    desc_parts.append(f"radius≤{args.radius:.0f}km  sort={args.sort}")
    desc_parts.append(f"📍 {user_lat:.4f},{user_lon:.4f}")

    total, docs = run_search(
        r,
        keyword=args.query,
        category=args.category,
        radius_km=args.radius,
        sort_by=args.sort,
        available_only=args.available,
        tier=args.tier,
        limit=args.limit,
        user_lat=user_lat,
        user_lon=user_lon,
    )
    render_search_results(total, docs, "  ".join(desc_parts), args.limit)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Sync mode — index existing catalogue sellers into Redis geo
# ─────────────────────────────────────────────────────────────────────────────

def fetch_catalogue_sellers() -> list[dict]:
    """Read all unique sellers from MongoDB catalog_db.catalogs."""
    client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
    try:
        db = client[MONGO_DB]
        docs = list(db.catalogs.find(
            {},
            {"seller_id": 1, "name": 1, "type": 1, "_id": 0}
        ))
    finally:
        client.close()

    # One entry per seller_id (a seller may have multiple catalogs)
    seen = {}
    for doc in docs:
        sid = doc.get("seller_id", "")
        if sid and sid not in seen:
            seen[sid] = doc
    return list(seen.values())


def build_seller_for_sync(doc: dict, index: int) -> dict:
    """Turn a catalogue MongoDB doc into a seller dict with locality-matched GPS."""
    rng = random.Random(index + 42000)
    prefix    = rng.choice(SHOP_PREFIXES)
    shop_type = rng.choice(SHOP_TYPES)
    locality  = rng.choice(BANGALORE_LOCALITIES)
    category  = rng.choice(CATEGORIES)
    tier      = random.choices(SUBSCRIPTION_TIERS, weights=TIER_WEIGHTS, k=1)[0]

    # GPS matches the address locality — so searching "Nagawara" only shows Nagawara shops
    lat, lon  = locality_gps(locality, rng)

    # Use catalogue name if it exists and looks reasonable
    name = doc.get("name", "") or f"{prefix} {shop_type}"
    if len(name) < 3:
        name = f"{prefix} {shop_type}"

    return {
        "seller_id":         doc["seller_id"],
        "name":              name,
        "address":           f"{rng.randint(1, 200)}, {locality}, Bangalore",
        "latitude":          round(lat, 6),
        "longitude":         round(lon, 6),
        "category":          category,
        "subscription_tier": tier,
        "rating":            round(rng.uniform(3.0, 5.0), 2),
        "review_count":      rng.randint(0, 500),
    }


_SELLER_CATEGORY_MAP: dict[str, str] = {}   # populated once by seed_items_mode


def _category_for_seller(seller_id: str) -> str:
    """Return seller's real category (from seller_geo), fallback to deterministic random."""
    if _SELLER_CATEGORY_MAP:
        return _SELLER_CATEGORY_MAP.get(seller_id) or random.Random(seller_id).choice(CATEGORIES)
    rng = random.Random(seller_id)
    return rng.choice(CATEGORIES)


def seed_items_for_catalog(catalog: dict) -> tuple[int, str]:
    """
    Insert realistic items into catalog_db.items for a single catalog.
    Returns (items_inserted, error_message_or_empty).
    """
    seller_id  = catalog.get("seller_id", "")
    # MongoDB stores catalog _id as a UUID string directly in the _id field
    catalog_id = catalog.get("_id", "") or catalog.get("id", "")
    if not seller_id or not catalog_id:
        return 0, "missing seller_id or catalog_id"

    # Determine category for this seller (consistent random seed)
    category = _category_for_seller(seller_id)
    products = CATEGORY_PRODUCTS.get(category, [])
    if not products:
        return 0, f"no products for category {category}"

    rng = random.Random(seller_id + catalog_id)

    # Pick items per shop — enough variety that all products surface across sellers
    if category == "Food & Grocery":
        n_items = rng.randint(30, min(60, len(products)))
    elif category == "Electronics":
        n_items = rng.randint(15, min(30, len(products)))
    elif category == "Health & Wellness":
        n_items = rng.randint(20, min(40, len(products)))
    elif category == "Clothing":
        n_items = rng.randint(20, min(40, len(products)))
    else:
        n_items = rng.randint(10, min(20, len(products)))
    selected = rng.sample(products, n_items)

    now = datetime.now(timezone.utc).isoformat()
    docs = []
    for product_name in selected:
        # Use real MRP from PRODUCT_PRICES table; fall back to category range
        price_paise = _product_price(product_name, category, rng)

        tags = [category.lower().split()[0], product_name.lower().split()[0]]
        docs.append({
            "_id":         str(uuid.uuid4()),   # matches itemDoc._id in MongoDB
            "catalog_id":  catalog_id,
            "seller_id":   seller_id,
            "name":        product_name,
            "description": f"Quality {product_name} available at our store.",
            "price":       price_paise,
            "category":    category,
            "subcategory": "",
            "type":        "product",
            "status":      "active",
            "tags":        tags,
            "quantity":    rng.randint(5, 200),
            "image_url":   "",
            "image_urls":  [],
            "variants":    [],
            "average_rating": round(rng.uniform(3.5, 5.0), 2),
            "review_count":   rng.randint(0, 100),
            "created_at":  now,
            "updated_at":  now,
        })

    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=4000)
        db = client[MONGO_DB]
        # Skip if this catalog already has real items
        existing_names = [
            d["name"] for d in db.items.find(
                {"catalog_id": catalog_id}, {"name": 1, "_id": 0}
            ).limit(10)
        ]
        new_docs = [d for d in docs if d["name"] not in existing_names]
        if new_docs:
            db.items.insert_many(new_docs)
        client.close()
        return len(new_docs), ""
    except Exception as exc:
        return 0, str(exc)


def seed_items_mode(args) -> None:
    """Seed realistic product items into MongoDB for every existing catalog."""
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Padosme — Seed Catalogue Items[/bold cyan]\n"
        f"Reads catalogs from MongoDB [bold]{MONGO_DB}[/bold]\n"
        f"Inserts real product names from CATEGORY_PRODUCTS\n"
        f"so that [bold]:product mango[/bold] and [bold]:product iPhone[/bold] return results.",
        border_style="cyan",
    ))

    console.print("\n[dim]Fetching catalogs and seller categories from MongoDB…[/dim]")
    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
        db = client[MONGO_DB]
        # Build seller_id → actual category map from seller_geo (real OSM categories)
        global _SELLER_CATEGORY_MAP
        _SELLER_CATEGORY_MAP = {
            str(d["seller_id"]): d["category"]
            for d in db.seller_geo.find({}, {"seller_id": 1, "category": 1, "_id": 0})
            if d.get("category")
        }
        console.print(f"[dim]  Loaded categories for {len(_SELLER_CATEGORY_MAP)} sellers.[/dim]")
        # _id is the catalog UUID string; include it explicitly
        raw = list(db.catalogs.find({}, {"seller_id": 1, "name": 1, "_id": 1}))
        client.close()
    except Exception as exc:
        console.print(f"[red]MongoDB error: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]Found {len(raw)} catalogs.[/green]\n")
    if not raw:
        console.print("[yellow]Nothing to seed.[/yellow]")
        return

    items_total = 0
    errors      = 0
    t0 = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Seeding items for {len(raw)} catalogs…", total=len(raw))

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(seed_items_for_catalog, doc): doc for doc in raw}
            for future in as_completed(futures):
                try:
                    n, err = future.result()
                except Exception as exc:
                    n, err = 0, str(exc)
                items_total += n
                if err:
                    errors += 1
                progress.advance(task)

    elapsed = time.perf_counter() - t0

    tbl = Table(title="Item Seeding Summary", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Metric",  style="bold", min_width=35)
    tbl.add_column("Value",   justify="right", min_width=12)
    tbl.add_row("Catalogs processed", str(len(raw)))
    tbl.add_row("Items inserted", f"[green]{items_total}[/green]")
    if errors:
        tbl.add_row("Errors", f"[red]{errors}[/red]")
    tbl.add_row("Elapsed", f"{elapsed:.1f}s")
    console.print(tbl)
    console.print()

    if items_total > 0:
        console.print(Panel(
            f"[green bold]{items_total} items seeded across {len(raw)} catalogs.[/green bold]\n"
            "[dim]Now try:  python3 seed_bangalore_sellers.py --search\n"
            "Then:  :product mango   or  :product iPhone[/dim]",
            border_style="green",
        ))
    console.print()


def save_seller_geo(sellers: list[dict]) -> int:
    """
    Persist seller GPS + info to MongoDB catalog_db.seller_geo collection.
    Used as fallback when Redis TTL expires so :product search always works.
    """
    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
        db = client[MONGO_DB]
        for s in sellers:
            db.seller_geo.update_one(
                {"seller_id": s["seller_id"]},
                {"$set": {
                    "seller_id":         s["seller_id"],
                    "name":              s["name"],
                    "address":           s["address"],
                    "lat":               s["latitude"],
                    "lon":               s["longitude"],
                    "category":          s["category"],
                    "subscription_tier": s["subscription_tier"],
                    "rating":            s["rating"],
                    "review_count":      s["review_count"],
                    "phone":             s.get("phone", ""),
                }},
                upsert=True,
            )
        client.close()
        return len(sellers)
    except Exception as exc:
        log.warning("Could not save seller_geo to MongoDB: %s", exc)
        return 0


def save_to_discovery(sellers: list[dict]) -> int:
    """
    Persist sellers to MongoDB discovery.sellers with GeoJSON Point format.
    Enables geo queries from the discovery service.
    """
    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
        db = client[MONGO_DISCOVERY_DB]
        for s in sellers:
            db.sellers.update_one(
                {"seller_id": s["seller_id"]},
                {"$set": {
                    "_id":               s["seller_id"],
                    "seller_id":         s["seller_id"],
                    "name":              s["name"],
                    "address":           s["address"],
                    "location": {
                        "type":        "Point",
                        "coordinates": [s["longitude"], s["latitude"]],
                    },
                    "category":          s["category"],
                    "subscription_tier": s["subscription_tier"],
                    "rating":            s["rating"],
                    "review_count":      s["review_count"],
                    "available":         True,
                    "status":            "active",
                    "entity_type":       "shop",
                    "city":              "Bangalore",
                }},
                upsert=True,
            )
        client.close()
        return len(sellers)
    except Exception as exc:
        log.warning("Could not save to discovery.sellers (MongoDB): %s", exc)
        return 0


def save_to_indexed_sellers(sellers: list[dict]) -> int:
    """
    Persist sellers to PostgreSQL padosme_indexing.indexed_sellers.
    Provides durable storage that survives Redis TTL expiry.
    """
    try:
        conn = psycopg2.connect(PG_DSN)
        cur  = conn.cursor()
        for s in sellers:
            cur.execute(
                """
                INSERT INTO indexed_sellers
                    (seller_id, name, latitude, longitude, h3_cell, available, status, entity_type, last_indexed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (seller_id) DO UPDATE SET
                    name            = EXCLUDED.name,
                    latitude        = EXCLUDED.latitude,
                    longitude       = EXCLUDED.longitude,
                    available       = EXCLUDED.available,
                    status          = EXCLUDED.status,
                    last_indexed_at = now()
                """,
                (
                    s["seller_id"],
                    s["name"],
                    s["latitude"],
                    s["longitude"],
                    "",        # h3_cell computed by indexing service
                    True,
                    "active",
                    "shop",
                ),
            )
        conn.commit()
        cur.close()
        conn.close()
        return len(sellers)
    except Exception as exc:
        log.warning("Could not save to indexed_sellers (PostgreSQL): %s", exc)
        return 0


def sync_mode(args) -> None:
    """Fetch existing catalogue sellers and geo-index them in Redis."""
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Padosme — Sync Catalogue → Geo Index[/bold cyan]\n"
        f"Reads existing sellers from MongoDB [bold]{MONGO_DB}[/bold]\n"
        f"Assigns Bangalore GPS and publishes seller.verified events\n"
        f"so the indexing service adds them to the Redis geo index.",
        border_style="cyan",
    ))

    # Fetch from MongoDB
    console.print("\n[dim]Connecting to MongoDB…[/dim]")
    try:
        sellers_raw = fetch_catalogue_sellers()
    except Exception as exc:
        console.print(f"[red]MongoDB error: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]Found {len(sellers_raw)} unique sellers in catalogue.[/green]\n")
    if not sellers_raw:
        console.print("[yellow]Nothing to sync.[/yellow]")
        return

    # Build seller dicts — prefer real GPS from seller_geo over random assignment
    client_geo = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
    geo_lookup = {
        d["seller_id"]: d
        for d in client_geo[MONGO_DB].seller_geo.find(
            {}, {"seller_id":1,"name":1,"address":1,"lat":1,"lon":1,
                 "category":1,"subscription_tier":1,"rating":1,"review_count":1,"phone":1,"_id":0}
        )
    }
    client_geo.close()

    sellers = []
    for i, doc in enumerate(sellers_raw):
        sid = doc.get("seller_id", "")
        if sid in geo_lookup:
            # Use the real pre-seeded data — don't overwrite with random GPS
            g = geo_lookup[sid]
            sellers.append({
                "seller_id":         sid,
                "name":              g.get("name", doc.get("name", "")),
                "address":           g.get("address", "Bangalore"),
                "latitude":          float(g.get("lat", BASE_LAT)),
                "longitude":         float(g.get("lon", BASE_LON)),
                "category":          g.get("category", "Food & Grocery"),
                "subscription_tier": g.get("subscription_tier", "free"),
                "rating":            float(g.get("rating", 3.5)),
                "review_count":      int(g.get("review_count", 0)),
                "phone":             g.get("phone", ""),
            })
        else:
            sellers.append(build_seller_for_sync(doc, i))

    # Persist to all three stores permanently
    console.print("[dim]Saving seller locations to MongoDB catalog_db.seller_geo…[/dim]")
    save_seller_geo(sellers)
    console.print("[dim]Saving sellers to MongoDB discovery.sellers…[/dim]")
    save_to_discovery(sellers)
    console.print("[dim]Saving sellers to PostgreSQL indexed_sellers…[/dim]")
    save_to_indexed_sellers(sellers)

    events_ok = events_fail = 0
    t0 = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Publishing seller.verified for {len(sellers)} sellers…", total=len(sellers))

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(publish_seller_verified, s): s for s in sellers}
            for future in as_completed(futures):
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                if ok:
                    events_ok += 1
                else:
                    events_fail += 1
                progress.advance(task)

    elapsed = time.perf_counter() - t0

    # Verify
    verify = verify_via_redis(radius_km=RADIUS_KM, wait_secs=5.0)

    tbl = Table(title="Sync Summary", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Metric",  style="bold", min_width=35)
    tbl.add_column("Value",   justify="right", min_width=12)
    tbl.add_row("Sellers from catalogue",       str(len(sellers)))
    tbl.add_row("seller.verified published",    f"[green]{events_ok}[/green]" if not events_fail else f"[yellow]{events_ok}[/yellow]")
    if events_fail:
        tbl.add_row("  └─ failed", f"[red]{events_fail}[/red]")
    tbl.add_row("", "")
    tbl.add_row("Discoverable within 10km",     f"[green]{verify['within_radius']}[/green]" if verify['within_radius'] > 0 else str(verify['within_radius']))
    tbl.add_row("Total in Redis index",         str(verify['total_indexed']))
    tbl.add_row("Elapsed",                      f"{elapsed:.1f}s")
    console.print(tbl)
    console.print()

    if events_ok > 0:
        console.print(Panel(
            f"[green bold]{events_ok} sellers are now geo-indexed in Bangalore.[/green bold]\n"
            "[dim]Run:  python3 seed_bangalore_sellers.py --search[/dim]",
            border_style="green",
        ))
    console.print()


def check_services() -> bool:
    """Verify that catalogue and indexing services are reachable."""
    ok = True
    for name, url in [("Catalogue", CATALOGUE_URL), ("Indexing", INDEXING_URL)]:
        try:
            r = requests.get(f"{url}/health", timeout=5)
            status = "healthy" if r.status_code == 200 else f"HTTP {r.status_code}"
        except Exception as exc:
            status = f"unreachable ({exc})"
            ok = False
        console.print(f"  {name:12} {url}  →  [{'green' if 'healthy' in status else 'red'}]{status}[/]")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed or search Bangalore sellers in the Padosme pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Sync existing sellers into geo index:
  python3 seed_bangalore_sellers.py --sync

Seed real product names (run after --sync):
  python3 seed_bangalore_sellers.py --seed-items

Search mode:
  python3 seed_bangalore_sellers.py --search               # interactive shell
  python3 seed_bangalore_sellers.py --search --query sharma
  python3 seed_bangalore_sellers.py --search --category Electronics --radius 5
  python3 seed_bangalore_sellers.py --search --sort distance --tier gold

Seed new sellers from scratch:
  python3 seed_bangalore_sellers.py
  python3 seed_bangalore_sellers.py --count 500 --workers 15
""",
    )
    # ── Search flags ──────────────────────────────────────────────────────────
    parser.add_argument("--sync",            action="store_true",                 help="Sync existing catalogue sellers → Redis geo index")
    parser.add_argument("--seed-items",     action="store_true",                 help="Seed real product names into MongoDB items (run after --sync)")
    parser.add_argument("--search",          action="store_true",                 help="Search mode (query the geo index)")
    parser.add_argument("--query",    "-q",  default="",                          help="[search] Keyword to match in seller name")
    parser.add_argument("--category", "-c",  default="",                          help="[search] Filter by category")
    parser.add_argument("--sort",     "-s",  default="rating",
                        choices=["rating", "distance", "popularity"],             help="[search] Sort order (default: rating)")
    parser.add_argument("--available", "-a", action="store_true",                 help="[search] Only open/available sellers")
    parser.add_argument("--tier",     "-t",  default="",
                        choices=["", "free", "bronze", "silver", "gold", "platinum"],
                        help="[search] Filter by subscription tier")
    parser.add_argument("--limit",    "-l",  type=int, default=20,               help="[search] Max results to display (default: 20)")
    parser.add_argument("--lat",             type=float, default=None,           help="[search] Your latitude  (default: Bangalore centre)")
    parser.add_argument("--lon",             type=float, default=None,           help="[search] Your longitude (default: Bangalore centre)")

    # ── Seed flags ────────────────────────────────────────────────────────────
    parser.add_argument("--count",           type=int,   default=DEFAULT_COUNT,   help="[seed] Number of sellers to create")
    parser.add_argument("--workers",         type=int,   default=DEFAULT_WORKERS, help="[seed] Concurrent workers")
    parser.add_argument("--radius",          type=float, default=RADIUS_KM,       help="Radius in km (seed + search, default: 10)")
    parser.add_argument("--skip-catalogue",  action="store_true",                 help="[seed] Skip catalogue API calls")
    parser.add_argument("--wait",            type=float, default=3.0,             help="[seed] Seconds to wait before verification")
    parser.add_argument("--debug",           action="store_true",                 help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("seeder").setLevel(logging.DEBUG)

    # ── Route to seed-items mode ──────────────────────────────────────────────
    if args.seed_items:
        seed_items_mode(args)
        return

    # ── Route to sync mode ────────────────────────────────────────────────────
    if args.sync:
        sync_mode(args)
        return

    # ── Route to search mode ──────────────────────────────────────────────────
    if args.search:
        search_mode(args)
        return

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Padosme — Bangalore Seller Seeder[/bold cyan]\n"
        f"Base location : {BASE_LAT}°N, {BASE_LON}°E (Bangalore)\n"
        f"Radius        : {args.radius} km\n"
        f"Sellers       : {args.count}\n"
        f"Workers       : {args.workers}",
        border_style="cyan",
    ))

    # Service health check
    console.print("\n[bold]Service health checks[/bold]")
    if not check_services():
        console.print("[red]One or more services are unreachable. Aborting.[/red]")
        raise SystemExit(1)
    console.print()

    # Generate seller data upfront — hardcoded real shops first, then live OSM fetch
    real_sellers = generate_real_sellers()
    existing_names = {s["name"].lower() for s in real_sellers}

    console.print("[dim]Fetching real shops from OpenStreetMap…[/dim]")
    osm_sellers = fetch_osm_sellers_for_seed(args.radius, args.count)
    # Deduplicate against the hardcoded list
    osm_sellers = [s for s in osm_sellers if s["name"].lower() not in existing_names]

    sellers = real_sellers + osm_sellers

    console.print(f"[dim]  {len(real_sellers)} hardcoded shops + {len(osm_sellers)} OSM shops = {len(sellers)} total[/dim]")

    # Verify all generated points are within radius (sanity check)
    max_dist = max(haversine_km(BASE_LAT, BASE_LON, s["latitude"], s["longitude"]) for s in sellers)
    console.print(f"[dim]Generated {len(sellers)} sellers — max distance from centre: {max_dist:.2f} km[/dim]")

    # Persist to PostgreSQL and MongoDB before publishing events
    console.print("[dim]Saving sellers to MongoDB catalog_db.seller_geo…[/dim]")
    save_seller_geo(sellers)
    console.print("[dim]Saving sellers to MongoDB discovery.sellers…[/dim]")
    save_to_discovery(sellers)
    console.print("[dim]Saving sellers to PostgreSQL indexed_sellers…[/dim]")
    save_to_indexed_sellers(sellers)

    events_ok  = 0
    events_fail = 0
    catalogue_ok   = 0
    catalogue_fail = 0
    t0 = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(
            f"Seeding {len(sellers)} sellers ({len(real_sellers)} hardcoded + {len(osm_sellers)} OSM)…",
            total=len(sellers),
        )

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(seed_one, s, args.skip_catalogue): s for s in sellers}
            for future in as_completed(futures):
                try:
                    res = future.result()
                except Exception as exc:
                    log.warning("Worker exception: %s", exc)
                    events_fail += 1
                else:
                    if res["event_published"]:
                        events_ok += 1
                    else:
                        events_fail += 1
                    if not args.skip_catalogue:
                        if res["catalogue_ok"]:
                            catalogue_ok += 1
                        else:
                            catalogue_fail += 1
                progress.advance(task)

    elapsed = time.perf_counter() - t0
    console.print()

    # ── Verification ──────────────────────────────────────────────────────────
    verify = verify_via_redis(radius_km=args.radius, wait_secs=args.wait)

    # ── Summary table ─────────────────────────────────────────────────────────
    tbl = Table(title="Seeding Summary", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Metric",  style="bold", min_width=35)
    tbl.add_column("Value",   justify="right", min_width=12)

    total_targeted = len(sellers)
    tbl.add_row("Sellers targeted",             str(total_targeted))
    tbl.add_row("  └─ hardcoded real shops",    str(len(real_sellers)))
    tbl.add_row("  └─ OSM live shops",          str(len(osm_sellers)))
    tbl.add_row(
        "seller.verified events published",
        f"[green]{events_ok}[/green]" if events_ok == total_targeted
        else f"[yellow]{events_ok}[/yellow]",
    )
    if events_fail:
        tbl.add_row("  └─ failed to publish", f"[red]{events_fail}[/red]")

    if not args.skip_catalogue:
        tbl.add_row(
            "Catalogue entries created",
            f"[green]{catalogue_ok}[/green]" if not catalogue_fail
            else f"[yellow]{catalogue_ok}[/yellow]",
        )
        if catalogue_fail:
            tbl.add_row("  └─ catalogue failures", f"[red]{catalogue_fail}[/red]")

    tbl.add_row("", "")   # separator

    tbl.add_row("Total indexed in Redis (all)",
                str(verify["total_indexed"]) if verify["total_indexed"] >= 0 else "[dim]n/a[/dim]")
    tbl.add_row(
        f"Discoverable within {args.radius:.0f} km of Bangalore",
        (f"[green]{verify['within_radius']}[/green]"
         if verify["within_radius"] >= events_ok * 0.9
         else f"[yellow]{verify['within_radius']}[/yellow]")
        if verify["within_radius"] >= 0 else "[dim]n/a[/dim]",
    )
    tbl.add_row("Elapsed", f"{elapsed:.1f}s")
    tbl.add_row("Throughput", f"{events_ok / elapsed:.1f} sellers/s")

    console.print(tbl)
    console.print()

    # ── Final verdict ──────────────────────────────────────────────────────────
    if events_ok == total_targeted and verify["within_radius"] >= events_ok * 0.9:
        console.print(Panel(
            f"[green bold]All {events_ok} sellers seeded and discoverable within {args.radius:.0f} km of Bangalore.[/green bold]\n"
            f"[dim]Discovery service will return these sellers via FT.SEARCH geo filter.[/dim]",
            border_style="green",
        ))
    elif events_ok > 0:
        console.print(Panel(
            f"[yellow]{events_ok}/{total_targeted} sellers published.\n"
            f"Indexing lag: if within_radius count is low, wait a few more seconds and re-check.\n"
            f"  redis-cli FT.SEARCH idx:sellers \"@location:[{BASE_LON} {BASE_LAT} {args.radius:.0f} km]\" LIMIT 0 0[/yellow]",
            border_style="yellow",
        ))
    else:
        console.print(Panel("[red bold]No sellers were published. Check RabbitMQ connectivity.[/red bold]",
                            border_style="red"))

    console.print()
    console.print("[dim]Verify manually:[/dim]")
    console.print(f'[dim]  redis-cli FT.SEARCH idx:sellers "@location:[{BASE_LON} {BASE_LAT} {args.radius:.0f} km]" LIMIT 0 5[/dim]')
    console.print(f'[dim]  curl {INDEXING_URL}/index/status[/dim]')
    console.print()


if __name__ == "__main__":
    main()
