import sys
sys.path.insert(0, ".")

try:
    from omweb.provider_packages.package_manager import provider_package_manager as _ppm
    print("[1] package_manager import: OK")
except Exception as e:
    print(f"[1] package_manager import FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

try:
    from omweb.routers.store.providers import _try_read_manifest
    print("[2] _try_read_manifest import: OK")
except Exception as e:
    print(f"[2] _try_read_manifest import FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

for pkg in ["cloudflare-ai", "gigachat"]:
    print(f"\n--- Testing {pkg} ---")
    try:
        entry = _ppm.get_package(pkg)
        print(f"[3] get_package({pkg!r}) = {entry!r}")
        if entry is None:
            print("   --> PACKAGE NOT REGISTERED")
            continue
        print(f"[4] active_version = {entry.active_version!r}")
        manifest = _try_read_manifest(pkg, entry.active_version)
        print(f"[5] manifest type = {type(manifest).__name__}")
        if isinstance(manifest, dict):
            print(f"[6] manifest.get('icon') = {manifest.get('icon')!r}")
        else:
            print(f"[6] manifest raw = {manifest!r}")
    except Exception as e:
        print(f"   EXCEPTION: {type(e).__name__}: {e}")
