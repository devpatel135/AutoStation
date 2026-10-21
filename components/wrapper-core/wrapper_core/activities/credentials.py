"""Credentials and assets (UiPath: Get Credential, Get Asset).

Credentials live in Windows Credential Manager under the target ``AUTOMAGIX/cred/<name>`` and are stored either as
``user:password`` or as JSON ``{"username": ..., "password": ...}``. Never put them in the manifest or the payload.

Development fallback (never use in production): environment variable ``AMX_CRED_<NAME>`` (same two formats), where NAME is the
credential name upper-cased with non-alphanumerics turned into ``_``.
"""
from __future__ import annotations

import json
import os
import re
from typing import NamedTuple

from .. import secrets
from ..retry import LogicError
from ._common import emit

PREFIX = "AUTOMAGIX/cred/"


class Secret(str):
    """A str whose repr is masked, so a password does not leak through logging of repr()/tracebacks of containers."""

    def __repr__(self) -> str:
        return "Secret('***')"


class Credential(NamedTuple):
    username: str
    password: Secret

    def __repr__(self) -> str:
        return f"Credential(username={self.username!r}, password=Secret('***'))"


class CredentialNotFound(LogicError):
    """No such credential: a configuration problem, so it escalates instead of retrying."""


def _env_name(name: str) -> str:
    return "AMX_CRED_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def parse_credential(raw: str) -> Credential:
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return Credential(str(d["username"]), Secret(str(d["password"])))
        except (ValueError, KeyError) as exc:
            raise CredentialNotFound("credential JSON must contain username and password") from exc
    if ":" not in raw:
        raise CredentialNotFound("credential must be 'user:password' or JSON {username, password}")
    user, _, pwd = raw.partition(":")
    return Credential(user, Secret(pwd))


def get_raw(name: str) -> str:
    """The stored value as-is (for secrets that are not user/password pairs, e.g. a webhook URL). Never log it."""
    raw = secrets.credman_read(PREFIX + name) or os.environ.get(_env_name(name))
    if not raw:
        raise CredentialNotFound(f"credential {name!r} not found (Credential Manager target {PREFIX}{name})")
    return raw


def get_credential(name: str) -> Credential:
    """Return ``Credential(username, password)``; unpacks like a tuple: ``user, pwd = get_credential("sap")``."""
    cred = parse_credential(get_raw(name))
    emit("credentials.get_credential", name=name)  # the name only - never the username or password
    return cred


def store_credential(name: str, username: str, password: str) -> None:
    """For ops/setup scripts (Windows only): store a credential as JSON in Credential Manager."""
    secrets.credman_write(PREFIX + name, json.dumps({"username": username, "password": password}), username="automagix")


def delete_credential(name: str) -> bool:
    return secrets.credman_delete(PREFIX + name)


def get_asset(name: str, default=None):
    """Non-secret configuration value (UiPath: Get Asset): manifest ``assets`` for this ring, overridable by env ``AMX_ASSET_<NAME>``."""
    env = os.environ.get("AMX_ASSET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper())
    if env is not None:
        return env
    from .. import assets
    return assets().get(name, default)
