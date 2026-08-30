# AI Attendance System

Face-recognition attendance system for a classroom setting. Flask web app,
MediaPipe FaceMesh for detection and landmarks, OpenCV LBPH for recognition,
MySQL for records.

> **Status:** under active refactoring. See [`tasks/todo.md`](tasks/todo.md)
> for the ISO/IEC 25010 audit and the phased plan, and the latest
> `tasks/handover-*.md` for current verified state. Findings carry IDs
> (`SE-15`, `PE-0`, `FS-3`) — cite them when working on one.

## Requirements

- **Python 3.11.** Not 3.12 or later: `mediapipe==0.10.14` publishes no wheels
  for them, so installation succeeds and `import mediapipe` then fails.
- MySQL server (XAMPP, WAMP, or standalone)
- A webcam. Recognition uses the one attached to the server; enrolment uses
  the operator's, through the browser — see *Known limitations*

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on POSIX

pip install -e ".[dev]"

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as SECRET_KEY

python setup_db.py
python app.py                   # http://127.0.0.1:5000
```

`SECRET_KEY` is required and has no default. The app refuses to start without
it, deliberately: the value it replaced is in git history, and a known Flask
secret key means session cookies can be forged.

### First login

`setup_db.py` seeds **`admin` / `admin`**, and that credential is published
here, so it is not a password. The account is flagged `must_change_password`
and can reach nothing but the change-password screen until it is replaced.
Sign in with it once, set a real password, and it is done.

### Upgrading a database created before Phase 2

If the `admin` or `instructors` tables already exist, they hold plaintext
passwords and have no `must_change_password` column. `CREATE TABLE IF NOT
EXISTS` will not fix either, so run the migration once:

```bash
python scripts/migrate_passwords.py --dry-run   # report what would change
python scripts/migrate_passwords.py             # apply
```

It adds the column, replaces every plaintext password with a bcrypt hash, and
flags any account still using a shipped default. Running it twice is safe.
**Hashing is one-way** — take a `mysqldump` first if any of those passwords
exist nowhere else.

### Serving over HTTPS

Only needed to enrol from **another machine's** browser. `getUserMedia` is
available in a *secure context* only — HTTPS, or `http://localhost` — so on
plain HTTP the camera call is rejected outright, with no prompt and nothing to
click through.

```bash
python scripts/make_dev_cert.py     # localhost, 127.0.0.1, this machine's LAN IP
```

Then put both paths in `.env`:

```
SSL_CERT_FILE=certs/dev-cert.pem
SSL_KEY_FILE=certs/dev-key.pem
```

Both or neither — one without the other is a deliberate startup failure, so a
mistyped path cannot leave you on HTTP believing you are on HTTPS. With them
set, the server binds every interface and the session cookie gains `Secure`
without anything else being changed.

⚠️ **A certificate with no Subject Alternative Name is refused outright**, and
no click-through overrides it. The script writes the SANs; a hand-rolled
`openssl req` usually does not. Better still, if you can install it, `mkcert`
also puts a local CA in the system trust store so there is no warning screen at
all — worth it if this is going on a projector.

⚠️ **A private key is a credential.** `certs/`, `*.pem`, `*.key` and `*.crt`
are gitignored; never commit one.

## Layout

Layered, as of Phase 5 (MA-1). `app.py` was 2,890 lines holding HTTP, SQL,
filesystem work and process management in the same function bodies.

| Path | What it is |
|---|---|
| `app.py` | The entry point: a factory call and `__main__`. 134 lines |
| `web/` | Eight blueprints — HTTP only: forms, redirects, templates, status codes |
| `services/` | Orchestration that is not about HTTP: the enrolment slot, the training job, the session lifecycle, the export |
| `repositories/` | **Every SQL statement.** Each takes a cursor and never opens one, so the caller owns the transaction |
| `infra/` | Database pool, camera reader, background jobs, uploads, dataset store, migrations |
| `vision/` | Recognition logic with no camera, no database and no model — the fast tests live here |
| `security/` | Access control, bcrypt, path validation, login throttling |
| `config/` | Settings (environment-driven, validated) and logging |
| `recognize_face.py` | Live recognition, tracking, liveness, MJPEG stream |
| `train_model.py` | LBPH training and atomic model persistence |
| `face_preprocessing.py` | Alignment and CLAHE, shared by training and recognition |
| `static/js/` | All JavaScript. Server values arrive as `data-` attributes, never interpolated into a script (US-5, US-8) |
| `templates/base.html` | The one layout every page extends |
| `migrations/` | The single source of schema truth |
| `scripts/` | Operator tooling: migrations, password migration, dev certificate, dataset folder rename |
| `eval_accuracy.py` | In-sample harness — **see the caveat below** |
| `eval_heldout_accuracy.py` | Held-out harness — **see the caveat below** |
| `tests/` | `pytest`. `tests/integration/` needs a scratch database and is opt-in |
| `dataset/`, `trainer/` | Face images and biometric templates — **gitignored, never commit** |

## Development

```bash
ruff check .            # lint gate
pytest                  # the fast suite, about 80 s
pytest -m "not slow"    # skip the tests that train a real LBPH model

# Integration tests. The target is named explicitly and must match test_*;
# pointing this at the configured database is a failure, never a skip.
INTEGRATION_DB_NAME=test_attendance_scratch pytest tests/integration/ -q
```

Set `LOG_LEVEL=DEBUG` in `.env` to see per-frame recognition diagnostics. They
fire once per tracked face per frame, so expect the log to grow quickly.

## Writing about this system?

[`docs/limitations.md`](docs/limitations.md) is the single source for
limitations, hard constraints and every measured figure, written to be handed
to a technical writer. It also lists six claims from earlier documentation that
could not be reproduced, so they are not recycled.

## Accuracy numbers: read this before quoting any

Both evaluators report 100%. Neither figure means what it appears to.

- **In-sample (`eval_accuracy.py`)** — every test image is also a training
  image, and LBPH stores one histogram per sample, so each query matches its
  own stored histogram exactly. This shows the train → save → load → predict
  pipeline works. It is not an accuracy measurement.
- **Held-out (`eval_heldout_accuracy.py`)** — all 100 images per student come
  from one continuous capture session, so the 80/20 split separates
  near-duplicate consecutive video frames. Not a generalisation estimate.
- **No impostor set**, so the false-acceptance rate — the security-critical
  metric for attendance — is unmeasured, and the 58.0 recognition threshold
  is uncalibrated.
- **N = 3.** A three-way classification problem does not extrapolate.

`docs/walkthrough.md` §4 (limitations) and §5 (correction log) have the
detail. Reporting FAR/FRR/EER against a separate-session test set is Phase 6.

## Known limitations

- **Enrolment needs a secure context.** It runs in the operator's browser via
  `getUserMedia`, which browsers expose only over HTTPS or `http://localhost`.
  Enrolling from another machine therefore needs TLS configured (see below);
  on the server itself it works as shipped. *Recognition* still opens the
  camera attached to the server, by design — that is the classroom camera.
- **Development server.** `app.run()` is the deployment: no WSGI server and no
  reverse proxy (PO-4). TLS *is* supported now (see below) and
  `SESSION_COOKIE_SECURE` follows it automatically, but the server itself is
  still Werkzeug's.
- **Liveness is defeatable by a video replay** (SE-12). The challenge is one
  of two fixed head poses, so a phone playing a recording passes it.
- **Security work still outstanding.** Phase 2 fixed authentication, roles,
  CSRF, password storage, path traversal, error disclosure, session hardening
  and login throttling. Not fixed: no encryption at rest for `dataset/` and
  `trainer/`, no consent record in the schema, no automated retention. See
  [`docs/data_privacy.md`](docs/data_privacy.md) §7.

## Data protection

`dataset/` holds face images of identifiable students and `trainer/` holds the
biometric templates derived from them. Both are **sensitive personal
information** under RA 10173 (Philippine Data Privacy Act), both are
gitignored, and neither may ever be committed — git history is permanent and
is copied to every clone.

Before committing, confirm the ignore rules are intact:

```bash
[ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP
```

[`docs/data_privacy.md`](docs/data_privacy.md) covers consent, retention,
erasure and the RA 10173 mapping. Two things there are worth knowing before
you need them:

- **Erasure takes four steps, not one.** Deleting a student removes their
  images, but their histograms stay in `trainer/trainer.yml` until a retrain,
  and in the `.bak` generation until a second one — and a pre-migration copy
  can survive in `dataset/_migrated_duplicates/`. Form C of the consent
  document carries the whole sequence as a checklist.
- **Consent is not yet recorded in software.** Until it is, collect it on
  paper before any enrolment, using
  [`docs/consent_form.md`](docs/consent_form.md) — Form A to enrol someone,
  Form B for anyone whose face is used to *test* the system, Form C to
  withdraw.
