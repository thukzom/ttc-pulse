# Convenience targets. Everything here also works as a plain python command;
# see the README if you would rather not use make.

.PHONY: help install test collect history demo reset serve

help:
	@echo "install  - install python dependencies"
	@echo "test     - run the offline test suite"
	@echo "collect  - fetch one live snapshot now"
	@echo "history  - rebuild the historical layer from Open Data (slow, ~40 downloads)"
	@echo "demo     - fill the dashboard with SYNTHETIC data for local preview"
	@echo "reset    - delete all collected and demo data"
	@echo "serve    - preview the dashboard at http://localhost:8000"

install:
	pip install -r requirements.txt

test:
	python tests/test_aggregate.py
	python tests/test_history.py
	python src/collect_realtime.py --fixture

collect:
	python src/collect_realtime.py

history:
	python src/build_history.py

demo:
	python tests/make_fixtures.py --seed-demo 7
	python tests/test_history.py --write-demo

reset:
	rm -f data/realtime/*.csv docs/data/*.json data/processed/*
	@echo "cleared collected data; the next run starts a fresh series"

serve:
	@echo "dashboard: http://localhost:8000"
	python -m http.server 8000 --directory docs
