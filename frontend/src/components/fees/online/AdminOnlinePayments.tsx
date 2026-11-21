// ==========================================
// Admin: online payments & refunds (server-backed). Transaction history,
// refunds, invoice generation from fee structures and invoice adjustments.
// Only rendered when there is a backend session.
// Developed by NTS Novalytix
// ==========================================

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  feePortalService,
  refundableAmount,
  isRefundable,
  AdjustmentKind,
  AdminTransaction,
  FeeStructureOption,
  Paged,
  PortalInvoice,
  TransactionFilters,
} from '../../../services/feePortalService';
import { studentService } from '../../../services/studentService';
import { isServerId } from '../../../utils/ids';
import { Modal } from '../../common/Modal';
import { ConfirmDialog } from '../../common/ConfirmDialog';
import { Pagination } from '../../common/Pagination';
import { badgeClass, dateTime, money, statusLabel } from './paymentFormat';
import { runReceiptAction } from './receiptActions';
import { Download, FilePlus2, RefreshCw, RotateCcw, Tag, Trash2 } from 'lucide-react';

type ToastFn = (type: 'success' | 'error' | 'info' | 'warning', title: string, message?: string) => void;
type Section = 'transactions' | 'generate' | 'adjustments';

const STATUS_OPTIONS = ['SUCCESS', 'PARTIALLY_REFUNDED', 'REFUNDED', 'PENDING', 'FAILED', 'CANCELLED', 'NEEDS_REVIEW'];
const ADJ_KINDS: { value: AdjustmentKind; label: string }[] = [
  { value: 'DISCOUNT', label: 'Discount' },
  { value: 'SCHOLARSHIP', label: 'Scholarship' },
  { value: 'WAIVER', label: 'Waiver' },
  { value: 'LATE_FINE', label: 'Late fine' },
];

// ---------- Student picker (server students from the synced roster) ----------

const StudentPicker: React.FC<{ id: string; value: string; onChange: (v: string) => void; allowAll?: boolean }> = ({
  id,
  value,
  onChange,
  allowAll,
}) => {
  const students = useMemo(() => {
    try {
      return studentService
        .getAll()
        .filter(s => isServerId(s.id))
        .sort((a, b) => a.fullName.localeCompare(b.fullName));
    } catch {
      return [];
    }
  }, []);
  if (!students.length) {
    return (
      <input
        id={id}
        className="form-input"
        placeholder="Student ID (UUID)"
        value={value}
        onChange={e => onChange(e.target.value.trim())}
      />
    );
  }
  return (
    <select id={id} className="form-select" value={value} onChange={e => onChange(e.target.value)}>
      <option value="">{allowAll ? 'All students' : 'Select a student'}</option>
      {students.map(s => (
        <option key={s.id} value={s.id}>
          {s.fullName}
          {s.admissionNo ? ` (${s.admissionNo})` : ''}
          {s.className ? ` · ${s.className}${s.section ? `-${s.section}` : ''}` : ''}
        </option>
      ))}
    </select>
  );
};

// ---------- Refund ----------

export const RefundDialog: React.FC<{
  tx: AdminTransaction;
  onClose: () => void;
  onRefunded: () => void;
  addToast?: ToastFn;
}> = ({ tx, onClose, onRefunded, addToast }) => {
  const max = refundableAmount(tx);
  const [amount, setAmount] = useState(String(max));
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);

  const validate = (): number | null => {
    const n = Number(amount);
    if (!amount.trim() || !isFinite(n) || n <= 0) {
      setError('Enter a refund amount greater than zero.');
      return null;
    }
    if (Math.round(n * 100) > Math.round(max * 100)) {
      setError(`The refund cannot exceed the refundable amount of ${money(max)}.`);
      return null;
    }
    if (!reason.trim()) {
      setError('A reason is required for every refund.');
      return null;
    }
    setError('');
    return Math.round(n * 100) / 100;
  };

  const review = (e: React.FormEvent) => {
    e.preventDefault();
    if (validate() !== null) setConfirming(true);
  };

  const doRefund = async () => {
    const n = validate();
    if (n === null) return;
    const res = await feePortalService.refund(tx.id, n, reason.trim());
    if (!res.success) {
      setConfirming(false);
      setError(res.error?.message || 'The refund could not be processed.');
      return;
    }
    addToast?.('success', 'Refund initiated', `${money(n)} refund recorded for ${tx.payment_number || 'the payment'}.`);
    onRefunded();
  };

  if (confirming) {
    return (
      <ConfirmDialog
        isOpen
        onClose={() => setConfirming(false)}
        onConfirm={doRefund}
        title="Confirm refund"
        message={`Refund ${money(Number(amount))} for payment ${tx.payment_number || tx.id}${tx.student_name ? ` (${tx.student_name})` : ''}? Online payments are refunded to the original payment method; this cannot be undone.`}
        confirmLabel="Refund"
        pendingLabel="Refunding…"
        isDestructive
      />
    );
  }

  return (
    <Modal
      isOpen
      onClose={onClose}
      title={`Refund ${tx.payment_number || 'payment'}`}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form="refund-form" className="btn btn-danger">
            Review refund
          </button>
        </>
      }
    >
      <form id="refund-form" onSubmit={review} noValidate>
        <p style={{ marginTop: 0, fontSize: 14 }}>
          Paid {money(tx.amount)}
          {tx.refunded_amount ? ` · already refunded ${money(tx.refunded_amount)}` : ''} · refundable <strong>{money(max)}</strong>
        </p>
        <div className="form-group">
          <label className="form-label required" htmlFor="refund-amount">
            Refund amount (₹)
          </label>
          <input
            id="refund-amount"
            className="form-input"
            type="number"
            inputMode="decimal"
            min={0.01}
            max={max}
            step="0.01"
            value={amount}
            // Pre-filled with the refundable amount: select it on focus so
            // typing replaces it instead of appending (e.g. "3333.331000").
            onFocus={e => e.currentTarget.select()}
            onChange={e => setAmount(e.target.value)}
          />
        </div>
        <div className="form-group">
          <label className="form-label required" htmlFor="refund-reason">
            Reason
          </label>
          <textarea id="refund-reason" className="form-input" rows={3} value={reason} onChange={e => setReason(e.target.value)} />
        </div>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
};

// ---------- Transactions ----------

const TransactionsSection: React.FC<{ addToast?: ToastFn }> = ({ addToast }) => {
  const [draft, setDraft] = useState<TransactionFilters>({});
  const [filters, setFilters] = useState<TransactionFilters>({ page: 1 });
  const [data, setData] = useState<Paged<AdminTransaction> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [refundTarget, setRefundTarget] = useState<AdminTransaction | null>(null);

  const load = useCallback(async (f: TransactionFilters) => {
    setLoading(true);
    const res = await feePortalService.listPayments(f);
    setLoading(false);
    if (res.success) {
      setData(res.data);
      setError('');
    } else {
      setError(res.error?.message || 'Could not load transactions.');
    }
  }, []);

  useEffect(() => {
    void load(filters);
  }, [filters, load]);

  const pageSize = data?.limit || Math.max(data?.items.length || 0, 1);

  const receipt = async (id: string, num?: string | null) => {
    const err = await runReceiptAction(id, 'download', num);
    if (err) addToast?.('error', 'Receipt unavailable', err);
  };

  return (
    <div>
      <form
        className="filter-toolbar"
        style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'flex-end' }}
        onSubmit={e => {
          e.preventDefault();
          setFilters({ ...draft, page: 1 });
        }}
        aria-label="Transaction filters"
      >
        <div className="form-group" style={{ minWidth: 200, marginBottom: 0 }}>
          <label className="form-label" htmlFor="tx-student">
            Student
          </label>
          <StudentPicker id="tx-student" value={draft.student_id || ''} onChange={v => setDraft(d => ({ ...d, student_id: v }))} allowAll />
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label" htmlFor="tx-status">
            Status
          </label>
          <select id="tx-status" className="form-select" value={draft.status || ''} onChange={e => setDraft(d => ({ ...d, status: e.target.value }))}>
            <option value="">All statuses</option>
            {STATUS_OPTIONS.map(s => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label" htmlFor="tx-from">
            From
          </label>
          <input id="tx-from" type="date" className="form-input" value={draft.from || ''} onChange={e => setDraft(d => ({ ...d, from: e.target.value }))} />
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label" htmlFor="tx-to">
            To
          </label>
          <input id="tx-to" type="date" className="form-input" value={draft.to || ''} onChange={e => setDraft(d => ({ ...d, to: e.target.value }))} />
        </div>
        <button type="submit" className="btn btn-primary">
          Apply
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => void load(filters)} aria-label="Reload transactions">
          <RefreshCw size={14} />
        </button>
      </form>

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <div className="table-responsive">
        <table className="data-table" aria-label="Online payment transactions" aria-busy={loading || undefined}>
          <thead>
            <tr>
              <th scope="col">Payment</th>
              <th scope="col">Student</th>
              <th scope="col">Invoice</th>
              <th scope="col">Amount</th>
              <th scope="col">Refunded</th>
              <th scope="col">Method</th>
              <th scope="col">Status</th>
              <th scope="col">Date</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={9} style={{ textAlign: 'center', color: '#64748B' }}>
                  No transactions match these filters.
                </td>
              </tr>
            )}
            {data?.items.map(tx => (
              <tr key={`${tx.kind || 'payment'}-${tx.id}`}>
                <td className="monospace" style={{ fontSize: 12 }}>
                  {tx.payment_number || tx.id.slice(0, 8)} {tx.is_test && <span className="badge badge-warning">TEST</span>}
                  {tx.kind === 'order' && <div style={{ color: '#64748B' }}>checkout attempt</div>}
                </td>
                <td>{tx.student_name || '—'}</td>
                <td>{tx.invoice_number || '—'}</td>
                <td>{money(tx.amount)}</td>
                <td>{tx.refunded_amount ? money(tx.refunded_amount) : '—'}</td>
                <td>
                  <span className="pay-method-badge">{tx.method || tx.provider || '—'}</span>
                </td>
                <td>
                  <span className={badgeClass(tx.status)} title={tx.failure_reason || undefined}>
                    {statusLabel(tx.status)}
                  </span>
                </td>
                <td>{dateTime(tx.paid_at || tx.created_at)}</td>
                <td>
                  <div className="table-action-cell" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {isRefundable(tx) && (
                      <button
                        type="button"
                        className="btn btn-outline btn-sm"
                        onClick={() => setRefundTarget(tx)}
                        aria-label={`Refund ${tx.payment_number || tx.id}`}
                      >
                        <RotateCcw size={13} /> Refund
                      </button>
                    )}
                    {tx.receipt_id && (
                      <button
                        type="button"
                        className="btn btn-outline btn-sm"
                        onClick={() => void receipt(tx.receipt_id!, tx.receipt_number)}
                        aria-label={`Download receipt ${tx.receipt_number || ''}`.trim()}
                      >
                        <Download size={13} /> Receipt
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {data && data.total > 0 && (
        <Pagination
          currentPage={data.page}
          totalRecords={data.total}
          pageSize={pageSize}
          onPageChange={p => setFilters(f => ({ ...f, page: p }))}
          itemLabel="transactions"
        />
      )}

      {refundTarget && (
        <RefundDialog
          tx={refundTarget}
          onClose={() => setRefundTarget(null)}
          onRefunded={() => {
            setRefundTarget(null);
            void load(filters);
          }}
          addToast={addToast}
        />
      )}
    </div>
  );
};

// ---------- Invoice generation ----------

const GenerateSection: React.FC<{ addToast?: ToastFn }> = ({ addToast }) => {
  const [structures, setStructures] = useState<FeeStructureOption[]>([]);
  const [structureId, setStructureId] = useState('');
  const [count, setCount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ created: number; skipped: number } | null>(null);

  useEffect(() => {
    void feePortalService.listFeeStructures().then(res => {
      if (res.success) setStructures(res.data || []);
      else setError(res.error?.message || 'Could not load fee structures.');
    });
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (!structureId) {
      setError('Choose a fee structure.');
      return;
    }
    const n = count ? Number(count) : undefined;
    if (n !== undefined && (!Number.isInteger(n) || n < 1 || n > 24)) {
      setError('Installments must be a whole number between 1 and 24.');
      return;
    }
    setError('');
    setBusy(true);
    const res = await feePortalService.generateInvoices(structureId, { installment_count: n });
    setBusy(false);
    if (res.success && res.data) {
      setResult(res.data);
      addToast?.('success', 'Invoices generated', `${res.data.created} created, ${res.data.skipped} skipped.`);
    } else {
      setError(res.error?.message || 'Could not generate invoices.');
    }
  };

  return (
    <form className="card" onSubmit={submit} aria-label="Generate invoices">
      <div className="card-header">
        <div>
          <h3 className="card-title">Generate invoices for a fee structure</h3>
          <p className="card-subtitle">Creates an invoice for every active student of the class (existing invoices are skipped).</p>
        </div>
      </div>
      <div className="card-body" style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'flex-end' }}>
        <div className="form-group" style={{ minWidth: 240, flex: '1 1 240px', marginBottom: 0 }}>
          <label className="form-label required" htmlFor="gen-structure">
            Fee structure
          </label>
          <select id="gen-structure" className="form-select" value={structureId} onChange={e => setStructureId(e.target.value)}>
            <option value="">Select…</option>
            {structures.map(s => (
              <option key={s.id} value={s.id}>
                {s.name}
                {s.class_name ? ` · ${s.class_name}` : ''}
                {s.academic_year_name ? ` · ${s.academic_year_name}` : ''}
                {typeof s.total_fee === 'number' ? ` · ${money(s.total_fee)}` : ''}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group" style={{ width: 160, marginBottom: 0 }}>
          <label className="form-label" htmlFor="gen-count">
            Installments (optional)
          </label>
          <input id="gen-count" className="form-input" type="number" min={1} max={24} step={1} value={count} onChange={e => setCount(e.target.value)} />
        </div>
        <button type="submit" className="btn btn-primary" disabled={busy} aria-busy={busy || undefined}>
          <FilePlus2 size={14} /> {busy ? 'Generating…' : 'Generate invoices'}
        </button>
      </div>
      {error && (
        <p className="form-error" role="alert" style={{ padding: '0 16px 12px' }}>
          {error}
        </p>
      )}
      {result && (
        <p role="status" style={{ padding: '0 16px 12px' }}>
          {result.created} invoice(s) created, {result.skipped} skipped.
        </p>
      )}
    </form>
  );
};

// ---------- Adjustments ----------

const AdjustmentForm: React.FC<{ invoice: PortalInvoice; onSaved: () => void; addToast?: ToastFn }> = ({ invoice, onSaved, addToast }) => {
  const [kind, setKind] = useState<AdjustmentKind>('DISCOUNT');
  const [label, setLabel] = useState('');
  const [amount, setAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const base = `adj-${invoice.id}`;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const n = Number(amount);
    if (!label.trim()) return setError('Enter a label.');
    if (!amount.trim() || !isFinite(n) || n <= 0) return setError('Enter an amount greater than zero.');
    if (kind !== 'LATE_FINE' && n > invoice.balance_amount) {
      return setError(`A ${statusLabel(kind).toLowerCase()} cannot exceed the balance of ${money(invoice.balance_amount)}.`);
    }
    setError('');
    setBusy(true);
    const res = await feePortalService.addAdjustment(invoice.id, { kind, label: label.trim(), amount: Math.round(n * 100) / 100 });
    setBusy(false);
    if (!res.success) return setError(res.error?.message || 'Could not add the adjustment.');
    setLabel('');
    setAmount('');
    addToast?.('success', 'Adjustment added', `${statusLabel(kind)} of ${money(n)} on ${invoice.invoice_number}.`);
    onSaved();
  };

  return (
    <form onSubmit={submit} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'flex-end', marginTop: 10 }} aria-label={`Add adjustment to ${invoice.invoice_number}`}>
      <div className="form-group" style={{ marginBottom: 0 }}>
        <label className="form-label" htmlFor={`${base}-kind`}>
          Type
        </label>
        <select id={`${base}-kind`} className="form-select" value={kind} onChange={e => setKind(e.target.value as AdjustmentKind)}>
          {ADJ_KINDS.map(k => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
      </div>
      <div className="form-group" style={{ marginBottom: 0, flex: '1 1 160px' }}>
        <label className="form-label" htmlFor={`${base}-label`}>
          Label
        </label>
        <input id={`${base}-label`} className="form-input" value={label} onChange={e => setLabel(e.target.value)} />
      </div>
      <div className="form-group" style={{ marginBottom: 0, width: 130 }}>
        <label className="form-label" htmlFor={`${base}-amount`}>
          Amount (₹)
        </label>
        <input id={`${base}-amount`} className="form-input" type="number" min={0.01} step="0.01" value={amount} onChange={e => setAmount(e.target.value)} />
      </div>
      <button type="submit" className="btn btn-primary btn-sm" disabled={busy}>
        <Tag size={13} /> {busy ? 'Saving…' : 'Add'}
      </button>
      {error && (
        <p className="form-error" role="alert" style={{ flexBasis: '100%', margin: 0 }}>
          {error}
        </p>
      )}
    </form>
  );
};

const AdjustmentsSection: React.FC<{ addToast?: ToastFn }> = ({ addToast }) => {
  const [studentId, setStudentId] = useState('');
  const [invoices, setInvoices] = useState<PortalInvoice[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [removeTarget, setRemoveTarget] = useState<{ invoice: PortalInvoice; adjId: string; label: string } | null>(null);

  const load = useCallback(async (id: string) => {
    if (!id) {
      setInvoices([]);
      return;
    }
    setLoading(true);
    const res = await feePortalService.getMyFees(id);
    setLoading(false);
    if (res.success && res.data) {
      setInvoices(res.data.students.flatMap(s => s.invoices));
      setError('');
    } else {
      setError(res.error?.message || 'Could not load invoices.');
    }
  }, []);

  useEffect(() => {
    void load(studentId);
  }, [studentId, load]);

  return (
    <div>
      <div className="form-group" style={{ maxWidth: 420 }}>
        <label className="form-label" htmlFor="adj-student">
          Student
        </label>
        <StudentPicker id="adj-student" value={studentId} onChange={setStudentId} />
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Loading invoices…</p>}
      {!loading && studentId && invoices.length === 0 && !error && <p>No invoices for this student.</p>}
      <div style={{ display: 'grid', gap: 12 }}>
        {invoices.map(inv => (
          <section key={inv.id} className="card" aria-label={`Invoice ${inv.invoice_number}`}>
            <div className="card-body">
              <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 8 }}>
                <strong>
                  {inv.invoice_number} · {inv.title}
                </strong>
                <span>
                  Total {money(inv.total_amount)} · Balance {money(inv.balance_amount)}{' '}
                  <span className={badgeClass(inv.status)}>{statusLabel(inv.status)}</span>
                </span>
              </div>
              {inv.adjustments.length > 0 ? (
                <ul style={{ listStyle: 'none', padding: 0, margin: '8px 0 0', display: 'grid', gap: 6 }}>
                  {inv.adjustments.map(a => (
                    <li key={a.id} style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                      <span className={`badge ${a.kind === 'LATE_FINE' ? 'badge-danger' : 'badge-success'}`}>{statusLabel(a.kind)}</span>
                      <span>{a.label}</span>
                      <strong>{money(a.amount)}</strong>
                      <button
                        type="button"
                        className="btn btn-outline btn-sm"
                        onClick={() => setRemoveTarget({ invoice: inv, adjId: a.id, label: a.label })}
                        aria-label={`Remove adjustment ${a.label}`}
                      >
                        <Trash2 size={13} /> Remove
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p style={{ margin: '8px 0 0', color: '#64748B', fontSize: 13 }}>No adjustments.</p>
              )}
              <AdjustmentForm invoice={inv} onSaved={() => void load(studentId)} addToast={addToast} />
            </div>
          </section>
        ))}
      </div>
      <ConfirmDialog
        isOpen={!!removeTarget}
        onClose={() => setRemoveTarget(null)}
        title="Remove adjustment"
        message={removeTarget ? `Remove "${removeTarget.label}" from ${removeTarget.invoice.invoice_number}? The invoice total will be recalculated.` : ''}
        confirmLabel="Remove"
        pendingLabel="Removing…"
        isDestructive
        onConfirm={async () => {
          if (!removeTarget) return;
          const res = await feePortalService.removeAdjustment(removeTarget.invoice.id, removeTarget.adjId);
          if (!res.success) {
            addToast?.('error', 'Could not remove adjustment', res.error?.message);
            throw new Error(res.error?.message || 'failed');
          }
          addToast?.('success', 'Adjustment removed');
          void load(studentId);
        }}
      />
    </div>
  );
};

// ---------- Container ----------

export const AdminOnlinePayments: React.FC<{ addToast?: ToastFn }> = ({ addToast }) => {
  const [section, setSection] = useState<Section>('transactions');
  const tabs: [Section, string][] = [
    ['transactions', 'Transactions & refunds'],
    ['generate', 'Generate invoices'],
    ['adjustments', 'Invoice adjustments'],
  ];
  return (
    <div>
      <div className="fee-tabs-container" role="tablist" aria-label="Online payments sections" style={{ marginBottom: 14 }}>
        {tabs.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={section === key}
            className={`fee-tab-pill ${section === key ? 'active' : ''}`}
            onClick={() => setSection(key)}
          >
            <span>{label}</span>
          </button>
        ))}
      </div>
      {section === 'transactions' && <TransactionsSection addToast={addToast} />}
      {section === 'generate' && <GenerateSection addToast={addToast} />}
      {section === 'adjustments' && <AdjustmentsSection addToast={addToast} />}
    </div>
  );
};
