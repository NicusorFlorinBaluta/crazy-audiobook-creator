/**
 * Script Viewer and Data Manager
 * Handles Characters, Script, and Quality tabs.
 */

window.ScriptViewer = (() => {
    let currentData = {
        characters: null,
        voices: null,
        script: null,
        quality: null,
        qualityReview: null,
        pronunciations: null
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
            fetchQuality(projectId),
            fetchQualityReview(projectId),
            fetchPronunciations(projectId)
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

    async function fetchQualityReview(projectId) {
        try {
            const res = await fetch(`api/projects/${projectId}/quality/review`);
            currentData.qualityReview = res.ok ? await res.json() : null;
        } catch (e) {
            currentData.qualityReview = null;
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
        const assignedProfileCount = voices.filter(voice => (voice.assigned_characters || []).length > 0).length;
        const alternativeCount = Math.max(0, voices.length - assignedProfileCount);
        if (els.castingSummary) {
            els.castingSummary.innerHTML = `
                <div>
                    <strong>${speakingCharacters.length} speaking character${speakingCharacters.length === 1 ? '' : 's'}</strong>
                    using ${assignedProfileCount} assigned voice profile${assignedProfileCount === 1 ? '' : 's'}.
                </div>
                <div class="casting-exclusion">
                    ${alternativeCount} optional alternative${alternativeCount === 1 ? '' : 's'} available; ${excluded} non-speaking registry entr${excluded === 1 ? 'y is' : 'ies are'} excluded.
                </div>
            `;
        }
        renderVoiceReviewBanner(voiceState);

        const groupedVoices = new Map();
        voices.forEach(voice => {
            const ownerId = voice.owner_character_id || voice.voice_id;
            if (!groupedVoices.has(ownerId)) {
                groupedVoices.set(ownerId, []);
            }
            groupedVoices.get(ownerId).push(voice);
        });

        els.charGrid.innerHTML = '';
        let idx = 0;
        groupedVoices.forEach((candidates, ownerId) => {
            const mainVoice = candidates.find(c => c.assigned_characters.length > 0) || candidates[0];
            const cardCharacter = speakerById.get(ownerId);
            const cardDisplayName = cardCharacter?.name || mainVoice.name || ownerId;
            const colorVar = ownerId.toLowerCase() === 'narrator'
                ? 'var(--speaker-narrator)'
                : `var(--speaker-${(idx % 10) + 1})`;
            idx++;

            const assigned = (mainVoice.assigned_characters || [])
                .map(characterId => speakerById.get(characterId))
                .filter(Boolean);
            const mainCandidateIndex = candidates.findIndex(
                candidate => candidate.voice_id === mainVoice.voice_id
            );
            const mainOptionLabel = String.fromCharCode(65 + Math.max(0, mainCandidateIndex));

            let candidatesHtml = '';
            if (candidates.length > 1) {
                candidatesHtml = `
                    <div class="voice-candidates">
                        <strong>Voice Comparison</strong>
                        <div class="voice-comparison-player" style="margin: 10px 0; background: var(--bg-elevated); padding: 10px; border-radius: var(--radius-md);">
                            <audio class="voice-preview-player" style="width: 100%; margin-bottom: 10px;" controls preload="none" src="${escapeHtml(mainVoice.preview_url)}"></audio>
                            <a class="btn btn-ghost btn-sm voice-download-link" href="${escapeHtml(mainVoice.download_url || '#')}">Download voice sample</a>
                            <div class="voice-candidates-toggles" style="display: flex; gap: 8px;">
                                ${candidates.map((candidate, idx) => `
                                    <label class="btn btn-sm ${candidate.voice_id === mainVoice.voice_id ? 'btn-primary' : 'btn-outline'}" style="flex: 1; text-align: center; cursor: pointer;">
                                        <input type="radio" class="visually-hidden" name="candidate-${ownerId}" value="${escapeHtml(candidate.voice_id)}"
                                            data-preview-url="${escapeHtml(candidate.preview_url || '')}"
                                            ${candidate.voice_id === mainVoice.voice_id ? 'checked' : ''}
                                            ${voiceState.editable ? '' : 'disabled'}>
                                        ${String.fromCharCode(65 + idx)} ${candidate.ready ? '' : '(prep)'}
                                    </label>
                                `).join('')}
                            </div>
                        </div>
                        <div class="selected-candidate-status" aria-live="polite">Option ${mainOptionLabel} is currently applied</div>
                        <button class="btn btn-secondary apply-candidate" data-owner-id="${ownerId}" disabled>Option ${mainOptionLabel} is applied</button>
                    </div>
                `;
            } else {
                candidatesHtml = mainVoice.ready
                    ? `<div class="char-voice-preview"><audio class="voice-preview-player" controls preload="none" src="${escapeHtml(mainVoice.preview_url)}"></audio><a class="btn btn-ghost btn-sm voice-download-link" href="${escapeHtml(mainVoice.download_url || '#')}">Download voice sample</a></div>`
                    : `<div class="voice-preview-loading">
                         <div class="voice-pulse-wave"><span></span><span></span><span></span><span></span></div>
                         <span class="voice-loading-text">Synthesizing voice audio preview...</span>
                       </div>`;
            }

            const badgeHtml = mainVoice.ready
                ? '<span class="voice-ready-badge ready">Ready</span>'
                : '<span class="voice-ready-badge preparing active-loading"><span class="voice-spinner-dot"></span> Preparing</span>';
            const warningHtml = (mainVoice.warnings || []).map(warning => `
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
            card.dataset.voiceId = mainVoice.voice_id;
            card.style.setProperty('--char-color', colorVar);
            card.innerHTML = `
                <div class="char-header voice-profile-header">
                    <div class="char-avatar" style="background: ${colorVar}">
                        ${escapeHtml(cardDisplayName.substring(0, 2).toUpperCase())}
                    </div>
                    <div>
                        <div class="char-name">${escapeHtml(cardDisplayName)}</div>
                        <div class="char-meta">
                            ${escapeHtml(mainVoice.gender || 'unknown')} · ${escapeHtml(mainVoice.age_range || 'unknown')}
                            · ${mainVoice.source_type === 'uploaded' ? 'uploaded reference' : 'generated design'}
                        </div>
                    </div>
                    ${badgeHtml}
                </div>
                <div class="voice-assigned-pills">
                    ${assigned.map(character => `<span>${escapeHtml(character.name)}</span>`).join('')}
                </div>
                <div class="char-voice">
                    <strong>Design direction</strong>
                    <p>${escapeHtml(mainVoice.description || 'No design direction available.')}</p>
                </div>
                ${warningHtml}
                ${candidatesHtml}
                <details class="voice-assignments">
                    <summary>Character assignments (${assigned.length})</summary>
                    <div class="voice-assignment-list">${assignmentRows}</div>
                </details>
                <details class="voice-redesign">
                    <summary>Redesign with text</summary>
                    <textarea class="voice-description-input" rows="4"
                              ${voiceState.editable ? '' : 'disabled'}>${escapeHtml(mainVoice.source_description || mainVoice.description || '')}</textarea>
                    <button class="btn btn-secondary voice-regenerate"
                            ${voiceState.editable ? '' : 'disabled'}>${candidates.length > 1 ? `Regenerate option ${mainOptionLabel} (replaces option ${mainOptionLabel})` : 'Generate new preview'}</button>
                    <small class="voice-regenerate-help">${candidates.length > 1 ? 'Only the selected comparison option will be replaced.' : "The app enforces this profile's gender and age metadata and marks only dependent chapters stale."}</small>
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
                const saveButton = row.querySelector('.char-voice-save');
                saveButton?.addEventListener('click', () =>
                    saveVoiceAssignment(row.dataset.characterId, select.value, saveButton)
                );
            });
            const regenerateButton = card.querySelector('.voice-regenerate');
            regenerateButton?.addEventListener('click', () => {
                const selected = card.querySelector(
                    `input[name="candidate-${ownerId}"]:checked`
                );
                regenerateVoice(
                    selected?.value || mainVoice.voice_id,
                    card.querySelector('.voice-description-input').value
                );
            });
            card.querySelector('.voice-upload-submit')?.addEventListener(
                'click',
                () => uploadVoiceSample(
                    mainVoice.voice_id,
                    card.querySelector('.voice-upload-file').files[0],
                    card.querySelector('.voice-upload-transcript').value
                )
            );

            // Add A/B sync player logic
            card.querySelectorAll('.voice-candidates-toggles input[type="radio"]').forEach(radio => {
                radio.addEventListener('change', (e) => {
                    const container = card.querySelector('.voice-comparison-player');
                    if (!container) return;
                    const player = container.querySelector('.voice-preview-player');
                    const download = container.querySelector('.voice-download-link');
                    
                    // Update button UI styles
                    container.querySelectorAll('.btn').forEach(btn => {
                        btn.classList.remove('btn-primary');
                        btn.classList.add('btn-outline');
                    });
                    e.target.closest('.btn').classList.remove('btn-outline');
                    e.target.closest('.btn').classList.add('btn-primary');

                    const selectedIndex = candidates.findIndex(
                        candidate => candidate.voice_id === e.target.value
                    );
                    const optionLabel = String.fromCharCode(65 + selectedIndex);
                    const selectedVoice = candidates[selectedIndex];
                    const status = card.querySelector('.selected-candidate-status');
                    const apply = card.querySelector('.apply-candidate');
                    const description = card.querySelector('.voice-description-input');
                    const regenerate = card.querySelector('.voice-regenerate');
                    if (status) {
                        status.textContent = selectedVoice.voice_id === mainVoice.voice_id
                            ? `Option ${optionLabel} is currently applied`
                            : `Option ${optionLabel} selected; not yet applied`;
                    }
                    if (apply) {
                        apply.disabled = !voiceState.editable || selectedVoice.voice_id === mainVoice.voice_id;
                        apply.textContent = selectedVoice.voice_id === mainVoice.voice_id
                            ? `Option ${optionLabel} is applied`
                            : `Apply option ${optionLabel}`;
                    }
                    if (description) {
                        description.value = selectedVoice.source_description || selectedVoice.description || '';
                    }
                    if (regenerate) {
                        regenerate.textContent = `Regenerate option ${optionLabel} (replaces option ${optionLabel})`;
                    }
                    
                    // A newly selected audition is a new comparison, not a
                    // continuation of the previous option's playback position.
                    const newSrc = e.target.dataset.previewUrl;
                    if (newSrc && newSrc !== 'undefined') {
                        player.pause();
                        player.src = newSrc;
                        player.load();
                        player.currentTime = 0;
                        if (download && selectedVoice?.download_url) {
                            download.href = selectedVoice.download_url;
                        }
                    }
                });
            });

            const applyBtn = card.querySelector('.apply-candidate');
            if (applyBtn) {
                applyBtn.addEventListener('click', async () => {
                    const selected = card.querySelector(`input[name="candidate-${ownerId}"]:checked`).value;
                    if (selected === mainVoice.voice_id) {
                        showToast('That voice option is already applied', 'info');
                        return;
                    }
                    
                    const btn = applyBtn;
                    const prevText = btn.textContent;
                    btn.textContent = 'Applying...';
                    btn.disabled = true;
                    try {
                        const projectId = window.state?.currentProjectId;
                        for (const character of assigned) {
                            const response = await fetch(
                                `api/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(character.character_id)}/voice`, {
                                method: 'PATCH',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ voice_id: selected })
                            });
                            const data = await response.json().catch(() => ({}));
                            if (!response.ok) {
                                throw new Error(data.detail || `Could not update ${character.name}`);
                            }
                        }
                        showToast(`Voice option applied to ${assigned.length} character${assigned.length === 1 ? '' : 's'}`, 'success');
                        await Promise.all([fetchCharacters(projectId), fetchVoices(projectId)]);
                        renderCharacters();
                    } catch (e) {
                        showToast(e.message || 'Failed to apply voice option', 'error');
                        btn.textContent = prevText;
                        btn.disabled = false;
                    }
                });
            }

            els.charGrid.appendChild(card);
        });
    }

    async function uploadVoiceSample(voiceId, file, transcript) {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        if (!file) {
            showToast('Please select an audio file', 'warning');
            return;
        }
        if (!transcript || transcript.trim().length < 3) {
            showToast('Please provide the exact transcript of the audio', 'warning');
            return;
        }
        
        const cardEl = els.charGrid.querySelector(`[data-voice-id="${CSS.escape(voiceId)}"]`);
        if (cardEl) {
            const badge = cardEl.querySelector('.voice-ready-badge');
            if (badge) {
                badge.className = 'voice-ready-badge preparing active-loading';
                badge.innerHTML = '<span class="voice-spinner-dot"></span> Uploading...';
            }
            const previewArea = cardEl.querySelector('.char-voice-preview');
            if (previewArea) {
                previewArea.innerHTML = `
                    <div class="voice-preview-loading">
                        <div class="voice-pulse-wave"><span></span><span></span><span></span><span></span></div>
                        <span class="voice-loading-text">Validating & importing uploaded sample...</span>
                    </div>`;
            }
        }

        const buttons = [...els.charGrid.querySelectorAll('.voice-upload-submit')];
        buttons.forEach(button => { button.disabled = true; });
        showToast('Uploading and validating reference voice...', 'info');
        
        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('transcript', transcript.trim());

            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/voices/${encodeURIComponent(voiceId)}/upload`,
                {
                    method: 'POST',
                    body: formData
                }
            );
            
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Voice upload failed');
            
            const affected = data.affected_chapters || [];
            showToast(
                `Voice imported for ${voiceId}! ${affected.length ? affected.length + ' chapters marked stale.' : ''}`, 
                'success'
            );
            await fetchVoices(projectId);
            renderCharacters();
        } catch (error) {
            showToast(error.message, 'error');
            if (cardEl) {
                const badge = cardEl.querySelector('.voice-ready-badge');
                if (badge) {
                    badge.className = 'voice-ready-badge failed';
                    badge.innerHTML = 'Upload failed';
                }
                // Show the error message directly in the card so it's not missed
                const previewArea = cardEl.querySelector('.char-voice-preview');
                if (previewArea) {
                    previewArea.innerHTML = `
                        <div class="voice-preview-loading" style="display: block; word-break: break-word; color: var(--danger, #f44); text-align: left; font-size: 0.85rem; padding: 0.75rem;">
                            <strong style="display: block; margin-bottom: 4px;">❌ Upload failed:</strong>
                            ${escapeHtml(error.message)}
                        </div>`;
                }
            }
            // Delay re-render so the user can read the error
            setTimeout(async () => {
                await fetchVoices(projectId);
                renderCharacters();
            }, 8000);
        } finally {
            buttons.forEach(button => { button.disabled = false; });
        }
    }

    async function fetchPronunciations(projectId) {
        try {
            const res = await fetch(`api/projects/${projectId}/pronunciations`);
            currentData.pronunciations = res.ok ? await res.json() : null;
        } catch (e) {
            currentData.pronunciations = null;
        }
    }

    function renderVoiceReviewBanner(voiceState) {
        if (!els.voiceReviewBanner) return;
        const review = voiceState.review || {};
        if (!review.required) {
            els.voiceReviewBanner.classList.add('hidden');
            els.voiceReviewBanner.innerHTML = '';
            return;
        }
        const allReady = (voiceState.voices || [])
            .filter(voice => voice.required || (voice.assigned_characters || []).length > 0)
            .every(voice => voice.ready);
        const narratorChoice = voiceState.narrator_choice;
        const narratorOptionsHtml = narratorChoice?.options?.length ? `
            <div class="narrator-choice">
                <div class="narrator-choice-heading">
                    <strong>Choose the narrator</strong>
                    <span>Preview both references; only the selected voice narrates the book.</span>
                </div>
                <div class="narrator-choice-options">
                    ${narratorChoice.options.map(option => `
                        <label class="narrator-choice-option ${option.voice_id === narratorChoice.selected_voice_id ? 'selected' : ''}">
                            <input type="radio" name="narrator-voice"
                                   value="${escapeHtml(option.voice_id)}"
                                   ${option.voice_id === narratorChoice.selected_voice_id ? 'checked' : ''}
                                   ${voiceState.editable ? '' : 'disabled'}>
                            <span class="narrator-choice-label">
                                <b>${escapeHtml(option.gender === 'male' ? 'Male narrator' : 'Female narrator')}</b>
                                <small>${option.voice_id === narratorChoice.selected_voice_id ? 'Selected' : 'Available'}</small>
                            </span>
                            ${option.ready
                                ? `<audio controls preload="none" src="${escapeHtml(option.preview_url)}"></audio>`
                                : '<span class="narrator-choice-pending">Preparing preview…</span>'}
                        </label>
                    `).join('')}
                </div>
            </div>
        ` : '';
        els.voiceReviewBanner.classList.remove('hidden');
        els.voiceReviewBanner.innerHTML = `
            <div class="voice-review-copy">
                <strong>Voice-cast approval required</strong>
                <p>This happens once for a new project, after book-wide scripting identifies the real speakers. Future chapter batches will not stop here again.</p>
                ${narratorOptionsHtml}
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
        els.voiceReviewBanner.querySelectorAll('input[name="narrator-voice"]').forEach(input => {
            input.addEventListener('change', () => {
                if (input.checked && input.value !== narratorChoice.selected_voice_id) {
                    saveVoiceAssignment(narratorChoice.character_id, input.value);
                }
            });
        });
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
                       src="${escapeHtml(assignedVoice.preview_url)}"></audio>
                   <a class="btn btn-ghost btn-sm voice-download-link" href="${escapeHtml(assignedVoice.download_url || '#')}">Download voice sample</a>`
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
                           src="${escapeHtml(voice.preview_url)}"></audio>
                       <a class="btn btn-ghost btn-sm voice-download-link" href="${escapeHtml(voice.download_url || '#')}">Download voice sample</a>`
                    : '<span class="voice-preview-pending">Preview available after voice preparation.</span>';
                const description = card.querySelector('.voice-description-input');
                if (description) description.value = voice?.description || '';
            });
            const saveButton = card.querySelector('.char-voice-save');
            saveButton?.addEventListener('click', () =>
                saveVoiceAssignment(char.id, select.value, saveButton)
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

    async function saveVoiceAssignment(characterId, voiceId, button = null) {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        const previousText = button?.textContent;
        if (button) {
            button.disabled = true;
            button.textContent = 'Assigning...';
        }
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
        } finally {
            if (button?.isConnected) {
                button.disabled = false;
                button.textContent = previousText;
            }
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
                        <span class="voice-loading-text">Checking transcript and audio quality; the first check may take a minute...</span>
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
        const hasQuality = currentData.quality && Object.keys(currentData.quality).length > 0;
        const hasPronunciations = currentData.pronunciations?.candidates?.length > 0;
        const hasJoinReview = currentData.qualityReview?.join_warnings?.length > 0;
        if (!hasQuality && !hasPronunciations && !hasJoinReview) {
            els.qualityOverview.innerHTML = '<div class="empty-state small"><p>Quality data will appear after audio generation and validation.</p></div>';
            return;
        }

        const q = currentData.quality || {};
        els.qualityOverview.innerHTML = '';

        if (hasQuality) {
            // Segments Total
            addQualityStat('Total Segments', q.total_segments || 0, 'neutral');
        
        // Pass Rate
        const acceptedSegments = (q.passed_segments || 0) + (q.accepted_with_warning_segments || 0);
        const passRate = q.total_segments > 0 ? Math.round((acceptedSegments / q.total_segments) * 100) : 0;
        const passStatus = passRate > 95 ? 'good' : (passRate > 85 ? 'warn' : 'bad');
        addQualityStat('Accepted Rate', `${passRate}%`, passStatus);

        addQualityStat(
            'Accepted Warnings',
            q.accepted_with_warning_segments || 0,
            (q.accepted_with_warning_segments || 0) > 0 ? 'warn' : 'good'
        );
        
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
                            <audio controls preload="none" src="${escapeHtml(item.audio_url || '')}"></audio>
                        </div>
                    `).join('')}
                </div>
            `;
            els.qualityOverview.appendChild(details);
        }
        const attemptHistory = q.attempts || [];
        const retriedIds = new Set(
            attemptHistory.filter(item => item.attempt > 1).map(item => item.line_id)
        );
        if (retriedIds.size) {
            const history = document.createElement('details');
            history.className = 'quality-attempt-history';
            history.innerHTML = `
                <summary>All attempts for ${retriedIds.size} retried line${retriedIds.size === 1 ? '' : 's'}</summary>
                <div class="quality-attempts-list">
                    ${attemptHistory.filter(item => retriedIds.has(item.line_id)).map(item => `
                        <div class="quality-attempt-row ${item.selected ? 'selected' : ''}">
                            <span>Ch ${item.chapter_number} · ${escapeHtml(item.line_id)}</span>
                            <strong>${item.selected ? 'Selected artifact' : 'Rejected candidate'}</strong>
                            <span>Attempt ${item.attempt}</span>
                            <span>${escapeHtml(item.status)}</span>
                            <span>WER ${((item.wer || 0) * 100).toFixed(1)}%</span>
                        </div>
                    `).join('')}
                </div>
            `;
            els.qualityOverview.appendChild(history);
        }
        }

        renderJoinReview();
        renderPronunciationInventory();
    }

    function renderJoinReview() {
        const review = currentData.qualityReview;
        const joins = review?.join_warnings || [];
        if (!joins.length) return;
        const section = document.createElement('section');
        section.className = 'join-review';
        const counts = review.review_counts || {};
        section.innerHTML = `
            <div class="join-review-heading">
                <div>
                    <strong>Chapter join review</strong>
                    <p>${joins.length} diagnostic warning${joins.length === 1 ? '' : 's'}, sorted by measured severity. A warning is not automatically an audible defect.</p>
                </div>
                <span>${counts.unreviewed || 0} unreviewed</span>
            </div>
            <div class="join-review-list">
                ${joins.map(item => `
                    <article class="join-review-item" data-item-id="${escapeHtml(item.item_id)}">
                        <div class="join-review-summary">
                            <strong>Chapter ${item.chapter_number} · ${escapeHtml(item.previous_line_id)} → ${escapeHtml(item.current_line_id)}</strong>
                            <span>Δ ${Number(item.loudness_delta_db || 0).toFixed(1)} dB · gap ${item.gap_ms || 0} ms · severity ${Number(item.severity || 0).toFixed(2)}</span>
                        </div>
                        <div class="join-review-lines">
                            <div><small>${escapeHtml(item.previous_line?.speaker || '')}</small><p>${escapeHtml(item.previous_line?.text || '')}</p><audio controls preload="none" src="${escapeHtml(item.previous_audio_url)}"></audio></div>
                            <div><small>${escapeHtml(item.current_line?.speaker || '')}</small><p>${escapeHtml(item.current_line?.text || '')}</p><audio controls preload="none" src="${escapeHtml(item.current_audio_url)}"></audio></div>
                        </div>
                        <div class="join-review-controls">
                            <select class="join-disposition input-sm" aria-label="Review disposition">
                                ${[
                                    ['unreviewed', 'Unreviewed'],
                                    ['acceptable', 'Acceptable'],
                                    ['needs_remaster', 'Needs remaster'],
                                    ['source_tts_issue', 'Source / TTS issue']
                                ].map(([value, label]) => `<option value="${value}" ${item.disposition === value ? 'selected' : ''}>${label}</option>`).join('')}
                            </select>
                            <input class="join-note input-sm" maxlength="2000" value="${escapeHtml(item.review_note || '')}" placeholder="Optional listening note">
                            <button type="button" class="btn btn-secondary btn-sm join-review-save">Save review</button>
                        </div>
                        <small>Reasons: ${escapeHtml((item.reasons || []).join(', ') || 'diagnostic threshold')}</small>
                    </article>
                `).join('')}
            </div>
        `;
        section.querySelectorAll('.join-review-save').forEach(button => {
            button.addEventListener('click', async () => {
                const row = button.closest('.join-review-item');
                await saveReviewDisposition(
                    row?.dataset.itemId || '',
                    row?.querySelector('.join-disposition')?.value || 'unreviewed',
                    row?.querySelector('.join-note')?.value || '',
                    button
                );
            });
        });
        els.qualityOverview.appendChild(section);
    }

    async function saveReviewDisposition(itemId, disposition, note, button) {
        const projectId = window.state?.currentProjectId;
        if (!projectId || !itemId) return;
        button.disabled = true;
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/quality/review`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        item_type: 'join',
                        item_id: itemId,
                        disposition,
                        note: note.trim()
                    })
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Could not save review');
            showToast('Join review saved', 'success');
            await fetchQualityReview(projectId);
            renderQuality();
        } catch (error) {
            showToast(error.message, 'error');
            button.disabled = false;
        }
    }

    function renderPronunciationInventory() {
        const inventory = currentData.pronunciations;
        if (!inventory?.candidates?.length) return;
        const unresolved = inventory.candidates.filter(item => item.status === 'review_required');
        const verified = inventory.candidates.filter(item => item.status === 'verified');
        const section = document.createElement('section');
        section.className = 'pronunciation-review';
        section.innerHTML = `
            <div class="pronunciation-heading">
                <div>
                    <strong>Book pronunciation lexicon</strong>
                    <p>${unresolved.length} term${unresolved.length === 1 ? '' : 's'} need review. Nothing is inferred or applied automatically.</p>
                </div>
                <span>${verified.length} verified</span>
            </div>
            <div class="pronunciation-list">
                ${unresolved.slice(0, 40).map(item => `
                    <div class="pronunciation-row" data-term="${escapeHtml(item.term)}">
                        <div class="pronunciation-term">
                            <strong>${escapeHtml(item.term)}</strong>
                            <small>${item.occurrences} occurrence${item.occurrences === 1 ? '' : 's'} · chapters ${(item.chapters || []).join(', ') || '—'}</small>
                            <span title="${escapeHtml((item.contexts || []).join(' | '))}">${escapeHtml((item.contexts || [])[0] || '')}</span>
                        </div>
                        <input type="text" maxlength="240" placeholder="Spoken form, e.g. Pah-chee" aria-label="Spoken form for ${escapeHtml(item.term)}">
                        <button type="button" class="btn btn-secondary pronunciation-save">Verify</button>
                    </div>
                `).join('')}
                ${verified.map(item => `
                    <div class="pronunciation-row verified">
                        <div class="pronunciation-term">
                            <strong>${escapeHtml(item.term)}</strong>
                            <small>${item.occurrences} occurrence${item.occurrences === 1 ? '' : 's'} · ${escapeHtml(item.mapping_source || 'project')}</small>
                        </div>
                        <span class="pronunciation-arrow">→</span>
                        <strong>${escapeHtml(item.spoken_text)}</strong>
                    </div>
                `).join('')}
            </div>
        `;
        section.querySelectorAll('.pronunciation-save').forEach(button => {
            button.addEventListener('click', () => {
                const row = button.closest('.pronunciation-row');
                approvePronunciation(row?.dataset.term || '', row?.querySelector('input')?.value || '', button);
            });
        });
        els.qualityOverview.appendChild(section);
    }

    async function approvePronunciation(term, spokenText, button) {
        const projectId = window.state?.currentProjectId;
        const spoken = spokenText.trim();
        if (!projectId || !term || !spoken) {
            showToast('Enter the exact spoken form first', 'warning');
            return;
        }
        button.disabled = true;
        try {
            const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/pronunciations`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({term, spoken_text: spoken})
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Could not save pronunciation');
            currentData.pronunciations = data.inventory;
            showToast(`Pronunciation saved; ${data.affected_chapters.length} chapter${data.affected_chapters.length === 1 ? '' : 's'} marked for regeneration`, 'success');
            renderQuality();
        } catch (error) {
            showToast(error.message, 'error');
            button.disabled = false;
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
