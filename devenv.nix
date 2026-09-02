{ pkgs, lib, config, ... }:
{
  # Environment shared by every Pearss dev shell.
  #
  # Secrets are deliberately NOT declared here: a value in this file lands in
  # the Nix store, which is world-readable and survives garbage collection
  # elsewhere (section 10.5). They come from a gitignored .env or the deploy
  # platform instead.

  env = {
    PEARSS_ENV = "development";
    # Keep the interpreter from scattering __pycache__ into the repo and the
    # store, and keep test output unbuffered so CI logs interleave correctly.
    PYTHONDONTWRITEBYTECODE = "1";
    PYTHONUNBUFFERED = "1";
  };

  languages.python = {
    enable = true;
    package = pkgs.python313;
    # uv owns the virtualenv. Letting devenv create a competing one would put
    # two different site-packages on the path depending on how you entered.
    venv.enable = false;
  };

  # P1 runs SQLite as a file with APScheduler in-process (section 11), so there
  # are no background services to declare. Postgres, Redis and MinIO belong
  # here when P3 needs them, behind their own profile.
  services = { };

  # Git hooks are installed by `just hooks`, not by devenv, so that humans and
  # agents share one definition of the hook set rather than two. (`pre-commit`
  # was renamed to `git-hooks` in devenv 2.x; both name the same module.)
  git-hooks.hooks = { };

  # `devenv test` runs this. Keep it a smoke check over the toolchain that
  # section 10.1 says Nix must supply — not a substitute for `just ci`.
  enterTest = ''
    set -euo pipefail
    echo "pearss: checking Nix-provided toolchain"
    python3 --version
    uv --version
    just --version
    ruff --version
    mypy --version
    sqlite3 --version
    echo "pearss: toolchain OK"
  '';
}
