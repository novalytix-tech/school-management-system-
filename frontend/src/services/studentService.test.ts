import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { studentService } from './studentService';
import { ERPStorage } from './storage';
import { httpClient } from './api/httpClient';

describe('studentService Student Lifecycle Management', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('retrieves all seeded students and fetches by ID', () => {
    const students = studentService.getAll();
    expect(students.length).toBeGreaterThan(0);

    const first = students[0];
    const retrieved = studentService.getById(first.id);
    expect(retrieved).toBeDefined();
    expect(retrieved?.admissionNo).toBe(first.admissionNo);
  });

  it('filters students by class and section', () => {
    const classStudents = studentService.getByClassAndSection('cls-10a', 'A');
    expect(Array.isArray(classStudents)).toBe(true);
    classStudents.forEach(s => {
      expect(s.classId).toBe('cls-10a');
      expect(s.section.toUpperCase()).toBe('A');
    });
  });

  it('creates a new student and rejects duplicate admission numbers', () => {
    const newStudentData = {
      admissionNo: 'ADM-TEST-999',
      firstName: 'Kabir',
      lastName: 'Khanna',
      fullName: 'Kabir Khanna',
      gender: 'Male' as const,
      dob: '2010-05-15',
      classId: 'cls-10a',
      className: 'Class 10A',
      section: 'A',
      rollNo: '45',
      academicYear: '2026-2027',
      admissionDate: '2026-04-01',
      parentName: 'Sanjay Khanna',
      parentPhone: '+91 98888 12345',
      parentEmail: 'sanjay.khanna@test.com',
      address: 'Civil Lines, Kanpur',
      status: 'active' as const,
      bloodGroup: 'B+',
      category: 'General',
    };

    const res = studentService.create(newStudentData);
    expect(res.success).toBe(true);
    expect(res.data?.id).toBeDefined();
    expect(res.data?.fullName).toBe('Kabir Khanna');

    // Duplicate admission number must be rejected
    const duplicateRes = studentService.create(newStudentData);
    expect(duplicateRes.success).toBe(false);
    expect(duplicateRes.error).toContain('already exists');
  });

  it('updates an existing student record and changes status', () => {
    const students = studentService.getAll();
    const target = students[0];

    const updateRes = studentService.update(target.id, {
      rollNo: '99',
      parentPhone: '+91 99999 00000',
    });

    expect(updateRes.success).toBe(true);
    expect(updateRes.data?.rollNo).toBe('99');
    expect(updateRes.data?.parentPhone).toBe('+91 99999 00000');

    // Toggle active / inactive status
    const initialStatus = updateRes.data?.status;
    const toggled = studentService.toggleStatus(target.id);
    expect(toggled?.status).not.toBe(initialStatus);
  });

  it('deletes a locally-created (unsynced) student from storage without a backend call', async () => {
    const students = studentService.getAll();
    const targetId = students[0].id; // seed ids are local, not database UUIDs

    const deleteSpy = vi.spyOn(httpClient, 'delete');
    const res = await studentService.delete(targetId);

    expect(res.success).toBe(true);
    expect(deleteSpy).not.toHaveBeenCalled(); // non-UUID id => local-only removal
    expect(studentService.getById(targetId)).toBeUndefined();
  });

  it('sends DELETE to the backend for a DB-backed student and keeps it locally if the server fails', async () => {
    // Inject a student whose id is a real database UUID (as sync/store would hydrate).
    const dbId = '11111111-1111-4111-8111-111111111111';
    const store = ERPStorage.get();
    store.students = [{ ...store.students[0], id: dbId, admissionNo: 'DB-UUID-1' }, ...store.students];
    ERPStorage.set(store);

    // Server rejects the delete -> student must NOT be removed locally (no ghost).
    const failSpy = vi.spyOn(httpClient, 'delete').mockResolvedValue({
      success: false,
      data: null as any,
      error: { code: 'NETWORK_ERROR', message: 'Server unavailable' },
    });
    const failRes = await studentService.delete(dbId);
    expect(failSpy).toHaveBeenCalledWith(`/students/${dbId}`);
    expect(failRes.success).toBe(false);
    expect(failRes.error).toBeTruthy();
    expect(studentService.getById(dbId)).toBeDefined();

    // Server confirms the delete -> student is removed locally.
    vi.spyOn(httpClient, 'delete').mockResolvedValue({ success: true, data: null as any });
    const okRes = await studentService.delete(dbId);
    expect(okRes.success).toBe(true);
    expect(studentService.getById(dbId)).toBeUndefined();
  });
});
