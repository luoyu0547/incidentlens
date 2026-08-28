import { Link } from '@tanstack/react-router';

/**
 * Top-level workspace navigation.
 *
 * All navigation labels are in Chinese. This component renders only links to
 * top-level pages; detail pages (service, issue detail, investigation) are
 * reached through content links within those pages.
 *
 * No mutation controls (approve/reject/execute/restart/rollback) are present.
 */
export function Navigation() {
  return (
    <nav className="app-shell__nav" aria-label="主导航">
      <Link to="/">总览</Link>
      <Link to="/issues">问题</Link>
    </nav>
  );
}
