import { describe, it, expect, beforeEach } from 'vitest';
import { importService } from './importService';
import { classService } from './classService';
import { ERPStorage } from './storage';

const SESSION = 'sess-2026-27';

const baseRow = {
  'First Name': 'Kabir',
  'Last Name': 'Khanna',
  Gender: 'Male',
  'Date of Birth': '2014-05-15',
  'Blood Group': 'B+',
  Grade: '10',
  Section: 'A',
  'Parent Name': 'Sanjay Khanna',
  'Parent Phone': '+91 98888 12345',
};

const validate = (overrides: Record<string, string>) => {
  const mapped = importService.mapRowToStudent({ ...baseRow, ...overrides }, 2);
  return { mapped, result: importService.validateImportRows([mapped], SESSION) };
};

describe('importService row validation (no silent defaults)', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
  });

  it('accepts a complete row and resolves the class', () => {
    const { result } = validate({});
    expect(result.errors).toEqual([]);
    expect(result.validRows).toHaveLength(1);
    expect(result.validRows[0].targetClassId).toBeTruthy();
  });

  it('reports a missing grade instead of defaulting to Class 10', () => {
    const { mapped, result } = validate({ Grade: '' });
    expect(Number.isNaN(mapped.grade)).toBe(true);
    expect(result.validRows).toHaveLength(0);
    expect(result.errors.some(e => e.field === 'Grade' && /required/i.test(e.message))).toBe(true);
  });

  it('reports an unknown non-numeric grade as a row error', () => {
    const { result } = validate({ Grade: 'Grade X-ish' });
    expect(result.validRows).toHaveLength(0);
    expect(result.errors.some(e => e.field === 'Grade' && e.value === 'Grade X-ish')).toBe(true);
  });

  it('accepts named grades (LKG) when a class with that name exists', () => {
    expect(classService.getBySession(SESSION).some(c => c.name === 'LKG' && c.section === 'A')).toBe(true);
    const { result } = validate({ Grade: 'LKG', 'Date of Birth': '2022-01-10' });
    expect(result.errors).toEqual([]);
    expect(result.validRows[0].targetClassName).toBe('LKG');
  });

  it('reports a missing or unknown gender instead of using "Other"', () => {
    const missing = validate({ Gender: '' });
    expect(missing.mapped.gender).toBe('');
    expect(missing.result.errors.some(e => e.field === 'Gender' && /required/i.test(e.message))).toBe(true);

    const unknown = validate({ Gender: 'xyz' });
    expect(unknown.result.validRows).toHaveLength(0);
    expect(unknown.result.errors.some(e => e.field === 'Gender' && e.value === 'xyz')).toBe(true);

    expect(validate({ Gender: 'F' }).mapped.gender).toBe('Female');
    expect(validate({ Gender: 'Other' }).mapped.gender).toBe('Other');
  });

  it('does not invent a blood group and rejects an invalid one', () => {
    const blank = validate({ 'Blood Group': '' });
    expect(blank.mapped.bloodGroup).toBeUndefined();
    expect(blank.result.errors).toEqual([]);

    const bad = validate({ 'Blood Group': 'Q+' });
    expect(bad.result.validRows).toHaveLength(0);
    expect(bad.result.errors.some(e => e.field === 'Blood Group')).toBe(true);
  });
});
