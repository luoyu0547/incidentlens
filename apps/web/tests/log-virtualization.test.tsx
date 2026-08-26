import type { LogRecordView } from '@incidentlens/protocol';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { VirtualLogViewport } from '../src/logs/VirtualLogViewport';

const record = (id: string): LogRecordView => ({ log_id: id, cursor: id, message: id, occurred_at: id, severity: 'info' });

describe('virtual log viewport', () => {
  it('renders a bounded number of DOM rows for a large history', () => {
    render(<VirtualLogViewport records={Array.from({ length: 10_000 }, (_, i) => record(String(i)))} />);
    expect(screen.getAllByRole('listitem').length).toBeLessThan(100);
  });
  it('uses log id as the row identity and exposes latest navigation', () => {
    render(<VirtualLogViewport records={[record('stable')]} paused unreadCount={3} />);
    expect(screen.getByRole('listitem')).toHaveAttribute('data-log-id', 'stable');
    expect(screen.getByRole('button', { name: '定位最新 (3)' })).toBeVisible();
  });
});
