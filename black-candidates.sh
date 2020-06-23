#!/bin/bash

# check all directories that contain '*.py' files
find alaya/ tests/ -type f -name '*.py' | xargs -n1 dirname | sort -u | while read dir
do
    black --check -q "$dir" && echo "already good: $dir"
done
