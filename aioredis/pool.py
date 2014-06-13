import asyncio

from .commands import create_redis, Redis


@asyncio.coroutine
def create_pool(address, db=0, password=None, *,
                minsize=10, maxsize=10, commands_factory=Redis, loop=None):
    """Creates Redis Pool.
    """

    pool = RedisPool(address, db, password,
                     minsize=minsize, maxsize=maxsize,
                     commands_factory=commands_factory,
                     loop=loop)
    yield from pool._fill_free()
    return pool


class RedisPool:
    """Redis pool.

    """

    def __init__(self, address, db=0, password=None,
                 *, minsize, maxsize, commands_factory, loop=None):
        if loop is None:
            loop = asyncio.get_event_loop()
        self._address = address
        self._db = db
        self._password = password
        self._minsize = minsize
        self._factory = commands_factory
        self._loop = loop
        self._pool = asyncio.Queue(maxsize, loop=loop)
        self._used = set()
        self._need_wait = None

    @property
    def minsize(self):
        """Minimum pool size.
        """
        return self._minsize

    @property
    def maxsize(self):
        """Maximum pool size.
        """
        return self._pool.maxsize

    @property
    def size(self):
        """Current pool size.
        """
        return self.freesize + len(self._used)

    @property
    def freesize(self):
        """Current number of free connections.
        """
        return self._pool.qsize()

    @asyncio.coroutine
    def clear(self):
        """Clear pool connections.

        Close and remove all free connections.
        """
        while not self._pool.empty():
            conn = yield from self._pool.get()
            conn.close()

    @property
    def db(self):
        return self._db

    @asyncio.coroutine
    def select(self, db):
        """Changes db index for all free connections."""
        self._need_wait = fut = asyncio.Future(loop=self._loop)
        try:
            yield from self._fill_free()
            for _ in range(self.freesize):
                conn = yield from self._pool.get()
                try:
                    yield from conn.select(db)
                    self._db = db
                finally:
                    yield from self._pool.put(conn)
        finally:
            self._need_wait = None
            fut.set_result(None)

    def _wait_select(self):
        if self._need_wait is None:
            return ()
        return self._need_wait

    @asyncio.coroutine
    def acquire(self):
        yield from self._wait_select()
        yield from self._fill_free()
        if self.minsize > 0 or not self._pool.empty():
            conn = yield from self._pool.get()
        else:
            conn = yield from self._create_new_connection()
        assert not conn.closed, conn
        assert conn not in self._used, (conn, self._used)
        self._used.add(conn)
        return conn

    def release(self, conn):
        # TODO: check if connection still on the same DB index;
        #       if not: either change to default or drop this connection;
        assert conn in self._used, "Invalid connection, maybe from other pool"
        self._used.remove(conn)
        if not conn.closed:
            if conn.db == self.db:
                try:
                    self._pool.put_nowait(conn)
                except asyncio.QueueFull:
                    # consider this connection as old and close it.
                    conn.close()
            else:
                conn.close()

    @asyncio.coroutine
    def _fill_free(self):
        while self.freesize < self.minsize and self.size < self.maxsize:
            conn = yield from self._create_new_connection()
            yield from self._pool.put(conn)

    @asyncio.coroutine
    def _create_new_connection(self):
        conn = yield from create_redis(self._address,
                                       self._db,
                                       self._password,
                                       commands_factory=self._factory,
                                       loop=self._loop)
        return conn

    def __enter__(self):
        raise RuntimeError(
            "'yield from' should be used as a context manager expression")

    def __exit__(self, *args):
        pass    # pragma: nocover

    def __iter__(self):
        conn = yield from self.acquire()
        return _ConnectionContextManager(self, conn)


class _ConnectionContextManager:

    __slots__ = ('_pool', '_conn')

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc_value, tb):
        try:
            self._pool.release(self._conn)
        finally:
            self._pool = None
            self._conn = None
