"""The built-in deliverers (ext/deliverers.py): `command` and `image-ready`. They are plugins (`PLUGIN` in each module,
pluginhost.BUILTINS), and they also register here, on import, through `add_deliverer`, so a change set's profile can
name them before (or without) the plugin host loading them: the same objects either way."""

from __future__ import annotations

from hangar.deliverers.command import INSTANCE as COMMAND
from hangar.deliverers.image_ready import INSTANCE as IMAGE_READY
from hangar.ext.deliverers import DELIVERERS, add_deliverer

BUILTINS = (COMMAND, IMAGE_READY)

for _d in BUILTINS:
    if _d.kind not in DELIVERERS:
        add_deliverer(_d)
