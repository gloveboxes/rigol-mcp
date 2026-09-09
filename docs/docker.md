# Docker

This page describes the Docker runtime. [Apple Container](apple-container.md) is
also supported on Apple silicon. No host Python or uv installation is needed;
those tools are contained in the build environment.

The image uses Python 3.12 Alpine and a multi-stage build. Only the installed
application and locked runtime dependencies are copied into the final stage;
build tools, tests, local configuration and captures are excluded. It runs as
UID/GID 10001, without USB libraries or a native VISA runtime.

The local ARM64 build measured 31.4 MB by `docker image inspect`, compared with
54.6 MB for the equivalent Debian slim build. Sizes vary by architecture and
base-image updates; these are Docker-reported image sizes, not RAM requirements.

## Build and Run

```sh
docker build -t rigol-mcp:local .
docker run --rm -i --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop=ALL --security-opt=no-new-privileges \
  -e RIGOL_IP=192.168.1.43 \
  --mount type=volume,src=rigol-mcp-data,dst=/data \
  rigol-mcp:local
```

Replace the IP with your scope's LAN address. The process waits for MCP requests
on stdin; it is normally launched by an MCP client, not used as an interactive
terminal. Use `-i`, **not `-t`**: allocating a TTY can corrupt the stdio protocol.
No port publishing is needed. Port 5555 is the scope's destination TCP port, not
a port listened on by this container.

Docker's network must be able to reach the scope. Docker Desktop normally routes
outbound LAN traffic without host networking; VPN/firewall policies may prevent
it. Do not use `--network=none` when connecting to hardware.

## MCP Client Configuration

For clients that use an `mcpServers` configuration section, the following is an
example. Other clients use different configuration layouts; adapt the process
command, arguments and environment to your client's format.

```json
{
  "mcpServers": {
    "rigol": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "-e", "RIGOL_IP=192.168.1.43",
        "--mount", "type=volume,src=rigol-mcp-data,dst=/data",
        "rigol-mcp:local"
      ]
    }
  }
}
```

The Docker daemon must be running, and `docker` must be on the client's PATH.
Launch one server instance per physical scope; separate containers do not share
the server's instrument lock and could interfere with one another.

## Captures and Configuration

The named volume preserves `/data/captures` and `/data/screenshots` after the
container exits. Returned file paths are container paths, not host paths.
`read_capture` retrieves bounded excerpts through MCP; `scpi_execute` can restore
a setup using its saved `data_path` without base64 passing through the agent.
For full file access on the host, use a bind mount instead of the named volume,
and ensure UID/GID 10001 can write to that directory.

Configuration is supplied through `-e`; Docker's `--env-file` is optional, not
required. Automatic dotenv loading is disabled in the image, and no scope IP or
environment file is baked in.
`RIGOL_ENABLE_SEND_RAW` remains disabled by default; documented DHO814 catalog
operations are available without that flag. Those operations can change or reset
the scope, so only trusted clients should have access.

## Stdio or HTTP?

**Keep stdio for a local client launching the container.** It has no listening
server, authentication setup, or additional service lifecycle. The client starts
the container and communicates over Docker's stdin/stdout pipes.

**Use Streamable HTTP for an independently running or remote service.** That would
require an additional server transport, authentication/authorization, TLS or a
trusted reverse proxy, and coordination of multiple clients controlling one scope.
Publishing Docker ports alone does not convert a stdio server to HTTP.

This image intentionally remains stdio-only. Docker does not itself make HTTP a
better choice, and sharing a physical instrument between clients needs explicit
coordination beyond simply switching transports.

## Container Tests

These tests use no network or hardware. They verify non-root execution with a
read-only root filesystem, writable capture storage, the packaged command
catalog, absence of build/test/USB packages, and modern/legacy MCP stdio clients.
CI builds the image and runs them on its Python 3.12 job. Normal unit tests skip
them unless `RIGOL_TEST_IMAGE` is set. For Docker-only local test commands and
the additional permissions needed by image smoke tests, see
[development checks](development.md).