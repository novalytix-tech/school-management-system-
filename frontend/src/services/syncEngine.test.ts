import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { syncEngine } from './syncEngine';
import { httpClient } from './api/httpClient';
import { ERPStorage } from './storage';
import { SYNC_ERROR_EVENT } from './notifyBus';

const emptyPull = (initialized = true) => ({
  success: true,
  data: { server_time: '2026-11-21T10:00:00+00:00', initialized, records: [] as any[] },
});

const okFor = (changes: any[], version = 1) => ({
  success: true,
  data: { results: changes.map(c => ({ collection: c.collection, id: c.id, status: 'ok', version })) },
});

/** Start a signed-in session whose server already has data (no first-sync upload). */
async function startSession(getMock = vi.spyOn(httpClient, 'get').mockResolvedValue(emptyPull())) {
  await syncEngine.start();
  return getMock;
}

function sentChanges(post: { mock: { calls: any[][] } }): any[] {
  return post.mock.calls.flatMap((call: any[]) => (call[1] as any).changes);
}

describe('syncEngine', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    httpClient.setToken('session-token');
  });

  afterEach(() => {
    syncEngine.stop();
    httpClient.setToken(null);
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('queues only the items that changed and pushes them with their base version', async () => {
    await startSession();
    const post = vi.spyOn(httpClient, 'post').mockImplementation(async (_url, body: any) => okFor(body.changes));

    const store = ERPStorage.get();
    const teacher = { ...store.teachers[0], fullName: 'Renamed Teacher' };
    store.teachers = [teacher, ...store.teachers.slice(1)];
    ERPStorage.set(store);
    await syncEngine.flush();

    const changes = sentChanges(post);
    expect(changes).toHaveLength(1);
    expect(changes[0]).toMatchObject({ collection: 'teachers', id: teacher.id, base_version: 0 });
    expect(changes[0].data.fullName).toBe('Renamed Teacher');
    expect(syncEngine.pendingCount()).toBe(0);

    // Next edit of the same item carries the version the server returned.
    post.mockClear();
    store.teachers = [{ ...teacher, fullName: 'Again' }, ...store.teachers.slice(1)];
    ERPStorage.set(store);
    await syncEngine.flush();
    expect(sentChanges(post)[0].base_version).toBe(1);
  });

  it('sends deletes, never touches students, and never echoes server data back', async () => {
    await startSession();
    const post = vi.spyOn(httpClient, 'post').mockImplementation(async (_url, body: any) => okFor(body.changes));

    const store = ERPStorage.get();
    const removed = store.notices[0];
    store.notices = store.notices.slice(1);
    store.students = store.students.slice(1); // relational: synced via /students instead
    ERPStorage.set(store);
    ERPStorage.set({ ...store, events: [] }, false); // came from the server
    await syncEngine.flush();

    const changes = sentChanges(post);
    expect(changes).toEqual([expect.objectContaining({ collection: 'notices', id: removed.id, deleted: true })]);
  });

  it('on conflict loads the server copy instead of overwriting it', async () => {
    await startSession();
    const store = ERPStorage.get();
    const notice = store.notices[0];
    const serverCopy = { ...notice, title: 'Edited on another device' };
    vi.spyOn(httpClient, 'post').mockResolvedValue({
      success: true,
      data: { results: [{ collection: 'notices', id: notice.id, status: 'conflict',
        current: { collection: 'notices', id: notice.id, data: serverCopy, version: 4, deleted: false } }] },
    });
    const toasts: any[] = [];
    const onToast = (e: Event) => toasts.push((e as CustomEvent).detail);
    window.addEventListener(SYNC_ERROR_EVENT, onToast);

    store.notices = [{ ...notice, title: 'My stale edit' }, ...store.notices.slice(1)];
    ERPStorage.set(store);
    await syncEngine.flush();
    window.removeEventListener(SYNC_ERROR_EVENT, onToast);

    expect(ERPStorage.get().notices.find(n => n.id === notice.id)?.title).toBe('Edited on another device');
    expect(syncEngine.pendingCount()).toBe(0);
    expect(toasts.some(t => /someone else/i.test(t.title))).toBe(true);
  });

  it('keeps changes queued while offline and retries later', async () => {
    await startSession();
    vi.useFakeTimers();
    const post = vi.spyOn(httpClient, 'post')
      .mockResolvedValueOnce({ success: false, data: null, error: { code: 'NETWORK_ERROR', message: 'offline' } })
      .mockImplementation(async (_url, body: any) => okFor(body.changes));

    const store = ERPStorage.get();
    store.notices = [{ ...store.notices[0], title: 'Offline edit' }, ...store.notices.slice(1)];
    ERPStorage.set(store);
    await syncEngine.flush();
    expect(syncEngine.pendingCount()).toBe(1);
    expect(JSON.parse(localStorage.getItem('nts_erp_sync_outbox') || '{}')).not.toEqual({});

    await vi.advanceTimersByTimeAsync(3_500);
    expect(post).toHaveBeenCalledTimes(2);
    expect(syncEngine.pendingCount()).toBe(0);
  });

  it('a full pull makes server-managed collections match the server', async () => {
    const serverNotice = { id: 'n-server', title: 'From server' };
    vi.spyOn(httpClient, 'get').mockResolvedValue({
      success: true,
      data: {
        server_time: '2026-11-21T10:00:00+00:00', initialized: true,
        records: [
          { collection: 'notices', id: 'n-server', data: serverNotice, version: 2, deleted: false },
          { collection: 'events', id: 'gone', data: null, version: 3, deleted: true },
        ],
      },
    });
    const localTeachers = ERPStorage.get().teachers.length;
    await syncEngine.start();

    const store = ERPStorage.get();
    expect(store.notices).toEqual([serverNotice]);
    expect(store.events).toEqual([]); // emptied on the server (tombstone only)
    expect(store.teachers).toHaveLength(localTeachers); // never synced: untouched
  });

  it('incremental pulls apply other devices changes without re-sending them', async () => {
    const get = await startSession();
    const post = vi.spyOn(httpClient, 'post').mockImplementation(async (_url, body: any) => okFor(body.changes));
    const notice = ERPStorage.get().notices[0];
    get.mockResolvedValue({
      success: true,
      data: { server_time: '2026-11-21T10:05:00+00:00', initialized: true, records: [
        { collection: 'notices', id: notice.id, data: { ...notice, title: 'Pulled' }, version: 7, deleted: false },
      ] },
    });
    // First pull of a new collection triggers a full reload; both paths must apply it.
    await syncEngine.pull();
    await syncEngine.flush();

    expect(ERPStorage.get().notices.find(n => n.id === notice.id)?.title).toBe('Pulled');
    expect(post).not.toHaveBeenCalled();
    expect(String(get.mock.calls.at(-1)?.[0])).toContain('/sync/records');
  });

  it('first sync of a school uploads an admin device\'s own data, not the demo seed', async () => {
    localStorage.setItem('nts_erp_current_user', JSON.stringify({ role: 'school_admin' }));
    const store = ERPStorage.get();
    store.notices = [...store.notices, { ...store.notices[0], id: 'n-real', title: 'Real notice' }];
    ERPStorage.set(store, false);
    const post = vi.spyOn(httpClient, 'post').mockImplementation(async (_url, body: any) => okFor(body.changes));

    await startSession(vi.spyOn(httpClient, 'get').mockResolvedValue(emptyPull(false)));

    const ids = sentChanges(post).map(c => `${c.collection}/${c.id}`);
    expect(ids).toContain('notices/n-real');
    expect(ids.every(id => !id.startsWith('students/'))).toBe(true);
    expect(ids).not.toContain(`notices/${store.notices[0].id}`); // untouched demo item
  });

  it('does nothing in demo mode and forgets queued changes on logout', async () => {
    httpClient.setToken(null);
    const post = vi.spyOn(httpClient, 'post');
    const store = ERPStorage.get();
    store.notices = store.notices.slice(1);
    ERPStorage.set(store);
    expect(syncEngine.pendingCount()).toBe(0);

    httpClient.setToken('session-token');
    await startSession();
    post.mockResolvedValue({ success: false, data: null, error: { code: 'NETWORK_ERROR', message: 'x' } });
    store.notices = store.notices.slice(1);
    ERPStorage.set(store);
    expect(syncEngine.pendingCount()).toBe(1);

    syncEngine.stop();
    expect(syncEngine.pendingCount()).toBe(0);
    expect(localStorage.getItem('nts_erp_sync_outbox')).toBeNull();
  });
});

describe('syncEngine incremental pulls', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    httpClient.setToken('session-token');
  });

  afterEach(() => {
    syncEngine.stop();
    httpClient.setToken(null);
    vi.restoreAllMocks();
  });

  it('a pending local edit still conflicts with a newer server copy', async () => {
    const notice = ERPStorage.get().notices[0];
    const get = vi.spyOn(httpClient, 'get').mockResolvedValue({
      success: true,
      data: { server_time: 't0', initialized: true, records: [
        { collection: 'notices', id: notice.id, data: notice, version: 1, deleted: false },
      ] },
    });
    const post = vi.spyOn(httpClient, 'post')
      .mockResolvedValue({ success: false, data: null, error: { code: 'NETWORK_ERROR', message: 'offline' } });
    await syncEngine.start();

    const store = ERPStorage.get();
    store.notices = [{ ...notice, title: 'Edited offline' }, ...store.notices.slice(1)];
    ERPStorage.set(store);
    await syncEngine.flush(); // offline: stays queued

    get.mockResolvedValue({
      success: true,
      data: { server_time: 't1', initialized: true, records: [
        { collection: 'notices', id: notice.id, data: { ...notice, title: 'Other device' }, version: 2, deleted: false },
      ] },
    });
    await syncEngine.pull();
    post.mockImplementation(async (_url, body: any) => okFor(body.changes));
    await syncEngine.flush();

    // Sent against version 1, so the server can flag it as a conflict.
    expect(post.mock.calls.at(-1)?.[1]).toMatchObject({ changes: [expect.objectContaining({ base_version: 1 })] });
  });

  it('does not refresh pages for this device\'s own saves coming back', async () => {
    const notice = ERPStorage.get().notices[0];
    const get = vi.spyOn(httpClient, 'get').mockResolvedValue({
      success: true,
      data: { server_time: 't0', initialized: true, records: [
        { collection: 'notices', id: notice.id, data: notice, version: 1, deleted: false },
      ] },
    });
    vi.spyOn(httpClient, 'post').mockImplementation(async (_url, body: any) => okFor(body.changes, 2));
    await syncEngine.start();
    const store = ERPStorage.get();
    const edited = { ...notice, title: 'Mine' };
    store.notices = [edited, ...store.notices.slice(1)];
    ERPStorage.set(store);
    await syncEngine.flush();

    let refreshes = 0;
    const onRemote = () => { refreshes += 1; };
    window.addEventListener('nts_erp_remote_update', onRemote);
    get.mockResolvedValue({
      success: true,
      data: { server_time: 't1', initialized: true, records: [
        { collection: 'notices', id: notice.id, data: edited, version: 2, deleted: false },
      ] },
    });
    await syncEngine.pull();
    window.removeEventListener('nts_erp_remote_update', onRemote);
    expect(refreshes).toBe(0);
  });
});
