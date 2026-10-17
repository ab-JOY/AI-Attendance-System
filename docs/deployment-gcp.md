# Deploying to Google Cloud behind Caddy (alternative - not the chosen deployment)

> **Not the chosen deployment.** A cloud VM has no camera, so attendance cannot be taken there (section 1). The chosen setup is Cloudflare Tunnel + Caddy on the classroom machine: [`deployment.md`](deployment.md). This is kept for reference.

**Topology:** one Compute Engine VM (Debian 12) running MariaDB, the app under
waitress (`serve.py`, loopback only) and Caddy in front of it. Caddy is a
standard site with a fixed domain, using automatic HTTPS (Let's Encrypt). On-demand
TLS is not used.

```
browser ──HTTPS──> Caddy :443 ──HTTP──> serve.py 127.0.0.1:8000 ──> MariaDB 127.0.0.1:3306
```

Files: [`deploy/gcp/Caddyfile`](../deploy/gcp/Caddyfile),
[`deploy/gcp/attendance.service`](../deploy/gcp/attendance.service),
[`deploy/gcp/mariadb-attendance.cnf`](../deploy/gcp/mariadb-attendance.cnf),
[`deploy/gcp/bootstrap.sh`](../deploy/gcp/bootstrap.sh), [`serve.py`](../serve.py).

---

## 1. ⚠️ Read first: a cloud VM has no camera, so attendance cannot be taken

Attendance recognition opens a camera **on the server** (`limitations.md`
§2.2, `todo.md` §7 Q2). `RecognitionSession.start()` refuses to start if
`open_camera()` finds nothing (`vision/session.py`), and a Compute Engine VM
has no camera. As things stand, the cloud copy will:

| Works | Does not work |
|---|---|
| Login, students, instructors, subjects, sections, class lists | **Start Attendance**: refused with "Cannot open camera" |
| **Browser enrolment.** The operator's webcam captures and uploads frames, and HTTPS makes `getUserMedia` available from any machine | The live camera view (`/video_feed`) |
| Reports, Excel export, attendance corrections | **Mobile self-verification** (`/api/attendance/frame`). It needs a running session, so it answers 409 |
| Retraining after enrolment | |

This is an architecture decision, and it is the user's call because the
manuscript describes how attendance is taken. The options:

- **A. Browser-camera attendance.** The attendance page captures frames in the
  instructor's browser and uploads them, the way enrolment already does. The
  existing tracking, liveness and threshold run on the server unchanged. The
  largest change, but nothing about recognition is weakened. **Recommended if
  the system must run in the cloud.**
- **B. Mobile-only attendance.** Let a session start without a server camera,
  so students verify from the app. A smaller change, but that endpoint has **no
  liveness check** and accepts LBPH distances up to **70.0** where the calibrated
  threshold is **58.0** (`web/api.py`, compare `RECOGNITION_THRESHOLD`; see
  `lessons.md` L2). Fix both before relying on it.
- **C. Stay on-premises.** Run the same Caddy + `serve.py` setup on the
  classroom machine, which has the camera, and skip Google Cloud.

Until one is chosen, the steps below give you a working HTTPS deployment of
everything in the left-hand column.

---

## 2. Prerequisites

- A Google Cloud project with billing enabled, and the
  [gcloud CLI](https://cloud.google.com/sdk/docs/install) signed in
  (`gcloud auth login`).
- **A domain name** whose DNS you control, e.g. `attendance.example.edu`.
  Without one, `<ip-with-dashes>.sslip.io` (e.g. `34-87-1-2.sslip.io`)
  resolves to that IP and works with Let's Encrypt.
- The deployment changes committed. Step 4 ships `git archive HEAD`, which
  contains committed files only. That is deliberate: it also means
  `dataset/` and `trainer/` structurally cannot travel with it.

---

## 3. Create the VM

```bash
gcloud config set project YOUR_PROJECT_ID

REGION=asia-southeast1      # pick the region nearest the school - see section 6
ZONE=asia-southeast1-b

gcloud compute addresses create attendance-ip --region=$REGION
gcloud compute addresses describe attendance-ip --region=$REGION --format='value(address)'

gcloud compute instances create attendance \
    --zone=$ZONE \
    --machine-type=e2-medium \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=20GB \
    --address=attendance-ip \
    --tags=attendance-web

gcloud compute firewall-rules create attendance-allow-web \
    --allow=tcp:80,tcp:443 --target-tags=attendance-web
```

- **Debian 12, not Ubuntu 24.04.** The app needs Python 3.11 (mediapipe 0.10.14
  has no 3.12 wheels). Debian 12 ships 3.11, and `bootstrap.sh` refuses any
  other version.
- **e2-medium (4 GB) is a starting size, not a measured one.** Nothing has been
  measured on Linux. Port 80 must stay open: Let's Encrypt validates over it,
  and Caddy redirects it to HTTPS.

Now point the domain's **A record** at the address printed above, and wait
until `nslookup your-domain` returns it. Caddy cannot get a certificate before
that.

---

## 4. Ship the code and run the bootstrap

From the project folder on your machine (PowerShell or Git Bash):

```bash
git archive --format=tar.gz -o attendance.tar.gz HEAD
gcloud compute scp attendance.tar.gz attendance:~ --zone=$ZONE
gcloud compute ssh attendance --zone=$ZONE
```

On the VM:

```bash
sudo mkdir -p /opt/attendance
sudo tar -xzf ~/attendance.tar.gz -C /opt/attendance
sudo bash /opt/attendance/deploy/gcp/bootstrap.sh attendance.example.edu
```

The script installs the packages, creates a `attendance` service account and
a database user with a generated password, and writes `/opt/attendance/.env`
(mode 600) with `FLASK_HOST=127.0.0.1`, `PROXY_HOPS=1` and
`SESSION_COOKIE_SECURE=true`. It then creates the schema and starts both
services.

**It prints a one-time admin password.** `setup_db.py` seeds `admin`/`admin`,
which anyone on the internet could try, so the script replaces it straight
away. Open `https://your-domain`, log in with it, and set your own password
when prompted.

---

## 5. Updating

```bash
# on your machine
git archive --format=tar.gz -o attendance.tar.gz HEAD
gcloud compute scp attendance.tar.gz attendance:~ --zone=$ZONE

# on the VM
sudo tar -xzf ~/attendance.tar.gz -C /opt/attendance
sudo bash /opt/attendance/deploy/gcp/bootstrap.sh attendance.example.edu
```

Re-running keeps `.env`, the database and the admin password. Migrations apply
on start-up (`AUTO_MIGRATE=true`, which is safe here because there is one process).

---

## 6. ⚠️ Students' face data on a cloud provider

`dataset/` (face images) and `trainer/` (templates) are sensitive personal
information under RA 10173 (`docs/data_privacy.md`). This setup **does not
copy them**. The VM starts empty, and students enrolled there are captured
there. Before any student is enrolled on the cloud copy, decide:

- whether the consent students gave covers storage with a cloud provider;
- the region. A region outside the Philippines is a cross-border transfer;
- whether to copy the existing `dataset/` over (`gcloud compute scp -r`), or
  re-enrol on the VM.

Then update `docs/data_privacy.md` to match. Its access-control and encryption
sections were written for one lab machine. A Compute Engine disk is encrypted
at rest by Google, but the files are still readable by anyone with SSH access
to the VM.

---

## 7. Backups

```bash
sudo mariadb-dump attendancesystem_db | gzip > ~/db-$(date +%F).sql.gz
gcloud compute disks snapshot attendance --zone=$ZONE   # whole disk, incl. dataset/
```

---

## 8. Troubleshooting

| Symptom | Look at |
|---|---|
| Browser shows a certificate error or no site | `journalctl -u caddy -e`. Usually DNS not yet pointing at the VM, or port 80 closed |
| 502 Bad Gateway | `journalctl -u attendance -e`. `serve.py` exits 1 and says why when the database is down or a migration fails |
| Every user locked out after a few bad passwords | `PROXY_HOPS` is not `1` in `.env` (`serve.py` warns about this at start-up) |
| "Cannot open camera" on Start Attendance | Expected. See section 1 |
