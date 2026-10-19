#!/bin/sh
# Publishes the version in addon/__init__.py as a GitHub release. Everyone's
# add-on installs it within a day (or right away with "Check for updates").
# Usage: ./release.sh "What changed, in a sentence or two"
set -e
cd "$(dirname "$0")"
version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' addon/__init__.py)
if [ -z "$version" ]; then echo "Couldn't read VERSION from addon/__init__.py"; exit 1; fi
if ! git diff --quiet || ! git diff --cached --quiet; then echo "Commit your changes first."; exit 1; fi
if gh release view "v$version" >/dev/null 2>&1; then echo "v$version is already released. Bump VERSION first."; exit 1; fi
./build.sh
git push
gh release create "v$version" dist/friends_leaderboard.ankiaddon --title "v$version" --notes "${1:-Version $version}"
