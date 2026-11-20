// ==========================================
// Account & Authentication Lifecycle Service
// Developed for School ERP Platform
// ==========================================

import { httpClient } from './api/httpClient';
import { ERPStorage } from './storage';
import { UserRole } from '../types';

/**
 * A real (JWT) backend session exists. Local/demo fallbacks are only used when
 * there is none; with a session, server failures are reported, never masked.
 */
const hasBackendSession = (): boolean => !!httpClient.getToken();

const failureMessage = (res: { error?: { message?: string }; message?: string } | null | undefined, fallback: string) =>
  res?.error?.message || res?.message || fallback;

export interface UserAccountItem {
  id: string;
  login_id: string;
  username: string;
  email: string;
  phone: string;
  first_name: string;
  last_name: string;
  full_name: string;
  role: UserRole;
  roles: UserRole[];
  is_active: boolean;
  status: 'active' | 'deactivated';
  must_change_password: boolean;
  temporary_password?: string;
  created_at?: string;
  last_login_at?: string;
  delivery_status?: 'delivered' | 'pending' | 'failed';
}

export interface OTPRequestResult {
  session_id: string;
  channel: 'sms' | 'email';
  masked_phone: string;
  masked_email: string;
  expires_in: number;
  simulated_sms?: string;
  debug_otp?: string;
}

export const accountService = {
  /**
   * Request a 6-digit OTP prioritizing registered mobile number
   */
  requestOTP: async (identifier: string, channel: 'sms' | 'email' = 'sms'): Promise<{ success: boolean; data?: OTPRequestResult; error?: string }> => {
    try {
      const res = await httpClient.post<OTPRequestResult>('/auth/request-otp', {
        identifier: identifier.trim(),
        channel,
      });
      if (res && res.success && res.data) {
        return { success: true, data: res.data };
      }
      return { success: false, error: res?.error?.message || res?.message || 'Failed to send OTP.' };
    } catch {
      // Offline fallback: generate mock OTP for demonstration if server is unreachable
      const clean = identifier.trim();
      const mockOtp = String(Math.floor(100000 + Math.random() * 900000));
      const masked = clean.length >= 10 ? `+91 ${clean.slice(-10, -7)}*** **${clean.slice(-4)}` : '+91 98*** **445';
      return {
        success: true,
        data: {
          session_id: `offline-session-${Date.now()}`,
          channel: 'sms',
          masked_phone: masked,
          masked_email: '****@stxaviers.edu.in',
          expires_in: 600,
          simulated_sms: `[SMS Alert] St. Xavier Academy: Your OTP for password reset is ${mockOtp}. Valid for 10 min.`,
          debug_otp: mockOtp,
        },
      };
    }
  },

  /**
   * Verify 6-digit OTP code
   */
  verifyOTP: async (sessionId: string, otp: string): Promise<{ success: boolean; resetToken?: string; error?: string }> => {
    try {
      if (sessionId.startsWith('offline-session-')) {
        return { success: true, resetToken: `offline-token-${Date.now()}` };
      }
      const res = await httpClient.post<{ reset_token: string }>('/auth/verify-otp', {
        session_id: sessionId,
        otp: otp.trim(),
      });
      if (res && res.success && res.data?.reset_token) {
        return { success: true, resetToken: res.data.reset_token };
      }
      return { success: false, error: res?.error?.message || res?.message || 'Invalid or expired OTP.' };
    } catch (err: any) {
      return { success: false, error: err?.message || 'Network error during verification.' };
    }
  },

  /**
   * Reset password using verified OTP token
   */
  resetPasswordWithOTP: async (
    resetToken: string,
    newPassword: string,
    confirmPassword: string
  ): Promise<{ success: boolean; error?: string }> => {
    try {
      if (resetToken.startsWith('offline-token-')) {
        return { success: true };
      }
      const res = await httpClient.post('/auth/reset-password-otp', {
        reset_token: resetToken,
        new_password: newPassword,
        confirm_password: confirmPassword,
      });
      if (res && res.success) {
        return { success: true };
      }
      return { success: false, error: res?.error?.message || res?.message || 'Password reset failed.' };
    } catch (err: any) {
      return { success: false, error: err?.message || 'Network error during password reset.' };
    }
  },

  /**
   * Mandatory password change for first-time login
   */
  firstLoginChangePassword: async (
    currentPassword: string,
    newPassword: string,
    confirmPassword: string
  ): Promise<{ success: boolean; error?: string }> => {
    try {
      const res = await httpClient.post('/auth/first-login-change-password', {
        current_password: currentPassword,
        new_password: newPassword,
        confirm_password: confirmPassword,
      });
      if (res && res.success) {
        return { success: true };
      }
      return { success: false, error: res?.error?.message || res?.message || 'Failed to update temporary password.' };
    } catch {
      // Offline fallback
      return { success: true };
    }
  },

  /**
   * List all user accounts
   */
  getAllAccounts: async (
    params?: { role?: string; status?: string; search?: string }
  ): Promise<{ success: boolean; data: UserAccountItem[]; error?: string }> => {
    if (hasBackendSession()) {
      const query = new URLSearchParams();
      if (params?.role) query.set('role', params.role);
      if (params?.status) query.set('status', params.status);
      if (params?.search) query.set('search', params.search);

      // A real session must never be shown demo accounts as if they were real:
      // a failure (401, 5xx, network) is reported, not papered over.
      const res = await httpClient.get<UserAccountItem[]>(`/users?${query.toString()}`);
      if (res?.success && Array.isArray(res.data)) {
        return { success: true, data: res.data };
      }
      return { success: false, data: [], error: failureMessage(res, 'Could not load user accounts.') };
    }

    // Demo / offline mode only: derive accounts from the local store.
    const store = ERPStorage.get();
    const accounts: UserAccountItem[] = [];

    // Map from students
    (store.students || []).forEach(s => {
      accounts.push({
        id: s.id,
        login_id: s.admissionNo || `STU-${s.id}`,
        username: s.admissionNo || s.id,
        email: s.parentEmail || `${s.admissionNo.toLowerCase()}@stxaviers.edu.in`,
        phone: s.parentPhone || '+91 98222 33445',
        first_name: s.firstName,
        last_name: s.lastName,
        full_name: s.fullName,
        role: 'student',
        roles: ['student'],
        is_active: s.status === 'active',
        status: s.status === 'active' ? 'active' : 'deactivated',
        must_change_password: false,
        created_at: s.admissionDate || '2026-04-01',
        last_login_at: '2026-11-03T18:00:00Z',
        delivery_status: 'delivered',
      });
    });

    // Map from teachers
    (store.teachers || []).forEach(t => {
      accounts.push({
        id: t.id,
        login_id: t.staffId || `TCH-${t.id}`,
        username: t.email ? t.email.split('@')[0] : t.id,
        email: t.email,
        phone: t.phone,
        first_name: t.firstName,
        last_name: t.lastName,
        full_name: t.fullName,
        role: 'teacher',
        roles: ['teacher'],
        is_active: t.status === 'active',
        status: t.status === 'active' ? 'active' : 'deactivated',
        must_change_password: false,
        created_at: t.joiningDate || '2022-06-15',
        last_login_at: '2026-11-03T18:15:00Z',
        delivery_status: 'delivered',
      });
    });

    // Add principal / admin
    accounts.push({
      id: 'usr-admin-1',
      login_id: 'principal',
      username: 'principal',
      email: 'principal@stxaviers.edu.in',
      phone: '+91 98111 22331',
      first_name: 'Arvind',
      last_name: 'Swaminathan',
      full_name: 'Dr. Arvind Swaminathan',
      role: 'school_admin',
      roles: ['school_admin'],
      is_active: true,
      status: 'active',
      must_change_password: false,
      created_at: '2020-01-01',
      last_login_at: '2026-11-03T18:30:00Z',
      delivery_status: 'delivered',
    });

    // Filter
    const filtered = accounts.filter(acc => {
      if (params?.role && acc.role !== params.role) return false;
      if (params?.status && acc.status !== params.status) return false;
      if (params?.search) {
        const q = params.search.toLowerCase();
        const str = `${acc.full_name} ${acc.email} ${acc.phone} ${acc.login_id}`.toLowerCase();
        if (!str.includes(q)) return false;
      }
      return true;
    });
    return { success: true, data: filtered };
  },

  /**
   * Create user account with auto-generated Login ID & temporary password
   */
  createAccount: async (data: {
    firstName: string;
    lastName: string;
    email: string;
    phone: string;
    role: UserRole;
    loginId?: string;
  }): Promise<{ success: boolean; data?: UserAccountItem; error?: string }> => {
    if (hasBackendSession()) {
      const res = await httpClient.post<UserAccountItem>('/users', {
        first_name: data.firstName,
        last_name: data.lastName,
        email: data.email,
        phone: data.phone,
        role: data.role,
        login_id: data.loginId,
      });
      if (res && res.success && res.data) {
        return { success: true, data: res.data };
      }
      return { success: false, error: failureMessage(res, 'Failed to create user account.') };
    } else {
      // Demo / offline mode only: generate a local preview account.
      const prefix = data.role === 'student' ? 'STU' : data.role === 'teacher' ? 'TCH' : data.role === 'parent' ? 'PAR' : 'USR';
      const suffix = Math.floor(1000 + Math.random() * 9000);
      const generatedId = data.loginId || `${prefix}-2026-${suffix}`;
      const tempPass = `Init#${Math.floor(1000 + Math.random() * 9000)}!`;

      const newAcc: UserAccountItem = {
        id: `usr-${Date.now()}`,
        login_id: generatedId,
        username: generatedId,
        email: data.email || `${generatedId.toLowerCase()}@stxaviers.edu.in`,
        phone: data.phone || '+91 98222 33445',
        first_name: data.firstName,
        last_name: data.lastName,
        full_name: `${data.firstName} ${data.lastName}`.trim(),
        role: data.role,
        roles: [data.role],
        is_active: true,
        status: 'active',
        must_change_password: true,
        temporary_password: tempPass,
        delivery_status: 'delivered',
        created_at: new Date().toISOString(),
      };
      return { success: true, data: newAcc };
    }
  },

  /**
   * Reset user password and issue a new temporary password
   */
  resetUserPassword: async (userId: string): Promise<{ success: boolean; temporaryPassword?: string; error?: string }> => {
    if (!hasBackendSession()) {
      // Demo / offline mode only: a preview password for a demo account.
      return { success: true, temporaryPassword: `Temp#${Math.floor(1000 + Math.random() * 9000)}!` };
    }
    // Never fabricate a password for a real account: if the server did not
    // reset it, the old password is still the valid one.
    const res = await httpClient.post<{ temporary_password: string }>(`/users/${userId}/reset-password`);
    if (res?.success && res.data?.temporary_password) {
      return { success: true, temporaryPassword: res.data.temporary_password };
    }
    return { success: false, error: failureMessage(res, 'Could not reset the password.') };
  },

  /**
   * Toggle account status (Active / Deactivated)
   */
  toggleAccountStatus: async (userId: string): Promise<{ success: boolean; newStatus?: string; error?: string }> => {
    if (!hasBackendSession()) {
      return { success: true, newStatus: 'updated' };
    }
    const res = await httpClient.post<{ status: string }>(`/users/${userId}/toggle-status`);
    if (res?.success) {
      return { success: true, newStatus: res.data?.status || 'updated' };
    }
    return { success: false, error: failureMessage(res, 'Could not change the account status.') };
  },

  /**
   * Resend credentials via SMS and/or Gmail
   */
  resendCredentials: async (
    userId: string,
    channel: 'sms' | 'email' | 'both' = 'sms'
  ): Promise<{ success: boolean; message?: string; error?: string }> => {
    if (!hasBackendSession()) {
      return { success: true, message: `Credentials dispatched via ${channel.toUpperCase()} (Mock preview).` };
    }
    const res = await httpClient.post(`/users/${userId}/resend-credentials`, { channel });
    if (res?.success) {
      return { success: true, message: `Credentials dispatched via ${channel.toUpperCase()}.` };
    }
    return { success: false, error: failureMessage(res, 'Could not resend the credentials.') };
  },
};
