"""Vendor adapters. One module per assistant, each a subclass of core.assistant.Assistant and
runnable as `python -m assistants.<name> <hook>`.

Deliberately empty of imports: a package that imports its own modules makes `python -m` load them
twice and warn on every hook invocation."""
