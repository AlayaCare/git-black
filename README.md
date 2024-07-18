# Git Black

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Reformat your source code without losing git's history

### Purpose

Git Black is a way of solving the problem of running an automated source code formatter,
like [Black][black] without overriding the authorship of a large portion of the code.

The goal is to adopt a consistent code style, but keeping `git blame` useful.

### Installation

In a Python virtualenv:

```bash
$ export PIP_EXTRA_INDEX_URL=<See docs for internal package registry>
$ pip install -U pip         # pygit2 has a problem with pip <= 18.1
$ pip install git-black
```

### Usage

Make sure your repository staging area is empty. Then do:

```bash
$ black my_code    # or prettier, or yapf, or gofmt, git-black doesn't care
$ git-black
```

### How does it work

Although Git Black was written with the express intention of running Black over a
relatively large code base (and the name reflects it), Git Black doesn't execute
or depend on Black in any way.

Git Black does this:

- looks at all uncommitted changes in the repository and keeps track of the
  author of each line
- groups all lines from all files that belong to the same original commit and
  creates _a new commit_ with the same author and date as the original
- the new commit's committer is the default committer; git-black doesn't try
  to hide itself
- adds the IDs of the original commits after the original message.


So if you have a blame like this:

```python
Adam  2015-04-14 ...    def some_func(self, arg):
Adam  2015-04-14 ...        assert SomeClass.__name__ in obj.clients, \
Eve   2018-11-05 ...            '{} is adding itself to {} clients.' \
Eve   2018-11-05 ...                .format(self.__class__.__name__, SomeClass.__name__)
John  2016-11-16 ...        obj.property = self.property
Eve   2016-12-15 ...        obj.long_property_name = self.long_property_name
Adam  2015-04-14 ...        obj.clients[
Adam  2015-04-14 ...            self.__class__.__name__
Adam  2015-04-14 ...        ] = self
Adam  2015-04-14 ...
Pete  2015-05-01 ...        obj.prop_dic.setdefault(self.SOME_LONG_NAME_CONST,
Pete  2015-05-01 ...                                SOME_LONG_NAME_CONST_DEFAULT)
```

After running `black` on that code, and then executing `git-black`, the same blame
would look like this:

```python
Adam  2015-04-14 ...  def some_func(self, arg):
Adam  2015-04-14 ...      assert (
Eve   2018-11-05 ...          SomeClass.__name__ in obj.clients
Eve   2018-11-05 ...      ), "{} is adding itself to {} clients.".format(
Eve   2018-11-05 ...          self.__class__.__name__, SomeClass.__name__
Eve   2018-11-05 ...      )
John  2016-11-16 ...      obj.property = self.property
Eve   2016-12-15 ...      obj.long_property_name = self.long_property_name
Adam  2015-04-14 ...      obj.clients[self.__class__.__name__] = self
Adam  2015-04-14 ...
Pete  2015-05-01 ...      obj.prop_dic.setdefault(
Pete  2015-05-01 ...          self.SOME_LONG_NAME_CONST, SOME_LONG_NAME_CONST_DEFAULT
Pete  2015-05-01 ...      )
```
