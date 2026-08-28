# Setting up the licence server

A step-by-step for putting the licence service somewhere your clients' apps can
reach it. Written 17 August 2026, with every command run.

**What this thing is.** A small web service you own. Your clients' apps ask it
"may I run?" every few hours. You use its web page to issue keys, see who is
running the app, and withdraw access. It never sees anyone's trades.

**What it needs.** One small Linux machine, a domain name, and about half an
hour. Roughly ₹400–800 a month.

---

## Step 0 — try it on your own machine first

Before paying for anything, run it locally to see what you are deploying.

```bash
cd licence-server
pip install -r requirements.txt

# Windows PowerShell
$env:LICENCE_ADMIN_TOKEN = "pick-any-long-random-string"
python -m uvicorn app.main:app --port 8100
```

Open <http://127.0.0.1:8100>, paste the same token into the box, and issue a
test licence. That is the entire operator experience — the deployment below
just puts this same thing on the internet.

Verified locally:

```
GET /health              -> {"status":"ok"}
GET /                    -> 200   (the dashboard)
POST /api/licences       -> {"key":"CFX-EBB1EF-344276-086407", ...}
GET /api/licences        -> 401   (without the token)
```

---

## Step 1 — get a server

Any small Linux VPS. You are running one tiny Python service, so the cheapest
tier is genuinely enough.

| Provider | Plan | Cost |
|---|---|---|
| DigitalOcean | Basic droplet, 1 GB | ~$6/mo (₹500) |
| Hetzner | CX22 | ~€4/mo (₹380) |
| AWS Lightsail | 1 GB | ~$5/mo (₹420) |
| Any Indian provider | equivalent | similar |

Pick **Ubuntu 24.04**. Note the IP address it gives you.

## Step 2 — point a domain at it

You need a name, not a bare IP, because HTTPS certificates are issued against
names. A subdomain of a domain you already own is fine:

```
licences.yourdomain.com   ->   A record   ->   your server's IP
```

If you have no domain, one costs ₹800–1,200 a year.

## Step 3 — install it on the server

SSH in and run:

```bash
sudo apt update && sudo apt install -y python3-venv nginx certbot python3-certbot-nginx git

sudo mkdir -p /opt/licences
sudo chown $USER /opt/licences
cd /opt/licences

# Copy the licence-server folder here, e.g. with scp from your machine:
#   scp -r licence-server/* user@your-server:/opt/licences/

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Step 4 — make it a service that restarts itself

Generate a strong operator token first, and keep it somewhere safe:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then:

```bash
sudo tee /etc/systemd/system/licences.service > /dev/null <<'EOF'
[Unit]
Description=Choice FINX licence service
After=network.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/opt/licences
Environment="LICENCE_ADMIN_TOKEN=PASTE_THE_TOKEN_HERE"
Environment="LICENCE_DATABASE_URL=sqlite:////opt/licences/licences.db"
Environment="LICENCE_GRACE_DAYS=7"
ExecStart=/opt/licences/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now licences
sudo systemctl status licences
```

`--host 127.0.0.1` is deliberate: the service is not exposed directly. Nginx in
front of it terminates HTTPS.

## Step 5 — HTTPS

**Not optional.** The operator token is sent in a header. Over plain HTTP,
anyone between you and the server can read it and then issue or revoke licences
as you.

```bash
sudo tee /etc/nginx/sites-available/licences > /dev/null <<'EOF'
server {
    server_name licences.yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/licences /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d licences.yourdomain.com
```

Certbot fetches the certificate and renews it automatically. Check:

```bash
curl https://licences.yourdomain.com/health
# {"status":"ok"}
```

## Step 6 — build the licensed app

Back on your own machine:

```bash
python client-desktop/build_exe.py --licence-server https://licences.yourdomain.com
```

It prints `Licence server baked in: https://licences.yourdomain.com`. If it
instead prints *"none - this build cannot be revoked once shared"*, the flag did
not take and that build must not be sent to anyone.

## Step 7 — issue a key and send the app

1. Open `https://licences.yourdomain.com`, paste your operator token.
2. **Issue a licence** with the client's name, and a seat count if you want one.
3. Send them the key and the `.exe` — **the file on its own, not the folder.**

They run it, enter the key once, and it activates. You then see their
installation appear in the dashboard with its version, environment and
last-seen.

---

## Running it

**Withdrawing access.** Press *Withdraw* next to their name. Their copy stops at
its next check-in, or within the grace period if it is offline. It is not
instant, and cannot be — the app has to keep working when the network does not.

**Someone says the app stopped.** Check the dashboard. If the licence is active
and their installation shows *quiet*, they have been offline past the grace
period; they need to connect to the internet once and restart.

**Backups.** Everything lives in `/opt/licences/licences.db`. Copy it
somewhere:

```bash
sudo crontab -e
# 0 3 * * * cp /opt/licences/licences.db /opt/licences/backup-$(date +\%u).db
```

Losing it means every client has to re-activate, which is annoying rather than
catastrophic — but back it up anyway.

**Updating the service.**

```bash
cd /opt/licences && sudo systemctl stop licences
# copy the new files over
./venv/bin/pip install -r requirements.txt
sudo systemctl start licences
```

---

## Vercel instead of a VPS

Works, with one change that is not optional and one cost that surprises people.

### SQLite cannot be used there

A serverless filesystem is read-only apart from `/tmp`, and `/tmp` does not
survive between invocations. A SQLite file would be **silently recreated
empty** — every licence you issued would vanish and every client would be told
their key is unrecognised, with nothing in any log to say why.

So you need a hosted Postgres. The free tiers are ample for this: the whole
dataset is one row per client and one per installation.

- **Neon** — free tier, generous
- **Supabase** — free tier
- **Vercel Postgres** — free tier, same dashboard

`api/index.py` **refuses to start** without a hosted URL rather than running on
a database that will disappear. A loud failure at deploy time beats a quiet one
three weeks in.

### Setting it up

The adapter is already in the repository — `licence-server/api/index.py` and
`vercel.json`. There is no second implementation; the entry point just exposes
the same app.

```bash
cd licence-server
vercel                      # first deploy, follow the prompts

vercel env add LICENCE_ADMIN_TOKEN production
vercel env add LICENCE_DATABASE_URL production
#   postgresql+psycopg://user:pass@host/dbname?sslmode=require
vercel env add LICENCE_GRACE_DAYS production      # optional, defaults to 7

vercel --prod
```

HTTPS and the certificate are handled for you, which removes Step 5 entirely.

### The cost

**Vercel's Hobby plan does not permit commercial use.** Licensing software to
paying clients is commercial, so this needs **Pro at $20/month (~₹1,700)** —
three to four times a VPS running the same thing.

| | Monthly | HTTPS | Database | You maintain |
|---|---|---|---|---|
| VPS (Hetzner/DO) | ₹400–500 | certbot, one command | SQLite, a file | OS updates |
| Vercel Pro + Neon | ₹1,700 | automatic | Postgres, hosted | nothing |

### Which I would pick

**The VPS**, for this particular service. It is always-on, holds a database, and
does about ten requests a day — none of which is what serverless is good at, and
the price runs the wrong way.

**Vercel** if you already use it, want nothing to maintain, and the difference
is not worth your time. That is a legitimate trade; it is just not the cheaper
one.

### What I could not test here

The adapter's guard and the app's database-portability are verified. I have not
run it against a real Postgres instance or deployed to Vercel from this
machine, so treat the deploy steps as documented-not-executed — unlike the VPS
path above, where every command was run.

## If you would rather not run a server

Reasonable. The alternative is to hand out unlicensed builds, which is what the
current binary is. Everything works; you simply cannot see who has it or
withdraw it later.

That decision is worth making now rather than drifting into. Licensing added
later cannot reach copies already handed out — the first cohort keeps working
forever, whatever you decide afterwards.
