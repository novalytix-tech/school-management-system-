# Task 7 — Feature Health Check & Error Surfacing

_Status report. Generated 2026-11-19. Verified against the local test database
(`school_erp_test`, PostgreSQL 18) and by code audit — **no writes were made to
the shared production database.**_

## 1. Error surfacing (fixed)

Several services performed a fire-and-forget backend call and **ignored the
result** — and because `httpClient` never throws (it returns `{ success, error }`),
the `.catch(console.debug)` guards almost never ran, so a failed server sync was
invisible to the user. These now surface a clear error toast (de-duplicated to
avoid storms) via a small bus (`services/notifyBus.ts` → `SYNC_ERROR_EVENT` →
`ERPContext` listener → toast):

| Site | Was | Now |
|---|---|---|
| `studentService.create` | swallowed (`console.debug`) | toast "Student saved locally only" on sync failure |
| `studentService.update` | swallowed | toast "Changes saved locally only" |
| `studentService.delete` (Task 1) | no backend call | awaits server, surfaces failure, keeps row on error |
| `feeService.recordPayment` | swallowed | toast "Payment recorded locally only" |
| `storage.set` → `/sync/store` | swallowed | toast "Changes not synced to server" (deduped) |

When the backend is healthy these never fire; they appear only on an actual sync
failure — which is exactly the signal that was previously hidden.

## 2. Automated feature health (test suite)

Full DB-backed suite against `school_erp_test`:
**123 passed, 9 failed** (`pytest tests -q` with `TEST_DATABASE_URL` set).
Default `pytest tests -q` (DB-backed opt-in, so skipped) stays green: 40 passed / ~92 skipped.

| Module / feature area | Coverage | Status |
|---|---|---|
| Auth: login, refresh rotation, reuse detection, logout | test_auth | ⚠️ see §3 (ordering) |
| Students: list/get/create/update/delete | test_api_modules, test_ghost_login | ✅ pass |
| Ghost-login prevention (deleted student can't log in) | test_ghost_login | ✅ pass |
| Class+Section enforcement on create/import | test_relationships | ✅ pass |
| Guardian list | test_relationships | ✅ pass |
| SCD2 student history | test_scd2_history | ✅ pass |
| Bulk import (summary, errors, section_id, warnings) | test_relationships, test_api_modules | ✅ pass |
| Academics (sessions, classes, subjects) | test_api_modules, test_workflows_e2e | ✅ pass |
| Attendance | test_workflows_e2e | ✅ pass |
| Examinations & grading | test_workflows_e2e | ✅ pass |
| Fees & ledger | test_workflows_e2e | ✅ pass |
| Library circulation & fines | test_workflows_e2e | ✅ pass |
| Transport routing | test_workflows_e2e | ✅ pass |
| Admissions → enrollment | test_workflows_e2e | ✅ pass |
| Promotion batch | test_workflows_e2e | ✅ pass |
| Leave request/approval | test_workflows_e2e | ✅ pass |
| Question paper review/versioning | test_workflows_e2e | ✅ pass |
| Supplier procurement / PO | test_workflows_e2e | ✅ pass |
| RBAC (role/permission) | test_rbac | ⚠️ see §3 |
| Multi-tenant isolation | test_multi_tenant_isolation | ⚠️ see §3 |
| DB-disconnected mode & route registry | test_database_disconnected | ✅ pass |
| Seed idempotency & personas | test_seed, test_models, test_constraints | ✅ pass |

Role coverage: the e2e workflows exercise School Admin, Teacher, Student, Parent
and Supplier paths; RBAC/isolation tests cover Super Admin and cross-tenant
boundaries. Class Teacher is a Teacher sub-case (section `class_teacher_id`).

## 3. Flagged (pre-existing, not caused by Tasks 1–7)

The 9 failures appear **only when the whole DB-backed suite runs together**;
each passes in isolation. They are test-isolation/ordering issues on the shared
seeded DB (tests mutate shared rows and don't always restore), plus one
seed/alias quirk — **not feature regressions**:

- `test_auth`: `test_inactive_account` (login resolves the `orders@…` → `vendor@…`
  alias to a still-active user), `test_access_token_validation`, `refresh_token_rotation`,
  `refresh_token_reuse_detection`, `logout`, `logout_all` ("Login failed for principal"
  — earlier test left shared state dirty).
- `test_rbac`: `test_parent_child_access_restrictions`, `test_teacher_class_restrictions`.
- `test_multi_tenant_isolation`: `test_parent_child_access_boundary`.

**Recommended follow-up (test hardening, separate from feature work):** wrap
DB-backed tests in a per-test transaction+rollback (or reset the seed per test)
so they stop leaking state into each other; fix the `orders@` alias so
deactivating the supplier actually blocks its login.

## 4. Other known issues (surfaced, for the backlog)

- **Open endpoints:** `students`/`sync` REST routes have no auth decorators — any
  caller reaching the API can read/write. Needs `@jwt_required` + RBAC (security).
- **Deep-link crash:** loading a protected route while logged out throws
  `useERP must be used within an ERPProvider` (provider/route ordering).
- **SCD2 prod migration pending:** `774db7963eda` (student_history) is verified
  locally but not applied to production; history recording no-ops on live until it runs.
- **Backfill pending:** 124 students without enrollment, 0 guardian links, most
  sections without a class teacher (needs a data source — Task 2).

## 5. Deferred UI (ready on the backend)

- Guardian list page (`GET /guardians`).
- Per-student History / Change Log view (`GET /students/<id>/history`).
