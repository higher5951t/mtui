.PHONY: test

# Run the full unit suite (mpp parts + clipboard, mtui clipboard,
# papertrade timeout handling). PATH-independent; no network or clipboard I/O.
test:
	python3 -m unittest discover -s tests -v
