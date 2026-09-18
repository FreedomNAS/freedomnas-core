"""
Threads Python creates can call into the native core.

The WebUI does exactly that: asyncio.to_thread and plain threading.Thread
run engine, config and key management calls off the event loop. A build
without the TLS flags in nim/freedomnas.nimble (--tlsEmulation:off,
-ftls-model=global-dynamic) segfaulted on the first such call once the
process had done enough allocation — one scrypt-sized keyslot wrap was
enough on the machine where it was found.

The work runs in a child interpreter, so a crash fails this test instead of
taking the whole suite down with it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

REPO = Path(__file__).parent.parent

WORKLOAD = r'''
import asyncio, json, os, sys, threading
sys.path = [p for p in sys.path if "freedomnas/core" not in p]
sys.path.insert(0, REPO)
import config_native, engine_native, keymgmt_native

for _ in range(3):
    slot = keymgmt_native.wrap_key(os.urandom(32), b"warm the allocator")
    assert keymgmt_native.unwrap_key(slot, b"warm the allocator")

engine = engine_native.FreedomNASEngine.for_target("debian", profile_dir=PROFILES)
errors = []

def worker(n):
    try:
        for i in range(200):
            engine.get_command("services", "start", service_name=f"svc{n}-{i}")
            config_native._unwrap(config_native._native.config_dump(json.dumps({"k": i})))
        secret = f"pw{n}".encode()
        assert keymgmt_native.unwrap_key(keymgmt_native.wrap_key(os.urandom(32), secret), secret)
    except Exception as exc:
        errors.append(repr(exc))

threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
for t in threads: t.start()
for t in threads: t.join()

async def main():
    await asyncio.gather(*(asyncio.to_thread(worker, 100 + n) for n in range(8)))
asyncio.run(main())

print("errors:", errors) if errors else print("ok")
'''


def test_python_threads_can_call_the_native_core():
    code = (
        f"REPO = {str(REPO)!r}\n"
        f"PROFILES = {str(REPO.parent / 'freedomnas-profiles')!r}\n"
        + WORKLOAD
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=600,
        env={k: v for k, v in os.environ.items() if not k.startswith("FREEDOMNAS_")},
    )
    assert result.returncode == 0, (
        f"exit {result.returncode} (negative = killed by that signal)\n{result.stderr[-2000:]}"
    )
    assert result.stdout.strip().endswith("ok"), result.stdout
