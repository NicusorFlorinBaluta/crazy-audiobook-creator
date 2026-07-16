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
    const MAX_LINES = 500;

    // DOM refs (populated after DOMContentLoaded)
    let console$, statusEl, autoscrollCheck, clearBtn, copyBtn;

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
        // Escape HTML first
        const safe = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

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
        div.innerHTML = highlightLine(text);
        console$.appendChild(div);
        lineCount++;

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
            const resp = await fetch(`/api/projects/${projectId}/logs`);
            if (resp.ok) {
                const data = await resp.json();
                (data.lines || []).forEach(appendLine);
            }
        } catch (e) {
            appendLine(`[LogConsole] Could not load history: ${e.message}`);
        }

        // 2. Open SSE stream for live updates
        setStatus('connecting', '◌ Connecting…');
        const es = new EventSource(`/api/projects/${projectId}/logs/stream`);
        eventSource = es;

        es.onopen = () => setStatus('live', '● Live');

        es.onmessage = (e) => {
            const text = e.data;
            if (text === '[PIPELINE ENDED]') {
                appendLine('─────────────────── Pipeline finished ───────────────────');
                setStatus('done', '◼ Run complete');
            } else {
                appendLine(text);
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
    }

    // ─── Public API ───────────────────────────────────────────────────────────
    function openForProject(projectId) {
        if (projectId === currentProjectId && eventSource) return; // already connected
        connect(projectId);
    }

    function closeForProject() {
        disconnect();
        currentProjectId = null;
    }

    // ─── Init ─────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        console$       = document.getElementById('log-console');
        statusEl       = document.getElementById('log-status');
        autoscrollCheck = document.getElementById('log-autoscroll');
        clearBtn       = document.getElementById('log-clear');
        copyBtn        = document.getElementById('log-copy');

        if (!console$) return;

        // Clear button
        clearBtn?.addEventListener('click', () => {
            console$.innerHTML = '';
            lineCount = 0;
        });

        // Copy button
        copyBtn?.addEventListener('click', async () => {
            const text = [...console$.querySelectorAll('.log-line')]
                .map(el => el.textContent)
                .join('\n');
            try {
                await navigator.clipboard.writeText(text);
                copyBtn.textContent = 'Copied!';
                setTimeout(() => { copyBtn.textContent = 'Copy All'; }, 1500);
            } catch (e) {
                copyBtn.textContent = 'Failed';
                setTimeout(() => { copyBtn.textContent = 'Copy All'; }, 1500);
            }
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

    return { openForProject, closeForProject, appendLine };
})();
