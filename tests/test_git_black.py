import os
from subprocess import run
from tempfile import TemporaryDirectory

from git_black import git_black

black_tests = b"""
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


def test_git_black(tmpdir):
    os.chdir(tmpdir)

    with open("blacktests.py", "wb") as f:
        f.write(black_tests)

    run(["git", "init"])
    run(["git", "add", "blacktests.py"])
    run(["git", "commit", "-m", "testing git-black"])

    git_black("blacktests.py")

    log = run(["git", "log", "--format=format:%s"], capture_output=True).stdout
    assert log == (
        b"testing git-black\ndelete-only commit by git-black\ntesting git-black"
    )
    assert run(["black", "--check", "blacktests.py"]).returncode == 0
