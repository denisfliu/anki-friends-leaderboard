#!/bin/sh
# Packages the add-on as dist/friends_leaderboard.ankiaddon.
# Fill in server_url in addon/config.json first so friends don't have to.
set -e
cd "$(dirname "$0")"
mkdir -p dist
rm -f dist/friends_leaderboard.ankiaddon
cd addon
zip -q -r ../dist/friends_leaderboard.ankiaddon . -x '__pycache__/*' '*.pyc' 'meta.json'
echo "Built dist/friends_leaderboard.ankiaddon"
