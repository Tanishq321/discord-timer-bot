# Running the bot 24/7 (laptop can be off)

The bot needs a computer that never sleeps. You rent a tiny Linux server in the cloud,
copy the bot to it once, and run one setup script. After that it runs on its own,
restarts if it crashes, and comes back after server reboots.

## Which host?

| Option | Cost | Card needed? | Notes |
|---|---|---|---|
| **Azure for Students** (recommended) | $100 credit, renewable yearly while you're a student | No | Sign up with your college email. When credit runs out, it just stops; you are never billed. |
| Oracle Cloud Always Free | Free forever | Yes (verification only) | Signup sometimes rejects cards, and you may hit "out of capacity". |
| Railway | ~$5/month (30-day free trial) | No for trial | Easiest (deploy from GitHub), but you **must** add a volume (see bottom). |

| Render + Neon (see **DEPLOY-RENDER.md**) | Free | No | No terminal at all. Needs a free pinger to stay awake; shared IPs occasionally get rate-limited by Discord. |

Avoid Replit / Glitch: they put the app to sleep, so the bot goes offline.
Vercel can't run this bot at all, because it can't keep a connection to Discord open.
Avoid Heroku for this bot: it wipes files daily, so saved timers would vanish.

---

## Azure for Students: step by step

### 1. Create the account
Go to https://azure.microsoft.com/free/students and sign up with your college email.

### 2. Create the server (VM)
In https://portal.azure.com → **Virtual machines** → **Create** → **Azure virtual machine**:

- **Resource group:** Create new → `discord-bot`
- **Virtual machine name:** `timer-bot`
- **Region:** (Asia Pacific) Central India, or any region that lets you create the size below
- **Image:** Ubuntu Server 24.04 LTS
- **Size:** the smallest B-series size (e.g. **B1s**). The bot uses very little memory.
  The page shows the monthly cost; your credit covers it.
- **Authentication type:** SSH public key, **Username:** `azureuser`
- **Key pair name:** `timer-bot-key`
- **Public inbound ports:** Allow selected → **SSH (22)**

Click **Review + create** → **Create**. When asked, **download the private key**
(`timer-bot-key.pem`). Keep it safe: it is your password to the server.

When it finishes, open the VM and copy its **Public IP address**.

### 3. Copy the bot to the server
On your laptop, open a terminal (PowerShell on Windows) in the folder that has both
`discord-timer-bot.zip` and `timer-bot-key.pem`:

```bash
scp -i timer-bot-key.pem discord-timer-bot.zip azureuser@YOUR_PUBLIC_IP:~
ssh -i timer-bot-key.pem azureuser@YOUR_PUBLIC_IP
```
Type `yes` the first time. You're now inside the server.

> **Windows "UNPROTECTED PRIVATE KEY FILE" error?** Run this once in PowerShell, then retry:
> `icacls timer-bot-key.pem /inheritance:r /grant:r "$($env:USERNAME):R"`
>
> **macOS/Linux:** run `chmod 400 timer-bot-key.pem` first.

### 4. Install and start the bot (on the server)
```bash
sudo apt-get install -y unzip
unzip discord-timer-bot.zip
cd discord-timer-bot
bash deploy/setup.sh        # stops and asks for your token the first time
nano .env                   # paste your DISCORD_TOKEN, then Ctrl+O, Enter, Ctrl+X
bash deploy/setup.sh        # run again: installs the 24/7 service
```
You should see `active (running)`. Type `exit` to leave the server, then close your laptop.

### 5. Test it
In Discord: `/schedule when: in 2m message: Test from the cloud ☁️`

### Day-to-day commands (after `ssh` into the server)
```bash
journalctl -u timer-bot -f            # watch live logs (Ctrl+C to stop watching)
sudo systemctl restart timer-bot      # restart
sudo systemctl stop timer-bot         # stop
```

**Updating the code later:** copy the new zip over with `scp` like in step 3, then on the server run
`cd ~ && unzip -o discord-timer-bot.zip && sudo systemctl restart timer-bot`.
Your `.env` and `timers.db` aren't in the zip, so the update leaves them untouched.

**Keep an eye on credit:** in the portal, search **"Education"** to see your remaining credit.

---

## Oracle Cloud Always Free (forever-free alternative)
1. Sign up at https://www.oracle.com/cloud/free/ (needs a credit/debit card for verification).
2. **Compute → Instances → Create instance**. Image: **Canonical Ubuntu 24.04**.
   Shape: **VM.Standard.E2.1.Micro** (Always Free) or **Ampere A1.Flex** with 1 OCPU / 6 GB.
3. Download the SSH private key on the same page, create the VM, and copy its public IP.
4. Do steps 3–5 from the Azure guide, using `ubuntu` as the username instead of `azureuser`.

## Railway (easiest, paid)
1. Push this folder to a **private** GitHub repo. `.gitignore` already keeps your `.env` out of it.
2. On https://railway.com → New Project → Deploy from GitHub repo.
3. **Variables:** add `DISCORD_TOKEN`, `TIMEZONE=Asia/Kolkata`, `DB_PATH=/data/timers.db`.
4. **Add a Volume** mounted at `/data`. Without it, your timers are erased on every redeploy.
5. Settings → Start command: `python bot.py`.
