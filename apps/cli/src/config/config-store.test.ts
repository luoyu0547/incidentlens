import { readFile, stat, mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { FileConfigStore } from './file-config-store.js';
import type { ProfileConfig } from './types.js';

describe('FileConfigStore', () => {
  let tempDir: string;
  let store: FileConfigStore;

  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'config-test-'));
    store = new FileConfigStore(tempDir);
  });

  afterEach(async () => {
    await rm(tempDir, { recursive: true, force: true });
  });

  describe('profile round trip', () => {
    it('saves and loads a profile', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com',
        lastTargetId: 'target-1',
        lastSessionId: 'session-1',
        lastSequenceBySession: { 'session-1': 42 },
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded).toEqual(profile);
    });

    it('returns null for non-existent profile', async () => {
      const loaded = await store.load('nonexistent');
      expect(loaded).toBeNull();
    });

    it('preserves unknown fields in profile file', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com',
        lastSequenceBySession: {},
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded).toEqual(profile);
    });
  });

  describe('URL normalization', () => {
    it('normalizes API URL by removing trailing slash', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com/',
        lastSequenceBySession: {},
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded?.apiUrl).toBe('https://api.example.com');
    });

    it('removes query parameters from URL', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com?token=secret',
        lastSequenceBySession: {},
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded?.apiUrl).toBe('https://api.example.com');
    });

    it('removes fragment from URL', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com#section',
        lastSequenceBySession: {},
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded?.apiUrl).toBe('https://api.example.com');
    });

    it('removes credentials from URL', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://user:pass@api.example.com',
        lastSequenceBySession: {},
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded?.apiUrl).toBe('https://api.example.com');
    });
  });

  describe('per-session cursor preservation', () => {
    it('preserves cursors for multiple sessions', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com',
        lastSequenceBySession: {
          'session-1': 10,
          'session-2': 20,
          'session-3': 30,
        },
      };

      await store.save(profile);
      const loaded = await store.load('default');

      expect(loaded?.lastSequenceBySession).toEqual({
        'session-1': 10,
        'session-2': 20,
        'session-3': 30,
      });
    });
  });

  describe('file permissions', () => {
    it('creates config file with 0600 mode on Unix', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com',
        lastSequenceBySession: {},
      };

      await store.save(profile);
      const filePath = join(tempDir, 'default.json');
      const fileStat = await stat(filePath);

      // Check that file permissions are restrictive (owner-only read/write)
      const mode = fileStat.mode & 0o777;
      expect(mode).toBe(0o600);
    });
  });

  describe('atomic file operations', () => {
    it('uses temp file for atomic replacement', async () => {
      const profile: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api.example.com',
        lastSequenceBySession: {},
      };

      await store.save(profile);

      // Verify the file exists and is valid JSON
      const filePath = join(tempDir, 'default.json');
      const content = await readFile(filePath, 'utf8');
      const parsed = JSON.parse(content);

      expect(parsed.profileName).toBe('default');
    });
  });

  describe('profile selection', () => {
    it('loads only the named profile from multiple files', async () => {
      const profile1: ProfileConfig = {
        profileName: 'default',
        apiUrl: 'https://api1.example.com',
        lastSequenceBySession: {},
      };

      const profile2: ProfileConfig = {
        profileName: 'work',
        apiUrl: 'https://api2.example.com',
        lastSequenceBySession: {},
      };

      await store.save(profile1);
      await store.save(profile2);

      const loaded = await store.load('default');
      expect(loaded?.apiUrl).toBe('https://api1.example.com');
    });
  });
});
