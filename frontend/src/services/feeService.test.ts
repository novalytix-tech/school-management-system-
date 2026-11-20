import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { feeService } from './feeService';
import { ERPStorage } from './storage';
import { httpClient } from './api/httpClient';

describe('feeService Financial Calculations & Workflows', () => {
  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
  });

  it('retrieves all seeded fee invoices', () => {
    const invoices = feeService.getAllInvoices();
    expect(Array.isArray(invoices)).toBe(true);
    expect(invoices.length).toBeGreaterThan(0);
    expect(invoices[0]).toHaveProperty('totalAmount');
    expect(invoices[0]).toHaveProperty('paidAmount');
  });

  it('calculates admin fee statistics accurately', () => {
    const stats = feeService.getAdminStats();
    expect(stats).toBeDefined();
    expect(stats.totalBilled).toBeGreaterThan(0);
    expect(stats.totalCollected).toBeGreaterThanOrEqual(0);
    expect(stats.totalOutstanding).toBe(stats.totalBilled - stats.totalCollected);
    expect(stats.collectionRate).toBeGreaterThanOrEqual(0);
    expect(stats.collectionRate).toBeLessThanOrEqual(100);
  });

  it('calculates student fee summary with outstanding balances', () => {
    const invoices = feeService.getAllInvoices();
    const studentId = invoices[0].studentId;

    const summary = feeService.getParentSummary(studentId);
    expect(summary).toBeDefined();
    expect(summary.totalYearFees).toBeGreaterThan(0);
    expect(summary.outstandingBalance).toBe(Math.max(0, summary.totalYearFees - summary.totalPaid));
  });

  it('records manual payment and updates invoice balance and status', () => {
    const invoices = feeService.getAllInvoices();
    const targetInvoice = invoices.find(inv => inv.totalAmount > inv.paidAmount) || invoices[0];
    const initialPaid = targetInvoice.paidAmount;
    const paymentAmount = 1000;

    const result = feeService.recordPayment(targetInvoice.id, {
      amount: paymentAmount,
      method: 'Net Banking',
      recordedBy: 'Admin Office',
      remarks: 'Fee payment via NEFT',
    });

    expect(result.success).toBe(true);
    expect(result.invoice).toBeDefined();
    expect(result.invoice?.paidAmount).toBe(initialPaid + paymentAmount);
    expect(result.payment?.receiptNo).toMatch(/^REC-/);

    // Verify invoice was updated in storage
    const updated = feeService.getInvoiceById(targetInvoice.id);
    expect(updated?.paidAmount).toBe(initialPaid + paymentAmount);
  });

  it('rejects manual payment exceeding the remaining balance', () => {
    const invoices = feeService.getAllInvoices();
    const target = invoices[0];
    const remaining = target.totalAmount - target.paidAmount;
    const excessiveAmount = remaining + 10000;

    const result = feeService.recordPayment(target.id, {
      amount: excessiveAmount,
      method: 'Cash',
      recordedBy: 'Admin Office',
    });

    expect(result.success).toBe(false);
    expect(result.error).toContain('cannot exceed remaining balance');
  });
});

describe('feeService.recordPayment server sync', () => {
  const SERVER_INV = '55555555-5555-4555-8555-555555555555';

  beforeEach(() => {
    localStorage.clear();
    ERPStorage.reset();
  });

  afterEach(() => {
    httpClient.setToken(null);
    vi.restoreAllMocks();
  });

  it('keeps demo/local invoices local-only (no server call)', async () => {
    const postSpy = vi.spyOn(httpClient, 'post');
    const target = feeService.getAllInvoices().find(i => i.totalAmount - i.paidAmount >= 100)!;
    const res = feeService.recordPayment(target.id, { amount: 100, method: 'Cash', recordedBy: 'Office' });
    expect(res.success).toBe(true);
    await res.synced;
    expect(postSpy).not.toHaveBeenCalled();
  });

  it('rolls back a server invoice payment when the server rejects it', async () => {
    httpClient.setToken('session-token');
    const store = ERPStorage.get();
    const base = store.feeInvoices.find(i => i.totalAmount - i.paidAmount >= 100)!;
    store.feeInvoices.push({ ...base, id: SERVER_INV, payments: [...base.payments] });
    ERPStorage.set(store);
    const before = feeService.getInvoiceById(SERVER_INV)!;
    const paidBefore = before.paidAmount;
    const statusBefore = before.status;
    const txBefore = (ERPStorage.get().paymentTransactions || []).length;

    const postSpy = vi.spyOn(httpClient, 'post').mockResolvedValue({
      success: false,
      data: null as any,
      error: { code: 'HTTP_500', message: 'Ledger locked' },
    });
    const res = feeService.recordPayment(SERVER_INV, { amount: 100, method: 'Cash', recordedBy: 'Office' });
    expect(postSpy).toHaveBeenCalledWith(`/finance/invoices/${SERVER_INV}/pay`, expect.objectContaining({ amount: 100 }));
    const synced = await res.synced!;

    expect(synced).toEqual({ success: false, error: 'Ledger locked' });
    const after = feeService.getInvoiceById(SERVER_INV)!;
    expect(after.paidAmount).toBe(paidBefore);
    expect(after.status).toBe(statusBefore);
    expect((ERPStorage.get().paymentTransactions || []).length).toBe(txBefore);
  });
});
