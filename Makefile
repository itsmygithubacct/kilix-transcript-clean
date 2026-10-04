PYTHON ?= python3

.PHONY: test check

test:
	PYTHONPATH=src:tests $(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m compileall -q src tests
