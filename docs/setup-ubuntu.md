# Ubuntu Setup (Legacy)

The current supported architecture is a single Windows workstation. The Voice API, Qwen3-TTS, Parler-TTS, Whisper, mastering, and export now run locally and default to loopback.

The former Ubuntu/NVIDIA deployment guide was removed because it described:

- model identifiers and APIs no longer used by the current code
- an unauthenticated LAN service
- SSH deployment and process-kill scripts that are outside the current lifecycle
- state and cache behavior that no longer matches the implementation

Use [Windows Setup](setup-windows.md) for the supported installation and [Architecture](architecture.md) for the current service boundary.

## If a remote Voice host is reintroduced

Treat it as a new deployment target requiring explicit engineering and verification:

1. Use the same repository version and shared Pydantic schemas on both machines.
2. Configure `brain.voice_server.host` to the remote HTTPS/reverse-proxy address.
3. Configure the same nonempty API token in Brain and Voice.
4. Restrict CORS and firewall rules to the intended Brain host.
5. Ensure all project-relative paths remain meaningful to the Voice workspace; never accept arbitrary Brain filesystem paths.
6. Replace local subprocess assumptions for the Parler bootstrap helper.
7. Verify cancellation, model locks, idle unload, downloads, and partial exports over the new boundary.
8. Add remote integration tests before calling that topology supported.

Do not follow old commands from `implementation_plan*.md` or `chat-history.md`; those files are historical records.
