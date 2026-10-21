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
    var PASSWORD_TOKEN = page.getAttribute("data-password-token");
    var TOTAL = Number(page.getAttribute("data-total"));
    var MODE = page.getAttribute("data-mode");

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

    // What each upload is sent as, largest first. **Stepped down only when
    // replies are slow**, so on the server machine (localhost: ~15 ms of server
    // work per frame) every frame still goes at 1920 and 0.92, as before.
    //
    // Through a Cloudflare tunnel a 1920 frame took 5-25 s to upload
    // (measured 2026-10-19: 15-70 KB/s up), so the stage hold - several
    // frames in a row - could never complete and no image was ever saved.
    // Attendance hit the same wall first (lessons.md L39).
    //
    // The floor is the server's MIN_NATIVE_FRAME_WIDTH: infra/uploads.py
    // refuses anything narrower, because upscaling further costs the blur gate
    // real detail. Quality stays >= 0.7 because heavier JPEG blocking adds
    // false edges to the Laplacian the blur gate measures. Every gate still
    // runs on the saved crop at every tier; the server logs which widths each
    // capture was built from.
    var TIERS = [
        { width: 1920, quality: 0.92 },
        { width: 1280, quality: 0.8 },
        { width: MIN_NATIVE_WIDTH, quality: 0.7 }
    ];
    // A reply slower than this, SLOW_REPLIES times in a row, steps down a tier.
    var SLOW_REPLY_MS = 700;
    var SLOW_REPLIES = 3;

    // **Only the face is sent; the rest of the frame is flat grey.** Measured
    // through the tunnel on 2026-10-19 after the tiers above: 268 frames at
    // 60 KB took 8 min 10 s (1.8 s a frame, ~33 KB/s up) - the link, not the
    // server, sets the pace, so the only lever left is bytes. A flat area
    // costs JPEG almost nothing.
    //
    // The frame keeps its full size and the face its position, so framing,
    // distance and the stage gates see exactly the geometry they would have;
    // the crop the server saves comes from pixels that are sent unchanged.
    // The region follows the last box the server returned. Every
    // FULL_FRAME_EVERY-th upload, and any upload after a reply with no box,
    // is the whole picture - so a face that moved is found again, and the
    // server's "more than one face" check still sees the room.
    var FACE_MARGIN = 0.35;
    var FULL_FRAME_EVERY = 15;
    var MASK_GREY = "rgb(96, 96, 96)";

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
    var conditionSelect = document.getElementById("condition-select");

    var scratch = document.createElement("canvas");
    var stream = null;
    var activeDeviceId = null;
    var timer = null;
    var inFlight = false;
    var running = false;
    var captured = 0;
    var tier = 0;
    var slowReplies = 0;
    var lastBox = null;
    var uploadsSent = 0;

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

        lastBox = progress.box || null;
        drawBox(progress.box);
    }

    function faceRegion(width, height) {
        // The server's box is in canonical 1920x1080 coordinates, after
        // infra/uploads.py's `canonicalise()` centre-cropped the frame to
        // 16:9 and scaled it. This undoes both, onto the video's own pixels.
        var aspect = 1920 / 1080;
        var croppedWidth = Math.min(width, height * aspect);
        var croppedHeight = Math.min(height, width / aspect);
        var offsetX = (width - croppedWidth) / 2;
        var offsetY = (height - croppedHeight) / 2;
        var scale = croppedWidth / 1920;

        var marginX = lastBox[2] * FACE_MARGIN;
        var marginY = lastBox[3] * FACE_MARGIN;

        var left = Math.max(0, offsetX + (lastBox[0] - marginX) * scale);
        var top = Math.max(0, offsetY + (lastBox[1] - marginY) * scale);
        var right = Math.min(
            width, offsetX + (lastBox[0] + lastBox[2] + marginX) * scale
        );
        var bottom = Math.min(
            height, offsetY + (lastBox[1] + lastBox[3] + marginY) * scale
        );

        return [left, top, right - left, bottom - top];
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

        // Never upscaled here: a camera narrower than the tier is sent as is.
        var upload = TIERS[tier];
        var scale = Math.min(1, upload.width / video.videoWidth);

        scratch.width = Math.round(video.videoWidth * scale);
        scratch.height = Math.round(video.videoHeight * scale);

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
        uploadsSent += 1;

        // The region in the upload's own pixels, whole numbers, and exactly
        // what is drawn - the server is told this and refuses any crop that
        // reaches outside it (vision/enrolment.py `_crop_was_sent`).
        var sent = null;

        if (lastBox && uploadsSent % FULL_FRAME_EVERY !== 0) {
            var region = faceRegion(video.videoWidth, video.videoHeight);
            var left = Math.floor(region[0] * scale);
            var top = Math.floor(region[1] * scale);
            var right = Math.min(
                scratch.width, Math.ceil((region[0] + region[2]) * scale)
            );
            var bottom = Math.min(
                scratch.height, Math.ceil((region[1] + region[3]) * scale)
            );

            if (right > left && bottom > top) {
                sent = [left, top, right - left, bottom - top];
            }
        }

        if (sent) {
            context.fillStyle = MASK_GREY;
            context.fillRect(0, 0, scratch.width, scratch.height);
            context.drawImage(
                video,
                sent[0] / scale, sent[1] / scale,
                sent[2] / scale, sent[3] / scale,
                sent[0], sent[1], sent[2], sent[3]
            );
        } else {
            context.drawImage(video, 0, 0, scratch.width, scratch.height);
        }

        inFlight = true;

        scratch.toBlob(function (blob) {
            if (!blob) { inFlight = false; return; }

            var sentAt = Date.now();

            post(FRAME_URL, {
                headers: sent
                    ? { "Content-Type": "image/jpeg", "X-Face-Region": sent.join(",") }
                    : { "Content-Type": "image/jpeg" },
                body: blob
            })
                .then(function (response) {
                    noteReplyTime(Date.now() - sentAt);
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
        }, "image/jpeg", upload.quality);
    }

    function noteReplyTime(elapsed) {
        if (elapsed < SLOW_REPLY_MS) {
            slowReplies = 0;
            return;
        }

        slowReplies += 1;

        if (slowReplies < SLOW_REPLIES || tier === TIERS.length - 1) { return; }

        tier += 1;
        slowReplies = 0;
        showUploadSize();
    }

    function showUploadSize() {
        // Said on the page, so a capture that is slow on a slow link is not
        // mistaken for one that is broken.
        var note = document.getElementById("upload-note");

        if (!note) {
            note = document.createElement("p");
            note.id = "upload-note";
            note.className = "camera-detail";
            cameraDetail.parentNode.insertBefore(note, cameraDetail.nextSibling);
        }

        note.textContent =
            "Slow connection: sending smaller pictures (" +
            TIERS[tier].width + " wide) to keep the capture moving.";
    }

    function start() {
        // FS-20. An added session says what was different; the server decides
        // which session it is and which images it replaces.
        var condition = conditionSelect ? conditionSelect.value : null;

        if (MODE === "add_session" && !condition) {
            say("Choose what is different about this session first.", "bad");
            conditionSelect.focus();
            return;
        }

        startButton.disabled = true;
        if (conditionSelect) { conditionSelect.disabled = true; }
        say("Starting…", "wait");

        post(START_URL, {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                student_id: STUDENT_ID,
                student_name: STUDENT_NAME,
                record: RECORD,
                password_token: PASSWORD_TOKEN,
                mode: MODE,
                condition: condition
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
                    if (conditionSelect) { conditionSelect.disabled = false; }
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
                if (conditionSelect) { conditionSelect.disabled = false; }
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
