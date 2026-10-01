# Invoice processing

Two containerised services that turn a photographed invoice into extracted data.

Only the backend talks to Azure. The frontend is a camera UI that hands the
chosen photo over the network, so it holds no credentials of any kind.

```
┌──────────────────────────┐        ┌──────────────────┐
│  frontend/               │        │  Blob Storage    │
│  camera UI + file        │───────▶│  container       │
│  chooser, no Azure       │  post  │  pictures/2026/  │
│  access (FastAPI)        │  photo │                  │
└────────────┬─────────────┘        └────────┬─────────┘
             │                               │ read back the photo
             │                        ┌──────┴──────────┐
             │ polls for results      │  backend/       │
             │◀───────────────────────│  Blob Storage   │
             │  extracted fields      │  + Document     │
             │                        │    Intelligence │
             │                        │  + Table Storage│
             │                        └─────────────────┘
```

| Directory | What it is | Runs as |
| --- | --- | --- |
| [`frontend/`](frontend/README.md) | Camera UI and file chooser. Serves the page and holds no Azure credentials | Docker container |
| [`backend/`](backend/README.md) | Stores the photo, analyses it, and keeps the extracted invoice fields | Docker container |
| `logic-app.json` | The Azure Logic App this replaces. Kept for reference. | Azure resource — **not** a container |

## How the pieces fit

1. The user photographs an invoice, or picks one that already exists on the
   device. `frontend/` captures or previews it and does nothing else.
2. The browser posts the file to `backend/`, which returns an invoice id
   immediately and continues in a background task.
3. `backend/` writes the photo to the `pictures` container with a managed
   identity (or an account key for local testing). It names the blob itself,
   from the photo's content hash.
4. Document Intelligence `prebuilt-invoice` extracts the fields. They are held in
   the backend's memory and **not** written anywhere yet: nothing about a photo
   is worth keeping until someone has looked at it. Confidence scores are
   returned to the browser but not stored, since a score is only useful while
   someone is reading the result.
5. The browser polls `GET /api/invoices/{id}` and renders the fields, with
   per-field confidence so a low-confidence reading is visible at a glance, then
   asks whether the photo was good enough.
6. **The record is created only on "Yes, it is readable"**, which is the single
   path that writes to Table Storage. Answering "No, retake it" deletes the
   result, answers with `204`, and the UI starts a new photo, so a reading
   somebody has rejected is never stored and never left on screen to be accepted
   by mistake.
7. The photo is deleted from blob storage as soon as it has been read, whether
   the analysis succeeded, failed or was rejected.

`ANALYSIS_API_BASE_URL` on the frontend points at step 2 onward. It has no
default: with no backend URL there is nowhere to send a photo, so the UI says so
and refuses to submit.

### Why the backend owns storage

The browser used to upload the photo itself and pass on the resulting blob name.
That put a storage key in the service that serves pages to anyone who can reach
it, and split the request across two services that had to agree on container and
prefix settings. Posting the file to the backend instead means:

- the frontend needs no Azure credentials, SDK or configuration at all;
- the backend decides the blob name, so a caller cannot aim a submission at an
  unrelated blob;
- the two services share no storage settings, only a URL.

### Repeated submissions

The invoice id is `sha256(photo bytes)`, so resubmitting the same photo returns
the same invoice and does not run Document Intelligence again — it bills per
page. The response carries `alreadyProcessed`, and the UI says the photo was
already processed and stops there rather than showing a result that was not
re-derived.

Claiming the photo is one atomic step that happens *before* the blob write, not a
check followed by a later claim. The write is awaited, so doing it in that order
left a window in which a second request for the same photo saw nothing claimed
and queued a second billable analysis.

A photo that previously *failed* is re-analysed, so a retry is never blocked by
the dedupe.

### Why this replaced the Logic App

`logic-app.json` called `Analyze Document` and read `analyzeResult` without
polling. Document Intelligence is a long-running operation: it returns `202`
with an `Operation-Location` header, and the results appear on a later request.
The workflow had no poll and no timeout, so it read a body that was not there
yet. It also wrote a random GUID as both `PartitionKey` and `RowKey`, so a
redelivered event produced a duplicate row that could never be found again.

`backend/` handles the poll through the SDK's LRO poller, and derives the
partition key from the photo's content, so a repeated submission overwrites its
own results.

## frontend/

See [`frontend/README.md`](frontend/README.md) for local development, the test
suite, and Azure Container Apps deployment.

```bash
cd frontend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # point ANALYSIS_API_BASE_URL at the backend
uvicorn app.main:app --reload
```

Container build (the build context is `frontend/`, not the repo root):

```bash
docker build -t invoice-upload ./frontend
```

## backend/

See [`backend/README.md`](backend/README.md) for the API, the storage layout,
the idempotency model, and what it deliberately does not do.

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in the Document Intelligence endpoint
uvicorn app.main:app --reload
```

```bash
docker build -t invoice-analysis ./backend
```

The browser posts to this service from another origin, so set
`CORS_ALLOW_ORIGINS` to the frontend's URL before exposing it. `*` is fine for
local work.

## Running both locally

Two terminals:

```bash
cd backend  && .venv/bin/uvicorn app.main:app --port 3333 --reload
cd frontend && .venv/bin/uvicorn app.main:app --port 8001 --reload
```

Then open `http://localhost:8001`. The analysis service allows all origins by
default; narrow `CORS_ALLOW_ORIGINS` before exposing it anywhere.

## Repository layout

```
.
├── frontend/                 # camera capture UI (containerised)
│   ├── app/                  # FastAPI app and config, no Azure
│   ├── static/               # camera UI and results panel (no build step)
│   ├── tests/                # pytest suite
│   ├── scripts/              # DOM checks
│   ├── Dockerfile
│   └── README.md
├── backend/                  # invoice analysis service (containerised)
│   ├── app/                  # FastAPI app, storage, analyzer, Table Storage
│   ├── tests/                # pytest suite
│   ├── Dockerfile
│   └── README.md
├── logic-app.json            # the Logic App this replaces, kept for reference
└── README.md                 # this file
```

## Testing

```bash
(cd frontend && ../backend/.venv/bin/python -m pytest)  # 58 checks
(cd backend  && .venv/bin/python -m pytest)  # 217 checks
node frontend/scripts/check_ui.js            # 142 DOM checks, needs: npm install jsdom
```

No suite contacts Azure, and none depends on a local `.env`.
