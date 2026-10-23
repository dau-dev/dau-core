#########
# BUILD #
#########
.PHONY: develop build install

develop:  ## install dependencies and build library
	uv pip install -e .[develop]

requirements:  ## install prerequisite python build requirements
	uv pip install -r pyproject.toml --extra develop

build:  ## build the python library
	python -m build -n

install:  ## install library
	uv pip install .

#########
# LINTS #
#########
.PHONY: lint-py lint-docs fix-py fix-docs lint lints fix format

lint-py:  ## lint python with ruff
	python -m ruff check dau_core
	python -m ruff format --check dau_core

lint-docs:  ## lint docs with mdformat and codespell
	python -m mdformat --check README.md $(wildcard docs/src) 
	python -m codespell_lib README.md $(wildcard docs/src) 

fix-py:  ## autoformat python code with ruff
	python -m ruff check --fix dau_core
	python -m ruff format dau_core

fix-docs:  ## autoformat docs with mdformat and codespell
	python -m mdformat README.md $(wildcard docs/src) 
	python -m codespell_lib --write README.md $(wildcard docs/src) 

lint: lint-py lint-docs  ## run all linters
lints: lint
fix: fix-py fix-docs  ## run all autoformatters
format: fix

################
# Other Checks #
################
.PHONY: check-dist check-types checks check

check-dist:  ## check python sdist and wheel with check-dist
	check-dist -v

check-types:  ## check python types with ty
	ty check --python $$(which python)

checks: check-dist check-types

# Alias
check: checks

#########
# TESTS #
#########
.PHONY: test test-hdl coverage tests

# THE TWO HDL TIERS, split by what they cost and what they catch.
#
# Elaboration is `verilator --lint-only` over every declared parameterization:
# 46 tests, ~11 seconds, no compile, no cocotb, no C++ toolchain. It is what
# an RTL retype breaks, so it belongs in the gate every change passes.
#
# The compile tier builds and simulates: 135 cocotb benches, 8 raw
# SystemVerilog testbenches and 2 mutation checks, ~34 minutes. It cannot be
# on that gate at that price.
#
# One list each so `coverage` and `test-hdl` cannot disagree about which file
# is in which tier. A NEW compile-tier bench file must be added to
# HDL_COMPILE_TESTS or `coverage` will start running it.
HDL_ELABORATION_TESTS := dau_core/tests/test_hdl_elaboration.py
HDL_COMPILE_TESTS := dau_core/tests/test_hdl_cocotb.py dau_core/tests/test_hdl_verilator.py dau_core/tests/test_benches_can_fail.py

test:  ## run python tests
	python -m pytest -v dau_core/tests

# the benches compile and simulate RTL, so they skip wherever verilator is
# absent: run them on their own so a skipped bench tier is visible as an empty
# run rather than as noise inside the full suite
test-hdl:  ## run the verilator benches (elaboration, cocotb, raw testbenches)
	python -m pytest -v $(HDL_ELABORATION_TESTS) $(HDL_COMPILE_TESTS)

# deselects the compile tier and NOT the elaboration tier: with verilator
# present this is ~46 more real tests for ~11 more seconds, instead of the
# ~34 minutes the whole testpath would cost
coverage:  ## run tests and collect test coverage, without the RTL compile tier
	python -m pytest -v dau_core/tests $(addprefix --deselect ,$(HDL_COMPILE_TESTS)) --cov=dau_core --cov-report term-missing --cov-report xml

# Alias
tests: test

###########
# VERSION #
###########
.PHONY: show-version patch minor major

show-version:  ## show current library version
	@bump-my-version show current_version

patch:  ## bump a patch version
	@bump-my-version bump patch

minor:  ## bump a minor version
	@bump-my-version bump minor

major:  ## bump a major version
	@bump-my-version bump major

########
# DIST #
########
.PHONY: dist dist-build dist-sdist dist-local-wheel publish

dist-build:  # build python dists
	python -m build -w -s

dist-check:  ## run python dist checker with twine
	python -m twine check dist/*

dist: clean dist-build dist-check  ## build all dists

publish: dist  ## publish python assets

#########
# CLEAN #
#########
.PHONY: deep-clean clean

deep-clean: ## clean everything from the repository
	git clean -fdx

clean: ## clean the repository
	rm -rf .coverage coverage cover htmlcov logs build dist *.egg-info

############################################################################################

.PHONY: help

# Thanks to Francoise at marmelab.com for this
.DEFAULT_GOAL := help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

print-%:
	@echo '$*=$($*)'
