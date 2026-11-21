import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { ServerFeePortalView } from './ServerFeePortal';
import { feePortalService, MyFeesResponse, PaymentConfig } from '../../../services/feePortalService';

const SAMPLE: MyFeesResponse = {
  students: [
    {
      student: {
        id: 'stu-1',
        name: 'Asha Verma',
        admission_number: 'ADM-001',
        class_name: '5',
        section_name: 'A',
        guardian_name: 'R. Verma',
        academic_session: '2026-27',
      },
      summary: {
        total_assigned: 125000,
        discounts: 5000,
        scholarships: 2500,
        fines: 300,
        paid: 40000,
        refunded: 0,
        pending: 77800,
        overdue: 20000,
        remaining_balance: 77800,
      },
      invoices: [
        {
          id: 'inv-uuid-1',
          invoice_number: 'INV-2026-0007',
          title: 'Annual fees 2026-27',
          status: 'PARTIALLY_PAID',
          issue_date: '2026-04-01',
          due_date: '2026-04-30',
          is_overdue: true,
          subtotal: 125000,
          discount_amount: 7500,
          fine_amount: 300,
          total_amount: 117800,
          paid_amount: 40000,
          refunded_amount: 0,
          balance_amount: 77800,
          items: [
            { fee_head: 'Tuition Fee', amount: 100000 },
            { fee_head: 'Transport', amount: 25000 },
          ],
          adjustments: [
            { id: 'a1', kind: 'SCHOLARSHIP', label: 'Merit scholarship', amount: 2500, created_at: '2026-04-02T10:00:00Z' },
            { id: 'a2', kind: 'LATE_FINE', label: 'Late fee April', amount: 300, created_at: '2026-05-02T10:00:00Z' },
          ],
          installments: [
            { id: 'i1', installment_number: 1, label: 'Q1', amount: 40000, paid_amount: 40000, due_date: '2026-04-30', status: 'PAID' },
            { id: 'i2', installment_number: 2, label: 'Q2', amount: 20000, paid_amount: 0, due_date: '2026-07-31', status: 'OVERDUE' },
            { id: 'i3', installment_number: 3, label: 'Q3', amount: 57800, paid_amount: 0, due_date: '2099-10-31', status: 'PENDING' },
          ],
          next_payable_amount: 20000,
        },
      ],
      timeline: [
        {
          order_id: 'ord-1',
          invoice_id: 'inv-uuid-1',
          invoice_number: 'INV-2026-0007',
          amount: 40000,
          status: 'SUCCEEDED',
          provider: 'RAZORPAY',
          is_test: false,
          gateway_order_id: 'order_A',
          gateway_payment_id: 'pay_A',
          failure_reason: null,
          initiated_at: '2026-04-10T05:00:00Z',
          pending_at: '2026-04-10T05:01:00Z',
          succeeded_at: '2026-04-10T05:02:00Z',
          failed_at: null,
          cancelled_at: null,
          receipt_id: 'rc-1',
          refunds: [],
        },
        {
          order_id: 'ord-2',
          invoice_id: 'inv-uuid-1',
          invoice_number: 'INV-2026-0007',
          amount: 20000,
          status: 'FAILED',
          provider: 'RAZORPAY',
          is_test: false,
          gateway_order_id: 'order_B',
          gateway_payment_id: null,
          failure_reason: 'Card declined by bank',
          initiated_at: '2026-05-01T05:00:00Z',
          pending_at: null,
          succeeded_at: null,
          failed_at: '2026-05-01T05:03:00Z',
          cancelled_at: null,
          receipt_id: null,
          refunds: [],
        },
      ],
      payments: [
        {
          id: 'p1',
          payment_number: 'PAY-0001',
          invoice_number: 'INV-2026-0007',
          amount: 40000,
          refunded_amount: 0,
          method: 'RAZORPAY',
          status: 'SUCCESS',
          is_test: false,
          paid_at: '2026-04-10T05:02:00Z',
          receipt_id: 'rc-1',
          receipt_number: 'RCT-2026-000001',
        },
      ],
    },
  ],
};

const cfg = (mode: PaymentConfig['mode'], is_test = false): PaymentConfig => ({ mode, is_test, key_id: null, currency: 'INR' });

function mockApi(config: PaymentConfig) {
  vi.spyOn(feePortalService, 'getConfig').mockResolvedValue({ success: true, data: config });
  vi.spyOn(feePortalService, 'getMyFees').mockResolvedValue({ success: true, data: SAMPLE });
  vi.spyOn(feePortalService, 'listReceipts').mockResolvedValue({
    success: true,
    data: [{ id: 'rc-1', receipt_number: 'RCT-2026-000001', issued_at: '2026-04-10T05:02:00Z', amount: 40000 }],
  });
}

describe('ServerFeePortalView', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the summary, invoice breakdown and installments from my-fees', async () => {
    mockApi(cfg('live'));
    render(<ServerFeePortalView />);
    const summary = await screen.findByTestId('fee-summary');
    expect(within(summary).getByText('Total assigned')).toBeInTheDocument();
    expect(within(summary).getByText('₹ 1,25,000.00')).toBeInTheDocument();
    expect(within(summary).getByText('Discounts & scholarships')).toBeInTheDocument();
    expect(within(summary).getByText('₹ 7,500.00')).toBeInTheDocument();
    expect(within(summary).getByText('Late fines')).toBeInTheDocument();
    expect(within(summary).getAllByText('₹ 77,800.00').length).toBeGreaterThan(0);

    expect(screen.getByText('Tuition Fee')).toBeInTheDocument();
    expect(screen.getByText('Merit scholarship')).toBeInTheDocument();
    const inst = screen.getByRole('table', { name: 'Installments for INV-2026-0007' });
    const rows = within(inst).getAllByRole('row');
    expect(rows).toHaveLength(4); // header + 3
    expect(rows[2]).toHaveAttribute('data-overdue', 'true');
    expect(rows[3]).not.toHaveAttribute('data-overdue');
    expect(screen.getByRole('button', { name: 'Pay now for INV-2026-0007' })).toHaveTextContent('₹ 20,000.00');
    expect(screen.queryByTestId('test-mode-badge')).not.toBeInTheDocument();
  });

  it('renders the payment timeline with statuses and failure reasons', async () => {
    mockApi(cfg('live'));
    render(<ServerFeePortalView />);
    fireEvent.click(await screen.findByRole('tab', { name: /Payment timeline/ }));
    const entries = screen.getAllByTestId('timeline-entry');
    expect(entries).toHaveLength(2);
    // Newest first: the failed attempt.
    expect(within(entries[0]).getByText('Card declined by bank')).toBeInTheDocument();
    expect(within(entries[0]).getAllByText('Failed').length).toBeGreaterThan(0);
    expect(within(entries[1]).getByText('Successful')).toBeInTheDocument();
    expect(within(entries[1]).getByText('Initiated')).toBeInTheDocument();
    expect(entries[1].querySelectorAll('time')).toHaveLength(3);

    fireEvent.click(screen.getByRole('tab', { name: /Receipts/ }));
    expect(screen.getByRole('button', { name: 'Download PDF of receipt RCT-2026-000001' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Print receipt RCT-2026-000001' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'View receipt RCT-2026-000001' })).toBeInTheDocument();
  });

  it('disabled mode hides pay buttons and points to the school office', async () => {
    mockApi(cfg('disabled'));
    render(<ServerFeePortalView />);
    expect(await screen.findByText(/Online payment is not configured yet/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Pay now/ })).not.toBeInTheDocument();
  });

  it('shows a persistent TEST MODE badge when config.is_test', async () => {
    mockApi(cfg('mock', true));
    render(<ServerFeePortalView />);
    expect(await screen.findByTestId('test-mode-badge')).toHaveTextContent('TEST MODE');
  });
});
