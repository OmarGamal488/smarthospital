# SmartHospital

> A production-grade, AI-assisted clinic management system built with Django 5.2 — three role-aware portals (staff, doctor, patient), an LLM-powered diagnosis pipeline, a tool-using chatbot grounded in real medical APIs, live WebSocket updates, a Telegram booking bot, and an automated email-reminder loop.

[![CI](https://github.com/OmarGamal488/smarthospital/actions/workflows/ci.yml/badge.svg)](https://github.com/OmarGamal488/smarthospital/actions/workflows/ci.yml)
[![Docker](https://github.com/OmarGamal488/smarthospital/actions/workflows/docker.yml/badge.svg)](https://github.com/OmarGamal488/smarthospital/actions/workflows/docker.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Django 5.2](https://img.shields.io/badge/Django-5.2-092E20.svg)](https://www.djangoproject.com/)
[![Channels 4](https://img.shields.io/badge/Channels-4-44B78B.svg)](https://channels.readthedocs.io/)
[![LangChain](https://img.shields.io/badge/LangChain-tool--calling-1c3c3c.svg)](https://www.langchain.com/)
[![DSPy](https://img.shields.io/badge/DSPy-ChainOfThought-7c3aed.svg)](https://dspy.ai/)
[![Tests](https://img.shields.io/badge/tests-40%20passing-success.svg)](#testing)
[![uv](https://img.shields.io/badge/managed_with-uv-7e6cff.svg)](https://docs.astral.sh/uv/)

---

## Demo

<video src="[https://github.com/OmarGamal488/smarthospital/raw/main/docs/smarthospital.webm](https://github.com/user-attachments/assets/be337234-c012-452f-aa9f-f4dbb352fd5b)" controls width="100%"></video>

> Video doesn't play inline? [Download / open it directly →](docs/smarthospital.webm)

---

## What it is

SmartHospital is a complete clinical operations platform. Patients book themselves online (web or Telegram), doctors see today's queue with live ETAs and write structured visit notes, and staff get a full admin console with audit trails, bulk AI-assisted triage, and conversational analytics. Every record change broadcasts over WebSocket so dashboards stay live. Diagnoses run through an LLM pipeline that combines a curated medical knowledge base with three free medical APIs (OpenFDA, MedlinePlus, NIH Clinical Tables) and optional Infermedica for differential diagnosis — all behind retry-and-timeout-hardened service modules.

The codebase is deliberately framework-honest: no microservice sprawl, no React, no GraphQL — a single Django app that does a lot, cleanly. **14 models**, **69 URL routes**, **16 chat tools**, **3 management commands**, **40 passing tests**.

---

## Highlights

### Three role-scoped portals from one app
- **Staff** at `/dashboard/` — cross-clinic ops console: appointments calendar, bulk-analyze, CSV exports, department drill-downs, audit log, AI-flag triage.
- **Doctor** at `/doctor/` — today's queue with live phase pills (in-progress / ETA / overdue), weekly availability + ad-hoc day-off blocks, in-line visit notes & prescriptions.
- **Patient** at `/patient/` — self-register, browse specialties, pick a slot, get instant AI symptom assistance, rate the visit, view the doctor's notes.

Role isolation enforced by purpose-built decorators (`staff_required`, `doctor_required`, `patient_required`) that bounce mis-routed users with friendly toasts instead of bare 403s.

### AI that actually does something useful
- **Auto-diagnosis** on every appointment via DSPy `ChainOfThought` (with a LangChain prompt fallback), persisted as an `AIPrediction` with confidence score and token usage.
- **Tool-using chatbot** with **16 tools** spanning records (`get_patient_summary`, `list_appointments`, `count_records`...) and medical knowledge (`look_up_drug` → OpenFDA, `explain_condition` → MedlinePlus, `differential_diagnosis` → Infermedica). Multi-turn loop with up to 6 tool iterations, every step persisted so chats survive page reloads.
- **Page-aware system prompt** — opening the chatbot from a patient detail page makes it immediately useful: *"The user is viewing patient #12. Call `get_patient_summary(12)` early."* Patients get a privacy-scoped tool subset that can't see other patients' data.
- **AI Complete for symptoms** — patient writes rough text, one click rewrites it into a clean clinical description (no fact-invention prompt).
- **Live header injection** — every chatbot turn gets today's date, weekday, time, current appointment counts, and doctors-off count freshly prepended to the system prompt so the model never hallucinates the date.

### Real-time everything
- **Custom audit log** — `AuditMiddleware` stashes `request.user` on a thread-local; `post_save`/`post_delete` signals on 5 models write `AuditEvent` rows and `channel_layer.group_send()` to the `updates` group.
- **WebSocket consumer** at `/ws/updates/` (Channels + Daphne ASGI) — every authenticated page subscribes and prepends new events to the recent-activity panel without polling.
- **In-memory channel layer** for dev (zero Redis dependency); swap to `channels_redis` for prod with one settings line.

### Patient self-service that respects clinic constraints
- **Slot computation** in pure Python (`hospital/booking.py`) — divides each `DoctorAvailability` weekly window into `slot_minutes` chunks, subtracts existing non-canceled appointments, honors `DoctorTimeOff` blocks.
- **Race-condition-safe booking** — the slot is re-validated against `available_slots(doctor, date)` *inside* the create transaction so two patients tapping the same chip can't double-book.
- **Reschedule + cancel + rate** flows with toast feedback, idempotent guards (`Reminder` unique constraint per `(appointment, kind)`).

### Telegram bot for off-the-website booking
- `python-telegram-bot v22` async `Application` with a 5-state `ConversationHandler` (`/book` → department → doctor → date → slot → reason).
- One-time `link_code` (8-char alphanumeric, no ambiguous chars) pairs a `chat_id` to a Patient — no OAuth dance.
- All ORM calls wrapped in `sync_to_async`. Run with `uv run manage.py runbot`.

### Operational extras
- **`run.sh` / `stop.sh`** — bash launchers with `--no-bot`, `--port`, `--reset-seed`, `--reminders` flags; multiplexed log prefixes (`[web]` / `[bot]`); clean shutdown via INT/TERM/EXIT traps.
- **`send_reminders` management command** — picks Scheduled appointments 18–30h out, dispatches reminders, writes idempotent `Reminder` rows.
- **`seed_demo`** — realistic data generator: 80 patients, 24 doctors with weekly availability, 380 appointments, ~225 AI predictions. `--reset` wipes and re-seeds in seconds.
- **Health probe** at `/healthz/` returns DB + channel-layer status as JSON.

---

## Tech stack

| Layer        | Choice                                                                 |
|--------------|------------------------------------------------------------------------|
| Web          | Django 5.2 · Channels 4 · Daphne (ASGI)                               |
| Realtime     | `InMemoryChannelLayer` (dev) / `RedisChannelLayer` (prod-ready)        |
| LLM agent    | LangChain `bind_tools` (16 tools, multi-turn) → Lightning AI endpoint  |
| LLM structured | DSPy `ChainOfThought` for diagnosis (confidence + reasoning)         |
| Medical KB   | OpenFDA · MedlinePlus Connect · NIH Clinical Tables · Infermedica · 25-entry curated JSON with embedding retrieval |
| Telegram     | `python-telegram-bot` v22 · `ConversationHandler` · async ORM bridge   |
| Frontend     | Vanilla JS · HTMX 1.9 · Chart.js (vendored, no bundler)               |
| Styling      | Hand-rolled design tokens (sage/clay/ai) · dark mode · RTL/Arabic     |
| DB           | SQLite (dev) — Postgres-ready via env vars                            |
| Email        | Console backend (dev) · SMTP via env vars (prod)                      |
| Packaging    | `uv` lockfile · Python 3.11+                                          |

---

## Architecture

```
                              ┌───────────────────────────┐
              ┌──────────────►│  /ws/updates/  Channels   │
              │   live push   │  UpdatesConsumer          │
              │               └──────────▲────────────────┘
              │                          │ group_send
   ┌──────────┴─────────┐    signals     │
   │ Browser (HTMX)     │     ┌──────────┴───────────┐
   │ Vanilla JS + Chart │◄────┤ AuditMiddleware +    │
   └──────────▲─────────┘     │ post_save/post_delete│
              │ HTTP          └──────────▲───────────┘
              │                          │
   ┌──────────┴──────────────────────────┴──────────┐
   │            Django 5.2 (ASGI / Daphne)          │
   │  ┌────────────────────────────────────────┐    │
   │  │ permissions.py  (staff/doctor/patient) │    │
   │  ├────────────────────────────────────────┤    │
   │  │ views.py · views_doctor · views_patient│    │
   │  ├────────────────────────────────────────┤    │
   │  │ booking.py   slot computation          │    │
   │  │ ai_service   predict_diagnosis (DSPy)  │    │
   │  │ chat_service 16-tool LangChain agent   │    │
   │  │ medical_kb   OpenFDA/MedlinePlus/NIH   │    │
   │  │ notifications  email + Telegram        │    │
   │  └────────────────────────────────────────┘    │
   │              models.py (14 models)              │
   └────┬───────────────────┬───────────────┬───────┘
        │ ORM               │ HTTPS         │ Bot API
   ┌────▼─────┐      ┌──────▼──────┐  ┌─────▼───────┐
   │ SQLite   │      │ Lightning AI│  │ Telegram    │
   │ db.sqlite│      │ (LLM)       │  │ users       │
   └──────────┘      └─────────────┘  └─────────────┘
```

---

## Roles at a glance

| Capability               | Staff | Doctor | Patient |
|--------------------------|:-----:|:------:|:-------:|
| Full admin console       |   ✓   |        |         |
| Bulk AI analyze          |   ✓   |        |         |
| CSV exports              |   ✓   |        |         |
| Audit log + activity     |   ✓   |        |         |
| Today's queue + ETA      |   ✓   |   ✓    |         |
| Visit notes & prescription |     |   ✓    |  read   |
| Set weekly availability  |       |   ✓    |         |
| Block specific days off  |       |   ✓    |         |
| Self-register            |       |        |   ✓     |
| Book a slot              |       |        |   ✓     |
| Book via Telegram        |       |        |   ✓     |
| AI symptom rewrite       |       |        |   ✓     |
| Reschedule / cancel      |   ✓   |   ✓    |   ✓     |
| Rate the visit           |       |        |   ✓     |
| Role-scoped chatbot      |   ✓   |   ✓    |   ✓     |

---

## Quickstart

```bash
# 1. Install Python 3.11+ and uv (https://docs.astral.sh/uv/)
git clone https://github.com/OmarGamal488/smarthospital.git
cd smarthospital
uv sync

# 2. Copy the env template and fill in your keys
cp .env.example .env
# edit .env — at minimum set LIGHTNING_API_KEY

# 3. Migrate + seed
uv run manage.py migrate
uv run manage.py seed_demo --reset   # creates 80 patients · 24 doctors · 380 appts

# 4. Create a staff superuser
uv run manage.py createsuperuser

# 5. Run (or use ./run.sh for web + Telegram bot together)
uv run manage.py runserver
```

Then visit:

| URL                     | Purpose                                         |
|-------------------------|-------------------------------------------------|
| `/`                     | Anonymous → `/login/`                           |
| `/welcome/`             | Public marketing landing                        |
| `/services/`            | Public doctor directory by department           |
| `/patient/register/`    | Patient sign-up                                 |
| `/patient/`             | Patient "My care" dashboard                     |
| `/doctor/`              | Doctor today-queue dashboard                    |
| `/dashboard/`           | Staff admin console                             |
| `/admin/`               | Django admin (superuser)                        |
| `/healthz/`             | JSON liveness probe                             |

### Environment variables

| Var                    | Required | Purpose                                            |
|------------------------|:--------:|----------------------------------------------------|
| `LIGHTNING_API_KEY`    |    ✓     | LLM endpoint key (diagnosis + chatbot)             |
| `TELEGRAM_BOT_TOKEN`   |          | Required only when running the Telegram bot       |
| `DJANGO_SECRET_KEY`    |  in prod | Django session signing                             |
| `DJANGO_DEBUG`         |          | `True` (default) / `False` for prod                |
| `DJANGO_ALLOWED_HOSTS` |  in prod | Comma-separated hostnames                          |
| `INFERMEDICA_APP_ID`   |          | Optional differential-diagnosis API                |
| `INFERMEDICA_APP_KEY`  |          | Optional differential-diagnosis API                |
| `LIGHTNING_EMBED_MODEL`|          | Enables embedding retrieval over the medical KB    |

---

## Testing

```bash
uv run manage.py test hospital                            # 40 tests, < 2s
uv run manage.py test hospital.tests.BookingFlowTests
uv run manage.py test hospital.tests.ChatbotTests.test_send_persists_messages
```

Coverage spans:
- Model invariants, signal-driven audit writes, broadcast no-op when channel layer is missing
- Permission boundaries (patient can't reach `/dashboard/`, can't open another patient's record)
- Slot computation under conflicting appointments
- Booking flow end-to-end (POST → Appointment created → email queued → AI prediction created)
- Chatbot (with mocked `ChatOpenAI`) — message persistence, role-scoped tool selection
- Notifications panel + count endpoint for staff / doctor / patient roles
- LLM robustness — `predict_diagnosis` returns `FAILED` cleanly when `LIGHTNING_API_KEY` is unset

---

## Notable implementation details

These are the bits I'm most proud of — each solves a real problem rather than ticking a buzzword.

- **Race-safe booking** — `patient_book_confirm` re-runs `available_slots(doctor, date)` *inside* the same transaction that creates the `Appointment`. Two patients clicking the same chip never collide.
- **Audit thread-local** — Django's signal framework doesn't get `request.user`; I bridge it with a tiny middleware that stashes the user on `threading.local()` and clears it in `finally`. Every model change everywhere — bulk action, shell, admin — gets attributed correctly.
- **Channel-layer graceful degradation** — `_broadcast(event)` is wrapped in `try/except` so unit tests (no ASGI runtime) still pass while production WebSocket pushes work.
- **Idempotent reminders** — `Reminder` has `unique_together = (appointment, kind)`. The `send_reminders` command runs hourly under cron without ever double-sending.
- **Telegram pairing without OAuth** — patient generates an 8-char `link_code` on their profile page, types `/link CODE` in Telegram, the bot atomically claims it. No web-flow, no PKCE, no token storage.
- **Role-scoped LLM tools** — `_tools_for_session(session)` returns one of two tool sets; patient sessions literally cannot call `list_all_patients`. The system prompt also rewrites itself to "you are talking to PATIENT #N, only describe their record".
- **Live system header** — every chat turn prepends *fresh* date/time/today's-counts text so the model never hallucinates "the date is January 2026" three months later.
- **Stay-on-page AI runs** — the "Run analysis" button uses `HTTP_REFERER` (with a safe-redirect check: `startswith('/')` and no newline injection) so the user lands back where they were instead of being thrown to `/appointments/`.
- **Hand-rolled design system** — `colors_and_type.css` defines tokens (`--sage`, `--clay`, `--ai`, `--ink-*`); the `[data-theme="dark"]` block overrides them. Adding a new component takes one token reference and dark mode keeps working. RTL/Arabic toggle is a JS translation dictionary + `[dir="rtl"]` CSS overrides.

---

## Project layout

```
smarthospital/
├── config/                      # Django project (settings, ASGI, URLs)
├── hospital/                    # Single app — all features live here
│   ├── models.py                # 14 models
│   ├── views.py                 # Staff console
│   ├── views_doctor.py          # Doctor portal
│   ├── views_patient.py         # Patient portal
│   ├── permissions.py           # Role decorators
│   ├── booking.py               # Pure-Python slot math
│   ├── ai_service.py            # DSPy ChainOfThought diagnosis
│   ├── chat_service.py          # LangChain 16-tool agent
│   ├── medical_kb.py            # OpenFDA / MedlinePlus / NIH / Infermedica
│   ├── audit.py                 # Middleware + signals + broadcast
│   ├── consumers.py             # WebSocket consumer
│   ├── routing.py               # Channels routes
│   ├── telegram_bot.py          # python-telegram-bot Application
│   ├── notifications.py         # Email + Telegram dispatch
│   ├── management/commands/     # runbot · seed_demo · send_reminders
│   ├── migrations/              # 9 migrations
│   ├── templates/hospital/      # 40+ templates (staff / doctor / patient)
│   └── tests.py                 # 40 tests
├── templates/                   # base.html · 403 · 404 · 500
├── static/                      # CSS tokens + dark mode + Chart.js + HTMX
├── run.sh · stop.sh             # Process lifecycle helpers
└── pyproject.toml · uv.lock     # uv-managed deps
```

---

## Deployment

### Docker

A multi-stage `Dockerfile` builds a non-root, Daphne-served image with `HEALTHCHECK` wired to `/healthz/`. CI publishes it to GHCR on every push to `main`:

```bash
# Pull a pre-built image
docker pull ghcr.io/omargamal488/smarthospital:latest

# Or build locally
docker build -t smarthospital .

# Run
docker run --rm -p 8000:8000 \
  -e DJANGO_SECRET_KEY=change-me \
  -e DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e LIGHTNING_API_KEY=$LIGHTNING_API_KEY \
  smarthospital
```

### Notes

- **ASGI-native** — `daphne` is first in `INSTALLED_APPS`, so `runserver` already serves HTTP + WebSocket. Prod: `daphne -b 0.0.0.0 -p 8000 config.asgi:application` behind nginx.
- **Static files** — `collectstatic` + WhiteNoise (single-binary deploy) or nginx alias. Already wired in the Dockerfile.
- **Multi-worker** — swap `CHANNEL_LAYERS` from `InMemoryChannelLayer` to `channels_redis.core.RedisChannelLayer` (one settings line).
- **Database** — swap `DATABASES` to Postgres; the codebase has no SQLite-specific code.
- **Secrets** — every secret already reads from env (`os.environ.get`), so a `.env` or a Docker secret mount both work.

## CI / CD

Two GitHub Actions workflows live in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| **`ci.yml`** | every push + PR to `main` | `uv sync --frozen` → `manage.py check` → migrate → run all 40 tests |
| **`docker.yml`** | push to `main`, tags, PRs | Buildx + GHA layer cache → build the image → push to `ghcr.io/omargamal488/smarthospital` (skipped on PRs) |

Pull requests get a green check before merge. Tagged releases (`v1.2.3`) produce versioned Docker tags automatically.

---

## Roadmap

- Patient-history view for doctors (read-only timeline across appointments)
- Proper i18n via Django `gettext` (currently a JS dictionary covers EN/AR)
- Postgres + Redis production compose file
- Per-doctor calendar export (ICS feed)
- WhatsApp parity with the Telegram bot

---

## Author

**Omar Gamal ElKady** — Software engineer focused on full-stack web + applied AI.

Built end-to-end as a portfolio piece during the ITI advanced track.

- GitHub: [@OmarGamal488](https://github.com/OmarGamal488)
- Email: omargamal48812@gmail.com
