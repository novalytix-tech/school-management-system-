// ==========================================
// Centralized HTTP Client with Auto-Refresh & JWT Auth
// Developed for School ERP (NTS Novalytix)
// ==========================================

import { loadingBus } from '../loadingBus';

export interface ApiResponse<T = any> {
  success: boolean;
  data: T;
  message?: string;
  error?: {
    code: string;
    message: string;
    details?: any;
  };
}

class HttpClient {
  private baseURL: string;
  private accessToken: string | null = null;
  private isRefreshing: boolean = false;
  private refreshSubscribers: ((token: string) => void)[] = [];
  private refreshFailSubscribers: (() => void)[] = [];
  private static readonly REQUEST_TIMEOUT_MS = 15000;

  constructor() {
    const proc = (globalThis as any).process;
    if ((import.meta as any).env?.VITE_API_URL) {
      this.baseURL = (import.meta as any).env.VITE_API_URL;
    } else if (proc && (proc.env?.NODE_ENV === 'test' || proc.env?.VITEST)) {
      this.baseURL = 'http://127.0.0.1:5000/api/v1';
    } else if (typeof window !== 'undefined' && window.location?.origin && !window.location.origin.includes('localhost:3000')) {
      this.baseURL = `${window.location.origin}/api/v1`;
    } else {
      this.baseURL = 'http://127.0.0.1:5000/api/v1';
    }
    // Hydrate token from storage on boot if available
    this.accessToken = localStorage.getItem('nts_erp_access_token');
  }

  public setToken(token: string | null) {
    this.accessToken = token;
    if (token) {
      localStorage.setItem('nts_erp_access_token', token);
    } else {
      localStorage.removeItem('nts_erp_access_token');
    }
  }

  public getToken(): string | null {
    return this.accessToken || localStorage.getItem('nts_erp_access_token');
  }

  private onTokenRefreshed(token: string) {
    this.refreshSubscribers.forEach(cb => cb(token));
    this.refreshSubscribers = [];
    this.refreshFailSubscribers = [];
  }

  // When refresh fails, release every queued request so their promises settle
  // instead of hanging forever (a hung promise would also freeze the top loader).
  private onTokenRefreshFailed() {
    this.refreshFailSubscribers.forEach(cb => cb());
    this.refreshFailSubscribers = [];
    this.refreshSubscribers = [];
  }

  private addRefreshSubscriber(cb: (token: string) => void) {
    this.refreshSubscribers.push(cb);
  }

  private addRefreshFailSubscriber(cb: () => void) {
    this.refreshFailSubscribers.push(cb);
  }

  /** fetch() wrapper that aborts if the server does not respond in time. */
  private async fetchWithTimeout(url: string, config: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HttpClient.REQUEST_TIMEOUT_MS);
    try {
      return await fetch(url, { ...config, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  public async request<T = any>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<ApiResponse<T>> {
    const url = endpoint.startsWith('http') ? endpoint : `${this.baseURL}${endpoint.startsWith('/') ? '' : '/'}${endpoint}`;
    const headers = new Headers(options.headers || {});

    // Ensure content type
    if (!headers.has('Content-Type') && !(options.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }

    // Attach JWT Bearer if available
    const token = this.getToken();
    if (token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`);
    }

    const config: RequestInit = {
      ...options,
      headers,
      credentials: 'include', // Include HttpOnly cookies for refresh token rotation
    };

    loadingBus.start();
    try {
      let response = await this.fetchWithTimeout(url, config);

      // Handle 401 Unauthorized with token refresh rotation
      if (response.status === 401 && !endpoint.includes('/auth/login') && !endpoint.includes('/auth/refresh')) {
        if (!this.isRefreshing) {
          this.isRefreshing = true;
          try {
            const refreshRes = await fetch(`${this.baseURL}/auth/refresh`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              credentials: 'include',
            });

            if (refreshRes.ok) {
              const refreshData = await refreshRes.json();
              const newToken = refreshData.data?.access_token;
              if (newToken) {
                this.setToken(newToken);
                this.onTokenRefreshed(newToken);
              } else {
                this.setToken(null);
                window.dispatchEvent(new CustomEvent('nts_erp_auth_expired'));
                this.onTokenRefreshFailed();
              }
            } else {
              this.setToken(null);
              window.dispatchEvent(new CustomEvent('nts_erp_auth_expired'));
              this.onTokenRefreshFailed();
            }
          } catch {
            this.setToken(null);
            this.onTokenRefreshFailed();
          } finally {
            this.isRefreshing = false;
          }
        }

        // Wait for refresh to complete then retry original request. Either path
        // (success or failure) settles the promise so it never hangs.
        const retryPromise = new Promise<ApiResponse<T>>((resolve) => {
          this.addRefreshSubscriber(async (newToken) => {
            headers.set('Authorization', `Bearer ${newToken}`);
            try {
              const retryRes = await this.fetchWithTimeout(url, { ...config, headers });
              const retryJson = await retryRes.json().catch(() => ({
                success: retryRes.ok,
                data: null,
                message: retryRes.statusText,
              }));
              resolve(retryJson);
            } catch (err: any) {
              resolve({
                success: false,
                data: null as any,
                error: { code: 'NETWORK_ERROR', message: err?.message || 'Network request failed' },
              });
            }
          });
          this.addRefreshFailSubscriber(() => {
            resolve({
              success: false,
              data: null as any,
              error: { code: 'AUTH_EXPIRED', message: 'Session expired. Please sign in again.' },
            });
          });
        });

        return await retryPromise;
      }

      const json = await response.json().catch(() => ({
        success: response.ok,
        data: null,
        message: response.statusText,
      }));

      return json;
    } catch (err: any) {
      const proc = (globalThis as any).process;
      if (!proc?.env?.VITEST) {
        console.warn(`HTTP request failed for ${url}:`, err);
      }
      return {
        success: false,
        data: null as any,
        error: {
          code: 'NETWORK_ERROR',
          message: err?.message || 'Network request failed',
        },
      };
    } finally {
      loadingBus.done();
    }
  }

  public get<T = any>(endpoint: string, options: RequestInit = {}) {
    return this.request<T>(endpoint, { ...options, method: 'GET' });
  }

  public post<T = any>(endpoint: string, body?: any, options: RequestInit = {}) {
    return this.request<T>(endpoint, {
      ...options,
      method: 'POST',
      body: body instanceof FormData ? body : JSON.stringify(body),
    });
  }

  public put<T = any>(endpoint: string, body?: any, options: RequestInit = {}) {
    return this.request<T>(endpoint, {
      ...options,
      method: 'PUT',
      body: body instanceof FormData ? body : JSON.stringify(body),
    });
  }

  public delete<T = any>(endpoint: string, options: RequestInit = {}) {
    return this.request<T>(endpoint, { ...options, method: 'DELETE' });
  }
}

export const httpClient = new HttpClient();

