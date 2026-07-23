#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/python -c "from src.trilobite.server import main; main()"
