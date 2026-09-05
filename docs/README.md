# Documentation

Start here. Every document carries a **Status** line directly under its title,
because a document with no lifecycle marker reads as current forever — the
problem [decisions/README.md](decisions/README.md) was created to solve, applied
to the rest of the directory.

| Status | Means |
| --- | --- |
| **Reference** | Describes current behaviour. Keep accurate when the code changes. |
| **Historical record** | A dated record of what was done and why. Evidence, not a specification — do not implement from it. |
| **Legacy** | Describes a design that is no longer supported. Context only. |
| **Deferred** | Evaluated and deliberately not adopted. Revisit only with new evidence. |

## Reference

The current system. If these disagree with the code, the code is right and the
document is a bug.

| Document | Covers |
| --- | --- |
| [api-reference.md](api-reference.md) | Endpoint reference for the Dashboard/Brain and Voice services. |
| [architecture.md](architecture.md) | How the shipped pipeline works. The authoritative description. |
| [configuration.md](configuration.md) | Every key in `brain/config.yaml` and `voice/config.yaml`. |
| [crazy-voice-companion.md](crazy-voice-companion.md) | The CrazyVoice Android companion app (separate repository). |
| [dashboard-guide.md](dashboard-guide.md) | Operating the dashboard, organised by decision rather than by stage. |
| [prompts.md](prompts.md) | Prompt contracts and the source-fidelity rules the LLM may not break. |
| [quality-assurance.md](quality-assurance.md) | Every quality gate, what it measures, and what it does on failure. |
| [scripting-quality-performance-policy.md](scripting-quality-performance-policy.md) | The order in which scripting trade-offs are decided. |
| [setup-windows.md](setup-windows.md) | Supported single-workstation install. |
| [socket-resilience-and-supervision.md](socket-resilience-and-supervision.md) | Three-layer resilience for staying online across network faults. |
| [voice-design.md](voice-design.md) | How reference voices are designed, cast, and reviewed. |

## Decision records

Dated records of *why* the pipeline behaves as it does, with their own status
convention and index: **[decisions/](decisions/README.md)**.

## Historical records

What was run, what broke, and what was decided at the time. Useful as evidence
and for archaeology. They cite project-specific artifacts that have since been
cleaned up, so some paths inside them no longer resolve.

| Document | Covers |
| --- | --- |
| [voice-review-incident-2026-08-10.md](voice-review-incident-2026-08-10.md) | Voice review failure and its resolution. |
| [tiered-attribution-and-audio-regeneration-2026-09-03.md](tiered-attribution-and-audio-regeneration-2026-09-03.md) | Conversational attribution collapse and the auto-fix engine. |
| [speaker-attribution-incident-2026-08-11.md](speaker-attribution-incident-2026-08-11.md) | Misattributed quotations in the shipped release, and the selective repair. |
| [speaker-attribution-improvements-2026-08-18.md](speaker-attribution-improvements-2026-08-18.md) | Attribution failure modes across a 63-chapter book, and the fixes. |
| [scripting-schema-v4-validation-2026-08-21.md](scripting-schema-v4-validation-2026-08-21.md) | Targeted validation of scripting schema v4. |
| [quality-performance-hardening-2026-08-21.md](quality-performance-hardening-2026-08-21.md) | Hardening pass with fidelity held as the release priority. |
| [production-readiness-2026-08-02.md](production-readiness-2026-08-02.md) | Decisions taken after the failed v32b production run. |
| [performance-improvement-plan-post-release-2026-08-11.md](performance-improvement-plan-post-release-2026-08-11.md) | Performance work identified from the clean release run. |
| [live-validation-results-2026-08-10.md](live-validation-results-2026-08-10.md) | Results of the gates in the plan above. |
| [live-validation-plan-2026-08-10.md](live-validation-plan-2026-08-10.md) | The model/GPU/listening gates deferred from the 2026-08-09 session. |
| [listening-qa-gate-2026-08-10.md](listening-qa-gate-2026-08-10.md) | Decision not to tune crossfade or gain from the existing queue. |
| [improvement-plan-post-e2e-2026-08-09.md](improvement-plan-post-e2e-2026-08-09.md) | Plan reconciling the pre-E2E review against the 2026-08-09 evidence. |
| [home-assistant-integration-plan.md](home-assistant-integration-plan.md) | Plan for the Home Assistant audiobook dashboard. |
| [e2e_benchmark_metrics.md](e2e_benchmark_metrics.md) | Performance and quality improvement plan, 2026-08-03. |
| [pipeline-validation-2026-09-05.md](pipeline-validation-2026-09-05.md) | Full pipeline validated to a published .m4b; the prefix-cache question answered. |
| [e2e-run-2026-09-04.md](e2e-run-2026-09-04.md) | Four defects a green suite could not see: a null-shape fixture, a checkpoint spent too early, a port kill, an unattributed pause. |
| [e2e-run-2026-08-11.md](e2e-run-2026-08-11.md) | Clean release-candidate full-book run. |
| [e2e-run-2026-08-09.md](e2e-run-2026-08-09.md) | First successful full-book run on Windows. |
| [character-augmentation-and-gender-resolution-2026-08-19.md](character-augmentation-and-gender-resolution-2026-08-19.md) | Why gender defaulted to 'other', and the augmentation pass that fixed it. |
| [audio-echo-incident-2026-08-10.md](audio-echo-incident-2026-08-10.md) | Echo smearing traced to the phase-vocoder fallback; why it is disabled. |
| [adaptive-scripting-recovery-2026-08-23.md](adaptive-scripting-recovery-2026-08-23.md) | Recovery from a degraded output rate on a restarted 63-chapter run. |

## Legacy and deferred

| Document | Covers |
| --- | --- |
| [future-tts-research.md](future-tts-research.md) | Alternative TTS engines evaluated but not adopted. |
| [setup-ubuntu.md](setup-ubuntu.md) | The retired Ubuntu/NVIDIA two-machine deployment. Not supported. |

## Measured results

**[benchmarks/](benchmarks/)** holds the raw JSON behind promotion decisions,
plus two write-ups:

| Document | Covers |
| --- | --- |
| [scripting-speed-experiments-2026-08-23.md](benchmarks/scripting-speed-experiments-2026-08-23.md) | Paired scripting throughput experiments across model, context and fragment count. |
| [full-book-two-pass-bootstrap-2026-08-23.md](benchmarks/full-book-two-pass-bootstrap-2026-08-23.md) | Full-book two-pass bootstrap measurements. |

**[plans/](plans/)** holds audit and validation plans:

| Document | Covers |
| --- | --- |
| [unattended-full-app-audit-2026-08-23.md](plans/unattended-full-app-audit-2026-08-23.md) | Unattended full-application audit plan. |

## Not documentation

`implementation_plan*.md`, `VOICE_APP_CHANGES_PLAN.md` and
`VOICE_CLIENT_SERVER_PLAN.md` in the repository root are historical planning
records from earlier two-machine and Ubuntu designs. They describe modules that
were never written. Do not implement from them.
