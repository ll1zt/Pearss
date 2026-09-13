# The NixOS module: declares the service, its state and its isolation.
#
# Configuration lives here; only secrets go through an environment file, so no
# credential ever reaches the Nix store (AGENTS.md, architecture section 10.5).
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.pearss;
  inherit (lib) mkIf mkOption mkEnableOption types;
in
{
  options.services.pearss = {
    enable = mkEnableOption "Pearss, a single-user agent-first RSS+ system";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "pearss.packages.\${system}.default";
      description = "The Pearss package to run.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/pearss-env";
      description = ''
        File holding PEARSS_* configuration and credentials, read by systemd at
        start. Keep it out of the world-readable Nix store: use sops-nix, or any
        file with mode 0400 owned by {option}`services.pearss.user`.
      '';
    };

    user = mkOption {
      type = types.str;
      default = "pearss";
      description = "User the service runs as.";
    };

    uid = mkOption {
      type = types.int;
      default = 993;
      description = ''
        The service's UID, and this default is load-bearing rather than
        cosmetic.

        On an Android host (a Droidspaces container on a tablet, which is this
        project's deployment target) the host's netd installs an eBPF program
        that filters *inbound* packets by the listening socket's UID: UIDs below
        10000 are passed unconditionally, anything at or above reaches a map
        lookup and is dropped when not allowlisted. The symptom is a service
        that starts, logs that it is listening, answers `curl` from inside the
        container -- and is unreachable from the host or the tailnet.

        NixOS's `DynamicUser` allocates from a high range, well past that
        threshold, so it cannot be used here. Keeping this value under 10000 is
        what makes the service reachable at all.
      '';
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Address to bind. The default keeps the service off the network: a tunnel
        (see ADR 0016) is the intended way to expose it, and it terminates TLS
        and applies the public/private split the app cannot do for itself.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8000;
      description = "Port to bind on {option}`services.pearss.host`.";
    };

    forwardedAllowIps = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Which peers may set forwarding headers. The auth rate limiter counts
        failures by client address taken from `X-Forwarded-For`, so this must
        name the tunnel's proxy and nothing else -- trusting a wider set lets a
        caller forge the address and evade the limit.
      '';
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/pearss";
      description = ''
        Directory holding the SQLite database and the content-addressed body
        store. Both are fact sources (docs/domain/data-model.md), so this is
        what backup must capture.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open {option}`services.pearss.port`. Off by default: the intended path is a tunnel, not an exposed port.";
    };
  };

  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
      # Fixed, not dynamic: see the uid option.
      uid = cfg.uid;
      home = cfg.stateDir;
    };
    users.groups.${cfg.user} = { };

    # The database and body store live here. systemd creates the directory with
    # the right owner, which keeps the unit from needing any privileged setup
    # step of its own.
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 ${cfg.user} ${cfg.user} - -"
    ];

    systemd.services.pearss = {
      description = "Pearss — single-user, agent-first RSS+ system";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = {
        PEARSS_BIND_HOST = cfg.host;
        PEARSS_PORT = toString cfg.port;
        PEARSS_FORWARDED_ALLOW_IPS = cfg.forwardedAllowIps;
        PEARSS_DATABASE_URL = "sqlite+pysqlite:///${cfg.stateDir}/pearss.db";
        PEARSS_STORAGE_DIR = "${cfg.stateDir}/bodies";
      };

      # Migrations run before the server: a schema behind the code is the one
      # startup state that must never be served from. PYTHONPATH is required
      # here for the same reason the launcher sets it -- the packaged venv holds
      # only the dependencies (`--no-install-project`), so `migrations/env.py`
      # cannot import `app` or `adapters` without being told where the source is.
      preStart = ''
        export PYTHONPATH="${cfg.package.passthru.projectRoot}/src''${PYTHONPATH:+:$PYTHONPATH}"
        ${cfg.package.passthru.venv}/venv/bin/python -m alembic \
          -c ${cfg.package.passthru.projectRoot}/alembic.ini upgrade head
      '';

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        WorkingDirectory = cfg.stateDir;

        ExecStart = "${cfg.package}/bin/pearss";
        EnvironmentFile = mkIf (cfg.environmentFile != null) cfg.environmentFile;

        Restart = "on-failure";
        RestartSec = 5;

        # Hardening. Each of these is checked against what the Droidspaces
        # container actually supports, because several systemd sandbox options
        # fail there: IPAddressDeny needs a BPF program attached to the unit's
        # cgroup, and the container's kernel refuses that attach. What remains
        # is what was verified to work.
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        # ReadWritePaths, not StateDirectory=: the state directory is created by
        # the tmpfiles rule above, and naming it here keeps ProtectSystem=strict
        # from making it read-only.
        ReadWritePaths = [ cfg.stateDir ];
      };
    };

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];
  };

  meta.maintainers = [ ];
}
