import { Outlet } from '@tanstack/react-router';
import { Navigation } from './Navigation';
import { WorkspaceEventBridge } from './WorkspaceEventBridge';

/**
 * Root application layout (App Shell).
 *
 * Provides:
 * - A skip-to-main-content link for keyboard/assistive technology users
 * - The workspace heading (IncidentLens)
 * - Top-level navigation (Chinese labels, no mutation controls)
 * - A workspace connection status location (placeholder for a later task)
 * - The content outlet where matched route components render
 */
export function AppShell() {
  return (
    <div className="app-shell">
      <a className="app-shell__skip-link" href="#main-content">跳转到主要内容</a>
      <header className="app-shell__header">
        <h1>IncidentLens</h1>
        <Navigation />
        <WorkspaceEventBridge />
      </header>
      <main className="app-shell__main" id="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
