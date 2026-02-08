import tomllib
from pathlib import Path
from typing import Any

from loguru import logger
from snowflake.connector import SnowflakeConnection, connect
from snowflake.snowpark import Session


def redact(cfg: dict[str, Any]) -> dict[str, Any]:
    secret = {"private_key_file_pwd"}
    return {k: ("***" if k in secret and v else v) for k, v in cfg.items()}


def resolve_conn_toml(explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"connections.toml not found at: {path}")
        return path

    candidates = (
        Path.home() / ".snowflake" / "connections.toml",
        Path.home()
        / "Library"
        / "Application Support"
        / "snowflake"
        / "connections.toml",
    )
    for path in candidates:
        if path.exists():
            return path

    raise RuntimeError(
        "connections.toml not found. Create ~/.snowflake/connections.toml "
        "or pass conn_toml explicitly."
    )


def load_conn(path: Path, name: str) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    cons = raw.get("connections")
    if isinstance(cons, dict):
        entry = cons.get(name)
        if isinstance(entry, dict):
            return dict(entry)

    entry = raw.get(name)
    if isinstance(entry, dict):
        return dict(entry)

    names: list[str] = []
    if isinstance(cons, dict):
        names.extend(sorted(k for k, v in cons.items() if isinstance(v, dict)))
    names.extend(
        sorted(k for k, v in raw.items() if k != "connections" and isinstance(v, dict))
    )
    names_txt = ", ".join(names) if names else "(none)"
    raise RuntimeError(
        f"Snowflake connection '{name}' not found in {path}. Available: {names_txt}."
    )


def norm_conn(raw: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}

    def take(key: str, dst: str | None = None) -> None:
        val = raw.get(key)
        if val not in (None, ""):
            cfg[dst or key] = val

    take("account")
    take("user")
    take("role")
    take("warehouse")
    take("database")
    take("schema")
    take("authenticator")

    pk_path = raw.get("private_key_path")
    if pk_path:
        cfg["private_key_file"] = pk_path

    pk_pass = raw.get("private_key_passphrase")
    if pk_pass:
        cfg["private_key_file_pwd"] = pk_pass

    take("private_key_file")
    take("private_key_file_pwd")

    return cfg


def _validated_conn_cfg(
    conn_name: str,
    conn_toml: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    toml_path = resolve_conn_toml(conn_toml)
    raw = load_conn(toml_path, conn_name)

    for key in ("password", "token", "passcode"):
        if raw.get(key) not in (None, ""):
            raise RuntimeError(
                f"Connection '{conn_name}' in {toml_path} defines '{key}', but this "
                "repo supports only key-pair auth "
                "(private_key_path/private_key_passphrase)."
            )

    cfg = norm_conn(raw)

    if not cfg.get("account") or not cfg.get("user"):
        raise RuntimeError(
            f"Connection '{conn_name}' in {toml_path} must define 'account' and 'user'."
        )

    auth = cfg.get("authenticator")
    if auth in (None, ""):
        cfg["authenticator"] = "SNOWFLAKE_JWT"
    elif str(auth).upper() != "SNOWFLAKE_JWT":
        raise RuntimeError(
            f"Connection '{conn_name}' in {toml_path} must use authenticator "
            "'SNOWFLAKE_JWT' for key-pair auth."
        )

    if not cfg.get("warehouse"):
        raise RuntimeError(
            f"Connection '{conn_name}' in {toml_path} must define 'warehouse'."
        )

    if not cfg.get("private_key_file"):
        raise RuntimeError(
            f"Connection '{conn_name}' in {toml_path} must define 'private_key_file' "
            "or 'private_key_path' (this repo uses key-pair auth)."
        )

    logger.debug("Using connection '{}' from {}", conn_name, toml_path)
    logger.debug("Snowflake config: {}", redact(cfg))

    return cfg, toml_path


def get_connection(
    conn_name: str,
    conn_toml: Path | None = None,
) -> SnowflakeConnection:
    cfg, _ = _validated_conn_cfg(conn_name, conn_toml)
    return connect(**cfg)


def get_session(
    conn_name: str,
    conn_toml: Path | None = None,
) -> Session:
    cfg, _ = _validated_conn_cfg(conn_name, conn_toml)
    return Session.builder.configs(cfg).create()
