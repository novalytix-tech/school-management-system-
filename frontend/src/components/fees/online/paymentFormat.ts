// ==========================================
// Formatting helpers for the server-backed fee portal.
// Developed by NTS Novalytix
// ==========================================

import { formatCurrencyINRWithDecimals } from '../../../utils/formatters';

/** ₹ with en-IN grouping and 2 decimals. */
export const money = (n: number | null | undefined): string => formatCurrencyINRWithDecimals(Number(n) || 0);

/** ISO timestamp in the user's locale (date + time). */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** ISO date (or timestamp) in the user's locale (date only). */
export function dateOnly(iso: string | null | undefined): string {
  if (!iso) return '—';
  // Bare YYYY-MM-DD: parse as a local date so it never shifts a day.
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { dateStyle: 'medium' });
}

const BADGE: Record<string, string> = {
  SUCCEEDED: 'badge-success',
  SUCCESS: 'badge-success',
  PAID: 'badge-success',
  PROCESSED: 'badge-success',
  PARTIALLY_PAID: 'badge-warning',
  PARTIAL: 'badge-warning',
  PARTIALLY_REFUNDED: 'badge-purple',
  REFUNDED: 'badge-purple',
  PENDING: 'badge-warning',
  CREATED: 'badge-info',
  INITIATED: 'badge-info',
  UNPAID: 'badge-neutral',
  ISSUED: 'badge-info',
  OVERDUE: 'badge-danger',
  FAILED: 'badge-danger',
  CANCELLED: 'badge-neutral',
  EXPIRED: 'badge-neutral',
  NEEDS_REVIEW: 'badge-warning',
};

export const badgeClass = (status: string | null | undefined): string =>
  `badge ${BADGE[String(status || '').toUpperCase()] || 'badge-neutral'}`;

/** "PARTIALLY_PAID" → "Partially paid". */
export function statusLabel(status: string | null | undefined): string {
  const s = String(status || '').replace(/_/g, ' ').toLowerCase();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '—';
}
