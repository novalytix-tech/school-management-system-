// ==========================================
// feePortalService — typed client for the server fee-payment API
// (docs/PAYMENTS_API.md). The server is the single source of truth for
// invoices, balances, payments, refunds and receipts: nothing here ever
// decides on the client that money was paid.
// Developed by NTS Novalytix
// ==========================================

import { httpClient, ApiResponse, BlobResponse } from './api/httpClient';

// ---------- Types (mirror the contract) ----------

export type PaymentMode = 'live' | 'mock' | 'disabled';

export interface PaymentConfig {
  mode: PaymentMode;
  is_test: boolean;
  key_id: string | null;
  currency: string;
  school_name?: string;
  merchant_name?: string;
}

export type AdjustmentKind = 'DISCOUNT' | 'SCHOLARSHIP' | 'WAIVER' | 'LATE_FINE';
export type InstallmentStatus = 'PENDING' | 'PARTIALLY_PAID' | 'PAID' | 'OVERDUE';
export type OrderStatus = 'CREATED' | 'PENDING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'EXPIRED' | 'NEEDS_REVIEW';
export type PaymentStatus = 'SUCCESS' | 'PARTIALLY_REFUNDED' | 'REFUNDED';
export type RefundStatus = 'PENDING' | 'PROCESSED' | 'FAILED';

export interface PortalStudent {
  id: string;
  name: string;
  admission_number?: string | null;
  class_name?: string | null;
  section_name?: string | null;
  guardian_name?: string | null;
  academic_session?: string | null;
}

export interface FeeSummary {
  total_assigned: number;
  discounts: number;
  scholarships: number;
  fines: number;
  paid: number;
  refunded: number;
  pending: number;
  overdue: number;
  remaining_balance: number;
}

export interface InvoiceItem {
  fee_head: string;
  amount: number;
}

export interface InvoiceAdjustment {
  id: string;
  kind: AdjustmentKind;
  label: string;
  amount: number;
  created_at: string;
}

export interface Installment {
  id: string;
  installment_number: number;
  label: string;
  amount: number;
  paid_amount: number;
  due_date: string;
  status: InstallmentStatus;
}

export interface PortalInvoice {
  id: string;
  invoice_number: string;
  title: string;
  status: string;
  issue_date: string;
  due_date: string;
  is_overdue: boolean;
  subtotal: number;
  discount_amount: number;
  fine_amount: number;
  total_amount: number;
  paid_amount: number;
  refunded_amount: number;
  balance_amount: number;
  items: InvoiceItem[];
  adjustments: InvoiceAdjustment[];
  installments: Installment[];
  next_payable_amount: number;
}

export interface TimelineRefund {
  id: string;
  amount: number;
  status: RefundStatus;
  created_at: string;
  processed_at: string | null;
}

export interface TimelineEntry {
  order_id: string;
  invoice_id: string;
  invoice_number: string;
  amount: number;
  status: OrderStatus;
  provider: 'RAZORPAY' | 'MOCK' | string;
  is_test: boolean;
  gateway_order_id: string | null;
  gateway_payment_id: string | null;
  failure_reason: string | null;
  initiated_at: string | null;
  pending_at: string | null;
  succeeded_at: string | null;
  failed_at: string | null;
  cancelled_at: string | null;
  receipt_id: string | null;
  refunds: TimelineRefund[];
}

export interface PortalPayment {
  id: string;
  payment_number: string;
  invoice_number: string;
  amount: number;
  refunded_amount: number;
  method: string;
  status: PaymentStatus | string;
  is_test: boolean;
  paid_at: string;
  receipt_id: string | null;
  receipt_number: string | null;
}

export interface StudentFees {
  student: PortalStudent;
  summary: FeeSummary;
  invoices: PortalInvoice[];
  timeline: TimelineEntry[];
  payments: PortalPayment[];
}

export interface MyFeesResponse {
  students: StudentFees[];
}

export interface CreateOrderInput {
  invoiceId: string;
  /** Rupees. Omit to pay the invoice's next_payable_amount. */
  amount?: number;
  idempotencyKey: string;
}

export interface CheckoutOrder {
  order_id: string;
  gateway_order_id: string;
  amount_paise: number;
  currency: string;
  key_id: string | null;
  mode: PaymentMode;
  is_test: boolean;
  invoice_number: string;
  student_name: string;
  prefill?: { name?: string; email?: string; contact?: string };
}

export interface VerifyInput {
  order_id: string;
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
}

/** Result of verify / mock-complete / getOrder: the order's server status. */
export interface SettlementResult {
  status: OrderStatus;
  order_id?: string;
  receipt_id?: string | null;
  receipt_number?: string | null;
  payment_id?: string | null;
  failure_reason?: string | null;
  invoice?: Partial<PortalInvoice>;
}

export interface ReceiptSummary {
  id: string;
  receipt_number: string;
  issued_at: string;
  amount?: number;
  is_test?: boolean;
  payment_number?: string;
  invoice_number?: string;
  student_name?: string;
}

export interface FeeStructureOption {
  id: string;
  name: string;
  class_name?: string | null;
  academic_year_name?: string | null;
  total_fee?: number;
  due_date?: string;
}

export interface CreateInvoiceInput {
  student_id: string;
  fee_structure_id: string;
  due_date?: string;
  installments?: { label: string; amount: number; due_date: string }[];
  installment_count?: number;
  adjustments?: { kind: AdjustmentKind; label: string; amount: number }[];
}

export interface AdminTransaction {
  id: string;
  /** 'payment' (settled) or 'order' (checkout attempt) when the server distinguishes them. */
  kind?: 'payment' | 'order' | string;
  payment_number?: string | null;
  student_id?: string | null;
  student_name?: string | null;
  invoice_number?: string | null;
  amount: number;
  refunded_amount?: number;
  refundable_amount?: number;
  method?: string | null;
  provider?: string | null;
  status: string;
  is_test?: boolean;
  paid_at?: string | null;
  created_at?: string | null;
  receipt_id?: string | null;
  receipt_number?: string | null;
  failure_reason?: string | null;
  refunds?: TimelineRefund[];
}

export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  pages: number;
  limit?: number;
}

export interface TransactionFilters {
  student_id?: string;
  status?: string;
  from?: string;
  to?: string;
  page?: number;
}

export interface OfflinePaymentInput {
  amount: number;
  /** CASH | CHEQUE | BANK_TRANSFER … (existing endpoint field). */
  payment_method: string;
  transaction_reference?: string;
}

// ---------- Helpers ----------

const enc = encodeURIComponent;

function qs(params: Record<string, string | number | undefined | null>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${enc(k)}=${enc(String(v))}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

/** Accept either a bare array or a paginated `{items,…}` envelope. */
function toPaged<T>(data: any, page = 1): Paged<T> {
  if (Array.isArray(data)) return { items: data, total: data.length, page, pages: 1 };
  const items: T[] = Array.isArray(data?.items) ? data.items : [];
  return {
    items,
    total: typeof data?.total === 'number' ? data.total : items.length,
    page: typeof data?.page === 'number' ? data.page : page,
    pages: typeof data?.pages === 'number' ? data.pages : 1,
    limit: typeof data?.limit === 'number' ? data.limit : undefined,
  };
}

function mapData<T, U>(res: ApiResponse<T>, fn: (d: T) => U): ApiResponse<U> {
  return res.success ? { ...res, data: fn(res.data) } : (res as unknown as ApiResponse<U>);
}

/** A fresh idempotency key for one checkout attempt. */
export function newIdempotencyKey(): string {
  const c: Crypto | undefined = (globalThis as any).crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

/** Whether the app has a backend session (server portal) vs demo/offline mode. */
export function hasBackendSession(): boolean {
  return !!httpClient.getToken();
}

// ---------- Client ----------

export const feePortalService = {
  getConfig(): Promise<ApiResponse<PaymentConfig>> {
    return httpClient.get<PaymentConfig>('/payments/config');
  },

  getMyFees(studentId?: string): Promise<ApiResponse<MyFeesResponse>> {
    return httpClient.get<MyFeesResponse>(`/finance/my-fees${qs({ student_id: studentId })}`);
  },

  createOrder({ invoiceId, amount, idempotencyKey }: CreateOrderInput): Promise<ApiResponse<CheckoutOrder>> {
    return httpClient.post<CheckoutOrder>('/payments/razorpay/order', {
      invoice_id: invoiceId,
      ...(amount !== undefined ? { amount: Math.round(amount * 100) / 100 } : {}),
      idempotency_key: idempotencyKey,
    });
  },

  verify(input: VerifyInput): Promise<ApiResponse<SettlementResult>> {
    return httpClient.post<SettlementResult>('/payments/razorpay/verify', input);
  },

  cancelOrder(orderId: string): Promise<ApiResponse<SettlementResult>> {
    return httpClient.post<SettlementResult>(`/payments/orders/${enc(orderId)}/cancel`, {});
  },

  failOrder(orderId: string, reason: string): Promise<ApiResponse<SettlementResult>> {
    return httpClient.post<SettlementResult>(`/payments/orders/${enc(orderId)}/fail`, { reason });
  },

  getOrder(orderId: string): Promise<ApiResponse<SettlementResult>> {
    return httpClient.get<SettlementResult>(`/payments/orders/${enc(orderId)}`);
  },

  mockComplete(orderId: string, outcome: 'success' | 'failure' | 'cancel'): Promise<ApiResponse<SettlementResult>> {
    return httpClient.post<SettlementResult>(`/payments/mock/orders/${enc(orderId)}/complete`, { outcome });
  },

  async listReceipts(studentId?: string): Promise<ApiResponse<ReceiptSummary[]>> {
    const res = await httpClient.get<any>(`/finance/receipts${qs({ student_id: studentId })}`);
    return mapData(res, d => toPaged<ReceiptSummary>(d).items);
  },

  getReceiptPdf(receiptId: string): Promise<BlobResponse> {
    return httpClient.getBlob(`/finance/receipts/${enc(receiptId)}/pdf`);
  },

  // ----- Administration -----

  async listFeeStructures(): Promise<ApiResponse<FeeStructureOption[]>> {
    const res = await httpClient.get<any>('/finance/fee-structures');
    return mapData(res, d => toPaged<FeeStructureOption>(d).items);
  },

  createInvoice(input: CreateInvoiceInput): Promise<ApiResponse<PortalInvoice>> {
    return httpClient.post<PortalInvoice>('/finance/invoices', input);
  },

  generateInvoices(
    feeStructureId: string,
    opts: { section_id?: string; installment_count?: number } = {}
  ): Promise<ApiResponse<{ created: number; skipped: number }>> {
    const body: Record<string, unknown> = {};
    if (opts.section_id) body.section_id = opts.section_id;
    if (opts.installment_count) body.installment_count = opts.installment_count;
    return httpClient.post(`/finance/fee-structures/${enc(feeStructureId)}/generate-invoices`, body);
  },

  addAdjustment(
    invoiceId: string,
    adj: { kind: AdjustmentKind; label: string; amount: number }
  ): Promise<ApiResponse<PortalInvoice>> {
    return httpClient.post<PortalInvoice>(`/finance/invoices/${enc(invoiceId)}/adjustments`, adj);
  },

  removeAdjustment(invoiceId: string, adjustmentId: string): Promise<ApiResponse<PortalInvoice>> {
    return httpClient.delete<PortalInvoice>(`/finance/invoices/${enc(invoiceId)}/adjustments/${enc(adjustmentId)}`);
  },

  async listPayments(filters: TransactionFilters = {}): Promise<ApiResponse<Paged<AdminTransaction>>> {
    const page = filters.page || 1;
    const res = await httpClient.get<any>(
      `/finance/payments${qs({ student_id: filters.student_id, status: filters.status, from: filters.from, to: filters.to, page })}`
    );
    return mapData(res, d => toPaged<AdminTransaction>(d, page));
  },

  refund(paymentId: string, amount: number, reason: string): Promise<ApiResponse<any>> {
    return httpClient.post(`/finance/payments/${enc(paymentId)}/refund`, {
      amount: Math.round(amount * 100) / 100,
      reason,
    });
  },

  recordOfflinePayment(invoiceId: string, input: OfflinePaymentInput): Promise<ApiResponse<any>> {
    return httpClient.post(`/finance/invoices/${enc(invoiceId)}/pay`, input);
  },
};

/** Amount a payment can still be refunded (server field preferred). */
export function refundableAmount(tx: Pick<AdminTransaction, 'amount' | 'refunded_amount' | 'refundable_amount'>): number {
  if (typeof tx.refundable_amount === 'number') return Math.max(0, tx.refundable_amount);
  return Math.max(0, Math.round(((tx.amount || 0) - (tx.refunded_amount || 0)) * 100) / 100);
}

/** A settled payment that still has money left to refund. */
export const isRefundable = (tx: AdminTransaction): boolean =>
  tx.kind !== 'order' && ['SUCCESS', 'PARTIALLY_REFUNDED'].includes(String(tx.status).toUpperCase()) && refundableAmount(tx) > 0;
