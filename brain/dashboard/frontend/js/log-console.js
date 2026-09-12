/**
 * Log Console — Real-time pipeline log viewer
 *
 * Connects to /api/projects/{id}/logs/stream (SSE) when the Logs tab is
 * opened for an active project. Also loads the buffered history via
 * /api/projects/{id}/logs on first open so past runs are visible.
 */

window.LogConsole = (() => {
    let currentProjectId = null;
    let eventSource = null;
    let lineCount = 0;
    let projectRunning = false;
    const MAX_LINES = 500;

    // DOM refs (populated after DOMContentLoaded)
    let console$, statusEl, autoscrollCheck, clearBtn, copyBtn;
    let searchInput, levelFilter, hideNoiseCheck, wrapCheck, downloadBtn;
    let filterFrame = null;

    const ROUTINE_LINE = /health|validation cache hit|GET \/api\/.*\/status|GET \/api\/voice\/health/i;

    // ─── Log level → CSS class ───────────────────────────────────────────────
    function classifyLine(text) {
        if (/\|\s*ERROR\s*\|/.test(text))   return 'log-error';
        if (/\|\s*WARNING\s*\|/.test(text)) return 'log-warn';
        if (/\|\s*INFO\s*\|/.test(text))    return 'log-info';
        if (/\|\s*DEBUG\s*\|/.test(text))   return 'log-debug';
        if (/^\[PIPELINE ENDED\]/.test(text)) return 'log-sentinel';
        return 'log-other';
    }

    // ─── Highlight known prefixes with extra span ────────────────────────────
    function highlightLine(text) {
        // Escape first. `escapeHtmlText` (js/dom-utils.js) preserves quotes,
        // which keeps log prose readable; the result is only ever placed in
        // text position, never inside an attribute.
        const safe = escapeHtmlText(text);

        // Colour the prefix badge (e.g. "[Ollama]", "[CharacterAnalyzer]", etc.)
        return safe.replace(
            /(\[Ollama\]|\[JSON\]|\[CharacterAnalyzer\]|\[ScriptGenerator\]|\[SCRIPTING\]|\[PIPELINE ENDED\])/g,
            '<span class="log-badge">$1</span>'
        );
    }

    // ─── Append a single line element ────────────────────────────────────────
    function appendLine(text) {
        if (!text.trim()) return; // skip heartbeat blanks

        // Prune old lines if over limit
        while (lineCount >= MAX_LINES) {
            const first = console$.querySelector('.log-line');
            if (first) { first.remove(); lineCount--; }
            else break;
        }

        // Remove placeholder
        const empty = console$.querySelector('.log-empty');
        if (empty) empty.remove();

        const div = document.createElement('div');
        div.className = `log-line ${classifyLine(text)}`;
        div.dataset.raw = text;
        div.dataset.level = div.className.replace('log-line ', '').replace('log-', '');
        div.dataset.routine = String(ROUTINE_LINE.test(text));
        div.innerHTML = highlightLine(text);
        console$.appendChild(div);
        lineCount++;
        scheduleFilters();

        if (autoscrollCheck && autoscrollCheck.checked) {
            requestAnimationFrame(() => {
                console$.scrollTop = console$.scrollHeight;
            });
        }
    }

    // ─── Load history then open live stream ──────────────────────────────────
    async function connect(projectId) {
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }

        currentProjectId = projectId;
        lineCount = 0;
        if (console$) console$.innerHTML = '';
        setStatus('loading', '◌ Loading history…');

        // 1. Fetch buffered history
        try {
            const resp = await fetch(`api/projects/${projectId}/logs`);
            if (resp.ok) {
                const data = await resp.json();
                (data.lines || []).forEach(appendLine);
            }
        } catch (e) {
            appendLine(`[LogConsole] Could not load history: ${e.message}`);
        }

        // 2. Open SSE stream for live updates
        setStatus('connecting', '◌ Connecting…');
        const es = new EventSource(`api/projects/${projectId}/logs/stream`);
        eventSource = es;

        es.onopen = () => setStatus(
            projectRunning ? 'live' : 'connected',
            projectRunning ? '● Live updates' : '● Historical log'
        );

        es.onmessage = (e) => {
            const text = e.data;
            if (text === '[PIPELINE ENDED]') {
                appendLine('─────────────────── Pipeline finished ───────────────────');
                setStatus('done', '◼ Run complete');
            } else {
                appendLine(text);
                setStatus(
                    projectRunning ? 'live' : 'connected',
                    projectRunning ? '● Live updates' : '● Historical log'
                );
            }
        };

        es.onerror = () => {
            setStatus('error', '✕ Disconnected');
            es.close();
            eventSource = null;
        };
    }

    function disconnect() {
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
        setStatus('disconnected', '● Disconnected');
    }

    function setStatus(state, label) {
        if (!statusEl) return;
        statusEl.textContent = label;
        statusEl.className = `log-status log-status-${state}`;
        statusEl.dataset.projectRunning = String(projectRunning);
    }

    function applyFilters() {
        if (!console$) return;
        const search = (searchInput?.value || '').trim().toLowerCase();
        const level = levelFilter?.value || 'all';
        const hideRoutine = Boolean(hideNoiseCheck?.checked);
        let hiddenRoutineCount = 0;
        console$.classList.toggle('wrap', Boolean(wrapCheck?.checked));
        console$.querySelectorAll('.log-line').forEach(line => {
            const raw = line.dataset.raw || line.textContent || '';
            const matchesSearch = !search || raw.toLowerCase().includes(search);
            const matchesLevel = level === 'all'
                || line.dataset.level === level
                || (level === 'warning' && line.dataset.level === 'warn');
            const matchesNoise = !hideRoutine || line.dataset.routine !== 'true';
            if (hideRoutine && line.dataset.routine === 'true') hiddenRoutineCount += 1;
            line.hidden = !(matchesSearch && matchesLevel && matchesNoise);
        });
        const summary = document.getElementById('log-noise-summary');
        if (summary) summary.textContent = hiddenRoutineCount
            ? `${hiddenRoutineCount} routine line${hiddenRoutineCount === 1 ? '' : 's'} hidden`
            : '';
    }

    function scheduleFilters() {
        if (filterFrame !== null) return;
        filterFrame = requestAnimationFrame(() => {
            filterFrame = null;
            applyFilters();
        });
    }

    function visibleLogText() {
        return [...console$.querySelectorAll('.log-line:not([hidden])')]
            .map(element => element.dataset.raw || element.textContent)
            .join('\n');
    }

    // ─── Public API ───────────────────────────────────────────────────────────
    function openForProject(projectId, running = null) {
        if (running !== null) projectRunning = Boolean(running);
        if (projectId === currentProjectId && eventSource) return; // already connected
        connect(projectId);
    }

    function closeForProject() {
        disconnect();
        currentProjectId = null;
    }

    function setProjectRunning(running) {
        projectRunning = Boolean(running);
        if (eventSource) {
            setStatus(
                projectRunning ? 'live' : 'connected',
                projectRunning ? '● Live updates' : '● Historical log'
            );
        }
    }

    // ─── Init ─────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        console$       = document.getElementById('log-console');
        statusEl       = document.getElementById('log-status');
        autoscrollCheck = document.getElementById('log-autoscroll');
        clearBtn       = document.getElementById('log-clear');
        copyBtn        = document.getElementById('log-copy');
        searchInput    = document.getElementById('log-search');
        levelFilter    = document.getElementById('log-level-filter');
        hideNoiseCheck = document.getElementById('log-hide-noise');
        wrapCheck      = document.getElementById('log-wrap');
        downloadBtn    = document.getElementById('log-download');

        if (!console$) return;

        // Clear button
        clearBtn?.addEventListener('click', () => {
            console$.innerHTML = '';
            lineCount = 0;
        });

        // Copy button
        copyBtn?.addEventListener('click', async () => {
            const text = visibleLogText();
            try {
                await navigator.clipboard.writeText(text);
                copyBtn.textContent = 'Copied!';
                setTimeout(() => { copyBtn.textContent = 'Copy All'; }, 1500);
            } catch (e) {
                copyBtn.textContent = 'Failed';
                setTimeout(() => { copyBtn.textContent = 'Copy All'; }, 1500);
            }
        });
        [searchInput, levelFilter, hideNoiseCheck, wrapCheck].forEach(control => {
            control?.addEventListener(control === searchInput ? 'input' : 'change', applyFilters);
        });
        downloadBtn?.addEventListener('click', () => {
            const blob = new Blob([visibleLogText()], {type: 'text/plain;charset=utf-8'});
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `${currentProjectId || 'project'}-dashboard-log.txt`;
            link.click();
            URL.revokeObjectURL(url);
        });

        // Auto-scroll: when user scrolls up, uncheck; when they scroll to bottom, re-check
        console$.addEventListener('scroll', () => {
            if (!autoscrollCheck) return;
            const atBottom = console$.scrollHeight - console$.scrollTop - console$.clientHeight < 40;
            autoscrollCheck.checked = atBottom;
        });

        // Hook into the tab click to open/close stream
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'tab-logs') {
                    const pid = window.state?.currentProjectId;
                    if (pid) openForProject(pid);
                } else {
                    // Don't close — keep buffering in background
                }
            });
        });
    });

    return { openForProject, closeForProject, appendLine, setProjectRunning };
})();
