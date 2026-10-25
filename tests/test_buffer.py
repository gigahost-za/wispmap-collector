from wispmap_collector.buffer import Buffer


class WallClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_fifo_newest_delete_depth(tmp_path):
    b = Buffer(tmp_path / "q.sqlite3")
    assert b.oldest() is None and b.newest() is None and b.depth() == 0
    for i in range(3):
        b.put(f"b{i}", f"body{i}".encode())
    assert (b.oldest().batch_id, b.newest().batch_id, b.depth()) == ("b0", "b2", 3)
    b.delete(b.oldest().id)
    assert b.oldest().batch_id == "b1" and b.oldest().body == b"body1"


def test_survives_restart(tmp_path):
    b = Buffer(tmp_path / "q.sqlite3")
    b.put("b0", b"x")
    b.close()
    b2 = Buffer(tmp_path / "q.sqlite3")
    assert b2.depth() == 1 and b2.oldest().batch_id == "b0"


def test_age_cap_drops_batches_older_than_48h(tmp_path):
    clock = WallClock()
    b = Buffer(tmp_path / "q.sqlite3", clock=clock)
    b.put("old", b"x")
    clock.t += 3600
    b.put("newer", b"x")
    clock.t += 47 * 3600 + 60                 # "old" is now 48h01m old, "newer" 47h01m
    assert b.enforce_caps() == 1
    assert b.oldest().batch_id == "newer" and b.dropped == 1


def test_size_cap_drops_oldest_first(tmp_path):
    b = Buffer(tmp_path / "q.sqlite3", max_bytes=250)
    for i in range(3):
        b.put(f"b{i}", b"x" * 100)
    assert b.enforce_caps() == 1
    assert b.oldest().batch_id == "b1" and b.size_bytes() == 200


def test_dropped_count_persists(tmp_path):
    b = Buffer(tmp_path / "q.sqlite3", max_bytes=50)
    b.put("a", b"x" * 100)
    b.enforce_caps()
    b.put("b", b"x" * 100)
    b.enforce_caps()
    b.close()
    assert Buffer(tmp_path / "q.sqlite3").dropped == 2


def test_put_keeps_given_created_time(tmp_path):
    b = Buffer(tmp_path / "q.sqlite3", clock=WallClock(5000))
    b.put("a", b"x", created=1234.0)
    assert b.oldest().created == 1234.0
