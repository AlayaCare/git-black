import os
from bisect import bisect
from collections import namedtuple
from dataclasses import dataclass
from importlib.resources import read_text
from subprocess import PIPE, Popen, run
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import List

import click
from git import Commit, Repo
from git.objects.util import altz_to_utctz_str
from jinja2 import Environment, FunctionLoader
from unidiff import Hunk, PatchedFile, PatchSet


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
        if not patch_set.modified_files:
            return
        self.modified_file = patch_set.modified_files[0]
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
        self.patchers = {}

    def commit_changes(self):
        grouped_deltas = {}
        for diff in self.repo.index.diff(None):
            if diff.change_type != "M":
                continue
            filename = diff.a_path
            self.patchers[filename] = Patcher(self.repo, filename)
            hb = HunkBlamer(self.repo, filename)
            for delta_blame in hb.blames():
                commits = tuple(sorted(delta_blame.commits))
                grouped_deltas.setdefault(commits, []).append(delta_blame.delta)

        for commits, deltas in grouped_deltas.items():
            self._commit(commits, deltas)

    def _commit(self, original_commits, deltas: List[Delta]):
        print("comitting {}...".format(original_commits))
        with TemporaryDirectory(dir=".") as tmpdir:

            dirs = set(os.path.dirname(d.filename) for d in deltas)
            for d in dirs:
                os.makedirs(os.path.join(tmpdir, d), exist_ok=True)

            for delta in deltas:
                filename = delta.filename
                patcher = self.patchers[filename]
                tmpf = os.path.join(tmpdir, filename)

                patcher.apply(delta)
                patcher.write(tmpf)

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

            working_file = WorkingFile(original_lines, deltas)

            grouped_deltas = {}
            for delta_idx, commits in delta_commits.items():
                t = tuple(sorted(commits))
                grouped_deltas.setdefault(t, []).append(delta_idx)

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


# def git_black(filename):
#     gb.commit_filename(filename)


@click.command()
# @click.argument("filename")
def cli():
    gb = GitBlack()
    gb.commit_changes()
    # git_black(filename)


if __name__ == "__main__":
    cli()
