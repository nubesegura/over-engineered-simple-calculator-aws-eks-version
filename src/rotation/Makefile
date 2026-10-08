# Build rule used by `sam build` (BuildMethod: makefile) for RotationFunction. Self-contained: package_bundle.py
# (this folder, not copied into the package) installs the pinned arm64 wheels, copies app.py and adds the
# validated RDS CA bundle, so the bundle reaches /var/task.
PYTHON ?= python3

build-RotationFunction:
	$(PYTHON) package_bundle.py --output-dir "$(ARTIFACTS_DIR)"
