# Deployment notes for running Pearss on a Droidspaces NixOS container.
#
# The concrete host files live in the host-config repository, not here: this
# document says what they must contain and why, so the reasoning travels with
# the module instead of being rediscovered on the next host.
#
#   deployment target: pearss-host (Lenovo Legion Y700, Droidspaces container)
#   hostname:          pearss.example.ts.net

## 1. Add the flake input

```nix
# host-config/flake.nix, in `inputs`
pearss = {
  url = "path:/path/to/pearss";   # or git+https once published
  inputs.nixpkgs.follows = "nixpkgs";      # one nixpkgs, so the module and the
};                                          # host agree on every package
```

`inputs.nixpkgs.follows` is not decoration: without it the module's package and
the host's systemd come from different nixpkgs revisions, and the service starts
against a systemd it was not built beside.

## 2. Import the module and enable the service

```nix
# host-config/hosts/<host>/pearss.nix
{ ... }:
{
  imports = [ ];   # the module comes from the flake, added in flake.nix below

  services.pearss = {
    enable = true;
    host = "127.0.0.1";   # the tunnel terminates TLS and splits public/private
    port = 8000;

    # Secrets never enter the Nix store: this file is read by systemd at start
    # and must be mode 0400 owned by the service user.
    environmentFile = "/run/secrets/pearss-env";
  };
}
```

Then pass the module in the host builder:

```nix
# host-config/flake.nix, inside mkNixosContainerSystem / mkNixosSystem
modules = [
  ./hosts/${hostName}/default.nix
  inputs.pearss.nixosModules.default        # <- added
];
```

## 3. Create the environment file

`/run/secrets/pearss-env`, mode `0400`, owned by `pearss`. Its contents are the
same variables `.env.example` documents; the ones that matter for a fresh
deployment:

```
PEARSS_API_TOKEN=<random>
PEARSS_MCP_TOKEN=<random>
PEARSS_READER_USERNAME=reader
PEARSS_READER_PASSWORD=<random>
PEARSS_DOCS_ENABLED=0
```

`PEARSS_DOCS_ENABLED=0` is not optional on a host that will be reachable from
the tailnet: it removes `/docs`, `/redoc` and `/openapi.json`, which otherwise
publish the full API surface to anyone who can reach the port.

`PEARSS_BIND_HOST`, `PEARSS_PORT` and `PEARSS_DATABASE_URL` are set by the
module; do not duplicate them here.

## 4. Deploy

```bash
cd ~/host-config
git add -A && git commit -m "add pearss"
sudo nixos-rebuild switch --flake .#pearss-host
systemctl status pearss
```

The first start runs the migrations (the unit's `ExecStartPre`) before the
server binds, so the database is created on an empty host with no manual step.

## 5. What was verified before writing this

Everything below was run on the target host, not inferred:

| Check | Result |
| --- | --- |
| `nix build .#packages.aarch64-linux.default` | succeeds; lxml/nh3/uvloop aarch64 wheels resolve from uv.lock |
| Two builds byte-compared | identical, given `uv venv --relocatable` and `--no-install-project` |
| Service under the full hardening set (`ProtectSystem=strict`, `PrivateTmp`, `ProtectHome`, `NoNewPrivileges`, `LockPersonality`, `RestrictSUIDSGID`, `ProtectKernel*`, `ProtectControlGroups`) | starts, `/healthz` 200 |
| `ExecStartPre` migrations under the same hardening | exit 0 |
| Four sandboxed collector children | all start |
| `/v1/sources` without a token | 401 |
| `/v1/sources` with a token | 200 |

## 6. Two constraints that are specific to this host

**The UID must stay below 10000.** The module's default is 993, and that is
load-bearing. Android's netd installs a cgroup eBPF program that filters
*inbound* packets by the listening socket's UID: `< 10000` passes
unconditionally, anything at or above goes through a map lookup and is dropped
when not allowlisted. The symptom is nasty because it looks like a firewall: the
service starts, logs that it is listening, answers `curl` from inside the
container, and is unreachable from the tailnet. NixOS's `DynamicUser`
allocates from a high range, which is why the module uses a fixed UID.

**`sandbox = false` is already the host's setting, and the package build needs
it.** The container's kernel refuses to mount `/proc` inside a user namespace
(`mount_too_revealing`), so Nix cannot sandbox builds there at all. The
consequence is that the package is not a reproducible build in the strict
sense — it is built without isolation — which is recorded in ADR 0020 rather
than glossed over. Download-and-switch operations are unaffected.

## 7. Next: the tunnel

Not yet configured. The shape decided for it (ADR 0016 plus the funnel
limitation) is:

- `tailscale serve --bg --https=8443 localhost:8000` — whole app, tailnet only.
  This is the path agent clients and `/v1` should use.
- `tailscale funnel --bg --https=10000` — pointed at nginx, **not** at Pearss,
  with nginx forwarding only `/api/greader.php`. Funnel cannot filter by path,
  so the path filter has to live in the proxy in front of it. That also keeps
  the application's trust model unchanged: everything that reaches the app has
  already passed the proxy's rules.

Serve and Funnel must use different *external* ports (Tailscale forbids the same
port for both); both may proxy to the same backend.
