import { join } from 'node:path';
import pty from 'node-pty';

export interface PtyDriver {
  readonly output: () => string;
  readonly write: (input: string) => void;
  readonly resize: (columns: number, rows: number) => void;
  readonly waitFor: (text: string, timeout?: number) => Promise<string>;
  readonly waitForExit: (timeout?: number) => Promise<number>;
  readonly dispose: () => void;
}

export function spawnCli(env: Record<string, string>, columns = 80, rows = 24): PtyDriver {
  const root = process.cwd();
  const executable = join(root, 'dist', 'cli.js');
  const child = pty.spawn(process.execPath, [executable], { name: 'xterm-256color', cols: columns, rows, cwd: root, env: { ...process.env, ...env, PATH: process.env['PATH'] ?? '/usr/bin:/bin' } as Record<string, string> });
  let text = '';
  child.onData((data) => { text += data; });
  const waitFor = async (needle: string, timeout = 5000): Promise<string> => {
    const started = Date.now();
    while (!text.includes(needle)) {
      if (Date.now() - started > timeout) throw new Error(`PTY timeout waiting for ${needle}; output=${text}`);
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    return text;
  };
  return { output: () => text, write: (input) => child.write(input), resize: (c, r) => child.resize(c, r), waitFor, waitForExit: (timeout = 3000) => new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error('PTY exit timeout')), timeout); child.onExit(({ exitCode }) => { clearTimeout(timer); resolve(exitCode); }); }), dispose: () => child.kill() };
}
