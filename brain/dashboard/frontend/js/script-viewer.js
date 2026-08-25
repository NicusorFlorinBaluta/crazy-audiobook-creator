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
        qualityOverview: document.getElementById('quality-overview'),
        castingSearch: document.getElementById('casting-search'),
        castingFilter: document.getElementById('casting-filter'),
        downloadAllVoices: document.getElementById('btn-download-all-voices'),
        scriptSearch: document.getElementById('script-search'),
        scriptSpeakerFilter: document.getElementById('script-speaker-filter'),
        scriptDialogueOnly: document.getElementById('script-dialogue-only'),
        scriptQuotedNarrator: document.getElementById('script-quoted-narrator'),
        scriptLowConfidence: document.getElementById('script-low-confidence'),
        scriptResultCount: document.getElementById('script-result-count'),
        btnRegenerateChapter: document.getElementById('btn-regenerate-chapter')
    };

    let currentScriptChapter = 0;

    els.castingSearch?.addEventListener('input', filterVoiceCards);
    els.castingFilter?.addEventListener('change', filterVoiceCards);
    els.scriptSearch?.addEventListener('input', () => renderScriptLines(currentScriptChapter));
    els.scriptSpeakerFilter?.addEventListener('change', () => renderScriptLines(currentScriptChapter));
    els.scriptDialogueOnly?.addEventListener('change', () => renderScriptLines(currentScriptChapter));
    els.scriptQuotedNarrator?.addEventListener('change', () => renderScriptLines(currentScriptChapter));
    els.scriptLowConfidence?.addEventListener('change', () => renderScriptLines(currentScriptChapter));

    els.btnRegenerateChapter?.addEventListener('click', async () => {
        if (!confirm(`Are you sure you want to regenerate Chapter ${currentScriptChapter}? This will delete the current script for this chapter and require a pipeline run to recreate it.`)) return;

        try {
            const projectId = window.state?.currentProjectId;
            if (!projectId) return;
            const actualChapterNumber = currentData.script?.chapters?.[currentScriptChapter]?.chapter_number || (currentScriptChapter + 1);
            const res = await fetch(`api/projects/${encodeURIComponent(projectId)}/chapters/${actualChapterNumber}/regenerate`, { method: 'POST' });
            if (res.ok) {
                showToast(`Chapter ${currentScriptChapter} queued for regeneration. Start the pipeline to rebuild it.`, 'success');
                // Remove the chapter data from memory and UI
                if (currentData.script && currentData.script.chapters) {
                    currentData.script.chapters[currentScriptChapter] = null;
                }
                renderScriptLines(currentScriptChapter);
            } else {
                showToast(`Failed to regenerate chapter: ${await res.text()}`, 'error');
            }
        } catch (e) {
            showToast(`Error regenerating chapter: ${e.message}`, 'error');
        }
    });

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
        const selectedVoices = voices.filter(voice => voice.ready && voice.assigned_characters && voice.assigned_characters.length > 0);
        const readyVoiceCount = selectedVoices.length;
        if (els.downloadAllVoices) {
            const projectId = window.state?.currentProjectId;
            els.downloadAllVoices.classList.toggle(
                'hidden', !projectId || readyVoiceCount === 0
            );
            els.downloadAllVoices.href = projectId
                ? `api/projects/${encodeURIComponent(projectId)}/voices/download-all`
                : '#';
            els.downloadAllVoices.textContent = readyVoiceCount
                ? `Download all samples (${readyVoiceCount})`
                : 'Download all samples';
            els.downloadAllVoices.title = 'Download selected character and narrator references as a ZIP';
        }
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
        const similarPairCount = (
            voiceState.quality?.cast_pair_diagnostics || []
        ).filter(item => item.status === 'similar' && !item.warning_suppressed).length;
        if (els.castingSummary) {
            els.castingSummary.innerHTML = `
                <div>
                    <strong>${speakingCharacters.length} speaking character${speakingCharacters.length === 1 ? '' : 's'}</strong>
                    using ${assignedProfileCount} assigned voice profile${assignedProfileCount === 1 ? '' : 's'}.
                </div>
                <div class="casting-exclusion">
                    ${alternativeCount} optional alternative${alternativeCount === 1 ? '' : 's'} available; ${excluded} non-speaking registry entr${excluded === 1 ? 'y is' : 'ies are'} excluded.
                </div>
                ${similarPairCount ? `<div class="casting-warning-summary">${similarPairCount} acoustically similar pair${similarPairCount === 1 ? '' : 's'} require preview and acknowledgement before approval.</div>` : ''}
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
        const assignedVoiceByCharacter = new Map();
        voices.forEach(voice => {
            (voice.assigned_characters || []).forEach(characterId => {
                assignedVoiceByCharacter.set(characterId, voice.voice_id);
            });
        });
        const voiceOptionLabel = voice => {
            const owner = speakerById.get(voice.owner_character_id);
            const ownerName = owner?.name || voice.owner_character_id || voice.name;
            return /^Candidate\s+\d+$/i.test(voice.name || '')
                ? `${ownerName} — ${voice.name}`
                : voice.name;
        };

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
                        <div class="voice-section-heading">
                            <strong>Voice options</strong>
                            <span>Preview, compare, then apply</span>
                        </div>
                        <div class="voice-comparison-player">
                            <audio class="voice-preview-player" aria-label="Preview ${escapeHtml(cardDisplayName)}, option ${mainOptionLabel}" controls preload="metadata" src="${escapeHtml(mainVoice.preview_url)}"></audio>
                            <div class="voice-preview-toolbar">
                                <div class="voice-candidates-toggles" aria-label="Voice options for ${escapeHtml(cardDisplayName)}">
                                ${candidates.map((candidate, idx) => `
                                    <label class="btn btn-sm ${candidate.voice_id === mainVoice.voice_id ? 'btn-primary' : 'btn-outline'}">
                                        <input type="radio" class="visually-hidden" name="candidate-${ownerId}" value="${escapeHtml(candidate.voice_id)}"
                                            data-preview-url="${escapeHtml(candidate.preview_url || '')}"
                                            ${candidate.voice_id === mainVoice.voice_id ? 'checked' : ''}
                                            ${voiceState.editable ? '' : 'disabled'}>
                                        ${String.fromCharCode(65 + idx)} ${candidate.ready ? '' : '(prep)'}
                                    </label>
                                `).join('')}
                                </div>
                                <a class="btn btn-ghost btn-sm voice-download-link" data-server-download href="${escapeHtml(mainVoice.download_url || '#')}">Download sample</a>
                            </div>
                        </div>
                        <div class="voice-selection-row">
                            <div class="selected-candidate-status" aria-live="polite">Option ${mainOptionLabel} applied</div>
                            <button class="btn btn-secondary apply-candidate" data-owner-id="${ownerId}" disabled>Applied</button>
                        </div>
                    </div>
                `;
            } else {
                candidatesHtml = mainVoice.ready
                    ? `<div class="voice-candidates voice-single-preview">
                           <div class="voice-section-heading"><strong>Voice sample</strong></div>
                           <div class="voice-comparison-player">
                               <audio class="voice-preview-player" aria-label="Preview ${escapeHtml(cardDisplayName)}" controls preload="metadata" src="${escapeHtml(mainVoice.preview_url)}"></audio>
                               <div class="voice-preview-toolbar voice-preview-toolbar-single">
                                   <a class="btn btn-ghost btn-sm voice-download-link" data-server-download href="${escapeHtml(mainVoice.download_url || '#')}">Download sample</a>
                               </div>
                           </div>
                       </div>`
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
            const designNotesHtml = (mainVoice.design_notes || []).length ? `
                <details class="voice-profile-notes">
                    <summary>Design safeguards applied (${mainVoice.design_notes.length})</summary>
                    ${(mainVoice.design_notes || []).map(note => `<p>${escapeHtml(note)}</p>`).join('')}
                </details>
            ` : '';
            const assignmentRows = assigned.map(character => `
                <div class="voice-assignment-row" data-character-id="${escapeHtml(character.character_id)}">
                    <span class="voice-speaker-name">${escapeHtml(character.name)}</span>
                    <select class="char-voice-select" aria-label="Voice assigned to ${escapeHtml(character.name)}" ${voiceState.editable ? '' : 'disabled'}>
                        ${voices.map(candidate => `
                            <option value="${escapeHtml(candidate.voice_id)}"
                                ${candidate.voice_id === assignedVoiceByCharacter.get(character.character_id) ? 'selected' : ''}>
                                ${escapeHtml(voiceOptionLabel(candidate))}
                            </option>
                        `).join('')}
                    </select>
                    <button class="btn btn-secondary char-voice-save"
                            ${voiceState.editable ? '' : 'disabled'}>Assign</button>
                    <details class="character-profile-editor">
                        <summary>Correct character profile</summary>
                        <div class="character-profile-fields">
                            <label>Gender
                                <select class="character-profile-gender" ${voiceState.editable ? '' : 'disabled'}>
                                    ${['male', 'female', 'other'].map(value => `
                                        <option value="${value}" ${character.gender === value ? 'selected' : ''}>${value}</option>
                                    `).join('')}
                                </select>
                            </label>
                            <label>Age range
                                <input class="character-profile-age" maxlength="80"
                                       value="${escapeHtml(character.age_range || 'unknown')}"
                                       ${voiceState.editable ? '' : 'disabled'}>
                            </label>
                            <label class="character-profile-wide">Speaking style
                                <textarea class="character-profile-style" rows="2" maxlength="500"
                                          ${voiceState.editable ? '' : 'disabled'}>${escapeHtml(character.speaking_style || '')}</textarea>
                            </label>
                            <label class="character-profile-wide">Voice description
                                <textarea class="character-profile-description" rows="3" maxlength="1000"
                                          ${voiceState.editable ? '' : 'disabled'}>${escapeHtml(character.voice_description || '')}</textarea>
                            </label>
                            <button class="btn btn-secondary character-profile-save"
                                    ${voiceState.editable ? '' : 'disabled'}>Save correction</button>
                        </div>
                    </details>
                </div>
            `).join('');

            const card = document.createElement('article');
            card.className = 'character-card voice-profile-card';
            card.dataset.voiceId = mainVoice.voice_id;
            card.dataset.search = `${cardDisplayName} ${mainVoice.name || ''} ${mainVoice.description || ''}`.toLowerCase();
            card.dataset.assigned = String(assigned.length > 0);
            card.dataset.alternatives = String(candidates.length > 1);
            card.dataset.warnings = String((mainVoice.warnings || []).length > 0);
            card.dataset.ready = String(Boolean(mainVoice.ready));
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
                <details class="char-voice voice-design">
                    <summary>Design direction</summary>
                    <p>${escapeHtml(mainVoice.description || 'No design direction available.')}</p>
                </details>
                ${warningHtml}
                ${designNotesHtml}
                ${candidatesHtml}
                <details class="voice-assignments">
                    <summary>Character assignments (${assigned.length})</summary>
                    <div class="voice-assignment-list">${assignmentRows}</div>
                </details>
                <details class="voice-redesign">
                    <summary>Redesign with text</summary>
                    <textarea class="voice-description-input" rows="4" aria-label="Voice design text for ${escapeHtml(cardDisplayName)}"
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
                const profileButton = row.querySelector('.character-profile-save');
                profileButton?.addEventListener('click', () =>
                    saveCharacterProfile(row.dataset.characterId, {
                        gender: row.querySelector('.character-profile-gender').value,
                        age_range: row.querySelector('.character-profile-age').value,
                        speaking_style: row.querySelector('.character-profile-style').value,
                        voice_description: row.querySelector('.character-profile-description').value
                    }, profileButton)
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
                    container.querySelectorAll('.voice-candidates-toggles .btn').forEach(btn => {
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
                            ? `Option ${optionLabel} applied`
                            : `Option ${optionLabel} selected`;
                    }
                    if (apply) {
                        apply.disabled = !voiceState.editable || selectedVoice.voice_id === mainVoice.voice_id;
                        apply.textContent = selectedVoice.voice_id === mainVoice.voice_id
                            ? 'Applied'
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
                        player.setAttribute('aria-label', `Preview ${cardDisplayName}, option ${optionLabel}`);
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
        filterVoiceCards();
    }

    function filterVoiceCards() {
        const search = (els.castingSearch?.value || '').trim().toLowerCase();
        const filter = els.castingFilter?.value || 'all';
        let visible = 0;
        els.charGrid?.querySelectorAll('.voice-profile-card').forEach(card => {
            const searchMatch = !search || card.dataset.search.includes(search);
            const filterMatch = filter === 'all' || card.dataset[filter] === 'true';
            card.hidden = !(searchMatch && filterMatch);
            if (!card.hidden) visible += 1;
        });
        document.getElementById('casting-toolbar')?.classList.toggle('no-results', visible === 0);
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
                                ? `<audio aria-label="Preview ${escapeHtml(option.gender === 'male' ? 'male narrator' : 'female narrator')}" controls preload="metadata" src="${escapeHtml(option.preview_url)}"></audio>`
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
                ? `<audio class="voice-preview-player" aria-label="Preview ${escapeHtml(char.name)}" controls preload="metadata"
                       src="${escapeHtml(assignedVoice.preview_url)}"></audio>
                   <a class="btn btn-ghost btn-sm voice-download-link" data-server-download href="${escapeHtml(assignedVoice.download_url || '#')}">Download voice sample</a>`
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
                    ? `<audio class="voice-preview-player" aria-label="Preview ${escapeHtml(char.name)}" controls preload="metadata"
                           src="${escapeHtml(voice.preview_url)}"></audio>
                       <a class="btn btn-ghost btn-sm voice-download-link" data-server-download href="${escapeHtml(voice.download_url || '#')}">Download voice sample</a>`
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
        const similarPairs = (currentData.voices?.quality?.cast_pair_diagnostics || [])
            .filter(item => item.status === 'similar' && !item.warning_suppressed);
        const distinctnessStale = currentData.voices?.quality?.distinctness_status === 'stale';
        let acknowledgeSimilarPairs = false;
        if (similarPairs.length || distinctnessStale) {
            acknowledgeSimilarPairs = window.confirm(
                (similarPairs.length
                    ? `${similarPairs.length} voice pair(s) sound unusually similar. `
                    : 'One or more voices changed after the last distinctness comparison. ') +
                'Preview the cast before continuing. Continue with this cast anyway?'
            );
            if (!acknowledgeSimilarPairs) return;
        }
        const button = els.voiceReviewBanner?.querySelector('.voice-approve');
        if (button) button.disabled = true;
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/voice-review/approve`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        continue_pipeline: true,
                        acknowledge_similar_pairs: acknowledgeSimilarPairs
                    })
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
            const chNum = ch.chapter_number || (idx + 1);
            opt.textContent = ch.chapter_title || `Chapter ${chNum}`;
            els.chapterSelect.appendChild(opt);
        });
        els.chapterSelect.value = 0;
        renderScriptLines(0);
        
        els.chapterSelect.onchange = (e) => {
            if (e.target.value !== "") {
                currentScriptChapter = parseInt(e.target.value);
                renderScriptLines(currentScriptChapter);
            }
        };
    }

    function renderScriptLines(chapterIndex) {
        if (!currentData.script || !currentData.script.chapters[chapterIndex]) return;
        
        currentScriptChapter = chapterIndex;
        const lines = currentData.script.chapters[chapterIndex].lines || [];
        els.scriptViewer.innerHTML = '';
        
        if (lines.length === 0) {
            els.scriptViewer.innerHTML = '<div class="empty-state small"><p>No lines in this chapter.</p></div>';
            return;
        }
        
        // Map character IDs to colors
        const charColorMap = {};
        const charNameMap = {};
        if (currentData.characters) {
            const charDict = currentData.characters.characters || currentData.characters;
            Object.keys(charDict).forEach((id, idx) => {
                charColorMap[id.toLowerCase()] = id.toLowerCase() === 'narrator' ? 'var(--speaker-narrator)' : `var(--speaker-${(idx % 10) + 1})`;
                charNameMap[id.toLowerCase()] = charDict[id].name || id;
            });
        }

        const speakerIds = [...new Set(lines.map(line => (line.speaker || 'narrator').trim().toLowerCase()))];
        const search = (els.scriptSearch?.value || '').trim().toLowerCase();
        const speakerFilter = els.scriptSpeakerFilter?.value || 'all';
        const dialogueOnly = Boolean(els.scriptDialogueOnly?.checked);
        const quotedNarratorOnly = Boolean(els.scriptQuotedNarrator?.checked);
        const lowConfidenceOnly = Boolean(els.scriptLowConfidence?.checked);

        const allCharacterIds = currentData.characters ? Object.keys(currentData.characters.characters || currentData.characters) : [];
        const combinedSpeakerIds = [...new Set([...allCharacterIds, ...speakerIds])].map(id => id.toLowerCase());
        const allSpeakerOptions = [...new Set(['narrator', ...combinedSpeakerIds.filter(id => id !== 'narrator')])].map(id =>
            `<option value="${escapeHtml(id)}">${escapeHtml(charNameMap[id] || humanizeToken(id))}</option>`
        ).join('');
        if (els.scriptSpeakerFilter) {
            const previousFilter = els.scriptSpeakerFilter.value || 'all';
            els.scriptSpeakerFilter.innerHTML =
                `<option value="all">All speakers</option>${allSpeakerOptions}`;
            els.scriptSpeakerFilter.value = [
                ...els.scriptSpeakerFilter.options
            ].some(option => option.value === previousFilter)
                ? previousFilter
                : 'all';
        }

        const visibleLines = lines.filter(line => {
            const speakerId = (line.speaker || 'narrator').toLowerCase();
            const lineId = String(line.line_id || line.id || '');
            const searchMatch = !search || `${lineId} ${line.text || ''} ${charNameMap[speakerId] || speakerId}`.toLowerCase().includes(search);
            return searchMatch
                && (speakerFilter === 'all' || speakerId === speakerFilter)
                && (!dialogueOnly || speakerId !== 'narrator')
                && (!quotedNarratorOnly || (speakerId === 'narrator' && /[“”"']/.test(line.text || '')))
                && (!lowConfidenceOnly || line.attribution_review_required || (line.speaker_confidence !== undefined && line.speaker_confidence !== null ? Number(line.speaker_confidence) : 1.0) < 0.55);
        });
        if (els.scriptResultCount) {
            els.scriptResultCount.textContent = `${visibleLines.length} of ${lines.length} lines`;
        }
        if (!visibleLines.length) {
            els.scriptViewer.innerHTML = '<div class="empty-state small"><p>No script lines match these filters.</p></div>';
            return;
        }

        visibleLines.forEach(line => {
            const speakerId = (line.speaker || 'narrator').trim().toLowerCase();
            const isNarrator = speakerId === 'narrator';
            const color = charColorMap[speakerId] || 'var(--text-muted)';
            const isLowConfidence = Boolean(line.attribution_review_required) || (line.speaker_confidence !== undefined && line.speaker_confidence !== null ? Number(line.speaker_confidence) : 1.0) < 0.55;
            const reviewTitle = line.attribution_review_reason || 'Low confidence attribution (needs review)';

            const div = document.createElement('div');
            div.className = `script-line ${isNarrator ? 'line-narrator' : ''}`;
            div.dataset.lineId = String(line.line_id || line.id || '');
            div.tabIndex = -1;
            div.style.borderLeft = `3px solid ${color}`;
            
            div.innerHTML = `
                <div class="line-speaker" style="color: ${color}">
                    <select class="speaker-edit-select input-sm" style="color: inherit; background: transparent; border: 1px solid transparent; max-width: 150px;" data-line-id="${escapeHtml(String(line.line_id || line.id || ''))}" title="Edit speaker">
                        ${allSpeakerOptions}
                    </select>
                    <small>${escapeHtml(String(line.line_id || line.id || ''))}</small>
                    ${line.speaker_confidence == null ? '' : `<small title="Attribution resolver: ${escapeHtml(humanizeToken(line.attribution_resolver || 'local'))}">${Math.round(Number(line.speaker_confidence) * 100)}% · ${escapeHtml(humanizeToken(line.attribution_resolver || 'local'))}</small>`}
                    ${isLowConfidence ? `<svg viewBox="0 0 24 24" width="14" height="14" stroke="var(--warning-color, orange)" stroke-width="2" fill="none" style="vertical-align: text-bottom;" title="${escapeHtml(reviewTitle)}"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>` : ''}
                </div>
                <div class="line-text">
                    ${escapeHtml(line.text)}
                </div>
                <div class="line-emotion">
                    ${line.emotion ? `[${escapeHtml(line.emotion)}]` : ''}
                </div>
            `;
            
            const selectEl = div.querySelector('.speaker-edit-select');
            if (selectEl) {
                selectEl.value = speakerId;
                selectEl.addEventListener('change', async (e) => {
                    const newSpeaker = e.target.value;
                    try {
                        const projectId = window.state?.currentProjectId;
                        if (!projectId) return;
                        const actualChapterNumber = currentData.script?.chapters?.[currentScriptChapter]?.chapter_number || (currentScriptChapter + 1);
                        const res = await fetch(`api/projects/${encodeURIComponent(projectId)}/script/chapter/${actualChapterNumber}/line/${line.line_id || line.id}`, {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ speaker: newSpeaker })
                        });
                        if (res.ok) {
                            showToast('Speaker updated successfully', 'success');
                            line.speaker = newSpeaker;
                            line.speaker_confidence = 1.0;
                            renderScriptLines(currentScriptChapter);
                        } else {
                            showToast(`Failed to update speaker: ${await res.text()}`, 'error');
                            e.target.value = speakerId;
                        }
                    } catch (err) {
                        showToast(`Error updating speaker: ${err.message}`, 'error');
                        e.target.value = speakerId;
                    }
                });
            }
            els.scriptViewer.appendChild(div);
        });
    }

    async function saveCharacterProfile(characterId, profile, button = null) {
        const projectId = window.state?.currentProjectId;
        if (!projectId) return;
        const payload = Object.fromEntries(
            Object.entries(profile).map(([key, value]) => [key, value.trim()])
        );
        if (!payload.age_range || !payload.voice_description || payload.voice_description.length < 12) {
            showToast('Provide an age range and a voice description of at least 12 characters', 'warning');
            return;
        }
        const previousText = button?.textContent;
        if (button) {
            button.disabled = true;
            button.textContent = 'Saving...';
        }
        try {
            const response = await fetch(
                `api/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterId)}/profile`,
                {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                }
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Could not save character profile');
            const affected = data.affected_chapters || [];
            showToast(
                data.status === 'unchanged'
                    ? 'Character profile unchanged'
                    : `Correction saved${data.requires_voice_regeneration ? '; regenerate the voice preview' : ''}${affected.length ? `; ${affected.length} chapter${affected.length === 1 ? '' : 's'} marked stale` : ''}`,
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

    function jumpToScriptLine(chapterNumber, lineId) {
        const chapters = currentData.script?.chapters || [];
        const index = chapters.findIndex((chapter, chapterIndex) =>
            Number(chapter.chapter_number || chapter.number || chapterIndex + 1) === Number(chapterNumber)
        );
        if (index < 0) return;
        if (typeof window.activateDetailTab === 'function') {
            window.activateDetailTab('tab-script', true);
        } else {
            document.querySelector('.tab[data-tab="tab-script"]')?.click();
        }
        els.chapterSelect.value = String(index);
        if (els.scriptSearch) els.scriptSearch.value = lineId;
        currentScriptChapter = index;
        renderScriptLines(index);
        requestAnimationFrame(() => {
            const line = els.scriptViewer.querySelector(`[data-line-id="${CSS.escape(lineId)}"]`);
            line?.classList.add('script-line-highlight');
            line?.scrollIntoView({behavior: 'smooth', block: 'center'});
            line?.focus({preventScroll: true});
        });
    }

    // ============================================================================
    // Quality Tab
    // ============================================================================

    function humanizeToken(value) {
        return String(value || 'unspecified')
            .replaceAll('_', ' ')
            .replace(/\b\w/g, character => character.toUpperCase());
    }

    function speakerDisplayName(speakerId) {
        const characters = currentData.characters?.characters || currentData.characters || {};
        return characters[speakerId]?.name || humanizeToken(speakerId);
    }

    function humanizeJoinReason(reason) {
        const labels = {
            segment_loudness_delta: 'Noticeable level difference between adjacent lines',
            short_gap: 'Very short pause between adjacent lines',
            long_gap: 'Long pause between adjacent lines',
            speaker_change: 'Speaker transition needs listening review'
        };
        return labels[reason] || humanizeToken(reason);
    }

    function renderQuality() {
        const q = currentData.quality || {};
        const hasQuality = (q.total_segments || 0) > 0 || (q.stale_records || 0) > 0;
        const hasPronunciations = currentData.pronunciations?.candidates?.length > 0;
        const hasJoinReview = currentData.qualityReview?.join_warnings?.length > 0;
        const hasSegmentReview = currentData.qualityReview?.segment_reviews?.length > 0;
        if (!hasQuality && !hasPronunciations && !hasJoinReview && !hasSegmentReview) {
            els.qualityOverview.innerHTML = '<div class="empty-state small"><p>Quality data will appear after audio generation and validation.</p></div>';
            return;
        }

        els.qualityOverview.innerHTML = '';

        if ((q.stale_records || 0) > 0) {
            const notice = document.createElement('div');
            notice.className = 'quality-stale-notice';
            notice.innerHTML = `<strong>Previous audio checks are archived</strong><span>${q.stale_records} segment result${q.stale_records === 1 ? '' : 's'} belong to audio outside the current reconciled generation. New results will appear as the selected chapters are generated again.</span>`;
            els.qualityOverview.appendChild(notice);
        }

        if (hasQuality && (q.total_segments || 0) > 0) {
            // Segments Total
            addQualityStat('Total Segments', q.total_segments || 0, 'neutral', 'All scripted utterances evaluated in the final run.');
        
        // Pass Rate
        const acceptedSegments = (q.passed_segments || 0) + (q.accepted_with_warning_segments || 0);
        const passRateValue = q.total_segments > 0 ? (acceptedSegments / q.total_segments) * 100 : 0;
        const passRate = acceptedSegments === q.total_segments && q.total_segments > 0
            ? '100%'
            : `${Math.min(99.9, passRateValue).toFixed(1)}%`;
        const passStatus = passRateValue > 95 ? 'good' : (passRateValue > 85 ? 'warn' : 'bad');
        addQualityStat('Accepted Rate', passRate, passStatus, 'Segments accepted automatically plus segments accepted with a documented soft warning.');

        addQualityStat(
            'Accepted Warnings',
            q.accepted_with_warning_segments || 0,
            (q.accepted_with_warning_segments || 0) > 0 ? 'warn' : 'good',
            'Accepted audio that passed hard safety checks but retained a non-blocking diagnostic warning.'
        );
        
        // Retries
        addQualityStat('Retries Triggered', q.retries_triggered || 0, q.retries_triggered > 0 ? 'warn' : 'good', 'Additional TTS attempts requested after an earlier candidate failed validation.');
        
        // WER (Word Error Rate)
        if (q.average_wer !== undefined) {
            const wer = (q.average_wer * 100).toFixed(1);
            const werStatus = q.average_wer < 0.02 ? 'good' : (q.average_wer < 0.05 ? 'warn' : 'bad');
            addQualityStat('Avg WER', `${wer}%`, werStatus, 'Average word error rate measured by transcription validation; lower is better. Approved pronunciation mappings may allow an otherwise high line-level value.');
        }
        
        // Silence Drops
        addQualityStat('Silence Errors', q.failed_silence || 0, q.failed_silence > 0 ? 'bad' : 'good', 'Segments rejected for excessive or invalid silence.');
        
        // Clipping
        addQualityStat('Clipping Errors', q.failed_clipping || 0, q.failed_clipping > 0 ? 'bad' : 'good', 'Segments rejected because the waveform clipped.');

        const noteworthy = (q.final_attempts || []).filter(
            item => item.status !== 'pass' || item.attempt > 1 || item.manual_review_required
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
                            <button type="button" class="quality-line-link" data-chapter="${item.chapter_number}" data-line-id="${escapeHtml(item.line_id)}">Ch ${item.chapter_number} · ${escapeHtml(item.line_id)}</button>
                            <strong class="quality-attempt-status quality-status-${escapeHtml(item.status)}">${escapeHtml(humanizeToken(item.status))}</strong>
                            <span>Attempt ${item.attempt}</span>
                            <span>WER ${((item.wer || 0) * 100).toFixed(1)}%</span>
                            <span title="Final validator confidence">Confidence ${item.validation_confidence == null ? 'n/a' : `${Math.round(item.validation_confidence * 100)}%`}</span>
                            ${item.external_validation_provider ? `<span title="${escapeHtml(item.external_validation_reason || '')}">${escapeHtml(humanizeToken(item.external_validation_provider))} · ${escapeHtml(humanizeToken(item.external_validation_decision || 'abstain'))}</span>` : ''}
                            <span class="quality-reason">${escapeHtml(humanizeToken(item.acceptance_reason))}</span>
                            <details><summary>Reveal transcript and decisions</summary><p>${escapeHtml(item.transcribed_text || 'Transcript unavailable')}</p>${(item.external_validation_history || []).map(step => `<small>${escapeHtml(step.provider || 'local')} · ${escapeHtml(step.decision || 'unknown')} · ${step.confidence == null ? 'n/a' : `${Math.round(step.confidence * 100)}%`} · ${escapeHtml(step.reason || '')}</small>`).join('<br>')}</details>
                            <audio class="quality-attempt-audio" aria-label="Listen to ${escapeHtml(item.line_id)}, final attempt ${item.attempt}" controls preload="metadata" src="${escapeHtml(item.audio_url || '')}"></audio>
                        </div>
                    `).join('')}
                </div>
            `;
            els.qualityOverview.appendChild(details);
            details.querySelectorAll('.quality-line-link').forEach(button => {
                button.addEventListener('click', () => jumpToScriptLine(
                    button.dataset.chapter,
                    button.dataset.lineId
                ));
            });
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

        renderSegmentReview();
        renderJoinReview();
        renderPronunciationInventory();
    }

    function renderSegmentReview() {
        const items = currentData.qualityReview?.segment_reviews || [];
        if (!items.length) return;
        const section = document.createElement('section');
        section.className = 'join-review segment-review';
        const unreviewed = items.filter(item => (item.disposition || 'unreviewed') === 'unreviewed');
        const reviewed = items.filter(item => (item.disposition || 'unreviewed') !== 'unreviewed');
        const cards = rows => rows.map(item => `
            <article class="join-review-item segment-review-item" data-item-id="${escapeHtml(item.item_id)}">
                <div class="join-review-summary">
                    <strong>Chapter ${item.chapter_number ?? '?'} · ${escapeHtml(item.item_id)}</strong>
                    <span>${escapeHtml(humanizeToken(item.status))} · confidence ${item.validation_confidence == null ? 'unavailable' : `${Math.round(item.validation_confidence * 100)}%`}</span>
                </div>
                ${item.text ? `
                    <div class="join-review-quote">
                        <strong>${escapeHtml(item.speaker ? (item.speaker.charAt(0).toUpperCase() + item.speaker.slice(1)) : 'Speaker')}:</strong>
                        <span>“${escapeHtml(item.text)}”</span>
                    </div>
                ` : ''}
                <p>${escapeHtml(item.reason || 'The validation ladder could not reach a safe automatic decision.')}</p>
                <p><small>${item.external_validation_provider ? `${escapeHtml(humanizeToken(item.external_validation_provider))} / ${escapeHtml(item.external_validation_model || 'default model')} / ${escapeHtml(humanizeToken(item.external_validation_decision || 'abstain'))}` : 'No external fallback produced a usable decision'}</small></p>
                <audio aria-label="Review ${escapeHtml(item.item_id)}" controls preload="metadata" src="${escapeHtml(item.audio_url)}"></audio>
                <div class="join-review-controls">
                    <label><span>Disposition</span><select class="segment-disposition input-sm">
                        ${[
                            ['unreviewed', 'Unreviewed'],
                            ['acceptable', 'Accept audio'],
                            ['regenerate', 'Regenerate this audio'],
                            ['source_tts_issue', 'Source / TTS issue (block)'],
                            ['needs_remaster', 'Needs remaster']
                        ].map(([value, label]) => `<option value="${value}" ${item.disposition === value ? 'selected' : ''}>${label}</option>`).join('')}
                    </select></label>
                    <label class="join-note-label"><span>Listening note</span><input class="segment-note input-sm" maxlength="2000" value="${escapeHtml(item.review_note || '')}" placeholder="Optional note"></label>
                    <button type="button" class="btn btn-secondary btn-sm segment-review-save">Save review</button>
                </div>
            </article>
        `).join('');
        section.innerHTML = `
            <div class="join-review-heading"><div><strong>Audio requiring your decision</strong><p>Only segments that exhausted automatic confidence checks appear here.</p></div><span>${unreviewed.length} unreviewed</span></div>
            <div class="join-review-list">${unreviewed.length ? cards(unreviewed) : '<div class="review-complete-message">All escalated audio has been reviewed.</div>'}${reviewed.length ? `<details class="reviewed-joins"><summary>Show reviewed (${reviewed.length})</summary>${cards(reviewed)}</details>` : ''}</div>
        `;
        section.querySelectorAll('.segment-review-save').forEach(button => {
            button.addEventListener('click', async () => {
                const row = button.closest('.segment-review-item');
                const projectId = window.state?.currentProjectId;
                button.disabled = true;
                button.textContent = 'Saving…';
                try {
                    const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/quality/review`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            item_type: 'segment',
                            item_id: row.dataset.itemId,
                            disposition: row.querySelector('.segment-disposition').value,
                            note: row.querySelector('.segment-note').value.trim()
                        })
                    });
                    if (!response.ok) throw new Error(await response.text());
                    const saved = await response.json();
                    showToast(saved.auto_resuming ? 'Audio review saved. Pipeline is resuming automatically.' : 'Audio review saved.', 'success');
                    await fetchQualityReview(projectId);
                    renderQuality();
                } catch (error) {
                    showToast(`Could not save review: ${error.message}`, 'error');
                    button.disabled = false;
                    button.textContent = 'Try again';
                }
            });
        });
        els.qualityOverview.appendChild(section);
    }

    function renderJoinReview() {
        const review = currentData.qualityReview;
        const joins = review?.join_warnings || [];
        if (!joins.length) return;
        const section = document.createElement('section');
        section.className = 'join-review';
        const counts = review.review_counts || {};
        const unreviewed = joins.filter(item => (item.disposition || 'unreviewed') === 'unreviewed');
        const reviewed = joins.filter(item => (item.disposition || 'unreviewed') !== 'unreviewed');
        const cardsHtml = items => items.map(item => `
            <article class="join-review-item" data-item-id="${escapeHtml(item.item_id)}"
                     data-initial-disposition="${escapeHtml(item.disposition || 'unreviewed')}"
                     data-initial-note="${escapeHtml(item.review_note || '')}"
                     data-disposition="${escapeHtml(item.disposition || 'unreviewed')}"
                     data-chapter="${item.chapter_number}"
                     data-speakers="${escapeHtml(`${item.previous_line?.speaker || ''} ${item.current_line?.speaker || ''}`.toLowerCase())}"
                     data-severity="${Number(item.severity || 0)}">
                <div class="join-review-summary">
                    <strong>Chapter ${item.chapter_number} · ${escapeHtml(item.previous_line_id)} → ${escapeHtml(item.current_line_id)}</strong>
                    <span>Level Δ ${Number(item.loudness_delta_db || 0).toFixed(1)} dB · gap ${item.gap_ms || 0} ms · severity ${Number(item.severity || 0).toFixed(2)}</span>
                </div>
                <div class="join-review-lines">
                    <div><small>${escapeHtml(speakerDisplayName(item.previous_line?.speaker || ''))}</small><p>${escapeHtml(item.previous_line?.text || '')}</p><audio aria-label="Previous line, ${escapeHtml(item.previous_line_id)}" controls preload="none" src="${escapeHtml(item.previous_audio_url)}"></audio></div>
                    <div><small>${escapeHtml(speakerDisplayName(item.current_line?.speaker || ''))}</small><p>${escapeHtml(item.current_line?.text || '')}</p><audio aria-label="Current line, ${escapeHtml(item.current_line_id)}" controls preload="none" src="${escapeHtml(item.current_audio_url)}"></audio></div>
                </div>
                <div class="join-review-controls">
                    <label><span>Disposition</span><select class="join-disposition input-sm">
                        ${[
                            ['unreviewed', 'Unreviewed'],
                            ['acceptable', 'Acceptable'],
                            ['needs_remaster', 'Needs remaster'],
                            ['source_tts_issue', 'Source / TTS issue']
                        ].map(([value, label]) => `<option value="${value}" ${item.disposition === value ? 'selected' : ''}>${label}</option>`).join('')}
                    </select></label>
                    <label class="join-note-label"><span>Listening note</span><input class="join-note input-sm" maxlength="2000" value="${escapeHtml(item.review_note || '')}" placeholder="Optional note"></label>
                    <button type="button" class="btn btn-secondary btn-sm join-review-save" disabled>Saved</button>
                </div>
                <small title="Diagnostic signals that placed this transition in the listening queue">Why flagged: ${escapeHtml((item.reasons || []).map(humanizeJoinReason).join(' · ') || 'Diagnostic threshold')}</small>
            </article>
        `).join('');
        section.innerHTML = `
            <div class="join-review-heading">
                <div>
                    <strong>Chapter join review</strong>
                    <p>${joins.length} diagnostic warning${joins.length === 1 ? '' : 's'}, sorted by measured severity. A warning is not automatically an audible defect.</p>
                </div>
                <span>${counts.unreviewed || 0} unreviewed</span>
            </div>
            <div class="join-review-filter-bar">
                <label><span>Disposition</span><select class="input-sm join-filter-disposition">
                    <option value="all">All</option><option value="unreviewed" selected>Unreviewed</option><option value="acceptable">Acceptable</option><option value="needs_remaster">Needs remaster</option><option value="source_tts_issue">Source / TTS issue</option>
                </select></label>
                <label><span>Chapter</span><select class="input-sm join-filter-chapter"><option value="all">All chapters</option>${[...new Set(joins.map(item => item.chapter_number))].sort((a, b) => a - b).map(chapter => `<option value="${chapter}">Chapter ${chapter}</option>`).join('')}</select></label>
                <label><span>Speaker</span><select class="input-sm join-filter-speaker"><option value="all">All speakers</option>${[...new Set(joins.flatMap(item => [item.previous_line?.speaker, item.current_line?.speaker]).filter(Boolean))].sort().map(speaker => `<option value="${escapeHtml(speaker.toLowerCase())}">${escapeHtml(speakerDisplayName(speaker))}</option>`).join('')}</select></label>
                <label><span>Severity</span><select class="input-sm join-filter-severity"><option value="all">All severities</option><option value="0.7">High (0.70+)</option><option value="0.4">Medium+ (0.40+)</option></select></label>
                <button type="button" class="btn btn-ghost btn-sm join-bulk-acceptable">Mark visible acceptable</button>
            </div>
            <div class="join-review-list">
                ${unreviewed.length ? cardsHtml(unreviewed) : '<div class="review-complete-message">All join warnings have been reviewed.</div>'}
                ${reviewed.length ? `<details class="reviewed-joins"><summary>Show reviewed (${reviewed.length})</summary>${cardsHtml(reviewed)}</details>` : ''}
            </div>
        `;
        section.querySelectorAll('.join-review-item').forEach(row => {
            const updateDirtyState = () => {
                const disposition = row.querySelector('.join-disposition')?.value || 'unreviewed';
                const note = row.querySelector('.join-note')?.value || '';
                const dirty = disposition !== row.dataset.initialDisposition || note !== row.dataset.initialNote;
                const button = row.querySelector('.join-review-save');
                button.disabled = !dirty;
                button.textContent = dirty ? 'Save review' : 'Saved';
            };
            row.querySelector('.join-disposition')?.addEventListener('change', updateDirtyState);
            row.querySelector('.join-note')?.addEventListener('input', updateDirtyState);
        });
        const applyJoinFilters = () => {
            const disposition = section.querySelector('.join-filter-disposition').value;
            const chapter = section.querySelector('.join-filter-chapter').value;
            const speaker = section.querySelector('.join-filter-speaker').value;
            const severity = section.querySelector('.join-filter-severity').value;
            let visibleCount = 0;
            section.querySelectorAll('.join-review-item').forEach(row => {
                row.hidden = !(
                    (disposition === 'all' || row.dataset.disposition === disposition)
                    && (chapter === 'all' || row.dataset.chapter === chapter)
                    && (speaker === 'all' || row.dataset.speakers.split(' ').includes(speaker))
                    && (severity === 'all' || Number(row.dataset.severity) >= Number(severity))
                );
                if (!row.hidden) visibleCount += 1;
            });
            section.querySelector('.join-bulk-acceptable').disabled = visibleCount === 0;
            const reviewedDetails = section.querySelector('.reviewed-joins');
            if (reviewedDetails && disposition !== 'unreviewed') reviewedDetails.open = true;
        };
        section.querySelectorAll('.join-review-filter-bar select').forEach(select => {
            select.addEventListener('change', applyJoinFilters);
        });
        section.querySelector('.join-bulk-acceptable')?.addEventListener('click', async event => {
            const visibleRows = [...section.querySelectorAll('.join-review-item:not([hidden])')];
            if (!visibleRows.length) {
                showToast('No visible join warnings to update', 'info');
                return;
            }
            if (!confirm(`Mark ${visibleRows.length} visible join warning${visibleRows.length === 1 ? '' : 's'} as acceptable?`)) return;
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = 'Saving…';
            try {
                const projectId = window.state?.currentProjectId;
                const responses = await Promise.all(visibleRows.map(row => fetch(
                    `api/projects/${encodeURIComponent(projectId)}/quality/review`,
                    {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            item_type: 'join',
                            item_id: row.dataset.itemId,
                            disposition: 'acceptable',
                            note: row.querySelector('.join-note')?.value.trim() || ''
                        })
                    }
                )));
                if (responses.some(response => !response.ok)) throw new Error('One or more reviews could not be saved');
                showToast(`${visibleRows.length} join review${visibleRows.length === 1 ? '' : 's'} marked acceptable`, 'success');
                await fetchQualityReview(projectId);
                renderQuality();
            } catch (error) {
                showToast(error.message, 'error');
                button.disabled = false;
                button.textContent = 'Try bulk update again';
            }
        });
        applyJoinFilters();
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
        button.textContent = 'Saving…';
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
            button.textContent = 'Try again';
        }
    }

    function renderPronunciationInventory() {
        const inventory = currentData.pronunciations;
        if (!inventory) return;
        const candidates = inventory.candidates || [];
        const unresolved = candidates.filter(item => item.status === 'review_required');
        const verified = candidates.filter(item => item.status === 'verified');
        const section = document.createElement('section');
        section.className = 'pronunciation-review';
        section.innerHTML = `
            <div class="pronunciation-heading">
                <div>
                    <strong>Book pronunciation lexicon</strong>
                    <p>${unresolved.length} term${unresolved.length === 1 ? '' : 's'} suggested from text · ${verified.length} custom verified mapping${verified.length === 1 ? '' : 's'}</p>
                </div>
                <span>${verified.length} verified</span>
            </div>

            <div class="pronunciation-add-card">
                <strong>+ Add Custom Phonetic Replacement</strong>
                <input type="text" id="lexicon-custom-term" placeholder="Word in book (e.g. homeisle, homeisler)" maxlength="120">
                <input type="text" id="lexicon-custom-spoken" placeholder="Spoken phonetic form (e.g. home-aisle, home-eye-ler)" maxlength="240">
                <div style="display:flex; gap:8px; align-items:center;">
                    <button type="button" class="btn btn-ghost btn-sm" id="btn-preview-lexicon-custom" title="Test audio preview">▶ Test Preview</button>
                    <button type="button" class="btn btn-primary btn-sm" id="btn-add-lexicon-custom">+ Add / Update Term</button>
                </div>
            </div>

            <div class="pronunciation-search-bar">
                <input type="text" id="lexicon-search-input" placeholder="🔍 Search words, terms, or replacements in lexicon...">
            </div>

            <div class="pronunciation-list" id="pronunciation-items-container">
                <!-- rendered items -->
            </div>
        `;

        function renderList(query = '') {
            const container = section.querySelector('#pronunciation-items-container');
            if (!container) return;
            const q = query.toLowerCase();

            const filteredVerified = verified.filter(item => 
                !q || item.term.toLowerCase().includes(q) || (item.spoken_text || '').toLowerCase().includes(q)
            );
            const filteredUnresolved = unresolved.filter(item => 
                !q || item.term.toLowerCase().includes(q)
            );

            let html = '';
            if (filteredVerified.length) {
                html += filteredVerified.map(item => `
                    <div class="pronunciation-row verified" data-term="${escapeHtml(item.term)}">
                        <div class="pronunciation-term">
                            <strong>${escapeHtml(item.term)}</strong>
                            <small>${item.occurrences || 0} occurrence${item.occurrences === 1 ? '' : 's'} · ${escapeHtml(item.mapping_source || 'project')}</small>
                        </div>
                        <span class="pronunciation-arrow">→</span>
                        <strong style="color:#86efac">${escapeHtml(item.spoken_text)}</strong>
                        <div style="display:flex; align-items:center; gap:6px; margin-left:auto;">
                            <button type="button" class="pronunciation-preview" data-term="${escapeHtml(item.term)}" data-spoken="${escapeHtml(item.spoken_text || '')}" title="Test audio preview">▶ Preview</button>
                            <button type="button" class="pronunciation-delete" data-term="${escapeHtml(item.term)}" title="Remove custom pronunciation">✕ Remove</button>
                        </div>
                    </div>
                `).join('');
            }

            if (filteredUnresolved.length) {
                html += filteredUnresolved.slice(0, 100).map(item => `
                    <div class="pronunciation-row" data-term="${escapeHtml(item.term)}">
                        <div class="pronunciation-term">
                            <strong>${escapeHtml(item.term)}</strong>
                            <small>${item.occurrences || 0} occurrence${item.occurrences === 1 ? '' : 's'} · chapters ${(item.chapters || []).join(', ') || '—'}</small>
                            <span title="${escapeHtml((item.contexts || []).join(' | '))}">${escapeHtml((item.contexts || [])[0] || '')}</span>
                        </div>
                        <input type="text" maxlength="240" placeholder="Spoken form, e.g. Pah-chee" aria-label="Spoken form for ${escapeHtml(item.term)}">
                        <div style="display:flex; align-items:center; gap:6px;">
                            <button type="button" class="btn btn-ghost btn-sm pronunciation-preview-unresolved" data-term="${escapeHtml(item.term)}" title="Test audio preview">▶ Preview</button>
                            <button type="button" class="btn btn-secondary pronunciation-save">Verify</button>
                        </div>
                    </div>
                `).join('');
            }

            if (!filteredVerified.length && !filteredUnresolved.length) {
                html = '<div class="review-complete-message">No lexicon terms match your search.</div>';
            }

            container.innerHTML = html;

            container.querySelectorAll('.pronunciation-save').forEach(button => {
                button.addEventListener('click', () => {
                    const row = button.closest('.pronunciation-row');
                    approvePronunciation(row?.dataset.term || '', row?.querySelector('input')?.value || '', button);
                });
            });

            container.querySelectorAll('.pronunciation-preview').forEach(button => {
                button.addEventListener('click', () => {
                    const term = button.dataset.term || '';
                    const spoken = button.dataset.spoken || '';
                    previewPronunciation(term, spoken, button);
                });
            });

            container.querySelectorAll('.pronunciation-preview-unresolved').forEach(button => {
                button.addEventListener('click', () => {
                    const row = button.closest('.pronunciation-row');
                    const term = button.dataset.term || '';
                    const spoken = row?.querySelector('input')?.value || term;
                    previewPronunciation(term, spoken, button);
                });
            });

            container.querySelectorAll('.pronunciation-delete').forEach(button => {
                button.addEventListener('click', () => {
                    const term = button.dataset.term;
                    if (confirm(`Remove custom pronunciation for "${term}"?`)) {
                        deletePronunciation(term, button);
                    }
                });
            });
        }

        renderList();

        section.querySelector('#lexicon-search-input')?.addEventListener('input', (e) => {
            renderList(e.target.value.trim());
        });

        section.querySelector('#btn-preview-lexicon-custom')?.addEventListener('click', () => {
            const termInput = section.querySelector('#lexicon-custom-term');
            const spokenInput = section.querySelector('#lexicon-custom-spoken');
            const term = termInput?.value.trim() || '';
            const spoken = spokenInput?.value.trim() || term;
            if (!spoken) {
                showToast('Enter a word or spoken form to preview', 'warning');
                return;
            }
            previewPronunciation(term, spoken, section.querySelector('#btn-preview-lexicon-custom'));
        });

        section.querySelector('#btn-add-lexicon-custom')?.addEventListener('click', () => {
            const termInput = section.querySelector('#lexicon-custom-term');
            const spokenInput = section.querySelector('#lexicon-custom-spoken');
            const term = termInput?.value.trim();
            const spoken = spokenInput?.value.trim();
            if (!term || !spoken) {
                showToast('Please enter both the word and its phonetic replacement', 'warning');
                return;
            }
            approvePronunciation(term, spoken, section.querySelector('#btn-add-lexicon-custom'));
        });

        els.qualityOverview.appendChild(section);
    }

    let activePreviewAudio = null;

    async function previewPronunciation(term, spokenText, button) {
        const projectId = window.state?.currentProjectId;
        const cleanTerm = (term || '').replace(/^Pronunciation:\s*/i, '').trim();
        const spoken = (spokenText || cleanTerm || '').replace(/^Pronunciation:\s*/i, '').trim();
        if (!projectId || !spoken) {
            showToast('Enter a term or spoken form to preview', 'warning');
            return;
        }

        if (activePreviewAudio) {
            activePreviewAudio.pause();
            activePreviewAudio = null;
        }

        const originalHtml = button.innerHTML;
        button.disabled = true;
        button.innerHTML = '🔊 <span class="preview-spinner">...</span>';

        try {
            const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/pronunciations/preview`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({term: cleanTerm, spoken_text: spoken})
            });
            const data = await response.json().catch(() => ({}));

            if (response.ok && data.audio_url) {
                const audio = new Audio(`${data.audio_url}?v=${Date.now()}`);
                activePreviewAudio = audio;
                audio.onended = () => {
                    button.innerHTML = originalHtml;
                    button.disabled = false;
                    activePreviewAudio = null;
                };
                audio.onerror = () => {
                    playWebSpeechFallback(spoken, button, originalHtml);
                };
                button.innerHTML = '🔊 Playing...';
                await audio.play();
            } else {
                playWebSpeechFallback(spoken, button, originalHtml);
            }
        } catch (error) {
            playWebSpeechFallback(spoken, button, originalHtml);
        }
    }

    function playWebSpeechFallback(text, button, originalHtml) {
        if ('speechSynthesis' in window) {
            window.speechSynthesis.cancel();
            const utterance = new SpeechSynthesisUtterance(text);
            utterance.rate = 0.95;
            utterance.onend = () => {
                button.innerHTML = originalHtml;
                button.disabled = false;
            };
            utterance.onerror = () => {
                button.innerHTML = originalHtml;
                button.disabled = false;
            };
            button.innerHTML = '🔊 Speaking...';
            window.speechSynthesis.speak(utterance);
        } else {
            button.innerHTML = originalHtml;
            button.disabled = false;
            showToast('Audio playback not supported in this browser', 'warning');
        }
    }

    async function deletePronunciation(term, button) {
        const projectId = window.state?.currentProjectId;
        if (!projectId || !term) return;
        button.disabled = true;
        try {
            const response = await fetch(`api/projects/${encodeURIComponent(projectId)}/pronunciations`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({term, spoken_text: ''})
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Could not delete pronunciation');
            currentData.pronunciations = data.inventory;
            showToast(`Pronunciation for "${term}" removed`, 'info');
            renderQuality();
        } catch (error) {
            showToast(error.message, 'error');
            button.disabled = false;
        }
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
            showToast(`Pronunciation for "${term}" saved! (${data.affected_chapters.length} chapter${data.affected_chapters.length === 1 ? '' : 's'} updated)`, 'success');
            renderQuality();
        } catch (error) {
            showToast(error.message, 'error');
            button.disabled = false;
        }
    }

    function addQualityStat(label, value, statusClass, description = '') {
        const div = document.createElement('div');
        div.className = 'quality-stat';
        if (description) {
            div.title = description;
            div.dataset.tooltip = description;
            div.tabIndex = 0;
            div.setAttribute('aria-label', `${label}: ${value}. ${description}`);
        }
        
        let valClass = '';
        if (statusClass === 'good') valClass = 'stat-good';
        if (statusClass === 'warn') valClass = 'stat-warn';
        if (statusClass === 'bad') valClass = 'stat-bad';
        
        div.innerHTML = `
            <div class="stat-value ${valClass}">${value}</div>
            <div class="stat-label">${label}${description ? ' <span aria-hidden="true">ⓘ</span>' : ''}</div>
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

    function revealLine(lineId) {
        if (!lineId) return false;
        if (!currentData.script || !currentData.script.chapters) return false;

        let targetChapterIndex = -1;
        for (let i = 0; i < currentData.script.chapters.length; i++) {
            const ch = currentData.script.chapters[i];
            if (ch && ch.lines && ch.lines.some(l => l.line_id === lineId)) {
                targetChapterIndex = i;
                break;
            }
        }

        if (targetChapterIndex === -1) {
            const m = lineId.match(/^ch(\d+)_/i);
            if (m) {
                const chNum = parseInt(m[1], 10);
                targetChapterIndex = currentData.script.chapters.findIndex(c => c && (c.chapter_number === chNum || c.number === chNum));
            }
        }

        if (targetChapterIndex !== -1 && targetChapterIndex < currentData.script.chapters.length) {
            if (els.chapterSelect) {
                els.chapterSelect.value = String(targetChapterIndex);
            }
            currentScriptChapter = targetChapterIndex;
            renderScriptLines(targetChapterIndex);

            setTimeout(() => {
                const el = document.querySelector(`[data-line-id="${CSS.escape(lineId)}"]`);
                if (el) {
                    el.scrollIntoView({behavior: 'smooth', block: 'center'});
                    el.classList.add('highlight-line-pulse');
                    setTimeout(() => el.classList.remove('highlight-line-pulse'), 3000);
                }
            }, 120);
            return true;
        }
        return false;
    }

    async function refreshVoices(projectId) {
        if (!projectId) return;
        await fetchVoices(projectId);
        renderCharacters();
    }

    async function refreshPronunciations(projectId) {
        if (!projectId) return;
        await fetchPronunciations(projectId);
        renderQuality();
    }

    return {
        loadData,
        refreshVoices,
        refreshPronunciations,
        previewPronunciation,
        revealLine
    };
})();
