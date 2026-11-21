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

export interface BlobResponse {
  success: boolean;
  blob?: Blob;
  /** From Content-Disposition, when the server sends one. */
  filename?: string;
  error?: { code: string; message: string };
}

/** Fired on window after /auth/refresh issues a new access token. */
export const TOKEN_REFRESHED_EVENT = 'nts_erp_token_refreshed';

class HttpClient {
  private baseURL: string;
  private accessToken: string | null = null;
  // One shared in-flight refresh: every request that hits a 401 awaits the same
  // promise, so concurrent 401s trigger a single /auth/refresh and all settle.
  private refreshPromise: Promise<string | null> | null = null;
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

  /** Refresh the access token once; resolves to the new token or null on failure. */
  private refreshAccessToken(): Promise<string | null> {
    if (!this.refreshPromise) {
      this.refreshPromise = (async () => {
        try {
          const refreshRes = await this.fetchWithTimeout(`${this.baseURL}/auth/refresh`, {
            method: 'POST',
            // The server requires this header for cookie-based refresh (CSRF defence).
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'include',
          });
          const refreshData = refreshRes.ok ? await refreshRes.json().catch(() => null) : null;
          const newToken: string | null = refreshData?.data?.access_token || null;
          if (newToken) {
            this.setToken(newToken);
            // Lets long-lived connections (realtime socket) re-authenticate.
            if (typeof window !== 'undefined') {
              window.dispatchEvent(new CustomEvent(TOKEN_REFRESHED_EVENT, { detail: { token: newToken } }));
            }
            return newToken;
          }
        } catch {
          // Network failure or timeout: treated as an expired session below.
        }
        this.setToken(null);
        if (typeof window !== 'undefined') {
          window.dispatchEvent(new CustomEvent('nts_erp_auth_expired'));
        }
        return null;
      })().finally(() => {
        this.refreshPromise = null;
      });
    }
    return this.refreshPromise;
  }

  /** Parse a response body, tolerating empty or non-JSON error pages. */
  private async parseJson<T>(response: Response): Promise<ApiResponse<T>> {
    return response.json().catch(() => ({
      success: response.ok,
      data: null as any,
      message: response.statusText,
      ...(response.ok ? {} : { error: { code: `HTTP_${response.status}`, message: response.statusText || 'Request failed' } }),
    }));
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
        const newToken = await this.refreshAccessToken();
        if (!newToken) {
          return {
            success: false,
            data: null as any,
            error: { code: 'AUTH_EXPIRED', message: 'Session expired. Please sign in again.' },
          };
        }
        headers.set('Authorization', `Bearer ${newToken}`);
        response = await this.fetchWithTimeout(url, { ...config, headers });
      }

      return await this.parseJson<T>(response);
    } catch (err: any) {
      const proc = (globalThis as any).process;
      if (!proc?.env?.VITEST) {
        console.warn(`HTTP request failed for ${url}:`, err);
      }
      return {
        success: false,
        data: null as any,
        error: {
          code: err?.name === 'AbortError' ? 'TIMEOUT' : 'NETWORK_ERROR',
          message: err?.name === 'AbortError'
            ? 'The server took too long to respond. Please check your connection and try again.'
            : err?.message || 'Network request failed',
        },
      };
    } finally {
      loadingBus.done();
    }
  }

  /**
   * GET a binary resource (e.g. a receipt PDF) with the same base URL, bearer
   * token and refresh-on-401 behaviour as request(). Never throws.
   */
  public async getBlob(endpoint: string): Promise<BlobResponse> {
    const url = endpoint.startsWith('http') ? endpoint : `${this.baseURL}${endpoint.startsWith('/') ? '' : '/'}${endpoint}`;
    const headers = new Headers();
    const token = this.getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const config: RequestInit = { method: 'GET', headers, credentials: 'include' };

    loadingBus.start();
    try {
      let response = await this.fetchWithTimeout(url, config);
      if (response.status === 401) {
        const newToken = await this.refreshAccessToken();
        if (!newToken) {
          return { success: false, error: { code: 'AUTH_EXPIRED', message: 'Session expired. Please sign in again.' } };
        }
        headers.set('Authorization', `Bearer ${newToken}`);
        response = await this.fetchWithTimeout(url, { ...config, headers });
      }
      if (!response.ok) {
        const body: any = await response.json().catch(() => null);
        return {
          success: false,
          error: body?.error || { code: `HTTP_${response.status}`, message: response.statusText || 'Request failed' },
        };
      }
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
      return { success: true, blob: await response.blob(), filename: match ? decodeURIComponent(match[1]) : undefined };
    } catch (err: any) {
      return {
        success: false,
        error: {
          code: err?.name === 'AbortError' ? 'TIMEOUT' : 'NETWORK_ERROR',
          message: err?.name === 'AbortError' ? 'The server took too long to respond.' : err?.message || 'Network request failed',
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

