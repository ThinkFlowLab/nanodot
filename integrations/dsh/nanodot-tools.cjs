'use strict';

// nanodot tools for DSH — topology B of the adapter study: nanodot stays a
// standalone CLI, and a DSH plugin exposes that CLI surface as model-facing
// tools. All policy (who may start a watch, which host it runs on) stays in
// DSH's approval hooks; all watch semantics stay in nanodot core.
//
// This module is deliberately framework-free and holds everything that is
// testable offline: the tool table (pure data) and the CLI runner (spawn
// with argument arrays, timeout, signal forwarding, typed errors).
// index.ts is the thin Cordis registration shim around it.

const { spawn } = require('node:child_process');

const DEFAULT_TIMEOUT_MS = 120000;
const DEFAULT_KILL_GRACE_MS = 5000;
const MAX_OUTPUT_CHARS = 8000;

class NanodotToolError extends Error {
  // Machine codes across the seam (adapter-seam.md, discipline 4):
  // never string passthrough — stderr is attached as data, not the identity.
  constructor(code, detail) {
    super(`${code}: ${detail}`);
    this.code = code;
    this.detail = detail;
  }
}

// Argument mapping — one function per tool, pure, string-safe: caller input
// becomes single argv entries, never a shell string.

function watchAddArgs(args) {
  const argv = ['watch', 'add', args.target, '--yes'];
  if (args.cadence != null) argv.push('--cadence', String(Math.trunc(args.cadence)));
  if (args.purpose) argv.push('--purpose', args.purpose);
  return argv;
}

const TOOLS = [
  {
    name: 'nanodot_watch_add',
    description:
      'Start a read-only watch on one GitHub pull request. nanodot notifies on '
      + 'new commits, check failures, access blockers, and the terminal outcome '
      + '(required checks pass, or the PR merges/closes). Read-only: it never '
      + 'writes to GitHub. Scope confirmation is delegated to the approval hook '
      + 'of this environment (--yes is passed on your behalf only after that).',
    parameters: {
      target: { type: 'string', required: true, description: 'PR to watch, as owner/repo#number' },
      cadence: { type: 'number', required: false, description: 'poll interval in seconds (default 300)' },
      purpose: { type: 'string', required: false, description: 'why this watch exists (descriptive only)' },
    },
    argv: watchAddArgs,
  },
  {
    name: 'nanodot_watch_list',
    description: 'List nanodot watches with status, latest result, next check, and blockers.',
    parameters: {},
    argv: () => ['watch', 'list'],
  },
  {
    name: 'nanodot_watch_show',
    description: 'Show one watch in full: saved scope, policy, state, and next check.',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task id from nanodot_watch_list' },
    },
    argv: (args) => ['watch', 'show', args.task_id],
  },
  {
    name: 'nanodot_watch_control',
    description:
      'Pause, resume, or cancel a watch. Cancelled and completed watches cannot '
      + 'be restarted; create a new one instead.',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task id from nanodot_watch_list' },
      action: { type: 'string', required: true, enum: ['pause', 'resume', 'cancel'] },
    },
    argv: (args) => ['watch', args.action, args.task_id],
  },
  {
    name: 'nanodot_status',
    description: 'Report whether nanodot\'s background runner is running on this host.',
    parameters: {},
    argv: () => ['status'],
  },
  {
    name: 'nanodot_inbox',
    description: 'Read nanodot\'s deduplicated notification inbox (most recent last).',
    parameters: {},
    argv: () => ['inbox'],
  },
  {
    name: 'nanodot_activity',
    description:
      'Read the full activity timeline of one watch: every poll\'s observation, '
      + 'decision events, retries, and notifications.',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task id from nanodot_watch_list' },
    },
    argv: (args) => ['activity', args.task_id],
  },
  {
    name: 'nanodot_tick',
    description:
      'Run one scheduler pass now (nanodot runner --once): every due task is '
      + 'checked once, blockers are reported. Lets this environment drive '
      + 'checking cadence instead of the background runner.',
    parameters: {},
    argv: () => ['runner', '--once'],
  },
];

function toolByName(name) {
  const tool = TOOLS.find((entry) => entry.name === name);
  if (!tool) throw new NanodotToolError('NANODOT_UNKNOWN_TOOL', name);
  return tool;
}

function validateParameters(tool, args) {
  const provided = args || {};
  for (const [key, spec] of Object.entries(tool.parameters)) {
    const value = provided[key];
    if (spec.required && (value == null || value === '')) {
      throw new NanodotToolError('NANODOT_MISSING_PARAMETER', key);
    }
    if (value != null && spec.type === 'number' && typeof value !== 'number') {
      throw new NanodotToolError('NANODOT_BAD_PARAMETER', `${key} must be a number`);
    }
    if (value != null && spec.type === 'string' && typeof value !== 'string') {
      throw new NanodotToolError('NANODOT_BAD_PARAMETER', `${key} must be a string`);
    }
    if (value != null && spec.enum && !spec.enum.includes(value)) {
      throw new NanodotToolError('NANODOT_BAD_PARAMETER', `${key} must be one of ${spec.enum.join(', ')}`);
    }
  }
  for (const key of Object.keys(provided)) {
    if (!(key in tool.parameters)) {
      throw new NanodotToolError('NANODOT_BAD_PARAMETER', `unknown parameter ${key}`);
    }
  }
}

function tail(text, limit) {
  if (text.length <= limit) return text;
  return `…${text.slice(-limit)}`;
}

function runNanodot(argv, { binary, signal, timeoutMs, killGraceMs, env }) {
  const executable = binary || process.env.NANODOT_BIN || 'nanodot';
  return new Promise((resolve, reject) => {
    let settled = false;
    const child = spawn(executable, argv, {
      env: { ...process.env, ...(env || {}) },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });

    let graceTimer = null;
    const finish = (code, signalName) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(graceTimer);
      if (signal) signal.removeEventListener('abort', onAbort);
      if (code === null && (signalName === 'SIGTERM' || signalName === 'SIGKILL')) {
        reject(new NanodotToolError('NANODOT_TERMINATED', 'the call was cancelled or timed out'));
        return;
      }
      if (code === 0) {
        // Success output is capped too: an activity timeline can be far
        // larger than any model context. nanodot prints oldest-first with
        // the newest last, so the tail keeps what matters.
        resolve(tail(stdout.trim() || '(no output)', MAX_OUTPUT_CHARS));
        return;
      }
      // Nonzero exit is a typed outcome, not prose: the code is the identity,
      // stderr is bounded diagnostic data (the CLI already redacts secrets).
      const detail = tail((stderr || stdout).trim(), 2000) || 'no diagnostics';
      reject(new NanodotToolError(`NANODOT_EXIT_${code ?? 'UNKNOWN'}`, detail));
    };

    // Termination is two-stage: SIGTERM first (the CLI shuts down
    // cooperatively), then SIGKILL — a process that ignores SIGTERM must
    // not hang the tool forever.
    const terminate = () => {
      if (settled) return;
      child.kill('SIGTERM');
      graceTimer = setTimeout(() => {
        if (!settled) child.kill('SIGKILL');
      }, killGraceMs || DEFAULT_KILL_GRACE_MS);
    };
    const timer = setTimeout(terminate, timeoutMs || DEFAULT_TIMEOUT_MS);

    const onAbort = () => terminate();
    if (signal) {
      if (signal.aborted) terminate();
      else signal.addEventListener('abort', onAbort, { once: true });
    }

    child.on('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(graceTimer);
      if (signal) signal.removeEventListener('abort', onAbort);
      reject(new NanodotToolError('NANODOT_SPAWN_FAILED', error.message));
    });
    child.on('close', finish);
  });
}

async function runTool(name, args, options = {}) {
  const tool = toolByName(name);
  validateParameters(tool, args);
  return runNanodot(tool.argv(args || {}), options);
}

module.exports = {
  TOOLS,
  NanodotToolError,
  runTool,
  runNanodot,
  toolByName,
  // exported for tests
  _internal: { tail, DEFAULT_TIMEOUT_MS },
};
