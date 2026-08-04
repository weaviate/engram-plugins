"""One-shot maintenance tools invoked by plugin slash commands (not hooks). Each module is an
entrypoint for `with-venv.sh -m core.tools.<name>`; unlike core.hooks they read argv, not stdin."""
