#!/usr/bin/env python3
"""
Padosme — Full Database Seeder
Seeds all PostgreSQL databases and MongoDB with test data
for the full_stack_test.py integration test suite.

Usage:
    python3 seed_databases.py
    python3 seed_databases.py --reset   # drop + recreate all data
"""

import subprocess
import sys
import uuid
import json
from datetime import datetime, timedelta, date

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
POSTGRES_CONTAINER = "padosme-postgres"
POSTGRES_USER      = "padosme"
POSTGRES_PASSWORD  = "Kaas-Labs"
MONGO_CONTAINER    = "padosme-mongodb"
MONGO_URI          = "mongodb://deploy:Kaas-Labs@localhost:27017/?authSource=admin"

RESET = "--reset" in sys.argv

# Fixed UUIDs so repeated runs are idempotent
TEST_USER_ID     = "04c4e0b1-5be7-43ff-b052-7bbd827a2308"   # primary test user (matches real auth user)
TEST_USER_ID_2   = "04c4e0b1-5be7-43ff-b052-7bbd827a2302"
TEST_USER_ID_3   = "04c4e0b1-5be7-43ff-b052-7bbd827a2303"
TEST_USER_ID_4   = "04c4e0b1-5be7-43ff-b052-7bbd827a2304"
TEST_USER_ID_5   = "04c4e0b1-5be7-43ff-b052-7bbd827a2305"

TEST_SELLER_ID   = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
TEST_SELLER_ID_2 = "a1b2c3d4-e5f6-7890-abcd-ef1234567892"
TEST_SELLER_ID_3 = "a1b2c3d4-e5f6-7890-abcd-ef1234567893"
TEST_SELLER_ID_4 = "a1b2c3d4-e5f6-7890-abcd-ef1234567894"
TEST_SELLER_ID_5 = "a1b2c3d4-e5f6-7890-abcd-ef1234567895"

TEST_CATALOG_ID  = "c0ca1000-0000-0000-0000-000000000001"
TEST_ITEM_ID     = "17e00000-0000-0000-0000-000000000001"

TEST_WALLET_ID   = "aa110000-0000-0000-0000-000000000001"
TEST_WALLET_ID_2 = "aa110000-0000-0000-0000-000000000002"
TEST_WALLET_ID_3 = "aa110000-0000-0000-0000-000000000003"
TEST_WALLET_ID_4 = "aa110000-0000-0000-0000-000000000004"
TEST_WALLET_ID_5 = "aa110000-0000-0000-0000-000000000005"

TEST_CAMPAIGN_ID = "ca11eca0-0000-0000-0000-000000000001"

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def run_sql(dbname: str, sql: str) -> str:
    cmd = [
        "docker", "exec", "-e", f"PGPASSWORD={POSTGRES_PASSWORD}",
        POSTGRES_CONTAINER,
        "psql", "-U", POSTGRES_USER, "-d", dbname,
        "-c", sql,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    ignored = ("already exists", "does not exist", "permission denied to create")
    if r.returncode != 0 and not any(s in r.stderr for s in ignored):
        print(f"  [WARN] {dbname}: {r.stderr.strip()[:200]}")
    return r.stdout


def run_sql_file(dbname: str, sql: str):
    """Execute a multi-statement SQL block via stdin."""
    cmd = [
        "docker", "exec", "-i", "-e", f"PGPASSWORD={POSTGRES_PASSWORD}",
        POSTGRES_CONTAINER,
        "psql", "-U", POSTGRES_USER, "-d", dbname,
    ]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        for line in r.stderr.splitlines():
            if "ERROR" in line and "already exists" not in line:
                print(f"  [WARN] {dbname}: {line}")


def create_database(dbname: str):
    run_sql(POSTGRES_USER, f"CREATE DATABASE {dbname};")
    print(f"  DB: {dbname}")


def run_mongo(js: str):
    cmd = [
        "docker", "exec", MONGO_CONTAINER,
        "mongosh", MONGO_URI, "--quiet", "--eval", js,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [WARN] MongoDB: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _run_migration_files(dbname: str, paths: list):
    """Run migration files if they exist; skip silently on deploy where schemas are pre-applied."""
    import os
    for path in paths:
        if os.path.exists(path):
            run_sql_file(dbname, open(path).read())
        # else: schema already applied by the service on deploy — skip


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — create missing databases
# ─────────────────────────────────────────────────────────────────────────────
section("Creating databases")

ALL_DBS = [
    "padosme_auth", "padosme_profiles", "padosme_sellers",
    "padosme_notifications", "rating_db", "channel_service",
    "padosme_coupon", "wallet_service", "subscription_service",
    "payment_service", "analytics_db", "discovery",
    "padosme_ledgers", "padosme_config", "padosme_location",
    "padosme_indexing",
]
for db in ALL_DBS:
    create_database(db)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — schemas
# ─────────────────────────────────────────────────────────────────────────────
section("Applying schemas")

# ── padosme_auth ─────────────────────────────────────────────────────────────
run_sql_file("padosme_auth", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone        VARCHAR(15) NOT NULL UNIQUE,
    country_code VARCHAR(5)  NOT NULL DEFAULT '+91',
    user_type    VARCHAR(20) NOT NULL DEFAULT 'user',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS otp_sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID REFERENCES users(id) ON DELETE CASCADE,
    phone        VARCHAR(15) NOT NULL,
    otp_hash     TEXT        NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    verified     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_otp_phone ON otp_sessions(phone);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT        NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS devices (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'android',
    fcm_token   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, device_id)
);

CREATE TABLE IF NOT EXISTS alternate_ids (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
    alt_type   TEXT NOT NULL,
    alt_value  TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""")
print("  ✓ padosme_auth")

# ── padosme_profiles ──────────────────────────────────────────────────────────
run_sql_file("padosme_profiles", """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS profiles (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id          VARCHAR(15) NOT NULL UNIQUE,
    user_id             UUID        NOT NULL UNIQUE,
    name                VARCHAR(100) NOT NULL DEFAULT '',
    address_line1       VARCHAR(200),
    address_line2       VARCHAR(200),
    city                VARCHAR(100),
    state               VARCHAR(100),
    pincode             VARCHAR(10),
    landmark            VARCHAR(200),
    language_preference VARCHAR(5)  NOT NULL DEFAULT 'en',
    is_seller           BOOLEAN     NOT NULL DEFAULT FALSE,
    is_deleted          BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_profiles_user_id  ON profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_profiles_city      ON profiles(city);

CREATE TABLE IF NOT EXISTS profile_fields (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL,
    field_value TEXT,
    field_type  VARCHAR(50)  NOT NULL DEFAULT 'string',
    visibility  VARCHAR(20)  NOT NULL DEFAULT 'private',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(profile_id, field_name)
);

CREATE TABLE IF NOT EXISTS kyc_records (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID        NOT NULL,
    kyc_type    VARCHAR(50) NOT NULL DEFAULT 'aadhar',
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
    reference   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS config (
    key   VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO config(key, value) VALUES
    ('max_custom_fields','50'),
    ('default_visibility_policy','private')
ON CONFLICT (key) DO NOTHING;
""")
print("  ✓ padosme_profiles")

# ── padosme_sellers ───────────────────────────────────────────────────────────
run_sql_file("padosme_sellers", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS sellers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL UNIQUE,
    business_name VARCHAR(200) NOT NULL DEFAULT '',
    category     VARCHAR(100),
    description  TEXT,
    phone        VARCHAR(15),
    email        VARCHAR(200),
    website      VARCHAR(500),
    latitude     DOUBLE PRECISION,
    longitude    DOUBLE PRECISION,
    address      TEXT,
    city         VARCHAR(100),
    state        VARCHAR(100),
    pincode      VARCHAR(10),
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    is_verified  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sellers_user_id ON sellers(user_id);
CREATE INDEX IF NOT EXISTS idx_sellers_city    ON sellers(city);

CREATE TABLE IF NOT EXISTS business_cards (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id   UUID NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    template    VARCHAR(50) NOT NULL DEFAULT 'default',
    headline    TEXT,
    tagline     TEXT,
    logo_url    TEXT,
    cover_url   TEXT,
    social_links JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS seller_locations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id   UUID NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    address     TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS config (
    key   VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO config(key, value) VALUES
    ('max_business_cards','5'),
    ('verification_required','false')
ON CONFLICT (key) DO NOTHING;
""")
print("  ✓ padosme_sellers")

# ── wallet_service ────────────────────────────────────────────────────────────
run_sql_file("wallet_service", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS wallets (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL UNIQUE,
    balance    BIGINT      NOT NULL DEFAULT 0,
    currency   VARCHAR(10) NOT NULL DEFAULT 'INR',
    status     VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id      UUID        NOT NULL REFERENCES wallets(id),
    type           VARCHAR(20) NOT NULL,
    amount         BIGINT      NOT NULL,
    balance_after  BIGINT      NOT NULL,
    description    TEXT,
    reference_id   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wtx_wallet_id  ON wallet_transactions(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wtx_created_at ON wallet_transactions(created_at DESC);
""")
print("  ✓ wallet_service")

# ── payment_service ───────────────────────────────────────────────────────────
run_sql_file("payment_service", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS payment_intents (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID        NOT NULL,
    amount           BIGINT      NOT NULL,
    currency         VARCHAR(10) NOT NULL DEFAULT 'INR',
    gateway          VARCHAR(50) NOT NULL DEFAULT 'razorpay',
    gateway_order_id VARCHAR(255),
    status           VARCHAR(50) NOT NULL DEFAULT 'pending',
    idempotency_key  VARCHAR(255) UNIQUE,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pi_user_id    ON payment_intents(user_id);
CREATE INDEX IF NOT EXISTS idx_pi_status     ON payment_intents(status);

CREATE TABLE IF NOT EXISTS payments (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id          UUID REFERENCES payment_intents(id),
    user_id            UUID        NOT NULL,
    amount             BIGINT      NOT NULL,
    currency           VARCHAR(10) NOT NULL DEFAULT 'INR',
    gateway            VARCHAR(50) NOT NULL,
    gateway_payment_id VARCHAR(255),
    status             VARCHAR(50) NOT NULL DEFAULT 'pending',
    gateway_response   JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payments_user_id   ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_intent_id ON payments(intent_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gateway     VARCHAR(50) NOT NULL,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    processed   BOOLEAN     NOT NULL DEFAULT FALSE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""")
print("  ✓ payment_service")

# ── padosme_coupon ────────────────────────────────────────────────────────────
run_sql_file("padosme_coupon", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS campaigns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    seller_id       UUID,
    discount_type   VARCHAR(20)  NOT NULL DEFAULT 'percent',
    discount_value  NUMERIC(10,2) NOT NULL DEFAULT 0,
    max_coupons     INT          NOT NULL DEFAULT 100,
    used_count      INT          NOT NULL DEFAULT 0,
    valid_from      TIMESTAMPTZ,
    valid_until     TIMESTAMPTZ,
    status          VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_by      UUID,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS coupons (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        VARCHAR(50)  NOT NULL UNIQUE,
    campaign_id UUID         NOT NULL REFERENCES campaigns(id),
    seller_id   UUID,
    user_id     UUID,
    salesman_id UUID,
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',
    redeemed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_coupons_campaign ON coupons(campaign_id);
CREATE INDEX IF NOT EXISTS idx_coupons_code     ON coupons(code);

CREATE TABLE IF NOT EXISTS salesman_stats (
    salesman_id    UUID        NOT NULL,
    campaign_id    UUID        NOT NULL REFERENCES campaigns(id),
    coupons_issued INT         NOT NULL DEFAULT 0,
    coupons_used   INT         NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (salesman_id, campaign_id)
);
""")
print("  ✓ padosme_coupon")

# ── subscription_service ──────────────────────────────────────────────────────
run_sql_file("subscription_service", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS subscription_packages (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(100) NOT NULL UNIQUE,
    description  TEXT,
    price        BIGINT       NOT NULL DEFAULT 0,
    currency     VARCHAR(10)  NOT NULL DEFAULT 'INR',
    duration_days INT         NOT NULL DEFAULT 30,
    features     JSONB        NOT NULL DEFAULT '[]',
    max_products INT          NOT NULL DEFAULT 10,
    max_images   INT          NOT NULL DEFAULT 5,
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id    UUID        NOT NULL UNIQUE,
    credits      BIGINT      NOT NULL DEFAULT 0,
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_history (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id   UUID        NOT NULL REFERENCES wallets(id),
    action      VARCHAR(50) NOT NULL,
    amount      BIGINT      NOT NULL,
    balance_after BIGINT    NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id   UUID NOT NULL,
    package_id  UUID NOT NULL REFERENCES subscription_packages(id),
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',
    started_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_subs_seller_id ON subscriptions(seller_id);
""")
print("  ✓ subscription_service")

# ── padosme_notifications ─────────────────────────────────────────────────────
run_sql_file("padosme_notifications", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS device_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL,
    token       TEXT        NOT NULL,
    platform    VARCHAR(20) NOT NULL DEFAULT 'android',
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, token)
);
CREATE INDEX IF NOT EXISTS idx_dt_user_id ON device_tokens(user_id);

CREATE TABLE IF NOT EXISTS notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL,
    title       TEXT        NOT NULL,
    body        TEXT        NOT NULL,
    data        JSONB,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
    sent_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""")
print("  ✓ padosme_notifications")

# ── channel_service ───────────────────────────────────────────────────────────
run_sql_file("channel_service", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS channels (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id   UUID        NOT NULL UNIQUE,
    name        TEXT        NOT NULL,
    description TEXT,
    subscriber_count INT    NOT NULL DEFAULT 0,
    post_count  INT         NOT NULL DEFAULT 0,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS posts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id  UUID        NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    seller_id   UUID        NOT NULL,
    content     TEXT        NOT NULL,
    media_urls  JSONB       NOT NULL DEFAULT '[]',
    post_type   VARCHAR(20) NOT NULL DEFAULT 'text',
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    like_count  INT         NOT NULL DEFAULT 0,
    view_count  INT         NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_posts_channel_id ON posts(channel_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id  UUID NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(channel_id, user_id)
);

CREATE TABLE IF NOT EXISTS post_flags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id     UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    reason      TEXT NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS config (
    key   VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO config(key,value) VALUES ('max_post_length','5000') ON CONFLICT DO NOTHING;
""")
print("  ✓ channel_service")

# ── padosme_ledgers ───────────────────────────────────────────────────────────
run_sql_file("padosme_ledgers", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS ledger_actions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    credit_cost BIGINT       NOT NULL DEFAULT 0,
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL,
    action_id   UUID        NOT NULL REFERENCES ledger_actions(id),
    amount      BIGINT      NOT NULL,
    balance_after BIGINT    NOT NULL DEFAULT 0,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger_entries(user_id);

CREATE TABLE IF NOT EXISTS ledger_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    processed   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""")
print("  ✓ padosme_ledgers")

# ── padosme_config ────────────────────────────────────────────────────────────
run_sql_file("padosme_config", """
CREATE TABLE IF NOT EXISTS configs (
    key         VARCHAR(200) PRIMARY KEY,
    value       TEXT         NOT NULL,
    service     VARCHAR(100) NOT NULL DEFAULT 'global',
    description TEXT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feature_flags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL UNIQUE,
    enabled     BOOLEAN      NOT NULL DEFAULT FALSE,
    description TEXT,
    rollout_pct INT          NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
""")
print("  ✓ padosme_config")

# ── padosme_location ──────────────────────────────────────────────────────────
run_sql_file("padosme_location", """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS locations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id   UUID        NOT NULL,
    entity_type VARCHAR(50) NOT NULL DEFAULT 'seller',
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    address     TEXT,
    city        VARCHAR(100),
    state       VARCHAR(100),
    pincode     VARCHAR(10),
    h3_cell     TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(entity_id, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_loc_entity ON locations(entity_id, entity_type);
""")
print("  ✓ padosme_location")

# ── padosme_indexing ──────────────────────────────────────────────────────────
_run_migration_files("padosme_indexing", [
    "/home/tameem/github/padosme-indexing-service/migrations/schema.sql",
])
print("  ✓ padosme_indexing")

# ── analytics_db ──────────────────────────────────────────────────────────────
_run_migration_files("analytics_db", [
    "/home/tameem/github/padosme-analytics-service/migrations/001_create_tables.up.sql",
    "/home/tameem/github/padosme-analytics-service/migrations/003_seller_profile_views_timeseries.up.sql",
    "/home/tameem/github/padosme-analytics-service/migrations/004_seller_dashboard_metrics.up.sql",
])
print("  ✓ analytics_db")

# ── rating_db ─────────────────────────────────────────────────────────────────
_run_migration_files("rating_db", [
    "/home/tameem/github/padosme-rating-service/migrations/001_create_tables.up.sql",
    "/home/tameem/github/padosme-rating-service/migrations/002_fix_duplicate_index.up.sql",
    "/home/tameem/github/padosme-rating-service/migrations/003_add_product_ratings.up.sql",
])
print("  ✓ rating_db")

# ── discovery (minimal) ───────────────────────────────────────────────────────
run_sql_file("discovery", """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'
);
INSERT INTO config(key,value) VALUES
    ('search_radius_km','10'),
    ('max_results','50')
ON CONFLICT (key) DO NOTHING;
""")
print("  ✓ discovery")


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — seed test data
# ─────────────────────────────────────────────────────────────────────────────
section("Seeding test data")

# Always truncate seed tables so repeated runs give exactly N rows
for db, tables in [
    ("padosme_auth",        ["otp_sessions", "users"]),
    ("padosme_profiles",    ["profiles"]),
    ("padosme_sellers",     ["sellers"]),
    ("wallet_service",      ["wallet_transactions", "wallets"]),
    ("payment_service",     ["payments", "payment_intents"]),
    ("padosme_coupon",      ["coupon_redemptions", "campaigns"]),
    ("subscription_service",["subscription_packages"]),
    ("padosme_ledgers",     ["ledger_transactions", "ledger_accounts", "ledger_actions"]),
    ("padosme_config",      ["feature_flags", "configs"]),
    ("padosme_indexing",    ["indexed_sellers"]),
    ("analytics_db",        ["search_metrics", "seller_metrics", "platform_metrics"]),
]:
    for tbl in tables:
        run_sql(db, f"TRUNCATE TABLE {tbl} CASCADE;")

uid  = TEST_USER_ID
sid  = TEST_SELLER_ID
wid  = TEST_WALLET_ID
cid  = TEST_CAMPAIGN_ID

# Short aliases for extra users/sellers/wallets
u2, u3, u4, u5 = TEST_USER_ID_2, TEST_USER_ID_3, TEST_USER_ID_4, TEST_USER_ID_5
s2, s3, s4, s5 = TEST_SELLER_ID_2, TEST_SELLER_ID_3, TEST_SELLER_ID_4, TEST_SELLER_ID_5
w2, w3, w4, w5 = TEST_WALLET_ID_2, TEST_WALLET_ID_3, TEST_WALLET_ID_4, TEST_WALLET_ID_5
now  = datetime.utcnow().isoformat()
exp  = (datetime.utcnow() + timedelta(days=30)).isoformat()

# ── users ─────────────────────────────────────────────────────────────────────
run_sql_file("padosme_auth", f"""
INSERT INTO users(id, phone, country_code, user_type, created_at, updated_at)
VALUES
  ('{uid}', '9089125818', '+91', 'seller', NOW(), NOW()),
  ('{u2}',  '9089125819', '+91', 'seller', NOW(), NOW()),
  ('{u3}',  '9000000001', '+91', 'user',   NOW(), NOW()),
  ('{u4}',  '9000000002', '+91', 'user',   NOW(), NOW()),
  ('{u5}',  '9000000003', '+91', 'user',   NOW(), NOW())
ON CONFLICT (phone) DO NOTHING;

INSERT INTO otp_sessions(user_id, phone, otp_hash, expires_at, verified)
VALUES
  ('{uid}', '9089125818', 'hashed_otp_1', NOW() + INTERVAL '10 minutes', true),
  ('{u2}',  '9089125819', 'hashed_otp_2', NOW() + INTERVAL '10 minutes', true),
  ('{u3}',  '9000000001', 'hashed_otp_3', NOW() + INTERVAL '10 minutes', false),
  ('{u4}',  '9000000002', 'hashed_otp_4', NOW() + INTERVAL '10 minutes', false),
  ('{u5}',  '9000000003', 'hashed_otp_5', NOW() + INTERVAL '10 minutes', false)
ON CONFLICT DO NOTHING;
""")
print("  ✓ users + otp_sessions")

# ── profiles ──────────────────────────────────────────────────────────────────
run_sql_file("padosme_profiles", f"""
INSERT INTO profiles(id, profile_id, user_id, name, city, state, is_seller)
VALUES
  (gen_random_uuid(), 'PADOS-TEST001', '{uid}', 'Arjun Sharma',    'Mumbai',   'Maharashtra', true),
  (gen_random_uuid(), 'PADOS-TEST002', '{u2}',  'Priya Mehta',     'Delhi',    'Delhi',       true),
  (gen_random_uuid(), 'PADOS-TEST003', '{u3}',  'Ravi Kumar',      'Bengaluru','Karnataka',   false),
  (gen_random_uuid(), 'PADOS-TEST004', '{u4}',  'Sneha Patel',     'Ahmedabad','Gujarat',     false),
  (gen_random_uuid(), 'PADOS-TEST005', '{u5}',  'Vikram Nair',     'Chennai',  'Tamil Nadu',  false)
ON CONFLICT (user_id) DO NOTHING;
""")
print("  ✓ profiles")

# ── sellers ───────────────────────────────────────────────────────────────────
run_sql_file("padosme_sellers", f"""
INSERT INTO sellers(id, user_id, business_name, category, city, state, latitude, longitude, status, is_verified)
VALUES
  ('{sid}', '{uid}', 'Arjun Electronics Hub',  'Electronics', 'Mumbai',   'Maharashtra', 19.0760, 72.8777, 'active',   true),
  ('{s2}',  '{u2}',  'Priya Fashion Store',     'Clothing',    'Delhi',    'Delhi',       28.6139, 77.2090, 'active',   true),
  ('{s3}',  '{u3}',  'Ravi Books & Stationery', 'Books',       'Bengaluru','Karnataka',   12.9716, 77.5946, 'active',   false),
  ('{s4}',  '{u4}',  'Sneha Groceries',         'Food & Grocery','Ahmedabad','Gujarat',   23.0225, 72.5714, 'pending',  false),
  ('{s5}',  '{u5}',  'Vikram Auto Parts',       'Automotive',  'Chennai',  'Tamil Nadu',  13.0827, 80.2707, 'active',   true)
ON CONFLICT (user_id) DO NOTHING;
""")
print("  ✓ sellers")

# ── wallets ───────────────────────────────────────────────────────────────────
run_sql_file("wallet_service", f"""
INSERT INTO wallets(id, user_id, balance, currency)
VALUES
  ('{wid}', '{uid}', 50000, 'INR'),
  ('{w2}',  '{u2}',  35000, 'INR'),
  ('{w3}',  '{u3}',  15000, 'INR'),
  ('{w4}',  '{u4}',  10000, 'INR'),
  ('{w5}',  '{u5}',  20000, 'INR')
ON CONFLICT (user_id) DO NOTHING;
""")
print("  ✓ wallets")

# ── subscription packages ─────────────────────────────────────────────────────
run_sql_file("subscription_service", f"""
INSERT INTO subscription_packages(id, name, description, price, duration_days, features, max_products, max_images, is_active)
VALUES
  (gen_random_uuid(), 'Free',       'Free tier — basic listing',             0,    30, '["Basic listing","1 catalog"]',                          10,   5, true),
  (gen_random_uuid(), 'Starter',    'Starter plan for new sellers',       19900,    30, '["5 products","Basic analytics"]',                       25,   8, true),
  (gen_random_uuid(), 'Basic',      'Basic plan for small sellers',       49900,    30, '["10 products","Analytics","Priority listing"]',          50,  10, true),
  (gen_random_uuid(), 'Pro',        'Pro plan for growing businesses',   149900,    30, '["100 products","Advanced analytics","Featured badge"]', 100,  20, true),
  (gen_random_uuid(), 'Enterprise', 'Unlimited plan for large sellers',  499900,    30, '["Unlimited products","Dedicated support","API access"]', 1000,50, true)
ON CONFLICT (name) DO NOTHING;

INSERT INTO wallets(id, seller_id, credits)
VALUES (gen_random_uuid(), '{sid}', 1000)
ON CONFLICT (seller_id) DO NOTHING;
""")
print("  ✓ subscription_packages")

# ── coupon campaigns ──────────────────────────────────────────────────────────
campaigns = [
    ("Summer Sale 2026", "Summer discount campaign", "percent", 10, 500),
    ("Welcome Offer",    "New user welcome coupon",  "percent", 15, 200),
    ("Diwali Special",   "Festival season offer",    "percent", 20, 1000),
    ("Flash Sale",       "24 hour flash sale",       "flat",    50, 100),
    ("Loyalty Reward",   "Loyal customer reward",    "percent", 5,  300),
]
campaign_values = ",\n  ".join(
    f"(gen_random_uuid(), '{name}', '{desc}', '{sid}', '{dtype}', {dval}, {mx}, 0, NOW(), NOW() + INTERVAL '30 days', 'active', '{uid}')"
    for name, desc, dtype, dval, mx in campaigns
)
run_sql_file("padosme_coupon", f"""
INSERT INTO campaigns(id, name, description, seller_id, discount_type, discount_value, max_coupons, used_count, valid_from, valid_until, status, created_by)
VALUES
  {campaign_values}
ON CONFLICT DO NOTHING;
""")
print("  ✓ coupon campaigns")

# ── payment seed data ─────────────────────────────────────────────────────────
run_sql_file("payment_service", f"""
INSERT INTO payment_intents(user_id, amount, currency, gateway, status, idempotency_key)
VALUES
  ('{uid}', 99900, 'INR', 'razorpay', 'pending',   'seed-intent-001'),
  ('{u2}',  49900, 'INR', 'stripe',   'succeeded', 'seed-intent-002'),
  ('{u3}',  19900, 'INR', 'razorpay', 'succeeded', 'seed-intent-003'),
  ('{u4}',  14990, 'INR', 'razorpay', 'failed',    'seed-intent-004'),
  ('{u5}', 149900, 'INR', 'stripe',   'pending',   'seed-intent-005')
ON CONFLICT (idempotency_key) DO NOTHING;
""")
print("  ✓ payment_intents")

# ── ledger actions ────────────────────────────────────────────────────────────
run_sql_file("padosme_ledgers", f"""
INSERT INTO ledger_actions(id, name, description, credit_cost, is_active)
VALUES
  (gen_random_uuid(), 'profile_view',    'Charge for profile view',        1,  true),
  (gen_random_uuid(), 'search_click',    'Charge for search click',        2,  true),
  (gen_random_uuid(), 'lead_generation', 'Charge for lead generation',    10,  true),
  (gen_random_uuid(), 'featured_listing','Charge for featured listing',   50,  true),
  (gen_random_uuid(), 'direct_message',  'Charge for direct message lead',  5,  true)
ON CONFLICT (name) DO NOTHING;
""")
print("  ✓ ledger_actions")

# ── config service seed ───────────────────────────────────────────────────────
run_sql_file("padosme_config", """
INSERT INTO configs(key, value, service, description) VALUES
  ('max_otp_attempts',    '5',     'auth',         'Max OTP verification attempts'),
  ('otp_expiry_minutes',  '10',    'auth',         'OTP expiry in minutes'),
  ('max_catalog_items',   '100',   'catalogue',    'Max items per catalog'),
  ('search_radius_km',    '10',    'discovery',    'Default search radius'),
  ('credits_per_view',    '1',     'ledger',       'Credits charged per profile view'),
  ('credits_per_lead',    '10',    'ledger',       'Credits charged per lead'),
  ('sms_provider',        'twilio','sms',          'Active SMS gateway'),
  ('payment_gateway',     'razorpay','payment',    'Active payment gateway')
ON CONFLICT (key) DO NOTHING;

INSERT INTO feature_flags(name, enabled, description, rollout_pct) VALUES
  ('new_search_ui',       false, 'New search interface rollout',    0),
  ('ai_recommendations',  false, 'AI product recommendations',      0),
  ('video_posts',         true,  'Allow video posts in channels',   100),
  ('wallet_top_up',       true,  'Wallet top-up via payment',       100)
ON CONFLICT (name) DO NOTHING;
""")
print("  ✓ configs + feature_flags")

# ── indexing seed ─────────────────────────────────────────────────────────────
run_sql_file("padosme_indexing", f"""
INSERT INTO indexed_sellers(seller_id, name, latitude, longitude, h3_cell, available, status)
VALUES
  ('{sid}', 'Arjun Electronics Hub',  19.0760, 72.8777, '8a1f2345abc0fff', true, 'active'),
  ('{s2}',  'Priya Fashion Store',    28.6139, 77.2090, '8a1f2345abc1fff', true, 'active'),
  ('{s3}',  'Ravi Books & Stationery',12.9716, 77.5946, '8a1f2345abc2fff', true, 'active'),
  ('{s4}',  'Sneha Groceries',        23.0225, 72.5714, '8a1f2345abc3fff', false,'pending'),
  ('{s5}',  'Vikram Auto Parts',      13.0827, 80.2707, '8a1f2345abc4fff', true, 'active')
ON CONFLICT (seller_id) DO NOTHING;
""")
print("  ✓ indexed_sellers")

# ── analytics seed ────────────────────────────────────────────────────────────
today = date.today().isoformat()
run_sql_file("analytics_db", f"""
INSERT INTO seller_metrics(seller_id, profile_views, search_impressions, search_clicks, followers, total_reviews)
VALUES
  ('{sid}', 120, 450, 38, 15, 5)
ON CONFLICT (seller_id) DO UPDATE SET
  profile_views = EXCLUDED.profile_views,
  updated_at    = NOW();

INSERT INTO platform_metrics(date, total_users, active_users, total_sellers, active_sellers, new_users, new_sellers)
VALUES
  ('{today}'::date, 1250, 340, 185, 120, 12, 3)
ON CONFLICT (date) DO NOTHING;

INSERT INTO search_metrics(keyword, search_count, click_count, has_results, date)
VALUES
  ('electronics Mumbai', 45, 18, true,  '{today}'::date),
  ('clothing store',     33, 12, true,  '{today}'::date),
  ('grocery near me',    28,  9, true,  '{today}'::date),
  ('auto parts Chennai', 17,  6, true,  '{today}'::date),
  ('books online',       52, 21, true,  '{today}'::date)
ON CONFLICT (keyword, date) DO NOTHING;
""")
print("  ✓ analytics seed data")


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — MongoDB seed
# ─────────────────────────────────────────────────────────────────────────────
section("Seeding MongoDB (catalog_db)")

catalog_id = TEST_CATALOG_ID
item_id    = TEST_ITEM_ID
seller_id  = TEST_SELLER_ID

run_mongo(f"""
var db2 = db.getSiblingDB('catalog_db');

// Clean stale data from previous runs
db2.catalogs.deleteMany({{}});
db2.items.deleteMany({{}});

// Runtime config
db2.config.updateOne(
  {{_id: 'allowed_categories'}},
  {{$set: {{value: 'Electronics,Clothing,Books,Sports,Home & Kitchen,Beauty & Personal Care,Food & Grocery,Toys & Baby,Automotive,Health & Wellness'}}}},
  {{upsert: true}}
);
db2.config.updateOne(
  {{_id: 'max_items_per_seller'}},
  {{$set: {{value: '100'}}}},
  {{upsert: true}}
);

// Seed 5 catalogs
[
  ['{catalog_id}',        '{seller_id}',    'Electronics Catalog',    'Electronics'],
  ['c0ca1000-0000-0000-0000-000000000002', '{s2}', 'Fashion Catalog', 'Clothing'],
  ['c0ca1000-0000-0000-0000-000000000003', '{s3}', 'Books Catalog',   'Books'],
  ['c0ca1000-0000-0000-0000-000000000004', '{s4}', 'Grocery Catalog', 'Food & Grocery'],
  ['c0ca1000-0000-0000-0000-000000000005', '{s5}', 'Auto Parts',      'Automotive'],
].forEach(function(row) {{
  db2.catalogs.updateOne(
    {{_id: row[0]}},
    {{$set: {{seller_id: row[1], name: row[2], description: 'Seeded catalog', type: 'product',
             visibility: 'public', is_active: true, created_at: new Date(), updated_at: new Date()}}}},
    {{upsert: true}}
  );
}});

// Seed 5 items
[
  ['{item_id}',              '{catalog_id}', '{seller_id}', 'Wireless Headphones', 'Electronics', 199900, 50],
  ['17e00000-0000-0000-0000-000000000002', 'c0ca1000-0000-0000-0000-000000000002', '{s2}', 'Cotton Kurta',  'Clothing',    89900, 100],
  ['17e00000-0000-0000-0000-000000000003', 'c0ca1000-0000-0000-0000-000000000003', '{s3}', 'Python Programming Book', 'Books', 59900, 30],
  ['17e00000-0000-0000-0000-000000000004', 'c0ca1000-0000-0000-0000-000000000004', '{s4}', 'Basmati Rice 5kg', 'Food & Grocery', 34900, 200],
  ['17e00000-0000-0000-0000-000000000005', 'c0ca1000-0000-0000-0000-000000000005', '{s5}', 'Car Phone Mount', 'Automotive',  14900, 75],
].forEach(function(row) {{
  db2.items.updateOne(
    {{_id: row[0]}},
    {{$set: {{catalog_id: row[1], seller_id: row[2], name: row[3], description: 'Seeded item',
             type: 'product', price: NumberLong(row[6]), image_url: 'https://example.com/item.jpg',
             image_urls: [], category: row[4], status: 'active', quantity: row[6],
             created_at: new Date(), updated_at: new Date()}}}},
    {{upsert: true}}
  );
}});

// Summary
print('catalogs: ' + db2.catalogs.countDocuments({{}}));
print('items: '    + db2.items.countDocuments({{}}));
print('config: '   + db2.config.countDocuments({{}}));
""")
print("  ✓ MongoDB catalog_db")


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 5 — verify
# ─────────────────────────────────────────────────────────────────────────────
section("Verification")

checks = [
    ("padosme_auth",        "SELECT 'users='||COUNT(*) FROM users; SELECT 'otp_sessions='||COUNT(*) FROM otp_sessions;"),
    ("wallet_service",      "SELECT 'wallets='||COUNT(*) FROM wallets;"),
    ("payment_service",     "SELECT 'payment_intents='||COUNT(*) FROM payment_intents; SELECT 'payments='||COUNT(*) FROM payments;"),
    ("padosme_coupon",      "SELECT 'campaigns='||COUNT(*) FROM campaigns;"),
    ("subscription_service","SELECT 'packages='||COUNT(*) FROM subscription_packages;"),
    ("padosme_config",      "SELECT 'configs='||COUNT(*) FROM configs; SELECT 'flags='||COUNT(*) FROM feature_flags;"),
    ("padosme_ledgers",     "SELECT 'actions='||COUNT(*) FROM ledger_actions;"),
]
for db, sql in checks:
    result = run_sql(db, sql).strip()
    rows = [l.strip() for l in result.splitlines() if "=" in l]
    print(f"  {db}: {' | '.join(rows)}")

print("\n✅  All databases seeded successfully.\n")
print("MongoDB check (run this on deploy server if auth fails):")
print("  docker exec padosme-mongodb mongosh 'mongodb://deploy:Kaas-Labs@localhost:27017/?authSource=admin' --quiet --eval \"db.getSiblingDB('catalog_db').getCollectionNames().forEach(c => print(c+': '+db.getSiblingDB('catalog_db')[c].countDocuments({})))\"")
