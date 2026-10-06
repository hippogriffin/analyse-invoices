/**
 * Two ways to add an invoice: capture one with the camera, or upload a file
 * that already exists. The chooser decides which; everything after that is
 * shared. The chosen photo is posted to the analysis service, which stores it,
 * reads the invoice fields and reports the result. This app holds no storage
 * credentials of its own and never sees an Azure response.
 */
(() => {
  "use strict";

  const els = {
    banner: document.getElementById("banner"),
    modePicker: document.getElementById("modePicker"),
    chooseCamera: document.getElementById("chooseCamera"),
    chooseFile: document.getElementById("chooseFile"),
    fileInput: document.getElementById("fileInput"),
    pickFile: document.getElementById("pickFile"),
    workbench: document.getElementById("workbench"),
    preview: document.getElementById("preview"),
    still: document.getElementById("still"),
    placeholder: document.getElementById("placeholder"),
    placeholderText: document.getElementById("placeholderText"),
    retry: document.getElementById("retry"),
    capture: document.getElementById("capture"),
    retake: document.getElementById("retake"),
    upload: document.getElementById("upload"),
    reviewActions: document.getElementById("reviewActions"),
    progress: document.getElementById("progress"),
    progressBar: document.getElementById("progressBar"),
    status: document.getElementById("status"),
    results: document.getElementById("results"),
    resultsTitle: document.getElementById("resultsTitle"),
    resultsMessage: document.getElementById("resultsMessage"),
    resultsBody: document.getElementById("resultsBody"),
    verdict: document.getElementById("verdict"),
    verdictQuestion: document.getElementById("verdictQuestion"),
    verdictNote: document.getElementById("verdictNote"),
    photoOkay: document.getElementById("photoOkay"),
    retakePhoto: document.getElementById("retakePhoto"),
    verdictStatus: document.getElementById("verdictStatus"),
  };

  const state = {
    stream: null,
    // A Blob from the camera, or the File the user picked.
    photo: null,
    photoUrl: null,
    mirror: false,
    analysisApiBaseUrl: "",
    analysisConfigured: false,
    maxUploadBytes: 0,
    allowedContentTypes: [],
    // "camera" | "upload" | null while the chooser is still showing.
    mode: null,
    // Bumped whenever a new photo is captured, so an in-flight poll for the
    // previous photo stops instead of overwriting the new results.
    analysisRun: 0,
    // The invoice currently on screen, and its keep/discard decision.
    invoiceId: "",
    photoSatisfied: null,
  };

  // Analysis usually finishes within a few seconds, but the free tier can be
  // slow. This is only the client-side give-up point; the service also
  // resolves jobs abandoned by a restart as failed.
  const ANALYSIS_DEADLINE_MS = 180000;
  const POLL_MIN_MS = 500;
  const POLL_MAX_MS = 4000;

  // Shown first, because these are the fields people actually look for.
  const PRIORITY_FIELDS = [
    "VendorName",
    "InvoiceId",
    "InvoiceDate",
    "DueDate",
    "InvoiceTotal",
    "AmountDue",
  ];

  // VendorName reads as "Merchant" to match the existing downstream schema.
  const FIELD_LABELS = {
    VendorName: "Merchant",
    VendorAddress: "Merchant address",
    VendorTaxId: "Merchant tax ID",
    CustomerName: "Customer",
    CustomerAddress: "Customer address",
    CustomerTaxId: "Customer tax ID",
    CustomerRegistrationId: "Customer registration ID",
    InvoiceId: "Invoice number",
    InvoiceDate: "Invoice date",
    DueDate: "Due date",
    InvoiceTotal: "Invoice total",
    AmountDue: "Amount due",
    AmountPaid: "Amount paid",
    SubTotal: "Subtotal",
    Taxes: "Tax",
    VATAmount: "VAT amount",
    VATTotal: "VAT total",
    Discount: "Discount",
    ShippingAmount: "Shipping",
    PaymentTerm: "Payment terms",
    ServiceAddress: "Service address",
    CustomerServiceRef: "Customer reference",
    BillingPeriodStart: "Billing period start",
    BillingPeriodEnd: "Billing period end",
  };

  const RESULTS_TITLES = {
    pending: "Reading invoice…",
    complete: "Extracted fields",
    failed: "Analysis failed",
  };

  // Only used when the browser reports no content type for a picked file.
  // Mirrors the server's CONTENT_TYPE_EXTENSIONS.
  const EXTENSION_TYPES = {
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    png: "image/png",
    webp: "image/webp",
  };

  const CAMERA_ERRORS = {
    NotAllowedError:
      "Camera permission was denied. Allow camera access in your browser settings and try again.",
    PermissionDeniedError:
      "Camera permission was denied. Allow camera access in your browser settings and try again.",
    NotFoundError: "No camera was found on this device.",
    DevicesNotFoundError: "No camera was found on this device.",
    NotReadableError:
      "The camera is already in use by another application. Close it and try again.",
    OverconstrainedError: "No camera matched the requested settings.",
    SecurityError: "Camera access is blocked in this context.",
  };

  init();

  async function init() {
    els.chooseCamera.addEventListener("click", () => chooseMode("camera"));
    els.chooseFile.addEventListener("click", () => chooseMode("upload"));
    els.pickFile.addEventListener("click", openFilePicker);
    els.fileInput.addEventListener("change", onFilePicked);
    els.capture.addEventListener("click", capturePhoto);
    els.retake.addEventListener("click", retake);
    els.upload.addEventListener("click", uploadPhoto);
    els.retry.addEventListener("click", () => startCamera({ waitScreen: true }));
    els.photoOkay.addEventListener("click", () => sendVerdict(true));
    els.retakePhoto.addEventListener("click", onRetakePhoto);
    window.addEventListener("pagehide", stopCamera);

    await loadConfig();
    // The chooser stays up until the user picks a route, so the browser is not
    // asked for camera permission the moment the page loads.
    showModePicker();
  }

  /**
   * Shows the take-photo / upload-photo choice. Neither the camera nor the
   * file input is touched until a button is pressed.
   */
  function showModePicker() {
    els.modePicker.hidden = false;
    els.workbench.hidden = true;
    stopCamera();
    els.still.hidden = true;
    els.preview.hidden = true;
    hideResults();
    hideProgress();
    els.placeholder.hidden = true;
  }

  function chooseMode(mode) {
    state.mode = mode;
    els.modePicker.hidden = true;
    els.workbench.hidden = false;
    if (mode === "upload") return enterUploadMode();
    return startCamera({ waitScreen: true });
  }

  /** Reads the service settings, including where the photo should be posted. */
  async function loadConfig() {
    try {
      const response = await fetch("/api/config");
      if (!response.ok) return;
      const config = await response.json();
      state.analysisApiBaseUrl = (config.analysisApiBaseUrl || "").replace(/\/+$/, "");
      state.analysisConfigured = Boolean(config.analysisConfigured);
      state.maxUploadBytes = Number(config.maxUploadBytes) || 0;
      state.allowedContentTypes = (config.allowedContentTypes || []).map((type) =>
        String(type).toLowerCase()
      );
      // Drives the OS file chooser, so it offers the formats the backend accepts.
      if (state.allowedContentTypes.length) {
        els.fileInput.accept = state.allowedContentTypes.join(",");
      }
      if (!state.analysisConfigured) {
        showBanner(
          "No analysis service is configured (ANALYSIS_API_BASE_URL). You can capture a photo, but there is nowhere to send it."
        );
      }
    } catch {
      // Non-fatal: the submit attempt will report any real problem.
    }
  }

  /* ------------------------------------------------------------- file upload */

  /**
   * Entering upload mode. The stage keeps its size and the file input is
   * offered as the primary action, so the page does not jump between modes.
   */
  function enterUploadMode() {
    stopCamera();
    setStatus("");
    hideBanner();
    // A new selection invalidates any analysis still running for the last one.
    state.analysisRun += 1;
    hideResults();
    hideProgress();
    clearPhoto();

    els.capture.hidden = true;
    els.capture.disabled = true;
    els.retry.hidden = true;
    els.reviewActions.hidden = true;
    els.upload.hidden = true;
    els.pickFile.hidden = false;
    showPlaceholder("Choose an invoice photo from this device.");
  }

  function openFilePicker() {
    els.fileInput.click();
  }

  function onFilePicked() {
    const file = els.fileInput.files && els.fileInput.files[0];
    // Cleared so choosing the same file twice still fires a change event.
    els.fileInput.value = "";
    if (!file) return;
    const problem = validateFile(file);
    if (problem) return setStatus(problem, "error");
    setStatus("");
    showPhoto(file);
  }

  /**
   * Checks the file against the same limits the server enforces, so an
   * unsupported photo is rejected before it is uploaded. Returns an error
   * message, or an empty string when the file is acceptable.
   */
  function validateFile(file) {
    if (state.maxUploadBytes && file.size > state.maxUploadBytes) {
      return `That photo is ${formatBytes(file.size)}. The limit is ${formatBytes(
        state.maxUploadBytes
      )}.`;
    }
    // A browser that reports no type for a file leaves this empty; the server
    // then decides, and answers with its own message.
    const type = resolveType(file);
    if (type && state.allowedContentTypes.length
        && !state.allowedContentTypes.includes(type)) {
      return `Unsupported image type ${type}. Allowed: ${state.allowedContentTypes.join(
        ", "
      )}.`;
    }
    return "";
  }

  /** The file's own type, or one inferred from its extension. */
  function resolveType(file) {
    const declared = String(file.type || "").split(";")[0].trim().toLowerCase();
    if (declared) return declared;
    const extension = String(file.name || "").split(".").pop().toLowerCase();
    return EXTENSION_TYPES[extension] || "";
  }

  function formatBytes(bytes) {
    if (bytes >= 1024 * 1024) return `${Math.round(bytes / (1024 * 1024))} MB`;
    if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${bytes} bytes`;
  }

  async function startCamera({ waitScreen = false } = {}) {
    stopCamera();
    setStatus("");
    hideBanner();
    // A new photo invalidates any analysis still running for the last one.
    state.analysisRun += 1;
    hideResults();
    els.still.hidden = true;

    if (waitScreen) {
      showPlaceholder("Starting camera…", { spinner: true });
    } else {
      // Retaking: keep the stage as it is so no "starting" screen flashes and
      // the layout never jumps. The preview element stays up, briefly black,
      // until the new stream is attached.
      hidePlaceholder();
      els.preview.hidden = false;
    }

    if (!window.isSecureContext) {
      return failCamera(
        "Camera access requires a secure context. Open this page over HTTPS or on localhost."
      );
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      return failCamera("This browser does not support camera access.");
    }

    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } },
        audio: false,
      });
    } catch (error) {
      const message =
        CAMERA_ERRORS[error?.name] || `Could not start the camera: ${error?.message}`;
      return failCamera(message);
    }

    els.preview.srcObject = state.stream;
    try {
      await els.preview.play();
    } catch {
      // Autoplay can be blocked; the stream is still attached and visible.
    }
    await waitForMetadata(els.preview);

    state.mirror = isFrontFacing(state.stream);
    els.preview.classList.toggle("is-mirrored", state.mirror);

    hidePlaceholder();
    els.preview.hidden = false;
    els.still.hidden = true;
    els.pickFile.hidden = true;
    els.retry.hidden = true;
    els.capture.hidden = false;
    els.capture.disabled = false;
    els.capture.textContent = "Take photo";
    els.retake.textContent = "Retake";
    els.upload.hidden = true;
    els.reviewActions.hidden = true;
    hideProgress();
  }

  function capturePhoto() {
    const video = els.preview;
    const width = video.videoWidth;
    const height = video.videoHeight;
    if (!width || !height) {
      setStatus("The camera is not ready yet. Try again in a moment.", "error");
      return;
    }

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (state.mirror) {
      context.translate(width, 0);
      context.scale(-1, 1);
    }
    context.drawImage(video, 0, 0, width, height);

    canvas.toBlob(
      (blob) => {
        if (!blob) {
          setStatus("Could not read the image from the camera.", "error");
          return;
        }
        showPhoto(blob);
      },
      "image/jpeg",
      0.92
    );
  }

  /** Shows a captured or picked image, and offers Upload. */
  function showPhoto(photo) {
    state.photo = photo;
    releasePhotoUrl();
    state.photoUrl = URL.createObjectURL(photo);

    els.still.src = encodeURI(state.photoUrl);
    els.still.hidden = false;
    els.preview.hidden = true;
    hidePlaceholder();
    stopCamera();

    els.capture.hidden = true;
    els.pickFile.hidden = true;
    els.reviewActions.hidden = false;
    els.upload.hidden = false;
    els.upload.disabled = false;
    els.upload.textContent = "Upload";
    els.retake.textContent = state.mode === "upload" ? "Choose another" : "Retake";
    setStatus(
      state.mode === "upload"
        ? "Photo selected. Upload it or choose another."
        : "Photo captured. Retake it or upload."
    );
  }

  function retake() {
    if (state.mode === "upload") {
      setStatus("");
      return openFilePicker();
    }
    // Hide before revoking, otherwise the visible <img> points at a revoked
    // object URL and briefly renders as a broken image.
    els.still.hidden = true;
    releasePhotoUrl();
    state.photo = null;
    setStatus("");
    startCamera();
  }

  function uploadPhoto() {
    if (!state.photo) {
      setStatus(
        state.mode === "upload"
          ? "Choose a photo before uploading."
          : "Capture a photo before uploading.",
        "error"
      );
      return;
    }
    if (!state.analysisConfigured) {
      setStatus(
        "No analysis service is configured, so there is nowhere to send this photo.",
        "error"
      );
      return;
    }

    const run = state.analysisRun;
    setBusy(true);
    setStatus("Sending…");
    hideResults();
    showProgress(0);

    const form = new FormData();
    // A picked file keeps its own name; a captured blob has none.
    form.append("photo", state.photo, state.photo.name || "invoice.jpg");

    // XHR rather than fetch: upload progress events are needed for the bar, and
    // a large photo should not be buffered into a second copy to send it.
    const request = new XMLHttpRequest();
    request.open("POST", `${state.analysisApiBaseUrl}/api/invoices`);

    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        showProgress((event.loaded / event.total) * 100);
      }
    });

    request.addEventListener("load", () => {
      setBusy(false);
      hideProgress();
      if (request.status < 200 || request.status >= 300) {
        setStatus(readError(request), "error");
        return;
      }

      let accepted = null;
      try {
        accepted = JSON.parse(request.responseText);
      } catch {
        // Handled by the message below.
      }
      const invoiceId = (accepted && accepted.invoiceId) || "";
      if (!invoiceId) {
        setStatus("The service did not return an invoice id.", "error");
        return;
      }

      els.upload.hidden = true;
      // The verdict panel owns going again once a result is on screen, so the
      // review bar's "Choose another photo" is withdrawn here rather than left
      // to compete with it. It comes back if the analysis fails, since that is
      // the one case where there is no verdict to answer.
      els.retake.hidden = true;
      // A repeat is not analysed again, because the service bills per page, so
      // there is no new result to wait for and none is fetched. The reviewer is
      // told why nothing is happening, which is the whole answer.
      if (accepted.alreadyProcessed) {
        setStatus("This photo has already been processed.", "success");
        return;
      }
      setStatus("Sent. Reading the invoice…", "success");
      pollAnalysis(state.analysisApiBaseUrl, invoiceId, run);
    });

    request.addEventListener("error", () => {
      setBusy(false);
      hideProgress();
      setStatus("Network error while sending. Check your connection.", "error");
    });

    request.send(form);
  }

  function readError(request) {
    try {
      const detail = JSON.parse(request.responseText).detail;
      if (detail) return detail;
    } catch {
      // Fall through to the generic message.
    }
    return `Upload failed (HTTP ${request.status}).`;
  }

  /* ---------------------------------------------------------------- analysis */

  async function pollAnalysis(base, invoiceId, run) {
    const deadline = Date.now() + ANALYSIS_DEADLINE_MS;
    let delay = POLL_MIN_MS;
    showResults({ view: "pending", message: "The service is reading the invoice." });

    while (Date.now() < deadline) {
      await sleep(delay);
      delay = Math.min(Math.round(delay * 1.5), POLL_MAX_MS);
      if (run !== state.analysisRun) return; // a newer photo took over

      let response;
      try {
        response = await fetch(`${base}/api/invoices/${invoiceId}`);
      } catch {
        // A dropped poll is not a failed analysis; keep trying until the deadline.
        continue;
      }
      if (response.status === 404) {
        // A row only exists once an analysis has succeeded, so 404 means the
        // service has no record and no held state: the job is unknown, which
        // happens if the service restarted mid-analysis. Retrying would only
        // spin until the deadline, so say so instead.
        if (run !== state.analysisRun) return;
        return showResults({
          view: "failed",
          message:
            "The service lost track of this photo, most likely because it " +
            "restarted. Send the photo again.",
        });
      }
      if (!response.ok) {
        continue;
      }

      let body;
      try {
        body = await response.json();
      } catch {
        continue;
      }

      if (body.status === "complete") {
        return showResults({
          view: "complete",
          message: summaryFor(body),
          documents: body.documents,
          invoiceId,
          photoSatisfied: body.photoSatisfied,
        });
      }
      if (body.status === "failed") {
        return showResults({
          view: "failed",
          message: body.error || "The invoice could not be read.",
        });
      }
      showResults({ view: "pending", message: "The service is reading the invoice." });
    }

    if (run === state.analysisRun) {
      showResults({
        view: "failed",
        message:
          "Still waiting on the analysis service. The photo was accepted, so it can be retried.",
      });
    }
  }

  function summaryFor(body) {
    const count = body.documentCount ?? body.documents?.length ?? 0;
    if (!count) return "No invoice fields were found.";
    return count === 1
      ? "1 invoice found in the photo."
      : `${count} invoices found in the photo.`;
  }

  /* ------------------------------------------------------- the photo verdict */

  /**
   * Ask whether the photo was good enough, once the fields are on screen.
   *
   * The photo itself has already been deleted by the time the fields arrive, so
   * this is not about keeping the image. It is the one chance to say the photo
   * was too blurry, crooked or cut off to read, which is a judgement only the
   * person who took it can make.
   *
   * Asked only while the answer is outstanding, and only for a completed
   * analysis: a failed job has no trustworthy reading to judge the photo
   * against, and an answer already given is not worth asking again.
   */
  function renderVerdict(invoiceId, photoSatisfied, complete) {
    state.invoiceId = invoiceId;
    state.photoSatisfied = photoSatisfied;
    els.verdictStatus.textContent = "";

    if (photoSatisfied === true) {
      // Accepted. The question and its note only exist to ask for a retake, so
      // the note goes with the answer. The button stays, but it stops being about
      // this photo: the reading is settled, so it now offers the next invoice
      // rather than inviting the user to undo a decision they have made.
      els.verdictQuestion.textContent = "Thanks - this invoice is done.";
      els.verdictNote.hidden = true;
      els.photoOkay.hidden = true;
      els.retakePhoto.textContent = "Choose another photo";
      els.retakePhoto.hidden = false;
      els.verdict.hidden = false;
      return;
    }
    if (photoSatisfied === false) {
      // Rejected: the retake has already been requested, so neither answer is
      // offered again. Leaving a button up would invite a second, contradictory
      // answer to a question that has been dealt with. The note stays, because
      // it explains why the photo cannot simply be kept and tried again.
      els.verdictQuestion.textContent = "Take another photo of this invoice.";
      hideVerdictAnswers();
      els.verdictNote.hidden = false;
      els.verdict.hidden = false;
      return;
    }
    if (!complete) {
      els.verdict.hidden = true;
      return;
    }
    els.verdictQuestion.textContent = "Are these values correct?";
    els.verdictNote.hidden = false;
    els.photoOkay.hidden = false;
    els.retakePhoto.textContent = "No, retake it";
    els.retakePhoto.hidden = false;
    els.verdict.hidden = false;
  }

  /** Withdraw both answer buttons once the question has been dealt with. */
  function hideVerdictAnswers() {
    els.photoOkay.hidden = true;
    els.retakePhoto.hidden = true;
  }

  function hideVerdict() {
    els.verdict.hidden = true;
    els.verdictStatus.textContent = "";
    state.invoiceId = "";
    state.photoSatisfied = null;
  }

  /** One button, two jobs, decided by whether the reading has been accepted. */
  function onRetakePhoto() {
    if (state.photoSatisfied === true) {
      // Already accepted, so this is not a retake of this invoice any more. It
      // is the offer of the next one, and recording a false verdict here would
      // contradict the answer the user has just given.
      return retake();
    }
    return sendVerdict(false);
  }

  async function sendVerdict(satisfied) {
    if (!state.invoiceId) return;
    const invoiceId = state.invoiceId;
    // Disabled rather than hidden, so the panel does not jump, and so a second
    // click cannot fire a second request.
    els.photoOkay.disabled = true;
    els.retakePhoto.disabled = true;
    els.verdictStatus.textContent = satisfied
      ? "Saving..."
      : "Getting the camera ready...";

    try {
      const response = await fetch(
        `${state.analysisApiBaseUrl}/api/invoices/${invoiceId}/photo`,
        { method: satisfied ? "POST" : "DELETE" }
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      // A new photo may have taken over while the request was in flight.
      if (state.invoiceId !== invoiceId) return;
      if (!satisfied) {
        // The reading has been thrown away, so there is nothing left to show and
        // nothing left to ask. Saying "noted" and leaving the rejected values on
        // screen would invite the reviewer to accept them anyway, and waiting for
        // them to find the button would leave them stuck. The answer was a
        // request for a different photo, so a different photo is what happens.
        hideResults();
        retake();
        return;
      }
      // Confirming needs no line of its own: the question has become
      // "Thanks - this invoice is done.", so a second confirmation would only
      // repeat it.
      renderVerdict(invoiceId, body.photoSatisfied, true);
    } catch (error) {
      if (state.invoiceId !== invoiceId) return;
      renderVerdict(invoiceId, state.photoSatisfied, true);
      els.verdictStatus.textContent = `That was not saved: ${error.message}`;
    } finally {
      els.photoOkay.disabled = false;
      els.retakePhoto.disabled = false;
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function showResults({ view, message, documents, invoiceId, photoSatisfied }) {
    els.results.hidden = false;
    els.resultsTitle.className = `results__title results__title--${view}`;
    els.resultsTitle.textContent = RESULTS_TITLES[view] || "Analysis";
    els.resultsMessage.textContent = message || "";
    els.resultsMessage.hidden = !message;
    els.resultsBody.replaceChildren();
    (documents || []).forEach((document_, index) => {
      els.resultsBody.appendChild(renderDocument(document_, index, documents.length));
    });
    // A finished analysis gets asked about the photo, and that panel is where
    // going again is offered. A failure is asked nothing, so the review bar has
    // to hand the way back or the reviewer is stranded with no way to retry. A
    // pending view is neither: it is on its way to one of the two.
    if (view === "failed") {
      els.retake.textContent =
        state.mode === "upload" ? "Choose another photo" : "Take another photo";
      els.retake.hidden = false;
    }
    // Only a finished analysis gets asked about; a pending view must not flash
    // the question and then take it away.
    renderVerdict(
      invoiceId || "",
      photoSatisfied === undefined ? null : photoSatisfied,
      view === "complete"
    );
  }

  function hideResults() {
    els.results.hidden = true;
    els.resultsBody.replaceChildren();
    hideVerdict();
  }

  function renderDocument(document_, index, total) {
    const section = document.createElement("section");
    section.className = "results__document";

    const heading = document.createElement("h3");
    heading.className = "results__document-title";
    heading.textContent = total > 1 ? `Invoice ${index + 1}` : "Invoice";
    if (typeof document_.confidence === "number") {
      heading.appendChild(confidenceBadge(document_.confidence));
    }
    section.appendChild(heading);

    const fields = Object.entries(document_.fields || {}).filter(
      ([, field]) => field && field.value !== "" && field.value !== null && field.value !== undefined
    );
    if (!fields.length) {
      const empty = document.createElement("p");
      empty.className = "results__message";
      empty.textContent = "No fields were extracted from this invoice.";
      section.appendChild(empty);
      return section;
    }

    const list = document.createElement("dl");
    list.className = "fields";
    sortFields(fields).forEach(([name, field]) => {
      const row = document.createElement("div");
      row.className = "fields__row";

      const term = document.createElement("dt");
      term.className = "fields__name";
      term.textContent = fieldLabel(name);

      // Built with textContent, never innerHTML: these are OCR values and are
      // not trusted input.
      const value = document.createElement("dd");
      value.className = "fields__value";
      value.textContent = String(field.value);
      if (typeof field.confidence === "number") {
        value.appendChild(confidenceBadge(field.confidence));
      }

      row.append(term, value);
      list.appendChild(row);
    });
    section.appendChild(list);
    return section;
  }

  function confidenceBadge(confidence) {
    const badge = document.createElement("span");
    // Below 70% the reading is worth a human glance.
    badge.className = confidence < 0.7 ? "confidence confidence--low" : "confidence";
    badge.textContent = `${Math.round(confidence * 100)}%`;
    return badge;
  }

  function sortFields(fields) {
    const priority = (name) => {
      const at = PRIORITY_FIELDS.indexOf(name);
      return at === -1 ? PRIORITY_FIELDS.length : at;
    };
    return [...fields].sort(
      (a, b) => priority(a[0]) - priority(b[0]) || a[0].localeCompare(b[0])
    );
  }

  function fieldLabel(name) {
    if (FIELD_LABELS[name]) return FIELD_LABELS[name];
    const spaced = name
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
  }

  function setBusy(busy) {
    els.upload.disabled = busy;
    els.retake.disabled = busy;
    els.capture.disabled = busy;
  }

  function stopCamera() {
    if (state.stream) {
      state.stream.getTracks().forEach((track) => track.stop());
      state.stream = null;
    }
    els.preview.srcObject = null;
  }

  function releasePhotoUrl() {
    if (state.photoUrl) {
      URL.revokeObjectURL(state.photoUrl);
      state.photoUrl = null;
    }
  }

  /** Drops the current image and its object URL, without touching the camera. */
  function clearPhoto() {
    els.still.hidden = true;
    els.still.removeAttribute("src");
    releasePhotoUrl();
    state.photo = null;
  }

  function showPlaceholder(text, { spinner = false } = {}) {
    els.placeholderText.textContent = text;
    els.placeholder.querySelector(".spinner").hidden = !spinner;
    els.placeholder.hidden = false;
  }

  function hidePlaceholder() {
    els.placeholder.hidden = true;
  }

  function failCamera(message) {
    stopCamera();
    els.preview.hidden = true;
    els.still.hidden = true;
    els.capture.disabled = true;
    showPlaceholder(message);
    els.retry.hidden = false;
  }

  function showBanner(message) {
    els.banner.textContent = message;
    els.banner.hidden = false;
  }

  function hideBanner() {
    els.banner.hidden = true;
  }

  function setStatus(message, kind = "") {
    els.status.textContent = message;
    els.status.className = `status${kind ? ` status--${kind}` : ""}`;
  }

  function showProgress(percent) {
    els.progress.hidden = false;
    els.progressBar.style.width = `${Math.min(100, Math.round(percent))}%`;
  }

  function hideProgress() {
    els.progress.hidden = true;
    els.progressBar.style.width = "0%";
  }

  function isFrontFacing(stream) {
    const track = stream.getVideoTracks()[0];
    const facingMode = track?.getSettings?.().facingMode;
    return facingMode === "user";
  }

  function waitForMetadata(video) {
    if (video.readyState >= 1 && video.videoWidth > 0) return Promise.resolve();
    return new Promise((resolve) => {
      video.addEventListener("loadedmetadata", () => resolve(), { once: true });
    });
  }
})();
