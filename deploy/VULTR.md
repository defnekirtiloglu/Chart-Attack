# Deploy OSCEnario Rounds on Vultr (about 10 minutes)

1. **Create the server.** Vultr dashboard → Deploy → Cloud Compute (Shared CPU) → any US region →
   image: **Marketplace → Docker** on Ubuntu → the cheapest plan with 2 GB RAM → Deploy.
   Copy the server's **IP address** and **root password**.

2. **Log in and get the code.**
   ```bash
   ssh root@YOUR_IP
   git clone https://github.com/OWNER/REPO.git rounds && cd rounds
   ```
   (Private repo: use a GitHub personal access token as the password.)

3. **Add your keys.** Never commit this file.
   ```bash
   cp .env.example .env && nano .env      # paste GEMINI / ANTHROPIC / ELEVENLABS keys
   ```
   Optional HTTPS: point a domain's A record at YOUR_IP, then add `DOMAIN=rounds.yourdomain.com` to `.env`.

4. **Start it.**
   ```bash
   docker compose up -d --build
   docker compose logs -f app       # look for "Uvicorn running"
   ```

5. **Open it:** `http://YOUR_IP` (or `https://your domain`). It goes straight to the portal.
   Check `http://YOUR_IP/health`: it shows `storage` and `case_author`.

Update after a new push: `git pull && docker compose up -d --build`.
The microphone button needs HTTPS (a domain). Typing works without it.
