import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { promisify } from 'node:util';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, it } from 'vitest';

const exec = promisify(execFile);
const root = join(process.cwd(), '../..');

it('installs a clean tarball and launches incidentlens', async () => {
  const packageJson = JSON.parse(await readFile(join(root, 'apps/cli/package.json'), 'utf8'));
  const packDir = await mkdtemp(join(tmpdir(), 'incidentlens-pack-'));
  const temp = await mkdtemp(join(tmpdir(), 'incidentlens-install-'));
  try {
    const { stdout } = await exec(
      'npm',
      ['pack', '--workspace', '@incidentlens/cli', '--json', '--ignore-scripts', '--pack-destination', packDir],
      { cwd: root },
    );
    const result = JSON.parse(stdout)[0] as { filename: string; files: { path: string }[] };
    const files = result.files.map((file) => file.path);
    expect(
      files.every(
        (file) =>
          file === 'package.json' || file === 'README.md' || file === 'LICENSE' || file.startsWith('dist/')
      )
    ).toBe(true);
    expect(files.some((file) => /(^|\/)(src|test|\.env|token)/i.test(file))).toBe(false);

    const packagePath = join(packDir, result.filename.split('/').pop() ?? result.filename);
    try {
      const packageStat = await stat(packagePath);
      expect(packageStat.isFile()).toBe(true);
    } catch {
      await exec(
        'npm',
        ['pack', '--workspace', '@incidentlens/cli', '--ignore-scripts', '--pack-destination', packDir],
        { cwd: root },
      );
      const packageStat = await stat(packagePath);
      expect(packageStat.isFile()).toBe(true);
    }

    await exec('npm', ['init', '-y'], { cwd: temp });
    await exec('npm', ['install', packagePath, '--ignore-scripts'], { cwd: temp });
    const bin = join(temp, 'node_modules/.bin/incidentlens');
    expect((await stat(bin)).mode & 0o111).not.toBe(0);
    const version = await exec(bin, ['--version'], { cwd: temp });
    expect(version.stdout.trim()).toBe(packageJson.version);
  } finally {
    await rm(packDir, { recursive: true, force: true });
    await rm(temp, { recursive: true, force: true });
  }
}, 30_000);
