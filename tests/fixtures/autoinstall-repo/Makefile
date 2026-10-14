PYTHON ?= python3

.PHONY: install lint test test-integration

install:
	mkdir -p .deps && touch .deps/installed

lint:
	$(PYTHON) -m py_compile app/calc.py && echo "lint: ok"

test:
	@test -f .deps/installed || (echo "run make install first" && exit 2)
	$(PYTHON) -m pytest -q -p no:cacheprovider tests/calc_checks.py

test-integration:
	@test -n "$$API_TOKEN" || (echo "API_TOKEN is not set" && exit 2)
