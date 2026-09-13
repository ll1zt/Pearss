# The tunnel: what is public, what stays on the tailnet, and why.
#
# Two entry points, deliberately different:
#
#   tailnet  (serve,  :8443)  -> Pearss directly. Every endpoint. Agent
#                                clients, /v1 and /mcp live here.
#   internet (funnel, :10000) -> nginx, which forwards only
#                                /api/greader.php to Pearss and 404s the rest.
#
# The funnel side goes through nginx because Funnel cannot filter by path. Its
# `--set-path` prepends a path to requests; it does not restrict which paths
# reach the backend, and a Funnel target is a whole service. So the filter has
# to live in a proxy in front of it.
#
# Doing it in nginx rather than in the application is deliberate. The app's
# trust model stays single: everything that reaches it has already passed
# whatever rules its entry point imposes. An application-side "is this request
# from the funnel?" check would be a second, weaker trust boundary -- harder to
# test (the tests would have to forge the distinction) and easy to get wrong in
# a way no test notices.
#
# Ports: Funnel accepts only 443, 8443 and 10000, and Tailscale forbids the same
# *external* port for serve and funnel at once. 8443 and 10000 are therefore the
# pair, and nginx listens on 127.0.0.1:10000 as Funnel's local target.
{ config, pkgs, ... }:

{
  # --------------------------------------------------------------------- serve
  # The whole application, reachable only from the tailnet. TLS terminates at
  # Tailscale with a certificate for pearss.example.ts.net.
  systemd.services.tailscale-serve-pearss = {
    description = "Expose Pearss on the tailnet (serve :8443)";
    after = [ "tailscaled.service" "pearss.service" ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.tailscale}/bin/tailscale serve --bg --https=8443 \
        http://127.0.0.1:${toString config.services.pearss.port}
    '';
    preStop = ''
      ${pkgs.tailscale}/bin/tailscale serve --https=8443 off || true
    '';
  };

  # -------------------------------------------------------------------- funnel
  # The public side. nginx is the target, and nginx is what decides which paths
  # exist. The Reader API is the only public surface (ADR 0016): NetNewsWire
  # speaks it, so RSS clients lose nothing, and /v1, /mcp, /docs and the health
  # panel stay unreachable from the internet.
  services.nginx = {
    enable = true;

    # Funnel's local target. Bound to loopback so nothing else on the LAN can
    # reach the proxied app through this port.
    virtualHosts."pearss-funnel" = {
      listen = [
        {
          addr = "127.0.0.1";
          port = 10000;
        }
      ];
      # A Host the funnel sets; nginx answers it and nothing else.
      serverName = "pearss.example.ts.net";

      locations."/api/greader.php" = {
        proxyPass = "http://127.0.0.1:${toString config.services.pearss.port}";
        extraConfig = ''
          # The app's rate limiter counts 401/403 by client address, and it
          # reads that address from X-Forwarded-For. Without these two lines
          # every public caller looks like 127.0.0.1 and one attacker's failures
          # would rate-limit everyone -- or nobody.
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header Host $host;
        '';
      };

      # Everything else is not there. 404 rather than 403: a public caller
      # learns nothing about what exists behind the proxy.
      locations."/" = {
        return = "404";
      };
    };
  };

  systemd.services.tailscale-funnel-pearss = {
    description = "Expose the Reader API publicly (funnel :10000)";
    after = [
      "tailscaled.service"
      "nginx.service"
      "pearss.service"
    ];
    wants = [
      "tailscaled.service"
      "nginx.service"
    ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.tailscale}/bin/tailscale funnel --bg --https=10000 \
        http://127.0.0.1:10000
    '';
    preStop = ''
      ${pkgs.tailscale}/bin/tailscale funnel --https=10000 off || true
    '';
  };

  # Funnel needs an explicit grant in the tailnet ACL:
  #
  #   "nodeAttrs": [
  #     { "target": ["pearss-host"], "attr": ["funnel"] }
  #   ]
  #
  # Without it `tailscale funnel` fails with a message about the node not being
  # allowed to use Funnel. That is an ACL edit in the admin console, not
  # something these files can do.
}
