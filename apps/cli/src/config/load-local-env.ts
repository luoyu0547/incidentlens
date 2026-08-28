import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

/** Load local demo settings without overriding an explicit shell environment. */
export function loadLocalEnv(directory = process.cwd()): void {
  for (const filename of ['.env', '.env.demo']) {
    const path = join(directory, filename);
    if (!existsSync(path)) continue;
    for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
      const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (!match || process.env[match[1]] !== undefined) continue;
      let value = match[2];
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      process.env[match[1]] = value;
    }
  }
}
