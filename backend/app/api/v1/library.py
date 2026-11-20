import uuid
from datetime import date, timedelta
from decimal import Decimal
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity
from sqlalchemy import or_
from app.extensions import db
from app.models import Book, BookCopy, BookLoan, LibraryFine, User
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException

library_bp = Blueprint("library", __name__, url_prefix="/library")


# -------------------------------------------------------------
# 1. Books Catalog
# -------------------------------------------------------------

@library_bp.route("/books", methods=["GET"])
def list_books():
    institution_id = get_current_tenant_id()
    query = Book.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    search = request.args.get("search", "").strip()
    if search:
        query = query.filter(
            (Book.title.ilike(f"%{search}%")) |
            (Book.author.ilike(f"%{search}%")) |
            (Book.isbn.ilike(f"%{search}%")) |
            (Book.category.ilike(f"%{search}%"))
        )

    category = request.args.get("category")
    if category:
        query = query.filter_by(category=category.upper())

    books = query.order_by(Book.title.asc()).all()
    data = [
        {
            "id": str(b.id),
            "isbn": b.isbn,
            "title": b.title,
            "author": b.author,
            "publisher": b.publisher,
            "edition": b.edition,
            "category": b.category,
            "total_copies": b.total_copies,
            "available_copies": b.available_copies,
            "copies": [
                {
                    "id": str(c.id),
                    "barcode": c.barcode,
                    "accession_number": c.accession_number,
                    "condition": c.condition,
                    "status": c.status,
                }
                for c in b.copies
            ]
        }
        for b in books
    ]
    return jsonify({"success": True, "data": data}), 200


MAX_COPIES_PER_BOOK = 500
MAX_LOAN_DAYS = 365


def _req_str(payload, key, max_len, required=True, default=None):
    value = payload.get(key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise APIException(f"{key} is required", status_code=400)
        return None
    if not isinstance(value, str) or len(value.strip()) > max_len:
        raise APIException(f"{key} must be a string (max {max_len} characters)", status_code=400)
    return value.strip()


def _strict_int(value, field, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        raise APIException(f"{field} must be an integer between {lo} and {hi}", status_code=400)
    return value


def _uuid(value, field):
    if not isinstance(value, str):
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")


def _actor_id():
    """The authenticated user performing the action (never a fallback user)."""
    identity = get_jwt_identity()
    if not identity:
        raise APIException("Authentication required", status_code=401, error_code="AUTH_REQUIRED")
    return uuid.UUID(str(identity))


@library_bp.route("/books", methods=["POST"])
def create_book():
    institution_id = get_current_tenant_id(required=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)

    title = _req_str(payload, "title", 255, required=False)
    author = _req_str(payload, "author", 255, required=False)
    if not title or not author:
        raise APIException("Title and author are required", status_code=400)
    isbn = _req_str(payload, "isbn", 20, required=False) or f"ISBN-{uuid.uuid4().hex[:12].upper()}"
    copies_count = _strict_int(payload.get("copies_count", 1), "copies_count", 1, MAX_COPIES_PER_BOOK)
    publisher = _req_str(payload, "publisher", 255, required=False)
    edition = _req_str(payload, "edition", 50, required=False)
    category = _req_str(payload, "category", 100, required=False) or "GENERAL"

    exists = Book.query.filter_by(institution_id=institution_id, isbn=isbn).first()
    if exists:
        raise APIException(f"Book with ISBN '{isbn}' already exists", status_code=409)

    book = Book(
        institution_id=institution_id,
        isbn=isbn,
        title=title,
        author=author,
        publisher=publisher,
        edition=edition,
        category=category.upper(),
        total_copies=copies_count,
        available_copies=copies_count,
    )
    db.session.add(book)
    db.session.flush()

    # Barcodes/accession numbers are globally UNIQUE, so include a book-specific
    # component: the same ISBN in another institution must not collide.
    book_tag = book.id.hex[:10].upper()
    for idx in range(1, copies_count + 1):
        db.session.add(BookCopy(
            book_id=book.id,
            barcode=f"BC-{book.isbn}-{book_tag}-{idx}",
            accession_number=f"ACC-{book.isbn}-{book_tag}-{idx}",
            condition="NEW",
            status="AVAILABLE",
        ))

    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(book.id), "title": book.title, "total_copies": book.total_copies},
        "message": f"Book '{book.title}' added with {copies_count} copies."
    }), 201


# -------------------------------------------------------------
# 2. Circulation (Issue, Return, Renew)
# -------------------------------------------------------------

def _tenant_loans(institution_id):
    return (
        BookLoan.query
        .join(BookCopy, BookLoan.copy_id == BookCopy.id)
        .join(Book, BookCopy.book_id == Book.id)
        .filter(Book.institution_id == institution_id)
    )


@library_bp.route("/loans", methods=["GET"])
def list_loans():
    institution_id = get_current_tenant_id(required=True)
    status = request.args.get("status")
    query = _tenant_loans(institution_id)
    if status:
        query = query.filter(BookLoan.status == status.upper())

    loans = query.order_by(BookLoan.issue_date.desc()).all()
    data = []
    for l in loans:
        data.append({
            "id": str(l.id),
            "copy_id": str(l.copy_id),
            "book_title": l.copy.book.title if l.copy and l.copy.book else None,
            "barcode": l.copy.barcode if l.copy else None,
            "borrower_user_id": str(l.borrower_user_id),
            "borrower_name": f"{l.borrower.first_name} {l.borrower.last_name}".strip() if l.borrower else None,
            "issue_date": l.issue_date.isoformat(),
            "due_date": l.due_date.isoformat(),
            "return_date": l.return_date.isoformat() if l.return_date else None,
            "status": l.status,
            "fine_amount": float(l.fine.amount) if l.fine else 0.0,
            "fine_paid": l.fine.is_paid if l.fine else False,
        })
    return jsonify({"success": True, "data": data}), 200


@library_bp.route("/loans/issue", methods=["POST"])
def issue_book():
    institution_id = get_current_tenant_id(required=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    copy_id = payload.get("copy_id")
    borrower_id = payload.get("borrower_user_id")

    if not copy_id or not borrower_id:
        raise APIException("copy_id and borrower_user_id are required", status_code=400)
    copy_uuid = _uuid(copy_id, "copy_id")
    borrower_uuid = _uuid(borrower_id, "borrower_user_id")
    duration_days = _strict_int(payload.get("duration_days", 14), "duration_days", 1, MAX_LOAN_DAYS)

    # Lock the copy row so two concurrent issues cannot both see it AVAILABLE.
    copy = (
        BookCopy.query
        .join(Book, BookCopy.book_id == Book.id)
        .filter(BookCopy.id == copy_uuid, Book.institution_id == institution_id)
        .with_for_update(of=BookCopy)
        .populate_existing()
        .first()
    )
    if not copy:
        raise APIException("Book copy not found", status_code=404)

    # Borrowers must belong to this institution (platform accounts with no
    # institution are tolerated for back-office use).
    borrower = User.query.filter(
        User.id == borrower_uuid,
        or_(User.institution_id == institution_id, User.institution_id.is_(None)),
    ).first()
    if not borrower:
        raise APIException("Borrower not found", status_code=404)

    if copy.status != "AVAILABLE":
        raise APIException(f"Copy is currently '{copy.status}' and cannot be issued", status_code=400)

    issuer_id = _actor_id()
    due_date = date.today() + timedelta(days=duration_days)

    loan = BookLoan(
        copy_id=copy.id,
        borrower_user_id=borrower.id,
        issue_date=date.today(),
        due_date=due_date,
        status="ISSUED",
        issued_by=issuer_id,
    )
    copy.status = "ISSUED"
    if copy.book.available_copies > 0:
        copy.book.available_copies -= 1

    db.session.add(loan)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "loan_id": str(loan.id),
            "book_title": copy.book.title,
            "due_date": due_date.isoformat(),
            "status": loan.status,
        },
        "message": f"Book copy issued successfully until {due_date.isoformat()}."
    }), 201


@library_bp.route("/loans/<uuid:loan_id>/return", methods=["POST"])
def return_book(loan_id):
    institution_id = get_current_tenant_id(required=True)
    loan = _tenant_loans(institution_id).filter(BookLoan.id == loan_id).first()
    if not loan:
        raise APIException("Loan record not found", status_code=404)

    if loan.status == "RETURNED":
        raise APIException("Book has already been returned", status_code=400)

    today = date.today()
    loan.return_date = today
    loan.status = "RETURNED"

    # Mark copy available
    if loan.copy:
        loan.copy.status = "AVAILABLE"
        if loan.copy.book:
            loan.copy.book.available_copies += 1

    # Check overdue fine (e.g. ₹5 per day late)
    fine_obj = None
    if today > loan.due_date:
        days_late = (today - loan.due_date).days
        fine_amount = Decimal(str(days_late * 5.0))
        fine = LibraryFine(
            loan_id=loan.id,
            amount=fine_amount,
            is_paid=False,
        )
        db.session.add(fine)
        fine_obj = {"amount": float(fine_amount), "days_late": days_late}

    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "loan_id": str(loan.id),
            "status": loan.status,
            "return_date": today.isoformat(),
            "fine": fine_obj,
        },
        "message": "Book copy returned successfully."
    }), 200
