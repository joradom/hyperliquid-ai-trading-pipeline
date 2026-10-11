"""Bounded in-memory capture/review handoff; no market or model I/O."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


class _Lease:
    def __init__(self, session, index):
        self.session, self.index = session, index
        self.transferred = False
        self.released = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.transferred:
            self.release()

    def release(self):
        with self.session._condition:
            if not self.released:
                self.released = True
                self.session._available += 1
                self.session._condition.notify_all()


class ReviewSession:
    """Reserve before capture; hand capacity to exactly one review per row.

    At most capacity captures/reviews are admitted together. Old scanner
    callables do not participate unless explicitly opted into this protocol.
    An injectable wait supports deterministic virtual-time concurrency tests.
    """
    def __init__(self, symbols, review, *, capacity=3, executor_factory=None, wait=None):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("review capacity must be a positive integer")
        self.symbols = list(symbols)
        self.review = review
        self.capacity = capacity
        self.wait = wait
        self._condition = threading.Condition()
        self._available = capacity
        self._leases = {}
        self._rows = {}
        self._checksums = set()
        self._results = {}
        self._futures = {}
        self._review_errors = {}
        self._closed = False
        self._pool = (executor_factory or ThreadPoolExecutor)(max_workers=capacity)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        with self._condition:
            self._closed = True
            for lease in self._leases.values():
                if not lease.transferred:
                    lease.release()
            self._condition.notify_all()
        # Drain accepted work even when a scanner/callback raises. Each review
        # has its existing bounded model/revalidation behavior.
        self._pool.__exit__(*args)

    def _validate_index(self, index):
        if type(index) is not int or not 0 <= index < len(self.symbols):
            raise ValueError("invalid incremental row index")

    def admit(self, index, symbol):
        self._validate_index(index)
        with self._condition:
            if symbol != self.symbols[index] or index in self._leases:
                raise ValueError("duplicate or mismatched capture admission")
            while self._available == 0 and not self._closed:
                if self.wait is None:
                    self._condition.wait()
                else:
                    self._condition.release()
                    try:
                        self.wait(0.01)
                    finally:
                        self._condition.acquire()
            if self._closed:
                raise RuntimeError("review session closed")
            # Recheck because another concurrent caller may have used this index.
            if index in self._leases:
                raise ValueError("duplicate capture admission")
            self._available -= 1
            lease = _Lease(self, index)
            self._leases[index] = lease
            return lease

    def deliver(self, index, row):
        self._validate_index(index)
        with self._condition:
            lease = self._leases.get(index)
            if lease is None or row.get("symbol") != self.symbols[index]:
                raise ValueError("incremental row without matching admission")
            if index in self._rows:
                if self._rows[index] != row:
                    raise ValueError("conflicting duplicate incremental row")
                return  # Repeated delivery never submits the same snapshot twice.
            if lease.released or self._closed:
                raise ValueError("incremental row delivered outside its capture lease")
            self._rows[index] = row
            snapshot = row.get("decision_snapshot") or {}
            checksum = snapshot.get("integrity", {}).get("checksum")
            if row.get("classification") == "ANALYZE" and checksum:
                if checksum in self._checksums:
                    raise ValueError("duplicate incremental snapshot checksum")
                self._checksums.add(checksum)
            lease.transferred = True
        if row.get("classification") != "ANALYZE":
            try:
                self._results[index] = self.review(row)
            except BaseException as exc:
                self._review_errors[index] = exc
                raise
            finally:
                lease.release()
            return
        try:
            self._futures[index] = self._pool.submit(self._review, row, lease)
        except BaseException:
            lease.release()
            raise

    def _review(self, row, lease):
        try:
            return self.review(row)
        finally:
            lease.release()

    def finish(self, rows):
        if len(rows) != len(self.symbols):
            raise ValueError("incremental scanner returned a different row count")
        results = []
        for index, row in enumerate(rows):
            if index not in self._rows or self._rows[index] != row:
                raise ValueError("incremental scanner omitted or changed a delivered row")
            if index in self._review_errors:
                raise self._review_errors[index]
            if index in self._futures:
                results.append(self._futures[index].result())
            elif index in self._results:
                results.append(self._results[index])
            elif row.get("error"):
                # Native scanner records handoff failures without recapturing.
                # Its error prevents the existing review gate from invoking IA.
                self._results[index] = self.review(row)
                results.append(self._results[index])
            else:
                raise ValueError("incremental row has no terminal review result")
        return results
