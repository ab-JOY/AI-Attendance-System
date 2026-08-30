/*
 * The model training panel (PE-5, US-2).
 *
 * Training used to run inside the HTTP request, so there was nothing to show
 * progress *for* - the page simply did not answer until it was over.
 * /train_model starts a job and answers 202; this polls /train_status.
 *
 * **Stages, not a percentage.** Training is ~20 s at three students, so the
 * panel shows which stage the run is in and how long it has taken - both true -
 * rather than a percentage that would have to be invented.
 *
 * Lifted out of templates/training_status.html by Phase 5 (US-8).
 */
(function () {
    "use strict";

    // The panel is an include, so it is present on some pages and not
    // others. Absent means there is nothing to poll.
    var panel = document.getElementById("training-panel");

    if (!panel) {
        return;
    }

    var CSRF_TOKEN = document
        .querySelector('meta[name="csrf-token"]')
        .getAttribute("content");
    var STATUS_URL = panel.getAttribute("data-status-url");
    var START_URL = panel.getAttribute("data-start-url");

    var POLL_MS = 1500;

    var statusText = document.getElementById("training-status");
    var button     = document.getElementById("retrain-button");
    var timer      = null;

    function describe(status) {
        if (status.running) {
            var stage = status.stage || "Working";
            var seconds = status.elapsed_seconds;
            return "Training: " + stage +
                   (seconds === null ? "" : " (" + seconds + "s)");
        }

        if (status.state === "succeeded") {
            return "Last training succeeded. " + (status.message || "");
        }

        if (status.state === "failed") {
            return "Last training FAILED. " + (status.message || "");
        }

        return "Idle. No training has run since the server started.";
    }

    function render(status) {
        statusText.textContent = describe(status);
        button.disabled = status.running;
        button.textContent = status.running ? "Training…" : "Retrain model";

        if (status.running) {
            schedule();
        } else if (timer !== null) {
            window.clearTimeout(timer);
            timer = null;
        }
    }

    function schedule() {
        if (timer !== null) {
            window.clearTimeout(timer);
        }
        timer = window.setTimeout(poll, POLL_MS);
    }

    function poll() {
        fetch(STATUS_URL, { credentials: "same-origin" })
            .then(function (response) {
                if (!response.ok) {
                    // An expired session is the likely cause. The endpoint is
                    // @json_api so the hook answers JSON rather than an HTML
                    // login page, but a non-OK status still means stop asking.
                    throw new Error("status " + response.status);
                }
                return response.json();
            })
            .then(render)
            .catch(function (error) {
                statusText.textContent =
                    "Could not read the training status (" + error.message +
                    "). Reload the page.";
                button.disabled = false;
                button.textContent = "Retrain model";
            });
    }

    button.addEventListener("click", function () {
        button.disabled = true;
        button.textContent = "Starting…";

        fetch(START_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRFToken": CSRF_TOKEN }
        })
            .then(function (response) { return response.json(); })
            .then(function (body) {
                if (body.status) {
                    render(body.status);
                } else {
                    poll();
                }
            })
            .catch(function () {
                statusText.textContent =
                    "Could not start training. Reload the page and try again.";
                button.disabled = false;
                button.textContent = "Retrain model";
            });
    });

    // One check on load, so a page opened while a retrain is already running
    // shows it rather than claiming the model is idle.
    poll();
}());
