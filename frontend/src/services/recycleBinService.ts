// ==========================================
// Recycle Bin Service — global soft-delete store
// Any delete across the app can route the removed record here with a full
// snapshot, so it can be seen, restored, or permanently purged later.
// Developed by NTS Novalytix
// ==========================================

import { ERPDataStore, RecycleBinEntry, User, UserRole } from '../types';
import { ERPStorage } from './storage';

interface RecordParams {
  entityType: string;
  storeKey: keyof ERPDataStore & string;
  originalId: string;
  label: string;
  description?: string;
  source: string;
  data: any;
  institutionId?: string;
}

/** Resolve who is performing the action from the persisted session (no prop drilling). */
function currentActor(): { id: string; name: string; role: string; institutionId?: string } {
  try {
    const raw = localStorage.getItem('nts_erp_current_user');
    if (raw) {
      const u = JSON.parse(raw) as User;
      return { id: u.id, name: u.name, role: u.role, institutionId: u.institutionId };
    }
  } catch {
    /* fall through to default */
  }
  return { id: 'system', name: 'System', role: 'school_admin' };
}

/** Roles that manage the whole institution's bin, not just their own deletions. */
const ADMIN_ROLES: UserRole[] = ['super_admin', 'school_admin'];

export const recycleBinService = {
  getAll(): RecycleBinEntry[] {
    const store = ERPStorage.get();
    return store.recycleBin || [];
  },

  /** Entries a given persona is allowed to see in their Recycle Bin. */
  getForUser(user: Pick<User, 'id' | 'role' | 'institutionId'>): RecycleBinEntry[] {
    const all = this.getAll();
    const sorted = [...all].sort((a, b) => (a.deletedAt < b.deletedAt ? 1 : -1));

    if (user.role === 'super_admin') return sorted;

    if (ADMIN_ROLES.includes(user.role)) {
      // School-level admins manage everything deleted within their institution.
      return sorted.filter(
        e => !e.institutionId || !user.institutionId || e.institutionId === user.institutionId
      );
    }

    // Everyone else sees only what they themselves deleted.
    return sorted.filter(e => e.deletedByUserId === user.id);
  },

  countForUser(user: Pick<User, 'id' | 'role' | 'institutionId'>): number {
    return this.getForUser(user).length;
  },

  /** Move a deleted record into the bin. Returns the created entry. */
  record(params: RecordParams): RecycleBinEntry {
    const store = ERPStorage.get();
    if (!store.recycleBin) store.recycleBin = [];
    const actor = currentActor();

    const entry: RecycleBinEntry = {
      id: `bin-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      entityType: params.entityType,
      storeKey: params.storeKey,
      originalId: params.originalId,
      label: params.label || 'Untitled record',
      description: params.description,
      source: params.source,
      data: JSON.parse(JSON.stringify(params.data)), // defensive deep clone
      institutionId: params.institutionId ?? (params.data && params.data.institutionId) ?? actor.institutionId,
      deletedAt: new Date().toISOString(),
      deletedByUserId: actor.id,
      deletedByName: actor.name,
      deletedByRole: actor.role,
    };

    store.recycleBin = [entry, ...store.recycleBin];
    ERPStorage.set(store);
    return entry;
  },

  /** Restore a record back into its original collection and remove it from the bin. */
  restore(id: string): { success: boolean; message?: string; entry?: RecycleBinEntry } {
    const store = ERPStorage.get();
    const bin = store.recycleBin || [];
    const entry = bin.find(e => e.id === id);
    if (!entry) return { success: false, message: 'Item not found in Recycle Bin.' };

    const key = entry.storeKey;
    const collection = (store as any)[key];
    const list: any[] = Array.isArray(collection) ? collection : [];

    // Guard against restoring a record whose id already exists again.
    const exists = list.some(r => r && r.id === entry.originalId);
    if (!exists) {
      list.unshift(entry.data);
    }
    (store as any)[key] = list;

    store.recycleBin = bin.filter(e => e.id !== id);
    ERPStorage.set(store);
    return { success: true, entry };
  },

  /** Permanently remove a single entry from the bin (cannot be undone). */
  permanentDelete(id: string): boolean {
    const store = ERPStorage.get();
    const bin = store.recycleBin || [];
    const next = bin.filter(e => e.id !== id);
    if (next.length === bin.length) return false;
    store.recycleBin = next;
    ERPStorage.set(store);
    return true;
  },

  /** Permanently clear everything the given persona can see in their bin. */
  emptyForUser(user: Pick<User, 'id' | 'role' | 'institutionId'>): number {
    const store = ERPStorage.get();
    const bin = store.recycleBin || [];
    const visible = new Set(this.getForUser(user).map(e => e.id));
    const next = bin.filter(e => !visible.has(e.id));
    const removed = bin.length - next.length;
    store.recycleBin = next;
    ERPStorage.set(store);
    return removed;
  },
};
