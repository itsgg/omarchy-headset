# Everything CI runs, in one command, before you push.
.PHONY: check test test-python test-js manifest validate lint install uninstall reload clean

check: test lint manifest validate
	@echo "all checks passed"

test: test-python test-js

# No PATH: the helper shells out to bluetoothctl, and a test that quietly takes
# a different branch when it is missing is a test that only passes here.
test-python:
	@env -u PATH $$(command -v python3) -m unittest discover -s tests -q

test-js:
	@node --test "tests/js/*.test.mjs"

manifest:
	@python3 tools/check_manifest.py

# The shell's own loader checks, so a manifest the running Omarchy would reject
# cannot be committed.
validate:
	@omarchy plugin validate . && echo "omarchy validate: ok"

# In `check` because it was not, and a widget that could not load at all passed
# a green `make check`: every Python and JavaScript test passes on QML that the
# shell refuses.
lint:
	@tools/qmllint.sh

# A real directory, not a symlink: the shell's file watcher does not follow one,
# so a symlinked plugin never hot-reloads and you debug yesterday's code.
install:
	@mkdir -p $(HOME)/.config/omarchy/plugins/io.github.itsgg.headset
	@rsync -a --delete --exclude '.git' --exclude '__pycache__' ./ $(HOME)/.config/omarchy/plugins/io.github.itsgg.headset/
	@echo "installed; enable with: omarchy plugin enable io.github.itsgg.headset"

# Quickshell caches compiled QML, and a changed file is not always enough to
# invalidate it. Clearing the cache and restarting is the reliable loop.
# The restart's own readiness probe times out on a bar with several plugins on
# it and reports failure for a shell that is coming up fine, so its exit code is
# not treated as the verdict.
reload: install
	@rm -rf $(HOME)/.cache/quickshell/qmlcache
	@omarchy restart shell || true
	@echo "restarted; give the bar a few seconds"

uninstall:
	@rm -rf $(HOME)/.config/omarchy/plugins/io.github.itsgg.headset
	@echo "removed; state lives only in $${XDG_RUNTIME_DIR}/omarchy-headset and ~/.cache/omarchy-headset"

clean:
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
