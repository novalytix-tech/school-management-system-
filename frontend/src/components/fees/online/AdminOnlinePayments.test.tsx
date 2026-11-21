import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { AdminOnlinePayments, RefundDialog } from './AdminOnlinePayments';
import { feePortalService, AdminTransaction } from '../../../services/feePortalService';

const TX: AdminTransaction = {
  id: 'pay-1',
  kind: 'payment',
  payment_number: 'PAY-0001',
  student_name: 'Asha Verma',
  invoice_number: 'INV-2026-0007',
  amount: 5000,
  refunded_amount: 1000,
  method: 'RAZORPAY',
  status: 'PARTIALLY_REFUNDED',
  paid_at: '2026-04-10T05:02:00Z',
};

describe('RefundDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('blocks an over-refund and a missing reason', async () => {
    const refund = vi.spyOn(feePortalService, 'refund');
    render(<RefundDialog tx={TX} onClose={vi.fn()} onRefunded={vi.fn()} />);
    expect(screen.getByLabelText(/Refund amount/)).toHaveValue(4000);

    fireEvent.change(screen.getByLabelText(/Refund amount/), { target: { value: '4000.01' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Duplicate payment' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review refund' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('cannot exceed the refundable amount of ₹ 4,000.00');

    fireEvent.change(screen.getByLabelText(/Refund amount/), { target: { value: '100' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: '  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review refund' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('reason is required');
    expect(screen.queryByText('Confirm refund')).not.toBeInTheDocument();
    expect(refund).not.toHaveBeenCalled();
  });

  it('a valid refund is confirmed, then sent once', async () => {
    const refund = vi.spyOn(feePortalService, 'refund').mockResolvedValue({ success: true, data: { status: 'PROCESSED' } });
    const onRefunded = vi.fn();
    render(<RefundDialog tx={TX} onClose={vi.fn()} onRefunded={onRefunded} />);
    fireEvent.change(screen.getByLabelText(/Refund amount/), { target: { value: '1500' } });
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Fee waived' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review refund' }));
    expect(screen.getByText('Confirm refund')).toBeInTheDocument();
    const btn = screen.getByRole('button', { name: 'Refund' });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(onRefunded).toHaveBeenCalled());
    expect(refund).toHaveBeenCalledTimes(1);
    expect(refund).toHaveBeenCalledWith('pay-1', 1500, 'Fee waived');
  });
});

describe('AdminOnlinePayments', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('lists transactions and offers refunds only for refundable payments', async () => {
    const list = vi.spyOn(feePortalService, 'listPayments').mockResolvedValue({
      success: true,
      data: {
        items: [
          TX,
          { id: 'pay-2', payment_number: 'PAY-0002', amount: 300, refunded_amount: 300, status: 'REFUNDED' },
          { id: 'ord-9', kind: 'order', amount: 200, status: 'FAILED', failure_reason: 'Declined' },
        ],
        total: 3,
        page: 1,
        pages: 1,
        limit: 20,
      },
    });
    render(<AdminOnlinePayments />);
    expect(await screen.findByText('PAY-0002')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refund PAY-0001' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refund PAY-0002' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refund ord-9' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'SUCCESS' } });
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-04-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'SUCCESS', from: '2026-04-01', page: 1 })));
  });

  it('generates invoices for a chosen fee structure', async () => {
    vi.spyOn(feePortalService, 'listPayments').mockResolvedValue({ success: true, data: { items: [], total: 0, page: 1, pages: 1 } });
    vi.spyOn(feePortalService, 'listFeeStructures').mockResolvedValue({
      success: true,
      data: [{ id: 'fs-1', name: 'Class 5 annual', class_name: '5', total_fee: 50000 }],
    });
    const gen = vi.spyOn(feePortalService, 'generateInvoices').mockResolvedValue({ success: true, data: { created: 30, skipped: 2 } });
    render(<AdminOnlinePayments />);
    fireEvent.click(screen.getByRole('tab', { name: 'Generate invoices' }));
    await screen.findByRole('option', { name: /Class 5 annual/ });
    fireEvent.change(screen.getByLabelText('Fee structure'), { target: { value: 'fs-1' } });
    fireEvent.change(screen.getByLabelText('Installments (optional)'), { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate invoices/ }));
    expect(await screen.findByText('30 invoice(s) created, 2 skipped.')).toBeInTheDocument();
    expect(gen).toHaveBeenCalledWith('fs-1', { installment_count: 4 });
  });
});
