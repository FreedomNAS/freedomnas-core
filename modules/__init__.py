"""
FreedomNAS pluggable modules.

Each module lives in its own subpackage (modules/<name>/), with a module.py
implementing FreedomNASModule (see orchestrator.py) and a manifest.json
declaring its entry point and supported OSes. ModuleLoader discovers and
registers modules with the Orchestrator at runtime — see module_loader.py.
A community module follows the same layout but usually ships in its own
repo/package, dropped into this directory at install time.
"""
