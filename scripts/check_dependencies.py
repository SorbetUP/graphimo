"""Check declared dependencies for selected Graphimo distributions only."""

from __future__ import annotations

import argparse
from importlib import metadata

from packaging.requirements import Requirement


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distributions", nargs="+", help="distribution names to verify")
    args = parser.parse_args()
    problems: list[str] = []
    for name in args.distributions:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name}: distribution is not installed")
            continue
        for raw_requirement in distribution.requires or ():
            requirement = Requirement(raw_requirement)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            try:
                installed = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                problems.append(f"{name}: missing {requirement}")
                continue
            if requirement.specifier and not requirement.specifier.contains(
                installed, prereleases=True
            ):
                problems.append(f"{name}: requires {requirement}, found {installed}")
    if problems:
        raise SystemExit("Graphimo dependency consistency failed:\n- " + "\n- ".join(problems))
    print(f"Graphimo dependency metadata is consistent for: {', '.join(args.distributions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
