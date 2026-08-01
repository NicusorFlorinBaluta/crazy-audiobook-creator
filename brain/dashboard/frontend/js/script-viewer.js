/**
 * Script Viewer and Data Manager
 * Handles Characters, Script, and Quality tabs.
 */

window.ScriptViewer = (() => {
    let currentData = {
        characters: null,
        voices: null,
        script: null,
        quality: null
    };

    const els = {
        charGrid: document.getElementById('character-grid'),
        castingSummary: document.getElementById('casting-summary'),
        voiceReviewBanner: document.getElementById('voice-review-banner'),
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
            fetchVoices(projectId),
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
            const res = await fetch(`api/projects/${projectId}/characters`);
            if (res.ok) {
                currentData.characters = await res.json();
            } else {
                currentData.characters = null;
            }
        } catch (e) {
            currentData.characters = null;
        }
    }

    async function fetchVoices(projectId) {
        try {
            const res = await fetch(`api/projects/${projectId}/voices`);
            currentData.voices = res.ok ? await res.json() : null;
        } catch (e) {
            currentData.voices = null;
        }
    }

    async function fetchScript(projectId) {
        try {
            const res = await fetch(`api/projects/${projectId}/script`);
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
            const res = await fetch(`api/projects/${projectId}/quality`);
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
        const voiceState = currentData.voices;
        const voices = voiceState?.voices || [];
        const speakingCharacters = voiceState?.speaking_characters || [];
        const speakerById = new Map(
            speakingCharacters.map(character => [character.character_id, character])
        );

        if (voiceState && !Array.isArray(voiceState.speaking_characters)) {
            if (els.castingSummary) els.castingSummary.innerHTML = '';
            if (els.voiceReviewBanner) els.voiceReviewBanner.classList.add('hidden');
            els.charGrid.innerHTML = `
                <div class="empty-state small">
                    <p>The dashboard backend is still running the previous casting API.</p>
                    <small>Restart the app once to activate speaking-only casting. No project data needs to be reset.</small>
                </div>
            `;
            return;
        }

        if (!voiceState || !voices.length) {
            if (els.castingSummary) els.castingSummary.innerHTML = '';
            if (els.voiceReviewBanner) els.voiceReviewBanner.classList.add('hidden');
            els.charGrid.innerHTML = `
                <div class="empty-state small">
                    <p>Voice casting becomes available after book-wide scripting identifies actual speakers.</p>
                    <small>Non-speaking people, places, groups, and referenced entities will not receive voice profiles.</small>
                </div>
            `;
            return;
        }

        const excluded = voiceState.non_speaking_count || 0;
        if (els.castingSummary) {
            els.castingSummary.innerHTML = `
                <div>
                    <strong>${speakingCharacters.length} speaking character${speakingCharacters.length === 1 ? '' : 's'}</strong>
                    using ${voices.length} reusable voice profile${voices.length === 1 ? '' : 's'}.
                </div>
                <div class="casting-exclusion">
                    ${excluded} non-speaking registry entr${excluded === 1 ? 'y is' : 'ies are'} excluded from casting.
                </div>
            `;
        }
        renderVoiceReviewBanner(voiceState);

        els.charGrid.innerHTML = '';
        voices.forEach((voice, idx) => {
            const colorVar = voice.voice_id.toLowerCase() === 'narrator'
                ? 'var(--speaker-narrator)'
                : `var(--speaker-${(idx % 10) + 1})`;
            const assigned = (voice.assigned_characters || [])
                .map(characterId => speakerById.get(characterId))
                .filter(Boolean);
            const previewHtml = voice.ready
                ? `<audio class="voice-preview-player" controls preload="none"
                       src="${escapeHtml(voice.preview_url)}"></audio>`
                : `<div class="voice-preview-loading">
                     <div class="voice-pulse-wave"><span></span><span></span><span></span><span></span></div>
                     <span class="voice-loading-text">Synthesizing voice audio preview...</span>
                   </div>`;
            const badgeHtml = voice.ready
                ? '<span class="voice-ready-badge ready">Ready</span>'
                : '<span class="voice-ready-badge preparing active-loading"><span class="voice-spinner-dot"></span> Preparing</span>';
            const warningHtml = (voice.warnings || []).map(warning => `
                <div class="voice-profile-warning">${escapeHtml(warning)}</div>
            `).join('');
            const assignmentRows = assigned.map(character => `
                <div class="voice-assignment-row" data-character-id="${escapeHtml(character.character_id)}">
                    <span class="voice-speaker-name">${escapeHtml(character.name)}</span>
                    <select class="char-voice-select" ${voiceState.editable ? '' : 'disabled'}>
                        ${voices.map(candidate => `
                            <option value="${escapeHtml(candidate.voice_id)}"
                                ${candidate.voice_id === character.voice_id ? 'selected' : ''}>
                                ${escapeHtml(candidate.name)}
                            </option>
                        `).join('')}
                    </select>
                    <button class="btn btn-secondary char-voice-save"
                            ${voiceState.editable ? '' : 'disabled'}>Assign</button>
                </div>
            `).join('');

            const card = document.createElement('article');
            card.className = 'character-card voice-profile-card';
            card.dataset.voiceId = voice.voice_id;
            card.style.setProperty('--char-color', colorVar);
            card.innerHTML = `
                <div class="char-header voice-profile-header">
                    <div class="char-avatar" style="background: ${colorVar}">
                        ${escapeHtml((voice.name || voice.voice_id).substring(0, 2).toUpperCase())}
                    </div>
                    <div>
                        <div class="char-name">${escapeHtml(voice.name)}</div>
                        <div class="char-meta">
                            ${escapeHtml(voice.gender || 'unknown')} · ${escapeHtml(voice.age_range || 'unknown')}
                            · ${voice.source_type === 'uploaded' ? 'uploaded reference' : 'generated design'}
                        </div>
                    </div>
                    ${badgeHtml}
                </div>
                <div class="voice-assigned-pills">
                    ${assigned.map(character => `<span>${escapeHtml(character.name)}</span>`).join('')}
                </div>
                <div class="char-voice">
                    <strong>Design direction</strong>
                    <p>${escapeHtml(voice.description || 'No design direction available.')}</p>
                </div>
                ${warningHtml}
                <div class="char-voice-preview">${previewHtml}</div>
                <details class="voice-assignments">
                    <summary>Character assignments (${assigned.length})</summary>
                    <div class="voice-assignment-list">${assignmentRows}</div>
                </details>
                <details class="voice-redesign">
                    <summary>Redesign with text</summary>
                    <textarea class="voice-description-input" rows="4"
                              ${voiceState.editable ? '' : 'disabled'}>${escapeHtml(voice.source_description || voice.description || '')}</textarea>
                    <button class="btn btn-secondary voice-regenerate"
                            ${voiceState.editable ? '' : 'disabled'}>Generate new preview</button>
                    <small>The app enforces this profile's gender and age metadata and marks only dependent chapters stale.</small>
                </details>
                <details class="voice-upload">
                    <summary>Use a recorded voice sample</summary>
                    <label>Audio file
                        <input class="voice-upload-file" type="file"
                               accept=".wav,.flac,.mp3,.m4a,.aac,.ogg,audio/*"
                               ${voiceState.editable ? '' : 'disabled'}>
                    </label>
                    <label>Exact words spoken in the recording
                        <textarea class="voice-upload-transcript" rows="3"
                                  placeholder="Paste the exact transcript…"
                                  ${voiceState.editable ? '' : 'disabled'}></textarea>
                    </label>
                    <button class="btn btn-secondary voice-upload-submit"
                            ${voiceState.editable ? '' : 'disabled'}>Import sample</button>
                    <small>Best results: one clean speaker, 3–30 seconds, no music or effects. Existing dependent chapters become stale.</small>
                </details>
            `;

            card.querySelectorAll('.voice-assignment-row').forEach(row => {
                const select = row.querySelector('.char-voice-select');
                row.querySelector('.char-voice-save')?.addEventListener(
                    'click',
                    () => saveVoiceAssignment(row.dataset.characterId, select.value)
                );
            });
            card.querySelector('.voice-regenerate')?.addEventListener(
                'click',
                () => regenerateVoice(
                    voice.voice_id,
                    card.querySelector('.voice-description-input').value
                )
            );
            card.querySelector('.voice-upload-submit')?.addEventListener(
                'click',
                () => uploadVoice(
                    voice.voice_id,
                    card.querySelector('.voice-upload-file').files[0],
                    card.querySelector('.voice-upload-transcript').value
                )
            );
            els.charGrid.appendChild(card);
        });
    }

    function renderVoiceReviewBanner(voiceState) {
        if (!els.voiceReviewBanner) return;
        const review = voiceState.review || {};
        if (!review.required) {
            els.voiceReviewBanner.classList.add('hidden');
            els.voiceReviewBanner.innerHTML = '';
            return;
        }
        const allReady = (voiceState.voices || []).every(voice => voice.ready);
        els.voiceReviewBanner.classList.remove('hidden');
        els.voiceReviewBanner.innerHTML = `
            <div>
                <strong>Voice-cast approval required</strong>
                <p>This happens once for a new project, after book-wide scripting identifies the real speakers. Future chapter batches will not stop here again.</p>
            </div>
            <button class="btn btn-primary voice-approve"
                    ${voiceState.editable && allReady ? '' : 'disabled'}>
                Approve voices & continue
            </button>
        `;
        els.voiceReviewBanner.querySelector('.voice-approve')?.addEventListener(
            'click',
            approveVoiceCast
        );
    }

    function renderCharactersLegacy() {
        if (!currentData.characters || Object.keys(currentData.characters).length === 0) {
            els.charGrid.innerHTML = '<div class="empty-state small"><p>Characters will appear after the LLM analysis completes (Pass 1).</p></div>';
            return;
        }

        els.charGrid.innerHTML = '';
        
        // Convert to array and sort (Narrator usually first if we identify it, or by mention count)
        // The API might return { book_title: "...", characters: { ... } } or just the characters dict.
        const charDict = currentData.characters.characters || currentData.characters;
        const chars = Object.entries(charDict).map(([id, data]) => ({ id, ...data }));
        const voiceState = currentData.voices || {voices: [], editable: false};
        const voices = voiceState.voices || [];
        const voicesById = new Map(voices.map(voice => [voice.voice_id, voice]));
        
        chars.forEach((char, idx) => {
            // Assign a color based on index
            const colorVar = char.id.toLowerCase() === 'narrator' ? 'var(--speaker-narrator)' : `var(--speaker-${(idx % 10) + 1})`;
            
            const initials = char.name ? char.name.substring(0, 2).toUpperCase() : '??';
            
            const card = document.createElement('div');
            card.className = 'character-card';
            card.style.setProperty('--char-color', colorVar);
            
            let traitsHtml = '';
            const traits = char.personality_traits || char.traits || [];
            if (traits.length > 0) {
                traitsHtml = `<div class="char-traits">` + 
                    traits.slice(0, 4).map(t => `<span class="trait-tag">${escapeHtml(t)}</span>`).join('') +
                `</div>`;
            }
            const assignedVoiceId = char.voice_id || char.id;
            const assignedVoice = voicesById.get(assignedVoiceId);
            const voiceOptions = voices.map(voice => `
                <option value="${escapeHtml(voice.voice_id)}" ${voice.voice_id === assignedVoiceId ? 'selected' : ''}>
                    ${escapeHtml(voice.name)}${voice.ready ? '' : ' (preparing)'}
                </option>
            `).join('');
            const previewHtml = assignedVoice?.ready
                ? `<audio class="voice-preview-player" controls preload="none"
                       src="${escapeHtml(assignedVoice.preview_url)}"></audio>`
                : '<span class="voice-preview-pending">Preview available after voice preparation.</span>';
            const editNote = voiceState.editable
                ? 'A change marks only affected chapters for regeneration.'
                : 'Pause at a safe boundary to change voices.';
            const voiceControlsHtml = currentData.voices ? `
                <div class="char-voice-controls">
                    <label>
                        <span>Assigned voice</span>
                        <select class="char-voice-select" ${voiceState.editable ? '' : 'disabled'}>
                            ${voiceOptions}
                        </select>
                    </label>
                    <button class="btn btn-secondary char-voice-save"
                            ${voiceState.editable ? '' : 'disabled'}>Apply voice</button>
                    <div class="char-voice-preview">${previewHtml}</div>
                    <small>${escapeHtml(editNote)}</small>
                    <details class="voice-redesign">
                        <summary>Redesign assigned voice</summary>
                        <textarea class="voice-description-input" rows="3"
                                  ${voiceState.editable ? '' : 'disabled'}>${escapeHtml(assignedVoice?.description || char.voice_description || '')}</textarea>
                        <button class="btn btn-secondary voice-regenerate"
                                ${voiceState.editable ? '' : 'disabled'}>Generate new preview</button>
                        <small>This changes every character sharing this voice and invalidates their generated chapters.</small>
                    </details>
                </div>
            ` : '';
            
            card.innerHTML = `
                <div class="char-header">
                    <div class="char-avatar" style="background: ${colorVar}">${escapeHtml(initials)}</div>
                    <div>
                        <div class="char-name">${escapeHtml(char.name)}</div>
                        <div class="char-meta">${escapeHtml(char.gender || 'Unknown')} • ${escapeHtml(char.age_range || char.age || 'Unknown Age')}</div>
                    </div>
                </div>
                ${traitsHtml}
                <div class="char-voice">
                    <strong>Voice:</strong> ${escapeHtml(char.voice_description || 'No description yet.')}
                </div>
                ${voiceControlsHtml}
            `;

            const select = card.querySelector('.char-voice-select');
            const preview = card.querySelector('.char-voice-preview');
            select?.addEventListener('change', () => {
                const voice = voicesById.get(select.value);
                preview.innerHTML = voice?.ready
                    ? `<audio class="voice-preview-player" controls preload="none"
                           src="${escapeHtml(voice.preview_url)}"></audio>`
                    : '<span class="voice-preview-pending">Preview available after voice preparation.</span>';
                const description = card.querySelector('.voice-description-input');
                if (description) description.value = voice?.description || '';
            });
            card.querySelector('.char-voice-save')?.addEventListener(
                'click',
                () => saveVoiceAssignment(char.id, select.value)
            );
            card.querySelector('.voice-regenerate')?.addEventListener(
                'click',
                () => regenerateVoice(
                    select.value,
                    card.querySelector('.voice-description-input').value
                )
            );
            
            els.charGrid.appendChild(card);
        });
    }

    async function saveVoiceAssignment(characterId, voiceId) {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterId)}/voice`,
                {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({voice_id: voiceId})
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Could not change voice');
            const affected = data.affected_chapters || [];
            showToast(
                affected.length
                    ? `Voice changed; regenerate chapter${affected.length === 1 ? '' : 's'} ${affected.join(', ')}`
                    : 'Voice assignment unchanged',
                'success'
            );
            await Promise.all([fetchCharacters(projectId), fetchVoices(projectId)]);
            renderCharacters();
        } catch (error) {
            showToast(error.message, 'error');
        }
    }

    async function regenerateVoice(voiceId, voiceDescription) {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        const description = voiceDescription.trim();
        if (description.length < 12) {
            showToast('Describe the voice in at least 12 characters', 'warning');
            return;
        }
        const cardEl = els.charGrid.querySelector(`[data-voice-id="${CSS.escape(voiceId)}"]`);
        if (cardEl) {
            const badge = cardEl.querySelector('.voice-ready-badge');
            if (badge) {
                badge.className = 'voice-ready-badge preparing active-loading';
                badge.innerHTML = '<span class="voice-spinner-dot"></span> Generating...';
            }
            const previewArea = cardEl.querySelector('.char-voice-preview');
            if (previewArea) {
                previewArea.innerHTML = `
                    <div class="voice-preview-loading">
                        <div class="voice-pulse-wave"><span></span><span></span><span></span><span></span></div>
                        <span class="voice-loading-text">Synthesizing & validating new voice preview...</span>
                    </div>`;
            }
        }

        const buttons = [...els.charGrid.querySelectorAll('.voice-regenerate')];
        buttons.forEach(button => { button.disabled = true; });
        showToast('Generating and validating a new voice preview…', 'info');
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/voices/${encodeURIComponent(voiceId)}/regenerate`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({voice_description: description})
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Voice regeneration failed');
            const affected = data.affected_chapters || [];
            showToast(
                `New preview ready${affected.length ? `; ${affected.length} chapter${affected.length === 1 ? '' : 's'} need regeneration` : ''}`,
                'success'
            );
            await Promise.all([fetchCharacters(projectId), fetchVoices(projectId)]);
            renderCharacters();
        } catch (error) {
            showToast(error.message, 'error');
            buttons.forEach(button => { button.disabled = false; });
            await fetchVoices(projectId);
            renderCharacters();
        }
    }

    async function uploadVoice(voiceId, file, transcript) {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        if (!file) {
            showToast('Choose an audio file first', 'warning');
            return;
        }
        if (transcript.trim().length < 3) {
            showToast('Paste the exact words spoken in the sample', 'warning');
            return;
        }

        const cardEl = els.charGrid.querySelector(`[data-voice-id="${CSS.escape(voiceId)}"]`);
        if (cardEl) {
            const badge = cardEl.querySelector('.voice-ready-badge');
            if (badge) {
                badge.className = 'voice-ready-badge preparing active-loading';
                badge.innerHTML = '<span class="voice-spinner-dot"></span> Importing...';
            }
            const previewArea = cardEl.querySelector('.char-voice-preview');
            if (previewArea) {
                previewArea.innerHTML = `
                    <div class="voice-preview-loading">
                        <div class="voice-pulse-wave"><span></span><span></span><span></span><span></span></div>
                        <span class="voice-loading-text">Validating and importing reference audio sample...</span>
                    </div>`;
            }
        }

        const buttons = [...els.charGrid.querySelectorAll('.voice-upload-submit')];
        buttons.forEach(button => { button.disabled = true; });
        const body = new FormData();
        body.append('file', file);
        body.append('transcript', transcript.trim());
        showToast('Validating and importing the voice sample…', 'info');
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/voices/${encodeURIComponent(voiceId)}/upload`,
                {method: 'POST', body}
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Voice upload failed');
            showToast(
                `Voice sample ready (${Number(data.duration_seconds).toFixed(1)}s)`,
                'success'
            );
            await Promise.all([fetchCharacters(projectId), fetchVoices(projectId)]);
            renderCharacters();
        } catch (error) {
            showToast(error.message, 'error');
            buttons.forEach(button => { button.disabled = false; });
            await fetchVoices(projectId);
            renderCharacters();
        }
    }

    async function approveVoiceCast() {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        const button = els.voiceReviewBanner?.querySelector('.voice-approve');
        if (button) button.disabled = true;
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/voice-review/approve`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({continue_pipeline: true})
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Voice approval failed');
            showToast('Voice cast approved; audio generation is starting', 'success');
            await fetchVoices(projectId);
            renderCharacters();
        } catch (error) {
            showToast(error.message, 'error');
            if (button) button.disabled = false;
        }
    }

    // ============================================================================
    // Script Tab
    // ============================================================================

    function renderScriptDropdown() {
        els.chapterSelect.innerHTML = '';
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
            const charDict = currentData.characters.characters || currentData.characters;
            Object.keys(charDict).forEach((id, idx) => {
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

        const noteworthy = (q.final_attempts || []).filter(
            item => item.status !== 'pass' || item.attempt > 1
        );
        if (noteworthy.length) {
            const details = document.createElement('div');
            details.className = 'quality-attempts';
            details.innerHTML = `
                <div class="quality-attempts-heading">
                    <strong>Retries and unresolved checks</strong>
                    <span>${noteworthy.length} line${noteworthy.length === 1 ? '' : 's'}</span>
                </div>
                <div class="quality-attempts-list">
                    ${noteworthy.map(item => `
                        <div class="quality-attempt-row">
                            <span>Ch ${item.chapter_number} · ${escapeHtml(item.line_id)}</span>
                            <strong class="quality-status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</strong>
                            <span>Attempt ${item.attempt}</span>
                            <span>WER ${((item.wer || 0) * 100).toFixed(1)}%</span>
                            <span title="${escapeHtml(item.transcribed_text || '')}">${escapeHtml((item.acceptance_reason || 'unspecified').replaceAll('_', ' '))}</span>
                        </div>
                    `).join('')}
                </div>
            `;
            els.qualityOverview.appendChild(details);
        }
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
