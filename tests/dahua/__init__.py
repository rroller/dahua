"""Helpers shared by the Dahua tests."""


def adds_entities(into):
    """Stand in for Home Assistant's AddConfigEntryEntitiesCallback.

    What a platform's `async_setup_entry` is handed to publish its entities. The real
    one takes `update_before_add` and `config_subentry_id` as well as the entities,
    and the platforms pass the subentry on every call so that each channel's entities
    are filed under its own subentry.

    `list.extend` was standing in for it, which modelled a callback that does not
    exist: it accepts the entities and nothing else. That is the shape of gap that
    let the missing `config_subentry_id` go unnoticed in the first place, so this
    accepts what the real one accepts and records what was added.
    """

    def add(new_entities, update_before_add=False, *, config_subentry_id=None):
        into.extend(new_entities)

    return add
