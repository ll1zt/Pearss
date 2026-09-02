{
  description = "Pearss — single-user, agent-first RSS+ system";

  # Flake inputs are declared with git+https rather than github: on purpose.
  # `github:` resolves refs through api.github.com, which rate-limits
  # unauthenticated CI runners hard (HTTP 403 mid-`nix flake check`). git+https
  # talks to the git protocol directly and has no such quota. See ADR 0001.
  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?ref=nixos-unstable&shallow=1";
    devenv = {
      url = "git+https://github.com/cachix/devenv?shallow=1";
      # devenv's own nixpkgs pin is only used to build the devenv CLI. Following
      # our nixpkgs keeps a single nixpkgs in the closure.
      inputs.nixpkgs.follows = "nixpkgs";
    };
    systems.url = "git+https://github.com/nix-systems/default?shallow=1";
  };

  nixConfig = {
    extra-trusted-public-keys = "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=";
    extra-substituters = "https://devenv.cachix.org";
  };

  outputs =
    { self, nixpkgs, devenv, systems, ... }@inputs:
    let
      forEachSystem = nixpkgs.lib.genAttrs (import systems);
    in
    {
      devShells = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python313;

          # Section 10.1 draws the responsibility line: Nix owns the interpreter,
          # system libraries, and CLI tooling; uv owns the Python dependency
          # graph. Project runtime libraries (fastapi, sqlalchemy, ...) are
          # deliberately absent here — they come from uv.lock, so the two
          # lockfiles never disagree about who installs what.
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
            devenv.lib.mkShell {
              inherit inputs pkgs;
              modules = [
                ./devenv.nix
                {
                  packages = packages;

                  # Point uv at the Nix-provided interpreter and a project-local
                  # cache, so the venv is reproducible and .cache/ stays ignored.
                  env.UV_PYTHON = "${python}/bin/python3";
                  env.UV_CACHE_DIR = ".cache/uv";
                  env.PEARSS_SHELL = name;

                  enterShell = ''
                    echo "pearss:${name} — python $(python3 --version | cut -d' ' -f2), uv $(uv --version | cut -d' ' -f2)"
                    echo "${greeting}"
                  '';
                }
              ];
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

      # `nix flake check` is the first CI gate (section 10.4). It stays cheap:
      # the shells must evaluate and the Nix sources must be formatted, but we
      # do not build the world here. The real gate is `just ci` inside the shell.
      formatter = forEachSystem (system: nixpkgs.legacyPackages.${system}.nixpkgs-fmt);
    };
}
