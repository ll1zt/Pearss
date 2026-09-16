"""Discovering and validating third-party plugins on disk (ADR 0023).

A plugin is a directory the operator placed under `PEARSS_PLUGIN_DIR`:

    <dir>/<collector_id>/
      manifest.json    # the declared identity
      collector.py     # the hookimpls, imported as module `collector`
      .venv/           # the author's own dependencies (optional)

This module owns three things: where to look, what a plugin directory must
contain, and the verification that a plugin's declarations match what its code
actually implements.

The verification is the same contract check ADR 0017 performs at startup for
shipped collectors, moved earlier: a manifest that declares `paged` while the
module has no `collect_pages` is refused at install time. That is a
*correctness* check, not a security one -- ADR 0023's position is that the
user owns the trust decision. What this refuses is a plugin that cannot serve
its own manifest, which would otherwise fail at the first collection.

Verification runs in a child process (ADR 0023): importing a plugin executes
its top-level code, and the loader must not do that in the host.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Where plugins are installed. A deployment states it; the default is the
# service's state directory, beside the database and stored bodies, so one
# backup path covers everything an install is made of.
PLUGIN_DIR_ENV = "PEARSS_PLUGIN_DIR"
DEFAULT_PLUGIN_DIR = "/var/lib/pearss/plugins"

# The module name a plugin's code must be importable as. Fixed rather than
# derived from the directory name: the directory is the collector *id* the
# operator types, and letting the two coincide would make an id that is also
# a valid Python identifier a hidden requirement.
MODULE_NAME = "collector"

MANIFEST_NAME = "manifest.json"
FEATURES = frozenset(
    {"fetch", "paged", "backfill", "signed", "cookie", "wordpress-upgrade"}
)


class PluginLoadError(Exception):
    """A plugin could not be installed, with a message for its author."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginOnDisk:
    """A validated plugin directory, read but not yet executed."""

    plugin_id: str
    directory: Path
    manifest: dict[str, Any]
    checksum: str


def plugin_root() -> Path:
    """The directory plugins are installed under."""
    return Path(os.environ.get(PLUGIN_DIR_ENV, DEFAULT_PLUGIN_DIR))


def plugin_directory(plugin_id: str) -> Path:
    """Where a given plugin lives.

    The id is validated before it reaches the filesystem: it becomes a path
    component, and an id containing a separator or a parent reference would
    let `:import` reach outside the plugin root.
    """
    if (
        not plugin_id
        or "/" in plugin_id
        or "\\" in plugin_id
        or plugin_id in {".", ".."}
    ):
        raise PluginLoadError(
            f"{plugin_id!r} is not a usable plugin id; use a name without path "
            "separators"
        )
    return plugin_root() / plugin_id


def read_manifest(plugin_id: str) -> PluginOnDisk:
    """Read and validate a plugin's manifest without executing its code.

    Structural checks first, because they are cheap and they produce the
    clearest message: a missing file or an unparseable manifest should not
    require spawning a process to diagnose.
    """
    directory = plugin_directory(plugin_id)
    if not directory.is_dir():
        raise PluginLoadError(
            f"no plugin directory at {directory}; place manifest.json and "
            f"collector.py there to install {plugin_id!r}"
        )

    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PluginLoadError(f"{manifest_path} is missing")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PluginLoadError(f"{manifest_path} is not readable JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PluginLoadError(f"{manifest_path} must contain a JSON object")

    declared_id = manifest.get("id")
    if declared_id != plugin_id:
        raise PluginLoadError(
            f"manifest declares id {declared_id!r} but the directory is named "
            f"{plugin_id!r}; they must agree, because the directory name is the "
            "id an operator types"
        )

    features = manifest.get("features", [])
    if not isinstance(features, list):
        raise PluginLoadError("manifest `features` must be a list")
    unknown = sorted(set(features) - FEATURES)
    if unknown:
        raise PluginLoadError(
            f"manifest declares unknown feature(s) {unknown}; known features: "
            f"{sorted(FEATURES)}"
        )

    if not (directory / f"{MODULE_NAME}.py").is_file():
        raise PluginLoadError(
            f"{directory / f'{MODULE_NAME}.py'} is missing; the hookimpls must be "
            f"importable as module {MODULE_NAME!r}"
        )

    return PluginOnDisk(
        plugin_id=plugin_id,
        directory=directory,
        manifest=manifest,
        # The checksum covers the manifest as written, not the code: it exists
        # so an install-time record can be compared against the manifest on
        # disk later (ADR 0023), and the code is verified by importing it.
        checksum=_manifest_checksum(manifest),
    )


def _manifest_checksum(manifest: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def child_pythonpath(directory: Path) -> str:
    """The `PYTHONPATH` a plugin's child needs: its code, its venv, the host.

    Three entries, in the order they must resolve:

    - the plugin directory, so `collector` is importable;
    - the plugin's own `.venv` site-packages, so its dependencies are (this
      is the "bring your own venv" decision in ADR 0023 -- Pearss never
      resolves a plugin's dependencies);
    - the host's source root, which the child already has, so the contract
      modules (`core.plugins.hooks`) import the same way they do for shipped
      collectors.
    """
    entries = [str(directory)]
    site_packages = _venv_site_packages(directory / ".venv")
    if site_packages is not None:
        entries.append(str(site_packages))
    host_root = os.environ.get("PYTHONPATH", "")
    if host_root:
        entries.extend(part for part in host_root.split(os.pathsep) if part)
    return os.pathsep.join(entries)


def _venv_site_packages(venv: Path) -> Path | None:
    """The site-packages of a plugin's bundled venv, if it has one.

    Located by globbing rather than by computing `python3.X` from the running
    interpreter: the plugin's venv may have been built by a different minor
    version than the host's, and that is the plugin author's business.
    """
    lib = venv / "lib"
    if not lib.is_dir():
        return None
    for candidate in sorted(lib.glob("python*/site-packages")):
        if candidate.is_dir():
            return candidate
    return None


def verify_declarations(plugin: PluginOnDisk, impls: dict[str, Any]) -> None:
    """Check a plugin's declarations against what its code implements.

    The same two-way rule ADR 0017 applies to shipped collectors, applied to
    an installed one. Two comparisons, because an installed plugin has two
    copies of its identity:

    - the code's own `source_config()` against its hookimpls -- the actual
      contract check, identical in form to the startup one;
    - the `manifest.json` on disk against the code's manifest -- the file is
      what the operator reads and what the install record was based on, so a
      disagreement means one of the two is stale.

    Refusing here means the operator hears about a broken plugin at install
    time, in the plugin author's own vocabulary ("you declared paged"),
    rather than at the first collection when the failure would look like the
    origin's fault.
    """
    declared = frozenset(str(f) for f in impls.get("declared_features") or [])
    structural = {
        "fetch": impls["has_fetch"],
        "paged": impls["has_collect_pages"],
        "backfill": impls["has_backfill"],
    }
    descriptive = {"signed", "cookie", "wordpress-upgrade"}
    for feature in sorted(declared):
        if feature in structural:
            if not structural[feature]:
                raise PluginLoadError(
                    f"{plugin.plugin_id!r} declares feature {feature!r} but its "
                    f"collector.py implements no {feature!r} hook"
                )
        elif feature in descriptive:
            if not impls["has_fetch"]:
                raise PluginLoadError(
                    f"{plugin.plugin_id!r} declares feature {feature!r}, which "
                    "describes how its fetch works, but it has no fetch hook"
                )
        else:
            raise PluginLoadError(
                f"{plugin.plugin_id!r} declares unknown feature {feature!r}; "
                f"known features: {sorted(FEATURES)}"
            )

    for required in ("parse", "match_url", "source_config"):
        if not impls[f"has_{required}"]:
            raise PluginLoadError(
                f"{plugin.plugin_id!r} implements no {required!r} hook, which "
                "every collector must provide"
            )

    file_features = frozenset(str(f) for f in plugin.manifest.get("features") or [])
    if file_features != declared:
        raise PluginLoadError(
            f"{plugin.plugin_id!r} disagrees with itself: manifest.json declares "
            f"{sorted(file_features)} but its source_config() declares "
            f"{sorted(declared)}"
        )


def required_credential_prefixes(manifest: dict[str, Any]) -> tuple[str, ...]:
    """The environment-variable prefixes a plugin asks to receive.

    Read from the manifest's `required_config` (the field ADR 0017 already
    defines), as prefixes rather than exact names so a collector can take a
    family of settings the way the shipped ones do. The child environment is
    built from this, so a plugin sees only what it declared -- and what it
    declared is what the operator was shown at install time.
    """
    prefixes: list[str] = []
    for entry in manifest.get("required_config") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("env_name")
        if isinstance(name, str) and name:
            prefixes.append(name)
    return tuple(prefixes)


def verify_in_child(plugin: PluginOnDisk) -> dict[str, Any]:
    """Import the plugin in a child process and report what it implements.

    The import happens in a child for two reasons, and both matter: the host
    must not execute a plugin's top-level code inside its own interpreter,
    and this is the only way to learn whether the plugin is importable at all
    before recording it as installed.

    Raises `PluginLoadError` with the child's own message when the import
    fails -- a syntax error, a missing dependency, an id the plugin does not
    answer to.
    """
    from app.plugin_runtime import PluginChild, SandboxLimits

    child = PluginChild(
        plugin.plugin_id,
        limits=SandboxLimits(),
        pythonpath=child_pythonpath(plugin.directory),
        env=_declared_env(plugin.manifest),
    )
    try:
        # `start` spawns and pings; a plugin that cannot be imported fails
        # there rather than on the first verify call, because the child's
        # entrypoint resolves its collector before serving anything.
        child.start()
        result = child.call("verify", {})
    except Exception as exc:  # the child reports why; the loader relays it
        raise PluginLoadError(
            f"{plugin.plugin_id!r} could not be loaded: {exc}"
        ) from exc
    finally:
        child.stop()
    if not isinstance(result, dict):
        raise PluginLoadError(
            f"{plugin.plugin_id!r} answered the verification with "
            f"{type(result).__name__}, not the expected mapping"
        )
    # Passed through as-is: the report mixes booleans with the feature list,
    # and coercing it to one type would silently drop what the list says.
    return dict(result)


def _declared_env(manifest: dict[str, Any]) -> dict[str, str]:
    """The credential values this plugin declared, for the verification child.

    Verification needs the same environment the collecting child will get:
    a plugin that imports a credential at module scope (a client built from
    the environment, say) would otherwise fail verification and be refused
    for a reason that would not have stopped it in production.
    """
    prefixes = required_credential_prefixes(manifest)
    if not prefixes:
        return {}
    return {
        key: value
        for key, value in os.environ.items()
        if any(key.startswith(prefix) for prefix in prefixes)
    }
