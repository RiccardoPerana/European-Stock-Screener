"""
Credential storage
==================

WHY THIS EXISTS
---------------
Environment variables are the right answer for a script and for CI, and the
wrong answer for a packaged application. You cannot reasonably ask someone
who double-clicks an .exe to open System Properties, add a variable, and
restart their shell -- and if they manage it, the key is then readable by
every process they run.

So the key goes in the operating system's own credential store: Windows
Credential Manager, macOS Keychain, or Secret Service on Linux. The GUI
gets a text box, the value is encrypted at rest by the OS, and nothing
lands in a file this project has to protect.

RESOLUTION ORDER
----------------
Highest priority first. Each layer exists for a different caller:

    1. explicit argument      --api-key, for a one-off test
    2. environment variable   CI, containers, headless servers
    3. OS credential store    the GUI and the packaged application
    4. interactive prompt     a human at a terminal with nothing set up

The environment variable deliberately outranks the credential store, so a
CI job or a container can override whatever a developer happens to have
saved locally without having to clear it first.

WHAT IS NOT DONE HERE
---------------------
No fallback to a plaintext file. If the OS store is unavailable -- a
headless Linux box with no Secret Service, typically -- this reports that
honestly and falls back to the environment variable. Silently writing the
key to a JSON file next to the source would be a downgrade disguised as
convenience, and the kind of thing that ends up committed.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys

log = logging.getLogger(__name__)

SERVICE = "esef-stock-screen"

# Every credential the tool can hold. Adding a provider means adding a row
# here and nothing else: the GUI builds its settings form from this.
PROVIDERS = {
    "openfigi": {
        "label": "OpenFIGI",
        "env": "OPENFIGI_API_KEY",
        "signup": "https://www.openfigi.com/api",
        "note": "Ticker resolution. Optional: raises the rate limit only.",
    },
    "gemini": {
        "label": "Google AI Studio (Gemini)",
        "env": "GEMINI_API_KEY",
        "signup": "https://aistudio.google.com/apikey",
        "note": "Auto-classifies missing industries. Optional: without "
                "it, use the manual copy/paste prompt instead.",
    },
}


class KeyringUnavailable(RuntimeError):
    """The OS credential store could not be reached."""


def _keyring():
    """Import lazily so the tool still runs where keyring is not installed."""
    try:
        import keyring
        from keyring.errors import NoKeyringError
    except ImportError as e:
        raise KeyringUnavailable("keyring is not installed") from e
    try:
        backend = keyring.get_keyring()
        # The null backend reports success and stores nothing, which is far
        # worse than failing: the user would save a key, see no error, and
        # find it gone next launch.
        if "fail" in type(backend).__module__.lower():
            raise KeyringUnavailable("no usable credential store on this system")
    except NoKeyringError as e:
        raise KeyringUnavailable(str(e)) from e
    return keyring


def keyring_available() -> bool:
    """True if a key can actually be stored. The GUI greys out its box if not."""
    try:
        _keyring()
        return True
    except KeyringUnavailable:
        return False


def store(provider: str, key: str) -> None:
    """
    Save a key to the OS credential store. This is what the GUI calls.

    Raises KeyringUnavailable if there is nowhere safe to put it, so the
    caller can say so rather than pretending the key was saved.
    """
    if provider not in PROVIDERS:
        raise KeyError(f"unknown provider {provider!r}")
    if not (key or "").strip():
        raise ValueError("refusing to store an empty key")
    _keyring().set_password(SERVICE, provider, key.strip())
    log.info("stored %s credential in the OS credential store", provider)


def forget(provider: str) -> bool:
    """Delete a stored key. True if one was there."""
    try:
        kr = _keyring()
        if kr.get_password(SERVICE, provider) is None:
            return False
        kr.delete_password(SERVICE, provider)
        log.info("removed %s credential", provider)
        return True
    except KeyringUnavailable:
        return False


def from_store(provider: str) -> str | None:
    try:
        return _keyring().get_password(SERVICE, provider)
    except KeyringUnavailable:
        return None


def resolve(provider: str, explicit: str | None = None,
            allow_prompt: bool = True) -> str | None:
    """
    Find the key, trying each layer in turn. None if nothing has one.

    `allow_prompt` must be False for the GUI and for any unattended run: a
    getpass() with nobody watching is a hang, not a prompt.
    """
    meta = PROVIDERS[provider]

    if explicit:
        # Works, but lands in shell history and in process listings, so it
        # is for a one-off test rather than a habit.
        log.debug("%s key taken from an explicit argument", provider)
        return explicit.strip()

    from_env = os.environ.get(meta["env"])
    if from_env:
        log.debug("%s key taken from %s", provider, meta["env"])
        return from_env.strip()

    saved = from_store(provider)
    if saved:
        log.debug("%s key taken from the OS credential store", provider)
        return saved

    if allow_prompt and sys.stdin.isatty():
        print(f"\nNo {meta['label']} key found.")
        print(f"  {meta['note']}")
        print(f"  Get one: {meta['signup']}")
        try:
            entered = getpass.getpass(f"{meta['label']} key (blank to skip): ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        entered = entered.strip()
        if not entered:
            return None
        if keyring_available():
            answer = input("Save it to your OS credential store? [Y/n]: ")
            if answer.strip().lower() in ("", "y", "yes"):
                try:
                    store(provider, entered)
                    print("Saved. It will be found automatically next time.")
                except (KeyringUnavailable, ValueError) as e:
                    print(f"Could not save: {e}")
        return entered

    return None


def is_set(provider: str) -> bool:
    """
    Whether a key can actually be resolved.

    Do NOT infer this by comparing describe() to a string. describe()
    returns a longer sentence when there is no credential store, so the
    comparison silently reported every key as present on a machine without
    one -- exactly backwards.
    """
    return bool(resolve(provider, allow_prompt=False))


def describe(provider: str) -> str:
    """Where the key is coming from, for a status screen. Never the key itself."""
    meta = PROVIDERS[provider]
    if os.environ.get(meta["env"]):
        return f"set via {meta['env']}"
    if from_store(provider):
        return "saved in the OS credential store"
    if not keyring_available():
        return f"not set (no credential store here; use {meta['env']})"
    return "not set"


def main() -> int:
    """
    Manage stored keys from the command line.

        python credentials.py --status
        python credentials.py --set openfigi
        python credentials.py --forget openfigi
    """
    import argparse

    p = argparse.ArgumentParser(description="Manage stored API keys.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--set", metavar="PROVIDER", choices=sorted(PROVIDERS))
    g.add_argument("--forget", metavar="PROVIDER", choices=sorted(PROVIDERS))
    args = p.parse_args()

    if args.status:
        print(f"Credential store: "
              f"{'available' if keyring_available() else 'NOT available here'}\n")
        for name, meta in PROVIDERS.items():
            print(f"  {meta['label']:<14}{describe(name)}")
            print(f"  {'':<14}{meta['note']}")
        return 0

    if args.set:
        meta = PROVIDERS[args.set]
        if not keyring_available():
            print("No credential store on this system.")
            print(f"Set {meta['env']} in the environment instead.")
            return 1
        print(f"{meta['label']}  --  {meta['note']}")
        print(f"Get a key: {meta['signup']}")
        key = getpass.getpass("Paste the key (hidden): ").strip()
        if not key:
            print("Nothing entered.")
            return 1
        store(args.set, key)
        print(f"Saved. {describe(args.set)}.")
        return 0

    print("Removed." if forget(args.forget) else "Nothing was stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
