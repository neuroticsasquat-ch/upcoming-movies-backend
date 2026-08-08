"""The neutral request DTO's own guarantees, independent of any adapter (spec §5.1)."""

import dataclasses

import pytest

from upmovies.llm import Prompt


def test_prompt_defaults_leave_the_optional_parts_unset():
    p = Prompt(stable_prefix="INSTRUCTIONS", user="{}")
    assert p.prefill is None
    assert p.max_tokens == 4096


def test_prompt_is_frozen():
    """`stable_prefix` promises byte-stability across calls; a mutable Prompt could not."""
    p = Prompt(stable_prefix="INSTRUCTIONS", user="{}")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.stable_prefix = "other"  # type: ignore[misc]


def test_the_varying_payload_does_not_disturb_the_stable_prefix():
    """Byte-stability across calls is the whole contract: two calls of the same stage differ
    only in `user`, which is what lets an automatic-prefix-caching provider match."""
    a = Prompt(stable_prefix="INSTRUCTIONS", user='{"day": 1}')
    b = Prompt(stable_prefix="INSTRUCTIONS", user='{"day": 2}')
    assert a.stable_prefix == b.stable_prefix
    assert a != b
