# MASTER PROMPT — SMS ERP (School Management System) · Supabase + Flask

> Paste this whole file to the coding agent as the task brief. It is self-contained and
> reflects the **verified current state** of the repo and the live database (inspected directly).

---

## 0. Verified project context (read first)

**Stack**
- **Frontend:** React 19 + TypeScript + Vite + React Router 7; state in `ERPContext`; UI with `lucide-react` / `recharts` / `leaflet`. HTTP via `frontend/src/services/api/httpClient.ts` (Bearer JWT + refresh, base `/api/v1`). Tests: Vitest.
- **Backend:** Python 3.12 Flask REST API, SQLAlchemy 2.0, Alembic, JWT (`flask-jwt-extended`), SocketIO available, 16+ blueprints. Tests: pytest. `psycopg2-binary` installed.

**Database — ALREADY LIVE on Supabase (do not re-provision)**
- `backend/.env` has `DATABASE_ENABLED=true` and a working **Supabase Mumbai (ap-south-1) session-pooler** `DATABASE_URL`. Connection verified: **PostgreSQL 17.12, 90 tables.**
- Live row counts: **1 institution, 152 users, 143 students**, 7 class_levels, 7 sections, 2 staff_profiles, 10 subjects, 24 class_subjects.
- ⚠️ **This DB is shared with the Render production deployment** (per the `.env` note). Treat all writes as production writes.
- ⚠️ **Alembic drift:** the live DB is at revision `341123d7b3c5`, but the repo's `migrations/versions/` contains only the initial `7b6c7bd1c318`. The repo cannot currently reproduce the live schema. Reconcile before generating new migrations (pull/author the missing migration files, or `alembic revision --autogenerate` against the live schema and reconcile).
- ⚠️ **Relational data is sparse/broken:** `student_parents = 0` (NO guardian links exist), `student_enrollments = 20` (only 20 of 143 students have a class/section — 123 are unmapped), `fee_invoices = 0`.
- ✅ **Soft-delete columns already exist:** `users.is_deleted`, `users.is_active`, `users.is_verified`, `users.avatar_url`; `students.is_deleted`, `students.status`; `sections.class_teacher_id`, `sections.is_deleted`. No schema change is needed for the P0 fix.

**How data actually flows today (important)**
- The frontend runs on a **localStorage mirror** (`nts_novalytix_school_erp_store_v1`) via `frontend/src/services/storage.ts` + the `*Service.ts` layer.
- On boot it hydrates from **`GET /api/v1/sync/store`** (`backend/app/api/v1/sync.py`), which serializes the DB into the frontend store **with heavy fallback-guessing** (unmapped students get defaulted class/parent/photo values).
- Write-back is **inconsistent**: `studentService.create` and `.update` call `POST/PUT /students`; **`studentService.delete` calls NOTHING** (localStorage only); bulk import calls `POST /students/bulk-import`; and `POST /sync/store` only **inserts new students** (no update, no delete).

**Scope decision:** Keep the React → Flask → Supabase architecture. The real work is **closing the write-path gaps and enforcing data integrity**, not re-provisioning. Where "database" appears below it means the live Supabase Postgres via Flask.

## Global working rules (apply to every task)

1. **Production safety.** The Supabase DB is shared with production. Do **not** test destructive logic against it. Use a **separate test database** (the local Postgres URL commented in `backend/.env`, or a throwaway Supabase project / branch) via `TEST_DATABASE_URL`, and only apply to production once verified and approved.
2. **Reconcile Alembic first** (see §0) before any schema change, so migrations match the live DB.
3. **Self-verify.** Verify behavior against a running (test) DB and in the browser preview; show proof (screenshots / network / SQL). Don't ask the user to test manually.
4. **Don't break what works.** Keep suites green: `cd frontend && npm run test && npm run build && npm run lint` and `cd backend && python -m pytest tests -q`. Add a regression test for every bug fixed.
5. **Surface failures, never hide them.** Every failing feature shows a clear UI error (toast/banner) — no blank screens or silent `console.debug` swallowing (note: `studentService` currently swallows backend sync errors with `.catch(console.debug)` — surface those).
6. **Protect secrets.** `backend/.env` already contains live DB, Razorpay and SMS credentials. Never commit them, never print them, never ship server secrets to the browser.
7. **Report, don't claim.** Per task: what changed, how verified, metrics/screenshots, anything deferred.

---

## TASK 1 — [P0 / CRITICAL] Deleted student can still log in (ghost login + wrong photo)

**Reported symptom:** after deleting a student from admin, logging in as that student still works; a generic photo shows and only the name changes.

**Confirmed root cause (two compounding bugs):**
- `frontend/src/services/studentService.ts` → `delete()` (~line 209) removes the student from localStorage **but makes no backend call** (unlike `create`/`update`). The delete never reaches Supabase.
- `backend/app/api/v1/students.py` → `delete_student()` (~line 654) only sets `student.status = "WITHDRAWN"`; it does **not** deactivate the linked `User`. And `backend/app/services/auth_service.py` → `authenticate_user()` (~line 116) checks only `user.is_active`/`is_verified` — never `student.status`, `students.is_deleted`, or `users.is_deleted`. So the `users` row stays active and login succeeds.
- "Same picture" = `studentService.getStudentPhoto()` (~line 57) returns a **shared gender-default SVG** when no real photo exists; the name comes from the real `User`.

**Required fix:**
- **Backend `delete_student`:** also set `student.is_deleted = True` and **deactivate the linked user** (`student.user.is_active = False`, `student.user.is_deleted = True`). Cascade/withdraw related rows (enrollments → INACTIVE). Wrap in one transaction.
- **Backend `authenticate_user`:** reject login when the user is `is_deleted`, the linked student is `is_deleted`/`WITHDRAWN`, or exclude such users from the lookup — with a clear "account not found / access revoked" message.
- **Backend reads** (`list_students`, `get_student`, `sync/store`): filter out `is_deleted`/withdrawn so deleted students stop reappearing.
- **Frontend `studentService.delete`:** call `httpClient.delete('/students/{id}')` (the DB UUID from `sync/store`) and only remove locally on success; surface failures. Handle locally-created-but-unsynced ids gracefully.
- **Profile resolution:** resolve post-login profile strictly by stable id; a deleted record resolves to nothing. Use real `avatar_url` where present instead of a shared default.

**Acceptance:** delete a student → login as them is **refused**; they disappear from all lists and don't return after refresh/hydrate; a regression test reproduces the original bug and passes. (Test against a non-production DB.)

---

## TASK 2 — Relationships & integrity: Student → Class+Section → Class Teacher, + backfill

**Current reality:** `student_parents = 0`, 123/143 students have no enrollment, `sections.class_teacher_id` mostly unset. Structure exists; data and enforcement don't.

**Requirement:**
- **Enforce** that every student has an active `StudentEnrollment` → `Section` → `ClassLevel`, and that the Section has a `class_teacher_id`. Block/clearly warn on create, edit, and import when missing (currently `create_student` silently skips enrollment if no `section_id`).
- **Backfill** the 123 unmapped students and the empty `student_parents` links (one-time, reviewed script run against the test DB first).
- Confirm/强化 FK + `NOT NULL` constraints so orphans are impossible (add a reconciled Alembic migration if any are missing). Define the rule for deleting a class/teacher that still has students (block / reassign / cascade) and document it.
- Deliver four filterable, searchable, cross-linked lists served by Flask: **Student List, Teachers List, Guardian List, Class/Section List** (showing the mapped Class Teacher). Guardian List is currently impossible (no `student_parents`) — fix the data + endpoint.

**Acceptance:** cannot create/import a student without a valid Class+Section that has a Class Teacher; all four lists populate correctly and cross-link; no orphaned references in the DB.

---

## TASK 3 — Slowly Changing Dimension (SCD) Type 2 history

**Requirement:** SCD Type 2 for key dimensions (Student; and where sensible enrollment/class assignment, Guardian), in **Postgres + frontend**. Note `audit_logs` and `student_promotions` tables already exist and can inform this.
- Each version: `valid_from`, `valid_to` (null = current), `is_current`, surrogate version key separate from the business key (admission number). Implement via history columns/tables + triggers or Flask service logic (document the choice); add a reconciled Alembic migration.
- Updates **close** the current version and insert a new current one; history is never destroyed. Deletes stay **soft** (reinforces Task 1).
- **Frontend:** per-student "History / Change Log" view; default lists show only `is_current`.

**Acceptance:** editing a student creates a new current version while the old remains queryable with correct dates; a history view renders the timeline.

---

## TASK 4 — Import: summary, error reporting, and performance

**Current reality:** there are **two** paths — a fast server-side `POST /students/bulk-import` (`backend/app/api/v1/students.py`, already batched: in-memory validation, parallel password hashing, one INSERT per table, single COMMIT) **and** a legacy localStorage path in `frontend/src/services/importService.ts` (`commitImport` writes straight to `ERPStorage`, with O(n²) `find` scans).

**Requirement:**
- Route **all** imports through `POST /students/bulk-import` → Supabase; retire/replace the localStorage `commitImport` so imports actually persist and enforce Task 2 rules (the bulk endpoint currently skips enrollment when the class/section doesn't resolve — make that a surfaced error, not a silent skip).
- Show a post-import **summary** (total / imported / skipped / failed) and **per-row errors** (the endpoint already returns `imported`, `skipped`, `errors`, and `credentials`), with the downloadable error-report CSV (already in `importService`).
- Surface the returned temporary **credentials** to the admin. Show a **progress indicator**; avoid re-render storms on large files.

**Acceptance:** a 500+ row file imports fast with progress; summary panel shows counts + per-row errors + report download; failures roll back cleanly (the endpoint already does all-or-nothing).

---

## TASK 5 — Faster login / authentication

**Context:** `authenticate_user` already eager-loads roles/permissions/institution to avoid N+1 over the cross-region link, and the engine pool is tuned in `config.py`. Build on that.

**Requirement:** trim the login → `/auth/me` → first dashboard render path (remove redundant awaits/reads; safe identity caching that doesn't reintroduce the Task 1 stale-profile bug); keep using the pooled Supabase connection; add a loading state so the UI never looks frozen. Consider Redis (`REDIS_URL`, already wired for cache/limiter/SocketIO) for shared caching.

**Acceptance:** measurably faster login (before/after numbers); no auth-correctness or Task 1 regressions.

---

## TASK 6 — Realtime

**Requirement:** live updates with no manual refresh. Backend **SocketIO is already initialized** (`app/sockets.py`, `extensions.py`, Redis message-queue ready). Prefer emitting domain events over SocketIO after writes (students/classes/enrollments/guardians), with the frontend subscribing; the client realtime layer should be transport-agnostic (SocketIO now, Supabase Realtime later if desired). Replace the current full-store re-hydrate pattern with targeted updates.

**Acceptance:** adding/deleting/importing a student, or changing a class-teacher mapping, updates the relevant lists live in the same and other open tabs, no full reload.

---

## TASK 7 — Full feature health-check with error surfacing

**Requirement:** verify every feature across all roles (Super Admin, School Admin, Teacher, Class Teacher, Student, Parent, Supplier) — now DB-backed. Note many services currently swallow backend errors (`.catch(console.debug)`) and silently fall back to localStorage, hiding failures; make failures **loud** with clear UI messages. Produce a **feature status report** (working / fixed / flagged).

**Acceptance:** a checklist of every major feature with pass/fail; all failures fixed or surfaced + listed.

---

## TASK 8 — UI: full-width toolbar + neumorphic 3D buttons with pressed-state feedback

**Requirement:**
- **Stretch the toolbar edge-to-edge (left→right)**; improve legibility (spacing, sizing, contrast, labels).
- Apply a **neumorphism / "morphism" 3D button** treatment so controls look like raised physical Windows-style tool buttons (soft dual shadows, depth).
- **Pressed/active state changes color** to mark the selected/active tool.
- Accessible (focus, contrast); works in light/dark and at mobile width.

**Acceptance:** full-width toolbar; 3D neumorphic buttons; pressing a tool visibly changes its color to mark it active. Verified with before/after screenshots in light and dark.

---

## Deliverables

1. Logical, reviewable commits (one concern each); Alembic reconciled; migrations for any schema change.
2. Passing frontend (test + build + lint) and backend (pytest) suites, with new/regression tests.
3. A short report per task: what changed, how verified, metrics/screenshots, and anything deferred.

## Priority order

**Task 1** (P0 ghost-login — no schema change needed) → **Task 2** (relationships + backfill) & **Task 3** (SCD2) → **Task 4** (import) → **Task 7** (health-check) → **Task 5** (auth speed), **Task 6** (realtime), **Task 8** (UI). Reconcile Alembic (§0) before Tasks 2/3.
