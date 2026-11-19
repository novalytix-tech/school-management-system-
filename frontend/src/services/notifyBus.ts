// ==========================================
// notifyBus — lets non-React service code surface a user-visible error.
// Services call notifySyncError(...) instead of silently swallowing a failed
// backend call; the ERP provider listens for SYNC_ERROR_EVENT and shows a toast.
// Identical messages fired in quick succession are de-duplicated so a batch
// operation or a persistent outage can't flood the UI with toasts.
// Developed by NTS Novalytix
// ==========================================

export const SYNC_ERROR_EVENT = 'nts_erp_sync_error';

let lastKey = '';
let lastAt = 0;
const DEDUPE_MS = 4000;

export function notifySyncError(title: string, message?: string): void {
  const key = `${title}|${message || ''}`;
  const now = Date.now();
  if (key === lastKey && now - lastAt < DEDUPE_MS) return;
  lastKey = key;
  lastAt = now;

  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
  try {
    window.dispatchEvent(new CustomEvent(SYNC_ERROR_EVENT, { detail: { title, message } }));
  } catch {
    /* non-browser environment without CustomEvent — safe to ignore */
  }
}
