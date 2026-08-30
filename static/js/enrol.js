/*
 * Browser-side enrolment (CO-3, MA-2, FS-9).
 *
 * The replacement for the server-side OpenCV capture window. **The browser
 * supplies a camera and a screen; nothing else moves.** Every quality
 * judgement - geometry, framing, pose, expression, blur, brightness - happens
 * on the server, through vision/enrolment.py, on the same code the capture
 * window used. Reimplementing the gates here would recreate MA-4, which Phase
 * 3 existed to delete.
 *
 * Lifted out of enrol.html by Phase 5 (US-8). The only change was to the
 * eleven server-injected values at the top, which are read from data-
 * attributes now instead of being written into the source by Jinja.
 */
(function () {
    "use strict";

    // Every server-supplied value arrives as a data- attribute on this
    // element rather than being interpolated into the script (US-8). The
    // page cannot run without it, which is the intended failure: an
    // enrolment page missing its configuration must not half-work.
    var page = document.querySelector("[data-enrol-page]");

    if (!page) {
        return;
    }

    var CSRF_TOKEN = document
        .querySelector('meta[name="csrf-token"]')
        .getAttribute("content");
    var STUDENT_ID = page.getAttribute("data-student-id");
    var STUDENT_NAME = page.getAttribute("data-student-name");
    var RECORD = JSON.parse(page.getAttribute("data-record"));
    var TOTAL = Number(page.getAttribute("data-total"));

    var START_URL = page.getAttribute("data-start-url");
    var FRAME_URL = page.getAttribute("data-frame-url");
    var FINISH_URL = page.getAttribute("data-finish-url");
    var CANCEL_URL = page.getAttribute("data-cancel-url");
    var DONE_URL = page.getAttribute("data-done-url");

    // ⚠️ **200 -> 120 on 2026-08-21.** The old comment here said the server
    // spends "roughly 50-150 ms on a frame", so asking faster would only queue
    // requests behind one another. That figure was carried from the
    // *recognition* loop, which does LBPH on every tracked face; enrolment
    // does MediaPipe plus one alignment and no matching at all.
    //
    // Measured on this machine, median of 20 frames at 1920x1080: **15 ms** of
    // server work, plus about 39 ms to JPEG-encode the frame in the browser.
    // So 200 ms was idling roughly three quarters of every tick, and the
    // stage-hold counter - which advances once per uploaded frame - was
    // waiting on that idle time.
    //
    // 120 ms still leaves better than 2x headroom over the measured round
    // trip, and `inFlight` below is the real backstop: a tick that arrives
    // while a request is outstanding is skipped, not queued.
    var FRAME_INTERVAL_MS = 120;

    // The resolution every threshold on the server was tuned at. The server
    // crops and scales whatever actually arrives, but asking for this means
    // it usually has nothing to do.
    var WANTED = { width: { ideal: 1920 }, height: { ideal: 1080 } };

    // From the server, not restated here: infra/uploads.py refuses a frame
    // narrower than this, and a constant duplicated in JavaScript is a
    // constant that drifts.
    var MIN_NATIVE_WIDTH = Number(page.getAttribute("data-min-native-width"));

    var video = document.getElementById("preview");
    var overlay = document.getElementById("overlay");
    var instruction = document.getElementById("instruction");
    var message = document.getElementById("message");
    var progressFill = document.getElementById("progress-fill");
    var progressText = document.getElementById("progress-text");
    var startButton = document.getElementById("start-button");
    var cancelButton = document.getElementById("cancel-button");
    var retryButton = document.getElementById("retry-button");
    var cameraSelect = document.getElementById("camera-select");
    var cameraDetail = document.getElementById("camera-detail");
    var stageItems = document.querySelectorAll("#stage-list li");

    var scratch = document.createElement("canvas");
    var stream = null;
    var activeDeviceId = null;
    var timer = null;
    var inFlight = false;
    var running = false;
    var captured = 0;

    function say(text, tone) {
        message.textContent = text;
        message.setAttribute("data-tone", tone || "wait");
    }

    function post(url, options) {
        var settings = options || {};
        settings.method = "POST";
        settings.credentials = "same-origin";
        settings.headers = settings.headers || {};
        settings.headers["X-CSRFToken"] = CSRF_TOKEN;
        return fetch(url, settings);
    }

    function handleAuthFailure(response) {
        // The session can expire while this page is open. @json_api means the
        // access hook answers JSON rather than an HTML login page, but the
        // only useful thing to do with a 401 is go and sign in again.
        if (response.status === 401) {
            window.location.href = "/";
            return true;
        }
        return false;
    }

    function renderProgress(progress) {
        if (!progress) { return; }

        captured = progress.captured;

        instruction.textContent = progress.instruction;
        progressFill.style.width = (100 * progress.captured / progress.total) + "%";
        progressText.textContent =
            progress.captured + " of " + progress.total + " images";

        var tone = "bad";
        if (progress.accepted || progress.done) {
            tone = "good";
        } else if (progress.hold > 0) {
            tone = "wait";
        }

        var text = progress.message;
        if (progress.hold_required > 1 && progress.hold > 0 && !progress.accepted) {
            text += " (" + progress.hold + "/" + progress.hold_required + ")";
        }
        say(text, tone);

        var seen = false;
        Array.prototype.forEach.call(stageItems, function (item) {
            var name = item.getAttribute("data-stage");
            if (name === progress.stage) {
                item.setAttribute("data-state", "current");
                seen = true;
            } else {
                item.setAttribute("data-state", seen ? "todo" : "done");
            }
        });

        drawBox(progress.box);
    }

    function drawBox(box) {
        var context = overlay.getContext("2d");

        if (overlay.width !== overlay.clientWidth) {
            overlay.width = overlay.clientWidth;
            overlay.height = overlay.clientHeight;
        }

        context.clearRect(0, 0, overlay.width, overlay.height);

        if (!box) { return; }

        // The server judged a 1920x1080 frame; the box comes back in those
        // coordinates and is scaled to however large the video is on screen.
        // Mirrored horizontally to match the preview, which is flipped so it
        // behaves like a mirror for the person using it.
        //
        // ⚠️ This sentence only became true on 2026-08-21, and it is worth
        // knowing which half changed. The preview genuinely is flipped now
        // (`#preview` had no CSS at all before, so it was not), and the box
        // genuinely arrives in camera-view coordinates now (`grabFrame()` was
        // mirroring the upload, so it did not). Two wrongs that happened to
        // cancel in this one line, while disagreeing everywhere else.
        var scaleX = overlay.width / 1920;
        var scaleY = overlay.height / 1080;

        var x = overlay.width - (box[0] + box[2]) * scaleX;

        context.strokeStyle = "#E5B84B";
        context.lineWidth = 3;
        context.strokeRect(x, box[1] * scaleY, box[2] * scaleX, box[3] * scaleY);
    }

    function grabFrame() {
        if (inFlight || !running || !video.videoWidth) { return; }

        scratch.width = video.videoWidth;
        scratch.height = video.videoHeight;

        var context = scratch.getContext("2d");

        // ⚠️ **The upload is NOT mirrored, and the flip that used to be here
        // was doing the opposite of what its comment claimed.**
        //
        // It read: "Undo the preview's mirror before uploading: the operator
        // sees a mirror, the server must see the real orientation or LEFT and
        // RIGHT are swapped for the whole capture." Right about the stakes,
        // wrong about the mechanism. `drawImage(video, ...)` samples the
        // **decoded video frame**, which no CSS transform touches - the
        // preview's `transform:scaleX(-1)` is presentation only. So the flip
        // had nothing to undo: it *introduced* a mirror, and the server saw a
        // mirrored world.
        //
        // That inverted exactly what the comment was worried about. `LEFT` is
        // `nose.x > eye_centre.x`, which is a turn to the subject's own left
        // seen from the camera - so a student told "TURN SLIGHTLY LEFT" could
        // only satisfy it by turning **right**.
        //
        // And it is not only the labels: the saved crop is the *mirror* of the
        // face. Neither `capture_dataset.py` (the OpenCV enrolment this
        // replaced) nor `recognize_face.py` mirrors anything, so browser
        // enrolment was the only path that did - it would have trained the
        // model on mirrored faces and then matched un-mirrored ones against
        // them, on a face that is not symmetric.
        //
        // The evidence that un-mirrored is the orientation the gates expect:
        // every LEFT-stage image in `dataset/` classifies as LEFT, and they
        // were captured by the un-mirrored path.
        //
        // The preview stays mirrored in CSS - a person adjusting their own
        // pose needs a mirror - and `drawBox()` still mirrors the box it gets
        // back, which maps the server's camera-view coordinates onto that
        // mirrored picture. Those two are unaffected by this.
        context.drawImage(video, 0, 0);

        inFlight = true;

        scratch.toBlob(function (blob) {
            if (!blob) { inFlight = false; return; }

            post(FRAME_URL, {
                headers: { "Content-Type": "image/jpeg" },
                body: blob
            })
                .then(function (response) {
                    if (handleAuthFailure(response)) { return null; }
                    return response.json();
                })
                .then(function (body) {
                    if (!body) { return; }

                    if (!body.success) {
                        say(body.message, "bad");
                        return;
                    }

                    renderProgress(body.progress);

                    if (body.progress && body.progress.done) {
                        finish();
                    }
                })
                .catch(function () {
                    say("Lost contact with the server. Check the connection.", "bad");
                })
                .then(function () {
                    inFlight = false;
                });
        }, "image/jpeg", 0.92);
    }

    function start() {
        startButton.disabled = true;
        say("Starting…", "wait");

        post(START_URL, {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                student_id: STUDENT_ID,
                student_name: STUDENT_NAME,
                record: RECORD
            })
        })
            .then(function (response) {
                if (handleAuthFailure(response)) { return null; }
                return response.json();
            })
            .then(function (body) {
                if (!body) { return; }

                if (!body.success) {
                    say(body.message, "bad");
                    startButton.disabled = false;
                    return;
                }

                running = true;
                cancelButton.disabled = false;
                renderProgress(body.progress);
                timer = window.setInterval(grabFrame, FRAME_INTERVAL_MS);
            })
            .catch(function () {
                say("Could not start the capture. Reload and try again.", "bad");
                startButton.disabled = false;
            });
    }

    function stopTimer() {
        running = false;
        if (timer !== null) {
            window.clearInterval(timer);
            timer = null;
        }
    }

    function finish() {
        stopTimer();
        say("Saving…", "wait");

        post(FINISH_URL, {})
            .then(function (response) {
                if (handleAuthFailure(response)) { return null; }
                return response.json();
            })
            .then(function (body) {
                if (!body) { return; }

                if (!body.success) {
                    say(body.message, "bad");
                    return;
                }

                say(body.message, "good");
                releaseCamera();
                window.setTimeout(function () {
                    window.location.href = DONE_URL;
                }, 1500);
            })
            .catch(function () {
                say("The capture finished but saving failed. Check the log.", "bad");
            });
    }

    function cancel() {
        stopTimer();
        cancelButton.disabled = true;
        say("Cancelling…", "wait");

        post(CANCEL_URL, {})
            .then(function () {
                releaseCamera();
                window.location.href = DONE_URL;
            })
            .catch(function () {
                window.location.href = DONE_URL;
            });
    }

    function releaseCamera() {
        if (stream) {
            stream.getTracks().forEach(function (track) { track.stop(); });
            stream = null;
        }
    }

    function listCameras() {
        // Device labels are only populated once permission has been granted,
        // which is why this runs after getUserMedia rather than before it.
        if (!navigator.mediaDevices.enumerateDevices) { return; }

        navigator.mediaDevices.enumerateDevices()
            .then(function (devices) {
                var cameras = devices.filter(function (d) {
                    return d.kind === "videoinput";
                });

                cameraSelect.innerHTML = "";

                if (!cameras.length) {
                    cameraSelect.disabled = true;
                    cameraSelect.innerHTML =
                        "<option value=''>No cameras found</option>";
                    return;
                }

                cameras.forEach(function (device, index) {
                    var option = document.createElement("option");
                    option.value = device.deviceId;
                    option.textContent =
                        device.label || ("Camera " + (index + 1));
                    if (device.deviceId === activeDeviceId) {
                        option.selected = true;
                    }
                    cameraSelect.appendChild(option);
                });

                cameraSelect.disabled = cameras.length < 2;
            })
            .catch(function () { /* listing is a convenience, never fatal */ });
    }

    function describeStream(granted) {
        var track = granted.getVideoTracks()[0];
        var settings = track ? track.getSettings() : {};
        var width = settings.width || video.videoWidth || 0;
        var height = settings.height || video.videoHeight || 0;

        activeDeviceId = settings.deviceId || activeDeviceId;

        cameraDetail.textContent =
            (track && track.label ? track.label + " — " : "") +
            (width ? width + "x" + height : "resolution unknown");

        // The server refuses anything narrower than this, so saying it here
        // turns a stream of per-frame rejections into one sentence up front.
        if (width && width < MIN_NATIVE_WIDTH) {
            say(
                "This camera is only " + width + " pixels wide. Enrolment needs " +
                "at least " + MIN_NATIVE_WIDTH + ". Choose a different camera, " +
                "or raise this one's resolution.",
                "bad"
            );
            startButton.disabled = true;
            return false;
        }

        return true;
    }

    function openCamera(deviceId) {
        releaseCamera();
        startButton.disabled = true;
        cameraDetail.textContent = "";

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            // The realistic cause is a non-secure origin: getUserMedia is
            // simply absent outside a secure context rather than failing when
            // called, so this is where "served over plain HTTP to another
            // machine" surfaces.
            say(
                "This browser will not give the page a camera. Enrolment must " +
                "be run on the server machine over http://localhost or " +
                "http://127.0.0.1, or the site must be served over HTTPS.",
                "bad"
            );
            return;
        }

        var constraints = { video: Object.assign({}, WANTED), audio: false };

        if (deviceId) {
            // `exact`, so a chosen camera that cannot be opened reports a
            // failure instead of silently falling back to the default one -
            // which is how "I picked DroidCam and still got a black picture"
            // happens.
            constraints.video.deviceId = { exact: deviceId };
        }

        say("Opening the camera…", "wait");

        navigator.mediaDevices.getUserMedia(constraints)
            .then(function (granted) {
                stream = granted;
                video.srcObject = granted;

                listCameras();

                if (describeStream(granted)) {
                    startButton.disabled = false;
                    say(
                        "Camera ready. If the picture above is black or frozen, " +
                        "choose a different camera before starting.",
                        "good"
                    );
                }
            })
            .catch(function (error) {
                var reason = "The camera could not be opened.";

                if (error && error.name === "NotAllowedError") {
                    reason = "Camera access was refused. Allow it in the " +
                             "browser's address bar, then press Reopen camera.";
                } else if (error && error.name === "NotFoundError") {
                    reason = "No camera was found on this machine.";
                } else if (error && error.name === "NotReadableError") {
                    reason = "The camera is in use by another program. Close " +
                             "anything else using it - OBS, DroidCam, Teams - " +
                             "then press Reopen camera.";
                } else if (error && error.name === "OverconstrainedError") {
                    reason = "That camera cannot provide a usable picture size.";
                }

                say(reason, "bad");
                startButton.disabled = true;

                // Still worth listing what exists, so the operator can try
                // another device rather than being told only that this failed.
                listCameras();
            });
    }

    startButton.addEventListener("click", start);
    cancelButton.addEventListener("click", cancel);
    retryButton.addEventListener("click", function () {
        openCamera(cameraSelect.value || null);
    });
    cameraSelect.addEventListener("change", function () {
        openCamera(cameraSelect.value || null);
    });

    // A closed tab would otherwise hold the single enrolment slot until the
    // server's idle timeout expires it.
    //
    // `keepalive` rather than navigator.sendBeacon: a beacon cannot set
    // request headers, so it would arrive without X-CSRFToken and CSRFProtect
    // would refuse it with a 400 - a cancel that looks like it works and
    // never does. keepalive carries headers and outlives the page.
    //
    // Best effort even so, so the server-side timeout is the real backstop
    // rather than a belt-and-braces extra.
    window.addEventListener("pagehide", function () {
        if (running) {
            post(CANCEL_URL, { keepalive: true }).catch(function () {});
        }
        releaseCamera();
    });

    openCamera(null);
}());
