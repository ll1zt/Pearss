# The deployable Pearss package.
#
# Section 10.1 splits ownership: Nix owns the interpreter and system libraries,
# uv owns the Python dependency graph. This file must not blur that line — so it
# does not list dependencies. It builds a venv from the project's own uv.lock.
#
# A venv cannot be a fixed-output derivation, and the reason is structural
# rather than a detail: `.venv/bin/python` is a symlink to the interpreter and
# `pyvenv.cfg` records its `home =` path, so every venv references the store.
# Nix forbids a FOD from doing that. The venv is therefore an ordinary
# derivation, built inside the sandbox where store references are expected.
#
# Two build-flag details make the output stable (verified by building twice and
# comparing byte-for-byte, ADR 0020):
#
#   1. `uv venv --relocatable`. Without it every console script bakes the build
#      directory into its shebang (`#!/tmp/tmp.XXXX/.venv/bin/python`).
#   2. `--no-install-project`. Installing the project writes an editable
#      install whose `.pth`, `direct_url.json` and `uv_cache.json` all carry
#      build-time paths and a timestamp, differing between every build. Runtime
#      reaches the code through PYTHONPATH instead.
#
# `dontFixup` is required for a different reason: the relocatable scripts carry
# `#!/bin/sh`, and fixup would rewrite each to a store-path bash. That is
# harmless in itself, but it makes the console scripts depend on this host's
# bash store path, which is noise a shell-script shim does not need -- the
# launcher below invokes the interpreter directly.
{
  lib,
  python313,
  uv,
  stdenv,
  cacert,
  git,
  zlib,
  writeShellApplication,
}:

let
  projectRoot = lib.cleanSourceWith {
    src = ../.;
    filter =
      path: type:
      let
        base = baseNameOf path;
        excluded = [
          ".git"
          ".venv"
          ".cache"
          ".pytest_cache"
          ".mypy_cache"
          ".ruff_cache"
          ".hypothesis"
          "data"
          "backups"
          "mutants"
          "result"
        ];
      in
      !(builtins.elem base excluded) && base != ".coverage";
  };

  # A venv built by uv from the committed lock. `--frozen` refuses to re-lock:
  # a deploy whose dependency set differs from the committed lock is exactly the
  # drift this design exists to prevent. `--no-dev` keeps pytest/ruff out of the
  # runtime closure.
  venv = stdenv.mkDerivation {
    pname = "pearss-venv";
    version = "0.1.0";

    src = projectRoot;

    nativeBuildInputs = [ uv ];

    dontConfigure = true;
    dontBuild = true;

    installPhase = ''
      runHook preInstall

      export HOME="$TMPDIR"
      export UV_CACHE_DIR="$TMPDIR/uv-cache"
      export UV_PYTHON="${python313}/bin/python3"
      export UV_PYTHON_DOWNLOADS=never
      export UV_LINK_MODE=copy
      export SSL_CERT_FILE="${cacert}/etc/ssl/certs/ca-bundle.crt"

      # See the header: these three flags are what make the output stable.
      # Without --relocatable every console script bakes the build directory
      # into its shebang; without --no-install-project the project's own
      # editable-install metadata carries a build path and a timestamp.
      uv venv --relocatable --python "$UV_PYTHON" .venv
      uv sync --frozen --no-dev --no-install-project \
        --python .venv/bin/python

      mkdir -p "$out"
      cp -r .venv "$out/venv"

      runHook postInstall
    '';

    # The venv's scripts already carry `#!/bin/sh` (that is what --relocatable
    # produces). Nix's fixup phase would rewrite each one to a store-path bash,
    # and a fixed-output derivation is forbidden from referencing store paths --
    # so fixup must not run. Nothing else here needs it: no ELF binaries are
    # patched, because every native wheel arrives pre-built and its shared
    # objects reference their own dependencies by soname.
    dontFixup = true;
  };

  # The launcher. Kept as a separate, cheap derivation so the expensive venv
  # above stays a single hashable artifact that a uv.lock bump rebuilds once.
  pearss = writeShellApplication {
    name = "pearss";

    runtimeInputs = [ git ];

    text = ''
      # PYTHONPATH rather than an editable install: the venv holds the
      # dependencies, the source tree holds the code. `--no-install-project`
      # above is what makes the venv reproducible, and this is the other half
      # of that trade -- without it the app package would not be importable.
      export PYTHONPATH="${projectRoot}/src''${PYTHONPATH:+:$PYTHONPATH}"

      # The system half of the onnxruntime wheel (ADR 0035 decision 5): its
      # shared objects reference libstdc++ and libz by soname and Nix puts
      # neither on the default loader path. The NixOS module starts this
      # launcher directly rather than through `nix develop`, so the devShell's
      # copy does not reach it -- the pair has to be here too. Appended, not
      # replaced: a deployment that already set one keeps it.
      export LD_LIBRARY_PATH="${stdenv.cc.cc.lib}/lib:${zlib}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

      # The venv's interpreter, not the system one: that is what makes the
      # dependency set the one uv.lock pinned.
      # --http installs the half-open request-header timeout (issue #9): a
      # connection that stops mid-header is closed rather than holding a slot
      # forever (measured: >120s with none). uvicorn exposes no setting for it,
      # so the protocol class is the seam; the deadline is
      # PEARSS_HTTP_HEADER_TIMEOUT_SECONDS (default 15s).
      exec ${venv}/venv/bin/python -m uvicorn app.main:app \
        --host "''${PEARSS_BIND_HOST:-127.0.0.1}" \
        --port "''${PEARSS_PORT:-8000}" \
        --http app.http_timeout:HeaderTimeoutProtocol \
        --proxy-headers \
        --forwarded-allow-ips="''${PEARSS_FORWARDED_ALLOW_IPS:-127.0.0.1}"
    '';
  };
in
pearss.overrideAttrs (prev: {
  passthru = (prev.passthru or { }) // {
    # The NixOS module needs both: the venv to run alembic, and the source tree
    # for alembic.ini and the migration scripts.
    inherit venv projectRoot;
  };

  meta = {
    description = "Single-user, agent-first RSS+ system";
    license = lib.licenses.mit;
    mainProgram = "pearss";
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
})
