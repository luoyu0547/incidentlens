/**
 * Catch-all 404 page for unmatched routes.
 *
 * Does not reveal which path was requested or any other details.
 */
export function NotFoundPage() {
  return (
    <div>
      <h2>页面未找到</h2>
      <p>请求的页面不存在。</p>
    </div>
  );
}
