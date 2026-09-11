#!/bin/zsh
set -e
cd -- "${0:A:h}"
python3 studio.py pull beauty-centers-growth-01
python3 studio.py validate beauty-centers-growth-01
open http://127.0.0.1:4173
python3 studio.py serve
