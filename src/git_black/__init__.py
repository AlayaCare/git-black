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

    def compute_source_mapping(self, hunk: Hunk):
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

        if hunk.source_length == 0:
            return [(-1,)] * hunk.target_length

        result = []
        if hunk.source_length < hunk.target_length:
            for i in range(hunk.source_length):
                result.append((i,))
            for i in range(hunk.target_length - hunk.source_length):
                result.append(hunk.source_length - 1)
        else:
            for i in range(hunk.target_length - 1):
                result.append((i,))
            result.append(tuple(range(hunk.target_length - 1, hunk.source_length)))

        target_lines = list(hunk.source_length)
        current_source_line = 0
        current_source_column = 0
        current_target_line = 0

        matches = []

        relevant_tokens = re.compile(r"\w+")

        a_preview = []
        b_preview = []
        b_col = 0

        for current_target_line, b_line in enumerate(hunk.target_lines()):
            for b_match in relevant_tokens.finditer(b_line.value):
                b_word = b_match.group()
                if current_source_line >= len(source_lines):
                    # we've reached the end, all remaining words/lines will be linked
                    # to the last source line
                    continue
                while current_source_line < len(source_lines):
                    a_line: Line = source_lines[current_source_line]
                    pos = a_line.value.find(b_word, current_source_column)
                    if pos == -1:
                        current_source_line += 1
                        current_source_column = 0
                        continue
                    match = (
                        current_source_line,
                        pos,
                        current_target_line,
                        b_match.start(),
                        len(b_word),
                    )
                    matches.append(match)
                    current_source_column += len(b_word)
                    break

        print(hunk)
        print("matches:", matches)

        a = list(line.value for line in hunk.source_lines())
        b = list(line.value for line in hunk.target_lines())
        # a_words = [m.group() for m in re.finditer(r"\w+", a)]
        # b_words = [m.group() for m in re.finditer(r"\w+", b)]
        # sm = SequenceMatcher(a=a_words, b=b_words, autojunk=False)
        # print(hunk)

        self.render_groups(a, b, matches)

        # # determine "sync points"
        # # print(repr(a))
        # # print(repr(b))
        # sync_points = []
        # for m in sm.get_matching_blocks():
        #     print(m)
        #     a_end = m.a + m.size
        #     b_end = m.b + m.size
        #     if m.size == 0:
        #         a_end = len(a)
        #         b_end = len(b)
        #     sync_points.append((a_end, b_end))

        # print(sync_points)

        # result = []
        # current_line = []
        # a_lineno = 1
        # b_lineno = 1
        # for m in sm.get_matching_blocks():
        #     if m.size == 0:
        #         break
        #     a_lineno += a[m.a:m.a+m.size].count("\n")
        #     b_lineno += a[]

        #     print("   ", m)
        #     for c in

    def commit_filename(self, filename):
        with TemporaryDirectory(dir=".") as tmpdir:
            a = os.path.join(tmpdir, "a")
            b = os.path.join(tmpdir, "b")
            # c = os.path.join(tmpdir, "c")

            # def separate(text):
            #     return re.sub(r"(\n{2,})", "\n---------git-black---------\n\1", text)

            # t = open(filename, "r").read()
            # f = open(a, "w")
            # f.write(separate(t))
            # f.close()

            # shutil.copy(filename, c)
            # reformat(c)

            # t = open(c, "r").read()
            # f = open(b, "w")
            # f.write(separate(t))
            # f.close()

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
            for hunk in sorted(mf, key=lambda hunk: hunk.source_start):
                self.a_html.append(
                    '<span class="unchanged">'
                    + ("".join(a_lines[a_start : hunk.source_start - 1]))
                    + "</span>"
                )
                self.b_html.append(
                    '<span class="unchanged">'
                    + ("".join(b_lines[b_start : hunk.target_start - 1]))
                    + "</span>"
                )
                a_start = hunk.source_start + hunk.source_length - 1
                b_start = hunk.target_start + hunk.target_length - 1
                self.compute_source_mapping(hunk)

            template = jinja_env.get_template("groups.j2.html")
            f = open("groups.html", "w")
            f.write(
                template.render(
                    a=self.a_html,  # .getvalue().replace("\n", "↲\n"),
                    b=self.b_html,  # .getvalue().replace("\n", "↲\n"),
                )
            )
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
