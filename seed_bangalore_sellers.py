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
REDIS_PASSWORD  = "kaaslabs123"
PG_DSN          = "host=localhost user=deploy password=kaaslabs123 dbname=padosme_indexing"

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
    # ── RT Nagar / 80 Feet Road / Ganga Nagar ────────────────────────────────
    ("Empire Restaurant",           "161, 80 Feet Road, Ganganagar, Matadahalli, RT Nagar, Bangalore - 560032",    13.016794, 77.591662, "Food & Grocery",    "gold",   4.0, 2413, "+918040414141"),
    ("Kudla",                       "166, C/O Coral Tree, 80 Feet Road, MLA Layout, RT Nagar, Bangalore - 560032", 13.017801, 77.591723, "Food & Grocery",    "silver", 4.9,   42, "+918043346600"),
    ("J B Bakery",                  "8, Near Post Office, 80 Feet Road, Ganga Nagar, RT Nagar, Bangalore",         13.020820, 77.592447, "Food & Grocery",    "bronze", 4.5,  810, "+918023439839"),
    ("Nandhana Palace",             "367, 1st Floor, Judges Colony, 80 Feet Road, RT Nagar, Bangalore - 560032",   13.024033, 77.592725, "Food & Grocery",    "silver", 4.1,   70, "+919845207677"),
    ("Al-Bek",                      "1, 80 Feet Road, Near Florence School, RT Nagar, Bangalore - 560032",         13.022515, 77.592625, "Food & Grocery",    "silver", 3.9,  400, "+919900319831"),
    ("Rahhams",                     "4, 80 Feet Road, Near Shanthi Sagar, RT Nagar, Bangalore - 560032",           13.023621, 77.592699, "Food & Grocery",    "silver", 4.1,  930, "+918023548700"),
    ("Huliyappa's Donne Biryani",   "33, 10th Cross, Ganga Nagar, RT Nagar, Bangalore - 560032",                   13.026943, 77.590814, "Food & Grocery",    "bronze", 4.0, 1809, "+919019932654"),
    ("The Momo Shop",               "SM Arcade, 98-98-4, 1st Main, KHM Block, Ganga Nagar, RT Nagar, Bangalore",  13.023040, 77.592141, "Food & Grocery",    "bronze", 4.3,   50, "+919845141954"),
    ("Dawat E Rasoi",               "5/3, ALS Complex, 1st Main, 2nd Cross, Opp IOC Pump, RT Nagar, Bangalore",   13.018548, 77.592179, "Food & Grocery",    "bronze", 4.3,  100, ""),
    ("Food Hub",                    "Kaveri Nagar, Manorayana Palya, RT Nagar, Bangalore",                         13.024187, 77.596961, "Food & Grocery",    "bronze", 4.2,  458, "+919900197851"),
    ("Maruthi Complex",             "19, Ravindra Tagore Nagar Main Rd, P&T Colony, Ganga Nagar, Bangalore",       13.021484, 77.592322, "Food & Grocery",    "free",   4.9,    2, "+919217002598"),
    ("Shree Raj Lassi Bar",         "RT Nagar Main Road, RT Nagar, Bangalore - 560032",                            13.021800, 77.596800, "Food & Grocery",    "silver", 4.6,  312, ""),
    ("Savera Restaurant",           "RT Nagar 1st Cross, RT Nagar, Bangalore - 560032",                            13.022500, 77.597500, "Food & Grocery",    "silver", 4.3,  198, ""),
    ("New Saraswati Medicals",      "34, RT Nagar Main Road, RT Nagar, Bangalore - 560032",                        13.022000, 77.597400, "Health & Wellness", "bronze", 4.3,  156, ""),
    ("Pooja Stationery & Books",    "19, RT Nagar 2nd Cross, RT Nagar, Bangalore - 560032",                        13.021600, 77.596700, "Stationery",        "free",   4.1,   72, ""),
    ("Kumar Hardware & Plumbing",   "RT Nagar Industrial Area, RT Nagar, Bangalore - 560032",                      13.020000, 77.595000, "Hardware",          "free",   3.9,   55, ""),
    ("Simpli Namdhari's",           "63, P&T Colony, HMT Layout, 10th Main, Opp Chaitanya Hospital, RT Nagar, Bangalore", 13.026000, 77.591500, "Food & Grocery", "silver", 4.3, 320, ""),

    # ── Rahamath Nagar — 1st Main Road, RT Nagar Post, Pin 560032 ─────────────
    ("Sichi Hotel",                 "No.9, 1st Main Road, Rahamath Nagar, RT Nagar, Bangalore - 560032",           13.017642, 77.591957, "Food & Grocery",    "bronze", 4.9,   20, "+918892062687"),
    ("New Kadai Family Restaurant", "7, 1st Main Road, Rahamath Nagar, RT Nagar, Bangalore - 560032",              13.018219, 77.591995, "Food & Grocery",    "bronze", 4.1,   81, "+919900486367"),
    ("Pik & Wrap",                  "8/14, 1st Main, Near IBP Petrol Bunk, Rahamath Nagar, RT Nagar, Bangalore",   13.019700, 77.592000, "Food & Grocery",    "bronze", 4.3,  150, ""),
    ("Al Noor Bakery",              "Rahamath Nagar, RT Nagar, Bangalore - 560032",                                 13.017900, 77.591800, "Food & Grocery",    "bronze", 4.3,  215, ""),
    ("Bismillah Provisions",        "Rahamath Nagar Cross, RT Nagar, Bangalore - 560032",                          13.018500, 77.592100, "Food & Grocery",    "free",   4.0,   78, ""),
    ("Rahmath Chicken Centre",      "Rahamath Nagar, RT Nagar, Bangalore - 560032",                                 13.017500, 77.591700, "Food & Grocery",    "bronze", 4.4,  167, ""),
    ("Star Medicals Rahamath Nagar","1st Main Road, Rahamath Nagar, RT Nagar, Bangalore - 560032",                 13.018000, 77.591850, "Health & Wellness", "free",   3.9,   44, ""),

    # ── Kammanahalli — Pin 560084 ─────────────────────────────────────────────
    # Verified: Al Amanah is at 20, 5th Avenue, 5th Main Road, Kammanahalli (NOT Rahamath Nagar)
    ("Al Amanah Cafe",              "20, 5th Avenue, 5th Main Road, Kammanahalli, Bangalore - 560084",   13.015544, 77.639521, "Food & Grocery",    "silver", 4.3, 1910, "+919900029506"),
    ("Kamadhenu Milk & Dairy",      "5, Kammanahalli Main Road, Kammanahalli, Bangalore - 560084",       13.014500, 77.648200, "Food & Grocery",    "bronze", 4.2,  188, ""),
    ("Hotel Anugraha",              "33, Kammanahalli Main Road, Kammanahalli, Bangalore - 560084",      13.014800, 77.647800, "Food & Grocery",    "silver", 4.4,  290, ""),
    ("Bhavani Bakery",              "18, Kammanahalli Circle, Kammanahalli, Bangalore - 560084",         13.014000, 77.647500, "Food & Grocery",    "bronze", 4.3,  225, ""),
    ("Madurai Idli Shop",           "9, Kammanahalli 1st Cross, Kammanahalli, Bangalore - 560084",       13.014200, 77.648000, "Food & Grocery",    "free",   4.1,  130, ""),
    ("Sri Lakshmi Departmental",    "44, Kammanahalli Main Road, Kammanahalli, Bangalore - 560084",      13.015000, 77.648500, "Food & Grocery",    "free",   3.8,   62, ""),
    ("Nagaraj Electronics",         "27, Kammanahalli Main Road, Kammanahalli, Bangalore - 560084",      13.015500, 77.649000, "Electronics",       "bronze", 4.0,   85, ""),
    ("Arogya Medicals",             "11, Kammanahalli 2nd Cross, Kammanahalli, Bangalore - 560084",      13.013800, 77.647200, "Health & Wellness", "bronze", 4.2,  114, ""),
    ("The Book Corner",             "6, Kammanahalli, Bangalore - 560084",                               13.014300, 77.647700, "Books",             "free",   4.3,   97, ""),

    # ── Hebbal — Pin 560024 ───────────────────────────────────────────────────
    ("Hebbal Fresh Vegetables",     "Near Hebbal Flyover, Hebbal, Bangalore - 560024",            13.035800, 77.596800, "Food & Grocery",    "free",   4.0, 112, ""),
    ("Hotel Dakshin Hebbal",        "14, Hebbal Main Road, Hebbal, Bangalore - 560024",           13.035000, 77.596500, "Food & Grocery",    "silver", 4.5, 380, ""),
    ("Srinivasa Kirana & General",  "8, Hebbal 1st Cross, Hebbal, Bangalore - 560024",            13.035500, 77.597200, "Food & Grocery",    "free",   3.9,  58, ""),
    ("Hebbal Auto Spare Parts",     "Opp. Hebbal Lake, Hebbal, Bangalore - 560024",               13.036200, 77.596000, "Automotive",        "bronze", 4.1,  93, ""),
    ("Om Sai Medical Store",        "22, Hebbal Main Road, Hebbal, Bangalore - 560024",           13.034800, 77.597000, "Health & Wellness", "bronze", 4.3, 148, ""),
    ("Balaji Mobiles Hebbal",       "5, Hebbal Main Road, Hebbal, Bangalore - 560024",            13.035600, 77.597500, "Electronics",       "bronze", 3.9,  74, ""),

    # ── Sahakar Nagar — Pin 560092 ────────────────────────────────────────────
    ("Anand Sweets & Snacks",       "3, Sahakar Nagar Main Road, Sahakar Nagar, Bangalore - 560092", 13.039000, 77.587800, "Food & Grocery",    "silver", 4.6, 445, ""),
    ("Sahakar Nagar Bakery",        "21, Sahakar Nagar, Bangalore - 560092",                      13.038500, 77.588200, "Food & Grocery",    "bronze", 4.2, 192, ""),
    ("Sri Ram General Store",       "9, Sahakar Nagar 3rd Block, Bangalore - 560092",             13.039300, 77.587500, "Food & Grocery",    "free",   4.0,  76, ""),
    ("Wellness Pharmacy",           "16, Sahakar Nagar Main Road, Bangalore - 560092",            13.038800, 77.588000, "Health & Wellness", "bronze", 4.4, 203, ""),
    ("Kids Zone Toys",              "7, Sahakar Nagar, Bangalore - 560092",                       13.038200, 77.588500, "Toys & Baby",        "free",   4.1,  88, ""),

    # ── Nagawara — Pin 560045 ─────────────────────────────────────────────────
    ("Green Valley Restaurant",     "12, Nagawara Main Road, Nagawara, Bangalore - 560045",       13.046800, 77.622000, "Food & Grocery",    "silver", 4.3, 265, ""),
    ("Nagawara Fruit & Veg Market", "Near Nagawara Lake, Nagawara, Bangalore - 560045",           13.047200, 77.621500, "Food & Grocery",    "free",   4.0, 110, ""),
    ("Galaxy Electronics",          "35, Nagawara Main Road, Nagawara, Bangalore - 560045",       13.046500, 77.622500, "Electronics",       "bronze", 4.1, 135, ""),
    ("Zam Zam Restaurant",          "8, Nagawara Main Road, Nagawara, Bangalore - 560045",        13.047000, 77.621800, "Food & Grocery",    "bronze", 4.5, 320, ""),

    # ── Sanjay Nagar — Pin 560094 ─────────────────────────────────────────────
    ("Sanjay Nagar Supermarket",    "Main Road, Sanjay Nagar, Bangalore - 560094",                13.030000, 77.574000, "Food & Grocery",    "bronze", 4.1, 155, ""),
    ("Fresh & Go Provisions",       "4th Block, Sanjay Nagar, Bangalore - 560094",                13.029500, 77.574500, "Food & Grocery",    "free",   3.9,  67, ""),
    ("Sharma Medicals",             "2nd Block, Sanjay Nagar, Bangalore - 560094",                13.030200, 77.573800, "Health & Wellness", "free",   4.0,  82, ""),

    # ── Ganganagar — Pin 560032 ───────────────────────────────────────────────
    ("Ganganagar Darshini",         "1st Main Road, Ganganagar, RT Nagar, Bangalore - 560032",    13.019800, 77.578400, "Food & Grocery",    "free",   4.2, 142, ""),
    ("Sri Durga Provisions",        "3rd Cross, Ganganagar, RT Nagar, Bangalore - 560032",        13.019400, 77.578000, "Food & Grocery",    "free",   3.8,  55, ""),
    ("Ganganagar Sports Corner",    "Main Road, Ganganagar, RT Nagar, Bangalore - 560032",        13.020000, 77.578800, "Sports",            "free",   4.0,  63, ""),

    # ── Rajanukunte — Pin 562157, NH 44 corridor ───────────────────────────────
    ("Rajanukunte Milk Dairy",             "Main Road, Rajanukunte, Bangalore - 562157",            13.126800, 77.596200, "Food & Grocery",    "free",   4.1,  98, ""),
    ("Highway Dhaba Rajanukunte",          "NH 44, Rajanukunte, Bangalore - 562157",                13.126000, 77.596800, "Food & Grocery",    "bronze", 4.3, 172, ""),
    ("Rajanukunte General Stores",         "Bus Stand Road, Rajanukunte, Bangalore - 562157",       13.127000, 77.595800, "Food & Grocery",    "free",   3.8,  45, ""),
    ("Sri Venkateswara Kirana",           "1st Cross, Rajanukunte, Bangalore - 562157",            13.127200, 77.596000, "Food & Grocery",    "free",   4.0,  62, ""),
    ("New Srinivasa Provisions",          "NH 44 Service Road, Rajanukunte, Bangalore - 562157",   13.125800, 77.597000, "Food & Grocery",    "free",   3.9,  55, ""),
    ("Rajanukunte Fruit & Veg Shop",       "Main Road, Rajanukunte, Bangalore - 562157",            13.126500, 77.595500, "Food & Grocery",    "free",   4.2, 118, ""),
    ("Rajanukunte Chicken & Mutton",       "Market Road, Rajanukunte, Bangalore - 562157",          13.127400, 77.596300, "Food & Grocery",    "free",   4.1,  88, ""),
    ("Hotel Surya Rajanukunte",            "NH 44, Rajanukunte, Bangalore - 562157",                13.125600, 77.597200, "Food & Grocery",    "bronze", 4.2, 210, ""),
    ("Shree Sagar Darshini",               "Bus Stand, Rajanukunte, Bangalore",               13.1268, 77.5958, "Food & Grocery",    "free",     4.3, 195),
    ("Bismillah Hotel",                    "NH 44, Rajanukunte, Bangalore",                   13.1262, 77.5966, "Food & Grocery",    "free",     4.4, 240),
    ("Rajanukunte Bakery & Sweets",         "1st Main, Rajanukunte, Bangalore",                13.1271, 77.5961, "Food & Grocery",    "free",     4.0,  77),
    ("Amruth Cool Bar",                    "Main Road, Rajanukunte, Bangalore",               13.1266, 77.5968, "Food & Grocery",    "free",     4.1,  92),
    ("Rajanukunte Medical Hall",            "Near Bus Stand, Rajanukunte, Bangalore",          13.1269, 77.5957, "Health & Wellness", "free",     4.2, 135),
    ("Life Care Pharmacy",                 "NH 44, Rajanukunte, Bangalore",                   13.1257, 77.5971, "Health & Wellness", "free",     4.0,  80),
    ("Dhanvantari Medical Store",          "Main Road, Rajanukunte, Bangalore",               13.1264, 77.5963, "Health & Wellness", "bronze",   4.3, 148),
    ("Rajanukunte Hardware & Paints",       "Main Road, Rajanukunte, Bangalore",               13.1275, 77.5964, "Hardware",          "free",     3.9,  52),
    ("Sri Sai Hardware Stores",            "1st Cross, Rajanukunte, Bangalore",               13.1270, 77.5956, "Hardware",          "free",     3.8,  38),
    ("Balaji Auto Garage",                 "NH 44, Rajanukunte, Bangalore",                   13.1252, 77.5975, "Automotive",        "free",     4.1,  73),
    ("National Tyres & Services",          "NH 44 Service Road, Rajanukunte, Bangalore",      13.1254, 77.5973, "Automotive",        "bronze",   4.2, 105),
    ("Rajanukunte Petrol Bunk Services",    "NH 44, Rajanukunte, Bangalore",                   13.1248, 77.5978, "Automotive",        "free",     3.9,  44),
    ("Sri Ganapathi Mobile Shop",          "Main Road, Rajanukunte, Bangalore",               13.1267, 77.5960, "Electronics",       "free",     3.9,  49),
    ("Rajanukunte Mobile Accessories",      "Bus Stand Road, Rajanukunte, Bangalore",          13.1271, 77.5955, "Electronics",       "free",     3.8,  35),
    ("Rajanukunte Stationery & Books",      "1st Cross, Rajanukunte, Bangalore",               13.1264, 77.5966, "Stationery",        "free",     4.0,  58),
    ("Little Stars Toys & Baby",           "Main Road, Rajanukunte, Bangalore",               13.1273, 77.5958, "Toys & Baby",       "free",     4.1,  67),
    # Nearby — Yelahanka New Town (2 km south)
    ("Yelahanka New Town Supermart",       "Main Road, Yelahanka New Town, Bangalore",       13.1082, 77.5944, "Food & Grocery",    "bronze",   4.3, 215),
    ("Hotel Udupi Garden",                 "1st Main, Yelahanka New Town, Bangalore",        13.1076, 77.5950, "Food & Grocery",    "silver",   4.4, 310),
    ("Sri Devi Medical Stores",            "Main Road, Yelahanka New Town, Bangalore",       13.1080, 77.5940, "Health & Wellness", "bronze",   4.1, 122),
    # Nearby — Singapura (1.5 km south-east)
    ("Singapura General Store",            "Main Road, Singapura, Bangalore",                13.0620, 77.5950, "Food & Grocery",    "free",     3.9,  48),
    ("Sri Rama Kirana",                    "1st Cross, Singapura, Bangalore",                13.0615, 77.5952, "Food & Grocery",    "free",     3.8,  40),
    # Nearby — Attur Layout (2 km south)
    ("Attur Layout Provisions",            "Main Road, Attur Layout, Yelahanka, Bangalore",  13.1078, 77.5772, "Food & Grocery",    "free",     4.0,  65),
    ("Hotel Green Park - Attur",           "Attur Layout, Yelahanka, Bangalore",             13.1075, 77.5775, "Food & Grocery",    "bronze",   4.2, 158),
    # Nearby — Doddaballapur Road (corridor)
    ("Doddaballapur Road Dhaba",           "Doddaballapur Road, Near Rajanukunte, Bangalore", 13.1310, 77.5822, "Food & Grocery",    "free",     4.1,  95),
    ("Road Side Auto Spares",              "Doddaballapur Road, Bangalore",                  13.1305, 77.5818, "Automotive",        "free",     3.9,  42),
    ("Farmers Market - Doddaballapur Rd",  "Doddaballapur Road, Bangalore",                  13.1315, 77.5825, "Food & Grocery",    "free",     4.3, 132),
]

# Product catalogue per category — used when creating catalogue items
CATEGORY_PRODUCTS = {
    "Food & Grocery": [
        # ── Generic search aliases ───────────────────────────────────────────────
        "Fruits", "Fruit", "Vegetables", "Vegetable", "Veggies",
        "Groceries", "Grocery", "Staples", "Dairy", "Eggs",
        "Snacks", "Beverages", "Drinks", "Sweets", "Spices",
        "Bread", "Bakery", "Noodles", "Pasta",

        # ── Fruits ──────────────────────────────────────────────────────────────
        "Mango", "Alphonso Mango", "Badami Mango", "Totapuri Mango", "Dasheri Mango",
        "Banana", "Robusta Banana", "Nendran Banana", "Red Banana", "Green Banana",
        "Apple", "Shimla Apple", "Royal Gala Apple", "Fuji Apple", "Green Apple",
        "Grapes", "Green Grapes", "Black Grapes", "Red Grapes", "Seedless Grapes",
        "Watermelon", "Muskmelon", "Honeydew Melon",
        "Papaya", "Raw Papaya",
        "Pomegranate", "Guava", "Orange", "Mosambi (Sweet Lime)", "Mandarin",
        "Pineapple", "Sapota (Chikoo)", "Jackfruit", "Raw Jackfruit",
        "Coconut", "Tender Coconut", "Lemon", "Lime",
        "Strawberry", "Kiwi", "Dragon Fruit", "Avocado",
        "Pear", "Plum", "Peach", "Apricot", "Cherry",
        "Litchi", "Custard Apple (Sitaphal)", "Jamun (Black Plum)",
        "Amla (Indian Gooseberry)", "Tamarind", "Passion Fruit",
        "Dates (Khajur)", "Fig (Anjeer)", "Mulberry",
        "Raspberry", "Blueberry", "Blackberry",

        # ── Vegetables ──────────────────────────────────────────────────────────
        "Tomato", "Cherry Tomato", "Onion", "Shallots", "Spring Onion",
        "Potato", "Sweet Potato", "Carrot", "Radish", "White Radish (Mooli)",
        "Spinach", "Methi (Fenugreek Leaves)", "Coriander", "Curry Leaves", "Mint Leaves",
        "Green Chilli", "Red Chilli", "Capsicum", "Brinjal (Baingan)",
        "Beans", "Cluster Beans (Guar)", "Broad Beans",
        "Cauliflower", "Broccoli", "Cabbage", "Knol Khol",
        "Drumstick (Moringa)", "Bitter Gourd (Karela)",
        "Ridge Gourd", "Snake Gourd", "Bottle Gourd (Lauki)",
        "Pumpkin", "Ash Gourd", "Ivy Gourd (Tindora)",
        "Raw Banana", "Raw Mango", "Corn (Maize)",
        "Peas", "Lady Finger (Okra)", "Taro Root (Arbi)",
        "Garlic", "Ginger", "Beetroot", "Turnip",
        "Yam (Suran)", "Elephant Foot Yam", "Raw Jackfruit",
        "Banana Flower", "Banana Stem", "Colocasia Leaves",
        "French Beans", "Runner Beans", "Flat Beans",

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
        # Men
        "Men's Round Neck T-Shirt", "Men's V-Neck T-Shirt", "Men's Polo T-Shirt",
        "Men's Formal Shirt", "Men's Casual Shirt", "Men's Linen Shirt",
        "Men's Jeans (Slim Fit)", "Men's Jeans (Regular)", "Men's Chinos",
        "Men's Formal Trousers", "Men's Shorts", "Men's Track Pants",
        "Men's Blazer", "Men's Suit", "Men's Waistcoat",
        "Men's Sweatshirt", "Men's Hoodie", "Men's Jacket",
        "Men's Kurta", "Men's Kurta Pyjama Set", "Men's Dhoti",
        "Men's Underwear (Briefs)", "Men's Boxer Shorts",
        "Men's Socks", "Men's Belt", "Men's Tie",
        # Women
        "Women's Kurti", "Women's Kurta Set", "Women's Anarkali",
        "Women's Saree (Silk)", "Women's Saree (Cotton)", "Women's Saree (Chiffon)",
        "Women's Salwar Kameez", "Women's Dupatta",
        "Women's Top", "Women's Blouse", "Women's Tank Top",
        "Women's Leggings", "Women's Palazzo", "Women's Skirt",
        "Women's Jeans", "Women's Jeggings", "Women's Shorts",
        "Women's Dress", "Women's Ethnic Gown", "Women's Night Suit",
        "Women's Sports Bra", "Women's Innerwear",
        "Women's Cardigan", "Women's Sweater", "Women's Jacket",
        # Kids
        "Kids T-Shirt", "Kids Frock", "Kids Shorts", "Kids Track Suit",
        "School Uniform Shirt", "School Uniform Trouser/Skirt",
        "Kids Kurta Pyjama", "Baby Romper", "Baby Onesie",
        # Accessories
        "Scarf", "Stole", "Cap", "Hat (Sun Hat)", "Beanie",
        "Gloves", "Woollen Socks",
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
        # Vitamins & Supplements
        "Vitamin C Tablets", "Vitamin D3 Tablets", "Vitamin B12 Tablets",
        "Multivitamins (Men)", "Multivitamins (Women)", "Calcium + D3",
        "Omega 3 Fish Oil", "Biotin Tablets", "Iron Tablets", "Zinc Tablets",
        "Protein Powder (Whey)", "Protein Powder (Plant Based)", "BCAA",
        "Mass Gainer", "Pre-Workout Supplement", "Creatine",
        # Ayurvedic
        "Ashwagandha Tablets", "Triphala Churna", "Chyawanprash",
        "Tulsi Drops", "Neem Tablets", "Giloy Juice", "Amla Juice",
        "Aloe Vera Juice", "Wheatgrass Powder", "Moringa Powder",
        # Medical Devices
        "Thermometer (Digital)", "Blood Pressure Monitor", "Glucometer",
        "Pulse Oximeter", "Nebulizer", "Heating Pad", "Ice Gel Pack",
        # First Aid
        "First Aid Kit", "Bandage Roll", "Adhesive Bandages",
        "Antiseptic Liquid (Dettol)", "Antiseptic Cream",
        "Hand Sanitizer", "Surgical Gloves", "Face Mask (N95)",
        # Immunity & Digestion
        "Hajmola", "Eno Fruit Salt", "ORS Powder", "Electrolyte Drink",
        "Probiotic Capsules", "Digestive Enzymes",
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


def generate_seller(index: int) -> dict:
    """Generate a single seller with GPS that matches the address locality."""
    rng = random.Random(index + 9999)
    prefix    = rng.choice(SHOP_PREFIXES)
    shop_type = rng.choice(SHOP_TYPES)
    # Round-robin through all localities so every area gets equal coverage
    locality  = BANGALORE_LOCALITIES[index % len(BANGALORE_LOCALITIES)]
    category  = rng.choice(CATEGORIES)
    tier      = random.choices(SUBSCRIPTION_TIERS, weights=TIER_WEIGHTS, k=1)[0]

    lat, lon = locality_gps(locality, rng)

    return {
        "seller_id":         str(uuid.uuid4()),
        "name":              f"{prefix} {shop_type}",
        "address":           f"{rng.randint(1, 200)}, {locality}, Bangalore",
        "latitude":          round(lat, 6),
        "longitude":         round(lon, 6),
        "category":          category,
        "subscription_tier": tier,
        "rating":            round(rng.uniform(3.0, 5.0), 2),
        "review_count":      rng.randint(0, 500),
    }


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
        "name":        f"{seller['name']} — {seller['category']}",
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
    Works directly from Redis — no MongoDB items collection needed.
    Matches product_query against CATEGORY_PRODUCTS, then FT.SEARCHes
    sellers in those categories within radius_km.
    Falls back to MongoDB seller_geo if Redis TTL has expired.
    """
    product_lower = product_query.lower().strip()

    # 1. Which categories carry this product?
    category_matches: dict[str, list[str]] = {}
    for cat, products in CATEGORY_PRODUCTS.items():
        matched = [p for p in products if product_lower in p.lower()]
        if matched:
            category_matches[cat] = matched

    if not category_matches:
        return []   # product not in our catalogue at all

    results: list[dict] = []
    seen: set[str] = set()

    # 2. For each matching category, FT.SEARCH sellers near user
    for category, matched_products in category_matches.items():
        safe_cat = (category
                    .replace(" ", "\\ ")
                    .replace("&", "\\&")
                    .replace("-", "\\-"))
        query = (f"@category:{{{safe_cat}}} "
                 f"@location:[{user_lon} {user_lat} {radius_km} km]")

        try:
            response = r.execute_command(
                "FT.SEARCH", "idx:sellers", query,
                "LIMIT", "0", str(limit * 3),
            )
        except Exception as exc:
            console.print(f"[dim]FT.SEARCH error for {category}: {exc}[/dim]")
            continue

        _, docs = _parse_ft_response(response)

        for doc in docs:
            key = doc.get("_key", "")
            sid = key.replace("seller:", "") if key.startswith("seller:") else key
            if not sid or sid in seen:
                continue
            seen.add(sid)

            try:
                s_lat = float(doc.get("lat", user_lat))
                s_lon = float(doc.get("lon", user_lon))
                dist  = haversine_km(user_lat, user_lon, s_lat, s_lon)
            except (ValueError, TypeError):
                dist = 0.0

            item_list = [
                {"name": p, "category": category, "price": 0, "quantity": 50, "tags": []}
                for p in matched_products[:6]
            ]

            results.append({
                "seller_id":    sid,
                "name":         doc.get("name", sid[:8]),
                "address":      doc.get("address", "—"),
                "rating":       float(doc.get("rating", 0)),
                "review_count": doc.get("review_count", "0"),
                "tier":         doc.get("subscription_tier", "free"),
                "available":    doc.get("available", "true"),
                "category":     category,
                "_dist":        dist,
                "_items":       item_list,
            })

    # 3. If Redis is empty (TTL expired), fall back to MongoDB seller_geo
    if not results:
        try:
            geo_client = pymongo.MongoClient(MONGO_URL, authSource="admin",
                                             serverSelectionTimeoutMS=3000)
            geo_db = geo_client[MONGO_DB]
            target_cats = list(category_matches.keys())
            for doc in geo_db.seller_geo.find(
                {"category": {"$in": target_cats}},
                {"_id": 0, "seller_id": 1, "name": 1, "address": 1,
                 "lat": 1, "lon": 1, "rating": 1, "review_count": 1,
                 "subscription_tier": 1, "category": 1},
            ):
                sid = doc.get("seller_id", "")
                if not sid or sid in seen:
                    continue
                s_lat, s_lon = doc["lat"], doc["lon"]
                dist = haversine_km(user_lat, user_lon, s_lat, s_lon)
                if dist > radius_km:
                    continue
                seen.add(sid)
                cat = doc.get("category", "")
                item_list = [
                    {"name": p, "category": cat, "price": 0, "quantity": 50, "tags": []}
                    for p in category_matches.get(cat, [])[:6]
                ]
                results.append({
                    "seller_id":    sid,
                    "name":         doc.get("name", sid[:8]),
                    "address":      doc.get("address", "—"),
                    "rating":       float(doc.get("rating", 0)),
                    "review_count": str(doc.get("review_count", 0)),
                    "tier":         doc.get("subscription_tier", "free"),
                    "available":    "true",
                    "category":     cat,
                    "_dist":        dist,
                    "_items":       item_list,
                })
            geo_client.close()
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
                           user_lat: float, user_lon: float, radius_km: float) -> None:
    console.print()
    if not results:
        console.print(Panel(
            f"[yellow]No shops near you sell '[bold]{product_query}[/bold]' within {radius_km:.0f} km.[/yellow]\n"
            "[dim]Try :radius 5 to widen the search, or check the spelling.[/dim]",
            border_style="yellow",
        ))
        console.print()
        return

    console.print(Panel(
        f"[bold]{len(results)}[/bold] shop{'s' if len(results) != 1 else ''} near you sell "
        f"[bold cyan]{product_query}[/bold cyan]  "
        f"[dim](within {radius_km:.0f} km, nearest first)[/dim]",
        border_style="cyan", padding=(0, 1),
    ))

    for i, shop in enumerate(results, 1):
        rating = shop["rating"]
        stars  = "★" * int(rating) + "☆" * (5 - int(rating))
        tier   = shop["tier"]
        avail  = "[green]open[/green]" if shop["available"] == "true" else "[dim]closed[/dim]"
        tier_color = TIER_COLORS.get(tier, "dim")

        console.print(
            f"  [bold]{i}.[/bold] [bold]{shop['name']}[/bold]  "
            f"[dim]{shop['address']}[/dim]  "
            f"[cyan]{shop['_dist']:.2f} km[/cyan]  "
            f"{rating:.1f} [dim]{stars}[/dim]  "
            f"[{tier_color}]{tier}[/{tier_color}]  {avail}"
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


def render_search_results(total: int, docs: list[dict], desc: str, limit: int) -> None:
    shown = len(docs)
    header = (
        f"[bold]{shown}[/bold] result{'s' if shown != 1 else ''}"
        + (f"  [dim](of {total} total)[/dim]" if total > shown else "")
        + f"  [dim]— {desc}[/dim]"
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
    tbl.add_column("Phone",     style="green", min_width=14)

    for i, doc in enumerate(docs, 1):
        rating  = float(doc.get("rating", 0))
        tier    = doc.get("subscription_tier", "free")
        avail   = doc.get("available", "false")
        stars   = "★" * int(rating) + ("½" if (rating % 1) >= 0.5 else "") + "☆" * (5 - int(rating) - (1 if (rating % 1) >= 0.5 else 0))
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
            console.print(f"[green]Location set: {lat}, {lon}[/green]")
            return lat, lon, is_area
        console.print("[red]Not recognised. Try area name like 'Koramangala' or coords like '12.9352 77.6245'[/red]")


def interactive_search(r: redis.Redis, start_lat: float = None, start_lon: float = None) -> None:
    # If location not passed via CLI, ask the user
    is_area = False
    if start_lat is None or start_lon is None:
        start_lat, start_lon, is_area = _ask_location(BASE_LAT, BASE_LON)

    console.print()
    console.print(Panel.fit(
        "[bold cyan]Padosme — Seller Search[/bold cyan]\n"
        "[dim]Type a seller name/keyword to search, or use commands:\n"
        "  [bold]:product <name>[/bold]       e.g. :product mango  (search items in shops)\n"
        "  [bold]:area <name>[/bold]          e.g. :area Koramangala\n"
        "  [bold]:location <lat> <lon>[/bold]  e.g. :location 12.9352 77.6245\n"
        "  [bold]:category <name>[/bold]   [bold]:radius <km>[/bold]   "
        "[bold]:sort rating|distance|popularity[/bold]\n"
        "  [bold]:tier gold|silver|...[/bold]   "
        "[bold]:available[/bold]   [bold]:limit <n>[/bold]   "
        "[bold]:reset[/bold]   [bold]:quit[/bold][/dim]",
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
            # Try product catalogue search as last resort
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

        render_search_results(total, docs, _desc(), state["limit"])

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
        elif raw.startswith(":product "):
            product_query = raw[len(":product "):].strip()
            if product_query:
                results = search_products(
                    r, product_query,
                    state["user_lat"], state["user_lon"],
                    state["radius_km"], state["limit"],
                )
                render_product_results(results, product_query,
                                       state["user_lat"], state["user_lon"],
                                       state["radius_km"])
            else:
                console.print("[red]Usage: :product mango[/red]")
        elif raw.startswith(":area "):
            query = raw.split(" ", 1)[1].strip()
            result = _resolve_location(query)
            if result:
                state["user_lat"], state["user_lon"] = result
                state["radius_km"] = 2.0   # tight radius — only that locality
                state["sort_by"]   = "distance"
                console.print(f"[green]Area set to '{query.title()}' — showing shops within 2 km, nearest first.[/green]")
                _run()
            else:
                console.print("[red]Not recognised. Try: :area Koramangala  or  :area Indiranagar[/red]")
        elif raw.startswith(":location "):
            query = raw.split(" ", 1)[1].strip()
            result = _resolve_location(query)
            if result:
                state["user_lat"], state["user_lon"] = result
                console.print(f"[green]Location → {state['user_lat']}, {state['user_lon']}[/green]")
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
            state["keyword"] = raw
            _run()


def search_mode(args) -> None:
    """One-shot or interactive search."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    try:
        r.ping()
    except Exception as exc:
        console.print(f"[red]Cannot connect to Redis: {exc}[/red]")
        raise SystemExit(1)

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


def _category_for_seller(seller_id: str) -> str:
    """Deterministic category assignment for a seller (seeded by seller_id)."""
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

    # Pick more items for food shops (wider variety), fewer for others
    if category == "Food & Grocery":
        n_items = rng.randint(10, min(20, len(products)))
    elif category == "Electronics":
        n_items = rng.randint(4, min(8, len(products)))
    else:
        n_items = rng.randint(3, min(6, len(products)))
    selected = rng.sample(products, n_items)

    now = datetime.now(timezone.utc).isoformat()
    docs = []
    for product_name in selected:
        # Price in paise: ₹10 – ₹5000 range depending on category
        if category == "Electronics":
            price_paise = rng.randint(50000, 500000)    # ₹500 – ₹5000
        elif category in ("Food & Grocery",):
            price_paise = rng.randint(1000, 50000)      # ₹10 – ₹500
        else:
            price_paise = rng.randint(5000, 200000)     # ₹50 – ₹2000

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

    console.print("\n[dim]Fetching catalogs from MongoDB…[/dim]")
    try:
        client = pymongo.MongoClient(MONGO_URL, authSource="admin", serverSelectionTimeoutMS=5000)
        db = client[MONGO_DB]
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

    # Build seller dicts with GPS
    sellers = [build_seller_for_sync(doc, i) for i, doc in enumerate(sellers_raw)]

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

    # Generate seller data upfront — real named shops first, then random fills
    real_sellers = generate_real_sellers()
    sellers = real_sellers + [generate_seller(i) for i in range(args.count)]

    console.print(f"[dim]  {len(real_sellers)} real named shops + {args.count} generated sellers = {len(sellers)} total[/dim]")

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
            f"Seeding {len(sellers)} sellers ({len(real_sellers)} real + {args.count} generated)…",
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
    tbl.add_row("  └─ real named shops",        str(len(real_sellers)))
    tbl.add_row("  └─ generated sellers",       str(args.count))
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
            f"[yellow]{events_ok}/{args.count} sellers published.\n"
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
