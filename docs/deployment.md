# Deploying with Cloudflare Tunnel + Caddy

**The system runs on the classroom machine, the one with the camera.**
Cloudflare publishes it at `https://attendance.your-domain` with a real
certificate. No router ports are opened and no public IP is needed: the
machine makes an outbound connection to Cloudflare, and visitors arrive
through it.

```
browser / mobile app
   │ HTTPS (Cloudflare's certificate)
   ▼
Cloudflare ══ tunnel (outbound from the classroom PC) ══▶ cloudflared
                                                            │ HTTP, loopback
                                                            ▼
                                              Caddy 127.0.0.1:8080
                                                            │
                                                            ▼
                                     serve.py 127.0.0.1:8000 ──▶ camera, MariaDB, dataset/
```

**Why this and not Google Cloud:** the camera is opened by whichever machine
runs `serve.py`, and a cloud VM has none ([`deployment-gcp.md`](deployment-gcp.md) §1).
Here everything works: attendance with the camera, the live view, mobile
verification, and browser enrolment from any machine, because the site is HTTPS.

**Caddy's role:** Cloudflare holds the certificate, so Caddy does no TLS
(automatic HTTPS is off; on-demand TLS is not used). It adds security headers,
caps upload size at the edge, and streams the live camera view without
buffering. Files: [`deploy/cloudflare/Caddyfile`](../deploy/cloudflare/Caddyfile),
[`deploy/cloudflare/install-autostart.ps1`](../deploy/cloudflare/install-autostart.ps1),
[`serve.py`](../serve.py).

**What you need:**
- A domain added to a Cloudflare account. The free plan is enough. A named tunnel publishes
  on a hostname in a domain Cloudflare manages.
- The classroom PC switched on, logged in and online during class. If it
  is off, the site is down.

---

## 1. MariaDB starts with Windows

XAMPP's MySQL does not start on its own after a restart (`lessons.md` L15),
and `serve.py` exits if it cannot reach the database. Open the **XAMPP Control
Panel as administrator** and tick the **Service** box beside MySQL. It then
starts at boot, before anyone logs on.

## 2. Configure the app

From the project folder, install the new dependency (waitress):

```powershell
.venv\Scripts\pip install -e .
```

Add or change these lines in `.env`:

```ini
FLASK_HOST=127.0.0.1
WSGI_PORT=8000
PROXY_HOPS=2
SESSION_COOKIE_SECURE=true
```

- **`FLASK_HOST=127.0.0.1` is required.** `serve.py` refuses to start with
  `PROXY_HOPS` set on any other address. Otherwise a machine on the LAN could
  connect directly, fake its address, and never trigger the login lockout.
- **`PROXY_HOPS=2`** covers Cloudflare and Caddy. With the wrong value, every visitor shares
  one lockout, so five wrong passwords from anyone lock that account for everyone.
- `SSL_CERT_FILE`/`SSL_KEY_FILE` are ignored by `serve.py`. Cloudflare holds the
  certificate now.

If the admin password is still `admin`, change it **before** step 4. Once the
tunnel is up, the login page is on the internet.

## 3. Caddy

1. Download `caddy_<version>_windows_amd64.zip` from
   <https://github.com/caddyserver/caddy/releases> (2.8.4 was used to test this
   setup). Unzip `caddy.exe` to `C:\caddy\`.
2. Copy `deploy\cloudflare\Caddyfile` to `C:\caddy\Caddyfile`.
3. In an **administrator** PowerShell (type `sc.exe` in full, because plain `sc` is
   a PowerShell alias):

   ```powershell
   C:\caddy\caddy.exe validate --config C:\caddy\Caddyfile
   sc.exe create caddy start= auto binPath= "C:\caddy\caddy.exe run --config C:\caddy\Caddyfile"
   sc.exe start caddy
   ```

## 4. Cloudflare Tunnel

1. In the Cloudflare dashboard, open **Zero Trust → Networks → Tunnels →
   Create a tunnel**. Choose **Cloudflared**, name it `attendance`, and pick **Windows**.
2. Install cloudflared as shown, then run the command the page gives you in an
   **administrator** prompt:
   `cloudflared.exe service install <token>`. It installs a Windows service that
   starts at boot.
3. Add a **public hostname**: subdomain `attendance`, your domain,
   service type **HTTP**, URL **`127.0.0.1:8080`**. Use `127.0.0.1`, not
   `localhost`, because Caddy listens on IPv4 loopback only.

## 5. Start the app at logon

In a normal PowerShell, from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\cloudflare\install-autostart.ps1
Start-ScheduledTask -TaskName AttendanceSystem
```

This is a logon task rather than a service because the camera belongs to the
logged-on user's session (see the script's header).

⚠️ **From now on, do not also run `python app.py`.** Two processes would mean two
attendance systems, each fighting the other for the camera.

## 6. Check it

- Open `https://attendance.your-domain` from a phone on mobile data, not
  school Wi-Fi, so you know it works from outside.
- **Mobile app:** Settings → server URL → `https://attendance.your-domain`.
  The old `http://192.168.1.51:5000` address no longer answers.
- Run one enrolment from a laptop and one attendance session. The live view
  through Cloudflare has **not been tested** yet. Caddy streams it, but
  Cloudflare's behaviour with a never-ending MJPEG response is unconfirmed.

**Verified on this machine (2026-10-17):** the real `serve.py` behind the real
Caddy, against the local database. Five bad logins from one visitor locked that visitor only,
and another visitor was unaffected. The request reached the app as `https`, and the start-up
guard refused `FLASK_HOST=0.0.0.0`. **Not verified:** the Cloudflare leg
itself, the autostart task, and Caddy as a Windows service.

---

## 7. Updating

Copy or pull the new code, then:

```powershell
.venv\Scripts\pip install -e .
Stop-ScheduledTask -TaskName AttendanceSystem; Start-ScheduledTask -TaskName AttendanceSystem
```

Migrations apply on start-up (`AUTO_MIGRATE=true`; there is one process).

## 8. ⚠️ Privacy: what Cloudflare sees

Face images and templates **stay on the classroom machine**. Nothing is stored
with Cloudflare. But Cloudflare decrypts traffic at its edge, so enrolment
frames, the live camera view and the registers pass through Cloudflare in
transit. That makes Cloudflare a processor under RA 10173. This is a much
smaller question than the Google Cloud option, where the data itself would have
been stored, but it belongs in `docs/data_privacy.md`, and the consent wording is your decision.

Optional: **Cloudflare Access** (Zero Trust → Access) can require a school email
before the login page is even shown. It isn't needed for the system to work.

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| Cloudflare error **1033** | cloudflared is not running: `Get-Service cloudflared` |
| **502 Bad Gateway** | Caddy (`Get-Service caddy`) or `serve.py` is not running. Check `logs\` |
| `serve.py` exits: "Cannot start: PROXY_HOPS=2 trusts X-Forwarded-For…" | `FLASK_HOST` is not `127.0.0.1` in `.env` |
| `serve.py` exits: database errors | MariaDB is not running (step 1) |
| One person's wrong passwords lock everyone out | `PROXY_HOPS` is not `2` |
| Login form answers 400 | A browser extension or privacy setting is stripping the `Referer` header. Flask-WTF requires it on HTTPS |
| "Cannot open camera" | Another program holds the camera, or the wrong camera is chosen in the attendance page's picker |
