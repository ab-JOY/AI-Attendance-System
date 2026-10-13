/*
 * The Classes list on the registration form (DM-7).
 *
 * A subject is taught to a section, and a section belongs to a program, so
 * which subjects a new student can be put into follows from the three fields
 * above the list. students.html renders every subject, one <optgroup> per
 * section, each carrying its program, year level and section name; this file
 * shows the chosen program's groups and selects the group that is the
 * student's own section.
 *
 * The server refuses a subject of another program whatever this file does -
 * see `subjects_repo.existing_ids()`. Nothing here is the guard.
 */
(function () {
    "use strict";

    var picker = document.querySelector("[data-class-picker]");
    var program = document.getElementById("program_id");
    var year = document.getElementById("year_level");
    var section = document.getElementById("section");

    if (!picker || !program || !year || !section) {
        return;
    }

    var groups = Array.prototype.slice.call(picker.querySelectorAll("optgroup"));

    function refresh() {
        // Section names are matched the way the database matches them: the
        // unique key on `sections` is case-insensitive, so `4b` is `4B`.
        var typed = section.value.trim().toLowerCase();

        groups.forEach(function (group) {
            var theirs = group.getAttribute("data-program-id");

            // A group with no program is a subject not linked to a section
            // yet: it is offered to every program, and selected for nobody.
            var offered = program.value !== "" &&
                (theirs === "" || theirs === program.value);

            var own = offered && theirs !== "" &&
                group.getAttribute("data-year-level") === year.value &&
                group.getAttribute("data-section").toLowerCase() === typed;

            // Both: `hidden` is what the operator sees, and `disabled` is what
            // keeps a hidden group's subjects out of the submitted form in a
            // browser that does not hide an <optgroup>.
            group.hidden = !offered;
            group.disabled = !offered;

            Array.prototype.forEach.call(group.children, function (option) {
                option.selected = own;
            });
        });
    }

    // ⚠️ Changing where the student belongs resets the selection to their own
    // section's subjects. The three fields come before the list in the form,
    // so the operator's own changes to the selection are made afterwards and
    // are kept - nothing here runs when the list itself is clicked.
    program.addEventListener("change", refresh);
    year.addEventListener("change", refresh);
    section.addEventListener("input", refresh);

    refresh();
}());
