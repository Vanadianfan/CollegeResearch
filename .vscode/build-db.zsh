#!/bin/zsh
set -e

".venv/bin/python" -m rail_data.build.main
echo
".venv/bin/python" -m population_data.build
echo
".venv/bin/python" -m centrality.calculate
echo
".venv/bin/python" -m visualizers.network_db "rail_network.sqlite" --open
