// ==========================================
// Student Service - Mock Implementation
// Developed by NTS Novalytix
// ==========================================

import { Student } from '../types';
import { ERPStorage } from './storage';
import { httpClient } from './api/httpClient';
import { notifySyncError } from './notifyBus';
import { recycleBinService } from './recycleBinService';
import { membershipService } from './membershipService';
import { isServerId } from '../utils/ids';

/** Outcome of the background server sync that follows an optimistic create. */
export interface StudentSyncResult {
  success: boolean;
  /** The id the student now has locally (the server UUID once synced). */
  id?: string;
  error?: string;
}

/** Maps the UI's student status onto the backend's Student.status vocabulary. */
export function toBackendStudentStatus(status: string | undefined): string | undefined {
  if (!status) return undefined;
  const s = status.toLowerCase();
  if (s === 'active') return 'ACTIVE';
  if (s === 'inactive' || s === 'withdrawn') return 'WITHDRAWN';
  if (s === 'graduated' || s === 'alumni') return 'GRADUATED';
  return status.toUpperCase();
}

/** A real (JWT) backend session exists; demo/offline personas have none. */
const hasBackendSession = (): boolean => !!httpClient.getToken();

/**
 * Replace every occurrence of a local, browser-generated student id with the
 * server's UUID throughout the store (students, enrollments, admissions,
 * membership events, invoices ...). The local id is a unique random token
 * (stud-<ts>-<rand>), so an exact string match cannot hit anything else.
 */
function remapStudentId(oldId: string, newId: string): void {
  const store = ERPStorage.get();
  const walk = (node: any): void => {
    if (Array.isArray(node)) {
      for (let i = 0; i < node.length; i++) {
        if (node[i] === oldId) node[i] = newId;
        else if (node[i] && typeof node[i] === 'object') walk(node[i]);
      }
      return;
    }
    for (const key of Object.keys(node)) {
      const v = node[key];
      if (v === oldId) node[key] = newId;
      else if (v && typeof v === 'object') walk(v);
    }
  };
  walk(store);
  ERPStorage.set(store, false);
}

/** Undo an optimistic local create that the server rejected. */
function rollbackLocalCreate(localId: string): void {
  const store = ERPStorage.get();
  store.students = store.students.filter(s => s.id !== localId);
  if (store.membershipEvents) {
    store.membershipEvents = store.membershipEvents.filter((e: any) => e.entityId !== localId);
  }
  ERPStorage.set(store, false);
}

export const studentService = {
  /**
   * Adds already-persisted students (e.g. returned from a backend bulk import)
   * to the local store for instant display — WITHOUT re-syncing to the backend.
   */
  addLocalBatch: (students: Student[]): void => {
    if (!students || students.length === 0) return;
    const store = ERPStorage.get();
    const existingIds = new Set(store.students.map(s => s.id));
    const existingAdm = new Set(store.students.map(s => (s.admissionNo || '').toLowerCase()));
    const fresh = students.filter(
      s => !existingIds.has(s.id) && !existingAdm.has((s.admissionNo || '').toLowerCase())
    );
    store.students = [...fresh, ...store.students];
    ERPStorage.set(store);
  },

  getAll: (): Student[] => {
    return ERPStorage.get().students;
  },

  getById: (id: string): Student | undefined => {
    return ERPStorage.get().students.find(s => s.id === id);
  },

  getByAdmissionNo: (admNo: string): Student | undefined => {
    return ERPStorage.get().students.find(s => s.admissionNo.toLowerCase() === admNo.toLowerCase().trim());
  },

  getByClassAndSection: (classId: string, section?: string): Student[] => {
    return ERPStorage.get().students.filter(s => {
      if (section) {
        return s.classId === classId && s.section.toLowerCase() === section.toLowerCase();
      }
      return s.classId === classId;
    });
  },

  getByParentEmailOrPhone: (parentContact: string): Student[] => {
    const term = parentContact.toLowerCase().trim();
    return ERPStorage.get().students.filter(
      s => s.parentEmail.toLowerCase().includes(term) || s.parentPhone.includes(term) || s.parentName.toLowerCase().includes(term)
    );
  },

  getStudentPhoto: (student?: Partial<Student> | null): string => {
    if (!student) return '/student-neutral-avatar.svg';
    if (student.photoUrl) return student.photoUrl;
    if (student.gender === 'Female') return '/student-female.svg';
    if (student.gender === 'Male') return '/student-male.svg';
    return '/default-student-avatar.svg';
  },

  getNextRollNo: (classId: string, section: string = 'A'): string => {
    const store = ERPStorage.get();
    const studentsInClass = store.students.filter(
      s => s.classId === classId && (s.section || 'A').toLowerCase() === section.toLowerCase()
    );
    const numericRolls = studentsInClass
      .map(s => {
        const match = s.rollNo ? String(s.rollNo).match(/\d+/) : null;
        return match ? parseInt(match[0], 10) : 0;
      })
      .filter(n => n > 0);
    const maxRoll = numericRolls.length > 0 ? Math.max(...numericRolls) : studentsInClass.length;
    return String(maxRoll + 1);
  },

  checkRollNoUnique: (
    classId: string,
    section: string,
    rollNo: string,
    excludeStudentId?: string
  ): { unique: boolean; existingStudent?: Student } => {
    const store = ERPStorage.get();
    const cleanRoll = rollNo.trim().toLowerCase();
    const cleanSec = (section || 'A').trim().toLowerCase();
    const existing = store.students.find(
      s =>
        s.id !== excludeStudentId &&
        s.classId === classId &&
        (s.section || 'A').trim().toLowerCase() === cleanSec &&
        (s.rollNo || '').trim().toLowerCase() === cleanRoll
    );
    return { unique: !existing, existingStudent: existing };
  },

  create: (
    data: Omit<Student, 'id'>
  ): { success: boolean; data?: Student; error?: string; synced?: Promise<StudentSyncResult> } => {
    const store = ERPStorage.get();

    // Prevent duplicate admission numbers
    const exists = store.students.some(
      s => s.admissionNo.toLowerCase() === data.admissionNo.toLowerCase().trim()
    );
    if (exists) {
      return { success: false, error: `Admission number "${data.admissionNo}" already exists.` };
    }

    // Validate class-wise unique roll number
    if (data.rollNo && data.classId) {
      const targetSec = (data.section || 'A').trim();
      const { unique, existingStudent } = studentService.checkRollNoUnique(data.classId, targetSec, data.rollNo);
      if (!unique && existingStudent) {
        return {
          success: false,
          error: `Roll Number "${data.rollNo}" is already assigned to ${existingStudent.fullName} in ${existingStudent.className || 'this class'} (${targetSec}). Roll numbers must be unique within each class.`,
        };
      }
    }

    const parts = (data.fullName || `${data.firstName || ''} ${data.lastName || ''}`).trim().split(' ');
    const firstName = data.firstName || parts[0] || 'Student';
    const lastName = data.lastName || (parts.length > 1 ? parts.slice(1).join(' ') : '');
    const fullName = data.fullName || `${firstName} ${lastName}`.trim();

    const newStudent: Student = {
      ...data,
      firstName,
      lastName,
      fullName,
      id: `stud-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    };

    // Record the join before the roster grows, so bootstrap can't double-count it.
    membershipService.recordJoin('student', newStudent);

    store.students = [newStudent, ...store.students];
    ERPStorage.set(store);

    // Demo / offline personas have no backend session: the record stays local.
    if (!hasBackendSession()) {
      return { success: true, data: newStudent, synced: Promise.resolve({ success: true, id: newStudent.id }) };
    }

    // Persist to the backend. On success the local stud-... id is swapped for
    // the server UUID so later PUT/DELETE calls target the real record; on
    // failure the optimistic insert is rolled back so no ghost student remains.
    const localId = newStudent.id;
    const payload: Record<string, any> = { ...newStudent, status: toBackendStudentStatus(newStudent.status) };
    delete payload.id;
    const fail = (error: string): StudentSyncResult => {
      rollbackLocalCreate(localId);
      notifySyncError('Student not saved', `${newStudent.fullName}: ${error}`);
      return { success: false, error };
    };
    const synced: Promise<StudentSyncResult> = httpClient
      .post<any>('/students', payload)
      .then(res => {
        const serverId: string | undefined = res?.data?.id;
        if (res?.success && serverId && isServerId(serverId)) {
          remapStudentId(localId, serverId);
          return { success: true, id: serverId };
        }
        if (res?.success) return { success: true, id: localId };
        return fail(res?.error?.message || 'Could not save the new student to the database.');
      })
      .catch(() => fail('Could not reach the server to save the new student.'));

    return { success: true, data: newStudent, synced };
  },

  update: (id: string, updates: Partial<Student>): { success: boolean; data?: Student; error?: string } => {
    const store = ERPStorage.get();
    const index = store.students.findIndex(s => s.id === id);
    if (index === -1) {
      return { success: false, error: 'Student record not found.' };
    }

    if (updates.admissionNo) {
      const duplicate = store.students.some(
        s => s.id !== id && s.admissionNo.toLowerCase() === updates.admissionNo!.toLowerCase().trim()
      );
      if (duplicate) {
        return { success: false, error: `Admission number "${updates.admissionNo}" is already in use.` };
      }
    }

    const existing = store.students[index];
    const targetClassId = updates.classId || existing.classId;
    const targetSection = (updates.section || existing.section || 'A').trim();
    const targetRollNo = updates.rollNo !== undefined ? updates.rollNo : existing.rollNo;

    if (targetRollNo && targetClassId) {
      const { unique, existingStudent } = studentService.checkRollNoUnique(
        targetClassId,
        targetSection,
        targetRollNo,
        id
      );
      if (!unique && existingStudent) {
        return {
          success: false,
          error: `Roll Number "${targetRollNo}" is already assigned to ${existingStudent.fullName} in ${existingStudent.className || 'this class'} (${targetSection}). Roll numbers must be unique within each class.`,
        };
      }
    }
    const updated: Student = {
      ...existing,
      ...updates,
      fullName:
        updates.firstName || updates.lastName
          ? `${(updates.firstName || existing.firstName).trim()} ${(updates.lastName || existing.lastName).trim()}`
          : existing.fullName,
    };

    store.students[index] = updated;
    ERPStorage.set(store);

    // Track active <-> inactive transitions as leave / rejoin events.
    if (updates.status && updates.status !== existing.status) {
      membershipService.recordStatusChange('student', updated, existing.status, updates.status);
    }

    // Persist to the backend only for server records (UUID ids) in a real
    // session; demo/local ids would 404 (or 401 without a token).
    if (!isServerId(id) || !hasBackendSession()) {
      return { success: true, data: updated };
    }
    const putBody: Record<string, any> = { ...updates };
    if (updates.status) putBody.status = toBackendStudentStatus(updates.status);
    httpClient.put(`/students/${id}`, putBody).then(res => {
      if (!res?.success) {
        notifySyncError(
          'Changes saved locally only',
          res?.error?.message || 'Could not sync the student update to the database — it may not persist.'
        );
      }
    }).catch(() => {
      notifySyncError('Changes saved locally only', 'Could not reach the server to sync the student update.');
    });

    return { success: true, data: updated };
  },

  /**
   * Deletes a student. For records that exist in the backend (their id is a
   * database UUID from sync/store) the DELETE is sent to PostgreSQL FIRST and the
   * local copy is removed only if the server confirms it. This closes the
   * "ghost login" bug where a student was dropped from localStorage but stayed
   * active in the database and could still sign in.
   *
   * Locally-created, not-yet-synced records (non-UUID ids) are removed locally.
   * Returns a result object; failures carry a human-readable `error` to surface.
   */
  delete: async (id: string): Promise<{ success: boolean; error?: string }> => {
    if (isServerId(id)) {
      const res = await httpClient.delete(`/students/${id}`);
      // httpClient never throws; it returns { success, error }. Treat a missing
      // record as already-gone (safe to remove locally); any other failure is
      // surfaced and the local copy is kept so the roster stays truthful.
      const notFound = res.error?.code === 'STUDENT_NOT_FOUND';
      if (!res.success && !notFound) {
        return {
          success: false,
          error: res.error?.message || 'Could not delete the student on the server. Please try again.',
        };
      }
    }

    const store = ERPStorage.get();
    const victim = store.students.find(s => s.id === id);
    const initialLen = store.students.length;
    store.students = store.students.filter(s => s.id !== id);
    if (store.students.length !== initialLen) {
      if (victim) {
        recycleBinService.record({
          entityType: 'Student',
          storeKey: 'students',
          originalId: victim.id,
          label: victim.fullName || `${victim.firstName} ${victim.lastName}`.trim(),
          description: `${victim.className || ''} ${victim.section ? '- ' + victim.section : ''} · Adm ${victim.admissionNo || '—'}`.trim(),
          source: 'Students Directory',
          data: victim,
          institutionId: victim.institutionId,
        });
        membershipService.recordDeparture('student', victim);
      }
      ERPStorage.set(store);
    }
    return { success: true };
  },

  /**
   * Applies a realtime student change pushed from the backend to the local store,
   * WITHOUT echoing back to the server (set(store, false)). `institutionId` is
   * the tenant of the socket room the event arrived on. Lists re-render via the
   * storage-updated event.
   */
  applyRealtimeChange: async (
    change: { action: string; data?: { id?: string; admission_number?: string } },
    institutionId?: string
  ): Promise<void> => {
    const action = change.action;
    const changedId = (change.data?.id || '').toString();
    const admNo = (change.data?.admission_number || '').toString().trim().toLowerCase();
    // Removes the changed student by server id; the admission-number fallback is
    // limited to the socket room's own institution (numbers are only unique per school).
    const removeLocal = (): void => {
      const store = ERPStorage.get();
      const before = store.students.length;
      if (changedId) store.students = store.students.filter(s => s.id !== changedId);
      if (store.students.length === before && admNo && institutionId) {
        store.students = store.students.filter(
          s => !(s.institutionId === institutionId && (s.admissionNo || '').toLowerCase() === admNo)
        );
      }
      if (store.students.length !== before) ERPStorage.set(store, false);
    };

    try {
      if (action === 'deleted') {
        removeLocal();
        return;
      }

      // created / imported / updated: pull the latest roster, update students we
      // already hold (by server id) and add the ones this tab doesn't hold yet.
      const res = await httpClient.get<any>('/sync/store');
      const backendStudents = res?.data?.students;
      if (!res?.success || !Array.isArray(backendStudents)) return;

      const byId = new Map<string, any>();
      backendStudents.forEach((st: any) => { if (st?.id) byId.set(String(st.id), st); });

      const store = ERPStorage.get();
      let changed = false;
      store.students = store.students.map(s => {
        const st = byId.get(s.id);
        if (!st) return s;
        const merged = { ...s, ...st, className: st.className || st.grade || s.className };
        if (JSON.stringify(merged) !== JSON.stringify(s)) changed = true;
        return merged;
      });

      const existingIds = new Set(store.students.map(s => s.id));
      const existingAdm = new Set(store.students.map(s => (s.admissionNo || '').toLowerCase()));
      const fresh = backendStudents
        .filter((st: any) =>
          st.admissionNo &&
          !existingIds.has(String(st.id)) &&
          !existingAdm.has(String(st.admissionNo).toLowerCase()))
        .map((st: any) => ({
          ...st,
          className: st.className || st.grade || 'Class 10',
          academicYear: st.academicYear || '2026-2027',
        }));
      if (fresh.length > 0) {
        store.students = [...fresh, ...store.students];
        changed = true;
      }
      if (changed) ERPStorage.set(store, false);

      // sync/store omits WITHDRAWN students: an update that withdrew one removes it.
      if (action === 'updated' && changedId && !byId.has(changedId)) removeLocal();
    } catch {
      /* realtime is best-effort — never throw into the socket handler */
    }
  },

  /**
   * Flips a student between active and inactive. Server records (UUID ids) are
   * persisted with PUT /students/{id} (active -> ACTIVE, inactive -> WITHDRAWN);
   * a failure reverts the local change and is surfaced as a sync-error toast.
   */
  toggleStatus: (id: string): Student | undefined => {
    const store = ERPStorage.get();
    const student = store.students.find(s => s.id === id);
    if (!student) return undefined;
    const prev = student.status;
    const next: Student['status'] = prev === 'active' ? 'inactive' : 'active';
    student.status = next;
    ERPStorage.set(store);
    membershipService.recordStatusChange('student', student, prev, next);

    if (isServerId(id) && hasBackendSession()) {
      const revert = (message: string) => {
        const current = ERPStorage.get();
        const target = current.students.find(s => s.id === id);
        if (target && target.status === next) {
          target.status = prev;
          ERPStorage.set(current);
          membershipService.recordStatusChange('student', target, next, prev);
        }
        notifySyncError('Status change not saved', message);
      };
      httpClient
        .put(`/students/${id}`, { status: toBackendStudentStatus(next) })
        .then(res => {
          if (!res?.success) revert(res?.error?.message || 'Could not update the student status on the server.');
        })
        .catch(() => revert('Could not reach the server to update the student status.'));
    }
    return student;
  },
};

