"""Seed a development database so the frontend has something to render.

Development only — it refuses to run against production, and nothing in the
application imports it. Run with:

    python scripts/seed.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta

sys.path.insert(0, ".")

from app.core.config import get_settings
from app.core.security import hash_password
from app.infrastructure.database.client import Collections, connect, disconnect
from app.infrastructure.database.indexes import ensure_indexes
from app.modules.users.models import Role, UserStatus

MODELS = [
    ("BYD", "Atto 3", 2023, "suv", 60.5, 420, 150, 27_000_000, 25_000_000),
    ("BYD", "Dolphin", 2024, "hatchback", 44.9, 340, 70, 21_000_000, 19_500_000),
    ("Nissan", "Leaf", 2021, "hatchback", 40.0, 270, 110, 18_500_000, 17_000_000),
    ("Hyundai", "Kona Electric", 2022, "suv", 64.0, 450, 150, 34_000_000, 31_500_000),
    ("Volkswagen", "ID4", 2023, "suv", 77.0, 520, 150, 46_000_000, 42_500_000),
    ("Toyota", "bZ4X", 2023, "suv", 71.4, 460, 150, 44_000_000, 40_500_000),
    ("Kia", "Niro EV", 2022, "suv", 64.8, 455, 150, 36_000_000, 33_500_000),
    ("MG", "MG4", 2024, "hatchback", 51.0, 350, 125, 24_000_000, 22_000_000),
]

CITIES = [
    ("Kigali", "Gasabo", "kigali"),
    ("Kigali", "Kicukiro", "kigali"),
    ("Musanze", None, "musanze"),
    ("Rubavu", None, "rubavu"),
]


async def main() -> None:
    if get_settings().is_production:
        raise SystemExit("refusing to seed a production database")

    db = await connect()
    await ensure_indexes(db)

    for collection in (
        Collections.VEHICLES,
        Collections.DEALERS,
        Collections.ARTICLES,
        Collections.CHARGING_LOCATIONS,
    ):
        await db[collection].delete_many({})

    now = datetime.now(UTC)

    dealer_id = uuid.uuid4().hex
    await db[Collections.DEALERS].insert_one(
        {
            "_id": dealer_id,
            "slug": "kigali-ev-motors",
            "name": "Kigali EV Motors",
            "verified": True,
            "status": "active",
            "city": "Kigali",
            "address": "KG 7 Ave, Kacyiru, Kigali",
            "description": "Rwanda's first dedicated electric vehicle dealership.",
            "public_contact": True,
            "phone": "+250788000111",
            "whatsapp": "250788000111",
            "established_year": 2021,
            "logo_url": None,
            "cover_image_url": None,
        }
    )

    for index, (make, model, year, body, kwh, rng, kw, price, expected) in enumerate(MODELS):
        city, district, city_slug = CITIES[index % len(CITIES)]
        await db[Collections.VEHICLES].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "slug": f"{make.lower()}-{model.lower().replace(' ', '-')}-{year}-{city_slug}",
                "dealer_id": dealer_id if index % 2 == 0 else None,
                "make": make,
                "make_slug": make.lower(),
                "model": model,
                "variant": None,
                "year": year,
                "condition": "used" if index % 3 else "new",
                "body_type": body,
                "mileage_km": 5_000 + index * 4_200,
                "agency_price": price,
                "currency": "RWF",
                "rental_price_per_day": 85_000 if index % 4 == 0 else None,
                "battery_kwh": kwh,
                "range_km": rng,
                "power_kw": kw,
                "charging": {
                    "ac_kw": 7 if index % 2 else 11,
                    "dc_kw": 88 if index % 3 else None,
                    "port_type": "CCS2",
                    "dc_10_80_minutes": 40 if index % 3 else None,
                },
                "seats": 5,
                "doors": 5,
                "drivetrain": "fwd",
                "location": {"city": city, "district": district, "slug": city_slug},
                "description": (
                    f"A well-kept {year} {make} {model}. Documents verified and battery "
                    "health read from the vehicle's own diagnostics."
                ),
                "features": ["Reverse camera", "Apple CarPlay", "Heat pump", "Fast charging"],
                "images": [],
                "status": "AVAILABLE",
                "verified": index % 3 != 0,
                "purchase_enabled": True,
                "rental_enabled": index % 4 == 0,
                "test_drive_available": True,
                "financing_available": index % 2 == 0,
                "faqs": [
                    {
                        "question": "Is the battery health report available?",
                        "answer": "Yes — ask through the enquiry form and we will send it.",
                    }
                ],
                "internal": {
                    "seller_expected_price": expected,
                    "commission_bps": 700,
                    "internal_notes": "Seller is motivated.",
                    "acquisition_cost": None,
                },
                "version": 0,
                "published_at": now - timedelta(days=index),
                "created_at": now - timedelta(days=index),
                "updated_at": now - timedelta(days=index),
                "deleted_at": None,
            }
        )

    for kind, slug, title in (
        ("guide", "buying-an-ev-in-rwanda", "Buying an EV in Rwanda: the whole process"),
        ("guide", "charging-at-home", "Charging at home on a standard socket"),
        ("blog", "voltaris-launch", "Voltaris is open"),
        ("blog", "rwanda-ev-duty-2026", "What the duty exemption actually saves you"),
    ):
        await db[Collections.ARTICLES].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "slug": slug,
                "kind": kind,
                "status": "published",
                "title": title,
                "excerpt": f"{title}. Written for the Rwandan market.",
                "category": "buying-guides" if kind == "guide" else "news",
                "cover_image": None,
                "author": "Voltaris",
                "reading_minutes": 6,
                "body_html": f"<p>{title}. Full text pending.</p>",
                "faqs": [],
                "related_slugs": [],
                "published_at": now,
                "updated_at": now,
            }
        )

    await db[Collections.CHARGING_LOCATIONS].insert_one(
        {
            "_id": uuid.uuid4().hex,
            "slug": "kigali-heights",
            "name": "Kigali Heights",
            "operator": "Voltaris Partner Network",
            "district": "Gasabo",
            "address": "KG 7 Ave, Kigali",
            # GeoJSON order is [longitude, latitude].
            "location": {"type": "Point", "coordinates": [30.0919, -1.9536]},
            "connectors": [{"type": "CCS2", "power_kw": 60, "count": 2}],
            "access": "public",
            "open_hours": "06:00-22:00",
            "published": True,
            "verified_at": now,
        }
    )

    if not await db[Collections.USERS].find_one({"email": "root@voltaris.rw"}):
        await db[Collections.USERS].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "name": "Root Admin",
                "email": "root@voltaris.rw",
                "phone": "0788000000",
                "password_hash": hash_password("development-only-password"),
                "roles": [Role.SUPER_ADMIN.value],
                "status": UserStatus.ACTIVE.value,
                "email_verified": True,
                "phone_verified": False,
                "mfa_enabled": False,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
        )

    print(f"seeded {len(MODELS)} vehicles, 1 dealer, 4 articles, 1 charging location")
    print("super admin: root@voltaris.rw / development-only-password")
    await disconnect()


if __name__ == "__main__":
    asyncio.run(main())
