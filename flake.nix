{
  description = "Pearss — single-user, agent-first RSS+ system";

  # Flake inputs are declared with git+https rather than github: on purpose.
  # `github:` resolves refs through api.github.com, which rate-limits
  # unauthenticated CI runners hard (HTTP 403 mid-`nix flake check`). git+https
  # talks to the git protocol directly and has no such quota. See ADR 0001.
  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?ref=nixos-unstable&shallow=1";
    systems.url = "git+https://github.com/nix-systems/default?shallow=1";
  };

  outputs =
    {
      self,
      nixpkgs,
      systems,
      ...
    }:
    let
      forEachSystem = nixpkgs.lib.genAttrs (import systems);
      pkgsFor = system: nixpkgs.legacyPackages.${system};
    in
    {
      # The deployable artifact. Section 10.1's line is preserved: this wraps a
      # uv-managed environment, so the Python dependency graph still comes from
      # uv.lock and not from nixpkgs. There is deliberately no second, parallel
      # expression of the same dependencies.
      packages = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.callPackage ./nix/package.nix { };
        }
      );

      # The NixOS module: declares the service, its user, its state directory
      # and its hardening. Configuration lives in the host's configuration.nix;
      # only secrets go through an environment file (never the Nix store).
      nixosModules.default = ./nix/module.nix;
      nixosModules.pearss = self.nixosModules.default;

      # The tunnel split is a separate module because it is optional and host
      # specific: a deployment behind a different proxy, or with no public
      # entry point at all, imports only the service module above.
      nixosModules.tunnel = ./nix/tunnel.nix;

      devShells = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python313;

          # Section 10.1 draws the responsibility line: Nix owns the
          # interpreter, system libraries and CLI tooling; uv owns the Python
          # dependency graph. Project runtime libraries (fastapi, sqlalchemy,
          # ...) are deliberately absent here — they come from uv.lock, so the
          # two lockfiles never contend for the same package.
          basePackages = with pkgs; [
            python
            uv
            just
            git
            sqlite
            ruff
            mypy
          ];

          # Section 10.2: heavyweight rendering dependencies stay out of the
          # default shell so it stays fast to enter.
          crawlerPackages = basePackages ++ (with pkgs; [ python313Packages.playwright ]);

          mkPearssShell =
            { name, packages, greeting }:
            pkgs.mkShell {
              inherit name;
              packages = packages;

              # Secrets are never set here: a value in this file lands in the
              # Nix store, which is world-readable (section 10.5). They come
              # from a gitignored .env or the deploy platform.
              env = {
                PEARSS_ENV = "development";
                # Point uv at the Nix interpreter and a project-local cache so
                # the venv is reproducible from uv.lock alone.
                UV_PYTHON = "${python}/bin/python3";
                UV_CACHE_DIR = ".cache/uv";
                # onnxruntime (ADR 0035 decision 5) arrives as a manylinux
                # wheel, whose shared objects reference libstdc++ and libz by
                # soname. Nix puts neither on the default loader path, so the
                # two entries come from here -- the same division section 10.1
                # draws above: Nix owns the system libraries, uv owns the Python
                # dependency graph. The systemd unit runs the service through
                # `nix develop`, so this covers development, CI and deployment.
                LD_LIBRARY_PATH = "${pkgs.stdenv.cc.cc.lib}/lib:${pkgs.zlib}/lib";
                PYTHONDONTWRITEBYTECODE = "1";
                PYTHONUNBUFFERED = "1";
              };

              shellHook = ''
                echo "pearss:${name} — python $(python3 --version | cut -d' ' -f2), uv $(uv --version | cut -d' ' -f2)"
                echo "${greeting}"
              '';
            };
        in
        {
          default = mkPearssShell {
            name = "default";
            packages = basePackages;
            greeting = "run 'just sync' to install Python deps, then 'just ci' to verify";
          };

          crawler = mkPearssShell {
            name = "crawler";
            packages = crawlerPackages;
            greeting = "crawler profile: Playwright available for render-dependent collectors";
          };
        }
      );

      # `nix flake check` is the first CI gate (section 10.4). The shells must
      # evaluate; the real gate is `just ci` inside them.
      formatter = forEachSystem (system: nixpkgs.legacyPackages.${system}.nixpkgs-fmt);
    };
}
