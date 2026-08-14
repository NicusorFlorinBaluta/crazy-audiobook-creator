/**
 * Main Application Logic — Crazy Audiobook Creator
 * Handles navigation, project CRUD, and global state.
 */

// Global State
const state = window.state = {
    projects: [],
    currentProjectId: null,
    currentProject: null,
    ws: null,
    voiceServerOnline: false,
    schedule: null,
    lastScheduleRefresh: 0,
    lastModalTrigger: null,
    metadataCandidate: null
};

const attentionState = {data: null, page: 1, pageSize: 10, projectId: null};

// DOM Elements
const els = {
    viewProjects: document.getElementById('view-projects'),
    viewDetail: document.getElementById('view-detail'),
    projectsGrid: document.getElementById('projects-grid'),
    projectsEmpty: document.getElementById('projects-empty'),
    projectSearch: document.getElementById('project-search'),
    projectStatusFilter: document.getElementById('project-status-filter'),
    projectSort: document.getElementById('project-sort'),
    btnNewProject: document.getElementById('btn-new-project'),
    btnEmptyNew: document.getElementById('btn-empty-new'),
    btnBack: document.getElementById('btn-back'),
    btnResetStage: document.getElementById('btn-reset-stage'),
    selectResetStage: document.getElementById('select-reset-stage'),
    btnDownloadAudiobook: document.getElementById('btn-download-audiobook'),
    uploadModal: document.getElementById('upload-modal'),
    modalClose: document.getElementById('modal-close'),
    modalCancel: document.getElementById('modal-cancel'),
    uploadZone: document.getElementById('upload-zone'),
    epubInput: document.getElementById('epub-file-input'),
    uploadEnableIncremental: document.getElementById('upload-enable-incremental'),
    uploadIncrementalOptions: document.getElementById('upload-incremental-options'),
    uploadDeliveryBatchSize: document.getElementById('upload-delivery-batch-size'),
    uploadInfo: document.getElementById('upload-info'),
    uploadFileName: document.getElementById('upload-file-name'),
    uploadFileSize: document.getElementById('upload-file-size'),
    uploadRemove: document.getElementById('upload-remove'),
    btnUpload: document.getElementById('modal-upload'),
    uploadProgress: document.getElementById('upload-progress'),
    uploadProgressFill: document.getElementById('upload-progress-fill'),
    uploadProgressText: document.getElementById('upload-progress-text'),
    metadataModal: document.getElementById('metadata-modal'),
    metadataModalClose: document.getElementById('metadata-modal-close'),
    metadataCancel: document.getElementById('metadata-cancel'),
    metadataApply: document.getElementById('metadata-apply'),
    toastContainer: document.getElementById('toast-container'),
    voiceStatusDot: document.getElementById('voice-status-dot'),
    voiceStatusText: document.getElementById('voice-status-text')
};

function usesEmbeddedMobileWebView() {
    return window.self !== window.top && (
        window.matchMedia('(pointer: coarse)').matches || window.innerWidth <= 768
    );
}

async function copyDownloadFallback(url) {
    const absoluteUrl = new URL(url, document.baseURI).href;
    try {
        await navigator.clipboard.writeText(absoluteUrl);
        showToast('The mobile app blocked the download window. Link copied; open it in your browser.', 'warning');
    } catch (_) {
        showToast('The mobile app blocked the download window. Open this dashboard in your browser to download.', 'warning');
    }
}

function startServerDownload(url) {
    if (!usesEmbeddedMobileWebView()) {
        window.location.href = url;
        return;
    }

    const opened = window.open(url, '_blank');
    if (opened) {
        try {
            opened.opener = null;
        } catch (_) {
            // Cross-origin mobile WebViews may make the returned handle opaque.
        }
        showToast('Download opened outside the embedded Home Assistant page.', 'info');
    } else {
        void copyDownloadFallback(url);
    }
}

// ============================================================================
// Initialization
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    initApp();
    setupEventListeners();
    connectWebSocket();
    // Simulate checking voice server initially
    checkVoiceServerStatus();
    setInterval(checkVoiceServerStatus, 30000); // Check every 30s
});

async function initApp() {
    await Promise.all([fetchProjects(), loadSchedule()]);
    handleHash();
    window.addEventListener('hashchange', handleHash);
}

function handleHash() {
    const hash = window.location.hash.substring(1);
    const savedProjectId = localStorage.getItem('activeProjectId');
    if (hash && hash.startsWith('project/')) {
        const projectId = hash.replace('project/', '');
        showDetailView(projectId, true);
    } else if (savedProjectId && !hash) {
        showDetailView(savedProjectId, true);
    } else {
        showProjectsView(true);
    }
}

function setupEventListeners() {
    // Navigation
    els.btnNewProject.addEventListener('click', openUploadModal);
    els.btnEmptyNew.addEventListener('click', openUploadModal);
    els.btnBack.addEventListener('click', showProjectsView);
    document.getElementById('nav-home-btn').addEventListener('click', showProjectsView);
    els.projectSearch?.addEventListener('input', renderProjectsList);
    els.projectStatusFilter?.addEventListener('change', renderProjectsList);
    els.projectSort?.addEventListener('change', renderProjectsList);
    document.addEventListener('click', event => {
        const downloadLink = event.target.closest('a[data-server-download]');
        if (!downloadLink || !usesEmbeddedMobileWebView()) return;
        event.preventDefault();
        startServerDownload(downloadLink.href);
    });
    
    const STAGE_TOOLTIPS = {
        extracting: 'Rebuild book text from the preserved source EPUB, then clear scripts, cast, segments, and mastered audio. Older projects without source.epub are left unchanged.',
        scripting: 'Re-run LLM script & character extraction (clears script JSONs and cast, keeps EPUB text)',
        bootstrapping: 'Re-generate voice design profiles and reference audio (keeps script intact)',
        voice_review: 'Re-open the Voice Review banner to change speaking voices or tweak cast without re-scripting',
        generating: 'Re-generate chapter audio segments (clears audio segments, keeps script and voice cast)',
        validating: 'Keep synthesized segment WAVs, invalidate validation results, and re-run Whisper, speaker, and audio checks. Mastered audio is cleared.',
        mastering: 'Keep accepted segments, clear mastered chapter audio and M4B, then re-run chapter mastering.',
        exporting: 'Keep mastered chapter audio and remove only the M4B package so export can run again.'
    };

    // Reset and Download features
    els.selectResetStage.addEventListener('change', () => {
        const val = els.selectResetStage.value;
        if (val) {
            els.btnResetStage.classList.remove('hidden');
            const desc = STAGE_TOOLTIPS[val] || '';
            document.getElementById('reset-stage-help').textContent = desc;
        }
    });
    
    els.btnResetStage.addEventListener('click', async () => {
        const stage = els.selectResetStage.value;
        if (!stage || !state.currentProjectId) return;

        const desc = STAGE_TOOLTIPS[stage] || '';
        const confirmed = confirm(`Are you sure you want to reset project '${state.currentProjectId}' to stage '${stage}'?\n\nEffect: ${desc}`);
        if (!confirmed) return;
        
        try {
            const resp = await fetch(`api/projects/${state.currentProjectId}/reset`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stage })
            });
            if (!resp.ok) {
                const data = await resp.json();
                throw new Error(data.detail || 'Failed to reset pipeline');
            }
            showToast(`Project reset to ${stage}`, 'success');
            els.selectResetStage.value = '';
            els.btnResetStage.classList.add('hidden');
            fetchProjectDetails(state.currentProjectId);
        } catch (e) {
            showToast(e.message, 'error');
        }
    });
    
    els.btnDownloadAudiobook.addEventListener('click', () => {
        if (!state.currentProjectId) return;
        startServerDownload(
            `api/projects/${encodeURIComponent(state.currentProjectId)}/download`
        );
    });

    // Modal
    els.modalClose.addEventListener('click', closeUploadModal);
    els.modalCancel.addEventListener('click', closeUploadModal);
    els.uploadModal.addEventListener('click', event => {
        if (event.target === els.uploadModal) closeUploadModal();
    });
    document.addEventListener('keydown', handleGlobalKeydown);
    els.metadataModalClose.addEventListener('click', closeMetadataModal);
    els.metadataCancel.addEventListener('click', closeMetadataModal);
    els.metadataApply.addEventListener('click', applyMetadataCandidate);
    document.getElementById('metadata-search-form')?.addEventListener(
        'submit', performManualMetadataSearch
    );
    document.getElementById('metadata-manual-search')?.addEventListener(
        'toggle', event => {
            if (event.target.open) populateMetadataSearchInputs();
        }
    );
    document.getElementById('metadata-search-results')?.addEventListener('click', event => {
        const result = event.target.closest('[data-metadata-volume-id]');
        if (result) selectManualMetadataCandidate(result.dataset.metadataVolumeId);
    });
    els.metadataModal.addEventListener('click', event => {
        if (event.target === els.metadataModal) closeMetadataModal();
    });
    
    // Drag and Drop Upload
    els.uploadZone.addEventListener('click', () => els.epubInput.click());
    els.uploadZone.addEventListener('dragover', handleDragOver);
    els.uploadZone.addEventListener('dragleave', handleDragLeave);
    els.uploadZone.addEventListener('drop', handleDrop);
    els.epubInput.addEventListener('change', handleFileSelect);
    els.uploadEnableIncremental?.addEventListener('change', event => {
        if (els.uploadIncrementalOptions) {
            els.uploadIncrementalOptions.style.display = event.target.checked
                ? 'flex'
                : 'none';
        }
    });
    els.uploadRemove.addEventListener('click', clearUpload);
    els.btnUpload.addEventListener('click', handleUploadSubmit);

    // Tabs
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => activateDetailTab(tab.dataset.tab, true));
        tab.addEventListener('keydown', handleTabKeydown);
    });

    // Detail Actions
    document.getElementById('btn-start-pipeline').addEventListener('click', startPipeline);
    document.getElementById('btn-pause-pipeline').addEventListener('click', pausePipeline);
    document.getElementById('btn-delete-project').addEventListener('click', deleteProject);
    ['attention-type', 'attention-status', 'attention-confidence'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', () => {
            attentionState.page = 1;
            renderAttentionInbox();
        });
    });
    document.getElementById('attention-prev')?.addEventListener('click', () => {
        attentionState.page = Math.max(1, attentionState.page - 1);
        renderAttentionInbox();
    });
    document.getElementById('attention-next')?.addEventListener('click', () => {
        attentionState.page += 1;
        renderAttentionInbox();
    });
    document.getElementById('btn-resume-after-review')?.addEventListener('click', startPipeline);

    // Feature Expansion Handlers
    const btnFetchMeta = document.getElementById('btn-fetch-metadata');
    if (btnFetchMeta) {
        btnFetchMeta.addEventListener('click', previewMetadataCandidate);
    }

    const btnReqDeploy = document.getElementById('btn-request-deploy');
    if (btnReqDeploy) {
        btnReqDeploy.addEventListener('click', async () => {
            if (!state.currentProjectId) return;
            try {
                const res = await fetch(`api/projects/${state.currentProjectId}/request-deploy`, { method: 'POST' });
                if (!res.ok) throw new Error('Failed to request deployment pause');
                showToast('Deployment pause requested — will park at next chapter', 'warning');
                fetchProjectDetails(state.currentProjectId);
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    }

    const btnResDeploy = document.getElementById('btn-resume-deploy');
    if (btnResDeploy) {
        btnResDeploy.addEventListener('click', async () => {
            if (!state.currentProjectId) return;
            try {
                const res = await fetch(`api/projects/${state.currentProjectId}/resume-deploy`, { method: 'POST' });
                if (!res.ok) throw new Error('Failed to resume deployment');
                showToast('Resuming pipeline from deploy pause...', 'success');
                fetchProjectDetails(state.currentProjectId);
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    }

    // Chapter Selection Toolbar
    const btnSelAll = document.getElementById('btn-select-all-chapters');
    if (btnSelAll) {
        btnSelAll.addEventListener('click', () => {
            document.querySelectorAll('.chapter-select-cb').forEach(cb => cb.checked = true);
            updateChapterSelectionState();
        });
    }

    const btnSelNone = document.getElementById('btn-select-none-chapters');
    if (btnSelNone) {
        btnSelNone.addEventListener('click', () => {
            document.querySelectorAll('.chapter-select-cb').forEach(cb => cb.checked = false);
            updateChapterSelectionState();
        });
    }

    const btnApplyRange = document.getElementById('btn-apply-range');
    if (btnApplyRange) {
        btnApplyRange.addEventListener('click', () => {
            const input = document.getElementById('chapter-range-input').value.trim();
            const chapters = parseChapterRange(input);
            if (!chapters) {
                showToast('Use a range such as 1-5, 8, 12-14', 'warning');
                return;
            }
            document.querySelectorAll('.chapter-select-cb').forEach(cb => {
                const ch = parseInt(cb.dataset.ch, 10);
                cb.checked = chapters.has(ch);
            });
            updateChapterSelectionState();
        });
    }

    document.getElementById('chapter-search-input')?.addEventListener('input', () => {
        chapterPaginationState.currentPage = 1;
        filterChapterRows();
    });
    document.getElementById('chapter-status-filter')?.addEventListener('change', () => {
        chapterPaginationState.currentPage = 1;
        filterChapterRows();
    });

    // Chapter Pagination Controls
    document.getElementById('select-page-size')?.addEventListener('change', (e) => {
        chapterPaginationState.pageSize = e.target.value;
        chapterPaginationState.currentPage = 1;
        filterChapterRows();
    });
    document.getElementById('btn-prev-page')?.addEventListener('click', () => {
        if (chapterPaginationState.currentPage > 1) {
            chapterPaginationState.currentPage--;
            filterChapterRows();
        }
    });
    document.getElementById('btn-next-page')?.addEventListener('click', () => {
        chapterPaginationState.currentPage++;
        filterChapterRows();
    });

    document.getElementById('btn-add-schedule-window')?.addEventListener('click', () => {
        addScheduleWindow({
            days: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
            start: '09:00',
            end: '17:00'
        });
    });
    document.getElementById('btn-save-schedule')?.addEventListener('click', saveSchedule);
    document.getElementById('operations-section')?.addEventListener('toggle', event => {
        if (event.target.open) loadOperations();
    });
    document.getElementById('btn-refresh-operations')?.addEventListener('click', loadOperations);
    document.getElementById('btn-restart-dashboard')?.addEventListener('click', restartDashboard);
    document.getElementById('btn-download-support')?.addEventListener('click', () => {
        if (state.currentProjectId) {
            startServerDownload(
                `api/projects/${encodeURIComponent(state.currentProjectId)}/support-bundle`
            );
        }
    });
}

function activateDetailTab(targetId, remember = false) {
    const target = document.querySelector(`.tab[data-tab="${targetId}"]`);
    if (!target) return;
    document.querySelectorAll('.tab').forEach(tab => {
        const active = tab === target;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('.tab-content').forEach(content => {
        const active = content.id === targetId;
        content.classList.toggle('active', active);
        content.hidden = !active;
    });
    if (remember) localStorage.setItem('projectDetailTab', targetId);
}

function handleTabKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('.tab')];
    const current = tabs.indexOf(event.currentTarget);
    let next = current;
    if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    tabs[next].focus();
    activateDetailTab(tabs[next].dataset.tab, true);
}

function handleGlobalKeydown(event) {
    const activeModal = [els.uploadModal, els.metadataModal].find(
        modal => modal && !modal.classList.contains('hidden')
    );
    if (!activeModal) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        if (activeModal === els.uploadModal) closeUploadModal();
        else closeMetadataModal();
        return;
    }
    if (event.key !== 'Tab') return;
    const focusable = [...activeModal.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter(element => !element.hidden && element.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
}

// ============================================================================
// Navigation
// ============================================================================

let detailPollTimer = null;

function showProjectsView(isHashLoad = false) {
    localStorage.removeItem('activeProjectId');
    if (!isHashLoad) {
        window.history.pushState(null, '', '#');
    }
    if (detailPollTimer) {
        clearInterval(detailPollTimer);
        detailPollTimer = null;
    }
    state.currentProjectId = null;
    els.viewDetail.classList.add('hidden');
    els.viewProjects.classList.remove('hidden');
    fetchProjects();
}

async function showDetailView(projectId, isHashLoad = false) {
    if (projectId) {
        localStorage.setItem('activeProjectId', projectId);
    }
    if (!isHashLoad) {
        window.history.pushState(null, '', `#project/${projectId}`);
    }
    state.currentProjectId = projectId;
    els.viewProjects.classList.add('hidden');
    els.viewDetail.classList.remove('hidden');
    
    const project = await fetchProjectDetails(projectId);
    const rememberedTab = localStorage.getItem('projectDetailTab');
    const defaultTab = ['complete', 'completed', 'selection_complete'].includes(
        String(project?.status || '').toLowerCase()
    ) ? 'tab-quality' : 'tab-characters';
    activateDetailTab(
        document.querySelector(`.tab[data-tab="${rememberedTab}"]`)
            ? rememberedTab
            : defaultTab
    );

    if (detailPollTimer) clearInterval(detailPollTimer);
    detailPollTimer = setInterval(() => {
        if (state.currentProjectId) {
            fetchProjectDetails(state.currentProjectId, true);
        }
    }, 2000);

    // Connect log console in background (non-blocking)
    if (window.LogConsole) {
        const terminal = ['complete', 'completed', 'selection_complete', 'paused', 'error', 'waiting_for_review'];
        const running = project?.running === true
            && !terminal.includes(String(project?.status || '').toLowerCase());
        window.LogConsole.openForProject(projectId, running);
    }
}

// ============================================================================
// API Calls & Data Fetching
// ============================================================================

async function fetchProjects() {
    try {
        const response = await fetch('api/projects');
        if (!response.ok) throw new Error('Failed to fetch projects');
        
        const projectsObj = await response.json();
        // Convert dict to array and sort by created_at descending
        state.projects = Object.values(projectsObj).sort((a, b) => {
            return new Date(b.created_at) - new Date(a.created_at);
        });
        
        renderProjectsList();
    } catch (error) {
        showToast(`Error loading projects: ${error.message}`, 'error');
        console.error(error);
    }
}

async function fetchProjectDetails(projectId, isPoll = false) {
    try {
        const [response, logsResponse] = await Promise.all([
            fetch(`api/projects/${projectId}/status`),
            fetch(`api/projects/${projectId}/logs?limit=160`).catch(() => null)
        ]);
        if (!response.ok) throw new Error('Failed to fetch project details');
        
        const data = await response.json();
        state.currentProject = data;
        if (logsResponse?.ok) {
            const logData = await logsResponse.json();
            data.work_progress = deriveWorkProgress(
                logData.lines || [],
                data.total_chapters || 0
            );
        }
        renderProjectDetails(data);
        if (!isPoll || Date.now() - (state.lastAttentionRefresh || 0) > 5000) {
            fetchAndRenderAttention(projectId, data);
        }
        fetchAndRenderDeliveries(projectId);

        const scheduleEditor = document.getElementById('schedule-section');
        if (
            Date.now() - state.lastScheduleRefresh > 30000
            && !scheduleEditor?.open
        ) {
            loadSchedule();
        }
        
        // Let pipeline.js and script-viewer.js update their parts
        if (window.PipelineManager) {
            const stage = (data.status || '').toLowerCase();
            const activeStages = ['extracting', 'scripting', 'bootstrapping', 'generating', 'validating', 'mastering', 'exporting', 'pausing', 'paused_scheduled', 'deploy_paused'];
            const isDoneStage = ['complete', 'completed', 'selection_complete', 'paused', 'error'].includes(stage);
            const isRunning = (data.running === true || activeStages.includes(stage)) && !isDoneStage;
            const coarseStatus = isRunning ? 'running' : stage;
            window.LogConsole?.setProjectRunning(isRunning);
            
            window.PipelineManager.updateTracker(data.active_stage || data.status, isRunning ? 'running' : coarseStatus, data);
            window.PipelineManager.toggleControls(data.status, isRunning, data);
        }
        
        if (window.ScriptViewer && !isPoll) {
            window.ScriptViewer.loadData(projectId);
        }
        return data;
        
    } catch (error) {
        if (!isPoll) {
            showToast(`Error loading project details: ${error.message}`, 'error');
            showProjectsView();
        }
        return null;
    }
}

// ============================================================================
// Upload Modal & Logic
// ============================================================================

let currentFile = null;

function openUploadModal() {
    state.lastModalTrigger = document.activeElement;
    clearUpload();
    els.uploadModal.classList.remove('hidden');
    requestAnimationFrame(() => els.uploadZone.focus());
}

function closeUploadModal() {
    els.uploadModal.classList.add('hidden');
    clearUpload();
    if (state.lastModalTrigger?.focus) state.lastModalTrigger.focus();
    state.lastModalTrigger = null;
}

function handleDragOver(e) {
    e.preventDefault();
    els.uploadZone.classList.add('dragover');
}

function handleDragLeave(e) {
    e.preventDefault();
    els.uploadZone.classList.remove('dragover');
}

function handleDrop(e) {
    e.preventDefault();
    els.uploadZone.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
        handleFile(e.dataTransfer.files[0]);
    }
}

function handleFileSelect(e) {
    if (e.target.files.length) {
        handleFile(e.target.files[0]);
    }
}

function handleFile(file) {
    if (!file.name.toLowerCase().endsWith('.epub')) {
        showToast('Please upload an EPUB file', 'error');
        return;
    }
    
    currentFile = file;
    els.uploadZone.classList.add('hidden');
    els.uploadInfo.classList.remove('hidden');
    els.uploadFileName.textContent = file.name;
    els.uploadFileSize.textContent = formatBytes(file.size);
    els.btnUpload.disabled = false;
}

function clearUpload() {
    currentFile = null;
    els.epubInput.value = '';
    els.uploadZone.classList.remove('hidden');
    els.uploadInfo.classList.add('hidden');
    els.uploadProgress.classList.add('hidden');
    els.btnUpload.disabled = true;
    els.uploadProgressFill.style.width = '0%';
}

async function handleUploadSubmit() {
    if (!currentFile) return;
    
    els.btnUpload.disabled = true;
    els.uploadRemove.disabled = true;
    els.uploadProgress.classList.remove('hidden');
    els.uploadProgressText.textContent = 'Uploading and extracting...';
    
    const formData = new FormData();
    formData.append('file', currentFile);
    // You could also add title/author inputs to the modal and append them here

    try {
        const response = await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', 'api/projects');
            xhr.responseType = 'json';
            xhr.upload.addEventListener('progress', event => {
                if (!event.lengthComputable) return;
                const percent = Math.round((event.loaded / event.total) * 100);
                els.uploadProgressFill.style.width = `${percent}%`;
                els.uploadProgressText.textContent = `Uploading… ${percent}%`;
            });
            xhr.upload.addEventListener('load', () => {
                els.uploadProgressFill.style.width = '100%';
                els.uploadProgressText.textContent = 'Extracting EPUB…';
            });
            xhr.addEventListener('load', () => resolve({
                ok: xhr.status >= 200 && xhr.status < 300,
                json: async () => xhr.response || {},
            }));
            xhr.addEventListener('error', () => reject(new Error('Upload connection failed')));
            xhr.addEventListener('abort', () => reject(new Error('Upload cancelled')));
            xhr.send(formData);
        });

        els.uploadProgressFill.style.width = '100%';
        
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Upload failed');
        }
        
        const data = await response.json();
        const deliveryResponse = await fetch(
            `api/projects/${encodeURIComponent(data.project_id)}/delivery-settings`,
            {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    enabled: Boolean(els.uploadEnableIncremental?.checked),
                    batch_size: Math.max(1, Math.min(20, Number(els.uploadDeliveryBatchSize?.value) || 5))
                })
            }
        );
        const deliveryData = await deliveryResponse.json().catch(() => ({}));
        if (!deliveryResponse.ok) {
            showToast(
                `Project created, but delivery settings were not saved: ${deliveryData.detail || 'unknown error'}`,
                'warning'
            );
        } else {
            showToast('Project created successfully', 'success');
        }
        closeUploadModal();
        await showDetailView(data.project_id);
        
    } catch (error) {
        showToast(error.message, 'error');
        els.btnUpload.disabled = false;
        els.uploadRemove.disabled = false;
        els.uploadProgressText.textContent = 'Upload failed';
        els.uploadProgressFill.style.background = 'var(--danger)';
    }
}

// ============================================================================
// Pipeline Control
// ============================================================================

async function startPipeline() {
    if (!state.currentProjectId) return;
    const chapterCheckboxes = [...document.querySelectorAll('.chapter-select-cb')];
    if (chapterCheckboxes.length && !chapterCheckboxes.some(cb => cb.checked)) {
        showToast('Select at least one chapter before starting', 'warning');
        return;
    }
    
    try {
        if (_selectionDebounceTimer) {
            clearTimeout(_selectionDebounceTimer);
            _selectionDebounceTimer = null;
        }
        if (chapterCheckboxes.length) {
            const selected = chapterCheckboxes
                .filter(cb => cb.checked)
                .map(cb => parseInt(cb.dataset.ch, 10));
            const selectionValue = selected.length === chapterCheckboxes.length
                ? null
                : selected;
            await saveChapterSelection(state.currentProjectId, selectionValue);
        }
        if (window.PipelineManager) {
            window.PipelineManager.toggleControls('generating', true);
        }
        const response = await fetch(`api/projects/${state.currentProjectId}/start`, { method: 'POST' });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to start pipeline');
        }
        showToast('Pipeline started', 'info');
        setTimeout(() => fetchProjectDetails(state.currentProjectId), 500);
    } catch (error) {
        showToast(error.message, 'error');
        if (state.currentProjectId) fetchProjectDetails(state.currentProjectId);
    }
}

async function pausePipeline() {
    if (!state.currentProjectId) return;
    
    try {
        const response = await fetch(`api/projects/${state.currentProjectId}/stop`, { method: 'POST' });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to pause pipeline');
        }
        showToast('Pipeline pausing...', 'info');
        // The pipeline thread might take a moment to gracefully stop.
        // Wait briefly before refreshing to ensure the UI reflects the PAUSED state.
        setTimeout(() => fetchProjectDetails(state.currentProjectId), 1000);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function deleteProject() {
    if (!state.currentProjectId) return;
    
    if (!confirm('Are you sure you want to delete this project? This cannot be undone.')) {
        return;
    }
    
    try {
        const response = await fetch(`api/projects/${state.currentProjectId}`, { method: 'DELETE' });
        if (!response.ok) throw new Error('Failed to delete project');
        
        showToast('Project deleted', 'success');
        showProjectsView();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

// ============================================================================
// UI Rendering
// ============================================================================

function renderProjectsList() {
    if (state.projects.length === 0) {
        els.projectsEmpty.classList.remove('hidden');
        els.projectsGrid.classList.add('hidden');
        return;
    }

    els.projectsEmpty.classList.add('hidden');
    els.projectsGrid.classList.remove('hidden');
    
    els.projectsGrid.innerHTML = '';

    const search = (els.projectSearch?.value || '').trim().toLowerCase();
    const statusFilter = els.projectStatusFilter?.value || 'all';
    const sort = els.projectSort?.value || 'newest';
    const terminal = new Set(['complete', 'completed', 'paused', 'error', 'selection_complete', 'waiting_for_review']);
    const projects = state.projects.filter(project => {
        const status = String(project.status || 'created').toLowerCase();
        const searchText = `${project.title || ''} ${project.author || ''} ${project.project_id || ''}`.toLowerCase();
        const statusMatch = statusFilter === 'all'
            || status === statusFilter
            || (statusFilter === 'complete' && ['complete', 'completed', 'selection_complete'].includes(status))
            || (statusFilter === 'active' && !terminal.has(status));
        return (!search || searchText.includes(search)) && statusMatch;
    }).sort((left, right) => {
        if (sort === 'oldest') return new Date(left.created_at) - new Date(right.created_at);
        if (sort === 'title') return String(left.title || '').localeCompare(String(right.title || ''));
        return new Date(right.created_at) - new Date(left.created_at);
    });

    if (!projects.length) {
        els.projectsGrid.innerHTML = '<div class="empty-state small project-filter-empty"><p>No projects match these filters.</p></div>';
        return;
    }

    projects.forEach(project => {
        const status = String(project.status || 'created').toLowerCase();
        const statusToken = status.replace(/[^a-z_]/g, '');
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'project-card';
        card.setAttribute(
            'aria-label',
            `Open ${project.title && project.title !== 'Unknown' ? project.title : 'untitled project'}, ${formatProjectStatus(status)}`
        );
        card.innerHTML = `
            <div class="card-header">
                <div class="card-emoji">📖</div>
                <div>
                    <h3 class="card-title">${escapeHtml(project.title && project.title !== 'Unknown' ? project.title : 'Untitled')}</h3>
                    <div class="card-author">${escapeHtml(project.author && project.author !== 'Unknown' ? project.author : 'Unknown Author')}</div>
                </div>
            </div>
            <div class="card-stats">
                <div class="card-stat">
                    <span class="card-stat-value">${project.total_chapters || 0}</span> chs
                </div>
                <div class="card-stat">
                    <span class="card-stat-value">${formatDate(project.created_at)}</span>
                </div>
            </div>
            <div class="card-project-id">${escapeHtml(project.project_id || '')}</div>
            ${project.attention_count ? `<div class="attention-card-badge">${project.blocking_review_count || 0} blocking · ${project.attention_count} total</div>` : ''}
            <div class="card-stage" style="background: var(--stage-${statusToken}-bg, var(--bg-elevated)); color: var(--stage-${statusToken}, var(--text-primary))">
                ${['error', 'paused', 'complete'].includes(status) ? (status === 'complete' ? '✅ ' : '⚠️ ') : '⏳ '}
                ${escapeHtml(formatProjectStatus(status))}
            </div>
        `;
        
        card.addEventListener('click', () => showDetailView(project.project_id));
        els.projectsGrid.appendChild(card);
    });
}

function confidenceBand(value) {
    if (value == null) return {key: 'unknown', label: 'No confidence'};
    if (value >= 0.9) return {key: 'high', label: `High · ${Math.round(value * 100)}%`};
    if (value >= 0.75) return {key: 'review', label: `Review · ${Math.round(value * 100)}%`};
    return {key: 'low', label: `Low · ${Math.round(value * 100)}%`};
}

async function fetchAndRenderAttention(projectId, project = state.currentProject) {
    try {
        const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/reviews`);
        if (!response.ok || state.currentProjectId !== projectId) return;
        const newProject = attentionState.projectId !== projectId;
        attentionState.data = await response.json();
        attentionState.projectId = projectId;
        if (newProject) {
            document.getElementById('attention-status').value =
                attentionState.data.blocking_count ? 'blocking' : 'all';
        }
        state.lastAttentionRefresh = Date.now();
        renderAttentionInbox(project);
    } catch (error) {
        console.warn('Could not refresh attention inbox', error);
    }
}

function renderAttentionInbox(project = state.currentProject) {
    const panel = document.getElementById('attention-panel');
    const data = attentionState.data;
    if (!panel || !data || (!data.total_count && project?.status !== 'waiting_for_review')) {
        panel?.classList.add('hidden');
        return;
    }
    panel.classList.remove('hidden');
    document.getElementById('attention-summary').textContent = data.blocking_count
        ? `${data.blocking_count} blocking decision${data.blocking_count === 1 ? '' : 's'}; the pipeline resumes automatically after the last one.`
        : `${data.total_count} non-blocking or resolved item${data.total_count === 1 ? '' : 's'}.`;
    const resume = document.getElementById('btn-resume-after-review');
    resume.classList.toggle('hidden', project?.status !== 'waiting_for_review' || data.blocking_count !== 0);
    const type = document.getElementById('attention-type').value;
    const status = document.getElementById('attention-status').value;
    const confidence = document.getElementById('attention-confidence').value;
    const filtered = data.items.filter(item => {
        const band = confidenceBand(item.confidence).key;
        return (type === 'all' || item.category === type)
            && (status === 'all' || (status === 'blocking' ? item.blocking : !item.blocking))
            && (confidence === 'all' || band === confidence);
    });
    const pages = Math.max(1, Math.ceil(filtered.length / attentionState.pageSize));
    attentionState.page = Math.min(attentionState.page, pages);
    const rows = filtered.slice((attentionState.page - 1) * attentionState.pageSize, attentionState.page * attentionState.pageSize);
    document.getElementById('attention-list').innerHTML = rows.length ? rows.map(item => {
        const band = confidenceBand(item.confidence);
        const trail = item.details?.decision_trail || [];
        return `<article class="attention-item ${item.blocking ? 'blocking' : ''}" data-line-id="${escapeHtml(item.item_id)}">
            <div class="attention-item-head"><strong>${escapeHtml(item.title)}</strong><span class="confidence-band confidence-${band.key}">${escapeHtml(band.label)}</span></div>
            <p>${escapeHtml(item.reason || '')}</p>
            <div class="attention-item-actions">
                ${item.category === 'audio' ? `<audio controls preload="none" src="${escapeHtml(item.details?.audio_url || '')}"></audio><button class="btn btn-ghost btn-sm load-candidates">Compare attempts</button>` : ''}
                ${item.category === 'attribution' ? `<button class="btn btn-ghost btn-sm reveal-context">Reveal in script editor</button>` : ''}
            </div>
            <div class="candidate-comparison hidden"></div>
            ${trail.length ? `<details class="decision-trail"><summary>Decision trail (${trail.length})</summary>${trail.map(step => `<div><strong>${escapeHtml(step.provider || step.resolver || 'validator')}</strong> · ${escapeHtml(step.decision || 'unknown')} · ${step.confidence == null ? 'n/a' : `${Math.round(step.confidence * 100)}%`}<br><small>${escapeHtml(step.reason || '')}</small></div>`).join('')}</details>` : ''}
        </article>`;
    }).join('') : '<div class="review-complete-message">No items match these filters.</div>';
    document.getElementById('attention-page').textContent = `Page ${attentionState.page} of ${pages} · ${filtered.length} items`;
    document.getElementById('attention-prev').disabled = attentionState.page <= 1;
    document.getElementById('attention-next').disabled = attentionState.page >= pages;
    panel.querySelectorAll('.load-candidates').forEach(button => button.addEventListener('click', async () => {
        const row = button.closest('.attention-item');
        const target = row.querySelector('.candidate-comparison');
        button.disabled = true;
        const response = await fetch(`api/projects/${encodeURIComponent(attentionState.projectId)}/segments/${encodeURIComponent(row.dataset.lineId)}/candidates`);
        const payload = response.ok ? await response.json() : {candidates: []};
        target.innerHTML = payload.candidates.length ? payload.candidates.map((candidate, index) => `<div><strong>${index === 0 ? 'Recommended' : 'Alternative'} · score ${Number(candidate.score).toFixed(1)}</strong><audio controls preload="metadata" src="${escapeHtml(candidate.audio_url)}"></audio><details><summary>Metrics and rationale</summary><pre>${escapeHtml(JSON.stringify(candidate.quality, null, 2))}</pre></details></div>`).join('') : '<small>No retained alternative is available yet.</small>';
        target.classList.remove('hidden');
        button.remove();
    }));
    panel.querySelectorAll('.reveal-context').forEach(button => button.addEventListener('click', () => {
        activateDetailTab('tab-script');
        const id = button.closest('.attention-item').dataset.lineId;
        setTimeout(() => document.querySelector(`[data-line-id="${CSS.escape(id)}"]`)?.scrollIntoView({behavior: 'smooth', block: 'center'}), 50);
    }));
}

function renderProjectDetails(project) {
    renderProjectHeader(project);
    renderChapterList(project);
}

function renderProjectHeader(project) {
    document.getElementById('project-title').textContent = (project.title && project.title !== 'Unknown') ? project.title : 'Untitled';
    document.getElementById('project-author').textContent = (project.author && project.author !== 'Unknown') ? project.author : 'Unknown Author';
    const cover = document.getElementById('project-cover');
    cover.replaceChildren();
    if (project.cover_url) {
        const image = document.createElement('img');
        image.src = project.cover_url;
        image.alt = `Cover of ${project.title || 'audiobook'}`;
        cover.appendChild(image);
    } else {
        cover.textContent = '📖';
    }
    
    const status = String(project.status || 'created').toLowerCase();
    const statusToken = status.replace(/[^a-z_]/g, '');
    document.getElementById('project-stats').innerHTML = `
        <span>${project.total_chapters || 0} Chapters</span>
        <span>ID: ${escapeHtml(String(project.project_id || ''))}</span>
        <span>Started: ${formatDate(project.created_at)}</span>
    `;
    
    const stageColor = `var(--stage-${statusToken}, var(--text-primary))`;
    const coarseStatus = project.running === true ? 'running' : status;
    const displayStatus = formatProjectStatus(coarseStatus);
    const activeStage = String(project.active_stage || '').replaceAll('_', ' ');
    const statusDetail = project.running && activeStage && activeStage !== displayStatus
        ? ` · ${activeStage}`
        : '';
    document.getElementById('project-stage').innerHTML = `
        <span class="card-stage" style="border: 1px solid ${stageColor}; color: ${stageColor}">
            ${escapeHtml(displayStatus)}${escapeHtml(statusDetail)}
        </span>
    `;
}

async function apiErrorMessage(response, fallback) {
    try {
        const payload = await response.json();
        return payload.detail || fallback;
    } catch (_) {
        return fallback;
    }
}

function closeMetadataModal() {
    els.metadataModal.classList.add('hidden');
    state.metadataCandidate = null;
    document.getElementById('metadata-cover-preview').removeAttribute('src');
    document.getElementById('metadata-search-results').replaceChildren();
    document.getElementById('metadata-search-status').textContent = '';
    document.getElementById('metadata-search-title').value = '';
    document.getElementById('metadata-search-author').value = '';
    document.getElementById('metadata-manual-search').open = false;
    document.getElementById('metadata-review').classList.remove('hidden');
    els.metadataApply.classList.remove('hidden');
    if (state.lastModalTrigger?.focus) state.lastModalTrigger.focus();
    state.lastModalTrigger = null;
}

function showMetadataCandidate(candidate) {
    state.metadataCandidate = candidate;
    document.getElementById('metadata-modal-title').textContent = 'Review matched book details';
    const percent = Math.round(Number(candidate.confidence || 0) * 100);
    document.getElementById('metadata-match-summary').textContent =
        `${percent}% match from Google Books${candidate.cached ? ' · cached result' : ''}`;
    for (const [id, value] of Object.entries({
        'metadata-title': candidate.title,
        'metadata-author': candidate.author,
        'metadata-year': candidate.year,
        'metadata-genre': candidate.genre,
        'metadata-isbn': candidate.isbn
    })) {
        document.getElementById(id).textContent = value || 'Not provided';
    }
    document.getElementById('metadata-description').textContent =
        candidate.description || 'No description was provided by this match.';

    const cover = document.getElementById('metadata-cover-preview');
    const empty = document.getElementById('metadata-cover-empty');
    if (candidate.cover_preview_url) {
        cover.src = `${candidate.cover_preview_url}?v=${encodeURIComponent(candidate.fetched_at || Date.now())}`;
        cover.hidden = false;
        empty.hidden = true;
    } else {
        cover.hidden = true;
        empty.hidden = false;
    }
    const replaceRow = document.getElementById('metadata-replace-cover-row');
    const replaceInput = document.getElementById('metadata-replace-cover');
    replaceInput.checked = false;
    replaceRow.classList.toggle(
        'hidden',
        !(candidate.existing_cover && candidate.cover_preview_url)
    );
    const warnings = document.getElementById('metadata-warnings');
    const warningItems = candidate.warnings || [];
    warnings.textContent = warningItems.join(' ');
    warnings.classList.toggle('hidden', warningItems.length === 0);

    if (els.metadataModal.classList.contains('hidden')) {
        state.lastModalTrigger = document.activeElement;
    }
    document.getElementById('metadata-review').classList.remove('hidden');
    els.metadataApply.classList.remove('hidden');
    els.metadataModal.classList.remove('hidden');
    requestAnimationFrame(() => els.metadataApply.focus());
}

async function previewMetadataCandidate() {
    if (!state.currentProjectId) return;
    const button = document.getElementById('btn-fetch-metadata');
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = 'Checking…';
    try {
        const response = await fetch(
            `api/projects/${encodeURIComponent(state.currentProjectId)}/fetch-metadata`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ apply: false })
            }
        );
        if (!response.ok) {
            const message = await apiErrorMessage(response, 'Could not find book details');
            if (response.status === 404) {
                openManualMetadataSearch(message);
                return;
            }
            throw new Error(message);
        }
        showMetadataCandidate(await response.json());
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        button.disabled = false;
        button.textContent = originalText;
    }
}

function populateMetadataSearchInputs() {
    const project = state.currentProject || {};
    const title = document.getElementById('metadata-search-title');
    const author = document.getElementById('metadata-search-author');
    if (!title.value) title.value = project.title && project.title !== 'Unknown' ? project.title : '';
    if (!author.value) author.value = project.author && project.author !== 'Unknown' ? project.author : '';
}

function openManualMetadataSearch(message = '') {
    state.metadataCandidate = null;
    state.lastModalTrigger = document.activeElement;
    document.getElementById('metadata-modal-title').textContent = 'Search for book details';
    document.getElementById('metadata-match-summary').textContent =
        'Search by title and optionally narrow the results by author.';
    populateMetadataSearchInputs();
    document.getElementById('metadata-manual-search').open = true;
    document.getElementById('metadata-review').classList.add('hidden');
    els.metadataApply.classList.add('hidden');
    document.getElementById('metadata-search-status').textContent =
        message || 'Adjust the title or author, then choose the correct result.';
    els.metadataModal.classList.remove('hidden');
    requestAnimationFrame(() => document.getElementById('metadata-search-title').focus());
}

function renderMetadataSearchResults(results) {
    const container = document.getElementById('metadata-search-results');
    container.replaceChildren();
    for (const candidate of results) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'metadata-search-result';
        button.dataset.metadataVolumeId = candidate.provider_id;

        const details = document.createElement('span');
        const title = document.createElement('strong');
        title.textContent = candidate.title || 'Untitled';
        const subtitle = document.createElement('small');
        subtitle.textContent = [candidate.author, candidate.year, candidate.isbn]
            .filter(Boolean).join(' · ') || 'No additional details';
        details.append(title, subtitle);

        const action = document.createElement('span');
        action.textContent = 'Review';
        button.append(details, action);
        container.appendChild(button);
    }
}

async function performManualMetadataSearch(event) {
    event?.preventDefault();
    if (!state.currentProjectId) return;
    const title = document.getElementById('metadata-search-title').value.trim();
    const author = document.getElementById('metadata-search-author').value.trim();
    const button = document.getElementById('metadata-search-submit');
    const status = document.getElementById('metadata-search-status');
    if (!title) {
        status.textContent = 'Enter a book title.';
        return;
    }
    button.disabled = true;
    status.textContent = 'Searching Google Books…';
    document.getElementById('metadata-search-results').replaceChildren();
    try {
        const response = await fetch(
            `api/projects/${encodeURIComponent(state.currentProjectId)}/search-metadata`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, author })
            }
        );
        if (!response.ok) {
            throw new Error(await apiErrorMessage(response, 'Metadata search failed'));
        }
        const payload = await response.json();
        const results = payload.results || [];
        renderMetadataSearchResults(results);
        status.textContent = results.length
            ? `${results.length} result${results.length === 1 ? '' : 's'} found. Choose one to review.`
            : (payload.error || 'No books matched this search.');
    } catch (error) {
        status.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

async function selectManualMetadataCandidate(providerId) {
    if (!state.currentProjectId || !providerId) return;
    const status = document.getElementById('metadata-search-status');
    status.textContent = 'Loading the selected edition…';
    try {
        const response = await fetch(
            `api/projects/${encodeURIComponent(state.currentProjectId)}/fetch-metadata`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    apply: false,
                    provider_id: providerId,
                    query_title: document.getElementById('metadata-search-title').value.trim(),
                    query_author: document.getElementById('metadata-search-author').value.trim()
                })
            }
        );
        if (!response.ok) {
            throw new Error(await apiErrorMessage(response, 'Could not load that edition'));
        }
        showMetadataCandidate(await response.json());
        document.getElementById('metadata-manual-search').open = false;
        status.textContent = '';
    } catch (error) {
        status.textContent = error.message;
    }
}

async function applyMetadataCandidate() {
    if (!state.currentProjectId || !state.metadataCandidate) return;
    const originalText = els.metadataApply.textContent;
    els.metadataApply.disabled = true;
    els.metadataApply.textContent = 'Applying…';
    try {
        const response = await fetch(
            `api/projects/${encodeURIComponent(state.currentProjectId)}/fetch-metadata`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    apply: true,
                    replace_cover: document.getElementById('metadata-replace-cover').checked
                })
            }
        );
        if (!response.ok) {
            throw new Error(await apiErrorMessage(response, 'Could not apply book details'));
        }
        closeMetadataModal();
        const result = await response.json();
        const exportNote = result.refreshed_exports
            ? ` and updated ${result.refreshed_exports} audiobook package${result.refreshed_exports === 1 ? '' : 's'}`
            : '';
        showToast(`Matched book details applied${exportNote}`, 'success');
        await fetchProjectDetails(state.currentProjectId);
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        els.metadataApply.disabled = false;
        els.metadataApply.textContent = originalText;
    }
}

function renderChapterList(project) {
    const grid = document.getElementById('chapter-grid');
    if (!grid) return;
    grid.innerHTML = '';

    const total = project.total_chapters || 0;
    if (chapterPaginationState.projectId !== project.project_id) {
        chapterPaginationState.projectId = project.project_id;
        const chapterDetails = document.getElementById('chapter-progress-section');
        if (chapterDetails) {
            chapterDetails.open = !['complete', 'completed'].includes(
                String(project.status || '').toLowerCase()
            );
        }
    }
    const scripted = new Set(project.scripted_chapters || []);
    const generated = new Set(project.generated_chapters || []);
    const mastered = new Set(project.mastered_chapters || []);
    const currentScript = project.current_script_chapter;
    const currentGen = project.current_gen_chapter;
    const selectedNumbers = project.active_generation_chapter_selection
        || project.generation_chapter_selection;
    const selection = selectedNumbers ? new Set(selectedNumbers) : null;
    const selectionLocked = project.running === true;
    const detailsMap = new Map(
        (project.chapter_details || []).map(detail => [detail.number, detail])
    );

    document.getElementById('chapter-summary-badge').textContent =
        `${mastered.size} / ${total} mastered`;

    for (let chapter = 1; chapter <= total; chapter++) {
        const detail = detailsMap.get(chapter) || {};
        const title = detail.title || `Chapter ${chapter}`;
        const totalLines = detail.total_lines || 0;
        const generatedLines = detail.lines_generated || 0;
        let percent = detail.progress_percent || 0;
        let statusKey = 'pending';
        let statusText = 'Pending';
        let statusBackground = 'rgba(148, 163, 184, 0.12)';
        let statusColor = '#94a3b8';
        let download = '<span></span>';
        const stage = String(project.active_stage || project.status || '').toLowerCase();

        const isSelectedInBatch = selection === null || selection.has(chapter);

        if (mastered.has(chapter)) {
            statusKey = 'done';
            statusText = 'Mastered';
            statusBackground = 'rgba(16, 185, 129, 0.15)';
            statusColor = '#34d399';
            percent = 100;
            download = `<a class="chapter-download" data-server-download href="api/projects/${encodeURIComponent(project.project_id)}/download/chapter/${chapter}" target="_blank" rel="noopener" aria-label="Download chapter ${chapter} mastered WAV" title="Download mastered chapter WAV">↓</a>`;
        } else if (generated.has(chapter)) {
            statusKey = 'generated';
            statusText = 'Generated';
            statusBackground = 'rgba(168, 85, 247, 0.15)';
            statusColor = '#c084fc';
            percent = 100;
        } else if (stage.includes('generat') && currentGen === chapter) {
            statusKey = 'active';
            statusText = totalLines > 0 && generatedLines >= totalLines
                ? `Validating ${generatedLines}/${totalLines}`
                : `Generating ${generatedLines}/${totalLines}`;
            statusBackground = 'rgba(59, 130, 246, 0.2)';
            statusColor = '#60a5fa';
            if (totalLines > 0 && generatedLines >= totalLines) percent = 99;
        } else if (stage.includes('script') && project.work_progress?.phase !== 'character_analysis' && (currentScript === chapter || (!currentScript && chapter === scripted.size + 1 && chapter <= total))) {
            statusKey = 'active';
            statusText = 'Scripting...';
            statusBackground = 'rgba(234, 179, 8, 0.2)';
            statusColor = '#facc15';
            percent = detail.progress_percent || 50;
        } else if (scripted.has(chapter)) {
            statusKey = 'scripted';
            statusText = totalLines > 0 ? `Scripted · ${totalLines} lines` : 'Scripted';
            statusBackground = 'rgba(132, 204, 22, 0.15)';
            statusColor = '#a3e635';
            if (stage.includes('script') || stage === 'voice_review') {
                percent = 100;
            }
        } else if (selection !== null && !selection.has(chapter) && (stage.includes('generat') || stage.includes('master') || stage.includes('validat'))) {
            statusKey = 'skipped';
            statusText = 'Skipped (Not in batch)';
            statusBackground = 'rgba(148, 163, 184, 0.08)';
            statusColor = '#64748b';
            percent = detail.progress_percent || 0;
        } else {
            statusKey = 'pending';
            statusText = 'Pending';
            statusBackground = 'rgba(148, 163, 184, 0.12)';
            statusColor = '#94a3b8';
            percent = detail.progress_percent || 0;
        }

        const row = document.createElement('div');
        row.className = `chapter-cell${statusKey === 'active' ? ' chapter-active' : ''}`;
        const isInActiveBatch = selectionLocked && selection?.has(chapter);
        if (isInActiveBatch) row.classList.add('chapter-selected');
        row.dataset.title = title.toLowerCase();
        row.dataset.status = statusKey;
        const isChecked = selection === null || selection.has(chapter);
        row.innerHTML = `
            <input type="checkbox" class="chapter-select-cb" data-ch="${chapter}"
                ${isChecked ? 'checked' : ''} ${selectionLocked ? 'disabled' : ''}
                aria-label="Include chapter ${chapter}, ${escapeHtml(title)}, in the next audio batch"
                title="${selectionLocked ? 'The active batch is locked while the pipeline runs' : 'Include this chapter in the next audio batch'}">
            <div class="chapter-title-wrap">
                <span class="chapter-number">${chapter}</span>
                <span class="chapter-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
                ${isInActiveBatch ? '<span class="chapter-run-badge">In this run</span>' : ''}
            </div>
            <span class="chapter-status-pill" style="background:${statusBackground};color:${statusColor}">${statusText}</span>
            <div class="chapter-progress-value">
                <div class="bar"><span style="width:${Math.min(percent, 100)}%;background:${statusColor}"></span></div>
                <span>${Math.min(percent, 100)}%</span>
            </div>
            ${download}
        `;
        row.querySelector('.chapter-select-cb').addEventListener(
            'change',
            updateChapterSelectionState
        );
        grid.appendChild(row);
    }

    updateSelectionSummary(project);
    renderWorkStatus(project);
    filterChapterRows();

    const requestDeploy = document.getElementById('btn-request-deploy');
    const resumeDeploy = document.getElementById('btn-resume-deploy');
    requestDeploy?.classList.toggle(
        'hidden',
        !project.running || project.status === 'deploy_paused'
    );
    resumeDeploy?.classList.toggle('hidden', project.status !== 'deploy_paused');
}

function parseChapterRange(value) {
    if (!value) return null;
    const result = new Set();
    for (const rawToken of value.split(',')) {
        const token = rawToken.trim();
        if (!token) continue;
        const match = token.match(/^(\d+)(?:\s*-\s*(\d+))?$/);
        if (!match) return null;
        const start = parseInt(match[1], 10);
        const end = parseInt(match[2] || match[1], 10);
        if (start < 1 || end < start || end - start > 10000) return null;
        for (let chapter = start; chapter <= end; chapter++) result.add(chapter);
    }
    return result.size ? result : null;
}

const chapterPaginationState = {
    currentPage: 1,
    pageSize: '15',
    projectId: null
};

function filterChapterRows() {
    const search = (document.getElementById('chapter-search-input')?.value || '')
        .trim().toLowerCase();
    const status = document.getElementById('chapter-status-filter')?.value || 'all';
    
    const rows = Array.from(document.querySelectorAll('.chapter-cell'));
    const matchingRows = rows.filter(row => {
        const titleMatch = !search || row.dataset.title.includes(search);
        const statusMatch = status === 'all' || row.dataset.status === status;
        return titleMatch && statusMatch;
    });

    const isAll = chapterPaginationState.pageSize === 'all';
    const pageSize = isAll ? Math.max(1, matchingRows.length) : parseInt(chapterPaginationState.pageSize, 10);
    const totalPages = Math.max(1, Math.ceil(matchingRows.length / (pageSize || 1)));
    if (chapterPaginationState.currentPage > totalPages) {
        chapterPaginationState.currentPage = totalPages;
    }
    const currentPage = chapterPaginationState.currentPage;

    const startIndex = (currentPage - 1) * pageSize;
    const endIndex = isAll ? matchingRows.length : Math.min(startIndex + pageSize, matchingRows.length);

    rows.forEach(row => {
        row.hidden = true;
    });

    matchingRows.forEach((row, idx) => {
        if (isAll || (idx >= startIndex && idx < endIndex)) {
            row.hidden = false;
        }
    });

    const info = document.getElementById('pagination-info');
    const pageNum = document.getElementById('pagination-page-num');
    const prevBtn = document.getElementById('btn-prev-page');
    const nextBtn = document.getElementById('btn-next-page');
    const pagination = document.getElementById('chapter-pagination-toolbar');

    if (info) {
        if (matchingRows.length === 0) {
            info.textContent = 'No matching chapters';
        } else if (isAll) {
            info.textContent = `Showing all ${matchingRows.length} chapters`;
        } else {
            info.textContent = `Showing ${startIndex + 1}-${endIndex} of ${matchingRows.length} chapters`;
        }
    }
    if (pageNum) {
        pageNum.textContent = `Page ${currentPage} of ${totalPages}`;
    }
    if (prevBtn) prevBtn.disabled = currentPage <= 1 || isAll;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages || isAll;
    if (pagination) pagination.classList.toggle('hidden', matchingRows.length <= pageSize || isAll);
}

function updateSelectionSummary(project = null) {
    const summary = document.getElementById('chapter-selection-summary');
    if (!summary) return;
    const checkboxes = [...document.querySelectorAll('.chapter-select-cb')];
    const selected = checkboxes.filter(cb => cb.checked).length;
    const locked = project?.running === true;
    summary.textContent = selected === checkboxes.length
        ? `All ${selected} chapters${locked ? ' · active batch' : ''}`
        : `${selected} of ${checkboxes.length} selected${locked ? ' · active batch' : ''}`;
}

function renderWorkStatus(project) {
    const details = project.chapter_details || [];
    const detailMap = new Map(details.map(item => [item.number, item]));
    const selected = project.active_generation_chapter_selection
        || project.generation_chapter_selection
        || Array.from({length: project.total_chapters || 0}, (_, index) => index + 1);
    const selectedSet = new Set(selected);
    const mastered = new Set(project.mastered_chapters || []);
    const generated = new Set(project.generated_chapters || []);
    const currentChapter = project.current_gen_chapter || project.current_script_chapter;
    const currentDetail = detailMap.get(currentChapter) || {};
    const status = String(project.status || 'created').toLowerCase();
    const terminalStatuses = new Set([
        'paused', 'pausing', 'paused_scheduled', 'deploy_paused', 'waiting_for_review', 'error',
        'complete', 'completed', 'selection_complete'
    ]);
    const stage = terminalStatuses.has(status)
        ? status
        : String(project.active_stage || status).toLowerCase();
    const chapterTitle = currentDetail.title || (
        currentChapter ? `Chapter ${currentChapter}` : ''
    );
    const workProgress = project.work_progress || {};
    const progress = project.progress || null;
    const totalBookChapters = project.total_chapters || 0;
    const isSelectiveBatch = selected.length > 0 && selected.length < totalBookChapters;
    let batchSummary = 'Full book';
    if (isSelectiveBatch) {
        if (selected.length <= 4) {
            batchSummary = `Chapters ${selected.join(', ')} (${selected.length} selected)`;
        } else {
            batchSummary = `${selected.length} selected chapters`;
        }
    }

    let completedUnits = 0;
    for (const chapter of selectedSet) {
        if (mastered.has(chapter)) completedUnits += 1;
        else if (generated.has(chapter)) completedUnits += 0.85;
        else if (chapter === currentChapter) {
            completedUnits += Math.min((currentDetail.progress_percent || 0) / 100, 0.8);
        }
    }
    let overall = selectedSet.size
        ? Math.round((completedUnits / selectedSet.size) * 100)
        : 0;
    let overallLabel = 'Audio batch';
    let chapterMetric = currentChapter
        ? `${selected.indexOf(currentChapter) >= 0 ? selected.indexOf(currentChapter) + 1 : '?'} / ${selected.length}`
        : '—';
    let chapterLabel = 'Batch chapter';
    let lineMetric = currentDetail.total_lines
        ? `${currentDetail.lines_generated || 0} / ${currentDetail.total_lines}`
        : '—';
    let lineLabel = 'Current utterance';

    let activity = 'Waiting to start';
    let description = 'Choose chapters and start the pipeline.';
    if (stage === 'paused_scheduled') {
        activity = 'Waiting for working hours';
        description = project.pause_reason ||
            'The pipeline will resume automatically when a configured window opens.';
    } else if (stage === 'deploy_paused') {
        activity = 'Parked safely';
        description = project.pause_reason ||
            'The current chapter boundary is safe for maintenance.';
    } else if (stage === 'paused' || stage === 'pausing') {
        activity = stage === 'pausing'
            ? 'Finishing the current safe unit'
            : 'Pipeline paused';
        description = project.pause_reason || 'Resume when you are ready.';
    } else if (stage.includes('script')) {
        overall = Number.isFinite(workProgress.stagePercent)
            ? workProgress.stagePercent
            : Math.round(
                100 * (project.scripted_chapters || []).length
                / Math.max(project.total_chapters || 0, 1)
            );
        overallLabel = workProgress.phase === 'character_analysis'
            ? 'Character Analysis'
            : 'Scripting stage';
        chapterMetric = workProgress.position || '—';
        chapterLabel = workProgress.chapterLabel || (
            workProgress.phase === 'character_analysis' ? 'Book chapter' : 'Book chapter'
        );
        lineMetric = workProgress.tokens ? `${workProgress.tokens}` : (workProgress.chunkPosition || '—');
        lineLabel = workProgress.tokens ? 'Tokens streaming' : (workProgress.phase === 'character_analysis' ? 'Analysis chunk' : 'LLM response');

        if (workProgress.phase === 'character_analysis') {
            activity = workProgress.current || 'Character Analysis · Extracting Cast';
            description = workProgress.detail || 'Extracting character profiles, genders, and aliases across the full book.';
            if (isSelectiveBatch) {
                description += ` · Selected for audio: ${batchSummary}`;
            }
        } else {
            activity = workProgress.current || (
                chapterTitle ? `Scripting — ${chapterTitle}` : 'Annotating scripts for the full book'
            );
            description = workProgress.detail || `${(project.scripted_chapters || []).length} of ${project.total_chapters || 0} chapters scripted.`;
            if (isSelectiveBatch) {
                description += ` · Selected for audio: ${batchSummary}`;
            }
        }
    } else if (stage === 'voice_review') {
        overall = 100;
        overallLabel = 'Voice preparation';
        chapterMetric = 'Ready';
        chapterLabel = 'Speaking cast';
        lineMetric = 'Approval';
        lineLabel = 'Next action';
        activity = 'Waiting for voice-cast approval';
        description = 'Preview or change the speaking voices in the Voice casting tab, then approve them once to begin audio generation.';
    } else if (stage.includes('bootstrap')) {
        activity = 'Preparing character voice references';
        description = 'Creating reusable voice identities before chapter generation.';
    } else if (stage.includes('generat') && currentChapter) {
        const validating = currentDetail.total_lines > 0
            && currentDetail.lines_generated >= currentDetail.total_lines;
        activity = `${validating ? 'Validating' : 'Generating'} — ${chapterTitle}`;
        description = validating
            ? 'All audio files exist; acceptance checks and retries are finishing.'
            : `Utterance ${currentDetail.lines_generated || 0} of ${currentDetail.total_lines || 0}.`;
    } else if (stage.includes('validat')) {
        lineMetric = currentDetail.total_lines
            ? `${currentDetail.lines_validated || 0} / ${currentDetail.total_lines}`
            : '—';
        lineLabel = 'Validated utterances';
        activity = chapterTitle
            ? `Validating — ${chapterTitle}`
            : 'Validating generated audio';
        description = 'Checking transcription, duration, silence, and pacing.';
    } else if (stage.includes('master')) {
        activity = chapterTitle
            ? `Mastering — ${chapterTitle}`
            : 'Mastering completed chapter audio';
        description = `${mastered.size} of ${selectedSet.size} selected chapters mastered.`;
    } else if (stage.includes('export')) {
        activity = 'Exporting audiobook';
        description = 'Packaging mastered chapters and metadata.';
    } else if (['complete', 'completed', 'selection_complete'].includes(stage)) {
        activity = stage === 'selection_complete'
            ? 'Selected batch complete'
            : 'Audiobook complete';
        description = `${mastered.size} chapters are mastered and available.`;
    } else if (stage === 'error') {
        activity = 'Pipeline stopped on an error';
        description = project.error_message ||
            'Inspect the logs, then resume after correcting the issue.';
    }

    if (progress && project.running) {
        activity = progress.message || activity;
        if (Number.isFinite(progress.percent)) {
            overall = Math.round(progress.percent);
            overallLabel = `${(progress.phase || progress.stage || 'Current').replaceAll('_', ' ')}`;
        }
        if (progress.chapter_position && progress.chapter_total) {
            chapterMetric = `${progress.chapter_position} / ${progress.chapter_total}`;
        }
        if (progress.line_position && progress.line_total) {
            lineMetric = `${progress.line_position} / ${progress.line_total}`;
        }
    }

    const etaSeconds = progress?.eta_seconds;
    const etaText = Number.isFinite(etaSeconds)
        ? (etaSeconds >= 3600
            ? `${(etaSeconds / 3600).toFixed(1)} h`
            : etaSeconds >= 60
                ? `${Math.ceil(etaSeconds / 60)} min`
                : `${Math.ceil(etaSeconds)} sec`)
        : '—';
    const updatedAt = progress?.updated_at || (project.running ? (project.last_run_started_at || new Date().toISOString()) : (project.last_activity_at || project.updated_at));
    let freshness = 'No activity recorded yet.';
    if (project.running) {
        freshness = 'Active · Running live';
    } else if (updatedAt) {
        const ageSeconds = Math.max(0, Math.round((Date.now() - Date.parse(updatedAt)) / 1000));
        freshness = ageSeconds < 10
            ? 'Updated just now'
            : ageSeconds < 120
                ? `Updated ${ageSeconds} seconds ago`
                : `Updated ${Math.round(ageSeconds / 60)} minutes ago`;
    }

    document.getElementById('work-status-title').textContent = activity;
    document.getElementById('work-status-detail').textContent = description;
    document.getElementById('work-overall-percent').textContent = `${overall}%`;
    document.getElementById('work-overall-fill').style.width = `${overall}%`;
    document.getElementById('work-overall-label').textContent = overallLabel;
    document.getElementById('work-chapter-position').textContent = chapterMetric;
    document.getElementById('work-chapter-label').textContent = chapterLabel;
    document.getElementById('work-line-position').textContent = lineMetric;
    document.getElementById('work-line-label').textContent = lineLabel;
    document.getElementById('work-eta').textContent = etaText;
    document.getElementById('work-eta-label').textContent = progress?.eta_confidence
        ? `ETA · ${progress.eta_confidence} confidence`
        : 'Estimated remaining';
    const isComplete = ['complete', 'completed'].includes(stage);
    const workPanel = document.getElementById('work-status-panel');
    workPanel.classList.toggle('terminal', isComplete);
    document.getElementById('work-status-freshness').textContent = isComplete
        ? 'No pipeline work is active.'
        : freshness;
    const pipelineSummary = document.getElementById('pipeline-summary');
    if (pipelineSummary) pipelineSummary.textContent = isComplete
        ? `${mastered.size} of ${project.total_chapters || 0} chapters mastered`
        : activity;
}

// Retained temporarily for compatibility with older embedded shells. New
// dashboard renders use the scalable row-based implementation above.
function renderChapterGridLegacy(project) {
    const grid = document.getElementById('chapter-grid');
    if (!grid) return;
    grid.innerHTML = '';
    grid.style.maxHeight = '420px';
    grid.style.overflowY = 'auto';
    grid.style.paddingRight = '6px';

    const total = project.total_chapters || 0;
    const scripted = new Set(project.scripted_chapters || []);
    const generated = new Set(project.generated_chapters || []);
    const mastered = new Set(project.mastered_chapters || []);
    const currentScript = project.current_script_chapter;
    const currentGen = project.current_gen_chapter;
    const selection = project.generation_chapter_selection ? new Set(project.generation_chapter_selection) : null;
    const detailsMap = {};
    if (project.chapter_details) {
        project.chapter_details.forEach(d => { detailsMap[d.number] = d; });
    }

    const summaryBadge = document.getElementById('chapter-summary-badge');
    if (summaryBadge) {
        const completedCount = mastered.size || generated.size || 0;
        summaryBadge.textContent = `${completedCount} / ${total} Completed`;
    }

    for (let i = 1; i <= total; i++) {
        const cell = document.createElement('div');
        cell.className = 'chapter-cell';
        cell.style.cssText = 'padding: 10px 14px; border-radius: 10px; background: rgba(24, 24, 37, 0.9); border: 1px solid rgba(255,255,255,0.08); display: flex; flex-direction: column; gap: 6px; font-size: 0.85em; transition: all 0.2s ease; box-shadow: 0 2px 8px rgba(0,0,0,0.2);';

        cell.onmouseenter = () => {
            cell.style.borderColor = 'rgba(99, 102, 241, 0.4)';
            cell.style.transform = 'translateY(-2px)';
            cell.style.boxShadow = '0 4px 12px rgba(99, 102, 241, 0.15)';
        };
        cell.onmouseleave = () => {
            cell.style.borderColor = 'rgba(255,255,255,0.08)';
            cell.style.transform = 'none';
            cell.style.boxShadow = '0 2px 8px rgba(0,0,0,0.2)';
        };

        const detail = detailsMap[i] || {};
        const title = detail.title || `Chapter ${i}`;
        const totalLines = detail.total_lines || 0;
        const genLines = detail.lines_generated || 0;
        let pct = detail.progress_percent || 0;

        let statusText = '⬜ Pending';
        let statusBg = 'rgba(148, 163, 184, 0.12)';
        let statusColor = '#94a3b8';
        let downloadBtn = '';

        const stageLower = (project.stage || project.status || '').toLowerCase();
        const isGeneratingStage = stageLower.includes('gen');
        const isScriptingStage = stageLower.includes('script');

        if (mastered.has(i)) {
            statusText = '✅ Done';
            statusBg = 'rgba(16, 185, 129, 0.15)';
            statusColor = '#34d399';
            pct = 100;
            downloadBtn = `<a data-server-download href="api/projects/${encodeURIComponent(project.project_id)}/download/chapter/${i}" target="_blank" rel="noopener" title="Download Mastered Chapter WAV" style="color: #34d399; text-decoration: none; font-size: 1.1em; margin-left: 6px; transition: transform 0.2s ease;">⬇</a>`;
        } else if (generated.has(i)) {
            statusText = '🟣 Generated';
            statusBg = 'rgba(168, 85, 247, 0.15)';
            statusColor = '#c084fc';
            pct = 100;
        } else if (isGeneratingStage && currentGen === i) {
            if (totalLines > 0 && genLines >= totalLines) {
                statusText = `🔎 Validating (${genLines}/${totalLines})`;
                // WAVs exist, but the chapter is incomplete until validation
                // accepts every required line.
                pct = 99;
            } else {
                statusText = `🔵 Gen (${genLines}/${totalLines})`;
            }
            statusBg = 'rgba(59, 130, 246, 0.15)';
            statusColor = '#60a5fa';
        } else if (scripted.has(i)) {
            statusText = `🟢 Scripted (${totalLines}l)`;
            statusBg = 'rgba(132, 204, 22, 0.15)';
            statusColor = '#a3e635';
        } else if (isScriptingStage && project.work_progress?.phase !== 'character_analysis' && (currentScript === i || (!currentScript && i === (scripted.size + 1)))) {
            statusText = '🟡 Scripting...';
            statusBg = 'rgba(234, 179, 8, 0.15)';
            statusColor = '#facc15';
        }

        const isChecked = selection === null || selection.has(i);

        cell.innerHTML = `
            <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                <div style="display: flex; align-items: center; gap: 6px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; flex: 1;">
                    <input type="checkbox" class="chapter-select-cb" data-ch="${i}" ${isChecked ? 'checked' : ''} style="cursor: pointer; accent-color: #6366f1; flex-shrink: 0;">
                    <span style="background: rgba(99, 102, 241, 0.2); color: #a5b4fc; padding: 1px 5px; border-radius: 4px; font-weight: 700; font-size: 0.78em; flex-shrink: 0;">Ch ${i}</span>
                    <span style="font-weight: 600; color: #f3f4f6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.84em;" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
                </div>
                <div style="display: flex; align-items: center; flex-shrink: 0;">
                    <span style="background: ${statusBg}; color: ${statusColor}; border: 1px solid ${statusColor}33; padding: 2px 8px; border-radius: 12px; font-weight: 600; font-size: 0.76em; letter-spacing: 0.02em;">${statusText}</span>
                    ${downloadBtn}
                </div>
            </div>
            <div style="display: flex; align-items: center; gap: 8px; margin-top: 2px;">
                <div style="flex: 1; height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden;">
                    <div style="height: 100%; width: ${pct}%; background: ${statusColor}; transition: width 0.4s ease; border-radius: 3px;"></div>
                </div>
                <span style="font-size: 0.75em; color: #9ca3af; font-weight: 500; width: 32px; text-align: right;">${pct}%</span>
            </div>
        `;

        const cb = cell.querySelector('.chapter-select-cb');
        cb.addEventListener('change', updateChapterSelectionState);

        grid.appendChild(cell);
    }

    const btnReqDeploy = document.getElementById('btn-request-deploy');
    const btnResDeploy = document.getElementById('btn-resume-deploy');
    if (project.status === 'deploy_paused') {
        if (btnReqDeploy) btnReqDeploy.classList.add('hidden');
        if (btnResDeploy) btnResDeploy.classList.remove('hidden');
    } else if (project.running) {
        if (btnReqDeploy) btnReqDeploy.classList.remove('hidden');
        if (btnResDeploy) btnResDeploy.classList.add('hidden');
    } else {
        if (btnReqDeploy) btnReqDeploy.classList.add('hidden');
        if (btnResDeploy) btnResDeploy.classList.add('hidden');
    }
}

let _selectionDebounceTimer = null;

async function saveChapterSelection(projectId, chapters) {
    const res = await fetch(`api/projects/${projectId}/set-selection`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapters })
    });
    if (!res.ok) {
        const error = await res.json().catch(() => ({}));
        throw new Error(error.detail || 'Failed to save chapter selection');
    }
}

function deriveWorkProgress(lines, totalChapters) {
    const progress = {
        phase: null,
        stagePercent: null,
        current: null,
        detail: null,
        position: null,
        chapterLabel: null,
        chunkPosition: null,
        tokens: null,
        chapterIndex: null,
        chapterTotal: null,
        chapterTitle: null
    };

    for (const line of lines) {
        let unitMatch = line.match(/Analyzing unit\s+(\d+)\/(\d+)(?::\s*chapter\s+(\d+)\s+part\s+(\d+)\s+['"](.+?)['"])?/i);
        if (unitMatch) {
            const currentUnit = Number(unitMatch[1]);
            const totalUnits = Number(unitMatch[2]);
            const chapterNum = unitMatch[3] ? Number(unitMatch[3]) : null;
            const partNum = unitMatch[4] ? Number(unitMatch[4]) : null;
            const chTitle = unitMatch[5] || '';

            progress.phase = 'character_analysis';
            progress.stagePercent = Math.round(20 * currentUnit / Math.max(totalUnits, 1));

            if (chapterNum && chTitle) {
                progress.current = `Character Analysis · ${chTitle} (Ch ${chapterNum} of ${totalChapters || '?'})`;
                progress.detail = `Scanning chapter text (part ${partNum}) to extract cast profiles, genders, and aliases.`;
                progress.position = `${chapterNum} / ${totalChapters || '?'}`;
                progress.chapterLabel = 'Book chapter';
            } else {
                progress.current = `Character Analysis · Chunk ${currentUnit} of ${totalUnits}`;
                progress.detail = `Scanning book text across chapters to extract character cast and aliases.`;
                progress.position = `${currentUnit} / ${totalUnits}`;
                progress.chapterLabel = 'Analysis chunk';
            }
            progress.chunkPosition = `${currentUnit} / ${totalUnits}`;
            progress.tokens = null;
            continue;
        }

        let match = line.match(/\[ScriptGenerator\].*Chapter\s+(\d+)\/(\d+):\s*['"](.+?)['"]/i);
        if (match) {
            const current = Number(match[1]);
            const total = Number(match[2]) || totalChapters;
            const title = match[3];
            progress.phase = 'chapter_scripting';
            progress.stagePercent = 20 + Math.round(80 * (current - 1) / Math.max(total, 1));
            progress.current = `Scripting · Chapter ${current} of ${total}: ${title}`;
            progress.detail = `Annotating dialogue, speaker attribution, and scene directions for ${title}.`;
            progress.position = `${current} / ${total}`;
            progress.chapterLabel = 'Scripting chapter';
            progress.tokens = null;
            progress.chapterIndex = current;
            progress.chapterTotal = total;
            progress.chapterTitle = title;
            continue;
        }

        match = line.match(/Processing fragment chunk\s+(\d+)\/(\d+)/i);
        if (match && progress.phase === 'chapter_scripting') {
            const chunk = Number(match[1]);
            const chunks = Number(match[2]);
            progress.stagePercent = 20 + Math.round(
                80 * (
                    (progress.chapterIndex - 1) + ((chunk - 1) / Math.max(chunks, 1))
                ) / Math.max(progress.chapterTotal, 1)
            );
            progress.detail = `Annotating dialogue batch ${chunk} of ${chunks} for ${progress.chapterTitle || 'current chapter'}.`;
            progress.tokens = null;
            continue;
        }

        match = line.match(/\[ScriptGenerator\].*Chapter\s+(\d+)\/(\d+)\s+done/i);
        if (match) {
            const current = Number(match[1]);
            const total = Number(match[2]) || totalChapters;
            progress.phase = 'chapter_scripting';
            progress.stagePercent = 20 + Math.round(80 * current / Math.max(total, 1));
            progress.current = `Scripting · Chapter ${current} of ${total} complete`;
            progress.detail = `${current} of ${total} chapter scripts complete.`;
            progress.position = `${current} / ${total}`;
            progress.chapterLabel = 'Scripting chapter';
            progress.tokens = null;
            continue;
        }

        match = line.match(/Streaming\D+(\d+)\s+tokens/i);
        if (match && progress.phase) {
            progress.tokens = Number(match[1]);
        }
    }

    return progress;
}

function updateChapterSelectionState() {
    if (!state.currentProjectId) return;
    if (_selectionDebounceTimer) clearTimeout(_selectionDebounceTimer);

    const cbs = document.querySelectorAll('.chapter-select-cb');
    const selected = [];
    let total = cbs.length;

    cbs.forEach(cb => {
        if (cb.checked) {
            selected.push(parseInt(cb.dataset.ch, 10));
        }
    });

    const selectionValue = selected.length === total ? null : selected;
    const targetProjectId = state.currentProjectId;
    updateSelectionSummary();

    _selectionDebounceTimer = setTimeout(async () => {
        try {
            await saveChapterSelection(targetProjectId, selectionValue);
            if (selected.length === 0) {
                showToast('Cleared chapter selection', 'info');
            }
        } catch (e) {
            console.error('Failed to update selection', e);
            showToast(e.message || 'Failed to save chapter selection', 'error');
        }
    }, 300);
}

const SCHEDULE_DAYS = [
    'Monday', 'Tuesday', 'Wednesday', 'Thursday',
    'Friday', 'Saturday', 'Sunday'
];

async function loadSchedule() {
    try {
        const response = await fetch('api/schedule');
        if (!response.ok) throw new Error('Failed to load working hours');
        const data = await response.json();
        state.schedule = data.schedule;
        state.lastScheduleRefresh = Date.now();
        renderSchedule(data.schedule, data.is_open);
    } catch (error) {
        console.error(error);
        const summary = document.getElementById('schedule-summary');
        if (summary) summary.textContent = 'Could not load schedule';
    }
}

function renderSchedule(schedule, isOpen) {
    document.getElementById('schedule-enabled').checked = Boolean(schedule.enabled);
    document.getElementById('schedule-timezone').value =
        schedule.timezone || 'Europe/Bucharest';
    const windows = document.getElementById('schedule-windows');
    windows.innerHTML = '';
    (schedule.windows || []).forEach(addScheduleWindow);
    if (!(schedule.windows || []).length) {
        addScheduleWindow({
            days: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
            start: '09:00',
            end: '17:00'
        });
    }

    const statePill = document.getElementById('schedule-state');
    statePill.className = 'schedule-state';
    if (!schedule.enabled) {
        statePill.textContent = 'Off';
        document.getElementById('schedule-summary').textContent =
            'Scheduling is off; manual starts run at any time';
    } else {
        statePill.textContent = isOpen ? 'Open now' : 'Closed now';
        statePill.classList.add(isOpen ? 'open' : 'closed');
        document.getElementById('schedule-summary').textContent =
            `${schedule.windows.length} working window${schedule.windows.length === 1 ? '' : 's'} · ${schedule.timezone}`;
    }
}

function addScheduleWindow(windowConfig) {
    const container = document.getElementById('schedule-windows');
    if (!container) return;
    const row = document.createElement('div');
    row.className = 'schedule-window';
    const selectedDays = new Set(windowConfig.days || []);
    row.innerHTML = `
        <div class="schedule-days">
            ${SCHEDULE_DAYS.map(day => `
                <label class="schedule-day" title="${day}">
                    <input type="checkbox" value="${day}" ${selectedDays.has(day) ? 'checked' : ''}>
                    <span>${day.slice(0, 2)}</span>
                </label>
            `).join('')}
        </div>
        <input class="input-sm schedule-start" type="time" value="${windowConfig.start || '09:00'}" aria-label="Start time">
        <span class="schedule-separator">to</span>
        <input class="input-sm schedule-end" type="time" value="${windowConfig.end || '17:00'}" aria-label="End time">
        <button type="button" class="schedule-remove" title="Remove window">×</button>
    `;
    row.querySelector('.schedule-remove').addEventListener('click', () => row.remove());
    container.appendChild(row);
}

async function saveSchedule() {
    const button = document.getElementById('btn-save-schedule');
    const windows = [...document.querySelectorAll('.schedule-window')].map(row => ({
        days: [...row.querySelectorAll('.schedule-day input:checked')].map(input => input.value),
        start: row.querySelector('.schedule-start').value,
        end: row.querySelector('.schedule-end').value
    }));
    const payload = {
        enabled: document.getElementById('schedule-enabled').checked,
        timezone: document.getElementById('schedule-timezone').value.trim(),
        windows
    };
    const invalidWindowIndex = windows.findIndex(window =>
        !window.days.length || !window.start || !window.end || window.start === window.end
    );
    if (invalidWindowIndex >= 0) {
        showToast(
            `Window ${invalidWindowIndex + 1} needs a weekday and different start/end times`,
            'warning'
        );
        return;
    }
    button.disabled = true;
    try {
        const response = await fetch('api/schedule', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Failed to save working hours');
        showToast('Working hours saved', 'success');
        await loadSchedule();
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        button.disabled = false;
    }
}

async function loadOperations() {
    const projectId = state.currentProjectId;
    if (!projectId) return;
    const preflightEl = document.getElementById('preflight-details');
    const storageEl = document.getElementById('storage-details');
    const cleanupEl = document.getElementById('cleanup-preview');
    const stateEl = document.getElementById('operations-state');
    stateEl.textContent = 'Checking';
    stateEl.className = 'schedule-state';
    try {
        const [preflightResponse, storageResponse] = await Promise.all([
            fetch('api/system/preflight'),
            fetch(`api/projects/${encodeURIComponent(projectId)}/storage`)
        ]);
        if (!preflightResponse.ok || !storageResponse.ok) throw new Error('Operations check failed');
        const preflight = await preflightResponse.json();
        const storage = await storageResponse.json();
        const runtime = preflight.effective_runtime || {};
        const notices = [...(preflight.errors || []), ...(preflight.warnings || [])];
        preflightEl.innerHTML = [
            ['Status', preflight.compatible ? 'Compatible' : 'Action required'],
            ['TTS', `${runtime.tts_model || 'unknown'} · ${runtime.attention_backend || 'default'}`],
            ['Validator', `${runtime.whisper_backend || 'unknown'} · ${runtime.whisper_model || 'unknown'} · VAD ${runtime.whisper_vad_filter ? 'on' : 'off'}`],
            ['FFmpeg', preflight.executables?.ffmpeg ? 'Available' : 'Missing'],
            ['Notices', notices.length ? notices.join(' · ') : 'None']
        ].map(([label, value]) => `<div class="operations-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>`).join('');
        storageEl.innerHTML = Object.entries(storage.categories || {}).map(([name, item]) =>
            `<div class="operations-row"><span>${escapeHtml(name.replaceAll('_', ' '))}</span><strong>${formatBytes(item.bytes || 0)} · ${item.files || 0} file${item.files === 1 ? '' : 's'}</strong></div>`
        ).join('') + `<div class="operations-row"><span>Total</span><strong>${formatBytes(storage.total_bytes || 0)}</strong></div>`;
        const preview = storage.cleanup_preview || {};
        cleanupEl.innerHTML = `<span>Safe cleanup preview: ${preview.files?.length || 0} temporary files · ${formatBytes(preview.bytes || 0)}</span>`;
        if (preview.files?.length) {
            const button = document.createElement('button');
            button.className = 'btn btn-danger btn-sm';
            button.textContent = 'Remove previewed temp files';
            button.addEventListener('click', () => runPreviewedCleanup(preview.confirmation_token));
            cleanupEl.appendChild(button);
        }
        stateEl.textContent = preflight.compatible ? 'Healthy' : 'Review';
        stateEl.className = `schedule-state ${preflight.compatible ? 'open' : 'closed'}`;
        document.getElementById('operations-summary').textContent = `${formatBytes(storage.total_bytes || 0)} · ${notices.length} runtime notice${notices.length === 1 ? '' : 's'}`;
    } catch (error) {
        stateEl.textContent = 'Unavailable';
        stateEl.className = 'schedule-state closed';
        preflightEl.textContent = error.message;
        storageEl.textContent = 'Could not load storage details.';
    }
}

async function runPreviewedCleanup(confirmationToken) {
    if (!state.currentProjectId || !confirm('Remove only the temporary files shown in the current preview?')) return;
    try {
        const response = await fetch(`api/projects/${encodeURIComponent(state.currentProjectId)}/storage/cleanup`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({confirmation_token: confirmationToken})
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Cleanup failed');
        showToast(`Removed ${data.removed?.length || 0} temporary files (${formatBytes(data.removed_bytes || 0)})`, 'success');
        await loadOperations();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function restartDashboard() {
    const button = document.getElementById('btn-restart-dashboard');
    if (!confirm('Restart the audiobook dashboard now? Active work will stop at the safest available point and can be resumed afterward.')) return;
    button.disabled = true;
    button.textContent = 'Restarting…';
    try {
        const response = await fetch('api/system/restart', {method: 'POST'});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Dashboard restart could not be started');
        showToast('Dashboard restart started. This page will reconnect automatically.', 'info');

        let observedOffline = false;
        const deadline = Date.now() + 120000;
        while (Date.now() < deadline) {
            await new Promise(resolve => setTimeout(resolve, 1500));
            try {
                const health = await fetch(`api/system/preflight?restart_probe=${Date.now()}`, {
                    cache: 'no-store'
                });
                if (observedOffline && health.ok) {
                    window.location.reload();
                    return;
                }
            } catch (_) {
                observedOffline = true;
            }
        }
        throw new Error('Restart was requested, but the dashboard did not reconnect within two minutes');
    } catch (error) {
        showToast(error.message, 'error');
        button.disabled = false;
        button.textContent = 'Restart dashboard';
    }
}

// ============================================================================
// Utilities
// ============================================================================

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    
    let icon = 'ℹ️';
    if (type === 'success') icon = '✅';
    if (type === 'error') icon = '❌';
    if (type === 'warning') icon = '⚠️';
    
    toast.innerHTML = `<span>${icon}</span> <span>${escapeHtml(message)}</span>`;
    
    els.toastContainer.appendChild(toast);
    
    setTimeout(() => {
        toast.classList.add('toast-exit');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function formatBytes(bytes, decimals = 2) {
    if (!+bytes) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

function formatDate(isoString) {
    if (!isoString) return '';
    const date = new Date(isoString);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function formatProjectStatus(status) {
    const labels = {
        created: 'Ready to configure',
        selection_complete: 'Selected batch complete',
        voice_review: 'Voice approval required',
        paused_scheduled: 'Waiting for working hours',
        deploy_paused: 'Parked safely',
        waiting_for_review: 'Waiting for review',
        complete: 'Complete',
        completed: 'Complete'
    };
    const token = String(status || 'created').toLowerCase();
    return labels[token] || token.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

function escapeHtml(unsafe) {
    if (!unsafe) return '';
    return unsafe
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
}

// ============================================================================
// WebSocket & Health Checks
// ============================================================================

function connectWebSocket() {
    const wsUrl = new URL('ws/updates', window.location.href);
    wsUrl.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    state.ws = new WebSocket(wsUrl.href);
    
    state.ws.onopen = () => {
        console.log('WebSocket connected');
    };
    
    state.ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWsMessage(data);
        } catch (e) {
            console.error('Failed to parse WS message:', e);
        }
    };
    
    state.ws.onclose = () => {
        console.log('WebSocket disconnected. Reconnecting in 3s...');
        setTimeout(connectWebSocket, 3000);
    };
    
    state.ws.onerror = (err) => {
        console.error('WebSocket error:', err);
    };
}

function handleWsMessage(data) {
    // Refresh project details if we are viewing the updated project
    if (data.project_id && state.currentProjectId === data.project_id) {
        if (data.type === 'status_update' && data.status) {
            renderProjectHeader(data.status);
        } else if (data.type === 'progress' || data.type === 'stage_change') {
            fetchProjectDetails(state.currentProjectId);
            
            // Show live progress line
            if (data.type === 'progress' && window.PipelineManager) {
                window.PipelineManager.updateLiveProgress(data);
            }

            // Auto-connect log console if the Logs tab is active
            if (window.LogConsole) {
                window.LogConsole.openForProject(data.project_id);
            }
        } else if (data.type === 'error') {
            showToast(data.message || 'Pipeline error occurred', 'error');
            fetchProjectDetails(state.currentProjectId);
        }
    }
}

// Just a visual check for the top right dot
async function checkVoiceServerStatus() {
    els.voiceStatusDot.className = 'status-dot checking';
    els.voiceStatusText.textContent = 'Voice Server: Checking...';

    try {
        const response = await fetch('api/voice/health');
        const health = await response.json();
        state.voiceServerOnline = Boolean(health.online);
        els.voiceStatusDot.className = health.online
            ? 'status-dot online'
            : 'status-dot offline';
        els.voiceStatusText.textContent = health.online
            ? `Voice Server: Online${health.model ? ` · ${health.model.split('/').pop()}` : ''}`
            : 'Voice Server: Offline (starts on demand)';
    } catch {
        state.voiceServerOnline = false;
        els.voiceStatusDot.className = 'status-dot offline';
        els.voiceStatusText.textContent = 'Voice Server: Unavailable';
    }
}



let currentDeliverySettings = { enabled: false, batch_size: 5 };

async function fetchAndRenderDeliveries(projectId) {

    try {
        const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/deliveries`);
        if (!response.ok) throw new Error('Could not load audiobook parts');
        const data = await response.json();
        if (state.currentProjectId !== projectId) return;

        const summary = document.getElementById('deliveries-summary');
        const list = document.getElementById('deliveries-list');
        list.innerHTML = '';

        // Update dashboard settings UI
        currentDeliverySettings = data.settings || {};
        const incToggle = document.getElementById('dashboard-enable-incremental');
        const batchInput = document.getElementById('dashboard-delivery-batch-size');
        const opts = document.getElementById('dashboard-incremental-options');
        const saveBtn = document.getElementById('dashboard-save-delivery');
        const settingsLocked = Boolean(state.currentProject?.running) ||
            Boolean(data.deliveries?.length);
        incToggle.disabled = settingsLocked;
        batchInput.disabled = settingsLocked;
        incToggle.title = settingsLocked
            ? 'Stop the pipeline and reset published parts before changing delivery boundaries'
            : '';

        // Only update UI elements if the user hasn't made unsaved changes
        if (saveBtn.style.display === 'none' || !saveBtn.style.display) {
            incToggle.checked = currentDeliverySettings.enabled || false;
            batchInput.value = currentDeliverySettings.batch_size || 5;

            if (incToggle.checked) {
                opts.style.display = 'flex';
            } else {
                opts.style.display = 'none';
            }
        }

        if (data.active_delivery_id) {
            const active = document.createElement('div');
            active.className = 'delivery-active';
            active.textContent = `Preparing ${data.active_delivery_id} (chapters ${(data.active_delivery_chapters || []).join(', ')})`;
            list.appendChild(active);
            if (!data.pause_after_delivery_requested) {
                const pauseButton = document.createElement('button');
                pauseButton.className = 'btn btn-secondary btn-sm';
                pauseButton.textContent = 'Pause after this part';
                pauseButton.addEventListener('click', async () => {
                    try {
                        const pauseResponse = await fetch(
                            `api/projects/${encodeURIComponent(projectId)}/pause-after-delivery`,
                            {method: 'POST'}
                        );
                        const pauseData = await pauseResponse.json().catch(() => ({}));
                        if (!pauseResponse.ok) throw new Error(pauseData.detail || 'Pause request failed');
                        await fetchAndRenderDeliveries(projectId);
                    } catch (error) {
                        showToast(error.message, 'error');
                    }
                });
                active.appendChild(pauseButton);
            } else {
                active.append(' — pause queued');
            }
        }

        if (!data.deliveries || data.deliveries.length === 0) {
            summary.textContent = 'No parts published yet';
            if (!data.active_delivery_id) {
                list.innerHTML = '<div style="color: var(--text-secondary);">No parts published yet. If incremental delivery is enabled, parts will appear here as they complete.</div>';
            }
            return;
        }

        summary.textContent = `${data.published_count} parts available`;


        data.deliveries.forEach((d, index) => {
            const row = document.createElement('div');
            row.style.display = 'flex';
            row.style.justifyContent = 'space-between';
            row.style.alignItems = 'center';
            row.style.padding = '10px';
            row.style.background = 'var(--bg-surface-secondary)';
            row.style.borderRadius = 'var(--radius-sm)';

            const info = document.createElement('div');
            const statusLabel = d.status === 'stale' ? ' — needs republishing' : '';
            info.innerHTML = `<strong>Part ${d.ordinal || index + 1}</strong> <span style="color: var(--text-secondary); font-size: 0.9em; margin-left: 10px;">(Chapters ${d.chapter_numbers.join(', ')})${statusLabel}</span>`;

            const actions = document.createElement('div');
            const downloadBtn = document.createElement('a');
            downloadBtn.href = `api/projects/${encodeURIComponent(projectId)}/deliveries/${encodeURIComponent(d.delivery_id)}/download`;
            downloadBtn.className = 'btn btn-primary btn-sm';
            downloadBtn.dataset.serverDownload = '';
            downloadBtn.textContent = '⬇ Download';

            if (d.status === 'published') {
                actions.appendChild(downloadBtn);
            }
            row.appendChild(info);
            row.appendChild(actions);
            list.appendChild(row);
        });

    } catch (e) {
        console.error("Failed to fetch deliveries", e);
        showToast(e.message, 'error');
    }
}

window.toggleIncrementalSettings = function(checked) {
    const opts = document.getElementById('dashboard-incremental-options');
    if (checked) {
        opts.style.display = 'flex';
    } else {
        opts.style.display = 'none';
    }
    window.checkIncrementalSettings();
};

window.checkIncrementalSettings = function() {
    const incToggle = document.getElementById('dashboard-enable-incremental');
    const batchInput = document.getElementById('dashboard-delivery-batch-size');
    const saveBtn = document.getElementById('dashboard-save-delivery');

    const changed = (incToggle.checked !== (currentDeliverySettings.enabled || false)) ||
                   (parseInt(batchInput.value, 10) !== (currentDeliverySettings.batch_size || 5));

    if (changed) {
        saveBtn.style.display = 'inline-block';
    } else {
        saveBtn.style.display = 'none';
    }
};

window.saveIncrementalSettings = async function() {
    const incToggle = document.getElementById('dashboard-enable-incremental');
    const batchInput = document.getElementById('dashboard-delivery-batch-size');
    const saveBtn = document.getElementById('dashboard-save-delivery');

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
        const projectId = state.currentProjectId;
        const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/delivery-settings`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled: incToggle.checked,
                batch_size: parseInt(batchInput.value, 10)
            })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Could not save delivery settings');
        if (state.currentProjectId !== projectId) return;
        currentDeliverySettings = { enabled: incToggle.checked, batch_size: parseInt(batchInput.value, 10) };
        saveBtn.style.display = 'none';
    } catch (e) {
        showToast(e.message, 'error');
    }
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save Settings';
};
