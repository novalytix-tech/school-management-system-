import { describe, it, expect, afterEach, vi } from 'vitest';
import { feePortalService, refundableAmount, isRefundable, newIdempotencyKey } from './feePortalService';
import { httpClient } from './api/httpClient';

const ok = (data: any) => ({ success: true, data });

describe('feePortalService request shapes', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('getConfig / getMyFees hit the contract endpoints', async () => {
    const get = vi.spyOn(httpClient, 'get').mockResolvedValue(ok({}));
    await feePortalService.getConfig();
    await feePortalService.getMyFees();
    await feePortalService.getMyFees('stu-1');
    expect(get.mock.calls.map(c => c[0])).toEqual(['/payments/config', '/finance/my-fees', '/finance/my-fees?student_id=stu-1']);
  });

  it('createOrder sends invoice_id, rupee amount and idempotency_key (amount omitted when not given)', async () => {
    const post = vi.spyOn(httpClient, 'post').mockResolvedValue(ok({ order_id: 'o1' }));
    await feePortalService.createOrder({ invoiceId: 'inv-1', amount: 1500.5, idempotencyKey: 'k1' });
    await feePortalService.createOrder({ invoiceId: 'inv-1', idempotencyKey: 'k2' });
    expect(post.mock.calls[0]).toEqual(['/payments/razorpay/order', { invoice_id: 'inv-1', amount: 1500.5, idempotency_key: 'k1' }]);
    expect(post.mock.calls[1]).toEqual(['/payments/razorpay/order', { invoice_id: 'inv-1', idempotency_key: 'k2' }]);
  });

  it('verify / cancel / fail / mock complete / getOrder', async () => {
    const post = vi.spyOn(httpClient, 'post').mockResolvedValue(ok({ status: 'SUCCEEDED' }));
    const get = vi.spyOn(httpClient, 'get').mockResolvedValue(ok({ status: 'PENDING' }));
    const v = { order_id: 'o1', razorpay_order_id: 'order_X', razorpay_payment_id: 'pay_X', razorpay_signature: 'sig' };
    await feePortalService.verify(v);
    await feePortalService.cancelOrder('o1');
    await feePortalService.failOrder('o1', 'card declined');
    await feePortalService.mockComplete('o1', 'success');
    await feePortalService.getOrder('o1');
    expect(post.mock.calls).toEqual([
      ['/payments/razorpay/verify', v],
      ['/payments/orders/o1/cancel', {}],
      ['/payments/orders/o1/fail', { reason: 'card declined' }],
      ['/payments/mock/orders/o1/complete', { outcome: 'success' }],
    ]);
    expect(get).toHaveBeenCalledWith('/payments/orders/o1');
  });

  it('receipts: list unwraps arrays and {items}; PDF goes through getBlob', async () => {
    const get = vi
      .spyOn(httpClient, 'get')
      .mockResolvedValueOnce(ok([{ id: 'r1', receipt_number: 'RCT-2026-000001' }]))
      .mockResolvedValueOnce(ok({ items: [{ id: 'r2', receipt_number: 'RCT-2026-000002' }], total: 1 }));
    const blob = vi.spyOn(httpClient, 'getBlob').mockResolvedValue({ success: true, blob: new Blob(['%PDF']) });
    expect((await feePortalService.listReceipts('s1')).data[0].id).toBe('r1');
    expect((await feePortalService.listReceipts()).data[0].id).toBe('r2');
    expect(get.mock.calls[0][0]).toBe('/finance/receipts?student_id=s1');
    await feePortalService.getReceiptPdf('r1');
    expect(blob).toHaveBeenCalledWith('/finance/receipts/r1/pdf');
  });

  it('admin endpoints', async () => {
    const post = vi.spyOn(httpClient, 'post').mockResolvedValue(ok({}));
    const del = vi.spyOn(httpClient, 'delete').mockResolvedValue(ok({}));
    const get = vi.spyOn(httpClient, 'get').mockResolvedValue(ok({ items: [], total: 0, page: 2, pages: 1 }));

    await feePortalService.createInvoice({ student_id: 's1', fee_structure_id: 'fs1', installment_count: 3 });
    await feePortalService.generateInvoices('fs1', { installment_count: 4 });
    await feePortalService.generateInvoices('fs2');
    await feePortalService.addAdjustment('inv1', { kind: 'SCHOLARSHIP', label: 'Merit', amount: 500 });
    await feePortalService.removeAdjustment('inv1', 'adj1');
    await feePortalService.refund('pay1', 100.256, 'Duplicate');
    await feePortalService.recordOfflinePayment('inv1', { amount: 200, payment_method: 'CASH' });
    const list = await feePortalService.listPayments({ student_id: 's1', status: 'SUCCESS', from: '2026-01-01', to: '2026-02-01', page: 2 });

    expect(post.mock.calls).toEqual([
      ['/finance/invoices', { student_id: 's1', fee_structure_id: 'fs1', installment_count: 3 }],
      ['/finance/fee-structures/fs1/generate-invoices', { installment_count: 4 }],
      ['/finance/fee-structures/fs2/generate-invoices', {}],
      ['/finance/invoices/inv1/adjustments', { kind: 'SCHOLARSHIP', label: 'Merit', amount: 500 }],
      ['/finance/payments/pay1/refund', { amount: 100.26, reason: 'Duplicate' }],
      ['/finance/invoices/inv1/pay', { amount: 200, payment_method: 'CASH' }],
    ]);
    expect(del).toHaveBeenCalledWith('/finance/invoices/inv1/adjustments/adj1');
    expect(get).toHaveBeenCalledWith('/finance/payments?student_id=s1&status=SUCCESS&from=2026-01-01&to=2026-02-01&page=2');
    expect(list.data.page).toBe(2);
  });

  it('refund helpers and idempotency keys', () => {
    expect(refundableAmount({ amount: 1000, refunded_amount: 250 })).toBe(750);
    expect(refundableAmount({ amount: 1000, refunded_amount: 0, refundable_amount: 300 })).toBe(300);
    expect(isRefundable({ id: 'p', amount: 100, refunded_amount: 100, status: 'PARTIALLY_REFUNDED' })).toBe(false);
    expect(isRefundable({ id: 'p', amount: 100, status: 'SUCCESS' })).toBe(true);
    expect(isRefundable({ id: 'o', kind: 'order', amount: 100, status: 'SUCCESS' })).toBe(false);
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
