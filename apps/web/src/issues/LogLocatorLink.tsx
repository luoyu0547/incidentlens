import type { ReactNode } from 'react';
import { evidenceSummaryUrl, logLocatorUrl, type LogLocator } from '../logs/useLogAnchor';

export interface LogLocatorLinkProps {
  readonly locator: LogLocator;
  readonly children?: ReactNode;
  readonly className?: string;
}

/** Link from issue/evidence summaries to the server-provided log locator. */
export function LogLocatorLink({ locator, children = '查看日志', className }: LogLocatorLinkProps) {
  return <a className={className} href={logLocatorUrl(locator)} data-fallback-href={evidenceSummaryUrl(locator)}>
    {children}
  </a>;
}
