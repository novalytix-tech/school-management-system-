import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { httpClient } from './httpClient';

type FakeResponse = { ok: boolean; status: number; statusText: string; json: () => Promise<any> };

const respond = (status: number, body: any): FakeResponse => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status === 401 ? 'Unauthorized' : 'OK',
  json: async () => body,
});

const authHeader = (init?: RequestInit) => new Headers(init?.headers || {}).get('Authorization');

describe('httpClient 401 refresh handling', () => {
  beforeEach(() => {
    localStorage.clear();
    httpClient.setToken('stale-token');
  });

  afterEach(() => {
    httpClient.setToken(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('two concurrent 401s trigger exactly one /auth/refresh and both requests are retried', async () => {
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/auth/refresh')) {
        refreshCalls++;
        // Keep the refresh in flight long enough for both 401s to queue on it.
        await new Promise(r => setTimeout(r, 20));
        return respond(200, { success: true, data: { access_token: 'fresh-token' } });
      }
      if (authHeader(init) === 'Bearer fresh-token') {
        return respond(200, { success: true, data: { url } });
      }
      return respond(401, { success: false, error: { code: 'UNAUTHORIZED', message: 'expired' } });
    });
    vi.stubGlobal('fetch', fetchMock);

    const [a, b] = await Promise.all([httpClient.get('/students'), httpClient.get('/classes')]);

    expect(refreshCalls).toBe(1);
    expect(a.success).toBe(true);
    expect(b.success).toBe(true);
    expect(a.data.url).toContain('/students');
    expect(b.data.url).toContain('/classes');
    // 2 original attempts + 1 refresh + 2 retries.
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(httpClient.getToken()).toBe('fresh-token');
  });

  it('a failed refresh settles every waiting request with AUTH_EXPIRED and fires nts_erp_auth_expired', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/auth/refresh')) return respond(401, { success: false });
      return respond(401, { success: false });
    });
    vi.stubGlobal('fetch', fetchMock);
    const expired = vi.fn();
    window.addEventListener('nts_erp_auth_expired', expired);

    const [a, b] = await Promise.all([httpClient.get('/students'), httpClient.get('/classes')]);
    window.removeEventListener('nts_erp_auth_expired', expired);

    expect(a.error?.code).toBe('AUTH_EXPIRED');
    expect(b.error?.code).toBe('AUTH_EXPIRED');
    expect(fetchMock.mock.calls.filter(c => String(c[0]).endsWith('/auth/refresh'))).toHaveLength(1);
    expect(expired).toHaveBeenCalledTimes(1);
    expect(httpClient.getToken()).toBeNull();
  });
});
