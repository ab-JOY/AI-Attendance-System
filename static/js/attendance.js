/*
 * The attendance session page: starting the camera, ending the session, and
 * the live recognised-students list (US-3).
 *
 * **US-3 is the reason this file is more than a move.** The recognised list was
 * rendered once, from `session_results`, which only exists *after*
 * /end-attendance has run. So the operator had to end the session to find out
 * whether anybody had been recorded - and a session silently recording nothing
 * looked exactly like a session nobody had walked past yet.
 *
 * That is not hypothetical. FS-14 was precisely this: twenty of twenty frames
 * agreeing on the right student, nothing written, nothing logged, and an
 * overlay reading "Verifying 20/20 100%" indefinitely. It was reported by a
 * person watching the screen, because the screen had no way to disagree with
 * itself.
 */
(function () {
    "use strict";

    var page = document.querySelector("[data-attendance-page]");

    if (!page) {
        return;
    }

    var CSRF_TOKEN = document
        .querySelector('meta[name="csrf-token"]')
        .getAttribute("content");

    var START_URL = page.getAttribute("data-start-url");
    var LIVE_URL = page.getAttribute("data-live-url");
    var CLASS_CODE_URL = page.getAttribute("data-class-code-url");
    var FEED_URL = page.getAttribute("data-feed-url");
    var CAMERAS_URL = page.getAttribute("data-cameras-url");
    var SELECT_CAMERA_URL = page.getAttribute("data-select-camera-url");
    var FRAME_URL = page.getAttribute("data-frame-url");
    var LOGIN_URL = page.getAttribute("data-login-url");

    // The resolution recognition judges faces at. Sent from the server
    // rather than restated here (see web/sessions.py).
    var RECOGNITION_WIDTH = Number(page.getAttribute("data-recognition-width"));
    var RECOGNITION_HEIGHT = Number(page.getAttribute("data-recognition-height"));

    // /attendance/frame refuses anything narrower (MIN_NATIVE_FRAME_WIDTH).
    var MIN_FRAME_WIDTH = Number(page.getAttribute("data-min-frame-width"));

    // Slow enough not to hammer a server that is also decoding frames - the
    // recognition loop costs 48-147 ms per frame on this machine
    // (docs/benchmarks.md §3) - and fast enough that a student who has just
    // been recognised sees their name while still standing there.
    var POLL_MS = 2000;

    var startForm = document.getElementById("startForm");
    var endForm = document.getElementById("endForm");
    var camera = document.getElementById("camera");
    var cameraText = document.getElementById("cameraText");
    var liveList = document.getElementById("live-recognised");
    var liveCount = document.getElementById("live-count");
    var sessionNotice = document.getElementById("session-notice");
    var cameraSelect = document.getElementById("camera-select");
    var cameraDetail = document.getElementById("camera-detail");

    var timer = null;

    function notify(message, kind) {
        if (!sessionNotice) {
            return;
        }

        sessionNotice.textContent = message;
        sessionNotice.className = "flash flash-" + (kind || "info");
        sessionNotice.hidden = !message;
    }

    /* ---------------------------------------------------------------
     * The refusal dialog
     *
     * ⚠️ **Why this exists, and why it is not alert().**
     *
     * `notify()` writes to a strip at the top of the page. On a phone the
     * Start button is below the fold, so a refusal scrolled nothing and
     * changed nothing the operator could see: pressing Start appeared to do
     * nothing at all, which reads as a broken button rather than as a
     * deliberate refusal. Reported exactly that way.
     *
     * US-1/US-2 removed `alert()` from this page and the reasons still hold -
     * it is unreadable to a screen reader until dismissed and gone the moment
     * it is, so the message had to be *remembered*. A <dialog> is none of
     * those things: it is labelled, it is announced, Escape closes it, focus
     * is trapped and returned, and it can carry the button that fixes the
     * problem instead of only describing it.
     *
     * **`notify()` is still called alongside it**, so dismissing the dialog
     * leaves the message on the page rather than erasing it. That is the half
     * of US-1/US-2 that a modal on its own would undo.
     * --------------------------------------------------------------- */

    var dialog = document.getElementById("session-dialog");
    var dialogMessage = document.getElementById("session-dialog-message");
    var dialogAction = document.getElementById("session-dialog-action");
    var dialogClose = document.getElementById("session-dialog-close");

    if (dialogClose && dialog) {
        dialogClose.addEventListener("click", function () {
            dialog.close();
        });
    }

    function refuse(message, action) {
        // Always, and first: the page keeps the message whether or not the
        // dialog can be shown.
        notify(message, "error");

        if (!dialog || typeof dialog.showModal !== "function") {
            // No <dialog> support. The notice above is the whole behaviour,
            // which is exactly what this page did before - degraded, not
            // broken.
            return;
        }

        dialogMessage.textContent = message;

        if (action && action.url) {
            dialogAction.textContent = action.label || "Fix this";
            dialogAction.href = action.url;
            dialogAction.hidden = false;
        } else {
            // Cleared, not left over: the dialog is reused, and a stale button
            // would send the next refusal somewhere unrelated.
            dialogAction.hidden = true;
            dialogAction.removeAttribute("href");
            dialogAction.textContent = "";
        }

        if (!dialog.open) {
            dialog.showModal();
        }
    }

    /* ---------------------------------------------------------------
     * The live list
     * --------------------------------------------------------------- */

    function renderStudents(students) {
        liveList.textContent = "";

        if (!students.length) {
            var empty = document.createElement("li");
            empty.className = "empty-state";
            empty.textContent = "Nobody recognised yet.";
            liveList.appendChild(empty);
            return;
        }

        students.forEach(function (student) {
            var item = document.createElement("li");
            item.className = "student-card";

            /*
             * textContent throughout, never innerHTML. A student's name comes
             * from the database and is rendered by Jinja everywhere else,
             * which escapes it; building this list as an HTML string would be
             * the one place in the application where a stored name reaches the
             * DOM as markup.
             */
            var name = document.createElement("strong");
            name.textContent = student.name;

            var meta = document.createElement("p");
            meta.textContent =
                student.student_id +
                (student.time_in ? " · " + student.time_in : "") +
                (student.status ? " · " + student.status : "");

            item.appendChild(name);
            item.appendChild(meta);
            liveList.appendChild(item);
        });
    }

    function poll() {
        fetch(LIVE_URL, { credentials: "same-origin" })
            .then(function (response) {
                if (response.status === 401) {
                    // The session expired while the page sat open. Asking
                    // again cannot help.
                    window.location.href = LOGIN_URL;
                    throw new Error("signed out");
                }

                if (!response.ok) {
                    throw new Error("status " + response.status);
                }

                return response.json();
            })
            .then(function (body) {
                if (!body.running) {
                    stopPolling();
                    stopUploading();
                    streamWanted = false;
                    hideClassCode();
                    return;
                }

                refreshClassCode();

                // Reloaded mid-session on the device feeding it: pick the
                // uploads back up, or recognition has nothing to look at.
                if (body.camera === "browser" && !uploading && !resuming) {
                    resumeBrowserSession();
                }

                if (liveCount) {
                    liveCount.textContent = body.recognised;
                }

                renderStudents(body.students || []);
                schedule();
            })
            .catch(function (error) {
                // Stop rather than retry. A poll that fails every two seconds
                // for the rest of the class is noise in the log and load on a
                // server that is busy recognising faces.
                stopPolling();

                if (error.message !== "signed out") {
                    notify(
                        "The live list stopped updating (" + error.message +
                        "). The session is unaffected; reload to resume.",
                        "warning"
                    );
                }
            });
    }

    function schedule() {
        stopPolling();
        timer = window.setTimeout(poll, POLL_MS);
    }

    /* ---------------------------------------------------------------
     * The classroom code (services/classroom_code.py)
     *
     * Refreshed on the live poll, every POLL_MS, and once more the moment
     * the code changes: a code lasts PERIOD_SECONDS (6 s), so waiting for the
     * next poll would leave a stale one up for a third of its life. The digits
     * are replaced only when the code changes. A
     * failure hides nothing that was shown - the last code stays valid for
     * up to two periods - and is not reported: the live list's own poll
     * already says when the server stops answering.
     * ------------------------------------------------------------- */

    var classCodePanel = document.getElementById("class-code-panel");
    var classCodeDigits = document.getElementById("class-code-digits");
    var classCodeLeft = document.getElementById("class-code-left");
    var classCodePeriod = document.getElementById("class-code-period");
    var shownCode = null;
    var classCodeTimer = null;

    function cancelClassCodeTimer() {
        if (classCodeTimer !== null) {
            window.clearTimeout(classCodeTimer);
            classCodeTimer = null;
        }
    }

    function hideClassCode() {
        if (classCodePanel) {
            classCodePanel.hidden = true;
        }
        shownCode = null;
        cancelClassCodeTimer();
    }

    function refreshClassCode() {
        if (!classCodePanel || !CLASS_CODE_URL) {
            return;
        }

        fetch(CLASS_CODE_URL, { credentials: "same-origin" })
            .then(function (response) {
                return response.ok ? response.json() : null;
            })
            .then(function (body) {
                if (!body || !body.running || !body.allowed) {
                    hideClassCode();
                    return;
                }

                if (body.code !== shownCode) {
                    shownCode = body.code;
                    // Two groups of three, easier to read off a projector.
                    classCodeDigits.textContent =
                        body.code.slice(0, 3) + " " + body.code.slice(3);
                }

                classCodeLeft.textContent = body.seconds_left;
                classCodePeriod.textContent = body.period;
                classCodePanel.hidden = false;

                // A little after the change, so the server's clock has moved
                // on to the new step when it is asked.
                cancelClassCodeTimer();
                classCodeTimer = window.setTimeout(
                    refreshClassCode, body.seconds_left * 1000 + 250
                );
            })
            .catch(function () {
                // See above: keep showing the last code.
            });
    }

    function stopPolling() {
        if (timer !== null) {
            window.clearTimeout(timer);
            timer = null;
        }
    }

    /* ---------------------------------------------------------------
     * Locking the End control to the running session (R3/FS-16)
     *
     * While a session is running there is exactly one subject the End form
     * may legitimately carry - the running one - because the register is
     * written for the *session*, not for whatever this dropdown says. Leaving
     * it free meant the only feedback on a mis-selection was a 409 after the
     * fact.
     *
     * ⚠️ **This is a convenience, not the guard.** A disabled control is
     * absent from a curl, a replayed POST or a browser with scripting off,
     * and what it stands in front of is a register written for a subject that
     * was never taught - referentially valid, factually wrong, and silent.
     * /end-attendance still refuses a mismatch. Do not treat this as having
     * made that check redundant.
     *
     * A disabled <select> submits nothing, so the value travels in a hidden
     * input. The server renders the same pair when the page loads with a
     * session already running; these two have to agree, which is why the
     * element ids are shared.
     * --------------------------------------------------------------- */

    var endSubject = document.getElementById("end_subject_id");

    function lockEndSubject(subjectId) {
        if (!endSubject || !subjectId) {
            return;
        }

        endSubject.value = String(subjectId);
        endSubject.disabled = true;

        // Inserted immediately after the select, which is where the template
        // puts them when the page loads with a session already running. The
        // two paths produce the same DOM on purpose: one of them is what a
        // reload shows, and a difference between them would be a difference
        // nobody would think to look for.
        var anchor = endSubject;

        if (!document.getElementById("end_subject_id_locked")) {
            var hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.id = "end_subject_id_locked";
            hidden.name = "subject_id";
            hidden.value = String(subjectId);
            anchor.parentNode.insertBefore(hidden, anchor.nextSibling);
            anchor = hidden;
        }

        if (!document.getElementById("end-subject-lock-note")) {
            var note = document.createElement("p");
            note.className = "hint";
            note.id = "end-subject-lock-note";
            note.textContent =
                "Locked to the session that is running. End it to choose a " +
                "different subject.";
            anchor.parentNode.insertBefore(note, anchor.nextSibling);
        }
    }

    /* ---------------------------------------------------------------
     * Which camera: this device's, or the classroom camera
     *
     * "browser" uses getUserMedia on whatever device this page is open on
     * and uploads its frames; "server" is the camera connected to the
     * machine running the system, opened there by OpenCV. Recognition is
     * the same code either way - only where the pixels come from differs.
     *
     * The default follows where the page is opened: on the server itself
     * (localhost) the classroom camera is right there; from anywhere else
     * it is in another room, and this device's camera is the useful one.
     * --------------------------------------------------------------- */

    var SOURCE_KEY = "attendance-camera-source";

    var sourceRadios = document.querySelectorAll('input[name="camera_source"]');
    var browserPicker = document.getElementById("browser-camera-picker");
    var serverPicker = document.getElementById("server-camera-picker");
    var browserSelect = document.getElementById("browser-camera-select");
    var browserDetail = document.getElementById("browser-camera-detail");
    var preview = document.getElementById("browser-preview");

    var browserStream = null;
    var serverCamerasLoaded = false;

    function onThisMachine() {
        var host = window.location.hostname;
        return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
    }

    function savedSource() {
        // localStorage can throw (private window, blocked site data). The
        // default is then the whole behaviour, which is fine.
        try {
            var value = window.localStorage.getItem(SOURCE_KEY);
            if (value === "browser" || value === "server") { return value; }
        } catch (ignored) { /* fall through to the default */ }

        return onThisMachine() ? "server" : "browser";
    }

    function rememberSource(value) {
        try {
            window.localStorage.setItem(SOURCE_KEY, value);
        } catch (ignored) { /* a convenience only */ }
    }

    function chosenSource() {
        for (var i = 0; i < sourceRadios.length; i += 1) {
            if (sourceRadios[i].checked) { return sourceRadios[i].value; }
        }
        return "server";
    }

    function sayAboutWebcam(text) {
        if (browserDetail) { browserDetail.textContent = text; }
    }

    function releaseBrowserCamera() {
        // Any request still opening a camera is now stale; see openBrowserCamera.
        cameraRequest += 1;
        browserWidth = 0;

        if (browserStream) {
            browserStream.getTracks().forEach(function (track) { track.stop(); });
            browserStream = null;
        }

        if (preview) {
            preview.srcObject = null;
            preview.hidden = true;
        }

        clearFaces();
    }

    function listWebcams(activeId) {
        if (!navigator.mediaDevices.enumerateDevices) { return; }

        navigator.mediaDevices.enumerateDevices().then(function (devices) {
            var webcams = devices.filter(function (d) { return d.kind === "videoinput"; });

            browserSelect.textContent = "";

            webcams.forEach(function (device, index) {
                var option = new Option(
                    device.label || ("Camera " + (index + 1)),
                    device.deviceId
                );
                option.selected = device.deviceId === activeId;
                browserSelect.appendChild(option);
            });

            browserSelect.disabled = webcams.length < 2;
        });
    }

    // Bumped by every open and every release. A getUserMedia() that resolves
    // after a newer request has started is stale: its stream is stopped and
    // thrown away. Without this, choosing webcams quickly let an older request
    // finish last, overwrite the newer stream and keep holding the device -
    // so the next open failed with "in use" (reported as "sometimes the webcam
    // won't open").
    var cameraRequest = 0;

    // Windows can take a moment to let go of a webcam after its track is
    // stopped. NotReadableError right after a switch is usually that, not
    // another application.
    var CAMERA_RETRY_MS = 700;

    /* The webcam this device last used successfully, so a reload - including
     * the one /end-attendance causes - reopens it instead of the browser's
     * default. That default is usually the built-in camera, often 640x480,
     * which the server refuses: reported as "it works, but after End and
     * Start again it says the camera is only 640 pixels wide". */
    var WEBCAM_KEY = "attendance-webcam-id";

    function savedWebcam() {
        try { return window.localStorage.getItem(WEBCAM_KEY); } catch (ignored) { return null; }
    }

    function rememberWebcam(id) {
        try { window.localStorage.setItem(WEBCAM_KEY, id); } catch (ignored) { /* convenience */ }
    }

    // The width the open webcam actually delivers; 0 when none is open.
    var browserWidth = 0;

    function tooSmallMessage(width, height) {
        return "This webcam gives only " + width + "x" + height + ". Attendance " +
               "needs one at least " + MIN_FRAME_WIDTH + " pixels wide - choose " +
               "another webcam from the list.";
    }

    // When the camera was not chosen by hand and is too small, try the
    // device's other webcams and keep the first one that is wide enough.
    function tryAnotherWebcam(tried) {
        return navigator.mediaDevices.enumerateDevices().then(function (devices) {
            var next = devices.filter(function (d) {
                return d.kind === "videoinput" && tried.indexOf(d.deviceId) === -1;
            })[0];

            // null: nothing left to try, so the caller keeps what it has open.
            return next ? openBrowserCamera(next.deviceId, false, tried) : null;
        });
    }

    // `tried` is set only while searching for a wide-enough webcam: those
    // opens were not chosen by hand, so a too-small result moves on.
    function openBrowserCamera(deviceId, isRetry, tried) {
        releaseBrowserCamera();
        var request = cameraRequest;
        var chosenByHand = Boolean(deviceId) && !tried;

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            // Absent rather than failing: the page is not in a secure context.
            sayAboutWebcam(
                "This browser will not give the page a camera. Open the system " +
                "through its https:// address."
            );
            return Promise.resolve(false);
        }

        var video = {
            width: { ideal: RECOGNITION_WIDTH },
            height: { ideal: RECOGNITION_HEIGHT }
        };

        if (deviceId) {
            // `exact`, so a chosen webcam that cannot be opened says so
            // instead of silently falling back to the built-in one.
            video.deviceId = { exact: deviceId };
        } else if (savedWebcam()) {
            // `ideal`: the remembered webcam if it is still plugged in, the
            // default if not.
            video.deviceId = { ideal: savedWebcam() };
        }

        sayAboutWebcam("Opening the camera…");

        return navigator.mediaDevices.getUserMedia({ video: video, audio: false })
            .then(function (stream) {
                if (request !== cameraRequest) {
                    stream.getTracks().forEach(function (track) { track.stop(); });
                    return false;
                }

                browserStream = stream;
                preview.srcObject = stream;
                preview.play().catch(function () { /* autoplay covers it */ });
                preview.hidden = false;
                cameraText.hidden = true;

                var track = stream.getVideoTracks()[0];
                var settings = track.getSettings ? track.getSettings() : {};

                track.addEventListener("ended", function () {
                    stopUploading();
                    notify("The webcam was disconnected. Reconnect it and reload " +
                           "the page to keep taking attendance.", "error");
                });

                listWebcams(settings.deviceId);

                browserWidth = settings.width || 0;

                if (browserWidth && browserWidth < MIN_FRAME_WIDTH) {
                    if (!chosenByHand) {
                        var alreadyTried = (tried || []).concat([settings.deviceId]);

                        return tryAnotherWebcam(alreadyTried).then(function (result) {
                            if (result === null) {
                                // No other webcam to try: this one stays open,
                                // and Start will refuse it with this reason.
                                sayAboutWebcam(tooSmallMessage(settings.width, settings.height));
                                return true;
                            }
                            return result;
                        });
                    }

                    sayAboutWebcam(tooSmallMessage(settings.width, settings.height));
                    return true;
                }

                // Only a webcam that works is worth reopening next time.
                if (settings.deviceId) { rememberWebcam(settings.deviceId); }

                sayAboutWebcam(
                    "Webcam ready" +
                    (settings.width ? " (" + settings.width + "x" + settings.height + ")" : "") +
                    ". If the picture is black or frozen, choose another webcam."
                );

                return true;
            })
            .catch(function (error) {
                if (request !== cameraRequest) { return false; }

                var name = error && error.name ? error.name : "unknown error";

                if (!isRetry && (name === "NotReadableError" || name === "AbortError")) {
                    sayAboutWebcam("The webcam is busy, trying again…");

                    return new Promise(function (resolve) {
                        window.setTimeout(resolve, CAMERA_RETRY_MS);
                    }).then(function () {
                        return request === cameraRequest
                            ? openBrowserCamera(deviceId, true)
                            : false;
                    });
                }

                sayAboutWebcam(
                    name === "NotAllowedError"
                        ? "Camera access was refused. Allow it in the browser's " +
                          "address bar, then choose the camera again."
                        : "The webcam could not be opened (" + name + "). Close " +
                          "any other app using it (Zoom, Teams, the Camera app), " +
                          "then choose it again."
                );
                return false;
            });
    }

    function showSource(source) {
        browserPicker.hidden = source !== "browser";
        serverPicker.hidden = source !== "server";

        if (source === "browser") {
            openBrowserCamera(browserSelect.value || null);
            return;
        }

        releaseBrowserCamera();
        cameraText.hidden = camera.style.display === "block";

        // The scan opens every device on the server and can take seconds, so
        // it runs only when the classroom camera is actually wanted.
        if (!serverCamerasLoaded) {
            serverCamerasLoaded = true;
            loadCameras();
        }
    }

    /* ---------------------------------------------------------------
     * The recognition results, drawn over this device's own picture
     *
     * ⚠️ **The page does not show the server's stream in this mode.** That
     * sent every picture across the internet twice - up, then back down
     * annotated - and through the Cloudflare tunnel a frame took ~1.3 s, so
     * recognition saw under one frame a second and never drew a box. The
     * webcam is shown directly instead (no lag at all), and each upload's
     * reply carries the boxes, names and statuses the server would have
     * drawn, in its 1280x720 recognition frame.
     * --------------------------------------------------------------- */

    var overlay = document.getElementById("face-overlay");

    function clearFaces() {
        if (!overlay) { return; }
        overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
    }

    function drawFaces(faces) {
        if (!overlay || !preview.videoWidth || faces === null || faces === undefined) {
            return;
        }

        var ratio = window.devicePixelRatio || 1;
        var shownWidth = overlay.clientWidth;
        var shownHeight = overlay.clientHeight;

        overlay.width = Math.round(shownWidth * ratio);
        overlay.height = Math.round(shownHeight * ratio);

        var context = overlay.getContext("2d");
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        context.clearRect(0, 0, shownWidth, shownHeight);

        var videoWidth = preview.videoWidth;
        var videoHeight = preview.videoHeight;

        // 1. Recognition frame -> video pixels. The server centre-cropped the
        //    upload to 16:9 and scaled it to RECOGNITION_WIDTH (infra/uploads.py).
        var target = RECOGNITION_WIDTH / RECOGNITION_HEIGHT;
        var cropX = 0;
        var cropY = 0;
        var perPixel;

        if (videoWidth / videoHeight > target) {
            var croppedWidth = videoHeight * target;
            cropX = (videoWidth - croppedWidth) / 2;
            perPixel = croppedWidth / RECOGNITION_WIDTH;
        } else {
            var croppedHeight = videoWidth / target;
            cropY = (videoHeight - croppedHeight) / 2;
            perPixel = videoWidth / RECOGNITION_WIDTH;
        }

        // 2. Video pixels -> the element, which shows it with object-fit:cover.
        var cover = Math.max(shownWidth / videoWidth, shownHeight / videoHeight);
        var offsetX = (shownWidth - videoWidth * cover) / 2;
        var offsetY = (shownHeight - videoHeight * cover) / 2;

        function toScreenX(x) { return offsetX + (cropX + x * perPixel) * cover; }
        function toScreenY(y) { return offsetY + (cropY + y * perPixel) * cover; }

        if (!faces.length) {
            context.fillStyle = "#ff0000";
            context.font = "bold 18px sans-serif";
            context.fillText("No valid face detected", 16, 30);
            return;
        }

        faces.forEach(function (face) {
            var x1 = toScreenX(face.box[0]);
            var y1 = toScreenY(face.box[1]);
            var x2 = toScreenX(face.box[2]);
            var y2 = toScreenY(face.box[3]);

            context.strokeStyle = face.color;
            context.fillStyle = face.color;
            context.lineWidth = 3;
            context.strokeRect(x1, y1, x2 - x1, y2 - y1);

            // Canvas text, never HTML: a student's name cannot become markup.
            context.font = "bold 18px sans-serif";
            context.fillText(face.name, x1, Math.max(y1 - 8, 20));

            context.font = "bold 15px sans-serif";
            context.fillText(
                face.status + " (" + face.score + ")",
                x1,
                Math.min(y2 + 22, shownHeight - 6)
            );
        });
    }

    /* ---------------------------------------------------------------
     * Uploading frames
     *
     * Up to MAX_IN_FLIGHT frames on the wire at once, never more often than
     * MIN_FRAME_MS. One at a time capped the rate at one frame per round trip
     * - ~0.35 s to Cloudflare and back before a single byte of picture -
     * whatever the bandwidth. Two hides that wait; more would only queue
     * frames behind a slow connection.
     *
     * Frames are UPLOAD_WIDTH wide at UPLOAD_QUALITY: 44 KB rather than the
     * 139 KB of a 1280-wide frame at 0.85, measured on a real webcam frame.
     * Recognition still confirms an enrolled student and passes liveness at
     * exactly these settings (tests/test_recognition_loop_smoke.py, browser
     * section); the server scales them up to 1280x720 to judge. 960 is also
     * the narrowest frame the server accepts (MIN_NATIVE_FRAME_WIDTH).
     *
     * ⚠️ Never mirrored. drawImage() samples the decoded video, which no CSS
     * transform touches - and a mirrored upload would turn "turn left" in
     * the liveness challenge into a turn to the right (see enrol.js).
     * --------------------------------------------------------------- */

    var UPLOAD_WIDTH = 960;
    var UPLOAD_QUALITY = 0.6;

    // ~8 frames a second: recognition handles about 6.8 with a face in shot
    // (docs/benchmarks.md), so sending faster only costs bandwidth.
    var MIN_FRAME_MS = 125;
    var MAX_IN_FLIGHT = 2;

    // Below this, a face track can expire between frames (1.5 s) and the
    // liveness steps can time out (8 s each), so a check may restart.
    var SLOW_FPS = 3;

    var uploading = false;
    var inFlight = 0;
    var lastSentAt = 0;
    var pumpTimer = null;
    var sentInWindow = 0;
    var windowStartedAt = 0;

    function reportRate() {
        var now = Date.now();
        var elapsed = (now - windowStartedAt) / 1000;

        if (elapsed < 2) { return; }

        var fps = sentInWindow / elapsed;
        sentInWindow = 0;
        windowStartedAt = now;

        sayAboutWebcam(
            "Sending " + fps.toFixed(1) + " frames/s from this webcam." +
            (fps < SLOW_FPS
                ? " Slow connection: students may need to repeat the check."
                : "")
        );
    }

    function pumpLater(ms) {
        if (pumpTimer !== null) { return; }

        pumpTimer = window.setTimeout(function () {
            pumpTimer = null;
            pump();
        }, ms);
    }

    function pump() {
        if (!uploading || inFlight >= MAX_IN_FLIGHT) {
            // A reply arriving calls pump() again.
            return;
        }

        var wait = MIN_FRAME_MS - (Date.now() - lastSentAt);

        if (wait > 0) {
            pumpLater(wait);
            return;
        }

        if (!browserStream || !preview.videoWidth) {
            // The webcam has not produced a frame yet.
            pumpLater(MIN_FRAME_MS);
            return;
        }

        lastSentAt = Date.now();
        inFlight += 1;
        sendFrame(function () {
            inFlight -= 1;
            pump();
        });

        // Start the next one too, if the limit allows; it waits MIN_FRAME_MS.
        pump();
    }

    var scratch = document.createElement("canvas");

    function sendFrame(done) {
        var scale = Math.min(1, UPLOAD_WIDTH / preview.videoWidth);
        scratch.width = Math.round(preview.videoWidth * scale);
        scratch.height = Math.round(preview.videoHeight * scale);
        scratch.getContext("2d").drawImage(preview, 0, 0, scratch.width, scratch.height);

        scratch.toBlob(function (blob) {
            if (!blob || !uploading) { done(); return; }

            fetch(FRAME_URL, {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "image/jpeg", "X-CSRFToken": CSRF_TOKEN },
                body: blob
            })
                .then(function (response) {
                    if (response.status === 401) {
                        window.location.href = LOGIN_URL;
                        throw new Error("signed out");
                    }

                    return response.json().then(function (body) {
                        return { status: response.status, body: body };
                    });
                })
                .then(function (result) {
                    if (result.status === 409 || result.status === 400) {
                        // 409: the session ended, or is not this device's.
                        // 400: the picture itself is unusable, and sending
                        // the same camera again cannot fix it.
                        stopUploading();
                        notify(result.body.message,
                               result.status === 400 ? "error" : "info");
                        done();
                        return;
                    }

                    if (uploading) { drawFaces(result.body.faces); }

                    sentInWindow += 1;
                    reportRate();
                    done();
                })
                .catch(function (error) {
                    if (error.message === "signed out") { return; }

                    // A dropped request on a flaky connection: carry on in a
                    // moment rather than giving up on the class.
                    sayAboutWebcam("Connection interrupted, retrying…");
                    window.setTimeout(done, 1000);
                });
        }, "image/jpeg", UPLOAD_QUALITY);
    }

    /* ---------------------------------------------------------------
     * The classroom camera's live stream, retried when its slot is held
     *
     * ⚠️ For the classroom camera, recognition runs only while a /video_feed
     * viewer is open - the single-viewer rule (vision/session.py). After a
     * reload the previous page's stream still holds the slot until the
     * server notices it has gone (at most VIEWER_STALE_SECONDS, 10 s), so
     * the new request is refused with a 409. Taken once, that refusal left a
     * session with nobody recognising (lessons.md L38). So a refused stream
     * is retried while the session runs.
     * --------------------------------------------------------------- */

    var STREAM_RETRY_MS = 2000;
    var streamWanted = false;

    function showStream() {
        streamWanted = true;
        camera.style.display = "block";
        cameraText.hidden = true;
        // A fresh URL each time, or the browser may reuse the refused one.
        camera.src = FEED_URL + "?t=" + Date.now();
    }

    camera.addEventListener("error", function () {
        if (!streamWanted) { return; }

        window.setTimeout(function () {
            if (streamWanted) { showStream(); }
        }, STREAM_RETRY_MS);
    });

    function startUploading() {
        if (uploading) { return; }

        uploading = true;
        sentInWindow = 0;
        windowStartedAt = Date.now();
        pump();
    }

    function stopUploading() {
        uploading = false;
        clearFaces();
    }

    window.addEventListener("pagehide", function () {
        stopUploading();
        releaseBrowserCamera();
    });

    Array.prototype.forEach.call(sourceRadios, function (radio) {
        radio.addEventListener("change", function () {
            if (uploading) {
                // Switching mid-session would leave it with no frames.
                notify("End the session before changing the camera.", "warning");
                return;
            }

            rememberSource(radio.value);
            showSource(radio.value);
        });
    });

    if (browserSelect) {
        browserSelect.addEventListener("change", function () {
            openBrowserCamera(browserSelect.value);
        });
    }

    /* ---------------------------------------------------------------
     * Starting and ending
     * --------------------------------------------------------------- */

    startForm.addEventListener("submit", function (event) {
        event.preventDefault();

        var subject = document.getElementById("subject_id").value;

        if (!subject) {
            // Same reasoning as a server refusal: on a phone the dropdown and
            // the notice are not on screen together.
            refuse("Choose a subject first.", null);
            return;
        }

        var source = chosenSource();

        if (source === "browser" && !browserStream) {
            refuse("This device's camera is not open yet. Allow camera access, " +
                   "or choose the classroom camera.", null);
            return;
        }

        // Refused here, before a session exists: the server would refuse
        // every frame from it, and the session would run with nothing to see.
        if (source === "browser" && browserWidth && browserWidth < MIN_FRAME_WIDTH) {
            refuse(tooSmallMessage(browserWidth, preview.videoHeight || "?"), null);
            return;
        }

        var button = startForm.querySelector("button[type=submit]");
        button.disabled = true;

        fetch(START_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": CSRF_TOKEN
            },
            body: "subject_id=" + encodeURIComponent(subject) +
                  "&camera=" + encodeURIComponent(source)
        })
            .then(function (response) {
                if (response.status === 401) {
                    window.location.href = LOGIN_URL;
                    throw new Error("signed out");
                }

                return response.json();
            })
            .then(function (body) {
                button.disabled = false;

                if (!body.success) {
                    // Every refusal, not only the empty class list. They all
                    // share the same failure: the operator pressed Start,
                    // nothing visible happened, and the reason was written
                    // somewhere they were not looking. `body.action` is
                    // present only when the server has somewhere to send them.
                    refuse(body.message, body.action);
                    return;
                }

                if (body.camera === "browser") {
                    // This device's picture stays on screen, with the
                    // results drawn over it; there is no stream to open.
                    startUploading();
                } else {
                    showStream();
                }

                // R3/FS-16. The page does not reload on start, so the End
                // control has to be locked here as well as on render - and
                // to the subject the *server* says it started, not the one
                // this form happens to be showing.
                lockEndSubject(body.subject_id);

                notify("Session running. Recognised students appear below.",
                       "success");

                poll();
            })
            .catch(function (error) {
                button.disabled = false;

                if (error.message !== "signed out") {
                    refuse("The session could not be started. Reload and try "
                           + "again.", null);
                }
            });
    });

    endForm.addEventListener("submit", function () {
        // Only the poller is stopped here. The form posts normally.
        //
        // ⚠️ **This handler used to POST /stop_camera first and submit the
        // form in its .then(), and that silently disabled R3** (FS-16).
        // /end-attendance refuses a submission naming a subject other than the
        // running one - but it reads that subject from the recognition
        // session, and `RecognitionSession.stop()` sets it to None. So the
        // pre-flight stop guaranteed the route saw no running session, took
        // the "nothing to prefer" fallback, and trusted the dropdown: the
        // absent register was written for the subject that was not taught,
        // stamped session_id NULL, while the real session's row was never
        // closed. Reproduced at HTTP 200 with the register read for the wrong
        // subject.
        //
        // Nothing is lost by dropping it: /end-attendance calls stop_camera()
        // itself, a few lines after the guard. The camera is released either
        // way - the difference is that the guard now runs against a session
        // that still exists. Do not reintroduce a stop before this post.
        stopPolling();
        stopUploading();
        streamWanted = false;
    });

    /* ---------------------------------------------------------------
     * Choosing the server's camera
     *
     * ⚠️ Not the browser's camera. Enrolment asks getUserMedia and the
     * browser enumerates its own devices; recognition opens the classroom
     * camera with OpenCV on the *server*, which this page cannot see. So the
     * list has to come from a route, and picking one is a POST rather than a
     * local preference.
     *
     * The scan opens each device in turn and can take seconds - measured at
     * 14.9 s for a device another application was holding - so the select
     * says so while it runs rather than sitting empty and looking broken.
     * --------------------------------------------------------------- */

    function describeCamera(cameraInfo) {
        var text = "Camera " + cameraInfo.index;

        if (cameraInfo.name) {
            text += " - " + cameraInfo.name;
        }

        // CAM-2: a plugged-in webcam is always opened first, so say which
        // one that is.
        if (cameraInfo.external) {
            text += " (connected webcam)";
        }

        if (cameraInfo.width && cameraInfo.height) {
            text += " - " + cameraInfo.width + "x" + cameraInfo.height;
        }

        // The operator has to be told this. open_best_camera() already
        // refuses to *save* a frozen device as the default; a picker that let
        // somebody choose one without saying so would undo that silently.
        if (cameraInfo.frozen) {
            text += " (still image, not a live feed)";
        }

        return text;
    }

    function renderCameras(cameras) {
        cameraSelect.textContent = "";

        if (!cameras.length) {
            cameraSelect.disabled = true;
            cameraSelect.appendChild(new Option("No cameras found", ""));
            return;
        }

        // Preselect what the next session will actually open: a connected
        // webcam if there is one (CAM-2) - the saved one if several are -
        // otherwise the saved choice.
        var webcams = cameras.filter(function (c) { return c.external; });
        var webcam = webcams.filter(function (c) {
            return c.is_saved_default;
        })[0] || webcams[0];

        cameras.forEach(function (cameraInfo) {
            var option = new Option(
                describeCamera(cameraInfo),
                String(cameraInfo.index)
            );
            option.selected = webcam
                ? cameraInfo === webcam
                : cameraInfo.is_saved_default;
            cameraSelect.appendChild(option);
        });

        cameraSelect.disabled = false;
    }

    function sayAboutCameras(text) {
        if (cameraDetail) {
            cameraDetail.textContent = text;
        }
    }

    function loadCameras() {
        if (!cameraSelect) {
            return;
        }

        fetch(CAMERAS_URL, { credentials: "same-origin" })
            .then(function (response) {
                if (response.status === 401) {
                    window.location.href = LOGIN_URL;
                    throw new Error("signed out");
                }

                return response.json();
            })
            .then(function (body) {
                if (!body.success) {
                    cameraSelect.textContent = "";
                    cameraSelect.appendChild(new Option("Not available", ""));
                    cameraSelect.disabled = true;
                    sayAboutCameras(body.message || "");
                    return;
                }

                renderCameras(body.cameras);

                if (!body.cameras.length) {
                    sayAboutCameras(
                        "No camera on this machine produced a picture. " +
                        "Checked " + body.checked + " device(s). If a phone " +
                        "camera is being used, start its client first."
                    );
                    return;
                }

                sayAboutCameras(
                    body.cameras.length === 1
                        ? "One camera found. It will be used for the session."
                        : body.cameras.length + " cameras found. Choose which " +
                          "one the session should use."
                );
            })
            .catch(function () {
                cameraSelect.disabled = true;
                sayAboutCameras("The cameras could not be listed.");
            });
    }

    function chooseCamera() {
        var index = cameraSelect.value;

        if (index === "") {
            return;
        }

        cameraSelect.disabled = true;
        sayAboutCameras("Checking camera " + index + "…");

        fetch(SELECT_CAMERA_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": CSRF_TOKEN
            },
            body: "index=" + encodeURIComponent(index)
        })
            .then(function (response) {
                if (response.status === 401) {
                    window.location.href = LOGIN_URL;
                    throw new Error("signed out");
                }

                return response.json();
            })
            .then(function (body) {
                cameraSelect.disabled = false;
                sayAboutCameras(body.message || "");

                // The server rescans to validate the choice, so its list is
                // fresher than the one on screen - a device unplugged since
                // page load disappears here rather than staying selectable.
                if (body.cameras) {
                    renderCameras(body.cameras);
                }
            })
            .catch(function () {
                cameraSelect.disabled = false;
                sayAboutCameras("The camera could not be selected.");
            });
    }

    var resuming = false;

    function resumeBrowserSession() {
        resuming = true;

        var radio = document.querySelector('input[name="camera_source"][value="browser"]');
        if (radio) { radio.checked = true; }
        browserPicker.hidden = false;
        serverPicker.hidden = true;

        openBrowserCamera(null).then(function (opened) {
            resuming = false;

            if (!opened) {
                notify("This device was sending the camera for the running " +
                       "session. Allow camera access to continue.", "warning");
                return;
            }

            startUploading();
        });
    }

    if (cameraSelect) {
        cameraSelect.addEventListener("change", chooseCamera);
    }

    var initialSource = savedSource();
    var initialRadio = document.querySelector(
        'input[name="camera_source"][value="' + initialSource + '"]'
    );
    if (initialRadio) { initialRadio.checked = true; }
    showSource(initialSource);

    // A page reloaded mid-session - the operator refreshed, or came back to
    // the tab - picks the running session up rather than claiming there is
    // none.
    poll();
}());
