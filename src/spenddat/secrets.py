"""Secrets access.

Secrets never live in config files or source control. Two backends:

* ``env``     - reads ``SPENDDAT_<MODE>_<NAME>`` environment variables. Intended for CI
                (injected from the CI platform's secret store) and local development.
* ``command`` - runs a secrets-manager CLI per lookup, e.g.
                ``op read op://spenddat/{mode}_{name}/credential`` (1Password) or
                ``aws secretsmanager get-secret-value --secret-id spenddat/{mode}/{name}
                --query SecretString --output text``. The value is read from stdout and is
                never logged.

Secret names are namespaced by runtime mode, so a sandbox run can never pick up a live key
by accident, and vice versa.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Protocol

from spenddat.models import RuntimeMode


class SecretMissing(RuntimeError):
    pass


class SecretsProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvSecrets:
    def __init__(self, mode: RuntimeMode, environ: dict[str, str] | None = None) -> None:
        self._mode = mode
        self._env = environ if environ is not None else os.environ

    def key(self, name: str) -> str:
        return f"SPENDDAT_{self._mode.value.upper()}_{name.upper()}"

    def get(self, name: str) -> str | None:
        v = self._env.get(self.key(name))
        return v.strip() if v else None


class CommandSecrets:
    def __init__(self, mode: RuntimeMode, template: str, timeout: float = 15.0) -> None:
        if "{name}" not in template:
            raise ValueError("SPENDDAT_SECRETS_COMMAND must contain '{name}'")
        self._mode = mode
        self._template = template
        self._timeout = timeout
        self._cache: dict[str, str | None] = {}

    def get(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        cmd = self._template.format(name=name.lower(), mode=self._mode.value)
        proc = subprocess.run(  # noqa: S603 - template is operator-supplied config
            shlex.split(cmd), capture_output=True, text=True, timeout=self._timeout, check=False
        )
        value = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None
        self._cache[name] = value
        return value


def require(provider: SecretsProvider, name: str) -> str:
    v = provider.get(name)
    if not v:
        raise SecretMissing(f"required secret '{name}' is not set")
    return v


def build_secrets(mode: RuntimeMode, environ: dict[str, str] | None = None) -> SecretsProvider:
    env = environ if environ is not None else os.environ
    backend = env.get("SPENDDAT_SECRETS_BACKEND", "env")
    if backend == "env":
        return EnvSecrets(mode, env)
    if backend == "command":
        template = env.get("SPENDDAT_SECRETS_COMMAND")
        if not template:
            raise SecretMissing(
                "SPENDDAT_SECRETS_BACKEND=command requires SPENDDAT_SECRETS_COMMAND"
            )
        return CommandSecrets(mode, template)
    raise ValueError(f"unknown SPENDDAT_SECRETS_BACKEND '{backend}'")
