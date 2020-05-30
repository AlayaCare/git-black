import re
from subprocess import PIPE, Popen

import click
from git import Repo
from git.diff import Diff

commit_re = re.compile(rb"(?P<commit>[0-9a-f]{40})\s+\d+\s+(?P<lineno>\d+)")


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
@click.argument("filename", required=False, default="")
def cli(filename):
    repo = Repo(search_parent_directories=True)
    print(repo)
    for diff in repo.index.diff(None):
        print(diff)
    # blame = git_blame(filename)
    # for commit, lines in blame.items():
    #    print(commit)
    #    for line in lines:
    #        print("   ", line)


if __name__ == "__main__":
    cli()
