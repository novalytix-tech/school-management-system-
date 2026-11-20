// ==========================================
// Pagination helpers shared by list pages.
// ==========================================

import { useEffect, useRef } from 'react';

/** Highest valid page for `total` records (always >= 1). */
export function maxPage(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
}

/**
 * Keeps `page` inside [1, maxPage] when the record count shrinks (e.g. after a
 * delete removes the last row of the final page) so the list never shows an
 * empty page.
 */
export function useClampPage(
  page: number,
  setPage: (page: number) => void,
  total: number,
  pageSize: number
): void {
  useEffect(() => {
    const last = maxPage(total, pageSize);
    if (page > last) setPage(last);
  }, [page, setPage, total, pageSize]);
}

/**
 * Resets to page 1 whenever any of `deps` (filters, search text …) changes,
 * skipping the initial render.
 */
export function useResetPageOn(setPage: (page: number) => void, deps: readonly unknown[]): void {
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
