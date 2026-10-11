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
    crop: document.getElementById("crop"),
    upload: document.getElementById("upload"),
    reviewActions: document.getElementById("reviewActions"),
    zoomControl: document.getElementById("zoomControl"),
    zoomSlider: document.getElementById("zoomSlider"),
    zoomValue: document.getElementById("zoomValue"),
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
    stage: document.querySelector(".stage"),
  };

  const state = {
    stream: null,
    // A Blob from the camera, or the File the user picked.
    photo: null,
    photoUrl: null,
    mirror: false,
    // 1..MAX_ZOOM, applied as a digital centre crop so the invoice fills the
    // frame while it is being captured.
    zoom: 1,
    // The crop selection as a percentage of the photo, plus the pointer
    // session that is dragging or resizing it.
    cropRect: null,
    cropCorner: null,
    cropStart: null,
    crop: null,
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
    // Becomes true when the reviewer changes, adds or removes any field value,
    // so that accepting sends those corrections instead of a plain confirmation.
    edited: false,
  };

  // Analysis usually finishes within a few seconds, but the free tier can be
  // slow. This is only the client-side give-up point; the service also
  // resolves jobs abandoned by a restart as failed.
  const ANALYSIS_DEADLINE_MS = 180000;
  const POLL_MIN_MS = 500;
  const POLL_MAX_MS = 4000;

  // Digital zoom is applied as a centre crop at capture time. More than this
  // starts looking soft even on a sharp photo.
  const MAX_ZOOM = 3;
  // The crop selection can be shrunk no further than this percentage of the
  // photo, so it never collapses to a sliver by accident.
  const MIN_CROP_PERCENT = 10;

  // The only fields a reviewer may correct, and the exact kind of value each may
  // hold. VendorName reads as "Merchant" and InvoiceDate as "Date" to match the
  // existing downstream schema. Everything else Document Intelligence returns is
  // dropped by the backend before it is even returned, so nothing outside this
  // list can be edited or injected.
  const REVIEWABLE_FIELDS = {
    // The merchant is required: a document with no merchant cannot be stored,
    // so it is shown as an empty row warning to be filled in.
    VendorName: { label: "Merchant", type: "text", required: true },
    // The date, invoice total and subtotal are required as well: a record
    // must have all of them before it is written. The date is picked from a
    // calendar, not typed, so the field is rendered as a date input.
    InvoiceDate: { label: "Date", type: "date", required: true },
    InvoiceTotal: { label: "Invoice total", type: "money", required: true },
    SubTotal: { label: "Subtotal", type: "money", required: true },
    // The total tax is required like the merchant: the document cannot be
    // stored without it, so an empty warning row asks for it too.
    TotalTax: { label: "Total tax", type: "money", required: true },
  };

  // Rendering order: merchants are what people look for first.
  const PRIORITY_FIELDS = Object.keys(REVIEWABLE_FIELDS);

  // How to say what an edited value must look like, per field type.
  const VALID_HINTS = {
    money: "an amount with two decimals, like 120.00",
    date: "a full date chosen from the calendar",
    text: "only letters, numbers and spaces",
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
  registerServiceWorker();

  async function init() {
    els.chooseCamera.addEventListener("click", () => chooseMode("camera"));
    els.chooseFile.addEventListener("click", () => chooseMode("upload"));
    els.pickFile.addEventListener("click", openFilePicker);
    els.fileInput.addEventListener("change", onFilePicked);
    els.capture.addEventListener("click", capturePhoto);
    els.retake.addEventListener("click", retake);
    els.crop.addEventListener("click", openCrop);
    els.zoomSlider.addEventListener("input", () =>
      setZoom(Number(els.zoomSlider.value))
    );
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
    hidePlaceholder();
    resetStage();
  }

  function chooseMode(mode) {
    state.mode = mode;
    els.modePicker.hidden = true;
    els.workbench.hidden = false;
    if (mode === "upload") return enterUploadMode();
    // A fresh start from the chooser zooms from 1×; a retake keeps the level
    // the previous photo was taken at, since that is usually still the right one.
    resetZoom();
    return startCamera({ waitScreen: true });
  }

  /**
   * Reads the service settings.
   *
   * The analysis backend's address is deliberately absent: the browser posts to
   * this page's own origin, and this service forwards to the backend, so the
   * browser never needs to know (or be able to resolve) where it lives.
   */
  async function loadConfig() {
    try {
      const response = await fetch("/api/config");
      if (!response.ok) return;
      const config = await response.json();
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
    resetStage();

    els.capture.hidden = true;
    els.capture.disabled = true;
    els.retry.hidden = true;
    els.reviewActions.hidden = true;
    els.upload.hidden = true;
    els.crop.hidden = true;
    els.zoomControl.hidden = true;
    els.pickFile.hidden = false;
    resetZoom();
    closeCrop();
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
    fitStage(els.preview.videoWidth, els.preview.videoHeight);

    state.mirror = isFrontFacing(state.stream);
    applyZoom();

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
    els.crop.hidden = true;
    els.reviewActions.hidden = true;
    els.zoomControl.hidden = false;
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

    // The zoom is a digital centre crop of the live frame, so the whole image
    // keeps its full resolution and the captured photo is exactly the part of
    // the feed the preview showed enlarged.
    const zoom = state.zoom;
    const sourceWidth = width / zoom;
    const sourceHeight = height / zoom;
    const sourceX = (width - sourceWidth) / 2;
    const sourceY = (height - sourceHeight) / 2;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (state.mirror) {
      context.translate(width, 0);
      context.scale(-1, 1);
    }
    context.drawImage(video, sourceX, sourceY, sourceWidth, sourceHeight, 0, 0, width, height);

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
    // The photo's dimensions are only known once it decodes; then the stage is
    // fitted to it so the reviewed photo fills the frame edge to edge.
    els.still.addEventListener("load", fitStageToStill, { once: true });
    els.still.hidden = false;
    els.preview.hidden = true;
    hidePlaceholder();
    stopCamera();

    els.capture.hidden = true;
    els.pickFile.hidden = true;
    els.crop.hidden = false;
    els.reviewActions.hidden = false;
    els.upload.hidden = false;
    els.upload.disabled = false;
    els.upload.textContent = "Upload";
    els.retake.textContent = state.mode === "upload" ? "Choose another" : "Retake";
    els.zoomControl.hidden = true;
    closeCrop();
    setStatus(
      state.mode === "upload"
        ? "Photo selected. Upload it or choose another."
        : "Photo captured. Retake it or upload."
    );
  }

  /* ------------------------------------------------------------- framing: zoom */

  /** Bakes the current zoom and mirror into the preview's transform.
   *
   * The mirror is folded in here (not a CSS class) so the two can coexist: an
   * inline transform overrides the class, so keeping them separate would lose
   * one or the other. The captured photo bakes the same framing into its
   * pixels, so what you saw is what is uploaded.
   */
  function applyZoom() {
    const flip = state.mirror ? "scaleX(-1) " : "";
    const scaled = state.zoom === 1 && !state.mirror ? "" : `${flip}scale(${state.zoom})`;
    els.preview.style.transform = scaled;
  }

  /** Sets the zoom, keeping state, the slider, the readout and the preview in
   * step. Clamped so a stray value cannot zoom the frame to nothing or beyond
   * the cap.
   */
  function setZoom(zoom) {
    state.zoom = Math.min(MAX_ZOOM, Math.max(1, zoom));
    els.zoomSlider.value = String(state.zoom);
    els.zoomValue.textContent = `${state.zoom.toFixed(1)}×`;
    applyZoom();
  }

  function resetZoom() {
    setZoom(1);
  }

  /* -------------------------------------------------------- framing: stage */

  /** Sizes the stage box to the media's intrinsic shape.
   *
   * The stage is the box both the preview/photo and the crop overlay live in,
   * so when the box does not match the media's aspect the image is letterboxed
   * (the black bars either side of a narrow photo) and the selection, drawn in
   * percent of the whole box, no longer lines up with the visible photo. Fitting
   * the box to the media keeps the container equal to the content: no bars, and
   * a selection percentage is the same fraction on screen and in the file.
   */
  function fitStage(width, height) {
    if (!width || !height) return;
    const ratio = width / height;
    els.stage.style.aspectRatio = String(ratio);
    // Bounded by the shell width and by the same 70dvh the stylesheet caps the
    // stage at, so fitting cannot outgrow the viewport. A width bound (rather
    // than the css max-height) is what keeps the ratio exact.
    els.stage.style.width = `min(100%, calc(70dvh * ${ratio}))`;
  }

  /** Fits the stage to the reviewed photo, once it has actual dimensions. */
  function fitStageToStill() {
    fitStage(els.still.naturalWidth, els.still.naturalHeight);
  }

  /** Returns the stage to its default shape when nothing is being shown. */
  function resetStage() {
    els.stage.style.aspectRatio = "3 / 4";
    els.stage.style.width = "";
  }

  /* ---------------------------------------------------------------- cropping */

  /** Opens the crop tool over the current photo.
   *
   * The selection is kept as a percentage of the photo, so it survives any
   * change in how the stage is sized, and the overlay is built fresh each time
   * so a cancelled session leaves no trace.
   */
  function openCrop() {
    if (!state.photo || state.crop) return;
    // The selection is drawn in percent of the stage box, so the box must be
    // the photo's own shape for the percent to describe the visible image.
    fitStageToStill();
    closeCrop();

    const overlay = document.createElement("div");
    overlay.className = "crop";

    const image = document.createElement("img");
    // Shares the stage__media rules so it is sized exactly like the reviewed
    // photo underneath; it is what the selection is drawn over.
    image.className = "stage__media crop__image";
    image.alt = "Crop selected photo";
    image.src = els.still.getAttribute("src");

    const selection = document.createElement("div");
    selection.className = "crop__selection";
    for (const corner of ["nw", "ne", "sw", "se"]) {
      const handle = document.createElement("span");
      handle.className = "crop__handle";
      handle.dataset.corner = corner;
      selection.appendChild(handle);
    }

    const bar = document.createElement("div");
    bar.className = "crop__bar";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "button button--primary";
    apply.textContent = "Apply crop";
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "button button--ghost";
    reset.textContent = "Reset";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button button--ghost";
    cancel.textContent = "Cancel";
    bar.append(apply, reset, cancel);

    overlay.append(image, selection, bar);
    els.still.parentElement.appendChild(overlay);

    apply.addEventListener("click", applyCrop);
    reset.addEventListener("click", resetCrop);
    cancel.addEventListener("click", closeCrop);
    window.addEventListener("keydown", onCropKeydown);
    overlay.addEventListener("pointerdown", onCropPointerDown);
    window.addEventListener("pointermove", onCropPointerMove);
    window.addEventListener("pointerup", onCropPointerEnd);

    state.crop = { overlay, selection };
    state.cropRect = fullCropRect();
    positionCropSelection();
  }

  function closeCrop() {
    window.removeEventListener("keydown", onCropKeydown);
    window.removeEventListener("pointermove", onCropPointerMove);
    window.removeEventListener("pointerup", onCropPointerEnd);
    if (state.crop) {
      state.crop.overlay.remove();
    }
    state.crop = null;
    state.cropRect = null;
    state.cropStart = null;
    state.cropCorner = null;
  }

  function onCropKeydown(event) {
    if (event.key === "Escape") closeCrop();
  }

  function fullCropRect() {
    return { left: 0, top: 0, width: 100, height: 100 };
  }

  /** Moves the visual box to match state.cropRect (both in percent). */
  function positionCropSelection() {
    if (!state.crop) return;
    const rect = state.cropRect;
    const selection = state.crop.selection;
    selection.style.left = `${rect.left}%`;
    selection.style.top = `${rect.top}%`;
    selection.style.width = `${rect.width}%`;
    selection.style.height = `${rect.height}%`;
  }

  /** Clamps a crop rect: the selection stays inside the photo and at least
   * MIN_CROP_PERCENT on each side, so it can always be resized back up.
   */
  function clampCropRect(rect) {
    const width = Math.min(100, Math.max(MIN_CROP_PERCENT, rect.width));
    const height = Math.min(100, Math.max(MIN_CROP_PERCENT, rect.height));
    const left = Math.min(100 - width, Math.max(0, rect.left));
    const top = Math.min(100 - height, Math.max(0, rect.top));
    return { left, top, width, height };
  }

  function setCropRect(rect) {
    state.cropRect = clampCropRect(rect);
    positionCropSelection();
  }

  /** How many crop-percent steps one screen pixel represents, taken from the
   * rendered image so the selection tracks the pointer 1:1. When nothing is
   * laid out yet the photo's intrinsic size stands in.
   */
  function cropPercentPerPixel() {
    const rect = els.still.getBoundingClientRect();
    if (rect.width > 0) return 100 / rect.width;
    return 100 / (els.still.naturalWidth || 1);
  }

  function onCropPointerDown(event) {
    const handle = event.target.closest
      ? event.target.closest(".crop__handle")
      : null;
    const onBox = event.target.classList?.contains("crop__selection");
    if (!handle && !onBox) return;
    state.cropCorner = handle ? handle.dataset.corner : null;
    state.cropStart = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      ...state.cropRect,
    };
    event.preventDefault();
  }

  function onCropPointerMove(event) {
    if (!state.cropStart || !state.cropRect) return;
    const perPixel = cropPercentPerPixel();
    const dx = (event.clientX - state.cropStart.pointerX) * perPixel;
    const dy = (event.clientY - state.cropStart.pointerY) * perPixel;
    const start = state.cropStart;
    const corner = state.cropCorner;

    if (!corner) {
      setCropRect({
        left: start.left + dx,
        top: start.top + dy,
        width: start.width,
        height: start.height,
      });
      return;
    }
    // The corners opposite the dragged one are anchored; only the two edges
    // sharing the dragged corner move.
    if (corner === "se") {
      setCropRect({ left: start.left, top: start.top, width: start.width + dx, height: start.height + dy });
    } else if (corner === "sw") {
      setCropRect({ left: start.left + dx, top: start.top, width: start.width - dx, height: start.height + dy });
    } else if (corner === "ne") {
      setCropRect({ left: start.left, top: start.top + dy, width: start.width + dx, height: start.height - dy });
    } else {
      setCropRect({ left: start.left + dx, top: start.top + dy, width: start.width - dx, height: start.height - dy });
    }
  }

  function onCropPointerEnd() {
    state.cropStart = null;
    state.cropCorner = null;
  }

  function resetCrop() {
    if (!state.crop) return;
    state.cropRect = fullCropRect();
    positionCropSelection();
  }

  /** The photo's size in pixels, for turning the percentage selection into a
   * real crop. A photo that has not decoded yet falls back to its rendered box,
   * which is at least something to keep the aspect from collapsing.
   */
  function imagePixelExtent() {
    const width = els.still.naturalWidth || els.still.getBoundingClientRect().width || 1;
    const height = els.still.naturalHeight || els.still.getBoundingClientRect().height || 1;
    return { width, height };
  }

  /** Cuts the selected region out of the photo and makes it the new photo. */
  function applyCrop() {
    if (!state.photo || !state.crop || !state.cropRect) return;
    const rect = state.cropRect;
    const image = imagePixelExtent();
    const width = Math.max(1, Math.round((rect.width / 100) * image.width));
    const height = Math.max(1, Math.round((rect.height / 100) * image.height));

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) {
      setStatus("Cropping is not supported here.", "error");
      return;
    }
    context.drawImage(
      els.still,
      Math.round((rect.left / 100) * image.width),
      Math.round((rect.top / 100) * image.height),
      width,
      height,
      0,
      0,
      width,
      height
    );

    canvas.toBlob(
      (blob) => {
        if (!blob) {
          setStatus("Could not crop the photo.", "error");
          return;
        }
        // An uploaded photo keeps its name through the crop; a captured blob
        // has none and needs no name.
        let cropped = blob;
        if (state.photo.name) {
          cropped = new File([blob], state.photo.name, {
            type: blob.type || state.photo.type,
          });
        }
        showCropped(cropped);
      },
      "image/jpeg",
      0.92
    );
  }

  /** Replaces the reviewed photo with a cropped one, then closes the tool. */
  function showCropped(photo) {
    state.photo = photo;
    releasePhotoUrl();
    state.photoUrl = URL.createObjectURL(photo);
    els.still.src = encodeURI(state.photoUrl);
    setStatus(
      state.mode === "upload"
        ? "Photo cropped. Upload it or choose another."
        : "Photo cropped. Retake it or upload."
    );
    closeCrop();
    // The cropped photo has its own dimensions; fit the stage once it decodes.
    els.still.addEventListener("load", fitStageToStill, { once: true });
  }

  function retake() {
    closeCrop();
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
    // a large photo should not be buffered into a second copy to send it. The
    // path is same-origin: this service forwards it to the analysis backend.
    const request = new XMLHttpRequest();
    request.open("POST", "/api/invoices");

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
      els.crop.hidden = true;
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
      pollAnalysis(invoiceId, run);
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

  async function pollAnalysis(invoiceId, run) {
    const deadline = Date.now() + ANALYSIS_DEADLINE_MS;
    let delay = POLL_MIN_MS;
    showResults({ view: "pending", message: "The service is reading the invoice." });

    while (Date.now() < deadline) {
      await sleep(delay);
      delay = Math.min(Math.round(delay * 1.5), POLL_MAX_MS);
      if (run !== state.analysisRun) return; // a newer photo took over

      let response;
      try {
        response = await fetch(`/api/invoices/${invoiceId}`);
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

    const options = { method: satisfied ? "POST" : "DELETE" };
    if (satisfied) {
      // The merchant is required, and a touched value must fit its field, so
      // neither a plain confirmation nor an edited one can store a document
      // without a merchant. The table is only ever written once it has one.
      const invalid = firstInvalidField();
      if (invalid) {
        els.photoOkay.disabled = false;
        els.retakePhoto.disabled = false;
        els.verdictStatus.textContent = "";
        setStatus(
          invalid.empty
            ? `Correct the ${invalid.reviewable.label.toLowerCase()} first; it is required.`
            : `Fix ${invalid.reviewable.label} first: enter ${VALID_HINTS[invalid.reviewable.type]}.`,
          "error"
        );
        return;
      }
      if (state.edited) {
        // The reviewer changed values, so the record must store what they
        // approved, not the original reading. A plain accept sends no body and
        // the stored extraction is kept as it is.
        options.headers = { "content-type": "application/json" };
        options.body = JSON.stringify(collectCorrections());
      }
    }

    try {
      const response = await fetch(`/api/invoices/${invoiceId}/photo`, options);
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
    // A fresh render starts from the extracted values with no edits.
    state.edited = false;
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
    state.edited = false;
    hideVerdict();
  }

  function renderDocument(document_, index, total) {
    const section = document.createElement("section");
    section.className = "results__document";
    // The page index the reviewer's corrections are matched on. This comes from
    // the model, not from the on-screen position, in case the two ever differ.
    section.dataset.index = Number.isInteger(document_.index) ? document_.index : index;

    const heading = document.createElement("h3");
    heading.className = "results__document-title";
    heading.textContent = total > 1 ? `Invoice ${index + 1}` : "Invoice";
    if (typeof document_.confidence === "number") {
      heading.appendChild(confidenceBadge(document_.confidence));
    }
    section.appendChild(heading);

    // Only the reviewable fields are shown. The model may return others (invoice
    // number, customer, a previous unpaid balance...), and the backend still
    // stores them, but the review screen is deliberately limited to the fields
    // a person is trusted to correct, so nothing else can be typed in.
    const extractedFields = document_.fields || {};
    const fields = new Map(
      Object.entries(extractedFields).filter(
        ([name, field]) =>
          REVIEWABLE_FIELDS[name] &&
          field &&
          field.value !== "" &&
          field.value !== null &&
          field.value !== undefined
      )
    );
    // A required field (the merchant) is shown even when the model did not
    // return it, as an empty row to be filled in: it must not sit out of sight
    // in the add control, and the document cannot be stored without it.
    Object.entries(REVIEWABLE_FIELDS).forEach(([name, reviewable]) => {
      if (reviewable.required && !fields.has(name)) {
        fields.set(name, { value: "", confidence: null });
      }
    });

    const list = document.createElement("dl");
    list.className = "fields";
    sortFields([...fields]).forEach(([name, field]) => {
      list.appendChild(renderFieldRow(name, field));
    });
    section.appendChild(list);
    return section;
  }

  /**
   * What an extracted value is shown as before anyone edits it.
   *
   * Money values arrive with a currency suffix ("120.00 EUR") and are shown
   * the way a person would type them, so the field reads as editable at a
   * glance. Dates are left as-is: a date input renders the ISO value through
   * its own calendar.
   */
  function displayFor(type, value) {
    const text = String(value);
    if (type === "money") {
      const amount = text.match(/\d+(?:\.\d{1,2})?/);
      return amount ? amount[0] : text;
    }
    return text;
  }

  /**
   * Strip anything a field type can never hold, as the reviewer types or
   * pastes. Money keeps digits and one decimal point; a merchant keeps
   * letters, numbers and spaces only, so command syntax and markup cannot be
   * entered. Dates are picked from a calendar, so there is nothing to clean.
   */
  function sanitizeFor(type, value) {
    if (type === "money") {
      let cleaned = value.replace(/[^\d.]/g, "");
      const dot = cleaned.indexOf(".");
      if (dot !== -1) {
        cleaned =
          cleaned.slice(0, dot + 1) +
          cleaned.slice(dot + 1).replace(/\./g, "").slice(0, 2);
      }
      return cleaned;
    }
    return value.replace(/[^\p{L}\p{N} ]/gu, "");
  }

  /** Whether a value is acceptable for its field. An empty value is a cleared
   * field and is allowed; anything else must fit the type.
   */
  function isValidValue(type, value) {
    if (value === "") return true;
    if (type === "money") return /^\d+\.\d{2}$/.test(value);
    if (type === "date") return /^\d{4}-\d{2}-\d{2}$/.test(value);
    return /^[\p{L}\p{N} ]+$/u.test(value);
  }

  /** Whether a row's value is acceptable as it stands. Empty is allowed only on
   * a field that is not required, so a merchant left blank blocks acceptance. */
  function isRowInvalid(reviewable, value) {
    return (reviewable.required && value.trim() === "") || !isValidValue(reviewable.type, value);
  }

  function showFieldError(row, invalid, reviewable) {
    const error = row.querySelector(".fields__error");
    row.classList.toggle("fields__row--invalid", invalid);
    if (error) {
      error.hidden = !invalid;
      if (reviewable) {
        const empty =
          reviewable.required && row.querySelector(".fields__input").value.trim() === "";
        error.textContent = empty
          ? `${reviewable.label} is required.`
          : `Enter ${VALID_HINTS[reviewable.type]}.`;
      }
    }
  }

  /**
   * One editable field row. The extracted value is shown in an input so a wrong
   * reading can be corrected, and the confidence badge is withdrawn once the
   * person overrides it: a score for automatic reading says nothing about a
   * value a person just typed.
   */
  function renderFieldRow(name, field) {
    const reviewable = REVIEWABLE_FIELDS[name];
    const row = document.createElement("div");
    row.className = "fields__row";
    row.dataset.field = name;

    const term = document.createElement("dt");
    term.className = "fields__name";
    term.textContent = reviewable.label;

    const value = document.createElement("dd");
    value.className = "fields__value";

    // Assigned through .value, never as markup: OCR output is untrusted input.
    const input = document.createElement("input");
    input.className = "fields__input";
    input.setAttribute("aria-label", reviewable.label);
    if (reviewable.type === "date") {
      // A calendar field: the browser offers the year, month and day to pick,
      // and holds the value as ISO (YYYY-MM-DD) with nothing to sanitize.
      input.type = "date";
      input.value = String(field.value ?? "");
    } else {
      input.type = "text";
      input.value = displayFor(reviewable.type, field.value);
    }
    input.addEventListener("input", () => {
      if (reviewable.type !== "date") {
        input.value = sanitizeFor(reviewable.type, input.value);
      }
      row.dataset.changed = "true";
      state.edited = true;
      const badge = value.querySelector(".confidence");
      if (badge) badge.hidden = true;
      showFieldError(row, isRowInvalid(reviewable, input.value), reviewable);
    });
    value.appendChild(input);

    if (typeof field.confidence === "number") {
      value.appendChild(confidenceBadge(field.confidence));
    }

    const error = document.createElement("p");
    error.className = "fields__error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    error.textContent = `Enter ${VALID_HINTS[reviewable.type]}.`;
    value.appendChild(error);

    // An empty required field is a prompt, shown even before the reviewer types:
    // it has to be filled in, so there is no point hiding why the accept will
    // not go through until it is.
    if (reviewable.required && !input.value.trim()) {
      input.setAttribute("placeholder", "Required");
      error.hidden = false;
      error.textContent = `${reviewable.label} is required.`;
      row.classList.toggle("fields__row--invalid", true);
    }

    row.append(term, value);
    return row;
  }

  /**
   * The values the reviewer changed, in the shape the backend merges over the
   * extracted reading when the photo is accepted. Only rows that were touched
   * are included, so an untouched reading keeps exactly what the model
   * returned.
   */
  function collectCorrections() {
    const documents = [];
    els.resultsBody.querySelectorAll(".results__document").forEach((section) => {
      const fields = {};
      section.querySelectorAll(".fields__row[data-changed]").forEach((row) => {
        const input = row.querySelector(".fields__input");
        if (!input) return;
        fields[row.dataset.field] = { value: input.value };
      });
      documents.push({ index: Number(section.dataset.index), fields });
    });
    return { documents };
  }

  /** The first row the reviewer must fix before accepting, if any.
   *
   * A touched row must fit its field. An untouched one only blocks acceptance
   * when it is a required field with no value, so a document whose merchant was
   * not read cannot be stored until it has one, while an untouched value that
   * merely looks odd is not hounded.
   */
  function firstInvalidField() {
    return [...els.resultsBody.querySelectorAll(".fields__row")]
      .map((row) => {
        const reviewable = REVIEWABLE_FIELDS[row.dataset.field];
        const input = row.querySelector(".fields__input");
        const value = input ? input.value : "";
        const touched = row.hasAttribute("data-changed");
        const empty = reviewable.required && value === "";
        return {
          reviewable,
          empty,
          valid: !empty && (touched ? isValidValue(reviewable.type, value) : true),
        };
      })
      .find((field) => !field.valid);
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
    const reviewable = REVIEWABLE_FIELDS[name];
    if (reviewable) return reviewable.label;
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
    els.crop.hidden = true;
    closeCrop();
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
    els.zoomControl.hidden = true;
    showPlaceholder(message);
    els.retry.hidden = false;
    resetStage();
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

  /** Registers the service worker that caches the shell, so the app can be
   * installed and opened offline. Entirely optional: the page works without it,
   * and on an insecure origin (where service workers are unavailable) it simply
   * does nothing. Registration waits for `load` so it never competes with the
   * first paint or the camera prompt.
   */
  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    window.addEventListener("load", () => {
      try {
        // The worker lives at the root so it can control the whole app.
        navigator.serviceWorker.register("/sw.js").catch(() => {
          // Offline support is a bonus; the app works without it.
        });
      } catch {
        // An insecure context, or a browser that refuses registration.
      }
    });
  }
})();
