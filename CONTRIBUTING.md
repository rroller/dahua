# Contributing

Thanks for wanting to help. This is a short guide to the things that are hard to guess
and easy to get caught by, rather than a general introduction to pull requests.

## Running the tests

The suite needs **Python 3.14 or newer**. `requirements_test.txt` pins
`pytest-homeassistant-custom-component`, whose `requires_python` is `>=3.14`, so pip
refuses to install it on anything older. CI uses 3.14.

That version moves as the pin does: it was `>=3.13` a few releases ago. If the install
fails, check the pin's own requirement before assuming your environment is wrong.

```bash
python3 -m pip install -r requirements_test.txt

pytest -qq --timeout=9 --durations=10 -n auto \
  --cov custom_components.dahua \
  -o console_output_style=count -p no:sugar tests
```

That is exactly what CI runs. Two details in it matter:

- **`--timeout=9`.** A test that waits on a real retry delay will be killed rather than
  failed, and a timeout reads like a hang rather than a bug. Patch the delay.
- **`-n auto`.** Tests run in parallel, so anything that writes to a module level global
  has to clear it, in a fixture, either side of the test. There are several such globals
  in `client.py` and `coordinator.py`.

## Checklists for the things that are wired in more than one place

These are the ones that fail CI with a message that does not explain itself.

### Adding a service

1. register it in `camera.py` with a `SERVICE_` constant
2. add it to `services.yaml` with a `name`, a `description`, and a `selector` on every
   field

`tests/dahua/test_services_yaml_stays_complete.py` checks both directions. A service
missing from `services.yaml` still works if you know its exact name, and is invisible in
**Developer tools -> Actions**, which for most users means it does not exist.

### Adding a platform

1. add it to `PLATFORMS` in `const.py`
2. classify it in `tests/dahua/test_parallel_updates.py` as read-only or acting, and set
   `PARALLEL_UPDATES` in the module
3. add it to the `PLATFORMS` tuple in **both**
   `tests/dahua/test_entity_icons_come_from_icons_json.py` and
   `tests/dahua/test_entity_names_come_from_translations.py`

Those last two keep their own list, so a platform absent from it is never scanned and its
translations silently belong to nothing.

### Adding an entity

- name it with `_attr_translation_key` and add the string to `translations/en.json`.
  Never set `_attr_name`; there is a test for both halves of that
- pass `config_subentry_id=coordinator.subentry_id` to `async_add_entities`, or the
  entity is filed under the host instead of its channel
- extend `DahuaBaseEntity`, which is where `device_info` comes from
- grep `tests/` for the class name of the entity next to yours. Anything asserting a
  list, a count, or a docstring reason why something is *not* there will need updating

### Adding an async test

A test is a bare `async def test_...` that awaits directly. `asyncio_mode` is `auto`, so
there is no marker, and `asyncio.run` inside a test dies during collection rather than at
an assertion.

## What tests are expected to assert

The house style is stricter than "it returns the right type", because most of this code
talks to a device that answers `OK` to requests it did not understand.

- **Assert the exact key or URL**, not just the shape. A `getConfig` for a table that
  does not exist returns empty rather than an error, and a `setConfig` naming an unknown
  key answers `OK`. A wrong key therefore produces a plausible answer for ever.
- **Include a neighbouring-but-wrong case.** A key off by one index or one letter is the
  realistic defect, and it passes any test that only checks the happy path.
- **Break the code and watch the test fail.** A green suite proves very little here.
  Several tests in this repo have passed while never reaching the code they named, and
  the mutation is what found them. The commit messages say which mutations were tried.
- **Watch for an expected value that equals the fixture's starting value.** It reads like
  a good test and proves nothing.

## Documentation lives in three places

- `README.md` for anything a user does
- `services.yaml` for services, which is what the UI renders
- `translations/en.json` for entity and form strings. English is the per-key fallback for
  every other language, so a missing English string is bare everywhere

## Hardware claims

If you are adding support for a device, say what you measured and on which model. A lot
of this integration's history is capabilities gated on a model name that then failed on a
device which had the hardware, so evidence from a real device is worth much more than a
plausible-looking condition. If you cannot test something, saying so is genuinely more
useful than a guess that reads as confident.
