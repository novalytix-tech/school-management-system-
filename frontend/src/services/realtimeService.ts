// ==========================================
// realtimeService — transport-agnostic realtime client.
// Backed by SocketIO today; the public surface (connect / disconnect /
// onEntityChange) is transport-neutral so it can be swapped for Supabase
// Realtime (or anything else) later without touching callers.
// Developed by NTS Novalytix
// ==========================================

import { io, Socket } from 'socket.io-client';
import { httpClient, TOKEN_REFRESHED_EVENT } from './api/httpClient';

export interface EntityChange {
  entity: string; // 'student' | 'class' | 'guardian' | ...
  action: string; // 'created' | 'updated' | 'deleted' | 'imported'
  data: Record<string, any>;
}

type ChangeHandler = (change: EntityChange) => void;

/** Origin of the backend for the socket, or undefined for this page's origin. */
export function socketOrigin(apiUrl: string | undefined = import.meta.env.VITE_API_URL): string | undefined {
  if (!apiUrl) return undefined;
  try {
    return new URL(apiUrl, typeof window !== 'undefined' ? window.location.href : undefined).origin;
  } catch {
    return undefined;
  }
}

class RealtimeService {
  private socket: Socket | null = null;
  private institutionId: string | null = null;
  private handlers = new Set<ChangeHandler>();

  /** Connect and subscribe to a tenant's room. No-op if already on that tenant. */
  private refreshListenerAttached = false;

  /**
   * After the HTTP client refreshes the access token, re-open the socket so the
   * server authenticates it with the new token (the old one has expired).
   */
  private readonly handleTokenRefreshed = (): void => {
    const instId = this.institutionId;
    if (!instId) return;
    this.disconnect();
    this.connect(instId);
  };

  /**
   * Connect and subscribe to a tenant's room. No-op if already on that tenant.
   * The backend refuses sockets without a valid access token, so nothing is
   * opened when there is no token (demo / signed-out).
   */
  connect(institutionId: string): void {
    if (!institutionId) return;
    if (!httpClient.getToken()) {
      this.disconnect();
      return;
    }
    if (this.socket && this.institutionId === institutionId && this.socket.connected) return;
    this.disconnect();
    this.institutionId = institutionId;
    if (!this.refreshListenerAttached && typeof window !== 'undefined') {
      window.addEventListener(TOKEN_REFRESHED_EVENT, this.handleTokenRefreshed);
      this.refreshListenerAttached = true;
    }

    // Same host as the REST API: VITE_API_URL's origin when the API lives on
    // another host (e.g. Vercel frontend + Render backend), otherwise this origin
    // (the dev server proxies /socket.io). Long-polling only — the threading/
    // Werkzeug backend has no WebSocket transport, and polling delivers events
    // fine. Reconnection is capped so a backend that is down (or incompatible)
    // can never storm the client.
    const url = socketOrigin();
    const socket = io(url, {
      // Callback form: every (re)connection attempt sends the CURRENT token.
      auth: cb => cb({ token: httpClient.getToken() }),
      path: '/socket.io',
      transports: ['polling'],
      reconnection: true,
      reconnectionAttempts: 8,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 8000,
      timeout: 8000,
    });
    socket.on('connect', () => {
      socket.emit('join_institution', { institution_id: institutionId });
    });
    socket.on('entity_changed', (change: EntityChange) => {
      this.handlers.forEach(h => {
        try { h(change); } catch { /* a bad handler must not kill the socket */ }
      });
    });
    this.socket = socket;
  }

  /** Register a change handler; returns an unsubscribe function. */
  onEntityChange(handler: ChangeHandler): () => void {
    this.handlers.add(handler);
    return () => { this.handlers.delete(handler); };
  }

  disconnect(): void {
    if (this.refreshListenerAttached && typeof window !== 'undefined') {
      window.removeEventListener(TOKEN_REFRESHED_EVENT, this.handleTokenRefreshed);
      this.refreshListenerAttached = false;
    }
    if (this.socket) {
      try { this.socket.removeAllListeners(); this.socket.disconnect(); } catch { /* ignore */ }
    }
    this.socket = null;
    this.institutionId = null;
  }

  isConnected(): boolean {
    return !!this.socket && this.socket.connected;
  }
}

export const realtimeService = new RealtimeService();
