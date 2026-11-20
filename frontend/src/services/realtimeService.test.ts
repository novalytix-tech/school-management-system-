import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const ioMock = vi.hoisted(() => vi.fn());
vi.mock('socket.io-client', () => ({ io: ioMock }));

import { realtimeService } from './realtimeService';
import { httpClient, TOKEN_REFRESHED_EVENT } from './api/httpClient';

const INST = '77777777-7777-4777-8777-777777777777';

const fakeSocket = () => ({
  connected: false,
  on: vi.fn(),
  emit: vi.fn(),
  removeAllListeners: vi.fn(),
  disconnect: vi.fn(),
});

describe('realtimeService socket authentication', () => {
  beforeEach(() => {
    ioMock.mockReset();
    ioMock.mockImplementation(() => fakeSocket());
  });

  afterEach(() => {
    realtimeService.disconnect();
    httpClient.setToken(null);
  });

  it('does not open a socket without an access token', () => {
    httpClient.setToken(null);
    realtimeService.connect(INST);
    expect(ioMock).not.toHaveBeenCalled();
  });

  it('passes the current access token in the socket auth payload', () => {
    httpClient.setToken('tok-1');
    realtimeService.connect(INST);
    expect(ioMock).toHaveBeenCalledTimes(1);
    const opts = ioMock.mock.calls[0][1];
    const cb = vi.fn();
    opts.auth(cb);
    expect(cb).toHaveBeenCalledWith({ token: 'tok-1' });
  });

  it('reconnects with the new token after a refresh', () => {
    httpClient.setToken('tok-1');
    realtimeService.connect(INST);
    const first = ioMock.mock.results[0].value;

    httpClient.setToken('tok-2');
    window.dispatchEvent(new CustomEvent(TOKEN_REFRESHED_EVENT, { detail: { token: 'tok-2' } }));

    expect(first.disconnect).toHaveBeenCalled();
    expect(ioMock).toHaveBeenCalledTimes(2);
    const cb = vi.fn();
    ioMock.mock.calls[1][1].auth(cb);
    expect(cb).toHaveBeenCalledWith({ token: 'tok-2' });
  });
});
