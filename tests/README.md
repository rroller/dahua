# Tests

How to run them, what they are expected to assert, and the checklists for the
things wired in more than one place are all in
[CONTRIBUTING.md](../CONTRIBUTING.md).

In short: Python 3.14 (whatever `requirements_test.txt`'s pin requires; read the
pin rather than this file), then

```bash
python3 -m pip install -r requirements_test.txt
pytest -qq --timeout=9 -n auto --cov custom_components.dahua tests
```

which is what CI runs. `-n auto` means tests run in parallel, so anything
writing to a module level global has to clear it in a fixture; `--timeout=9`
means a test waiting on a real retry delay is killed rather than failed.
