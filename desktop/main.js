const { app, BrowserWindow, Tray, Menu, ipcMain } = require('electron');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const http = require('http');
const fs = require('fs');

let mainWindow = null;
let tray = null;
let pythonProcesses = [];
let isQuitting = false;
let gpuReleaseRequested = false;
let backendLogTail = '';

// Project root directory
const rootDir = path.resolve(__dirname, '..');

// Find Python executable (prefer virtualenv)
function getPythonExecutable() {
  const candidates = [
    process.env.CAC_PYTHON,
    process.env.VIRTUAL_ENV && path.join(process.env.VIRTUAL_ENV, 'Scripts', 'python.exe'),
    path.join(rootDir, 'venv', 'Scripts', 'python.exe')
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return 'python';
}

// Ask the dashboard to interrupt active inference and unload Ollama/Voice
// before its process is terminated. Closing only the UI must not strand a
// model in GPU memory.
function releaseGpuResources() {
  if (gpuReleaseRequested) {
    return;
  }
  gpuReleaseRequested = true;
  const curl = process.platform === 'win32' ? 'curl.exe' : 'curl';
  try {
    execFileSync(curl, [
      '--silent',
      '--show-error',
      '--max-time', '20',
      '--request', 'POST',
      'http://127.0.0.1:8000/api/system/release-gpu'
    ], { stdio: 'ignore', timeout: 22000 });
  } catch (err) {
    // The dashboard may already be stopped; process-tree cleanup still runs.
  }
}

// Stop only Python process trees launched by this Electron instance.
function stopPythonProcesses() {
  console.log('[Electron] Cleaning up Python subprocesses...');
  releaseGpuResources();
  for (const proc of pythonProcesses) {
    if (proc && proc.pid) {
      try {
        if (process.platform === 'win32') {
          execFileSync('taskkill.exe', ['/F', '/T', '/PID', String(proc.pid)], { stdio: 'ignore' });
        } else {
          proc.kill('SIGKILL');
        }
        console.log(`[Electron] Stopped process PID ${proc.pid}`);
      } catch (err) {
        // Process may have already exited
      }
    }
  }
  pythonProcesses = [];
}

// Start the Dashboard API. The pipeline owns the Voice server lifecycle so
// there is never a duplicate process racing for port 8100.
function startBackendServers() {
  const pythonExe = getPythonExecutable();
  const env = {
    ...process.env,
    PYTHONPATH: rootDir,
    ROCM_SDK_TARGET_FAMILY: process.env.ROCM_SDK_TARGET_FAMILY || 'custom'
  };

  console.log(`[Electron] Using Python: ${pythonExe}`);
  console.log(`[Electron] Working Dir: ${rootDir}`);

  // The dashboard starts Voice on demand when a pipeline reaches work that
  // needs it, and releases it after completion or a scheduled pause.
  const dashProc = spawn(pythonExe, ['-m', 'uvicorn', 'brain.dashboard.api.main:app', '--host', '127.0.0.1', '--port', '8000'], {
    cwd: rootDir,
    env: env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true
  });
  const rememberBackendOutput = chunk => {
    backendLogTail = (backendLogTail + chunk.toString()).slice(-8000);
  };
  dashProc.stdout.on('data', rememberBackendOutput);
  dashProc.stderr.on('data', rememberBackendOutput);
  dashProc.on('error', err => rememberBackendOutput(`\n${err.stack || err.message}`));
  if (dashProc.pid) {
    pythonProcesses.push(dashProc);
    console.log(`[Electron] Launched Dashboard API (PID ${dashProc.pid})`);
  }
}

// Check if Dashboard API is up and responding
function checkServerHealth(url, callback) {
  http.get(url, (res) => {
    if (res.statusCode === 200) {
      callback(true);
    } else {
      callback(false);
    }
  }).on('error', () => {
    callback(false);
  });
}

// Poll server until ready then load URL
function waitForServerAndLoad(url, window, maxAttempts = 30) {
  let attempts = 0;
  const interval = setInterval(() => {
    attempts++;
    checkServerHealth(url + '/api/projects', (healthy) => {
      if (healthy) {
        clearInterval(interval);
        console.log('[Electron] Backend ready! Loading app UI...');
        window.loadURL(url);
      } else if (attempts >= maxAttempts) {
        clearInterval(interval);
        console.log('[Electron] Server wait timeout. Showing diagnostics.');
        const diagnostics = backendLogTail || 'The dashboard process produced no output.';
        const html = `<!doctype html><meta charset="utf-8"><title>Dashboard startup failed</title>
          <style>body{font:16px system-ui;background:#111827;color:#f3f4f6;padding:32px}pre{white-space:pre-wrap;background:#030712;padding:16px;border-radius:8px}</style>
          <h1>Dashboard startup failed</h1><p>Check the configured Python environment and dependencies, then restart the app.</p>
          <pre>${diagnostics.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`;
        window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
      }
    });
  }, 1000);
}

// Create Main Electron Window
function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    title: 'Crazy Audiobook Creator',
    backgroundColor: '#111827',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true
    }
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // Prevent default close to allow tray minimizing if desired, or clean exit
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      // Clean quit when window closed by user
      isQuitting = true;
      stopPythonProcesses();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  const targetUrl = 'http://127.0.0.1:8000';
  waitForServerAndLoad(targetUrl, mainWindow);
}

// App Lifecycle
app.whenReady().then(() => {
  startBackendServers();
  createMainWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

app.on('before-quit', () => {
  isQuitting = true;
  stopPythonProcesses();
});

app.on('window-all-closed', () => {
  isQuitting = true;
  stopPythonProcesses();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
