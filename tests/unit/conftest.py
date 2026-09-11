"""Unit-test fixtures and hypothesis configuration.

Hypothesis is derandomized deliberately: the testing guide forbids tests that
depend on randomness, and a randomized property suite would violate that every
run. The profile pins the seed to the test's name, so a failing example
reproduces identically forever -- and a deliberate widening of the example
budget is a visible diff to this file, not silent variation.
"""

from hypothesis import settings

settings.register_profile("pearss", derandomize=True, max_examples=100)
settings.load_profile("pearss")
