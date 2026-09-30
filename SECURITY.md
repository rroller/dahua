# Security policy

## Supported versions

The current release. This is a Home Assistant custom integration distributed through
HACS, so fixes ship in a new release rather than being backported.

## Reporting something

**Please do not open a public issue for a security problem.** That includes an issue that
merely describes the shape of it, if the description would be enough for someone to find
it.

GitHub's private vulnerability reporting is the intended route. When it is enabled for
this repository it appears as **Report a vulnerability** on the
[Security tab](https://github.com/rroller/dahua/security).

If that button is not there yet, open an issue that says only that you have a security
report and would like somewhere private to send it, **withholding the detail**. A
maintainer can then enable private reporting, which takes one setting, or give you
another way to send it. An issue asking for a channel is not a disclosure; an issue
containing the finding is.

## What is in scope

This integration holds credentials for cameras and recorders, and those devices usually
have access to a home network and a live view of the inside of a house. The things worth
reporting are the ones that leak that:

- a username, password, or session token reaching the Home Assistant log, at any level
- anything surviving the redaction in a diagnostics download, which is meant to remove
  credentials, serial numbers and unique ids before the file is written
- a credential reaching a URL, since those are logged by other tools even when this
  integration does not log them
- anything that lets one Home Assistant user reach a device or a channel they were not
  configured for
- a way to make the integration send credentials somewhere other than the configured
  device

Reports of the "it logged something it should not have" kind are welcome even when they
look minor. Redaction that is inconsistent between two places is exactly the sort of
thing that is invisible from the inside: the integration removes the serial number from
diagnostics, so anywhere else it appears is a gap rather than a decision.

## What is not a vulnerability here

- **A Dahua device's own weak authentication.** Digest auth over HTTP on the local
  network is how these devices work, and the integration cannot improve on what the
  firmware offers. Report that to the vendor.
- **A device locking out after failed logins.** That is the device protecting itself. It
  is worth an ordinary issue if the integration triggers it unnecessarily.
- **Credentials stored in Home Assistant's own configuration.** Anything with read access
  to `.storage` already has everything, and that is Home Assistant's threat model rather
  than this integration's.
- **A user posting their own diagnostics or logs publicly.** Worth telling them, but not
  a flaw in the integration unless something that should have been redacted was not, in
  which case it very much is.

## What to expect

This is a volunteer project, so there is no response time commitment. What is reasonable
to expect is that a report is acknowledged, that a fix credits you unless you would
rather it did not, and that nothing is published about it before there is a release to
update to.
