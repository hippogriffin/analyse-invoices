# Invoice Upload — camera UI

The capture service. It runs in the browser, offers a choice of **Take a photo**
or **Upload a photo**, shows a live preview or the chosen file, and forwards the
result to the [`backend/`](../backend/README.md) service.

It holds **no Azure credentials, no Azure SDK and no storage access**. It serves
pages and proxies the browser's API calls to the analysis service; everything
Azure happens in the backend.

The browser only ever talks to this service's own origin. The server forwards
`/api/invoices*` to `ANALYSIS_API_BASE_URL`, so that address only has to be
reachable from this container — an in-cluster Kubernetes service name is fine —
and there is no cross-origin request for the backend to allow.

This is the `frontend/` component of the repository. It is self-contained:
everything below is relative to this directory.

## How it works

| Piece | File |
| --- | --- |
| Camera UI, file chooser, submit and poll | `static/app.js` |
| Page, `/api/config`, and the proxy | `app/main.py`, `app/proxy.py` |
| Env var config | `app/config.py` |

Flow in the browser:

1. On load, the app shows the available options. 
2. **Take a photo** requests camera access and shows a live preview. A **Zoom**
   slider (1×–3×) enlarges the preview so an invoice can fill the frame from a
   distance; it is a digital centre crop, so the whole sensor resolution is kept
   and the captured photo is exactly the part the preview showed. **Take photo**
   draws the current frame to a canvas, applies that zoom, and freezes it.
3. **Upload a photo** opens the OS file chooser, filtered to the accepted image
   types. The file is validated and previewed. The camera is never started.
4. **Upload** POSTs the image as multipart form field `photo` to this service's
   own `/api/invoices`, then polls `/api/invoices/{id}` until the fields are
   ready. The server forwards each call to `{ANALYSIS_API_BASE_URL}` and hands
   the answer straight back.

Either way the same review screen follows: the photo, with **Retake** /
**Choose another**, **Crop** and **Upload**. **Crop** opens a selection over
the photo — drag its corners to resize, drag the box to move, then **Apply
crop** to make the selection the new photo, **Reset** to start again, or
**Cancel** (or `Escape`) to leave the photo untouched. The selection is kept as
a percentage of the photo, so it survives any stage resize, and a cropped
uploaded file keeps its filename. The stage sizes itself to the feed or the
photo, so an invoice fills the frame edge to edge (no black bars around it) and
the crop selection always lines up with the photo that is actually visible.

The camera is stopped as soon as a photo is captured, and
also on `pagehide`. Front-facing cameras are mirrored in the preview, and the
mirror is baked into the captured image so the photo matches what you saw.

## Local development

```bash
cd frontend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env    # then point ANALYSIS_API_BASE_URL at the backend
python -m app
```

`HOST` defaults to `0.0.0.0`, which binds every interface, so the UI is reachable
from any IP (for example `http://<lan-ip>:8000`) as well as <http://localhost:8000>.
Set `HOST=127.0.0.1` to restrict it to the local machine. For a reloading dev
server run `uvicorn app.main:app --host 0.0.0.0 --reload` directly.

`localhost` counts as a secure context, so the camera works there. On any other
host you need real HTTPS, otherwise browsers block camera access and the app
shows that error.

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
at each step. It also covers the framing: the zoom slider enlarging the live
preview and the captured photo being the zoomed centre of the frame, and the
crop tool resizing and moving its selection and applying exactly that region.
It also covers the analysis panel: results appearing while
analysis runs, field labels and confidence badges, the remaining-monthly-allowance
line, editing and adding values
with their validation (money and merchant sanitising, malformed additions being
refused), the failure state, the keep-or-discard question in each of its states,
stale polling being cancelled by a retake, submission being refused when no
backend is configured, and that a cached page shell predating the allowance line
still reaches the file picker. The script prints `SKIP` if jsdom
is not installed, so it is safe to call in a pipeline.

### Testing the camera

Open the site over HTTPS and grant camera
permission when the browser asks. Note that a phone on `http://<lan-ip>` will
**not** get camera access — use HTTPS when accessing from a phone.

## Installable as an app (PWA)

The page can be installed to a phone's home screen and opened without browser
chrome. Three pieces make that work:

| Piece | File | Notes |
| --- | --- | --- |
| Web App Manifest | `static/manifest.json` | name, colours and the 192/512px icons; `display: standalone` is what drops the browser UI |
| App icons | `static/icons/` | the `any` icons, a `purpose: maskable` one, and the iOS touch icon. Regenerate with `python3 scripts/make_icons.py` (standard library only) |
| Service worker | `static/sw.js` | precaches the shell so the app opens offline; served from `/sw.js` so its scope is the whole app |

To install: on Android/Chrome use **Install app** (or the ⋮ → *Add to Home
screen*); on iOS use Safari's Share sheet → **Add to Home Screen**. Both need a
secure context (HTTPS), the same requirement the camera already has — so the
deployed site installs, but `http://<lan-ip>` does not.

## Configuration

All settings come from environment variables (see `.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANALYSIS_API_BASE_URL` | – | Where the server forwards the photo. Only has to be reachable from this container; empty means there is nowhere to send it |
| `HOST` | `0.0.0.0` | Interface the UI listens on. `0.0.0.0` is reachable from any IP |
| `PORT` | `8000` | Port the UI listens on |
| `MAX_UPLOAD_BYTES` | `10485760` | Size cap offered to the UI (10 MB). The backend enforces its own |
| `ALLOWED_CONTENT_TYPES` | `image/jpeg,image/png,image/webp` | Image types offered in the file chooser |

`ANALYSIS_API_BASE_URL` has no default on purpose. This service has no storage
of its own, so with no backend there is nothing it could do with a photo. It
starts anyway, shows a banner explaining why, and refuses to submit rather than
quietly discarding the image.

Because the forwarding happens server-side, the address may be one the browser
could not use: `http://localhost:3333` for local work, or a Kubernetes service
name such as `http://invoice-analysis.default.svc.cluster.local:3333` in a
cluster. The browser is never told it.

`MAX_UPLOAD_BYTES` and `ALLOWED_CONTENT_TYPES` exist only so an unusable file
is rejected in the browser instead of after an upload. The backend enforces the
same rules authoritatively, so a client that ignores them gains nothing. Keep
the two `.env` files in step.

### Submitting a photo

The photo is posted to this service as multipart form field `photo`, and the
server forwards it to the backend. The backend stores it, analyses it and returns
an invoice id, so there is no second request to send the image again. The
backend's validation message and status code are passed back unchanged, so the
UI reports the backend's own reason for a rejection.

Two details worth knowing if you change this flow:

- Polling is cancelled when a new photo is taken, so results for an old photo
  cannot overwrite new ones.
- Extracted values are assigned to editable inputs with `.value`, never
  `innerHTML`. OCR output is untrusted input; `tests/test_frontend.py` guards
  this, and the review screen can correct or add values before accepting.

### Confidence, and the keep-the-photo question

Each field is shown with the confidence Document Intelligence reported, and
anything under 70% is highlighted, because that is the point at which a reading
is worth checking by eye.

The service does **not** store those scores. It returns them while the analysis
is live in the process that produced them, so after a service restart the fields
are still there but the badges are absent — the UI omits them rather than showing
a stale number. A field with no confidence is rendered without a badge either
way.

### The remaining monthly allowance

Every analysis costs one against a monthly Document Intelligence allowance, and
the results panel says how much is left — *"499 of 500 documents left this
month."* The number comes from the backend, which reads it from its usage table on
every status call, so it survives a restart and counts a photo that was read and
then rejected, because that run was billed either way. It is shown next to the
result, not enforced: a reading still goes ahead at zero. When the backend cannot
read the usage it omits the fields entirely, and the panel shows no line rather
than inventing a figure.

### Correcting a reading

Document Intelligence reads some fields wrong and misses others — a merchant is
a common omission. The review screen shows a fixed set of fields that a person
is trusted to correct, and nothing else:

| Field | Shown as | Accepts |
| ----- | -------- | ------- |
| `VendorName` | Merchant | letters, numbers and spaces |
| `InvoiceDate` | Date | a date picked from the calendar, held as `YYYY-MM-DD` |
| `InvoiceTotal` | Invoice total | an amount with two decimals, shown as a bare number |
| `SubTotal` | Subtotal | an amount with two decimals, shown as a bare number |
| `TotalTax` | Total tax | an amount with two decimals, shown as a bare number |

These five are the only fields the service keeps — the backend drops everything
else Document Intelligence returns at extraction (an invoice id, a customer
name, addresses, the previous unpaid balance, the amount due, ...) so no other
value is ever on the wire or stored.

Every reviewed field is **required**. If the model could not
read a value that corresponds to a field, it will be empty, labelled with a red
"... is required." warning and stay ready to be filled in. The document cannot be
accepted until every field holds a value. The backend enforces the same rule, so
nothing is stored without them.

Values are constrained as
they are typed or pasted (money strips letters, a merchant strips markup and
punctuation), and accepting is refused while an edited value does not fit.
Nothing is kept off by wanting to type "*120.00 EUR*": only the bare number is
accepted, because the two decimals are the exactness invoice values are matched
on. Editing a value withdraws its confidence badge, because the score described
the automatic reading, not what the reviewer typed.

The corrections travel with the **Upload my invoice** answer as a JSON body on
`POST /api/invoices/{id}/photo`, and the backend merges them over the extracted
reading before writing the record. A plain confirmation, with nothing changed,
sends no body and stores the reading exactly as Document Intelligence returned it.

Once the fields are on screen, the app asks whether the photo was good enough:

- **Upload my invoice** accepts the reading, including any corrections, and
  that is what makes the backend write the record.
- **No, retake it** throws the reading away and immediately starts a new photo.

The choice decides whether the invoice is
kept at all. Nothing is written to Table Storage until "Yes", and "No" deletes
the result server-side and answers `204`. The UI therefore clears the results
and the panel and starts a new photo in one step.

If the same photo is submitted again, the backend deliberately does not analyse
it a second time and answers with `alreadyProcessed`. The app then says the photo
was already processed and stops: it does not poll, and it does not put a stored
reading back on screen as though it had just been produced. A photo that was read
but never accepted is *not* reported this way — nothing is stored yet, so the
backend hands the values back and the reviewer lands on the answer they have not
given, rather than being told it is already done.

The photo itself is deleted by the backend as soon as it has been read.

Once a reading is accepted the panel keeps a single button, relabelled **Choose
another photo**: the invoice is settled, so offering to retake it would invite
the user to undo their own answer, but the next invoice still has to be one
click away. The review bar's own "Choose another photo" is withdrawn once a
result is on screen so the two do not compete, and is restored if the analysis
fails, since a failure is asked nothing and would otherwise leave no way to
start again.

The answer is held by the backend for as long as the process lives, so it is
asked once rather than on every poll. A restart clears it, with one exception: a
*stored* record is its own proof that it was accepted, so an accepted invoice is
not asked about again even though the flag is gone. A failed call is reported in
the panel and the question stays open, with both buttons enabled again, so the
choice can be retried.

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

The backend no longer needs `CORS_ALLOW_ORIGINS` for this UI: the browser only
talks to this app, and the app forwards to the backend server-to-server. Set
`ANALYSIS_API_BASE_URL` to whatever address this app can reach — a Container
Apps FQDN, or, on Kubernetes, the service's in-cluster name.

For later changes to config or scaling, a revision is immutable — create a new
one rather than editing in place:

```bash
az containerapp update -g <rg> -n <app> --min-replicas 2
az containerapp revision list -g <rg> -n <app> -o table
```

## Operational notes

- **No auth on this service.** Anyone who can reach the ingress URL can submit a
  photo to the backend. It is recommended to place this service behind a firewall, VPN or a proxy.
  Authentication can be added using services like [OAuth2 Proxy](https://github.com/oauth2-proxy/oauth2-proxy).
- **Container Apps or Kubernetes must scale to at least 1 replica**; the default min is 0.
- **The backend must be reachable from this container**, not the browser. It can
  stay on a private network or an in-cluster service name; no public ingress and
  no CORS entry are required for the UI to work.
