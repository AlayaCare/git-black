import os
import re
import shutil
import sys
import time
from bisect import bisect
from datetime import datetime
from difflib import SequenceMatcher
from email.utils import format_datetime
from importlib.resources import read_text
from io import StringIO
from subprocess import PIPE, Popen, run
from tempfile import NamedTemporaryFile, TemporaryDirectory

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


class GitBlack:
    def __init__(self):
        self.repo = Repo(search_parent_directories=True)
        self._blame_starts = {}
        self._blame_commits = {}
        self.a_html = []
        self.b_html = []
        self.color_idx = 0

    def commit_for_line(self, filename, lineno) -> Commit:
        if not filename in self._blame_starts:
            blame = sorted(
                [
                    (e.linenos.start, e.commit)
                    for e in self.repo.blame_incremental("HEAD", filename)
                ]
            )
            self._blame_starts[filename] = []
            self._blame_commits[filename] = []
            for line, commit in blame:
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

        result = []
        # if hunk.source_length == 0:
        #     result = [(-1,)] * hunk.target_length

        if hunk.source_length < hunk.target_length:
            for i in range(hunk.source_length):
                result.append((i,))
            for i in range(hunk.target_length - hunk.source_length):
                result.append((hunk.source_length - 1,))
        else:
            for i in range(hunk.target_length - 1):
                result.append((i,))
            result.append(tuple(range(hunk.target_length - 1, hunk.source_length)))
        # print("---- hunk ----")
        # print(hunk)
        # print("hunk.target_length:", hunk.target_length)
        # print("---- result ----")
        # print(result)
        return result

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

            a_lines = open(a).readlines()
            b_lines = open(b).readlines()
            a_start = 0
            b_start = 0
            original_commits = {}
            for hunk in sorted(mf, key=lambda hunk: hunk.source_start):
                origin = self.compute_origin(hunk)
                for i, t in enumerate(origin):
                    for l in t:
                        target_line = hunk.target_start + i
                        origin_line = max(1, hunk.source_start + l)
                        original_commits[target_line] = self.commit_for_line(
                            filename, origin_line
                        )

            for k in sorted(original_commits.keys()):
                print(k, original_commits[k])

            return

            for hunk in sorted(mf, key=lambda hunk: -hunk.source_start):
                continue
                target_lines = [
                    line.value.encode("latin-1") for line in hunk.target_lines()
                ]
                self.apply(a, b, hunk.source_start, hunk.source_length, target_lines)
                os.rename(b, a)

                original_commit = self.commit_for_line(filename, hunk.source_start)

                self.repo.index.add(a, path_rewriter=lambda entry: filename, write=True)
                self.repo.index.commit(
                    original_commit.message,
                    author=original_commit.author,
                    author_date=format_datetime(original_commit.authored_datetime),
                )

    def apply(
        self, a: str, b: str, source_start: int, source_length: int, target_lines: list
    ):
        """copy `a` to `b`, but apply the (source_start, source_length, lines) patch"""
        with open(a, "rb") as f:
            source_lines = f.readlines()

        # I don't understand why, but unified diff needs
        # this when the source length is 0
        if source_length == 0:
            source_start += 1

        with open(b, "wb") as f:
            f.writelines(source_lines[0 : source_start - 1])
            f.writelines(target_lines)
            f.writelines(source_lines[source_start + source_length - 1 :])


def git_blame(filename):
    p = Popen(["git", "blame", "-p", filename], stdout=PIPE)
    blame = {}
    for porcelain_line in p.stdout:
        m = commit_re.match(porcelain_line)
        if m:
            commit = m.group("commit").decode()
            lineno = int(m.group("lineno"))
        if porcelain_line.startswith(b"\t"):
            line = porcelain_line[1:]
            blame.setdefault(commit, []).append((lineno, line))
    return blame


@click.command()
@click.argument("filename")
def cli(filename):
    gb = GitBlack()
    gb.commit_filename(filename)


if __name__ == "__main__":
    cli()
