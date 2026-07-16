# Run with Docker Compose

The Compose setup runs the gateway on port 8000, mounts `config.yaml` read-only,
and stores SQLite data in the named `gateway_data` volume. Provider and gateway
keys are supplied through `.env`; they are not copied into the image.

From the repository root:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# Edit .env and set the five real/local keys.
docker compose up --build
```

The container exposes:

```text
http://127.0.0.1:8000/healthz
http://127.0.0.1:8000/v1/
http://127.0.0.1:8000/dashboard
```

The dashboard and V1 endpoints require the gateway bearer key:

```bash
curl http://127.0.0.1:8000/healthz
curl -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
  http://127.0.0.1:8000/dashboard
```

Stop the service with `docker compose down`. The request ledger remains in
`gateway_data`; `docker compose down -v` also deletes that volume and its data.

This file only describes local/container startup. It does not configure a TLS
reverse proxy or deploy to a VPS.
