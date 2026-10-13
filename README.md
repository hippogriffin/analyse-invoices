> [!AI-GENERATED CODE DISCLAIMER]
>
> Most of the code and documentation in this project has been generated using OpenCode.
>
> Many people are uncomfortable using applications that have been created using AI and I want to make this clear from the outset.
>
> No AI is used within the application itself as per the architecture diagrams.
>
> I am a DevOps engineer by trade but not a python expert. Best efforts have been made to check the code for bad practices but some may exist.
>
> This is a personal passion project that I wouldn't have got up and running easily without AI assistance.
>
> The code has been human reviewed and checks have been added to scan for vulnerabilities automatically.

# Process invoices using Azure Document Intelligence

Two containerised services that turn a photographed invoice into extracted data.

The solution is composed of a python frontend that can take photos using the device camera or accept file uploads.

The frontend connects to a FastAPI backend which connects to Azure Document Intelligence to perform the document analysis.

The results of the analysis are returned to the frontend for the user to confirm and edit as needed before submission.

The submitted values are stored in an Azure table.

```
┌──────────────┐ post/poll ┌──────────────────┐  forward  ┌──────────────────┐
│  browser     │           │  frontend/       │──────────▶│  backend/        │
│  camera UI   │──────────▶│  UI + reverse    │  photo    │  Blob Storage    │
│              │◀──────────│  proxy, no Azure │◀──────────│  + Document      │
└──────────────┘  fields   │  (FastAPI)       │  fields   │    Intelligence  │
                           └──────────────────┘           │  + Table Storage │
                                                          └────────┬─────────┘
                                                                   │
                                                          ┌────────▼─────────┐
                                                          │  Blob Storage    │
                                                          │  container       │
                                                          └──────────────────┘
```

| Directory | What it is | Runs as |
| --- | --- | --- |
| [`frontend/`](frontend/README.md) | Camera UI and file chooser, installable as an app (PWA). Serves the page, forwards API calls to the backend, and holds no Azure credentials | Docker container |
| [`backend/`](backend/README.md) | Stores the photo, analyses it, and keeps the extracted invoice fields | Docker container |
| `logic-app.json` | The Azure Logic App this replaces. Kept for reference. | Azure resource — **not** a container |

## How the pieces fit

1. The user photographs an invoice, or picks one that already exists on the
   device. `frontend/` captures or previews it and does nothing else. While the
   preview is live a **Zoom** slider (1×–3×) enlarges the frame — a digital
   centre crop, so the captured photo keeps the full resolution of just that
   part — and a captured or chosen photo can be trimmed with **Crop** before it
   is sent. The page is installable to a phone's home screen (a web app
   manifest, icons and a service worker) and opens offline.
2. The browser posts the file to `frontend/`, which forwards it to `backend/`.
   The backend returns an invoice id immediately and continues in a background
   task.
3. `backend/` writes the photo to the `pictures` container with a managed
   identity (or an account key for local testing). It names the blob itself,
   from the photo's content hash.
4. Document Intelligence `prebuilt-invoice` extracts the fields. They are held in
   the backend's memory and **not** written anywhere yet: nothing about a photo
   is worth keeping until someone has looked at it. Confidence scores are
   returned to the browser but not stored, since a score is only useful while
   someone is reading the result.
5. The browser polls `GET /api/invoices/{id}` (through the frontend proxy) and
   renders the fields, with per-field confidence so a low-confidence reading is
   visible at a glance, then asks whether the photo was good enough. The same
   response carries how much of the month's analysis allowance is left, which the
   panel shows next to the result.
6. **The record is created only on "Upload my invoice"**, which is the single
   path that writes to Table Storage. Answering "No, retake it" deletes the
   result, answers with `204`, and the UI starts a new photo, so a reading
   somebody has rejected is never stored. Before confirming, the reviewer can
correct the reviewed fields the model returned — merchant, invoice date,
    invoice total, subtotal, total tax — validated as they are entered; there is
    no "add a field" dropdown. The invoice date is picked from a calendar, not
    typed. Every reviewed field is required: a document missing any of them
    shows that row empty with a red warning and cannot be stored until it is
    filled in. The corrections travel with the answer and are what gets stored.
    These five fields are the only ones the service keeps: every other extracted
    field — an invoice id, a customer name, addresses, a previous unpaid
    balance, the amount due — is dropped at extraction and is neither shown to
    the reviewer nor stored.
7. The photo is deleted from blob storage as soon as it has been read, whether
   the analysis succeeded, failed or was rejected.

`ANALYSIS_API_BASE_URL` on the frontend points at step 2 onward and is used by
the frontend's reverse proxy, not by the browser. It has no default: with no
backend URL there is nowhere to send a photo, so the UI says so and refuses to
submit. Because the browser never sees the address, it may be one the browser
cannot resolve, such as a Kubernetes service name.

### Repeated submissions

The invoice id is `sha256(photo bytes)`, so resubmitting the same photo returns
the same invoice and does not run Document Intelligence again — it bills per
page. The response carries `alreadyProcessed`, and the UI says the photo was
already processed and stops there rather than showing a result that was not
re-derived.

Claiming the photo is one atomic step that happens before the blob write. A photo that previously failed is re-analysed.

## frontend/

See [`frontend/README.md`](frontend/README.md) for local development and the test suite.

```bash
cd frontend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # point ANALYSIS_API_BASE_URL at the backend
python -m app             # HOST defaults to 0.0.0.0 (any IP); PORT to 8000
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

The browser does not talk to this directly; the frontend proxies to
it server-to-server.

## Running both locally

Two terminals:

```bash
cd backend  && .venv/bin/uvicorn app.main:app --port 3333 --reload
cd frontend && PORT=8001 .venv/bin/python -m app
```

Then open `http://localhost:8001`, or `http://<lan-ip>:8001` from another device. The analysis service allows all origins by
default; narrow `CORS_ALLOW_ORIGINS` before exposing it anywhere.

More conveniently, build the docker image and run the docker containers.

See the deploy sections of the respective README files:

- [backend](./backend/README.md#deploy)
- [frontend](./frontend/README.md#deploy)

## Repository layout

```
.
├── frontend/                 # camera capture UI (containerised)
│   ├── app/                  # FastAPI app and config, no Azure
│   ├── static/               # camera UI, manifest, icons and service worker (no build step)
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
(cd frontend && ../backend/.venv/bin/python -m pytest)  # 88 checks
(cd backend  && .venv/bin/python -m pytest)  # 256 checks
node frontend/scripts/check_ui.js            # 208 DOM checks, needs: npm install jsdom
```

No suite contacts Azure, and none depends on a local `.env`. One backend
SDK-contract check (`test_sdk_contract.py::test_upload_blob_call_uses_only_real_parameters`)
can fail against an SDK built for a different package layout; deselect it if your
environment hits that.
