/**
 * Main Application Logic — Crazy Audiobook Creator
 * Handles navigation, project CRUD, and global state.
 */

// Global State
const state = window.state = {
    projects: [],
    currentProjectId: null,
    ws: null,
    voiceServerOnline: false,
    schedule: null,
    lastScheduleRefresh: 0
};

// DOM Elements
const els = {
    viewProjects: document.getElementById('view-projects'),
    viewDetail: document.getElementById('view-detail'),
    projectsGrid: document.getElementById('projects-grid'),
    projectsEmpty: document.getElementById('projects-empty'),
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
    uploadInfo: document.getElementById('upload-info'),
    uploadFileName: document.getElementById('upload-file-name'),
    uploadFileSize: document.getElementById('upload-file-size'),
    uploadRemove: document.getElementById('upload-remove'),
    btnUpload: document.getElementById('modal-upload'),
    uploadProgress: document.getElementById('upload-progress'),
    uploadProgressFill: document.getElementById('upload-progress-fill'),
    uploadProgressText: document.getElementById('upload-progress-text'),
    toastContainer: document.getElementById('toast-container'),
    voiceStatusDot: document.getElementById('voice-status-dot'),
    voiceStatusText: document.getElementById('voice-status-text')
};

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
    if (hash && hash.startsWith('project/')) {
        const projectId = hash.replace('project/', '');
        showDetailView(projectId, true);
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
    
    const STAGE_TOOLTIPS = {
        extracting: 'Re-extract text from EPUB file (clears all script, cast, and audio files)',
        scripting: 'Re-run LLM script & character extraction (clears script JSONs and cast, keeps EPUB text)',
        bootstrapping: 'Re-generate voice design profiles and reference audio (keeps script intact)',
        voice_review: 'Re-open the Voice Review banner to change speaking voices or tweak cast without re-scripting',
        generating: 'Re-generate chapter audio segments (clears audio segments, keeps script and voice cast)',
        validating: 'Re-run Whisper Speech-to-Text quality validation checks',
        mastering: 'Re-run ffmpeg/sox chapter audio mastering (clears mastered audio and M4B)',
        exporting: 'Re-run M4B audiobook packaging over mastered chapter files'
    };

    // Reset and Download features
    els.selectResetStage.addEventListener('change', () => {
        const val = els.selectResetStage.value;
        if (val) {
            els.btnResetStage.classList.remove('hidden');
            const desc = STAGE_TOOLTIPS[val] || '';
            els.selectResetStage.title = `${val.toUpperCase()}: ${desc}`;
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
        window.location.href = `api/projects/${state.currentProjectId}/download`;
    });

    // Modal
    els.modalClose.addEventListener('click', closeUploadModal);
    els.modalCancel.addEventListener('click', closeUploadModal);
    
    // Drag and Drop Upload
    els.uploadZone.addEventListener('click', () => els.epubInput.click());
    els.uploadZone.addEventListener('dragover', handleDragOver);
    els.uploadZone.addEventListener('dragleave', handleDragLeave);
    els.uploadZone.addEventListener('drop', handleDrop);
    els.epubInput.addEventListener('change', handleFileSelect);
    els.uploadRemove.addEventListener('click', clearUpload);
    els.btnUpload.addEventListener('click', handleUploadSubmit);

    // Tabs
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            const targetId = e.target.dataset.tab;
            
            // Update buttons
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            e.target.classList.add('active');
            
            // Update content
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById(targetId).classList.add('active');
        });
    });

    // Detail Actions
    document.getElementById('btn-start-pipeline').addEventListener('click', startPipeline);
    document.getElementById('btn-pause-pipeline').addEventListener('click', pausePipeline);
    document.getElementById('btn-delete-project').addEventListener('click', deleteProject);

    // Feature Expansion Handlers
    const btnFetchMeta = document.getElementById('btn-fetch-metadata');
    if (btnFetchMeta) {
        btnFetchMeta.addEventListener('click', async () => {
            if (!state.currentProjectId) return;
            showToast('Fetching artwork and info...', 'info');
            try {
                const res = await fetch(`api/projects/${state.currentProjectId}/fetch-metadata`, { method: 'POST' });
                if (!res.ok) throw new Error('Metadata fetch failed');
                const data = await res.json();
                showToast('Artwork & metadata updated!', 'success');
                fetchProjectDetails(state.currentProjectId);
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
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

    document.getElementById('chapter-search-input')?.addEventListener('input', filterChapterRows);
    document.getElementById('chapter-status-filter')?.addEventListener('change', filterChapterRows);
    document.getElementById('btn-add-schedule-window')?.addEventListener('click', () => {
        addScheduleWindow({
            days: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
            start: '09:00',
            end: '17:00'
        });
    });
    document.getElementById('btn-save-schedule')?.addEventListener('click', saveSchedule);
}

// ============================================================================
// Navigation
// ============================================================================

let detailPollTimer = null;

function showProjectsView(isHashLoad = false) {
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
    if (!isHashLoad) {
        window.history.pushState(null, '', `#project/${projectId}`);
    }
    state.currentProjectId = projectId;
    els.viewProjects.classList.add('hidden');
    els.viewDetail.classList.remove('hidden');
    
    // Switch to Characters tab by default
    document.querySelector('.tab[data-tab="tab-characters"]').click();
    
    await fetchProjectDetails(projectId);

    if (detailPollTimer) clearInterval(detailPollTimer);
    detailPollTimer = setInterval(() => {
        if (state.currentProjectId) {
            fetchProjectDetails(state.currentProjectId, true);
        }
    }, 2000);

    // Connect log console in background (non-blocking)
    if (window.LogConsole) {
        window.LogConsole.openForProject(projectId);
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
        if (logsResponse?.ok) {
            const logData = await logsResponse.json();
            data.work_progress = deriveWorkProgress(
                logData.lines || [],
                data.total_chapters || 0
            );
        }
        renderProjectDetails(data);

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
            
            window.PipelineManager.updateTracker(data.active_stage || data.status, isRunning ? 'running' : coarseStatus, data);
            window.PipelineManager.toggleControls(data.status, isRunning, data);
        }
        
        if (window.ScriptViewer && !isPoll) {
            window.ScriptViewer.loadData(projectId);
        }
        
    } catch (error) {
        if (!isPoll) {
            showToast(`Error loading project details: ${error.message}`, 'error');
            showProjectsView();
        }
    }
}

// ============================================================================
// Upload Modal & Logic
// ============================================================================

let currentFile = null;

function openUploadModal() {
    clearUpload();
    els.uploadModal.classList.remove('hidden');
}

function closeUploadModal() {
    els.uploadModal.classList.add('hidden');
    clearUpload();
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
    
    // Simulate progress bar (actual progress requires XHR, using fetch for simplicity here)
    let progress = 0;
    const progressInterval = setInterval(() => {
        progress += Math.random() * 10;
        if (progress > 90) progress = 90;
        els.uploadProgressFill.style.width = `${progress}%`;
    }, 500);

    const formData = new FormData();
    formData.append('file', currentFile);
    // You could also add title/author inputs to the modal and append them here

    try {
        const response = await fetch('api/projects', {
            method: 'POST',
            body: formData
        });
        
        clearInterval(progressInterval);
        els.uploadProgressFill.style.width = '100%';
        
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Upload failed');
        }
        
        const data = await response.json();
        showToast('Project created successfully', 'success');
        closeUploadModal();
        await showDetailView(data.project_id);
        
    } catch (error) {
        clearInterval(progressInterval);
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
    
    state.projects.forEach(project => {
        const status = String(project.status || 'created').toLowerCase();
        const statusToken = status.replace(/[^a-z_]/g, '');
        const card = document.createElement('div');
        card.className = 'project-card';
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
            <div class="card-stage" style="background: var(--stage-${statusToken}-bg, var(--bg-elevated)); color: var(--stage-${statusToken}, var(--text-primary))">
                ${['error', 'paused', 'complete'].includes(status) ? (status === 'complete' ? '✅ ' : '⚠️ ') : '⏳ '}
                ${escapeHtml(status.replaceAll('_', ' '))}
            </div>
        `;
        
        card.addEventListener('click', () => showDetailView(project.project_id));
        els.projectsGrid.appendChild(card);
    });
}

function renderProjectDetails(project) {
    renderProjectHeader(project);
    renderChapterList(project);
}

function renderProjectHeader(project) {
    document.getElementById('project-title').textContent = (project.title && project.title !== 'Unknown') ? project.title : 'Untitled';
    document.getElementById('project-author').textContent = (project.author && project.author !== 'Unknown') ? project.author : 'Unknown Author';
    
    const status = String(project.status || 'created').toLowerCase();
    const statusToken = status.replace(/[^a-z_]/g, '');
    document.getElementById('project-stats').innerHTML = `
        <span>${project.total_chapters || 0} Chapters</span>
        <span>ID: ${escapeHtml(String(project.project_id || ''))}</span>
        <span>Started: ${formatDate(project.created_at)}</span>
    `;
    
    const stageColor = `var(--stage-${statusToken}, var(--text-primary))`;
    const coarseStatus = project.running === true ? 'running' : status;
    const displayStatus = coarseStatus.replaceAll('_', ' ');
    document.getElementById('project-stage').innerHTML = `
        <span class="card-stage" style="border: 1px solid ${stageColor}; color: ${stageColor}">
            Status: ${displayStatus.toUpperCase()} | Stage: ${escapeHtml(status.replaceAll('_', ' ').toUpperCase())}
        </span>
    `;
}

function renderChapterList(project) {
    const grid = document.getElementById('chapter-grid');
    if (!grid) return;
    grid.innerHTML = '';

    const total = project.total_chapters || 0;
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
            download = `<a class="chapter-download" href="api/projects/${encodeURIComponent(project.project_id)}/download/chapter/${chapter}" target="_blank" title="Download mastered chapter WAV">↓</a>`;
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
        } else if (stage.includes('script') && (currentScript === chapter || (!currentScript && chapter === scripted.size + 1 && chapter <= total))) {
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

function filterChapterRows() {
    const search = (document.getElementById('chapter-search-input')?.value || '')
        .trim().toLowerCase();
    const status = document.getElementById('chapter-status-filter')?.value || 'all';
    document.querySelectorAll('.chapter-cell').forEach(row => {
        const titleMatch = !search || row.dataset.title.includes(search);
        const statusMatch = status === 'all' || row.dataset.status === status;
        row.hidden = !(titleMatch && statusMatch);
    });
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
    const stage = String(project.active_stage || project.status || 'created').toLowerCase();
    const chapterTitle = currentDetail.title || (
        currentChapter ? `Chapter ${currentChapter}` : ''
    );
    const workProgress = project.work_progress || {};
    const selectedNames = selected.map(chapter => {
        const title = detailMap.get(chapter)?.title || `Chapter ${chapter}`;
        return `Chapter ${chapter} — ${title}`;
    });
    const batchDescription = selectedNames.length <= 3
        ? selectedNames.join(', ')
        : `${selectedNames.length} selected chapters`;

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
        overallLabel = 'Scripting stage';
        chapterMetric = workProgress.position || '—';
        chapterLabel = workProgress.phase === 'character_analysis'
            ? 'Analysis unit'
            : 'Book chapter';
        lineMetric = workProgress.tokens ? `${workProgress.tokens}` : '—';
        lineLabel = workProgress.tokens ? 'Current response tokens' : 'LLM response';
        activity = workProgress.current || (
            chapterTitle ? `Scripting — ${chapterTitle}` : 'Analyzing and scripting the full book'
        );
        description = `${workProgress.detail || `${(project.scripted_chapters || []).length} of ${project.total_chapters || 0} chapters scripted.`} Audio generation is queued for: ${batchDescription}.`;
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
        description = project.error ||
            'Inspect the logs, then resume after correcting the issue.';
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
            downloadBtn = `<a href="api/projects/${encodeURIComponent(project.project_id)}/download/chapter/${i}" target="_blank" title="Download Mastered Chapter WAV" style="color: #34d399; text-decoration: none; font-size: 1.1em; margin-left: 6px; transition: transform 0.2s ease;">⬇</a>`;
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
        } else if (isScriptingStage && (currentScript === i || (!currentScript && i === (scripted.size + 1)))) {
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
        tokens: null,
        chapterIndex: null,
        chapterTotal: null,
        chapterTitle: null
    };

    for (const line of lines) {
        let match = line.match(/Analyzing unit\s+(\d+)\/(\d+):\s*(.+?)(?:\.\.\.)?$/i);
        if (match) {
            const current = Number(match[1]);
            const total = Number(match[2]);
            progress.phase = 'character_analysis';
            progress.stagePercent = Math.round(20 * current / Math.max(total, 1));
            progress.current = `Character analysis — unit ${current} of ${total}`;
            progress.detail = match[3].replace(/\.\.\.$/, '');
            progress.position = `${current} / ${total}`;
            progress.tokens = null;
            continue;
        }

        match = line.match(/\[ScriptGenerator\].*Chapter\s+(\d+)\/(\d+):\s*['"](.+?)['"]/i);
        if (match) {
            const current = Number(match[1]);
            const total = Number(match[2]) || totalChapters;
            const title = match[3];
            progress.phase = 'chapter_scripting';
            progress.stagePercent = 20 + Math.round(80 * (current - 1) / Math.max(total, 1));
            progress.current = `Scripting — chapter ${current} of ${total}: ${title}`;
            progress.detail = `Generating the production script for ${title}.`;
            progress.position = `${current} / ${total}`;
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
            progress.detail = `Processing fragment chunk ${chunk} of ${chunks} for ${progress.chapterTitle}.`;
            progress.tokens = null;
            continue;
        }

        match = line.match(/\[ScriptGenerator\].*Chapter\s+(\d+)\/(\d+)\s+done/i);
        if (match) {
            const current = Number(match[1]);
            const total = Number(match[2]) || totalChapters;
            progress.phase = 'chapter_scripting';
            progress.stagePercent = 20 + Math.round(80 * current / Math.max(total, 1));
            progress.current = `Scripting — chapter ${current} of ${total} complete`;
            progress.detail = `${current} of ${total} chapter scripts complete.`;
            progress.position = `${current} / ${total}`;
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
        if (selected.length === 0) {
            showToast('No chapters selected. Choose one or more before starting.', 'warning');
            return;
        }
        try {
            await saveChapterSelection(targetProjectId, selectionValue);
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
        <span>to</span>
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
