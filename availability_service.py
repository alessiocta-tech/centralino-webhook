from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import pymysql
import os


# -----------------------------
# Config
# -----------------------------
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "dbiid6aizvpaqr")


# -----------------------------
# Domain models
# -----------------------------
class MealSlot(str, Enum):
    pranzo = "pranzo"
    cena = "cena"


WEEK_DAYS = [
    "lunedi",
    "martedi",
    "mercoledi",
    "giovedi",
    "venerdi",
    "sabato",
    "domenica",
]


@dataclass
class ServiceAvailability:
    day: str
    slot: MealSlot
    double_turn: bool
    first_turn_covers: int
    second_turn_covers: int | None


class ServiceAvailabilityOut(BaseModel):
    day: str
    slot: MealSlot
    is_double_turn: bool = Field(alias="double_turn")
    first_turn_covers: int
    second_turn_covers: int | None = None

    class Config:
        populate_by_name = True


class RestaurantAvailabilityOut(BaseModel):
    restaurant_id: int
    restaurant_name: str
    coperti_default: int
    uses_calendar: bool
    availability: list[ServiceAvailabilityOut]


class RestaurantSummaryOut(BaseModel):
    restaurant_id: int
    restaurant_name: str
    coperti_default: int
    calendario_raw: str
    prenotazioni: str
    active: str


# -----------------------------
# DB helpers
# -----------------------------
def get_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def fetch_restaurants() -> list[dict[str, Any]]:
    query = """
        SELECT
            ID,
            Nome,
            Coperti,
            Calendario,
            Prenotazioni,
            Attivo
        FROM Esercizi
        ORDER BY ID
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return list(cur.fetchall())


def fetch_restaurant_by_id(restaurant_id: int) -> dict[str, Any] | None:
    query = """
        SELECT
            ID,
            Nome,
            Coperti,
            Calendario,
            Prenotazioni,
            Attivo
        FROM Esercizi
        WHERE ID = %s
        LIMIT 1
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (restaurant_id,))
            return cur.fetchone()


# -----------------------------
# Availability parsing
# -----------------------------
def normalize_calendar_value(value: str | None) -> str:
    return (value or "").strip()


def parse_service_token(token: str, default_covers: int) -> tuple[bool, int, int | None]:
    """
    Rules inferred from the SQL dump:
    - empty calendario => all services use Coperti
    - token like '35' => single turn with 35 covers
    - token like '35|40' => double turn, first=35, second=40
    - malformed or empty token => fallback to default_covers
    """
    token = (token or "").strip()

    if not token:
        return False, int(default_covers), None

    if "|" in token:
        left, right = token.split("|", 1)
        first = int(left.strip()) if left.strip() else int(default_covers)
        second = int(right.strip()) if right.strip() else int(default_covers)
        return True, first, second

    return False, int(token), None


def build_default_week(default_covers: int) -> list[ServiceAvailability]:
    services: list[ServiceAvailability] = []
    for day in WEEK_DAYS:
        for slot in (MealSlot.pranzo, MealSlot.cena):
            services.append(
                ServiceAvailability(
                    day=day,
                    slot=slot,
                    double_turn=False,
                    first_turn_covers=int(default_covers),
                    second_turn_covers=None,
                )
            )
    return services


def parse_calendar(calendar_raw: str, default_covers: int) -> list[ServiceAvailability]:
    calendar_raw = normalize_calendar_value(calendar_raw)
    if not calendar_raw:
        return build_default_week(default_covers)

    tokens = [part.strip() for part in calendar_raw.split(",")]

    expected = 14
    if len(tokens) < expected:
        tokens.extend([""] * (expected - len(tokens)))
    elif len(tokens) > expected:
        tokens = tokens[:expected]

    services: list[ServiceAvailability] = []
    idx = 0
    for day in WEEK_DAYS:
        for slot in (MealSlot.pranzo, MealSlot.cena):
            is_double, first_turn, second_turn = parse_service_token(tokens[idx], default_covers)
            services.append(
                ServiceAvailability(
                    day=day,
                    slot=slot,
                    double_turn=is_double,
                    first_turn_covers=first_turn,
                    second_turn_covers=second_turn,
                )
            )
            idx += 1

    return services


def restaurant_to_availability(row: dict[str, Any]) -> RestaurantAvailabilityOut:
    default_covers = int(row["Coperti"])
    calendar_raw = normalize_calendar_value(row.get("Calendario"))
    services = parse_calendar(calendar_raw, default_covers)

    return RestaurantAvailabilityOut(
        restaurant_id=int(row["ID"]),
        restaurant_name=str(row["Nome"]),
        coperti_default=default_covers,
        uses_calendar=bool(calendar_raw),
        availability=[
            ServiceAvailabilityOut(
                day=s.day,
                slot=s.slot,
                double_turn=s.double_turn,
                first_turn_covers=s.first_turn_covers,
                second_turn_covers=s.second_turn_covers,
            )
            for s in services
        ],
    )


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(
    title="deRione Availability Service",
    version="0.1.0",
    description="Servizio iniziale per leggere i coperti disponibili per giorno/servizio e rilevare il doppio turno dalla tabella Esercizi.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/restaurants", response_model=list[RestaurantSummaryOut])
def list_restaurants() -> list[RestaurantSummaryOut]:
    rows = fetch_restaurants()
    return [
        RestaurantSummaryOut(
            restaurant_id=int(r["ID"]),
            restaurant_name=str(r["Nome"]),
            coperti_default=int(r["Coperti"]),
            calendario_raw=normalize_calendar_value(r.get("Calendario")),
            prenotazioni=str(r.get("Prenotazioni", "")),
            active=str(r.get("Attivo", "")),
        )
        for r in rows
    ]


@app.get("/restaurants/{restaurant_id}/availability", response_model=RestaurantAvailabilityOut)
def get_restaurant_availability(restaurant_id: int) -> RestaurantAvailabilityOut:
    row = fetch_restaurant_by_id(restaurant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Esercizio non trovato")
    return restaurant_to_availability(row)


@app.get("/restaurants/{restaurant_id}/availability/{day}/{slot}", response_model=ServiceAvailabilityOut)
def get_service_availability(
    restaurant_id: int,
    day: str,
    slot: MealSlot,
) -> ServiceAvailabilityOut:
    row = fetch_restaurant_by_id(restaurant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Esercizio non trovato")

    normalized_day = day.strip().lower()
    if normalized_day not in WEEK_DAYS:
        raise HTTPException(status_code=400, detail=f"Giorno non valido: {day}")

    availability = restaurant_to_availability(row)
    for service in availability.availability:
        if service.day == normalized_day and service.slot == slot:
            return service

    raise HTTPException(status_code=404, detail="Servizio non trovato")


@app.get("/double-turns")
def get_all_double_turns(
    restaurant_id: int | None = Query(default=None),
) -> dict[str, Any]:
    rows = fetch_restaurants()
    if restaurant_id is not None:
        rows = [r for r in rows if int(r["ID"]) == restaurant_id]

    result: list[dict[str, Any]] = []
    for row in rows:
        parsed = restaurant_to_availability(row)
        for service in parsed.availability:
            if service.is_double_turn:
                result.append(
                    {
                        "restaurant_id": parsed.restaurant_id,
                        "restaurant_name": parsed.restaurant_name,
                        "day": service.day,
                        "slot": service.slot,
                        "first_turn_covers": service.first_turn_covers,
                        "second_turn_covers": service.second_turn_covers,
                    }
                )

    return {"items": result, "count": len(result)}


# -----------------------------
# Local dev
# -----------------------------
# Run with:
# uvicorn availability_service:app --reload --host 0.0.0.0 --port 8000
