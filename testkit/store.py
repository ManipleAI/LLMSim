"""Where a run is kept.

SQLite, because it is in the standard library and needs no server, and because a
run that is only in memory is gone with the process — a test of an agent is
evidence, and evidence that disappears when the simulator restarts is a story.

Three kinds of thing live here:

  runs     one row per run the kit has seen
  events   everything that happened in a run, in the order it did
  docs     everything a person writes or a run produces that is not an event:
           scenarios, datasets, results, experiments, mocks, evaluators, reviews.
           One table, keyed by (area, id), holding JSON. A new kind of document
           needs no migration, and the documents are read whole.

The file is owner-only and is never committed: it holds what agents were asked and
what they answered.
"""

import json
import os
import sqlite3
import threading

from . import core

DEFAULT_RETENTION = {"maxRuns": 500, "maxEventsPerRun": 20000, "days": 30}
SCHEMA = """
create table if not exists runs (id text primary key, created real, updated real, label text, meta text);
create table if not exists events (id integer primary key autoincrement, run text, at real, kind text, agent text,
                                   parent integer, data text);
create index if not exists events_run on events (run, at, id);
create table if not exists docs (area text, id text, body text, updated real, primary key (area, id));
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        fresh = path != ":memory:" and not os.path.exists(path)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if fresh:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        try:
            os.chmod(path, 0o600) if path != ":memory:" else None
        except OSError:
            pass
        self.db.execute("pragma journal_mode=wal") if path != ":memory:" else None
        self.db.execute("pragma synchronous=normal")
        self.db.executescript(SCHEMA)

    # -- documents ----------------------------------------------------------

    def put(self, area, doc_id, body):
        with self.lock:
            self.db.execute("insert into docs (area, id, body, updated) values (?,?,?,?) "
                            "on conflict(area, id) do update set body=excluded.body, updated=excluded.updated",
                            (area, doc_id, json.dumps(core.scrub(body), default=str), core.now()))
        return body

    def get(self, area, doc_id):
        with self.lock:
            row = self.db.execute("select body from docs where area=? and id=?", (area, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def docs(self, area, prefix=None):
        with self.lock:
            if prefix:
                rows = self.db.execute("select id, body, updated from docs where area=? and id like ? order by updated",
                                       (area, prefix + "%")).fetchall()
            else:
                rows = self.db.execute("select id, body, updated from docs where area=? order by updated", (area,)).fetchall()
        return [(r[0], json.loads(r[1]), r[2]) for r in rows]

    def areas(self, prefix):
        with self.lock:
            return [r[0] for r in self.db.execute("select distinct area from docs where area like ? order by area", (prefix + "%",))]

    def drop_area(self, area):
        with self.lock:
            return self.db.execute("delete from docs where area=?", (area,)).rowcount

    def drop(self, area, doc_id):
        with self.lock:
            return self.db.execute("delete from docs where area=? and id=?", (area, doc_id)).rowcount > 0

    # -- runs and events ----------------------------------------------------

    def touch_run(self, run, label=None, meta=None):
        with self.lock:
            row = self.db.execute("select 1 from runs where id=?", (run,)).fetchone()
            t = core.now()
            if not row:
                self.db.execute("insert into runs (id, created, updated, label, meta) values (?,?,?,?,?)",
                                (run, t, t, label or "", json.dumps(core.scrub(meta or {}))))
                self.prune()
            else:
                self.db.execute("update runs set updated=? where id=?", (t, run))
                if label:
                    self.db.execute("update runs set label=? where id=?", (label, run))
                if meta:
                    current = json.loads(self.db.execute("select meta from runs where id=?", (run,)).fetchone()[0] or "{}")
                    current.update(meta)
                    self.db.execute("update runs set meta=? where id=?", (json.dumps(core.scrub(current)), run))

    def add_event(self, run, kind, agent="", parent=None, data=None, at=None):
        retention = self.retention()
        with self.lock:
            self.touch_run(run)
            count = self.db.execute("select count(*) from events where run=?", (run,)).fetchone()[0]
            if count >= retention["maxEventsPerRun"]:
                return None
            cur = self.db.execute("insert into events (run, at, kind, agent, parent, data) values (?,?,?,?,?,?)",
                                  (run, at if at is not None else core.now(), kind, agent or "", parent,
                                   json.dumps(core.scrub(data if data is not None else {}), default=str)))
            return cur.lastrowid

    def set_event_parent(self, event_id, parent):
        with self.lock:
            self.db.execute("update events set parent=? where id=?", (parent, event_id))

    def merge_event(self, event_id, fields):
        with self.lock:
            row = self.db.execute("select data from events where id=?", (event_id,)).fetchone()
            if not row:
                return False
            data = json.loads(row[0])
            data.update(fields)
            self.db.execute("update events set data=? where id=?", (json.dumps(core.scrub(data), default=str), event_id))
            return True

    def events(self, run, kinds=None, agent=None, since=0, limit=None):
        sql, args = "select id, run, at, kind, agent, parent, data from events where run=? and id>?", [run, since]
        if kinds:
            sql += " and kind in (%s)" % ",".join("?" * len(kinds))
            args += list(kinds)
        if agent:
            sql += " and agent=?"
            args.append(agent)
        sql += " order by at, id"
        if limit:
            sql += " limit %d" % int(limit)
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [{"id": r[0], "run": r[1], "at": r[2], "kind": r[3], "agent": r[4], "parent": r[5], "data": json.loads(r[6])}
                for r in rows]

    def event(self, event_id):
        with self.lock:
            r = self.db.execute("select id, run, at, kind, agent, parent, data from events where id=?", (event_id,)).fetchone()
        return {"id": r[0], "run": r[1], "at": r[2], "kind": r[3], "agent": r[4], "parent": r[5], "data": json.loads(r[6])} if r else None

    def runs(self, limit=200):
        with self.lock:
            rows = self.db.execute(
                "select r.id, r.created, r.updated, r.label, r.meta, (select count(*) from events e where e.run=r.id), "
                "(select group_concat(distinct e.agent) from events e where e.run=r.id and e.agent != '') "
                "from runs r order by r.updated desc limit ?", (limit,)).fetchall()
        return [{"run": r[0], "created": r[1], "updated": r[2], "label": r[3], "meta": json.loads(r[4] or "{}"),
                 "events": r[5], "agents": sorted((r[6] or "").split(",")) if r[6] else []} for r in rows]

    def run(self, run):
        return next((r for r in self.runs(10 ** 6) if r["run"] == run), None)

    def delete_run(self, run):
        with self.lock:
            n = self.db.execute("delete from events where run=?", (run,)).rowcount
            self.db.execute("delete from runs where id=?", (run,))
            self.db.execute("delete from docs where area='results' and id=?", (run,))
        return n

    # -- retention ----------------------------------------------------------

    def retention(self):
        return dict(DEFAULT_RETENTION, **(self.get("settings", "retention") or {}))

    def prune(self):
        r = self.retention()
        with self.lock:
            cutoff = core.now() - r["days"] * 86400
            old = [x[0] for x in self.db.execute("select id from runs where updated < ?", (cutoff,)).fetchall()]
            extra = self.db.execute("select id from runs order by updated desc limit -1 offset ?", (r["maxRuns"],)).fetchall()
            for run in old + [x[0] for x in extra]:
                self.db.execute("delete from events where run=?", (run,))
                self.db.execute("delete from runs where id=?", (run,))


_STORE = None
_STORE_LOCK = threading.Lock()


def store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            path = os.environ.get("LLMSIM_RUNS_DB") or os.path.join(core.DATA, "llmsim.runs.db")
            _STORE = Store(path)
        return _STORE
