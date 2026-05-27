# Deploying Loss Analysis to AWS

This guide walks you through deploying the app to AWS so every manager can
reach it from any device, at a permanent URL, with their own login. No server
admin experience required.

**What you're building:**
```
[Managers' browsers]
        ↓  HTTPS
[App Runner]  ←—— auto-scaling Flask app, managed by AWS
        ↓  private network (VPC Connector)
[RDS PostgreSQL]  ←—— all ticket data and manager accounts
```

**Time:** ~45 minutes total on first run.  
**Cost:** Free tier covers RDS for 12 months. App Runner costs roughly
$1–3/month for a low-traffic internal tool at the smallest configuration.

---

## Before you start

- AWS account (free to create at aws.amazon.com)
- This repository pushed to GitHub (it is — you're reading this in it)
- A strong random secret key. Run this locally to generate one:
  ```
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  Copy the output. You'll paste it as `SECRET_KEY` later.

---

## Part 1 — Create the RDS PostgreSQL Database

> This is where all the ticket history and manager accounts will live.

1. Sign in to **console.aws.amazon.com** and make sure you're in your
   preferred region (top-right corner — e.g. `us-east-1`). Use the same
   region for every step in this guide.

2. In the search bar at the top, type **RDS** and click it.

3. Click **"Create database"**.

4. Under *Choose a database creation method* → select **Standard create**.

5. Under *Engine options* → select **PostgreSQL**.

6. Under *Templates* → select **Free tier**.
   > Free tier gives you a `db.t3.micro` instance — more than enough for
   > a kitchen ops tool. After 12 months it costs ~$15/month if you keep it.

7. Under *Settings*:
   - **DB instance identifier:** `lossanalysis-db`
   - **Master username:** `lossanalysis`
   - **Master password:** choose a strong password and save it — you'll
     need it in a moment.  
     (e.g. `Lm7#kPq9@VrX2` — no simple dictionary words)

8. Under *Instance configuration* — leave the defaults (`db.t3.micro`).

9. Under *Storage* — leave the defaults (20 GB gp2).

10. Under *Connectivity*:
    - **Virtual Private Cloud (VPC):** leave as **Default VPC**
    - **Public access:** select **No**
      > Setting this to No means only services inside the same AWS network
      > can reach the DB — the app will connect through a private channel
      > you set up in Part 2.
    - **VPC security group:** select **Create new**
    - **New VPC security group name:** `lossanalysis-rds-sg`
    - **Availability Zone:** No preference

11. Under *Database authentication* — leave **Password authentication**.

12. Expand **Additional configuration**:
    - **Initial database name:** `lossanalysis`
    > If you skip this, the database won't be created automatically and
    > the app will fail to connect.

13. Click **"Create database"** at the bottom.

14. **Wait** — it takes 3–5 minutes to spin up. The status will say
    *Creating* and then *Available*.

15. Once it says *Available*, click on the database name → under
    *Connectivity & security*, copy the **Endpoint** URL.
    It looks like:
    ```
    lossanalysis-db.xxxxxxxxxxxx.us-east-1.rds.amazonaws.com
    ```
    Save this — you'll need it soon.

---

## Part 2 — Create a VPC Connector

> This gives App Runner a private channel to talk to your RDS database
> without exposing the DB to the internet.

1. In the AWS search bar, type **App Runner** and click it.

2. In the left sidebar, click **"VPC connectors"**.

3. Click **"Add VPC connector"**.

4. Fill in:
   - **VPC connector name:** `lossanalysis-connector`
   - **VPC:** select **Default VPC** (same one RDS is in)
   - **Subnets:** check all available subnets (select all)
   - **Security groups:** click **Create new security group**
     - Name: `lossanalysis-apprunner-sg`
     - Description: `App Runner outbound for Loss Analysis`
     - Leave the default outbound rule (Allow all)

5. Click **"Add"**.

6. The connector is created immediately. You'll see it in the list.
   Note the **Security group ID** that was created (something like
   `sg-0abc123...`) — you need it in Part 3.

---

## Part 3 — Allow the App Runner to Reach the Database

> You need to tell the RDS security group to accept connections that
> come from the App Runner connector you just created.

1. In the AWS search bar, type **VPC** and click it.

2. In the left sidebar under *Security*, click **"Security groups"**.

3. Find the group named **`lossanalysis-rds-sg`** (the one created in Part 1).
   Click on it.

4. Click the **"Inbound rules"** tab → click **"Edit inbound rules"**.

5. Click **"Add rule"**:
   - **Type:** PostgreSQL
   - **Protocol:** TCP (filled automatically)
   - **Port range:** 5432 (filled automatically)
   - **Source:** Custom → paste the **security group ID** from Part 2
     (`lossanalysis-apprunner-sg`, e.g. `sg-0abc123...`)

6. Click **"Save rules"**.

---

## Part 4 — Create the App Runner Service

> App Runner pulls your code from GitHub, builds it automatically on every
> push, and serves it at an HTTPS URL.

1. Go back to **App Runner** (search bar → App Runner).

2. Click **"Create service"**.

3. Under *Source and deployment*:
   - **Repository type:** Source code repository
   - Click **"Add new"** under GitHub connection
     - Follow the prompts to authorise AWS to access your GitHub account
     - You only need to grant access to the `lossanalysis` repository
   - **Repository:** `yeemscoffee/lossanalysis`
   - **Branch:** `claude/ecstatic-lamport-mAIiY`
     > Once you merge this to `main` later, switch this to `main`.
   - **Deployment trigger:** Automatic
     > Every time you push to this branch, App Runner redeploys
     > automatically — no manual steps needed.

4. Under *Configure build*:
   - **Configuration file:** select **"Use a configuration file"**
   > This uses the `apprunner.yaml` already in the repo.

5. Click **"Next"**.

6. Under *Service settings*:
   - **Service name:** `lossanalysis`
   - **CPU:** 1 vCPU
   - **Memory:** 2 GB

7. Under *Environment variables*, click **"Add environment variable"**
   for each of these. Select *Plaintext* for all of them:

   | Key | Value |
   |-----|-------|
   | `SECRET_KEY` | the 64-char hex string you generated earlier |
   | `DATABASE_URL` | `postgresql://lossanalysis:YOUR_PASSWORD@YOUR_ENDPOINT:5432/lossanalysis` |
   | `INITIAL_ADMIN_EMAIL` | your email address |
   | `INITIAL_ADMIN_PASSWORD` | a temporary password (you'll change it after first login) |

   **DATABASE_URL example** (replace the placeholders):
   ```
   postgresql://lossanalysis:Lm7#kPq9@VrX2@lossanalysis-db.xxxxxxxxxxxx.us-east-1.rds.amazonaws.com:5432/lossanalysis
   ```

   > `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD` are only used
   > **once** — on the very first boot when the users table is empty.
   > After that, you can clear these env vars from the App Runner settings
   > if you like (the account stays in the DB).

8. Under *Security* → **Instance role**: leave as *Create new service role*.

9. Under *Networking*:
   - **Outgoing network traffic:** select **Custom VPC**
   - **VPC connector:** select `lossanalysis-connector` (the one from Part 2)

10. Click **"Next"** → review everything → click **"Create & deploy"**.

11. **Wait** — the first deploy takes 3–5 minutes. You'll see a progress
    log. When it says *Running*, your app is live.

12. Copy the **Default domain** URL at the top — it looks like:
    ```
    https://xxxxxxxxxx.us-east-1.awsapprunner.com
    ```
    This is your permanent app URL.

---

## Part 5 — First Login and Adding Your Team

1. Open the App Runner URL in your browser.

2. You'll see the Loss Analysis sign-in page. Sign in with the
   `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD` you set in Part 4.

3. You're in! Click **"Manage"** in the top navigation.

4. For each manager on your team:
   - Fill in their name, work email, and a temporary password
   - Click **"Create Account"**
   - Send them the URL and their temporary credentials

5. Managers sign in with their own credentials. Each person's session
   is separate — they can all be in the app at the same time.

---

## Part 6 — Optional: Clean Up the Seed Credentials

Once you've confirmed everything works and your own account is set up:

1. In App Runner, click your service → **"Configuration"** tab → **"Edit"**.
2. Under Environment variables, delete `INITIAL_ADMIN_EMAIL` and
   `INITIAL_ADMIN_PASSWORD`.
3. Click **"Save changes"** — App Runner will redeploy (takes ~2 min).

The admin account stays in the database — removing the env vars just
prevents any future "first boot" from accidentally creating a second one.

---

## Troubleshooting

### App shows "Service unavailable" after first deploy
- Check the App Runner **event log** for errors.
- Most common cause: typo in `DATABASE_URL`. The password must be
  URL-encoded if it contains special characters like `@` or `#`.
  Use this tool to encode: https://www.urlencoder.org/
  For example, `Lm7#kPq9@VrX2` becomes `Lm7%23kPq9%40VrX2`.
  Full URL: `postgresql://lossanalysis:Lm7%23kPq9%40VrX2@...`

### "could not connect to server" in logs
- The VPC connector isn't attached, or the RDS security group inbound
  rule isn't set up correctly. Re-check Part 2 and Part 3.

### Login says "Invalid email or password"
- The initial admin seed only runs once when no users exist. If the app
  deployed before the env vars were set, re-check the vars and then
  manually redeploy: App Runner → your service → **"Deploy"** button.

### Uploading a CSV gives an error
- Make sure the database is *Available* in RDS (not still creating).
- The app creates its tables automatically on first boot — check the
  App Runner deploy log for any Python errors.

---

## Keeping the App Up to Date

Whenever you want to update the app (the developer pushes changes to
the branch), App Runner detects the new commit and redeploys
automatically. No action needed from you — just wait ~3 minutes.

To deploy manually at any time: App Runner → your service → **"Deploy"**.

---

## Cost Estimate

| Service | Free tier | After free tier |
|---------|-----------|-----------------|
| RDS db.t3.micro | Free for 12 months | ~$15–17/month |
| App Runner (1 vCPU / 2 GB) | None | ~$2–5/month active |
| Data transfer | 1 GB free/month | Minimal for internal tool |

For a kitchen ops tool used by a handful of managers, total cost after
the free tier expires is roughly **$20/month**.
