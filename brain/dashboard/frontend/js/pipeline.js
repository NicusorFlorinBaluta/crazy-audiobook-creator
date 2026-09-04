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
        'VOICE_REVIEW',
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

    let pipelineDisclosureProject = null;
    let pipelineDisclosureInitialized = false;
    let chapterDetailsMap = new Map();

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
        const selectionComplete = stageUpper === 'SELECTION_COMPLETE'
            || statusLower === 'selection_complete';
        const isFinished = ['COMPLETE', 'COMPLETED'].includes(stageUpper) ||
                           ['complete', 'completed'].includes(statusLower);
        
        let currentIndex = STAGES.indexOf(stageUpper);
        if (stageUpper === 'SELECTION_COMPLETE' || stageUpper === 'COMPLETED') {
            currentIndex = STAGES.length - 1;
        }
        
        document.querySelectorAll('.pipeline-stage').forEach((el, idx) => {
            el.className = 'pipeline-stage'; // reset
            const percentEl = el.querySelector('.stage-percent');
            const nameEl = el.querySelector('.stage-name');
            if (percentEl) percentEl.textContent = '';
            if (nameEl) nameEl.textContent = STAGES[idx].replace('_', ' ');
            
            if (selectionComplete && idx === STAGES.length - 1) {
                el.classList.add('active');
                if (nameEl) nameEl.textContent = 'BATCH COMPLETE';
                if (percentEl) percentEl.textContent = 'PARTIAL';
            } else if (isFinished || idx < currentIndex) {
                el.classList.add('done');
                if (percentEl) percentEl.textContent = '100%';
            } else if (idx === currentIndex) {
                if (status === 'error') {
                    el.classList.add('error');
                } else if (
                    statusLower === 'running'
                    || statusLower === 'paused'
                    || statusLower === 'voice_review'
                    || statusLower === 'waiting_for_review'
                ) {
                    el.classList.add('active');
                    
                    if (data && Array.isArray(data.chapter_details)) {
                        chapterDetailsMap = new Map(data.chapter_details.map(d => [d.number, d.title]));
                    }

                    // Compute percentage based on real metrics from the pipeline state!
                    if (data && percentEl) {
                        let pct = null;
                        const stage = STAGES[idx];
                        const totalCh = data.total_chapters || 0;
                        const selected = data.active_generation_chapter_selection
                            || data.generation_chapter_selection
                            || Array.from({length: totalCh}, (_, index) => index + 1);
                        const selectedSet = new Set(selected);
                        const batchTotal = selectedSet.size || totalCh;
                        const canonicalProgress = data.progress || null;
                        const canonicalStage = String(
                            canonicalProgress?.stage || ''
                        ).toUpperCase();
                        
                        if (
                            canonicalStage === stage
                            && Number.isFinite(canonicalProgress?.percent)
                        ) {
                            pct = canonicalProgress.percent;
                        } else if (stage === 'SCRIPTING' && data.scripted_chapters) {
                            pct = Number.isFinite(data.work_progress?.stagePercent)
                                ? data.work_progress.stagePercent
                                : (
                                    totalCh
                                        ? (data.scripted_chapters.length / totalCh) * 100
                                        : 0
                                );
                        } else if (stage === 'SCRIPTING') {
                            percentEl.innerHTML = '<span class="loading-dots">⏳</span>';
                        } else if (stage === 'BOOTSTRAPPING') {
                            pct = data.bootstrapping_completed ? 100 : null;
                            if (!data.bootstrapping_completed) {
                                percentEl.innerHTML = '<span class="loading-dots">⏳</span>';
                            }
                        } else if (stage === 'VOICE_REVIEW') {
                            pct = 100;
                        } else if (stage === 'GENERATING' && batchTotal > 0) {
                            const genSet = new Set(
                                (data.generated_chapters || []).filter(chapter => selectedSet.has(chapter))
                            );
                            const curCh = data.current_gen_chapter || 1;
                            const curDetail = data.chapter_details ? data.chapter_details.find(d => d.number === curCh) : null;
                            const curPct = curDetail ? (curDetail.progress_percent / 100) : 0;
                            pct = ((genSet.size + curPct) / batchTotal) * 100;
                        } else if (stage === 'VALIDATING' && batchTotal > 0) {
                            const genCount = (data.generated_chapters || [])
                                .filter(chapter => selectedSet.has(chapter)).length;
                            pct = (genCount / batchTotal) * 100;
                        } else if (stage === 'MASTERING' && batchTotal > 0) {
                            const masterCount = (data.mastered_chapters || [])
                                .filter(chapter => selectedSet.has(chapter)).length;
                            pct = (masterCount / batchTotal) * 100;
                        } else if (stage === 'EXPORTING') {
                            pct = null;
                            percentEl.innerHTML = '<span class="loading-dots">⏳</span>';
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
        if (!data || !data.message) {
            els.live.classList.remove('active');
            els.live.innerHTML = '';
            return;
        }

        const percent = Number.isFinite(data.percent) ? data.percent : 0;

        let etaStr = '';
        if (Number.isFinite(data.eta_seconds)) {
            const remainingSec = Math.max(0, Math.round(data.eta_seconds));
            etaStr = remainingSec > 60
                ? ` ~${Math.ceil(remainingSec / 60)} min remaining`
                : ` ~${remainingSec} sec remaining`;
        }

        let displayMessage = data.message || 'Processing...';
        if (data.message) {
            const map = chapterDetailsMap.size > 0
                ? chapterDetailsMap
                : new Map((window.state?.currentProject?.chapter_details || []).map(d => [d.number, d.title]));
            const m = data.message.match(/^(synthesis|validation|scripting|mastering|generating)\s+chapter\s+(\d+):\s*(.*)$/i);
            if (m) {
                const phaseName = m[1].charAt(0).toUpperCase() + m[1].slice(1).toLowerCase();
                const chNum = parseInt(m[2], 10);
                const bookTitle = map.get(chNum) || `Chapter ${chNum}`;
                displayMessage = `${phaseName} — ${bookTitle}: ${m[3]}`;
            } else {
                displayMessage = displayMessage.replace(/\bchapter\s+(\d+)\b/gi, (match, chStr) => {
                    const chNum = parseInt(chStr, 10);
                    return map.get(chNum) || match;
                });
            }
        }

        els.live.classList.add('active');
        els.live.innerHTML = `
            <div class="live-dot"></div>
            <div class="live-progress">
                <div>${escapeHtml(displayMessage)} <span class="eta" style="opacity:0.7;font-size:0.9em">${etaStr}</span></div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${percent}%"></div>
                </div>
            </div>
            <div>${Number.isFinite(data.percent) ? data.percent.toFixed(1) + '%' : ''}</div>
        `;
    }

    function toggleControls(status, isRunning, data = null) {
        const btnResetStage = document.getElementById('btn-reset-stage');
        const selectResetStage = document.getElementById('select-reset-stage');
        const btnDownloadAudiobook = document.getElementById('btn-download-audiobook');
        const pipelineDetails = document.getElementById('pipeline-details');
        const advancedActions = document.getElementById('advanced-actions');

        const statusLower = (status || '').toLowerCase();
        const isDone = ['complete', 'completed', 'selection_complete'].includes(statusLower);
        const hasMastered = data && data.mastered_chapters && data.mastered_chapters.length > 0;

        const projectId = data?.project_id || null;
        if (pipelineDetails && (
            !pipelineDisclosureInitialized
            || projectId !== pipelineDisclosureProject
        )) {
            // Choose a useful default once per project. Subsequent polling must
            // preserve the user's disclosure choice instead of snapping it shut.
            pipelineDetails.open = !isDone || isRunning;
            pipelineDisclosureProject = projectId;
            pipelineDisclosureInitialized = true;
        }
        if (advancedActions && isRunning) advancedActions.open = false;

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

            const total = data?.total_chapters || 0;
            const mastered = new Set(data?.mastered_chapters || []);
            const rawSelection = data?.generation_chapter_selection;
            const selected = (rawSelection && Array.isArray(rawSelection))
                ? rawSelection
                : (total ? Array.from({length: total}, (_, i) => i + 1) : []);
            const hasCustomSelection = rawSelection && Array.isArray(rawSelection) && rawSelection.length < total;
            const unmasteredSelected = selected.filter(ch => !mastered.has(ch));

            if (['error', 'paused', 'paused_scheduled', 'deploy_paused', 'voice_review', 'waiting_for_review'].includes(statusLower)) {
                els.btnStart.textContent = '▶ Resume Pipeline';
                els.btnStart.title = 'A deliberate manual resume can run outside configured working hours for this run only';
            } else if (hasCustomSelection) {
                if (unmasteredSelected.length > 0) {
                    els.btnStart.textContent = '▶ Generate selected chapters';
                    els.btnStart.title = `Generate ${selected.length} selected chapter${selected.length > 1 ? 's' : ''} (${unmasteredSelected.length} unmastered)`;
                } else {
                    els.btnStart.textContent = '▶ Re-generate selected chapters';
                    els.btnStart.title = `Re-generate ${selected.length} already-mastered chapter${selected.length > 1 ? 's' : ''}`;
                }
            } else if (isDone) {
                if (unmasteredSelected.length > 0) {
                    els.btnStart.textContent = '▶ Generate remaining chapters';
                    els.btnStart.title = `Generate remaining ${unmasteredSelected.length} unmastered chapters`;
                } else {
                    els.btnStart.textContent = '▶ Re-generate audiobook';
                    els.btnStart.title = 'Re-generate all chapters of the audiobook';
                }
            } else {
                els.btnStart.textContent = '▶ Start Pipeline';
                els.btnStart.removeAttribute('title');
            }
        }
    }
    
    // Expose HTML escaping utility locally
    // escapeHtml now lives in js/dom-utils.js, which loads before this file.

    // Run init on load
    document.addEventListener('DOMContentLoaded', init);

    return {
        updateTracker,
        updateLiveProgress,
        toggleControls
    };
})();
