import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { OnlineCheckoutModal } from './OnlineCheckoutModal';
import { feePortalService, PaymentConfig, PortalInvoice } from '../../../services/feePortalService';

const INVOICE: PortalInvoice = {
  id: '11111111-1111-4111-8111-111111111111',
  invoice_number: 'INV-2026-0001',
  title: 'Term 1 fees',
  status: 'PARTIALLY_PAID',
  issue_date: '2026-04-01',
  due_date: '2026-04-30',
  is_overdue: false,
  subtotal: 10000,
  discount_amount: 0,
  fine_amount: 0,
  total_amount: 10000,
  paid_amount: 2000,
  refunded_amount: 0,
  balance_amount: 8000,
  items: [{ fee_head: 'Tuition', amount: 10000 }],
  adjustments: [],
  installments: [],
  next_payable_amount: 3000,
};

const MOCK_CFG: PaymentConfig = { mode: 'mock', is_test: true, key_id: null, currency: 'INR', school_name: 'NTS School' };
const LIVE_CFG: PaymentConfig = { mode: 'live', is_test: true, key_id: 'rzp_test_abc', currency: 'INR', school_name: 'NTS School' };

const order = (mode: 'mock' | 'live') => ({
  success: true,
  data: {
    order_id: 'ord-1',
    gateway_order_id: 'order_GW1',
    amount_paise: 300000,
    currency: 'INR',
    key_id: mode === 'live' ? 'rzp_test_abc' : null,
    mode,
    is_test: true,
    invoice_number: INVOICE.invoice_number,
    student_name: 'Asha',
    prefill: { name: 'Asha Parent', email: 'p@example.com', contact: '9999999999' },
  },
});

/** Fake Razorpay SDK capturing the options and event handlers. */
function stubRazorpay() {
  const ctl: { opts: any; failed?: (r: any) => void; opened: boolean } = { opts: null, opened: false };
  (window as any).Razorpay = class {
    constructor(opts: any) {
      ctl.opts = opts;
    }
    on(evt: string, cb: (r: any) => void) {
      if (evt === 'payment.failed') ctl.failed = cb;
    }
    open() {
      ctl.opened = true;
    }
  };
  return ctl;
}

const renderModal = (config: PaymentConfig, extra: Partial<React.ComponentProps<typeof OnlineCheckoutModal>> = {}) =>
  render(<OnlineCheckoutModal invoice={INVOICE} config={config} studentName="Asha" onClose={vi.fn()} {...extra} />);

describe('OnlineCheckoutModal', () => {
  afterEach(() => {
    delete (window as any).Razorpay;
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('mock mode: success shows the receipt number from the server', async () => {
    const create = vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('mock') as any);
    const complete = vi
      .spyOn(feePortalService, 'mockComplete')
      .mockResolvedValue({ success: true, data: { status: 'SUCCEEDED', receipt_id: 'rc-1', receipt_number: 'RCT-2026-000042' } });
    const onSettled = vi.fn();
    const onReceiptAction = vi.fn();
    renderModal(MOCK_CFG, { onSettled, onReceiptAction });

    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    expect(await screen.findByText('TEST MODE — no real money will be charged')).toBeInTheDocument();
    expect(create.mock.calls[0][0]).toMatchObject({ invoiceId: INVOICE.id, amount: undefined });
    expect(typeof create.mock.calls[0][0].idempotencyKey).toBe('string');

    fireEvent.click(screen.getByRole('button', { name: 'Simulate success' }));
    expect(await screen.findByTestId('receipt-number')).toHaveTextContent('RCT-2026-000042');
    expect(screen.getByText('Payment successful')).toBeInTheDocument();
    expect(complete).toHaveBeenCalledWith('ord-1', 'success');
    expect(onSettled).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /Download receipt/ }));
    expect(onReceiptAction).toHaveBeenCalledWith('rc-1', 'download', 'RCT-2026-000042');
  });

  it('mock mode: failure shows a failure and never success', async () => {
    vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('mock') as any);
    vi.spyOn(feePortalService, 'mockComplete').mockResolvedValue({
      success: true,
      data: { status: 'FAILED', failure_reason: 'Simulated failure' },
    });
    renderModal(MOCK_CFG);
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Simulate failure' }));
    expect(await screen.findByText(/Simulated failure/)).toBeInTheDocument();
    expect(screen.queryByText('Payment successful')).not.toBeInTheDocument();
  });

  it('double-click on Proceed creates only one order', async () => {
    let resolve!: (v: any) => void;
    const create = vi.spyOn(feePortalService, 'createOrder').mockImplementation(() => new Promise(r => (resolve = r)));
    renderModal(MOCK_CFG);
    const btn = screen.getByRole('button', { name: 'Proceed to pay' });
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(create).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Starting payment…' })).toBeDisabled();
    await act(async () => resolve(order('mock')));
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('custom amount must be between ₹1 and the balance', async () => {
    const create = vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('mock') as any);
    renderModal(MOCK_CFG);
    fireEvent.click(screen.getByLabelText('Another amount'));
    fireEvent.change(screen.getByLabelText(/Amount \(₹1/), { target: { value: '9000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('cannot exceed the balance');
    expect(create).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/Amount \(₹1/), { target: { value: '2500' } });
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0].amount).toBe(2500);
  });

  it('live: handler → verify SUCCEEDED shows success with the receipt', async () => {
    const rzp = stubRazorpay();
    vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('live') as any);
    const verify = vi
      .spyOn(feePortalService, 'verify')
      .mockResolvedValue({ success: true, data: { status: 'SUCCEEDED', receipt_id: 'rc-9', receipt_number: 'RCT-2026-000009' } });
    renderModal(LIVE_CFG);
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    await waitFor(() => expect(rzp.opened).toBe(true));
    expect(rzp.opts).toMatchObject({ key: 'rzp_test_abc', order_id: 'order_GW1', amount: 300000, currency: 'INR' });
    expect(rzp.opts.prefill).toMatchObject({ email: 'p@example.com' });

    await act(async () => {
      rzp.opts.handler({ razorpay_order_id: 'order_GW1', razorpay_payment_id: 'pay_1', razorpay_signature: 'sig' });
    });
    expect(verify).toHaveBeenCalledWith({
      order_id: 'ord-1',
      razorpay_order_id: 'order_GW1',
      razorpay_payment_id: 'pay_1',
      razorpay_signature: 'sig',
    });
    expect(await screen.findByTestId('receipt-number')).toHaveTextContent('RCT-2026-000009');
  });

  it('live: a verify error (signature mismatch / 5xx) never shows success', async () => {
    const rzp = stubRazorpay();
    vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('live') as any);
    vi.spyOn(feePortalService, 'verify').mockResolvedValue({
      success: false,
      data: null as any,
      error: { code: 'SIGNATURE_MISMATCH', message: 'Invalid signature' },
    });
    renderModal(LIVE_CFG);
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    await waitFor(() => expect(rzp.opened).toBe(true));
    await act(async () => {
      rzp.opts.handler({ razorpay_order_id: 'order_GW1', razorpay_payment_id: 'pay_1', razorpay_signature: 'bad' });
    });
    expect(await screen.findByText(/has NOT been recorded/)).toBeInTheDocument();
    expect(screen.queryByText('Payment successful')).not.toBeInTheDocument();
  });

  it('live: dismissing checkout cancels the order', async () => {
    const rzp = stubRazorpay();
    vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('live') as any);
    const cancel = vi.spyOn(feePortalService, 'cancelOrder').mockResolvedValue({ success: true, data: { status: 'CANCELLED' } });
    renderModal(LIVE_CFG);
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    await waitFor(() => expect(rzp.opened).toBe(true));
    await act(async () => {
      rzp.opts.modal.ondismiss();
    });
    expect(cancel).toHaveBeenCalledWith('ord-1');
    expect(await screen.findByText(/Payment was cancelled/)).toBeInTheDocument();
  });

  it('live: payment.failed reports the failure to the server', async () => {
    const rzp = stubRazorpay();
    vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('live') as any);
    const fail = vi.spyOn(feePortalService, 'failOrder').mockResolvedValue({ success: true, data: { status: 'FAILED' } });
    renderModal(LIVE_CFG);
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    await waitFor(() => expect(rzp.opened).toBe(true));
    await act(async () => {
      rzp.failed!({ error: { description: 'Card declined' } });
    });
    expect(fail).toHaveBeenCalledWith('ord-1', 'Card declined');
    expect(await screen.findByText(/Card declined/)).toBeInTheDocument();
  });

  it('live: PENDING polls getOrder every 3s and stops on SUCCEEDED', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const rzp = stubRazorpay();
    vi.spyOn(feePortalService, 'createOrder').mockResolvedValue(order('live') as any);
    vi.spyOn(feePortalService, 'verify').mockResolvedValue({ success: true, data: { status: 'PENDING' } });
    const getOrder = vi
      .spyOn(feePortalService, 'getOrder')
      .mockResolvedValueOnce({ success: true, data: { status: 'PENDING' } })
      .mockResolvedValueOnce({ success: true, data: { status: 'SUCCEEDED', receipt_id: 'rc-3', receipt_number: 'RCT-2026-000003' } });
    renderModal(LIVE_CFG);
    fireEvent.click(screen.getByRole('button', { name: 'Proceed to pay' }));
    await waitFor(() => expect(rzp.opened).toBe(true));
    await act(async () => {
      rzp.opts.handler({ razorpay_order_id: 'order_GW1', razorpay_payment_id: 'pay_1', razorpay_signature: 'sig' });
    });
    expect(await screen.findByText(/Payment processing — we'll update automatically/)).toBeInTheDocument();
    expect(screen.queryByText('Payment successful')).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(getOrder).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(await screen.findByTestId('receipt-number')).toHaveTextContent('RCT-2026-000003');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });
    expect(getOrder).toHaveBeenCalledTimes(2);
  });
});
