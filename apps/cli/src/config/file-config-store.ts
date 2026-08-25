import { readFile, writeFile, rename, chmod, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { ProfileConfigSchema, type ProfileConfig, type ConfigStore } from './types.js';

/**
 * File-based configuration store.
 * Stores profile data in the user's config directory with restrictive permissions.
 * Tokens are never stored here.
 */
export class FileConfigStore implements ConfigStore {
  private readonly configDir: string;

  constructor(configDir: string) {
    this.configDir = configDir;
  }

  async load(profileName: string): Promise<ProfileConfig | null> {
    try {
      const filePath = this.getFilePath(profileName);
      const content = await readFile(filePath, 'utf8');
      const raw = JSON.parse(content) as unknown;

      // Validate with Zod schema
      const result = ProfileConfigSchema.safeParse(raw);
      if (!result.success) {
        return null;
      }

      return result.data;
    } catch {
      return null;
    }
  }

  async save(profile: ProfileConfig): Promise<void> {
    // Ensure config directory exists
    await mkdir(this.configDir, { recursive: true });

    // Normalize API URL
    const normalizedProfile: ProfileConfig = {
      ...profile,
      apiUrl: this.normalizeApiUrl(profile.apiUrl),
    };

    // Validate before saving
    const validated = ProfileConfigSchema.parse(normalizedProfile);

    const filePath = this.getFilePath(profile.profileName);
    const tempPath = `${filePath}.tmp.${randomBytes(8).toString('hex')}`;

    try {
      // Write to temp file first
      await writeFile(tempPath, JSON.stringify(validated, null, 2), 'utf8');

      // Set restrictive permissions (owner read/write only)
      await chmod(tempPath, 0o600);

      // Atomic rename
      await rename(tempPath, filePath);
    } catch (error) {
      // Clean up temp file on error
      try {
        const { unlink } = await import('node:fs/promises');
        await unlink(tempPath);
      } catch {
        // Ignore cleanup errors
      }
      throw error;
    }
  }

  private getFilePath(profileName: string): string {
    return join(this.configDir, `${profileName}.json`);
  }

  private normalizeApiUrl(url: string): string {
    try {
      const parsed = new URL(url);

      // Remove credentials
      parsed.username = '';
      parsed.password = '';

      // Remove search params
      parsed.search = '';

      // Remove hash
      parsed.hash = '';

      // Remove trailing slash
      let normalized = parsed.toString();
      if (normalized.endsWith('/')) {
        normalized = normalized.slice(0, -1);
      }

      return normalized;
    } catch {
      // If URL parsing fails, return as-is
      return url;
    }
  }
}
