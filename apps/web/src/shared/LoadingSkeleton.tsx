export interface LoadingSkeletonProps { readonly label?: string; readonly lines?: number; }

export function LoadingSkeleton({ label = '正在加载', lines = 3 }: LoadingSkeletonProps) {
  return <div role="status" aria-label={label} aria-busy="true" className="loading-skeleton">{Array.from({ length: lines }, (_, index) => <div key={index} aria-hidden="true" />)}</div>;
}
