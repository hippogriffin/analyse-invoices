# Invoice Upload — camera UI

The capture service. It runs in the browser, offers a choice of **Take a photo**
or **Upload a photo**, shows a live preview or the chosen file, and sends the
result to the [`backend/`](../backend/README.md) service.

It holds **no Azure credentials, no Azure SDK and no storage access**. It serves
pages and tells the browser where the analysis service lives; everything Azure
happens in the backend.

This is the `frontend/` component of the repository. It is self-contained:
everything below is relative to this directory.

## How it works

| Piece | File |
| --- | --- |
| Camera UI, file chooser, submit and poll | `static/app.js` |
| Page and `/api/config` | `app/main.py` |
| Env var config | `app/config.py` |

Flow in the browser:

1. On load, the app shows the chooser. It does **not** ask for camera
   permission, so nothing is prompted before the user has chosen.
2. **Take a photo** requests the camera and shows a live preview. **Take photo**
   draws the current frame to a canvas and freezes it.
3. **Upload a photo** opens the OS file chooser, filtered to the accepted image
   types. The file is validated and previewed. The camera is never started.
4. **Upload** POSTs the image as multipart form field `photo` to
   `{ANALYSIS_API_BASE_URL}/api/invoices`, then polls
   `{ANALYSIS_API_BASE_URL}/api/invoices/{id}` until the fields are ready.

Either way the same review screen follows: the photo, with **Retake** /
**Choose another** and **Upload**.

The camera is stopped as soon as a photo is captured (privacy and battery), and
also on `pagehide`. Front-facing cameras are mirrored in the preview, and the
mirror is baked into the captured image so the photo matches what you saw.

## Local development

```bash
cd frontend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env    # then point ANALYSIS_API_BASE_URL at the backend
uvicorn app.main:app --reload
```

Open <http://localhost:8000>. `localhost` counts as a secure context, so the
camera works. On any other host you need real HTTPS, otherwise browsers block
camera access and the app shows that error.

Run the tests:

```bash
pytest
```

`pytest` covers config parsing and the endpoints this service exposes —
deliberately a short list, because there is no longer any storage code to test.
Two guards are worth calling out: one asserts this service exposes no Azure
routes, and one asserts that importing it pulls in no Azure SDK.

The camera flow is plain JavaScript with no build step, so it has its own
optional check that drives the real DOM with a fake camera:

```bash
npm install jsdom
node scripts/check_ui.js
```

It walks the whole flow (load → choose → capture/pick → submit → results, plus a
denied-permission case and a rejected submission) and asserts what is on screen
at each step. It also covers the analysis panel: results appearing while
analysis runs, field labels and confidence badges, the failure state, the
keep-or-discard question in each of its states, stale polling being cancelled by
a retake, and submission being refused when no backend is configured. The script
prints `SKIP` if jsdom is not installed, so it is safe to call in a pipeline.

### Testing the camera on a desktop

Desktop webcams work. On a phone, open the site over HTTPS and grant camera
permission when the browser asks. Note that a phone on `http://<lan-ip>` will
**not** get camera access — use HTTPS.

## Configuration

All settings come from environment variables (see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANALYSIS_API_BASE_URL` | – | Where the photo is posted. Empty means there is nowhere to send it |
| `MAX_UPLOAD_BYTES` | `10485760` | Size cap offered to the UI (10 MB). The backend enforces its own |
| `ALLOWED_CONTENT_TYPES` | `image/jpeg,image/png,image/webp` | Image types offered in the file chooser |

`ANALYSIS_API_BASE_URL` has no default on purpose. This service has no storage
of its own, so with no backend there is nothing it could do with a photo. It
starts anyway, shows a banner explaining why, and refuses to submit rather than
quietly discarding the image.

`MAX_UPLOAD_BYTES` and `ALLOWED_CONTENT_TYPES` exist only so an unusable file
is rejected in the browser instead of after an upload. The backend enforces the
same rules authoritatively, so a client that ignores them gains nothing. Keep
the two `.env` files in step.

### Submitting a photo

The photo is posted as multipart form field `photo`. The backend stores it,
analyses it and returns an invoice id, so there is no second request to send the
image again.

Two details worth knowing if you change this flow:

- Polling is cancelled when a new photo is taken, so results for an old photo
  cannot overwrite new ones.
- Extracted values are inserted with `textContent`, never `innerHTML`. OCR
  output is untrusted input; `tests/test_frontend.py` guards this.

### Confidence, and the keep-the-photo question

Each field is shown with the confidence Document Intelligence reported, and
anything under 70% is highlighted, because that is the point at which a reading
is worth checking by eye.

The service does **not** store those scores. It returns them while the analysis
is live in the process that produced them, so after a service restart the fields
are still there but the badges are absent — the UI omits them rather than showing
a stale number. A field with no confidence is rendered without a badge either
way.

Once the fields are on screen, the app asks whether the photo was good enough:

- **Yes, it is readable** accepts the reading, and that is what makes the backend
  write the record.
- **No, retake it** throws the reading away and immediately starts a new photo.

The choice is not a note about the photo — it decides whether the invoice is
kept at all. Nothing is written to Table Storage until "Yes", and "No" deletes
the result server-side and answers `204`. The UI therefore clears the results
and the panel and starts a new photo in one step, rather than printing "noted"
and leaving rejected values on screen where they could still be accepted by
mistake. The old "Noted. Take a clearer photo and send it again." line is gone
for that reason: the reviewer has asked for a different photo, so a different
photo is what happens.

If the same photo is submitted again, the backend deliberately does not analyse
it a second time and answers with `alreadyProcessed`. The app then says the photo
was already processed and stops: it does not poll, and it does not put a stored
reading back on screen as though it had just been produced. A photo that was read
but never accepted is *not* reported this way — nothing is stored yet, so the
backend hands the values back and the reviewer lands on the answer they have not
given, rather than being told it is already done.

The photo itself is deleted by the backend as soon as it has been read, whichever
answer is given, so the question is never about deleting it. The panel says so,
because "the photo is gone" is otherwise a surprise.

Once a reading is accepted the panel keeps a single button, relabelled **Choose
another photo**: the invoice is settled, so offering to retake it would invite
the user to undo their own answer, but the next invoice still has to be one
click away. The review bar's own "Choose another photo" is withdrawn once a
result is on screen so the two do not compete, and is restored if the analysis
fails, since a failure is asked nothing and would otherwise leave no way to
start again.

The answer is held by the backend for as long as the process lives, so it is
asked once rather than on every poll — but a service restart clears it and the
question returns. A failed call is reported in the panel and the question stays
open, with both buttons enabled again, so the choice can be retried.

## Deploy to Azure Container Apps

This service needs no managed identity: it makes no Azure calls. It only needs to
reach the backend, so give it no `--identity` and no secrets.

### 1. Build the image

```bash
az acr create -g <rg> -n <registry> --sku Basic --admin-enabled true
ACR_LOGIN=$(az acr list-credentials --name <registry> --query username -o tsv)
ACR_PWD=$(az acr list-credentials --name <registry> --query passwords[0].value -o tsv)

docker build -t <registry>.azurecr.io/invoice-upload:v1 ./frontend
docker login <registry> -u "$ACR_LOGIN" --password-stdin <<< "$ACR_PWD"
docker push <registry>.azurecr.io/invoice-upload:v1
```

### 2. Create the container app

```bash
az containerapp create \
  -g <rg> -n <app> \
  --image <registry>.azurecr.io/invoice-upload:v1 \
  --registry-server <registry> \
  --registry-username "$ACR_LOGIN" \
  --registry-password "$ACR_PWD" \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --env-vars \
      ANALYSIS_API_BASE_URL=https://<backend>.azurecontainerapps.io
```

### 3. Get the URL and allow the camera

```bash
az containerapp show -g <rg> -n <app> --query properties.configuration.ingress.fqdn -o tsv
```

Container Apps ingress serves HTTPS by default, which satisfies the secure
context requirement, so the camera works from a phone.

Set the backend's `CORS_ALLOW_ORIGINS` to this app's URL, otherwise the browser
blocks the file POST.

For later changes to config or scaling, a revision is immutable — create a new
one rather than editing in place:

```bash
az containerapp update -g <rg> -n <app> --min-replicas 2
az containerapp revision list -g <rg> -n <app> -o table
```

## Operational notes

- **No auth on this service.** Anyone who can reach the ingress URL can submit a
  photo to the backend. Add an Easy Auth / Entra ID front door or a private
  endpoint if that matters.
- **Container Apps must scale to at least 1 replica**; the default min is 0.
- **The backend must be reachable from the browser**, not just from this
  container. Its CORS setting has to allow this app's origin.
