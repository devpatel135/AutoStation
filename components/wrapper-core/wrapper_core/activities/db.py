"""Database activities (UiPath: Connect / Execute Query / Execute Non Query).

* SQLite: stdlib ``sqlite3`` with dict rows (`connect_sqlite`, `query`, `execute`).
* Any ODBC source (SQL Server, Oracle, Access, ...): `query_odbc` / `execute_odbc` run through Windows PowerShell and
  ``System.Data.Odbc`` - no Python driver to install, the machine's ODBC drivers and Windows auth are used as the user.
  Statements are parameterised with positional ``?`` markers; values are passed as JSON, never spliced into the script.
  The connection string comes from the argument, or - preferably - from Credential Manager target
  ``AUTOMAGIX/connstr/<name>`` via ``connection_string_secret="name"`` so it never appears in code or the manifest.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .. import secrets
from ..retry import DataError, LogicError, NetworkError, SystemUnavailable
from ._common import ActivityError, NotAvailable, emit, powershell_json, require_windows

Row = Dict[str, Any]
CONNSTR_PREFIX = "AUTOMAGIX/connstr/"


def _dict_factory(cur: sqlite3.Cursor, row: tuple) -> Row:
    return {c[0]: row[i] for i, c in enumerate(cur.description)}


def connect_sqlite(path: "str | os.PathLike", readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a SQLite database with dict rows, WAL journalling and a busy timeout (safe for several writers)."""
    p = os.fspath(path)
    if readonly:
        conn = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True, timeout=timeout)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        conn = sqlite3.connect(p, timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def sqlite(path: "str | os.PathLike", readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """``with db.sqlite(path) as conn:`` - commits on success, rolls back on error, always closes."""
    conn = connect_sqlite(path, readonly)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[Row]:
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise SystemUnavailable(f"database is busy: {exc}") from exc
        raise DataError(f"SQL error: {exc}") from exc
    except sqlite3.Error as exc:
        raise DataError(f"SQL error: {exc}") from exc
    emit("db.query", rows=len(rows))
    return rows


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    try:
        cur = conn.execute(sql, tuple(params))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise SystemUnavailable(f"database is busy: {exc}") from exc
        raise DataError(f"SQL error: {exc}") from exc
    except sqlite3.Error as exc:
        raise DataError(f"SQL error: {exc}") from exc
    emit("db.execute", rowcount=cur.rowcount)
    return cur.rowcount


# --- ODBC via PowerShell -------------------------------------------------------------------------------
ODBC_SCRIPT = r"""
$conn = New-Object System.Data.Odbc.OdbcConnection($env:AMX_CONNSTR)
$conn.ConnectionTimeout = [int]$A.timeout
try {
    $conn.Open()
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = [string]$A.sql
    $cmd.CommandTimeout = [int]$A.timeout
    foreach ($p in @($A.params)) {
        $param = $cmd.CreateParameter()
        $param.Value = if ($null -eq $p) { [System.DBNull]::Value } else { $p }
        [void]$cmd.Parameters.Add($param)
    }
    if ($A.mode -eq 'query') {
        $reader = $cmd.ExecuteReader()
        $rows = New-Object System.Collections.ArrayList
        while ($reader.Read()) {
            $row = [ordered]@{}
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                $v = $reader.GetValue($i)
                if ($v -is [System.DBNull]) { $v = $null }
                elseif ($v -is [datetime]) { $v = $v.ToString('o') }
                elseif ($v -is [byte[]]) { $v = [Convert]::ToBase64String($v) }
                elseif ($v -is [decimal]) { $v = [double]$v }
                $row[$reader.GetName($i)] = $v
            }
            [void]$rows.Add([pscustomobject]$row)
        }
        $reader.Close()
        ConvertTo-Json -InputObject @($rows) -Compress -Depth 4
    } else {
        $n = $cmd.ExecuteNonQuery()
        ConvertTo-Json -InputObject @{ rowcount = $n } -Compress
    }
} finally {
    $conn.Close()
}
"""


def _connstr(connection_string: Optional[str], secret: Optional[str]) -> str:
    if connection_string:
        return connection_string
    if secret:
        v = secrets.credman_read(CONNSTR_PREFIX + secret) or os.environ.get("AMX_CONNSTR_" + re.sub(r"[^A-Za-z0-9]", "_", secret).upper())
        if not v:
            raise LogicError(f"connection string {secret!r} not found (Credential Manager target {CONNSTR_PREFIX}{secret})")
        return v
    raise LogicError("give connection_string or connection_string_secret")


def build_odbc_request(sql: str, params: Optional[Sequence[Any]], timeout: int, mode: str) -> Dict[str, Any]:
    """The JSON handed to PowerShell as $A (pure; unit-tested). Only JSON scalars are accepted as parameter values."""
    ps = list(params or [])
    for p in ps:
        if p is not None and not isinstance(p, (str, int, float, bool)):
            raise DataError(f"ODBC parameters must be str/int/float/bool/None, got {type(p).__name__}")
    if mode not in ("query", "nonquery"):
        raise ValueError(mode)
    return {"sql": sql, "params": ps, "timeout": int(timeout), "mode": mode}


def _run_odbc(sql, params, connection_string, connection_string_secret, timeout, mode):
    require_windows("ODBC access")
    cs = _connstr(connection_string, connection_string_secret)
    try:
        return powershell_json(ODBC_SCRIPT, build_odbc_request(sql, params, timeout, mode), timeout=timeout + 30,
                               env_extra={"AMX_CONNSTR": cs}, what="ODBC")
    except ActivityError as exc:
        msg = str(exc)
        if re.search(r"login failed|authentication|password|permission denied", msg, re.I):
            raise DataError("ODBC authentication/permission failure") from exc  # not retried; message withheld (may echo the connstr)
        if re.search(r"timeout expired|communication link|connection (failure|is closed)|network|server does not exist|08S01", msg, re.I):
            raise NetworkError("ODBC connection failure") from exc
        if re.search(r"data source name not found|driver", msg, re.I):
            raise NotAvailable("ODBC driver / data source is not installed on this machine") from exc
        raise DataError("ODBC statement failed: " + re.sub(r"(?i)(pwd|password)=[^;]*", r"\1=***", msg)[:300]) from exc


def query_odbc(connection_string: Optional[str] = None, sql: str = "", params: Optional[Sequence[Any]] = None,
               connection_string_secret: Optional[str] = None, timeout: int = 30) -> List[Row]:
    """SELECT through ODBC; returns ``list[dict]`` (dates as ISO strings, decimals as floats, binary as base64)."""
    rows = _run_odbc(sql, params, connection_string, connection_string_secret, timeout, "query")
    rows = rows or []
    emit("db.query_odbc", rows=len(rows))
    return rows


def execute_odbc(connection_string: Optional[str] = None, sql: str = "", params: Optional[Sequence[Any]] = None,
                 connection_string_secret: Optional[str] = None, timeout: int = 30) -> int:
    """INSERT/UPDATE/DELETE through ODBC; returns the affected row count."""
    out = _run_odbc(sql, params, connection_string, connection_string_secret, timeout, "nonquery")
    emit("db.execute_odbc", rowcount=(out or {}).get("rowcount"))
    return int((out or {}).get("rowcount", -1))
