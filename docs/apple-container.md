# Apple Container

On Apple silicon Macs, Apple's `container` CLI can run the same Alpine Linux
image without Docker Desktop. This is an alternative container runtime, not a
native Python installation. It was tested with **container 1.4.1** on this Mac.

## Build and Prepare

From the repository root:

```sh
container system start
container build -t rigol-mcp:local .
```

Apple Container and Docker have **separate image and volume stores**. Building
or rebuilding in Docker does not update Apple's copy of `rigol-mcp:local`.

Initialize the Apple capture volume once. Unlike Docker, Apple Container does
not populate a new named volume with the image's directory ownership. Without
this step the non-root server cannot save captures:

```sh
container run --rm --progress none --user 0 \
  --mount type=volume,source=rigol-mcp-data,target=/data \
  --entrypoint sh rigol-mcp:local -c \
  'mkdir -p /data/captures /data/screenshots && chown 10001:10001 /data /data/captures /data/screenshots'
```

The runtime creates the named volume if it does not exist. This setup command
only creates capture directories and sets their ownership; it does not connect
to the scope. Subsequent server launches run as UID/GID 10001, not root.

## VS Code

The workspace [MCP configuration](../.vscode/mcp.json) contains alternatives:

| Server | Runtime |
| --- | --- |
| `rigol-docker` | Docker |
| `rigol-apple-container` | Apple Container |

Use **MCP: List Servers** to stop/disable the unwanted server and start/enable
the selected one. **Do not use both against the same scope.** VS Code keeps its
enable/disable choices separately from the shared configuration file; adding an
entry does not select it as the exclusive runtime.

Edit `env.RIGOL_IP` in the selected entry for your scope. Both entries pass the
variable into their container with `-e RIGOL_IP`. The `container` executable must
be on VS Code's PATH. Restart the selected server after rebuilding its image.

Equivalent command-line launch:

```sh
export RIGOL_IP=192.168.1.43
container run --rm -i --read-only --progress none \
  --tmpfs /tmp --cap-drop ALL \
  -e RIGOL_IP \
  --mount type=volume,source=rigol-mcp-data,target=/data \
  rigol-mcp:local
```

Keep `-i` and omit `-t`: MCP uses stdin/stdout, not a terminal. No listening
port or HTTP endpoint is added. Docker's `--security-opt` flag is not supported
by this CLI; the Apple launch uses its supported read-only filesystem and
capability-drop options rather than passing incompatible flags.

## Storage and Networking

Captures persist in Apple's `rigol-mcp-data` volume under `/data/captures` and
`/data/screenshots`. This is not Docker's volume even though the name matches.
Returned paths are inside the container; use `read_capture` for bounded excerpts.

The runtime must reach the scope's LAN IP on TCP port 5555. macOS firewall,
VPN and network policies may affect that access. The container runs Linux in
Apple's virtualization environment, not directly as a macOS process.

Validated locally: Dockerfile build, non-root capture writes, MCP initialization,
44-tool discovery, and catalog queries using the exact VS Code launch arguments.
No physical scope calls were made during Apple-runtime validation. Docker-based
development and CI workflows remain documented in [development checks](development.md).
