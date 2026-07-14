# Agent Customizations

## Ubuntu Server Rules
**CRITICAL: DO NOT DO DESTRUCTIVE OPERATIONS ON THE UBUNTU HOST (192.168.50.180).**
- Do NOT restart or reboot the machine under any circumstances.
- Do NOT change network settings, firewall rules, or DNS.
- Do NOT modify global system settings or existing Docker/VM configurations.
- The machine hosts other projects (VMs and Docker). Your operations MUST remain strictly isolated to the project directory and its virtual environment.
- Any installation should be localized (`venv`, user-space) rather than system-wide whenever possible to avoid package conflicts.
- **RESOURCE LIMITS:** Ensure any processes run on this machine do not bottleneck existing apps. Use `nice`, limit thread counts, and ensure GPU memory isn't fully exhausted by the pipeline.

## Windows Server Rules
**CRITICAL: DO NOT DO DESTRUCTIVE OPERATIONS ON THE WINDOWS HOST (7900XTX).**
- Do NOT reboot the machine.
- Do NOT change network or global system settings.
- Do NOT uninstall or modify existing applications.
- Run everything locally within the project structure (venv, local installs) when possible.
