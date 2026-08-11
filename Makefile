.PHONY: test

# Run the full unit suite: the hermetic mtui suite (bug regressions, loaders,
# dispatch, render, PTY boot) plus the mpp merged-copy tests. PATH-independent;
# no network or real clipboard I/O.
test:
	python3 -m unittest test_mtui -v
	python3 -m unittest discover -s tests -v
