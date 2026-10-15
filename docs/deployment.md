# ChunkWise Dashboard Deployment

ChunkWise is a single-process Python service with a local SQLite index and a
filesystem chunk store. The simplest deployment is one container or one
persistent-disk application instance. Do not scale it horizontally: SQLite and
the local chunk directory are a single-writer, single-machine store.

The application has no authentication or multi-user isolation. Keep it private
or put it behind your hosting platform's access-control layer; do not publish
an unauthenticated storage dashboard to the public internet.

## Run locally

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
CHUNKWISE_HOME="$PWD/.chunkwise" CHUNKWISE_HOST=127.0.0.1 PORT=8080 chunkwise-web
```

Open `http://127.0.0.1:8080`. The service uses only the Python standard library
at runtime. The UI is served by the same process and makes same-origin requests.

## Environment

| Variable                     | Default                       | Purpose                                                          |
| ---------------------------- | ----------------------------- | ---------------------------------------------------------------- |
| `CHUNKWISE_HOME`             | `./.chunkwise`                | Private directory containing `index.sqlite3` and chunk data.     |
| `CHUNKWISE_HOST`             | `0.0.0.0`                     | Listen interface. Set to `127.0.0.1` for local-only development. |
| `PORT`                       | `CHUNKWISE_PORT`, then `8080` | Hosting-platform port. `PORT` takes precedence.                  |
| `CHUNKWISE_MAX_UPLOAD_BYTES` | `104857600`                   | Maximum request body accepted by the upload API (100 MiB).       |

Set the platform's port variable to the HTTP port. The app has no database or
cloud-storage service to configure. The browser only receives the dashboard and
API responses; the storage home is never a static-file root.

## Docker

Build and run with a named persistent volume:

```sh
docker build -t chunkwise .
docker volume create chunkwise-data
docker run --name chunkwise --restart unless-stopped \
  -p 8080:8080 \
  -e PORT=8080 \
  -e CHUNKWISE_HOME=/data/chunkwise \
  -v chunkwise-data:/data/chunkwise \
  chunkwise
```

The image runs as UID 10001. Keep `/data/chunkwise` on persistent storage and
include it in backups. A container's writable layer is not durable across
replacement. Do not mount the data directory into a web-server document root,
and do not run multiple service replicas against separate ephemeral disks.

For a hosting platform, deploy this Dockerfile as one service, attach one
persistent disk at `/data/chunkwise`, set `CHUNKWISE_HOME=/data/chunkwise`, and
allow the platform-assigned `PORT` through its HTTP proxy. The app binds to
`0.0.0.0` by default. TLS termination should be handled by the platform proxy.

This configuration is deployment-ready code, not a claim that the application
has been deployed to a hosting provider.
