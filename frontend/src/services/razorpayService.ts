// ==========================================
// Razorpay Checkout integration
// Loads the Razorpay SDK on demand and opens the hosted checkout for fee
// payments. Works in Razorpay Test Mode with just a publishable key id
// (VITE_RAZORPAY_KEY_ID).
//  - Server invoices (UUID ids, signed-in session): a server order bound to the
//    invoice is created, and /razorpay/verify both checks the signature AND
//    records the payment server-side (idempotent on the payment id). The client
//    must not POST /finance/invoices/{id}/pay for the same payment.
//  - Local / demo invoices: order-less test checkout, nothing hits the server.
// Developed by NTS Novalytix
// ==========================================

import { httpClient } from './api/httpClient';
import { isServerId } from '../utils/ids';

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
  /** Amount in rupees (will be converted to integer paise). */
  amount: number;
  /** Merchant / school name shown on the checkout. */
  name: string;
  description: string;
  /**
   * The fee invoice being paid. A server (UUID) invoice gets a server order and
   * is recorded by /razorpay/verify; any other id uses the local test checkout.
   */
  invoiceId?: string;
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
  /**
   * True when the backend already recorded the payment against the invoice
   * (during verify). Callers must then NOT post a separate /pay for it.
   */
  recordedOnServer?: boolean;
  /** Server receipt / payment number (e.g. RZP-pay_xxx) when recorded. */
  serverPaymentNumber?: string;
  /** True when the server had already recorded this payment (replay). */
  duplicate?: boolean;
}

export interface RazorpayVerification {
  /** true = signature valid, false = tampered, null = could not verify. */
  verified: boolean | null;
  /** The payment was recorded server-side against the invoice. */
  recorded?: boolean;
  duplicate?: boolean;
  paymentNumber?: string;
  error?: string;
}

/** Whether this invoice should go through the server-recorded payment flow. */
const isServerInvoice = (invoiceId?: string): boolean => !!invoiceId && isServerId(invoiceId) && !!httpClient.getToken();

export const razorpayService = {
  /** Publishable key id, read from the Vite environment. */
  getKeyId(): string {
    return ((import.meta as any).env?.VITE_RAZORPAY_KEY_ID as string) || '';
  },

  isConfigured(): boolean {
    return !!this.getKeyId();
  },

  /**
   * Create a server-side order bound to a server invoice. Amount is integer
   * paise (>= 100, <= the invoice balance, INR only — enforced by the server).
   */
  async createOrder(
    amountPaise: number,
    invoiceId: string,
    notes?: Record<string, string>
  ): Promise<{ orderId: string | null; error?: string }> {
    const res = await httpClient.post<{ orderId?: string; id?: string }>('/payments/razorpay/order', {
      amount: Math.round(amountPaise),
      currency: 'INR',
      invoice_id: invoiceId,
      notes,
    });
    if (res?.success && res.data) {
      const orderId = res.data.orderId || res.data.id || null;
      if (orderId) return { orderId };
    }
    return { orderId: null, error: res?.error?.message || 'Could not start the payment with the server.' };
  },

  /**
   * Verify an order-based payment's signature on the server. When `invoiceId`
   * is sent the server also records the payment (idempotently).
   */
  async verifyPayment(result: RazorpaySuccess, invoiceId?: string): Promise<RazorpayVerification> {
    if (!result.razorpay_order_id || !result.razorpay_signature) return { verified: null };
    const res = await httpClient.post<{
      verified?: boolean;
      recorded?: boolean;
      duplicate?: boolean;
      payment_number?: string;
    }>('/payments/razorpay/verify', {
      razorpay_order_id: result.razorpay_order_id,
      razorpay_payment_id: result.razorpay_payment_id,
      razorpay_signature: result.razorpay_signature,
      ...(invoiceId ? { invoice_id: invoiceId } : {}),
    });
    const data = res?.data;
    if (res?.success && data?.verified) {
      return {
        verified: true,
        recorded: !!data.recorded,
        duplicate: !!data.duplicate,
        paymentNumber: data.payment_number,
      };
    }
    // Explicit signature mismatch → reject (never mark as paid).
    if (res?.error?.code === 'SIGNATURE_MISMATCH' || data?.verified === false) {
      return { verified: false, error: res?.error?.message };
    }
    // Signature OK but recording failed (e.g. Razorpay unreachable) — retryable.
    if (data?.verified === true) {
      return { verified: true, recorded: false, error: res?.error?.message };
    }
    return { verified: null, error: res?.error?.message };
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

    const amountPaise = Math.round(params.amount * 100);
    const serverInvoice = isServerInvoice(params.invoiceId);
    if (serverInvoice && amountPaise < 100) {
      return { success: false, error: 'The minimum online payment is ₹1.' };
    }

    const loaded = await loadSdk();
    if (!loaded || !(window as any).Razorpay) {
      return { success: false, error: 'Could not load Razorpay checkout. Please check your internet connection and try again.' };
    }

    // Server invoices must have a server order (it is what lets verify record
    // the payment). Local/demo invoices never touch the server.
    let orderId: string | null = null;
    if (serverInvoice) {
      const order = await this.createOrder(amountPaise, params.invoiceId!, params.notes);
      if (!order.orderId) {
        return { success: false, error: order.error };
      }
      orderId = order.orderId;
    }

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
          if (!orderId) {
            // Pure demo/offline mode: no server order was created, so there is
            // nothing to verify against (order-less test checkout).
            done({ success: true, result: response, recordedOnServer: false });
            return;
          }

          // Verify (and record) on the server. Recording is idempotent on the
          // payment id, so a transient failure is retried a couple of times.
          let v = await this.verifyPayment(response, params.invoiceId);
          for (let attempt = 0; attempt < 2 && v.verified === true && !v.recorded; attempt++) {
            await new Promise(r => setTimeout(r, 1500));
            v = await this.verifyPayment(response, params.invoiceId);
          }

          if (v.verified === true && v.recorded) {
            done({
              success: true,
              result: response,
              recordedOnServer: true,
              serverPaymentNumber: v.paymentNumber,
              duplicate: v.duplicate,
            });
          } else if (v.verified === false) {
            done({ success: false, error: 'Payment signature verification failed. If money was debited it will be auto-refunded.' });
          } else {
            // A server order exists but verification/recording could not
            // complete (5xx, network, expired session): never mark it paid here.
            done({
              success: false,
              error: `Payment could not be confirmed right now (payment id ${response.razorpay_payment_id}). ${v.error ? v.error + ' ' : ''}Please contact the school office before retrying.`,
            });
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
