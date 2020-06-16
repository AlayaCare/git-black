import os
from bisect import bisect
from dataclasses import dataclass
from importlib.resources import read_text
from subprocess import PIPE, Popen, run
from tempfile import NamedTemporaryFile, TemporaryDirectory
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


@dataclass
class Delta:
    """this is a simplified version of unidiff.Hunk"""

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
    def from_hunk(hunk: Hunk, encoding: str):
        return Delta(
            src_start=hunk.source_start,
            src_lines=[line.value.encode(encoding) for line in hunk.source_lines()],
            dst_start=hunk.target_start,
            dst_lines=[line.value.encode(encoding) for line in hunk.target_lines()],
        )

    def __str__(self):
        s = f"Delta(\n    src_start={self.src_start},\n    src_lines=[\n"
        for line in self.src_lines:
            s += "        {!r},\n".format(line)
        s += f"    ],\n    dst_start={self.dst_start},\n    dst_lines=[\n"
        for line in self.dst_lines:
            s += "        {!r},\n".format(line)
        s += "    ]\n)"
        return s


class WorkingFile:
    def __init__(self, original_lines: str, deltas: List[Delta]):
        self._lines = original_lines
        self._deltas = deltas
        self._offsets = [0] * len(deltas)
        self._applied = {}

    @property
    def deltas(self):
        return self._deltas

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
        if filename not in self._blame_starts:
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

    @staticmethod
    def compute_origin(delta: Delta):
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

    def _commit_empty_deltas(self, working_file, filename):
        # if a delta has no target lines, it means stuff was just deleted
        # we'll commit those as ourselves (with no targe lines, there's
        # no entry in the blame anyway)
        with NamedTemporaryFile(dir=".") as f:
            found_empty_deltas = False
            for delta_idx, delta in enumerate(working_file.deltas):
                if delta.dst_lines:
                    continue
                found_empty_deltas = True
                working_file.apply(delta_idx)
                working_file.write(f.name)
                self.repo.index.add(
                    f.name, path_rewriter=lambda entry: filename, write=True
                )

            if found_empty_deltas:
                self.repo.index.commit("delete-only commit by git-black",)

    def commit_filename(self, filename):
        with TemporaryDirectory(dir=".") as tmpdir:
            tmpf = os.path.join(tmpdir, "b.py")

            reformat(filename)

            # why latin-1 ?
            # The PatchSet object demands an encoding, even when I think
            # it should treat its input as raw data with newlines, not text.
            # so I use an 8 bit reversible encoding just to make it happy
            # and I'll "encode" back to bytes when needed.
            # Even if the input is UTF-8 or anything else, this should work.

            patch_set = PatchSet(
                Popen(["git", "diff", "-U0", filename], stdout=PIPE,).stdout,
                encoding="latin-1",
            )
            original_lines = Popen(
                ["git", "show", "HEAD:" + filename], stdout=PIPE
            ).stdout.readlines()

            if not patch_set.modified_files:
                return

            mf = patch_set.modified_files[0]
            hunk_deltas = [Delta.from_hunk(hunk, "latin1") for hunk in mf]

            # let's map each hunk to its source commits and break down the deltas
            # in smaller chunks; this will let prepare and group commits with
            # a much smaller granularity
            deltas = []
            for hd in hunk_deltas:
                if not hd.dst_lines:
                    deltas.append(hd)
                    continue

                for src_linenos, dst_linenos in self.compute_origin(hd).items():
                    ss = hd.src_start + min(src_linenos, default=0)
                    sl = [hd.src_lines[lineno] for lineno in src_linenos]
                    ds = hd.dst_start + min(dst_linenos)
                    dl = [hd.dst_lines[lineno] for lineno in dst_linenos]
                    deltas.append(
                        Delta(src_start=ss, src_lines=sl, dst_start=ds, dst_lines=dl)
                    )

            working_file = WorkingFile(original_lines, deltas)

            delta_commits = {}
            for delta_idx, delta in enumerate(deltas):
                for line in range(delta.src_start, delta.src_start + delta.src_length):
                    commit = self.blame(filename, line)
                    delta_commits.setdefault(delta_idx, set()).add(commit.hexsha)

            grouped_deltas = {}
            for delta_idx, commits in delta_commits.items():
                t = tuple(sorted(commits))
                grouped_deltas.setdefault(t, []).append(delta_idx)

            self._commit_empty_deltas(working_file, filename)

            for commit_hashes, delta_idxs in grouped_deltas.items():

                for delta_idx in delta_idxs:
                    working_file.apply(delta_idx)

                working_file.write(tmpf)
                self.repo.index.add(
                    tmpf, path_rewriter=lambda entry: filename, write=True
                )

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

                date_ts = main_commit.authored_date
                date_tz = altz_to_utctz_str(main_commit.author_tz_offset)
                self.repo.index.commit(
                    commit_message,
                    author=main_commit.author,
                    author_date="{} {}".format(date_ts, date_tz),
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
