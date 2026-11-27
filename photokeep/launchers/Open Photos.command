#!/bin/sh
cd "$(dirname "$0")" && PYTHONPATH=app exec python3 -m photokeep serve .
