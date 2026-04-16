#!/usr/bin/env python3
"""
Padosme — Google Maps Seller Discovery Importer
================================================
Production-grade pipeline that fetches real businesses from Google Places API
across Bangalore metropolitan region and stores them as DISCOVERED sellers.

Data policy
-----------
  • Uses ONLY Google Places API (Nearby Search + Place Details)
  • Zero synthetic data: no fake names, ratings, phones, or hours
  • Permanently closed businesses are silently dropped
  • Stops safely on quota exhaustion — no fallback data sources

Storage modes
-------------
  --use-db-mode   Write directly to MongoDB discovery.sellers
  --use-api-mode  POST to catalogue-service REST API
  (both flags can be set simultaneously)

Usage
-----
    python3 seed_bangalore_sellers.py \\
        --google-api-key YOUR_KEY \\
        --radius 45 \\
        --count 3000 \\
        --grid-size 10 \\
        --threads 8 \\
        --batch-size 100 \\
        --use-db-mode \\
        --include-phone \\
        --include-ratings \\
        --verbose

Requirements
------------
    pip install requests pymongo rich
"""

# ── Standard library ──────────────────────────────────────────────────────────
import argparse
import logging
import math
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Optional

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    import pymongo
    import pymongo.errors
    import requests
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table
    from rich import box
except ImportError as _missing:
    sys.exit(
        f"[ERROR] Missing dependency: {_missing}\n"
        "Install: pip install requests pymongo rich"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

console = Console()

logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
log = logging.getLogger("discovery")


# ─────────────────────────────────────────────────────────────────────────────
# Geographic constants
# ─────────────────────────────────────────────────────────────────────────────

# Bangalore city centre
BANGALORE_LAT: float = 12.9716
BANGALORE_LON: float = 77.5946

EARTH_RADIUS_KM: float = 6371.0

# Bounding box: 45 km radius from centre covers the full metro + airport corridor.
#   South  → Electronic City  (12.840°N)
#   North  → Devanahalli      (13.247°N)
#   West   → Kengeri          (77.482°E)
#   East   → Whitefield       (77.785°E)
#
# 45 km in degrees:  lat ≈ 45/111 = 0.405°   lon ≈ 45/96 = 0.469°
BBOX_LAT_MIN: float = BANGALORE_LAT - 0.405   # ≈ 12.567°N
BBOX_LAT_MAX: float = BANGALORE_LAT + 0.405   # ≈ 13.377°N  (past Devanahalli)
BBOX_LON_MIN: float = BANGALORE_LON - 0.469   # ≈ 77.126°E
BBOX_LON_MAX: float = BANGALORE_LON + 0.469   # ≈ 78.064°E


# ─────────────────────────────────────────────────────────────────────────────
# Google Places API
# ─────────────────────────────────────────────────────────────────────────────

PLACES_NEARBY_URL  = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# Fields requested on Place Details — only real Google data, nothing fabricated.
DETAIL_FIELDS = (
    "place_id,name,types,"
    "geometry/location,"
    "formatted_address,vicinity,"
    "rating,user_ratings_total,"
    "formatted_phone_number,website,"
    "opening_hours,business_status,url"
)

# Status codes that mean our quota is exhausted or key is invalid.
QUOTA_STATUS_CODES = frozenset({"OVER_QUERY_LIMIT", "REQUEST_DENIED"})

# Statuses we treat as permanently closed and discard.
CLOSED_STATUSES = frozenset({"PERMANENTLY_CLOSED"})

# Google Places API allows up to 3 pages (60 results) per Nearby Search query.
MAX_PAGES_PER_QUERY = 3

# Minimum sleep between page-token requests (Google enforces ~2 s).
PAGE_TOKEN_SLEEP_S = 2.2

# Retry / backoff
MAX_RETRIES    = 3
BACKOFF_BASE_S = 1.5     # wait = BACKOFF_BASE_S × 2^(attempt-1)


# ─────────────────────────────────────────────────────────────────────────────
# Business type catalogue
# ─────────────────────────────────────────────────────────────────────────────
# Each entry: (padosme_category, google_places_type, optional_keyword)
#   • padosme_category → stored as primary_category in the seller document
#   • google_places_type → used in the Nearby Search "type" parameter
#   • optional_keyword  → extra keyword to narrow results (e.g. "mobile phone")

BUSINESS_TYPES: list[tuple[str, str, Optional[str]]] = [
    ("restaurant",             "restaurant",              None),
    ("pharmacy",               "pharmacy",                None),
    ("grocery_or_supermarket", "grocery_or_supermarket",  None),
    ("electronics_store",      "electronics_store",       None),
    ("bakery",                 "bakery",                  None),
    ("clothing_store",         "clothing_store",          None),
    ("salon",                  "beauty_salon",            None),
    ("hospital",               "hospital",                None),
    ("hardware_store",         "hardware_store",          None),
    ("mobile_store",           "electronics_store",       "mobile phone store"),
]

# Quick lookup: padosme category → row in BUSINESS_TYPES
_CATEGORY_INDEX: dict[str, tuple[str, str, Optional[str]]] = {
    row[0]: row for row in BUSINESS_TYPES
}


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class QuotaExhaustedError(RuntimeError):
    """Google API signalled quota exhaustion or key denial."""


class PlacesAPIError(RuntimeError):
    """Transient / retryable Google API error."""


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two GPS coordinates."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(max(0.0, a)))


# ─────────────────────────────────────────────────────────────────────────────
# Grid engine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GridCell:
    row:      int
    col:      int
    lat:      float    # centre latitude
    lon:      float    # centre longitude
    label:    str      # nearest named area (for logging)
    radius_m: int      # per-cell search radius in metres


# Named landmarks used to produce human-readable cell labels.
_NAMED_AREAS: list[tuple[float, float, str]] = [
    (13.246, 77.712, "Devanahalli/Airport"),
    (12.970, 77.750, "Whitefield"),
    (12.959, 77.697, "Marathahalli"),
    (12.978, 77.641, "Indiranagar"),
    (12.928, 77.627, "Koramangala"),
    (12.912, 77.639, "HSR Layout"),
    (12.917, 77.610, "BTM Layout"),
    (12.840, 77.677, "Electronic City"),
    (12.910, 77.585, "JP Nagar"),
    (12.931, 77.583, "Jayanagar"),
    (12.990, 77.556, "Rajajinagar"),
    (13.004, 77.568, "Malleshwaram"),
    (13.035, 77.597, "Hebbal"),
    (13.101, 77.596, "Yelahanka"),
    (13.058, 77.624, "Thanisandra"),
    (13.042, 77.622, "Nagawara"),
    (13.042, 77.639, "Hennur"),
    (12.914, 77.482, "Kengeri"),
    (13.028, 77.550, "Yeshwanthpur"),
    (12.984, 77.595, "Sadashivanagar"),
    (12.969, 77.594, "MG Road"),
    (12.983, 77.607, "Commercial Street"),
    (13.007, 77.693, "KR Puram"),
    (12.956, 77.697, "Bellandur"),
    (13.067, 77.600, "Jakkur"),
    (13.020, 77.646, "Horamavu"),
    (13.017, 77.640, "Banaswadi"),
]


def _nearest_label(lat: float, lon: float) -> str:
    """Return the name of the nearest named area within 8 km, else coords."""
    best_d = float("inf")
    best_n = ""
    for alat, alon, name in _NAMED_AREAS:
        d = haversine_km(lat, lon, alat, alon)
        if d < best_d:
            best_d, best_n = d, name
    return best_n if best_d < 8.0 else f"({lat:.3f},{lon:.3f})"


def build_grid(radius_km: float, grid_size: int) -> list[GridCell]:
    """
    Divide the Bangalore metro bounding box into grid_size×grid_size cells.

    Only cells whose centre lies within ``radius_km`` of Bangalore centre
    are included.  Each cell's search radius equals its half-diagonal plus a
    15 % overlap buffer so adjacent cells share a small overlap band and
    leave no uncovered gaps.  Capped at 50 000 m (Google's Nearby Search max).
    """
    lat_span  = BBOX_LAT_MAX - BBOX_LAT_MIN
    lon_span  = BBOX_LON_MAX - BBOX_LON_MIN
    cell_dlat = lat_span / grid_size
    cell_dlon = lon_span / grid_size

    km_per_lat = 111.0
    km_per_lon = 111.0 * math.cos(math.radians(BANGALORE_LAT))

    half_diag_km = math.sqrt(
        (cell_dlat * km_per_lat / 2) ** 2
        + (cell_dlon * km_per_lon / 2) ** 2
    )
    cell_radius_m = int(min(half_diag_km * 1.15 * 1000, 50_000))

    cells: list[GridCell] = []
    for row in range(grid_size):
        for col in range(grid_size):
            clat = BBOX_LAT_MIN + (row + 0.5) * cell_dlat
            clon = BBOX_LON_MIN + (col + 0.5) * cell_dlon
            dist = haversine_km(BANGALORE_LAT, BANGALORE_LON, clat, clon)
            # Include cells whose centre is within radius + half-diagonal
            # so edge cells that straddle the boundary are not clipped.
            if dist > radius_km + half_diag_km:
                continue
            cells.append(GridCell(
                row=row,
                col=col,
                lat=clat,
                lon=clon,
                label=_nearest_label(clat, clon),
                radius_m=cell_radius_m,
            ))
    return cells


# ─────────────────────────────────────────────────────────────────────────────
# Google Places API client
# ─────────────────────────────────────────────────────────────────────────────

class GooglePlacesClient:
    """
    Thread-safe wrapper around the Google Places Nearby Search and
    Place Details APIs.

    A single ``quota_exhausted`` flag is shared across all threads:
    as soon as one thread hits OVER_QUERY_LIMIT the flag is set and
    every other thread stops making new API calls.
    """

    def __init__(self, api_key: str) -> None:
        self._key           = api_key
        self._sess          = requests.Session()
        self._quota_flag    = Event()

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def quota_exhausted(self) -> bool:
        return self._quota_flag.is_set()

    # ── Internal GET with retry ───────────────────────────────────────────────

    def _get(self, url: str, params: dict) -> dict:
        """
        Perform a GET request with exponential-backoff retry.
        Raises QuotaExhaustedError on OVER_QUERY_LIMIT / REQUEST_DENIED.
        Raises PlacesAPIError when all retries are exhausted.
        """
        params = dict(params)
        params["key"] = self._key
        last_exc: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._sess.get(url, params=params, timeout=20)
                resp.raise_for_status()
                data: dict = resp.json()
            except requests.exceptions.Timeout as exc:
                last_exc = exc
                log.debug("Timeout (attempt %d/%d)", attempt, MAX_RETRIES)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                log.debug("Request error: %s (attempt %d/%d)", exc, attempt, MAX_RETRIES)
            else:
                status = data.get("status", "UNKNOWN")
                if status in ("OK", "ZERO_RESULTS", "NOT_FOUND"):
                    return data
                if status in QUOTA_STATUS_CODES:
                    self._quota_flag.set()
                    raise QuotaExhaustedError(
                        f"Google API quota/access denied (status={status}, "
                        f"error={data.get('error_message', 'n/a')})"
                    )
                # Transient errors — retry
                last_exc = PlacesAPIError(
                    f"status={status}, error={data.get('error_message', 'n/a')}"
                )
                log.debug("API status %s (attempt %d/%d)", status, attempt, MAX_RETRIES)

            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.debug("Retrying in %.1f s …", wait)
                time.sleep(wait)

        raise last_exc or PlacesAPIError("All retries exhausted")

    # ── Nearby Search ─────────────────────────────────────────────────────────

    def nearby_search_page(
        self,
        lat: float,
        lon: float,
        radius_m: int,
        place_type: str,
        keyword: Optional[str] = None,
        page_token: Optional[str] = None,
    ) -> dict:
        """Single page of Nearby Search results."""
        if page_token:
            # Google requires a pause before consuming next_page_token.
            time.sleep(PAGE_TOKEN_SLEEP_S)
            params: dict = {"pagetoken": page_token}
        else:
            params = {
                "location": f"{lat},{lon}",
                "radius":   radius_m,
                "type":     place_type,
            }
            if keyword:
                params["keyword"] = keyword
        return self._get(PLACES_NEARBY_URL, params)

    def nearby_search_all(
        self,
        lat: float,
        lon: float,
        radius_m: int,
        place_type: str,
        keyword: Optional[str] = None,
    ) -> list[dict]:
        """
        Paginate through all Nearby Search results for a single type.
        Returns a list of place stubs (sans detail fields).
        Stops at MAX_PAGES_PER_QUERY pages or when no next_page_token.
        """
        if self.quota_exhausted:
            raise QuotaExhaustedError("Quota already exhausted")

        stubs:      list[dict]    = []
        page_token: Optional[str] = None

        for page_num in range(1, MAX_PAGES_PER_QUERY + 1):
            data       = self.nearby_search_page(lat, lon, radius_m, place_type,
                                                  keyword=keyword, page_token=page_token)
            stubs.extend(data.get("results", []))
            page_token = data.get("next_page_token")
            if not page_token:
                break

        return stubs

    # ── Place Details ─────────────────────────────────────────────────────────

    def place_details(self, place_id: str) -> dict:
        """
        Fetch full Place Details for one place_id.
        Returns the ``result`` sub-dict (may be empty on NOT_FOUND).
        """
        if self.quota_exhausted:
            raise QuotaExhaustedError("Quota already exhausted")
        data = self._get(PLACES_DETAILS_URL, {
            "place_id": place_id,
            "fields":   DETAIL_FIELDS,
        })
        return data.get("result", {})


# ─────────────────────────────────────────────────────────────────────────────
# Data transformer  (Google → Padosme seller document)
# ─────────────────────────────────────────────────────────────────────────────

_RE_JUNK_PHONE = re.compile(r"[^\d+\-\s()]")


def _normalise_phone(raw: str) -> str:
    """Clean and optionally reformat an Indian phone number."""
    if not raw:
        return ""
    cleaned = _RE_JUNK_PHONE.sub("", raw.strip())
    digits  = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return f"+91 {digits[:5]} {digits[5:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits[:2]} {digits[2:7]} {digits[7:]}"
    return cleaned.strip()


def _normalise_address(raw: str) -> str:
    """Light normalisation: strip trailing ', India', collapse whitespace."""
    if not raw:
        return "Bangalore, Karnataka, India"
    addr = re.sub(r",?\s*India\s*$", "", raw.strip(), flags=re.IGNORECASE)
    addr = re.sub(r"\s{2,}", " ", addr)
    return addr


def detail_to_seller(
    detail:          dict,
    padosme_category: str,
    include_phone:   bool,
    include_ratings: bool,
) -> Optional[dict]:
    """
    Convert a raw Google Place Details dict into a Padosme seller document.

    Returns ``None`` when:
      • place_id or name is missing
      • business_status is PERMANENTLY_CLOSED
      • coordinates are absent
    """
    place_id = detail.get("place_id", "").strip()
    name     = detail.get("name", "").strip()
    if not place_id or not name:
        return None

    biz_status = detail.get("business_status", "OPERATIONAL")
    if biz_status in CLOSED_STATUSES:
        return None

    geo = detail.get("geometry", {}).get("location", {})
    lat = geo.get("lat")
    lon = geo.get("lng")
    if lat is None or lon is None:
        return None

    # 7 decimal places ≈ 1 cm precision — sufficient, avoids float noise
    lat = round(float(lat), 7)
    lon = round(float(lon), 7)

    raw_address = (
        detail.get("formatted_address") or detail.get("vicinity", "")
    )
    address = _normalise_address(raw_address)

    google_types  = detail.get("types") or []
    oh            = detail.get("opening_hours") or {}
    open_now      = oh.get("open_now")           # bool | None
    weekday_text  = oh.get("weekday_text") or []  # list[str]

    doc: dict = {
        # ── Identity ──────────────────────────────────────────────────────────
        "seller_id":         str(uuid.uuid4()),
        "place_id":          place_id,
        # ── Business info ─────────────────────────────────────────────────────
        "name":              name,
        "primary_category":  padosme_category,
        "categories":        google_types,
        # ── Location ──────────────────────────────────────────────────────────
        "latitude":          lat,
        "longitude":         lon,
        "address":           address,
        "city":              "Bangalore",
        # GeoJSON Point — consumed by discovery-service geo queries
        "location": {
            "type":        "Point",
            "coordinates": [lon, lat],
        },
        # Elasticsearch-compatible geo — consumed by indexing-service
        "location_es": {
            "lat": lat,
            "lon": lon,
        },
        # ── Contact / web ─────────────────────────────────────────────────────
        "website":           detail.get("website") or "",
        "google_maps_url":   detail.get("url") or "",
        # ── Availability ─────────────────────────────────────────────────────
        "opening_hours": {
            "open_now":     open_now,
            "weekday_text": weekday_text,
        },
        "business_status":   biz_status,
        # ── Pipeline status ───────────────────────────────────────────────────
        "seller_status":     "DISCOVERED",
        "invitation_sent":   False,
        "verified":          False,
        "onboarded":         False,
        "discovered_at":     datetime.now(timezone.utc).isoformat(),
    }

    if include_ratings:
        doc["rating"]             = detail.get("rating")            # float | None
        doc["user_ratings_total"] = detail.get("user_ratings_total")  # int | None

    if include_phone:
        doc["phone_number"] = _normalise_phone(
            detail.get("formatted_phone_number") or ""
        )

    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Storage backends
# ─────────────────────────────────────────────────────────────────────────────

class MongoStore:
    """
    Persist sellers directly into MongoDB ``discovery.sellers``.

    Deduplication is enforced at the database level via a unique index on
    ``place_id``.  Upserts use ``$setOnInsert`` so re-running the importer
    never overwrites an already-stored seller.
    """

    def __init__(self, mongo_url: str, db_name: str = "discovery") -> None:
        self._url     = mongo_url
        self._db_name = db_name
        self._client: Optional[pymongo.MongoClient]       = None
        self._col:    Optional[pymongo.collection.Collection] = None

    def connect(self) -> None:
        self._client = pymongo.MongoClient(
            self._url,
            serverSelectionTimeoutMS=8_000,
            connectTimeoutMS=8_000,
        )
        self._client.admin.command("ping")
        db        = self._client[self._db_name]
        self._col = db.sellers

        # Unique index — the primary deduplication gate
        self._col.create_index("place_id", unique=True, sparse=True, background=True)
        # 2dsphere index — enables $near / $geoWithin in discovery-service
        self._col.create_index(
            [("location", pymongo.GEOSPHERE)],
            background=True,
        )
        log.info("MongoDB connected → %s / %s.sellers", self._url, self._db_name)

    def close(self) -> None:
        if self._client:
            self._client.close()

    def insert_batch(self, sellers: list[dict]) -> tuple[int, int]:
        """
        Bulk-upsert sellers.  ``$setOnInsert`` means existing documents are
        never modified.  Returns ``(inserted, skipped_as_duplicates)``.
        """
        if not sellers or self._col is None:
            return 0, 0

        ops = [
            pymongo.UpdateOne(
                {"place_id": s["place_id"]},
                {"$setOnInsert": s},
                upsert=True,
            )
            for s in sellers
        ]
        try:
            result   = self._col.bulk_write(ops, ordered=False)
            inserted = result.upserted_count
            skipped  = len(sellers) - inserted
            return inserted, skipped
        except pymongo.errors.BulkWriteError as bwe:
            details  = bwe.details
            inserted = details.get("nUpserted", 0)
            skipped  = len(sellers) - inserted
            non_dup  = [e for e in details.get("writeErrors", [])
                        if e.get("code") != 11000]
            if non_dup:
                log.warning("%d non-duplicate write errors (first 3 shown):", len(non_dup))
                for err in non_dup[:3]:
                    log.warning("  code=%s  msg=%s", err.get("code"), err.get("errmsg", ""))
            return inserted, skipped
        except Exception as exc:
            log.error("MongoDB bulk_write failed: %s", exc)
            return 0, len(sellers)


class APIStore:
    """
    Push sellers to the catalogue-service REST API.

    Attempts a bulk endpoint first (``POST /api/sellers/bulk-discover``).
    Falls back to individual ``POST /api/sellers/discover`` requests if the
    bulk endpoint returns 404.  A 409 Conflict from the individual endpoint
    is treated as a successful duplicate-skip (not an error).
    """

    def __init__(self, api_url: str) -> None:
        self._base      = api_url.rstrip("/")
        self._sess      = requests.Session()
        self._has_bulk: Optional[bool] = None  # lazily probed

    def insert_batch(self, sellers: list[dict]) -> tuple[int, int]:
        """Returns ``(inserted, failed)``."""
        if not sellers:
            return 0, 0

        # ── Try bulk endpoint ─────────────────────────────────────────────────
        if self._has_bulk is not False:
            try:
                resp = self._sess.post(
                    f"{self._base}/api/sellers/bulk-discover",
                    json={"sellers": sellers},
                    timeout=60,
                )
                if resp.status_code in (200, 201):
                    self._has_bulk = True
                    body = resp.json() if resp.content else {}
                    ins  = body.get("inserted", len(sellers))
                    skip = body.get("skipped",  0)
                    return ins, skip
                if resp.status_code == 404:
                    log.debug("Bulk endpoint not found; falling back to individual POSTs")
                    self._has_bulk = False
                else:
                    log.warning(
                        "Bulk endpoint returned HTTP %d: %s",
                        resp.status_code, resp.text[:300],
                    )
            except Exception as exc:
                log.warning("Bulk POST error: %s — falling back to individual POSTs", exc)
                self._has_bulk = False

        # ── Individual POST fallback ──────────────────────────────────────────
        ok = failed = 0
        for seller in sellers:
            try:
                resp = self._sess.post(
                    f"{self._base}/api/sellers/discover",
                    json=seller,
                    timeout=30,
                )
                if resp.status_code in (200, 201, 409):
                    ok += 1
                else:
                    log.debug(
                        "POST /discover returned %d for '%s'",
                        resp.status_code, seller.get("name"),
                    )
                    failed += 1
            except Exception as exc:
                log.warning("POST failed for '%s': %s", seller.get("name"), exc)
                failed += 1

        return ok, failed


# ─────────────────────────────────────────────────────────────────────────────
# Thread-safe shared discovery state
# ─────────────────────────────────────────────────────────────────────────────

class DiscoveryState:
    """
    Counters and deduplication set shared across all worker threads.
    All methods are safe to call concurrently.
    """

    def __init__(self, target: int) -> None:
        self._lock              = Lock()
        self._seen_ids: set[str] = set()
        self._inserted          = 0
        self._skipped           = 0
        self._failed            = 0
        self._api_errors        = 0
        self._cells_done        = 0
        self._quota_hit         = False
        self.target             = target

    # ── Deduplication ─────────────────────────────────────────────────────────

    def filter_unseen(self, sellers: list[dict]) -> list[dict]:
        """
        Return only sellers whose ``place_id`` has not been seen before.
        Registers the new IDs atomically.
        """
        with self._lock:
            new = [s for s in sellers if s["place_id"] not in self._seen_ids]
            for s in new:
                self._seen_ids.add(s["place_id"])
            return new

    # ── Mutators ──────────────────────────────────────────────────────────────

    def add_inserted(self, n: int) -> None:
        with self._lock:
            self._inserted += n

    def add_skipped(self, n: int) -> None:
        with self._lock:
            self._skipped += n

    def add_failed(self, n: int) -> None:
        with self._lock:
            self._failed += n

    def add_api_error(self) -> None:
        with self._lock:
            self._api_errors += 1

    def cell_done(self) -> None:
        with self._lock:
            self._cells_done += 1

    def set_quota_hit(self) -> None:
        with self._lock:
            self._quota_hit = True

    # ── Read-only properties ──────────────────────────────────────────────────

    @property
    def inserted(self) -> int:
        return self._inserted

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def api_errors(self) -> int:
        return self._api_errors

    @property
    def cells_done(self) -> int:
        return self._cells_done

    @property
    def quota_hit(self) -> bool:
        return self._quota_hit

    @property
    def target_reached(self) -> bool:
        return self._inserted >= self.target


# ─────────────────────────────────────────────────────────────────────────────
# Grid cell worker
# ─────────────────────────────────────────────────────────────────────────────

def scan_grid_cell(
    cell:            GridCell,
    client:          GooglePlacesClient,
    state:           DiscoveryState,
    include_phone:   bool,
    include_ratings: bool,
    category_filter: Optional[str],
    verbose:         bool,
) -> list[dict]:
    """
    Scan a single grid cell for all configured business types.

    For each business type:
      1. Paginate through Nearby Search results (up to 60 stubs)
      2. Fetch Place Details for each new place_id
      3. Convert to a Padosme seller document

    Returns the list of seller dicts found in this cell.
    Stops immediately if quota is exhausted or the insertion target is reached.
    """
    if client.quota_exhausted or state.quota_hit or state.target_reached:
        return []

    # Decide which business types to scan
    if category_filter and category_filter in _CATEGORY_INDEX:
        types_to_scan = [_CATEGORY_INDEX[category_filter]]
    else:
        types_to_scan = BUSINESS_TYPES

    cell_sellers: list[dict] = []
    seen_in_cell: set[str]   = set()

    for padosme_cat, google_type, keyword in types_to_scan:
        if client.quota_exhausted or state.quota_hit or state.target_reached:
            break

        # ── Nearby Search ─────────────────────────────────────────────────────
        try:
            stubs = client.nearby_search_all(
                cell.lat, cell.lon, cell.radius_m, google_type, keyword=keyword
            )
        except QuotaExhaustedError:
            state.set_quota_hit()
            raise
        except Exception as exc:
            state.add_api_error()
            log.warning("Nearby search error [%s / %s]: %s", cell.label, google_type, exc)
            continue

        # ── Place Details ─────────────────────────────────────────────────────
        for stub in stubs:
            if client.quota_exhausted or state.quota_hit or state.target_reached:
                break

            place_id = stub.get("place_id", "")
            if not place_id or place_id in seen_in_cell:
                continue
            seen_in_cell.add(place_id)

            try:
                detail = client.place_details(place_id)
            except QuotaExhaustedError:
                state.set_quota_hit()
                raise
            except Exception as exc:
                state.add_api_error()
                log.debug("Details error [%s]: %s", place_id, exc)
                continue

            if not detail:
                continue

            seller = detail_to_seller(
                detail,
                padosme_category=padosme_cat,
                include_phone=include_phone,
                include_ratings=include_ratings,
            )
            if seller:
                cell_sellers.append(seller)

    if verbose:
        console.print(
            f"  Scanning [cyan]{cell.label}[/cyan] grid cell "
            f"[dim]({cell.row},{cell.col})[/dim] ... "
            f"fetched [green]{len(cell_sellers)}[/green] businesses"
        )

    return cell_sellers


# ─────────────────────────────────────────────────────────────────────────────
# Main discovery pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_discovery(args: argparse.Namespace) -> None:

    # ── Resolve API key ───────────────────────────────────────────────────────
    api_key = (args.google_api_key or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not api_key:
        console.print("[red bold]ERROR: No Google API key provided.[/red bold]")
        console.print(
            "Supply via [bold]--google-api-key YOUR_KEY[/bold]  "
            "or  [bold]export GOOGLE_API_KEY=YOUR_KEY[/bold]"
        )
        sys.exit(1)

    # ── Default insertion mode ────────────────────────────────────────────────
    if not args.use_db_mode and not args.use_api_mode:
        args.use_db_mode = True

    # ── Build grid ────────────────────────────────────────────────────────────
    cells = build_grid(args.radius, args.grid_size)
    if not cells:
        console.print(
            f"[red]No grid cells fall within {args.radius} km of Bangalore centre. "
            "Try a larger --radius.[/red]"
        )
        sys.exit(1)

    # ── Connect to storage ────────────────────────────────────────────────────
    mongo_store: Optional[MongoStore] = None
    api_store:   Optional[APIStore]   = None

    if args.use_db_mode:
        mongo_store = MongoStore(args.mongo_url)
        try:
            mongo_store.connect()
        except Exception as exc:
            console.print(f"[red]MongoDB connection failed: {exc}[/red]")
            console.print(
                "[dim]Verify --mongo-url and that the MongoDB service is running.[/dim]"
            )
            sys.exit(1)

    if args.use_api_mode:
        api_store = APIStore(args.api_url)

    # ── Initialise shared objects ─────────────────────────────────────────────
    state  = DiscoveryState(target=args.count)
    client = GooglePlacesClient(api_key)

    active_types = (
        f"[cyan]{args.category}[/cyan] only"
        if args.category
        else f"all {len(BUSINESS_TYPES)} types"
    )
    mode_label = (
        ("MongoDB" if args.use_db_mode else "")
        + (" + " if args.use_db_mode and args.use_api_mode else "")
        + ("API" if args.use_api_mode else "")
    )

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Padosme — Google Maps Seller Discovery[/bold cyan]\n"
        f"Grid          : {args.grid_size}×{args.grid_size} "
        f"([green]{len(cells)}[/green] active cells within {args.radius} km)\n"
        f"Business types: {active_types}\n"
        f"Threads       : {args.threads}\n"
        f"Target        : {args.count:,} sellers\n"
        f"Batch size    : {args.batch_size}\n"
        f"Storage mode  : {mode_label}\n"
        f"Data source   : [bold]Google Places API only[/bold]",
        border_style="cyan",
    ))
    console.print()

    # ── Scan + persist ────────────────────────────────────────────────────────
    t_start             = time.perf_counter()
    pending:  list[dict] = []
    milestone_next       = 500   # print "Inserted N sellers" at each milestone

    def flush(batch: list[dict]) -> None:
        """Deduplicate and persist one batch."""
        new = state.filter_unseen(batch)
        if not new:
            state.add_skipped(len(batch))
            return

        if mongo_store:
            ins, skip = mongo_store.insert_batch(new)
            state.add_inserted(ins)
            state.add_skipped(skip)

        if api_store:
            ok, fail = api_store.insert_batch(new)
            if not mongo_store:
                state.add_inserted(ok)
                state.add_failed(fail)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(
            f"Scanning {len(cells)} grid cells", total=len(cells)
        )

        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = {}
            for cell in cells:
                if state.quota_hit or state.target_reached:
                    break
                f = pool.submit(
                    scan_grid_cell,
                    cell, client, state,
                    args.include_phone, args.include_ratings,
                    args.category or None,
                    args.verbose,
                )
                futures[f] = cell

            for future in as_completed(futures):
                cell = futures[future]
                state.cell_done()
                progress.advance(task)

                try:
                    cell_results = future.result()
                except QuotaExhaustedError:
                    state.set_quota_hit()
                    console.print(
                        "\n[red bold]Google API quota exhausted. "
                        "Stopping safely — no synthetic data will be generated.[/red bold]"
                    )
                    for f in futures:
                        f.cancel()
                    break
                except Exception as exc:
                    log.warning("Cell worker raised an unexpected error: %s", exc)
                    continue

                pending.extend(cell_results)

                # Flush when we have a full batch
                if len(pending) >= args.batch_size:
                    flush(pending)
                    pending.clear()

                # Milestone console output
                nonlocal_milestone = milestone_next
                while state.inserted >= nonlocal_milestone:
                    console.print(
                        f"[green]Inserted {nonlocal_milestone:,} sellers[/green]"
                    )
                    nonlocal_milestone += 500
                milestone_next = nonlocal_milestone

                if state.target_reached:
                    console.print(
                        f"\n[green]Target of {args.count:,} sellers reached.[/green]"
                    )
                    break

        # Flush any remaining sellers
        if pending:
            flush(pending)

    elapsed = time.perf_counter() - t_start

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if mongo_store:
        mongo_store.close()

    # ── Print final milestone if not already printed ───────────────────────────
    if state.inserted >= milestone_next - 500 and state.inserted > 0:
        pass  # already printed above
    elif state.inserted > 0:
        console.print(f"[green]Inserted {state.inserted:,} sellers[/green]")

    # ── Summary table ─────────────────────────────────────────────────────────
    console.print()
    tbl = Table(
        title="Discovery Summary",
        box=box.ROUNDED,
        show_lines=True,
        min_width=52,
    )
    tbl.add_column("Metric",  style="bold",    min_width=34)
    tbl.add_column("Value",   justify="right", min_width=14)

    tbl.add_row("Grid cells scanned",
                f"{state.cells_done} / {len(cells)}")
    tbl.add_row("Sellers inserted",
                f"[green]{state.inserted:,}[/green]")
    tbl.add_row("Duplicate places skipped",
                str(state.skipped))
    if state.failed:
        tbl.add_row("Insert failures",
                    f"[red]{state.failed:,}[/red]")
    if state.api_errors:
        tbl.add_row("Google API errors",
                    f"[yellow]{state.api_errors}[/yellow]")
    if state.quota_hit:
        tbl.add_row("Quota exhausted",
                    "[red bold]YES (partial run)[/red bold]")
    tbl.add_row("Elapsed",
                f"{elapsed:.1f}s")
    if elapsed > 0 and state.inserted:
        tbl.add_row("Throughput",
                    f"{state.inserted / elapsed:.1f} sellers/s")

    console.print(tbl)
    console.print()

    # ── Final verdict ─────────────────────────────────────────────────────────
    if state.inserted > 0:
        console.print(Panel(
            f"[green bold]Completed successfully.[/green bold]\n"
            f"Inserted [cyan]{state.inserted:,}[/cyan] DISCOVERED sellers.\n"
            f"[dim]seller_status=DISCOVERED  "
            f"invitation_sent=false  verified=false  onboarded=false[/dim]",
            border_style="green",
        ))
    elif state.quota_hit:
        console.print(Panel(
            "[red bold]Run terminated: Google API quota exhausted.[/red bold]\n"
            "[dim]No synthetic data was generated or inserted.[/dim]",
            border_style="red",
        ))
    else:
        console.print(Panel(
            "[yellow]No sellers were inserted.[/yellow]\n"
            "[dim]Check your API key, network connectivity, and --radius value.[/dim]",
            border_style="yellow",
        ))

    console.print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed_bangalore_sellers.py",
        description=(
            "Padosme — Google Maps Seller Discovery Importer\n"
            "Fetches real businesses from Google Places API and stores them\n"
            "as DISCOVERED sellers ready for platform onboarding."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Full Bangalore scan → MongoDB (recommended for production)
  python3 seed_bangalore_sellers.py \\
      --google-api-key YOUR_KEY \\
      --radius 45 --count 3000 \\
      --grid-size 10 --threads 8 --batch-size 100 \\
      --use-db-mode --include-phone --include-ratings --verbose

  # Restaurants only → catalogue-service API
  python3 seed_bangalore_sellers.py \\
      --google-api-key YOUR_KEY \\
      --category restaurant \\
      --use-api-mode --api-url http://localhost:8085 --verbose

  # Use environment variable for API key
  export GOOGLE_API_KEY=YOUR_KEY
  python3 seed_bangalore_sellers.py --use-db-mode --verbose
""",
    )

    # ── Authentication ────────────────────────────────────────────────────────
    parser.add_argument(
        "--google-api-key",
        default=os.environ.get("GOOGLE_API_KEY", ""),
        metavar="KEY",
        help="Google Places API key (or set GOOGLE_API_KEY env var)",
    )

    # ── Geographic coverage ───────────────────────────────────────────────────
    parser.add_argument(
        "--radius",
        type=float,
        default=45.0,
        metavar="KM",
        help="Scan radius in km from Bangalore centre (default: 45)",
    )
    parser.add_argument(
        "--grid-size",
        type=int,
        default=10,
        metavar="N",
        help="Divide coverage area into N×N grid cells (default: 10)",
    )

    # ── Business type filter ──────────────────────────────────────────────────
    parser.add_argument(
        "--category",
        default="",
        metavar="TYPE",
        choices=[
            "", "restaurant", "pharmacy", "grocery_or_supermarket",
            "electronics_store", "bakery", "clothing_store",
            "salon", "hospital", "hardware_store", "mobile_store",
        ],
        help="Limit scan to one business type (default: all 10 types)",
    )

    # ── Volume / performance ──────────────────────────────────────────────────
    parser.add_argument(
        "--count",
        type=int,
        default=3000,
        metavar="N",
        help="Stop after inserting this many sellers (default: 3000)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=8,
        metavar="N",
        help="Parallel scanner threads (default: 8)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        metavar="N",
        help="DB / API insert batch size (default: 100)",
    )

    # ── Insertion mode ────────────────────────────────────────────────────────
    parser.add_argument(
        "--use-db-mode",
        action="store_true",
        help="Write sellers directly into MongoDB discovery.sellers",
    )
    parser.add_argument(
        "--use-api-mode",
        action="store_true",
        help="POST sellers to the catalogue-service REST API",
    )
    parser.add_argument(
        "--mongo-url",
        default="mongodb://localhost:27017/catalogue_db",
        metavar="URL",
        help="MongoDB connection URL (default: mongodb://localhost:27017/catalogue_db)",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8085",
        metavar="URL",
        help="Catalogue-service base URL (default: http://localhost:8085)",
    )

    # ── Data fields ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--include-phone",
        action="store_true",
        help="Store phone_number from Google (empty string when not available)",
    )
    parser.add_argument(
        "--include-ratings",
        action="store_true",
        help="Store rating and user_ratings_total from Google",
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a line per grid cell showing businesses fetched",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (very verbose)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show seeding status across MongoDB, Redis, and PostgreSQL then exit",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_status() -> None:
    """Print a full seeding status report across every data store."""
    from rich.text import Text

    def ok(v):  return f"[green]{v}[/green]"
    def warn(v): return f"[yellow]{v}[/yellow]"
    def bad(v):  return f"[red]{v}[/red]"

    console.print()
    console.print(Panel.fit(
        "[bold cyan]Padosme — Seeding Status Report[/bold cyan]",
        border_style="cyan",
    ))
    console.print()

    tbl = Table(box=box.ROUNDED, show_lines=True, min_width=70)
    tbl.add_column("Store",      style="bold",    min_width=28)
    tbl.add_column("Collection / Key", min_width=26)
    tbl.add_column("Count",      justify="right", min_width=10)
    tbl.add_column("Notes",      min_width=28)

    # ── MongoDB ───────────────────────────────────────────────────────────────
    try:
        client = pymongo.MongoClient(
            "mongodb://deploy:kaaslabs123@localhost:27017/",
            authSource="admin",
            serverSelectionTimeoutMS=4000,
        )
        client.admin.command("ping")

        # catalog_db
        cdb = client["catalog_db"]
        seller_geo_n = cdb.seller_geo.count_documents({})
        catalogs_n   = cdb.catalogs.count_documents({})
        items_n      = cdb.items.count_documents({})

        tbl.add_row("MongoDB  catalog_db", "seller_geo",
                    ok(f"{seller_geo_n:,}") if seller_geo_n else bad("0"),
                    "GPS-assigned sellers")
        tbl.add_row("", "catalogs",
                    ok(f"{catalogs_n:,}") if catalogs_n else bad("0"),
                    "Catalogue entries")
        tbl.add_row("", "items",
                    ok(f"{items_n:,}") if items_n else bad("0"),
                    "Product items")

        # discovery
        ddb         = client["discovery"]
        total_d     = ddb.sellers.count_documents({})
        discovered  = ddb.sellers.count_documents({"seller_status": "DISCOVERED"})
        legacy      = ddb.sellers.count_documents(
            {"$or": [{"seller_status": {"$exists": False}},
                     {"seller_status": None}]}
        )
        google_src  = ddb.sellers.count_documents(
            {"place_id": {"$exists": True, "$ne": ""}}
        )
        tbl.add_row("MongoDB  discovery", "sellers (total)",
                    ok(f"{total_d:,}") if total_d else bad("0"), "")
        tbl.add_row("", "  status=DISCOVERED",
                    ok(f"{discovered:,}") if discovered else warn("0"),
                    "Google-imported" if discovered else "None yet")
        tbl.add_row("", "  status=null (legacy)",
                    warn(f"{legacy:,}") if legacy else ok("0"),
                    "Old OSM/hardcoded data" if legacy else "")
        tbl.add_row("", "  with place_id",
                    ok(f"{google_src:,}") if google_src else bad("0"),
                    "Real Google data" if google_src else "Run --google-api-key")

        client.close()
    except Exception as exc:
        tbl.add_row("MongoDB", "—", bad("ERR"), str(exc)[:40])

    tbl.add_row("", "", "", "")

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        import redis as redislib
        r = redislib.Redis(host="localhost", port=6379, decode_responses=True)
        r.ping()

        total_keys   = r.dbsize()
        seller_keys  = len(r.keys("seller:*"))

        idx_docs = -1
        idx_name = "—"
        try:
            idxs = r.execute_command("FT._LIST")
            if idxs:
                idx_name = idxs[0]
                info     = r.execute_command("FT.INFO", idx_name)
                pairs    = dict(zip(info[::2], info[1::2]))
                idx_docs = int(pairs.get("num_docs", -1))
        except Exception:
            pass

        tbl.add_row("Redis", "seller:* keys",
                    ok(f"{seller_keys:,}") if seller_keys else warn("0"),
                    "Geo-indexed seller hashes")
        tbl.add_row("", f"FT index  {idx_name}",
                    ok(f"{idx_docs:,}") if idx_docs > 0 else warn(str(idx_docs)),
                    "Full-text + geo searchable")
    except Exception as exc:
        tbl.add_row("Redis", "—", bad("ERR"), str(exc)[:40])

    tbl.add_row("", "", "", "")

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    try:
        import psycopg2
        conn = psycopg2.connect(
            "host=localhost port=5433 user=deploy "
            "password=kaaslabs123 dbname=padosme_indexing"
        )
        cur = conn.cursor()
        for table in ("indexed_sellers", "indexed_catalog", "processed_events"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            n = cur.fetchone()[0]
            note = {
                "indexed_sellers":  "Durable geo index (survives Redis TTL)",
                "indexed_catalog":  "Catalogue search index",
                "processed_events": "Pipeline event log",
            }.get(table, "")
            tbl.add_row(
                "PostgreSQL  padosme_indexing", table,
                ok(f"{n:,}") if n else warn("0"),
                note,
            )
        cur.close()
        conn.close()
    except Exception as exc:
        tbl.add_row("PostgreSQL", "—", bad("ERR"), str(exc)[:40])

    console.print(tbl)
    console.print()

    # ── Verdict ───────────────────────────────────────────────────────────────
    issues = []
    try:
        client = pymongo.MongoClient(
            "mongodb://deploy:kaaslabs123@localhost:27017/",
            authSource="admin", serverSelectionTimeoutMS=4000,
        )
        ddb        = client["discovery"]
        discovered = ddb.sellers.count_documents({"seller_status": "DISCOVERED"})
        google_src = ddb.sellers.count_documents({"place_id": {"$exists": True, "$ne": ""}})
        client.close()

        if google_src == 0:
            issues.append(
                "No Google Places data imported yet.\n"
                "  Run:  python3 ~/seed_bangalore_sellers.py \\\n"
                "          --google-api-key YOUR_KEY --radius 45 --count 3000 \\\n"
                "          --grid-size 10 --threads 8 --use-db-mode \\\n"
                "          --include-phone --include-ratings --verbose"
            )
        if discovered == 0 and google_src > 0:
            issues.append("Google sellers present but seller_status != DISCOVERED — check importer.")
    except Exception:
        pass

    try:
        import redis as redislib
        r = redislib.Redis(host="localhost", port=6379, decode_responses=True)
        r.ping()
        seller_keys = len(r.keys("seller:*"))
        import pymongo as pm2
        c2 = pm2.MongoClient("mongodb://deploy:kaaslabs123@localhost:27017/",
                              authSource="admin", serverSelectionTimeoutMS=4000)
        mongo_total = c2["catalog_db"].seller_geo.count_documents({})
        c2.close()
        if seller_keys < mongo_total:
            issues.append(
                f"Redis geo index has {seller_keys:,} sellers but MongoDB has "
                f"{mongo_total:,}.\n"
                "  Some sellers are not discoverable. Re-run --sync to re-index."
            )
    except Exception:
        pass

    if not issues:
        console.print(Panel(
            "[green bold]All stores are populated and consistent.[/green bold]",
            border_style="green",
        ))
    else:
        body = "\n\n".join(f"[yellow]•[/yellow] {i}" for i in issues)
        console.print(Panel(body, title="[yellow]Action required[/yellow]",
                            border_style="yellow"))
    console.print()


def main() -> None:
    args = parse_args()
    if args.debug:
        log.setLevel(logging.DEBUG)
    if args.status:
        run_status()
        return
    try:
        run_discovery(args)
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrupted by user. "
            "Sellers inserted so far have been committed.[/yellow]"
        )
        sys.exit(130)


if __name__ == "__main__":
    main()
