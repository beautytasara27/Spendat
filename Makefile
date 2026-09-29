.PHONY: install test lint fmt demo merchant

install:
	pip install -e ".[dev]"

test:            ## full suite, sandbox only, no credentials
	SPENDDAT_MODE=sandbox python3 -m pytest

lint:
	ruff check src tests tools && ruff format --check src tests tools

fmt:
	ruff format src tests tools && ruff check --fix src tests tools

demo:            ## both PRD flows end-to-end over HTTP, offline
	SPENDDAT_MODE=sandbox python3 tools/sandbox_demo.py

merchant:        ## local reference ACP merchant on :8787
	spenddat sandbox-merchant
