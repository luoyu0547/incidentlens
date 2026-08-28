export interface ErrorNoticeProps { readonly title?: string; readonly message?: string; }

export function ErrorNotice({ title = '加载失败', message = '暂时无法加载此内容，请稍后重试。' }: ErrorNoticeProps) {
  return <section role="alert" aria-label={title}><h3>{title}</h3><p>{message}</p></section>;
}
