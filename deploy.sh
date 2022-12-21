#!/bin/sh

set -e

CURDIR=$(dirname "$0")
SITEDIR=${1-"$CURDIR"/_site}

rm -rf "$SITEDIR"
mkdir -p "$SITEDIR"
cp -a "$CURDIR"/assets/* "$SITEDIR"/
"$CURDIR"/render.py > "$SITEDIR"/index.html
