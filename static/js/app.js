/*
 * Behaviour shared by every page (US-2, US-5, US-7, US-8).
 *
 * US-8: `static/js/script.js` was a committed empty file and every line of
 * JavaScript in this application lived inline in a template. Inline script is
 * also what makes the US-5 bug below possible in the first place, because it
 * is the only place a server-rendered value ends up *inside* a JavaScript
 * string literal.
 *
 * Loaded with `defer` from base.html, so the DOM is parsed before any of this
 * runs and nothing here needs a DOMContentLoaded wrapper.
 */
(function () {
    "use strict";

    /* =====================================================================
     * US-5 - the confirmation that silently did not happen
     *
     * The delete buttons were written as:
     *
     *     onsubmit="return confirm('Delete {{ student.name }}?')"
     *
     * Jinja escapes `'` to `&#39;` for the attribute, and the browser decodes
     * it back to `'` *before* handing the value to the JavaScript parser. So a
     * student named `O'Brien` produced
     *
     *     return confirm('Delete O'Brien?')
     *
     * which is a syntax error. The handler never compiled, `onsubmit` was
     * therefore absent, and **the form submitted with no confirmation at all**
     * - the opposite of what the attribute was there for, on exactly the
     * records where a mis-click is least recoverable.
     *
     * An attribute *value* has no such problem: it is text, read with
     * getAttribute, never parsed as code. One delegated listener covers every
     * form on every page, including ones added later.
     * ===================================================================== */
    document.addEventListener("submit", function (event) {
        var form = event.target;
        var message = form.getAttribute("data-confirm");

        if (message && !window.confirm(message)) {
            event.preventDefault();
            return;
        }

        markBusy(form);
    });

    /* =====================================================================
     * US-2 - loading states
     *
     * Enrolment and retraining take minutes. A form that has been submitted
     * looks identical to one that has not, so the operator clicks again - and
     * a second POST to /enrol or /delete_student is a real second request.
     *
     * The button is disabled and relabelled rather than the form being
     * blocked, so the page still says what is happening.
     * ===================================================================== */
    /*
     * ⚠️ Re-enabled on the browser's back/forward cache restore.
     *
     * Firefox and Safari serve a "back" navigation from bfcache, which
     * restores the DOM exactly as it was left - including a permanently
     * disabled button. Without this, going back to a form leaves it unusable
     * until a manual reload, which reads as a broken page.
     *
     * ⚠️ **Registered once, here, rather than inside markBusy().** It used to
     * be added per call, so every form submission left another window listener
     * behind - and each captured its own `button`, so a page the operator
     * submitted, went back to, and submitted again accumulated closures over
     * buttons no longer on screen. Nothing visible broke, which is why it
     * survived; it is a leak whose size is "how much has this operator done
     * today".
     *
     * One listener that finds the busy buttons when it fires is also more
     * correct: it covers a button disabled by a submission this closure never
     * saw.
     */
    window.addEventListener("pageshow", function (event) {
        if (!event.persisted) {
            return;
        }

        var buttons = document.querySelectorAll("[data-busy-label][disabled]");

        Array.prototype.forEach.call(buttons, function (button) {
            button.disabled = false;

            if (button.dataset.idleLabel) {
                button.textContent = button.dataset.idleLabel;
            }
        });
    });

    function markBusy(form) {
        var button = form.querySelector("[data-busy-label]");

        if (!button || button.disabled) {
            return;
        }

        button.dataset.idleLabel = button.textContent.trim();
        button.textContent = button.getAttribute("data-busy-label");
        button.disabled = true;
    }

    /* =====================================================================
     * US-7 - the sidebar at narrow widths
     *
     * The layout is a fixed 250px sidebar and a margin to clear it, which is
     * most of a phone's screen. The stylesheet turns the sidebar into a
     * collapsed panel below the breakpoint; this is the control that opens it.
     *
     * `aria-expanded` rather than a class alone, so the state is available to
     * a screen reader and not only to the eye.
     * ===================================================================== */
    var navToggle = document.querySelector("[data-nav-toggle]");

    if (navToggle) {
        navToggle.addEventListener("click", function () {
            var open = document.body.classList.toggle("nav-open");
            navToggle.setAttribute("aria-expanded", open ? "true" : "false");
        });
    }

    /* =====================================================================
     * The login page's placeholder swap
     *
     * Was inline in login.html. The mapping is on the <select> as data-
     * attributes so this file knows nothing about roles.
     * ===================================================================== */
    var placeholderSource = document.querySelector("[data-placeholder-for]");

    if (placeholderSource) {
        var target = document.getElementById(
            placeholderSource.getAttribute("data-placeholder-for")
        );

        if (target) {
            placeholderSource.addEventListener("change", function () {
                var chosen = placeholderSource.value;

                target.placeholder =
                    placeholderSource.getAttribute("data-placeholder-" + chosen) ||
                    placeholderSource.getAttribute("data-placeholder-default") ||
                    "";
            });
        }
    }
}());
