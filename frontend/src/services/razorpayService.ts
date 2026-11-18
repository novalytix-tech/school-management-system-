// ==========================================
// Razorpay Checkout integration
// Loads the Razorpay SDK on demand and opens the hosted checkout for fee
// payments. Works in Razorpay Test Mode with just a publishable key id
// (VITE_RAZORPAY_KEY_ID). If a backend order endpoint is available it is used
// so the payment can be verified server-side; otherwise it falls back to an
// amount-only (order-less) test checkout.
// Developed by NTS Novalytix
// ==========================================

import { httpClient } from './api/httpClient';

const RAZORPAY_SDK_URL = 'https://checkout.razorpay.com/v1/checkout.js';

let sdkPromise: Promise<boolean> | null = null;

function loadSdk(): Promise<boolean> {
  if (typeof window === 'undefined') return Promise.resolve(false);
  if ((window as any).Razorpay) return Promise.resolve(true);
  if (sdkPromise) return sdkPromise;

  sdkPromise = new Promise<boolean>(resolve => {
    const script = document.createElement('script');
    script.src = RAZORPAY_SDK_URL;
    script.async = true;
    script.onload = () => resolve(true);
    script.onerror = () => {
      sdkPromise = null; // allow a retry on next attempt
      resolve(false);
    };
    document.body.appendChild(script);
  });
  return sdkPromise;
}

export interface RazorpayCheckoutParams {
  /** Amount in rupees (will be converted to paise). */
  amount: number;
  /** Merchant / school name shown on the checkout. */
  name: string;
  description: string;
  prefill?: { name?: string; email?: string; contact?: string };
  notes?: Record<string, string>;
}

export interface RazorpaySuccess {
  razorpay_payment_id: string;
  razorpay_order_id?: string;
  razorpay_signature?: string;
}

export interface RazorpayOutcome {
  success: boolean;
  result?: RazorpaySuccess;
  error?: string;
  /** True when the user closed the checkout without paying. */
  dismissed?: boolean;
}

export const razorpayService = {
  /** Publishable key id, read from the Vite environment. */
  getKeyId(): string {
    return ((import.meta as any).env?.VITE_RAZORPAY_KEY_ID as string) || '';
  },

  isConfigured(): boolean {
    return !!this.getKeyId();
  },

  /**
   * Try to create a server-side order (secure flow). Returns the order id, or
   * null if no backend order endpoint is available (then we use test checkout).
   */
  async createOrder(amountPaise: number, notes?: Record<string, string>): Promise<string | null> {
    try {
      const res = await httpClient.post<{ orderId?: string; id?: string }>('/payments/razorpay/order', {
        amount: amountPaise,
        currency: 'INR',
        notes,
      });
      if (res?.success && res.data) {
        return res.data.orderId || res.data.id || null;
      }
    } catch {
      /* backend order endpoint not available — fall back to order-less test checkout */
    }
    return null;
  },

  /**
   * Verify an order-based payment's signature on the server. Returns true when
   * verified, false when tampered, and null when verification is unavailable
   * (no backend / not configured) — callers treat null as "skip verification".
   */
  async verifyPayment(result: RazorpaySuccess): Promise<boolean | null> {
    if (!result.razorpay_order_id || !result.razorpay_signature) return null;
    try {
      const res = await httpClient.post<{ verified?: boolean }>('/payments/razorpay/verify', {
        razorpay_order_id: result.razorpay_order_id,
        razorpay_payment_id: result.razorpay_payment_id,
        razorpay_signature: result.razorpay_signature,
      });
      if (res?.success && res.data?.verified) return true;
      // Explicit signature mismatch → reject (never mark as paid).
      const code = (res as any)?.error?.code;
      if (code === 'SIGNATURE_MISMATCH' || (res as any)?.data?.verified === false) return false;
      // Not configured / unknown → skip verification (e.g. order-less test checkout).
      return null;
    } catch {
      return null;
    }
  },

  /**
   * Open the Razorpay checkout. Resolves on success (with the payment id),
   * or with an error / dismissed flag otherwise.
   */
  async openCheckout(params: RazorpayCheckoutParams): Promise<RazorpayOutcome> {
    const key = this.getKeyId();
    if (!key) {
      return {
        success: false,
        error:
          'Razorpay is not configured. Add VITE_RAZORPAY_KEY_ID (your rzp_test_… / rzp_live_… key) to frontend/.env and restart the dev server.',
      };
    }

    const loaded = await loadSdk();
    if (!loaded || !(window as any).Razorpay) {
      return { success: false, error: 'Could not load Razorpay checkout. Please check your internet connection and try again.' };
    }

    const amountPaise = Math.round(params.amount * 100);
    const orderId = await this.createOrder(amountPaise, params.notes);

    return new Promise<RazorpayOutcome>(resolve => {
      let settled = false;
      const done = (o: RazorpayOutcome) => {
        if (!settled) {
          settled = true;
          resolve(o);
        }
      };

      const options: any = {
        key,
        amount: amountPaise,
        currency: 'INR',
        name: params.name,
        description: params.description,
        image: '/nts-logo.png',
        prefill: params.prefill || {},
        notes: params.notes || {},
        theme: { color: '#3B82F6' },
        handler: async (response: RazorpaySuccess) => {
          // When the payment used a server order, verify its signature before
          // treating it as successful. A tampered/invalid signature is rejected.
          const verified = await this.verifyPayment(response);
          if (verified === false) {
            done({ success: false, error: 'Payment signature verification failed. If money was debited it will be auto-refunded.' });
          } else {
            done({ success: true, result: response });
          }
        },
        modal: {
          ondismiss: () => done({ success: false, dismissed: true, error: 'Payment was cancelled.' }),
          escape: true,
        },
      };
      if (orderId) options.order_id = orderId;

      try {
        const rzp = new (window as any).Razorpay(options);
        rzp.on('payment.failed', (resp: any) => {
          done({ success: false, error: resp?.error?.description || 'Payment failed at the gateway.' });
        });
        rzp.open();
      } catch (err: any) {
        done({ success: false, error: err?.message || 'Unable to open Razorpay checkout.' });
      }
    });
  },
};
