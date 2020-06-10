import os
import re
import shutil
import sys
import time
from bisect import bisect
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from email.utils import format_datetime
from importlib.resources import read_text
from io import StringIO
from subprocess import PIPE, Popen, run
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import List

import click
from git import Commit, Repo
from git.diff import Diff
from git.util import Actor
from jinja2 import Environment, FunctionLoader
from unidiff import Hunk, PatchSet
from unidiff.patch import Line

commit_re = re.compile(rb"(?P<commit>[0-9a-f]{40})\s+\d+\s+(?P<lineno>\d+)")


def load_template(template):
    return read_text(__package__, template, "utf-8")


jinja_env = Environment(loader=FunctionLoader(load_template))
jinja_env.filters["zip"] = zip


def reformat(a):
    run(["black", "-l89", a])


@dataclass
class Delta:
    """this is a simplified version of unidiff.Hunk"""

    src_start: int
    src_lines: List[str]
    dst_start: int
    dst_lines: List[str]
    offset: int = field(init=False)

    def __post_init__(self):
        self.offset = len(self.dst_lines) - len(self.src_lines)

    @staticmethod
    def from_hunk(hunk: Hunk, encoding: str):
        return Delta(
            src_start=hunk.source_start,
            src_lines=[line.value.encode(encoding) for line in hunk.source_lines()],
            dst_start=hunk.target_start,
            dst_lines=[line.value.encode(encoding) for line in hunk.target_lines()],
        )


class WorkingFile:
    def __init__(self, source_file: str, deltas: List[Delta]):
        self._lines = open(source_file, "rb").readlines()
        self._deltas = deltas
        self._offsets = [0] * len(deltas)
        self._applied = {}

    def apply(self, idx):
        if idx in self._applied:
            return
        delta = self._deltas[idx]

        src_length = len(delta.src_lines)
        src_start = delta.src_start + self._offsets[idx]

        # I don't understand why, but unified diff needs
        # this when the source length is 0
        if src_length == 0:
            src_start += 1

        i = src_start - 1
        j = i + src_length
        self._lines[i:j] = delta.dst_lines

        for i in range(idx + 1, len(self._deltas)):
            self._offsets[i] += delta.offset

        self._applied[idx] = True

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

    def blame(self, filename, lineno) -> Commit:
        if not filename in self._blame_starts:
            _blame = sorted(
                [
                    (e.linenos.start, e.commit)
                    for e in self.repo.blame_incremental("HEAD", filename)
                ]
            )
            self._blame_starts[filename] = []
            self._blame_commits[filename] = []
            for line, commit in _blame:
                self._blame_starts[filename].append(line)
                self._blame_commits[filename].append(commit)

        idx = bisect(self._blame_starts[filename], lineno) - 1
        return self._blame_commits[filename][idx]

    def compute_origin(self, hunk: Hunk):
        """
        compute which line or lines from the hunk source end up
        in each line of the hunk target

        returns a list of tuples to be interpreted like this:

        [
            (1,),       # target line 1 comes from source line 1
            (2,3)       # target line 2 comes from source lines 2 and 3
            (),         # target line 3 doesn't come from the source (an inserted line)
            (6,),       # target line 4 comes from line 6
        ]

        the fact that source lines 4 and 5 never appear in the result means
        those lines were deleted.
        """

        # this is harder than I thought; I'll start with a super naive
        # approach and improve it later (or never)

        result = []

        if hunk.source_length < hunk.target_length:
            for i in range(hunk.source_length):
                result.append((i,))
            for i in range(hunk.target_length - hunk.source_length):
                result.append((hunk.source_length - 1,))
        elif hunk.target_length > 0:
            for i in range(hunk.target_length - 1):
                result.append((i,))
            result.append(tuple(range(hunk.target_length - 1, hunk.source_length)))

        return result

    def _commit_empty_hunks(self, filename, tmpfile, working_file, hunks):
        # if a hunk has no target lines, it means stuff was just deleted
        # we'll commit those as ourselves (with no targe lines, there's
        # no entry in the blame anyway)
        for hunk_idx, hunk in enumerate(hunks):
            if hunk.target_length > 0:
                continue
            working_file.apply(hunk_idx)
            working_file.write(tmpfile)
            self.repo.index.add(
                tmpfile, path_rewriter=lambda entry: filename, write=True
            )

        self.repo.index.commit("delete-only commit by git-black",)

    def commit_filename(self, filename):
        with TemporaryDirectory(dir=".") as tmpdir:
            a = os.path.join(tmpdir, "a")
            b = os.path.join(tmpdir, "b")

            shutil.copy(filename, a)
            shutil.copy(filename, b)

            reformat(b)

            # why latin-1 ?
            # The PatchSet object demands an encoding, even when I think
            # it should treat its input as raw data with newlines, not text.
            # so I use an 8 bit reversible encoding just to make it happy
            # and I'll "encode" back to bytes when needed.
            # Even if the input is UTF-8 or anything else, this should work.

            patch_set = PatchSet(
                Popen(["git", "diff", "-U0", "--no-index", a, b], stdout=PIPE,).stdout,
                encoding="latin-1",
            )

            if not patch_set.modified_files:
                return

            mf = patch_set.modified_files[0]
            hunks = list(mf)

            working_file = WorkingFile(
                filename, [Delta.from_hunk(h, "latin1") for h in hunks]
            )

            # lest map each hunk to its source commits
            hunk_commits = {}
            for hunk_idx, hunk in enumerate(hunks):
                for t in self.compute_origin(hunk):
                    hunk_commits.setdefault(hunk_idx, set())
                    for l in t:
                        origin_line = max(1, hunk.source_start + l)
                        commit = self.blame(filename, origin_line)
                        hunk_commits[hunk_idx].add(commit.hexsha)

            grouped_hunks = {}
            for hunk_idx, commits in hunk_commits.items():
                t = tuple(sorted(commits))
                grouped_hunks.setdefault(t, []).append(hunk_idx)

            self._commit_empty_hunks(filename, a, working_file, hunks)

            for commit_hashes, hunk_idxs in grouped_hunks.items():

                for hunk_idx in hunk_idxs:
                    working_file.apply(hunk_idx)

                working_file.write(a)
                self.repo.index.add(a, path_rewriter=lambda entry: filename, write=True)

                commits = [self.repo.commit(h) for h in commit_hashes]

                main_commit = commits[0]
                commit_message = main_commit.message

                if len(commits) > 1:
                    # most recent commit
                    main_commit = sorted(commits, key=lambda c: c.authored_datetime)[-1]

                commit_message += (
                    "\n\nautomatic commit by git-black, original commits:\n"
                )
                commit_message += "\n".join(["  {}".format(c.hexsha) for c in commits])

                self.repo.index.commit(
                    commit_message,
                    author=main_commit.author,
                    author_date=format_datetime(main_commit.authored_datetime),
                )

            working_file.write(filename)


def git_black(filename):
    gb = GitBlack()
    gb.commit_filename(filename)


@click.command()
@click.argument("filename")
def cli(filename):
    git_black(filename)


if __name__ == "__main__":
    cli()
