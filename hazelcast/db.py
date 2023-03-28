from datetime import date, datetime, time
from time import localtime
from typing import Any, Callable, Iterator, List, Optional, Sequence, Union, Tuple, Set, NamedTuple
import enum
import itertools
import threading
import urllib.parse

from hazelcast import HazelcastClient
from hazelcast.config import Config
from hazelcast.sql import (
    HazelcastSqlError,
    SqlColumnType,
    SqlResult,
    SqlRow,
    SqlRowMetadata,
    SqlExpectedResultType,
    _DEFAULT_CURSOR_BUFFER_SIZE,
)

apilevel = "2.0"
# Threads may share the module and connections.
threadsafety = 2
paramstyle = "qmark"


class Type(enum.Enum):
    NULL = 0
    STRING = 1
    BOOLEAN = 2
    DATE = 3
    TIME = 4
    DATETIME = 5
    INTEGER = 6
    FLOAT = 7
    DECIMAL = 8
    JSON = 9
    OBJECT = 10


ColumnDescription = NamedTuple(
    "ColumnDescription",
    [
        ("name", str),
        ("type", Type),
        ("display_size", None),
        ("internal_size", None),
        ("precision", None),
        ("scale", None),
        ("null_ok", bool),
    ],
)


class _DBAPIType:
    def __init__(self, *values: Type):
        self._values = values

    def __eq__(self, other: object) -> bool:
        return other in self._values

    def __ne__(self, other: object) -> bool:
        return other not in self._values


Date = date
Time = time
Timestamp = datetime
Binary = bytes
STRING = _DBAPIType(Type.STRING)
DATETIME = _DBAPIType(Type.DATE, Type.TIME, Type.DATETIME)
BINARY = _DBAPIType()
NUMBER = _DBAPIType(Type.INTEGER, Type.FLOAT, Type.DECIMAL)
ROWID = _DBAPIType()


def DateFromTicks(ticks):
    return date(*localtime(ticks)[:3])


def TimeFromTicks(ticks):
    return time(*localtime(ticks)[3:6])


def TimestampFromTicks(ticks):
    return datetime(*localtime(ticks)[:6])


class Cursor:
    def __init__(self, conn: "Connection"):
        self.arraysize = 1
        self._conn = conn
        self._res: Union[SqlResult, None] = None
        self._description: Union[List[ColumnDescription], None] = None
        self._iter: Optional[Iterator[SqlRow]] = None
        self._rownumber = -1
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __iter__(self) -> Optional[Iterator[SqlRow]]:
        return self._iter

    @property
    def connection(self):
        return self._conn

    @property
    def description(self) -> Union[List[ColumnDescription], None]:
        return self._description

    @property
    def rowcount(self) -> int:
        return -1

    @property
    def rownumber(self) -> Optional[int]:
        if self._rownumber < 0:
            return None
        return self._rownumber

    def close(self):
        if not self._closed:
            self._closed = True
            self._conn._close_cursor(self)
            if self._res:
                self._res.close()
                self._res = None

    def execute(self, operation: str, params: Optional[Tuple] = None) -> None:
        if params is not None and not isinstance(params, tuple):
            raise InterfaceError("params must be a tuple or None")
        params = params or ()
        self._ensure_open()
        self._rownumber = -1
        self._iter = None
        self._res = None
        cbs = _DEFAULT_CURSOR_BUFFER_SIZE
        if self.arraysize > 0:
            cbs = self.arraysize
        self._description = None
        res = (
            self._conn._get_client()
            .sql.execute(operation, *params, cursor_buffer_size=cbs)
            .result()
        )
        if res.is_row_set():
            self._rownumber = 0
            self._res = res
            self._description = self._make_description(res.get_row_metadata())
            self._iter = res.__iter__()

    def executemany(self, operation: str, seq_of_params: Sequence[Tuple]) -> None:
        self._ensure_open()
        self._rownumber = -1
        self._iter = None
        self._res = None
        futures = []
        svc = self._conn._get_client().sql
        for params in seq_of_params:
            futures.append(
                svc.execute(
                    operation, *params, expected_result_type=SqlExpectedResultType.UPDATE_COUNT
                )
            )
        for fut in futures:
            fut.result()

    def fetchone(self) -> Optional[SqlRow]:
        if self._iter is None:
            raise InterfaceError("fetch can only be called after row returning queries")
        try:
            row = next(self._iter)
            self._rownumber += 1
            return row
        except StopIteration:
            return None

    def fetchmany(self, size: Optional[int] = None) -> List[SqlRow]:
        if self._iter is None:
            raise InterfaceError("fetchmany can only be called after row returning queries")
        if size is None:
            size = self.arraysize
        rows = list(itertools.islice(self._iter, size))
        self._rownumber += len(rows)
        return rows

    def fetchall(self) -> List[SqlRow]:
        if self._iter is None:
            raise InterfaceError("fetchall can only be called after row returning queries")
        rows = list(self._iter)
        self._rownumber += len(rows)
        return rows

    def next(self) -> Optional[SqlRow]:
        if self._iter is None:
            return None
        return next(self._iter)

    def setinputsizes(self, sizes):
        pass

    def setoutputsize(self, size=None, column=None):
        pass

    @classmethod
    def _make_description(cls, metadata: SqlRowMetadata) -> List[ColumnDescription]:
        r = []
        for col in metadata.columns:
            r.append(
                ColumnDescription(
                    name=col.name,
                    type=_map_type(col.type),
                    display_size=None,
                    internal_size=None,
                    precision=None,
                    scale=None,
                    null_ok=col.nullable,
                )
            )
        return r

    def _ensure_open(self):
        if self._closed:
            raise self.connection.ProgrammingError("connection is closed")


class Connection:
    def __init__(self, config: Config):
        self.__mu = threading.RLock()
        self.__client: Optional[HazelcastClient] = HazelcastClient(config)
        self._cursors: Set[Cursor] = set()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self.__client:
            with self.__mu:
                if self.__client:
                    self.__client.shutdown()
                    self.__client = None
                    return
        raise InterfaceError("connection was already closed")

    def commit(self) -> None:
        # transactions are not supported
        # ensure an exception is thrown if there is no client
        self._get_client()

    def cursor(self) -> Cursor:
        with self.__mu:
            if self.__client is not None:
                cursor = Cursor(self)
                self._cursors.add(cursor)
                return cursor
        raise InterfaceError("connection is already closed")

    def _get_client(self) -> HazelcastClient:
        with self.__mu:
            if self.__client is not None:
                return self.__client
        raise InterfaceError("connection is closed")

    def _close_cursor(self, cursor: Cursor) -> None:
        with self.__mu:
            if cursor in self._cursors:
                self._cursors.remove(cursor)

    @property
    def Error(self):
        return Error

    @property
    def Warning(self):
        return Warning

    @property
    def InterfaceError(self):
        return InterfaceError

    @property
    def DatabaseError(self):
        return DatabaseError

    @property
    def InternalError(self):
        return InternalError

    @property
    def OperationalError(self):
        return OperationalError

    @property
    def ProgrammingError(self):
        return ProgrammingError

    @property
    def IntegrityError(self):
        return IntegrityError

    @property
    def DataError(self):
        return DataError

    @property
    def NotSupportedError(self):
        return NotSupportedError


def connect(
    config=None,
    *,
    dsn="",
    user: str = None,
    password: str = None,
    host: str = None,
    port: int = None,
    cluster_name: str = None,
) -> Connection:
    c = _make_config(
        config,
        dsn=dsn,
        user=user,
        password=password,
        host=host,
        port=port,
        cluster_name=cluster_name,
    )
    return Connection(c)


class Error(Exception):
    pass


class Warning(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class InternalError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class DataError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


def _wrap_error(f: Callable) -> Any:
    try:
        return f()
    except HazelcastSqlError as e:
        raise DatabaseError(f"{e.args}") from e
    except Exception as e:
        raise DatabaseError from e


def _map_type(code: int) -> Type:
    type = _type_map.get(code)
    if type is None:
        raise NotSupportedError(f"Unknown type code: {code}")
    return type


_type_map = {
    SqlColumnType.VARCHAR: Type.STRING,
    SqlColumnType.BOOLEAN: Type.BOOLEAN,
    SqlColumnType.TINYINT: Type.INTEGER,
    SqlColumnType.SMALLINT: Type.INTEGER,
    SqlColumnType.INTEGER: Type.INTEGER,
    SqlColumnType.BIGINT: Type.INTEGER,
    SqlColumnType.DECIMAL: Type.DECIMAL,
    SqlColumnType.REAL: Type.FLOAT,
    SqlColumnType.DOUBLE: Type.FLOAT,
    SqlColumnType.DATE: Type.DATE,
    SqlColumnType.TIME: Type.TIME,
    SqlColumnType.TIMESTAMP: Type.DATETIME,
    SqlColumnType.TIMESTAMP_WITH_TIME_ZONE: Type.DATETIME,
    SqlColumnType.OBJECT: Type.OBJECT,
    SqlColumnType.NULL: Type.NULL,
    SqlColumnType.JSON: Type.JSON,
}


def _make_config(
    config: Config = None,
    *,
    dsn="",
    user: str = None,
    password: str = None,
    host: str = None,
    port: int = None,
    cluster_name: str = None,
) -> Config:
    kwargs_used = user or password or host or port or cluster_name
    if config is not None:
        if not isinstance(config, Config):
            raise InterfaceError("config must be a hazelcast.Config object")
        if dsn or kwargs_used:
            raise InterfaceError("config argument cannot be used with keyword arguments")
        return config
    if dsn:
        if kwargs_used:
            raise InterfaceError("dsn argument cannot be used with other keyword arguments")
        return _parse_dsn(dsn)
    config = Config()
    if not host:
        host = "localhost"
    if not port:
        port = 5701
    host = f"{host}:{port}"
    config.cluster_members = [host]
    if user is not None:
        config.creds_username = user
    if password is not None:
        config.creds_password = password
    if cluster_name is not None:
        config.cluster_name = cluster_name
    return config


def _parse_dsn(dsn: str) -> Config:
    r = urllib.parse.urlparse(dsn)
    if r.scheme != "hz":
        raise InterfaceError(f"Scheme must be hz, but it is: {r.scheme}")
    cfg = Config()
    host = "localhost"
    port = 5701
    if r.hostname:
        host = r.hostname
    if r.port:
        port = r.port
    cfg.cluster_members = [f"{host}:{port}"]
    if r.username:
        cfg.creds_username = r.username
    if r.password:
        cfg.creds_password = r.password
    for k, v in urllib.parse.parse_qsl(r.query):
        value: Any = v
        if k in _parse_dsn_map:
            attr_name, transform = _parse_dsn_map[k]
            if transform:
                try:
                    value = transform(value)
                except ValueError as e:
                    raise InterfaceError from e
            setattr(cfg, attr_name, value)
        else:
            raise InterfaceError(f"Unknown DSN attribute: {k}")
    return cfg


def _make_bool(v: str) -> bool:
    if v == "true":
        return True
    if v == "false":
        return False
    raise ValueError(f"Invalid boolean: {v}")


_parse_dsn_map = {
    "cluster.name": ("cluster_name", None),
    "cloud.token": ("cloud_discovery_token", None),
    "smart": ("smart_routing", _make_bool),
    "ssl": ("ssl_enabled", _make_bool),
    "ssl.ca.path": ("ssl_cafile", None),
    "ssl.cert.path": ("ssl_certfile", None),
    "ssl.key.path": ("ssl_keyfile", None),
    "ssl.key.password": ("ssl_password", None),
}