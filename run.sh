#!/bin/sh
exec sudo "$(dirname "$0")/venv/bin/python" "$(dirname "$0")/overclock.py" "$@"
