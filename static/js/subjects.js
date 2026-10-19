/*
 * "Add another section" on the Add Subject form.
 *
 * subjects.html renders one section row, `sections-0-*`, and a <template> of
 * a row numbered `__N__`. This file copies the template with the next number,
 * starting it at the previous row's instructor, program and year level, and
 * lets any row but the last one standing be removed.
 *
 * It also fills in the name of a subject code already in use (DM-11): PE1 is
 * one subject whichever department's sections take it.
 *
 * Without this file the form still works, for one section. The server decides
 * what a valid set of sections is - see `_subject_forms()` in web/subjects.py.
 */
(function () {
    "use strict";

    var rows = document.querySelector("[data-section-rows]");
    var template = document.querySelector("[data-section-template]");
    var add = document.querySelector("[data-add-section]");

    if (!rows || !template || !add) {
        return;
    }

    // Never reused, so a removed row's number cannot come back on a new row
    // and mix the two rows' fields. The server reads rows in number order;
    // gaps are fine.
    var next = rows.querySelectorAll("[data-section-row]").length;

    function allRows() {
        return Array.prototype.slice.call(
            rows.querySelectorAll("[data-section-row]")
        );
    }

    function refresh() {
        var current = allRows();

        current.forEach(function (row, index) {
            row.querySelector("[data-section-number]").textContent = index + 1;
            row.querySelector("[data-remove-section]").hidden = current.length < 2;
        });
    }

    function copyChoice(from, to, field) {
        var source = from.querySelector('[name$="-' + field + '"]');
        var target = to.querySelector('[name$="-' + field + '"]');

        if (source && target) {
            target.value = source.value;
        }
    }

    add.addEventListener("click", function () {
        var html = template.innerHTML.replace(/__N__/g, String(next));
        var holder = document.createElement("div");

        holder.innerHTML = html;

        var row = holder.querySelector("[data-section-row]");
        var current = allRows();
        var previous = current[current.length - 1];

        if (previous) {
            copyChoice(previous, row, "instructor_id");
            copyChoice(previous, row, "program_id");
            copyChoice(previous, row, "year_level");
        }

        rows.appendChild(row);
        next += 1;
        refresh();

        row.querySelector('[name$="-section"]').focus();
    });

    rows.addEventListener("click", function (event) {
        var button = event.target.closest("[data-remove-section]");

        if (!button || allRows().length < 2) {
            return;
        }

        button.closest("[data-section-row]").remove();
        refresh();
    });

    add.hidden = false;
    refresh();

    var code = document.getElementById("subject_code");
    var name = document.getElementById("subject_name");
    var hint = document.querySelector("[data-known-subject-hint]");
    var known = {};

    try {
        Object.entries(JSON.parse(code.getAttribute("data-known-subjects") || "{}"))
            .forEach(function (entry) {
                // Codes match the way the database matches them: `pe1` is `PE1`.
                known[entry[0].trim().toLowerCase()] = entry[1];
            });
    } catch (error) {
        known = {};
    }

    function fillName() {
        var existing = known[code.value.trim().toLowerCase()];

        if (existing !== undefined) {
            name.value = existing;
        }

        name.readOnly = existing !== undefined;
        hint.hidden = existing === undefined;
    }

    code.addEventListener("input", fillName);
    fillName();
}());
