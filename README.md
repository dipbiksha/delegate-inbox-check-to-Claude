# delegate-inbox-check-to-Claude

This tool tells you which of your client workspaces got new lead replies since the last working day. It checks **Email Bison** and **HeyReach** and prints one short list.

```
Inbox check: Tue 06 Oct 2026, 09:00 (America/New_York)
New replies since Mon 05 Oct, 09:00 (last 24h)

Acme Corp      Bison: 3 new    HeyReach: 4 new (3 awaiting reply)
Globex         Bison: 0        HeyReach: not set up
Initech        Bison: ERROR (key rejected, 401)    HeyReach: 0

1 of 3 clients have new replies.
```

It only reads. It never replies, tags, or marks anything as read. You need Python 3.9 or newer on your computer. Nothing else to install.

---

## Setup

### 1. Get the code

Open the Terminal (Mac) or Command Prompt (Windows) and run:

```bash
git clone https://github.com/dipbiksha/delegate-inbox-check-to-Claude
cd delegate-inbox-check-to-Claude
chmod +x check.sh
```

The first line downloads the tool, the second moves you into its folder, and the third lets your Mac run the launcher. Windows users skip `chmod` and type `check.bat` wherever this guide says `./check.sh`.

### 2. Add your clients

A "client" here is one Bison workspace. This step tells the tool which workspaces to watch. Run:

```bash
./check.sh setup
```

The tool asks for your Bison address and your Bison **master key**. The master key is the admin key that can see every workspace. It is only used here, to read the list of workspaces. Say `y` when it offers to save them.

It then shows each Bison workspace one by one. For each one:

- **Include it?** Type `y` for every workspace you want to check in the master inbox. Type `n` to skip the rest.
- **Name and slug.** The name is how the client shows up in your report (for example `Acme Corp`). The slug is a short nickname in lowercase letters with no spaces (for example `acme`). Write your slugs down, because you need them in step 4.
- **HeyReach?** Type `y` if this client also runs outreach on HeyReach, otherwise `n`. If you say `y`, you will be asked for their HeyReach key in step 4.

When it finishes it creates a file called `clients.json`. This is your client list. It has names only, no keys.

To add one more client later, run `./check.sh add-client`.

### 3. Optional: let the tool fetch HeyReach keys for you

Skip this if you don't have a HeyReach **Organization** key. Without it, you will paste each client's HeyReach key by hand in step 4, which works fine.

If you do have one, open the `.env` file (created in step 2) in any text editor and add this line:

```
HEYREACH_ORG_KEY=paste-your-org-key-here
```

Save the file. With this set, the tool collects each client's HeyReach key itself.

### 4. Create the keys

A key is a password that lets the tool read one client's inbox. Each person who runs the check needs their own set.

```bash
./check.sh issue-keys acme globex --for priya
```

- Replace `acme globex` with your slugs from step 2, separated by spaces.
- Replace `priya` with the name of the person who will use the keys. Use lowercase letters. For yourself, use your own name.

The tool creates a Bison key for each client and tests it. For HeyReach it asks you to paste each client's key (nothing shows on screen as you paste, that is normal), unless you did step 3.

At the end it prints a block of text like this:

```
IC_BISON_URL=https://mail.yourcompany.com
IC_ACME_NAME=Acme Corp
IC_ACME_BISON_KEY=xxx
IC_ACME_HEYREACH_KEY=xxx
```

**Copy it straight away.** This is the only time the keys are shown. Paste the block into your `.env` file, or send it to your teammate through a password manager so they can paste it into theirs.

If you already have your keys and don't need `issue-keys`, you can type them into `.env` yourself. Start with these two lines:

```
IC_BISON_URL=https://mail.yourcompany.com
BISON_MASTER_KEY=xxx
```

Then add three lines per client, using the client's slug in capitals:

```
IC_ACME_NAME=Acme Corp
IC_ACME_BISON_KEY=xxx
IC_ACME_HEYREACH_KEY=xxx
```

Leave out the HeyReach line for clients who don't use it.

### 5. Test it

This checks every key against Bison and HeyReach and tells you which ones work.

```bash
./check.sh doctor
```

If every key passes, you are set up. If one fails, the message tells you which client and why (see Troubleshooting below).

### 6. Use it

```bash
./check.sh
```

This prints the report. It takes 20 to 45 seconds, because HeyReach is slow to answer.

| Command | What it does |
|---|---|
| `./check.sh acme globex` | Checks only those clients |
| `./check.sh --hours 6` | Counts replies from the last 6 hours |
| `./check.sh --unread` | Counts replies still marked unread |

**Reading the report:** a number is the count of new replies. `not set up` means the client doesn't use that platform. `no key` means you have no key for it. `ERROR` means the check failed for that client.

"New" means received in the last 24 hours (on Mondays, since Friday, so weekends are covered). Bison counts inbox replies only, with spam, bounces and out-of-office replies left out. For HeyReach, "awaiting reply" means the lead's message is still the last one in the conversation.

---

## Run it automatically with Claude

This sends you the report on a schedule, so you don't have to run it by hand. Do this under your own Claude account, after step 5 passes.

### 1. Create a private environment

An environment is the settings box the scheduled run uses. It holds your keys, so the tool can sign in to Bison and HeyReach.

At claude.ai/code, open the environment selector (or use the Desktop app) and create one:

- **Name:** `inbox-check`
- **Network access:** Custom
  - Allowed domains: your Bison address without the `https://` (for example `mail.yourcompany.com`) and `api.heyreach.io`
  - Tick "Also include default list of common package managers"
- **Environment variables:** copy and paste every line from your `.env` file, except `BISON_MASTER_KEY` and `HEYREACH_ORG_KEY`. Those two stay on your computer.
- Keep the environment **personal**. A shared one lets other members read your keys.

### 2. Create the routine

A routine is the scheduled job. At claude.ai/code/routines, choose **New routine**:

- **Repository:** your copy of delegate-inbox-check-to-Claude
- **Environment:** `inbox-check`
- **Schedule:** once a day, for example weekdays at 9:07. The preset buttons use your local time. The Custom box uses **UTC**.
- **Prompt:**

  > Run `python3 inboxcheck.py check` from the repository root and reply with its output exactly as printed. Do not print, echo, or inspect environment variables. Do not modify, commit, or push any files. If the command fails, report the error message only.

Click **Run now** once to confirm it works.

### Running several times a day

By default each run counts the last 24 hours, so a second run in the same day would repeat the same replies. To fix that, tell the tool your run times:

1. Add these to the environment variables (times are 24-hour, in your time zone, so also set `IC_TIMEZONE`, for example `Europe/London`):
   ```
   IC_SCHEDULE=12:00,15:00,19:00
   IC_SCHEDULE_DAYS=mon-fri
   ```
2. Give the routine a **Custom** trigger with the same times converted to UTC. For example, 12:00, 15:00 and 19:00 in Nepal (UTC+5:45) are 06:15, 09:15 and 13:15 UTC:
   ```
   15 6,9,13 * * 1-5
   ```
   Times with different minutes need one trigger each (**+ Add another trigger**).

Each run then counts only what arrived since the previous scheduled run. Keep run times at least an hour apart.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| `IC_BISON_URL is not set` | Add your Bison address to `.env`. |
| `key rejected, 401` | The key was cancelled or mistyped, or the Bison address is wrong. Issue or paste it again. |
| A client is missing from the report | You have no key for it. Run `./check.sh doctor`. |
| A client shows its slug instead of its name | Add `IC_<SLUG>_NAME=...` to `.env`. |
| `HeyReach: ERROR (timed out after 120s)` | HeyReach is slow right now. Run it again later. |
| Blocked in a routine | Add your Bison host and `api.heyreach.io` to the environment's allowed domains. |

## Keep your keys safe

- Share keys only through a password manager.
- `.env` and `clients.json` stay on your computer. Never commit them to Git.
- Never put `BISON_MASTER_KEY` or `HEYREACH_ORG_KEY` in Claude.
# delegate-inbox-check-to-Claude git init git add . git commit -m first commit git branch -M main git remote add origin https://github.com/dipbiksha/delegate-inbox-check-to-Claude.git git push -u origin main
