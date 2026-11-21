# Fee Payments — Razorpay integration (design & API contract)

Server is the single source of truth for invoices, balances, payments, refunds
and receipts. The browser never decides that money was paid.

## Payment modes (`PAYMENT_MODE`)

| Value | Behaviour |
|---|---|
| `auto` (default) | `live` when `RAZORPAY_KEY_ID` + `RAZORPAY_KEY_SECRET` are set, otherwise `disabled` |
| `live` | Real Razorpay Checkout. Requires both keys (fails at startup otherwise). A `rzp_test_` key is Razorpay's own sandbox and is labelled **TEST** |
| `mock` | Simulated gateway for development/demo. **Must be chosen explicitly.** No money moves. Every mock payment/receipt is flagged `is_test=true` and labelled "TEST PAYMENT — NOT A REAL TRANSACTION" |
| `disabled` | Fees are visible; online payment buttons are hidden ("Online payment not configured") |

Env vars (never hardcoded): `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `PAYMENT_MODE`.
Webhook URL to register in the Razorpay dashboard: `POST {API}/api/v1/payments/razorpay/webhook`
(events: `payment.authorized`, `payment.captured`, `payment.failed`, `order.paid`, `refund.processed`, `refund.failed`).

## Data model (new / extended)

- `fee_invoices` + `fine_amount`, `refunded_amount`. `total = subtotal − discount_amount + fine_amount`;
  `balance = total − paid_amount + refunded_amount` (a refund re-opens the balance).
  `discount_amount` = sum of DISCOUNT + SCHOLARSHIP + WAIVER adjustments.
- `fee_invoice_adjustments` (invoice_id, kind `DISCOUNT|SCHOLARSHIP|WAIVER|LATE_FINE`, label, amount > 0, created_by, created_at).
- `fee_installments` (invoice_id, installment_number, label, amount, due_date, paid_amount, status `PENDING|PARTIALLY_PAID|PAID|OVERDUE`).
  Payments are allocated to installments in due-date order automatically. Sum of installment amounts == invoice total
  (re-balanced proportionally on the last installment when adjustments change the total).
- `payment_orders` — one row per checkout attempt: institution_id, invoice_id, student_id, initiated_by, provider
  (`RAZORPAY|MOCK`), is_test, gateway_order_id (unique), gateway_payment_id (unique, nullable), amount, currency `INR`,
  status `CREATED → PENDING → SUCCEEDED | FAILED | CANCELLED | EXPIRED | NEEDS_REVIEW`, failure_reason,
  idempotency_key (unique per initiated_by), created_at, pending_at, succeeded_at, failed_at, cancelled_at,
  payment_id (→ fee_payments when succeeded).
- `fee_payments` + `refunded_amount`, `is_test`, `order_id`; status `SUCCESS|PARTIALLY_REFUNDED|REFUNDED`.
- `fee_refunds` — payment_id, gateway_refund_id (unique), amount, reason, status `PENDING|PROCESSED|FAILED`,
  requested_by, created_at, processed_at.
- `fee_receipts` — payment_id (unique), receipt_number (unique, `RCT-<YYYY>-<6 digits>` per institution), issued_at,
  is_test, `snapshot` JSONB (every field printed on the receipt, frozen at issue time — later fee-structure or
  student changes never alter an issued receipt).
- `payment_gateway_webhooks` (existing): event_id unique → webhook idempotency.

Invariants: a receipt exists only for a SUCCEEDED order / recorded payment; settlement is idempotent (row lock on
the order + unique gateway_payment_id); the amount always comes from the server (order row / Razorpay API), never
from the client.

## API (all under `/api/v1`, JSON envelope `{success, data, error:{code,message}}`)

Amounts in JSON are numbers in rupees with 2 decimals; gateway amounts are integer paise (`amount_paise`).
Timestamps are ISO-8601 UTC.

### Config
`GET /payments/config` (any signed-in user) →
`{ mode: "live"|"mock"|"disabled", is_test: bool, key_id: string|null, currency: "INR", school_name, merchant_name }`

### Student / parent fee portal
`GET /finance/my-fees[?student_id=<uuid>]` — students see themselves, parents their wards, staff any student of the
school (student_id required for staff). Response:
```json
{
  "students": [{
    "student": { "id", "name", "admission_number", "class_name", "section_name", "guardian_name", "academic_session" },
    "summary": { "total_assigned", "discounts", "scholarships", "fines", "paid", "refunded", "pending", "overdue", "remaining_balance" },
    "invoices": [{
      "id", "invoice_number", "title", "status", "issue_date", "due_date", "is_overdue",
      "subtotal", "discount_amount", "fine_amount", "total_amount", "paid_amount", "refunded_amount", "balance_amount",
      "items": [{ "fee_head", "amount" }],
      "adjustments": [{ "id", "kind", "label", "amount", "created_at" }],
      "installments": [{ "id", "installment_number", "label", "amount", "paid_amount", "due_date", "status" }],
      "next_payable_amount"
    }],
    "timeline": [{ "order_id", "invoice_id", "invoice_number", "amount", "status", "provider", "is_test",
                   "gateway_order_id", "gateway_payment_id", "failure_reason",
                   "initiated_at", "pending_at", "succeeded_at", "failed_at", "cancelled_at",
                   "receipt_id", "refunds": [{ "id", "amount", "status", "created_at", "processed_at" }] }],
    "payments": [{ "id", "payment_number", "invoice_number", "amount", "refunded_amount", "method", "status",
                   "is_test", "paid_at", "receipt_id", "receipt_number" }]
  }]
}
```

### Checkout
`POST /payments/razorpay/order` `{ invoice_id, amount?: number (rupees), idempotency_key: string }` →
`{ order_id (our uuid), gateway_order_id, amount_paise, currency, key_id, mode, is_test, invoice_number, student_name,
   prefill: { name, email, contact } }`
- amount defaults to the invoice's `next_payable_amount`; must be ≥ ₹1 and ≤ balance; invoice must be payable and
  belong to the caller (student self / parent ward / admin).
- Same `idempotency_key` from the same user returns the same open order (no duplicate orders on double-click).
- `mode=disabled` → 409 `PAYMENTS_DISABLED`.

`POST /payments/razorpay/verify` `{ order_id, razorpay_order_id, razorpay_payment_id, razorpay_signature }` →
200 `{ status: "SUCCEEDED", receipt_id, receipt_number, payment_id, invoice: {…balance fields} }` |
202 `{ status: "PENDING" }` (authorized, not yet captured — webhook will finish it) |
400 `SIGNATURE_MISMATCH` (order untouched). Never marks success without a valid signature AND a captured payment
fetched from Razorpay.

`POST /payments/orders/<order_id>/cancel` — user closed checkout → `CANCELLED` (only from CREATED).
`POST /payments/orders/<order_id>/fail` `{ reason }` — checkout reported failure → `FAILED` (only from CREATED/PENDING;
a later verified capture/webhook still wins and settles it).
`GET /payments/orders/<order_id>` — current status (for polling while PENDING).

Mock mode only: `POST /payments/mock/orders/<order_id>/complete` `{ outcome: "success"|"failure"|"cancel" }` →
same response shapes as verify/fail/cancel. 404 when not in mock mode.

### Webhook (public, no JWT)
`POST /payments/razorpay/webhook` — verifies `X-Razorpay-Signature` = HMAC-SHA256(raw body, RAZORPAY_WEBHOOK_SECRET);
dedupes by `X-Razorpay-Event-Id`; `payment.captured`/`order.paid` → settle (idempotent), `payment.authorized` → PENDING,
`payment.failed` → FAILED, `refund.processed`/`refund.failed` → refund status. Always 200 for verified, already-seen
events; 400 for bad signatures.

### Receipts
`GET /finance/receipts?student_id=` — list (same visibility rules).
`GET /finance/receipts/<receipt_id>` — JSON snapshot.
`GET /finance/receipts/<receipt_id>/pdf` — `application/pdf` (`Content-Disposition: inline; filename=<receipt_number>.pdf`).
Rendered from the frozen snapshot. Test receipts carry a "TEST PAYMENT — NOT A REAL TRANSACTION" watermark.

### Administration (school_admin / super_admin)
- `POST /finance/invoices` `{ student_id, fee_structure_id, due_date?, installments?: [{label, amount, due_date}] |
  installment_count?: int, adjustments?: [{kind,label,amount}] }` — copies structure items into invoice items.
- `POST /finance/fee-structures/<id>/generate-invoices` `{ section_id?, installment_count? }` — every active enrolled
  student of the class (skips students that already have an invoice for that structure) → `{ created, skipped }`.
- `POST /finance/invoices/<id>/adjustments` `{ kind, label, amount }` / `DELETE /finance/invoices/<id>/adjustments/<adj_id>`.
- `POST /finance/invoices/<id>/pay` (existing, offline cash/cheque/bank) — now also allocates installments and issues a receipt.
- `GET /finance/payments?student_id=&status=&from=&to=&page=` — transaction history incl. orders and refunds.
- `POST /finance/payments/<payment_id>/refund` `{ amount, reason }` — Razorpay refund API in live mode, immediate
  PROCESSED in mock mode, immediate PROCESSED for offline payments (cash returned). Cannot exceed
  amount − refunded_amount. Audit-logged.
