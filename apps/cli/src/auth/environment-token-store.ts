import type { TokenStore, CredentialStoreUnavailable } from '../config/types.js';

const ENV_TOKEN_KEY = 'INCIDENTLENS_TOKEN';

/**
 * Environment variable-based token store.
 * Read-only for CI/headless runs.
 * Token is read from INCIDENTLENS_TOKEN environment variable.
 */
export class EnvironmentTokenStore implements TokenStore {
  async get(_profileName: string): Promise<string | CredentialStoreUnavailable | null> {
    const token = process.env[ENV_TOKEN_KEY];
    if (token === undefined || token === '') {
      return null;
    }
    return token;
  }

  async set(_profileName: string, _token: string): Promise<CredentialStoreUnavailable> {
    // Environment tokens are read-only
    return { kind: 'CredentialStoreUnavailable' };
  }

  async delete(_profileName: string): Promise<CredentialStoreUnavailable> {
    // Environment tokens are read-only
    return { kind: 'CredentialStoreUnavailable' };
  }
}
