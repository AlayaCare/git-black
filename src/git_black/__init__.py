import logging
import os
import sys
from bisect import bisect
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import read_text
from io import BytesIO
from subprocess import PIPE, Popen, run
from tempfile import TemporaryDirectory
from typing import Dict, List, Tuple

import click

# from git import Commit, Repo
from git.objects.util import altz_to_utctz_str
from jinja2 import Environment, FunctionLoader
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

# from unidiff import Hunk, PatchSet


# def load_template(template):
#     return read_text(__package__, template, "utf-8")


# jinja_env = Environment(loader=FunctionLoader(load_template))
# jinja_env.filters["zip"] = zip


# def reformat(a):
#     run(["black", "-l89", a])


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
        # return len(self.new_lines) - len(self.old_lines)
        return self.new_length - self.old_length

    # @property
    # def src_length(self):
    #     return len(self.old_lines)

    # @property
    # def dst_length(self):
    #     return len(self.new_lines)

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


class HunkBlamer:
    # PATCH_ENCODING = "latin-1"

    def __init__(self, repo, patch: Patch):
        self.repo = repo
        self.patch = patch
        self.filename = patch.delta.old_file.path
        # patch_set = PatchSet(
        #    Popen(["git", "diff", "-U0", filename], stdout=PIPE,).stdout,
        #    encoding=self.PATCH_ENCODING,
        # )

        # self.modified_file = None

        # if patch_set.modified_files:
        #     self.modified_file = patch_set.modified_files[0]

        # # # if a file only has deleted lines, unidiff thinks it was
        # # # deleted; but if we got this far, it's because git
        # # # showed it as a modified file
        # # if patch_set.removed_files:
        # #     self.modified_file = patch_set.removed_files[0]

        # if not self.modified_file:
        #     return

        self._load_blame()

    def _load_blame(self):
        _blame: List[Tuple[int, Oid]] = []
        for blame_hunk in self.repo.blame(self.filename):
            _blame.append(
                (blame_hunk.final_start_line_number, blame_hunk.final_commit_id)
            )
        # sorted(
        #    [
        #        (e.linenos.start, e.commit)
        #        for e in self.repo.blame_incremental("HEAD", self.modified_file.path)
        #    ]
        # )
        self._blame_starts = []
        self._blame_commits = []
        for line, commit in _blame:
            self._blame_starts.append(line)
            self._blame_commits.append(commit)

    def _blame(self, lineno) -> Commit:
        idx = bisect(self._blame_starts, lineno) - 1
        return self.repo.get(self._blame_commits[idx])

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
        # if not self.modified_file:
        #     return []

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
                ns = hd.new_start + min(new_linenos, default=0)
                nl = [hd.new_lines[lineno] for lineno in new_linenos]
                delta = Delta(
                    filename=self.filename,
                    old_start=old_start,
                    old_lines=old_lines,
                    old_length=len(old_linenos),
                    new_start=ns,
                    new_lines=nl,
                    new_length=len(nl),
                )
                deltas.append(delta)

        blames = []
        for i, delta in enumerate(deltas):
            delta_blame = DeltaBlame(delta=delta, commits=set())
            for line in range(
                delta.old_start, delta.old_start + max(1, delta.old_length)
            ):
                commit = self._blame(line)
                delta_blame.commits.add(commit.hex)
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
        # self._lines = [(line + b"\n") for line in obj.data.split(b"\n")]

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

    # def write(self, filename):
    #     f = open(filename, "wb")
    #     f.writelines(self._lines)
    #     f.close()

    def content(self):
        return b"".join(self._lines)


class GitIndexNotEmpty(Exception):
    pass


class GitBlack:
    def __init__(self):
        self.repo = Repository(".")
        # self.repo = Repo(search_parent_directories=True)
        # self._blame_starts = {}
        # self._blame_commits = {}
        # self.a_html = []
        # self.b_html = []
        # self.color_idx = 0
        self.patchers = {}

    def commit_changes(self):
        sys.stdout.write("Reading changes... ")
        sys.stdout.flush()
        grouped_deltas = {}

        for path, status in self.repo.status().items():
            if status & index_statuses:
                raise GitIndexNotEmpty

        for patch in self.repo.diff(context_lines=0, flags=GIT_DIFF_IGNORE_SUBMODULES):
            if patch.delta.status != GIT_DELTA_MODIFIED:
                continue
            #            for hunk in patch.hunks:
            #                print("hunk:", hunk)
            #                print("new_lines:", hunk.new_lines)
            #                print("old_lines:", hunk.old_lines)
            #                print("new_start:", hunk.new_start)
            #                print("new_start:", hunk.new_start)

            #        for diff in self.repo.index.diff(None):
            #            if diff.change_type != "M":
            #                continue

            filename = patch.delta.old_file.path
            # blame = self.repo.blame(filename)

            # for blame_hunk in blame:
            #     print(
            #         blame_hunk.final_commit_id,
            #         blame_hunk.final_start_line_number,
            #         blame_hunk.lines_in_hunk,
            #     )

            self.patchers[filename] = Patcher(self.repo, filename)
            hb = HunkBlamer(self.repo, patch)
            for delta_blame in hb.blames():
                commits = tuple(sorted(delta_blame.commits))
                grouped_deltas.setdefault(commits, []).append(delta_blame.delta)

        sys.stdout.write("done.\n")
        total = len(grouped_deltas)
        progress = 0
        for commits, deltas in grouped_deltas.items():
            self._commit(commits, deltas)
            progress += 1
            sys.stdout.write("Making commit {}/{} \r".format(progress, total))
            sys.stdout.flush()

    def _commit(self, original_commits, deltas: List[Delta]):
        # self.repo.index.read()

        filenames = set()
        for delta in deltas:
            self.patchers[delta.filename].apply(delta)
            filenames.add(delta.filename)

        for filename in filenames:
            # tmpf = os.path.join(tmpdir, filename)
            # self.patchers[filename].write(tmpf)

            blob_id = self.repo.create_blob(self.patchers[filename].content())
            # b = repo[blob_id]
            index_entry = IndexEntry(filename, blob_id, GIT_FILEMODE_BLOB)
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
        # self.repo.state_cleanup()

    # def _old_commit(self, original_commits, deltas: List[Delta]):
    #     with TemporaryDirectory(dir=".") as tmpdir:

    #         dirs = set(os.path.dirname(d.filename) for d in deltas)
    #         for d in dirs:
    #             os.makedirs(os.path.join(tmpdir, d), exist_ok=True)

    #         filenames = set()
    #         for delta in deltas:
    #             self.patchers[delta.filename].apply(delta)
    #             filenames.add(delta.filename)

    #         for filename in filenames:
    #             tmpf = os.path.join(tmpdir, filename)
    #             self.patchers[filename].write(tmpf)
    #             self.repo.index.add(tmpf, path_rewriter=lambda entry: filename)

    #         commits = [self.repo.commit(h) for h in original_commits]

    #         main_commit = commits[0]
    #         commit_message = main_commit.message

    #         if len(commits) > 1:
    #             # most recent commit
    #             main_commit = sorted(commits, key=lambda c: c.authored_datetime)[-1]

    #         commit_message += "\n\nautomatic commit by git-black, original commits:\n"
    #         commit_message += "\n".join(["  {}".format(c.hexsha) for c in commits])

    #         date_ts = main_commit.authored_date
    #         date_tz = altz_to_utctz_str(main_commit.author_tz_offset)
    #         self.repo.index.write()
    #         self.repo.index.commit(
    #             commit_message,
    #             author=main_commit.author,
    #             author_date="{} {}".format(date_ts, date_tz),
    #         )


@click.command()
def cli():
    gb = GitBlack()
    try:
        gb.commit_changes()
    except GitIndexNotEmpty:
        raise click.ClickException("staging area must be empty")


if __name__ == "__main__":
    cli()
