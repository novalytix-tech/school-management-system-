"""
PDF fee receipts rendered from the frozen receipt snapshot (fpdf2, pure Python).

Only the snapshot is printed, so later fee-structure or student changes never
alter an issued receipt. The refund note is the one thing computed at render
time (from the refunds recorded since), and it is printed as a separate note.

The logo is read only from local files (never fetched over the network):
RECEIPT_LOGO_PATH, a logo_url that names a file bundled in app/static, or the
bundled default app/static/receipt_logo.png. Built-in PDF fonts are Latin-1
only, so the rupee sign is written as "Rs.".
"""
import os
from datetime import datetime, timedelta, timezone

from flask import current_app
from fpdf import FPDF
from fpdf.enums import XPos, YPos

IST = timezone(timedelta(hours=5, minutes=30), "IST")
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
DEFAULT_LOGO = os.path.join(STATIC_DIR, "receipt_logo.png")
TEST_WATERMARK = "TEST PAYMENT - NOT A REAL TRANSACTION"

_REPLACEMENTS = {
    "₹": "Rs.", "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
}


def _t(value) -> str:
    """Latin-1 safe text for the core PDF fonts."""
    s = "" if value is None else str(value)
    for k, v in _REPLACEMENTS.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _rs(value) -> str:
    try:
        return f"Rs. {float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "Rs. 0.00"


def _ist(iso_value) -> str:
    if not iso_value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return _t(iso_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _date(iso_value) -> str:
    if not iso_value:
        return "-"
    try:
        return datetime.fromisoformat(str(iso_value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return _t(iso_value)


def _local_file_inside(path: str, root: str):
    full = os.path.abspath(os.path.join(root, path))
    if os.path.commonpath([full, root]) != root:
        return None
    return full if os.path.isfile(full) else None


def resolve_logo(logo_url=None):
    """A local logo file; remote URLs are never fetched (no SSRF)."""
    configured = (current_app.config.get("RECEIPT_LOGO_PATH") or "").strip() if current_app else ""
    if configured and os.path.isfile(configured):
        return configured
    if isinstance(logo_url, str) and logo_url and "://" not in logo_url and not logo_url.startswith("//"):
        name = logo_url.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        found = _local_file_inside(name, STATIC_DIR)
        if found and found.lower().endswith((".png", ".jpg", ".jpeg")):
            return found
    return DEFAULT_LOGO if os.path.isfile(DEFAULT_LOGO) else None


class _ReceiptPDF(FPDF):
    def __init__(self, watermark: bool):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.watermark = watermark
        self.set_auto_page_break(auto=True, margin=18)

    def header(self):
        if self.watermark:
            self.set_font("Helvetica", "B", 30)
            self.set_text_color(235, 180, 180)
            with self.rotation(35, self.w / 2, self.h / 2):
                self.text(self.w / 2 - 105, self.h / 2, TEST_WATERMARK)
            self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, _t("Computer-generated receipt. No signature required."), align="C")
        self.set_text_color(0, 0, 0)


def render_receipt_pdf(snapshot: dict, refund_state: dict = None) -> bytes:
    snap = snapshot or {}
    school = snap.get("school") or {}
    student = snap.get("student") or {}
    invoice = snap.get("invoice") or {}
    payment = snap.get("payment") or {}
    is_test = bool(snap.get("is_test"))
    refund_state = refund_state or {}

    pdf = _ReceiptPDF(watermark=is_test)
    pdf.set_title(_t(snap.get("receipt_number") or "Fee receipt"))
    pdf.set_author(_t(school.get("name") or "School"))
    pdf.add_page()

    # Header: logo + school details
    logo = resolve_logo(school.get("logo_url"))
    top = pdf.get_y()
    if logo:
        try:
            pdf.image(logo, x=10, y=top, w=22)
        except Exception:  # unreadable image: render without it
            logo = None
    pdf.set_xy(36 if logo else 10, top)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 8, _t(school.get("name") or "School"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    for line in (school.get("address"), school.get("affiliation"),
                 " | ".join(p for p in [school.get("phone"), school.get("email")] if p)):
        if line:
            pdf.set_x(36 if logo else 10)
            pdf.cell(0, 4.5, _t(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(max(pdf.get_y(), top + 24) + 2)

    pdf.set_draw_color(30, 64, 120)
    pdf.set_line_width(0.6)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _t("FEE RECEIPT" + ("  (TEST)" if is_test else "")), align="C",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if is_test:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(190, 30, 30)
        pdf.cell(0, 6, TEST_WATERMARK, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    def pair_row(l1, v1, l2, v2):
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(32, 6, _t(l1))
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(63, 6, _t(v1 if v1 not in (None, "") else "-"))
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(32, 6, _t(l2))
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, _t(v2 if v2 not in (None, "") else "-"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    class_section = " - ".join(p for p in [student.get("class_name"), student.get("section_name")] if p) or "-"
    provider = payment.get("provider") or "OFFLINE"
    pair_row("Receipt No.", snap.get("receipt_number"), "Issued", _ist(snap.get("issued_at")))
    pair_row("Student", student.get("name"), "Admission No.", student.get("admission_number"))
    pair_row("Class & Section", class_section, "Session", student.get("academic_session"))
    pair_row("Guardian", student.get("guardian_name"), "Invoice No.", invoice.get("invoice_number"))
    pair_row("Payment date", _ist(payment.get("paid_at")), "Fee", invoice.get("title"))
    method = payment.get("method") or "-"
    if payment.get("instrument"):
        method = f"{method} ({str(payment.get('instrument')).upper()})"
    pair_row("Method", method, "Status", payment.get("status") or "SUCCESS")
    if provider == "RAZORPAY":
        pair_row("Razorpay order", payment.get("gateway_order_id"), "Razorpay payment",
                 payment.get("gateway_payment_id"))
    elif provider == "MOCK":
        pair_row("Mock order", payment.get("gateway_order_id"), "Mock payment", payment.get("gateway_payment_id"))
    else:
        pair_row("Payment No.", payment.get("payment_number"), "Reference", payment.get("reference") or "Offline")
    pdf.ln(3)

    # Fee breakdown
    pdf.set_fill_color(230, 236, 245)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(140, 7, "Fee category", border=1, fill=True)
    pdf.cell(50, 7, "Amount", border=1, fill=True, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    for item in snap.get("items") or []:
        pdf.cell(140, 6.5, _t(item.get("fee_head")), border=1)
        pdf.cell(50, 6.5, _rs(item.get("amount")), border=1, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def total_row(label, value, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(140, 6.5, _t(label), border=1, align="R")
        pdf.cell(50, 6.5, _t(value), border=1, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    total_row("Subtotal", _rs(invoice.get("subtotal")))
    for adj in snap.get("adjustments") or []:
        sign = "+" if adj.get("kind") == "LATE_FINE" else "-"
        total_row(f"{str(adj.get('kind', '')).replace('_', ' ').title()}: {adj.get('label', '')}",
                  f"{sign} {_rs(adj.get('amount'))}")
    total_row("Discount / scholarship", f"- {_rs(snap.get('discount'))}")
    total_row("Fine", f"+ {_rs(snap.get('fine'))}")
    total_row("Invoice total", _rs(invoice.get("total_amount")), bold=True)
    total_row("Amount paid (this receipt)", _rs(snap.get("amount_paid")), bold=True)
    total_row("Total paid to date", _rs(snap.get("total_paid_to_date")))
    total_row("Remaining balance after this payment", _rs(snap.get("balance_after")), bold=True)
    pdf.ln(4)

    state = refund_state.get("refund_status")
    if state in ("PARTIAL", "FULL"):
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(170, 90, 0)
        label = "REFUNDED" if state == "FULL" else "PARTIALLY REFUNDED"
        pdf.multi_cell(0, 6, _t(f"{label}: {_rs(refund_state.get('refunded_amount'))} of this payment has been "
                                f"refunded (as of {datetime.now(IST).strftime('%d %b %Y')}). The amounts above "
                                "are as originally issued."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(0, 4.5, _t("Amounts in Indian Rupees (INR). Times shown in IST. "
                              "Please quote the receipt number for any query."),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())
