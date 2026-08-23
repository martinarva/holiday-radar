# Deployment

Two containers from one image, sharing a data volume:

| Service | Role | Network |
|---|---|---|
| `holiday-radar-web` | read-only UI + JSON API (SQLite snapshot only) | publishes `RADAR_PORT` |
| `holiday-radar-scheduler` | nightly full-grid collection | outbound only |

Both are `restart: unless-stopped`, so a host reboot brings them back.

## Install

```bash
git clone https://github.com/martinarva/holiday-radar.git
cd holiday-radar
printf 'RADAR_PORT=8770\nTZ=Europe/Tallinn\n' > .env
docker compose up -d --build
```

Everything persistent lives in `./data`:

- `radar.db` — observations, offers, watch state, verifications, runs
- `climate_normals.json` — one-off Open-Meteo climatology (copy it along
  when moving hosts; otherwise the first run refetches ~120 API calls)

`config.yaml` and `presets/` are bind-mounted read-only, so a config change
is an edit plus `docker compose restart`, with no rebuild.

## Schedule

The scheduler waits for `scheduler.screen_cron` (default 02:45
`Europe/Tallinn`). On start it does a **catch-up** run if today's slot has
already passed and no run is recorded for tonight — so the first boot fills
the database immediately instead of idling until the next night. Whether
tonight already ran is read from the `runs` table, never from memory, so a
restart mid-evening neither double-runs nor skips.

Manual run:

```bash
docker compose exec scheduler python -m app.cli nightly
docker compose exec scheduler python -m app.cli coverage-report
```

## Reverse proxy

The app only publishes a port; TLS and the hostname belong to the proxy.
On this network `*.arvahome.org` resolves to the nginx host, which proxies
to the docker host. The vhost:

```nginx
server {
    listen 80; listen 443 ssl;
    server_name radar.arvahome.org;
    include snippets/ssl-arvahome.conf;
    location / {
        proxy_pass http://192.168.1.35:8770;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

`nginx -t && systemctl reload nginx` after adding it.

**There is no authentication.** The UI is intended for a home network behind
a proxy; do not expose it to the internet without putting auth in front.

## Updating

```bash
git pull
docker compose up -d --build      # data/ and config.yaml survive
```

## Health

```bash
curl http://localhost:8770/health
docker logs -f holiday-radar-scheduler
```

`/health` reports the latest data night, total observations and the last
run with its error count; the System screen in the UI shows the same plus
coverage, provider status and budget usage.
