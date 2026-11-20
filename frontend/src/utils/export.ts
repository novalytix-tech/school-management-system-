// ==========================================
// School ERP CSV & JSON Export / Backup Utilities
// Developed by NTS Novalytix
// ==========================================

type CSVCell = string | number | boolean | null | undefined;

/** UTF-8 byte-order mark: makes Excel decode non-ASCII text (₹, Hindi names) correctly. */
export const UTF8_BOM = String.fromCharCode(0xfeff);

// Characters that make spreadsheet apps (Excel, Sheets, LibreOffice) treat a
// cell as a formula: = + - @ and a leading tab / carriage return.
const FORMULA_TRIGGER = /^[=+\-@\t\r]/;
const PLAIN_NUMBER = /^[-+]?\d+(\.\d+)?$/;

/**
 * Quotes one CSV cell and neutralises CSV/formula injection: text that starts
 * with a formula trigger is prefixed with an apostrophe so it is shown as text
 * instead of being evaluated. Plain numbers (e.g. -250) are left untouched.
 */
export function escapeCSVCell(val: CSVCell): string {
  if (val === null || val === undefined) return '""';
  let str = String(val);
  if (typeof val === 'string' && FORMULA_TRIGGER.test(str) && !PLAIN_NUMBER.test(str)) {
    str = `'${str}`;
  }
  return `"${str.replace(/"/g, '""')}"`;
}

/** Builds the CSV document (BOM + CRLF rows) that exportToCSV downloads. */
export function buildCSV(headers: string[], rows: CSVCell[][]): string {
  const headerRow = headers.map(escapeCSVCell).join(',');
  const dataRows = rows.map(row => row.map(escapeCSVCell).join(','));
  return UTF8_BOM + [headerRow, ...dataRows].join('\r\n');
}

export function exportToCSV(filename: string, headers: string[], rows: CSVCell[][]): void {
  const csvContent = buildCSV(headers, rows);

  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  const url = URL.createObjectURL(blob);
  link.setAttribute('href', url);
  link.setAttribute('download', `${filename}_${new Date().toISOString().split('T')[0]}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function downloadJSONBackup(data: unknown, filename = 'School_ERP_NTS_Backup'): void {
  const jsonStr = JSON.stringify(data, null, 2);
  const blob = new Blob([jsonStr], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.setAttribute('href', url);
  link.setAttribute('download', `${filename}_${new Date().toISOString().split('T')[0]}.json`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

