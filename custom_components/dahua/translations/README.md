# About these translations

**The error messages in the non-English files are machine-assisted, not written by
native speakers. Corrections from a native speaker would be very welcome** — a pull
request changing any string here needs no discussion, and you do not need to know
anything about the integration to improve one.

## Why they were added rather than left out

Each of the eight non-English files used to define exactly **one** error string,
`config.error.auth`, and in every language it was an older sentence that named the
*address*:

> Gebruikersnaam, Wachtwoord, of Adres is verkeerd.

Home Assistant falls back to English per key, so the result was the worst of both:
eleven accurate English messages, and the one message somebody could read in their own
language was the misleading one. It sent people to check their IP address when the
device had returned a 401 and only the credentials were in question. That is the
failure the English wording was rewritten to end, and #34 is somebody hitting it:
*"Says 'Username, Password, or Address is wrong'. Which it isn't."*

So the choice was not between good translations and machine ones. It was between
machine translations and a string that was actively wrong.

## What was translated, and what was not

**Translated:** the twelve `config.error` strings, and the `channel_not_added` abort
reason.

**Not translated:** the form labels, the step titles and descriptions, the repair
notices, and most of the options screen. Those fall back to English, which is
accurate, so they are ordinary translation debt rather than a correctness problem.

## A deliberate choice about device menus

Names of menus **on the camera or recorder** are left in English:

> ... System, then Security or Safety, then System Service, then CGI ...

A Dahua device's own interface language is unpredictable, and somebody hunting for a
menu is better served by the label they will actually see on the device than by a
translation of it. If your device shows those menus in your language, translating them
in the message would help — that is exactly the kind of local knowledge this file is
asking for.

## What the tests enforce

`tests/dahua/test_translations_coverage.py` checks that every language defines the
same error keys as English, that none defines one English does not have, that none is
blank, and that no language's `auth` message names the address in that language. That
last rule existed for English only, and the eight other files had all broken it.
