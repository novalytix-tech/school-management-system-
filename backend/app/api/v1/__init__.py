from flask import Blueprint
from app.api.v1.system import system_bp
from app.api.v1.auth import auth_bp
from app.api.v1.test_routes import test_bp
from app.api.v1.students import students_bp
from app.api.v1.guardians import guardians_bp
from app.api.v1.academics import academics_bp
from app.api.v1.admissions import admissions_bp
from app.api.v1.attendance import attendance_bp
from app.api.v1.finance import finance_bp
from app.api.v1.library import library_bp
from app.api.v1.transport import transport_bp
from app.api.v1.communications import communications_bp
from app.api.v1.examinations import examinations_bp
from app.api.v1.question_papers import question_papers_bp
from app.api.v1.staff import staff_bp
from app.api.v1.institutions import institutions_bp
from app.api.v1.sync import sync_bp
from app.api.v1.sync_records import sync_records_bp
from app.api.v1.users import users_bp
from app.api.v1.payments import payments_bp

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

api_v1_bp.register_blueprint(system_bp)
api_v1_bp.register_blueprint(auth_bp)
api_v1_bp.register_blueprint(test_bp)
api_v1_bp.register_blueprint(students_bp)
api_v1_bp.register_blueprint(guardians_bp)
api_v1_bp.register_blueprint(academics_bp)
api_v1_bp.register_blueprint(admissions_bp)
api_v1_bp.register_blueprint(attendance_bp)
api_v1_bp.register_blueprint(finance_bp)
api_v1_bp.register_blueprint(library_bp)
api_v1_bp.register_blueprint(transport_bp)
api_v1_bp.register_blueprint(communications_bp)
api_v1_bp.register_blueprint(examinations_bp)
api_v1_bp.register_blueprint(question_papers_bp)
api_v1_bp.register_blueprint(staff_bp)
api_v1_bp.register_blueprint(institutions_bp)
api_v1_bp.register_blueprint(sync_bp)
api_v1_bp.register_blueprint(sync_records_bp)
api_v1_bp.register_blueprint(users_bp)
api_v1_bp.register_blueprint(payments_bp)


# ---------------------------------------------------------------------------
# Secure-by-default access control for every /api/v1 route.
#
# Individual blueprints historically shipped without @jwt_required, which left
# student, finance, attendance, sync, etc. readable and writable anonymously.
# Rather than rely on each route remembering a decorator, the parent blueprint
# authenticates every request; only endpoints listed in PUBLIC_ENDPOINTS are
# reachable without a token, and ENDPOINT_ROLES narrows who may call each
# route (super_admin always passes, matching role_required).
# ---------------------------------------------------------------------------
from flask import current_app, request  # noqa: E402
from flask_jwt_extended import get_jwt, verify_jwt_in_request  # noqa: E402
from app.utils.errors import APIException  # noqa: E402

PUBLIC_ENDPOINTS = {
    "api_v1.system.health_check",
    "api_v1.auth.login",
    "api_v1.auth.refresh",
    "api_v1.auth.forgot_password",
    "api_v1.auth.reset_password",
    "api_v1.auth.request_otp",
    "api_v1.auth.verify_otp",
    "api_v1.auth.reset_password_with_otp",
    # Public website admission forms.
    "api_v1.admissions.create_inquiry",
    "api_v1.admissions.submit_application",
}

ADMIN = ("school_admin",)
STAFF = ("school_admin", "teacher")

ENDPOINT_ROLES = {
    # Students & guardians
    "api_v1.students.list_students": STAFF,
    "api_v1.students.create_student": ADMIN,
    "api_v1.students.update_student": ADMIN,
    "api_v1.students.delete_student": ADMIN,
    "api_v1.students.bulk_import_students": ADMIN,
    "api_v1.students.get_student_history": STAFF,
    "api_v1.guardians.list_guardians": STAFF,
    "api_v1.sync.post_store": ADMIN,
    # Academics
    "api_v1.academics.create_class": ADMIN,
    "api_v1.academics.create_session": ADMIN,
    "api_v1.academics.create_subject": ADMIN,
    # Admissions back office
    "api_v1.admissions.list_applications": ADMIN,
    "api_v1.admissions.update_application_status": ADMIN,
    "api_v1.admissions.list_inquiries": ADMIN,
    # Examinations: results list every student's marks school-wide.
    "api_v1.examinations.list_results": STAFF,
    # Attendance
    "api_v1.attendance.record_student_attendance_batch": STAFF,
    "api_v1.attendance.get_staff_attendance": ADMIN,
    "api_v1.attendance.get_student_attendance": STAFF,
    # Communications
    "api_v1.communications.create_notice": STAFF,
    "api_v1.communications.send_credentials": ADMIN,
    # Finance
    "api_v1.finance.create_fee_structure": ADMIN,
    "api_v1.finance.list_invoices": ADMIN,
    "api_v1.finance.get_invoice": ADMIN,
    "api_v1.finance.get_finance_summary": ADMIN,
    "api_v1.finance.record_invoice_payment": ADMIN,
    # Library
    "api_v1.library.create_book": STAFF,
    "api_v1.library.issue_book": STAFF,
    "api_v1.library.return_book": STAFF,
    "api_v1.library.list_loans": STAFF,
    # Question papers
    "api_v1.question_papers.list_question_papers": STAFF,
    "api_v1.question_papers.create_question_paper": STAFF,
    "api_v1.question_papers.get_question_paper": STAFF,
    "api_v1.question_papers.transition_question_paper_status": STAFF,
    # Staff & institution administration
    "api_v1.staff.list_staff": STAFF,
    "api_v1.staff.list_leaves": STAFF,
    "api_v1.staff.take_leave_action": ADMIN,
    "api_v1.institutions.update_institution_settings": ADMIN,
}


@api_v1_bp.before_request
def _enforce_authentication():
    if request.method == "OPTIONS" or not request.endpoint or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    # Without a database every data endpoint answers 503 by itself and there is
    # nothing to protect; auth routes keep their own decorators.
    if not current_app.config.get("DATABASE_ENABLED") and not request.endpoint.startswith("api_v1.auth."):
        return None
    verify_jwt_in_request()

    allowed = ENDPOINT_ROLES.get(request.endpoint)
    if allowed:
        roles = set(get_jwt().get("roles", []))
        if "super_admin" not in roles and not roles.intersection(allowed):
            raise APIException(
                message="You do not have the required role to access this resource.",
                status_code=403,
                error_code="FORBIDDEN_ROLE",
            )
    return None
