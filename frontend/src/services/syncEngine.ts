// ==========================================
// syncEngine — mirrors every local store collection to the server
// (/api/v1/sync/records) so all devices of a school share the same data.
//
// How it works
// - Every ERPStorage.set() is diffed against the last known state, item by item
//   (by `id`). Only items that actually changed are queued in a persistent
//   outbox, so nothing is lost offline or on reload.
// - The outbox is flushed in batches. Each change carries the server version
//   the device last saw; if someone else saved first the server answers
//   "conflict" with its copy, which replaces the local one (no silent overwrite).
// - Pulls bring other devices' changes in: a full pull on login, then
//   incremental pulls on realtime "sync" events and on a timer.
// - Only active with a backend session (token). Demo/offline mode is untouched.
// Students are excluded: they have dedicated relational endpoints.
// Developed by NTS Novalytix
// ==========================================

import type { ERPDataStore } from '../types';
import { INITIAL_ERP_DATA } from '../data/seedData';
import { httpClient } from './api/httpClient';
import { notifySyncError } from './notifyBus';

export const EXCLUDED_COLLECTIONS = new Set(['students', 'institutions', 'version', 'migrationAuditResult']);
/** Record id used for collections stored as one document (objects, id-less arrays). */
export const WHOLE_ID = '_';

const OUTBOX_KEY = 'nts_erp_sync_outbox';
const VERSIONS_KEY = 'nts_erp_sync_versions';
const CURSOR_KEY = 'nts_erp_sync_cursor';
const SERVER_COLLECTIONS_KEY = 'nts_erp_sync_server_collections';

const FLUSH_DEBOUNCE_MS = 800;
const POLL_INTERVAL_MS = 60_000;
const MAX_BATCH_ITEMS = 200;
const MAX_BATCH_BYTES = 1_500_000;
const RETRY_DELAYS_MS = [3_000, 10_000, 30_000, 60_000];

export interface SyncChange {
  collection: string;
  id: string;
  data?: unknown;
  deleted?: boolean;
}

interface ServerRecord {
  collection: string;
  id: string;
  data: unknown;
  version: number;
  deleted: boolean;
}

interface BatchResult {
  collection: string;
  id: string;
  status: 'ok' | 'conflict' | 'forbidden' | 'invalid';
  version?: number;
  current?: ServerRecord | null;
  message?: string;
}

interface StoreAccess {
  get: () => ERPDataStore;
  /** Persist without queueing the write for sync (it came from the server). */
  setFromServer: (data: ERPDataStore) => void;
}

const keyOf = (collection: string, id: string) => `${collection}\u0000${id}`;

/** Fired after data from the server (another device, or a conflict) lands in the store. */
export const REMOTE_UPDATE_EVENT = 'nts_erp_remote_update';

function announceRemoteUpdate(): void {
  if (typeof window === 'undefined') return;
  try { window.dispatchEvent(new CustomEvent(REMOTE_UPDATE_EVENT)); } catch { /* ignore */ }
}

/** Identifies this browser tab/device in realtime events (not the user). */
export const CLIENT_ID = `c-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;

function readJSON<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage full / unavailable — in-memory state still works this session */
  }
}

/** Items are synced one by one only when every element is an object with an id. */
function isItemCollection(value: unknown): value is Array<{ id: string | number }> {
  return Array.isArray(value) && value.every(
    item => item && typeof item === 'object' && (typeof (item as any).id === 'string' || typeof (item as any).id === 'number'),
  );
}

/** Serialized view of one collection: record id -> JSON. */
function snapshotCollection(value: unknown): Map<string, string> {
  const out = new Map<string, string>();
  if (value === undefined) return out;
  if (isItemCollection(value)) {
    for (const item of value) out.set(String(item.id), JSON.stringify(item));
  } else {
    out.set(WHOLE_ID, JSON.stringify(value));
  }
  return out;
}

function syncedKeys(store: ERPDataStore): string[] {
  return Object.keys(store).filter(k => !EXCLUDED_COLLECTIONS.has(k));
}

/** Seed items, serialized, to tell untouched demo data apart from real data. */
let seedSnapshot: Map<string, Map<string, string>> | null = null;
function seedFor(collection: string): Map<string, string> {
  if (!seedSnapshot) {
    seedSnapshot = new Map();
    for (const k of Object.keys(INITIAL_ERP_DATA)) {
      seedSnapshot.set(k, snapshotCollection((INITIAL_ERP_DATA as any)[k]));
    }
  }
  return seedSnapshot.get(collection) || new Map();
}

class SyncEngine {
  private store: StoreAccess | null = null;
  private baseline = new Map<string, Map<string, string>>();
  private baselineWhole = new Map<string, string>();
  private outbox: Record<string, SyncChange> = readJSON(OUTBOX_KEY, {});
  private versions: Record<string, number> = readJSON(VERSIONS_KEY, {});
  private serverCollections = new Set<string>(readJSON<string[]>(SERVER_COLLECTIONS_KEY, []));
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private flushing: Promise<void> | null = null;
  private pulling: Promise<void> | null = null;
  private retryIndex = 0;
  private started = false;
  private focusListenerAttached = false;
  private readonly handleFocus = (): void => {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    if (!this.isActive()) return;
    void this.pull();
    void this.flush();
  };

  attach(store: StoreAccess): void {
    this.store = store;
  }

  /** Whether writes are mirrored to the server (a backend session exists). */
  isActive(): boolean {
    return this.started && !!httpClient.getToken();
  }

  pendingCount(): number {
    return Object.keys(this.outbox).length;
  }

  /** Remember `store` as the known state (after load, or after server data). */
  captureBaseline(store: ERPDataStore): void {
    this.baseline.clear();
    this.baselineWhole.clear();
    for (const k of syncedKeys(store)) {
      const value = (store as any)[k];
      this.baseline.set(k, snapshotCollection(value));
      this.baselineWhole.set(k, JSON.stringify(value ?? null));
    }
  }

  /** Diff `store` against the baseline and queue whatever changed. */
  recordChanges(store: ERPDataStore): void {
    const keys = new Set([...syncedKeys(store), ...this.baseline.keys()]);
    let queued = false;
    for (const k of keys) {
      const value = (store as any)[k];
      const whole = JSON.stringify(value ?? null);
      if (this.baselineWhole.get(k) === whole) continue;

      const next = snapshotCollection(value);
      const prev = this.baseline.get(k) || new Map<string, string>();
      if (this.started) {
        for (const [id, json] of next) {
          if (prev.get(id) !== json) {
            this.enqueue({ collection: k, id, data: JSON.parse(json) });
            queued = true;
          }
        }
        for (const id of prev.keys()) {
          if (!next.has(id)) {
            this.enqueue({ collection: k, id, deleted: true });
            queued = true;
          }
        }
      }
      this.baseline.set(k, next);
      this.baselineWhole.set(k, whole);
    }
    if (queued) {
      this.persistOutbox();
      this.scheduleFlush();
    }
  }

  private enqueue(change: SyncChange): void {
    this.outbox[keyOf(change.collection, change.id)] = change;
  }

  /** Start syncing for the signed-in user: full pull, then push pending changes. */
  async start(): Promise<void> {
    if (!httpClient.getToken()) return;
    this.started = true;
    if (this.store) this.captureBaseline(this.store.get());
    if (!this.pollTimer && typeof setInterval === 'function') {
      this.pollTimer = setInterval(() => { void this.pull(); }, POLL_INTERVAL_MS);
    }
    if (!this.focusListenerAttached && typeof window !== 'undefined') {
      // A phone/tab coming back to the foreground catches up immediately
      // (timers are throttled in the background and sockets may have dropped).
      window.addEventListener('focus', this.handleFocus);
      document.addEventListener('visibilitychange', this.handleFocus);
      this.focusListenerAttached = true;
    }
    await this.pull(true);
    await this.flush();
  }

  /** Stop syncing (logout). Sends what's pending, then forgets this user's state. */
  stop(): void {
    // Best effort, fire-and-forget: the results must not touch the next user's
    // state, so they're not processed. (The token is read synchronously here,
    // before logout clears it.)
    if (this.started && httpClient.getToken() && this.pendingCount() > 0) {
      const changes = Object.values(this.outbox).map(c => ({
        ...c, base_version: this.versions[keyOf(c.collection, c.id)] || 0,
      }));
      void httpClient.post('/sync/records/batch', { changes }).catch(() => undefined);
    }
    this.started = false;
    if (this.flushTimer) clearTimeout(this.flushTimer);
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.flushTimer = null;
    this.pollTimer = null;
    this.outbox = {};
    this.versions = {};
    this.serverCollections.clear();
    this.retryIndex = 0;
    for (const key of [OUTBOX_KEY, VERSIONS_KEY, CURSOR_KEY, SERVER_COLLECTIONS_KEY]) {
      try { localStorage.removeItem(key); } catch { /* ignore */ }
    }
  }

  private scheduleFlush(delay = FLUSH_DEBOUNCE_MS): void {
    if (!this.isActive()) return;
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      void this.flush();
    }, delay);
  }

  /** Push the outbox to the server (batched). Safe to call any time. */
  async flush(): Promise<void> {
    if (this.flushing) return this.flushing;
    this.flushing = this.flushLoop().finally(() => { this.flushing = null; });
    return this.flushing;
  }

  private async flushLoop(): Promise<void> {
    while (this.isActive() && this.pendingCount() > 0) {
      const batch: SyncChange[] = [];
      let bytes = 0;
      for (const change of Object.values(this.outbox)) {
        const size = JSON.stringify(change).length;
        if (batch.length && (batch.length >= MAX_BATCH_ITEMS || bytes + size > MAX_BATCH_BYTES)) break;
        batch.push(change);
        bytes += size;
      }
      const ok = await this.sendBatch(batch);
      if (!ok) return;
    }
  }

  /** Returns false when the server couldn't be reached (a retry is scheduled). */
  private async sendBatch(batch: SyncChange[]): Promise<boolean> {
    const res = await httpClient.post<{ results: BatchResult[] }>('/sync/records/batch', {
      client_id: CLIENT_ID,
      changes: batch.map(c => ({ ...c, base_version: this.versions[keyOf(c.collection, c.id)] || 0 })),
    });

    if (!res.success || !res.data?.results) {
      const code = res.error?.code || '';
      if (code === 'AUTH_EXPIRED') return false;
      const delay = RETRY_DELAYS_MS[Math.min(this.retryIndex, RETRY_DELAYS_MS.length - 1)];
      this.retryIndex += 1;
      notifySyncError(
        'Saved on this device — will sync',
        'The server could not be reached. Your changes are kept and will be synced automatically.',
      );
      this.scheduleFlush(delay);
      return false;
    }
    this.retryIndex = 0;

    let store: ERPDataStore | null = null;
    let conflicts = 0;
    let rejected = 0;
    for (const result of res.data.results) {
      const key = keyOf(result.collection, result.id);
      const sent = batch.find(c => c.collection === result.collection && c.id === result.id);
      // A newer local edit made while this batch was in flight stays queued.
      if (sent && this.outbox[key] === sent) delete this.outbox[key];

      if (result.status === 'ok') {
        this.versions[key] = result.version ?? 0;
        continue;
      }
      if (result.status === 'invalid') {
        rejected += 1;
        continue;
      }
      // conflict / forbidden: the server copy wins locally.
      store = store || this.store?.get() || null;
      if (store) this.applyRecord(store, result.collection, result.id, result.current ?? null);
      if (result.current) this.versions[key] = result.current.version;
      if (result.status === 'conflict') conflicts += 1;
      else rejected += 1;
    }
    this.persistOutbox();
    this.persistVersions();
    if (store && this.store) {
      this.store.setFromServer(store);
      announceRemoteUpdate();
    }

    if (conflicts) {
      notifySyncError(
        'Updated by someone else',
        `${conflicts} change(s) were already updated on another device. The latest version has been loaded — please re-apply your edit if still needed.`,
      );
    }
    if (rejected) {
      notifySyncError('Some changes were not saved', `${rejected} change(s) were not allowed for your account and have been undone.`);
    }
    return true;
  }

  /** Bring other devices' changes in. `full` reloads everything (on login). */
  async pull(full = false): Promise<void> {
    if (this.pulling) return this.pulling;
    this.pulling = this.pullOnce(full).finally(() => { this.pulling = null; });
    return this.pulling;
  }

  private async pullOnce(full: boolean): Promise<void> {
    if (!this.isActive() || !this.store) return;
    const cursor = full ? null : localStorage.getItem(CURSOR_KEY);
    if (!full && !cursor) return this.pullOnce(true);

    const res = await httpClient.get<{ server_time: string; initialized: boolean; records: ServerRecord[] }>(
      cursor ? `/sync/records?since=${encodeURIComponent(cursor)}` : '/sync/records',
    );
    if (!res.success || !res.data) return;
    const { records, initialized, server_time: serverTime } = res.data;

    // A collection that becomes server-managed for the first time needs a full
    // reload so stale local items (e.g. demo data) are replaced consistently.
    if (!full && records.some(r => !this.serverCollections.has(r.collection))) {
      return this.pullOnce(true);
    }

    const store = this.store.get();
    let applied = 0;
    if (full) {
      if (!initialized) {
        this.bootstrap(store);
      } else {
        this.replaceFromServer(store, records);
      }
    } else {
      for (const rec of records) {
        const key = keyOf(rec.collection, rec.id);
        // Already have this version (typically our own save coming back).
        if (this.versions[key] === rec.version) continue;
        // Local edit pending: keep its base version so the push is checked
        // against this newer server copy and reported as a conflict.
        if (this.outbox[key]) continue;
        this.versions[key] = rec.version;
        this.applyRecord(store, rec.collection, rec.id, rec);
        applied += 1;
      }
    }
    try { localStorage.setItem(CURSOR_KEY, serverTime); } catch { /* ignore */ }
    this.persistVersions();
    if (full || applied > 0) {
      this.store.setFromServer(store);
      announceRemoteUpdate();
    }
  }

  /** Server-managed collections become exactly the server's data (+ pending local edits). */
  private replaceFromServer(store: ERPDataStore, records: ServerRecord[]): void {
    const byCollection = new Map<string, ServerRecord[]>();
    for (const rec of records) {
      this.versions[keyOf(rec.collection, rec.id)] = rec.version;
      if (!byCollection.has(rec.collection)) byCollection.set(rec.collection, []);
      byCollection.get(rec.collection)!.push(rec);
    }
    for (const [collection, recs] of byCollection) {
      if (EXCLUDED_COLLECTIONS.has(collection)) continue;
      this.serverCollections.add(collection);
      const whole = recs.find(r => r.id === WHOLE_ID);
      if (whole) {
        if (!whole.deleted) (store as any)[collection] = whole.data;
      } else {
        (store as any)[collection] = recs.filter(r => !r.deleted).map(r => r.data);
      }
    }
    // Local edits not yet pushed win over what was just loaded.
    for (const change of Object.values(this.outbox)) {
      this.applyRecord(store, change.collection, change.id,
        change.deleted ? null : { collection: change.collection, id: change.id, data: change.data, version: 0, deleted: false });
    }
    writeJSON(SERVER_COLLECTIONS_KEY, [...this.serverCollections]);
  }

  /**
   * First sync of a school: an administrator's device uploads the data that was
   * entered on it (anything that differs from the built-in demo data).
   */
  private bootstrap(store: ERPDataStore): void {
    const role = (() => {
      try { return JSON.parse(localStorage.getItem('nts_erp_current_user') || '{}').role; } catch { return undefined; }
    })();
    if (role !== 'school_admin' && role !== 'super_admin') return;
    for (const k of syncedKeys(store)) {
      const seed = seedFor(k);
      for (const [id, json] of snapshotCollection((store as any)[k])) {
        if (seed.get(id) !== json) this.enqueue({ collection: k, id, data: JSON.parse(json) });
      }
    }
    this.persistOutbox();
  }

  /** Upsert (or remove, when `rec` is null/deleted) one record in the store. */
  private applyRecord(store: ERPDataStore, collection: string, id: string, rec: ServerRecord | null): void {
    const removed = !rec || rec.deleted;
    if (id === WHOLE_ID) {
      if (!removed) (store as any)[collection] = rec!.data;
      return;
    }
    const list: any[] = Array.isArray((store as any)[collection]) ? [...(store as any)[collection]] : [];
    const idx = list.findIndex(item => item && String(item.id) === id);
    if (removed) {
      if (idx >= 0) list.splice(idx, 1);
    } else if (idx >= 0) {
      list[idx] = rec!.data;
    } else {
      list.push(rec!.data);
    }
    (store as any)[collection] = list;
  }

  private persistOutbox(): void {
    writeJSON(OUTBOX_KEY, this.outbox);
  }

  private persistVersions(): void {
    writeJSON(VERSIONS_KEY, this.versions);
  }
}

export const syncEngine = new SyncEngine();
