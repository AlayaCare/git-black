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
