import os
import re
import shutil
import sys
from subprocess import PIPE, Popen, run
from tempfile import NamedTemporaryFile, TemporaryDirectory

import click
from git import Repo
from git.diff import Diff
from unidiff import Hunk, PatchSet

commit_re = re.compile(rb"(?P<commit>[0-9a-f]{40})\s+\d+\s+(?P<lineno>\d+)")


def reformat(a):
    run(["black", a])


class GitBlack:
    def __init__(self):
        self.repo = Repo(search_parent_directories=True)

    def commit_filename(self, filename):
        with TemporaryDirectory(dir=".") as tmpdir:
            # a = os.path.join(tmpdir, "a.py")
            # b = os.path.join(tmpdir, "b.py")
            # shutil.copy(filename, a)
            # shutil.copy(a, b)
            reformat(filename)

            patch_set = PatchSet(
                Popen(
                    ["git", "diff", "--patience", "-U0", filename],
                    stdout=PIPE
                    # ["git", "diff", "--patience", "--no-index", "-U0", a, b], stdout=PIPE
                ).stdout,
                encoding="latin-1",
            )

            for mf in patch_set.modified_files:
                for hunk in mf:
                    print(hunk.source_start, hunk.source_length)
                    print(hunk.target_start, hunk.target_length)
                    target_lines = [
                        line.value.encode("latin-1") for line in hunk.target_lines()
                    ]
                    stage_lines(
                        repo,
                        filename,
                        hunk.source_start,
                        hunk.source_length,
                        target_lines,
                    )
                    # sys.exit(1)
                    print("committing hunk:", hunk)
                    repo.index.commit(
                        "hunk {}-{}".format(hunk.source_start, hunk.source_length)
                    )
                    # repo.index.write()
                    # each one of these hunks will become one or more commits


def path_rewriter(entry):
    print("path_rewrite(args={!r}, kwargs={!r}".format(args, kwargs))
    return entry.path


def stage_lines(
    repo, filename: str, source_start: int, source_length: int, target_lines: list
):
    f = Popen(["git", "show", "HEAD:" + filename], stdout=PIPE)
    lines = [None] + f.stdout.readlines()
    print("lines={!r}".format(lines))
    print(
        "filename={!r} start={} length={}".format(filename, source_start, source_length)
    )
    print("target_lines: {!r}".format(target_lines))

    def write_lines(f, lines):
        print("writing:\n", lines)
        f.writelines(lines)

    with NamedTemporaryFile(dir=".") as tmpf:
        write_lines(tmpf.file, lines[1:source_start])
        write_lines(tmpf.file, target_lines)
        write_lines(tmpf.file, lines[source_start + source_length :])
        tmpf.flush()
        repo.index.add(tmpf.name, path_rewriter=lambda entry: filename, write=True)


def commit_hunk(hunk: Hunk):
    # prepare a commit that includes _only_ the changes that happened in the provided hunk
    a_s = hunk.source_start
    a_l = hunk.source_length
    b_s = hunk.target_start
    b_l = hunk.target_length

    pass


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
    repo = Repo(search_parent_directories=True)
    # print(repo)
    # for diff in repo.index.diff(None):
    #    print(diff)
    list_patches(repo, filename)

    # blame = git_blame(filename)
    # for commit, lines in blame.items():
    #    print(commit)
    #    for line in lines:
    #        print("   ", line)


if __name__ == "__main__":
    cli()
