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

1. In Google Cloud (project of the somosboro.com Workspace), enable the Gmail
   API and create an OAuth client of type **Desktop app**. Download its JSON.
2. On your Mac, in the repository:
   ```bash
   gtm engine gmail-auth-url --client-id <client id from the JSON>
   ```
   Open the URL, sign in as `abourgeoisroa@somosboro.com`, approve. The browser
   lands on `http://127.0.0.1:8765/?code=…` (the page will not load; that is
   fine). Copy the `code` value.
3. ```bash
   gtm engine gmail-auth-exchange --client-secret-file ~/Downloads/client_secret.json --out ~/gmail.json
   ```
   Paste the code when prompted (hidden).
4. Copy it to the VPS and restart the worker:
   ```bash
   scp -i ~/.ssh/getboro_deploy ~/gmail.json boro-vps:/opt/boro_gtm/secrets/gmail.json
   ssh -i ~/.ssh/getboro_deploy boro-vps 'chmod 600 /opt/boro_gtm/secrets/gmail.json && cd /opt/boro_gtm && docker compose restart worker'
   rm ~/gmail.json
   ```
5. Backfill history so nobody already contacted is contacted again:
   `E ingest-history --since 2026-06-01`

Scopes requested: `gmail.send` and `gmail.readonly`. Nothing else.

## Cutover from the ChatGPT automations

The VPS engine and the ChatGPT automations must never both send.

1. Gmail connected and history backfilled (above).
2. Week-1 pool imported if it exists.
3. Shadow mode for at least two business days. Compare `E sends` (SHADOW rows)
   with what the ChatGPT automations actually sent that day.
4. `E cutover-report` returns `"ready_for_live": true`.
5. **Switch off** the ChatGPT automations *BoRo Demand Radar*, *BoRo Lead
   Enrichment*, *BoRo Initial Outreach* and *BoRo Follow-ups & Replies*.
   **Leave *Radar ATS AutoPostulación* alone** — it is the personal employment
   lane and is not part of this engine.
6. Wait until the last ChatGPT run has finished, then `E run ingest`.
7. Set `GTM_ENGINE_SHADOW_MODE=false` in `/opt/boro_gtm/.env`, then
   `docker compose up -d worker`.
8. Watch the first day: `E sends`, `E failed`, `docker compose logs -f worker`.

To go back to shadow at any time: set it to `true` and `docker compose up -d worker`.

## Deploying a new version

From the repository on your Mac, at the commit to deploy:

```bash
SHA=$(git rev-parse --short HEAD)
git archive --format=tar HEAD | ssh -i ~/.ssh/getboro_deploy boro-vps \
  "rm -rf /opt/boro_gtm/src/* && tar -x -C /opt/boro_gtm/src && \
   cp /opt/boro_gtm/src/deploy/vps/docker-compose.yml /opt/boro_gtm/ && \
   cd /opt/boro_gtm && nice -n 10 docker build -q -t boro-gtm-core:$SHA -f src/services/api/Dockerfile src && \
   sed -i 's/^GTM_IMAGE_TAG=.*/GTM_IMAGE_TAG=$SHA/' .env && ./backup.sh && docker compose up -d"
```

Migrations run automatically (`migrate` service) before the API and worker start.

## Backups

* Nightly at 03:30 (server time): `/opt/boro_gtm/backup.sh`, 14 days kept in
  `/opt/boro_gtm/backups`.
* Prove a restore any time: `/opt/boro_gtm/restore-test.sh`.
* Off-host: set `GTM_BACKUP_REMOTE` (an rclone remote) once a bucket exists.
  Until then the backups share the VPS disk.

## If something is wrong

| Symptom | Do |
| --- | --- |
| `E health` says unhealthy | `docker compose logs --tail 200 worker`; `docker compose restart worker` |
| Bounce spike, sending paused automatically | `E summary` shows the reason; fix the list, then `E resume` |
| A message stuck SENDING | Nothing: the reconciler asks Gmail within 10 minutes. Never resend by hand. |
| A source is BLOCKED | The site refused us; it rests a week. Do not work around it. |
