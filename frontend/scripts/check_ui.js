/**
 * Real DOM check of the invoice UI visibility rules, using jsdom.
 * Verifies the reported bug: broken <img> icon and a Retake button that
 * appeared before any photo was taken. Also covers the take-photo /
 * upload-a-photo chooser and the file upload path.
 */
const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch {
  console.log("SKIP: jsdom is not installed. This is an optional check.");
  console.log("      Run:  npm install jsdom   (from the repo root)");
  process.exit(0);
}

const REPO = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(REPO, "static/index.html"), "utf8");
const appjs = fs.readFileSync(path.join(REPO, "static/app.js"), "utf8");
const css = fs.readFileSync(path.join(REPO, "static/styles.css"), "utf8");

const failures = [];
function check(label, cond, extra = "") {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${extra ? " -> " + extra : ""}`);
  if (!cond) failures.push(label);
}

/** Visible = not hidden. Mirrors what the browser paints, given the CSS fix. */
function snapshot(win, label) {
  const g = (id) => win.document.getElementById(id);
  const state = {
    modePicker: !g("modePicker").hidden,
    workbench: !g("workbench").hidden,
    pickFile: !g("pickFile").hidden,
    still: !g("still").hidden,
    preview: !g("preview").hidden,
    retake: !g("reviewActions").hidden,
    upload: !g("upload").hidden,
    capture: !g("capture").hidden,
    placeholder: !g("placeholder").hidden,
    results: !g("results").hidden,
  };
  console.log(`  [${label}]`, JSON.stringify(state));
  return state;
}

const COMPLETE_STATUS = {
  status: "complete",
  documentCount: 1,
  documents: [
    {
      index: 0,
      confidence: 0.98,
      fields: {
        // Deliberately contains markup: OCR output is untrusted input.
        VendorName: { value: "Acme <b>Ltd</b>", confidence: 0.99 },
        InvoiceDate: { value: "2026-03-01", confidence: 0.4 },
        SomeOddFieldName: { value: "x", confidence: null },
        EmptyField: { value: "", confidence: 0.9 },
      },
    },
  ],
};

/**
 * Route the app's fetch calls. The photo is posted with XMLHttpRequest, so this
 * only serves the config and the status polls. Returns the log of URLs
 * requested, so tests can assert polling actually happened.
 */
function makeFetchStub({
  analysisApiBaseUrl = "",
  statuses = [],
  config = {},
  photoDecision = null,
  lostJob = false,
} = {}) {
  const requested = [];
  const decisions = [];
  let statusCall = 0;
  const stub = async (url, options = {}) => {
    const method = options.method || "GET";
    requested.push(`${method} ${url}`);
    if (url === "/api/config") {
      return {
        ok: true,
        json: async () => ({
          analysisApiBaseUrl,
          analysisConfigured: Boolean(analysisApiBaseUrl),
          maxUploadBytes: 10 * 1024 * 1024,
          allowedContentTypes: ["image/jpeg", "image/png", "image/webp"],
          ...config,
        }),
      };
    }
    // Checked before the status branch, since this path also matches
    // "/api/invoices/".
    if (/\/api\/invoices\/[^/]+\/photo$/.test(url)) {
      const satisfied = method === "POST";
      decisions.push({ satisfied, url, method });
      const result =
        (typeof photoDecision === "function" ? photoDecision(satisfied) : photoDecision) || {
          photoSatisfied: satisfied,
        };
      const ok = result.ok !== false;
      return {
        ok,
        status: result.status || (ok ? 200 : 502),
        json: async () => result,
      };
    }
    if (url.includes("/api/invoices/")) {
      // A 404 is a real answer: no row and no held state means the service has
      // no record of the job at all.
      if (lostJob) {
        return { ok: false, status: 404, json: async () => ({ detail: "Unknown invoice id." }) };
      }
      const body = statuses[Math.min(statusCall++, statuses.length - 1)] || {
        status: "pending",
      };
      return { ok: true, json: async () => body };
    }
    return { ok: true, json: async () => ({}) };
  };
  stub.requested = requested;
  stub.decisions = decisions;
  return stub;
}

function buildDom({
  cameraAvailable = true,
  analysisApiBaseUrl = "",
  statuses = [],
  config = {},
  xhrResponse = {},
  photoDecision = null,
  lostJob = false,
} = {}) {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "https://localhost/" });
  const win = dom.window;
  const doc = win.document;

  // A fake camera. The request count matters: nothing may ask for the camera
  // before the user picks "Take a photo".
  const track = { stop: () => {}, getSettings: () => ({ facingMode: "environment" }) };
  const fakeStream = { getTracks: () => [track], getVideoTracks: () => [track] };
  win.__cameraRequests = 0;

  win.navigator.mediaDevices = {
    getUserMedia: async () => {
      win.__cameraRequests += 1;
      if (!cameraAvailable) {
        const err = new Error("denied");
        err.name = "NotAllowedError";
        throw err;
      }
      return fakeStream;
    },
  };

  const video = doc.getElementById("preview");
  // jsdom does not implement media playback or canvas rasterisation.
  Object.defineProperty(win, "isSecureContext", { value: true, configurable: true });
  Object.defineProperty(video, "play", { value: async () => {}, configurable: true });
  Object.defineProperty(video, "readyState", { value: 1, configurable: true });
  Object.defineProperty(video, "videoWidth", { value: 1280, configurable: true });
  Object.defineProperty(video, "videoHeight", { value: 720, configurable: true });
  video.addEventListener = () => {};

  const origCreate = doc.createElement.bind(doc);
  doc.createElement = (tag) => {
    if (String(tag).toLowerCase() !== "canvas") return origCreate(tag);
    return {
      width: 0,
      height: 0,
      getContext: () => ({
        translate() {},
        scale() {},
        drawImage() {},
      }),
      toBlob(cb) {
        cb(new win.Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" }));
      },
    };
  };

  win.URL.createObjectURL = () => "blob:fake";
  win.URL.revokeObjectURL = () => {};
  win.__fetchStub = makeFetchStub({
    analysisApiBaseUrl,
    statuses,
    config,
    photoDecision,
    lostJob,
  });
  win.fetch = win.__fetchStub;

  // jsdom cannot open a file chooser, so record the intent and let a test
  // decide what the user "picked".
  const fileInput = doc.getElementById("fileInput");
  win.__pickerOpened = 0;
  fileInput.click = function () {
    win.__pickerOpened += 1;
  };

  // Record every moment the placeholder becomes visible, not just the end
  // state. A wait screen that flashes for one frame is still a bug.
  const watch = (win.__placeholderShown = []);
  const observer = new win.MutationObserver((records) => {
    for (const record of records) {
      const el = record.target;
      if (el.hidden) continue;
      if (el.id === "placeholder") watch.push("WAIT-SCREEN: " + el.querySelector("p").textContent);
      if (el.id === "still" && el.getAttribute("src") === null) watch.push("STILL-VISIBLE-WITHOUT-SRC");
    }
  });
  observer.observe(doc.getElementById("placeholder"), { attributes: true, attributeFilter: ["hidden"] });
  observer.observe(doc.getElementById("still"), { attributes: true, attributeFilter: ["hidden"] });
  // The photo is submitted with XHR, so the fake records what was sent. This is
  // the only place the file leaves the page: there is no self-upload endpoint.
  class FakeXHR {
    constructor() {
      this.upload = {
        addEventListener: (e, cb) =>
          e === "progress" && cb({ lengthComputable: true, loaded: 50, total: 100 }),
      };
      const canned = win.__xhrResponse || {};
      this.status = canned.status === undefined ? 200 : canned.status;
      this.responseText =
        canned.body === undefined
          ? JSON.stringify({ invoiceId: "inv-123", alreadyProcessed: !!canned.alreadyProcessed })
          : canned.body;
    }
    addEventListener(e, cb) {
      if (e === "load") setTimeout(cb, 0);
    }
    open(method, url) {
      this._method = method;
      this._url = url;
    }
    send(form) {
      win.__xhrPosts.push({ method: this._method, url: this._url, form });
    }
  }
  win.__xhrPosts = [];
  win.__xhrResponse = xhrResponse;
  win.XMLHttpRequest = FakeXHR;

  win.eval(appjs);
  return win;
}

const tick = () => new Promise((r) => setTimeout(r, 25));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** Press one of the two chooser buttons. */
async function chooseMode(win, mode) {
  win.document.getElementById(mode === "camera" ? "chooseCamera" : "chooseFile").click();
  await tick();
}

/** Simulate the user picking a file, then fire change as a browser would. */
async function pickFile(win, { name = "invoice.jpg", type = "image/jpeg", bytes = 64 } = {}) {
  const input = win.document.getElementById("fileInput");
  const file = new win.File([new Uint8Array(bytes)], name, { type });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new win.Event("change"));
  await tick();
  return file;
}

/** Capture a photo and upload it, the path that triggers analysis. */
async function captureAndUpload(win) {
  await chooseMode(win, "camera");
  win.document.getElementById("capture").click();
  await tick();
  win.document.getElementById("upload").click();
  await tick();
}

(async () => {
  console.log("\n--- 1. initial load: the chooser, and no camera request ---");
  let win = buildDom();
  await tick();
  let s = snapshot(win, "after init");
  check("chooser visible", s.modePicker === true);
  check("workbench hidden behind the chooser", s.workbench === false);
  check("chooser offers both routes", ["Take a photo", "Upload a photo"].every(
    (label) => ["chooseCamera", "chooseFile"].some(
      (id) => win.document.getElementById(id).textContent.trim() === label
    )
  ));
  check("still image hidden (no broken icon)", s.still === false);
  check("preview hidden", s.preview === false);
  check("retake hidden before any photo", s.retake === false);
  // The Upload button only becomes reachable once a mode is chosen, so what
  // matters here is that its container is hidden, not the button's own flag.
  check(
    "upload unreachable before any photo",
    win.document.getElementById("reviewActions").hidden === true
  );
  check(
    "camera not requested before a mode is chosen",
    win.__cameraRequests === 0,
    `${win.__cameraRequests} request(s)`
  );
  check(
    "file chooser not opened either",
    win.__pickerOpened === 0,
    `${win.__pickerOpened} open(s)`
  );

  console.log("\n--- 1b. choosing 'Upload a photo' never touches the camera ---");
  await chooseMode(win, "upload");
  s = snapshot(win, "after choosing upload");
  check("chooser gone", s.modePicker === false);
  check("workbench shown", s.workbench === true);
  check("choose-a-photo offered", s.pickFile === true);
  check("camera not requested in upload mode", win.__cameraRequests === 0);
  check("preview hidden in upload mode", s.preview === false);
  check("take-photo button hidden in upload mode", s.capture === false);
  check(
    "placeholder explains what to do",
    /choose an invoice photo/i.test(win.document.getElementById("placeholderText").textContent),
    win.document.getElementById("placeholderText").textContent
  );
  check(
    "accept list came from the server config",
    win.document.getElementById("fileInput").accept === "image/jpeg,image/png,image/webp",
    win.document.getElementById("fileInput").accept
  );

  console.log("\n--- 1b2. with no backend, the chooser still works ---");
  win = buildDom();
  await tick();
  check(
    "a missing backend is explained on load, not silent",
    /ANALYSIS_API_BASE_URL/.test(win.document.getElementById("banner").textContent) &&
      win.document.getElementById("banner").hidden === false,
    win.document.getElementById("banner").textContent
  );
  await chooseMode(win, "upload");
  check(
    "choosing a route clears that banner",
    win.document.getElementById("banner").hidden === true
  );
  await pickFile(win, { name: "orphan.jpg" });
  win.document.getElementById("upload").click();
  await tick();
  check(
    "but submission is refused before anything is sent",
    win.__xhrPosts.length === 0,
    JSON.stringify(win.__xhrPosts)
  );
  check(
    "and the reason is shown",
    /nowhere to send/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  check(
    "and the photo can still be retried once configured",
    win.document.getElementById("upload").hidden === false
  );

  console.log("\n--- 1c. picking a file, then sending it to the backend ---");
  win = buildDom({ analysisApiBaseUrl: "https://analysis.example" });
  await tick();
  await chooseMode(win, "upload");
  await pickFile(win, { name: "march-invoice.jpg" });
  s = snapshot(win, "after picking a file");
  check("still image shows the chosen file", s.still === true);
  check("upload offered", s.upload === true);
  check("second action is to choose another", !win.document.getElementById("retake").hidden);
  check(
    "second action reads 'Choose another'",
    win.document.getElementById("retake").textContent.trim() === "Choose another",
    win.document.getElementById("retake").textContent.trim()
  );
  check(
    "status mentions the selection",
    /photo selected/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  win.document.getElementById("upload").click();
  await tick();
  check(
    "the file was posted to the backend",
    win.__xhrPosts.length === 1 &&
      win.__xhrPosts[0].url === "https://analysis.example/api/invoices" &&
      win.__xhrPosts[0].method === "POST",
    JSON.stringify(win.__xhrPosts.map((p) => `${p.method} ${p.url}`))
  );
  check(
    "the chosen file, not a placeholder, was sent",
    (win.__xhrPosts[0] && win.__xhrPosts[0].form.get("photo")?.name) === "march-invoice.jpg",
    win.__xhrPosts[0] ? String(win.__xhrPosts[0].form.get("photo")?.name) : "nothing sent"
  );
  check(
    "no self-upload endpoint is contacted",
    !win.__fetchStub.requested.some((r) => r.includes("/api/upload")),
    win.__fetchStub.requested.join(" | ")
  );
  check(
    "submission reported as accepted",
    /sent/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );

  console.log("\n--- 1d. the file input is opened by a button, not by display:none ---");
  check(
    "file input is not display:none",
    !/\.visually-hidden\s*\{[^}]*display/.test(css)
  );
  win.document.getElementById("retake").click();
  await tick();
  check(
    "'Choose another' reopens the picker",
    win.__pickerOpened > 0,
    `${win.__pickerOpened} open(s)`
  );
  check(
    "and does not start the camera",
    win.__cameraRequests === 0
  );

  console.log("\n--- 1e. unsupported and oversized files are rejected client-side ---");
  win = buildDom();
  await tick();
  await chooseMode(win, "upload");
  await pickFile(win, { name: "notes.pdf", type: "application/pdf" });
  check(
    "a PDF is rejected before uploading",
    /unsupported image type/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  check("nothing was uploaded", win.document.getElementById("upload").hidden === true);
  await pickFile(win, { name: "huge.jpg", type: "image/jpeg", bytes: 11 * 1024 * 1024 });
  check(
    "an oversized image is rejected before uploading",
    /limit is/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  check("still nothing uploaded", win.document.getElementById("upload").hidden === true);

  console.log("\n--- 1f. a type-less file is inferred from its extension ---");
  win = buildDom();
  await tick();
  await chooseMode(win, "upload");
  await pickFile(win, { name: "scan.png", type: "" });
  check(
    "an undeclared type is still accepted when the extension is known",
    win.document.getElementById("upload").hidden === false,
    win.document.getElementById("status").textContent
  );

  console.log("\n--- 2. choosing 'Take a photo' starts the camera ---");
  win = buildDom({ analysisApiBaseUrl: "https://analysis.example" });
  await tick();
  await chooseMode(win, "camera");
  s = snapshot(win, "after choosing camera");
  check("chooser gone", s.modePicker === false);
  check("workbench shown", s.workbench === true);
  check("camera requested once", win.__cameraRequests === 1, `${win.__cameraRequests}`);
  check("preview visible", s.preview === true);
  check("take-photo button visible", s.capture === true);
  check("choose-a-photo not offered in camera mode", s.pickFile === false);
  check("placeholder hidden", s.placeholder === false);

  console.log("\n--- 3. after taking a photo ---");
  win.document.getElementById("capture").click();
  await tick();
  s = snapshot(win, "after capture");
  check("still image visible", s.still === true);
  check("preview hidden", s.preview === false);
  check("retake visible after photo", s.retake === true);
  check("upload visible after photo", s.upload === true);
  check("take-photo button hidden", s.capture === false);

  console.log("\n--- 4. after clicking Retake ---");
  win.__placeholderShown.length = 0; // only care about the retake transition
  win.document.getElementById("retake").click();
  await tick();
  s = snapshot(win, "after retake");
  check("preview back", s.preview === true);
  check("still image hidden again", s.still === false);
  check("retake hidden again until next photo", s.retake === false);
  check("upload hidden again", s.upload === false);
  check("take-photo button visible again", s.capture === true);
  const flash = win.__placeholderShown.slice();
  console.log("  transient visibility events during retake:", JSON.stringify(flash));
  check(
    "no wait screen flashes during retake",
    !flash.some((e) => e.startsWith("WAIT-SCREEN")),
    flash.filter((e) => e.startsWith("WAIT-SCREEN")).join(" | ")
  );
  check(
    "still image never renders without a src",
    !flash.includes("STILL-VISIBLE-WITHOUT-SRC")
  );

  console.log("\n--- 5. second photo then send ---");
  win.document.getElementById("capture").click();
  await tick();
  win.document.getElementById("upload").click();
  await tick();
  s = snapshot(win, "after sending");
  check("retake stays available after sending", s.retake === true);
  check("upload hidden after success", s.upload === false);
  const status = win.document.getElementById("status").textContent;
  check("success message shown", /sent/i.test(status), status);

  console.log("\n--- 6. the backend rejects the photo ---");
  // The backend owns validation and storage, so its answer is what the user
  // must see. The photo stays on screen so it can be retried.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    xhrResponse: {
      status: 502,
      body: JSON.stringify({ detail: "Blob storage returned AuthenticationFailed (HTTP 403)." }),
    },
  });
  await tick();
  await captureAndUpload(win);
  await wait(150);
  check(
    "the backend's own message is shown",
    /AuthenticationFailed/.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  check(
    "the photo can be retried without recapturing",
    win.document.getElementById("upload").hidden === false &&
      win.document.getElementById("still").hidden === false
  );
  check("results panel stays hidden", win.document.getElementById("results").hidden === true);
  check(
    "no polling is started for a submission that was rejected",
    !win.__fetchStub.requested.some((r) => r.includes("/api/invoices/")),
    win.__fetchStub.requested.join(" | ")
  );

  console.log("\n--- 7. analysis pending, then complete ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ status: "processing" }, COMPLETE_STATUS],
  });
  await tick();
  await captureAndUpload(win);
  await wait(150);
  s = snapshot(win, "just after sending");
  check("results panel appears during analysis", s.results === true);
  check(
    "pending title shown first",
    win.document.getElementById("resultsTitle").textContent === "Reading invoice…",
    win.document.getElementById("resultsTitle").textContent
  );
  check(
    "the photo went to the backend in one request",
    win.__xhrPosts.length === 1 &&
      win.__xhrPosts[0].url === "https://analysis.example/api/invoices",
    JSON.stringify(win.__xhrPosts.map((p) => `${p.method} ${p.url}`))
  );
  check(
    "a captured blob is sent with a filename",
    win.__xhrPosts[0] && win.__xhrPosts[0].form.get("photo") instanceof win.Blob,
    win.__xhrPosts[0] ? String(win.__xhrPosts[0].form.get("photo")?.name) : "nothing sent"
  );

  // Two polls: the first 500ms, the next 750ms.
  await wait(2000);
  const title = win.document.getElementById("resultsTitle").textContent;
  check("title switches to extracted fields", title === "Extracted fields", title);
  check(
    "polled the status endpoint",
    win.__fetchStub.requested.filter((r) => r.includes("/api/invoices/inv-123")).length >= 2,
    win.__fetchStub.requested.join(" | ")
  );
  check(
    "document count summarised",
    /1 invoice found/.test(win.document.getElementById("resultsMessage").textContent),
    win.document.getElementById("resultsMessage").textContent
  );

  const names = [...win.document.querySelectorAll(".fields__name")].map((n) => n.textContent);
  check("VendorName shown as Merchant", names.includes("Merchant"), names.join(" | "));
  check(
    "unknown field names are humanised",
    names.includes("Some Odd Field Name"),
    names.join(" | ")
  );
  check("empty values are omitted", !names.includes("Empty Field"), names.join(" | "));
  check(
    "priority fields sort first",
    names.indexOf("Merchant") === 0,
    names.join(" | ")
  );

  const values = [...win.document.querySelectorAll(".fields__value")];
  const merchant = values.find((v) => v.textContent.includes("Acme"));
  check(
    "OCR markup is escaped, not rendered",
    !!merchant && merchant.querySelector("b") === null && merchant.textContent.includes("Acme <b>Ltd</b>"),
    merchant ? merchant.innerHTML : "not found"
  );
  const badges = [...win.document.querySelectorAll(".confidence")].map((b) => ({
    text: b.textContent,
    low: b.classList.contains("confidence--low"),
  }));
  check("high confidence rendered as a percentage", badges.some((b) => b.text === "99%"), JSON.stringify(badges));
  check(
    "low confidence is flagged",
    badges.some((b) => b.text === "40%" && b.low),
    JSON.stringify(badges)
  );
  check("document confidence shown", badges.some((b) => b.text === "98%"), JSON.stringify(badges));

  console.log("\n--- 8. analysis failure ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ status: "failed", error: "No invoice was recognised." }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  check(
    "failure title shown",
    win.document.getElementById("resultsTitle").textContent === "Analysis failed",
    win.document.getElementById("resultsTitle").textContent
  );
  check(
    "failure reason surfaced",
    /No invoice was recognised/.test(win.document.getElementById("resultsMessage").textContent),
    win.document.getElementById("resultsMessage").textContent
  );
  check(
    "the submission itself is still reported as accepted",
    /sent/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );

  console.log("\n--- 9. a new photo discards stale analysis ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ status: "processing" }, COMPLETE_STATUS],
  });
  await tick();
  await captureAndUpload(win);
  await wait(150);
  check("results shown before retake", !win.document.getElementById("results").hidden === true);
  win.document.getElementById("retake").click();
  await tick();
  s = snapshot(win, "after retake during analysis");
  check("results hidden on the new photo", s.results === false);
  const callsAtRetake = win.__fetchStub.requested.length;
  await wait(2000);
  check(
    "stale polling stopped",
    !win.document.getElementById("results").hidden === false &&
      win.__fetchStub.requested.length <= callsAtRetake + 1,
    win.__fetchStub.requested.join(" | ")
  );

  console.log("\n--- 10. camera denied ---");
  win = buildDom({ cameraAvailable: false });
  await tick();
  await chooseMode(win, "camera");
  s = snapshot(win, "denied");
  check("still image hidden", s.still === false);
  check("preview hidden", s.preview === false);
  check("retake hidden", s.retake === false);
  check("take-photo disabled", win.document.getElementById("capture").disabled === true);
  check(
    "permission message shown",
    /permission/i.test(win.document.getElementById("placeholderText").textContent),
    win.document.getElementById("placeholderText").textContent
  );
  check("retry button offered", win.document.getElementById("retry").hidden === false);

  console.log("\n--- 11. a denied camera is not a dead end ---");
  // The chooser is gone once a mode is picked, so reload to confirm the other
  // route is still reachable from scratch.
  win = buildDom({ cameraAvailable: false });
  await tick();
  await chooseMode(win, "upload");
  await pickFile(win, { name: "fallback.jpg" });
  check(
    "upload still works when the camera is unavailable",
    win.document.getElementById("upload").hidden === false,
    win.document.getElementById("status").textContent
  );
  check("and no camera was requested", win.__cameraRequests === 0);

  console.log("\n--- 12. is the photo good enough? ---");
  const verdict = (w) => {
    const g = (id) => w.document.getElementById(id);
    return {
      visible: !g("verdict").hidden,
      question: g("verdictQuestion").textContent.trim(),
      retakeLabel: g("retakePhoto").textContent.trim(),
      note: g("verdictNote").hidden,
      okay: g("photoOkay").hidden,
      retake: g("retakePhoto").hidden,
      status: g("verdictStatus").textContent,
    };
  };

  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  let r = verdict(win);
  check("asked once the fields are on screen", r.visible === true, JSON.stringify(r));
  check(
    "the question asks whether the values are right",
    /values correct/i.test(r.question),
    r.question
  );
  check("both answers offered", r.okay === false && r.retake === false, JSON.stringify(r));
  check("the note is offered with the question", r.note === false, JSON.stringify(r));
  check(
    "it says the photo is deleted, so retaking is the only option",
    /deleted/i.test(
      win.document.getElementById("verdict").textContent.replace(/\s+/g, " ")
    )
  );
  check(
    "nothing is answered before the user answers",
    win.__fetchStub.decisions.length === 0,
    JSON.stringify(win.__fetchStub.decisions)
  );

  win.document.getElementById("photoOkay").click();
  await wait(300);
  r = verdict(win);
  check(
    "saying yes posts to the photo resource",
    win.__fetchStub.decisions.length === 1 &&
      win.__fetchStub.decisions[0].method === "POST" &&
      win.__fetchStub.decisions[0].url ===
        "https://analysis.example/api/invoices/inv-123/photo",
    JSON.stringify(win.__fetchStub.decisions)
  );
  check(
    "and nothing more is said about it",
    r.status === "",
    JSON.stringify(r.status)
  );
  check(
    "and the other answer is not offered again",
    r.okay === true,
    JSON.stringify(r)
  );
  check("the answer is settled", /done/i.test(r.question), r.question);
  check(
    "the note about the deleted photo goes too",
    r.note === true,
    JSON.stringify(r)
  );
  check(
    "the retake button now offers the next invoice, not a retake",
    r.retake === false && r.retakeLabel === "Choose another photo",
    JSON.stringify(r)
  );

  console.log("\n--- 13. asking for a retake ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  check(
    "the values are on screen to be judged",
    win.document.getElementById("resultsBody").textContent.includes("Acme")
  );
  win.document.getElementById("retakePhoto").click();
  await wait(300);
  r = verdict(win);
  check(
    "asking for a retake uses DELETE",
    win.__fetchStub.decisions.length === 1 &&
      win.__fetchStub.decisions[0].method === "DELETE" &&
      win.__fetchStub.decisions[0].satisfied === false,
    JSON.stringify(win.__fetchStub.decisions)
  );
  // The values were never stored, and a rejected reading must not linger on
  // screen where the reviewer could still accept them by mistake.
  check(
    "the rejected values are taken away, not left to be accepted by accident",
    win.document.getElementById("results").hidden === true &&
      !win.document.getElementById("resultsBody").textContent.includes("Acme"),
    JSON.stringify({
      hidden: win.document.getElementById("results").hidden,
      body: win.document.getElementById("resultsBody").textContent.trim(),
    })
  );
  check("and the question goes with them", r.visible === false, JSON.stringify(r));
  // Camera mode, so a new photo means a live preview again rather than the
  // picker. Anything that leaves the reviewer on a blank page fails here.
  check(
    "a new photo is actually started, not merely suggested",
    win.__cameraRequests > 0 && win.document.getElementById("capture") !== null,
    `camera requests: ${win.__cameraRequests}`
  );
  check(
    "and the old photo is not still on the stage",
    win.document.getElementById("still").hidden === true,
    "the rejected photo is still displayed"
  );

  console.log("\n--- 14. the question respects what the service already knows ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: true }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  r = verdict(win);
  check(
    "an answered invoice is not asked again",
    r.okay === true,
    JSON.stringify(r)
  );
  check("and the answer is shown", /done/i.test(r.question), r.question);
  check(
    "and the note is gone with it",
    r.note === true,
    JSON.stringify(r)
  );
  check(
    "but the next invoice is still one click away",
    r.retake === false && r.retakeLabel === "Choose another photo",
    JSON.stringify(r)
  );

  console.log("\n--- 15. no question before there is something to judge ---");
  for (const [label, statuses] of [
    ["while it is still running", [{ status: "pending" }, { status: "pending" }, { status: "pending" }]],
    ["when the reading failed", [{ status: "failed", error: "Too blurry." }, { status: "failed" }]],
  ]) {
    win = buildDom({ analysisApiBaseUrl: "https://analysis.example", statuses });
    await tick();
    await captureAndUpload(win);
    await wait(1200);
    check(`not asked ${label}`, verdict(win).visible === false, JSON.stringify(verdict(win)));
  }

  console.log("\n--- 16. a lost job is reported, not waited out ---");
  // A row exists only once an analysis succeeds, so a 404 means the service holds
  // neither a result nor any memory of the job. Retrying would spin until the
  // deadline, so the user is told instead.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [],
    config: {},
    lostJob: true,
  });
  await tick();
  await captureAndUpload(win);
  await wait(1500);
  {
    const text = win.document.getElementById("resultsMessage").textContent;
    check("a 404 is terminal", /lost track/i.test(text), text);
    check("it does not claim to still be working", /still waiting/i.test(text) === false, text);
  }

  console.log("\n--- 17. a failed verdict is reported, not swallowed ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
    photoDecision: () => ({
      ok: false,
      status: 500,
      detail: "The service could not save that.",
    }),
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  win.document.getElementById("retakePhoto").click();
  await wait(300);
  r = verdict(win);
  check("the error is shown", /could not save that/i.test(r.status), r.status);
  check("the service's reason is kept", /not saved/i.test(r.status), r.status);
  check(
    "the question stays open so it can be retried",
    r.visible === true && r.okay === false && r.retake === false,
    JSON.stringify(r)
  );
  check("and the buttons work again", win.document.getElementById("photoOkay").disabled === false);

  console.log("\n--- 18. a new photo clears the previous question ---");
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  check("asked for the first photo", verdict(win).visible === true);
  win.document.getElementById("retakePhoto").click();
  await wait(200);
  // The results panel's own "Retake" control, which is a different button.
  win.document.getElementById("retake").click();
  await tick();
  check("gone once the user starts again", verdict(win).visible === false, JSON.stringify(verdict(win)));

  console.log("\n--- 18b. one way onward, not two ---");
  // The review bar's "Choose another photo" and the verdict's retake button used
  // to both offer to start again, sitting a few centimetres apart.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  const reviewBar = win.document.getElementById("retake");
  check(
    "the review bar's choose-another is withdrawn once a result is up",
    reviewBar.hidden === true,
    JSON.stringify({ hidden: reviewBar.hidden, label: reviewBar.textContent.trim() })
  );
  check(
    "so the verdict panel is the only way onward",
    verdict(win).retake === false,
    JSON.stringify(verdict(win))
  );

  // Confirming turns that single button into the offer for the next invoice.
  win.document.getElementById("photoOkay").click();
  await wait(300);
  const settled = verdict(win);
  check(
    "confirming labels it for the next invoice",
    settled.retakeLabel === "Choose another photo",
    JSON.stringify(settled)
  );
  win.document.getElementById("retakePhoto").click();
  await tick();
  check(
    "and pressing it starts a new photo",
    win.document.getElementById("results").hidden === true,
    "the results stayed on screen"
  );

  // A failure gets no verdict panel, so the review bar has to come back or the
  // reviewer has no way to try again.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ status: "failed", error: "Too blurry." }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);
  const afterFailure = win.document.getElementById("retake");
  check(
    "a failure still offers a way to start again",
    afterFailure.hidden === false && verdict(win).visible === false,
    JSON.stringify({
      reviewBar: afterFailure.hidden,
      verdict: verdict(win).visible,
    })
  );

  console.log("\n--- 18c. a retake from an uploaded file ---");
  // The same promise has to hold in the other mode: an uploaded photo is
  // replaced by a fresh file chooser, not by a dead end.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await chooseMode(win, "upload");
  await pickFile(win, { name: "invoice.jpg" });
  win.document.getElementById("upload").click();
  await wait(1200);
  check(
    "the values are on screen to be judged",
    win.document.getElementById("resultsBody").textContent.includes("Acme"),
    win.document.getElementById("status").textContent
  );
  win.document.getElementById("retakePhoto").click();
  await wait(300);
  win.document.getElementById("retakePhoto").click();
  await wait(300);
  check(
    "a retake opens the file picker",
    win.__pickerOpened === 1,
    `the file picker was opened ${win.__pickerOpened} time(s)`
  );
  check(
    "and the rejected values are gone",
    win.document.getElementById("results").hidden === true,
    "the rejected values are still on screen"
  );

  console.log("\n--- 19. a photo that has already been processed ---");
  // The service deliberately does not analyse a repeat, because Document
  // Intelligence bills per page. There is no new result, so the app must not
  // fetch or display one: it says the photo was already processed and stops.
  const submitAndSettle = async (alreadyProcessed) => {
    const w = buildDom({
      analysisApiBaseUrl: "https://analysis.example",
      statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
      xhrResponse: { alreadyProcessed },
    });
    await tick();
    await captureAndUpload(w);
    await wait(1200);
    return {
      said: w.document.getElementById("status").textContent,
      polls: w.__fetchStub.requested.filter((url) => /\/api\/invoices\/inv-123$/.test(url)),
      results: w.document.getElementById("results").hidden,
      body: w.document.getElementById("resultsBody").textContent,
    };
  };

  const repeat = await submitAndSettle(true);
  check(
    "it says the photo was already processed",
    /already been processed/i.test(repeat.said),
    repeat.said
  );
  check(
    "it does not claim to be reading it now",
    !/reading the invoice/i.test(repeat.said),
    repeat.said
  );
  check(
    "and it does not wait for a result that will not come",
    repeat.polls.length === 0,
    JSON.stringify(repeat.polls)
  );
  check("no results are shown", repeat.results === true, JSON.stringify(repeat));
  check(
    "so no fields are rendered either",
    !repeat.body.includes("Acme"),
    JSON.stringify(repeat.body)
  );

  const fresh = await submitAndSettle(false);
  check("a new photo reads normally", /^Sent\. Reading the invoice/.test(fresh.said), fresh.said);
  check("and its result is fetched", fresh.polls.length > 0, JSON.stringify(fresh.polls));
  check("and shown", fresh.body.includes("Acme"), JSON.stringify(fresh.body));

  console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nAll checks passed.");
  process.exit(failures.length ? 1 : 0);
})();
