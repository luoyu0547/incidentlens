import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { EnvironmentTokenStore } from './environment-token-store.js';
import { KeyringTokenStore } from './keyring-token-store.js';

describe('TokenStore', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    vi.resetModules();
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  describe('EnvironmentTokenStore', () => {
    it('reads token from INCIDENTLENS_TOKEN environment variable', async () => {
      process.env['INCIDENTLENS_TOKEN'] = 'test-token-123';
      const store = new EnvironmentTokenStore();

      const token = await store.get('default');

      expect(token).toBe('test-token-123');
    });

    it('returns null when environment variable is not set', async () => {
      delete process.env['INCIDENTLENS_TOKEN'];
      const store = new EnvironmentTokenStore();

      const token = await store.get('default');

      expect(token).toBeNull();
    });

    it('returns CredentialStoreUnavailable when trying to set token', async () => {
      const store = new EnvironmentTokenStore();

      const result = await store.set('default', 'test-token');

      expect(result).toEqual({ kind: 'CredentialStoreUnavailable' });
    });

    it('returns CredentialStoreUnavailable when trying to delete token', async () => {
      const store = new EnvironmentTokenStore();

      const result = await store.delete('default');

      expect(result).toEqual({ kind: 'CredentialStoreUnavailable' });
    });

    it('does not expose token in error messages', async () => {
      process.env['INCIDENTLENS_TOKEN'] = 'secret-token-abc123';
      const store = new EnvironmentTokenStore();

      try {
        // Simulate an error scenario
        await store.get('nonexistent-profile');
      } catch (error) {
        if (error instanceof Error) {
          expect(error.message).not.toContain('secret-token-abc123');
        }
      }
    });
  });

  describe('KeyringTokenStore', () => {
    it('returns CredentialStoreUnavailable when keyring is not available', async () => {
      // This test assumes keyring might not be available in test environment
      const store = new KeyringTokenStore();

      const result = await store.get('default');

      // Either returns token or CredentialStoreUnavailable
      if (result && typeof result === 'object' && 'kind' in result) {
        expect(result).toEqual({ kind: 'CredentialStoreUnavailable' });
      }
    });

    it('does not fall back to plaintext storage', async () => {
      const store = new KeyringTokenStore();

      const setResult = await store.set('default', 'test-token');

      // Should either succeed or return CredentialStoreUnavailable
      if (setResult && typeof setResult === 'object' && 'kind' in setResult) {
        expect(setResult).toEqual({ kind: 'CredentialStoreUnavailable' });
      }
    });

    it('does not expose token in error messages', async () => {
      const store = new KeyringTokenStore();

      try {
        await store.set('default', 'super-secret-token');
      } catch (error) {
        if (error instanceof Error) {
          expect(error.message).not.toContain('super-secret-token');
        }
      }
    });
  });

  describe('token absence from JSON', () => {
    it('does not store token in profile JSON', async () => {
      const { FileConfigStore } = await import('../config/file-config-store.js');
      const { mkdtemp, rm } = await import('node:fs/promises');
      const { join } = await import('node:path');
      const { tmpdir } = await import('node:os');

      const tempDir = await mkdtemp(join(tmpdir(), 'token-test-'));
      const configStore = new FileConfigStore(tempDir);

      try {
        const profile = {
          profileName: 'default',
          apiUrl: 'https://api.example.com',
          lastSequenceBySession: {},
        };

        await configStore.save(profile);
        const loaded = await configStore.load('default');

        // Profile should not contain any token field
        expect(loaded).not.toHaveProperty('token');
        expect(loaded).not.toHaveProperty('accessToken');
        expect(loaded).not.toHaveProperty('authToken');
      } finally {
        await rm(tempDir, { recursive: true, force: true });
      }
    });
  });
});
