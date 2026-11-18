"""
Delete ONLY the imported student batch (admission_number like 'ADM2...' with NO dash)
from the database. Original demo seed students ('ADM-YYYY-...') are preserved.

Usage:
    python _delete_imported_students.py            # DRY RUN (no changes)
    python _delete_imported_students.py --execute   # actually delete
"""
import os, sys
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app
from app.extensions import db
from sqlalchemy import text, bindparam

EXECUTE = "--execute" in sys.argv

# Imported batch = ADM followed directly by a digit (no dash). Seed = 'ADM-...'.
WHERE = "admission_number LIKE 'ADM2%' AND admission_number NOT LIKE 'ADM-%'"

app = create_app('development')
with app.app_context():
    rows = db.session.execute(text(
        f"SELECT id, user_id, admission_number FROM students WHERE {WHERE} ORDER BY admission_number"
    )).fetchall()

    student_ids = [r.id for r in rows]
    user_ids = [r.user_id for r in rows if r.user_id]
    seed_count = db.session.execute(text("SELECT COUNT(*) FROM students WHERE admission_number LIKE 'ADM-%'")).scalar()

    print(f"MODE = {'EXECUTE (will delete)' if EXECUTE else 'DRY RUN (no changes)'}")
    print(f"Imported students to delete : {len(student_ids)}")
    print(f"Linked login accounts        : {len(user_ids)}")
    print(f"Seed students to KEEP        : {seed_count}")

    if not student_ids:
        print("Nothing to delete. Exiting.")
        sys.exit(0)

    def count(sql):
        stmt = text(sql).bindparams(bindparam("ids", expanding=True))
        return db.session.execute(stmt, {"ids": student_ids}).scalar()

    inv = count("SELECT COUNT(*) FROM fee_invoices WHERE student_id IN :ids")
    print(f"Fee invoices for these students (RESTRICT, must clear first): {inv}")

    print("\nFirst 10 to be deleted:")
    for r in rows[:10]:
        print(f"   {r.admission_number}  (student {r.id})")

    if not EXECUTE:
        print("\nDRY RUN only — re-run with --execute to perform the deletion.")
        sys.exit(0)

    # ---- EXECUTE ----
    try:
        # 1) Clear RESTRICT-blocking finance rows for these students, if any.
        if inv:
            inv_del = text(
                "DELETE FROM fee_payments WHERE invoice_id IN "
                "(SELECT id FROM fee_invoices WHERE student_id IN :ids)"
            ).bindparams(bindparam("ids", expanding=True))
            db.session.execute(inv_del, {"ids": student_ids})
            item_del = text(
                "DELETE FROM fee_invoice_items WHERE invoice_id IN "
                "(SELECT id FROM fee_invoices WHERE student_id IN :ids)"
            ).bindparams(bindparam("ids", expanding=True))
            try:
                db.session.execute(item_del, {"ids": student_ids})
            except Exception:
                db.session.rollback()  # table/col may differ; ignore and continue
            fi_del = text("DELETE FROM fee_invoices WHERE student_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            db.session.execute(fi_del, {"ids": student_ids})

        # 2) Delete the students. DB-level ON DELETE CASCADE handles enrollments,
        #    student_parents, promotions, assignment submissions, awards, transport.
        s_del = text("DELETE FROM students WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        res = db.session.execute(s_del, {"ids": student_ids})
        db.session.commit()
        print(f"\nDeleted students: {res.rowcount}")
    except Exception as e:
        db.session.rollback()
        print(f"\nERROR deleting students, rolled back: {e}")
        sys.exit(1)

    # 3) Best-effort cleanup of the now-orphaned login accounts.
    deleted_users = 0
    if user_ids:
        try:
            u_del = text("DELETE FROM users WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            )
            res = db.session.execute(u_del, {"ids": user_ids})
            db.session.commit()
            deleted_users = res.rowcount
            print(f"Deleted linked login accounts: {deleted_users}")
        except Exception as e:
            db.session.rollback()
            print(f"Login-account cleanup skipped (kept as orphans, harmless): {e}")

    remaining = db.session.execute(text("SELECT COUNT(*) FROM students")).scalar()
    seed_after = db.session.execute(text("SELECT COUNT(*) FROM students WHERE admission_number LIKE 'ADM-%'")).scalar()
    print(f"\nStudents remaining in DB: {remaining}  (seed preserved: {seed_after})")
