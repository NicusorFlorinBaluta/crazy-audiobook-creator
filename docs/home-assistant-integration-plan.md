# Home Assistant Audiobook Dashboard Integration

## Goal

Add an `Audiobooks` dashboard to the Home Assistant configuration in
`E:\Projects\crazy-ha` that can:

- show whether Crazy-PC and the Crazy Audiobook Creator dashboard are reachable;
- wake Crazy-PC and start the dashboard when it is unavailable;
- safely pause active audiobook work, release app-managed GPU services, and shut
  down Crazy-PC;
- embed the complete Crazy Audiobook Creator web UI;
- remain usable remotely through the existing Home Assistant hostname without
  consuming another DynuDNS record.

This document is the implementation and rollout plan. Repository changes can be
prepared and tested by Codex, but deployment, Home Assistant reload/restart,
Windows task registration, firewall changes, and reverse-proxy changes remain
explicit user actions.

## Final Architecture

```text
Remote or local browser
        |
        | HTTPS https://<existing-ha-host>/audiobook/
        v
Existing Nginx Proxy Manager on the HA server
        |
        | HTTP over the trusted LAN
        v
Crazy-PC:8000
        |
        +-- FastAPI dashboard
        +-- pipeline orchestration
        +-- managed Ollama and Voice subprocesses
```

Home Assistant does not fetch iframe content on behalf of the browser. The
browser must be able to load an HTTPS URL itself, so the existing reverse proxy
will expose one additional path on the existing HA hostname. No new DNS record,
proxy installation, or public port is required.

The proxy will strip `/audiobook/` before forwarding requests. The frontend uses
relative URLs so it works both:

- locally at `http://127.0.0.1:8000/`; and
- through HA at `https://<existing-ha-host>/audiobook/`.

## Security Boundaries

Crazy-PC port 8000 must not be exposed directly to the internet.

The intended layers are:

1. Windows Firewall permits TCP 8000 only from Crazy-PC itself, the HA VM, and
   the reverse-proxy host.
2. The audiobook app trusts the actual HA/reverse-proxy TCP peer because it is
   inside `dashboard.trusted_lan_cidrs`; forwarded headers do not grant trust.
3. An application token is optional for this trusted-LAN route.
4. The public `/audiobook/` proxy location must use the same external access
   policy already protecting the proxy, or a dedicated NPM Access List.

Important: a path intercepted by Nginx does not automatically inherit Home
Assistant's application login. If the existing proxy has no authentication in
front of HA, the `/audiobook/` location needs its own access policy. Token
injection authenticates the proxy to the app; it does not authenticate arbitrary
internet clients to the proxy.

The unauthenticated `/health` endpoint contains only readiness information. It
does not expose projects, book names, logs, or controls.

## Application Changes

### Prefix-safe frontend

Replace root-absolute frontend URLs with document-relative URLs:

- `static/...`
- `api/...`
- `ws/updates`

This includes fetch requests, SSE logs, downloads, preview URLs, CSS/JS assets,
and the WebSocket connection.

### Health and remote-request protection

Add:

- `GET /health` with a minimal JSON response;
- optional support for `CRAZY_AUDIOBOOK_DASHBOARD_TOKEN` outside trusted LANs;
- loopback and configured trusted-LAN access without a token;
- fail-closed access from peers outside those CIDRs when no token is configured;
- `X-API-Token` WebSocket authentication for reverse-proxy injection, while
  retaining the existing query-token compatibility.

### Headless launch

Add a PowerShell launcher that:

- locates the repository and configured Python environment;
- reads the ignored local `.env` file;
- optionally reads `CRAZY_AUDIOBOOK_DASHBOARD_TOKEN`;
- starts `python -m brain.dashboard.api.main` in the foreground;
- does not open Electron or a browser.

Add a second PowerShell helper that registers an on-demand Windows Scheduled
Task named `Crazy Audiobook Dashboard`. Registration is a documented manual
step and is not run automatically.

Recommended ignored `.env` entry:

```dotenv
# Optional when every caller is inside dashboard.trusted_lan_cidrs
CRAZY_AUDIOBOOK_DASHBOARD_TOKEN=<long-random-value>
```

The tracked `brain/config.yaml` listens on `0.0.0.0`; Windows Firewall and
`dashboard.trusted_lan_cidrs` constrain unauthenticated LAN access.

## Home Assistant Changes

### Secrets

Add placeholders to `secrets.yaml.example` and real values only to the ignored
`secrets.yaml`:

```yaml
audiobook_health_url: "http://CRAZY_PC_IP:8000/health"
audiobook_release_gpu_url: "http://CRAZY_PC_IP:8000/api/system/release-gpu"
audiobook_external_url: "https://YOUR_EXISTING_HA_HOST/audiobook/"
audiobook_api_token: "OPTIONAL_FOR_TRUSTED_LAN"
```

### Entities

- `binary_sensor.crazy_pc_online`
  - based on HASS.Agent entity availability, not the unreliable assumed state of
    the Wake-on-LAN switch;
- `binary_sensor.crazy_audiobook_app`
  - polls `/health` with a short timeout;
- `sensor.crazy_audiobook_status`
  - summarizes Off, PC Online/App Stopped, Ready, or Transitioning;
- `input_boolean.crazy_audiobook_transition`
  - prevents overlapping dashboard actions and makes progress visible;
- `input_text.audiobook_url`
  - loads the external iframe URL from `secrets.yaml`.

### HASS.Agent command

Create a HASS.Agent Custom command exposed as a button:

```text
Entity:  button.crazy_home_start_audiobook
Command: schtasks.exe /Run /TN "Crazy Audiobook Dashboard"
```

The entity must be verified on the live HA instance before deployment. If
HASS.Agent assigns a different entity ID, update `scripts.yaml`.

### Scripts

- `script.crazy_audiobook_start`
  - set transition state;
  - send Wake-on-LAN through the correct Ethernet and Wi-Fi switches when the PC
    is offline;
  - wait for HASS.Agent;
  - press the HASS.Agent launch button;
  - wait for `/health`;
  - notify on success or timeout;
  - always clear transition state;
- `script.crazy_audiobook_stop`
  - set transition state;
  - call the authenticated GPU-release endpoint;
  - shut down through the existing RPC-backed switches;
  - wait for HASS.Agent to disappear;
  - notify on timeout;
  - clear transition state;
- `script.crazy_audiobook_power_toggle`
  - stop when the app is ready;
  - otherwise start or recover it.

The existing generic Crazy-PC controls remain intact. A later cleanup can
extract their duplicated wake/shutdown logic into shared scripts after this
integration is proven.

## Dashboard

Register an admin-only storage dashboard:

```text
Title: Audiobooks
Path:  dashboard-audiobook
Icon:  mdi:book-music
```

### Control view

- current summarized status;
- Crazy-PC online state;
- app health state;
- large conditional Start or Safe Shutdown button;
- disabled transition card while starting/stopping;
- separate safe GPU-release action;
- link to the embedded App view.

The shutdown control requires confirmation.

### App view

- when healthy: full-panel iframe whose URL comes from
  `input_text.audiobook_url`;
- the iframe aspect ratio is computed from the portrait viewport (with a 75%
  landscape/desktop fallback) so the Companion App does not render it as a
  short half-screen card;
- on an embedded coarse-pointer/mobile client, attachment links open in a new
  browser context because the Companion App WebView does not consistently hand
  downloads to the operating system. If that context is blocked, the dashboard
  copies the direct URL and explains that it must be opened in the browser;
- when unavailable: offline explanation and Start button instead of a browser
  connection error.

## Existing Reverse-Proxy Change

On the existing HA Proxy Host, add a custom location for `/audiobook/`:

```nginx
location /audiobook/ {
    proxy_pass http://CRAZY_PC_LAN_IP:8000/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header X-API-Token "THE_APP_TOKEN";
}
```

The exact NPM generated configuration may already define WebSocket variables;
do not duplicate conflicting directives. Ensure `/audiobook` redirects to
`/audiobook/` so relative URLs resolve correctly.

Apply the appropriate NPM Access List or existing edge authentication to this
location.

## Rollout Order

1. Confirm the HA/proxy peer address belongs to `dashboard.trusted_lan_cidrs`.
2. Optionally configure a token for defense in depth outside the trusted LAN.
3. Register the Windows Scheduled Task.
4. Configure the HASS.Agent launch button and verify its live entity ID.
5. Add the Windows Firewall allow rules and verify direct access only from HA/
   proxy.
6. Deploy and validate the audiobook app changes locally.
7. Add and test the NPM path while on the LAN.
8. Deploy the Home Assistant repository changes.
9. Run Home Assistant Check Configuration.
10. Restart/reload only through the user's normal deployment process.
11. Test the complete start and stop flows.

## Validation Matrix

### Application tests

- root-local static assets and API URLs still work;
- prefixed iframe assets, API, SSE, downloads, and WebSocket work;
- embedded mobile download clicks use the external-context handoff while
  desktop and standalone download behavior remains unchanged;
- `/health` responds without loading a project;
- loopback API works without a token;
- configured trusted-LAN API works without a token;
- peers outside configured CIDRs fail closed without a token;
- header-authenticated WebSocket succeeds;
- `release-gpu` remains idempotent.

### Home Assistant tests

- `python tools/validate_yaml.py`;
- dashboard JSON parses;
- portrait iframe sizing uses the available mobile viewport rather than a fixed
  desktop ratio;
- all new YAML keys map to expected entity IDs;
- read-only live entity queries confirm the HASS.Agent command and sensors;
- no real IP, token, domain, or MAC is committed.

### End-to-end tests

1. PC off → Start → WOL → HASS.Agent online → task start → health on.
2. PC on/app off → Start → task start → health on.
3. App idle → Safe Shutdown → health off → PC off.
4. Pipeline active → Safe Shutdown → pipeline parks/stops → GPU services unload
   → PC off.
5. HASS.Agent timeout → visible failure notification and transition cleared.
6. App health timeout → visible failure notification and transition cleared.
7. Remote HA dashboard loads the embedded app over HTTPS with live WebSocket
   progress.

## Rollback

- Remove or disable the `Audiobooks` dashboard registration.
- Disable the three audiobook scripts.
- Remove the NPM custom location.
- Disable the Windows Scheduled Task.
- Return `dashboard.host` to `127.0.0.1` if LAN serving is no longer required.

Existing projects, audio artifacts, the generic Crazy-PC controls, and the
pipeline database are unaffected.
