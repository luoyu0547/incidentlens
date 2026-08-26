/**
 * Target configuration wizard for IncidentLens CLI.
 *
 * Walks a user through name / host / user / port / authentication
 * reference / host-key policy in a linear, keyboard-driven flow.
 *
 * Security contract:
 * - The wizard collects only metadata and an opaque `authentication_ref`
 *   string (for example `ssh-agent:user@host` or a named profile). It
 *   never offers a private-key input and never forwards key material.
 * - Each wizard session pins a stable idempotency key, so a failed
 *   submit retried after a network error is deduplicated server-side.
 * - The review step renders the auth reference as a masked placeholder.
 *
 * Input handling reads current values through refs (updated every
 * render) because Ink's `useInput` subscribes a handler per render and
 * closure state can otherwise lag one keystroke behind.
 */

import React, { useMemo, useRef, useState } from 'react';
import { Box, Text, useInput } from 'ink';
import type { TargetCreate, TargetPatch, TargetView } from '@incidentlens/protocol';
import { TargetController } from '../features/targets/target-controller.js';
import { createIdempotencyKey } from '../api/idempotency.js';

export type TargetWizardMode = 'create' | 'edit';

/**
 * Mutable draft collected by the wizard.
 * `sshPort` is kept editable as a string until submission.
 */
export interface TargetDraft {
  readonly name: string;
  readonly host: string;
  readonly sshUser: string;
  readonly sshPort: string;
  readonly authenticationRef: string;
  readonly hostKeyPolicy: 'strict' | 'pinned';
  readonly pinnedHostKeySha256: string | null;
}

export interface TargetWizardProps {
  readonly mode: TargetWizardMode;
  /** Existing target when editing; undefined when creating. */
  readonly target?: TargetView;
  readonly controller: TargetController;
  readonly onComplete: (target: TargetView) => void;
  readonly onCancel: () => void;
}

/**
 * Ordered wizard fields. `pin` only appears when the host-key policy is
 * `pinned`; `review` is the summary before submission.
 */
type WizardField = 'name' | 'host' | 'user' | 'port' | 'auth' | 'policy' | 'pin' | 'review';

const FIELD_LABELS: Record<WizardField, string> = {
  name: 'Target name',
  host: 'Host',
  user: 'SSH user',
  port: 'SSH port',
  auth: 'Authentication reference',
  policy: 'Host key policy',
  pin: 'Pinned host key SHA-256',
  review: 'Review',
};

const FIELD_HINTS: Record<WizardField, string | undefined> = {
  name: 'A short display name, e.g. production',
  host: 'Hostname or IP address',
  user: 'SSH login user',
  port: 'Default: 22',
  auth: 'Opaque reference only (e.g. ssh-agent:user@host) — never paste keys or credentials',
  policy: '1 = strict, 2 = pinned',
  pin: 'SHA-256 fingerprint, e.g. sha256:...',
  review: 'Enter to save, e to edit, esc to cancel',
};

/**
 * Build the initial draft from an existing target (edit) or empty values.
 */
function createInitialDraft(target?: TargetView): TargetDraft {
  return {
    name: target?.name ?? '',
    host: target?.host ?? '',
    sshUser: target?.ssh_user ?? '',
    sshPort: target ? String(target.ssh_port) : '22',
    authenticationRef: '',
    hostKeyPolicy: target?.host_key_policy ?? 'strict',
    pinnedHostKeySha256: target?.pinned_host_key_sha256 ?? null,
  };
}

/**
 * Target configuration wizard.
 */
export function TargetWizard({
  mode,
  target,
  controller,
  onComplete,
  onCancel,
}: TargetWizardProps): React.ReactElement {
  const [field, setField] = useState<WizardField>('name');
  const [draft, setDraft] = useState<TargetDraft>(() => createInitialDraft(target));
  const [error, setError] = useState<string | undefined>(undefined);
  const [busy, setBusy] = useState(false);

  // Latest-value refs so the useInput handler never reads stale state.
  const fieldRef = useRef<WizardField>(field);
  fieldRef.current = field;
  const draftRef = useRef<TargetDraft>(draft);
  draftRef.current = draft;
  const busyRef = useRef<boolean>(busy);
  busyRef.current = busy;
  const modeRef = useRef<TargetWizardMode>(mode);
  modeRef.current = mode;
  const targetRef = useRef<TargetView | undefined>(target);
  targetRef.current = target;

  // Stable per-session idempotency key: retries after a network failure
  // reuse it, so the server deduplicates the mutation.
  const idempotencyKey = useMemo(() => createIdempotencyKey(), []);

  const patch = (part: Partial<TargetDraft>): void => {
    setDraft((current) => ({ ...current, ...part }));
  };

  const updateCurrentField = (transform: (value: string) => string): void => {
    switch (fieldRef.current) {
      case 'name':
        setDraft((c) => ({ ...c, name: transform(c.name) }));
        break;
      case 'host':
        setDraft((c) => ({ ...c, host: transform(c.host) }));
        break;
      case 'user':
        setDraft((c) => ({ ...c, sshUser: transform(c.sshUser) }));
        break;
      case 'port':
        setDraft((c) => ({ ...c, sshPort: transform(c.sshPort) }));
        break;
      case 'auth':
        setDraft((c) => ({ ...c, authenticationRef: transform(c.authenticationRef) }));
        break;
      case 'pin':
        setDraft((c) => ({
          ...c,
          pinnedHostKeySha256: transform(c.pinnedHostKeySha256 ?? ''),
        }));
        break;
      default:
        break;
    }
  };

  const validateAndAdvance = (): void => {
    const current = fieldRef.current;
    const currentDraft = draftRef.current;
    const currentMode = modeRef.current;

    const next: WizardField | undefined = (() => {
      switch (current) {
        case 'name':
          if (currentDraft.name.trim() === '') {
            setError('Name is required.');
            return undefined;
          }
          return 'host';
        case 'host':
          if (currentDraft.host.trim() === '') {
            setError('Host is required.');
            return undefined;
          }
          return 'user';
        case 'user':
          if (currentDraft.sshUser.trim() === '') {
            setError('SSH user is required.');
            return undefined;
          }
          return 'port';
        case 'port': {
          const port = Number(currentDraft.sshPort);
          if (!Number.isInteger(port) || port < 1 || port > 65535) {
            setError('Port must be an integer between 1 and 65535.');
            return undefined;
          }
          return 'auth';
        }
        case 'auth':
          if (currentMode === 'create' && currentDraft.authenticationRef.trim() === '') {
            setError('An authentication reference is required.');
            return undefined;
          }
          return 'policy';
        case 'policy':
          return currentDraft.hostKeyPolicy === 'pinned' ? 'pin' : 'review';
        case 'pin':
          if ((currentDraft.pinnedHostKeySha256 ?? '').trim() === '') {
            setError('A SHA-256 fingerprint is required for pinned policy.');
            return undefined;
          }
          return 'review';
        case 'review':
          return undefined;
      }
    })();

    if (next === undefined) {
      if (current === 'review') {
        void submit();
      }
      return;
    }

    setError(undefined);
    setField(next);
  };

  const submit = async (): Promise<void> => {
    if (busyRef.current) {
      return;
    }

    const currentMode = modeRef.current;
    const currentTarget = targetRef.current;
    const currentDraft = draftRef.current;

    if (currentMode === 'edit' && !currentTarget) {
      onCancel();
      return;
    }

    setBusy(true);
    setError(undefined);

    try {
      if (currentMode === 'create') {
        const input: TargetCreate = {
          name: currentDraft.name.trim(),
          host: currentDraft.host.trim(),
          ssh_user: currentDraft.sshUser.trim(),
          ssh_port: Number(currentDraft.sshPort),
          authentication_ref: currentDraft.authenticationRef.trim(),
          host_key_policy: currentDraft.hostKeyPolicy,
          pinned_host_key_sha256:
            currentDraft.hostKeyPolicy === 'pinned' ? currentDraft.pinnedHostKeySha256 : null,
        };
        const created = await controller.create(input, idempotencyKey);
        onComplete(created);
      } else {
        const patchInput: TargetPatch = {
          expected_version: currentTarget?.version ?? 0,
          name: currentDraft.name.trim(),
          host: currentDraft.host.trim(),
          ssh_user: currentDraft.sshUser.trim(),
          ssh_port: Number(currentDraft.sshPort),
          host_key_policy: currentDraft.hostKeyPolicy,
          pinned_host_key_sha256:
            currentDraft.hostKeyPolicy === 'pinned' ? currentDraft.pinnedHostKeySha256 : null,
          // An empty auth reference means "keep the existing server-side ref".
          authentication_ref:
            currentDraft.authenticationRef.trim() === ''
              ? null
              : currentDraft.authenticationRef.trim(),
        };
        const updated = await controller.update(
          currentTarget?.target_id ?? '',
          patchInput,
          idempotencyKey
        );
        onComplete(updated);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to save target';
      setError(message);
      setBusy(false);
    }
  };

  useInput((input, key) => {
    if (busyRef.current) {
      return;
    }

    if (key.escape) {
      onCancel();
      return;
    }

    if (key.return) {
      validateAndAdvance();
      return;
    }

    const currentField = fieldRef.current;

    if (currentField === 'review') {
      if (input.toLowerCase() === 'e') {
        setError(undefined);
        setField('name');
      }
      return;
    }

    if (key.backspace || key.delete) {
      updateCurrentField((value) => value.slice(0, -1));
      return;
    }

    if (currentField === 'policy') {
      if (input === '1') {
        patch({ hostKeyPolicy: 'strict' });
      } else if (input === '2') {
        patch({ hostKeyPolicy: 'pinned' });
      }
      return;
    }

    if (currentField === 'port') {
      if (input.length === 1 && /^[0-9]$/.test(input)) {
        setDraft((c) => ({ ...c, sshPort: c.sshPort === '' ? input : c.sshPort + input }));
      }
      return;
    }

    if (input.length > 0) {
      updateCurrentField((value) => value + input);
    }
  });

  const displayValue = ((): string => {
    switch (field) {
      case 'name':
        return draft.name;
      case 'host':
        return draft.host;
      case 'user':
        return draft.sshUser;
      case 'port':
        return draft.sshPort;
      case 'auth':
        return draft.authenticationRef;
      case 'policy':
        return draft.hostKeyPolicy === 'strict' ? 'strict (1)' : 'pinned (2)';
      case 'pin':
        return draft.pinnedHostKeySha256 ?? '';
      case 'review':
        return '';
    }
  })();

  const hint = busy ? 'Saving…' : (FIELD_HINTS[field] ?? '');

  return (
    <Box flexDirection="column">
      <Text bold color="blue">
        {mode === 'create' ? 'New Target' : `Edit Target: ${target?.name ?? ''}`}
      </Text>

      {field === 'review' ? (
        <Box flexDirection="column">
          <Text color="gray">Review</Text>
          <Text>{`name:        ${draft.name}`}</Text>
          <Text>{`host:        ${draft.sshUser}@${draft.host}:${draft.sshPort}`}</Text>
          <Text>
            {`auth:        ${draft.authenticationRef.trim() === '' ? '(keep current)' : referenceMask}`}
          </Text>
          <Text>
            {`host-key:    ${draft.hostKeyPolicy}${draft.hostKeyPolicy === 'pinned' ? ` ${draft.pinnedHostKeySha256 ?? ''}` : ''}`}
          </Text>
        </Box>
      ) : (
        <Box flexDirection="column">
          <Text color="gray">{FIELD_LABELS[field]}</Text>
          <Box>
            <Text color={busy ? 'yellow' : 'blue'} bold>
              {'> '}
            </Text>
            {displayValue === '' ? (
              <Text color="gray">{field === 'policy' ? '' : placeholderFor(field)}</Text>
            ) : (
              <Text>{displayValue}</Text>
            )}
          </Box>
          {field === 'policy' && (
            <Box flexDirection="column" marginTop={0}>
              <Text color={draft.hostKeyPolicy === 'strict' ? 'green' : 'gray'}>
                strict (1) — verify host key against known_hosts
              </Text>
              <Text color={draft.hostKeyPolicy === 'pinned' ? 'green' : 'gray'}>
                pinned (2) — pin the exact host-key fingerprint
              </Text>
            </Box>
          )}
        </Box>
      )}

      <Text color="gray">{hint}</Text>
      {error && <Text color="red">{error}</Text>}
    </Box>
  );
}

/**
 * Masked rendering of the collected authentication reference — the CLI
 * never echoes the full value once it has been entered.
 */
const referenceMask = '●●● (reference set)';

/**
 * Per-field placeholder for empty inputs.
 */
function placeholderFor(field: WizardField): string {
  switch (field) {
    case 'name':
      return 'production';
    case 'host':
      return 'web-01.example.com';
    case 'user':
      return 'deploy';
    case 'port':
      return '22';
    case 'auth':
      return 'ssh-agent:user@host';
    case 'pin':
      return 'sha256:...';
    default:
      return '';
  }
}

/**
 * Typed deletion confirmation prompt.
 *
 * The remove action only fires once the user has typed the exact target
 * name — an explicit, deliberate confirmation.
 */
export interface RemoveTargetPromptProps {
  readonly target: TargetView;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}

/**
 * Typed deletion confirmation overlay.
 */
export function RemoveTargetPrompt({
  target,
  onConfirm,
  onCancel,
}: RemoveTargetPromptProps): React.ReactElement {
  const [value, setValue] = useState('');

  // Latest-value ref so the handler never reads a stale typed value.
  const valueRef = useRef<string>(value);
  valueRef.current = value;

  useInput((input, key) => {
    if (key.escape) {
      onCancel();
      return;
    }

    if (key.return) {
      if (valueRef.current === target.name) {
        onConfirm();
      }
      return;
    }

    if (key.backspace || key.delete) {
      setValue((current) => current.slice(0, -1));
      return;
    }

    if (input.length > 0) {
      setValue((current) => current + input);
    }
  });

  const matches = value === target.name;

  return (
    <Box flexDirection="column">
      <Text color="red" bold>
        {`Remove target ${target.name}?`}
      </Text>
      <Text color="gray">Type the target name to confirm. This cannot be undone.</Text>
      <Box>
        <Text color={matches ? 'green' : 'blue'} bold>
          {'> '}
        </Text>
        <Text>{value}</Text>
      </Box>
      <Text color={matches ? 'green' : 'gray'}>
        {matches ? 'Press Enter to remove.' : 'Name does not match yet.'}
      </Text>
    </Box>
  );
}
