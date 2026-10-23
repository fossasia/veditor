# Production deployment

This directory contains the production stack. It runs the API and both RQ
workers from the published `fossasia/veditor` image, while PostgreSQL and Redis
keep their data in named Docker volumes.

## Provision a host

1. Install Docker Engine and the Compose plugin.
2. Create a deployment directory and copy `docker-compose.yml` and `env.sample`
   into it as `docker-compose.yml` and `.env`.
3. Set a strong database password and `SESSION_SECRET` (at least 32 bytes).
4. Log in to Docker Hub if the repository is private.
5. Start the stack:

   ```bash
   docker compose pull
   docker compose up -d
   docker compose ps
   ```

The API waits for PostgreSQL and Redis, runs `alembic upgrade head` once, and
then starts Uvicorn. Workers wait for a healthy API, so they cannot consume
queued jobs until migrations have completed. Nickfedor Watchtower checks the
production `latest` image every five minutes and only updates containers
carrying its enable label. Worker updates use a one-hour graceful shutdown
deadline so active RQ jobs can drain before replacement.

Watchtower talks to Docker through a restricted socket-proxy service rather
than mounting the Docker socket directly. The proxy permits only the container,
image, info, and required write operations used for image replacement.

Workers also wait for the API health endpoint at startup. This remains
effective during Watchtower restarts, ensuring migrations finish before a
restarted worker can consume queued jobs.

Redis uses `appendfsync always` so an acknowledged enqueue is persisted before
the write returns. This reduces throughput, so monitor Redis latency and
consider a transactional outbox if queue volume later requires higher
performance.

The API binds to `127.0.0.1` by default. Put Nginx or Caddy in front of it for
public HTTPS access; do not expose Uvicorn directly to the internet.

## Reverse proxy and HTTPS

Keep port 8000 private to the host or firewall and put Nginx, Caddy, or another
TLS reverse proxy in front of it. Proxy HTTPS traffic to
`http://127.0.0.1:8000`, forward the `Host` and `X-Forwarded-Proto` headers,
and configure `BASE_URL` to the public HTTPS URL. Obtain and renew the
certificate in the proxy rather than in the application container.

## Operations

Check service logs with `docker compose logs -f api` or
`docker compose logs -f worker-heavy`. Back up PostgreSQL before planned
changes, and do not remove the named volumes: they contain the database,
Redis queue data, uploaded recordings, and generated video files.

Production releases from `main` publish both the `main` and `latest` images.
Pushes to `dev` publish only the `dev` image and cannot update this production
stack. To apply a new production image immediately instead of waiting for
Watchtower's five-minute poll:

```bash
docker compose pull
docker compose up -d
```

## Existing root-Compose deployments

The production file uses explicit volume names. The defaults match the named
volumes created by a root Compose project named `veditor`. Before switching an
existing host, inspect the actual names:

```bash
docker volume ls | grep veditor
```

Set `POSTGRES_VOLUME_NAME`, `REDIS_VOLUME_NAME`, `DATA_VOLUME_NAME`, and
`INGEST_VOLUME_NAME` in `.env` to those existing names. Use unique names for an
isolated test stack. Never run `docker compose down -v` until the volume
migration has been verified.
