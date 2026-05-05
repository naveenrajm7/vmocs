#!/usr/bin/env python3
"""
Generate the gres.conf File= line for GPU passthrough.

Run once on each compute node after binding GPUs to vfio-pci (bare VFIO)
or loading the GIM driver (SR-IOV VFs). Redirect the output into
/etc/slurm/gres.conf or include it in your configuration management.

Usage:
    python3 gres-conf-gen.py
    python3 gres-conf-gen.py --check   # print BDF→group mapping, no gres line

Supports:
  - Bare VFIO: physical GPU PF bound to vfio-pci (no physfn symlink)
  - GIM SR-IOV: virtual functions bound to vfio-pci (physfn symlink present)
"""

import argparse
import sys
from pathlib import Path


def vfio_gpu_devices():
    """
    Return sorted list of /dev/vfio/<N> paths for GPU-representing IOMMU groups.

    Filters to groups that contain at least one display-class (0x03xx) PCI
    device bound to vfio-pci. This excludes audio-only groups (0x04xx) and
    unrelated VFIO devices.

    For GIM VFs: the VF has a physfn symlink and is the only device in its
    group — always included.
    For bare VFIO PFs: the group may contain both GPU (0x03xx) and audio
    (0x04xx) functions; included if any device in the group is 0x03xx.
    """
    gpu_groups = set()

    for bdf_path in Path("/sys/bus/pci/devices").iterdir():
        drv = bdf_path / "driver"
        if not drv.exists():
            continue
        if Path(drv).resolve().name != "vfio-pci":
            continue

        iommu_link = bdf_path / "iommu_group"
        if not iommu_link.exists():
            continue
        grp = Path(iommu_link).resolve().name

        # VFs (GIM SR-IOV) always qualify — they're single-function GPU devices
        if (bdf_path / "physfn").exists():
            gpu_groups.add(grp)
            continue

        # For PFs, only include groups that have a display-class device
        cls_file = bdf_path / "class"
        if cls_file.exists() and cls_file.read_text().strip().startswith("0x03"):
            gpu_groups.add(grp)

    devs = []
    for grp in gpu_groups:
        dev = Path(f"/dev/vfio/{grp}")
        if dev.exists():
            devs.append(str(dev))

    return sorted(devs)


def bdf_mapping():
    """Return list of (bdf, grp, dev) tuples for all vfio-pci bound devices."""
    rows = []
    for bdf_path in sorted(Path("/sys/bus/pci/devices").iterdir()):
        drv = bdf_path / "driver"
        if not drv.exists():
            continue
        if Path(drv).resolve().name != "vfio-pci":
            continue
        iommu_link = bdf_path / "iommu_group"
        if not iommu_link.exists():
            continue
        grp = Path(iommu_link).resolve().name
        rows.append((bdf_path.name, grp, f"/dev/vfio/{grp}"))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="Print BDF→group mapping instead of gres.conf line")
    args = parser.parse_args()

    if args.check:
        rows = bdf_mapping()
        if not rows:
            print("No vfio-pci bound devices found.", file=sys.stderr)
            return
        print(f"{'BDF':<20} {'IOMMU group':<14} {'device file'}")
        print("-" * 50)
        for bdf, grp, dev in rows:
            print(f"{bdf:<20} {grp:<14} {dev}")
        return

    devs = vfio_gpu_devices()
    if not devs:
        print("# No vfio-pci GPU devices found on this node.", file=sys.stderr)
        sys.exit(1)

    print(f"Name=gpu File={','.join(devs)}")


if __name__ == "__main__":
    main()
