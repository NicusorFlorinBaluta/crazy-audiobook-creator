/**
 * Shared DOM helpers for the dashboard frontend.
 *
 * This file must load before app.js, pipeline.js, script-viewer.js and
 * log-console.js, which all rely on `escapeHtml`.
 *
 * Why it exists: `escapeHtml` was defined four separate times -- once in each
 * of those files -- with three different bodies. The copy in app.js omitted
 * `.toString()`, so passing a number threw "unsafe.replace is not a
 * function", and the copy inside log-console.js escaped only `&`, `<` and `>`,
 * which is safe in text position but not inside an attribute. One definition
 * removes the chance of the strictest copy being the one that gets dropped.
 */

/**
 * Escape a value for safe interpolation into HTML, including attributes.
 *
 * Book text, character names, LLM output and error messages all reach the DOM
 * through `innerHTML` in the script viewer and log console, so quotes must be
 * escaped as well as angle brackets.
 *
 * @param {unknown} unsafe Any value; `null`/`undefined`/`''` become `''`.
 * @returns {string} An HTML-safe string.
 */
function escapeHtml(unsafe) {
    if (unsafe === null || unsafe === undefined || unsafe === '') return '';
    return String(unsafe)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

/**
 * Escape a value for text position only, preserving quote characters.
 *
 * Used by the log console, where prose readability matters and the result is
 * never interpolated into an attribute.
 *
 * @param {unknown} unsafe Any value.
 * @returns {string} A string safe to place in element text content.
 */
function escapeHtmlText(unsafe) {
    if (unsafe === null || unsafe === undefined || unsafe === '') return '';
    return String(unsafe)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}
