# Setting up James IV, starting from zero

This guide assumes you have never written code, never opened a terminal, and
would like to keep it that way as much as possible. Every step tells you
exactly what to type and what you should see. Nothing here requires
understanding — only copying carefully.

**The plan in one paragraph:** you will rent a small computer in a data center
(about $8/month — called a "server"), put this bot on it, and tell it your Resy
login and which restaurants you want. It then runs day and night: at each
venue's release time it fires the instant tables drop, and in between it
watches for cancellations. When it books, your phone buzzes. Your laptop can be
closed, off, or in a lake — the server doesn't care.

A "terminal" is the black window where you type commands at a computer. You'll
use one, but only to paste lines from this guide, one at a time, pressing
Enter after each.

---

## Part 1 — Phone alerts (5 minutes, free, do this first)

1. Install the **ntfy** app (App Store / Google Play).
2. In the app, tap **+** to subscribe to a topic. A topic is just a name —
   anyone who knows it can see those alerts, so make it unguessable. Something
   like `james-iv-mg5-x84kq2` (make up your own ending). Write it down.
3. That's it. Later, the bot will send to that name and your phone will buzz.

## Part 2 — Rent the server (10 minutes)

Any provider works; these steps use DigitalOcean because its setup is gentle.

1. Create an account at digitalocean.com (needs a credit card).
2. Click **Create → Droplets** ("droplet" is their word for a small server).
3. Choose:
   - **Region: New York.** Not optional-ish — actually important. At release
     time you're racing other people's requests, and a server in the same city
     as Resy's is a head start.
   - **OS image:** click the **Marketplace** tab and pick **Docker**
     (it comes with the software the bot runs inside).
   - **Size:** the cheapest **Basic / Regular** option (~$6–8/month) is plenty.
   - **Authentication:** choose **Password** and set a strong one. Save it.
4. Click **Create Droplet** and wait a minute for it to come online.

## Part 3 — Open the terminal (1 minute)

On your droplet's page in DigitalOcean, click **Access → Launch Droplet
Console**. A black window opens in your browser, already logged in. That's
your terminal. All commands below get pasted into it.

## Part 4 — Get the bot onto the server (2 minutes)

**If pasting into the web console doesn't work** (a known DigitalOcean
annoyance): try right-click → Paste inside the black window, or
Ctrl+Shift+V — and if the browser asks for clipboard permission, click
Allow. If none of that works, use your own computer's terminal instead:
open Terminal (Mac) or PowerShell (Windows), type
`ssh root@YOUR_DROPLET_IP` (the IP is on your droplet's page), enter your
droplet password — pasting works normally there. And the commands below are
deliberately short enough to type by hand if all else fails.

Type or paste, then press Enter after each line:

```bash
git clone https://github.com/mgrause5/James-IV.git
cd James-IV
```

**You should see:** a few lines ending without the word "error", and your
prompt now says `James-IV`.

## Part 5 — The installer does the rest (10 minutes)

```bash
bash setup.sh
```

That one command replaces everything that used to be Parts 5–7: it asks for
your Resy email and password (typing the password is invisible — that's
normal), your ntfy topic (it offers to generate a random one — if you use
it, subscribe to that exact name in the phone app), and your name and
mobile number (DoorDash venues place the reservation under this name).
Then it writes the settings file itself, builds the bot, runs the full
health check, buzzes your phone, and asks whether to start.

**You should see** green `ok` lines through the health check, then feel a
buzz. If doctor prints red lines instead, read them — the fix is almost
always a typo'd password (`rm .env`, then `bash setup.sh` again) or a
missing saved card on resy.com. Safe to re-run as many times as needed.

Also make sure the Resy account has a **saved credit card**
(resy.com → your profile → Payment Methods). Hard venues won't book
without one.

## Part 6 — Dress rehearsal (optional) (2 minutes)

```bash
docker compose run --rm james simulate Torrisi --scenario drop
```

This plays out a fake 10:00:00 AM release against a pretend Resy, using your
real settings, and shows the bot catching it second by second. Try
`--scenario contested` (competitors grab it first) and
`--scenario cancellation` too. Nothing real is touched.

## Part 7 — Let it off the leash (if you told the installer not to)

```bash
docker compose up -d
```

That's the "go" command. `-d` means it keeps running after you close the
window. Your phone gets a "James IV started" message listing what it's
hunting. To watch it think:

```bash
docker compose logs -f
```

(Ctrl+C stops *watching*; the bot itself keeps running.)

## Part 8 — Graduation (over the next week)

1. **Days 1–3, dry run:** you'll get "[dry run] Would book:" alerts. Check
   them. Right restaurant? Right day? A time you'd actually eat at? If not,
   fix `config.yaml`, then `docker compose restart`.
2. **Go live:** edit `config.yaml`, change `dry_run: true` to `false`, save,
   `docker compose restart`.
3. **First real test on an easy target:** before trusting it with the one that
   matters, add some bookable place, let it book a real table, confirm the
   reservation shows in your Resy app — then cancel it politely:
   `docker compose run --rm james status` (copy the token) and
   `docker compose run --rm james cancel <token>`.
4. Now let it hunt the real prizes.

## Cheat sheet

| You want to... | Paste this |
| --- | --- |
| See what it's booked + what's next | `docker compose run --rm james status` |
| Watch it live | `docker compose logs -f` |
| Change restaurants | `nano config.yaml`, then `docker compose restart` |
| Pause everything | `docker compose down` |
| Resume | `docker compose up -d` |
| Full health check | `docker compose run --rm james doctor` |
| What's available right now | `docker compose run --rm james check` |
| Cancel a booking it made | `docker compose run --rm james cancel <token>` |

## When something goes wrong

- **Phone alert: "cannot see availability."** Resy's edge is throttling your
  server's address. The bot keeps retrying and tells you if sight returns; if
  it doesn't within a few hours, rebuild the droplet for a fresh address.
- **Phone alert: "re-auth failed."** Log into resy.com in your browser once
  (Resy sometimes wants to see a human), then `docker compose restart`.
- **A drop came and went with nothing.** Read
  `docker compose logs | grep -i snipe`. "NO inventory appeared" means the
  drop time is wrong — `docker compose run --rm james policy <slug>` shows
  what the venue states. "appeared but sold out" means you lost the race:
  that happens; the cancellation watcher is your second chance.
- **Honesty corner:** bots are against Resy's terms of service. The realistic
  worst case is your Resy account getting suspended. The bot's polite request
  pacing exists to make that unlikely, but the risk isn't zero — don't point
  it at your account if that risk is unacceptable, and book tables you'll
  actually sit at.
