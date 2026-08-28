import { open, readFile, rename, chmod, mkdir, unlink } from 'node:fs/promises';
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
      const result = ProfileConfigSchema.safeParse(raw);

      if (!result.success || result.data.profileName !== profileName) {
        return null;
      }
      return result.data;
    } catch {
      return null;
    }
  }

  async save(profile: ProfileConfig): Promise<void> {
    await mkdir(this.configDir, { recursive: true, mode: 0o700 });
    // mkdir does not tighten an existing directory, so enforce the mode too.
    await chmod(this.configDir, 0o700);

    const normalizedProfile: ProfileConfig = {
      ...profile,
      apiUrl: this.normalizeApiUrl(profile.apiUrl),
    };
    const validated = ProfileConfigSchema.parse(normalizedProfile);
    const filePath = this.getFilePath(validated.profileName);
    const tempPath = `${filePath}.tmp.${randomBytes(8).toString('hex')}`;

    try {
      const handle = await open(tempPath, 'wx', 0o600);
      try {
        await handle.writeFile(JSON.stringify(validated, null, 2), 'utf8');
        await handle.chmod(0o600);
        await handle.sync();
      } finally {
        await handle.close();
      }
      await rename(tempPath, filePath);

      // Sync the containing directory so the replacement survives a power loss.
      const directory = await open(this.configDir, 'r');
      try {
        await directory.sync();
      } finally {
        await directory.close();
      }
    } catch (error) {
      await unlink(tempPath).catch(() => undefined);
      throw error;
    }
  }

  private getFilePath(profileName: string): string {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(profileName)) {
      throw new Error('Invalid profile name');
    }
    return join(this.configDir, `${profileName}.json`);
  }

  private normalizeApiUrl(url: string): string {
    const parsed = new URL(url);
    parsed.username = '';
    parsed.password = '';
    parsed.search = '';
    parsed.hash = '';
    parsed.pathname = parsed.pathname.replace(/\/+$/, '');
    return parsed.toString().replace(/\/$/, '');
  }
}
