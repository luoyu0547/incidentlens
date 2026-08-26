import { Outlet } from '@tanstack/react-router';
import { Navigation } from './Navigation';

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
    <div>
      <a href="#main-content">跳转到主要内容</a>
      <header>
        <h1>IncidentLens</h1>
        <Navigation />
        <span>工作区连接状态：未知</span>
      </header>
      <main id="main-content">
        <Outlet />
      </main>
    </div>
  );
}
