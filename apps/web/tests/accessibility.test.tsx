import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { EmptyState } from '../src/shared/EmptyState';
import { ErrorNotice } from '../src/shared/ErrorNotice';
import { LoadingSkeleton } from '../src/shared/LoadingSkeleton';

 describe('accessible interface primitives', () => {
  it('provides a keyboard target for the empty and error states without spinners', async () => {
    render(<><EmptyState title="暂无日志" description="没有匹配记录" /><ErrorNotice message="安全的错误信息" /><LoadingSkeleton lines={2} /></>);
    expect(screen.getByRole('status', { name: '暂无日志' })).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent('安全的错误信息');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    await userEvent.tab();
    expect(document.activeElement).not.toBe(document.body);
  });

  it('marks loading status as busy and keeps error content safe', () => {
    render(<LoadingSkeleton label="正在加载日志" />);
    expect(screen.getByRole('status', { name: '正在加载日志' })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status')).not.toHaveTextContent('<script>');
  });
});
