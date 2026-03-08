# CLAUDE.md — Centralino Webhook

## Overview

This is a **restaurant table booking automation service** for the Italian restaurant chain **Rione**. It uses FastAPI to expose a webhook API and Playwright to drive a headless Chromium browser to interact with the booking website (`rione.fidy.app`). It handles availability checks and end-to-end form submission for restaurant reservations.

The entire application lives in a single file: `main.py` (≈1400 lines).

---

## Repository Structure

```
centralino-webhook/
├── main.py            # Entire application — API, DB, browser automation
├── requirements.txt   # Python dependencies (FastAPI, uvicorn, playwright)
├── Dockerfile         # Container build using Microsoft Playwright Python image
└── railway.json       # Railway.app deployment configuration
```

There are no test files, no separate modules, and no other source code.

---

## Tech Stack

| Component | Library/Version | Purpose |
|-----------|----------------|---------|
| Web framework | FastAPI 0.110.0 | REST API server |
| ASGI server | uvicorn 0.27.1 | Runs FastAPI |
| Browser automation | playwright 1.41.0 | Headless Chromium for booking form interaction |
| Database | sqlite3 (stdlib) | Persists bookings and customer profiles |
| Deployment | Railway.app | Cloud hosting |

---

## main.py Internal Structure

The file is organized into logical sections (not separate modules):

| Lines | Section | Description |
|-------|---------|-------------|
| 1–115 | Imports & Config | Environment variables, Italian locale constants, FastAPI app init |
| 118–245 | Database Layer | SQLite init, booking log, customer upsert/get |
| 248–331 | Normalization Utils | Time/date/meal/sede string normalization |
| 361–443 | Date Resolution | `/resolve_date` endpoint; Italian relative-date parser |
| 451–509 | Pydantic Model | `RichiestaPrenotazione` — booking request model with validation |
| 516–957 | Playwright Helpers | All browser automation functions (prefixed `_`) |
| 958–1377 | API Routes | Health check, admin dashboard, `/book_table` endpoint |

---

## API Endpoints

### `GET /`
Health check. Returns `{"status": "ok"}`.

### `POST /resolve_date`
Converts Italian relative date expressions to ISO dates.

Input:
```json
{ "testo": "domani sera", "timezone": "Europe/Rome" }
```
Output:
```json
{ "data": "2026-03-09", "giorno": "Lunedì", "tipo": "Domani" }
```

### `GET /time_now`
Returns current time in the configured timezone.

### `POST /book_table`
Main booking endpoint. Operates in two phases controlled by the `fase` field:

**Phase 1 — `"availability"`**: Checks available restaurants for the given date, time, and party size. Returns list of sedi with availability.

**Phase 2 — `"book"`**: Completes the full booking flow for the selected restaurant.

Request body (`RichiestaPrenotazione`):
```json
{
  "fase": "availability" | "book",
  "nome": "Mario",
  "cognome": "Rossi",
  "email": "mario@example.com",
  "telefono": "3331234567",
  "sede": "Talenti",
  "data": "2026-03-10",
  "orario": "20:30",
  "persone": 2,
  "seggiolini": 0,
  "note": ""
}
```

### `GET /_admin/dashboard`
Admin dashboard showing booking stats and customer history. Requires `Authorization: Bearer <ADMIN_TOKEN>` header.

### `GET /_admin/customer/{phone}`
Returns stored customer profile for a given phone number. Requires admin token.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKING_URL` | `rione.fidy.app` | Target booking website hostname |
| `PW_TIMEOUT_MS` | `60000` | General Playwright timeout (ms) |
| `PW_NAV_TIMEOUT_MS` | `60000` | Page navigation timeout (ms) |
| `DISABLE_FINAL_SUBMIT` | `false` | If `true`, skips actual booking submission (test mode) |
| `DEBUG_ECHO_PAYLOAD` | `false` | Log incoming request payload |
| `DEBUG_LOG_AJAX_POST` | `false` | Log outgoing AJAX booking request/response |
| `ADMIN_TOKEN` | `""` | Bearer token required to access admin endpoints |
| `DATA_DIR` | `/tmp` | Directory where SQLite database is stored |
| `MAX_SLOT_RETRIES` | `2` | Max retries if selected time slot is full |
| `MAX_SUBMIT_RETRIES` | `1` | Max retries on final booking submission |
| `RETRY_TIME_WINDOW_MIN` | `90` | Window (minutes) for searching alternative time slots |
| `DEFAULT_EMAIL` | `default@prenotazioni.com` | Fallback email if none provided |

---

## Database Schema

SQLite database stored at `{DATA_DIR}/centralino.sqlite3`.

### `bookings` table
```sql
CREATE TABLE bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,          -- ISO timestamp
  nome TEXT,
  cognome TEXT,
  telefono TEXT,
  sede TEXT,
  data TEXT,
  orario TEXT,
  persone INTEGER,
  status TEXT,      -- "success" | "failed" | "timeout"
  detail TEXT       -- JSON with extra info
);
```

### `customers` table
```sql
CREATE TABLE customers (
  telefono TEXT PRIMARY KEY,
  nome TEXT,
  cognome TEXT,
  email TEXT,
  last_seen TEXT    -- ISO timestamp
);
```

---

## Playwright Automation Flow

### Availability Phase
1. Launch headless Chromium (blocking heavy assets like images/fonts/styles)
2. Navigate to `BOOKING_URL`
3. Dismiss cookie/consent banners (`_maybe_click_cookie`)
4. Wait for `.nCoperti` selector to confirm page is ready
5. Set party size (`_click_persone`) and highchairs (`_set_seggiolini`)
6. Set date (`_set_date`) and meal period (`_click_pasto`)
7. Scrape all sede availability data (`_scrape_sedi_availability`)
8. Return list to caller

### Booking Phase
9. Click selected sede (`_click_sede`)
10. Select meal turn if needed (`_maybe_select_turn`)
11. Select time slot with fallback to closest available (`_select_orario_or_retry`)
12. Fill notes in step 5 (`_fill_note_step5`)
13. Fill personal data form (`_fill_form`)
14. Submit booking and wait for AJAX response (`_wait_ajax_final`)
15. Parse confirmation and return result

---

## Sede (Location) Mapping

The booking system supports these restaurant locations:

| Internal Name | Aliases |
|--------------|---------|
| Talenti | talenti, roma talenti, rione talenti |
| Ostia Lido | ostia, lido, ostia lido |
| Appia | appia, roma appia, rione appia |
| Palermo | palermo |
| Reggio Calabria | reggio, reggio calabria, rc |

Normalization is handled by `_norm_sede()` and matching by `_click_sede()`.

---

## Date & Time Conventions

- All dates are in `YYYY-MM-DD` ISO format.
- All times are `HH:MM` (24-hour).
- Timezone: `Europe/Rome` (falls back to `CET` if `zoneinfo` unavailable).
- The Italian date parser (`/resolve_date`) handles: `oggi`, `domani`, `dopodomani`, `stasera`, `stanotte`, weekday names, `questo weekend`, `prossimo [weekday]`, etc.
- Meal periods: `pranzo` (lunch, before 15:30) / `cena` (dinner, from 17:00 onward) are inferred from `orario` by `_calcola_pasto()`.

---

## Deployment

### Docker
Built from `mcr.microsoft.com/playwright/python:v1.41.0-jammy`. Playwright Chromium and its system dependencies are pre-installed in the base image.

```bash
docker build -t centralino-webhook .
docker run -p 8080:8080 -e ADMIN_TOKEN=secret centralino-webhook
```

### Railway.app
Configured via `railway.json`. Uses NIXPACKS builder with explicit build commands to install Python deps and Playwright Chromium browser.

The app binds to `$PORT` (default 8080) with a single uvicorn worker.

---

## Development Conventions

### Code Style
- All internal helper functions are prefixed with `_` (e.g., `_norm_sede`, `_click_pasto`).
- API route handlers are defined at module level without prefix.
- Pydantic models use `model_validator` and `field_validator` for coercion.
- Italian strings and UI labels are kept as-is (no translation layer).

### Error Handling
- Playwright steps raise exceptions on timeout; these are caught at the route level.
- On error, a screenshot is saved to disk with a timestamped filename.
- All booking attempts (success and failure) are logged to the SQLite database.
- HTTP response codes: `200` (success), `422` (validation error), `500` (booking failure).

### Adding New Functionality
- All new code belongs in `main.py` (the project intentionally uses a single-file structure).
- New helper functions should be prefixed with `_` and placed in the relevant section.
- New API routes should be added at the bottom of the file near the existing routes.
- Environment variables should be read at module level with a sensible default.

### Testing
There are currently no automated tests. When testing manually:
- Set `DISABLE_FINAL_SUBMIT=true` to prevent actual bookings during development.
- Set `DEBUG_ECHO_PAYLOAD=true` and `DEBUG_LOG_AJAX_POST=true` for verbose logging.
- Use the `/resolve_date` and `/time_now` endpoints to test date logic without browser automation.

---

## Known Constraints

- **Single-file architecture**: All logic lives in `main.py`. Do not split into multiple files without explicit instruction.
- **Single worker**: The Railway deployment runs one uvicorn worker. Playwright is not thread-safe; concurrent booking requests may interfere.
- **No tests**: No testing framework is set up. Avoid breaking existing behavior without manual verification.
- **Italian-only UI**: The booking website is in Italian. All field names, labels, and parsing logic assume Italian text.
- **Ephemeral storage**: The SQLite database is stored in `/tmp` by default and will not persist across Railway deployments unless `DATA_DIR` is set to a persistent volume.
