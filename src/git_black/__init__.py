import logging
import re
import sys
import time
from bisect import bisect
from collections import namedtuple
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from subprocess import PIPE, Popen
from threading import Lock
from typing import Dict, List, Tuple

import click
from pygit2 import (
    GIT_DELTA_MODIFIED,
    GIT_DIFF_IGNORE_SUBMODULES,
    GIT_FILEMODE_BLOB,
    GIT_STATUS_INDEX_DELETED,
    GIT_STATUS_INDEX_MODIFIED,
    GIT_STATUS_INDEX_NEW,
    GIT_STATUS_INDEX_RENAMED,
    GIT_STATUS_INDEX_TYPECHANGE,
    Commit,
    DiffHunk,
    IndexEntry,
    Oid,
    Patch,
    Repository,
    Signature,
)

logger = logging.getLogger(__name__)

index_statuses = (
    GIT_STATUS_INDEX_NEW
    | GIT_STATUS_INDEX_MODIFIED
    | GIT_STATUS_INDEX_DELETED
    | GIT_STATUS_INDEX_RENAMED
    | GIT_STATUS_INDEX_TYPECHANGE
)


def commit_datetime(commit: Commit):
    tzinfo = timezone(timedelta(minutes=commit.commit_time_offset))
    return datetime.fromtimestamp(float(commit.commit_time), tzinfo)


@dataclass(frozen=True)
class Delta:
    """this is a simplified version of unidiff.Hunk"""

    filename: str
    old_start: int
    old_lines: List[bytes]
    old_length: int
    new_start: int
    new_length: int
    new_lines: List[bytes]

    @property
    def offset(self):
        return self.new_length - self.old_length

    @staticmethod
    def from_hunk(hunk: DiffHunk, filename):
        old_lines = [line.raw_content for line in hunk.lines if line.origin == "-"]
        new_lines = [line.raw_content for line in hunk.lines if line.origin == "+"]
        return Delta(
            filename=filename,
            old_start=hunk.old_start,
            old_length=hunk.old_lines,
            old_lines=old_lines,
            new_start=hunk.new_start,
            new_length=hunk.new_lines,
            new_lines=new_lines,
        )

    def __str__(self):
        s = [
            "Delta(",
            f"    filename={self.filename},",
            f"    old_start={self.old_start},",
            f"    old_length={self.old_length}",
            "    old_lines=[",
        ]
        for line in self.old_lines:
            s.append("        {!r},".format(line))
        s.append("    ],")
        s.append(f"    new_start={self.new_start},")
        s.append(f"    new_length={self.new_length},")
        s.append("    new_lines=[")
        for line in self.new_lines:
            s.append("        {!r},".format(line))
        s.append("    ]")
        s.append(")")
        return "\n".join(s)


DeltaBlame = namedtuple("DeltaBlame", "delta commits")


blame_re = re.compile(rb"^(?P<commit>[0-9a-f]{40}) (\d+) (?P<lineno>\d+).*")


class HunkBlamer:
    def __init__(self, repo, patch: Patch):
        self.repo = repo
        self.patch = patch
        self.filename = patch.delta.old_file.path
        # self._load_blame()
        # self._blame_obj = self.repo.blame(self.filename)
        self._load_blame()

    def _load_blame(self):
        # libgit2 blame is currently much much slower than calling an external
        # git command: https://github.com/libgit2/libgit2/issues/3027

        blame_proc = Popen(
            ["git", "blame", "--porcelain", "HEAD", self.filename], stdout=PIPE
        )
        self._blame_map = {}
        for line in blame_proc.stdout:
            m = blame_re.match(line)
            if not m:
                continue
            commit = m.group("commit").decode("ascii")
            lineno = int(m.group("lineno"))
            self._blame_map[lineno] = commit

    def _blame(self, lineno) -> str:
        return self._blame_map[lineno]

    def _map_lines(self, delta: Delta) -> Dict[Tuple, Tuple]:
        """
        return a dict that maps tuples of source lines
        to tuples of destination lines. Each key/value pair
        in the dict represents a set of source lines (the key tuple)
        that became the destination lines (the value tuple).

        although weird, the numbers in the tuples are 0-indexed.

        e.g.
        {   # src: dst
            (0,1): (0)     # the first 2 lines collapsed into the first line of the output
            (2,): (1,2,3)  # the third line expanded into lines 2 3 and 4
        }
        """

        # this is harder than I thought; I'll start with a super naive
        # approach and improve it later (or never)

        if delta.old_length == 0:
            return {(): tuple(range(delta.new_length))}
        if delta.new_length == 0:
            return {tuple(range(delta.old_length)): ()}

        result: Dict[Tuple[int, ...], Tuple[int, ...]] = {}

        for i in range(min(delta.old_length, delta.new_length) - 1):
            result[(i,)] = (i,)

        if delta.old_length >= delta.new_length:
            result[tuple(range(delta.new_length - 1, delta.old_length))] = (
                delta.new_length - 1,
            )
        else:
            result[(delta.old_length - 1,)] = tuple(
                range(delta.old_length - 1, delta.new_length)
            )

        return result

    def blames(self) -> List[DeltaBlame]:
        hunk_deltas = [
            Delta.from_hunk(hunk, self.filename) for hunk in self.patch.hunks
        ]

        # let's map each hunk to its source commits and break down the deltas
        # in smaller chunks; this will make it possible to prepare and group
        # commits with a much smaller granularity
        deltas = []
        for hd in hunk_deltas:
            for old_linenos, new_linenos in self._map_lines(hd).items():
                old_start = hd.old_start + min(old_linenos, default=0)
                old_lines = [hd.old_lines[lineno] for lineno in old_linenos]
                new_start = hd.new_start + min(new_linenos, default=0)
                new_lines = [hd.new_lines[lineno] for lineno in new_linenos]
                delta = Delta(
                    filename=self.filename,
                    old_start=old_start,
                    old_lines=old_lines,
                    old_length=len(old_linenos),
                    new_start=new_start,
                    new_lines=new_lines,
                    new_length=len(new_lines),
                )
                deltas.append(delta)

        blames = []
        for i, delta in enumerate(deltas):
            delta_blame = DeltaBlame(delta=delta, commits=set())
            for line in range(
                delta.old_start, delta.old_start + max(1, delta.old_length)
            ):
                commit = self._blame(line)
                delta_blame.commits.add(commit)
            blames.append(delta_blame)

        return blames


class Patcher:
    def __init__(self, repo, filename):
        self.repo = repo
        self.filename = filename
        self._load_lines()
        self._offsets = {}
        self._applied = set()

    def _load_lines(self):
        head = self.repo.head.peel()
        obj = head.tree
        for component in self.filename.split("/"):
            obj = obj / component
        self._lines = BytesIO(obj.data).readlines()

    def apply(self, delta: Delta):
        if (delta.old_start) in self._applied:
            return

        old_length = delta.old_length
        old_start = delta.old_start
        for pos, off in self._offsets.items():
            if delta.old_start > pos:
                old_start += off

        # I don't understand why, but hunks need
        # this when the old_length is 0
        if old_length == 0:
            old_start += 1

        i = old_start - 1
        j = i + old_length
        self._lines[i:j] = delta.new_lines

        self._offsets[delta.old_start] = delta.offset

        self._applied.add(delta.old_start)

    def content(self):
        return b"".join(self._lines)


class GitIndexNotEmpty(Exception):
    pass


class GitBlack:
    def __init__(self):
        self.repo = Repository(".")
        self.patchers = {}

    def get_blamed_deltas(self, patch):
        filename = patch.delta.old_file.path
        self.patchers[filename] = Patcher(self.repo, filename)
        hb = HunkBlamer(self.repo, patch)
        return hb.blames()

    def group_blame_deltas(self, blames):
        for delta_blame in blames:
            commits = tuple(sorted(delta_blame.commits))
            self.grouped_deltas.setdefault(commits, []).append(delta_blame.delta)

        self.progress += 1
        now = time.monotonic()
        if now - self.last_log > 0.04:
            sys.stdout.write("Reading file {}/{} \r".format(self.progress, self.total))
            sys.stdout.flush()
            self.last_log = now

    def commit_changes(self):
        start = time.monotonic()
        self.grouped_deltas = {}

        for path, status in self.repo.status().items():
            if status & index_statuses:
                raise GitIndexNotEmpty

        patches = []
        self._file_modes = {}
        diff = self.repo.diff(context_lines=0, flags=GIT_DIFF_IGNORE_SUBMODULES)
        for patch in diff:
            if patch.delta.status != GIT_DELTA_MODIFIED:
                continue
            self._file_modes[patch.delta.old_file.path] = patch.delta.old_file.mode
            patches.append(patch)

        self.progress = 0
        self.last_log = 0
        self.total = len(patches)

        executor = ThreadPoolExecutor(max_workers=8)
        tasks = set()
        for patch in patches:
            tasks.add(executor.submit(self.get_blamed_deltas, patch))
            if len(tasks) > 8:
                done, not_done = wait(tasks, return_when=FIRST_COMPLETED)
                for task in done:
                    self.group_blame_deltas(task.result())
                tasks -= set(done)

        for task in tasks:
            self.group_blame_deltas(task.result())

        secs = time.monotonic() - start
        sys.stdout.write(
            "Reading file {}/{} ({:.2f} secs).\n".format(
                self.progress, self.total, secs
            )
        )

        start = time.monotonic()
        self.total = len(self.grouped_deltas)
        self.progress = 0
        self.last_log = 0

        for commits, deltas in self.grouped_deltas.items():
            blobs = self._create_blobs(deltas)
            self._commit(commits, blobs)

        secs = time.monotonic() - start
        print(
            "Making commit {}/{} ({:.2f} secs).".format(self.progress, self.total, secs)
        )

    def _create_blobs(self, deltas):
        filenames = set()
        for delta in deltas:
            self.patchers[delta.filename].apply(delta)
            filenames.add(delta.filename)

        blobs = {}
        for filename in filenames:
            blob_id = self.repo.create_blob(self.patchers[filename].content())
            blobs[filename] = blob_id

        return blobs

    def _commit(self, original_commits, blobs):
        for filename, blob_id in blobs.items():
            file_mode = self._file_modes[filename]
            index_entry = IndexEntry(filename, blob_id, file_mode)
            self.repo.index.add(index_entry)

        commits = [self.repo.get(h) for h in original_commits]

        main_commit = commits[0]
        if len(commits) > 1:
            # most recent commit
            main_commit = sorted(commits, key=commit_datetime)[-1]

        commit_message = main_commit.message
        commit_message += "\n\nautomatic commit by git-black, original commits:\n"
        commit_message += "\n".join(["  {}".format(c) for c in original_commits])

        committer = Signature(
            name=self.repo.config["user.name"], email=self.repo.config["user.email"],
        )

        self.repo.index.write()
        tree = self.repo.index.write_tree()
        head = self.repo.head.peel()
        self.repo.create_commit(
            "HEAD", main_commit.author, committer, commit_message, tree, [head.id]
        )
        self.progress += 1
        now = time.monotonic()
        if now - self.last_log > 0.04:
            sys.stdout.write("Making commit {}/{} \r".format(self.progress, self.total))
            sys.stdout.flush()
            self.last_log = now


@click.command()
def cli():
    gb = GitBlack()
    try:
        gb.commit_changes()
    except GitIndexNotEmpty:
        raise click.ClickException("staging area must be empty")


if __name__ == "__main__":
    cli()
