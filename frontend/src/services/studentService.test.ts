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

const newStudentInput = (admissionNo: string) => ({
  admissionNo,
  firstName: 'Ira',
  lastName: 'Menon',
  fullName: 'Ira Menon',
  gender: 'Female' as const,
  dob: '2012-02-02',
  classId: 'cls-10a',
  className: 'Class 10',
  section: 'A',
  rollNo: '',
  academicYear: '2026-2027',
  admissionDate: '2026-04-01',
  parentName: 'Ravi Menon',
  parentPhone: '+91 90000 11111',
  parentEmail: 'ravi@test.com',
  address: 'Kochi',
  status: 'active' as const,
});

describe('studentService with a backend session', () => {
  const SERVER_ID = '22222222-2222-4222-8222-222222222222';

  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    httpClient.setToken('session-token');
  });

  afterEach(() => {
    httpClient.setToken(null);
    vi.restoreAllMocks();
  });

  it('replaces the local stud-… id with the server id after a successful create', async () => {
    const postSpy = vi.spyOn(httpClient, 'post').mockResolvedValue({ success: true, data: { id: SERVER_ID } });
    const res = studentService.create(newStudentInput('ADM-SYNC-1'));
    const localId = res.data!.id;
    expect(localId).toMatch(/^stud-/);

    // Something else in the store references the local id (e.g. an admission).
    const store = ERPStorage.get();
    (store.admissions as any[]).push({ id: 'adm-x', enrolledStudentId: localId });
    ERPStorage.set(store);

    const synced = await res.synced!;
    expect(synced).toEqual({ success: true, id: SERVER_ID });
    expect(postSpy.mock.calls[0][0]).toBe('/students');
    expect((postSpy.mock.calls[0][1] as any).id).toBeUndefined();
    expect((postSpy.mock.calls[0][1] as any).status).toBe('ACTIVE');

    expect(studentService.getById(localId)).toBeUndefined();
    expect(studentService.getById(SERVER_ID)?.admissionNo).toBe('ADM-SYNC-1');
    expect((ERPStorage.get().admissions as any[]).find(a => a.id === 'adm-x').enrolledStudentId).toBe(SERVER_ID);

    // Later updates/deletes now target the real server record.
    const putSpy = vi.spyOn(httpClient, 'put').mockResolvedValue({ success: true, data: null as any });
    studentService.update(SERVER_ID, { parentPhone: '+91 90000 22222' });
    expect(putSpy).toHaveBeenCalledWith(`/students/${SERVER_ID}`, { parentPhone: '+91 90000 22222' });
  });

  it('rolls back the local insert and surfaces the error when the create fails', async () => {
    vi.spyOn(httpClient, 'post').mockResolvedValue({
      success: false,
      data: null as any,
      error: { code: 'SECTION_REQUIRED', message: 'A valid section is required' },
    });
    const toast = vi.fn();
    window.addEventListener('nts_erp_sync_error', toast);
    const res = studentService.create(newStudentInput('ADM-SYNC-FAIL'));
    const synced = await res.synced!;
    window.removeEventListener('nts_erp_sync_error', toast);

    expect(synced.success).toBe(false);
    expect(synced.error).toBe('A valid section is required');
    expect(studentService.getById(res.data!.id)).toBeUndefined();
    expect(studentService.getByAdmissionNo('ADM-SYNC-FAIL')).toBeUndefined();
    expect(toast).toHaveBeenCalled();
  });

  it('toggleStatus PUTs the backend status for server ids and reverts on failure', async () => {
    const store = ERPStorage.get();
    store.students = [{ ...store.students[0], id: SERVER_ID, admissionNo: 'DB-T-1', status: 'active' }, ...store.students];
    ERPStorage.set(store);

    let resolvePut!: (v: any) => void;
    const putSpy = vi.spyOn(httpClient, 'put').mockImplementation(() => new Promise(r => { resolvePut = r; }));
    const toggled = studentService.toggleStatus(SERVER_ID);
    expect(toggled?.status).toBe('inactive');
    expect(putSpy).toHaveBeenCalledWith(`/students/${SERVER_ID}`, { status: 'WITHDRAWN' });

    resolvePut({ success: false, data: null, error: { code: 'HTTP_500', message: 'boom' } });
    await new Promise(r => setTimeout(r, 0));
    expect(studentService.getById(SERVER_ID)?.status).toBe('active');
  });

  it('does not PUT local/demo ids', () => {
    const putSpy = vi.spyOn(httpClient, 'put');
    studentService.toggleStatus(studentService.getAll()[0].id);
    studentService.update(studentService.getAll()[0].id, { address: 'x' });
    expect(putSpy).not.toHaveBeenCalled();
  });
});

describe('studentService realtime changes', () => {
  const ID = '33333333-3333-4333-8333-333333333333';
  const INST = '44444444-4444-4444-8444-444444444444';

  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    const store = ERPStorage.get();
    store.students = [
      { ...store.students[0], id: ID, admissionNo: 'RT-1', fullName: 'Old Name', institutionId: INST },
      { ...store.students[1], id: 'stud-other-school', admissionNo: 'RT-2', institutionId: 'inst-other' },
      ...store.students.slice(2),
    ];
    ERPStorage.set(store);
  });

  afterEach(() => vi.restoreAllMocks());

  it('applies "updated" events to the existing student by server id', async () => {
    vi.spyOn(httpClient, 'get').mockResolvedValue({
      success: true,
      data: { students: [{ id: ID, admissionNo: 'RT-1', fullName: 'New Name', className: 'Class 10', institutionId: INST }] },
    });
    await studentService.applyRealtimeChange({ action: 'updated', data: { id: ID, admission_number: 'RT-1' } }, INST);
    expect(studentService.getById(ID)?.fullName).toBe('New Name');
  });

  it('"deleted" removes by id and never touches another institution with the same admission number', async () => {
    await studentService.applyRealtimeChange(
      { action: 'deleted', data: { id: '99999999-9999-4999-8999-999999999999', admission_number: 'RT-2' } },
      INST
    );
    expect(studentService.getById('stud-other-school')).toBeDefined();

    await studentService.applyRealtimeChange({ action: 'deleted', data: { id: ID, admission_number: 'RT-1' } }, INST);
    expect(studentService.getById(ID)).toBeUndefined();
  });
});

describe('ERPStorage backend sync', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    httpClient.setToken(null);
  });

  afterEach(() => vi.restoreAllMocks());

  it('set() no longer pushes the whole student list to /sync/store', () => {
    const postSpy = vi.spyOn(httpClient, 'post');
    ERPStorage.set(ERPStorage.get());
    expect(postSpy).not.toHaveBeenCalled();
  });

  it('syncFromBackend() is skipped without a token', async () => {
    const getSpy = vi.spyOn(httpClient, 'get');
    await expect(ERPStorage.syncFromBackend()).resolves.toBe(false);
    expect(getSpy).not.toHaveBeenCalled();
  });
});
