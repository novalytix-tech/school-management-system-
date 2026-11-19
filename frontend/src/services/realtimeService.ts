// ==========================================
// realtimeService — transport-agnostic realtime client.
// Backed by SocketIO today; the public surface (connect / disconnect /
// onEntityChange) is transport-neutral so it can be swapped for Supabase
// Realtime (or anything else) later without touching callers.
// Developed by NTS Novalytix
// ==========================================

import { io, Socket } from 'socket.io-client';

export interface EntityChange {
  entity: string; // 'student' | 'class' | 'guardian' | ...
  action: string; // 'created' | 'updated' | 'deleted' | 'imported'
  data: Record<string, any>;
}

type ChangeHandler = (change: EntityChange) => void;

class RealtimeService {
  private socket: Socket | null = null;
  private institutionId: string | null = null;
  private handlers = new Set<ChangeHandler>();

  /** Connect and subscribe to a tenant's room. No-op if already on that tenant. */
  connect(institutionId: string): void {
    if (!institutionId) return;
    if (this.socket && this.institutionId === institutionId && this.socket.connected) return;
    this.disconnect();
    this.institutionId = institutionId;

    // Dev: connect straight to the Flask backend (:5000, CORS "*"); prod: same
    // origin. Long-polling only — the threading/Werkzeug backend has no WebSocket
    // transport, and polling delivers events fine. Reconnection is capped so a
    // backend that is down (or incompatible) can never storm the client.
    const url = import.meta.env.DEV
      ? `${window.location.protocol}//${window.location.hostname}:5000`
      : undefined;
    const socket = io(url, {
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
