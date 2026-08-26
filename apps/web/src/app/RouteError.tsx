/**
 * Default route error boundary.
 *
 * Renders a user-facing error message without exposing raw response bodies,
 * internal stack traces, or any implementation detail. The error object is
 * deliberately not rendered so no sensitive information leaks.
 */
export function RouteError() {
  return (
    <div role="alert">
      <h2>页面加载错误</h2>
      <p>加载页面时出现问题，请稍后重试。</p>
    </div>
  );
}
