"""
Database provider registry (#9) — provider/adapter pattern.

Abstracts the target database behind a small provider spec so adding support
for a new engine is a data change here, not scattered code. Each provider knows
how to build its connection URL/handle and how to test connectivity.

Relational providers use SQLAlchemy (so the migration engine is engine-agnostic);
MongoDB uses pymongo. Drivers are imported lazily inside `test_connection` /
`get_engine`, so a missing driver only affects that one provider — SQLite (the
default) and the rest of the app keep working with no extra packages installed.

Config dict shape (per provider; unused keys ignored):
    {host, port, database, username, password, extra}   # relational
    {uri} OR {host, port, database, username, password}  # mongodb
    {path}                                                # sqlite
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str                     # "sqlite" | "relational" | "mongodb"
    default_port: int | None
    driver_module: str | None     # import name used to detect availability
    fields: tuple = field(default_factory=tuple)   # form fields needed


PROVIDERS: dict[str, Provider] = {
    "sqlite": Provider("sqlite", "SQLite (default)", "sqlite", None, None, ("path",)),
    "postgresql": Provider("postgresql", "PostgreSQL", "relational", 5432, "psycopg",
                           ("host", "port", "database", "username", "password")),
    "mysql": Provider("mysql", "MySQL / MariaDB", "relational", 3306, "pymysql",
                      ("host", "port", "database", "username", "password")),
    "mssql": Provider("mssql", "Microsoft SQL Server", "relational", 1433, "pyodbc",
                      ("host", "port", "database", "username", "password", "extra")),
    "oracle": Provider("oracle", "Oracle", "relational", 1521, "oracledb",
                       ("host", "port", "service_name", "database", "username", "password")),
    "mongodb": Provider("mongodb", "MongoDB", "mongodb", 27017, "pymongo",
                        ("host", "port", "database", "username", "password", "uri")),
}


def list_providers() -> list[Provider]:
    return list(PROVIDERS.values())


def get_provider(pid: str) -> Provider | None:
    return PROVIDERS.get(pid)


def driver_available(pid: str) -> bool:
    p = PROVIDERS.get(pid)
    if p is None or p.driver_module is None:
        return True  # sqlite / no driver needed
    try:
        __import__(p.driver_module)
        return True
    except Exception:  # noqa: BLE001
        return False


def build_sqlalchemy_url(pid: str, cfg: dict) -> str:
    """Build a SQLAlchemy URL for a relational (or sqlite) provider."""
    from sqlalchemy.engine import URL

    if pid == "sqlite":
        return "sqlite:///" + (cfg.get("path") or ":memory:")

    dialects = {
        "postgresql": "postgresql+psycopg",
        "mysql": "mysql+pymysql",
        "mssql": "mssql+pyodbc",
        "oracle": "oracle+oracledb",
    }
    if pid not in dialects:
        raise ValueError(f"'{pid}' is not a relational provider.")
    p = PROVIDERS[pid]
    query = {}
    database = cfg.get("database") or None
    if pid == "mssql":
        # e.g. extra = "driver=ODBC Driver 18 for SQL Server"
        for part in (cfg.get("extra") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                query[k.strip()] = v.strip()
        query.setdefault("driver", "ODBC Driver 18 for SQL Server")
    if pid == "oracle":
        # Oracle connects by SERVICE NAME (Oracle Cloud / modern listeners) or by
        # SID. Prefer an explicit service_name (rendered as ?service_name=… which
        # oracledb resolves via Easy Connect); fall back to the `database` field
        # treated as a SID for legacy instances. This keeps existing configs
        # working while making the common service-name case connect correctly.
        service = (cfg.get("service_name") or "").strip()
        if not service:
            for part in (cfg.get("extra") or "").split(";"):
                if part.strip().lower().startswith("service_name="):
                    service = part.split("=", 1)[1].strip()
        if service:
            query["service_name"] = service
            database = None
    return str(URL.create(
        dialects[pid],
        username=cfg.get("username") or None,
        password=cfg.get("password") or None,
        host=cfg.get("host") or None,
        port=int(cfg["port"]) if cfg.get("port") else p.default_port,
        database=database,
        query=query,
    ))


def _mongo_client(cfg: dict):
    import pymongo
    uri = (cfg.get("uri") or "").strip()
    if not uri:
        user = cfg.get("username") or ""
        pw = cfg.get("password") or ""
        auth = f"{user}:{pw}@" if user else ""
        host = cfg.get("host") or "localhost"
        port = cfg.get("port") or 27017
        uri = f"mongodb://{auth}{host}:{port}/"
    return pymongo.MongoClient(uri, serverSelectionTimeoutMS=4000)


def test_connection(pid: str, cfg: dict) -> dict:
    """Attempt a real connection. Returns {ok, message, latency_ms}."""
    p = PROVIDERS.get(pid)
    if p is None:
        return {"ok": False, "message": f"Unknown provider '{pid}'.", "latency_ms": None}
    if not driver_available(pid):
        return {"ok": False,
                "message": f"Driver '{p.driver_module}' is not installed on the server.",
                "latency_ms": None}
    start = time.perf_counter()
    try:
        if p.kind == "mongodb":
            client = _mongo_client(cfg)
            client.admin.command("ping")
            client.close()
        else:
            from sqlalchemy import create_engine, text
            engine = create_engine(build_sqlalchemy_url(pid, cfg), pool_pre_ping=True)
            with engine.connect() as con:
                con.execute(text("SELECT 1"))
            engine.dispose()
        ms = int((time.perf_counter() - start) * 1000)
        return {"ok": True, "message": "Connection successful.", "latency_ms": ms}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "latency_ms": None}
