import { Link } from 'react-router-dom';

/**
 * Catch-all page shown for any route that matches no defined path. Gives an
 * unknown URL (a mistyped address, a stale bookmark, or a removed route) a clear
 * dead-end with a way back to the live board, instead of rendering a blank page.
 */
export function NotFound() {
  return (
    <div className="page container--lg">
      <h1 className="page-title mb-4">404 &mdash; Page not found</h1>
      <p className="text-muted mb-6" style={{ lineHeight: 'var(--leading-relaxed)' }}>
        The page you are looking for does not exist. It may have been moved or the
        address may be mistyped.
      </p>
      <Link to="/" className="btn btn--primary">
        Back to board
      </Link>
    </div>
  );
}
