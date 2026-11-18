// ==========================================
// Membership / Tenure History Service
// Tracks when students & teachers join, leave, and rejoin the organization so
// admins can see every stint, how many times a person left & came back, each
// period's dates and duration, and their total time as a member.
// Developed by NTS Novalytix
// ==========================================

import { ClassTenureRecord, MembershipEvent, Student, Teacher } from '../types';
import { ERPStorage } from './storage';

/**
 * Open-ended "high date" sentinel. While a teacher is an active member every
 * class they joined carries this as its end date, meaning "no end yet". When
 * the teacher leaves the organization it is replaced with the real leave date.
 */
export const ACTIVE_SENTINEL_DATE = '9999-12-31';

export interface TenureStint {
  start: string;
  end: string | null; // null = still ongoing
  durationDays: number;
  ongoing: boolean;
}

export interface TenureRecord {
  entityType: 'student' | 'teacher';
  entityId: string;
  personName: string;
  personRef?: string;
  roleLabel?: string;
  institutionId?: string;
  firstJoined: string;
  lastEventDate: string;
  stints: TenureStint[];
  leftCount: number;
  rejoinCount: number;
  totalActiveDays: number;
  currentlyMember: boolean;
}

const DAY_MS = 86400000;
const todayIso = () => new Date().toISOString().split('T')[0];

function daysBetween(startIso: string, endIso: string): number {
  const a = new Date(startIso).getTime();
  const b = new Date(endIso).getTime();
  if (isNaN(a) || isNaN(b)) return 0;
  return Math.max(0, Math.round((b - a) / DAY_MS));
}

function addDaysIso(iso: string, days: number): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  d.setDate(d.getDate() + days);
  return d.toISOString().split('T')[0];
}

function newId(): string {
  return `mbr-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

function studentMeta(s: Student) {
  return {
    personName: s.fullName || `${s.firstName} ${s.lastName}`.trim(),
    personRef: s.admissionNo,
    roleLabel: `${s.className || ''}${s.section ? ' - ' + s.section : ''}`.trim() || 'Student',
    institutionId: s.institutionId,
    joinDate: s.admissionDate || todayIso(),
  };
}

function teacherMeta(t: Teacher) {
  return {
    personName: t.fullName || `${t.firstName} ${t.lastName}`.trim(),
    personRef: t.staffId,
    roleLabel: `${t.designation || ''}${t.department ? ' · ' + t.department : ''}`.trim() || 'Staff',
    institutionId: t.institutionId,
    joinDate: t.joiningDate || todayIso(),
  };
}

export const membershipService = {
  getEvents(): MembershipEvent[] {
    this.ensureBootstrap();
    return ERPStorage.get().membershipEvents || [];
  },

  /** Append a membership event to the store. */
  record(evt: Omit<MembershipEvent, 'id'>): MembershipEvent {
    // Derive baseline history for the existing roster before the first manual
    // event is written, so a single create/toggle can never "lock in" an empty
    // history for everyone else.
    this.ensureBootstrap();
    const store = ERPStorage.get();
    if (!store.membershipEvents) store.membershipEvents = [];
    const full: MembershipEvent = { ...evt, id: newId() };
    store.membershipEvents = [...store.membershipEvents, full];
    ERPStorage.set(store);
    return full;
  },

  /** Record a first-time join (called when a student/teacher is created). */
  recordJoin(entityType: 'student' | 'teacher', entity: Student | Teacher): void {
    const meta = entityType === 'student' ? studentMeta(entity as Student) : teacherMeta(entity as Teacher);
    this.record({
      entityType,
      entityId: entity.id,
      personName: meta.personName,
      personRef: meta.personRef,
      roleLabel: meta.roleLabel,
      institutionId: meta.institutionId,
      eventType: 'joined',
      date: meta.joinDate,
      note: 'Joined the organization',
    });
  },

  /**
   * Record a status transition (active <-> inactive) as a leave or rejoin.
   * No-op when the status did not actually change.
   */
  recordStatusChange(
    entityType: 'student' | 'teacher',
    entity: Student | Teacher,
    prevStatus: string | undefined,
    nextStatus: string | undefined
  ): void {
    if (!prevStatus || !nextStatus || prevStatus === nextStatus) return;
    const meta = entityType === 'student' ? studentMeta(entity as Student) : teacherMeta(entity as Teacher);
    const leaving = nextStatus === 'inactive';
    this.record({
      entityType,
      entityId: entity.id,
      personName: meta.personName,
      personRef: meta.personRef,
      roleLabel: meta.roleLabel,
      institutionId: meta.institutionId,
      eventType: leaving ? 'left' : 'rejoined',
      date: todayIso(),
      note: leaving ? 'Left / deactivated' : 'Rejoined / reactivated',
    });
  },

  /** Record a departure when a record is deleted outright. */
  recordDeparture(entityType: 'student' | 'teacher', entity: Student | Teacher): void {
    // Only meaningful if the person was an active member.
    if ((entity as any).status === 'inactive') return;
    const meta = entityType === 'student' ? studentMeta(entity as Student) : teacherMeta(entity as Teacher);
    this.record({
      entityType,
      entityId: entity.id,
      personName: meta.personName,
      personRef: meta.personRef,
      roleLabel: meta.roleLabel,
      institutionId: meta.institutionId,
      eventType: 'left',
      date: todayIso(),
      note: 'Record removed',
    });
  },

  /** Aggregate raw events into per-person tenure records. */
  getTenures(): TenureRecord[] {
    const events = this.getEvents();
    const byEntity = new Map<string, MembershipEvent[]>();
    events.forEach(e => {
      const arr = byEntity.get(e.entityId) || [];
      arr.push(e);
      byEntity.set(e.entityId, arr);
    });

    const rank = (t: string) => (t === 'left' ? 1 : 0); // join/rejoin before a same-day leave
    const records: TenureRecord[] = [];

    byEntity.forEach(evts => {
      const sorted = [...evts].sort((a, b) =>
        a.date === b.date ? rank(a.eventType) - rank(b.eventType) : a.date < b.date ? -1 : 1
      );
      const latest = sorted[sorted.length - 1];

      const stints: TenureStint[] = [];
      let openStart: string | null = null;
      let leftCount = 0;
      let rejoinCount = 0;

      sorted.forEach(e => {
        if (e.eventType === 'joined' || e.eventType === 'rejoined') {
          if (e.eventType === 'rejoined') rejoinCount++;
          if (openStart === null) openStart = e.date;
        } else if (e.eventType === 'left') {
          leftCount++;
          if (openStart !== null) {
            stints.push({
              start: openStart,
              end: e.date,
              durationDays: daysBetween(openStart, e.date),
              ongoing: false,
            });
            openStart = null;
          }
        }
      });

      if (openStart !== null) {
        stints.push({
          start: openStart,
          end: null,
          durationDays: daysBetween(openStart, todayIso()),
          ongoing: true,
        });
      }

      const totalActiveDays = stints.reduce((sum, s) => sum + s.durationDays, 0);
      const currentlyMember = stints.length > 0 && stints[stints.length - 1].ongoing;

      records.push({
        entityType: latest.entityType,
        entityId: latest.entityId,
        personName: latest.personName,
        personRef: latest.personRef,
        roleLabel: latest.roleLabel,
        institutionId: latest.institutionId,
        firstJoined: sorted[0].date,
        lastEventDate: latest.date,
        stints,
        leftCount,
        rejoinCount,
        totalActiveDays,
        currentlyMember,
      });
    });

    // Most recently active first.
    return records.sort((a, b) => (a.lastEventDate < b.lastEventDate ? 1 : -1));
  },

  /**
   * Derive one tenure row per (teacher, class) assignment, using the open-ended
   * 9999-12-31 sentinel as the end date while the teacher is active. Once the
   * teacher has left the organization, the sentinel is replaced with the actual
   * departure date (the end of their last membership stint) and the rows are
   * flagged inactive.
   */
  getClassTenures(): ClassTenureRecord[] {
    const store = ERPStorage.get();
    // Index teacher tenures so we can recover the real departure date.
    const teacherTenure = new Map<string, TenureRecord>();
    this.getTenures()
      .filter(t => t.entityType === 'teacher')
      .forEach(t => teacherTenure.set(t.entityId, t));

    const rows: ClassTenureRecord[] = [];
    (store.teachers || []).forEach(t => {
      const active = t.status !== 'inactive';
      const tenure = teacherTenure.get(t.id);
      const lastStint = tenure?.stints[tenure.stints.length - 1];
      // Real leave date when inactive, otherwise the open-ended sentinel.
      const leaveDate = !active
        ? lastStint?.end || todayIso()
        : ACTIVE_SENTINEL_DATE;
      const startDate = t.joiningDate || tenure?.firstJoined || todayIso();

      (t.assignedClasses || []).forEach(a => {
        rows.push({
          teacherId: t.id,
          teacherName: t.fullName || `${t.firstName} ${t.lastName}`.trim(),
          staffId: t.staffId,
          institutionId: t.institutionId,
          classId: a.classId,
          className: a.className,
          section: a.section,
          subject: a.subject,
          startDate,
          endDate: active ? ACTIVE_SENTINEL_DATE : leaveDate,
          isActive: active,
        });
      });
    });

    return rows;
  },

  /** Class-assignment tenure rows for a single teacher. */
  getTeacherClassTenures(teacherId: string): ClassTenureRecord[] {
    return this.getClassTenures().filter(r => r.teacherId === teacherId);
  },

  /**
   * One-time derivation of initial history from the existing roster. Creates a
   * "joined" event (from admission / joining date) for every student & teacher,
   * a matching "left" for anyone currently inactive, and a few illustrative
   * leave/rejoin cycles so the timeline is meaningful in the demo. Idempotent.
   */
  ensureBootstrap(): void {
    const store = ERPStorage.get();
    if (store.membershipEvents) return; // already initialised

    const events: MembershipEvent[] = [];
    const push = (e: Omit<MembershipEvent, 'id'>) => events.push({ ...e, id: newId() });

    // Demo multi-stint scripts keyed by seed id → extra [left, rejoin] cycles
    // expressed as day-offsets from the join date.
    const demoCycles: Record<string, Array<{ leftAfter: number; rejoinAfter: number }>> = {
      'stud-3': [{ leftAfter: 180, rejoinAfter: 320 }],
      'stud-6': [{ leftAfter: 150, rejoinAfter: 260 }, { leftAfter: 540, rejoinAfter: 680 }],
      'teach-2': [{ leftAfter: 400, rejoinAfter: 760 }],
      'teach-3': [{ leftAfter: 300, rejoinAfter: 430 }, { leftAfter: 900, rejoinAfter: 1010 }],
    };

    const emit = (
      entityType: 'student' | 'teacher',
      entity: Student | Teacher,
      meta: ReturnType<typeof studentMeta>
    ) => {
      const base = {
        entityType,
        entityId: entity.id,
        personName: meta.personName,
        personRef: meta.personRef,
        roleLabel: meta.roleLabel,
        institutionId: meta.institutionId,
      };
      push({ ...base, eventType: 'joined', date: meta.joinDate, note: 'Joined the organization' });

      const cycles = demoCycles[entity.id] || [];
      cycles.forEach(c => {
        const leftDate = addDaysIso(meta.joinDate, c.leftAfter);
        const rejoinDate = addDaysIso(meta.joinDate, c.rejoinAfter);
        if (leftDate < todayIso()) {
          push({ ...base, eventType: 'left', date: leftDate, note: 'Left the organization' });
          if (rejoinDate <= todayIso()) {
            push({ ...base, eventType: 'rejoined', date: rejoinDate, note: 'Rejoined the organization' });
          }
        }
      });

      // Reflect current inactive status as a final departure if none recorded yet.
      if ((entity as any).status === 'inactive' && cycles.length === 0) {
        push({ ...base, eventType: 'left', date: addDaysIso(meta.joinDate, 200), note: 'Left / deactivated' });
      }
    };

    (store.students || []).forEach(s => emit('student', s, studentMeta(s)));
    (store.teachers || []).forEach(t => emit('teacher', t, teacherMeta(t)));

    store.membershipEvents = events;
    ERPStorage.set(store);
  },
};
