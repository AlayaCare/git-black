import os
from subprocess import PIPE, Popen, run
from textwrap import dedent

import py
import pytest

from git_black import Delta, GitBlack


@pytest.fixture
def in_tmpdir(tmpdir):
    with tmpdir.as_cwd():
        yield tmpdir


@pytest.fixture
def tmp_repo(tmpdir):
    with tmpdir.as_cwd():
        run(["git", "init", "--quiet"])
        yield tmpdir


@pytest.fixture
def unblacked_file(tmp_repo):
    f = tmp_repo.join("unblacked_file.py")
    f.write(
        dedent(
            """
        from collections import (
                namedtuple
        )


        def func1(
                a,
                b):
            pass

        def func2():
            return [
                'one',
                'two',
                'three',
            ]

        def func3():
            return 3
        def func4():
            return 4

        @property
        def some_long_name(self):
            if not self.condition:
                return None
            return self.some_long_value or \
                (self.object1.property1.property2
                if self.object1 and self.object1.property1 else None)




        def func5():
            pass




        def func6():
            pass
        """
        ).encode()
    )
    return f


def git_add(path):
    run(["git", "add", str(path)])


def git_commit(msg):
    run(["git", "commit", "-m", msg])


def git_log():
    return [
        line.decode().strip()
        for line in Popen(["git", "log", r"--format=format:%s"], stdout=PIPE).stdout
    ]


def git_black():
    gb = GitBlack()
    gb.commit_changes()


def test_git_black(tmp_repo, unblacked_file):

    git_add(unblacked_file)
    git_commit("testing git-black")

    gb = GitBlack()
    gb.commit_changes()

    log = git_log()
    assert log == (["testing git-black", "testing git-black"])
    assert run(["black", "--check", "blacktests.py"]).returncode == 0


def test_insert_only(tmp_repo):
    a = py.path.local("a.py")
    a.write(
        dedent(
            """
            line1
            line2
            line3
            """
        )
    )
    git_add(a)
    git_commit("commit1")

    a.write(
        dedent(
            """
            line1
            """
        )
    )

    git_black()

    assert git_log() == ["commit2", "commit1"]


# @pytest.mark.parametrize(
#    ("src", "dst", "expected"),
#    [
#        ("abc", "abcde", [(0,), (1,), (2,), (2,), (2,)]),
#        ("abcde", "abc", [(0,), (1,), (2, 3, 4)]),
#        ("", "abc", [(), (), ()]),
#        ("abc", "", []),
#    ],
# )
# def test_delta_origins(src, dst, expected):
#    delta = Delta(src_start=0, src_lines=src, dst_start=0, dst_lines=dst)
#    assert GitBlack.compute_origin(delta) == expected
