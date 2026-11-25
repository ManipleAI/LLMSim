# Contributing to LLMSim

Thanks for looking. LLMSim is small on purpose, and a good change usually keeps it that way.

## Before you start

- **Open an issue first** for anything bigger than a typo or a one-line fix, so we can agree on the shape of it before you spend the time. Issues labelled [`good first issue`](https://github.com/ManipleAI/LLMSim/labels/good%20first%20issue) are small, self-contained and a good way in.
- **Questions and ideas** go in [Discussions](https://github.com/ManipleAI/LLMSim/discussions), not in issues.
- **Vulnerabilities** are not reported in public. See [SECURITY.md](SECURITY.md).

## Run it and run the tests

You need Python 3.9 or newer. There is nothing to install.

```bash
git clone https://github.com/ManipleAI/LLMSim.git
cd LLMSim
python3 server.py                                    # http://localhost:8099/ui
for t in tests/test_*.py; do python3 "$t" || break; done
```

Each test file starts its own LLMSim on a free port with a throwaway data directory, so tests never touch your own settings and you can run one file on its own: `python3 tests/test_routing.py`.

Two files drive the console in a real browser (`test_access_page.py`, `test_console_look.py`). They need Node and [Playwright for Node](https://playwright.dev/), and skip without them:

```bash
NODE_PATH=/path/to/node_modules python3 tests/test_console_look.py
```

## What a good pull request looks like

- **It has a test that fails without the change.** Add a case to the closest `tests/test_*.py`, or a new file in the same style (plain Python, one `case_...` function per behaviour, no test framework).
- **It adds no dependencies.** The server and the test kit use only the Python standard library, and that is a feature. The console is one file, `ui.html`, with no build step.
- **It keeps the fake keys fake.** The published keys in the README are safe to commit. Never commit a real key, a real address or a real person's name or email into code, tests, docs or screenshots.
- **It updates the docs it touches.** The guide is `docs/guide.md`; `docs/guide.html` is built from it with `bin/build-docs`, and a test fails when the two disagree.
- **It does one thing.** A bug fix and a refactor are two pull requests.
- **It says what you ran.** Paste the test output (or say which tests need a browser you did not have) in the pull request.

## Commit and review

Write commit messages that say why, not only what. We review in the pull request, and we may ask for changes or say no; a "no" is about fit, not about you.

By contributing you agree that your work is released under the [MIT license](LICENSE), and you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
