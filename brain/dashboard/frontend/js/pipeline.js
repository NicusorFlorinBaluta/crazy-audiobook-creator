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
        btnPause: document.getElementById('btn-pause-pipeline'),
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
                <span class="stage-percent" style="font-size: 0.8em; opacity: 0.7; margin-left: 5px; font-weight: bold;"></span>
            `;
            
            els.tracker.appendChild(stageDiv);
        });
    }

    function updateTracker(currentStage, status, data = null) {
        if (!currentStage) return;
        
        const stageUpper = currentStage.toUpperCase();
        const statusLower = (status || '').toLowerCase();
        const isFinished = ['COMPLETE', 'COMPLETED', 'SELECTION_COMPLETE'].includes(stageUpper) || 
                           ['complete', 'completed', 'selection_complete'].includes(statusLower);
        
        let currentIndex = STAGES.indexOf(stageUpper);
        if (stageUpper === 'SELECTION_COMPLETE' || stageUpper === 'COMPLETED') {
            currentIndex = STAGES.length - 1;
        }
        
        document.querySelectorAll('.pipeline-stage').forEach((el, idx) => {
            el.className = 'pipeline-stage'; // reset
            const percentEl = el.querySelector('.stage-percent');
            if (percentEl) percentEl.textContent = '';
            
            if (isFinished || idx < currentIndex) {
                el.classList.add('done');
                if (percentEl) percentEl.textContent = '100%';
            } else if (idx === currentIndex) {
                if (status === 'error') {
                    el.classList.add('error');
                } else if (status === 'running' || status === 'paused') {
                    el.classList.add('active');
                    
                    // Compute percentage based on real metrics from the pipeline state!
                    if (data && percentEl) {
                        let pct = null;
                        const stage = STAGES[idx];
                        const totalCh = data.total_chapters || 0;
                        const totalLines = data.total_lines || 0;
                        
                        if (stage === 'SCRIPTING' && data.completed_script_chapters) {
                            pct = (data.completed_script_chapters.length / totalCh) * 100;
                        } else if (stage === 'SCRIPTING') {
                            percentEl.innerHTML = '<span class="loading-dots">⏳</span>';
                        } else if (stage === 'BOOTSTRAPPING') {
                            pct = data.bootstrapping_completed ? 100 : 25;
                        } else if (stage === 'GENERATING' && totalCh > 0) {
                            const doneCount = data.completed_gen_chapters ? data.completed_gen_chapters.length : 0;
                            pct = ((doneCount + (doneCount < totalCh ? 0.5 : 0)) / totalCh) * 100;
                        } else if (stage === 'VALIDATING' && totalCh > 0) {
                            const doneCount = data.completed_gen_chapters ? data.completed_gen_chapters.length : 0;
                            pct = ((doneCount + (doneCount < totalCh ? 0.5 : 0)) / totalCh) * 100;
                        } else if (stage === 'MASTERING' && totalCh > 0) {
                            const doneCount = data.completed_master_chapters ? data.completed_master_chapters.length : 0;
                            pct = (doneCount / totalCh) * 100;
                        } else if (stage === 'EXPORTING') {
                            pct = 50; // coarse
                        }
                        
                        if (pct !== null) {
                            percentEl.textContent = Math.min(100, Math.round(pct)) + '%';
                        }
                    }
                }
            }
        });
        
        // Hide live progress if not running
        if (!['running', 'in_progress'].includes(statusLower) || isFinished) {
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

    function toggleControls(status, isRunning, data = null) {
        const btnResetStage = document.getElementById('btn-reset-stage');
        const selectResetStage = document.getElementById('select-reset-stage');
        const btnDownloadAudiobook = document.getElementById('btn-download-audiobook');

        const statusLower = (status || '').toLowerCase();
        const isDone = ['complete', 'completed', 'selection_complete'].includes(statusLower);
        const hasMastered = data && data.mastered_chapters && data.mastered_chapters.length > 0;

        if (btnDownloadAudiobook) {
            if (isDone || hasMastered) {
                btnDownloadAudiobook.classList.remove('hidden');
            } else {
                btnDownloadAudiobook.classList.add('hidden');
            }
        }

        if (isRunning && !isDone) {
            els.btnStart.classList.add('hidden');
            els.btnPause.classList.remove('hidden');
            if (selectResetStage) selectResetStage.classList.add('hidden');
            if (btnResetStage) btnResetStage.classList.add('hidden');
        } else {
            els.btnStart.classList.remove('hidden');
            els.btnPause.classList.add('hidden');
            if (selectResetStage) selectResetStage.classList.remove('hidden');
            
            if (isDone) {
                els.btnStart.textContent = '▶ Run Again / Selection';
            } else if (statusLower === 'error' || statusLower === 'paused') {
                els.btnStart.textContent = '▶ Resume Pipeline';
            } else {
                els.btnStart.textContent = '▶ Start Pipeline';
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
