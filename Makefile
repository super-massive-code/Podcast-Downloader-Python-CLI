.PHONY: build clean

# Build a standalone single-file binary for the current OS (macOS or Linux).
# Output: dist/podcast
build:
	.venv/bin/pyinstaller podcast.spec --clean --noconfirm

clean:
	rm -rf build dist
