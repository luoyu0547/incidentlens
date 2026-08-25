import { z } from 'zod';

/**
 * Profile configuration stored in the user's config directory.
 * Tokens are never stored here - they go in OS credential storage.
 */
export const ProfileConfigSchema = z.object({
  profileName: z.string(),
  apiUrl: z.string().url(),
  lastTargetId: z.string().optional(),
  lastSessionId: z.string().optional(),
  lastSequenceBySession: z.record(z.string(), z.number()),
});

export type ProfileConfig = z.infer<typeof ProfileConfigSchema>;

/**
 * Configuration store interface for persisting profile data.
 */
export interface ConfigStore {
  load(profileName: string): Promise<ProfileConfig | null>;
  save(profile: ProfileConfig): Promise<void>;
}

/**
 * Credential store unavailability indicator.
 * Returned when OS keyring is not accessible.
 */
export interface CredentialStoreUnavailable {
  readonly kind: 'CredentialStoreUnavailable';
}

/**
 * Token store interface for secure credential storage.
 * Tokens are stored in OS credential storage, never in plaintext files.
 */
export interface TokenStore {
  get(profileName: string): Promise<string | CredentialStoreUnavailable | null>;
  set(profileName: string, token: string): Promise<CredentialStoreUnavailable | void>;
  delete(profileName: string): Promise<CredentialStoreUnavailable | void>;
}
