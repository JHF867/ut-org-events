# Setup (about 20 minutes)

You've already made your Anthropic key, your Apify token, and a GitHub repo. These steps finish the job.

## 1. Check your two secrets are named exactly right

In your repo: **Settings > Secrets and variables > Actions**. You need two repository secrets, spelled exactly:

- `APIFY_TOKEN`
- `ANTHROPIC_API_KEY`

If yours are named differently, delete them and add them again with these names. The spelling has to match.

## 2. Upload the files

1. Unzip this folder on your computer.
2. In your repo on github.com, click **Add file > Upload files**.
3. Drag in **everything inside** the unzipped folder: `pipeline.py`, `triage.py`, `requirements.txt`, `README.md`, `SETUP.md`, and the `data`, `docs` and `.github` folders.
4. Click **Commit changes**.

**If the `.github` folder doesn't upload** (Macs hide folders that start with a dot):
click **Add file > Create new file**, type `.github/workflows/pipeline.yml` as the name,
paste in the contents of that file from the zip (open it in any text editor), and click **Commit changes**.

## 3. Turn on the website

**Settings > Pages**. Under "Build and deployment", pick **Deploy from a branch**, branch **main**, folder **/docs**, then **Save**.
After a minute or two your site is live at `https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/`.
(The repo must be public for free Pages.)

## 4. Run a cheap test (about $0.10)

1. Click the **Actions** tab. If it asks, click the button to enable workflows.
2. Click **Pull org events** on the left, then **Run workflow**.
3. Leave "how many accounts" at **10** and click the green **Run workflow** button.
4. Wait 2 to 5 minutes, then click the run. The **Summary** shows how many posts were read and events found.

If it goes green, open `docs/events.json` in your repo to see what Claude pulled out, and check the site.

If it goes red, click the failed step. The error message says what's wrong (usually a secret name). Send it to Claude.

## 5. Turn on the schedule (when you're ready to pay)

1. Upgrade Apify to **Starter** ($19/month).
2. In your repo, open `.github/workflows/pipeline.yml`, click the pencil icon, and delete the `#` at the start of these two lines:
   ```
   # schedule:
   #   - cron: "0 12 * * 1,3,5"
   ```
   The `schedule:` line should line up under `workflow_dispatch:`.
3. Commit. It now runs every Mon, Wed and Fri at 7 a.m. Central on its own.

## After that

- **Once a week for the first two weeks:** open the site and spot-check a few events against the original posts.
- **Your only file to edit:** `data/orgs.csv` (fix a handle, add or remove an org, change a tier).
- **See which accounts are active, quiet or not found:** open `data/account_status.csv`.
- **Each run's summary** (Actions tab > click a run) shows accounts checked, rows, estimated Apify cost, and events found.
