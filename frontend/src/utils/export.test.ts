import { describe, it, expect } from 'vitest';
import { buildCSV, escapeCSVCell, UTF8_BOM } from './export';
import { detectDelimiter, parseCSV } from './csv';

describe('CSV export escaping', () => {
  it('neutralises formula-injection prefixes with an apostrophe', () => {
    expect(escapeCSVCell('=HYPERLINK("http://evil","x")')).toBe(`"'=HYPERLINK(""http://evil"",""x"")"`);
    expect(escapeCSVCell('+cmd')).toBe(`"'+cmd"`);
    expect(escapeCSVCell('-2+3')).toBe(`"'-2+3"`);
    expect(escapeCSVCell('@SUM(A1)')).toBe(`"'@SUM(A1)"`);
    expect(escapeCSVCell('\tTAB')).toBe(`"'\tTAB"`);
    expect(escapeCSVCell('\rCR')).toBe(`"'\rCR"`);
  });

  it('leaves ordinary text and real numbers untouched', () => {
    expect(escapeCSVCell('Aarav Sharma')).toBe('"Aarav Sharma"');
    expect(escapeCSVCell(-250)).toBe('"-250"');
    expect(escapeCSVCell('-250')).toBe('"-250"');
    expect(escapeCSVCell(null)).toBe('""');
    expect(escapeCSVCell(undefined)).toBe('""');
    expect(escapeCSVCell(true)).toBe('"true"');
  });

  it('prepends a UTF-8 BOM and joins rows with CRLF', () => {
    const csv = buildCSV(['Name', 'Fee'], [['रिया', '₹1,200'], ['=1+1', 5]]);
    expect(csv.startsWith(UTF8_BOM)).toBe(true);
    expect(csv.charCodeAt(0)).toBe(0xfeff);
    expect(csv.slice(1).split('\r\n')).toEqual(['"Name","Fee"', '"रिया","₹1,200"', `"'=1+1","5"`]);
  });
});

describe('CSV / TSV paste parsing', () => {
  it('detects tab-separated clipboard text and keeps commas inside cells', () => {
    const tsv = 'Full Name\tAddress\nAarav Sharma\tFlat 4, MG Road, Pune\n';
    expect(detectDelimiter(tsv)).toBe('\t');
    const rows = parseCSV(tsv, detectDelimiter(tsv));
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ 'Full Name': 'Aarav Sharma', Address: 'Flat 4, MG Road, Pune' });
  });

  it('falls back to comma-separated parsing', () => {
    const csv = 'Full Name,Address\nAarav,"Flat 4, Pune"\n';
    expect(detectDelimiter(csv)).toBe(',');
    expect(parseCSV(csv, detectDelimiter(csv))[0]).toMatchObject({ 'Full Name': 'Aarav', Address: 'Flat 4, Pune' });
  });
});
