// ==========================================
// Online checkout for a server invoice (Razorpay live / mock test gateway).
// The server decides the outcome: success is shown ONLY when the server
// reports the order SUCCEEDED (verify / mock-complete / polling).
// Developed by NTS Novalytix
// ==========================================

import React, { useEffect, useRef, useState } from 'react';
import { Modal } from '../../common/Modal';
import {
  feePortalService,
  newIdempotencyKey,
  CheckoutOrder,
  PaymentConfig,
  PortalInvoice,
  SettlementResult,
} from '../../../services/feePortalService';
import { money } from './paymentFormat';
import type { ReceiptAction } from './receiptActions';
import { loadRazorpayCheckout, POLL_INTERVAL_MS, POLL_MAX_ATTEMPTS } from './razorpayCheckout';
import { AlertCircle, CheckCircle2, Clock, Download, FlaskConical, Printer, XCircle } from 'lucide-react';

type Phase = 'select' | 'creating' | 'mock' | 'gateway' | 'verifying' | 'pending' | 'success' | 'failed' | 'cancelled' | 'timeout';
type AmountChoice = 'next' | 'full' | 'custom';

export interface OnlineCheckoutModalProps {
  invoice: PortalInvoice | null;
  config: PaymentConfig;
  studentName?: string;
  onClose: () => void;
  /** Called once when an attempt reaches a final state (refresh the portal). */
  onSettled?: () => void;
  onReceiptAction?: (receiptId: string, action: ReceiptAction, receiptNumber?: string | null) => void;
}

const round2 = (n: number) => Math.round(n * 100) / 100;

export const OnlineCheckoutModal: React.FC<OnlineCheckoutModalProps> = ({
  invoice,
  config,
  studentName,
  onClose,
  onSettled,
  onReceiptAction,
}) => {
  const [phase, setPhase] = useState<Phase>('select');
  const [choice, setChoice] = useState<AmountChoice>('next');
  const [custom, setCustom] = useState('');
  const [amountError, setAmountError] = useState('');
  const [message, setMessage] = useState('');
  const [order, setOrder] = useState<CheckoutOrder | null>(null);
  const [result, setResult] = useState<SettlementResult | null>(null);
  const [mockBusy, setMockBusy] = useState(false);

  // Ref guards: a second click in the same frame must not start a second order.
  const inFlightRef = useRef(false);
  const mockBusyRef = useRef(false);
  const mountedRef = useRef(true);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settledNotifiedRef = useRef(false);
  const phaseRef = useRef<Phase>('select');
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  if (!invoice) return null;

  const balance = round2(invoice.balance_amount);
  const nextPayable = round2(invoice.next_payable_amount || balance);

  const notifySettled = () => {
    if (settledNotifiedRef.current) return;
    settledNotifiedRef.current = true;
    onSettled?.();
  };

  const finish = (p: Phase, msg = '', res: SettlementResult | null = null) => {
    if (!mountedRef.current) return;
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setPhase(p);
    setMessage(msg);
    if (res) setResult(res);
    if (p === 'success' || p === 'failed' || p === 'cancelled') notifySettled();
  };

  /** Apply a server settlement response (verify / mock / poll). */
  const applySettlement = (data: SettlementResult | null | undefined, fallbackError?: string): boolean => {
    const status = data?.status;
    if (status === 'SUCCEEDED') {
      finish('success', '', data!);
      return true;
    }
    if (status === 'FAILED' || status === 'EXPIRED') {
      finish('failed', data?.failure_reason || fallbackError || 'The payment failed. No money was taken for this attempt.');
      return true;
    }
    if (status === 'CANCELLED') {
      finish('cancelled', 'Payment was cancelled. You can try again whenever you are ready.');
      return true;
    }
    return false;
  };

  const startPolling = (orderId: string) => {
    setPhase('pending');
    setMessage('');
    let attempts = 0;
    const tick = async () => {
      attempts += 1;
      const res = await feePortalService.getOrder(orderId);
      if (!mountedRef.current || phaseRef.current !== 'pending') return;
      if (res.success && applySettlement(res.data)) return;
      if (attempts >= POLL_MAX_ATTEMPTS) {
        finish('timeout');
        notifySettled();
        return;
      }
      pollTimerRef.current = setTimeout(tick, POLL_INTERVAL_MS);
    };
    pollTimerRef.current = setTimeout(tick, POLL_INTERVAL_MS);
  };

  const verify = async (o: CheckoutOrder, resp: any) => {
    if (!mountedRef.current) return;
    setPhase('verifying');
    const res = await feePortalService.verify({
      order_id: o.order_id,
      razorpay_order_id: resp?.razorpay_order_id || o.gateway_order_id,
      razorpay_payment_id: resp?.razorpay_payment_id,
      razorpay_signature: resp?.razorpay_signature,
    });
    if (res.success && res.data?.status === 'PENDING') {
      startPolling(o.order_id);
      return;
    }
    if (res.success && applySettlement(res.data)) return;
    if (res.error?.code === 'SIGNATURE_MISMATCH') {
      finish(
        'failed',
        'We could not verify this payment with the bank, so it has NOT been recorded. If money was debited it will be refunded automatically — please contact the school office.'
      );
      return;
    }
    finish(
      'failed',
      `We could not confirm this payment right now${resp?.razorpay_payment_id ? ` (payment id ${resp.razorpay_payment_id})` : ''}. ` +
        `${res.error?.message ? res.error.message + ' ' : ''}Please do not pay again — your fee status will update automatically once the bank confirms. Contact the school office if it does not.`
    );
  };

  const openGateway = async (o: CheckoutOrder) => {
    const loaded = await loadRazorpayCheckout();
    if (!mountedRef.current) return;
    const Razorpay = (window as any).Razorpay;
    if (!loaded || !Razorpay) {
      void feePortalService.cancelOrder(o.order_id);
      finish('failed', 'Could not load the payment gateway. Please check your internet connection and try again.');
      return;
    }
    setPhase('gateway');
    let handled = false;
    let failureReported = false;
    try {
      const rzp = new Razorpay({
        key: o.key_id || config.key_id,
        order_id: o.gateway_order_id,
        amount: o.amount_paise,
        currency: o.currency || 'INR',
        name: config.merchant_name || config.school_name || 'School fees',
        description: `Fee payment — ${o.invoice_number || invoice.invoice_number}`,
        prefill: o.prefill || { name: studentName },
        notes: { order_id: o.order_id, invoice_number: o.invoice_number || invoice.invoice_number },
        theme: { color: '#3B82F6' },
        handler: (response: any) => {
          handled = true;
          void verify(o, response);
        },
        modal: {
          escape: true,
          ondismiss: () => {
            if (handled || failureReported) return;
            void feePortalService.cancelOrder(o.order_id).then(res => {
              // The server may report the order already settled (e.g. a
              // webhook won the race): trust its status.
              if (res.success && applySettlement(res.data)) return;
              finish('cancelled', 'Payment was cancelled. You can try again whenever you are ready.');
            });
          },
        },
      });
      rzp.on('payment.failed', (resp: any) => {
        failureReported = true;
        const reason = resp?.error?.description || resp?.error?.reason || 'Payment failed at the gateway.';
        void feePortalService.failOrder(o.order_id, reason).then(() => {
          // A later successful retry inside the gateway still wins (handler → verify).
          if (!handled) finish('failed', reason);
        });
      });
      rzp.open();
    } catch (err: any) {
      void feePortalService.cancelOrder(o.order_id);
      finish('failed', err?.message || 'Unable to open the payment gateway.');
    }
  };

  const resolveAmount = (): number | undefined | null => {
    if (choice === 'next') return undefined; // server default: next_payable_amount
    if (choice === 'full') return balance;
    const n = Number(custom);
    if (!custom.trim() || !isFinite(n)) {
      setAmountError('Enter an amount.');
      return null;
    }
    if (n < 1) {
      setAmountError('The minimum online payment is ₹1.');
      return null;
    }
    if (round2(n) > balance) {
      setAmountError(`The amount cannot exceed the balance of ${money(balance)}.`);
      return null;
    }
    return round2(n);
  };

  const startPayment = async () => {
    if (inFlightRef.current) return;
    setAmountError('');
    const amount = resolveAmount();
    if (amount === null) return;
    inFlightRef.current = true;
    setPhase('creating');
    setMessage('');
    settledNotifiedRef.current = false;
    const res = await feePortalService.createOrder({ invoiceId: invoice.id, amount, idempotencyKey: newIdempotencyKey() });
    if (!mountedRef.current) return;
    if (!res.success || !res.data?.order_id) {
      inFlightRef.current = false;
      setPhase('select');
      setMessage(
        res.error?.code === 'PAYMENTS_DISABLED'
          ? 'Online payment is not configured yet. Please pay at the school office.'
          : res.error?.message || 'Could not start the payment. Please try again.'
      );
      return;
    }
    const o = res.data;
    setOrder(o);
    const mode = o.mode || config.mode;
    if (mode === 'mock') {
      setPhase('mock');
    } else if (mode === 'live') {
      await openGateway(o);
    } else {
      finish('failed', 'Online payment is not configured yet. Please pay at the school office.');
    }
  };

  const mockComplete = async (outcome: 'success' | 'failure' | 'cancel') => {
    if (!order || mockBusyRef.current) return;
    mockBusyRef.current = true;
    setMockBusy(true);
    const res = await feePortalService.mockComplete(order.order_id, outcome);
    mockBusyRef.current = false;
    if (!mountedRef.current) return;
    setMockBusy(false);
    if (res.success && res.data?.status === 'PENDING') {
      startPolling(order.order_id);
      return;
    }
    if (res.success && applySettlement(res.data)) return;
    finish('failed', res.error?.message || 'The test payment could not be completed.');
  };

  const tryAgain = () => {
    inFlightRef.current = false;
    setOrder(null);
    setResult(null);
    setMessage('');
    setPhase('select');
  };

  const handleClose = () => {
    if (phase === 'creating' || phase === 'verifying' || mockBusy) return;
    // Leaving the test gateway without choosing: the attempt is abandoned.
    if (phase === 'mock' && order) void feePortalService.cancelOrder(order.order_id);
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    onClose();
  };

  const busy = phase === 'creating' || phase === 'gateway' || phase === 'verifying';
  const orderAmount = order ? order.amount_paise / 100 : null;
  const isTest = !!(order?.is_test ?? config.is_test);

  const footer = (() => {
    if (phase === 'select' || phase === 'creating') {
      return (
        <>
          <button type="button" className="btn btn-secondary" onClick={handleClose} disabled={phase === 'creating'}>
            Close
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void startPayment()}
            disabled={phase === 'creating'}
            aria-busy={phase === 'creating' || undefined}
          >
            {phase === 'creating' ? 'Starting payment…' : 'Proceed to pay'}
          </button>
        </>
      );
    }
    if (phase === 'failed' || phase === 'cancelled') {
      return (
        <>
          <button type="button" className="btn btn-secondary" onClick={handleClose}>
            Close
          </button>
          <button type="button" className="btn btn-primary" onClick={tryAgain}>
            Try again
          </button>
        </>
      );
    }
    if (phase === 'mock') return null;
    return (
      <button type="button" className="btn btn-secondary" onClick={handleClose} disabled={busy}>
        Close
      </button>
    );
  })();

  return (
    <Modal isOpen onClose={handleClose} title={`Pay fees — ${invoice.invoice_number}`} footer={footer}>
      <div aria-live="polite">
        {isTest && (
          <p className="badge badge-warning" style={{ marginBottom: 12 }} data-testid="checkout-test-badge">
            <FlaskConical size={12} /> TEST MODE
          </p>
        )}

        {(phase === 'select' || phase === 'creating') && (
          <fieldset style={{ border: 0, padding: 0, margin: 0 }} disabled={phase === 'creating'}>
            <legend className="form-label" style={{ marginBottom: 8 }}>
              {invoice.title} · Balance {money(balance)}
            </legend>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
              <input type="radio" name="pay-amount" checked={choice === 'next'} onChange={() => setChoice('next')} />
              <span>
                Amount due now <strong>{money(nextPayable)}</strong>
              </span>
            </label>
            {nextPayable < balance && (
              <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
                <input type="radio" name="pay-amount" checked={choice === 'full'} onChange={() => setChoice('full')} />
                <span>
                  Full balance <strong>{money(balance)}</strong>
                </span>
              </label>
            )}
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
              <input type="radio" name="pay-amount" checked={choice === 'custom'} onChange={() => setChoice('custom')} />
              <span>Another amount</span>
            </label>
            {choice === 'custom' && (
              <div className="form-group">
                <label className="form-label" htmlFor="custom-pay-amount">
                  Amount (₹1 – {money(balance)})
                </label>
                <input
                  id="custom-pay-amount"
                  className="form-input"
                  type="number"
                  inputMode="decimal"
                  min={1}
                  max={balance}
                  step="0.01"
                  value={custom}
                  onChange={e => setCustom(e.target.value)}
                  aria-invalid={!!amountError || undefined}
                  aria-describedby={amountError ? 'custom-pay-amount-error' : undefined}
                />
              </div>
            )}
            {amountError && (
              <p id="custom-pay-amount-error" className="form-error" role="alert">
                {amountError}
              </p>
            )}
            {message && (
              <p className="form-error" role="alert">
                {message}
              </p>
            )}
          </fieldset>
        )}

        {phase === 'mock' && order && (
          <div role="group" aria-label="Test payment gateway">
            <div
              style={{ background: '#FEF3C7', border: '1px solid #FCD34D', borderRadius: 8, padding: 12, marginBottom: 12, color: '#92400E' }}
            >
              <strong>TEST MODE — no real money will be charged</strong>
              <p style={{ margin: '4px 0 0', fontSize: 13 }}>
                Simulated gateway for {order.invoice_number || invoice.invoice_number}: {money(orderAmount)}
              </p>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              <button type="button" className="btn btn-primary" disabled={mockBusy} onClick={() => void mockComplete('success')}>
                Simulate success
              </button>
              <button type="button" className="btn btn-danger" disabled={mockBusy} onClick={() => void mockComplete('failure')}>
                Simulate failure
              </button>
              <button type="button" className="btn btn-secondary" disabled={mockBusy} onClick={() => void mockComplete('cancel')}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {phase === 'gateway' && <p>Complete the payment in the Razorpay window…</p>}
        {phase === 'verifying' && <p>Confirming your payment with the server…</p>}

        {phase === 'pending' && (
          <p style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Clock size={18} color="#D97706" /> Payment processing — we'll update automatically.
          </p>
        )}

        {phase === 'timeout' && (
          <p style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Clock size={18} color="#D97706" /> Your payment is still being processed by the bank. You can close this
            window — the fee status will update automatically. Please do not pay again.
          </p>
        )}

        {phase === 'success' && result && (
          <div>
            <p style={{ display: 'flex', gap: 8, alignItems: 'center', color: '#16A34A', fontWeight: 700 }}>
              <CheckCircle2 size={20} /> Payment successful
            </p>
            {orderAmount !== null && <p>Amount paid: {money(orderAmount)}</p>}
            {result.receipt_number ? (
              <p>
                Receipt number: <strong data-testid="receipt-number">{result.receipt_number}</strong>
              </p>
            ) : (
              <p>Your receipt will appear in the Receipts tab shortly.</p>
            )}
            {isTest && <p className="badge badge-warning">TEST PAYMENT — NOT A REAL TRANSACTION</p>}
            {result.receipt_id && onReceiptAction && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
                <button
                  type="button"
                  className="btn btn-outline btn-sm"
                  onClick={() => onReceiptAction(result.receipt_id!, 'download', result.receipt_number)}
                >
                  <Download size={14} /> Download receipt
                </button>
                <button
                  type="button"
                  className="btn btn-outline btn-sm"
                  onClick={() => onReceiptAction(result.receipt_id!, 'print', result.receipt_number)}
                >
                  <Printer size={14} /> Print receipt
                </button>
              </div>
            )}
          </div>
        )}

        {phase === 'failed' && (
          <p role="alert" style={{ display: 'flex', gap: 8, alignItems: 'flex-start', color: '#DC2626' }}>
            <XCircle size={20} style={{ flexShrink: 0 }} /> <span>Payment failed. {message}</span>
          </p>
        )}

        {phase === 'cancelled' && (
          <p style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <AlertCircle size={18} color="#64748B" /> {message}
          </p>
        )}
      </div>
    </Modal>
  );
};
