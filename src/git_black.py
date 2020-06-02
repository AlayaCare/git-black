import os
import re
import shutil
import sys
import time
from datetime import datetime
from subprocess import PIPE, Popen, run
from tempfile import NamedTemporaryFile, TemporaryDirectory

import click
from git import Repo
from git.diff import Diff
from git.util import Actor
from unidiff import Hunk, PatchSet

commit_re = re.compile(rb"(?P<commit>[0-9a-f]{40})\s+\d+\s+(?P<lineno>\d+)")


def reformat(a):
    run(["black", a])


class GitBlack:
    def __init__(self):
        self.repo = Repo(search_parent_directories=True)

    def commit_filename(self, filename):
        blame_map = sorted(
            self.repo.blame_incremental("HEAD", filename),
            key=lambda blame_entry: blame_entry.linenos.start,
        )

        with TemporaryDirectory(dir=".") as tmpdir:
            a = os.path.join(tmpdir, "a")
            b = os.path.join(tmpdir, "b")
            shutil.copy(filename, a)
            # shutil.copy(a, b)
            reformat(filename)

            # why latin-1 ?
            # The PatchSet object demands an encoding, even when I think
            # it should treat its input as raw data with newlines, not text.
            # so I use an 8 bit reversible encoding just to make it happy
            # but I'll "encode" back to bytes when needed.
            # Even if the input is UTF-8 or anything else, this should work.

            patch_set = PatchSet(
                Popen(
                    ["git", "diff", "--patience", "-U0", filename], stdout=PIPE
                ).stdout,
                encoding="latin-1",
            )

            if not patch_set.modified_files:
                return

            mf = patch_set.modified_files[0]

            for hunk in sorted(mf, key=lambda hunk: -hunk.source_start):
                target_lines = [
                    line.value.encode("latin-1") for line in hunk.target_lines()
                ]
                self.apply(a, b, hunk.source_start, hunk.source_length, target_lines)
                os.rename(b, a)

                self.repo.index.add(a, path_rewriter=lambda entry: filename, write=True)
                self.repo.index.commit(
                    "hunk {}-{}".format(hunk.source_start, hunk.source_length),
                    author=Actor("John Doe", "johndoe@example.com"),
                    author_date=datetime.now()
                    .replace(minute=0)
                    .isoformat(timespec="seconds"),
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
