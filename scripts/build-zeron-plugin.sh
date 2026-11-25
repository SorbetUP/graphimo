#!/usr/bin/env bash
set -euo pipefail

dist_dir="${2:-${GRAPHIMO_DIST_DIR:-${HOME}/Desktop/Dev/graphimo/dist}}"
zeron_bin="${ZERON_BIN:-/Applications/Zeron.app/Contents/MacOS/zeron}"
plugins_root="${ZERON_PLUGINS_DIR:-${HOME}/.zeron/plugins}"
source_path="${1:-}"

if [[ ! -x "$zeron_bin" ]]; then
	printf 'Zeron CLI not found or not executable: %s\n' "$zeron_bin" >&2
	exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
	printf 'python3 is required to validate and package the plugin.\n' >&2
	exit 1
fi
mkdir -p "$dist_dir" "$plugins_root"

if [[ -z "$source_path" ]]; then
	if [[ -d "$plugins_root/graphimo" ]]; then
		source_path="$plugins_root/graphimo"
	else
		source_path="$(python3 - "$dist_dir" <<'PY'
import json
import re
import sys
from pathlib import Path

dist = Path(sys.argv[1])
packages = []
for path in dist.glob("*.zeron-plugin"):
    try:
        manifest = json.loads(path.read_text()).get("manifest", {})
        version = manifest.get("version", "")
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
        if manifest.get("id") == "graphimo" and match:
            packages.append((tuple(map(int, match.groups())), path))
    except (OSError, json.JSONDecodeError):
        continue
if not packages:
    raise SystemExit(f"No Graphimo package found in {dist}")
print(max(packages, key=lambda item: item[0])[1])
PY
)"
	fi
fi

source_path="$(cd "$(dirname "$source_path")" && pwd)/$(basename "$source_path")"
if [[ -d "$source_path" ]]; then
	manifest_path="$source_path/zeron-plugin.json"
	if [[ ! -f "$manifest_path" ]]; then
		printf 'Missing manifest: %s\n' "$manifest_path" >&2
		exit 1
	fi
	metadata="$(python3 - "$manifest_path" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
manifest = json.loads(manifest_path.read_text())
plugin_id = manifest.get("id", "")
version = manifest.get("version", "")
entry = manifest.get("entry", "")
if not re.fullmatch(r"[a-z0-9-]+", plugin_id):
    raise SystemExit("Manifest id must use lowercase letters, digits, or hyphens")
if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
    raise SystemExit("Manifest version must be semantic, such as 0.8.3")
entry_path = Path(entry)
if not entry or entry_path.is_absolute() or ".." in entry_path.parts:
    raise SystemExit("Manifest entry must be a safe relative path")
if not (manifest_path.parent / entry_path).is_file():
    raise SystemExit(f"Manifest entry does not exist: {entry}")
print(f"{plugin_id}\t{version}")
PY
)"
	IFS=$'\t' read -r plugin_id version <<< "$metadata"
	plugin_parent="$(cd "$(dirname "$source_path")" && pwd)"
	if [[ "$plugin_parent" != "$(cd "$plugins_root" && pwd)" ]]; then
		printf 'Source folder must be directly inside %s so Zeron can sign it.\n' "$plugins_root" >&2
		exit 1
	fi
	plugin_dir="$source_path"
else
	if [[ ! -f "$source_path" ]]; then
		printf 'Source folder or .zeron-plugin package not found: %s\n' "$source_path" >&2
		exit 1
	fi
	metadata="$(python3 - "$source_path" <<'PY'
import base64
import json
import re
import sys
from pathlib import Path, PurePosixPath

package_path = Path(sys.argv[1])
package = json.loads(package_path.read_text())
if package.get("format") != "zeron-plugin:v1":
    raise SystemExit("Unsupported source package format")
manifest = package.get("manifest", {})
plugin_id = manifest.get("id", "")
version = manifest.get("version", "")
entry = manifest.get("entry", "")
if not re.fullmatch(r"[a-z0-9-]+", plugin_id):
    raise SystemExit("Manifest id must use lowercase letters, digits, or hyphens")
if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
    raise SystemExit("Manifest version must be semantic, such as 0.8.3")
files = package.get("files", {})
if entry not in files:
    raise SystemExit(f"Package is missing its entry file: {entry}")
for name, encoded in files.items():
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"Unsafe package path: {name}")
    base64.b64decode(encoded, validate=True)
print(f"{plugin_id}\t{version}")
PY
)"
	IFS=$'\t' read -r plugin_id version <<< "$metadata"
	plugin_dir="$plugins_root/$plugin_id"
	if [[ -e "$plugin_dir" ]]; then
		printf 'Zeron already has a source folder for %s. Review/remove the current plugin in Zeron, then rerun.\n' "$plugin_id" >&2
		exit 1
	fi
	python3 - "$source_path" "$plugin_dir" <<'PY'
import base64
import json
import sys
from pathlib import Path

package = json.loads(Path(sys.argv[1]).read_text())
target = Path(sys.argv[2])
target.mkdir(parents=True)
for name, encoded in package["files"].items():
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(encoded, validate=True))
(target / "zeron-plugin.json").write_text(json.dumps(package["manifest"], indent=2) + "\n")
PY
	source_path="$plugin_dir"
fi

if [[ "$(basename "$(cd "$(dirname "$source_path")" && pwd)/$(basename "$source_path")")" != "$plugin_id" ]]; then
	printf 'Source folder must be named after the plugin id (%s).\n' "$plugin_id" >&2
	exit 1
fi

output_path="$dist_dir/${plugin_id}-${version}.zeron-plugin"
temporary_path="$dist_dir/.${plugin_id}-${version}.$$.zeron-plugin"
trap 'rm -f "$temporary_path"' EXIT

verification=""
if verification="$("$zeron_bin" plugin verify "$plugin_dir" 2>&1)"; then
	printf '%s\n' "$verification"
fi
if [[ "$verification" == *"Signed by you. Its files match."* ]]; then
	printf 'Using the valid signature already on %s %s.\n' "$plugin_id" "$version"
else
	printf 'Signing %s %s with Zeron…\n' "$plugin_id" "$version"
	printf 'Zeron may require an in-app permission review; the script will not bypass it.\n'
	"$zeron_bin" plugin sign "$plugin_id"
fi

printf 'Exporting signed package to %s…\n' "$output_path"
"$zeron_bin" plugin export "$plugin_id" --to "$temporary_path"

python3 - "$temporary_path" "$plugin_id" "$version" <<'PY'
import base64
import hashlib
import json
import sys
from pathlib import PurePosixPath

package_path, expected_id, expected_version = sys.argv[1:]
package = json.loads(open(package_path, "r", encoding="utf-8").read())
manifest = package.get("manifest", {})
signature = manifest.get("signature")
if package.get("format") != "zeron-plugin:v1":
    raise SystemExit("Zeron exported an unsupported package format")
if manifest.get("id") != expected_id or manifest.get("version") != expected_version:
    raise SystemExit("Exported package id/version does not match the source manifest")
if not isinstance(signature, dict) or not signature.get("key") or not signature.get("sig"):
    raise SystemExit("Exported package has no Zeron signature")
files = package.get("files", {})
signed_files = signature.get("files", {})
if set(files) != set(signed_files):
    raise SystemExit("Signature file list does not match package contents")
for name, encoded in files.items():
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"Unsafe package path: {name}")
    digest = hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
    if digest != signed_files[name]:
        raise SystemExit(f"Signature hash does not match packaged file: {name}")
print(f"Verified Zeron signature for {len(files)} files.")
PY

mv -f "$temporary_path" "$output_path"
trap - EXIT
printf 'Built and signed %s\n' "$output_path"
