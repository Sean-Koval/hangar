"""The built-in deliverers (ext/deliverers.py): `command` and `image-ready`. They register here, on import, through
the same `add_deliverer` a plugin uses, so a change set's profile can name them with no plugin enabled."""

from __future__ import annotations

from hangar.deliverers.command import Command
from hangar.deliverers.image_ready import ImageReady
from hangar.ext.deliverers import DELIVERERS, add_deliverer

BUILTINS = (Command(), ImageReady())

for _d in BUILTINS:
    if _d.kind not in DELIVERERS:
        add_deliverer(_d)
