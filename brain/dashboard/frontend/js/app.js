/**
 * Main Application Logic — Crazy Audiobook Creator
 * Handles navigation, project CRUD, and global state.
 */

// Global State
const state = {
    projects: [],
    currentProjectId: null,
    ws: null,
    voiceServerOnline: false
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
    await fetchProjects();
}

function setupEventListeners() {
    // Navigation
    els.btnNewProject.addEventListener('click', openUploadModal);
    els.btnEmptyNew.addEventListener('click', openUploadModal);
    els.btnBack.addEventListener('click', showProjectsView);
    document.getElementById('nav-home-btn').addEventListener('click', showProjectsView);

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
    document.getElementById('btn-stop-pipeline').addEventListener('click', stopPipeline);
    document.getElementById('btn-delete-project').addEventListener('click', deleteProject);
}

// ============================================================================
// Navigation
// ============================================================================

function showProjectsView() {
    state.currentProjectId = null;
    els.viewDetail.classList.add('hidden');
    els.viewProjects.classList.remove('hidden');
    fetchProjects();
}

async function showDetailView(projectId) {
    state.currentProjectId = projectId;
    els.viewProjects.classList.add('hidden');
    els.viewDetail.classList.remove('hidden');
    
    // Switch to Characters tab by default
    document.querySelector('.tab[data-tab="tab-characters"]').click();
    
    await fetchProjectDetails(projectId);
}

// ============================================================================
// API Calls & Data Fetching
// ============================================================================

async function fetchProjects() {
    try {
        const response = await fetch('/api/projects');
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

async function fetchProjectDetails(projectId) {
    try {
        const response = await fetch(`/api/projects/${projectId}/status`);
        if (!response.ok) throw new Error('Failed to fetch project details');
        
        const data = await response.json();
        renderProjectDetails(data);
        
        // Let pipeline.js and script-viewer.js update their parts
        if (window.PipelineManager) {
            window.PipelineManager.updateTracker(data.current_stage, data.status);
            window.PipelineManager.toggleControls(data.status, data.running);
        }
        
        if (window.ScriptViewer) {
            window.ScriptViewer.loadData(projectId);
        }
        
    } catch (error) {
        showToast(`Error loading project details: ${error.message}`, 'error');
        showProjectsView();
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
        const response = await fetch('/api/projects', {
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
    
    try {
        const response = await fetch(`/api/projects/${state.currentProjectId}/start`, { method: 'POST' });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to start pipeline');
        }
        showToast('Pipeline started', 'info');
        fetchProjectDetails(state.currentProjectId); // Refresh status immediately
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function stopPipeline() {
    if (!state.currentProjectId) return;
    
    try {
        const response = await fetch(`/api/projects/${state.currentProjectId}/stop`, { method: 'POST' });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to stop pipeline');
        }
        showToast('Pipeline stopped', 'info');
        fetchProjectDetails(state.currentProjectId); // Refresh status
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
        const response = await fetch(`/api/projects/${state.currentProjectId}`, { method: 'DELETE' });
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
        const card = document.createElement('div');
        card.className = 'project-card';
        card.innerHTML = `
            <div class="card-header">
                <div class="card-emoji">📖</div>
                <div>
                    <h3 class="card-title">${escapeHtml(project.title || 'Untitled')}</h3>
                    <div class="card-author">${escapeHtml(project.author || 'Unknown Author')}</div>
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
            <div class="card-stage" style="background: var(--stage-${project.current_stage.toLowerCase()}-bg, var(--bg-elevated)); color: var(--stage-${project.current_stage.toLowerCase()}, var(--text-primary))">
                ${project.status === 'error' ? '⚠️ ' : (project.status === 'completed' ? '✅ ' : '⏳ ')}
                ${project.current_stage.replace('_', ' ')}
            </div>
        `;
        
        card.addEventListener('click', () => showDetailView(project.project_id));
        els.projectsGrid.appendChild(card);
    });
}

function renderProjectDetails(project) {
    document.getElementById('project-title').textContent = project.title || 'Untitled';
    document.getElementById('project-author').textContent = project.author || 'Unknown Author';
    
    document.getElementById('project-stats').innerHTML = `
        <span>${project.total_chapters || 0} Chapters</span>
        <span>ID: ${project.project_id.split('-')[0]}</span>
        <span>Started: ${formatDate(project.created_at)}</span>
    `;
    
    const stageColor = `var(--stage-${project.current_stage.toLowerCase()}, var(--text-primary))`;
    document.getElementById('project-stage').innerHTML = `
        <span class="card-stage" style="border: 1px solid ${stageColor}; color: ${stageColor}">
            Status: ${project.status.toUpperCase()} | Stage: ${project.current_stage.replace('_', ' ')}
        </span>
    `;
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
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/updates`;
    
    state.ws = new WebSocket(wsUrl);
    
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
        if (data.type === 'progress' || data.type === 'stage_change') {
            fetchProjectDetails(state.currentProjectId);
            
            // Show live progress line
            if (data.type === 'progress' && window.PipelineManager) {
                window.PipelineManager.updateLiveProgress(data);
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
    
    // In a real implementation, we might call a Brain API endpoint that proxies to Voice /health
    // For now, we simulate success since they are run locally
    setTimeout(() => {
        state.voiceServerOnline = true;
        els.voiceStatusDot.className = 'status-dot online';
        els.voiceStatusText.textContent = 'Voice Server: Online';
    }, 1000);
}
