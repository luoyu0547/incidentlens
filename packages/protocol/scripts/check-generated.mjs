#!/usr/bin/env node

/**
 * check-generated.mjs
 *
 * Deterministic drift checker: regenerates into a temporary directory and
 * byte-compares against committed generated files.  Must NOT rewrite files
 * during `check`.
 */

import { execSync } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { join, relative } from 'node:path';
import { tmpdir } from 'node:os';

const GENERATED_DIR = join(import.meta.dirname, '..', 'src', 'generated');

if (!existsSync(GENERATED_DIR)) {
  console.error('❌ src/generated/ does not exist. Run `npm run generate` first.');
  process.exit(1);
}

// 1. Create a temporary directory for regeneration
const tmpDir = mkdtempSync(join(tmpdir(), 'protocol-check-'));

try {
  // 2. Run the generation into the temp directory
  console.log('🔄 Regenerating into temporary directory...');
  execSync('npx openapi-ts -o ' + tmpDir, {
    cwd: import.meta.dirname,
    stdio: 'pipe',
    env: { ...process.env, NODE_ENV: 'production' },
  });

  // 3. Compare files recursively
  function getFiles(dir, prefix = '') {
    const files = [];
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const relPath = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        files.push(...getFiles(join(dir, entry.name), relPath));
      } else if (entry.name.endsWith('.ts') || entry.name.endsWith('.js')) {
        files.push(relPath);
      }
    }
    return files;
  }

  const generatedFiles = getFiles(GENERATED_DIR);
  const tempFiles = getFiles(tmpDir);

  // Check for missing files
  const missingInTemp = generatedFiles.filter((f) => !tempFiles.includes(f));
  if (missingInTemp.length > 0) {
    console.error('❌ Generated files are missing from fresh generation:');
    for (const f of missingInTemp) {
      console.error(`   - ${f}`);
    }
    process.exit(1);
  }

  // Check for extra files
  const extraInTemp = tempFiles.filter((f) => !generatedFiles.includes(f));
  if (extraInTemp.length > 0) {
    console.error('❌ Fresh generation produces extra files not in src/generated/:');
    for (const f of extraInTemp) {
      console.error(`   - ${f}`);
    }
    process.exit(1);
  }

  // Byte-compare each file
  let driftFound = false;
  for (const file of generatedFiles) {
    const committed = readFileSync(join(GENERATED_DIR, file));
    const fresh = readFileSync(join(tmpDir, file));

    if (!committed.equals(fresh)) {
      console.error(`❌ Drift detected in ${file}`);
      driftFound = true;
    }
  }

  if (driftFound) {
    console.error('\nGenerated code has drifted. Run `npm run generate` and commit the result.');
    process.exit(1);
  }

  console.log('✅ All generated files match. No drift detected.');
  process.exit(0);
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}
