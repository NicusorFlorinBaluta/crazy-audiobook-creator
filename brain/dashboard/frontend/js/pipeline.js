/**
 * Pipeline UI Manager
 * Handles the visual pipeline tracker and controls.
 */

window.PipelineManager = (() => {
    // Pipeline stages in order
    const STAGES = [
        'CREATED',
        'EXTRACTING',
        'SCRIPTING',
        'BOOTSTRAPPING',
        'GENERATING',
        'VALIDATING',
        'MASTERING',
        'EXPORTING',
        'COMPLETED'
    ];

    const els = {
        tracker: document.getElementById('pipeline-tracker'),
        live: document.getElementById('pipeline-live'),
        btnStart: document.getElementById('btn-start-pipeline'),
        btnStop: document.getElementById('btn-stop-pipeline'),
    };

    function init() {
        renderTracker();
    }

    function renderTracker() {
        els.tracker.innerHTML = '';
        STAGES.forEach((stage, idx) => {
            const stageDiv = document.createElement('div');
            stageDiv.className = 'pipeline-stage';
            stageDiv.dataset.stage = stage;
            
            stageDiv.innerHTML = `
                <span class="stage-num">${idx + 1}</span>
                <span class="stage-name">${stage.replace('_', ' ')}</span>
            `;
            
            els.tracker.appendChild(stageDiv);
        });
    }

    function updateTracker(currentStage, status) {
        if (!currentStage) return;
        
        const currentIndex = STAGES.indexOf(currentStage.toUpperCase());
        
        document.querySelectorAll('.pipeline-stage').forEach((el, idx) => {
            el.className = 'pipeline-stage'; // reset
            
            if (idx < currentIndex || status === 'complete') {
                el.classList.add('done');
            } else if (idx === currentIndex) {
                if (status === 'error') {
                    el.classList.add('error');
                } else if (status === 'running' || status === 'paused') {
                    el.classList.add('active');
                }
            }
        });
        
        // Hide live progress if not running
        if (status !== 'running') {
            els.live.classList.remove('active');
        }
    }

    function updateLiveProgress(data) {
        if (!data) return;
        
        els.live.classList.add('active');
        els.live.innerHTML = `
            <div class="live-dot"></div>
            <div class="live-progress">
                <div>${escapeHtml(data.message || 'Processing...')}</div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${data.percent || 100}%"></div>
                </div>
            </div>
            <div>${data.percent ? data.percent.toFixed(1) + '%' : ''}</div>
        `;
    }

    function toggleControls(status, isRunning) {
        // Find DOM elements directly since this is in a separate module scope
        const btnResetStage = document.getElementById('btn-reset-stage');
        const selectResetStage = document.getElementById('select-reset-stage');
        const btnDownloadAudiobook = document.getElementById('btn-download-audiobook');

        if (isRunning) {
            els.btnStart.classList.add('hidden');
            els.btnStop.classList.remove('hidden');
            if (selectResetStage) selectResetStage.classList.add('hidden');
            if (btnResetStage) btnResetStage.classList.add('hidden');
        } else {
            els.btnStart.classList.remove('hidden');
            els.btnStop.classList.add('hidden');
            if (selectResetStage) selectResetStage.classList.remove('hidden');
            
            if (status === 'complete' || status === 'completed') {
                els.btnStart.textContent = '▶ Run Again';
                if (btnDownloadAudiobook) btnDownloadAudiobook.classList.remove('hidden');
            } else if (status === 'error' || status === 'paused') {
                els.btnStart.textContent = '▶ Resume Pipeline';
                if (btnDownloadAudiobook) btnDownloadAudiobook.classList.add('hidden');
            } else {
                els.btnStart.textContent = '▶ Start Pipeline';
                if (btnDownloadAudiobook) btnDownloadAudiobook.classList.add('hidden');
            }
        }
    }
    
    // Expose HTML escaping utility locally
    function escapeHtml(unsafe) {
        if (!unsafe) return '';
        return unsafe.toString()
             .replace(/&/g, "&amp;")
             .replace(/</g, "&lt;")
             .replace(/>/g, "&gt;")
             .replace(/"/g, "&quot;")
             .replace(/'/g, "&#039;");
    }

    // Run init on load
    document.addEventListener('DOMContentLoaded', init);

    return {
        updateTracker,
        updateLiveProgress,
        toggleControls
    };
})();
