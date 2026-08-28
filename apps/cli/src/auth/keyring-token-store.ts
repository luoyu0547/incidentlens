import type { TokenStore, CredentialStoreUnavailable } from '../config/types.js';

const SERVICE_NAME = 'incidentlens';

/**
 * OS keyring-based token store.
 * Uses @napi-rs/keyring for secure credential storage.
 * Falls back to CredentialStoreUnavailable if keyring is not available.
 */
export class KeyringTokenStore implements TokenStore {
  private AsyncEntry: (typeof import('@napi-rs/keyring'))['AsyncEntry'] | null = null;
  private initPromise: Promise<void> | null = null;

  private async ensureKeyring(): Promise<typeof import('@napi-rs/keyring')['AsyncEntry'] | null> {
    if (this.AsyncEntry !== null) {
      return this.AsyncEntry;
    }

    if (this.initPromise === null) {
      this.initPromise = this.initKeyring();
    }

    await this.initPromise;
    return this.AsyncEntry;
  }

  private async initKeyring(): Promise<void> {
    try {
      const keyringModule = await import('@napi-rs/keyring');
      if (keyringModule.AsyncEntry) {
        this.AsyncEntry = keyringModule.AsyncEntry;
      }
    } catch {
      // Keyring not available
      this.AsyncEntry = null;
    }
  }

  async get(profileName: string): Promise<string | CredentialStoreUnavailable | null> {
    const AsyncEntry = await this.ensureKeyring();

    if (AsyncEntry === null) {
      return { kind: 'CredentialStoreUnavailable' };
    }

    try {
      const entry = new AsyncEntry(SERVICE_NAME, profileName);
      const password = await entry.getPassword();
      return password ?? null;
    } catch {
      return { kind: 'CredentialStoreUnavailable' };
    }
  }

  async set(
    profileName: string,
    token: string
  ): Promise<CredentialStoreUnavailable | void> {
    const AsyncEntry = await this.ensureKeyring();

    if (AsyncEntry === null) {
      return { kind: 'CredentialStoreUnavailable' };
    }

    try {
      const entry = new AsyncEntry(SERVICE_NAME, profileName);
      await entry.setPassword(token);
    } catch {
      return { kind: 'CredentialStoreUnavailable' };
    }
  }

  async delete(profileName: string): Promise<CredentialStoreUnavailable | void> {
    const AsyncEntry = await this.ensureKeyring();

    if (AsyncEntry === null) {
      return { kind: 'CredentialStoreUnavailable' };
    }

    try {
      const entry = new AsyncEntry(SERVICE_NAME, profileName);
      await entry.deletePassword();
    } catch {
      return { kind: 'CredentialStoreUnavailable' };
    }
  }
}
