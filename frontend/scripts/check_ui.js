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
  monthlyAllowance: 500,
  documentsRemaining: 499,
  documents: [
    {
      index: 0,
      confidence: 0.98,
      fields: {
        // Deliberately contains markup and special characters: OCR output is
        // untrusted input, and the review screen must keep them as text.
        VendorName: { value: "Acme <b>Ltd</b> & Sons", confidence: 0.99 },
        InvoiceTotal: { value: "120.00 EUR", confidence: 0.9 },
        InvoiceDate: { value: "2026-03-01", confidence: 0.4 },
        SubTotal: { value: "100.00 EUR", confidence: null },
        TotalTax: { value: "10.00 EUR", confidence: 0.9 },
        // The backend keeps only the five stored fields, so whatever else the
        // model returns must not even show up here. PreviousUnpaidBalance,
        // AmountDue and a customer name are all below the whitelist.
        PreviousUnpaidBalance: { value: "50.00 EUR", confidence: 0.8 },
        AmountDue: { value: "120.00", confidence: 0.6 },
        CustomerName: { value: "Buyer Corp", confidence: 0.9 },
        EmptyField: { value: "", confidence: 0.9 },
      },
    },
  ],
};

// A document the model read no merchant from. The merchant row must still be on
// screen, empty, prompting for a value, and the document must be impossible to
// accept until one is typed.
const NO_MERCHANT_STATUS = {
  status: "complete",
  documentCount: 1,
  documents: [
    {
      index: 0,
      confidence: 0.95,
      fields: {
        InvoiceDate: { value: "2026-03-01", confidence: 0.7 },
        InvoiceTotal: { value: "120.00 EUR", confidence: 0.9 },
        SubTotal: { value: "100.00 EUR", confidence: 0.8 },
        TotalTax: { value: "10.00 EUR", confidence: 0.9 },
      },
    },
  ],
};

// A document the model read no total tax from. Like a missing merchant, the
// total tax row must still be on screen, empty, warning, and impossible to
// accept without a value.
const NO_TOTAX_STATUS = {
  status: "complete",
  documentCount: 1,
  documents: [
    {
      index: 0,
      confidence: 0.95,
      fields: {
        VendorName: { value: "Acme Ltd", confidence: 0.99 },
        InvoiceDate: { value: "2026-03-01", confidence: 0.7 },
        InvoiceTotal: { value: "120.00 EUR", confidence: 0.9 },
        SubTotal: { value: "100.00 EUR", confidence: 0.8 },
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
      decisions.push({ satisfied, url, method, body: options.body || null });
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
  htmlOverride = null,
} = {}) {
  const dom = new JSDOM(htmlOverride || html, {
    runScripts: "outside-only",
    url: "https://localhost/",
  });
  const win = dom.window;
  const doc = win.document;

  // Any uncaught error or rejected promise during the run is a failure even if
  // the visible flags happen to come out right: a thrown helper can silently
  // strand a flow half-way.
  win.__errors = [];
  win.addEventListener("error", (e) => win.__errors.push(String(e.error || e.message)));
  win.addEventListener("unhandledrejection", (e) =>
    win.__errors.push("rejection: " + (e.reason && e.reason.message ? e.reason.message : e.reason))
  );

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

  // So a photo has known pixel dimensions for the crop math.
  const still = doc.getElementById("still");
  Object.defineProperty(still, "naturalWidth", { value: 1000, configurable: true });
  Object.defineProperty(still, "naturalHeight", { value: 1400, configurable: true });

  // Record every canvas draw: the real-world lossiness of capture and crop
  // cannot run in jsdom, but the source rectangle they ask for can.
  const canvasDraws = [];
  win.__canvasDraws = canvasDraws;
  const origCreate = doc.createElement.bind(doc);
  doc.createElement = (tag) => {
    if (String(tag).toLowerCase() !== "canvas") return origCreate(tag);
    const canvas = {
      width: 0,
      height: 0,
      getContext: () => ({
        translate() {},
        scale() {},
        drawImage(src, sx, sy, sw, sh) {
          canvasDraws.push({
            canvas: { width: canvas.width, height: canvas.height },
            source: { x: sx, y: sy, width: sw, height: sh },
          });
        },
      }),
      toBlob(cb) {
        cb(new win.Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" }));
      },
    };
    return canvas;
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
  check(
    "the OS file chooser opens on the first click",
    win.__pickerOpened === 1,
    `${win.__pickerOpened} open(s)`
  );
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

  console.log("\n--- 1b1. a stale shell without the allowance line still uploads ---");
  // A cached older page predates the allowance line. The newer app.js must not
  // let the missing element stop the upload route from revealing the picker.
  const staleShell = html.replace(/[^\S\n]*<p[^>]*id="resultsAllowance"[^>]*><\/p>\n/, "");
  check(
    "the stale shell really lacks the allowance line",
    !/id="resultsAllowance"/.test(staleShell)
  );
  win = buildDom({ htmlOverride: staleShell });
  await tick();
  await chooseMode(win, "upload");
  s = snapshot(win, "after choosing upload on a stale shell");
  check("chooser gone on a stale shell", s.modePicker === false);
  check("workbench shown on a stale shell", s.workbench === true);
  check(
    "choose-a-photo still offered on a stale shell",
    s.pickFile === true,
    "the picker button must still appear"
  );
  check(
    "no uncaught error from the missing allowance line",
    win.__errors.length === 0,
    win.__errors.join(" | ")
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
    "the file was posted to the frontend's own origin",
    win.__xhrPosts.length === 1 &&
      win.__xhrPosts[0].url === "/api/invoices" &&
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

  console.log("\n--- 2z. zoom, to fill the frame before the photo is taken ---");
  // The zoom is a slider on the live preview. It must be reachable only while
  // the camera is live, and the captured photo must ask the canvas for exactly
  // the zoomed centre of the feed, so the whole resolution is spent on the
  // invoice, not the table around it.
  function lastCanvasDraw(win_) {
    const draws = win_.__canvasDraws || [];
    return draws[draws.length - 1];
  }
  function pointer(win_, type, target, x, y) {
    target.dispatchEvent(
      new win_.PointerEvent(type, { clientX: x, clientY: y, pointerId: 1, buttons: 1, bubbles: true })
    );
  }
  const zw = buildDom({ analysisApiBaseUrl: "https://analysis.example" });
  await tick();
  await chooseMode(zw, "camera");
  check(
    "the zoom control is offered with the live preview",
    zw.document.getElementById("zoomControl").hidden === false &&
      zw.document.getElementById("zoomSlider") &&
      (zw.document.getElementById("zoomValue").textContent || "").trim() === "1.0×",
    zw.document.getElementById("zoomValue").textContent.trim()
  );
  const zSlider = zw.document.getElementById("zoomSlider");
  zSlider.value = "2";
  zSlider.dispatchEvent(new zw.Event("input"));
  check(
    "the readout follows the slider",
    (zw.document.getElementById("zoomValue").textContent || "").trim() === "2.0×",
    zw.document.getElementById("zoomValue").textContent
  );
  check(
    "the live preview carries the zoom",
    (zw.document.getElementById("preview").style.transform || "").includes("scale(2)"),
    zw.document.getElementById("preview").style.transform
  );
  zw.document.getElementById("capture").click();
  await tick();
  const zoomedDraw = lastCanvasDraw(zw);
  check(
    "the captured photo is the zoomed centre of the frame",
    !!zoomedDraw &&
      zoomedDraw.canvas.width === 1280 &&
      zoomedDraw.canvas.height === 720 &&
      Math.abs(zoomedDraw.source.width - 640) <= 1 &&
      Math.abs(zoomedDraw.source.height - 360) <= 1 &&
      Math.abs(zoomedDraw.source.x - 320) <= 1 &&
      Math.abs(zoomedDraw.source.y - 180) <= 1,
    JSON.stringify(zoomedDraw)
  );
  check(
    "zooming down back to 1× restores the full frame",
    (() => {
      zSlider.value = "1";
      zSlider.dispatchEvent(new zw.Event("input"));
      return (zw.document.getElementById("preview").style.transform || "") === "";
    })(),
    zw.document.getElementById("preview").style.transform
  );
  check(
    "the zoom control is withdrawn once the photo is taken",
    zw.document.getElementById("zoomControl").hidden === true
  );

  console.log("\n--- 3. after taking a photo ---");
  win.document.getElementById("capture").click();
  await tick();
  s = snapshot(win, "after capture");
  check("still image visible", s.still === true);
  check("preview hidden", s.preview === false);
  check("retake visible after photo", s.retake === true);
  check("upload visible after photo", s.upload === true);
  check("take-photo button hidden", s.capture === false);
  check("the crop action is offered with a photo",
    win.document.getElementById("crop").hidden === false,
    "crop button not shown"
  );

  console.log("\n--- 3b. cropping a photo ---");
  // The crop tool cuts the selection out and makes it the new photo: the
  // canvas is asked for only the selected region, and cancelling leaves the
  // photo exactly as it was.
  const cw = buildDom({ analysisApiBaseUrl: "https://analysis.example" });
  await tick();
  await chooseMode(cw, "camera");
  cw.document.getElementById("capture").click();
  await tick();
  const drawsBefore = cw.__canvasDraws.length;
  cw.document.getElementById("crop").click();
  await tick();
  const cropOverlay = cw.document.querySelector(".crop");
  const cropSelection = cw.document.querySelector(".crop__selection");
  check(
    "the crop tool opens over the photo",
    !!cropOverlay && !!cropSelection,
    cropOverlay ? "overlay shown" : "no overlay"
  );
  check(
    "starting selection covers the whole photo",
    !!cropSelection &&
      cropSelection.style.left === "0%" &&
      cropSelection.style.top === "0%" &&
      cropSelection.style.width === "100%" &&
      cropSelection.style.height === "100%",
    cropSelection ? cropSelection.outerHTML : "no selection"
  );
  check(
    "with a handle in each corner",
    cropOverlay.querySelectorAll(".crop__handle[data-corner]").length === 4,
    String(cropOverlay ? cropOverlay.querySelectorAll(".crop__handle[data-corner]").length : 0)
  );
  // Resize from the bottom-right handle, then drag the box to centre-left.
  const se = cropSelection.querySelector('[data-corner="se"]');
  pointer(cw, "pointerdown", se, 500, 500);
  pointer(cw, "pointermove", cw.document, 440, 440);
  pointer(cw, "pointerup", cw.document, 440, 440);
  check(
    "dragging a corner resizes the selection",
    cropSelection.style.width === "94%" && cropSelection.style.height === "94%",
    `${cropSelection.style.width}/${cropSelection.style.height}/${cropSelection.style.left}/${cropSelection.style.top}`
  );
  pointer(cw, "pointerdown", cropSelection, 200, 200);
  pointer(cw, "pointermove", cw.document, 250, 250);
  pointer(cw, "pointerup", cw.document, 250, 250);
  check(
    "dragging the box moves it without changing its size",
    cropSelection.style.left === "5%" && cropSelection.style.top === "5%" &&
      cropSelection.style.width === "94%" && cropSelection.style.height === "94%",
    `${cropSelection.style.left}/${cropSelection.style.top}/${cropSelection.style.width}/${cropSelection.style.height}`
  );
  const apply = [...cropOverlay.querySelectorAll("button")].find((b) => /apply crop/i.test(b.textContent));
  apply.click();
  await tick();
  const croppedDraw = lastCanvasDraw(cw);
  check(
    "applying reads exactly the selected region",
    !!croppedDraw &&
      croppedDraw.source.x === 50 &&
      croppedDraw.source.y === 70 &&
      croppedDraw.source.width === 940 &&
      croppedDraw.source.height === 1316 &&
      croppedDraw.canvas.width === 940 &&
      croppedDraw.canvas.height === 1316,
    JSON.stringify(croppedDraw)
  );
  check(
    "the crop tool closes once applied",
    !cw.document.querySelector(".crop"),
    "overlay still open"
  );
  check(
    "and the photo is still there to upload",
    !cw.document.getElementById("crop").hidden &&
      cw.document.getElementById("still").hidden === false
  );
  check(
    "the cropped photo was not re-read from the camera",
    cw.__canvasDraws[drawsBefore - 1] && cw.__canvasDraws.length === drawsBefore + 1,
    JSON.stringify(cw.__canvasDraws.slice(drawsBefore))
  );
  // Cancelling after a partial change must not re-encode anything.
  cw.document.getElementById("crop").click();
  await tick();
  const cancelOverlay = cw.document.querySelector(".crop");
  const cancelSelection = cw.document.querySelector(".crop__selection");
  pointer(cw, "pointerdown", cancelSelection.querySelector('[data-corner="se"]'), 500, 500);
  pointer(cw, "pointermove", cw.document, 400, 400);
  pointer(cw, "pointerup", cw.document, 400, 400);
  [...cancelOverlay.querySelectorAll("button")].find((b) => /cancel/i.test(b.textContent)).click();
  await tick();
  check(
    "cancelling closes the tool without re-encoding the photo",
    !cw.document.querySelector(".crop") &&
      cw.__canvasDraws.length === drawsBefore + 1,
    JSON.stringify(cw.__canvasDraws.slice(drawsBefore))
  );

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
    "the photo went to the frontend in one request",
    win.__xhrPosts.length === 1 &&
      win.__xhrPosts[0].url === "/api/invoices",
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
  check(
    "the remaining monthly allowance is shown with the result",
    win.document.getElementById("resultsAllowance").textContent ===
      "499 of 500 documents left this month.",
    win.document.getElementById("resultsAllowance").textContent
  );
  check(
    "the allowance line is visible once an analysis is done",
    !win.document.getElementById("resultsAllowance").hidden,
    String(win.document.getElementById("resultsAllowance").hidden)
  );

  const names = [...win.document.querySelectorAll(".fields__name")].map((n) => n.textContent);
  check("VendorName shown as Merchant", names.includes("Merchant"), names.join(" | "));
  check(
    "only the reviewed fields are on the screen",
    names.join(" | ") ===
      "Merchant | Date | Invoice total | Subtotal | Total tax",
    names.join(" | ")
  );
  check(
    "fields outside the stored whitelist are not shown",
    !names.includes("Previous unpaid balance") && !names.includes("Amount due"),
    names.join(" | ")
  );
  check(
    "empty non-required values are omitted",
    names.length === 5,
    names.join(" | ")
  );
  check(
    "priority fields sort first",
    names.indexOf("Merchant") === 0,
    names.join(" | ")
  );
  check(
    "there is no add-a-field dropdown",
    !win.document.querySelector(".fields__add") &&
      !win.document.querySelector(".fields__add-select"),
    "dropdown still present"
  );

  const values = [...win.document.querySelectorAll(".fields__input")];
  const merchant = values.find((input) => input.value.includes("Acme"));
  check(
    "OCR markup is escaped, not rendered",
    !!merchant && merchant.value === "Acme <b>Ltd</b> & Sons",
    merchant ? merchant.value : "not found"
  );
  check(
    "every value is an editable input",
    values.length === 5,
    `${values.length} input(s)`
  );
  check(
    "money is shown without its currency suffix",
    values.filter((input) => /^\d+\.\d{2}$/.test(input.value)).length === 3 &&
      values.some((input) => input.value === "120.00") &&
      values.some((input) => input.value === "100.00") &&
      values.some((input) => input.value === "10.00"),
    values.map((input) => input.value).join(" | ")
  );
  check(
    "the date is a calendar field showing the ISO value",
    values.some(
      (input) =>
        input.getAttribute("type") === "date" && input.value === "2026-03-01"
    ),
    values.map((input) => input.value).join(" | ")
  );
  check(
    "confirming is labelled Upload my invoice",
    win.document.getElementById("photoOkay").textContent.trim() === "Upload my invoice",
    win.document.getElementById("photoOkay").textContent.trim()
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
  check(
    "no allowance line when the service did not report one",
    win.document.getElementById("resultsAllowance").hidden &&
      win.document.getElementById("resultsAllowance").textContent === "",
    `${win.document.getElementById("resultsAllowance").hidden} / ` +
      win.document.getElementById("resultsAllowance").textContent
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
  // Values now live in editable inputs, whose contents are not in textContent.
  const valuesOnScreen = (w) =>
    [...w.document.querySelectorAll(".fields__input")].map((input) => input.value).join(" | ");

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
      win.__fetchStub.decisions[0].url === "/api/invoices/inv-123/photo",
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
    valuesOnScreen(win).includes("Acme")
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
      !valuesOnScreen(win).includes("Acme"),
    JSON.stringify({
      hidden: win.document.getElementById("results").hidden,
      body: valuesOnScreen(win).trim(),
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
    valuesOnScreen(win).includes("Acme"),
    win.document.getElementById("status").textContent
  );
  // Choosing "Upload a photo" itself already opened the chooser once; count
  // only what the retake does from here.
  win.__pickerOpened = 0;
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
      body: valuesOnScreen(w),
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

  console.log("\n--- 20. fixing, sanitising and validating fields before accepting ---");
  const inputField = (field) => {
    const row = [...win.document.querySelectorAll(".fields__row")]
      .find((r) => r.dataset.field === field);
    return row ? row.querySelector(".fields__input") : null;
  };

  // A fresh reading with a single correction: the untouched values must not
  // travel in the corrections payload.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);

  const dueDate = [...win.document.querySelectorAll(".fields__input")]
    .find((input) => input.getAttribute("type") === "date" && input.value === "2026-03-01");
  check(
    "an extracted date is editable through its calendar field",
    !!dueDate && dueDate instanceof win.HTMLInputElement,
    valuesOnScreen(win)
  );
  if (dueDate) {
    dueDate.value = "2026-04-01";
    dueDate.dispatchEvent(new win.Event("input"));
  }
  // Correcting an extracted value withdraws its confidence badge: the score
  // described the reading, not the value the user just typed.
  check(
    "editing a value withdraws its confidence badge",
    !!dueDate &&
      dueDate.closest(".fields__value").querySelector(".confidence").hidden === true
  );
  win.document.getElementById("photoOkay").click();
  await wait(300);
  const accepted = win.__fetchStub.decisions[win.__fetchStub.decisions.length - 1];
  check(
    "accepting sends the corrections",
    !!accepted && accepted.method === "POST" && typeof accepted.body === "string",
    JSON.stringify(accepted)
  );
  let correctedFields = {};
  try {
    const payload = JSON.parse(accepted.body);
    correctedFields = ((payload.documents || [])[0] || {}).fields || {};
  } catch {
    // Reported by the check below.
  }
  check(
    "the corrections carry the edited date",
    correctedFields.InvoiceDate && correctedFields.InvoiceDate.value === "2026-04-01",
    JSON.stringify(correctedFields)
  );
  check(
    "and nothing the reviewer left alone",
    !correctedFields.VendorName && !correctedFields.InvoiceTotal &&
      !correctedFields.SubTotal,
    JSON.stringify(correctedFields)
  );

  // Text cannot be typed into a money field, and whatever is pasted is
  // sanitised into a number the moment it lands in the input.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);

  const money = [...win.document.querySelectorAll(".fields__input")]
    .find((input) => input.value === "120.00");
  check(
    "money is shown as a bare two-decimal number",
    !!money,
    valuesOnScreen(win)
  );
  if (money) {
    money.value = "abc129.50xyz";
    money.dispatchEvent(new win.Event("input"));
  }
  check(
    "letters typed into a money field are stripped",
    inputField("InvoiceTotal").value === "129.50",
    inputField("InvoiceTotal").value
  );
  check(
    "a pasted command is reduced to its numbers/dots",
    !/abc|xyz|<|>|&/.test(inputField("InvoiceTotal").value),
    inputField("InvoiceTotal").value
  );

  // The merchant keeps exactly what was read until edited, and then only
  // letters, numbers and spaces survive: no markup, no command syntax.
  const merchantInput = inputField("VendorName");
  check(
    "an untouched merchant keeps exactly what was read",
    merchantInput.value === "Acme <b>Ltd</b> & Sons",
    merchantInput.value
  );
  merchantInput.value = "Acme <script>x</script> Ltd!";
  merchantInput.dispatchEvent(new win.Event("input"));
  check(
    "markup and punctuation are stripped from a merchant",
    merchantInput.value === "Acme scriptxscript Ltd" && !/[<>&'"]/.test(merchantInput.value),
    merchantInput.value
  );

  // Adding a missing field (TotalTax) is no longer possible: the add-a-field
  // dropdown is gone, so the reviewer edits only what the model returned or
  // fills the required merchant where the model found none.
  check(
    "no add-a-field dropdown is present",
    !win.document.querySelector(".fields__add-select"),
    valuesOnScreen(win)
  );

  // Accepting while an edited value does not fit is refused, not sent.
  const moneyField = inputField("InvoiceTotal");
  moneyField.value = "12.";
  moneyField.dispatchEvent(new win.Event("input"));
  const decisionsBefore = win.__fetchStub.decisions.length;
  win.document.getElementById("photoOkay").click();
  await wait(300);
  check(
    "accepting an edited value that does not fit is refused",
    win.__fetchStub.decisions.length === decisionsBefore &&
      /Invoice total/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  moneyField.value = "12.50";
  moneyField.dispatchEvent(new win.Event("input"));
  win.document.getElementById("photoOkay").click();
  await wait(300);
  const clean = win.__fetchStub.decisions[win.__fetchStub.decisions.length - 1];
  let cleanFields = {};
  try {
    cleanFields = ((JSON.parse(clean.body).documents || [])[0] || {}).fields || {};
  } catch {
    // Reported by the check below.
  }
  check(
    "the cleaned money travels in the corrections",
    cleanFields.InvoiceTotal && cleanFields.InvoiceTotal.value === "12.50",
    JSON.stringify(cleanFields)
  );
  check(
    "and the cleaned merchant does",
    cleanFields.VendorName &&
      cleanFields.VendorName.value === "Acme scriptxscript Ltd",
    JSON.stringify(cleanFields)
  );
  check(
    "while untouched values stay off the wire",
    !cleanFields.SubTotal && !cleanFields.TotalTax,
    JSON.stringify(cleanFields)
  );

  console.log("\n--- 20b. a missing merchant is required, not optional ---");
  // A document Document Intelligence read no merchant from shows the merchant
  // row empty and prompting, and the document cannot be stored until the
  // reviewer types one.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...NO_MERCHANT_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);

  const merchantRow = [...win.document.querySelectorAll(".fields__row")]
    .find((row) => row.dataset.field === "VendorName");
  const merchantBox = merchantRow && merchantRow.querySelector(".fields__input");
  const merchantError = merchantRow && merchantRow.querySelector(".fields__error");
  check(
    "a missing merchant is shown as an empty editable row",
    !!merchantBox && merchantBox.value === "",
    valuesOnScreen(win)
  );
  check(
    "labelled Merchant, ready to type into",
    !!merchantRow &&
      merchantRow.querySelector(".fields__name").textContent === "Merchant",
    valuesOnScreen(win)
  );
  check(
    "with a prompt that it is required",
    !!merchantError && !merchantError.hidden &&
      /required/i.test(merchantError.textContent),
    merchantError ? merchantError.textContent : "no error row"
  );
  check(
    "and there is no add control to find it in",
    !win.document.querySelector(".fields__add-select"),
    valuesOnScreen(win)
  );
  const blockedDecisions = win.__fetchStub.decisions.length;
  win.document.getElementById("photoOkay").click();
  await wait(300);
  check(
    "accepting without a merchant is refused",
    win.__fetchStub.decisions.length === blockedDecisions &&
      /merchant/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  merchantBox.value = "New Vendor Ltd";
  merchantBox.dispatchEvent(new win.Event("input"));
  check(
    "typing one withdraws the prompt",
    !merchantError.hidden === false,
    merchantError.textContent
  );
  win.document.getElementById("photoOkay").click();
  await wait(300);
  const merchantDecision = win.__fetchStub.decisions[win.__fetchStub.decisions.length - 1];
  let merchantPayload = {};
  try {
    merchantPayload = JSON.parse(merchantDecision.body);
  } catch {
    // Reported by the check below.
  }
  const merchantFields = ((merchantPayload.documents || [])[0] || {}).fields || {};
  check(
    "with a merchant typed it is stored",
    merchantFields.VendorName && merchantFields.VendorName.value === "New Vendor Ltd",
    JSON.stringify(merchantFields)
  );
  check(
    "and the untouched values still stay off the wire",
    !merchantFields.InvoiceTotal && !merchantFields.InvoiceDate,
    JSON.stringify(merchantFields)
  );

  console.log("\n--- 20c. a missing total tax is required, like the merchant ---");
  // TotalTax is the merchant's partner in requiredness: a document the model
  // read none from shows the row empty and warning, and the document cannot be
  // stored until a valid amount is typed.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...NO_TOTAX_STATUS, photoSatisfied: null }],
  });
  await tick();
  await captureAndUpload(win);
  await wait(1200);

  const taxRow = [...win.document.querySelectorAll(".fields__row")]
    .find((row) => row.dataset.field === "TotalTax");
  const taxBox = taxRow && taxRow.querySelector(".fields__input");
  const taxError = taxRow && taxRow.querySelector(".fields__error");
  check(
    "a missing total tax is shown as an empty editable row",
    !!taxBox && taxBox.value === "",
    valuesOnScreen(win)
  );
  check(
    "labelled Total tax, ready to type into",
    !!taxRow &&
      taxRow.querySelector(".fields__name").textContent === "Total tax",
    valuesOnScreen(win)
  );
  check(
    "with a red warning that it is required",
    !!taxError && !taxError.hidden &&
      /required/i.test(taxError.textContent),
    taxError ? taxError.textContent : "no error row"
  );
  const blockedTax = win.__fetchStub.decisions.length;
  win.document.getElementById("photoOkay").click();
  await wait(300);
  check(
    "accepting without a total tax is refused",
    win.__fetchStub.decisions.length === blockedTax &&
      /total tax/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  taxBox.value = "12.5"; // needs two decimals
  taxBox.dispatchEvent(new win.Event("input"));
  win.document.getElementById("photoOkay").click();
  await wait(300);
  check(
    "a total tax without two decimals is still refused",
    win.__fetchStub.decisions.length === blockedTax &&
      /total tax/i.test(win.document.getElementById("status").textContent),
    win.document.getElementById("status").textContent
  );
  taxBox.value = "12.50";
  taxBox.dispatchEvent(new win.Event("input"));
  check(
    "a valid amount withdraws the warning",
    !taxError.hidden === false,
    taxError.textContent
  );
  win.document.getElementById("photoOkay").click();
  await wait(300);
  const taxDecision = win.__fetchStub.decisions[win.__fetchStub.decisions.length - 1];
  let taxPayload = {};
  try {
    const payload = JSON.parse(taxDecision.body);
    taxPayload = ((payload.documents || [])[0] || {}).fields || {};
  } catch {
    // Reported by the check below.
  }
  check(
    "with a total tax typed it is stored",
    taxPayload.TotalTax && taxPayload.TotalTax.value === "12.50",
    JSON.stringify(taxPayload)
  );
  check(
    "and the untouched merchant stays off the wire",
    !taxPayload.VendorName,
    JSON.stringify(taxPayload)
  );

  console.log("\n--- 21. the same editing and validation works for an uploaded photo ---");
  // The upload path must offer exactly the same correction controls as the
  // camera path: both reach the same review screen, so there is no reason for
  // an uploaded invoice to be fixed by hand and a photographed one not.
  win = buildDom({
    analysisApiBaseUrl: "https://analysis.example",
    statuses: [{ ...COMPLETE_STATUS, photoSatisfied: null }],
  });
  await tick();
  await chooseMode(win, "upload");
  await pickFile(win);
  await wait(300); // the preview renders, then Upload is reachable
  win.document.getElementById("upload").click();
  await wait(1200);

  const uploadInputs = win.document.querySelectorAll(".fields__input");
  check(
    "an uploaded photo's fields are editable too",
    uploadInputs.length > 0,
    valuesOnScreen(win)
  );
  check(
    "and there is no add-a-field dropdown either",
    !win.document.querySelector(".fields__add-select"),
    valuesOnScreen(win)
  );

  const due = [...uploadInputs].find(
    (input) => input.getAttribute("type") === "date" && input.value === "2026-03-01"
  );
  due.value = "2026-05-01";
  due.dispatchEvent(new win.Event("input"));
  win.document.getElementById("photoOkay").click();
  await wait(300);
  const uploadDecision = win.__fetchStub.decisions[win.__fetchStub.decisions.length - 1];
  let uploadPayload = {};
  try {
    uploadPayload = JSON.parse(uploadDecision.body);
  } catch {
    // Reported by the check below.
  }
  const uploadFields = ((uploadPayload.documents || [])[0] || {}).fields || {};
  check(
    "accepting an uploaded photo sends its corrections",
    uploadFields.InvoiceDate && uploadFields.InvoiceDate.value === "2026-05-01",
    JSON.stringify(uploadFields)
  );

  console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nAll checks passed.");
  process.exit(failures.length ? 1 : 0);
})();
