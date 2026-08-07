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
- A webcam, on the same machine as the server — see *Known limitations*

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

## Layout

| Path | What it is |
|---|---|
| `app.py` | Flask routes, all of them (being split into blueprints — MA-1) |
| `recognize_face.py` | Live recognition, tracking, liveness, MJPEG stream |
| `capture_dataset.py` | Enrolment capture, runs as a subprocess with its own OpenCV window |
| `train_model.py` | LBPH training and atomic model persistence |
| `face_preprocessing.py` | Alignment and CLAHE, shared by training and recognition |
| `config/settings.py` | All configuration, environment-driven and validated |
| `config/logging_config.py` | Rotating file + console logging |
| `eval_accuracy.py` | In-sample harness — **see the caveat below** |
| `eval_heldout_accuracy.py` | Held-out harness — **see the caveat below** |
| `tests/` | Unit tests (`pytest`) |
| `dataset/`, `trainer/` | Face images and biometric templates — **gitignored, never commit** |

## Development

```bash
ruff check .            # lint gate
pytest                  # unit tests, a few seconds
pytest -m "not slow"    # skip the tests that train a real LBPH model
```

Set `LOG_LEVEL=DEBUG` in `.env` to see per-frame recognition diagnostics. They
fire once per tracked face per frame, so expect the log to grow quickly.

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

- **Single machine only.** `/capture_face` spawns an OpenCV GUI window on the
  *server*, so enrolment only works when the browser and the server are the
  same computer.
- **Development server.** `app.run()` binds loopback; there is no WSGI server
  or reverse proxy (PO-4).
- **Security work outstanding.** Passwords are stored and compared in
  plaintext (SE-1) and case-insensitively (SE-15); several routes lack
  authentication (SE-4) and there is no role enforcement (SE-5) or CSRF
  protection (SE-7). Phase 2 addresses these. Do not deploy this on a shared
  network as it stands.

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

A written consent, retention and erasure policy is still outstanding (SE-9).
