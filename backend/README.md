# Invoice analysis service

Accepts a photo, stores it in Blob Storage, and extracts the invoice fields with
Azure Document Intelligence. The result is held in memory until a reviewer accepts
it, and only then written to Table Storage.

This service owns every Azure credential and every blob name. The browser posts
the image itself; it never names a blob and never holds a key.

This replaces the Azure Logic App in `../logic-app.json`. It is a plain
container, so it builds and runs the same way as the frontend.

## How it works

```
POST /api/invoices   multipart: photo=@invoice.jpg
  -> 202 {"invoiceId": "9f2c…", "statusUrl": "/api/invoices/9f2c…"}
  -> the photo is stored, then analysis runs in a background task

GET  /api/invoices/{invoiceId}
  -> {"status": "complete", "documents": [{"fields": {...}}]}
```

The response is `202` before analysis runs, so the caller is not blocked on
Document Intelligence, which takes seconds to tens of seconds per page.

The upload is validated before anything is written: content type against
`ALLOWED_CONTENT_TYPES`, and size against `MAX_UPLOAD_BYTES` while the body is
being read, so an oversized upload is refused rather than buffered in full.

The bytes are held in memory for the duration of the request, but the background
task reads the photo back from Blob Storage rather than being handed the buffer.
That is deliberate: the job is identified by a blob name, so it does not depend
on a request that has already returned, and the bytes analysed are provably the
bytes stored. The cost is one extra read of an object this service just wrote.

### The invoice id is the content hash

`invoiceId` is `sha256(photo bytes)`, truncated to 32 hex characters. That single
value is the blob name, the partition key and the idempotency key. Consequences
worth knowing:

- Submitting the same photo again returns the same id and does not analyse it
  again, so a retry cannot duplicate an invoice or bill a second page. The
  response says so with `alreadyProcessed`, because a UI that cannot tell a
  repeat from a fresh photo will make the reviewer wait for work that is not
  going to happen.
- That is one atomic claim (`JobStateStore.claim`), taken before the blob write
  rather than after it. The write is awaited, so a check-then-claim left a window
  in which a concurrent second request saw an unclaimed invoice and queued a
  second billable analysis.
- The same photo is the same invoice however it arrived, because the identity
  comes from the bytes rather than from a name the caller chose.
- A photo that previously *failed* is re-analysed. Otherwise a transient
  Document Intelligence error would make that photo permanently unprocessable.
- The key is a hash, so it is stable across restarts and needs no side index.

The blob name is `{SOURCE_PREFIX}/{sha256[:32]}.{jpg,png,webp}`, so the container
does not fill with near-duplicates either.

### Confidence is returned but not stored

The status response carries a confidence for the document and for each field, and
the UI shows it, flagging anything under 70%. **No confidence is written to
Table Storage.** The rows hold `f_<FieldName>` and nothing else.

A confidence answers "how much should I trust this reading?", which is only
useful while someone is looking at the result. It is a property of one model run,
not a durable fact about the invoice, and a stale 0.99 is worse than no number at
all — it invites confidence in a reading that was never re-checked.

The consequence is that confidence is served from the process that produced it.
`app/job_state.py` holds it, bounded by entry count and age, alongside the job
status, any error, and the user's answer about the photo — none of which are
durable facts about the invoice. So:

- while the service is running, a completed invoice reports its scores;
- after a restart, the values are still there and the scores are `null`, and the
  UI omits the badges rather than inventing them;
- the same applies to a second replica, since the scores are not shared between
  instances. Run one backend instance, or accept that scores may be absent on
  some responses — the extracted fields are never affected;
- `tests/test_job_state.py` and the API tests pin this both ways, so a
  passing response is never mistaken for evidence that something was stored.

### The photo is always deleted, and the user is asked about quality

Every source photo is deleted from blob storage once it has been read, in a
`finally` block that runs whether the analysis succeeded or failed. Nothing is
kept so that a later "discard" can act on it: the pixels are not the product,
the extracted values are.

Once the fields are on screen the user is asked whether the photo was good
enough. This is a question about the photo, not a request to delete it:

| Answer  | Request                | Meaning                              |
| ------- | ---------------------- | ------------------------------------ |
| yes     | `POST .../photo`       | the reading is accepted, store it    |
| no      | `DELETE .../photo`     | too blurry, keep nothing             |

**This is the only path that creates rows.** The analysis reads the photo and
holds the values in `app/job_state.py`; the table learns nothing until a reviewer
says the photo was readable. Until then the result exists in this process alone,
so a row in the table means exactly one thing: *these values were read from a
photo, and a person accepted them.*

Saying no therefore takes the result away rather than recording an opinion about
it. It deletes every row for the partition and returns `204` with no body, so the
caller has nothing to render. That includes a record accepted before a restart,
which the transient verdict has since forgotten: a reviewer saying the photo was
not readable is saying the stored values are not to be trusted, whatever this
process remembers about it.

Neither answer touches blob storage. The photo is already gone by the time
either is possible.

Accepting writes the record once. A second confirmation of the same invoice is a
no-op rather than a second write, so a double click cannot re-bill or duplicate
anything. A storage failure while accepting returns `502` and keeps the result in
memory, so the reviewer can accept again once the table is back rather than
paying for a second analysis.

### What a restart costs

A reading that has not been accepted lives only in this process. A restart loses
it: the status becomes unknown and the reviewer has to send the photo again,
paying Document Intelligence a second time for the same page. That is the price
of a table that never holds a reading nobody approved, and it is the right way
round for a service whose output is paperwork someone has to sign off on.

Once accepted, the values are durable, and the verdict need not be: a stored
record is itself the proof of acceptance, so `photoSatisfied` reports `true` for
a stored invoice even after a restart that forgot the answer.

### Row layout

One partition per invoice, which keeps every read and write inside a single
partition key:

| Row       | Contents                                                 |
| --------- | -------------------------------------------------------- |
| `doc-000` | identity, timestamps, blob name, doc count, first document's fields |
| `doc-001` | second document's fields, for multi-page inputs           |

`doc-000` carries the invoice-level columns *and* the first document's fields.
It is the marker that the reading was accepted, which is why the read is a point
get on a row key the service already knows rather than a scan of the partition.

The identity fields are `PartitionKey`, `RowKey`, `invoiceId`, `blobName`,
`createdAt`, `completedAt` and `documentCount`. There is no `status`, no `error`,
no `photoSatisfied` and no `updatedAt`: a row only ever describes an accepted
reading, so a row that said `processing` could only be a row about a job that no
longer exists, and a row that recorded the verdict would duplicate the proof that
the row's existence already gives. Document order comes from the `doc-NNN`
`RowKey`, so there is no `index` column to disagree with it, and no `docType`
column either. `blobName` is a historical reference to a blob that no longer
exists. A `meta` row from an earlier version is deleted on the next accepted
write for the same photo.

Field values are stored as `f_<FieldName>`.

Writes pass `mode=UpdateMode.MERGE` explicitly. The SDK defaults
`update_entity` to merging, but the application does not depend on that default:
a replace would drop every property the call did not name, and silently narrow a
document set. The one consequence of merging is that a property the new read does
not produce is left as it was, so a photo re-read with a changed model keeps
stale fields until the partition is cleared.

Every row for an invoice shares one partition, and a Table transaction is atomic
within a partition, so the document rows and the deletion of any now-stale
`doc-NNN` rows are written in a single `submit_transaction`. A reader therefore
never sees a partial reading, and a re-run with fewer pages cannot leave old
results behind. `delete_record` removes the whole partition the same way, in
chunks, so a rejected reading leaves nothing behind — including `doc-001+`, which
would otherwise keep the values the reviewer rejected. Table Storage caps a
transaction at 100 entities, so the operations are chunked for a very large
document set. Nothing is written before a reviewer accepts, so a failure and an
unanswered question both leave no row at all.

## The failure this fixes

`logic-app.json` called `Analyze Document` and then read `analyzeResult`
directly. Document Intelligence is a long-running operation: it returns `202`
with an `Operation-Location` header, and the results are only there once a
subsequent request returns `200`. The workflow had no poll and no timeout, so
it read a body that did not exist yet.

Here the LRO is handled by the SDK's poller:

```python
poller = client.begin_analyze_document("prebuilt-invoice", photo_bytes)
result = poller.result(timeout=settings.analysis_timeout_seconds)
```

It also wrote a random GUID as both `PartitionKey` and `RowKey`, so a
redelivered event produced a duplicate row that could never be found again. The
partition key is the content hash here, so a repeated submission overwrites its
own results.

### Two storage endpoints

`STORAGE_ACCOUNT_URL` is the **blob** endpoint. Table Storage is a different
service on a different host, so the table endpoint is derived from the account
name as `https://<account>.table.core.windows.net`.

This matters: sending a table write to the blob endpoint reaches the blob
service, which answers `405 UnsupportedHttpVerb` — an error that names a verb
rather than the real problem. An emulator or custom domain that serves both
services from one URL is passed through unchanged.

## What it does not do

Background work runs **in the same process**, so a restart loses the job in
flight. That is the deliberate trade for a single container with no queue.

Job state lives in the process, not in Table Storage, so nothing survives a
restart. A `pending`, `processing` or `failed` invoice is simply not found
afterwards: the status endpoint returns `404`, and the UI says the service lost
track of the photo and asks for it again. That is honest — a job whose only
record was the memory of the process that was running it cannot be resumed, and
inventing a "stale, please retry" row for it would put a status back into storage
that storage was freed of precisely so it could not hold.

A completed invoice is different: its fields are in Table Storage, so it is
still served after a restart. Only the transient parts are gone — confidence,
`error` and `photoSatisfied` come back `null`.

If you later need retries, dead-lettering or burst handling, move the same
`analyze` call behind a queue: Event Grid → Service Bus → worker. The call site
does not change.

There is **no authentication on this API**. Anyone who can reach it can spend
Document Intelligence quota. Put an Easy Auth / Entra ID front door or a private
endpoint in front of it if that matters.

## Configuration

Copy `.env.example` to `.env`. The settings that matter:

| Variable                          | Default                        | Purpose |
| --------------------------------- | ------------------------------ | ------- |
| `STORAGE_ACCOUNT_URL`             | –                              | Blob endpoint. Empty means the service returns `503` |
| `SOURCE_CONTAINER`                | `pictures`                     | Container photos are written to |
| `SOURCE_PREFIX`                   | –                              | Optional prefix, e.g. `2026`. Also bounds which blobs are analysed |
| `MAX_UPLOAD_BYTES`                | `10485760`                     | Largest accepted photo (10 MB) |
| `ALLOWED_CONTENT_TYPES`           | `image/jpeg,image/png,image/webp` | Accepted image types |
| `RESULT_TABLE_NAME`               | `invoices`                     | Table holding successful readings |
| `DOCUMENT_INTELLIGENCE_ENDPOINT`  | –                              | Resource endpoint. Empty means `503` |
| `DOCUMENT_INTELLIGENCE_MODEL`     | `prebuilt-invoice`             | Analysis model |
| `ANALYSIS_TIMEOUT_SECONDS`        | `120`                          | How long to wait for the analyze operation |
| `CORS_ALLOW_ORIGINS`              | `*`                            | Origins the browser may post from |
| `BLOB_AUTH_METHOD`                | `managed_identity`             | Or `account_key`, for local testing only |

`SOURCE_PREFIX` is a guard, not just tidiness. The API has no caller
authentication, so it also bounds which blobs this service will read back for
analysis. The name is now generated here rather than supplied by the caller,
which removes the need for that check on the way in, but the read side still
enforces it.

`MAX_UPLOAD_BYTES` and `ALLOWED_CONTENT_TYPES` are published on `/api/config`
so the UI can filter its file chooser and reject a bad photo early. They are
enforced here regardless: the browser's copy is a convenience, not a control.

Configuration problems are checked at startup and logged, because a wrong
service endpoint otherwise produces an Azure error that names no setting. The
endpoint in particular must be the resource's own,
`https://<resource>.cognitiveservices.azure.com`. The legacy multi-service host
`*.api.cognitive.microsoft.com` serves Form Recognizer v2.1 and answers the GA
path with a 404, so it is reported at startup rather than failing as "unsupported
Http Verb" mid-request.

Authentication is managed identity by default, with
`BLOB_AUTH_METHOD=account_key` for local testing only. In that mode both
`AZURE_STORAGE_ACCOUNT_KEY` and `DOCUMENT_INTELLIGENCE_KEY` are required, and
starting without them is an error rather than a runtime surprise.

In Azure, the identity needs:

- **Storage Blob Data Contributor** on the source container
- **Storage Table Data Contributor** on the results table
- **Cognitive Services User** on the Document Intelligence resource

## API

| Method | Path                        | Purpose                                  |
| ------ | --------------------------- | ---------------------------------------- |
| `POST` | `/api/invoices`             | Store and analyse a photo, returns `202` |
| `GET`  | `/api/invoices/{invoiceId}` | Status and extracted fields              |
| `POST` | `/api/invoices/{invoiceId}/photo` | Accept the reading; stores it, `200` |
| `DELETE` | `/api/invoices/{invoiceId}/photo` | Reject the photo; deletes it, `204` |
| `GET`  | `/api/config`               | Non-secret settings                      |
| `GET`  | `/healthz`                  | Liveness                                 |

`status` is one of `pending`, `processing`, `complete` or `failed`.

A reading that has not been accepted yet is reported as `complete` with its
documents from memory, so the reviewer has something to be asked about.

`photoSatisfied` is `null` until the user answers, then `true` or `false`, and
the UI prompts only on `null`. The three states are distinct on purpose. The flag
is transient, so a restart forgets which answer was given — but a *stored*
record is its own proof of acceptance and always reports `true`, so a restart
never asks about a reading that was already agreed.

Both photo routes record an opinion about the photo and touch no storage. The
photo itself is deleted during processing, before either route can be called.

`GET /docs` serves interactive OpenAPI docs.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env      # then fill it in
.venv/bin/uvicorn app.main:app --reload
```

The browser posts from the frontend's origin, so set `CORS_ALLOW_ORIGINS` to it.
`*` is fine for local work.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite replaces the Azure clients with in-memory fakes, so it never contacts
Azure and needs no credentials. The fakes deliberately mirror the real SDK
surface, including where it differs from the obvious guess: `TableClient` has no
`create_table_if_not_exists`, `upsert_entity` takes one entity rather than a
batch, and `list_entities` has no `partition_key` argument. A suite whose fakes
agreed with the code would not catch a call to a method that does not exist.

## Deploy

```bash
docker build -t invoice-analysis .
docker run -p 3333:3333 --env-file .env invoice-analysis
```

Deploy the image to Azure Container Apps, Azure Container Apps Jobs or Azure
Functions custom containers. Set `CORS_ALLOW_ORIGINS` to the frontend's origin,
since the browser calls this service directly.
