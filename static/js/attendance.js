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
    var FEED_URL = page.getAttribute("data-feed-url");
    var CAMERAS_URL = page.getAttribute("data-cameras-url");
    var SELECT_CAMERA_URL = page.getAttribute("data-select-camera-url");
    var LOGIN_URL = page.getAttribute("data-login-url");

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
                    return;
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

        var button = startForm.querySelector("button[type=submit]");
        button.disabled = true;

        fetch(START_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": CSRF_TOKEN
            },
            body: "subject_id=" + encodeURIComponent(subject)
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

                camera.style.display = "block";
                camera.src = FEED_URL;
                cameraText.hidden = true;

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

        cameras.forEach(function (cameraInfo) {
            var option = new Option(
                describeCamera(cameraInfo),
                String(cameraInfo.index)
            );
            option.selected = cameraInfo.is_saved_default;
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

    if (cameraSelect) {
        cameraSelect.addEventListener("change", chooseCamera);
        loadCameras();
    }

    // A page reloaded mid-session - the operator refreshed, or came back to
    // the tab - picks the running session up rather than claiming there is
    // none.
    poll();
}());
