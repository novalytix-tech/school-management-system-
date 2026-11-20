// ==========================================
// Id helpers — tell server (PostgreSQL UUID) ids apart from local/demo ids
// such as stud-1, inv-123 or cls-10a.
// ==========================================

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True when `id` is a database UUID, i.e. the record exists on the server. */
export function isServerId(id: string | null | undefined): boolean {
  return !!id && UUID_RE.test(id);
}
