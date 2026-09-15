/** Upstream query-token preview compatibility, restricted to our own file endpoints. */
export function authenticatedFileUrl(value: string): string {
  const url = new URL(value, window.location.origin)
  if (url.origin !== window.location.origin || !/^\/api\/v1\/files\/(output|upload)\//.test(url.pathname)) return value
  const token = localStorage.getItem('auth_token')
  if (token) url.searchParams.set('token', token)
  return url.pathname + url.search + url.hash
}
