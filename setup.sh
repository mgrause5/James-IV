#!/usr/bin/env bash
# James IV interactive installer.
#
# Exists because the person running this may never have used a terminal, and
# the DigitalOcean web console makes even pasting hard. After `git clone`,
# this is the ONLY command to run: it asks questions in plain English, writes
# the .env file itself, builds the bot, health-checks it, and offers to start
# it. Safe to re-run any time.
set -euo pipefail

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[!]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[x]\033[0m %s\n' "$*"; exit 1; }

# Values go into .env as KEY="value"; escape what would break the quoting.
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

[ -f docker-compose.yml ] || fail "Run this from inside the James-IV folder (type: cd James-IV)"

DOCKER="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    DOCKER="docker-compose"
  elif ! command -v docker >/dev/null 2>&1; then
    # Plain-Ubuntu droplet (Marketplace Docker image not used). Self-heal:
    # Docker's official install script sets up the engine + compose plugin.
    warn "Docker isn't installed on this server yet."
    read -rp "  Install it now? Takes about 2 minutes. [Y/n]: " INST
    if [ "${INST:-Y}" = "n" ] || [ "${INST:-Y}" = "N" ]; then
      fail "The bot runs inside Docker. Re-run 'bash setup.sh' when ready to install it."
    fi
    curl -fsSL https://get.docker.com | sh || fail "Docker install failed -- paste the lines above to your assistant."
    systemctl enable --now docker >/dev/null 2>&1 || true
    docker compose version >/dev/null 2>&1 || fail "Docker installed but 'docker compose' is still unavailable -- try: apt-get install -y docker-compose-plugin"
    ok "Docker installed"
  else
    # Engine present, compose plugin missing (rare) -- fetch just the plugin.
    apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
    docker compose version >/dev/null 2>&1 || fail "Docker is here but Compose is not -- run: apt-get install -y docker-compose-plugin"
    ok "Docker Compose plugin installed"
  fi
fi

say "James IV setup"
echo "  Answers are only written to files on THIS server. Nothing is sent anywhere else."

# ---------------------------------------------------------------- config.yaml
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  ok "config.yaml created with all 15 restaurants pre-loaded"
else
  ok "config.yaml already exists -- keeping it"
fi

# ----------------------------------------------------------------------- .env
if [ -f .env ]; then
  ok ".env already exists -- keeping your saved details"
  echo "     (to redo this part: delete it with 'rm .env' and run 'bash setup.sh' again)"
else
  say "1/4 -- Your Resy login"
  echo "  Used only to log in as you, from this server."
  read -rp "  Resy account email: " RESY_EMAIL
  read -rsp "  Resy password (typing is invisible -- that's normal): " RESY_PASSWORD; echo

  say "2/4 -- Phone alerts (ntfy)"
  RAND_TOPIC="james-iv-$(head -c 64 /dev/urandom | tr -dc 'a-z0-9' | head -c 10)"
  echo "  In the ntfy app on your phone, tap + and subscribe to a topic name."
  echo "  Press Enter to use this generated one: ${RAND_TOPIC}"
  read -rp "  ntfy topic [${RAND_TOPIC}]: " NTFY_TOPIC
  NTFY_TOPIC=${NTFY_TOPIC:-$RAND_TOPIC}
  echo "  -> Make sure your phone is subscribed to exactly: ${NTFY_TOPIC}"

  say "3/4 -- Your name, for DoorDash venues"
  echo "  Or'esh, The Corner Store and The Eighty Six book without an account;"
  echo "  the reservation is placed under this name and phone number."
  read -rp "  First name: " GUEST_FIRST_NAME
  read -rp "  Last name: " GUEST_LAST_NAME
  read -rp "  Mobile number (like +12125551234): " GUEST_PHONE

  cat > .env <<ENVEOF
RESY_EMAIL="$(esc "$RESY_EMAIL")"
RESY_PASSWORD="$(esc "$RESY_PASSWORD")"
NTFY_TOPIC="$(esc "$NTFY_TOPIC")"
GUEST_FIRST_NAME="$(esc "$GUEST_FIRST_NAME")"
GUEST_LAST_NAME="$(esc "$GUEST_LAST_NAME")"
GUEST_PHONE="$(esc "$GUEST_PHONE")"
ENVEOF
  chmod 600 .env
  ok ".env written (readable only by this server's admin user)"
fi

# ---------------------------------------------------------------- state + build
mkdir -p state
chown 1000:1000 state 2>/dev/null || warn "could not chown state/ (fine if not running as root)"

say "4/4 -- Building the bot (one time, a few minutes of scrolling text)"
$DOCKER build
ok "built"

# --------------------------------------------------------------------- checks
say "Health check (doctor)"
if $DOCKER run --rm james doctor; then
  ok "doctor passed"
else
  warn "doctor found problems -- read the red lines above, fix, then run 'bash setup.sh' again."
  warn "Most common: a typo in the Resy email/password (rm .env, re-run), or no saved card on resy.com."
  exit 1
fi

say "Test alert -- your phone should buzz in a few seconds"
$DOCKER run --rm james test-notify || warn "test-notify failed; check the ntfy topic matches your phone exactly"

# ---------------------------------------------------------------------- start
say "Everything checks out."
echo "  The bot starts in DRY RUN: it finds tables and texts what it WOULD book,"
echo "  without spending anything. Recommended: let one morning of drops run dry,"
echo "  then go live by editing config.yaml (dry_run: false) and restarting."
read -rp "Start the bot now? [Y/n]: " START
if [ "${START:-Y}" != "n" ] && [ "${START:-Y}" != "N" ]; then
  $DOCKER up -d
  ok "running -- your phone gets a 'James IV started' message"
  echo "  Watch it live:  $DOCKER logs -f      (Ctrl+C stops watching, not the bot)"
else
  echo "  Start later with:  $DOCKER up -d"
fi
