# Contributing

## General

This project welcomes new [issues](https://github.com/tekgnosis-net/blackvuesync-v2/issues) and [pull requests](https://github.com/tekgnosis-net/blackvuesync-v2/pulls).

## Responsible AI contributions

The use of generative AI is welcome, provided these conditions are met:

- **Human ownership:** You as a human are responsible for the contents of your contribution.
- **Human oversight and expertise:** Please review, validate and revise issues and pull requests with your own expertise so that they reflect your personal understanding and voice.

This AI contribution policy is loosely based on the one in the [Microsoft Open Source CoC](https://opensource.microsoft.com/codeofconduct/).

## Development Setup

```bash
# clone and setup
git clone https://github.com/tekgnosis-net/blackvuesync-v2.git
cd blackvuesync-v2

# create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# install pre-commit hooks (runs linters, formatters, security checks)
pre-commit install
pre-commit install --hook-type commit-msg
```

Pre-commit hooks will automatically run quality checks on `git commit` and in pull requests.

## Tests

```bash
# unit tests
pytest test --ignore=test/e2e

# browser tests (Playwright Chromium)
pytest test/e2e -m e2e

# integration tests against a mock dashcam
behave
```

GitHub Actions runs all of them, plus the pre-commit checks, on every pull
request.

## Documentation

The documentation site is built with Material for MkDocs from `docs/` and
`mkdocs.yml`:

```bash
pip install -e ".[docs]"
mkdocs serve            # preview at http://127.0.0.1:8000/
mkdocs build --strict   # what CI runs; fails on broken links
```

Update the relevant guide in `docs/guide/` together with any change in
behaviour, and add an entry to `CHANGELOG.md`. After a user-interface change,
regenerate the screenshots from synthetic demo data (requires ffmpeg):

```bash
python scripts/screenshots.py
```

## Releasing

Versions follow [semantic versioning](https://semver.org/). Between releases,
changes are listed under `## Unreleased` at the top of `CHANGELOG.md`. To
release, from an up-to-date `main`:

```bash
python scripts/release.py prepare 3.1.0   # bumps the version, dates the changelog, opens a PR
# review and merge the release PR, then:
git pull
python scripts/release.py tag 3.1.0       # pushes the v3.1.0 tag
```

The tag publishes the GitHub release and the Docker images `3.1.0`, `3.1` and
`3`. Regenerate the screenshots in the release PR if the footer version matters
for the docs.
