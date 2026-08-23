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

The live instance runs from `/home/arva/docker/holiday-radar` on
`192.168.1.35`. To push local changes to it:

```bash
rsync -az --exclude .venv --exclude data --exclude __pycache__ --exclude .git \
  ./ arva@192.168.1.35:/home/arva/docker/holiday-radar/ \
  && ssh arva@192.168.1.35 'cd /home/arva/docker/holiday-radar \
     && docker compose build --quiet && docker compose up -d'
```

`config.yaml`, `presets/` and `data/` are bind-mounted, so edits to those
take effect without a rebuild.

## Fetching one carrier without a full nightly

A nightly cycle re-queries the whole grid. To backfill a single source into
an existing database — after admitting a new carrier, say:

```bash
docker exec holiday-radar-web python -m app.cli fetch-wizz
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

## Deal alerts to Home Assistant

The night decides, the morning delivers. The 02:45 cycle queues anything
newsworthy; at 07:00 the daemon posts the queue as **one** digest. Three
things earn a place in it, and each is a *change* of state, never a state:

| Rule | Fires when |
|---|---|
| `buy` | effective cost crossed a tier's buy threshold downward |
| `new_low` | cheaper than anything ever recorded for that holiday × destination |
| `new_best` | the holiday's top-ranked destination changed |

A fare that merely holds is silent. A re-alert must beat the last one by
**both** `min_drop_pct` and `min_drop_eur`, or simply be `repeat_after_days`
stale — otherwise a €12 wobble on a €2000 trip would buzz every morning.

### Wiring it up

1. In Home Assistant, create an automation with a **Webhook** trigger and
   copy its URL (`https://<ha-host>/api/webhook/<id>`).
2. Put it in `.env` on the server as `HA_WEBHOOK_URL=` — the real host and
   id, not a placeholder — then **recreate** the containers:

   ```bash
   docker compose up -d --force-recreate
   ```

   Plain `docker compose up -d` reports "Running" and changes nothing when
   only `.env` differs. Secrets reach the containers via the `env_file:`
   directive; without it compose uses `.env` merely to substitute `${...}`
   inside the compose file, and the process itself sees nothing.
   With no webhook the radar runs exactly as before and sends nothing.
3. Check the plumbing without waiting for a real price drop:

```bash
docker exec holiday-radar-web python -m app.cli test-alert
```

### The payload

```yaml
automation:
  - alias: Flight deal
    trigger:
      - platform: webhook
        webhook_id: <id>
        allowed_methods: [POST]
        local_only: false        # only if the radar reaches HA from outside
    action:
      - service: notify.mobile_app_<device>
        data:
          title: "{{ trigger.json.title }}"
          message: >
            {{ trigger.json.best.detail }}
          data:
            url: "https://<radar-host>/#/h/{{ trigger.json.best.holiday_id }}/{{ trigger.json.best.destination }}"
```

`trigger.json` carries `count`, `title`, `headline`, `summary`, `best` (the
cheapest find, fully described) and `alerts` (all of them). Each entry has
`effective_eur`, `flights_eur`, `logistics_eur`, `layover_hotel_eur`,
`out_date`, `back_date`, `nights`, `origin`, `airlines`, `is_direct`,
`layover`, `layover_overnight`, `times`, `school_days`, `climate_c`, `deal`,
`score`, `previous_eur` and a ready-made `detail` line.

Prices are **indicative screening numbers** — `confidence` says so. Google's
cached fares can lack seats for four, so the booking price may differ.

### Tuning

Everything lives under `preferences.alerts` in `config.yaml`:
`enabled`, `deal_levels`, `min_drop_pct`, `min_drop_eur`,
`repeat_after_days`, `max_per_run` and `deliver_cron` (the 07:00 slot).
