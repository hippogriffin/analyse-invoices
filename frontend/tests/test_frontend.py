"""Static guards for the camera UI.

The camera flow is plain JavaScript with no build step, so these checks assert
the two things that silently broke before: that the `hidden` attribute can
actually hide something, and that controls which depend on a captured photo
start hidden in the markup.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STYLES = (STATIC_DIR / "styles.css").read_text()
HTML = (STATIC_DIR / "index.html").read_text()
APP_JS = (STATIC_DIR / "app.js").read_text()

CSS_RULES = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
HIDDEN_OVERRIDE = re.compile(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", re.S)


def elements_with_hidden_attribute() -> dict[str, list[str]]:
    """Map tag ids that carry the hidden attribute to their classes."""
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"<(\w+)[^>]*\bid=\"([^\"]+)\"[^>]*>", HTML, re.S):
        tag = match.group(0)
        if "hidden" not in tag:
            continue
        classes = re.search(r'class="([^"]*)"', tag)
        found[match.group(2)] = classes.group(1).split() if classes else []
    return found


def rules_setting_display() -> set[str]:
    """Class names whose CSS rule declares a display property."""
    names: set[str] = set()
    for selector, body in CSS_RULES.findall(STYLES):
        if "display:" not in body:
            continue
        for selector_part in selector.split(","):
            for name in re.findall(r"\.([\w-]+)", selector_part):
                names.add(name)
    return names


def test_hidden_attribute_overrides_display_rules():
    """Regression: author `display` rules beat the UA [hidden] rule.

    Without the !important override, `.stage__media { display: block }` left the
    src-less still image on screen (a broken image icon) and
    `.controls__group { display: grid }` left Retake/Upload visible before any
    photo was taken.
    """
    assert HIDDEN_OVERRIDE.search(STYLES), (
        "styles.css must keep a `[hidden] { display: none !important; }` rule, "
        "otherwise elements with a display rule ignore the hidden attribute"
    )


def test_every_hidden_element_is_covered_by_the_override():
    """A hidden element styled with display must be protected, not just happen to be."""
    display_classes = rules_setting_display()
    for element_id, classes in elements_with_hidden_attribute().items():
        conflicting = display_classes.intersection(classes)
        if conflicting:
            assert HIDDEN_OVERRIDE.search(STYLES), (
                f"#{element_id} uses hidden with class(es) {sorted(conflicting)} that set "
                "display, but no [hidden] override exists"
            )


def test_still_image_starts_hidden_without_a_source():
    """The captured <img> ships with no src; a broken icon shows if it renders."""
    img = re.search(r"<img[^>]*id=\"still\"[^>]*>", HTML, re.S)
    assert img, "still image element is missing"
    assert "hidden" in img.group(0), "still image must start hidden"
    assert "src=" not in img.group(0), (
        "still image must not ship a src attribute; it is set on capture"
    )


def test_retake_and_upload_start_hidden():
    """Retake and Upload must not be reachable until a photo exists."""
    group = re.search(r"<div[^>]*id=\"reviewActions\"[^>]*>", HTML, re.S)
    assert group, "review actions container is missing"
    assert "hidden" in group.group(0), "review actions must start hidden"
    for button in ("retake", "upload"):
        element = re.search(rf"<button[^>]*id=\"{button}\"[^>]*>", HTML, re.S)
        assert element, f"#{button} button is missing"


def test_preview_starts_hidden():
    """The <video> is revealed only after getUserMedia succeeds."""
    video = re.search(r"<video[^>]*id=\"preview\"[^>]*>", HTML, re.S)
    assert video, "preview video element is missing"
    assert "hidden" in video.group(0), "preview must start hidden"


def test_capture_button_is_restored_after_retake():
    """Regression: retake left no way to take another photo.

    showPhoto() hides the capture button, so startCamera() must show it again.
    """
    start_camera = re.search(
        r"async function startCamera\([^)]*\)\s*\{.*?\n  \}", APP_JS, re.S
    )
    assert start_camera, "startCamera function not found"
    assert "els.capture.hidden = false" in start_camera.group(0), (
        "startCamera() must set els.capture.hidden = false, otherwise retaking "
        "leaves the user with a preview but no Take photo button"
    )


def test_wait_screen_is_not_shown_when_retaking():
    """Regression: retaking flashed the "Starting camera..." screen.

    It appeared because startCamera() showed the placeholder unconditionally,
    and as a sibling of the preview it squeezed the feed sideways for the
    length of the getUserMedia call.
    """
    start_camera = re.search(
        r"async function startCamera\(([^)]*)\)\s*\{.*?\n  \}", APP_JS, re.S
    )
    assert start_camera, "startCamera function not found"
    assert "waitScreen" in start_camera.group(1), (
        "startCamera() should take a waitScreen option"
    )
    body = start_camera.group(0)
    assert re.search(r"if \(waitScreen\)\s*\{\s*showPlaceholder", body, re.S), (
        "the wait screen must be conditional on waitScreen, not unconditional"
    )

    retake = re.search(r"function retake\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert retake, "retake function not found"
    assert re.search(r"startCamera\(\)", retake.group(0)), (
        "retake() must call startCamera() without the wait screen"
    )
    assert "waitScreen" not in retake.group(0), (
        "retake() must not ask for the wait screen"
    )


def test_retry_still_shows_the_wait_screen():
    """Retrying after a permission error should give feedback."""
    assert re.search(r'startCamera\(\{ waitScreen: true \}\)', APP_JS), (
        "the retry button and initial load should show the wait screen"
    )
    assert APP_JS.count("startCamera({ waitScreen: true })") >= 2


def test_retake_hides_still_before_revoking_object_url():
    """Revoking while the image is visible renders a broken image icon."""
    retake = re.search(r"function retake\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    body = retake.group(0)
    hide_index = body.find("els.still.hidden = true")
    revoke_index = body.find("releasePhotoUrl()")
    assert hide_index != -1, "retake() should hide the still image"
    assert revoke_index != -1, "retake() should release the previous photo"
    assert hide_index < revoke_index, (
        "the still image must be hidden before its object URL is revoked"
    )


def test_stage_children_cannot_reflow_each_other():
    """Regression: sibling flex children shoved the feed sideways when swapped."""
    stage = re.search(r"\.stage\s*\{([^}]*)\}", STYLES, re.S)
    assert stage, ".stage rule not found"
    assert "position: relative" in stage.group(1), (
        ".stage must be a positioning context for its absolutely positioned children"
    )
    assert "aspect-ratio" in stage.group(1), (
        ".stage should have a fixed aspect ratio so the frame never resizes"
    )
    for selector in (".stage__media", ".stage__placeholder"):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", STYLES, re.S)
        assert rule, f"{selector} rule not found"
        assert "position: absolute" in rule.group(1), (
            f"{selector} must be absolutely positioned so it cannot reflow its siblings"
        )
        assert "inset: 0" in rule.group(1), f"{selector} should fill the stage"


def test_camera_is_released_on_capture_and_unload():
    """Tracks must be stopped so the camera indicator does not stay on."""
    assert "pagehide" in APP_JS, "camera should be released on pagehide"
    capture = re.search(r"function capturePhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    show_photo = re.search(r"function showPhoto\(photo\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert show_photo, "showPhoto(photo) not found"
    assert "stopCamera" in show_photo.group(0), "camera should stop once a photo is taken"


def test_results_panel_starts_hidden():
    """Nothing is extracted until a photo has been analysed."""
    results = re.search(r"<section[^>]*id=\"results\"[^>]*>", HTML, re.S)
    assert results, "results panel is missing"
    assert "hidden" in results.group(0), "results panel must start hidden"


def test_photo_is_posted_straight_to_the_backend():
    """The UI holds no storage, so one POST to the backend is the whole flow.

    The old design uploaded to itself first and then handed on a blob name.
    That split is what put a storage key in the browser-facing service.
    """
    upload = re.search(r"function uploadPhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert upload, "uploadPhoto not found"
    body = upload.group(0)
    assert re.search(
        r'request\.open\("POST", `\$\{state\.analysisApiBaseUrl\}/api/invoices`\)', body
    ), (
        "uploadPhoto() must post the file to the backend's /api/invoices, since "
        "the backend is what stores it"
    )
    assert 'form.append("photo"' in body, "the file must be sent as multipart form data"
    assert "/api/upload" not in APP_JS, (
        "the old self-upload endpoint is gone; nothing here should reference it"
    )
    assert "runAnalysis" not in APP_JS, (
        "there is no separate analysis step: submitting stores and analyses"
    )
    assert "blobName" not in APP_JS, (
        "the UI never names a blob; only the backend knows the name"
    )


def test_submission_is_blocked_when_no_backend_is_configured():
    """With no backend there is nowhere to send the photo, so fail before uploading."""
    upload = re.search(r"function uploadPhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    body = upload.group(0)
    assert "state.analysisConfigured" in body, (
        "uploadPhoto() must check that an analysis service is configured"
    )
    guard = body.index("state.analysisConfigured")
    assert guard < body.index("request.send(form)"), (
        "the configuration check must happen before the request is sent"
    )


def test_a_backend_error_is_shown_and_the_upload_stays_available():
    """A rejected photo must be retryable without recapturing."""
    upload = re.search(r"function uploadPhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    body = upload.group(0)
    error_branch = body[body.index("request.status < 200") : body.index("els.upload.hidden = true")]
    assert "setStatus(readError(request), \"error\")" in error_branch, (
        "a failed submission must report the backend's own message"
    )
    assert "els.upload.hidden = true" not in error_branch, (
        "the Upload button must stay available so the photo can be retried"
    )


def test_poll_starts_from_the_returned_invoice_id():
    """The id comes from the POST response, so the UI must use it, not invent one."""
    upload = re.search(r"function uploadPhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    body = upload.group(0)
    assert "accepted.invoiceId" in body, (
        "the invoice id must be the one the backend returned"
    )
    assert "pollAnalysis(state.analysisApiBaseUrl, invoiceId, run)" in body, (
        "polling must start with the id the backend returned"
    )


def test_an_already_processed_photo_shows_only_the_message():
    """A repeat is not analysed again, so there is no result to show.

    Polling anyway would put a stale reading back on screen as though it had just
    been produced, and would promise work the service has deliberately skipped
    because Document Intelligence bills per page.
    """
    upload = re.search(r"function uploadPhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    body = upload.group(0)
    assert "alreadyProcessed" in body, (
        "the service's alreadyProcessed flag must be read"
    )
    assert "already been processed" in body, (
        "the user must be told the photo was already processed"
    )
    repeat = re.search(
        r"if \(accepted\.alreadyProcessed\) \{(.*?)\n      \}", body, re.S
    )
    assert repeat, "the already-processed branch not found"
    assert "pollAnalysis" not in repeat.group(1), (
        "an already-processed photo must not be polled for a result"
    )
    assert "return" in repeat.group(1), (
        "the already-processed branch must stop before the results are fetched"
    )


def test_extracted_values_are_never_inserted_as_html():
    """Extracted fields are OCR output, so they are untrusted input.

    A field value containing markup must be rendered as text. Using innerHTML
    or insertAdjacentHTML with a value would make this a stored XSS sink.
    """
    render = re.search(r"function renderDocument\(.*?\n  \}", APP_JS, re.S)
    assert render, "renderDocument not found"
    # Comments are stripped first, so prose mentioning a sink cannot mask it.
    code = re.sub(r"/\*.*?\*/", "", render.group(0), flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in code, (
            f"renderDocument() must not use {forbidden} with extracted field values"
        )
    assert "value.textContent = String(field.value)" in code, (
        "field values must be assigned with textContent"
    )


def test_stale_polling_is_cancelled_by_a_new_photo():
    """An in-flight poll for the previous photo must not overwrite new results."""
    assert "state.analysisRun" in APP_JS, "a run token is needed to cancel polling"
    poll = re.search(r"async function pollAnalysis\(.*?\n  \}", APP_JS, re.S)
    assert poll, "pollAnalysis not found"
    assert "run !== state.analysisRun" in poll.group(0), (
        "pollAnalysis() must stop when the run token changes"
    )
    start_camera = re.search(r"async function startCamera\([^)]*\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert "state.analysisRun += 1" in start_camera.group(0), (
        "startCamera() must bump the run token so old polling stops"
    )


def test_results_are_hidden_when_a_new_photo_starts():
    """Old results must not linger while the next photo is captured."""
    start_camera = re.search(r"async function startCamera\([^)]*\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert "hideResults()" in start_camera.group(0), (
        "startCamera() must clear previous results"
    )


# --------------------------------------------------------------- mode chooser


def test_chooser_is_the_first_thing_shown():
    """The user must be offered a choice before anything is started."""
    picker = re.search(r"<section[^>]*id=\"modePicker\"[^>]*>", HTML, re.S)
    assert picker, "mode chooser is missing"
    assert "hidden" not in picker.group(0), (
        "the chooser must be visible on load, not hidden behind a click"
    )
    for button in ("chooseCamera", "chooseFile"):
        element = re.search(rf"<button[^>]*id=\"{button}\"[^>]*>", HTML, re.S)
        assert element, f"#{button} button is missing"
    labels = re.findall(r"id=\"choose(?:Camera|File)\"[^>]*>\s*([^<]+)", HTML)
    assert [label.strip() for label in labels] == ["Take a photo", "Upload a photo"], (
        f"chooser buttons should read 'Take a photo' and 'Upload a photo', got {labels}"
    )


def test_camera_is_not_started_until_a_mode_is_chosen():
    """Regression risk: asking for camera permission on page load.

    The old flow called startCamera() from init(), so a browser permission
    prompt appeared before the user had chosen anything.
    """
    init = re.search(r"async function init\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert init, "init function not found"
    body = init.group(0)
    # The only permitted mention is the retry handler, which fires on a click.
    calls = re.findall(r"startCamera\(", re.sub(r"[^\n]*addEventListener[^\n]*", "", body))
    assert not calls, (
        "init() must not start the camera on load; it should wait for chooseMode()"
    )
    assert "showModePicker()" in body, "init() should show the chooser"
    assert 'els.chooseCamera.addEventListener("click", () => chooseMode("camera"))' in body, (
        "the take-photo button must open camera mode"
    )
    assert 'els.chooseFile.addEventListener("click", () => chooseMode("upload"))' in body, (
        "the upload-photo button must open upload mode"
    )
    assert "startCamera" in re.search(
        r"function chooseMode\(mode\)\s*\{.*?\n  \}", APP_JS, re.S
    ).group(0), "camera mode must start the camera"


def test_camera_controls_stay_hidden_behind_the_chooser():
    """The stage and its actions are not reachable until a mode is chosen."""
    workbench = re.search(r"<div[^>]*id=\"workbench\"[^>]*>", HTML, re.S)
    assert workbench, "workbench wrapper is missing"
    assert "hidden" in workbench.group(0), (
        "the capture/upload workbench must start hidden behind the chooser"
    )
    assert re.search(r"function showModePicker\(\)\s*\{.*?\n  \}", APP_JS, re.S), (
        "showModePicker() is missing"
    )
    picker_fn = re.search(r"function showModePicker\(\)\s*\{.*?\n  \}", APP_JS, re.S).group(0)
    assert "els.workbench.hidden = true" in picker_fn
    assert "els.modePicker.hidden = false" in picker_fn
    choose = re.search(r"function chooseMode\(mode\)\s*\{.*?\n  \}", APP_JS, re.S).group(0)
    assert "els.modePicker.hidden = true" in choose
    assert "els.workbench.hidden = false" in choose


def test_file_input_is_reachable_without_being_displayed():
    """A `display: none` or hidden file input cannot be opened from script.

    Safari in particular ignores a programmatic .click() on a display:none
    file input, so the picker would silently do nothing.
    """
    file_input = re.search(r"<input[^>]*id=\"fileInput\"[^>]*>", HTML, re.S)
    assert file_input, "file input is missing"
    tag = file_input.group(0)
    # Matches the attribute, not the tail of class="visually-hidden".
    assert not re.search(r"(?<![\w-])hidden(?=[\s/>])", tag), (
        "the file input must not carry the hidden attribute"
    )
    assert 'type="file"' in tag
    assert "accept=" in tag, "the file input needs an accept filter"
    assert re.search(r'class="[^"]*visually-hidden', tag), (
        "the file input should be clipped with .visually-hidden, not display:none"
    )
    css = re.search(r"\.visually-hidden\s*\{([^}]*)\}", STYLES, re.S)
    assert css, ".visually-hidden rule is missing"
    assert "display" not in css.group(1), (
        ".visually-hidden must not set display, or the input cannot be clicked"
    )
    assert re.search(r"function openFilePicker\(\)\s*\{[^}]*els\.fileInput\.click\(\)", APP_JS), (
        "the visible button must open the picker by clicking the input"
    )


def test_file_chooser_accept_comes_from_the_server_config():
    """The picker should offer exactly the types the server accepts."""
    assert re.search(r"els\.fileInput\.accept = state\.allowedContentTypes\.join\(\",\"\)", APP_JS), (
        "the input's accept list should be built from the configured types"
    )
    load = re.search(r"async function loadConfig\(\)\s*\{.*?\n  \}", APP_JS, re.S).group(0)
    assert "config.allowedContentTypes" in load
    assert "config.maxUploadBytes" in load


def test_picked_file_is_validated_before_upload():
    """Rejecting in the browser avoids a pointless round trip."""
    validate = re.search(r"function validateFile\(file\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert validate, "validateFile not found"
    body = validate.group(0)
    assert "state.maxUploadBytes" in body, "the size limit must be checked client-side"
    assert "state.allowedContentTypes" in body, "the allowed types must be checked client-side"
    picked = re.search(r"function onFilePicked\(\)\s*\{.*?\n  \}", APP_JS, re.S).group(0)
    assert re.search(r"validateFile\(file\)", picked), (
        "onFilePicked() must validate before showing the photo"
    )
    # Validation must run before showPhoto, so a bad file never previews.
    assert picked.index("validateFile") < picked.index("showPhoto")


def test_upload_sends_the_picked_file_name():
    """A picked file keeps its own name; a camera blob has none."""
    upload = re.search(r"function uploadPhoto\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert upload, "uploadPhoto not found"
    body = upload.group(0)
    assert 'state.photo.name || "invoice.jpg"' in body, (
        "the multipart filename should fall back to invoice.jpg for camera blobs"
    )


def test_picked_file_name_is_never_treated_as_html():
    """A filename is user-controlled input, so it must reach the DOM as text.

    The name only ever goes into FormData, but the chosen file is also echoed
    into the status line, which rules out an innerHTML sink here.
    """
    for name in ("onFilePicked", "showPhoto", "validateFile", "resolveType"):
        fn = re.search(rf"function {name}\(.*?\n  \}}", APP_JS, re.S)
        assert fn, f"{name}() not found"
        code = re.sub(r"/\*.*?\*/", "", fn.group(0), flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML"):
            assert forbidden not in code, f"{name}() must not use {forbidden}"


def test_upload_mode_hides_camera_only_controls():
    """The capture button and camera retry are meaningless without a camera."""
    enter = re.search(r"function enterUploadMode\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert enter, "enterUploadMode not found"
    body = enter.group(0)
    assert "els.capture.hidden = true" in body, "the capture button must be hidden"
    assert "els.retry.hidden = true" in body, "the camera retry must be hidden"
    assert "els.pickFile.hidden = false" in body, "the file picker must be offered"
    assert "stopCamera()" in body, "upload mode must not leave a camera running"


def test_choose_another_reopens_the_picker():
    """In upload mode the second action reopens the picker, not the camera."""
    retake = re.search(r"function retake\(\)\s*\{.*?\n  \}", APP_JS, re.S)
    assert retake, "retake not found"
    body = retake.group(0)
    assert re.search(r'state\.mode === "upload"', body), (
        "retake() must branch on the mode"
    )
    upload_branch, _, camera_branch = body.partition("startCamera()")
    assert "openFilePicker()" in upload_branch, (
        "upload mode must reopen the file picker"
    )
    assert "openFilePicker()" not in camera_branch, (
        "the camera branch must not reopen the file picker"
    )


def test_the_photo_quality_question_is_in_the_markup():
    """The prompt needs both answers, and has to say the photo is already gone."""
    for element_id in (
        "verdict",
        "verdictQuestion",
        "verdictNote",
        "photoOkay",
        "retakePhoto",
    ):
        assert f'id="{element_id}"' in HTML, f"missing #{element_id}"
    panel = re.search(r'id="verdict".*?</section>', HTML, re.S)
    assert panel, "verdict panel not found"
    text = re.sub(r"\s+", " ", panel.group(0))
    assert "deleted once its fields have been read" in text, (
        "the panel must say the photo is deleted whatever the user answers"
    )
    assert "take another" in text.lower(), "the panel must offer a retake"
    # The answers are about whether the photo was readable, not about the
    # photo's fate: deletion is automatic, so no button may offer it as a choice.
    labels = " ".join(re.findall(r"<button[^>]*>(.*?)</button>", text, re.S)).lower()
    assert labels.strip(), "the panel has no answer buttons"
    for forbidden in ("delete", "discard", "keep"):
        assert forbidden not in labels, (
            f"the answers must be about readability, not about {forbidden}ing the photo"
        )


def test_confirming_turns_the_retake_offer_into_the_next_invoice():
    """One button, and after accepting it stops being about this photo.

    The reading is settled, so a button still offering to retake it invites the
    user to undo a decision they have made. It stays, relabelled, because the
    reviewer still needs a way on to the next invoice and no other control
    offers one once the review bar has gone.
    """
    render = re.search(r"function renderVerdict\(.*?\n  \}", APP_JS, re.S)
    assert render, "renderVerdict not found"
    body = render.group(0)
    accepted = re.search(
        r"if \(photoSatisfied === true\) \{(.*?)\n    \}", body, re.S
    )
    assert accepted, "the confirmed branch not found"
    branch = accepted.group(1)
    assert "els.verdictNote.hidden = true" in branch, (
        "the note must go once the values are confirmed"
    )
    assert "els.photoOkay.hidden = true" in branch, (
        "the accept button must not be offered again"
    )
    assert 'els.retakePhoto.textContent = "Choose another photo"' in branch, (
        "the remaining button must offer the next invoice, not a retake"
    )
    assert "els.retakePhoto.hidden = false" in branch, (
        "the way on to the next invoice must stay available"
    )
    # ...but only there: the note explains why a retake cannot be undone, so it
    # has to survive the retake branch.
    rejected = re.search(
        r"if \(photoSatisfied === false\) \{(.*?)\n    \}", body, re.S
    )
    assert rejected, "the retake branch not found"
    assert "els.verdictNote.hidden = false" in rejected.group(1), (
        "the note must survive a retake, since that is when it matters most"
    )


def test_the_retake_button_does_not_contradict_a_confirmed_reading():
    """After "yes" the button must not record a retake.

    The same control is a retake before the answer and the next invoice after
    it. Wired unconditionally to the retake request, pressing it on a settled
    invoice would store ``photoSatisfied: false`` for a reading the reviewer
    just approved.
    """
    handler = re.search(r"function onRetakePhoto\(\) \{.*?\n  \}", APP_JS, re.S)
    assert handler, "onRetakePhoto not found"
    body = handler.group(0)
    assert "state.photoSatisfied === true" in body, (
        "the button must know whether the reading was already accepted"
    )
    assert "retake()" in body, "the accepted case must start a new photo"
    assert "sendVerdict(false)" in body, "the unanswered case must record a retake"
    # The listener has to go through the branch, not straight to sendVerdict.
    listener = re.search(
        r'els\.retakePhoto\.addEventListener\("click", ([^)]+)\)', APP_JS
    )
    assert listener, "the retakePhoto listener not found"
    assert listener.group(1) == "onRetakePhoto", (
        "the retake button must not be wired straight to the retake request"
    )


def test_the_question_is_only_asked_once_the_fields_are_shown():
    render = re.search(r"function renderVerdict\(.*?\n  \}", APP_JS, re.S)
    assert render, "renderVerdict not found"
    body = render.group(0)
    assert "complete" in body, (
        "renderVerdict must be told whether the analysis finished"
    )
    # A pending view must not flash the question and then take it away.
    assert re.search(r"if \(!complete\)\s*\{\s*els\.verdict\.hidden = true;", body)
    # A question with no buttons would be a dead end.
    assert "els.photoOkay.hidden = false" in body
    assert "els.retakePhoto.hidden = false" in body


def test_both_answers_reach_the_backend():
    send = re.search(r"async function sendVerdict\(.*?\n  \}", APP_JS, re.S)
    assert send, "sendVerdict not found"
    body = send.group(0)
    assert "/api/invoices/${invoiceId}/photo" in body
    assert 'method: satisfied ? "POST" : "DELETE"' in body, (
        "yes must POST and retake must DELETE, or the answer is not recorded"
    )
    # The answer is about the photo, so the name must not imply a photo is kept.
    assert "satisfied" in body


def test_a_failed_answer_is_surfaced_rather_than_swallowed():
    send = re.search(r"async function sendVerdict\(.*?\n  \}", APP_JS, re.S)
    body = send.group(0)
    assert "if (!response.ok)" in body, "a failed answer must not read as success"
    assert "catch" in body
    assert "That was not saved" in body, "the failure must be shown to the user"
    # The buttons are re-enabled so the choice can be retried.
    assert "els.photoOkay.disabled = false" in body
    assert "els.retakePhoto.disabled = false" in body


def test_the_verdict_buttons_do_not_collide_with_the_results_controls():
    """A duplicate id silently rebinds listeners to the wrong element."""
    ids = re.findall(r'id="([^"]+)"', HTML)
    duplicates = {element_id for element_id in ids if ids.count(element_id) > 1}
    assert not duplicates, f"duplicate element ids: {sorted(duplicates)}"
