// ==========================================
// Server-backed fee portal for students and parents. Used whenever there is a
// backend session; the local demo portal (ParentFeePortal) is used otherwise.
// Every figure shown here comes from the server (GET /finance/my-fees).
// Developed by NTS Novalytix
// ==========================================

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useERP } from '../../../context/ERPContext';
import {
  feePortalService,
  PaymentConfig,
  PortalInvoice,
  ReceiptSummary,
  StudentFees,
  TimelineEntry,
} from '../../../services/feePortalService';
import { realtimeService } from '../../../services/realtimeService';
import { OnlineCheckoutModal } from './OnlineCheckoutModal';
import { runReceiptAction, ReceiptAction } from './receiptActions';
import { badgeClass, dateOnly, dateTime, money, statusLabel } from './paymentFormat';
import { EmptyState } from '../../common/EmptyState';
import {
  AlertCircle,
  AlertTriangle,
  BadgePercent,
  CheckCircle2,
  Clock,
  CreditCard,
  Download,
  Eye,
  FileText,
  FlaskConical,
  Gavel,
  History,
  IndianRupee,
  Printer,
  Receipt,
  RefreshCw,
  Users,
  Wallet,
} from 'lucide-react';

/** Class names are stored as "Class 6"; avoid printing "Class Class 6". */
function classLabel(name: string): string {
  return /^class/i.test(name.trim()) ? name.trim() : `Class ${name.trim()}`;
}

type ToastFn = (type: 'success' | 'error' | 'info' | 'warning', title: string, message?: string) => void;
type PortalTab = 'invoices' | 'timeline' | 'history' | 'receipts';

export interface ServerFeePortalViewProps {
  /** Student to select first (parent's currently selected ward), if present. */
  preferredStudentId?: string;
  addToast?: ToastFn;
}

const todayIso = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

// ---------- Small presentational pieces ----------

const StatCard: React.FC<{ label: string; value: number; tone?: string; icon: React.ReactNode; iconClass: string; sub?: string }> = ({
  label,
  value,
  tone,
  icon,
  iconClass,
  sub,
}) => (
  <div className="stat-card">
    <div className="stat-content">
      <span className="stat-label">{label}</span>
      <span className="stat-value" style={tone ? { color: tone } : undefined}>
        {money(value)}
      </span>
      {sub && <span className="stat-subtext">{sub}</span>}
    </div>
    <div className={`stat-icon-wrapper ${iconClass}`} aria-hidden="true">
      {icon}
    </div>
  </div>
);

const ReceiptButtons: React.FC<{
  receiptId: string;
  receiptNumber?: string | null;
  busy: boolean;
  onAction: (id: string, action: ReceiptAction, num?: string | null) => void;
}> = ({ receiptId, receiptNumber, busy, onAction }) => (
  <div className="table-action-cell" style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
    <button
      type="button"
      className="btn btn-outline btn-sm"
      disabled={busy}
      onClick={() => onAction(receiptId, 'view', receiptNumber)}
      aria-label={`View receipt ${receiptNumber || ''}`.trim()}
    >
      <Eye size={13} /> View
    </button>
    <button
      type="button"
      className="btn btn-outline btn-sm"
      disabled={busy}
      onClick={() => onAction(receiptId, 'download', receiptNumber)}
      aria-label={`Download PDF of receipt ${receiptNumber || ''}`.trim()}
    >
      <Download size={13} /> Download PDF
    </button>
    <button
      type="button"
      className="btn btn-outline btn-sm"
      disabled={busy}
      onClick={() => onAction(receiptId, 'print', receiptNumber)}
      aria-label={`Print receipt ${receiptNumber || ''}`.trim()}
    >
      <Printer size={13} /> Print
    </button>
  </div>
);

function timelineSteps(t: TimelineEntry) {
  const steps: { key: string; label: string; status: string; at: string | null; note?: string }[] = [];
  steps.push({ key: 'init', label: 'Initiated', status: 'INITIATED', at: t.initiated_at });
  if (t.pending_at) steps.push({ key: 'pending', label: 'Pending', status: 'PENDING', at: t.pending_at });
  if (t.succeeded_at) steps.push({ key: 'ok', label: 'Successful', status: 'SUCCEEDED', at: t.succeeded_at });
  if (t.failed_at) steps.push({ key: 'fail', label: 'Failed', status: 'FAILED', at: t.failed_at, note: t.failure_reason || undefined });
  if (t.cancelled_at) steps.push({ key: 'cancel', label: 'Cancelled', status: 'CANCELLED', at: t.cancelled_at });
  if (t.status === 'EXPIRED' || t.status === 'NEEDS_REVIEW') {
    steps.push({ key: 'final', label: statusLabel(t.status), status: t.status, at: null });
  }
  (t.refunds || []).forEach(r => {
    steps.push({
      key: `refund-${r.id}`,
      label: `Refund ${money(r.amount)} — ${statusLabel(r.status)}`,
      status: r.status === 'PROCESSED' ? 'REFUNDED' : r.status,
      at: r.processed_at || r.created_at,
      note: r.processed_at ? `Requested ${dateTime(r.created_at)}` : undefined,
    });
  });
  return steps;
}

const PaymentTimeline: React.FC<{ entries: TimelineEntry[] }> = ({ entries }) => {
  const sorted = [...entries].sort((a, b) => String(b.initiated_at || '').localeCompare(String(a.initiated_at || '')));
  if (!sorted.length) {
    return <EmptyState icon={<History size={28} />} title="No payment attempts yet" description="Online payment attempts will appear here." />;
  }
  return (
    <ol aria-label="Payment timeline" style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 12 }}>
      {sorted.map(t => (
        <li key={t.order_id} className="card" data-testid="timeline-entry">
          <div className="card-body" style={{ padding: 14 }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
              <div>
                <strong>{t.invoice_number}</strong> · {money(t.amount)}
                <div style={{ fontSize: 12, color: '#64748B' }}>
                  {t.provider === 'MOCK' ? 'Test gateway' : 'Razorpay'}
                  {t.gateway_payment_id ? ` · ${t.gateway_payment_id}` : t.gateway_order_id ? ` · ${t.gateway_order_id}` : ''}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {t.is_test && <span className="badge badge-warning">TEST</span>}
                <span className={badgeClass(t.status)}>{statusLabel(t.status)}</span>
              </div>
            </div>
            <ul style={{ listStyle: 'none', padding: 0, margin: '10px 0 0', display: 'grid', gap: 6 }}>
              {timelineSteps(t).map(s => (
                <li key={s.key} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', fontSize: 13 }}>
                  <span className={badgeClass(s.status)}>{s.label}</span>
                  {s.at && <time dateTime={s.at}>{dateTime(s.at)}</time>}
                  {s.note && <span style={{ color: '#64748B' }}>{s.note}</span>}
                </li>
              ))}
            </ul>
          </div>
        </li>
      ))}
    </ol>
  );
};

const InvoiceCard: React.FC<{ inv: PortalInvoice; canPay: boolean; onPay: (inv: PortalInvoice) => void }> = ({ inv, canPay, onPay }) => {
  const today = todayIso();
  return (
    <section className="card" aria-labelledby={`inv-${inv.id}-title`} data-testid="invoice-card">
      <div className="card-header" style={{ flexWrap: 'wrap', gap: 8 }}>
        <div>
          <h3 className="card-title" id={`inv-${inv.id}-title`}>
            {inv.title || 'Fee invoice'}
          </h3>
          <p className="card-subtitle">
            {inv.invoice_number} · Due {dateOnly(inv.due_date)}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          {inv.is_overdue && <span className="badge badge-danger">Overdue</span>}
          <span className={badgeClass(inv.status)}>{statusLabel(inv.status)}</span>
        </div>
      </div>
      <div className="card-body">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 16 }}>
          <div>
            <h4 style={{ fontSize: 13, margin: '0 0 6px' }}>Fee heads</h4>
            <table className="data-table" style={{ fontSize: 13 }}>
              <tbody>
                {inv.items.map((it, i) => (
                  <tr key={`${it.fee_head}-${i}`}>
                    <td>{it.fee_head}</td>
                    <td style={{ textAlign: 'right' }}>{money(it.amount)}</td>
                  </tr>
                ))}
                <tr>
                  <td>Subtotal</td>
                  <td style={{ textAlign: 'right' }}>{money(inv.subtotal)}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div>
            <h4 style={{ fontSize: 13, margin: '0 0 6px' }}>Adjustments & totals</h4>
            <table className="data-table" style={{ fontSize: 13 }}>
              <tbody>
                {inv.adjustments.map(a => (
                  <tr key={a.id}>
                    <td>
                      <span className={`badge ${a.kind === 'LATE_FINE' ? 'badge-danger' : 'badge-success'}`}>{statusLabel(a.kind)}</span>{' '}
                      {a.label}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      {a.kind === 'LATE_FINE' ? '+' : '−'} {money(a.amount)}
                    </td>
                  </tr>
                ))}
                <tr>
                  <td>Total</td>
                  <td style={{ textAlign: 'right' }}>{money(inv.total_amount)}</td>
                </tr>
                <tr>
                  <td>Paid</td>
                  <td style={{ textAlign: 'right', color: '#16A34A' }}>{money(inv.paid_amount)}</td>
                </tr>
                {inv.refunded_amount > 0 && (
                  <tr>
                    <td>Refunded</td>
                    <td style={{ textAlign: 'right' }}>{money(inv.refunded_amount)}</td>
                  </tr>
                )}
                <tr>
                  <td>
                    <strong>Balance</strong>
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <strong style={{ color: inv.balance_amount > 0 ? '#DC2626' : '#16A34A' }}>{money(inv.balance_amount)}</strong>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        {inv.installments.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <h4 style={{ fontSize: 13, margin: '0 0 6px' }}>Installments</h4>
            <div className="table-responsive">
              <table className="data-table" aria-label={`Installments for ${inv.invoice_number}`}>
                <thead>
                  <tr>
                    <th scope="col">Installment</th>
                    <th scope="col">Due date</th>
                    <th scope="col">Amount</th>
                    <th scope="col">Paid</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {[...inv.installments]
                    .sort((a, b) => a.installment_number - b.installment_number)
                    .map(ins => {
                      const overdue = ins.status === 'OVERDUE' || (ins.status !== 'PAID' && !!ins.due_date && ins.due_date.slice(0, 10) < today);
                      return (
                        <tr
                          key={ins.id}
                          data-overdue={overdue || undefined}
                          style={overdue ? { background: '#FEF2F2' } : undefined}
                        >
                          <td>{ins.label}</td>
                          <td>{dateOnly(ins.due_date)}</td>
                          <td>{money(ins.amount)}</td>
                          <td>{money(ins.paid_amount)}</td>
                          <td>
                            <span className={badgeClass(overdue ? 'OVERDUE' : ins.status)}>
                              {overdue ? 'Overdue' : statusLabel(ins.status)}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {canPay && inv.balance_amount > 0 && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => onPay(inv)}
              aria-label={`Pay now for ${inv.invoice_number}`}
            >
              <CreditCard size={15} /> Pay now · {money(inv.next_payable_amount || inv.balance_amount)}
            </button>
          </div>
        )}
      </div>
    </section>
  );
};

// ---------- The portal ----------

export const ServerFeePortalView: React.FC<ServerFeePortalViewProps> = ({ preferredStudentId, addToast }) => {
  const [config, setConfig] = useState<PaymentConfig | null>(null);
  const [configError, setConfigError] = useState('');
  const [students, setStudents] = useState<StudentFees[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<PortalTab>('invoices');
  const [receipts, setReceipts] = useState<ReceiptSummary[]>([]);
  const [checkoutInvoice, setCheckoutInvoice] = useState<PortalInvoice | null>(null);
  const [receiptBusy, setReceiptBusy] = useState<string | null>(null);
  const toastRef = useRef(addToast);
  useEffect(() => {
    toastRef.current = addToast;
  }, [addToast]);

  const loadFees = useCallback(async () => {
    const res = await feePortalService.getMyFees();
    if (res.success && res.data) {
      setStudents(res.data.students || []);
      setLoadError('');
    } else {
      setLoadError(res.error?.message || 'Could not load fee details.');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    let alive = true;
    void feePortalService.getConfig().then(res => {
      if (!alive) return;
      if (res.success && res.data) setConfig(res.data);
      else setConfigError(res.error?.message || 'Could not load payment settings.');
    });
    void loadFees();
    return () => {
      alive = false;
    };
  }, [loadFees]);

  const current: StudentFees | null = useMemo(() => {
    if (!students.length) return null;
    return (
      students.find(s => s.student.id === selectedId) ||
      students.find(s => s.student.id === preferredStudentId) ||
      students[0]
    );
  }, [students, selectedId, preferredStudentId]);
  const currentId = current?.student.id;

  const loadReceipts = useCallback(async (studentId: string) => {
    const res = await feePortalService.listReceipts(studentId);
    if (res.success) setReceipts(res.data || []);
  }, []);

  useEffect(() => {
    if (currentId) void loadReceipts(currentId);
  }, [currentId, loadReceipts]);

  const refresh = useCallback(() => {
    void loadFees();
    if (currentId) void loadReceipts(currentId);
  }, [loadFees, loadReceipts, currentId]);

  // Live updates: the backend emits "fee_payment" after a settlement / refund.
  useEffect(() => {
    return realtimeService.onEntityChange(change => {
      if (change.entity === 'fee_payment') refresh();
    });
  }, [refresh]);

  const handleReceipt = useCallback(async (receiptId: string, action: ReceiptAction, num?: string | null) => {
    setReceiptBusy(receiptId);
    const err = await runReceiptAction(receiptId, action, num);
    setReceiptBusy(null);
    if (err) toastRef.current?.('error', 'Receipt unavailable', err);
  }, []);

  if (loading) {
    return (
      <div style={{ padding: 24 }} role="status">
        Loading fee details…
      </div>
    );
  }

  if (loadError && !students.length) {
    return (
      <div style={{ padding: '40px 20px' }}>
        <EmptyState icon={<AlertCircle size={32} />} title="Could not load fees" description={loadError} />
        <div style={{ textAlign: 'center', marginTop: 12 }}>
          <button type="button" className="btn btn-primary" onClick={() => void loadFees()}>
            <RefreshCw size={14} /> Retry
          </button>
        </div>
      </div>
    );
  }

  if (!current) {
    return (
      <div style={{ padding: '40px 20px' }}>
        <EmptyState
          icon={<Users size={32} />}
          title="No fee records"
          description="No student is linked to your account yet. Please contact the school office."
        />
      </div>
    );
  }

  const { student, summary, invoices, timeline, payments } = current;
  const mode = config?.mode;
  const canPay = mode === 'live' || mode === 'mock';
  const isTest = !!config?.is_test;

  return (
    <div className="parent-fee-portal-wrapper">
      <div className="page-header" style={{ marginBottom: 16 }}>
        <div className="page-title-group">
          <h1 style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            Fees & Payments
            {isTest && (
              <span className="badge badge-warning" data-testid="test-mode-badge" title="Payments are simulated or in sandbox">
                <FlaskConical size={12} /> TEST MODE
              </span>
            )}
          </h1>
          <p className="page-subtitle">
            {student.name}
            {student.admission_number ? ` · Adm. ${student.admission_number}` : ''}
            {student.class_name ? ` · ${classLabel(student.class_name)}${student.section_name ? `-${student.section_name}` : ''}` : ''}
            {student.academic_session ? ` · ${student.academic_session}` : ''}
          </p>
        </div>

        {students.length > 1 && (
          <div className="ward-selector-box" role="group" aria-label="Select ward">
            <span className="ward-selector-label">
              <Users size={14} /> Ward:
            </span>
            <div className="ward-buttons-group">
              {students.map(s => (
                <button
                  key={s.student.id}
                  type="button"
                  onClick={() => setSelectedId(s.student.id)}
                  className={`ward-pill-btn ${s.student.id === student.id ? 'active' : ''}`}
                  aria-pressed={s.student.id === student.id}
                >
                  <span className="ward-dot" />
                  <strong>{s.student.name}</strong>
                  {s.student.class_name && <span className="ward-class-badge">{classLabel(s.student.class_name)}</span>}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {mode === 'disabled' && (
        <div className="card" role="note" style={{ marginBottom: 16, borderColor: '#FCD34D', background: '#FFFBEB' }}>
          <div className="card-body" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <AlertTriangle size={18} color="#D97706" />
            <span>Online payment is not configured yet. Please pay at the school office.</span>
          </div>
        </div>
      )}
      {configError && !config && (
        <div className="card" role="alert" style={{ marginBottom: 16 }}>
          <div className="card-body">{configError} Online payment is unavailable right now.</div>
        </div>
      )}

      <div className="stat-grid" style={{ marginBottom: 22 }} data-testid="fee-summary">
        <StatCard label="Total assigned" value={summary.total_assigned} icon={<FileText size={22} />} iconClass="stat-icon-blue" />
        <StatCard label="Paid" value={summary.paid} tone="#16A34A" icon={<CheckCircle2 size={22} />} iconClass="stat-icon-green" />
        <StatCard label="Pending" value={summary.pending} icon={<Clock size={22} />} iconClass="stat-icon-amber" />
        <StatCard label="Overdue" value={summary.overdue} tone={summary.overdue > 0 ? '#DC2626' : undefined} icon={<AlertCircle size={22} />} iconClass="stat-icon-red" />
        <StatCard
          label="Discounts & scholarships"
          value={(summary.discounts || 0) + (summary.scholarships || 0)}
          icon={<BadgePercent size={22} />}
          iconClass="stat-icon-green"
        />
        <StatCard label="Late fines" value={summary.fines} icon={<Gavel size={22} />} iconClass="stat-icon-red" />
        <StatCard
          label="Remaining balance"
          value={summary.remaining_balance}
          tone={summary.remaining_balance > 0 ? '#DC2626' : '#16A34A'}
          icon={<IndianRupee size={22} />}
          iconClass="stat-icon-blue"
          sub={summary.refunded > 0 ? `Includes ${money(summary.refunded)} refunded` : undefined}
        />
      </div>

      <div className="fee-tabs-container" role="tablist" aria-label="Fee sections">
        {(
          [
            ['invoices', <FileText size={15} key="i" />, `Invoices (${invoices.length})`],
            ['timeline', <History size={15} key="t" />, `Payment timeline (${timeline.length})`],
            ['history', <Wallet size={15} key="h" />, `Payment history (${payments.length})`],
            ['receipts', <Receipt size={15} key="r" />, `Receipts (${receipts.length})`],
          ] as const
        ).map(([key, icon, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`fee-tab-pill ${tab === key ? 'active' : ''}`}
            onClick={() => setTab(key)}
          >
            {icon}
            <span>{label}</span>
          </button>
        ))}
      </div>

      {tab === 'invoices' && (
        <div role="tabpanel" style={{ display: 'grid', gap: 16 }}>
          {invoices.length === 0 ? (
            <EmptyState icon={<FileText size={28} />} title="No invoices" description="No fee invoices have been issued yet." />
          ) : (
            invoices.map(inv => <InvoiceCard key={inv.id} inv={inv} canPay={canPay} onPay={setCheckoutInvoice} />)
          )}
        </div>
      )}

      {tab === 'timeline' && (
        <div role="tabpanel">
          <PaymentTimeline entries={timeline} />
        </div>
      )}

      {tab === 'history' && (
        <div role="tabpanel" className="table-responsive">
          <table className="data-table" aria-label="Payment history">
            <thead>
              <tr>
                <th scope="col">Payment</th>
                <th scope="col">Invoice</th>
                <th scope="col">Amount</th>
                <th scope="col">Refunded</th>
                <th scope="col">Method</th>
                <th scope="col">Status</th>
                <th scope="col">Paid at</th>
                <th scope="col">Receipt</th>
              </tr>
            </thead>
            <tbody>
              {payments.length === 0 && (
                <tr>
                  <td colSpan={8} style={{ textAlign: 'center', color: '#64748B' }}>
                    No payments yet.
                  </td>
                </tr>
              )}
              {payments.map(p => (
                <tr key={p.id}>
                  <td className="monospace">
                    {p.payment_number} {p.is_test && <span className="badge badge-warning">TEST</span>}
                  </td>
                  <td>{p.invoice_number}</td>
                  <td>{money(p.amount)}</td>
                  <td>{p.refunded_amount > 0 ? money(p.refunded_amount) : '—'}</td>
                  <td>
                    <span className="pay-method-badge">{p.method}</span>
                  </td>
                  <td>
                    <span className={badgeClass(p.status)}>{statusLabel(p.status)}</span>
                  </td>
                  <td>{dateTime(p.paid_at)}</td>
                  <td>
                    {p.receipt_id ? (
                      <>
                        <div className="monospace" style={{ fontSize: 12 }}>
                          {p.receipt_number}
                        </div>
                        <ReceiptButtons receiptId={p.receipt_id} receiptNumber={p.receipt_number} busy={receiptBusy === p.receipt_id} onAction={handleReceipt} />
                      </>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'receipts' && (
        <div role="tabpanel" className="table-responsive">
          <table className="data-table" aria-label="Receipts">
            <thead>
              <tr>
                <th scope="col">Receipt</th>
                <th scope="col">Issued</th>
                <th scope="col">Amount</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {receipts.length === 0 && (
                <tr>
                  <td colSpan={4} style={{ textAlign: 'center', color: '#64748B' }}>
                    No receipts yet.
                  </td>
                </tr>
              )}
              {receipts.map(r => (
                <tr key={r.id}>
                  <td className="monospace">
                    {r.receipt_number} {r.is_test && <span className="badge badge-warning">TEST</span>}
                    {r.invoice_number && <div style={{ fontSize: 12, color: '#64748B' }}>{r.invoice_number}</div>}
                  </td>
                  <td>{dateTime(r.issued_at)}</td>
                  <td>{r.amount !== undefined ? money(r.amount) : '—'}</td>
                  <td>
                    <ReceiptButtons receiptId={r.id} receiptNumber={r.receipt_number} busy={receiptBusy === r.id} onAction={handleReceipt} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {checkoutInvoice && config && canPay && (
        <OnlineCheckoutModal
          invoice={checkoutInvoice}
          config={config}
          studentName={student.name}
          onClose={() => setCheckoutInvoice(null)}
          onSettled={refresh}
          onReceiptAction={(id, action, num) => void handleReceipt(id, action, num)}
        />
      )}
    </div>
  );
};

/** Context-bound wrapper used by FeesPage. */
export const ServerFeePortal: React.FC = () => {
  const { selectedChild, addToast } = useERP();
  return <ServerFeePortalView preferredStudentId={selectedChild?.id} addToast={addToast} />;
};
