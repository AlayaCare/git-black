import os
import sys
from bisect import bisect
from collections import namedtuple
from dataclasses import dataclass
from importlib.resources import read_text
from subprocess import PIPE, Popen, run
from tempfile import TemporaryDirectory
from typing import List

import click
from git import Commit, Repo
from git.objects.util import altz_to_utctz_str
from jinja2 import Environment, FunctionLoader
from unidiff import Hunk, PatchSet


def load_template(template):
    return read_text(__package__, template, "utf-8")


jinja_env = Environment(loader=FunctionLoader(load_template))
jinja_env.filters["zip"] = zip


def reformat(a):
    run(["black", "-l89", a])


@dataclass(frozen=True)
class Delta:
    """this is a simplified version of unidiff.Hunk"""

    filename: str
    src_start: int
    src_lines: List[str]
    dst_start: int
    dst_lines: List[str]

    @property
    def offset(self):
        return len(self.dst_lines) - len(self.src_lines)

    @property
    def src_length(self):
        return len(self.src_lines)

    @property
    def dst_length(self):
        return len(self.dst_lines)

    @staticmethod
    def from_hunk(hunk: Hunk, filename, encoding: str):
        return Delta(
            filename=filename,
            src_start=hunk.source_start,
            src_lines=[line.value.encode(encoding) for line in hunk.source_lines()],
            dst_start=hunk.target_start,
            dst_lines=[line.value.encode(encoding) for line in hunk.target_lines()],
        )

    def __str__(self):
        s = [
            "Delta(" f"    filename={self.filename},",
            f"    src_start={self.src_start},",
            "    src_lines=[",
        ]
        for line in self.src_lines:
            s.append("        {!r},".format(line))
        s.extend(["    ],", f"    dst_start={self.dst_start},", "    dst_lines=["])
        for line in self.dst_lines:
            s.append("        {!r},".format(line))
        s.append("    ])")
        return "\n".join(s)


DeltaBlame = namedtuple("DeltaBlame", "delta commits")


class HunkBlamer:
    PATCH_ENCODING = "latin-1"

    def __init__(self, repo, filename):
        self.repo = repo
        self.filename = filename
        patch_set = PatchSet(
            Popen(["git", "diff", "-U0", filename], stdout=PIPE,).stdout,
            encoding=self.PATCH_ENCODING,
        )

        self.modified_file = None

        if patch_set.modified_files:
            self.modified_file = patch_set.modified_files[0]

        # if a file only has deleted lines, unidiff thinks it was
        # deleted; but if we got this far, it's because git
        # showed it as a modified file
        if patch_set.removed_files:
            self.modified_file = patch_set.removed_files[0]

        if not self.modified_file:
            return

        self._load_blame()

    def _load_blame(self):
        _blame = sorted(
            [
                (e.linenos.start, e.commit)
                for e in self.repo.blame_incremental("HEAD", self.modified_file.path)
            ]
        )
        self._blame_starts = []
        self._blame_commits = []
        for line, commit in _blame:
            self._blame_starts.append(line)
            self._blame_commits.append(commit)

    def _blame(self, lineno) -> Commit:
        idx = bisect(self._blame_starts, lineno) - 1
        return self._blame_commits[idx]

    def _map_lines(self, delta: Delta):
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

        if delta.src_length == 0:
            return {(): tuple(range(delta.dst_length))}
        if delta.dst_length == 0:
            return {tuple(range(delta.src_length)): ()}

        result = {}

        for i in range(min(delta.src_length, delta.dst_length) - 1):
            result[(i,)] = (i,)

        if delta.src_length >= delta.dst_length:
            result[tuple(range(delta.dst_length - 1, delta.src_length))] = (
                delta.dst_length - 1,
            )
        else:
            result[(delta.src_length - 1,)] = tuple(
                range(delta.src_length - 1, delta.dst_length)
            )

        return result

    def blames(self) -> List[DeltaBlame]:
        if not self.modified_file:
            return []

        hunk_deltas = [
            Delta.from_hunk(hunk, self.filename, self.PATCH_ENCODING)
            for hunk in self.modified_file
        ]

        # let's map each hunk to its source commits and break down the deltas
        # in smaller chunks; this will make it possible to prepare and group
        # commits with a much smaller granularity
        deltas = []
        for hd in hunk_deltas:
            for src_linenos, dst_linenos in self._map_lines(hd).items():
                ss = hd.src_start + min(src_linenos, default=0)
                sl = [hd.src_lines[lineno] for lineno in src_linenos]
                ds = hd.dst_start + min(dst_linenos, default=0)
                dl = [hd.dst_lines[lineno] for lineno in dst_linenos]
                delta = Delta(
                    filename=self.filename,
                    src_start=ss,
                    src_lines=sl,
                    dst_start=ds,
                    dst_lines=dl,
                )
                deltas.append(delta)

        blames = []
        for i, delta in enumerate(deltas):
            delta_blame = DeltaBlame(delta=delta, commits=set())
            for line in range(
                delta.src_start, delta.src_start + max(1, delta.src_length)
            ):
                commit = self._blame(line)
                delta_blame.commits.add(commit.hexsha)
            blames.append(delta_blame)

        return blames


class Patcher:
    def __init__(self, repo, filename):
        self.repo = repo
        self.filename = filename
        self._offsets = {}
        self._lines = Popen(
            ["git", "show", "HEAD:" + self.filename], stdout=PIPE
        ).stdout.readlines()
        self._applied = set()

    def apply(self, delta):
        if (delta.src_start) in self._applied:
            return

        src_length = len(delta.src_lines)
        src_start = delta.src_start
        for pos, off in self._offsets.items():
            if delta.src_start > pos:
                src_start += off

        # I don't understand why, but unified diff needs
        # this when the source length is 0
        if src_length == 0:
            src_start += 1

        i = src_start - 1
        j = i + src_length
        self._lines[i:j] = delta.dst_lines

        self._offsets[delta.src_start] = delta.offset

        self._applied.add(delta.src_start)

    def write(self, filename):
        f = open(filename, "wb")
        f.writelines(self._lines)
        f.close()


class GitBlack:
    def __init__(self):
        self.repo = Repo(search_parent_directories=True)
        self._blame_starts = {}
        self._blame_commits = {}
        self.a_html = []
        self.b_html = []
        self.color_idx = 0
        self.patchers = {}

    def commit_changes(self):
        sys.stdout.write("Reading changes... ")
        sys.stdout.flush()
        grouped_deltas = {}
        submodules = set(s.path for s in self.repo.submodules)
        for diff in self.repo.index.diff(None):
            if diff.change_type != "M":
                continue
            if diff.a_path in submodules:
                continue
            filename = diff.a_path
            self.patchers[filename] = Patcher(self.repo, filename)
            hb = HunkBlamer(self.repo, filename)
            for delta_blame in hb.blames():
                commits = tuple(sorted(delta_blame.commits))
                grouped_deltas.setdefault(commits, []).append(delta_blame.delta)

        sys.stdout.write("done.")
        total = len(grouped_deltas)
        progress = 0
        for commits, deltas in grouped_deltas.items():
            self._commit(commits, deltas)
            progress += 1
            sys.stdout.write("Making commit {}/{} \r".format(progress, total))
            sys.stdout.flush()

    def _commit(self, original_commits, deltas: List[Delta]):
        with TemporaryDirectory(dir=".") as tmpdir:

            dirs = set(os.path.dirname(d.filename) for d in deltas)
            for d in dirs:
                os.makedirs(os.path.join(tmpdir, d), exist_ok=True)

            filenames = set()
            for delta in deltas:
                self.patchers[delta.filename].apply(delta)
                filenames.add(delta.filename)

            for filename in filenames:
                tmpf = os.path.join(tmpdir, filename)
                self.patchers[filename].write(tmpf)
                self.repo.index.add(tmpf, path_rewriter=lambda entry: filename)

            commits = [self.repo.commit(h) for h in original_commits]

            main_commit = commits[0]
            commit_message = main_commit.message

            if len(commits) > 1:
                # most recent commit
                main_commit = sorted(commits, key=lambda c: c.authored_datetime)[-1]

            commit_message += "\n\nautomatic commit by git-black, original commits:\n"
            commit_message += "\n".join(["  {}".format(c.hexsha) for c in commits])

            date_ts = main_commit.authored_date
            date_tz = altz_to_utctz_str(main_commit.author_tz_offset)
            self.repo.index.write()
            self.repo.index.commit(
                commit_message,
                author=main_commit.author,
                author_date="{} {}".format(date_ts, date_tz),
            )


@click.command()
def cli():
    gb = GitBlack()
    gb.commit_changes()


if __name__ == "__main__":
    cli()
