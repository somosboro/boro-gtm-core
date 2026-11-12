# Commercial engine runbook

The engine runs on the BoRo VPS in `/opt/boro_gtm` (compose project `boro_gtm`).
Every command below runs on the VPS:

```bash
ssh -i ~/.ssh/getboro_deploy boro-vps
cd /opt/boro_gtm
```

`E` below stands for `docker compose exec -T worker gtm engine`.

## Daily

| Question | Command |
| --- | --- |
| Pipeline summary (today, pool by state, funnel by lane) | `E summary` |
| READY leads, best first | `E ready` |
| Today's sends (or shadow renders) | `E sends` |
| Replies, with escalations flagged | `E replies` |
| Follow-ups due in the next 7 days | `E followups` |
| Opportunities and deals | `E opportunities` |
| Funnel by country / variant / segment | `E funnel --by country` |
| What sourcing tried and why it fell short | `E sources` |
| Worker health | `E health` |
| Failed tasks, sources and messages | `E failed` |
| Logs | `docker compose logs --tail 200 worker` |

## Moving a deal (only a person does this)

```bash
E advance <target_id> CONVERSATION --note "call booked 2026-11-10"
E advance <target_id> OPPORTUNITY
E advance <target_id> PROPOSAL --amount 4800 --currency USD
E advance <target_id> WON --amount 4800 --currency USD
E advance <target_id> LOST --note "went with an in-house hire"
```

## Controls

```bash
E pause "manual pause: checking deliverability"   # stops all real sends
E resume
E suppress someone@example.com                      # or a whole domain: E suppress example.com
E run source                                        # any task, now: ingest reconcile guard source enrich plan send close
E import-pool /opt/boro_gtm/imports/BoRo_Week1_Commercial_Pool_100.xlsx
```

Copy an import file in first: `docker compose cp ./file.xlsx worker:/tmp/` and
import `/tmp/file.xlsx`.

## Connecting the corporate Gmail (one-time, human step)

Scopes: `gmail.send` and `gmail.readonly`, nothing else. The token file lives
only in `/opt/boro_gtm/secrets/gmail.json` (root, mode 600, outside git). The
engine refuses a file readable by others, and refuses any token that does not
open `abourgeoisroa@somosboro.com`.

`gtm engine gmail-connect` runs OAuth for an installed app (RFC 8252) with PKCE
S256 (RFC 7636): it opens the browser, a one-shot listener on `127.0.0.1` and an
ephemeral port receives the redirect, `state` is checked before the code is
read, and the code is exchanged with the verifier. The credentials file is
written only if the grant carries exactly the two scopes and opens the
corporate mailbox. No code, verifier, secret or token is ever printed, logged
or typed on a command line; the command refuses to write inside a git
repository.

1. **Google Cloud** (signed in as a somosboro.com Workspace admin):
   * Create (or pick) a project → *APIs & Services → Library* → enable **Gmail API**.
   * *OAuth consent screen* → User type **Internal** (an External app in testing
     mode issues refresh tokens that expire after 7 days).
   * *Credentials → Create credentials → OAuth client ID* → type **Desktop app**
     → download the JSON (`client_secret_….json`). A *Web application* client is
     refused.
2. **On the Windows PC** (PowerShell), once, in the repository:
   ```powershell
   cd D:\BoRo\Projects\boro-gtm-core
   python -m venv .venv
   .venv\Scripts\python -m pip install -e .
   ```
3. Connect (the browser opens; sign in as `abourgeoisroa@somosboro.com` and approve):
   ```powershell
   $env:PYTHONIOENCODING = "utf-8"
   .venv\Scripts\gtm engine gmail-connect --client-secret-file "$HOME\Downloads\client_secret_<id>.json"
   ```
   It prints the mailbox and the scopes and writes `%USERPROFILE%\.boro-gtm\gmail.json`.
   Restrict that folder to your Windows user:
   ```powershell
   icacls "$HOME\.boro-gtm" /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
   ```
4. Copy the file over SSH into the root-only `secrets` folder (mode 700, so the
   file is never readable by anyone else, even before the `chmod`), then restart:
   ```powershell
   scp "$HOME\.boro-gtm\gmail.json" boro-vps:/opt/boro_gtm/secrets/gmail.json
   ssh boro-vps "chown root:root /opt/boro_gtm/secrets/gmail.json && chmod 600 /opt/boro_gtm/secrets/gmail.json && cd /opt/boro_gtm && docker compose restart worker sourcer"
   ssh boro-vps "cd /opt/boro_gtm && docker compose exec -T worker gtm engine gmail-check"
   ```
   `gmail-check` must show `abourgeoisroa@somosboro.com` and `"token_refresh": "ok"`.
5. Remove the local copies: `Remove-Item "$HOME\.boro-gtm\gmail.json"`; keep
   the `client_secret_….json` in BoRo's password manager and delete it from
   Downloads.
6. On the VPS, backfill and reconcile:
   ```bash
   E ingest-history --since 2026-06-01
   E parity --day 2026-11-03 --record
   E import-status "import:BoRo_Week1_Commercial_Pool_100"
   E cutover-report
   ```
   The backfill stores every sent and received message once (drafts never),
   turns past bounces and opt-outs into suppressions, and adopts any running
   ChatGPT sequence (CE-ADR-019). Re-running it is safe.

To revoke: <https://myaccount.google.com/permissions> → remove the app, then
delete `/opt/boro_gtm/secrets/gmail.json` and restart the worker.

## Imports

```bash
docker compose cp imports/<file>.xlsx worker:/tmp/
E import-pool /tmp/<file>.xlsx          # idempotent; prints a row-by-row report
E import-status "import:<file stem>"    # where every row stands now
E manual                                # platform applications a person must make
```

## Cutover checks

```bash
E verify                                # bounce/opt-out/follow-ups on prod, rolled back
E parity --day YYYY-MM-DD --record      # each shadow business day, after the backfill
E audit-sample --n 30 > /tmp/audit.json # review; then record the verdicts:
E record-audit /tmp/verdicts.json       # {"sample":30,"metrics":{...},"defects":[...],"by":"..."}
E cutover-report                        # exit code 2 until every blocking check passes
```

## Deploying a new version

From the repository on your Mac, at the commit to deploy:

```bash
SHA=$(git rev-parse --short HEAD)
git archive --format=tar HEAD | ssh -i ~/.ssh/getboro_deploy boro-vps \
  "rm -rf /opt/boro_gtm/src/* && tar -x -C /opt/boro_gtm/src && \
   cp /opt/boro_gtm/src/deploy/vps/docker-compose.yml /opt/boro_gtm/ && \
   install -m 700 /opt/boro_gtm/src/deploy/vps/backup.sh /opt/boro_gtm/src/deploy/vps/restore-test.sh /opt/boro_gtm/ && \
   cd /opt/boro_gtm && nice -n 10 docker build -q -t boro-gtm-core:$SHA -f src/services/api/Dockerfile src && \
   sed -i 's/^GTM_IMAGE_TAG=.*/GTM_IMAGE_TAG=$SHA/' .env && ./backup.sh && docker compose up -d"
```

Migrations run automatically (`migrate` service) before the API and worker start.

## Backups

* Nightly at 03:30 (server time): `/opt/boro_gtm/backup.sh`, 14 days kept in
  `/opt/boro_gtm/backups`, outcome recorded for the cutover report.
* Prove a restore any time: `/opt/boro_gtm/restore-test.sh` (also recorded).
* **Off-host (required before live sending, CE-ADR-018).** Recommended: an
  encrypted rclone remote on the somosboro.com Google Drive. Free, and rclone is
  already installed on the VPS.
  1. On the Mac: `brew install rclone`, then `rclone authorize "drive"`. Sign in
     as `abourgeoisroa@somosboro.com`. It prints a token JSON; keep it for step 2.
  2. On the VPS, run `rclone config`:
     * `n`, name `gdrive`, storage `drive`, leave client id and secret empty,
       scope `drive.file`, answer `n` to "Use auto config?", then paste the token
       from step 1.
     * `n` again, name `gtmbackup`, storage `crypt`, remote `gdrive:boro-gtm-backups`.
       Let rclone generate both passwords and **store them in BoRo's password
       manager**. Without them the copies cannot be read.
  3. `chmod 600 /root/.config/rclone/rclone.conf`.
  4. `echo 'GTM_BACKUP_REMOTE=gtmbackup:' >> /opt/boro_gtm/.env`.
  5. Run `/opt/boro_gtm/backup.sh`. It must print `off-host=true`.

  Alternatives that work the same way: Backblaze B2 (10 GB free) or any
  S3-compatible bucket. Point a crypt remote at it.

  **Chosen on 2026-11-12: the operator's PC.** `deploy/pc/pull-backup.ps1` pulls the
  newest dump over SSH (`boro-vps`), checks its SHA-256 against the VPS, keeps 30
  days in `D:\BoRo\Backups\boro-gtm` (only the operator and SYSTEM can read it) and
  records `last_off_host_copy`. The cutover check accepts a verified copy of a dump
  younger than 50 hours, so the PC must be on at least every other day. A Windows
  scheduled task runs it daily at 09:00 and at logon (missed runs start when the PC
  is next on). Log: `D:\BoRo\Backups\boro-gtm\pull.log`.

## If something is wrong

| Symptom | Do |
| --- | --- |
| `E health` says unhealthy | `docker compose logs --tail 200 worker`; `docker compose restart worker` |
| Bounce spike, sending paused automatically | `E summary` shows the reason; fix the list, then `E resume` |
| A message stuck SENDING | Nothing: the reconciler asks Gmail within 10 minutes. Never resend by hand. |
| A source is BLOCKED | The site refused us; it rests a week. Do not work around it. |
| `E sources` shows a lane short | Read the per-source counts; add a source or import, never lower the threshold. |
| Worker fails with "not the corporate sender" | The token opens another mailbox; redo the Gmail steps as abourgeoisroa@somosboro.com. |
