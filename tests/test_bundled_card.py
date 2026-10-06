"""Exercise bundled card registration in a JavaScript runtime."""

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("existing", [False, True])
def test_card_registration_initializes_or_preserves_frontend_registry(existing):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the bundled frontend registration check")
    bundle = Path(
        "custom_components/sunsynk/www/sunsynk-power-flow-card.js"
    ).read_text()
    # Execute the shipped registration expression, without needing the DOM
    # rendering code or a Home Assistant server. Preserve the real card metadata.
    end = bundle.index(";var vi", bundle.index('fi=t([Ct("sunsynk-power-flow-card")'))
    start = bundle.rindex(
        ",",
        0,
        bundle.index(
            "window.customCards", bundle.index('fi=t([Ct("sunsynk-power-flow-card")')
        ),
    )
    registration = bundle[start + 1 : end]
    script = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
const prior = [{type: 'another-card'}];
const context = {window: EXISTING ? {customCards: prior} : {}, Ln: () => 'description'};
vm.runInNewContext(REGISTRATION, context);
const cards = context.window.customCards;
assert.equal(cards.length, EXISTING ? 2 : 1);
assert.equal(cards.at(-1).type, 'sunsynk-power-flow-card');
assert.equal(cards.at(-1).configurable, true);
if (EXISTING) { assert.equal(cards, prior); assert.equal(cards[0].type, 'another-card'); }
"""
    import json

    script = script.replace("EXISTING", "true" if existing else "false").replace(
        "REGISTRATION", json.dumps(registration)
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
