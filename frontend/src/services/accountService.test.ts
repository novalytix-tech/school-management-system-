import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { accountService } from './accountService';
import { httpClient } from './api/httpClient';
import { ERPStorage } from './storage';

const failure = (code: string, message: string) => ({
  success: false,
  data: null as any,
  error: { code, message },
});

describe('accountService with a real backend session', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    httpClient.setToken('session-token');
  });

  afterEach(() => {
    httpClient.setToken(null);
    vi.restoreAllMocks();
  });

  it('resetUserPassword never fabricates a password when the server fails', async () => {
    vi.spyOn(httpClient, 'post').mockResolvedValue(failure('HTTP_500', 'Internal error'));
    const res = await accountService.resetUserPassword('u-1');
    expect(res.success).toBe(false);
    expect(res.temporaryPassword).toBeUndefined();
    expect(res.error).toBe('Internal error');
  });

  it('resetUserPassword returns the server-issued password on success', async () => {
    vi.spyOn(httpClient, 'post').mockResolvedValue({ success: true, data: { temporary_password: 'Srv#1234!' } });
    const res = await accountService.resetUserPassword('u-1');
    expect(res).toEqual({ success: true, temporaryPassword: 'Srv#1234!' });
  });

  it('toggleAccountStatus and resendCredentials report failure', async () => {
    vi.spyOn(httpClient, 'post').mockResolvedValue(failure('NETWORK_ERROR', 'Network request failed'));
    const toggle = await accountService.toggleAccountStatus('u-1');
    expect(toggle.success).toBe(false);
    expect(toggle.error).toBe('Network request failed');

    const resend = await accountService.resendCredentials('u-1', 'sms');
    expect(resend.success).toBe(false);
    expect(resend.error).toBe('Network request failed');
  });

  it('getAllAccounts does not fall back to local demo accounts on 401', async () => {
    vi.spyOn(httpClient, 'get').mockResolvedValue(failure('AUTH_EXPIRED', 'Session expired. Please sign in again.'));
    const res = await accountService.getAllAccounts();
    expect(res.success).toBe(false);
    expect(res.data).toEqual([]);
    expect(res.error).toMatch(/Session expired/);
  });

  it('createAccount reports server rejection instead of creating a local account', async () => {
    vi.spyOn(httpClient, 'post').mockResolvedValue(failure('DUPLICATE', 'Login ID already exists'));
    const res = await accountService.createAccount({
      firstName: 'A',
      lastName: 'B',
      email: 'a@b.c',
      phone: '1',
      role: 'student',
    });
    expect(res).toEqual({ success: false, error: 'Login ID already exists' });
  });
});

describe('accountService in demo / offline mode (no token)', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
    httpClient.setToken(null);
  });

  afterEach(() => vi.restoreAllMocks());

  it('uses the local store and never calls the server', async () => {
    const getSpy = vi.spyOn(httpClient, 'get');
    const postSpy = vi.spyOn(httpClient, 'post');

    const list = await accountService.getAllAccounts({ role: 'student' });
    expect(list.success).toBe(true);
    expect(list.data.length).toBeGreaterThan(0);

    const reset = await accountService.resetUserPassword('stud-1');
    expect(reset.success).toBe(true);
    expect(reset.temporaryPassword).toBeTruthy();

    expect(getSpy).not.toHaveBeenCalled();
    expect(postSpy).not.toHaveBeenCalled();
  });
});
