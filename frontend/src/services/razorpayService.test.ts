import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { razorpayService } from './razorpayService';
import { httpClient } from './api/httpClient';

const SERVER_INV = '66666666-6666-4666-8666-666666666666';
const PAYMENT = {
  razorpay_payment_id: 'pay_TEST123',
  razorpay_order_id: 'order_TEST123',
  razorpay_signature: 'sig',
};

/** Fake Razorpay SDK: "pays" immediately when opened. */
function stubRazorpay(payment: Record<string, string>) {
  (window as any).Razorpay = class {
    private opts: any;
    constructor(opts: any) {
      this.opts = opts;
    }
    on() {}
    open() {
      void this.opts.handler({ ...payment, razorpay_order_id: this.opts.order_id || undefined });
    }
  };
}

describe('razorpayService', () => {
  beforeEach(() => {
    vi.spyOn(razorpayService, 'getKeyId').mockReturnValue('rzp_test_key');
  });

  afterEach(() => {
    httpClient.setToken(null);
    delete (window as any).Razorpay;
    vi.restoreAllMocks();
  });

  it('server invoice: sends invoice_id to /order and /verify and never posts /pay', async () => {
    httpClient.setToken('session-token');
    stubRazorpay(PAYMENT);
    const postSpy = vi.spyOn(httpClient, 'post').mockImplementation(async (url: string) => {
      if (url === '/payments/razorpay/order') return { success: true, data: { orderId: 'order_TEST123' } };
      if (url === '/payments/razorpay/verify') {
        return { success: true, data: { verified: true, recorded: true, duplicate: false, payment_number: 'RZP-pay_TEST123' } };
      }
      return { success: true, data: null };
    });

    const outcome = await razorpayService.openCheckout({ amount: 1500.5, name: 'School', description: 'Fee', invoiceId: SERVER_INV });

    expect(outcome.success).toBe(true);
    expect(outcome.recordedOnServer).toBe(true);
    expect(outcome.serverPaymentNumber).toBe('RZP-pay_TEST123');
    const order = postSpy.mock.calls.find(c => c[0] === '/payments/razorpay/order')!;
    expect(order[1]).toMatchObject({ amount: 150050, currency: 'INR', invoice_id: SERVER_INV });
    const verify = postSpy.mock.calls.find(c => c[0] === '/payments/razorpay/verify')!;
    expect(verify[1]).toMatchObject({ invoice_id: SERVER_INV, razorpay_payment_id: 'pay_TEST123' });
    expect(postSpy.mock.calls.some(c => String(c[0]).includes('/finance/invoices'))).toBe(false);
  });

  it('server invoice: a failed verification (5xx / network) is NOT treated as success', async () => {
    httpClient.setToken('session-token');
    stubRazorpay(PAYMENT);
    vi.spyOn(httpClient, 'post').mockImplementation(async (url: string) => {
      if (url === '/payments/razorpay/order') return { success: true, data: { orderId: 'order_TEST123' } };
      return { success: false, data: null, error: { code: 'HTTP_500', message: 'Internal Server Error' } };
    });

    const outcome = await razorpayService.openCheckout({ amount: 100, name: 'S', description: 'F', invoiceId: SERVER_INV });
    expect(outcome.success).toBe(false);
    expect(outcome.error).toContain('pay_TEST123');
  });

  it('server invoice: an order that cannot be created stops before checkout', async () => {
    httpClient.setToken('session-token');
    stubRazorpay(PAYMENT);
    vi.spyOn(httpClient, 'post').mockResolvedValue({
      success: false,
      data: null,
      error: { code: 'AMOUNT_EXCEEDS_BALANCE', message: "Amount exceeds the invoice's remaining balance." },
    });
    const outcome = await razorpayService.openCheckout({ amount: 99999, name: 'S', description: 'F', invoiceId: SERVER_INV });
    expect(outcome).toEqual({ success: false, error: "Amount exceeds the invoice's remaining balance." });
  });

  it('local/demo invoice: order-less test checkout with no server calls', async () => {
    stubRazorpay({ razorpay_payment_id: 'pay_LOCAL' });
    const postSpy = vi.spyOn(httpClient, 'post');
    const outcome = await razorpayService.openCheckout({ amount: 500, name: 'S', description: 'F', invoiceId: 'inv-101' });
    expect(outcome.success).toBe(true);
    expect(outcome.recordedOnServer).toBe(false);
    expect(postSpy).not.toHaveBeenCalled();
  });
});
