/**
 * Script Viewer and Data Manager
 * Handles Characters, Script, and Quality tabs.
 */

window.ScriptViewer = (() => {
    let currentData = {
        characters: null,
        script: null,
        quality: null
    };

    const els = {
        charGrid: document.getElementById('character-grid'),
        scriptViewer: document.getElementById('script-viewer'),
        chapterSelect: document.getElementById('script-chapter-select'),
        scriptLegend: document.getElementById('script-legend'),
        qualityOverview: document.getElementById('quality-overview')
    };

    // Called by app.js when opening a project
    async function loadData(projectId) {
        els.charGrid.innerHTML = '<div class="empty-state small"><p>Loading...</p></div>';
        els.scriptViewer.innerHTML = '<div class="empty-state small"><p>Loading...</p></div>';
        els.qualityOverview.innerHTML = '<div class="empty-state small"><p>Loading...</p></div>';
        
        await Promise.allSettled([
            fetchCharacters(projectId),
            fetchScript(projectId),
            fetchQuality(projectId)
        ]);
        
        renderCharacters();
        renderScriptDropdown();
        renderQuality();
    }

    // ============================================================================
    // Fetching
    // ============================================================================

    async function fetchCharacters(projectId) {
        try {
            const res = await fetch(`/api/projects/${projectId}/characters`);
            if (res.ok) {
                currentData.characters = await res.json();
            } else {
                currentData.characters = null;
            }
        } catch (e) {
            currentData.characters = null;
        }
    }

    async function fetchScript(projectId) {
        try {
            const res = await fetch(`/api/projects/${projectId}/script`);
            if (res.ok) {
                currentData.script = await res.json();
            } else {
                currentData.script = null;
            }
        } catch (e) {
            currentData.script = null;
        }
    }

    async function fetchQuality(projectId) {
        try {
            const res = await fetch(`/api/projects/${projectId}/quality`);
            if (res.ok) {
                currentData.quality = await res.json();
            } else {
                currentData.quality = null;
            }
        } catch (e) {
            currentData.quality = null;
        }
    }

    // ============================================================================
    // Characters Tab
    // ============================================================================

    function renderCharacters() {
        if (!currentData.characters || Object.keys(currentData.characters).length === 0) {
            els.charGrid.innerHTML = '<div class="empty-state small"><p>Characters will appear after the LLM analysis completes (Pass 1).</p></div>';
            return;
        }

        els.charGrid.innerHTML = '';
        
        // Convert to array and sort (Narrator usually first if we identify it, or by mention count)
        // The API might return { book_title: "...", characters: { ... } } or just the characters dict.
        const charDict = currentData.characters.characters || currentData.characters;
        const chars = Object.entries(charDict).map(([id, data]) => ({ id, ...data }));
        
        chars.forEach((char, idx) => {
            // Assign a color based on index
            const colorVar = char.id.toLowerCase() === 'narrator' ? 'var(--speaker-narrator)' : `var(--speaker-${(idx % 10) + 1})`;
            
            const initials = char.name ? char.name.substring(0, 2).toUpperCase() : '??';
            
            const card = document.createElement('div');
            card.className = 'character-card';
            card.style.setProperty('--char-color', colorVar);
            
            let traitsHtml = '';
            if (char.traits && char.traits.length > 0) {
                traitsHtml = `<div class="char-traits">` + 
                    char.traits.slice(0, 4).map(t => `<span class="trait-tag">${escapeHtml(t)}</span>`).join('') +
                `</div>`;
            }
            
            card.innerHTML = `
                <div class="char-header">
                    <div class="char-avatar" style="background: ${colorVar}">${initials}</div>
                    <div>
                        <div class="char-name">${escapeHtml(char.name)}</div>
                        <div class="char-meta">${escapeHtml(char.gender || 'Unknown')} • ${escapeHtml(char.age || 'Unknown Age')}</div>
                    </div>
                </div>
                ${traitsHtml}
                <div class="char-voice">
                    <strong>Voice:</strong> ${escapeHtml(char.voice_description || 'No description yet.')}
                </div>
            `;
            
            els.charGrid.appendChild(card);
        });
    }

    // ============================================================================
    // Script Tab
    // ============================================================================

    function renderScriptDropdown() {
        els.chapterSelect.innerHTML = '<option value="">Select Chapter</option>';
        els.scriptLegend.innerHTML = '';
        
        if (!currentData.script || !currentData.script.chapters || currentData.script.chapters.length === 0) {
            els.scriptViewer.innerHTML = '<div class="empty-state small"><p>Script will appear after LLM generation completes (Pass 2).</p></div>';
            els.chapterSelect.disabled = true;
            return;
        }

        els.chapterSelect.disabled = false;
        
        currentData.script.chapters.forEach((ch, idx) => {
            const opt = document.createElement('option');
            opt.value = idx;
            opt.textContent = ch.title || `Chapter ${idx + 1}`;
            els.chapterSelect.appendChild(opt);
        });
        
        // Build legend based on characters found
        if (currentData.characters) {
            const charDict = currentData.characters.characters || currentData.characters;
            const chars = Object.entries(charDict);
            chars.forEach(([id, data], idx) => {
                const colorVar = id.toLowerCase() === 'narrator' ? 'var(--speaker-narrator)' : `var(--speaker-${(idx % 10) + 1})`;
                els.scriptLegend.innerHTML += `
                    <div class="legend-item">
                        <div class="legend-dot" style="background: ${colorVar}"></div>
                        <span>${escapeHtml(data.name || id)}</span>
                    </div>
                `;
            });
        }
        
        // Select first chapter by default
        els.chapterSelect.value = 0;
        renderScriptLines(0);
        
        els.chapterSelect.addEventListener('change', (e) => {
            if (e.target.value !== "") {
                renderScriptLines(parseInt(e.target.value));
            }
        });
    }

    function renderScriptLines(chapterIndex) {
        if (!currentData.script || !currentData.script.chapters[chapterIndex]) return;
        
        const lines = currentData.script.chapters[chapterIndex].lines || [];
        els.scriptViewer.innerHTML = '';
        
        if (lines.length === 0) {
            els.scriptViewer.innerHTML = '<div class="empty-state small"><p>No lines in this chapter.</p></div>';
            return;
        }
        
        // Map character IDs to colors
        const charColorMap = {};
        if (currentData.characters) {
            Object.keys(currentData.characters).forEach((id, idx) => {
                charColorMap[id.toLowerCase()] = id.toLowerCase() === 'narrator' ? 'var(--speaker-narrator)' : `var(--speaker-${(idx % 10) + 1})`;
            });
        }

        lines.forEach(line => {
            const speakerId = (line.speaker || 'narrator').toLowerCase();
            const isNarrator = speakerId === 'narrator';
            const color = charColorMap[speakerId] || 'var(--text-muted)';
            
            const div = document.createElement('div');
            div.className = `script-line ${isNarrator ? 'line-narrator' : ''}`;
            div.style.borderLeft = `3px solid ${color}`;
            
            div.innerHTML = `
                <div class="line-speaker" style="color: ${color}">
                    ${escapeHtml(line.speaker || 'Narrator')}
                </div>
                <div class="line-text">
                    ${escapeHtml(line.text)}
                </div>
                <div class="line-emotion">
                    ${line.emotion ? `[${escapeHtml(line.emotion)}]` : ''}
                </div>
            `;
            
            els.scriptViewer.appendChild(div);
        });
    }

    // ============================================================================
    // Quality Tab
    // ============================================================================

    function renderQuality() {
        if (!currentData.quality || Object.keys(currentData.quality).length === 0) {
            els.qualityOverview.innerHTML = '<div class="empty-state small"><p>Quality data will appear after audio generation and validation.</p></div>';
            return;
        }

        const q = currentData.quality;
        els.qualityOverview.innerHTML = '';

        // Segments Total
        addQualityStat('Total Segments', q.total_segments || 0, 'neutral');
        
        // Pass Rate
        const passRate = q.total_segments > 0 ? Math.round((q.passed_segments / q.total_segments) * 100) : 0;
        const passStatus = passRate > 95 ? 'good' : (passRate > 85 ? 'warn' : 'bad');
        addQualityStat('First Pass Rate', `${passRate}%`, passStatus);
        
        // Retries
        addQualityStat('Retries Triggered', q.retries_triggered || 0, q.retries_triggered > 0 ? 'warn' : 'good');
        
        // WER (Word Error Rate)
        if (q.average_wer !== undefined) {
            const wer = (q.average_wer * 100).toFixed(1);
            const werStatus = q.average_wer < 0.02 ? 'good' : (q.average_wer < 0.05 ? 'warn' : 'bad');
            addQualityStat('Avg WER', `${wer}%`, werStatus);
        }
        
        // Silence Drops
        addQualityStat('Silence Errors', q.failed_silence || 0, q.failed_silence > 0 ? 'bad' : 'good');
        
        // Clipping
        addQualityStat('Clipping Errors', q.failed_clipping || 0, q.failed_clipping > 0 ? 'bad' : 'good');
    }

    function addQualityStat(label, value, statusClass) {
        const div = document.createElement('div');
        div.className = 'quality-stat';
        
        let valClass = '';
        if (statusClass === 'good') valClass = 'stat-good';
        if (statusClass === 'warn') valClass = 'stat-warn';
        if (statusClass === 'bad') valClass = 'stat-bad';
        
        div.innerHTML = `
            <div class="stat-value ${valClass}">${value}</div>
            <div class="stat-label">${label}</div>
        `;
        
        els.qualityOverview.appendChild(div);
    }
    
    function escapeHtml(unsafe) {
        if (!unsafe) return '';
        return unsafe.toString()
             .replace(/&/g, "&amp;")
             .replace(/</g, "&lt;")
             .replace(/>/g, "&gt;")
             .replace(/"/g, "&quot;")
             .replace(/'/g, "&#039;");
    }

    return {
        loadData
    };
})();
